"""Saves what a playlist held before it is changed and puts it back on request.

Every run that changes or deletes a playlist writes one JSON file per playlist
to <data dir>/snapshots/<run id>/<platform>/<playlist name>.json before the
change is made. A run id is the local time the run started, so run folders sort
by age.
"""

import json
import logging
import os
import re
import shutil
from collections import Counter
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

RUN_ID_FORMAT = "%Y%m%d-%H%M%S"
RUN_ID_PATTERN = re.compile(r"^(\d{8}-\d{6})(?:-(\d+))?$")
DEFAULT_RUNS_KEPT = 30
MAX_FILE_STEM = 100
FORMAT_VERSION = 1
BEFORE_SYNC = "sync"
BEFORE_DELETE = "delete"
BEFORE_RESTORE = "restore"
SPOTIFY = "spotify"

_SHARED_RUN: ContextVar[dict[str, str] | None] = ContextVar("shared_snapshot_run", default=None)


class SnapshotError(RuntimeError):
    """A snapshot could not be written or read, so the playlist was left alone."""


@dataclass(frozen=True)
class SavedTrack:
    """One track of a saved playlist.

    Args:
        id: Track id on the platform.
        title: Track title, when the platform gave one.
        artist: Artist name, when the platform gave one.
    """

    id: str
    title: str | None = None
    artist: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """What one playlist held at one moment.

    Args:
        run_id: Run the snapshot belongs to.
        platform: spotify or soundcloud.
        playlist_id: Playlist id on the platform.
        playlist_name: Playlist name on the platform.
        saved_at: Local time the snapshot was taken, in ISO format.
        tracks: Tracks in playlist order.
        reason: What was about to happen: sync, delete or restore.
        public: Whether the playlist was public, when the platform said so.
    """

    run_id: str
    platform: str
    playlist_id: str
    playlist_name: str
    saved_at: str
    tracks: tuple[SavedTrack, ...]
    reason: str = BEFORE_SYNC
    public: bool | None = None

    @property
    def deleted(self) -> bool:
        """Tells whether the playlist was deleted right after the snapshot.

        Returns:
            True for a snapshot taken before a delete.
        """
        return self.reason == BEFORE_DELETE

    @property
    def track_ids(self) -> list[str]:
        """Lists the saved track ids.

        Returns:
            Ids in playlist order.
        """
        return [track.id for track in self.tracks]


@dataclass(frozen=True)
class RunSummary:
    """The snapshots one run saved for one platform.

    Args:
        run_id: Run id.
        platform: spotify or soundcloud.
        playlists: Number of playlists saved.
        tracks: Number of tracks saved across those playlists.
        deleted: Playlists that were saved because they were about to be deleted.
        before_restore: Whether the run was a restore saving what it replaced.
        unreadable: Snapshot files that could not be read.
    """

    run_id: str
    platform: str
    playlists: int
    tracks: int
    deleted: int = 0
    before_restore: bool = False
    unreadable: int = 0


@dataclass
class RestoreStep:
    """What putting one playlist back involves.

    Args:
        snapshot: The saved playlist.
        playlist: The playlist on the platform, or None when it has to be created.
        current: Tracks the playlist holds now, as the platform returned them.
        keep: Number of leading tracks that already match and stay untouched.
        remove: Track ids to take out before the saved tail is added.
        add: Track ids to add after the kept tracks, in order.
        put_back: Saved tracks the playlist lacks now.
        taken_out: Tracks in the playlist now that the snapshot does not hold.
    """

    snapshot: Snapshot
    playlist: object | None
    current: list
    keep: int
    remove: list[str]
    add: list[str]
    put_back: list[SavedTrack]
    taken_out: list[SavedTrack]

    @property
    def changes(self) -> bool:
        """Tells whether restoring would change anything.

        Returns:
            True when the playlist is missing or differs from the snapshot.
        """
        return self.playlist is None or bool(self.remove or self.add)

    @property
    def action(self) -> str:
        """Names what restoring does to the playlist.

        Returns:
            recreate, restore, reorder or unchanged.
        """
        if self.playlist is None:
            return "recreate"
        if not self.changes:
            return "unchanged"
        return "restore" if self.put_back or self.taken_out else "reorder"


