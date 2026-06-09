# Employee Surface Map

Foundation branch: `emp-foundation`

Design shell:
- Shared CSS: `app/static/css/employee_console.css`
- Base shell: `app/templates/employee_console_base.html`

The employee app is server-rendered Jinja. Each page must keep its own
`session["employee_id"]` guard and existing per-route data scoping.

| Tab | Route | Route owner | Template | Primary data source |
| --- | --- | --- | --- | --- |
| Today | `/employee/dashboard` | `app/web/employee_auth.py::dashboard_page` | `app/templates/employee_dashboard.html` | Page reads `Employee` + `EmployeeStoreAssignment` for the greeting/store label. Client fetches `/employee/my-performance` and `/employee/performance-center` from `app/web/employee_auth.py`; those use `PerfPeriodCache`, `PerfRankCache`, `PerfShiftCache`, confirmed `CenaToastLink`, and live Toast helpers in `app/services/toast_reports.py`. |
| Tables | `/employee/tables` | `app/web/employee_tables_page.py::employee_tables_page` | `app/templates/employee_tables.html` | Client fetches `/employee/tables/data`; route scopes through `session["employee_id"]` and confirmed `CenaToastLink`. Today uses live Toast via `app/services/toast_reports.py::server_table_timelines_for_guids`; history first uses `app/services/employee_table_timelines.py` against `cena_employee_<id>.sqlite`. |
| Shifts | `/employee/my-schedule` | `app/web/employee_schedule_page.py::my_schedule_page` | `app/templates/employee_schedule.html` | Page reads `Employee` for the greeting only. Client fetches `/employee/my-schedule/shifts` from `app/web/schedules_v2_employee.py`; actions post to `/employee/shifts/<id>/accept` and `/decline`. Data is scoped to the logged-in employee's published shifts. |
| Inbox | `/employee/messages` | `app/web/employee_messages.py::employee_messages_page` | `app/templates/employee_messages.html` | Client fetches `/employee/messages/directory`, `/employee/messages/conversations`, `/employee/messages/thread/<other_id>`, and posts `/employee/messages/send`; data comes from `Employee` and `Message` rows scoped by the session employee. |
| You | `/employee/my-profile` | `app/web/employee_my_profile_page.py::employee_my_profile_page` | `app/templates/employee_my_profile.html` | Page reads `Employee`, `EmployeeStoreAssignment`, `EmployeePosition`, `Position`, published schedule snippets, roster peers, and client fetches `/employee/performance-center` for role-aware performance/ranking. |

Related employee pages still available outside the 5-tab shell:
- `/employee/time-off` -> `app/templates/employee_time_off.html`, routes in `app/web/employee_time_off_page.py` + `app/web/employee_time_off.py`
- `/employee/profile` -> `app/templates/employee_profile.html`, alarm/preference surface in `app/web/employee_profile_page.py` + `app/web/employee_alarm_prefs.py`
- `/employee/shift-marketplace` -> `app/templates/employee_shift_marketplace.html`, routes in `app/web/employee_shift_marketplace_page.py` + `app/web/employee_shift_market.py`
- `/employee/performance/<metric>` -> `app/templates/employee_performance_detail.html`, route in `app/web/employee_auth.py::performance_detail`
