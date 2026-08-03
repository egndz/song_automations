"""Rate-limited HTTP client for the Discogs API.

The client adapts to the live rate limit budget reported by Discogs instead of
sleeping a fixed amount between requests, and it degrades to ``None`` on the
error classes that must never terminate a long crawl.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator

import httpx

try:
    from radio.config import BASE_URL, MAX_SEARCH_ITEMS, PER_PAGE, RATELIMIT_FLOOR, USER_AGENT
except ImportError:
    BASE_URL = "https://api.discogs.com"
    USER_AGENT = "SellerRadio/0.1 +https://github.com/egndz"
    MAX_SEARCH_ITEMS = 10000
    PER_PAGE = 100
    RATELIMIT_FLOOR = 3

logger = logging.getLogger(__name__)

REMAINING_HEADER = "X-Discogs-Ratelimit-Remaining"
TOTAL_HEADER = "X-Discogs-Ratelimit"
DEFAULT_TOTAL = 60
WINDOW_SECONDS = 60.0
REQUEST_TIMEOUT = 30.0
RATE_LIMIT_BACKOFF: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0)
MAX_RATE_LIMIT_ATTEMPTS = 5
SERVER_RETRIES = 3
SERVER_BACKOFF = 2.0
NETWORK_RETRIES = 3
NETWORK_BACKOFF = 2.0
SOFT_FAIL_STATUSES = frozenset({401, 403, 404})
ITEM_KEYS: tuple[str, ...] = ("listings", "results", "wants", "releases", "items")
SEARCH_PATH = "/database/search"


class DiscogsClient:
    """Thin, resilient wrapper around the Discogs REST API.

    Attributes:
        request_count: Total number of HTTP requests issued so far.
    """

    def __init__(
        self,
        token: str,
        user_agent: str = USER_AGENT,
        base_url: str = BASE_URL,
        sleep: Callable[[float], None] = time.sleep,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Builds a client with authentication headers applied once.

        Args:
            token: Discogs personal access token.
            user_agent: User-Agent header value, mandatory for the Discogs API.
            base_url: API root the relative paths are resolved against.
            sleep: Blocking sleep callable, injected so tests run instantly.
            transport: Optional httpx transport, used by tests to fake responses.
        """
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._remaining: int | None = None
        self._total = DEFAULT_TOTAL
        self._request_count = 0
        self._skip_next_wait = False
        self._client = httpx.Client(
            headers={
                "Authorization": f"Discogs token={token}",
                "User-Agent": user_agent,
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
            transport=transport,
        )

    @property
    def request_count(self) -> int:
        """Returns the number of HTTP requests issued by this client.

        Returns:
            Total request count including retries.
        """
        return self._request_count

    @property
    def remaining(self) -> int | None:
        """Returns the last known remaining rate limit budget.

        Returns:
            Remaining requests in the current window, or None before the first
            response has been seen.
        """
        return self._remaining

    def get(self, path: str, params: dict | None = None) -> dict | None:
        """Performs a GET request and decodes the JSON body.

        Args:
            path: Absolute URL or path relative to the API root.
            params: Optional query parameters.

        Returns:
            Decoded JSON object, or None when the request failed or the body
            could not be parsed.
        """
        response = self._request("GET", path, params=params)
        if response is None:
            return None
        try:
            payload = response.json()
        except ValueError:
            logger.warning("Unparseable JSON body from %s", path)
            return None
        if not isinstance(payload, dict):
            logger.warning("Unexpected JSON shape from %s", path)
            return None
        return payload

    def put(self, path: str) -> bool:
        """Performs a PUT request without a body.

        Args:
            path: Absolute URL or path relative to the API root.

        Returns:
            True when the API answered with a 2xx status, False otherwise.
        """
        return self._request("PUT", path) is not None

    def delete(self, path: str) -> bool:
        """Performs a DELETE request.

        Args:
            path: Absolute URL or path relative to the API root.

        Returns:
            True when the API answered with a 2xx status, False otherwise.
        """
        return self._request("DELETE", path) is not None

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        max_items: int | None = None,
        items_key: str | None = None,
    ) -> Iterator[dict]:
        """Iterates every item across the pages of a paginated endpoint.

        A 404 on a follow-up page is treated as a clean end of iteration because
        the Discogs deep pagination cap surfaces that way.

        Args:
            path: Absolute URL or path relative to the API root.
            params: Optional query parameters, per_page defaults to PER_PAGE.
            max_items: Optional ceiling on the number of yielded items.
            items_key: Payload key holding the item list, auto-detected when None.

        Yields:
            Item dictionaries in page order.
        """
        base_params = dict(params or {})
        base_params.setdefault("per_page", PER_PAGE)
        limit = self._item_limit(path, max_items)
        page = 1
        pages: int | None = None
        yielded = 0

        while limit is None or yielded < limit:
            page_params = dict(base_params)
            page_params["page"] = page
            response = self._request("GET", path, params=page_params)
            if response is None:
                if page > 1:
                    logger.info("Pagination for %s ended at page %d", path, page - 1)
                break
            try:
                body = response.json()
            except ValueError:
                logger.warning("Unparseable JSON page %d from %s", page, path)
                break
            if not isinstance(body, dict):
                logger.warning("Unexpected page shape at page %d from %s", page, path)
                break

            items = self._extract_items(body, items_key)
            if not items:
                break
            for item in items:
                if limit is not None and yielded >= limit:
                    return
                if isinstance(item, dict):
                    yield item
                    yielded += 1

            if pages is None:
                pages = self._page_total(body)
            if pages is not None and page >= pages:
                break
            page += 1

    def identity(self) -> dict | None:
        """Fetches the authenticated user identity.

        Returns:
            Identity payload, or None when the token is rejected.
        """
        return self.get("/oauth/identity")

    def close(self) -> None:
        """Closes the underlying HTTP connection pool."""
        self._client.close()

    def _item_limit(self, path: str, max_items: int | None) -> int | None:
        """Resolves the effective ceiling of items for a pagination run.

        The 10,000 item deep pagination cap is enforced for database search,
        which is the endpoint the cap applies to; other endpoints rely on the
        404 end-of-crawl signal so large inventories are not truncated.

        Args:
            path: Request path being paginated.
            max_items: Caller supplied ceiling, if any.

        Returns:
            Maximum number of items to yield, or None when unbounded.
        """
        capped = SEARCH_PATH in path
        if capped and max_items is not None:
            return min(max_items, MAX_SEARCH_ITEMS)
        if capped:
            return MAX_SEARCH_ITEMS
        return max_items

    def _extract_items(self, body: dict, items_key: str | None) -> list:
        """Pulls the item list out of a paginated payload.

        Args:
            body: Decoded page payload.
            items_key: Explicit key, or None to auto-detect a known key.

        Returns:
            List of items, empty when no list could be located.
        """
        if items_key is not None:
            value = body.get(items_key)
            return value if isinstance(value, list) else []
        for key in ITEM_KEYS:
            value = body.get(key)
            if isinstance(value, list):
                return value
        return []

    def _page_total(self, body: dict) -> int | None:
        """Reads the reported page count from a payload.

        Args:
            body: Decoded page payload.

        Returns:
            Number of pages, or None when the payload does not report one.
        """
        pagination = body.get("pagination")
        if not isinstance(pagination, dict):
            return None
        pages = pagination.get("pages")
        return pages if isinstance(pages, int) else None

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
    ) -> httpx.Response | None:
        """Issues a request applying rate limiting and the retry policy.

        Args:
            method: HTTP verb.
            path: Absolute URL or path relative to the API root.
            params: Optional query parameters.

        Returns:
            The successful response, or None for a soft failure or an exhausted
            retry budget.
        """
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        rate_attempts = 0
        server_attempts = 0
        network_attempts = 0

        while True:
            self._await_budget()
            self._request_count += 1
            try:
                response = self._client.request(method, url, params=params)
            except httpx.HTTPError as exc:
                network_attempts += 1
                if network_attempts > NETWORK_RETRIES:
                    logger.warning("Network failure on %s %s: %s", method, url, exc)
                    return None
                self._sleep(NETWORK_BACKOFF * network_attempts)
                continue

            self._track_budget(response)
            status = response.status_code

            if 200 <= status < 300:
                return response

            if status == 429:
                rate_attempts += 1
                self._remaining = 0
                if rate_attempts >= MAX_RATE_LIMIT_ATTEMPTS:
                    logger.warning("Rate limit not cleared for %s %s", method, url)
                    return None
                index = min(rate_attempts - 1, len(RATE_LIMIT_BACKOFF) - 1)
                self._sleep(RATE_LIMIT_BACKOFF[index])
                self._skip_next_wait = True
                continue

            if status in SOFT_FAIL_STATUSES:
                logger.warning("Skipping %s %s: HTTP %d", method, url, status)
                return None

            if status >= 500:
                server_attempts += 1
                if server_attempts > SERVER_RETRIES:
                    logger.warning("Server error on %s %s: HTTP %d", method, url, status)
                    return None
                self._sleep(SERVER_BACKOFF * server_attempts)
                continue

            logger.warning("Unexpected status on %s %s: HTTP %d", method, url, status)
            return None

    def _await_budget(self) -> None:
        """Sleeps out the rate limit window only when the budget is exhausted.

        A single retry immediately after a 429 backoff is allowed through without a second
        wait, because that backoff has already been served. The budget is never optimistically
        restored by the 429 path itself, so a retry that answers without rate limit headers
        still leaves the client believing the budget is spent and the next request pauses for a
        full window instead of resuming a burst that would earn a ban.
        """
        if self._skip_next_wait:
            self._skip_next_wait = False
            return
        if self._remaining is None or self._remaining > RATELIMIT_FLOOR:
            return
        logger.info("Rate limit budget at %d, pausing for the window", self._remaining)
        self._sleep(WINDOW_SECONDS)
        self._remaining = self._total

    def _track_budget(self, response: httpx.Response) -> None:
        """Records the rate limit budget reported on a response.

        Args:
            response: Response whose headers carry the rate limit counters.
        """
        total = self._header_int(response, TOTAL_HEADER)
        if total is not None and total > 0:
            self._total = total
        remaining = self._header_int(response, REMAINING_HEADER)
        if remaining is not None:
            self._remaining = remaining

    def _header_int(self, response: httpx.Response, name: str) -> int | None:
        """Parses an integer header value.

        Args:
            response: Response to read the header from.
            name: Header name.

        Returns:
            Parsed integer, or None when absent or malformed.
        """
        raw = response.headers.get(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None
