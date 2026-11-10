"""Tests for the audit of stored matches, which reads the database and nothing else."""

import csv
import hashlib
import socket
import sqlite3
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from song_automations import cli
from song_automations.config import Settings
from song_automations.reports import audit
from song_automations.reports.audit import StoredMatch
from song_automations.state.tracker import StateTracker

ORIGINAL_COLUMNS = """
    id INTEGER PRIMARY KEY,
    discogs_release_id INTEGER NOT NULL,
    discogs_track_position TEXT NOT NULL,
    artist TEXT NOT NULL,
    track_name TEXT NOT NULL,
    destination TEXT NOT NULL,
    destination_track_id TEXT,
    match_confidence REAL,
    searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
"""


def _flat(text: str) -> str:
    """Joins wrapped console output into one line.

    Args:
        text: Console output.

    Returns:
        The text with every run of whitespace turned into one space.
    """
    return " ".join(text.split())


def _match(platform: str, track_id: str | None = "id-1", position: str = "A1", **fields) -> StoredMatch:
    """Builds a stored match of the track Bicep - Glue.

    Args:
        platform: spotify or soundcloud.
        track_id: Platform track id, None for a track that was not found.
        position: Track position on the release.
        **fields: Other fields of the stored match.

    Returns:
        The stored match.
    """
    values = {"release_id": 1, "artist": "Bicep", "title": "Glue", "tier": "exact"}
    return StoredMatch(platform=platform, platform_id=track_id, position=position, **{**values, **fields})


def _save(state: StateTracker, platform: str, position: str, track_id: str | None, **fields) -> None:
    """Stores a match of the release 1 the way a sync does.

    Args:
        state: State tracker.
        platform: spotify or soundcloud.
        position: Track position on the release.
        track_id: Platform track id, None for a track that was not found.
        **fields: Other columns, as save_matched_track names them.
    """
    values = {"artist": "Bicep", "track_name": f"Track {position}", "match_tier": "exact"}
    values.update(fields)
    state.save_matched_track(
        1, position, values.pop("artist"), values.pop("track_name"), platform, track_id, 0.9, **values
    )


def _collection(tmp_path: Path) -> Path:
    """Fills a database with a small collection that shows every kind of finding.

    A1 is the same recording on both platforms, A2 has two different lengths,
    A3 has nothing to compare, B1 is only on Spotify, B2 is missing on
    Spotify and gated on SoundCloud, and B3 shares the Spotify track of A1.

    Args:
        tmp_path: Temporary folder of the test.

    Returns:
        The database file.
    """
    path = tmp_path / "state.db"
    state = StateTracker(path)
    _save(state, "spotify", "A1", "sp-1", matched_title="Glue", matched_isrc="GBABC1234567", matched_duration_ms=269_000)
    _save(
        state,
        "soundcloud",
        "A1",
        "sc-1",
        matched_title="Bicep - Glue",
        matched_uploader="Bicep",
        match_reading="isrc",
        matched_isrc="gb-abc-12-34567",
        matched_duration_ms=270_000,
    )
    _save(state, "spotify", "A2", "sp-2", matched_title="Atlas", matched_duration_ms=300_000)
    _save(
        state,
        "soundcloud",
        "A2",
        "sc-2",
        matched_title="Atlas (Live)",
        matched_uploader="Vinyl Ripper",
        match_reading="dash",
        matched_duration_ms=421_000,
    )
    _save(state, "spotify", "A3", "sp-3", match_tier="original")
    _save(state, "soundcloud", "A3", "sc-3", match_tier="other_mix", matched_uploader="Bicep", match_reading="uploader")
    _save(state, "spotify", "B1", "sp-4")
    _save(state, "spotify", "B2", None, match_tier=None)
    _save(state, "soundcloud", "B2", None, match_tier="gated")
    _save(state, "spotify", "B3", "sp-1", track_name="Glue (Reprise)")
    return path


