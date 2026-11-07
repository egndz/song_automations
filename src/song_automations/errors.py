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
    """

    def __init__(self, platform: str) -> None:
        self.platform = platform
        super().__init__(
            f"Your {platform} login has expired. Unfinished folders were left unchanged. "
            f"Run the sync again in a terminal and approve the {platform} page that opens."
        )


class SearchBudgetExhaustedError(RuntimeError):
    """A run used all the searches it may make, to stay under the platform quota.

    Args:
        platform: Platform name.
        budget: Searches allowed per run.
    """

    def __init__(self, platform: str, budget: int) -> None:
        self.platform = platform
        self.budget = budget
        super().__init__(
            f"This run used its {budget} {platform} searches. Finished folders were synced and "
            "every search so far is saved, so the next run continues where this one stopped."
        )
