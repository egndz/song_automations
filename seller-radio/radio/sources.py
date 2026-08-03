"""Release id sources: seller inventory sync, search bank crawling, and account sync.

Every function in this module is defensive by design. A single unavailable seller, a private
inventory, or the Discogs deep pagination cap must never terminate a long run, so failures are
reported through the returned result dictionaries instead of exceptions.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from radio.client import DiscogsClient
from radio.config import DEFAULT_CURRENCY, MAX_SEARCH_ITEMS, PER_PAGE, bank_key
from radio.db import now_iso, record_sync

logger = logging.getLogger(__name__)

SEARCH_PATH = "/database/search"
SOURCE_SELLER = "seller"
SOURCE_SEARCH = "search"
DEFAULT_SEARCH_TYPE = "release"
DEFAULT_SEARCH_FORMAT = "Vinyl"
TITLE_SEPARATOR = " - "
PASSTHROUGH_BANK_KEYS: tuple[str, ...] = ("genre", "style", "country", "label")
MAX_INVENTORY_PAGES = 2000
FOREIGN_INVENTORY_PAGE_CEILING = 100
INVENTORY_SORT = "listed"
CRAWL_COMMIT_EVERY = PER_PAGE

_LISTING_UPSERT = """
INSERT INTO listings (
    listing_id, seller, release_id, price, currency, condition, sleeve,
    ships_from, listing_uri, comments, posted, active, last_seen
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
ON CONFLICT(listing_id) DO UPDATE SET
    seller = excluded.seller,
    release_id = excluded.release_id,
    price = excluded.price,
    currency = excluded.currency,
    condition = excluded.condition,
    sleeve = excluded.sleeve,
    ships_from = excluded.ships_from,
    listing_uri = excluded.listing_uri,
    comments = excluded.comments,
    posted = excluded.posted,
    active = 1,
    last_seen = excluded.last_seen
"""

_POOL_SELLER_UPSERT = """
INSERT INTO pool (release_id, source, bank_key, discovered_at)
VALUES (?, ?, NULL, ?)
ON CONFLICT(release_id) DO UPDATE SET source = excluded.source
"""

_POOL_SEARCH_INSERT = """
INSERT INTO pool (release_id, source, bank_key, discovered_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(release_id) DO NOTHING
"""

_RELEASE_SEED_FROM_LISTING = """
INSERT INTO releases (
    release_id, artist, title, label, catno, year, thumb, has_video, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)
ON CONFLICT(release_id) DO NOTHING
"""

_RELEASE_SEED_FROM_SEARCH = """
INSERT INTO releases (
    release_id, artist, title, label, catno, year, country, genres, styles,
    formats, master_id, thumb, has_video, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
ON CONFLICT(release_id) DO NOTHING
"""


def _walk_inventory(
    conn: sqlite3.Connection,
    client: DiscogsClient,
    path: str,
    base_params: dict[str, Any],
    seller: str,
    stamp: str,
    seen: set[int],
    sort_order: str,
    page_target: int,
    first_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Walk one sort direction of a seller inventory, committing every page as it arrives.

    Discogs refuses page numbers above one hundred for inventories other than your own, which
    caps a single direction at ten thousand listings. Walking the same sort key in reverse
    reaches the tail, so a caller can cover a larger shop by combining an ascending pass with a
    descending one and relying on the shared seen set to discard the overlap.

    Args:
        conn: Open SQLite connection.
        client: Discogs API client.
        path: Inventory path for the seller.
        base_params: Query parameters shared by both directions.
        seller: Discogs seller username, used for logging.
        stamp: Timestamp written to every listing touched by this walk.
        seen: Listing ids observed so far, mutated in place and shared across directions.
        sort_order: Either "asc" or "desc".
        page_target: Last page this direction should read.
        first_payload: Already fetched first page, or None to fetch it.

    Returns:
        Mapping with the number of newly pooled releases under "new_releases", the last page
        reached under "page", and whether the direction finished under "complete".
    """
    params = dict(base_params, sort_order=sort_order)
    payload = first_payload
    page = 1
    new_releases = 0
    complete = False

    while True:
        if payload is None:
            payload = client.get(path, dict(params, page=page))
            if payload is None:
                logger.warning(
                    "Inventory for %s failed at %s page %d of %d",
                    seller,
                    sort_order,
                    page,
                    page_target,
                )
                break

        rows = _as_dict_list(payload.get("listings"))
        if not rows:
            complete = True
            break

        for listing in rows:
            listing_id = _as_int(listing.get("id"))
            release = listing.get("release")
            release_id = _as_int(release.get("id")) if isinstance(release, dict) else None
            if listing_id is None or release_id is None:
                continue
            _write_listing(conn, seller, listing, listing_id, release_id, stamp)
            seen.add(listing_id)
            if _upgrade_pool_to_seller(conn, release_id, stamp):
                new_releases += 1
            _seed_release_from_listing(conn, release_id, release)
        conn.commit()

        if page >= page_target:
            complete = True
            break
        page += 1
        payload = None

    return {"new_releases": new_releases, "page": page, "complete": complete}


def sync_seller(
    conn: sqlite3.Connection,
    client: DiscogsClient,
    seller: str,
) -> dict[str, Any]:
    """Mirror one seller's public inventory into the listings and pool tables.

    Every fetched page is written back as active and committed immediately, so an interrupt or
    a crash halfway through a hundred page inventory keeps the work already paid for with the
    rate limit budget. Listings that vanished from the inventory are deactivated only after the
    walk reached the last reported page, because a partial walk cannot distinguish a sold record
    from a page that simply failed to load, and deactivating on a partial walk would silently
    empty the crate. A private or missing inventory answers with a soft failure, which is
    reported through the "error" key rather than raised.

    Args:
        conn: Open SQLite connection.
        client: Discogs API client.
        seller: Discogs seller username whose inventory is mirrored.

    Returns:
        Mapping with the seller name, the number of active listings written, the number of
        release ids newly added to the pool, the number of listings that went inactive, and an
        optional "error" key when the inventory could not be read in full.
    """
    path = f"/users/{seller}/inventory"
    base_params: dict[str, Any] = {
        "status": "For Sale",
        "per_page": PER_PAGE,
        "curr_abbr": DEFAULT_CURRENCY,
        "sort": INVENTORY_SORT,
    }
    sync_key = f"seller:{seller}"

    payload = client.get(path, dict(base_params, sort_order="asc", page=1))
    if payload is None:
        logger.warning("Inventory for %s is unavailable, skipping the seller", seller)
        record_sync(conn, sync_key, 0)
        return {
            "seller": seller,
            "listings": 0,
            "new_releases": 0,
            "deactivated": 0,
            "error": "inventory unavailable",
        }

    previously_active = {
        int(row[0])
        for row in conn.execute(
            "SELECT listing_id FROM listings WHERE seller = ? AND active = 1", (seller,)
        )
    }

    stamp = now_iso()
    seen: set[int] = set()
    pages = _page_count(payload)
    ceiling = FOREIGN_INVENTORY_PAGE_CEILING
    head_target = min(pages, ceiling) if pages is not None else ceiling

    head = _walk_inventory(
        conn, client, path, base_params, seller, stamp, seen, "asc", head_target, payload
    )
    new_releases = head["new_releases"]
    complete = head["complete"]
    error = None if complete else (
        f"inventory walk stopped at page {head['page']}, nothing was deactivated"
    )

    if complete and pages is not None and pages > ceiling:
        tail_target = pages - ceiling
        if tail_target > ceiling:
            complete = False
            error = (
                f"inventory reports {pages} pages but only {2 * ceiling} are reachable from "
                f"both ends, leaving {pages - 2 * ceiling} unreadable, nothing was deactivated"
            )
        else:
            logger.info(
                "Inventory for %s exceeds the %d page ceiling, walking the last %d pages in "
                "reverse to reach the tail",
                seller,
                ceiling,
                tail_target,
            )
            tail = _walk_inventory(
                conn, client, path, base_params, seller, stamp, seen, "desc", tail_target, None
            )
            new_releases += tail["new_releases"]
            complete = tail["complete"]
            if not complete:
                error = (
                    f"inventory tail walk stopped at page {tail['page']}, "
                    "nothing was deactivated"
                )

    if not complete:
        record_sync(conn, sync_key, len(seen))
        return {
            "seller": seller,
            "listings": len(seen),
            "new_releases": new_releases,
            "deactivated": 0,
            "error": error,
        }

    stale = sorted(previously_active - seen)
    if stale:
        conn.executemany(
            "UPDATE listings SET active = 0 WHERE listing_id = ?",
            [(listing_id,) for listing_id in stale],
        )
        conn.commit()
    record_sync(conn, sync_key, len(seen))
    return {
        "seller": seller,
        "listings": len(seen),
        "new_releases": new_releases,
        "deactivated": len(stale),
    }


def sync_sellers(
    conn: sqlite3.Connection,
    client: DiscogsClient,
    sellers: list[str],
) -> dict[str, Any]:
    """Sync several seller inventories in sequence.

    Args:
        conn: Open SQLite connection.
        client: Discogs API client.
        sellers: Seller usernames to mirror.

    Returns:
        Mapping with the aggregate listing, new release, and deactivation counts, the per seller
        result dictionaries under "results", and the names that failed under "errors".
    """
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    listings = 0
    new_releases = 0
    deactivated = 0

    for seller in sellers:
        result = sync_seller(conn, client, seller)
        results.append(result)
        if result.get("error"):
            errors.append(seller)
            continue
        listings += result["listings"]
        new_releases += result["new_releases"]
        deactivated += result["deactivated"]

    record_sync(conn, "sellers", listings)
    return {
        "sellers": len(sellers),
        "listings": listings,
        "new_releases": new_releases,
        "deactivated": deactivated,
        "errors": errors,
        "results": results,
    }


def crawl_bank(
    conn: sqlite3.Connection,
    client: DiscogsClient,
    bank: dict,
) -> dict[str, Any]:
    """Crawl one search bank and add every matching release id to the pool.

    Pool rows are inserted without conflict handling beyond a no-op, so a release already
    discovered through a seller inventory is never downgraded to the search source. Writes are
    committed once per page rather than once per bank, so an interrupt on page ninety of a
    hundred page crawl keeps the eighty nine pages already paid for.

    Args:
        conn: Open SQLite connection.
        client: Discogs API client.
        bank: Search bank definition supporting the genre, style, country, label, format,
            year, year_from, year_to, and query keys.

    Returns:
        Mapping with the bank key, the number of pages consumed, the number of results seen,
        the number of release ids newly added to the pool, and a "capped" flag that is True when
        the crawl stopped at the Discogs deep pagination cap.
    """
    key = bank_key(bank)
    params = build_search_params(bank)
    stamp = now_iso()
    found = 0
    new = 0

    for result in client.paginate(
        SEARCH_PATH,
        params=params,
        max_items=MAX_SEARCH_ITEMS,
        items_key="results",
    ):
        release_id = _as_int(result.get("id"))
        if release_id is None:
            continue
        found += 1
        cursor = conn.execute(_POOL_SEARCH_INSERT, (release_id, SOURCE_SEARCH, key, stamp))
        if cursor.rowcount:
            new += 1
        _seed_release_from_search(conn, release_id, result)
        if found % CRAWL_COMMIT_EVERY == 0:
            conn.commit()

    conn.commit()
    capped = found >= MAX_SEARCH_ITEMS
    if capped:
        logger.warning("Bank %s hit the %d item cap, slice it more narrowly", key, MAX_SEARCH_ITEMS)
    record_sync(conn, f"bank:{key}", found)
    pages = (found + PER_PAGE - 1) // PER_PAGE
    return {"bank_key": key, "pages": pages, "found": found, "new": new, "capped": capped}


def crawl_banks(
    conn: sqlite3.Connection,
    client: DiscogsClient,
    banks: list[dict],
) -> dict[str, Any]:
    """Crawl several search banks in sequence.

    Args:
        conn: Open SQLite connection.
        client: Discogs API client.
        banks: Search bank definitions.

    Returns:
        Mapping with the aggregate found and new counts, the per bank result dictionaries under
        "results", and the keys of the banks that hit the pagination cap under "capped".
    """
    results: list[dict[str, Any]] = []
    capped: list[str] = []
    found = 0
    new = 0

    for bank in banks:
        result = crawl_bank(conn, client, bank)
        results.append(result)
        found += result["found"]
        new += result["new"]
        if result["capped"]:
            capped.append(result["bank_key"])

    record_sync(conn, "banks", found)
    return {"banks": len(banks), "found": found, "new": new, "capped": capped, "results": results}


def build_search_params(bank: dict) -> dict[str, Any]:
    """Translate a search bank definition into Discogs search query parameters.

    A year_from and year_to pair becomes the verified range syntax such as "1990-1999", while a
    lone year value is passed through unchanged.

    Args:
        bank: Search bank definition.

    Returns:
        Query parameters ready for the database search endpoint.
    """
    params: dict[str, Any] = {
        "type": bank.get("type") or DEFAULT_SEARCH_TYPE,
        "format": bank.get("format") or DEFAULT_SEARCH_FORMAT,
        "per_page": PER_PAGE,
    }
    for name in PASSTHROUGH_BANK_KEYS:
        value = bank.get(name)
        if value:
            params[name] = value
    query = bank.get("query")
    if query:
        params["q"] = query
    year = _build_year_param(bank)
    if year:
        params["year"] = year
    return params


def sync_owned(conn: sqlite3.Connection, client: DiscogsClient, username: str) -> int:
    """Replace the owned table with the user's full collection.

    Args:
        conn: Open SQLite connection.
        client: Discogs API client.
        username: Discogs username whose collection is read.

    Returns:
        Number of release ids stored. The existing table is left untouched when the fetch yields
        nothing, so a transient failure cannot erase the collection.
    """
    path = f"/users/{username}/collection/folders/0/releases"
    release_ids = _collect_release_ids(client, path, "releases")
    return _replace_release_table(conn, "owned", release_ids, "owned")


def sync_wantlist(conn: sqlite3.Connection, client: DiscogsClient, username: str) -> int:
    """Replace the wantlist table with the user's full wantlist.

    Args:
        conn: Open SQLite connection.
        client: Discogs API client.
        username: Discogs username whose wantlist is read.

    Returns:
        Number of release ids stored. The existing table is left untouched when the fetch yields
        nothing, so a transient failure cannot erase the wantlist.
    """
    path = f"/users/{username}/wants"
    release_ids = _collect_release_ids(client, path, "wants")
    return _replace_release_table(conn, "wantlist", release_ids, "wantlist")


def _collect_release_ids(client: DiscogsClient, path: str, items_key: str) -> list[int]:
    """Page an account endpoint and pull out the release ids in order.

    Args:
        client: Discogs API client.
        path: Endpoint path to paginate.
        items_key: Payload key holding the item list.

    Returns:
        Deduplicated release ids in the order they were seen.
    """
    seen: set[int] = set()
    ordered: list[int] = []
    for item in client.paginate(path, params={"per_page": PER_PAGE}, items_key=items_key):
        release_id = _as_int(item.get("id"))
        if release_id is None:
            basic = item.get("basic_information")
            if isinstance(basic, dict):
                release_id = _as_int(basic.get("id"))
        if release_id is None or release_id in seen:
            continue
        seen.add(release_id)
        ordered.append(release_id)
    return ordered


def _replace_release_table(
    conn: sqlite3.Connection,
    table: str,
    release_ids: list[int],
    sync_key: str,
) -> int:
    """Swap the contents of a release id table inside a single transaction.

    Args:
        conn: Open SQLite connection.
        table: Target table name, either "owned" or "wantlist".
        release_ids: Release ids to store.
        sync_key: Key used for the sync log entry.

    Returns:
        Number of release ids stored, or zero when nothing was fetched.
    """
    if not release_ids:
        logger.warning("No rows fetched for %s, keeping the existing table", table)
        record_sync(conn, sync_key, 0)
        return 0

    stamp = now_iso()
    conn.execute(f"DELETE FROM {table}")
    conn.executemany(
        f"INSERT INTO {table} (release_id, synced_at) VALUES (?, ?)",
        [(release_id, stamp) for release_id in release_ids],
    )
    conn.commit()
    record_sync(conn, sync_key, len(release_ids))
    return len(release_ids)


def _write_listing(
    conn: sqlite3.Connection,
    seller: str,
    listing: dict,
    listing_id: int,
    release_id: int,
    stamp: str,
) -> None:
    """Insert or refresh a single listing row as active.

    Args:
        conn: Open SQLite connection.
        seller: Seller username owning the listing.
        listing: Raw listing payload.
        listing_id: Listing identifier.
        release_id: Release identifier the listing points at.
        stamp: Timestamp recorded as last_seen.

    Returns:
        None.
    """
    price = listing.get("price")
    price_value: float | None = None
    currency: str | None = None
    if isinstance(price, dict):
        price_value = _as_float(price.get("value"))
        currency = price.get("currency")
    else:
        price_value = _as_float(price)

    conn.execute(
        _LISTING_UPSERT,
        (
            listing_id,
            seller,
            release_id,
            price_value,
            currency,
            listing.get("condition"),
            listing.get("sleeve_condition"),
            listing.get("ships_from"),
            listing.get("uri"),
            listing.get("comments"),
            listing.get("posted"),
            stamp,
        ),
    )


def _upgrade_pool_to_seller(conn: sqlite3.Connection, release_id: int, stamp: str) -> bool:
    """Add a release to the pool as a seller discovery, upgrading a search row in place.

    Args:
        conn: Open SQLite connection.
        release_id: Release identifier to record.
        stamp: Timestamp recorded on first discovery.

    Returns:
        True when the release was not in the pool before, False when it was already known.
    """
    known = conn.execute("SELECT 1 FROM pool WHERE release_id = ?", (release_id,)).fetchone()
    conn.execute(_POOL_SELLER_UPSERT, (release_id, SOURCE_SELLER, stamp))
    return known is None


def _seed_release_from_listing(conn: sqlite3.Connection, release_id: int, release: dict) -> None:
    """Store the cheap display metadata carried inside an inventory listing.

    The row is left unenriched, with has_video at zero and fetched_at null, so the enrichment
    pass still picks the release up and a later full fetch is never clobbered.

    Args:
        conn: Open SQLite connection.
        release_id: Release identifier.
        release: Nested release object from the listing payload.

    Returns:
        None.
    """
    conn.execute(
        _RELEASE_SEED_FROM_LISTING,
        (
            release_id,
            _clean_text(release.get("artist")),
            _clean_text(release.get("title")),
            _first_text(release.get("label")),
            _clean_text(release.get("catalog_number")),
            _as_int(release.get("year")) or None,
            _clean_text(release.get("thumbnail")),
        ),
    )


def _seed_release_from_search(conn: sqlite3.Connection, release_id: int, result: dict) -> None:
    """Store the metadata carried by a database search result.

    Search results use the singular "genre" and "style" keys and pack the artist and the title
    into one combined string, which is split on the first separator.

    Args:
        conn: Open SQLite connection.
        release_id: Release identifier.
        result: Raw search result payload.

    Returns:
        None.
    """
    artist, title = split_search_title(result.get("title"))
    conn.execute(
        _RELEASE_SEED_FROM_SEARCH,
        (
            release_id,
            artist,
            title,
            _first_text(result.get("label")),
            _clean_text(result.get("catno")),
            _as_int(result.get("year")) or None,
            _clean_text(result.get("country")),
            _json_list(result.get("genre")),
            _json_list(result.get("style")),
            _json_list(result.get("format")),
            _as_int(result.get("master_id")) or None,
            _clean_text(result.get("thumb")) or _clean_text(result.get("cover_image")),
        ),
    )


def split_search_title(raw: Any) -> tuple[str | None, str | None]:
    """Split a combined search title into an artist and a title.

    Args:
        raw: Combined "Artist - Title" string from a search result.

    Returns:
        Tuple of artist and title. The artist is None when the string carries no separator, and
        both entries are None when the string is empty.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    artist, separator, title = raw.partition(TITLE_SEPARATOR)
    if not separator:
        return None, raw.strip()
    artist = artist.strip()
    title = title.strip()
    if not title:
        return None, artist or None
    return artist or None, title


def _build_year_param(bank: dict) -> str | None:
    """Derive the year query parameter from a bank definition.

    Args:
        bank: Search bank definition.

    Returns:
        A single year, a "from-to" range, or None when the bank has no year constraint.
    """
    year_from = bank.get("year_from")
    year_to = bank.get("year_to")
    if year_from and year_to:
        return f"{year_from}-{year_to}"
    if year_from:
        return str(year_from)
    if year_to:
        return str(year_to)
    year = bank.get("year")
    return str(year) if year else None


def _page_count(payload: dict) -> int | None:
    """Read the reported page count from a paginated payload.

    Args:
        payload: Decoded page payload.

    Returns:
        Number of pages, or None when the payload does not report one.
    """
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        return None
    pages = pagination.get("pages")
    return pages if isinstance(pages, int) else None


def _as_dict_list(value: Any) -> list[dict]:
    """Coerce a payload field into a list of dictionaries.

    Args:
        value: Candidate list value.

    Returns:
        The dictionary entries of the value, empty when it is not a list.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_int(value: Any) -> int | None:
    """Coerce a payload value into an integer.

    Args:
        value: Candidate value.

    Returns:
        The integer value, or None when it cannot be parsed.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    """Coerce a payload value into a float.

    Args:
        value: Candidate value.

    Returns:
        The float value, or None when it cannot be parsed.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str | None:
    """Normalize a payload value into a trimmed string.

    Args:
        value: Candidate value.

    Returns:
        The trimmed string, or None when the value is empty or not a string.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _first_text(value: Any) -> str | None:
    """Take the first usable string from a value that may be a list.

    Args:
        value: Candidate string or list of strings.

    Returns:
        The first non-empty string, or None when there is none.
    """
    if isinstance(value, list):
        for item in value:
            text = _clean_text(item)
            if text:
                return text
        return None
    return _clean_text(value)


def _json_list(value: Any) -> str | None:
    """Serialize a list payload field as a JSON array.

    Args:
        value: Candidate list value.

    Returns:
        JSON encoded array of strings, or None when there is nothing to store.
    """
    if not isinstance(value, list):
        text = _clean_text(value)
        return json.dumps([text]) if text else None
    items = [item for item in (_clean_text(entry) for entry in value) if item]
    return json.dumps(items) if items else None
