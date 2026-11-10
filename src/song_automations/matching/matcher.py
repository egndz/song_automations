"""Gate-based track matching.

A candidate is accepted only when the artist and the base title both agree with
the source track. There is no combined score, so a strong artist match can never
make up for a wrong title. Accepted candidates are then ranked by how closely the
version agrees: the exact mix first, then the original, then any other mix.
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum

from rapidfuzz import fuzz

ARTIST_RATIO = 90
TITLE_RATIO = 92
VERSION_RATIO = 85
EXACT_LENGTH_BELOW = 5
DURATION_TOLERANCE_MS = 20_000
PREVIEW_MAX_MS = 60_000
PREVIEW_CLIP_MS = 30_000
ISRC_KIND = "isrc"
PLAYABLE = "playable"
PREVIEW_SOURCE_FRACTION = 0.5
LONG_MIX_MIN_MS = 1_500_000
LONG_MIX_SOURCE_FACTOR = 2.5
MAX_LOOSE_EXTRA_WORDS = 2

VERSION_WORDS = {
    "remix",
    "rmx",
    "mix",
    "edit",
    "reedit",
    "dub",
    "version",
    "rework",
    "refix",
    "reprise",
    "vip",
    "flip",
    "instrumental",
    "extended",
    "radio",
    "club",
    "acapella",
    "vocal",
    "dubmix",
    "remake",
    "remodel",
    "interpretation",
}
GENERIC_VERSION_WORDS = {"remix", "rmx", "mix", "version", "edit", "reedit"}
NEUTRAL_VERSION = re.compile(
    r"^(original( mix| version)?|album( version| mix)?|single( version)?|lp( version| mix)?|"
    r"stereo|mono|12( inch)?( version| mix)?|12 mix|vinyl( rip| version)?|"
    r"(digital(ly)? )?remaster(ed)?( \d{4})?|\d{4} (digital )?remaster(ed)?( version)?|"
    r"main( mix| version)?)$"
)
REJECT_MARKERS = re.compile(
    r"\b(cover|covered by|karaoke|tribute|in the style of|originally performed|made famous|"
    r"nightcore|sped up|speed up|slowed|8d audio|type beat|reaction|tutorial|"
    r"preview|snippet|teaser|clip|sample pack|mashup|mash up|bootleg|vs|"
    r"acapella|a cappella|instrumental|piano version|acoustic|"
    r"jools holland|kexp|tiny desk|peel session|bbc session|maida vale|live lounge|audiotree|"
    r"colors show|live at|live from|live on|in session|unplugged|ao vivo|en vivo|en directo|en concierto)\b"
)
SOFT_MARKERS = re.compile(
    r"\b(dj set|live set|mixed by|mixtape|podcast|radio show|episode|boiler room|essential mix|"
    r"full album|full ep|album mix|continuous mix|dj mix|live)\b"
)
SOFT_MARKER_TOLERANCE_MS = 5_000
COVER_ACCOUNT = re.compile(r"tribute|cover|karaoke|choir|orchestra|ensemble")
PART_PATTERN = re.compile(r"\b(?:part|pt)\.?\s*(\d+|[ivx]{1,4})\b", re.IGNORECASE)
BRACKET_PATTERN = re.compile(r"[(\[{]([^)\]}]*)[)\]}]")
FEATURING_PATTERN = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|with)\s+.*$", re.IGNORECASE)
ARTIST_SEPARATOR = re.compile(
    r"\s*(?:,|&|/|\+|;|\s+x\s+|\bfeat\.?|\bft\.?|\bfeaturing\b|\band\b|\bvs\.?\b|\bwith\b|\bpresents\b|\bpres\.?\b)\s*",
    re.IGNORECASE,
)
DISAMBIGUATION = re.compile(r"\s*\(\d+\)")
DASH_SPLIT = re.compile(r"\s+[-‐-―|~]+\s+|\s*[–—]\s*")
LEADING_NOISE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*|\([^)]*\)\s*|premiere\s*[:|\-]?\s*|exclusive\s*[:|\-]?\s*|"
    r"free download\s*[:|\-]?\s*|[a-h]\d{0,2}\s*[.)|:\-]\s*|[a-h]\d{1,2}\s+|\d{1,2}\s*[.)|:\-]\s+)+",
    re.IGNORECASE,
)
QUOTED_TITLE = re.compile(r"""^(.+?)\s+["'‘“](.+)["'’”]\s*$""")
BY_PATTERN = re.compile(r"^(.+?)\s+by\s+(.+)$", re.IGNORECASE)
COLON_PATTERN = re.compile(r"^([^:]{2,}):\s+(.+)$")


class Tier(IntEnum):
    """How closely a candidate agrees with the source version. Higher is better."""

    OTHER_MIX = 1
    ORIGINAL = 2
    EXACT = 3


@dataclass(frozen=True)
class SourceTrack:
    """A Discogs track prepared for matching.

    Attributes:
        artists: Credited artist names plus any known aliases.
        title: Raw track title.
        duration_ms: Track length if Discogs knows it.
        release_title: Title of the release the track is on.
        labels: Label names of the release.
        remixers: Remixers credited on the track but missing from the title.
        fan_uploads: Whether uploads by strangers may match. Off when Spotify
            already carries the track, since fan uploads of released music
            turned out to be covers or tributes about as often as the record.
        isrcs: Recording codes of this exact version with the length the
            catalogue gives for each, taken from a catalogue that lists it. A
            candidate carrying one of them is the same recording, whoever
            uploaded it.
    """

    artists: tuple[str, ...]
    title: str
    duration_ms: int | None = None
    release_title: str = ""
    labels: tuple[str, ...] = ()
    remixers: tuple[str, ...] = ()
    fan_uploads: bool = True
    isrcs: Mapping[str, int | None] = field(default_factory=dict)


@dataclass(frozen=True)
class Reading:
    """One plausible interpretation of a candidate's artist and title.

    Attributes:
        artists: Artist names claimed by this reading.
        title: Title claimed by this reading.
        kind: Which rule produced the reading.
    """

    artists: tuple[str, ...]
    title: str
    kind: str = "metadata"


@dataclass(frozen=True)
class Candidate:
    """A track offered by a streaming platform.

    Attributes:
        id: Platform track id.
        raw_title: Title exactly as the platform shows it.
        readings: Interpretations to test against the source.
        duration_ms: Track length if known.
        album: Album name if the platform has one.
        uploader: Uploading account, SoundCloud only.
        plays: Play count used only as the last tiebreak.
        isrc: Recording code the platform lists, empty when it lists none.
        access: SoundCloud access level: playable, preview or blocked.
    """

    id: str
    raw_title: str
    readings: tuple[Reading, ...]
    duration_ms: int | None = None
    album: str = ""
    uploader: str = ""
    plays: int = 0
    isrc: str = ""
    access: str = PLAYABLE


@dataclass(frozen=True)
class Match:
    """An accepted candidate with the evidence for accepting it.

    Attributes:
        candidate: The accepted candidate.
        tier: How closely the version agrees.
        reading: The reading that passed the gates.
        on_same_release: True when the candidate's album is the source release.
        duration_delta_ms: Absolute length difference, None when unknown.
        confidence: Value between 0 and 1 for reports and ordering.
    """

    candidate: Candidate
    tier: Tier
    reading: Reading
    on_same_release: bool = False
    duration_delta_ms: int | None = None
    confidence: float = 0.0


@dataclass
class Rejection:
    """Why a candidate was refused, kept for the evaluation report.

    Attributes:
        candidate: The refused candidate.
        reason: Short machine readable reason.
    """

    candidate: Candidate
    reason: str


@dataclass
class Decision:
    """The outcome of matching one source track.

    Attributes:
        match: The best match, or None.
        alternatives: Other accepted candidates, best first.
        rejections: Candidates that failed a gate.
    """

    match: Match | None = None
    alternatives: list[Match] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)


