"""SQLite-based state tracking for sync operations."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

Destination = Literal["spotify", "soundcloud"]
EventType = Literal[
    "sync_start",
    "sync_complete",
    "folder_start",
    "folder_complete",
    "track_matched",
    "track_flagged",
    "track_missing",
    "playlist_created",
    "playlist_updated",
    "playlist_deleted",
    "api_error",
    "rate_limit",
    "exception",
]
LogStatus = Literal["info", "success", "warning", "error"]


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


@dataclass
class SyncLog:
    """Represents a sync event log entry.

    Args:
        id: Database row ID.
        sync_id: UUID identifying the sync run.
        destination: Target platform.
        folder_id: Discogs folder ID (optional).
        folder_name: Discogs folder name (optional).
        event_type: Type of event.
        status: Event status (info, success, warning, error).
        track_artist: Track artist (optional).
        track_name: Track name (optional).
        track_confidence: Match confidence score (optional).
        message: Human-readable message.
        details: Additional JSON details.
        created_at: When the event occurred.
    """

    id: int
    sync_id: str
    destination: str
    folder_id: int | None
    folder_name: str | None
    event_type: str
    status: str
    track_artist: str | None
    track_name: str | None
    track_confidence: float | None
    message: str | None
    details: dict[str, Any] | None
    created_at: datetime


@dataclass
class SyncSummary:
    """Aggregate statistics for a sync run.

    Args:
        sync_id: UUID identifying the sync run.
        destination: Target platform.
        started_at: When the sync started.
        completed_at: When the sync completed.
        total_events: Total number of events.
        success_count: Number of successful events.
        warning_count: Number of warning events.
        error_count: Number of error events.
        tracks_matched: Number of tracks matched.
        tracks_flagged: Number of tracks flagged for review.
        tracks_missing: Number of tracks not found.
        playlists_created: Number of playlists created.
        folders_processed: Number of folders processed.
    """

    sync_id: str
    destination: str
    started_at: datetime | None
    completed_at: datetime | None
    total_events: int
    success_count: int
    warning_count: int
    error_count: int
    tracks_matched: int
    tracks_flagged: int
    tracks_missing: int
    playlists_created: int
    folders_processed: int


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

                CREATE TABLE IF NOT EXISTS sync_logs (
                    id INTEGER PRIMARY KEY,
                    sync_id TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    folder_id INTEGER,
                    folder_name TEXT,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    track_artist TEXT,
                    track_name TEXT,
                    track_confidence REAL,
                    message TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_sync_logs_sync
                    ON sync_logs(sync_id);
                CREATE INDEX IF NOT EXISTS idx_sync_logs_status
                    ON sync_logs(status);
                CREATE INDEX IF NOT EXISTS idx_sync_logs_created
                    ON sync_logs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sync_logs_destination
                    ON sync_logs(destination);
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
        """Get a cached track match (excludes rejected tracks).

        Args:
            discogs_release_id: Discogs release ID.
            track_position: Track position (e.g., A1).
            destination: Target platform.
            max_age_days: Maximum age of cache entry in days (default 30).

        Returns:
            MatchedTrack if found, not expired, and not rejected. None otherwise.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM matched_tracks
                WHERE discogs_release_id = ?
                AND discogs_track_position = ?
                AND destination = ?
                AND searched_at > datetime('now', ?)
                AND (review_status IS NULL OR review_status != 'rejected')
                """,
                (discogs_release_id, track_position, destination, f"-{max_age_days} days"),
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

    def clear_matched_tracks(
        self,
        destination: Destination | None = None,
        preserve_reviewed: bool = True,
    ) -> int:
        """Clear matched tracks cache to force re-matching.

        Args:
            destination: Optional filter by destination (spotify/soundcloud).
            preserve_reviewed: If True, keep tracks that have been approved or
                rejected. Only clears pending/unreviewed tracks.

        Returns:
            Number of records deleted.
        """
        with self._get_connection() as conn:
            if preserve_reviewed:
                review_filter = "AND (review_status IS NULL OR review_status = 'pending')"
            else:
                review_filter = ""

            if destination:
                cursor = conn.execute(
                    f"DELETE FROM matched_tracks WHERE destination = ? {review_filter}",
                    (destination,),
                )
            else:
                cursor = conn.execute(
                    f"DELETE FROM matched_tracks WHERE 1=1 {review_filter}"
                )
            return cursor.rowcount

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

    def update_matched_track(
        self,
        track_id: int,
        destination_track_id: str,
        match_confidence: float,
    ) -> None:
        """Update a matched track with corrected info.

        Args:
            track_id: Database row ID.
            destination_track_id: New platform-specific track ID.
            match_confidence: New match confidence score.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE matched_tracks
                SET destination_track_id = ?, match_confidence = ?, searched_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (destination_track_id, match_confidence, track_id),
            )

    def log_sync_event(
        self,
        sync_id: str,
        destination: Destination,
        event_type: EventType,
        status: LogStatus,
        folder_id: int | None = None,
        folder_name: str | None = None,
        track_artist: str | None = None,
        track_name: str | None = None,
        track_confidence: float | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Log a sync event.

        Args:
            sync_id: UUID identifying the sync run.
            destination: Target platform.
            event_type: Type of event.
            status: Event status.
            folder_id: Discogs folder ID (optional).
            folder_name: Discogs folder name (optional).
            track_artist: Track artist (optional).
            track_name: Track name (optional).
            track_confidence: Match confidence score (optional).
            message: Human-readable message (optional).
            details: Additional JSON details (optional).
        """
        details_json = json.dumps(details) if details else None
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sync_logs
                (sync_id, destination, folder_id, folder_name, event_type, status,
                 track_artist, track_name, track_confidence, message, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sync_id,
                    destination,
                    folder_id,
                    folder_name,
                    event_type,
                    status,
                    track_artist,
                    track_name,
                    track_confidence,
                    message,
                    details_json,
                ),
            )

    def get_sync_logs(
        self,
        destination: Destination | None = None,
        status: LogStatus | None = None,
        sync_id: str | None = None,
        event_type: EventType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SyncLog]:
        """Get sync logs with optional filtering.

        Args:
            destination: Filter by destination platform.
            status: Filter by event status.
            sync_id: Filter by sync run ID.
            event_type: Filter by event type.
            limit: Maximum number of records to return.
            offset: Number of records to skip.

        Returns:
            List of SyncLog objects.
        """
        query = "SELECT * FROM sync_logs WHERE 1=1"
        params: list[Any] = []

        if destination:
            query += " AND destination = ?"
            params.append(destination)
        if status:
            query += " AND status = ?"
            params.append(status)
        if sync_id:
            query += " AND sync_id = ?"
            params.append(sync_id)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()

            return [
                SyncLog(
                    id=row["id"],
                    sync_id=row["sync_id"],
                    destination=row["destination"],
                    folder_id=row["folder_id"],
                    folder_name=row["folder_name"],
                    event_type=row["event_type"],
                    status=row["status"],
                    track_artist=row["track_artist"],
                    track_name=row["track_name"],
                    track_confidence=row["track_confidence"],
                    message=row["message"],
                    details=json.loads(row["details"]) if row["details"] else None,
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            ]

    def get_sync_log_count(
        self,
        destination: Destination | None = None,
        status: LogStatus | None = None,
        sync_id: str | None = None,
        event_type: EventType | None = None,
    ) -> int:
        """Get count of sync logs matching filters.

        Args:
            destination: Filter by destination platform.
            status: Filter by event status.
            sync_id: Filter by sync run ID.
            event_type: Filter by event type.

        Returns:
            Count of matching log entries.
        """
        query = "SELECT COUNT(*) FROM sync_logs WHERE 1=1"
        params: list[Any] = []

        if destination:
            query += " AND destination = ?"
            params.append(destination)
        if status:
            query += " AND status = ?"
            params.append(status)
        if sync_id:
            query += " AND sync_id = ?"
            params.append(sync_id)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

        with self._get_connection() as conn:
            result = conn.execute(query, params).fetchone()
            return result[0] if result else 0

    def get_sync_summary(self, sync_id: str) -> SyncSummary | None:
        """Get aggregate statistics for a sync run.

        Args:
            sync_id: UUID identifying the sync run.

        Returns:
            SyncSummary with aggregate stats, or None if no logs found.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    sync_id,
                    destination,
                    MIN(created_at) as started_at,
                    MAX(created_at) as completed_at,
                    COUNT(*) as total_events,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN status = 'warning' THEN 1 ELSE 0 END) as warning_count,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count,
                    SUM(CASE WHEN event_type = 'track_matched' THEN 1 ELSE 0 END) as tracks_matched,
                    SUM(CASE WHEN event_type = 'track_flagged' THEN 1 ELSE 0 END) as tracks_flagged,
                    SUM(CASE WHEN event_type = 'track_missing' THEN 1 ELSE 0 END) as tracks_missing,
                    SUM(CASE WHEN event_type = 'playlist_created' THEN 1 ELSE 0 END) as playlists_created,
                    COUNT(DISTINCT folder_id) as folders_processed
                FROM sync_logs
                WHERE sync_id = ?
                GROUP BY sync_id, destination
                """,
                (sync_id,),
            ).fetchone()

            if row is None:
                return None

            return SyncSummary(
                sync_id=row["sync_id"],
                destination=row["destination"],
                started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
                completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
                total_events=row["total_events"],
                success_count=row["success_count"],
                warning_count=row["warning_count"],
                error_count=row["error_count"],
                tracks_matched=row["tracks_matched"],
                tracks_flagged=row["tracks_flagged"],
                tracks_missing=row["tracks_missing"],
                playlists_created=row["playlists_created"],
                folders_processed=row["folders_processed"],
            )

    def get_recent_sync_ids(self, limit: int = 20) -> list[tuple[str, str, datetime]]:
        """Get recent sync run IDs with their destination and start time.

        Args:
            limit: Maximum number of sync IDs to return.

        Returns:
            List of (sync_id, destination, started_at) tuples.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT sync_id, destination, MIN(created_at) as started_at
                FROM sync_logs
                GROUP BY sync_id
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            return [
                (row["sync_id"], row["destination"], datetime.fromisoformat(row["started_at"]))
                for row in rows
            ]

    def cleanup_old_logs(self, days: int = 90) -> int:
        """Remove logs older than the specified number of days.

        Args:
            days: Number of days to retain logs.

        Returns:
            Number of records deleted.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM sync_logs
                WHERE created_at < datetime('now', ?)
                """,
                (f"-{days} days",),
            )
            return cursor.rowcount
