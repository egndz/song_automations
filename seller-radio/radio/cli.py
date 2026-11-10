"""Command line interface for Seller Radio.

Every subcommand opens the database and applies the schema before doing anything else, so a
fresh clone works without a setup step. Network credentials are resolved lazily and a missing
token is reported as a plain, actionable message rather than a traceback.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from radio import config, db, enrich, sources
from radio.client import DiscogsClient

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:
    Console = None
    Table = None

_CONSOLE = Console() if Console is not None else None

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

DEFAULT_ENRICH_LIMIT = 500
DEFAULT_HITRATE_SAMPLE = 500
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8733
RADIO_THRESHOLD = 0.40
SHOP_THRESHOLD = 0.30
REQUESTS_PER_MINUTE = 60.0
PROGRESS_EVERY = 25
MAX_STYLE_ROWS = 15
MIN_STYLE_SAMPLE = 5

PROGRAM = "radio"
DESCRIPTION = "Turn a Discogs seller inventory into a playable crate."


@dataclass
class ProgressState:
    """Mutable bookkeeping for a long running enrichment run.

    Attributes:
        done: Number of releases processed so far.
        total: Number of releases scheduled for this run.
        started: Monotonic clock reading taken when the run began.
    """

    done: int = 0
    total: int = 0
    started: float = 0.0


def out(message: str = "") -> None:
    """Print a line of normal output.

    Args:
        message: Text to print.

    Returns:
        None.
    """
    if _CONSOLE is not None:
        _CONSOLE.print(message, markup=False, highlight=False)
    else:
        print(message)


def heading(message: str) -> None:
    """Print a section heading.

    Args:
        message: Text to print.

    Returns:
        None.
    """
    if _CONSOLE is not None:
        _CONSOLE.print(message, style="bold", markup=False, highlight=False)
    else:
        print(message)


def error(message: str) -> None:
    """Print an error line to standard error.

    Args:
        message: Text to print.

    Returns:
        None.
    """
    print(message, file=sys.stderr)


def table(title: str, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Print a table using Rich when it is installed and plain text otherwise.

    Args:
        title: Table caption.
        columns: Column headers.
        rows: Row values, already converted to strings.

    Returns:
        None.
    """
    if not rows:
        out(f"{title}: nothing to show")
        return
    if _CONSOLE is not None and Table is not None:
        rich_table = Table(title=title, title_justify="left")
        for column in columns:
            rich_table.add_column(column)
        for row in rows:
            rich_table.add_row(*[str(value) for value in row])
        _CONSOLE.print(rich_table)
        return

    widths = [len(str(column)) for column in columns]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))
    print(title)
    print("  ".join(str(column).ljust(widths[index]) for index, column in enumerate(columns)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(value).ljust(widths[index]) for index, value in enumerate(row)))


def number(value: int | None) -> str:
    """Format an integer with thousands separators.

    Args:
        value: Number to format, or None.

    Returns:
        Formatted number, or "0" when the value is None.
    """
    return f"{value or 0:,}"


def percent(part: int, whole: int) -> str:
    """Format a ratio as a percentage string.

    Args:
        part: Numerator.
        whole: Denominator.

    Returns:
        A string such as "42.3 percent", or "n/a" when the denominator is zero.
    """
    if whole <= 0:
        return "n/a"
    return f"{100.0 * part / whole:.1f} percent"


