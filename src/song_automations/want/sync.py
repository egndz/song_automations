"""Adds the vinyl of every track in the Want playlist to the Discogs wantlist."""

from dataclasses import dataclass, field

from song_automations.matching.matcher import (
    Candidate,
    Match,
    Reading,
    SourceTrack,
    Tier,
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


def release_tier(discogs, release_id: int, reading: Reading) -> Tier | None:
    """Finds how closely the best track of a release agrees with the wanted track.

    Args:
        discogs: Discogs client.
        release_id: Release to read.
        reading: Who and what the track is.

    Returns:
        Best tier among the tracks that pass the matching gates, or None.
    """
    source = SourceTrack(artists=reading.artists, title=reading.title)
    best: Tier | None = None
    for track in discogs.get_release_tracks(release_id):
        names = track.artist_names or (track.artist,)
        candidate = Candidate(
            id=track.position,
            raw_title=track.title,
            readings=(Reading(names, track.title),),
        )
        decision = evaluate(source, candidate)
        if isinstance(decision, Match) and (best is None or decision.tier > best):
            best = decision.tier
    return best


def find_pressing(discogs, readings: list[Reading]) -> int | None:
    """Finds the earliest vinyl pressing that verifiably holds a track.

    A pressing with the exact version wins over an earlier one that only holds
    another mix, so wanting a remix never lands on the original single.

    Args:
        discogs: Discogs client.
        readings: Who and what the track could be.

    Returns:
        Discogs release id, or None.
    """
    fallback: tuple[Tier, int] | None = None
    for reading in readings:
        artist = reading.artists[0] if reading.artists else ""
        results = discogs.search_vinyl(artist, reading.title)
        results.sort(key=lambda r: (int(r.get("year") or UNKNOWN_YEAR), r["id"]))
        for result in results[:MAX_RELEASES_CHECKED]:
            tier = release_tier(discogs, result["id"], reading)
            if tier == Tier.EXACT:
                return result["id"]
            if tier is not None and (fallback is None or tier > fallback[0]):
                fallback = (tier, result["id"])
    return fallback[1] if fallback else None


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
