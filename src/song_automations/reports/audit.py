"""Checks every stored match against the evidence kept for it, without calling any platform."""

import collections
import csv
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from song_automations.matching.matcher import ISRC_KIND, artists_agree, clean, normalize_isrc
from song_automations.state.tracker import GATED

PLATFORMS = ("spotify", "soundcloud")
PLATFORM_LABELS = {"spotify": "Spotify", "soundcloud": "SoundCloud"}
LENGTH_TOLERANCE_MS = 5_000
DISAGREEMENTS_NAME = "audit-disagreements.csv"
SHARED_IDS_NAME = "audit-shared-ids.csv"

REQUIRED_COLUMNS = (
    "discogs_release_id",
    "discogs_track_position",
    "artist",
    "track_name",
    "destination",
    "destination_track_id",
)
OPTIONAL_COLUMNS = (
    "match_tier",
    "match_reading",
    "matched_title",
    "matched_uploader",
    "matched_duration_ms",
    "matched_isrc",
    "review_status",
)

MATCHED = "matched"
MISSING = "missing"
REFUSED = "refused"
STATUS_LABELS = {
    MATCHED: "Matched",
    MISSING: "Missing",
    GATED: "Gated (only as an upload the account cannot play)",
    REFUSED: "Refused in review, waiting for a new search",
}

SAME_RECORDING = "same_recording"
EXACT = "exact"
ORIGINAL = "original"
OTHER_MIX = "other_mix"
OTHER_ACCOUNT = "other_account"
UNCLASSIFIED = "unclassified"
EVIDENCE_LABELS = {
    SAME_RECORDING: "Same recording code (ISRC)",
    EXACT: "Exact version",
    ORIGINAL: "Original mix instead of the listed version",
    OTHER_MIX: "Another mix of the same song",
    OTHER_ACCOUNT: "Upload by an account that is not the artist's",
    UNCLASSIFIED: "No evidence stored (older or hand-picked match)",
}
ACCOUNT_READINGS = frozenset({"uploader", "label"})

VERIFIED = "verified"
UNCHECKED = "unchecked"

DISAGREEMENT_FIELDS = [
    "artist",
    "title",
    "reason",
    "spotify_title",
    "soundcloud_title",
    "spotify_length",
    "soundcloud_length",
    "spotify_isrc",
    "soundcloud_isrc",
    "soundcloud_uploader",
    "spotify_id",
    "soundcloud_id",
    "release_url",
]
SHARED_ID_FIELDS = [
    "platform",
    "platform_id",
    "matched_title",
    "uses",
    "same_discogs_title",
    "discogs_tracks",
]


@dataclass(frozen=True)
class StoredMatch:
    """One row of the match table, with the columns an old database lacks left empty.

    Attributes:
        release_id: Discogs release id.
        position: Track position on the release.
        artist: Artist as Discogs credits it.
        title: Title on Discogs.
        platform: spotify or soundcloud.
        platform_id: Track id on the platform, None when nothing was matched.
        tier: exact, original, other_mix or gated, None when not stored.
        reading: Which reading of the platform title passed, None when not stored.
        matched_title: Title on the platform.
        uploader: Uploading account on SoundCloud.
        duration_ms: Length of the platform track, None when not stored.
        isrc: Recording code of the platform track, empty when not stored.
        review: Review status, None when not stored.
    """

    release_id: int
    position: str
    artist: str
    title: str
    platform: str
    platform_id: str | None = None
    tier: str | None = None
    reading: str | None = None
    matched_title: str | None = None
    uploader: str | None = None
    duration_ms: int | None = None
    isrc: str = ""
    review: str | None = None

    @property
    def key(self) -> tuple[int, str]:
        """Identifies the Discogs track, whatever the platform.

        Returns:
            Release id and position.
        """
        return (self.release_id, self.position)

    @property
    def status(self) -> str:
        """Tells what the row stands for.

        Returns:
            matched, missing, gated or refused.
        """
        if self.platform_id:
            return REFUSED if self.review == "rejected" else MATCHED
        return GATED if self.tier == GATED else MISSING


@dataclass
class PlatformAudit:
    """Counts for one platform.

    Attributes:
        statuses: Tracks per status.
        evidence: Matched tracks per evidence class.
    """

    statuses: collections.Counter = field(default_factory=collections.Counter)
    evidence: collections.Counter = field(default_factory=collections.Counter)


