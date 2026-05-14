"""scheduled_events: catering + event calendar — the 1C ribbon source

Revision ID: 18_scheduled_event
Revises: 17_ribbon_category_preference
Create Date: 2026-05-14

Phase 2 / Block 1 precondition (samai spec, Sam 1C §13 Path A
2026-05-14) — see /partner/developer/app/block-1-precond-scheduled-event-spec.
Ships the ScheduledEvent model + this table only, so 1C's Caterings
(ribbon category 2) and Events (category 3) adapters have a real model
to read instead of an undefined dependency. The table ships empty;
Block 2's admin form fills it.

Not an audit log — ScheduledEvent rows are mutable operational records
(an event gets confirmed, rescheduled, cancelled), so there is
deliberately no append-only constraint.

Render note: this migration is *documentation* — alembic isn't wired
on the live service; Base.metadata.create_all() in app/__init__.py
handles new tables on boot. Keeping the migration file in lockstep
with the model so a future alembic-Pre-Deploy environment stays
correct.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "18_scheduled_event"
down_revision: Union[str, Sequence[str], None] = "17_ribbon_category_preference"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduled_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("store", sa.String(20), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("scheduled_at", sa.DateTime, nullable=False),
        sa.Column("scheduled_end_at", sa.DateTime, nullable=True),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="scheduled"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index(
        "ix_scheduled_events_ribbon", "scheduled_events",
        ["store", "status", "scheduled_at"])
    # scheduled_at also gets a standalone index (the model declares
    # index=True on the column) — the 1C "upcoming events" query sorts
    # on it independently of the composite.
    op.create_index(
        "ix_scheduled_events_scheduled_at", "scheduled_events",
        ["scheduled_at"])


def downgrade() -> None:
    op.drop_index("ix_scheduled_events_scheduled_at",
                  table_name="scheduled_events")
    op.drop_index("ix_scheduled_events_ribbon",
                  table_name="scheduled_events")
    op.drop_table("scheduled_events")
