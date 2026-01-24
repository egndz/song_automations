"""SoundCloud API client for playlist management and track search."""

import hashlib
import json
import secrets
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from song_automations.config import Settings


@dataclass
class SoundCloudTrack:
    """Represents a SoundCloud track.

    Args:
        id: SoundCloud track ID.
        permalink_url: URL to the track page.
        title: Track title.
        artist: User/artist name.
        playback_count: Number of plays.
        likes_count: Number of likes.
        duration_ms: Track duration in milliseconds.
        user_id: Uploader's user ID.
        is_streamable: Whether the track can be streamed.
    """

    id: int
    permalink_url: str
    title: str
    artist: str
    playback_count: int
    likes_count: int
    duration_ms: int
    user_id: int
    is_streamable: bool

    @property
    def full_title(self) -> str:
        """Full title for display."""
        return f"{self.artist} - {self.title}"


@dataclass
class SoundCloudPlaylist:
    """Represents a SoundCloud playlist.

    Args:
        id: SoundCloud playlist ID.
        permalink_url: URL to the playlist page.
        title: Playlist title.
        user_id: Owner's user ID.
        track_count: Number of tracks.
        is_public: Whether the playlist is public.
    """

    id: int
    permalink_url: str
    title: str
    user_id: int
    track_count: int
    is_public: bool


