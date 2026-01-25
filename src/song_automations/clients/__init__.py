"""API clients for music services."""

from song_automations.clients.discogs import DiscogsClient
from song_automations.clients.soundcloud import SoundCloudClient
from song_automations.clients.spotify import SpotifyClient

__all__ = ["DiscogsClient", "SpotifyClient", "SoundCloudClient"]
