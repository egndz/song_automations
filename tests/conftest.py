"""Pytest fixtures for song_automations tests."""

import pytest

from song_automations.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Create test settings with dummy values."""
    return Settings(
        discogs_user_token="test_token",
        spotify_client_id="test_spotify_id",
        spotify_client_secret="test_spotify_secret",
        soundcloud_client_id="test_soundcloud_id",
        soundcloud_client_secret="test_soundcloud_secret",
        min_confidence=0.30,
        high_confidence=0.50,
    )
