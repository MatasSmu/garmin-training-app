# Training app + session analysis

A personal training app (FastAPI + SQLite + a vanilla-JS single page) that
syncs Garmin activities and daily health metrics, keeps a journal/calendar,
has a coach chat — and a **Tracks** tab: a GPS session analyser for
windfoil / windsurf / wingfoil / kite sessions in the spirit of WaterSpeed
Ultra. It infers the wind from how you sailed, finds every tack and gybe,
and gives you legs, polars, targets, splits, foiling stats, HR, race legs,
speed maps, gear hours and progress over time.

## Quick start

```bash
./start.sh                 # http://localhost:8731  (PORT=… HOST=… to change)
```

`start.sh` creates `venv/` and installs `requirements.txt` on first run,
refuses to start without credentials, seeds the analysis database from
`samples/*.fit|gpx` if it has no tracks yet, then serves the app. Log in
with `APP_USERNAME` / `APP_PASSWORD` (HTTP Basic).

`run_app.sh` is the lean entry point used by the `garmin-app` systemd user
service; after pulling code changes run `systemctl --user restart garmin-app`
(it doesn't reload on its own).

### Environment (`.env` in the project root, or real env vars)

| Variable | Required | Meaning |
|---|---|---|
| `APP_USERNAME`, `APP_PASSWORD` | yes | Basic-auth login for every page and API call (the app binds to the LAN) |
| `DEEPSEEK_API_KEY` | no | Enables the LLM coach chat; without it the coach uses fixed templates |
| `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` | no | Override the coach endpoint / model |
| `PORT`, `HOST` | no | Only read by `start.sh` (defaults 8731 / 0.0.0.0) |

Garmin login tokens live in `~/.garminconnect` (created by
`python garmin_export.py` / the sync). They're needed for the Garmin sync
and for the **Analyse** button; uploads and samples work without them.
Weather comes from Open-Meteo (free, no key) and map tiles from Esri /
OpenStreetMap, so those features need internet.

## Getting sessions in

* **Upload** `.fit` / `.gpx` files on *Tracks → Sessions* (drag & drop, several at once).
* **Analyse** button on the *Sessions* tab: for any synced water session it
  downloads the original FIT from Garmin and opens the analysis.
* **CLI**:
  ```bash
  venv/bin/python -m app.analysis.importer path/to/*.fit           # files
  venv/bin/python -m app.analysis.importer --garmin-since 2026-01-01  # all synced water sessions
  venv/bin/python -m app.analysis.importer --reanalyze            # refresh every cached analysis
  ```

Files are stored under `data/tracks/<sha1>.<ext>` (re-uploading the same
file is a no-op). On import a track is matched to its Garmin activity (UTC
start within 3 min) for title, spot and local time; otherwise the spot is
taken from a named session within 5 km or, failing that, reverse-geocoded
via OpenStreetMap Nominatim. Open-Meteo conditions are fetched for its
place and hours.

## What you get per session

* **Map replay** coloured by speed, VMG, foiling/planing or tack, with tack ▲ /
  gybe ● markers (hollow = touched down), mark roundings, speed heat layer,
  wind arrow, and a scrubber (play at 1–120×) synced to every chart.
* **Timeline charts**: speed (+ foiling strip), VMG, TWA, HR, distance made to
  windward. Drag to zoom all charts together, click to move the replay.
* **Maneuvers** table + min-speed-vs-loss scatter; **legs**, **best splits**,
  **up/down × port/stbd** matrix, **flights** — every row click zooms the map
  and charts to it.
* **Polar** (session p90 / median vs your personal polar), **gain/loss vs
  target** per leg, **wind** rose, shift chart and override, **HR** zones /
  HR by speed / HR in maneuvers, **race** start + marks, notes, spot, gear,
  conditions and every engine setting.

Across sessions: filterable list (sport, spot, gear, wind strength and
direction) with 2–4-way **compare**, **progress** trends, **personal polar**,
a **speed map** per spot filtered by wind direction, and a **gear log** with
hours / km / nm / top speed per item.

## Tests & checks

```bash
venv/bin/python -m pytest                       # engine on synthetic tracks + API end-to-end
venv/bin/python scripts/analyze_samples.py      # every samples/ file: summary + sanity checks
BASE=http://127.0.0.1:8731 node scripts/e2e_pages.js   # headless Chrome over every page
```

The synthetic tests (`app/analysis/synthetic.py`) simulate windward/leeward
sessions with known wind (constant, veering 20°, oscillating), tacks, gybes,
falls, GPS noise and speed spikes. Acceptance: wind within ±5° (typically
≤ 1–2°), ≥ 95 % of maneuvers detected with the right type (currently 100 %,
0 false positives on 448). `e2e_pages.js` needs `npm i puppeteer-core` and
Chrome; it fails on any JS error or failed request and cross-checks tiles,
tables, map markers and the API.

## Code map

| Path | What |
|---|---|
| `app/analysis/loader.py` | FIT/GPX → columnar `Track` (+ sport, local offset, max HR, device wind setting) |
| `clean.py` | projection, jump/spike removal, gaps, heading, turn rate, HDOP flags |
| `wind.py` | wind direction: axis, which end is upwind, shifts, overrides |
| `state.py` | foiling/planing hysteresis, flights, touchdowns |
| `maneuvers.py` | tacks, gybes, roundings, legs |
| `splits.py`, `stats.py`, `race.py` | best splits; point of sail, polars, targets, HR, speed grid; race |
| `pipeline.py` | runs it all → JSON-safe result |
| `service.py`, `conditions.py`, `importer.py` | DB storage/caching, Open-Meteo, CLI |
| `app/api/tracks.py`, `app/api/gear.py` | REST API |
| `app/static/tracks*.js`, `tracks.css` | Tracks tab (Leaflet + uPlot, vendored) |

Schema: `tracks`, `track_analysis` (gzipped result keyed by a hash of
engine version + settings + personal-polar signature, so edits re-run the
engine and unchanged sessions open instantly), `gear`, `track_gear` — see
`app/schema.sql`.

## Algorithms and their assumptions

Speeds are knots in the UI, SI inside. TWA is signed: + = port tack (wind
over the left side), − = starboard. VMG is positive upwind.

1. **Cleaning.** Doppler speed is used when the file has it (much less noisy
   than differentiated positions). A fix that implies > 60 kn both in and
   out is a position jump and is dropped. Speed spikes = 1–3-sample
   excursions whose acceleration in *and* out exceed 3 m/s² with opposite
   signs; they're interpolated (a real crash decelerates hard but doesn't
   bounce back). Gaps > 5 s split the track into segments nothing smooths
   or integrates across. Heading = course over ground from a ±2 s central
   difference, Gaussian-smoothed (σ 1.5 s) on the unit circle. Fixes with
   HDOP > 5 are flagged; Garmin files carry no HDOP, so sustained Doppler vs
   position-speed disagreement is flagged instead.
