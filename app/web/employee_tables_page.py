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
        return render_template(
            "employee_tables.html",
            employee=emp,
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
