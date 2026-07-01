from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from sqlalchemy import (
    String,
    Integer,
    Boolean,
    DateTime,
    Float,
    Numeric,
    Date,
    Time,
    Text,
    ForeignKey,
    UniqueConstraint,
    JSON,
    Index,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _local_today() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(os.getenv("APP_TZ", "America/Chicago"))).date()
    except Exception:
        utc_now = datetime.utcnow()
        y = utc_now.year
        mar1 = date(y, 3, 1)
        second_sunday_march = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
        nov1 = date(y, 11, 1)
        first_sunday_nov = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
        offset = -5 if second_sunday_march <= utc_now.date() < first_sunday_nov else -6
        return (utc_now + timedelta(hours=offset)).date()


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    # ezCater Delivery UUID (e.g. "5cbca855-da04-46c9-9e3d-234d089ac3b0").
    # Needed by the "unassign auto-assigned courier" action so the API can
    # be called without re-looking-up the order by external_order_id.
    external_delivery_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    client: Mapped[str | None] = mapped_column(String(255), nullable=True)
    upon_delivery_ask_for: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    delivery_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    delivery_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    headcount: Mapped[int | None] = mapped_column(Integer, nullable=True)

    reported_store: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reported_store_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    origin_store_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    delivery_date: Mapped[str | None] = mapped_column(String(50), nullable=True)
    deliver_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    delivery_window: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    setup_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    status: Mapped[str] = mapped_column(String(50), default="new", nullable=False, index=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    warning_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    flags: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Sum of qty × unit_price across this order's items, computed from the
    # "Item @ $XX.XX" prices baked into OrderItem.raw_alias by the ingest
    # pipeline. Used by /reports/sales (ezCater channel) + the labor cost
    # ratio. Nullable for legacy rows; populated by a backfill on first boot.
    total_amount: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Per-order data backfilled from the ezCater Delivery Performance Report
    # and Order Data XLSX exports (migration 11_payroll_backfill). These back
    # the driver-payroll page and the per-driver / per-store sales views.
    tracking_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ezcater_driver_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    pickup_kitchen: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pickup_miles: Mapped[float | None] = mapped_column(Float, nullable=True)
    food_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    tip_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    delivery_fee: Mapped[float | None] = mapped_column(Float, nullable=True)
    caterer_total_due: Mapped[float | None] = mapped_column(Float, nullable=True)
    delivery_result: Mapped[str | None] = mapped_column(String(60), nullable=True)
    delivery_start_time: Mapped[str | None] = mapped_column(String(20), nullable=True)
    delivery_complete_time: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # ezCater live tracking (migration 12). Their public tracker page calls
    # delivery-management.ezcater.com/delivery_tracking/v1/delivery/<uuid>
    # which returns the driver's live GPS + status key. We capture the UUID
    # per order (manual paste of the tracker URL for now) and cache the
    # latest poll result inline.
    delivery_tracking_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ezcater_status_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    ezcater_driver_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    ezcater_driver_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    ezcater_status_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Dispatch result, computed at upload time. Persisted so per-order views
    # can render the Driver / Prep Expo / Master tabs without re-running the
    # Google Maps + dispatch_planner stack.
    kitchen_ready_time: Mapped[str | None] = mapped_column(String(50), nullable=True)
    driver_departure_time: Mapped[str | None] = mapped_column(String(50), nullable=True)
    assigned_driver: Mapped[str | None] = mapped_column(String(50), nullable=True)
    route_group_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    route_stop_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---- Driver-system / delivery state-machine columns (migration 15) ----
    # Order is the Delivery in SPEC.md terms — same row, just with the
    # bid/approval/lifecycle tracking added on top of the existing
    # ingest fields. status reuses the existing column; new values:
    # available | requested | approved | picked_up | en_route | delivered |
    # cancelled | no_show. Old ingest values ('new', etc) continue to work.
    delivery_window_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    delivery_window_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    customer_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    setup_photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    setup_photo_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parking_photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    parking_photo_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parking_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    # potential_payout is the estimated total computed at delivery creation
    # via app.services.ezcater_payroll.compute_one(). Snapshotted so pay-
    # structure changes don't retroactively change quoted earnings.
    potential_payout: Mapped[float | None] = mapped_column(Float, nullable=True)
    # paid_payout is set when payroll closes a PayCheck; nullable until then.
    paid_payout: Mapped[float | None] = mapped_column(Float, nullable=True)
    paycheck_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # FK to Driver (the integer ID). Distinct from the legacy assigned_driver
    # string field which captured ezCater's freeform driver name.
    assigned_driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    approved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pickup_actual_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    en_route_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_actual_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ---- Manager payroll inputs (Sam #1492/#1503, 2026-05-28) ----
    # Written from the Ez Drivers payroll-entry surface; read by compute_one(),
    # which prefers a set value and otherwise falls back to the auto-derived
    # estimate. All nullable — NULL means "not yet verified", so estimates keep
    # showing until a manager confirms. Only pay_verified_miles changes pay
    # (extra miles over 20 at $2.00/mi); pay_driven_miles is display-only
    # and overrides the ezCater route-history driven estimate when set.
    pay_verified_miles: Mapped[float | None] = mapped_column(Float, nullable=True)
    pay_driven_miles: Mapped[float | None] = mapped_column(Float, nullable=True)
    pay_bonus_tracked: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pay_five_star: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pay_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pay_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pay_verified_by: Mapped[str | None] = mapped_column(String(80), nullable=True)

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )

    processing_orders: Mapped[list["ProcessingOrder"]] = relationship(
        back_populates="order"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)

    raw_alias: Mapped[str] = mapped_column(String(255))
    item_key: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    qty: Mapped[int | None] = mapped_column(Integer, nullable=True)

    package_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    packaging: Mapped[str | None] = mapped_column(String(50), nullable=True)

    servings: Mapped[int | None] = mapped_column(Integer, nullable=True)

    choices: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extras: Mapped[list | None] = mapped_column(JSON, nullable=True)
    flags: Mapped[list | None] = mapped_column(JSON, nullable=True)

    source: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    order: Mapped["Order"] = relationship(back_populates="items")

    breakdowns: Mapped[list["PrepBreakdownRecord"]] = relationship(
        back_populates="order_item",
        cascade="all, delete-orphan",
    )


class PrepBreakdownRecord(Base):
    __tablename__ = "prep_breakdowns"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("order_items.id", ondelete="CASCADE"),
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    rules_version: Mapped[str | None] = mapped_column(String(50), nullable=True)

    breakdown: Mapped[dict] = mapped_column(JSON)

    order_item: Mapped["OrderItem"] = relationship(back_populates="breakdowns")


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String(50), default="processing", nullable=False, index=True)

    pdf_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    trigger_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    processing_orders: Mapped[list["ProcessingOrder"]] = relationship(
        back_populates="processing_job",
        cascade="all, delete-orphan",
    )


class ProcessingOrder(Base):
    __tablename__ = "processing_orders"

    id: Mapped[int] = mapped_column(primary_key=True)

    processing_job_id: Mapped[int] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        index=True,
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(50), default="processing", nullable=False, index=True)
    stage_failed: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    warning_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    processing_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    processing_job: Mapped["ProcessingJob"] = relationship(back_populates="processing_orders")
    order: Mapped[Order | None] = relationship(back_populates="processing_orders")

    failure_snapshots: Mapped[list["FailureSnapshot"]] = relationship(
        back_populates="processing_order",
        cascade="all, delete-orphan",
    )


class FailureSnapshot(Base):
    __tablename__ = "failure_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)

    processing_order_id: Mapped[int] = mapped_column(
        ForeignKey("processing_orders.id", ondelete="CASCADE"),
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.utcnow() + timedelta(days=14),
        index=True,
    )

    raw_order_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    normalized_order_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    traceback_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    processing_order: Mapped["ProcessingOrder"] = relationship(back_populates="failure_snapshots")

