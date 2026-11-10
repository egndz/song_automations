"""Tests for the snapshots the sync engine takes before it changes a playlist."""

from io import StringIO

import pytest
from rich.console import Console

from song_automations.config import Settings
from song_automations.state.tracker import StateTracker
from song_automations.sync.engine import SyncEngine, SyncResult
from test_snapshots.fakes import FakePlatform

NAME = "Discogs - Electro"


@pytest.fixture
def state(tmp_path) -> StateTracker:
    """Provides a real state database.

    Args:
        tmp_path: Temporary folder.

    Returns:
        The tracker.
    """
    return StateTracker(tmp_path / "state.db")


@pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
def test_a_playlist_left_alone_is_named_on_either_platform(state, platform):
    """The warning about a playlist nothing matched for works for a SoundCloud playlist too."""
    output = StringIO()
    fake = FakePlatform(platform, {NAME: ["1"]})
    engine = SyncEngine(Settings(_env_file=None), None, state, Console(file=output, width=500))
    engine._apply_playlist(fake.playlist(NAME), [], fake, platform, False, SyncResult())
    assert f"Nothing matched for '{NAME}'" in output.getvalue()
    assert fake.playlists[NAME] == ["1"]
