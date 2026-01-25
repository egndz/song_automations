"""Fuzzy matching system optimized for electronic music track matching."""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from rapidfuzz import fuzz


class VersionType(Enum):
    """Type of track version/remix."""

    ORIGINAL = "original"
    REMIX = "remix"
    EDIT = "edit"
    DUB = "dub"
    EXTENDED = "extended"
    RADIO = "radio"
    INSTRUMENTAL = "instrumental"
    ACAPELLA = "acapella"
    REMASTER = "remaster"
    LIVE = "live"
    OTHER = "other"


@dataclass
class ParsedTrack:
    """Parsed components of a track title.

    Args:
        base_title: The core track title without version info.
        version: The version/remix info (e.g., "Hardfloor Remix").
        version_type: Categorized version type.
        remixer: Name of the remixer if applicable.
        full_title: Complete original title.
        artist: Artist name.
    """

    base_title: str
    version: str
    version_type: VersionType
    remixer: str
    full_title: str
    artist: str

    @property
    def search_query(self) -> str:
        """Generate a search query for this track."""
        if self.version:
            return f"{self.artist} {self.base_title} {self.version}"
        return f"{self.artist} {self.base_title}"

    @property
    def fallback_query(self) -> str:
        """Generate a fallback search query without version info."""
        return f"{self.artist} {self.base_title}"


@dataclass
class MatchResult:
    """Result of matching a track.

    Args:
        track_id: ID of the matched track (platform-specific).
        track_uri: URI of the matched track (Spotify) or URL (SoundCloud).
        confidence: Match confidence score (0.0 to 1.0).
        artist_score: Artist name match score.
        title_score: Title match score.
        verified_bonus: Bonus for verified/official artist.
        popularity_score: Normalized popularity score.
        is_verified: Whether the artist is verified.
        matched_title: Title of the matched track.
        matched_artist: Artist of the matched track.
    """

    track_id: str | int
    track_uri: str
    confidence: float
    artist_score: float
    title_score: float
    verified_bonus: float
    popularity_score: float
    is_verified: bool
    matched_title: str
    matched_artist: str


