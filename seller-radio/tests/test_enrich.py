"""Tests for the enrichment stage: uri parsing, metadata persistence, and resumability."""

from __future__ import annotations

import json
import sqlite3

import pytest

from radio import enrich
from radio.db import now_iso

VIDEO_ID = "dQw4w9WgXcQ"
OTHER_ID = "kJQP7kiw5Fk"


class FakeClient:
    """Deterministic stand in for DiscogsClient that never touches the network.

    Attributes:
        calls: Ordered list of requested paths.
    """

    def __init__(self, payloads: dict[str, dict | None]) -> None:
        """Builds a fake client backed by a fixed path to payload mapping.

        Args:
            payloads: Mapping of request path to the payload returned for it. A missing
                path or an explicit None value simulates a 404 or 403 soft failure.
        """
        self._payloads = payloads
        self.calls: list[str] = []

    def get(self, path: str, params: dict | None = None) -> dict | None:
        """Returns the canned payload for a path and records the call.

        Args:
            path: Requested API path.
            params: Ignored query parameters.

        Returns:
            The configured payload, or None when the path was not configured.
        """
        self.calls.append(path)
        return self._payloads.get(path)

    @property
    def request_count(self) -> int:
        """Returns the number of requests issued so far.

        Returns:
            Count of recorded calls.
        """
        return len(self.calls)


def _pool(conn: sqlite3.Connection, release_id: int, source: str = "seller") -> None:
    """Inserts a pooled release id.

    Args:
        conn: Open SQLite connection.
        release_id: Release identifier to pool.
        source: Discovery source, either "seller" or "search".

    Returns:
        None.
    """
    conn.execute(
        "INSERT INTO pool (release_id, source, bank_key, discovered_at) VALUES (?, ?, NULL, ?)",
        (release_id, source, now_iso()),
    )
    conn.commit()


def _release_payload(release_id: int, **overrides: object) -> dict:
    """Builds a release payload shaped like the live API response.

    Args:
        release_id: Release identifier.
        **overrides: Keys replacing the defaults.

    Returns:
        A release payload dictionary.
    """
    payload: dict = {
        "id": release_id,
        "title": "Sunrise EP",
        "year": 1996,
        "country": "Germany",
        "genres": ["Electronic"],
        "styles": ["Deep House", "Techno"],
        "artists": [{"name": "Alton (2)"}],
        "labels": [{"name": "Peacefrog Records", "catno": "PF 042"}],
        "formats": [{"name": "Vinyl", "qty": "1", "descriptions": ['12"', "EP"]}],
        "master_id": None,
        "thumb": "https://img.discogs.com/thumb.jpg",
        "community": {"have": 12, "want": 7, "rating": {"count": 2, "average": 4.5}},
        "videos": [
            {"uri": f"https://www.youtube.com/watch?v={VIDEO_ID}", "title": "A1", "duration": 320}
        ],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (f"https://www.youtube.com/watch?v={VIDEO_ID}", VIDEO_ID),
        (f"http://www.youtube.com/watch?v={VIDEO_ID}", VIDEO_ID),
        (f"https://m.youtube.com/watch?v={VIDEO_ID}&t=30s", VIDEO_ID),
        (f"https://www.youtube.com/watch?feature=share&v={VIDEO_ID}&list=PL1", VIDEO_ID),
        (f"https://youtu.be/{VIDEO_ID}", VIDEO_ID),
        (f"https://youtu.be/{VIDEO_ID}/", VIDEO_ID),
        (f"https://youtu.be/{VIDEO_ID}?t=42", VIDEO_ID),
        (f"https://www.youtube.com/embed/{VIDEO_ID}", VIDEO_ID),
        (f"https://www.youtube.com/embed/{VIDEO_ID}?start=10", VIDEO_ID),
        (f"www.youtube.com/watch?v={VIDEO_ID}", VIDEO_ID),
        (f"https://youtube.com/shorts/{OTHER_ID}", OTHER_ID),
        ("https://vimeo.com/123456789", None),
        ("https://soundcloud.com/artist/track", None),
        ("https://www.youtube.com/watch?v=tooshort", None),
        ("https://www.youtube.com/watch?v=waaaaaaaaytoolong", None),
        ("https://youtu.be/short", None),
        ("https://www.youtube.com/watch", None),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_extract_youtube_id(uri: str | None, expected: str | None) -> None:
    """Verifies every supported YouTube uri form and the rejected cases.

    Args:
        uri: Candidate video uri.
        expected: Expected extracted id.

    Returns:
        None.
    """
    assert enrich.extract_youtube_id(uri) == expected


@pytest.mark.parametrize(
    ("pooled", "prefetched", "expected"),
    [
        ([], [], 0),
        ([101], [], 1),
        ([101, 102, 103], [], 3),
        ([101, 102, 103], [102], 2),
        ([101, 102], [101, 102], 0),
    ],
)
def test_pending_count(
    conn: sqlite3.Connection,
    pooled: list[int],
    prefetched: list[int],
    expected: int,
) -> None:
    """Verifies that only pooled releases without a fetch timestamp are pending.

    Args:
        conn: Initialized in memory database.
        pooled: Release ids inserted into the pool.
        prefetched: Release ids already carrying a fetch timestamp.
        expected: Expected pending count.

    Returns:
        None.
    """
    for release_id in pooled:
        _pool(conn, release_id)
    for release_id in prefetched:
        conn.execute(
            "INSERT INTO releases (release_id, fetched_at) VALUES (?, ?)",
            (release_id, now_iso()),
        )
    conn.commit()

    assert enrich.pending_count(conn) == expected
    assert len(enrich.pending_release_ids(conn, 100)) == expected


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(0, 0), (-1, 0), (1, 1), (2, 2), (5, 3)],
)
def test_pending_release_ids_respects_limit(
    conn: sqlite3.Connection,
    limit: int,
    expected: int,
) -> None:
    """Verifies the batch size ceiling and the non positive limit guard.

    Args:
        conn: Initialized in memory database.
        limit: Requested batch size.
        expected: Expected number of returned ids.

    Returns:
        None.
    """
    for release_id in (101, 102, 103):
        _pool(conn, release_id)

    assert len(enrich.pending_release_ids(conn, limit)) == expected


