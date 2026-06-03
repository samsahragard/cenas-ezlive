"""Isolated READ-ONLY per-employee data-mart export endpoint (Sam #3330 / aick #3315).

Serves the CK-local employee data-mart the app-DB domains it cannot read directly:
per-employee PROFILE + SCHEDULE, field-whitelisted (default-exclude PII / credentials /
GUID / pay / manager free-text). READ-ONLY (SELECT only; zero writes).

ISOLATION (Sam #3178): imports ONLY stdlib + flask + app.db + app.models. NEVER
driver_system (the frozen catering/driver coupling). Same pattern as perf_push_routes.

AUTH (aick #3182 fail-closed): a dedicated DATAMART_EXPORT_TOKEN (separate from
CRON_TOKEN -- aick #3315 defense-in-depth, since this serves manager-adjacent data).
Unset/empty/wrong token -> 403 (never fail-open). NOT employee-facing (token-gated /cron).

SCOPE (aick #3334): only PROFILE + SCHEDULE are exported -- they are the domains keyed
by employee_id. INCIDENT / COUNSELING / manager-ATTENDANCE are NOT served (those tables
have NO employee_id FK; held pending Sam/CK decision). Perf data is CK-local (perf.sqlite).
"""
import os
from flask import Blueprint, abort, jsonify, request

from app.db import SessionLocal
from app.models import (
    Employee, EmployeePosition, Position, EmployeeStoreAssignment,
    CenaToastLink, Shift, Schedule, ShiftAcceptance,
)

datamart_export_bp = Blueprint("datamart_export", __name__)


def _extract_token():
    """Self-contained token read (no driver_system import). Precedence:
    Authorization: Bearer <t>  ->  X-Datamart-Token header  ->  ?token= query."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Datamart-Token") or request.args.get("token")


def _profile(db, emp):
    # PROFILE whitelist: id, full_name, active, positions[], stores[], toast_links[].
    # EXCLUDED (sensitive): phone/email/address (PII), passcode_hash (credential),
    # sling_id, user_id, session_version, toast_id (Toast GUID).
    pos_rows = (
        db.query(EmployeePosition.store_key, Position.name)
        .outerjoin(Position, EmployeePosition.position_id == Position.id)
        .filter(EmployeePosition.employee_id == emp.id)
        .all()
    )
    positions = [{"position": name, "store_key": sk} for sk, name in pos_rows]
    stores = [
        sk for (sk,) in
        db.query(EmployeeStoreAssignment.store_key)
        .filter(EmployeeStoreAssignment.employee_id == emp.id).all()
    ]
    toast_links = [
        {"store_key": sk, "toast_name": tn}
        for sk, tn in db.query(CenaToastLink.store_key, CenaToastLink.toast_name)
        .filter(CenaToastLink.cena_employee_id == emp.id).all()
    ]
    return {
        "employee_id": emp.id,
        "full_name": emp.full_name,
        "active": bool(emp.active),
        "positions": positions,
        "stores": stores,
        "toast_links": toast_links,  # store_key + toast_name only -- NO toast_id GUID
    }


def _schedule(db, eid):
    # SCHEDULE whitelist: own PUBLISHED shifts (date/time/position/status) + acceptance
    # responses. EXCLUDED: Shift.notes + Shift.display_name (free-text), Acceptance.reason.
    shifts = []
    # Select ONLY the whitelisted columns (not the whole Shift entity) so free-text
    # Shift.notes / Shift.display_name are never even loaded -- removes the footgun
    # DM-AUDIT-DATA flagged (a future naive spread of the entity could otherwise leak).
    rows = (
        db.query(Shift.id, Shift.start_at, Shift.end_at, Shift.status,
                 Schedule.store_key, Position.name)
        .join(Schedule, Shift.schedule_id == Schedule.id)
        .outerjoin(Position, Shift.position_id == Position.id)
        .filter(Shift.employee_id == eid, Schedule.status == "published")
        .order_by(Shift.start_at.asc())
        .all()
    )
    for shift_id, start_at, end_at, status, store_key, pos_name in rows:
        shifts.append({
            "shift_id": shift_id,
            "store_key": store_key,
            "position": pos_name,
            "start_at": start_at.isoformat() if start_at else None,
            "end_at": end_at.isoformat() if end_at else None,
            "status": status,
        })
    acceptances = [
        {"shift_id": shift_id, "response": response}
        for shift_id, response in db.query(ShiftAcceptance.shift_id, ShiftAcceptance.response)
        .filter(ShiftAcceptance.employee_id == eid).all()
    ]
    return {"published_shifts": shifts, "acceptances": acceptances}


@datamart_export_bp.route("/cron/employee-datamart-export", methods=["GET"])
def employee_datamart_export():
    # FAIL-CLOSED (aick #3182): read expected ONCE; unset/empty token MUST 403 (not
    # fail-open). Dedicated DATAMART_EXPORT_TOKEN (separate from CRON_TOKEN).
    expected = os.getenv("DATAMART_EXPORT_TOKEN")
    if not expected or _extract_token() != expected:
        abort(403)

    emp_filter = request.args.get("employee_id")
    db = SessionLocal()
    try:
        q = db.query(Employee).filter(Employee.active.is_(True))
        if emp_filter:
            try:
                q = q.filter(Employee.id == int(emp_filter))
            except (TypeError, ValueError):
                abort(400)
        employees = q.order_by(Employee.id.asc()).all()
        out = [
            {
                "employee_id": emp.id,
                "profile": _profile(db, emp),
                "schedule": _schedule(db, emp.id),
            }
            for emp in employees
        ]
        return jsonify({"ok": True, "count": len(out), "domains": ["profile", "schedule"], "employees": out})
    finally:
        db.close()
