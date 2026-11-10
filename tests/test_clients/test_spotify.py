"""Tests for Spotify client."""

from unittest.mock import MagicMock

import pytest
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOauthError

from song_automations.clients.spotify import (
    DEFAULT_RETRY_AFTER_SECONDS,
    SpotifyClient,
    SpotifyPlaylist,
    SpotifyTrack,
    retry_after_seconds,
)
from song_automations.errors import (
    RateLimitedError,
    ReauthRequiredError,
    SearchBudgetExhaustedError,
)


class TestSpotifyTrack:
    """Tests for SpotifyTrack dataclass."""

    @pytest.mark.parametrize(
        "track_id,name,artist,popularity,duration_ms",
        [
            ("abc123", "Test Track", "Test Artist", 75, 180000),
            ("def456", "Another", "Artist", 0, 240000),
            ("ghi789", "Popular", "Famous DJ", 100, 360000),
        ],
    )
    def test_track_properties(
        self, track_id, name, artist, popularity, duration_ms
    ):
        """Track should store properties correctly."""
        track = SpotifyTrack(
            id=track_id,
            uri=f"spotify:track:{track_id}",
            name=name,
            artist=artist,
            artists=[artist],
            album="Test Album",
            popularity=popularity,
            duration_ms=duration_ms,
            is_playable=True,
        )
        assert track.id == track_id
        assert track.name == name
        assert track.artist == artist
        assert track.popularity == popularity
        assert track.duration_ms == duration_ms

    def test_track_full_title(self):
        """Full title should combine artist and name."""
        track = SpotifyTrack(
            id="abc123",
            uri="spotify:track:abc123",
            name="Test Track",
            artist="Test Artist",
            artists=["Test Artist"],
            album="Test Album",
            popularity=50,
            duration_ms=180000,
            is_playable=True,
        )
        assert track.full_title == "Test Artist - Test Track"

    def test_track_multiple_artists(self):
        """Track should store multiple artists correctly."""
        track = SpotifyTrack(
            id="abc123",
            uri="spotify:track:abc123",
            name="Collaboration",
            artist="Artist A",
            artists=["Artist A", "Artist B", "Artist C"],
            album="Collab Album",
            popularity=80,
            duration_ms=200000,
            is_playable=True,
        )
        assert track.artist == "Artist A"
        assert len(track.artists) == 3
        assert track.artists == ["Artist A", "Artist B", "Artist C"]

    @pytest.mark.parametrize(
        "is_playable",
        [True, False],
    )
    def test_track_playability(self, is_playable):
        """Track should store playability correctly."""
        track = SpotifyTrack(
            id="abc123",
            uri="spotify:track:abc123",
            name="Test",
            artist="Artist",
            artists=["Artist"],
            album="Album",
            popularity=50,
            duration_ms=180000,
            is_playable=is_playable,
        )
        assert track.is_playable == is_playable


class TestSpotifyPlaylist:
    """Tests for SpotifyPlaylist dataclass."""

    @pytest.mark.parametrize(
        "playlist_id,name,track_count,public",
        [
            ("playlist1", "My Playlist", 10, True),
            ("playlist2", "Empty Playlist", 0, True),
            ("playlist3", "Private Playlist", 5, False),
        ],
    )
    def test_playlist_properties(self, playlist_id, name, track_count, public):
        """Playlist should store properties correctly."""
        playlist = SpotifyPlaylist(
            id=playlist_id,
            uri=f"spotify:playlist:{playlist_id}",
            name=name,
            owner_id="user123",
            track_count=track_count,
            public=public,
        )
        assert playlist.id == playlist_id
        assert playlist.name == name
        assert playlist.track_count == track_count
        assert playlist.public == public

    def test_playlist_uri_format(self):
        """Playlist URI should follow Spotify format."""
        playlist = SpotifyPlaylist(
            id="37i9dQZF1DXcBWIGoYBM5M",
            uri="spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
            name="Today's Top Hits",
            owner_id="spotify",
            track_count=50,
            public=True,
        )
        assert playlist.uri.startswith("spotify:playlist:")
        assert playlist.id in playlist.uri


