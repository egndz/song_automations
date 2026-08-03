"""Tests for the local player server, driven over real HTTP against a stubbed Discogs client."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from radio import server as server_module
from radio.server import create_server

TOKEN = "super-secret-token-value"
USERNAME = "egunduz"
SELLERS = ["allcitydublin"]
INDEX_HTML = "<!doctype html><html><body>Seller Radio</body></html>"
APP_JS = "window.RADIO = true;"
STYLES_CSS = "body { margin: 0; }"


@dataclass
class FakeClient:
    """Discogs client stub that records writes instead of performing them.

    Attributes:
        ok: Whether the stub reports success for every call.
        calls: Recorded (method, path) pairs in call order.
    """

    ok: bool = True
    calls: list[tuple[str, str]] = field(default_factory=list)

    def put(self, path: str) -> bool:
        """Records a PUT call.

        Args:
            path: API path requested by the server.

        Returns:
            The configured success flag.
        """
        self.calls.append(("PUT", path))
        return self.ok

    def delete(self, path: str) -> bool:
        """Records a DELETE call.

        Args:
            path: API path requested by the server.

        Returns:
            The configured success flag.
        """
        self.calls.append(("DELETE", path))
        return self.ok


@dataclass
class Harness:
    """A running server plus everything a test needs to inspect it.

    Attributes:
        base_url: Root URL of the bound server.
        client: The Discogs client stub the server proxies through.
        db_path: Path to the SQLite database the server reads and writes.
        web_dir: Directory of static assets being served.
    """

    base_url: str
    client: FakeClient
    db_path: Path
    web_dir: Path


@dataclass
class Response:
    """A decoded HTTP response.

    Attributes:
        status: HTTP status code.
        body: Response body decoded as text.
        headers: Response headers as a plain mapping.
    """

    status: int
    body: str
    headers: dict[str, str]

    def json(self) -> dict:
        """Decodes the body as JSON.

        Returns:
            The decoded JSON object.
        """
        return json.loads(self.body)


def fake_crate(conn: sqlite3.Connection) -> dict:
    """Builds a minimal crate payload from the database.

    Args:
        conn: Open SQLite connection.

    Returns:
        A crate shaped payload.
    """
    releases = conn.execute("SELECT release_id FROM releases").fetchall()
    return {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "sellers": list(SELLERS),
        "stats": {
            "tracks": len(releases),
            "releases": len(releases),
            "with_video": len(releases),
            "listings_active": 0,
        },
        "tracks": [{"release_id": int(row[0])} for row in releases],
    }


def request(
    base_url: str,
    path: str,
    method: str = "GET",
    data: bytes | None = None,
) -> Response:
    """Performs an HTTP request without raising on error statuses.

    Args:
        base_url: Root URL of the server.
        path: Request path, sent as given so traversal attempts stay intact.
        method: HTTP verb.
        data: Optional request body.

    Returns:
        The decoded response.
    """
    req = urllib.request.Request(f"{base_url}{path}", method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as raw:
            return Response(raw.status, raw.read().decode("utf-8"), dict(raw.headers))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read().decode("utf-8"), dict(exc.headers))


@pytest.fixture
def make_harness(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    """Provide a factory starting servers that are shut down on teardown.

    Args:
        tmp_path: Pytest supplied temporary directory.

    Yields:
        Callable accepting an ``ok`` flag and returning a running harness.
    """
    servers: list[ThreadingHTTPServer] = []
    threads: list[threading.Thread] = []
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (web_dir / "app.js").write_text(APP_JS, encoding="utf-8")
    (web_dir / "styles.css").write_text(STYLES_CSS, encoding="utf-8")
    (tmp_path / "secret.txt").write_text(TOKEN, encoding="utf-8")

    counter = {"n": 0}

    def factory(ok: bool = True, crate_builder: Callable[..., dict] = fake_crate) -> Harness:
        counter["n"] += 1
        db_path = tmp_path / f"crate{counter['n']}.db"
        client = FakeClient(ok=ok)
        httpd = create_server(
            db_path,
            client,
            USERNAME,
            host="127.0.0.1",
            port=0,
            web_dir=web_dir,
            sellers=SELLERS,
            crate_builder=crate_builder,
        )
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append(httpd)
        threads.append(thread)
        host, port = httpd.server_address[0], httpd.server_address[1]
        return Harness(f"http://{host}:{port}", client, db_path, web_dir)

    try:
        yield factory
    finally:
        for httpd in servers:
            httpd.shutdown()
            httpd.server_close()
        for thread in threads:
            thread.join(timeout=5)


def wantlist_ids(db_path: Path) -> list[int]:
    """Reads every release id currently stored in the wantlist table.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        Sorted release ids.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT release_id FROM wantlist ORDER BY release_id").fetchall()
    finally:
        conn.close()
    return [int(row[0]) for row in rows]


