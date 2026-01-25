"""FastAPI application for reviewing track matches."""

import math
import re
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from song_automations.clients.discogs import DiscogsClient
from song_automations.clients.soundcloud import SoundCloudClient
from song_automations.clients.spotify import SpotifyClient
from song_automations.config import get_settings
from song_automations.matching.fuzzy import parse_track_title, score_candidate
from song_automations.state.tracker import StateTracker

ITEMS_PER_PAGE = 10
LOGS_PER_PAGE = 50


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

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI app instance.
    """
    app = FastAPI(
        title="Song Automations Review",
        description="Review flagged track matches",
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    settings = get_settings()
    tracker = StateTracker(settings.db_path)
    discogs = DiscogsClient(settings) if settings.discogs_user_token else None

    release_cache: dict[int, dict] = {}

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
            "index.html",
            {
                "request": request,
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
    async def approve_all(destination: str | None = None):
        """Approve all currently displayed tracks."""
        flagged = tracker.get_flagged_tracks(
            high_confidence=settings.high_confidence,
            destination=destination,
        )
        for track in flagged:
            if track.id:
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
        """Search for alternative matches for a track."""
        track = tracker.get_matched_track_by_id(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")

        release_info = get_release_info(track.discogs_release_id)
        label = ""
        if release_info and release_info.get("labels"):
            label = release_info["labels"][0].get("name", "")

        parsed = parse_track_title(track.track_name, track.artist)

        queries = [parsed.search_query]
        if parsed.version:
            queries.append(parsed.fallback_query)
        if label:
            queries.append(f"{label} {parsed.base_title}")
        if parsed.remixer:
            queries.append(f"{parsed.remixer} {parsed.base_title}")
        queries.append(parsed.base_title)

        try:
            if track.destination == "spotify":
                client = SpotifyClient(settings)
            else:
                client = SoundCloudClient(settings)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to initialize client: {e}")

        all_results = []
        seen_ids = set()

        for query in queries[:5]:
            try:
                results = client.search_tracks(query, limit=10)
                for result in results:
                    result_track = result.track
                    track_id_val = result_track.id
                    if track_id_val in seen_ids:
                        continue
                    seen_ids.add(track_id_val)

                    if track.destination == "spotify":
                        candidate_title = result_track.name
                        candidate_artist = result_track.artist
                        popularity = result_track.popularity
                        is_verified = result.is_verified
                    else:
                        candidate_title = result_track.title
                        candidate_artist = result_track.artist
                        raw_plays = result_track.playback_count or 0
                        popularity = int(math.log10(raw_plays + 1) / 6 * 100)
                        is_verified = result.is_verified

                    total_score, artist_score, title_score, verified_bonus, pop_score = score_candidate(
                        parsed_track=parsed,
                        candidate_title=candidate_title,
                        candidate_artist=candidate_artist,
                        is_verified=is_verified,
                        popularity=popularity,
                        max_popularity=100,
                        label=label,
                    )

                    if track.destination == "spotify":
                        embed_url = f"https://open.spotify.com/embed/track/{track_id_val}?utm_source=generator&theme=0"
                        external_url = f"https://open.spotify.com/track/{track_id_val}"
                    else:
                        embed_url = f"https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/tracks/{track_id_val}&color=%23ff5500&auto_play=false&hide_related=true&show_comments=false"
                        external_url = result_track.permalink_url if hasattr(result_track, 'permalink_url') else ""

                    all_results.append({
                        "track_id": str(track_id_val),
                        "title": candidate_title,
                        "artist": candidate_artist,
                        "score": round(total_score, 3),
                        "artist_score": round(artist_score, 3),
                        "title_score": round(title_score, 3),
                        "is_verified": is_verified,
                        "embed_url": embed_url,
                        "external_url": external_url,
                        "is_current": str(track_id_val) == track.destination_track_id,
                    })
            except Exception:
                continue

        all_results.sort(key=lambda x: x["score"], reverse=True)

        return {
            "track_id": track_id,
            "search_query": parsed.search_query,
            "alternatives": all_results[:10],
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
            "logs.html",
            {
                "request": request,
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
