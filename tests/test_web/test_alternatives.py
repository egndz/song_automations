"""Tests for the alternatives route of the review app."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from song_automations.clients.discogs import Track
from song_automations.clients.spotify import SpotifyClient
from song_automations.config import Settings
from song_automations.errors import (
    LoginFailedError,
    LoginTimeoutError,
    RateLimitedError,
    ReauthRequiredError,
    SearchBudgetExhaustedError,
    UnattendedLoginError,
)
from song_automations.state.tracker import MatchedTrack, StateTracker
from song_automations.web.app import MAX_SEARCHES, build_source, create_app


def spotify_item(id, name, artist, duration_ms=200000):
    """Builds a raw Spotify track.

    Args:
        id: Track id.
        name: Title.
        artist: Artist.
        duration_ms: Length.

    Returns:
        Raw track.
    """
    return {
        "id": id,
        "name": name,
        "artists": [{"name": artist}],
        "duration_ms": duration_ms,
        "album": {"name": "x"},
    }


def soundcloud_item(id, title, uploader, isrc=""):
    """Builds a raw SoundCloud track.

    Args:
        id: Track id.
        title: Upload title.
        uploader: Account name.
        isrc: Recording code.

    Returns:
        Raw track.
    """
    return {
        "id": id,
        "title": title,
        "user": {"username": uploader},
        "duration": 200000,
        "access": "playable",
        "isrc": isrc,
    }


class FakePlatform:
    """Platform client stand-in that records every search.

    Args:
        items: Raw tracks every search returns.
        error: Raised by every search instead of answering.
    """

    def __init__(self, items=(), error=None):
        self.items, self.error = list(items), error
        self.queries = []

    def search_raw(self, query, kind="track", limit=10):
        """Searches.

        Args:
            query: Query text.
            kind: Result kind.
            limit: Maximum results.

        Returns:
            Raw tracks.

        Raises:
            Exception: The configured error, when there is one.
        """
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return list(self.items)


class FakeDiscogs:
    """Discogs stand-in.

    Args:
        tracks: Tracks every release has.
    """

    def __init__(self, tracks):
        self.tracks = tracks

    def get_release_tracks(self, release_id):
        """Lists tracks.

        Args:
            release_id: Release id.

        Returns:
            Tracks.
        """
        return list(self.tracks)


class FakeFinder:
    """Recording code finder stand-in.

    Args:
        codes: Length by recording code, returned for every track.
    """

    def __init__(self, codes=None):
        self.codes = codes or {}

    def find(self, source, artist):
        """Looks up codes.

        Args:
            source: Source track.
            artist: Artist.

        Returns:
            Length by code.
        """
        return dict(self.codes)


class Review:
    """A review app over a temporary database with fake platforms.

    Args:
        tmp_path: Temporary folder.
        spotify: Spotify client stand-in.
        soundcloud: SoundCloud client stand-in.
        finder: Recording code finder stand-in.
        discogs: Discogs stand-in.
        budget: Spotify searches the app may make.
        factories: Client factories to use instead of the ones handing out the stand-ins.
    """

    def __init__(
        self,
        tmp_path,
        spotify=None,
        soundcloud=None,
        finder=None,
        discogs=None,
        budget=400,
        factories=None,
    ):
        self.settings = Settings(_env_file=None, data_dir=tmp_path, spotify_search_budget=budget)
        self.state = StateTracker(self.settings.db_path)
        self.built = []
        self.platforms = {"spotify": spotify, "soundcloud": soundcloud}
        app = create_app(
            settings=self.settings,
            client_factories=factories or {name: self._factory(name) for name in self.platforms},
            discogs_client=discogs,
            isrc_finder=finder or FakeFinder(),
        )
        self.client = TestClient(app)

    def _factory(self, name):
        """Builds the factory of one platform.

        Args:
            name: Platform name.

        Returns:
            Factory that records each call and hands out the stand-in.
        """

        def build(settings):
            """Hands out the stand-in.

            Args:
                settings: Application settings.

            Returns:
                The platform client.
            """
            self.built.append(name)
            return self.platforms[name]

        return build

    def add(self, destination="spotify", title="Glue", artist="Bicep", matched="t1", position="A1"):
        """Stores a match to review.

        Args:
            destination: Platform.
            title: Track title.
            artist: Artist.
            matched: Platform track id the match points at.
            position: Track position.

        Returns:
            Database row id of the match.
        """
        self.state.save_matched_track(1, position, artist, title, destination, matched, 0.45, "other_mix")
        return self.state.get_cached_match(1, position, destination).id

    def alternatives(self, row_id):
        """Asks for the alternatives of a match.

        Args:
            row_id: Database row id.

        Returns:
            The HTTP response.
        """
        return self.client.get(f"/alternatives/{row_id}")


CANDIDATES = [
    spotify_item("remix", "Glue (Hammer Remix)", "Bicep", 320000),
    spotify_item("other", "Glue", "Somebody Else"),
    spotify_item("orig", "Glue", "Bicep"),
]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Glue", [("orig", "exact"), ("remix", "other_mix")]),
        ("Glue (Hammer Remix)", [("remix", "exact"), ("orig", "original")]),
        ("Opal", []),
    ],
)
def test_accepted_candidates_come_best_first_with_tier_and_confidence(tmp_path, title, expected):
    """Only candidates that pass the gates are listed, ranked by the live matcher."""
    review = Review(tmp_path, spotify=FakePlatform(CANDIDATES))
    body = review.alternatives(review.add(title=title, matched="orig")).json()
    listed = body["alternatives"]
    assert [(entry["track_id"], entry["tier"]) for entry in listed] == expected
    assert all(0 < entry["confidence"] <= 1 for entry in listed)
    assert all(entry["score"] == entry["confidence"] for entry in listed)
    assert [entry["is_current"] for entry in listed] == [id == "orig" for id, _ in expected]
    assert body["search_query"] == review.platforms["spotify"].queries[0]


@pytest.mark.parametrize(
    ("destination", "items"),
    [
        ("spotify", CANDIDATES),
        ("soundcloud", [soundcloud_item(7, "Glue", "Bicep")]),
    ],
)
def test_a_request_searches_at_most_twice_and_repeats_cost_nothing(tmp_path, destination, items):
    """One request makes at most two searches, and a repeat is answered from the stored ones."""
    platform = FakePlatform(items)
    review = Review(tmp_path, **{destination: platform})
    row_id = review.add(destination=destination, title="Glue (Hammer Remix)")
    first = review.alternatives(row_id)
    assert first.status_code == 200
    assert 1 <= len(platform.queries) <= MAX_SEARCHES
    made = len(platform.queries)
    assert review.alternatives(row_id).json() == first.json()
    assert len(platform.queries) == made


@pytest.mark.parametrize("requests", [1, 3])
def test_one_client_serves_every_request(tmp_path, requests):
    """The platform client is built once, however many requests arrive."""
    review = Review(tmp_path, spotify=FakePlatform(CANDIDATES))
    for index in range(requests):
        row_id = review.add(title=f"Glue {index}", position=f"A{index}")
        assert review.alternatives(row_id).status_code == 200
    assert review.built == ["spotify"]


@pytest.mark.parametrize(
    ("budget", "statuses"),
    [
        (2, [200, 429]),
        (3, [200, 429]),
        (4, [200, 200, 429]),
    ],
)
def test_search_budget_holds_across_requests(tmp_path, monkeypatch, budget, statuses):
    """The real client, built by the real factory, keeps counting from one request to the next."""
    spotipy_client = MagicMock()
    spotipy_client.search.return_value = {"tracks": {"items": CANDIDATES}}
    monkeypatch.setattr("spotipy.Spotify", lambda *args, **kwargs: spotipy_client)
    monkeypatch.setattr("song_automations.clients.spotify.time.sleep", lambda _: None)
    review = Review(tmp_path, budget=budget, factories={"spotify": SpotifyClient})
    answers = [
        review.alternatives(review.add(title=f"Glue {index}", position=f"A{index}"))
        for index in range(len(statuses))
    ]
    assert [answer.status_code for answer in answers] == statuses
    assert answers[-1].json()["error"] == "search_budget_exhausted"
    assert spotipy_client.search.call_count == budget


@pytest.mark.parametrize(
    ("error", "status", "code", "retry_after", "advice"),
    [
        (RateLimitedError("Spotify", 3600), 429, "rate_limited", "3600", "Try again after"),
        (ReauthRequiredError("Spotify"), 401, "reauth_required", None, "song-automations login spotify"),
        (LoginTimeoutError("Spotify", 600), 401, "reauth_required", None, "within 600 seconds"),
        (LoginFailedError("Spotify", "invalid_client"), 401, "reauth_required", None, "invalid_client"),
        (SearchBudgetExhaustedError("Spotify", 400), 429, "search_budget_exhausted", None, "400"),
    ],
)
def test_platform_errors_are_reported_not_swallowed(
    tmp_path, error, status, code, retry_after, advice
):
    """A blocked, logged out or used up platform answers with a clear JSON error."""
    platform = FakePlatform(error=error)
    review = Review(tmp_path, spotify=platform)
    answer = review.alternatives(review.add())
    assert answer.status_code == status
    assert answer.json()["error"] == code
    assert answer.json()["platform"] == "Spotify"
    assert "Spotify" in answer.json()["detail"]
    assert advice in answer.json()["detail"]
    assert "sync again" not in answer.json()["detail"]
    assert answer.headers.get("retry-after") == retry_after
    assert len(platform.queries) == 1


@pytest.mark.parametrize(
    ("destination", "error", "status", "code", "advice"),
    [
        ("spotify", ReauthRequiredError("Spotify"), 401, "reauth_required", "song-automations login spotify"),
        ("soundcloud", UnattendedLoginError("SoundCloud"), 401, "reauth_required", "song-automations login soundcloud"),
        ("soundcloud", LoginTimeoutError("SoundCloud", 600), 401, "reauth_required", "within 600 seconds"),
        ("spotify", LoginFailedError("Spotify", "invalid_client"), 401, "reauth_required", "invalid_client"),
        ("spotify", RateLimitedError("Spotify", 3600), 429, "rate_limited", "Try again after"),
        ("spotify", SearchBudgetExhaustedError("Spotify", 400), 429, "search_budget_exhausted", "400"),
        ("spotify", RuntimeError("no credentials"), 500, None, "Failed to initialize client: no credentials"),
    ],
)
def test_errors_while_building_the_client_keep_their_meaning(tmp_path, destination, error, status, code, advice):
    """A login or limit problem that shows while the client is built is reported as such, not as a crash."""

    def build(settings):
        """Stands in for a client that cannot be built.

        Args:
            settings: Application settings.

        Raises:
            Exception: The configured error.
        """
        raise error

    review = Review(tmp_path, factories={"spotify": build, "soundcloud": build})
    answer = review.alternatives(review.add(destination=destination))
    assert answer.status_code == status
    assert answer.json().get("error") == code
    assert advice in answer.json()["detail"]


@pytest.mark.parametrize("row_id", [1, 999])
def test_unknown_track_is_not_found(tmp_path, row_id):
    """A row id that is not stored answers 404 without searching."""
    platform = FakePlatform(CANDIDATES)
    review = Review(tmp_path, spotify=platform)
    assert review.alternatives(row_id).status_code == 404
    assert platform.queries == []


@pytest.mark.parametrize(
    ("refused", "expected"),
    [
        ([], ["orig", "remix"]),
        (["orig"], ["remix"]),
        (["orig", "remix"], []),
    ],
)
def test_refused_candidates_are_left_out(tmp_path, refused, expected):
    """Candidates a person refused for this track are never offered again."""
    review = Review(tmp_path, spotify=FakePlatform(CANDIDATES))
    for track_id in refused:
        review.state.update_review_status(review.add(matched=track_id), "rejected")
    row_id = review.add(matched="elsewhere")
    listed = review.alternatives(row_id).json()["alternatives"]
    assert [entry["track_id"] for entry in listed] == expected


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ({"GBABC1234567": 200000}, [("7", "exact", 1.0)]),
        ({}, []),
    ],
)
def test_soundcloud_upload_by_a_stranger_needs_the_recording_code(tmp_path, codes, expected):
    """An upload under another account counts only when it carries the code of the recording."""
    upload = soundcloud_item(7, "Bicep - Glue", "Some Distributor", isrc="GB-ABC-12-34567")
    review = Review(tmp_path, soundcloud=FakePlatform([upload]), finder=FakeFinder(codes))
    body = review.alternatives(review.add(destination="soundcloud", matched="7")).json()
    listed = [(e["track_id"], e["tier"], e["confidence"]) for e in body["alternatives"]]
    assert listed == expected
    assert all("api.soundcloud.com/tracks/7" in entry["embed_url"] for entry in body["alternatives"])


@pytest.mark.parametrize(
    ("discogs_duration", "candidate_ms", "expected_tier"),
    [
        ("3:20", 200000, "exact"),
        ("3:20", 400000, "other_mix"),
        ("", 400000, "exact"),
    ],
)
def test_discogs_details_feed_the_gates(tmp_path, discogs_duration, candidate_ms, expected_tier):
    """The Discogs length decides whether the same title is the same version."""
    pressing = Track("A1", "Glue", "Bicep", discogs_duration, 1, "Bicep EP", artist_names=("Bicep",))
    platform = FakePlatform([spotify_item("orig", "Glue", "Bicep", candidate_ms)])
    review = Review(tmp_path, spotify=platform, discogs=FakeDiscogs([pressing]))
    listed = review.alternatives(review.add(matched="orig")).json()["alternatives"]
    assert [entry["tier"] for entry in listed] == [expected_tier]


@pytest.mark.parametrize(
    ("tracks", "artists", "duration", "labels", "search_artist"),
    [
        ([], ("Bicep & Hammer",), None, (), "Bicep & Hammer"),
        (
            [
                Track("A2", "Opal", "Other", "1:00", 1, "EP"),
                Track(
                    "A1", "Glue", "Bicep & Hammer", "4:29", 1, "EP",
                    artist_names=("Bicep", "Hammer"), labels=("Ninja Tune",),
                ),
            ],
            ("Bicep", "Hammer"),
            269000,
            ("Ninja Tune",),
            "Bicep",
        ),
        ([Track("A1", "Glue", "Bicep", "", 1, "EP")], ("Bicep",), None, (), "Bicep"),
    ],
)
def test_build_source(tracks, artists, duration, labels, search_artist):
    """The source uses the Discogs track at the stored position, or the stored row alone."""
    row = MatchedTrack(1, "A1", "Bicep & Hammer", "Glue", "spotify", "t1", 0.45, None)
    source, artist = build_source(row, tracks)
    assert (source.artists, source.title) == (artists, "Glue")
    assert (source.duration_ms, source.labels) == (duration, labels)
    assert artist == search_artist


@pytest.mark.parametrize(
    ("path", "offers_alternatives"),
    [
        ("/", True),
        ("/?destination=spotify", True),
        ("/?destination=soundcloud", False),
        ("/logs", False),
    ],
)
def test_pages_render(tmp_path, path, offers_alternatives):
    """The pages load, and a listed match gets the button that calls the route."""
    review = Review(tmp_path)
    review.add()
    answer = review.client.get(path)
    assert answer.status_code == 200
    assert ('onclick="showAlternatives(' in answer.text) == offers_alternatives


@pytest.mark.parametrize(
    ("spotify_code", "spotify_tier", "spotify_title", "expected"),
    [
        ("GBABC1234567", "exact", "Glue", [("7", "exact")]),
        ("gb-abc-12-34567", "exact", "Glue", [("7", "exact")]),
        (None, "exact", "Glue", []),
        ("GBABC1234567", "other_mix", "Glue", []),
        ("GBABC1234567", "exact", "Opal", []),
        ("GBXYZ7654321", "exact", "Glue", []),
    ],
)
def test_soundcloud_alternatives_use_the_code_of_the_spotify_match(
    tmp_path, spotify_code, spotify_tier, spotify_title, expected
):
    """The route accepts what a sync accepts: an upload proven by the code of the exact Spotify match."""
    upload = soundcloud_item(7, "Bicep - Glue", "Some Distributor", isrc="GB-ABC-12-34567")
    review = Review(tmp_path, soundcloud=FakePlatform([upload]), finder=FakeFinder({}))
    review.state.save_matched_track(
        1, "A1", "Bicep", spotify_title, "spotify", "sp1", 0.95,
        match_tier=spotify_tier, matched_duration_ms=200000, matched_isrc=spotify_code,
    )
    body = review.alternatives(review.add(destination="soundcloud", matched="7")).json()
    assert [(entry["track_id"], entry["tier"]) for entry in body["alternatives"]] == expected


@pytest.mark.parametrize(
    ("tier", "confidence", "label"),
    [
        ("other_mix", 0.45, "Another mix of the song"),
        ("other_mix", 0.7, "Another mix of the song"),
        ("original", 0.85, "Original mix, not the listed version"),
        ("exact", 0.4, None),
        ("exact", 0.95, None),
    ],
)
def test_review_page_lists_what_a_sync_flags(tmp_path, tier, confidence, label):
    """A match of another version is listed and named whatever its confidence, and approve all clears it."""
    review = Review(tmp_path)
    review.state.save_matched_track(1, "A1", "Bicep", "Glue", "spotify", "t1", confidence, tier)
    listed = tier != "exact" or confidence < review.settings.high_confidence
    page = review.client.get("/").text
    assert ("Searching for: <strong>Bicep - Glue</strong>" in page) == listed
    assert ("No tracks need review" in page) != listed
    labels = ("Another mix of the song", "Original mix, not the listed version")
    assert [text for text in labels if text in page] == ([label] if label else [])
    shown = {"track_ids": [review.state.get_cached_match(1, "A1", "spotify").id]}
    assert review.client.post("/approve-all", data=shown, follow_redirects=False).status_code == 303
    assert review.state.get_cached_match(1, "A1", "spotify").review_status == ("approved" if listed else "pending")
    assert "No tracks need review" in review.client.get("/").text


@pytest.mark.parametrize(("flagged", "posted", "approved"), [(12, 10, 10), (12, 0, 0), (3, 3, 3), (3, 1, 1)])
def test_approve_all_only_approves_what_the_page_showed(tmp_path, flagged, posted, approved):
    """Matches on other pages, or never posted, stay unreviewed."""
    review = Review(tmp_path)
    for number in range(flagged):
        review.state.save_matched_track(1, f"A{number}", "Bicep", f"Glue {number}", "spotify", f"t{number}", 0.55, "other_mix")
    ids = [review.state.get_cached_match(1, f"A{number}", "spotify").id for number in range(flagged)]
    review.client.post("/approve-all", data={"track_ids": ids[:posted]}, follow_redirects=False)
    statuses = [review.state.get_cached_match(1, f"A{number}", "spotify").review_status for number in range(flagged)]
    assert statuses.count("approved") == approved
    assert statuses[:posted] == ["approved"] * posted


@pytest.mark.parametrize("tier", ["exact"])
def test_approve_all_ignores_an_id_that_is_not_flagged(tmp_path, tier):
    """A posted id of a match that needs no review is left alone."""
    review = Review(tmp_path)
    review.state.save_matched_track(1, "A1", "Bicep", "Glue", "spotify", "t1", 0.95, tier)
    row = review.state.get_cached_match(1, "A1", "spotify")
    review.client.post("/approve-all", data={"track_ids": [row.id]}, follow_redirects=False)
    assert review.state.get_cached_match(1, "A1", "spotify").review_status == "pending"


@pytest.mark.parametrize(
    ("spotify_track_id", "spotify_tier", "spotify_title", "expected"),
    [
        (None, None, None, []),
        (None, None, "Glue", ["7"]),
        ("sp1", "other_mix", "Glue", ["7"]),
        ("sp1", "original", "Glue", ["7"]),
        ("sp1", "exact", "Glue", []),
        (None, None, "Opal", []),
    ],
)
def test_fan_upload_is_offered_only_after_spotify_was_asked(
    tmp_path, spotify_track_id, spotify_tier, spotify_title, expected
):
    """The route offers a stranger's upload under the rule a sync follows: Spotify was asked and lacks the exact version."""
    pressing = Track("A1", "Glue", "Bicep", "3:20", 1, "Bicep EP", artist_names=("Bicep",))
    rip = soundcloud_item(7, "Bicep - Glue", "Vinyl Digger")
    review = Review(tmp_path, soundcloud=FakePlatform([rip]), finder=FakeFinder({}), discogs=FakeDiscogs([pressing]))
    if spotify_title:
        review.state.save_matched_track(
            1, "A1", "Bicep", spotify_title, "spotify", spotify_track_id,
            0.9 if spotify_track_id else 0.0, match_tier=spotify_tier,
        )
    body = review.alternatives(review.add(destination="soundcloud", matched="7")).json()
    assert [entry["track_id"] for entry in body["alternatives"]] == expected
