"""sample_approvals + sample_approval_attachments tables (Samples-page approval workflow)

Revision ID: 28_sample_approvals
Revises: 27_drop_whatsapp_messages
Create Date: 2026-05-17

Per cena #2549 item 2 + dck spec 68c5248
(spec_samples_approval_workflow.html §2.1 + §2.2). Two tables, one
parent + one child cascading. Sam toggles approval state on each
sample card; correction notes + image attachments allowed.

Render note: matches the convention of migrations 8-27 — alembic
isn't wired on the live Render service. Actual CREATE TABLE happens
via the idempotent boot-time table-backfill in app/__init__.py
(table-presence-gated metadata.create_all subset).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "28_sample_approvals"
down_revision: Union[str, Sequence[str], None] = "27_drop_whatsapp_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sample_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sample_slug", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("marked_by_user_id", sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_table(
        "sample_approval_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sample_approval_id", sa.Integer(),
                  sa.ForeignKey("sample_approvals.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("sample_approval_attachments")
    op.drop_table("sample_approvals")
