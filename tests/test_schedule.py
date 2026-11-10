"""Tests for the daily schedule, with a stand-in for launchd and a temporary folder."""

import plistlib
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from song_automations import cli, schedule
from song_automations.config import Settings

UID = 501
TARGET = f"gui/{UID}/{schedule.LABEL}"
PYTHON = "/opt/tool/env/bin/python"


class FakeLaunchd:
    """Stand-in for launchctl that remembers what is loaded and what was asked.

    Args:
        loaded: Whether the schedule starts out loaded.
        refusals: How many times loading is refused before it works.
        last_exit: What launchd reports as the last exit code, None for a job that never ran.
        running: Whether a run is going on.
    """

    def __init__(self, loaded=False, refusals=0, last_exit=None, running=False):
        self.loaded = loaded
        self.refusals = refusals
        self.last_exit = last_exit
        self.running = running
        self.commands: list[list[str]] = []
        self.slept: list[float] = []

    @property
    def verbs(self) -> list[str]:
        """Lists the launchctl verbs that were used, in order.

        Returns:
            The verbs.
        """
        return [command[1] for command in self.commands]

    def _report(self) -> str:
        """Writes what launchctl print says about a loaded job.

        Returns:
            The report text.
        """
        last = "(never exited)" if self.last_exit is None else str(self.last_exit)
        state = "running" if self.running else "not running"
        return (
            f"{TARGET} = {{\n\tactive count = 0\n\tstate = {state}\n\truns = 3\n"
            f"\tlast exit code = {last}\n\tjob state = exited\n}}\n"
        )

    def run(self, command: list[str]) -> subprocess.CompletedProcess:
        """Answers a launchctl command without touching the machine.

        Args:
            command: The command and its arguments.

        Returns:
            The finished process.
        """
        self.commands.append(command)
        assert command[0] == schedule.LAUNCHCTL
        verb = command[1]
        if verb == "print":
            if self.loaded:
                return subprocess.CompletedProcess(command, 0, self._report(), "")
            return subprocess.CompletedProcess(command, 113, "", "Could not find service")
        if verb == "bootout":
            self.loaded = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if verb == "bootstrap":
            if self.refusals > 0:
                self.refusals -= 1
                return subprocess.CompletedProcess(command, 5, "", "Bootstrap failed: 5: Input/output error")
            self.loaded = True
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected launchctl verb: {verb}")

    def system(self, agents_dir: Path, name: str = "Darwin") -> schedule.ScheduleSystem:
        """Builds the machine description that uses this stand-in.

        Args:
            agents_dir: Folder standing in for the user's LaunchAgents.
            name: Operating system name.

        Returns:
            The machine description.
        """
        return schedule.ScheduleSystem(
            name=name, uid=UID, agents_dir=agents_dir, run=self.run, sleep=self.slept.append
        )


def _flat(text: str) -> str:
    """Joins wrapped console output into one line.

    Args:
        text: Console output.

    Returns:
        The text with every run of whitespace turned into one space.
    """
    return " ".join(text.split())


def _project(tmp_path: Path, with_env: bool = True) -> Path:
    """Creates a folder standing in for the project the command is run from.

    Args:
        tmp_path: Temporary folder of the test.
        with_env: Whether the folder holds a .env file.

    Returns:
        The folder.
    """
    folder = tmp_path / "project folder"
    folder.mkdir()
    if with_env:
        (folder / ".env").write_text("")
    return folder


def _install(launchd: FakeLaunchd, tmp_path: Path, target="all", hour=4, minute=30, with_env=True) -> Path:
    """Installs a schedule into the temporary folder.

    Args:
        launchd: Stand-in for launchctl.
        tmp_path: Temporary folder of the test.
        target: Platform to sync.
        hour: Hour to run at.
        minute: Minute to run at.
        with_env: Whether the project folder holds a .env file.

    Returns:
        The schedule file.
    """
    return schedule.install(
        launchd.system(tmp_path / "LaunchAgents"),
        PYTHON,
        target,
        hour,
        minute,
        _project(tmp_path, with_env),
        tmp_path / "data",
    )