def test_pending_release_ids_prefers_seller_source(conn: sqlite3.Connection) -> None:
    """Verifies that purchasable seller releases are enriched before search results.

    Args:
        conn: Initialized in memory database.

    Returns:
        None.
    """
    _pool(conn, 201, source="search")
    _pool(conn, 202, source="seller")

    assert enrich.pending_release_ids(conn, 2)[0] == 202


def test_enrich_pending_persists_metadata_and_videos(conn: sqlite3.Connection) -> None:
    """Verifies that a successful fetch writes every metadata column and its video rows.

    Args:
        conn: Initialized in memory database.

    Returns:
        None.
    """
    _pool(conn, 101)
    client = FakeClient({"/releases/101": _release_payload(101)})

    result = enrich.enrich_pending(conn, client, 10)

    assert result == {
        "processed": 1,
        "with_video": 1,
        "from_master": 0,
        "videos": 1,
        "failed": 0,
        "requests": 1,
    }

    row = conn.execute("SELECT * FROM releases WHERE release_id = 101").fetchone()
    assert row["artist"] == "Alton"
    assert row["title"] == "Sunrise EP"
    assert row["label"] == "Peacefrog Records"
    assert row["catno"] == "PF 042"
    assert row["year"] == 1996
    assert row["country"] == "Germany"
    assert json.loads(row["genres"]) == ["Electronic"]
    assert json.loads(row["styles"]) == ["Deep House", "Techno"]
    assert json.loads(row["formats"]) == ["Vinyl"]
    assert json.loads(row["format_descriptions"]) == ['12"', "EP"]
    assert row["have"] == 12
    assert row["want"] == 7
    assert row["rating"] == 4.5
    assert row["rating_count"] == 2
    assert row["has_video"] == 1
    assert row["video_source"] == "release"
    assert row["fetched_at"] is not None

    videos = conn.execute("SELECT * FROM videos WHERE release_id = 101").fetchall()
    assert [(v["video_id"], v["position"], v["duration"]) for v in videos] == [(VIDEO_ID, 0, 320)]