class TestParseTrack:
    """Tests for track parsing from API response."""

    @pytest.fixture
    def mock_spotify_client(self, settings, monkeypatch):
        """Create a mocked SpotifyClient for testing."""
        monkeypatch.setattr("spotipy.Spotify", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "spotipy.oauth2.SpotifyOAuth",
            lambda *args, **kwargs: None
        )

        from song_automations.clients.spotify import SpotifyClient
        return SpotifyClient(settings)

    @pytest.mark.parametrize(
        "item,expected_id,expected_name,expected_artist",
        [
            (
                {
                    "id": "abc123",
                    "uri": "spotify:track:abc123",
                    "name": "Track Title",
                    "artists": [{"name": "Artist Name"}],
                    "album": {"name": "Album Name"},
                    "popularity": 75,
                    "duration_ms": 180000,
                    "is_playable": True,
                },
                "abc123",
                "Track Title",
                "Artist Name",
            ),
            (
                {
                    "id": "def456",
                    "uri": "spotify:track:def456",
                    "name": "Another Track",
                    "artists": [{"name": "Another Artist"}, {"name": "Featured Artist"}],
                    "album": {"name": "Another Album"},
                    "popularity": 50,
                    "duration_ms": 240000,
                    "is_playable": True,
                },
                "def456",
                "Another Track",
                "Another Artist",
            ),
        ],
    )
    def test_parse_track(
        self, mock_spotify_client, item, expected_id, expected_name, expected_artist
    ):
        """Parse track should extract correct fields from API response."""
        track = mock_spotify_client._parse_track(item)
        assert track.id == expected_id
        assert track.name == expected_name
        assert track.artist == expected_artist

    def test_parse_track_missing_album(self, mock_spotify_client):
        """Parse track should handle missing album gracefully."""
        item = {
            "id": "abc123",
            "uri": "spotify:track:abc123",
            "name": "Orphan Track",
            "artists": [{"name": "Some Artist"}],
            "popularity": 0,
            "duration_ms": 180000,
            "is_playable": True,
        }
        track = mock_spotify_client._parse_track(item)
        assert track.id == "abc123"
        assert track.name == "Orphan Track"
        assert track.album == ""

    def test_parse_track_empty_artists_list(self, mock_spotify_client):
        """Parse track should handle empty artists list."""
        item = {
            "id": "abc123",
            "uri": "spotify:track:abc123",
            "name": "Mystery Track",
            "artists": [],
            "album": {"name": "Mystery Album"},
            "popularity": 0,
            "duration_ms": 180000,
            "is_playable": True,
        }
        track = mock_spotify_client._parse_track(item)
        assert track.artist == "Unknown Artist"
        assert track.artists == []

    def test_parse_track_missing_optional_fields(self, mock_spotify_client):
        """Parse track should handle missing optional fields."""
        item = {
            "id": "abc123",
            "uri": "spotify:track:abc123",
            "name": "Minimal Track",
            "artists": [{"name": "Artist"}],
            "album": {"name": "Album"},
        }
        track = mock_spotify_client._parse_track(item)
        assert track.popularity == 0
        assert track.duration_ms == 0
        assert track.is_playable is True

    def test_parse_track_multiple_artists_preserves_all(self, mock_spotify_client):
        """Parse track should preserve all artists in the list."""
        item = {
            "id": "abc123",
            "uri": "spotify:track:abc123",
            "name": "Collab Track",
            "artists": [
                {"name": "Artist A"},
                {"name": "Artist B"},
                {"name": "Artist C"},
            ],
            "album": {"name": "Collab Album"},
            "popularity": 80,
            "duration_ms": 200000,
            "is_playable": True,
        }
        track = mock_spotify_client._parse_track(item)
        assert track.artist == "Artist A"
        assert track.artists == ["Artist A", "Artist B", "Artist C"]
        assert len(track.artists) == 3


