"""Schedules V2 - Block 6: the employee notification-preferences PAGE (frontend shell, ck).

GET /employee/profile renders the mobile settings view where a logged-in employee
chooses how they want to be reminded before each shift - SMS and/or email, how
long before, and an optional second reminder.

Same split as B5 (employee_schedule_page.py): this page route lives in its own
file and ATTACHES to the existing employee_auth blueprint (imported for its
decorator side effect in app/__init__.py BEFORE ezempauth.install registers the
blueprint), so employee_auth.py stays untouched (ckai's B6 backend lane) and all
/employee/* routes share one namespace (the B2 house pattern).

The page reads ONLY the Employee row, for the greeting name - ckai owns the
employee_alarm_preferences model + the GET/POST endpoints. It hands the template a
small JSON config of endpoint PATHS and the client JS fetches + saves the prefs;
the paths are plain strings so the page renders even before ckai's endpoint exists
in a given tree (same decoupling as the B4/B5 pages).

AUTH: /employee/profile is added to auth.py EXEMPT_PREFIXES so a session-less hit
reaches THIS view (not the staff keypad); the view's own employee_id guard then
302s to /employee/login - exactly like my_schedule_page / dashboard_page. A
logged-in employee passes the global gate via auth_ok anyway. Every datum here is
scoped to session['employee_id'].
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from flask import redirect, render_template, session

from app.db import SessionLocal
from app.models import Employee
from app.web.employee_auth import employee_auth


@employee_auth.route("/employee/profile", methods=["GET"])
def employee_profile_page():
    """Render the client-side notification-preferences view. Requires an employee
    session; with none we bounce to /employee/login (the employee door, not the
    staff keypad). Only the Employee name is read here - the preferences come from
    ckai's endpoint via fetch(); this view just assembles the config blob + the
    greeting name."""
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
        view = SimpleNamespace(first_name=first_name, full_name=full_name or None)
    finally:
        db.close()

    config = {
        # ckai LOCKED (#1953): GET /employee/alarm-preferences -> 200 {ok, preferences:
        # {sms_enabled:bool, email_enabled:bool, minutes_before:int, second_minutes_before:
        # int|null, is_default:bool}} (no saved row -> defaults sms-on/email-off/60/null +
        # is_default true); POST same 4 fields -> 200 {ok, preferences}. 400 invalid, 401 no
        # employee session. ckai EXEMPT-prefixes this endpoint itself so unauth = clean 401 JSON.
        "prefsUrl": "/employee/alarm-preferences",
        "dashboardUrl": "/employee/dashboard",
        "loginUrl": "/employee/login",
    }
    return render_template(
        "employee_profile.html",
        employee=view,
        config_json=json.dumps(config),
        dashboard_url="/employee/dashboard",
        login_url="/employee/login",
    )
