"""Reads the public Deezer catalogue, which needs no account and lists a recording code per track."""

import time

import httpx

from song_automations.clients.http import DEFAULT_TIMEOUT, retry_on_transient_error

QUOTA_ERROR_CODE = 4
QUOTA_RETRIES = 3
QUOTA_WAIT_SECONDS = 2.0
PAUSE_SECONDS = 0.11


class DeezerError(RuntimeError):
    """Deezer answered with an error instead of data."""


class DeezerQuotaError(DeezerError):
    """Deezer keeps refusing requests because too many were sent."""


class DeezerClient:
    """Client for the unauthenticated Deezer API.

    Args:
        http_client: HTTP client to use, a new one when omitted.
        pause_seconds: Wait after each request, to stay under 50 requests per 5 seconds.
    """

    API_BASE = "https://api.deezer.com"

    def __init__(self, http_client: httpx.Client | None = None, pause_seconds: float = PAUSE_SECONDS) -> None:
        self._http = http_client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._pause = pause_seconds

    @retry_on_transient_error
    def _get(self, path: str, params: dict | None = None) -> dict:
        """Fetches one JSON document, waiting when the quota is used up.

        Args:
            path: Path below the API base.
            params: Query parameters.

        Returns:
            Decoded body.

        Raises:
            DeezerQuotaError: If the quota does not clear after waiting.
            DeezerError: If Deezer reports any other error or sends no JSON object.
        """
        for attempt in range(QUOTA_RETRIES + 1):
            response = self._http.get(f"{self.API_BASE}{path}", params=params)
            response.raise_for_status()
            try:
                body = response.json()
            except ValueError as error:
                raise DeezerError("answer is not JSON") from error
            if not isinstance(body, dict):
                raise DeezerError("answer is not an object")
            error = body.get("error")
            if not error:
                time.sleep(self._pause)
                return body
            code = error.get("code") if isinstance(error, dict) else None
            if code != QUOTA_ERROR_CODE:
                raise DeezerError(str(error))
            time.sleep(QUOTA_WAIT_SECONDS * (attempt + 1))
        raise DeezerQuotaError("quota limit exceeded")

    def search_raw(self, query: str, limit: int = 25) -> list[dict]:
        """Searches tracks.

        Args:
            query: Search query.
            limit: Maximum results.

        Returns:
            Raw tracks, without recording codes.
        """
        return self._get("/search", {"q": query, "limit": limit}).get("data") or []

    def track_raw(self, track_id: str) -> dict:
        """Reads one track, including its recording code.

        Args:
            track_id: Deezer track id.

        Returns:
            Raw track.
        """
        return self._get(f"/track/{track_id}")
