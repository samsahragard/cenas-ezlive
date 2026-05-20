"""Incident Reports v4 fields — discrete date/time/location/people/action + lock

Revision ID: 34_incident_reports_v4_fields
Revises: 33_incident_reports_v3
Create Date: 2026-05-20

ck build-order (Sam dev chat #4:22 + #4:23 spec 2026-05-20 — convert
the new-incident form from the v3 simple-form shell to the rich v4
"File new incident" page: discrete what/when/where/who fields plus
a lock-on-submit flag that freezes the row into an immutable Original
Record).

Additive columns on the existing manager_incident_report table:

- date_of_incident:    DATE       NULL
- time_of_incident:    TIME       NULL
- location_in_store:   VARCHAR(200) NULL
- people_involved:     TEXT       NULL
- witnesses:           TEXT       NULL
- immediate_action:    TEXT       NULL
- locked:              BOOLEAN    NOT NULL DEFAULT FALSE
- locked_at:           DATETIME   NULL

All additive: existing v3 rows stay valid (defaults apply to locked).

Render note: matches the convention of migrations 8-33 — alembic isn't
wired on the live Render service. Actual schema change happens via the
idempotent boot-time backfill in app/__init__.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "34_incident_reports_v4_fields"
down_revision: Union[str, Sequence[str], None] = "33_incident_reports_v3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("manager_incident_report",
                  sa.Column("date_of_incident", sa.Date(), nullable=True))
    op.add_column("manager_incident_report",
                  sa.Column("time_of_incident", sa.Time(), nullable=True))
    op.add_column("manager_incident_report",
                  sa.Column("location_in_store", sa.String(200), nullable=True))
    op.add_column("manager_incident_report",
                  sa.Column("people_involved", sa.Text(), nullable=True))
    op.add_column("manager_incident_report",
                  sa.Column("witnesses", sa.Text(), nullable=True))
    op.add_column("manager_incident_report",
                  sa.Column("immediate_action", sa.Text(), nullable=True))
    op.add_column("manager_incident_report",
                  sa.Column("locked", sa.Boolean(), nullable=False,
                            server_default=sa.false()))
    op.add_column("manager_incident_report",
                  sa.Column("locked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("manager_incident_report", "locked_at")
    op.drop_column("manager_incident_report", "locked")
    op.drop_column("manager_incident_report", "immediate_action")
    op.drop_column("manager_incident_report", "witnesses")
    op.drop_column("manager_incident_report", "people_involved")
    op.drop_column("manager_incident_report", "location_in_store")
    op.drop_column("manager_incident_report", "time_of_incident")
    op.drop_column("manager_incident_report", "date_of_incident")
