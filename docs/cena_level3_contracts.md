# C.E.N.A. Level 3 - Frozen Build Contracts

This file is the single source of truth for the Level 3 build (branch `cena-sql-level3`).
Every subagent reads this FIRST. Contracts here are FROZEN: build to them exactly.
If reality forces a deviation, document it in your report; do not silently change a signature.

## 0. Environment facts (verified 2026-06-09)

- Repo root (worktree): `C:\Users\sam\cenas-ezlive-l3`, branch `cena-sql-level3` off origin/main d4dfffd.
- Python 3.14.4 (global), pytest 9.0.3, sqlglot 30.10.0 installed, `google-genai` importable, `anthropic` importable and key WORKS.
- NO numpy/pandas. Allowed deps: stdlib, sqlglot, google-genai, anthropic. Nothing new in requirements.txt without need (sqlglot + google-genai are already specced there).
- pytest config: `pytest.ini` sets `pythonpath = .`, `testpaths = tests`. CI runs `pytest -q tests/` with NO DATABASE_URL (tests must be hermetic: no real DBs, no network, no secrets).
- Store keys: canonical `copperfield` / `tomball` (None = both). Use `app.services.assistant_routing_shared.normalize_store_key`. Never show raw `store_N` in answers.
- Secrets resolution: `app.services.assistant_routing_shared.read_secret(name)` (env var -> `{name}_FILE` -> default paths).
- LLM reality on this machine: there is NO working Gemini key (the `google_api_key.txt` fallback is a Maps key, BLOCKED for generativelanguage.googleapis.com, verified 403 API_KEY_SERVICE_BLOCKED). The Anthropic key at `C:\Users\sam\cena-secrets\anthropic_api_key.txt` WORKS (verified live).
  Therefore the LLM layer MUST be provider-pluggable: Gemini primary when a key resolves AND works, Anthropic fallback (`claude-haiku-4-5-20251001` default for reasoning steps, env-overridable), injectable fake client for tests. Eval will run on the Anthropic path until Sam installs a real Gemini key.

## 1. Data sources (env-overridable defaults, verified live today)

