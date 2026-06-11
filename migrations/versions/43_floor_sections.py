"""Sections / Floor feature tables (docs/floor_contract.md section 3)

Revision ID: 43_floor_sections
Revises: 42_ezcater_route_history
Create Date: 2026-06-11

HISTORY PARITY ONLY: alembic is not wired on Render. The real schema apply
is app.floor_models.ensure_floor_tables(engine), called at floor_routes
import time (boot-time checkfirst create_all). This file exists so the
alembic chain matches the live schema.

10 tables: toast_tables + toast_service_areas (Toast config mirrors,
SOFT-delete only), floor_sync_state (SA-1 incremental high-water mark),
floor_layouts + floor_fixtures (canvas), sections + section_tables (shift
assignments), seatings, reservations, waitlist.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "43_floor_sections"
down_revision: Union[str, Sequence[str], None] = "42_ezcater_route_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "toast_tables",
        sa.Column("guid", sa.String(length=36), nullable=False),
        sa.Column("location_guid", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("service_area_guid", sa.String(length=36), nullable=True),
        sa.Column("revenue_center_guid", sa.String(length=36), nullable=True),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("last_synced", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("guid"),
    )
    op.create_index("ix_toast_tables_location_guid", "toast_tables", ["location_guid"])
    op.create_index("ix_toast_tables_loc_deleted", "toast_tables", ["location_guid", "deleted"])

    op.create_table(
        "toast_service_areas",
        sa.Column("guid", sa.String(length=36), nullable=False),
        sa.Column("location_guid", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("last_synced", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("guid"),
    )
    op.create_index("ix_toast_service_areas_location_guid", "toast_service_areas", ["location_guid"])

    op.create_table(
        "floor_sync_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("location_guid", sa.String(length=36), nullable=False),
        sa.Column("resource", sa.String(length=40), nullable=False),
        sa.Column("last_modified", sa.String(length=64), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("location_guid", "resource", name="uq_floor_sync_loc_resource"),
    )

    op.create_table(
        "floor_layouts",
        sa.Column("location_guid", sa.String(length=36), nullable=False),
        sa.Column("table_guid", sa.String(length=36), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("w", sa.Float(), nullable=False),
        sa.Column("h", sa.Float(), nullable=False),
        sa.Column("shape", sa.String(length=10), nullable=False),
        sa.Column("rotation", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("location_guid", "table_guid"),
    )

    op.create_table(
        "floor_fixtures",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("location_guid", sa.String(length=36), nullable=False),
        sa.Column("type", sa.String(length=10), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("w", sa.Float(), nullable=False),
        sa.Column("h", sa.Float(), nullable=False),
        sa.Column("rotation", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=60), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_floor_fixtures_location_guid", "floor_fixtures", ["location_guid"])

    op.create_table(
        "sections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("location_guid", sa.String(length=36), nullable=False),
        sa.Column("shift_date", sa.Date(), nullable=False),
        sa.Column("server_employee_guid", sa.String(length=36), nullable=False),
        sa.Column("color", sa.String(length=10), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("location_guid", "shift_date", "server_employee_guid",
                            name="uq_sections_loc_date_server"),
    )
    op.create_index("ix_sections_location_guid", "sections", ["location_guid"])
    op.create_index("ix_sections_shift_date", "sections", ["shift_date"])

    op.create_table(
        "section_tables",
        sa.Column("section_id", sa.Integer(), nullable=False),
        sa.Column("table_guid", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
        sa.PrimaryKeyConstraint("section_id", "table_guid"),
    )

    op.create_table(
        "seatings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("location_guid", sa.String(length=36), nullable=False),
        sa.Column("table_guid", sa.String(length=36), nullable=False),
        sa.Column("party_size", sa.Integer(), nullable=False),
        sa.Column("seated_at", sa.DateTime(), nullable=False),
        sa.Column("seated_by", sa.String(length=80), nullable=False),
        sa.Column("server_employee_guid_at_seat", sa.String(length=36), nullable=True),
        sa.Column("cleared_at", sa.DateTime(), nullable=True),
        sa.Column("reservation_id", sa.Integer(), nullable=True),
        sa.Column("waitlist_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_seatings_location_guid", "seatings", ["location_guid"])
    op.create_index("ix_seatings_table_guid", "seatings", ["table_guid"])
    op.create_index("ix_seatings_loc_open", "seatings", ["location_guid", "cleared_at"])
    op.create_index("ix_seatings_loc_seated_at", "seatings", ["location_guid", "seated_at"])

    op.create_table(
        "reservations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("location_guid", sa.String(length=36), nullable=False),
        sa.Column("guest_name", sa.String(length=120), nullable=False),
        sa.Column("phone", sa.String(length=40), nullable=False),
        sa.Column("party_size", sa.Integer(), nullable=False),
        sa.Column("reserved_for", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("seating_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reservations_location_guid", "reservations", ["location_guid"])
    op.create_index("ix_reservations_reserved_for", "reservations", ["reserved_for"])

    op.create_table(
        "waitlist",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("location_guid", sa.String(length=36), nullable=False),
        sa.Column("guest_name", sa.String(length=120), nullable=False),
        sa.Column("phone", sa.String(length=40), nullable=False),
        sa.Column("party_size", sa.Integer(), nullable=False),
        sa.Column("quoted_minutes", sa.Integer(), nullable=True),
        sa.Column("joined_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("seating_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_waitlist_location_guid", "waitlist", ["location_guid"])


def downgrade() -> None:
    op.drop_index("ix_waitlist_location_guid", table_name="waitlist")
    op.drop_table("waitlist")
    op.drop_index("ix_reservations_reserved_for", table_name="reservations")
    op.drop_index("ix_reservations_location_guid", table_name="reservations")
    op.drop_table("reservations")
    op.drop_index("ix_seatings_loc_seated_at", table_name="seatings")
    op.drop_index("ix_seatings_loc_open", table_name="seatings")
    op.drop_index("ix_seatings_table_guid", table_name="seatings")
    op.drop_index("ix_seatings_location_guid", table_name="seatings")
    op.drop_table("seatings")
    op.drop_table("section_tables")
    op.drop_index("ix_sections_shift_date", table_name="sections")
    op.drop_index("ix_sections_location_guid", table_name="sections")
    op.drop_table("sections")
    op.drop_index("ix_floor_fixtures_location_guid", table_name="floor_fixtures")
    op.drop_table("floor_fixtures")
    op.drop_table("floor_layouts")
    op.drop_table("floor_sync_state")
    op.drop_index("ix_toast_service_areas_location_guid", table_name="toast_service_areas")
    op.drop_table("toast_service_areas")
    op.drop_index("ix_toast_tables_loc_deleted", table_name="toast_tables")
    op.drop_index("ix_toast_tables_location_guid", table_name="toast_tables")
    op.drop_table("toast_tables")
