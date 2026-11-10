"""Tests for putting a playlist back as a snapshot saved it."""

from types import SimpleNamespace

import pytest

from song_automations.errors import RateLimitedError
from song_automations.snapshots import (
    BEFORE_DELETE,
    BEFORE_RESTORE,
    RestoreOutcome,
    RestoreStoppedError,
    SavedTrack,
    SnapshotError,
    SnapshotStore,
    apply_restore,
    change_lines,
    describe_track,
    load_run,
    plan_restores,
    run_ids,
    shared_targets,
    split_restore,
)
from test_snapshots.fakes import FakePlatform

SAVED_RUN = "20261110-080000"
RESTORE_RUN = "20261110-090000"
UNDO_RUN = "20261110-100000"
NAME = "Discogs - Electro"

BEFORE_AND_AFTER = [
    (["1", "2", "3"], ["1", "2", "3", "4"]),
    (["1", "2", "3"], ["1", "3"]),
    (["1", "2", "3"], ["1", "9", "3", "4"]),
    (["1", "2", "3"], ["3", "2", "1"]),
    (["1", "2", "3"], []),
    (["1", "2", "3"], ["7", "8"]),
    (["1", "2", "1"], ["1", "2"]),
    (["1", "2"], ["1", "2", "1"]),
    (["1", "1", "2"], ["2", "1"]),
    ([], ["1", "2"]),
]


def saved_then_changed(root, platform, saved, current, **options):
    """Saves a playlist, then makes the platform hold something else.

    Args:
        root: Folder holding one folder per run.
        platform: spotify or soundcloud.
        saved: Track ids the snapshot holds.
        current: Track ids the playlist holds afterwards, or None when it is gone.
        **options: Options of the fake platform.

    Returns:
        The fake platform and the snapshot.
    """
    fake = FakePlatform(platform, {NAME: saved}, **options)
    playlist = fake.playlist(NAME)
    SnapshotStore(root, SAVED_RUN).save(platform, playlist, fake.get_playlist_tracks(playlist.id))
    if current is None:
        del fake.playlists[NAME]
    else:
        fake.playlists[NAME] = list(current)
    fake.calls.clear()
    return fake, load_run(root, SAVED_RUN)[0]


def plan(snapshot, fake):
    """Plans the restore of one snapshot.

    Args:
        snapshot: The saved playlist.
        fake: Fake platform the snapshot belongs to.

    Returns:
        The planned restore.
    """
    return plan_restores([snapshot], {snapshot.platform: fake})[0]


