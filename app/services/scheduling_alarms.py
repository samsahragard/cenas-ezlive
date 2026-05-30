"""Schedules V2 - Block 6: shift-alarm creation + the send worker (ckai).

Two halves:

  create_for_schedule(schedule_id)  - called by the B5 publish endpoint right
      after a schedule flips to "published" (wrapped there so it can never fail
      publish). It (re)computes the PENDING shift_alarms rows for the schedule:
      one per ASSIGNED shift x the employee's enabled channels x their alarm
      offsets (minutes_before [+ second_minutes_before]). It is a DELETE-then-
      INSERT recompute, NOT an append: it first drops the schedule's PENDING
      (unsent) alarms, then re-inserts from the current shift times. That makes
      re-publish idempotent AND correct after an edit - if a manager moves a
      shift's start_at and re-publishes, the old pending alarm (old time) is
      dropped instead of surviving to fire spuriously (aick's B6 catch). Rows
      already 'sent'/'failed' are kept as history. Open (unassigned) shifts and
      already-started shifts get no alarm. Fast: pure inserts, publish stays <2s.

  process_due_alarms(limit)  - called every minute by the CRON_TOKEN-gated
      endpoint (app/web/scheduling_cron.py). Sends every pending alarm whose
      alarm_time has arrived, marking each 'sent' (+sent_at) or 'failed'
      (+error_message). No infinite retry: a failed alarm stays failed.

SMS is a mock-log STUB until Sam confirms Twilio creds; email is a mock-log STUB
too (secondary channel, default-off). Both _send_* log and return; swap in the
real Twilio / brief_email SMTP send when each is green-lit. The send is the only
thing stubbed - the full pending->due->send->mark pipeline is real and testable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.db import SessionLocal
from app.models import (
    Employee,
    EmployeeAlarmPreference,
    Schedule,
    Shift,
    ShiftAlarm,
)

log = logging.getLogger(__name__)

# Defaults when an employee has no employee_alarm_preferences row.
_DEFAULT_SMS = True
_DEFAULT_EMAIL = False
_DEFAULT_MINUTES_BEFORE = 60


def _offsets_for(pref) -> list[int]:
    """The minute-offsets-before-start to fire at, de-duped + sorted. Default
    pref (no row) = [60]. A second_minutes_before equal to minutes_before
    collapses to one offset (the UNIQUE would reject the dup anyway)."""
    if pref is None:
        return [_DEFAULT_MINUTES_BEFORE]
    offs = {pref.minutes_before}
    if pref.second_minutes_before:
        offs.add(pref.second_minutes_before)
    return sorted(o for o in offs if o is not None and o >= 0)


def _channels_for(pref) -> list[str]:
    """Enabled channels for a pref row (or the default when none)."""
    sms = _DEFAULT_SMS if pref is None else bool(pref.sms_enabled)
    email = _DEFAULT_EMAIL if pref is None else bool(pref.email_enabled)
    chans = []
    if sms:
        chans.append("sms")
    if email:
        chans.append("email")
    return chans


def create_for_schedule(schedule_id) -> None:
    """(Re)compute pending shift_alarms for a just-published schedule. Idempotent
    + edit-safe: drops the schedule's PENDING alarms, then re-inserts from the
    current assigned-shift times x each employee's preferences. Never touches
    'sent'/'failed' rows. See module docstring."""
    db = SessionLocal()
    try:
        sched = db.query(Schedule).filter_by(id=schedule_id).first()
        if sched is None or sched.status != "published":
            return  # only published schedules carry alarms

        shifts = db.query(Shift).filter_by(schedule_id=schedule_id).all()
        shift_ids = [s.id for s in shifts]
        if not shift_ids:
            return

        # Recompute: drop stale PENDING alarms for this schedule's shifts. Keep
        # sent/failed (history). This is what makes an edited-then-republished
        # start_at safe - the old pending alarm at the old time is removed.
        (db.query(ShiftAlarm)
           .filter(ShiftAlarm.shift_id.in_(shift_ids),
                   ShiftAlarm.status == "pending")
           .delete(synchronize_session=False))

        # Preload prefs for the assigned employees (one query, not N).
        emp_ids = {s.employee_id for s in shifts if s.employee_id}
        prefs_by_emp = {}
        if emp_ids:
            for p in (db.query(EmployeeAlarmPreference)
                        .filter(EmployeeAlarmPreference.employee_id.in_(emp_ids))
                        .all()):
                prefs_by_emp[p.employee_id] = p

        now = datetime.utcnow()
        seen = set()  # (shift_id, emp_id, alarm_time, channel) guard within this pass
        # Pre-seed `seen` with the alarms that SURVIVED the delete (only sent/failed
        # remain - the delete removed pending). This (a) avoids a UNIQUE collision on a
        # re-publish-AFTER-send (the surviving sent row would collide with a reinsert at
        # the same key -> IntegrityError that the publish-wrapper swallows, silently
        # rolling back the WHOLE recompute), and (b) never recreates an already-sent
        # alarm as fresh pending (its alarm_time is now past -> the next cron would
        # double-remind). Still-pending alarms were deleted above so they recompute
        # normally; an edited start_at yields a NEW key not in `seen` -> inserts cleanly
        # while the sent row is kept as history.
        for ex in (db.query(ShiftAlarm)
                     .filter(ShiftAlarm.shift_id.in_(shift_ids),
                             ShiftAlarm.status != "pending").all()):
            seen.add((ex.shift_id, ex.employee_id, ex.alarm_time, ex.channel))
        for sh in shifts:
            if not sh.employee_id:
                continue  # open shift - nobody to remind
            if sh.start_at is None or sh.start_at <= now:
                continue  # past / already started - no reminder
            pref = prefs_by_emp.get(sh.employee_id)
            channels = _channels_for(pref)
            if not channels:
                continue  # employee opted out of every channel
            for off in _offsets_for(pref):
                at = sh.start_at - timedelta(minutes=off)
                for ch in channels:
                    key = (sh.id, sh.employee_id, at, ch)
                    if key in seen:
                        continue
                    seen.add(key)
                    db.add(ShiftAlarm(
                        shift_id=sh.id,
                        employee_id=sh.employee_id,
                        alarm_time=at,
                        channel=ch,
                        status="pending",
                        created_at=now,
                    ))
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Send worker (per-minute cron)
# --------------------------------------------------------------------------
def _build_message(emp, sh) -> str:
    """The reminder text. Portable strftime (no %-codes - this runs on Windows
    in tests + Linux in prod)."""
    first = ((emp.full_name or "").split(" ")[0] or "there") if emp else "there"
    when = sh.start_at.strftime("%a %b %d, %I:%M %p") if sh and sh.start_at else "soon"
    return (f"Hi {first}, reminder: you have a Cenas shift starting {when}. "
            f"See your schedule in the app.")


def _send_sms(to_phone, body) -> None:
    """STUB: Twilio is Sam-gated. Until creds are confirmed this only logs (a
    mock send). When creds land, send via Twilio here (env TWILIO_*). Raising
    surfaces as a 'failed' alarm with the reason - so a missing phone is a real
    failure, not a silent skip."""
    if not to_phone:
        raise ValueError("no phone on file")
    log.info("[shift-alarm][SMS-STUB] to=%s :: %s", to_phone, body)


def _send_email(to_email, subject, body) -> None:
    """STUB: secondary channel, default-off. Logs a mock send; swap in the
    brief_email SMTP_SSL path (orders@ mailbox) when email alarms are enabled."""
    if not to_email:
        raise ValueError("no email on file")
    log.info("[shift-alarm][EMAIL-STUB] to=%s subj=%s :: %s", to_email, subject, body)


def _dispatch(alarm, emp, sh) -> None:
    """Route one alarm to its channel. Raises on any failure (caught by the
    caller, which marks the alarm 'failed' + records the reason)."""
    body = _build_message(emp, sh)
    if alarm.channel == "sms":
        _send_sms(emp.phone if emp else None, body)
    elif alarm.channel == "email":
        _send_email(emp.email if emp else None, "Cenas shift reminder", body)
    else:
        raise ValueError("unknown channel %r" % (alarm.channel,))


def process_due_alarms(limit: int = 500) -> dict:
    """Send every pending alarm whose alarm_time has arrived (alarm_time <= now),
    oldest first, up to `limit`. Marks each 'sent' (+sent_at) or 'failed'
    (+error_message). Commits per-alarm so a mid-batch error never loses prior
    sends. Returns {processed, sent, failed} for the cron response."""
    db = SessionLocal()
    processed = sent = failed = 0
    try:
        now = datetime.utcnow()
        due = (db.query(ShiftAlarm)
                 .filter(ShiftAlarm.status == "pending",
                         ShiftAlarm.alarm_time <= now)
                 .order_by(ShiftAlarm.alarm_time)
                 .limit(limit)
                 .all())
        for al in due:
            processed += 1
            try:
                sh = db.query(Shift).filter_by(id=al.shift_id).first()
                emp = db.query(Employee).filter_by(id=al.employee_id).first()
                if sh is None or emp is None:
                    raise ValueError("shift or employee no longer exists")
                _dispatch(al, emp, sh)
                al.status = "sent"
                al.sent_at = datetime.utcnow()
                al.error_message = None
                sent += 1
            except Exception as e:  # noqa: BLE001 - a bad alarm must not stall the batch
                al.status = "failed"
                al.error_message = (str(e) or e.__class__.__name__)[:500]
                failed += 1
            db.commit()
        if processed:
            log.info("[shift-alarm] cron processed=%d sent=%d failed=%d",
                     processed, sent, failed)
        return {"processed": processed, "sent": sent, "failed": failed}
    finally:
        db.close()
