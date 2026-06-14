"""Draft schedule import helpers for Schedules V2.

Accepts parsed Sling-style shift records and inserts them as unpublished draft
shifts. This deliberately does not use the historical importer: that importer
publishes old weeks, while this helper is for manager review before publish.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import or_

from app.models import (
    CANONICAL_POSITIONS,
    CenaToastLink,
    Employee,
    EmployeePosition,
    EmployeeStoreAssignment,
    Position,
    Schedule,
    Shift,
    ShiftAcceptance,
    ShiftAlarm,
    ShiftOffer,
    ShiftSwap,
    ShiftTag,
    TimeOffRequest,
)

STORE_ALIASES = {
    "tomball": "tomball",
    "dos": "tomball",
    "27727 tomball parkway": "tomball",
    "copperfield": "copperfield",
    "uno": "copperfield",
    "fm 529 - copperfield": "copperfield",
    "fm529 copperfield": "copperfield",
}

POSITION_ALIASES = {
    "server trainee": "Training",
    "cashier training": "Training",
    "food runner": "Expo",
}


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _store_key(value: str | None) -> str | None:
    return STORE_ALIASES.get(_norm(value))


def _canonical_position(value: str | None) -> tuple[str | None, str | None]:
    original = re.sub(r"\s+", " ", (value or "").strip())
    if not original:
        return (None, None)
    alias = POSITION_ALIASES.get(_norm(original))
    if alias:
        return (alias, original)
    by_norm = {name.casefold(): name for name in CANONICAL_POSITIONS}
    return (by_norm.get(original.casefold()), original)


def _parse_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value or "").strip())


def _parse_dt(record: dict, key: str, day: date) -> datetime:
    raw = record.get(f"{key}_at") or record.get(key)
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None)
    raw_s = str(raw or "").strip()
    if not raw_s:
        raise ValueError(f"{key} required")
    try:
        return datetime.fromisoformat(raw_s).replace(tzinfo=None)
    except ValueError:
        t = datetime.strptime(raw_s.upper(), "%I:%M %p").time()
        return datetime(day.year, day.month, day.day, t.hour, t.minute)


def _record_date(record: dict) -> date:
    raw = record.get("shift_date") or record.get("iso_date") or record.get("date")
    if raw:
        return _parse_date(raw)
    start_raw = record.get("start_at")
    if start_raw:
        return datetime.fromisoformat(str(start_raw).strip()).date()
    raise ValueError("shift_date required")


def _target_weeks(week_start: date) -> list[date]:
    weeks = [week_start]
    if week_start.weekday() == 6:  # Sunday; legacy rows may be Saturday keyed.
        weeks.append(week_start - timedelta(days=1))
    return weeks


def _position_ids(db) -> dict[str, int]:
    out: dict[str, int] = {}
    now = datetime.utcnow()
    existing = db.query(Position).all()
    for pos in existing:
        if pos.name:
            out.setdefault(pos.name.casefold(), pos.id)
    for name in CANONICAL_POSITIONS:
        if name.casefold() not in out:
            pos = Position(name=name, store_key=None, created_at=now)
            db.add(pos)
            db.flush()
            out[name.casefold()] = pos.id
    return out


def _eligible_employee_lookup(db) -> tuple[dict[str, list[Employee]], dict[tuple[int, str], bool]]:
    by_name: dict[str, list[Employee]] = defaultdict(list)
    for emp in db.query(Employee).all():
        by_name[_norm(emp.full_name)].append(emp)

    assigned = {
        (row.employee_id, row.store_key)
        for row in db.query(EmployeeStoreAssignment).all()
    }
    linked = {
        (row.cena_employee_id, row.store_key)
        for row in db.query(CenaToastLink.cena_employee_id, CenaToastLink.store_key).all()
    }
    canonical_pids = {
        pid for (pid,) in db.query(Position.id).filter(Position.name.in_(CANONICAL_POSITIONS)).all()
    }
    has_position = {
        (row.employee_id, row.store_key)
        for row in db.query(EmployeePosition).all()
        if row.position_id in canonical_pids and row.store_key in ("tomball", "copperfield")
    }

    eligible: dict[tuple[int, str], bool] = {}
    emp_ids = {emp.id for emps in by_name.values() for emp in emps}
    for emp_id in emp_ids:
        for store in ("tomball", "copperfield"):
            eligible[(emp_id, store)] = (
                (emp_id, store) in assigned
                and (emp_id, store) in linked
                and (emp_id, store) in has_position
            )
    return by_name, eligible


def _match_employee(
    by_name: dict[str, list[Employee]],
    eligible: dict[tuple[int, str], bool],
    name: str,
    store: str,
) -> tuple[int | None, str | None]:
    candidates = by_name.get(_norm(name)) or []
    active_candidates = [emp for emp in candidates if emp.active]
    for emp in active_candidates:
        if eligible.get((emp.id, store)):
            return emp.id, None
    if not candidates:
        return None, "no employee profile match"
    if not active_candidates:
        return None, "employee profile inactive"
    return None, "not eligible on this store board"


def _timeoff_conflict(db, employee_id: int | None, on_date: date) -> str | None:
    if employee_id is None:
        return None
    row = (
        db.query(TimeOffRequest)
        .filter(
            TimeOffRequest.employee_id == employee_id,
            TimeOffRequest.status == "approved",
            TimeOffRequest.start_date <= on_date,
            TimeOffRequest.end_date >= on_date,
        )
        .first()
    )
    if row is None:
        return None
    emp = db.query(Employee).filter_by(id=employee_id).first()
    who = emp.full_name if (emp and emp.full_name) else "This employee"
    return f"{who} has approved time off {row.start_date.isoformat()} to {row.end_date.isoformat()}"


def _clear_schedule_rows(db, schedules: list[Schedule]) -> dict:
    """Delete schedules and shift-dependent rows for an authorized replacement."""
    schedule_ids = [s.id for s in schedules if s and s.id is not None]
    if not schedule_ids:
        return {"schedules": 0, "shifts": 0}
    shift_ids = [
        sid for (sid,) in db.query(Shift.id).filter(Shift.schedule_id.in_(schedule_ids)).all()
    ]
    if shift_ids:
        db.query(ShiftTag).filter(ShiftTag.shift_id.in_(shift_ids)).delete(synchronize_session=False)
        db.query(ShiftAcceptance).filter(
            ShiftAcceptance.shift_id.in_(shift_ids)
        ).delete(synchronize_session=False)
        db.query(ShiftAlarm).filter(
            ShiftAlarm.shift_id.in_(shift_ids)
        ).delete(synchronize_session=False)
        db.query(ShiftOffer).filter(ShiftOffer.shift_id.in_(shift_ids)).delete(synchronize_session=False)
        db.query(ShiftSwap).filter(
            or_(
                ShiftSwap.from_shift_id.in_(shift_ids),
                ShiftSwap.to_shift_id.in_(shift_ids),
            )
        ).delete(synchronize_session=False)
        db.query(Shift).filter(Shift.id.in_(shift_ids)).delete(synchronize_session=False)
    db.query(Schedule).filter(Schedule.id.in_(schedule_ids)).delete(synchronize_session=False)
    return {"schedules": len(schedule_ids), "shifts": len(shift_ids)}


def _prepare_records(records: list[dict], week_start: date) -> tuple[list[dict], list[dict]]:
    prepared: list[dict] = []
    errors: list[dict] = []
    week_end = week_start + timedelta(days=7)
    for idx, record in enumerate(records, start=1):
        try:
            name = re.sub(r"\s+", " ", str(record.get("employee_name") or record.get("name") or "").strip())
            if not name:
                raise ValueError("employee_name required")
            store = _store_key(record.get("store_key") or record.get("store") or record.get("location"))
            if store is None:
                raise ValueError("store_key must be tomball or copperfield")
            shift_date = _record_date(record)
            if not (week_start <= shift_date < week_end):
                raise ValueError("shift date is outside week_start")
            start_at = _parse_dt(record, "start", shift_date)
            end_at = _parse_dt(record, "end", shift_date)
            if end_at <= start_at:
                end_at += timedelta(days=1)
            position, original_position = _canonical_position(
                record.get("position_name") or record.get("position") or record.get("job")
            )
            if position is None:
                raise ValueError("unknown position")
            notes = re.sub(r"\s+", " ", str(record.get("notes") or "").strip()) or None
            prepared.append({
                "name": name,
                "store": store,
                "start_at": start_at,
                "end_at": end_at,
                "position": position,
                "original_position": original_position,
                "break_minutes": int(record.get("break_minutes") or 0),
                "notes": notes,
            })
        except Exception as exc:  # noqa: BLE001 - returned to caller as row data.
            errors.append({"row": idx, "error": str(exc), "record": record})
    return prepared, errors


def import_draft_records(
    records: list[dict],
    db,
    *,
    week_start: date,
    actor_id: int | None,
    commit: bool = False,
    replace_existing: bool = False,
    target_store: str | None = None,
) -> dict:
    """Validate and optionally insert unpublished draft shifts.

    Refuses the whole import if any target store/week already has shifts. This
    keeps a bad second run from duplicating 400+ rows.
    """
    prepared, errors = _prepare_records(records, week_start)
    if errors:
        return {"ok": False, "error": "some records could not be parsed", "errors": errors[:25]}
    if not prepared:
        return {"ok": False, "error": "no usable shift records"}

    target_stores = sorted({r["store"] for r in prepared})
    target_store_key = _store_key(target_store) if target_store else None
    if replace_existing:
        if target_store_key is None:
            return {
                "ok": False,
                "error": "target_store is required when replace_existing=true",
            }
        if target_stores != [target_store_key]:
            return {
                "ok": False,
                "error": "replace_existing target_store must match every record",
                "target_store": target_store_key,
                "record_stores": target_stores,
            }
    blockers = []
    schedules: dict[str, Schedule] = {}
    existing_by_store: dict[str, list[Schedule]] = {}
    for store in target_stores:
        existing = (
            db.query(Schedule)
            .filter(Schedule.store_key == store, Schedule.week_start.in_(_target_weeks(week_start)))
            .order_by(Schedule.week_start.desc())
            .all()
        )
        existing_by_store[store] = existing
        for sched in existing:
            shift_count = db.query(Shift).filter_by(schedule_id=sched.id).count()
            if replace_existing:
                continue
            if shift_count:
                blockers.append({
                    "store": store,
                    "week_start": sched.week_start.isoformat(),
                    "schedule_id": sched.id,
                    "shifts": shift_count,
                    "status": sched.status,
                })
            elif sched.week_start == week_start and sched.status == "draft" and sched.published_at is None:
                schedules[store] = sched
            elif sched.week_start == week_start:
                blockers.append({
                    "store": store,
                    "week_start": sched.week_start.isoformat(),
                    "schedule_id": sched.id,
                    "shifts": shift_count,
                    "status": sched.status,
                })
    if blockers:
        return {
            "ok": False,
            "error": "target week already has schedule data; import refused",
            "blockers": blockers,
        }

    position_ids = _position_ids(db)
    by_name, eligible = _eligible_employee_lookup(db)
    now = datetime.utcnow()
    matched = 0
    name_only = 0
    name_only_reasons: Counter[str] = Counter()
    mapped_roles: Counter[str] = Counter()
    per_store: Counter[str] = Counter()
    per_position: Counter[str] = Counter()
    cleared = {"schedules": 0, "shifts": 0}

    if commit:
        if replace_existing:
            for store in target_stores:
                removed = _clear_schedule_rows(db, existing_by_store.get(store, []))
                cleared["schedules"] += removed["schedules"]
                cleared["shifts"] += removed["shifts"]
            schedules = {}
        for store in target_stores:
            if store not in schedules:
                sched = Schedule(
                    store_key=store,
                    week_start=week_start,
                    status="draft",
                    published_at=None,
                    created_by=actor_id,
                    created_at=now,
                    updated_at=now,
                )
                db.add(sched)
                db.flush()
                schedules[store] = sched

    for rec in prepared:
        emp_id, name_only_reason = _match_employee(by_name, eligible, rec["name"], rec["store"])
        conflict = _timeoff_conflict(db, emp_id, rec["start_at"].date()) if emp_id else None
        if conflict:
            name_only_reason = f"time-off conflict: {conflict}"
            emp_id = None
        if emp_id:
            matched += 1
        else:
            name_only += 1
            name_only_reasons[name_only_reason or "not eligible on this store board"] += 1

        notes = []
        if rec["original_position"] and rec["original_position"].casefold() != rec["position"].casefold():
            notes.append(f"Sling role: {rec['original_position']}")
            mapped_roles[f"{rec['original_position']} -> {rec['position']}"] += 1
        if name_only_reason:
            notes.append(f"Needs review: {name_only_reason}")
        if rec["notes"]:
            notes.append(rec["notes"])
        note_text = " | ".join(notes) or None

        per_store[rec["store"]] += 1
        per_position[rec["position"]] += 1
        if commit:
            db.add(Shift(
                schedule_id=schedules[rec["store"]].id,
                employee_id=emp_id,
                display_name=None if emp_id else rec["name"],
                position_id=position_ids[rec["position"].casefold()],
                start_at=rec["start_at"],
                end_at=rec["end_at"],
                break_minutes=rec["break_minutes"],
                status="assigned",
                notes=note_text,
                published_at=None,
                created_at=now,
                updated_at=now,
            ))

    if commit:
        db.commit()
    else:
        db.rollback()

    return {
        "ok": True,
        "committed": bool(commit),
        "week_start": week_start.isoformat(),
        "target_stores": target_stores,
        "shifts": len(prepared),
        "matched": matched,
        "name_only": name_only,
        "name_only_reasons": dict(name_only_reasons),
        "mapped_roles": dict(mapped_roles),
        "per_store": dict(per_store),
        "per_position": dict(per_position),
        "schedule_ids": {store: schedules[store].id for store in schedules} if commit else {},
        "published_shifts": 0,
        "replace_existing": bool(replace_existing),
        "cleared": cleared if commit else {
            "schedules": sum(len(existing_by_store.get(store, [])) for store in target_stores),
            "shifts": sum(
                db.query(Shift)
                .filter(Shift.schedule_id.in_([s.id for s in existing_by_store.get(store, [])]))
                .count()
                for store in target_stores
            ),
        },
    }