def duration(seconds: float) -> str:
    """Format a number of seconds as a clock style duration.

    Args:
        seconds: Duration in seconds.

    Returns:
        A string of the form "HH:MM:SS".
    """
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open the database and make sure the schema exists.

    Args:
        db_path: Path to the SQLite file.

    Returns:
        An initialized connection.
    """
    conn = db.connect(db_path)
    db.init_db(conn)
    return conn


def resolve_token() -> str | None:
    """Resolve the Discogs token, reporting a readable error when it is missing.

    Returns:
        The token, or None when no token could be found.
    """
    try:
        return config.get_token()
    except RuntimeError as exc:
        error(f"Configuration error: {exc}")
        return None


def make_client(token: str) -> DiscogsClient:
    """Build a Discogs client with the project user agent.

    Args:
        token: Discogs personal access token.

    Returns:
        A ready to use client.
    """
    return DiscogsClient(token, user_agent=config.USER_AGENT, base_url=config.BASE_URL)


def load_export() -> ModuleType:
    """Import the export module on demand.

    Returns:
        The radio.export module.
    """
    from radio import export

    return export


def load_server() -> ModuleType:
    """Import the server module on demand.

    Returns:
        The radio.server module.
    """
    from radio import server

    return server


def cmd_sync(args: argparse.Namespace) -> int:
    """Sync seller inventories into the listings table and the pool.

    Args:
        args: Parsed arguments carrying db and seller.

    Returns:
        Process exit code.
    """
    token = resolve_token()
    if token is None:
        return EXIT_CONFIG

    sellers = [args.seller] if args.seller else list(config.SELLERS)
    if not sellers:
        error("No sellers configured. Add one to SELLERS in radio/config.py.")
        return EXIT_CONFIG

    conn = open_db(args.db)
    client = make_client(token)
    try:
        heading(f"Syncing {len(sellers)} seller inventory(s)")
        result = sources.sync_sellers(conn, client, sellers)
    except KeyboardInterrupt:
        out("")
        out("Interrupted. Listings written before the interrupt are already saved.")
        return EXIT_INTERRUPTED
    finally:
        client.close()
        conn.close()

    rows = [
        [
            str(entry.get("seller", "")),
            number(entry.get("listings")),
            number(entry.get("new_releases")),
            number(entry.get("deactivated")),
            str(entry.get("error") or ""),
        ]
        for entry in result.get("results", [])
    ]
    table(
        "Seller sync",
        ["seller", "active listings", "new releases", "deactivated", "error"],
        rows,
    )
    out(
        f"Total active listings {number(result.get('listings'))}, "
        f"new releases pooled {number(result.get('new_releases'))}, "
        f"deactivated {number(result.get('deactivated'))}."
    )
    errors = result.get("errors") or []
    if errors:
        error(f"Failed sellers: {', '.join(str(name) for name in errors)}")
        return EXIT_FAIL
    return EXIT_OK


def cmd_crawl(args: argparse.Namespace) -> int:
    """Crawl the configured search banks into the pool.

    Args:
        args: Parsed arguments carrying db and bank_index.

    Returns:
        Process exit code.
    """
    token = resolve_token()
    if token is None:
        return EXIT_CONFIG

    banks = list(config.SEARCH_BANKS)
    if not banks:
        error("No search banks configured. Add one to SEARCH_BANKS in radio/config.py.")
        return EXIT_CONFIG
    if args.bank_index is not None:
        if args.bank_index < 0 or args.bank_index >= len(banks):
            error(f"Bank index {args.bank_index} is out of range 0 to {len(banks) - 1}.")
            return EXIT_CONFIG
        banks = [banks[args.bank_index]]

    conn = open_db(args.db)
    client = make_client(token)
    try:
        heading(f"Crawling {len(banks)} search bank(s)")
        result = sources.crawl_banks(conn, client, banks)
    except KeyboardInterrupt:
        out("")
        out("Interrupted. Release ids discovered before the interrupt are already pooled.")
        return EXIT_INTERRUPTED
    finally:
        client.close()
        conn.close()

    rows = [
        [
            str(entry.get("bank_key", "")),
            number(entry.get("pages")),
            number(entry.get("found")),
            number(entry.get("new")),
            "yes" if entry.get("capped") else "no",
        ]
        for entry in result.get("results", [])
    ]
    table("Search banks", ["bank", "pages", "results seen", "new in pool", "hit cap"], rows)
    out(
        f"Total results seen {number(result.get('found'))}, "
        f"new releases pooled {number(result.get('new'))}."
    )
    capped = result.get("capped") or []
    if capped:
        out(
            f"{len(capped)} bank(s) stopped at the {number(config.MAX_SEARCH_ITEMS)} item "
            "search cap. Narrow them by year or country to reach the rest."
        )
    return EXIT_OK


def cmd_enrich(args: argparse.Namespace) -> int:
    """Enrich pooled releases with metadata and videos.

    Args:
        args: Parsed arguments carrying db and limit.

    Returns:
        Process exit code.
    """
    token = resolve_token()
    if token is None:
        return EXIT_CONFIG

    conn = open_db(args.db)
    pending_before = enrich.pending_count(conn)
    if pending_before == 0:
        out("Nothing pending. Run sync or crawl first to fill the pool.")
        conn.close()
        return EXIT_OK

    limit = max(0, args.limit)
    planned = min(limit, pending_before)
    heading(f"Enriching {number(planned)} of {number(pending_before)} pending releases")
    out("One request per release at 60 requests per minute. Press Ctrl-C to stop safely.")

    state = ProgressState(started=time.monotonic())
    interactive = sys.stdout.isatty()

    def report(done: int, total: int) -> None:
        """Print a running progress line with an estimate of the time remaining.

        Args:
            done: Releases finished so far.
            total: Releases scheduled for this run.

        Returns:
            None.
        """
        state.done = done
        state.total = total
        if not interactive and done % PROGRESS_EVERY != 0 and done != total:
            return
        line = progress_line(state)
        if interactive:
            sys.stdout.write(f"\r{line}")
        else:
            sys.stdout.write(f"{line}\n")
        sys.stdout.flush()

    client = make_client(token)
    interrupted = False
    result: dict = {}
    try:
        result = enrich.enrich_pending(conn, client, limit, report)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if interactive and state.done:
            sys.stdout.write("\n")
            sys.stdout.flush()
        client.close()

    pending_after = enrich.pending_count(conn)
    with_video = scalar(conn, "SELECT COUNT(*) AS n FROM releases WHERE has_video = 1")
    enriched = scalar(conn, "SELECT COUNT(*) AS n FROM releases WHERE fetched_at IS NOT NULL")
    conn.close()

    if interrupted:
        elapsed = time.monotonic() - state.started
        out(
            f"Interrupted after {number(state.done)} release(s) in {duration(elapsed)}. "
            "Every finished release is already committed, so rerunning resumes here."
        )
    else:
        out(
            f"Processed {number(result.get('processed'))}, "
            f"with video {number(result.get('with_video'))}, "
            f"from master {number(result.get('from_master'))}, "
            f"videos stored {number(result.get('videos'))}, "
            f"failed {number(result.get('failed'))}, "
            f"requests {number(result.get('requests'))}."
        )
    out(f"Video hit rate so far {percent(with_video, enriched)} of {number(enriched)} enriched.")
    out(
        f"Still pending {number(pending_after)}. "
        f"Estimated time to finish {eta_text(pending_after)}."
    )
    return EXIT_INTERRUPTED if interrupted else EXIT_OK


def progress_line(state: ProgressState) -> str:
    """Build the single line progress readout for an enrichment run.

    Args:
        state: Live counters for the run.

    Returns:
        A padded status line safe to rewrite in place.
    """
    elapsed = max(0.001, time.monotonic() - state.started)
    rate = state.done / elapsed
    remaining = max(0, state.total - state.done)
    eta = remaining / rate if rate > 0 else 0.0
    share = percent(state.done, state.total)
    line = (
        f"enrich {state.done}/{state.total} {share} "
        f"elapsed {duration(elapsed)} eta {duration(eta)} "
        f"{rate * 60:.1f} releases/min"
    )
    return line.ljust(88)


def eta_text(pending: int) -> str:
    """Estimate how long the remaining pending releases will take.

    Args:
        pending: Number of releases still to enrich.

    Returns:
        A clock style duration assuming the documented 60 requests per minute budget.
    """
    return duration(pending * 60.0 / REQUESTS_PER_MINUTE)


def cmd_export(args: argparse.Namespace) -> int:
    """Write the crate file consumed by the web player.

    Args:
        args: Parsed arguments carrying db and out.

    Returns:
        Process exit code.
    """
    conn = open_db(args.db)
    target = Path(args.out) if args.out else config.CRATE_JS_PATH
    try:
        export = load_export()
        result = export.write_crate(conn, target)
    except (ImportError, OSError) as exc:
        error(f"Export failed: {exc}")
        return EXIT_FAIL
    finally:
        conn.close()

    heading(f"Wrote {target}")
    rows = [[str(key), str(value)] for key, value in sorted(result.items())]
    table("Crate", ["field", "value"], rows)
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the local player server.

    Args:
        args: Parsed arguments carrying db, host, and port.

    Returns:
        Process exit code.
    """
    token = resolve_token()
    if token is None:
        return EXIT_CONFIG

    conn = open_db(args.db)
    conn.close()

    username = config.get_username()
    heading(f"Serving on http://{args.host}:{args.port} as {username}")
    out("Press Ctrl-C to stop.")
    try:
        server = load_server()
        server.serve(args.db, token, username, host=args.host, port=args.port)
    except KeyboardInterrupt:
        out("")
        out("Server stopped.")
        return EXIT_OK
    except (ImportError, OSError) as exc:
        error(f"Server failed: {exc}")
        return EXIT_FAIL
    return EXIT_OK


