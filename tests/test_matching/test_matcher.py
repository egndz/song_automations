"""Tests for the gate-based matcher, using mistakes seen in the real collection."""

import pytest

from song_automations.matching.matcher import (
    Candidate,
    Match,
    Reading,
    Rejection,
    SourceTrack,
    Tier,
    assign_release,
    classify_tier,
    clean,
    decide,
    evaluate,
    parse_title,
    soundcloud_candidate,
    soundcloud_readings,
    split_artists,
    spotify_candidate,
)


def spotify(title: str, artist: str, ms: int | None = None, id: str = "x", album: str = "") -> Candidate:
    """Builds a Spotify style candidate.

    Args:
        title: Track title.
        artist: Artist name.
        ms: Duration.
        id: Track id.
        album: Album name.

    Returns:
        The candidate.
    """
    return spotify_candidate(
        {"id": id, "name": title, "artists": [{"name": artist}], "duration_ms": ms, "album": {"name": album}}
    )


def soundcloud(title: str, uploader: str, ms: int | None = 300_000, id: str = "x", artist: str = "") -> Candidate:
    """Builds a SoundCloud style candidate.

    Args:
        title: Upload title.
        uploader: Account name.
        ms: Duration.
        id: Track id.
        artist: Metadata artist.

    Returns:
        The candidate.
    """
    return soundcloud_candidate(
        {"id": id, "title": title, "user": {"username": uploader}, "duration": ms, "metadata_artist": artist}
    )


@pytest.mark.parametrize(
    ("artist", "title", "candidate"),
    [
        ("Eddy Love", "The Fear", spotify("The Fear", "Blue Oyster Cult")),
        ("T-Total", "Afterglow", spotify("Afterglow", "Ed Sheeran")),
        ("Diana Ross", "Upside Down", spotify("Upside Down (Cover)", "Diana Ross")),
        ("Fontaines D.C.", "Romance", soundcloud("Romance", "Roman Holiday")),
        ("Muddy Waters", "Got My Mojo Working (Part 1)", spotify("Got My Mojo Working (Part 2)", "Muddy Waters")),
        ("Bicep", "Glue", spotify("Glue", "Bicep Tribute Band")),
        ("Bicep", "Glue", soundcloud("Bicep - Glue (karaoke)", "someone")),
        ("Fontaines D.C.", "Favourite", soundcloud("Fontaines D.C. - Favourite (Later... with Jools Holland)", "Fontaines D.C.")),
        ("Bicep", "Glue", soundcloud("Bicep - Glue (Live at Printworks)", "Bicep")),
        ("Bicep", "Glue", spotify("Glue - KEXP Session", "Bicep")),
        ("Stevie Wonder", "Uptight (Everything's Alright)", soundcloud("Stevie Wonder vs The Clash - Uptight", "Stevie Wonder")),
        ("Bicep", "Glue", soundcloud("Bicep - Glue", "someone", ms=45_000)),
        ("Bicep", "Glue", soundcloud("Bicep - Glue", "someone", ms=3_600_000)),
        ("Response & Pliskin", "Soon Forgotten", soundcloud("Response & Pliskin - Soon Forgotten (preview)", "OKBRON")),
    ],
)
def test_wrong_candidates_are_refused(artist, title, candidate):
    """Known wrong matches never pass, whatever else agrees."""
    assert isinstance(evaluate(SourceTrack((artist,), title), candidate), Rejection)