class Driver(Base):
    __tablename__ = "drivers"
    __table_args__ = (
        UniqueConstraint("name", "location", name="uq_driver_name_location"),
        UniqueConstraint("email", name="uq_drivers_email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    location: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lockout_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 5-digit PIN keypad auth (2026-05-12 — migrating drivers off email+password
    # to email+PIN, mirroring the User keypad pattern in app/web/keypad_auth.py).
    # password_hash is retained for backwards compat: legacy accounts log in via
    # the fallback path until an admin reset moves them onto passcode_hash.
    passcode_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    first_login_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # ---- Driver-system fields (migration 15) ----
    # status replaces the legacy `active` boolean over time. `active` stays
    # for backwards compat; new code should read `status`. Values:
    # 'active' | 'suspended' | 'terminated'.
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # joined_at is the day they signed up (Date — no time component needed).
    joined_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Lifetime delivery count is the cumulative number of `delivered` orders
    # this driver completed. Used for the "20 lifetime" New-tier exit threshold.
    lifetime_delivery_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # current_score / current_tier are snapshots of the latest DriverScore
    # row, denormalized here for fast read-side queries (Ez Manage row sort,
    # tier-cap enforcement). Updated by the nightly recompute job.
    current_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_tier: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # Default origin store for this driver (e.g. 'dos' / 'uno'). Distinct
    # from the legacy `location` field which doubled as auth + display.
    home_store_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Latest GPS fix (cached, written from /driver/track). DriverLocation
    # remains the canonical trail; these are read-side conveniences.
    last_known_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_known_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_location_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Battery-optimization whitelist state (Sam #1025 2026-05-19). True when
    # the driver's phone has Cenas Kitchen whitelisted from Doze / battery
    # saver — required for the GPS foreground service to survive screen off.
    # Reported by the native plugin at shift start.
    battery_opt_ignored: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    battery_opt_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DriverApplication(Base):
    __tablename__ = "driver_application"
    __table_args__ = (
        Index("ix_driver_application_location_created", "preferred_location", "created_at"),
        Index("ix_driver_application_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    full_name: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(50), nullable=False)
    whatsapp: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    zip_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 'copperfield' | 'tomball' | 'both'. The 'both' value appears in both
    # store-scoped application tabs.
    preferred_location: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    available_days: Mapped[list | None] = mapped_column(JSON, nullable=True)
    shift_preference: Mapped[str | None] = mapped_column(String(40), nullable=True)
    has_license: Mapped[str | None] = mapped_column(String(10), nullable=True)
    has_vehicle: Mapped[str | None] = mapped_column(String(10), nullable=True)
    has_insurance: Mapped[str | None] = mapped_column(String(10), nullable=True)
    has_smartphone: Mapped[str | None] = mapped_column(String(10), nullable=True)
    delivery_experience: Mapped[str | None] = mapped_column(String(10), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="new", nullable=False, index=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    remote_addr: Mapped[str | None] = mapped_column(String(80), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)


class DriverLog(Base):
    __tablename__ = "driver_logs"
    # Driver name, date, order link, ex miles, ex miles verified, $10 bonus (on time? tracking? took photo?), 5 star, notes

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    driver_name: Mapped[str] = mapped_column(String(100), nullable=False)
    pickup_date: Mapped[str] = mapped_column(String(20), nullable=False)
    order_link: Mapped[str | None] = mapped_column(String(100), nullable=True)

    ex_miles: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ex_miles_verified: Mapped[int | None] = mapped_column(Integer, nullable=True)

    on_time: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tracking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    picture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    five_star: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    location: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    logged_by: Mapped[str | None] = mapped_column(String(100), nullable=True)


class DriverShift(Base):
    """An on-clock period for a driver. GPS streaming runs only while ended_at
    is NULL — drivers explicitly tap Start/End in the portal."""
    __tablename__ = "driver_shift"

    id: Mapped[int] = mapped_column(primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="CASCADE"),
                                           nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DriverLocation(Base):
    """One GPS fix from the driver's phone, FK'd to its shift so the route
    can be replayed (Phase C). When a driver is actively working an order,
    order_id ties the GPS fix to that delivery without relying on ezCater
    tracking."""
    __tablename__ = "driver_location"

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("driver_shift.id", ondelete="CASCADE"),
                                          nullable=False, index=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="CASCADE"),
                                           nullable=False, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"),
                                                 nullable=True, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                  nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading_deg: Mapped[float | None] = mapped_column(Float, nullable=True)


class DriverFile(Base):
    """Registry row for every driver-uploaded proof/receipt/document.

    Orders still keep setup_photo_url / parking_photo_url for backwards
    compatibility. This table is the driver-centered file ledger that lets the
    profile, payroll, manager views, and local DB mirror all point at the same
    upload record.
    """
    __tablename__ = "driver_file"
    __table_args__ = (
        UniqueConstraint("order_id", "kind", "public_route", name="uq_driver_file_order_kind_route"),
        Index("ix_driver_file_driver_created", "driver_id", "created_at"),
        Index("ix_driver_file_order_created", "order_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    public_route: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exists: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    meta_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DriverEvent(Base):
    """Driver-centered timeline.

    This is the small connective layer Sam asked for: driver actions are still
    stored in their operational tables, but every meaningful action also has a
    timeline row tied back to driver_id/order_id/file_id.
    """
    __tablename__ = "driver_event"
    __table_args__ = (
        Index("ix_driver_event_driver_time", "driver_id", "created_at"),
        Index("ix_driver_event_order_time", "order_id", "created_at"),
        Index("ix_driver_event_type_time", "event_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    file_id: Mapped[int | None] = mapped_column(
        ForeignKey("driver_file.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    actor_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class EzcaterTrackingPoint(Base):
    """One sampled ezCater live-tracking location for a delivery.

    This is separate from DriverLocation because the source is ezCater's
    public delivery tracker, not the driver's phone streaming to Cenas.
    """
    __tablename__ = "ezcater_tracking_point"
    __table_args__ = (
        Index("ix_ezcater_tracking_point_order_time", "order_id", "captured_at"),
        Index("ix_ezcater_tracking_point_driver_time", "driver_id", "captured_at"),
        Index("ix_ezcater_tracking_point_uuid_time", "tracking_uuid", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tracking_uuid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    driver_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    provider_status_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)


class ProducePriceSnapshot(Base):
    """One row per (vendor, item, snapshot_date) — captures the price the
    vendor quoted in their weekly price sheet. Populated by produce_ingest
    every time a fresh email is parsed; the (snapshot_date, vendor,
    canonical_name, canonical_size) uniqueness keeps re-runs idempotent."""
    __tablename__ = "produce_price_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    snapshot_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    vendor: Mapped[str] = mapped_column(String(50), nullable=False, index=True)         # 'alvarado' / 'jluna'
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    canonical_size: Mapped[str | None] = mapped_column(String(100), nullable=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    raw_item_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    parsed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)            # ISO timestamp from the source email
    date_range: Mapped[str | None] = mapped_column(String(80), nullable=True)           # e.g. "5/5 - 5/11"

    __table_args__ = (
        UniqueConstraint("snapshot_date", "vendor", "canonical_name", "canonical_size",
                         name="uq_pps_per_day_vendor_item"),
    )


class DeveloperChatMessage(Base):
    """Persistent chat for Sam + Claude instances (and any other AI agents)
    to coordinate via the Partner-only Developer view."""
    __tablename__ = "developer_chat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    author: Mapped[str] = mapped_column(String(60), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    attachments: Mapped[list["DeveloperChatAttachment"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="DeveloperChatAttachment.id",
    )


class DeveloperChatMessageArchive(Base):
    """Append-only archive of dev chat messages trimmed by the rolling
    200/100 cap or the one-time bulk archive+wipe.

    Per Sam dev chat 2026-05-19 4:07pm: "remember max 200msgs on this
    chage. the rest consistently archive." Honors samai #2887 archive-
    before-delete safety flag — INSERT here precedes any DELETE from
    developer_chat per samai #2980 spec.

    Schema mirrors DeveloperChatMessage (author/body/created_at) with
    original_id preserving the source row id after delete + archived_at
    marking when the row landed here. Attachments not archived — out of
    scope; the source-row delete cascade handles file cleanup.
    """
    __tablename__ = "developer_chat_archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    author: Mapped[str] = mapped_column(String(60), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    archived_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)


class CenaChatLog(Base):
    """Logs of CENA AI Assistant interactions across all employees.
    Provides Sam with an audit trail to daily review and rate responses
    ('good' vs 'needs_address') for system improvements.
    """
    __tablename__ = "cena_chat_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_name: Mapped[str] = mapped_column(String(150), nullable=False)
    user_tier: Mapped[str] = mapped_column(String(20), nullable=False)  # partner | manager | hourly
    question: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_status: Mapped[str] = mapped_column(String(20), default="unreviewed", nullable=False)  # unreviewed | good | needs_address
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class DevChatAttributionCorrection(Base):
    """Sidecar table mapping a developer_chat row to its corrected author
    when the original author column got misattributed (e.g. the
    2026-05-17 attribution incident: ck-via-Chrome-MCP-fetch-without-
    author-form-field landed 4 ck posts under author='sam' because the
    server default at developer_chat.py:119 was 'sam'). Sidecar over
    in-place UPDATE per samai #2024: mutating the source column is
    irreversible if a correction is wrong; the sidecar preserves the
    original row and overlays the correction, so the audit trail can
    show 'displayed as X, actually authored by Y' transparently.

    UI render: developer_chat.html joins on message_id; when a row
    exists, shows corrected_author with the original as a strikethrough
    or footnote. UI fold-in is a follow-up commit (ck/cena lane). This
    table is the persistent audit-correction store.
    """
    __tablename__ = "dev_chat_attribution_corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("developer_chat.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True)
    original_author: Mapped[str] = mapped_column(String(60), nullable=False)
    corrected_author: Mapped[str] = mapped_column(String(60), nullable=False)
    correction_reason: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    corrected_by: Mapped[str] = mapped_column(String(60), nullable=False)


class EzcaterKnownDriver(Base):
    """Manager-maintained roster of ezCater drivers we recognize, used to
    auto-verify Driver signups. When a Driver signs up with a phone that
    matches a row here, their `Driver.active` reflects 'verified ezCater
    driver' rather than a manual toggle. Seeded from Sam's 5/10 screenshot
    roster (CK#1 = Copperfield kitchen, CK#2 = Tomball kitchen)."""
    __tablename__ = "ezcater_known_driver"
    __table_args__ = (
        UniqueConstraint("phone_e164", name="uq_ezcater_known_driver_phone"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # 1 = Copperfield kitchen (UNO MAS), 2 = Tomball kitchen (DOS MAS),
    # NULL = ambiguous (no CK# prefix in source roster).
    ck_prefix: Mapped[int | None] = mapped_column(Integer, nullable=True)


class EzcaterOrderDetails(Base):
    """PDF-extracted detail fields per order (migration 36, Sam #530
    pipeline). One row per external_order_id; UPSERT on re-extraction.

    Holds fields the ezCater Partner API does NOT surface but the PDF
    does: per-item prices, setup-piece counts, dietary notes, day-of
    contact, gate codes, special-instructions free-text, and the fee
    breakdown (commission / service / processing) that orders.fee
    bundles into a single total. Cena #534 locked the field list;
    aick built the migration + model + extractor; ck built the
    Step-1 Playwright download script.

    Kept separate from `orders` per Cena #534 directive: "don't bolt
    PDF-derived fields onto orders, keep API-authoritative fields
    pristine in their own table."
    """
    __tablename__ = "ezcater_order_details"
    __table_args__ = (
        UniqueConstraint("external_order_id",
                         name="uq_ezcater_order_details_external_order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_order_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    # PDF-only fields per Cena #534.
    items_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    setup_pieces_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    special_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    gate_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    day_of_contact_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    day_of_contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # ezCater fee breakdown — cents (integer) to avoid float drift.
    commission_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    service_fee_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_fee_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Provenance — reproducible audit trail.
    source_pdf_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_pdf_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extractor_version: Mapped[str | None] = mapped_column(String(20), nullable=True, default="1")
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    extracted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False,
    )


class DriverAssignmentJob(Base):
    """One in-flight or completed re-assignment of an ezCater order's
    driver (migration 37, Sam #669 build). Spawned by the per-order
    dropdown on the catering Ez Orders page; consumed by the Selenium
    flow in app/services/ezcater_driver_assigner.py.

    Per Sam's amendment, verification is done by DOM re-read on the
    order page after submit — NOT by PDF parse — so no
    verification_pdf_path column. The PDF-archive flow (nightly cron)
    is unrelated and unaffected.
    """
    __tablename__ = "driver_assignment_jobs"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_driver_assignment_jobs_job_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    current_driver: Mapped[str | None] = mapped_column(String(160), nullable=True)
    new_driver: Mapped[str] = mapped_column(String(160), nullable=False)

    # pending -> running -> completed | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    gateway_processed: Mapped[str | None] = mapped_column(String(40), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False,
    )


class User(Base):
    """Site-wide user account (migration 13). Replaces the shared-password
    Tier 1/Tier 2 gates with per-person 5-digit numeric passcodes. Roles in
    descending privilege: partner, corporate, gm, manager, expo, corporate-driver.
    Sam (2026-05-11): keypad-only login (no username field) — when 5 digits
    are entered we scan active users for a passcode_hash match; passcode
    uniqueness is enforced at create/change time."""
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        UniqueConstraint("phone", name="uq_users_phone"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    passcode_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'partner' > 'corporate' > 'gm' > 'manager' > 'expo' > 'corporate-driver'
    permission_level: Mapped[str] = mapped_column(String(30), nullable=False, default="manager")
    # 'tomball' | 'copperfield' | 'both' | NULL (NULL == all stores, used for partner/corporate)
    store_scope: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # First-time login forces a passcode change before any other route is reached.
    first_login_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lockout_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Bumped on passcode reset / deactivation to force-invalidate all live
    # sessions for this user. Sessions stamp this value at login; each request
    # re-checks it and signs the user out on mismatch.
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class PermissionDenial(Base):
    """Append-only log of permission_denied events from the
    @requires_permission decorator. Surfaces on
    /partner/developer/app/denials so partners can see what's getting
    blocked and decide if a role's tag set needs adjustment.

    Phase 0 Block 4 follow-up — denials surface (Sam: 2026-05-13), per
    samai's permission_system spec section 5.3. Not enforced-append-only
    at the ORM layer — denials can be high-volume and a future
    retention job may legitimately prune old rows. Audit trail integrity
    is on UserAuditLog (which IS append-only-enforced), not here."""
    __tablename__ = "permission_denial"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True,
    )
    # The denied user. SET NULL if the user row is later removed (we
    # archive, never delete, but the FK is defensive). Tier-2-only
    # sessions (site-password but no User) get NULL user_id.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    user_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    user_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Permission tag the user was missing (e.g. "orders.assign_driver").
    tag: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    # Request path that triggered the denial.
    route: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 'ENFORCING' (denial redirected to /access-denied) or 'DARK-LAUNCH'
    # (denial logged but request passed through — only possible if
    # PERMISSION_ENFORCE=0 in env).
    mode: Mapped[str] = mapped_column(String(20), nullable=False)


class UserAuditLog(Base):
    """Append-only audit trail for Team admin (User table) mutations.
    Every create / edit / role_change / activate / deactivate / passcode_reset
    inserts a row. Deletions are blocked at the ORM layer (see the
    before_delete listener at the bottom of this file) so role-change
    history reads true even if a row is later challenged or redacted.

    Phase 0 / Block 4 follow-up — Team UI commit (Sam: 2026-05-13). Mirrors
    the LegalAccessLog pattern: actor_label cached so the trail still names
    a human even if the actor row is later deactivated; action enum-style;
    before/after values stored as plain text so a role-change diff is
    self-describing without joining other tables.
    """
    __tablename__ = "user_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True,
    )
    # Target = the user whose row was mutated. SET NULL on delete so audit
    # rows survive a (hypothetical) hard-delete of the user row; in practice
    # we archive (active=False), not delete, but the FK is defensive.
    target_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    target_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Actor = the partner-level user who performed the mutation.
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    actor_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # 'create' | 'edit' | 'role_change' | 'deactivate' | 'reactivate' | 'passcode_reset'
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # For role_change: "old_role|old_store_scope" / "new_role|new_store_scope".
    # For create: after_value carries the initial "role|store_scope".
    # For edit (non-role): details lists which fields changed.
    before_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class UserPermissionOverride(Base):
    """Per-user, per-store permission override for the PERMISSIONS admin page
    (Sam #1676, PARTNER-ONLY). A row = an explicit 'allow'/'deny' for
    (user, store, perm_key) that overrides the role-template default; 'inherit'
    = no row. Managed via /partner/developer/permissions. NOT for ezCater
    driver perms (those are coded-locked, separate)."""
    __tablename__ = "user_permission_override"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    store_key: Mapped[str] = mapped_column(String(40), nullable=False)   # 'copperfield' | 'tomball'
    perm_key: Mapped[str] = mapped_column(String(80), nullable=False)    # permission_catalog key
    mode: Mapped[str] = mapped_column(String(10), nullable=False)        # 'allow' | 'deny'
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False,
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )


class PositionPermission(Base):
    """A permission GRANTED to a (position, store) - the POSITION-based
    permissions model (Sam #2426/#2435). The partner toggles each catalog
    permission ON/OFF for a position-at-a-store; a row here = ON (granted),
    no row = OFF. A person's effective perms at their active store = the UNION
    of their positions' rows for that store (Q1=union, #2435). Supersedes the
    per-user UserPermissionOverride for the permissions page (Sam's pivot from
    per-user to per-position). position_key = a permission_catalog ROLE key
    (the 16 positions). NOT ezCater driver perms (coded-locked, separate)."""
    __tablename__ = "position_permission"
    __table_args__ = (
        UniqueConstraint("position_key", "store_key", "perm_key", name="uq_pos_store_perm"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)  # permission_catalog role/position key
    store_key: Mapped[str] = mapped_column(String(40), nullable=False)   # 'copperfield' | 'tomball'
    perm_key: Mapped[str] = mapped_column(String(80), nullable=False)    # permission_catalog key (present = ON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False,
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )


class SampleApproval(Base):
    """Approval state for a sample on the /partner/developer/samples
    page. One row per sample_slug (latest state only — no history
    table in v1; if audit becomes needed, add sample_approval_history
    later). Sam toggles status via the per-card approval UI; aick/ck
    track via reads.

    Spec: app/templates/docs/spec_samples_approval_workflow.html §2.1
    (dck 68c5248 + Cena #2549 item 2 + ck #2548 ship-pending-models).
    """
    __tablename__ = "sample_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_slug: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # 'pending' | 'approved' | 'rejected'
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    marked_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow,
        onupdate=datetime.utcnow, nullable=False
    )

    attachments: Mapped[list["SampleApprovalAttachment"]] = relationship(
        back_populates="approval", cascade="all, delete-orphan"
    )


class SampleApprovalAttachment(Base):
    """Image attachments to a SampleApproval correction note. v1 caps
    at image/png + image/jpeg + image/webp, 5 MB per file. Files
    live under SAMPLE_APPROVAL_ATTACHMENTS_DIR (env-overridable, ck
    #2548 per spec §2.2 storage backend note).

    Spec: spec_samples_approval_workflow.html §2.2.
    """
    __tablename__ = "sample_approval_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_approval_id: Mapped[int] = mapped_column(
        ForeignKey("sample_approvals.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    approval: Mapped["SampleApproval"] = relationship(
        back_populates="attachments"
    )


class CenaWakeDecision(Base):
    """Telemetry row for the Haiku-classifier-gated cena wake pipeline.
    One row per dev chat message the watcher considers, regardless of
    whether cena actually fires. Captures both the classifier verdict
    and the watcher's actual decision so the cena-stats dashboard can
    compute the would-have-fired vs did-fire delta that drives the
    cutover-from-shadow-to-enforcement call.

    Spec: Sam #2576 6-piece proposal (greenlight 2026-05-17) + cena
    #2572 refinements. Migration 29.
    """
    __tablename__ = "cena_wake_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dev_chat_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("developer_chat.id"), nullable=True, index=True
    )
    author: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)

    classifier_label: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # 'wake' | 'skip' | 'uncertain' | 'error'
    classifier_confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    classifier_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    classifier_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classifier_input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    classifier_output_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    classifier_cache_create_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    classifier_cache_read_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    classifier_latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    would_fire: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    did_fire: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    actual_rule_trigger: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    shadow_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )


class DeveloperChatAttachment(Base):
    """Files attached to a Developer Chat message. Up to 5 per message,
    enforced at the route layer. Files live under CHAT_ATTACHMENTS_DIR
    (default /var/data/chat-attachments) at <message_id>/<safe_filename>."""
    __tablename__ = "developer_chat_attachment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("developer_chat.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    is_image: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    message: Mapped["DeveloperChatMessage"] = relationship(back_populates="attachments")



# ============================================================
# Driver system (migration 15) — see SPEC.md sections 15-16
# ============================================================

class DeliveryRequest(Base):
    """One driver's request to take a delivery, with manager decision tracking.
    Unique on (delivery_id, driver_id) — a given driver can request each
    order at most once. status walks: pending → approved | declined |
    cancelled_by_driver. The approving manager FK's to users via
    decided_by_user_id."""
    __tablename__ = "delivery_request"
    __table_args__ = (
        UniqueConstraint("delivery_id", "driver_id", name="uq_delivery_request_delivery_driver"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    # 'pending' | 'approved' | 'declined' | 'cancelled_by_driver'
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )


class DriverNotification(Base):
    """One row per driver-facing notification (Issue B / Sam #1591 +
    samai #1599 spec). Append-only: rows are written by the lifecycle
    + request flow, and the only field a driver-side action mutates is
    read_at (mark-as-read). kinds:
      - order_taken_by_other: someone else got the order this driver
        had requested (written when approve_request declines siblings).
      - request_cancelled_admin: a manager declined this driver's
        request explicitly (back_to_bidding / decline_all flows).
    Surfaces inline at the top of /ez-market as a badge + dismissible
    cards. created_at index supports the most-recent-N read on the
    badge fetch.
    """
    __tablename__ = "driver_notification"
    __table_args__ = (
        Index("ix_driver_notification_driver_unread",
              "driver_id", "read_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    related_delivery_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True,
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DriverScore(Base):
    """One snapshot per nightly recompute (4am cron). Latest row per driver
    is what My Profile reads. Each metric_pts column is the breakdown so
    drivers can see exactly which metric is pulling them up/down. Score
    is the sum of the six pts columns."""
    __tablename__ = "driver_score"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    # 'new' | 'trusted' | 'rockstar' | 'top_rockstar'
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    # Component breakdown — sums to `score`
    tracking_pts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    on_time_pts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancellation_pts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    photo_pts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    response_pts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    star_pts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PayCheck(Base):
    """A closed bi-weekly paycheck. When payroll closes a period, each
    delivery in that window gets paid_payout + paycheck_id stamped on it.
    Driver's view at /pay-history reads from this table."""
    __tablename__ = "paycheck"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    pay_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    pay_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    gross_amount: Mapped[float] = mapped_column(Float, nullable=False)
    net_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    check_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)


class Cancellation(Base):
    """Log of driver-initiated cancellations after manager approval. Used
    for the 30-day / 90-day cancel-threshold rules in SPEC § 13:
      - 1 in 30d: score deduction, no admin action
      - 2 in 30d: flag for manager check-in
      - 3 in 90d: auto account review event"""
    __tablename__ = "cancellation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False,
    )
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    cancelled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # 'driver' | 'manager' | 'customer' | 'system'
    cancelled_by: Mapped[str] = mapped_column(String(20), default="driver", nullable=False)


class ManagerMessage(Base):
    """Outbound manager→driver messages during an active delivery. The
    reply latency from each message feeds the 'Manager response time'
    scoring metric (10 pts). Messages sent outside an active delivery
    don't count for that metric (during_active_delivery=False)."""
    __tablename__ = "manager_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sender_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    delivery_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    replied_within_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    during_active_delivery: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AccessRequest(Base):
    """A 'Request Access' submission from /request-access — used by the
    Cenas Kitchen Employee mobile app's first-launch flow and by web
    visitors who try to sign in without an account.

    Sam (or Masood, once they have Partner) sees pending rows in
    /partner/team and clicks Approve to convert them into a real User
    row (with an auto-generated temp passcode shown back to Sam to relay
    to the requester). Decline marks the row as rejected, no User
    created.

    Identifying fields are duplicated with what the eventual User row
    will hold, so we can build the User from this row alone on approve
    without asking the requester anything new."""
    __tablename__ = "access_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    requested_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'pending' | 'approved' | 'declined'
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    # If approved, points at the created User row so we can show
    # 'approved → created John Smith on 5/12' in the admin view.
    created_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    # Auto-generated temp passcode at approve time, shown ONCE to the
    # admin who approved (no plaintext stored after relay; this column
    # is a convenience while the row is fresh). NULL once dismissed.
    temp_passcode_one_shot: Mapped[str | None] = mapped_column(String(10), nullable=True)

# ============================================================
# Legal — Matters + Access Log (Phase 0 / Block 3, 2026-05-13)
# ============================================================

class LegalMatter(Base):
    """Open / in-review / resolved legal records the partners track —
    contracts, employment matters, compliance items, IP, litigation,
    counsel correspondence. Plain text only; signed PDFs etc. live on
    SiteGround/Drive, not in this table.

    Append-mostly: rows are kept after a matter closes (status flips
    to 'resolved' / 'archived' instead of deletion) so the audit
    history stays whole. UI does not expose a delete button.
    """
    __tablename__ = "legal_matters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 onupdate=datetime.utcnow, nullable=False)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # 'contract' | 'employment' | 'compliance' | 'litigation' | 'ip' |
    # 'corporate' | 'real-estate' | 'other'
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="other",
                                          index=True)
    # 'open' | 'in-review' | 'resolved' | 'archived'
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open",
                                        index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    counterparty: Mapped[str | None] = mapped_column(String(200), nullable=True)
    counsel_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    counsel_firm: Mapped[str | None] = mapped_column(String(200), nullable=True)
    counsel_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    counsel_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    matter_ref: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)

    opened_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    closed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_action_on: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    next_action_text: Mapped[str | None] = mapped_column(String(300), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Arbitrary named dates beyond opened_on / closed_on / next_action_on.
    # JSON shape: {"filed_on": "2026-05-01", "mediation_on": "2026-06-15",
    # "settlement_on": null, "hearing_on": "2026-07-30", ...}
    # The fixed columns above stay as denormalized convenience for sort +
    # the overview "next action" panel; key_dates absorbs everything else.
    # (Phase 0 / Block 3 cleanup, 2026-05-13.)
    key_dates: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )


class LegalMatterNote(Base):
    """Append-only timeline entries on a single LegalMatter. Replaces
    the role the old LegalMatter.notes text field used to play — that
    column is kept for backwards-compat but reads should pull from this
    table (most-recent first). Boot backfill migrates any non-empty
    legacy LegalMatter.notes into a 'first-note' row.

    Append-only by ORM listener (same pattern as LegalAccessLog) so a
    note can never silently disappear from the matter's history.
    Editing a note means appending a new one.
    """
    __tablename__ = "legal_matter_note"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    matter_id: Mapped[int] = mapped_column(
        ForeignKey("legal_matters.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 nullable=False, index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    actor_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)


class LegalDocument(Base):
    """A file uploaded to the Legal section — either pinned to a Matter
    (matter_id set) or filed globally (matter_id NULL). Storage path
    points at /var/data/legal-attachments/<id>/<safe_filename> on
    Render, mirroring the chat-attachments pattern from
    developer_chat_attachment.

    Append-only at the ORM layer for now — once the file is uploaded
    it stays in the history. If a file shouldn't be there anymore,
    we'll surface a redaction flag on the row but keep the bytes.
    """
    __tablename__ = "legal_document"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    matter_id: Mapped[int | None] = mapped_column(
        ForeignKey("legal_matters.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    uploaded_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    actor_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class LegalCompanyStructure(Base):
    """The /partner/legal/structure page is a single-row record describing
    the company entity layout — LLC / Corp + ownership splits + EINs +
    registered agent + registered office. Inline-editable; no notes
    timeline (changes are captured in LegalAccessLog).
    """
    __tablename__ = "legal_company_structure"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 onupdate=datetime.utcnow, nullable=False)

    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 'LLC' | 'C-Corp' | 'S-Corp' | 'Partnership' | other
    legal_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    dba: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state_of_formation: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ein: Mapped[str | None] = mapped_column(String(30), nullable=True)
    formed_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    registered_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    registered_office_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    principal_office_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Ownership as JSON list of {name, role, ownership_pct, notes}.
    # Plain JSON so a partner can add/remove members without a schema change.
    ownership: Mapped[list | None] = mapped_column(JSON, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )


class LegalInsurancePolicy(Base):
    """One row per active policy on /partner/legal/insurance. Renewal
    date drives an overview-page banner when within 30 days.
    """
    __tablename__ = "legal_insurance_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 onupdate=datetime.utcnow, nullable=False)

    carrier: Mapped[str | None] = mapped_column(String(200), nullable=True)
    policy_number: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # 'general-liability' | 'property' | 'workers-comp' | 'auto' |
    # 'cyber' | 'umbrella' | 'BOP' | 'other'
    policy_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    coverage_limit: Mapped[str | None] = mapped_column(String(80), nullable=True)
    deductible: Mapped[str | None] = mapped_column(String(80), nullable=True)
    premium: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Strings (not Float) so users can type "$5,000,000 per occurrence"
    # without us trying to parse currency.

    effective_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    renewal_on: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    broker_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    broker_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    broker_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'active' | 'lapsed' | 'cancelled'
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False,
                                        index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )


class LegalAccessLog(Base):
    """Append-only audit trail for the Legal section. Every view of a
    matter, every edit, every status change, every audit-page visit
    inserts a row. Deletions are blocked at the ORM layer via a
    before_delete event listener (see _no_delete_legal_log below) so
    no application code path — even an admin tool — can wipe entries.
    """
    __tablename__ = "legal_access_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # Cached display name in case the User row is later deactivated or
    # removed; the audit row still attributes the action to a human.
    actor_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # 'view_overview' | 'view_matters' | 'view_matter' | 'create_matter' |
    # 'edit_matter' | 'status_change' | 'view_audit'
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


# No-delete enforcement at the ORM layer. The 'before_delete' event fires
# inside the SQLAlchemy unit-of-work flush before the SQL DELETE is sent,
# so any code path that does db.delete(row) or cascades through a parent
# delete will raise here. Phase 0 / Block 3 (Sam: 2026-05-13).
from sqlalchemy import event as _sa_event

@_sa_event.listens_for(LegalAccessLog, 'before_delete')
def _no_delete_legal_log(mapper, connection, target):
    raise RuntimeError(
        "LegalAccessLog is append-only — rows cannot be deleted. "
        "If a record needs redaction, append a new audit row "
        "describing the redaction request and resolve it through "
        "a Matter; the underlying row stays."
    )


@_sa_event.listens_for(LegalMatterNote, 'before_delete')
def _no_delete_legal_matter_note(mapper, connection, target):
    raise RuntimeError(
        "LegalMatterNote is append-only — to update a note, append a "
        "new one. The old one stays so the matter's timeline reads "
        "true even when an earlier read of the situation was wrong."
    )


@_sa_event.listens_for(LegalDocument, 'before_delete')
def _no_delete_legal_document(mapper, connection, target):
    raise RuntimeError(
        "LegalDocument rows are append-only at the ORM layer. The bytes "
        "on disk stay too — if a file should not be there anymore, "
        "flag it via the matter audit + Sam handles removal manually "
        "(both the row and the file) outside the app."
    )


@_sa_event.listens_for(UserAuditLog, 'before_delete')
def _no_delete_user_audit_log(mapper, connection, target):
    raise RuntimeError(
        "UserAuditLog is append-only — rows cannot be deleted. "
        "Role-change history is the authoritative record of who got "
        "promoted/demoted and when; redacting a row would break the "
        "trail that the audit is for."
    )


# ============================================================
# Anomaly service (Phase 1 / Block 1, 2026-05-13)
# Companion to app/templates/docs/anomaly_service_spec.html.
# ============================================================

class Signal(Base):
    """One fired anomaly rule. Engine upserts on
    (rule_name, subject_id, store_id) — see app.services.anomaly_engine
    for the dedup + auto-clear flow.
    """
    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_rule_subject_store",
              "rule_name", "subject_id", "store_id"),
        Index("ix_signals_unresolved",
              "resolved_at", "acknowledged_at"),
        Index("ix_signals_trigger_at", "trigger_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_name: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    subject_label: Mapped[str] = mapped_column(String(200), nullable=False)
    trigger_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    action_text: Mapped[str] = mapped_column(String(400), nullable=False)
    # Stored as JSON arrays so SQLite can hold them natively.
    surfaces: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    audience_roles: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    acknowledged_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SignalAck(Base):
    """Acknowledgment audit log. One row per click of the Ack button on
    a Signal card — outlives the Signal row going resolved, so we can
    after-the-fact see who saw what + when.
    """
    __tablename__ = "signal_acks"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False)
    acked_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    note: Mapped[str | None] = mapped_column(String(400), nullable=True)


class MorningBrief(Base):
    """One row per (audience_user_id, brief_date). Body is the composed
    JSON from app.services.brief_composer matching the spec at
    /partner/developer/app/morning-brief-composer-spec §3. Composer is
    read-only against Signal; persists here after composition + dispatch.
    """
    __tablename__ = "morning_briefs"
    __table_args__ = (
        UniqueConstraint("audience_user_id", "brief_date",
                         name="uq_morning_briefs_user_date"),
        Index("ix_morning_briefs_date_role", "brief_date", "audience_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    brief_id: Mapped[str] = mapped_column(String(40), nullable=False,
                                          unique=True)
    audience_role: Mapped[str] = mapped_column(String(30), nullable=False)
    audience_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False)
    brief_date: Mapped[date] = mapped_column(Date, nullable=False)
    body: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    composed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    composer_model: Mapped[str] = mapped_column(String(60), nullable=False)
    fallback_used: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False)


class BriefFeedback(Base):
    """One row per (morning_brief × recipient × submission). Captures
    the calibration-panel reply for a single brief, per spec at
    /partner/developer/app/morning-brief-composer-spec §13 + Sam 20:13
    calibration directive.

    Two submission channels — Round 1 (email_reply) is the active path,
    Round 2 (form) is the upgrade once we have a baseline:
      - submitted_via='email_reply': samai/aick reads the panel
        member's email reply and creates this row. submitted_at is set
        on INSERT.
      - submitted_via='form': link clicked → row INSERTed with
        submitted_at=NULL (created_at marks the click). Form POST then
        UPDATEs submitted_at to the actual submission time.
        NULL submitted_at = link clicked but form not yet posted;
        non-NULL = submission completed.

    Response latency is a real signal per Sam 20:47 — the join
    submitted_at − morning_briefs.composed_at tells us about utility
    independent of what the recipient wrote. Indexes on FK +
    timestamps below support that join query cheaply.

    Append-only: an event listener at the bottom of this module rejects
    UPDATE/DELETE on this table outside of the explicit form-submit
    flow (which writes submitted_at exactly once). Keeps the audit
    trail clean for the Round 1 → Round 2 aggregation doc.
    """
    __tablename__ = "brief_feedback"
    __table_args__ = (
        Index("ix_brief_feedback_brief", "morning_brief_id"),
        Index("ix_brief_feedback_user", "user_id"),
        Index("ix_brief_feedback_submitted_at", "submitted_at"),
        Index("ix_brief_feedback_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    morning_brief_id: Mapped[int] = mapped_column(
        ForeignKey("morning_briefs.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 1 = "not useful", 5 = "extremely useful vs current morning workflow"
    useful_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    missed_something: Mapped[str | None] = mapped_column(Text, nullable=True)
    was_noise: Mapped[str | None] = mapped_column(Text, nullable=True)
    single_change: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "email_reply" | "form" — see docstring for the dual-channel split.
    submitted_via: Mapped[str] = mapped_column(String(20), nullable=False)
    # Round 1: set on INSERT. Round 2: NULL until form POST.
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)


class RuleOverride(Base):
    """Per-rule threshold + severity edits applied by a partner via
    /partner/anomalies/rules. Engine consults overrides at run start;
    falls back to RuleSpec.severity_default + the code defaults if
    nothing's stored. One row per (rule_name, store_id) — global
    overrides have store_id IS NULL.
    """
    __tablename__ = "rule_overrides"
    __table_args__ = (
        UniqueConstraint("rule_name", "store_id", name="uq_rule_override"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_name: Mapped[str] = mapped_column(String(80), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    threshold: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    severity_override: Mapped[str | None] = mapped_column(String(10), nullable=True)
    updated_by: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False)


class RibbonCategoryPreference(Base):
    """Per-user, per-category collapse state for the universal ribbon.

    Phase 2 / Block 1 / sub-block 1B (ck, 2026-05-14). The ribbon
    renders all seven categories on every authenticated page; this
    table records which categories a given user has collapsed.

    Absence of a row == expanded (the default). A row with
    is_collapsed=True == collapsed. A fresh user has zero preference
    rows and sees everything expanded — no backfill needed. The
    collapse-toggle endpoint (POST /partner/ribbon/collapse/<category>)
    upserts against the (user_id, category) unique constraint.

    user_id ondelete=CASCADE — pure per-user UI state, meaningless
    once the user is gone (same reasoning as RibbonItemDismissal in
    Block 1A).
    """
    __tablename__ = "ribbon_category_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "category",
                         name="uq_ribbon_pref_user_category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # One of the seven RIBBON_CATEGORIES slugs (app/services/ribbon.py).
    # Validated against that set in the collapse-toggle endpoint.
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    is_collapsed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False)


# Phase 2 / Block 1 precondition (samai spec, Sam 1C §13 Path A
# 2026-05-14). Valid-value constants for ScheduledEvent — String columns
# (not native ENUM, same call as Task / SalesInsight / BriefFeedback:
# SQLite-compat + simpler migrations). The Block 2 admin form, the 1C
# ribbon adapter, and the model tests all validate against these single
# sources.
_VALID_EVENT_STORES = {"tomball", "copperfield", "both"}
_VALID_EVENT_CATEGORIES = {"catering", "event"}
_VALID_EVENT_STATUSES = {"scheduled", "confirmed", "completed", "cancelled"}


class ScheduledEvent(Base):
    """A catering or event on a store's calendar — the ribbon source for
    the Caterings (ribbon category 2) and Events (category 3) sections.

    Phase 2 / Block 1 precondition (samai spec, Sam 1C §13 Path A
    2026-05-14). Ships the model + migration only, so 1C's adapter has a
    real model to read instead of an undefined dependency, and Block 1
    doesn't launch with 2 of 7 ribbon categories permanently empty.

    Empty-but-populatable from day one: the table ships empty; Block 2's
    admin form fills it; the 1C adapter renders whatever is in it.
    Explicitly NOT in this precondition: the Block 2 admin form, the 1C
    adapter, ezCater-webhook auto-population, and created_by_user_id
    (a Block 2 admin-form concern, not a ribbon-rendering minimum field).

    NOT an audit log — ScheduledEvent rows are mutable operational
    records (an event gets confirmed, rescheduled, cancelled), so there
    is deliberately no before_delete listener / append-only constraint.

    store / category / status are String, validated application-side
    against the _VALID_EVENT_* constants above.
    """
    __tablename__ = "scheduled_events"
    __table_args__ = (
        # The 1C ribbon query is "upcoming scheduled/confirmed events for
        # this viewer's store" — hits exactly these three columns.
        Index("ix_scheduled_events_ribbon",
              "store", "status", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # When the event starts.
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True)
    # When it ends; NULL = point-in-time (a catering delivery vs a
    # spirit night that spans hours). 1C uses it for the ribbon
    # relevance window.
    scheduled_end_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False)


# Phase 2 / Block 1A (samai spec, 2026-05-14). Valid-value constants for
# Task — String columns (not native ENUM, same call as ScheduledEvent /
# BriefFeedback / SalesInsight: SQLite-compat + simpler migrations).
# The create/reassign routes + the tests validate against these single
# sources.
_VALID_STORE_SCOPES = {"tomball", "copperfield", "both", "none"}
_VALID_CATEGORIES = {
    "vendor", "catering", "event", "employee",
    "maintenance", "sales", "general",
}
# TaskAuditLog.action values — fully defined in 1A so the table is
# stable, but 1A's own routes only emit "created" + "reassigned". The
# other values are emitted by the sub-blocks that own those write paths
# (completed/dismissed → 1D, escalated → 1E, assigned → Block 3B
# delegation-approval).
_VALID_TASK_ACTIONS = {
    "created", "assigned", "completed",
    "dismissed", "reassigned", "escalated",
}


class Task(Base):
    """A unit of operational work — owned, assignable, escalatable,
    audited. The data foundation of the Block 1 ribbon system.

    Phase 2 / Block 1A (samai spec, 2026-05-14). 1A makes tasks exist,
    be owned, be reassigned, and be audited; it does NOT render a
    ribbon (1B), route ribbon content (1C), write the X/Check controls
    (1D), or run the escalation cron (1E). The completed_at /
    completed_by_user_id columns are defined here but written by 1D's
    Check handler; escalated_to_user_id / escalated_at are written by
    1E's escalation cron.

    No hard-delete path for Task in v1 — tasks are completed
    (completed_at set), not deleted. There is no DELETE route and no
    before_delete listener on Task itself, but TaskAuditLog's
    ondelete=RESTRICT FK makes a Task with audit history effectively
    undeletable, which is the intended property for an operational
    audit trail.
    """
    __tablename__ = "tasks"
    __table_args__ = (
        # The hot ribbon query: "my open tasks" (owner = me AND
        # completed_at IS NULL). Composite covers it.
        Index("ix_tasks_owner_open", "owner_user_id", "completed_at"),
        # 1E's escalation cron scans WHERE deadline_at < now AND
        # completed_at IS NULL AND escalated_at IS NULL every 5 min.
        # Lay the index now so 1E doesn't add it as a separate migration.
        Index("ix_tasks_escalation_scan",
              "completed_at", "escalated_at", "deadline_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # owner / assigned_by → RESTRICT: NOT NULL, and the codebase follows
    # archive-not-delete (users deactivated, never hard-deleted), so
    # RESTRICT should never fire — it's a documented safety net. If a
    # delete path is ever written it must reassign the task first.
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    assigned_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    # tomball | copperfield | both | none — validated against
    # _VALID_STORE_SCOPES in the route.
    store_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    # vendor | catering | event | employee | maintenance | sales |
    # general — validated against _VALID_CATEGORIES in the route.
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True)
    # completed_* written by 1D's Check handler, not 1A's routes.
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True)
    completed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # escalated_* written by 1E's escalation cron, not 1A's routes.
    escalated_to_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True)
    escalated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False)


class TaskAuditLog(Base):
    """Append-only audit trail for Task lifecycle events.

    Phase 2 / Block 1A. One row per state-changing event on a Task.
    The action enum is fully defined (see _VALID_TASK_ACTIONS) so the
    table is stable, but 1A's own routes only ever emit "created" and
    "reassigned" — the other values are emitted by the sub-blocks that
    own those write paths.

    task_id ondelete=RESTRICT (not CASCADE): a Task with audit rows
    cannot be DB-deleted — combined with archive-not-delete this makes
    Tasks immutable-once-audited, the right property for an operational
    audit trail. RESTRICT also avoids the ORM-event-vs-DB-cascade
    ambiguity (a DB-level CASCADE would bypass the before_delete event).
    actor_user_id ondelete=RESTRICT — same archive-not-delete reasoning;
    the actor on an audit row must stay resolvable.

    Append-only: a before_delete listener (below) raises RuntimeError.
    UPDATE is not blocked, but no write path UPDATEs an existing audit
    row — they only INSERT.
    """
    __tablename__ = "task_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    # created | assigned | completed | dismissed | reassigned |
    # escalated — see _VALID_TASK_ACTIONS + the spec §7 emission map.
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    # Per-action JSON payload — shape per spec §7.
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)


class RibbonItemDismissal(Base):
    """Per-user, per-day "not now" dismissal of a ribbon item.

    Phase 2 / Block 1A. 1A ships the model + migration only; the
    POST /partner/ribbon/dismiss + /check write routes are 1D.

    item_id is a POLYMORPHIC reference — it points at tasks.id,
    signals.id, or sales_insights.id (and scheduled_events.id once 1C's
    contract change lands) depending on item_type. It is deliberately
    NOT a DB-level FK (one column can't FK multiple tables);
    referential integrity is application-enforced in 1D's dismiss
    handler.

    Daily-reset semantics (the directive flagged this as "samai's
    call"): dismiss_day is a "YYYY-MM-DD" string, NOT a session_id. A
    manager X's a ribbon item, it's gone for today, it's back tomorrow
    morning so they re-triage it — matching the daily operational
    rhythm. The UniqueConstraint(user_id, item_type, item_id,
    dismiss_day) makes 1D's dismiss endpoint naturally idempotent
    (dismissing twice in one day is a harmless no-op).

    user_id ondelete=CASCADE — dismissals are ephemeral per-user UI
    state, meaningless once the user is gone (unlike Task ownership).
    """
    __tablename__ = "ribbon_item_dismissals"
    __table_args__ = (
        UniqueConstraint("user_id", "item_type", "item_id", "dismiss_day",
                         name="uq_ribbon_dismissal_per_day"),
        Index("ix_ribbon_dismissal_lookup", "user_id", "dismiss_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # task | signal | sales_insight (+ scheduled_event once 1C lands).
    item_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Polymorphic ref — see docstring; not a DB FK.
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # "YYYY-MM-DD" — the daily reset boundary. date.today().isoformat()
    # at write time (1D).
    dismiss_day: Mapped[str] = mapped_column(String(10), nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)


# Phase 2 / Block 1F (samai spec, 2026-05-14). Valid-value constants for
# SalesInsight — String columns (not native ENUM), same call as Task /
# BriefFeedback: SQLite-compat + simpler migrations. The 1F synthesis
# writer validates every produced row against these before insert.
_VALID_INSIGHT_CATEGORIES = {
    "weather", "events", "school_calendar", "traffic",
    "outage", "yoy_comparison", "ai_synthesized",
}
# tomball | copperfield | both — note: no "none" (contrast Task's
# _VALID_STORE_SCOPES). An insight always pertains to at least one store.
_VALID_INSIGHT_STORE_SCOPES = {"tomball", "copperfield", "both"}
_VALID_INSIGHT_SEVERITIES = {"info", "warn", "alert"}


class SalesInsight(Base):
    """A piece of time-bound external intelligence — weather, a local
    event, a school-calendar day, traffic, an outage — synthesized
    daily and rendered in the ribbon's Sales category.

    Phase 2 / Block 1F (samai spec, 2026-05-14). 1F is the PRODUCER: a
    5am-CT cron pulls external sources, a Haiku-normalize → Opus-
    synthesize pipeline turns them into these rows. 1C's ribbon router
    is the CONSUMER (reads live rows for the viewer's store). 1F ships
    the model + the pipeline + the cron; it does NOT render the ribbon
    (1C), write the X/Check dismissal paths (1D), or run the expiry
    scan (1E).

    NOT an audit log — SalesInsight rows are ephemeral operational
    intelligence ("95F and humid today"), not history. They are
    DELETEd once past valid_until_at by 1E's every-5m cron
    (escalation.py leg 3). So there is deliberately no before_delete
    listener / append-only constraint — contrast TaskAuditLog.

    category / store_scope / severity are String, validated
    application-side against the _VALID_INSIGHT_* constants above.
    """
    __tablename__ = "sales_insights"
    __table_args__ = (
        # Both the 1C ribbon query ("live insights for this store") and
        # 1E's expiry scan ("rows past valid_until_at") hit these two
        # columns — one composite covers both.
        Index("ix_sales_insights_live", "valid_until_at", "store_scope"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    # When this insight stops being relevant — 1E's auto-expiry scans
    # this. NOT NULL: every insight must expire so nothing lingers in
    # the ribbon forever (1F spec §6 — end-of-day floor if none
    # otherwise derivable).
    valid_until_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True)
    # weather | events | school_calendar | traffic | outage |
    # yoy_comparison | ai_synthesized — _VALID_INSIGHT_CATEGORIES.
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    # tomball | copperfield | both — _VALID_INSIGHT_STORE_SCOPES.
    store_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    # info | warn | alert — _VALID_INSIGHT_SEVERITIES.
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    # Short, ribbon-renderable.
    headline: Mapped[str] = mapped_column(String(200), nullable=False)
    # Longer, for click-through.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional link to the source.
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # JSON list[int] of user_ids who PERMANENTLY dismissed this insight
    # via the ribbon Check control. 1F ships the column; 1D wires the
    # write path; 1C filters on it. NOT a User FK — it is a JSON list,
    # not a relation (1F spec §2.1). The daily-reset X dismissal is a
    # separate channel (RibbonItemDismissal).
    dismissed_by: Mapped[list | None] = mapped_column(JSON, nullable=True)


@_sa_event.listens_for(BriefFeedback, 'before_delete')
def _no_delete_brief_feedback(mapper, connection, target):
    raise RuntimeError(
        "BriefFeedback is append-only — rows cannot be deleted. "
        "Each row is panel-member input on a specific brief; the "
        "Round 1 → Round 2 aggregation doc derives from these rows. "
        "Mistakes are corrected by appending a follow-up row "
        "(submitted_via still indicates source), not by deleting."
    )


@_sa_event.listens_for(TaskAuditLog, 'before_delete')
def _no_delete_task_audit(mapper, connection, target):
    raise RuntimeError(
        "TaskAuditLog is append-only — task history cannot be deleted. "
        "Corrections are made by appending a new audit row, not by "
        "removing one."
    )


# ---- Sam Chat — standalone /sam/chat surface (Sam request 2026-05-14) ----
# A dedicated chat page for Sam (partner) to converse with Claude
# through the Sam Chat AI surface — no agentic context, no Cenas Kitchen
# system prompt. Deliberately ISOLATED from the agentic pipeline: no FK
# to User (the route is hard-gated to SAM_CHAT_USER_ID), and no
# reads/writes to AgentChatMessage / AgentActionLog / any Phase 2 Block
# 3 table. Distinct from the agent Developer Chat and from Block 3's
# manager-facing in-app agent.
_VALID_SAM_CHAT_ROLES = {"user", "assistant", "system", "dck", "cena", "aick"}


class SamChatSession(Base):
    """One Sam Chat conversation thread.

    NOT an audit log — sessions are mutable operational records (title
    editable, archivable), so there is deliberately no append-only
    constraint. No per-row owner FK: the /sam/chat surface is
    hard-gated to a single user (SAM_CHAT_USER_ID), so every row
    belongs to Sam by construction.
    """
    __tablename__ = "sam_chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    # Bumped on every new message — the history sidebar orders by this.
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    # Auto-generated from the first user message (~60 chars), editable.
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_archived: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False)


class SamChatMessage(Base):
    """One message in a SamChatSession — user, assistant, or system.
    The app reconstructs model context from these rows on each turn.

    cost_* columns are populated on assistant messages from the
    response usage block, feeding the live session-cost + 30-day-total
    displays. Attachments are NOT persisted here (per the Sam Chat
    model spec): images/PDFs go base64 into the send-time API payload,
    text files are read into `content`. Consequence: a reloaded
    session's history is text-only for past attachment turns —
    flagged for samai's review; an `attachments` JSON column is the
    fast-follow if persistence is wanted.

    session_id ondelete=CASCADE — messages are meaningless without
    their session; v1 has no delete-session route (sessions are
    archived, not deleted) but the cascade is the correct safety
    declaration.
    """
    __tablename__ = "sam_chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sam_chat_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True)
    # user | assistant | system — validated against _VALID_SAM_CHAT_ROLES.
    role: Mapped[str] = mapped_column(String(12), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Which Claude model produced an assistant message; NULL on
    # user/system rows.
    model: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cost_input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True)
    cost_output_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True)
    # Anthropic prompt-cache token counters from the response usage block.
    # cost_input_tokens covers only the UNCACHED portion; cache_creation
    # is what was written to cache (paid at 2x normal input rate for the
    # 1h ephemeral TTL set in cena_gateway.py), cache_read is what was
    # served from cache (paid at 0.10x normal input rate).
    cost_cache_creation_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True)
    cost_cache_read_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(
        Numeric(10, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)


# Sam's TODO list under /sam/chat (Sam directive 2026-05-23 #563).
# Sam adds items, the team works them top-down — Cena cannot skip,
# must complete the top item before moving to the next. Reorderable
# by Sam via up/down. ALL fields Sam-filled (no auto-default for
# date_added per Sam's literal "everything has to be filled out by me").
#
# Lifecycle: position is 1-based; smaller = higher priority. When Sam
# marks an item done, status flips to 'done' and the renumber-active
# pass on the route pulls remaining active positions tight (no holes).
class SamChatTodo(Base):
    """One Sam-authored TODO item under /sam/chat.

    Top row (smallest position among status='active') is the current
    focus the team is required to work on next. UI prevents skipping;
    Cena's get_current_todo tool returns ONLY this row so the agent
    sees a single priority at a time.
    """
    __tablename__ = "sam_chat_todos"

    id: Mapped[int] = mapped_column(primary_key=True)
    details: Mapped[str] = mapped_column(Text, nullable=False)
    # Sam-typed; not auto-defaulted. Per Sam #563: "everything has to
    # be filled out by me" — the form refuses empty date_added.
    date_added: Mapped[date] = mapped_column(Date, nullable=False)
    date_completed: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 1-based ordering; smaller = higher priority. Renumbered tight
    # on every move + on every status change.
    position: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 'active' | 'done'. Validated application-side.
    status: Mapped[str] = mapped_column(String(12), nullable=False,
                                        default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False)


_VALID_SAM_CHAT_TODO_STATUS = frozenset({"active", "done"})


class SamChatSuggestion(Base):
    """One improvement suggestion surfaced from Sam Chat/review messages.

    Suggestions are deliberately separate from SamChatTodo: a row here is
    an observation that Sam can approve or deny, not work the team is
    automatically allowed to start.
    """
    __tablename__ = "sam_chat_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sam_chat_sessions.id", ondelete="SET NULL"),
        nullable=True, index=True)
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("sam_chat_messages.id", ondelete="SET NULL"),
        nullable=True, index=True)
    source_label: Mapped[str | None] = mapped_column(String(160),
                                                     nullable=True)
    summary: Mapped[str] = mapped_column(String(220), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'pending' | 'approved' | 'denied'. Validated application-side.
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default="pending", index=True)
    created_by: Mapped[str | None] = mapped_column(String(80),
                                                   nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                        nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False)


_VALID_SAM_CHAT_SUGGESTION_STATUS = frozenset(
    {"pending", "approved", "denied"})


class DevChatTodo(Base):
    """Sam-authored TODO under /partner/developer/chat (Sam #1066, 2026-05-26).

    Distinct from SamChatTodo (which is the cena-page focus-queue) — this
    one is the dev-chat shared work list. Items are assignable to a
    specific agent (aick / ck / cena) or left unassigned (any can grab).
    The agent who is the assignee picks the item up when they refresh
    the dev chat page.
    """
    __tablename__ = "dev_chat_todos"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'aick' | 'ck' | 'cena' | NULL (= any agent).
    assigned_to: Mapped[str | None] = mapped_column(
        String(40), nullable=True, index=True)
    # 'open' | 'in_progress' | 'done' | 'cancelled'. Validated app-side.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", index=True)
    # Author label — usually 'sam' for Sam-typed items; agents can also
    # leave themselves a TODO.
    created_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False)


_VALID_DEV_CHAT_TODO_STATUS = frozenset(
    {"open", "in_progress", "done", "cancelled"})
_VALID_DEV_CHAT_TODO_ASSIGNEES = frozenset({"aick", "ck", "cena", "samai"})


# ---- Block 1J — AmbientSignal data plane (samai spec, 2026-05-14) ----
# The in-app data-plane / control-plane separation: six per-source
# /cron/refresh-* crons WRITE AmbientSignal rows; the 1C ribbon router
# + the /cron/sales-insights pipeline READ them. One producer table,
# many consumers. Valid-value sets are validated application-side (in
# ambient_signal_upsert) — String columns, not native ENUM, same call
# as Task / SalesInsight.
_VALID_AMBIENT_SOURCES = {
    "weather", "events", "outages",
    "catering_pipeline", "vendor_status", "traffic",
}
# The ribbon category an ambient signal feeds (1J spec §2 / §7).
_VALID_AMBIENT_CATEGORIES = {"caterings", "events", "maintenance"}
_VALID_AMBIENT_STORE_SCOPES = {"tomball", "copperfield", "both"}
_VALID_AMBIENT_SEVERITIES = {"info", "warn", "alert"}
# AmbientSignalRun.status (1J spec §2.4 — Q3: no "skipped_unchanged").
_VALID_AMBIENT_RUN_STATUSES = {"success", "error", "partial"}


class AmbientSignal(Base):
    """One logical piece of external intelligence with a STABLE
    IDENTITY across refreshes — "Tomball weather today", "Astros home
    game 5/16", "outage near Copperfield".

    Phase 2 / Block 1J (samai spec, 2026-05-14). The data-plane row:
    six per-source /cron/refresh-* crons upsert these via
    ambient_signal_upsert(); the 1C ribbon router + the
    /cron/sales-insights pipeline read them.

    The id-stable contract (§2.2): when a cron re-pulls the same
    logical signal — same (source, signal_key) — with a fresh payload,
    the row is UPDATED IN PLACE and its id never changes. That is what
    makes a user's RibbonItemDismissal survive a payload refresh (§6,
    "the critical invariant"): the dismissal references (item_type,
    item_id), so a stable id keeps the (updated) row dismissed.

    NOT an audit log — like SalesInsight, an AmbientSignal is ephemeral
    operational intelligence, not history. There is deliberately no
    before_delete listener: expired rows (valid_until_at < now) are
    DELETEd by each cron's own per-source expiry sweep (§2.3).

    No dismissed_by column (contrast SalesInsight): ambient signals get
    daily X-dismiss only (RibbonItemDismissal) and age out on their own
    via valid_until_at — can_check is False for them (§7.2).
    """
    __tablename__ = "ambient_signals"
    __table_args__ = (
        # One row per logical signal. THIS is what makes "re-pull the
        # same signal" deterministically find the existing row instead
        # of inserting a duplicate — the id-stable upsert's lookup key.
        UniqueConstraint("source", "signal_key",
                         name="uq_ambient_signal_identity"),
        # Hot reads: the 1C ribbon live-query (valid_until_at,
        # store_scope) and each cron's own expiry sweep (source,
        # valid_until_at). Mirrors SalesInsight's ix_sales_insights_live.
        Index("ix_ambient_signals_live", "valid_until_at", "store_scope"),
        Index("ix_ambient_signals_source_expiry",
              "source", "valid_until_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # weather | events | outages | catering_pipeline | vendor_status |
    # traffic — _VALID_AMBIENT_SOURCES.
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # Source-scoped LOGICAL identity (§2.1) — derived from the signal's
    # identity, never its content. Two pulls of the same logical signal
    # produce the same signal_key even when the payload changed.
    signal_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # The structured signal content.
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    # sha256 of the canonical payload serialization (§2.1) — the change
    # detector. Set by _ambient_payload_hash() so all six crons hash
    # identically.
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # tomball | copperfield | both — _VALID_AMBIENT_STORE_SCOPES.
    store_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    # caterings | events | maintenance — the ribbon category this
    # signal feeds (§7). _VALID_AMBIENT_CATEGORIES.
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    # info | warn | alert — _VALID_AMBIENT_SEVERITIES.
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    # When the signal ages out (§2.3) — NOT NULL so nothing lingers on
    # the ribbon forever. Each cron sweeps its own source's rows past
    # this; DELETE, not flag-flip.
    valid_until_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False)
    # The last cron run that re-confirmed this signal still exists —
    # bumped on every upsert (created / updated / unchanged), so an
    # unchanged-but-still-live signal isn't mistaken for stale (§2.2).
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)


class AmbientSignalRun(Base):
    """One row per per-source cron run — the operational record of
    "did each refresh cron run, and what did it do."

    Phase 2 / Block 1J (samai spec §2.4). The cron endpoint returns
    this run summary as its JSON response so a manual trigger is
    inspectable (same shape as the 1E / 1F cron summaries).
    """
    __tablename__ = "ambient_signal_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Which /cron/refresh-* produced this run.
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    # success | error | partial — _VALID_AMBIENT_RUN_STATUSES. (§2.4
    # Q3: no "skipped_unchanged" — an all-no-op cycle is still a
    # successful run; signals_unchanged carries that detail per-run.)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    signals_created: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False)
    signals_updated: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False)
    signals_unchanged: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False)
    # Rows swept by this run's per-source expiry sweep.
    signals_expired: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False)
    # Set when status != success.
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)


