from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import calendar, coach, dashboard, gear, journal, metrics, sessions, tracks
from app.auth import require_auth
from app.db import get_connection, init_db

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

app.include_router(sessions.router)
app.include_router(metrics.router)
app.include_router(journal.router)
app.include_router(dashboard.router)
app.include_router(coach.router)
app.include_router(calendar.router)
app.include_router(tracks.router)
app.include_router(gear.router)


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
    for name in ("app.js", "style.css"):
        html = _versioned(html, name)
    return HTMLResponse(html)
