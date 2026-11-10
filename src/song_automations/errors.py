"""Errors that stop a sync before any playlist is changed."""

from datetime import datetime, timedelta


class RateLimitedError(RuntimeError):
    """A platform refuses requests for longer than is worth waiting.

    Args:
        platform: Platform name.
        retry_after_seconds: Seconds until requests are accepted again.
    """

    def __init__(self, platform: str, retry_after_seconds: int) -> None:
        self.platform = platform
        self.retry_after_seconds = retry_after_seconds
        until = datetime.now() + timedelta(seconds=retry_after_seconds)
        super().__init__(
            f"{platform} has rate limited this app until {until:%Y-%m-%d %H:%M}. "
            "Folders finished before that were synced, the rest were left unchanged. "
            "Run the sync again after that time."
        )


class ReauthRequiredError(RuntimeError):
    """A platform login has expired and needs the browser again.

    Args:
        platform: Platform name.
        message: Text to show instead of the expired-login one.
    """

    def __init__(self, platform: str, message: str | None = None) -> None:
        self.platform = platform
        super().__init__(
            message
            or f"Your {platform} login has expired. Unfinished folders were left unchanged. "
            f"Run 'song-automations login {platform.lower()}' in a terminal, approve the "
            f"{platform} page that opens, then run the sync again."
        )


class LoginTimeoutError(ReauthRequiredError):
    """The approval page of a platform was not approved in time.

    Args:
        platform: Platform name.
        waited_seconds: How long the approval was waited for.
    """

    def __init__(self, platform: str, waited_seconds: int) -> None:
        self.waited_seconds = waited_seconds
        super().__init__(
            platform,
            f"The {platform} page was not approved within {waited_seconds} seconds. "
            f"Run 'song-automations login {platform.lower()}' in a terminal, approve the "
            f"{platform} page that opens, then run the command again. "
            "Set LOGIN_WAIT_SECONDS to wait longer.",
        )


class LoginFailedError(ReauthRequiredError):
    """A platform refused the login after the page was approved.

    Args:
        platform: Platform name.
        reason: What the platform answered.
    """

    def __init__(self, platform: str, reason: str) -> None:
        self.reason = reason
        super().__init__(
            platform,
            f"The {platform} login did not go through ({reason}). Check the {platform} client "
            f"id, client secret and redirect address in .env, then run "
            f"'song-automations login {platform.lower()}' again.",
        )


class UnattendedLoginError(ReauthRequiredError):
    """A run nobody is watching needs a login that only a browser can give.

    Args:
        platform: Platform name.
    """

    def __init__(self, platform: str) -> None:
        super().__init__(
            platform,
            f"{platform} has no working saved login, and this unattended run may not open "
            "a browser. Unfinished folders were left unchanged. Run "
            f"'song-automations login {platform.lower()}' in a terminal and approve the "
            f"{platform} page that opens. The next scheduled run then carries on by itself.",
        )


class WritesRefusedError(RuntimeError):
    """A platform refuses to let this account change playlists at all.

    SoundCloud answers this way to accounts without an Artist Pro
    subscription, so the same request would fail on every run.

    Args:
        platform: Platform name.
        reason: What the platform said.
    """

    def __init__(self, platform: str, reason: str) -> None:
        self.platform = platform
        self.reason = reason
        super().__init__(
            f"{platform} refused to change a playlist: {reason}. "
            f"Playlists on {platform} were left as they were."
        )


class SearchBudgetExhaustedError(RuntimeError):
    """The searches allowed in a day are used, so the run stops to stay under the platform quota.

    Args:
        platform: Platform name.
        budget: Searches allowed in the counting window, which is just under a day.
        resumes_at: When searches are allowed again, in seconds since the epoch, if known.
    """

    def __init__(self, platform: str, budget: int, resumes_at: float | None = None) -> None:
        self.platform = platform
        self.budget = budget
        self.resumes_at = resumes_at
        later = f"a run after {datetime.fromtimestamp(resumes_at):%H:%M on %d %b}" if resumes_at else "the next run"
        super().__init__(
            f"The {budget} {platform} searches allowed per day are used. Finished folders were synced "
            f"and every search so far is saved, so {later} continues where this one stopped."
        )
