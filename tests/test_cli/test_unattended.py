"""Tests for unattended mode on the command line and its desktop notification."""

import pytest
import typer
from typer.testing import CliRunner

from song_automations import cli
from song_automations.clients.http import RateLimitError
from song_automations.config import Settings
from song_automations.errors import (
    LoginTimeoutError,
    RateLimitedError,
    ReauthRequiredError,
    SearchBudgetExhaustedError,
    UnattendedLoginError,
)

NOTIFIED_ERRORS = [
    (RateLimitedError("Spotify", 3600), RateLimitedError, "Spotify"),
    (ReauthRequiredError("Spotify"), ReauthRequiredError, "Spotify"),
    (UnattendedLoginError("SoundCloud"), UnattendedLoginError, "SoundCloud"),
    (LoginTimeoutError("Spotify", 600), LoginTimeoutError, "Spotify"),
    (RateLimitError(120), RateLimitedError, "SoundCloud"),
]


@pytest.fixture
def alerts(monkeypatch) -> list:
    """Replaces the desktop notification with a list of what would have been shown.

    Args:
        monkeypatch: Pytest monkeypatch.

    Returns:
        The errors the owner would have been told about.
    """
    shown: list = []
    monkeypatch.setattr(cli, "notify_attention", shown.append)
    return shown


