"""Tests for the Seller Radio command line interface.

Every network touching entry point is monkeypatched, so the suite never issues an HTTP request
and never depends on a Discogs token being present in the environment.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from radio import cli, db

ALL_COMMANDS: tuple[str, ...] = (
    "sync",
    "crawl",
    "enrich",
    "export",
    "serve",
    "owned",
    "stats",
    "hitrate",
)

TOKEN_COMMANDS: tuple[str, ...] = ("sync", "crawl", "enrich", "serve", "owned")

OFFLINE_COMMANDS: tuple[str, ...] = ("export", "stats", "hitrate")


@dataclass
class FakeClient:
    """Stand-in for DiscogsClient that never touches the network.

    Attributes:
        closed: Whether close was called.
        request_count: Number of requests the fake pretends to have issued.
    """

    closed: bool = False
    request_count: int = 0

    def close(self) -> None:
        """Record that the client was closed.

        Returns:
            None.
        """
        self.closed = True


@dataclass
class FakeModule:
    """Stand-in for the export or server module.

    Attributes:
        calls: Arguments captured from each call.
        result: Value returned by write_crate.
    """

    calls: list[tuple] = field(default_factory=list)
    result: dict = field(default_factory=lambda: {"tracks": 3, "releases": 5})

    def write_crate(self, conn: sqlite3.Connection, path: Path) -> dict:
        """Pretend to write a crate file.

        Args:
            conn: Open SQLite connection.
            path: Destination path.

        Returns:
            A fake statistics mapping.
        """
        self.calls.append((conn, path))
        return self.result

    def serve(
        self,
        db_path: Path,
        token: str,
        username: str,
        host: str = "127.0.0.1",
        port: int = 8733,
    ) -> None:
        """Pretend to run the player server.

        Args:
            db_path: Path to the database.
            token: Discogs token.
            username: Discogs username.
            host: Bind host.
            port: Bind port.

        Returns:
            None.
        """
        self.calls.append((db_path, token, username, host, port))


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    """Provide a database path inside an isolated temporary directory.

    Args:
        tmp_path: Pytest supplied temporary directory.

    Returns:
        Path to a database file that does not exist yet.
    """
    return tmp_path / "crate.db"


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Replace token lookup and client construction with offline fakes.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The fake client every command will receive.
    """
    client = FakeClient()
    monkeypatch.setattr(cli.config, "get_token", lambda: "test-token")
    monkeypatch.setattr(cli.config, "get_username", lambda: "egunduz")
    monkeypatch.setattr(cli, "make_client", lambda token: client)
    return client


