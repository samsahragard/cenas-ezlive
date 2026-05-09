"""add developer_chat table

Revision ID: 6_developer_chat
Revises: 5_produce_price_snapshot
Create Date: 2026-05-09

Persistent chat in the Partner-only Developer area so Sam can coordinate
with multiple AI agents (this Claude, CK Claude, future ones) in one
shared thread.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6_developer_chat"
down_revision: Union[str, Sequence[str], None] = "5_produce_price_snapshot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "developer_chat",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("author", sa.String(length=60), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
    )
    op.create_index("ix_dev_chat_created_at", "developer_chat", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_dev_chat_created_at", table_name="developer_chat")
    op.drop_table("developer_chat")
