"""End-to-end engine tests with fake platforms and a real state database."""

from types import SimpleNamespace

import pytest
from rich.console import Console

from song_automations.clients.discogs import DiscogsClient, Folder, Release, Track
from song_automations.config import Settings
from song_automations.errors import RateLimitedError
from song_automations.state.tracker import StateTracker
from song_automations.sync.engine import SyncEngine


class FakeDiscogs:
    """Discogs stand-in with one folder per entry.

    Args:
        folders: Mapping of folder name to tracks.
    """

    def __init__(self, folders: dict[str, list[Track]]) -> None:
        self.by_name = folders
        self.ids = {name: index + 1 for index, name in enumerate(folders)}

    def get_folders(self):
        """Lists folders.

        Returns:
            Folders.
        """
        return [Folder(id=i, name=n, count=1) for n, i in self.ids.items()]

    def get_folder_releases(self, folder_id):
        """Lists releases of a folder, one per distinct release id.

        Args:
            folder_id: Folder id.

        Returns:
            Releases.
        """
        name = next(n for n, i in self.ids.items() if i == folder_id)
        seen = {t.release_id: t for t in self.by_name[name]}
        return [
            Release(id=r, title=t.release_title, artist=t.artist, year=2000, folder_id=folder_id, folder_name=name)
            for r, t in seen.items()
        ]

    def get_wantlist_releases(self):
        """Lists the wantlist.

        Returns:
            Nothing.
        """
        return []

    def get_release_tracks(self, release_id):
        """Lists tracks.

        Args:
            release_id: Release id.

        Returns:
            Tracks.
        """
        return [t for tracks in self.by_name.values() for t in tracks if t.release_id == release_id]

    def get_artist_names(self, artist_id):
        """Lists aliases.

        Args:
            artist_id: Artist id.

        Returns:
            Nothing.
        """
        return ()


class FakeSpotify:
    """Spotify stand-in holding playlists in memory.

    Args:
        playlists: Mapping of playlist name to track ids.
        catalogue: Track search results by query.
        error: Raised on any search when set.
    """

    def __init__(self, playlists, catalogue, error=None) -> None:
        self.playlists, self.catalogue, self.error = playlists, catalogue, error
        self.deleted = []

    def search_raw(self, query, kind="track"):
        """Searches.

        Args:
            query: Query.
            kind: track or album.

        Returns:
            Results.
        """
        if self.error:
            raise self.error
        return [] if kind == "album" else self.catalogue.get(query, [])

    def album_raw(self, album_id):
        """Reads an album.

        Args:
            album_id: Album id.

        Returns:
            Nothing.
        """
        return {}

    def find_playlist_by_name(self, name):
        """Finds a playlist.

        Args:
            name: Name.

        Returns:
            The playlist, or None.
        """
        return SimpleNamespace(id=name, name=name) if name in self.playlists else None

    def create_playlist(self, name, description="", public=True):
        """Creates a playlist.

        Args:
            name: Name.
            description: Description.
            public: Visibility.

        Returns:
            The playlist.
        """
        self.playlists[name] = []
        return SimpleNamespace(id=name, name=name)

    def delete_playlist(self, playlist_id):
        """Deletes a playlist.

        Args:
            playlist_id: Id.
        """
        self.deleted.append(playlist_id)
        self.playlists.pop(playlist_id, None)

    def get_playlist_tracks(self, playlist_id):
        """Lists tracks.

        Args:
            playlist_id: Id.

        Returns:
            Tracks.
        """
        return [SimpleNamespace(id=t) for t in self.playlists[playlist_id]]

    def add_tracks_to_playlist(self, playlist_id, uris):
        """Adds tracks.

        Args:
            playlist_id: Id.
            uris: Track uris.
        """
        self.playlists[playlist_id] += [u.rsplit(":", 1)[1] for u in uris]

    def remove_tracks_from_playlist(self, playlist_id, uris):
        """Removes every occurrence of tracks.

        Args:
            playlist_id: Id.
            uris: Track uris.
        """
        gone = {u.rsplit(":", 1)[1] for u in uris}
        self.playlists[playlist_id] = [t for t in self.playlists[playlist_id] if t not in gone]


def discogs_track(title="Glue", position="A1", release_id=1, artist="Bicep") -> Track:
    """Builds a Discogs track.

    Args:
        title: Title.
        position: Position.
        release_id: Release id.
        artist: Artist.

    Returns:
        The track.
    """
    return Track(position, title, artist, "", release_id, "Release", artist_names=(artist,), release_artist=artist)


