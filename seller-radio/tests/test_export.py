"""Tests for the crate exporter."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from radio import export
from radio.db import now_iso


def _add_release(conn: sqlite3.Connection, release_id: int, **overrides: Any) -> None:
    """Insert a release row with sensible defaults.

    Args:
        conn: Open SQLite connection.
        release_id: Release identifier.
        **overrides: Column values overriding the defaults.

    Returns:
        None.
    """
    values: dict[str, Any] = {
        "artist": f"Artist {release_id}",
        "title": f"Title {release_id}",
        "label": "Label",
        "catno": "CAT001",
        "year": 1995,
        "country": "Germany",
        "genres": json.dumps(["Electronic"]),
        "styles": json.dumps(["Techno"]),
        "formats": json.dumps(["Vinyl"]),
        "format_descriptions": json.dumps(['12"']),
        "master_id": 900 + release_id,
        "have": 10,
        "want": 5,
        "rating": 4.5,
        "rating_count": 2,
        "has_video": 1,
        "video_source": "release",
        "thumb": "https://img/thumb.jpg",
        "fetched_at": now_iso(),
    }
    values.update(overrides)
    columns = ["release_id", *values]
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO releases ({', '.join(columns)}) VALUES ({placeholders})",
        (release_id, *values.values()),
    )
    conn.commit()


def _add_video(conn: sqlite3.Connection, release_id: int, video_id: str, **overrides: Any) -> None:
    """Insert a video row with sensible defaults.

    Args:
        conn: Open SQLite connection.
        release_id: Release identifier the video belongs to.
        video_id: YouTube video identifier.
        **overrides: Column values overriding the defaults.

    Returns:
        None.
    """
    values: dict[str, Any] = {
        "title": f"Video {video_id}",
        "duration": 321,
        "position": 0,
        "dead": 0,
    }
    values.update(overrides)
    conn.execute(
        "INSERT INTO videos (release_id, video_id, title, duration, position, dead) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            release_id,
            video_id,
            values["title"],
            values["duration"],
            values["position"],
            values["dead"],
        ),
    )
    conn.commit()


def _add_listing(
    conn: sqlite3.Connection, listing_id: int, release_id: int, **overrides: Any
) -> None:
    """Insert a listing row with sensible defaults.

    Args:
        conn: Open SQLite connection.
        listing_id: Listing identifier.
        release_id: Release identifier the listing points at.
        **overrides: Column values overriding the defaults.

    Returns:
        None.
    """
    values: dict[str, Any] = {
        "seller": "allcitydublin",
        "price": 19.03,
        "currency": "EUR",
        "condition": "Near Mint (NM or M-)",
        "sleeve": "Very Good Plus (VG+)",
        "ships_from": "Ireland",
        "listing_uri": f"https://www.discogs.com/sell/item/{listing_id}",
        "comments": None,
        "posted": now_iso(),
        "active": 1,
        "last_seen": now_iso(),
    }
    values.update(overrides)
    columns = ["listing_id", "release_id", *values]
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO listings ({', '.join(columns)}) VALUES ({placeholders})",
        (listing_id, release_id, *values.values()),
    )
    conn.commit()


def _add_pool(conn: sqlite3.Connection, release_id: int, source: str) -> None:
    """Insert a pool row.

    Args:
        conn: Open SQLite connection.
        release_id: Release identifier.
        source: Discovery source, either "seller" or "search".

    Returns:
        None.
    """
    conn.execute(
        "INSERT INTO pool (release_id, source, bank_key, discovered_at) VALUES (?, ?, ?, ?)",
        (release_id, source, None, now_iso()),
    )
    conn.commit()


def _track_by_id(crate: dict, release_id: int) -> dict | None:
    """Find one exported track by release id.

    Args:
        crate: Crate payload from build_crate.
        release_id: Release identifier to look for.

    Returns:
        The matching track dict, or None when the release was not exported.
    """
    for track in crate["tracks"]:
        if track["release_id"] == release_id:
            return track
    return None


@pytest.mark.parametrize(
    ("videos", "expect_exported"),
    [
        ([], False),
        ([("aaaaaaaaaaa", 1)], False),
        ([("aaaaaaaaaaa", 1), ("bbbbbbbbbbb", 1)], False),
        ([("aaaaaaaaaaa", 0)], True),
        ([("aaaaaaaaaaa", 1), ("bbbbbbbbbbb", 0)], True),
    ],
)
def test_only_releases_with_playable_video_are_exported(
    conn: sqlite3.Connection, videos: list[tuple[str, int]], expect_exported: bool
) -> None:
    """Releases without a live video are dropped from the crate.

    Args:
        conn: In memory database fixture.
        videos: Video identifier and dead flag pairs to seed.
        expect_exported: Whether the release should reach the crate.

    Returns:
        None.
    """
    _add_release(conn, 100)
    _add_pool(conn, 100, "search")
    for video_id, dead in videos:
        _add_video(conn, 100, video_id, dead=dead)

    crate = export.build_crate(conn)

    assert (_track_by_id(crate, 100) is not None) is expect_exported
    assert crate["stats"]["tracks"] == (1 if expect_exported else 0)


@pytest.mark.parametrize(
    ("prices", "expected_listing_id", "expected_price"),
    [
        ([(1, 25.0), (2, 12.5)], 2, 12.5),
        ([(1, 12.5), (2, 25.0)], 1, 12.5),
        ([(1, 12.5), (2, 12.5)], 1, 12.5),
        ([(1, None), (2, 30.0)], 2, 30.0),
        ([(1, 8.0), (2, 9.0), (3, 7.25)], 3, 7.25),
    ],
)
def test_cheapest_active_listing_wins(
    conn: sqlite3.Connection,
    prices: list[tuple[int, float | None]],
    expected_listing_id: int,
    expected_price: float | None,
) -> None:
    """Several sellers on one release collapse into the cheapest active listing.

    Args:
        conn: In memory database fixture.
        prices: Listing identifier and price pairs to seed.
        expected_listing_id: Listing identifier expected to win.
        expected_price: Price expected on the exported track.

    Returns:
        None.
    """
    _add_release(conn, 200)
    _add_pool(conn, 200, "seller")
    _add_video(conn, 200, "vvvvvvvvvvv")
    for listing_id, price in prices:
        _add_listing(conn, listing_id, 200, price=price, seller=f"seller{listing_id}")

    crate = export.build_crate(conn)

    assert len(crate["tracks"]) == 1
    track = crate["tracks"][0]
    assert track["listing_id"] == expected_listing_id
    assert track["price"] == expected_price
    assert track["seller"] == f"seller{expected_listing_id}"


@pytest.mark.parametrize(
    ("active_flags", "expected_listing_id"),
    [
        ([(1, 5.0, 0), (2, 40.0, 1)], 2),
        ([(1, 5.0, 0), (2, 40.0, 0)], None),
        ([(1, 5.0, 1), (2, 40.0, 1)], 1),
    ],
)
def test_inactive_listings_are_ignored(
    conn: sqlite3.Connection,
    active_flags: list[tuple[int, float, int]],
    expected_listing_id: int | None,
) -> None:
    """Sold or removed listings never win the cheapest listing race.

    Args:
        conn: In memory database fixture.
        active_flags: Listing identifier, price, and active flag triples to seed.
        expected_listing_id: Listing identifier expected on the exported track.

    Returns:
        None.
    """
    _add_release(conn, 210)
    _add_pool(conn, 210, "seller")
    _add_video(conn, 210, "wwwwwwwwwww")
    for listing_id, price, active in active_flags:
        _add_listing(conn, listing_id, 210, price=price, active=active)

    crate = export.build_crate(conn)

    assert crate["tracks"][0]["listing_id"] == expected_listing_id


@pytest.mark.parametrize(
    ("have", "want", "expected"),
    [
        (10, 5, 0.5),
        (0, 5, None),
        (None, 5, None),
        (10, None, None),
        (0, 0, None),
        (4, 1, 0.25),
        (3, 1, 0.3333),
    ],
)
def test_want_ratio_never_divides_by_zero(
    conn: sqlite3.Connection, have: int | None, want: int | None, expected: float | None
) -> None:
    """The want ratio degrades to None instead of raising on a zero or missing have count.

    Args:
        conn: In memory database fixture.
        have: Seeded have count.
        want: Seeded want count.
        expected: Expected want ratio on the exported track.

    Returns:
        None.
    """
    _add_release(conn, 300, have=have, want=want)
    _add_pool(conn, 300, "search")
    _add_video(conn, 300, "yyyyyyyyyyy")

    crate = export.build_crate(conn)

    assert crate["tracks"][0]["want_ratio"] == expected


@pytest.mark.parametrize(
    ("source", "expected_source"),
    [
        ("search", "search"),
        ("seller", "seller"),
    ],
)
def test_release_without_listing_still_exports(
    conn: sqlite3.Connection, source: str, expected_source: str
) -> None:
    """A pooled release with no active listing appears with null price and condition.

    Args:
        conn: In memory database fixture.
        source: Pool discovery source to seed.
        expected_source: Source expected on the exported track.

    Returns:
        None.
    """
    _add_release(conn, 400)
    _add_pool(conn, 400, source)
    _add_video(conn, 400, "zzzzzzzzzzz")

    crate = export.build_crate(conn)
    track = crate["tracks"][0]

    assert track["source"] == expected_source
    assert track["listing_id"] is None
    assert track["seller"] is None
    assert track["price"] is None
    assert track["currency"] is None
    assert track["condition"] is None
    assert track["condition_grade"] is None
    assert track["sleeve"] is None
    assert track["sleeve_grade"] is None
    assert track["listing_uri"] is None
    assert track["discogs_uri"] == "https://www.discogs.com/release/400"


@pytest.mark.parametrize(
    ("column", "raw"),
    [
        ("genres", "not json at all"),
        ("genres", "{"),
        ("genres", None),
        ("genres", ""),
        ("genres", '{"genre": "Electronic"}'),
        ("styles", "[unclosed"),
        ("formats", "null"),
        ("format_descriptions", "12"),
    ],
)
def test_malformed_json_columns_degrade_to_empty_list(
    conn: sqlite3.Connection, column: str, raw: str | None
) -> None:
    """Broken JSON in a list column yields an empty list rather than an exception.

    Args:
        conn: In memory database fixture.
        column: Release column to corrupt.
        raw: Raw value written into that column.

    Returns:
        None.
    """
    _add_release(conn, 500, **{column: raw})
    _add_pool(conn, 500, "search")
    _add_video(conn, 500, "qqqqqqqqqqq")

    crate = export.build_crate(conn)

    assert crate["tracks"][0][column] == []


@pytest.mark.parametrize(
    ("condition", "sleeve", "expected_condition_grade", "expected_sleeve_grade"),
    [
        ("Near Mint (NM or M-)", "Very Good Plus (VG+)", "NM", "VG+"),
        ("Mint (M)", "Generic", "M", None),
        ("Very Good (VG)", "No Cover", "VG", None),
        ("Good Plus (G+)", "Not Graded", "G+", None),
        ("Poor (P)", "Fair (F)", "P", "F"),
        (None, None, None, None),
    ],
)
def test_condition_grades_use_the_shared_vocabulary(
    conn: sqlite3.Connection,
    condition: str | None,
    sleeve: str | None,
    expected_condition_grade: str | None,
    expected_sleeve_grade: str | None,
) -> None:
    """Listing conditions map onto short grades through the config vocabulary.

    Args:
        conn: In memory database fixture.
        condition: Seeded media condition.
        sleeve: Seeded sleeve condition.
        expected_condition_grade: Expected media grade.
        expected_sleeve_grade: Expected sleeve grade.

    Returns:
        None.
    """
    _add_release(conn, 600)
    _add_pool(conn, 600, "seller")
    _add_video(conn, 600, "rrrrrrrrrrr")
    _add_listing(conn, 60, 600, condition=condition, sleeve=sleeve)

    crate = export.build_crate(conn)
    track = crate["tracks"][0]

    assert track["condition_grade"] == expected_condition_grade
    assert track["sleeve_grade"] == expected_sleeve_grade


@pytest.mark.parametrize(
    ("owned_ids", "wanted_ids", "expected_owned", "expected_wanted"),
    [
        ([], [], False, False),
        ([700], [], True, False),
        ([], [700], False, True),
        ([700], [700], True, True),
    ],
)
def test_owned_and_wanted_flags(
    conn: sqlite3.Connection,
    owned_ids: list[int],
    wanted_ids: list[int],
    expected_owned: bool,
    expected_wanted: bool,
) -> None:
    """Collection and wantlist membership surface as booleans on each track.

    Args:
        conn: In memory database fixture.
        owned_ids: Release identifiers seeded into the owned table.
        wanted_ids: Release identifiers seeded into the wantlist table.
        expected_owned: Expected owned flag.
        expected_wanted: Expected wanted flag.

    Returns:
        None.
    """
    _add_release(conn, 700)
    _add_pool(conn, 700, "search")
    _add_video(conn, 700, "ttttttttttt")
    for release_id in owned_ids:
        conn.execute(
            "INSERT INTO owned (release_id, synced_at) VALUES (?, ?)", (release_id, now_iso())
        )
    for release_id in wanted_ids:
        conn.execute(
            "INSERT INTO wantlist (release_id, synced_at) VALUES (?, ?)", (release_id, now_iso())
        )
    conn.commit()

    track = export.build_crate(conn)["tracks"][0]

    assert track["owned"] is expected_owned
    assert track["wanted"] is expected_wanted


@pytest.mark.parametrize(
    ("seeded", "expected_order"),
    [
        ([(1, "Beta", "Song"), (2, "Alpha", "Song")], [2, 1]),
        ([(1, "Alpha", "Zulu"), (2, "Alpha", "Alpha")], [2, 1]),
        ([(1, "alpha", "b"), (2, "Alpha", "a")], [2, 1]),
        ([(3, "Same", "Same"), (1, "Same", "Same")], [1, 3]),
    ],
)
def test_tracks_sort_deterministically(
    conn: sqlite3.Connection, seeded: list[tuple[int, str, str]], expected_order: list[int]
) -> None:
    """Tracks are ordered by artist then title so the committed file has a stable diff.

    Args:
        conn: In memory database fixture.
        seeded: Release identifier, artist, and title triples to seed.
        expected_order: Release identifiers in the expected exported order.

    Returns:
        None.
    """
    for release_id, artist, title in seeded:
        _add_release(conn, release_id, artist=artist, title=title)
        _add_pool(conn, release_id, "search")
        _add_video(conn, release_id, f"vid{release_id:08d}")

    crate = export.build_crate(conn)

    assert [track["release_id"] for track in crate["tracks"]] == expected_order


@pytest.mark.parametrize(
    ("positions", "expected_ids"),
    [
        ([("aaaaaaaaaaa", 1), ("bbbbbbbbbbb", 0)], ["bbbbbbbbbbb", "aaaaaaaaaaa"]),
        ([("aaaaaaaaaaa", 0), ("bbbbbbbbbbb", 1)], ["aaaaaaaaaaa", "bbbbbbbbbbb"]),
        ([("bbbbbbbbbbb", 0), ("aaaaaaaaaaa", 0)], ["aaaaaaaaaaa", "bbbbbbbbbbb"]),
    ],
)
def test_videos_are_ordered_by_position(
    conn: sqlite3.Connection, positions: list[tuple[str, int]], expected_ids: list[str]
) -> None:
    """Playable videos keep their recorded order inside each track.

    Args:
        conn: In memory database fixture.
        positions: Video identifier and position pairs to seed.
        expected_ids: Video identifiers in the expected exported order.

    Returns:
        None.
    """
    _add_release(conn, 800)
    _add_pool(conn, 800, "search")
    for video_id, position in positions:
        _add_video(conn, 800, video_id, position=position)

    track = export.build_crate(conn)["tracks"][0]

    assert [video["id"] for video in track["videos"]] == expected_ids
    assert all(video["duration"] == 321 for video in track["videos"])


@pytest.mark.parametrize(
    ("n_releases", "n_dead_only", "n_active_listings"),
    [
        (1, 0, 1),
        (3, 1, 2),
        (0, 0, 0),
    ],
)
def test_stats_block(
    conn: sqlite3.Connection, n_releases: int, n_dead_only: int, n_active_listings: int
) -> None:
    """The stats block counts releases, playable releases, and active listings.

    Args:
        conn: In memory database fixture.
        n_releases: Number of releases with a playable video.
        n_dead_only: Number of extra releases whose only video is dead.
        n_active_listings: Number of active listings to seed.

    Returns:
        None.
    """
    for index in range(n_releases):
        release_id = 1000 + index
        _add_release(conn, release_id)
        _add_pool(conn, release_id, "search")
        _add_video(conn, release_id, f"live{release_id:07d}")
    for index in range(n_dead_only):
        release_id = 2000 + index
        _add_release(conn, release_id)
        _add_pool(conn, release_id, "search")
        _add_video(conn, release_id, f"dead{release_id:07d}", dead=1)
    for index in range(n_active_listings):
        _add_listing(conn, 5000 + index, 1000, seller=f"seller{index}")

    crate = export.build_crate(conn)

    assert crate["stats"]["tracks"] == n_releases
    assert crate["stats"]["releases"] == n_releases + n_dead_only
    assert crate["stats"]["with_video"] == n_releases
    assert crate["stats"]["listings_active"] == n_active_listings
    assert crate["sellers"] == sorted(f"seller{index}" for index in range(n_active_listings))


@pytest.mark.parametrize(
    ("relative_path",),
    [
        ("crate.js",),
        ("web/crate.js",),
        ("deep/nested/dir/crate.js",),
    ],
)
def test_write_crate_emits_window_assignment(
    conn: sqlite3.Connection, tmp_path: Path, relative_path: str
) -> None:
    """The written file assigns the payload to window.CRATE and creates missing directories.

    Args:
        conn: In memory database fixture.
        tmp_path: Pytest supplied temporary directory.
        relative_path: Destination path relative to the temporary directory.

    Returns:
        None.
    """
    _add_release(conn, 900, artist="Mood II Swing", title="Can't Get Away")
    _add_pool(conn, 900, "seller")
    _add_video(conn, 900, "ppppppppppp")
    _add_listing(conn, 90, 900)
    target = tmp_path / relative_path

    stats = export.write_crate(conn, target)
    text = target.read_text(encoding="utf-8")

    assert text.startswith("window.CRATE = ")
    assert text.rstrip().endswith(";")
    assert stats == {"tracks": 1, "releases": 1, "with_video": 1, "listings_active": 1}

    payload = json.loads(text[len("window.CRATE = ") : text.rstrip().rfind(";")])
    assert payload["stats"] == stats
    assert payload["tracks"][0]["artist"] == "Mood II Swing"
    assert payload["sellers"] == ["allcitydublin"]


@pytest.mark.parametrize(
    ("key", "expected_type"),
    [
        ("generated_at", str),
        ("sellers", list),
        ("stats", dict),
        ("tracks", list),
    ],
)
def test_crate_top_level_shape(
    conn: sqlite3.Connection, key: str, expected_type: type
) -> None:
    """The crate payload always carries the four documented top level keys.

    Args:
        conn: In memory database fixture.
        key: Top level key to inspect.
        expected_type: Expected Python type of the value.

    Returns:
        None.
    """
    crate = export.build_crate(conn)

    assert key in crate
    assert isinstance(crate[key], expected_type)