def seed(db_path: Path) -> None:
    """Fill a database with a small, realistic sample.

    Args:
        db_path: Path to the database file.

    Returns:
        None.
    """
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.executemany(
        "INSERT INTO pool (release_id, source, bank_key, discovered_at) VALUES (?, ?, ?, ?)",
        [
            (1, "seller", None, "2026-01-01T00:00:00+00:00"),
            (2, "seller", None, "2026-01-01T00:00:00+00:00"),
            (3, "search", "genre=Electronic", "2026-01-01T00:00:00+00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO releases (release_id, artist, title, year, styles, has_video, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "Artist A", "Title A", 1995, '["Deep House"]', 1, "2026-01-02T00:00:00+00:00"),
            (2, "Artist B", "Title B", 1988, '["Techno"]', 0, "2026-01-02T00:00:00+00:00"),
        ],
    )
    conn.execute(
        "INSERT INTO videos (release_id, video_id, title, duration, position, dead) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (1, "abcdefghijk", "Title A", 320, 1),
    )
    conn.execute(
        "INSERT INTO listings (listing_id, seller, release_id, price, currency, active, last_seen)"
        " VALUES (?, ?, ?, ?, ?, 1, ?)",
        (999, "allcitydublin", 1, 19.03, "EUR", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_every_subcommand_parses(command: str) -> None:
    """Each documented subcommand parses and binds a handler.

    Args:
        command: Subcommand name.

    Returns:
        None.
    """
    args = cli.build_parser().parse_args([command])
    assert args.command == command
    assert callable(args.handler)


@pytest.mark.parametrize(
    ("argv", "attribute", "expected"),
    [
        (["sync", "--seller", "allcitydublin"], "seller", "allcitydublin"),
        (["crawl", "--bank-index", "1"], "bank_index", 1),
        (["enrich", "--limit", "25"], "limit", 25),
        (["enrich"], "limit", cli.DEFAULT_ENRICH_LIMIT),
        (["export", "--out", "/tmp/crate.js"], "out", Path("/tmp/crate.js")),
        (["serve", "--port", "9000"], "port", 9000),
        (["serve", "--host", "0.0.0.0"], "host", "0.0.0.0"),
        (["serve"], "port", cli.DEFAULT_PORT),
        (["hitrate", "--sample", "42"], "sample", 42),
        (["hitrate"], "sample", cli.DEFAULT_HITRATE_SAMPLE),
    ],
)
def test_subcommand_options(argv: list[str], attribute: str, expected: object) -> None:
    """Subcommand options land on the namespace with the documented defaults.

    Args:
        argv: Argument list to parse.
        attribute: Namespace attribute to inspect.
        expected: Expected attribute value.

    Returns:
        None.
    """
    args = cli.build_parser().parse_args(argv)
    assert getattr(args, attribute) == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["frobnicate"],
        ["syncc"],
        ["sync", "--nope"],
        ["enrich", "--limit", "not-a-number"],
    ],
)
def test_unknown_input_exits_non_zero(argv: list[str]) -> None:
    """Unparseable command lines return a non-zero code instead of raising.

    Args:
        argv: Argument list to parse.

    Returns:
        None.
    """
    assert cli.main(argv) != 0


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_exits_zero(flag: str) -> None:
    """The help flag returns the success code.

    Args:
        flag: Help flag variant.

    Returns:
        None.
    """
    assert cli.main([flag]) == 0


def test_no_command_prints_help() -> None:
    """Running with no subcommand reports a usage error code.

    Returns:
        None.
    """
    assert cli.main([]) == cli.EXIT_CONFIG


@pytest.mark.parametrize("command", TOKEN_COMMANDS)
def test_missing_token_exits_two(
    command: str,
    db_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing token yields exit code 2 and a readable message, not a traceback.

    Args:
        command: Subcommand that needs credentials.
        db_file: Temporary database path.
        monkeypatch: Pytest monkeypatch fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """

    def boom() -> str:
        """Simulate an unconfigured environment.

        Returns:
            Never returns.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("No Discogs token found. Set DISCOGS_TOKEN in your environment.")

    monkeypatch.setattr(cli.config, "get_token", boom)
    code = cli.main(["--db", str(db_file), command])
    captured = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert "No Discogs token found" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("command", OFFLINE_COMMANDS)
def test_offline_commands_need_no_token(
    command: str,
    db_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Export, stats, and hitrate run without any credentials.

    Args:
        command: Subcommand under test.
        db_file: Temporary database path.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """

    def boom() -> str:
        """Simulate an unconfigured environment.

        Returns:
            Never returns.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("No Discogs token found.")

    monkeypatch.setattr(cli.config, "get_token", boom)
    monkeypatch.setattr(cli, "load_export", lambda: FakeModule())
    seed(db_file)
    assert cli.main(["--db", str(db_file), command]) == cli.EXIT_OK


@pytest.mark.parametrize(
    ("command", "target", "attribute", "replacement", "expected_fragment"),
    [
        (
            ["sync"],
            "sources",
            "sync_sellers",
            lambda conn, client, sellers: {
                "sellers": len(sellers),
                "listings": 7,
                "new_releases": 3,
                "deactivated": 1,
                "results": [
                    {
                        "seller": sellers[0],
                        "listings": 7,
                        "new_releases": 3,
                        "deactivated": 1,
                    }
                ],
                "errors": [],
            },
            "new releases pooled 3",
        ),
        (
            ["crawl"],
            "sources",
            "crawl_banks",
            lambda conn, client, banks: {
                "banks": len(banks),
                "found": 200,
                "new": 40,
                "capped": [],
                "results": [
                    {"bank_key": "genre=Electronic", "pages": 2, "found": 200, "new": 40}
                ],
            },
            "new releases pooled 40",
        ),
        (
            ["owned"],
            "sources",
            "sync_owned",
            lambda conn, client, username: 12,
            "Account sync",
        ),
    ],
)
def test_network_commands_report_results(
    command: list[str],
    target: str,
    attribute: str,
    replacement: Callable,
    expected_fragment: str,
    db_file: Path,
    offline: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Network commands print their results and close the client.

    Args:
        command: Subcommand argument list.
        target: Name of the cli attribute holding the module to patch.
        attribute: Function name to replace on that module.
        replacement: Fake implementation.
        expected_fragment: Text expected in the output.
        db_file: Temporary database path.
        offline: Fake client fixture.
        monkeypatch: Pytest monkeypatch fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    monkeypatch.setattr(getattr(cli, target), attribute, replacement)
    monkeypatch.setattr(cli.sources, "sync_wantlist", lambda conn, client, username: 4)
    code = cli.main(["--db", str(db_file), *command])
    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert expected_fragment in captured.out
    assert offline.closed is True


@pytest.mark.parametrize(
    ("pending", "expected_fragment"),
    [
        (0, "Nothing pending"),
        (3, "Processed 3"),
    ],
)
def test_enrich_reports_progress(
    pending: int,
    expected_fragment: str,
    db_file: Path,
    offline: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Enrich prints a progress readout and a summary without touching the network.

    Args:
        pending: Number of pooled releases waiting for enrichment.
        expected_fragment: Text expected in the output.
        db_file: Temporary database path.
        offline: Fake client fixture.
        monkeypatch: Pytest monkeypatch fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    conn = db.connect(db_file)
    db.init_db(conn)
    conn.executemany(
        "INSERT INTO pool (release_id, source, bank_key, discovered_at) VALUES (?, ?, NULL, ?)",
        [(index, "seller", "2026-01-01T00:00:00+00:00") for index in range(1, pending + 1)],
    )
    conn.commit()
    conn.close()

    def fake_enrich(
        conn: sqlite3.Connection,
        client: FakeClient,
        limit: int,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Pretend to enrich releases while driving the progress callback.

        Args:
            conn: Open SQLite connection.
            client: Fake client.
            limit: Requested batch size.
            progress: Progress callback.

        Returns:
            A fake result mapping.
        """
        total = min(limit, pending)
        for done in range(1, total + 1):
            if progress is not None:
                progress(done, total)
        return {
            "processed": total,
            "with_video": total,
            "from_master": 0,
            "videos": total,
            "failed": 0,
            "requests": total,
        }

    monkeypatch.setattr(cli.enrich, "enrich_pending", fake_enrich)
    code = cli.main(["--db", str(db_file), "enrich", "--limit", "10"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert expected_fragment in captured.out


def test_enrich_interrupt_is_reported(
    db_file: Path,
    offline: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl-C during enrichment reports the work already done instead of a traceback.

    Args:
        db_file: Temporary database path.
        offline: Fake client fixture.
        monkeypatch: Pytest monkeypatch fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    seed(db_file)
    conn = db.connect(db_file)
    conn.execute(
        "INSERT INTO pool (release_id, source, bank_key, discovered_at) VALUES (9, 'seller', "
        "NULL, '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    def interrupted(
        conn: sqlite3.Connection,
        client: FakeClient,
        limit: int,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Raise KeyboardInterrupt after reporting one finished release.

        Args:
            conn: Open SQLite connection.
            client: Fake client.
            limit: Requested batch size.
            progress: Progress callback.

        Returns:
            Never returns.

        Raises:
            KeyboardInterrupt: Always.
        """
        if progress is not None:
            progress(1, 5)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.enrich, "enrich_pending", interrupted)
    code = cli.main(["--db", str(db_file), "enrich"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_INTERRUPTED
    assert "Interrupted after 1 release" in captured.out
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("out_flag", [[], ["--out"]])
def test_export_writes_through_module(
    out_flag: list[str],
    db_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Export delegates to the export module and reports the destination.

    Args:
        out_flag: Either no output flag or the flag expecting a path.
        db_file: Temporary database path.
        tmp_path: Pytest supplied temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    module = FakeModule()
    monkeypatch.setattr(cli, "load_export", lambda: module)
    argv = ["--db", str(db_file), "export", *out_flag]
    if out_flag:
        argv.append(str(tmp_path / "crate.js"))
    code = cli.main(argv)
    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert len(module.calls) == 1
    assert "tracks" in captured.out


@pytest.mark.parametrize(
    ("argv", "expected_host", "expected_port"),
    [
        (["serve"], cli.DEFAULT_HOST, cli.DEFAULT_PORT),
        (["serve", "--host", "0.0.0.0", "--port", "9100"], "0.0.0.0", 9100),
    ],
)
def test_serve_passes_bind_settings(
    argv: list[str],
    expected_host: str,
    expected_port: int,
    db_file: Path,
    offline: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serve hands the database path and bind settings to the server module.

    Args:
        argv: Subcommand argument list.
        expected_host: Host the server should receive.
        expected_port: Port the server should receive.
        db_file: Temporary database path.
        offline: Fake client fixture.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    module = FakeModule()
    monkeypatch.setattr(cli, "load_server", lambda: module)
    code = cli.main(["--db", str(db_file), *argv])
    assert code == cli.EXIT_OK
    assert module.calls == [(db_file, "test-token", "egunduz", expected_host, expected_port)]


@pytest.mark.parametrize(
    "expected_fragment",
    ["VIDEO HIT RATE", "pool size", "allcitydublin", "exportable tracks"],
)
def test_stats_reports_the_key_numbers(
    expected_fragment: str,
    db_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stats leads with the video hit rate and lists the supporting counts.

    Args:
        expected_fragment: Text expected in the output.
        db_file: Temporary database path.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    seed(db_file)
    code = cli.main(["--db", str(db_file), "stats"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert expected_fragment in captured.out


@pytest.mark.parametrize(
    ("seeded", "expected_fragment"),
    [
        (False, "No enriched releases to sample"),
        (True, "By decade"),
    ],
)
def test_hitrate_breakdown(
    seeded: bool,
    expected_fragment: str,
    db_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Hitrate samples enriched releases and breaks the rate down by decade.

    Args:
        seeded: Whether the database holds enriched releases.
        expected_fragment: Text expected in the output.
        db_file: Temporary database path.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    if seeded:
        seed(db_file)
    code = cli.main(["--db", str(db_file), "hitrate", "--sample", "10"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert expected_fragment in captured.out


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "00:00:00"),
        (59.4, "00:00:59"),
        (61, "00:01:01"),
        (3661, "01:01:01"),
        (-5, "00:00:00"),
        (10716, "02:58:36"),
    ],
)
def test_duration_formatting(seconds: float, expected: str) -> None:
    """Durations format as a zero padded clock string.

    Args:
        seconds: Duration in seconds.
        expected: Expected formatted value.

    Returns:
        None.
    """
    assert cli.duration(seconds) == expected


@pytest.mark.parametrize(
    ("part", "whole", "expected"),
    [
        (0, 0, "n/a"),
        (1, 0, "n/a"),
        (1, 2, "50.0 percent"),
        (43, 100, "43.0 percent"),
    ],
)
def test_percent_formatting(part: int, whole: int, expected: str) -> None:
    """Ratios format as a percentage or as n/a when undefined.

    Args:
        part: Numerator.
        whole: Denominator.
        expected: Expected formatted value.

    Returns:
        None.
    """
    assert cli.percent(part, whole) == expected


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (1995, "1990s"),
        (1980, "1980s"),
        (2003, "2000s"),
        (None, "unknown"),
        (12, "unknown"),
    ],
)
def test_decade_label(year: int | None, expected: str) -> None:
    """Years bucket into decade labels with a fallback for missing data.

    Args:
        year: Release year.
        expected: Expected label.

    Returns:
        None.
    """
    assert cli.decade_label(year) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["Deep House", "Techno"]', ["Deep House", "Techno"]),
        ("[]", []),
        (None, []),
        ("", []),
        ("not json", []),
        ('{"style": "Techno"}', []),
        ("[1, \"Techno\"]", ["Techno"]),
    ],
)
def test_parse_styles(raw: str | None, expected: list[str]) -> None:
    """Stored style arrays decode defensively.

    Args:
        raw: Column value.
        expected: Expected style list.

    Returns:
        None.
    """
    assert cli.parse_styles(raw) == expected


@pytest.mark.parametrize(
    ("with_video", "enriched", "expected_fragment"),
    [
        (0, 0, "Nothing enriched yet"),
        (50, 100, "behaves like a radio"),
        (40, 100, "behaves like a radio"),
        (35, 100, "visible gaps"),
        (10, 100, "browsable shop list"),
    ],
)
def test_interpretation(with_video: int, enriched: int, expected_fragment: str) -> None:
    """The hit rate interpretation matches the documented thresholds.

    Args:
        with_video: Enriched releases with a video.
        enriched: Enriched releases in total.
        expected_fragment: Text expected in the sentence.

    Returns:
        None.
    """
    assert expected_fragment in cli.interpretation(with_video, enriched)


@pytest.mark.parametrize("bank_index", [-1, 99])
def test_crawl_rejects_out_of_range_bank(
    bank_index: int,
    db_file: Path,
    offline: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An out of range bank index is a configuration error, not a crash.

    Args:
        bank_index: Index outside the configured banks.
        db_file: Temporary database path.
        offline: Fake client fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    code = cli.main(["--db", str(db_file), "crawl", "--bank-index", str(bank_index)])
    captured = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert "out of range" in captured.err


@pytest.mark.parametrize("command", ["stats", "hitrate"])
def test_fresh_database_is_created_on_demand(command: str, db_file: Path) -> None:
    """Read only commands create the database file when it does not exist.

    Args:
        command: Subcommand under test.
        db_file: Temporary database path.

    Returns:
        None.
    """
    assert not db_file.exists()
    assert cli.main(["--db", str(db_file), command]) == cli.EXIT_OK
    assert db_file.exists()