# ============================================================
# Phase 2 / Cena — Sam's personal operational AI surface
# (PART 4 of Sam's 2026-05-15 directive)
# ============================================================

# Tool-name vocabulary. Mirrors the methods exposed in cena_gateway.py
# on AiCk. Not enum-enforced at the column level so new tools can be
# added without a migration; the gateway side validates before logging.
_VALID_CENA_ACTION_TYPES = {
    "shell_execute",
    "git_commit", "git_push",
    "render_deploy", "render_env_get", "render_env_set",
    "file_read", "file_write", "file_delete",
    "db_query", "db_execute",
    "cf_api_call",
    "telegram_send",
    "post_to_dev_chat", "read_dev_chat",
    "read_agent_chat_history",
    "anthropic_chat",
}


class CenaActionLog(Base):
    """One row per tool invocation Cena makes through the gateway.

    The gateway (cena_gateway.py on AiCk) POSTs to /sam/cena/log
    after each tool call returns (success or failure). /sam/cena-audit/
    renders this table in reverse-chronological order for review.

    Fields follow Sam's PART 4 spec (id, action_type, parameters,
    result, timestamp, cena_session_id), with operational additions:
    started_at + finished_at (latency), success + error_text (failure
    triage), message_id (which user turn drove the action).
    """
    __tablename__ = "cena_action_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The tool that was invoked. See _VALID_CENA_ACTION_TYPES.
    action_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True)

    # Arguments passed in. JSON so any tool's shape can be persisted.
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Return value (success) or partial result (on error).
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    success: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Bracketing timestamps. started_at indexed for the most common
    # query (reverse-chronological feed at /sam/cena-audit/).
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)

    # Which Sam Chat session + turn triggered this action. Nullable
    # because Cena could also run scheduled / ambient actions later
    # that are not tied to a chat turn.
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sam_chat_sessions.id", ondelete="SET NULL"),
        nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("sam_chat_messages.id", ondelete="SET NULL"),
        nullable=True, index=True)


