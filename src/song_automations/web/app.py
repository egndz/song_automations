"""FastAPI application for reviewing track matches."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from song_automations.config import get_settings
from song_automations.state.tracker import StateTracker

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

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, destination: str | None = None):
        """Display flagged tracks for review."""
        flagged = tracker.get_flagged_tracks(
            high_confidence=settings.high_confidence,
            destination=destination,
        )

        stats = {
            "total": len(flagged),
            "spotify": len([t for t in flagged if t.destination == "spotify"]),
            "soundcloud": len([t for t in flagged if t.destination == "soundcloud"]),
        }

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "tracks": flagged,
                "stats": stats,
                "destination_filter": destination,
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
        """Reject a track match and remove from cache."""
        track = tracker.get_matched_track_by_id(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")

        tracker.update_review_status(track_id, "rejected")
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
