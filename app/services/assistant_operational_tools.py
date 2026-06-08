"""Read-only operational tools for the Cenas AI assistant.

These functions are intentionally not wired into routing here. Agent 1 owns the
frozen router unlock; this module only provides safe, query-only implementations
plus a small dispatcher for focused tests and later activation.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Iterable

from sqlalchemy import and_, or_

from app.db import SessionLocal
from app.models import (
    AttendanceShift,
    Employee,
    EmployeeAlarmPreference,
    EmployeeAvailability,
    EmployeePhone,
    EmployeePosition,
    EmployeeStoreAssignment,
    EmployeeUnavailabilityBlock,
    Position,
    PrepEntry,
    PrepItem,
    Recipe,
    Schedule,
    Shift,
    ShiftAlarm,
    TimeOffRequest,
    VendorRecentOrder,
)
from app.services.assistant_routing_shared import STORE_ALIASES, normalize_store_key


def _today() -> date:
    return date.today()


def _as_date(value: Any, fallback: date | None = None) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if value:
        return date.fromisoformat(str(value)[:10])
    return fallback or _today()


def _week_start(value: Any = None) -> date:
    d = _as_date(value)
    return d - timedelta(days=d.weekday())


def _day_bounds(value: Any = None) -> tuple[datetime, datetime, date]:
    d = _as_date(value)
    return datetime.combine(d, time.min), datetime.combine(d + timedelta(days=1), time.min), d


def _store(value: Any = None) -> str | None:
    if not value:
        return None
    key = str(value).strip().lower()
    if key in {"all", "both", "partner", "corporate"}:
        return None
    return normalize_store_key(key)


def _limit(value: Any, default: int = 20, maximum: int = 100) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(maximum, n))


@contextmanager
def _session_scope(db=None):
    if db is not None:
        yield db
        return
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL not set")
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _employee_or_none(db, employee_id: int | str | None) -> Employee | None:
    try:
        emp_id = int(employee_id)
    except (TypeError, ValueError):
        return None
    return db.query(Employee).filter(Employee.id == emp_id).one_or_none()


def _stores_for_employee(db, employee_id: int) -> list[str]:
    rows = (
        db.query(EmployeeStoreAssignment.store_key)
        .filter(EmployeeStoreAssignment.employee_id == employee_id)
        .order_by(EmployeeStoreAssignment.store_key.asc())
        .all()
    )
    return [r[0] for r in rows if r[0]]


def _positions_for_employee(db, employee_id: int) -> list[dict[str, Any]]:
    rows = (
        db.query(EmployeePosition, Position)
        .join(Position, EmployeePosition.position_id == Position.id)
        .filter(EmployeePosition.employee_id == employee_id)
        .order_by(EmployeePosition.store_key.asc(), Position.name.asc())
        .all()
    )
    return [
        {"store": ep.store_key, "position": pos.name, "position_id": pos.id}
        for ep, pos in rows
    ]


def _shift_row(shift: Shift, schedule: Schedule, employee: Employee | None, position: Position | None) -> dict[str, Any]:
    return {
        "id": shift.id,
        "store": schedule.store_key,
        "week_start": schedule.week_start.isoformat() if schedule.week_start else None,
        "employee_id": shift.employee_id,
        "employee_name": employee.full_name if employee else shift.display_name,
        "position": position.name if position else None,
        "start_at": shift.start_at.isoformat() if shift.start_at else None,
        "end_at": shift.end_at.isoformat() if shift.end_at else None,
        "break_minutes": shift.break_minutes,
        "status": shift.status,
        "published": bool(shift.published_at),
        "notes": shift.notes,
    }


def _schedule_rows(db, *, store: str | None, start: datetime, end: datetime, employee_id: int | None = None) -> list[dict[str, Any]]:
    q = (
        db.query(Shift, Schedule, Employee, Position)
        .join(Schedule, Shift.schedule_id == Schedule.id)
        .outerjoin(Employee, Shift.employee_id == Employee.id)
        .outerjoin(Position, Shift.position_id == Position.id)
        .filter(Shift.start_at >= start, Shift.start_at < end)
    )
    if store:
        q = q.filter(Schedule.store_key == store)
    if employee_id is not None:
        q = q.filter(Shift.employee_id == employee_id)
    rows = q.order_by(Shift.start_at.asc(), Employee.full_name.asc()).all()
    return [_shift_row(shift, sched, emp, pos) for shift, sched, emp, pos in rows]


def _attendance_rows(db, *, store: str | None, day: date, statuses: Iterable[str] | None = None) -> list[AttendanceShift]:
    q = db.query(AttendanceShift).filter(AttendanceShift.entry_date == day)
    if store:
        q = q.filter(AttendanceShift.store_scope == store)
    if statuses:
        q = q.filter(AttendanceShift.status.in_(list(statuses)))
    return q.order_by(AttendanceShift.employee_name.asc()).all()


def _attendance_payload(row: AttendanceShift) -> dict[str, Any]:
    return {
        "id": row.id,
        "store": row.store_scope,
        "date": row.entry_date.isoformat() if row.entry_date else None,
        "employee_name": row.employee_name,
        "role_title": row.role_title,
        "section": row.section,
        "scheduled_start": row.scheduled_start.isoformat() if row.scheduled_start else None,
        "scheduled_end": row.scheduled_end.isoformat() if row.scheduled_end else None,
        "clock_in": row.clock_in.isoformat() if row.clock_in else None,
        "clock_out": row.clock_out.isoformat() if row.clock_out else None,
        "status": row.status,
        "late_minutes": row.late_minutes,
        "note": row.note,
    }


def employee_my_profile(args: dict[str, Any], db=None) -> dict[str, Any]:
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        return {
            "ok": True,
            "employee": {
                "id": emp.id,
                "full_name": emp.full_name,
                "email": emp.email,
                "phone": emp.phone,
                "active": emp.active,
                "created_at": emp.created_at.isoformat() if emp.created_at else None,
                "stores": _stores_for_employee(s, emp.id),
                "positions": _positions_for_employee(s, emp.id),
            },
        }


def employee_my_contact(args: dict[str, Any], db=None) -> dict[str, Any]:
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        phones = (
            s.query(EmployeePhone)
            .filter(EmployeePhone.employee_id == emp.id)
            .order_by(EmployeePhone.is_primary.desc(), EmployeePhone.id.asc())
            .all()
        )
        return {
            "ok": True,
            "contact": {
                "employee_id": emp.id,
                "full_name": emp.full_name,
                "email": emp.email,
                "phone": emp.phone,
                "address": emp.address,
                "secondary_phones": [
                    {"phone": p.phone, "is_primary": p.is_primary} for p in phones
                ],
            },
        }


def employee_my_stores(args: dict[str, Any], db=None) -> dict[str, Any]:
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        return {"ok": True, "employee_id": emp.id, "stores": _stores_for_employee(s, emp.id)}


def employee_my_positions(args: dict[str, Any], db=None) -> dict[str, Any]:
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        return {"ok": True, "employee_id": emp.id, "positions": _positions_for_employee(s, emp.id)}


def employee_my_schedule_today(args: dict[str, Any], db=None) -> dict[str, Any]:
    start, end, day = _day_bounds(args.get("date"))
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        rows = _schedule_rows(s, store=None, start=start, end=end, employee_id=emp.id)
        return {"ok": True, "employee_id": emp.id, "date": day.isoformat(), "count": len(rows), "shifts": rows}


def employee_my_schedule_week(args: dict[str, Any], db=None) -> dict[str, Any]:
    week = _week_start(args.get("week_start"))
    start = datetime.combine(week, time.min)
    end = start + timedelta(days=7)
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        rows = _schedule_rows(s, store=None, start=start, end=end, employee_id=emp.id)
        return {"ok": True, "employee_id": emp.id, "week_start": week.isoformat(), "count": len(rows), "shifts": rows}


def employee_my_recent_shifts(args: dict[str, Any], db=None) -> dict[str, Any]:
    lim = _limit(args.get("limit"), 10, 50)
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        rows = (
            s.query(Shift, Schedule, Employee, Position)
            .join(Schedule, Shift.schedule_id == Schedule.id)
            .outerjoin(Employee, Shift.employee_id == Employee.id)
            .outerjoin(Position, Shift.position_id == Position.id)
            .filter(Shift.employee_id == emp.id)
            .order_by(Shift.start_at.desc())
            .limit(lim)
            .all()
        )
        shifts = [_shift_row(shift, sched, employee, pos) for shift, sched, employee, pos in rows]
        return {"ok": True, "employee_id": emp.id, "count": len(shifts), "shifts": shifts}


def employee_my_open_shifts(args: dict[str, Any], db=None) -> dict[str, Any]:
    lim = _limit(args.get("limit"), 20, 100)
    now = datetime.combine(_today(), time.min)
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        stores = _stores_for_employee(s, emp.id)
        q = (
            s.query(Shift, Schedule, Employee, Position)
            .join(Schedule, Shift.schedule_id == Schedule.id)
            .outerjoin(Employee, Shift.employee_id == Employee.id)
            .outerjoin(Position, Shift.position_id == Position.id)
            .filter(Shift.employee_id.is_(None), Shift.start_at >= now)
        )
        if stores:
            q = q.filter(Schedule.store_key.in_(stores))
        rows = q.order_by(Shift.start_at.asc()).limit(lim).all()
        shifts = [_shift_row(shift, sched, employee, pos) for shift, sched, employee, pos in rows]
        return {"ok": True, "employee_id": emp.id, "stores": stores, "count": len(shifts), "open_shifts": shifts}


def employee_my_availability(args: dict[str, Any], db=None) -> dict[str, Any]:
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        rows = (
            s.query(EmployeeAvailability)
            .filter(EmployeeAvailability.employee_id == emp.id)
            .order_by(EmployeeAvailability.day_of_week.asc(), EmployeeAvailability.start_minute.asc())
            .all()
        )
        return {
            "ok": True,
            "employee_id": emp.id,
            "availability": [
                {"day_of_week": r.day_of_week, "start_minute": r.start_minute, "end_minute": r.end_minute}
                for r in rows
            ],
        }


def employee_my_time_off(args: dict[str, Any], db=None) -> dict[str, Any]:
    lim = _limit(args.get("limit"), 20, 100)
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        rows = (
            s.query(TimeOffRequest)
            .filter(TimeOffRequest.employee_id == emp.id)
            .order_by(TimeOffRequest.start_date.desc())
            .limit(lim)
            .all()
        )
        return {
            "ok": True,
            "employee_id": emp.id,
            "requests": [
                {
                    "id": r.id,
                    "start_date": r.start_date.isoformat(),
                    "end_date": r.end_date.isoformat(),
                    "status": r.status,
                    "reason": r.reason,
                }
                for r in rows
            ],
        }


def employee_my_shift_alarm_settings(args: dict[str, Any], db=None) -> dict[str, Any]:
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        pref = (
            s.query(EmployeeAlarmPreference)
            .filter(EmployeeAlarmPreference.employee_id == emp.id)
            .one_or_none()
        )
        pending = (
            s.query(ShiftAlarm)
            .filter(ShiftAlarm.employee_id == emp.id, ShiftAlarm.status == "pending")
            .order_by(ShiftAlarm.alarm_time.asc())
            .limit(20)
            .all()
        )
        return {
            "ok": True,
            "employee_id": emp.id,
            "settings": {
                "sms_enabled": True if pref is None else pref.sms_enabled,
                "email_enabled": False if pref is None else pref.email_enabled,
                "minutes_before": 60 if pref is None else pref.minutes_before,
                "second_minutes_before": None if pref is None else pref.second_minutes_before,
            },
            "pending_alarms": [
                {"shift_id": r.shift_id, "alarm_time": r.alarm_time.isoformat(), "channel": r.channel}
                for r in pending
            ],
        }


def employee_my_attendance_summary(args: dict[str, Any], db=None) -> dict[str, Any]:
    days = _limit(args.get("days"), 14, 120)
    start_day = _today() - timedelta(days=days - 1)
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        rows = (
            s.query(AttendanceShift)
            .filter(AttendanceShift.employee_name == emp.full_name, AttendanceShift.entry_date >= start_day)
            .order_by(AttendanceShift.entry_date.desc())
            .all()
        )
        counts = Counter(r.status for r in rows)
        return {
            "ok": True,
            "employee_id": emp.id,
            "employee_name": emp.full_name,
            "days": days,
            "by_status": dict(counts),
            "late_minutes": sum(r.late_minutes or 0 for r in rows),
            "rows": [_attendance_payload(r) for r in rows[:20]],
        }


def employee_my_day_breakdown(args: dict[str, Any], db=None) -> dict[str, Any]:
    start, end, day = _day_bounds(args.get("date"))
    with _session_scope(db) as s:
        emp = _employee_or_none(s, args.get("employee_id"))
        if not emp:
            return {"ok": False, "error": "employee_not_found"}
        shifts = _schedule_rows(s, store=None, start=start, end=end, employee_id=emp.id)
        attendance = (
            s.query(AttendanceShift)
            .filter(AttendanceShift.employee_name == emp.full_name, AttendanceShift.entry_date == day)
            .order_by(AttendanceShift.scheduled_start.asc())
            .all()
        )
        return {
            "ok": True,
            "employee_id": emp.id,
            "date": day.isoformat(),
            "schedule": shifts,
            "attendance": [_attendance_payload(r) for r in attendance],
        }


def schedules_today_view(args: dict[str, Any], db=None) -> dict[str, Any]:
    store = _store(args.get("store"))
    start, end, day = _day_bounds(args.get("date"))
    with _session_scope(db) as s:
        rows = _schedule_rows(s, store=store, start=start, end=end)
        return {"ok": True, "store": store or "all", "date": day.isoformat(), "count": len(rows), "shifts": rows}


def schedules_week_view(args: dict[str, Any], db=None) -> dict[str, Any]:
    store = _store(args.get("store"))
    week = _week_start(args.get("week_start"))
    start = datetime.combine(week, time.min)
    end = start + timedelta(days=7)
    with _session_scope(db) as s:
        rows = _schedule_rows(s, store=store, start=start, end=end)
        by_day: dict[str, int] = defaultdict(int)
        for row in rows:
            by_day[str(row["start_at"])[:10]] += 1
        return {
            "ok": True,
            "store": store or "all",
            "week_start": week.isoformat(),
            "count": len(rows),
            "by_day": dict(sorted(by_day.items())),
            "shifts": rows,
        }


def kitchen_recipe_search(args: dict[str, Any], db=None) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    category = (args.get("category") or "").strip()
    lim = _limit(args.get("limit"), 10, 50)
    with _session_scope(db) as s:
        q = s.query(Recipe)
        if category:
            q = q.filter(Recipe.category == category)
        if query:
            pat = f"%{query}%"
            q = q.filter(or_(Recipe.name.ilike(pat), Recipe.code.ilike(pat), Recipe.english_instructions.ilike(pat)))
        rows = q.order_by(Recipe.category.asc(), Recipe.name.asc()).limit(lim).all()
        return {
            "ok": True,
            "count": len(rows),
            "recipes": [
                {
                    "id": r.id,
                    "code": r.code,
                    "name": r.name,
                    "category": r.category,
                    "prep_time": r.prep_time,
                    "shelf_life": r.shelf_life,
                }
                for r in rows
            ],
        }


def kitchen_recipe_lookup(args: dict[str, Any], db=None) -> dict[str, Any]:
    with _session_scope(db) as s:
        q = s.query(Recipe)
        if args.get("recipe_id") is not None:
            recipe = q.filter(Recipe.id == int(args["recipe_id"])).one_or_none()
        elif args.get("code"):
            recipe = q.filter(Recipe.code == str(args["code"]).strip()).one_or_none()
        elif args.get("name"):
            recipe = q.filter(Recipe.name.ilike(f"%{str(args['name']).strip()}%")).order_by(Recipe.name.asc()).first()
        else:
            recipe = None
        if not recipe:
            return {"ok": False, "error": "recipe_not_found"}
        return {
            "ok": True,
            "recipe": {
                "id": recipe.id,
                "code": recipe.code,
                "category": recipe.category,
                "name": recipe.name,
                "prep_time": recipe.prep_time,
                "shelf_life": recipe.shelf_life,
                "english_instructions": recipe.english_instructions,
                "spanish_instructions": recipe.spanish_instructions,
                "ingredients_json": recipe.ingredients_json,
                "batch_sizes_json": recipe.batch_sizes_json,
                "notes": recipe.notes,
            },
        }


def _prep_entries(args: dict[str, Any], db=None) -> dict[str, Any]:
    store = _store(args.get("store"))
    day = _as_date(args.get("date"))
    with _session_scope(db) as s:
        q = (
            s.query(PrepEntry, PrepItem)
            .join(PrepItem, PrepEntry.prep_item_id == PrepItem.id)
            .filter(PrepEntry.entry_date == day)
        )
        if store:
            q = q.filter(or_(PrepEntry.store_scope == store, PrepEntry.store_scope.is_(None)))
        rows = q.order_by(PrepItem.sort_order.asc(), PrepItem.name.asc()).all()
        entries = [
            {
                "id": entry.id,
                "date": entry.entry_date.isoformat(),
                "store": entry.store_scope,
                "item": item.name,
                "category": item.category,
                "kind": item.kind,
                "selected": entry.selected,
                "on_hand": entry.on_hand,
                "prep_qty": entry.prep_qty,
                "assignee_name": entry.assignee_name,
                "status": entry.status,
                "batch_size": entry.batch_size,
                "locked": entry.locked,
            }
            for entry, item in rows
        ]
        return {"ok": True, "store": store or "all", "date": day.isoformat(), "count": len(entries), "entries": entries}


def vendors_recent_orders(args: dict[str, Any], db=None) -> dict[str, Any]:
    store = _store(args.get("store"))
    vendor = (args.get("vendor") or "").strip()
    lim = _limit(args.get("limit"), 10, 50)
    with _session_scope(db) as s:
        q = s.query(VendorRecentOrder)
        if store:
            q = q.filter(VendorRecentOrder.store_scope == store)
        if vendor:
            q = q.filter(VendorRecentOrder.vendor == vendor)
        rows = q.order_by(VendorRecentOrder.placed_at.desc().nullslast(), VendorRecentOrder.created_at.desc()).limit(lim).all()
        return {
            "ok": True,
            "store": store or "all",
            "vendor": vendor or "all",
            "count": len(rows),
            "orders": [
                {
                    "id": r.id,
                    "vendor": r.vendor,
                    "store": r.store_scope,
                    "order_number": r.order_number,
                    "placed_at": r.placed_at.isoformat() if r.placed_at else None,
                    "total_cents": r.total_cents,
                    "status": r.status,
                    "parse_status": r.parse_status,
                    "items_json": r.items_json,
                }
                for r in rows
            ],
        }


def attendance_board_summary(args: dict[str, Any], db=None) -> dict[str, Any]:
    store = _store(args.get("store"))
    day = _as_date(args.get("date"))
    with _session_scope(db) as s:
        rows = _attendance_rows(s, store=store, day=day)
        counts = Counter(r.status for r in rows)
        return {
            "ok": True,
            "store": store or "all",
            "date": day.isoformat(),
            "count": len(rows),
            "by_status": dict(counts),
            "late_minutes": sum(r.late_minutes or 0 for r in rows),
            "rows": [_attendance_payload(r) for r in rows],
        }


def _attendance_status_summary(args: dict[str, Any], statuses: Iterable[str], db=None) -> dict[str, Any]:
    store = _store(args.get("store"))
    day = _as_date(args.get("date"))
    with _session_scope(db) as s:
        rows = _attendance_rows(s, store=store, day=day, statuses=statuses)
        return {
            "ok": True,
            "store": store or "all",
            "date": day.isoformat(),
            "statuses": list(statuses),
            "count": len(rows),
            "rows": [_attendance_payload(r) for r in rows],
        }


def attendance_missed_punch_summary(args: dict[str, Any], db=None) -> dict[str, Any]:
    store = _store(args.get("store"))
    day = _as_date(args.get("date"))
    with _session_scope(db) as s:
        q = s.query(AttendanceShift).filter(AttendanceShift.entry_date == day)
        if store:
            q = q.filter(AttendanceShift.store_scope == store)
        rows = q.filter(or_(
            and_(AttendanceShift.clock_in.is_(None), AttendanceShift.status.notin_(["no-show", "callout"])),
            and_(AttendanceShift.clock_in.is_not(None), AttendanceShift.clock_out.is_(None), AttendanceShift.status == "out"),
        )).order_by(AttendanceShift.employee_name.asc()).all()
        return {
            "ok": True,
            "store": store or "all",
            "date": day.isoformat(),
            "count": len(rows),
            "rows": [_attendance_payload(r) for r in rows],
        }


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any], Any], dict[str, Any]]] = {
    "employee.my_profile.read": employee_my_profile,
    "employee.my_contact.read": employee_my_contact,
    "employee.my_stores.read": employee_my_stores,
    "employee.my_positions.read": employee_my_positions,
    "employee.my_schedule.today": employee_my_schedule_today,
    "employee.my_schedule.week": employee_my_schedule_week,
    "employee.my_recent_shifts": employee_my_recent_shifts,
    "employee.my_open_shifts": employee_my_open_shifts,
    "employee.my_availability.read": employee_my_availability,
    "employee.my_time_off.status": employee_my_time_off,
    "employee.my_shift_alarm_settings": employee_my_shift_alarm_settings,
    "employee.my_attendance_summary": employee_my_attendance_summary,
    "employee.my_day_breakdown": employee_my_day_breakdown,
    "schedules.today_view": schedules_today_view,
    "schedules.week_view": schedules_week_view,
    "kitchen.recipe_search": kitchen_recipe_search,
    "kitchen.recipe_lookup": kitchen_recipe_lookup,
    "kitchen.prep_list_today": _prep_entries,
    "kitchen.prep_entries_by_day": _prep_entries,
    "vendors.vendor_recent_orders": vendors_recent_orders,
    "attendance.manager_board_summary": attendance_board_summary,
    "attendance.late_summary": lambda args, db=None: _attendance_status_summary(args, ["late"], db),
    "attendance.no_show_summary": lambda args, db=None: _attendance_status_summary(args, ["no-show"], db),
    "attendance.callout_summary": lambda args, db=None: _attendance_status_summary(args, ["callout"], db),
    "attendance.missed_punch_summary": attendance_missed_punch_summary,
}


def implemented_tool_ids() -> tuple[str, ...]:
    return tuple(sorted(TOOL_HANDLERS))


def run_operational_tool(tool_id: str, args: dict[str, Any] | None = None, *, db=None) -> dict[str, Any]:
    handler = TOOL_HANDLERS.get(tool_id)
    if handler is None:
        return {"ok": False, "error": "unknown_tool", "tool_id": tool_id}
    payload = handler(args or {}, db)
    payload.setdefault("ok", True)
    payload["tool_id"] = tool_id
    payload["read_only"] = True
    return payload