def cmd_owned(args: argparse.Namespace) -> int:
    """Sync the collection and the wantlist used by the exclusion filters.

    Args:
        args: Parsed arguments carrying db.

    Returns:
        Process exit code.
    """
    token = resolve_token()
    if token is None:
        return EXIT_CONFIG

    username = config.get_username()
    conn = open_db(args.db)
    client = make_client(token)
    try:
        heading(f"Syncing collection and wantlist for {username}")
        owned = sources.sync_owned(conn, client, username)
        wanted = sources.sync_wantlist(conn, client, username)
    except KeyboardInterrupt:
        out("")
        out("Interrupted before both accounts finished syncing.")
        return EXIT_INTERRUPTED
    finally:
        client.close()
        conn.close()

    table(
        "Account sync",
        ["table", "releases"],
        [["owned", number(owned)], ["wantlist", number(wanted)]],
    )
    return EXIT_OK


def scalar(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> int:
    """Run a counting query and return its single integer result.

    Args:
        conn: Open SQLite connection.
        sql: Query selecting a single column aliased as n.
        params: Query parameters.

    Returns:
        The counted value, zero when the query returned no row.
    """
    row = conn.execute(sql, tuple(params)).fetchone()
    if row is None or row["n"] is None:
        return 0
    return int(row["n"])


def interpretation(with_video: int, enriched: int) -> str:
    """Describe what the current video hit rate means for daily use.

    Args:
        with_video: Enriched releases carrying at least one video.
        enriched: Enriched releases in total.

    Returns:
        A single sentence recommendation.
    """
    if enriched <= 0:
        return "Nothing enriched yet. Run enrich before judging the hit rate."
    rate = with_video / enriched
    if rate >= RADIO_THRESHOLD:
        return (
            "At or above 40 percent this behaves like a radio. Hit play and let it run "
            "unattended."
        )
    if rate >= SHOP_THRESHOLD:
        return (
            "Between 30 and 40 percent it plays but with visible gaps. Keep enriching, and "
            "treat the gaps as browsing rather than listening."
        )
    return (
        "Below 30 percent it is not a radio. Use it as a browsable shop list with preview "
        "buttons on the records that do have audio."
    )


def cmd_stats(args: argparse.Namespace) -> int:
    """Print the numbers that decide whether the project works.

    Args:
        args: Parsed arguments carrying db.

    Returns:
        Process exit code.
    """
    conn = open_db(args.db)
    try:
        pool_total = scalar(conn, "SELECT COUNT(*) AS n FROM pool")
        from_seller = scalar(conn, "SELECT COUNT(*) AS n FROM pool WHERE source = 'seller'")
        from_search = scalar(conn, "SELECT COUNT(*) AS n FROM pool WHERE source = 'search'")
        enriched = scalar(conn, "SELECT COUNT(*) AS n FROM releases WHERE fetched_at IS NOT NULL")
        with_video = scalar(conn, "SELECT COUNT(*) AS n FROM releases WHERE has_video = 1")
        pending = enrich.pending_count(conn)
        playable = scalar(
            conn, "SELECT COUNT(DISTINCT release_id) AS n FROM videos WHERE dead = 0"
        )
        videos = scalar(conn, "SELECT COUNT(*) AS n FROM videos WHERE dead = 0")
        owned = scalar(conn, "SELECT COUNT(*) AS n FROM owned")
        wanted = scalar(conn, "SELECT COUNT(*) AS n FROM wantlist")
        seller_rows = conn.execute(
            "SELECT seller, COUNT(*) AS n FROM listings WHERE active = 1 "
            "GROUP BY seller ORDER BY n DESC"
        ).fetchall()
        sync_rows = conn.execute(
            "SELECT key, synced_at, n FROM sync_log ORDER BY synced_at DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()

    heading("")
    heading(f"VIDEO HIT RATE   {percent(with_video, enriched)}")
    out(f"{number(with_video)} of {number(enriched)} enriched releases have a playable video.")
    out(interpretation(with_video, enriched))
    out("")

    table(
        "Pool",
        ["metric", "value"],
        [
            ["pool size", number(pool_total)],
            ["from seller", number(from_seller)],
            ["from search", number(from_search)],
            ["enriched", number(enriched)],
            ["pending", number(pending)],
            ["releases with video", number(with_video)],
            ["videos stored", number(videos)],
            ["exportable tracks", number(playable)],
            ["owned releases", number(owned)],
            ["wantlist releases", number(wanted)],
        ],
    )

    table(
        "Active listings",
        ["seller", "listings"],
        [[str(row["seller"]), number(row["n"])] for row in seller_rows],
    )

    crate_path = config.CRATE_JS_PATH
    if crate_path.is_file():
        size_kb = crate_path.stat().st_size / 1024.0
        out(f"Crate file {crate_path} present, {size_kb:.0f} KB.")
    else:
        out(f"Crate file {crate_path} not written yet. Run export.")

    if sync_rows:
        table(
            "Recent syncs",
            ["step", "when", "items"],
            [[str(row["key"]), str(row["synced_at"]), number(row["n"])] for row in sync_rows],
        )
    if pending:
        out(f"Enriching the remaining {number(pending)} releases takes about {eta_text(pending)}.")
    return EXIT_OK


def decade_label(year: int | None) -> str:
    """Bucket a release year into a decade label.

    Args:
        year: Release year, or None.

    Returns:
        A label such as "1990s", or "unknown" when the year is missing or implausible.
    """
    if year is None or year < 1900 or year > 2100:
        return "unknown"
    return f"{(year // 10) * 10}s"


def parse_styles(raw: str | None) -> list[str]:
    """Decode the JSON array stored in the styles column.

    Args:
        raw: Column value, expected to be a JSON array of strings.

    Returns:
        List of style names, empty when the value is missing or malformed.
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def cmd_hitrate(args: argparse.Namespace) -> int:
    """Sample enriched releases and break the video hit rate down by decade and style.

    Args:
        args: Parsed arguments carrying db and sample.

    Returns:
        Process exit code.
    """
    conn = open_db(args.db)
    try:
        rows = conn.execute(
            "SELECT r.year AS year, r.styles AS styles, r.has_video AS has_video "
            "FROM pool AS p JOIN releases AS r ON r.release_id = p.release_id "
            "WHERE r.fetched_at IS NOT NULL ORDER BY RANDOM() LIMIT ?",
            (max(1, args.sample),),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        out("No enriched releases to sample yet. Run enrich first.")
        return EXIT_OK

    hits = sum(1 for row in rows if row["has_video"])
    heading(f"Sampled {number(len(rows))} enriched releases")
    heading(f"VIDEO HIT RATE   {percent(hits, len(rows))}")
    out(interpretation(hits, len(rows)))
    out("")

    by_decade: dict[str, list[int]] = {}
    by_style: dict[str, list[int]] = {}
    for row in rows:
        hit = 1 if row["has_video"] else 0
        decade = decade_label(row["year"])
        bucket = by_decade.setdefault(decade, [0, 0])
        bucket[0] += 1
        bucket[1] += hit
        for style in parse_styles(row["styles"]):
            style_bucket = by_style.setdefault(style, [0, 0])
            style_bucket[0] += 1
            style_bucket[1] += hit

    table(
        "By decade",
        ["decade", "sampled", "with video", "hit rate"],
        [
            [key, number(counts[0]), number(counts[1]), percent(counts[1], counts[0])]
            for key, counts in sorted(by_decade.items())
        ],
    )

    ranked = [
        (key, counts)
        for key, counts in by_style.items()
        if counts[0] >= min(MIN_STYLE_SAMPLE, len(rows))
    ]
    ranked.sort(key=lambda item: (-(item[1][1] / item[1][0]), -item[1][0]))
    table(
        f"By style, top {MAX_STYLE_ROWS} with at least {MIN_STYLE_SAMPLE} samples",
        ["style", "sampled", "with video", "hit rate"],
        [
            [key, number(counts[0]), number(counts[1]), percent(counts[1], counts[0])]
            for key, counts in ranked[:MAX_STYLE_ROWS]
        ],
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with every subcommand attached.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(prog=PROGRAM, description=DESCRIPTION)
    parser.add_argument(
        "--db",
        type=Path,
        default=config.DB_PATH,
        help=f"SQLite database path, default {config.DB_PATH}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    sync = subparsers.add_parser("sync", help="sync seller inventories into the pool")
    sync.add_argument("--seller", default=None, help="one seller name, default all in config")
    sync.set_defaults(handler=cmd_sync)

    crawl = subparsers.add_parser("crawl", help="crawl search banks into the pool")
    crawl.add_argument(
        "--bank-index",
        type=int,
        default=None,
        help="index of a single bank in SEARCH_BANKS, default all",
    )
    crawl.set_defaults(handler=cmd_crawl)

    enrich_parser = subparsers.add_parser("enrich", help="fetch metadata and videos")
    enrich_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_ENRICH_LIMIT,
        help=f"maximum releases to enrich in this run, default {DEFAULT_ENRICH_LIMIT}",
    )
    enrich_parser.set_defaults(handler=cmd_enrich)

    export_parser = subparsers.add_parser("export", help="write the crate file for the player")
    export_parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"output path, default {config.CRATE_JS_PATH}",
    )
    export_parser.set_defaults(handler=cmd_export)

    serve = subparsers.add_parser("serve", help="run the local player server")
    serve.add_argument("--host", default=DEFAULT_HOST, help=f"bind host, default {DEFAULT_HOST}")
    serve.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"bind port, default {DEFAULT_PORT}"
    )
    serve.set_defaults(handler=cmd_serve)

    owned = subparsers.add_parser("owned", help="sync your collection and wantlist")
    owned.set_defaults(handler=cmd_owned)

    stats = subparsers.add_parser("stats", help="print pool, video, and listing statistics")
    stats.set_defaults(handler=cmd_stats)

    hitrate = subparsers.add_parser("hitrate", help="video hit rate by decade and style")
    hitrate.add_argument(
        "--sample",
        type=int,
        default=DEFAULT_HITRATE_SAMPLE,
        help=f"number of enriched releases to sample, default {DEFAULT_HITRATE_SAMPLE}",
    )
    hitrate.set_defaults(handler=cmd_hitrate)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line interface.

    Args:
        argv: Argument list, defaulting to sys.argv[1:].

    Returns:
        Process exit code.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else EXIT_OK

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_CONFIG

    try:
        return int(handler(args))
    except KeyboardInterrupt:
        out("")
        out("Interrupted.")
        return EXIT_INTERRUPTED
    except sqlite3.Error as exc:
        error(f"Database error: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
