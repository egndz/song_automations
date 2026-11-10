"""End-to-end engine tests with fake platforms and a real state database."""

from dataclasses import replace
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


def settings_beside(state, **overrides) -> Settings:
    """Builds settings whose data directory is the temporary folder of the state database.

    Without a data directory the settings would point at the real one in the
    home folder, and anything an engine wrote would land there.

    Args:
        state: Tracker.
        **overrides: Settings to change.

    Returns:
        Settings that read no .env file and keep their data next to the database.
    """
    return Settings(_env_file=None, data_dir=state._db_path.parent, **overrides)


@pytest.mark.parametrize("overrides", [{}, {"soundcloud_gated_tracks": True}, {"unattended": True}])
def test_engine_settings_stay_in_the_temporary_folder(state, tmp_path, overrides):
    """Every path the engine settings name lies inside the temporary folder of the test."""
    settings = settings_beside(state, **overrides)
    paths = [settings.data_dir, settings.db_path, settings.cache_dir, settings.reports_dir, settings.log_path]
    assert all(path.is_relative_to(tmp_path) for path in paths)
    assert all(getattr(settings, name) == value for name, value in overrides.items())


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
    engine = SyncEngine(settings_beside(state), discogs, state, Console(quiet=True))
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
        codes: Codes every track gets, with their lengths when given as a mapping.
    """

    def __init__(self, codes=()) -> None:
        self.codes = dict(codes) if isinstance(codes, dict) else dict.fromkeys(codes)
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


def store_spotify_decision(state, outcome, title="Glue"):
    """Stores what Spotify answered for track A1 of release 1.

    Args:
        state: Tracker.
        outcome: unknown for no stored decision, missing for a track Spotify
            lacks, or the tier of the match Spotify has.
        title: Title the decision was made for.
    """
    if outcome == "unknown":
        return
    found = outcome != "missing"
    state.save_matched_track(
        1, "A1", "Bicep", title, "spotify", "sp1" if found else None, 0.9 if found else 0.0,
        match_tier=outcome if found else None,
    )


@pytest.mark.parametrize(
    ("destination", "codes", "spotify", "isrcs", "fan_uploads", "lookups"),
    [
        ("soundcloud", set(), "unknown", set(), False, 1),
        ("soundcloud", set(), "missing", set(), True, 1),
        ("soundcloud", set(), "other_mix", set(), True, 1),
        ("soundcloud", set(), "original", set(), True, 1),
        ("soundcloud", set(), "exact", set(), False, 1),
        ("soundcloud", {"GBCFB1700231"}, "unknown", {"GBCFB1700231"}, False, 1),
        ("soundcloud", {"GBCFB1700231"}, "missing", {"GBCFB1700231"}, False, 1),
        ("soundcloud", {"GBCFB1700231"}, "other_mix", {"GBCFB1700231"}, False, 1),
        ("soundcloud", {"GBCFB1700231"}, "exact", {"GBCFB1700231"}, False, 1),
        ("spotify", {"GBCFB1700231"}, "unknown", set(), False, 0),
        ("spotify", {"GBCFB1700231"}, "exact", set(), False, 0),
    ],
)
def test_fan_uploads_need_spotify_to_have_been_asked(state, destination, codes, spotify, isrcs, fan_uploads, lookups):
    """A stranger's upload may match only once Spotify was asked, lacks the exact version, and no code is known."""
    store_spotify_decision(state, spotify)
    finder = FakeFinder(codes)
    engine = SyncEngine(settings_beside(state), FakeDiscogs({}), state, Console(quiet=True), isrc_finder=finder)
    source = engine._to_source(discogs_track(), Release(1, "Release", "Bicep", 2017, 1, "Folder"), destination)
    assert (set(source.isrcs), source.fan_uploads, len(finder.asked)) == (set(isrcs), fan_uploads, lookups)


