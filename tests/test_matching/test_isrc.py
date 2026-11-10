"""Tests for looking up recording codes in the Deezer catalogue."""

import httpx
import pytest

from song_automations.clients.deezer import DeezerError, DeezerQuotaError
from song_automations.matching.isrc import IsrcFinder, deezer_candidate
from song_automations.matching.matcher import SourceTrack


class MemoryCache:
    """In-memory stand-in for the search cache."""

    def __init__(self) -> None:
        self.data = {}

    def get_search_cache(self, platform, key):
        """Reads an entry.

        Args:
            platform: Platform name.
            key: Query key.

        Returns:
            The stored payload, or None.
        """
        return self.data.get((platform, key))

    def save_search_cache(self, platform, key, payload):
        """Writes an entry.

        Args:
            platform: Platform name.
            key: Query key.
            payload: Response.
        """
        self.data[(platform, key)] = payload


class FakeCatalogue:
    """Catalogue stand-in that counts requests.

    Args:
        results: Tracks every search returns.
        codes: Recording code by track id.
        error: Raised by every request when set.
    """

    def __init__(self, results=(), codes=None, error=None) -> None:
        self.results, self.codes, self.error = list(results), codes or {}, error
        self.calls = 0

    def search_raw(self, query):
        """Searches.

        Args:
            query: Query text.

        Returns:
            Tracks.
        """
        self.calls += 1
        if self.error:
            raise self.error
        return self.results

    def track_raw(self, track_id):
        """Reads a track.

        Args:
            track_id: Track id.

        Returns:
            Track with its code.
        """
        self.calls += 1
        return {"id": track_id, "isrc": self.codes.get(track_id)}


def entry(id, title, artist="Bicep", seconds=300):
    """Builds a raw Deezer track.

    Args:
        id: Track id.
        title: Title.
        artist: Artist.
        seconds: Length.

    Returns:
        Raw track.
    """
    return {"id": id, "title": title, "artist": {"name": artist}, "duration": seconds, "album": {"title": "Isles"}}


@pytest.mark.parametrize(
    ("title", "results", "codes", "expected"),
    [
        ("Glue", [entry(1, "Glue")], {"1": "gb-cfb-17-00231"}, {"GBCFB1700231": 300_000}),
        ("Glue", [entry(1, "Glue"), entry(2, "Glue", seconds=301)], {"1": "A1", "2": "B2"}, {"A1": 300_000, "B2": 301_000}),
        ("Glue (Hardfloor Remix)", [entry(1, "Glue")], {"1": "A1"}, {}),
        ("Glue", [entry(1, "Glue (Hardfloor Remix)")], {"1": "A1"}, {}),
        ("Glue", [entry(1, "Glue", artist="Bicep Tribute Band")], {"1": "A1"}, {}),
        ("Glue", [entry(1, "Glue")], {}, {}),
        ("Glue", [], {}, {}),
    ],
)
def test_only_the_exact_version_gives_its_code(title, results, codes, expected):
    """Another mix, another artist or a missing code yields nothing."""
    finder = IsrcFinder(FakeCatalogue(results, codes), MemoryCache())
    assert finder.find(SourceTrack(("Bicep",), title), "Bicep") == expected


@pytest.mark.parametrize(("results", "codes"), [([entry(1, "Glue")], {"1": "A1"}), ([], {})])
def test_answer_is_remembered_found_or_not(results, codes):
    """A second lookup of the same track costs no request."""
    catalogue, cache = FakeCatalogue(results, codes), MemoryCache()
    first = IsrcFinder(catalogue, cache).find(SourceTrack(("Bicep",), "Glue"), "Bicep")
    spent = catalogue.calls
    assert IsrcFinder(catalogue, cache).find(SourceTrack(("Bicep",), "Glue"), "Bicep") == first
    assert catalogue.calls == spent


@pytest.mark.parametrize("error", [httpx.ConnectError("down"), DeezerQuotaError("quota limit exceeded")])
def test_unreachable_catalogue_costs_coverage_not_the_sync(error):
    """The lookup gives up for the run, returns no codes and remembers nothing."""
    catalogue, cache = FakeCatalogue(error=error), MemoryCache()
    finder = IsrcFinder(catalogue, cache)
    assert finder.find(SourceTrack(("Bicep",), "Glue"), "Bicep") == {}
    assert finder.find(SourceTrack(("Bicep",), "Opal"), "Bicep") == {}
    assert catalogue.calls == 1
    assert cache.data == {}


@pytest.mark.parametrize(("seconds", "expected"), [(300, 300_000), (0, None), (None, None)])
def test_deezer_candidate_reads_the_track(seconds, expected):
    """Deezer lengths are in seconds and a missing one stays unknown."""
    candidate = deezer_candidate(entry(7, "Glue", seconds=seconds))
    assert (candidate.id, candidate.raw_title, candidate.album, candidate.duration_ms) == ("7", "Glue", "Isles", expected)
    assert candidate.readings[0].artists == ("Bicep",)


@pytest.mark.parametrize("error", [DeezerError("no data"), DeezerError("answer is not JSON")])
def test_error_about_one_track_only_skips_that_track(error):
    """A bad answer for one track is not remembered and does not switch the lookups off."""
    catalogue, cache = FakeCatalogue(error=error), MemoryCache()
    finder = IsrcFinder(catalogue, cache)
    assert finder.find(SourceTrack(("Bicep",), "Glue"), "Bicep") == {}
    assert finder.find(SourceTrack(("Bicep",), "Opal"), "Bicep") == {}
    assert catalogue.calls == 2
    assert cache.data == {}
