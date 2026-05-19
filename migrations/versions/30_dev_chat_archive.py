"""developer_chat_archive table (rolling-cap archive of dev chat history)

Revision ID: 30_dev_chat_archive
Revises: 29_cena_wake_decisions
Create Date: 2026-05-19

Per Sam dev chat 2026-05-19 4:07pm: "remember max 200msgs on this chage.
the rest consistently archive." Pair with the one-time archive+wipe of the
current ~2900 messages on /sam/cena/run-archive-and-wipe-dev-chat.

Honors samai #2887 PASS-WITH-CONCERN-FLAG on the prior cleanup-dev-chat
endpoint missing the archive-before-delete pattern. samai #2980 spec:
INSERT INTO archive SELECT * FROM messages BEFORE the DELETE; archive
table is append-only / never auto-trimmed.

Schema mirrors developer_chat (author/body/created_at) with an extra
original_id column preserving the source row id after delete, plus
archived_at marking when this row landed in archive.

Render note: matches the convention of migrations 8-29 — alembic isn't
wired on the live Render service. Actual CREATE TABLE happens via the
idempotent boot-time table-backfill in app/__init__.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "30_dev_chat_archive"
down_revision: Union[str, Sequence[str], None] = "29_cena_wake_decisions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "developer_chat_archive",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("original_id", sa.Integer(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("author", sa.String(60), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index(
        "ix_developer_chat_archive_archived_at",
        "developer_chat_archive", ["archived_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_developer_chat_archive_archived_at",
                  table_name="developer_chat_archive")
    op.drop_table("developer_chat_archive")
