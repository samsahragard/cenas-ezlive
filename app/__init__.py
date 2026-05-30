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
from app.web import schedules_v2  # noqa: F401  B4: attaches schedule/shift routes to store_bp (must precede register_blueprint)
from app.web import schedules_v2_pages  # noqa: F401  B4: attaches the manager week-view PAGE route to store_bp (ck; must precede register_blueprint)
from app.web import schedules_v2_timeoff  # noqa: F401  B7: attaches the manager time-off review routes to store_bp (ckai; must precede register_blueprint)
from app.web import schedules_v2_availability  # noqa: F401  B8: attaches the manager availability view to store_bp (ckai; must precede register_blueprint)
from app.web import schedules_v2_market  # noqa: F401  B9: attaches the manager offer/swap approval routes to store_bp (ckai; must precede register_blueprint)
from app.web.developer_chat import dev_chat as dev_chat_bp
from app.web.interview import interview as interview_bp
from app.web.corporate_order import corp_order as corp_order_bp
from app.web.ezcater_import_routes import ezc_import as ezc_import_bp
from app.web.ezcater_live_routes import ezc_live as ezc_live_bp
from app.web.team_routes import team_bp
from app.web.legal_routes import legal as legal_bp
from app.web.access_request_routes import access_req as access_req_bp
from app.web.driver_system import driver_system_bp
from app.web.scheduling_cron import scheduling_cron_bp  # B6: shift-alarm send cron (ckai)
from app.web.briefs import briefs_bp
from app.web.tasks import tasks_bp
from app.web.team_reports import team_reports_bp
from app.web import auth as ezauth
from app.web import keypad_auth as ezkeypad
from app.web import employee_auth as ezempauth
from app.web import employee_schedule_page  # noqa: F401  B5: attaches GET /employee/my-schedule to the employee_auth blueprint (ck; must import before ezempauth.install)
from app.web import schedules_v2_employee  # noqa: F401  B5: attaches the employee schedule DATA + accept/decline endpoints to the employee_auth blueprint (aick; must import before ezempauth.install)
from app.web import employee_alarm_prefs  # noqa: F401  B6: attaches GET/POST /employee/alarm-preferences to the employee_auth blueprint (ckai; must import before ezempauth.install)
from app.web import employee_profile_page  # noqa: F401  B6: attaches GET /employee/profile (alarm-preferences UI) to the employee_auth blueprint (ck; must import before ezempauth.install)
from app.web import employee_time_off  # noqa: F401  B7: attaches the employee time-off endpoints to the employee_auth blueprint (ckai; must import before ezempauth.install)
from app.web import employee_time_off_page  # noqa: F401  B7: attaches GET /employee/time-off (time-off request UI) to the employee_auth blueprint (ck; must import before ezempauth.install)
from app.web import employee_availability  # noqa: F401  B8: attaches the employee availability endpoints to the employee_auth blueprint (ckai; must import before ezempauth.install)
from app.web import employee_availability_page  # noqa: F401  B8: attaches GET /employee/availability (availability editor) to the employee_auth blueprint (ck; must import before ezempauth.install)
from app.web import employee_shift_market  # noqa: F401  B9: attaches the employee offer/swap/marketplace endpoints to the employee_auth blueprint (ckai; must import before ezempauth.install)
from app.web import anomaly_routes as ezanomaly
from app.web import ribbon_routes as ezribbon
from app.web import notifications as eznotifications
from app.web import sam_chat as ezsamchat
from app.web import cena as ezcena
from app.services import produce_ingest
from app.services import permissions as ezperms


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
    # Interview Tracker (Sam #5:48) — partner-only candidate hiring
    # pipeline. Routes registered at /partner/interview-tracker
    # directly (not under store_bp), so not auto-partner-gated; each
    # route re-checks the session flag via _enforce_partner(). See
    # app/web/interview.py.
    app.register_blueprint(interview_bp)
    app.register_blueprint(ezc_import_bp)
    app.register_blueprint(ezc_live_bp)
    app.register_blueprint(team_bp)
    app.register_blueprint(legal_bp)
    app.register_blueprint(access_req_bp)
    app.register_blueprint(driver_system_bp)
    app.register_blueprint(scheduling_cron_bp)  # B6: POST /internal/scheduling/cron/process-shift-alarms (ckai)
    app.register_blueprint(briefs_bp)
    # Phase 2 / Block 1A — task create + reassign routes
    # (/partner/tasks/*). Registered directly (not under store_bp), so
    # not auto-partner-gated; the routes require g.current_user and use
    # can_assign_to as the authorization gate. See app/web/tasks.py.
    app.register_blueprint(tasks_bp)
    # Team-reports tab (Phase 2 / Block 1G, ck 2026-05-14). The
    # task-based team-reports tab at /partner/team-reports/. Every
    # route carries @requires_permission("team_reports.view"); store
    # scope is server-derived from current_user (a GM is confined to
    # their own store, no request-param override). See
    # app/web/team_reports.py.
    app.register_blueprint(team_reports_bp)
    # Corporate-order Blueprint mounts under <store_slug> just like store_bp;
    # has its own url_value_preprocessor + partner_gate so it's standalone.
    app.register_blueprint(corp_order_bp, url_prefix="/<store_slug>")

    # docck v1 — /docck/* endpoints (Sam #1191 multi-agent reliability monitor)
    from app.web.docck import bp as docck_bp
    app.register_blueprint(docck_bp)

    # docck self-tick — background monitoring thread (Sam #1257: docck drives
    # itself, no external trigger dependency on aick). Multi-worker safe via DB lease.
    try:
        from app.services.docck_monitor import start_background_ticker
        start_background_ticker()
    except Exception:
        logging.getLogger(__name__).exception("docck background ticker failed to start (non-fatal)")
    # Install the shared-password gate AFTER all other blueprints so the
    # before_request hook sees their routes. Webhook + ingest endpoints
    # are exempted inside auth.install().
    ezauth.install(app)
    # Keypad-auth (migration 13) — new per-person 5-digit passcode flow.
    # Registers /keypad-login, /change-passcode, /keypad-logout and a
    # before_request that stashes g.current_user. Must come after ezauth so
    # its EXEMPT_PREFIXES updates win the path-match race for /keypad-login.
    ezkeypad.install(app)
    # Schedules V2 B2 (ckai): employee SMS-login endpoints + the employee->
    # /partner firewall. After ezauth/ezkeypad so the global gate is
    # registered first; the firewall 403s any employee session on /partner/*.
    ezempauth.install(app)
    # Role dashboard badge (Sam 2026-05-21). The marquee badge in
    # base_dashboard.html shows on EVERY page, so the role-matched banner
    # stem must be resolvable app-wide. register_dashboard_banner installs
    # an @app.context_processor that injects `dashboard_banner` into every
    # template; it mirrors base_dashboard.html's user_role detection
    # (driver session / permission_level). Must come after ezkeypad so the
    # _attach_current_user before_request that sets g.current_user is
    # registered first. Routes that pass an explicit dashboard_banner still
    # override the context-processor value.
    from app.web.ezcater_routes import register_dashboard_banner
    register_dashboard_banner(app)
    # Anomaly blueprint + Jinja global `anomaly_signals_for(page_slug)`.
    # Phase 1 / Block 3 (ck 2026-05-13). Templates opt in by setting
    # `anomaly_page_slug = '<slug>'` before {% block content %} — the
    # base layout's partial picks it up and renders cards.
    ezanomaly.install(app)
    # Permission system (Phase 0 / Block 4, ck 2026-05-13). Registers the
    # `has_permission` Jinja global so templates can hide UI on missing
    # tags. Decorator + ROLE_PERMISSIONS dict + _user_has live in
    # app/services/permissions.py. Dark-launch by default — flip
    # PERMISSION_ENFORCE=1 once denial logs show no legit flows
    # blocked.
    ezperms.install(app)
    # Universal ribbon (Phase 2 / Block 1B, ck 2026-05-14). Registers
    # the /partner/ribbon/collapse/<category> endpoint + two Jinja
    # globals: ribbon_items_for (the stub content router from
    # app/services/ribbon.py — 1C replaces the body) and
    # ribbon_render_context (1B's defensive presentation-layer wrapper
    # that _ribbon.html actually calls). The partial is mounted from
    # base_dashboard.html above {% block content %}.
    ezribbon.install(app)
    # Notifications page (Sam approved dck mockup #2641, cena #2569 + #2628
    # two-commit + behavior-parity-gate). Commit 1: /partner/notifications
    # route + ribbon_render_context() reuse. Ribbon stays live as the
    # parity-test baseline; commit 2 retires the ribbon in a separate PR
    # only after Sam validates a real operational beat on live.
    eznotifications.install(app)
    # Sam Chat (standalone, Sam request 2026-05-14). Registers /sam/chat
    # + the is_sam_chat_user Jinja global (the sidebar link uses it).
    # Hard-gated to SAM_CHAT_USER_ID — dormant/safe-closed until that
    # env var is set. Deliberately isolated from the agentic pipeline.
    ezsamchat.install(app)

    # Cena — Sam's personal operational AI surface. Two routes:
    # POST /sam/cena/log (gateway ingress, X-Cena-Token auth) and
    # GET /sam/cena-audit/ (Sam-only viewer). Shares the SAM_CHAT_USER_ID
    # gate with sam_chat — also dormant when that env is unset.
    ezcena.install(app)

    # Physical-kitchen pickup label for driver-facing templates. The raw
    # Order.reported_store is the ezCater storefront-of-record (ghost for
    # store_3/store_4); pickup_label collapses to the actual kitchen.
    # See app/domain/normalize.py and samai #1488. Audit / review templates
    # deliberately keep reading reported_store directly.
    from app.domain.normalize import pickup_label as _pickup_label
    app.jinja_env.globals["pickup_label"] = _pickup_label

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

    # docck v1 - multi-agent reliability monitor seed (Sam #1191, samai #1208)
    try:
        from app.services.docck_seed import seed_docck_agents
        seed_docck_agents()
    except Exception:
        logging.getLogger(__name__).exception("docck seed failed (non-fatal)")

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
                # Sam #1025 2026-05-19 — battery-optimization whitelist state
                # reported by the native plugin at shift start.
                ("battery_opt_ignored",     "BOOLEAN"),
                ("battery_opt_checked_at",  "TIMESTAMP"),
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

    # Idempotent column backfill for the manager payroll-input columns
    # (Sam #1492/#1503, 2026-05-28 — migration 41). alembic isn't wired on
    # Render, so these gated-absence ALTERs are how the columns reach prod.
    # Managers fill verified miles / $10 bonus / 5-star / notes from the Ez
    # Drivers page; compute_one() prefers them over the auto estimate.
    try:
        from sqlalchemy import inspect as _sa_inspect_pi, text as _sa_text_pi
        from app.db import engine as _eng_pi
        if _eng_pi is not None:
            insp = _sa_inspect_pi(_eng_pi)
            if "orders" in insp.get_table_names():
                existing = {c["name"] for c in insp.get_columns("orders")}
                payroll_input_cols = [
                    ("pay_verified_miles", "FLOAT"),
                    ("pay_driven_miles",   "FLOAT"),
                    ("pay_bonus_tracked",  "BOOLEAN"),
                    ("pay_five_star",      "BOOLEAN"),
                    ("pay_notes",          "VARCHAR(500)"),
                    ("pay_verified_at",    "TIMESTAMP"),
                    ("pay_verified_by",    "VARCHAR(80)"),
                ]
                added = []
                with _eng_pi.begin() as conn:
                    for col_name, col_def in payroll_input_cols:
                        if col_name not in existing:
                            conn.execute(_sa_text_pi(f"ALTER TABLE orders ADD COLUMN {col_name} {col_def}"))
                            added.append(col_name)
                if added:
                    logging.getLogger(__name__).info("orders table: backfilled payroll-input columns %s", added)
    except Exception:
        logging.getLogger(__name__).exception("payroll-input column backfill failed (non-fatal)")

    # Idempotent column backfill for sam_chat_messages cache-token columns
    # (migration 25, Sam #1895 caching SECONDARY per samai #2058 amendment).
    # Captures cache_creation_input_tokens + cache_read_input_tokens from
    # the gateway SSE done event so prod surfaces the cache-savings
    # accounting Sam's #1895 cost-impact projection promised.
    try:
        from sqlalchemy import inspect as _sa_inspect_25, text as _sa_text_25
        from app.db import engine as _eng_25
        if _eng_25 is not None:
            insp = _sa_inspect_25(_eng_25)
            if "sam_chat_messages" in insp.get_table_names():
                existing = {c["name"] for c in insp.get_columns("sam_chat_messages")}
                additions = [
                    ("cost_cache_creation_tokens", "INTEGER"),
                    ("cost_cache_read_tokens",     "INTEGER"),
                ]
                added = []
                with _eng_25.begin() as conn:
                    for col_name, col_def in additions:
                        if col_name not in existing:
                            conn.execute(_sa_text_25(
                                f"ALTER TABLE sam_chat_messages "
                                f"ADD COLUMN {col_name} {col_def}"))
                            added.append(col_name)
                if added:
                    logging.getLogger(__name__).info(
                        "sam_chat_messages (migration 25): backfilled %s",
                        added)
    except Exception:
        logging.getLogger(__name__).exception(
            "sam_chat_messages cache-token backfill failed (non-fatal)")

    # Idempotent table + data backfill for dev_chat_attribution_corrections
    # (migration 26, Sam #2031 item 3 + samai #2024 sidecar plan). Created
    # as a response to the 2026-05-17 attribution incident: 4 ck-authored
    # work reports landed in developer_chat under author='sam' because the
    # POST handler at developer_chat.py:119 defaulted a missing form field
    # to "sam". 4c25f3b changed the default to "unknown" (bleeding stop);
    # this sidecar audits the rows already in the DB by overlaying the
    # corrected author without mutating the source row. Alembic isn't
    # wired on Render so we create the table + seed the 4 known rows here.
    try:
        from sqlalchemy import inspect as _sa_inspect_26
        from app.db import engine as _eng_26, SessionLocal as _SL_26
        from app.models import (Base as _Base_26,
                                DevChatAttributionCorrection as _DCAC_26)
        if _eng_26 is not None:
            insp = _sa_inspect_26(_eng_26)
            existing_tables = set(insp.get_table_names())
            if "dev_chat_attribution_corrections" not in existing_tables:
                # Create just this one table; metadata.create_all is a no-op
                # for already-present tables but only operates on tables it
                # knows about, so passing tables=[...] scopes it precisely.
                _DCAC_26.__table__.create(_eng_26)
                logging.getLogger(__name__).info(
                    "dev_chat_attribution_corrections: table created")
            # Seed the 4 known mislabels from the 2026-05-17 incident.
            # message_id is UNIQUE so re-running is a no-op; only inserts
            # for ids that don't already have a correction row.
            _reason_26 = (
                "2026-05-17 attribution incident: ck posted via "
                "Chrome-MCP-fetch without the author form field; "
                "developer_chat.py:119 default 'sam' applied. "
                "Body sign-off '-- ck' confirms actual author. "
                "aick #2013 diagnosis + samai #2016 sweep + Sam "
                "#2031 cleanup directive.")
            _2026_05_17_mislabels = [(2051, "sam", "ck"),
                                     (2056, "sam", "ck"),
                                     (2097, "sam", "ck"),
                                     (2098, "sam", "ck")]
            db_26 = _SL_26()
            try:
                existing_corr_msg_ids = {
                    r[0] for r in db_26.query(_DCAC_26.message_id).all()
                }
                seeded_26: list[int] = []
                for mid, orig, corr in _2026_05_17_mislabels:
                    if mid in existing_corr_msg_ids:
                        continue
                    db_26.add(_DCAC_26(message_id=mid,
                                       original_author=orig,
                                       corrected_author=corr,
                                       correction_reason=_reason_26,
                                       corrected_by="samai"))
                    seeded_26.append(mid)
                if seeded_26:
                    db_26.commit()
                    logging.getLogger(__name__).info(
                        "dev_chat_attribution_corrections: seeded %d rows %s",
                        len(seeded_26), seeded_26)
            finally:
                db_26.close()
    except Exception:
        logging.getLogger(__name__).exception(
            "dev_chat_attribution_corrections backfill failed (non-fatal)")

    # Idempotent table create — sample_approvals + sample_approval_attachments
    # (migration 28, cena #2549 item 2 + dck 68c5248 spec + ck #2548 dep-chain).
    # Sam approval workflow for the /partner/developer/samples page:
    # one row per sample_slug (latest state only, no history table in v1),
    # cascading attachments. Boot-time create via metadata.create_all
    # scoped to just these two tables — no-op once present.
    try:
        from sqlalchemy import inspect as _sa_insp_28
        from app.db import engine as _eng_28
        from app.models import (Base as _Base_28,
                                SampleApproval as _SA_28,
                                SampleApprovalAttachment as _SAA_28)
        if _eng_28 is not None:
            insp_28 = _sa_insp_28(_eng_28)
            existing_28 = set(insp_28.get_table_names())
            tables_to_create = []
            if "sample_approvals" not in existing_28:
                tables_to_create.append(_SA_28.__table__)
            if "sample_approval_attachments" not in existing_28:
                tables_to_create.append(_SAA_28.__table__)
            if tables_to_create:
                _Base_28.metadata.create_all(
                    bind=_eng_28, tables=tables_to_create)
                logging.getLogger(__name__).info(
                    "sample_approvals (migration 28): created %d table(s) %s",
                    len(tables_to_create),
                    [t.name for t in tables_to_create])
    except Exception:
        logging.getLogger(__name__).exception(
            "sample_approvals backfill failed (non-fatal)")

    # Idempotent table create — vendor_recent_orders (Sam #837 items
    # 9-12 vendor email watchers framework). Each parsed vendor email
    # lands as a row so /<store>/vendors/<vendor>/recent-orders renders.
    try:
        from sqlalchemy import inspect as _sa_insp_32
        from app.db import engine as _eng_32
        from app.models import (Base as _Base_32,
                                VendorRecentOrder as _VRO_32)
        if _eng_32 is not None:
            insp_32 = _sa_insp_32(_eng_32)
            if "vendor_recent_orders" not in set(insp_32.get_table_names()):
                _Base_32.metadata.create_all(
                    bind=_eng_32, tables=[_VRO_32.__table__])
                logging.getLogger(__name__).info(
                    "vendor_recent_orders: table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "vendor_recent_orders backfill failed (non-fatal)")

    # Idempotent table create — sam_chat_attachments (Sam #837 item 5
    # vision parity for dev-team agents). Persists image/PDF blocks so
    # aick / ck / samai can fetch the same files cena saw at API time.
    try:
        from sqlalchemy import inspect as _sa_insp_31
        from app.db import engine as _eng_31
        from app.models import (Base as _Base_31,
                                SamChatAttachment as _SCAT_31)
        if _eng_31 is not None:
            insp_31 = _sa_insp_31(_eng_31)
            if "sam_chat_attachments" not in set(insp_31.get_table_names()):
                _Base_31.metadata.create_all(
                    bind=_eng_31, tables=[_SCAT_31.__table__])
                logging.getLogger(__name__).info(
                    "sam_chat_attachments: table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "sam_chat_attachments backfill failed (non-fatal)")

    # Idempotent table create — cena_usage_logs (Sam /sam/chat session 13
    # #11 cost telemetry). One row per cena gateway streaming turn
    # capturing token counts so /partner/cena-usage can roll up dollars.
    try:
        from sqlalchemy import inspect as _sa_insp_30
        from app.db import engine as _eng_30
        from app.models import (Base as _Base_30,
                                CenaUsageLog as _CUL_30)
        if _eng_30 is not None:
            insp_30 = _sa_insp_30(_eng_30)
            if "cena_usage_logs" not in set(insp_30.get_table_names()):
                _Base_30.metadata.create_all(
                    bind=_eng_30, tables=[_CUL_30.__table__])
                logging.getLogger(__name__).info(
                    "cena_usage_logs: table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "cena_usage_logs backfill failed (non-fatal)")

    # Idempotent table create — in_house_catering_quotes (Sam #837 item 16
    # + cena #1031 2026-05-19). Staff-built custom quotes for in-house
    # catering orders off the Cenas Fajitas menu.
    try:
        from sqlalchemy import inspect as _sa_insp_33
        from app.db import engine as _eng_33
        from app.models import (Base as _Base_33,
                                InHouseCateringQuote as _IHCQ_33)
        if _eng_33 is not None:
            insp_33 = _sa_insp_33(_eng_33)
            if "in_house_catering_quotes" not in set(insp_33.get_table_names()):
                _Base_33.metadata.create_all(
                    bind=_eng_33, tables=[_IHCQ_33.__table__])
                logging.getLogger(__name__).info(
                    "in_house_catering_quotes: table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "in_house_catering_quotes backfill failed (non-fatal)")

    # Idempotent table create — manager pages (14 tables, Sam #1102 +
    # cena #1111 2026-05-19). All share ManagerLogMixin shape.
    try:
        from sqlalchemy import inspect as _sa_insp_34
        from app.db import engine as _eng_34
        from app.models import (
            Base as _Base_34,
            DailyManagerLog, ShiftHandoff, IncidentReport, SupplyRequest,
            DailyGoals, StaffFeedback, PreShiftChecklist, CloseOfDayAudit,
            RecipePage, AttendanceTracking, InterviewSurface, TrainingRecord,
            MaintenanceRequest, EmployeeCounseling,
        )
        if _eng_34 is not None:
            insp_34 = _sa_insp_34(_eng_34)
            existing_tables = set(insp_34.get_table_names())
            _MGR_MODELS = [
                DailyManagerLog, ShiftHandoff, IncidentReport, SupplyRequest,
                DailyGoals, StaffFeedback, PreShiftChecklist, CloseOfDayAudit,
                RecipePage, AttendanceTracking, InterviewSurface,
                TrainingRecord, MaintenanceRequest, EmployeeCounseling,
            ]
            to_create = [m.__table__ for m in _MGR_MODELS
                         if m.__tablename__ not in existing_tables]
            if to_create:
                _Base_34.metadata.create_all(bind=_eng_34, tables=to_create)
                logging.getLogger(__name__).info(
                    "manager pages: created %d tables (%s)",
                    len(to_create), [t.name for t in to_create])
    except Exception:
        logging.getLogger(__name__).exception(
            "manager pages table backfill failed (non-fatal)")

    # Idempotent table create — recipes + fresh_food (Sam #1130-#1144
    # 2026-05-19). Recipes single table; Fresh Food normalized header
    # + line tables for daily-grid + fulfillment shape.
    try:
        from sqlalchemy import inspect as _sa_insp_35
        from app.db import engine as _eng_35
        from app.models import (
            Base as _Base_35, Recipe as _R_35,
            FreshFoodOrder as _FFO_35, FreshFoodOrderLine as _FFL_35,
        )
        if _eng_35 is not None:
            insp_35 = _sa_insp_35(_eng_35)
            existing = set(insp_35.get_table_names())
            to_create = [m.__table__ for m in (_R_35, _FFO_35, _FFL_35)
                         if m.__tablename__ not in existing]
            if to_create:
                _Base_35.metadata.create_all(bind=_eng_35, tables=to_create)
                logging.getLogger(__name__).info(
                    "recipes + fresh_food: created %d tables (%s)",
                    len(to_create), [t.name for t in to_create])
    except Exception:
        logging.getLogger(__name__).exception(
            "recipes + fresh_food table backfill failed (non-fatal)")

    # Idempotent table create — attendance tracking v3 (Sam #10:14,
    # dck build). AttendanceShift = per-employee-per-day clock board;
    # AttendanceEvent = its timeline. New tables, additive.
    try:
        from sqlalchemy import inspect as _sa_insp_36
        from app.db import engine as _eng_36
        from app.models import (
            Base as _Base_36, AttendanceShift as _AS_36, AttendanceEvent as _AE_36,
        )
        if _eng_36 is not None:
            insp_36 = _sa_insp_36(_eng_36)
            existing = set(insp_36.get_table_names())
            to_create = [m.__table__ for m in (_AS_36, _AE_36)
                         if m.__tablename__ not in existing]
            if to_create:
                _Base_36.metadata.create_all(bind=_eng_36, tables=to_create)
                logging.getLogger(__name__).info(
                    "attendance v3: created %d tables (%s)",
                    len(to_create), [t.name for t in to_create])
    except Exception:
        logging.getLogger(__name__).exception(
            "attendance v3 table backfill failed (non-fatal)")

    # Idempotent table create — ezcater_order_details (migration 36,
    # Sam #530 PDF pipeline + Cena #534 field-list lock). Holds the
    # PDF-only extraction fields that the ezCater Partner API does not
    # surface (per-item prices, setup-piece counts, dietary notes,
    # day-of contact, gate codes, special-instructions free-text, fee
    # breakdown). One row per external_order_id with UPSERT semantics.
    try:
        from sqlalchemy import inspect as _sa_insp_eod
        from app.db import engine as _eng_eod
        from app.models import Base as _Base_eod, EzcaterOrderDetails as _EOD_eod
        if _eng_eod is not None:
            insp_eod = _sa_insp_eod(_eng_eod)
            if "ezcater_order_details" not in set(insp_eod.get_table_names()):
                _Base_eod.metadata.create_all(
                    bind=_eng_eod, tables=[_EOD_eod.__table__])
                logging.getLogger(__name__).info(
                    "ezcater_order_details: table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "ezcater_order_details backfill failed (non-fatal)")

    # Idempotent table create — driver_assignment_jobs (migration 37,
    # Sam #669 driver-assignment build). One row per re-assignment job
    # the catering Ez Orders dropdown spawns; consumed by the Selenium
    # flow in app/services/ezcater_driver_assigner.py.
    try:
        from sqlalchemy import inspect as _sa_insp_daj
        from app.db import engine as _eng_daj
        from app.models import Base as _Base_daj, DriverAssignmentJob as _DAJ_daj
        if _eng_daj is not None:
            insp_daj = _sa_insp_daj(_eng_daj)
            if "driver_assignment_jobs" not in set(insp_daj.get_table_names()):
                _Base_daj.metadata.create_all(
                    bind=_eng_daj, tables=[_DAJ_daj.__table__])
                logging.getLogger(__name__).info(
                    "driver_assignment_jobs: table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "driver_assignment_jobs backfill failed (non-fatal)")

    # Idempotent table create — interview_candidates (Interview Tracker,
    # Sam #5:48, aick build). The unconditional Base.metadata.create_all
    # above already covers this new table; this scoped block mirrors the
    # per-feature backfill pattern and logs the create explicitly.
    try:
        from sqlalchemy import inspect as _sa_insp_ivt
        from app.db import engine as _eng_ivt
        from app.models import Base as _Base_ivt, Candidate as _Cand_ivt
        if _eng_ivt is not None:
            insp_ivt = _sa_insp_ivt(_eng_ivt)
            if "interview_candidates" not in set(insp_ivt.get_table_names()):
                _Base_ivt.metadata.create_all(
                    bind=_eng_ivt, tables=[_Cand_ivt.__table__])
                logging.getLogger(__name__).info(
                    "interview_candidates: table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "interview_candidates table backfill failed (non-fatal)")

    # Idempotent table create + master-list seed — prep list v3 (Sam,
    # dck build). PrepItem = master prep list (44 items seeded from the
    # rendering); PrepEntry = per-item-per-day working row. Additive.
    try:
        from sqlalchemy import inspect as _sa_insp_pl
        from app.db import engine as _eng_pl, SessionLocal as _SL_pl
        from app.models import (
            Base as _Base_pl, PrepItem as _PI_pl, PrepEntry as _PE_pl,
        )
        if _eng_pl is not None:
            insp_pl = _sa_insp_pl(_eng_pl)
            existing = set(insp_pl.get_table_names())
            to_create = [m.__table__ for m in (_PI_pl, _PE_pl)
                         if m.__tablename__ not in existing]
            if to_create:
                _Base_pl.metadata.create_all(bind=_eng_pl, tables=to_create)
                logging.getLogger(__name__).info(
                    "prep list v3: created %d tables (%s)",
                    len(to_create), [t.name for t in to_create])
            _prep_seed = [
                ("hot", "item", ["Masa Flour", "Charros", "Refried",
                    "Black Bean", "Costillas", "Cochina", "Taco Meat",
                    "Pollo Ranchero", "Chicken Stock", "Mexican Butter",
                    "Vegetales", "Charro Mix", "Spinach Mix"]),
                ("hot", "sauce", ["Seafood Sauce", "Tomatillo Mix",
                    "Tomatillo Sauce", "Ranchera Sauce", "Poblano Sauce",
                    "Street Taco Sauce", "BBQ Sauce", "Chile Con Queso",
                    "Chile Gravy", "Chips"]),
                ("cold", "item", ["Salad Mix", "Shredded Lettuce",
                    "Cabbage Mix", "Pickled Onions"]),
                ("cold", "sauce", ["Roja", "Verde", "Ranch",
                    "Avocado Ranch", "Honey Mustard",
                    "Beef Fajita Marination", "Chipotle Mayo",
                    "Chipotle Cream", "Cilantro Ginger"]),
                ("chop", "item", ["Cebolla de Parrilla", "Cebolla Pelado",
                    "Onions Chop", "Bell Pepper", "Enchilada Cheese",
                    "Queso Fresco", "Poblano", "Mango"]),
            ]
            _db_pl = _SL_pl()
            try:
                if _db_pl.query(_PI_pl).first() is None:
                    _so = 0
                    for _cat_pl, _kind_pl, _names_pl in _prep_seed:
                        for _nm_pl in _names_pl:
                            _db_pl.add(_PI_pl(name=_nm_pl, category=_cat_pl,
                                              kind=_kind_pl, sort_order=_so))
                            _so += 1
                    _db_pl.commit()
                    logging.getLogger(__name__).info(
                        "prep list v3: seeded %d master items", _so)
            finally:
                _db_pl.close()
    except Exception:
        logging.getLogger(__name__).exception(
            "prep list v3 table backfill/seed failed (non-fatal)")

    # Idempotent table create — cena_wake_decisions (migration 29,
    # Sam #2576 6-piece proposal Phase A piece #3 — telemetry-first).
    # One row per dev chat message considered by the watcher; captures
    # classifier verdict + tokens + latency + watcher's actual decision
    # so the cena-stats dashboard can compute would-have-fired vs
    # did-fire delta during shadow-mode observation period.
    try:
        from sqlalchemy import inspect as _sa_insp_29
        from app.db import engine as _eng_29
        from app.models import (Base as _Base_29,
                                CenaWakeDecision as _CWD_29)
        if _eng_29 is not None:
            insp_29 = _sa_insp_29(_eng_29)
            if "cena_wake_decisions" not in set(insp_29.get_table_names()):
                _Base_29.metadata.create_all(
                    bind=_eng_29, tables=[_CWD_29.__table__])
                logging.getLogger(__name__).info(
                    "cena_wake_decisions (migration 29): table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "cena_wake_decisions backfill failed (non-fatal)")

    # developer_chat_archive (migration 30, Sam dev chat 2026-05-19 4:07pm
    # + samai #2980 spec). Append-only archive table; one-time bulk wipe
    # + rolling 200/100 cap copy oldest-N here before DELETE from
    # developer_chat.
    try:
        from sqlalchemy import inspect as _sa_insp_dca
        from app.db import engine as _eng_dca
        from app.models import (Base as _Base_dca,
                                DeveloperChatMessageArchive as _DCMA)
        if _eng_dca is not None:
            insp_dca = _sa_insp_dca(_eng_dca)
            if "developer_chat_archive" not in set(insp_dca.get_table_names()):
                _Base_dca.metadata.create_all(
                    bind=_eng_dca, tables=[_DCMA.__table__])
                logging.getLogger(__name__).info(
                    "developer_chat_archive (migration 30): table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "developer_chat_archive backfill failed (non-fatal)")

    # manager_daily_log v3 fields + daily_log_entry_image (migration 31,
    # dck build-order #2 2026-05-19). Additive: 6 columns on the existing
    # manager_daily_log table + a new image table. Render runs no alembic
    # — this idempotent backfill is the real schema apply.
    try:
        from sqlalchemy import inspect as _sa_insp_31, text as _sa_text_31
        from app.db import engine as _eng_31m
        from app.models import Base as _Base_31m, DailyLogEntryImage as _DLEI
        if _eng_31m is not None:
            insp_31 = _sa_insp_31(_eng_31m)
            tables_31 = set(insp_31.get_table_names())
            if "manager_daily_log" in tables_31:
                existing_31 = {c["name"]
                               for c in insp_31.get_columns("manager_daily_log")}
                _cols_31 = [
                    ("module",         "VARCHAR(20) NOT NULL DEFAULT 'general'"),
                    ("subject",        "VARCHAR(24) NOT NULL DEFAULT 'general'"),
                    ("issue",          "VARCHAR(16) NOT NULL DEFAULT 'general'"),
                    ("priority",       "VARCHAR(10) NOT NULL DEFAULT 'low'"),
                    ("entry_date",     "DATE NOT NULL DEFAULT CURRENT_DATE"),
                    ("show_on_roster", "BOOLEAN NOT NULL DEFAULT 0"),
                ]
                with _eng_31m.begin() as _conn_31:
                    for _name, _ddl in _cols_31:
                        if _name not in existing_31:
                            _conn_31.execute(_sa_text_31(
                                f"ALTER TABLE manager_daily_log "
                                f"ADD COLUMN {_name} {_ddl}"))
                            logging.getLogger(__name__).info(
                                "manager_daily_log: backfilled %s (migration 31)",
                                _name)
            if "daily_log_entry_image" not in tables_31:
                _Base_31m.metadata.create_all(
                    bind=_eng_31m, tables=[_DLEI.__table__])
                logging.getLogger(__name__).info(
                    "daily_log_entry_image (migration 31): table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "manager_daily_log v3 backfill failed (non-fatal)")

    # recipes code + english_instructions (migration 32,
    # ck build-order #3 2026-05-19). Additive: 2 columns on the
    # existing recipes table. Render runs no alembic — this
    # idempotent backfill is the real schema apply.
    try:
        from sqlalchemy import inspect as _sa_insp_32, text as _sa_text_32
        from app.db import engine as _eng_32r
        if _eng_32r is not None:
            insp_32 = _sa_insp_32(_eng_32r)
            tables_32 = set(insp_32.get_table_names())
            if "recipes" in tables_32:
                existing_32 = {c["name"]
                               for c in insp_32.get_columns("recipes")}
                _cols_32 = [
                    ("code",                 "VARCHAR(20) NULL"),
                    ("english_instructions", "TEXT NULL"),
                ]
                with _eng_32r.begin() as _conn_32:
                    for _name, _ddl in _cols_32:
                        if _name not in existing_32:
                            _conn_32.execute(_sa_text_32(
                                f"ALTER TABLE recipes "
                                f"ADD COLUMN {_name} {_ddl}"))
                            logging.getLogger(__name__).info(
                                "recipes: backfilled %s (migration 32)",
                                _name)
                    _idx_32 = {i["name"]
                               for i in insp_32.get_indexes("recipes")}
                    if "ix_recipes_code" not in _idx_32:
                        try:
                            _conn_32.execute(_sa_text_32(
                                "CREATE INDEX ix_recipes_code "
                                "ON recipes(code)"))
                            logging.getLogger(__name__).info(
                                "recipes: created ix_recipes_code (migration 32)")
                        except Exception:
                            pass  # index may race; non-fatal
    except Exception:
        logging.getLogger(__name__).exception(
            "recipes migration-32 backfill failed (non-fatal)")

    # Idempotent recipes seed (ck build-order #3, 2026-05-19).
    # Reads data/recipes/recipes_seed_data.json + inserts any rows
    # whose code isn't already present. Safe on every boot.
    try:
        from app.services.recipes_seed import seed_recipes_from_json
        _c, _s, _e = seed_recipes_from_json()
        if _c:
            logging.getLogger(__name__).info(
                "recipes seed: inserted %d (skipped %d, errored %d)",
                _c, _s, _e)
    except FileNotFoundError:
        pass  # fixture not on disk yet — non-fatal
    except Exception:
        logging.getLogger(__name__).exception(
            "recipes seed failed (non-fatal)")

    # manager_incident_report v3 fields (migration 33, ck build-order
    # Sam #10:11/#10:15 2026-05-19 — convert Incident Reports v1 text-
    # heavy shell to the samai+dck v3 design). Additive: 5 columns + 1
    # index on the existing manager_incident_report table.
    try:
        from sqlalchemy import inspect as _sa_insp_33, text as _sa_text_33
        from app.db import engine as _eng_33ir
        if _eng_33ir is not None:
            insp_33 = _sa_insp_33(_eng_33ir)
            tables_33 = set(insp_33.get_table_names())
            if "manager_incident_report" in tables_33:
                existing_33 = {c["name"]
                               for c in insp_33.get_columns("manager_incident_report")}
                _cols_33 = [
                    ("severity",      "VARCHAR(20) NOT NULL DEFAULT 'moderate'"),
                    ("status",        "VARCHAR(20) NOT NULL DEFAULT 'open'"),
                    ("incident_type", "VARCHAR(40) NULL"),
                    ("report_id",     "VARCHAR(40) NULL"),
                    ("archived_at",   "DATETIME NULL"),
                ]
                with _eng_33ir.begin() as _conn_33:
                    for _name, _ddl in _cols_33:
                        if _name not in existing_33:
                            _conn_33.execute(_sa_text_33(
                                f"ALTER TABLE manager_incident_report "
                                f"ADD COLUMN {_name} {_ddl}"))
                            logging.getLogger(__name__).info(
                                "manager_incident_report: backfilled %s (migration 33)",
                                _name)
                    _idx_33 = {i["name"]
                               for i in insp_33.get_indexes("manager_incident_report")}
                    if "ix_manager_incident_report_report_id" not in _idx_33:
                        try:
                            _conn_33.execute(_sa_text_33(
                                "CREATE INDEX ix_manager_incident_report_report_id "
                                "ON manager_incident_report(report_id)"))
                            logging.getLogger(__name__).info(
                                "manager_incident_report: created ix_report_id "
                                "(migration 33)")
                        except Exception:
                            pass  # index race; non-fatal
    except Exception:
        logging.getLogger(__name__).exception(
            "manager_incident_report v3 backfill failed (non-fatal)")

    # manager_incident_report incident_type widen (migration 35,
    # 2026-05-20 Sam #5:08 — incident-type grid is now multi-select so
    # the column stores a CSV like "injury,equipment,food-safety"; the
    # v3 VARCHAR(40) is too narrow for all 8 types combined). Idempotent:
    # check the current column length before issuing ALTER COLUMN. Only
    # runs on Postgres (Render); SQLite is lenient about string lengths
    # so the local dev DB doesn't need the alter.
    try:
        from sqlalchemy import inspect as _sa_insp_35, text as _sa_text_35
        from app.db import engine as _eng_35
        if _eng_35 is not None and _eng_35.dialect.name == "postgresql":
            insp_35 = _sa_insp_35(_eng_35)
            if "manager_incident_report" in set(insp_35.get_table_names()):
                for _c in insp_35.get_columns("manager_incident_report"):
                    if _c["name"] == "incident_type":
                        _len = getattr(_c["type"], "length", None) or 0
                        if _len and _len < 200:
                            with _eng_35.begin() as _conn_35:
                                _conn_35.execute(_sa_text_35(
                                    "ALTER TABLE manager_incident_report "
                                    "ALTER COLUMN incident_type TYPE VARCHAR(200)"))
                            logging.getLogger(__name__).info(
                                "manager_incident_report.incident_type widened "
                                "VARCHAR(%d -> 200) (migration 35)", _len)
                        break
    except Exception:
        logging.getLogger(__name__).exception(
            "manager_incident_report.incident_type widen failed (non-fatal)")

    # manager_incident_report v4 fields (migration 34, ck build-order
    # Sam dev chat #4:22 + #4:23 spec 2026-05-20 — rich "File new incident"
    # form with discrete what/when/where/who fields + lock-on-submit.
    # All columns nullable (date/time/text fields may be missing on
    # drafts) except 'locked' which defaults False.
    try:
        from sqlalchemy import inspect as _sa_insp_34, text as _sa_text_34
        from app.db import engine as _eng_34ir
        if _eng_34ir is not None:
            insp_34 = _sa_insp_34(_eng_34ir)
            if "manager_incident_report" in set(insp_34.get_table_names()):
                existing_34 = {c["name"]
                               for c in insp_34.get_columns("manager_incident_report")}
                _cols_34 = [
                    ("date_of_incident",  "DATE NULL"),
                    ("time_of_incident",  "TIME NULL"),
                    ("location_in_store", "VARCHAR(200) NULL"),
                    ("people_involved",   "TEXT NULL"),
                    ("witnesses",         "TEXT NULL"),
                    ("immediate_action",  "TEXT NULL"),
                    ("locked",            "BOOLEAN NOT NULL DEFAULT FALSE"),
                    ("locked_at",         "DATETIME NULL"),
                ]
                with _eng_34ir.begin() as _conn_34:
                    for _name, _ddl in _cols_34:
                        if _name not in existing_34:
                            _conn_34.execute(_sa_text_34(
                                f"ALTER TABLE manager_incident_report "
                                f"ADD COLUMN {_name} {_ddl}"))
                            logging.getLogger(__name__).info(
                                "manager_incident_report: backfilled %s (migration 34)",
                                _name)
    except Exception:
        logging.getLogger(__name__).exception(
            "manager_incident_report v4 backfill failed (non-fatal)")

    # Idempotent table create — sam_chat_todos (migration 37, Sam
    # directive 2026-05-23 #563 + re-raise "this is job number 2").
    # Sam's TODO list under /sam/chat: he writes items in, top item is
    # the current focus, Cena cannot skip. All fields Sam-filled (no
    # auto-default for date_added). create_all is a no-op when the
    # table already exists; scoped to just the SamChatTodo table so it
    # never touches unrelated metadata.
    try:
        from sqlalchemy import inspect as _sa_insp_37
        from app.db import engine as _eng_37
        from app.models import (Base as _Base_37,
                                SamChatTodo as _SCTD_37)
        if _eng_37 is not None:
            insp_37 = _sa_insp_37(_eng_37)
            if "sam_chat_todos" not in set(insp_37.get_table_names()):
                _Base_37.metadata.create_all(
                    bind=_eng_37, tables=[_SCTD_37.__table__])
                logging.getLogger(__name__).info(
                    "sam_chat_todos (migration 37): table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "sam_chat_todos backfill failed (non-fatal)")

    # Idempotent destructive teardown — DROP TABLE whatsapp_messages
    # (migration 27, Track 3 final teardown per cena #2257: Sam green-
    # light on all 4 Track 3 items). The WhatsApp ingest route + model
    # + ck-side runtime were already removed in 72c46a5 (samai PASS
    # #2117); the WhatsAppMessage class is gone from models.py. This
    # migration drops the legacy table itself so the schema matches
    # the live code. No-op when the table is already absent.
    try:
        from sqlalchemy import inspect as _sa_inspect_27, text as _sa_text_27
        from app.db import engine as _eng_27
        if _eng_27 is not None:
            insp_27 = _sa_inspect_27(_eng_27)
            if "whatsapp_messages" in insp_27.get_table_names():
                with _eng_27.begin() as _conn_27:
                    _conn_27.execute(_sa_text_27(
                        "DROP TABLE IF EXISTS whatsapp_messages"))
                logging.getLogger(__name__).info(
                    "whatsapp_messages (migration 27): table dropped "
                    "(Track 3 teardown)")
    except Exception:
        logging.getLogger(__name__).exception(
            "whatsapp_messages drop failed (non-fatal)")

    # Idempotent data-backfill: rewrite legacy Order.status='processed'
    # rows to 'available' (migration 24, Sam #1646 + samai #1645). The
    # ingest pipeline historically wrote 'processed' (an ezCater-job-
    # state marker predating the bid system). The new persist code
    # writes 'available' directly, but any rows ingested before this
    # boot still hold the legacy value and would be silently
    # unrequestable in /ez-market. Runs every boot and is a no-op once
    # the table is clean.
    try:
        from sqlalchemy import text as _sa_text_proc
        from app.db import engine as _eng_proc
        if _eng_proc is not None:
            with _eng_proc.begin() as _conn:
                result = _conn.execute(_sa_text_proc(
                    "UPDATE orders SET status='available' "
                    "WHERE status='processed'"
                ))
                if result.rowcount:
                    logging.getLogger(__name__).info(
                        "orders backfill (migration 24): %d processed -> available",
                        result.rowcount,
                    )
    except Exception:
        logging.getLogger(__name__).exception(
            "orders 'processed'->'available' backfill failed (non-fatal)")

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

    # Phase 1 / Block 5: register all anomaly rules. Module-level
    # @anomaly_rule decorators populate app.services.anomaly_engine.REGISTRY
    # at import time. Importing here means the engine + cron + admin
    # all see every rule on boot. Failure to import is non-fatal: the
    # engine still runs with whatever the import managed before the
    # raise (each rule registers independently).
    # IMPORTANT: must use `from app.services import ...` (or
    # importlib.import_module) here, NOT `import app.services.anomaly_rules`.
    # The bare-`import a.b.c` form creates a LOCAL binding for the name `a`
    # — inside this function that shadows the Flask instance assigned at
    # `app = Flask(__name__)` above, and `@app.cli.command(...)` two
    # lines below then raises:
    #   AttributeError: module 'app' has no attribute 'cli'
    # Hit twice already: 324dd2f (Block 5) + 44cc72b (Block 4 merge
    # reintroduced it). Leave the comment so the next agent doesn't
    # reintroduce a third time.
    try:
        from app.services import anomaly_rules as _anom_rules  # noqa: F401
        from app.services.anomaly_engine import REGISTRY as _ANOM_REG
        logging.getLogger(__name__).info(
            "anomaly_rules registered: %d rules", len(_ANOM_REG))
    except Exception:
        logging.getLogger(__name__).exception(
            "anomaly_rules import failed (non-fatal)")

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