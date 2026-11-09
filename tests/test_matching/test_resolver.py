"""Tests for how the resolver spends requests and what it does with refusals."""

import pytest

from song_automations.matching.matcher import SourceTrack, Tier
from song_automations.matching.resolver import (
    SoundCloudSearcher,
    SpotifySearcher,
    resolve_release,
    search_title,
)


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


class FakeSpotify:
    """Spotify stand-in that counts requests.

    Args:
        albums: Album search results.
        album_bodies: Album bodies by id.
        tracks: Track search results by query.
    """

    def __init__(self, albums=(), album_bodies=None, tracks=None) -> None:
        self.albums, self.album_bodies, self.tracks = list(albums), album_bodies or {}, tracks or {}
        self.calls = []

    def search_raw(self, query, kind="track"):
        """Searches.

        Args:
            query: Query text.
            kind: track or album.

        Returns:
            Results.
        """
        self.calls.append((kind, query))
        return self.albums if kind == "album" else self.tracks.get(query, [])

    def album_raw(self, album_id):
        """Reads an album.

        Args:
            album_id: Album id.

        Returns:
            The album.
        """
        self.calls.append(("get_album", album_id))
        return self.album_bodies[album_id]


def track(id, name, artist="Bicep"):
    """Builds a raw Spotify track.

    Args:
        id: Track id.
        name: Title.
        artist: Artist.

    Returns:
        Raw track.
    """
    return {"id": id, "name": name, "artists": [{"name": artist}], "duration_ms": 200_000, "album": {"name": "x"}}


def test_album_hit_resolves_the_whole_release_in_two_requests():
    """An EP costs two requests when its album exists."""
    client = FakeSpotify(
        albums=[{"id": "a1", "name": "Glue EP", "artists": [{"name": "Bicep"}]}],
        album_bodies={"a1": {"name": "Glue EP", "tracks": {"items": [track("1", "Glue"), track("2", "Opal"), track("3", "Rain")]}}},
    )
    sources = [SourceTrack(("Bicep",), title) for title in ("Glue", "Opal", "Rain")]
    got = resolve_release(SpotifySearcher(client, MemoryCache()), sources, ["Bicep"] * 3, "Bicep", "Glue EP")
    assert [m.candidate.id for m in got] == ["1", "2", "3"]
    assert len(client.calls) == 2


def test_track_search_runs_only_for_what_the_album_lacks():
    """Only the unresolved track spends search requests."""
    query = 'track:"Opal" artist:"Bicep"'
    client = FakeSpotify(
        albums=[{"id": "a1", "name": "Glue EP", "artists": [{"name": "Bicep"}]}],
        album_bodies={"a1": {"name": "Glue EP", "tracks": {"items": [track("1", "Glue")]}}},
        tracks={query: [track("2", "Opal")]},
    )
    sources = [SourceTrack(("Bicep",), "Glue"), SourceTrack(("Bicep",), "Opal")]
    got = resolve_release(SpotifySearcher(client, MemoryCache()), sources, ["Bicep"] * 2, "Bicep", "Glue EP")
    assert got[1].candidate.id == "2"
    assert client.calls == [("album", 'album:"Glue EP" artist:"Bicep"'), ("get_album", "a1"), ("track", query)]


def test_second_run_uses_the_cache():
    """A rerun spends no requests, which is what protects the rate limit."""
    client = FakeSpotify(tracks={'track:"Glue" artist:"Bicep"': [track("1", "Glue")]})
    cache = MemoryCache()
    sources = [SourceTrack(("Bicep",), "Glue")]
    for _ in range(2):
        resolve_release(SpotifySearcher(client, cache), sources, ["Bicep"], "Bicep", "Glue")
    assert len(client.calls) == 2


def test_refused_candidate_is_never_chosen_again():
    """A person's refusal survives any later search."""
    client = FakeSpotify(tracks={'track:"Glue" artist:"Bicep"': [track("bad", "Glue"), track("good", "Glue")]})
    got = resolve_release(
        SpotifySearcher(client, MemoryCache()),
        [SourceTrack(("Bicep",), "Glue")],
        ["Bicep"],
        "Bicep",
        "Glue",
        excluded=[frozenset({"bad"})],
    )
    assert got[0].candidate.id == "good"


