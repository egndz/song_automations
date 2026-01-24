"""Tests for the fuzzy matching system."""

import pytest

from song_automations.matching.fuzzy import (
    VersionType,
    calculate_artist_score,
    calculate_title_score,
    normalize_artist,
    normalize_text,
    parse_track_title,
    score_candidate,
    should_use_fallback,
)


class TestNormalizeText:
    """Tests for text normalization."""

    @pytest.mark.parametrize(
        "input_text,expected",
        [
            ("Hello World", "hello world"),
            ("  spaces  ", "spaces"),
            ("The Artist", "artist"),
            ("Track - Name", "track name"),
            ("It's OK", "it's ok"),
            ('"Quote"', '"quote"'),
        ],
    )
    def test_normalize_text(self, input_text: str, expected: str) -> None:
        """Test text normalization handles various cases."""
        assert normalize_text(input_text) == expected


class TestNormalizeArtist:
    """Tests for artist name normalization."""

    @pytest.mark.parametrize(
        "input_artist,expected",
        [
            ("DJ Shadow", "dj shadow"),
            ("The Chemical Brothers", "chemical brothers"),
            ("Fatboy Slim feat. Macy Gray", "fatboy slim"),
            ("Daft Punk ft. Pharrell", "daft punk"),
            ("Above & Beyond", "above"),
            ("Artist featuring Guest", "artist"),
            ("Artist (2)", "artist"),
        ],
    )
    def test_normalize_artist(self, input_artist: str, expected: str) -> None:
        """Test artist normalization extracts primary artist."""
        assert normalize_artist(input_artist) == expected


class TestParseTrackTitle:
    """Tests for track title parsing."""

    @pytest.mark.parametrize(
        "title,artist,expected_base,expected_version,expected_type",
        [
            ("Blue Monday", "New Order", "Blue Monday", "", VersionType.ORIGINAL),
            (
                "Blue Monday (Hardfloor Remix)",
                "New Order",
                "Blue Monday",
                "Hardfloor Remix",
                VersionType.REMIX,
            ),
            (
                "Track (Extended Mix)",
                "Artist",
                "Track",
                "Extended Mix",
                VersionType.EXTENDED,
            ),
            (
                "Song (Radio Edit)",
                "Band",
                "Song",
                "Radio Edit",
                VersionType.RADIO,
            ),
            (
                "Groove (Dub)",
                "Producer",
                "Groove",
                "Dub",
                VersionType.DUB,
            ),
            (
                "Classic (Remastered 2020)",
                "Legend",
                "Classic",
                "Remastered 2020",
                VersionType.REMASTER,
            ),
            (
                "Beat (Original Mix)",
                "DJ",
                "Beat",
                "Original Mix",
                VersionType.ORIGINAL,
            ),
            (
                "Melody (Instrumental)",
                "Composer",
                "Melody",
                "Instrumental",
                VersionType.INSTRUMENTAL,
            ),
            (
                "Vocals (Acapella)",
                "Singer",
                "Vocals",
                "Acapella",
                VersionType.ACAPELLA,
            ),
            (
                "Live Song (Live at Wembley)",
                "Band",
                "Live Song",
                "Live at Wembley",
                VersionType.LIVE,
            ),
            (
                "Tune (Artist Edit)",
                "Editor",
                "Tune",
                "Artist Edit",
                VersionType.EDIT,
            ),
            (
                "Groove (Rework)",
                "Reworker",
                "Groove",
                "Rework",
                VersionType.EDIT,
            ),
            (
                "Banger (VIP Mix)",
                "Producer",
                "Banger",
                "VIP Mix",
                VersionType.EDIT,
            ),
        ],
    )
    def test_parse_track_title(
        self,
        title: str,
        artist: str,
        expected_base: str,
        expected_version: str,
        expected_type: VersionType,
    ) -> None:
        """Test track title parsing extracts components correctly."""
        result = parse_track_title(title, artist)
        assert result.base_title == expected_base
        assert result.version == expected_version
        assert result.version_type == expected_type
        assert result.full_title == title
        assert result.artist == artist

    def test_parse_track_remixer_extraction(self) -> None:
        """Test that remixer name is extracted from remix info."""
        result = parse_track_title("Track (DJ Name Remix)", "Original Artist")
        assert result.remixer == "DJ Name"
        assert result.version_type == VersionType.REMIX

    def test_search_query_with_version(self) -> None:
        """Test search query includes version info."""
        result = parse_track_title("Track (Cool Remix)", "Artist")
        assert "Remix" in result.search_query
        assert "Artist" in result.search_query
        assert "Track" in result.search_query

    def test_fallback_query_without_version(self) -> None:
        """Test fallback query excludes version info."""
        result = parse_track_title("Track (Cool Remix)", "Artist")
        assert "Remix" not in result.fallback_query
        assert "Artist" in result.fallback_query
        assert "Track" in result.fallback_query


class TestShouldUseFallback:
    """Tests for fallback search decision."""

    @pytest.mark.parametrize(
        "title,should_fallback",
        [
            ("Track (Original Mix)", True),
            ("Track (Radio Edit)", True),
            ("Track (Extended Mix)", True),
            ("Track (Remastered 2020)", True),
            ("Track (DJ Remix)", False),
            ("Track (Dub)", False),
            ("Track (Artist Edit)", False),
        ],
    )
    def test_should_use_fallback(self, title: str, should_fallback: bool) -> None:
        """Test fallback decision based on version type."""
        parsed = parse_track_title(title, "Artist")
        assert should_use_fallback(parsed) == should_fallback


