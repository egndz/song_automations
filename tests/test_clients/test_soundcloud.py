"""Tests for SoundCloud client."""

import base64
import hashlib

import httpx
import pytest

from song_automations.clients import soundcloud
from song_automations.clients.http import RateLimitError
from song_automations.clients.soundcloud import (
    OAuthCallbackHandler,
    SoundCloudClient,
    SoundCloudPlaylist,
    SoundCloudTrack,
)
from song_automations.config import Settings
from song_automations.errors import (
    LoginFailedError,
    LoginTimeoutError,
    ReauthRequiredError,
    UnattendedLoginError,
)


class TestPKCEGeneration:
    """Tests for PKCE code verifier and challenge generation."""

    @pytest.fixture
    def client(self, settings):
        """Create a SoundCloud client for testing."""
        return SoundCloudClient(settings)

    def test_pkce_verifier_length(self, client):
        """Verifier should be between 43-128 characters."""
        verifier, _ = client._generate_pkce()
        assert 43 <= len(verifier) <= 128

    def test_pkce_verifier_is_url_safe(self, client):
        """Verifier should only contain URL-safe base64 characters."""
        verifier, _ = client._generate_pkce()
        allowed_chars = set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        assert all(c in allowed_chars for c in verifier)

    def test_pkce_challenge_is_sha256_of_verifier(self, client):
        """Challenge should be base64url(SHA256(verifier))."""
        verifier, challenge = client._generate_pkce()
        expected_digest = hashlib.sha256(verifier.encode()).digest()
        expected_challenge = (
            base64.urlsafe_b64encode(expected_digest).rstrip(b"=").decode("ascii")
        )
        assert challenge == expected_challenge

    def test_pkce_challenge_no_padding(self, client):
        """Challenge should not have base64 padding characters."""
        _, challenge = client._generate_pkce()
        assert "=" not in challenge

    @pytest.mark.parametrize("_iteration", range(5))
    def test_pkce_generates_unique_values(self, client, _iteration):
        """Each call should generate unique verifier/challenge pairs."""
        pairs = [client._generate_pkce() for _ in range(3)]
        verifiers = [p[0] for p in pairs]
        challenges = [p[1] for p in pairs]
        assert len(set(verifiers)) == 3
        assert len(set(challenges)) == 3


class TestSoundCloudTrack:
    """Tests for SoundCloudTrack dataclass."""

    @pytest.mark.parametrize(
        "track_id,title,artist,playback_count,likes_count",
        [
            (123, "Test Track", "Test Artist", 1000, 50),
            (456, "Another", "Artist", 0, 0),
            (789, "Popular", "Famous DJ", 1000000, 50000),
        ],
    )
    def test_track_properties(
        self, track_id, title, artist, playback_count, likes_count
    ):
        """Track should store properties correctly."""
        track = SoundCloudTrack(
            id=track_id,
            permalink_url="https://soundcloud.com/test",
            title=title,
            artist=artist,
            playback_count=playback_count,
            likes_count=likes_count,
            duration_ms=180000,
            user_id=1,
            is_streamable=True,
        )
        assert track.id == track_id
        assert track.title == title
        assert track.artist == artist
        assert track.playback_count == playback_count
        assert track.likes_count == likes_count

    def test_track_full_title(self):
        """Full title should combine artist and title."""
        track = SoundCloudTrack(
            id=123,
            permalink_url="https://soundcloud.com/test",
            title="Test Track",
            artist="Test Artist",
            playback_count=0,
            likes_count=0,
            duration_ms=180000,
            user_id=1,
            is_streamable=True,
        )
        assert track.full_title == "Test Artist - Test Track"


class TestSoundCloudPlaylist:
    """Tests for SoundCloudPlaylist dataclass."""

    @pytest.mark.parametrize(
        "playlist_id,title,track_count,is_public",
        [
            (100, "My Playlist", 10, True),
            (200, "Empty Playlist", 0, True),
            (300, "Private Playlist", 5, False),
        ],
    )
    def test_playlist_properties(self, playlist_id, title, track_count, is_public):
        """Playlist should store properties correctly."""
        playlist = SoundCloudPlaylist(
            id=playlist_id,
            permalink_url="https://soundcloud.com/user/sets/playlist",
            title=title,
            user_id=1,
            track_count=track_count,
            is_public=is_public,
        )
        assert playlist.id == playlist_id
        assert playlist.title == title
        assert playlist.track_count == track_count
        assert playlist.is_public == is_public