@pytest.mark.parametrize(
    ("artist", "title", "candidate", "tier"),
    [
        ("Bicep", "Glue", spotify("Glue", "Bicep"), Tier.EXACT),
        ("Bicep", "Glue", soundcloud("BICEP | GLUE", "BICEP"), Tier.EXACT),
        ("Umberto Lumber", "Between Those Sheets", soundcloud("B2. Umberto Lumber - Between Those Sheets", "HUGH"), Tier.EXACT),
        ("Teknoafro", "Mama Africa", soundcloud("A1 TEKNOAFRO - MAMA AFRICA", "Dualismo Sound"), Tier.EXACT),
        ("Chain Reaction", "Dance Freak", soundcloud("Chain Reaction - Dance Freak", "x"), Tier.EXACT),
        ("Westcoast Goddess", "Change Is Comin'", soundcloud("PREMIERE: Westcoast Goddess - Change Is Comin [Intrinsic]", "Juno"), Tier.EXACT),
        ("Fio Fa", "In To Da Groove (New Jackson Remix)", soundcloud("Fio Fa - In To Da Groove (New Jackson Remix)", "Pear"), Tier.EXACT),
        ("Fio Fa", "In To Da Groove (New Jackson Remix)", spotify("In To Da Groove - New Jackson Mix", "Fio Fa"), Tier.EXACT),
        ("Cyre", "Last Chance (Club Version)", soundcloud("CYRE - LAST CHANCE", "x"), Tier.ORIGINAL),
        ("Bicep", "Opal", soundcloud("Bicep - Opal (Four Tet Remix)", "x"), Tier.OTHER_MIX),
        ("Dj Spooge", "Sit Yourself Down", soundcloud("Sit Yourself Down [LG001]", "DJ SPOOGE"), Tier.EXACT),
        ("Beyoncé", "Halo", spotify("Halo", "Beyonce"), Tier.EXACT),
        ("Model 500", "No UFOs", spotify("No UFO's", "Model500"), Tier.EXACT),
        ("Earth, Wind & Fire", "September", spotify("September", "Earth, Wind & Fire"), Tier.EXACT),
        ("Bicep", "Glue", spotify("Glue - 2017 Remaster", "Bicep"), Tier.EXACT),
    ],
)
def test_right_candidates_are_accepted_on_the_right_tier(artist, title, candidate, tier):
    """Real title shapes pass, placed at the correct rung of the version ladder."""
    outcome = evaluate(SourceTrack((artist,), title, duration_ms=candidate.duration_ms), candidate)
    assert isinstance(outcome, Match)
    assert outcome.tier == tier


def test_marker_is_forgiven_when_source_title_carries_it():
    """A source that is itself a live recording may match a live upload."""
    outcome = evaluate(SourceTrack(("Bicep",), "Glue (Live)"), spotify("Glue (Live)", "Bicep"))
    assert isinstance(outcome, Match)


@pytest.mark.parametrize(
    ("artist", "title", "candidate", "accepted"),
    [
        ("Pearl Jam", "Alive", spotify("Alive (Live)", "Pearl Jam"), False),
        ("Pink Floyd", "Eclipse", soundcloud("Pink Floyd - Eclipse (Clip)", "Pink Floyd"), False),
        ("The Rolling Stones", "Undercover Of The Night", spotify("Undercover Of The Night (Cover)", "The Rolling Stones"), False),
        ("Marina", "Obsessions", spotify("Obsessions - BBC Sessions", "Marina"), False),
        ("Big Daddy Kane", "Special Delivery", spotify("Special Delivery - Live", "Big Daddy Kane"), False),
        ("Oasis", "Live Forever", spotify("Live Forever - Live at Knebworth", "Oasis"), False),
        ("The Who", "Substitute (Live At Leeds)", spotify("Substitute - Live At Leeds", "The Who"), True),
        ("Nirvana", "About A Girl (MTV Unplugged)", spotify("About A Girl - MTV Unplugged", "Nirvana"), True),
        ("Pearl Jam", "Alive (Live)", spotify("Alive - Live", "Pearl Jam"), True),
        ("Pearl Jam", "Alive", spotify("Alive", "Pearl Jam"), True),
    ],
)
def test_marker_is_excused_only_by_the_same_whole_words(artist, title, candidate, accepted):
    """A title that merely contains the letters of a marker does not excuse it."""
    assert isinstance(evaluate(SourceTrack((artist,), title), candidate), Match) is accepted


@pytest.mark.parametrize(("delta", "accepted"), [(1_000, True), (60_000, False)])
def test_gig_word_is_forgiven_only_at_the_same_length(delta, accepted):
    """A label channel named after a venue is fine if the length is the record's."""
    source = SourceTrack(("Chaos In The CBD",), "Significant Others", duration_ms=320_000)
    candidate = soundcloud('Chaos In The CBD "Significant Others" - Boiler Room Debuts', "Boiler Room", ms=320_000 + delta)
    assert isinstance(evaluate(source, candidate), Match) is accepted


def test_label_account_upload_is_accepted_only_for_the_release_label():
    """A bare title on an account counts if that account is the label."""
    source = SourceTrack(("Nobody",), "Deep Track", labels=("Ki Records",))
    assert isinstance(evaluate(source, soundcloud("Deep Track", "Ki Records")), Match)
    assert isinstance(evaluate(source, soundcloud("Deep Track", "Other Records")), Rejection)