2. **Wind axis.** From steady sailing at speed, a time-weighted heading
   histogram. The wind axis is where the histogram is most mirror-symmetric
   (port/starboard close-hauled, and broad reaches, sit symmetrically
   either side), penalised by any time spent inside ±30–50° of it (the
   no-go zone). Refined by the bisector of the port/starboard close-hauled
   heading modes (weight 2) and broad-reach modes (+180°, weight 1).
3. **Which end is upwind.** A foil's downwind gap is nearly as empty as the
   upwind no-go zone, so symmetry alone is ambiguous. Weighted votes decide:
   more time is spent on the upwind side (you come back to the beach and
   upwind VMG is lower), upwind is slower, turns through the wind lose more
   speed than turns through dead downwind, the no-go gap, drift direction
   while stopped, and a prior — Open-Meteo's direction (weight 1), a manual
   conditions entry (3) or the watch's Connect IQ wind setting (0.35, it is
   often stale). On the samples this matches Open-Meteo within 0–7°.
4. **Shifts.** Each tack's typical angle to the wind is calibrated against
   the global estimate; in 20-min windows every close-hauled / broad-reach
   mode found near its expected heading yields a wind estimate (heading −
   calibrated angle), tracked window to window and clamped to ±35°. One
   tack is enough, reaches are ignored, asymmetric pointing doesn't bias
   it. You can override with a fixed direction or time keyframes.
