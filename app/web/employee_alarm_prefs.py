"""Schedules V2 - Block 6: the employee shift-alarm PREFERENCES API (ckai).

GET/POST /employee/alarm-preferences  - the backend ck's /employee/profile UI
calls to read + save an employee's reminder settings (SMS/email toggles +
minutes_before / second_minutes_before).

Like the B5 employee data endpoints (schedules_v2_employee.py), this ATTACHES to
the existing employee_auth blueprint (imported for its decorator side effect in
app/__init__.py BEFORE ezempauth.install) so all /employee/* routes share one
blueprint + URL namespace; employee_auth.py itself stays untouched.

AUTH / ISOLATION: every request is scoped to session['employee_id'] (401 JSON
with no employee session). One row per employee (UNIQUE employee_id); no row =
the system default (SMS on, email off, 60 min before). The /employee/alarm-
preferences prefix is added to auth.py EXEMPT_PREFIXES so a session-less hit
gets this clean 401 JSON instead of the staff-keypad redirect - the same
treatment as /employee/my-schedule; isolation is enforced here by the
employee_id guard, not by the site gate.
"""
from __future__ import annotations

from datetime import datetime

from flask import jsonify, request, session

from app.db import SessionLocal
from app.models import EmployeeAlarmPreference
from app.web.employee_auth import employee_auth

# System defaults when an employee has no saved row (mirrors scheduling_alarms).
_DEFAULTS = {
    "sms_enabled": True,
    "email_enabled": False,
    "minutes_before": 60,
    "second_minutes_before": None,
}
_MIN_MINUTES = 1        # 1 minute before
_MAX_MINUTES = 10080    # 1 week before


def _require_emp():
    """(employee_id, None) for a logged-in employee, else (None, (json, 401))."""
    eid = session.get("employee_id")
    if not eid:
        return None, (jsonify({"ok": False, "error": "login required"}), 401)
    return eid, None


def _serialize(pref):
    """(preferences dict, is_default bool). No row -> the defaults."""
    if pref is None:
        return dict(_DEFAULTS), True
    return ({
        "sms_enabled": bool(pref.sms_enabled),
        "email_enabled": bool(pref.email_enabled),
        "minutes_before": pref.minutes_before,
        "second_minutes_before": pref.second_minutes_before,
    }, False)


def _coerce_minutes(value, field, allow_none=False):
    """(int, None) on success or (None, errmsg). Range 1..10080. allow_none lets
    second_minutes_before be omitted/null (a single reminder)."""
    if value is None or value == "":
        if allow_none:
            return None, None
        return None, "%s is required" % field
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return None, "%s must be a whole number of minutes" % field
    if iv < _MIN_MINUTES or iv > _MAX_MINUTES:
        return None, "%s must be between %d and %d minutes" % (
            field, _MIN_MINUTES, _MAX_MINUTES)
    return iv, None


@employee_auth.route("/employee/alarm-preferences", methods=["GET"])
def emp_alarm_prefs_get():
    """Return the employee's saved reminder preferences (or the defaults)."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        pref = (db.query(EmployeeAlarmPreference)
                  .filter_by(employee_id=emp_id).first())
        prefs, is_default = _serialize(pref)
        return jsonify({"ok": True, "preferences": prefs,
                        "is_default": is_default}), 200
    finally:
        db.close()


@employee_auth.route("/employee/alarm-preferences", methods=["POST"])
def emp_alarm_prefs_post():
    """Upsert the employee's reminder preferences. Validates the minute offsets;
    booleans are coerced. Returns the saved settings."""
    emp_id, err = _require_emp()
    if err:
        return err
    data = request.get_json(silent=True) or {}

    minutes_before, e1 = _coerce_minutes(
        data.get("minutes_before", _DEFAULTS["minutes_before"]), "minutes_before")
    if e1:
        return jsonify({"ok": False, "error": e1}), 400
    second_minutes_before, e2 = _coerce_minutes(
        data.get("second_minutes_before"), "second_minutes_before", allow_none=True)
    if e2:
        return jsonify({"ok": False, "error": e2}), 400

    sms_enabled = bool(data.get("sms_enabled", _DEFAULTS["sms_enabled"]))
    email_enabled = bool(data.get("email_enabled", _DEFAULTS["email_enabled"]))

    now = datetime.utcnow()
    db = SessionLocal()
    try:
        pref = (db.query(EmployeeAlarmPreference)
                  .filter_by(employee_id=emp_id).first())
        if pref is None:
            pref = EmployeeAlarmPreference(employee_id=emp_id,
                                           created_at=now, updated_at=now)
            db.add(pref)
        pref.sms_enabled = sms_enabled
        pref.email_enabled = email_enabled
        pref.minutes_before = minutes_before
        pref.second_minutes_before = second_minutes_before
        pref.updated_at = now
        db.commit()
        prefs, _ = _serialize(pref)
        return jsonify({"ok": True, "preferences": prefs}), 200
    finally:
        db.close()