class VendorRecentOrder(Base):
    """One row per email-parsed vendor order (Webstaurant, Performance
    Food, Restaurant Depot, Specs — Sam #837 items 9-12).

    The existing produce_ingest.py IMAP poller on orders@cenaskitchen.com
    extends to handle these four senders. Each parsed email lands here
    so the /<store>/vendors/<vendor>/recent-orders page can render the
    rolling order list.

    Parsing is per-vendor and shipped one vendor at a time as Sam
    forwards sample emails. Until a parser exists, the email body is
    saved verbatim in raw_body so we don't lose data.
    """
    __tablename__ = "vendor_recent_orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 'webstaurant' | 'performance-food' | 'restaurant-depot' | 'specs'
    vendor: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # Which store the order belongs to — derived from From/To/CC matching
    # or the vendor account that received the email. 'tomball' / 'copperfield'.
    store_scope: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)

    order_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_or_caterer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Structured order items + tracking links, JSON-encoded; shape varies
    # per vendor parser.
    items_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tracking_links_json: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Raw email fields preserved so a future parser update can re-parse.
    source_email_mid: Mapped[str | None] = mapped_column(String(80), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    from_addr: Mapped[str | None] = mapped_column(String(200), nullable=True)
    raw_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_status: Mapped[str] = mapped_column(
        String(20), default="unparsed", nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)


class ManagementEmailMessage(Base):
    """Imported dashboard email row.

    Body text and attachment metadata are cached so management can search/read
    the recent mailbox quickly. Attachment bytes and replies still flow through
    the live mailbox adapter.
    """
    __tablename__ = "management_email_messages"
    __table_args__ = (
        UniqueConstraint(
            "account_key", "provider_message_id",
            name="uq_management_email_account_message",
        ),
        Index("ix_management_email_account_date", "account_key", "date_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    account_address: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mailbox: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    from_addr: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_addr: Mapped[str | None] = mapped_column(Text, nullable=True)
    date_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    date_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    unread: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attachments_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message_id_header: Mapped[str | None] = mapped_column(String(500), nullable=True)
    references_header: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ManagementEmailSyncState(Base):
    """One row per connected management mailbox sync cursor."""
    __tablename__ = "management_email_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    account_address: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    initial_sync_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_full_import_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_incremental_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SamChatAttachment(Base):
    """One row per file Sam attached to a /sam/chat user turn — images
    and PDFs base64-encoded for storage. Per Sam #837 item 5 (vision
    parity for dev-team agents): cena saw images at the API layer but
    they were thrown away after the turn, leaving aick/ck/samai blind
    on the read side. Persisting them here lets the /sam/cena/sam-chat
    read endpoint surface attachment IDs + a download URL so any agent
    polling /sam/chat can fetch and process the same images.

    Storage shape: inline base64 in `data_base64`. Capped at 5MB per
    file in the POST handler (post-base64 inflation ~6.7MB DB cell).
    Larger files would belong on disk / object storage; for the
    screenshot + small-PDF workflow this is enough."""
    __tablename__ = "sam_chat_attachments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("sam_chat_messages.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    data_base64: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)


class CenaUsageLog(Base):
    """Per-turn token + cost telemetry for the cena gateway.

    One row per Anthropic API turn cena runs. Captures input/output/cache
    token counts so we can roll up "what did cena cost me today" — the
    #11 ask from Sam /sam/chat session 13.

    Cost is computed at query time from the token counts using
    claude-opus-4-7 pricing (input $15/MTok, output $75/MTok, cache_read
    $1.50/MTok, cache_write $18.75/MTok) so price changes don't require
    a backfill.
    """
    __tablename__ = "cena_usage_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    model: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    in_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    out_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    tool_rounds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sam_chat_sessions.id", ondelete="SET NULL"),
        nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("sam_chat_messages.id", ondelete="SET NULL"),
        nullable=True, index=True)