@dataclass(frozen=True)
class RestoreOutcome:
    """How a playlist compares with its snapshot after a restore.

    Args:
        lost: Saved tracks the platform did not take back.
        left_over: Tracks still in the playlist that the snapshot does not hold.
        in_order: Whether the playlist holds exactly the saved tracks in the saved order.
    """

    lost: list[SavedTrack] = field(default_factory=list)
    left_over: list[SavedTrack] = field(default_factory=list)
    in_order: bool = True

    @property
    def problems(self) -> list[str]:
        """Words what did not go back as saved.

        Returns:
            One sentence per difference, empty when the playlist is exactly as saved.
        """
        found = []
        if self.lost:
            found.append(
                f"{len(self.lost)} saved tracks did not come back, "
                "most likely because the platform no longer has them."
            )
        if self.left_over:
            found.append(f"{len(self.left_over)} tracks that were not in the snapshot are still there.")
        if not found and not self.in_order:
            found.append("The saved tracks are back, but not in the saved order.")
        return found


def name_of(playlist) -> str:
    """Reads the name of a playlist of either platform.

    Args:
        playlist: A Spotify playlist, which has a name, or a SoundCloud one, which has a title.

    Returns:
        The name, or the id when the playlist carries neither.
    """
    for attribute in ("name", "title"):
        value = getattr(playlist, attribute, None)
        if isinstance(value, str) and value:
            return value
    return str(playlist.id)


def _visibility(playlist) -> bool | None:
    """Reads whether a playlist of either platform is public.

    Args:
        playlist: A Spotify or SoundCloud playlist.

    Returns:
        True or False, or None when the playlist does not say.
    """
    for attribute in ("public", "is_public"):
        value = getattr(playlist, attribute, None)
        if isinstance(value, bool):
            return value
    return None


def _text(track, *attributes: str) -> str | None:
    """Reads the first text attribute a fetched track carries.

    Args:
        track: A Spotify or SoundCloud track.
        *attributes: Attribute names to try in order.

    Returns:
        The text, or None when the track carries none of them.
    """
    for attribute in attributes:
        value = getattr(track, attribute, None)
        if isinstance(value, str) and value:
            return value
    return None


def saved_track(track) -> SavedTrack:
    """Turns a fetched track into the form that is saved.

    Args:
        track: A Spotify or SoundCloud track.

    Returns:
        The id with the title and artist when the track carries them.
    """
    return SavedTrack(id=str(track.id), title=_text(track, "name", "title"), artist=_text(track, "artist"))


def safe_file_stem(name: str) -> str:
    """Turns a playlist name into a file name that works on every disk.

    Args:
        name: Playlist name.

    Returns:
        The name with letters, digits, spaces, dashes, underscores and dots kept
        and everything else replaced by an underscore.
    """
    cleaned = "".join(char if char.isalnum() or char in " -_." else "_" for char in name)
    return cleaned.strip(" .")[:MAX_FILE_STEM].strip(" .") or "playlist"


def _run_order(run_id: str) -> tuple[str, int]:
    """Builds the sort key of a run id.

    Args:
        run_id: Run id, with or without a counter.

    Returns:
        The timestamp and the counter.
    """
    found = RUN_ID_PATTERN.match(run_id)
    return found[1], int(found[2] or 1)


def run_ids(root: Path) -> list[str]:
    """Lists the runs that have snapshots.

    Args:
        root: Folder holding one folder per run.

    Returns:
        Run ids, newest first. Anything else in the folder is ignored.
    """
    if not root.is_dir():
        return []
    found = [entry.name for entry in root.iterdir() if entry.is_dir() and RUN_ID_PATTERN.match(entry.name)]
    return sorted(found, key=_run_order, reverse=True)


def new_run_id(root: Path, now: datetime | None = None) -> str:
    """Names a new run after the current local time.

    Args:
        root: Folder holding one folder per run.
        now: Time to use instead of the current one.

    Returns:
        A run id no existing run has, with a counter added when two runs
        started in the same second.
    """
    stamp = (now or datetime.now()).strftime(RUN_ID_FORMAT)
    run_id, attempt = stamp, 1
    while (root / run_id).exists():
        attempt += 1
        run_id = f"{stamp}-{attempt}"
    return run_id


