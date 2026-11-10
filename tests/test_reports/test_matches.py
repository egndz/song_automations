"""Tests for the match report."""

import csv

import pytest

from song_automations.reports.matches import write_match_report
from song_automations.state.tracker import StateTracker


@pytest.mark.parametrize(
    ("track_id", "tier", "status"),
    [
        ("sc1", "exact", "exact"),
        ("sc1", "other_mix", "other_mix"),
        ("sc1", None, "legacy"),
        (None, None, "missing"),
        (None, "gated", "gated"),
    ],
)
def test_status_tells_missing_from_gated(tmp_path, track_id, tier, status):
    """A track that exists only as an upload the account cannot play is listed as gated, not missing."""
    state = StateTracker(tmp_path / "state.db")
    state.save_matched_track(1, "A1", "Bicep", "Glue", "soundcloud", track_id, 0.9 if track_id else 0.0, match_tier=tier)
    summary = write_match_report(state, tmp_path / "matches.csv")
    with (tmp_path / "matches.csv").open() as handle:
        [row] = list(csv.DictReader(handle))
    assert row["status"] == status
    assert summary["soundcloud"][status] == 1
