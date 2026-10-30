# Gym tracker – setup, testing and use

The gym tracker is part of the training app:

* **Phone logger:** `/gym/` is an offline-first page (PWA) for logging workouts in the gym.
* **Gym tab:** the main app (`/`) has history, progress, weekly volume and edits.
* **Coach:** the coach chat sees your recent gym workouts and can read the plan.

The plan comes from `gym_plan.json`.

## 1. Run the app

The live app is the systemd user service `garmin-app`. It runs `run_app.sh` →
uvicorn on port 8731 and doesn't reload by itself.

```bash
systemctl --user restart garmin-app      # after pulling / changing code
systemctl --user status garmin-app
curl -u "$APP_USERNAME:$APP_PASSWORD" http://localhost:8731/healthz
```

To run it by hand instead, use `./start.sh` (http://localhost:8731).

### Load or update the plan

```bash
venv/bin/python -m app.gym.plan_import            # reads gym_plan.json
venv/bin/python -m app.gym.plan_import other.json # another plan file
```

The import is idempotent: running it again prints `0 row changes`. Expected
counts for the current file:

```
{"plan_days": 4, "exercises": 29, "items_per_day": {"A": 10, "B": 10, "C": 11, "D": 6}, "locations": 2}
```

If you edit the JSON (new weights, swapped exercises) and import again, the
plan is updated in place. Workouts you already logged are not touched.

## 2. Run the tests

Tests never touch `data/garmin.db`. `tests/conftest.py` sets `APP_DB_PATH` to a
temp file, and the smoke and browser tests start their own uvicorn on a free
port with their own throwaway database.

One-time setup for the browser test:

```bash
venv/bin/pip install -r requirements.txt          # includes playwright
venv/bin/python -m playwright install chromium
```

Commands:

```bash
venv/bin/python -m pytest                          # everything (~1 min)
venv/bin/python -m pytest tests/gym                # gym: import, API, coach, e2e
venv/bin/python -m pytest tests/gym/test_e2e_offline.py   # the offline browser test
venv/bin/python -m pytest tests/test_smoke.py      # app starts, main routes answer 200
venv/bin/python -m pytest -m "not e2e"             # skip the browser test
```

The end-to-end test (Chromium, phone-sized screen) goes through these steps:

1. Opens `/gym/` online, then switches the browser offline.
2. Starts day A, ticks four sets, and changes one squat set to 8 reps at 85 kg.
3. Finishes the workout.
4. Reloads the page while still offline and checks everything is still there.
5. Goes back online and waits for "All synced".
6. Checks through the API and the DB that exactly one session with five sets arrived.

## 3. Reach the app from the phone: Tailscale + HTTPS

A service worker (what makes the page work offline) only runs on **HTTPS**
or on `localhost`. A LAN address like `http://192.168.x.x:8731` won't do.
Tailscale gives the laptop a private HTTPS address that the phone can reach
from anywhere, without opening any ports.

### Laptop (Ubuntu)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                      # opens a browser login
sudo tailscale set --operator=$USER    # lets you run `tailscale serve` without sudo
```

In the Tailscale admin console (https://login.tailscale.com/admin/dns):

* make sure **MagicDNS** is on, and
* under **HTTPS Certificates**, click **Enable HTTPS**.

Then publish the app on the tailnet only (not the public internet):

```bash
tailscale serve --bg 8731
tailscale serve status
```

`status` prints the address, for example
`https://<laptop-name>.<tailnet-name>.ts.net/` → `http://127.0.0.1:8731`.
The `--bg` setting survives reboots. To stop it, run `tailscale serve --https=443 off`
(or `tailscale serve reset`). The first HTTPS request can take a few seconds
while the certificate is issued.

### Phone (Android)

1. Install **Tailscale** from the Play Store and sign in with the same account. Leave it connected.
2. In Chrome, open `https://<laptop-name>.<tailnet-name>.ts.net/gym/`.
3. Log in with `APP_USERNAME` / `APP_PASSWORD` (the same Basic-auth login as the main app).

The app keeps its Basic-auth login behind Tailscale too. If Chrome ever forgets
the login, the sync pill shows "… · sign in". Tap it to log in again.

## 4. Install the logger on Android ("Add to home screen")

1. With `/gym/` open in Chrome (step 3) and **while the laptop is on**, wait until
   the page shows the plan. That's when the page, plan, locations and last numbers
   are saved on the phone.
2. Chrome menu **⋮** → **Add to home screen** → **Install** (on some versions:
   **Install app**).
3. The "Gym" icon now opens the logger full screen. It opens on today's plan day.
   On a day with no gym session in the plan, it opens on the next one.

Open it once while connected after every plan change, so the phone picks up the new plan.

## 5. Test it yourself (airplane mode)

1. Laptop on, phone on Tailscale: open the Gym app once. The pill should say **All synced**.
2. Turn on **airplane mode** (or switch the laptop off).
3. Open the Gym app from the home screen. Pick a day, tap **Start workout**, and tick a
   few sets with ✓. Change one set with − / +, add a note, and tap **Finish workout**.
   The pill should say **1 session waiting to sync**.
4. Close the app completely, lock the phone, then reopen the app. The workout
   should still be there, including an unfinished one.
5. Turn airplane mode off (and the laptop on). Within a few seconds (on reopening,
   on reconnect, or within 30 s) the pill should change to **All synced**. Tapping
   the pill syncs right away.
6. On the laptop, open the **Gym** tab: the workout is listed with its sets. Click
   it to see planned vs done and to edit the location or times.

Notes:

* Every tap is saved on the phone at once, so closing the app or a dead
  battery loses nothing. Workouts stay on the phone until the server has
  confirmed them.
* With mobile data on but the laptop off, the phone is "online" but the server
  isn't there. The app treats that like offline: requests time out after a few seconds and retry later.
* The rest timer keeps the right time if the phone locks. Chrome can only
  vibrate while the app is on screen, though.
* Edits made on the laptop win over an older copy still on the phone. The
  phone takes the laptop's version at its next sync.

## 6. Garmin link

When the regular Garmin sync brings in a **strength_training** activity that
overlaps a logged workout (start − 30 min … end + 30 min), the workout is
linked to it. The Gym tab then shows that activity's duration and avg / max HR.
Linking happens on every gym sync and whenever the Gym tab loads, so it also
works when Garmin syncs later than the phone. Garmin sync itself is unchanged.

## 7. Later: moving to the Raspberry Pi

* **Install:** copy the project (`git clone` + `.env`) and the data
  (`data/garmin.db`, `data/tracks/`). Also copy the Garmin tokens in `~/.garminconnect`.
  Then run `./start.sh` once to create the venv. On a 64-bit Pi OS, Playwright is
  only needed if you want to run the browser test there.
* **Services:** copy `~/.config/systemd/user/garmin-app.service` and the
  `garmin-sync.timer` / `.service` units (adjust `WorkingDirectory` /
  `ExecStart` paths). Run `loginctl enable-linger $USER` so user services start
  at boot without a login.
* **Time zone:** set it to yours (`sudo timedatectl set-timezone Europe/Vilnius`).
  Plan weeks, dates in the Gym tab and the Garmin link use the server's local time.
* **Tailscale:** install it on the Pi and run `tailscale serve --bg 8731` there.
* **Sync the phone first.** The phone keeps unsynced workouts per web address
  (origin). Before switching, open the Gym app with the laptop on and wait for
  **All synced**. You can keep the same address: remove the laptop from the
  tailnet (or rename it), then give the Pi the laptop's old machine name in the
  admin console. The installed app keeps working. With a new address,
  install the app again from the new URL.
* **SQLite on an SD card:** it's fine for this load. An SSD/USB disk is nicer, and
  keep backups of `data/garmin.db` (e.g. a nightly `sqlite3 data/garmin.db ".backup …"`).
