"""FastAPI application for reviewing track matches."""

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from song_automations.clients.deezer import DeezerClient
from song_automations.clients.discogs import DiscogsClient, Track
from song_automations.clients.soundcloud import SoundCloudClient
from song_automations.clients.spotify import SpotifyClient
from song_automations.config import Settings, get_settings
from song_automations.errors import (
    LoginFailedError,
    LoginTimeoutError,
    RateLimitedError,
    ReauthRequiredError,
    SearchBudgetExhaustedError,
)
from song_automations.matching.isrc import IsrcFinder, with_recording_evidence
from song_automations.matching.matcher import Match, SourceTrack, decide
from song_automations.matching.resolver import (
    SoundCloudSearcher,
    SpotifySearcher,
    search_title,
)
from song_automations.state.tracker import MatchedTrack, StateTracker

ITEMS_PER_PAGE = 10
LOGS_PER_PAGE = 50
MAX_SEARCHES = 2
MAX_ALTERNATIVES = 10


def extract_track_id(url: str, platform: str) -> str | None:
    """Extract track ID from a Spotify or SoundCloud URL.

    Args:
        url: Full URL or just the ID.
        platform: Either 'spotify' or 'soundcloud'.

    Returns:
        Extracted track ID or None if invalid.
    """
    url = url.strip()

    if platform == "spotify":
        match = re.search(r"track[/:]([a-zA-Z0-9]+)", url)
        if match:
            return match.group(1)
        if re.match(r"^[a-zA-Z0-9]{22}$", url):
            return url

    elif platform == "soundcloud":
        match = re.search(r"tracks[/:](\d+)", url)
        if match:
            return match.group(1)
        if re.match(r"^\d+$", url):
            return url

    return None


def duration_ms(text: str) -> int | None:
    """Reads a Discogs duration.

    Args:
        text: Duration such as 6:32.

    Returns:
        Milliseconds, or None when absent or malformed.
    """
    found = re.match(r"^(\d+):(\d{2})$", (text or "").strip())
    return (int(found[1]) * 60 + int(found[2])) * 1000 if found else None


def build_source(row: MatchedTrack, release_tracks: Sequence[Track]) -> tuple[SourceTrack, str]:
    """Prepares a stored track for matching, the way a sync prepares it.

    The Discogs tracklist supplies the credited names, the length, the labels
    and the remixers. Without it only the stored artist and title are known.

    Args:
        row: Stored match under review.
        release_tracks: Discogs tracks of the release, empty when unavailable.

    Returns:
        The source track and the artist name worth typing into a search box.
    """
    track = next((t for t in release_tracks if t.position == row.discogs_track_position), None)
    if track is None:
        return SourceTrack(artists=(row.artist,), title=row.track_name), row.artist
    source = SourceTrack(
        artists=track.artist_names or (track.artist,),
        title=track.title,
        duration_ms=duration_ms(track.duration),
        release_title=track.release_title,
        labels=track.labels,
        remixers=track.remixers,
    )
    return source, track.artist_names[0] if track.artist_names else track.artist


def alternative_queries(searcher, artist: str, title: str) -> list[str]:
    """Lists the searches one request may make, most precise first.

    Args:
        searcher: Platform searcher.
        artist: Artist to search for.
        title: Track title.

    Returns:
        At most MAX_SEARCHES queries, without repeats.
    """
    queries = [*searcher.track_queries(artist, title), f"{artist} {search_title(title)}"]
    return list(dict.fromkeys(queries))[:MAX_SEARCHES]


def find_alternatives(
    searcher, source: SourceTrack, queries: Sequence[str], refused: frozenset[str]
) -> list[Match]:
    """Searches a platform and keeps the candidates that pass the matching gates.

    Args:
        searcher: Platform searcher.
        source: Source track.
        queries: Searches to run.
        refused: Candidate ids a person already refused for this track.

    Returns:
        Accepted candidates, best first.
    """
    candidates = [
        candidate
        for query in queries
        for candidate in searcher.search(query)
        if candidate.id not in refused
    ]
    decision = decide(source, candidates)
    return ([decision.match] if decision.match else []) + decision.alternatives


