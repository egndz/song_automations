"""Reads the tracklist the uploader of a DJ set wrote under the video."""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

MIN_TRACKS = 3
FETCH_TIMEOUT_SECONDS = 120
CLOCK = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})"
LEADING_TIME = re.compile(rf"^\s*[\[(]?{CLOCK}[\])]?[\s\-–—:|.]*")
TRAILING_TIME = re.compile(rf"[\s\-–—|]*[\[(]?{CLOCK}[\])]?\s*$")
NUMBER = re.compile(r"^\s*#?\d{1,3}\s*[.)\]:]+\s*")
TRAILING_NOTE = re.compile(r"\s*[\[{][^\[\]{}]*[\]}]\s*$")
PIPE = re.compile(r"\s*\|{1,2}\s*")
DASH = re.compile(r"\s+[-–—~]+\s+|\s*[–—]\s*")
UNKNOWN = re.compile(r"^(?:id|ids|unknown|unreleased|tba|\?+)$", re.IGNORECASE)
QUOTES = " \"'“”‘’"


class SetlistError(RuntimeError):
    """The tracklist of a set could not be read."""


@dataclass(frozen=True)
class SetTrack:
    """One line of a tracklist.

    Attributes:
        artist: First field of the line, the artist in most tracklists.
        title: Second field of the line, the title in most tracklists.
        starts_at: Second of the set the track starts at, None when the line gives no time.
        numbered: Whether the line carried a running number.
    """

    artist: str
    title: str
    starts_at: int | None = None
    numbered: bool = False


@dataclass(frozen=True)
class Video:
    """What a source says about a set.

    Attributes:
        title: Title of the video, or the name of the file.
        description: Text that may hold the tracklist.
        chapters: Chapters of the video, each with a title and a start_time.
        url: Link of the video, empty for a file.
        from_file: Whether the text is a file the user wrote the tracklist in.
    """

    title: str
    description: str
    chapters: tuple[dict, ...] = ()
    url: str = ""
    from_file: bool = False


def _seconds(found: re.Match) -> int:
    """Turns a matched clock time into seconds.

    Args:
        found: Match of the clock pattern.

    Returns:
        Seconds from the start of the set.
    """
    hours, minutes, seconds = found.groups()[-3:]
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)


def parse_line(text: str) -> SetTrack | None:
    """Reads one line as a track.

    The time and the running number are taken off the front, notes in square
    brackets such as the tempo or the label off the end, and the rest is split
    into its first two fields.

    Args:
        text: Line of a description, or the title of a chapter.

    Returns:
        The track, or None when the line is a link, names an unknown track or
        does not have two fields.
    """
    if "http" in text.lower():
        return None
    rest, starts_at, numbered = text, None, False
    while True:
        found = LEADING_TIME.match(rest)
        if found:
            starts_at = _seconds(found)
        else:
            found = NUMBER.match(rest)
            if not found:
                break
            numbered = True
        rest = rest[found.end():]
    while TRAILING_NOTE.search(rest):
        rest = TRAILING_NOTE.sub("", rest)
    found = TRAILING_TIME.search(rest)
    if found and starts_at is None:
        starts_at = _seconds(found)
        rest = rest[: found.start()]
    fields = PIPE.split(rest) if "|" in rest else DASH.split(rest, maxsplit=1)
    fields = [part.strip(QUOTES) for part in fields if part.strip(QUOTES)]
    if len(fields) < 2 or UNKNOWN.match(fields[0]) or UNKNOWN.match(fields[1]):
        return None
    return SetTrack(fields[0], fields[1], starts_at, numbered)


def parse_tracklist(
    description: str, chapters: Sequence[dict] = (), every_line: bool = False
) -> list[SetTrack]:
    """Finds the tracklist of a set.

    Lines with a time are trusted first, then the chapters of the video, then
    numbered lines. Other lines with a dash are mostly links and credits, so
    they only count in a file the user wrote the tracklist in.

    Args:
        description: Description of the video, or the text of a file.
        chapters: Chapters of the video.
        every_line: Whether lines without a time or a number count too.

    Returns:
        The tracks in set order, empty when no tracklist was found.
    """
    lines = [track for line in description.splitlines() if (track := parse_line(line))]
    timed = [track for track in lines if track.starts_at is not None]
    if len(timed) >= MIN_TRACKS:
        return timed
    chaptered = [
        replace(track, starts_at=int(chapter.get("start_time") or 0))
        for chapter in chapters
        if (track := parse_line(str(chapter.get("title") or "")))
    ]
    if len(chaptered) >= MIN_TRACKS:
        return chaptered
    numbered = [track for track in lines if track.numbered]
    if len(numbered) >= MIN_TRACKS:
        return numbered
    return lines if every_line else []


def _downloader() -> list[str]:
    """Finds the program that reads a video page.

    Returns:
        The command to run.

    Raises:
        SetlistError: When yt-dlp is not installed.
    """
    program = shutil.which("yt-dlp")
    if program:
        return [program]
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    raise SetlistError("yt-dlp is needed to read a video. Install it with: brew install yt-dlp")


def fetch_video(url: str) -> Video:
    """Reads the title, the description and the chapters of a video.

    Nothing is downloaded but the page details.

    Args:
        url: Link of the video.

    Returns:
        The video.

    Raises:
        SetlistError: When the video could not be read.
    """
    command = [
        *_downloader(),
        "--ignore-no-formats-error",
        "--no-warnings",
        "--no-playlist",
        "--skip-download",
        "--dump-json",
        "--",
        url,
    ]
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=FETCH_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetlistError(f"The video could not be read: {error}") from error
    first = done.stdout.strip().splitlines()[:1]
    if done.returncode != 0 or not first:
        reason = (done.stderr.strip().splitlines() or ["no answer"])[-1]
        raise SetlistError(f"The video could not be read: {reason}")
    try:
        details = json.loads(first[0])
    except ValueError as error:
        raise SetlistError("The video could not be read: the answer was not understood") from error
    return Video(
        title=str(details.get("title") or "Untitled set"),
        description=str(details.get("description") or ""),
        chapters=tuple(details.get("chapters") or ()),
        url=url,
    )


def read_source(source: str) -> Video:
    """Reads a set from a link or from a text file holding its tracklist.

    Args:
        source: Link of a video, or the path of a text file.

    Returns:
        The video, or the file standing in for one.

    Raises:
        SetlistError: When the source is neither, or could not be read.
    """
    if source.startswith(("http://", "https://")):
        return fetch_video(source)
    path = Path(source)
    if not path.is_file():
        raise SetlistError(f"{source} is neither a link nor a file")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise SetlistError(f"{source} could not be read: {error}") from error
    return Video(title=path.stem, description=text, from_file=True)
