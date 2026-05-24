"""Sam chat TODO list — new sam_chat_todos table

Revision ID: 37_sam_chat_todos
Revises: 35_incident_type_widen
Create Date: 2026-05-23

Sam directive 2026-05-23 (#563 + the explicit "this is job number 2"
re-raise): a TODO list lives under /sam/chat. Sam writes items in for
the team to complete; Cena is required to follow up with each as the
next project. Top item = current focus. Cena cannot skip — must finish
the top before moving on to the next.

Fields are ALL Sam-filled (no auto-default for date_added per Sam's
literal "everything has to be filled out by me"):

- details:        TEXT not-null    — what the work is
- date_added:     DATE not-null    — when Sam added it (Sam-typed)
- date_completed: DATE nullable    — set by Sam when done
- position:       INT not-null     — 1-based, smaller = higher
                                      priority (top = current focus)
- status:         VARCHAR(12)      — 'active' | 'done'

Bookkeeping:
- created_at / updated_at timestamps for audit

down_revision skips aick's local 36_ezcater_order_details (which is
written on aick's box but not yet pushed). When aick lands 36, his
migration becomes 36 and this one continues as 37 cleanly — both
chain off 35.

Render note: matches the convention of migrations 8-35 — alembic
isn't wired on the live Render service. Actual schema create happens
via the idempotent boot-time backfill in app/__init__.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "37_sam_chat_todos"
down_revision: Union[str, Sequence[str], None] = "35_incident_type_widen"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sam_chat_todos",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("details", sa.Text, nullable=False),
        sa.Column("date_added", sa.Date, nullable=False),
        sa.Column("date_completed", sa.Date, nullable=True),
        sa.Column("position", sa.Integer, nullable=False, index=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
    )


def downgrade() -> None:
    op.drop_table("sam_chat_todos")
