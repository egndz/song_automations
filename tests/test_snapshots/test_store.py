"""Tests for writing, listing and pruning playlist snapshots."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from song_automations import snapshots
from song_automations.snapshots import (
    BEFORE_DELETE,
    BEFORE_RESTORE,
    BEFORE_SYNC,
    RunSummary,
    SnapshotError,
    SnapshotStore,
    list_runs,
    load_run,
    new_run_id,
    prune_runs,
    read_snapshot,
    run_ids,
    safe_file_stem,
    select_snapshots,
    shared_run,
)
from test_snapshots.fakes import FakePlatform

RUN = "20261110-143205"


def save(store, platform, name, ids, reason=BEFORE_SYNC, **options):
    """Saves a playlist of a fake platform.

    Args:
        store: Store to save with.
        platform: spotify or soundcloud.
        name: Playlist name.
        ids: Track ids the playlist holds.
        reason: What is about to happen to the playlist.
        **options: Options of the fake platform.

    Returns:
        The file holding the snapshot.
    """
    fake = FakePlatform(platform, {name: ids}, **options)
    playlist = fake.playlist(name)
    return store.save(platform, playlist, fake.get_playlist_tracks(playlist.id), reason=reason)


def fill(root, runs):
    """Creates runs holding one small playlist each.

    Args:
        root: Folder holding one folder per run.
        runs: Run ids to create.
    """
    for run_id in runs:
        save(SnapshotStore(root, run_id), "spotify", "Mix", ["a"])


@pytest.mark.parametrize(
    ("platform", "ids", "public"),
    [
        ("spotify", ["b", "a", "c"], True),
        ("spotify", ["a", "a"], False),
        ("spotify", [], None),
        ("soundcloud", ["30", "10", "20"], True),
        ("soundcloud", [], False),
    ],
)
def test_snapshot_file_holds_the_playlist_in_order(root, platform, ids, public):
    """A snapshot lands in the run and platform folder and names every track in order."""
    before = datetime.now()
    path = save(SnapshotStore(root, RUN), platform, "Discogs - Electro", ids, public=public)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path == root / RUN / platform / "Discogs - Electro.json"
    assert (data["platform"], data["playlist_id"], data["playlist_name"]) == (
        platform,
        "id of Discogs - Electro",
        "Discogs - Electro",
    )
    assert (data["run_id"], data["public"], data["deleted"], data["reason"]) == (RUN, public, False, "sync")
    assert before <= datetime.fromisoformat(data["saved_at"]) <= datetime.now()
    assert data["tracks"] == [
        {"id": track_id, "title": f"Title {track_id}", "artist": f"Artist {track_id}"} for track_id in ids
    ]
    assert data["track_count"] == len(ids)


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        (SimpleNamespace(id="a"), {"id": "a"}),
        (SimpleNamespace(id=7), {"id": "7"}),
        (SimpleNamespace(id="a", name="Glue"), {"id": "a", "title": "Glue"}),
        (SimpleNamespace(id="a", artist="Bicep"), {"id": "a", "artist": "Bicep"}),
        (SimpleNamespace(id="a", name="", artist=None), {"id": "a"}),
        (SimpleNamespace(id="a", name=object(), artist=12), {"id": "a"}),
        (SimpleNamespace(id=7, title="Glue", artist="Bicep"), {"id": "7", "title": "Glue", "artist": "Bicep"}),
    ],
)
def test_title_and_artist_are_saved_only_when_the_track_carries_them(root, track, expected):
    """A track always keeps its id, and a title or artist only when it is real text."""
    playlist = SimpleNamespace(id="p", name="Mix")
    path = SnapshotStore(root, RUN).save("spotify", playlist, [track])
    assert json.loads(path.read_text(encoding="utf-8"))["tracks"] == [expected]


@pytest.mark.parametrize(
    ("reason", "deleted"),
    [(BEFORE_SYNC, False), (BEFORE_DELETE, True), (BEFORE_RESTORE, False)],
)
def test_snapshot_before_a_delete_is_marked_deleted(root, reason, deleted):
    """Only a snapshot taken before a delete says the playlist was deleted."""
    path = save(SnapshotStore(root, RUN), "spotify", "Mix", ["a"], reason=reason)
    assert json.loads(path.read_text(encoding="utf-8"))["deleted"] is deleted
    assert (read_snapshot(path).deleted, read_snapshot(path).reason) == (deleted, reason)


@pytest.mark.parametrize(
    ("playlist", "expected"),
    [
        (SimpleNamespace(id=5, name="Spotify name"), "Spotify name"),
        (SimpleNamespace(id=5, title="SoundCloud title"), "SoundCloud title"),
        (SimpleNamespace(id=5, name="", title="Fallback"), "Fallback"),
        (SimpleNamespace(id=5), "5"),
    ],
)
def test_playlist_name_is_read_from_either_platform(playlist, expected):
    """A Spotify playlist has a name, a SoundCloud one a title, and both are found."""
    assert snapshots.name_of(playlist) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Discogs - Electro", "Discogs - Electro"),
        ("AC/DC: Live?", "AC_DC_ Live_"),
        ("../../etc/passwd", "_.._etc_passwd"),
        ("Türkçe Şarkılar 90'lar", "Türkçe Şarkılar 90_lar"),
        ("  padded.  ", "padded"),
        ("", "playlist"),
        ("...", "playlist"),
        ("x" * 300, "x" * 100),
    ],
)
def test_playlist_name_becomes_a_safe_file_name(name, expected):
    """Characters a disk could refuse are replaced and nothing escapes the folder."""
    assert safe_file_stem(name) == expected


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
def test_a_playlist_is_saved_once_per_run(root, platform):
    """The first state saved in a run stays, a later one of the same playlist is not written."""
    store = SnapshotStore(root, RUN)
    first = save(store, platform, "Mix", ["1", "2"])
    second = save(store, platform, "Mix", ["3"])
    assert first == second
    assert store.written == [first]
    assert read_snapshot(first).track_ids == ["1", "2"]


@pytest.mark.parametrize(
    ("names", "files"),
    [
        (["A/B", "A:B"], ["A_B-2.json", "A_B.json"]),
        (["A/B", "A:B", "A?B"], ["A_B-2.json", "A_B-3.json", "A_B.json"]),
        (["Mix", "Other"], ["Mix.json", "Other.json"]),
    ],
)
def test_names_that_collide_get_their_own_files(root, names, files):
    """Two playlists whose names clean up to the same file name are both kept."""
    store = SnapshotStore(root, RUN)
    for name in names:
        save(store, "spotify", name, [name])
    assert sorted(path.name for path in (root / RUN / "spotify").iterdir()) == files
    assert sorted(snapshot.playlist_name for snapshot in load_run(root, RUN)) == sorted(names)


@pytest.mark.parametrize("stores", [2, 3])
def test_an_existing_snapshot_is_never_replaced(root, stores):
    """Runs that share an id write next to each other and the earliest state wins."""
    for index in range(stores):
        save(SnapshotStore(root, RUN), "spotify", "Mix", [f"state{index}"])
    assert len(list((root / RUN / "spotify").iterdir())) == stores
    assert [snapshot.track_ids for snapshot in load_run(root, RUN)] == [["state0"]]


def break_replace(monkeypatch):
    """Makes moving the finished file into place fail.

    Args:
        monkeypatch: Pytest monkeypatch.
    """

    def refuse(source, target):
        """Stands in for a disk that refuses the move.

        Args:
            source: File to move.
            target: Where it should go.
        """
        raise OSError("disk full")

    monkeypatch.setattr(snapshots.os, "replace", refuse)


def break_dump(monkeypatch):
    """Makes turning the snapshot into JSON fail.

    Args:
        monkeypatch: Pytest monkeypatch.
    """

    def refuse(*args, **kwargs):
        """Stands in for a value JSON cannot hold.

        Args:
            *args: Ignored.
            **kwargs: Ignored.
        """
        raise TypeError("not serializable")

    monkeypatch.setattr(snapshots.json, "dump", refuse)


def break_read_back(monkeypatch):
    """Makes the file read back differ from what was saved.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    monkeypatch.setattr(snapshots, "read_snapshot", lambda path: None)