@pytest.mark.parametrize(
    ("current", "saved", "keep", "remove", "add"),
    [
        (["a", "b", "c"], ["a", "b", "c"], 3, [], []),
        (["a", "b"], ["a", "b", "c"], 2, [], ["c"]),
        (["a", "b", "c", "d"], ["a", "b"], 2, ["c", "d"], []),
        (["a", "x", "c"], ["a", "b", "c"], 1, ["x", "c"], ["b", "c"]),
        (["a", "b", "a"], ["a", "b"], 0, ["a", "b"], ["a", "b"]),
        (["a"], ["a", "a"], 1, [], ["a"]),
        (["b", "a"], ["a", "b"], 0, ["b", "a"], ["a", "b"]),
        (["a", "b"], [], 0, ["a", "b"], []),
        ([], ["a", "b"], 0, [], ["a", "b"]),
        ([], [], 0, [], []),
        (["a", "x", "x"], ["a", "b"], 1, ["x"], ["b"]),
    ],
)
def test_matching_start_of_a_playlist_is_left_in_place(current, saved, keep, remove, add):
    """Only what follows the first difference is taken out and added again."""
    assert split_restore(current, saved) == (keep, remove, add)


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize(("saved", "current"), BEFORE_AND_AFTER)
def test_restore_puts_the_same_tracks_back_in_the_same_order(root, platform, saved, current):
    """Whatever a run did to a playlist, a restore leaves it exactly as saved."""
    fake, snapshot = saved_then_changed(root, platform, saved, current)
    outcome = apply_restore(plan(snapshot, fake), fake, SnapshotStore(root, RESTORE_RUN))
    assert fake.playlists[NAME] == saved
    assert outcome == RestoreOutcome()
    assert outcome.problems == []


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize(("saved", "current"), BEFORE_AND_AFTER)
def test_restore_saves_what_it_replaces_so_it_can_be_undone(root, platform, saved, current):
    """A restore is itself a run with a snapshot, and restoring that one brings the playlist back."""
    fake, snapshot = saved_then_changed(root, platform, saved, current)
    apply_restore(plan(snapshot, fake), fake, SnapshotStore(root, RESTORE_RUN))
    undo = load_run(root, RESTORE_RUN)[0]
    assert (undo.track_ids, undo.reason, undo.playlist_name) == (current, BEFORE_RESTORE, NAME)
    apply_restore(plan(undo, fake), fake, SnapshotStore(root, UNDO_RUN))
    assert fake.playlists[NAME] == current


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize(
    ("saved", "previewed", "meanwhile"),
    [
        (["1", "2", "3"], ["1", "9"], ["1", "9", "50", "51"]),
        (["1", "2", "3"], ["1", "9"], ["77"]),
        (["1", "2", "3"], ["1", "9"], []),
        (["1", "2", "3"], ["1", "2", "3", "4"], ["1", "2"]),
        ([], ["1"], ["1", "2", "3"]),
    ],
)
def test_restore_saves_what_the_playlist_holds_right_before_it_writes(root, platform, saved, previewed, meanwhile):
    """A playlist that changed while the question waited is saved as it is then, not as the plan saw it."""
    fake, snapshot = saved_then_changed(root, platform, saved, previewed)
    step = plan(snapshot, fake)
    fake.playlists[NAME] = list(meanwhile)
    outcome = apply_restore(step, fake, SnapshotStore(root, RESTORE_RUN))
    undo = load_run(root, RESTORE_RUN)[0]
    assert fake.playlists[NAME] == saved
    assert outcome.changed_since_preview is True
    assert outcome.problems == []
    assert undo.track_ids == meanwhile
    apply_restore(plan(undo, fake), fake, SnapshotStore(root, UNDO_RUN))
    assert fake.playlists[NAME] == meanwhile


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize(("saved", "previewed"), [(["1", "2", "3"], ["1", "9"]), ([], ["4"])])
def test_restore_writes_nothing_when_the_playlist_was_put_right_meanwhile(root, platform, saved, previewed):
    """A playlist that already matches the snapshot by the time of the restore is neither written nor saved."""
    fake, snapshot = saved_then_changed(root, platform, saved, previewed)
    step = plan(snapshot, fake)
    fake.playlists[NAME] = list(saved)
    fake.calls.clear()
    store = SnapshotStore(root, RESTORE_RUN)
    outcome = apply_restore(step, fake, store)
    assert outcome == RestoreOutcome(changed_since_preview=True)
    assert fake.calls == ["get_playlist_tracks"]
    assert store.written == []


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize(
    ("public", "created_public"),
    [(True, True), (False, False), (None, True)],
)
def test_a_deleted_playlist_is_recreated_by_name_and_filled(root, platform, public, created_public):
    """A playlist that is gone comes back under its name with its tracks and visibility."""
    fake = FakePlatform(platform, {NAME: ["1", "2", "3"]}, public=public)
    playlist = fake.playlist(NAME)
    tracks = fake.get_playlist_tracks(playlist.id)
    SnapshotStore(root, SAVED_RUN).save(platform, playlist, tracks, reason=BEFORE_DELETE)
    fake.delete_playlist(playlist.id)
    step = plan(load_run(root, SAVED_RUN)[0], fake)
    assert (step.action, step.changes, len(step.put_back), step.note) == ("recreate", True, 3, "")
    apply_restore(step, fake, SnapshotStore(root, RESTORE_RUN))
    undo = load_run(root, RESTORE_RUN)[0]
    assert fake.playlists[NAME] == ["1", "2", "3"]
    assert fake.created == [(NAME, created_public)]
    assert (undo.playlist_name, undo.playlist_id, undo.track_ids, undo.reason, undo.public) == (
        NAME,
        f"id of {NAME}",
        [],
        BEFORE_RESTORE,
        public,
    )


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize("saved", [["1", "2", "3"], []])
def test_a_failed_snapshot_leaves_no_empty_playlist_behind(root, blocked_root, platform, saved):
    """A playlist is not recreated when the state before the restore cannot be saved."""
    fake, snapshot = saved_then_changed(root, platform, saved, None)
    step = plan(snapshot, fake)
    with pytest.raises(RestoreStoppedError, match="It was left as it was") as caught:
        apply_restore(step, fake, SnapshotStore(blocked_root, RESTORE_RUN))
    assert isinstance(caught.value.__cause__, SnapshotError)
    assert caught.value.touched is False
    assert NAME not in fake.playlists
    assert fake.writes == []


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
def test_the_snapshot_of_a_missing_playlist_exists_before_it_is_created(root, platform):
    """When creating the playlist fails, the snapshot was already written and nothing was created."""
    fake, snapshot = saved_then_changed(root, platform, ["1", "2"], None)
    step = plan(snapshot, fake)
    fake.fail("create_playlist", RuntimeError("refused"))
    store = SnapshotStore(root, RESTORE_RUN)
    with pytest.raises(RestoreStoppedError) as caught:
        apply_restore(step, fake, store)
    assert caught.value.touched is False
    assert len(store.written) == 1
    assert load_run(root, RESTORE_RUN)[0].track_ids == []
    assert NAME not in fake.playlists


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize(
    ("saved", "current", "action", "put_back", "taken_out"),
    [
        (["1", "2"], ["1", "2"], "unchanged", [], []),
        ([], [], "unchanged", [], []),
        (["1", "2"], ["2", "1"], "reorder", [], []),
        (["1", "2", "3"], ["1"], "restore", ["2", "3"], []),
        (["1"], ["1", "2", "3"], "restore", [], ["2", "3"]),
        (["1", "2", "3"], ["1", "9"], "restore", ["2", "3"], ["9"]),
        (["1", "1"], ["1"], "restore", ["1"], []),
        (["1"], ["1", "1", "1"], "restore", [], ["1", "1"]),
        (["1", "2"], None, "recreate", ["1", "2"], []),
        ([], None, "recreate", [], []),
    ],
)
def test_plan_says_what_a_restore_would_change(root, platform, saved, current, action, put_back, taken_out):
    """The plan names the tracks coming back and going out, by reading and nothing else."""
    fake, snapshot = saved_then_changed(root, platform, saved, current)
    step = plan(snapshot, fake)
    assert step.action == action
    assert [(track.id, track.title) for track in step.put_back] == [(t, f"Title {t}") for t in put_back]
    assert [(track.id, track.title) for track in step.taken_out] == [(t, f"Title {t}") for t in taken_out]
    assert step.changes is (action != "unchanged")
    assert step.note == ""
    assert fake.writes == []
    assert fake.calls == ["get_user_playlists"] + ["get_playlist_tracks"] * (current is not None)


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize("playlists", [1, 3, 6])
def test_the_playlists_of_a_platform_are_listed_once(root, platform, playlists):
    """However many playlists a run holds, finding them costs one listing per platform."""
    names = [f"Mix {number}" for number in range(playlists)]
    fake = FakePlatform(platform, {name: ["1"] for name in names})
    store = SnapshotStore(root, SAVED_RUN)
    for name in names:
        store.save(platform, fake.playlist(name), fake.get_playlist_tracks(f"id of {name}"))
        fake.playlists[name] = ["2"]
    fake.calls.clear()
    steps = plan_restores(load_run(root, SAVED_RUN), {platform: fake})
    assert [step.snapshot.playlist_name for step in steps] == names
    assert fake.calls.count("get_user_playlists") == 1
    assert fake.calls.count("get_playlist_tracks") == playlists


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize("current", [["1", "9"], ["1", "2", "3"], []])
def test_a_renamed_playlist_is_restored_in_place(root, platform, current):
    """A playlist that got another name since is found by its id and not created a second time."""
    fake, snapshot = saved_then_changed(root, platform, ["1", "2", "3"], current)
    fake.rename(NAME, "Renamed")
    step = plan(snapshot, fake)
    assert (step.found_by, step.note) == ("id", "now named 'Renamed'")
    assert step.action == ("unchanged" if current == ["1", "2", "3"] else "restore")
    apply_restore(step, fake, SnapshotStore(root, RESTORE_RUN))
    assert fake.playlists == {"Renamed": ["1", "2", "3"]}
    assert fake.created == []


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize("current", [["1", "9"], []])
def test_a_playlist_that_only_shares_the_name_is_flagged_in_the_plan(root, platform, current):
    """When the saved id is gone, the playlist with the saved name is used and the plan says it is another one."""
    fake, snapshot = saved_then_changed(root, platform, ["1", "2", "3"], None)
    fake.create_playlist(NAME)
    fake.playlists[NAME] = list(current)
    step = plan(snapshot, fake)
    assert str(step.playlist.id) != snapshot.playlist_id
    assert (step.found_by, step.action) == ("name", "restore")
    assert step.note == "same name, but not the playlist that was saved"
    apply_restore(step, fake, SnapshotStore(root, RESTORE_RUN))
    assert fake.playlists[NAME] == ["1", "2", "3"]
    assert len(fake.created) == 1


