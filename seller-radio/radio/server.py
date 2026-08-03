"""Minimal localhost web server for the Seller Radio player.

The player itself is a static page, but adding a release to the Discogs wantlist needs an
authenticated write that a ``file://`` page can neither hold a secret for nor send past CORS.
This module is that single trusted component: it serves the static player, exposes a live crate
feed straight from SQLite, and proxies wantlist writes through a server side Discogs client so
the token never leaves the process.
"""

from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit

from radio.client import DiscogsClient
from radio.config import SELLERS, WEB_DIR
from radio.db import connect, init_db, now_iso

DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8733
INDEX_FILE: str = "index.html"
WANTLIST_PATH = re.compile(r"^/api/wantlist/([^/]*)/?$")
RELEASE_ID_PATTERN = re.compile(r"^[0-9]+$")
FORBIDDEN_HOSTS: frozenset[str] = frozenset({"", "0.0.0.0", "::", "*"})

CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

COUNT_QUERIES: dict[str, str] = {
    "listings_active": "SELECT COUNT(*) FROM listings WHERE active = 1",
    "releases": "SELECT COUNT(*) FROM releases",
    "with_video": "SELECT COUNT(*) FROM releases WHERE has_video = 1",
    "videos": "SELECT COUNT(*) FROM videos WHERE dead = 0",
    "pool": "SELECT COUNT(*) FROM pool",
    "owned": "SELECT COUNT(*) FROM owned",
    "wantlist": "SELECT COUNT(*) FROM wantlist",
}


class WantlistClient(Protocol):
    """Subset of the Discogs client the server depends on."""

    def put(self, path: str) -> bool:
        """Sends an authenticated PUT request.

        Args:
            path: API path relative to the Discogs API root.

        Returns:
            True when the API accepted the write.
        """
        ...

    def delete(self, path: str) -> bool:
        """Sends an authenticated DELETE request.

        Args:
            path: API path relative to the Discogs API root.

        Returns:
            True when the API accepted the delete.
        """
        ...


CrateBuilder = Callable[[sqlite3.Connection], dict]


@dataclass(frozen=True)
class ServerContext:
    """Everything a request handler needs, resolved once at startup.

    The Discogs token is deliberately absent: it lives inside the client only, so no handler can
    reach it and no response or log line can echo it.

    Attributes:
        db_path: Path to the SQLite database opened fresh for every request.
        client: Client used to proxy wantlist writes to Discogs.
        username: Discogs account whose wantlist is modified.
        web_dir: Resolved directory of static player assets.
        sellers: Seller handles reported by the status endpoint.
        crate_builder: Callable turning a connection into the crate payload.
    """

    db_path: Path
    client: WantlistClient
    username: str
    web_dir: Path
    sellers: list[str] = field(default_factory=list)
    crate_builder: CrateBuilder | None = None


def _default_crate_builder(conn: sqlite3.Connection) -> dict:
    """Builds the crate payload with the project exporter.

    The import is deferred so the server module stays importable when the exporter is absent.

    Args:
        conn: Open SQLite connection.

    Returns:
        The crate payload.
    """
    from radio.export import build_crate

    return build_crate(conn)