def break_nothing(monkeypatch):
    """Leaves writing intact, for the case that fails through the folder.

    Args:
        monkeypatch: Pytest monkeypatch.
    """


@pytest.mark.parametrize(
    ("breaker", "blocked", "detail"),
    [
        (break_nothing, True, ""),
        (break_replace, False, "disk full"),
        (break_dump, False, "not serializable"),
        (break_read_back, False, "differs from what was saved"),
    ],
)
def test_a_snapshot_that_cannot_be_written_raises(root, blocked_root, monkeypatch, breaker, blocked, detail):
    """Every way a write can fail ends in one clear error and no half-written file."""
    breaker(monkeypatch)
    store = SnapshotStore(blocked_root if blocked else root, RUN)
    with pytest.raises(SnapshotError) as caught:
        save(store, "spotify", "Discogs - Electro", ["a"])
    message = str(caught.value)
    assert "Discogs - Electro" in message
    assert "The playlist was left unchanged" in message
    assert detail in message
    assert store.written == []
    assert store.hint is None
    assert not list(root.rglob("*.tmp"))


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        ([], RUN),
        ([RUN], f"{RUN}-2"),
        ([RUN, f"{RUN}-2"], f"{RUN}-3"),
        (["20261110-143204"], RUN),
    ],
)
def test_run_id_is_the_start_time_and_never_reused(root, existing, expected):
    """A run is named after the second it started, with a counter when that name is taken."""
    for run_id in existing:
        (root / run_id).mkdir(parents=True)
    assert new_run_id(root, datetime(2026, 11, 10, 14, 32, 5)) == expected


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([], []),
        (["20260101-000000", "20261110-143205", "20260615-120000"], ["20261110-143205", "20260615-120000", "20260101-000000"]),
        (["20260101-000000", "20260101-000000-2", "20260101-000000-10"], ["20260101-000000-10", "20260101-000000-2", "20260101-000000"]),
        (["notes", "20260101-000000", "2026", ".hidden", "20260101-000000.bak"], ["20260101-000000"]),
    ],
)
def test_runs_are_listed_newest_first_and_other_folders_ignored(root, entries, expected):
    """Only folders named like a run id count, and they sort by age."""
    for entry in entries:
        (root / entry).mkdir(parents=True)
    assert run_ids(root) == expected


