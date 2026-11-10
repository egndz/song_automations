"""Runs the sync every day without anyone starting it."""

import os
import platform
import plistlib
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from xml.parsers.expat import ExpatError

import typer
from rich.console import Console

from song_automations.config import get_settings

LABEL = "com.songautomations.sync"
DEFAULT_TIME = "04:30"
TARGETS = ("all", "spotify", "soundcloud")
LOG_NAME = "scheduled-sync.log"
MACOS = "Darwin"
LAUNCHCTL = "/bin/launchctl"
BOOTSTRAP_ATTEMPTS = 3
BOOTSTRAP_PAUSE_SECONDS = 1.0
TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})$")
EXIT_CODE_PATTERN = re.compile(r"last exit code = (-?\d+)")
RUNNING_PATTERN = re.compile(r"^\s*state = running\s*$", re.MULTILINE)
RUN_ENVIRONMENT = {"PYTHONUNBUFFERED": "1", "_TYPER_STANDARD_TRACEBACK": "1"}
EXIT_CODE_MEANINGS = {
    0: "finished, or stopped normally after using its searches",
    2: "stopped because a login is needed or a platform rate limited the app",
}

Runner = Callable[[list[str]], subprocess.CompletedProcess]

console = Console()
schedule_app = typer.Typer(help="Run the sync every day by itself.")


class ScheduleError(RuntimeError):
    """The schedule could not be installed, and the message says why."""


@dataclass(frozen=True)
class ScheduleSystem:
    """The parts of the machine a schedule touches, so tests can stand in for them.

    Attributes:
        name: Operating system name, Darwin on macOS.
        uid: Numeric id of the user the schedule belongs to.
        agents_dir: Folder launchd reads the user's schedules from.
        run: Function that runs a command and returns the finished process.
        sleep: Function that waits for a number of seconds.
    """

    name: str
    uid: int
    agents_dir: Path
    run: Runner
    sleep: Callable[[float], None]


@dataclass(frozen=True)
class ScheduleStatus:
    """What is known about the installed schedule.

    Attributes:
        installed: Whether the schedule file exists.
        loaded: Whether launchd has the schedule loaded.
        running: Whether a scheduled run is going on right now.
        time: Time of day it runs at, as HH:MM, None when unknown.
        target: Platform it syncs, None when unknown.
        last_exit_code: Exit code of the last run, None when launchd reports none.
        log_path: File the runs write to, None when unknown.
        plist_path: Where the schedule file is or would be.
    """

    installed: bool
    loaded: bool
    running: bool
    time: str | None
    target: str | None
    last_exit_code: int | None
    log_path: Path | None
    plist_path: Path


def _run_command(command: list[str]) -> subprocess.CompletedProcess:
    """Runs a command without a shell and keeps its output.

    Args:
        command: The command and its arguments.

    Returns:
        The finished process.
    """
    return subprocess.run(command, check=False, capture_output=True, text=True)


def current_system() -> ScheduleSystem:
    """Describes the machine this runs on.

    Returns:
        The real operating system, user, schedule folder and command runner.
    """
    return ScheduleSystem(
        name=platform.system(),
        uid=getattr(os, "getuid", lambda: 0)(),
        agents_dir=Path.home() / "Library" / "LaunchAgents",
        run=_run_command,
        sleep=time.sleep,
    )


def parse_time(text: str) -> tuple[int, int]:
    """Reads a time of day.

    Args:
        text: Time as HH:MM on the 24 hour clock.

    Returns:
        Hour and minute.

    Raises:
        ScheduleError: When the text is not a time of day.
    """
    found = TIME_PATTERN.match(text.strip())
    if found and int(found[1]) < 24 and int(found[2]) < 60:
        return int(found[1]), int(found[2])
    raise ScheduleError(f"'{text}' is not a time of day. Use HH:MM on the 24 hour clock, such as 04:30.")


