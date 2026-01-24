"""Missing tracks report generation."""

import csv
import json
from datetime import datetime
from pathlib import Path

from song_automations.config import Settings
from song_automations.state.tracker import Destination, StateTracker


def generate_missing_report(
    state_tracker: StateTracker,
    settings: Settings,
    format: str = "csv",
    destination: Destination | None = None,
    output_path: str | None = None,
) -> Path | None:
    """Generate a report of tracks that couldn't be found.

    Args:
        state_tracker: State tracker with missing track data.
        settings: Application settings.
        format: Output format ('csv' or 'json').
        destination: Optional filter by destination platform.
        output_path: Optional output file path.

    Returns:
        Path to the generated report, or None if no missing tracks.
    """
    missing_tracks = state_tracker.get_missing_tracks(destination)

    if not missing_tracks:
        return None

    settings.reports_dir.mkdir(parents=True, exist_ok=True)

    if output_path:
        report_path = Path(output_path)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_suffix = f"_{destination}" if destination else ""
        report_path = settings.reports_dir / f"missing_tracks{dest_suffix}_{timestamp}.{format}"

    if format == "json":
        _write_json_report(missing_tracks, report_path)
    else:
        _write_csv_report(missing_tracks, report_path)

    return report_path


def _write_csv_report(missing_tracks: list, report_path: Path) -> None:
    """Write missing tracks report as CSV.

    Args:
        missing_tracks: List of MissingTrack objects.
        report_path: Output file path.
    """
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "Artist",
                "Track Name",
                "Destination",
                "Discogs Release ID",
                "Discogs Folder ID",
                "Searched At",
            ]
        )

        for track in missing_tracks:
            writer.writerow(
                [
                    track.artist,
                    track.track_name,
                    track.destination,
                    track.discogs_release_id,
                    track.discogs_folder_id,
                    track.searched_at.isoformat(),
                ]
            )


def _write_json_report(missing_tracks: list, report_path: Path) -> None:
    """Write missing tracks report as JSON.

    Args:
        missing_tracks: List of MissingTrack objects.
        report_path: Output file path.
    """
    data = {
        "generated_at": datetime.now().isoformat(),
        "total_count": len(missing_tracks),
        "tracks": [
            {
                "artist": track.artist,
                "track_name": track.track_name,
                "destination": track.destination,
                "discogs_release_id": track.discogs_release_id,
                "discogs_folder_id": track.discogs_folder_id,
                "searched_at": track.searched_at.isoformat(),
            }
            for track in missing_tracks
        ],
    }

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
