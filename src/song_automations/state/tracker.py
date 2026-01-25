"""SQLite-based state tracking for sync operations."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

Destination = Literal["spotify", "soundcloud"]


@dataclass
class FolderMapping:
    """Represents a mapping between a Discogs folder and a playlist.

    Args:
        discogs_folder_id: Discogs folder ID.
        discogs_folder_name: Discogs folder name.
        destination: Target platform.
        playlist_id: Platform-specific playlist ID.
        playlist_name: Playlist name on the platform.
        created_at: When the mapping was created.
    """

    discogs_folder_id: int
    discogs_folder_name: str
    destination: str
    playlist_id: str
    playlist_name: str
    created_at: datetime


@dataclass
class MatchedTrack:
    """Represents a cached track match.

    Args:
        discogs_release_id: Discogs release ID.
        discogs_track_position: Track position (e.g., A1, B2).
        artist: Artist name.
        track_name: Track name.
        destination: Target platform.
        destination_track_id: Platform-specific track ID (None if not found).
        match_confidence: Match confidence score.
        searched_at: When the search was performed.
        review_status: Review status (pending, approved, rejected).
        id: Database row ID.
    """

    discogs_release_id: int
    discogs_track_position: str
    artist: str
    track_name: str
    destination: str
    destination_track_id: str | None
    match_confidence: float
    searched_at: datetime
    review_status: str = "pending"
    id: int | None = None


@dataclass
class MissingTrack:
    """Represents a track that couldn't be found.

    Args:
        discogs_release_id: Discogs release ID.
        discogs_folder_id: Discogs folder ID.
        artist: Artist name.
        track_name: Track name.
        destination: Target platform.
        searched_at: When the search was performed.
    """

    discogs_release_id: int
    discogs_folder_id: int
    artist: str
    track_name: str
    destination: str
    searched_at: datetime


class StateTracker:
    """SQLite-based state tracker for sync operations.

    Args:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Initialize the database schema."""
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS folder_mappings (
                    id INTEGER PRIMARY KEY,
                    discogs_folder_id INTEGER NOT NULL,
                    discogs_folder_name TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    playlist_id TEXT NOT NULL,
                    playlist_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(discogs_folder_id, destination)
                );

                CREATE TABLE IF NOT EXISTS folder_releases (
                    id INTEGER PRIMARY KEY,
                    discogs_folder_id INTEGER NOT NULL,
                    discogs_release_id INTEGER NOT NULL,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(discogs_folder_id, discogs_release_id)
                );

                CREATE TABLE IF NOT EXISTS matched_tracks (
                    id INTEGER PRIMARY KEY,
                    discogs_release_id INTEGER NOT NULL,
                    discogs_track_position TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    track_name TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    destination_track_id TEXT,
                    match_confidence REAL,
                    searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    review_status TEXT DEFAULT 'pending',
                    UNIQUE(discogs_release_id, discogs_track_position, destination)
                );

                CREATE TABLE IF NOT EXISTS missing_tracks (
                    id INTEGER PRIMARY KEY,
                    discogs_release_id INTEGER NOT NULL,
                    discogs_folder_id INTEGER NOT NULL,
                    artist TEXT NOT NULL,
                    track_name TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(discogs_release_id, track_name, destination)
                );

                CREATE INDEX IF NOT EXISTS idx_folder_mappings_destination
                    ON folder_mappings(destination);
                CREATE INDEX IF NOT EXISTS idx_matched_tracks_destination
                    ON matched_tracks(destination);
                CREATE INDEX IF NOT EXISTS idx_missing_tracks_destination
                    ON missing_tracks(destination);
                CREATE INDEX IF NOT EXISTS idx_matched_tracks_lookup
                    ON matched_tracks(discogs_release_id, discogs_track_position, destination);
                CREATE INDEX IF NOT EXISTS idx_folder_releases_folder
                    ON folder_releases(discogs_folder_id);
            """
            )
            self._migrate_review_status(conn)

    def _migrate_review_status(self, conn: sqlite3.Connection) -> None:
        """Add review_status column if it doesn't exist (migration)."""
        cursor = conn.execute("PRAGMA table_info(matched_tracks)")
        columns = [row[1] for row in cursor.fetchall()]
        if "review_status" not in columns:
            conn.execute(
                "ALTER TABLE matched_tracks ADD COLUMN review_status TEXT DEFAULT 'pending'"
            )

    def get_folder_mapping(
        self,
        discogs_folder_id: int,
        destination: Destination,
    ) -> FolderMapping | None:
        """Get the playlist mapping for a Discogs folder.

        Args:
            discogs_folder_id: Discogs folder ID.
            destination: Target platform.

        Returns:
            FolderMapping if found, None otherwise.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM folder_mappings
                WHERE discogs_folder_id = ? AND destination = ?
                """,
                (discogs_folder_id, destination),
            ).fetchone()

            if row is None:
                return None

            return FolderMapping(
                discogs_folder_id=row["discogs_folder_id"],
                discogs_folder_name=row["discogs_folder_name"],
                destination=row["destination"],
                playlist_id=row["playlist_id"],
                playlist_name=row["playlist_name"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )

    def get_all_folder_mappings(
        self,
        destination: Destination,
    ) -> list[FolderMapping]:
        """Get all folder mappings for a destination.

        Args:
            destination: Target platform.

        Returns:
            List of FolderMapping objects.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM folder_mappings WHERE destination = ?
                """,
                (destination,),
            ).fetchall()

            return [
                FolderMapping(
                    discogs_folder_id=row["discogs_folder_id"],
                    discogs_folder_name=row["discogs_folder_name"],
                    destination=row["destination"],
                    playlist_id=row["playlist_id"],
                    playlist_name=row["playlist_name"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            ]

    def save_folder_mapping(
        self,
        discogs_folder_id: int,
        discogs_folder_name: str,
        destination: Destination,
        playlist_id: str,
        playlist_name: str,
    ) -> None:
        """Save a folder-to-playlist mapping.

        Args:
            discogs_folder_id: Discogs folder ID.
            discogs_folder_name: Discogs folder name.
            destination: Target platform.
            playlist_id: Platform-specific playlist ID.
            playlist_name: Playlist name on the platform.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO folder_mappings
                (discogs_folder_id, discogs_folder_name, destination, playlist_id, playlist_name)
                VALUES (?, ?, ?, ?, ?)
                """,
                (discogs_folder_id, discogs_folder_name, destination, playlist_id, playlist_name),
            )

    def delete_folder_mapping(
        self,
        discogs_folder_id: int,
        destination: Destination,
    ) -> None:
        """Delete a folder mapping.

        Args:
            discogs_folder_id: Discogs folder ID.
            destination: Target platform.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                DELETE FROM folder_mappings
                WHERE discogs_folder_id = ? AND destination = ?
                """,
                (discogs_folder_id, destination),
            )

    def update_folder_releases(
        self,
        discogs_folder_id: int,
        release_ids: list[int],
    ) -> None:
        """Update the list of releases in a folder.

        Args:
            discogs_folder_id: Discogs folder ID.
            release_ids: List of release IDs currently in the folder.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                DELETE FROM folder_releases WHERE discogs_folder_id = ?
                """,
                (discogs_folder_id,),
            )

            conn.executemany(
                """
                INSERT INTO folder_releases (discogs_folder_id, discogs_release_id)
                VALUES (?, ?)
                """,
                [(discogs_folder_id, rid) for rid in release_ids],
            )

    def get_folder_release_ids(self, discogs_folder_id: int) -> list[int]:
        """Get all release IDs in a folder from the last sync.

        Args:
            discogs_folder_id: Discogs folder ID.

        Returns:
            List of release IDs.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT discogs_release_id FROM folder_releases
                WHERE discogs_folder_id = ?
                """,
                (discogs_folder_id,),
            ).fetchall()

            return [row["discogs_release_id"] for row in rows]

    def get_cached_match(
        self,
        discogs_release_id: int,
        track_position: str,
        destination: Destination,
        max_age_days: int = 30,
    ) -> MatchedTrack | None:
        """Get a cached track match.

        Args:
            discogs_release_id: Discogs release ID.
            track_position: Track position (e.g., A1).
            destination: Target platform.
            max_age_days: Maximum age of cache entry in days (default 30).

        Returns:
            MatchedTrack if found and not expired, None otherwise.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM matched_tracks
                WHERE discogs_release_id = ?
                AND discogs_track_position = ?
                AND destination = ?
                AND searched_at > datetime('now', ?)
                """,
                (discogs_release_id, track_position, destination, f"-{max_age_days} days"),
            ).fetchone()

            if row is None:
                return None

            return MatchedTrack(
                discogs_release_id=row["discogs_release_id"],
                discogs_track_position=row["discogs_track_position"],
                artist=row["artist"],
                track_name=row["track_name"],
                destination=row["destination"],
                destination_track_id=row["destination_track_id"],
                match_confidence=row["match_confidence"],
                searched_at=datetime.fromisoformat(row["searched_at"]),
            )

    def save_matched_track(
        self,
        discogs_release_id: int,
        track_position: str,
        artist: str,
        track_name: str,
        destination: Destination,
        destination_track_id: str | None,
        match_confidence: float,
    ) -> None:
        """Save a track match to the cache.

        Args:
            discogs_release_id: Discogs release ID.
            track_position: Track position (e.g., A1).
            artist: Artist name.
            track_name: Track name.
            destination: Target platform.
            destination_track_id: Platform-specific track ID (None if not found).
            match_confidence: Match confidence score.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO matched_tracks
                (discogs_release_id, discogs_track_position, artist, track_name,
                 destination, destination_track_id, match_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    discogs_release_id,
                    track_position,
                    artist,
                    track_name,
                    destination,
                    destination_track_id,
                    match_confidence,
                ),
            )

    def save_missing_track(
        self,
        discogs_release_id: int,
        discogs_folder_id: int,
        artist: str,
        track_name: str,
        destination: Destination,
    ) -> None:
        """Record a track that couldn't be found.

        Args:
            discogs_release_id: Discogs release ID.
            discogs_folder_id: Discogs folder ID.
            artist: Artist name.
            track_name: Track name.
            destination: Target platform.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO missing_tracks
                (discogs_release_id, discogs_folder_id, artist, track_name, destination)
                VALUES (?, ?, ?, ?, ?)
                """,
                (discogs_release_id, discogs_folder_id, artist, track_name, destination),
            )

    def get_missing_tracks(
        self,
        destination: Destination | None = None,
    ) -> list[MissingTrack]:
        """Get all missing tracks.

        Args:
            destination: Optional filter by destination.

        Returns:
            List of MissingTrack objects.
        """
        with self._get_connection() as conn:
            if destination:
                rows = conn.execute(
                    """
                    SELECT * FROM missing_tracks WHERE destination = ?
                    ORDER BY searched_at DESC
                    """,
                    (destination,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM missing_tracks ORDER BY searched_at DESC
                    """
                ).fetchall()

            return [
                MissingTrack(
                    discogs_release_id=row["discogs_release_id"],
                    discogs_folder_id=row["discogs_folder_id"],
                    artist=row["artist"],
                    track_name=row["track_name"],
                    destination=row["destination"],
                    searched_at=datetime.fromisoformat(row["searched_at"]),
                )
                for row in rows
            ]

    def clear_missing_tracks(self, destination: Destination | None = None) -> None:
        """Clear missing tracks records.

        Args:
            destination: Optional filter by destination.
        """
        with self._get_connection() as conn:
            if destination:
                conn.execute(
                    "DELETE FROM missing_tracks WHERE destination = ?",
                    (destination,),
                )
            else:
                conn.execute("DELETE FROM missing_tracks")

    def get_matched_track_ids(
        self,
        discogs_release_id: int,
        destination: Destination,
    ) -> list[str]:
        """Get all matched track IDs for a release.

        Args:
            discogs_release_id: Discogs release ID.
            destination: Target platform.

        Returns:
            List of destination track IDs.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT destination_track_id FROM matched_tracks
                WHERE discogs_release_id = ?
                AND destination = ?
                AND destination_track_id IS NOT NULL
                """,
                (discogs_release_id, destination),
            ).fetchall()

            return [row["destination_track_id"] for row in rows]

    def get_flagged_tracks(
        self,
        high_confidence: float = 0.50,
        destination: Destination | None = None,
    ) -> list[MatchedTrack]:
        """Get tracks that need review (confidence below threshold, not yet reviewed).

        Args:
            high_confidence: Threshold below which tracks are flagged.
            destination: Optional filter by destination.

        Returns:
            List of MatchedTrack objects needing review.
        """
        with self._get_connection() as conn:
            if destination:
                rows = conn.execute(
                    """
                    SELECT * FROM matched_tracks
                    WHERE match_confidence < ?
                    AND match_confidence > 0
                    AND destination_track_id IS NOT NULL
                    AND (review_status IS NULL OR review_status = 'pending')
                    AND destination = ?
                    ORDER BY match_confidence ASC
                    """,
                    (high_confidence, destination),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM matched_tracks
                    WHERE match_confidence < ?
                    AND match_confidence > 0
                    AND destination_track_id IS NOT NULL
                    AND (review_status IS NULL OR review_status = 'pending')
                    ORDER BY match_confidence ASC
                    """,
                    (high_confidence,),
                ).fetchall()

            return [
                MatchedTrack(
                    id=row["id"],
                    discogs_release_id=row["discogs_release_id"],
                    discogs_track_position=row["discogs_track_position"],
                    artist=row["artist"],
                    track_name=row["track_name"],
                    destination=row["destination"],
                    destination_track_id=row["destination_track_id"],
                    match_confidence=row["match_confidence"],
                    searched_at=datetime.fromisoformat(row["searched_at"]),
                    review_status=row["review_status"] or "pending",
                )
                for row in rows
            ]

    def get_matched_track_by_id(self, track_id: int) -> MatchedTrack | None:
        """Get a matched track by its database ID.

        Args:
            track_id: Database row ID.

        Returns:
            MatchedTrack if found, None otherwise.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM matched_tracks WHERE id = ?",
                (track_id,),
            ).fetchone()

            if row is None:
                return None

            return MatchedTrack(
                id=row["id"],
                discogs_release_id=row["discogs_release_id"],
                discogs_track_position=row["discogs_track_position"],
                artist=row["artist"],
                track_name=row["track_name"],
                destination=row["destination"],
                destination_track_id=row["destination_track_id"],
                match_confidence=row["match_confidence"],
                searched_at=datetime.fromisoformat(row["searched_at"]),
                review_status=row["review_status"] or "pending",
            )

    def update_review_status(
        self,
        track_id: int,
        status: Literal["pending", "approved", "rejected"],
    ) -> None:
        """Update the review status of a matched track.

        Args:
            track_id: Database row ID.
            status: New review status.
        """
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE matched_tracks SET review_status = ? WHERE id = ?",
                (status, track_id),
            )

    def delete_matched_track(self, track_id: int) -> None:
        """Delete a matched track by its database ID.

        Args:
            track_id: Database row ID.
        """
        with self._get_connection() as conn:
            conn.execute("DELETE FROM matched_tracks WHERE id = ?", (track_id,))