def normalize_isrc(text: str | None) -> str:
    """Brings a recording code to one spelling.

    Args:
        text: Code as a platform writes it, with or without dashes.

    Returns:
        Upper case code without separators, empty when there is none.
    """
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def clean(text: str) -> str:
    """Normalizes text for comparison.

    Args:
        text: Raw text.

    Returns:
        Lowercase text without accents or punctuation.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    lowered = stripped.lower().replace("&", " and ").replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^\w ]", " ", lowered).replace("_", " ").split())


def _digits(text: str) -> list[str]:
    """Extracts the numbers from text.

    Args:
        text: Normalized text.

    Returns:
        Numbers in order of appearance.
    """
    return re.findall(r"\d+", text)


def _same(left: str, right: str, threshold: int) -> bool:
    """Compares two normalized strings, requiring numbers to agree.

    Args:
        left: First string.
        right: Second string.
        threshold: Minimum fuzzy ratio.

    Returns:
        True when the strings are the same for matching purposes.
    """
    if not left or not right:
        return False
    if left == right or left.replace(" ", "") == right.replace(" ", ""):
        return True
    if _digits(left) != _digits(right):
        return False
    if min(len(left), len(right)) < EXACT_LENGTH_BELOW:
        return False
    return fuzz.ratio(left, right) >= threshold


def split_artists(credit: str) -> tuple[str, ...]:
    """Splits an artist credit into the whole credit and its individual names.

    Args:
        credit: Artist credit such as "Earth, Wind & Fire".

    Returns:
        Normalized names, keeping the whole credit so band names with
        separators still match.
    """
    bare = DISAMBIGUATION.sub("", credit or "")
    names = [clean(bare)]
    names += [clean(part) for part in ARTIST_SEPARATOR.split(bare)]
    unique: dict[str, None] = {}
    for name in names:
        trimmed = re.sub(r"^(the|dj)\s+", "", name) if len(name.split()) > 1 else name
        if name:
            unique[name] = None
        if trimmed:
            unique[trimmed] = None
    return tuple(unique)


def artists_agree(source: Iterable[str], candidate: Iterable[str]) -> bool:
    """Checks whether any source artist is one of the candidate artists.

    Args:
        source: Source credits.
        candidate: Candidate credits.

    Returns:
        True when at least one name agrees.
    """
    source_names = [name for credit in source for name in split_artists(credit)]
    candidate_names = [name for credit in candidate for name in split_artists(credit)]
    return any(_same(a, b, ARTIST_RATIO) for a in source_names for b in candidate_names)


def _is_version_text(text: str) -> bool:
    """Decides whether an annotation describes a version of the track.

    Args:
        text: Normalized annotation.

    Returns:
        True when the annotation names a mix or edit.
    """
    words = set(text.split())
    return bool(words & VERSION_WORDS) or text.replace(" ", "") in {"reedit", "dubmix"}


def parse_title(title: str) -> tuple[list[str], list[str]]:
    """Splits a title into its possible base titles and its version annotations.

    Args:
        title: Raw title.

    Returns:
        Normalized base title variants, most likely first, and non-neutral
        version annotations.
    """
    raw = title or ""
    part = PART_PATTERN.search(raw)
    part_suffix = f" part {part[1].lower()}" if part else ""
    without_part = PART_PATTERN.sub(" ", raw)
    annotations = BRACKET_PATTERN.findall(without_part)
    body = BRACKET_PATTERN.sub(" ", without_part)
    body = FEATURING_PATTERN.sub("", body)
    segments = [segment for segment in DASH_SPLIT.split(body) if segment.strip()]
    head = segments[0] if segments else body
    trailing = segments[1:]
    versions = [clean(a) for a in annotations]
    bases = [head]
    for segment in trailing:
        cleaned = clean(segment)
        if _is_version_text(cleaned) or NEUTRAL_VERSION.match(cleaned):
            versions.append(cleaned)
        else:
            bases.append(f"{head} {segment}")
    if len(bases) > 1:
        bases.insert(1, body)
    variants: dict[str, None] = {}
    for base in bases:
        for alternative in re.split(r"\s+=\s+", base):
            normalized = clean(alternative)
            if normalized:
                variants[f"{normalized}{part_suffix}"] = None
    kept = [v for v in versions if v and _is_version_text(v) and not NEUTRAL_VERSION.match(v)]
    return list(variants), kept


def _distinctive(version: str) -> str:
    """Removes generic mix words so that remixer names can be compared.

    Args:
        version: Normalized version annotation.

    Returns:
        The distinctive words of the annotation.
    """
    return " ".join(word for word in version.split() if word not in GENERIC_VERSION_WORDS)


def versions_agree(source: Sequence[str], candidate: Sequence[str]) -> bool:
    """Checks whether two lists of version annotations describe the same mix.

    Args:
        source: Source annotations.
        candidate: Candidate annotations.

    Returns:
        True when any pair agrees.
    """
    for left in source:
        for right in candidate:
            left_word, right_word = _distinctive(left), _distinctive(right)
            if left_word and right_word:
                if fuzz.token_set_ratio(left_word, right_word) >= VERSION_RATIO:
                    return True
            elif left == right or (not left_word and not right_word):
                return True
    return False


def classify_tier(source: Sequence[str], candidate: Sequence[str]) -> Tier:
    """Places a candidate on the version ladder.

    Args:
        source: Source version annotations.
        candidate: Candidate version annotations.

    Returns:
        The tier of the candidate.
    """
    if not source:
        return Tier.EXACT if not candidate else Tier.OTHER_MIX
    if not candidate:
        return Tier.ORIGINAL
    return Tier.EXACT if versions_agree(source, candidate) else Tier.OTHER_MIX


def _rejected_marker(source: SourceTrack, candidate: Candidate) -> str | None:
    """Finds a disqualifying word the source itself does not carry.

    Artist and title words are ignored, so the act Chain Reaction is not taken
    for a reaction video. Words that usually mean a recording of a set or a gig
    are forgiven when the length is within seconds of the source, because then
    the upload is the record itself on a channel with that word in its title.

    Args:
        source: Source track.
        candidate: Candidate track.

    Returns:
        The disqualifying word, or None.
    """
    source_clean = clean(source.title)
    text = f" {clean(candidate.raw_title)} "
    for credit in source.artists:
        text = text.replace(f" {clean(DISAMBIGUATION.sub('', credit))} ", " ")
    for found in REJECT_MARKERS.finditer(text):
        if found[0] not in source_clean:
            return found[0]
    close = (
        source.duration_ms
        and candidate.duration_ms
        and abs(source.duration_ms - candidate.duration_ms) <= SOFT_MARKER_TOLERANCE_MS
    )
    if not close:
        for found in SOFT_MARKERS.finditer(text):
            if found[0] not in source_clean:
                return found[0]
    return None


def _duration_rejection(
    source_ms: int | None, candidate_ms: int | None, tier: Tier
) -> str | None:
    """Rules out previews, long mixes and, for the same mix, a different length.

    Args:
        source_ms: Source length, if known.
        candidate_ms: Candidate length, if known.
        tier: Tier of the candidate.

    Returns:
        A rejection reason, or None.
    """
    if not candidate_ms:
        return None
    if not source_ms:
        return "too_short" if candidate_ms < PREVIEW_MAX_MS else (
            "too_long" if candidate_ms > LONG_MIX_MIN_MS else None
        )
    if candidate_ms < source_ms * PREVIEW_SOURCE_FRACTION:
        return "too_short"
    if candidate_ms > source_ms * LONG_MIX_SOURCE_FACTOR and candidate_ms > LONG_MIX_MIN_MS:
        return "too_long"
    if tier == Tier.EXACT and abs(source_ms - candidate_ms) > DURATION_TOLERANCE_MS:
        return "duration_mismatch"
    return None


def _confidence(tier: Tier, reading: Reading, delta: int | None, same_release: bool) -> float:
    """Summarizes the evidence as a number for reports.

    Args:
        tier: Version tier.
        reading: Reading that passed.
        delta: Duration difference, if known.
        same_release: Whether the album is the source release.

    Returns:
        Value between 0 and 1.
    """
    if reading.kind == ISRC_KIND:
        return 1.0
    base = {Tier.EXACT: 0.85, Tier.ORIGINAL: 0.7, Tier.OTHER_MIX: 0.55}[tier]
    if reading.kind in {"label", "loose"}:
        base -= 0.1
    if delta is not None and delta <= DURATION_TOLERANCE_MS:
        base += 0.1
    if same_release:
        base += 0.05
    return round(min(base, 1.0), 2)


def _account_is(names: Iterable[str], account: str) -> bool:
    """Checks whether an account is one of the names, comparing the whole name.

    Account names are not split like artist credits and must match exactly,
    apart from spaces and punctuation, so neither a tribute act called
    "Pet Shop Boys, actually" nor one called "FOOT Fighters" passes.

    Args:
        names: Artist, label or remixer names.
        account: Uploading account name.

    Returns:
        True when the account is one of the names.
    """
    target = clean(account).replace(" ", "")
    return bool(target) and any(
        clean(DISAMBIGUATION.sub("", name)).replace(" ", "") == target for name in names
    )


def _fan_upload_unverified(source: SourceTrack, candidate: Candidate) -> bool:
    """Checks whether an upload by a stranger lacks the evidence that would confirm it.

    Uploads by the artist, the label or a remixer Discogs credits are trusted.
    Anyone else's upload, including a self-made remix or edit, counts only for music Spotify does not carry
    (vinyl rips of rare records), and only with a length that matches Discogs.

    Args:
        source: Source track.
        candidate: Candidate track.

    Returns:
        True when the candidate is an unconfirmed fan upload.
    """
    if not candidate.uploader:
        return False
    if _account_is((*source.artists, *source.remixers, *source.labels), candidate.uploader):
        return False
    name = clean(candidate.uploader)
    if not source.fan_uploads or COVER_ACCOUNT.search(name.replace(" ", "")):
        return True
    if source.duration_ms and candidate.duration_ms:
        return abs(source.duration_ms - candidate.duration_ms) > DURATION_TOLERANCE_MS
    return True


def _is_cut(length: int | None, *full_lengths: int | None) -> bool:
    """Checks whether an upload is too far from the length of the recording.

    Labels post clips of a release under the code of the full track, so the
    code alone does not show that the whole recording is there.

    Args:
        length: Length of the upload, None when unknown.
        full_lengths: Lengths Discogs and the catalogue give, None when unknown.

    Returns:
        True when a known length differs from the upload by more than the tolerance.
    """
    if not length:
        return False
    return any(full and abs(full - length) > DURATION_TOLERANCE_MS for full in full_lengths)


def _same_recording(source: SourceTrack, candidate: Candidate) -> Match:
    """Accepts a candidate that carries a recording code of the source.

    The code names one recording, so no reading of the title or the account
    is needed, and a cover or a live take never shares it.

    Args:
        source: Source track.
        candidate: Candidate with a matching code.

    Returns:
        An exact match.
    """
    known = candidate.readings[0] if candidate.readings else Reading((), candidate.raw_title)
    reading = Reading(known.artists, known.title, ISRC_KIND)
    delta = (
        abs(source.duration_ms - candidate.duration_ms)
        if source.duration_ms and candidate.duration_ms
        else None
    )
    return Match(
        candidate=candidate,
        tier=Tier.EXACT,
        reading=reading,
        duration_delta_ms=delta,
        confidence=_confidence(Tier.EXACT, reading, delta, False),
    )


def evaluate(source: SourceTrack, candidate: Candidate) -> Match | Rejection:
    """Applies the gates to one candidate.

    Args:
        source: Source track.
        candidate: Candidate track.

    Returns:
        A Match when every gate passes, otherwise a Rejection naming the first
        failed gate.
    """
    if candidate.isrc and candidate.isrc in source.isrcs:
        if _is_cut(candidate.duration_ms, source.duration_ms, source.isrcs[candidate.isrc]):
            return Rejection(candidate, "isrc_length")
        return _same_recording(source, candidate)
    marker = _rejected_marker(source, candidate)
    if marker:
        return Rejection(candidate, f"marker:{marker}")
    bases, source_versions = parse_title(source.title)
    if not source_versions and source.remixers:
        source_versions = [f"{clean(name)} remix" for name in source.remixers]
    best: tuple[Tier, Reading] | None = None
    reason = "title"
    for reading in candidate.readings:
        reading_bases, reading_versions = parse_title(reading.title)
        if not any(_same(a, b, TITLE_RATIO) for a in bases for b in reading_bases):
            continue
        reason = "artist"
        if reading.kind == "label":
            if not _account_is(source.labels, candidate.uploader):
                continue
        elif reading.kind == "uploader":
            if not _account_is(source.artists, candidate.uploader):
                continue
        elif not artists_agree(source.artists, reading.artists):
            continue
        tier = classify_tier(source_versions, reading_versions)
        if best is None or tier > best[0]:
            best = (tier, reading)
    if best is None:
        return Rejection(candidate, reason)
    tier, reading = best
    duration_reason = _duration_rejection(source.duration_ms, candidate.duration_ms, tier)
    if duration_reason == "duration_mismatch":
        tier = Tier.OTHER_MIX
    elif duration_reason:
        return Rejection(candidate, duration_reason)
    if _fan_upload_unverified(source, candidate):
        return Rejection(candidate, "fan_unverified")
    delta = (
        abs(source.duration_ms - candidate.duration_ms)
        if source.duration_ms and candidate.duration_ms
        else None
    )
    same_release = bool(
        candidate.album
        and source.release_title
        and _same(clean(candidate.album), clean(source.release_title), TITLE_RATIO)
    )
    return Match(
        candidate=candidate,
        tier=tier,
        reading=reading,
        on_same_release=same_release,
        duration_delta_ms=delta,
        confidence=_confidence(tier, reading, delta, same_release),
    )


def _rank(match: Match) -> tuple:
    """Builds the sort key for accepted candidates.

    Args:
        match: Accepted candidate.

    Returns:
        Tuple that sorts the best candidate first, ending in the id so the
        order never depends on the order of search results.
    """
    delta = match.duration_delta_ms if match.duration_delta_ms is not None else 10**9
    return (
        -int(match.tier),
        match.reading.kind != ISRC_KIND,
        match.candidate.access != PLAYABLE,
        not match.on_same_release,
        match.reading.kind in {"label", "loose"},
        delta // 5000,
        -match.candidate.plays,
        match.candidate.id,
    )


def decide(source: SourceTrack, candidates: Iterable[Candidate]) -> Decision:
    """Chooses the best candidate for a source track.

    Args:
        source: Source track.
        candidates: Candidates from any number of searches.

    Returns:
        The decision with the chosen match, alternatives and rejections.
    """
    unique = {candidate.id: candidate for candidate in candidates}
    accepted: list[Match] = []
    decision = Decision()
    for candidate in unique.values():
        outcome = evaluate(source, candidate)
        if isinstance(outcome, Match):
            accepted.append(outcome)
        else:
            decision.rejections.append(outcome)
    accepted.sort(key=_rank)
    if accepted:
        decision.match = accepted[0]
        decision.alternatives = accepted[1:]
    return decision


def assign_release(decisions: Sequence[Decision]) -> list[Match | None]:
    """Gives each track of a release its own candidate where possible.

    The best evidence chooses first, so an exact mix keeps its upload and a
    track that only reached it through fallback moves to its next alternative.

    Args:
        decisions: Decisions for the tracks of one release, in track order.

    Returns:
        One entry per track. None means no candidate remains for that track.
    """
    order = sorted(
        range(len(decisions)),
        key=lambda i: _rank(decisions[i].match) if decisions[i].match else (99,),
    )
    taken: set[str] = set()
    assigned: list[Match | None] = [None] * len(decisions)
    for index in order:
        decision = decisions[index]
        options = ([decision.match] if decision.match else []) + decision.alternatives
        for option in options:
            if option.candidate.id not in taken:
                assigned[index] = option
                taken.add(option.candidate.id)
                break
    return assigned


def spotify_candidate(item: dict, album_name: str | None = None) -> Candidate:
    """Builds a candidate from a Spotify track object.

    Args:
        item: Spotify track or simplified album track.
        album_name: Album name when the item came from an album listing.

    Returns:
        The candidate.
    """
    artists = tuple(a.get("name", "") for a in item.get("artists") or [])
    name = item.get("name") or ""
    album = album_name if album_name is not None else (item.get("album") or {}).get("name", "")
    return Candidate(
        id=str(item.get("id")),
        raw_title=name,
        readings=(Reading(artists, name),),
        duration_ms=item.get("duration_ms"),
        album=album or "",
        isrc=normalize_isrc((item.get("external_ids") or {}).get("isrc")),
    )


def soundcloud_readings(title: str, uploader: str, metadata_artist: str = "") -> tuple[Reading, ...]:
    """Lists every plausible artist and title reading of a SoundCloud upload.

    Args:
        title: Upload title.
        uploader: Account name.
        metadata_artist: The artist field the uploader filled in, if any.

    Returns:
        Readings to test against the source.
    """
    bare = LEADING_NOISE.sub("", title or "").strip() or (title or "")
    readings: list[Reading] = []
    if metadata_artist:
        readings.append(Reading((metadata_artist,), bare, "metadata"))
    pieces = [piece.strip() for piece in DASH_SPLIT.split(bare) if piece.strip()]
    if len(pieces) >= 2:
        readings.append(Reading((pieces[0],), " - ".join(pieces[1:]), "dash"))
        readings.append(Reading((pieces[-1],), " - ".join(pieces[:-1]), "dash_reversed"))
        readings.extend(
            Reading((pieces[0],), LEADING_NOISE.sub("", piece), "dash") for piece in pieces[2:]
        )
        readings.append(Reading((pieces[0],), LEADING_NOISE.sub("", pieces[1]), "dash"))
    for text in dict.fromkeys([bare, *pieces[:1]]):
        for pattern, kind in ((QUOTED_TITLE, "quoted"), (COLON_PATTERN, "colon")):
            found = pattern.match(text)
            if found:
                readings.append(Reading((found[1],), found[2], kind))
    by_match = BY_PATTERN.match(bare)
    if by_match:
        readings.append(Reading((by_match[2],), by_match[1], "by"))
    readings.append(Reading((uploader,), bare, "uploader"))
    readings.extend(Reading((uploader,), piece, "uploader") for piece in pieces[1:])
    readings.append(Reading((uploader,), bare, "label"))
    return tuple(dict.fromkeys(readings))


def soundcloud_candidate(item: dict) -> Candidate:
    """Builds a candidate from a SoundCloud track object.

    Args:
        item: SoundCloud track.

    Returns:
        The candidate.
    """
    uploader = (item.get("user") or {}).get("username") or ""
    title = item.get("title") or ""
    access = item.get("access") or PLAYABLE
    duration = item.get("duration")
    if access == "preview" and duration == PREVIEW_CLIP_MS:
        duration = None
    return Candidate(
        id=str(item.get("id")),
        raw_title=title,
        readings=soundcloud_readings(title, uploader, item.get("metadata_artist") or ""),
        duration_ms=duration,
        album=item.get("release") or "",
        uploader=uploader,
        plays=item.get("playback_count") or 0,
        isrc=normalize_isrc(item.get("isrc")),
        access=access,
    )
