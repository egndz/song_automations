"""Tests for sync engine."""

from unittest.mock import MagicMock

import pytest

from song_automations.clients.discogs import Release
from song_automations.matching.fuzzy import parse_track_title
from song_automations.sync.engine import (
    OperationType,
    SyncEngine,
    SyncOperation,
    SyncResult,
)


class TestOperationType:
    """Tests for OperationType enum."""

    @pytest.mark.parametrize(
        "operation_type,expected_value",
        [
            (OperationType.CREATE_PLAYLIST, "create_playlist"),
            (OperationType.DELETE_PLAYLIST, "delete_playlist"),
            (OperationType.ADD_TRACK, "add_track"),
            (OperationType.REMOVE_TRACK, "remove_track"),
        ],
    )
    def test_operation_type_values(self, operation_type, expected_value):
        """OperationType should have correct string values."""
        assert operation_type.value == expected_value


class TestSyncOperation:
    """Tests for SyncOperation dataclass."""

    @pytest.mark.parametrize(
        "operation_type,folder_name,playlist_name",
        [
            (OperationType.CREATE_PLAYLIST, "Electronic", "Discogs - Electronic"),
            (OperationType.DELETE_PLAYLIST, "Old Folder", "Discogs - Old Folder"),
            (OperationType.ADD_TRACK, "House", "Discogs - House"),
            (OperationType.REMOVE_TRACK, "Techno", "Discogs - Techno"),
        ],
    )
    def test_sync_operation_properties(
        self, operation_type, folder_name, playlist_name
    ):
        """SyncOperation should store properties correctly."""
        operation = SyncOperation(
            operation_type=operation_type,
            folder_name=folder_name,
            playlist_name=playlist_name,
        )
        assert operation.operation_type == operation_type
        assert operation.folder_name == folder_name
        assert operation.playlist_name == playlist_name

    def test_sync_operation_default_values(self):
        """SyncOperation should have correct defaults."""
        operation = SyncOperation(
            operation_type=OperationType.CREATE_PLAYLIST,
            folder_name="Test",
            playlist_name="Discogs - Test",
        )
        assert operation.track_title == ""
        assert operation.track_artist == ""
        assert operation.confidence == 0.0
        assert operation.flagged is False

    def test_sync_operation_track_details(self):
        """SyncOperation should store track details for ADD_TRACK."""
        operation = SyncOperation(
            operation_type=OperationType.ADD_TRACK,
            folder_name="Electronic",
            playlist_name="Discogs - Electronic",
            track_title="Acperience 1",
            track_artist="Hardfloor",
            confidence=0.92,
            flagged=False,
        )
        assert operation.track_title == "Acperience 1"
        assert operation.track_artist == "Hardfloor"
        assert operation.confidence == 0.92
        assert operation.flagged is False

    @pytest.mark.parametrize(
        "confidence,flagged",
        [
            (0.95, False),
            (0.45, True),
            (0.30, True),
            (0.50, False),
        ],
    )
    def test_sync_operation_flagged_tracks(self, confidence, flagged):
        """SyncOperation should correctly flag low-confidence matches."""
        operation = SyncOperation(
            operation_type=OperationType.ADD_TRACK,
            folder_name="Test",
            playlist_name="Discogs - Test",
            track_title="Track",
            track_artist="Artist",
            confidence=confidence,
            flagged=flagged,
        )
        assert operation.flagged == flagged


class TestSyncResult:
    """Tests for SyncResult dataclass."""

    def test_sync_result_default_values(self):
        """SyncResult should have correct defaults."""
        result = SyncResult()
        assert result.operations == []
        assert result.playlists_created == 0
        assert result.playlists_deleted == 0
        assert result.tracks_added == 0
        assert result.tracks_removed == 0
        assert result.tracks_missing == 0
        assert result.tracks_flagged == 0

    def test_sync_result_aggregation(self):
        """SyncResult should aggregate operation counts."""
        result = SyncResult(
            playlists_created=2,
            tracks_added=50,
            tracks_removed=5,
            tracks_missing=10,
            tracks_flagged=8,
        )
        assert result.playlists_created == 2
        assert result.tracks_added == 50
        assert result.tracks_removed == 5
        assert result.tracks_missing == 10
        assert result.tracks_flagged == 8

    def test_sync_result_operations_list(self):
        """SyncResult should store operations list."""
        operations = [
            SyncOperation(
                operation_type=OperationType.CREATE_PLAYLIST,
                folder_name="Test",
                playlist_name="Discogs - Test",
            ),
            SyncOperation(
                operation_type=OperationType.ADD_TRACK,
                folder_name="Test",
                playlist_name="Discogs - Test",
                track_title="Track",
                track_artist="Artist",
            ),
        ]
        result = SyncResult(operations=operations)
        assert len(result.operations) == 2
        assert result.operations[0].operation_type == OperationType.CREATE_PLAYLIST
        assert result.operations[1].operation_type == OperationType.ADD_TRACK