@pytest.mark.parametrize(
    ("path", "expected_body", "expected_type"),
    [
        ("/", INDEX_HTML, "text/html; charset=utf-8"),
        ("/index.html", INDEX_HTML, "text/html; charset=utf-8"),
        ("/app.js", APP_JS, "text/javascript; charset=utf-8"),
        ("/styles.css", STYLES_CSS, "text/css; charset=utf-8"),
    ],
)
def test_static_assets_are_served(
    make_harness: Callable[..., Harness],
    path: str,
    expected_body: str,
    expected_type: str,
) -> None:
    """Static player files are served with the correct body and content type."""
    harness = make_harness()
    response = request(harness.base_url, path)
    assert response.status == 200
    assert response.body == expected_body
    assert response.headers["Content-Type"] == expected_type


@pytest.mark.parametrize(
    "path",
    [
        "/../../etc/passwd",
        "/..%2f..%2fetc%2fpasswd",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/../secret.txt",
        "/%2e%2e%2fsecret.txt",
        "/subdir/../../secret.txt",
    ],
)
def test_path_traversal_is_rejected(make_harness: Callable[..., Harness], path: str) -> None:
    """Requests escaping the web directory are refused with a JSON 403."""
    harness = make_harness()
    response = request(harness.base_url, path)
    assert response.status == 403
    assert response.json()["ok"] is False
    assert TOKEN not in response.body


@pytest.mark.parametrize("path", ["/missing.js", "/nested/missing.css"])
def test_missing_static_file_returns_json_404(
    make_harness: Callable[..., Harness], path: str
) -> None:
    """A missing asset returns a JSON 404 rather than an HTML traceback."""
    harness = make_harness()
    response = request(harness.base_url, path)
    assert response.status == 404
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    assert response.json()["ok"] is False


@pytest.mark.parametrize("release_ids", [[], [12345], [12345, 67890]])
def test_api_crate_returns_json(
    make_harness: Callable[..., Harness], release_ids: list[int]
) -> None:
    """The crate endpoint returns a live JSON payload built from the database."""
    harness = make_harness()
    conn = sqlite3.connect(harness.db_path)
    try:
        conn.executemany(
            "INSERT INTO releases (release_id, artist, title) VALUES (?, 'A', 'T')",
            [(release_id,) for release_id in release_ids],
        )
        conn.commit()
    finally:
        conn.close()

    response = request(harness.base_url, "/api/crate")
    assert response.status == 200
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    payload = response.json()
    assert payload["stats"]["releases"] == len(release_ids)
    assert [track["release_id"] for track in payload["tracks"]] == release_ids


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        (ImportError("no module named radio.export"), 503),
        (sqlite3.OperationalError("no such table"), 500),
        (ValueError("bad row"), 500),
    ],
)
def test_crate_failures_return_json_errors(
    make_harness: Callable[..., Harness], failure: Exception, expected_status: int
) -> None:
    """A broken crate build returns a JSON error instead of an HTML traceback."""

    def broken(conn: sqlite3.Connection) -> dict:
        """Raises the parametrized failure.

        Args:
            conn: Open SQLite connection, unused.

        Returns:
            Never returns.
        """
        raise failure

    harness = make_harness(crate_builder=broken)
    response = request(harness.base_url, "/api/crate")
    assert response.status == expected_status
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    assert response.json()["ok"] is False


@pytest.mark.parametrize("key", ["username", "sellers", "counts", "server"])
def test_api_status_reports_state(make_harness: Callable[..., Harness], key: str) -> None:
    """The status endpoint exposes account, sellers, and row counts."""
    harness = make_harness()
    payload = request(harness.base_url, "/api/status").json()
    assert key in payload
    assert payload["username"] == USERNAME
    assert payload["sellers"] == SELLERS
    assert payload["server"] is True
    assert payload["counts"]["wantlist"] == 0


