"""Core sync engine for playlist synchronization."""

import re
import traceback
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from song_automations.clients.discogs import DiscogsClient, Folder, Release, Track
from song_automations.config import Settings
from song_automations.matching.isrc import IsrcFinder
from song_automations.matching.matcher import PLAYABLE, Match, SourceTrack, Tier, normalize_isrc
from song_automations.matching.resolver import (
    SoundCloudSearcher,
    SpotifySearcher,
    resolve_release,
)
from song_automations.snapshots import name_of
from song_automations.state.tracker import GATED, Destination, StateTracker

MISSING_RETRY_DAYS = 7
EXACT = Tier.EXACT.name.lower()


def _duration_ms(text: str) -> int | None:
    """Reads a Discogs duration.

    Args:
        text: Duration such as 6:32.

    Returns:
        Milliseconds, or None when absent or malformed.
    """
    found = re.match(r"^(\d+):(\d{2})$", (text or "").strip())
    return (int(found[1]) * 60 + int(found[2])) * 1000 if found else None


def _first_artist(track: Track) -> str:
    """Picks the artist name worth typing into a search box.

    Args:
        track: Discogs track.

    Returns:
        First credited name.
    """
    return track.artist_names[0] if track.artist_names else track.artist


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
        tracks_gated: Missing tracks that exist only as uploads the account cannot play in full.
    """

    operations: list[SyncOperation] = field(default_factory=list)
    playlists_created: int = 0
    playlists_deleted: int = 0
    tracks_added: int = 0
    tracks_removed: int = 0
    tracks_missing: int = 0
    tracks_flagged: int = 0
    tracks_gated: int = 0


class PlaylistClient(Protocol):
    """Protocol for playlist management clients."""

    def search_tracks(self, query: str, limit: int = 10) -> list: ...

    def find_playlist_by_name(self, name: str): ...

    def create_playlist(self, name: str, description: str = "", public: bool = True): ...

    def delete_playlist(self, playlist_id) -> None: ...

    def get_playlist_tracks(self, playlist_id) -> list: ...

    def add_tracks_to_playlist(self, playlist_id, track_ids: list) -> None: ...

    def remove_tracks_from_playlist(self, playlist_id, track_ids: list) -> None: ...

    def search_raw(self, query: str, *args, **kwargs) -> list: ...


class SyncEngine:
    """Engine for syncing Discogs folders to playlists.

    Args:
        settings: Application settings.
        discogs_client: Discogs API client.
        state_tracker: State tracker for caching.
        console: Rich console for output.
        isrc_finder: Looks up recording codes for SoundCloud matching, optional.
    """

    def __init__(
        self,
        settings: Settings,
        discogs_client: DiscogsClient,
        state_tracker: StateTracker,
        console: Console | None = None,
        isrc_finder: IsrcFinder | None = None,
    ) -> None:
        self._settings = settings
        self._discogs = discogs_client
        self._state = state_tracker
        self._console = console or Console()
        self._isrc_finder = isrc_finder

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
        sync_id = str(uuid.uuid4())

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=self._console,
            ) as progress:
                task = progress.add_task("Fetching Discogs folders...", total=None)

                folders = self._discogs.get_folders()
                all_folder_ids = {f.id for f in folders} | {DiscogsClient.WANTLIST_FOLDER_ID}
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

                self._state.log_sync_event(
                    sync_id=sync_id,
                    destination=destination,
                    event_type="sync_start",
                    status="info",
                    message=f"Starting sync to {destination} with {len(folders)} folders",
                    details={"folder_count": len(folders), "dry_run": dry_run},
                )

                progress.update(task, description=f"Processing {len(folders)} folders...")

                for folder in folders:
                    folder_result = self._sync_folder(
                        folder=folder,
                        playlist_client=playlist_client,
                        destination=destination,
                        dry_run=dry_run,
                        progress=progress,
                        sync_id=sync_id,
                    )
                    result.operations.extend(folder_result.operations)
                    result.playlists_created += folder_result.playlists_created
                    result.tracks_added += folder_result.tracks_added
                    result.tracks_removed += folder_result.tracks_removed
                    result.tracks_missing += folder_result.tracks_missing
                    result.tracks_flagged += folder_result.tracks_flagged
                    result.tracks_gated += folder_result.tracks_gated

                progress.update(task, description="Checking for deleted folders...")
                deleted_result = self._cleanup_deleted_folders(
                    playlist_client=playlist_client,
                    destination=destination,
                    current_folder_ids=all_folder_ids,
                    dry_run=dry_run,
                    sync_id=sync_id,
                )
                result.operations.extend(deleted_result.operations)
                result.playlists_deleted += deleted_result.playlists_deleted

            self._state.log_sync_event(
                sync_id=sync_id,
                destination=destination,
                event_type="sync_complete",
                status="info",
                message=f"Sync completed: {result.tracks_added} added, {result.tracks_missing} missing",
                details={
                    "playlists_created": result.playlists_created,
                    "playlists_deleted": result.playlists_deleted,
                    "tracks_added": result.tracks_added,
                    "tracks_removed": result.tracks_removed,
                    "tracks_missing": result.tracks_missing,
                    "tracks_flagged": result.tracks_flagged,
                },
            )

        except Exception as e:
            self._state.log_sync_event(
                sync_id=sync_id,
                destination=destination,
                event_type="exception",
                status="error",
                message=f"Sync failed: {str(e)}",
                details={"error": str(e), "traceback": traceback.format_exc()},
            )
            raise

        return result

    def _sync_folder(
        self,
        folder: Folder,
        playlist_client: PlaylistClient,
        destination: Destination,
        dry_run: bool,
        progress: Progress,
        sync_id: str,
    ) -> SyncResult:
        """Sync a single folder to a playlist.

        Args:
            folder: Discogs folder to sync.
            playlist_client: Platform client.
            destination: Target platform.
            dry_run: If True, don't make changes.
            progress: Progress bar.
            sync_id: UUID identifying the sync run.

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

        self._state.log_sync_event(
            sync_id=sync_id,
            destination=destination,
            folder_id=folder.id,
            folder_name=folder.name,
            event_type="folder_start",
            status="info",
            message=f"Processing folder '{folder.name}' with {len(releases)} releases",
            details={"release_count": len(releases)},
        )

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
                self._state.log_sync_event(
                    sync_id=sync_id,
                    destination=destination,
                    folder_id=folder.id,
                    folder_name=folder.name,
                    event_type="playlist_created",
                    status="success",
                    message=f"Created playlist '{playlist_name}'",
                    details={"playlist_id": str(playlist.id)},
                )
            result.playlists_created += 1

        searcher = self._make_searcher(playlist_client, destination)
        desired: dict[str, None] = {}

        for release in releases:
            task = progress.add_task(
                f"  Processing: {release.artist} - {release.title}",
                total=None,
            )

            tracks = self._discogs.get_release_tracks(release.id)

            if not tracks:
                self._state.log_sync_event(
                    sync_id=sync_id,
                    destination=destination,
                    folder_id=folder.id,
                    folder_name=folder.name,
                    event_type="api_error",
                    status="warning",
                    message=f"Release '{release.artist} - {release.title}' not found (deleted or merged on Discogs)",
                    details={"release_id": release.id, "release_url": f"https://www.discogs.com/release/{release.id}"},
                )
                progress.remove_task(task)
                continue

            for track, track_id, confidence, tier in self._match_release(
                release, tracks, searcher, destination
            ):
                if track_id:
                    desired[track_id] = None
                    flagged = tier != EXACT
                    result.tracks_flagged += flagged
                    result.operations.append(
                        SyncOperation(
                            operation_type=OperationType.ADD_TRACK,
                            folder_name=folder.name,
                            playlist_name=playlist_name,
                            track_title=track.title,
                            track_artist=track.artist,
                            confidence=confidence,
                            flagged=flagged,
                        )
                    )
                else:
                    result.tracks_missing += 1
                    result.tracks_gated += tier == GATED
                    self._state.save_missing_track(
                        discogs_release_id=release.id,
                        discogs_folder_id=folder.id,
                        artist=track.artist,
                        track_name=track.title,
                        destination=destination,
                    )

            progress.remove_task(task)

        if playlist:
            self._apply_playlist(playlist, list(desired), playlist_client, destination, dry_run, result)

        if not dry_run:
            self._state.update_folder_releases(folder.id, [r.id for r in releases])

        return result

    def _make_searcher(self, playlist_client: PlaylistClient, destination: Destination):
        """Builds the searcher of a platform.

        Args:
            playlist_client: Platform client.
            destination: Target platform.

        Returns:
            The searcher.
        """
        if destination == "spotify":
            return SpotifySearcher(playlist_client, self._state)
        return SoundCloudSearcher(
            playlist_client, self._state, allow_gated=self._settings.soundcloud_gated_tracks
        )

    def _match_release(
        self,
        release: Release,
        tracks: list[Track],
        searcher,
        destination: Destination,
    ) -> list[tuple[Track, str | None, float, str | None]]:
        """Matches the tracks of a release, reusing stored decisions.

        A track left without a match is looked up once more with gated uploads
        allowed, and counts as gated only when that finds an upload the
        account cannot play in full. Uploads already given to other tracks of
        the release are left out of that second look.

        Args:
            release: Parent release.
            tracks: Tracks of the release.
            searcher: Platform searcher.
            destination: Target platform.

        Returns:
            For each track: the track, the chosen id, confidence and tier.
        """
        stored = {
            track.position: self._state.get_cached_match(release.id, track.position, destination)
            for track in tracks
        }
        known = {
            position: row
            for position, row in stored.items()
            if row is not None and self._is_fresh(row, destination)
        }
        pending = [track for track in tracks if track.position not in known]
        outcomes: dict[str, tuple[str | None, float, str | None]] = {
            position: (row.destination_track_id, row.match_confidence, row.match_tier or EXACT)
            for position, row in known.items()
        }
        if pending:
            taken = {row.destination_track_id for row in known.values() if row.destination_track_id}
            excluded = [
                self._state.get_rejected_ids(release.id, track.position, destination) | taken
                for track in pending
            ]
            sources = [self._to_source(track, release, destination) for track in pending]

            def resolve(
                using, indexes: list[int], used: frozenset[str] = frozenset()
            ) -> list[Match | None]:
                """Matches some of the pending tracks.

                Args:
                    using: Searcher to use.
                    indexes: Positions in the pending list.
                    used: Candidate ids that already belong to other tracks.

                Returns:
                    One entry per index.
                """
                return resolve_release(
                    using,
                    [sources[i] for i in indexes],
                    [_first_artist(pending[i]) for i in indexes],
                    pending[0].release_artist,
                    release.title,
                    pending[0].labels,
                    excluded=[excluded[i] | used for i in indexes],
                    alias_lookup=lambda index: self._aliases(pending[indexes[index]]),
                )

            matches = resolve(searcher, list(range(len(pending))))
            unmatched = [i for i, match in enumerate(matches) if match is None]
            gated: set[int] = set()
            if unmatched and hasattr(searcher, "including_gated") and not self._settings.soundcloud_gated_tracks:
                used = frozenset(match.candidate.id for match in matches if match)
                found = resolve(searcher.including_gated(), unmatched, used)
                gated = {
                    i
                    for i, match in zip(unmatched, found, strict=True)
                    if match and match.candidate.access != PLAYABLE
                }
            for index, (track, match) in enumerate(zip(pending, matches, strict=True)):
                tier = match.tier.name.lower() if match else GATED if index in gated else None
                self._save_decision(release, track, match, destination, tier)
                outcomes[track.position] = (
                    (match.candidate.id, match.confidence, tier) if match else (None, 0.0, tier)
                )
        return [(track, *outcomes[track.position]) for track in tracks]

    def _is_fresh(self, row, destination: Destination) -> bool:
        """Decides whether a stored decision can be reused.

        A found track or a reviewed one is kept. A track that was not found is
        searched again after a week, because catalogues change, and at once
        when it was only missing for being gated and gated tracks are now allowed.

        Args:
            row: Stored decision.
            destination: Target platform.

        Returns:
            True when the decision can be reused.
        """
        if row.destination_track_id:
            return True
        if row.match_tier == GATED and destination == "soundcloud" and self._settings.soundcloud_gated_tracks:
            return False
        age = datetime.now() - row.searched_at
        return age < timedelta(days=MISSING_RETRY_DAYS)

    def _aliases(self, track: Track) -> tuple[str, ...]:
        """Lists the other names of the artists of a track.

        Args:
            track: Discogs track.

        Returns:
            Other names, without repeats.
        """
        names: list[str] = []
        for artist_id in track.artist_ids:
            names += self._discogs.get_artist_names(artist_id)
        return tuple(dict.fromkeys(names))

    def _to_source(self, track: Track, release: Release, destination: Destination) -> SourceTrack:
        """Prepares a Discogs track for matching.

        On SoundCloud the recording codes of the track are looked up first, so
        the official upload is recognised whatever its account is called. The
        code of an exact Spotify match counts too, which covers tracks the
        catalogue lacks. Fan uploads are allowed only for tracks no catalogue
        carries, which keeps covers out.

        Args:
            track: Discogs track.
            release: Parent release.
            destination: Target platform.

        Returns:
            The source track.
        """
        source = SourceTrack(
            artists=track.artist_names or (track.artist,),
            title=track.title,
            duration_ms=_duration_ms(track.duration),
            release_title=release.title,
            labels=track.labels,
            remixers=track.remixers,
        )
        isrcs: dict[str, int | None] = {}
        if destination == "soundcloud":
            carried = self._state.get_exact_spotify_recording(release.id, track.position, track.title)
            code = normalize_isrc(carried[0]) if carried else ""
            if code:
                isrcs[code] = carried[1]
            if self._isrc_finder is not None:
                isrcs.update(self._isrc_finder.find(source, _first_artist(track)))
        released = bool(isrcs) or self._state.has_exact_spotify_match(release.id, track.position)
        return replace(source, isrcs=isrcs, fan_uploads=not released)

    def _save_decision(
        self,
        release: Release,
        track: Track,
        match: Match | None,
        destination: Destination,
        tier: str | None,
    ) -> None:
        """Stores a decision with its evidence.

        Args:
            release: Parent release.
            track: Discogs track.
            match: Chosen match, or None.
            destination: Target platform.
            tier: Version tier of the match, or gated for a missing track that
                exists only as an upload the account cannot play in full.
        """
        self._state.save_matched_track(
            discogs_release_id=release.id,
            track_position=track.position,
            artist=track.artist,
            track_name=track.title,
            destination=destination,
            destination_track_id=match.candidate.id if match else None,
            match_confidence=match.confidence if match else 0.0,
            match_tier=tier,
            matched_title=match.candidate.raw_title if match else None,
            matched_uploader=match.candidate.uploader if match else None,
            match_reading=match.reading.kind if match else None,
            matched_duration_ms=match.candidate.duration_ms if match else None,
            matched_isrc=(match.candidate.isrc or None) if match else None,
        )

    def _apply_playlist(
        self,
        playlist,
        desired: list[str],
        playlist_client: PlaylistClient,
        destination: Destination,
        dry_run: bool,
        result: SyncResult,
    ) -> None:
        """Makes a playlist hold exactly the chosen tracks, each one once.

        Args:
            playlist: Target playlist.
            desired: Chosen ids in playlist order, without repeats.
            playlist_client: Platform client.
            destination: Target platform.
            dry_run: If True, only count what would change.
            result: Result to update.
        """
        current = [str(track.id) for track in playlist_client.get_playlist_tracks(playlist.id)]
        if current and not desired:
            self._console.print(
                f"  [yellow]Nothing matched for '{name_of(playlist)}'; leaving the playlist alone.[/yellow]"
            )
            return
        wanted = set(desired)
        repeated = {track_id for track_id in current if current.count(track_id) > 1}
        to_add = [track_id for track_id in desired if track_id not in set(current)]
        to_remove = [track_id for track_id in dict.fromkeys(current) if track_id not in wanted]
        result.tracks_added += len(to_add)
        result.tracks_removed += len(to_remove)
        if dry_run or not (to_add or to_remove or repeated):
            return
        if destination == "spotify":
            uri = "spotify:track:{}".format
            drop = to_remove + [track_id for track_id in repeated if track_id in wanted]
            if drop:
                playlist_client.remove_tracks_from_playlist(playlist.id, [uri(t) for t in drop])
            restore = to_add + [track_id for track_id in repeated if track_id in wanted]
            if restore:
                playlist_client.add_tracks_to_playlist(playlist.id, [uri(t) for t in restore])
        else:
            playlist_client.set_playlist_tracks(playlist.id, [int(t) for t in desired])

    def _cleanup_deleted_folders(
        self,
        playlist_client: PlaylistClient,
        destination: Destination,
        current_folder_ids: set[int],
        dry_run: bool,
        sync_id: str,
    ) -> SyncResult:
        """Delete playlists for folders that no longer exist.

        Args:
            playlist_client: Platform client.
            destination: Target platform.
            current_folder_ids: Set of current folder IDs.
            dry_run: If True, don't make changes.
            sync_id: UUID identifying the sync run.

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
                    self._state.log_sync_event(
                        sync_id=sync_id,
                        destination=destination,
                        folder_id=mapping.discogs_folder_id,
                        folder_name=mapping.discogs_folder_name,
                        event_type="playlist_deleted",
                        status="success",
                        message=f"Deleted playlist '{mapping.playlist_name}' (folder no longer exists)",
                    )

                result.playlists_deleted += 1

        return result
