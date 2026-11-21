"""Tests for the setlist command."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from song_automations import cli
from song_automations.errors import SearchBudgetExhaustedError

runner = CliRunner()
TRACKLIST = "Bicep - Glue\nOvermono - So U Kno\n"
GLUE = {"id": "glue", "name": "Glue", "artists": [{"name": "Bicep"}], "duration_ms": 200000, "album": {"name": "x"}}


class FakeSpotify:
    """Spotify client that knows one track.

    Args:
        settings: Settings, unused.
    """

    playlists: dict[str, list[str]] = {}
    error: Exception | None = None

    def __init__(self, settings) -> None:
        self.settings = settings

    def search_raw(self, query, kind="track"):
        """Searches.

        Args:
            query: Query.
            kind: track or album.

        Returns:
            Raw tracks.
        """
        if self.error:
            raise self.error
        return [GLUE] if "Glue" in query else []

    def find_playlist_by_name(self, name):
        """Finds a playlist.

        Args:
            name: Name.

        Returns:
            The playlist or None.
        """
        return SimpleNamespace(id=name, name=name) if name in self.playlists else None

    def get_playlist_tracks(self, playlist_id):
        """Lists the tracks.

        Args:
            playlist_id: Playlist id.

        Returns:
            Tracks.
        """
        return [SimpleNamespace(id=track_id) for track_id in self.playlists[playlist_id]]

    def create_playlist(self, name, description="", public=True):
        """Creates a playlist.

        Args:
            name: Name.
            description: Description.
            public: Visibility.

        Returns:
            The playlist.
        """
        self.playlists[name] = []
        return SimpleNamespace(id=name, name=name)

    def add_tracks_to_playlist(self, playlist_id, uris):
        """Adds tracks.

        Args:
            playlist_id: Playlist id.
            uris: Track uris.
        """
        self.playlists[playlist_id] += [uri.rsplit(":", 1)[-1] for uri in uris]


@pytest.fixture
def spotify(monkeypatch, make_settings):
    """Puts the fake Spotify client and test settings behind the command.

    Args:
        monkeypatch: Pytest patcher.
        make_settings: Settings factory.

    Returns:
        The fake client class.
    """
    monkeypatch.setattr(FakeSpotify, "playlists", {})
    monkeypatch.setattr(FakeSpotify, "error", None)
    monkeypatch.setattr(cli, "SpotifyClient", FakeSpotify)
    monkeypatch.setattr(cli, "_init_settings", lambda: make_settings())
    return FakeSpotify


@pytest.mark.parametrize(
    ("options", "playlists", "said"),
    [
        ([], {"Set - Boiler Room": ["glue"]}, "1 added to the new playlist 'Set - Boiler Room', 0 already there."),
        (["--name", "Sunday"], {"Sunday": ["glue"]}, "1 added to the new playlist 'Sunday'"),
        (["--dry-run"], {}, "1 would be added to the new playlist 'Set - Boiler Room'"),
    ],
)
def test_setlist_fills_a_playlist_from_a_file(spotify, tmp_path, flat, options, playlists, said):
    """Found tracks go in the playlist, missing ones are listed as not found."""
    source = tmp_path / "Boiler Room.txt"
    source.write_text(TRACKLIST, encoding="utf-8")

    result = runner.invoke(cli.app, ["setlist", str(source), *options])

    output = flat(result.output)
    assert result.exit_code == 0
    assert "1 of 2 tracks found on Spotify." in output
    assert "not found" in output
    assert said in output
    assert spotify.playlists == playlists


@pytest.mark.parametrize(
    ("text", "said"),
    [("Thanks for watching\n", "No tracklist found"), (None, "neither a link nor a file")],
)
def test_setlist_says_when_there_is_nothing_to_read(spotify, tmp_path, flat, text, said):
    """A source without a tracklist ends the command before anything is searched."""
    source = tmp_path / "notes.txt"
    if text is not None:
        source.write_text(text, encoding="utf-8")

    result = runner.invoke(cli.app, ["setlist", str(source)])

    assert result.exit_code == 1
    assert said in flat(result.output)
    assert spotify.playlists == {}


def test_setlist_stops_plainly_when_the_searches_are_used(spotify, tmp_path, flat):
    """A used-up search allowance is a normal stop that changes no playlist."""
    spotify.error = SearchBudgetExhaustedError("Spotify", 400, None)
    source = tmp_path / "Boiler Room.txt"
    source.write_text(TRACKLIST, encoding="utf-8")

    result = runner.invoke(cli.app, ["setlist", str(source)])

    assert result.exit_code == 0
    assert "400" in flat(result.output)
    assert spotify.playlists == {}
