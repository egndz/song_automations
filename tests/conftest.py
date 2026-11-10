"""Pytest fixtures for song_automations tests."""

from pathlib import Path

import pytest

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
def isolated_data_dir(
    isolated_settings: None,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Points the data directory of every test at a temporary folder.

    Settings built without an explicit data directory would otherwise use the
    real one in the home folder, and a sync under test would save its playlist
    snapshots there.

    Args:
        isolated_settings: Clears the exported settings first, so the variable set here stays.
        tmp_path_factory: Pytest factory for temporary folders.
        monkeypatch: Restores the environment afterwards.

    Returns:
        The folder standing in for the data directory.
    """
    folder = tmp_path_factory.mktemp("data_dir")
    monkeypatch.setenv("DATA_DIR", str(folder))
    return folder


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
