# Song Automations

Sync your Discogs collection folders to Spotify and SoundCloud playlists.

## Features

- **Folder-based sync**: Each Discogs folder becomes a playlist (e.g., "Discogs - Electronic")
- **Wantlist support**: Your wantlist syncs as "Discogs - Wantlist"
- **Want playlist**: Add a track to a playlist named "Want" and its vinyl lands on your Discogs wantlist
- **Strict track matching**: Every match has to pass artist, title, version and length checks, so wrong songs stay out
- **Full sync**: Additions AND removals - keeps playlists in sync with your collection
- **Missing tracks report**: Generate CSV/JSON reports of tracks that couldn't be found
- **Runs by itself**: One command schedules a daily sync that never opens a browser and tells you when it needs you
- **Checks itself**: An audit compares every stored match with its evidence and with the other platform, without calling either

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

### Log in

```bash
# Both platforms
song-automations login

# One platform
song-automations login spotify
song-automations login soundcloud
```

Opens the approval page of each platform in your browser, waits for you to approve it and confirms with a green line. The saved login is replaced, so this is also the fix when a sync says a login has expired. A sync still opens the page by itself when no login is saved, unless it runs unattended (see "Run it every day").

The tool waits 10 minutes for the approval and then stops with a message instead of hanging. Set `LOGIN_WAIT_SECONDS` to change that. The saved logins live in `~/.song_automations/` and only your user can read them.

If Spotify refuses the saved login in the middle of a sync (for example because it was made for another Spotify app, or the approval was withdrawn), the file is renamed to `.spotify_cache.stale-` followed by the date and time, a yellow line says where it went, the approval page opens and the run carries on once you approve it.

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

### Run it every day

```bash
# Every day at 04:30, both platforms
song-automations schedule install

# Another time, or one platform
song-automations schedule install --at 06:15 --platform spotify

# Is it installed, when does it run, how did the last run end
song-automations schedule status

# Stop running by itself
song-automations schedule uninstall
```

Run `schedule install` from the folder that holds your `.env` file, after logging in once with `song-automations login`. On macOS it writes `~/Library/LaunchAgents/com.songautomations.sync.plist` and loads it, and from then on macOS runs `song-automations --unattended sync all` at that time every day, with no terminal open. If the Mac is asleep at that time, the run happens when it wakes up. If it is switched off, that day is skipped. Installing again replaces the earlier schedule, which is how you change the time. Do that too after moving the project folder or rebuilding its Python environment, because the schedule remembers both paths.

One run makes at most 400 Spotify searches, so a large collection fills in over several days. Each run carries on where the last one stopped.

**Unattended mode.** With `--unattended` in front of a command (or `UNATTENDED=true` in `.env`) the tool never opens a browser and never waits for one. If a login is missing, has expired or is refused, that platform stops at once with a message that names `song-automations login`, the other platform still runs, and the exit code is 2. On macOS a notification titled "Song Automations" tells you the same, so you find out without reading a log. A rate limit is reported the same way, with the time it lasts until. Using up the day's searches is the normal end of a run: exit code 0 and no notification. `song-automations login` always opens the browser, whatever the setting.

**The log.** Everything a scheduled run prints is added to `~/.song_automations/logs/scheduled-sync.log`, and each run starts with a line that carries its date and time. `schedule status` shows whether the schedule is installed and loaded, its time, the exit code of the last run and where the log is.

**Uninstalling.** `schedule uninstall` unloads the schedule and deletes its file. Your logins, the database and the log stay where they are.

If the project lives in Desktop, Documents or Downloads, macOS may ask once whether Python may access that folder. Allow it, otherwise the scheduled run cannot read its settings.

On Linux and other systems nothing is installed. `schedule install` prints the crontab line that does the same, for you to add with `crontab -e`.

### Want playlist

Add any track to a playlist called `Want` on Spotify or SoundCloud. On the next sync (or with the command below) the tool finds the earliest vinyl pressing whose tracklist really holds that track and adds it to your Discogs wantlist. Each track is handled once; tracks with no verified vinyl are retried on later runs. Set `WANT_PLAYLIST_NAME` to use another playlist name.

```bash
song-automations want sync --platform spotify --dry-run
song-automations want sync --platform all
```

During a sync the Want playlist is handled first, and a problem there never costs you the sync. If Discogs is down or answers with an error, the tool prints a yellow warning, skips the Want playlist for that run and syncs the folders as usual. Only an expired login, a rate limit or a used-up search budget stop the run, because the folder sync would hit the same wall. With `--platform all`, a platform that is stopped does not keep the other one from running.

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

### Check the matches

```bash
song-automations audit

# Counts for one platform
song-automations audit --platform soundcloud

# Choose where the list of disagreements goes
song-automations audit --output disagreements.csv
```

The audit reads the local database and nothing else. It calls no platform, uses no quota and changes nothing, so run it whenever you like.

For each platform it shows how many tracks are matched, missing and gated, and sorts the matched ones by the evidence behind them, strongest first:

- **Same recording code (ISRC)**: the track was accepted by its code, or the match on the other platform carries the same code.
- **Exact version**: artist, title and version passed the checks.
- **Original mix instead of the listed version** and **another mix of the same song**: substitutions, the right song in another version.
- **Upload by an account that is not the artist's**: a SoundCloud upload whose account is neither the artist nor recognised as the label. The database does not keep the label or the remixers of a track, so this group holds label and remixer accounts as well as vinyl rips by strangers.

