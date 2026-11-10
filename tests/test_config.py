"""Tests for the application settings."""

import pytest

from song_automations import cli
from song_automations.config import Settings

REMOVED_VARIABLES = [
    ("MIN_CONFIDENCE", "0.30"),
    ("MEDIUM_CONFIDENCE", "0.30"),
    ("MAX_WORKERS", "4"),
    ("ARTIST_WEIGHT", "0.45"),
    ("TITLE_WEIGHT", "0.35"),
    ("VERIFIED_WEIGHT", "0.10"),
    ("POPULARITY_WEIGHT", "0.10"),
    ("VERSION_MATCH_BONUS", "0.10"),
    ("VERSION_PENALTY_WEIGHT", "0.15"),
    ("LABEL_BONUS_WEIGHT", "0.10"),
    ("MAX_SEARCH_QUERIES", "5"),
    ("SOMETHING_NOBODY_EVER_DEFINED", "not a number"),
]


@pytest.mark.parametrize(("name", "value"), REMOVED_VARIABLES)
def test_env_file_with_a_removed_or_unknown_variable_still_loads(tmp_path, name, value):
    """A .env written for the old matcher loads, and the variable it sets is dropped."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{name}={value}\nPLAYLIST_PREFIX=Vinyl\n")
    settings = Settings(_env_file=env_file)
    assert settings.playlist_prefix == "Vinyl"
    assert not hasattr(settings, name.lower())


@pytest.mark.parametrize(("name", "value"), REMOVED_VARIABLES)
def test_environment_with_a_removed_or_unknown_variable_still_loads(monkeypatch, name, value):
    """The same variable exported in the shell is ignored as well."""
    monkeypatch.setenv(name, value)
    monkeypatch.setenv("PLAYLIST_PREFIX", "Vinyl")
    settings = Settings(_env_file=None)
    assert settings.playlist_prefix == "Vinyl"
    assert not hasattr(settings, name.lower())


@pytest.mark.parametrize("min_confidence", [None, 0.3, 0.9])
def test_min_confidence_option_is_accepted_and_ignored(tmp_path, monkeypatch, min_confidence):
    """The hidden --min-confidence value changes nothing now that matching has no threshold."""
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None, data_dir=tmp_path))
    monkeypatch.setattr(cli, "setup_logging", lambda **kwargs: None)
    settings = cli._init_settings(min_confidence)
    assert not hasattr(settings, "min_confidence")
    assert settings.data_dir == tmp_path