def _old_database(path: Path, extra_columns: tuple[str, ...]) -> None:
    """Creates a database the way an older version left it, with two matched rows.

    Args:
        path: Database file to create.
        extra_columns: Columns later versions added that this database already has.
    """
    extra = "".join(f", {name} TEXT" for name in extra_columns)
    connection = sqlite3.connect(path)
    connection.execute(f"CREATE TABLE matched_tracks ({ORIGINAL_COLUMNS}{extra})")
    connection.executemany(
        "INSERT INTO matched_tracks (discogs_release_id, discogs_track_position, artist, track_name, "
        "destination, destination_track_id, match_confidence) VALUES (1, 'A1', 'Bicep', 'Glue', ?, ?, ?)",
        [("spotify", "sp-1", 0.9), ("soundcloud", None, 0.0)],
    )
    connection.commit()
    connection.close()


class TestEvidence:
    """Naming the evidence behind one match."""

    @pytest.mark.parametrize(
        ("match", "other", "expected"),
        [
            (_match("soundcloud", reading="isrc", uploader="SomeLabel"), None, "same_recording"),
            (_match("spotify", isrc="GB1"), _match("soundcloud", isrc="GB1"), "same_recording"),
            (_match("spotify", isrc="GB1"), _match("soundcloud", isrc="GB2"), "exact"),
            (_match("spotify", isrc="GB1"), _match("soundcloud"), "exact"),
            (_match("spotify"), None, "exact"),
            (_match("spotify", tier="original"), None, "original"),
            (_match("spotify", tier="other_mix"), None, "other_mix"),
            (_match("soundcloud", reading="dash", uploader="Vinyl Ripper"), None, "other_account"),
            (_match("soundcloud", tier="other_mix", reading="metadata", uploader="xx"), None, "other_account"),
            (_match("soundcloud", reading="dash", uploader="Bicep"), None, "exact"),
            (_match("soundcloud", reading="uploader", uploader="BICEP"), None, "exact"),
            (_match("soundcloud", tier="original", reading="label", uploader="Ninja Tune"), None, "original"),
            (_match("spotify", tier=None), None, "unclassified"),
            (_match("soundcloud", tier=None, uploader="Vinyl Ripper"), None, "unclassified"),
        ],
    )
    def test_strongest_stored_evidence_names_the_class(self, match, other, expected):
        """A recording code beats the version tier, and an upload by another account comes last."""
        assert audit.evidence_class(match, other) == expected

    @pytest.mark.parametrize(
        ("spotify", "soundcloud", "expected"),
        [
            ({"isrc": "GB1"}, {"isrc": "GB1"}, "verified"),
            ({"isrc": "GB1", "duration_ms": 200_000}, {"isrc": "GB1", "duration_ms": 203_000}, "verified"),
            ({"duration_ms": 200_000}, {"duration_ms": 205_000}, "verified"),
            ({"duration_ms": 200_000}, {"duration_ms": 205_001}, "length"),
            ({"duration_ms": 205_001}, {"duration_ms": 200_000}, "length"),
            ({"isrc": "GB1"}, {"isrc": "GB2"}, "isrc"),
            ({"isrc": "GB1", "duration_ms": 200_000}, {"isrc": "GB2", "duration_ms": 201_000}, "isrc"),
            ({"isrc": "GB1", "duration_ms": 200_000}, {"isrc": "GB1", "duration_ms": 260_000}, "length"),
            ({"isrc": "GB1", "duration_ms": 200_000}, {"isrc": "GB2", "duration_ms": 260_000}, "isrc and length"),
            ({}, {}, "unchecked"),
            ({"isrc": "GB1"}, {"duration_ms": 200_000}, "unchecked"),
            ({"isrc": "GB1", "duration_ms": 200_000}, {}, "unchecked"),
        ],
    )
    def test_two_matches_are_compared_on_what_both_store(self, spotify, soundcloud, expected):
        """Codes are compared when both sides have one, lengths within 5 seconds likewise."""
        verdict = audit.compare(_match("spotify", **spotify), _match("soundcloud", **soundcloud))
        assert verdict == expected

    @pytest.mark.parametrize(
        ("duration_ms", "expected"),
        [(None, ""), (0, ""), (59_000, "0:59"), (60_000, "1:00"), (269_499, "4:29"), (3_600_000, "60:00")],
    )
    def test_lengths_read_like_a_player_shows_them(self, duration_ms, expected):
        """Lengths are minutes and seconds, and nothing when not stored."""
        assert audit.format_length(duration_ms) == expected