Then it takes every track that is matched on both platforms and checks whether the two matches are one recording: the recording codes have to be the same when both platforms list one, and the lengths have to be within 5 seconds when both are stored. The result is one line, for example "412 of 530 matches found on both platforms are cross-verified", followed by how many disagree and how many could not be compared.

Every disagreement goes to `~/.song_automations/reports/audit-disagreements.csv` with the artist, the title, both matched titles, both lengths, both codes and the check that failed. Different lengths are the ones worth a listen. Different codes with the same length are usually the same recording released twice.

Next to it, `audit-shared-ids.csv` lists every platform track that more than one Discogs track on that platform is matched to. The same song on two of your records shares a track for a good reason, and the column `same_discogs_title` says `yes` for those. A `no` means two different titles ended up on one track.

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

- **Artist**: one of the credited artists, under any name variation the release uses, must match the candidate artist. The other name variations and aliases Discogs lists for the artist are tried only when nothing matched under the credited names.
- **Title**: the base title must match, ignoring punctuation, accents and catalog prefixes such as `A1`.
- **Unwanted uploads**: covers, karaoke, tributes, mashups, bootlegs, acoustic and instrumental versions, previews, reaction videos, live recordings and radio or TV sessions (KEXP, Tiny Desk, Maida Vale and the like) are rejected unless the Discogs title carries the same word. The word has to be there as a whole word, so a track called "Alive" does not excuse a live take, and words in the artist's own name never count against an upload. DJ sets, mixtapes, radio shows and uploads merely marked "live" are rejected too, except when their length is within 5 seconds of the Discogs length, because then the upload is the record itself on a channel with such a word in its title.
- **Version**: the exact mix is preferred, then the original mix, then another mix of the same song. A mix with the wrong length drops to the lowest tier.
- **Length**: an exact-version match must be within 20 seconds of the Discogs duration when both are known.
- **No duplicates**: each streaming track is used once per playlist, and two tracks of one release never share one match. The same song on two of your records does share its streaming track.

On Spotify the tool looks for the release as an album first, then runs one search per remaining track. On SoundCloud it also looks at sets and at the label's or artist's own account.

### SoundCloud: the same recording, proven by its code

Every released recording has an ISRC, a code that names that one recording. A cover, a live take or a tribute band's version has a different code or none. For each Discogs track the tool looks the code up in the public Deezer catalogue (free, no account, and the entry has to pass the checks above as the exact version). A SoundCloud track carrying the same code is accepted whatever its account is called, so the official upload on "YusufCatStevens" or "SadeOfficial" counts for Cat Stevens and Sade. The recording code Spotify lists for an exact match of the same track is used to verify the SoundCloud track in the same way, which covers tracks Deezer lacks. An upload that carries the code but is more than 20 seconds shorter or longer than the recording is refused, because labels post clips under the code of the full track.

Without a matching code the older rules apply. Uploads by the artist, the label or a remixer credited on Discogs are trusted (the account name has to match exactly, so tribute accounts such as "FOOT Fighters" do not pass, and an account that only carries one of the artist's aliases is not trusted either, since short aliases are common account names). Anyone else's upload, such as a vinyl rip, is only accepted for tracks that neither Deezer nor Spotify carries and only when its length is within 20 seconds of the Discogs length. In a blind check, fan uploads of released music turned out to be covers, tributes or other cuts about as often as the real record, while fan uploads of rare records were mostly genuine rips.

If Deezer cannot be reached the sync still runs, it just recognises fewer official uploads that day.

### SoundCloud Go+

Labels deliver most released music to SoundCloud as tracks that only Go+ subscribers can play in full; everyone else hears a 30 second clip, and some are blocked outright. On a sample of this collection, three out of four tracks that Spotify carries exist on SoundCloud only in that form.

SoundCloud tells the tool, per track, what the logged-in account may do with it. By default only tracks the account can play in full are added, so a Go+ account gets the label releases and a free account does not. The sync reports how many missing tracks exist only as such gated uploads, and `report matches` lists them with the status `gated`. Set `SOUNDCLOUD_GATED_TRACKS=true` to add them anyway; an upload the account can play in full is still preferred over a gated one of the same version. Switching it off again leaves the tracks that were added in place.

A gated track shows a 30 second clip instead of its length. When Discogs lists a length that therefore cannot be checked, the match is kept but marked as another version in the report, unless its ISRC proves it.

### Spotify limits

Spotify apps in development mode are blocked for about a day after roughly 900 requests. One run makes at most 400 searches (`SPOTIFY_SEARCH_BUDGET`). When the budget is used up the run stops cleanly, finished folders are synced and every search is saved, so the next run carries on from there. If Spotify blocks the app anyway, the sync says until when and leaves unfinished folders alone.

Tracks that are not found are retried after a week. A match you approved or corrected in the review tool is kept for good and never searched again. Saved search responses older than 30 days are deleted at the start of each sync. Run `song-automations report matches` to get a CSV of every match with its tier, so you can check substitutions and missing tracks. `song-automations review` opens a local page that lists the same matches a sync flags, which is every match that is not the exact version and that you have not approved or refused yet, weakest first.

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
