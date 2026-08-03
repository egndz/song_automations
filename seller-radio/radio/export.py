"""Crate export stage: flatten the database into the JSON payload the web player consumes.

The exporter is the join point of the whole pipeline. Listings supply price and condition,
releases supply display metadata, videos supply playability, and the pool table records how a
release entered the system in the first place. A release only reaches the crate when it has at
least one video that is not marked dead, because a track with nothing to play is dead weight in
the player.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from radio.config import CONDITION_TO_GRADE
from radio.db import now_iso

_BEST_LISTING = """
SELECT
    release_id,
    listing_id,
    seller,
    price,
    currency,
    condition,
    sleeve,
    ships_from,
    listing_uri
FROM (
    SELECT
        l.release_id AS release_id,
        l.listing_id AS listing_id,
        l.seller AS seller,
        l.price AS price,
        l.currency AS currency,
        l.condition AS condition,
        l.sleeve AS sleeve,
        l.ships_from AS ships_from,
        l.listing_uri AS listing_uri,
        ROW_NUMBER() OVER (
            PARTITION BY l.release_id
            ORDER BY
                CASE WHEN l.price IS NULL THEN 1 ELSE 0 END,
                l.price,
                l.listing_id
        ) AS rank_in_release
    FROM listings AS l
    WHERE l.active = 1
)
WHERE rank_in_release = 1
"""

_TRACK_QUERY = f"""
WITH best AS ({_BEST_LISTING})
SELECT
    r.release_id AS release_id,
    r.artist AS artist,
    r.title AS title,
    r.label AS label,
    r.catno AS catno,
    r.year AS year,
    r.country AS country,
    r.genres AS genres,
    r.styles AS styles,
    r.formats AS formats,
    r.format_descriptions AS format_descriptions,
    r.master_id AS master_id,
    r.have AS have,
    r.want AS want,
    r.rating AS rating,
    r.rating_count AS rating_count,
    r.thumb AS thumb,
    b.listing_id AS listing_id,
    b.seller AS seller,
    b.price AS price,
    b.currency AS currency,
    b.condition AS condition,
    b.sleeve AS sleeve,
    b.ships_from AS ships_from,
    b.listing_uri AS listing_uri,
    p.source AS pool_source,
    CASE WHEN o.release_id IS NULL THEN 0 ELSE 1 END AS owned,
    CASE WHEN w.release_id IS NULL THEN 0 ELSE 1 END AS wanted