def sync_command(python: str, target: str) -> list[str]:
    """Builds the command a scheduled run executes.

    Args:
        python: Path of the Python interpreter that has the tool installed.
        target: all, spotify or soundcloud.

    Returns:
        The command and its arguments.
    """
    return [python, "-m", "song_automations", "--unattended", "sync", target]


def log_path(data_dir: Path) -> Path:
    """Names the file scheduled runs write to.

    Args:
        data_dir: Data directory of the tool.

    Returns:
        The log file as a full path, since launchd does not know the folder
        a relative one would start from.
    """
    return (data_dir / "logs" / LOG_NAME).absolute()


def plist_path(agents_dir: Path) -> Path:
    """Names the schedule file.

    Args:
        agents_dir: Folder launchd reads the user's schedules from.

    Returns:
        The schedule file.
    """
    return agents_dir / f"{LABEL}.plist"


def build_plist(
    python: str,
    target: str,
    hour: int,
    minute: int,
    working_dir: Path,
    log: Path,
) -> dict:
    """Describes the daily run the way launchd wants it.

    The run prints as it goes, and a crash is logged as a plain traceback,
    which never lists the values of variables and so keeps secrets out of
    the log.

    Args:
        python: Path of the Python interpreter that has the tool installed.
        target: all, spotify or soundcloud.
        hour: Hour of the day to run at.
        minute: Minute of the hour to run at.
        working_dir: Folder the run starts in, the one that holds the .env file.
        log: File that receives everything the run prints.

    Returns:
        The content of the schedule file.
    """
    return {
        "Label": LABEL,
        "ProgramArguments": sync_command(python, target),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "WorkingDirectory": str(working_dir),
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": dict(RUN_ENVIRONMENT),
        "RunAtLoad": False,
    }


def crontab_line(
    python: str,
    target: str,
    hour: int,
    minute: int,
    working_dir: Path,
    log: Path,
) -> str:
    """Writes the crontab line that does what the macOS schedule does.

    Args:
        python: Path of the Python interpreter that has the tool installed.
        target: all, spotify or soundcloud.
        hour: Hour of the day to run at.
        minute: Minute of the hour to run at.
        working_dir: Folder the run starts in, the one that holds the .env file.
        log: File that receives everything the run prints.

    Returns:
        One line for crontab -e.
    """
    environment = " ".join(f"{name}={value}" for name, value in RUN_ENVIRONMENT.items())
    command = " ".join(shlex.quote(part) for part in sync_command(python, target))
    return (
        f"{minute} {hour} * * * cd {shlex.quote(str(working_dir))} && "
        f"{environment} {command} >> {shlex.quote(str(log))} 2>&1"
    )


def check_request(target: str, working_dir: Path) -> None:
    """Refuses a schedule that could not work.

    Args:
        target: Platform to sync.
        working_dir: Folder the run would start in.

    Raises:
        ScheduleError: When the platform is unknown or the folder has no .env file.
    """
    if target not in TARGETS:
        raise ScheduleError(f"Unknown platform '{target}'. Use spotify, soundcloud or all.")
    if not (working_dir / ".env").is_file():
        raise ScheduleError(
            f"There is no .env file in {working_dir}. A scheduled run reads its settings from "
            "the folder it starts in, so run this command from the folder that holds your .env file."
        )


def service_target(uid: int) -> str:
    """Names the schedule the way launchctl addresses it.

    Args:
        uid: Numeric id of the user.

    Returns:
        The service target.
    """
    return f"gui/{uid}/{LABEL}"


def is_loaded(system: ScheduleSystem) -> bool:
    """Asks launchd whether it has the schedule loaded.

    Args:
        system: The machine.

    Returns:
        True when the schedule is loaded.
    """
    return system.run([LAUNCHCTL, "print", service_target(system.uid)]).returncode == 0