class TestBuildAudit:
    """The audit of a whole collection."""

    @pytest.mark.parametrize(
        ("platforms", "statuses", "evidence"),
        [
            (
                ("spotify",),
                {"spotify": {"matched": 5, "missing": 1}},
                {"spotify": {"same_recording": 1, "exact": 3, "original": 1}},
            ),
            (
                ("soundcloud",),
                {"soundcloud": {"matched": 3, "gated": 1}},
                {"soundcloud": {"same_recording": 1, "other_account": 1, "other_mix": 1}},
            ),
            (
                ("spotify", "soundcloud"),
                {"spotify": {"matched": 5, "missing": 1}, "soundcloud": {"matched": 3, "gated": 1}},
                {
                    "spotify": {"same_recording": 1, "exact": 3, "original": 1},
                    "soundcloud": {"same_recording": 1, "other_account": 1, "other_mix": 1},
                },
            ),
        ],
    )
    def test_counts_cover_the_chosen_platforms(self, tmp_path, platforms, statuses, evidence):
        """Each chosen platform gets its matched, missing and gated counts and its evidence classes."""
        report = audit.build_audit(audit.load_matches(_collection(tmp_path)), platforms)
        assert {name: dict(item.statuses) for name, item in report.platforms.items()} == statuses
        assert {name: dict(item.evidence) for name, item in report.platforms.items()} == evidence

    @pytest.mark.parametrize("platforms", [("spotify",), ("soundcloud",), ("spotify", "soundcloud")])
    def test_platforms_are_compared_whatever_the_filter(self, tmp_path, platforms):
        """One pair agrees, one has different lengths and one has nothing to compare."""
        report = audit.build_audit(audit.load_matches(_collection(tmp_path)), platforms)
        assert (report.both, report.verified, report.unchecked) == (3, 1, 1)
        [disagreement] = report.disagreements
        assert disagreement.reason == "length"
        assert disagreement.spotify.matched_title == "Atlas"
        assert disagreement.soundcloud.matched_title == "Atlas (Live)"
        assert (disagreement.spotify.duration_ms, disagreement.soundcloud.duration_ms) == (300_000, 421_000)

    @pytest.mark.parametrize(
        ("review", "counted", "status"),
        [("rejected", False, "refused"), ("approved", True, "matched"), ("pending", True, "matched"), (None, True, "matched")],
    )
    def test_refused_match_is_not_treated_as_a_match(self, review, counted, status):
        """A match refused in review is counted on its own and never compared or shared."""
        matches = [
            _match("spotify", "sp-1", review=review, isrc="GB1"),
            _match("soundcloud", "sc-1", isrc="GB1"),
            _match("spotify", "sp-1", position="A2"),
        ]
        report = audit.build_audit(matches)
        assert report.platforms["spotify"].statuses[status] >= 1
        assert report.both == (1 if counted else 0)
        assert len(report.shared_ids) == (1 if counted else 0)

    @pytest.mark.parametrize(
        ("second", "shared", "same_title"),
        [
            ({"platform": "spotify", "track_id": "sp-1", "title": "Glue"}, 1, True),
            ({"platform": "spotify", "track_id": "sp-1", "title": "GLUE!"}, 1, True),
            ({"platform": "spotify", "track_id": "sp-1", "title": "Aura"}, 1, False),
            ({"platform": "spotify", "track_id": "sp-2", "title": "Aura"}, 0, None),
            ({"platform": "soundcloud", "track_id": "sp-1", "title": "Aura"}, 0, None),
            ({"platform": "spotify", "track_id": None, "title": "Aura"}, 0, None),
        ],
    )
    def test_platform_track_used_twice_within_a_platform_is_listed(self, second, shared, same_title):
        """Two Discogs tracks on one platform track are listed, and whether their titles agree."""
        matches = [_match("spotify", "sp-1"), _match(position="B1", release_id=2, **second)]
        found = audit.build_audit(matches).shared_ids
        assert len(found) == shared
        if shared:
            assert found[0].same_title is same_title
            assert len(found[0].users) == 2


