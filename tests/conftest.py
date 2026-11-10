"""Pytest fixtures for song_automations tests."""

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


@pytest.fixture(autouse=True)
def no_real_system_commands(monkeypatch) -> None:
    """Keeps every test from showing a real desktop notification or touching launchd.

    Args:
        monkeypatch: Pytest monkeypatch.
    """

    def refuse(command: list[str]) -> None:
        """Fails instead of running a command on the real machine.

        Args:
            command: The command a test tried to run.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError(f"a test tried to run a real command: {command}")

    monkeypatch.setattr(notify, "_run_command", refuse)
    monkeypatch.setattr(schedule, "_run_command", refuse)


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