@pytest.mark.parametrize(("shared", "same"), [(True, True), (False, False)])
def test_stores_of_one_shared_run_use_one_id(root, shared, same):
    """Inside a shared run both platforms save under one id, outside each store gets its own."""

    def open_two():
        """Opens two stores one after the other, the first one saving a playlist.

        Returns:
            The run ids of both stores.
        """
        first = SnapshotStore(root)
        save(first, "spotify", "Mix", ["a"])
        return first.run_id, SnapshotStore(root).run_id

    if shared:
        with shared_run():
            first, second = open_two()
    else:
        first, second = open_two()
    assert (first == second) is same
    assert SnapshotStore(root).run_id != first


@pytest.mark.parametrize(
    ("existing", "keep", "remaining"),
    [
        (35, 30, 30),
        (30, 30, 30),
        (31, 30, 30),
        (3, 1, 1),
        (0, 30, 0),
        (5, 0, 1),
    ],
)
def test_pruning_keeps_only_the_newest_runs(root, existing, keep, remaining):
    """Runs beyond the newest ones kept are deleted, oldest first."""
    runs = [f"202601{day:02d}-000000" for day in range(1, existing + 1)]
    fill(root, runs)
    removed = prune_runs(root, keep)
    assert run_ids(root) == sorted(runs, reverse=True)[:remaining]
    assert sorted(removed) == sorted(runs)[: existing - remaining]


@pytest.mark.parametrize("keep", [1, 2])
def test_pruning_touches_nothing_but_run_folders(root, keep):
    """A stray folder or file in the snapshot folder survives pruning."""
    fill(root, ["20260101-000000", "20260102-000000", "20260103-000000"])
    (root / "notes").mkdir()
    (root / "notes" / "keep.txt").write_text("mine")
    (root / "20260101-000000.txt").write_text("mine")
    prune_runs(root, keep)
    assert (root / "notes" / "keep.txt").read_text() == "mine"
    assert (root / "20260101-000000.txt").read_text() == "mine"
    assert len(run_ids(root)) == keep


