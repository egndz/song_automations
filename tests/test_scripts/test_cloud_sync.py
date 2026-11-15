"""Tests for the script that runs the sync on a machine that starts empty."""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "cloud_sync.sh"
KEY = "0123456789abcdef0123456789abcdef"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "git", "openssl")),
    reason="needs bash, git and openssl",
)


class Machines:
    """A git remote and any number of machines that share state through it.

    Args:
        folder: Folder of the test.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.remote = folder / "remote.git"
        self.checkout = folder / "checkout"
        subprocess.run(["git", "init", "--quiet", "--bare", str(self.remote)], check=True)
        subprocess.run(["git", "init", "--quiet", str(self.checkout)], check=True)
        subprocess.run(
            ["git", "-C", str(self.checkout), "remote", "add", "origin", str(self.remote)], check=True
        )

    def data_dir(self, name: str, matches: tuple[str, ...] | None = None) -> Path:
        """Makes the data directory of one machine.

        Args:
            name: Name of the machine.
            matches: Matched tracks it remembers, None for a machine that remembers nothing.

        Returns:
            The data directory.
        """
        folder = self.folder / name
        folder.mkdir()
        if matches is not None:
            with sqlite3.connect(folder / "state.db") as database:
                database.execute("CREATE TABLE matched_tracks (name TEXT)")
                database.executemany("INSERT INTO matched_tracks VALUES (?)", [(name,) for name in matches])
        return folder

    def run(self, data_dir: Path, *arguments: str, **environment: str) -> subprocess.CompletedProcess:
        """Runs the script as one machine.

        Args:
            data_dir: That machine's data directory.
            *arguments: Arguments of the script.
            **environment: Variables that replace the defaults; an empty value removes one.

        Returns:
            The finished process.
        """
        variables = {
            **os.environ,
            "DATA_DIR": str(data_dir),
            "PYTHONPATH": str(ROOT / "src"),
            "SONG_AUTOMATIONS_PYTHON": sys.executable,
            "SONG_AUTOMATIONS_STATE_REPO": str(self.checkout),
            "SONG_AUTOMATIONS_STATE_KEY": KEY,
            "SONG_AUTOMATIONS_CLOUD_ENV": str(self.folder / "no-such-file"),
            **environment,
        }
        variables = {name: value for name, value in variables.items() if value != ""}
        return subprocess.run(
            ["bash", str(SCRIPT), *arguments], env=variables, capture_output=True, text=True, timeout=120
        )

    def commits(self) -> int:
        """Counts the commits on the state branch of the remote."""
        counted = subprocess.run(
            ["git", "-C", str(self.remote), "rev-list", "--count", "sync-state"],
            capture_output=True,
            text=True,
        )
        return int(counted.stdout or 0)


def matches(data_dir: Path) -> list[str]:
    """Reads the matched tracks a machine remembers.

    Args:
        data_dir: The machine's data directory.

    Returns:
        Their names, sorted.
    """
    with sqlite3.connect(data_dir / "state.db") as database:
        return sorted(row[0] for row in database.execute("SELECT name FROM matched_tracks"))


def fake_python(folder: Path) -> Path:
    """Writes a python stand-in whose sync remembers one more track and exits as told.

    Every other command goes to the real python, so the state commands are real.

    Args:
        folder: Folder of the test.

    Returns:
        The stand-in.
    """
    stand_in = folder / "python"
    record = (
        "import os, sqlite3, sys; "
        "database = sqlite3.connect(os.path.join(os.environ['DATA_DIR'], 'state.db')); "
        "database.execute(\\\"INSERT INTO matched_tracks VALUES ('found by the sync')\\\"); "
        "database.commit(); database.close(); "
        "os.system(os.environ.get('DURING_SYNC', '')); sys.exit(int(os.environ['SYNC_STATUS']))"
    )
    stand_in.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  *"--unattended sync spotify"*) echo "$*" > "$SYNC_ARGUMENTS"; exec "{sys.executable}" -c "{record}" ;;\n'
        f'  *) exec "{sys.executable}" "$@" ;;\n'
        "esac\n"
    )
    stand_in.chmod(0o755)
    return stand_in


class TestStateTravels:
    """Tests for storing the state on a branch and reading it elsewhere."""

    @pytest.mark.parametrize("pushes", [1, 2, 3])
    def test_another_machine_gets_the_newest_state(self, tmp_path, pushes):
        """Each push adds one commit, and a pull brings the last one."""
        machines = Machines(tmp_path)
        for number in range(pushes):
            pushed = machines.run(machines.data_dir(f"laptop{number}", (f"match {number}",)), "push")
            assert pushed.returncode == 0, pushed.stderr
        cloud = machines.data_dir("cloud")

        pulled = machines.run(cloud, "pull")

        assert pulled.returncode == 0, pulled.stderr
        assert matches(cloud) == [f"match {pushes - 1}"]
        assert machines.commits() == pushes

    @pytest.mark.parametrize("secret", ["match 0", "matched_tracks", "SQLite format"])
    def test_what_is_stored_cannot_be_read_without_the_key(self, tmp_path, secret):
        """The file on the branch shows neither the matches nor that it is a database."""
        machines = Machines(tmp_path)
        assert machines.run(machines.data_dir("laptop", ("match 0",)), "push").returncode == 0

        stored = subprocess.run(
            ["git", "-C", str(machines.remote), "show", "sync-state:state.tar.gz.enc"],
            capture_output=True,
            check=True,
        ).stdout

        assert stored.startswith(b"Salted__")
        assert secret.encode() not in stored

    @pytest.mark.parametrize(
        "command, stored, environment, message",
        [
            ("pull", False, {}, "is not on 'origin'"),
            ("pull", True, {"SONG_AUTOMATIONS_STATE_KEY": "another key"}, "could not be decrypted"),
            ("pull", True, {"SONG_AUTOMATIONS_STATE_KEY": ""}, "SONG_AUTOMATIONS_STATE_KEY is not set"),
            ("push", False, {"SONG_AUTOMATIONS_STATE_KEY": ""}, "SONG_AUTOMATIONS_STATE_KEY is not set"),
            ("run", False, {}, "is not on 'origin'"),
            ("sideways", False, {}, "usage:"),
            ("", False, {}, "usage:"),
        ],
        ids=["no branch", "wrong key", "no key to pull", "no key to push", "run with no branch", "unknown", "nothing"],
    )
    def test_failures_stop_before_anything_changes(self, tmp_path, command, stored, environment, message):
        """A missing branch or key stops the script and leaves the machine's state as it was."""
        machines = Machines(tmp_path)
        if stored:
            assert machines.run(machines.data_dir("laptop", ("match 0",)), "push").returncode == 0
        cloud = machines.data_dir("cloud", ("already here",))
        before = machines.commits()

        result = machines.run(cloud, *([command] if command else []), **environment)

        assert result.returncode != 0
        assert message in result.stderr
        assert matches(cloud) == ["already here"]
        assert machines.commits() == before

    def test_key_is_read_from_the_file_when_not_exported(self, tmp_path):
        """A laptop keeps the key next to its other settings instead of exporting it."""
        machines = Machines(tmp_path)
        key_file = tmp_path / "cloud.env"
        key_file.write_text(f"SPOTIFY_REFRESH_TOKEN=abc\nSONG_AUTOMATIONS_STATE_KEY={KEY}\n")
        from_file = {"SONG_AUTOMATIONS_STATE_KEY": "", "SONG_AUTOMATIONS_CLOUD_ENV": str(key_file)}
        assert machines.run(machines.data_dir("laptop", ("match 0",)), "push", **from_file).returncode == 0
        cloud = machines.data_dir("cloud")

        pulled = machines.run(cloud, "pull")

        assert pulled.returncode == 0, pulled.stderr
        assert matches(cloud) == ["match 0"]