def item(id, name, artist="Bicep"):
    """Builds a raw Spotify track.

    Args:
        id: Id.
        name: Title.
        artist: Artist.

    Returns:
        Raw track.
    """
    return {"id": id, "name": name, "artists": [{"name": artist}], "duration_ms": 200000, "album": {"name": "x"}}


@pytest.fixture
def state(tmp_path):
    """Provides a real state database.

    Args:
        tmp_path: Temporary folder.

    Returns:
        The tracker.
    """
    return StateTracker(tmp_path / "state.db")


def run(state, discogs, spotify, folder_names=None, dry_run=False):
    """Runs a Spotify sync.

    Args:
        state: Tracker.
        discogs: Discogs fake.
        spotify: Spotify fake.
        folder_names: Folder filter.
        dry_run: Dry run flag.

    Returns:
        The result.
    """
    engine = SyncEngine(Settings(_env_file=None), discogs, state, Console(quiet=True))
    return engine.sync_to_spotify(spotify, include_wantlist=False, folder_names=folder_names, dry_run=dry_run)


def map_folder(state, discogs, name):
    """Records that a folder already has a playlist.

    Args:
        state: Tracker.
        discogs: Discogs fake.
        name: Folder name.
    """
    state.save_folder_mapping(discogs.ids[name], name, "spotify", f"Discogs - {name}", f"Discogs - {name}")


QUERY = 'track:"Glue" artist:"Bicep"'


def test_rebuild_removes_what_the_matcher_did_not_choose(state):
    """Wrong and hand-added tracks go, the chosen track arrives."""
    discogs = FakeDiscogs({"Electro": [discogs_track()]})
    map_folder(state, discogs, "Electro")
    spotify = FakeSpotify({"Discogs - Electro": ["wrong", "hand_added"]}, {QUERY: [item("right", "Glue")]})
    result = run(state, discogs, spotify)
    assert spotify.playlists["Discogs - Electro"] == ["right"]
    assert (result.tracks_added, result.tracks_removed) == (1, 2)


def test_existing_duplicates_are_collapsed(state):
    """A track already in the playlist twice ends up there once."""
    discogs = FakeDiscogs({"Electro": [discogs_track()]})
    map_folder(state, discogs, "Electro")
    spotify = FakeSpotify({"Discogs - Electro": ["right", "right"]}, {QUERY: [item("right", "Glue")]})
    run(state, discogs, spotify)
    assert spotify.playlists["Discogs - Electro"] == ["right"]


def test_same_track_on_two_releases_is_added_once(state):
    """A reissue in the same folder does not add a duplicate."""
    discogs = FakeDiscogs({"Electro": [discogs_track(release_id=1), discogs_track(release_id=2)]})
    map_folder(state, discogs, "Electro")
    spotify = FakeSpotify({"Discogs - Electro": []}, {QUERY: [item("right", "Glue")]})
    run(state, discogs, spotify)
    assert spotify.playlists["Discogs - Electro"] == ["right"]


def test_folder_filter_never_deletes_other_playlists(state):
    """Syncing one folder leaves every other playlist in place."""
    discogs = FakeDiscogs({"Electro": [discogs_track()], "House": [discogs_track(release_id=2)]})
    map_folder(state, discogs, "Electro")
    map_folder(state, discogs, "House")
    spotify = FakeSpotify({"Discogs - Electro": [], "Discogs - House": ["kept"]}, {QUERY: [item("right", "Glue")]})
    result = run(state, discogs, spotify, folder_names=["Electro"])
    assert spotify.deleted == []
    assert result.playlists_deleted == 0
    assert spotify.playlists["Discogs - House"] == ["kept"]


def test_vanished_folder_still_deletes_its_playlist(state):
    """A folder really gone from Discogs takes its playlist with it."""
    discogs = FakeDiscogs({"Electro": [discogs_track()]})
    map_folder(state, discogs, "Electro")
    state.save_folder_mapping(99, "Gone", "spotify", "Discogs - Gone", "Discogs - Gone")
    spotify = FakeSpotify({"Discogs - Electro": [], "Discogs - Gone": []}, {QUERY: [item("right", "Glue")]})
    run(state, discogs, spotify)
    assert spotify.deleted == ["Discogs - Gone"]


def test_rate_limit_stops_before_any_playlist_changes(state):
    """A refused search must not look like a missing song."""
    discogs = FakeDiscogs({"Electro": [discogs_track()]})
    map_folder(state, discogs, "Electro")
    spotify = FakeSpotify({"Discogs - Electro": ["existing"]}, {}, error=RateLimitedError("Spotify", 80000))
    with pytest.raises(RateLimitedError):
        run(state, discogs, spotify)
    assert spotify.playlists["Discogs - Electro"] == ["existing"]
    assert state.get_cached_match(1, "A1", "spotify") is None


