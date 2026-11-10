"""Tests for the snapshots the sync engine takes before it changes a playlist."""

from io import StringIO

import pytest
from rich.console import Console
from test_sync.test_engine_sync import (
    QUERY,
    FakeDiscogs,
    FakeSpotify,
    discogs_track,
    item,
    map_folder,
)

from song_automations.config import Settings
from song_automations.snapshots import SnapshotError, SnapshotStore, load_run, run_ids
from song_automations.state.tracker import StateTracker
from song_automations.sync.engine import SyncEngine, SyncResult
from test_snapshots.fakes import FakePlatform

RUN = "20261110-143205"
NAME = "Discogs - Electro"


@pytest.fixture
def state(tmp_path) -> StateTracker:
    """Provides a real state database.

    Args:
        tmp_path: Temporary folder.

    Returns:
        The tracker.
    """
    return StateTracker(tmp_path / "state.db")


def engine_with(state, store, discogs=None, output=None) -> SyncEngine:
    """Builds an engine that saves its snapshots with the given store.

    Args:
        state: Tracker.
        store: Snapshot store.
        discogs: Discogs fake, when the test runs a whole sync.
        output: Text buffer that receives what the engine prints.

    Returns:
        The engine.
    """
    console = Console(file=output, width=500) if output is not None else Console(quiet=True)
    return SyncEngine(Settings(_env_file=None), discogs, state, console, snapshots=store)


def apply(engine, fake, desired, dry_run=False) -> SyncResult:
    """Makes the playlist of a fake platform hold the chosen tracks.

    Args:
        engine: The engine.
        fake: Fake platform holding the playlist.
        desired: Track ids the playlist should hold.
        dry_run: If True, only count what would change.

    Returns:
        The result with the counts.
    """
    result = SyncResult()
    engine._apply_playlist(fake.playlist(NAME), desired, fake, fake.platform, dry_run, result)
    return result


@pytest.mark.parametrize("titled", [True, False])
@pytest.mark.parametrize(
    ("platform", "current", "desired"),
    [
        ("spotify", ["b", "a", "c"], ["a", "d"]),
        ("spotify", ["a", "a"], ["a"]),
        ("spotify", ["a"], ["a", "b"]),
        ("spotify", [], ["a"]),
        ("soundcloud", ["30", "10", "20"], ["10", "40"]),
        ("soundcloud", ["10"], ["10", "20"]),
        ("soundcloud", [], ["10"]),
    ],
)
def test_a_changed_playlist_is_saved_first_from_the_tracks_already_read(root, state, platform, current, desired, titled):
    """The snapshot holds the old tracks in order and costs no call beyond the one read a sync makes anyway."""
    fake = FakePlatform(platform, {NAME: current}, titled=titled)
    store = SnapshotStore(root, RUN)
    apply(engine_with(state, store), fake, desired)
    snapshot = load_run(root, RUN)[0]
    assert fake.playlists[NAME] == desired
    assert (snapshot.platform, snapshot.playlist_name, snapshot.playlist_id) == (platform, NAME, f"id of {NAME}")
    assert snapshot.track_ids == current
    assert [track.title for track in snapshot.tracks] == [f"Title {t}" if titled else None for t in current]
    assert [track.artist for track in snapshot.tracks] == [f"Artist {t}" if titled else None for t in current]
    assert not snapshot.deleted
    assert fake.calls.count("get_playlist_tracks") == 1
    assert "find_playlist_by_name" not in fake.calls
    assert store.written == [root / RUN / platform / f"{NAME}.json"]


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize(
    ("current", "desired", "dry_run"),
    [
        (["1", "2"], ["1", "2"], False),
        (["1", "2"], ["2", "1"], False),
        (["1", "2"], [], False),
        ([], [], False),
        (["1", "2"], ["3"], True),
        ([], ["3"], True),
    ],
)
def test_nothing_is_saved_when_a_run_changes_nothing(root, state, platform, current, desired, dry_run):
    """An unchanged playlist, a playlist left alone and a dry run write no snapshot at all."""
    fake = FakePlatform(platform, {NAME: current})
    store = SnapshotStore(root, RUN)
    apply(engine_with(state, store), fake, desired, dry_run)
    assert fake.playlists[NAME] == current
    assert store.written == []
    assert not root.exists()


@pytest.mark.parametrize(
    ("platform", "current", "desired"),
    [
        ("spotify", ["a", "b"], ["a", "c"]),
        ("spotify", ["a", "a"], ["a"]),
        ("spotify", [], ["a"]),
        ("soundcloud", ["10", "20"], ["10", "30"]),
        ("soundcloud", [], ["10"]),
    ],
)
def test_a_playlist_is_not_changed_when_its_snapshot_cannot_be_written(blocked_root, state, platform, current, desired):
    """A failed snapshot stops the change with an error that names the playlist."""
    fake = FakePlatform(platform, {NAME: current})
    with pytest.raises(SnapshotError) as caught:
        apply(engine_with(state, SnapshotStore(blocked_root, RUN)), fake, desired)
    assert NAME in str(caught.value)
    assert "The playlist was left unchanged" in str(caught.value)
    assert fake.playlists[NAME] == current
    assert fake.writes == []


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize(
    ("dry_run", "writable", "deleted", "saved"),
    [
        (False, True, True, True),
        (True, True, False, False),
        (False, False, False, False),
    ],
)
def test_a_playlist_is_saved_before_it_is_deleted(root, blocked_root, state, platform, dry_run, writable, deleted, saved):
    """The playlist of a vanished folder is saved as deleted first, and kept when that fails."""
    state.save_folder_mapping(99, "Gone", platform, "id of Discogs - Gone", "Discogs - Gone")
    fake = FakePlatform(platform, {"Discogs - Gone": ["1", "2", "3"]})
    store = SnapshotStore(root if writable else blocked_root, RUN)
    engine = engine_with(state, store)
    if writable:
        engine._cleanup_deleted_folders(fake, platform, {1}, dry_run, "sync")
    else:
        with pytest.raises(SnapshotError, match="Discogs - Gone"):
            engine._cleanup_deleted_folders(fake, platform, {1}, dry_run, "sync")
    assert ("Discogs - Gone" not in fake.playlists) is deleted
    assert (state.get_folder_mapping(99, platform) is None) is deleted
    assert bool(store.written) is saved
    if saved:
        snapshot = load_run(root, RUN)[0]
        assert (snapshot.deleted, snapshot.playlist_name, snapshot.track_ids) == (True, "Discogs - Gone", ["1", "2", "3"])
        assert fake.calls == ["find_playlist_by_name", "get_playlist_tracks", "delete_playlist"]


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
def test_a_playlist_left_alone_is_named_on_either_platform(root, state, platform):
    """The warning about a playlist nothing matched for works for a SoundCloud playlist too."""
    output = StringIO()
    fake = FakePlatform(platform, {NAME: ["1"]})
    apply(engine_with(state, SnapshotStore(root, RUN), output=output), fake, [])
    assert f"Nothing matched for '{NAME}'" in output.getvalue()
    assert fake.playlists[NAME] == ["1"]


