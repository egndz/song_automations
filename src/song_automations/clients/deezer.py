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
            DeezerError: If Deezer reports an error.
        """
        for attempt in range(QUOTA_RETRIES + 1):
            response = self._http.get(f"{self.API_BASE}{path}", params=params)
            response.raise_for_status()
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            if not error:
                time.sleep(self._pause)
                return body
            if error.get("code") != QUOTA_ERROR_CODE or attempt == QUOTA_RETRIES:
                raise DeezerError(str(error.get("message") or error))
            time.sleep(QUOTA_WAIT_SECONDS * (attempt + 1))
        raise DeezerError("no answer")

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
