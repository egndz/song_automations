"""Tests for matching a set and filling its playlist."""

from types import SimpleNamespace

import pytest

from song_automations.matching.matcher import spotify_candidate
from song_automations.setlist.sync import match_tracks, sync_setlist
from song_automations.setlist.tracklist import SetTrack


class FakeSearcher:
    """Searcher over a small catalogue.

    Args:
        catalogue: Raw platform tracks by "artist|title" query.
    """

    def __init__(self, catalogue) -> None:
        self.catalogue, self.searches = catalogue, []

    def track_queries(self, artist, title):
        """Lists the queries for a track.

        Args:
            artist: Artist.
            title: Title.

        Returns:
            One query.
        """
        return [f"{artist}|{title}"]

    def search(self, query):
        """Searches.

        Args:
            query: Query.

        Returns:
            Candidates.
        """
        self.searches.append(query)
        return [spotify_candidate(item) for item in self.catalogue.get(query, [])]


class FakeClient:
    """Playlist client holding playlists in memory.

    Args:
        playlists: Track ids by playlist name.
    """

    def __init__(self, playlists=None) -> None:
        self.playlists = dict(playlists or {})
        self.created = []

    def find_playlist_by_name(self, name):
        """Finds a playlist.

        Args:
            name: Name.

        Returns:
            The playlist or None.
        """
        return SimpleNamespace(id=name, name=name) if name in self.playlists else None

    def get_playlist_tracks(self, playlist_id):
        """Lists the tracks.

        Args:
            playlist_id: Playlist id.

        Returns:
            Tracks.
        """
        return [SimpleNamespace(id=track_id) for track_id in self.playlists[playlist_id]]

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
        self.created.append((name, description, public))
        return SimpleNamespace(id=name, name=name)

    def add_tracks_to_playlist(self, playlist_id, uris):
        """Adds tracks.

        Args:
            playlist_id: Playlist id.
            uris: Track uris.
        """
        self.playlists[playlist_id] += [uri.rsplit(":", 1)[-1] for uri in uris]

    def remove_tracks_from_playlist(self, playlist_id, uris):
        """Removes tracks.

        Args:
            playlist_id: Playlist id.
            uris: Track uris.
        """
        gone = {uri.rsplit(":", 1)[-1] for uri in uris}
        self.playlists[playlist_id] = [t for t in self.playlists[playlist_id] if t not in gone]


class HeldClient(FakeClient):
    """Client whose playlist tracks carry a name and a length.

    Args:
        held: Tracks of the playlist "Set - x" as (id, name, length).
    """

    def __init__(self, held) -> None:
        super().__init__({"Set - x": [track_id for track_id, _, _ in held]})
        self.details = {track_id: (name, ms) for track_id, name, ms in held}

    def get_playlist_tracks(self, playlist_id):
        """Lists the tracks with their details.

        Args:
            playlist_id: Playlist id.

        Returns:
            Tracks.
        """
        return [
            SimpleNamespace(id=track_id, name=self.details.get(track_id, ("", None))[0], duration_ms=self.details.get(track_id, ("", None))[1])
            for track_id in self.playlists[playlist_id]
        ]


class FakeSnapshots:
    """Snapshot store that remembers what it was asked to save."""

    def __init__(self) -> None:
        self.saved = []

    def save(self, platform, playlist, tracks):
        """Saves a playlist.

        Args:
            platform: Platform name.
            playlist: Playlist.
            tracks: Tracks it held.
        """
        self.saved.append((platform, playlist.id, [str(track.id) for track in tracks]))


def item(id, name, artist, isrc="", ms=200000):
    """Builds a raw Spotify track.

    Args:
        id: Id.
        name: Title.
        artist: Artist.
        isrc: Recording code.
        ms: Length.

    Returns:
        Raw track.
    """
    return {
        "id": id,
        "name": name,
        "artists": [{"name": artist}],
        "duration_ms": ms,
        "album": {"name": "x"},
        "external_ids": {"isrc": isrc},
    }


SET = [SetTrack("Bicep", "Glue"), SetTrack("Overmono", "So U Kno"), SetTrack("Joy Orbison", "Hyph Mngo"), SetTrack("Burial", "Archangel")]
WRITTEN = {
    "Bicep|Glue": [item("glue", "Glue", "Bicep")],
    "Overmono|So U Kno": [item("kno", "So U Kno", "Overmono")],
    "Burial|Archangel": [item("arch", "Archangel", "Burial")],
}
TURNED = [SetTrack(track.title, track.artist) for track in SET]


