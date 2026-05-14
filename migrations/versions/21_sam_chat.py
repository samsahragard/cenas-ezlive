"""sam_chat: SamChatSession + SamChatMessage

Revision ID: 21_sam_chat
Revises: 20_sales_insights
Create Date: 2026-05-14

Sam Chat (standalone, Sam request 2026-05-14) — a dedicated /sam/chat
surface for Sam (partner) to converse with Claude directly via the
Anthropic API. Deliberately ISOLATED from the agentic pipeline: no FK
to User (the route is hard-gated to SAM_CHAT_USER_ID), no references
to AgentChatMessage / AgentActionLog / any Phase 2 Block 3 table.

Two new tables:
  - sam_chat_sessions: one conversation thread. Mutable operational
    record (title editable, is_archived) — NOT an audit log.
  - sam_chat_messages: one message per row (user / assistant /
    system). session_id ondelete=CASCADE. cost_* columns populated on
    assistant rows from the Anthropic usage block.

Render note: this migration is *documentation* — alembic isn't wired
on the live service; Base.metadata.create_all() in app/__init__.py
handles new tables on boot. Kept in lockstep with the models so a
future alembic Pre-Deploy environment stays correct.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "21_sam_chat"
down_revision: Union[str, Sequence[str], None] = "20_sales_insights"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sam_chat_sessions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("last_message_at", sa.DateTime, nullable=False),
        sa.Column("title", sa.String(120), nullable=True),
        sa.Column("is_archived", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_sam_chat_sessions_last_message_at",
                    "sam_chat_sessions", ["last_message_at"])

    op.create_table(
        "sam_chat_messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Integer,
                  sa.ForeignKey("sam_chat_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("role", sa.String(12), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("model", sa.String(40), nullable=True),
        sa.Column("cost_input_tokens", sa.Integer, nullable=True),
        sa.Column("cost_output_tokens", sa.Integer, nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_sam_chat_messages_session_id",
                    "sam_chat_messages", ["session_id"])
    op.create_index("ix_sam_chat_messages_created_at",
                    "sam_chat_messages", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_sam_chat_messages_created_at",
                  table_name="sam_chat_messages")
    op.drop_index("ix_sam_chat_messages_session_id",
                  table_name="sam_chat_messages")
    op.drop_table("sam_chat_messages")
    op.drop_index("ix_sam_chat_sessions_last_message_at",
                  table_name="sam_chat_sessions")
    op.drop_table("sam_chat_sessions")