def test_nothing_matched_leaves_the_playlist_alone(state):
    """An empty outcome is treated as suspicious rather than emptying a playlist."""
    discogs = FakeDiscogs({"Electro": [discogs_track()]})
    map_folder(state, discogs, "Electro")
    spotify = FakeSpotify({"Discogs - Electro": ["existing"]}, {})
    result = run(state, discogs, spotify)
    assert spotify.playlists["Discogs - Electro"] == ["existing"]
    assert result.tracks_missing == 1


def test_dry_run_reports_without_changing(state):
    """A dry run counts the changes and makes none."""
    discogs = FakeDiscogs({"Electro": [discogs_track()]})
    map_folder(state, discogs, "Electro")
    spotify = FakeSpotify({"Discogs - Electro": ["wrong"]}, {QUERY: [item("right", "Glue")]})
    result = run(state, discogs, spotify, dry_run=True)
    assert spotify.playlists["Discogs - Electro"] == ["wrong"]
    assert (result.tracks_added, result.tracks_removed) == (1, 1)


def test_wrong_artist_is_reported_missing_not_added(state):
    """The example that started this work ends as a miss."""
    discogs = FakeDiscogs({"Electro": [discogs_track("The Fear", artist="Eddy Love")]})
    map_folder(state, discogs, "Electro")
    catalogue = {'track:"The Fear" artist:"Eddy Love"': [item("boc", "The Fear", artist="Blue Oyster Cult")]}
    spotify = FakeSpotify({"Discogs - Electro": []}, catalogue)
    result = run(state, discogs, spotify)
    assert spotify.playlists["Discogs - Electro"] == []
    assert result.tracks_missing == 1


def test_stored_decision_is_reused_without_searching(state):
    """A second run does not search for tracks already decided."""
    discogs = FakeDiscogs({"Electro": [discogs_track()]})
    map_folder(state, discogs, "Electro")
    spotify = FakeSpotify({"Discogs - Electro": []}, {QUERY: [item("right", "Glue")]})
    run(state, discogs, spotify)
    spotify.error = RateLimitedError("Spotify", 80000)
    run(state, discogs, spotify)
    assert spotify.playlists["Discogs - Electro"] == ["right"]


def test_wantlist_constant_is_protected_from_cleanup(state):
    """Excluding the wantlist does not delete its playlist."""
    discogs = FakeDiscogs({"Electro": [discogs_track()]})
    map_folder(state, discogs, "Electro")
    state.save_folder_mapping(DiscogsClient.WANTLIST_FOLDER_ID, "Wantlist", "spotify", "W", "W")
    spotify = FakeSpotify({"Discogs - Electro": [], "W": ["kept"]}, {QUERY: [item("right", "Glue")]})
    run(state, discogs, spotify)
    assert spotify.playlists["W"] == ["kept"]


class FakeFinder:
    """Recording code lookup stand-in.

    Args:
        codes: Codes every track gets.
    """

    def __init__(self, codes=()) -> None:
        self.codes = frozenset(codes)
        self.asked = []

    def find(self, source, artist):
        """Looks up a track.

        Args:
            source: Source track.
            artist: Artist searched for.

        Returns:
            The codes.
        """
        self.asked.append((artist, source.title))
        return self.codes


@pytest.mark.parametrize(
    ("destination", "codes", "on_spotify", "isrcs", "fan_uploads", "lookups"),
    [
        ("soundcloud", {"GBCFB1700231"}, False, {"GBCFB1700231"}, False, 1),
        ("soundcloud", set(), False, set(), True, 1),
        ("soundcloud", set(), True, set(), False, 1),
        ("spotify", {"GBCFB1700231"}, False, set(), True, 0),
    ],
)
def test_released_tracks_refuse_fan_uploads(state, destination, codes, on_spotify, isrcs, fan_uploads, lookups):
    """A track that a catalogue or Spotify carries takes only verified uploads, and only SoundCloud looks codes up."""
    track = discogs_track()
    if on_spotify:
        state.save_matched_track(1, "A1", "Bicep", "Glue", "spotify", "sp1", 0.9, match_tier="exact")
    finder = FakeFinder(codes)
    engine = SyncEngine(Settings(_env_file=None), FakeDiscogs({}), state, Console(quiet=True), isrc_finder=finder)
    source = engine._to_source(track, Release(1, "Release", "Bicep", 2017, 1, "Folder"), destination)
    assert (source.isrcs, source.fan_uploads, len(finder.asked)) == (frozenset(isrcs), fan_uploads, lookups)