@dataclass(frozen=True)
class Disagreement:
    """A Discogs track whose two platform matches do not look like one recording.

    Attributes:
        spotify: The Spotify match.
        soundcloud: The SoundCloud match.
        reason: isrc, length, or isrc and length.
    """

    spotify: StoredMatch
    soundcloud: StoredMatch
    reason: str


@dataclass(frozen=True)
class SharedId:
    """A platform track that more than one Discogs track is matched to.

    Attributes:
        platform: spotify or soundcloud.
        platform_id: Track id on the platform.
        users: The Discogs tracks matched to it.
    """

    platform: str
    platform_id: str
    users: tuple[StoredMatch, ...]

    @property
    def same_title(self) -> bool:
        """Tells whether every Discogs track using the id has the same title.

        The same song on two records in the collection shares a platform
        track for a good reason, two different titles do not.

        Returns:
            True when the titles agree.
        """
        return len({clean(user.title) for user in self.users}) == 1


@dataclass
class AuditReport:
    """Everything the audit found.

    Attributes:
        platforms: Counts per audited platform.
        both: Discogs tracks matched on both platforms.
        verified: Of those, how many pass every check that could be made.
        unchecked: Of those, how many have nothing stored to compare.
        disagreements: Of those, the ones that fail a check.
        shared_ids: Platform tracks used by more than one Discogs track.
    """

    platforms: dict[str, PlatformAudit] = field(default_factory=dict)
    both: int = 0
    verified: int = 0
    unchecked: int = 0
    disagreements: list[Disagreement] = field(default_factory=list)
    shared_ids: list[SharedId] = field(default_factory=list)


def load_matches(db_path: Path) -> list[StoredMatch]:
    """Reads the match table without changing the database.

    The file is opened read-only and only the columns it really has are
    asked for, so a database from an older version is read as it is instead
    of being upgraded.

    Args:
        db_path: The state database.

    Returns:
        Every stored row, empty when there is no database or no match table.
    """
    if not db_path.is_file():
        return []
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(matched_tracks)")}
        if not set(REQUIRED_COLUMNS) <= columns:
            return []
        selected = [*REQUIRED_COLUMNS, *(name for name in OPTIONAL_COLUMNS if name in columns)]
        rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM matched_tracks "
            "ORDER BY destination, artist, track_name, discogs_release_id, discogs_track_position"
        ).fetchall()
    finally:
        connection.close()
    return [_stored_match(dict(row)) for row in rows]


def _stored_match(row: dict) -> StoredMatch:
    """Turns a database row into a stored match.

    Args:
        row: Column names and values, without the columns the database lacks.

    Returns:
        The stored match.
    """
    track_id = row["destination_track_id"]
    return StoredMatch(
        release_id=row["discogs_release_id"],
        position=row["discogs_track_position"],
        artist=row["artist"] or "",
        title=row["track_name"] or "",
        platform=row["destination"],
        platform_id=str(track_id) if track_id not in (None, "") else None,
        tier=row.get("match_tier"),
        reading=row.get("match_reading"),
        matched_title=row.get("matched_title"),
        uploader=row.get("matched_uploader"),
        duration_ms=row.get("matched_duration_ms") or None,
        isrc=normalize_isrc(row.get("matched_isrc")),
        review=row.get("review_status"),
    )


def evidence_class(match: StoredMatch, other: StoredMatch | None = None) -> str:
    """Names the strongest evidence stored for a matched track.

    A recording code settles it, whether the matcher accepted the track by
    its code or the other platform's match carries the same one. An upload by
    an account that is neither the artist's nor recognised as the label's
    comes last whatever its version, because the stored columns cannot tell a
    label or a credited remixer from a stranger.

    Args:
        match: A matched track.
        other: The match of the same Discogs track on the other platform, if any.

    Returns:
        The evidence class.
    """
    if match.reading == ISRC_KIND:
        return SAME_RECORDING
    if match.isrc and other is not None and other.isrc == match.isrc:
        return SAME_RECORDING
    if match.tier not in (EXACT, ORIGINAL, OTHER_MIX):
        return UNCLASSIFIED
    if (
        match.uploader
        and match.reading not in ACCOUNT_READINGS
        and not artists_agree((match.artist,), (match.uploader,))
    ):
        return OTHER_ACCOUNT
    return match.tier


