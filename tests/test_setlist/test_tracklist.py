"""Tests for reading the tracklist of a set."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from song_automations.setlist import tracklist
from song_automations.setlist.tracklist import (
    SetlistError,
    SetTrack,
    fetch_video,
    parse_line,
    parse_tracklist,
    read_source,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0:00 Kings of Tomorrow – Finally (Original Extended Mix) [125 BPM]", SetTrack("Kings of Tomorrow", "Finally (Original Extended Mix)", 0)),
        ("07:49  Greed - Pump Up The Volume (DMC Mix) [1994 | Stress Records]", SetTrack("Greed", "Pump Up The Volume (DMC Mix)", 469)),
        ("[1:02:03] Bicep - Glue", SetTrack("Bicep", "Glue", 3723)),
        ("Bicep - Glue 1:02:03", SetTrack("Bicep", "Glue", 3723)),
        ("12:10 - Bicep - Glue", SetTrack("Bicep", "Glue", 730)),
        ("01. Bicep - Glue", SetTrack("Bicep", "Glue", None, True)),
        ("12) Bicep — Glue (Dub) {Ninja Tune}", SetTrack("Bicep", "Glue (Dub)", None, True)),
        ("03. 10:00 Jay-Z - 99 Problems", SetTrack("Jay-Z", "99 Problems", 600, True)),
        ("01..Mesa Dub || Federsen || Ranges 10 Years || Ranges", SetTrack("Mesa Dub", "Federsen", None, True)),
        ("Bicep - Glue - Part 2", SetTrack("Bicep", "Glue - Part 2")),
        ('Bicep - "Glue"', SetTrack("Bicep", "Glue")),
        ("[00:45:10] ID - ID", None),
        ("5. Unknown - ???", None),
        ("Instagram - https://example.com/me", None),
        ("0:00 Intro", None),
        ("", None),
    ],
)
def test_parse_line_reads_the_first_two_fields(text, expected):
    """Times, numbers and bracketed notes are taken off, links and unknown tracks are skipped."""
    assert parse_line(text) == expected


CHAPTERS = [{"title": f"Artist {n} - Title {n}", "start_time": n * 60.0} for n in range(3)]
TIMED = "\n".join(f"{n}:00 Timed {n} - Song {n}" for n in range(3))
NUMBERED = "\n".join(f"{n}. Numbered {n} - Song {n}" for n in range(1, 4))
PLAIN = "Follow me - on tour\nBicep - Glue\nOvermono - So U Kno"


@pytest.mark.parametrize(
    ("description", "chapters", "every_line", "artists"),
    [
        (TIMED, CHAPTERS, False, ["Timed 0", "Timed 1", "Timed 2"]),
        (f"{NUMBERED}\n0:00 Only - One", CHAPTERS, False, ["Artist 0", "Artist 1", "Artist 2"]),
        (NUMBERED, [{"title": "<Untitled Chapter 1>", "start_time": 0.0}], False, ["Numbered 1", "Numbered 2", "Numbered 3"]),
        (PLAIN, [], False, []),
        (PLAIN, [], True, ["Follow me", "Bicep", "Overmono"]),
        ("1. One - Song\n2. Two - Song", [], False, []),
        ("", [], True, []),
    ],
)
def test_parse_tracklist_trusts_times_then_chapters_then_numbers(description, chapters, every_line, artists):
    """Plain dashed lines only count in a file the user wrote the tracklist in."""
    assert [track.artist for track in parse_tracklist(description, chapters, every_line)] == artists


def test_chapters_carry_their_start_time():
    """A track read from a chapter starts where the chapter starts."""
    assert [track.starts_at for track in parse_tracklist("", CHAPTERS)] == [0, 60, 120]


def answer(stdout="", code=0, stderr=""):
    """Builds what the video program answered.

    Args:
        stdout: Printed details.
        code: Exit code.
        stderr: Printed errors.

    Returns:
        The finished process.
    """
    return SimpleNamespace(stdout=stdout, returncode=code, stderr=stderr)


DETAILS = json.dumps({"title": "Vinyl set", "description": TIMED, "chapters": CHAPTERS})


@pytest.mark.parametrize(
    ("outcome", "said"),
    [
        (answer(DETAILS), None),
        (answer(json.dumps({"title": None})), None),
        (answer("", 1, "WARNING: slow\nERROR: Video unavailable"), "ERROR: Video unavailable"),
        (answer("", 0), "no answer"),
        (answer("not json"), "not understood"),
        (subprocess.TimeoutExpired("yt-dlp", 120), "timed out"),
        (OSError("cannot run"), "cannot run"),
    ],
)
def test_fetch_video_reads_details_or_says_why_not(monkeypatch, outcome, said):
    """A video that cannot be read gives one plain reason."""
    seen = []

    def run(command, **options):
        """Stands in for the video program.

        Args:
            command: Command line.
            **options: Process options.

        Returns:
            The prepared answer.
        """
        seen.append(command)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(tracklist, "_downloader", lambda: ["yt-dlp"])
    monkeypatch.setattr(tracklist.subprocess, "run", run)
    if said:
        with pytest.raises(SetlistError, match=said):
            fetch_video("https://example.com/watch?v=1")
    else:
        video = fetch_video("https://example.com/watch?v=1")
        assert video.url == "https://example.com/watch?v=1"
        assert video.title in {"Vinyl set", "Untitled set"}
    assert seen[0][-2:] == ["--", "https://example.com/watch?v=1"]
    assert "--skip-download" in seen[0]


@pytest.mark.parametrize(
    ("on_path", "module", "expected"),
    [("/bin/yt-dlp", None, ["/bin/yt-dlp"]), (None, object(), "yt_dlp"), (None, None, None)],
)
def test_downloader_is_found_or_asked_for(monkeypatch, on_path, module, expected):
    """The program on the path wins, then the installed module, else the user is told to install it."""
    monkeypatch.setattr(tracklist.shutil, "which", lambda name: on_path)
    monkeypatch.setattr(tracklist.importlib.util, "find_spec", lambda name: module)
    if expected is None:
        with pytest.raises(SetlistError, match="brew install yt-dlp"):
            tracklist._downloader()
    elif isinstance(expected, list):
        assert tracklist._downloader() == expected
    else:
        assert tracklist._downloader()[-1] == expected


@pytest.mark.parametrize(
    ("kind", "said"),
    [("file", None), ("missing", "neither a link nor a file"), ("folder", "neither a link nor a file")],
)
def test_read_source_takes_a_tracklist_file(tmp_path, kind, said):
    """A text file stands in for a video, anything else that is not a link is refused."""
    path = tmp_path / "Boiler Room.txt"
    if kind == "file":
        path.write_text(PLAIN, encoding="utf-8")
    elif kind == "folder":
        path.mkdir()
    if said:
        with pytest.raises(SetlistError, match=said):
            read_source(str(path))
    else:
        video = read_source(str(path))
        assert (video.title, video.from_file, video.description) == ("Boiler Room", True, PLAIN)