def _bootstrap(system: ScheduleSystem, path: Path) -> None:
    """Hands the schedule file to launchd.

    launchd unloads in the background, so loading right after an unload can
    be refused for a moment. It is tried a few times before giving up.

    Args:
        system: The machine.
        path: The schedule file.

    Raises:
        ScheduleError: When launchd keeps refusing the schedule.
    """
    detail = ""
    for attempt in range(BOOTSTRAP_ATTEMPTS):
        result = system.run([LAUNCHCTL, "bootstrap", f"gui/{system.uid}", str(path)])
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout or "").strip() or f"exit code {result.returncode}"
        if attempt < BOOTSTRAP_ATTEMPTS - 1:
            system.sleep(BOOTSTRAP_PAUSE_SECONDS)
    raise ScheduleError(
        f"launchd did not accept the schedule ({detail}). The file {path} was written, so "
        "macOS picks it up at your next login. Run 'song-automations schedule install' again to retry now."
    )


def install(
    system: ScheduleSystem,
    python: str,
    target: str,
    hour: int,
    minute: int,
    working_dir: Path,
    data_dir: Path,
) -> Path:
    """Writes the schedule file and loads it, replacing an earlier schedule.

    Args:
        system: The machine.
        python: Path of the Python interpreter that has the tool installed.
        target: all, spotify or soundcloud.
        hour: Hour of the day to run at.
        minute: Minute of the hour to run at.
        working_dir: Folder the run starts in, the one that holds the .env file.
        data_dir: Data directory of the tool, the log goes below it.

    Returns:
        The schedule file.

    Raises:
        ScheduleError: When the request cannot work or launchd refuses it.
    """
    check_request(target, working_dir)
    log = log_path(data_dir)
    log.parent.mkdir(parents=True, exist_ok=True)
    system.agents_dir.mkdir(parents=True, exist_ok=True)
    path = plist_path(system.agents_dir)
    if is_loaded(system):
        system.run([LAUNCHCTL, "bootout", service_target(system.uid)])
    with path.open("wb") as handle:
        plistlib.dump(build_plist(python, target, hour, minute, working_dir, log), handle)
    _bootstrap(system, path)
    return path


def uninstall(system: ScheduleSystem) -> bool:
    """Unloads the schedule and deletes its file.

    Args:
        system: The machine.

    Returns:
        True when there was a schedule to remove.
    """
    path = plist_path(system.agents_dir)
    loaded = is_loaded(system)
    if loaded:
        system.run([LAUNCHCTL, "bootout", service_target(system.uid)])
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return loaded or existed


def _read_plist(path: Path) -> dict:
    """Reads the schedule file, treating a damaged one as empty.

    Args:
        path: The schedule file.

    Returns:
        Its content, empty when it cannot be read.
    """
    try:
        with path.open("rb") as handle:
            content = plistlib.load(handle)
    except (OSError, ValueError, ExpatError):
        return {}
    return content if isinstance(content, dict) else {}


def read_status(system: ScheduleSystem) -> ScheduleStatus:
    """Collects what the schedule file and launchd say about the schedule.

    Args:
        system: The machine.

    Returns:
        The status.
    """
    path = plist_path(system.agents_dir)
    installed = path.is_file()
    content = _read_plist(path) if installed else {}
    interval = content.get("StartCalendarInterval") or {}
    arguments = content.get("ProgramArguments") or []
    log = content.get("StandardOutPath")
    time_text = None
    if isinstance(interval, dict) and "Hour" in interval and "Minute" in interval:
        time_text = f"{int(interval['Hour']):02d}:{int(interval['Minute']):02d}"
    result = system.run([LAUNCHCTL, "print", service_target(system.uid)])
    loaded = result.returncode == 0
    report = (result.stdout or "") if loaded else ""
    exit_code = EXIT_CODE_PATTERN.search(report)
    return ScheduleStatus(
        installed=installed,
        loaded=loaded,
        running=bool(RUNNING_PATTERN.search(report)),
        time=time_text,
        target=arguments[-1] if arguments and arguments[-1] in TARGETS else None,
        last_exit_code=int(exit_code[1]) if exit_code else None,
        log_path=Path(log) if log else None,
        plist_path=path,
    )


