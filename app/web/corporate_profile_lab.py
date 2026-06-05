"""Corporate read-only profile lab.

This is an explicit owner/corporate testing surface for inspecting employee
and driver profile state without impersonating them. It never sets employee or
driver session keys, never calls driver workflow routes, and never exposes
passcodes, hashes, Toast ids, or employee contact fields.
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from app.db import SessionLocal
from app.models import (
    CenaToastLink,
    DeliveryRequest,
    Driver,
    DriverLog,
    DriverScore,
    DriverShift,
    Employee,
    EmployeePosition,
    EmployeeStoreAssignment,
    Order,
    PayCheck,
    PerfPeriodCache,
    PerfRankCache,
    Position,
    UserAuditLog,
    sanitize_rank_json,
)
from app.web.employee_auth import _STORE_LABELS
from app.web.employee_my_profile_page import _profile_roster, _profile_schedule
from app.web.permissions import level_at_least, require_level


profile_lab_bp = Blueprint("corporate_profile_lab", __name__)


def _base_path() -> str:
    return "/corporate/profile-lab" if request.path.startswith("/corporate/") else "/partner/profile-lab"


def _can_view_as() -> bool:
    """True only for a REAL partner -- gates whether the Profile-Lab shows the
    'See their actual login view' buttons. The Profile Lab itself is open to
    corporate (require_level 'corporate'), but the employee/driver view-as
    swap is owner-only (the /view-as/* routes re-check via _require_owner), so
    we only render the button for partners to avoid offering a 403 action."""
    ru = getattr(g, "real_user", None) or getattr(g, "current_user", None)
    return ru is not None and level_at_least(ru.permission_level, "partner")


def _store_label(store_key: str | None) -> str:
    return _STORE_LABELS.get(store_key, (store_key or "").title())


def _money(value) -> str:
    try:
        return f"${float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _hours(value) -> str:
    try:
        return f"{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _audit_view(db, *, target_type: str, target_label: str, details: str) -> None:
    actor = getattr(g, "current_user", None)
    try:
        db.add(UserAuditLog(
            target_user_id=None,
            target_label=f"{target_type}:{target_label}"[:120],
            actor_user_id=actor.id if actor else None,
            actor_label=(actor.full_name if actor else None),
            action="profile_lab_view",
            before_value=None,
            after_value=None,
            details=details[:500],
            ip=(request.remote_addr or None) if request else None,
        ))
        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.exception("profile_lab audit write failed")


def _employee_stores(db, employee_id: int) -> list[str]:
    return [
        sk
        for (sk,) in (
            db.query(EmployeeStoreAssignment.store_key)
              .filter(EmployeeStoreAssignment.employee_id == employee_id)
              .order_by(EmployeeStoreAssignment.store_key.asc())
              .all()
        )
        if sk
    ]


def _employee_positions(db, employee_id: int) -> list[dict]:
    rows = (
        db.query(EmployeePosition.store_key, Position.name)
          .outerjoin(Position, EmployeePosition.position_id == Position.id)
          .filter(EmployeePosition.employee_id == employee_id)
          .order_by(EmployeePosition.store_key.asc(), Position.name.asc())
          .all()
    )
    out = []
    seen = set()
    for store_key, name in rows:
        label = (name or "").strip()
        if not label:
            continue
        item = {"name": label, "store": _store_label(store_key)}
        key = (item["name"], item["store"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _employee_perf(db, employee_id: int) -> dict:
    rank_row = (
        db.query(PerfRankCache)
          .filter(PerfRankCache.cena_employee_id == employee_id)
          .first()
    )
    rank_json = sanitize_rank_json(rank_row.rank_json) if rank_row and rank_row.rank_json else {}
    is_tipped = bool(rank_json.get("is_tipped"))

    period_rows = (
        db.query(PerfPeriodCache)
          .filter(PerfPeriodCache.cena_employee_id == employee_id)
          .order_by(PerfPeriodCache.period.asc())
          .all()
    )
    periods = []
    for row in period_rows:
        hours = float(row.total_hours or 0)
        base = float(row.base_pay or 0)
        tips = float(row.tips or 0) if is_tipped else 0.0
        total = base + tips
        item = {
            "period": row.period,
            "range": " - ".join([x for x in (row.period_start, row.period_end) if x]),
            "hours": _hours(hours),
            "base_pay": _money(base),
            "total_pay": _money(total),
            "effective_hourly": _money(total / hours) if hours > 0 else "Not available",
        }
        if is_tipped:
            item["tips"] = _money(tips)
            item["tips_per_hour"] = _money(tips / hours) if hours > 0 else "Not available"
        periods.append(item)

    rank_period = ((rank_json.get("ranks") or {}).get("last30") or {})
    rank_metrics = []
    for key, label in (
        ("effective_hourly", "Effective hourly"),
        ("tip_percent", "Tip percentage"),
        ("tips_per_hour", "Tips per hour"),
        ("combined", "Combined"),
    ):
        if key != "effective_hourly" and not is_tipped:
            continue
        obj = rank_period.get(key) or {}
        if not isinstance(obj, dict):
            continue
        rank_metrics.append({
            "label": label,
            "rank": obj.get("rank"),
            "cohort_size": obj.get("cohort_size"),
            "status": obj.get("status") or "Not available",
            "value": obj.get("value"),
        })

    leaderboards = []
    boards = ((rank_json.get("leaderboards") or {}).get("last30") or {})
    for key, label in (
        ("effective_hourly", "Effective hourly"),
        ("tip_percent", "Tip percentage"),
        ("tips_per_hour", "Tips per hour"),
        ("combined", "Combined"),
    ):
        if key != "effective_hourly" and not is_tipped:
            continue
        rows = (((boards.get(key) or {}).get("rows")) or [])[:12]
        if rows:
            leaderboards.append({"label": label, "rows": rows})

    return {
        "linked": db.query(CenaToastLink.id).filter(CenaToastLink.cena_employee_id == employee_id).count() > 0,
        "has_period_cache": bool(period_rows),
        "has_rank_cache": bool(rank_row and rank_row.rank_json),
        "is_tipped": is_tipped,
        "periods": periods,
        "rank_metrics": rank_metrics,
        "leaderboards": leaderboards,
        "computed_at": rank_row.computed_at if rank_row else None,
    }


def _employee_list_rows(db) -> list[dict]:
    employees = db.query(Employee).order_by(Employee.active.desc(), Employee.full_name.asc()).all()
    rows = []
    for emp in employees:
        stores = _employee_stores(db, emp.id)
        positions = _employee_positions(db, emp.id)
        perf = _employee_perf(db, emp.id)
        rows.append({
            "id": emp.id,
            "name": emp.full_name,
            "active": bool(emp.active),
            "stores": [_store_label(sk) for sk in stores],
            "positions": positions,
            "linked": perf["linked"],
            "is_tipped": perf["is_tipped"],
            "has_cache": perf["has_period_cache"] or perf["has_rank_cache"],
            "href": f"{_base_path()}/employee/{emp.id}",
        })
    return rows


def _driver_list_rows(db) -> list[dict]:
    drivers = db.query(Driver).order_by(Driver.active.desc(), Driver.name.asc()).all()
    rows = []
    for driver in drivers:
        rows.append({
            "id": driver.id,
            "name": driver.name,
            "active": bool(driver.active),
            "status": driver.status,
            "location": driver.location,
            "home_store": _store_label(driver.home_store_id or driver.location),
            "tier": driver.current_tier or "Not available",
            "score": driver.current_score,
            "href": f"{_base_path()}/driver/{driver.id}",
        })
    return rows


def _driver_profile(db, driver: Driver) -> dict:
    today = datetime.utcnow().date()
    latest_score = (
        db.query(DriverScore)
          .filter(DriverScore.driver_id == driver.id)
          .order_by(DriverScore.computed_at.desc())
          .first()
    )
    open_shift = (
        db.query(DriverShift)
          .filter(DriverShift.driver_id == driver.id, DriverShift.ended_at.is_(None))
          .order_by(DriverShift.started_at.desc())
          .first()
    )
    paychecks = (
        db.query(PayCheck)
          .filter(PayCheck.driver_id == driver.id)
          .order_by(PayCheck.closed_at.desc())
          .limit(6)
          .all()
    )
    logs = (
        db.query(DriverLog)
          .filter(DriverLog.driver_name == driver.name, DriverLog.location == driver.location)
          .order_by(DriverLog.created_at.desc())
          .limit(8)
          .all()
    )
    pending_requests = (
        db.query(DeliveryRequest)
          .filter(DeliveryRequest.driver_id == driver.id, DeliveryRequest.status == "pending")
          .count()
    )
    active_orders = (
        db.query(Order)
          .filter(
              Order.assigned_driver_id == driver.id,
              Order.status.in_(["approved", "picked_up", "en_route"]),
          )
          .count()
    )
    delivered_today = (
        db.query(Order)
          .filter(
              Order.assigned_driver_id == driver.id,
              Order.status == "delivered",
              Order.delivery_date == today.isoformat(),
          )
          .count()
    )

    return {
        "identity": {
            "name": driver.name,
            "active": bool(driver.active),
            "status": driver.status,
            "location": driver.location,
            "home_store": _store_label(driver.home_store_id or driver.location),
            "joined_at": driver.joined_at.isoformat() if driver.joined_at else None,
            "lifetime_delivery_count": driver.lifetime_delivery_count,
        },
        "operations": {
            "active_orders": active_orders,
            "pending_requests": pending_requests,
            "delivered_today": delivered_today,
            "open_shift_started": open_shift.started_at.isoformat() if open_shift else None,
        },
        "score": {
            "score": latest_score.score if latest_score else driver.current_score,
            "tier": latest_score.tier if latest_score else (driver.current_tier or "Not available"),
            "window": (
                f"{latest_score.window_start.isoformat()} - {latest_score.window_end.isoformat()}"
                if latest_score else "Not available"
            ),
            "tracking_pts": latest_score.tracking_pts if latest_score else None,
            "on_time_pts": latest_score.on_time_pts if latest_score else None,
            "cancellation_pts": latest_score.cancellation_pts if latest_score else None,
            "photo_pts": latest_score.photo_pts if latest_score else None,
            "response_pts": latest_score.response_pts if latest_score else None,
            "star_pts": latest_score.star_pts if latest_score else None,
        },
        "paychecks": [
            {
                "period": f"{p.pay_period_start.isoformat()} - {p.pay_period_end.isoformat()}",
                "closed_at": p.closed_at.isoformat(),
                "gross_amount": _money(p.gross_amount),
                "net_amount": _money(p.net_amount) if p.net_amount is not None else "Not available",
            }
            for p in paychecks
        ],
        "logs": [
            {
                "pickup_date": row.pickup_date,
                "location": row.location,
                "ex_miles": row.ex_miles,
                "ex_miles_verified": row.ex_miles_verified,
                "on_time": bool(row.on_time),
                "tracking": bool(row.tracking),
                "picture": bool(row.picture),
                "five_star": bool(row.five_star),
            }
            for row in logs
        ],
    }


def _filter_rows(rows: list[dict], query: str) -> list[dict]:
    q = (query or "").strip().lower()
    if not q:
        return rows
    out = []
    for row in rows:
        hay = " ".join(str(v) for v in row.values() if not isinstance(v, (list, dict))).lower()
        if q in hay:
            out.append(row)
    return out


@profile_lab_bp.route("/partner/profile-lab", methods=["GET"])
@profile_lab_bp.route("/corporate/profile-lab", methods=["GET"])
@require_level("corporate")
def profile_lab_index():
    q = (request.args.get("q") or "").strip()
    db = SessionLocal()
    try:
        employees = _filter_rows(_employee_list_rows(db), q)
        drivers = _filter_rows(_driver_list_rows(db), q)
        _audit_view(db, target_type="profile_lab", target_label="index", details=f"path={request.path}")
    finally:
        db.close()
    return render_template(
        "corporate_profile_lab.html",
        base_path=_base_path(),
        employees=employees,
        drivers=drivers,
        q=q,
        can_view_as=_can_view_as(),
        counts={
            "employees": len(employees),
            "drivers": len(drivers),
            "linked": sum(1 for row in employees if row["linked"]),
            "cached": sum(1 for row in employees if row["has_cache"]),
        },
    )


@profile_lab_bp.route("/partner/profile-lab/employee/<int:employee_id>", methods=["GET"])
@profile_lab_bp.route("/corporate/profile-lab/employee/<int:employee_id>", methods=["GET"])
@require_level("corporate")
def profile_lab_employee(employee_id: int):
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == employee_id).first()
        if emp is None:
            return redirect(url_for("corporate_profile_lab.profile_lab_index"))
        store_keys = _employee_stores(db, emp.id)
        profile = {
            "identity": {
                "name": emp.full_name,
                "active": bool(emp.active),
                "stores": [_store_label(sk) for sk in store_keys],
                "positions": _employee_positions(db, emp.id),
            },
            "performance": _employee_perf(db, emp.id),
            "schedule": _profile_schedule(db, emp.id),
            "roster": _profile_roster(db, store_keys),
        }
        _audit_view(
            db,
            target_type="employee",
            target_label=emp.full_name,
            details=f"path={request.path}; employee={employee_id}",
        )
    finally:
        db.close()
    return render_template(
        "corporate_profile_lab_detail.html",
        base_path=_base_path(),
        kind="employee",
        profile=profile,
        can_view_as=_can_view_as(),
        view_as_id=employee_id,
    )


@profile_lab_bp.route("/partner/profile-lab/driver/<int:driver_id>", methods=["GET"])
@profile_lab_bp.route("/corporate/profile-lab/driver/<int:driver_id>", methods=["GET"])
@require_level("corporate")
def profile_lab_driver(driver_id: int):
    db = SessionLocal()
    try:
        driver = db.query(Driver).filter(Driver.id == driver_id).first()
        if driver is None:
            return redirect(url_for("corporate_profile_lab.profile_lab_index"))
        profile = _driver_profile(db, driver)
        _audit_view(
            db,
            target_type="driver",
            target_label=driver.name,
            details=f"path={request.path}; driver={driver_id}",
        )
    finally:
        db.close()
    return render_template(
        "corporate_profile_lab_detail.html",
        base_path=_base_path(),
        kind="driver",
        profile=profile,
        can_view_as=_can_view_as(),
        view_as_id=driver_id,
    )