class SearchableTrack(Protocol):
    """Protocol for tracks that can be matched against."""

    @property
    def id(self) -> str | int: ...

    @property
    def name(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def artist(self) -> str: ...


VERSION_PATTERNS = [
    (r"\(extended\s+mix\)", VersionType.EXTENDED),
    (r"\(extended\s+version\)", VersionType.EXTENDED),
    (r"\(extended\)", VersionType.EXTENDED),
    (r"\(original\s+mix\)", VersionType.ORIGINAL),
    (r"\(original\s+version\)", VersionType.ORIGINAL),
    (r"\(original\)", VersionType.ORIGINAL),
    (r"\(radio\s+edit\)", VersionType.RADIO),
    (r"\(radio\s+mix\)", VersionType.RADIO),
    (r"\(radio\s+version\)", VersionType.RADIO),
    (r"\(instrumental\s+mix\)", VersionType.INSTRUMENTAL),
    (r"\(instrumental\)", VersionType.INSTRUMENTAL),
    (r"\(acapella\)", VersionType.ACAPELLA),
    (r"\(a\s*cappella\)", VersionType.ACAPELLA),
    (r"\(remaster(?:ed)?\s*(?:\d{4})?\)", VersionType.REMASTER),
    (r"\(live(?:\s+[^)]+)?\)", VersionType.LIVE),
    (r"\(dub\)", VersionType.DUB),
    (r"\(rework\)", VersionType.EDIT),
    (r"\(bootleg\)", VersionType.EDIT),
    (r"\(vip\s+mix\)", VersionType.EDIT),
    (r"\(vip\)", VersionType.EDIT),
    (r"\(([^)]+)\s+remix\)", VersionType.REMIX),
    (r"\(([^)]+)\s+edit\)", VersionType.EDIT),
    (r"\(([^)]+)\s+dub\)", VersionType.DUB),
    (r"\(([^)]+)\s+mix\)", VersionType.REMIX),
]

FEATURING_PATTERNS = [
    r"\s+feat\.?\s+",
    r"\s+ft\.?\s+",
    r"\s+featuring\s+",
    r"\s+with\s+",
    r"\s+x\s+",
]

AMPERSAND_PATTERN = r"\s+&\s+"


def normalize_text(text: str) -> str:
    """Normalize text for comparison.

    Args:
        text: Input text to normalize.

    Returns:
        Normalized text.
    """
    text = text.lower().strip()

    text = re.sub(r"[''`]", "'", text)
    text = re.sub(r"[""„]", '"', text)

    text = re.sub(r"\s+", " ", text)

    text = re.sub(r"^the\s+", "", text)

    text = re.sub(r"\s*-\s*", " ", text)

    return text


def normalize_artist(artist: str) -> str:
    """Normalize artist name for comparison.

    Args:
        artist: Artist name to normalize.

    Returns:
        Normalized artist name.
    """
    artist = normalize_text(artist)

    for pattern in FEATURING_PATTERNS:
        parts = re.split(pattern, artist, flags=re.IGNORECASE)
        if len(parts) > 1:
            artist = parts[0].strip()
            break

    parts = re.split(AMPERSAND_PATTERN, artist)
    if len(parts) > 1:
        artist = parts[0].strip()

    if artist.endswith(")") and " (" in artist:
        idx = artist.rfind(" (")
        suffix = artist[idx + 2 : -1]
        if suffix.isdigit():
            artist = artist[:idx]

    return artist


def parse_track_title(title: str, artist: str) -> ParsedTrack:
    """Parse a track title into its components.

    Args:
        title: Track title to parse.
        artist: Artist name.

    Returns:
        ParsedTrack with parsed components.
    """
    original_title = title
    base_title = title
    version = ""
    version_type = VersionType.ORIGINAL
    remixer = ""

    for pattern, vtype in VERSION_PATTERNS:
        match = re.search(pattern, title, flags=re.IGNORECASE)
        if match:
            version_type = vtype
            start, end = match.span()
            version = title[start:end].strip("()")

            if match.lastindex and match.lastindex >= 1:
                remixer_match = re.search(pattern, title, flags=re.IGNORECASE)
                if remixer_match and remixer_match.lastindex:
                    remixer = remixer_match.group(1).strip()

            base_title = (title[:start] + title[end:]).strip()
            break

    if not version:
        paren_match = re.search(r"\(([^)]+)\)", title)
        if paren_match:
            version = paren_match.group(1)
            version_type = VersionType.OTHER
            base_title = title[: paren_match.start()].strip()

    return ParsedTrack(
        base_title=base_title,
        version=version,
        version_type=version_type,
        remixer=remixer,
        full_title=original_title,
        artist=artist,
    )


def calculate_artist_score(source_artist: str, candidate_artist: str) -> float:
    """Calculate fuzzy match score between artist names.

    Args:
        source_artist: Source artist name (from Discogs).
        candidate_artist: Candidate artist name (from Spotify/SoundCloud).

    Returns:
        Match score from 0.0 to 1.0.
    """
    source_norm = normalize_artist(source_artist)
    candidate_norm = normalize_artist(candidate_artist)

    ratio = fuzz.ratio(source_norm, candidate_norm) / 100.0

    token_ratio = fuzz.token_sort_ratio(source_norm, candidate_norm) / 100.0

    return max(ratio, token_ratio)


def calculate_title_score(source_title: str, candidate_title: str) -> float:
    """Calculate fuzzy match score between track titles.

    Args:
        source_title: Source track title (from Discogs).
        candidate_title: Candidate track title (from Spotify/SoundCloud).

    Returns:
        Match score from 0.0 to 1.0.
    """
    source_norm = normalize_text(source_title)
    candidate_norm = normalize_text(candidate_title)

    ratio = fuzz.ratio(source_norm, candidate_norm) / 100.0

    token_ratio = fuzz.token_sort_ratio(source_norm, candidate_norm) / 100.0

    partial_ratio = fuzz.partial_ratio(source_norm, candidate_norm) / 100.0

    return max(ratio, token_ratio * 0.95, partial_ratio * 0.9)


def normalize_popularity(value: int, max_value: int = 100) -> float:
    """Normalize a popularity value to 0.0-1.0 range.

    Args:
        value: Raw popularity value.
        max_value: Maximum expected value.

    Returns:
        Normalized value from 0.0 to 1.0.
    """
    if max_value <= 0:
        return 0.0
    return min(1.0, max(0.0, value / max_value))


def calculate_version_bonus(parsed_track: ParsedTrack, candidate_title: str) -> float:
    """Calculate bonus for matching version/remix info.

    Args:
        parsed_track: Parsed source track.
        candidate_title: Candidate track title.

    Returns:
        1.0 if version info matches, 0.0 otherwise.
    """
    if not parsed_track.version:
        return 0.0

    candidate_lower = candidate_title.lower()
    version_lower = parsed_track.version.lower()

    if version_lower in candidate_lower:
        return 1.0

    if parsed_track.remixer:
        remixer_lower = parsed_track.remixer.lower()
        if remixer_lower in candidate_lower:
            return 1.0

    return 0.0


def score_candidate(
    parsed_track: ParsedTrack,
    candidate_title: str,
    candidate_artist: str,
    is_verified: bool,
    popularity: int,
    max_popularity: int = 100,
    artist_weight: float = 0.45,
    title_weight: float = 0.35,
    verified_weight: float = 0.10,
    popularity_weight: float = 0.10,
    version_bonus_weight: float = 0.0,
) -> tuple[float, float, float, float, float]:
    """Score a candidate track against a source track.

    Args:
        parsed_track: Parsed source track.
        candidate_title: Candidate track title.
        candidate_artist: Candidate artist name.
        is_verified: Whether the candidate artist is verified.
        popularity: Candidate track popularity.
        max_popularity: Maximum popularity for normalization.
        artist_weight: Weight for artist score component.
        title_weight: Weight for title score component.
        verified_weight: Weight for verified bonus.
        popularity_weight: Weight for popularity score.
        version_bonus_weight: Weight for version/remix matching bonus.

    Returns:
        Tuple of (total_score, artist_score, title_score, verified_bonus, popularity_score).
    """
    artist_score = calculate_artist_score(parsed_track.artist, candidate_artist)

    title_score = calculate_title_score(parsed_track.full_title, candidate_title)

    verified_bonus = 1.0 if is_verified else 0.0

    popularity_score = normalize_popularity(popularity, max_popularity)

    version_bonus = calculate_version_bonus(parsed_track, candidate_title)

    total_score = (
        artist_score * artist_weight
        + title_score * title_weight
        + verified_bonus * verified_weight
        + popularity_score * popularity_weight
        + version_bonus * version_bonus_weight
    )

    return total_score, artist_score, title_score, verified_bonus, popularity_score


def should_use_fallback(parsed_track: ParsedTrack) -> bool:
    """Determine if fallback search (without version) should be used.

    Args:
        parsed_track: Parsed track to check.

    Returns:
        True if fallback search is acceptable (not a remix).
    """
    return parsed_track.version_type not in (
        VersionType.REMIX,
        VersionType.DUB,
        VersionType.EDIT,
    )