class TestParseTrack:
    """Tests for track parsing from API response."""

    @pytest.fixture
    def client(self, settings):
        """Create a SoundCloud client for testing."""
        return SoundCloudClient(settings)

    @pytest.mark.parametrize(
        "item,expected_id,expected_title,expected_artist",
        [
            (
                {
                    "id": 123,
                    "title": "Track Title",
                    "user": {"username": "Artist Name", "id": 1},
                    "permalink_url": "https://soundcloud.com/artist/track",
                    "playback_count": 1000,
                    "likes_count": 50,
                    "duration": 180000,
                    "streamable": True,
                },
                123,
                "Track Title",
                "Artist Name",
            ),
            (
                {
                    "id": 456,
                    "title": "Another Track",
                    "user": {"username": "Another Artist", "id": 2},
                    "permalink_url": "https://soundcloud.com/another/track",
                    "playback_count": None,
                    "likes_count": None,
                    "duration": 240000,
                    "streamable": True,
                },
                456,
                "Another Track",
                "Another Artist",
            ),
        ],
    )
    def test_parse_track(
        self, client, item, expected_id, expected_title, expected_artist
    ):
        """Parse track should extract correct fields from API response."""
        track = client._parse_track(item)
        assert track.id == expected_id
        assert track.title == expected_title
        assert track.artist == expected_artist

    def test_parse_track_missing_user(self, client):
        """Parse track should handle missing user gracefully."""
        item = {
            "id": 789,
            "title": "Orphan Track",
            "user": None,
            "permalink_url": "https://soundcloud.com/unknown/track",
            "playback_count": 0,
            "likes_count": 0,
            "duration": 180000,
            "streamable": True,
        }
        track = client._parse_track(item)
        assert track.id == 789
        assert track.title == "Orphan Track"
        assert track.artist == "Unknown Artist"

    def test_parse_track_null_counts(self, client):
        """Parse track should handle null playback/likes counts."""
        item = {
            "id": 999,
            "title": "New Track",
            "user": {"username": "New Artist", "id": 1},
            "permalink_url": "https://soundcloud.com/new/track",
            "playback_count": None,
            "likes_count": None,
            "duration": 180000,
            "streamable": True,
        }
        track = client._parse_track(item)
        assert track.playback_count == 0
        assert track.likes_count == 0


class TestSearchAccess:
    """Tests for which tracks searches ask SoundCloud for."""

    @pytest.fixture
    def client(self, settings, monkeypatch):
        """Create a client whose requests are recorded instead of sent.

        Args:
            settings: Test settings.
            monkeypatch: Pytest patcher.

        Returns:
            The client, with the requests in client.sent and the next body in client.body.
        """
        client = SoundCloudClient(settings)
        client.sent = []
        client.body = []

        def fake_get(url, params=None):
            """Records one request.

            Args:
                url: Target URL.
                params: Query parameters.

            Returns:
                The prepared body.
            """
            client.sent.append((url, params))
            return client.body

        monkeypatch.setattr(client, "_get_json", fake_get)
        return client

    @pytest.mark.parametrize("search", ["tracks", "sets", "account"])
    def test_searches_ask_for_every_access_level(self, client, search):
        """Label releases are only returned when preview and blocked tracks are asked for too."""
        if search == "tracks":
            client.search_raw("Bicep Glue")
        elif search == "sets":
            client.search_playlists_raw("Bicep Isles")
        else:
            client.body = [{"username": "Bicep", "track_count": 3, "followers_count": 9, "urn": "soundcloud:users:1"}]
            client.account_tracks_raw("Bicep")
        asked = [params["access"] for _, params in client.sent if params and "access" in params]
        assert asked == ["playable,preview,blocked"]


