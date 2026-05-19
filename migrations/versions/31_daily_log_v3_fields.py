"""Daily Manager Log v3 fields + entry-image table

Revision ID: 31_daily_log_v3_fields
Revises: 30_dev_chat_archive
Create Date: 2026-05-19

dck build-order #2 (2026-05-19): the Daily Manager Log v3 design needs
a richer structured entry than the shared ManagerLogMixin shape. Adds
6 discrete columns to manager_daily_log (module / subject / issue /
priority / entry_date / show_on_roster) — real columns instead of a
type_tag composite, which avoids the samai #3.49 'STAFF:URGENT'
composite-display bug. Plus daily_log_entry_image for the modal image
upload + detail-pane gallery.

All additive: new columns carry server defaults so existing rows stay
valid; the image table is brand new. No data transform, no drops.

Render note: matches the convention of migrations 8-30 — alembic isn't
wired on the live Render service. Actual schema change happens via the
idempotent boot-time backfill in app/__init__.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "31_daily_log_v3_fields"
down_revision: Union[str, Sequence[str], None] = "30_dev_chat_archive"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("manager_daily_log",
                  sa.Column("module", sa.String(20), nullable=False,
                            server_default="general"))
    op.add_column("manager_daily_log",
                  sa.Column("subject", sa.String(24), nullable=False,
                            server_default="general"))
    op.add_column("manager_daily_log",
                  sa.Column("issue", sa.String(16), nullable=False,
                            server_default="general"))
    op.add_column("manager_daily_log",
                  sa.Column("priority", sa.String(10), nullable=False,
                            server_default="low"))
    op.add_column("manager_daily_log",
                  sa.Column("entry_date", sa.Date(), nullable=False,
                            server_default=sa.text("CURRENT_DATE")))
    op.add_column("manager_daily_log",
                  sa.Column("show_on_roster", sa.Boolean(), nullable=False,
                            server_default=sa.text("0")))

    op.create_table(
        "daily_log_entry_image",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entry_id", sa.Integer(),
                  sa.ForeignKey("manager_daily_log.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("daily_log_entry_image")
    for col in ("show_on_roster", "entry_date", "priority",
                "issue", "subject", "module"):
        op.drop_column("manager_daily_log", col)
