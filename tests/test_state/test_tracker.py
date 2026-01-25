"""Tests for state tracker."""

from datetime import datetime

import pytest

from song_automations.state.tracker import (
    FolderMapping,
    MatchedTrack,
    MissingTrack,
    StateTracker,
    SyncLog,
    SyncSummary,
)


class TestFolderMapping:
    """Tests for FolderMapping dataclass."""

    @pytest.mark.parametrize(
        "folder_id,folder_name,destination,playlist_id",
        [
            (1, "Electronic", "spotify", "abc123"),
            (9056466, "House", "soundcloud", "456"),
            (-1, "Wantlist", "spotify", "xyz789"),
        ],
    )
    def test_folder_mapping_properties(
        self, folder_id, folder_name, destination, playlist_id
    ):
        """FolderMapping should store properties correctly."""
        mapping = FolderMapping(
            discogs_folder_id=folder_id,
            discogs_folder_name=folder_name,
            destination=destination,
            playlist_id=playlist_id,
            playlist_name=f"Discogs - {folder_name}",
            created_at=datetime.now(),
        )
        assert mapping.discogs_folder_id == folder_id
        assert mapping.discogs_folder_name == folder_name
        assert mapping.destination == destination
        assert mapping.playlist_id == playlist_id


class TestMatchedTrack:
    """Tests for MatchedTrack dataclass."""

    @pytest.mark.parametrize(
        "release_id,position,confidence",
        [
            (12345, "A1", 0.95),
            (67890, "B2", 0.45),
            (11111, "1", 0.30),
        ],
    )
    def test_matched_track_properties(self, release_id, position, confidence):
        """MatchedTrack should store properties correctly."""
        track = MatchedTrack(
            discogs_release_id=release_id,
            discogs_track_position=position,
            artist="Test Artist",
            track_name="Test Track",
            destination="spotify",
            destination_track_id="spotify123",
            match_confidence=confidence,
            searched_at=datetime.now(),
        )
        assert track.discogs_release_id == release_id
        assert track.discogs_track_position == position
        assert track.match_confidence == confidence

    def test_matched_track_with_none_destination_id(self):
        """MatchedTrack should allow None destination_track_id."""
        track = MatchedTrack(
            discogs_release_id=12345,
            discogs_track_position="A1",
            artist="Artist",
            track_name="Track",
            destination="spotify",
            destination_track_id=None,
            match_confidence=0.0,
            searched_at=datetime.now(),
        )
        assert track.destination_track_id is None


class TestMissingTrack:
    """Tests for MissingTrack dataclass."""

    @pytest.mark.parametrize(
        "release_id,folder_id,destination",
        [
            (12345, 1, "spotify"),
            (67890, 9056466, "soundcloud"),
            (11111, -1, "spotify"),
        ],
    )
    def test_missing_track_properties(self, release_id, folder_id, destination):
        """MissingTrack should store properties correctly."""
        track = MissingTrack(
            discogs_release_id=release_id,
            discogs_folder_id=folder_id,
            artist="Unknown Artist",
            track_name="Rare Track",
            destination=destination,
            searched_at=datetime.now(),
        )
        assert track.discogs_release_id == release_id
        assert track.discogs_folder_id == folder_id
        assert track.destination == destination


