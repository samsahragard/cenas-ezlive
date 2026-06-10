"""Historical schedule import (Sam #2872).

Loads exported shift records (parsed from the weekly-grid CSV) into the V2
schedule model so a manager paging BACK through past weeks sees what was
scheduled. Each name is matched to a current Employee; anyone no longer employed
(no Employee record) is stored as a name-only shift (employee_id NULL +
display_name) so the week-view renders them struck-through. Jobs map to the
closest Position; the store comes from the record.

Idempotency: import-created Schedules are marked with created_by=IMPORT_SENTINEL.
Re-running CLEARS + reinserts those schedules' shifts, and NEVER touches a real
manager-made schedule for the same week (those are skipped + reported).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from app.models import Employee, EmployeeStoreAssignment, Position, Schedule, Shift

# Schedule.created_by marker for histimport. Negative => never a real user id, so
# we can always tell an import-created week from a manager-made one.
IMPORT_SENTINEL = -2872


def _norm(name: str) -> str:
    """Normalized match key: collapse internal whitespace, strip, lowercase."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _week_start(d: date) -> date:
    # The schedule week starts SUNDAY (schedules_v2_week.html WEEK_START_DOW=0),
    # and the board queries Schedule by that Sunday week_start. Python weekday():
    # Mon=0..Sun=6, so days since Sunday is (weekday + 1) % 7.
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _parse_dt(iso_date: str, time_str: str) -> datetime:
    """('2026-03-01', '9:00 AM') -> datetime. Accepts 12h with AM/PM."""
    d = date.fromisoformat(iso_date)
    t = datetime.strptime((time_str or "").strip(), "%I:%M %p").time()
    return datetime(d.year, d.month, d.day, t.hour, t.minute)


def _position_id_for_job(db, job: str) -> int | None:
    """Map a CSV job to the closest Position id (ignore tags). Exact name match
    first (case-insensitive), else None (shift still imports, just no position)."""
    j = (job or "").strip().lower()
    if not j:
        return None
    for p in db.query(Position).all():
        if (p.name or "").strip().lower() == j:
            return p.id
    return None


def import_historical(records: list[dict], db) -> dict:
    """Import parsed shift records. records: [{iso_date,start,end,job,store,name}].
    Returns a summary dict. Commits at the end. Caller owns the session lifecycle."""
    emps = {_norm(e.full_name): e.id for e in db.query(Employee).all() if e.full_name}
    # which stores each employee is CURRENTLY rostered at. A matched person only
    # shows as a current employee in the store where they still work; gone OR
    # moved-stores -> "no longer here [at this store]" -> struck-through name. This
    # also guarantees every imported shift lands in a row (employee row if current
    # here, else a struck name row) -- nothing is silently dropped.
    emp_stores: dict[int, set] = {}
    for a in db.query(EmployeeStoreAssignment).all():
        emp_stores.setdefault(a.employee_id, set()).add(a.store_key)
    pos_cache: dict[str, int | None] = {}

    # group by (store, week_start Sunday)
    weeks: dict[tuple, list] = {}
    for r in records:
        try:
            wk = _week_start(date.fromisoformat(r["iso_date"]))
        except Exception:
            continue
        weeks.setdefault((r.get("store") or "tomball", wk), []).append(r)

    inserted = matched = former = 0
    former_names: set[str] = set()
    skipped_manager_weeks: list[str] = []
    schedules_created = 0

    for (store, wk), recs in sorted(weeks.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        sched = db.query(Schedule).filter_by(store_key=store, week_start=wk).first()
        if sched is not None and sched.created_by != IMPORT_SENTINEL:
            # a real manager-made schedule for this week -- never clobber it
            skipped_manager_weeks.append(f"{store} {wk.isoformat()}")
            continue
        if sched is None:
            sched = Schedule(store_key=store, week_start=wk, status="published",
                             published_at=datetime.utcnow(), created_by=IMPORT_SENTINEL)
            db.add(sched)
            db.flush()
            schedules_created += 1
        else:
            # re-import: clear the prior import shifts for this week, then reinsert
            db.query(Shift).filter_by(schedule_id=sched.id).delete()

        for r in recs:
            name = r.get("name") or ""
            eid = emps.get(_norm(name))
            # "current here" only if matched AND still rostered at THIS store
            here = (eid is not None and store in emp_stores.get(eid, set()))
            job = r.get("job") or ""
            if job not in pos_cache:
                pos_cache[job] = _position_id_for_job(db, job)
            try:
                start_at = _parse_dt(r["iso_date"], r.get("start") or "")
                end_at = _parse_dt(r["iso_date"], r.get("end") or "")
            except Exception:
                continue
            if end_at <= start_at:
                end_at += timedelta(days=1)  # overnight shift guard
            db.add(Shift(
                schedule_id=sched.id,
                employee_id=(eid if here else None),
                display_name=(None if here else name),  # gone / moved-stores -> struck-through name
                position_id=pos_cache[job],
                start_at=start_at, end_at=end_at,
                break_minutes=0, status="assigned",
            ))
            inserted += 1
            if here:
                matched += 1
            else:
                former += 1
                former_names.add(name)

    db.commit()

    all_names = {(r.get("name") or "") for r in records}
    unmatched = sorted(n for n in all_names if n and _norm(n) not in emps)
    return {
        "weeks": len(weeks),
        "schedules_created": schedules_created,
        "shifts_inserted": inserted,
        "matched": matched,
        "former": former,
        "former_names": sorted(former_names),
        "unmatched_names": unmatched,
        "skipped_manager_weeks": skipped_manager_weeks,
    }
