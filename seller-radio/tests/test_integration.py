"""End to end integration tests for the whole Seller Radio pipeline.

Every test here drives the real modules against a fake Discogs client built from canned
payloads, so the full chain of seller inventory sync, search bank crawl, account sync,
enrichment, crate build, and crate file write runs without opening a socket. The fake refuses
any path the pipeline is not supposed to request, which is what keeps the suite honest about
never reaching api.discogs.com.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from radio import enrich, export, sources

SELLER = "allcitydublin"
USERNAME = "egunduz"
INVENTORY_PATH = f"/users/{SELLER}/inventory"
COLLECTION_PATH = f"/users/{USERNAME}/collection/folders/0/releases"
WANTS_PATH = f"/users/{USERNAME}/wants"
SEARCH_PATH = "/database/search"
MASTER_ID = 9001
CRATE_PREFIX = "window.CRATE = "

BANK: dict[str, Any] = {
    "type": "release",
    "format": "Vinyl",
    "genre": "Electronic",
    "style": "Deep House",
    "year": "1993-1999",
}

ALLOWED_PREFIXES: tuple[str, ...] = (
    INVENTORY_PATH,
    COLLECTION_PATH,
    WANTS_PATH,
    SEARCH_PATH,
    "/releases/",
    "/masters/",
)

TRACK_KEYS: frozenset[str] = frozenset(
    {
        "release_id",
        "listing_id",
        "seller",
        "price",
        "currency",
        "condition",
        "condition_grade",
        "sleeve",
        "sleeve_grade",
        "ships_from",
        "listing_uri",
        "discogs_uri",
        "artist",
        "title",
        "label",
        "catno",
        "year",
        "country",
        "genres",
        "styles",
        "formats",
        "format_descriptions",
        "master_id",
        "have",
        "want",
        "want_ratio",
        "rating",
        "rating_count",
        "thumb",
        "videos",
        "source",
        "owned",
        "wanted",
    }
)

PLAYABLE_RELEASE_IDS: tuple[int, ...] = (101, 103, 104, 201)
POOLED_RELEASE_IDS: tuple[int, ...] = (101, 102, 103, 104, 105, 201)


@dataclass
class FakeDiscogs:
    """Canned stand in for DiscogsClient that can never reach the network.

    Attributes:
        inventory_pages: Seller inventory payloads keyed by page number.
        search_pages: Database search payloads keyed by page number.
        releases: Release payloads keyed by release id. A missing id models a fetch failure.
        masters: Master payloads keyed by master id.
        accounts: Collection and wantlist payloads keyed by request path.
        request_count: Number of requests served so far.
        paths: Every requested path, in call order.
    """

    inventory_pages: dict[int, dict]
    search_pages: dict[int, dict]
    releases: dict[int, dict]
    masters: dict[int, dict]
    accounts: dict[str, dict]
    request_count: int = 0
    paths: list[str] = field(default_factory=list)

    def get(self, path: str, params: dict | None = None) -> dict | None:
        """Serve one canned payload.

        Args:
            path: Request path relative to the Discogs API root.
            params: Query parameters, only the page number is honoured.

        Returns:
            The canned payload, or None when the path models a soft failure.

        Raises:
            AssertionError: If the pipeline asks for a path outside the allowed surface.
        """
        assert path.startswith(ALLOWED_PREFIXES), f"unexpected request path {path}"
        self.request_count += 1
        self.paths.append(path)
        page = _as_page(params)
        if path == INVENTORY_PATH:
            return self.inventory_pages.get(page)
        if path == SEARCH_PATH:
            return self.search_pages.get(page)
        if path in self.accounts:
            return self.accounts[path] if page == 1 else None
        if path.startswith("/releases/"):
            return self.releases.get(_trailing_id(path))
        if path.startswith("/masters/"):
            return self.masters.get(_trailing_id(path))
        return None

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        max_items: int | None = None,
        items_key: str | None = None,
    ) -> Iterator[dict]:
        """Walk every canned page of a paginated endpoint.

        Args:
            path: Request path relative to the Discogs API root.
            params: Query parameters, extended with the page number per page.
            max_items: Optional ceiling on the number of yielded items.
            items_key: Payload key holding the item list.

        Yields:
            Item dictionaries in page order.
        """
        page = 1
        yielded = 0
        while True:
            body = self.get(path, dict(params or {}, page=page))
            if body is None:
                return
            items = [
                item
                for item in (body.get(items_key or "results") or [])
                if isinstance(item, dict)
            ]
            if not items:
                return
            for item in items:
                if max_items is not None and yielded >= max_items:
                    return
                yield item
                yielded += 1
            pagination = body.get("pagination")
            total = pagination.get("pages") if isinstance(pagination, dict) else None
            if not isinstance(total, int) or page >= total:
                return
            page += 1


@dataclass(frozen=True)
class PipelineRun:
    """Everything a pipeline run produced, ready to assert against.

    Attributes:
        client: The fake client that served the run.
        crate: The crate payload returned by build_crate.
        stats: The stats mapping returned by write_crate.
        crate_path: Path of the written crate file.
    """

    client: FakeDiscogs
    crate: dict
    stats: dict
    crate_path: Path


def _as_page(params: dict | None) -> int:
    """Read the page number out of query parameters.

    Args:
        params: Query parameters, possibly without a page key.

    Returns:
        The requested page number, defaulting to the first page.
    """
    try:
        return int((params or {}).get("page", 1))
    except (TypeError, ValueError):
        return 1


def _trailing_id(path: str) -> int:
    """Read the numeric identifier at the end of a request path.

    Args:
        path: Request path such as "/releases/101".

    Returns:
        The trailing identifier, or zero when the path does not end in one.
    """
    try:
        return int(path.rsplit("/", 1)[1])
    except (IndexError, ValueError):
        return 0


def _video(video_id: str, title: str, duration: int = 300) -> dict:
    """Build a Discogs video entry.

    Args:
        video_id: Eleven character YouTube identifier.
        title: Video title.
        duration: Video duration in seconds.

    Returns:
        A video payload shaped like the Discogs release videos entries.
    """
    return {
        "uri": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "duration": duration,
    }


def _listing(listing_id: int, release_id: int, price: float) -> dict:
    """Build an inventory listing payload with its nested release metadata.

    Args:
        listing_id: Listing identifier.
        release_id: Release identifier the listing points at.
        price: Listing price in euro.

    Returns:
        A listing payload shaped like the Discogs inventory response.
    """
    return {
        "id": listing_id,
        "status": "For Sale",
        "price": {"value": price, "currency": "EUR"},
        "condition": "Near Mint (NM or M-)",
        "sleeve_condition": "Very Good Plus (VG+)",
        "ships_from": "Ireland",
        "uri": f"https://www.discogs.com/sell/item/{listing_id}",
        "comments": "plays clean",
        "posted": "2026-01-01T00:00:00-08:00",
        "release": {
            "id": release_id,
            "artist": f"Artist {release_id}",
            "title": f"Title {release_id}",
            "label": [f"Label {release_id}"],
            "catalog_number": f"CAT{release_id}",
            "format": "Vinyl",
            "year": 1995,
            "thumbnail": f"https://img.discogs.com/{release_id}.jpg",
        },
    }


def _release(
    release_id: int,
    videos: list[dict],
    have: int,
    want: int,
    master_id: int | None = None,
) -> dict:
    """Build a full release payload as returned by the release endpoint.

    Args:
        release_id: Release identifier.
        videos: Video entries carried by the release itself.
        have: Community have count.
        want: Community want count.
        master_id: Optional master identifier used for the video fallback.

    Returns:
        A release payload shaped like the Discogs release response.
    """
    return {
        "id": release_id,
        "title": f"Title {release_id}",
        "artists": [{"name": f"Artist {release_id} (2)"}],
        "labels": [{"name": f"Label {release_id}", "catno": f"CAT{release_id}"}],
        "formats": [{"name": "Vinyl", "qty": "1", "descriptions": ['12"', "EP"]}],
        "genres": ["Electronic"],
        "styles": ["Deep House"],
        "country": "Germany",
        "year": 1995,
        "master_id": master_id,
        "community": {"have": have, "want": want, "rating": {"count": 4, "average": 4.25}},
        "thumb": f"https://img.discogs.com/{release_id}.jpg",
        "videos": videos,
    }


def _inventory_pages(sold_listing_ids: frozenset[int]) -> dict[int, dict]:
    """Build the two page seller inventory, dropping any listing that has sold.

    Args:
        sold_listing_ids: Listing identifiers that must be absent from the response.

    Returns:
        Inventory payloads keyed by page number.
    """
    first = [
        _listing(1001, 101, 19.03),
        _listing(1002, 102, 8.50),
        _listing(1003, 103, 30.00),
        _listing(1004, 104, 12.00),
    ]
    second = [_listing(1005, 105, 25.00), _listing(1006, 101, 15.00)]
    pages = {}
    for number, listings in ((1, first), (2, second)):
        kept = [entry for entry in listings if entry["id"] not in sold_listing_ids]
        pages[number] = {
            "pagination": {"page": number, "pages": 2, "items": len(kept), "per_page": 4},
            "listings": kept,
        }
    return pages


def make_client(sold_listing_ids: frozenset[int] = frozenset()) -> FakeDiscogs:
    """Build a fake client seeded with the whole fixture world.

    Release 102 has no video anywhere, release 103 only has one through its master, and
    release 105 cannot be fetched at all, so the fixture covers every branch the exporter
    has to survive.

    Args:
        sold_listing_ids: Listing identifiers to drop from the inventory response.

    Returns:
        A fake client ready to serve the pipeline.
    """
    return FakeDiscogs(
        inventory_pages=_inventory_pages(sold_listing_ids),
        search_pages={
            1: {
                "pagination": {"page": 1, "pages": 1, "items": 2},
                "results": [
                    {
                        "id": 201,
                        "title": "Search Artist - Search Title",
                        "label": ["Search Label"],
                        "catno": "SR001",
                        "year": "1994",
                        "country": "UK",
                        "genre": ["Electronic"],
                        "style": ["Deep House"],
                        "format": ["Vinyl", '12"'],
                        "thumb": "https://img.discogs.com/201.jpg",
                    },
                    {"id": 103, "title": "Artist 103 - Title 103", "genre": ["Electronic"]},
                ],
            }
        },
        releases={
            101: _release(
                101,
                [_video("aaaaaaaaaaa", "Side A"), _video("bbbbbbbbbbb", "Side B")],
                have=40,
                want=120,
            ),
            102: _release(102, [], have=3, want=1),
            103: _release(103, [], have=12, want=7, master_id=MASTER_ID),
            104: _release(104, [_video("ddddddddddd", "Only cut")], have=5, want=2),
            201: _release(201, [_video("eeeeeeeeeee", "Search cut")], have=0, want=9),
        },
        masters={MASTER_ID: {"id": MASTER_ID, "videos": [_video("ccccccccccc", "Master cut")]}},
        accounts={
            COLLECTION_PATH: {
                "pagination": {"page": 1, "pages": 1, "items": 1},
                "releases": [{"id": 103, "basic_information": {"id": 103}}],
            },
            WANTS_PATH: {
                "pagination": {"page": 1, "pages": 1, "items": 1},
                "wants": [{"id": 201, "basic_information": {"id": 201}}],
            },
        },
    )


def run_pipeline(
    conn: sqlite3.Connection,
    client: FakeDiscogs,
    crate_path: Path,
    batches: tuple[int, ...] = (50,),
) -> PipelineRun:
    """Run the full pipeline once and write the crate file.

    Args:
        conn: Open SQLite connection with the schema applied.
        client: Fake client serving the canned payloads.
        crate_path: Destination of the generated crate file.
        batches: Enrichment limits applied in order, modelling resumed runs.

    Returns:
        The run outcome with the crate payload, the stats, and the crate path.
    """
    sources.sync_seller(conn, client, SELLER)
    sources.crawl_bank(conn, client, BANK)
    sources.sync_owned(conn, client, USERNAME)
    sources.sync_wantlist(conn, client, USERNAME)
    for limit in batches:
        enrich.enrich_pending(conn, client, limit)
    stats = export.write_crate(conn, crate_path)
    return PipelineRun(
        client=client,
        crate=export.build_crate(conn),
        stats=stats,
        crate_path=crate_path,
    )


def read_crate_file(path: Path) -> dict:
    """Parse a written crate file back into a payload.

    Args:
        path: Path of the crate file.

    Returns:
        The decoded crate payload.
    """
    text = path.read_text(encoding="utf-8")
    assert text.startswith(CRATE_PREFIX)
    assert text.rstrip("\n").endswith(";")
    body = text.removeprefix(CRATE_PREFIX).rstrip("\n").removesuffix(";")
    return json.loads(body)


def track_by_release(crate: dict, release_id: int) -> dict | None:
    """Find a crate track by its release id.

    Args:
        crate: Crate payload.
        release_id: Release identifier to look for.

    Returns:
        The matching track, or None when the release is not in the crate.
    """
    for track in crate["tracks"]:
        if track["release_id"] == release_id:
            return track
    return None


@pytest.mark.parametrize(
    "batches",
    [(50,), (1, 1, 1, 1, 1, 1), (2, 4), (3, 3, 3)],
)
def test_pipeline_writes_a_playable_crate_whatever_the_enrichment_batching(
    conn: sqlite3.Connection,
    tmp_path: Path,
    batches: tuple[int, ...],
) -> None:
    """The full offline pipeline produces the same valid crate however enrichment is split."""
    crate_path = tmp_path / "web" / "crate.js"
    run = run_pipeline(conn, make_client(), crate_path, batches)
    written = read_crate_file(crate_path)

    assert enrich.pending_count(conn) == 0
    assert [track["release_id"] for track in written["tracks"]] == sorted(PLAYABLE_RELEASE_IDS)
    assert written["sellers"] == [SELLER]
    assert written["stats"] == {
        "tracks": len(PLAYABLE_RELEASE_IDS),
        "releases": len(POOLED_RELEASE_IDS),
        "with_video": len(PLAYABLE_RELEASE_IDS),
        "listings_active": 6,
    }
    assert written["stats"] == run.stats
    assert all(set(track) == TRACK_KEYS for track in written["tracks"])
    assert all(track["videos"] for track in written["tracks"])
    assert written["generated_at"]


@pytest.mark.parametrize(
    ("release_id", "expected_in_crate"),
    [(101, True), (103, True), (104, True), (201, True), (102, False), (105, False)],
)
def test_only_releases_with_a_video_reach_the_crate(
    conn: sqlite3.Connection,
    tmp_path: Path,
    release_id: int,
    expected_in_crate: bool,
) -> None:
    """A pooled release without a playable video is stored but never exported."""
    run = run_pipeline(conn, make_client(), tmp_path / "crate.js")
    pooled = {
        int(row["release_id"]) for row in conn.execute("SELECT release_id FROM pool")
    }

    assert release_id in pooled
    assert (track_by_release(run.crate, release_id) is not None) is expected_in_crate


@pytest.mark.parametrize(
    ("release_id", "dead_video_ids", "expected_video_count"),
    [
        (101, ("bbbbbbbbbbb",), 1),
        (101, ("aaaaaaaaaaa", "bbbbbbbbbbb"), 0),
        (104, ("ddddddddddd",), 0),
        (103, ("ccccccccccc",), 0),
    ],
)
def test_dead_videos_drop_out_and_can_empty_a_track(
    conn: sqlite3.Connection,
    tmp_path: Path,
    release_id: int,
    dead_video_ids: tuple[str, ...],
    expected_video_count: int,
) -> None:
    """Videos marked dead never reach the crate, and a track with none left disappears."""
    run_pipeline(conn, make_client(), tmp_path / "crate.js")
    conn.executemany(
        "UPDATE videos SET dead = 1 WHERE release_id = ? AND video_id = ?",
        [(release_id, video_id) for video_id in dead_video_ids],
    )
    conn.commit()

    crate = export.build_crate(conn)
    track = track_by_release(crate, release_id)

    if expected_video_count == 0:
        assert track is None
    else:
        assert track is not None
        assert len(track["videos"]) == expected_video_count


@pytest.mark.parametrize(
    ("sold_listing_id", "release_id", "expected_listing_id", "expected_price"),
    [
        (1006, 101, 1001, 19.03),
        (1004, 104, None, None),
        (1003, 103, None, None),
    ],
)
def test_a_sold_listing_disappears_on_the_second_sync(
    conn: sqlite3.Connection,
    tmp_path: Path,
    sold_listing_id: int,
    release_id: int,
    expected_listing_id: int | None,
    expected_price: float | None,
) -> None:
    """A listing gone from the inventory goes inactive and leaves the crate on the next run."""
    crate_path = tmp_path / "crate.js"
    first = run_pipeline(conn, make_client(), crate_path)
    assert first.stats["listings_active"] == 6
    assert track_by_release(first.crate, release_id)["listing_id"] is not None

    result = sources.sync_seller(conn, make_client(frozenset({sold_listing_id})), SELLER)
    export.write_crate(conn, crate_path)
    written = read_crate_file(crate_path)
    track = track_by_release(written, release_id)

    assert result["deactivated"] == 1
    assert "error" not in result
    assert conn.execute(
        "SELECT active FROM listings WHERE listing_id = ?", (sold_listing_id,)
    ).fetchone()["active"] == 0
    assert written["stats"]["listings_active"] == 5
    assert track is not None
    assert track["listing_id"] == expected_listing_id
    assert track["price"] == expected_price
    assert sold_listing_id not in {
        int(row["listing_id"])
        for row in conn.execute("SELECT listing_id FROM listings WHERE active = 1")
    }


@pytest.mark.parametrize(
    ("release_id", "field", "expected"),
    [
        (101, "source", "seller"),
        (101, "listing_id", 1006),
        (101, "price", 15.00),
        (101, "condition_grade", "NM"),
        (101, "sleeve_grade", "VG+"),
        (101, "seller", SELLER),
        (101, "currency", "EUR"),
        (101, "ships_from", "Ireland"),
        (101, "artist", "Artist 101"),
        (101, "want_ratio", 3.0),
        (101, "discogs_uri", "https://www.discogs.com/release/101"),
        (103, "source", "seller"),
        (103, "owned", True),
        (103, "wanted", False),
        (104, "want_ratio", 0.4),
        (201, "source", "search"),
        (201, "listing_id", None),
        (201, "price", None),
        (201, "condition_grade", None),
        (201, "owned", False),
        (201, "wanted", True),
        (201, "want_ratio", None),
    ],
)
def test_crate_track_fields_carry_the_joined_state(
    conn: sqlite3.Connection,
    tmp_path: Path,
    release_id: int,
    field: str,
    expected: Any,
) -> None:
    """Listing, pool, collection, and wantlist state all land on the exported track."""
    run = run_pipeline(conn, make_client(), tmp_path / "crate.js")
    track = track_by_release(run.crate, release_id)

    assert track is not None
    assert track[field] == expected


@pytest.mark.parametrize(
    ("release_id", "expected_video_ids"),
    [
        (101, ["aaaaaaaaaaa", "bbbbbbbbbbb"]),
        (103, ["ccccccccccc"]),
        (104, ["ddddddddddd"]),
        (201, ["eeeeeeeeeee"]),
    ],
)
def test_videos_come_from_the_release_or_fall_back_to_the_master(
    conn: sqlite3.Connection,
    tmp_path: Path,
    release_id: int,
    expected_video_ids: list[str],
) -> None:
    """Release videos win, and a release without any inherits its master's videos."""
    run = run_pipeline(conn, make_client(), tmp_path / "crate.js")
    track = track_by_release(run.crate, release_id)

    assert track is not None
    assert [video["id"] for video in track["videos"]] == expected_video_ids