@dataclass
class SearchResult:
    """Represents a search result with scoring metadata.

    Args:
        track: The SoundCloud track.
        is_verified: Whether the user is verified or pro.
    """

    track: SoundCloudTrack
    is_verified: bool


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for OAuth callback."""

    authorization_code: str | None = None

    def do_GET(self) -> None:
        """Handle GET request with authorization code."""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "code" in params:
            OAuthCallbackHandler.authorization_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Authorization successful!</h1>"
                b"<p>You can close this window.</p></body></html>"
            )
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format: str, *args) -> None:
        """Suppress logging."""
        pass


class SoundCloudClient:
    """Client for interacting with the SoundCloud API.

    Args:
        settings: Application settings containing SoundCloud credentials.
    """

    API_BASE = "https://api.soundcloud.com"
    AUTH_URL = "https://secure.soundcloud.com/authorize"
    TOKEN_URL = "https://secure.soundcloud.com/oauth/token"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token_path = settings.data_dir / ".soundcloud_token.json"
        settings.data_dir.mkdir(parents=True, exist_ok=True)

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._user_id: int | None = None

        self._load_tokens()

    def _load_tokens(self) -> None:
        """Load tokens from cache file."""
        if self._token_path.exists():
            try:
                data = json.loads(self._token_path.read_text())
                self._access_token = data.get("access_token")
                self._refresh_token = data.get("refresh_token")
            except (json.JSONDecodeError, KeyError):
                pass

    def _save_tokens(self) -> None:
        """Save tokens to cache file."""
        data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
        }
        self._token_path.write_text(json.dumps(data))

    def _generate_pkce(self) -> tuple[str, str]:
        """Generate PKCE code verifier and challenge.

        Returns:
            Tuple of (code_verifier, code_challenge).
        """
        code_verifier = secrets.token_urlsafe(64)[:128]
        code_challenge = hashlib.sha256(code_verifier.encode()).digest()
        code_challenge = secrets.token_urlsafe(32)
        return code_verifier, code_challenge

    def authenticate(self) -> None:
        """Perform OAuth 2.1 authentication flow."""
        if self._access_token:
            return

        code_verifier, code_challenge = self._generate_pkce()

        parsed_redirect = urlparse(self._settings.soundcloud_redirect_uri)
        port = parsed_redirect.port or 8889

        params = {
            "client_id": self._settings.soundcloud_client_id,
            "redirect_uri": self._settings.soundcloud_redirect_uri,
            "response_type": "code",
            "scope": "non-expiring",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        auth_url = f"{self.AUTH_URL}?{urlencode(params)}"

        server = HTTPServer(("localhost", port), OAuthCallbackHandler)
        server_thread = Thread(target=server.handle_request)
        server_thread.start()

        webbrowser.open(auth_url)
        server_thread.join(timeout=120)

        if not OAuthCallbackHandler.authorization_code:
            raise RuntimeError("Failed to receive authorization code")

        token_data = {
            "client_id": self._settings.soundcloud_client_id,
            "client_secret": self._settings.soundcloud_client_secret,
            "grant_type": "authorization_code",
            "code": OAuthCallbackHandler.authorization_code,
            "redirect_uri": self._settings.soundcloud_redirect_uri,
            "code_verifier": code_verifier,
        }

        with httpx.Client() as client:
            response = client.post(self.TOKEN_URL, data=token_data)
            response.raise_for_status()
            tokens = response.json()

        self._access_token = tokens["access_token"]
        self._refresh_token = tokens.get("refresh_token")
        self._save_tokens()

        OAuthCallbackHandler.authorization_code = None

    def _get_headers(self) -> dict[str, str]:
        """Get authorization headers for API requests."""
        if not self._access_token:
            self.authenticate()
        return {
            "Authorization": f"OAuth {self._access_token}",
            "Accept": "application/json",
        }

    @property
    def user_id(self) -> int:
        """Get the authenticated user's ID."""
        if self._user_id is None:
            with httpx.Client() as client:
                response = client.get(
                    f"{self.API_BASE}/me",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                self._user_id = response.json()["id"]
        return self._user_id

    def search_tracks(
        self,
        query: str,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Search for tracks matching a query.

        Args:
            query: Search query string.
            limit: Maximum number of results.

        Returns:
            List of SearchResult objects.
        """
        with httpx.Client() as client:
            response = client.get(
                f"{self.API_BASE}/tracks",
                headers=self._get_headers(),
                params={"q": query, "limit": limit},
            )
            response.raise_for_status()
            items = response.json()

        results = []
        for item in items:
            track = self._parse_track(item)
            is_verified = item.get("user", {}).get("verified", False)
            results.append(SearchResult(track=track, is_verified=is_verified))

        return results

    def get_user_playlists(self, prefix: str | None = None) -> list[SoundCloudPlaylist]:
        """Get all playlists owned by the current user.

        Args:
            prefix: Optional prefix to filter playlists by title.

        Returns:
            List of SoundCloudPlaylist objects.
        """
        playlists = []

        with httpx.Client() as client:
            response = client.get(
                f"{self.API_BASE}/me/playlists",
                headers=self._get_headers(),
            )
            response.raise_for_status()
            items = response.json()

        for item in items:
            if prefix and not item["title"].startswith(prefix):
                continue

            playlists.append(
                SoundCloudPlaylist(
                    id=item["id"],
                    permalink_url=item["permalink_url"],
                    title=item["title"],
                    user_id=item["user"]["id"],
                    track_count=item["track_count"],
                    is_public=item.get("sharing", "public") == "public",
                )
            )

        return playlists

    def find_playlist_by_title(self, title: str) -> SoundCloudPlaylist | None:
        """Find a playlist by exact title match.

        Args:
            title: Playlist title to search for.

        Returns:
            SoundCloudPlaylist if found, None otherwise.
        """
        playlists = self.get_user_playlists()
        for playlist in playlists:
            if playlist.title == title:
                return playlist
        return None

    def create_playlist(
        self,
        title: str,
        description: str = "",
        public: bool = True,
    ) -> SoundCloudPlaylist:
        """Create a new playlist.

        Args:
            title: Playlist title.
            description: Playlist description.
            public: Whether the playlist is public.

        Returns:
            The created SoundCloudPlaylist.
        """
        data = {
            "playlist": {
                "title": title,
                "description": description,
                "sharing": "public" if public else "private",
                "tracks": [],
            }
        }

        with httpx.Client() as client:
            response = client.post(
                f"{self.API_BASE}/playlists",
                headers=self._get_headers(),
                json=data,
            )
            response.raise_for_status()
            result = response.json()

        return SoundCloudPlaylist(
            id=result["id"],
            permalink_url=result["permalink_url"],
            title=result["title"],
            user_id=result["user"]["id"],
            track_count=0,
            is_public=public,
        )

    def delete_playlist(self, playlist_id: int) -> None:
        """Delete a playlist.

        Args:
            playlist_id: SoundCloud playlist ID.
        """
        with httpx.Client() as client:
            response = client.delete(
                f"{self.API_BASE}/playlists/{playlist_id}",
                headers=self._get_headers(),
            )
            response.raise_for_status()

    def get_playlist_tracks(self, playlist_id: int) -> list[SoundCloudTrack]:
        """Get all tracks in a playlist.

        Args:
            playlist_id: SoundCloud playlist ID.

        Returns:
            List of SoundCloudTrack objects.
        """
        with httpx.Client() as client:
            response = client.get(
                f"{self.API_BASE}/playlists/{playlist_id}",
                headers=self._get_headers(),
            )
            response.raise_for_status()
            data = response.json()

        tracks = []
        for item in data.get("tracks", []):
            track = self._parse_track(item)
            tracks.append(track)

        return tracks

    def set_playlist_tracks(
        self,
        playlist_id: int,
        track_ids: list[int],
    ) -> None:
        """Set the tracks in a playlist (replaces all existing tracks).

        Args:
            playlist_id: SoundCloud playlist ID.
            track_ids: List of track IDs to set.
        """
        data = {
            "playlist": {
                "tracks": [{"id": tid} for tid in track_ids],
            }
        }

        with httpx.Client() as client:
            response = client.put(
                f"{self.API_BASE}/playlists/{playlist_id}",
                headers=self._get_headers(),
                json=data,
            )
            response.raise_for_status()

    def add_tracks_to_playlist(
        self,
        playlist_id: int,
        track_ids: list[int],
    ) -> None:
        """Add tracks to a playlist.

        Args:
            playlist_id: SoundCloud playlist ID.
            track_ids: List of track IDs to add.
        """
        current_tracks = self.get_playlist_tracks(playlist_id)
        current_ids = [t.id for t in current_tracks]
        all_ids = current_ids + [tid for tid in track_ids if tid not in current_ids]
        self.set_playlist_tracks(playlist_id, all_ids)

    def remove_tracks_from_playlist(
        self,
        playlist_id: int,
        track_ids: list[int],
    ) -> None:
        """Remove tracks from a playlist.

        Args:
            playlist_id: SoundCloud playlist ID.
            track_ids: List of track IDs to remove.
        """
        current_tracks = self.get_playlist_tracks(playlist_id)
        remaining_ids = [t.id for t in current_tracks if t.id not in track_ids]
        self.set_playlist_tracks(playlist_id, remaining_ids)

    def _parse_track(self, item: dict) -> SoundCloudTrack:
        """Parse a SoundCloud API track response.

        Args:
            item: Raw track data from SoundCloud API.

        Returns:
            Parsed SoundCloudTrack object.
        """
        user = item.get("user", {})
        return SoundCloudTrack(
            id=item["id"],
            permalink_url=item.get("permalink_url", ""),
            title=item.get("title", ""),
            artist=user.get("username", "Unknown Artist"),
            playback_count=item.get("playback_count", 0) or 0,
            likes_count=item.get("likes_count", 0) or 0,
            duration_ms=item.get("duration", 0),
            user_id=user.get("id", 0),
            is_streamable=item.get("streamable", True),
        )
