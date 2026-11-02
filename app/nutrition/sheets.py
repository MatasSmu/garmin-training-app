"""Import weigh-ins (date, time, weight, morning score, note) from the
athlete's private Google Sheet.

The app reads the sheet as a Google service account (a key file in data/),
so the sheet stays private: the athlete shares it with the service account's
e-mail as Viewer. Columns are found by their header names (English or
Lithuanian), so column order doesn't matter. Morning / evening comes from a
slot column, else the time (before 14:00 = morning), else the order of the
rows within a day.

Import only adds and updates, never deletes. Each date + slot remembers a
hash of the sheet row it last imported, so an unchanged sheet row never
overwrites an edit made in the app; changing the row in the sheet does.
The automatic import runs twice a day, at SHEETS_IMPORT_TIMES (default
10:00 and 23:30 local), catching up when the laptop was off or asleep.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import PROJECT_ROOT
from app.nutrition import store

log = logging.getLogger(__name__)

KEY_PATH = Path(os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or PROJECT_ROOT / "data" / "google-service-account.json")
# Local times of the automatic import (after the morning and the evening
# weigh-in); empty disables it. "Import now" in the Nutrition tab works any time.
IMPORT_TIMES = [t.strip() for t in os.environ.get("SHEETS_IMPORT_TIMES", "10:00,23:30").split(",")
                if re.fullmatch(r"\d{1,2}:\d{2}", t.strip())]
RETRY_MINUTES = 30
CHECK_EVERY_S = 300
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
API = "https://sheets.googleapis.com/v4/spreadsheets"
MORNING_BEFORE_HOUR = 14
NIGHT_UNTIL_HOUR = 4          # a weigh-in before 04:00 belongs to the previous day's evening
MIN_KG, MAX_KG = 25, 250
HEADER_SCAN_ROWS = 10
MAX_SKIPPED_SHOWN = 30

_lock = threading.Lock()
_creds = None


class SheetError(Exception):
    """A problem the athlete can fix (sharing, link, key, headers)."""


# ------------------------------------------------------------------ settings


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row and row["value"] is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))
    conn.commit()


def parse_sheet_url(url: str) -> tuple[str, int | None]:
    """(spreadsheet id, gid or None) from a sheet link, or a bare id."""
    url = (url or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", url)
    sid = m.group(1) if m else (url if re.fullmatch(r"[A-Za-z0-9_-]{20,}", url) else None)
    if not sid:
        raise SheetError("That doesn't look like a Google Sheets link (it should contain /spreadsheets/d/…).")
    g = re.search(r"[#&?]gid=(\d+)", url)
    return sid, int(g.group(1)) if g else None


def save_config(conn: sqlite3.Connection, url: str, tab: str | None = None) -> dict:
    sid, gid = parse_sheet_url(url)
    set_setting(conn, "sheet", {"url": url.strip(), "id": sid, "gid": gid, "tab": (tab or "").strip() or None})
    return get_setting(conn, "sheet")


def key_info() -> dict:
    if not KEY_PATH.exists():
        return {"key_present": False, "service_account_email": None, "key_error": None}
    try:
        data = json.loads(KEY_PATH.read_text())
        if data.get("type") != "service_account" or not data.get("client_email"):
            raise ValueError("not a service-account key")
        return {"key_present": True, "service_account_email": data["client_email"], "key_error": None}
    except (OSError, ValueError) as e:
        return {"key_present": True, "service_account_email": None,
                "key_error": f"{KEY_PATH.name} isn't a valid service-account JSON key ({e})."}


def status(conn: sqlite3.Connection) -> dict:
    cfg = get_setting(conn, "sheet") or {}
    return {
        **key_info(),
        "key_path": str(KEY_PATH),
        "sheet_url": cfg.get("url"),
        "tab": cfg.get("tab"),
        "configured": bool(cfg.get("id")),
        "import_times": IMPORT_TIMES,
        "running": _lock.locked(),
        "last": get_setting(conn, "sheet_last_import"),
    }


# ------------------------------------------------------------------ Google API


def _token() -> str:
    global _creds
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    info = key_info()
    if not info["key_present"]:
        raise SheetError(f"No service-account key yet: save the JSON key as {KEY_PATH}.")
    if info["key_error"]:
        raise SheetError(info["key_error"])
    if _creds is None or _creds.service_account_email != info["service_account_email"]:
        _creds = service_account.Credentials.from_service_account_file(str(KEY_PATH), scopes=SCOPES)
    if not _creds.valid:
        try:
            _creds.refresh(Request())
        except Exception as e:  # noqa: BLE001 -- google-auth raises several types
            raise SheetError(f"Google rejected the service-account key: {e}") from e
    return _creds.token


def _get(url: str, params: dict | None = None) -> dict:
    try:
        r = httpx.get(url, params=params, headers={"Authorization": f"Bearer {_token()}"}, timeout=30)
    except httpx.HTTPError as e:
        raise SheetError(f"Couldn't reach Google ({type(e).__name__}). Is the laptop online?") from e
    if r.status_code == 200:
        return r.json()
    try:
        msg = r.json().get("error", {}).get("message", r.text)
    except ValueError:
        msg = r.text
    email = key_info()["service_account_email"]
    if r.status_code == 403 and ("has not been used" in msg or "disabled" in msg):
        raise SheetError("The Google Sheets API isn't enabled in the Cloud project yet "
                         "(console.cloud.google.com → APIs & Services → Library → Google Sheets API → Enable).")
    if r.status_code == 403:
        raise SheetError(f"No access: share the sheet with {email} (Viewer).")
    if r.status_code == 404:
        raise SheetError("Sheet not found: check the link (and that it's shared with the service account).")
    if r.status_code == 400 and "Unable to parse range" in msg:
        raise SheetError("That tab name doesn't exist in the sheet.")
    raise SheetError(f"Google Sheets error {r.status_code}: {msg[:200]}")


def fetch(cfg: dict) -> tuple[list[list], str, str, list[list]]:
    """(rows, tab title, locale, displayed rows) for the configured sheet.
    rows hold real values (dates as serial numbers); displayed rows hold the
    text as the athlete sees it, used for the score -- Sheets turns a typed
    8.5 into a date (5 August) under the Lithuanian locale."""
    meta = _get(f"{API}/{cfg['id']}", {"fields": "properties(locale,title),sheets(properties(sheetId,title))"})
    tabs = [s["properties"] for s in meta.get("sheets", [])]
    tab = cfg.get("tab")
    if not tab:
        by_gid = [t for t in tabs if cfg.get("gid") is not None and t["sheetId"] == cfg["gid"]]
        tab = (by_gid or tabs)[0]["title"]
    rng = quote("'" + tab.replace("'", "''") + "'", safe="")
    data = _get(f"{API}/{cfg['id']}/values/{rng}",
                {"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"})
    shown = _get(f"{API}/{cfg['id']}/values/{rng}", {"valueRenderOption": "FORMATTED_VALUE"})
    return data.get("values", []), tab, meta.get("properties", {}).get("locale", ""), shown.get("values", [])


# ------------------------------------------------------------------ parsing

# Checked in this order; the first field whose keyword is in the header wins.
FIELDS = [
    ("score", ("score", "feel", "mood", "savijaut", "rating", "balas", "ivertin", "energ", "readiness", "jauc")),
    ("note", ("note", "pastab", "komentar", "comment", "remark", "pasta")),
    ("weight", ("weight", "svor", "kg", "mass")),
    ("date", ("date", "data", "diena", "timestamp", "laiko zyma", "day")),
    ("time", ("time", "laik", "hour", "valand")),
    ("slot", ("slot", "am pm", "period", "rytas", "vakar", "morning", "evening", "paros")),
]
MORNING_WORDS = ("morning", "rytas", "ryte", "ryt", "am")
EVENING_WORDS = ("evening", "vakaras", "vakare", "vak", "pm", "night", "naktis")
EPOCH = date(1899, 12, 30)   # Google Sheets serial day 0


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def find_columns(header: list) -> dict[str, int]:
    cols: dict[str, int] = {}
    for i, h in enumerate(header):
        n = _norm(h)
        if not n:
            continue
        for field, words in FIELDS:
            if field not in cols and any(w in n for w in words):
                cols[field] = i
                break
    return cols


def _kind(v) -> str | None:
    """What a cell looks like: date | time | weight | score | text."""
    if isinstance(v, bool) or v in (None, "") or not str(v).strip():
        return None
    if parse_date(v)[0] is not None:
        return "date"
    if isinstance(v, (int, float)):
        n = float(v)
        if 0 <= n < 1:
            return "time"                      # Sheets stores 07:24 as 0.308
    else:
        s = str(v).strip()
        if re.fullmatch(r"\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s*[ap]\.?m\.?)?", s, re.I) and _parse_time_str(s):
            return "time"
        if not re.fullmatch(r"-?\d+(?:[.,]\d+)?(?:\s*(?:kg|/\s*10))?", s, re.I):
            return "text"
        n = parse_number(s)
    return "weight" if MIN_KG <= n <= MAX_KG else "score" if -100 <= n < MIN_KG else None


def _infer_columns(rows: list[list], taken: dict[str, int]) -> dict[str, int]:
    """Columns for the fields not named by a header, from what the cells hold:
    weights of 25-250, dates, times, small numbers (score) and text (note)."""
    counts: dict[int, dict[str, int]] = {}
    for r in rows[:300]:
        for i, v in enumerate(r):
            k = _kind(v)
            if k:
                counts.setdefault(i, {}).setdefault(k, 0)
                counts[i][k] += 1
    cols = dict(taken)
    for field in ("date", "weight", "time", "score", "note"):
        if field in cols:
            continue
        kind = "text" if field == "note" else field
        free = [i for i in counts if i not in cols.values() and counts[i].get(kind)]
        if field != "note":   # a data column must be mostly that kind; notes can be anything
            free = [i for i in free if counts[i][kind] >= 0.6 * sum(counts[i].values())]
        if free:
            cols[field] = max(free, key=lambda i: counts[i][kind])
    return cols


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def _find_header(rows: list[list]) -> tuple[int, dict[str, int], dict[str, str]]:
    """(header row index or -1, {field: column}, {field: header text or 'column X'}).
    Headers (English/Lithuanian) win; anything they don't name, or a sheet
    with no header row at all, is recognised from the cell contents."""
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        named = find_columns(row)
        if "date" in named and "weight" in named:
            cols = _infer_columns(rows[i + 1:], named)
            return i, cols, {f: str(row[c]) if f in named else f"column {_col_letter(c)}" for f, c in cols.items()}
    cols = _infer_columns(rows, {})
    if "date" not in cols or "weight" not in cols:
        seen = " | ".join(str(c) for c in (rows[0] if rows else []))
        raise SheetError("Couldn't find a date and a weight column in the sheet "
                         f"(first row is: {seen or 'empty'}).")
    # Rows above the first one with a date (a title, unrecognised headers) are ignored.
    first = next(i for i, r in enumerate(rows) if cols["date"] < len(r) and parse_date(r[cols["date"]])[0])
    if first:   # count again without the rows above, so header words don't blur the columns
        cols = _infer_columns(rows[first:], {}) | {"date": cols["date"]}
    return first - 1, cols, {f: f"column {_col_letter(c)}" for f, c in cols.items()}


def _hhmm(fraction: float) -> str:
    minutes = round((fraction % 1) * 24 * 60) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_time_str(s: str) -> str | None:
    m = re.search(r"(\d{1,2})[:.h](\d{2})(?:[:.]\d{2})?\s*([ap]\.?m\.?)?", s.lower())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        h = h % 12 + (12 if m.group(3).startswith("p") else 0)
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def parse_date(v, month_first: bool = False) -> tuple[date | None, str | None]:
    """(date, HH:MM or None) from a serial number or a date string."""
    if isinstance(v, bool) or v in (None, ""):
        return None, None
    if isinstance(v, (int, float)):
        if not 20000 < v < 80000:
            return None, None
        return EPOCH + timedelta(days=int(v)), (_hhmm(v) if v % 1 else None)
    s = str(v).strip()
    m = re.match(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})", s)
        if not m:
            return None, None
        a, b, y = map(int, m.groups())
        y += 2000 if y < 100 else 0
        sep = s[m.end(1)]
        # Dots are always day.month (Lithuanian); slashes follow the sheet's locale.
        mo, d = (a, b) if (sep == "/" and month_first and a <= 12) or b > 12 else (b, a)
    t = _parse_time_str(s[m.end():])
    try:
        return date(y, mo, d), t
    except ValueError:
        return None, None


def parse_time(v) -> tuple[str | None, str | None]:
    """(HH:MM, slot word) from a time cell."""
    if isinstance(v, bool) or v in (None, ""):
        return None, None
    if isinstance(v, (int, float)):
        return _hhmm(v), None
    s = str(v)
    return _parse_time_str(s), parse_slot(s)


def parse_slot(v) -> str | None:
    words = _norm(v).split()
    if any(w in MORNING_WORDS for w in words):
        return "morning"
    if any(w in EVENING_WORDS for w in words):
        return "evening"
    return None


def parse_number(v) -> float | None:
    if isinstance(v, bool) or v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(m.group(0).replace(",", ".")) if m else None


def parse_rows(rows: list[list], locale: str = "", shown: list[list] | None = None) -> tuple[list[dict], list[dict], dict[str, int]]:
    """(entries, skipped, {field: header text}). Entries: {date, slot, weight_kg, score, note, time, row}.
    shown: the same rows as displayed text; the score is read from it when given."""
    h, cols, names = _find_header(rows)

    def score_of(i: int, r: list):
        if shown is not None and "score" in cols:
            row = shown[i] if i < len(shown) else []
            return parse_number(row[cols["score"]] if cols["score"] < len(row) else None)
        return parse_number(cell(r, "score"))

    month_first = locale.startswith("en_US")
    cell = lambda r, f: r[cols[f]] if f in cols and cols[f] < len(r) else None  # noqa: E731
    pending, skipped = [], []
    for n, r in enumerate(rows[h + 1:], start=h + 2):     # n = row number as shown in Sheets
        if not any(str(c).strip() for c in r):
            continue
        d, t = parse_date(cell(r, "date"), month_first)
        if d is None:
            skipped.append({"row": n, "reason": f"no readable date ({cell(r, 'date')!r})"})
            continue
        t2, slot = parse_time(cell(r, "time"))
        t = t2 or t
        slot = (parse_slot(cell(r, "slot")) if "slot" in cols else None) or slot
        auto = False
        if slot is None and t:
            if int(t[:2]) < NIGHT_UNTIL_HOUR:     # 00:10 is still the night before: that day's evening
                d, slot = d - timedelta(days=1), "evening"
            else:
                slot = "morning" if int(t[:2]) < MORNING_BEFORE_HOUR else "evening"
            auto = True
        w = parse_number(cell(r, "weight"))
        if w is None:
            skipped.append({"row": n, "reason": "no weight"})
            continue
        if not MIN_KG <= w <= MAX_KG:
            skipped.append({"row": n, "reason": f"weight {w} kg is out of range"})
            continue
        note = cell(r, "note")
        pending.append({"date": d.isoformat(), "slot": slot, "time": t, "weight_kg": round(w, 2),
                        "score": score_of(n - 1, r),
                        "note": str(note).strip() if note not in (None, "") else None, "row": n, "auto": auto})
    # Rows without a time or slot: first of the day = morning, second = evening.
    # Two weigh-ins that both look like mornings (08:41 and 13:10): the later is the evening.
    entries: dict[tuple[str, str], dict] = {}
    for e in sorted(pending, key=lambda e: (e["date"], e["time"] or "", e["row"])):
        auto = e.pop("auto")
        if e["slot"] is None or (auto and e["slot"] == "morning" and (e["date"], "morning") in entries
                                 and (e["date"], "evening") not in entries):
            taken = {s for (d, s) in entries if d == e["date"]}
            free = [s for s in store.SLOTS if s not in taken]
            if not free:
                skipped.append({"row": e["row"], "reason": "a third weigh-in that day (no time to tell which)"})
                continue
            e["slot"] = free[0]
        prev = entries.get((e["date"], e["slot"]))
        if prev is not None:
            skipped.append({"row": prev["row"], "reason": f"two {e['slot']} weigh-ins on {e['date']}; kept row {e['row']}"})
        entries[(e["date"], e["slot"])] = e          # a later row for the same date + slot wins
    return list(entries.values()), sorted(skipped, key=lambda s: s["row"]), names


# ------------------------------------------------------------------ import


def _hash(e: dict) -> str:
    keep = {k: e[k] for k in ("weight_kg", "score", "note", "time")}
    return hashlib.sha1(json.dumps(keep, sort_keys=True).encode()).hexdigest()


def apply_entries(conn: sqlite3.Connection, entries: list[dict]) -> dict:
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    now = datetime.now(timezone.utc).isoformat()
    for e in entries:
        h = _hash(e)
        prev = conn.execute("SELECT row_hash FROM sheet_import_rows WHERE date = ? AND slot = ?",
                            (e["date"], e["slot"])).fetchone()
        if prev is not None and prev["row_hash"] == h:
            counts["unchanged"] += 1
            continue
        res = store.upsert_weight(conn, e["date"], e["slot"], e["weight_kg"], None,
                                  note=e["note"], score=e["score"], time=e["time"])
        counts["added" if res == "created" else "updated" if res == "updated" else "unchanged"] += 1
        conn.execute("INSERT INTO sheet_import_rows (date, slot, row_hash, imported_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(date, slot) DO UPDATE SET row_hash = excluded.row_hash, imported_at = excluded.imported_at",
                     (e["date"], e["slot"], h, now))
    conn.commit()
    return counts


def run_import(conn: sqlite3.Connection, source: str = "manual") -> dict:
    """Fetch, parse and apply. Always returns (and saves) a result dict."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "message": "An import is already running."}
    started = datetime.now(timezone.utc)
    try:
        cfg = get_setting(conn, "sheet") or {}
        if not cfg.get("id"):
            raise SheetError("No sheet link saved yet.")
        rows, tab, locale, shown = fetch(cfg)
        entries, skipped, cols = parse_rows(rows, locale, shown)
        counts = apply_entries(conn, entries)
        parts = [f"{counts['added']} added", f"{counts['updated']} updated", f"{counts['unchanged']} unchanged"]
        if skipped:
            parts.append(f"{len(skipped)} skipped")
        result = {"ok": True, **counts, "rows_read": len(entries), "skipped_count": len(skipped),
                  "skipped": skipped[:MAX_SKIPPED_SHOWN], "tab": tab,
                  "columns": cols,
                  "message": f"Tab “{tab}”: " + ", ".join(parts) + "."}
    except SheetError as e:
        result = {"ok": False, "message": str(e)}
    except Exception as e:  # noqa: BLE001 -- show anything in the UI instead of dying silently
        log.exception("Sheet import failed")
        result = {"ok": False, "message": f"Import failed ({type(e).__name__}): {e}"}
    finally:
        _lock.release()
    result.update(source=source, finished_at=datetime.now(timezone.utc).isoformat(),
                  seconds=round((datetime.now(timezone.utc) - started).total_seconds(), 1))
    set_setting(conn, "sheet_last_import", result)
    if result["ok"]:
        set_setting(conn, "sheet_last_ok_at", result["finished_at"])
    return result