def _plain(text: object) -> None:
    """Prints a path or command on one line, exactly as it is.

    Args:
        text: What to print.
    """
    console.print(str(text), markup=False, highlight=False, soft_wrap=True)


def _fail(error: ScheduleError) -> typer.Exit:
    """Prints why a schedule command cannot go on.

    Args:
        error: The reason.

    Returns:
        The exit to raise.
    """
    console.print(f"[red]Error:[/red] {error}", soft_wrap=True)
    return typer.Exit(1)


@schedule_app.command("install")
def install_command(
    at: Annotated[
        str,
        typer.Option("--at", help="Time of day to run at, as HH:MM on the 24 hour clock."),
    ] = DEFAULT_TIME,
    target: Annotated[
        str,
        typer.Option("--platform", "-p", help="spotify, soundcloud or all."),
    ] = "all",
) -> None:
    """Run the sync every day at a set time, without a terminal."""
    settings = get_settings()
    system = current_system()
    working_dir = Path.cwd()
    log = log_path(settings.data_dir)
    try:
        hour, minute = parse_time(at)
        if system.name != MACOS:
            check_request(target, working_dir)
            console.print(
                "Installing the schedule by itself only works on macOS. "
                "On this system, add this line with 'crontab -e':"
            )
            _plain(crontab_line(sys.executable, target, hour, minute, working_dir, log))
            return
        path = install(system, sys.executable, target, hour, minute, working_dir, settings.data_dir)
    except ScheduleError as error:
        raise _fail(error) from error
    console.print(
        f"[green]Scheduled.[/green] The sync ({target}) now runs every day at {hour:02d}:{minute:02d}."
    )
    console.print("Schedule file:")
    _plain(path)
    console.print("Log of the scheduled runs:")
    _plain(log)
    console.print(
        "[dim]A scheduled run never opens a browser. Save both logins once with "
        "'song-automations login', and a notification tells you if one is needed again.[/dim]"
    )


@schedule_app.command("uninstall")
def uninstall_command() -> None:
    """Stop the daily run and remove its schedule file."""
    system = current_system()
    if system.name != MACOS:
        console.print(
            "Nothing was installed on this system. If you added the crontab line, "
            "remove it with 'crontab -e'."
        )
        return
    if uninstall(system):
        console.print("[green]The schedule was removed.[/green] The sync no longer runs by itself.")
    else:
        console.print("[yellow]No schedule was installed.[/yellow]")


@schedule_app.command("status")
def status_command() -> None:
    """Show whether the daily run is installed, when it runs and how the last run ended."""
    system = current_system()
    if system.name != MACOS:
        console.print(
            "The schedule is only installed by this tool on macOS. "
            "On this system, 'crontab -l' shows whether the line is there."
        )
        return
    status = read_status(system)
    if not status.installed and not status.loaded:
        console.print(
            "[yellow]Not installed.[/yellow] Run 'song-automations schedule install' to run the sync every day."
        )
        return
    if status.installed and status.loaded:
        console.print("[green]Installed and loaded.[/green]")
    elif status.installed:
        console.print(
            "[yellow]Installed but not loaded.[/yellow] Run 'song-automations schedule install' again."
        )
    else:
        console.print(
            "[yellow]Loaded, but its schedule file is gone.[/yellow] "
            "Run 'song-automations schedule install' again."
        )
    if status.time:
        console.print(f"Runs every day at {status.time}, syncing {status.target or 'an unknown platform'}.")
    if status.running:
        console.print("A scheduled run is going on right now.")
    if status.last_exit_code is None:
        console.print("Last run: launchd reports none yet.")
    else:
        meaning = EXIT_CODE_MEANINGS.get(status.last_exit_code, "ended with an error, see the log")
        console.print(f"Last run: exit code {status.last_exit_code} ({meaning}).")
    if status.log_path:
        console.print("Log of the scheduled runs:")
        _plain(status.log_path)
