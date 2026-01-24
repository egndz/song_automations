"""Configuration management using Pydantic settings."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Args:
        discogs_user_token: Personal access token from Discogs developer settings.
        spotify_client_id: Spotify application client ID.
        spotify_client_secret: Spotify application client secret.
        spotify_redirect_uri: OAuth redirect URI for Spotify authentication.
        soundcloud_client_id: SoundCloud application client ID.
        soundcloud_client_secret: SoundCloud application client secret.
        soundcloud_redirect_uri: OAuth redirect URI for SoundCloud authentication.
        data_dir: Directory for storing SQLite database and cache files.
        playlist_prefix: Prefix for created playlists.
        min_confidence: Minimum confidence threshold for track matching.
        high_confidence: Threshold for high confidence matches (auto-add).
        medium_confidence: Threshold for medium confidence matches (add with flag).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    discogs_user_token: str = Field(
        default="",
        description="Discogs personal access token",
    )

    spotify_client_id: str = Field(
        default="",
        description="Spotify application client ID",
    )
    spotify_client_secret: str = Field(
        default="",
        description="Spotify application client secret",
    )
    spotify_redirect_uri: str = Field(
        default="http://localhost:8888/callback",
        description="Spotify OAuth redirect URI",
    )

    soundcloud_client_id: str = Field(
        default="",
        description="SoundCloud application client ID",
    )
    soundcloud_client_secret: str = Field(
        default="",
        description="SoundCloud application client secret",
    )
    soundcloud_redirect_uri: str = Field(
        default="http://localhost:8889/callback",
        description="SoundCloud OAuth redirect URI",
    )

    data_dir: Path = Field(
        default=Path.home() / ".song_automations",
        description="Directory for storing application data",
    )

    playlist_prefix: str = Field(
        default="Discogs - ",
        description="Prefix for created playlists",
    )

    min_confidence: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description="Minimum confidence threshold (skip if below)",
    )
    high_confidence: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description="High confidence threshold (auto-add)",
    )
    medium_confidence: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description="Medium confidence threshold (add with flag)",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Logging level",
    )

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file."""
        return self.data_dir / "state.db"

    @property
    def cache_dir(self) -> Path:
        """Path to the cache directory."""
        return self.data_dir / "cache"

    @property
    def reports_dir(self) -> Path:
        """Path to the reports directory."""
        return self.data_dir / "reports"

    def ensure_directories(self) -> None:
        """Create necessary directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """Get the application settings singleton."""
    return Settings()
