"""Tests for Discogs client."""

import pytest

from song_automations.clients.discogs import (
    DiscogsClient,
    Folder,
    Release,
    Track,
)


class TestTrack:
    """Tests for Track dataclass."""

    @pytest.mark.parametrize(
        "position,title,artist,release_id",
        [
            ("A1", "First Track", "Test Artist", 12345),
            ("B2", "Second Track", "Another Artist", 67890),
            ("1", "Digital Track", "DJ Name", 11111),
        ],
    )
    def test_track_properties(self, position, title, artist, release_id):
        """Track should store properties correctly."""
        track = Track(
            position=position,
            title=title,
            artist=artist,
            duration="4:30",
            release_id=release_id,
            release_title="Test Release",
        )
        assert track.position == position
        assert track.title == title
        assert track.artist == artist
        assert track.release_id == release_id

    def test_track_full_title(self):
        """Full title should combine artist and title."""
        track = Track(
            position="A1",
            title="Test Track",
            artist="Test Artist",
            duration="3:45",
            release_id=12345,
            release_title="Test Release",
        )
        assert track.full_title == "Test Artist - Test Track"

    @pytest.mark.parametrize(
        "duration",
        ["", "3:45", "12:00", "0:30"],
    )
    def test_track_duration_formats(self, duration):
        """Track should accept various duration formats."""
        track = Track(
            position="A1",
            title="Test",
            artist="Artist",
            duration=duration,
            release_id=1,
            release_title="Album",
        )
        assert track.duration == duration


class TestFolder:
    """Tests for Folder dataclass."""

    @pytest.mark.parametrize(
        "folder_id,name,count",
        [
            (1, "Uncategorized", 50),
            (9056466, "Electronic", 100),
            (123, "House", 0),
        ],
    )
    def test_folder_properties(self, folder_id, name, count):
        """Folder should store properties correctly."""
        folder = Folder(
            id=folder_id,
            name=name,
            count=count,
        )
        assert folder.id == folder_id
        assert folder.name == name
        assert folder.count == count


class TestRelease:
    """Tests for Release dataclass."""

    @pytest.mark.parametrize(
        "release_id,title,artist,year",
        [
            (12345, "Test Album", "Test Artist", 2023),
            (67890, "Compilation", "Various Artists", 2020),
            (11111, "Classic EP", "Old School", 1995),
        ],
    )
    def test_release_properties(self, release_id, title, artist, year):
        """Release should store properties correctly."""
        release = Release(
            id=release_id,
            title=title,
            artist=artist,
            year=year,
            folder_id=1,
            folder_name="Uncategorized",
        )
        assert release.id == release_id
        assert release.title == title
        assert release.artist == artist
        assert release.year == year

    def test_release_zero_year(self):
        """Release should handle unknown year as 0."""
        release = Release(
            id=12345,
            title="Unknown Year Album",
            artist="Mystery Artist",
            year=0,
            folder_id=1,
            folder_name="Unknown",
        )
        assert release.year == 0


class TestCleanArtistName:
    """Tests for artist name cleaning logic."""

    @pytest.fixture
    def client(self, settings, monkeypatch):
        """Create a Discogs client for testing."""
        mock_client = type("MockClient", (), {"identity": lambda self: None})()
        monkeypatch.setattr(
            "discogs_client.Client",
            lambda *args, **kwargs: mock_client
        )
        return DiscogsClient(settings)

    @pytest.mark.parametrize(
        "input_name,expected",
        [
            ("Artist Name", "Artist Name"),
            ("Artist (2)", "Artist"),
            ("DJ Name (3)", "DJ Name"),
            ("Band (10)", "Band"),
            ("Artist (text)", "Artist (text)"),
            ("  Spaced Artist  ", "Spaced Artist"),
        ],
    )
    def test_clean_artist_name(self, client, input_name, expected):
        """Clean artist name should remove numeric disambiguation suffixes."""
        result = client._clean_artist_name(input_name)
        assert result == expected

    def test_clean_artist_name_preserves_non_numeric_suffix(self, client):
        """Non-numeric suffixes should be preserved."""
        result = client._clean_artist_name("Artist (Remix)")
        assert result == "Artist (Remix)"

    def test_clean_artist_name_empty(self, client):
        """Empty string should remain empty after stripping."""
        result = client._clean_artist_name("")
        assert result == ""


class TestExtractArtists:
    """Tests for artist extraction from Discogs artist list."""

    @pytest.fixture
    def client(self, settings, monkeypatch):
        """Create a Discogs client for testing."""
        mock_client = type("MockClient", (), {"identity": lambda self: None})()
        monkeypatch.setattr(
            "discogs_client.Client",
            lambda *args, **kwargs: mock_client
        )
        return DiscogsClient(settings)

    def test_extract_single_artist(self, client):
        """Single artist should return just the name."""
        artists = [type("Artist", (), {"name": "Test Artist"})()]
        result = client._extract_artists(artists)
        assert result == "Test Artist"

    def test_extract_two_artists(self, client):
        """Two artists should be joined with ampersand."""
        artists = [
            type("Artist", (), {"name": "Artist A"})(),
            type("Artist", (), {"name": "Artist B"})(),
        ]
        result = client._extract_artists(artists)
        assert result == "Artist A & Artist B"

    def test_extract_multiple_artists(self, client):
        """Multiple artists should use comma and ampersand."""
        artists = [
            type("Artist", (), {"name": "Artist A"})(),
            type("Artist", (), {"name": "Artist B"})(),
            type("Artist", (), {"name": "Artist C"})(),
        ]
        result = client._extract_artists(artists)
        assert result == "Artist A, Artist B & Artist C"

    def test_extract_empty_list(self, client):
        """Empty list should return Unknown Artist."""
        result = client._extract_artists([])
        assert result == "Unknown Artist"

    def test_extract_artist_with_numeric_suffix(self, client):
        """Numeric disambiguation suffix should be removed."""
        artists = [type("Artist", (), {"name": "Common Name (2)"})()]
        result = client._extract_artists(artists)
        assert result == "Common Name"

    def test_extract_four_artists(self, client):
        """Four artists should use Oxford comma style."""
        artists = [
            type("Artist", (), {"name": "A"})(),
            type("Artist", (), {"name": "B"})(),
            type("Artist", (), {"name": "C"})(),
            type("Artist", (), {"name": "D"})(),
        ]
        result = client._extract_artists(artists)
        assert result == "A, B, C & D"


class TestWantlistConstants:
    """Tests for wantlist folder constants."""

    def test_wantlist_folder_id(self):
        """Wantlist folder ID should be -1."""
        assert DiscogsClient.WANTLIST_FOLDER_ID == -1

    def test_wantlist_folder_name(self):
        """Wantlist folder name should be 'Wantlist'."""
        assert DiscogsClient.WANTLIST_FOLDER_NAME == "Wantlist"