class TestCalculateArtistScore:
    """Tests for artist matching scores."""

    @pytest.mark.parametrize(
        "source,candidate,min_score",
        [
            ("Daft Punk", "Daft Punk", 0.95),
            ("Daft Punk", "daft punk", 0.95),
            ("The Chemical Brothers", "Chemical Brothers", 0.85),
            ("DJ Shadow", "DJ Shadow", 0.95),
            ("Above & Beyond", "Above", 0.90),
            ("Fatboy Slim", "Fat Boy Slim", 0.80),
        ],
    )
    def test_artist_score_similar(
        self, source: str, candidate: str, min_score: float
    ) -> None:
        """Test artist scores for similar names."""
        score = calculate_artist_score(source, candidate)
        assert score >= min_score

    @pytest.mark.parametrize(
        "source,candidate,max_score",
        [
            ("Daft Punk", "The Beatles", 0.35),
            ("Aphex Twin", "Radiohead", 0.35),
        ],
    )
    def test_artist_score_different(
        self, source: str, candidate: str, max_score: float
    ) -> None:
        """Test artist scores for different names."""
        score = calculate_artist_score(source, candidate)
        assert score <= max_score


class TestCalculateTitleScore:
    """Tests for title matching scores."""

    @pytest.mark.parametrize(
        "source,candidate,min_score",
        [
            ("Blue Monday", "Blue Monday", 0.95),
            ("Blue Monday (Extended Mix)", "Blue Monday (Extended Mix)", 0.95),
            ("Blue Monday (Hardfloor Remix)", "Blue Monday - Hardfloor Remix", 0.80),
            ("Around the World", "Around The World", 0.90),
            ("Track (2020 Remaster)", "Track (Remastered 2020)", 0.70),
        ],
    )
    def test_title_score_similar(
        self, source: str, candidate: str, min_score: float
    ) -> None:
        """Test title scores for similar names."""
        score = calculate_title_score(source, candidate)
        assert score >= min_score

    @pytest.mark.parametrize(
        "source,candidate,max_score",
        [
            ("Blue Monday", "Purple Rain", 0.40),
            ("One More Time", "Get Lucky", 0.30),
        ],
    )
    def test_title_score_different(
        self, source: str, candidate: str, max_score: float
    ) -> None:
        """Test title scores for different names."""
        score = calculate_title_score(source, candidate)
        assert score <= max_score


class TestScoreCandidate:
    """Tests for overall candidate scoring."""

    def test_score_perfect_match(self) -> None:
        """Test scoring for a perfect match."""
        parsed = parse_track_title("Track (Remix)", "Artist")
        total, artist, title, verified, pop = score_candidate(
            parsed_track=parsed,
            candidate_title="Track (Remix)",
            candidate_artist="Artist",
            is_verified=True,
            popularity=100,
            max_popularity=100,
        )
        assert total >= 0.90
        assert artist >= 0.90
        assert title >= 0.90
        assert verified == 1.0
        assert pop >= 0.90

    def test_score_partial_match(self) -> None:
        """Test scoring for a partial match."""
        parsed = parse_track_title("Blue Monday (Hardfloor Remix)", "New Order")
        total, artist, title, verified, pop = score_candidate(
            parsed_track=parsed,
            candidate_title="Blue Monday",
            candidate_artist="New Order",
            is_verified=False,
            popularity=50,
            max_popularity=100,
        )
        assert 0.40 <= total <= 0.80
        assert artist >= 0.90
        assert title >= 0.50

    def test_score_poor_match(self) -> None:
        """Test scoring for a poor match."""
        parsed = parse_track_title("Specific Track Name", "Obscure Artist")
        total, artist, title, verified, pop = score_candidate(
            parsed_track=parsed,
            candidate_title="Completely Different Song",
            candidate_artist="Other Artist",
            is_verified=False,
            popularity=10,
            max_popularity=100,
        )
        assert total < 0.40

    def test_verified_bonus_applied(self) -> None:
        """Test that verified bonus increases score."""
        parsed = parse_track_title("Track", "Artist")

        total_unverified, _, _, _, _ = score_candidate(
            parsed_track=parsed,
            candidate_title="Track",
            candidate_artist="Artist",
            is_verified=False,
            popularity=50,
            max_popularity=100,
        )

        total_verified, _, _, verified_bonus, _ = score_candidate(
            parsed_track=parsed,
            candidate_title="Track",
            candidate_artist="Artist",
            is_verified=True,
            popularity=50,
            max_popularity=100,
        )

        assert verified_bonus == 1.0
        assert total_verified > total_unverified

    def test_popularity_tiebreaker(self) -> None:
        """Test that popularity acts as tiebreaker."""
        parsed = parse_track_title("Track", "Artist")

        total_low, _, _, _, pop_low = score_candidate(
            parsed_track=parsed,
            candidate_title="Track",
            candidate_artist="Artist",
            is_verified=False,
            popularity=10,
            max_popularity=100,
        )

        total_high, _, _, _, pop_high = score_candidate(
            parsed_track=parsed,
            candidate_title="Track",
            candidate_artist="Artist",
            is_verified=False,
            popularity=90,
            max_popularity=100,
        )

        assert pop_high > pop_low
        assert total_high > total_low
