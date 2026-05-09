"""extend drivers table with self-service auth + admin reset fields

Revision ID: 8_drivers_auth
Revises: 7_developer_chat_attachments
Create Date: 2026-05-09

Per-driver login: drivers self-sign-up from the main store picker (location +
name + email + address + phone + password). Auto-approved. Managers can reset
a driver's password to a one-time temp value or deactivate the account from
the per-store Drivers admin page. Account lockout after repeated failures.

All new columns are nullable (or have a sensible default) so existing rows
in the drivers table — which were inserted by the manager dashboard with
just (name, location) — remain valid. Those legacy rows have a NULL
password_hash and can't log in until a manager resets/sets a password.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8_drivers_auth"
down_revision: Union[str, Sequence[str], None] = "7_developer_chat_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("drivers") as batch:
        batch.add_column(sa.Column("email", sa.String(length=200), nullable=True))
        batch.add_column(sa.Column("phone", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("address", sa.String(length=300), nullable=True))
        batch.add_column(sa.Column("password_hash", sa.String(length=200), nullable=True))
        batch.add_column(sa.Column(
            "active", sa.Boolean, nullable=False, server_default=sa.text("1"),
        ))
        batch.add_column(sa.Column(
            "failed_attempts", sa.Integer, nullable=False, server_default=sa.text("0"),
        ))
        batch.add_column(sa.Column("lockout_until", sa.DateTime, nullable=True))
        batch.create_unique_constraint("uq_drivers_email", ["email"])
        batch.create_index("ix_drivers_email", ["email"])


def downgrade() -> None:
    with op.batch_alter_table("drivers") as batch:
        batch.drop_index("ix_drivers_email")
        batch.drop_constraint("uq_drivers_email", type_="unique")
        batch.drop_column("lockout_until")
        batch.drop_column("failed_attempts")
        batch.drop_column("active")
        batch.drop_column("password_hash")
        batch.drop_column("address")
        batch.drop_column("phone")
        batch.drop_column("email")
