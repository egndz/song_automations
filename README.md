# Song Automations

Sync your Discogs collection folders to Spotify and SoundCloud playlists.

## Features

- **Folder-based sync**: Each Discogs folder becomes a playlist (e.g., "Discogs - Electronic")
- **Wantlist support**: Your wantlist syncs as "Discogs - Wantlist"
- **Smart track matching**: Fuzzy matching optimized for electronic music (preserves remix info)
- **Full sync**: Additions AND removals - keeps playlists in sync with your collection
- **Missing tracks report**: Generate CSV/JSON reports of tracks that couldn't be found

## Installation

```bash
pip install -e .
```

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Get your API credentials:
- **Discogs**: https://www.discogs.com/settings/developers
- **Spotify**: https://developer.spotify.com/dashboard
- **SoundCloud**: https://soundcloud.com/you/apps

## Usage

### Sync to Spotify

```bash
# Sync all folders
song-automations sync spotify

# Sync specific folders
song-automations sync spotify --folders "Electronic,disco"

# Exclude wantlist
song-automations sync spotify --exclude-wantlist

# Dry run (preview changes)
song-automations sync spotify --dry-run
```

### Sync to SoundCloud

```bash
song-automations sync soundcloud
```

### Sync to both platforms

```bash
song-automations sync all
```

### View status

```bash
song-automations status
```

### Generate missing tracks report

```bash
# CSV format
song-automations report missing --format csv

# JSON format
song-automations report missing --format json

# Filter by platform
song-automations report missing --destination spotify
```

## How it works

1. Fetches all folders from your Discogs collection
2. For each folder, creates/finds a playlist named "Discogs - {folder_name}"
3. Fetches the tracklist for each release in the folder
4. Searches for each track on the target platform using fuzzy matching
5. Adds matched tracks to the playlist
6. Removes tracks that are no longer in the Discogs folder
7. Deletes playlists for folders that no longer exist

## Matching system

The matching system is optimized for electronic music:
- **Preserves remix info**: "(Hardfloor Remix)" is treated as essential, not noise
- **Multi-factor scoring**: Artist match (40%) + Title match (30%) + Verified (20%) + Popularity (10%)
- **Low thresholds**: Starting at 30% minimum confidence to be inclusive

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run linting
ruff check .
```

## License

MIT
