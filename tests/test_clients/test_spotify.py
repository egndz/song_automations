"""Tests for Spotify client."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOauthError

from song_automations.clients.spotify import (
    DEFAULT_RETRY_AFTER_SECONDS,
    SpotifyClient,
    SpotifyPlaylist,
    SpotifyTrack,
    TimedSpotifyOAuth,
    retry_after_seconds,
)
from song_automations.config import Settings
from song_automations.errors import (
    LoginFailedError,
    LoginTimeoutError,
    RateLimitedError,
    ReauthRequiredError,
    SearchBudgetExhaustedError,
    UnattendedLoginError,
)


class TestSpotifyTrack:
    """Tests for SpotifyTrack dataclass."""

    @pytest.mark.parametrize(
        "track_id,name,artist,popularity,duration_ms",
        [
            ("abc123", "Test Track", "Test Artist", 75, 180000),
            ("def456", "Another", "Artist", 0, 240000),
            ("ghi789", "Popular", "Famous DJ", 100, 360000),
        ],
    )
    def test_track_properties(
        self, track_id, name, artist, popularity, duration_ms
    ):
        """Track should store properties correctly."""
        track = SpotifyTrack(
            id=track_id,
            uri=f"spotify:track:{track_id}",
            name=name,
            artist=artist,
            artists=[artist],
            album="Test Album",
            popularity=popularity,
            duration_ms=duration_ms,
            is_playable=True,
        )
        assert track.id == track_id
        assert track.name == name
        assert track.artist == artist
        assert track.popularity == popularity
        assert track.duration_ms == duration_ms

    def test_track_full_title(self):
        """Full title should combine artist and name."""
        track = SpotifyTrack(
            id="abc123",
            uri="spotify:track:abc123",
            name="Test Track",
            artist="Test Artist",
            artists=["Test Artist"],
            album="Test Album",
            popularity=50,
            duration_ms=180000,
            is_playable=True,
        )
        assert track.full_title == "Test Artist - Test Track"

    def test_track_multiple_artists(self):
        """Track should store multiple artists correctly."""
        track = SpotifyTrack(
            id="abc123",
            uri="spotify:track:abc123",
            name="Collaboration",
            artist="Artist A",
            artists=["Artist A", "Artist B", "Artist C"],
            album="Collab Album",
            popularity=80,
            duration_ms=200000,
            is_playable=True,
        )
        assert track.artist == "Artist A"
        assert len(track.artists) == 3
        assert track.artists == ["Artist A", "Artist B", "Artist C"]

    @pytest.mark.parametrize(
        "is_playable",
        [True, False],
    )
    def test_track_playability(self, is_playable):
        """Track should store playability correctly."""
        track = SpotifyTrack(
            id="abc123",
            uri="spotify:track:abc123",
            name="Test",
            artist="Artist",
            artists=["Artist"],
            album="Album",
            popularity=50,
            duration_ms=180000,
            is_playable=is_playable,
        )
        assert track.is_playable == is_playable


class TestSpotifyPlaylist:
    """Tests for SpotifyPlaylist dataclass."""

    @pytest.mark.parametrize(
        "playlist_id,name,track_count,public",
        [
            ("playlist1", "My Playlist", 10, True),
            ("playlist2", "Empty Playlist", 0, True),
            ("playlist3", "Private Playlist", 5, False),
        ],
    )
    def test_playlist_properties(self, playlist_id, name, track_count, public):
        """Playlist should store properties correctly."""
        playlist = SpotifyPlaylist(
            id=playlist_id,
            uri=f"spotify:playlist:{playlist_id}",
            name=name,
            owner_id="user123",
            track_count=track_count,
            public=public,
        )
        assert playlist.id == playlist_id
        assert playlist.name == name
        assert playlist.track_count == track_count
        assert playlist.public == public

    def test_playlist_uri_format(self):
        """Playlist URI should follow Spotify format."""
        playlist = SpotifyPlaylist(
            id="37i9dQZF1DXcBWIGoYBM5M",
            uri="spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
            name="Today's Top Hits",
            owner_id="spotify",
            track_count=50,
            public=True,
        )
        assert playlist.uri.startswith("spotify:playlist:")
        assert playlist.id in playlist.uri


class TestParseTrack:
    """Tests for track parsing from API response."""

    @pytest.fixture
    def mock_spotify_client(self, settings, monkeypatch):
        """Create a mocked SpotifyClient for testing."""
        monkeypatch.setattr("spotipy.Spotify", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "spotipy.oauth2.SpotifyOAuth",
            lambda *args, **kwargs: None
        )

        from song_automations.clients.spotify import SpotifyClient
        return SpotifyClient(settings)

    @pytest.mark.parametrize(
        "item,expected_id,expected_name,expected_artist",
        [
            (
                {
                    "id": "abc123",
                    "uri": "spotify:track:abc123",
                    "name": "Track Title",
                    "artists": [{"name": "Artist Name"}],
                    "album": {"name": "Album Name"},
                    "popularity": 75,
                    "duration_ms": 180000,
                    "is_playable": True,
                },
                "abc123",
                "Track Title",
                "Artist Name",
            ),
            (
                {
                    "id": "def456",
                    "uri": "spotify:track:def456",
                    "name": "Another Track",
                    "artists": [{"name": "Another Artist"}, {"name": "Featured Artist"}],
                    "album": {"name": "Another Album"},
                    "popularity": 50,
                    "duration_ms": 240000,
                    "is_playable": True,
                },
                "def456",
                "Another Track",
                "Another Artist",
            ),
        ],
    )
    def test_parse_track(
        self, mock_spotify_client, item, expected_id, expected_name, expected_artist
    ):
        """Parse track should extract correct fields from API response."""
        track = mock_spotify_client._parse_track(item)
        assert track.id == expected_id
        assert track.name == expected_name
        assert track.artist == expected_artist

    def test_parse_track_missing_album(self, mock_spotify_client):
        """Parse track should handle missing album gracefully."""
        item = {
            "id": "abc123",
            "uri": "spotify:track:abc123",
            "name": "Orphan Track",
            "artists": [{"name": "Some Artist"}],
            "popularity": 0,
            "duration_ms": 180000,
            "is_playable": True,
        }
        track = mock_spotify_client._parse_track(item)
        assert track.id == "abc123"
        assert track.name == "Orphan Track"
        assert track.album == ""

    def test_parse_track_empty_artists_list(self, mock_spotify_client):
        """Parse track should handle empty artists list."""
        item = {
            "id": "abc123",
            "uri": "spotify:track:abc123",
            "name": "Mystery Track",
            "artists": [],
            "album": {"name": "Mystery Album"},
            "popularity": 0,
            "duration_ms": 180000,
            "is_playable": True,
        }
        track = mock_spotify_client._parse_track(item)
        assert track.artist == "Unknown Artist"
        assert track.artists == []

    def test_parse_track_missing_optional_fields(self, mock_spotify_client):
        """Parse track should handle missing optional fields."""
        item = {
            "id": "abc123",
            "uri": "spotify:track:abc123",
            "name": "Minimal Track",
            "artists": [{"name": "Artist"}],
            "album": {"name": "Album"},
        }
        track = mock_spotify_client._parse_track(item)
        assert track.popularity == 0
        assert track.duration_ms == 0
        assert track.is_playable is True

    def test_parse_track_multiple_artists_preserves_all(self, mock_spotify_client):
        """Parse track should preserve all artists in the list."""
        item = {
            "id": "abc123",
            "uri": "spotify:track:abc123",
            "name": "Collab Track",
            "artists": [
                {"name": "Artist A"},
                {"name": "Artist B"},
                {"name": "Artist C"},
            ],
            "album": {"name": "Collab Album"},
            "popularity": 80,
            "duration_ms": 200000,
            "is_playable": True,
        }
        track = mock_spotify_client._parse_track(item)
        assert track.artist == "Artist A"
        assert track.artists == ["Artist A", "Artist B", "Artist C"]
        assert len(track.artists) == 3


class TestSpotifyRateLimitsAndNewApi:
    """Long limits fail clearly and the 2026 playlist shape is understood."""

    @staticmethod
    def _client(monkeypatch):
        """Builds a client with a mocked spotipy.

        Args:
            monkeypatch: Pytest monkeypatch.

        Returns:
            The client.
        """
        monkeypatch.setattr("song_automations.clients.spotify.time.sleep", lambda _: None)
        client = SpotifyClient.__new__(SpotifyClient)
        client._client = MagicMock()
        client._last_search = 0.0
        client._searches = 0
        client._search_budget = 1000
        return client

    @pytest.mark.parametrize("budget", [1, 3])
    def test_search_budget_stops_the_run(self, monkeypatch, budget):
        """Searches beyond the budget raise before any request is sent."""
        client = self._client(monkeypatch)
        client._search_budget = budget
        client._client.search.return_value = {"tracks": {"items": []}}
        for _ in range(budget):
            client.search_raw("q")
        with pytest.raises(SearchBudgetExhaustedError):
            client.album_raw("a")
        assert client._client.search.call_count == budget
        assert client._client.album.call_count == 0

    @pytest.mark.parametrize("wait", [61, 79320])
    def test_long_limit_raises_at_once(self, monkeypatch, wait):
        """A wait of hours becomes an error, never a silent hang."""
        client = self._client(monkeypatch)
        client._client.search.side_effect = SpotifyException(429, -1, "limited", headers={"Retry-After": str(wait)})
        with pytest.raises(RateLimitedError):
            client.search_raw("q")
        assert client._client.search.call_count == 1

    def test_short_limit_is_waited_out(self, monkeypatch):
        """A short limit retries on its own."""
        client = self._client(monkeypatch)
        client._client.search.side_effect = [
            SpotifyException(429, -1, "limited", headers={"Retry-After": "2"}),
            {"tracks": {"items": [{"id": "1"}]}},
        ]
        assert client.search_raw("q") == [{"id": "1"}]

    @pytest.mark.parametrize("field", ["item", "track"])
    def test_playlist_reads_new_and_old_field_names(self, monkeypatch, field):
        """Both the current and the previous response shape are read."""
        client = self._client(monkeypatch)
        raw = {"id": "1", "uri": "u", "name": "n", "artists": [{"name": "a"}]}
        client._client.playlist_items.return_value = {"items": [{field: raw}, {field: None}], "next": None}
        assert [t.id for t in client.get_playlist_tracks("p")] == ["1"]

    def test_expired_login_is_named(self, monkeypatch):
        """An expired refresh token asks for the browser, plainly."""
        client = self._client(monkeypatch)
        client._client.search.side_effect = SpotifyOauthError("invalid_grant")
        with pytest.raises(ReauthRequiredError):
            client.search_raw("q")


class TestRetryAfterHeader:
    """The wait Spotify asks for is read without ever raising."""

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"Retry-After": "7"}, 7),
            ({"Retry-After": 12}, 12),
            ({"Retry-After": "2.6"}, 2),
            ({"Retry-After": " 30 "}, 30),
            ({"Retry-After": "-4"}, 0),
            ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": ""}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": None}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": "inf"}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": "nan"}, DEFAULT_RETRY_AFTER_SECONDS),
            ({"Retry-After": ["3"]}, DEFAULT_RETRY_AFTER_SECONDS),
            ({}, DEFAULT_RETRY_AFTER_SECONDS),
            (None, DEFAULT_RETRY_AFTER_SECONDS),
            ("not a mapping", DEFAULT_RETRY_AFTER_SECONDS),
        ],
    )
    def test_header_is_read_or_defaulted(self, headers, expected):
        """A missing, empty or malformed header falls back to the default wait."""
        assert retry_after_seconds(headers) == expected

    @pytest.mark.parametrize("headers", [None, {}, {"Retry-After": "soon"}, {"Retry-After": None}])
    def test_limit_without_usable_header_is_waited_out(self, monkeypatch, headers):
        """A limit with a broken header retries after the default wait instead of crashing."""
        slept = []
        monkeypatch.setattr("song_automations.clients.spotify.time.sleep", slept.append)
        client = SpotifyClient.__new__(SpotifyClient)
        client._client = MagicMock()
        client._last_search = 0.0
        client._searches = 0
        client._search_budget = 1000
        client._client.search.side_effect = [
            SpotifyException(429, -1, "limited", headers=headers),
            {"tracks": {"items": [{"id": "1"}]}},
        ]
        assert client.search_raw("q") == [{"id": "1"}]
        assert DEFAULT_RETRY_AFTER_SECONDS + 1 in slept


class TestSpotifyLogin:
    """Logging in from the browser and replacing a saved login Spotify refuses."""

    @staticmethod
    def _client(tmp_path, monkeypatch, saved=True):
        """Builds a client whose spotipy and login are fakes, so nothing leaves the machine.

        Args:
            tmp_path: Folder standing in for the data directory.
            monkeypatch: Pytest monkeypatch.
            saved: Whether a saved login exists.

        Returns:
            The client.
        """
        monkeypatch.setattr("song_automations.clients.spotify.time.sleep", lambda _: None)
        settings = Settings(
            spotify_client_id="id", spotify_client_secret="secret", data_dir=tmp_path
        )
        client = SpotifyClient(settings)
        client._client = MagicMock()
        client._auth = MagicMock()
        if saved:
            client._cache_path.write_text("old login")
            client._cache_path.chmod(0o644)

        def approve(**kwargs):
            """Stands in for an approved browser login.

            Args:
                **kwargs: Options spotipy would get.

            Returns:
                A token.
            """
            client._cache_path.write_text("new login")
            client._cache_path.chmod(0o644)
            return "token"

        client._auth.get_access_token.side_effect = approve
        return client

    @pytest.mark.parametrize("code", ["invalid_client", "invalid_grant"])
    def test_refused_saved_login_is_set_aside_and_replaced(self, tmp_path, monkeypatch, code):
        """A login saved for another app is renamed, a new one is asked for, and the call goes on."""
        client = self._client(tmp_path, monkeypatch)
        client._client.search.side_effect = [
            SpotifyOauthError(f"error: {code}", error=code),
            {"tracks": {"items": [{"id": "1"}]}},
        ]
        assert client.search_raw("q") == [{"id": "1"}]
        (stale,) = tmp_path.glob(".spotify_cache.stale-*")
        assert stale.read_text() == "old login"
        assert client._cache_path.read_text() == "new login"
        assert stale.stat().st_mode & 0o777 == 0o600
        assert client._cache_path.stat().st_mode & 0o777 == 0o600
        client._auth.get_access_token.assert_called_once_with(as_dict=False, check_cache=False)

    @pytest.mark.parametrize(
        ("earlier", "expected_name"),
        [
            ([], ".spotify_cache.stale-20261110-043000"),
            ([".spotify_cache.stale"], ".spotify_cache.stale-20261110-043000"),
            ([".spotify_cache.stale-20261109-043000"], ".spotify_cache.stale-20261110-043000"),
            ([".spotify_cache.stale-20261110-043000"], ".spotify_cache.stale-20261110-043000-2"),
            (
                [".spotify_cache.stale-20261110-043000", ".spotify_cache.stale-20261110-043000-2"],
                ".spotify_cache.stale-20261110-043000-3",
            ),
        ],
    )
    def test_earlier_set_aside_login_is_never_overwritten(
        self, tmp_path, monkeypatch, capsys, earlier, expected_name
    ):
        """Each refused login gets its own dated name, and one line says where it went."""

        class FixedClock:
            """Clock that always shows the same moment."""

            @staticmethod
            def now():
                """Tells the time.

                Returns:
                    Half past four on the tenth of November 2026.
                """
                return datetime(2026, 11, 10, 4, 30, 0)

        monkeypatch.setattr("song_automations.clients.spotify.datetime", FixedClock)
        client = self._client(tmp_path, monkeypatch)
        for name in earlier:
            (tmp_path / name).write_text(f"kept {name}")
        client._client.search.side_effect = [
            SpotifyOauthError("error: invalid_grant", error="invalid_grant"),
            {"tracks": {"items": []}},
        ]
        client.search_raw("q")
        assert (tmp_path / expected_name).read_text() == "old login"
        assert (tmp_path / expected_name).stat().st_mode & 0o777 == 0o600
        assert all((tmp_path / name).read_text() == f"kept {name}" for name in earlier)
        printed = capsys.readouterr().out
        assert printed.count("\n") == 1
        assert "The old login was moved to" in printed
        assert str(tmp_path / expected_name) in printed

    @pytest.mark.parametrize("code", ["invalid_client", "invalid_grant"])
    def test_login_refused_twice_stops_instead_of_looping(self, tmp_path, monkeypatch, code):
        """When the new login is refused too, the run stops with a plain message."""
        client = self._client(tmp_path, monkeypatch)
        client._client.search.side_effect = SpotifyOauthError(f"error: {code}", error=code)
        with pytest.raises(ReauthRequiredError):
            client.search_raw("q")
        assert client._client.search.call_count == 2
        assert client._auth.get_access_token.call_count == 1

    @pytest.mark.parametrize("code", [None, "server_error", "access_denied"])
    def test_other_login_errors_leave_the_saved_login_alone(self, tmp_path, monkeypatch, code):
        """Only a refused saved login is replaced, any other login error is just reported."""
        client = self._client(tmp_path, monkeypatch)
        client._client.search.side_effect = SpotifyOauthError("failed", error=code)
        with pytest.raises(ReauthRequiredError):
            client.search_raw("q")
        assert client._cache_path.read_text() == "old login"
        assert not list(tmp_path.glob(".spotify_cache.stale*"))
        assert client._auth.get_access_token.call_count == 0

    @pytest.mark.parametrize("saved", [True, False])
    def test_login_asks_the_browser_even_with_a_saved_login(self, tmp_path, monkeypatch, saved):
        """Logging in never trusts the saved login and leaves the new one private."""
        client = self._client(tmp_path, monkeypatch, saved=saved)
        client.login()
        client._auth.get_access_token.assert_called_once_with(as_dict=False, check_cache=False)
        assert client._cache_path.stat().st_mode & 0o777 == 0o600
        assert not list(tmp_path.glob(".spotify_cache.stale*"))

    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (SpotifyOauthError("error: invalid_client", error="invalid_client"), LoginFailedError),
            (SpotifyOauthError("Received error from auth server: access_denied"), LoginFailedError),
            (OSError("Address already in use"), LoginFailedError),
            (LoginTimeoutError("Spotify", 600), LoginTimeoutError),
        ],
    )
    def test_failed_login_is_explained(self, tmp_path, monkeypatch, raised, expected):
        """A refused, impossible or unanswered login becomes a message that names the command."""
        client = self._client(tmp_path, monkeypatch)
        client._auth.get_access_token.side_effect = raised
        with pytest.raises(expected) as caught:
            client.login()
        assert "song-automations login spotify" in str(caught.value)
        assert isinstance(caught.value, ReauthRequiredError)
        assert client._cache_path.read_text() == "old login"

    @pytest.mark.parametrize("wait", [1, 600, 1800])
    def test_wait_comes_from_the_settings(self, tmp_path, wait):
        """The client waits for the approval as long as the settings say."""
        settings = Settings(
            spotify_client_id="id",
            spotify_client_secret="secret",
            data_dir=tmp_path,
            login_wait_seconds=wait,
        )
        client = SpotifyClient(settings)
        assert isinstance(client._auth, TimedSpotifyOAuth)
        assert client._auth._wait_seconds == wait


class FakeCallbackServer:
    """Local server that never touches the network.

    Args:
        answers: What each handled request brings, as (code, error) pairs.
        clock: Shared fake clock, advanced by a second per handled request.
    """

    def __init__(self, answers, clock):
        self.auth_code = None
        self.error = None
        self.timeout = None
        self.closed = False
        self.timeouts = []
        self._answers = list(answers)
        self._clock = clock

    def handle_request(self):
        """Handles one request, or lets a second pass when none is left."""
        self.timeouts.append(self.timeout)
        self._clock[0] += 1
        if self._answers:
            self.auth_code, self.error = self._answers.pop(0)

    def server_close(self):
        """Records that the port was released."""
        self.closed = True


class TestTimedSpotifyOAuth:
    """The wait for the Spotify approval page ends."""

    @staticmethod
    def _auth(tmp_path, monkeypatch, answers, wait):
        """Builds a login whose browser, clock and local server are fakes.

        Args:
            tmp_path: Folder for the saved login.
            monkeypatch: Pytest monkeypatch.
            answers: What each handled request brings, as (code, error) pairs.
            wait: Seconds to wait for the approval.

        Returns:
            The login and its fake server.
        """
        clock = [0.0]
        server = FakeCallbackServer(answers, clock)
        monkeypatch.setattr("song_automations.clients.spotify.time.monotonic", lambda: clock[0])
        monkeypatch.setattr(
            "song_automations.clients.spotify.start_local_http_server", lambda port: server
        )
        auth = TimedSpotifyOAuth(
            wait_seconds=wait,
            client_id="id",
            client_secret="secret",
            redirect_uri="http://127.0.0.1:8888/callback",
            cache_path=str(tmp_path / "cache"),
        )
        monkeypatch.setattr(auth, "_open_auth_url", lambda: None)
        return auth, server

    @pytest.mark.parametrize(
        ("answers", "handled"),
        [
            ([("code-1", None)], 1),
            ([(None, None), ("code-1", None)], 2),
            ([(None, None), (None, None), ("code-1", None)], 3),
        ],
    )
    def test_code_is_returned_once_the_page_is_approved(self, tmp_path, monkeypatch, answers, handled):
        """Stray requests are skipped until the approval arrives, then the port is released."""
        auth, server = self._auth(tmp_path, monkeypatch, answers, wait=10)
        assert auth._get_auth_response_local_server(8888) == "code-1"
        assert len(server.timeouts) == handled
        assert server.closed

    @pytest.mark.parametrize("wait", [1, 3, 600])
    def test_unanswered_page_times_out(self, tmp_path, monkeypatch, wait):
        """Nobody approving ends the wait with a plain message after the configured time."""
        auth, server = self._auth(tmp_path, monkeypatch, [], wait=wait)
        with pytest.raises(LoginTimeoutError) as caught:
            auth._get_auth_response_local_server(8888)
        assert f"{wait} seconds" in str(caught.value)
        assert server.timeouts[0] == wait
        assert len(server.timeouts) == wait
        assert server.closed

    @pytest.mark.parametrize("reason", ["access_denied", "server_error"])
    def test_refusal_on_the_page_is_raised(self, tmp_path, monkeypatch, reason):
        """Refusing on the page is reported as what it is, not as a timeout."""
        refusal = SpotifyOauthError(f"Received error from auth server: {reason}", error=reason)
        auth, server = self._auth(tmp_path, monkeypatch, [(None, refusal)], wait=10)
        with pytest.raises(SpotifyOauthError) as caught:
            auth._get_auth_response_local_server(8888)
        assert caught.value.error == reason
        assert server.closed


class FakeTerminal:
    """Standard input that only says whether a person can type into it.

    Args:
        interactive: Whether it is a terminal.
    """

    def __init__(self, interactive):
        self._interactive = interactive

    def isatty(self):
        """Tells whether a person can type here.

        Returns:
            True for a terminal.
        """
        return self._interactive


class TestSpotifyUnattended:
    """An unattended run never opens a browser, waits for one or asks for typed input."""

    @staticmethod
    def _forbid_asking(monkeypatch):
        """Makes every way of asking a person, and the network, fail the test.

        Args:
            monkeypatch: Pytest monkeypatch.

        Returns:
            A list that receives the name of anything that was tried anyway.
        """
        tried = []

        def forbid(name):
            """Builds a stand-in that records the attempt and fails.

            Args:
                name: What was tried.

            Returns:
                The stand-in.
            """

            def refuse(*args, **kwargs):
                """Records the attempt and fails.

                Args:
                    *args: Ignored.
                    **kwargs: Ignored.

                Raises:
                    AssertionError: Always.
                """
                tried.append(name)
                raise AssertionError(f"an unattended run tried: {name}")

            return refuse

        monkeypatch.setattr("spotipy.oauth2.webbrowser.open", forbid("browser"))
        monkeypatch.setattr("song_automations.clients.spotify.start_local_http_server", forbid("server"))
        monkeypatch.setattr("spotipy.oauth2.start_local_http_server", forbid("server"))
        monkeypatch.setattr("builtins.input", forbid("input"))
        monkeypatch.setattr("requests.sessions.Session.request", forbid("network"))
        return tried

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "http://127.0.0.1:8888/callback",
            "http://localhost:8888/callback",
            "http://127.0.0.1/callback",
            "https://example.com/callback",
        ],
    )
    def test_missing_login_stops_at_once(self, tmp_path, monkeypatch, redirect_uri):
        """Without a saved login the first call stops with the command to run, whatever the redirect."""
        tried = self._forbid_asking(monkeypatch)
        settings = Settings(
            spotify_client_id="id",
            spotify_client_secret="secret",
            spotify_redirect_uri=redirect_uri,
            data_dir=tmp_path,
            unattended=True,
        )
        client = SpotifyClient(settings)
        with pytest.raises(UnattendedLoginError) as caught:
            client.search_raw("q")
        assert isinstance(caught.value, ReauthRequiredError)
        assert "song-automations login spotify" in str(caught.value)
        assert tried == []
        assert not client._cache_path.exists()

    @pytest.mark.parametrize("code", ["invalid_client", "invalid_grant"])
    def test_refused_saved_login_stops_and_stays_in_place(self, tmp_path, monkeypatch, code):
        """A saved login Spotify refuses is not replaced when nobody can approve a new one."""
        settings = Settings(
            spotify_client_id="id", spotify_client_secret="secret", data_dir=tmp_path, unattended=True
        )
        client = SpotifyClient(settings)
        client._client = MagicMock()
        client._auth = MagicMock()
        client._cache_path.write_text("old login")
        client._client.search.side_effect = SpotifyOauthError(f"error: {code}", error=code)
        with pytest.raises(UnattendedLoginError):
            client.search_raw("q")
        assert client._cache_path.read_text() == "old login"
        assert not list(tmp_path.glob(".spotify_cache.stale*"))
        assert client._auth.get_access_token.call_count == 0
        assert client._client.search.call_count == 1

    @pytest.mark.parametrize(("unattended", "asks"), [(True, False), (False, True)])
    def test_only_an_attended_login_asks_the_browser(self, tmp_path, monkeypatch, unattended, asks):
        """The approval is asked for when someone is there, and refused when nobody is."""
        auth = TimedSpotifyOAuth(
            wait_seconds=10,
            unattended=unattended,
            client_id="id",
            client_secret="secret",
            redirect_uri="http://127.0.0.1:8888/callback",
            cache_path=str(tmp_path / "cache"),
        )
        monkeypatch.setattr(auth, "_get_auth_response_local_server", lambda port: "code-1")
        if asks:
            assert auth.get_auth_response() == "code-1"
        else:
            with pytest.raises(UnattendedLoginError):
                auth.get_auth_response()

    @pytest.mark.parametrize("unattended", [True, False])
    def test_login_by_name_opens_the_page_whatever_the_setting(self, tmp_path, unattended):
        """The login command asks the browser even in unattended mode, then the mode is back."""
        settings = Settings(
            spotify_client_id="id",
            spotify_client_secret="secret",
            data_dir=tmp_path,
            unattended=unattended,
        )
        client = SpotifyClient(settings)
        seen = []

        def approve(**kwargs):
            """Stands in for the browser login and notes whether asking was allowed.

            Args:
                **kwargs: Options spotipy would get.

            Returns:
                A token.
            """
            seen.append(client._auth.unattended)
            return "token"

        client._auth.get_access_token = approve
        client.login()
        assert seen == [False]
        assert client._auth.unattended is unattended

    @pytest.mark.parametrize(("interactive", "expected"), [(False, LoginFailedError), (True, None)])
    def test_pasting_the_redirect_needs_a_terminal(self, tmp_path, monkeypatch, interactive, expected):
        """Without a terminal the paste prompt is refused instead of waiting for ever."""
        auth = TimedSpotifyOAuth(
            wait_seconds=10,
            client_id="id",
            client_secret="secret",
            redirect_uri="https://example.com/callback",
            cache_path=str(tmp_path / "cache"),
        )
        prompts = []

        def typed(prompt):
            """Stands in for a person pasting the redirect address.

            Args:
                prompt: Text of the prompt.

            Returns:
                The pasted address.
            """
            prompts.append(prompt)
            return "https://example.com/callback?code=code-1"

        monkeypatch.setattr(auth, "_open_auth_url", lambda: None)
        monkeypatch.setattr("builtins.input", typed)
        monkeypatch.setattr("song_automations.clients.spotify.sys.stdin", FakeTerminal(interactive))
        if expected is None:
            assert auth.get_auth_response() == "code-1"
            assert len(prompts) == 1
        else:
            with pytest.raises(expected) as caught:
                auth.get_auth_response()
            assert "redirect address" in str(caught.value)
            assert prompts == []
