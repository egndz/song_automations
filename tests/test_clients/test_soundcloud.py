"""Tests for SoundCloud client."""

import base64
import hashlib

import pytest

from song_automations.clients.soundcloud import (
    SoundCloudClient,
    SoundCloudPlaylist,
    SoundCloudTrack,
)


class TestPKCEGeneration:
    """Tests for PKCE code verifier and challenge generation."""

    @pytest.fixture
    def client(self, settings):
        """Create a SoundCloud client for testing."""
        return SoundCloudClient(settings)

    def test_pkce_verifier_length(self, client):
        """Verifier should be between 43-128 characters."""
        verifier, _ = client._generate_pkce()
        assert 43 <= len(verifier) <= 128

    def test_pkce_verifier_is_url_safe(self, client):
        """Verifier should only contain URL-safe base64 characters."""
        verifier, _ = client._generate_pkce()
        allowed_chars = set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        assert all(c in allowed_chars for c in verifier)

    def test_pkce_challenge_is_sha256_of_verifier(self, client):
        """Challenge should be base64url(SHA256(verifier))."""
        verifier, challenge = client._generate_pkce()
        expected_digest = hashlib.sha256(verifier.encode()).digest()
        expected_challenge = (
            base64.urlsafe_b64encode(expected_digest).rstrip(b"=").decode("ascii")
        )
        assert challenge == expected_challenge

    def test_pkce_challenge_no_padding(self, client):
        """Challenge should not have base64 padding characters."""
        _, challenge = client._generate_pkce()
        assert "=" not in challenge

    @pytest.mark.parametrize("_iteration", range(5))
    def test_pkce_generates_unique_values(self, client, _iteration):
        """Each call should generate unique verifier/challenge pairs."""
        pairs = [client._generate_pkce() for _ in range(3)]
        verifiers = [p[0] for p in pairs]
        challenges = [p[1] for p in pairs]
        assert len(set(verifiers)) == 3
        assert len(set(challenges)) == 3


class TestSoundCloudTrack:
    """Tests for SoundCloudTrack dataclass."""

    @pytest.mark.parametrize(
        "track_id,title,artist,playback_count,likes_count",
        [
            (123, "Test Track", "Test Artist", 1000, 50),
            (456, "Another", "Artist", 0, 0),
            (789, "Popular", "Famous DJ", 1000000, 50000),
        ],
    )
    def test_track_properties(
        self, track_id, title, artist, playback_count, likes_count
    ):
        """Track should store properties correctly."""
        track = SoundCloudTrack(
            id=track_id,
            permalink_url="https://soundcloud.com/test",
            title=title,
            artist=artist,
            playback_count=playback_count,
            likes_count=likes_count,
            duration_ms=180000,
            user_id=1,
            is_streamable=True,
        )
        assert track.id == track_id
        assert track.title == title
        assert track.artist == artist
        assert track.playback_count == playback_count
        assert track.likes_count == likes_count

    def test_track_full_title(self):
        """Full title should combine artist and title."""
        track = SoundCloudTrack(
            id=123,
            permalink_url="https://soundcloud.com/test",
            title="Test Track",
            artist="Test Artist",
            playback_count=0,
            likes_count=0,
            duration_ms=180000,
            user_id=1,
            is_streamable=True,
        )
        assert track.full_title == "Test Artist - Test Track"


class TestSoundCloudPlaylist:
    """Tests for SoundCloudPlaylist dataclass."""

    @pytest.mark.parametrize(
        "playlist_id,title,track_count,is_public",
        [
            (100, "My Playlist", 10, True),
            (200, "Empty Playlist", 0, True),
            (300, "Private Playlist", 5, False),
        ],
    )
    def test_playlist_properties(self, playlist_id, title, track_count, is_public):
        """Playlist should store properties correctly."""
        playlist = SoundCloudPlaylist(
            id=playlist_id,
            permalink_url="https://soundcloud.com/user/sets/playlist",
            title=title,
            user_id=1,
            track_count=track_count,
            is_public=is_public,
        )
        assert playlist.id == playlist_id
        assert playlist.title == title
        assert playlist.track_count == track_count
        assert playlist.is_public == is_public


class TestParseTrack:
    """Tests for track parsing from API response."""

    @pytest.fixture
    def client(self, settings):
        """Create a SoundCloud client for testing."""
        return SoundCloudClient(settings)

    @pytest.mark.parametrize(
        "item,expected_id,expected_title,expected_artist",
        [
            (
                {
                    "id": 123,
                    "title": "Track Title",
                    "user": {"username": "Artist Name", "id": 1},
                    "permalink_url": "https://soundcloud.com/artist/track",
                    "playback_count": 1000,
                    "likes_count": 50,
                    "duration": 180000,
                    "streamable": True,
                },
                123,
                "Track Title",
                "Artist Name",
            ),
            (
                {
                    "id": 456,
                    "title": "Another Track",
                    "user": {"username": "Another Artist", "id": 2},
                    "permalink_url": "https://soundcloud.com/another/track",
                    "playback_count": None,
                    "likes_count": None,
                    "duration": 240000,
                    "streamable": True,
                },
                456,
                "Another Track",
                "Another Artist",
            ),
        ],
    )
    def test_parse_track(
        self, client, item, expected_id, expected_title, expected_artist
    ):
        """Parse track should extract correct fields from API response."""
        track = client._parse_track(item)
        assert track.id == expected_id
        assert track.title == expected_title
        assert track.artist == expected_artist

    def test_parse_track_missing_user(self, client):
        """Parse track should handle missing user gracefully."""
        item = {
            "id": 789,
            "title": "Orphan Track",
            "user": None,
            "permalink_url": "https://soundcloud.com/unknown/track",
            "playback_count": 0,
            "likes_count": 0,
            "duration": 180000,
            "streamable": True,
        }
        track = client._parse_track(item)
        assert track.id == 789
        assert track.title == "Orphan Track"
        assert track.artist == "Unknown Artist"

    def test_parse_track_null_counts(self, client):
        """Parse track should handle null playback/likes counts."""
        item = {
            "id": 999,
            "title": "New Track",
            "user": {"username": "New Artist", "id": 1},
            "permalink_url": "https://soundcloud.com/new/track",
            "playback_count": None,
            "likes_count": None,
            "duration": 180000,
            "streamable": True,
        }
        track = client._parse_track(item)
        assert track.playback_count == 0
        assert track.likes_count == 0