def describe_alternative(match: Match, destination: str, current_id: str | None) -> dict:
    """Turns an accepted candidate into the JSON the review page shows.

    Args:
        match: Accepted candidate.
        destination: Either 'spotify' or 'soundcloud'.
        current_id: Platform track id the stored match points at.

    Returns:
        One entry of the alternatives list. The score is the confidence, kept
        under that name for the page.
    """
    track_id = match.candidate.id
    if destination == "spotify":
        embed_url = f"https://open.spotify.com/embed/track/{track_id}?utm_source=generator&theme=0"
        external_url = f"https://open.spotify.com/track/{track_id}"
    else:
        embed_url = (
            "https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/tracks/"
            f"{track_id}&color=%23ff5500&auto_play=false&hide_related=true&show_comments=false"
        )
        external_url = ""
    return {
        "track_id": track_id,
        "title": match.candidate.raw_title,
        "artist": ", ".join(match.reading.artists),
        "score": match.confidence,
        "confidence": match.confidence,
        "tier": match.tier.name.lower(),
        "embed_url": embed_url,
        "external_url": external_url,
        "is_current": track_id == current_id,
    }


def platform_error(
    status_code: int,
    error: str,
    platform: str,
    detail: str,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    """Builds the JSON answer for a platform that cannot be searched right now.

    Args:
        status_code: HTTP status.
        error: Short machine readable reason.
        platform: Platform name.
        detail: Message for the person reviewing.
        retry_after_seconds: Seconds until the platform accepts requests again, if known.

    Returns:
        The response.
    """
    content: dict[str, str | int] = {"error": error, "platform": platform, "detail": detail}
    headers = None
    if retry_after_seconds is not None:
        content["retry_after_seconds"] = retry_after_seconds
        headers = {"Retry-After": str(retry_after_seconds)}
    return JSONResponse(status_code=status_code, content=content, headers=headers)


TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(
    settings: Settings | None = None,
    client_factories: Mapping[str, Callable[[Settings], object]] | None = None,
    discogs_client: DiscogsClient | None = None,
    isrc_finder: IsrcFinder | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Each platform client is built on first use and kept for the life of the
    app, so the Spotify search budget counts every request, not just one.

    Args:
        settings: Application settings, read from the environment when omitted.
        client_factories: Builds the client of a platform, by platform name.
        discogs_client: Discogs client, built from the settings when omitted.
        isrc_finder: Looks up recording codes for SoundCloud, built when omitted.

    Returns:
        Configured FastAPI app instance.
    """
    app = FastAPI(
        title="Song Automations Review",
        description="Review flagged track matches",
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    settings = settings or get_settings()
    tracker = StateTracker(settings.db_path)
    discogs = discogs_client or (DiscogsClient(settings) if settings.discogs_user_token else None)
    factories = dict(client_factories or {"spotify": SpotifyClient, "soundcloud": SoundCloudClient})
    finder = isrc_finder or IsrcFinder(DeezerClient(), tracker)

    release_cache: dict[int, dict] = {}
    release_tracks: dict[int, list[Track]] = {}
    searchers: dict[str, SpotifySearcher | SoundCloudSearcher] = {}

    def get_release_tracks(release_id: int) -> list[Track]:
        """Fetches and caches the Discogs tracks of a release.

        Args:
            release_id: Discogs release ID.

        Returns:
            Tracks of the release, empty without a Discogs client.
        """
        if discogs is None:
            return []
        if release_id not in release_tracks:
            release_tracks[release_id] = discogs.get_release_tracks(release_id)
        return release_tracks[release_id]

    def get_searcher(destination: str) -> SpotifySearcher | SoundCloudSearcher:
        """Builds the searcher of a platform on first use and keeps it.

        Args:
            destination: Either 'spotify' or 'soundcloud'.

        Returns:
            The searcher, backed by the one client of that platform.
        """
        platform = "spotify" if destination == "spotify" else "soundcloud"
        if platform not in searchers:
            client = factories[platform](settings)
            if platform == "spotify":
                searchers[platform] = SpotifySearcher(client, tracker)
            else:
                searchers[platform] = SoundCloudSearcher(
                    client, tracker, allow_gated=settings.soundcloud_gated_tracks
                )
        return searchers[platform]

    @app.exception_handler(RateLimitedError)
    async def rate_limited(request: Request, error: RateLimitedError) -> JSONResponse:
        """Reports that a platform refuses requests for now.

        Args:
            request: Request that hit the limit.
            error: The limit.

        Returns:
            HTTP 429 naming the platform and the wait.
        """
        until = datetime.now() + timedelta(seconds=error.retry_after_seconds)
        return platform_error(
            429,
            "rate_limited",
            error.platform,
            f"{error.platform} has rate limited this app until {until:%Y-%m-%d %H:%M}. "
            "Nothing was changed. Try again after that time.",
            retry_after_seconds=error.retry_after_seconds,
        )

    @app.exception_handler(ReauthRequiredError)
    async def reauth_required(request: Request, error: ReauthRequiredError) -> JSONResponse:
        """Reports that a platform login has expired or could not be renewed.

        Args:
            request: Request that needed the login.
            error: The expired or failed login.

        Returns:
            HTTP 401 saying how to log in again.
        """
        detail = (
            f"Your {error.platform} login has expired. Run "
            f"'song-automations login {error.platform.lower()}' in a terminal, approve the "
            f"{error.platform} page that opens, then try again."
        )
        if isinstance(error, LoginTimeoutError | LoginFailedError):
            detail = str(error)
        return platform_error(401, "reauth_required", error.platform, detail)

    @app.exception_handler(SearchBudgetExhaustedError)
    async def budget_exhausted(request: Request, error: SearchBudgetExhaustedError) -> JSONResponse:
        """Reports that the review app made all the searches it may make.

        Args:
            request: Request that asked for one search too many.
            error: The used budget.

        Returns:
            HTTP 429 naming the budget.
        """
        return platform_error(
            429,
            "search_budget_exhausted",
            error.platform,
            f"The review app used its {error.budget} {error.platform} searches, the limit that "
            f"keeps it under the daily quota of {error.platform}. Searches already made stay "
            "saved. Restart the review app to search again.",
        )

    def get_release_info(release_id: int) -> dict | None:
        """Fetch and cache Discogs release info."""
        if release_id in release_cache:
            return release_cache[release_id]

        if not discogs:
            return None

        try:
            release = discogs._client.release(release_id)
            info = {
                "id": release_id,
                "title": release.title,
                "artist": ", ".join(a.name for a in release.artists),
                "year": release.year,
                "thumb": release.thumb,
                "images": [img["uri"] for img in (release.images or [])[:1]],
                "labels": [{"name": lbl.name, "catno": lbl.data.get("catno", "")} for lbl in release.labels],
                "genres": release.genres or [],
                "styles": release.styles or [],
                "country": release.country,
                "format": ", ".join(f["name"] for f in (release.formats or [])),
                "url": f"https://www.discogs.com/release/{release_id}",
                "tracklist": [
                    {"position": t.position, "title": t.title, "duration": t.duration}
                    for t in release.tracklist
                ],
            }
            release_cache[release_id] = info
            return info
        except Exception:
            return None

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        destination: str | None = None,
        page: int = Query(1, ge=1),
    ):
        """Display flagged tracks for review with pagination."""
        all_flagged = tracker.get_flagged_tracks(
            high_confidence=settings.high_confidence,
            destination=destination,
        )

        total_items = len(all_flagged)
        total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        page = min(page, total_pages)

        start_idx = (page - 1) * ITEMS_PER_PAGE
        end_idx = start_idx + ITEMS_PER_PAGE
        paginated = all_flagged[start_idx:end_idx]

        tracks_with_info = []
        for track in paginated:
            release_info = get_release_info(track.discogs_release_id)
            tracks_with_info.append({
                "track": track,
                "release": release_info,
            })

        stats = {
            "total": total_items,
            "spotify": len([t for t in all_flagged if t.destination == "spotify"]),
            "soundcloud": len([t for t in all_flagged if t.destination == "soundcloud"]),
        }

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "tracks_with_info": tracks_with_info,
                "stats": stats,
                "destination_filter": destination,
                "page": page,
                "total_pages": total_pages,
                "total_items": total_items,
            },
        )

    @app.post("/approve/{track_id}")
    async def approve_track(track_id: int):
        """Approve a track match."""
        track = tracker.get_matched_track_by_id(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")

        tracker.update_review_status(track_id, "approved")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/reject/{track_id}")
    async def reject_track(track_id: int):
        """Reject a track match - will be removed from playlist on next sync."""
        track = tracker.get_matched_track_by_id(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")

        tracker.update_review_status(track_id, "rejected")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/correct/{track_id}")
    async def correct_track(
        track_id: int,
        correct_url: str = Form(...),
    ):
        """Update a track with the correct URL provided by user."""
        track = tracker.get_matched_track_by_id(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")

        new_track_id = extract_track_id(correct_url, track.destination)
        if not new_track_id:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {track.destination} URL or track ID",
            )

        tracker.update_matched_track(
            track_id=track_id,
            destination_track_id=new_track_id,
            match_confidence=1.0,
        )
        tracker.update_review_status(track_id, "approved")

        return RedirectResponse(url="/", status_code=303)

    @app.post("/approve-all")
    async def approve_all(destination: str | None = None, track_ids: list[int] = Form(default=[])):
        """Approves the matches the page was showing when the button was pressed.

        Only the posted ids are approved, and only while they are still
        flagged, so matches on other pages or added since are never approved
        unseen.

        Args:
            destination: Platform filter the page was using.
            track_ids: Ids of the matches shown on the page.

        Returns:
            Redirect to the review page.
        """
        shown = set(track_ids)
        flagged = tracker.get_flagged_tracks(
            high_confidence=settings.high_confidence,
            destination=destination,
        )
        for track in flagged:
            if track.id in shown:
                tracker.update_review_status(track.id, "approved")
        return RedirectResponse(url="/", status_code=303)

    @app.get("/track/{track_id}")
    async def get_track_details(track_id: int):
        """Get track details as JSON."""
        track = tracker.get_matched_track_by_id(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")

        links = {}
        if track.destination == "spotify" and track.destination_track_id:
            links["platform"] = f"https://open.spotify.com/track/{track.destination_track_id}"
        elif track.destination == "soundcloud" and track.destination_track_id:
            links["platform"] = f"https://soundcloud.com/tracks/{track.destination_track_id}"

        return {
            "id": track.id,
            "artist": track.artist,
            "track_name": track.track_name,
            "destination": track.destination,
            "confidence": track.match_confidence,
            "destination_track_id": track.destination_track_id,
            "links": links,
        }

    @app.get("/alternatives/{track_id}")
    async def get_alternatives(track_id: int):
        """Search for alternative matches for a track with the live matcher.

        At most MAX_SEARCHES platform searches are made, and none when the
        answers are already stored. Only candidates that pass the matching
        gates are listed, best first.

        Args:
            track_id: Database row ID of the match under review.

        Returns:
            The first query and the accepted candidates with tier and confidence.

        Raises:
            HTTPException: If the track is unknown, or Discogs or the platform
                client cannot be used.
            RateLimitedError: When the platform asks for a long wait.
            ReauthRequiredError: When the platform login has expired, also
                when that shows while the platform client is being built.
            SearchBudgetExhaustedError: When the app has used its searches.
        """
        track = tracker.get_matched_track_by_id(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")

        try:
            tracks = get_release_tracks(track.discogs_release_id)
        except Exception as error:
            raise HTTPException(status_code=502, detail=f"Discogs lookup failed: {error}") from error

        try:
            searcher = get_searcher(track.destination)
        except (RateLimitedError, ReauthRequiredError, SearchBudgetExhaustedError):
            raise
        except Exception as error:
            raise HTTPException(status_code=500, detail=f"Failed to initialize client: {error}") from error

        source, artist = build_source(track, tracks)
        source = with_recording_evidence(
            source,
            artist,
            track.destination,
            track.discogs_release_id,
            track.discogs_track_position,
            tracker,
            finder,
        )

        queries = alternative_queries(searcher, artist, source.title)
        refused = tracker.get_rejected_ids(
            track.discogs_release_id, track.discogs_track_position, track.destination
        )
        accepted = find_alternatives(searcher, source, queries, refused)

        return {
            "track_id": track_id,
            "search_query": queries[0],
            "alternatives": [
                describe_alternative(match, track.destination, track.destination_track_id)
                for match in accepted[:MAX_ALTERNATIVES]
            ],
        }

    @app.post("/select-alternative/{track_id}")
    async def select_alternative(
        track_id: int,
        new_track_id: str = Form(...),
    ):
        """Select one of the alternative matches."""
        track = tracker.get_matched_track_by_id(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")

        tracker.update_matched_track(
            track_id=track_id,
            destination_track_id=new_track_id,
            match_confidence=1.0,
        )
        tracker.update_review_status(track_id, "approved")

        return RedirectResponse(url="/", status_code=303)

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(
        request: Request,
        destination: str | None = None,
        status: str | None = None,
        sync_id: str | None = None,
        page: int = Query(1, ge=1),
    ):
        """Display sync logs with filtering and pagination."""
        dest_filter = destination if destination in ("spotify", "soundcloud") else None
        status_filter = status if status in ("info", "success", "warning", "error") else None

        total_count = tracker.get_sync_log_count(
            destination=dest_filter,
            status=status_filter,
            sync_id=sync_id,
        )
        total_pages = max(1, (total_count + LOGS_PER_PAGE - 1) // LOGS_PER_PAGE)
        page = min(page, total_pages)

        logs = tracker.get_sync_logs(
            destination=dest_filter,
            status=status_filter,
            sync_id=sync_id,
            limit=LOGS_PER_PAGE,
            offset=(page - 1) * LOGS_PER_PAGE,
        )

        recent_syncs = tracker.get_recent_sync_ids(limit=20)

        sync_summary = None
        if sync_id:
            sync_summary = tracker.get_sync_summary(sync_id)

        status_counts = {
            "all": tracker.get_sync_log_count(destination=dest_filter, sync_id=sync_id),
            "error": tracker.get_sync_log_count(destination=dest_filter, status="error", sync_id=sync_id),
            "warning": tracker.get_sync_log_count(destination=dest_filter, status="warning", sync_id=sync_id),
            "success": tracker.get_sync_log_count(destination=dest_filter, status="success", sync_id=sync_id),
        }

        return templates.TemplateResponse(
            request,
            "logs.html",
            {
                "logs": logs,
                "recent_syncs": recent_syncs,
                "sync_summary": sync_summary,
                "status_counts": status_counts,
                "destination_filter": destination,
                "status_filter": status,
                "sync_id_filter": sync_id,
                "page": page,
                "total_pages": total_pages,
                "total_count": total_count,
            },
        )

    @app.get("/logs/{log_id}")
    async def get_log_details(log_id: int):
        """Get log entry details as JSON."""
        logs = tracker.get_sync_logs(limit=1, offset=0)
        for log in logs:
            if log.id == log_id:
                return {
                    "id": log.id,
                    "sync_id": log.sync_id,
                    "destination": log.destination,
                    "folder_id": log.folder_id,
                    "folder_name": log.folder_name,
                    "event_type": log.event_type,
                    "status": log.status,
                    "track_artist": log.track_artist,
                    "track_name": log.track_name,
                    "track_confidence": log.track_confidence,
                    "message": log.message,
                    "details": log.details,
                    "created_at": log.created_at.isoformat(),
                }
        raise HTTPException(status_code=404, detail="Log not found")

    return app
