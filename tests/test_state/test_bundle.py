"""Tests for carrying the state to another machine."""

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from song_automations.config import Settings
from song_automations.state.bundle import (
    DATABASE_MEMBER,
    LEDGER_MEMBER,
    StateBundleError,
    export_state,
    import_state,
)


def make_settings(folder: Path) -> Settings:
    """Builds settings whose data directory is a folder of the test.

    Args:
        folder: The data directory.

    Returns:
        Settings that read no .env file.
    """
    return Settings(_env_file=None, data_dir=folder)


def write_state(
    settings: Settings,
    matches: tuple[str, ...] = ("first", "second"),
    cached: int = 3,
    searches: list[float] | None = None,
    runs: tuple[str, ...] = (),
) -> None:
    """Writes a state like the one a sync leaves behind.

    Args:
        settings: Where the state goes.
        matches: Names stored as matched tracks.
        cached: Rows put in the search cache, a negative number for no such table.
        searches: Spotify search times, None for no file.
        runs: Snapshot runs, each with one playlist file.
    """
    settings.ensure_directories()
    with sqlite3.connect(settings.db_path) as database:
        database.execute("CREATE TABLE matched_tracks (name TEXT)")
        database.executemany("INSERT INTO matched_tracks VALUES (?)", [(name,) for name in matches])
        if cached >= 0:
            database.execute("CREATE TABLE search_cache (payload TEXT)")
            database.executemany("INSERT INTO search_cache VALUES (?)", [("x" * 500,)] * cached)
    if searches is not None:
        (settings.data_dir / LEDGER_MEMBER).write_text(json.dumps(searches))
    for run in runs:
        playlist = settings.snapshots_dir / run / "spotify" / "Discogs - house.json"
        playlist.parent.mkdir(parents=True, exist_ok=True)
        playlist.write_text(json.dumps({"run": run}))


def rows(database: Path, table: str) -> list[str]:
    """Reads the single column of a table.

    Args:
        database: The database file.
        table: The table to read.

    Returns:
        The values, sorted.
    """
    with sqlite3.connect(database) as connection:
        return sorted(row[0] for row in connection.execute(f"SELECT * FROM {table}"))


def archive_of(path: Path, members: dict[str, bytes | None], links: tuple[str, ...] = ()) -> Path:
    """Writes a gzipped tar with the given members.

    Args:
        path: The archive to write.
        members: Content per member name, None for a directory.
        links: Member names written as symbolic links to /etc/hosts.

    Returns:
        The archive.
    """
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        for name in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/hosts"
            archive.addfile(info)
    return path


class TestExport:
    """Tests for writing the bundle."""

    @pytest.mark.parametrize(
        "cached, searches, runs",
        [
            (3, [1.0, 2.0], ("20261110-092826",)),
            (0, None, ()),
            (-1, [5.0], ("a", "b")),
        ],
        ids=["everything", "bare database", "no cache table"],
    )
    def test_bundle_holds_the_state_without_the_cache(self, tmp_path, cached, searches, runs):
        """The matches, search times and snapshots go in; cached search answers do not."""
        source = make_settings(tmp_path / "source")
        write_state(source, cached=cached, searches=searches, runs=runs)

        exported = export_state(source, tmp_path / "bundle.tar.gz")

        unpacked = tmp_path / "unpacked"
        with tarfile.open(exported.path) as archive:
            archive.extractall(unpacked, filter="data")
        assert rows(unpacked / DATABASE_MEMBER, "matched_tracks") == ["first", "second"]
        if cached >= 0:
            assert rows(unpacked / DATABASE_MEMBER, "search_cache") == []
        assert (unpacked / LEDGER_MEMBER).exists() == (searches is not None)
        assert sorted(run.name for run in (unpacked / "snapshots").glob("*")) == sorted(runs)
        assert exported.snapshot_runs == len(runs)
        assert exported.size_bytes == exported.path.stat().st_size

    @pytest.mark.parametrize("cached", [1, 400])
    def test_live_database_is_left_alone(self, tmp_path, cached):
        """Exporting empties the cache in the copy, never in the database in use."""
        source = make_settings(tmp_path / "source")
        write_state(source, cached=cached)

        export_state(source, tmp_path / "bundle.tar.gz")

        assert len(rows(source.db_path, "search_cache")) == cached

    @pytest.mark.parametrize("make_directory", [True, False])
    def test_nothing_to_export_is_an_error(self, tmp_path, make_directory):
        """A machine that never synced has no state to hand over."""
        source = make_settings(tmp_path / "source")
        if make_directory:
            source.ensure_directories()

        with pytest.raises(StateBundleError, match="no state to export"):
            export_state(source, tmp_path / "bundle.tar.gz")


