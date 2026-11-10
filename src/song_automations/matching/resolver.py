"""Finds candidates for the tracks of one release, spending as few requests as possible."""

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Protocol

from song_automations.matching.matcher import (
    PLAYABLE,
    Candidate,
    Decision,
    Match,
    SourceTrack,
    Tier,
    artists_agree,
    assign_release,
    clean,
    decide,
    soundcloud_candidate,
    spotify_candidate,
)

MAX_ALIAS_SEARCHES = 2
EVERY_ACCESS_KEY = "@all"
MAX_ALBUMS = 2
MIN_BARE_TITLE_QUERY = 8
PLAYLIST_LIMIT = 20
ALBUM_TITLE_RATIO_WORDS = 1


class SearchCache(Protocol):
    """Stores raw search responses between runs."""

    def get_search_cache(self, platform: str, key: str) -> list | dict | None: ...

    def save_search_cache(self, platform: str, key: str, payload: list | dict) -> None: ...


def unquoted(text: str) -> str:
    """Removes double quotes, which would end a quoted query field early.

    Args:
        text: Artist, title or release title.

    Returns:
        Text without double quotes, with single spaces.
    """
    return " ".join((text or "").replace('"', " ").split())


def search_title(title: str) -> str:
    """Reduces a title to what is worth typing into a search box.

    Args:
        title: Raw title.

    Returns:
        Title without annotations or quotes, empty when the title is nothing
        but annotations.
    """
    stripped = re.sub(r"[(\[][^)\]]*[)\]]", " ", title or "")
    segments = [unquoted(segment) for segment in re.split(r"\s+-\s+|\s+=\s+", stripped)]
    return next((segment for segment in segments if segment), "")


def raw_title(title: str) -> str:
    """Keeps the whole title, annotations included, for a search box.

    Args:
        title: Raw title.

    Returns:
        Title without quotes.
    """
    return unquoted(title)


def query_title(title: str) -> str:
    """Picks the title to search for.

    Args:
        title: Raw title.

    Returns:
        The bare title, or the whole title when it is nothing but annotations,
        such as "(Intro)". Empty only when the title has no text at all.
    """
    return search_title(title) or raw_title(title)


class SpotifySearcher:
    """Searches Spotify, trying the album before single tracks.

    Args:
        client: Client exposing search_raw and album_raw.
        cache: Response cache.
    """

    platform = "spotify"
    alias_searches = 0

    def __init__(self, client, cache: SearchCache) -> None:
        self._client = client
        self._cache = cache

    def _cached(self, key: str, fetch: Callable[[], list | dict]) -> list | dict:
        """Returns a cached response or fetches and stores it.

        Args:
            key: Cache key.
            fetch: Performs the request.

        Returns:
            Raw response.
        """
        stored = self._cache.get_search_cache(self.platform, key)
        if stored is not None:
            return stored
        payload = fetch()
        self._cache.save_search_cache(self.platform, key, payload)
        return payload

    def release_candidates(
        self, artist: str, release_title: str, labels: Sequence[str]
    ) -> list[Candidate]:
        """Lists the tracks of albums that look like the release.

        Args:
            artist: Main release artist, empty for compilations.
            release_title: Release title.
            labels: Release labels, unused on Spotify.

        Returns:
            Candidates from the matching albums.
        """
        name, album_title = unquoted(artist), raw_title(release_title)
        if not name or not album_title:
            return []
        query = f'album:"{album_title}" artist:"{name}"'
        albums = self._cached(
            f"album:{query}", lambda: self._client.search_raw(query, kind="album")
        )
        release_words = set(clean(release_title).split())
        candidates: list[Candidate] = []
        plausible = [
            album
            for album in albums
            if set(clean(album.get("name", "")).split()) & release_words
            and artists_agree([artist], [a.get("name", "") for a in album.get("artists", [])])
        ]
        for album in plausible[:MAX_ALBUMS]:
            body = self._cached(
                f"get_album:{album['id']}", lambda a=album: self._client.album_raw(a["id"])
            )
            items = (body.get("tracks") or {}).get("items", []) if isinstance(body, dict) else []
            candidates += [spotify_candidate(item, body.get("name", "")) for item in items]
        return candidates

    def track_queries(self, artist: str, title: str) -> list[str]:
        """Lists the queries for one track, most precise first.

        Args:
            artist: Track artist.
            title: Track title.

        Returns:
            Queries to try in order, none when the artist or the title has no
            text, since an empty field would match any track.
        """
        name, wanted = unquoted(artist), query_title(title)
        if not name or not wanted:
            return []
        return [f'track:"{wanted}" artist:"{name}"']

    def search(self, query: str) -> list[Candidate]:
        """Runs one track search.

        Args:
            query: Query text.

        Returns:
            Candidates.
        """
        items = self._cached(f"track:{query}", lambda: self._client.search_raw(query, kind="track"))
        return [spotify_candidate(item) for item in items]


