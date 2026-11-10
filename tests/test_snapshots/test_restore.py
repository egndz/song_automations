"""Tests for putting a playlist back as a snapshot saved it."""

import pytest

from song_automations.snapshots import (
    BEFORE_DELETE,
    BEFORE_RESTORE,
    RestoreOutcome,
    SavedTrack,
    SnapshotError,
    SnapshotStore,
    apply_restore,
    change_lines,
    describe_track,
    load_run,
    plan_restore,
    split_restore,
)
from test_snapshots.fakes import FakePlatform

SAVED_RUN = "20261110-080000"
RESTORE_RUN = "20261110-090000"
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
    step = plan_restore(snapshot, fake)
    outcome = apply_restore(step, fake, SnapshotStore(root, RESTORE_RUN))
    assert fake.playlists[NAME] == saved
    assert outcome == RestoreOutcome()
    assert outcome.problems == []


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize(("saved", "current"), BEFORE_AND_AFTER)
def test_restore_saves_what_it_replaces_so_it_can_be_undone(root, platform, saved, current):
    """A restore is itself a run with a snapshot, and restoring that one brings the playlist back."""
    fake, snapshot = saved_then_changed(root, platform, saved, current)
    apply_restore(plan_restore(snapshot, fake), fake, SnapshotStore(root, RESTORE_RUN))
    undo = load_run(root, RESTORE_RUN)[0]
    assert (undo.track_ids, undo.reason, undo.playlist_name) == (current, BEFORE_RESTORE, NAME)
    apply_restore(plan_restore(undo, fake), fake, SnapshotStore(root, "20261110-100000"))
    assert fake.playlists[NAME] == current


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
    step = plan_restore(load_run(root, SAVED_RUN)[0], fake)
    assert (step.action, step.changes, len(step.put_back)) == ("recreate", True, 3)
    apply_restore(step, fake, SnapshotStore(root, RESTORE_RUN))
    assert fake.playlists[NAME] == ["1", "2", "3"]
    assert fake.created == [(NAME, created_public)]


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
    step = plan_restore(snapshot, fake)
    assert step.action == action
    assert [(track.id, track.title) for track in step.put_back] == [(t, f"Title {t}") for t in put_back]
    assert [(track.id, track.title) for track in step.taken_out] == [(t, f"Title {t}") for t in taken_out]
    assert step.changes is (action != "unchanged")
    assert fake.writes == []
    assert fake.calls == ["find_playlist_by_name"] + ["get_playlist_tracks"] * (current is not None)


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
    assert change_lines(plan_restore(snapshot, fake), limit) == expected


@pytest.mark.parametrize(
    ("platform", "saved", "current", "calls"),
    [
        ("spotify", ["1", "2"], ["1", "2", "3"], ["remove_tracks_from_playlist"]),
        ("spotify", ["1", "2", "3"], ["1", "2"], ["add_tracks_to_playlist"]),
        ("spotify", ["1", "2"], ["2", "1"], ["remove_tracks_from_playlist", "add_tracks_to_playlist"]),
        ("spotify", ["1"], None, ["create_playlist", "add_tracks_to_playlist"]),
        ("spotify", [], None, ["create_playlist"]),
        ("soundcloud", ["1", "2"], ["1", "2", "3"], ["set_playlist_tracks", "get_playlist_tracks"]),
        ("soundcloud", ["1"], None, ["create_playlist", "set_playlist_tracks", "get_playlist_tracks"]),
    ],
)
def test_restore_makes_only_the_calls_it_needs(root, platform, saved, current, calls):
    """Spotify gets a removal and an addition at most, SoundCloud one write and one check."""
    fake, snapshot = saved_then_changed(root, platform, saved, current)
    step = plan_restore(snapshot, fake)
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
    step = plan_restore(snapshot, fake)
    outcome = apply_restore(step, fake, SnapshotStore(root, RESTORE_RUN))
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
    step = plan_restore(snapshot, fake)
    with pytest.raises(SnapshotError, match="The playlist was left unchanged"):
        apply_restore(step, fake, SnapshotStore(blocked_root, RESTORE_RUN))
    assert fake.playlists[NAME] == current
    assert fake.writes == []
