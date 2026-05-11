"""Site-wide User table for the keypad-auth rewrite.

Revision ID: 13_users_keypad_auth
Revises: 12_ezcater_live_tracking
Create Date: 2026-05-11

Sam (2026-05-11): replace the shared-password Tier 1 ('cenas') + Tier 2
(Partner password) gates with per-person 5-digit numeric passcode login.
Roles: partner > corporate > gm > manager > expo > corporate-driver.
First-login forces a passcode change. Sam seeded as partner with passcode
12345 by the boot block in app/__init__.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "13_users_keypad_auth"
down_revision: Union[str, Sequence[str], None] = "12_ezcater_live_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("full_name", sa.String(150), nullable=False),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("passcode_hash", sa.String(255), nullable=False),
        sa.Column("permission_level", sa.String(30), nullable=False, server_default="manager"),
        sa.Column("store_scope", sa.String(20), nullable=True),
        sa.Column("first_login_done", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("failed_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("lockout_until", sa.DateTime, nullable=True),
        sa.Column("last_login_at", sa.DateTime, nullable=True),
        sa.Column("last_login_ip", sa.String(64), nullable=True),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("phone", name="uq_users_phone"),
    )


def downgrade() -> None:
    op.drop_table("users")
