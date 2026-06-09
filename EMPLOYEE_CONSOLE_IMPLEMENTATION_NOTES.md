# Cenas Floor OS - Implementation Notes (Agent 1)

Branch: `floor-os` (off `emp-foundation`)
Worktree: `Documents/Codex/2026-06-09/floor-os-work`

## What shipped

A premium, mobile-first Cenas Floor OS over the existing five employee tabs:

| Tab     | Route                   | Template                                |
|---------|-------------------------|-----------------------------------------|
| Today   | `/employee/dashboard`   | `app/templates/employee_dashboard.html` |
| Tables  | `/employee/tables`      | `app/templates/employee_tables.html`    |
| Shifts  | `/employee/my-schedule` | `app/templates/employee_schedule.html`  |
| Inbox   | `/employee/messages`    | `app/templates/employee_messages.html`  |
| You     | `/employee/my-profile`  | `app/templates/employee_my_profile.html`|

All five extend `employee_console_base.html`, share the `cc-*` foundation, and use the new `cf-*` Floor OS visual layer for richer components (hero command, coaching card, station map, ticket chit with perforated edge + timeline rail, daily ledger, toggle rows, profile, what's-new card).

## Agent ownership (delivered)

- **Agent 1 - Lead / Integration (this doc):** repo inspection, file ownership map, route wiring, final QA orchestration. Owns this file + `emp_surface_map.md`.
- **Agent 2 - UI / Visual System:** `app/static/css/employee_console.css` (existing `cc-*` foundation + new `cf-*` layer appended at the bottom) and the reusable `app/templates/_floor_macros.html` (hero_command, coaching_card, metric_card, station_chip, tip_pill, stop, segmented, welcome_row, toggle_row, empty_state, source_footer, sync_chip).
- **Agent 3 - Data / Metrics / Toast Adapter:** `app/services/employee_floor_metrics.py` (pure dataclasses + safe calculators: tip%, drink/dessert attach, tips-per-hour, seat-to-drink / seat-to-kitchen / table-turn, format_money), `app/services/floor_demo.py` (Kennya / Copperfield fixture matching the design prototype + best_next_action + table_attention + clock/ago helpers), `app/services/floor_toast_adapter.py` (allow-listed Toast -> FloorDay mapping; Toast TODOs documented inline).
- **Agent 4 - Workflows / Page Logic:** the five tab templates + route extensions in `app/web/employee_auth.py`, `employee_tables_page.py`, `employee_schedule_page.py`, `employee_messages.py`, `employee_my_profile_page.py`.
- **Agent 5 - QA / Accessibility / Performance:** `tests/test_floor_os_smoke.py` (8 cases - one render assertion per tab + day-toggle + alerts-direct + logged-out redirect for all 5 tabs).

## Business rules (preserved verbatim)

- Every route still self-guards `session["employee_id"]` and redirects logged-out callers to `/employee/login`, NOT the staff keypad.
- Manager/partner/kitchen/expo/driver dashboards were NOT touched. The git diff lists only `employee_*` files; manager surfaces are untouched by construction.
- Open checks carry no final tip; pending tips use a configurable `pending_tip_rate` (default 18%).
- Cash tips with `tip == None` stay neutral ("cash tip unknown"). They are never treated as a bad tip and never coerced to zero.
- Empty data renders an honest `cf-empty-state` card; no number is fabricated.
- Demo mode is labelled honestly: the topbar reads "demo mode" instead of "live", a `cf-demo-badge` floats over the Today hero, and the Tables tab footer says "demo mode - cash tips may be unknown".

## Run instructions

From the worktree root:

```pwsh
$env:ALLOW_DEV_SECRET = "1"
$env:DATABASE_URL = "sqlite:///dev.db"  # or your usual dev DB
python -m flask --app wsgi run
```

Then open `http://127.0.0.1:5000/employee/login` (or whatever the dev port is), log in as any active employee, and visit each tab.

## Test instructions

```pwsh
python -m pytest tests/test_employee_console_foundation.py -q
python -m pytest tests/test_employee_floor_metrics.py -q
python -m pytest tests/test_employee_messages.py -q
python -m pytest tests/test_floor_os_smoke.py -q
```

Each file in isolation passes (the existing repo has a known cross-file `SessionLocal` ordering quirk when several `app.create_app()`-using tests share one pytest process; not introduced by this change).

| File                                       | Cases | Status     |
| ------------------------------------------ | ----- | ---------- |
| `tests/test_employee_console_foundation.py`| 4     | passing    |
| `tests/test_employee_floor_metrics.py`     | 4     | passing    |
| `tests/test_employee_messages.py`          | 4     | passing    |
| `tests/test_floor_os_smoke.py` (new)       | 8     | passing    |

Total: **20 passing**.

## Honest QA gaps

- A real-browser preview was not run from this turn; the verification is server-render smoke + render-template structural assertions. Recommend a manual Chrome spot-check on Today + Tables before deploy.
- The Today range selector (today / week / month / last 30) renders the segmented control but currently feeds only the today fixture. The other ranges are deliberately empty rather than fabricated, per the "don't pretend live" rule.

## Remaining Toast API work

1. Confirm the live Toast check shape reaching `floor_toast_adapter.map_toast_checks_to_floor_day` and update the allow-list if Toast field names differ from `_CHECK_ALLOW_LIST`.
2. Replace the placeholder menu-category-to-kind table in `_KIND_BY_CATEGORY` with the canonical Cena menu mapping (probably wired through `app/services/menu_kind.py` or a sibling).
3. Derive per-check `drinks_fired_at` / `kitchen_fired_at` from per-item fire timestamps - take the earliest drink-item fire and the earliest non-drink-item fire respectively. Today the adapter passes `None` when the check-level timestamp is absent, and the chit renders the "No drinks rung" empty stop.
4. Wire `dashboard_page` / `employee_tables_page` to call the live adapter when `CenaToastLink` rows exist for the session employee, with a graceful fallback to `floor_demo` when no link or no rows.
5. The Shifts tab currently renders the empty-state path server-side; the existing `/employee/my-schedule/shifts` JSON endpoint is the live source - the next pass should fetch it client-side and populate the shift list inside `cf-pane-shifts`.
6. Performance ranges (week / month / last 30) on Today need historical aggregations from `PerfPeriodCache` + `PerfRankCache`. The hooks are reserved (`segmented(...today-range...)`) but currently render only the today values to avoid faking numbers.