@pytest.mark.parametrize(
    ("release_id", "expected_source"),
    [(101, "seller"), (103, "seller"), (201, "search")],
)
def test_a_search_hit_never_downgrades_a_seller_discovery(
    conn: sqlite3.Connection,
    tmp_path: Path,
    release_id: int,
    expected_source: str,
) -> None:
    """Release 103 is found by both the seller and the bank yet stays a seller discovery."""
    run_pipeline(conn, make_client(), tmp_path / "crate.js")
    row = conn.execute("SELECT source FROM pool WHERE release_id = ?", (release_id,)).fetchone()

    assert row is not None
    assert row["source"] == expected_source


@pytest.mark.parametrize(
    ("path", "expected_calls"),
    [
        (INVENTORY_PATH, 2),
        (SEARCH_PATH, 1),
        (COLLECTION_PATH, 1),
        (WANTS_PATH, 1),
        ("/releases/101", 1),
        ("/releases/105", 1),
        (f"/masters/{MASTER_ID}", 1),
    ],
)
def test_the_pipeline_only_talks_to_the_expected_endpoints(
    conn: sqlite3.Connection,
    tmp_path: Path,
    path: str,
    expected_calls: int,
) -> None:
    """Every request stays inside the fake, and each endpoint is hit exactly once per need."""
    run = run_pipeline(conn, make_client(), tmp_path / "crate.js")

    assert run.client.paths.count(path) == expected_calls
    assert all(seen.startswith(ALLOWED_PREFIXES) for seen in run.client.paths)


