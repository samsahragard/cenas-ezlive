"""Schedules V2 - Block 5: the employee schedule-view PAGE (frontend shell, ck).

GET /employee/my-schedule renders the mobile schedule view where a logged-in
employee sees their own PUBLISHED shifts (this week + next) and accepts or
declines each one.

The split mirrors B4 (schedules_v2_pages.py): this page route lives in its own
file so it never collides with aick's B5 data/action endpoints. It ATTACHES to
the existing employee_auth blueprint - imported for its decorator side effect in
app/__init__.py BEFORE ezempauth.install(app) registers that blueprint - so all
/employee/* routes stay on one blueprint and one URL namespace (the B2 house
pattern), while employee_auth.py itself stays untouched (aick's B5 lane).

The page reads ONLY the Employee row, for the greeting name - aick owns the
shifts + acceptance models and endpoints. It hands the template a small JSON
config of endpoint PATHS and the client JS fetches everything; the paths are
plain strings so the page renders even before aick's endpoints exist in a given
tree (same decoupling as the B4 manager page).

AUTH: /employee/my-schedule is added to auth.py EXEMPT_PREFIXES so a session-less
hit reaches THIS view (not the staff keypad); the view's own employee_id guard
then 302s to /employee/login - exactly like dashboard_page. A logged-in employee
passes the global gate via auth_ok anyway. The exemption matches the prefix of
aick's data endpoint (/employee/my-schedule/shifts) too, but that only changes
the UNAUTHENTICATED response there from the shared-password keypad redirect to
aick's own JSON 401 - employee isolation is enforced by each endpoint's
session['employee_id'] guard, not by the site gate, so the data stays protected.
Every datum here is scoped to session['employee_id'].
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from flask import redirect, render_template, request, session

from app.db import SessionLocal
from app.models import Employee
from app.web.employee_auth import employee_auth


@employee_auth.route("/employee/my-schedule", methods=["GET"])
def my_schedule_page():
    """Render the client-side employee schedule view. Requires an employee
    session; with none we bounce to /employee/login (the employee door, not the
    staff keypad). Only the Employee name is read here - the shifts come from
    aick's data endpoint via fetch(); this view just assembles the config blob
    + the greeting name."""
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect("/employee/login")

    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == emp_id).first()
        if emp is None:
            # Stale/cleared employee - drop the session keys + bounce to login.
            for k in ("employee_id", "employee_session_version", "auth_ok"):
                session.pop(k, None)
            return redirect("/employee/login")
        full_name = (emp.full_name or "").strip()
        first_name = full_name.split(" ")[0] if full_name else None
        # Plain-string namespace (no detached ORM rows used after close).
        view = SimpleNamespace(first_name=first_name, full_name=full_name or None)
    finally:
        db.close()

    config = {
        "dataUrl": "/employee/my-schedule/shifts",   # aick: GET -> {ok, employee, shifts:[...]}
        "shiftActionBase": "/employee/shifts",        # aick: POST <base>/<id>/accept | <base>/<id>/decline
        "dashboardUrl": "/employee/dashboard",
        "loginUrl": "/employee/login",
    }

    # Cenas Floor Pulse: the Shifts tab reads its own shift list. The
    # /employee/my-schedule/shifts JSON endpoint is the live source -- but for
    # the initial server-render we ship the empty-state path so the tab works
    # before the JSON client is wired. active_section drives the segmented
    # control; the time-off form posts to its backend when that lands.
    section = (request.args.get("view") or "shifts").lower()
    if section not in ("shifts", "timeoff"):
        section = "shifts"
    return render_template(
        "employee_schedule.html",
        employee=view,
        shifts=[],            # populated by /employee/my-schedule/shifts client-side
        active_section=section,
        config_json=json.dumps(config),
        dashboard_url="/employee/dashboard",
        login_url="/employee/login",
    )
