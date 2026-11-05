"""Adds the vinyl of every track in the Want playlist to the Discogs wantlist."""

from dataclasses import dataclass, field

from song_automations.matching.matcher import (
    Candidate,
    Match,
    Reading,
    SourceTrack,
    evaluate,
    soundcloud_readings,
)

MAX_READINGS = 3
MAX_RELEASES_CHECKED = 8
UNKNOWN_YEAR = 9999
ARTIST_READINGS = ("metadata", "dash", "dash_reversed", "quoted", "colon", "by", "uploader")


@dataclass
class WantResult:
    """What a run did.

    Attributes:
        added: Labels of the tracks whose pressing was added.
        already_wanted: Labels of the tracks whose pressing was on the wantlist.
        not_found: Labels of the tracks with no verified vinyl.
    """

    added: list[str] = field(default_factory=list)
    already_wanted: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)


def track_readings(track) -> list[Reading]:
    """Lists who and what a playlist track could be.

    Args:
        track: A SpotifyTrack or a SoundCloudTrack.

    Returns:
        Readings, most reliable first.
    """
    if hasattr(track, "artists"):
        return [Reading(tuple(track.artists), track.name)]
    readings = soundcloud_readings(track.title, track.artist)
    order = {kind: index for index, kind in enumerate(ARTIST_READINGS)}
    usable = [r for r in readings if r.kind in order]
    return sorted(usable, key=lambda r: order[r.kind])[:MAX_READINGS]


def track_label(track) -> str:
    """Formats a playlist track for messages.

    Args:
        track: A SpotifyTrack or a SoundCloudTrack.

    Returns:
        Readable label.
    """
    if hasattr(track, "artists"):
        return f"{', '.join(track.artists)} - {track.name}"
    return f"{track.artist} | {track.title}"


def release_has_track(discogs, release_id: int, reading: Reading) -> bool:
    """Checks that a release really contains the track, whatever the mix.

    Args:
        discogs: Discogs client.
        release_id: Release to read.
        reading: Who and what the track is.

    Returns:
        True when any track of the release passes the matching gates.
    """
    source = SourceTrack(artists=reading.artists, title=reading.title)
    for track in discogs.get_release_tracks(release_id):
        names = track.artist_names or (track.artist,)
        candidate = Candidate(
            id=track.position,
            raw_title=track.title,
            readings=(Reading(names, track.title),),
        )
        if isinstance(evaluate(source, candidate), Match):
            return True
    return False


def find_pressing(discogs, readings: list[Reading]) -> int | None:
    """Finds the earliest vinyl pressing that verifiably holds a track.

    Args:
        discogs: Discogs client.
        readings: Who and what the track could be.

    Returns:
        Discogs release id, or None.
    """
    for reading in readings:
        artist = reading.artists[0] if reading.artists else ""
        results = discogs.search_vinyl(artist, reading.title)
        results.sort(key=lambda r: (int(r.get("year") or UNKNOWN_YEAR), r["id"]))
        for result in results[:MAX_RELEASES_CHECKED]:
            if release_has_track(discogs, result["id"], reading):
                return result["id"]
    return None


def sync_want_playlist(client, destination: str, discogs, state, playlist_name: str, dry_run: bool = False) -> WantResult:
    """Processes the tracks that are new in the Want playlist.

    The playlist is only read, and a handled track is never handled again, so
    removing a record from the wantlist later will not bring it back.

    Args:
        client: Spotify or SoundCloud client.
        destination: Platform name.
        discogs: Discogs client.
        state: State tracker.
        playlist_name: Name of the Want playlist.
        dry_run: If True, change nothing.

    Returns:
        What happened to each new track.
    """
    result = WantResult()
    playlist = client.find_playlist_by_name(playlist_name)
    if playlist is None:
        if not dry_run:
            client.create_playlist(
                name=playlist_name,
                description="Add a track here to put its vinyl on your Discogs wantlist.",
                public=False,
            )
        return result
    handled = state.get_handled_wants(destination)
    new_tracks = [t for t in client.get_playlist_tracks(playlist.id) if str(t.id) not in handled]
    if not new_tracks:
        return result
    wanted = discogs.get_wantlist_ids()
    for track in new_tracks:
        label = track_label(track)
        release_id = find_pressing(discogs, track_readings(track))
        if release_id is None:
            result.not_found.append(label)
            status = "not_found"
        elif release_id in wanted:
            result.already_wanted.append(label)
            status = "already_wanted"
        else:
            result.added.append(label)
            status = "added"
            if not dry_run:
                discogs.add_to_wantlist(release_id)
            wanted.add(release_id)
        if not dry_run and status != "not_found":
            state.save_handled_want(destination, str(track.id), release_id, status, label)
    return result