@pytest.mark.parametrize("suffix", [".js", ".json", ".txt"])
def test_the_crate_file_is_loadable_over_the_file_protocol(
    conn: sqlite3.Connection,
    tmp_path: Path,
    suffix: str,
) -> None:
    """The exporter always writes an assignment to window.CRATE, never a bare JSON body."""
    crate_path = tmp_path / f"crate{suffix}"
    run_pipeline(conn, make_client(), crate_path)
    text = crate_path.read_text(encoding="utf-8")
    decoded = json.loads(text.removeprefix(CRATE_PREFIX).rstrip("\n").removesuffix(";"))

    assert text.startswith(CRATE_PREFIX)
    assert text.endswith(";\n")
    assert not text.lstrip().startswith("{")
    assert len(decoded["tracks"]) == len(PLAYABLE_RELEASE_IDS)


@pytest.mark.parametrize("failed_release_id", [105])
def test_an_unfetchable_release_is_recorded_and_never_retried(
    conn: sqlite3.Connection,
    tmp_path: Path,
    failed_release_id: int,
) -> None:
    """A release the API refuses is stamped as done so a rerun does not loop on it."""
    run = run_pipeline(conn, make_client(), tmp_path / "crate.js")
    row = conn.execute(
        "SELECT fetched_at, has_video FROM releases WHERE release_id = ?",
        (failed_release_id,),
    ).fetchone()
    before = run.client.request_count

    enrich.enrich_pending(conn, run.client, 50)

    assert row is not None
    assert row["fetched_at"] is not None
    assert row["has_video"] == 0
    assert run.client.request_count == before
    assert track_by_release(run.crate, failed_release_id) is None
