from __future__ import annotations

from datetime import datetime, timedelta
from sqlalchemy import (
    String,
    Integer,
    Boolean,
    DateTime,
    Float,
    Date,
    Text,
    ForeignKey,
    UniqueConstraint,
    JSON,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


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
    can be replayed (Phase C)."""
    __tablename__ = "driver_location"

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("driver_shift.id", ondelete="CASCADE"),
                                          nullable=False, index=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="CASCADE"),
                                           nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                  nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading_deg: Mapped[float | None] = mapped_column(Float, nullable=True)


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