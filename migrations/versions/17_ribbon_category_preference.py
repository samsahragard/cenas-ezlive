"""ribbon_category_preferences: per-user collapse state for the universal ribbon

Revision ID: 17_ribbon_category_preference
Revises: 16_brief_feedback
Create Date: 2026-05-14

Phase 2 / Block 1 / sub-block 1B (ck) — see samai's Block 1B Ribbon
Component spec (/partner/developer/app/block-1b-ribbon-component-spec)
§5. One row per (user_id, category); absence of a row means the
category renders expanded (the default), so no backfill is needed for
existing users.

Render note: this migration is *documentation* — alembic isn't wired
on the live service; Base.metadata.create_all() in app/__init__.py
handles new tables on boot and the idempotent column backfill handles
new columns on existing ones. Keeping the migration file in lockstep
with the model so a future alembic-Pre-Deploy environment stays
correct.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "17_ribbon_category_preference"
down_revision: Union[str, Sequence[str], None] = "16_brief_feedback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ribbon_category_preferences",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("is_collapsed", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "user_id", "category", name="uq_ribbon_pref_user_category"),
    )


def downgrade() -> None:
    op.drop_table("ribbon_category_preferences")
