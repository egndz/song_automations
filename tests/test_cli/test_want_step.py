"""Tests for the Want step in front of a sync and for the shared error messages."""

from datetime import datetime

import httpx
import pytest
import typer
from typer.testing import CliRunner

from song_automations import cli
from song_automations.clients.http import RateLimitError
from song_automations.errors import (
    LoginFailedError,
    LoginTimeoutError,
    RateLimitedError,
    ReauthRequiredError,
    SearchBudgetExhaustedError,
)
from song_automations.state.tracker import StateTracker
from song_automations.want.sync import WantResult

SYNCED = object()

STOPPING_ERRORS = [
    (RateLimitedError("Spotify", 3600), 2, "Spotify has rate limited this app"),
    (ReauthRequiredError("Spotify"), 2, "Your Spotify login has expired"),
    (LoginTimeoutError("SoundCloud", 600), 2, "was not approved within 600 seconds"),
    (LoginFailedError("Spotify", "invalid_client"), 2, "did not go through (invalid_client)"),
    (SearchBudgetExhaustedError("Spotify", 400), 0, "used its 400 Spotify searches"),
    (RateLimitError(120), 2, "SoundCloud has rate limited this app"),
]

PASSING_ERRORS = [
    RuntimeError("Discogs answered 502 Bad Gateway"),
    httpx.ConnectError("Discogs cannot be reached"),
    ValueError("unexpected Discogs response"),
    KeyError("pagination"),
    OSError("network is down"),
]


class FixedClock:
    """Clock that always shows the same moment, so two messages name the same time."""

    @staticmethod
    def now() -> datetime:
        """Tells the time.

        Returns:
            Noon on the first day of 2026.
        """
        return datetime(2026, 1, 1, 12, 0)


class FakeEngine:
    """Sync engine that records the folder sync instead of running it.

    Args:
        *args: Ignored.
        **kwargs: Ignored.
    """

    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    def sync_to_spotify(self, **kwargs):
        """Records a Spotify folder sync.

        Args:
            **kwargs: Sync options.

        Returns:
            A marker standing in for the sync result.
        """
        FakeEngine.calls.append("spotify")
        return SYNCED

    def sync_to_soundcloud(self, **kwargs):
        """Records a SoundCloud folder sync.

        Args:
            **kwargs: Sync options.

        Returns:
            A marker standing in for the sync result.
        """
        FakeEngine.calls.append("soundcloud")
        return SYNCED


@pytest.fixture
def offline(monkeypatch):
    """Replaces every client and the engine of the CLI with fakes.

    Args:
        monkeypatch: Pytest monkeypatch.

    Returns:
        The list of folder syncs the fake engine ran.
    """
    FakeEngine.calls = []
    for name in ("DiscogsClient", "SpotifyClient", "SoundCloudClient", "DeezerClient", "IsrcFinder"):
        monkeypatch.setattr(cli, name, lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "SyncEngine", FakeEngine)
    return FakeEngine.calls


def want_raising(monkeypatch, error):
    """Makes the Want playlist step fail, or succeed when there is no error.

    Args:
        monkeypatch: Pytest monkeypatch.
        error: Exception to raise, or None for a run that adds one record.
    """

    def run(*args, **kwargs):
        """Stands in for the Want playlist step.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Returns:
            What the step did.
        """
        if error is not None:
            raise error
        return WantResult(added=["Bicep - Glue"])

    monkeypatch.setattr(cli, "sync_want_playlist", run)


class TestWantStepInSync:
    """The Want step in front of a folder sync."""

    @pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
    @pytest.mark.parametrize("error", PASSING_ERRORS, ids=lambda e: type(e).__name__)
    def test_failing_want_step_is_a_warning_and_the_sync_runs(
        self, make_settings, flat, monkeypatch, capsys, offline, platform, error
    ):
        """A Discogs outage in the Want step costs only the Want step."""
        want_raising(monkeypatch, error)
        result = cli._run_sync(make_settings(), platform, None, False, False)
        assert result is SYNCED
        assert offline == [platform]
        output = flat(capsys.readouterr().out)
        assert "Skipped the Want playlist this run" in output
        assert "The folder sync continues" in output

    @pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
    @pytest.mark.parametrize(
        "error", [row[0] for row in STOPPING_ERRORS], ids=lambda e: type(e).__name__
    )
    def test_login_limit_and_budget_errors_still_stop_the_run(
        self, make_settings, monkeypatch, offline, platform, error
    ):
        """Errors the folder sync would hit too are passed on unchanged."""
        want_raising(monkeypatch, error)
        with pytest.raises(type(error)) as caught:
            cli._run_sync(make_settings(), platform, None, False, False)
        assert caught.value is error
        assert offline == []

    @pytest.mark.parametrize("platform", ["spotify", "soundcloud"])
    def test_working_want_step_reports_and_the_sync_runs(
        self, make_settings, flat, monkeypatch, capsys, offline, platform
    ):
        """A Want step that works prints what it added and no warning."""
        want_raising(monkeypatch, None)
        assert cli._run_sync(make_settings(), platform, None, False, False) is SYNCED
        output = flat(capsys.readouterr().out)
        assert "Added to wantlist: Bicep - Glue" in output
        assert "Skipped the Want playlist" not in output

    @pytest.mark.parametrize(("age_days", "kept"), [(1, True), (29, True), (31, False), (300, False)])
    def test_old_search_responses_are_purged_when_a_sync_starts(
        self, make_settings, monkeypatch, offline, age_days, kept
    ):
        """A sync run starts by deleting search responses too old to be read."""
        settings = make_settings()
        tracker = StateTracker(settings.db_path)
        tracker.save_search_cache("spotify", "q", [{"id": 1}])
        with tracker._get_connection() as conn:
            conn.execute("UPDATE search_cache SET fetched_at = datetime('now', ?)", (f"-{age_days} days",))
        want_raising(monkeypatch, None)
        cli._run_sync(settings, "spotify", None, False, True)
        with tracker._get_connection() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
        assert rows == (1 if kept else 0)


