"""Pytest fixtures for song_automations tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from song_automations import notify, schedule
from song_automations.config import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps every test away from the real .env and from exported settings.

    Settings built in a test read no .env file unless the test names one, and
    the application's variables are taken out of the environment, because some
    pytest plugins export the .env of the working directory before tests run.

    Args:
        monkeypatch: Restores the configuration and the environment afterwards.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)


class CommandGuard:
    """Refuses commands on the real machine and remembers each one it refused.

    Code that shows a notification swallows every error, the refusal too, so
    the refusal alone would let such a test pass. The guard is therefore asked
    again once the test is over.
    """

    def __init__(self) -> None:
        self.refused: list[list[str]] = []

    def refuse(self, command: list[str]) -> None:
        """Notes the command and fails instead of running it.

        Args:
            command: The command a test tried to run.

        Raises:
            AssertionError: Always.
        """
        self.refused.append(list(command))
        raise AssertionError(f"a test tried to run a real command: {command}")

    def check(self) -> None:
        """Fails the test when it tried to run a real command, even one whose refusal was swallowed."""
        if self.refused:
            pytest.fail(f"a test tried to run real commands: {self.refused}")


@pytest.fixture(autouse=True)
def no_real_system_commands(monkeypatch) -> Iterator[CommandGuard]:
    """Keeps every test from showing a real desktop notification or touching launchd.

    Args:
        monkeypatch: Pytest monkeypatch.

    Yields:
        The guard, which fails the test afterwards when a command was refused.
    """
    guard = CommandGuard()
    monkeypatch.setattr(notify, "_run_command", guard.refuse)
    monkeypatch.setattr(schedule, "_run_command", guard.refuse)
    yield guard
    guard.check()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Create test settings with dummy values that never touch the real data directory.

    Args:
        tmp_path: Temporary folder used as the data directory.

    Returns:
        Settings that ignore any .env file.
    """
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        discogs_user_token="test_token",
        spotify_client_id="test_spotify_id",
        spotify_client_secret="test_spotify_secret",
        soundcloud_client_id="test_soundcloud_id",
        soundcloud_client_secret="test_soundcloud_secret",
    )