class TestImport:
    """Tests for reading the bundle on another machine."""

    @staticmethod
    def bundle(tmp_path: Path, **state) -> Path:
        """Exports a state written for the test.

        Args:
            tmp_path: Folder of the test.
            **state: Passed to write_state.

        Returns:
            The bundle file.
        """
        source = make_settings(tmp_path / "source")
        write_state(source, **state)
        return export_state(source, tmp_path / "bundle.tar.gz").path

    @pytest.mark.parametrize(
        "sidecars",
        [None, (), ("-wal",), ("-wal", "-shm")],
        ids=["empty machine", "database", "database and journal", "database, journal and index"],
    )
    def test_state_arrives_and_the_old_one_is_kept(self, tmp_path, sidecars):
        """The bundle's matches replace the local ones, which are renamed with their journal files."""
        bundle = self.bundle(tmp_path, matches=("from the cloud",))
        target = make_settings(tmp_path / "target")
        if sidecars is not None:
            write_state(target, matches=("local",))
            for suffix in sidecars:
                (target.data_dir / f"state.db{suffix}").write_text("journal")

        imported = import_state(target, bundle)

        assert rows(target.db_path, "matched_tracks") == ["from the cloud"]
        if sidecars is None:
            assert imported.set_aside is None
            return
        assert imported.set_aside.parent == target.data_dir
        for suffix in ("-wal", "-shm"):
            moved = target.data_dir / f"{imported.set_aside.name}{suffix}"
            assert not (target.data_dir / f"state.db{suffix}").exists()
            assert moved.exists() == (suffix in sidecars)
            moved.unlink(missing_ok=True)
        assert rows(imported.set_aside, "matched_tracks") == ["local"]

    @pytest.mark.parametrize("earlier", [1, 3])
    def test_states_set_aside_earlier_are_not_overwritten(self, tmp_path, earlier):
        """Importing several times in one second keeps every earlier state."""
        bundle = self.bundle(tmp_path)
        target = make_settings(tmp_path / "target")
        write_state(target, matches=("local",))

        kept = [import_state(target, bundle).set_aside for _ in range(earlier + 1)]

        assert len(set(kept)) == earlier + 1
        assert all(path.exists() for path in kept)

    @pytest.mark.parametrize(
        "incoming, local, expected",
        [
            ([1.0, 2.0], None, [1.0, 2.0]),
            ([1.0, 2.0], [2.0, 3.0], [1.0, 2.0, 3.0]),
            ([], [4.0], [4.0]),
            ([1.0], "damaged", [1.0]),
            (None, [9.0], [9.0]),
        ],
        ids=["none here", "both", "none in bundle", "damaged here", "no file in bundle"],
    )
    def test_searches_of_both_machines_count(self, tmp_path, incoming, local, expected):
        """Both machines spend the same Spotify allowance, so their search times are combined."""
        bundle = self.bundle(tmp_path, searches=incoming)
        target = make_settings(tmp_path / "target")
        target.ensure_directories()
        ledger = target.data_dir / LEDGER_MEMBER
        if local is not None:
            ledger.write_text(local if isinstance(local, str) else json.dumps(local))

        import_state(target, bundle)

        assert json.loads(ledger.read_text()) == expected

    @pytest.mark.parametrize(
        "incoming, local, added",
        [
            (("a", "b"), (), 2),
            (("a", "b"), ("a",), 1),
            ((), ("a",), 0),
        ],
        ids=["all new", "one already here", "none in bundle"],
    )
    def test_snapshot_runs_are_added_never_replaced(self, tmp_path, incoming, local, added):
        """A run this machine already has keeps its own files; missing runs arrive."""
        bundle = self.bundle(tmp_path, runs=incoming)
        target = make_settings(tmp_path / "target")
        for run in local:
            playlist = target.snapshots_dir / run / "spotify" / "Discogs - house.json"
            playlist.parent.mkdir(parents=True)
            playlist.write_text("local copy")

        imported = import_state(target, bundle)

        assert imported.snapshot_runs_added == added
        for run in local:
            assert (target.snapshots_dir / run / "spotify" / "Discogs - house.json").read_text() == "local copy"
        present = {run.name for run in target.snapshots_dir.glob("*")} if target.snapshots_dir.exists() else set()
        assert present == set(incoming) | set(local)

    @pytest.mark.parametrize(
        "members, links, message",
        [
            ({"notes.txt": b"hello"}, (), "never does"),
            ({DATABASE_MEMBER: b"db", "../outside": b"x"}, (), "never does"),
            ({DATABASE_MEMBER: b"db", "snapshots/../../outside": b"x"}, (), "never does"),
            ({DATABASE_MEMBER: b"db", ".spotify_cache": b"login"}, (), "never does"),
            ({DATABASE_MEMBER: b"db"}, ("snapshots/link",), "never does"),
            ({}, (DATABASE_MEMBER,), "never does"),
            ({"snapshots": None}, (), "holds no state.db"),
        ],
        ids=["stray file", "parent path", "parent inside snapshots", "a login", "link", "database as link", "no database"],
    )
    def test_anything_but_a_bundle_is_refused_and_changes_nothing(self, tmp_path, members, links, message):
        """A file an export would not write is refused before the local state is touched."""
        archive = archive_of(tmp_path / "odd.tar.gz", members, links)
        target = make_settings(tmp_path / "target")
        write_state(target, matches=("local",))

        with pytest.raises(StateBundleError, match=message):
            import_state(target, archive)

        assert rows(target.db_path, "matched_tracks") == ["local"]
        assert not (tmp_path / "outside").exists()

    @pytest.mark.parametrize("content", [b"", b"not an archive", None], ids=["empty", "text", "missing"])
    def test_unreadable_file_is_an_error(self, tmp_path, content):
        """A file that is not a gzipped archive, or is not there, gives a clear error."""
        source = tmp_path / "odd.tar.gz"
        if content is not None:
            source.write_bytes(content)

        with pytest.raises(StateBundleError, match="could not be read"):
            import_state(make_settings(tmp_path / "target"), source)