class TestSharedErrorMessages:
    """The sync commands and want sync explain a stopped run the same way."""

    @staticmethod
    def _want_sync(monkeypatch, settings, arguments, failures):
        """Runs want sync with fake clients.

        Args:
            monkeypatch: Pytest monkeypatch.
            settings: Settings the command gets.
            arguments: Command line arguments after want sync.
            failures: Exception to raise per platform name.

        Returns:
            The command result and the platforms that were processed.
        """
        processed = []

        def run_want(settings, client, platform, discogs_client, state_tracker, dry_run):
            """Stands in for the Want playlist step.

            Args:
                settings: Application settings.
                client: Platform client.
                platform: Platform name.
                discogs_client: Discogs client.
                state_tracker: State tracker.
                dry_run: If True, change nothing.
            """
            processed.append(platform)
            if platform in failures:
                raise failures[platform]

        monkeypatch.setattr(cli, "_init_settings", lambda: settings)
        for name in ("DiscogsClient", "SpotifyClient", "SoundCloudClient"):
            monkeypatch.setattr(cli, name, lambda *args, **kwargs: object())
        monkeypatch.setattr(cli, "_run_want", run_want)
        result = CliRunner().invoke(cli.app, ["want", "sync", *arguments])
        return result, processed

    @pytest.mark.parametrize(
        ("error", "exit_code", "message"), STOPPING_ERRORS, ids=lambda v: type(v).__name__
    )
    def test_want_sync_and_sync_print_the_same_message_and_exit_code(
        self, make_settings, flat, monkeypatch, capsys, error, exit_code, message
    ):
        """Each stopping error gets one message and one exit code, whichever command hit it."""

        def run_sync(*args, **kwargs):
            """Stands in for a sync that is refused.

            Args:
                *args: Ignored.
                **kwargs: Ignored.
            """
            raise error

        monkeypatch.setattr("song_automations.errors.datetime", FixedClock)
        monkeypatch.setattr(cli, "_run_sync", run_sync)
        with pytest.raises(typer.Exit) as stopped:
            cli._guarded_sync(make_settings())
        from_sync = flat(capsys.readouterr().out)
        result, _ = self._want_sync(
            monkeypatch, make_settings(), ["--platform", "spotify"], {"spotify": error}
        )
        assert stopped.value.exit_code == exit_code
        assert result.exit_code == exit_code
        assert message in from_sync
        assert flat(result.output) == from_sync

    @pytest.mark.parametrize(
        ("error", "exit_code"),
        [(row[0], row[1]) for row in STOPPING_ERRORS],
        ids=lambda v: type(v).__name__,
    )
    def test_spotify_stop_does_not_skip_soundcloud(self, make_settings, monkeypatch, error, exit_code):
        """With both platforms asked for, SoundCloud is processed whatever stopped Spotify."""
        result, processed = self._want_sync(monkeypatch, make_settings(), [], {"spotify": error})
        assert processed == ["spotify", "soundcloud"]
        assert result.exit_code == exit_code

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            ([], ["spotify", "soundcloud"]),
            (["--platform", "all"], ["spotify", "soundcloud"]),
            (["--platform", "spotify"], ["spotify"]),
            (["-p", "soundcloud"], ["soundcloud"]),
        ],
    )
    def test_want_sync_processes_the_platforms_asked_for(
        self, make_settings, monkeypatch, arguments, expected
    ):
        """A run without errors processes each chosen platform and exits cleanly."""
        result, processed = self._want_sync(monkeypatch, make_settings(), arguments, {})
        assert processed == expected
        assert result.exit_code == 0

    @pytest.mark.parametrize("platform", ["deezer", "Spotify", ""])
    def test_unknown_platform_is_refused(self, make_settings, flat, monkeypatch, platform):
        """A platform that does not exist is named instead of silently doing nothing."""
        result, processed = self._want_sync(
            monkeypatch, make_settings(), ["--platform", platform], {}
        )
        assert processed == []
        assert result.exit_code == 1
        assert "Unknown platform" in flat(result.output)