@contextmanager
def shared_run() -> Iterator[None]:
    """Makes every snapshot store opened inside the block save under one run id.

    Syncing both platforms builds one store per platform, and both belong to
    the same run.

    Yields:
        Nothing, the work that shares a run id runs inside the with block.
    """
    token = _SHARED_RUN.set({})
    try:
        yield
    finally:
        _SHARED_RUN.reset(token)


def _resolve_run_id(root: Path) -> str:
    """Picks the run id of a store that was not given one.

    Args:
        root: Folder holding one folder per run.

    Returns:
        The id shared by the surrounding run, or a new one.
    """
    shared = _SHARED_RUN.get()
    if shared is None:
        return new_run_id(root)
    if str(root) not in shared:
        shared[str(root)] = new_run_id(root)
    return shared[str(root)]


def restore_hint(run_dir: Path) -> str:
    """Words the line that tells where a run saved its snapshots.

    Args:
        run_dir: Folder of the run.

    Returns:
        One sentence naming the folder and the command that undoes the run.
    """
    return (
        "Playlists were saved before this run changed them. "
        f"Undo with: song-automations restore {run_dir.name} (snapshot in {run_dir})"
    )


def _to_json(snapshot: Snapshot) -> dict:
    """Lays a snapshot out for its file.

    Args:
        snapshot: The snapshot.

    Returns:
        A dictionary of plain values.
    """
    tracks = []
    for track in snapshot.tracks:
        row = {"id": track.id}
        if track.title is not None:
            row["title"] = track.title
        if track.artist is not None:
            row["artist"] = track.artist
        tracks.append(row)
    return {
        "version": FORMAT_VERSION,
        "run_id": snapshot.run_id,
        "platform": snapshot.platform,
        "playlist_id": snapshot.playlist_id,
        "playlist_name": snapshot.playlist_name,
        "public": snapshot.public,
        "saved_at": snapshot.saved_at,
        "reason": snapshot.reason,
        "deleted": snapshot.deleted,
        "track_count": len(tracks),
        "tracks": tracks,
    }