def _stub_sync(monkeypatch, settings, error=None) -> list:
    """Replaces the settings and the sync so that a sync command runs nothing real.

    Args:
        monkeypatch: Pytest monkeypatch.
        settings: Settings every command gets.
        error: What the sync raises, None for a sync that works.

    Returns:
        A list that receives the unattended flag each sync was started with.
    """
    seen: list = []

    def run_sync(run_settings, *args, **kwargs):
        """Stands in for a sync and notes whether it runs unattended.

        Args:
            run_settings: Settings of the run.
            *args: Ignored.
            **kwargs: Ignored.

        Returns:
            An empty sync result.
        """
        seen.append(run_settings.unattended)
        if error is not None:
            raise error
        return cli.SyncResult()

    monkeypatch.setattr(cli, "get_settings", lambda: settings.model_copy())
    monkeypatch.setattr(cli, "setup_logging", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_run_sync", run_sync)
    return seen


class TestUnattendedSetting:
    """Where unattended mode can be switched on."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("true", True), ("1", True), ("false", False), ("0", False), (None, False)],
    )
    def test_environment_switches_it_on(self, tmp_path, monkeypatch, value, expected):
        """UNATTENDED in the environment sets the mode, and it is off by default."""
        monkeypatch.setenv("UNATTENDED", "false")
        if value is None:
            monkeypatch.delenv("UNATTENDED")
        else:
            monkeypatch.setenv("UNATTENDED", value)
        assert Settings(data_dir=tmp_path).unattended is expected

    @pytest.mark.parametrize(
        ("arguments", "configured", "expected"),
        [
            (["--unattended", "sync", "spotify"], False, True),
            (["sync", "spotify"], False, False),
            (["sync", "spotify"], True, True),
            (["--unattended", "sync", "soundcloud"], False, True),
            (["--unattended", "sync", "all"], False, True),
        ],
    )
    def test_option_switches_it_on_for_the_run(
        self, make_settings, monkeypatch, arguments, configured, expected
    ):
        """The option turns the mode on, and without it the configured value counts."""
        seen = _stub_sync(monkeypatch, make_settings(unattended=configured))
        result = CliRunner().invoke(cli.app, arguments)
        assert result.exit_code == 0
        assert seen and all(flag is expected for flag in seen)

    @pytest.mark.parametrize("command", [["sync", "spotify"], ["sync", "soundcloud"]])
    def test_option_does_not_outlive_its_run(self, make_settings, monkeypatch, command):
        """A run without the option is attended again, even after an unattended one."""
        seen = _stub_sync(monkeypatch, make_settings())
        CliRunner().invoke(cli.app, ["--unattended", *command])
        CliRunner().invoke(cli.app, command)
        assert seen == [True, False]

    @pytest.mark.parametrize(("arguments", "stamped"), [(["--unattended"], True), ([], False)])
    def test_unattended_run_writes_when_it_started(
        self, make_settings, flat, monkeypatch, arguments, stamped
    ):
        """An unattended run starts with a dated line, so the log shows which run is which."""
        _stub_sync(monkeypatch, make_settings())
        result = CliRunner().invoke(cli.app, [*arguments, "sync", "spotify"])
        assert ("Unattended run started 20" in flat(result.output)) is stamped


class TestOwnerAlert:
    """The desktop notification of a stopped unattended run."""

    @pytest.mark.parametrize(
        ("error", "told", "platform"), NOTIFIED_ERRORS, ids=lambda value: type(value).__name__
    )
    def test_stopped_unattended_run_tells_the_owner_once(
        self, make_settings, monkeypatch, alerts, error, told, platform
    ):
        """An expired login or a rate limit posts exactly one notification and exits with 2."""
        _stub_sync(monkeypatch, make_settings(), error)
        result = CliRunner().invoke(cli.app, ["--unattended", "sync", "spotify"])
        assert result.exit_code == 2
        assert len(alerts) == 1
        assert type(alerts[0]) is told
        assert alerts[0].platform == platform

    @pytest.mark.parametrize(
        ("error", "told", "platform"), NOTIFIED_ERRORS, ids=lambda value: type(value).__name__
    )
    def test_attended_run_only_prints(self, make_settings, monkeypatch, alerts, error, told, platform):
        """With someone at the terminal the message is printed and nothing is posted."""
        _stub_sync(monkeypatch, make_settings(), error)
        result = CliRunner().invoke(cli.app, ["sync", "spotify"])
        assert result.exit_code == 2
        assert alerts == []

    @pytest.mark.parametrize("budget", [1, 400])
    def test_used_up_search_budget_is_a_quiet_stop(self, make_settings, monkeypatch, alerts, budget):
        """Running out of searches is the normal end of a daily run and tells nobody."""
        _stub_sync(monkeypatch, make_settings(), SearchBudgetExhaustedError("Spotify", budget))
        result = CliRunner().invoke(cli.app, ["--unattended", "sync", "spotify"])
        assert result.exit_code == 0
        assert alerts == []

    @pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
    def test_login_command_never_notifies(self, make_settings, monkeypatch, alerts, platform):
        """A login someone started by hand reports its failure in the terminal only."""

        class FailingClient:
            """Client whose login is never approved.

            Args:
                settings: Application settings.
            """

            def __init__(self, settings):
                pass

            def login(self):
                """Fails like a page nobody approved.

                Raises:
                    LoginTimeoutError: Always.
                """
                raise LoginTimeoutError(platform, 600)

        monkeypatch.setattr(cli, "_init_settings", lambda: make_settings(unattended=True))
        monkeypatch.setattr(cli, "SpotifyClient", FailingClient)
        monkeypatch.setattr(cli, "SoundCloudClient", FailingClient)
        result = CliRunner().invoke(cli.app, ["--unattended", "login", platform])
        assert result.exit_code == 2
        assert alerts == []

    @pytest.mark.parametrize(("unattended", "expected"), [(True, 1), (False, 0)])
    def test_want_sync_tells_the_owner_too(self, make_settings, monkeypatch, alerts, unattended, expected):
        """The Want command on its own follows the same rule as a sync."""

        def run_want(*args, **kwargs):
            """Stands in for a Want step that needs a login.

            Args:
                *args: Ignored.
                **kwargs: Ignored.

            Raises:
                ReauthRequiredError: Always.
            """
            raise ReauthRequiredError("Spotify")

        monkeypatch.setattr(cli, "_init_settings", lambda: make_settings(unattended=unattended))
        for name in ("DiscogsClient", "SpotifyClient", "SoundCloudClient"):
            monkeypatch.setattr(cli, name, lambda *args, **kwargs: object())
        monkeypatch.setattr(cli, "_run_want", run_want)
        result = CliRunner().invoke(cli.app, ["want", "sync", "--platform", "spotify"])
        assert result.exit_code == 2
        assert len(alerts) == expected

    @pytest.mark.parametrize("error", [ReauthRequiredError("Spotify"), RateLimitedError("Spotify", 60)])
    def test_missing_settings_mean_nobody_is_told(self, alerts, error):
        """A command that passes no settings never posts a notification."""
        with pytest.raises(typer.Exit), cli._friendly_errors():
            raise error
        assert alerts == []