class TestStateTrackerFolderMappings:
    """Tests for StateTracker folder mapping operations."""

    @pytest.fixture
    def tracker(self, tmp_path):
        """Create a StateTracker with a temporary database."""
        db_path = tmp_path / "test_state.db"
        return StateTracker(db_path)

    def test_save_and_get_folder_mapping(self, tracker):
        """Should save and retrieve folder mappings."""
        tracker.save_folder_mapping(
            discogs_folder_id=123,
            discogs_folder_name="Electronic",
            destination="spotify",
            playlist_id="playlist123",
            playlist_name="Discogs - Electronic",
        )

        mapping = tracker.get_folder_mapping(123, "spotify")

        assert mapping is not None
        assert mapping.discogs_folder_id == 123
        assert mapping.discogs_folder_name == "Electronic"
        assert mapping.playlist_id == "playlist123"

    def test_get_nonexistent_mapping_returns_none(self, tracker):
        """Should return None for nonexistent mappings."""
        mapping = tracker.get_folder_mapping(999, "spotify")
        assert mapping is None

    @pytest.mark.parametrize("destination", ["spotify", "soundcloud"])
    def test_mappings_are_destination_specific(self, tracker, destination):
        """Mappings should be unique per destination."""
        tracker.save_folder_mapping(
            discogs_folder_id=123,
            discogs_folder_name="Electronic",
            destination=destination,
            playlist_id=f"playlist_{destination}",
            playlist_name="Discogs - Electronic",
        )

        mapping = tracker.get_folder_mapping(123, destination)
        assert mapping is not None
        assert mapping.playlist_id == f"playlist_{destination}"

    def test_get_all_folder_mappings(self, tracker):
        """Should retrieve all mappings for a destination."""
        tracker.save_folder_mapping(
            discogs_folder_id=1,
            discogs_folder_name="Electronic",
            destination="spotify",
            playlist_id="p1",
            playlist_name="Discogs - Electronic",
        )
        tracker.save_folder_mapping(
            discogs_folder_id=2,
            discogs_folder_name="House",
            destination="spotify",
            playlist_id="p2",
            playlist_name="Discogs - House",
        )
        tracker.save_folder_mapping(
            discogs_folder_id=3,
            discogs_folder_name="Techno",
            destination="soundcloud",
            playlist_id="p3",
            playlist_name="Discogs - Techno",
        )

        spotify_mappings = tracker.get_all_folder_mappings("spotify")
        soundcloud_mappings = tracker.get_all_folder_mappings("soundcloud")

        assert len(spotify_mappings) == 2
        assert len(soundcloud_mappings) == 1

    def test_delete_folder_mapping(self, tracker):
        """Should delete folder mappings."""
        tracker.save_folder_mapping(
            discogs_folder_id=123,
            discogs_folder_name="Electronic",
            destination="spotify",
            playlist_id="p1",
            playlist_name="Discogs - Electronic",
        )

        tracker.delete_folder_mapping(123, "spotify")

        mapping = tracker.get_folder_mapping(123, "spotify")
        assert mapping is None

    def test_save_mapping_updates_existing(self, tracker):
        """Should update existing mappings on save."""
        tracker.save_folder_mapping(
            discogs_folder_id=123,
            discogs_folder_name="Electronic",
            destination="spotify",
            playlist_id="old_id",
            playlist_name="Old Name",
        )
        tracker.save_folder_mapping(
            discogs_folder_id=123,
            discogs_folder_name="Electronic Updated",
            destination="spotify",
            playlist_id="new_id",
            playlist_name="New Name",
        )

        mapping = tracker.get_folder_mapping(123, "spotify")
        assert mapping.playlist_id == "new_id"
        assert mapping.discogs_folder_name == "Electronic Updated"


class TestStateTrackerMatchedTracks:
    """Tests for StateTracker matched track operations."""

    @pytest.fixture
    def tracker(self, tmp_path):
        """Create a StateTracker with a temporary database."""
        db_path = tmp_path / "test_state.db"
        return StateTracker(db_path)

    def test_save_and_get_cached_match(self, tracker):
        """Should save and retrieve cached matches."""
        tracker.save_matched_track(
            discogs_release_id=12345,
            track_position="A1",
            artist="Test Artist",
            track_name="Test Track",
            destination="spotify",
            destination_track_id="spotify123",
            match_confidence=0.85,
        )

        match = tracker.get_cached_match(12345, "A1", "spotify")

        assert match is not None
        assert match.destination_track_id == "spotify123"
        assert match.match_confidence == 0.85

    def test_get_nonexistent_match_returns_none(self, tracker):
        """Should return None for nonexistent matches."""
        match = tracker.get_cached_match(999, "Z9", "spotify")
        assert match is None

    def test_save_unmatched_track(self, tracker):
        """Should save tracks that weren't matched."""
        tracker.save_matched_track(
            discogs_release_id=12345,
            track_position="A1",
            artist="Rare Artist",
            track_name="Rare Track",
            destination="spotify",
            destination_track_id=None,
            match_confidence=0.0,
        )

        match = tracker.get_cached_match(12345, "A1", "spotify")
        assert match is not None
        assert match.destination_track_id is None
        assert match.match_confidence == 0.0

    def test_get_matched_track_ids(self, tracker):
        """Should retrieve all matched track IDs for a release."""
        tracker.save_matched_track(
            discogs_release_id=12345,
            track_position="A1",
            artist="Artist",
            track_name="Track 1",
            destination="spotify",
            destination_track_id="id1",
            match_confidence=0.9,
        )
        tracker.save_matched_track(
            discogs_release_id=12345,
            track_position="A2",
            artist="Artist",
            track_name="Track 2",
            destination="spotify",
            destination_track_id="id2",
            match_confidence=0.8,
        )
        tracker.save_matched_track(
            discogs_release_id=12345,
            track_position="B1",
            artist="Artist",
            track_name="Track 3",
            destination="spotify",
            destination_track_id=None,
            match_confidence=0.0,
        )

        track_ids = tracker.get_matched_track_ids(12345, "spotify")
        assert len(track_ids) == 2
        assert "id1" in track_ids
        assert "id2" in track_ids


