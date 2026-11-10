"""Writes every matching decision with its evidence, for checking by eye."""

import collections
import csv
from pathlib import Path

from song_automations.state.tracker import GATED, StateTracker

FIELDS = [
    "destination",
    "status",
    "confidence",
    "artist",
    "title",
    "matched_title",
    "uploader",
    "reading",
    "review",
    "release_url",
    "platform_id",
]


def write_match_report(
    state: StateTracker,
    path: Path,
    destination: str | None = None,
) -> dict[str, collections.Counter]:
    """Writes the decisions to a CSV file, weakest evidence first.

    Args:
        state: State tracker.
        path: Output file.
        destination: Optional platform filter.

    Returns:
        Counts per status for each platform.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    summary: dict[str, collections.Counter] = {}
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in state.get_matches_for_report(destination):
            if row["destination_track_id"]:
                status = row["match_tier"] or "legacy"
            else:
                status = GATED if row["match_tier"] == GATED else "missing"
            summary.setdefault(row["destination"], collections.Counter())[status] += 1
            writer.writerow(
                {
                    "destination": row["destination"],
                    "status": status,
                    "confidence": row["match_confidence"],
                    "artist": row["artist"],
                    "title": row["track_name"],
                    "matched_title": row["matched_title"] or "",
                    "uploader": row["matched_uploader"] or "",
                    "reading": row["match_reading"] or "",
                    "review": row["review_status"] or "",
                    "release_url": f"https://www.discogs.com/release/{row['discogs_release_id']}",
                    "platform_id": row["destination_track_id"] or "",
                }
            )
    return summary
