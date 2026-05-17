"""dev_chat_attribution_corrections: sidecar for author re-attribution

Revision ID: 26_dev_chat_attribution_corrections
Revises: 25_sam_chat_cache_tokens
Create Date: 2026-05-17

Per Sam #2031 item 3 + samai #2024 sidecar plan, response to the
2026-05-17 author-attribution incident: 4 ck-authored work reports
(#2051, #2056, #2097, #2098) landed in developer_chat under
author='sam' because the POST handler at developer_chat.py:119
defaulted a missing form field to the literal "sam". The 4c25f3b
commit changed the default to "unknown" (bleeding stop); this
table is the audit-correction store for the rows already in the
DB.

Sidecar over in-place UPDATE: mutating dev chat author is
irreversible if a correction is wrong; the sidecar preserves the
original row and overlays the correction so the audit trail can
show "displayed as X, actually authored by Y" transparently.

Render note: matches the convention of migrations 8-25 — alembic
isn't wired on the live Render service. The actual CREATE TABLE
happens via the idempotent boot-time table-backfill in
app/__init__.py (table-absence-gated metadata.create_all subset),
which runs on every boot and is a no-op once the table exists.
The 4 initial correction rows are seeded by a separate boot-time
data backfill, also idempotent (gated on message_id absence).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "26_dev_chat_attribution_corrections"
down_revision: Union[str, Sequence[str], None] = "25_sam_chat_cache_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dev_chat_attribution_corrections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_id", sa.Integer(),
                  sa.ForeignKey("developer_chat.id", ondelete="CASCADE"),
                  nullable=False, unique=True, index=True),
        sa.Column("original_author", sa.String(60), nullable=False),
        sa.Column("corrected_author", sa.String(60), nullable=False),
        sa.Column("correction_reason", sa.Text(), nullable=False),
        sa.Column("corrected_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("corrected_by", sa.String(60), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("dev_chat_attribution_corrections")
