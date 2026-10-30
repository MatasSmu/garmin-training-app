# Gym Tracker – Spec

## Context
This project is my personal training app. It already pulls Garmin Fenix 6X data (HRV, sleep, activities), has a DeepSeek-powered coach chat, and an iQFOIL sailing analysis/replay view. It runs on my laptop; I open it on my phone (Xiaomi 14T, Chrome). Later it will move to a Raspberry Pi.

**Key constraint: my laptop is OFF (in my backpack) while I train.** The gym tracker must work on the phone with no server reachable, then sync later.

The training plan to load is in `gym_plan.json` (project root). It is my coach's 8-week winter plan, starting 2026-11-01, with 4 gym days: A (Mon), B (Wed), C (Thu), D (Sat).

## How to work
1. First explore the project: stack, framework, database, folder structure, how pages and API routes are built, existing tests. Write a short plan to `docs/gym-tracker-plan.md`, then implement it. Do not stop to ask for approval; make reasonable decisions and note them in the plan.
2. Use the existing stack, database and code style. Don't add a large new framework unless there's no reasonable alternative.
3. Do not break existing features (Garmin sync, coach chat, sailing analysis).
4. If the project is a git repo, commit in small logical steps. Never commit `.env` or any secrets.
5. Do not try to set up Tailscale or anything needing my accounts. Write instructions for me instead (see Docs).
6. **Protect my real data.** The live app runs from this folder (FastAPI/uvicorn via the systemd user service `garmin-app`, port 8731) and uses `data/garmin.db`, which holds all my Garmin history. Tests must use a separate temporary/test database, never `data/garmin.db`. Schema changes to the real DB must only add new tables/columns, never drop or alter existing ones. Run the plan import against the real DB only once the tests pass.
7. When finished, restart the live app with `systemctl --user restart garmin-app` and confirm http://localhost:8731 still responds.

## Data
Tables (adapt naming to the project's conventions):
- `locations` (id, name). Seed from `gym_plan.json`; user can add new ones from the phone, also offline.
- `plans`, `plan_days` (A–D, weekday, default time, default location), `exercises` (id, name_lt, name_en, type, per_side, weight_mode, notes), `plan_exercises` (day, order, exercise, sets, reps_min/max, weight_kg / weight_kg_min/max, duration_sec / min/max, rest_sec, superset_group, notes).
- `workout_sessions` (id, plan_day, location_id, started_at, ended_at, notes, garmin_activity_id nullable).
- `set_logs` (id, session_id, exercise_id, set_number, reps, weight_kg, duration_sec, rpe nullable, completed_as_planned bool).

All session/set/location IDs are **client-generated UUIDs**, so syncing the same data twice never creates duplicates.

Write an import script that loads `gym_plan.json` into the DB. It must be **idempotent** (running it twice gives the same result). Expected after import: 4 plan days, 29 exercises, A = 10 items, B = 10, C = 11, D = 6, 2 locations.

The `cardio`, `hr_zones` and `nutrition` sections of the JSON are not logged in the tracker. Store them where the coach chat can read them (or leave them in the JSON and load at runtime). Don't build UI for them.

## Logging page (mobile-first)
- Opens on today's planned day (by weekday); I can switch to any day A–D.
- **Start workout** button saves `started_at`. **Finish** saves `ended_at`. Both editable afterwards.
- Location picker: pre-selected from the day's default, can switch to another saved location or add a new one.
- Each exercise shows planned targets (e.g. "3 × 8–10 @ 80–90 kg") and **my numbers from the last session** of that exercise (across any day, since bench press appears on A and D).
- Each set is pre-filled with the plan. **One tap ✓** = done as planned. Otherwise adjust with +/− steppers or type a number.
  - weighted: reps + kg
  - bodyweight: reps (optional added kg)
  - timed: seconds
  - per_side exercises: reps are per leg
- Supersets (same `superset_group`) are shown together as a pair.
- Rest timer starts automatically after each ✓, using `rest_sec` (skippable). Vibrate when done if supported.
- Optional RPE / 1–5 feeling per exercise, and a session note.
- Big touch targets; usable with sweaty hands. Exercise names in Lithuanian (`name_lt`) with English small underneath.

## Offline + sync
- PWA: web manifest + service worker so the app opens and works **with no network** after it has been loaded once.
- Cache the plan, locations and last-session numbers on the phone.
- Store logged sessions/sets/new locations in IndexedDB. Sync to the server automatically when reachable (on load, on `online` event, and periodically). Server sync endpoint is idempotent (upsert by UUID).
- Show a small indicator: "X sessions waiting to sync" / "All synced".
- An in-progress workout must survive the app being closed or the phone locking.

## Garmin link
If the existing Garmin data includes strength-training activities, link each `workout_session` to the Garmin activity that overlaps its start/end time (±30 min) and show its avg/max HR and duration on the session. If there's no match, leave it empty. Don't change how Garmin sync itself works.

## Analysis (keep it simple)
- Session history list.
- Per exercise: history table + chart of top weight and estimated 1RM (Epley: weight × (1 + reps/30)).
- Weekly volume (sets × reps × kg) per week of the plan.
- Planned vs done per session (sets/reps completed vs target).
- Give the DeepSeek coach chat access to a compact summary of recent gym sessions so I can ask about them.

## Tests (required)
- Unit/API tests: import is idempotent with the expected counts; sync endpoint stores a session with sets; posting the same payload twice results in exactly one session and no duplicate sets; editing location and times works.
- **Playwright end-to-end test** (Chromium, mobile viewport, against localhost): load the app online → set browser context offline → start day A, confirm some sets with ✓, adjust one set's reps and weight, finish → reload the page while still offline and check data is still there → go back online → verify via the API that the session and all sets reached the database exactly once.
- Smoke test: the app starts and existing main pages/routes still respond with 200.

## Docs
Write `docs/gym-tracker-setup.md` covering:
- How to run the app and the tests.
- How to set up Tailscale on laptop + phone and `tailscale serve` for HTTPS (needed for the service worker on the phone).
- How to install the PWA on Android ("Add to home screen").
- How to test it myself: airplane mode, log a workout, reconnect, check it synced.
- Notes for later moving to the Raspberry Pi.

## Done when
1. Import runs and the DB has the expected counts; running it again changes nothing.
2. All unit/API tests pass.
3. The Playwright offline end-to-end test passes.
4. The existing app still starts and the smoke test passes.
5. `docs/gym-tracker-plan.md` and `docs/gym-tracker-setup.md` exist.
6. Final report: what was built, the commands run with their passing output, decisions made, and any open issues.
