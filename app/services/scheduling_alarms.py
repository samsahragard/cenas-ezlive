"""Schedules V2 - Block 6: shift-alarm creation + the send worker (ckai).

Two halves:

  create_for_schedule(schedule_id)  - called by the B5 publish endpoint right
      after a schedule flips to "published" (wrapped there so it can never fail
      publish). DELETE-then-reinsert recompute of the PENDING shift_alarms (one
      per ASSIGNED shift x channel x offset); keeps sent/failed as history;
      pre-seeds the dedup set with surviving sent/failed rows so a re-publish-
      after-send can't collide on the UNIQUE (B6 finding #1). Fast: pure inserts.

  process_due_alarms(limit)  - the per-minute CRON_TOKEN-gated send worker. Sends
      every pending alarm whose alarm_time has arrived, marking each sent/failed.
      CONCURRENCY-SAFE: each alarm is claimed by a compare-and-swap on its status,
      so the Render cron + the in-process ticker can never double-send one. No
      infinite retry: a failed alarm stays failed.

SMS send is CREDS-GATED Twilio - a real SMS when TWILIO_ACCOUNT_SID/AUTH_TOKEN/
FROM_NUMBER are set on the web service, otherwise a mock-log stub (the safe
default until Sam provisions Twilio; secret VALUES live only in the env, never in
code or chat). Email stays a mock-log stub (secondary channel, default-off).
Reminder times render in store-local time (America/Chicago), not UTC.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import (
    Employee,
    EmployeeAlarmPreference,
    Schedule,
    Shift,
    ShiftAlarm,
)

log = logging.getLogger(__name__)

# Reminder text renders in store-local time (Cenas = Houston / US Central): naive
# UTC start_at -> America/Chicago. Falls back to UTC if tzdata is unavailable
# (e.g. a bare Windows test box without the tzdata package).
try:
    from zoneinfo import ZoneInfo
    _STORE_TZ = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover
    _STORE_TZ = None

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
        # remain). Avoids a UNIQUE collision on a re-publish-AFTER-send + never
        # recreates an already-sent alarm as fresh pending (B6 finding #1).
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
    """The reminder text. start_at is naive UTC -> render in store-local time
    (America/Chicago) so the employee sees their actual shift time, not UTC.
    Portable strftime (no %-codes); UTC fallback if tzdata is absent."""
    first = ((emp.full_name or "").split(" ")[0] or "there") if emp else "there"
    if sh and sh.start_at:
        if _STORE_TZ is not None:
            local = sh.start_at.replace(tzinfo=timezone.utc).astimezone(_STORE_TZ)
            when = local.strftime("%a %b %d, %I:%M %p")
        else:
            when = sh.start_at.strftime("%a %b %d, %I:%M %p") + " UTC"
    else:
        when = "soon"
    return (f"Hi {first}, reminder: you have a Cenas shift starting {when}. "
            f"See your schedule in the app.")


def _to_e164(phone) -> str:
    """A US 10/11-digit number -> +1XXXXXXXXXX (Twilio needs E.164)."""
    s = str(phone).strip()
    if s.startswith("+"):
        return s
    return "+1" + "".join(c for c in s if c.isdigit())[-10:]


def _send_sms(to_phone, body) -> None:
    """CREDS-GATED Twilio send. With TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM_NUMBER all
    set in the env, send a real SMS; otherwise log a mock-stub (the safe default
    until Sam provisions Twilio - secret VALUES live only in the web-service env,
    never in code or chat). Raising surfaces as a 'failed' alarm with the reason
    (a missing phone is a real failure, not a silent skip)."""
    if not to_phone:
        raise ValueError("no phone on file")
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_num = os.getenv("TWILIO_FROM_NUMBER")
    msid = os.getenv("TWILIO_MESSAGING_SERVICE_SID")  # prod-friendly alt to a From number
    if not (sid and token and (from_num or msid)):
        log.info("[shift-alarm][SMS-STUB] to=%s :: %s", to_phone, body)
        return
    _twilio_send(sid, token, from_num, msid, to_phone, body)


def _twilio_send(sid, token, from_num, msid, to_phone, body) -> None:
    """Raw HTTPS POST to the Twilio Messages API (no extra dependency). Uses a
    Messaging Service SID if set (prod-friendly: pooled numbers, compliance), else
    a From number. Raises on a non-2xx so the caller marks the alarm 'failed'."""
    import base64
    import urllib.error
    import urllib.parse
    import urllib.request

    params = {"To": _to_e164(to_phone), "Body": body}
    if msid:
        params["MessagingServiceSid"] = msid
    else:
        params["From"] = from_num
    url = "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % sid
    data = urllib.parse.urlencode(params).encode()
    auth = base64.b64encode(("%s:%s" % (sid, token)).encode()).decode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": "Basic " + auth,
                 "User-Agent": "cenas-shift-alarms/1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status >= 300:
                raise RuntimeError("twilio HTTP %s" % r.status)
    except urllib.error.HTTPError as e:  # surface Twilio's error body (truncated)
        detail = e.read()[:200].decode("utf-8", "replace") if hasattr(e, "read") else ""
        raise RuntimeError("twilio HTTP %s: %s" % (e.code, detail))


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
    oldest first, up to `limit`. Returns {processed, sent, failed}.

    CONCURRENCY-SAFE claim: each alarm is grabbed by a compare-and-swap UPDATE
    (status pending -> sent) guarded on status='pending'; only ONE runner wins a
    row, so the Render cron + the in-process ticker can't both send it. We mark
    'sent' optimistically as the claim, then send; if the send throws we correct
    the row to 'failed'. A crash between claim and send => a MISSED reminder
    (under-send), the safe failure mode vs a duplicate text. No infinite retry:
    once 'sent'/'failed' a row is never re-selected."""
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
            won = (db.query(ShiftAlarm)
                     .filter(ShiftAlarm.id == al.id, ShiftAlarm.status == "pending")
                     .update({"status": "sent", "sent_at": datetime.utcnow(),
                              "error_message": None}, synchronize_session=False))
            db.commit()
            if not won:
                continue  # another runner already claimed this alarm
            processed += 1
            try:
                sh = db.query(Shift).filter_by(id=al.shift_id).first()
                emp = db.query(Employee).filter_by(id=al.employee_id).first()
                if sh is None or emp is None:
                    raise ValueError("shift or employee no longer exists")
                _dispatch(al, emp, sh)
                sent += 1
            except Exception as e:  # noqa: BLE001 - a bad alarm must not stall the batch
                (db.query(ShiftAlarm).filter_by(id=al.id)
                   .update({"status": "failed", "sent_at": None,
                            "error_message": (str(e) or e.__class__.__name__)[:500]},
                           synchronize_session=False))
                db.commit()
                failed += 1
        if processed:
            log.info("[shift-alarm] cron processed=%d sent=%d failed=%d",
                     processed, sent, failed)
        return {"processed": processed, "sent": sent, "failed": failed}
    finally:
        db.close()
