"""Desktop notifications for runs nobody is watching."""

import logging
import platform
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta

from song_automations.errors import RateLimitedError, ReauthRequiredError

logger = logging.getLogger(__name__)

TITLE = "Song Automations"
OSASCRIPT = "/usr/bin/osascript"
MACOS = "Darwin"
TIMEOUT_SECONDS = 10
SCRIPT_LINES = (
    "on run argv",
    "display notification (item 1 of argv) with title (item 2 of argv)",
    "end run",
)

Runner = Callable[[list[str]], object]


def notification_command(message: str, title: str = TITLE) -> list[str]:
    """Builds the macOS command that shows a notification.

    The text travels as arguments of the script, so no quote or backslash in
    it can change what the script does, and no shell is involved.

    Args:
        message: Notification text.
        title: Notification title.

    Returns:
        The command and its arguments.
    """
    command = [OSASCRIPT]
    for line in SCRIPT_LINES:
        command += ["-e", line]
    return [*command, message, title]


def _run_command(command: list[str]) -> object:
    """Runs a command without a shell and without waiting long for it.

    Args:
        command: The command and its arguments.

    Returns:
        The finished process.
    """
    return subprocess.run(command, check=False, capture_output=True, timeout=TIMEOUT_SECONDS)


def notify(
    message: str,
    title: str = TITLE,
    system: str | None = None,
    runner: Runner | None = None,
) -> bool:
    """Shows a desktop notification on macOS and does nothing elsewhere.

    A notification that cannot be shown is only logged, it never fails the run.

    Args:
        message: Notification text.
        title: Notification title.
        system: Operating system name, None for the one this runs on.
        runner: Function that runs the command, None for the real one.

    Returns:
        True when the notification command ran.
    """
    if (system or platform.system()) != MACOS:
        return False
    try:
        (runner or _run_command)(notification_command(message, title))
    except Exception:
        logger.info("Desktop notification could not be shown", exc_info=True)
        return False
    return True


def attention_message(error: BaseException, now: datetime | None = None) -> str | None:
    """Words what the owner has to know about a stopped run.

    A used-up search budget is a normal stop and gets no message.

    Args:
        error: What stopped the run.
        now: Current time, None for the real one.

    Returns:
        The notification text, or None when nobody has to be told.
    """
    if isinstance(error, ReauthRequiredError):
        return (
            f"{error.platform} needs a new login. Open a terminal and run: "
            f"song-automations login {error.platform.lower()}"
        )
    if isinstance(error, RateLimitedError):
        until = (now or datetime.now()) + timedelta(seconds=error.retry_after_seconds)
        return (
            f"{error.platform} rate limited the app until {until:%Y-%m-%d %H:%M}. "
            "Nothing to do, scheduled runs after that carry on by themselves."
        )
    return None


def notify_attention(
    error: BaseException,
    system: str | None = None,
    runner: Runner | None = None,
) -> bool:
    """Tells the owner that an unattended run stopped and needs them.

    Args:
        error: What stopped the run.
        system: Operating system name, None for the one this runs on.
        runner: Function that runs the command, None for the real one.

    Returns:
        True when a notification was shown.
    """
    message = attention_message(error)
    if message is None:
        return False
    return notify(message, system=system, runner=runner)