@pytest.mark.parametrize(
    ("run_id", "survives"),
    [("20260101-000000", True), ("20260102-000000", True), ("20260105-000000", True)],
)
def test_a_store_never_prunes_its_own_run(root, run_id, survives):
    """Even the oldest run is kept while it is the one being written."""
    fill(root, ["20260101-000000", "20260102-000000", "20260103-000000", "20260104-000000"])
    SnapshotStore(root, run_id, runs_kept=1).prune()
    assert ((root / run_id).exists() or run_id == "20260105-000000") is survives
    assert run_ids(root)[0] == "20260104-000000"


@pytest.mark.parametrize("failing", ["20260101-000000", "20260102-000000"])
def test_a_run_that_cannot_be_deleted_does_not_stop_pruning(root, monkeypatch, failing):
    """A folder the disk refuses to delete is skipped and the others still go."""
    fill(root, ["20260101-000000", "20260102-000000", "20260103-000000"])
    real = snapshots.shutil.rmtree

    def remove(path):
        """Deletes a folder unless it is the one that fails.

        Args:
            path: Folder to delete.
        """
        if path.name == failing:
            raise OSError("in use")
        real(path)

    monkeypatch.setattr(snapshots.shutil, "rmtree", remove)
    removed = prune_runs(root, 1)
    assert removed == [run_id for run_id in ("20260102-000000", "20260101-000000") if run_id != failing]
    assert run_ids(root) == sorted(["20260103-000000", failing], reverse=True)


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ([], []),
        (
            [("20260101-000000", "spotify", "A", ["1", "2"], BEFORE_SYNC)],
            [RunSummary("20260101-000000", "spotify", 1, 2)],
        ),
        (
            [
                ("20260101-000000", "spotify", "A", ["1", "2"], BEFORE_SYNC),
                ("20260102-000000", "soundcloud", "B", ["3"], BEFORE_DELETE),
                ("20260102-000000", "soundcloud", "C", ["4", "5"], BEFORE_SYNC),
                ("20260102-000000", "spotify", "A", [], BEFORE_SYNC),
                ("20260103-000000", "spotify", "A", ["1"], BEFORE_RESTORE),
            ],
            [
                RunSummary("20260103-000000", "spotify", 1, 1, before_restore=True),
                RunSummary("20260102-000000", "soundcloud", 2, 3, deleted=1),
                RunSummary("20260102-000000", "spotify", 1, 0),
                RunSummary("20260101-000000", "spotify", 1, 2),
            ],
        ),
    ],
)
def test_runs_are_summarised_per_platform_newest_first(root, saved, expected):
    """The listing counts playlists and tracks of each run and platform."""
    for run_id, platform, name, ids, reason in saved:
        save(SnapshotStore(root, run_id), platform, name, ids, reason=reason)
    assert list_runs(root) == expected


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"platform": "spotify"}', '{"tracks": 5}'])
def test_a_damaged_file_is_counted_in_the_listing_and_refused_by_a_restore(root, content):
    """A file that is not a snapshot shows up as unreadable and stops the run from loading."""
    save(SnapshotStore(root, RUN), "spotify", "Good", ["a"])
    damaged = root / RUN / "spotify" / "Bad.json"
    damaged.write_text(content)
    assert list_runs(root) == [RunSummary(RUN, "spotify", 1, 1, unreadable=1)]
    with pytest.raises(SnapshotError, match="cannot be read"):
        load_run(root, RUN)


@pytest.mark.parametrize(
    ("run_id", "message"),
    [
        ("20200101-000000", "There is no snapshot run '20200101-000000'"),
        ("../elsewhere", "There is no snapshot run"),
        ("", "There is no snapshot run"),
        ("notes", "There is no snapshot run 'notes'"),
        ("20260101-000000", "holds no playlists"),
    ],
)
def test_a_run_that_cannot_be_loaded_says_why(root, tmp_path, run_id, message):
    """An unknown, empty or made-up run id is refused with a message."""
    (root / "20260101-000000").mkdir(parents=True)
    (root / "notes").mkdir()
    (tmp_path / "elsewhere").mkdir()
    with pytest.raises(SnapshotError, match=message):
        load_run(root, run_id)


