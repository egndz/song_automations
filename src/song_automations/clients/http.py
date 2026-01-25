"""HTTP utilities for API clients."""

import time

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ConnectTimeout,
)


def retry_on_transient_error(func):
    """Decorator that retries on transient HTTP errors.

    Retries up to 3 times with exponential backoff (2-30 seconds).
    Handles connection errors, timeouts, and rate limits (429).

    Args:
        func: The function to wrap.

    Returns:
        Wrapped function with retry logic.
    """
    return retry(
        retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )(func)


class RateLimitError(Exception):
    """Raised when API returns 429 Too Many Requests."""

    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after} seconds.")


def handle_rate_limit(response: httpx.Response) -> None:
    """Check response for rate limiting and handle appropriately.

    Args:
        response: The HTTP response to check.

    Raises:
        RateLimitError: If response indicates rate limiting (429).
    """
    if response.status_code == 429:
        retry_after = int(response.headers.get("Retry-After", "60"))
        raise RateLimitError(retry_after)


def wait_for_rate_limit(retry_after: int) -> None:
    """Wait for the specified rate limit period.

    Args:
        retry_after: Seconds to wait before retrying.
    """
    time.sleep(retry_after)