@pytest.mark.parametrize(
    ("artists", "expected"),
    [
        ([{"name": "Alton (2)"}], "Alton"),
        ([{"name": "Model 500"}], "Model 500"),
        (
            [{"name": "Mood II Swing (3)"}, {"name": "Kerri Chandler"}],
            "Mood II Swing, Kerri Chandler",
        ),
        ([], None),
        ("not-a-list", None),
    ],
)
def test_enrich_pending_normalizes_artist(
    conn: sqlite3.Connection,
    artists: object,
    expected: str | None,
) -> None:
    """Verifies the Discogs numeric disambiguation suffix is stripped from artist names.

    Args:
        conn: Initialized in memory database.
        artists: Artists block from the payload.
        expected: Expected stored artist string.

    Returns:
        None.
    """
    _pool(conn, 101)
    client = FakeClient({"/releases/101": _release_payload(101, artists=artists)})

    enrich.enrich_pending(conn, client, 10)

    row = conn.execute("SELECT artist FROM releases WHERE release_id = 101").fetchone()
    assert row["artist"] == expected


@pytest.mark.parametrize(
    ("release_videos", "master_id", "expected_source", "expected_from_master"),
    [
        ([{"uri": f"https://youtu.be/{VIDEO_ID}"}], 900, "release", 0),
        ([], 900, "master", 1),
        ([], None, None, 0),
        ([{"uri": "https://vimeo.com/1"}], None, None, 0),
    ],
)
def test_enrich_pending_master_fallback(
    conn: sqlite3.Connection,
    release_videos: list[dict],
    master_id: int | None,
    expected_source: str | None,
    expected_from_master: int,
) -> None:
    """Verifies the master fallback only fires when the release itself has no videos.

    Args:
        conn: Initialized in memory database.
        release_videos: Videos attached to the release payload.
        master_id: Master identifier on the release payload.
        expected_source: Expected video_source column value.
        expected_from_master: Expected from_master counter.

    Returns:
        None.
    """
    _pool(conn, 101)
    client = FakeClient(
        {
            "/releases/101": _release_payload(101, videos=release_videos, master_id=master_id),
            "/masters/900": {"videos": [{"uri": f"https://youtu.be/{OTHER_ID}", "title": "B1"}]},
        }
    )

    result = enrich.enrich_pending(conn, client, 10)

    row = conn.execute(
        "SELECT video_source, has_video FROM releases WHERE release_id = 101"
    ).fetchone()
    assert row["video_source"] == expected_source
    assert row["has_video"] == (1 if expected_source else 0)
    assert result["from_master"] == expected_from_master


def test_enrich_pending_caches_master_across_releases(conn: sqlite3.Connection) -> None:
    """Verifies two pressings sharing a master cost exactly one masters request.

    Args:
        conn: Initialized in memory database.

    Returns:
        None.
    """
    _pool(conn, 101)
    _pool(conn, 102)
    client = FakeClient(
        {
            "/releases/101": _release_payload(101, videos=[], master_id=900),
            "/releases/102": _release_payload(102, videos=[], master_id=900),
            "/masters/900": {"videos": [{"uri": f"https://youtu.be/{OTHER_ID}", "title": "B1"}]},
        }
    )

    result = enrich.enrich_pending(conn, client, 10)

    assert client.calls.count("/masters/900") == 1
    assert client.calls == ["/releases/101", "/masters/900", "/releases/102"]
    assert result["requests"] == 3
    assert result["from_master"] == 2
    assert result["with_video"] == 2
    assert result["videos"] == 2


def test_enrich_pending_marks_failed_release_as_fetched(conn: sqlite3.Connection) -> None:
    """Verifies a soft failing release is stamped so it is never retried in a loop.

    Args:
        conn: Initialized in memory database.

    Returns:
        None.
    """
    _pool(conn, 101)
    client = FakeClient({"/releases/101": None})

    result = enrich.enrich_pending(conn, client, 10)

    assert result["failed"] == 1
    assert result["processed"] == 1
    assert result["with_video"] == 0

    row = conn.execute(
        "SELECT fetched_at, has_video FROM releases WHERE release_id = 101"
    ).fetchone()
    assert row["fetched_at"] is not None
    assert row["has_video"] == 0

    assert enrich.pending_count(conn) == 0
    second = enrich.enrich_pending(conn, client, 10)
    assert second["processed"] == 0
    assert second["requests"] == 0
    assert client.calls == ["/releases/101"]


