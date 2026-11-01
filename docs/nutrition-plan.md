# Weight & nutrition – implementation plan

Spec: `NUTRITION_SPEC.md`. Targets: `gym_plan.json` → `nutrition` and `plan.athlete`.

## What the project is (as found)

* FastAPI + uvicorn, SQLite (`app/schema.sql`, `CREATE TABLE IF NOT EXISTS` on every
  start, small additive migrations in `app/db.py`), vanilla-JS SPA with tabs
  (`app/static/index.html`, `TK` helpers in `tracks-core.js`), HTTP Basic auth
  middleware on every request.
* The gym tracker (see `docs/gym-tracker-plan.md`) is the model to copy: an
  offline-first phone page `/gym/` with its own service worker (scope `/gym/`,
  cache-first shell), IndexedDB for everything the athlete does, client UUIDs and
  one idempotent `POST /api/gym/sync` (upsert by id, last-write-wins on the
  client's `updated_at`), API calls with a short timeout (laptop off ≠ phone
  offline), and an analysis tab in the SPA.
* Coach: `app/coach/engine.py` builds a context dict every turn, `responder.py`
  turns it into briefing lines for the DeepSeek system prompt.
* DeepSeek JSON calls: `app/sailing/deepseek.call_json` (JSON mode, schema
  validation, usage logging, `post_chat` is the single HTTP call tests replace).
* Tests: pytest, `tests/conftest.py` points `APP_DB_PATH` at a temp file before
  the app is imported; `tests/live_server.py` starts uvicorn on its own temp DB for
  the smoke and Playwright tests.

## Decisions

1. **Same app, new package `app/nutrition/`** (`calc.py` pure maths, `store.py` DB,
   `openfoodfacts.py`, `estimate.py`), router `app/api/nutrition.py`
   (`/api/nutrition/*`). New tables only: `weight_entries`, `foods`, `saved_meals`,
   `saved_meal_items`, `food_logs`. Nothing existing is altered.
2. **Phone page `/food/`** ("Food & weight"), built like `/gym/`: own service worker
   (`/food/sw.js`, scope `/food/`), manifest, IndexedDB database `food-tracker`.
   Three views: **Today** (logging), **Weight** (grid + chart), **Week** (summary and
   advice). The SPA gets a **"Nutrition" tab** (after Gym) with the same chart,
   status, weekly table and expenditure estimate, plus a link to the phone page;
   the Dashboard gets two tiles (trend weight, protein today).
3. **Offline/sync = the gym mechanism.** One `POST /api/nutrition/sync`
   `{foods, saved_meals, logs, weights}`. Logs, foods and saved meals have client
   UUIDs and are upserted by id with last-write-wins on the client `updated_at`;
   deletes are tombstones (`deleted = 1`) so a re-sent delete is a no-op and an
   old copy can't resurrect an entry. Weights are upserted by **(date, slot)**
   (unique), `weight_kg = null` deletes the entry (a cleared cell). Sync runs on
   load, on `online`, every 30 s and shortly after a change; every call has a
   short timeout. Re-sending the same payload never duplicates anything.
4. **Barcode lookup and quick text need the server** (Open Food Facts / DeepSeek
   run server side). Everything else – saved meals, recent foods, manual entry,
   entering label values for an unknown barcode, the weight grid – works offline.
5. **Calculations live on the server** (`app/nutrition/calc.py`, unit tested) and the
   phone caches the latest result for offline viewing. Today's totals are also
   summed on the phone so they update instantly while offline.
6. **Trend weight:** morning weights only. Hacker's Diet EMA, `T = T + 0.1·(w − T)`,
   started at the first morning weight, one step per calendar day; days without a
   morning weight are filled by linear interpolation between the neighbouring
   weights (as the Hacker's Diet does), so a gap doesn't freeze or jerk the trend.
   No extrapolation past the last weighing.
7. **Weekly rate** = (trend at the last weighing − trend `n` days earlier) / `n` × 7,
   with `n` = up to 14 days; needs at least 7 days of trend (else "not enough data").
8. **Targets from the plan:** start = `plan.athlete.bodyweight_kg` on `plan.start_date`,
   goal = `target_bodyweight_kg` at `start_date + weeks`, target rate =
   (goal − start) / weeks (= +0.625 kg/week, shown as +0.6). kcal band and protein
   target from `nutrition`. Read from the imported plan in the DB, falling back to
   the `gym_plan.json` file.
9. **Status:** "on track" if the rate is within ±0.25 kg/week of the target rate,
   below that "too slow", above "too fast"; "goal reached" once trend ≥ goal.
   Projection: today + (goal − trend) / rate weeks, only when the rate is > 0.05.
10. **Morning→evening difference** = mean of (evening − morning) over days with both;
    info only.
11. **Food model:** `foods` per 100 g (plus optional `serving_g`, `brand`); a log is
    either food + grams (values computed from the food), a saved meal (one log row
    with the meal's totals – one tap = one entry), or free text (manual / estimate)
    with fixed values. `food_logs.source` = `food | saved_meal | manual | estimate`
    (`food` covers Open Food Facts foods – the food carries its own source).
    `saved_meal_items` holds either food + grams or a free item with fixed values,
    so a meal can be saved from any of today's entries.
12. **Meal slot** defaults from the clock (breakfast < 10:30, lunch < 15:00,
    snack < 17:30, dinner < 21:30, else snack); it can be changed when editing.
13. **Fast paths:** saved meals one tap (sorted by uses in the last 30 days); recent
    foods one tap (re-logs with the last grams, editable in the entry afterwards);
    quick text → DeepSeek estimate shown as editable rows, saved only on confirm;
    barcode → camera `BarcodeDetector` (or typed code) → server Open Food Facts
    lookup cached in `foods`; unknown → enter the label once, the food is saved
    with its barcode. Grams default to the last amount used for that food
    (else its serving size, else 100 g).
14. **Weekly summary** (Mon–Sun weeks): avg kcal and protein over days with food
    logged, days protein ≥ target, trend change, status. A day counts as "food
    logged" for the expenditure estimate only when it has ≥ 50 % of the kcal
    minimum (2000 kcal) – a day with one snack logged would wreck the estimate.
15. **Expenditure estimate** over the last 28 complete days (today excluded), only
    when ≥ 14 days have both a morning weight and a logged food day:
    avg intake − Δtrend × 7700 / days. Suggested daily change =
    (target rate − actual rate) × 7700 / 7. Without enough food logs the advice is
    from the weight trend alone, as a rounded range ("gaining 0.3 kg/week → eat
    roughly 300–400 kcal more per day").
16. **Coach:** the briefing gets a compact block (trend, weekly rate, goal status and
    projection, last 7 days' kcal/protein per day, expenditure estimate if any).
17. **Tests never touch `data/garmin.db`** (existing conftest). DeepSeek and Open
    Food Facts are refused by autouse fixtures; tests that need them install fakes.
    The Playwright test runs on its own uvicorn + temp DB with a phone viewport.

## Steps

1. Schema + `calc.py` + `store.py` + unit tests.
2. API router (bootstrap, sync, weights batch, foods, saved meals, barcode,
   estimate, summary) + API tests with mocks.
3. Phone page `/food/` (IndexedDB, sync, Today/Weight/Week, scanner) + service worker.
4. SPA Nutrition tab + dashboard tiles + coach briefing.
5. Playwright E2E, smoke test additions, docs, restart the live app, verify.
