"""add developer_chat_attachment table

Revision ID: 7_developer_chat_attachments
Revises: 6_developer_chat
Create Date: 2026-05-09

Files attached to a Developer Chat message. Up to 5 per message, enforced
at the route layer. Files live under CHAT_ATTACHMENTS_DIR (default
/var/data/chat-attachments) at <message_id>/<safe_filename>; the row
stores filename, mime, size, storage_path, and an is_image flag for
inline thumbnail rendering.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7_developer_chat_attachments"
down_revision: Union[str, Sequence[str], None] = "6_developer_chat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "developer_chat_attachment",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "message_id",
            sa.Integer,
            sa.ForeignKey("developer_chat.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=True),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("storage_path", sa.String(length=500), nullable=False),
        sa.Column("is_image", sa.Boolean, nullable=False, server_default=sa.text("0")),
    )
    op.create_index(
        "ix_dev_chat_attachment_message_id",
        "developer_chat_attachment",
        ["message_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dev_chat_attachment_message_id",
        table_name="developer_chat_attachment",
    )
    op.drop_table("developer_chat_attachment")
