# Gym tracker – implementation plan

Spec: `GYM_TRACKER_SPEC.md`. Plan data: `gym_plan.json`.

## What the project is (as found)

* **Stack:** FastAPI + uvicorn, SQLite (`data/garmin.db`, plain `sqlite3`, schema in
  `app/schema.sql` run with `CREATE TABLE IF NOT EXISTS` on every start), vanilla-JS
  single page (`app/static/index.html` + `app.js`, Session map in `tracks*.js` with
  shared helpers on `window.TK`, uPlot for charts). No build step, no JS framework.
* **Routing:** one `APIRouter` per area in `app/api/*.py`, included in `app/main.py`;
  DB connection through the `get_db` dependency (`app/api/deps.py`).
* **Auth:** HTTP Basic middleware on every request (`app/auth.py`), static files included.
* **Coach:** `app/coach/engine.py` builds a context dict every turn,
  `responder.py` turns it into the DeepSeek system prompt and offers tools from
  `app/coach/tools.py`.
* **Tests:** pytest (`tests/`), API tests use `TestClient` + a throwaway SQLite
  file via `app.dependency_overrides[get_db]`.
* **Live app:** systemd user unit `garmin-app` (`run_app.sh`, port 8731), no auto-reload.

## Decisions

1. **Tables are prefixed `gym_`** (`gym_locations`, `gym_plans`, `gym_plan_days`,
   `gym_exercises`, `gym_plan_exercises`, `gym_sessions`, `gym_set_logs`). The app
   already has a `sessions` table (Garmin activities), so an unprefixed
   `workout_sessions`/`locations` would be confusing. Added to `schema.sql` as new
   `CREATE TABLE IF NOT EXISTS` statements only – nothing existing is altered.
2. **IDs:** sessions, sets and user-added locations use client-generated UUIDs
   (text primary keys). Seeded locations keep their JSON ids (`bsb`, `zb`);
   exercises keep their JSON keys (`pull_up` …) as primary key so the import is
   naturally idempotent.
3. **Plan import** `python -m app.gym.plan_import [gym_plan.json]`: upserts the plan
   (unique on name + start date), days (unique plan + letter), exercises (by key),
   plan items (unique day + order; items no longer in the JSON are removed),
   locations (by id). The whole JSON is also stored in `gym_plans.source_json`,
   which is where the coach reads `cardio`, `hr_zones` and `nutrition` from (no UI).
4. **Sync** `POST /api/gym/sync` takes `{locations: [...], sessions: [...]}` where each
   session carries its full set list. Upsert by UUID; sets of that session that are
   not in the payload are deleted (so un-ticking a set offline syncs correctly).
   Last-write-wins on the client's `updated_at`, so a stale phone copy can't
   overwrite an edit made later on the laptop. Posting the same payload twice is a no-op.
5. **Logger is its own page at `/gym/`** (same app, same auth, same DB, same CSS
   tokens), with its own service worker (`/gym/sw.js`, scope `/gym/`) and manifest
   (`start_url /gym/`). Making the whole SPA (Leaflet maps, Session map, coach…)
   work offline would be large and fragile; the logger only needs a small page.
   The **analysis lives in a new "Gym" tab** in the main SPA (as the other features
   do), with an "Open logger" link.
6. **Offline strategy:** the service worker serves the logger shell
   stale-while-revalidate (cache first, refresh in background). API data (plan,
   locations, last numbers) is cached in IndexedDB by the page. All API calls use
   a short timeout, because with the laptop off the phone is usually *online*
   (mobile data) but the server is unreachable – `navigator.onLine` is not trusted.
   Sync runs on load, on the `online` event, every 30 s and a few seconds after
   any change.
7. **In-progress workout** is written to IndexedDB on every tap (including the rest
   timer's end time), so closing the app / locking the phone loses nothing. It is
   also synced while in progress when the server is reachable.
8. **Pre-fill:** reps = top of the planned range; weight = planned kg (bottom of a
   kg range), or – when the plan gives no kg – the last logged weight for that
   exercise; seconds = planned (bottom of a range). ✓ stores the shown values;
   `completed_as_planned` is true only if they were not changed from the pre-fill.
   Tapping a done set again un-ticks it.
9. **RPE / feeling (1–5) is per exercise** in the UI and is stored on every set of
   that exercise (`gym_set_logs.rpe`), keeping the spec's table shape.
10. **Volume** = Σ reps × kg over sets with a weight; per-side exercises count
    ×2 (reps are per leg). Plan weeks start at the plan's `start_date`
    (2026-11-01 = week 1).
11. **Garmin link:** done lazily (after every sync and when the history is listed),
    against `sessions` rows of type `strength_training` whose
    `[startTimeGMT, +duration]` overlaps the workout's `[start − 30 min, end + 30 min]`;
    largest overlap wins, an activity is linked to at most one workout. Garmin sync
    code is untouched.
12. **Coach:** the briefing gets a few compact lines (plan week, last ~6 gym sessions
    with sets done/planned and top sets). Two read-only tools: `get_gym_session`
    (full sets of one workout) and `get_gym_plan` (plan + cardio + HR zones +
    nutrition from the stored JSON).
13. **Tests never touch `data/garmin.db`:** `config.DB_PATH` can be overridden by the
    `APP_DB_PATH` env var, and `tests/conftest.py` points it at a temp file before
    the app is imported (importing `app.main` runs `init_db`). The Playwright test
    starts its own uvicorn on a free port with its own temp DB.
14. **Playwright:** Python package (`playwright`, Chromium) installed in the venv, so
    the e2e test is a normal pytest test (`tests/gym/test_e2e_offline.py`, marker `e2e`).

## Steps

1. Config override + schema tables + import script (+ tests).
2. Gym store/API: bootstrap, sync, sessions list/detail/edit, locations, exercise
   history, weekly volume, Garmin linking (+ tests).
3. Logger page, IndexedDB store, service worker, manifest.
4. Gym tab analysis in the SPA.
5. Coach briefing + tools.
6. Playwright offline e2e test, smoke test.
7. Docs (`docs/gym-tracker-setup.md`), import into the real DB (after tests pass,
   with a backup), restart `garmin-app`, verify.