class RadioHandler(BaseHTTPRequestHandler):
    """Routes player asset reads, crate reads, and wantlist writes.

    Attributes:
        context: Server context injected by the factory onto the handler subclass.
        server_version: Product token sent in the Server header.
    """

    context: ServerContext
    server_version = "SellerRadio"
    sys_version = ""

    def do_GET(self) -> None:
        """Handles a GET request for the player assets, the crate, or the status payload."""
        path = self._path()
        if path == "/api/crate":
            self._handle_crate()
            return
        if path == "/api/status":
            self._handle_status()
            return
        if path.startswith("/api/"):
            self._send_json(404, {"ok": False, "error": "unknown endpoint"})
            return
        self._handle_static(path)

    def do_POST(self) -> None:
        """Handles a POST request, which only ever adds a release to the wantlist."""
        self._drain_body()
        if not self._same_origin():
            return
        match = WANTLIST_PATH.match(self._path())
        if match is None:
            self._send_json(404, {"ok": False, "error": "unknown endpoint"})
            return
        self._handle_wantlist(match.group(1), wanted=True)

    def do_DELETE(self) -> None:
        """Handles a DELETE request, which only ever removes a release from the wantlist."""
        self._drain_body()
        if not self._same_origin():
            return
        match = WANTLIST_PATH.match(self._path())
        if match is None:
            self._send_json(404, {"ok": False, "error": "unknown endpoint"})
            return
        self._handle_wantlist(match.group(1), wanted=False)

    def _same_origin(self) -> bool:
        """Rejects a state changing request that a foreign page triggered.

        A plain POST is not preflighted, so any site the user happens to be browsing could
        otherwise reach this loopback server and write to the Discogs wantlist with the token
        held here. Requests carrying no Origin header are same origin by construction, since a
        browser always attaches one to a cross site request.

        Returns:
            True when the request may proceed. False after an error response was already sent.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = self.headers.get("Host") or ""
        allowed = {f"http://{host}", f"https://{host}"}
        if origin in allowed:
            return True
        self._send_json(403, {"ok": False, "error": "cross origin request refused"})
        return False

    def log_message(self, format: str, *args: object) -> None:
        """Writes a compact access log line without any query string.

        Query strings are dropped because they are the one part of a request that could ever
        carry a secret, and the standard implementation would echo them verbatim.

        Args:
            format: Standard library format string, unused.
            *args: Standard library format arguments, unused.

        Returns:
            None.
        """
        raw = getattr(self, "path", "")
        safe = raw.split("?", 1)[0] if isinstance(raw, str) else ""
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.command} {safe}\n")

    def _path(self) -> str:
        """Returns the request path without the query string.

        Returns:
            The raw, still percent encoded path component.
        """
        return urlsplit(self.path).path or "/"

    def _drain_body(self) -> None:
        """Reads and discards any request body so the connection closes cleanly."""
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw is not None else 0
        except ValueError:
            length = 0
        if length > 0:
            self.rfile.read(length)

    def _handle_static(self, path: str) -> None:
        """Serves a file from the web directory, refusing anything outside it.

        Args:
            path: Request path component.

        Returns:
            None.
        """
        relative = unquote(path).lstrip("/")
        if "\x00" in relative:
            self._send_json(403, {"ok": False, "error": "forbidden path"})
            return
        if not relative:
            relative = INDEX_FILE

        root = self.context.web_dir
        try:
            candidate = (root / relative).resolve()
        except (OSError, ValueError):
            self._send_json(403, {"ok": False, "error": "forbidden path"})
            return

        if candidate != root and root not in candidate.parents:
            self._send_json(403, {"ok": False, "error": "forbidden path"})
            return

        if candidate.is_dir():
            candidate = candidate / INDEX_FILE

        if not candidate.is_file():
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        try:
            payload = candidate.read_bytes()
        except OSError:
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        self._send_bytes(200, payload, _content_type(candidate))

    def _handle_crate(self) -> None:
        """Builds the crate payload live from the database and sends it as JSON."""
        try:
            with self._db() as conn:
                builder = self.context.crate_builder or _default_crate_builder
                crate = builder(conn)
        except ImportError:
            self._send_json(503, {"ok": False, "error": "crate exporter unavailable"})
            return
        except (sqlite3.Error, OSError, ValueError) as exc:
            self._send_json(500, {"ok": False, "error": type(exc).__name__})
            return
        self._send_json(200, crate)

    def _handle_status(self) -> None:
        """Sends the account, seller, and row count summary as JSON."""
        counts: dict[str, int] = {}
        try:
            with self._db() as conn:
                for name, sql in COUNT_QUERIES.items():
                    counts[name] = int(conn.execute(sql).fetchone()[0])
        except (sqlite3.Error, OSError) as exc:
            self._send_json(500, {"ok": False, "error": type(exc).__name__})
            return
        self._send_json(
            200,
            {
                "username": self.context.username,
                "sellers": list(self.context.sellers),
                "counts": counts,
                "server": True,
            },
        )

    def _handle_wantlist(self, raw_release_id: str, wanted: bool) -> None:
        """Proxies a wantlist write to Discogs and mirrors it into the local table.

        Args:
            raw_release_id: Unvalidated release id captured from the request path.
            wanted: True to add the release, False to remove it.

        Returns:
            None.
        """
        release_id = _parse_release_id(raw_release_id)
        if release_id is None:
            self._send_json(400, {"ok": False, "error": "release_id must be a positive integer"})
            return

        path = f"/users/{quote(self.context.username, safe='')}/wants/{release_id}"
        client = self.context.client
        accepted = client.put(path) if wanted else client.delete(path)
        if not accepted:
            self._send_json(
                502,
                {
                    "ok": False,
                    "release_id": release_id,
                    "wanted": not wanted,
                    "error": "discogs rejected the request",
                },
            )
            return

        try:
            with self._db() as conn:
                if wanted:
                    conn.execute(
                        "INSERT INTO wantlist (release_id, synced_at) VALUES (?, ?) "
                        "ON CONFLICT(release_id) DO UPDATE SET synced_at=excluded.synced_at",
                        (release_id, now_iso()),
                    )
                else:
                    conn.execute("DELETE FROM wantlist WHERE release_id = ?", (release_id,))
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            self._send_json(
                500,
                {"ok": False, "release_id": release_id, "error": type(exc).__name__},
            )
            return

        self._send_json(200, {"ok": True, "release_id": release_id, "wanted": wanted})

    def _db(self) -> _closing:
        """Opens a private connection for the current request.

        SQLite connections are not safe to share between threads and this server is threaded, so
        every request gets its own short lived connection instead of a shared, locked one.

        Returns:
            A context manager yielding an open connection and closing it on exit.
        """
        return _closing(connect(self.context.db_path))

    def _send_json(self, status: int, payload: dict) -> None:
        """Sends a JSON body, never an HTML traceback.

        Args:
            status: HTTP status code.
            payload: JSON serialisable body.

        Returns:
            None.
        """
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            status = 500
            body = b'{"ok": false, "error": "unserialisable payload"}'
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        """Writes a complete response.

        Args:
            status: HTTP status code.
            body: Raw response body.
            content_type: Value of the Content-Type header.

        Returns:
            None.
        """
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class _closing:
    """Context manager closing a SQLite connection on exit.

    Attributes:
        conn: The wrapped connection.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Wraps a connection.

        Args:
            conn: Connection to close on exit.
        """
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        """Returns the wrapped connection.

        Returns:
            The open connection.
        """
        return self.conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Closes the wrapped connection.

        Args:
            exc_type: Exception type, if any.
            exc: Exception instance, if any.
            tb: Traceback, if any.

        Returns:
            None.
        """
        self.conn.close()


