"""Tests for Spotify client."""

import pytest

from song_automations.clients.spotify import (
    SpotifyPlaylist,
    SpotifyTrack,
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