class TestStateTrackerMissingTracks:
    """Tests for StateTracker missing track operations."""

    @pytest.fixture
    def tracker(self, tmp_path):
        """Create a StateTracker with a temporary database."""
        db_path = tmp_path / "test_state.db"
        return StateTracker(db_path)

    def test_save_and_get_missing_tracks(self, tracker):
        """Should save and retrieve missing tracks."""
        tracker.save_missing_track(
            discogs_release_id=12345,
            discogs_folder_id=1,
            artist="Rare Artist",
            track_name="Rare Track",
            destination="spotify",
        )

        missing = tracker.get_missing_tracks("spotify")
        assert len(missing) == 1
        assert missing[0].artist == "Rare Artist"
        assert missing[0].track_name == "Rare Track"

    def test_get_missing_tracks_filters_by_destination(self, tracker):
        """Should filter missing tracks by destination."""
        tracker.save_missing_track(
            discogs_release_id=1,
            discogs_folder_id=1,
            artist="Artist",
            track_name="Track 1",
            destination="spotify",
        )
        tracker.save_missing_track(
            discogs_release_id=2,
            discogs_folder_id=1,
            artist="Artist",
            track_name="Track 2",
            destination="soundcloud",
        )

        spotify_missing = tracker.get_missing_tracks("spotify")
        soundcloud_missing = tracker.get_missing_tracks("soundcloud")
        all_missing = tracker.get_missing_tracks()

        assert len(spotify_missing) == 1
        assert len(soundcloud_missing) == 1
        assert len(all_missing) == 2

    def test_clear_missing_tracks_by_destination(self, tracker):
        """Should clear missing tracks for a specific destination."""
        tracker.save_missing_track(
            discogs_release_id=1,
            discogs_folder_id=1,
            artist="Artist",
            track_name="Track",
            destination="spotify",
        )
        tracker.save_missing_track(
            discogs_release_id=2,
            discogs_folder_id=1,
            artist="Artist",
            track_name="Track",
            destination="soundcloud",
        )

        tracker.clear_missing_tracks("spotify")

        assert len(tracker.get_missing_tracks("spotify")) == 0
        assert len(tracker.get_missing_tracks("soundcloud")) == 1

    def test_clear_all_missing_tracks(self, tracker):
        """Should clear all missing tracks."""
        tracker.save_missing_track(
            discogs_release_id=1,
            discogs_folder_id=1,
            artist="Artist",
            track_name="Track 1",
            destination="spotify",
        )
        tracker.save_missing_track(
            discogs_release_id=2,
            discogs_folder_id=1,
            artist="Artist",
            track_name="Track 2",
            destination="soundcloud",
        )

        tracker.clear_missing_tracks()

        assert len(tracker.get_missing_tracks()) == 0


