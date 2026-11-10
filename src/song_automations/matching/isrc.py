"""Looks up the recording codes of a Discogs track in a catalogue that lists them."""

import logging
from dataclasses import replace
from typing import Protocol

import httpx

from song_automations.clients.deezer import DeezerError, DeezerQuotaError
from song_automations.matching.matcher import (
    Candidate,
    Reading,
    SourceTrack,
    Tier,
    decide,
    normalize_isrc,
)
from song_automations.matching.resolver import SearchCache, query_title, unquoted

MAX_VERSIONS = 3
VERSION_NOT_ON_SPOTIFY = ("", "original")
MAX_ERRORS_IN_A_ROW = 3
PLATFORM = "deezer"

logger = logging.getLogger(__name__)


class CatalogueClient(Protocol):
    """Catalogue that can be searched and that lists a recording code per track."""

    def search_raw(self, query: str) -> list[dict]: ...

    def track_raw(self, track_id: str) -> dict: ...


def deezer_candidate(item: dict) -> Candidate:
    """Builds a candidate from a Deezer track object.

    Args:
        item: Deezer track.

    Returns:
        The candidate.
    """
    title = item.get("title") or ""
    artist = (item.get("artist") or {}).get("name") or ""
    seconds = item.get("duration") or 0
    return Candidate(
        id=str(item.get("id")),
        raw_title=title,
        readings=(Reading((artist,), title),),
        duration_ms=seconds * 1000 or None,
        album=(item.get("album") or {}).get("title") or "",
    )


class RecordingStore(Protocol):
    """Stored Spotify matches, read for what they prove about a track."""

    def get_exact_spotify_recording(
        self, discogs_release_id: int, track_position: str, track_name: str
    ) -> tuple[str, int | None] | None: ...

    def get_spotify_outcome(
        self, discogs_release_id: int, track_position: str, track_name: str
    ) -> str | None: ...


def with_recording_evidence(
    source: SourceTrack,
    artist: str,
    destination: str,
    release_id: int,
    position: str,
    store: RecordingStore,
    finder: "IsrcFinder | None",
) -> SourceTrack:
    """Adds what is known about the official release of a track to its source.

    On SoundCloud the recording codes of the track are looked up first, so the
    official upload is recognised whatever its account is called. The code of
    an exact Spotify match counts too, which covers tracks the catalogue
    lacks, and the length the catalogue lists wins when both know the code.
    Fan uploads are allowed only for tracks no catalogue carries, which keeps
    covers out: Spotify must have been asked and have found nothing, or only
    the original of a listed mix. A full Spotify sync takes several runs, so
    until a Spotify decision is stored for the track fan uploads stay off. A sync and the
    review app both prepare their sources here, so the review app offers what
    a sync would accept.

    Args:
        source: Source track built from Discogs.
        artist: Artist name worth typing into a search box.
        destination: Target platform, spotify or soundcloud.
        release_id: Discogs release ID.
        position: Track position on the release.
        store: Stored Spotify matches.
        finder: Looks up recording codes in a catalogue, None to skip that.

    Returns:
        The source with its recording codes and whether fan uploads may match.
    """
    isrcs: dict[str, int | None] = {}
    if destination == "soundcloud":
        carried = store.get_exact_spotify_recording(release_id, position, source.title)
        code = normalize_isrc(carried[0]) if carried else ""
        if code:
            isrcs[code] = carried[1]
        if finder is not None:
            isrcs.update(finder.find(source, artist))
    outcome = store.get_spotify_outcome(release_id, position, source.title)
    return replace(source, isrcs=isrcs, fan_uploads=outcome in VERSION_NOT_ON_SPOTIFY and not isrcs)


class IsrcFinder:
    """Finds the recording codes of the exact version of a track.

    The catalogue entries pass the same gates as any other candidate, and only
    exact versions count, so the codes never belong to another mix.

    Args:
        client: Catalogue client.
        cache: Response cache.
    """

    def __init__(self, client: CatalogueClient, cache: SearchCache) -> None:
        self._client = client
        self._cache = cache
        self._failed = False
        self._errors = 0

    def find(self, source: SourceTrack, artist: str) -> dict[str, int | None]:
        """Lists the recording codes of a track with the length of each recording.

        A catalogue that cannot be reached costs coverage, never correctness,
        so the failure is logged once and the sync carries on without codes.
        An error about one track only skips that track, but several bad
        answers in a row mean the catalogue itself is broken, and the lookups
        stop for the run. A failed lookup is never remembered. An answer is
        remembered per artist, title and credited remixers, so a remix that
        Discogs names only in its credits never gets the codes of the original.

        Args:
            source: Source track.
            artist: Artist to search for.

        Returns:
            Length in milliseconds by code for the exact version, empty when
            the catalogue lacks it.
        """
        key = "|".join([f"isrc:{artist}", source.title, *source.remixers])
        stored = self._cache.get_search_cache(PLATFORM, key)
        if stored is not None:
            return dict(stored)
        if self._failed:
            return {}
        try:
            codes = self._lookup(source, artist)
        except (httpx.HTTPError, DeezerQuotaError) as error:
            self._failed = True
            logger.warning("Recording codes are unavailable for this run: %s", error)
            return {}
        except DeezerError as error:
            self._errors += 1
            logger.warning("No recording code for %s - %s: %s", artist, source.title, error)
            if self._errors >= MAX_ERRORS_IN_A_ROW:
                self._failed = True
                logger.warning(
                    "Recording codes are unavailable for this run: %d bad answers in a row",
                    self._errors,
                )
            return {}
        self._errors = 0
        self._cache.save_search_cache(PLATFORM, key, codes)
        return dict(codes)

    def _lookup(self, source: SourceTrack, artist: str) -> list[list]:
        """Searches the catalogue and reads the codes of the exact versions.

        Args:
            source: Source track.
            artist: Artist to search for.

        Returns:
            Pairs of code and length, best version first. Nothing is searched
            when the artist or the title has no text, since an empty field
            would match any track.
        """
        name, title = unquoted(artist), query_title(source.title)
        if not name or not title:
            return []
        seen: dict[str, Candidate] = {}
        for query in (f'artist:"{name}" track:"{title}"', f"{name} {title}"):
            for item in self._client.search_raw(query):
                seen.setdefault(str(item.get("id")), deezer_candidate(item))
        decision = decide(source, seen.values())
        accepted = ([decision.match] if decision.match else []) + decision.alternatives
        exact = [match for match in accepted if match.tier == Tier.EXACT][:MAX_VERSIONS]
        found: dict[str, int | None] = {}
        for match in exact:
            code = normalize_isrc(self._client.track_raw(match.candidate.id).get("isrc"))
            if code:
                found.setdefault(code, match.candidate.duration_ms)
        return [[code, length] for code, length in found.items()]