def two_saved_with_one_name(root, platform):
    """Saves two playlists that share a name and have different ids.

    Args:
        root: Folder holding one folder per run.
        platform: spotify or soundcloud.

    Returns:
        The two snapshots, the one with id one first.
    """
    store = SnapshotStore(root, SAVED_RUN)
    maker = FakePlatform(platform, {})
    for playlist_id, tracks in (("one", ["1", "2"]), ("two", ["3", "4"])):
        store.save(platform, SimpleNamespace(id=playlist_id, name="Twin"), [maker.track(t) for t in tracks])
    return load_run(root, SAVED_RUN)


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize(
    ("live_id", "live_tracks", "actions", "clashes"),
    [
        ("three", ["9"], ["restore", "restore"], 1),
        ("three", [], ["restore", "restore"], 1),
        ("three", ["1", "2"], ["unchanged", "restore"], 0),
        ("one", ["9"], ["restore", "recreate"], 0),
        ("two", ["9"], ["recreate", "restore"], 0),
        (None, None, ["recreate", "recreate"], 0),
    ],
)
def test_two_saved_playlists_never_share_one_live_playlist(root, platform, live_id, live_tracks, actions, clashes):
    """Two saved playlists of one name are refused when both would be written into the same playlist."""
    snapshots = two_saved_with_one_name(root, platform)
    fake = FakePlatform(platform, {} if live_id is None else {"Twin": live_tracks})
    fake.ids["Twin"] = live_id
    steps = plan_restores(snapshots, {platform: fake})
    found = shared_targets([step for step in steps if step.changes])
    assert [step.action for step in steps] == actions
    assert len(found) == clashes
    assert all("2 saved playlists named 'Twin'" in message and "Nothing was changed" in message for message in found)


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        (SavedTrack("7", "Glue", "Bicep"), "Bicep - Glue"),
        (SavedTrack("7", "Glue"), "Glue"),
        (SavedTrack("7", None, "Bicep"), "Bicep"),
        (SavedTrack("7"), "7"),
    ],
)
def test_a_track_is_described_by_what_is_known_about_it(track, expected):
    """Artist and title are shown when the snapshot has them, the id otherwise."""
    assert describe_track(track) == expected


