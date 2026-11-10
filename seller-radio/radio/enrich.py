"""Enrichment stage: turn pooled release ids into full metadata plus playable videos.

The stage is deliberately incremental. Only pooled releases whose ``fetched_at`` column is
still NULL are considered, so a run can be interrupted at any point and resumed later without
repeating work or losing the rate limit budget already spent.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from radio.client import DiscogsClient
from radio.config import DEFAULT_CURRENCY
from radio.db import now_iso

logger = logging.getLogger(__name__)

YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
ARTIST_SUFFIX_PATTERN = re.compile(r"\s*\(\d+\)\s*$")
YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
YOUTUBE_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
YOUTUBE_PATH_PREFIXES: tuple[str, ...] = ("embed", "v", "shorts", "live")

_PENDING_WHERE = """
FROM pool AS p
LEFT JOIN releases AS r ON r.release_id = p.release_id
WHERE r.fetched_at IS NULL
"""

_PENDING_ORDER = """
ORDER BY CASE WHEN p.source = 'seller' THEN 0 ELSE 1 END, p.discovered_at, p.release_id
"""

_RELEASE_UPSERT = """
INSERT INTO releases (
    release_id, artist, title, label, catno, year, country, genres, styles, formats,
    format_descriptions, master_id, have, want, rating, rating_count, has_video,
    video_source, thumb, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(release_id) DO UPDATE SET
    artist = excluded.artist,
    title = excluded.title,
    label = excluded.label,
    catno = excluded.catno,
    year = excluded.year,
    country = excluded.country,
    genres = excluded.genres,
    styles = excluded.styles,
    formats = excluded.formats,
    format_descriptions = excluded.format_descriptions,
    master_id = excluded.master_id,
    have = excluded.have,
    want = excluded.want,
    rating = excluded.rating,
    rating_count = excluded.rating_count,
    has_video = excluded.has_video,
    video_source = excluded.video_source,
    thumb = excluded.thumb,
    fetched_at = excluded.fetched_at
"""

_RELEASE_FAILED_UPSERT = """
INSERT INTO releases (release_id, has_video, fetched_at) VALUES (?, 0, ?)
ON CONFLICT(release_id) DO UPDATE SET has_video = 0, fetched_at = excluded.fetched_at
"""

_VIDEO_UPSERT = """
INSERT INTO videos (release_id, video_id, title, duration, position)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(release_id, video_id) DO UPDATE SET
    title = excluded.title,
    duration = excluded.duration,
    position = excluded.position
"""


def extract_youtube_id(uri: str | None) -> str | None:
    """Pull the eleven character YouTube video id out of a video uri.

    Handles the watch, short link, and embed forms, tolerates extra query parameters,
    timestamps, mobile hosts, missing schemes, and trailing slashes.

    Args:
        uri: Video uri taken from a Discogs release or master payload.

    Returns:
        The validated eleven character video id, or None when the uri is empty, is not a
        YouTube link, or carries an id of the wrong shape.
    """
    if not uri or not isinstance(uri, str):
        return None

    candidate = uri.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None

    host = (parsed.netloc or "").lower().split(":")[0]
    segments = [segment for segment in parsed.path.split("/") if segment]

    if host in YOUTUBE_SHORT_HOSTS:
        return _validated(segments[0]) if segments else None

    if host not in YOUTUBE_HOSTS:
        return None

    query_values = parse_qs(parsed.query).get("v") or []
    if query_values:
        return _validated(query_values[0])

    if len(segments) >= 2 and segments[0].lower() in YOUTUBE_PATH_PREFIXES:
        return _validated(segments[1])

    return None


def pending_count(conn: sqlite3.Connection) -> int:
    """Count pooled releases that still need enrichment.

    Args:
        conn: Open SQLite connection.

    Returns:
        Number of pooled releases whose metadata has never been fetched.
    """
    row = conn.execute(f"SELECT COUNT(*) AS n {_PENDING_WHERE}").fetchone()
    return int(row["n"]) if row is not None else 0


def pending_release_ids(conn: sqlite3.Connection, limit: int) -> list[int]:
    """List the next pooled releases to enrich.

    Seller sourced releases are returned before search sourced ones because they are the
    records actually purchasable right now.

    Args:
        conn: Open SQLite connection.
        limit: Maximum number of release ids to return.

    Returns:
        Release ids in processing order, empty when nothing is pending or limit is not positive.
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        f"SELECT p.release_id AS release_id {_PENDING_WHERE} {_PENDING_ORDER} LIMIT ?",
        (limit,),
    ).fetchall()
    return [int(row["release_id"]) for row in rows]


