# Song Automations

Sync your Discogs collection folders to Spotify and SoundCloud playlists.

## Features

- **Folder-based sync**: Each Discogs folder becomes a playlist (e.g., "Discogs - Electronic")
- **Wantlist support**: Your wantlist syncs as "Discogs - Wantlist"
- **Strict track matching**: Every match has to pass artist, title, version and length checks, so wrong songs stay out
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

### Review every match

```bash
song-automations report matches --destination spotify
```

Writes a CSV with the Discogs track, the chosen streaming track, its tier (exact, original or other mix) and the tracks that were not found.

## How it works

1. Fetches all folders from your Discogs collection
2. For each folder, creates/finds a playlist named "Discogs - {folder_name}"
3. Fetches the tracklist for each release in the folder
4. Searches each release on the target platform and matches its tracks with hard checks
5. Adds matched tracks to the playlist
6. Removes tracks that are no longer in the Discogs folder
7. Deletes playlists for folders that no longer exist

## Matching system

Matching works release by release and only accepts a candidate that passes every check:

- **Artist**: one of the credited artists (or a Discogs name variation or alias) must match the candidate artist.
- **Title**: the base title must match, ignoring punctuation, accents and catalog prefixes such as `A1`.
- **Unwanted uploads**: covers, karaoke, DJ sets, live recordings, previews and reaction videos are rejected unless the Discogs title says the same.
- **Version**: the exact mix is preferred, then the original mix, then another mix of the same song. A mix with the wrong length drops to the lowest tier.
- **Length**: an exact-version match must be within 20 seconds of the Discogs duration when both are known.
- **No duplicates**: each streaming track is used once per playlist, and two Discogs tracks never share one match.

On Spotify the tool looks for the release as an album first, then runs one search per remaining track. On SoundCloud it also looks at sets and at the label's or artist's own account.

SoundCloud uploads by the artist, the label or a remixer credited on Discogs are trusted (the account name has to match exactly, so tribute accounts such as "FOOT Fighters" do not pass). Anyone else's upload, such as a vinyl rip, is only accepted for tracks Spotify does not carry and only when its length is within 20 seconds of the Discogs length. In a blind check, fan uploads of music that is on Spotify turned out to be covers, tributes or other cuts about as often as the real record, while fan uploads of rare records were mostly genuine rips. Run Spotify before SoundCloud so this works (`sync all` does).

### Spotify limits

Spotify apps in development mode are blocked for about a day after roughly 900 requests. One run makes at most 400 searches (`SPOTIFY_SEARCH_BUDGET`). When the budget is used up the run stops cleanly, finished folders are synced and every search is saved, so the next run carries on from there. If Spotify blocks the app anyway, the sync says until when and leaves unfinished folders alone.

Tracks that are not found are retried after a week. Run `song-automations report matches` to get a CSV of every match with its tier, so you can check substitutions and missing tracks.

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
