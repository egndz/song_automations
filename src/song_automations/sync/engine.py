"""Core sync engine for playlist synchronization."""

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from song_automations.clients.discogs import DiscogsClient, Folder, Release, Track
from song_automations.config import Settings
from song_automations.matching.fuzzy import (
    MatchResult,
    parse_track_title,
    score_candidate,
    should_use_fallback,
)
from song_automations.state.tracker import Destination, StateTracker

HIGH_CONFIDENCE_THRESHOLD = 0.95


class OperationType(Enum):
    """Type of sync operation."""

    CREATE_PLAYLIST = "create_playlist"
    DELETE_PLAYLIST = "delete_playlist"
    ADD_TRACK = "add_track"
    REMOVE_TRACK = "remove_track"


@dataclass
class SyncOperation:
    """Represents a single sync operation.

    Args:
        operation_type: Type of operation.
        folder_name: Discogs folder name.
        playlist_name: Target playlist name.
        track_title: Track title (for track operations).
        track_artist: Track artist (for track operations).
        confidence: Match confidence (for add operations).
        flagged: Whether the match is flagged for review.
    """

    operation_type: OperationType
    folder_name: str
    playlist_name: str
    track_title: str = ""
    track_artist: str = ""
    confidence: float = 0.0
    flagged: bool = False


@dataclass
class SyncResult:
    """Result of a sync operation.

    Args:
        operations: List of operations performed.
        playlists_created: Number of playlists created.
        playlists_deleted: Number of playlists deleted.
        tracks_added: Number of tracks added.
        tracks_removed: Number of tracks removed.
        tracks_missing: Number of tracks not found.
        tracks_flagged: Number of tracks flagged for review.
    """

    operations: list[SyncOperation] = field(default_factory=list)
    playlists_created: int = 0
    playlists_deleted: int = 0
    tracks_added: int = 0
    tracks_removed: int = 0
    tracks_missing: int = 0
    tracks_flagged: int = 0


class PlaylistClient(Protocol):
    """Protocol for playlist management clients."""

    def search_tracks(self, query: str, limit: int = 10) -> list: ...

    def find_playlist_by_name(self, name: str): ...

    def create_playlist(self, name: str, description: str = "", public: bool = True): ...

    def delete_playlist(self, playlist_id) -> None: ...

    def get_playlist_tracks(self, playlist_id) -> list: ...

    def add_tracks_to_playlist(self, playlist_id, track_ids: list) -> None: ...

    def remove_tracks_from_playlist(self, playlist_id, track_ids: list) -> None: ...