class TestStateTrackerFolderReleases:
    """Tests for StateTracker folder release tracking."""

    @pytest.fixture
    def tracker(self, tmp_path):
        """Create a StateTracker with a temporary database."""
        db_path = tmp_path / "test_state.db"
        return StateTracker(db_path)

    def test_update_and_get_folder_releases(self, tracker):
        """Should update and retrieve folder releases."""
        release_ids = [123, 456, 789]
        tracker.update_folder_releases(1, release_ids)

        result = tracker.get_folder_release_ids(1)
        assert set(result) == set(release_ids)

    def test_update_folder_releases_replaces_existing(self, tracker):
        """Should replace existing releases on update."""
        tracker.update_folder_releases(1, [123, 456])
        tracker.update_folder_releases(1, [789, 101])

        result = tracker.get_folder_release_ids(1)
        assert set(result) == {789, 101}

    def test_get_empty_folder_releases(self, tracker):
        """Should return empty list for folders with no releases."""
        result = tracker.get_folder_release_ids(999)
        assert result == []


class TestStateTrackerDatabaseInit:
    """Tests for StateTracker database initialization."""

    def test_creates_database_file(self, tmp_path):
        """Should create database file on initialization."""
        db_path = tmp_path / "subdir" / "test_state.db"
        StateTracker(db_path)

        assert db_path.exists()

    def test_creates_parent_directories(self, tmp_path):
        """Should create parent directories if they don't exist."""
        db_path = tmp_path / "deep" / "nested" / "path" / "test_state.db"
        StateTracker(db_path)

        assert db_path.parent.exists()

    def test_reuses_existing_database(self, tmp_path):
        """Should reuse existing database with data."""
        db_path = tmp_path / "test_state.db"

        tracker1 = StateTracker(db_path)
        tracker1.save_folder_mapping(
            discogs_folder_id=123,
            discogs_folder_name="Test",
            destination="spotify",
            playlist_id="p1",
            playlist_name="Test",
        )

        tracker2 = StateTracker(db_path)
        mapping = tracker2.get_folder_mapping(123, "spotify")

        assert mapping is not None
        assert mapping.discogs_folder_name == "Test"


class TestSyncLog:
    """Tests for SyncLog dataclass."""

    @pytest.mark.parametrize(
        "event_type,status",
        [
            ("sync_start", "info"),
            ("track_matched", "success"),
            ("track_missing", "warning"),
            ("exception", "error"),
        ],
    )
    def test_sync_log_properties(self, event_type, status):
        """SyncLog should store properties correctly."""
        log = SyncLog(
            id=1,
            sync_id="abc-123",
            destination="spotify",
            folder_id=42,
            folder_name="Electronic",
            event_type=event_type,
            status=status,
            track_artist="Test Artist",
            track_name="Test Track",
            track_confidence=0.85,
            message="Test message",
            details={"key": "value"},
            created_at=datetime.now(),
        )
        assert log.event_type == event_type
        assert log.status == status
        assert log.destination == "spotify"


class TestSyncSummary:
    """Tests for SyncSummary dataclass."""

    def test_sync_summary_properties(self):
        """SyncSummary should store aggregate stats correctly."""
        summary = SyncSummary(
            sync_id="abc-123",
            destination="spotify",
            started_at=datetime.now(),
            completed_at=datetime.now(),
            total_events=100,
            success_count=80,
            warning_count=15,
            error_count=5,
            tracks_matched=75,
            tracks_flagged=10,
            tracks_missing=15,
            playlists_created=3,
            folders_processed=5,
        )
        assert summary.total_events == 100
        assert summary.success_count == 80
        assert summary.error_count == 5


