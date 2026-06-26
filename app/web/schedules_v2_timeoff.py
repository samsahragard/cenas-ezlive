"""Schedules V2 - Block 7: the MANAGER time-off review endpoints (ckai).

  GET  /<store>/schedules-v2/time-off/list [?status=...]  -> this store's requests (JSON; the
       PAGE at the bare /<store>/schedules-v2/time-off is ck's HTML - /list avoids the collision)
  POST /<store>/schedules-v2/time-off/<id>/approve         {manager_notes?}
  POST /<store>/schedules-v2/time-off/<id>/deny            {manager_notes?}

These ride the EXISTING store_bp blueprint (/<store_slug>/ prefix) exactly like
the B4 manager endpoints (schedules_v2.py), so they inherit store_bp's gates:
_pull_store (404 on bad slug) + _per_store_gate (a Tomball gm hitting /uno/... is
403/redirected BEFORE the view, zero rows touched) + the partner second factor.
On top, @require_level('foh_manager') gates to managers (expo/driver 403,
employees redirected to login).

STORE SCOPING: time-off is per-EMPLOYEE (an employee is off regardless of store),
but the manager view + approve/deny are scoped to the employees ASSIGNED to this
store - the request's employee must have an employee_store_assignments row for
g.current_location (the location, matching _store() in schedules_v2.py), else the
request is invisible here (404 on approve/deny - never reveal another store's
rows). Only an APPROVED request blocks shift-create (scheduling_timeoff.conflict).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from flask import g, jsonify, render_template_string, request

from app.db import SessionLocal
from app.models import Employee, EmployeeStoreAssignment, TimeOffRequest
from app.web.permissions import current_user_id, require_level
from app.web.store_routes import store_bp

_MGR = "foh_manager"  # mirror schedules_v2.py's manager gate


def _store() -> str | None:
    """LOCATION ('tomball'/'copperfield') - joins employee_store_assignments.store_key
    (B2 contract). Mirror of schedules_v2.py._store()."""
    return getattr(g, "current_location", None)


def _serialize(r, employee_name=None):
    return {
        "id": r.id,
        "employee_id": r.employee_id,
        "employee_name": employee_name,
        "start_date": r.start_date.isoformat() if r.start_date else None,
        "end_date": r.end_date.isoformat() if r.end_date else None,
        "reason": r.reason,
        "status": r.status,
        "manager_notes": r.manager_notes,
        "reviewed_by": r.reviewed_by,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _request_in_store(db, req_id):
    """The TimeOffRequest IFF its employee is assigned to the current store, else
    None (so a cross-store id is a 404, never revealed/mutated)."""
    return (db.query(TimeOffRequest)
              .join(EmployeeStoreAssignment,
                    EmployeeStoreAssignment.employee_id == TimeOffRequest.employee_id)
              .filter(TimeOffRequest.id == req_id,
                      EmployeeStoreAssignment.store_key == _store())
              .first())


@store_bp.route("/schedules-v2/time-off/list", methods=["GET"])
@require_level(_MGR)
def sv2_time_off_list():
    """This store's time-off requests (employees assigned here). ?status filters."""
    status = (request.args.get("status") or "").strip()
    db = SessionLocal()
    try:
        q = (db.query(TimeOffRequest, Employee.full_name)
               .join(EmployeeStoreAssignment,
                     EmployeeStoreAssignment.employee_id == TimeOffRequest.employee_id)
               .join(Employee, Employee.id == TimeOffRequest.employee_id)
               .filter(EmployeeStoreAssignment.store_key == _store()))
        if status:
            q = q.filter(TimeOffRequest.status == status)
        rows = q.order_by(TimeOffRequest.start_date.asc(), TimeOffRequest.id.asc()).all()
        return jsonify({"ok": True,
                        "requests": [_serialize(r, name) for (r, name) in rows]}), 200
    finally:
        db.close()


def _review(req_id, new_status, allowed_from):
    """Shared approve/deny body. allowed_from = statuses this transition is valid
    from. Same status -> 200 no-op (idempotent); a terminal/foreign state -> 409;
    not-in-this-store -> 404."""
    data = request.get_json(silent=True) or {}
    notes = (data.get("manager_notes") or "").strip() or None
    db = SessionLocal()
    try:
        r = _request_in_store(db, req_id)
        if r is None:
            return jsonify({"ok": False, "error": "request not found in this store"}), 404
        if r.status == new_status:
            return jsonify({"ok": True, "request": _serialize(r)}), 200  # idempotent
        if r.status not in allowed_from:
            return jsonify({"ok": False,
                            "error": "cannot %s a %s request" % (new_status, r.status)}), 409
        r.status = new_status
        if notes is not None:
            r.manager_notes = notes
        r.reviewed_by = current_user_id()
        r.reviewed_at = datetime.utcnow()
        r.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "request": _serialize(r)}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/time-off/<int:req_id>/approve", methods=["POST"])