FROM releases AS r
LEFT JOIN best AS b ON b.release_id = r.release_id
LEFT JOIN pool AS p ON p.release_id = r.release_id
LEFT JOIN owned AS o ON o.release_id = r.release_id
LEFT JOIN wantlist AS w ON w.release_id = r.release_id
WHERE EXISTS (
    SELECT 1 FROM videos AS v WHERE v.release_id = r.release_id AND v.dead = 0
)
"""

_VIDEO_QUERY = """
SELECT release_id, video_id, title, duration
FROM videos
WHERE dead = 0
ORDER BY release_id, CASE WHEN position IS NULL THEN 1 ELSE 0 END, position, video_id
"""


def _decode_list(value: Any) -> list[str]:
    """Decode a JSON array column into a list of strings.

    Args:
        value: Raw column value, which may be NULL, malformed JSON, or a JSON array.

    Returns:
        The decoded list of strings, or an empty list when the value is missing, not valid
        JSON, or not a JSON array.
    """
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if item is not None]


def _grade(condition: Any) -> str | None:
    """Map a Discogs condition string onto a short grade.

    Args:
        condition: Discogs condition vocabulary value such as "Near Mint (NM or M-)".

    Returns:
        The short grade such as "NM", or None when the condition is missing or ungraded.
    """
    if not isinstance(condition, str):
        return None
    return CONDITION_TO_GRADE.get(condition)


def _want_ratio(have: Any, want: Any) -> float | None:
    """Compute the want to have ratio for a release.

    Args:
        have: Number of collectors who own the release.
        want: Number of collectors who want the release.

    Returns:
        The ratio rounded to four decimal places, or None when either side is missing or when
        have is zero.
    """
    if have is None or want is None:
        return None
    try:
        have_value = float(have)
        want_value = float(want)
    except (TypeError, ValueError):
        return None
    if have_value == 0:
        return None
    return round(want_value / have_value, 4)


def _text(value: Any) -> str:
    """Coerce a nullable text column into a plain string.

    Args:
        value: Raw column value.

    Returns:
        The value as a string, or an empty string when the value is NULL.
    """
    return "" if value is None else str(value)


def _optional_text(value: Any) -> str | None:
    """Coerce a nullable text column into a string while preserving NULL.

    Args:
        value: Raw column value.

    Returns:
        The value as a string, or None when the value is NULL.
    """
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    """Coerce a nullable numeric column into an integer while preserving NULL.

    Args:
        value: Raw column value.

    Returns:
        The value as an integer, or None when the value is NULL or not numeric.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    """Coerce a nullable numeric column into a float while preserving NULL.

    Args:
        value: Raw column value.

    Returns:
        The value as a float, or None when the value is NULL or not numeric.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _videos_by_release(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """Group every playable video by its release.

    Args:
        conn: Open SQLite connection.

    Returns:
        Mapping of release id to an ordered list of video dicts with id, title, and duration.
    """
    grouped: dict[int, list[dict]] = {}
    for row in conn.execute(_VIDEO_QUERY):
        release_id = int(row["release_id"])
        grouped.setdefault(release_id, []).append(
            {
                "id": str(row["video_id"]),
                "title": _text(row["title"]),
                "duration": _optional_int(row["duration"]),
            }
        )
    return grouped


def _sellers(conn: sqlite3.Connection) -> list[str]:
    """List every seller that appears in the listings table.

    Args:
        conn: Open SQLite connection.

    Returns:
        Sorted list of distinct seller names.
    """
    rows = conn.execute(
        "SELECT DISTINCT seller FROM listings WHERE seller IS NOT NULL ORDER BY seller"
    )
    return [str(row["seller"]) for row in rows]


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    """Run a single value count query.

    Args:
        conn: Open SQLite connection.
        sql: Query returning exactly one numeric column in one row.

    Returns:
        The value as an integer, or zero when the query returns nothing.
    """
    row = conn.execute(sql).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _track_from_row(row: sqlite3.Row, videos: list[dict]) -> dict:
    """Build one crate track from a joined database row.

    Args:
        row: Joined row of release, best listing, pool, owned, and wantlist columns.
        videos: Playable videos already ordered for this release.

    Returns:
        A crate track dict matching the exported JSON shape.
    """
    release_id = int(row["release_id"])
    pool_source = _optional_text(row["pool_source"])
    if pool_source not in ("seller", "search"):
        pool_source = "seller" if row["listing_id"] is not None else "search"
    return {
        "release_id": release_id,
        "listing_id": _optional_int(row["listing_id"]),
        "seller": _optional_text(row["seller"]),
        "price": _optional_float(row["price"]),
        "currency": _optional_text(row["currency"]),
        "condition": _optional_text(row["condition"]),
        "condition_grade": _grade(row["condition"]),
        "sleeve": _optional_text(row["sleeve"]),
        "sleeve_grade": _grade(row["sleeve"]),
        "ships_from": _optional_text(row["ships_from"]),
        "listing_uri": _optional_text(row["listing_uri"]),
        "discogs_uri": f"https://www.discogs.com/release/{release_id}",
        "artist": _text(row["artist"]),
        "title": _text(row["title"]),
        "label": _optional_text(row["label"]),
        "catno": _optional_text(row["catno"]),
        "year": _optional_int(row["year"]),
        "country": _optional_text(row["country"]),
        "genres": _decode_list(row["genres"]),
        "styles": _decode_list(row["styles"]),
        "formats": _decode_list(row["formats"]),
        "format_descriptions": _decode_list(row["format_descriptions"]),
        "master_id": _optional_int(row["master_id"]),
        "have": _optional_int(row["have"]),
        "want": _optional_int(row["want"]),
        "want_ratio": _want_ratio(row["have"], row["want"]),
        "rating": _optional_float(row["rating"]),
        "rating_count": _optional_int(row["rating_count"]),
        "thumb": _optional_text(row["thumb"]),
        "videos": videos,
        "source": pool_source,
        "owned": bool(row["owned"]),
        "wanted": bool(row["wanted"]),
    }


def build_crate(conn: sqlite3.Connection) -> dict:
    """Assemble the full crate payload from the database.

    Only releases with at least one video that is not marked dead are exported. When several
    active listings point at the same release, the cheapest one wins so the crate holds exactly
    one row per release. Releases discovered by search and never listed still appear, with a
    null price and null condition.

    Args:
        conn: Open SQLite connection.

    Returns:
        The crate payload with generated_at, sellers, stats, and a deterministically sorted
        tracks list.
    """
    videos = _videos_by_release(conn)
    tracks = [
        _track_from_row(row, videos.get(int(row["release_id"]), []))
        for row in conn.execute(_TRACK_QUERY)
    ]
    tracks.sort(
        key=lambda track: (
            track["artist"].lower(),
            track["title"].lower(),
            track["release_id"],
        )
    )

    with_video_sql = "SELECT COUNT(DISTINCT release_id) FROM videos WHERE dead = 0"
    stats = {
        "tracks": len(tracks),
        "releases": _scalar(conn, "SELECT COUNT(*) FROM releases"),
        "with_video": _scalar(conn, with_video_sql),
        "listings_active": _scalar(conn, "SELECT COUNT(*) FROM listings WHERE active = 1"),
    }

    return {
        "generated_at": now_iso(),
        "sellers": _sellers(conn),
        "stats": stats,
        "tracks": tracks,
    }


def write_crate(conn: sqlite3.Connection, path: Path) -> dict:
    """Build the crate and write it as a browser loadable JavaScript file.

    The file assigns the payload to window.CRATE so the player works over file:// with no
    server and no fetch call.

    Args:
        conn: Open SQLite connection.
        path: Destination file path. Parent directories are created when missing.

    Returns:
        The stats dict from the generated crate.
    """
    crate = build_crate(conn)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(crate, ensure_ascii=False, indent=2)
    target.write_text(f"window.CRATE = {payload};\n", encoding="utf-8")
    return crate["stats"]
