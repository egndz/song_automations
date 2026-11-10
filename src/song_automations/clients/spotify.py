"""Spotify API client for playlist management and track search."""

import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import spotipy
from rich.console import Console
from rich.markup import escape
from spotipy.cache_handler import CacheFileHandler
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import (
    SpotifyOAuth,
    SpotifyOauthError,
    SpotifyStateError,
    start_local_http_server,
)

from song_automations.clients.http import parse_retry_after
from song_automations.config import Settings
from song_automations.errors import (
    LoginFailedError,
    LoginTimeoutError,
    RateLimitedError,
    ReauthRequiredError,
    SearchBudgetExhaustedError,
    UnattendedLoginError,
)

logger = logging.getLogger(__name__)
console = Console()

PLATFORM = "Spotify"
STALE_LOGIN_ERRORS = frozenset({"invalid_client", "invalid_grant"})
STALE_SUFFIX = ".stale"
STALE_STAMP = "%Y%m%d-%H%M%S"
PLAYLIST_CACHE_TTL = 300
MAX_WAIT_SECONDS = 60
MAX_ATTEMPTS = 5
SEARCH_PAUSE_SECONDS = 0.5
SEARCH_WINDOW_SECONDS = 24 * 3600
SEARCH_LEDGER_FILE = ".spotify_searches.json"
SEARCH_LIMIT = 10
DEFAULT_RETRY_AFTER_SECONDS = 5


def retry_after_seconds(headers: dict | None) -> int:
    """Reads how long Spotify asks to wait, whatever shape the header has.

    Args:
        headers: Response headers, possibly missing.

    Returns:
        Seconds to wait, the default when the header is absent or not a number.
    """
    return parse_retry_after(headers, DEFAULT_RETRY_AFTER_SECONDS)


