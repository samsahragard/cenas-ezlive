"""brief_feedback: calibration-panel feedback table for morning briefs

Revision ID: 16_brief_feedback
Revises: 15_driver_system
Create Date: 2026-05-13

C1 of the Phase 1 calibration-panel work — see Sam's directive 2026-05-13
20:13 and samai's consolidated design in chat. The table captures panel
member responses to [Calibration] morning briefs:

  - Round 1 (active): samai/aick reads the panel-member's reply email
    and INSERTs a row with submitted_via='email_reply', submitted_at set.
  - Round 2 (deferred until form endpoint lands in C2):
    submitted_via='form', row INSERTed when feedback link clicked
    (submitted_at=NULL), submitted_at updated when form POSTed.

Latency = submitted_at − morning_briefs.composed_at is a real signal per
Sam 20:47 — indexes on FK + timestamps support that join query cheaply.

Render note: this migration is *documentation* — alembic isn't wired
on the live service; Base.metadata.create_all() handles new tables and
the boot-time backfill in app/__init__.py handles new columns on
existing ones. Keeping the migration file in lockstep with the model so
a future alembic-Pre-Deploy environment stays correct.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "16_brief_feedback"
down_revision: Union[str, Sequence[str], None] = "15_driver_system"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "brief_feedback",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "morning_brief_id", sa.Integer,
            sa.ForeignKey("morning_briefs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("useful_score", sa.Integer, nullable=True),
        sa.Column("missed_something", sa.Text, nullable=True),
        sa.Column("was_noise", sa.Text, nullable=True),
        sa.Column("single_change", sa.Text, nullable=True),
        sa.Column("submitted_via", sa.String(20), nullable=False),
        sa.Column("submitted_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index(
        "ix_brief_feedback_brief", "brief_feedback", ["morning_brief_id"])
    op.create_index(
        "ix_brief_feedback_user", "brief_feedback", ["user_id"])
    op.create_index(
        "ix_brief_feedback_submitted_at", "brief_feedback", ["submitted_at"])
    op.create_index(
        "ix_brief_feedback_created_at", "brief_feedback", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_brief_feedback_created_at", table_name="brief_feedback")
    op.drop_index("ix_brief_feedback_submitted_at", table_name="brief_feedback")
    op.drop_index("ix_brief_feedback_user", table_name="brief_feedback")
    op.drop_index("ix_brief_feedback_brief", table_name="brief_feedback")
    op.drop_table("brief_feedback")
