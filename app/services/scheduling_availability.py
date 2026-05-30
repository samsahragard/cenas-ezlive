"""Schedules V2 - availability soft-warning hook (Block 8).

NO-OP STUB at B4 (per ckai #1864): B4 shift create/update call warning() for a
SOFT advisory (it must NEVER block the save). ckai fills the real body + owns the
availability table when he builds B8.
"""
from __future__ import annotations


def warning(employee_id, at_dt) -> str | None:
    """Return a soft-warning string if the shift at at_dt falls outside the
    employee's set availability, else None. NEVER blocks the save - advisory only.
    B4 stub: always None (no B8 availability data exists yet)."""
    return None
