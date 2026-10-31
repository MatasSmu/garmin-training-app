CREATE TABLE IF NOT EXISTS sessions (
  activity_id        INTEGER PRIMARY KEY,
  activity_type      TEXT NOT NULL,          -- activityType.typeKey
  activity_name      TEXT,
  start_time_local   TEXT NOT NULL,
  start_date         TEXT NOT NULL,          -- derived 'YYYY-MM-DD', for range queries
  duration_s         REAL,
  distance_m         REAL,
  elevation_gain_m   REAL,
  avg_speed_mps      REAL,
  max_speed_mps      REAL,
  avg_hr             REAL,
  max_hr             REAL,
  calories           REAL,
  location           TEXT,                   -- via location_of(), populated for windsurfing_v2 only
  raw_json           TEXT NOT NULL,          -- full original activity dict -- future-proofing
  updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_start_date ON sessions(start_date);
CREATE INDEX IF NOT EXISTS idx_sessions_activity_type ON sessions(activity_type);

CREATE TABLE IF NOT EXISTS daily_metrics (
  date                 TEXT PRIMARY KEY,
  sleep_duration_s     INTEGER,
  sleep_deep_s         INTEGER,
  sleep_light_s        INTEGER,
  sleep_rem_s          INTEGER,
  sleep_awake_s        INTEGER,
  sleep_score          INTEGER,
  hrv_last_night_avg   REAL,
  hrv_status           TEXT,
  body_battery_max     INTEGER,
  body_battery_min     INTEGER,
  stress_avg           INTEGER,
  rhr                  INTEGER,
  updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strength_exercise_sets (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  activity_id    INTEGER NOT NULL REFERENCES sessions(activity_id) ON DELETE CASCADE,
  set_index      INTEGER NOT NULL,
  exercise_name  TEXT,
  category       TEXT,
  reps           INTEGER,
  weight_kg      REAL,
  duration_s     REAL,
  raw_json       TEXT NOT NULL,
  UNIQUE(activity_id, set_index)
);
CREATE INDEX IF NOT EXISTS idx_strength_sets_activity ON strength_exercise_sets(activity_id);

CREATE TABLE IF NOT EXISTS journal_entries (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  date        TEXT NOT NULL,
  activity_id INTEGER REFERENCES sessions(activity_id) ON DELETE SET NULL,
  text        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_date ON journal_entries(date);

CREATE TABLE IF NOT EXISTS coach_conversations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- conversation_id index is created in db.init_db, after the migration that
-- adds the column to databases created before conversations existed.
CREATE TABLE IF NOT EXISTS coach_messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER REFERENCES coach_conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content         TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

-- Hand-entered calendar: planned trainings, races, plans, and days when
-- training isn't possible (reason says why). end_date is inclusive and NULL
-- for single-day events.
CREATE TABLE IF NOT EXISTS calendar_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL CHECK (kind IN ('training', 'race', 'plan', 'blocked')),
  date        TEXT NOT NULL,
  end_date    TEXT,
  time        TEXT,
  title       TEXT NOT NULL,
  reason      TEXT CHECK (reason IN ('exam', 'friends', 'party', 'other')),
  notes       TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calendar_events_date ON calendar_events(date);

-- ---------------------------------------------------------------------------
-- Session analysis (GPS tracks from FIT/GPX files). A track may link to the
-- Garmin activity it was recorded as; analysis results are cached per
-- settings hash so reopening a session is instant.
CREATE TABLE IF NOT EXISTS tracks (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  activity_id      INTEGER REFERENCES sessions(activity_id) ON DELETE SET NULL,
  file_name        TEXT NOT NULL,
  file_sha1        TEXT NOT NULL UNIQUE,
  file_path        TEXT NOT NULL,            -- relative to data/tracks/
  sport            TEXT NOT NULL,
  title            TEXT,
  start_utc        REAL NOT NULL,            -- epoch seconds
  end_utc          REAL NOT NULL,
  utc_offset_s     INTEGER NOT NULL DEFAULT 0,
  start_date       TEXT NOT NULL,            -- local 'YYYY-MM-DD'
  lat              REAL,
  lon              REAL,
  spot             TEXT,
  notes            TEXT,
  settings_json    TEXT NOT NULL DEFAULT '{}',  -- user overrides for the engine
  conditions_json  TEXT,                        -- wind/temps, manual or Open-Meteo
  summary_json     TEXT,                        -- headline numbers for lists/trends/filters
  polar_hist_json  TEXT,                        -- mergeable polar histogram
  grid_json        TEXT,                        -- speed-map cells
  polar_sig        TEXT,
  analysis_hash    TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_start ON tracks(start_utc);
CREATE INDEX IF NOT EXISTS idx_tracks_activity ON tracks(activity_id);

CREATE TABLE IF NOT EXISTS track_analysis (
  track_id     INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
  hash         TEXT NOT NULL,
  result_gz    BLOB NOT NULL,
  computed_at  TEXT NOT NULL
);

-- iQFOIL parts (board ... boom) are identified by their code, stored as typed
-- ("SC 392 899") and matched ignoring case and spaces. db._migrate_gear
-- rebuilds tables created before codes existed.
CREATE TABLE IF NOT EXISTS gear (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL CHECK (kind IN ('board', 'front_wing', 'back_wing', 'foil_mast', 'fuselage', 'sail',
                                            'mast', 'boom', 'fin', 'foil', 'wing', 'kite', 'other')),
  code        TEXT,
  name        TEXT NOT NULL,
  brand       TEXT,
  size        TEXT,
  notes       TEXT,
  retired     INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS track_gear (
  track_id  INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  gear_id   INTEGER NOT NULL REFERENCES gear(id) ON DELETE CASCADE,
  PRIMARY KEY (track_id, gear_id)
);
CREATE INDEX IF NOT EXISTS idx_track_gear_gear ON track_gear(gear_id);

-- Saved gear combinations to apply to a session in one go.
CREATE TABLE IF NOT EXISTS gear_setups (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  notes       TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gear_setup_parts (
  setup_id  INTEGER NOT NULL REFERENCES gear_setups(id) ON DELETE CASCADE,
  gear_id   INTEGER NOT NULL REFERENCES gear(id) ON DELETE CASCADE,
  PRIMARY KEY (setup_id, gear_id)
);

-- ---------------------------------------------------------------------------
-- iQFOIL analysis (app/sailing). Baselines are derived data, rebuilt from
-- tracks + track_gear after every change; gear_key '' = band-only baseline.
CREATE TABLE IF NOT EXISTS sail_baselines (
  band         TEXT NOT NULL,
  gear_key     TEXT NOT NULL DEFAULT '',
  gear_label   TEXT,
  sessions     INTEGER NOT NULL,
  rolling_n    INTEGER NOT NULL,
  enough_data  INTEGER NOT NULL,
  best_json    TEXT NOT NULL,
  rolling_json TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (band, gear_key)
);

-- One DeepSeek analysis per session: the exact packet sent and the parsed reply.
CREATE TABLE IF NOT EXISTS sail_session_analysis (
  track_id     INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
  packet_json  TEXT NOT NULL,
  result_json  TEXT NOT NULL,
  inputs_sig   TEXT,                         -- gear + note + wind it saw; differs -> "out of date"
  model        TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sail_weekly_summaries (
  week_start   TEXT PRIMARY KEY,             -- Monday, local date
  week_end     TEXT NOT NULL,
  track_ids    TEXT NOT NULL,                -- JSON list of the analyses it was built from
  result_json  TEXT NOT NULL,
  model        TEXT,
  created_at   TEXT NOT NULL
);

-- Month / season reviews, built from weekly summaries only.
CREATE TABLE IF NOT EXISTS sail_reviews (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL CHECK (kind IN ('month', 'season')),
  start_date   TEXT NOT NULL,
  end_date     TEXT NOT NULL,
  weeks        TEXT NOT NULL,                -- JSON list of week_start
  result_json  TEXT NOT NULL,
  model        TEXT,
  created_at   TEXT NOT NULL
);

-- Every LLM call, successful or not, with its token usage and cost estimate.
CREATE TABLE IF NOT EXISTS llm_usage (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  purpose            TEXT NOT NULL,          -- session | weekly | review
  ref                TEXT,                   -- track id / week start / period
  model              TEXT,
  prompt_tokens      INTEGER,
  completion_tokens  INTEGER,
  cache_hit_tokens   INTEGER,
  cache_miss_tokens  INTEGER,
  cost_usd           REAL,
  ok                 INTEGER NOT NULL,
  error              TEXT,
  created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ref ON llm_usage(purpose, ref);

-- ---------------------------------------------------------------------------
-- Gym tracker (app/gym). The plan comes from gym_plan.json via
-- `python -m app.gym.plan_import`; sessions, sets and user-added locations are
-- logged on the phone (offline first) and synced with client-generated UUIDs,
-- so re-sending the same data never duplicates anything.
CREATE TABLE IF NOT EXISTS gym_locations (
  id          TEXT PRIMARY KEY,              -- 'bsb' for seeded ones, else a UUID
  name        TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gym_plans (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL,
  start_date   TEXT NOT NULL,
  weeks        INTEGER,
  source_json  TEXT NOT NULL,                -- the whole plan file; cardio/hr_zones/nutrition are read from here
  updated_at   TEXT NOT NULL,
  UNIQUE (name, start_date)
);

CREATE TABLE IF NOT EXISTS gym_plan_days (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id              INTEGER NOT NULL REFERENCES gym_plans(id) ON DELETE CASCADE,
  code                 TEXT NOT NULL,         -- 'A' .. 'D'
  name                 TEXT,
  weekday              INTEGER NOT NULL,      -- 0 = Monday
  default_time         TEXT,                  -- '15:00-17:00'
  default_location_id  TEXT REFERENCES gym_locations(id) ON DELETE SET NULL,
  focus                TEXT,
  duration_min         INTEGER,
  warmup_min           INTEGER,
  cooldown             TEXT,
  UNIQUE (plan_id, code)
);

CREATE TABLE IF NOT EXISTS gym_exercises (
  id           TEXT PRIMARY KEY,              -- key from the plan file, e.g. 'bench_press'
  name_lt      TEXT NOT NULL,
  name_en      TEXT,
  type         TEXT NOT NULL,                 -- weighted | bodyweight | bodyweight_plus | timed | timed_weighted
  per_side     INTEGER NOT NULL DEFAULT 0,
  weight_mode  TEXT,                          -- 'total' = both dumbbells together
  notes        TEXT
);

CREATE TABLE IF NOT EXISTS gym_plan_exercises (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_day_id       INTEGER NOT NULL REFERENCES gym_plan_days(id) ON DELETE CASCADE,
  position          INTEGER NOT NULL,
  exercise_id       TEXT NOT NULL REFERENCES gym_exercises(id),
  sets              INTEGER NOT NULL,
  reps_min          INTEGER,
  reps_max          INTEGER,
  weight_kg         REAL,
  weight_kg_min     REAL,
  weight_kg_max     REAL,
  duration_sec      INTEGER,
  duration_sec_min  INTEGER,
  duration_sec_max  INTEGER,
  rest_sec          INTEGER,
  superset_group    TEXT,
  notes             TEXT,
  UNIQUE (plan_day_id, position)
);

CREATE TABLE IF NOT EXISTS gym_sessions (
  id                  TEXT PRIMARY KEY,       -- client UUID
  plan_day_id         INTEGER REFERENCES gym_plan_days(id) ON DELETE SET NULL,
  location_id         TEXT REFERENCES gym_locations(id) ON DELETE SET NULL,
  started_at          TEXT,                   -- ISO 8601 UTC
  ended_at            TEXT,
  notes               TEXT,
  garmin_activity_id  INTEGER REFERENCES sessions(activity_id) ON DELETE SET NULL,
  garmin_time_dismissed INTEGER,              -- Garmin activity whose times the athlete chose not to use
  client_updated_at   TEXT NOT NULL,          -- last-write-wins between phone and laptop edits
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gym_sessions_started ON gym_sessions(started_at);

CREATE TABLE IF NOT EXISTS gym_set_logs (
  id                    TEXT PRIMARY KEY,     -- client UUID
  session_id            TEXT NOT NULL REFERENCES gym_sessions(id) ON DELETE CASCADE,
  exercise_id           TEXT NOT NULL REFERENCES gym_exercises(id),
  set_number            INTEGER NOT NULL,
  reps                  INTEGER,
  weight_kg             REAL,
  duration_sec          INTEGER,
  rpe                   INTEGER,              -- 1-5 feeling, given per exercise
  completed_as_planned  INTEGER NOT NULL DEFAULT 0,
  done_at               TEXT
);
CREATE INDEX IF NOT EXISTS idx_gym_sets_session ON gym_set_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_gym_sets_exercise ON gym_set_logs(exercise_id);