class TestReadingTheDatabase:
    """Reading what is stored without changing it."""

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [("gb-abc-12-34567", "GBABC1234567"), ("GBABC1234567", "GBABC1234567"), (None, "")],
    )
    def test_rows_come_back_with_their_evidence(self, tmp_path, stored, expected):
        """Every column a sync stores is read, and the recording code in one spelling."""
        state = StateTracker(tmp_path / "state.db")
        _save(
            state,
            "soundcloud",
            "A1",
            "sc-1",
            matched_title="Bicep - Glue",
            matched_uploader="Bicep",
            match_reading="dash",
            matched_duration_ms=270_000,
            matched_isrc=stored,
        )
        [match] = audit.load_matches(tmp_path / "state.db")
        assert match == StoredMatch(
            release_id=1,
            position="A1",
            artist="Bicep",
            title="Track A1",
            platform="soundcloud",
            platform_id="sc-1",
            tier="exact",
            reading="dash",
            matched_title="Bicep - Glue",
            uploader="Bicep",
            duration_ms=270_000,
            isrc=expected,
            review="pending",
        )

    @pytest.mark.parametrize(
        "extra_columns",
        [
            (),
            ("review_status",),
            ("review_status", "match_tier", "matched_title"),
            ("matched_isrc",),
        ],
    )
    def test_old_database_is_read_as_it_is(self, tmp_path, extra_columns):
        """A database from an older version is audited without being upgraded or copied."""
        path = tmp_path / "state.db"
        _old_database(path, extra_columns)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        report = audit.run_audit(Console(quiet=True), path, audit.PLATFORMS, tmp_path / "out.csv")
        assert dict(report.platforms["spotify"].statuses) == {"matched": 1}
        assert dict(report.platforms["spotify"].evidence) == {"unclassified": 1}
        assert dict(report.platforms["soundcloud"].statuses) == {"missing": 1}
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        assert sorted(item.name for item in tmp_path.iterdir()) == [
            "audit-shared-ids.csv",
            "out.csv",
            "state.db",
        ]

    @pytest.mark.parametrize("kind", ["absent", "empty_file", "no_table", "empty_table", "other_table"])
    def test_nothing_stored_is_said_plainly(self, tmp_path, capsys, kind):
        """Without a database, a match table or a row, the audit says so and writes nothing."""
        path = tmp_path / "state.db"
        if kind == "empty_file":
            path.write_bytes(b"")
        elif kind == "no_table":
            sqlite3.connect(path).close()
        elif kind == "empty_table":
            StateTracker(path)
        elif kind == "other_table":
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE matched_tracks (id INTEGER PRIMARY KEY, note TEXT)")
            connection.commit()
            connection.close()
        report = audit.run_audit(Console(), path, audit.PLATFORMS, tmp_path / "reports" / "out.csv")
        assert report is None
        assert "No stored matches found" in capsys.readouterr().out
        assert not (tmp_path / "reports").exists()


