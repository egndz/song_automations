"""Matches the tracks of a set and puts them in a playlist."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from song_automations.matching.matcher import (
    ARTIST_SEPARATOR,
    Candidate,
    Match,
    SourceTrack,
    is_repeat,
    parse_title,
    split_artists,
)
from song_automations.matching.resolver import resolve_track
from song_automations.setlist.tracklist import SetTrack

ORDER_SAMPLE = 3
SAME_COPY_TOLERANCE_MS = 2_000
MIXED_CUT = re.compile(r"[-(\[]\s*mixed\s*[)\]]?\s*$", re.IGNORECASE)


@dataclass
class SetlistRow:
    """What happened to one track of the set.

    Attributes:
        track: Track as the tracklist names it.
        match: Chosen platform track, None when nothing passed the gates.
        repeat: Whether the recording is in the playlist through an earlier track.
    """

    track: SetTrack
    match: Match | None = None
    repeat: bool = False


@dataclass
class SetlistResult:
    """What a run did.

    Attributes:
        playlist_name: Name of the playlist.
        rows: One row per track of the set, in set order.
        created: Whether the playlist is new.
        added: Tracks added, or that a dry run would add.
        present: Chosen tracks the playlist already held.
        removed: Tracks a replace took out, or that a dry run would take out.
    """

    playlist_name: str
    rows: list[SetlistRow] = field(default_factory=list)
    created: bool = False
    added: int = 0
    present: int = 0
    removed: int = 0


def is_mixed_cut(match: Match) -> bool:
    """Checks whether a match is the cut of a track from a mixed compilation.

    Args:
        match: Accepted candidate.

    Returns:
        True when the platform marks the track as mixed.
    """
    return bool(MIXED_CUT.search(match.candidate.raw_title))


def _find(searcher, artist: str, title: str, album: str = "") -> Match | None:
    """Looks one track up.

    A set gives no length, so the version, the title and the artist carry the
    decision, and of several accepted copies the one whose title is closest
    wins. Uploads by strangers never count.

    Args:
        searcher: Platform searcher.
        artist: Artist credit.
        title: Track title.
        album: Release the track is on, which is preferred when known.

    Returns:
        The chosen match, or None.
    """
    source = SourceTrack(
        artists=split_artists(artist), title=title, release_title=album, fan_uploads=False
    )
    first = ARTIST_SEPARATOR.split(artist)[0].strip()
    return resolve_track(searcher, source, first or artist).match


def match_tracks(
    searcher, tracks: Sequence[SetTrack], order_known: bool = False
) -> list[Match | None]:
    """Matches the tracks of a set.

    Most tracklists read artist then title, a few the other way round. The
    first tracks that find nothing as written are tried turned round, and the
    order that finds more is used for the rest, so a set costs at most a few
    searches more than it has tracks.

    Args:
        searcher: Platform searcher.
        tracks: Tracks in set order.
        order_known: Whether the source names the artist and the title itself,
            so nothing is tried turned round.

    Returns:
        One entry per track, None when nothing passed the gates.
    """
    if order_known:
        return [_find(searcher, track.artist, track.title, track.album) for track in tracks]
    sample = range(min(ORDER_SAMPLE, len(tracks)))
    written = {i: _find(searcher, tracks[i].artist, tracks[i].title) for i in sample}
    turned = {
        i: _find(searcher, tracks[i].title, tracks[i].artist) for i in sample if written[i] is None
    }
    title_first = sum(m is not None for m in turned.values()) > sum(
        m is not None for m in written.values()
    )
    matches: list[Match | None] = []
    for index, track in enumerate(tracks):
        if index in written:
            matches.append(written[index] or turned.get(index))
        elif title_first:
            matches.append(_find(searcher, track.title, track.artist))
        else:
            matches.append(_find(searcher, track.artist, track.title))
    return matches


def _is_held(candidate: Candidate, held: Sequence) -> bool:
    """Checks whether a playlist already holds a chosen track.

    A platform lists one recording under several ids, and a later run can pick
    another of them. A held track with the same title and the same length is
    the same recording, whether or not one of them says "Original Mix".

    Args:
        candidate: Chosen platform track.
        held: Tracks the playlist holds, each with an id, a name and a length.

    Returns:
        True when the playlist holds the track or another copy of it.
    """
    bases, versions = parse_title(candidate.raw_title)
    for track in held:
        if str(track.id) == candidate.id:
            return True
        length = getattr(track, "duration_ms", None)
        same_length = (
            bool(length)
            and bool(candidate.duration_ms)
            and abs(length - candidate.duration_ms) <= SAME_COPY_TOLERANCE_MS
        )
        held_bases, held_versions = parse_title(getattr(track, "name", "") or "")
        if same_length and held_bases[:1] == bases[:1] and sorted(held_versions) == sorted(versions):
            return True
    return False


def sync_setlist(
    client,
    searcher,
    tracks: Sequence[SetTrack],
    playlist_name: str,
    description: str = "",
    dry_run: bool = False,
    order_known: bool = False,
    replace: bool = False,
    snapshots=None,
) -> SetlistResult:
    """Puts the tracks of a set in a playlist, each recording once.

    Tracks are only ever added unless a replace is asked for. A playlist that
    exists keeps what it holds, so running a set again adds what was not found
    the first time. A replace makes the playlist hold exactly the set, in set
    order, after saving what it held. It never empties a playlist.

    Args:
        client: Platform client.
        searcher: Platform searcher.
        tracks: Tracks in set order.
        playlist_name: Name of the playlist.
        description: Description for a new playlist.
        dry_run: If True, nothing is created or added.
        order_known: Whether the source names the artist and the title itself.
        replace: Whether an existing playlist is made to hold exactly the set.
        snapshots: Store that saves what a playlist held before a replace.

    Returns:
        What was matched and added.

    Raises:
        SnapshotError: When a replace could not save the playlist first. The
            playlist is then left unchanged.
    """
    result = SetlistResult(playlist_name)
    desired: list[str] = []
    chosen: dict[str, list[int | None]] = {}
    for track, match in zip(tracks, match_tracks(searcher, tracks, order_known), strict=True):
        repeat = False
        if match:
            candidate = match.candidate
            repeat = candidate.id in desired or is_repeat(candidate.isrc, candidate.duration_ms, chosen)
            if not repeat:
                desired.append(candidate.id)
                if candidate.isrc:
                    chosen.setdefault(candidate.isrc, []).append(candidate.duration_ms)
        result.rows.append(SetlistRow(track, match, repeat))
    playlist = client.find_playlist_by_name(playlist_name)
    held = list(client.get_playlist_tracks(playlist.id)) if playlist else []
    chosen_copies = {row.match.candidate.id: row.match.candidate for row in result.rows if row.match}
    to_add = [track_id for track_id in desired if not _is_held(chosen_copies[track_id], held)]
    result.created = playlist is None and bool(desired)
    held_ids = [str(track.id) for track in held]
    if replace and playlist is not None and desired:
        result.added = len([track_id for track_id in desired if track_id not in held_ids])
        result.removed = len([track_id for track_id in held_ids if track_id not in desired])
        result.present = len(desired) - result.added
        if dry_run or held_ids == desired:
            return result
        if snapshots is not None:
            snapshots.save("spotify", playlist, held)
        uri = "spotify:track:{}".format
        client.remove_tracks_from_playlist(playlist.id, [uri(t) for t in dict.fromkeys(held_ids)])
        client.add_tracks_to_playlist(playlist.id, [uri(t) for t in desired])
        return result
    result.added = len(to_add)
    result.present = len(desired) - len(to_add)
    if dry_run or not to_add:
        return result
    if playlist is None:
        playlist = client.create_playlist(name=playlist_name, description=description, public=False)
    client.add_tracks_to_playlist(playlist.id, [f"spotify:track:{track_id}" for track_id in to_add])
    return result
