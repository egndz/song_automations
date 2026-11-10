"""Looks up the recording codes of a Discogs track in a catalogue that lists them."""

import logging
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

    def find(self, source: SourceTrack, artist: str) -> dict[str, int | None]:
        """Lists the recording codes of a track with the length of each recording.

        A catalogue that cannot be reached costs coverage, never correctness,
        so the failure is logged once and the sync carries on without codes.
        An error about one track only skips that track.

        Args:
            source: Source track.
            artist: Artist to search for.

        Returns:
            Length in milliseconds by code for the exact version, empty when
            the catalogue lacks it.
        """
        key = f"isrc:{artist}|{source.title}"
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
            logger.warning("No recording code for %s - %s: %s", artist, source.title, error)
            return {}
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
