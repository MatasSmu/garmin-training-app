# Weight & nutrition – setup and use

Part of the training app, built the same way as the gym tracker:

* **Phone page:** `/food/` ("Food & weight") is an offline-first page (PWA) with
  three views: **Today** (log food), **Weight** (week grid + chart) and **Week**
  (summary and advice).
* **Nutrition tab:** the main app (`/`) shows the weight chart, status against the
  plan, the expenditure estimate, the last 14 days and weekly summaries. The
  **Dashboard** has two tiles: trend weight and today's protein.
* **Coach:** the coach chat sees a short summary every turn: trend weight, weekly
  rate, goal status and projection, and the last 7 days of kcal/protein.

The targets come from `gym_plan.json` (`plan.athlete` and `nutrition`): 81 → 86 kg
from 2026-11-01 over 8 weeks (+0.625 kg/week), 4000–4200 kcal and 160 g protein a
day. Edit the JSON and re-import (`venv/bin/python -m app.gym.plan_import`) to
change them; nothing is hardcoded.

## 1. Open it on the phone

Use the same Tailscale address as the gym logger (see `docs/gym-tracker-setup.md`,
section 3), with `/food/` at the end:

```
https://<laptop-name>.<tailnet-name>.ts.net/food/
```

Log in with `APP_USERNAME` / `APP_PASSWORD`. While the laptop is on, wait for
**All synced**, then Chrome menu **⋮** → **Add to home screen** → **Install**. The
"Food" icon opens the page full screen and works without the laptop.

**HTTPS is required** for the offline part (service worker) and for the camera
barcode scanner. It works on the Tailscale `https://…ts.net` address or on
`http://localhost:8731` on the laptop, **not** on a plain LAN address like
`http://192.168.x.x:8731`.

## 2. Weight: the week grid (once a week from paper)

Keep weighing morning and evening on your normal scale and write it on paper.
Once a week:

1. Open **Weight**. The grid shows this week (Mon–Sun), 7 rows × Morning/Evening,
   filled with what's already saved.
2. Tap the first cell and type. The keyboard's **Next** (Enter) key jumps to the
   next cell, row by row (Mon morning → Mon evening → Tue morning …). A comma
   works as the decimal point. Leave a cell empty if you skipped it.
3. Tap **Save week**. To fix a value, type over it; to remove one, clear the
   cell and save.
4. **Old paper records:** use **‹** to go back week by week and type them in the
   same way.

What you see:

* **Trend weight:** an exponential moving average of the **morning** weights
  only (Hacker's Diet, smoothing 0.1). Missed days are filled in between the
  neighbouring weighings, so a gap doesn't make the trend jump.
* **Weekly rate:** how much the trend changed per week over the last 14 days
  (at least 7 days of data needed).
* **Status** against the plan's +0.6 kg/week: **On track** (within ±0.25),
  **Too slow**, **Too fast**, or **Goal reached**. Plus the date you'd reach
  86 kg at the current rate.
* **Evening vs morning:** your average difference, for information only.
* **Chart:** morning dots, lighter evening dots, the trend line and the plan's
  dashed line from 81 kg to 86 kg.

Weights typed in offline are kept on the phone and sync later. The trend is
calculated on the server, so it updates after the sync.

## 3. Food: fastest first

The **Today** view shows protein toward 160 g and kcal toward the 4000–4200 band,
then the ways to log, then today's entries by meal. The meal (breakfast / lunch /
snack / dinner) is picked from the clock; you can change it when editing. Use **‹**
to log for yesterday.

1. **Saved meals (one tap).** The buttons at the top add a whole meal to today.
   The ones you use most come first. To make one, log the meal once, then tap
   **Save as meal** next to that meal's heading, tick what belongs in it and give
   it a name (e.g. "Protein shake with plant milk", "Usual breakfast"). Delete
   them under **Manage**.
2. **Recent (one tap).** Re-logs something you had in the last 30 days. A food is
   added with the grams you used last time. The toast that appears has **Edit**
   (change grams) and **Undo**.
3. **Type it (quick text).** Type e.g. "2 burgers and 500 ml milk" and tap
   **Estimate**. DeepSeek returns the items with kcal and protein. Fix any number,
   remove rows, then tap **Add**. Nothing is saved before that. These entries are
   marked **est**. This needs the server (and `DEEPSEEK_API_KEY` in `.env`).
4. **Scan.** The camera opens and reads the barcode (Chrome on Android,
   HTTPS only). You can also type the number and tap **Look up**. The server
   looks it up on Open Food Facts and remembers the product, so it works offline
   after that. Enter the grams (the default is what you used last time, else the
   pack's serving, else 100 g) and tap **Add**. If the product isn't found (or the
   server can't be reached), enter the label values per 100 g once. The food is
   saved with that barcode, and the next scan finds it.
5. **Manual.** Name + kcal + protein (carbs/fat optional).

Tap any entry to change grams, kcal, protein, meal or time, or to delete it.

Everything except **Type it** and the online part of **Scan** works offline. The
pill at the top shows **N changes waiting to sync** until the server has
confirmed them. Sync runs on opening, on reconnect, every 30 s, or when you tap
the pill.

## 4. Week: summary and advice

* **Last 7 days:** average kcal and protein, days you hit 160 g, days logged.
* **Energy:** with **14 or more days** in the last 28 that have both a morning
  weight and a full food log (at least half the kcal minimum, 2000 kcal), the
  app estimates your expenditure:
  average intake − trend change (kg) × 7700 / days. It also shows the daily
  kcal that would give +0.6 kg/week. It's an estimate, and only as good as the
  logging. With fewer logged days, the advice comes from the weight trend only,
  e.g. "gaining 0.3 kg/week → eat roughly 300–400 kcal more per day"
  ((target − actual) × 7700 / 7).
* **Weeks:** per Mon–Sun week, average kcal and protein, protein-target days,
  weigh-ins, trend change and status.

## 5. Tests

Tests never touch `data/garmin.db`. DeepSeek and Open Food Facts are blocked in
tests and replaced by fakes where needed.

```bash
venv/bin/python -m pytest tests/nutrition                    # unit + API + browser tests
venv/bin/python -m pytest tests/nutrition/test_e2e_food.py   # Playwright only
venv/bin/python -m pytest                                    # everything
```

The Playwright test (Chromium, phone screen, its own server and database) types a
week into the grid and checks the trend and status. It then logs a saved meal and
a manual food and checks the totals. Finally it repeats a food offline, reloads
offline, comes back online, and checks that the entry reached the server exactly
once.

## 6. Data

New tables only: `weight_entries` (one row per date + morning/evening),
`foods` (per 100 g; source manual / openfoodfacts / estimate), `saved_meals` +
`saved_meal_items`, `food_logs`. Deleted food entries are kept with
`deleted = 1` so a phone that syncs late can't bring them back. A backup taken
before these tables were added is `data/garmin.db.bak-before-nutrition`.