def test_different_length_of_same_named_version_drops_a_tier():
    """A length far from the source means it is not really the same mix."""
    source = SourceTrack(("Bicep",), "Glue", duration_ms=300_000)
    outcome = evaluate(source, spotify("Glue", "Bicep", ms=200_000))
    assert outcome.tier == Tier.OTHER_MIX


@pytest.mark.parametrize(
    ("source", "candidate", "tier"),
    [
        ([], [], Tier.EXACT),
        ([], ["radio edit"], Tier.OTHER_MIX),
        (["club version"], [], Tier.ORIGINAL),
        (["hardfloor remix"], ["hardfloor mix"], Tier.EXACT),
        (["hardfloor remix"], ["carl craig remix"], Tier.OTHER_MIX),
    ],
)
def test_classify_tier(source, candidate, tier):
    """The version ladder follows the agreed preference."""
    assert classify_tier(source, candidate) == tier


@pytest.mark.parametrize(
    ("title", "candidate", "tier"),
    [
        ("Glue (Remix)", "Glue - Remix", Tier.EXACT),
        ("Glue (Remix)", "Glue (RMX)", Tier.EXACT),
        ("Glue (Remix)", "Glue (Remix Version)", Tier.EXACT),
        ("Glue (Edit)", "Glue (Reedit)", Tier.EXACT),
        ("Glue (Version)", "Glue (Mix)", Tier.EXACT),
        ("Glue (Edit)", "Glue (Remix)", Tier.OTHER_MIX),
        ("Glue (Remix)", "Glue - Edit", Tier.OTHER_MIX),
        ("Glue (Version)", "Glue (Edit)", Tier.OTHER_MIX),
        ("Glue (Remix)", "Glue (Remix Edit)", Tier.OTHER_MIX),
        ("Glue (Hardfloor Remix)", "Glue (Remix)", Tier.OTHER_MIX),
    ],
)
def test_unnamed_versions_agree_only_when_they_are_the_same_kind(title, candidate, tier):
    """A bare edit is not the exact version of a bare remix, though neither names anyone."""
    outcome = evaluate(SourceTrack(("Bicep",), title), spotify(candidate, "Bicep"))
    assert isinstance(outcome, Match)
    assert outcome.tier == tier


@pytest.mark.parametrize(
    ("credit", "names"),
    [
        ("Feater", ("feater",)),
        ("Featurecast", ("featurecast",)),
        ("FTampa", ("ftampa",)),
        ("Bicep feat. Clara La San", ("bicep feat clara la san", "bicep", "clara la san")),
        ("Bicep ft Clara La San", ("bicep ft clara la san", "bicep", "clara la san")),
        ("Bicep Feat Clara La San", ("bicep feat clara la san", "bicep", "clara la san")),
    ],
)
def test_featuring_splits_a_credit_only_as_a_whole_word(credit, names):
    """A name that starts like feat or ft is one artist, not a credit with a guest."""
    assert split_artists(credit) == names


@pytest.mark.parametrize(
    ("title", "base", "versions"),
    [
        ("Track (Hardfloor Remix)", "track", ["hardfloor remix"]),
        ("Track - Radio Edit", "track", ["radio edit"]),
        ("Track (Original Mix)", "track", []),
        ("Track [LG001]", "track", []),
        ("Track Pt. 2", "track part 2", []),
        ("Aoi Galasu Dama = Blue Glass Ball", "aoi galasu dama", []),
        ("Track feat. Someone", "track", []),
    ],
)
def test_parse_title(title, base, versions):
    """Titles split into base and version."""
    bases, found = parse_title(title)
    assert bases[0] == base
    assert found == versions


def test_parse_title_keeps_translated_title():
    """Both sides of an equals sign can match."""
    assert "blue glass ball" in parse_title("Aoi Galasu Dama = Blue Glass Ball")[0]


@pytest.mark.parametrize(("raw", "expected"), [("Beyoncé", "beyonce"), ("AC/DC", "ac dc"), ("Rock & Roll", "rock and roll")])
def test_clean(raw, expected):
    """Normalization removes accents and punctuation."""
    assert clean(raw) == expected


