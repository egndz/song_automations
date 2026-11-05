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
            "Nothing was changed. Run the sync again after that time."
        )


class ReauthRequiredError(RuntimeError):
    """A platform login has expired and needs the browser again.

    Args:
        platform: Platform name.
    """

    def __init__(self, platform: str) -> None:
        self.platform = platform
        super().__init__(
            f"Your {platform} login has expired. Nothing was changed. "
            f"Run the sync again in a terminal and approve the {platform} page that opens."
        )