class SearchLedger:
    """Remembers when searches were made, so the quota holds across runs.

    Spotify counts requests over about a day, not per run, so a dry run
    followed by a real run must share one allowance.

    Args:
        path: File that keeps the times, None to keep them in memory only.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._times = self._load()

    def _load(self) -> list[float]:
        """Reads the stored times.

        Returns:
            Times in seconds since the epoch, empty when nothing usable is stored.
        """
        if self._path is None or not self._path.exists():
            return []
        try:
            stored = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return []
        if not isinstance(stored, list):
            return []
        return [
            float(moment)
            for moment in stored
            if isinstance(moment, int | float) and not isinstance(moment, bool) and math.isfinite(moment)
        ]

    def recent(self, now: float) -> int:
        """Counts the searches of the last 24 hours and forgets older ones.

        The file is read again each time, so a review page and a scheduled
        sync running together share one count.

        Args:
            now: Current time in seconds since the epoch.

        Returns:
            Number of searches inside the window.
        """
        known = set(self._times) | set(self._load())
        self._times = sorted(moment for moment in known if now - moment < SEARCH_WINDOW_SECONDS)
        return len(self._times)

    def record(self, now: float) -> None:
        """Notes one search and stores the list.

        The list is written to a temporary file and moved into place, so a
        crash cannot leave a half-written count, and a disk that cannot be
        written to costs the stored count, not the run.

        Args:
            now: Current time in seconds since the epoch.
        """
        self._times.append(now)
        if self._path is None:
            return
        scratch = self._path.with_name(f"{self._path.name}.tmp")
        try:
            scratch.write_text(json.dumps(self._times))
            os.replace(scratch, self._path)
        except OSError as error:
            logger.warning("Could not store the Spotify search count: %s", error)

    def frees_at(self) -> float | None:
        """Tells when the oldest search leaves the window.

        Returns:
            Time in seconds since the epoch, None when nothing is recorded.
        """
        return min(self._times) + SEARCH_WINDOW_SECONDS if self._times else None


@dataclass
class CachedPlaylists:
    """Cache entry for user playlists.

    Args:
        playlists: List of cached playlists.
        timestamp: When the cache was populated.
    """

    playlists: list = field(default_factory=list)
    timestamp: float = 0.0

    def is_valid(self) -> bool:
        """Check if cache is still valid."""
        return time.time() - self.timestamp < PLAYLIST_CACHE_TTL


@dataclass
class SpotifyTrack:
    """Represents a Spotify track.

    Args:
        id: Spotify track ID.
        uri: Spotify track URI.
        name: Track name.
        artist: Primary artist name.
        artists: All artist names.
        album: Album name.
        popularity: Spotify popularity score (0-100).
        duration_ms: Track duration in milliseconds.
        is_playable: Whether the track is playable in the user's region.
    """

    id: str
    uri: str
    name: str
    artist: str
    artists: list[str]
    album: str
    popularity: int
    duration_ms: int
    is_playable: bool

    @property
    def full_title(self) -> str:
        """Full title for display."""
        return f"{self.artist} - {self.name}"


@dataclass
class SpotifyPlaylist:
    """Represents a Spotify playlist.

    Args:
        id: Spotify playlist ID.
        uri: Spotify playlist URI.
        name: Playlist name.
        owner_id: Owner's Spotify user ID.
        track_count: Number of tracks in the playlist.
        public: Whether the playlist is public.
    """

    id: str
    uri: str
    name: str
    owner_id: str
    track_count: int
    public: bool


@dataclass
class SearchResult:
    """Represents a search result with scoring metadata.

    Args:
        track: The Spotify track.
        is_verified: Whether the artist is verified (has high follower count).
    """

    track: SpotifyTrack
    is_verified: bool


class PrivateCacheFileHandler(CacheFileHandler):
    """Saves the Spotify login in a file only the user can read.

    Every write of the login goes through here: the first login spotipy asks
    for by itself, the login command and each refresh. The file is made
    private before the login is written into it, whatever the spotipy version
    does about permissions.
    """

    def save_token_to_cache(self, token_info: dict) -> None:
        """Writes the login to the cache file and leaves the file at mode 600.

        Args:
            token_info: The login as spotipy holds it.
        """
        path = Path(self.cache_path)
        try:
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o600)
            path.write_text(json.dumps(token_info, cls=self.encoder_cls), encoding="utf-8")
        except OSError as error:
            logger.warning("The Spotify login could not be saved to %s: %s", path, error)


class TimedSpotifyOAuth(SpotifyOAuth):
    """Spotify login that stops waiting for the browser after a set time.

    spotipy waits for the approval for ever, which hangs a run nobody is watching.
    An unattended run does not ask the browser at all.

    Args:
        wait_seconds: Seconds to wait for the approval.
        unattended: Whether nobody is there to approve a page, so none may be opened.
        **kwargs: Passed to SpotifyOAuth.
    """

    def __init__(self, wait_seconds: int, unattended: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self._wait_seconds = wait_seconds
        self.unattended = unattended

    def get_auth_response(self, open_browser: bool | None = None) -> str:
        """Asks the browser for the approval, unless nobody is there to give it.

        spotipy comes here whenever no usable login is saved, so this is the
        one place where a first login would open a page or wait for typed input.

        Args:
            open_browser: Whether to open the page, None for the configured choice.

        Returns:
            The authorization code.

        Raises:
            UnattendedLoginError: When the run is unattended.
        """
        if self.unattended:
            raise UnattendedLoginError(PLATFORM)
        return super().get_auth_response(open_browser)

    def _get_auth_response_interactive(self, open_browser: bool = False) -> str:
        """Asks for the redirect address to be pasted, only when someone can type it.

        spotipy falls back to this prompt when the redirect address is not a
        local http address with a port. Without a terminal the prompt would
        wait for ever or crash, so the login is refused with an explanation.

        Args:
            open_browser: Whether to open the page first.

        Returns:
            The authorization code.

        Raises:
            LoginFailedError: When there is no terminal to paste into.
        """
        if not sys.stdin or not sys.stdin.isatty():
            raise LoginFailedError(
                PLATFORM,
                "the redirect address is not a local http address with a port, such as "
                "http://127.0.0.1:8888/callback, so the approval would have to be pasted "
                "into a terminal and there is none",
            )
        return super()._get_auth_response_interactive(open_browser)

    def _get_auth_response_local_server(self, redirect_port: int) -> str:
        """Opens the approval page and waits for Spotify to send the browser back.

        Args:
            redirect_port: Local port the approval is sent to.

        Returns:
            The authorization code.

        Raises:
            LoginTimeoutError: When the page is not approved in time.
            SpotifyOauthError: When Spotify answers with an error.
        """
        server = start_local_http_server(redirect_port)
        deadline = time.monotonic() + self._wait_seconds
        try:
            self._open_auth_url()
            while server.auth_code is None and server.error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LoginTimeoutError(PLATFORM, self._wait_seconds)
                server.timeout = remaining
                server.handle_request()
        finally:
            server.server_close()
        if server.error is not None:
            raise server.error
        received = getattr(server, "state", None)
        if self.state is not None and received != self.state:
            raise SpotifyStateError(self.state, received)
        return server.auth_code


class SpotifyClient:
    """Client for interacting with the Spotify API.

    Args:
        settings: Application settings containing Spotify credentials.
    """

    SCOPES = [
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-public",
        "playlist-modify-private",
        "user-library-read",
    ]

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache_path = settings.data_dir / ".spotify_cache"
        settings.data_dir.mkdir(parents=True, exist_ok=True)

        self._auth = TimedSpotifyOAuth(
            wait_seconds=settings.login_wait_seconds,
            unattended=settings.unattended,
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret,
            redirect_uri=settings.spotify_redirect_uri,
            scope=" ".join(self.SCOPES),
            cache_handler=PrivateCacheFileHandler(cache_path=str(self._cache_path)),
            open_browser=True,
        )
        self._client = spotipy.Spotify(
            auth_manager=self._auth,
            retries=0,
            status_retries=0,
            requests_timeout=20,
        )
        self._last_search = 0.0
        self._search_budget = settings.spotify_search_budget
        self._ledger = SearchLedger(settings.data_dir / SEARCH_LEDGER_FILE)
        self._user_id: str | None = None
        self._playlist_cache = CachedPlaylists()

    @property
    def user_id(self) -> str:
        """Get the authenticated user's ID."""
        if self._user_id is None:
            self._user_id = self._call(self._client.current_user)["id"]
        return self._user_id

    def login(self) -> None:
        """Opens the Spotify approval page in the browser and waits for the approval.

        The saved login is not consulted, so one that stopped working is replaced.
        Asking for a login by name means someone is there, so the page opens
        even when the settings say the run is unattended.

        Raises:
            LoginTimeoutError: When the page is not approved in time.
            LoginFailedError: When Spotify refuses the login.
        """
        self._auth.unattended = False
        try:
            self._auth.get_access_token(as_dict=False, check_cache=False)
        except (SpotifyOauthError, OSError) as error:
            raise LoginFailedError(PLATFORM, str(error)) from error
        finally:
            self._auth.unattended = self._settings.unattended
        if self._cache_path.exists():
            self._cache_path.chmod(0o600)
        self._user_id = None

    def _set_login_aside(self) -> Path | None:
        """Renames the saved login so that it is no longer used but not lost.

        The new name carries the time, and a number when that name is taken,
        so a login set aside earlier is never overwritten.

        Returns:
            Where the saved login went, or None when there was none.
        """
        if not self._cache_path.exists():
            return None
        name = f"{self._cache_path.name}{STALE_SUFFIX}-{datetime.now().strftime(STALE_STAMP)}"
        stale = self._cache_path.with_name(name)
        repeat = 1
        while stale.exists():
            repeat += 1
            stale = self._cache_path.with_name(f"{name}-{repeat}")
        self._cache_path.replace(stale)
        stale.chmod(0o600)
        return stale

    def _replace_stale_login(self, error: SpotifyOauthError) -> None:
        """Sets a saved login that Spotify refuses aside and asks for a new one.

        A login saved for another Spotify app, or one whose approval was
        withdrawn, fails on every refresh until the saved file is gone. An
        unattended run cannot ask for a new one, so it leaves the file where
        it is and stops.

        Args:
            error: The refusal.

        Raises:
            UnattendedLoginError: When the run is unattended.
            LoginTimeoutError: When the page is not approved in time.
            LoginFailedError: When Spotify refuses the new login.
        """
        if self._settings.unattended:
            raise UnattendedLoginError(PLATFORM) from error
        stale = self._set_login_aside()
        logger.warning(
            "Spotify refused the saved login (%s). It was moved to %s and the Spotify "
            "approval page is opening in your browser.",
            error.error,
            stale,
        )
        if stale is not None:
            console.print(
                f"[yellow]Spotify refused the saved login. The old login was moved to "
                f"{escape(str(stale))} and a new one is being asked for.[/yellow]",
                soft_wrap=True,
                highlight=False,
            )
        self.login()

    def _call(self, method, *args, **kwargs):
        """Calls Spotify and replaces a saved login that Spotify no longer accepts.

        Args:
            method: Bound spotipy method.
            *args: Positional arguments.
            **kwargs: Keyword arguments.

        Returns:
            The response.

        Raises:
            RateLimitedError: When Spotify asks for a long wait.
            ReauthRequiredError: When the login cannot be refreshed or replaced.
        """
        try:
            return self._call_with_retries(method, *args, **kwargs)
        except SpotifyOauthError as error:
            if error.error not in STALE_LOGIN_ERRORS:
                raise ReauthRequiredError(PLATFORM) from error
            self._replace_stale_login(error)
        try:
            return self._call_with_retries(method, *args, **kwargs)
        except SpotifyOauthError as error:
            raise ReauthRequiredError(PLATFORM) from error

    def _call_with_retries(self, method, *args, **kwargs):
        """Calls Spotify, waiting out short limits and refusing to wait out long ones.

        spotipy would otherwise honor any Retry-After silently, and Spotify can
        ask for a wait of many hours, which looks like a hang.

        Args:
            method: Bound spotipy method.
            *args: Positional arguments.
            **kwargs: Keyword arguments.

        Returns:
            The response.

        Raises:
            RateLimitedError: When Spotify asks for a long wait.
            SpotifyOauthError: When the saved login is refused.
        """
        for attempt in range(MAX_ATTEMPTS):
            try:
                return method(*args, **kwargs)
            except SpotifyException as error:
                last_attempt = attempt == MAX_ATTEMPTS - 1
                if error.http_status == 429:
                    wait = retry_after_seconds(error.headers)
                    if wait > MAX_WAIT_SECONDS or last_attempt:
                        raise RateLimitedError(PLATFORM, wait) from error
                    time.sleep(wait + 1)
                elif error.http_status >= 500 and not last_attempt:
                    time.sleep(2**attempt)
                else:
                    raise
        raise RuntimeError("unreachable")

    def search_raw(self, query: str, kind: str = "track", limit: int = SEARCH_LIMIT) -> list[dict]:
        """Searches Spotify and returns the raw items.

        Args:
            query: Search query.
            kind: track or album.
            limit: Maximum results, 10 is the most Spotify allows in development mode.

        Returns:
            Raw result items.
        """
        self._pace()
        results = self._call(self._client.search, q=query, type=kind, limit=limit)
        return results[f"{kind}s"]["items"]

    def album_raw(self, album_id: str) -> dict:
        """Fetches an album with its tracks.

        Args:
            album_id: Spotify album id.

        Returns:
            Raw album object.
        """
        self._pace()
        return self._call(self._client.album, album_id)

    def _pace(self) -> None:
        """Spaces out searches and stops the run when the searches of the day are used.

        Raises:
            SearchBudgetExhaustedError: When the last 24 hours already hold all the searches allowed.
        """
        now = time.time()
        if self._ledger.recent(now) >= self._search_budget:
            raise SearchBudgetExhaustedError(PLATFORM, self._search_budget, self._ledger.frees_at())
        self._ledger.record(now)
        pause = SEARCH_PAUSE_SECONDS - (time.monotonic() - self._last_search)
        if pause > 0:
            time.sleep(pause)
        self._last_search = time.monotonic()

    def search_tracks(
        self,
        query: str,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Search for tracks matching a query.

        Args:
            query: Search query string.
            limit: Maximum number of results to return.

        Returns:
            List of SearchResult objects.
        """
        items = self.search_raw(query, limit=limit)
        return [SearchResult(track=self._parse_track(item), is_verified=False) for item in items]

    def invalidate_playlist_cache(self) -> None:
        """Invalidate the playlist cache."""
        self._playlist_cache = CachedPlaylists()

    def get_user_playlists(self, prefix: str | None = None) -> list[SpotifyPlaylist]:
        """Get all playlists owned by the current user.

        Args:
            prefix: Optional prefix to filter playlists by name.

        Returns:
            List of SpotifyPlaylist objects.
        """
        if not self._playlist_cache.is_valid():
            self._refresh_playlist_cache()

        if prefix:
            return [p for p in self._playlist_cache.playlists if p.name.startswith(prefix)]
        return list(self._playlist_cache.playlists)

    def _refresh_playlist_cache(self) -> None:
        """Refresh the playlist cache from API."""
        playlists = []
        offset = 0
        limit = 50

        while True:
            results = self._call(self._client.current_user_playlists, limit=limit, offset=offset)
            if not results["items"]:
                break

            for item in results["items"]:
                if item["owner"]["id"] != self.user_id:
                    continue

                playlists.append(
                    SpotifyPlaylist(
                        id=item["id"],
                        uri=item["uri"],
                        name=item["name"],
                        owner_id=item["owner"]["id"],
                        track_count=(item.get("items") or item.get("tracks") or {}).get("total", 0),
                        public=item["public"],
                    )
                )

            if not results["next"]:
                break
            offset += limit

        self._playlist_cache = CachedPlaylists(playlists=playlists, timestamp=time.time())

    def find_playlist_by_name(self, name: str) -> SpotifyPlaylist | None:
        """Find a playlist by exact name match.

        Args:
            name: Playlist name to search for.

        Returns:
            SpotifyPlaylist if found, None otherwise.
        """
        playlists = self.get_user_playlists()
        for playlist in playlists:
            if playlist.name == name:
                return playlist
        return None

    def create_playlist(
        self,
        name: str,
        description: str = "",
        public: bool = True,
    ) -> SpotifyPlaylist:
        """Create a new playlist.

        Args:
            name: Playlist name.
            description: Playlist description.
            public: Whether the playlist is public.

        Returns:
            The created SpotifyPlaylist.
        """
        result = self._call(
            self._client.current_user_playlist_create,
            name=name,
            public=public,
            description=description,
        )
        self.invalidate_playlist_cache()
        return SpotifyPlaylist(
            id=result["id"],
            uri=result["uri"],
            name=result["name"],
            owner_id=result["owner"]["id"],
            track_count=0,
            public=result["public"],
        )

    def delete_playlist(self, playlist_id: str) -> None:
        """Unfollow (delete) a playlist.

        Args:
            playlist_id: Spotify playlist ID.
        """
        self._call(self._client.current_user_unfollow_playlist, playlist_id)
        self.invalidate_playlist_cache()

    def get_playlist_tracks(self, playlist_id: str) -> list[SpotifyTrack]:
        """Get all tracks in a playlist.

        Args:
            playlist_id: Spotify playlist ID.

        Returns:
            List of SpotifyTrack objects.
        """
        tracks = []
        offset = 0
        limit = 100

        while True:
            results = self._call(self._client.playlist_items, playlist_id, limit=limit, offset=offset)

            for entry in results["items"]:
                item = entry.get("item") or entry.get("track")
                if item is None or item.get("type", "track") != "track" or item.get("id") is None:
                    continue
                tracks.append(self._parse_track(item))

            if not results["next"]:
                break
            offset += limit

        return tracks

    def add_tracks_to_playlist(
        self,
        playlist_id: str,
        track_uris: list[str],
    ) -> None:
        """Add tracks to a playlist.

        Args:
            playlist_id: Spotify playlist ID.
            track_uris: List of Spotify track URIs to add.
        """
        for i in range(0, len(track_uris), 100):
            batch = track_uris[i : i + 100]
            self._call(self._client.playlist_add_items, playlist_id, batch)

    def remove_tracks_from_playlist(
        self,
        playlist_id: str,
        track_uris: list[str],
    ) -> None:
        """Remove tracks from a playlist.

        Args:
            playlist_id: Spotify playlist ID.
            track_uris: List of Spotify track URIs to remove.
        """
        for i in range(0, len(track_uris), 100):
            batch = track_uris[i : i + 100]
            self._call(self._client.playlist_remove_all_occurrences_of_items, playlist_id, batch)

    def _parse_track(self, item: dict) -> SpotifyTrack:
        """Parse a Spotify API track response into a SpotifyTrack.

        Args:
            item: Raw track data from Spotify API.

        Returns:
            Parsed SpotifyTrack object.
        """
        artists = [a["name"] for a in item["artists"]]
        return SpotifyTrack(
            id=item["id"],
            uri=item["uri"],
            name=item["name"],
            artist=artists[0] if artists else "Unknown Artist",
            artists=artists,
            album=item["album"]["name"] if item.get("album") else "",
            popularity=item.get("popularity", 0),
            duration_ms=item.get("duration_ms", 0),
            is_playable=item.get("is_playable", True),
        )