class TestSpotifyRateLimitsAndNewApi:
    """Long limits fail clearly and the 2026 playlist shape is understood."""

    @staticmethod
    def _client(monkeypatch):
        """Builds a client with a mocked spotipy.

        Args:
            monkeypatch: Pytest monkeypatch.

        Returns:
            The client.
        """
        monkeypatch.setattr("song_automations.clients.spotify.time.sleep", lambda _: None)
        client = SpotifyClient.__new__(SpotifyClient)
        client._client = MagicMock()
        client._last_search = 0.0
        client._searches = 0
        client._search_budget = 1000
        return client

    @pytest.mark.parametrize("budget", [1, 3])
    def test_search_budget_stops_the_run(self, monkeypatch, budget):
        """Searches beyond the budget raise before any request is sent."""
        client = self._client(monkeypatch)
        client._search_budget = budget
        client._client.search.return_value = {"tracks": {"items": []}}
        for _ in range(budget):
            client.search_raw("q")
        with pytest.raises(SearchBudgetExhaustedError):
            client.album_raw("a")
        assert client._client.search.call_count == budget
        assert client._client.album.call_count == 0

    @pytest.mark.parametrize("wait", [61, 79320])
    def test_long_limit_raises_at_once(self, monkeypatch, wait):
        """A wait of hours becomes an error, never a silent hang."""
        client = self._client(monkeypatch)
        client._client.search.side_effect = SpotifyException(429, -1, "limited", headers={"Retry-After": str(wait)})
        with pytest.raises(RateLimitedError):
            client.search_raw("q")
        assert client._client.search.call_count == 1

    def test_short_limit_is_waited_out(self, monkeypatch):
        """A short limit retries on its own."""
        client = self._client(monkeypatch)
        client._client.search.side_effect = [
            SpotifyException(429, -1, "limited", headers={"Retry-After": "2"}),
            {"tracks": {"items": [{"id": "1"}]}},
        ]
        assert client.search_raw("q") == [{"id": "1"}]

    @pytest.mark.parametrize("field", ["item", "track"])
    def test_playlist_reads_new_and_old_field_names(self, monkeypatch, field):
        """Both the current and the previous response shape are read."""
        client = self._client(monkeypatch)
        raw = {"id": "1", "uri": "u", "name": "n", "artists": [{"name": "a"}]}
        client._client.playlist_items.return_value = {"items": [{field: raw}, {field: None}], "next": None}
        assert [t.id for t in client.get_playlist_tracks("p")] == ["1"]

    def test_expired_login_is_named(self, monkeypatch):
        """An expired refresh token asks for the browser, plainly."""
        client = self._client(monkeypatch)
        client._client.search.side_effect = SpotifyOauthError("invalid_grant")
        with pytest.raises(ReauthRequiredError):
            client.search_raw("q")


class TestRetryAfterHeader:
    """The wait Spotify asks for is read without ever raising."""

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"Retry-After": "7"}, 7),
            ({"Retry-After": 12}, 12),
            ({"Retry-After": "2.6"}, 2),
            ({"Retry-After": " 30 "}, 30),
            ({"Retry-After": "-4"}, 0),
            ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": ""}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": None}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": "inf"}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": "nan"}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": ["3"]}, DEFAULT_RETRY_AFTER_SECONDS),
            ({}, DEFAULT_RETRY_AFTER_SECONDS),
            (None, DEFAULT_RETRY_AFTER_SECONDS),
            ("not a mapping", DEFAULT_RETRY_AFTER_SECONDS),
        ],
    )
    def test_header_is_read_or_defaulted(self, headers, expected):
        """A missing, empty or malformed header falls back to the default wait."""
        assert retry_after_seconds(headers) == expected

    @pytest.mark.parametrize("headers", [None, {}, {"Retry-After": "soon"}, {"Retry-After": None}])
    def test_limit_without_usable_header_is_waited_out(self, monkeypatch, headers):
        """A limit with a broken header retries after the default wait instead of crashing."""
        slept = []
        monkeypatch.setattr("song_automations.clients.spotify.time.sleep", slept.append)
        client = SpotifyClient.__new__(SpotifyClient)
        client._client = MagicMock()
        client._last_search = 0.0
        client._searches = 0
        client._search_budget = 1000
        client._client.search.side_effect = [
            SpotifyException(429, -1, "limited", headers=headers),
            {"tracks": {"items": [{"id": "1"}]}},
        ]
        assert client.search_raw("q") == [{"id": "1"}]
        assert DEFAULT_RETRY_AFTER_SECONDS + 1 in slept
