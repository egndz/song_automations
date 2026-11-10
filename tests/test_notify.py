"""Tests for the desktop notification of a stopped unattended run."""

import subprocess
from datetime import datetime

import pytest

from song_automations import notify
from song_automations.errors import (
    LoginFailedError,
    LoginTimeoutError,
    RateLimitedError,
    ReauthRequiredError,
    SearchBudgetExhaustedError,
    UnattendedLoginError,
)
from song_automations.snapshots import SnapshotError

NOW = datetime(2026, 11, 10, 4, 30)


def _recorder(failure: Exception | None = None):
    """Builds a command runner that records instead of running.

    Args:
        failure: What running the command raises, None for a command that works.

    Returns:
        The runner and the list of commands it was given.
    """
    commands: list[list[str]] = []

    def run(command: list[str]) -> None:
        """Records the command and fails when the test asks for it.

        Args:
            command: The command and its arguments.
        """
        commands.append(command)
        if failure is not None:
            raise failure

    return run, commands


@pytest.mark.parametrize(
    "message",
    [
        "Spotify needs a new login.",
        'He said "hi" and left',
        "back\\slash and 'single' quotes",
        '" & do shell script "touch /tmp/owned" & "',
        "$(touch /tmp/owned); `id`",
    ],
)
def test_text_travels_as_arguments_not_as_script(message):
    """Whatever the text holds, it is an argument of its own and never part of the script."""
    command = notify.notification_command(message)
    assert command[0] == "/usr/bin/osascript"
    assert command[-2:] == [message, "Song Automations"]
    script = command[1:-2]
    assert script[0::2] == ["-e"] * len(notify.SCRIPT_LINES)
    assert tuple(script[1::2]) == notify.SCRIPT_LINES
    assert all(message not in part for part in script)


@pytest.mark.parametrize(
    ("system", "shown"),
    [("Darwin", True), ("Linux", False), ("Windows", False), ("FreeBSD", False)],
)
def test_only_macos_shows_a_notification(system, shown):
    """macOS runs the command once, every other system does nothing."""
    run, commands = _recorder()
    assert notify.notify("text", system=system, runner=run) is shown
    assert len(commands) == (1 if shown else 0)


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("osascript"),
        PermissionError("not allowed"),
        subprocess.TimeoutExpired("osascript", 10),
        RuntimeError("anything else"),
    ],
)
def test_failing_notification_never_fails_the_run(failure):
    """A notification that cannot be shown is swallowed and reported as not shown."""
    run, commands = _recorder(failure)
    assert notify.notify("text", system="Darwin", runner=run) is False
    assert len(commands) == 1


@pytest.mark.parametrize(
    ("error", "parts"),
    [
        (ReauthRequiredError("Spotify"), ["Spotify", "song-automations login spotify"]),
        (UnattendedLoginError("SoundCloud"), ["SoundCloud", "song-automations login soundcloud"]),
        (LoginTimeoutError("Spotify", 600), ["song-automations login spotify"]),
        (LoginFailedError("SoundCloud", "invalid_client"), ["song-automations login soundcloud"]),
        (RateLimitedError("Spotify", 3600), ["Spotify", "until 2026-11-10 05:30"]),
        (RateLimitedError("SoundCloud", 86400), ["SoundCloud", "until 2026-11-11 04:30"]),
        (SnapshotError("disk full"), ["could not save a playlist", "left as it was", "log"]),
    ],
)
def test_message_says_what_to_do(error, parts):
    """A login problem names the command to run, a rate limit says until when, a failed snapshot points at the log."""
    message = notify.attention_message(error, now=NOW)
    assert all(part in message for part in parts)


@pytest.mark.parametrize(
    "error",
    [SearchBudgetExhaustedError("Spotify", 400), ValueError("unrelated"), KeyError("x")],
)
def test_normal_stops_have_no_message(error):
    """A used-up search budget, or anything that is not a login or a limit, tells nobody."""
    run, commands = _recorder()
    assert notify.attention_message(error) is None
    assert notify.notify_attention(error, system="Darwin", runner=run) is False
    assert commands == []


@pytest.mark.parametrize(
    ("error", "system", "expected"),
    [
        (ReauthRequiredError("Spotify"), "Darwin", 1),
        (RateLimitedError("Spotify", 3600), "Darwin", 1),
        (ReauthRequiredError("Spotify"), "Linux", 0),
        (RateLimitedError("Spotify", 3600), "Windows", 0),
    ],
)
def test_attention_posts_one_notification_on_macos(error, system, expected):
    """One stop is one notification, and only on macOS."""
    run, commands = _recorder()
    assert notify.notify_attention(error, system=system, runner=run) is bool(expected)
    assert len(commands) == expected
    if expected:
        assert commands[0][-1] == "Song Automations"
        assert error.platform in commands[0][-2]
