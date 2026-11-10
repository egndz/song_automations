"""Shared pytest fixtures for the Seller Radio test suite."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from radio import db


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """Provide an initialized in-memory database.

    Yields:
        An open SQLite connection with the full schema applied.
    """
    connection = db.connect(":memory:")
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def tmp_db(tmp_path: Path) -> Iterator[Path]:
    """Provide a path to a temporary on disk database file.

    The file is not created up front, so tests can exercise the connect and init path
    themselves. Any write ahead log side files are removed on teardown.

    Args:
        tmp_path: Pytest supplied temporary directory.

    Yields:
        Path to a database file inside an isolated temporary directory.
    """
    path = tmp_path / "crate.db"
    try:
        yield path
    finally:
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            if candidate.exists():
                candidate.unlink()


@pytest.fixture
def tmp_db_conn(tmp_db: Path) -> Iterator[sqlite3.Connection]:
    """Provide an initialized database backed by a temporary file.

    Args:
        tmp_db: Temporary database path fixture.

    Yields:
        An open SQLite connection with the full schema applied.
    """
    connection = db.connect(tmp_db)
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()
