"""Dev chat TODO list — new dev_chat_todos table

Revision ID: 40_dev_chat_todos
Revises: 39_driver_assignment_jobs
Create Date: 2026-05-26

Sam directive 2026-05-26 (#1066): a todo list lives under
/partner/developer/chat. Sam adds items + the assigned agent (aick/ck/
cena, or any when unassigned) picks them up. Distinct from
sam_chat_todos which is the cena-page single-focus queue.

Fields:
- title:        VARCHAR(500) not-null — the work item
- body:         TEXT nullable         — optional details / notes
- assigned_to: VARCHAR(40) nullable   — 'aick' | 'ck' | 'cena' | NULL
- status:       VARCHAR(16) not-null  — 'open' | 'in_progress' | 'done'
                                         | 'cancelled'
- created_by:  VARCHAR(80) nullable   — author label (usually 'sam')

Bookkeeping: created_at / updated_at / completed_at.

Render note: same convention as migrations 8-39 — alembic isn't wired
on Render. Actual schema create runs via boot-time create_all in
app/__init__.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "40_dev_chat_todos"
down_revision: Union[str, Sequence[str], None] = "39_driver_assignment_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dev_chat_todos",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text, nullable=True),
        sa.Column("assigned_to", sa.String(40), nullable=True, index=True),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="open", index=True),
        sa.Column("created_by", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, index=True,
                  server_default=sa.func.current_timestamp()),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
    )


def downgrade() -> None:
    op.drop_table("dev_chat_todos")