@require_level(_MGR)
def sv2_time_off_approve(req_id):
    """Approve a pending (or previously-denied) request. An approved request then
    blocks conflicting shift-create. Cancelled -> 409."""
    return _review(req_id, "approved", allowed_from=("pending", "denied"))


@store_bp.route("/schedules-v2/time-off/<int:req_id>/deny", methods=["POST"])
@require_level(_MGR)
def sv2_time_off_deny(req_id):
    """Deny a pending (or reverse a previously-approved) request. Cancelled -> 409."""
    return _review(req_id, "denied", allowed_from=("pending", "approved"))


_SLING_IMPORT_FORM = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Sling time-off import</title>
    <style>
      body { font-family: system-ui, sans-serif; margin: 24px; background: #1c1009; color: #fff8ea; }
      textarea { width: 100%; min-height: 280px; background: #100804; color: #fff8ea; border: 1px solid #7b4a16; border-radius: 6px; padding: 10px; }
      button { margin-top: 12px; padding: 10px 14px; border-radius: 6px; border: 1px solid #d8a629; background: #d8a629; color: #160c06; font-weight: 800; }
      label { display: block; margin-top: 12px; }
      input[type=text] { background: #100804; color: #fff8ea; border: 1px solid #7b4a16; border-radius: 6px; padding: 8px; }
      pre { white-space: pre-wrap; background: #100804; border: 1px solid #7b4a16; border-radius: 6px; padding: 12px; }
    </style>
  </head>
  <body>
    <h1>Sling time-off import - {{ store }}</h1>
    <form method="post">
      <textarea name="requests_json" placeholder='{"requests":[{"name":"Employee","start_date":"2026-07-01","end_date":"2026-07-02","status":"pending","reason":"Vacation"}]}'></textarea>
      <label><input type="checkbox" name="commit" value="1"> Commit import</label>
      <label>Type IMPORT to commit <input type="text" name="confirm" autocomplete="off"></label>
      <button type="submit">Run import</button>
    </form>
    {% if result %}<h2>Result</h2><pre>{{ result }}</pre>{% endif %}
  </body>
</html>
"""


def _norm_import_name(value):
    text = re.sub(r"[^a-z0-9]+", " ", (value or "").casefold())
    return re.sub(r"\s+", " ", text).strip()


def _parse_import_date(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _load_import_rows(body):
    if isinstance(body, list):
        rows = body
    elif isinstance(body, dict):
        rows = body.get("requests")
    else:
        rows = None
    return rows if isinstance(rows, list) else []


def _sling_timeoff_import_summary(db, rows, commit=False):
    now = datetime.utcnow()
    employees = (db.query(Employee)
                   .join(EmployeeStoreAssignment,
                         EmployeeStoreAssignment.employee_id == Employee.id)
                   .filter(EmployeeStoreAssignment.store_key == _store())
                   .order_by(Employee.full_name.asc(), Employee.id.asc())
                   .all())
    by_name = {}
    for emp in employees:
        key = _norm_import_name(emp.full_name)
        if key:
            by_name.setdefault(key, []).append(emp)

    summary = {
        "ok": True,
        "store": _store(),
        "commit": bool(commit),
        "received": len(rows),
        "created": 0,
        "duplicates": [],
        "overlaps": [],
        "unmatched": [],
        "ambiguous": [],
        "invalid": [],
        "created_requests": [],
    }
    allowed_statuses = {"pending", "approved", "denied", "cancelled"}

    for idx, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            summary["invalid"].append({"row": idx, "error": "row is not an object"})
            continue
        name = (raw.get("name") or raw.get("employee_name") or "").strip()
        start = _parse_import_date(raw.get("start_date"))
        end = _parse_import_date(raw.get("end_date") or raw.get("start_date"))
        status = (raw.get("status") or "pending").strip().lower()
        reason = (raw.get("reason") or "").strip() or None
        if not name or start is None or end is None or end < start:
            summary["invalid"].append({
                "row": idx, "name": name, "start_date": raw.get("start_date"),
                "end_date": raw.get("end_date"), "error": "missing or invalid name/date range",
            })
            continue
        if status not in allowed_statuses:
            summary["invalid"].append({
                "row": idx, "name": name, "status": raw.get("status"),
                "error": "invalid status",
            })
            continue

        matches = by_name.get(_norm_import_name(name), [])
        if not matches:
            summary["unmatched"].append({
                "row": idx, "name": name, "start_date": start.isoformat(),
                "end_date": end.isoformat(), "reason": reason,
            })
            continue
        if len(matches) > 1:
            summary["ambiguous"].append({
                "row": idx, "name": name,
                "candidates": [{"id": emp.id, "full_name": emp.full_name} for emp in matches],
            })
            continue
        emp = matches[0]

        exact = (db.query(TimeOffRequest)
                   .filter(TimeOffRequest.employee_id == emp.id,
                           TimeOffRequest.start_date == start,
                           TimeOffRequest.end_date == end,
                           TimeOffRequest.status == status,
                           TimeOffRequest.reason == reason)
                   .first())
        if exact is not None:
            summary["duplicates"].append({
                "row": idx, "id": exact.id, "employee_id": emp.id,
                "employee_name": emp.full_name, "start_date": start.isoformat(),
                "end_date": end.isoformat(), "status": status,
            })
            continue

        overlap = (db.query(TimeOffRequest)
                     .filter(TimeOffRequest.employee_id == emp.id,
                             TimeOffRequest.status.in_(("pending", "approved")),
                             TimeOffRequest.start_date <= end,
                             TimeOffRequest.end_date >= start)
                     .first())
        if overlap is not None:
            summary["overlaps"].append({
                "row": idx, "employee_id": emp.id, "employee_name": emp.full_name,
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "existing_id": overlap.id,
                "existing_start_date": overlap.start_date.isoformat(),
                "existing_end_date": overlap.end_date.isoformat(),
                "existing_status": overlap.status,
            })
            continue

        item = {
            "row": idx, "employee_id": emp.id, "employee_name": emp.full_name,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "status": status, "reason": reason,
        }
        if commit:
            req = TimeOffRequest(employee_id=emp.id, start_date=start, end_date=end,
                                 reason=reason, status=status, created_at=now,
                                 updated_at=now)
            db.add(req)
            db.flush()
            item["id"] = req.id
            summary["created"] += 1
        summary["created_requests"].append(item)

    if commit:
        db.commit()
    return summary


@store_bp.route("/schedules-v2/time-off/import-sling", methods=["GET", "POST"])
@require_level("partner")
def sv2_time_off_import_sling():
    """Hidden partner utility for Sling time-off exports.

    Creates real TimeOffRequest rows, so imported requests appear in:
      - employee self-service time-off history,
      - manager time-off review/list flows,
      - the week-builder request markers.
    """
    if request.method == "GET":
        return render_template_string(_SLING_IMPORT_FORM, store=_store(), result=None)

    if request.is_json:
        body = request.get_json(silent=True) or {}
        commit = bool(body.get("commit")) if isinstance(body, dict) else False
    else:
        raw = (request.form.get("requests_json") or "").strip()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            result = {"ok": False, "error": f"invalid JSON: {exc}"}
            return render_template_string(_SLING_IMPORT_FORM, store=_store(),
                                          result=json.dumps(result, indent=2)), 400
        commit = request.form.get("commit") == "1" and request.form.get("confirm") == "IMPORT"

    rows = _load_import_rows(body)
    db = SessionLocal()
    try:
        summary = _sling_timeoff_import_summary(db, rows, commit=commit)
    finally:
        db.close()

    if request.is_json or "application/json" in (request.headers.get("Accept") or ""):
        return jsonify(summary), 200
    return render_template_string(_SLING_IMPORT_FORM, store=_store(),
                                  result=json.dumps(summary, indent=2))


# ---------------------------------------------------------------------------
# Time-off POLICY (Sam 2026-06-13): the Operations -> Team -> Settings tab.
# Approval-required + an N-days-in-advance cutoff, set per store.
# ---------------------------------------------------------------------------
@store_bp.route("/schedules-v2/time-off/policy", methods=["GET"])
@require_level(_MGR)
def sv2_time_off_policy_get():
    """This store's time-off policy (defaults if never set)."""
    from app.services import timeoff_policy
    db = SessionLocal()
    try:
        return jsonify({"ok": True, "policy": timeoff_policy.get_policy(db, _store())}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/time-off/policy", methods=["POST"])
@require_level(_MGR)
def sv2_time_off_policy_set():
    """Save this store's time-off policy:
    {require_approval: bool, cutoff_enabled: bool, cutoff_days: int}."""
    from app.services import timeoff_policy
    data = request.get_json(silent=True) or {}
    try:
        cutoff_days = int(data.get("cutoff_days", 14))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "cutoff_days must be a whole number"}), 400
    if cutoff_days < 0 or cutoff_days > 365:
        return jsonify({"ok": False, "error": "cutoff_days must be 0-365"}), 400
    db = SessionLocal()
    try:
        pol = timeoff_policy.set_policy(
            db, _store(),
            require_approval=bool(data.get("require_approval", True)),
            cutoff_enabled=bool(data.get("cutoff_enabled", False)),
            cutoff_days=cutoff_days)
        return jsonify({"ok": True, "policy": pol}), 200
    finally:
        db.close()
