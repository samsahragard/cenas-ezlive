-- Cena performance DB -- CK / Mini_IT13 is the source of truth (Sam #2896/#2901).
-- Engine: SQLite. Schema version 1.
-- DESIGN RULE: restaurant sales dollars are PHYSICALLY isolated in perf_internal.
-- The sanitized push to the app reads perf_period/time_entry ONLY, so sales can
-- never leak into an employee-facing payload "by construction" (not template-hide).

-- Employee dimension + cena<->toast link keys.
CREATE TABLE IF NOT EXISTS employee (
  cena_employee_id   INTEGER PRIMARY KEY,
  toast_employee_id  TEXT,
  full_name          TEXT NOT NULL,
  active             INTEGER NOT NULL DEFAULT 1,
  updated_at         TEXT
);

-- Employee <-> store assignments (people can work both stores).
CREATE TABLE IF NOT EXISTS employee_store (
  cena_employee_id   INTEGER NOT NULL,
  store_key          TEXT NOT NULL,
  PRIMARY KEY (cena_employee_id, store_key)
);

-- Per-employee per-period NORMALIZED performance (SANITIZED / employee-visible).
-- Contains NO restaurant sales dollars.
CREATE TABLE IF NOT EXISTS perf_period (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  cena_employee_id    INTEGER NOT NULL,
  toast_employee_id   TEXT,
  store_key           TEXT NOT NULL,
  period              TEXT NOT NULL,          -- today | week | month | last30
  period_start        TEXT NOT NULL,          -- ISO date
  period_end          TEXT NOT NULL,          -- ISO date
  reg_hours           REAL DEFAULT 0,
  ot_hours            REAL DEFAULT 0,
  total_hours         REAL DEFAULT 0,
  base_pay            REAL DEFAULT 0,         -- employee earnings (rate*hours); NOT sales
  tips                REAL DEFAULT 0,
  tip_pct             REAL,
  service_json        TEXT,                   -- service metrics (aick Phase 1 fills)
  attendance_json     TEXT,                   -- late / no-show inputs
  rank_in_store       INTEGER,                -- ranking output (peer-visible summary)
  rank_metric         REAL,                   -- composite score used for ranking
  computed_at         TEXT,
  UNIQUE (cena_employee_id, store_key, period)
);

-- INTERNAL ONLY. Restaurant sales + raw scoring inputs. NEVER pushed to the app,
-- NEVER in any employee-facing payload. Used only for scoring/ranking math on CK.
CREATE TABLE IF NOT EXISTS perf_internal (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  cena_employee_id    INTEGER NOT NULL,
  store_key           TEXT NOT NULL,
  period              TEXT NOT NULL,
  sales_dollars       REAL,                   -- INTERNAL ONLY
  sales_attributed    REAL,                   -- INTERNAL ONLY
  scoring_json        TEXT,                   -- INTERNAL ONLY
  computed_at         TEXT,
  UNIQUE (cena_employee_id, store_key, period)
);

-- Time entries (explain hour/pay totals). Pay rate is employee-own; no sales.
CREATE TABLE IF NOT EXISTS time_entry (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  cena_employee_id    INTEGER,
  toast_employee_id   TEXT,
  store_key           TEXT,
  business_date       TEXT,
  clock_in            TEXT,
  clock_out           TEXT,
  reg_hours           REAL DEFAULT 0,
  ot_hours            REAL DEFAULT 0,
  hourly_rate         REAL,
  tips                REAL DEFAULT 0,
  tips_declared       INTEGER DEFAULT 1,   -- v2/N4: 0 = neither cc nor cash tip declared (null, not $0)
  needs_review        INTEGER DEFAULT 0,   -- v2/N5 (Sam #2973): 1 = likely missed-punch/auto-closed
  review_reason       TEXT,                -- v2/N5: human-readable reason when needs_review=1
  source              TEXT,
  UNIQUE (toast_employee_id, store_key, business_date, clock_in)
);

-- Refresh audit log (Phase 2 writes one row per run).
CREATE TABLE IF NOT EXISTS sync_run (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at          TEXT,
  finished_at         TEXT,
  scope               TEXT,                   -- yadira | all | <cena_employee_id>
  period              TEXT,
  status              TEXT,                   -- ok | fail | partial | dry-run
  employees_processed INTEGER DEFAULT 0,
  rows_written        INTEGER DEFAULT 0,
  error               TEXT,
  note                TEXT
);

-- Schema versioning / integrity meta.
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE INDEX IF NOT EXISTS idx_perf_period_emp  ON perf_period (cena_employee_id);
CREATE INDEX IF NOT EXISTS idx_time_entry_emp   ON time_entry (cena_employee_id, business_date);
CREATE INDEX IF NOT EXISTS idx_perf_internal_emp ON perf_internal (cena_employee_id);