def test_ranking_prefers_exact_then_original_then_other():
    """The best tier wins regardless of search order or plays."""
    source = SourceTrack(("Bicep",), "Glue (Dub)")
    decision = decide(
        source,
        [
            soundcloud("Bicep - Glue (Four Tet Remix)", "Bicep", id="other"),
            soundcloud("Bicep - Glue", "Bicep", id="original"),
            soundcloud("Bicep - Glue (Dub)", "Bicep", id="exact"),
        ],
    )
    assert decision.match.candidate.id == "exact"
    assert [m.candidate.id for m in decision.alternatives] == ["original", "other"]


def test_ranking_prefers_the_source_release():
    """Among equal versions, the original release beats a compilation."""
    source = SourceTrack(("Bicep",), "Glue", release_title="Bicep")
    decision = decide(
        source,
        [
            spotify("Glue", "Bicep", id="comp", album="Ministry Of Sound 2017"),
            spotify("Glue", "Bicep", id="album", album="Bicep"),
        ],
    )
    assert decision.match.candidate.id == "album"


def test_assign_release_gives_each_track_its_own_upload():
    """Two tracks falling back to one upload never share it."""
    candidates = [soundcloud("Bicep - Glue", "Bicep", id="plain"), soundcloud("Bicep - Glue (Dub)", "Bicep", id="dub")]
    decisions = [
        decide(SourceTrack(("Bicep",), "Glue (Remix)"), candidates),
        decide(SourceTrack(("Bicep",), "Glue (Dub)"), candidates),
    ]
    assigned = assign_release(decisions)
    assert assigned[1].candidate.id == "dub"
    assert assigned[0].candidate.id == "plain"


def test_assign_release_leaves_a_track_empty_rather_than_repeat():
    """When only one upload exists, the second track gets nothing."""
    candidate = [soundcloud("Bicep - Glue", "Bicep", id="only")]
    decisions = [decide(SourceTrack(("Bicep",), "Glue"), candidate), decide(SourceTrack(("Bicep",), "Glue (Dub)"), candidate)]
    assert [m.candidate.id if m else None for m in assign_release(decisions)] == ["only", None]


@pytest.mark.parametrize(
    ("source", "candidate", "accepted"),
    [
        (SourceTrack(("Bicep",), "Glue"), soundcloud("Bicep - Glue", "Bicep"), True),
        (SourceTrack(("Bicep",), "Glue", labels=("Ninja Tune",)), soundcloud("Bicep - Glue", "Ninja Tune"), True),
        (SourceTrack(("Bicep",), "Glue", duration_ms=269_000), soundcloud("Bicep - Glue", "fan", ms=275_000), True),
        (SourceTrack(("Bicep",), "Glue", duration_ms=269_000), soundcloud("Bicep - Glue", "fan", ms=442_000), False),
        (SourceTrack(("Bicep",), "Glue"), soundcloud("Bicep - Glue", "fan", ms=269_000), False),
        (SourceTrack(("Bicep",), "Glue (Pomo Edit)", remixers=("Pomo",)), soundcloud("Bicep - Glue (Pomo Edit)", "Pomo", ms=300_000), True),
        (SourceTrack(("Bicep",), "Glue"), soundcloud("Bicep - Glue (Pomo Edit)", "Pomo", ms=300_000), False),
        (SourceTrack(("Foo Fighters",), "Long Road To Ruin", fan_uploads=False), soundcloud("Foo Fighters - Long Road To Ruin", "FOOT Fighters", ms=300_000), False),
        (SourceTrack(("Fontaines D.C.",), "Favourite", fan_uploads=False), soundcloud("Fontaines D.C. - Favourite", "Fontaines DC", ms=300_000), True),
        (SourceTrack(("Sade",), "Smooth Operator", duration_ms=298_000), soundcloud("Sade - Smooth Operator", "Sade Tribute Band", ms=298_000), False),
        (SourceTrack(("Pet Shop Boys",), "Rent", duration_ms=238_000), soundcloud("Rent (Pet Shop Boys, actually)", "Pet Shop Boys, actually", ms=238_000), False),
        (SourceTrack(("Led Zeppelin",), "Whole Lotta Love", duration_ms=334_000, fan_uploads=False), soundcloud("Led Zeppelin ~ Whole Lotta Love", "Strat Guy Vic", ms=335_000), False),
        (SourceTrack(("Led Zeppelin",), "Whole Lotta Love", duration_ms=334_000, fan_uploads=False), soundcloud("Led Zeppelin - Whole Lotta Love", "Led Zeppelin", ms=335_000), True),
    ],
)
def test_fan_uploads_need_a_matching_length(source, candidate, accepted):
    """Strangers' uploads count only when the length confirms them, the artist, label or remixer always count."""
    assert isinstance(evaluate(source, candidate), Match) is accepted


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        ("Bicep - Glue", "dash"),
        ("Glue - Bicep", "dash_reversed"),
        ('Bicep "Glue"', "quoted"),
        ("Bicep: Glue", "colon"),
        ("Glue by Bicep", "by"),
    ],
)
def test_soundcloud_readings_find_the_artist(title, kind):
    """Every common upload format yields the right artist and title."""
    readings = soundcloud_readings(title, "someone")
    assert any(r.kind == kind and clean(r.artists[0]) == "bicep" and clean(r.title) == "glue" for r in readings)