Base data dir: `CENA_L3_DATA_DIR` default `C:\Users\sam\cena-l3data`
(subdirs: `snapshots\`, `memory\` created on demand).

| alias    | env override          | default path                                          | contents (verified) |
|----------|-----------------------|-------------------------------------------------------|---------------------|
| appdb    | CENA_L3_SRC_APPDB     | C:\Users\sam\cenas-ezlive\dev_local.db                | app DB copy. orders n=8 (2026-05-25..29), order_items empty, drivers n=13, kitchen/fresh-food/manager tables, signals, tasks. 89 tables. |
| toast    | CENA_L3_SRC_TOAST     | C:\Users\sam\cena-perfdb\perf.sqlite                  | time_entry n=1784 (2026-05-11..2026-06-09, per-shift labor incl hourly_rate/tips), perf_period n=380, perf_internal n=380 (ISOLATED sales lane), employee, employee_store. |
| toastdm  | CENA_L3_SRC_TOASTDM   | C:\Users\sam\cena-perfdb\datamart\datamart.sqlite     | dm_time_entry n=2293 (2026-05-04..2026-06-09, has base_pay/tips), dm_schedule n=1865 (start_at/end_at/position/status), dm_internal_sales n=380 (PERIOD-based: today/week/month/last30 - NOT daily), dm_profile n=88, dm_rank, dm_attendance n=352. |
| ordersdc | CENA_L3_SRC_ORDERSDC  | C:\Users\sam\cena-driverdc\_live\ordersdc.sqlite      | dm_order n=846 (2026-02-10..2026-08-01, full ezCater economics, customer_hash pseudonym), dm_order_item n=1092 (name/category/qty/line_total), dm_order_timing n=846 (on_time), dm_customer n=609. |
| driverdc | CENA_L3_SRC_DRIVERDC  | C:\Users\sam\cena-driverdc\_live\driverdc.sqlite      | dm_driver n=48, dm_delivery n=20, dm_pay n=9, dm_driver_score. |

DATA REALITY (drives gold questions + analytics): the app-DB copy has only 8 orders;
the RICH order history is ordersdc (846 orders / 1092 items / 6 months). Daily sales =
ezCater catering revenue from ordersdc.dm_order. Labor = toast.time_entry (canonical) and
toastdm.dm_time_entry (cross-check). In-store restaurant sales exist only PERIOD-based and
per-employee (isolated lane) - daily in-store sales is NOT derivable; the analytics column
for it stays NULL with a documented gotcha.

## 2. Executor connection layout (FROZEN)

- `refresh_snapshots()` copies each source via the sqlite3 backup API into
  `%CENA_L3_DATA_DIR%\snapshots\{appdb,toast,toastdm,ordersdc,driverdc}.sqlite`,
  then calls `cena_sql_analytics.build_analytics_db()` which writes
  `%CENA_L3_DATA_DIR%\snapshots\cena_analytics.db`. Writes a `snapshot_meta.json`
  (per-source: source path, copied_at, ok/error, row hint). Missing sources are recorded,
  not fatal.
- `run_readonly_sql` opens MAIN = `snapshots\cena_analytics.db` via URI
  `mode=ro&immutable=1`, then ATTACHes the five snapshot files read-only as schemas
  `appdb`, `toast`, `toastdm`, `ordersdc`, `driverdc`. ATTACH is trusted setup code only;
  user SQL containing ATTACH is rejected.
- SQL therefore references analytics tables UNQUALIFIED and raw tables QUALIFIED:
  `appdb.orders`, `toast.time_entry`, `toastdm.dm_schedule`, `ordersdc.dm_order`,
  `driverdc.dm_driver`.
- Caps: 5s wall timeout (progress handler interrupt), hard row cap 1000 (LIMIT injected
  if absent; enforced during fetch regardless), total result size cap 2 MB, `truncated`
  flag, `elapsed_ms` measured. `PRAGMA query_only=ON` as further defense.

## 3. Module contracts (FROZEN signatures; all modules in app/services/)

```python
# cena_sql_schema.py                                  (owner: Subagent A)
get_schema_context() -> str          # curated, token-efficient, allowlisted tables only
get_allowlist() -> dict[str, frozenset[str]]
    # qualified table name -> allowed column names. Analytics tables unqualified
    # ("daily_sales_summary"), raw tables qualified ("appdb.orders").

# cena_sql_validator.py                               (owner: Subagent A)
validate_sql(sql: str) -> tuple[bool, str]            # (ok, reason); reason "" when ok.
    # Rejection reasons MUST be specific and actionable (the reasoner self-repairs on them).

# cena_sql_executor.py                                (owner: Subagent B)
class CenaSqlError(Exception): ...                    # .reason str, clean message
run_readonly_sql(sql: str) -> dict
    # {"rows": list[tuple], "columns": list[str], "row_count": int,
    #  "truncated": bool, "elapsed_ms": float}
refresh_snapshots() -> dict                           # per-source status
snapshot_status() -> dict                             # ages, paths, missing list

# cena_sql_analytics.py                               (owner: Subagent C)
build_analytics_db(snapshot_dir: str | None = None) -> str   # returns built db path
SCHEMA_DOC: str        # every analytics table + column documented; A embeds this
                       # in schema context (A falls back to section 4 text if import fails)

# cena_memory.py                                      (owner: Subagent D)
recall(question: str) -> dict                         # {"exemplars": [...], "insights": [...]}
record(question: str, answer: str, confidence: str, queries: list, outcome: str,
       verified: bool, **meta) -> None                # only verified material admitted
promote_exemplar(question: str, sql_plan: list[str], answer: str, verified_by: str) -> None
# storage: %CENA_L3_DATA_DIR%\memory\cena_memory.db

# cena_reasoner.py                                    (owner: Subagent D)
investigate(question: str, context: dict | None = None, *,
            llm=None, executor=None, schema_context: str | None = None,
            memory=None, max_queries: int = 6, time_budget_s: float = 60.0) -> dict
    # {"answer": str, "confidence": "high"|"medium"|"low", "confidence_reason": str,
    #  "trace": list[dict], "queries": list[dict]}
    # trace entries: {"step": int, "type": "plan"|"query"|"repair"|"observation"|
    #                 "verify"|"discard"|"flag"|"limit", ...}

# cena_sql_orchestrator.py                            (owner: Subagent F, Wave 3)
answer_question(question: str, principal: dict, context: dict | None = None) -> dict
```

Until the owning module is green, consumers stub these contracts in tests; never import
a sibling's unbuilt internals.

## 4. Analytics schema (FROZEN - A documents, C builds, D/E consume)

All tables live in cena_analytics.db (unqualified). store_key is canonical
copperfield/tomball. Dates are ISO `YYYY-MM-DD` strings, local business dates
(America/Chicago). Every table has `built_at` TEXT (ISO UTC).

1. `daily_sales_summary(store_key, business_date, net_sales, gross_sales, order_count,
   check_count, avg_check, covers, instore_net, daypart_breakfast_net, daypart_lunch_net,
   daypart_dinner_net, built_at)`
   - Source ordersdc.dm_order (catering). net_sales = SUM(caterer_total_due) [what Cenas
     receives - the NET business number]. gross_sales = SUM(ezcater_total) [customer-paid
     gross incl fees/taxes]. check_count = order_count (catering: 1 check per order).
     avg_check = net_sales/order_count. covers = SUM(headcount). instore_net = NULL
     (period-only source; documented gotcha). Dayparts by window_start local time:
     breakfast < 10:30, lunch 10:30-14:59, dinner >= 15:00 (NULL window -> lunch).
2. `daily_labor_summary(store_key, business_date, total_hours, reg_hours, ot_hours,
   labor_cost, net_sales, labor_pct, splh, employee_count, built_at)`
   - Source toast.time_entry. labor_cost = SUM(reg_hours*hourly_rate +
     1.5*ot_hours*hourly_rate). net_sales joined from daily_sales_summary (catering net;
     gotcha: labor covers whole store, sales is catering-only - labor_pct is therefore
     labor-vs-catering-net and must be described that way). splh = net_sales/total_hours.
     labor_pct = labor_cost/net_sales*100 (NULL when net_sales is 0/NULL).
3. `weekly_rollups(store_key, iso_week, week_start, net_sales, order_count, labor_cost,
   labor_pct, splh, total_hours, wow_net_sales_delta, wow_net_sales_pct,
   wow_labor_pct_delta, built_at)`  - iso_week like `2026-W23`, week starts Monday.
4. `item_sales_summary(store_key, business_date, item_name, category, qty, net_amount,
   built_at)` - from ordersdc.dm_order_item joined dm_order (line_total as net_amount).
5. `daypart_comparison(store_key, business_date, daypart, net_sales, order_count,
   built_at)` - long form of the daypart splits.
6. `same_day_lastweek` VIEW: (store_key, business_date, day_of_week, net_sales,
   prev_week_net_sales, delta, pct_change) - anchored same weekday, prior week.
7. `anomaly_flags(store_key, business_date, metric, value, baseline_mean, baseline_std,
   z_score, direction, built_at)` - metric in ('net_sales','labor_pct','avg_check');
   baseline = trailing 8 SAME-WEEKDAY values (min 4 required, std > 0 guard);
   flag when |z| > 2; direction 'high'|'low'.

## 5. Data hygiene policy (allowlist correctness decisions - A enforces)

EXCLUDED tables (never in allowlist): appdb users, permission_denial, user_audit_log,
legal_*, sam_chat_*, developer_chat*, dev_chat_*, docck_*, cena_action_logs,
cena_usage_logs, cena_wake_decisions, access_request, paycheck, processing_*,
failure_snapshots, ribbon_*, rule_overrides, interview_candidates, sample_*,
in_house_catering_quotes (quotes contain client contacts); toast perf_internal
(isolated sales lane - NEVER exposed); toastdm dm_internal_sales (same lane, raw);
ordersdc dm_ingest_ledger.

EXCLUDED columns on included tables (PII / auth / individual pay):
- appdb.orders: client, upon_delivery_ask_for, customer_phone, delivery_address,
  delivery_instructions, ezcater_driver_lat/lng, pay_notes, source_filename.
- appdb.drivers: email, phone, address, password_hash, passcode_hash, last_known_lat,
  last_known_lng, failed_attempts, lockout_until, session_version, photo_url.
- toast.time_entry: hourly_rate, tips, tips_declared (individual pay - labor cost
  appears ONLY aggregated in analytics tables).
- toast.perf_period / toastdm.dm_perf_period: base_pay, tips, tip_pct (hours/rank ok).
- toastdm.dm_time_entry: base_pay, tips, tips_declared.
- Any *_hash except customer_hash (opaque pseudonym, allowed), any email/phone/address
  column anywhere.
Driver delivery economics (driverdc.dm_delivery payout fields, appdb.orders payout
fields, driverdc.dm_pay) ARE allowed: delivery-cost economics, already management-visible.
SELECT * is rejected against any table that has excluded columns (reason must list the
allowed columns); allowed on analytics tables.

## 6. Gates and rules of engagement

- Each subagent: new files only in its stated territory; NO git commands (the
  orchestrator commits); no pushes, no deploys; no edits outside territory.
- Unit tests hermetic: synthetic tmp-dir SQLite fixtures, no network, no real DBs,
  no secrets. Read-only probes of the real DBs are allowed during DEVELOPMENT for
  semantics, and in optional `@pytest.mark.skipif`-guarded real-data smoke tests.
- Each subagent runs `python -m pytest tests/<its files> -q` from the repo root,
  iterates to green, and reports the exact pass line.
- Wave gate: orchestrator runs the full suite; green unlocks the next wave.