def last_slot(now: datetime, times: list[str] = IMPORT_TIMES) -> datetime | None:
    """The most recent scheduled import time at or before now (local, aware)."""
    best = None
    for day in (now.date(), now.date() - timedelta(days=1)):
        for t in times:
            h, m = map(int, t.split(":"))
            at = datetime.combine(day, datetime.min.time(), now.tzinfo).replace(hour=h, minute=m)
            if at <= now and (best is None or at > best):
                best = at
    return best


def is_due(now: datetime, last_ok: datetime | None, last_attempt: datetime | None,
           times: list[str] = IMPORT_TIMES) -> bool:
    """Due when a scheduled time has passed since the last successful import
    (also catches up after the laptop was off or asleep); after a failure,
    try again at most every RETRY_MINUTES."""
    slot = last_slot(now, times)
    if slot is None or (last_ok is not None and last_ok >= slot):
        return False
    return last_attempt is None or now - last_attempt >= timedelta(minutes=RETRY_MINUTES)


def _ts(v) -> datetime | None:
    return datetime.fromisoformat(v).astimezone() if v else None


def _loop(stop: threading.Event) -> None:
    from app.db import get_connection

    if stop.wait(60):          # let the app finish starting
        return
    while True:
        try:
            conn = get_connection()
            try:
                last = get_setting(conn, "sheet_last_import") or {}
                if ((get_setting(conn, "sheet") or {}).get("id") and KEY_PATH.exists()
                        and is_due(datetime.now().astimezone(), _ts(get_setting(conn, "sheet_last_ok_at")),
                                   _ts(last.get("finished_at")))):
                    run_import(conn, source="auto")
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 -- the loop must survive anything
            log.exception("Sheet auto-import tick failed")
        if stop.wait(CHECK_EVERY_S):
            return


def start_auto_import() -> threading.Event | None:
    if not IMPORT_TIMES:
        return None
    stop = threading.Event()
    threading.Thread(target=_loop, args=(stop,), name="sheet-import", daemon=True).start()
    return stop
