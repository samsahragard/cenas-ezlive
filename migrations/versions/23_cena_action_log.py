"""cena_action_log: CenaActionLog

Revision ID: 23_cena_action_log
Revises: 22_ambient_signals
Create Date: 2026-05-15

Phase 2 / Cena (PART 4 of Sam's 2026-05-15 directive). One row per
tool invocation Cena makes through the gateway (cena_gateway.py on
AiCk). /sam/cena-audit/ renders this table reverse-chronologically
for review.

Fields follow Sam's spec (id, action_type, parameters, result,
timestamp, cena_session_id), with operational additions: started_at
+ finished_at (latency), success + error_text (failure triage),
message_id (which user turn drove the action).

Render note: this migration is *documentation* — alembic isn't
wired on the live service; Base.metadata.create_all() in
app/__init__.py handles new tables on boot. Kept in lockstep with
the model.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "23_cena_action_log"
down_revision: Union[str, Sequence[str], None] = "22_ambient_signals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cena_action_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("parameters", sa.JSON, nullable=False),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("success", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("error_text", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column(
            "session_id",
            sa.Integer,
            sa.ForeignKey("sam_chat_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            sa.Integer,
            sa.ForeignKey("sam_chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_cena_action_logs_action_type",
        "cena_action_logs",
        ["action_type"],
    )
    op.create_index(
        "ix_cena_action_logs_success",
        "cena_action_logs",
        ["success"],
    )
    op.create_index(
        "ix_cena_action_logs_started_at",
        "cena_action_logs",
        ["started_at"],
    )
    op.create_index(
        "ix_cena_action_logs_session_id",
        "cena_action_logs",
        ["session_id"],
    )
    op.create_index(
        "ix_cena_action_logs_message_id",
        "cena_action_logs",
        ["message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_cena_action_logs_message_id", table_name="cena_action_logs")
    op.drop_index("ix_cena_action_logs_session_id", table_name="cena_action_logs")
    op.drop_index("ix_cena_action_logs_started_at", table_name="cena_action_logs")
    op.drop_index("ix_cena_action_logs_success", table_name="cena_action_logs")
    op.drop_index("ix_cena_action_logs_action_type", table_name="cena_action_logs")
    op.drop_table("cena_action_logs")
