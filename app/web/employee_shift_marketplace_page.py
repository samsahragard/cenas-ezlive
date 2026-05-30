"""Schedules V2 - Block 9: the employee shift-marketplace PAGE (frontend shell, ck).

GET /employee/shift-marketplace renders the mobile marketplace where a logged-in
employee takes an open shift, offers up one of their own, proposes a swap, and
manages their offers/swaps.

Same split as B5-B8: this page route attaches to the employee_auth blueprint
(imported in app/__init__.py before ezempauth.install) so employee_auth.py stays
ckai's lane. The page reads ONLY the Employee name; ckai owns the offers/swaps
models + the data/action endpoints. Config hands the client the endpoint PATHS,
LOCKED #1996/#1998 (B7-style): the PAGE owns the parent /employee/shift-marketplace,
the DATA list is at /employee/shift-marketplace/list.

AUTH: /employee/shift-marketplace is in auth.py EXEMPT_PREFIXES so a session-less
hit reaches THIS view (not the staff keypad); the view self-guards on
session['employee_id'] and 302s to /employee/login. (ckai EXEMPTs the sibling
/employee/shift-offers + /employee/shift-swaps data prefixes separately, each with
its own employee_id guard.) Every datum here is scoped to session['employee_id'].
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from flask import redirect, render_template, session

from app.db import SessionLocal
from app.models import Employee
from app.web.employee_auth import employee_auth


@employee_auth.route("/employee/shift-marketplace", methods=["GET"])
def employee_shift_marketplace_page():
    """Render the client-side marketplace. Requires an employee session; with none
    we bounce to /employee/login (the employee door, not the keypad). Only the
    Employee name is read here - offers/swaps/candidates come from ckai's endpoints
    via fetch(); the offer/swap pickers reuse the B5 my-schedule shifts feed."""
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect("/employee/login")

    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == emp_id).first()
        if emp is None:
            for k in ("employee_id", "employee_session_version", "auth_ok"):
                session.pop(k, None)
            return redirect("/employee/login")
        full_name = (emp.full_name or "").strip()
        first_name = full_name.split(" ")[0] if full_name else None
        view = SimpleNamespace(first_name=first_name, full_name=full_name or None)
    finally:
        db.close()

    config = {
        "openListUrl": "/employee/shift-marketplace/list",   # ckai: GET -> {ok, offers:[offer]} (take-able)
        "myOffersUrl": "/employee/shift-offers/list",         # ckai: GET -> {ok, offers:[offer]} (mine)
        "offerBase": "/employee/shift-offers",                # ckai: POST {shift_id,expires_at?,unrestricted?}; POST <base>/<id>/take|cancel
        "swapsUrl": "/employee/shift-swaps/list",             # ckai: GET -> {ok, swaps:[swap]}
        "swapBase": "/employee/shift-swaps",                  # ckai: POST <base>/propose {from_shift_id,to_shift_id}; POST <base>/<id>/accept|cancel
        "candidatesUrl": "/employee/shift-swaps/candidates",  # ckai: GET ?from_shift_id=<mine> -> {ok, candidates:[...]}
        "myShiftsUrl": "/employee/my-schedule/shifts",        # B5 (aick): GET -> {ok, shifts:[...]} for the offer/swap-from picker
        "dashboardUrl": "/employee/dashboard",
        "loginUrl": "/employee/login",
    }
    return render_template(
        "employee_shift_marketplace.html",
        employee=view,
        config_json=json.dumps(config),
        dashboard_url="/employee/dashboard",
        login_url="/employee/login",
    )