@pytest.mark.parametrize(
    ("saved", "current", "limit", "expected"),
    [
        (["1", "2"], ["1", "2"], 10, []),
        (["1", "2"], ["2", "1"], 10, []),
        (["1", "2", "3"], ["1", "9"], 10, ["+ Artist 2 - Title 2", "+ Artist 3 - Title 3", "- Artist 9 - Title 9"]),
        (["1", "2", "3", "4"], [], 2, ["+ Artist 1 - Title 1", "+ Artist 2 - Title 2", "+ and 2 more"]),
        (["1"], ["1", "7", "8", "9"], 1, ["- Artist 7 - Title 7", "- and 2 more"]),
        (["1", "2"], None, 10, ["+ Artist 1 - Title 1", "+ Artist 2 - Title 2"]),
    ],
)
def test_changes_are_listed_track_by_track_up_to_a_limit(root, saved, current, limit, expected):
    """Tracks that come back and tracks that go are named, and a long list ends in a count."""
    fake, snapshot = saved_then_changed(root, "spotify", saved, current)
    assert change_lines(plan(snapshot, fake), limit) == expected


@pytest.mark.parametrize(
    ("platform", "saved", "current", "calls"),
    [
        ("spotify", ["1", "2"], ["1", "2", "3"], ["get_playlist_tracks", "remove_tracks_from_playlist"]),
        ("spotify", ["1", "2", "3"], ["1", "2"], ["get_playlist_tracks", "add_tracks_to_playlist"]),
        (
            "spotify",
            ["1", "2"],
            ["2", "1"],
            ["get_playlist_tracks", "remove_tracks_from_playlist", "add_tracks_to_playlist"],
        ),
        ("spotify", ["1"], None, ["create_playlist", "add_tracks_to_playlist"]),
        ("spotify", [], None, ["create_playlist"]),
        (
            "soundcloud",
            ["1", "2"],
            ["1", "2", "3"],
            ["get_playlist_tracks", "set_playlist_tracks", "get_playlist_tracks"],
        ),
        ("soundcloud", ["1"], None, ["create_playlist", "set_playlist_tracks", "get_playlist_tracks"]),
    ],
)
def test_restore_makes_only_the_calls_it_needs(root, platform, saved, current, calls):
    """One fresh read, then a removal and an addition at most on Spotify, one write and one check on SoundCloud."""
    fake, snapshot = saved_then_changed(root, platform, saved, current)
    step = plan(snapshot, fake)
    fake.calls.clear()
    apply_restore(step, fake, SnapshotStore(root, RESTORE_RUN))
    assert fake.calls == calls