@pytest.mark.parametrize(("alias_searches", "expected"), [(2, "1"), (0, None)])
def test_aliases_rescue_a_track_filed_under_another_name(alias_searches, expected):
    """Discogs name variations are searched only when the searcher allows it."""
    query = "The Swingle Singers Vendome"
    client = FakeSpotify(tracks={query: [track("1", "Vendome", artist="The Swingle Singers")]})
    searcher = SpotifySearcher(client, MemoryCache())
    searcher.alias_searches = alias_searches
    got = resolve_release(
        searcher,
        [SourceTrack(("Les Swingle Singers",), "Vendome")],
        ["Les Swingle Singers"],
        "Les Swingle Singers",
        "Jazz Sebastien Bach",
        alias_lookup=lambda index: ("The Swingle Singers",),
    )
    assert (got[0].candidate.id if got[0] else None) == expected


@pytest.mark.parametrize(
    ("searcher_class", "alias_searches"), [(SpotifySearcher, 0), (SoundCloudSearcher, 2)]
)
def test_spotify_spends_no_searches_on_aliases(searcher_class, alias_searches):
    """Spotify skips alias searches to stay under its daily quota."""
    assert searcher_class.alias_searches == alias_searches


def test_spotify_makes_one_query_per_track():
    """Spotify only runs the fielded query, the one that finds almost every match."""
    searcher = SpotifySearcher(FakeSpotify(), MemoryCache())
    assert searcher.track_queries("Bicep", "Glue (Original Mix)") == ['track:"Glue" artist:"Bicep"']


class FakeSoundCloud:
    """SoundCloud stand-in.

    Args:
        sets: Sets returned by search.
        account: Uploads of the exactly named account.
    """

    def __init__(self, sets=(), account=()) -> None:
        self.sets, self.account = list(sets), list(account)
        self.account_calls = 0

    def search_raw(self, query):
        """Searches tracks.

        Args:
            query: Query text.

        Returns:
            Nothing, so only sets and accounts can match.
        """
        return []

    def search_playlists_raw(self, query):
        """Searches sets.

        Args:
            query: Query text.

        Returns:
            Sets.
        """
        return self.sets

    def account_tracks_raw(self, name):
        """Lists an account.

        Args:
            name: Account name.

        Returns:
            Uploads.
        """
        self.account_calls += 1
        return self.account


@pytest.mark.parametrize(
    ("sets", "account"),
    [
        ([{"tracks": [{"id": 1, "title": "BICEP | GLUE", "user": {"username": "BICEP"}, "duration": 300000}]}], []),
        ([], [{"id": 1, "title": "Glue", "user": {"username": "Bicep"}, "duration": 300000}]),
    ],
)
def test_soundcloud_finds_tracks_in_sets_and_accounts(sets, account):
    """Uploads plain search cannot see are still found."""
    got = resolve_release(
        SoundCloudSearcher(FakeSoundCloud(sets, account), MemoryCache()),
        [SourceTrack(("Bicep",), "Glue")],
        ["Bicep"],
        "Bicep",
        "Isles",
    )
    assert got[0].tier == Tier.EXACT


def test_soundcloud_reads_an_account_once_per_run():
    """Twenty releases by one artist read the account once."""
    client = FakeSoundCloud()
    searcher = SoundCloudSearcher(client, MemoryCache())
    for _ in range(3):
        resolve_release(searcher, [SourceTrack(("Bicep",), "Glue")], ["Bicep"], "Bicep", "Isles")
    assert client.account_calls == 1


@pytest.mark.parametrize(
    ("title", "expected"),
    [("Track (Remix)", "Track"), ("Track - Edit", "Track"), ('"Track"', "Track"), ("A = B", "A")],
)
def test_search_title(title, expected):
    """Search boxes get the bare title."""
    assert search_title(title) == expected
