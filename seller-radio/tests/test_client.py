"""Tests for the rate-limited Discogs HTTP client."""

from __future__ import annotations

import httpx
import pytest

from radio.client import (
    MAX_RATE_LIMIT_ATTEMPTS,
    NETWORK_RETRIES,
    RATELIMIT_FLOOR,
    SERVER_RETRIES,
    WINDOW_SECONDS,
    DiscogsClient,
)


class SleepRecorder:
    """Callable that records sleep durations instead of blocking.

    Attributes:
        calls: Durations passed to the recorder, in call order.
    """

    def __init__(self) -> None:
        """Initialises an empty call log."""
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        """Records a sleep request.

        Args:
            seconds: Duration the caller wanted to sleep for.
        """
        self.calls.append(seconds)


def headers(remaining: int | None = 55, total: int = 60) -> dict[str, str]:
    """Builds Discogs rate limit response headers.

    Args:
        remaining: Remaining budget, omitted from the headers when None.
        total: Total window budget.

    Returns:
        Header mapping suitable for an httpx response.
    """
    values = {"X-Discogs-Ratelimit": str(total)}
    if remaining is not None:
        values["X-Discogs-Ratelimit-Remaining"] = str(remaining)
    return values


def make_client(handler, sleep: SleepRecorder | None = None) -> DiscogsClient:
    """Builds a client wired to a mock transport.

    Args:
        handler: Callable mapping an httpx request to an httpx response.
        sleep: Optional sleep recorder, created when not supplied.

    Returns:
        Configured client instance.
    """
    return DiscogsClient(
        token="test-token",
        sleep=sleep or SleepRecorder(),
        transport=httpx.MockTransport(handler),
    )


def sequence_handler(responses: list[httpx.Response]):
    """Builds a handler replaying a fixed response sequence.

    Args:
        responses: Responses returned in order, the last one repeats.

    Returns:
        Handler callable for httpx.MockTransport.
    """
    state = {"index": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = min(state["index"], len(responses) - 1)
        state["index"] += 1
        return responses[index]

    return handler


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"id": 1}, {"id": 1}),
        (
            200,
            {"pagination": {"pages": 1}, "results": []},
            {"pagination": {"pages": 1}, "results": []},
        ),
        (201, {"ok": True}, {"ok": True}),
    ],
)
def test_get_returns_decoded_body(status: int, body: dict, expected: dict) -> None:
    """A successful response is decoded into a dictionary."""
    client = make_client(lambda request: httpx.Response(status, json=body, headers=headers()))
    assert client.get("/releases/1") == expected
    assert client.request_count == 1


def test_auth_and_user_agent_headers_are_sent() -> None:
    """Every request carries the token and the mandatory User-Agent."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, json={}, headers=headers())

    make_client(handler).get("/oauth/identity")
    assert seen["authorization"] == "Discogs token=test-token"
    assert seen["user-agent"].startswith("SellerRadio/")


@pytest.mark.parametrize("status", [401, 403, 404])
def test_soft_failures_return_none_without_retry(status: int) -> None:
    """Auth and missing resource errors are skipped, never raised or retried."""
    sleep = SleepRecorder()
    client = make_client(
        lambda request: httpx.Response(status, json={"message": "nope"}, headers=headers()),
        sleep=sleep,
    )
    assert client.get("/users/private/inventory") is None
    assert client.request_count == 1
    assert sleep.calls == []


@pytest.mark.parametrize(
    ("first_status", "expected_sleep"),
    [
        (429, 5.0),
        (500, 2.0),
        (502, 2.0),
        (503, 2.0),
    ],
)
def test_retryable_status_then_success(first_status: int, expected_sleep: float) -> None:
    """A retryable failure is retried and the following success is returned."""
    sleep = SleepRecorder()
    client = make_client(
        sequence_handler(
            [
                httpx.Response(first_status, json={"message": "later"}, headers=headers()),
                httpx.Response(200, json={"id": 7}, headers=headers()),
            ]
        ),
        sleep=sleep,
    )
    assert client.get("/releases/7") == {"id": 7}
    assert client.request_count == 2
    assert sleep.calls == [expected_sleep]


@pytest.mark.parametrize(
    ("status", "expected_requests", "expected_sleeps"),
    [
        (429, MAX_RATE_LIMIT_ATTEMPTS, [5.0, 10.0, 20.0, 40.0]),
        (500, SERVER_RETRIES + 1, [2.0, 4.0, 6.0]),
    ],
)
def test_retry_budget_is_bounded(
    status: int, expected_requests: int, expected_sleeps: list[float]
) -> None:
    """Persistent retryable errors exhaust a bounded budget and return None."""
    sleep = SleepRecorder()
    no_budget_headers = headers(remaining=None)
    client = make_client(
        lambda request: httpx.Response(status, json={"message": "no"}, headers=no_budget_headers),
        sleep=sleep,
    )
    assert client.get("/releases/7") is None
    assert client.request_count == expected_requests
    assert sleep.calls == expected_sleeps


def test_network_errors_are_retried_then_give_up() -> None:
    """Transport exceptions are retried a bounded number of times."""
    sleep = SleepRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = make_client(handler, sleep=sleep)
    assert client.get("/releases/7") is None
    assert client.request_count == NETWORK_RETRIES + 1
    assert sleep.calls == [2.0, 4.0, 6.0]


def test_network_error_then_success() -> None:
    """A single transport failure does not lose the request."""
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"id": 3}, headers=headers())

    client = make_client(handler)
    assert client.get("/releases/3") == {"id": 3}
    assert client.request_count == 2


@pytest.mark.parametrize("remaining", [60, 30, RATELIMIT_FLOOR + 1])
def test_no_sleep_while_budget_is_healthy(remaining: int) -> None:
    """The client never pauses while rate limit budget remains."""
    sleep = SleepRecorder()
    client = make_client(
        lambda request: httpx.Response(200, json={"id": 1}, headers=headers(remaining=remaining)),
        sleep=sleep,
    )
    for _ in range(4):
        client.get("/releases/1")
    assert sleep.calls == []
    assert client.request_count == 4


@pytest.mark.parametrize("remaining", [RATELIMIT_FLOOR, 1, 0])
def test_sleeps_for_window_when_budget_hits_floor(remaining: int) -> None:
    """Reaching the floor pauses for the window before the next request."""
    sleep = SleepRecorder()
    client = make_client(
        lambda request: httpx.Response(200, json={"id": 1}, headers=headers(remaining=remaining)),
        sleep=sleep,
    )
    client.get("/releases/1")
    assert sleep.calls == []
    client.get("/releases/1")
    assert sleep.calls == [WINDOW_SECONDS]


@pytest.mark.parametrize(
    ("method", "status", "expected"),
    [
        ("put", 201, True),
        ("put", 200, True),
        ("put", 404, False),
        ("put", 403, False),
        ("delete", 204, True),
        ("delete", 404, False),
        ("delete", 401, False),
    ],
)
def test_put_and_delete_report_success_without_raising(
    method: str, status: int, expected: bool
) -> None:
    """Wantlist writes report a boolean and never raise."""
    client = make_client(lambda request: httpx.Response(status, headers=headers()))
    call = getattr(client, method)
    assert call("/users/egunduz/wants/12345") is expected


@pytest.mark.parametrize(
    ("payload_key", "items_key"),
    [
        ("listings", None),
        ("results", None),
        ("wants", None),
        ("releases", None),
        ("items", None),
        ("results", "results"),
    ],
)
def test_paginate_yields_items_across_pages(payload_key: str, items_key: str | None) -> None:
    """Pagination yields items, not pages, and stops at the reported page count."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        body = {
            "pagination": {"page": page, "pages": 3, "per_page": 2},
            payload_key: [{"id": page * 10}, {"id": page * 10 + 1}],
        }
        return httpx.Response(200, json=body, headers=headers())

    client = make_client(handler)
    items = list(client.paginate("/users/allcitydublin/inventory", items_key=items_key))
    assert [item["id"] for item in items] == [10, 11, 20, 21, 30, 31]
    assert client.request_count == 3


