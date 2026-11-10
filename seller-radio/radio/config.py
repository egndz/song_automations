"""Static configuration, credential lookup, and shared vocabularies for Seller Radio."""

from __future__ import annotations

import os
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parent.parent
DB_PATH: Path = ROOT / "crate.db"
WEB_DIR: Path = ROOT / "web"
CRATE_JS_PATH: Path = WEB_DIR / "crate.js"

USER_AGENT: str = "SellerRadio/0.1 +https://github.com/egndz"
BASE_URL: str = "https://api.discogs.com"

MAX_SEARCH_ITEMS: int = 10000
PER_PAGE: int = 100
RATELIMIT_FLOOR: int = 3
DEFAULT_CURRENCY: str = "EUR"

SELLERS: list[str] = ["allcitydublin"]

SEARCH_BANKS: list[dict] = [
    {
        "type": "release",
        "format": "Vinyl",
        "genre": "Electronic",
        "style": "Deep House",
        "year": "1993-1999",
    },
    {
        "type": "release",
        "format": "Vinyl",
        "genre": "Electronic",
        "style": "Techno",
        "country": "Germany",
        "year": "1992-1997",
    },
    {
        "type": "release",
        "format": "Vinyl",
        "genre": "Funk / Soul",
        "style": "Disco",
        "year": "1977-1982",
    },
]

GRADE_ORDER: list[str] = ["P", "F", "G", "G+", "VG", "VG+", "NM", "M"]

CONDITION_TO_GRADE: dict[str, str | None] = {
    "Mint (M)": "M",
    "Near Mint (NM or M-)": "NM",
    "Very Good Plus (VG+)": "VG+",
    "Very Good (VG)": "VG",
    "Good Plus (G+)": "G+",
    "Good (G)": "G",
    "Fair (F)": "F",
    "Poor (P)": "P",
    "Generic": None,
    "No Cover": None,
    "Not Graded": None,
}

_TOKEN_ENV_KEYS: tuple[str, ...] = ("DISCOGS_TOKEN", "DISCOGS_USER_TOKEN")
_DEFAULT_USERNAME: str = "egunduz"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv style file into a plain mapping.

    Args:
        path: Path to the dotenv file. A missing file yields an empty mapping.

    Returns:
        Mapping of environment keys to unquoted values.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def get_token() -> str:
    """Resolve the Discogs personal access token.

    Looks at DISCOGS_TOKEN and DISCOGS_USER_TOKEN in the environment, then falls back to the
    parent repository dotenv file next to the project root.

    Returns:
        The Discogs personal access token.

    Raises:
        RuntimeError: If no token is available from the environment or the dotenv file.
    """
    for key in _TOKEN_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value

    env_file = ROOT.parent / ".env"
    file_values = _parse_env_file(env_file)
    for key in _TOKEN_ENV_KEYS:
        value = file_values.get(key, "").strip()
        if value:
            return value

    raise RuntimeError(
        "No Discogs token found. Set DISCOGS_TOKEN in your environment, or add "
        f"DISCOGS_USER_TOKEN=<token> to {env_file}. Generate one at "
        "https://www.discogs.com/settings/developers"
    )


def get_username() -> str:
    """Resolve the Discogs username whose collection and wantlist are synced.

    Returns:
        The username from DISCOGS_USERNAME, or the default account name.
    """
    return os.environ.get("DISCOGS_USERNAME", "").strip() or _DEFAULT_USERNAME


def bank_key(bank: dict) -> str:
    """Build a stable identifier for a search bank.

    Keys are sorted so that two dicts with the same contents in a different insertion order
    always produce the same string.

    Args:
        bank: Search bank parameters such as genre, style, country, and year.

    Returns:
        A deterministic key of the form "genre=Electronic|style=Techno|year=1992-1997".
    """
    parts = [f"{key}={bank[key]}" for key in sorted(bank) if bank[key] is not None]
    return "|".join(parts)