class TestAuditCommand:
    """The audit command as typed in a terminal."""

    @staticmethod
    def _run(tmp_path, monkeypatch, arguments):
        """Runs the audit command on the small collection with the network cut off.

        Args:
            tmp_path: Temporary folder of the test, used as the data directory.
            monkeypatch: Pytest monkeypatch.
            arguments: Command line arguments after audit.

        Returns:
            The command result.
        """

        def refuse(*args, **kwargs):
            """Fails instead of opening a network connection.

            Args:
                *args: Ignored.
                **kwargs: Ignored.

            Raises:
                AssertionError: Always.
            """
            raise AssertionError("the audit tried to use the network")

        _collection(tmp_path)
        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(cli, "get_settings", lambda: Settings(data_dir=tmp_path))
        return CliRunner().invoke(cli.app, ["audit", *arguments])

    @pytest.mark.parametrize(
        ("arguments", "folder"),
        [([], "reports"), (["--platform", "all"], "reports"), (["--output", "custom/mine.csv"], "custom")],
    )
    def test_audit_prints_the_summary_and_writes_both_files(self, tmp_path, monkeypatch, arguments, folder):
        """The headline, the table and both file paths are printed, and the files hold the findings."""
        monkeypatch.chdir(tmp_path)
        result = self._run(tmp_path, monkeypatch, arguments)
        output = _flat(result.output)
        name = "mine.csv" if folder == "custom" else "audit-disagreements.csv"
        assert result.exit_code == 0
        assert "1 of 3 matches found on both platforms are cross-verified" in output
        assert "Disagreeing: 1. Not comparable: 1," in output
        assert "Platform tracks used by more than one Discogs track: 1, of which 1 for different titles" in output
        assert "Same recording code (ISRC)" in output
        assert "Spotify" in output and "SoundCloud" in output
        assert f"{folder}/{name}" in result.output
        assert f"{folder}/audit-shared-ids.csv" in result.output
        with (tmp_path / folder / name).open() as handle:
            [row] = list(csv.DictReader(handle))
        assert row["artist"] == "Bicep"
        assert row["title"] == "Track A2"
        assert row["reason"] == "length"
        assert (row["spotify_title"], row["soundcloud_title"]) == ("Atlas", "Atlas (Live)")
        assert (row["spotify_length"], row["soundcloud_length"]) == ("5:00", "7:01")
        assert row["soundcloud_uploader"] == "Vinyl Ripper"
        with (tmp_path / folder / "audit-shared-ids.csv").open() as handle:
            [shared] = list(csv.DictReader(handle))
        assert (shared["platform"], shared["platform_id"], shared["uses"]) == ("spotify", "sp-1", "2")
        assert shared["same_discogs_title"] == "no"
        assert "Bicep - Track A1 [release 1 A1]" in shared["discogs_tracks"]
        assert "Bicep - Glue (Reprise) [release 1 B3]" in shared["discogs_tracks"]

    @pytest.mark.parametrize(
        ("platform", "shown", "hidden", "shared"),
        [("spotify", "Spotify", "SoundCloud", 1), ("soundcloud", "SoundCloud", "Spotify", 0)],
    )
    def test_platform_option_narrows_the_counts(self, tmp_path, monkeypatch, platform, shown, hidden, shared):
        """One platform shows its own column and shared tracks, the comparison stays."""
        result = self._run(tmp_path, monkeypatch, ["--platform", platform])
        table = result.output.split("cross-verified")[0]
        assert result.exit_code == 0
        assert shown in table
        assert hidden not in table
        assert "1 of 3 matches found on both platforms" in _flat(result.output)
        assert f"used by more than one Discogs track: {shared}," in _flat(result.output)

    @pytest.mark.parametrize("platform", ["tidal", "", "both"])
    def test_unknown_platform_is_refused(self, tmp_path, monkeypatch, platform):
        """A platform that does not exist ends with an error and no files."""
        result = self._run(tmp_path, monkeypatch, ["--platform", platform])
        assert result.exit_code == 1
        assert "Unknown platform" in result.output
        assert not (tmp_path / "reports" / "audit-disagreements.csv").exists()