def run_sync(state, discogs, spotify, dry_run=False, output=None, **settings) -> SyncResult:
    """Runs a whole Spotify sync with the snapshot store the engine builds itself.

    Args:
        state: Tracker.
        discogs: Discogs fake.
        spotify: Spotify fake.
        dry_run: Dry run flag.
        output: Text buffer that receives what the engine prints.
        **settings: Settings to change.

    Returns:
        The result.
    """
    console = Console(file=output, width=500) if output is not None else Console(quiet=True)
    engine = SyncEngine(Settings(_env_file=None, **settings), discogs, state, console)
    return engine.sync_to_spotify(spotify, include_wantlist=False, dry_run=dry_run)


@pytest.mark.parametrize(
    ("existing", "dry_run", "snapshotted"),
    [
        (["wrong"], False, True),
        ([], False, True),
        (["right"], False, False),
        (["wrong"], True, False),
    ],
)
def test_a_sync_reports_where_its_snapshots_are(state, isolated_data_dir, existing, dry_run, snapshotted):
    """A run that changed a playlist names its snapshot folder under the data directory, any other run names none."""
    discogs = FakeDiscogs({"Electro": [discogs_track()]})
    map_folder(state, discogs, "Electro")
    spotify = FakeSpotify({NAME: list(existing)}, {QUERY: [item("right", "Glue")]})
    result = run_sync(state, discogs, spotify, dry_run=dry_run)
    runs = run_ids(isolated_data_dir / "snapshots")
    assert len(runs) == (1 if snapshotted else 0)
    assert result.snapshot_dir == (isolated_data_dir / "snapshots" / runs[0] if snapshotted else None)
    if snapshotted:
        assert load_run(isolated_data_dir / "snapshots", runs[0])[0].track_ids == existing


@pytest.mark.parametrize(
    ("kept", "dry_run", "old_runs_left"),
    [
        (30, False, 30),
        (5, False, 5),
        (1, False, 1),
        (30, True, 33),
    ],
)
def test_old_runs_are_pruned_when_a_real_sync_starts(state, isolated_data_dir, kept, dry_run, old_runs_left):
    """A real run deletes the runs beyond the newest ones kept before it does anything else, a dry run none."""
    root = isolated_data_dir / "snapshots"
    old = [f"202601{day:02d}-000000" for day in range(1, 34)]
    for run_id in old:
        fake = FakePlatform("spotify", {"Old": ["a"]})
        SnapshotStore(root, run_id).save("spotify", fake.playlist("Old"), fake.get_playlist_tracks("id of Old"))
    discogs = FakeDiscogs({"Electro": [discogs_track()]})
    map_folder(state, discogs, "Electro")
    spotify = FakeSpotify({NAME: ["right"]}, {QUERY: [item("right", "Glue")]})
    run_sync(state, discogs, spotify, dry_run=dry_run, snapshot_runs_kept=kept)
    assert run_ids(root) == sorted(old, reverse=True)[:old_runs_left]


class FailingDiscogs(FakeDiscogs):
    """Discogs stand-in whose second folder cannot be read."""

    def get_folder_releases(self, folder_id):
        """Lists releases of the first folder and fails on any other.

        Args:
            folder_id: Folder id.

        Returns:
            Releases.

        Raises:
            RuntimeError: For every folder but the first.
        """
        if folder_id != 1:
            raise RuntimeError("Discogs answered 502")
        return super().get_folder_releases(folder_id)


@pytest.mark.parametrize(("existing", "hinted"), [(["wrong"], True), (["right"], False)])
def test_a_sync_that_fails_midway_still_says_how_to_undo_it(state, isolated_data_dir, existing, hinted):
    """When a later folder stops the run, the playlists changed before it can still be put back."""
    discogs = FailingDiscogs({"Electro": [discogs_track()], "House": [discogs_track(release_id=2)]})
    map_folder(state, discogs, "Electro")
    spotify = FakeSpotify({NAME: list(existing)}, {QUERY: [item("right", "Glue")]})
    output = StringIO()
    with pytest.raises(RuntimeError, match="502"):
        run_sync(state, discogs, spotify, output=output)
    runs = run_ids(isolated_data_dir / "snapshots")
    assert len(runs) == (1 if hinted else 0)
    assert ("Undo with: song-automations restore" in output.getvalue()) is hinted
    if hinted:
        assert f"song-automations restore {runs[0]}" in output.getvalue()