class SoundCloudSearcher:
    """Searches SoundCloud through EP sets, accounts and plain search.

    Searches return every track, and tracks the account cannot play in full
    (Go+ releases heard as a clip, or blocked ones) are dropped here unless
    they are allowed, so both views share the same stored responses.

    Args:
        client: Client exposing search_raw, search_playlists_raw and account_tracks_raw.
        cache: Response cache.
        allow_gated: Whether tracks the account cannot play in full may match.
    """

    platform = "soundcloud"
    alias_searches = MAX_ALIAS_SEARCHES

    def __init__(self, client, cache: SearchCache, allow_gated: bool = False) -> None:
        self._client = client
        self._cache = cache
        self._allow_gated = allow_gated
        self._accounts: dict[str, list[Candidate]] = {}

    def including_gated(self) -> "SoundCloudSearcher":
        """Builds the view of this searcher that also offers gated tracks.

        Returns:
            A searcher over the same client and stored responses.
        """
        return SoundCloudSearcher(self._client, self._cache, allow_gated=True)

    def _usable(self, candidates: list[Candidate]) -> list[Candidate]:
        """Drops the tracks the account cannot play in full, unless allowed.

        Args:
            candidates: Candidates of any access level.

        Returns:
            Candidates that may match.
        """
        if self._allow_gated:
            return candidates
        return [candidate for candidate in candidates if candidate.access == PLAYABLE]

    @staticmethod
    def _candidates(items: list | dict) -> list[Candidate]:
        """Builds candidates from raw tracks that name their title and their account.

        A track without an account name would skip the check on uploads by
        strangers, so it is not offered.

        Args:
            items: Raw tracks.

        Returns:
            Candidates.
        """
        return [
            soundcloud_candidate(item)
            for item in items
            if item.get("title") and (item.get("user") or {}).get("username")
        ]

    def _cached(self, key: str, fetch: Callable[[], list | dict]) -> list | dict:
        """Returns a cached response or fetches and stores it.

        Args:
            key: Cache key.
            fetch: Performs the request.

        Returns:
            Raw response.
        """
        key = f"{key}{EVERY_ACCESS_KEY}"
        stored = self._cache.get_search_cache(self.platform, key)
        if stored is not None:
            return stored
        payload = fetch()
        self._cache.save_search_cache(self.platform, key, payload)
        return payload

    def _account(self, name: str) -> list[Candidate]:
        """Lists the uploads of the account with exactly this name.

        Args:
            name: Artist or label name.

        Returns:
            Candidates from the account.
        """
        key = clean(name)
        if key not in self._accounts:
            items = self._cached(f"account:{key}", lambda: self._client.account_tracks_raw(name))
            self._accounts[key] = self._candidates(items)
        return self._usable(self._accounts[key])

    def release_candidates(
        self, artist: str, release_title: str, labels: Sequence[str]
    ) -> list[Candidate]:
        """Collects uploads from sets that look like the release and from accounts.

        Args:
            artist: Main release artist, empty for compilations.
            release_title: Release title.
            labels: Release labels.

        Returns:
            Candidates.
        """
        queries = [f"{artist} {release_title}"] if artist else []
        queries += [f"{label} {release_title}" for label in labels[:1]]
        candidates: list[Candidate] = []
        for query in dict.fromkeys(queries):
            sets = self._cached(f"sets:{query}", lambda q=query: self._client.search_playlists_raw(q))
            for playlist in sets:
                candidates += self._candidates(playlist.get("tracks") or [])
        candidates = self._usable(candidates)
        for name in ([artist] if artist else []) + list(labels[:1]):
            candidates += self._account(name)
        return candidates

    def track_queries(self, artist: str, title: str) -> list[str]:
        """Lists the queries for one track, most precise first.

        Args:
            artist: Track artist.
            title: Track title.

        Returns:
            Queries to try in order, none when the title has no text, since
            an empty query would return unrelated tracks.
        """
        base, raw = query_title(title), raw_title(title)
        if not base:
            return []
        queries = [f"{artist} {base}", f"{artist} {raw}", base]
        return list(dict.fromkeys(query.strip() for query in queries))

    def search(self, query: str) -> list[Candidate]:
        """Runs one track search.

        Args:
            query: Query text.

        Returns:
            Candidates.
        """
        items = self._cached(f"track:{query}", lambda: self._client.search_raw(query))
        return self._usable(self._candidates(items))


def _is_settled(decision: Decision) -> bool:
    """Checks whether a decision cannot be improved by more searching.

    Args:
        decision: Current decision.

    Returns:
        True when an exact version has been accepted.
    """
    return bool(decision.match and decision.match.tier == Tier.EXACT)


def resolve_release(
    searcher,
    sources: Sequence[SourceTrack],
    search_artists: Sequence[str],
    release_artist: str,
    release_title: str,
    labels: Sequence[str] = (),
    excluded: Sequence[frozenset[str]] | None = None,
    alias_lookup: Callable[[int], tuple[str, ...]] | None = None,
) -> list[Match | None]:
    """Matches every track of one release.

    Args:
        searcher: Platform searcher.
        sources: Source tracks in release order.
        search_artists: Artist to search for, per source track.
        release_artist: Main release artist, empty for compilations.
        release_title: Release title.
        labels: Release labels.
        excluded: Candidate ids a person refused, per source track.
        alias_lookup: Returns other names of the artist of the track at an index.

    Returns:
        One entry per source track, None when nothing passed the gates.
    """
    refused = excluded or [frozenset()] * len(sources)
    pool = searcher.release_candidates(release_artist, release_title, labels) if sources else []
    decisions: list[Decision] = []
    for index, source in enumerate(sources):
        blocked = refused[index]
        seen = [c for c in pool if c.id not in blocked]
        decision = decide(source, seen)
        if not _is_settled(decision):
            for query in searcher.track_queries(search_artists[index], source.title):
                seen += [c for c in searcher.search(query) if c.id not in blocked]
                decision = decide(source, seen)
                if _is_settled(decision):
                    break
        if decision.match is None and alias_lookup is not None:
            names = alias_lookup(index)
            if names:
                source = replace(source, aliases=tuple(names))
                decision = decide(source, seen)
                wanted = query_title(source.title)
                if decision.match is None and wanted:
                    for name in names[: searcher.alias_searches]:
                        query = f"{name} {wanted}"
                        seen += [c for c in searcher.search(query) if c.id not in blocked]
                    decision = decide(source, seen)
        decisions.append(decision)
    return assign_release(decisions)