@pytest.mark.parametrize(
    ("tracks", "found", "searches"),
    [
        (SET, ["glue", "kno", None, "arch"], 5),
        (TURNED, ["glue", "kno", None, "arch"], 7),
        (SET[:1], ["glue"], 1),
        ([], [], 0),
    ],
)
def test_match_tracks_settles_the_order_on_the_first_tracks(tracks, found, searches):
    """A title-first tracklist is recognised, at the cost of a few searches."""
    searcher = FakeSearcher(WRITTEN)
    matches = match_tracks(searcher, tracks)
    assert [match.candidate.id if match else None for match in matches] == found
    assert len(searcher.searches) == searches


@pytest.mark.parametrize(
    ("wanted", "titles", "chosen"),
    [
        ("Glue", ["Glue - Mixed", "Glue"], "1"),
        ("Glue", ["Glue (Mixed)", "Glue"], "1"),
        ("Glue", ["Glue [Mixed]", "Glue"], "1"),
        ("Glue (Dub Mix)", ["Glue (Dub Mix) - Mixed", "Glue - Dub Mix"], "1"),
        ("Glue", ["Glue - Mixed"], "0"),
        ("Glue", ["Glue", "Glue - Mixed"], "0"),
        ("Glue", ["Glue - Mixed", "Glue (Dub)"], "0"),
        ("Glue (Solomun Vox)", ["Glue (Solomun Vox) - Mixed", "Glue"], "0"),
        ("Glue (Late Night)", ["Glue (Late Night) - Mixed", "Glue"], "0"),
    ],
)
def test_the_release_is_preferred_over_its_cut_from_a_mix(wanted, titles, chosen):
    """A cut from a mixed compilation gives way only to a release with the very same title."""
    items = [item(str(index), title, "Bicep") for index, title in enumerate(titles)]
    matches = match_tracks(FakeSearcher({f"Bicep|{wanted}": items}), [SetTrack("Bicep", wanted)])
    assert matches[0].candidate.id == chosen


@pytest.mark.parametrize(("order_known", "searches"), [(False, 7), (True, 4)])
def test_a_source_that_names_artist_and_title_is_not_tried_turned_round(order_known, searches):
    """Songs the platform names itself cost one search each, even when none is found."""
    searcher = FakeSearcher({})
    assert match_tracks(searcher, SET, order_known) == [None] * 4
    assert len(searcher.searches) == searches


@pytest.mark.parametrize(("album", "chosen"), [("Isles", "b_album"), ("", "a_single"), ("Something Else", "a_single")])
def test_the_release_the_source_names_is_preferred(album, chosen):
    """With the release known, the copy of the track on that release wins over the usual order."""
    copies = [item("a_single", "Glue", "Bicep"), {**item("b_album", "Glue", "Bicep"), "album": {"name": "Isles"}}]
    track = SetTrack("Bicep", "Glue", album=album)
    matches = match_tracks(FakeSearcher({"Bicep|Glue": copies}), [track], order_known=True)
    assert matches[0].candidate.id == chosen


def test_wrong_artist_is_not_taken():
    """A track by someone else with the same title stays out."""
    searcher = FakeSearcher({"Bicep|Glue": [item("other", "Glue", "Somebody Else")]})
    assert match_tracks(searcher, [SetTrack("Bicep", "Glue")]) == [None]


@pytest.mark.parametrize(
    ("second", "kept", "repeats"),
    [
        (item("glue", "Glue", "Friend", "GB1"), ["glue"], [False, True]),
        (item("glue_album", "Glue", "Friend", "GB1"), ["glue"], [False, True]),
        (item("glue_long", "Glue", "Friend", "GB1", ms=400000), ["glue", "glue_long"], [False, False]),
        (item("glue_other", "Glue", "Friend", "GB2"), ["glue", "glue_other"], [False, False]),
    ],
)
def test_a_recording_played_twice_is_added_once(second, kept, repeats):
    """The same id or the same recording code with the same length counts as a repeat."""
    searcher = FakeSearcher({"Bicep|Glue": [item("glue", "Glue", "Bicep", "GB1")], "Friend|Glue": [second]})
    client = FakeClient()
    result = sync_setlist(client, searcher, [SetTrack("Bicep", "Glue"), SetTrack("Friend", "Glue")], "Set - x")
    assert client.playlists["Set - x"] == kept
    assert [row.repeat for row in result.rows] == repeats


