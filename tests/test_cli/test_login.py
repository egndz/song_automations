"""Tests for the login command and the setting that bounds its wait."""

import pytest
from typer.testing import CliRunner

from song_automations import cli
from song_automations.clients.http import RateLimitError
from song_automations.config import Settings
from song_automations.errors import LoginFailedError, LoginTimeoutError, ReauthRequiredError


class TestLoginCommand:
    """The login command."""

    @staticmethod
    def _login(monkeypatch, settings, arguments, failures=None):
        """Runs the login command with clients that record instead of logging in.

        Args:
            monkeypatch: Pytest monkeypatch.
            settings: Settings the command gets.
            arguments: Command line arguments after login.
            failures: Exception to raise per platform name.

        Returns:
            The command result and the platforms whose login was started.
        """
        started = []

        def fake_client(platform):
            """Builds a stand-in client class for a platform.

            Args:
                platform: Platform name.

            Returns:
                A class whose login records the platform.
            """

            class FakeClient:
                """Client whose login only records that it was asked for.

                Args:
                    settings: Application settings.
                """

                def __init__(self, settings):
                    pass

                def login(self):
                    """Records the login and fails when the test asks for it."""
                    started.append(platform)
                    if platform in (failures or {}):
                        raise failures[platform]

            return FakeClient

        monkeypatch.setattr(cli, "_init_settings", lambda: settings)
        monkeypatch.setattr(cli, "SpotifyClient", fake_client("spotify"))
        monkeypatch.setattr(cli, "SoundCloudClient", fake_client("soundcloud"))
        result = CliRunner().invoke(cli.app, ["login", *arguments])
        return result, started

    @pytest.mark.parametrize(
        ("arguments", "expected", "lines"),
        [
            ([], ["spotify", "soundcloud"], ["Spotify login approved", "SoundCloud login approved"]),
            (["all"], ["spotify", "soundcloud"], ["Spotify login approved", "SoundCloud login approved"]),
            (["spotify"], ["spotify"], ["Spotify login approved"]),
            (["soundcloud"], ["soundcloud"], ["SoundCloud login approved"]),
        ],
    )
    def test_login_opens_each_chosen_platform_and_confirms(
        self, make_settings, flat, monkeypatch, arguments, expected, lines
    ):
        """Each chosen platform is logged in, Spotify first, and confirmed."""
        result, started = self._login(monkeypatch, make_settings(), arguments)
        output = flat(result.output)
        assert started == expected
        assert result.exit_code == 0
        for line in lines:
            assert line in output
        assert output.count("login approved") == len(expected)

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (LoginTimeoutError("Spotify", 600), "was not approved within 600 seconds"),
            (LoginFailedError("Spotify", "invalid_client"), "did not go through (invalid_client)"),
            (ReauthRequiredError("Spotify"), "Your Spotify login has expired"),
            (RateLimitError(90), "SoundCloud has rate limited this app"),
        ],
        ids=lambda v: type(v).__name__,
    )
    def test_failed_login_is_explained_and_the_next_platform_still_runs(
        self, make_settings, flat, monkeypatch, error, message
    ):
        """A login that fails prints why, is not confirmed, and exits with 2."""
        result, started = self._login(monkeypatch, make_settings(), ["all"], {"spotify": error})
        output = flat(result.output)
        assert started == ["spotify", "soundcloud"]
        assert result.exit_code == 2
        assert message in output
        assert "Spotify login approved" not in output
        assert "SoundCloud login approved" in output

    @pytest.mark.parametrize(
        ("wait", "shown"),
        [
            (600, "up to 10 minutes for"),
            (60, "up to 1 minute for"),
            (20, "up to 1 minute for"),
            (1800, "up to 30 minutes for"),
        ],
    )
    def test_login_says_how_long_it_waits(self, make_settings, flat, monkeypatch, wait, shown):
        """The wait that is announced follows the setting."""
        settings = make_settings(login_wait_seconds=wait)
        result, _ = self._login(monkeypatch, settings, ["spotify"])
        assert shown in flat(result.output)

    @pytest.mark.parametrize(
        ("missing", "arguments", "started", "named"),
        [
            ("spotify_client_id", ["spotify"], [], "SPOTIFY_CLIENT_ID"),
            ("spotify_client_secret", ["all"], ["soundcloud"], "SPOTIFY_CLIENT_SECRET"),
            ("soundcloud_client_id", ["all"], ["spotify"], "SOUNDCLOUD_CLIENT_ID"),
            ("soundcloud_client_secret", ["soundcloud"], [], "SOUNDCLOUD_CLIENT_SECRET"),
        ],
    )
    def test_missing_credentials_are_named_before_any_page_opens(
        self, make_settings, flat, monkeypatch, missing, arguments, started, named
    ):
        """A platform without credentials is reported and never opened."""
        result, opened = self._login(monkeypatch, make_settings(**{missing: ""}), arguments)
        assert opened == started
        assert result.exit_code == 1
        assert named in flat(result.output)

    @pytest.mark.parametrize("platform", ["deezer", "Spotify", "both"])
    def test_unknown_platform_is_refused(self, make_settings, flat, monkeypatch, platform):
        """A platform that does not exist is named and nothing is opened."""
        result, started = self._login(monkeypatch, make_settings(), [platform])
        assert started == []
        assert result.exit_code == 1
        assert "Unknown platform" in flat(result.output)


class TestLoginWaitSetting:
    """The setting that says how long a login page is waited for."""

    @pytest.mark.parametrize(("value", "expected"), [(None, 600), ("900", 900), ("1", 1)])
    def test_wait_defaults_to_ten_minutes_and_reads_the_environment(self, monkeypatch, value, expected):
        """Without a value the wait is 600 seconds, LOGIN_WAIT_SECONDS changes it."""
        monkeypatch.delenv("LOGIN_WAIT_SECONDS", raising=False)
        if value is not None:
            monkeypatch.setenv("LOGIN_WAIT_SECONDS", value)
        assert Settings(_env_file=None).login_wait_seconds == expected

    @pytest.mark.parametrize("value", [0, -5])
    def test_wait_must_be_positive(self, value):
        """A wait of zero or less is refused."""
        with pytest.raises(ValueError):
            Settings(_env_file=None, login_wait_seconds=value)