@pytest.mark.parametrize(
    ("saved", "quirk", "held", "lost", "left_over", "problem"),
    [
        (["1", "2", "3"], {}, ["1", "2", "3"], [], [], None),
        (["1", "2", "3"], {"unavailable": ("2",)}, ["1", "3"], ["2"], [], "1 saved tracks did not come back"),
        (["1", "2", "3"], {"unavailable": ("1", "3")}, ["2"], ["1", "3"], [], "2 saved tracks did not come back"),
        ([], {"ignores_empty": True}, ["9"], [], ["9"], "1 tracks that were not in the snapshot are still there"),
        (["1", "2", "3"], {"reverses": True}, ["3", "2", "1"], [], [], "not in the saved order"),
    ],
)
def test_soundcloud_playlist_that_differs_after_a_restore_is_reported(root, saved, quirk, held, lost, left_over, problem):
    """Whatever SoundCloud silently does differently from what was asked is named afterwards."""
    fake, snapshot = saved_then_changed(root, "soundcloud", saved, ["9"], **quirk)
    outcome = apply_restore(plan(snapshot, fake), fake, SnapshotStore(root, RESTORE_RUN))
    assert fake.playlists[NAME] == held
    assert [(track.id, track.title) for track in outcome.lost] == [(t, f"Title {t}") for t in lost]
    assert [(track.id, track.title) for track in outcome.left_over] == [(t, f"Title {t}") for t in left_over]
    assert outcome.in_order is (held == saved)
    assert len(outcome.problems) == (0 if problem is None else 1)
    assert problem is None or problem in outcome.problems[0]


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize("current", [["1", "2", "3", "4"], ["9"], []])
def test_restore_changes_nothing_when_its_own_snapshot_fails(root, blocked_root, platform, current):
    """A restore that cannot save the current state stops before touching the playlist."""
    fake, snapshot = saved_then_changed(root, platform, ["1", "2", "3"], current)
    step = plan(snapshot, fake)
    with pytest.raises(RestoreStoppedError, match="It was left as it was") as caught:
        apply_restore(step, fake, SnapshotStore(blocked_root, RESTORE_RUN))
    assert isinstance(caught.value.__cause__, SnapshotError)
    assert "The playlist was left unchanged" in str(caught.value.__cause__)
    assert fake.playlists[NAME] == current
    assert fake.writes == []


