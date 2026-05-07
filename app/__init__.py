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

    # Install the shared-password gate AFTER all other blueprints so the
    # before_request hook sees their routes. Webhook + ingest endpoints
    # are exempted inside auth.install().
    ezauth.install(app)

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