@pytest.mark.parametrize(
    ("platforms", "playlist", "expected"),
    [
        (["spotify", "soundcloud"], None, ["A", "B", "A"]),
        (["spotify"], None, ["A", "B"]),
        (["soundcloud"], None, ["A"]),
        (["spotify", "soundcloud"], "A", ["A", "A"]),
        (["spotify"], "b", ["B"]),
        (["soundcloud"], "B", None),
        (["spotify"], "Missing", None),
    ],
)
def test_snapshots_are_narrowed_by_platform_and_playlist(root, platforms, playlist, expected):
    """Filters keep the matching playlists, and a filter that matches nothing is an error."""
    store = SnapshotStore(root, RUN)
    for platform, name in (("spotify", "A"), ("spotify", "B"), ("soundcloud", "A")):
        save(store, platform, name, ["1"])
    loaded = load_run(root, RUN)
    if expected is None:
        with pytest.raises(SnapshotError, match="holds no snapshot"):
            select_snapshots(loaded, platforms, playlist)
    else:
        assert [snapshot.playlist_name for snapshot in select_snapshots(loaded, platforms, playlist)] == expected


def mode_of(path) -> int:
    """Reads the permission bits of a folder or file.

    Args:
        path: Folder or file.

    Returns:
        The bits for owner, group and others.
    """
    return path.stat().st_mode & 0o777


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
@pytest.mark.parametrize("existing_mode", [None, 0o755, 0o777])
def test_snapshots_are_for_their_owner_only(root, platform, existing_mode):
    """Folders are 0700 and files 0600, and a snapshot folder that was open before is closed."""
    if existing_mode is not None:
        (root / RUN / platform).mkdir(parents=True)
        for folder in (root, root / RUN, root / RUN / platform):
            folder.chmod(existing_mode)
    path = save(SnapshotStore(root, RUN), platform, "Mix", ["1", "2"])
    assert [mode_of(folder) for folder in (root, root / RUN, root / RUN / platform)] == [0o700] * 3
    assert mode_of(path) == 0o600
    assert read_snapshot(path).track_ids == ["1", "2"]


@pytest.mark.parametrize("error", [PermissionError("not permitted"), OSError("no permissions on this disk")])
def test_a_disk_without_permissions_still_gets_its_snapshot(root, monkeypatch, error):
    """When the permissions cannot be set, the snapshot is written anyway."""

    def refuse(path, mode):
        """Stands in for a disk that cannot set permissions.

        Args:
            path: Folder or file.
            mode: Permission bits.
        """
        raise error

    monkeypatch.setattr(snapshots.Path, "chmod", refuse)
    path = save(SnapshotStore(root, RUN), "spotify", "Mix", ["1"])
    assert read_snapshot(path).track_ids == ["1"]


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        (SimpleNamespace(id="a", name="Bro\ud800ken", artist="Fine"), {"id": "a", "title": "Bro?ken", "artist": "Fine"}),
        (SimpleNamespace(id="a", name="Fine", artist="\udfffB"), {"id": "a", "title": "Fine", "artist": "?B"}),
        (SimpleNamespace(id=7, title="\ud83d", artist="\ude00"), {"id": "7", "title": "?", "artist": "?"}),
        (SimpleNamespace(id="a\udc80", name="Glue"), {"id": "a?", "title": "Glue"}),
        (SimpleNamespace(id="a", name="Emoji \U0001f600 stays"), {"id": "a", "title": "Emoji \U0001f600 stays"}),
    ],
)
def test_text_that_cannot_be_encoded_is_replaced_not_fatal(root, track, expected):
    """Half of a surrogate pair in a title, artist or id becomes a question mark and the snapshot is written."""
    playlist = SimpleNamespace(id="p", name="Mix")
    path = SnapshotStore(root, RUN).save("spotify", playlist, [track])
    assert json.loads(path.read_text(encoding="utf-8"))["tracks"] == [expected]


@pytest.mark.parametrize(
    ("name", "saved_name", "file_name"),
    [
        ("Mix \ud800", "Mix ?", "Mix _.json"),
        ("\udfff", "?", "_.json"),
        ("Plain", "Plain", "Plain.json"),
    ],
)
def test_a_playlist_name_that_cannot_be_encoded_is_replaced(root, name, saved_name, file_name):
    """A playlist name holding half of a surrogate pair is saved with a question mark in its place."""
    path = SnapshotStore(root, RUN).save("spotify", SimpleNamespace(id="p", name=name), [])
    assert path.name == file_name
    assert read_snapshot(path).playlist_name == saved_name
