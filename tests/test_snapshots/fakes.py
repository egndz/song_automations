"""Platform stand-in shared by the snapshot tests."""

from types import SimpleNamespace

SPOTIFY_URI = "spotify:track:"


class FakePlatform:
    """Spotify or SoundCloud stand-in holding playlists in memory.

    Every call is recorded, so a test can count the reads and writes a piece of
    code makes. A Spotify playlist has a name and takes track addresses, a
    SoundCloud one has a title and takes the whole list of numeric ids, the way
    the real clients differ.

    Args:
        platform: spotify or soundcloud.
        playlists: Mapping of playlist name to track ids.
        public: Whether the playlists are public, or None when they do not say.
        titled: Whether tracks carry a title and an artist.
        unavailable: Track ids SoundCloud silently refuses to put in a playlist.
    """

    def __init__(
        self,
        platform: str,
        playlists: dict[str, list[str]],
        public: bool | None = True,
        titled: bool = True,
        unavailable: tuple[str, ...] = (),
    ) -> None:
        self.platform = platform
        self.playlists = {name: list(ids) for name, ids in playlists.items()}
        self.public = public
        self.titled = titled
        self.unavailable = set(unavailable)
        self.created: list[tuple[str, bool]] = []
        self.calls: list[str] = []

    @property
    def writes(self) -> list[str]:
        """Lists the calls that changed something.

        Returns:
            Names of the changing calls, in order.
        """
        reads = ("find_playlist_by_name", "get_playlist_tracks")
        return [call for call in self.calls if call not in reads]

    def playlist(self, name: str) -> SimpleNamespace:
        """Builds the playlist object the platform would return.

        Args:
            name: Playlist name.

        Returns:
            The playlist, shaped like the one of the platform.
        """
        if self.platform == "spotify":
            return SimpleNamespace(id=f"id of {name}", name=name, public=self.public)
        return SimpleNamespace(id=f"id of {name}", title=name, is_public=self.public)

    def track(self, track_id: str) -> SimpleNamespace:
        """Builds the track object the platform would return.

        Args:
            track_id: Track id.

        Returns:
            The track, shaped like the one of the platform.
        """
        value = track_id if self.platform == "spotify" else int(track_id)
        if not self.titled:
            return SimpleNamespace(id=value)
        if self.platform == "spotify":
            return SimpleNamespace(id=value, name=f"Title {track_id}", artist=f"Artist {track_id}")
        return SimpleNamespace(id=value, title=f"Title {track_id}", artist=f"Artist {track_id}")

    @staticmethod
    def _name(playlist_id: str) -> str:
        """Reads the playlist name out of an id.

        Args:
            playlist_id: Playlist id.

        Returns:
            The name.
        """
        return playlist_id.removeprefix("id of ")

    def find_playlist_by_name(self, name: str) -> SimpleNamespace | None:
        """Finds a playlist.

        Args:
            name: Playlist name.

        Returns:
            The playlist, or None.
        """
        self.calls.append("find_playlist_by_name")
        return self.playlist(name) if name in self.playlists else None

    def create_playlist(self, name: str, description: str = "", public: bool = True) -> SimpleNamespace:
        """Creates an empty playlist.

        Args:
            name: Playlist name.
            description: Playlist description.
            public: Whether the playlist is public.

        Returns:
            The playlist.
        """
        self.calls.append("create_playlist")
        self.created.append((name, public))
        self.playlists[name] = []
        return self.playlist(name)

    def delete_playlist(self, playlist_id: str) -> None:
        """Deletes a playlist.

        Args:
            playlist_id: Playlist id.
        """
        self.calls.append("delete_playlist")
        del self.playlists[self._name(playlist_id)]

    def get_playlist_tracks(self, playlist_id: str) -> list[SimpleNamespace]:
        """Lists the tracks of a playlist.

        Args:
            playlist_id: Playlist id.

        Returns:
            Tracks in playlist order.
        """
        self.calls.append("get_playlist_tracks")
        return [self.track(track_id) for track_id in self.playlists[self._name(playlist_id)]]

    def add_tracks_to_playlist(self, playlist_id: str, uris: list[str]) -> None:
        """Adds tracks at the end of a Spotify playlist.

        Args:
            playlist_id: Playlist id.
            uris: Track addresses.
        """
        assert self.platform == "spotify"
        assert all(uri.startswith(SPOTIFY_URI) for uri in uris)
        self.calls.append("add_tracks_to_playlist")
        self.playlists[self._name(playlist_id)] += [uri.removeprefix(SPOTIFY_URI) for uri in uris]

    def remove_tracks_from_playlist(self, playlist_id: str, uris: list[str]) -> None:
        """Removes every copy of some tracks from a Spotify playlist.

        Args:
            playlist_id: Playlist id.
            uris: Track addresses.
        """
        assert self.platform == "spotify"
        assert all(uri.startswith(SPOTIFY_URI) for uri in uris)
        self.calls.append("remove_tracks_from_playlist")
        gone = {uri.removeprefix(SPOTIFY_URI) for uri in uris}
        name = self._name(playlist_id)
        self.playlists[name] = [track_id for track_id in self.playlists[name] if track_id not in gone]

    def set_playlist_tracks(self, playlist_id: str, track_ids: list[int]) -> None:
        """Replaces the tracks of a SoundCloud playlist.

        Args:
            playlist_id: Playlist id.
            track_ids: Numeric track ids.
        """
        assert self.platform == "soundcloud"
        assert all(isinstance(track_id, int) for track_id in track_ids)
        self.calls.append("set_playlist_tracks")
        self.playlists[self._name(playlist_id)] = [
            str(track_id) for track_id in track_ids if str(track_id) not in self.unavailable
        ]
