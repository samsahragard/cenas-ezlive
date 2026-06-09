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

        # Cenas Floor OS: demo fixture today; the live Toast path lives in
        # /employee/tables/data above and remains available for callers that
        # already know how to consume it. The Floor OS shell renders the
        # demo when the live links are not present.
        from app.services import floor_demo
        from app.services.employee_floor_metrics import calculate_day_stats

        day_key = (request.args.get("day") or "today").lower()
        if day_key not in ("today", "yesterday"):
            day_key = "today"
        floor_day = floor_demo.demo_today() if day_key == "today" else floor_demo.demo_yesterday()

        hours = floor_day.get("hours_worked")
        base_pay_amount = (hours or 0) * 2.13
        stats = calculate_day_stats(
            floor_day,
            pending_tip_rate=floor_demo.PENDING_TIP_RATE,
            hours=hours,
            base_pay=base_pay_amount,
        )
        target = floor_day.get("target_tips") or 0
        opportunity = max(0.0, target - (stats.get("recorded_tips") or 0))
        coaching = floor_demo.best_next_action(floor_day, stats)
        station_chips = [
            {
                "table": t,
                "attention": floor_demo.table_attention(t),
                "summary": floor_demo.table_summary(t)["label"],
            }
            for t in floor_day["tables"]
        ]

        return render_template(
            "employee_tables.html",
            employee=emp,
            floor_day=floor_day,
            stats=stats,
            opportunity=opportunity,
            coaching=coaching,
            station_chips=station_chips,
            day_key=day_key,
            day_options=[("today", "Today"), ("yesterday", "Yesterday")],
            now_minutes=floor_demo.DEMO_NOW_MINUTES,
            clock=floor_demo.clock,
            ago=floor_demo.ago,
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
