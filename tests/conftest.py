"""Pytest fixtures for song_automations tests."""

from pathlib import Path

import pytest

from song_automations.config import Settings


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