5. **Tacks / gybes.** Tack side is held steadily (≥ 6 s, turn rate < 8°/s)
   on either side of a change; if the gap between two opposite sides is
   ≤ 40 s, following the unwrapped TWA through it says whether the bow went
   through the wind (tack) or dead downwind (gybe). Start/end = last/first
   sample within 10° of the steady heading. **Distance lost** = over −10 s…+20 s
   around the crossing, (mean steady VMG before and after) × time − progress
   actually made along the wind axis. A turn from upwind to downwind (or
   back) is flagged a **rounding** and gets no loss figure. **Success** =
   foiling/planing the whole turn.
6. **Foiling / planing.** Hysteresis on a 3-sample median of speed: on at
   10 kn, off below 8 kn (windfoil; per-sport defaults, editable). Touchdown
   = a flight that ends while recording continues.
7. **Splits.** Best average speed over 2 s, 10 s, 1 min, 1 h (time) and 100 m,
   500 m, 1 nm (distance of integrated Doppler speed), never across a gap.
8. **Point of sail.** Upwind |TWA| ≤ 75°, downwind ≥ 105°, reach between;
   stats only over steady sailing outside maneuvers.
9. **Legs** are steady sailing between maneuvers and stops (< 2 kn for 10 s),
   split when point of sail or tack changes for ≥ 15 s. **Gain** = ∫VMG dt
   (metres along the wind axis, following wind shifts). **Angle vs ref** =
   leg TWA − best-VMG angle.
10. **Polars.** Speed by |TWA| in 5° bins over steady sailing on a *held*
    course (heading within a 15° band over ±4 s). Stored as a mergeable
    speed histogram per session, so the **personal polar** (p90 per bin) is
    exact across any filter. A bin needs ≥ 15 s (30 s aggregated) and
    ≥ 0.4 % of the polar's time.
11. **Targets.** Polar mode: best VMG angle/speed from the personal polar's
    p75 curve (p50/p90 selectable), only among angles you sail regularly
    (≥ 25 % of that side's busiest bin, 25–80° up, 100–170° down). Or enter
    manual upwind/downwind targets. Legs get % of target speed, % of target
    VMG and metres gained/lost vs target.
12. **HR.** Zones as % of max HR (from the watch profile, or Settings);
    HR by 2-kn speed band; HR during maneuvers vs steady sailing; peak per
    tack / gybe.
13. **Race.** Set the start gun to split pre-start vs race. Legs run between
    marks you place (rounded in order at closest approach within 60 m, laps
    repeat) or, with no marks, automatically detected windward (upwind →
    downwind) and leeward roundings at the extreme point along the wind axis.
14. **Speed map.** 40 m cells on a spot-stable lat/lon grid (mean, max, %
    foiling); merged across sessions at a spot and filterable by wind
    direction (±30°).

## Known limits

* **Pure beam-reaching sessions** (windsurf slalom back and forth) have no
  up/down evidence; the axis is right but the direction then relies on the
  Open-Meteo / manual prior — the page warns when confidence is low.
* Wind is inferred from *your* sailing: asymmetric pointing biases the
  global direction by half the asymmetry; very short or cruise-only sessions
  can't be solved (set it manually). Open-Meteo is a model value at ~km
  resolution, not a spot measurement.
* No true wind speed from GPS, so polars aren't split by wind strength
  unless you filter the personal polar by the (model) conditions.
* Kite transitions on twin-tips reverse without turning; they're classified
  by the TWA path and can be labelled either way.
* Garmin FIT has no HDOP; quality flags are heuristics.
* 1 Hz recordings: maneuver timings are ±1 s; very fast spins (> 180°/s) can
  alias.
* Targets are only as good as the personal polar: with few sessions they
  come from the current one.
* Map tiles, Open-Meteo and the Garmin download need internet; the app
  itself (and Leaflet/uPlot, vendored in `app/static/vendor/`) runs offline.
