import os
import logging
from flask import Flask, jsonify, render_template, request
import click
from dotenv import load_dotenv

from app.web.ezcater_routes import cater as ezc
from app.web.manager_routes import manager as mngr
from app.web.driver_routes import driver as drvr
from app.web.review_routes import review as rvw
from app.web.orders_browse import browse as obrowse
from app.web.ezcater_webhook import webhook as ezwh
from app.web.toast_webhook import toast_webhook_bp
from app.web.produce_order import produce_order as produce
from app.web.reports import reports as reports_bp
from app.web.store_routes import store_bp
from app.web import schedules_v2  # noqa: F401  B4: attaches schedule/shift routes to store_bp (must precede register_blueprint)
from app.web import schedules_v2_pages  # noqa: F401  B4: attaches the manager week-view PAGE route to store_bp (ck; must precede register_blueprint)
from app.web import schedules_v2_timeoff  # noqa: F401  B7: attaches the manager time-off review routes to store_bp (ckai; must precede register_blueprint)
from app.web import schedules_v2_availability  # noqa: F401  B8: attaches the manager availability view to store_bp (ckai; must precede register_blueprint)
from app.web import schedules_v2_market  # noqa: F401  B9: attaches the manager offer/swap approval routes to store_bp (ckai; must precede register_blueprint)
from app.web import schedules_v2_roster  # noqa: F401  email-pivot: attaches the manager roster-assignment write route (POST /<store>/schedules-v2/roster) to store_bp (ckai; must precede register_blueprint)
from app.web import toast_link_routes  # noqa: F401  Link tab: attaches the manager Toast match-suggestions + per-employee labor/perf routes (GET /<store>/schedules-v2/toast/*) to store_bp (ckbro; must precede register_blueprint)
from app.web.developer_chat import dev_chat as dev_chat_bp
from app.web.permissions_admin import permissions_admin as perms_admin_bp  # PERMISSIONS admin page (partner-only, Sam #1676)
from app.web.interview import interview as interview_bp
from app.web.corporate_order import corp_order as corp_order_bp
from app.web.ezcater_import_routes import ezc_import as ezc_import_bp
from app.web.ezcater_live_routes import ezc_live as ezc_live_bp
from app.web.ezcater_tracking_watch_routes import ezcater_tracking_watch_bp
from app.web.assistant_routes import assistant_bp
from app.web.team_routes import team_bp
from app.web.legal_routes import legal as legal_bp
from app.web.access_request_routes import access_req as access_req_bp
from app.web.driver_system import driver_system_bp
from app.web.perf_roster_link import perf_roster_link_bp
from app.web.scheduling_cron import scheduling_cron_bp  # B6: shift-alarm send cron (ckai)
from app.web.briefs import briefs_bp
from app.web.tasks import tasks_bp
from app.web.team_reports import team_reports_bp
from app.web.floor_routes import floor_bp  # Sections/Floor: map setup + section assignment + host seating + reservations (ck Gate 2; self-contained /floor blueprint, see docs/floor_contract.md)
from app.web.worldcup import worldcup_bp  # PUBLIC no-login /worldcup page (World Cup only); exempted in auth.py EXEMPT_PREFIXES
from app.web import auth as ezauth
from app.web import keypad_auth as ezkeypad
from app.web import employee_auth as ezempauth
from app.web import employee_schedule_page  # noqa: F401  B5: attaches GET /employee/my-schedule to the employee_auth blueprint (ck; must import before ezempauth.install)
from app.web import schedules_v2_employee  # noqa: F401  B5: attaches the employee schedule DATA + accept/decline endpoints to the employee_auth blueprint (aick; must import before ezempauth.install)
from app.web import employee_alarm_prefs  # noqa: F401  B6: attaches GET/POST /employee/alarm-preferences to the employee_auth blueprint (ckai; must import before ezempauth.install)
from app.web import employee_profile_page  # noqa: F401  B6: attaches GET /employee/profile (alarm-preferences UI) to the employee_auth blueprint (ck; must import before ezempauth.install)
from app.web import employee_my_profile_page  # noqa: F401  Staff profile hub: attaches GET /employee/my-profile to employee_auth blueprint
from app.web import employee_tables_page  # noqa: F401  Employee own Toast table/check timelines
from app.web import employee_sports_page  # noqa: F401  Sports tab: GET /employee/sports + /data.json on the employee_auth blueprint (must import before ezempauth.install)
from app.web.corporate_profile_lab import profile_lab_bp
from app.web.employee_messages import employee_messages_bp  # Employee-to-employee messaging blueprint (standalone; registered below)
from app.web import employee_time_off  # noqa: F401  B7: attaches the employee time-off endpoints to the employee_auth blueprint (ckai; must import before ezempauth.install)
from app.web import employee_time_off_page  # noqa: F401  B7: attaches GET /employee/time-off (time-off request UI) to the employee_auth blueprint (ck; must import before ezempauth.install)
from app.web import employee_availability  # noqa: F401  B8: attaches the employee availability endpoints to the employee_auth blueprint (ckai; must import before ezempauth.install)
from app.web import employee_availability_page  # noqa: F401  B8: attaches GET /employee/availability (availability editor) to the employee_auth blueprint (ck; must import before ezempauth.install)
from app.web import employee_shift_market  # noqa: F401  B9: attaches the employee offer/swap/marketplace endpoints to the employee_auth blueprint (ckai; must import before ezempauth.install)
from app.web import employee_shift_marketplace_page  # noqa: F401  B9: attaches GET /employee/shift-marketplace to the employee_auth blueprint (ck; must import before ezempauth.install)
from app.web import employee_setup  # noqa: F401  email-pivot: passcode login + email self-setup endpoints on the employee_auth blueprint (ckai; must import before ezempauth.install)
from app.web import employee_setup_page  # noqa: F401  B11: attaches GET /employee/setup/<token> (employee self-setup page) to the employee_auth blueprint (ck; must import before ezempauth.install). ckai's /employee/setup/<token>/info + /complete are in employee_setup.py (separate import); the auth EXEMPT /employee/setup (ckai's) covers this page too.
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
    app.config["RENDER_GIT_COMMIT"] = (
        os.getenv("RENDER_GIT_COMMIT")
        or os.getenv("GIT_COMMIT")
        or "local"
    )

    app.register_blueprint(ezc)
    app.register_blueprint(mngr)
    app.register_blueprint(drvr)
    app.register_blueprint(rvw)
    app.register_blueprint(obrowse)
    app.register_blueprint(ezwh)
    app.register_blueprint(toast_webhook_bp)
    app.register_blueprint(produce)
    app.register_blueprint(reports_bp)
    app.register_blueprint(store_bp)
    app.register_blueprint(floor_bp)  # Sections/Floor pages + /floor/api/* (ck Gate 2)
    app.register_blueprint(worldcup_bp)  # PUBLIC /worldcup (no login) — World Cup board
    app.register_blueprint(dev_chat_bp)
    app.register_blueprint(perms_admin_bp)  # PERMISSIONS admin page (partner-only, Sam #1676)
    # Interview Tracker (Sam #5:48) — partner-only candidate hiring
    # pipeline. Routes registered at /partner/interview-tracker
    # directly (not under store_bp), so not auto-partner-gated; each
    # route re-checks the session flag via _enforce_partner(). See
    # app/web/interview.py.
    app.register_blueprint(interview_bp)
    app.register_blueprint(ezc_import_bp)
    app.register_blueprint(ezc_live_bp)
    app.register_blueprint(ezcater_tracking_watch_bp)
    app.register_blueprint(assistant_bp)
    app.register_blueprint(team_bp)
    app.register_blueprint(profile_lab_bp)
    app.register_blueprint(employee_messages_bp)
    app.register_blueprint(legal_bp)
    app.register_blueprint(access_req_bp)
    app.register_blueprint(driver_system_bp)
    app.register_blueprint(perf_roster_link_bp)
    # Isolated employee-perf-push receiver (Sam #3178: decoupled from driver/catering;
    # imports only app.db/app.models, never driver_system). Legacy /cron/perf-push in
    # driver_system.py stays untouched + unused.
    from app.web.perf_push_routes import perf_push_bp
    app.register_blueprint(perf_push_bp)
    # Isolated READ-ONLY data-mart export endpoint (Sam #3330 / aick #3315/#3334):
    # serves CK's mart per-employee PROFILE + SCHEDULE only; imports only app.db/app.models,
    # never driver_system; fail-closed DATAMART_EXPORT_TOKEN; SELECT-only (no writes).
    from app.web.datamart_export_routes import datamart_export_bp
    app.register_blueprint(datamart_export_bp)
    # Isolated READ-ONLY driver + ezCater-orders data-center export (Sam #3592/#3610;
    # aick #3609 contract, frozen CK #3612): serves CK's R1-minimized driver/orders marts;
    # imports only app.db/app.models, never driver_system; fail-closed DRIVERDC_EXPORT_TOKEN;
    # SELECT-only; customer_hash + gps-summary computed APP-SIDE (raw GPS/cleartext NEVER served).
    from app.web.driverdc_export_routes import driverdc_export_bp
    app.register_blueprint(driverdc_export_bp)
    # Isolated READ-ONLY full app-DB snapshot export (Sam 2026-06-10, appdb
    # live-mirror lane): one gzipped VACUUM-INTO sqlite copy, credential/PII
    # scrubbed server-side before the bytes leave the box; fail-closed
    # APPDB_EXPORT_TOKEN (falls back to CENA_GATEWAY_TOKEN); imports only
    # app.db, never models/driver_system. Feeds CK's CENA_L3_SRC_APPDB so
    # Cena's L3 reasons over LIVE app data instead of dev_local.db.
    from app.web.appdb_export_routes import appdb_export_bp
    app.register_blueprint(appdb_export_bp)
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
    if os.getenv("DISABLE_DOCCK_TICKER") != "1":
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
    from app.web.dashboard_access import has_dashboard_access as _has_dashboard_access
    app.jinja_env.globals["has_dashboard_access"] = _has_dashboard_access
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

    def _wants_json_error_response() -> bool:
        if request.path.endswith(".json") or request.blueprint in {
            "assistant",
            "toast_webhook",
            "ezcater_tracking_watch",
        }:
            return True
        best = request.accept_mimetypes.best_match(["application/json", "text/html"])
        return best == "application/json" and (
            request.accept_mimetypes["application/json"]
            >= request.accept_mimetypes["text/html"]
        )

    def _render_update_error(error, status_code: int):
        if _wants_json_error_response():
            return jsonify({
                "ok": False,
                "error": "temporary_update",
                "message": "Sam is making an update. Please try again shortly.",
            }), status_code
        return render_template("update_error.html", status_code=status_code), status_code

    @app.errorhandler(500)
    def _friendly_500(error):
        return _render_update_error(error, 500)

    @app.errorhandler(502)
    def _friendly_502(error):
        return _render_update_error(error, 502)

    @app.errorhandler(503)
    def _friendly_503(error):
        return _render_update_error(error, 503)

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

    # Idempotent column backfill for the employees table (email-pivot 2026-05-30,
    # "B11"): the passcode-auth columns added to the Employee model. create_all()
    # does NOT ALTER the already-populated employees table, so add them here, each
    # gated on column absence (safe to run every boot). All nullable or
    # NOT NULL DEFAULT 0 -> a safe SQLite ADD COLUMN. The new employee_setup_tokens
    # table is a NEW table, so Base.metadata.create_all already creates it.
    try:
        from sqlalchemy import inspect as _sa_inspect_emp, text as _sa_text_emp
        from app.db import engine as _eng_emp
        if _eng_emp is not None:
            insp = _sa_inspect_emp(_eng_emp)
            if "employees" in insp.get_table_names():
                existing = {c["name"] for c in insp.get_columns("employees")}
                additions = [
                    ("passcode_hash",   "VARCHAR(255)"),
                    ("failed_attempts", "INTEGER NOT NULL DEFAULT 0"),
                    ("lockout_until",   "TIMESTAMP"),
                    ("session_version", "INTEGER NOT NULL DEFAULT 0"),
                    # Unify LINK (Sam #2261, 2026-05-31): nullable FK -> users.id.
                    # Raw ADD COLUMN can't carry the FK constraint on SQLite, but
                    # the ORM ForeignKey declaration handles the Employee->User join;
                    # the column itself is all the storage we need.
                    ("user_id",         "INTEGER"),
                    # Roster edit-contact (2026-05-31, roster-edit branch): nullable
                    # free-text mailing address, manager-editable from the Team roster.
                    ("address",         "VARCHAR(255)"),
                ]
                added = []
                with _eng_emp.begin() as conn:
                    for col_name, col_def in additions:
                        if col_name not in existing:
                            conn.execute(_sa_text_emp(f"ALTER TABLE employees ADD COLUMN {col_name} {col_def}"))
                            added.append(col_name)
                if added:
                    logging.getLogger(__name__).info(
                        "employees table: backfilled missing columns %s", added)
    except Exception:
        logging.getLogger(__name__).exception("employees column backfill failed (non-fatal)")

    # Dual-channel PIN reset (Sam 2026-06-07): add employee_setup_tokens.code_hash
    # + code_attempts so a reset can ALSO mint a short manager-displayed code on the
    # SAME single-use token. create_all() won't ALTER the already-populated
    # employee_setup_tokens table, so add them here, each gated on column absence
    # (safe + idempotent on every boot). code_hash is nullable (old link-only rows
    # stay valid); code_attempts is NOT NULL DEFAULT 0 -> a safe SQLite ADD COLUMN.
    try:
        from sqlalchemy import inspect as _sa_inspect_sc, text as _sa_text_sc
        from app.db import engine as _eng_sc
        if _eng_sc is not None:
            insp = _sa_inspect_sc(_eng_sc)
            if "employee_setup_tokens" in insp.get_table_names():
                _sccols = {c["name"] for c in insp.get_columns("employee_setup_tokens")}
                _scadd = []
                with _eng_sc.begin() as conn:
                    for col_name, col_def in (
                        ("code_hash",     "VARCHAR(64)"),
                        ("code_attempts", "INTEGER NOT NULL DEFAULT 0"),
                    ):
                        if col_name not in _sccols:
                            conn.execute(_sa_text_sc(
                                f"ALTER TABLE employee_setup_tokens ADD COLUMN {col_name} {col_def}"))
                            _scadd.append(col_name)
                if _scadd:
                    logging.getLogger(__name__).info(
                        "employee_setup_tokens table: backfilled missing columns %s", _scadd)
    except Exception:
        logging.getLogger(__name__).exception("employee_setup_tokens column backfill failed (non-fatal)")

    # Shift-market money offers (Sam 2026-06-13): add shift_offers.incentive_cents
    # (NULL = no cash attached). create_all() won't ALTER the existing shift_offers
    # table, so add it here, gated on absence -- safe + idempotent on every boot.
    try:
        from sqlalchemy import inspect as _sa_inspect_so, text as _sa_text_so
        from app.db import engine as _eng_so
        if _eng_so is not None:
            insp = _sa_inspect_so(_eng_so)
            if "shift_offers" in insp.get_table_names():
                _socols = {c["name"] for c in insp.get_columns("shift_offers")}
                if "incentive_cents" not in _socols:
                    with _eng_so.begin() as conn:
                        conn.execute(_sa_text_so(
                            "ALTER TABLE shift_offers ADD COLUMN incentive_cents INTEGER"))
                    logging.getLogger(__name__).info(
                        "shift_offers table: backfilled missing column incentive_cents")
    except Exception:
        logging.getLogger(__name__).exception("shift_offers column backfill failed (non-fatal)")

    # Sam #2872: add shifts.display_name (historical import shows a former, no-record
    # employee's name struck-through). create_all() won't ALTER the existing shifts
    # table, so add it here, gated on absence -- safe + idempotent on every boot.
    try:
        from sqlalchemy import inspect as _sa_inspect_sh, text as _sa_text_sh
        from app.db import engine as _eng_sh
        if _eng_sh is not None:
            insp = _sa_inspect_sh(_eng_sh)
            if "shifts" in insp.get_table_names():
                cols = {c["name"] for c in insp.get_columns("shifts")}
                if "display_name" not in cols:
                    with _eng_sh.begin() as conn:
                        conn.execute(_sa_text_sh("ALTER TABLE shifts ADD COLUMN display_name VARCHAR(120)"))
                    logging.getLogger(__name__).info("shifts table: added display_name column")
    except Exception:
        logging.getLogger(__name__).exception("shifts.display_name backfill failed (non-fatal)")

    # Sam (Sling-parity): per-shift publish state (shifts.published_at). NULL = unpublished
    # (hollow + hidden from the employee); set = published. Add the column, then BACKFILL
    # existing shifts in PUBLISHED schedules so already-published weeks stay visible to
    # employees (published_at := the schedule's published_at, else its updated_at). Additive,
    # one-time (gated on column absence), and never touches draft/unpublished shifts.
    try:
        from sqlalchemy import inspect as _sa_insp_pub, text as _sa_text_pub
        from app.db import engine as _eng_pub
        if _eng_pub is not None:
            _insp_pub = _sa_insp_pub(_eng_pub)
            if "shifts" in _insp_pub.get_table_names():
                _shcols = {c["name"] for c in _insp_pub.get_columns("shifts")}
                if "published_at" not in _shcols:
                    # 1) add the column in its OWN txn so the model<->table match is
                    #    guaranteed even if the backfill below hiccups.
                    with _eng_pub.begin() as conn:
                        conn.execute(_sa_text_pub("ALTER TABLE shifts ADD COLUMN published_at TIMESTAMP"))
                    logging.getLogger(__name__).info("shifts table: added published_at column")
                    # 2) backfill existing published-week shifts in a SEPARATE txn (prep
                    #    for the per-shift employee filter); a failure leaves the column
                    #    intact + retryable -- never a model/table mismatch.
                    try:
                        with _eng_pub.begin() as conn:
                            conn.execute(_sa_text_pub(
                                "UPDATE shifts SET published_at = COALESCE("
                                "(SELECT s.published_at FROM schedules s WHERE s.id = shifts.schedule_id), "
                                "(SELECT s.updated_at FROM schedules s WHERE s.id = shifts.schedule_id)) "
                                "WHERE published_at IS NULL AND schedule_id IN "
                                "(SELECT id FROM schedules WHERE status = 'published')"))
                        logging.getLogger(__name__).info("shifts table: backfilled published_at for published-week shifts")
                    except Exception:
                        logging.getLogger(__name__).exception("shifts.published_at backfill failed (non-fatal; column intact)")
    except Exception:
        logging.getLogger(__name__).exception("shifts.published_at add/backfill failed (non-fatal)")

    # Phase 3.5 hardening (Sam #2973, N4/N5): add per-shift markers to perf_shift_cache.
    # create_all won't ALTER the existing table, so add gated-on-absence (idempotent).
    try:
        from sqlalchemy import inspect as _sa_insp_ps, text as _sa_text_ps
        from app.db import engine as _eng_ps
        if _eng_ps is not None:
            _insp_ps = _sa_insp_ps(_eng_ps)
            if "perf_shift_cache" in _insp_ps.get_table_names():
                _pscols = {c["name"] for c in _insp_ps.get_columns("perf_shift_cache")}
                for _cn, _cdef in (("tips_declared", "INTEGER DEFAULT 1"),
                                   ("needs_review", "INTEGER DEFAULT 0"),
                                   ("review_reason", "VARCHAR(120)")):
                    if _cn not in _pscols:
                        with _eng_ps.begin() as conn:
                            conn.execute(_sa_text_ps("ALTER TABLE perf_shift_cache ADD COLUMN %s %s" % (_cn, _cdef)))
                        logging.getLogger(__name__).info("perf_shift_cache: added %s column", _cn)
    except Exception:
        logging.getLogger(__name__).exception("perf_shift_cache markers backfill failed (non-fatal)")

    # Idempotent seed of the canonical schedule positions (Sam 2026-05-31): the
    # jobs that must appear in the manager schedule dropdowns. The management
    # roles (Partner, Corporate, GM, KM, ...) are User
    # permission levels, not Sling-imported Position rows, so they may be absent;
    # create any missing canonical name as an all-store row (store_key=NULL).
    # Runs at single-threaded startup (no worker race) and is gated on name
    # absence, so it is safe + idempotent on every boot. NON-DESTRUCTIVE: only
    # inserts missing names; never edits/deletes a Position row (the board READ
    # is filtered to CANONICAL_POSITIONS).
    try:
        from sqlalchemy import inspect as _sa_inspect_pos
        from app.db import SessionLocal as _SL_pos, engine as _eng_pos
        if _eng_pos is not None and "positions" in _sa_inspect_pos(_eng_pos).get_table_names():
            from app.models import CANONICAL_POSITIONS as _CANON_POS, Position as _Position
            _db_pos = _SL_pos()
            try:
                _have = {(p.name or "").strip().lower() for p in _db_pos.query(_Position).all()}
                _seeded = []
                for _disp in _CANON_POS:
                    if _disp.lower() not in _have:
                        _db_pos.add(_Position(name=_disp, store_key=None))
                        _seeded.append(_disp)
                if _seeded:
                    _db_pos.commit()
                    logging.getLogger(__name__).info(
                        "positions table: seeded canonical jobs %s", _seeded)
            finally:
                _db_pos.close()
    except Exception:
        logging.getLogger(__name__).exception("canonical positions seed failed (non-fatal)")

    # Unify backfill (Sam #2261, 2026-05-31): bring managers/partners into the ONE
    # team list (Team+Schedule combine). For each ACTIVE User, LINK it to an
    # Employee matched by email (case-insensitive) - or CREATE + link an Employee
    # if none exists (a "pure manager" who was never a scheduling employee;
    # created phone=NULL to dodge the Employee.phone UNIQUE, since their contact
    # lives on the linked User). The link (Employee.user_id) is purely additive:
    # the User row + its keypad auth are UNTOUCHED. Idempotent - skips a User
    # already linked (User.email is UNIQUE so each email-match is 1:1). Gated on
    # the user_id column existing; single-threaded at boot (ckai seam point 2, #2295).
    try:
        from sqlalchemy import inspect as _sa_inspect_lnk
        from app.db import SessionLocal as _SL_lnk, engine as _eng_lnk
        if _eng_lnk is not None:
            _insp_lnk = _sa_inspect_lnk(_eng_lnk)
            _tabs_lnk = _insp_lnk.get_table_names()
            _ecols = ({c["name"] for c in _insp_lnk.get_columns("employees")}
                      if "employees" in _tabs_lnk else set())
            if "user_id" in _ecols and "users" in _tabs_lnk:
                from app.services.team_roster import backfill_user_links as _bfl
                _db_lnk = _SL_lnk()
                try:
                    _linked, _created = _bfl(_db_lnk)
                    if _linked or _created:
                        logging.getLogger(__name__).info(
                            "unify link: linked %d + created %d employee(s) from users",
                            _linked, _created)
                finally:
                    _db_lnk.close()
    except Exception:
        logging.getLogger(__name__).exception("unify user-link backfill failed (non-fatal)")

    # Per-store POSITIONS migration + backfills (Sam #2457, permissions rework
    # 2026-05-31). EmployeePosition is now PER-STORE (store_key). create_all does
    # NOT alter the populated employee_positions table, so we must swap the unique
    # (uq_emp_position 2-col -> uq_emp_position_store 3-col) DIALECT-AWARE and BEFORE
    # the backfills run (hole-1, #2457): SQLite cannot ALTER DROP/ADD CONSTRAINT, so
    # it gets an atomic TABLE REBUILD; Postgres uses ADD COLUMN + ALTER CONSTRAINT.
    # The 3-col uq must be in place first or the backfills collide on the surviving
    # 2-col uq (expanding a legacy global row, or a both-store manager). Then run
    # BOTH backfills - manager-positions FIRST (linked managers get their mgmt
    # position so enforcement finds their perms) then expand legacy global rows -
    # BEFORE enforcement reads positions (ckai #2488 hole-2 ordering; runs after the
    # unify link above, which it depends on for managers + their store assignments).
    # Idempotent: the sqlite rebuild is skipped when the new 3-col uq already exists
    # (fresh db via create_all, or a re-run); the backfills are no-ops on re-run.
    try:
        from sqlalchemy import inspect as _sa_insp_ep, text as _sa_text_ep
        from app.db import SessionLocal as _SL_ep, engine as _eng_ep
        if _eng_ep is not None and "employee_positions" in _sa_insp_ep(_eng_ep).get_table_names():
            _insp_ep = _sa_insp_ep(_eng_ep)
            _epcols = {c["name"] for c in _insp_ep.get_columns("employee_positions")}
            # Detect the 3-col uq BY COLUMN SET, not by name: a prod uq created
            # without an explicit name (auto-named sqlite_autoindex_*) would defeat a
            # name-based check and silently SKIP the rebuild -> the inert bug would
            # persist. Column-set detection is name-independent + bulletproof.
            _epuq_colsets = [frozenset(u.get("column_names") or [])
                             for u in _insp_ep.get_unique_constraints("employee_positions")]
            _has_3col_uq = frozenset(("employee_id", "position_id", "store_key")) in _epuq_colsets

            if _eng_ep.dialect.name == "sqlite":
                # SQLite cannot ALTER TABLE DROP/ADD CONSTRAINT, so the only way to
                # swap uq_emp_position(2-col) -> uq_emp_position_store(3-col) is a
                # TABLE REBUILD. The 3-col uq MUST exist before the backfills run or
                # they collide on the surviving 2-col uq (hole-1, #2457). Idempotent:
                # skip when the 3-col uq already exists (fresh db built by create_all,
                # or an already-migrated db). Atomic: the whole rebuild is ONE
                # transaction, so a mid-rebuild failure loses no data.
                if _has_3col_uq:
                    pass  # already migrated: 3-col uq present (fresh db, or a re-run)
                else:
                    _has_store = "store_key" in _epcols
                    _sel_store = "store_key" if _has_store else "NULL"
                    # FK enforcement is OFF by default on this sqlite engine (no
                    # PRAGMA foreign_keys=ON listener) and nothing references
                    # employee_positions, but force it OFF on THIS connection
                    # (before BEGIN; the pragma is a no-op inside a txn) so the
                    # DROP/RENAME can never trip a referencing-FK check, then begin
                    # the atomic rebuild.
                    _raw = _eng_ep.raw_connection()
                    try:
                        _cur0 = _raw.cursor()
                        _cur0.execute("PRAGMA foreign_keys=OFF")
                        _cur0.close()
                        _cur = _raw.cursor()
                        _cur.execute("BEGIN")
                        try:
                            _cur.execute(
                                "CREATE TABLE employee_positions_new ("
                                "id INTEGER NOT NULL PRIMARY KEY, "
                                "employee_id INTEGER NOT NULL, "
                                "position_id INTEGER NOT NULL, "
                                "store_key VARCHAR(40), "
                                "created_at DATETIME NOT NULL, "
                                "CONSTRAINT uq_emp_position_store UNIQUE (employee_id, position_id, store_key), "
                                "FOREIGN KEY(employee_id) REFERENCES employees (id) ON DELETE CASCADE, "
                                "FOREIGN KEY(position_id) REFERENCES positions (id) ON DELETE CASCADE)")
                            _cur.execute(
                                "INSERT INTO employee_positions_new "
                                "(id, employee_id, position_id, store_key, created_at) "
                                "SELECT id, employee_id, position_id, " + _sel_store + ", created_at "
                                "FROM employee_positions")
                            _cur.execute("DROP TABLE employee_positions")
                            _cur.execute("ALTER TABLE employee_positions_new RENAME TO employee_positions")
                            _cur.execute("CREATE INDEX ix_employee_positions_employee_id "
                                         "ON employee_positions (employee_id)")
                            _cur.execute("CREATE INDEX ix_employee_positions_position_id "
                                         "ON employee_positions (position_id)")
                            _cur.execute("CREATE INDEX ix_employee_positions_store_key "
                                         "ON employee_positions (store_key)")
                            _raw.commit()
                            logging.getLogger(__name__).info(
                                "per-store positions: sqlite table rebuild swapped "
                                "uq_emp_position -> uq_emp_position_store")
                        except Exception:
                            _raw.rollback()
                            raise
                        finally:
                            _cur.close()
                    finally:
                        try:
                            _cur1 = _raw.cursor()
                            _cur1.execute("PRAGMA foreign_keys=ON")
                            _cur1.close()
                        except Exception:
                            pass
                        _raw.close()
            else:
                # Non-sqlite (Postgres): ADD COLUMN + ALTER DROP/ADD CONSTRAINT work
                # natively. Each is its own txn, defensive (constraint absent/exists
                # is fine), so a no-op or re-run can't poison the others.
                def _try_alter_ep(_stmt):
                    try:
                        with _eng_ep.begin() as _c:
                            _c.execute(_sa_text_ep(_stmt))
                    except Exception:
                        pass  # constraint absent/exists / unsupported on this DB - fine
                if "store_key" not in _epcols:
                    _try_alter_ep("ALTER TABLE employee_positions ADD COLUMN store_key VARCHAR(40)")
                _try_alter_ep("ALTER TABLE employee_positions DROP CONSTRAINT uq_emp_position")
                _try_alter_ep("ALTER TABLE employee_positions ADD CONSTRAINT uq_emp_position_store "
                              "UNIQUE (employee_id, position_id, store_key)")
            from app.services.team_roster import (
                backfill_manager_positions as _bmp,
                backfill_employee_position_stores as _bps)
            _db_ep = _SL_ep()
            try:
                _assigned = _bmp(_db_ep)         # layer-1: managers get their position
                _exp, _rem = _bps(_db_ep)         # expand legacy global rows -> per-store
                if _assigned or _exp or _rem:
                    logging.getLogger(__name__).info(
                        "per-store positions: %d mgr-position(s) assigned, %d expanded, %d global replaced",
                        _assigned, _exp, _rem)
            finally:
                _db_ep.close()
    except Exception:
        logging.getLogger(__name__).exception("per-store positions migration/backfill failed (non-fatal)")

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
                ("parking_photo_url",       "VARCHAR(500)"),
                ("parking_photo_uploaded_at","TIMESTAMP"),
                ("parking_cost",            "FLOAT"),
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
            driver_location_additions = [
                ("order_id",                "INTEGER"),
            ]
            added_orders, added_drivers, added_driver_locations = [], [], []
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
                if "driver_location" in insp.get_table_names():
                    existing = {c["name"] for c in insp.get_columns("driver_location")}
                    for col_name, col_def in driver_location_additions:
                        if col_name not in existing:
                            conn.execute(_sa_text_15(f"ALTER TABLE driver_location ADD COLUMN {col_name} {col_def}"))
                            added_driver_locations.append(col_name)
            if added_orders:
                logging.getLogger(__name__).info("orders table (migration 15): backfilled %s", added_orders)
            if added_drivers:
                logging.getLogger(__name__).info("drivers table (migration 15): backfilled %s", added_drivers)
            if added_driver_locations:
                logging.getLogger(__name__).info("driver_location table (migration 15): backfilled %s", added_driver_locations)
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

    # Heal vendor_recent_orders schema (Sam #2762): samai's d2e6416 (full email detail) +
    # b94da15 (order_number) ADDED columns to the model, but the boot path above only CREATEs
    # the table -- an EXISTING prod DB still has the OLD column set, so every Vendors tab 500s
    # on the SELECT ('no such column'). ADD any model columns the live table is missing
    # (SQLite-safe ALTER ADD COLUMN, nullable; idempotent -- only adds what's absent).
    try:
        from sqlalchemy import inspect as _sa_insp_vh
        from app.db import engine as _eng_vh
        from app.models import VendorRecentOrder as _VRO_vh
        if _eng_vh is not None:
            _insp_vh = _sa_insp_vh(_eng_vh)
            if "vendor_recent_orders" in set(_insp_vh.get_table_names()):
                _have_vh = {c["name"] for c in _insp_vh.get_columns("vendor_recent_orders")}
                _missing_vh = [c for c in _VRO_vh.__table__.columns if c.name not in _have_vh]
                if _missing_vh:
                    with _eng_vh.begin() as _cx_vh:
                        for _col_vh in _missing_vh:
                            _ty_vh = str(_col_vh.type).split("(")[0].upper()
                            if _ty_vh not in ("INTEGER", "VARCHAR", "TEXT", "DATETIME",
                                              "BOOLEAN", "FLOAT", "NUMERIC", "BIGINT"):
                                _ty_vh = "TEXT"
                            _cx_vh.exec_driver_sql(
                                'ALTER TABLE vendor_recent_orders ADD COLUMN "%s" %s'
                                % (_col_vh.name, _ty_vh))
                    logging.getLogger(__name__).info(
                        "vendor_recent_orders: healed missing columns %s",
                        [c.name for c in _missing_vh])
    except Exception:
        logging.getLogger(__name__).exception(
            "vendor_recent_orders column-heal failed (non-fatal)")

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
        from sqlalchemy import text as _sa_text_pl
        from app.db import engine as _eng_pl, SessionLocal as _SL_pl
        from app.models import (
            Base as _Base_pl, PrepItem as _PI_pl, PrepEntry as _PE_pl,
            PrepAuditLog as _PAL_pl,
        )
        if _eng_pl is not None:
            insp_pl = _sa_insp_pl(_eng_pl)
            existing = set(insp_pl.get_table_names())
            to_create = [m.__table__ for m in (_PI_pl, _PE_pl, _PAL_pl)
                         if m.__tablename__ not in existing]
            if to_create:
                _Base_pl.metadata.create_all(bind=_eng_pl, tables=to_create)
                logging.getLogger(__name__).info(
                    "prep list v3: created %d tables (%s)",
                    len(to_create), [t.name for t in to_create])
            if "kitchen_prep_entry" in set(insp_pl.get_table_names()):
                _entry_cols_pl = {
                    c["name"] for c in insp_pl.get_columns("kitchen_prep_entry")
                }
                _entry_additions_pl = [
                    ("prep_qty", "INTEGER"),
                    ("helper_names", "TEXT"),
                    ("completed_by_name", "VARCHAR(120)"),
                    ("completed_at", "TIMESTAMP"),
                ]
                _added_entry_cols_pl = []
                with _eng_pl.begin() as conn:
                    for _col_pl, _ddl_pl in _entry_additions_pl:
                        if _col_pl not in _entry_cols_pl:
                            conn.execute(_sa_text_pl(
                                f"ALTER TABLE kitchen_prep_entry ADD COLUMN {_col_pl} {_ddl_pl}"))
                            _added_entry_cols_pl.append(_col_pl)
                if _added_entry_cols_pl:
                    logging.getLogger(__name__).info(
                        "prep list v3: backfilled entry columns %s",
                        _added_entry_cols_pl)
            _prep_seed = [
                ("hot", "item", ["Masa Flour", "Charros", "Refried",
                    "Black Bean", "Costillas", "Cochina", "Taco Meat",
                    "Pollo Ranchero", "Chicken Stock", "Mexican Butter",
                    "Vegetales", "Charro Mix", "Spinach Mix", "Empanadas"]),
                ("hot", "sauce", ["Seafood Sauce", "Tomatillo Mix",
                    "Tomatillo Sauce", "Ranchera Sauce", "Poblano Sauce",
                    "Street Taco Sauce", "BBQ Sauce", "Chile Con Queso",
                    "Chile Gravy", "Chips"]),
                ("cold", "item", ["Salad Mix", "Shredded Lettuce",
                    "Cabbage Mix", "Pickled Onions", "Salad Shrimp"]),
                ("cold", "sauce", ["Roja", "Verde", "Ranch",
                    "Avocado Ranch", "Honey Mustard",
                    "Beef Fajita Marination", "Chipotle Mayo",
                    "Chipotle Cream", "Cilantro Ginger"]),
                ("chop", "item", ["Cebolla de Parrilla", "Cebolla Pelado",
                    "Onions Chop", "Bell Pepper", "Enchilada Cheese",
                    "Queso Fresco", "Poblano", "Mango", "Jalapenos"]),
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
                else:
                    _existing_keys_pl = {
                        (
                            (_row.name or "").strip().lower(),
                            (_row.category or "").strip().lower(),
                            (_row.kind or "").strip().lower(),
                            _row.store_scope,
                        )
                        for _row in _db_pl.query(_PI_pl).all()
                    }
                    _max_so_pl = max(
                        [
                            _row.sort_order or 0
                            for _row in _db_pl.query(_PI_pl.sort_order).all()
                        ] or [0])
                    _inserted_pl = 0
                    for _cat_pl, _kind_pl, _names_pl in _prep_seed:
                        for _nm_pl in _names_pl:
                            _key_pl = (
                                _nm_pl.strip().lower(), _cat_pl, _kind_pl, None)
                            if _key_pl in _existing_keys_pl:
                                continue
                            _max_so_pl += 1
                            _db_pl.add(_PI_pl(
                                name=_nm_pl, category=_cat_pl, kind=_kind_pl,
                                sort_order=_max_so_pl))
                            _existing_keys_pl.add(_key_pl)
                            _inserted_pl += 1
                    if _inserted_pl:
                        _db_pl.commit()
                        logging.getLogger(__name__).info(
                            "prep list v3: inserted %d missing master items",
                            _inserted_pl)
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
                    # samai (Sam Kitchen-dashboard batch): ES counterparts
                    # for the recipe-card EN/ES toggle. Additive, idempotent.
                    ("prep_time_es",         "VARCHAR(80) NULL"),
                    ("shelf_life_es",        "VARCHAR(80) NULL"),
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
        _c, _s, _e = seed_recipes_from_json(skip_if_populated=True)
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

    # Idempotent table create -- cena_toast_link (Cena<->Toast Link tab,
    # Sam #2629). Persists a CONFIRMED Cena-employee <-> Toast-employee match
    # per store (UNIQUE(cena_employee_id, store_key)) so the Link tab shows
    # verified links + can later load that person's Toast data. Render runs no
    # alembic -- this boot create is the real schema apply. create_all scoped to
    # just this table is a no-op once present; SQLite-safe on a fresh AND an
    # existing DB (the UniqueConstraint becomes the table-level UNIQUE).
    try:
        from sqlalchemy import inspect as _sa_insp_ctl
        from app.db import engine as _eng_ctl
        from app.models import Base as _Base_ctl, CenaToastLink as _CTL
        if _eng_ctl is not None:
            if "cena_toast_link" not in set(_sa_insp_ctl(_eng_ctl).get_table_names()):
                _Base_ctl.metadata.create_all(
                    bind=_eng_ctl, tables=[_CTL.__table__])
                logging.getLogger(__name__).info(
                    "cena_toast_link (Sam #2629): table created")
    except Exception:
        logging.getLogger(__name__).exception(
            "cena_toast_link table create failed (non-fatal)")

    # Start the IMAP poller for produce vendor pricing. No-op unless
    # PRODUCE_INGEST_ENABLED=1 is set (Render). Cross-process file lock
    # ensures only one gunicorn worker actually polls.
    produce_ingest.start_in_background()

    # Sam #2845/#2853: the Toast -> snapshot refresh. The IN-APP poller did the
    # heavy Toast pulls (many calls per employee) INSIDE the web worker, which
    # periodically saturated the Render worker -> intermittent 502s. So it is OFF
    # by default now: the snapshot still SERVES from the DB (fast read, pages stay
    # up), but it is REFRESHED by an EXTERNAL process so heavy Toast work never
    # touches the web dyno -- a Render cron running `python -m app.services.toast_sync`
    # (samai), or the token-gated POST /cron/toast-sync. Set TOAST_SYNC_POLLER=1 to
    # run the in-app poller anyway (e.g. local dev).
    import os as _os_poller
    if _os_poller.getenv("TOAST_SYNC_POLLER") == "1":
        try:
            from app.services import toast_sync
            toast_sync.start_in_background()
        except Exception:
            logging.getLogger(__name__).exception(
                "toast-sync poller failed to start (non-fatal)")

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
