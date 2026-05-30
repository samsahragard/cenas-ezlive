"""Schedules V2 - time-off conflict hook (Block 7).

NO-OP STUB at B4 (per ckai #1864): B4 shift create/update call conflict() so the
B7 integration point is wired in now (no retrofit). ckai fills the real body +
owns the time_off_requests table when he builds B7.
"""
from __future__ import annotations


def conflict(employee_id, on_date) -> str | None:
    """Return a human-readable blocker string (caller -> HTTP 409) if the employee
    has APPROVED time off covering on_date, else None. B4 stub: always None
    (no B7 time-off data exists yet)."""
    return None