def enrich_pending(
    conn: sqlite3.Connection,
    client: DiscogsClient,
    limit: int,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Fetch metadata and videos for the next batch of pooled releases.

    Each release costs one request. When a release carries no videos of its own and has a
    master, the master is fetched once and reused for every other pressing of that master seen
    in the same run. Releases that cannot be fetched are still stamped with a fetch timestamp so
    they are never retried in a loop.

    Args:
        conn: Open SQLite connection.
        client: Rate limited Discogs client.
        limit: Maximum number of releases to enrich in this run.
        progress: Optional callback invoked as progress(done, total) after each release.

    Returns:
        Mapping with processed, with_video, from_master, videos, failed, and requests counts.
    """
    release_ids = pending_release_ids(conn, limit)
    total = len(release_ids)
    start_requests = client.request_count
    master_cache: dict[int, list[dict]] = {}

    processed = 0
    with_video = 0
    from_master = 0
    video_rows = 0
    failed = 0

    for done, release_id in enumerate(release_ids, start=1):
        payload = client.get(f"/releases/{release_id}", {"curr_abbr": DEFAULT_CURRENCY})
        processed += 1

        if payload is None:
            failed += 1
            _mark_failed(conn, release_id)
            logger.info("Release %d could not be fetched, marked as done", release_id)
            if progress is not None:
                progress(done, total)
            continue

        try:
            videos, source = _resolve_videos(client, payload, master_cache)
            conn.execute(_RELEASE_UPSERT, _release_row(payload, release_id, videos, source))
            for video in videos:
                conn.execute(
                    _VIDEO_UPSERT,
                    (
                        release_id,
                        video["id"],
                        video["title"],
                        video["duration"],
                        video["position"],
                    ),
                )
            conn.commit()
        except (sqlite3.IntegrityError, sqlite3.InterfaceError, TypeError, ValueError):
            conn.rollback()
            failed += 1
            logger.warning(
                "Release %d could not be stored, marked as done", release_id, exc_info=True
            )
            _mark_failed(conn, release_id)
            if progress is not None:
                progress(done, total)
            continue

        if videos:
            with_video += 1
            video_rows += len(videos)
            if source == "master":
                from_master += 1

        if progress is not None:
            progress(done, total)

    return {
        "processed": processed,
        "with_video": with_video,
        "from_master": from_master,
        "videos": video_rows,
        "failed": failed,
        "requests": client.request_count - start_requests,
    }


def _mark_failed(conn: sqlite3.Connection, release_id: int) -> None:
    """Stamp a release as attempted so it is never retried in a loop.

    Args:
        conn: Open SQLite connection.
        release_id: Release identifier that could not be fetched or stored.

    Returns:
        None.
    """
    conn.execute(_RELEASE_FAILED_UPSERT, (release_id, now_iso()))
    conn.commit()


def _resolve_videos(
    client: DiscogsClient,
    payload: dict,
    master_cache: dict[int, list[dict]],
) -> tuple[list[dict], str | None]:
    """Pick the playable videos for a release, falling back to its master.

    Args:
        client: Rate limited Discogs client.
        payload: Decoded release payload.
        master_cache: Mutable per run cache of master videos keyed by master id.

    Returns:
        Tuple of parsed video rows and the origin of those videos, which is "release",
        "master", or None when nothing playable was found.
    """
    raw_videos = _video_list(payload)
    source = "release" if raw_videos else None
    master_id = _as_int(payload.get("master_id"))

    if not raw_videos and master_id is not None:
        raw_videos = _master_videos(client, master_id, master_cache)
        source = "master" if raw_videos else None

    videos = _parse_videos(raw_videos)
    return videos, source if videos else None


def _validated(value: str | None) -> str | None:
    """Accept a candidate video id only when it has the exact YouTube shape.

    Args:
        value: Raw path or query fragment believed to hold a video id.

    Returns:
        The id when it is eleven characters of the YouTube alphabet, None otherwise.
    """
    if not value:
        return None
    return value if YOUTUBE_ID_PATTERN.match(value) else None


def _master_videos(
    client: DiscogsClient,
    master_id: int,
    cache: dict[int, list[dict]],
) -> list[dict]:
    """Fetch a master's videos, reusing an in-process cache across pressings.

    Args:
        client: Rate limited Discogs client.
        master_id: Master release identifier.
        cache: Mutable per run cache keyed by master id.

    Returns:
        The master's raw video entries, empty when the master has none or cannot be fetched.
    """
    if master_id in cache:
        return cache[master_id]
    payload = client.get(f"/masters/{master_id}")
    videos = _video_list(payload) if payload is not None else []
    cache[master_id] = videos
    return videos


def _video_list(payload: dict | None) -> list[dict]:
    """Read the videos list out of a release or master payload.

    Args:
        payload: Decoded API payload, possibly without a videos key.

    Returns:
        List of raw video entries, empty when the key is absent or malformed.
    """
    if not isinstance(payload, dict):
        return []
    videos = payload.get("videos")
    if not isinstance(videos, list):
        return []
    return [entry for entry in videos if isinstance(entry, dict)]


def _parse_videos(raw_videos: list[dict]) -> list[dict]:
    """Convert raw video entries into deduplicated rows with their original ordering.

    Args:
        raw_videos: Video entries from a release or master payload.

    Returns:
        List of mappings with id, title, duration, and position keys.
    """
    parsed: list[dict] = []
    seen: set[str] = set()
    for position, entry in enumerate(raw_videos):
        video_id = extract_youtube_id(entry.get("uri"))
        if video_id is None or video_id in seen:
            continue
        seen.add(video_id)
        parsed.append(
            {
                "id": video_id,
                "title": _as_text(entry.get("title")),
                "duration": _as_int(entry.get("duration")),
                "position": position,
            }
        )
    return parsed


def _release_row(
    payload: dict,
    release_id: int,
    videos: list[dict],
    source: str | None,
) -> tuple:
    """Flatten a release payload into the releases table column order.

    Args:
        payload: Decoded release payload.
        release_id: Release identifier used as the primary key.
        videos: Parsed video rows for this release.
        source: Origin of the videos, either "release", "master", or None.

    Returns:
        Tuple of column values matching the releases upsert statement.
    """
    community = payload.get("community")
    community = community if isinstance(community, dict) else {}
    rating = community.get("rating")
    rating = rating if isinstance(rating, dict) else {}
    label_name, catno = _label(payload)
    formats, descriptions = _formats(payload)

    return (
        release_id,
        _artist(payload),
        _as_text(payload.get("title")),
        label_name,
        catno,
        _as_int(payload.get("year")),
        _as_text(payload.get("country")),
        json.dumps(_str_list(payload.get("genres"))),
        json.dumps(_str_list(payload.get("styles"))),
        json.dumps(formats),
        json.dumps(descriptions),
        _as_int(payload.get("master_id")),
        _as_int(community.get("have")),
        _as_int(community.get("want")),
        _as_float(rating.get("average")),
        _as_int(rating.get("count")),
        1 if videos else 0,
        source,
        _as_text(payload.get("thumb")),
        now_iso(),
    )


def _artist(payload: dict) -> str | None:
    """Build a display artist string from the credited artists.

    The Discogs numeric disambiguation suffix, as in "Alton (2)", is stripped.

    Args:
        payload: Decoded release payload.

    Returns:
        Comma separated artist names, or None when no artist is credited.
    """
    artists = payload.get("artists")
    if not isinstance(artists, list):
        return None
    names: list[str] = []
    for artist in artists:
        if not isinstance(artist, dict):
            continue
        name = _as_text(artist.get("name"))
        if name is None:
            continue
        cleaned = ARTIST_SUFFIX_PATTERN.sub("", name).strip()
        if cleaned:
            names.append(cleaned)
    return ", ".join(names) if names else None


def _label(payload: dict) -> tuple[str | None, str | None]:
    """Read the primary label name and catalogue number.

    Args:
        payload: Decoded release payload.

    Returns:
        Tuple of label name and catalogue number, each None when unavailable.
    """
    labels = payload.get("labels")
    if not isinstance(labels, list):
        return None, None
    for label in labels:
        if isinstance(label, dict):
            return _as_text(label.get("name")), _as_text(label.get("catno"))
    return None, None


def _formats(payload: dict) -> tuple[list[str], list[str]]:
    """Collect format names and the flattened format descriptions.

    Args:
        payload: Decoded release payload.

    Returns:
        Tuple of unique format names and unique descriptions, both in payload order.
    """
    names: list[str] = []
    descriptions: list[str] = []
    entries = payload.get("formats")
    if not isinstance(entries, list):
        return names, descriptions
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = _as_text(entry.get("name"))
        if name is not None and name not in names:
            names.append(name)
        for description in _str_list(entry.get("descriptions")):
            if description not in descriptions:
                descriptions.append(description)
    return names, descriptions


def _str_list(value: Any) -> list[str]:
    """Normalize a payload value into a list of non empty strings.

    Args:
        value: Candidate list from the payload.

    Returns:
        List of trimmed strings, empty when the value is not a list of strings.
    """
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = _as_text(item)
        if text is not None:
            items.append(text)
    return items


def _as_text(value: Any) -> str | None:
    """Coerce a payload value into a trimmed string.

    Args:
        value: Candidate value.

    Returns:
        The trimmed string, or None when the value is not a non empty string.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _as_int(value: Any) -> int | None:
    """Coerce a payload value into an integer.

    Args:
        value: Candidate value.

    Returns:
        The integer value, or None when it cannot be represented as one.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    """Coerce a payload value into a float.

    Args:
        value: Candidate value.

    Returns:
        The float value, or None when it cannot be represented as one.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
