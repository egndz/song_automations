"""Tests for the Want playlist."""

from types import SimpleNamespace

import pytest

from song_automations.clients.discogs import Track
from song_automations.clients.soundcloud import SoundCloudTrack
from song_automations.clients.spotify import SpotifyTrack
from song_automations.matching.matcher import Reading
from song_automations.state.tracker import StateTracker
from song_automations.want.sync import find_pressing, sync_want_playlist, track_readings


def spotify_track(id="t1", name="Glue", artist="Bicep"):
    """Builds a Spotify track.

    Args:
        id: Track id.
        name: Title.
        artist: Artist.

    Returns:
        The track.
    """
    return SpotifyTrack(id, "u", name, artist, [artist], "a", 0, 1000, True)


class FakeDiscogs:
    """Discogs stand-in.

    Args:
        results: Search results by artist.
        tracks: Tracklists by release id.
        wanted: Ids already on the wantlist.
    """

    def __init__(self, results, tracks, wanted=()):
        self.results, self.tracks, self.wanted = results, tracks, set(wanted)
        self.added = []

    def search_vinyl(self, artist, title):
        """Searches.

        Args:
            artist: Artist.
            title: Title.

        Returns:
            Results.
        """
        return list(self.results.get(artist, []))

    def get_release_tracks(self, release_id):
        """Lists tracks.

        Args:
            release_id: Release id.

        Returns:
            Tracks.
        """
        return self.tracks.get(release_id, [])

    def get_wantlist_ids(self):
        """Lists the wantlist.

        Returns:
            Ids.
        """
        return set(self.wanted)

    def add_to_wantlist(self, release_id):
        """Adds to the wantlist.

        Args:
            release_id: Release id.
        """
        self.added.append(release_id)


class FakePlatform:
    """Platform stand-in.

    Args:
        tracks: Playlist tracks, None when the playlist does not exist.
    """

    def __init__(self, tracks):
        self.tracks = tracks
        self.created = []

    def find_playlist_by_name(self, name):
        """Finds the playlist.

        Args:
            name: Name.

        Returns:
            The playlist, or None.
        """
        return None if self.tracks is None else SimpleNamespace(id="w")

    def create_playlist(self, name, description="", public=True):
        """Creates the playlist.

        Args:
            name: Name.
            description: Description.
            public: Visibility.
        """
        self.created.append(name)

    def get_playlist_tracks(self, playlist_id):
        """Lists tracks.

        Args:
            playlist_id: Id.

        Returns:
            Tracks.
        """
        return self.tracks


def pressing(title="Glue", artist="Bicep", position="A1"):
    """Builds a Discogs track.

    Args:
        title: Title.
        artist: Artist.
        position: Position.

    Returns:
        The track.
    """
    return Track(position, title, artist, "", 1, "R", artist_names=(artist,))


@pytest.fixture
def state(tmp_path):
    """Provides a state database.

    Args:
        tmp_path: Temporary folder.

    Returns:
        The tracker.
    """
    return StateTracker(tmp_path / "state.db")


def test_earliest_verified_pressing_wins():
    """A later pressing loses, and an unverified earlier one is passed over."""
    discogs = FakeDiscogs(
        {"Bicep": [{"id": 3, "year": "2020"}, {"id": 2, "year": "2017"}, {"id": 1, "year": "2010"}]},
        {1: [pressing("Something Else")], 2: [pressing()], 3: [pressing()]},
    )
    assert find_pressing(discogs, [Reading(("Bicep",), "Glue")]) == 2


def test_other_mix_of_the_track_counts_as_the_record():
    """A vinyl holding a remix of the song is still the record to want."""
    discogs = FakeDiscogs({"Bicep": [{"id": 1, "year": "2017"}]}, {1: [pressing("Glue (Dub)")]})
    assert find_pressing(discogs, [Reading(("Bicep",), "Glue")]) == 1