class TestStateTrackerSyncLogs:
    """Tests for StateTracker sync log operations."""

    @pytest.fixture
    def tracker(self, tmp_path):
        """Create a StateTracker with a temporary database."""
        db_path = tmp_path / "test_state.db"
        return StateTracker(db_path)

    def test_log_sync_event_creates_entry(self, tracker):
        """Should create log entries."""
        tracker.log_sync_event(
            sync_id="test-sync-id",
            destination="spotify",
            event_type="sync_start",
            status="info",
            message="Test sync started",
        )

        logs = tracker.get_sync_logs()
        assert len(logs) == 1
        assert logs[0].sync_id == "test-sync-id"
        assert logs[0].event_type == "sync_start"
        assert logs[0].status == "info"

    @pytest.mark.parametrize(
        "event_type,status,track_confidence",
        [
            ("track_matched", "success", 0.95),
            ("track_flagged", "warning", 0.45),
            ("track_missing", "warning", 0.20),
            ("exception", "error", None),
        ],
    )
    def test_log_sync_event_with_track_info(
        self, tracker, event_type, status, track_confidence
    ):
        """Should log track information correctly."""
        tracker.log_sync_event(
            sync_id="test-sync-id",
            destination="spotify",
            event_type=event_type,
            status=status,
            folder_id=123,
            folder_name="Electronic",
            track_artist="Test Artist",
            track_name="Test Track",
            track_confidence=track_confidence,
            message=f"Test {event_type}",
        )

        logs = tracker.get_sync_logs()
        assert len(logs) == 1
        assert logs[0].event_type == event_type
        assert logs[0].track_artist == "Test Artist"
        assert logs[0].track_confidence == track_confidence

    def test_log_sync_event_with_details(self, tracker):
        """Should store JSON details correctly."""
        details = {
            "error": "Connection timeout",
            "traceback": "Traceback (most recent call last)...",
            "retry_count": 3,
        }
        tracker.log_sync_event(
            sync_id="test-sync-id",
            destination="spotify",
            event_type="exception",
            status="error",
            message="API error",
            details=details,
        )

        logs = tracker.get_sync_logs()
        assert len(logs) == 1
        assert logs[0].details == details
        assert logs[0].details["retry_count"] == 3

    def test_get_sync_logs_filters_by_destination(self, tracker):
        """Should filter logs by destination."""
        tracker.log_sync_event(
            sync_id="sync-1",
            destination="spotify",
            event_type="sync_start",
            status="info",
        )
        tracker.log_sync_event(
            sync_id="sync-2",
            destination="soundcloud",
            event_type="sync_start",
            status="info",
        )

        spotify_logs = tracker.get_sync_logs(destination="spotify")
        soundcloud_logs = tracker.get_sync_logs(destination="soundcloud")
        all_logs = tracker.get_sync_logs()

        assert len(spotify_logs) == 1
        assert len(soundcloud_logs) == 1
        assert len(all_logs) == 2

    def test_get_sync_logs_filters_by_status(self, tracker):
        """Should filter logs by status."""
        tracker.log_sync_event(
            sync_id="sync-1",
            destination="spotify",
            event_type="track_matched",
            status="success",
        )
        tracker.log_sync_event(
            sync_id="sync-1",
            destination="spotify",
            event_type="track_missing",
            status="warning",
        )
        tracker.log_sync_event(
            sync_id="sync-1",
            destination="spotify",
            event_type="exception",
            status="error",
        )

        success_logs = tracker.get_sync_logs(status="success")
        warning_logs = tracker.get_sync_logs(status="warning")
        error_logs = tracker.get_sync_logs(status="error")

        assert len(success_logs) == 1
        assert len(warning_logs) == 1
        assert len(error_logs) == 1

    def test_get_sync_logs_filters_by_sync_id(self, tracker):
        """Should filter logs by sync run ID."""
        for i in range(3):
            tracker.log_sync_event(
                sync_id="sync-1",
                destination="spotify",
                event_type="track_matched",
                status="success",
            )
        for i in range(2):
            tracker.log_sync_event(
                sync_id="sync-2",
                destination="spotify",
                event_type="track_matched",
                status="success",
            )

        sync1_logs = tracker.get_sync_logs(sync_id="sync-1")
        sync2_logs = tracker.get_sync_logs(sync_id="sync-2")

        assert len(sync1_logs) == 3
        assert len(sync2_logs) == 2

    def test_get_sync_logs_pagination(self, tracker):
        """Should paginate logs correctly."""
        for i in range(25):
            tracker.log_sync_event(
                sync_id="sync-1",
                destination="spotify",
                event_type="track_matched",
                status="success",
                message=f"Track {i}",
            )

        page1 = tracker.get_sync_logs(limit=10, offset=0)
        page2 = tracker.get_sync_logs(limit=10, offset=10)
        page3 = tracker.get_sync_logs(limit=10, offset=20)

        assert len(page1) == 10
        assert len(page2) == 10
        assert len(page3) == 5

    def test_get_sync_log_count(self, tracker):
        """Should count logs with filters."""
        for _ in range(5):
            tracker.log_sync_event(
                sync_id="sync-1",
                destination="spotify",
                event_type="track_matched",
                status="success",
            )
        for _ in range(3):
            tracker.log_sync_event(
                sync_id="sync-1",
                destination="spotify",
                event_type="track_missing",
                status="warning",
            )

        total = tracker.get_sync_log_count()
        success_count = tracker.get_sync_log_count(status="success")
        warning_count = tracker.get_sync_log_count(status="warning")

        assert total == 8
        assert success_count == 5
        assert warning_count == 3

    def test_get_sync_summary(self, tracker):
        """Should calculate aggregate stats for sync run."""
        sync_id = "test-sync-123"

        tracker.log_sync_event(
            sync_id=sync_id,
            destination="spotify",
            event_type="sync_start",
            status="info",
        )
        for i in range(10):
            tracker.log_sync_event(
                sync_id=sync_id,
                destination="spotify",
                folder_id=i % 3,
                event_type="track_matched",
                status="success",
            )
        for i in range(3):
            tracker.log_sync_event(
                sync_id=sync_id,
                destination="spotify",
                folder_id=i,
                event_type="track_flagged",
                status="warning",
            )
        for i in range(2):
            tracker.log_sync_event(
                sync_id=sync_id,
                destination="spotify",
                folder_id=i,
                event_type="track_missing",
                status="warning",
            )
        tracker.log_sync_event(
            sync_id=sync_id,
            destination="spotify",
            folder_id=0,
            event_type="playlist_created",
            status="success",
        )
        tracker.log_sync_event(
            sync_id=sync_id,
            destination="spotify",
            event_type="sync_complete",
            status="info",
        )

        summary = tracker.get_sync_summary(sync_id)

        assert summary is not None
        assert summary.sync_id == sync_id
        assert summary.destination == "spotify"
        assert summary.total_events == 18
        assert summary.success_count == 11
        assert summary.warning_count == 5
        assert summary.tracks_matched == 10
        assert summary.tracks_flagged == 3
        assert summary.tracks_missing == 2
        assert summary.playlists_created == 1
        assert summary.folders_processed == 3

    def test_get_sync_summary_nonexistent_returns_none(self, tracker):
        """Should return None for nonexistent sync ID."""
        summary = tracker.get_sync_summary("nonexistent-sync-id")
        assert summary is None

    def test_get_recent_sync_ids(self, tracker):
        """Should get recent sync IDs with timestamps."""
        tracker.log_sync_event(
            sync_id="sync-1",
            destination="spotify",
            event_type="sync_start",
            status="info",
        )
        tracker.log_sync_event(
            sync_id="sync-2",
            destination="soundcloud",
            event_type="sync_start",
            status="info",
        )

        recent = tracker.get_recent_sync_ids(limit=10)

        assert len(recent) == 2
        sync_ids = [r[0] for r in recent]
        assert "sync-1" in sync_ids
        assert "sync-2" in sync_ids

    def test_cleanup_old_logs(self, tracker):
        """Should delete logs older than specified days."""
        tracker.log_sync_event(
            sync_id="sync-1",
            destination="spotify",
            event_type="sync_start",
            status="info",
        )

        deleted = tracker.cleanup_old_logs(days=0)

        assert deleted == 0

        deleted = tracker.cleanup_old_logs(days=1)

        assert deleted == 0

    def test_cleanup_old_logs_returns_count(self, tracker):
        """Should return count of deleted records."""
        for i in range(5):
            tracker.log_sync_event(
                sync_id=f"sync-{i}",
                destination="spotify",
                event_type="sync_start",
                status="info",
            )

        initial_count = tracker.get_sync_log_count()
        assert initial_count == 5

        deleted = tracker.cleanup_old_logs(days=365)
        assert deleted == 0

        final_count = tracker.get_sync_log_count()
        assert final_count == 5