def compare(spotify: StoredMatch, soundcloud: StoredMatch) -> str:
    """Checks whether two platform matches of one Discogs track look like one recording.

    Args:
        spotify: The Spotify match.
        soundcloud: The SoundCloud match.

    Returns:
        verified when every check that can be made passes, unchecked when
        none can be made, otherwise the checks that fail: isrc, length, or
        isrc and length.
    """
    checks: list[tuple[str, bool]] = []
    if spotify.isrc and soundcloud.isrc:
        checks.append(("isrc", spotify.isrc == soundcloud.isrc))
    if spotify.duration_ms and soundcloud.duration_ms:
        difference = abs(spotify.duration_ms - soundcloud.duration_ms)
        checks.append(("length", difference <= LENGTH_TOLERANCE_MS))
    failed = [name for name, passed in checks if not passed]
    if failed:
        return " and ".join(failed)
    return VERIFIED if checks else UNCHECKED


def find_shared_ids(matches: Iterable[StoredMatch], platforms: Sequence[str]) -> list[SharedId]:
    """Finds platform tracks that more than one Discogs track is matched to.

    Args:
        matches: Matched tracks.
        platforms: Platforms to look at.

    Returns:
        The shared tracks, those used for different titles first.
    """
    users: dict[tuple[str, str], list[StoredMatch]] = {}
    for match in matches:
        if match.status == MATCHED and match.platform in platforms:
            users.setdefault((match.platform, match.platform_id), []).append(match)
    shared = [
        SharedId(platform, platform_id, tuple(group))
        for (platform, platform_id), group in users.items()
        if len(group) > 1
    ]
    return sorted(shared, key=lambda item: (item.same_title, item.platform, item.platform_id))


def build_audit(matches: Sequence[StoredMatch], platforms: Sequence[str] = PLATFORMS) -> AuditReport:
    """Audits the stored matches.

    The per-platform counts and the shared tracks cover the chosen platforms.
    The comparison between the platforms always uses both, since it is about
    pairs.

    Args:
        matches: Every stored row.
        platforms: Platforms to report on.

    Returns:
        The audit.
    """
    matched: dict[str, dict[tuple[int, str], StoredMatch]] = {name: {} for name in PLATFORMS}
    for match in matches:
        if match.status == MATCHED and match.platform in matched:
            matched[match.platform][match.key] = match
    report = AuditReport(platforms={name: PlatformAudit() for name in platforms})
    for match in matches:
        audit = report.platforms.get(match.platform)
        if audit is None:
            continue
        audit.statuses[match.status] += 1
        if match.status == MATCHED:
            other_platform = next((name for name in PLATFORMS if name != match.platform), None)
            other = matched.get(other_platform, {}).get(match.key)
            audit.evidence[evidence_class(match, other)] += 1
    for key, spotify in matched["spotify"].items():
        soundcloud = matched["soundcloud"].get(key)
        if soundcloud is None:
            continue
        report.both += 1
        verdict = compare(spotify, soundcloud)
        if verdict == VERIFIED:
            report.verified += 1
        elif verdict == UNCHECKED:
            report.unchecked += 1
        else:
            report.disagreements.append(Disagreement(spotify, soundcloud, verdict))
    report.disagreements.sort(key=lambda item: (item.reason, item.spotify.artist, item.spotify.title))
    report.shared_ids = find_shared_ids(matches, platforms)
    return report