def test_enrich_pending_failure_keeps_existing_display_metadata(conn: sqlite3.Connection) -> None:
    """Verifies a failed fetch does not erase listing sourced artist and title values.

    Args:
        conn: Initialized in memory database.

    Returns:
        None.
    """
    _pool(conn, 101)
    conn.execute(
        "INSERT INTO releases (release_id, artist, title) VALUES (?, ?, ?)",
        (101, "Alton", "Sunrise EP"),
    )
    conn.commit()
    client = FakeClient({"/releases/101": None})

    enrich.enrich_pending(conn, client, 10)

    row = conn.execute(
        "SELECT artist, title, fetched_at FROM releases WHERE release_id = 101"
    ).fetchone()
    assert row["artist"] == "Alton"
    assert row["title"] == "Sunrise EP"
    assert row["fetched_at"] is not None


def test_enrich_pending_is_resumable_across_batches(conn: sqlite3.Connection) -> None:
    """Verifies successive limited runs advance through the pool without repeating work.

    Args:
        conn: Initialized in memory database.

    Returns:
        None.
    """
    for release_id in (101, 102, 103):
        _pool(conn, release_id)
    client = FakeClient({f"/releases/{rid}": _release_payload(rid) for rid in (101, 102, 103)})

    first = enrich.enrich_pending(conn, client, 2)
    assert first["processed"] == 2
    assert enrich.pending_count(conn) == 1

    second = enrich.enrich_pending(conn, client, 2)
    assert second["processed"] == 1
    assert enrich.pending_count(conn) == 0
    assert sorted(client.calls) == ["/releases/101", "/releases/102", "/releases/103"]


def test_enrich_pending_deduplicates_and_preserves_position(conn: sqlite3.Connection) -> None:
    """Verifies duplicate ids collapse and the source ordering index is stored.

    Args:
        conn: Initialized in memory database.

    Returns:
        None.
    """
    _pool(conn, 101)
    videos = [
        {"uri": "https://soundcloud.com/x/y", "title": "skip"},
        {"uri": f"https://youtu.be/{VIDEO_ID}", "title": "A1", "duration": 300},
        {"uri": f"https://www.youtube.com/watch?v={VIDEO_ID}", "title": "A1 dupe"},
        {"uri": f"https://www.youtube.com/embed/{OTHER_ID}", "title": "B1", "duration": 240},
    ]
    client = FakeClient({"/releases/101": _release_payload(101, videos=videos)})

    result = enrich.enrich_pending(conn, client, 10)

    rows = conn.execute(
        "SELECT video_id, position FROM videos WHERE release_id = 101 ORDER BY position"
    ).fetchall()
    assert [(row["video_id"], row["position"]) for row in rows] == [(VIDEO_ID, 1), (OTHER_ID, 3)]
    assert result["videos"] == 2


@pytest.mark.parametrize(
    ("limit", "expected_calls"),
    [(1, [(1, 1)]), (2, [(1, 2), (2, 2)]), (3, [(1, 3), (2, 3), (3, 3)])],
)
def test_enrich_pending_reports_progress(
    conn: sqlite3.Connection,
    limit: int,
    expected_calls: list[tuple[int, int]],
) -> None:
    """Verifies the progress callback receives done and total for every release.

    Args:
        conn: Initialized in memory database.
        limit: Batch size for the run.
        expected_calls: Expected sequence of progress callback arguments.

    Returns:
        None.
    """
    for release_id in (101, 102, 103):
        _pool(conn, release_id)
    client = FakeClient({f"/releases/{rid}": _release_payload(rid) for rid in (101, 102, 103)})
    seen: list[tuple[int, int]] = []

    enrich.enrich_pending(
        conn, client, limit, progress=lambda done, total: seen.append((done, total))
    )

    assert seen == expected_calls
