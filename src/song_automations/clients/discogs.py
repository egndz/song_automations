"""Discogs API client for fetching collection and wantlist data."""

from collections.abc import Iterator
from dataclasses import dataclass

import discogs_client

from song_automations.config import Settings


@dataclass
class Track:
    """Represents a single track from a Discogs release.

    Args:
        position: Track position on the release (e.g., A1, B2).
        title: Track title including any remix/version info.
        artist: Primary artist name for this track.
        duration: Track duration string if available.
        release_id: Parent release ID.
        release_title: Parent release title.
    """

    position: str
    title: str
    artist: str
    duration: str
    release_id: int
    release_title: str

    @property
    def full_title(self) -> str:
        """Full title including artist for search queries."""
        return f"{self.artist} - {self.title}"


@dataclass
class Folder:
    """Represents a Discogs collection folder.

    Args:
        id: Folder ID.
        name: Folder name.
        count: Number of releases in the folder.
    """

    id: int
    name: str
    count: int


@dataclass
class Release:
    """Represents a Discogs release.

    Args:
        id: Release ID.
        title: Release title.
        artist: Primary artist name.
        year: Release year.
        folder_id: Folder ID this release belongs to.
        folder_name: Folder name this release belongs to.
    """

    id: int
    title: str
    artist: str
    year: int
    folder_id: int
    folder_name: str


class DiscogsClient:
    """Client for interacting with the Discogs API.

    Args:
        settings: Application settings containing the Discogs user token.
    """

    WANTLIST_FOLDER_ID = -1
    WANTLIST_FOLDER_NAME = "Wantlist"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = discogs_client.Client(
            "SongAutomations/0.1.0",
            user_token=settings.discogs_user_token,
        )
        self._user: discogs_client.User | None = None

    @property
    def user(self) -> discogs_client.User:
        """Get the authenticated user, fetching if necessary."""
        if self._user is None:
            self._user = self._client.identity()
        return self._user

    def get_folders(self) -> list[Folder]:
        """Get all collection folders for the authenticated user.

        Returns:
            List of Folder objects representing collection folders.
        """
        folders = []
        for folder in self.user.collection_folders:
            if folder.id == 0:
                continue
            folders.append(
                Folder(
                    id=folder.id,
                    name=folder.name,
                    count=folder.count,
                )
            )
        return folders

    def _get_folder_by_id(self, folder_id: int):
        """Get a collection folder by its ID.

        Args:
            folder_id: The folder ID to find.

        Returns:
            The folder object or None if not found.
        """
        for folder in self.user.collection_folders:
            if folder.id == folder_id:
                return folder
        return None

    def get_folder_releases(self, folder_id: int) -> Iterator[Release]:
        """Get all releases in a specific folder.

        Args:
            folder_id: The folder ID to fetch releases from.

        Yields:
            Release objects for each release in the folder.
        """
        folder = self._get_folder_by_id(folder_id)
        if folder is None:
            return
        for item in folder.releases:
            release = item.release
            artists = self._extract_artists(release.artists)
            yield Release(
                id=release.id,
                title=release.title,
                artist=artists,
                year=release.year or 0,
                folder_id=folder_id,
                folder_name=folder.name,
            )

    def get_wantlist_releases(self) -> Iterator[Release]:
        """Get all releases in the user's wantlist.

        Yields:
            Release objects for each release in the wantlist.
        """
        for item in self.user.wantlist:
            release = item.release
            artists = self._extract_artists(release.artists)
            yield Release(
                id=release.id,
                title=release.title,
                artist=artists,
                year=release.year or 0,
                folder_id=self.WANTLIST_FOLDER_ID,
                folder_name=self.WANTLIST_FOLDER_NAME,
            )

    def get_release_tracks(self, release_id: int) -> list[Track]:
        """Fetch the tracklist for a specific release.

        Args:
            release_id: The Discogs release ID.

        Returns:
            List of Track objects for all tracks on the release.
        """
        release = self._client.release(release_id)
        tracks = []

        release_artist = self._extract_artists(release.artists)

        for track in release.tracklist:
            if not track.position or track.position.lower() in ("video", "dvd"):
                continue

            if hasattr(track, "artists") and track.artists:
                track_artist = self._extract_artists(track.artists)
            else:
                track_artist = release_artist

            tracks.append(
                Track(
                    position=track.position,
                    title=track.title,
                    artist=track_artist,
                    duration=track.duration or "",
                    release_id=release_id,
                    release_title=release.title,
                )
            )

        return tracks

    def _extract_artists(self, artists: list) -> str:
        """Extract a clean artist name from Discogs artist list.

        Args:
            artists: List of artist objects from Discogs.

        Returns:
            Cleaned artist name string.
        """
        if not artists:
            return "Unknown Artist"

        names = []
        for artist in artists:
            name = artist.name
            name = self._clean_artist_name(name)
            names.append(name)

        if len(names) == 1:
            return names[0]

        if len(names) == 2:
            return f"{names[0]} & {names[1]}"

        return ", ".join(names[:-1]) + f" & {names[-1]}"

    def _clean_artist_name(self, name: str) -> str:
        """Clean up Discogs artist name quirks.

        Args:
            name: Raw artist name from Discogs.

        Returns:
            Cleaned artist name.
        """
        if " (" in name and name.endswith(")"):
            idx = name.rfind(" (")
            suffix = name[idx + 2 : -1]
            if suffix.isdigit():
                name = name[:idx]

        return name.strip()

    def get_all_releases_with_tracks(
        self,
        include_wantlist: bool = True,
        folder_names: list[str] | None = None,
    ) -> dict[str, list[tuple[Release, list[Track]]]]:
        """Get all releases with their tracks, organized by folder.

        Args:
            include_wantlist: Whether to include wantlist releases.
            folder_names: Optional list of folder names to filter by.

        Returns:
            Dictionary mapping folder names to lists of (Release, tracks) tuples.
        """
        result: dict[str, list[tuple[Release, list[Track]]]] = {}

        folders = self.get_folders()
        if folder_names:
            folders = [f for f in folders if f.name in folder_names]

        for folder in folders:
            releases_with_tracks = []
            for release in self.get_folder_releases(folder.id):
                tracks = self.get_release_tracks(release.id)
                releases_with_tracks.append((release, tracks))
            result[folder.name] = releases_with_tracks

        if include_wantlist:
            wantlist_releases = []
            for release in self.get_wantlist_releases():
                tracks = self.get_release_tracks(release.id)
                wantlist_releases.append((release, tracks))
            if wantlist_releases:
                result[self.WANTLIST_FOLDER_NAME] = wantlist_releases

        return result
