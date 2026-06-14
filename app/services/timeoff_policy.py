"""Per-store time-off policy (Sam 2026-06-13).

A manager sets, in Operations -> Team -> Settings, whether time-off requests
need approval and how many days in advance they must be submitted. This module
reads/writes TimeOffPolicy and resolves the EFFECTIVE policy for an employee
(who may be assigned to more than one store -- we take the most restrictive:
approval required if ANY store requires it; the largest enabled cutoff).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.models import EmployeeStoreAssignment, TimeOffPolicy

DEFAULTS = {"require_approval": True, "cutoff_enabled": False, "cutoff_days": 14}


def _row_to_dict(row) -> dict:
    return {
        "require_approval": bool(row.require_approval),
        "cutoff_enabled": bool(row.cutoff_enabled),
        "cutoff_days": int(row.cutoff_days or 0),
    }


def get_policy(db, store_key: str) -> dict:
    """The store's policy, or DEFAULTS if none saved yet."""
    row = db.query(TimeOffPolicy).filter_by(store_key=store_key).first()
    return _row_to_dict(row) if row else dict(DEFAULTS)


def set_policy(db, store_key: str, *, require_approval: bool,
               cutoff_enabled: bool, cutoff_days: int) -> dict:
    """Upsert the store's policy. cutoff_days is clamped to 0..365."""
    cutoff_days = max(0, min(365, int(cutoff_days)))
    row = db.query(TimeOffPolicy).filter_by(store_key=store_key).first()
    now = datetime.utcnow()
    if row is None:
        row = TimeOffPolicy(store_key=store_key, updated_at=now)
        db.add(row)
    row.require_approval = bool(require_approval)
    row.cutoff_enabled = bool(cutoff_enabled)
    row.cutoff_days = cutoff_days
    row.updated_at = now
    db.commit()
    return _row_to_dict(row)


def effective_for_employee(db, emp_id: int) -> dict:
    """Resolve the policy an employee is held to across ALL their assigned
    stores -- the MOST RESTRICTIVE: approval required if any store requires it;
    cutoff = the largest enabled cutoff_days (cutoff_enabled if any store has
    one). No assignments / no policies -> DEFAULTS."""
    stores = [a.store_key for a in
              db.query(EmployeeStoreAssignment.store_key)
                .filter(EmployeeStoreAssignment.employee_id == emp_id).all()]
    if not stores:
        return dict(DEFAULTS)
    require_approval = False
    cutoff_enabled = False
    cutoff_days = 0
    for sk in stores:
        p = get_policy(db, sk)
        require_approval = require_approval or p["require_approval"]
        if p["cutoff_enabled"]:
            cutoff_enabled = True
            cutoff_days = max(cutoff_days, p["cutoff_days"])
    # If no store had a row at all, get_policy returned DEFAULTS (require_approval
    # True), so require_approval already reflects the safe default.
    return {"require_approval": require_approval,
            "cutoff_enabled": cutoff_enabled,
            "cutoff_days": cutoff_days if cutoff_enabled else DEFAULTS["cutoff_days"]}


try:  # store-local calendar day (both stores = Houston) for the cutoff base
    from zoneinfo import ZoneInfo
    _STORE_TZ = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover - tzdata missing
    _STORE_TZ = None


def _local_today() -> date:
    if _STORE_TZ is not None:
        return datetime.now(_STORE_TZ).date()
    return (datetime.utcnow() - timedelta(hours=5)).date()  # CDT fallback


def earliest_allowed_start(policy: dict, today: date | None = None) -> date | None:
    """The first date an employee may request off under `policy`: today +
    cutoff_days when the cutoff is on, else None (no restriction). `today`
    defaults to STORE-LOCAL (Houston) day, not UTC, so the window doesn't shift
    a day in the evening."""
    if not policy.get("cutoff_enabled"):
        return None
    base = today or _local_today()
    return base + timedelta(days=int(policy.get("cutoff_days") or 0))