def _content_type(path: Path) -> str:
    """Resolves the Content-Type for a static file.

    Args:
        path: Path of the file being served.

    Returns:
        A Content-Type header value, defaulting to a binary type.
    """
    suffix = path.suffix.lower()
    if suffix in CONTENT_TYPES:
        return CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _parse_release_id(raw: str) -> int | None:
    """Validates a release id taken from a request path.

    Args:
        raw: Still percent encoded path segment.

    Returns:
        The positive integer release id, or None when the segment is not one.
    """
    candidate = unquote(raw).strip()
    if not RELEASE_ID_PATTERN.match(candidate):
        return None
    value = int(candidate)
    return value if value > 0 else None


def create_server(
    db_path: Path | str,
    client: WantlistClient,
    username: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    web_dir: Path | str = WEB_DIR,
    sellers: list[str] | None = None,
    crate_builder: CrateBuilder | None = None,
) -> ThreadingHTTPServer:
    """Builds a bound, ready to run HTTP server.

    Args:
        db_path: Path to the SQLite database.
        client: Client used to proxy wantlist writes.
        username: Discogs account whose wantlist is modified.
        host: Interface to bind, restricted to loopback style addresses.
        port: TCP port, 0 lets the operating system choose a free one.
        web_dir: Directory of static player assets.
        sellers: Seller handles reported by the status endpoint.
        crate_builder: Optional override for the crate payload builder.

    Returns:
        A bound server whose ``serve_forever`` has not been called yet.

    Raises:
        ValueError: If the host would expose the server beyond the local machine.
    """
    if host in FORBIDDEN_HOSTS:
        raise ValueError(f"Refusing to bind to {host!r}: this server must stay on loopback")

    resolved_db = Path(db_path)
    resolved_web = Path(web_dir).resolve()
    conn = connect(resolved_db)
    try:
        init_db(conn)
    finally:
        conn.close()

    context = ServerContext(
        db_path=resolved_db,
        client=client,
        username=username,
        web_dir=resolved_web,
        sellers=list(sellers if sellers is not None else SELLERS),
        crate_builder=crate_builder,
    )

    handler = type("BoundRadioHandler", (RadioHandler,), {"context": context})

    class RadioServer(ThreadingHTTPServer):
        """Threaded server whose worker threads never outlive the process."""

        daemon_threads = True
        allow_reuse_address = True

    return RadioServer((host, port), handler)


def serve(
    db_path: Path,
    token: str,
    username: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    """Runs the player server until interrupted.

    Args:
        db_path: Path to the SQLite database.
        token: Discogs personal access token, kept inside the client only.
        username: Discogs account whose wantlist is modified.
        host: Interface to bind, defaults to loopback.
        port: TCP port to listen on.

    Returns:
        None.
    """
    client = DiscogsClient(token)
    httpd = create_server(db_path, client, username, host=host, port=port)
    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
    print(f"Seller Radio is playing at http://{bound_host}:{bound_port}/")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Seller Radio.")
    finally:
        httpd.shutdown()
        httpd.server_close()
        close = getattr(client, "close", None)
        if callable(close):
            close()