def read_snapshot(path: Path) -> Snapshot:
    """Reads one snapshot file.

    Args:
        path: The file.

    Returns:
        The snapshot.

    Raises:
        SnapshotError: When the file is missing, damaged or not a snapshot.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        reason = BEFORE_DELETE if data.get("deleted") else data.get("reason", BEFORE_SYNC)
        return Snapshot(
            run_id=str(data["run_id"]),
            platform=str(data["platform"]),
            playlist_id=str(data["playlist_id"]),
            playlist_name=str(data["playlist_name"]),
            saved_at=str(data["saved_at"]),
            tracks=tuple(
                SavedTrack(id=str(row["id"]), title=row.get("title"), artist=row.get("artist"))
                for row in data["tracks"]
            ),
            reason=reason,
            public=data.get("public"),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        detail = str(error) or type(error).__name__
        raise SnapshotError(f"The snapshot {path} cannot be read ({detail}).") from error


def _snapshot_files(run_dir: Path) -> list[Path]:
    """Lists the snapshot files of a run.

    Args:
        run_dir: Folder of the run.

    Returns:
        Files of every platform, sorted by path.
    """
    return sorted(run_dir.glob("*/*.json"))


def load_run(root: Path, run_id: str) -> list[Snapshot]:
    """Reads every snapshot of a run.

    Args:
        root: Folder holding one folder per run.
        run_id: Run to read.

    Returns:
        One snapshot per playlist, in the order they were saved. When a playlist
        was saved more than once, the earliest state is the one returned.

    Raises:
        SnapshotError: When the run does not exist, is empty or holds a damaged file.
    """
    if not RUN_ID_PATTERN.match(run_id) or not (root / run_id).is_dir():
        raise SnapshotError(
            f"There is no snapshot run '{run_id}'. "
            "Run 'song-automations snapshots' to list the saved runs."
        )
    snapshots = [read_snapshot(path) for path in _snapshot_files(root / run_id)]
    if not snapshots:
        raise SnapshotError(f"The snapshot run '{run_id}' holds no playlists.")
    earliest: dict[tuple[str, str], Snapshot] = {}
    for snapshot in sorted(snapshots, key=lambda saved: saved.saved_at):
        earliest.setdefault((snapshot.platform, snapshot.playlist_id), snapshot)
    return list(earliest.values())


def select_snapshots(
    snapshots: list[Snapshot],
    platforms: Collection[str],
    playlist: str | None = None,
) -> list[Snapshot]:
    """Narrows the snapshots of a run to the ones asked for.

    Args:
        snapshots: Snapshots of one run.
        platforms: Platforms to keep.
        playlist: Playlist name to keep, compared without regard to case, or None for all.

    Returns:
        The matching snapshots.

    Raises:
        SnapshotError: When nothing matches.
    """
    chosen = [
        snapshot
        for snapshot in snapshots
        if snapshot.platform in platforms
        and (playlist is None or snapshot.playlist_name.casefold() == playlist.casefold())
    ]
    if not chosen:
        wanted = f"the playlist '{playlist}'" if playlist else "any playlist"
        raise SnapshotError(
            f"The run holds no snapshot of {wanted} on {' or '.join(platforms)}. "
            "Use --dry-run without filters to see what it holds."
        )
    return chosen


def list_runs(root: Path) -> list[RunSummary]:
    """Summarises the saved runs.

    Args:
        root: Folder holding one folder per run.

    Returns:
        One summary per run and platform, newest run first.
    """
    summaries = []
    for run_id in run_ids(root):
        by_platform: dict[str, list[Snapshot | None]] = {}
        for path in _snapshot_files(root / run_id):
            try:
                snapshot = read_snapshot(path)
            except SnapshotError:
                snapshot = None
            by_platform.setdefault(path.parent.name, []).append(snapshot)
        for platform, found in sorted(by_platform.items()):
            readable = [snapshot for snapshot in found if snapshot is not None]
            summaries.append(
                RunSummary(
                    run_id=run_id,
                    platform=platform,
                    playlists=len(readable),
                    tracks=sum(len(snapshot.tracks) for snapshot in readable),
                    deleted=sum(snapshot.deleted for snapshot in readable),
                    before_restore=any(snapshot.reason == BEFORE_RESTORE for snapshot in readable),
                    unreadable=len(found) - len(readable),
                )
            )
    return summaries


def prune_runs(root: Path, keep: int, protect: str | None = None) -> list[str]:
    """Deletes the snapshots of all but the newest runs.

    Only folders named like a run id are touched. A folder that cannot be
    listed or deleted is logged and left in place, because old snapshots that
    stay around are no reason to stop a sync.

    Args:
        root: Folder holding one folder per run.
        keep: Number of newest runs to keep, at least one.
        protect: Run id that is never deleted.

    Returns:
        The run ids that were deleted.
    """
    try:
        outdated = run_ids(root)[max(keep, 1) :]
    except OSError:
        logger.warning("Could not list the snapshot runs in %s", root, exc_info=True)
        return []
    removed = []
    for run_id in outdated:
        if run_id == protect:
            continue
        try:
            shutil.rmtree(root / run_id)
        except OSError:
            logger.warning("Could not delete the old snapshot run %s", root / run_id, exc_info=True)
            continue
        removed.append(run_id)
    return removed


class SnapshotStore:
    """Writes the snapshots of one run.

    Args:
        root: Folder holding one folder per run.
        run_id: Run to write under. Without one the store uses the id of the
            surrounding shared run, or names a new run after the current time.
        runs_kept: Number of newest runs that pruning keeps.
    """

    def __init__(self, root: Path, run_id: str | None = None, runs_kept: int = DEFAULT_RUNS_KEPT) -> None:
        self.root = Path(root)
        self.run_id = run_id or _resolve_run_id(self.root)
        self.runs_kept = runs_kept
        self.written: list[Path] = []
        self._saved: dict[tuple[str, str], Path] = {}

    @property
    def run_dir(self) -> Path:
        """Locates the folder of this run.

        Returns:
            The folder, which exists only once a snapshot was written.
        """
        return self.root / self.run_id

    @property
    def hint(self) -> str | None:
        """Words where this run saved its snapshots.

        Returns:
            The sentence, or None when the run saved nothing.
        """
        return restore_hint(self.run_dir) if self.written else None

    def prune(self) -> list[str]:
        """Deletes the snapshots of runs older than the newest ones kept.

        Returns:
            The run ids that were deleted.
        """
        return prune_runs(self.root, self.runs_kept, protect=self.run_id)

    def save(self, platform: str, playlist, tracks: list, reason: str = BEFORE_SYNC) -> Path:
        """Saves what a playlist holds, once per playlist and run.

        The first state saved in a run is the one from before the run touched
        the playlist, so a second call for the same playlist writes nothing.

        Args:
            platform: spotify or soundcloud.
            playlist: The playlist as the platform client returned it.
            tracks: The tracks as the platform client returned them, in order.
            reason: What is about to happen: sync, delete or restore.

        Returns:
            The file holding the snapshot.

        Raises:
            SnapshotError: When the snapshot could not be written and read back.
                The caller must then leave the playlist unchanged.
        """
        key = (platform, str(playlist.id))
        if key in self._saved:
            return self._saved[key]
        name = name_of(playlist)
        try:
            snapshot = Snapshot(
                run_id=self.run_id,
                platform=platform,
                playlist_id=str(playlist.id),
                playlist_name=name,
                saved_at=datetime.now().isoformat(),
                tracks=tuple(saved_track(track) for track in tracks),
                reason=reason,
                public=_visibility(playlist),
            )
            path = self._write(snapshot)
        except (OSError, TypeError, ValueError, AttributeError, SnapshotError) as error:
            detail = str(error) or type(error).__name__
            raise SnapshotError(
                f"Could not save a snapshot of the {platform} playlist '{name}' under "
                f"{self.run_dir} ({detail}). The playlist was left unchanged."
            ) from error
        self._saved[key] = path
        self.written.append(path)
        return path

    def _write(self, snapshot: Snapshot) -> Path:
        """Writes a snapshot to a new file and checks that it reads back the same.

        Args:
            snapshot: The snapshot.

        Returns:
            The file, which never replaces an existing one.

        Raises:
            OSError: When the folder or the file cannot be written.
            ValueError: When the file read back differs from the snapshot.
            SnapshotError: When the file cannot be read back.
        """
        folder = self.run_dir / snapshot.platform
        folder.mkdir(parents=True, exist_ok=True)
        stem = safe_file_stem(snapshot.playlist_name)
        path, attempt = folder / f"{stem}.json", 1
        while path.exists():
            attempt += 1
            path = folder / f"{stem}-{attempt}.json"
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(_to_json(snapshot), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        if read_snapshot(path) != snapshot:
            raise ValueError("the file read back differs from what was saved")
        return path


def split_restore(current: list[str], saved: list[str]) -> tuple[int, list[str], list[str]]:
    """Works out how to turn the current track list into the saved one.

    Tracks at the start that already match stay where they are. Everything
    after them is taken out and the saved tail is added in order. Taking a
    track out removes every copy of it, so when a track of the tail also sits
    among the kept ones nothing is kept.

    Args:
        current: Track ids the playlist holds now, in order.
        saved: Track ids the snapshot holds, in order.

    Returns:
        The number of leading tracks kept, the ids to take out and the ids to add.
    """
    keep = 0
    for now, before in zip(current, saved, strict=False):
        if now != before:
            break
        keep += 1
    if set(current[keep:]) & set(current[:keep]):
        keep = 0
    return keep, list(dict.fromkeys(current[keep:])), saved[keep:]


def _lacking(wanted: Collection[SavedTrack], held: Collection[SavedTrack]) -> list[SavedTrack]:
    """Lists the tracks of one list that another list does not hold.

    Copies count: a track wanted twice and held once is lacking once.

    Args:
        wanted: Tracks that should be there.
        held: Tracks that are there.

    Returns:
        The wanted tracks without a counterpart, in their order.
    """
    available = Counter(track.id for track in held)
    lacking = []
    for track in wanted:
        if available[track.id] > 0:
            available[track.id] -= 1
        else:
            lacking.append(track)
    return lacking


def plan_restore(snapshot: Snapshot, client) -> RestoreStep:
    """Compares a snapshot with the playlist as it is now.

    Reads the playlist once and changes nothing.

    Args:
        snapshot: The saved playlist.
        client: Client of the platform the snapshot belongs to.

    Returns:
        What putting the playlist back involves.
    """
    playlist = client.find_playlist_by_name(snapshot.playlist_name)
    current = list(client.get_playlist_tracks(playlist.id)) if playlist is not None else []
    now = [saved_track(track) for track in current]
    keep, remove, add = split_restore([track.id for track in now], snapshot.track_ids)
    return RestoreStep(
        snapshot=snapshot,
        playlist=playlist,
        current=current,
        keep=keep,
        remove=remove,
        add=add,
        put_back=_lacking(snapshot.tracks, now),
        taken_out=_lacking(now, snapshot.tracks),
    )


def describe_track(track: SavedTrack) -> str:
    """Words a track for a person.

    Args:
        track: A saved track.

    Returns:
        Artist and title when known, otherwise whatever is known, at least the id.
    """
    return " - ".join(part for part in (track.artist, track.title) if part) or track.id


def change_lines(step: RestoreStep, limit: int = 10) -> list[str]:
    """Lists the tracks a restore would put back and take out.

    Args:
        step: The planned restore.
        limit: Tracks named per direction before the rest is only counted.

    Returns:
        One line per track, starting with a plus for a track that comes back
        and a minus for one that goes, and a line counting what was left out.
    """
    lines = []
    for sign, tracks in (("+", step.put_back), ("-", step.taken_out)):
        lines += [f"{sign} {describe_track(track)}" for track in tracks[:limit]]
        if len(tracks) > limit:
            lines.append(f"{sign} and {len(tracks) - limit} more")
    return lines


def apply_restore(step: RestoreStep, client, store: SnapshotStore) -> RestoreOutcome:
    """Puts one playlist back as its snapshot saved it.

    A playlist that no longer exists is created under its saved name. What the
    playlist holds now is saved first, so the restore can be undone. Spotify
    takes tracks by address and SoundCloud takes the whole list of numeric ids,
    the same way a sync writes them. SoundCloud drops tracks that are gone
    without saying so, so its playlist is read once more afterwards and
    compared with the snapshot. Spotify either takes a change or raises, so
    its playlist is not read again.

    Args:
        step: The planned restore.
        client: Client of the platform the snapshot belongs to.
        store: Store that saves the current state before it is replaced.

    Returns:
        How the playlist differs from the snapshot afterwards, if at all.

    Raises:
        SnapshotError: When the current state could not be saved. The playlist
            is then left unchanged.
    """
    snapshot = step.snapshot
    playlist = step.playlist
    if playlist is None:
        playlist = client.create_playlist(
            name=snapshot.playlist_name,
            description=f"Restored from the snapshot of {snapshot.run_id}",
            public=snapshot.public is not False,
        )
    store.save(snapshot.platform, playlist, step.current, reason=BEFORE_RESTORE)
    if snapshot.platform == SPOTIFY:
        uri = "spotify:track:{}".format
        if step.remove:
            client.remove_tracks_from_playlist(playlist.id, [uri(track_id) for track_id in step.remove])
        if step.add:
            client.add_tracks_to_playlist(playlist.id, [uri(track_id) for track_id in step.add])
        return RestoreOutcome()
    client.set_playlist_tracks(playlist.id, [int(track_id) for track_id in snapshot.track_ids])
    held = [saved_track(track) for track in client.get_playlist_tracks(playlist.id)]
    return RestoreOutcome(
        lost=_lacking(snapshot.tracks, held),
        left_over=_lacking(held, snapshot.tracks),
        in_order=[track.id for track in held] == snapshot.track_ids,
    )