class TestPureParts:
    """The parts that only compute."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("04:30", (4, 30)), ("4:30", (4, 30)), ("00:00", (0, 0)), ("23:59", (23, 59)), (" 12:05 ", (12, 5))],
    )
    def test_time_of_day_is_read(self, text, expected):
        """A time on the 24 hour clock becomes an hour and a minute."""
        assert schedule.parse_time(text) == expected

    @pytest.mark.parametrize("text", ["24:00", "12:60", "noon", "", "4.30", "12:5", "-1:30", "04:30pm"])
    def test_anything_else_is_refused_with_an_example(self, text):
        """What is not a time of day is refused, and the message shows the format."""
        with pytest.raises(schedule.ScheduleError) as caught:
            schedule.parse_time(text)
        assert "04:30" in str(caught.value)

    @pytest.mark.parametrize(
        ("target", "hour", "minute"),
        [("all", 4, 30), ("spotify", 0, 0), ("soundcloud", 23, 59)],
    )
    def test_plist_describes_the_daily_unattended_run(self, tmp_path, target, hour, minute):
        """The schedule runs the interpreter with the module, unattended, at the chosen time."""
        log = tmp_path / "data" / "logs" / "scheduled-sync.log"
        content = schedule.build_plist(PYTHON, target, hour, minute, tmp_path, log)
        assert content["Label"] == "com.songautomations.sync"
        assert content["ProgramArguments"] == [
            PYTHON,
            "-m",
            "song_automations",
            "--unattended",
            "sync",
            target,
        ]
        assert content["StartCalendarInterval"] == {"Hour": hour, "Minute": minute}
        assert content["WorkingDirectory"] == str(tmp_path)
        assert content["StandardOutPath"] == content["StandardErrorPath"] == str(log)
        assert content["RunAtLoad"] is False
        assert content["EnvironmentVariables"] == {
            "PYTHONUNBUFFERED": "1",
            "_TYPER_STANDARD_TRACEBACK": "1",
        }
        assert plistlib.loads(plistlib.dumps(content)) == content

    @pytest.mark.parametrize(
        ("folder", "target", "hour", "minute", "expected"),
        [
            (
                "/home/me/songs",
                "all",
                4,
                30,
                "30 4 * * * cd /home/me/songs && PYTHONUNBUFFERED=1 _TYPER_STANDARD_TRACEBACK=1 "
                "/opt/tool/env/bin/python -m song_automations "
                "--unattended sync all >> /data/logs/scheduled-sync.log 2>&1",
            ),
            (
                "/home/me/my songs",
                "spotify",
                23,
                5,
                "5 23 * * * cd '/home/me/my songs' && PYTHONUNBUFFERED=1 _TYPER_STANDARD_TRACEBACK=1 "
                "/opt/tool/env/bin/python -m song_automations "
                "--unattended sync spotify >> /data/logs/scheduled-sync.log 2>&1",
            ),
        ],
    )
    def test_crontab_line_does_the_same(self, folder, target, hour, minute, expected):
        """Other systems get one crontab line, with folders that hold spaces quoted."""
        line = schedule.crontab_line(
            PYTHON, target, hour, minute, Path(folder), Path("/data/logs/scheduled-sync.log")
        )
        assert line == expected

    @pytest.mark.parametrize(
        ("data_dir", "inside"),
        [("data", True), ("nested/data", True), ("/var/songs", False)],
    )
    def test_log_path_is_always_a_full_path(self, tmp_path, monkeypatch, data_dir, inside):
        """A data directory given relative to the project still yields a full log path."""
        monkeypatch.chdir(tmp_path)
        log = schedule.log_path(Path(data_dir))
        assert log.is_absolute()
        assert log.name == "scheduled-sync.log"
        assert log.parent.name == "logs"
        assert (Path.cwd() in log.parents) is inside

    @pytest.mark.parametrize("arguments", [["--version"], ["--help"], ["schedule", "--help"]])
    def test_package_runs_as_a_module(self, monkeypatch, capsys, arguments):
        """python -m song_automations starts the command line, which the schedule relies on."""
        monkeypatch.setattr(sys, "argv", ["song_automations", *arguments])
        with pytest.raises(SystemExit) as stopped:
            runpy.run_module("song_automations", run_name="__main__")
        assert stopped.value.code == 0
        assert capsys.readouterr().out


class TestInstall:
    """Writing and loading the schedule."""

    @pytest.mark.parametrize(
        ("loaded", "verbs"),
        [(False, ["print", "bootstrap"]), (True, ["print", "bootout", "bootstrap"])],
    )
    def test_schedule_is_written_and_loaded(self, tmp_path, loaded, verbs):
        """The file lands in the agents folder and is loaded, after unloading an earlier one."""
        launchd = FakeLaunchd(loaded=loaded)
        path = _install(launchd, tmp_path, target="spotify", hour=5, minute=15)
        assert path == tmp_path / "LaunchAgents" / "com.songautomations.sync.plist"
        with path.open("rb") as handle:
            content = plistlib.load(handle)
        assert content["ProgramArguments"][0] == PYTHON
        assert content["ProgramArguments"][-3:] == ["--unattended", "sync", "spotify"]
        assert content["StartCalendarInterval"] == {"Hour": 5, "Minute": 15}
        assert content["WorkingDirectory"] == str(tmp_path / "project folder")
        assert content["StandardOutPath"] == str(tmp_path / "data" / "logs" / "scheduled-sync.log")
        assert (tmp_path / "data" / "logs").is_dir()
        assert launchd.verbs == verbs
        assert launchd.commands[-1] == [schedule.LAUNCHCTL, "bootstrap", f"gui/{UID}", str(path)]
        assert all(command[2] == TARGET for command in launchd.commands[:-1])
        assert launchd.loaded

    @pytest.mark.parametrize(
        ("target", "with_env", "words"),
        [
            ("all", False, ".env"),
            ("spotify", False, ".env"),
            ("soundcloud", False, ".env"),
            ("tidal", True, "Unknown platform"),
            ("", True, "Unknown platform"),
        ],
    )
    def test_request_that_cannot_work_is_refused_before_anything_changes(
        self, tmp_path, target, with_env, words
    ):
        """Without a .env file in the folder, or with an unknown platform, nothing is written or run."""
        launchd = FakeLaunchd()
        with pytest.raises(schedule.ScheduleError) as caught:
            _install(launchd, tmp_path, target=target, with_env=with_env)
        assert words in str(caught.value)
        assert launchd.commands == []
        assert not (tmp_path / "LaunchAgents").exists()
        assert not (tmp_path / "data").exists()

    @pytest.mark.parametrize(
        ("refusals", "accepted", "pauses"),
        [(0, True, 0), (1, True, 1), (2, True, 2), (3, False, 2), (9, False, 2)],
    )
    def test_loading_is_retried_a_few_times(self, tmp_path, refusals, accepted, pauses):
        """A short refusal right after unloading is waited out, a lasting one is reported."""
        launchd = FakeLaunchd(loaded=True, refusals=refusals)
        if accepted:
            _install(launchd, tmp_path)
        else:
            with pytest.raises(schedule.ScheduleError) as caught:
                _install(launchd, tmp_path)
            assert "Input/output error" in str(caught.value)
        assert launchd.loaded is accepted
        assert launchd.slept == [schedule.BOOTSTRAP_PAUSE_SECONDS] * pauses
        assert (tmp_path / "LaunchAgents" / "com.songautomations.sync.plist").is_file()


class TestUninstall:
    """Removing the schedule."""

    @pytest.mark.parametrize(
        ("installed", "loaded", "verbs", "removed"),
        [
            (True, True, ["print", "bootout"], True),
            (True, False, ["print"], True),
            (False, True, ["print", "bootout"], True),
            (False, False, ["print"], False),
        ],
    )
    def test_schedule_is_unloaded_and_its_file_deleted(self, tmp_path, installed, loaded, verbs, removed):
        """Whatever is left of a schedule is removed, and nothing is done when there is none."""
        launchd = FakeLaunchd()
        if installed:
            _install(launchd, tmp_path)
            launchd.commands.clear()
        launchd.loaded = loaded
        assert schedule.uninstall(launchd.system(tmp_path / "LaunchAgents")) is removed
        assert launchd.verbs == verbs
        assert not launchd.loaded
        assert not (tmp_path / "LaunchAgents" / "com.songautomations.sync.plist").exists()


class TestStatus:
    """Reading the schedule back."""

    @pytest.mark.parametrize(
        ("last_exit", "running"),
        [(None, False), (0, False), (2, False), (1, True), (-9, False)],
    )
    def test_loaded_schedule_reports_time_and_last_run(self, tmp_path, last_exit, running):
        """The time, platform and log come from the file, the last exit code from launchd."""
        launchd = FakeLaunchd(last_exit=last_exit, running=running)
        path = _install(launchd, tmp_path, target="soundcloud", hour=4, minute=5)
        status = schedule.read_status(launchd.system(tmp_path / "LaunchAgents"))
        assert status == schedule.ScheduleStatus(
            installed=True,
            loaded=True,
            running=running,
            time="04:05",
            target="soundcloud",
            last_exit_code=last_exit,
            log_path=tmp_path / "data" / "logs" / "scheduled-sync.log",
            plist_path=path,
        )

    @pytest.mark.parametrize(
        ("content", "loaded", "installed", "time"),
        [
            (None, False, False, None),
            (None, True, False, None),
            (b"not a plist", False, True, None),
            (b"not a plist", True, True, None),
            (plistlib.dumps(["a", "list"]), False, True, None),
            (plistlib.dumps({"StartCalendarInterval": {"Hour": 7, "Minute": 0}}), False, True, "07:00"),
        ],
    )
    def test_missing_or_damaged_file_is_reported_not_raised(self, tmp_path, content, loaded, installed, time):
        """A schedule that is absent, half there or unreadable still gets a status."""
        agents = tmp_path / "LaunchAgents"
        agents.mkdir()
        if content is not None:
            schedule.plist_path(agents).write_bytes(content)
        status = schedule.read_status(FakeLaunchd(loaded=loaded).system(agents))
        assert (status.installed, status.loaded, status.time) == (installed, loaded, time)
        assert status.target is None


class TestScheduleCommands:
    """The schedule commands as typed in a terminal."""

    @staticmethod
    def _stage(tmp_path, monkeypatch, system_name="Darwin", with_env=True, launchd=None):
        """Points the commands at a temporary folder and a stand-in for launchd.

        Args:
            tmp_path: Temporary folder of the test.
            monkeypatch: Pytest monkeypatch.
            system_name: Operating system the commands believe they run on.
            with_env: Whether the folder the command is run from holds a .env file.
            launchd: Stand-in to use, None for a fresh one.

        Returns:
            The stand-in for launchd and the project folder.
        """
        launchd = launchd or FakeLaunchd()
        project = tmp_path / "project folder"
        if not project.exists():
            _project(tmp_path, with_env)
        system = launchd.system(tmp_path / "LaunchAgents", system_name)
        monkeypatch.chdir(project)
        monkeypatch.setattr(schedule, "current_system", lambda: system)
        monkeypatch.setattr(schedule, "get_settings", lambda: Settings(data_dir=tmp_path / "data"))
        return launchd, project

    @pytest.mark.parametrize(
        ("arguments", "time", "target"),
        [
            ([], {"Hour": 4, "Minute": 30}, "all"),
            (["--at", "06:15"], {"Hour": 6, "Minute": 15}, "all"),
            (["--at", "23:00", "--platform", "spotify"], {"Hour": 23, "Minute": 0}, "spotify"),
            (["-p", "soundcloud"], {"Hour": 4, "Minute": 30}, "soundcloud"),
        ],
    )
    def test_install_writes_the_schedule_for_this_interpreter_and_folder(
        self, tmp_path, monkeypatch, arguments, time, target
    ):
        """The schedule runs the interpreter in use, from the folder the command was typed in."""
        launchd, project = self._stage(tmp_path, monkeypatch)
        result = CliRunner().invoke(cli.app, ["schedule", "install", *arguments])
        assert result.exit_code == 0
        with (tmp_path / "LaunchAgents" / "com.songautomations.sync.plist").open("rb") as handle:
            content = plistlib.load(handle)
        assert content["ProgramArguments"] == schedule.sync_command(sys.executable, target)
        assert content["StartCalendarInterval"] == time
        assert Path(content["WorkingDirectory"]).resolve() == project.resolve()
        assert launchd.loaded
        assert f"{time['Hour']:02d}:{time['Minute']:02d}" in _flat(result.output)
        assert "scheduled-sync.log" in result.output

    @pytest.mark.parametrize(
        ("arguments", "with_env", "words"),
        [
            ([], False, "no .env file"),
            (["--at", "25:00"], True, "not a time of day"),
            (["--platform", "tidal"], True, "Unknown platform"),
        ],
    )
    def test_install_explains_a_refusal(self, tmp_path, monkeypatch, arguments, with_env, words):
        """A refused install exits with 1, says why and leaves the machine alone."""
        launchd, _ = self._stage(tmp_path, monkeypatch, with_env=with_env)
        result = CliRunner().invoke(cli.app, ["schedule", "install", *arguments])
        assert result.exit_code == 1
        assert words in _flat(result.output)
        assert launchd.commands == []
        assert not (tmp_path / "LaunchAgents").exists()

    @pytest.mark.parametrize("system_name", ["Linux", "Windows", "FreeBSD"])
    def test_other_systems_get_the_crontab_line_and_nothing_is_installed(
        self, tmp_path, monkeypatch, system_name
    ):
        """Away from macOS every schedule command only explains, it never runs or writes anything."""
        launchd, _ = self._stage(tmp_path, monkeypatch, system_name=system_name)
        installed = CliRunner().invoke(cli.app, ["schedule", "install", "--at", "07:45"])
        others = [CliRunner().invoke(cli.app, ["schedule", verb]) for verb in ("status", "uninstall")]
        assert installed.exit_code == 0
        assert "45 7 * * * cd " in installed.output
        assert "-m song_automations --unattended sync all >> " in _flat(installed.output)
        assert all(result.exit_code == 0 and "crontab" in result.output for result in others)
        assert launchd.commands == []
        assert not (tmp_path / "LaunchAgents").exists()

    @pytest.mark.parametrize(
        ("installed", "last_exit", "words"),
        [
            (False, None, ["Not installed"]),
            (True, None, ["Installed and loaded", "04:30", "syncing all", "reports none yet"]),
            (True, 0, ["Installed and loaded", "exit code 0", "scheduled-sync.log"]),
            (True, 2, ["exit code 2", "login is needed"]),
            (True, 1, ["exit code 1", "see the log"]),
        ],
    )
    def test_status_says_what_is_installed_and_how_the_last_run_ended(
        self, tmp_path, monkeypatch, installed, last_exit, words
    ):
        """Status names the state, the time, the last exit code and the log."""
        launchd, _ = self._stage(tmp_path, monkeypatch, launchd=FakeLaunchd(last_exit=last_exit))
        if installed:
            CliRunner().invoke(cli.app, ["schedule", "install"])
        result = CliRunner().invoke(cli.app, ["schedule", "status"])
        output = _flat(result.output)
        assert result.exit_code == 0
        assert all(word in output for word in words)

    @pytest.mark.parametrize(
        ("loaded", "file_there", "words"),
        [(False, True, "Installed but not loaded"), (True, False, "schedule file is gone")],
    )
    def test_status_names_a_half_installed_schedule(
        self, tmp_path, monkeypatch, loaded, file_there, words
    ):
        """A file without a loaded job, or the reverse, is called what it is."""
        launchd, _ = self._stage(tmp_path, monkeypatch)
        CliRunner().invoke(cli.app, ["schedule", "install"])
        launchd.loaded = loaded
        if not file_there:
            (tmp_path / "LaunchAgents" / "com.songautomations.sync.plist").unlink()
        result = CliRunner().invoke(cli.app, ["schedule", "status"])
        assert words in _flat(result.output)

    @pytest.mark.parametrize(("installed", "words"), [(True, "was removed"), (False, "No schedule was installed")])
    def test_uninstall_removes_the_schedule(self, tmp_path, monkeypatch, installed, words):
        """Uninstall unloads the job, deletes the file and says so."""
        launchd, _ = self._stage(tmp_path, monkeypatch)
        if installed:
            CliRunner().invoke(cli.app, ["schedule", "install"])
        result = CliRunner().invoke(cli.app, ["schedule", "uninstall"])
        assert result.exit_code == 0
        assert words in _flat(result.output)
        assert not launchd.loaded
        assert not (tmp_path / "LaunchAgents" / "com.songautomations.sync.plist").exists()
