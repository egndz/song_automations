"""Tests for the snapshots and restore commands and the line a sync prints about its snapshots."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from test_sync.test_engine_sync import (
    QUERY,
    FakeDiscogs,
    FakeSpotify,
    discogs_track,
    item,
    map_folder,
)
from typer.testing import CliRunner

from song_automations import cli
from song_automations.config import Settings
from song_automations.errors import RateLimitedError, ReauthRequiredError
from song_automations.snapshots import (
    BEFORE_DELETE,
    BEFORE_RESTORE,
    BEFORE_SYNC,
    SnapshotError,
    SnapshotStore,
    load_run,
    run_ids,
)
from song_automations.state.tracker import StateTracker
from song_automations.sync.engine import SyncResult
from song_automations.want.sync import WantResult
from test_snapshots.fakes import FakePlatform

RUN = "20250110-080000"
NAME = "Discogs - Electro"
SAVED = ["1", "2", "3"]
CHANGED = ["1", "9"]


@pytest.fixture
def wired(monkeypatch, tmp_path) -> SimpleNamespace:
    """Points the CLI at a temporary data directory and at fake platforms.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Folder standing in for the data directory.

    Returns:
        The settings, the snapshot folder and one fake per platform.
    """
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        discogs_user_token="token",
        spotify_client_id="spotify-id",
        spotify_client_secret="spotify-secret",
        soundcloud_client_id="soundcloud-id",
        soundcloud_client_secret="soundcloud-secret",
    )
    platforms = {name: FakePlatform(name, {}) for name in ("spotify", "soundcloud")}
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_init_settings", lambda *args: settings)
    monkeypatch.setattr(cli, "_make_client", lambda settings, name: platforms[name])
    return SimpleNamespace(settings=settings, root=settings.snapshots_dir, platforms=platforms)


def invoke(arguments, answer=None):
    """Runs the CLI on a terminal wide enough that nothing wraps.

    Args:
        arguments: Command line arguments.
        answer: Text typed at a question, when one is asked.

    Returns:
        The command result.
    """
    return CliRunner().invoke(cli.app, arguments, input=answer, env={"COLUMNS": "250"})


def snapshot(wired, platform, name, saved, current, run_id=RUN, reason=BEFORE_SYNC):
    """Saves a playlist of a wired platform, then makes it hold something else.

    Args:
        wired: The wired CLI.
        platform: spotify or soundcloud.
        name: Playlist name.
        saved: Track ids the snapshot holds.
        current: Track ids the playlist holds afterwards, or None when it is gone.
        run_id: Run to save under.
        reason: What was about to happen to the playlist.

    Returns:
        The fake platform.
    """
    fake = wired.platforms[platform]
    fake.playlists[name] = list(saved)
    playlist = fake.playlist(name)
    SnapshotStore(wired.root, run_id).save(platform, playlist, fake.get_playlist_tracks(playlist.id), reason=reason)
    if current is None:
        del fake.playlists[name]
    else:
        fake.playlists[name] = list(current)
    fake.calls.clear()
    return fake


@pytest.mark.parametrize("folder_exists", [False, True])
def test_listing_without_snapshots_says_so(wired, folder_exists):
    """Before the first changing sync the listing explains that nothing was saved yet."""
    if folder_exists:
        wired.root.mkdir(parents=True)
    result = invoke(["snapshots"])
    assert result.exit_code == 0
    assert "No snapshots yet" in result.output


@pytest.mark.parametrize(
    ("saved", "rows"),
    [
        (
            [("20260101-090000", "spotify", "A", ["1", "2"], BEFORE_SYNC)],
            [r"20260101-090000\W+Spotify\W+1\W+2\W"],
        ),
        (
            [
                ("20260101-090000", "spotify", "A", ["1", "2"], BEFORE_SYNC),
                ("20260102-101500", "soundcloud", "B", ["3"], BEFORE_DELETE),
                ("20260102-101500", "soundcloud", "C", ["4", "5"], BEFORE_SYNC),
                ("20260102-101500", "spotify", "A", ["1"], BEFORE_SYNC),
                ("20260103-233000", "spotify", "A", ["1", "2", "3"], BEFORE_RESTORE),
            ],
            [
                r"20260103-233000\W+Spotify\W+1\W+3\W+before a restore",
                r"20260102-101500\W+SoundCloud\W+2\W+3\W+1 deleted",
                r"20260102-101500\W+Spotify\W+1\W+1\W",
                r"20260101-090000\W+Spotify\W+1\W+2\W",
            ],
        ),
    ],
)
def test_listing_shows_runs_newest_first_with_counts(wired, saved, rows):
    """Each run and platform gets a row with its playlist and track counts, newest run on top."""
    for run_id, platform, name, ids, reason in saved:
        snapshot(wired, platform, name, ids, ids, run_id=run_id, reason=reason)
    result = invoke(["snapshots"])
    found = [re.search(row, result.output) for row in rows]
    assert result.exit_code == 0
    assert all(found), result.output
    assert [match.start() for match in found] == sorted(match.start() for match in found)
    assert "song-automations restore RUN_ID" in result.output


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["20200101-000000"], "There is no snapshot run '20200101-000000'"),
        (["../.."], "There is no snapshot run"),
        ([RUN, "--platform", "deezer"], "Unknown platform 'deezer'"),
        ([RUN, "--platform", "soundcloud"], "holds no snapshot of any playlist on soundcloud"),
        ([RUN, "--playlist", "Nope"], "holds no snapshot of the playlist 'Nope'"),
    ],
)
def test_restore_of_something_that_was_not_saved_is_refused(wired, flat, arguments, message):
    """An unknown run, platform or playlist stops the restore before any platform is asked."""
    fake = snapshot(wired, "spotify", NAME, SAVED, CHANGED)
    result = invoke(["restore", *arguments, "--yes"])
    assert result.exit_code == 1
    assert message in flat(result.output)
    assert fake.calls == []
    assert fake.playlists[NAME] == CHANGED


@pytest.mark.parametrize(
    ("platform", "label", "current", "row"),
    [
        ("spotify", "Spotify", CHANGED, r"Spotify\W+Discogs - Electro\W+2\W+3\W+2\W+1\W+restore"),
        ("soundcloud", "SoundCloud", CHANGED, r"SoundCloud\W+Discogs - Electro\W+2\W+3\W+2\W+1\W+restore"),
        ("spotify", "Spotify", ["3", "2", "1"], r"Spotify\W+Discogs - Electro\W+3\W+3\W+0\W+0\W+reorder"),
        ("soundcloud", "SoundCloud", None, r"SoundCloud\W+Discogs - Electro\W+gone\W+3\W+3\W+0\W+recreate"),
    ],
)
def test_dry_run_shows_the_plan_and_changes_nothing(wired, platform, label, current, row):
    """A dry run prints what would change per playlist and neither writes nor saves anything."""
    fake = snapshot(wired, platform, NAME, SAVED, current)
    result = invoke(["restore", RUN, "--dry-run"])
    assert result.exit_code == 0
    assert re.search(row, result.output), result.output
    assert "DRY RUN" in result.output
    assert fake.writes == []
    assert fake.playlists.get(NAME) == current
    assert run_ids(wired.root) == [RUN]


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize("current", [CHANGED, ["3", "2", "1"], [], ["1", "2", "3", "4"], None])
def test_restore_puts_the_playlist_back_and_can_itself_be_undone(wired, flat, platform, current):
    """With --yes the playlist is exactly as saved, and the run of the restore brings the replaced state back."""
    fake = snapshot(wired, platform, NAME, SAVED, current)
    result = invoke(["restore", RUN, "--yes"])
    output = flat(result.output)
    undo_run = run_ids(wired.root)[0]
    undo = load_run(wired.root, undo_run)[0]
    assert result.exit_code == 0
    assert fake.playlists[NAME] == SAVED
    assert f"Restored {NAME}" in output
    assert "3 of 3 tracks" in output
    assert "The next sync rebuilds these playlists from your Discogs folders again." in output
    assert undo_run != RUN
    assert f"Undo with: song-automations restore {undo_run}" in output
    assert (undo.reason, undo.track_ids) == (BEFORE_RESTORE, current or [])
    assert invoke(["restore", undo_run, "--yes"]).exit_code == 0
    assert fake.playlists[NAME] == (current or [])


@pytest.mark.parametrize(
    ("answer", "exit_code", "restored"),
    [("y\n", 0, True), ("n\n", 1, False), ("\n", 1, False), (None, 1, False)],
)
def test_restore_asks_before_changing_anything(wired, answer, exit_code, restored):
    """Without --yes the restore waits for a yes, and anything else leaves the playlist alone."""
    fake = snapshot(wired, "spotify", NAME, SAVED, CHANGED)
    result = invoke(["restore", RUN], answer=answer)
    assert result.exit_code == exit_code
    assert "Put 1 playlist back?" in result.output
    assert fake.playlists[NAME] == (SAVED if restored else CHANGED)
    assert (fake.writes != []) is restored
    assert len(run_ids(wired.root)) == (2 if restored else 1)


@pytest.mark.parametrize(
    ("arguments", "restored"),
    [
        ([], {("spotify", "A"), ("spotify", "B"), ("soundcloud", "A")}),
        (["--platform", "all"], {("spotify", "A"), ("spotify", "B"), ("soundcloud", "A")}),
        (["--platform", "spotify"], {("spotify", "A"), ("spotify", "B")}),
        (["-p", "soundcloud"], {("soundcloud", "A")}),
        (["--playlist", "A"], {("spotify", "A"), ("soundcloud", "A")}),
        (["--platform", "spotify", "--playlist", "B"], {("spotify", "B")}),
    ],
)
def test_restore_touches_only_the_playlists_asked_for(wired, arguments, restored):
    """Platform and playlist filters decide which playlists are put back, the others stay as they are."""
    every = [("spotify", "A"), ("spotify", "B"), ("soundcloud", "A")]
    for platform, name in every:
        snapshot(wired, platform, name, SAVED, CHANGED)
    result = invoke(["restore", RUN, "--yes", *arguments])
    assert result.exit_code == 0
    for platform, name in every:
        expected = SAVED if (platform, name) in restored else CHANGED
        assert wired.platforms[platform].playlists[name] == expected
    untouched = {platform for platform, _ in every} - {platform for platform, _ in restored}
    assert all(wired.platforms[platform].calls == [] for platform in untouched)


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize("flags", [["--yes"], [], ["--dry-run"]])
def test_restore_of_an_unchanged_playlist_does_nothing(wired, platform, flags):
    """A playlist that already matches its snapshot is neither written nor saved again."""
    fake = snapshot(wired, platform, NAME, SAVED, SAVED)
    result = invoke(["restore", RUN, *flags])
    assert result.exit_code == 0
    assert "Nothing to restore" in result.output
    assert fake.writes == []
    assert run_ids(wired.root) == [RUN]


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
def test_restore_names_soundcloud_tracks_that_did_not_come_back(wired, flat, platform):
    """Tracks SoundCloud no longer takes are counted in the output, Spotify has no such gap."""
    fake = snapshot(wired, platform, NAME, SAVED, CHANGED)
    fake.unavailable = {"2"}
    output = flat(invoke(["restore", RUN, "--yes"]).output)
    lost = platform == "soundcloud"
    assert ("1 saved tracks did not come back" in output) is lost
    assert ("2 of 3 tracks" in output) is lost


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
def test_restore_without_credentials_is_refused(wired, platform):
    """A platform whose app keys are missing is named before any client is built."""
    fake = snapshot(wired, platform, NAME, SAVED, CHANGED)
    setattr(wired.settings, f"{platform}_client_secret", "")
    result = invoke(["restore", RUN, "--yes"])
    assert result.exit_code == 1
    assert f"{platform.upper()}_CLIENT_ID and {platform.upper()}_CLIENT_SECRET not set" in result.output
    assert fake.calls == []


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
def test_restore_stops_when_it_cannot_save_the_current_state(wired, flat, monkeypatch, blocked_root, platform):
    """A restore that cannot write its own snapshot exits with an error and changes nothing."""
    fake = snapshot(wired, platform, NAME, SAVED, CHANGED)
    monkeypatch.setattr(cli, "SnapshotStore", lambda root: SnapshotStore(blocked_root))
    result = invoke(["restore", RUN, "--yes"])
    assert result.exit_code == 1
    assert "The playlist was left unchanged" in flat(result.output)
    assert fake.playlists[NAME] == CHANGED
    assert fake.writes == []


@pytest.mark.parametrize(
    ("error", "exit_code", "message"),
    [
        (RateLimitedError("Spotify", 3600), 2, "Spotify has rate limited this app"),
        (ReauthRequiredError("Spotify"), 2, "Your Spotify login has expired"),
    ],
    ids=lambda value: type(value).__name__,
)
def test_restore_explains_a_platform_that_refuses(wired, flat, monkeypatch, error, exit_code, message):
    """A rate limit or an expired login during a restore ends with the usual message and exit code."""
    fake = snapshot(wired, "spotify", NAME, SAVED, CHANGED)

    def refuse(name):
        """Stands in for a platform that refuses the request.

        Args:
            name: Playlist name.
        """
        raise error

    monkeypatch.setattr(fake, "find_playlist_by_name", refuse)
    result = invoke(["restore", RUN, "--yes"])
    assert result.exit_code == exit_code
    assert message in flat(result.output)
    assert fake.playlists[NAME] == CHANGED


@pytest.mark.parametrize(
    ("snapshot_dir", "dry_run", "shown"),
    [
        (Path("/data/snapshots/20261110-080000"), False, True),
        (None, False, False),
        (None, True, False),
    ],
)
def test_sync_summary_ends_with_where_the_snapshot_is(flat, capsys, monkeypatch, snapshot_dir, dry_run, shown):
    """A run that saved something ends with the folder and the restore command, any other run does not."""
    monkeypatch.setenv("COLUMNS", "250")
    cli._print_sync_result(SyncResult(snapshot_dir=snapshot_dir), dry_run)
    output = flat(capsys.readouterr().out)
    hint = (
        "Playlists were saved before this run changed them. "
        "Undo with: song-automations restore 20261110-080000 (snapshot in /data/snapshots/20261110-080000)"
    )
    assert output.endswith(hint) is shown
    assert ("song-automations restore" in output) is shown


@pytest.mark.parametrize("message", ["Could not save a snapshot of the spotify playlist 'Mix [live]'."])
def test_a_sync_stopped_by_a_failed_snapshot_exits_with_an_error(flat, capsys, monkeypatch, message):
    """A snapshot that cannot be written ends the sync with the message and exit code 1."""

    def run_sync(*args, **kwargs):
        """Stands in for a sync whose snapshot fails.

        Args:
            *args: Ignored.
            **kwargs: Ignored.
        """
        raise SnapshotError(message)

    monkeypatch.setattr(cli, "_run_sync", run_sync)
    with pytest.raises(typer.Exit) as stopped:
        cli._guarded_sync()
    assert stopped.value.exit_code == 1
    assert message in flat(capsys.readouterr().out)


@pytest.mark.parametrize(("through_sync_all", "same_run"), [(True, True), (False, False)])
def test_both_platforms_of_sync_all_save_under_one_run(monkeypatch, root, through_sync_all, same_run):
    """Syncing both platforms gives one run folder holding both, two separate syncs give two."""
    seen = []

    def platform_sync(platform):
        """Builds a stand-in for the sync of one platform that saves one playlist.

        Args:
            platform: spotify or soundcloud.

        Returns:
            The stand-in command.
        """

        def command(**kwargs):
            """Opens a snapshot store the way an engine does and saves a playlist.

            Args:
                **kwargs: Command options.
            """
            fake = FakePlatform(platform, {"Mix": ["1"]})
            store = SnapshotStore(root)
            store.save(platform, fake.playlist("Mix"), fake.get_playlist_tracks("id of Mix"))
            seen.append(store.run_id)

        return command

    monkeypatch.setattr(cli, "sync_spotify", platform_sync("spotify"))
    monkeypatch.setattr(cli, "sync_soundcloud", platform_sync("soundcloud"))
    if through_sync_all:
        assert invoke(["sync", "all"]).exit_code == 0
    else:
        cli.sync_spotify()
        cli.sync_soundcloud()
    assert (seen[0] == seen[1]) is same_run
    assert len(run_ids(root)) == (1 if same_run else 2)
    assert {saved.platform for run_id in run_ids(root) for saved in load_run(root, run_id)} == {"spotify", "soundcloud"}


@pytest.mark.parametrize(
    ("existing", "after_sync", "snapshotted"),
    [
        (["wrong", "hand_added"], ["right"], True),
        (["hand_added", "right", "right"], ["right"], True),
        (["right"], ["right"], False),
    ],
)
def test_a_sync_can_be_undone_from_the_command_line(wired, flat, monkeypatch, existing, after_sync, snapshotted):
    """A sync that rebuilds a playlist prints the restore command, and running it brings the old playlist back."""
    discogs = FakeDiscogs({"Electro": [discogs_track()]})
    spotify = FakeSpotify({NAME: list(existing)}, {QUERY: [item("right", "Glue")]})
    map_folder(StateTracker(wired.settings.db_path), discogs, "Electro")
    monkeypatch.setattr(cli, "DiscogsClient", lambda settings: discogs)
    monkeypatch.setattr(cli, "SpotifyClient", lambda settings: spotify)
    monkeypatch.setattr(cli, "DeezerClient", lambda: object())
    monkeypatch.setattr(cli, "IsrcFinder", lambda *args: None)
    monkeypatch.setattr(cli, "sync_want_playlist", lambda *args, **kwargs: WantResult())
    monkeypatch.setattr(cli, "_make_client", lambda settings, name: spotify)

    synced = invoke(["sync", "spotify", "--exclude-wantlist"])
    runs = run_ids(wired.root)
    assert synced.exit_code == 0, synced.output
    assert spotify.playlists[NAME] == after_sync
    assert len(runs) == (1 if snapshotted else 0)
    assert ("Undo with: song-automations restore" in flat(synced.output)) is snapshotted
    if snapshotted:
        assert flat(synced.output).endswith(
            f"Undo with: song-automations restore {runs[0]} (snapshot in {wired.root / runs[0]})"
        )
        assert invoke(["restore", runs[0], "--yes"]).exit_code == 0
        assert spotify.playlists[NAME] == existing