class InHouseCateringQuote(Base):
    """One row per In-House Catering quote built by Cenas staff.

    Sam #837 item 16 + cena #1031: staff-facing tool that lets a manager
    build a custom-priced quote off the (zeroed) Cenas Fajitas Tomball
    menu. Two checkout flows:
      - Quote   → email summary to customer for approval
      - Payment → Pay Now (links to new Order row with the In-House
                 indicator set) or Pay Later (placeholder fields per
                 Sam #1041 — no PCI exposure, free-text payment notes)

    items_json: serialized list[{"slug","label","qty","unit_price",
                                 "line_total","modifiers": [...]}]
    """
    __tablename__ = "in_house_catering_quotes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    store_scope: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    customer_name:  Mapped[str | None] = mapped_column(String(200), nullable=True)
    customer_email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    customer_phone: Mapped[str | None] = mapped_column(String(50),  nullable=True)
    event_date:     Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    event_address:  Mapped[str | None] = mapped_column(String(500), nullable=True)
    guest_count:    Mapped[int | None] = mapped_column(Integer, nullable=True)

    items_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    subtotal:   Mapped[float | None] = mapped_column(Float, nullable=True)
    notes:      Mapped[str | None] = mapped_column(Text, nullable=True)

    # status moves: draft → sent | pay_now_pending | pay_later_pending →
    #               paid | canceled
    status: Mapped[str] = mapped_column(String(40), default="draft", nullable=False, index=True)

    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Pay Now path links to the ezOrder row created from this quote.
    ezorder_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)

    # Pay Later path is a free-text bucket per Sam #1041 — staff fills
    # check #, account reference, paypal handle, or whatever payment
    # mechanism we eventually wire. No CC validation, no PCI exposure.
    payment_method:  Mapped[str | None] = mapped_column(String(80),  nullable=True)
    payment_details: Mapped[str | None] = mapped_column(Text, nullable=True)


