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