@pytest.mark.parametrize(
    ("stored_title", "spotify", "fan_uploads", "isrcs"),
    [
        ("Glue", "missing", True, set()),
        ("Glue", "exact", False, {"GBCFB1700231"}),
        ("Glue (Original Mix)", "missing", False, set()),
        ("Glue (Original Mix)", "exact", False, set()),
        ("Opal", "missing", False, set()),
        ("Opal", "other_mix", False, set()),
        ("Opal", "exact", False, set()),
    ],
)
def test_spotify_decision_for_another_title_counts_as_not_asked(state, stored_title, spotify, fan_uploads, isrcs):
    """After Discogs changed the title at a position, the old Spotify answer neither allows fan uploads nor lends its code."""
    store_spotify_decision(state, spotify, stored_title)
    if spotify == "exact":
        with state._get_connection() as conn:
            conn.execute("UPDATE matched_tracks SET matched_isrc = 'GBCFB1700231'")
    engine = SyncEngine(settings_beside(state), FakeDiscogs({}), state, Console(quiet=True), isrc_finder=FakeFinder({}))
    source = engine._to_source(discogs_track(), Release(1, "Release", "Bicep", 2017, 1, "Folder"), "soundcloud")
    assert (source.fan_uploads, set(source.isrcs)) == (fan_uploads, isrcs)


class FakeSoundCloud(FakeSpotify):
    """SoundCloud stand-in holding playlists in memory.

    Args:
        uploads: Raw tracks every track search returns.
    """

    def __init__(self, uploads) -> None:
        super().__init__({}, {})
        self.uploads = list(uploads)

    def search_raw(self, query, kind="track"):
        """Searches tracks.

        Args:
            query: Query.
            kind: Unused.

        Returns:
            The uploads.
        """
        return self.uploads

    def search_playlists_raw(self, query):
        """Searches sets.

        Args:
            query: Query.

        Returns:
            Nothing.
        """
        return []

    def account_tracks_raw(self, name):
        """Lists an account.

        Args:
            name: Account name.

        Returns:
            Nothing.
        """
        return []

    def set_playlist_tracks(self, playlist_id, track_ids):
        """Replaces the tracks of a playlist.

        Args:
            playlist_id: Id.
            track_ids: Track ids.
        """
        self.playlists[playlist_id] = [str(t) for t in track_ids]


def run_soundcloud(state, soundcloud, gated: bool, tracks=None):
    """Runs a SoundCloud sync of one folder, holding one track unless others are given.

    Args:
        state: Tracker.
        soundcloud: SoundCloud fake.
        gated: Whether gated tracks may match.
        tracks: Discogs tracks of the folder.

    Returns:
        The result.
    """
    settings = settings_beside(state, soundcloud_gated_tracks=gated)
    discogs = FakeDiscogs({"House": tracks or [discogs_track()]})
    engine = SyncEngine(settings, discogs, state, Console(quiet=True))
    return engine.sync_to_soundcloud(soundcloud, include_wantlist=False)


@pytest.mark.parametrize(
    ("spotify", "codes", "upload_code", "playlist"),
    [
        ("unknown", {}, None, []),
        ("missing", {}, None, ["7"]),
        ("other_mix", {}, None, ["7"]),
        ("original", {}, None, ["7"]),
        ("exact", {}, None, []),
        ("unknown", {"GBCFB1700231": 300_000}, None, []),
        ("missing", {"GBCFB1700231": 300_000}, None, []),
        ("unknown", {"GBCFB1700231": 300_000}, "GBCFB1700231", ["7"]),
        ("exact", {"GBCFB1700231": 300_000}, "GBCFB1700231", ["7"]),
    ],
)
def test_vinyl_rip_by_a_stranger_waits_until_spotify_was_asked(state, spotify, codes, upload_code, playlist):
    """A SoundCloud run that reaches a track before Spotify did takes no fan upload, only one proven by its code."""
    store_spotify_decision(state, spotify)
    rip = {
        "id": 7,
        "title": "Bicep - Glue",
        "user": {"username": "Vinyl Digger"},
        "duration": 301_000,
        "access": "playable",
        "isrc": upload_code,
    }
    soundcloud = FakeSoundCloud([rip])
    discogs = FakeDiscogs({"House": [replace(discogs_track(), duration="5:00")]})
    engine = SyncEngine(settings_beside(state), discogs, state, Console(quiet=True), isrc_finder=FakeFinder(codes))
    result = engine.sync_to_soundcloud(soundcloud, include_wantlist=False)
    assert soundcloud.playlists.get("Discogs - House", []) == playlist
    assert result.tracks_missing == (0 if playlist else 1)