class TestSyncResultCombining:
    """Tests for combining multiple SyncResult objects."""

    def test_combine_sync_results(self):
        """Should be able to combine results from multiple folders."""
        result1 = SyncResult(
            playlists_created=1,
            tracks_added=10,
            tracks_flagged=2,
        )
        result2 = SyncResult(
            playlists_created=1,
            tracks_added=15,
            tracks_missing=3,
        )

        combined = SyncResult(
            playlists_created=result1.playlists_created + result2.playlists_created,
            tracks_added=result1.tracks_added + result2.tracks_added,
            tracks_missing=result1.tracks_missing + result2.tracks_missing,
            tracks_flagged=result1.tracks_flagged + result2.tracks_flagged,
        )

        assert combined.playlists_created == 2
        assert combined.tracks_added == 25
        assert combined.tracks_missing == 3
        assert combined.tracks_flagged == 2

    def test_extend_operations_list(self):
        """Should be able to extend operations list from multiple results."""
        result = SyncResult()
        folder1_ops = [
            SyncOperation(
                operation_type=OperationType.CREATE_PLAYLIST,
                folder_name="Folder1",
                playlist_name="Discogs - Folder1",
            ),
        ]
        folder2_ops = [
            SyncOperation(
                operation_type=OperationType.ADD_TRACK,
                folder_name="Folder2",
                playlist_name="Discogs - Folder2",
            ),
        ]

        result.operations.extend(folder1_ops)
        result.operations.extend(folder2_ops)

        assert len(result.operations) == 2
        assert result.operations[0].folder_name == "Folder1"
        assert result.operations[1].folder_name == "Folder2"


class TestPlaylistClientProtocol:
    """Tests for PlaylistClient protocol compliance."""

    def test_spotify_client_matches_protocol(self, monkeypatch):
        """SpotifyClient should match PlaylistClient protocol."""
        monkeypatch.setattr(
            "song_automations.clients.spotify.spotipy.oauth2.SpotifyOAuth",
            MagicMock,
        )
        monkeypatch.setattr(
            "song_automations.clients.spotify.spotipy.Spotify",
            MagicMock,
        )

        import importlib

        import song_automations.clients.spotify
        importlib.reload(song_automations.clients.spotify)

        from song_automations.clients.spotify import SpotifyClient

        assert hasattr(SpotifyClient, "search_tracks")
        assert hasattr(SpotifyClient, "find_playlist_by_name")
        assert hasattr(SpotifyClient, "create_playlist")
        assert hasattr(SpotifyClient, "delete_playlist")
        assert hasattr(SpotifyClient, "get_playlist_tracks")
        assert hasattr(SpotifyClient, "add_tracks_to_playlist")
        assert hasattr(SpotifyClient, "remove_tracks_from_playlist")

    def test_soundcloud_client_matches_protocol(self, settings, monkeypatch):
        """SoundCloudClient should match PlaylistClient protocol."""
        from song_automations.clients.soundcloud import SoundCloudClient

        client = SoundCloudClient(settings)

        assert hasattr(client, "search_tracks")
        assert hasattr(client, "find_playlist_by_name")
        assert hasattr(client, "create_playlist")
        assert hasattr(client, "delete_playlist")
        assert hasattr(client, "get_playlist_tracks")
        assert hasattr(client, "add_tracks_to_playlist")
        assert hasattr(client, "remove_tracks_from_playlist")


