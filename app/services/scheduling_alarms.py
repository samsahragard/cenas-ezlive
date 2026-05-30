"""Schedules V2 - shift-alarm creation hook (Block 6).

NO-OP STUB at B5 (per ckai #1912): the B5 publish endpoint calls
create_for_schedule(schedule_id) right AFTER it flips a schedule to "published",
so the B6 integration point is wired in now (no retrofit). ckai fills the body +
owns the shift_alarms table in B6 - it INSERTs pending alarm rows (per assigned
shift x each employee's alarm preferences). Keep it fast: publish must stay <2s;
the per-minute cron does the actual SMS/email send.
"""
from __future__ import annotations


def create_for_schedule(schedule_id) -> None:
    """Insert pending shift_alarms rows for a just-published schedule. B5 stub:
    no-op (no B6 alarm tables exist yet)."""
    return None
