"""Driver payroll manager-input columns on orders

Revision ID: 41_driver_payroll_inputs
Revises: 40_dev_chat_todos
Create Date: 2026-05-28

Sam #1492/#1503 (2026-05-28): the driver pay page gets three miles columns
(E-Miles expected / D-Miles driven / V-Miles verified) plus per-order $25,
$10 tracked bonus, notes, and a $5 five-star bonus. A manager fills the
verified miles, the $10 bonus, the notes, and the 5-star from the Ez Drivers
page; the rest auto-fill from the order. These columns persist that input.

Fields (all nullable — NULL means "not yet verified", so the auto estimate
keeps showing until a manager confirms):
- pay_verified_miles: FLOAT      — V-Miles; the only field that changes pay
                                    (extra miles over 20 at $2.00/mi)
- pay_driven_miles:   FLOAT      — D-Miles; display-only, no automatic source
- pay_bonus_tracked:  BOOLEAN    — manager override of the $10 tracked bonus
- pay_five_star:      BOOLEAN    — manager 5-star toggle ($5 bonus)
- pay_notes:          VARCHAR    — free-text notes shown in the Notes column
- pay_verified_at:    TIMESTAMP  — when a manager saved payroll for this order
- pay_verified_by:    VARCHAR    — which manager saved it

Render note: same convention as migrations 8-40 — alembic isn't wired on
Render. Actual schema apply runs via the gated-absence ALTER backfill in
app/__init__.py (and create_all for fresh DBs). This file documents the
change for local `alembic upgrade head`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "41_driver_payroll_inputs"
down_revision: Union[str, Sequence[str], None] = "40_dev_chat_todos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLS = [
    ("pay_verified_miles", sa.Float()),
    ("pay_driven_miles", sa.Float()),
    ("pay_bonus_tracked", sa.Boolean()),
    ("pay_five_star", sa.Boolean()),
    ("pay_notes", sa.String(500)),
    ("pay_verified_at", sa.DateTime()),
    ("pay_verified_by", sa.String(80)),
]


def upgrade() -> None:
    for name, type_ in _COLS:
        op.add_column("orders", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLS):
        op.drop_column("orders", name)
