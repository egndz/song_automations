"""Spotify API client for playlist management and track search."""

import time
from dataclasses import dataclass, field

import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError

from song_automations.config import Settings
from song_automations.errors import (
    RateLimitedError,
    ReauthRequiredError,
    SearchBudgetExhaustedError,
)

PLAYLIST_CACHE_TTL = 300
MAX_WAIT_SECONDS = 60
MAX_ATTEMPTS = 5
SEARCH_PAUSE_SECONDS = 0.5
SEARCH_LIMIT = 10


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
        cache_path = settings.data_dir / ".spotify_cache"
        settings.data_dir.mkdir(parents=True, exist_ok=True)

        auth_manager = SpotifyOAuth(
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret,
            redirect_uri=settings.spotify_redirect_uri,
            scope=" ".join(self.SCOPES),
            cache_path=str(cache_path),
            open_browser=True,
        )
        self._client = spotipy.Spotify(
            auth_manager=auth_manager,
            retries=0,
            status_retries=0,
            requests_timeout=20,
        )
        self._last_search = 0.0
        self._search_budget = settings.spotify_search_budget
        self._searches = 0
        self._user_id: str | None = None
        self._playlist_cache = CachedPlaylists()

    @property
    def user_id(self) -> str:
        """Get the authenticated user's ID."""
        if self._user_id is None:
            self._user_id = self._call(self._client.current_user)["id"]
        return self._user_id

    def _call(self, method, *args, **kwargs):
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
            ReauthRequiredError: When the login can no longer be refreshed.
        """
        for attempt in range(MAX_ATTEMPTS):
            try:
                return method(*args, **kwargs)
            except SpotifyOauthError as error:
                raise ReauthRequiredError("Spotify") from error
            except SpotifyException as error:
                last_attempt = attempt == MAX_ATTEMPTS - 1
                if error.http_status == 429:
                    wait = int((error.headers or {}).get("Retry-After", 5))
                    if wait > MAX_WAIT_SECONDS or last_attempt:
                        raise RateLimitedError("Spotify", wait) from error
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
        """Spaces out searches and stops the run when its search budget is used.

        Raises:
            SearchBudgetExhaustedError: When the run has made all the searches it may make.
        """
        if self._searches >= self._search_budget:
            raise SearchBudgetExhaustedError("Spotify", self._search_budget)
        self._searches += 1
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
