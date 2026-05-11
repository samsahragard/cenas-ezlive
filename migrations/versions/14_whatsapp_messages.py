"""whatsapp_messages: mirror of ock awareness.db for the Partner inbox

Revision ID: 14_whatsapp_messages
Revises: 13_users_keypad_auth
Create Date: 2026-05-11

Adds the whatsapp_messages table where the CK-side awareness daemon
POSTs new WhatsApp messages (inbound + later outbound). Drives the
Partner-only inbox at /partner/operations/whatsapp.

Schema mirrors ock's awareness.db messages table 1:1 except:
- direction column (inbound/outbound) so we can render outbound
  messages distinctly once Sam/Masood can reply through EZLive
- sent_by_user column tracks WHO sent an outbound message (e.g. 'sam'
  or 'masood') for attribution
- ingested_at tracks when EZLive first received the message (vs `ts`
  which is when ock saw it on the channel side)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "14_whatsapp_messages"
down_revision: Union[str, Sequence[str], None] = "13_users_keypad_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("external_id", sa.String(120)),
        sa.Column("ts", sa.String(40), nullable=False),
        sa.Column("chat_id", sa.String(80), nullable=False),
        sa.Column("chat_type", sa.String(20), nullable=False),
        sa.Column("chat_name", sa.String(200)),
        sa.Column("sender_id", sa.String(80), nullable=False),
        sa.Column("sender_name", sa.String(200)),
        sa.Column("body", sa.Text),
        sa.Column("media_kind", sa.String(30)),
        sa.Column("direction", sa.String(10), nullable=False, server_default="inbound"),
        sa.Column("sent_by_user", sa.String(80)),
        sa.Column("reply_to_external_id", sa.String(120)),
        sa.Column("raw_metadata", sa.Text, nullable=False, server_default="{}"),
        sa.Column("ingested_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("external_id", name="uq_whatsapp_external_id"),
    )
    op.create_index("idx_whatsapp_chat_ts", "whatsapp_messages", ["chat_id", "ts"])
    op.create_index("idx_whatsapp_ts", "whatsapp_messages", ["ts"])
    op.create_index("idx_whatsapp_sender_ts", "whatsapp_messages", ["sender_id", "ts"])


def downgrade() -> None:
    op.drop_index("idx_whatsapp_sender_ts", "whatsapp_messages")
    op.drop_index("idx_whatsapp_ts", "whatsapp_messages")
    op.drop_index("idx_whatsapp_chat_ts", "whatsapp_messages")
    op.drop_table("whatsapp_messages")