def test_decide_is_independent_of_candidate_order():
    """The same candidates in any order give the same choice."""
    source = SourceTrack(("Bicep",), "Glue")
    items = [soundcloud("Bicep - Glue", "Bicep", id=str(i)) for i in range(4)]
    assert decide(source, items).match.candidate.id == decide(source, items[::-1]).match.candidate.id


def test_reading_is_hashable():
    """Readings can be deduplicated."""
    assert len({Reading(("a",), "b"), Reading(("a",), "b")}) == 1


def catalogue_upload(title="Katmandu (Remastered 2020)", uploader="YusufCatStevens", isrc="GBUM71905959", access="blocked", ms=203_000, id="x") -> Candidate:
    """Builds a SoundCloud track the way a label delivers it.

    Args:
        title: Upload title.
        uploader: Account name.
        isrc: Recording code.
        access: Access level.
        ms: Duration.
        id: Track id.

    Returns:
        The candidate.
    """
    return soundcloud_candidate(
        {"id": id, "title": title, "user": {"username": uploader}, "duration": ms, "isrc": isrc, "access": access}
    )


@pytest.mark.parametrize(
    ("known", "candidate", "accepted"),
    [
        ({"GBUM71905959"}, catalogue_upload(), True),
        ({"GBUM71905959"}, catalogue_upload(isrc="gb-um7-19-05959"), True),
        ({"GBUM71905959"}, catalogue_upload(title="Katmandu (Live at the BBC)"), True),
        ({"GBUM71905959"}, catalogue_upload(isrc=""), False),
        ({"GBUM71905959"}, catalogue_upload(title="Katmandu (Made Popular By Cat Stevens)", uploader="Party Tyme Karaoke", isrc="USSYS1103776"), False),
        (set(), catalogue_upload(), False),
    ],
)
def test_same_recording_code_is_trusted_whatever_the_account(known, candidate, accepted):
    """An upload carrying the code of the record is the record; anything else still needs a trusted account."""
    source = SourceTrack(("Cat Stevens",), "Katmandu", fan_uploads=False, isrcs=dict.fromkeys(known))
    outcome = evaluate(source, candidate)
    assert isinstance(outcome, Match) is accepted
    if accepted:
        assert (outcome.tier, outcome.reading.kind, outcome.confidence) == (Tier.EXACT, "isrc", 1.0)


@pytest.mark.parametrize(
    ("candidates", "winner"),
    [
        ([catalogue_upload(title="Glue", uploader="Bicep", isrc="", access="playable", id="own"), catalogue_upload(title="Glue", uploader="BICEP MUSIC", isrc="GBCFB1700231", access="preview", id="code")], "code"),
        ([catalogue_upload(title="Glue", uploader="Bicep", isrc="", access="preview", id="gated"), catalogue_upload(title="Glue", uploader="Bicep", isrc="", access="playable", id="free")], "free"),
        ([catalogue_upload(title="Glue", uploader="Bicep", isrc="GBCFB1700231", access="blocked", id="gated"), catalogue_upload(title="Glue", uploader="Bicep", isrc="GBCFB1700231", access="playable", id="free")], "free"),
    ],
)
def test_verified_recording_wins_then_the_freely_playable_one(candidates, winner):
    """The upload with the recording code goes first, and between equals the one anybody can play."""
    source = SourceTrack(("Bicep",), "Glue", fan_uploads=False, isrcs={"GBCFB1700231": None})
    assert decide(source, candidates).match.candidate.id == winner


