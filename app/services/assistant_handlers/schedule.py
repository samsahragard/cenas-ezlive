"""Read-only Cenas AI handlers for internal Schedules V2 questions."""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    Employee,
    EmployeeAvailability,
    EmployeeStoreAssignment,
    EmployeeUnavailabilityBlock,
    Position,
    Schedule,
    Shift,
    ShiftAcceptance,
    ShiftAlarm,
    ShiftOffer,
    ShiftSwap,
    TimeOffRequest,
)

MAX_SAMPLE_ROWS = 20
_LOCAL_TZ = ZoneInfo("America/Chicago")
_STORE_ALIASES = {
    "1": "copperfield",
    "uno": "copperfield",
    "uno mas": "copperfield",
    "copperfield": "copperfield",
    "2": "tomball",
    "dos": "tomball",
    "dos mas": "tomball",
    "tomball": "tomball",
}


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _today_local() -> date:
    return datetime.now(_LOCAL_TZ).date()


def _now_local_naive() -> datetime:
    return datetime.now(_LOCAL_TZ).replace(tzinfo=None)


def _normalize_store(raw: Any) -> str:
    value = str(raw or "").strip().casefold()
    return _STORE_ALIASES.get(value, value or "unknown")


def _tool_store_filter(ctx: dict[str, Any]) -> set[str] | None:
    if ctx.get("is_owner_operator"):
        return None
    return {_normalize_store(store) for store in (ctx.get("store_slugs") or [])}


def _allowed_schedules(db: Session, ctx: dict[str, Any]) -> list[Schedule]:
    schedules = db.query(Schedule).all()
    allowed = _tool_store_filter(ctx)
    if allowed is None:
        return schedules
    if not allowed:
        return []
    return [row for row in schedules if _normalize_store(row.store_key) in allowed]


def _allowed_shifts(db: Session, ctx: dict[str, Any]) -> list[Shift]:
    schedule_ids = {row.id for row in _allowed_schedules(db, ctx)}
    if not schedule_ids:
        return []
    return db.query(Shift).filter(Shift.schedule_id.in_(list(schedule_ids))).all()


def _allowed_employee_ids(db: Session, ctx: dict[str, Any]) -> set[int]:
    allowed = _tool_store_filter(ctx)
    if allowed is not None and not allowed:
        return set()
    assignments = db.query(EmployeeStoreAssignment).all()
    if allowed is None:
        return {row.employee_id for row in assignments}
    return {row.employee_id for row in assignments if _normalize_store(row.store_key) in allowed}


def _schedule_store(db: Session, schedule_id: int | None) -> str:
    if not schedule_id:
        return "unknown"
    schedule = db.get(Schedule, schedule_id)
    return _normalize_store(schedule.store_key if schedule else None)


def _employee_names(db: Session) -> dict[int, str]:
    return {
        row.id: str(row.full_name or f"employee_{row.id}").strip()
        for row in db.query(Employee).all()
    }


def _position_names(db: Session) -> dict[int, str]:
    return {
        row.id: str(row.name or f"position_{row.id}").strip()
        for row in db.query(Position).all()
    }


def _week_start(value: date | None = None) -> date:
    value = value or _today_local()
    return value - timedelta(days=value.weekday())


def _safe_shift(
    db: Session,
    shift: Shift,
    employees: dict[int, str] | None = None,
    positions: dict[int, str] | None = None,
) -> dict[str, Any]:
    employees = employees or _employee_names(db)
    positions = positions or _position_names(db)
    employee_name = employees.get(shift.employee_id) if shift.employee_id else shift.display_name
    return {
        "store": _schedule_store(db, shift.schedule_id),
        "employee_name": employee_name,
        "position_name": positions.get(shift.position_id) if shift.position_id else None,
        "start_at": shift.start_at.isoformat() if shift.start_at else None,
        "end_at": shift.end_at.isoformat() if shift.end_at else None,
        "status": shift.status,
        "break_minutes": shift.break_minutes,
        "has_notes": bool(shift.notes),
    }


