"""Employee-owned tables/check timeline page."""
from __future__ import annotations

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

        # Cenas Floor Pulse V2: the Tables tab is date-true. "Today" has no
        # posted checks (date reset) and shows an empty state with a jump to
        # Yesterday's completed ticket rail. Table tiles deep-link to their
        # ticket via ?table=; filters via ?filter=. The live Toast path lives in
        # /employee/tables/data above and remains available untouched.
        from app.services import floor_pulse as fp

        day_key = (request.args.get("day") or "today").lower()
        if day_key not in ("today", "yesterday"):
            day_key = "today"

        flt = (request.args.get("filter") or "all").lower()
        if flt not in ("all", "mine", "open", "attention", "new"):
            flt = "all"

        selected_table = request.args.get("table") or None

        all_tickets = fp.tickets_for_day(day_key)
        counts = fp.filter_counts(all_tickets)
        # A ?table= deep-link must show that ticket even if a stale filter would
        # hide it -- fall back to "all" so the selection is never orphaned.
        if selected_table and flt != "all":
            visible_ids = {t["table_id"] for t in fp.filter_tickets(all_tickets, flt)}
            if selected_table not in visible_ids:
                flt = "all"
        tickets = [fp.ticket_view(t) for t in fp.filter_tickets(all_tickets, flt)]

        return render_template(
            "employee_tables.html",
            employee=emp,
            tickets=tickets,
            counts=counts,
            day_key=day_key,
            day_options=[("today", "Today"), ("yesterday", "Yesterday")],
            filter_key=flt,
            selected_table=selected_table,
            demo_mode=True,
            sync_label="demo mode",
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
