from __future__ import annotations

from html import escape
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from app import config
from app.api import (
    calendar, coach, dashboard, gear, gym, journal, metrics, nutrition, plans, sailing, sessions, sync, tracks,
)
from app.auth import require_auth
from app.db import get_connection, init_db
from app.nutrition import sheets

STATIC_DIR = Path(__file__).with_name("static")

app = FastAPI(title="Training App")

app.middleware("http")(require_auth)
# Session analyses carry per-second series (~1-2 MB of JSON); gzip cuts that ~5x.
app.add_middleware(GZipMiddleware, minimum_size=2048)


@app.middleware("http")
async def no_stale_assets(request, call_next):
    # Without this, browsers reuse a cached app.js indefinitely (heuristic
    # freshness from Last-Modified) and never see UI updates. no-cache still
    # allows cheap 304 revalidation.
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response

init_db(get_connection())
# Pulls new weigh-ins from the athlete's Google Sheet every few minutes (no-op until set up).
sheets.start_auto_import()

app.include_router(sessions.router)
app.include_router(metrics.router)
app.include_router(journal.router)
app.include_router(dashboard.router)
app.include_router(coach.router)
app.include_router(calendar.router)
app.include_router(tracks.router)
app.include_router(gear.router)
app.include_router(sailing.router)
app.include_router(sync.router)
app.include_router(gym.router)
app.include_router(plans.router)
app.include_router(nutrition.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _versioned(html: str, name: str) -> str:
    # A changing ?v= makes the browser fetch the new file even if it holds a
    # cached copy it believes is fresh (which bypasses the no-cache header).
    version = int((STATIC_DIR / name).stat().st_mtime)
    return html.replace(f"/static/{name}\"", f"/static/{name}?v={version}\"")


@app.get("/")
def index():
    html = (STATIC_DIR / "index.html").read_text()
    for name in ("app.js", "style.css", "tracks-core.js", "tracks-detail.js", "tracks.js", "tracks.css", "gym-analysis.js", "plans.js",
                 "weight-chart.js", "nutrition.js"):
        html = _versioned(html, name)
    photo = config.background_photo()
    html = html.replace("{{ATHLETE_NAME}}", escape(config.ATHLETE_NAME))
    html = html.replace("{{BODY_CLASS}}", "has-photo" if photo else "")
    if photo:   # ?v= so a new photo shows at once
        css = f':root{{--bg-photo:url("/background-photo?v={int(photo.stat().st_mtime)}")}}'
        html = html.replace("</head>", f"<style>{css}</style>\n</head>", 1)
    return HTMLResponse(html)


@app.get("/background-photo", include_in_schema=False)
def background_photo():
    """The athlete's own photo behind the app (newest picture in data/background/, not in git)."""
    photo = config.background_photo()
    if photo is None:
        return Response(status_code=404)
    return FileResponse(photo, media_type=config.PHOTO_TYPES[photo.suffix.lower()],
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})
