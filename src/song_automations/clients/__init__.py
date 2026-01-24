"""API clients for music services."""

from song_automations.clients.discogs import DiscogsClient
from song_automations.clients.spotify import SpotifyClient
from song_automations.clients.soundcloud import SoundCloudClient

__all__ = ["DiscogsClient", "SpotifyClient", "SoundCloudClient"]
