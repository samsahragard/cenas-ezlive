import os
import logging
from flask import Flask
import click
from dotenv import load_dotenv

from app.web.ezcater_routes import cater as ezc
from app.web.manager_routes import manager as mngr
from app.web.driver_routes import driver as drvr
from app.web.review_routes import review as rvw
from app.web.orders_browse import browse as obrowse
from app.web.ezcater_webhook import webhook as ezwh
from app.web.produce_order import produce_order as produce
from app.web.reports import reports as reports_bp
from app.web.store_routes import store_bp
from app.web.developer_chat import dev_chat as dev_chat_bp
from app.web.corporate_order import corp_order as corp_order_bp
from app.web.ezcater_import_routes import ezc_import as ezc_import_bp
from app.web.ezcater_live_routes import ezc_live as ezc_live_bp
from app.web.ck_whatsapp import ck_whatsapp_bp
from app.web.team_routes import team_bp
from app.web.legal_routes import legal as legal_bp
from app.web.access_request_routes import access_req as access_req_bp
from app.web.driver_system import driver_system_bp
from app.web import auth as ezauth
from app.web import keypad_auth as ezkeypad
from app.services import produce_ingest


def _init_sentry() -> None:
    """Initialize Sentry SDK if SENTRY_DSN is set in env (production only).
    No-op otherwise so local dev + chat tools stay quiet. Done BEFORE Flask
    init so exceptions during startup are captured too. Phase 0 / Block 2
    (2026-05-13)."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        environment = (
            "production" if os.getenv("RENDER")
            else os.getenv("FLASK_ENV", "development")
        )
        sentry_sdk.init(
            dsn=dsn,
            integrations=[FlaskIntegration()],
            environment=environment,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
            send_default_pii=False,  # don't ship form bodies / cookies
            release=os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT"),
        )
        logging.getLogger(__name__).info(
            "Sentry initialized — environment=%s, release=%s",
            environment, (os.getenv("RENDER_GIT_COMMIT") or "(unset)")[:7])
    except Exception:
        logging.getLogger(__name__).exception("Sentry init failed (non-fatal)")


def create_app():
    load_dotenv(override=True)

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Sentry — only activates when SENTRY_DSN is set in env (Phase 0 / Block 2).
    # Local dev + chat tools stay quiet.
    _init_sentry()

    app = Flask(__name__)
    # SECRET_KEY: fail loud if unset (Phase 0, 2026-05-13 — closes the
    # regression risk Sam flagged in ck-2026-05-12). The legacy fallback
    # ('dev-secret') silently shipped to Render until the 2026-05-12
    # rotation — anyone who knew the fallback could have forged Flask
    # session cookies against production. We now hard-fail at startup
    # so a future deploy can't silently regress. Local dev that
    # deliberately wants the fallback can set ALLOW_DEV_SECRET=1.
    _secret = os.getenv("SECRET_KEY")
    if not _secret:
        if os.getenv("ALLOW_DEV_SECRET") == "1":
            _secret = "dev-secret"
            logging.getLogger(__name__).warning(
                "SECRET_KEY not set — falling back to 'dev-secret' because "
                "ALLOW_DEV_SECRET=1. NEVER do this in production."
            )
        else:
            raise RuntimeError(
                "SECRET_KEY env var is not set. Set it in the environment "
                "(Render: Service → Environment; local: .env or shell). "
                "For deliberate local-dev use the literal fallback, set "
                "ALLOW_DEV_SECRET=1."
            )
    app.config["SECRET_KEY"] = _secret

    app.register_blueprint(ezc)
    app.register_blueprint(mngr)
    app.register_blueprint(drvr)
    app.register_blueprint(rvw)
    app.register_blueprint(obrowse)
    app.register_blueprint(ezwh)
    app.register_blueprint(produce)
    app.register_blueprint(reports_bp)
    app.register_blueprint(store_bp)
    app.register_blueprint(dev_chat_bp)
    app.register_blueprint(ezc_import_bp)
    app.register_blueprint(ezc_live_bp)
    app.register_blueprint(ck_whatsapp_bp)
    app.register_blueprint(team_bp)
    app.register_blueprint(legal_bp)
    app.register_blueprint(access_req_bp)
    app.register_blueprint(driver_system_bp)
    # Corporate-order Blueprint mounts under <store_slug> just like store_bp;
    # has its own url_value_preprocessor + partner_gate so it's standalone.
    app.register_blueprint(corp_order_bp, url_prefix="/<store_slug>")

    # Install the shared-password gate AFTER all other blueprints so the
    # before_request hook sees their routes. Webhook + ingest endpoints
    # are exempted inside auth.install().
    ezauth.install(app)
    # Keypad-auth (migration 13) — new per-person 5-digit passcode flow.
    # Registers /keypad-login, /change-passcode, /keypad-logout and a
    # before_request that stashes g.current_user. Must come after ezauth so
    # its EXEMPT_PREFIXES updates win the path-match race for /keypad-login.
    ezkeypad.install(app)

    # Ensure model tables exist. Idempotent — won't recreate or alter
    # existing tables, just creates any missing ones. This is a backstop
    # for environments where alembic Pre-Deploy isn't configured (Render's
    # preDeployCommand was set to None as of 2026-05-09; alembic migrations
    # 1-4 had already been applied to the live DB so this is a no-op for
    # them — only NEW model tables in newer commits get created).
    try:
        from app.db import engine
        from app.models import Base
        if engine is not None:
            Base.metadata.create_all(engine)
            logging.getLogger(__name__).info("Base.metadata.create_all completed")
    except Exception:
        logging.getLogger(__name__).exception("Base.metadata.create_all failed (non-fatal)")

    # Idempotent column backfill for the drivers table. Required because
    # alembic Pre-Deploy isn't wired on Render and create_all() doesn't ALTER
    # existing tables — so the columns added in migration 8_drivers_auth would
    # otherwise never appear in production. Each ALTER is gated on column
    # absence, so this is safe to run on every boot.
    try:
        from sqlalchemy import inspect as _sa_inspect, text as _sa_text
        from app.db import engine as _eng
        if _eng is not None:
            insp = _sa_inspect(_eng)
            if "drivers" in insp.get_table_names():
                existing = {c["name"] for c in insp.get_columns("drivers")}
                bool_true = "1" if _eng.dialect.name == "sqlite" else "TRUE"
                additions = [
                    ("email", "VARCHAR(200)"),
                    ("phone", "VARCHAR(50)"),
                    ("address", "VARCHAR(300)"),
                    ("password_hash", "VARCHAR(200)"),
                    ("active", f"BOOLEAN NOT NULL DEFAULT {bool_true}"),
                    ("failed_attempts", "INTEGER NOT NULL DEFAULT 0"),
                    ("lockout_until", "TIMESTAMP"),
                ]
                added = []
                with _eng.begin() as conn:
                    for col_name, col_def in additions:
                        if col_name not in existing:
                            conn.execute(_sa_text(f"ALTER TABLE drivers ADD COLUMN {col_name} {col_def}"))
                            added.append(col_name)
                if added:
                    logging.getLogger(__name__).info(
                        "drivers table: backfilled missing columns %s", added)
    except Exception:
        logging.getLogger(__name__).exception("drivers column backfill failed (non-fatal)")

    # Idempotent column backfill for orders.total_amount (migration 10) AND
    # the payroll backfill columns from migration 11. Same self-healing
    # pattern as the drivers backfill above — each ALTER is gated on column
    # absence so this is safe to run on every boot.
    try:
        from sqlalchemy import inspect as _sa_inspect2, text as _sa_text2
        from app.db import engine as _eng2
        if _eng2 is not None:
            insp = _sa_inspect2(_eng2)
            if "orders" in insp.get_table_names():
                existing = {c["name"] for c in insp.get_columns("orders")}
                migration_11_cols = [
                    ("tracking_status",        "VARCHAR(40)"),
                    ("ezcater_driver_name",    "VARCHAR(150)"),
                    ("pickup_kitchen",         "VARCHAR(20)"),
                    ("pickup_miles",           "FLOAT"),
                    ("food_total",             "FLOAT"),
                    ("tip_amount",             "FLOAT"),
                    ("delivery_fee",           "FLOAT"),
                    ("caterer_total_due",      "FLOAT"),
                    ("delivery_result",        "VARCHAR(60)"),
                    ("delivery_start_time",    "VARCHAR(20)"),
                    ("delivery_complete_time", "VARCHAR(20)"),
                    # Migration 12: ezCater live tracking columns
                    ("delivery_tracking_id",       "VARCHAR(64)"),
                    ("ezcater_status_key",         "VARCHAR(60)"),
                    ("ezcater_driver_lat",         "FLOAT"),
                    ("ezcater_driver_lng",         "FLOAT"),
                    ("ezcater_status_updated_at",  "TIMESTAMP"),
                ]
                added = []
                with _eng2.begin() as conn:
                    if "total_amount" not in existing:
                        conn.execute(_sa_text2("ALTER TABLE orders ADD COLUMN total_amount FLOAT"))
                        added.append("total_amount")
                    for col_name, col_def in migration_11_cols:
                        if col_name not in existing:
                            conn.execute(_sa_text2(f"ALTER TABLE orders ADD COLUMN {col_name} {col_def}"))
                            added.append(col_name)
                if added:
                    logging.getLogger(__name__).info("orders table: backfilled missing columns %s", added)
    except Exception:
        logging.getLogger(__name__).exception("orders column backfill failed (non-fatal)")

    # Idempotent column backfill for the driver-system additions (migration 15).
    # Adds the delivery state-machine + payout snapshot columns on `orders`
    # and the tier/score/lifetime/status columns on `drivers`. Same gated-
    # absence pattern as the older backfills above.
    try:
        from sqlalchemy import inspect as _sa_inspect_15, text as _sa_text_15
        from app.db import engine as _eng_15
        if _eng_15 is not None:
            insp = _sa_inspect_15(_eng_15)
            bool_false = "0" if _eng_15.dialect.name == "sqlite" else "FALSE"
            orders_additions = [
                ("delivery_window_start",   "TIMESTAMP"),
                ("delivery_window_end",     "TIMESTAMP"),
                ("customer_rating",         "INTEGER"),
                ("setup_photo_url",         "VARCHAR(500)"),
                ("setup_photo_uploaded_at", "TIMESTAMP"),
                ("potential_payout",        "FLOAT"),
                ("paid_payout",             "FLOAT"),
                ("paycheck_id",             "INTEGER"),
                ("assigned_driver_id",      "INTEGER"),
                ("approved_by_user_id",     "INTEGER"),
                ("approved_at",             "TIMESTAMP"),
                ("pickup_actual_at",        "TIMESTAMP"),
                ("en_route_at",             "TIMESTAMP"),
                ("delivered_actual_at",     "TIMESTAMP"),
            ]
            drivers_additions = [
                ("status",                  "VARCHAR(20) NOT NULL DEFAULT 'active'"),
                ("terminated_at",           "TIMESTAMP"),
                ("termination_reason",      "VARCHAR(200)"),
                ("joined_at",               "DATE"),
                ("lifetime_delivery_count", "INTEGER NOT NULL DEFAULT 0"),
                ("current_score",           "INTEGER"),
                ("current_tier",            "VARCHAR(20)"),
                ("home_store_id",           "VARCHAR(20)"),
                ("last_known_lat",          "FLOAT"),
                ("last_known_lng",          "FLOAT"),
                ("last_location_at",        "TIMESTAMP"),
                ("photo_url",               "VARCHAR(500)"),
            ]
            added_orders, added_drivers = [], []
            with _eng_15.begin() as conn:
                if "orders" in insp.get_table_names():
                    existing = {c["name"] for c in insp.get_columns("orders")}
                    for col_name, col_def in orders_additions:
                        if col_name not in existing:
                            conn.execute(_sa_text_15(f"ALTER TABLE orders ADD COLUMN {col_name} {col_def}"))
                            added_orders.append(col_name)
                if "drivers" in insp.get_table_names():
                    existing = {c["name"] for c in insp.get_columns("drivers")}
                    for col_name, col_def in drivers_additions:
                        if col_name not in existing:
                            conn.execute(_sa_text_15(f"ALTER TABLE drivers ADD COLUMN {col_name} {col_def}"))
                            added_drivers.append(col_name)
            if added_orders:
                logging.getLogger(__name__).info("orders table (migration 15): backfilled %s", added_orders)
            if added_drivers:
                logging.getLogger(__name__).info("drivers table (migration 15): backfilled %s", added_drivers)
    except Exception:
        logging.getLogger(__name__).exception("driver-system column backfill failed (non-fatal)")

    # Seed ezcater_known_driver from the static roster Sam captured in his
    # 5/10 screenshots. Idempotent: only inserts rows for phones not already
    # present, so re-edits in the seed module on later boots add/update
    # without duplicating. Empty rows can be added manually via a future
    # admin UI; for now this is the authoritative starting set.
    try:
        from app.db import SessionLocal
        from app.models import EzcaterKnownDriver
        from app.services.ezcater_known_drivers_seed import seed_roster
        if SessionLocal is not None:
            db = SessionLocal()
            try:
                existing_phones = {p for (p,) in db.query(EzcaterKnownDriver.phone_e164).all()}
                inserted = 0
                for row in seed_roster():
                    if row["phone_e164"] and row["phone_e164"] not in existing_phones:
                        db.add(EzcaterKnownDriver(**row))
                        inserted += 1
                if inserted:
                    db.commit()
                    logging.getLogger(__name__).info(
                        "ezcater_known_driver: seeded %d new rows", inserted)
            finally:
                db.close()
    except Exception:
        logging.getLogger(__name__).exception("ezcater_known_driver seed failed (non-fatal)")

    # One-shot row backfill: compute total_amount for any existing Order rows
    # where it's NULL but items have parseable unit prices. Runs once per boot
    # for orders that haven't been touched yet — capped to 500 per boot so a
    # cold-start on Render doesn't time out the worker if the table is huge.
    try:
        from app.db import SessionLocal
        from app.models import Order, OrderItem
        from app.services.ezcater_pricing import compute_order_total
        if SessionLocal is not None:
            db = SessionLocal()
            try:
                pending = (db.query(Order)
                           .filter(Order.total_amount.is_(None))
                           .filter(Order.external_order_id.isnot(None))
                           .limit(500)
                           .all())
                fixed = 0
                for o in pending:
                    items = db.query(OrderItem).filter(OrderItem.order_id == o.id).all()
                    if not items:
                        continue
                    o.total_amount = compute_order_total(items)
                    fixed += 1
                if fixed:
                    db.commit()
                    logging.getLogger(__name__).info(
                        "orders.total_amount: backfilled %d rows", fixed)
            finally:
                db.close()
    except Exception:
        logging.getLogger(__name__).exception("orders total_amount row-backfill failed (non-fatal)")

    # One-time bootstrap of produce_price_snapshot from the current vendor JSONs
    # if the table is empty and the JSONs exist. Idempotent — only runs once.
    try:
        from app.db import SessionLocal
        from app.models import ProducePriceSnapshot
        from app.services.produce_history import bootstrap_from_current_jsons
        from pathlib import Path as _P
        if SessionLocal is not None:
            db = SessionLocal()
            try:
                count = db.query(ProducePriceSnapshot).count()
            finally:
                db.close()
            if count == 0:
                state_dir = _P(os.getenv("PRODUCE_STATE_DIR")
                               or (_P(__file__).resolve().parents[1] / "instance" / "produce"))
                if state_dir.exists():
                    result = bootstrap_from_current_jsons(state_dir)
                    logging.getLogger(__name__).info(
                        "produce price-snapshot bootstrap: inserted=%d skipped=%d",
                        result.get("inserted", 0), result.get("skipped", 0))
    except Exception:
        logging.getLogger(__name__).exception("produce snapshot bootstrap failed (non-fatal)")

    # Idempotent column backfill for users.session_version (added 2026-05-11
    # for the force-logout-on-reset feature). create_all only creates missing
    # tables — adding a column on an existing one requires this ALTER.
    try:
        from sqlalchemy import inspect as _sa_inspect3, text as _sa_text3
        from app.db import engine as _eng3
        if _eng3 is not None:
            insp = _sa_inspect3(_eng3)
            if "users" in insp.get_table_names():
                existing = {c["name"] for c in insp.get_columns("users")}
                if "session_version" not in existing:
                    with _eng3.begin() as conn:
                        conn.execute(_sa_text3("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1"))
                    logging.getLogger(__name__).info("users: backfilled session_version column")
    except Exception:
        logging.getLogger(__name__).exception("users.session_version backfill failed (non-fatal)")

    # Idempotent column backfill for drivers PIN-keypad columns (2026-05-12 —
    # migrating drivers off email+password to email+PIN, mirroring the User
    # keypad pattern).
    try:
        from sqlalchemy import inspect as _sa_inspect_pin, text as _sa_text_pin
        from app.db import engine as _eng_pin
        if _eng_pin is not None:
            insp = _sa_inspect_pin(_eng_pin)
            if "drivers" in insp.get_table_names():
                existing = {c["name"] for c in insp.get_columns("drivers")}
                bool_false = "0" if _eng_pin.dialect.name == "sqlite" else "FALSE"
                pin_additions = [
                    ("passcode_hash",     "VARCHAR(200)"),
                    ("first_login_done",  f"BOOLEAN NOT NULL DEFAULT {bool_false}"),
                    ("session_version",   "INTEGER NOT NULL DEFAULT 1"),
                ]
                added = []
                with _eng_pin.begin() as conn:
                    for col_name, col_def in pin_additions:
                        if col_name not in existing:
                            conn.execute(_sa_text_pin(f"ALTER TABLE drivers ADD COLUMN {col_name} {col_def}"))
                            added.append(col_name)
                if added:
                    logging.getLogger(__name__).info("drivers table: backfilled PIN-auth columns %s", added)
    except Exception:
        logging.getLogger(__name__).exception("drivers PIN-auth column backfill failed (non-fatal)")

    # Seed Sam as partner with passcode "12345" if no User rows exist
    # (migration 13 keypad auth). Idempotent: only inserts if the table is
    # empty, so we don't clobber later edits. Sam's first login forces a
    # passcode-change via first_login_done=False.
    try:
        from app.db import SessionLocal
        from app.models import User
        from werkzeug.security import generate_password_hash
        if SessionLocal is not None:
            db = SessionLocal()
            try:
                if db.query(User).count() == 0:
                    db.add(User(
                        full_name="Sam Sahragard",
                        email="sam@cenaskitchen.com",
                        passcode_hash=generate_password_hash("12345"),
                        permission_level="partner",
                        store_scope=None,
                        first_login_done=False,
                        active=True,
                    ))
                    db.commit()
                    logging.getLogger(__name__).info(
                        "users: seeded Sam as partner with bootstrap passcode (first_login_done=False)")
            finally:
                db.close()
    except Exception:
        logging.getLogger(__name__).exception("users seed failed (non-fatal)")

    # Idempotent column backfill for legal_matters.key_dates (added
    # 2026-05-13 Phase 0 Block 3 follow-up). create_all only creates
    # missing tables, not new columns on existing ones.
    try:
        from sqlalchemy import inspect as _sa_inspect_legal, text as _sa_text_legal
        from app.db import engine as _eng_legal
        if _eng_legal is not None:
            insp = _sa_inspect_legal(_eng_legal)
            if "legal_matters" in insp.get_table_names():
                existing = {c["name"] for c in insp.get_columns("legal_matters")}
                if "key_dates" not in existing:
                    with _eng_legal.begin() as conn:
                        # Cross-dialect JSON: Postgres has native JSON,
                        # SQLite falls back to TEXT — SQLAlchemy reads
                        # both via its JSON type.
                        json_type = "JSONB" if _eng_legal.dialect.name == "postgresql" else "TEXT"
                        conn.execute(_sa_text_legal(f"ALTER TABLE legal_matters ADD COLUMN key_dates {json_type}"))
                    logging.getLogger(__name__).info(
                        "legal_matters: backfilled key_dates column")
    except Exception:
        logging.getLogger(__name__).exception(
            "legal_matters.key_dates backfill failed (non-fatal)")

    # One-shot migration: any LegalMatter that has a non-empty `notes`
    # text field but no LegalMatterNote rows yet gets a single 'first
    # note' inserted from the legacy text. Idempotent — runs once per
    # matter (skip if at least one note already exists).
    try:
        from app.db import SessionLocal
        from app.models import LegalMatter, LegalMatterNote
        if SessionLocal is not None:
            db = SessionLocal()
            try:
                migrated = 0
                # Only inspect matters with non-empty notes (cheap filter).
                candidates = (db.query(LegalMatter)
                                .filter(LegalMatter.notes.isnot(None))
                                .all())
                for m in candidates:
                    if not (m.notes and m.notes.strip()):
                        continue
                    has_note = (db.query(LegalMatterNote)
                                  .filter(LegalMatterNote.matter_id == m.id)
                                  .first())
                    if has_note is not None:
                        continue
                    db.add(LegalMatterNote(
                        matter_id=m.id,
                        body=m.notes.strip(),
                        actor_label="(migrated from legacy notes field)",
                        # Inherit the matter's creation time so the
                        # timeline starts when the matter started.
                        created_at=m.created_at,
                    ))
                    migrated += 1
                if migrated:
                    db.commit()
                    logging.getLogger(__name__).info(
                        "legal_matter_note: migrated %d legacy notes", migrated)
            finally:
                db.close()
    except Exception:
        logging.getLogger(__name__).exception(
            "legal_matter_note migration failed (non-fatal)")

    # Start the IMAP poller for produce vendor pricing. No-op unless
    # PRODUCE_INGEST_ENABLED=1 is set (Render). Cross-process file lock
    # ensures only one gunicorn worker actually polls.
    produce_ingest.start_in_background()

    @app.cli.command("create-driver")
    @click.argument("name")
    @click.argument("location")
    def create_driver(name: str, location: str):
        from app.db import SessionLocal
        from app.models import Driver
        db = SessionLocal()
        try:
            if db.query(Driver).filter_by(name=name, location=location).first():
                click.echo(f"Driver '{name}' at '{location}' already exists")
                return
            db.add(Driver(name=name, location=location))
            db.commit()
            click.echo(f"Driver '{name}' created at '{location}'")
        except Exception as e:
            db.rollback()
            click.echo(f"Error: {e}")
        finally:
            db.close()

    return app