"""Tests for the guard that keeps tests from running commands on the real machine."""

import pytest

from song_automations import notify, schedule
from song_automations.errors import ReauthRequiredError


@pytest.mark.parametrize(
    ("module", "command"),
    [
        (notify, [notify.OSASCRIPT, "-e", "display notification"]),
        (schedule, [schedule.LAUNCHCTL, "print", "gui/501/com.songautomations.sync"]),
    ],
)
def test_refused_command_is_remembered_and_fails_the_test_afterwards(no_real_system_commands, module, command):
    """A real command is refused when it is tried, and the test is failed again once it is over."""
    guard = no_real_system_commands
    with pytest.raises(AssertionError, match="real command"):
        module._run_command(command)
    assert guard.refused == [command]
    with pytest.raises(pytest.fail.Exception, match="real commands"):
        guard.check()
    guard.refused.clear()


@pytest.mark.parametrize("platform", ["Spotify", "SoundCloud"])
def test_swallowed_refusal_still_fails_the_test(no_real_system_commands, platform):
    """A notification swallows the refusal and reports nothing shown, yet the guard remembers it."""
    guard = no_real_system_commands
    assert notify.notify_attention(ReauthRequiredError(platform), system=notify.MACOS) is False
    assert [command[0] for command in guard.refused] == [notify.OSASCRIPT]
    with pytest.raises(pytest.fail.Exception, match="osascript"):
        guard.check()
    guard.refused.clear()


@pytest.mark.parametrize("calls", [0])
def test_quiet_test_passes_the_guard(no_real_system_commands, calls):
    """A test that runs no real command is left alone."""
    assert len(no_real_system_commands.refused) == calls
    no_real_system_commands.check()