@pytest.mark.parametrize(
    ("access", "ms", "expected"),
    [("preview", 30_000, None), ("preview", 250_000, 250_000), ("playable", 30_000, 30_000), ("blocked", 30_000, 30_000)],
)
def test_preview_clip_length_is_not_the_track_length(access, ms, expected):
    """A subscriber-only track that reports its 30 second clip has an unknown length, not a short one."""
    assert catalogue_upload(access=access, ms=ms).duration_ms == expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [({"external_ids": {"isrc": "gbcfb1700231"}}, "GBCFB1700231"), ({"external_ids": {}}, ""), ({}, "")],
)
def test_spotify_candidate_reads_the_recording_code(item, expected):
    """Search results carry the code, album listings do not."""
    assert spotify_candidate({"id": "1", "name": "Glue", "artists": [], **item}).isrc == expected


@pytest.mark.parametrize(
    ("discogs_ms", "catalogue_ms", "upload_ms", "access", "accepted"),
    [
        (269_000, 269_000, 103_000, "playable", False),
        (None, 269_000, 103_000, "playable", False),
        (269_000, None, 103_000, "playable", False),
        (None, None, 103_000, "playable", True),
        (269_000, 269_000, 270_000, "playable", True),
        (269_000, 269_000, 30_000, "preview", True),
        (269_000, 269_000, 30_000, "playable", False),
    ],
)
def test_clip_posted_under_the_code_of_the_full_track_is_refused(discogs_ms, catalogue_ms, upload_ms, access, accepted):
    """BICEP posts "GLUE // CLIP" with the code of Glue; the code is only trusted when the length fits."""
    source = SourceTrack(("Bicep",), "Glue", duration_ms=discogs_ms, fan_uploads=False, isrcs={"GBCFB1700229": catalogue_ms})
    clip = catalogue_upload(title="BICEP | GLUE // CLIP", uploader="BICEP", isrc="GBCFB1700229", access=access, ms=upload_ms)
    assert isinstance(evaluate(source, clip), Match) is accepted


@pytest.mark.parametrize(
    ("candidate", "accepted"),
    [
        (soundcloud("YAUNiMAMi - Drift (original)", "Dove"), False),
        (soundcloud("Drift", "Dove"), False),
        (spotify("Drift", "Dove"), True),
        (soundcloud("Drift", "Bicep"), True),
    ],
)
def test_account_named_like_an_alias_is_not_the_artist(candidate, accepted):
    """Bicep also record as Dove; a stranger's account called Dove proves nothing, a track credited to Dove does."""
    source = SourceTrack(("Bicep",), "Drift", aliases=("Dove",))
    assert isinstance(evaluate(source, candidate), Match) is accepted


@pytest.mark.parametrize(
    ("record", "upload", "accepted"),
    [
        ("Bullfrog Blues", "Bullfrog Blues (WNCR Cleveland Radio Session / 1973)", False),
        ("Bullfrog Blues", "Bullfrog Blues (Peel Sessions)", False),
        ("Bullfrog Blues", "Bullfrog Blues", True),
        ("Jam Session", "Jam Session", True),
    ],
)
def test_radio_session_is_not_the_record(record, upload, accepted):
    """A session recording only matches a record whose own title says session."""
    source = SourceTrack(("Rory Gallagher",), record)
    assert isinstance(evaluate(source, soundcloud(upload, "Rory Gallagher")), Match) is accepted


@pytest.mark.parametrize(
    ("record_ms", "access", "upload_ms", "isrcs", "tier"),
    [
        (187_000, "preview", 30_000, {}, Tier.OTHER_MIX),
        (None, "preview", 30_000, {}, Tier.EXACT),
        (187_000, "preview", 190_000, {}, Tier.EXACT),
        (187_000, "playable", None, {}, Tier.EXACT),
        (187_000, "preview", 30_000, {"USRH11505140": 187_000}, Tier.EXACT),
    ],
)
def test_gated_track_with_hidden_length_is_not_called_exact(record_ms, access, upload_ms, isrcs, tier):
    """A live bootleg with a length cannot be told from the studio cut when SoundCloud hides the length."""
    source = SourceTrack(("Led Zeppelin",), "Since I've Been Loving You", duration_ms=record_ms, isrcs=isrcs)
    upload = catalogue_upload("Since I've Been Loving You (Remaster)", "Led Zeppelin", "USRH11505140", access, upload_ms)
    assert evaluate(source, upload).tier == tier