def format_length(duration_ms: int | None) -> str:
    """Writes a length the way a player shows it.

    Args:
        duration_ms: Length in milliseconds, None when not stored.

    Returns:
        Minutes and seconds, empty when the length is not stored.
    """
    if not duration_ms:
        return ""
    seconds = round(duration_ms / 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


def write_disagreements(report: AuditReport, path: Path) -> None:
    """Writes the tracks whose two matches disagree to a CSV file.

    Args:
        report: The audit.
        path: Output file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DISAGREEMENT_FIELDS)
        writer.writeheader()
        for item in report.disagreements:
            writer.writerow(
                {
                    "artist": item.spotify.artist,
                    "title": item.spotify.title,
                    "reason": item.reason,
                    "spotify_title": item.spotify.matched_title or "",
                    "soundcloud_title": item.soundcloud.matched_title or "",
                    "spotify_length": format_length(item.spotify.duration_ms),
                    "soundcloud_length": format_length(item.soundcloud.duration_ms),
                    "spotify_isrc": item.spotify.isrc,
                    "soundcloud_isrc": item.soundcloud.isrc,
                    "soundcloud_uploader": item.soundcloud.uploader or "",
                    "spotify_id": item.spotify.platform_id,
                    "soundcloud_id": item.soundcloud.platform_id,
                    "release_url": f"https://www.discogs.com/release/{item.spotify.release_id}",
                }
            )


def write_shared_ids(report: AuditReport, path: Path) -> None:
    """Writes the platform tracks used by more than one Discogs track to a CSV file.

    Args:
        report: The audit.
        path: Output file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHARED_ID_FIELDS)
        writer.writeheader()
        for item in report.shared_ids:
            writer.writerow(
                {
                    "platform": item.platform,
                    "platform_id": item.platform_id,
                    "matched_title": next(
                        (user.matched_title for user in item.users if user.matched_title), ""
                    ),
                    "uses": len(item.users),
                    "same_discogs_title": "yes" if item.same_title else "no",
                    "discogs_tracks": " | ".join(
                        f"{user.artist} - {user.title} [release {user.release_id} {user.position}]"
                        for user in item.users
                    ),
                }
            )


def summary_table(report: AuditReport) -> Table:
    """Lays the per-platform counts out as a table.

    Args:
        report: The audit.

    Returns:
        A table with the statuses first and the evidence classes, strongest first, below.
    """
    table = Table(title="Stored matches")
    table.add_column("")
    for name in report.platforms:
        table.add_column(PLATFORM_LABELS.get(name, name), justify="right")
    for status, label in STATUS_LABELS.items():
        counts = [audit.statuses[status] for audit in report.platforms.values()]
        if status in (MATCHED, MISSING) or any(counts):
            table.add_row(label, *(str(count) for count in counts))
    table.add_section()
    table.add_row("[bold]Matched tracks by evidence, strongest first[/bold]")
    for evidence, label in EVIDENCE_LABELS.items():
        counts = [audit.evidence[evidence] for audit in report.platforms.values()]
        if evidence != UNCLASSIFIED or any(counts):
            table.add_row(label, *(str(count) for count in counts))
    return table


def headline(report: AuditReport) -> str:
    """Sums the comparison between the platforms up in one line.

    Args:
        report: The audit.

    Returns:
        The line.
    """
    if not report.both:
        return "No track is matched on both platforms yet, so no match could be cross-verified."
    return (
        f"{report.verified} of {report.both} matches found on both platforms are cross-verified "
        "(same recording code, or lengths within 5 seconds)."
    )


def run_audit(
    console: Console,
    db_path: Path,
    platforms: Sequence[str],
    output: Path,
) -> AuditReport | None:
    """Audits the stored matches, writes the two CSV files and prints the summary.

    Args:
        console: Where to print.
        db_path: The state database.
        platforms: Platforms to report on.
        output: CSV file for the disagreements, the shared tracks go next to it.

    Returns:
        The audit, or None when nothing is stored yet.
    """
    matches = load_matches(db_path)
    if not matches:
        console.print("[yellow]No stored matches found. Run a sync first.[/yellow]")
        return None
    report = build_audit(matches, platforms)
    shared_path = output.with_name(SHARED_IDS_NAME)
    write_disagreements(report, output)
    write_shared_ids(report, shared_path)
    console.print(summary_table(report))
    console.print(f"[bold]{headline(report)}[/bold]", highlight=False, soft_wrap=True)
    if report.both:
        console.print(
            f"Disagreeing: {len(report.disagreements)}. Not comparable: {report.unchecked}, "
            "because neither a recording code nor a length is stored for both.",
            highlight=False,
            soft_wrap=True,
        )
    console.print("Disagreements written to:")
    console.print(str(output), markup=False, highlight=False, soft_wrap=True)
    different = sum(1 for item in report.shared_ids if not item.same_title)
    console.print(
        f"Platform tracks used by more than one Discogs track: {len(report.shared_ids)}, "
        f"of which {different} for different titles. Written to:",
        highlight=False,
        soft_wrap=True,
    )
    console.print(str(shared_path), markup=False, highlight=False, soft_wrap=True)
    return report