class SyncEngine:
    """Engine for syncing Discogs folders to playlists.

    Args:
        settings: Application settings.
        discogs_client: Discogs API client.
        state_tracker: State tracker for caching.
        console: Rich console for output.
    """

    def __init__(
        self,
        settings: Settings,
        discogs_client: DiscogsClient,
        state_tracker: StateTracker,
        console: Console | None = None,
    ) -> None:
        self._settings = settings
        self._discogs = discogs_client
        self._state = state_tracker
        self._console = console or Console()

    def sync_to_spotify(
        self,
        playlist_client: PlaylistClient,
        include_wantlist: bool = True,
        folder_names: list[str] | None = None,
        dry_run: bool = False,
    ) -> SyncResult:
        """Sync Discogs folders to Spotify playlists.

        Args:
            playlist_client: Spotify client.
            include_wantlist: Whether to include wantlist.
            folder_names: Optional list of folder names to sync.
            dry_run: If True, don't actually make changes.

        Returns:
            SyncResult with operation details.
        """
        return self._sync(
            playlist_client=playlist_client,
            destination="spotify",
            include_wantlist=include_wantlist,
            folder_names=folder_names,
            dry_run=dry_run,
        )

    def sync_to_soundcloud(
        self,
        playlist_client: PlaylistClient,
        include_wantlist: bool = True,
        folder_names: list[str] | None = None,
        dry_run: bool = False,
    ) -> SyncResult:
        """Sync Discogs folders to SoundCloud playlists.

        Args:
            playlist_client: SoundCloud client.
            include_wantlist: Whether to include wantlist.
            folder_names: Optional list of folder names to sync.
            dry_run: If True, don't actually make changes.

        Returns:
            SyncResult with operation details.
        """
        return self._sync(
            playlist_client=playlist_client,
            destination="soundcloud",
            include_wantlist=include_wantlist,
            folder_names=folder_names,
            dry_run=dry_run,
        )

    def _sync(
        self,
        playlist_client: PlaylistClient,
        destination: Destination,
        include_wantlist: bool,
        folder_names: list[str] | None,
        dry_run: bool,
    ) -> SyncResult:
        """Internal sync implementation.

        Args:
            playlist_client: Platform client.
            destination: Target platform.
            include_wantlist: Whether to include wantlist.
            folder_names: Optional folder name filter.
            dry_run: If True, don't make changes.

        Returns:
            SyncResult with operation details.
        """
        result = SyncResult()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self._console,
        ) as progress:
            task = progress.add_task("Fetching Discogs folders...", total=None)

            folders = self._discogs.get_folders()
            if folder_names:
                folders = [f for f in folders if f.name in folder_names]

            if include_wantlist:
                folders.append(
                    Folder(
                        id=DiscogsClient.WANTLIST_FOLDER_ID,
                        name=DiscogsClient.WANTLIST_FOLDER_NAME,
                        count=0,
                    )
                )

            progress.update(task, description=f"Processing {len(folders)} folders...")

            for folder in folders:
                folder_result = self._sync_folder(
                    folder=folder,
                    playlist_client=playlist_client,
                    destination=destination,
                    dry_run=dry_run,
                    progress=progress,
                )
                result.operations.extend(folder_result.operations)
                result.playlists_created += folder_result.playlists_created
                result.tracks_added += folder_result.tracks_added
                result.tracks_removed += folder_result.tracks_removed
                result.tracks_missing += folder_result.tracks_missing
                result.tracks_flagged += folder_result.tracks_flagged

            progress.update(task, description="Checking for deleted folders...")
            deleted_result = self._cleanup_deleted_folders(
                playlist_client=playlist_client,
                destination=destination,
                current_folder_ids={f.id for f in folders},
                dry_run=dry_run,
            )
            result.operations.extend(deleted_result.operations)
            result.playlists_deleted += deleted_result.playlists_deleted

        return result

    def _sync_folder(
        self,
        folder: Folder,
        playlist_client: PlaylistClient,
        destination: Destination,
        dry_run: bool,
        progress: Progress,
    ) -> SyncResult:
        """Sync a single folder to a playlist.

        Args:
            folder: Discogs folder to sync.
            playlist_client: Platform client.
            destination: Target platform.
            dry_run: If True, don't make changes.
            progress: Progress bar.

        Returns:
            SyncResult for this folder.
        """
        result = SyncResult()
        playlist_name = f"{self._settings.playlist_prefix}{folder.name}"

        progress.console.print(f"[bold]Syncing folder:[/bold] {folder.name}")

        if folder.id == DiscogsClient.WANTLIST_FOLDER_ID:
            releases = list(self._discogs.get_wantlist_releases())
        else:
            releases = list(self._discogs.get_folder_releases(folder.id))

        if not releases:
            progress.console.print("  [dim]No releases in folder[/dim]")
            return result

        mapping = self._state.get_folder_mapping(folder.id, destination)
        playlist = None

        if mapping:
            playlist = playlist_client.find_playlist_by_name(mapping.playlist_name)

        if playlist is None:
            result.operations.append(
                SyncOperation(
                    operation_type=OperationType.CREATE_PLAYLIST,
                    folder_name=folder.name,
                    playlist_name=playlist_name,
                )
            )
            if not dry_run:
                playlist = playlist_client.create_playlist(
                    name=playlist_name,
                    description=f"Synced from Discogs folder: {folder.name}",
                )
                self._state.save_folder_mapping(
                    discogs_folder_id=folder.id,
                    discogs_folder_name=folder.name,
                    destination=destination,
                    playlist_id=str(playlist.id),
                    playlist_name=playlist_name,
                )
            result.playlists_created += 1

        desired_track_ids: set[str] = set()
        tracks_to_add: list[tuple[str, float, bool]] = []

        def process_release(rel: Release) -> tuple[set[str], list, list, int, int]:
            task = progress.add_task(
                f"  Processing: {rel.artist} - {rel.title}",
                total=None,
            )
            local_ids: set[str] = set()
            local_adds: list[tuple[str, float, bool]] = []
            local_ops: list[SyncOperation] = []
            local_missing = 0
            local_flagged = 0

            tracks = self._discogs.get_release_tracks(rel.id)

            for track in tracks:
                match_result = self._find_track_match(
                    track=track,
                    release=rel,
                    playlist_client=playlist_client,
                    destination=destination,
                    folder_id=folder.id,
                )

                if match_result:
                    track_id = str(match_result.track_id)
                    local_ids.add(track_id)

                    flagged = match_result.confidence < self._settings.high_confidence
                    local_adds.append((track_id, match_result.confidence, flagged))

                    local_ops.append(
                        SyncOperation(
                            operation_type=OperationType.ADD_TRACK,
                            folder_name=folder.name,
                            playlist_name=playlist_name,
                            track_title=track.title,
                            track_artist=track.artist,
                            confidence=match_result.confidence,
                            flagged=flagged,
                        )
                    )

                    if flagged:
                        local_flagged += 1
                else:
                    local_missing += 1
                    self._state.save_missing_track(
                        discogs_release_id=rel.id,
                        discogs_folder_id=folder.id,
                        artist=track.artist,
                        track_name=track.title,
                        destination=destination,
                    )

            progress.remove_task(task)
            return local_ids, local_adds, local_ops, local_missing, local_flagged

        with ThreadPoolExecutor(max_workers=self._settings.max_workers) as executor:
            futures = [executor.submit(process_release, rel) for rel in releases]
            for future in as_completed(futures):
                ids, adds, ops, missing, flagged = future.result()
                desired_track_ids.update(ids)
                tracks_to_add.extend(adds)
                result.operations.extend(ops)
                result.tracks_missing += missing
                result.tracks_flagged += flagged

        if playlist and not dry_run:
            current_tracks = playlist_client.get_playlist_tracks(playlist.id)

            if destination == "spotify":
                current_track_ids = {t.id for t in current_tracks}
                ids_to_add = [
                    tid for tid, _, _ in tracks_to_add if tid not in current_track_ids
                ]
                ids_to_remove = [tid for tid in current_track_ids if tid not in desired_track_ids]

                if ids_to_add:
                    uris = [f"spotify:track:{tid}" for tid in ids_to_add]
                    playlist_client.add_tracks_to_playlist(playlist.id, uris)
                    result.tracks_added += len(ids_to_add)

                if ids_to_remove:
                    uris = [f"spotify:track:{tid}" for tid in ids_to_remove]
                    playlist_client.remove_tracks_from_playlist(playlist.id, uris)
                    result.tracks_removed += len(ids_to_remove)

            else:
                current_track_ids = {t.id for t in current_tracks}
                int_desired = {int(tid) for tid in desired_track_ids}
                ids_to_add = [int(tid) for tid, _, _ in tracks_to_add if int(tid) not in current_track_ids]
                ids_to_remove = [tid for tid in current_track_ids if tid not in int_desired]

                if ids_to_add:
                    playlist_client.add_tracks_to_playlist(playlist.id, ids_to_add)
                    result.tracks_added += len(ids_to_add)

                if ids_to_remove:
                    playlist_client.remove_tracks_from_playlist(playlist.id, ids_to_remove)
                    result.tracks_removed += len(ids_to_remove)

        self._state.update_folder_releases(folder.id, [r.id for r in releases])

        return result

    def _find_track_match(
        self,
        track: Track,
        release: Release,
        playlist_client: PlaylistClient,
        destination: Destination,
        folder_id: int,
    ) -> MatchResult | None:
        """Find a matching track on the target platform.

        Args:
            track: Discogs track.
            release: Parent release.
            playlist_client: Platform client.
            destination: Target platform.
            folder_id: Discogs folder ID.

        Returns:
            MatchResult if found, None otherwise.
        """
        cached = self._state.get_cached_match(release.id, track.position, destination)
        if cached and cached.destination_track_id:
            return MatchResult(
                track_id=cached.destination_track_id,
                track_uri="",
                confidence=cached.match_confidence,
                artist_score=0.0,
                title_score=0.0,
                verified_bonus=0.0,
                popularity_score=0.0,
                is_verified=False,
                matched_title=cached.track_name,
                matched_artist=cached.artist,
            )

        parsed = parse_track_title(track.title, track.artist)

        search_results = playlist_client.search_tracks(parsed.search_query, limit=10)

        if not search_results and should_use_fallback(parsed):
            search_results = playlist_client.search_tracks(parsed.fallback_query, limit=10)

        if not search_results:
            self._state.save_matched_track(
                discogs_release_id=release.id,
                track_position=track.position,
                artist=track.artist,
                track_name=track.title,
                destination=destination,
                destination_track_id=None,
                match_confidence=0.0,
            )
            return None

        best_match: MatchResult | None = None
        best_score = 0.0

        for search_result in search_results:
            result_track = search_result.track

            if destination == "spotify":
                candidate_title = result_track.name
                candidate_artist = result_track.artist
                popularity = result_track.popularity
                max_pop = 100
                is_verified = search_result.is_verified
            else:
                candidate_title = result_track.title
                candidate_artist = result_track.artist
                raw_plays = result_track.playback_count or 0
                popularity = int(math.log10(raw_plays + 1) / 6 * 100)
                max_pop = 100
                is_verified = False

            total_score, artist_score, title_score, verified_bonus, pop_score = score_candidate(
                parsed_track=parsed,
                candidate_title=candidate_title,
                candidate_artist=candidate_artist,
                is_verified=is_verified,
                popularity=popularity,
                max_popularity=max_pop,
                artist_weight=self._settings.artist_weight,
                title_weight=self._settings.title_weight,
                verified_weight=self._settings.verified_weight,
                popularity_weight=self._settings.popularity_weight,
                version_bonus_weight=self._settings.version_match_bonus,
            )

            if total_score > best_score:
                best_score = total_score

                if destination == "spotify":
                    track_uri = result_track.uri
                    track_id = result_track.id
                else:
                    track_uri = result_track.permalink_url
                    track_id = result_track.id

                best_match = MatchResult(
                    track_id=track_id,
                    track_uri=track_uri,
                    confidence=total_score,
                    artist_score=artist_score,
                    title_score=title_score,
                    verified_bonus=verified_bonus,
                    popularity_score=pop_score,
                    is_verified=search_result.is_verified,
                    matched_title=candidate_title,
                    matched_artist=candidate_artist,
                )

                if total_score >= HIGH_CONFIDENCE_THRESHOLD:
                    break

        if best_match and best_match.confidence >= self._settings.min_confidence:
            self._state.save_matched_track(
                discogs_release_id=release.id,
                track_position=track.position,
                artist=track.artist,
                track_name=track.title,
                destination=destination,
                destination_track_id=str(best_match.track_id),
                match_confidence=best_match.confidence,
            )
            return best_match

        self._state.save_matched_track(
            discogs_release_id=release.id,
            track_position=track.position,
            artist=track.artist,
            track_name=track.title,
            destination=destination,
            destination_track_id=None,
            match_confidence=best_score,
        )
        return None

    def _cleanup_deleted_folders(
        self,
        playlist_client: PlaylistClient,
        destination: Destination,
        current_folder_ids: set[int],
        dry_run: bool,
    ) -> SyncResult:
        """Delete playlists for folders that no longer exist.

        Args:
            playlist_client: Platform client.
            destination: Target platform.
            current_folder_ids: Set of current folder IDs.
            dry_run: If True, don't make changes.

        Returns:
            SyncResult with deletion operations.
        """
        result = SyncResult()

        mappings = self._state.get_all_folder_mappings(destination)

        for mapping in mappings:
            if mapping.discogs_folder_id not in current_folder_ids:
                result.operations.append(
                    SyncOperation(
                        operation_type=OperationType.DELETE_PLAYLIST,
                        folder_name=mapping.discogs_folder_name,
                        playlist_name=mapping.playlist_name,
                    )
                )

                if not dry_run:
                    playlist = playlist_client.find_playlist_by_name(mapping.playlist_name)
                    if playlist:
                        playlist_client.delete_playlist(playlist.id)
                    self._state.delete_folder_mapping(mapping.discogs_folder_id, destination)

                result.playlists_deleted += 1

        return result
