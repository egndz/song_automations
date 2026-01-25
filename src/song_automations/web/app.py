"""FastAPI application for reviewing track matches."""

import re
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from song_automations.clients.discogs import DiscogsClient
from song_automations.config import get_settings
from song_automations.state.tracker import StateTracker

ITEMS_PER_PAGE = 10


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

    return app
