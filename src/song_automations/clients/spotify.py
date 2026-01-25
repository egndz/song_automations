"""Spotify API client for playlist management and track search."""

import time
from dataclasses import dataclass, field

import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

from song_automations.config import Settings

PLAYLIST_CACHE_TTL = 300  # 5 minutes


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
        self._client = spotipy.Spotify(auth_manager=auth_manager)
        self._user_id: str | None = None
        self._playlist_cache = CachedPlaylists()

    @property
    def user_id(self) -> str:
        """Get the authenticated user's ID."""
        if self._user_id is None:
            self._user_id = self._client.current_user()["id"]
        return self._user_id

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
        results = self._client.search(q=query, type="track", limit=limit)
        items = results["tracks"]["items"]

        artist_ids = list({
            item["artists"][0]["id"]
            for item in items
            if item["artists"]
        })

        verified_artists: set[str] = set()
        if artist_ids:
            try:
                artists_data = self._client.artists(artist_ids)
                for artist in artists_data.get("artists", []):
                    if artist and artist.get("followers", {}).get("total", 0) > 10000:
                        verified_artists.add(artist["id"])
            except SpotifyException:
                pass

        search_results = []
        for item in items:
            track = self._parse_track(item)
            is_verified = False
            if item["artists"]:
                is_verified = item["artists"][0]["id"] in verified_artists
            search_results.append(SearchResult(track=track, is_verified=is_verified))

        return search_results

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
            results = self._client.current_user_playlists(limit=limit, offset=offset)
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
                        track_count=item["tracks"]["total"],
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
        result = self._client.user_playlist_create(
            user=self.user_id,
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
        self._client.current_user_unfollow_playlist(playlist_id)
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
            results = self._client.playlist_tracks(
                playlist_id,
                limit=limit,
                offset=offset,
                fields="items(track(id,uri,name,artists,album,popularity,duration_ms,is_playable)),next",
            )

            for item in results["items"]:
                if item["track"] is None:
                    continue
                track = self._parse_track(item["track"])
                tracks.append(track)

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
            self._client.playlist_add_items(playlist_id, batch)

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
            self._client.playlist_remove_all_occurrences_of_items(playlist_id, batch)

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
