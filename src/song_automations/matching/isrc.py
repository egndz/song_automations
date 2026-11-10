"""Looks up the recording codes of a Discogs track in a catalogue that lists them."""

import logging
from typing import Protocol

import httpx

from song_automations.clients.deezer import DeezerError
from song_automations.matching.matcher import (
    Candidate,
    Reading,
    SourceTrack,
    Tier,
    decide,
    normalize_isrc,
)
from song_automations.matching.resolver import SearchCache, search_title

MAX_VERSIONS = 3
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

    def find(self, source: SourceTrack, artist: str) -> frozenset[str]:
        """Lists the recording codes of a track.

        A catalogue that cannot be reached costs coverage, never correctness,
        so the failure is logged once and the sync carries on without codes.

        Args:
            source: Source track.
            artist: Artist to search for.

        Returns:
            Codes of the exact version, empty when the catalogue lacks it.
        """
        key = f"isrc:{artist}|{source.title}"
        stored = self._cache.get_search_cache(PLATFORM, key)
        if stored is not None:
            return frozenset(stored)
        if self._failed:
            return frozenset()
        try:
            codes = self._lookup(source, artist)
        except (httpx.HTTPError, DeezerError) as error:
            self._failed = True
            logger.warning("Recording codes are unavailable for this run: %s", error)
            return frozenset()
        self._cache.save_search_cache(PLATFORM, key, codes)
        return frozenset(codes)

    def _lookup(self, source: SourceTrack, artist: str) -> list[str]:
        """Searches the catalogue and reads the codes of the exact versions.

        Args:
            source: Source track.
            artist: Artist to search for.

        Returns:
            Codes, best version first.
        """
        title = search_title(source.title)
        seen: dict[str, Candidate] = {}
        for query in (f'artist:"{artist}" track:"{title}"', f"{artist} {title}"):
            for item in self._client.search_raw(query):
                seen.setdefault(str(item.get("id")), deezer_candidate(item))
        decision = decide(source, seen.values())
        accepted = ([decision.match] if decision.match else []) + decision.alternatives
        exact = [match for match in accepted if match.tier == Tier.EXACT][:MAX_VERSIONS]
        codes = [normalize_isrc(self._client.track_raw(match.candidate.id).get("isrc")) for match in exact]
        return list(dict.fromkeys(code for code in codes if code))