def _payload(tool_id: str, ctx: dict[str, Any], **data: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "tool_id": tool_id,
        "generated_at": _now_iso(),
        "data_class": "schedule_read_sanitized",
        "scope": {
            "owner_operator": bool(ctx.get("is_owner_operator")),
            "store_slugs": list(ctx.get("store_slugs") or []),
            "current_store": ctx.get("current_store"),
        },
        **data,
    }


def _shift_hours(shift: Shift) -> float:
    if not shift.start_at or not shift.end_at:
        return 0.0
    minutes = max(0, int((shift.end_at - shift.start_at).total_seconds() // 60))
    minutes = max(0, minutes - int(shift.break_minutes or 0))
    return round(minutes / 60, 2)


def _window_shifts(shifts: list[Shift], start: date, end: date) -> list[Shift]:
    return [
        shift for shift in shifts
        if shift.start_at and start <= shift.start_at.date() <= end
    ]


def schedule_store_today(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    today = _today_local()
    db = SessionLocal()
    try:
        shifts = sorted(_window_shifts(_allowed_shifts(db, ctx), today, today), key=lambda row: row.start_at)
        employees = _employee_names(db)
        positions = _position_names(db)
        return _payload(
            "schedule.store_today",
            ctx,
            question=question,
            date=today.isoformat(),
            shift_count=len(shifts),
            open_shift_count=sum(1 for row in shifts if row.status == "open" or row.employee_id is None),
            assigned_shift_count=sum(1 for row in shifts if row.employee_id is not None),
            total_hours=round(sum(_shift_hours(row) for row in shifts), 2),
            by_store=dict(Counter(_schedule_store(db, row.schedule_id) for row in shifts)),
            by_status=dict(Counter(str(row.status or "unknown") for row in shifts)),
            shifts=[_safe_shift(db, row, employees, positions) for row in shifts[:MAX_SAMPLE_ROWS]],
            truncated=len(shifts) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def schedule_store_week(question: str, ctx: dict[str, Any], *, tool_id: str = "schedule.store_week") -> dict[str, Any]:
    start = _week_start()
    end = start + timedelta(days=6)
    db = SessionLocal()
    try:
        schedules = [
            row for row in _allowed_schedules(db, ctx)
            if row.week_start == start
        ]
        shifts = sorted(_window_shifts(_allowed_shifts(db, ctx), start, end), key=lambda row: row.start_at)
        return _payload(
            tool_id,
            ctx,
            question=question,
            week_start=start.isoformat(),
            week_end=end.isoformat(),
            schedule_count=len(schedules),
            published_schedule_count=sum(1 for row in schedules if row.status == "published"),
            draft_schedule_count=sum(1 for row in schedules if row.status == "draft"),
            shift_count=len(shifts),
            open_shift_count=sum(1 for row in shifts if row.status == "open" or row.employee_id is None),
            assigned_shift_count=sum(1 for row in shifts if row.employee_id is not None),
            total_hours=round(sum(_shift_hours(row) for row in shifts), 2),
            by_store=dict(Counter(_schedule_store(db, row.schedule_id) for row in shifts)),
            by_status=dict(Counter(str(row.status or "unknown") for row in shifts)),
        )
    finally:
        db.close()


def schedule_view(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    return schedule_store_week(question, ctx, tool_id="schedule.view")


def schedule_open_shifts(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    today = _today_local()
    db = SessionLocal()
    try:
        shifts = [
            row for row in _allowed_shifts(db, ctx)
            if (row.status == "open" or row.employee_id is None)
            and row.start_at
            and row.start_at.date() >= today
        ]
        shifts = sorted(shifts, key=lambda row: row.start_at)
        positions = _position_names(db)
        return _payload(
            "schedule.open_shifts",
            ctx,
            question=question,
            count=len(shifts),
            by_store=dict(Counter(_schedule_store(db, row.schedule_id) for row in shifts)),
            shifts=[_safe_shift(db, row, {}, positions) for row in shifts[:MAX_SAMPLE_ROWS]],
            truncated=len(shifts) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def schedule_shift_acceptance_summary(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        shifts = [row for row in _allowed_shifts(db, ctx) if row.employee_id is not None]
        shift_ids = {row.id for row in shifts}
        rows = (
            db.query(ShiftAcceptance).filter(ShiftAcceptance.shift_id.in_(list(shift_ids))).all()
            if shift_ids
            else []
        )
        responded = {(row.shift_id, row.employee_id) for row in rows}
        pending = [
            row for row in shifts
            if (row.id, row.employee_id) not in responded
        ]
        return _payload(
            "schedule.shift_acceptance_summary",
            ctx,
            question=question,
            assigned_shift_count=len(shifts),
            response_count=len(rows),
            pending_count=len(pending),
            by_response=dict(Counter(str(row.response or "unknown") for row in rows)),
            by_store=dict(Counter(_schedule_store(db, row.schedule_id) for row in shifts)),
        )
    finally:
        db.close()


def schedule_alarm_pending_summary(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    now = _now_local_naive()
    db = SessionLocal()
    try:
        shift_ids = {row.id for row in _allowed_shifts(db, ctx)}
        rows = (
            db.query(ShiftAlarm).filter(ShiftAlarm.shift_id.in_(list(shift_ids))).all()
            if shift_ids
            else []
        )
        pending = [row for row in rows if row.status == "pending"]
        return _payload(
            "schedule.alarm_pending_summary",
            ctx,
            question=question,
            pending_count=len(pending),
            overdue_count=sum(1 for row in pending if row.alarm_time and row.alarm_time <= now),
            by_channel=dict(Counter(str(row.channel or "unknown") for row in pending)),
            by_status=dict(Counter(str(row.status or "unknown") for row in rows)),
        )
    finally:
        db.close()


def schedule_time_off_pending(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        employee_ids = _allowed_employee_ids(db, ctx)
        employees = _employee_names(db)
        rows = (
            db.query(TimeOffRequest)
            .filter(TimeOffRequest.employee_id.in_(list(employee_ids)))
            .filter(TimeOffRequest.status == "pending")
            .order_by(TimeOffRequest.start_date.asc(), TimeOffRequest.id.asc())
            .all()
            if employee_ids
            else []
        )
        return _payload(
            "schedule.time_off_pending",
            ctx,
            question=question,
            pending_count=len(rows),
            requests=[
                {
                    "employee_name": employees.get(row.employee_id),
                    "start_date": row.start_date.isoformat() if row.start_date else None,
                    "end_date": row.end_date.isoformat() if row.end_date else None,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "has_reason": bool(row.reason),
                }
                for row in rows[:MAX_SAMPLE_ROWS]
            ],
            truncated=len(rows) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def schedule_unavailability_blocks(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    now = _now_local_naive()
    db = SessionLocal()
    try:
        employee_ids = _allowed_employee_ids(db, ctx)
        employees = _employee_names(db)
        rows = (
            db.query(EmployeeUnavailabilityBlock)
            .filter(EmployeeUnavailabilityBlock.employee_id.in_(list(employee_ids)))
            .filter(EmployeeUnavailabilityBlock.end_at >= now)
            .order_by(EmployeeUnavailabilityBlock.start_at.asc())
            .all()
            if employee_ids
            else []
        )
        return _payload(
            "schedule.unavailability_blocks",
            ctx,
            question=question,
            block_count=len(rows),
            blocks=[
                {
                    "employee_name": employees.get(row.employee_id),
                    "start_at": row.start_at.isoformat() if row.start_at else None,
                    "end_at": row.end_at.isoformat() if row.end_at else None,
                    "has_reason": bool(row.reason),
                }
                for row in rows[:MAX_SAMPLE_ROWS]
            ],
            truncated=len(rows) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def _availability_warning(db: Session, shift: Shift, employees: dict[int, str]) -> dict[str, Any] | None:
    if not shift.employee_id or not shift.start_at or not shift.end_at:
        return None
    block = (
        db.query(EmployeeUnavailabilityBlock)
        .filter(EmployeeUnavailabilityBlock.employee_id == shift.employee_id)
        .filter(EmployeeUnavailabilityBlock.start_at < shift.end_at)
        .filter(EmployeeUnavailabilityBlock.end_at > shift.start_at)
        .first()
    )
    if block is not None:
        return {
            "employee_name": employees.get(shift.employee_id),
            "store": _schedule_store(db, shift.schedule_id),
            "start_at": shift.start_at.isoformat(),
            "conflict_type": "unavailability_block",
        }
    windows = (
        db.query(EmployeeAvailability)
        .filter(EmployeeAvailability.employee_id == shift.employee_id)
        .filter(EmployeeAvailability.day_of_week == shift.start_at.weekday())
        .all()
    )
    if not windows:
        return None
    start_minute = shift.start_at.hour * 60 + shift.start_at.minute
    end_minute = shift.end_at.hour * 60 + shift.end_at.minute
    if any(row.start_minute <= start_minute and end_minute <= row.end_minute for row in windows):
        return None
    return {
        "employee_name": employees.get(shift.employee_id),
        "store": _schedule_store(db, shift.schedule_id),
        "start_at": shift.start_at.isoformat(),
        "conflict_type": "outside_recurring_availability",
    }


def schedule_availability_conflicts(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    today = _today_local()
    db = SessionLocal()
    try:
        employees = _employee_names(db)
        shifts = [
            row for row in _allowed_shifts(db, ctx)
            if row.start_at and row.start_at.date() >= today and row.employee_id is not None
        ]
        conflicts = [item for row in shifts if (item := _availability_warning(db, row, employees))]
        return _payload(
            "schedule.availability_conflicts",
            ctx,
            question=question,
            conflict_count=len(conflicts),
            by_type=dict(Counter(row["conflict_type"] for row in conflicts)),
            conflicts=conflicts[:MAX_SAMPLE_ROWS],
            truncated=len(conflicts) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def schedule_shift_offer_summary(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        shift_ids = {row.id for row in _allowed_shifts(db, ctx)}
        rows = (
            db.query(ShiftOffer).filter(ShiftOffer.shift_id.in_(list(shift_ids))).all()
            if shift_ids
            else []
        )
        return _payload(
            "schedule.shift_offer_summary",
            ctx,
            question=question,
            offer_count=len(rows),
            by_status=dict(Counter(str(row.status or "unknown") for row in rows)),
            restricted_count=sum(1 for row in rows if row.restricted),
            recent_offers=[
                {
                    "status": row.status,
                    "restricted": bool(row.restricted),
                    "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                }
                for row in sorted(rows, key=lambda item: item.created_at or datetime.min, reverse=True)[:MAX_SAMPLE_ROWS]
            ],
            truncated=len(rows) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def schedule_shift_swap_summary(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        shift_ids = {row.id for row in _allowed_shifts(db, ctx)}
        rows = (
            db.query(ShiftSwap)
            .filter(ShiftSwap.from_shift_id.in_(list(shift_ids)))
            .filter(ShiftSwap.to_shift_id.in_(list(shift_ids)))
            .all()
            if shift_ids
            else []
        )
        return _payload(
            "schedule.shift_swap_summary",
            ctx,
            question=question,
            swap_count=len(rows),
            by_status=dict(Counter(str(row.status or "unknown") for row in rows)),
            recent_swaps=[
                {
                    "status": row.status,
                    "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                }
                for row in sorted(rows, key=lambda item: item.created_at or datetime.min, reverse=True)[:MAX_SAMPLE_ROWS]
            ],
            truncated=len(rows) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def _txt(question: str) -> str:
    return str(question or "").casefold()


def _schedule_context(question: str) -> bool:
    return bool(re.search(r"\b(schedule|schedules|shift|shifts|roster|time[- ]off|availability|unavailability|alarm|reminder)\b", _txt(question)))


def _wants_alarm_pending(question: str) -> bool:
    text = _txt(question)
    return _schedule_context(question) and bool(re.search(r"\b(alarm|alarms|reminders?|pending reminders?)\b", text))


def _wants_availability_conflicts(question: str) -> bool:
    text = _txt(question)
    return _schedule_context(question) and bool(re.search(r"\b(availability conflicts?|not normally available|outside availability|conflicts?)\b", text))


def _wants_open_shifts(question: str) -> bool:
    return _schedule_context(question) and bool(re.search(r"\b(open shifts?|unassigned shifts?)\b", _txt(question)))


def _wants_shift_acceptance(question: str) -> bool:
    return _schedule_context(question) and bool(re.search(r"\b(acceptance|accepted|declined|pending acceptance)\b", _txt(question)))


def _wants_shift_offers(question: str) -> bool:
    return _schedule_context(question) and bool(re.search(r"\b(shift offers?|offers?)\b", _txt(question)))


def _wants_shift_swaps(question: str) -> bool:
    return _schedule_context(question) and bool(re.search(r"\b(shift swaps?|swaps?)\b", _txt(question)))


def _wants_today(question: str) -> bool:
    return _schedule_context(question) and "today" in _txt(question)


def _wants_week(question: str) -> bool:
    return _schedule_context(question) and bool(re.search(r"\b(this week|week|weekly)\b", _txt(question)))


def _wants_time_off_pending(question: str) -> bool:
    text = _txt(question)
    return _schedule_context(question) and bool(re.search(r"\b(pending time[- ]off|time[- ]off pending|time[- ]off requests?)\b", text))


def _wants_unavailability_blocks(question: str) -> bool:
    text = _txt(question)
    return _schedule_context(question) and bool(re.search(r"\b(unavailability|unavailable blocks?|blocked availability)\b", text))


def _wants_view(question: str) -> bool:
    text = _txt(question)
    return _schedule_context(question) and bool(re.search(r"\b(view|show|list|summary|schedule|roster)\b", text))


SCHEDULE_TOOL_HANDLERS: dict[str, Callable[[str, dict[str, Any]], dict[str, Any]]] = {
    "schedule_alarm_pending_summary": schedule_alarm_pending_summary,
    "schedule_availability_conflicts": schedule_availability_conflicts,
    "schedule_open_shifts": schedule_open_shifts,
    "schedule_shift_acceptance_summary": schedule_shift_acceptance_summary,
    "schedule_shift_offer_summary": schedule_shift_offer_summary,
    "schedule_shift_swap_summary": schedule_shift_swap_summary,
    "schedule_store_today": schedule_store_today,
    "schedule_store_week": schedule_store_week,
    "schedule_time_off_pending": schedule_time_off_pending,
    "schedule_unavailability_blocks": schedule_unavailability_blocks,
    "schedule_view": schedule_view,
}


SCHEDULE_TOOL_MATCHERS: dict[str, Callable[[str], bool]] = {
    "schedule_alarm_pending_summary": _wants_alarm_pending,
    "schedule_availability_conflicts": _wants_availability_conflicts,
    "schedule_open_shifts": _wants_open_shifts,
    "schedule_shift_acceptance_summary": _wants_shift_acceptance,
    "schedule_shift_offer_summary": _wants_shift_offers,
    "schedule_shift_swap_summary": _wants_shift_swaps,
    "schedule_store_today": _wants_today,
    "schedule_store_week": _wants_week,
    "schedule_time_off_pending": _wants_time_off_pending,
    "schedule_unavailability_blocks": _wants_unavailability_blocks,
    "schedule_view": _wants_view,
}
