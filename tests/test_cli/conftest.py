"""Fixtures shared by the command-line tests."""

from collections.abc import Callable

import pytest

from song_automations.config import Settings


@pytest.fixture
def flat() -> Callable[[str], str]:
    """Provides a function that joins wrapped console output into one line.

    Returns:
        A function turning every run of whitespace into one space.
    """
    return lambda text: " ".join(text.split())


@pytest.fixture
def make_settings(tmp_path) -> Callable[..., Settings]:
    """Provides a factory for settings that keep every file inside the test folder.

    Args:
        tmp_path: Folder standing in for the data directory.

    Returns:
        A function building settings, with keyword arguments as overrides.
    """

    def build(**overrides) -> Settings:
        """Builds the settings.

        Args:
            **overrides: Settings to change.

        Returns:
            The settings.
        """
        values = {
            "discogs_user_token": "token",
            "spotify_client_id": "spotify-id",
            "spotify_client_secret": "spotify-secret",
            "soundcloud_client_id": "soundcloud-id",
            "soundcloud_client_secret": "soundcloud-secret",
            "data_dir": tmp_path,
        }
        return Settings(**{**values, **overrides})

    return build