@pytest.mark.parametrize("release_id", [1, 42, 4297213464])
def test_wantlist_add_then_remove(
    make_harness: Callable[..., Harness], release_id: int
) -> None:
    """A successful proxy write adds the row, and a delete removes it again."""
    harness = make_harness()

    added = request(harness.base_url, f"/api/wantlist/{release_id}", method="POST", data=b"")
    assert added.status == 200
    assert added.json() == {"ok": True, "release_id": release_id, "wanted": True}
    assert wantlist_ids(harness.db_path) == [release_id]
    assert harness.client.calls[-1] == ("PUT", f"/users/{USERNAME}/wants/{release_id}")

    removed = request(harness.base_url, f"/api/wantlist/{release_id}", method="DELETE")
    assert removed.status == 200
    assert removed.json() == {"ok": True, "release_id": release_id, "wanted": False}
    assert wantlist_ids(harness.db_path) == []
    assert harness.client.calls[-1] == ("DELETE", f"/users/{USERNAME}/wants/{release_id}")


@pytest.mark.parametrize("release_id", [7, 99])
def test_wantlist_add_is_idempotent(
    make_harness: Callable[..., Harness], release_id: int
) -> None:
    """Adding the same release twice keeps exactly one row."""
    harness = make_harness()
    for _ in range(2):
        response = request(
            harness.base_url, f"/api/wantlist/{release_id}", method="POST", data=b""
        )
        assert response.status == 200
    assert wantlist_ids(harness.db_path) == [release_id]


@pytest.mark.parametrize(
    "raw_id",
    ["abc", "-1", "0", "1.5", "1%3BDROP%20TABLE%20wantlist", "%2e%2e", "1%20OR%201", ""],
)
@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_invalid_release_id_returns_400(
    make_harness: Callable[..., Harness], raw_id: str, method: str
) -> None:
    """A release id that is not a positive integer is refused before any client call."""
    harness = make_harness()
    data = b"" if method == "POST" else None
    response = request(harness.base_url, f"/api/wantlist/{raw_id}", method=method, data=data)
    assert response.status == 400
    assert response.json()["ok"] is False
    assert harness.client.calls == []


@pytest.mark.parametrize(
    ("method", "expected_wanted"),
    [("POST", False), ("DELETE", True)],
)
def test_failing_client_returns_non_200(
    make_harness: Callable[..., Harness], method: str, expected_wanted: bool
) -> None:
    """A rejected Discogs write surfaces as ok false with a non-200 status and no row change."""
    harness = make_harness(ok=False)
    data = b"" if method == "POST" else None
    response = request(harness.base_url, "/api/wantlist/555", method=method, data=data)
    assert response.status != 200
    assert response.status == 502
    payload = response.json()
    assert payload["ok"] is False
    assert payload["release_id"] == 555
    assert payload["wanted"] is expected_wanted
    assert wantlist_ids(harness.db_path) == []


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/unknown", "GET"),
        ("/api/wantlist", "POST"),
        ("/api/wantlist/1/extra", "POST"),
        ("/api/wantlist/1/extra", "DELETE"),
    ],
)
def test_unknown_endpoints_return_json_404(
    make_harness: Callable[..., Harness], path: str, method: str
) -> None:
    """Unrouted API requests return a JSON 404."""
    harness = make_harness()
    data = b"" if method == "POST" else None
    response = request(harness.base_url, path, method=method, data=data)
    assert response.status == 404
    assert response.json()["ok"] is False


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/", "GET"),
        ("/api/crate", "GET"),
        ("/api/status", "GET"),
        ("/api/wantlist/321", "POST"),
        ("/api/wantlist/321", "DELETE"),
        ("/api/unknown", "GET"),
    ],
)
def test_token_never_leaves_the_process(
    make_harness: Callable[..., Harness], path: str, method: str
) -> None:
    """No response body or header ever contains the Discogs token."""
    harness = make_harness()
    data = b"" if method == "POST" else None
    response = request(harness.base_url, path, method=method, data=data)
    assert TOKEN not in response.body
    assert TOKEN not in json.dumps(response.headers)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "*"])
def test_public_bind_is_refused(tmp_path: Path, host: str) -> None:
    """Binding to a non loopback interface raises instead of exposing the server."""
    with pytest.raises(ValueError):
        create_server(tmp_path / "crate.db", FakeClient(), USERNAME, host=host, port=0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", 1),
        ("4297213464", 4297213464),
        ("007", 7),
        ("0", None),
        ("-3", None),
        ("3.0", None),
        ("abc", None),
        ("", None),
        ("%2e%2e", None),
        ("١٢٣", None),
    ],
)
def test_parse_release_id(raw: str, expected: int | None) -> None:
    """Release id parsing accepts only positive ASCII integers."""
    assert server_module._parse_release_id(raw) == expected
