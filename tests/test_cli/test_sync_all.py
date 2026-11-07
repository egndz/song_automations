"""Tests for the sync all command."""

import pytest
import typer
from typer.testing import CliRunner

from song_automations import cli


@pytest.mark.parametrize(("spotify_exit", "expected_exit"), [(None, 0), (0, 0), (2, 2)])
def test_soundcloud_still_runs_when_spotify_stops(monkeypatch, spotify_exit, expected_exit):
    """A Spotify budget stop, block or expired login never skips SoundCloud."""
    calls = []

    def spotify(**kwargs):
        """Stands in for the Spotify sync.

        Args:
            **kwargs: Command options.
        """
        calls.append("spotify")
        if spotify_exit is not None:
            raise typer.Exit(spotify_exit)

    def soundcloud(**kwargs):
        """Stands in for the SoundCloud sync.

        Args:
            **kwargs: Command options.
        """
        calls.append("soundcloud")

    monkeypatch.setattr(cli, "sync_spotify", spotify)
    monkeypatch.setattr(cli, "sync_soundcloud", soundcloud)
    result = CliRunner().invoke(cli.app, ["sync", "all"])
    assert calls == ["spotify", "soundcloud"]
    assert result.exit_code == expected_exit