@pytest.mark.parametrize(
    ("wanted", "tracks", "expected"),
    [
        ("Glue (Hardfloor Remix)", {1: [pressing("Glue")], 2: [pressing("Glue (Hardfloor Remix)")]}, 2),
        ("Glue - Hardfloor Remix", {1: [pressing("Glue")], 2: [pressing("Glue (Hardfloor Remix)")]}, 2),
        ("Glue (Hardfloor Remix)", {1: [pressing("Glue")], 2: [pressing("Other")]}, 1),
        ("Glue", {1: [pressing("Glue (Dub)")], 2: [pressing("Glue")]}, 2),
    ],
)
def test_exact_version_beats_an_earlier_other_mix(wanted, tracks, expected):
    """The record holding the exact mix wins, and another mix is only a fallback."""
    discogs = FakeDiscogs({"Bicep": [{"id": 1, "year": "2017"}, {"id": 2, "year": "2019"}]}, tracks)
    assert find_pressing(discogs, [Reading(("Bicep",), wanted)]) == expected


def test_wrong_artist_on_the_record_is_refused():
    """A record with the same title by someone else is not added."""
    discogs = FakeDiscogs({"Bicep": [{"id": 1, "year": "2017"}]}, {1: [pressing(artist="Someone Else")]})
    assert find_pressing(discogs, [Reading(("Bicep",), "Glue")]) is None


def test_new_track_goes_on_the_wantlist_once(state):
    """A second run does not act on the same track."""
    discogs = FakeDiscogs({"Bicep": [{"id": 1, "year": "2017"}]}, {1: [pressing()]})
    platform = FakePlatform([spotify_track()])
    first = sync_want_playlist(platform, "spotify", discogs, state, "Want")
    second = sync_want_playlist(platform, "spotify", discogs, state, "Want")
    assert discogs.added == [1]
    assert len(first.added) == 1
    assert second.added == []


def test_removing_from_wantlist_later_does_not_bring_it_back(state):
    """The person's later removal is respected."""
    discogs = FakeDiscogs({"Bicep": [{"id": 1, "year": "2017"}]}, {1: [pressing()]})
    platform = FakePlatform([spotify_track()])
    sync_want_playlist(platform, "spotify", discogs, state, "Want")
    discogs.wanted.clear()
    discogs.added.clear()
    sync_want_playlist(platform, "spotify", discogs, state, "Want")
    assert discogs.added == []


def test_already_wanted_is_not_added_again(state):
    """A pressing on the wantlist stays as it is."""
    discogs = FakeDiscogs({"Bicep": [{"id": 1, "year": "2017"}]}, {1: [pressing()]}, wanted=[1])
    result = sync_want_playlist(FakePlatform([spotify_track()]), "spotify", discogs, state, "Want")
    assert discogs.added == []
    assert len(result.already_wanted) == 1


def test_not_found_is_retried_next_time(state):
    """A record listed on Discogs later is picked up on a later run."""
    discogs = FakeDiscogs({}, {})
    platform = FakePlatform([spotify_track()])
    assert len(sync_want_playlist(platform, "spotify", discogs, state, "Want").not_found) == 1
    discogs.results = {"Bicep": [{"id": 1, "year": "2017"}]}
    discogs.tracks = {1: [pressing()]}
    sync_want_playlist(platform, "spotify", discogs, state, "Want")
    assert discogs.added == [1]


def test_dry_run_changes_nothing(state):
    """A dry run reports the addition and makes none, and remembers nothing."""
    discogs = FakeDiscogs({"Bicep": [{"id": 1, "year": "2017"}]}, {1: [pressing()]})
    result = sync_want_playlist(FakePlatform([spotify_track()]), "spotify", discogs, state, "Want", dry_run=True)
    assert discogs.added == []
    assert len(result.added) == 1
    assert state.get_handled_wants("spotify") == set()


@pytest.mark.parametrize(("dry_run", "created"), [(False, ["Want"]), (True, [])])
def test_missing_playlist_is_created(state, dry_run, created):
    """The playlist appears on the first real run."""
    platform = FakePlatform(None)
    sync_want_playlist(platform, "spotify", FakeDiscogs({}, {}), state, "Want", dry_run=dry_run)
    assert platform.created == created


@pytest.mark.parametrize(
    ("title", "uploader", "artist", "name"),
    [
        ("Bicep - Glue", "fan account", "Bicep", "Glue"),
        ("Glue", "Bicep", "Bicep", "Glue"),
    ],
)
def test_soundcloud_track_is_read_for_artist_and_title(title, uploader, artist, name):
    """An upload's title is decoded before searching Discogs."""
    track = SoundCloudTrack(1, "u", title, uploader, 0, 0, 1000, 1, True)
    first = track_readings(track)[0]
    assert (first.artists[0], first.title) == (artist, name)