# ============================================================
# MANAGER PAGES — 14 log-entry tables sharing one shape
# (Sam #1102 + cena #1111 — approach A text-heavy v1).
#
# Each page is a simple "title + body + author + date" log. Tables
# share columns via the ManagerLogMixin below so all 14 routes can
# share rendering logic. Audience gate applies uniformly via existing
# roles (gm / km / asst_km / foh_manager) — no new helper or hierarchy
# per Sam #1112 + #1115. Store-scoped.
# ============================================================
class ManagerLogMixin:
    """Shared shape for all manager-section log-entry tables."""
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    store_scope: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional per-row type tag (e.g. Incident Reports = injury / theft /
    # complaint). NULL for pages that don't use type tags (Daily Manager Log).
    type_tag: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)


class DailyManagerLog(ManagerLogMixin, Base):
    __tablename__ = "manager_daily_log"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    # v3 design fields (dck build-order #2, 2026-05-19). The Daily
    # Manager Log gets a richer structured entry than the shared
    # ManagerLogMixin shape — real columns instead of a type_tag
    # composite (samai #3.49 flagged the 'STAFF:URGENT' composite
    # display bug; discrete columns avoid it). All have defaults so
    # the additive migration is safe on existing rows.
    module: Mapped[str] = mapped_column(String(20), nullable=False, default="general")
    subject: Mapped[str] = mapped_column(String(24), nullable=False, default="general")
    issue: Mapped[str] = mapped_column(String(16), nullable=False, default="general")
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="low")
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, default=_local_today)
    show_on_roster: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    author: Mapped["User | None"] = relationship("User")
    images: Mapped[list["DailyLogEntryImage"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan",
        order_by="DailyLogEntryImage.position")


class DailyLogEntryImage(Base):
    """Image attached to a Daily Manager Log entry (dck build-order #2,
    2026-05-19 — the v3 design's modal image upload + detail-pane
    gallery). Mirrors the DeveloperChatAttachment pattern: file on disk,
    row holds the path; served via the daily-log image route. The .url
    property is what the template's e.images|map('url') consumes."""
    __tablename__ = "daily_log_entry_image"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("manager_daily_log.id", ondelete="CASCADE"),
        nullable=False, index=True)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)

    entry: Mapped["DailyManagerLog"] = relationship(back_populates="images")

    @property
    def url(self) -> str:
        # Store-prefixed so it resolves under the current /<store>/ scope.
        # g.current_store is set by store_routes' url_value_preprocessor
        # during the request the template renders in.
        from flask import g as _g
        store = getattr(_g, "current_store", None) or "partner"
        return f"/{store}/manager/daily-log/image/{self.id}"


class ShiftHandoff(ManagerLogMixin, Base):
    __tablename__ = "manager_shift_handoff"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


class IncidentReport(ManagerLogMixin, Base):
    __tablename__ = "manager_incident_report"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    # v3 design fields (ck build-order Sam #10:11/#10:15 2026-05-19 —
    # convert v1 text-heavy shell to the samai #6:27 + dck #6:39 v3
    # design). Severity / status / type surface in the dashboard cards
    # + filter chips. report_id = IR-YYYY-MMDD-NNN human label.
    # archived_at moves a row out of the rolling 30-day window into
    # the searchable archive view.
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="moderate")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    # Was VARCHAR(40); bumped to (200) on 2026-05-20 to fit the
    # multi-select CSV that the v4 incident-type grid now produces
    # (Sam #5:08). Backfill ALTER COLUMN lives in app/__init__.py.
    incident_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    report_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # v4 form fields (Sam dev chat #4:22 + #4:23 spec 2026-05-20; ck
    # build #4:32). The rich "File new incident" form collects discrete
    # what/when/where/who fields plus a lock-on-submit flag that freezes
    # the row into an immutable Original Record. body (from
    # ManagerLogMixin) holds the longform description; immediate_action
    # is its own column so the audit trail keeps them separate.
    date_of_incident: Mapped[date | None] = mapped_column(Date, nullable=True)
    time_of_incident: Mapped[time | None] = mapped_column(Time, nullable=True)
    location_in_store: Mapped[str | None] = mapped_column(String(200), nullable=True)
    people_involved: Mapped[str | None] = mapped_column(Text, nullable=True)
    witnesses: Mapped[str | None] = mapped_column(Text, nullable=True)
    immediate_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    author: Mapped["User | None"] = relationship("User")


class SupplyRequest(ManagerLogMixin, Base):
    __tablename__ = "manager_supply_request"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


class DailyGoals(ManagerLogMixin, Base):
    __tablename__ = "manager_daily_goals"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


class StaffFeedback(ManagerLogMixin, Base):
    __tablename__ = "manager_staff_feedback"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


class PreShiftChecklist(ManagerLogMixin, Base):
    __tablename__ = "manager_pre_shift_checklist"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


class CloseOfDayAudit(ManagerLogMixin, Base):
    __tablename__ = "manager_close_of_day_audit"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


class RecipePage(ManagerLogMixin, Base):
    """Note: also stores the 14 recipe PDFs Sam attached at /sam/chat
    #1130-#1133 + #1134 (those are Cold/Hot/Marinated/Sauce recipes).
    For v1, recipes are simple title + body text entries; uploaded PDF
    referencing is a follow-up."""
    __tablename__ = "manager_recipe_page"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


class AttendanceTracking(ManagerLogMixin, Base):
    __tablename__ = "manager_attendance_tracking"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


class InterviewSurface(ManagerLogMixin, Base):
    __tablename__ = "manager_interview_surface"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


class TrainingRecord(ManagerLogMixin, Base):
    __tablename__ = "manager_training_record"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


class MaintenanceRequest(ManagerLogMixin, Base):
    __tablename__ = "manager_maintenance_request"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


class EmployeeCounseling(ManagerLogMixin, Base):
    __tablename__ = "manager_employee_counseling"
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


# ============================================================
# ATTENDANCE TRACKING v3 — manager-operated daily time clock.
# Sam #10:14 (dck build). The shared AttendanceTracking log table
# (ManagerLogMixin) is the wrong shape for a per-employee-per-day
# clock board, so v3 gets its own schema: one AttendanceShift row
# per teammate per day + an AttendanceEvent timeline. No external
# integration — the manager builds the roster + drives every punch.
# ============================================================
class AttendanceShift(Base):
    """One teammate's shift for one day — the v3 Attendance Tracking
    board. Status machine: scheduled -> clocked-in/late -> break ->
    out; no-show / callout are off-states."""
    __tablename__ = "manager_attendance_shift"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    store_scope: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    entry_date: Mapped[date] = mapped_column(
        Date, nullable=False, default=_local_today, index=True)

    employee_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role_title: Mapped[str | None] = mapped_column(String(60), nullable=True)
    section: Mapped[str] = mapped_column(String(8), nullable=False, default="boh")  # boh | foh
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)

    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    clock_in: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    clock_out: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # scheduled | clocked-in | late | no-show | callout | break | out
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    late_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    events: Mapped[list["AttendanceEvent"]] = relationship(
        back_populates="shift", cascade="all, delete-orphan",
        order_by="AttendanceEvent.at")


class AttendanceEvent(Base):
    """A timeline entry on an AttendanceShift — clock punch, late log,
    callout, break, early-out, or free note."""
    __tablename__ = "manager_attendance_event"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("manager_attendance_shift.id", ondelete="CASCADE"),
        nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    # in | out | late | callout | break | no-show | early-out | note
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="note")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    counts_as_occurrence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    manager_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    shift: Mapped["AttendanceShift"] = relationship(back_populates="events")


class UniformIssue(Base):
    """One uniform item issued to one employee, logged by a manager."""
    __tablename__ = "manager_uniform_issue"
    __table_args__ = (
        Index("ix_uniform_issue_emp_store", "employee_name", "store_scope"),
        Index("ix_uniform_issue_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    store_scope: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    employee_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    employee_role: Mapped[str | None] = mapped_column(String(80), nullable=True)
    item_key: Mapped[str] = mapped_column(String(32), nullable=False)
    item_label: Mapped[str] = mapped_column(String(80), nullable=False)

    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    manager_name: Mapped[str | None] = mapped_column(String(120), nullable=True)


class ManagerReportEdit(Base):
    """Audit row for a manager correction made from Manager Reports."""
    __tablename__ = "manager_report_edit"
    __table_args__ = (
        Index("ix_manager_report_edit_target", "target_type", "target_id"),
        Index("ix_manager_report_edit_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    manager_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)


# ============================================================
# PREP LIST v3 — kitchen's daily prep board (Sam, dck build).
# PrepItem = the stable master list (hot/cold/chop × item/sauce).
# PrepEntry = one working row per item per day, so each day is its
# own lockable productivity record. No external integration — the
# manager drives every selection/assignment/status. recipe_id links
# a PrepItem to the existing Recipe table so the detail panel can
# auto-pull the ingredient breakdown.
# ============================================================
class PrepItem(Base):
    """A master prep-list item. Shown every day; PrepEntry rows hang
    off it per day. category = hot|cold|chop, kind = item|sauce."""
    __tablename__ = "kitchen_prep_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(8), nullable=False, default="hot")   # hot | cold | chop
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default="item")      # item | sauce
    recipe_id: Mapped[int | None] = mapped_column(
        ForeignKey("recipes.id", ondelete="SET NULL"), nullable=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    store_scope: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)


class PrepEntry(Base):
    """One PrepItem's working state for one day — the v3 Prep List
    board. status machine: selected -> assigned -> in-progress ->
    done. locked freezes the row at end-of-day submission."""
    __tablename__ = "kitchen_prep_entry"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    entry_date: Mapped[date] = mapped_column(
        Date, nullable=False, default=date.today, index=True)
    store_scope: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    prep_item_id: Mapped[int] = mapped_column(
        ForeignKey("kitchen_prep_item.id", ondelete="CASCADE"),
        nullable=False, index=True)

    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_hand: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prep_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assignee_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    helper_names: Mapped[str | None] = mapped_column(Text, nullable=True)
    # selected | assigned | in-progress | done
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="selected")
    batch_size: Mapped[str | None] = mapped_column(String(16), nullable=True)   # single | double
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_by_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    item: Mapped["PrepItem"] = relationship()


class PrepAuditLog(Base):
    """Append-only audit events for Prep List changes. This feeds the
    kitchen Developer tab without making the daily entry row carry every
    historical action inline."""
    __tablename__ = "kitchen_prep_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("kitchen_prep_entry.id", ondelete="SET NULL"),
        nullable=True, index=True)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    store_scope: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    prep_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("kitchen_prep_item.id", ondelete="SET NULL"),
        nullable=True, index=True)
    item_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class PrepTeamMember(Base):
    """Saved prep roster for the Kitchen Prep Team tab.

    Assignments already store names, so the roster is name-based too. The
    active employee table remains the source of available names.
    """
    __tablename__ = "kitchen_prep_team_member"
    __table_args__ = (
        UniqueConstraint("store_scope", "name",
                         name="uq_kitchen_prep_team_member_scope_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store_scope: Mapped[str] = mapped_column(
        String(50), nullable=False, default="both", index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False)


# ============================================================
# INTERVIEW TRACKER — candidate hiring pipeline (Sam #5:48, dck
# render + aick build). NEW feature, not the older text-shell
# InterviewSurface manager page. One Candidate row per applicant;
# `stage` walks the 4-stage pipeline applied -> first -> second ->
# hired. Source fields only — the route derives all display fields
# (initials, meta/tag labels, timeline) so the model stays a clean
# record of the candidate, not the rendering.
# ============================================================
class Candidate(Base):
    """An applicant in the Interview Tracker pipeline. stage is the
    pipeline position: applied | first | second | hired."""
    __tablename__ = "interview_candidates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str | None] = mapped_column(String(80), nullable=True)
    store: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # applied | first | second | hired
    stage: Mapped[str] = mapped_column(String(16), nullable=False, default="applied")
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    position: Mapped[str | None] = mapped_column(String(120), nullable=True)
    desired_wage: Mapped[str | None] = mapped_column(String(60), nullable=True)
    availability: Mapped[str | None] = mapped_column(Text, nullable=True)
    experience: Mapped[str | None] = mapped_column(Text, nullable=True)
    referred_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    urgent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class WebsiteFormSubmission(Base):
    """First-party public website form submissions.

    Captures the new cenaskitchen.com forms that used to post to Formspree:
    career applications, spirit day requests, donation requests, catering
    requests, and contact/feedback messages. Flexible fields stay in JSON so
    the public form can evolve without a schema change, while common columns
    support the partner dashboard filters.
    """

    __tablename__ = "website_form_submissions"
    __table_args__ = (
        Index("ix_website_forms_type_created", "form_type", "created_at"),
        Index("ix_website_forms_location_type", "location", "form_type"),
        Index("ix_website_forms_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    form_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="new", nullable=False)
    source_page: Mapped[str | None] = mapped_column(String(255), nullable=True)

    location: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    position: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    subject: Mapped[str | None] = mapped_column(String(160), nullable=True)
    applicant_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    organization: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(60), nullable=True)

    fields: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    attachments: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Locations explicitly shared by a full-access reviewer. Empty means
    # managers cannot see the submission yet.
    shared_locations: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    shared_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    shared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    referrer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status_changed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ============================================================
# RECIPES — Sam /sam/chat #1130-#1133 attached 14 PDFs; spec at
# cena #1209 / Sam dev #3074. Single table; batch sizes + ingredients
# stored as JSON for flexibility.
# ============================================================
class Recipe(Base):
    __tablename__ = "recipes"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    code: Mapped[str | None] = mapped_column(String(20), index=True, nullable=True)
    category: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), index=True, nullable=False)
    prep_time: Mapped[str | None] = mapped_column(String(80), nullable=True)
    shelf_life: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # samai (Sam Kitchen-dashboard batch): Spanish counterparts so the
    # recipe cards can toggle EN/ES. prep_time/shelf_life hold EN.
    prep_time_es: Mapped[str | None] = mapped_column(String(80), nullable=True)
    shelf_life_es: Mapped[str | None] = mapped_column(String(80), nullable=True)

    spanish_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    english_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingredients_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch_sizes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


# ============================================================
# FRESH FOOD — Sam /sam/chat #1120-#1144. Cross-store visible
# (no store_scope filter on reads). Daily order header + per-item
# lines (INV / OR placed; SENT filled at fulfillment).
# ============================================================
class FreshFoodOrder(Base):
    __tablename__ = "fresh_food_order"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    placed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True)
    order_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    store_scope: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    placed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    placed_by_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False, index=True)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fulfilled_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    fulfilled_by_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sent_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class FreshFoodOrderLine(Base):
    __tablename__ = "fresh_food_order_line"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("fresh_food_order.id", ondelete="CASCADE"),
        nullable=False, index=True)
    item_slug: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    item_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    inv_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    or_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    sent_qty: Mapped[float | None] = mapped_column(Float, nullable=True)



