# Seller Radio

Turn a Discogs seller inventory into a crate you can actually play. Seller Radio pulls a shop's
listings, discovers extra records through saved search banks, enriches everything with the
YouTube videos attached to each Discogs release, and exports a single file that a local web
player reads. The result is a queue of records that are for sale right now, with audio.

## Quick start

```bash
cd seller-radio
export DISCOGS_TOKEN=your_personal_access_token

python -m radio owned      # your collection and wantlist, used as filters
python -m radio sync       # the seller inventory, about 108 requests
python -m radio enrich     # metadata and videos, one request per release
python -m radio export     # writes web/crate.js
python -m radio serve      # http://127.0.0.1:8733
```

Run `python -m radio stats` at any point to see whether the crate is worth playing yet.

Always reach the player through `serve`, not by opening `web/index.html` from disk. A page loaded
over `file://` has a null origin, and YouTube refuses to embed a large share of videos from a null
origin, so playback fails for reasons that look like a broken crate. The wantlist button is also
disabled over `file://`, because adding to your wantlist needs an authenticated request that only
the local server can make.

A personal access token comes from https://www.discogs.com/settings/developers. The token is
read from `DISCOGS_TOKEN` or `DISCOGS_USER_TOKEN` in the environment, and as a fallback from a
`.env` file in the parent repository. The account name defaults to `egunduz` and can be
overridden with `DISCOGS_USERNAME`.

## Commands

| Command | What it does |
| --- | --- |
| `owned` | Replaces the local copy of your collection and wantlist. Cheap, run it first. |
| `sync [--seller NAME]` | Pulls active listings for every seller in `SELLERS`, marks vanished listings inactive, and adds their releases to the pool. |
| `crawl [--bank-index N]` | Runs the saved search banks and adds matching release ids to the pool. |
| `enrich [--limit N]` | Fetches full metadata and videos for pooled releases that have never been fetched. Default limit 500. |
| `export [--out PATH]` | Writes `web/crate.js`, the only file the player needs. |
| `serve [--host H] [--port N]` | Runs the local player on port 8733. |
| `stats` | The scoreboard. Leads with the video hit rate. |
| `hitrate [--sample N]` | Samples enriched releases and breaks the hit rate down by decade and by style. |

Every command accepts a global `--db PATH` if you want to keep more than one crate. The database
is created and migrated on first use, so a fresh clone needs no setup step.

## Rate limits and how long things take

Discogs allows 60 authenticated requests per minute, which works out to one request per second.
The client reads the remaining budget off every response and pauses only when it runs out, so
the numbers below are close to the real wall clock time.

* Syncing a 10,716 listing shop costs about 108 requests at 100 listings per page, so roughly
  two minutes.
* Enrichment costs one request per release, plus one extra per master when a release carries no
  video of its own. A full pass over that same shop is about 10,700 requests, which is close to
  three hours.
* `owned` costs one request per 100 items in your collection and your wantlist.

Enrichment is incremental and resumable. Each release is committed as soon as it is fetched,
and only releases with no fetch timestamp are ever picked up again, so Ctrl-C is safe and
rerunning continues where it stopped. The progress line shows elapsed time, an estimate of the
time remaining, and the current rate.

Run it in batches if you would rather not sit through the whole thing:

```bash
python -m radio enrich --limit 1000
```

Seller sourced releases are always enriched before search sourced ones, so the records you can
actually buy get audio first.

## Adding a seller or a search bank

Both live in `radio/config.py`.

```python
SELLERS: list[str] = ["allcitydublin", "another-shop"]

SEARCH_BANKS: list[dict] = [
    {
        "type": "release",
        "format": "Vinyl",
        "genre": "Electronic",
        "style": "Deep House",
        "year": "1993-1999",
    },
]
```

A bank is just a set of Discogs search parameters. `genre`, `style`, `country`, `label`, and
`year` are all supported, and `year` accepts a single year such as `1995` or a range such as
`1990-1999`. Adding a seller then running `python -m radio sync` is enough. Use
`python -m radio sync --seller another-shop` to sync only the new one, and
`python -m radio crawl --bank-index 0` to test a single bank before committing to all of them.

## The 10,000 item search cap

Discogs stops deep pagination at 10,000 items per search query. Page 101 at 100 results per page
returns a 404, which the crawler treats as a normal end of crawl rather than an error. The page
count reported by the API is not reliable and often claims more pages than you can actually
reach.

That means a broad bank such as every Electronic vinyl release will silently give you the first
10,000 results and nothing more. Slice broad banks into narrower ones so each stays under the
cap. Year is the easiest axis, country is the next best.

```python
{"format": "Vinyl", "genre": "Electronic", "style": "Techno", "year": "1992-1994"},
{"format": "Vinyl", "genre": "Electronic", "style": "Techno", "year": "1995-1997"},
```

`python -m radio crawl` prints a `hit cap` column per bank. Any bank marked `yes` is truncated
and should be split.

## Reading the stats

`python -m radio stats` puts the video hit rate first because that single number decides whether
this is a radio or a shop list.

* At or above 40 percent it behaves like a radio. Press play and leave it running.
* Between 30 and 40 percent it plays with visible gaps. Keep enriching.
* Below 30 percent it is not a radio. Use it as a browsable shop list with preview buttons on
  the records that do have audio.

`python -m radio hitrate` explains where the gaps are. Older and more obscure pressings tend to
have fewer videos, so if one decade or one style is dragging the average down, narrowing the
search banks toward the parts of the catalogue that do have audio is the fastest fix.

## Troubleshooting

**`Configuration error: No Discogs token found`**
Set `DISCOGS_TOKEN` in your shell, or add `DISCOGS_USER_TOKEN=...` to the `.env` file in the
parent repository. Every command that needs credentials exits with code 2 and this message
rather than a traceback.

**HTTP 403 on every request**
Discogs requires a User-Agent header. The project sends one, so a 403 across the board usually
means the token is wrong or revoked rather than a header problem.

**Sync reports zero listings for a seller**
The seller name must be the Discogs username, not the shop display name. It is the last segment
of the seller URL. A private or empty inventory also reports zero, and the seller name shows up
under the error column when the inventory could not be read at all.

**Enrichment feels stuck**
It is not. One request per second is the ceiling, so a thousand releases take about seventeen
minutes. The progress line updates after every release. If the rate drops below sixty releases
per minute, the client is waiting out a rate limit window it hit earlier.

**A crawl stops early**
Check the `hit cap` column. A 404 at the cap ends that bank cleanly and the run continues with
the next one.

**`Crate file ... not written yet`**
Run `python -m radio export`. The player reads `web/crate.js` and nothing else, so the file has
to be regenerated after every enrichment batch to pick up new audio.

**Nothing plays in the browser**
Only releases with at least one live video are exported, so an empty player usually means
enrichment has not run far enough. Check `exportable tracks` in `stats`. Videos that YouTube has
taken down are marked dead in the database and dropped from the next export.

## Development

```bash
python -m pytest
python -m ruff check .
```

The test suite never touches the network. Every client call is faked, and the database fixtures
are in-memory or in a temporary directory.