def test_paginate_treats_page_404_as_clean_end() -> None:
    """A 404 beyond the deep pagination cap ends iteration without raising."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page > 3:
            return httpx.Response(
                404,
                json={"message": f"Page {page} is outside of valid range [1, 3]."},
                headers=headers(),
            )
        body = {
            "pagination": {"page": page, "pages": 9999},
            "results": [{"id": page}],
        }
        return httpx.Response(200, json=body, headers=headers())

    client = make_client(handler)
    items = list(client.paginate("/database/search", params={"type": "release"}))
    assert [item["id"] for item in items] == [1, 2, 3]
    assert client.request_count == 4


@pytest.mark.parametrize(
    ("max_items", "expected"),
    [(1, 1), (3, 3), (5, 5), (None, 6)],
)
def test_paginate_respects_max_items(max_items: int | None, expected: int) -> None:
    """The caller supplied ceiling stops iteration early."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        body = {
            "pagination": {"page": page, "pages": 3},
            "listings": [{"id": page * 10}, {"id": page * 10 + 1}],
        }
        return httpx.Response(200, json=body, headers=headers())

    client = make_client(handler)
    items = list(client.paginate("/users/allcitydublin/inventory", max_items=max_items))
    assert len(items) == expected


def test_paginate_enforces_search_item_cap() -> None:
    """Database search pagination stops at the hard ten thousand item cap."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        body = {
            "pagination": {"page": page, "pages": 500},
            "results": [{"id": page * 100 + offset} for offset in range(100)],
        }
        return httpx.Response(200, json=body, headers=headers())

    client = make_client(handler)
    items = list(client.paginate("/database/search", params={"type": "release"}))
    assert len(items) == 10000
    assert client.request_count == 100


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"pagination": {"pages": 1}, "results": []}, 0),
        ({"pagination": {"pages": 1}}, 0),
        ({"pagination": {"pages": 1}, "results": [{"id": 1}]}, 1),
    ],
)
def test_paginate_handles_empty_and_missing_item_lists(body: dict, expected: int) -> None:
    """Missing or empty item lists end iteration instead of looping."""
    client = make_client(lambda request: httpx.Response(200, json=body, headers=headers()))
    assert len(list(client.paginate("/users/egunduz/wants"))) == expected


def test_paginate_sends_default_per_page() -> None:
    """Pagination defaults per_page to the configured page size."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["per_page"])
        return httpx.Response(
            200,
            json={"pagination": {"pages": 1}, "wants": [{"id": 1}]},
            headers=headers(),
        )

    client = make_client(handler)
    list(client.paginate("/users/egunduz/wants"))
    assert seen == ["100"]


@pytest.mark.parametrize("status", [401, 404])
def test_identity_soft_fails(status: int) -> None:
    """An unusable token yields None rather than an exception."""
    client = make_client(lambda request: httpx.Response(status, json={}, headers=headers()))
    assert client.identity() is None


def test_get_returns_none_on_unparseable_body() -> None:
    """A malformed body is reported as a failure, not a crash."""
    client = make_client(lambda request: httpx.Response(200, text="not json", headers=headers()))
    assert client.get("/releases/1") is None
