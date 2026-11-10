"""Tests for the shared HTTP helpers."""

import httpx
import pytest

from song_automations.clients.http import (
    DEFAULT_RETRY_AFTER_SECONDS,
    RateLimitError,
    handle_rate_limit,
    parse_retry_after,
)


class TestRetryAfter:
    """Tests for reading how long a platform asks to wait."""

    @pytest.mark.parametrize(
        ("headers", "default", "expected"),
        [
            ({"Retry-After": "7"}, 60, 7),
            ({"Retry-After": 12}, 60, 12),
            ({"Retry-After": "2.6"}, 60, 2),
            ({"Retry-After": "-4"}, 60, 0),
            ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 60, 60),
            ({"Retry-After": ""}, 5, 5),
            ({"Retry-After": None}, 5, 5),
            ({"Retry-After": "inf"}, 60, 60),
            ({"Retry-After": "nan"}, 60, 60),
            (httpx.Headers({"retry-after": "30"}), 60, 30),
            (httpx.Headers({}), 60, 60),
            ({}, 9, 9),
            (None, 9, 9),
            ("not a mapping", 9, 9),
        ],
    )
    def test_header_is_read_or_defaulted(self, headers, default, expected):
        """A missing, empty or malformed header falls back to the given default."""
        assert parse_retry_after(headers, default) == expected

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"Retry-After": "90"}, 90),
            ({"Retry-After": "1.5"}, 1),
            ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": "soon"}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": ""}, DEFAULT_RETRY_AFTER_SECONDS),
            ({}, DEFAULT_RETRY_AFTER_SECONDS),
        ],
    )
    def test_limited_response_raises_with_a_usable_wait(self, headers, expected):
        """A 429 becomes a rate limit error even when its header is not a number."""
        with pytest.raises(RateLimitError) as caught:
            handle_rate_limit(httpx.Response(429, headers=headers))
        assert caught.value.retry_after == expected

    @pytest.mark.parametrize("status", [200, 204, 401, 422, 500])
    def test_other_responses_pass(self, status):
        """Only a 429 counts as a rate limit."""
        assert handle_rate_limit(httpx.Response(status, headers={"Retry-After": "soon"})) is None