class FakeCallbackServer:
    """Local server that never touches the network.

    Args:
        codes: What each handled request brings, None for a request without a code.
        clock: Shared fake clock, advanced by a second per handled request.
    """

    def __init__(self, codes, clock):
        self.timeout = None
        self.closed = False
        self.timeouts = []
        self._codes = list(codes)
        self._clock = clock

    def handle_request(self):
        """Handles one request, or lets a second pass when none is left."""
        self.timeouts.append(self.timeout)
        self._clock[0] += 1
        if self._codes:
            OAuthCallbackHandler.authorization_code = self._codes.pop(0)

    def server_close(self):
        """Records that the port was released."""
        self.closed = True


class FakeTokenResponse:
    """Answer of the token endpoint.

    Args:
        status_code: HTTP status.
        body: Decoded JSON body.
    """

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.headers = {}
        self._body = body

    def json(self):
        """Returns the body.

        Returns:
            Decoded JSON.
        """
        return self._body

    def raise_for_status(self):
        """Raises for an error status, like httpx does.

        Raises:
            httpx.HTTPStatusError: When the status is 400 or higher.
        """
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://secure.soundcloud.com/oauth/token")
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )


class TestSoundCloudLogin:
    """Waiting for the SoundCloud approval page, for a configurable time."""

    @staticmethod
    def _client(tmp_path, monkeypatch, codes, wait=600, token=None, saved=None, unattended=False):
        """Builds a client whose browser, clock, local server and token endpoint are fakes.

        Args:
            tmp_path: Folder standing in for the data directory.
            monkeypatch: Pytest monkeypatch.
            codes: What each request to the local server brings.
            wait: Seconds to wait for the approval.
            token: Answer of the token endpoint.
            saved: Access token of a saved login, if any.
            unattended: Whether the run may not open a browser.

        Returns:
            The client, with the fake server in client.server and opened pages in client.opened.
        """
        if saved:
            (tmp_path / ".soundcloud_token.json").write_text(
                f'{{"access_token": "{saved}", "refresh_token": "r-old"}}'
            )
        settings = Settings(
            soundcloud_client_id="id",
            soundcloud_client_secret="secret",
            data_dir=tmp_path,
            login_wait_seconds=wait,
            unattended=unattended,
        )
        clock = [0.0]
        client = SoundCloudClient(settings)
        client.server = FakeCallbackServer(codes, clock)
        client.opened = []
        client.bound = []

        def serve(address, handler):
            """Hands out the fake server.

            Args:
                address: Host and port asked for.
                handler: Request handler class.

            Returns:
                The fake server.
            """
            client.bound.append(address)
            return client.server

        answer = token or FakeTokenResponse(200, {"access_token": "a-new", "refresh_token": "r-new"})
        monkeypatch.setattr(soundcloud, "HTTPServer", serve)
        monkeypatch.setattr(soundcloud.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(soundcloud.webbrowser, "open", client.opened.append)
        monkeypatch.setattr(client._http_client, "post", lambda url, data: answer)
        monkeypatch.setattr(OAuthCallbackHandler, "authorization_code", None)
        return client

    @pytest.mark.parametrize("codes", [["c"], [None, "c"], [None, None, "c"]])
    def test_approval_saves_a_private_login(self, tmp_path, monkeypatch, codes):
        """An approved page yields a saved login only the user can read."""
        client = self._client(tmp_path, monkeypatch, codes)
        client.authenticate()
        saved = tmp_path / ".soundcloud_token.json"
        assert '"a-new"' in saved.read_text()
        assert saved.stat().st_mode & 0o777 == 0o600
        assert len(client.opened) == 1
        assert client.bound == [("localhost", 8889)]
        assert len(client.server.timeouts) == len(codes)
        assert client.server.closed
        assert OAuthCallbackHandler.authorization_code is None

    @pytest.mark.parametrize("wait", [1, 5, 600])
    def test_unanswered_page_times_out_after_the_configured_wait(self, tmp_path, monkeypatch, wait):
        """Nobody approving ends with a plain message, and the saved login stays."""
        client = self._client(tmp_path, monkeypatch, [], wait=wait, saved="a-old")
        with pytest.raises(LoginTimeoutError) as caught:
            client.login()
        assert isinstance(caught.value, ReauthRequiredError)
        assert f"{wait} seconds" in str(caught.value)
        assert "song-automations login soundcloud" in str(caught.value)
        assert client.server.timeouts[0] == wait
        assert len(client.server.timeouts) == wait
        assert client.server.closed
        assert '"a-old"' in (tmp_path / ".soundcloud_token.json").read_text()

    @pytest.mark.parametrize(
        ("saved", "method", "pages_opened"),
        [
            ("a-old", "login", 1),
            (None, "login", 1),
            ("a-old", "authenticate", 0),
            (None, "authenticate", 1),
        ],
    )
    def test_login_always_asks_and_authenticate_only_when_needed(
        self, tmp_path, monkeypatch, saved, method, pages_opened
    ):
        """The login command asks the browser again, a sync only when no login is saved."""
        client = self._client(tmp_path, monkeypatch, ["c"], saved=saved)
        getattr(client, method)()
        assert len(client.opened) == pages_opened
        assert client._access_token == ("a-new" if pages_opened else "a-old")

    @pytest.mark.parametrize(
        ("status", "body"),
        [(401, {}), (400, {}), (500, {}), (200, {"refresh_token": "r"})],
    )
    def test_refused_code_is_explained(self, tmp_path, monkeypatch, status, body):
        """A token endpoint that refuses becomes a message that names what to check."""
        client = self._client(
            tmp_path, monkeypatch, ["c"], token=FakeTokenResponse(status, body), saved="a-old"
        )
        with pytest.raises(LoginFailedError) as caught:
            client.login()
        assert "client secret" in str(caught.value)
        assert '"a-old"' in (tmp_path / ".soundcloud_token.json").read_text()

    @pytest.mark.parametrize("retry_after", [60, 3600])
    def test_rate_limit_during_login_keeps_its_own_error(self, tmp_path, monkeypatch, retry_after):
        """A limit on the token endpoint is still reported as a limit."""
        answer = FakeTokenResponse(429, {})
        answer.headers = {"Retry-After": str(retry_after)}
        client = self._client(tmp_path, monkeypatch, ["c"], token=answer)
        with pytest.raises(RateLimitError) as caught:
            client.login()
        assert caught.value.retry_after == retry_after

    @pytest.mark.parametrize("reason", ["Address already in use", "Permission denied"])
    def test_port_that_cannot_be_used_is_explained(self, tmp_path, monkeypatch, reason):
        """A busy local port is reported plainly and no page is opened."""
        client = self._client(tmp_path, monkeypatch, ["c"])

        def refuse(address, handler):
            """Stands in for a port that cannot be listened on.

            Args:
                address: Host and port asked for.
                handler: Request handler class.

            Raises:
                OSError: Always.
            """
            raise OSError(reason)

        monkeypatch.setattr(soundcloud, "HTTPServer", refuse)
        with pytest.raises(LoginFailedError) as caught:
            client.login()
        assert reason in str(caught.value)
        assert client.opened == []

    @pytest.mark.parametrize(
        ("saved", "entry"),
        [
            (None, "authenticate"),
            (None, "_get_headers"),
            ("a-old", "refused"),
        ],
    )
    def test_unattended_run_stops_instead_of_opening_the_page(self, tmp_path, monkeypatch, saved, entry):
        """A missing login, or one SoundCloud no longer accepts, stops an unattended run at once."""
        client = self._client(
            tmp_path,
            monkeypatch,
            ["c"],
            token=FakeTokenResponse(401, {}),
            saved=saved,
            unattended=True,
        )
        with pytest.raises(UnattendedLoginError) as caught:
            if entry == "refused":
                client._handle_auth_error(FakeTokenResponse(401, {}))
            else:
                getattr(client, entry)()
        assert isinstance(caught.value, ReauthRequiredError)
        assert "song-automations login soundcloud" in str(caught.value)
        assert client.opened == []
        assert client.bound == []

    @pytest.mark.parametrize(
        ("saved", "method", "pages_opened"),
        [
            ("a-old", "login", 1),
            (None, "login", 1),
            ("a-old", "authenticate", 0),
        ],
    )
    def test_unattended_setting_spares_the_login_command_and_a_saved_login(
        self, tmp_path, monkeypatch, saved, method, pages_opened
    ):
        """Logging in by name still opens the page, and a saved login is used without asking."""
        client = self._client(tmp_path, monkeypatch, ["c"], saved=saved, unattended=True)
        getattr(client, method)()
        assert len(client.opened) == pages_opened
        assert client._access_token == ("a-new" if pages_opened else "a-old")
