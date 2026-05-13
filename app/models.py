from __future__ import annotations

from datetime import date, datetime, timedelta
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
    Index,
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


class WhatsAppMessage(Base):
    """Partner-side mirror of ock's WhatsApp inbox. Populated by the
    CK-Mini-PC daemon POSTing to /api/inbox/whatsapp; rendered by the
    Partner-gated /partner/operations/whatsapp inbox so Sam + Masood can
    read every thread on ock's number (+13464620746) without a phone in
    hand. Phase 2 adds the outbound side via the same model + a
    cloudflared tunnel on CK that hosts ock's send endpoint.
    """
    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        UniqueConstraint("external_id", name="uq_whatsapp_external_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ock's awareness.db message id (or whatsapp stanza id) — dedupe key.
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)
    # ISO8601 timestamp from ock side (when the channel saw the message).
    ts: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # WhatsApp JID (group or DM).
    chat_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    chat_type: Mapped[str] = mapped_column(String(20), nullable=False)  # 'group' | 'dm'
    chat_name: Mapped[str | None] = mapped_column(String(200))
    sender_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    sender_name: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    media_kind: Mapped[str | None] = mapped_column(String(30))  # image|video|audio|document|sticker
    # 'inbound' for messages ock received; 'outbound' once Phase 2 lets
    # Sam/Masood reply through EZLive.
    direction: Mapped[str] = mapped_column(String(10), nullable=False, default="inbound")
    sent_by_user: Mapped[str | None] = mapped_column(String(80))  # 'sam' | 'masood' for outbound
    reply_to_external_id: Mapped[str | None] = mapped_column(String(120))
    raw_metadata: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # When EZLive received the message (vs `ts` which is when ock saw it).
    ingested_at: Mapped[str] = mapped_column(String(40), nullable=False)


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