class TestMultiQuerySearch:
    """Tests for multi-query search functionality."""

    def test_get_search_queries_basic(self, settings):
        """Test basic query generation."""
        engine = SyncEngine(
            settings=settings,
            discogs_client=MagicMock(),
            state_tracker=MagicMock(),
        )
        parsed = parse_track_title("Track Title", "Artist Name")
        release = Release(
            id=1,
            title="Album",
            artist="Artist Name",
            year=2024,
            folder_id=1,
            folder_name="Test",
            label="",
            catalog_number="",
        )

        queries = engine._get_search_queries(parsed, release)

        assert len(queries) >= 1
        assert "Artist Name" in queries[0]
        assert "Track Title" in queries[0]

    @pytest.mark.parametrize(
        "title,label,expected_count",
        [
            ("Track", "", 2),
            ("Track (DJ Remix)", "Kompakt", 5),
            ("Track (Hardfloor Remix)", "", 4),
            ("Track", "Label Records", 3),
        ],
    )
    def test_get_search_queries_count(
        self, settings, title, label, expected_count
    ):
        """Test that correct number of queries are generated."""
        engine = SyncEngine(
            settings=settings,
            discogs_client=MagicMock(),
            state_tracker=MagicMock(),
        )
        parsed = parse_track_title(title, "Artist")
        release = Release(
            id=1,
            title="Album",
            artist="Artist",
            year=2024,
            folder_id=1,
            folder_name="Test",
            label=label,
            catalog_number="",
        )

        queries = engine._get_search_queries(parsed, release)

        assert len(queries) == expected_count

    def test_get_search_queries_with_label(self, settings):
        """Test query generation includes label search."""
        engine = SyncEngine(
            settings=settings,
            discogs_client=MagicMock(),
            state_tracker=MagicMock(),
        )
        parsed = parse_track_title("Track Title (Remix)", "Artist")
        release = Release(
            id=1,
            title="Album",
            artist="Artist",
            year=2024,
            folder_id=1,
            folder_name="Test",
            label="Kompakt",
            catalog_number="KOM123",
        )

        queries = engine._get_search_queries(parsed, release)

        label_query_found = any("Kompakt" in q for q in queries)
        assert label_query_found

    def test_get_search_queries_with_remixer(self, settings):
        """Test query generation includes remixer search."""
        engine = SyncEngine(
            settings=settings,
            discogs_client=MagicMock(),
            state_tracker=MagicMock(),
        )
        parsed = parse_track_title("Track (Hardfloor Remix)", "Original Artist")
        release = Release(
            id=1,
            title="Album",
            artist="Original Artist",
            year=2024,
            folder_id=1,
            folder_name="Test",
            label="",
            catalog_number="",
        )

        queries = engine._get_search_queries(parsed, release)

        remixer_query_found = any("Hardfloor" in q for q in queries)
        assert remixer_query_found

    def test_get_search_queries_respects_max_limit(self, settings):
        """Test that max_search_queries setting is respected."""
        settings.max_search_queries = 2
        engine = SyncEngine(
            settings=settings,
            discogs_client=MagicMock(),
            state_tracker=MagicMock(),
        )
        parsed = parse_track_title("Track (DJ Remix)", "Artist")
        release = Release(
            id=1,
            title="Album",
            artist="Artist",
            year=2024,
            folder_id=1,
            folder_name="Test",
            label="Kompakt",
            catalog_number="",
        )

        queries = engine._get_search_queries(parsed, release)

        assert len(queries) <= 2

    def test_get_search_queries_no_duplicates(self, settings):
        """Test that duplicate queries are not generated."""
        engine = SyncEngine(
            settings=settings,
            discogs_client=MagicMock(),
            state_tracker=MagicMock(),
        )
        parsed = parse_track_title("Track", "Artist")
        release = Release(
            id=1,
            title="Album",
            artist="Artist",
            year=2024,
            folder_id=1,
            folder_name="Test",
            label="",
            catalog_number="",
        )

        queries = engine._get_search_queries(parsed, release)

        normalized = [q.lower().strip() for q in queries]
        assert len(normalized) == len(set(normalized))

    def test_multi_query_search_deduplicates_results(self, settings):
        """Test that multi-query search deduplicates by track ID."""
        engine = SyncEngine(
            settings=settings,
            discogs_client=MagicMock(),
            state_tracker=MagicMock(),
        )
        parsed = parse_track_title("Track (Remix)", "Artist")
        release = Release(
            id=1,
            title="Album",
            artist="Artist",
            year=2024,
            folder_id=1,
            folder_name="Test",
            label="",
            catalog_number="",
        )

        mock_track1 = MagicMock()
        mock_track1.id = 123
        mock_track2 = MagicMock()
        mock_track2.id = 456
        mock_track3 = MagicMock()
        mock_track3.id = 123

        mock_result1 = MagicMock()
        mock_result1.track = mock_track1
        mock_result2 = MagicMock()
        mock_result2.track = mock_track2
        mock_result3 = MagicMock()
        mock_result3.track = mock_track3

        mock_client = MagicMock()
        mock_client.search_tracks.side_effect = [
            [mock_result1, mock_result2],
            [mock_result3],
            [],
        ]

        results = engine._multi_query_search(parsed, release, mock_client)

        assert len(results) == 2
        result_ids = {r.track.id for r in results}
        assert result_ids == {123, 456}