@pytest.mark.parametrize(
    ("held", "dry_run", "after", "created", "added", "present"),
    [
        (None, False, ["glue", "kno", "arch"], True, 3, 0),
        (None, True, None, True, 3, 0),
        (["hand_added", "kno"], False, ["hand_added", "kno", "glue", "arch"], False, 2, 1),
        (["glue", "kno", "arch"], False, ["glue", "kno", "arch"], False, 0, 3),
    ],
)
def test_sync_setlist_only_ever_adds(held, dry_run, after, created, added, present):
    """A new playlist is private, an existing one keeps what it holds, a dry run changes nothing."""
    client = FakeClient({} if held is None else {"Set - x": list(held)})
    result = sync_setlist(client, FakeSearcher(WRITTEN), SET, "Set - x", "from a video", dry_run)
    assert client.playlists.get("Set - x") == after
    assert (result.created, result.added, result.present) == (created, added, present)
    assert client.created == ([("Set - x", "from a video", False)] if held is None and not dry_run else [])


def test_nothing_found_creates_no_playlist():
    """A set with no track on the platform leaves no empty playlist behind."""
    client = FakeClient()
    result = sync_setlist(client, FakeSearcher({}), SET, "Set - x")
    assert client.playlists == {}
    assert (result.created, result.added) == (False, 0)


@pytest.mark.parametrize(
    ("held", "added"),
    [
        (("other_copy", "Glue", 200000), 0),
        (("other_copy", "Glue - Original Mix", 201500), 0),
        (("other_copy", "Glue", 300000), 1),
        (("other_copy", "Glue - Dub", 200000), 1),
        (("other_copy", "Something Else", 200000), 1),
        (("other_copy", "Glue", None), 1),
        (("glue", "", None), 0),
    ],
)
def test_another_copy_of_a_held_track_is_not_added_again(held, added):
    """A held track with the same title and length is the same recording under another id."""
    client = HeldClient([held])
    result = sync_setlist(client, FakeSearcher(WRITTEN), [SetTrack("Bicep", "Glue")], "Set - x")
    assert result.added == added
    assert len(client.playlists["Set - x"]) == 1 + added


@pytest.mark.parametrize(
    ("held", "dry_run", "after", "added", "removed", "saved"),
    [
        (["kno", "old", "glue"], False, ["glue", "kno", "arch"], 1, 1, True),
        (["arch", "kno", "glue"], False, ["glue", "kno", "arch"], 0, 0, True),
        (["glue", "kno", "arch"], False, ["glue", "kno", "arch"], 0, 0, False),
        (["kno", "old", "glue"], True, ["kno", "old", "glue"], 1, 1, False),
    ],
)
def test_replace_makes_the_playlist_the_set_in_set_order(held, dry_run, after, added, removed, saved):
    """What the playlist held is saved before it changes, and an identical playlist is left alone."""
    client, snapshots = FakeClient({"Set - x": list(held)}), FakeSnapshots()
    result = sync_setlist(client, FakeSearcher(WRITTEN), SET, "Set - x", dry_run=dry_run, replace=True, snapshots=snapshots)
    assert client.playlists["Set - x"] == after
    assert (result.added, result.removed) == (added, removed)
    assert snapshots.saved == ([("spotify", "Set - x", held)] if saved else [])


@pytest.mark.parametrize(("catalogue", "held", "after"), [({}, ["old"], ["old"]), (WRITTEN, None, ["glue", "kno", "arch"])])
def test_replace_never_empties_a_playlist_and_creates_a_missing_one(catalogue, held, after):
    """With nothing found the playlist is untouched, and without a playlist a replace is a plain run."""
    client = FakeClient({} if held is None else {"Set - x": list(held)})
    sync_setlist(client, FakeSearcher(catalogue), SET, "Set - x", replace=True, snapshots=FakeSnapshots())
    assert client.playlists["Set - x"] == after
