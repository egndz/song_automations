"""Tests for the state export and import commands."""

import sqlite3

import pytest
from typer.testing import CliRunner

from song_automations.cli import app

runner = CliRunner()


def write_database(folder) -> None:
    """Writes a small state database.

    Args:
        folder: The data directory.
    """
    folder.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(folder / "state.db") as database:
        database.execute("CREATE TABLE matched_tracks (name TEXT)")
        database.execute("INSERT INTO matched_tracks VALUES ('kept')")


class TestStateCommands:
    """Tests for 'state export' and 'state import'."""

    @pytest.mark.parametrize("has_state, code, said", [(True, 0, "Exported."), (False, 1, "no state to export")])
    def test_export_says_what_happened(self, isolated_data_dir, tmp_path, flat, has_state, code, said):
        """An export reports the bundle, or that there was nothing to export."""
        if has_state:
            write_database(isolated_data_dir)

        result = runner.invoke(app, ["state", "export", str(tmp_path / "bundle.tar.gz")])

        assert result.exit_code == code
        assert said in flat(result.output)
        assert (tmp_path / "bundle.tar.gz").exists() == has_state

    @pytest.mark.parametrize(
        "local_state, good_bundle, code, said",
        [
            (False, True, 0, "Imported."),
            (True, True, 0, "The state that was here is now"),
            (True, False, 1, "could not be read"),
        ],
        ids=["empty machine", "replaces", "not a bundle"],
    )
    def test_import_says_what_happened(
        self, isolated_data_dir, tmp_path, flat, monkeypatch, local_state, good_bundle, code, said
    ):
        """An import reports where the earlier state went, or why the file was refused."""
        bundle = tmp_path / "bundle.tar.gz"
        if good_bundle:
            write_database(tmp_path / "other")
            monkeypatch.setenv("DATA_DIR", str(tmp_path / "other"))
            assert runner.invoke(app, ["state", "export", str(bundle)]).exit_code == 0
            monkeypatch.setenv("DATA_DIR", str(isolated_data_dir))
        else:
            bundle.write_text("not a bundle")
        if local_state:
            write_database(isolated_data_dir)

        result = runner.invoke(app, ["state", "import", str(bundle)])

        assert result.exit_code == code
        assert said in flat(result.output)
        assert (isolated_data_dir / "state.db").exists() == (local_state or good_bundle)
