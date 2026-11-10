"""Fixtures shared by the snapshot tests."""

from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def root(tmp_path) -> Path:
    """Provides the folder that holds one folder per run.

    Args:
        tmp_path: Temporary folder.

    Returns:
        A folder that does not exist yet.
    """
    return tmp_path / "snapshots"


@pytest.fixture
def blocked_root(tmp_path) -> Path:
    """Provides a snapshot folder nothing can be written to.

    Args:
        tmp_path: Temporary folder.

    Returns:
        A path that is a file, so no run folder can be created inside it.
    """
    path = tmp_path / "blocked"
    path.write_text("not a folder")
    return path


@pytest.fixture
def flat() -> Callable[[str], str]:
    """Provides a function that joins wrapped console output into one line.

    Returns:
        A function turning every run of whitespace into one space.
    """
    return lambda text: " ".join(text.split())
