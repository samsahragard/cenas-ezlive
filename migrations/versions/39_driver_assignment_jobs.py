"""driver_assignment_jobs table (Sam #669 driver-assignment build).

Revision ID: 39_driver_assignment_jobs
Revises: 38_ezcater_order_details
Create Date: 2026-05-23

(renumbered from 37 → 39 because ck's 37_sam_chat_todos landed on
origin/main first; 38 is now ezcater_order_details, 39 is this one.)

Tracks in-flight + completed driver re-assignment jobs spawned by the
catering dashboard's per-order driver dropdown. Each job represents one
end-to-end Selenium flow on aick (or ck via failover) that unhooks an
order's current ezCater-auto-assigned driver and assigns a real one
picked by the manager.

Per Sam's amendment (post-#669): verification is done by DOM re-read on
the order detail page, NOT by PDF parse — so this table does NOT carry
a verification_pdf_path column. The separate PDF-archive flow (nightly
cron) is unchanged and unrelated.

Render note: matches the convention of migrations 8-36 — alembic isn't
wired on the live Render service. Actual schema change happens via the
idempotent boot-time backfill in app/__init__.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "39_driver_assignment_jobs"
down_revision: Union[str, Sequence[str], None] = "38_ezcater_order_details"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "driver_assignment_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        # UUID4 string — the public job_id the frontend polls on. Kept
        # separate from the integer PK so we never leak row counts.
        sa.Column("job_id", sa.String(40), nullable=False,
                  unique=True, index=True),

        # ezCater external_order_id (e.g. UK2-1EW). Indexed because the
        # concurrency guard ("reject duplicate jobs within 5s for the
        # same order") queries by this column.
        sa.Column("order_id", sa.String(50), nullable=False, index=True),

        # Strings, not FKs — drivers can be CK#1 / Sam #2 / Masood etc.
        # which we don't always have in our local driver tables.
        sa.Column("current_driver", sa.String(160), nullable=True),
        sa.Column("new_driver", sa.String(160), nullable=False),

        # pending -> running -> completed | failed
        sa.Column("status", sa.String(16), nullable=False,
                  server_default=sa.text("'pending'"), index=True),
        sa.Column("error_message", sa.Text(), nullable=True),

        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),

        # 'aick' when run locally; 'ck' (or the cena2 gateway hostname)
        # after a failover hop. Lets us see in dev chat how often the
        # primary side is needing rescue.
        sa.Column("gateway_processed", sa.String(40), nullable=True),

        # 0 / 1 / 2 — number of fresh-context restarts attempted before
        # the eventual success or final failure. Sam's spec caps at 2.
        sa.Column("retry_count", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),

        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index(
        "ix_driver_assignment_jobs_order_started",
        "driver_assignment_jobs", ["order_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_driver_assignment_jobs_order_started",
                  table_name="driver_assignment_jobs")
    op.drop_table("driver_assignment_jobs")