@pytest.mark.parametrize(
    ("access", "gated", "playlist", "missing", "counted"),
    [
        ("playable", False, ["7"], 0, 0),
        ("preview", False, [], 1, 1),
        ("blocked", False, [], 1, 1),
        ("preview", True, ["7"], 0, 0),
    ],
)
def test_gated_upload_is_counted_not_added_unless_allowed(state, access, gated, playlist, missing, counted):
    """An official upload the account cannot play in full is reported as such, and added only on request."""
    upload = {"id": 7, "title": "Glue", "user": {"username": "Bicep"}, "duration": 300_000, "access": access}
    soundcloud = FakeSoundCloud([upload])
    result = run_soundcloud(state, soundcloud, gated)
    assert soundcloud.playlists.get("Discogs - House", []) == playlist
    assert (result.tracks_missing, result.tracks_gated) == (missing, counted)


@pytest.mark.parametrize(("allowed_later", "playlist"), [(True, ["7"]), (False, [])])
def test_allowing_gated_tracks_takes_effect_on_the_next_run(state, allowed_later, playlist):
    """A track stored as gated is looked at again at once when the setting is switched on."""
    upload = {"id": 7, "title": "Glue", "user": {"username": "Bicep"}, "duration": 300_000, "access": "preview"}
    soundcloud = FakeSoundCloud([upload])
    run_soundcloud(state, soundcloud, False)
    result = run_soundcloud(state, soundcloud, allowed_later)
    assert soundcloud.playlists.get("Discogs - House", []) == playlist
    assert result.tracks_gated == (0 if allowed_later else 1)


def bicep_upload(id, title, access, ms=300_000):
    """Builds a raw SoundCloud track on the Bicep account.

    Args:
        id: Track id.
        title: Upload title.
        access: Access level for the account.
        ms: Length SoundCloud reports, 30 seconds for a clip that hides it.

    Returns:
        Raw track.
    """
    return {"id": id, "title": title, "user": {"username": "Bicep"}, "duration": ms, "access": access}


@pytest.mark.parametrize(
    ("length", "uploads", "playlist", "missing", "counted"),
    [
        ("", [bicep_upload(7, "Opal", "playable")], ["7"], 1, 0),
        ("", [bicep_upload(7, "Opal", "playable"), bicep_upload(8, "Opal (Four Tet Remix)", "preview")], ["7"], 1, 1),
        ("", [bicep_upload(7, "Opal", "playable"), bicep_upload(8, "Opal (Four Tet Remix)", "playable")], ["7", "8"], 0, 0),
        ("5:00", [bicep_upload(7, "Opal", "playable"), bicep_upload(8, "Opal (Four Tet Remix)", "preview", 30_000)], ["7"], 1, 1),
        ("", [bicep_upload(7, "Opal", "preview")], [], 2, 1),
    ],
)
def test_only_an_upload_the_account_cannot_play_counts_as_gated(state, length, uploads, playlist, missing, counted):
    """A playable upload that went to the original does not make the missing remix look gated."""
    tracks = [
        replace(discogs_track("Opal", "A1"), duration=length),
        replace(discogs_track("Opal (Four Tet Remix)", "B1"), duration=length),
    ]
    soundcloud = FakeSoundCloud(uploads)
    result = run_soundcloud(state, soundcloud, False, tracks)
    assert soundcloud.playlists.get("Discogs - House", []) == playlist
    assert (result.tracks_missing, result.tracks_gated) == (missing, counted)


KATMANDU_QUERY = 'track:"Katmandu" artist:"Cat Stevens"'
KATMANDU_CODE = "GBUM71905959"


def sync_katmandu_to_spotify(state, external_ids):
    """Syncs Cat Stevens' Katmandu to Spotify from a track search result.

    Args:
        state: Tracker.
        external_ids: The external ids Spotify lists for the track, None when it lists none.

    Returns:
        The Discogs tracks of the folder.
    """
    tracks = [discogs_track("Katmandu", "A1", artist="Cat Stevens")]
    found = item("sp1", "Katmandu", "Cat Stevens")
    if external_ids is not None:
        found["external_ids"] = external_ids
    run(state, FakeDiscogs({"House": tracks}), FakeSpotify({}, {KATMANDU_QUERY: [found]}))
    return tracks


