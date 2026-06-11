"""Frozen Gate-0 schema for the Sections / Floor feature (docs/floor_contract.md).

Written by the ck orchestrator at Gate 0; OWNED BY SA-2 from Gate 1 on.
Any column change must be reflected in docs/floor_contract.md's deviation log.

Design constraints (see contract sections 2-3):
- All DateTime columns store naive UTC. Business-date logic lives in routes.
- All rows keyed by location_guid = Toast restaurant GUID.
- Prod applies schema via boot-time create_all (alembic is not wired on
  Render) -> ensure_floor_tables(engine) below is the real schema apply;
  floor_routes calls it at import time.
- toast_tables / toast_service_areas are SOFT-deleted only (deleted=1):
  historical seatings join on old GUIDs.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base

# Frozen 8-color server palette (contract section 5). Index order matters.
FLOOR_PALETTE = [
    {"key": "teal", "hex": "#14B8A6"},
    {"key": "purple", "hex": "#8B5CF6"},
    {"key": "blue", "hex": "#3B82F6"},
    {"key": "pink", "hex": "#EC4899"},
    {"key": "green", "hex": "#22C55E"},
    {"key": "amber", "hex": "#F59E0B"},
    {"key": "red", "hex": "#EF4444"},
    {"key": "slate", "hex": "#64748B"},
]

TABLE_SHAPES = ("square", "rect", "circle", "diamond")
FIXTURE_TYPES = ("wall", "label")
RESERVATION_STATUSES = (
    "upcoming", "confirmed", "arrived", "seated", "no_show", "cancelled",
)
WAITLIST_STATUSES = ("waiting", "notified", "seated", "left")


class ToastTableCfg(Base):
    """Toast table config mirror (read-only POS data; SA-1 syncs it)."""
    __tablename__ = "toast_tables"

    guid: Mapped[str] = mapped_column(String(36), primary_key=True)
    location_guid: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    service_area_guid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revenue_center_guid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    last_synced: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_toast_tables_loc_deleted", "location_guid", "deleted"),
    )


class ToastServiceArea(Base):
    __tablename__ = "toast_service_areas"

    guid: Mapped[str] = mapped_column(String(36), primary_key=True)
    location_guid: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    last_synced: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FloorSyncState(Base):
    """SA-1 incremental-sync high-water mark per (location, resource)."""
    __tablename__ = "floor_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_guid: Mapped[str] = mapped_column(String(36))
    resource: Mapped[str] = mapped_column(String(40))  # 'tables' | 'service_areas'
    last_modified: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("location_guid", "resource", name="uq_floor_sync_loc_resource"),
    )


class FloorLayout(Base):
    """Where a Toast table sits on the canvas (contract section 4 coords)."""
    __tablename__ = "floor_layouts"

    location_guid: Mapped[str] = mapped_column(String(36), primary_key=True)
    table_guid: Mapped[str] = mapped_column(String(36), primary_key=True)
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    w: Mapped[float] = mapped_column(Float, default=80.0)
    h: Mapped[float] = mapped_column(Float, default=80.0)
    shape: Mapped[str] = mapped_column(String(10), default="square")
    rotation: Mapped[int] = mapped_column(Integer, default=0)


class FloorFixture(Base):
    __tablename__ = "floor_fixtures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_guid: Mapped[str] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(10))  # 'wall' | 'label'
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    w: Mapped[float] = mapped_column(Float, default=120.0)
    h: Mapped[float] = mapped_column(Float, default=20.0)
    rotation: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str | None] = mapped_column(String(60), nullable=True)


class FloorSection(Base):
    """One server's section for one shift date at one location."""
    __tablename__ = "sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_guid: Mapped[str] = mapped_column(String(36), index=True)
    shift_date: Mapped["Date"] = mapped_column(Date, index=True)
    server_employee_guid: Mapped[str] = mapped_column(String(36))
    color: Mapped[str] = mapped_column(String(10), default="")  # hex from FLOOR_PALETTE
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "location_guid", "shift_date", "server_employee_guid",
            name="uq_sections_loc_date_server",
        ),
    )


class FloorSectionTable(Base):
    __tablename__ = "section_tables"

    section_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sections.id"), primary_key=True
    )
    table_guid: Mapped[str] = mapped_column(String(36), primary_key=True)


class FloorSeating(Base):
    """A party seated at a table. Open while cleared_at IS NULL."""
    __tablename__ = "seatings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_guid: Mapped[str] = mapped_column(String(36), index=True)
    table_guid: Mapped[str] = mapped_column(String(36), index=True)
    party_size: Mapped[int] = mapped_column(Integer)
    seated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    seated_by: Mapped[str] = mapped_column(String(80), default="")
    # Snapshot of who served, taken at seat time (section lookup or explicit
    # override). Authoritative for covers; NULL = unassigned table.
    server_employee_guid_at_seat: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reservation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    waitlist_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_seatings_loc_open", "location_guid", "cleared_at"),
        Index("ix_seatings_loc_seated_at", "location_guid", "seated_at"),
    )


class FloorReservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_guid: Mapped[str] = mapped_column(String(36), index=True)
    guest_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(40), default="")
    party_size: Mapped[int] = mapped_column(Integer)
    reserved_for: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(12), default="upcoming")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    seating_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FloorWaitlistEntry(Base):
    __tablename__ = "waitlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_guid: Mapped[str] = mapped_column(String(36), index=True)
    guest_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(40), default="")
    party_size: Mapped[int] = mapped_column(Integer)
    quoted_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 'notified' is a MANUAL toggle this run (SMS out of scope) - clean hook
    # for a future messaging integration.
    status: Mapped[str] = mapped_column(String(10), default="waiting")
    seating_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


FLOOR_TABLE_CLASSES = [
    ToastTableCfg,
    ToastServiceArea,
    FloorSyncState,
    FloorLayout,
    FloorFixture,
    FloorSection,
    FloorSectionTable,
    FloorSeating,
    FloorReservation,
    FloorWaitlistEntry,
]


def ensure_floor_tables(engine) -> None:
    """Idempotent create of just the floor tables (CREATE TABLE IF NOT
    EXISTS semantics via checkfirst). Called at floor_routes import time so
    a fresh Render deploy creates the schema with no alembic step. Safe
    under gunicorn multi-worker boot."""
    if engine is None:
        return
    Base.metadata.create_all(
        bind=engine,
        tables=[cls.__table__ for cls in FLOOR_TABLE_CLASSES],
        checkfirst=True,
    )
