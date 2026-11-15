"""Packs what the tool remembers into one file, so another machine can carry on.

A machine that starts empty, such as a cloud agent, would search for every
track again and run out of Spotify searches long before it finished. The
bundle holds what a run needs to pick up where the last one stopped: the
matches and playlist ids, the count of recent Spotify searches and the
playlist snapshots that make a run undoable. It holds no login.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from song_automations.clients.spotify import SEARCH_LEDGER_FILE
from song_automations.config import Settings

DATABASE_MEMBER = "state.db"
LEDGER_MEMBER = SEARCH_LEDGER_FILE
SNAPSHOTS_MEMBER = "snapshots"
DISPOSABLE_TABLES = ("search_cache",)
SIDECAR_SUFFIXES = ("-wal", "-shm")
ASIDE_STAMP = "%Y%m%d-%H%M%S"


class StateBundleError(RuntimeError):
    """Raised when a bundle cannot be written or is not one this tool wrote."""


@dataclass(frozen=True)
class ExportedState:
    """What an export wrote.

    Attributes:
        path: The bundle file.
        size_bytes: Its size.
        snapshot_runs: Runs whose playlist snapshots are inside.
    """

    path: Path
    size_bytes: int
    snapshot_runs: int


@dataclass(frozen=True)
class ImportedState:
    """What an import changed.

    Attributes:
        set_aside: Where the state that was there before went, None when there was none.
        snapshot_runs_added: Snapshot runs this machine did not have yet.
    """

    set_aside: Path | None
    snapshot_runs_added: int


def export_state(settings: Settings, target: Path) -> ExportedState:
    """Writes the state a run on another machine needs into one file.

    The cache of raw search answers is left out: it is most of the database
    and anything in it can be asked for again.

    Args:
        settings: Where the state lives.
        target: The bundle file to write.

    Returns:
        What was written.

    Raises:
        StateBundleError: When there is no state to export.
    """
    if not settings.db_path.is_file():
        raise StateBundleError(f"There is no state to export at {settings.db_path}.")
    ledger = settings.data_dir / LEDGER_MEMBER
    snapshots = settings.snapshots_dir
    with tempfile.TemporaryDirectory() as scratch:
        slim = Path(scratch) / DATABASE_MEMBER
        _copy_without_caches(settings.db_path, slim)
        with tarfile.open(target, "w:gz") as archive:
            archive.add(slim, arcname=DATABASE_MEMBER)
            if ledger.is_file():
                archive.add(ledger, arcname=LEDGER_MEMBER)
            if snapshots.is_dir():
                archive.add(snapshots, arcname=SNAPSHOTS_MEMBER)
    return ExportedState(
        path=target,
        size_bytes=target.stat().st_size,
        snapshot_runs=len(_run_directories(snapshots)),
    )


def import_state(settings: Settings, source: Path) -> ImportedState:
    """Replaces this machine's state with the state in a bundle.

    Nothing is deleted. The database that was here is renamed, the count of
    Spotify searches becomes the union of both machines' counts, because both
    spend the same allowance, and snapshot runs are only ever added.

    Args:
        settings: Where the state lives.
        source: The bundle file to read.

    Returns:
        What changed.

    Raises:
        StateBundleError: When the file is not a bundle this tool wrote.
    """
    with tempfile.TemporaryDirectory() as scratch:
        unpacked = Path(scratch)
        _unpack(source, unpacked)
        database = unpacked / DATABASE_MEMBER
        if not database.is_file():
            raise StateBundleError(f"{source} holds no {DATABASE_MEMBER}, so it is not a state bundle.")
        settings.ensure_directories()
        set_aside = _set_aside(settings.db_path)
        shutil.move(str(database), settings.db_path)
        _merge_ledger(unpacked / LEDGER_MEMBER, settings.data_dir / LEDGER_MEMBER)
        added = _add_snapshot_runs(unpacked / SNAPSHOTS_MEMBER, settings.snapshots_dir)
    return ImportedState(set_aside=set_aside, snapshot_runs_added=added)


def _copy_without_caches(source: Path, target: Path) -> None:
    """Copies the database into a single file and empties the tables that are only caches.

    Args:
        source: The live database, which is only read.
        target: The copy to write.
    """
    live = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        live.backup(copy)
        tables = {row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in DISPOSABLE_TABLES:
            if table in tables:
                copy.execute(f"DELETE FROM {table}")
        copy.commit()
        copy.execute("PRAGMA journal_mode = DELETE")
        copy.execute("VACUUM")
    finally:
        live.close()
        copy.close()


def _unpack(source: Path, target: Path) -> None:
    """Unpacks a bundle after checking it holds nothing but what an export writes.

    Args:
        source: The bundle file.
        target: The empty directory to unpack into.

    Raises:
        StateBundleError: When the file cannot be read or holds something else.
    """
    try:
        with tarfile.open(source, "r:gz") as archive:
            for member in archive.getmembers():
                if not _is_expected(member):
                    raise StateBundleError(f"{source} holds '{member.name}', which a state bundle never does.")
            archive.extractall(target, filter="data")
    except (tarfile.TarError, OSError) as error:
        raise StateBundleError(f"{source} could not be read as a state bundle: {error}") from error


def _is_expected(member: tarfile.TarInfo) -> bool:
    """Tells whether an archive member is one an export writes.

    Args:
        member: The archive member.

    Returns:
        True for the database, the search count and plain files and directories under snapshots.
    """
    if member.name in (DATABASE_MEMBER, LEDGER_MEMBER):
        return member.isfile()
    parts = Path(member.name).parts
    inside_snapshots = bool(parts) and parts[0] == SNAPSHOTS_MEMBER and ".." not in parts
    return inside_snapshots and (member.isfile() or member.isdir())


def _set_aside(database: Path) -> Path | None:
    """Renames the database that is about to be replaced, with its journal files.

    Args:
        database: The live database.

    Returns:
        The new name, or None when there was no database.
    """
    if not database.exists():
        return None
    stamp = datetime.now().strftime(ASIDE_STAMP)
    aside = database.with_name(f"{database.stem}.before-import-{stamp}{database.suffix}")
    repeat = 1
    while aside.exists():
        repeat += 1
        aside = database.with_name(f"{database.stem}.before-import-{stamp}-{repeat}{database.suffix}")
    database.replace(aside)
    for suffix in SIDECAR_SUFFIXES:
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists():
            sidecar.replace(aside.with_name(aside.name + suffix))
    return aside


def _merge_ledger(incoming: Path, local: Path) -> None:
    """Combines the Spotify search times of the bundle with the ones recorded here.

    Args:
        incoming: The search times in the bundle, which may be missing.
        local: The search times on this machine, which may be missing.
    """
    if not incoming.is_file():
        return
    times = sorted(set(_search_times(incoming)) | set(_search_times(local)))
    local.write_text(json.dumps(times), encoding="utf-8")


def _search_times(path: Path) -> list[float]:
    """Reads recorded search times, treating a missing or damaged file as none.

    Args:
        path: The file of search times.

    Returns:
        The times in it that are numbers.
    """
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(stored, list):
        return []
    return [float(moment) for moment in stored if isinstance(moment, int | float) and not isinstance(moment, bool)]


def _add_snapshot_runs(incoming: Path, local: Path) -> int:
    """Copies the snapshot runs this machine does not have yet.

    Args:
        incoming: The snapshots directory in the bundle, which may be missing.
        local: The snapshots directory on this machine.

    Returns:
        How many runs were added.
    """
    added = 0
    for run in _run_directories(incoming):
        destination = local / run.name
        if destination.exists():
            continue
        local.mkdir(parents=True, exist_ok=True)
        shutil.copytree(run, destination)
        added += 1
    return added


def _run_directories(snapshots: Path) -> list[Path]:
    """Lists the run directories under a snapshots directory.

    Args:
        snapshots: The snapshots directory, which may be missing.

    Returns:
        One directory per run, sorted by name.
    """
    if not snapshots.is_dir():
        return []
    return sorted(entry for entry in snapshots.iterdir() if entry.is_dir())
