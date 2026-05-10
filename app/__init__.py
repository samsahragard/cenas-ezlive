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
from app.web import auth as ezauth
from app.services import produce_ingest


def create_app():
    load_dotenv(override=True)

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")

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
    # Corporate-order Blueprint mounts under <store_slug> just like store_bp;
    # has its own url_value_preprocessor + partner_gate so it's standalone.
    app.register_blueprint(corp_order_bp, url_prefix="/<store_slug>")

    # Install the shared-password gate AFTER all other blueprints so the
    # before_request hook sees their routes. Webhook + ingest endpoints
    # are exempted inside auth.install().
    ezauth.install(app)

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

    # Idempotent column backfill for orders.total_amount (migration 10).
    # Same self-healing pattern as the drivers backfill above.
    try:
        from sqlalchemy import inspect as _sa_inspect2, text as _sa_text2
        from app.db import engine as _eng2
        if _eng2 is not None:
            insp = _sa_inspect2(_eng2)
            if "orders" in insp.get_table_names():
                existing = {c["name"] for c in insp.get_columns("orders")}
                if "total_amount" not in existing:
                    with _eng2.begin() as conn:
                        conn.execute(_sa_text2("ALTER TABLE orders ADD COLUMN total_amount FLOAT"))
                    logging.getLogger(__name__).info("orders table: added total_amount column")
    except Exception:
        logging.getLogger(__name__).exception("orders column backfill failed (non-fatal)")

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