# Employee Console Implementation Notes

Branch: `emp-foundation`

## Repo Findings

- Framework: Flask 2.x with gunicorn. Pages are server-rendered Jinja templates.
- Frontend structure: employee pages are standalone full HTML templates under `app/templates/employee_*.html`; most include `app/templates/partials/_employee_nav.html`.
- Styling: current employee app uses inline page CSS and `app/static/css/employee_theme.css`. The new ink-navy console foundation is `app/static/css/employee_console.css`.
- Auth/role structure: employee pages self-guard with `session["employee_id"]` and redirect to `/employee/login`; employee route prefixes are exempted from the global staff keypad gate in `app/web/auth.py`. Preserve these guards.
- Employee routes:
  - Today: `GET /employee/dashboard` in `app/web/employee_auth.py`
  - Tables: `GET /employee/tables` in `app/web/employee_tables_page.py`
  - Shifts: `GET /employee/my-schedule` in `app/web/employee_schedule_page.py`
  - Inbox: `GET /employee/messages` in `app/web/employee_messages.py`
  - You: `GET /employee/my-profile` in `app/web/employee_my_profile_page.py`
- Toast/live data:
  - Confirmed employee Toast links use `CenaToastLink`.
  - Today performance uses `PerfPeriodCache`, `PerfShiftCache`, `PerfRankCache`, plus live Toast helpers in `app/services/toast_reports.py`.
  - Tables use `app/services/employee_table_timelines.py` and live Toast helper `server_table_timelines_for_guids`.
- Test runner: focused Python tests use `python -m pytest -p no:cacheprovider <test files> -q`.
- Package manager: no React/Vite/Next package file is present for this surface; do not introduce React.

## Agent Ownership

- Agent 1 Lead/Integration:
  - Owns `EMPLOYEE_CONSOLE_IMPLEMENTATION_NOTES.md`, `emp_surface_map.md`, final review, and integration.
- Agent 2 UI/Visual:
  - Owns `app/static/css/employee_console.css` and `app/templates/partials/_employee_nav.html`.
- Agent 3 Data/Metrics:
  - Owns `app/services/employee_floor_metrics.py` and `tests/test_employee_floor_metrics.py`.
- Agent 4 Product/Templates:
  - Owns the five tab templates:
    - `app/templates/employee_dashboard.html`
    - `app/templates/employee_tables.html`
    - `app/templates/employee_schedule.html`
    - `app/templates/employee_messages.html`
    - `app/templates/employee_my_profile.html`
- Agent 5 QA:
  - Owns `tests/test_employee_console_foundation.py` and QA findings.

## Implementation Approach

- Preserve existing routes and data endpoints rather than adding a new React app.
- Use the new `employee_console.css` tokens/classes as the shared visual system.
- Keep each employee tab scoped to the logged-in employee through the existing session guards.
- Keep live data wording honest: use "synced" or "live" only where the existing endpoint is actually live; use neutral copy for cached/history data.
- Keep cash tips neutral when the tip is unknown; do not count unknown cash tips as bad performance.
- Avoid exposing manager analytics or cross-employee/store data.

## Integration Checklist

- `employee_console.css` exposes the shared component classes.
- `_employee_nav.html` has exactly five tabs: Today, Tables, Shifts, Inbox, You.
- All five tab templates either use or are compatible with the shared console classes.
- Data utilities are pure and covered by focused tests.
- Foundation tests confirm route/template map and shell/nav structure.
- Focused employee tests pass.
- No push, deploy, migration, or unrelated dashboard edits in this branch.
