"""Tests for deciding whether subscriber-only SoundCloud tracks may match."""

import pytest

from song_automations import cli
from song_automations.config import Settings


class FakeClient:
    """SoundCloud stand-in.

    Args:
        subscribed: Whether the account plays the catalogue in full.
    """

    def __init__(self, subscribed: bool) -> None:
        self.subscribed = subscribed
        self.asked = 0
        self.enabled = None

    def has_full_catalogue(self) -> bool:
        """Reports the plan.

        Returns:
            The prepared answer.
        """
        self.asked += 1
        return self.subscribed

    def include_catalogue(self, enabled: bool) -> None:
        """Records the choice.

        Args:
            enabled: Chosen value.
        """
        self.enabled = enabled


@pytest.mark.parametrize(
    ("mode", "subscribed", "enabled", "asked"),
    [
        ("auto", True, True, 1),
        ("auto", False, False, 1),
        ("always", False, True, 0),
        ("never", True, False, 0),
    ],
)
def test_catalogue_follows_the_plan_unless_told(mode, subscribed, enabled, asked):
    """Auto asks the account once; always and never do not ask."""
    client = FakeClient(subscribed)
    cli._choose_catalogue(Settings(_env_file=None, soundcloud_catalogue=mode), client)
    assert (client.enabled, client.asked) == (enabled, asked)
