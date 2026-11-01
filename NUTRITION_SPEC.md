# Weight & Nutrition – Spec

## Context
This project is my personal training app (FastAPI/uvicorn, SQLite in `data/garmin.db`, runs as systemd user service `garmin-app` on port 8731). It has Garmin data, a DeepSeek coach chat, sailing analysis and a gym tracker. I use it on my phone (Xiaomi 14T, Chrome).

My goal (coach's 8-week winter plan from 2026-11-01): bodyweight **81 → 86 kg in ~8 weeks (≈ +0.6 kg/week)**, **4000–4200 kcal/day**, **~160 g protein/day** (2–2.2 g/kg). These targets are in `gym_plan.json` under `nutrition` and `plan.athlete`; read them from there, don't hardcode.

**How I weigh myself:** I have a normal (non-smart) scale. I weigh every **morning and evening** and write it on paper, because I don't want to stare at my phone in the morning. So weight entry must be a **fast batch entry**, e.g. once a week, not a daily phone habit. I also have older paper records I want to type in.

**Main design rule:** logging must be as low-effort as possible. If something takes more than a few taps, it won't get used.

## How to work
1. Explore the project first (including how the gym tracker was built). Write a short plan to `docs/nutrition-plan.md`, then implement it without stopping to ask. Note decisions in the plan.
2. Use the existing stack, DB and code style. Reuse the gym tracker's offline/sync mechanism (PWA, IndexedDB, UUID upserts) if it exists.
3. Don't break existing features (Garmin sync, coach chat, sailing analysis, gym tracker).
4. Commit in small steps. Never commit `.env` or secrets.
5. **Protect my real data:** tests use a separate test database, never `data/garmin.db`. Schema changes to the real DB only add new tables/columns.
6. When finished, restart with `systemctl --user restart garmin-app` and confirm http://localhost:8731 responds.

## Part 1 – Weight
**Data:** `weight_entries` (id UUID, date, slot = `morning` | `evening`, weight_kg, note). One entry per date+slot; re-entering updates it (upsert).

**Batch entry page (mobile-first):**
- A week grid: 7 rows (days) × 2 columns (morning / evening), already filled with any existing values.
- Numeric keypad input, tab/next moves to the next cell. Empty cells are allowed.
- Week picker to go back to earlier weeks, so I can type in old paper records.
- One Save button for the whole week.

**Calculations:**
- **Trend weight** from morning weights only (most consistent): exponential moving average (e.g. Hacker's Diet style, smoothing ~0.1), handling missing days.
- **Weekly rate** = change in trend weight per week (based on last 7–14 days).
- Compare to target rate (+0.6 kg/week) and to the goal (86 kg): on track / too slow / too fast, and projected date to reach 86 kg.
- Show the average morning→evening difference as info only.

**Weight view:** chart with daily morning dots, evening dots (lighter) and the trend line, target line from start weight to 86 kg, plus the current weekly rate and status.

## Part 2 – Food (keep it simple)
Focus on **protein and calories**. Carbs/fat stored if available but not the focus.

**Data:**
- `foods` (id, name, kcal/protein/carbs/fat per 100 g, barcode nullable, source = `manual` | `openfoodfacts` | `estimate`).
- `saved_meals` (id, name) + `saved_meal_items` (food, grams). Example: "Protein shake with plant milk", "Usual breakfast".
- `food_logs` (id UUID, date, time, meal slot, kcal, protein_g, carbs_g, fat_g, plus reference to food+grams or saved meal or free-text description).

**Ways to log (fastest first):**
1. **Saved meals:** one tap adds it to today. Most used saved meals shown at the top.
2. **Recent foods:** re-log with one tap, adjust grams if needed.
3. **Quick text:** I type e.g. "2 burgers and 500 ml milk". DeepSeek returns a JSON estimate (items, kcal, protein). I see and can edit it before saving; nothing is saved without my confirmation. Mark these entries as `estimate`.
4. **Barcode:** scan with the phone camera (BarcodeDetector API in Chrome on Android, with manual barcode entry as fallback), look up on Open Food Facts (server side, cache results in `foods`). If not found, let me enter the label values once and save the food.
5. **Manual:** name + kcal + protein.

Grams input defaults to the last amount I used for that food.

**Today view:**
- Protein progress bar toward 160 g and kcal toward 4000–4200.
- List of today's entries, editable/deletable.
- Works offline; syncs later (same mechanism as gym tracker).

## Part 3 – Weekly check & coach
- **Weekly summary:** avg kcal, avg protein, days protein target hit, trend weight change, status.
- **Expenditure estimate** (only when ≥ 14 days with both weight and food logs): average intake − (trend change in kg × 7700 / days). Show it as an estimate, and the suggested daily kcal change to reach +0.6 kg/week: (target rate − actual rate) × 7700 / 7. If food logging is incomplete, base the advice on the weight trend only (e.g. "gaining 0.3 kg/week → eat roughly 300–400 kcal more per day").
- Give the DeepSeek coach chat access to a compact summary: weight trend + weekly rate + goal status + last 7 days nutrition.
- Add weight trend and today's protein to the main dashboard if there is one.

## Tests (required)
- Unit tests: trend EMA with missing days; weekly rate; status vs target; expenditure formula; weight upsert (same date+slot twice → one entry); food log totals for a day; saved meal adds correct totals.
- API tests: batch save a week of weights; log saved meal; DeepSeek quick-text endpoint with the DeepSeek call **mocked** (no real API calls in tests); Open Food Facts lookup **mocked**.
- Playwright E2E (Chromium, mobile viewport, localhost): enter a week of weights in the grid and save → weight view shows trend and status; log a saved meal and a manual food → today view shows correct protein/kcal totals; repeat one food log offline and confirm it syncs exactly once after going online.
- Smoke test: the app starts and existing pages (including gym tracker) still return 200; existing tests still pass.

## Docs
`docs/nutrition-setup.md`: how to use the weight grid, saved meals, quick text and barcode scanning; note that barcode scanning needs HTTPS (works via Tailscale serve or localhost).

## Done when
1. All new unit/API tests and existing tests pass.
2. The Playwright E2E test passes.
3. The live app restarts and http://localhost:8731 responds; gym tracker still works.
4. `docs/nutrition-plan.md` and `docs/nutrition-setup.md` exist.
5. Final report: what was built, commands run with passing output, decisions, open issues.