class TestRun:
    """Tests for the whole run: pull, sync, push."""

    @pytest.mark.parametrize("status", [0, 1, 3])
    def test_state_is_stored_whether_or_not_the_sync_finished(self, tmp_path, status):
        """What the sync found is pushed even when it stopped early, and its exit status is kept."""
        machines = Machines(tmp_path)
        assert machines.run(machines.data_dir("laptop", ("match 0",)), "push").returncode == 0
        arguments = tmp_path / "arguments"

        ran = machines.run(
            machines.data_dir("cloud"),
            "run",
            "--dry-run",
            SONG_AUTOMATIONS_PYTHON=str(fake_python(tmp_path)),
            SYNC_STATUS=str(status),
            SYNC_ARGUMENTS=str(arguments),
        )

        assert ran.returncode == status, ran.stderr
        assert arguments.read_text().split() == "-m song_automations --unattended sync spotify --dry-run".split()
        assert ("stopped early" in ran.stderr) == (status != 0)
        later = machines.data_dir("later")
        assert machines.run(later, "pull").returncode == 0
        assert matches(later) == ["found by the sync", "match 0"]
        assert machines.commits() == 2

    @pytest.mark.parametrize("quoting", ['{}', '"{}"', "'{}'", "{}\r"], ids=["plain", "double quotes", "single quotes", "windows line end"])
    def test_key_file_may_quote_the_key(self, tmp_path, quoting):
        """Quotes and a carriage return around the key in the file are not part of the key."""
        machines = Machines(tmp_path)
        key_file = tmp_path / "cloud.env"
        key_file.write_text(f"SONG_AUTOMATIONS_STATE_KEY={quoting.format(KEY)}\n")
        from_file = {"SONG_AUTOMATIONS_STATE_KEY": "", "SONG_AUTOMATIONS_CLOUD_ENV": str(key_file)}
        assert machines.run(machines.data_dir("laptop", ("match 0",)), "push", **from_file).returncode == 0
        cloud = machines.data_dir("cloud")

        pulled = machines.run(cloud, "pull")

        assert pulled.returncode == 0, pulled.stderr
        assert matches(cloud) == ["match 0"]

    @pytest.mark.parametrize("status", [0, 3])
    def test_state_stored_by_another_machine_during_the_run_is_not_replaced(self, tmp_path, status):
        """A push made while the sync was running wins; the run's own push is refused."""
        machines = Machines(tmp_path)
        assert machines.run(machines.data_dir("laptop", ("match 0",)), "push").returncode == 0
        meanwhile = machines.data_dir("meanwhile", ("pushed meanwhile",))
        other_push = (
            f"DATA_DIR='{meanwhile}' SONG_AUTOMATIONS_PYTHON='{sys.executable}' bash '{SCRIPT}' push >/dev/null 2>&1"
        )

        ran = machines.run(
            machines.data_dir("cloud"),
            "run",
            SONG_AUTOMATIONS_PYTHON=str(fake_python(tmp_path)),
            SYNC_STATUS=str(status),
            SYNC_ARGUMENTS=str(tmp_path / "arguments"),
            DURING_SYNC=other_push,
        )

        assert ran.returncode != 0
        assert "another machine stored its state" in ran.stderr
        later = machines.data_dir("later")
        assert machines.run(later, "pull").returncode == 0
        assert matches(later) == ["pushed meanwhile"]
        assert machines.commits() == 2