@pytest.mark.parametrize(
    ("external_ids", "expected"),
    [
        ({"isrc": "GBUM71905959"}, (KATMANDU_CODE, 200_000)),
        ({"isrc": "gb-um7-19-05959"}, (KATMANDU_CODE, 200_000)),
        ({}, None),
        (None, None),
    ],
)
def test_recording_code_of_a_spotify_match_is_stored(state, external_ids, expected):
    """The code Spotify lists is kept with the match, in one spelling, and nothing is kept when it lists none."""
    sync_katmandu_to_spotify(state, external_ids)
    assert state.get_cached_match(1, "A1", "spotify").destination_track_id == "sp1"
    assert state.get_exact_spotify_recording(1, "A1", "Katmandu") == expected


@pytest.mark.parametrize(
    ("destination", "stored", "found", "has_finder", "isrcs"),
    [
        ("soundcloud", (KATMANDU_CODE, 203_000), {}, True, {KATMANDU_CODE: 203_000}),
        ("soundcloud", (KATMANDU_CODE, 203_000), {KATMANDU_CODE: 204_000}, True, {KATMANDU_CODE: 204_000}),
        ("soundcloud", (KATMANDU_CODE, None), {"GBUM72005432": 215_000}, True, {KATMANDU_CODE: None, "GBUM72005432": 215_000}),
        ("soundcloud", (KATMANDU_CODE, 203_000), {}, False, {KATMANDU_CODE: 203_000}),
        ("soundcloud", ("gb-um7-19-05959", 203_000), {}, True, {KATMANDU_CODE: 203_000}),
        ("soundcloud", None, {"GBUM72005432": 215_000}, True, {"GBUM72005432": 215_000}),
        ("spotify", (KATMANDU_CODE, 203_000), {"GBUM72005432": 215_000}, True, {}),
    ],
)
def test_spotify_code_joins_the_catalogue_codes(state, destination, stored, found, has_finder, isrcs):
    """SoundCloud matching gets the Spotify code next to the catalogue's, and the catalogue's length wins."""
    track = discogs_track("Katmandu", "A1", artist="Cat Stevens")
    if stored:
        state.save_matched_track(
            1, "A1", "Cat Stevens", "Katmandu", "spotify", "sp1", 0.95,
            match_tier="exact", matched_duration_ms=stored[1], matched_isrc=stored[0],
        )
    finder = FakeFinder(found) if has_finder else None
    engine = SyncEngine(settings_beside(state), FakeDiscogs({}), state, Console(quiet=True), isrc_finder=finder)
    source = engine._to_source(track, Release(1, "Release", "Cat Stevens", 1970, 1, "Folder"), destination)
    assert dict(source.isrcs) == isrcs
    assert source.fan_uploads is not bool(stored or found)


@pytest.mark.parametrize(
    ("external_ids", "upload_code", "upload_ms", "playlist"),
    [
        ({"isrc": KATMANDU_CODE}, KATMANDU_CODE, 203_000, ["7"]),
        ({"isrc": KATMANDU_CODE}, "gb-um7-19-05959", 203_000, ["7"]),
        (None, KATMANDU_CODE, 203_000, []),
        ({"isrc": KATMANDU_CODE}, "GBUM72005432", 203_000, []),
        ({"isrc": KATMANDU_CODE}, None, 203_000, []),
        ({"isrc": KATMANDU_CODE}, KATMANDU_CODE, 90_000, []),
    ],
)
def test_spotify_code_proves_the_official_upload_the_catalogue_lacks(state, external_ids, upload_code, upload_ms, playlist):
    """An upload on a differently named account passes only with the code of the Spotify match and its full length."""
    tracks = sync_katmandu_to_spotify(state, external_ids)
    upload = {
        "id": 7,
        "title": "Katmandu (Remastered 2020)",
        "user": {"username": "YusufCatStevens"},
        "duration": upload_ms,
        "access": "playable",
        "isrc": upload_code,
    }
    soundcloud = FakeSoundCloud([upload])
    run_soundcloud(state, soundcloud, False, tracks)
    assert soundcloud.playlists.get("Discogs - House", []) == playlist
