"""Tests for state tracker."""

from datetime import datetime

import pytest

from song_automations.state.tracker import (
    FolderMapping,
    MatchedTrack,
    MissingTrack,
    StateTracker,
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
