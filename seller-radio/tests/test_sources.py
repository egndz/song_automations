"""Tests for the seller inventory sync and the search bank crawler."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from radio import sources


class FakeClient:
    """Canned response stand-in for DiscogsClient that never touches the network.

    Attributes:
        calls: Every request recorded as a path and parameter pair.
    """

    def __init__(self, responses: dict[str, list[dict | None]] | None = None) -> None:
        """Builds a client backed by queued page payloads.

        Args:
            responses: Mapping of request path to the page payloads served in order. A None
                entry models a soft failure such as a private inventory.
        """
        self._responses = {key: list(value) for key, value in (responses or {}).items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, path: str, params: dict | None = None) -> dict | None:
        """Serves the next queued payload for a path.

        Args:
            path: Request path.
            params: Query parameters, recorded for assertions.

        Returns:
            The next queued payload, or None when the queue is empty or holds a failure.
        """
        self.calls.append((path, dict(params or {})))
        queue = self._responses.get(path)
        if not queue:
            return None
        return queue.pop(0)

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        max_items: int | None = None,
        items_key: str | None = None,
    ) -> Iterator[dict]:
        """Yields the items of every queued page for a path.

        Args:
            path: Request path.
            params: Query parameters, recorded for assertions.
            max_items: Optional ceiling on the number of yielded items.
            items_key: Payload key holding the item list.

        Yields:
            Item dictionaries in page order.
        """
        self.calls.append((path, dict(params or {})))
        yielded = 0
        for payload in self._responses.get(path, []):
            if payload is None:
                return
            items = payload.get(items_key or "results") or []
            for item in items:
                if max_items is not None and yielded >= max_items:
                    return
                yield item
                yielded += 1


def _listing(listing_id: int, release_id: int, price: float = 12.5) -> dict:
    """Builds a minimal inventory listing payload.

    Args:
        listing_id: Listing identifier.
        release_id: Release identifier the listing points at.
        price: Listing price value in euro.

    Returns:
        A listing payload shaped like the Discogs inventory response.
    """
    return {
        "id": listing_id,
        "price": {"value": price, "currency": "EUR"},
        "condition": "Near Mint (NM or M-)",
        "sleeve_condition": "Very Good Plus (VG+)",
        "ships_from": "Ireland",
        "uri": f"https://www.discogs.com/sell/item/{listing_id}",
        "comments": "plays great",
        "posted": "2026-01-01T00:00:00-08:00",
        "release": {
            "id": release_id,
            "artist": "Mr Fingers",
            "title": "Washing Machine",
            "label": "Trax Records",
            "catalog_number": "TX-127",
            "year": 1986,
            "thumbnail": "https://img.discogs.com/thumb.jpg",
        },
    }


def _inventory_page(listings: list[dict], pages: int = 1) -> dict:
    """Wraps listings in an inventory page payload.

    Args:
        listings: Listing payloads on the page.
        pages: Total page count reported by the API.

    Returns:
        An inventory page payload.
    """
    return {"pagination": {"page": 1, "pages": pages, "items": len(listings)}, "listings": listings}


def _search_page(results: list[dict]) -> dict:
    """Wraps results in a database search page payload.

    Args:
        results: Search result payloads on the page.

    Returns:
        A search page payload.
    """
    return {"pagination": {"page": 1, "pages": 1, "items": len(results)}, "results": results}


def _pool_source(conn: sqlite3.Connection, release_id: int) -> str | None:
    """Reads the pool source of a release.

    Args:
        conn: Open SQLite connection.
        release_id: Release identifier.

    Returns:
        The pool source string, or None when the release is not pooled.
    """
    row = conn.execute("SELECT source FROM pool WHERE release_id = ?", (release_id,)).fetchone()
    return row["source"] if row else None


@pytest.mark.parametrize(
    ("kept_ids", "sold_ids"),
    [
        ([101], [102]),
        ([101, 102], [103]),
        ([], [101, 102]),
    ],
)
def test_sync_seller_deactivates_sold_listings(
    conn: sqlite3.Connection,
    kept_ids: list[int],
    sold_ids: list[int],
) -> None:
    """Listings absent from the fresh inventory end up inactive while the rest stay active."""
    seller = "allcitydublin"
    first = FakeClient(
        {
            f"/users/{seller}/inventory": [
                _inventory_page([_listing(lid, lid + 900) for lid in kept_ids + sold_ids])
            ]
        }
    )
    sources.sync_seller(conn, first, seller)

    second = FakeClient(
        {
            f"/users/{seller}/inventory": [
                _inventory_page([_listing(lid, lid + 900) for lid in kept_ids])
            ]
        }
    )
    result = sources.sync_seller(conn, second, seller)

    active = {
        int(row["listing_id"])
        for row in conn.execute("SELECT listing_id FROM listings WHERE active = 1")
    }
    inactive = {
        int(row["listing_id"])
        for row in conn.execute("SELECT listing_id FROM listings WHERE active = 0")
    }

    assert active == set(kept_ids)
    assert inactive == set(sold_ids)
    assert result["deactivated"] == len(sold_ids)
    assert result["listings"] == len(kept_ids)
    assert "error" not in result


@pytest.mark.parametrize("page_count", [1, 3])
def test_sync_seller_writes_listing_and_seed_metadata(
    conn: sqlite3.Connection,
    page_count: int,
) -> None:
    """Every page is walked and the cheap release metadata is seeded unenriched."""
    seller = "allcitydublin"
    pages = [
        _inventory_page([_listing(100 + index, 900 + index)], pages=page_count)
        for index in range(page_count)
    ]
    client = FakeClient({f"/users/{seller}/inventory": pages})

    result = sources.sync_seller(conn, client, seller)

    assert result["listings"] == page_count
    assert result["new_releases"] == page_count
    row = conn.execute("SELECT * FROM listings WHERE listing_id = 100").fetchone()
    assert row["price"] == 12.5
    assert row["currency"] == "EUR"
    assert row["sleeve"] == "Very Good Plus (VG+)"
    assert row["ships_from"] == "Ireland"
    release = conn.execute("SELECT * FROM releases WHERE release_id = 900").fetchone()
    assert release["artist"] == "Mr Fingers"
    assert release["catno"] == "TX-127"
    assert release["has_video"] == 0
    assert release["fetched_at"] is None


@pytest.mark.parametrize("seller", ["privateseller", "deletedseller"])
def test_sync_seller_private_inventory_returns_error(
    conn: sqlite3.Connection,
    seller: str,
) -> None:
    """A soft failure on the first page reports an error instead of raising."""
    client = FakeClient({f"/users/{seller}/inventory": [None]})

    result = sources.sync_seller(conn, client, seller)

    assert result["error"] == "inventory unavailable"
    assert result["listings"] == 0
    assert result["deactivated"] == 0
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


@pytest.mark.parametrize("release_id", [555, 777])
def test_sync_seller_preserves_existing_rows_on_failure(
    conn: sqlite3.Connection,
    release_id: int,
) -> None:
    """An unreadable inventory leaves previously stored listings active and untouched."""
    seller = "allcitydublin"
    good = FakeClient({f"/users/{seller}/inventory": [_inventory_page([_listing(1, release_id)])]})
    sources.sync_seller(conn, good, seller)

    broken = FakeClient({f"/users/{seller}/inventory": [None]})
    result = sources.sync_seller(conn, broken, seller)

    assert "error" in result
    assert conn.execute("SELECT active FROM listings WHERE listing_id = 1").fetchone()[0] == 1


@pytest.mark.parametrize("release_id", [4242, 9001])
def test_pool_upgrades_search_to_seller(conn: sqlite3.Connection, release_id: int) -> None:
    """A seller discovery upgrades an existing search row."""
    bank = {"genre": "Electronic", "style": "Techno"}
    search_client = FakeClient(
        {sources.SEARCH_PATH: [_search_page([{"id": release_id, "title": "Artist - Title"}])]}
    )
    sources.crawl_bank(conn, search_client, bank)
    assert _pool_source(conn, release_id) == sources.SOURCE_SEARCH

    seller_client = FakeClient(
        {"/users/allcitydublin/inventory": [_inventory_page([_listing(1, release_id)])]}
    )
    result = sources.sync_seller(conn, seller_client, "allcitydublin")

    assert _pool_source(conn, release_id) == sources.SOURCE_SELLER
    assert result["new_releases"] == 0


@pytest.mark.parametrize("release_id", [4242, 9001])
def test_pool_never_downgrades_seller_to_search(
    conn: sqlite3.Connection,
    release_id: int,
) -> None:
    """A later search crawl leaves a seller sourced pool row alone."""
    seller_client = FakeClient(
        {"/users/allcitydublin/inventory": [_inventory_page([_listing(1, release_id)])]}
    )
    sources.sync_seller(conn, seller_client, "allcitydublin")

    search_client = FakeClient(
        {sources.SEARCH_PATH: [_search_page([{"id": release_id, "title": "Artist - Title"}])]}
    )
    result = sources.crawl_bank(conn, search_client, {"genre": "Electronic"})

    assert _pool_source(conn, release_id) == sources.SOURCE_SELLER
    assert result["found"] == 1
    assert result["new"] == 0


@pytest.mark.parametrize(
    ("bank", "expected_year"),
    [
        ({"year_from": 1990, "year_to": 1999}, "1990-1999"),
        ({"year_from": "1977", "year_to": "1982"}, "1977-1982"),
        ({"year": "1995"}, "1995"),
        ({"year": 1995}, "1995"),
        ({"year_from": 1990}, "1990"),
        ({"year_to": 1999}, "1999"),
        ({}, None),
    ],
)
def test_search_params_year_construction(bank: dict, expected_year: str | None) -> None:
    """The year range syntax is built from year_from and year_to, and a lone year passes through."""
    params = sources.build_search_params(bank)

    assert params.get("year") == expected_year
    assert params["type"] == "release"
    assert params["format"] == "Vinyl"


@pytest.mark.parametrize(
    ("bank", "expected"),
    [
        ({"genre": "Electronic"}, {"genre": "Electronic"}),
        ({"style": "Deep House"}, {"style": "Deep House"}),
        ({"country": "Germany"}, {"country": "Germany"}),
        ({"label": "Trax Records"}, {"label": "Trax Records"}),
        ({"query": "detroit"}, {"q": "detroit"}),
        ({"format": "LP"}, {"format": "LP"}),
    ],
)
def test_search_params_passthrough_keys(bank: dict, expected: dict) -> None:
    """Every supported bank key lands on the query parameters."""
    params = sources.build_search_params(bank)

    for key, value in expected.items():
        assert params[key] == value


@pytest.mark.parametrize(
    ("bank", "expected_year"),
    [
        ({"genre": "Electronic", "year_from": 1990, "year_to": 1999}, "1990-1999"),
        ({"genre": "Electronic", "year": "1995"}, "1995"),
    ],
)
def test_crawl_bank_sends_built_params(
    conn: sqlite3.Connection,
    bank: dict,
    expected_year: str,
) -> None:
    """The crawler passes the constructed parameters to the paginated request."""
    client = FakeClient({sources.SEARCH_PATH: [_search_page([])]})

    sources.crawl_bank(conn, client, bank)

    path, params = client.calls[0]
    assert path == sources.SEARCH_PATH
    assert params["year"] == expected_year
    assert params["genre"] == "Electronic"


@pytest.mark.parametrize(
    ("raw_title", "expected_artist", "expected_title"),
    [
        ("Mr Fingers - Washing Machine", "Mr Fingers", "Washing Machine"),
        ("Various - Compilation - Volume 2", "Various", "Compilation - Volume 2"),
        ("Untitled", None, "Untitled"),
        ("  Padded - Spaced  ", "Padded", "Spaced"),
        ("Artist - ", None, "Artist"),
        ("", None, None),
        (None, None, None),
    ],
)
def test_split_search_title(
    raw_title: str | None,
    expected_artist: str | None,
    expected_title: str | None,
) -> None:
    """The combined search title splits on the first separator and degrades sensibly."""
    artist, title = sources.split_search_title(raw_title)

    assert artist == expected_artist
    assert title == expected_title


@pytest.mark.parametrize(
    ("result_payload", "expected_artist", "expected_title", "expected_genres"),
    [
        (
            {
                "id": 11,
                "title": "Mr Fingers - Washing Machine",
                "genre": ["Electronic"],
                "style": ["Deep House", "Acid"],
                "country": "US",
                "year": "1986",
                "label": ["Trax Records"],
                "catno": "TX-127",
                "master_id": 4321,
                "thumb": "https://img.discogs.com/thumb.jpg",
            },
            "Mr Fingers",
            "Washing Machine",
            '["Electronic"]',
        ),
        (
            {"id": 12, "title": "Untitled", "genre": [], "style": []},
            None,
            "Untitled",
            None,
        ),
    ],
)
def test_crawl_bank_seeds_releases(
    conn: sqlite3.Connection,
    result_payload: dict,
    expected_artist: str | None,
    expected_title: str | None,
    expected_genres: str | None,
) -> None:
    """Search results seed unenriched release rows using the singular genre and style keys."""
    client = FakeClient({sources.SEARCH_PATH: [_search_page([result_payload])]})

    sources.crawl_bank(conn, client, {"genre": "Electronic"})

    row = conn.execute(
        "SELECT * FROM releases WHERE release_id = ?", (result_payload["id"],)
    ).fetchone()
    assert row["artist"] == expected_artist
    assert row["title"] == expected_title
    assert row["genres"] == expected_genres
    assert row["fetched_at"] is None
    assert row["has_video"] == 0


@pytest.mark.parametrize(
    ("total_results", "expected_capped"),
    [
        (5, False),
        (sources.MAX_SEARCH_ITEMS, True),
    ],
)
def test_crawl_bank_reports_cap(
    conn: sqlite3.Connection,
    total_results: int,
    expected_capped: bool,
) -> None:
    """Hitting the deep pagination cap is surfaced so the bank can be sliced more narrowly."""
    results = [{"id": index + 1, "title": f"A{index} - T{index}"} for index in range(total_results)]
    page = _search_page(results)
    client = FakeClient({sources.SEARCH_PATH: [page]})

    result = sources.crawl_bank(conn, client, {"genre": "Electronic"})

    assert result["found"] == total_results
    assert result["new"] == total_results
    assert result["capped"] is expected_capped
    assert result["bank_key"] == "genre=Electronic"


@pytest.mark.parametrize(
    ("sellers", "failing"),
    [
        (["a", "b"], []),
        (["a", "b"], ["b"]),
    ],
)
def test_sync_sellers_isolates_failures(
    conn: sqlite3.Connection,
    sellers: list[str],
    failing: list[str],
) -> None:
    """One unreadable inventory does not stop the remaining sellers."""
    responses: dict[str, list[dict | None]] = {}
    for index, seller in enumerate(sellers):
        page = None if seller in failing else _inventory_page([_listing(index + 1, index + 500)])
        responses[f"/users/{seller}/inventory"] = [page]
    client = FakeClient(responses)

    summary = sources.sync_sellers(conn, client, sellers)

    assert summary["sellers"] == len(sellers)
    assert summary["errors"] == failing
    assert summary["listings"] == len(sellers) - len(failing)


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([{"id": 1}, {"id": 2}], 2),
        ([{"basic_information": {"id": 7}}], 1),
        ([{"id": 3}, {"id": 3}], 1),
        ([], 0),
    ],
)
def test_sync_owned_replaces_table(
    conn: sqlite3.Connection,
    items: list[dict],
    expected: int,
) -> None:
    """The collection sync stores every release id once and keeps data on an empty fetch."""
    path = "/users/egunduz/collection/folders/0/releases"
    conn.execute(
        "INSERT INTO owned (release_id, synced_at) VALUES (?, ?)", (99, "2026-01-01T00:00:00+00:00")
    )
    conn.commit()
    client = FakeClient({path: [{"pagination": {"pages": 1}, "releases": items}]})

    count = sources.sync_owned(conn, client, "egunduz")
    stored = {int(row["release_id"]) for row in conn.execute("SELECT release_id FROM owned")}

    assert count == expected
    if expected:
        assert 99 not in stored
        assert len(stored) == expected
    else:
        assert stored == {99}


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([{"id": 10, "basic_information": {"id": 10}}], 1),
        ([{"id": 10}, {"id": 11}], 2),
    ],
)
def test_sync_wantlist_replaces_table(
    conn: sqlite3.Connection,
    items: list[dict],
    expected: int,
) -> None:
    """The wantlist sync stores the release ids and records the sync log entry."""
    client = FakeClient({"/users/egunduz/wants": [{"pagination": {"pages": 1}, "wants": items}]})

    count = sources.sync_wantlist(conn, client, "egunduz")

    assert count == expected
    assert conn.execute("SELECT COUNT(*) FROM wantlist").fetchone()[0] == expected
    logged = conn.execute("SELECT n FROM sync_log WHERE key = 'wantlist'").fetchone()
    assert logged["n"] == expected


class PagedInventoryClient:
    """Inventory stand-in that enforces the real hundred page ceiling on foreign shops.

    Discogs answers page numbers above one hundred with HTTP 403 for any inventory other than
    your own, which the client surfaces as None. Ascending and descending walks of the same sort
    key address opposite ends of the shop, so a shop larger than ten thousand listings is only
    fully reachable by combining both directions.

    Attributes:
        calls: Every request recorded as a path and parameter pair.
    """

    PAGE_CEILING = 100

    def __init__(self, total: int, per_page: int = 100) -> None:
        """Builds a fake inventory of a given size.

        Args:
            total: Number of active listings the shop holds.
            per_page: Listings returned per page.
        """
        self.total = total
        self.per_page = per_page
        self.pages = max(1, -(-total // per_page))
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, path: str, params: dict | None = None) -> dict | None:
        """Serves one inventory page honouring sort order and the page ceiling.

        Args:
            path: Request path.
            params: Query parameters including page and sort_order.

        Returns:
            A payload with a pagination block and listings, or None when Discogs would refuse.
        """
        params = dict(params or {})
        self.calls.append((path, params))
        page = int(params.get("page", 1))
        if page > self.PAGE_CEILING:
            return None

        offset = (page - 1) * self.per_page
        if params.get("sort_order") == "desc":
            hi = self.total - offset
            ids = list(range(hi, max(hi - self.per_page, 0), -1))
        else:
            ids = list(range(offset + 1, min(offset + self.per_page, self.total) + 1))

        listings = [
            {
                "id": listing_id,
                "price": {"value": 10.0, "currency": "EUR"},
                "condition": "Near Mint (NM or M-)",
                "sleeve_condition": "Very Good Plus (VG+)",
                "ships_from": "Ireland",
                "uri": f"https://www.discogs.com/sell/item/{listing_id}",
                "release": {"id": 900000 + listing_id, "title": "A - B", "year": 1994},
            }
            for listing_id in ids
        ]
        return {"pagination": {"items": self.total, "pages": self.pages}, "listings": listings}


@pytest.mark.parametrize(
    ("total", "expect_complete", "expect_desc_walk"),
    [
        (500, True, False),
        (10_000, True, False),
        (10_716, True, True),
        (12_500, True, True),
        (25_000, False, False),
    ],
)
def test_sync_seller_spans_the_hundred_page_ceiling(
    conn: sqlite3.Connection,
    total: int,
    expect_complete: bool,
    expect_desc_walk: bool,
) -> None:
    """A shop larger than ten thousand listings is covered from both ends or refused outright."""
    client = PagedInventoryClient(total)

    result = sources.sync_seller(conn, client, "allcitydublin")

    used_desc = any(call[1].get("sort_order") == "desc" for call in client.calls)
    assert used_desc is expect_desc_walk

    if expect_complete:
        assert "error" not in result
        assert result["listings"] == total
        stored = conn.execute("SELECT COUNT(*) FROM listings WHERE active = 1").fetchone()[0]
        assert stored == total
    else:
        assert "error" in result
        assert result["deactivated"] == 0
        assert "unreadable" in result["error"]


@pytest.mark.parametrize("total", [10_716, 12_500])
def test_sync_seller_deactivates_only_after_full_bidirectional_coverage(
    conn: sqlite3.Connection,
    total: int,
) -> None:
    """A listing that vanishes from an oversized shop still goes inactive on the next sync."""
    sources.sync_seller(conn, PagedInventoryClient(total), "allcitydublin")

    result = sources.sync_seller(conn, PagedInventoryClient(total - 1), "allcitydublin")

    assert "error" not in result
    assert result["deactivated"] == 1
    gone = conn.execute(
        "SELECT active FROM listings WHERE listing_id = ?", (total,)
    ).fetchone()
    assert gone["active"] == 0