@pytest.mark.parametrize(
    ("platform", "call", "skip", "touched"),
    [
        ("spotify", "get_playlist_tracks", 0, False),
        ("spotify", "remove_tracks_from_playlist", 0, True),
        ("spotify", "add_tracks_to_playlist", 0, True),
        ("soundcloud", "get_playlist_tracks", 0, False),
        ("soundcloud", "set_playlist_tracks", 0, True),
        ("soundcloud", "get_playlist_tracks", 1, True),
    ],
)
@pytest.mark.parametrize(
    "error",
    [RuntimeError("connection reset"), RateLimitedError("Spotify", 3600), KeyboardInterrupt()],
    ids=lambda value: type(value).__name__,
)
def test_a_restore_that_stops_says_whether_the_playlist_was_touched(root, platform, call, skip, touched, error):
    """A failure names the playlist, keeps its cause and tells a half restored playlist from an untouched one."""
    fake, snapshot = saved_then_changed(root, platform, ["1", "2", "3"], ["1", "9"])
    step = plan(snapshot, fake)
    fake.fail(call, error, skip)
    store = SnapshotStore(root, RESTORE_RUN)
    with pytest.raises(RestoreStoppedError) as caught:
        apply_restore(step, fake, store)
    message = str(caught.value)
    assert caught.value.__cause__ is error
    assert caught.value.touched is touched
    assert f"{platform} playlist '{NAME}'" in message
    assert ("may hold only part of the saved tracks" in message) is touched
    assert ("It was left as it was" in message) is not touched
    assert (fake.writes == []) is not touched
    assert bool(store.written) is touched
    if touched:
        assert load_run(root, RESTORE_RUN)[0].track_ids == ["1", "9"]


@pytest.mark.parametrize(
    ("platform", "call"),
    [
        ("spotify", "add_tracks_to_playlist"),
        ("spotify", "remove_tracks_from_playlist"),
        ("soundcloud", "set_playlist_tracks"),
    ],
)
def test_a_half_restored_playlist_can_be_finished_or_undone(root, platform, call):
    """After a failure in the middle, running the restore again finishes it and the safety run undoes it."""
    fake, snapshot = saved_then_changed(root, platform, ["1", "2", "3"], ["1", "9"])
    fake.fail(call, RuntimeError("connection reset"))
    with pytest.raises(RestoreStoppedError):
        apply_restore(plan(snapshot, fake), fake, SnapshotStore(root, RESTORE_RUN))
    fake.failures.clear()
    apply_restore(plan(snapshot, fake), fake, SnapshotStore(root, UNDO_RUN))
    assert fake.playlists[NAME] == ["1", "2", "3"]
    undo = load_run(root, RESTORE_RUN)[0]
    apply_restore(plan(undo, fake), fake, SnapshotStore(root, "20261110-110000"))
    assert fake.playlists[NAME] == ["1", "9"]
    assert run_ids(root)[0] == "20261110-110000"
