"""On-demand Garmin sync for the web UI's Sync button.

Runs in a background thread (a sync takes 10-60 s) so the request returns
at once; the page polls status(). Only one sync runs at a time. It pulls
everything since shortly before the newest synced activity (at least the
last 3 days, at most the usual 35-day window), then downloads and analyses
any new water sessions for the Session map.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone

from app.config import DEFAULT_SYNC_WINDOW_DAYS
from app.db import get_connection, init_db

log = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict = {"running": False, "step": None, "started_at": None, "finished_at": None,
                "ok": None, "message": None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def status(conn) -> dict:
    with _lock:
        out = dict(_state)
    row = conn.execute("SELECT MAX(updated_at) FROM sessions").fetchone()
    out["last_synced"] = row[0] if row else None
    return out


def start() -> bool:
    """Kick off a sync unless one is already running. Returns True if started."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, step="Starting…", started_at=_now(), finished_at=None, ok=None, message=None)
    threading.Thread(target=_run, name="garmin-sync", daemon=True).start()
    return True


def sync_window(conn, today: date | None = None) -> date:
    today = today or date.today()
    start = today - timedelta(days=3)
    last = conn.execute("SELECT MAX(start_date) FROM sessions").fetchone()[0]
    if last:
        start = min(start, date.fromisoformat(last) - timedelta(days=1))
    return max(start, today - timedelta(days=DEFAULT_SYNC_WINDOW_DAYS))


def _run() -> None:
    from app.analysis import service
    from app.ingest.garmin_client import GarminUnavailable, login_saved_tokens
    from app.ingest.sync import run_sync

    conn = get_connection()
    try:
        init_db(conn)
        _set(step="Signing in to Garmin…")
        client = login_saved_tokens()
        since = sync_window(conn)
        before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        _set(step="Downloading activities and health data…")
        run_sync(since, date.today(), client=client)
        new_acts = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] - before

        def progress(i, n, row):
            _set(step=f"Analysing water session {i} of {n}…")

        res = service.import_garmin_water(conn, since.isoformat(), client=client, on_item=progress)
        parts = [f"{new_acts} new activit{'y' if new_acts == 1 else 'ies'}" if new_acts else "No new activities"]
        if res["imported"]:
            parts.append(f"{res['imported']} new session{'s' if res['imported'] != 1 else ''} on the Session map")
        if res["failed"]:
            parts.append(f"{res['failed']} couldn't be analysed")
        _set(ok=True, message=", ".join(parts) + ".")
    except GarminUnavailable as e:
        _set(ok=False, message=str(e))
    except Exception as e:  # noqa: BLE001 -- report anything to the UI instead of dying silently
        log.exception("Garmin sync failed")
        _set(ok=False, message=f"Sync failed ({type(e).__name__}): {e}")
    finally:
        conn.close()
        _set(running=False, step=None, finished_at=_now())
