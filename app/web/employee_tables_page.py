"""Employee-owned tables/check timeline page."""
from __future__ import annotations

import re

from flask import jsonify, redirect, render_template, request, session

from app.db import SessionLocal
from app.models import CenaToastLink, Employee
from app.services.employee_table_timelines import employee_table_timelines_payload
from app.web.employee_auth import employee_auth


@employee_auth.route("/employee/tables", methods=["GET"])
def employee_tables_page():
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect("/employee/login")
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == emp_id).first()
        if emp is None:
            return redirect("/employee/login")

        # Cenas Floor Pulse - Tables tab wired to REAL Toast table timelines. The
        # page renders identity + a config of endpoint paths; the table map +
        # ticket rail hydrate client-side from /employee/tables/data (already
        # session-scoped to the employee's confirmed Toast guid(s); no cash-tip /
        # GUID leakage). Today vs Yesterday is the existing ?day= the data
        # endpoint supports; deep-link a ticket via ?table=.
        day_key = (request.args.get("day") or "today").lower()
        if day_key not in ("today", "yesterday"):
            day_key = "today"
        # Bound the deep-link arg to a DOM-safe charset. It is only ever used as
        # a CSS.escape'd element-id lookup on the client; restricting it here
        # also closes the only request-controlled value that reaches the config
        # (defense in depth on top of the |tojson HTML-safe embed below).
        selected_table = re.sub(r"[^A-Za-z0-9_-]", "", request.args.get("table") or "")[:32] or None

        config = {
            "dataUrl": "/employee/tables/data",   # GET ?day=today|yesterday&limit=
            "loginUrl": "/employee/login",
            "dayKey": day_key,
            "selectedTable": selected_table,
        }
        return render_template(
            "employee_tables.html",
            employee=emp,
            config=config,   # embedded via |tojson (HTML-safe) in the template
            day_key=day_key,
            day_options=[("today", "Today"), ("yesterday", "Yesterday")],
            selected_table=selected_table,
            dashboard_url="/employee/dashboard",
            login_url="/employee/login",
        )
    finally:
        db.close()


@employee_auth.route("/employee/tables/data", methods=["GET"])
def employee_tables_data():
    emp_id = session.get("employee_id")
    if not emp_id:
        return jsonify({"ok": False, "error": "not signed in"}), 401

    day = request.args.get("day") or "today"
    try:
        limit = max(1, min(50, int(request.args.get("limit") or 24)))
    except (TypeError, ValueError):
        limit = 24

    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == emp_id).first()
        if emp is None:
            return jsonify({"ok": False, "error": "unknown employee"}), 404
        links = (
            db.query(CenaToastLink)
            .filter(CenaToastLink.cena_employee_id == emp.id)
            .all()
        )
        if not links:
            return jsonify({"ok": True, "linked": False, "timelines": []}), 200
        payload = employee_table_timelines_payload(
            emp.id,
            links,
            day=day,
            limit=limit,
        )
        payload["linked"] = True
        return jsonify(payload), 200
    finally:
        db.close()