# === docck v1 - multi-agent reliability monitor (Sam #1191, samai #1208 contracts) ===

class DocckAgent(Base):
    """Registry of agents docck monitors. Data-driven — adding an agent
    is an INSERT, not a code change. See Sam #1191 (multi-agent amendment).
    """
    __tablename__ = "docck_agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # 'cena', 'pwck'
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    machine_label: Mapped[str] = mapped_column(String(64), nullable=False)  # 'AiCk', 'Mini_IT13'

    # watchdog endpoint + auth
    watchdog_url: Mapped[str] = mapped_column(String(255), nullable=False)
    watchdog_secret_env_var: Mapped[str] = mapped_column(String(64), nullable=False)

    # heartbeat auth — stored as a custom-format hash (pbkdf2-sha256). Verify
    # with docck.security.check_hash(token, stored_hash).
    heartbeat_token_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # service config — JSON object mapping role → service name.
    # e.g. {"service": "cena_service", "gateway": "cena_gateway"}
    services_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    # restart sequence — JSON array of step dicts.
    # e.g. [{"action": "restart_service", "service_name": "cena_service", "wait_seconds": 30}, ...]
    restart_sequence_json: Mapped[list] = mapped_column(JSON, nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    alert_dev_chat: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    alert_telegram_threshold_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class DocckHeartbeat(Base):
    """Every heartbeat from every agent. Append-only.

    Insert-rate: 2 agents × 1 heartbeat / 30s = 4 inserts/min = ~5700/day.
    Retention: aggressive — purge anything older than 30 days via a
    nightly cron (TODO). For now, no purge.
    """
    __tablename__ = "docck_heartbeats"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # payload echoes (denormalized for easy querying without JSON parsing)
    agent_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    agent_state: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 'healthy' | 'degraded' | 'stopping'
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_active: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_anthropic_api_call_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    uptime_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    in_flight_requests: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # full body for forensics / future schema additions without migration
    extras: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_docck_hb_agent_received", "agent_id", "received_at"),
    )


class DocckRestartSequence(Base):
    """One row per restart-sequence run. Records start, end, outcome,
    which step recovered the agent (or 'escalated' if all exhausted).
    """
    __tablename__ = "docck_restart_sequences"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 'recovered' | 'escalated' | 'canceled'
    recovered_at_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(64), default="auto", nullable=False)  # 'auto' | 'admin'


class DocckRestartStep(Base):
    """One row per step within a restart sequence. Audit trail."""
    __tablename__ = "docck_restart_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    sequence_id: Mapped[int] = mapped_column(Integer, ForeignKey("docck_restart_sequences.id"), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # 'restart_service' | 'restart_services' | 'reboot_machine'
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # 'started' | 'watchdog_failure' | 'no_heartbeat_after_wait' | 'recovered'
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    watchdog_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DocckAlertSent(Base):
    """Every alert posted. Dedupe by (agent_id, dedupe_key) within a
    sliding window so we don't spam dev chat / Telegram for the same
    underlying condition.
    """
    __tablename__ = "docck_alerts_sent"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # null = docck-level
    severity: Mapped[str] = mapped_column(String(32), nullable=False)  # 'info' | 'warn' | 'urgent'
    channel: Mapped[str] = mapped_column(String(32), nullable=False)  # 'dev_chat' | 'telegram'
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class DocckCircuitBreaker(Base):
    """Per-agent circuit breaker. Tripped when too many failed recovery
    sequences accumulate within a window. Manually resettable via
    POST /docck/admin/<agent_id>/force_recovery.
    """
    __tablename__ = "docck_circuit_breaker"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    failed_sequence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    manually_tripped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class DocckTickLease(Base):
    """Singleton lease row (id=1) coordinating the self-tick background thread
    across gunicorn workers. Exactly one worker holds the lease and runs the
    monitoring evaluation; others stand by. Holder death -> lease expires (TTL
    90s) -> another worker takes over. Replaces DocckTickFirer (Sam #1257)."""
    __tablename__ = "docck_tick_lease"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    holder: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ============================================================
# Schedules V2 - Block 2 (Sam #1742). Employee scheduling identity +
# phone/SMS auth + position/tag taxonomy. Models owned by aick in one hand;
# ckai builds the auth endpoints (app/web/employee_auth.py) against this
# schema. Held on the schedules-v2-b2 branch until the B2 cross-review +
# samai gate, then merged to main (create_all materializes the tables).
# Employees are DISTINCT from the User/keypad-PIN auth system: they log in
# by phone + one-time SMS code, never a PIN, and never reach /partner/*.
# ============================================================


class Employee(Base):
    """A scheduling employee (the Sling-style workforce). Phone+SMS login."""

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # B3 migration map back to Sling; null for app-created employees
    sling_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    # Toast alignment (Bulletproof mapping, Sam #3250): direct connection
    # to Toast server GUID. Saves names to bypass Toast API failures.
    toast_employee_guid: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    toast_employee_name: Mapped[str | None] = mapped_column(
        String(150), nullable=True
    )
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    # E.164-ish; the SMS login identity. NULLABLE (B3, samai #1812): a Sling
    # employee may have no / blank / duplicate phone -> stored NULL (UNIQUE
    # permits multiple NULLs in SQLite), imported inactive + punch-listed; no
    # SMS login until a phone is set via the admin page.
    phone: Mapped[str | None] = mapped_column(
        String(32), unique=True, nullable=True, index=True
    )
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Roster edit-contact (2026-05-31, roster-edit branch): free-text mailing
    # address a manager can set/update from the Team roster. Nullable; the boot
    # ALTER adds it to the populated employees table (app/__init__.py), same
    # mechanism as store_key/user_id/session_version.
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Durable Toast identity (Sam 2026-06-16): managers verify this once from
    # Toast and every sales/labor/tip lookup can join by GUID instead of
    # re-guessing names at boot. CenaToastLink stays as a compatibility/display
    # cache while older screens migrate.
    toast_employee_guid: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    toast_employee_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Email-pivot (2026-05-30): set by the employee during email self-setup; NULL =
    # not set up yet. Numeric PIN (4-8 digits), hashed. Login = identifier (email or
    # phone) + this passcode (replaces the retired SMS-OTP). aick runs the prod
    # column-add on the populated employees table.
    passcode_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # passcode-login lockout (lift of keypad_auth's pattern; samai guardrail #3)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lockout_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # bumped on passcode-set; a before_request invalidates any session whose
    # employee_session_version != this (samai guardrail #4)
    session_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    # Unify LINK (2026-05-31, Sam #2261 Team+Schedule combine; seam ckai-locked
    # #2295): nullable FK to the User account for a team member who ALSO has
    # system access (manager/partner). NULL = a pure scheduling employee (no
    # /partner login). Employee stays the canonical identity - auth/isolation/
    # positions UNTOUCHED; the link carries the legacy User.permission_level
    # transitionally for require_level gates, until Project 2's position-driven
    # permissions land. ondelete=SET NULL: deleting a User unlinks, never deletes
    # the employee. The boot ALTER adds this column to the populated employees
    # table (app/__init__.py).
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )


class Message(Base):
    """A one-to-one in-app message between two employees (the /employee/messages
    portal surface). Peer-to-peer: any ACTIVE employee may message any other
    ACTIVE employee. A conversation/thread is the set of rows between a given
    (from, to) pair in either direction; read_at NULL = unread by the recipient.

    NEW table -> Base.metadata.create_all(engine) at boot creates it on both a
    fresh and a populated DB (create_all only skips tables that already exist);
    no ALTER/migration needed (same as EmployeeSetupToken).
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_pair", "from_employee_id", "to_employee_id", "id"),
        Index("ix_messages_inbox", "to_employee_id", "read_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )
    # NULL until the recipient opens the thread (mark-thread-read).
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EmployeeSetupToken(Base):
    """Email-pivot (2026-05-30): a one-time, expiring email-setup link for an
    admin-added employee. The admin-add emails /employee/setup/<token>; the
    employee opens it to set their passcode + complete their profile. Stored as a
    SHA-256 hash of a high-entropy value (lookupable, not reversible), single-use
    (used flips on consume), and expiring - so a leaked or stale link can't hijack
    an invite (samai guardrail #1)."""

    __tablename__ = "employee_setup_tokens"
    __table_args__ = (
        Index("ix_setup_token_emp", "employee_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # sha256 hex
    # Dual-channel reset (Sam 2026-06-07): a short MANAGER-DISPLAYED code, tied to
    # this SAME single-use row, as an alternative to the email link. Stored as
    # sha256 hex of the 6-digit code (lookupable, not reversible). NULLABLE so old
    # rows (link-only) remain valid; code_attempts gives the 6-digit code its own
    # per-token brute-force lockout (the link token is high-entropy, but a 6-digit
    # code is guessable, so it needs an attempt cap). The boot ALTER (app/__init__.py)
    # adds both columns to the populated table.
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # sha256 hex of the short code
    code_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class EmployeePhone(Base):
    """Secondary phone numbers for an employee. The primary login phone is
    Employee.phone; this supports additional / historical numbers."""

    __tablename__ = "employee_phones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class EmployeeStoreAssignment(Base):
    """Which store(s) an employee works at. Each employee has >=1 (B3)."""

    __tablename__ = "employee_store_assignments"
    __table_args__ = (
        UniqueConstraint("employee_id", "store_key", name="uq_emp_store"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 'tomball' | 'copperfield'
    store_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class Position(Base):
    """A job position (cook, server, ...) - the Sling position taxonomy."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sling_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # null = applies to all stores
    store_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


# The canonical schedule-position catalog (Sam 2026-05-31): the ONLY jobs that
# may appear in the manager schedule dropdowns. Everything else in the positions
# table is Sling-import residue (C-Grill,
# C-Prep, Chba, Chip, Cenas Togo, Dish, ...) and is filtered OUT of the board's
# position list (see app.web.schedules_v2). The management roles (Partner,
# Corporate, GM, KM, ...) are User permission levels, not Sling positions, so
# they may be absent from this table; app.create_app() seeds any missing
# canonical name as an all-store row (store_key=NULL). Finer kitchen roles
# ("tags") are deferred. The filter is NON-DESTRUCTIVE - read-side only:
# EmployeePosition.position_id is ondelete=CASCADE, so deleting a Position row
# would silently wipe migrated employees' assignments; we never delete here.
CANONICAL_POSITIONS = [
    "Partner", "Corporate", "Corporate Chef", "GM", "KM", "Assistant KM",
    "FOH Manager", "Expo", "Busser", "Cashier", "Server", "Well",
    "Bartender", "Host", "Cook", "Prep", "Dishwasher", "Training",
]


class EmployeePosition(Base):
    """Which positions an employee holds, PER STORE (Sam #2435/#2457: positions
    are assigned per-store, so one person on ONE login can be Manager @ Tomball
    + Server @ Copperfield). store_key added 2026-05-31 (permissions rework); the
    boot migration backfills existing global rows across each person's stores.
    store_key is nullable only to survive the ADD COLUMN on the populated table -
    app logic + the backfill always set it; the union enforcement keys off it."""

    __tablename__ = "employee_positions"
    __table_args__ = (
        UniqueConstraint("employee_id", "position_id", "store_key", name="uq_emp_position_store"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position_id: Mapped[int] = mapped_column(
        ForeignKey("positions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 'tomball' | 'copperfield' - the store this position is held at (#2457).
    store_key: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class Tag(Base):
    """Shift / employee tags - the Sling tag taxonomy."""

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sling_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class EmployeeSmsCode(Base):
    """One-time SMS login codes (B2 phone+SMS auth). Codes are HASHED
    (generate_password_hash on request-code, check_password_hash on verify) -
    never stored in plaintext. 10-minute expiry, single-use, 5-attempt lock."""

    __tablename__ = "employee_sms_codes"
    __table_args__ = (
        Index("ix_sms_emp_active", "employee_id", "used", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    # created_at + 10 minutes (set by the endpoint)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ============================================================
# Schedules V2 - Block 4 (Sam #1742): manager DRAFT schedule creation.
# aick owns these 3 tables; ck builds the week-view UI against them; the CRUD
# endpoints + the store-audience gate live in app/web/schedules_v2.py. B6-B9
# hooks baked in per ckai #1864: shifts.employee_id NULLABLE + reassignable
# (B9 swaps rewrite it); shifts.start_at/end_at are datetimes (B6 alarms key
# off start_at). ckai owns the later-block tables (shift_alarms,
# time_off_requests, availability, offers/swaps) as he builds B6-B9.
# ============================================================


class Schedule(Base):
    """A weekly schedule for one store. status 'draft' until B5 publish."""

    __tablename__ = "schedules"
    __table_args__ = (
        UniqueConstraint("store_key", "week_start", name="uq_schedule_store_week"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 'tomball' | 'copperfield' - the audience scope (the store-gate keys off this)
    store_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)  # Sunday of the week
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)  # draft | published
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # set on B5 publish
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # manager user_id
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class Shift(Base):
    """A single shift. employee_id NULL = OPEN shift (B4) + reassignable (B9).
    start_at/end_at are full datetimes (B6 alarms key off start_at)."""

    __tablename__ = "shifts"
    __table_args__ = (
        Index("ix_shifts_schedule", "schedule_id"),
        Index("ix_shifts_employee", "employee_id"),
        # B5 my-schedule: employee_date index (directive 367) so GET /employee/
        # my-schedule/shifts (employee_id + start_at range) rides an index, no scan.
        Index("ix_shifts_emp_start", "employee_id", "start_at"),
        # partial index for OPEN shifts (directive 3.8) - fast open-shift lookups (B5/B9)
        Index("ix_shifts_open", "schedule_id", sqlite_where=text("status = 'open'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False
    )
    # NULL = open shift; rewritten on assign / B9 swap-approval (ckai #1864)
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # B6 alarm key
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    break_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="assigned", nullable=False)  # assigned | open
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sam #2872: for an imported HISTORICAL shift whose person is no longer employed
    # (no Employee record), employee_id is NULL and display_name carries their name
    # so the week-view renders it struck-through. NULL for normal shifts.
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Sam (Sling-parity): PER-SHIFT publish state. NULL = unpublished -> renders
    # "hollow"/outline in the Week Builder + HIDDEN from the employee (they only see
    # published shifts). Set = published (filled + visible). Editing a published shift
    # via "Save" clears it (unpublish that one shift); "Save & Publish" / the re-publish
    # action sets it. Backfilled on boot from the schedule's published_at so existing
    # published weeks stay visible -> no regression.
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class ShiftTag(Base):
    """Tags on a shift (M2M shifts <-> the B2 Tag taxonomy)."""

    __tablename__ = "shift_tags"
    __table_args__ = (
        UniqueConstraint("shift_id", "tag_id", name="uq_shift_tag"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ShiftAcceptance(Base):
    """Employee accept/decline of an assigned shift (Schedules V2 B5). One row per
    (shift, employee); a decline carries a required reason that surfaces to the
    assigning manager. No row = 'pending'."""

    __tablename__ = "shift_acceptances"
    __table_args__ = (
        UniqueConstraint("shift_id", "employee_id", name="uq_shift_acceptance"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    response: Mapped[str] = mapped_column(String(20), nullable=False)  # 'accepted' | 'declined'
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)    # required on decline
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class ShiftAlarm(Base):
    """Schedules V2 B6: one pending/sent shift reminder for (shift, employee,
    fire-time, channel). Created on publish (scheduling_alarms.create_for_schedule);
    the per-minute cron sends due pending rows. UNIQUE(shift_id, employee_id,
    alarm_time, channel) makes re-publish idempotent (no double-create). The
    partial index on status='pending' keeps the per-minute cron O(small)."""

    __tablename__ = "shift_alarms"
    __table_args__ = (
        UniqueConstraint("shift_id", "employee_id", "alarm_time", "channel",
                         name="uq_shift_alarm"),
        # the per-minute cron only ever scans pending rows
        Index("ix_shift_alarms_pending", "alarm_time",
              sqlite_where=text("status = 'pending'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # when to fire = shift.start_at - employee's minutes_before
    alarm_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)   # 'sms' | 'email'
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)  # pending|sent|failed
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class EmployeeAlarmPreference(Base):
    """Schedules V2 B6: an employee's shift-reminder preferences. One row per
    employee (UNIQUE). No row = the default (SMS on, email off, 60 min before)."""

    __tablename__ = "employee_alarm_preferences"
    __table_args__ = (
        UniqueConstraint("employee_id", name="uq_employee_alarm_pref"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    minutes_before: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    second_minutes_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class TimeOffRequest(Base):
    """Schedules V2 B7: an employee's time-off request for a date range. status is
    'pending' until a manager approves/denies; only an 'approved' request blocks
    shift-create (scheduling_timeoff.conflict). A cancel is a soft status flip
    (kept as history). One employee can have many; overlap with an existing
    pending/approved request is rejected in the endpoint (a date-range overlap
    can't be a simple UNIQUE), not by a DB constraint."""

    __tablename__ = "time_off_requests"
    __table_args__ = (
        # powers conflict() (employee_id + status='approved' + date range) + the employee list
        Index("ix_timeoff_emp_status", "employee_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)   # inclusive; >= start_date
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)  # pending|approved|denied|cancelled
    manager_notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # set on deny (optional on approve)
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # manager User.id
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class TimeOffPolicy(Base):
    """Per-store time-off request policy a manager sets in Operations -> Team ->
    Settings (Sam 2026-06-13). require_approval: requests need a manager OK
    (else auto-approved). cutoff_enabled + cutoff_days: requests must be
    submitted at least N days in advance, so the employee's date picker blocks
    today..today+N and only allows day N+1 onward. One row per store_key;
    absence = defaults below. A NEW table (Base.metadata.create_all creates it
    on boot -- no ALTER needed)."""

    __tablename__ = "time_off_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_key: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cutoff_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cutoff_days: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class EmployeeAvailability(Base):
    """Schedules V2 B8: an employee's recurring weekly AVAILABLE window (they
    declare when they CAN work). One employee has many (per weekday + window).
    Times are minutes-since-midnight (0..1439) - portable + a trivial int compare
    in warning(); the API exchanges 'HH:MM'. day_of_week 0=Mon..6=Sun. Distinct
    from B7 time_off_requests (approval-gated, HARD block) - availability is
    informational + drives only a SOFT warning."""

    __tablename__ = "employee_availability"
    __table_args__ = (
        Index("ix_avail_emp_dow", "employee_id", "day_of_week"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)    # 0=Mon .. 6=Sun
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)   # minutes since midnight
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)     # > start_minute
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class EmployeeUnavailabilityBlock(Base):
    """Schedules V2 B8: a date-specific span the employee CANNOT work (a one-off
    exception, distinct from recurring availability + from B7 time-off). warning()
    flags a shift whose start falls inside a block."""

    __tablename__ = "employee_unavailability_blocks"
    __table_args__ = (
        Index("ix_unavail_emp_start", "employee_id", "start_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)   # > start_at
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class CenaToastLink(Base):
    """Cena<->Toast Link tab (Sam #2629): a CONFIRMED match between a Cena
    employee and a Toast employee, scoped per store. A manager VERIFIES one of
    ckbro's GET .../toast/match-suggestions rows; that confirmation persists here
    so the Link tab can show verified links + later load that person's Toast data
    (by toast_id). One confirmed Toast link per Cena person per store
    (UNIQUE(cena_employee_id, store_key)) -- re-confirming UPSERTs the same row.
    cena_employee_id is conceptually an employees.id; store_key is the LOCATION
    ('tomball'/'copperfield'), same key the roster/board filter by. toast_id is a
    Toast guid (string). confirmed_by is the User.id who verified."""

    __tablename__ = "cena_toast_link"
    __table_args__ = (
        # one confirmed Toast link per Cena person per store
        UniqueConstraint("cena_employee_id", "store_key", name="uq_cena_toast_link_emp_store"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cena_employee_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)  # -> employees.id (conceptual FK)
    store_key: Mapped[str] = mapped_column(String(40), nullable=False)  # location: 'tomball'/'copperfield'
    toast_id: Mapped[str] = mapped_column(String(64), nullable=False)   # Toast employee guid
    toast_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # User.id who verified
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class CenaToastIgnore(Base):
    """Partner cleanup marker for the Team > Link tab.

    Toast-only people cannot be deleted from Toast from inside Cenas, and a
    Cenas-only profile may need to be hidden from matching without losing
    historical rows. This table stores one ignored identity per store/source so
    the Link page can stay clean while the underlying systems remain intact.
    source = 'cena' uses employees.id as source_id; source = 'toast' uses the
    Toast employee guid.
    """

    __tablename__ = "cena_toast_ignore"
    __table_args__ = (
        UniqueConstraint("store_key", "source", "source_id", name="uq_cena_toast_ignore_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    ignored_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ignored_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ToastEmployeeSnapshot(Base):
    """Sam #2845: a CACHED snapshot of one Toast employee's labor/performance/pay
    for a store, refreshed by a scheduled BULK sync (sync_toast_snapshots) so the
    Link tab + the employee 'My Hours & Pay' panel serve from a fast DB read
    instead of a live Toast pull on every page load. The live-per-request model
    fired ~30 Toast calls per view (the performance pull) and, x many employees,
    choked the workers -> Render 502s + on/off flicker. Now the heavy Toast work
    happens once in the background; web requests just read this row.

    Keyed (store_key, toast_id); the JSON columns hold the exact pieces
    toast_employee_summary() returns. ok/error capture the last pull's outcome so
    the UI can show a 'syncing'/'unavailable' state without ever calling Toast."""

    __tablename__ = "toast_employee_snapshot"
    __table_args__ = (
        UniqueConstraint("store_key", "toast_id", name="uq_toast_snapshot_store_toast"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    toast_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(String(400), nullable=True)
    hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    timecards_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    performance_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    payroll_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PerfPeriodCache(Base):
    """Phase 3 (Sam #2938): the SANITIZED per-employee per-period performance
    snapshot PUSHED token-gated from the CK-local perf DB (Mini_IT13 = source of
    truth, Sam #2896/#2901). /employee/my-performance reads THIS (a fast DB read)
    -- no live Toast, no live CK read (Mini_IT13 sleeps). Holds NO restaurant
    sales: sales are isolated on CK in perf_internal and never pushed -- there is
    simply no sales column here, so sanitize-by-construction continues app-side."""

    __tablename__ = "perf_period_cache"
    __table_args__ = (
        UniqueConstraint("cena_employee_id", "period", name="uq_perfcache_emp_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cena_employee_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    toast_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    store_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[str | None] = mapped_column(String(20), nullable=True)
    period_end: Mapped[str | None] = mapped_column(String(20), nullable=True)
    total_hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reg_hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ot_hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    base_pay: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tips: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    service_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)        # EMPLOYEE-VISIBLE service metrics ONLY (v1: empty; v2 fills)
    attribution_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)    # INTERNAL audit metadata -- NEVER read by the employee payload (samai #2939)
    computed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PerfMetricDetailCache(Base):
    """Employee-visible performance detail cache.

    The summary metrics live on PerfPeriodCache.service_json; this companion table
    stores the click-through explanation for each metric so dashboard card clicks
    read the DB instead of calling Toast. It is keyed by the same local employee id
    used by the profile/performance caches and never stores Toast sales totals or
    private identifiers.
    """

    __tablename__ = "perf_metric_detail_cache"
    __table_args__ = (
        UniqueConstraint(
            "cena_employee_id", "period", "metric_key",
            name="uq_perfmetricdetail_emp_period_metric",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cena_employee_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    metric_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    period_start: Mapped[str | None] = mapped_column(String(20), nullable=True)
    period_end: Mapped[str | None] = mapped_column(String(20), nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    display: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    formula: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    computed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PerfShiftCache(Base):
    """Phase 3.5 (Sam #2938 / samai #2954): SANITIZED per-shift rows pushed from the
    CK perf DB. Employee-own + sales-free. attribution_json is INTERNAL (the payload
    builder never reads it). One row per (employee, shift clock_in)."""

    __tablename__ = "perf_shift_cache"
    __table_args__ = (
        UniqueConstraint("cena_employee_id", "clock_in", name="uq_perfshift_emp_clockin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cena_employee_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    toast_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    store_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    business_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    clock_in: Mapped[str | None] = mapped_column(String(40), nullable=True)
    clock_out: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reg_hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ot_hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    base_pay: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tips: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tips_declared: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)    # N4: null (undeclared) vs $0
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)    # N5: employee/mgr-visible missed-punch flag
    review_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    attribution_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PerfRankCache(Base):
    """Phase 5.1 (Sam #3009/#3014/#3019): the SANITIZED per-employee RANK output,
    pushed from the CK perf DB. rank_json holds ONLY allowed rank output --
    own ranks {effective_hourly, tip_percent, combined} per period (own values are
    the Phase-4 own view) + per-cohort leaderboards whose peer rows carry ONLY
    {name, rank, effective_hourly, tip_percent, combined} and gate independently on
    min-cohort. It holds NO restaurant sales / eligible_sales / GUID / attribution /
    peer pay breakdown -- sales is walled in CK perf_internal and only the tip%
    RATIO ever derives out (the receiver re-checks with a sales-wall guard before
    storing). held_days stays 'pending' until real rank_snapshots accrue (no faking)."""

    __tablename__ = "perf_rank_cache"
    __table_args__ = (
        UniqueConstraint("cena_employee_id", name="uq_perfrank_emp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cena_employee_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    rank_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # sanitized rank output (no sales/GUID/peer-pay)
    computed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


# N-c (Sam #3028 hardening): explicit peer-row field whitelist for leaderboard rows in a
# PerfRankCache.rank_json. The /cron/perf-push receiver REJECTS (422) a push whose leaderboard
# rows carry any field outside this set (fail-closed); the read-path STRIPS to this set before
# serving (fail-safe). Guards a future change from leaking peer base_pay/tips/sales into a row.
RANK_PEER_FIELDS = {"name", "rank", "effective_hourly", "tip_percent", "combined",
                    "combined_rank", "is_me",
                    # Sam #3104 / samai #3102: the new tips/hr rank leaderboard (tipped-only).
                    # tips_per_hour = tips/total_hours -- sales-CLEAN (no sales denominator);
                    # added so the fail-closed whitelist admits it while still rejecting peer
                    # base_pay / tips$ / sales / GUID / employee_id / internal.
                    "tips_per_hour"}


def rank_peer_rows_ok(rank_json):
    """(ok, offending_fields). ok=False if any leaderboard peer row carries a field outside
    RANK_PEER_FIELDS."""
    bad = set()
    lbs = (rank_json or {}).get("leaderboards") or {}
    if isinstance(lbs, dict):
        for _per, boards in lbs.items():
            if not isinstance(boards, dict):
                continue
            for _b, board in boards.items():
                for row in ((board or {}).get("rows") or []):
                    if isinstance(row, dict):
                        bad |= (set(row.keys()) - RANK_PEER_FIELDS)
    return (not bad), sorted(bad)


def sanitize_rank_json(rank_json):
    """Return a COPY with every leaderboard peer row stripped to RANK_PEER_FIELDS, and with
    internal-only plumbing removed from the client payload (samai #3142 notes 1/3 -- zero
    internal ids/plumbing client-side): the own surrogate `cena_employee_id` (top-level) and
    every `cohort_key` (store|role|metric). Neither is read by the UI (which uses is_tipped +
    ranks + leaderboards). Read-path belt; never mutates the stored object."""
    if not isinstance(rank_json, dict):
        return rank_json
    import copy
    d = copy.deepcopy(rank_json)
    d.pop("cena_employee_id", None)                     # note 1: own internal surrogate id off the client
    lbs = d.get("leaderboards")
    if isinstance(lbs, dict):
        for _per, boards in lbs.items():
            if not isinstance(boards, dict):
                continue
            for _b, board in boards.items():
                if not isinstance(board, dict):
                    continue
                board.pop("cohort_key", None)            # note 3: internal cohort plumbing off the client
                rows = board.get("rows")
                if isinstance(rows, list):
                    board["rows"] = [{k: v for k, v in row.items() if k in RANK_PEER_FIELDS}
                                     for row in rows if isinstance(row, dict)]
    ranks = d.get("ranks")                              # defensive: strip cohort_key anywhere it appears
    if isinstance(ranks, dict):
        for _per, metrics in ranks.items():
            if isinstance(metrics, dict):
                for _m, obj in metrics.items():
                    if isinstance(obj, dict):
                        obj.pop("cohort_key", None)
    return d


class ShiftOffer(Base):
    """Schedules V2 B9: an employee offers up their assigned shift; an eligible
    employee takes it; a manager approves -> the shift's employee_id moves to the
    taker. status open->taken->approved|denied, plus cancelled (by the offerer) /
    expired (the cron, past expires_at). restricted=True -> the taker must share
    the shift's store + position (unless the offer is unrestricted)."""

    __tablename__ = "shift_offers"
    __table_args__ = (
        Index("ix_shift_offers_status_exp", "status", "expires_at"),  # powers the expiry cron
        Index("ix_shift_offers_shift", "shift_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False
    )
    offered_by_employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    taken_by_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(12), default="open", nullable=False)  # open|taken|approved|denied|cancelled|expired
    restricted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # taker must match store+position
    # Cash incentive the offerer attaches to sweeten the pickup (Sam 2026-06-13).
    # Integer CENTS to avoid float money bugs; NULL = no money offered. DISPLAYED
    # incentive only -- the app never moves money; the two employees settle offline.
    # Surfaced on the browse cards + the manager Market dashboard.
    incentive_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # manager User.id
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class ShiftSwap(Base):
    """Schedules V2 B9: employee A proposes trading their shift (from_shift) for
    employee B's shift (to_shift); B accepts; a manager approves -> the two shifts'
    employee_ids swap. status proposed->accepted->approved|denied, plus cancelled
    (by the proposer) / expired (cron)."""

    __tablename__ = "shift_swaps"
    __table_args__ = (
        Index("ix_shift_swaps_status_exp", "status", "expires_at"),  # powers the expiry cron
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    to_employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(12), default="proposed", nullable=False)  # proposed|accepted|approved|denied|cancelled|expired
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
