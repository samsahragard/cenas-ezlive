"""Per-store URL prefix layer.

The site has 4 store contexts:
  /dos/        → Tomball  (single location)
  /uno/        → Copperfield  (single location)
  /corporate/  → both locations (general manager view)
  /partner/    → both locations (owners only — Sam + Masood; for now mirrors corporate)

Each route here is a thin wrapper that sets `g.current_store` and `g.current_location`
on the request, then delegates to the underlying view via an internal call.
The existing top-level routes (e.g. /reports/labor) continue to work for backwards
compat, but the new sidebar links exclusively use the /<store>/... URLs.

Flask's `url_value_preprocessor` pulls the slug off the URL into `g`; `url_defaults`
auto-injects it back when generating URLs inside a store context. This means
templates can write `url_for('store.reports_labor')` and Flask will produce
`/dos/reports/labor` while in the Tomball scope.
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, Response, g, abort, request, render_template, redirect, url_for, session, jsonify, send_file
from werkzeug.utils import secure_filename

from datetime import datetime, timedelta, date

from app.db import get_db
from app.models import Driver, DriverShift, DriverLocation, Order
from app.web.driver_routes import issue_temp_password, LOCATION_LABELS
# Phase 0 Block 4 (ck, 2026-05-13): permission gating per samai's spec.
from app.services.permissions import requires_permission
from app.web.dashboard_access import (
    current_role_is,
    has_dashboard_access,
    require_dashboard_access,
)

APP_TZ = os.getenv("APP_TZ", "America/Chicago")


def _central_offset_hours(utc_now: datetime) -> int:
    """Fallback for hosts without IANA tzdata: US Central DST by date."""
    y = utc_now.year
    mar1 = date(y, 3, 1)
    second_sunday_march = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    nov1 = date(y, 11, 1)
    first_sunday_nov = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return -5 if second_sunday_march <= utc_now.date() < first_sunday_nov else -6


def _local_today() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(APP_TZ)).date()
    except Exception:
        utc_now = datetime.utcnow()
        return (utc_now + timedelta(hours=_central_offset_hours(utc_now))).date()


def _today_label() -> str:
    _t = _local_today()
    return f"{_t:%a, %b} {_t.day}"


def _is_expo_insights_path(path: str, store_slug: str | None) -> bool:
    """True only for the real store-scoped Insights pages Expo cannot open."""
    slug = (store_slug or "").strip().lower()
    if not slug:
        return False
    norm = (path or "").strip().lower().rstrip("/")
    blocked_prefixes = (
        f"/{slug}/reports/sales",
        f"/{slug}/reports/labor",
        f"/{slug}/reports/server-performance",
        f"/{slug}/labor",
        f"/{slug}/sales",
        f"/{slug}/performance",
        f"/{slug}/forecast",
    )
    return any(norm == prefix or norm.startswith(prefix + "/")
               for prefix in blocked_prefixes)


# slug → location filter for downstream report functions
STORE_TO_LOCATION = {
    "dos":       "tomball",
    "uno":       "copperfield",
    "corporate": "both",
    "partner":   "both",
}

STORE_LABELS = {
    "dos":       "Tomball",
    "uno":       "Copperfield",
    "corporate": "Corporate",
    "partner":   "Partner",
}

# Marquee branding shown on the store picker
STORE_BRAND = {
    "dos":       {"marquee": "DOS MAS", "tagline": "Tomball · 27727 Tomball Pkwy"},
    "uno":       {"marquee": "UNO MAS", "tagline": "Copperfield · 15650 FM 529"},
    "corporate": {"marquee": "CORPORATE", "tagline": "Both locations"},
    "partner":   {"marquee": "PARTNER", "tagline": "Owners only"},
}


store_bp = Blueprint("store", __name__, url_prefix="/<store_slug>")


@store_bp.url_value_preprocessor
def _pull_store(endpoint, values):
    """Strip the slug off the URL and put it on `g` for downstream code."""
    if values is None:
        return
    slug = values.pop("store_slug", None)
    if slug not in STORE_TO_LOCATION:
        abort(404)
    g.current_store = slug
    g.current_location = STORE_TO_LOCATION[slug]
    g.store_label = STORE_LABELS[slug]


@store_bp.url_defaults
def _inject_store(endpoint, values):
    """When generating URLs from inside a store context, auto-fill the slug."""
    if "store_slug" not in values and getattr(g, "current_store", None):
        values["store_slug"] = g.current_store


@store_bp.before_request
def _partner_gate():
    """Second-factor auth for /partner/* — only owners (Sam + Masood) can see
    full management labor + future financial/legal sections. Everyone else
    sees Tomball / Copperfield / Corporate, which redact management data."""
    if getattr(g, "current_store", None) == "partner" and not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login"))


@store_bp.before_request
def _per_store_gate():
    """Per-store URL block + Expo-blocks-insights. Sam's 2026-05-11 spec:
      (1) GM/Manager/Expo with a single-store scope are hard-blocked from
          the other store's URLs (not just sidebar-hidden). Multi-store
          users can access each of their assigned stores.
      (2) Expo gets NO Insights pages — Performance / Sales / Labor /
          Forecasts return 403 even if URL-hopped.
    Partner / Corporate are unrestricted.

    NOTE: ck's c2ab56a 'docs' commit silently dropped this hook while
    committing a stale working copy. Restored 2026-05-11 — if it
    disappears again, check the most recent diff on store_routes.py."""
    from app.web.permissions import accessible_store_slugs

    u = getattr(g, "current_user", None)
    target = getattr(g, "current_store", None)
    if target:
        session["last_store_slug"] = target
    if u is None:
        return None  # legacy auth_ok sessions (tooling) skip this
    if u.permission_level in ("partner", "corporate"):
        return None

    # (2) Expo: deny any Insights-section URL outright.
    if u.permission_level == "expo":
        if _is_expo_insights_path(request.path or "", target):
            return ("Forbidden — Expo accounts don't have Insights access.", 403)

    # (1) Per-store scope block.
    if target is None:
        return None
    allowed = accessible_store_slugs(u)
    if target in allowed:
        return None
    if allowed:
        return redirect(f"/{allowed[0]}/")
    return ("Forbidden — your account isn't assigned to this store.", 403)


# ============== HOME DASHBOARD ==============

@store_bp.route("/")
def home():
    """Per-store manager dashboard. Re-uses the existing /home view but
    filtered to the current store's location."""
    require_dashboard_access("dash.today")
    if current_role_is("expo"):
        abort(403)
    from app.web import ezcater_routes
    return ezcater_routes.home()


# ============== GROUP LANDING PAGES ==============
# Each top-level sidebar group (Vendors / Ezcater / Schedule / Performance /
# Sales / Labor) is also clickable from the sidebar — clicking it lands on
# a per-section page that shows all its options as cards. The same children
# render in the hover-flyout on the sidebar.

def _render_landing(group_active: str, title: str, subtitle: str, cards: list[dict]):
    return render_template(
        "group_landing.html",
        store_label=g.store_label,
        group_active=group_active,
        landing_title=title,
        landing_subtitle=subtitle,
        cards=cards,
    )


# ============== PERSISTENT CATEGORY SUB-NAV ==============
# Sam (2026-05-10) wants the options that appear on a category's landing
# page (Ezcater → Orders / Order Processor / Driver Payroll / Driver Portal
# / Drivers Admin / Drivers Live) to also persist as a horizontal nav-row
# at the top of every sub-page in that category. base_dashboard.html calls
# subnav_for(active, store_slug) via the context processor below.
#
# This commit ships the Ezcater proof. Vendors / Schedule / Performance
# plug in their own builders in a follow-up once Sam signs off on the look.

def _ezcater_subnav_cards(store_slug: str) -> list[dict]:
    return [
        {"label": "Orders", "icon": "📋", "href": f"/{store_slug}/orders",
         "active": ["ezcater_orders"],
         "sub": "Today + upcoming catering orders for this store."},
        {"label": "Order Processor", "icon": "📄", "href": f"/{store_slug}/orders/processor",
         "active": ["processor"],
         "sub": "Upload PDF orders for legacy ingest (webhook is primary now)."},
        {"label": "Driver Payroll", "icon": "💵", "href": f"/{store_slug}/driver-tracking",
         "active": ["driver_tracking"],
         "sub": "Per-driver delivery log: miles / on-time / tracking / 5★ / notes."},
        {"label": "Drivers (Admin)", "icon": "👥", "href": f"/{store_slug}/drivers",
         "active": ["drivers_admin"],
         "sub": "Add / reset password / deactivate driver accounts."},
        {"label": "Drivers Live", "icon": "📍", "href": f"/{store_slug}/drivers-live",
         "active": ["drivers_live"],
         "sub": "Live GPS map of all drivers currently on shift."},
    ]


def _corporate_subnav_cards(store_slug: str) -> list[dict]:
    return [
        {"label": "Order", "icon": "🛒", "href": f"/{store_slug}/corporate-order",
         "active": ["corporate_order"],
         "sub": "Catalog + cart for the corporate order surface."},
        {"label": "Reports", "icon": "📊", "href": f"/{store_slug}/corporate-order/reports",
         "active": ["corporate_order_reports"],
         "sub": "Order history + per-store analytics + top products."},
    ]


def _vendors_subnav_cards(store_slug: str) -> list[dict]:
    return [
        {"label": "Produce — Order", "icon": "🥬", "href": f"/{store_slug}/produce/",
         "active": ["produce"],
         "sub": "Today's order guide with cheaper-vendor pricing + one-click submit."},
        {"label": "Produce — Price History", "icon": "📈", "href": f"/{store_slug}/produce/orders",
         "active": ["produce_orders"],
         "sub": "Per-item price tracking and biggest-movers callout."},
        {"label": "Webstaurant", "icon": "📦", "disabled": True,
         "active": ["webstaurant"],
         "sub": "Restaurant-supply orders. Coming soon."},
        {"label": "Vendor Performance", "icon": "📊", "disabled": True,
         "active": ["vendor_perf"],
         "sub": "On-time / accuracy scoring per vendor. Coming soon."},
        {"label": "Specs", "icon": "📋", "disabled": True,
         "active": ["specs"],
         "sub": "Product specs + nutritional info. Coming soon."},
    ]


def _schedule_subnav_cards(store_slug: str) -> list[dict]:
    return [
        {"label": "BOH Roster", "icon": "👨‍🍳", "href": f"/{store_slug}/roster/boh",
         "active": ["boh_roster"],
         "sub": "Back-of-house employees on roster."},
        {"label": "FOH Roster", "icon": "🍽", "href": f"/{store_slug}/roster/foh",
         "active": ["foh_roster"],
         "sub": "Front-of-house employees on roster."},
        {"label": "All Roster", "icon": "👥", "href": f"/{store_slug}/roster/all",
         "active": ["all_roster"],
         "sub": "Combined roster across BOH + FOH."},
        {"label": "Weekly Schedule", "icon": "📅", "href": f"/{store_slug}/schedules-v2/",
         "active": ["schedules_v2", "weekly_schedule"],
         "sub": "Build, publish & assign shifts."},
        {"label": "Time Off", "icon": "🌴", "href": f"/{store_slug}/schedules-v2/time-off",
         "active": ["schedules_v2_timeoff"],
         "sub": "Review & approve time-off requests."},
        {"label": "Availability", "icon": "🕒", "href": f"/{store_slug}/schedules-v2/availability",
         "active": ["schedules_v2_availability"],
         "sub": "Who can work, when."},
        {"label": "Marketplace", "icon": "🔁", "href": f"/{store_slug}/schedules-v2/marketplace",
         "active": ["schedules_v2_marketplace"],
         "sub": "Approve shift offers & swaps."},
        {"label": "Add Staff", "icon": "➕", "href": f"/{store_slug}/schedules-v2/employees",
         "active": ["schedules_v2_employees"],
         "sub": "Invite a new team member by email."},
    ]


def _performance_subnav_cards(store_slug: str) -> list[dict]:
    return [
        {"label": "Server", "icon": "🍽", "href": f"/{store_slug}/reports/server-performance/server",
         "active": ["perf_server"],
         "sub": "Per-server tip % + service timing."},
        {"label": "Bartenders", "icon": "🍹", "href": f"/{store_slug}/reports/server-performance/bartenders",
         "active": ["perf_bartenders"],
         "sub": "Bartender-specific performance breakdown."},
        {"label": "Prep", "icon": "🔪", "disabled": True,
         "active": ["perf_prep"],
         "sub": "Prep-side performance metrics. Coming soon."},
        {"label": "All", "icon": "📊", "href": f"/{store_slug}/reports/server-performance/all",
         "active": ["perf_all", "server_perf"],
         "sub": "All FOH service performance combined."},
    ]


def _sales_subnav_cards(store_slug: str) -> list[dict]:
    return [
        {"label": "All", "icon": "📊", "href": f"/{store_slug}/sales",
         "active": ["sales_landing", "third_party_sales", "sales_total"],
         "sub": "All channels combined."},
        {"label": "Toast", "icon": "🍞", "href": f"/{store_slug}/sales?channels=toast",
         "active": ["sales_toast"],
         "sub": "Toast in-store sales."},
        {"label": "Online Toast", "icon": "💻", "href": f"/{store_slug}/sales?channels=toast_online",
         "active": ["sales_online"],
         "sub": "Toast online ordering sales."},
        {"label": "Ezcater", "icon": "📋", "href": f"/{store_slug}/sales?channels=ezcater",
         "active": ["sales_ezcater"],
         "sub": "ezCater catering channel."},
        {"label": "DoorDash", "icon": "🚪", "href": f"/{store_slug}/sales?channels=doordash",
         "active": ["sales_doordash"],
         "sub": "DoorDash channel."},
        {"label": "Uber", "icon": "🚗", "href": f"/{store_slug}/sales?channels=uber",
         "active": ["sales_uber"],
         "sub": "Uber Eats channel."},
    ]


def _labor_subnav_cards(store_slug: str) -> list[dict]:
    return [
        {"label": "Total", "icon": "📊", "href": f"/{store_slug}/labor",
         "active": ["labor_landing", "labor"],
         "sub": "All-roles labor cost vs revenue."},
        {"label": "BOH Labor", "icon": "👨‍🍳", "href": f"/{store_slug}/labor/boh",
         "active": ["boh_labor"],
         "sub": "Back-of-house labor breakdown."},
        {"label": "FOH Labor", "icon": "🍽", "href": f"/{store_slug}/labor/foh",
         "active": ["foh_labor"],
         "sub": "Front-of-house labor breakdown."},
    ]


def _developer_subnav_cards(store_slug: str) -> list[dict]:
    # Developer routes live under /partner/developer/ — Partner-gated; the
    # store_slug arg is ignored since the URLs are absolute. The App pill
    # highlights for any of the doc_* active values so it stays active as
    # users move between docs.
    return [
        {"label": "Chat", "icon": "💬", "href": "/partner/developer/chat",
         "active": ["dev_chat", "chat"],
         "sub": "AI + human Developer Chat (DB-backed, attachments)."},
        {"label": "Ezcater Review", "icon": "⚠", "href": "/partner/developer/ezcater",
         "active": ["dev_ezcater_review"],
         "sub": "Partner-only review queue for needs_review orders."},
        {"label": "App", "icon": "📘", "href": "/partner/developer/app",
         "active": ["doc_readme", "doc_architecture", "doc_features",
                    "doc_tech_stack", "doc_deployment", "doc_data_sources",
                    "doc_ck_session_2026_05_10", "doc_aick_session_2026_05_10",
                    "doc_ck_session_2026_05_11",
                    "doc_agent_bootstrap"],
         "sub": "App docs hub: README / architecture / features / etc."},
    ]


# Map each per-page `active` value to its category. Keep in lock-step with
# the *_open sets in base_dashboard.html. Pages whose active isn't here
# don't get a sub-nav (None falls through cleanly in the template).
_ACTIVE_TO_CATEGORY = {
    # Ezcater (matches ezc_open)
    "ezcater_landing":  "ezcater",
    "ezcater_orders":   "ezcater",
    "processor":        "ezcater",
    "driver_tracking":  "ezcater",
    "drivers_admin":    "ezcater",
    "drivers_live":     "ezcater",
    # Corporate Order (matches corp_open)
    "corporate_order":         "corporate",
    "corporate_order_reports": "corporate",
    # Vendors (matches vendors_open)
    "vendors":         "vendors",
    "produce":         "vendors",
    "produce_orders":  "vendors",
    "webstaurant":     "vendors",
    "vendor_perf":     "vendors",
    "specs":           "vendors",
    # Schedule (matches sched_open)
    "schedule_landing":  "schedule",
    "boh_roster":        "schedule",
    "foh_roster":        "schedule",
    "all_roster":        "schedule",
    "weekly_schedule":   "schedule",
    "schedules_v2":             "schedule",   # B-cutover: V2 week-view is the primary schedule surface
    "schedules_v2_timeoff":     "schedule",
    "schedules_v2_availability":"schedule",
    "schedules_v2_marketplace": "schedule",
    "schedules_v2_employees":   "schedule",   # B11: manager Add-Staff (email onboarding)
    # Performance (matches perf_open)
    "perf_landing":     "performance",
    "perf_server":      "performance",
    "perf_bartenders":  "performance",
    "perf_prep":        "performance",
    "perf_all":         "performance",
    "server_perf":      "performance",
    # Sales (matches sales_open)
    "sales_landing":      "sales",
    "third_party_sales":  "sales",
    "sales_toast":        "sales",
    "sales_online":       "sales",
    "sales_ezcater":      "sales",
    "sales_doordash":     "sales",
    "sales_uber":         "sales",
    "sales_total":        "sales",
    # Labor (matches labor_open)
    "labor_landing":  "labor",
    "labor":          "labor",
    "boh_labor":      "labor",
    "foh_labor":      "labor",
    # Developer (matches dev_chat / dev_ezcater_review / app_doc_open)
    "dev_chat":                     "developer",
    "chat":                         "developer",
    "dev_ezcater_review":           "developer",
    "doc_readme":                   "developer",
    "doc_architecture":             "developer",
    "doc_features":                 "developer",
    "doc_tech_stack":               "developer",
    "doc_deployment":               "developer",
    "doc_data_sources":             "developer",
    "doc_ck_session_2026_05_10":    "developer",
    "doc_aick_session_2026_05_10":  "developer",
    "doc_ck_session_2026_05_11":    "developer",
    "doc_agent_bootstrap":          "developer",
}

_CATEGORY_SUBNAV_BUILDERS = {
    "ezcater":     _ezcater_subnav_cards,
    "corporate":   _corporate_subnav_cards,
    "vendors":     _vendors_subnav_cards,
    "schedule":    _schedule_subnav_cards,
    "performance": _performance_subnav_cards,
    "sales":       _sales_subnav_cards,
    "labor":       _labor_subnav_cards,
    "developer":   _developer_subnav_cards,
}


def _subnav_for(active, store_slug):
    if not active:
        return None
    category = _ACTIVE_TO_CATEGORY.get(active)
    if not category:
        return None
    builder = _CATEGORY_SUBNAV_BUILDERS.get(category)
    if not builder:
        return None
    return builder(store_slug or "partner")


@store_bp.app_context_processor
def _inject_subnav():
    """Expose subnav_for() to every template via the base layout."""
    return {"subnav_for": _subnav_for}


@store_bp.route("/ezcater")
def ezcater_landing():
    cards = _ezcater_subnav_cards(g.current_store)
    return _render_landing("ezcater_landing", "Ezcater", f"{g.store_label} · catering operations", cards)


@store_bp.route("/schedule-overview")
def schedule_landing():
    cards = _schedule_subnav_cards(g.current_store)
    return _render_landing("schedule_landing", "Schedule",
                           f"{g.store_label} · roster + Sling weekly schedule", cards)


@store_bp.route("/performance")
def performance_landing():
    cards = _performance_subnav_cards(g.current_store)
    return _render_landing("perf_landing", "Performance",
                           f"{g.store_label} · service + prep metrics", cards)


@store_bp.route("/sales")
def sales_landing():
    """Per Sam: /uno/sales lands on the actual sales report (not a card-grid),
    with multi-channel selection + Today/This Week/Last Week pills. Default
    = All channels + Today. Delegates to the third-party-sales view so URL
    state (channels=, period=) stays share-friendly."""
    from app.web.reports import third_party_sales as view
    if g.current_location and g.current_location != "both":
        g.location_override = g.current_location
    return view()


@store_bp.route("/labor")
def labor_landing():
    """Per Sam: lands directly on the labor report with Today/Week/LastWeek
    + BOH/FOH/All pills. Default = All + Today. Delegates to reports.labor."""
    from app.web.reports import labor as view
    if g.current_location and g.current_location != "both":
        g.location_override = g.current_location
    return view()


# ============== OPERATIONS — VENDORS ==============

@store_bp.route("/produce/")
def produce_root():
    """Render the produce order guide inline so g.current_store survives.
    Redirecting to /produce/ would lose the store context and base_dashboard
    falls back to dos (Tomball) — same shape as the orders_processor fix."""
    from app.web.produce_order import index as _produce_index
    return _produce_index()


@store_bp.route("/produce/<path:subpath>")
def produce_subpath(subpath: str = ""):
    """Sub-routes (submit, confirm, cancel, etc.) still 302 since they're
    POST endpoints or terminal pages with their own templates (produce/base
    extends base_dashboard but those pages don't need the store sidebar)."""
    target = "/produce/" + (subpath or "")
    if request.query_string:
        target += "?" + request.query_string.decode()
    return redirect(target)


@store_bp.route("/produce/orders")
def produce_orders():
    """Vendors → Produce → Orders (per Sam's mockup). Price history + biggest
    movers + winner table. Currently org-wide (vendor pricing isn't location-
    specific in our setup); the per-store URL prefix is preserved for symmetry
    with the other sidebar leaves."""
    from app.web.reports import produce_orders as view
    return view()


# ============== OPERATIONS — EZCATER ==============

@store_bp.route("/orders")
def orders_list():
    """Per-location order list. For partner/corporate (location=both), renders
    a combined view of Tomball + Copperfield orders so Ezcater→Orders is a
    functional landing for partner-level users. For per-store sidebars,
    renders just that store's orders. Sidebar context is preserved via
    store_bp.url_value_preprocessor — without that, bare /orders/<location>
    would lose store context (same shape as the driver-tracking bug)."""
    if g.current_location == "both":
        from app.web.orders_browse import (
            list_orders_for_location, group_orders_by_date,
            _active_drivers_by_prefix, _driver_status_by_order_id,
        )
        from app.services.orders_query import rotated_dispatch_letters
        db = next(get_db())
        try:
            selected_store_filter = _orders_store_filter_arg(request.args.get("store"))
            tom = (
                list_orders_for_location(db, "tomball")
                if selected_store_filter in ("", "tomball")
                else []
            )
            cop = (
                list_orders_for_location(db, "copperfield")
                if selected_store_filter in ("", "copperfield")
                else []
            )
            combined = tom + cop
            groups = group_orders_by_date(combined)
            display_drivers = rotated_dispatch_letters(groups)
            from app.services.ezcater_management_presenter import compact_order_card
            return render_template(
                "orders_by_store.html",
                location="both",
                location_label="All Orders",
                groups=groups,
                display_drivers=display_drivers,
                active_drivers_by_prefix=_active_drivers_by_prefix(db),
                driver_status_by_order_id=_driver_status_by_order_id(db, combined),
                compact_order_card=compact_order_card,
                selected_store_filter=selected_store_filter,
                store_filter_options=[
                    {"value": "", "label": "All"},
                    {"value": "copperfield", "label": "Copperfield"},
                    {"value": "tomball", "label": "Tomball"},
                ],
            )
        finally:
            db.close()
    from app.web.orders_browse import location_orders
    return location_orders(g.current_location)


@store_bp.route("/orders/processor", methods=["GET", "POST"])
def orders_processor():
    """PDF processor — renders cater.orders inline so the sidebar inherits
    g.current_store / g.store_label (otherwise the bare /orders URL falls
    back to Tomball default — same shape as the driver-tracking bug)."""
    from app.web.ezcater_routes import orders as _cater_orders
    return _cater_orders()


@store_bp.route("/review")
def review_queue():
    """Retired 2026-05-10 — Review Queue replaced by auto-resolver +
    Telegram alerts. Redirect any old bookmarks back to the store dashboard."""
    return redirect(f"/{g.current_store}/")


@store_bp.route("/driver-tracking", methods=["GET"])
def driver_tracking():
    """Driver Payroll — per-driver list. Each name links to that driver's
    paycheck-history page. Replaces the old manager_log view at this URL
    (the old form still lives at /driver-logs for now). Sam's 2026-05-10
    spec: show name/email/phone/address/account, click name -> paycheck."""
    from app.models import EzcaterKnownDriver
    from app.services.ezcater_known_drivers_seed import normalize_phone
    from app.services.ezcater_payroll import (
        period_containing, previous_period, paycheck_for,
    )
    from datetime import date as _date

    db = next(get_db())
    try:
        # Scope by current store. /partner + /corporate show all; /dos shows
        # ck_prefix=2 (Tomball) drivers; /uno shows ck_prefix=1 (Copperfield).
        q = db.query(EzcaterKnownDriver)
        if g.current_location == "tomball":
            q = q.filter(EzcaterKnownDriver.ck_prefix == 2)
        elif g.current_location == "copperfield":
            q = q.filter(EzcaterKnownDriver.ck_prefix == 1)
        roster = q.order_by(EzcaterKnownDriver.ck_prefix.asc(),
                            EzcaterKnownDriver.name.asc()).all()

        # Match each roster row to a Driver in our DB by phone (for showing
        # email / address / account status).
        drivers_by_phone = {}
        for d in db.query(Driver).filter(Driver.phone.isnot(None)).all():
            drivers_by_phone[normalize_phone(d.phone)] = d

        # Per-period summary: show the JUST-CLOSED period (the one being paid
        # out next), not the in-progress one. Sam's spec sample shows period
        # 4/26-5/9 with check date 5/14 — that's the previous period when
        # today is 5/10 (the new period's first day). The paycheck detail
        # page still shows full history including the in-progress period.
        cur_start, _, _ = period_containing(_local_today())
        period_start, period_end, check_date = previous_period(cur_start)

        rows = []
        for kd in roster:
            signed_up = drivers_by_phone.get(kd.phone_e164)
            pp = paycheck_for(kd.name, period_start, period_end)
            rows.append({
                "id": kd.id,
                "name": kd.name,
                "ck_prefix": kd.ck_prefix,
                "phone": kd.phone_e164,
                "signed_up_driver": signed_up,
                "current_deliveries": len(pp.deliveries),
                "current_total": pp.grand_total,
            })

        return render_template(
            "driver_payroll_list.html",
            active="driver_tracking",
            page_title="Driver Payroll",
            rows=rows,
            current_period_start=period_start,
            current_period_end=period_end,
            current_check_date=check_date,
        )
    finally:
        db.close()


@store_bp.route("/driver-paycheck/by-driver/<int:driver_id>", methods=["GET"])
def driver_paycheck_by_driver(driver_id: int):
    """Per Sam #837 item 6a + 6d — paycheck history keyed on Driver.id
    instead of EzcaterKnownDriver.id. Lets deactivated / hard-deleted
    drivers still surface their historical paycheck rows because
    paycheck_for() looks up by NAME against Order.ezcater_driver_name,
    not by the roster table (which item 6 wiped today). Replaces the
    old EzcaterKnownDriver-keyed driver_paycheck route for the new
    Payroll column on /partner/drivers."""
    from app.services.ezcater_payroll import paycheck_history

    db = next(get_db())
    try:
        d = db.get(Driver, driver_id)
        if not d:
            from flask import abort as _abort
            _abort(404)
        # store-scope guard: a tomball manager shouldn't be able to peek
        # at a copperfield driver's paychecks. partner / corporate see all.
        if g.current_location == "tomball" and (d.location or "") == "copperfield":
            from flask import abort as _abort
            _abort(404)
        if g.current_location == "copperfield" and (d.location or "") == "tomball":
            from flask import abort as _abort
            _abort(404)
        history = paycheck_history(d.name, periods=6)
        return render_template(
            "driver_paycheck.html",
            active="drivers_admin",
            page_title=f"Paycheck — {d.name}",
            driver=d,
            history=history,
            manager_edit=True,
        )
    finally:
        db.close()


@store_bp.route("/driver-tracking/<int:known_id>", methods=["GET"])
def driver_paycheck(known_id: int):
    """Per-driver paycheck history. Shows the current bi-weekly period plus
    the previous 5, each with one row per delivery (tracking status, ex
    miles, base, bonuses, total)."""
    from app.models import EzcaterKnownDriver
    from app.services.ezcater_payroll import paycheck_history

    db = next(get_db())
    try:
        kd = db.get(EzcaterKnownDriver, known_id)
        if not kd:
            from flask import abort as _abort
            _abort(404)
        # Optional store-scope guard: a /dos manager shouldn't be able to
        # peek at a CK#1 driver's paycheck. Soft enforcement — only block
        # cross-store views on per-location store contexts (partner/corp see all).
        if g.current_location == "tomball" and kd.ck_prefix == 1:
            from flask import abort as _abort
            _abort(404)
        if g.current_location == "copperfield" and kd.ck_prefix == 2:
            from flask import abort as _abort
            _abort(404)
        history = paycheck_history(kd.name, periods=6)
        return render_template(
            "driver_paycheck.html",
            active="driver_tracking",
            page_title=f"Paycheck — {kd.name}",
            driver=kd,
            history=history,
            manager_edit=True,
        )
    finally:
        db.close()


def _parse_payroll_float(raw):
    """Parse a manager miles input. Blank/garbage -> None (= 'not verified
    yet', so the auto estimate keeps paying). '0' -> 0.0 (an explicit zero).
    Negative values clamp to 0."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return round(max(0.0, v), 2)


def _safe_return_path(raw: str | None, fallback: str) -> str:
    """Only allow a same-site relative redirect (block open-redirects)."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return fallback


@store_bp.route("/driver-payroll/save", methods=["POST"])
@requires_permission("drivers.admin")
def driver_payroll_save():
    """Manager payroll input (Sam #1492/#1503, 2026-05-28). Writes the
    per-order pay_* columns for each payroll-ready order id posted from the
    Ez Drivers paycheck page. The 2h-after-delivery readiness gate is
    re-enforced server-side (never trust the client). On success we redirect
    back to the same paycheck page with a ?saved= banner; the saved verified
    miles / $10 / 5★ / notes then feed both this view and the driver's own
    pay page."""
    from app.models import Order
    from app.services.ezcater_payroll import payroll_ready

    rowids = [x.strip() for x in (request.form.get("rowids", "") or "").split(",")
              if x.strip().isdigit()]
    user = getattr(g, "current_user", None)
    saved_by = (getattr(user, "full_name", None)
                or getattr(user, "email", None) or "manager")
    now = datetime.utcnow()

    db = next(get_db())
    saved = 0
    skipped = 0
    try:
        for rid in rowids:
            o = db.get(Order, int(rid))
            if o is None:
                continue
            ready, _ = payroll_ready(o)
            if not ready:
                skipped += 1
                continue
            o.pay_verified_miles = _parse_payroll_float(request.form.get(f"vmiles_{rid}"))
            o.pay_driven_miles = _parse_payroll_float(request.form.get(f"dmiles_{rid}"))
            o.pay_bonus_tracked = request.form.get(f"bonus_{rid}") is not None
            o.pay_five_star = request.form.get(f"fivestar_{rid}") is not None
            note = (request.form.get(f"notes_{rid}") or "").strip()
            o.pay_notes = note[:500] or None
            o.pay_verified_at = now
            o.pay_verified_by = str(saved_by)[:80]
            saved += 1
        db.commit()
    finally:
        db.close()

    dest = _safe_return_path(request.form.get("return_to"),
                             url_for("store.driver_tracking"))
    sep = "&" if "?" in dest else "?"
    dest = f"{dest}{sep}saved={saved}"
    if skipped:
        dest = f"{dest}&pending={skipped}"
    return redirect(dest)


@store_bp.route("/driver-portal")
def driver_portal():
    return redirect("/driver" + (("?" + request.query_string.decode()) if request.query_string else ""))


# ============== OPERATIONS — DRIVERS ADMIN ==============

@store_bp.route("/drivers", methods=["GET"])
@requires_permission("drivers.admin")
def drivers_admin():
    """Per-store driver admin: list / reset PW / deactivate.

    Per-location stores see only their own drivers; corporate + partner see all.
    Anyone past the site `cenas` gate can reach /uno/, /dos/, /corporate/.
    Partner is additionally gated by the partner-auth before_request hook above.
    """
    from app.models import EzcaterKnownDriver
    from app.services.ezcater_known_drivers_seed import normalize_phone, names_match
    # Tab filter — defaults to active. Intentional default-state change; see spec §3.
    status = request.args.get("status", "active")
    if status not in ("active", "inactive"):
        status = "active"
    show_active = (status == "active")

    db = next(get_db())
    try:
        from sqlalchemy import func
        q = db.query(Driver).filter(Driver.active == show_active)
        if g.current_location != "both":
            q = q.filter(Driver.location == g.current_location)
        rows = q.order_by(Driver.location, Driver.name).all()

        # Count both tabs for pill labels
        active_q = db.query(Driver).filter(Driver.active == True)
        inactive_q = db.query(Driver).filter(Driver.active == False)
        if g.current_location != "both":
            active_q = active_q.filter(Driver.location == g.current_location)
            inactive_q = inactive_q.filter(Driver.location == g.current_location)
        active_count = active_q.count()
        inactive_count = inactive_q.count()

        # latest shift per driver — drives the click-Active-to-see-location link
        latest_shift_for = {}
        if rows:
            ids = [d.id for d in rows]
            for did, sid in (db.query(DriverShift.driver_id, func.max(DriverShift.id))
                              .filter(DriverShift.driver_id.in_(ids))
                              .group_by(DriverShift.driver_id)
                              .all()):
                latest_shift_for[did] = sid

        # Phone-match → "Verified ezCater driver" badge. Sam (2026-05-10):
        # the green Active badge should reflect whether the driver's signup
        # phone matches an entry in our seeded ezCater roster, not the
        # manual on/off toggle alone. The toggle still exists as an override.
        known_all = db.query(EzcaterKnownDriver).all()
        known_by_phone = {kd.phone_e164: kd for kd in known_all}

        def _match_known(d):
            # Phone match is authoritative (Sam 2026-05-10). Fall back to
            # a fuzzy NAME match against the roster (Sam #1431) so a real
            # ezCater driver still earns the badge when their signup phone
            # is blank or differs from the number we have on the roster.
            if d.phone:
                kd = known_by_phone.get(normalize_phone(d.phone))
                if kd:
                    return kd
            for kd in known_all:
                if names_match(kd.name, d.name):
                    return kd
            return None

        verified_for = {}
        ezcater_name_for = {}
        for d in rows:
            kd = _match_known(d)
            verified_for[d.id] = kd is not None
            # Per Sam #837 item 6c — show "CK #X · Kitchen" on the
            # combined drivers page so managers see ezCater identity
            # alongside the internal driver record.
            if kd:
                if kd.ck_prefix == 1:
                    ezcater_name_for[d.id] = "CK #1 · Copperfield"
                elif kd.ck_prefix == 2:
                    ezcater_name_for[d.id] = "CK #2 · Tomball"
                else:
                    ezcater_name_for[d.id] = kd.name

        return render_template(
            "driver_admin.html",
            drivers=rows,
            latest_shift_for=latest_shift_for,
            verified_for=verified_for,
            ezcater_name_for=ezcater_name_for,
            store_label=g.store_label,
            current_location=g.current_location,
            location_labels=LOCATION_LABELS,
            temp_pw=request.args.get("temp_pw"),
            temp_for=request.args.get("temp_for"),
            error=request.args.get("error"),
            active="drivers_admin",
            current_status=status,
            active_count=active_count,
            inactive_count=inactive_count,
        )
    finally:
        db.close()


@store_bp.route("/drivers/<int:driver_id>/update", methods=["POST"])
@requires_permission("drivers.admin")
def drivers_update(driver_id: int):
    """Update editable driver fields per Sam #837 item 6b — inline
    dropdown panel on /partner/drivers. Accepts location / email /
    phone / address. Each field updated only if present in the form
    (so the form can be partial). Returns to the drivers admin list."""
    db = next(get_db())
    try:
        row = db.get(Driver, driver_id)
        if not row or (g.current_location != "both" and row.location != g.current_location):
            return redirect(url_for("store.drivers_admin",
                                    error="Driver not found at this store."))
        loc = (request.form.get("location") or "").strip().lower()
        if loc in ("tomball", "copperfield"):
            row.location = loc
        if "email" in request.form:
            v = (request.form.get("email") or "").strip()
            row.email = v or None
        if "phone" in request.form:
            v = (request.form.get("phone") or "").strip()
            row.phone = v or None
        if "address" in request.form:
            v = (request.form.get("address") or "").strip()
            row.address = v or None
        db.commit()
        return redirect(url_for("store.drivers_admin"))
    finally:
        db.close()


@store_bp.route("/drivers/<int:driver_id>/reset", methods=["POST"])
@requires_permission("drivers.reset_passcode")
def drivers_reset(driver_id: int):
    db = next(get_db())
    try:
        row = db.get(Driver, driver_id)
        if not row or (g.current_location != "both" and row.location != g.current_location):
            return redirect(url_for("store.drivers_admin", error="Driver not found at this store."))
        temp = issue_temp_password(db, row)
        return redirect(url_for("store.drivers_admin", temp_pw=temp, temp_for=row.name))
    finally:
        db.close()


# ============== OPERATIONS — DRIVERS LIVE MAP ==============

# Locations of the two stores — used as the initial map centre when no
# drivers are streaming yet
_STORE_CENTRES = {
    "tomball":     (30.1118, -95.6230),   # 27727 Tomball Pkwy
    "copperfield": (29.8730, -95.6428),   # 15650 FM 529
    "both":        (30.0,    -95.6),      # midpoint-ish
}


@store_bp.route("/drivers-live", methods=["GET"])
def drivers_live():
    """Live map: a marker per driver currently on shift, auto-refreshing."""
    centre_lat, centre_lng = _STORE_CENTRES.get(g.current_location, _STORE_CENTRES["both"])
    return render_template(
        "drivers_live.html",
        store_label=g.store_label,
        current_location=g.current_location,
        centre_lat=centre_lat,
        centre_lng=centre_lng,
        active="drivers_live",
    )


@store_bp.route("/drivers-live/positions.json", methods=["GET"])
def drivers_live_positions():
    """JSON feed for the map. Returns one record per driver currently on shift,
    with their most recent position fix. Filters to drivers at this store
    (or all-locations for corporate/partner)."""
    db = next(get_db())
    try:
        # Open shifts joined to driver, optionally filtered by location
        q = (db.query(DriverShift, Driver)
             .join(Driver, DriverShift.driver_id == Driver.id)
             .filter(DriverShift.ended_at.is_(None)))
        if g.current_location != "both":
            q = q.filter(Driver.location == g.current_location)
        results = []
        now = datetime.utcnow()
        for shift, drv in q.all():
            latest = (db.query(DriverLocation)
                      .filter(DriverLocation.shift_id == shift.id)
                      .order_by(DriverLocation.captured_at.desc())
                      .first())
            if not latest:
                continue
            active_order = db.get(Order, latest.order_id) if latest.order_id else None
            seconds_ago = max(0, int((now - latest.captured_at).total_seconds()))
            results.append({
                "driver_id":      drv.id,
                "name":           drv.name,
                "location":       drv.location,
                "order_id":       latest.order_id,
                "order_number":   active_order.external_order_id if active_order else None,
                "shift_started":  shift.started_at.isoformat() + "Z",
                "lat":            latest.lat,
                "lng":            latest.lng,
                "accuracy_m":     latest.accuracy_m,
                "speed_mps":      latest.speed_mps,
                "heading_deg":    latest.heading_deg,
                "captured_at":    latest.captured_at.isoformat() + "Z",
                "seconds_ago":    seconds_ago,
                "stale":          seconds_ago > 30,
            })
        return jsonify({"drivers": results, "now": now.isoformat() + "Z"})
    finally:
        db.close()


# ============== OPERATIONS — DRIVER SHIFT HISTORY / PLAYBACK ==============

import math


def _haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dlng/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def _shift_summary(db, shift):
    """Lightweight summary for one shift: duration, point count, distance, max speed.
    Distance is the sum of consecutive haversine hops, so it includes minor GPS
    jitter — fine for a glance-able number; not survey-grade."""
    pts = (db.query(DriverLocation)
           .filter(DriverLocation.shift_id == shift.id)
           .order_by(DriverLocation.captured_at.asc())
           .all())
    distance_m = 0.0
    max_speed = 0.0
    for prev, cur in zip(pts, pts[1:]):
        distance_m += _haversine_m(prev.lat, prev.lng, cur.lat, cur.lng)
        if cur.speed_mps and cur.speed_mps > max_speed:
            max_speed = cur.speed_mps
    end = shift.ended_at or datetime.utcnow()
    duration_s = max(0, int((end - shift.started_at).total_seconds()))
    return {
        "point_count": len(pts),
        "distance_m":  round(distance_m, 1),
        "duration_s":  duration_s,
        "max_speed_mps": round(max_speed, 2),
        "last_lat":    pts[-1].lat if pts else None,
        "last_lng":    pts[-1].lng if pts else None,
    }


def _scoped_driver(db, driver_id):
    """Look up a driver, enforcing the store's location scope."""
    drv = db.get(Driver, driver_id)
    if not drv:
        return None
    if g.current_location != "both" and drv.location != g.current_location:
        return None
    return drv


def _store_can_view_order(order: Order) -> bool:
    if g.current_location == "both":
        return True
    origin = (order.origin_store_id or order.reported_store_id or "").strip().lower()
    pickup = (order.pickup_kitchen or "").strip().lower()
    if g.current_location == "tomball":
        return origin in {"store_2", "store_4"} or pickup == "tomball"
    if g.current_location == "copperfield":
        return origin in {"store_1", "store_3"} or pickup == "copperfield"
    return False


@store_bp.route("/drivers/<int:driver_id>/shifts", methods=["GET"])
def driver_shifts(driver_id: int):
    db = next(get_db())
    try:
        drv = _scoped_driver(db, driver_id)
        if not drv:
            return redirect(url_for("store.drivers_admin", error="Driver not found at this store."))
        shifts = (db.query(DriverShift)
                  .filter(DriverShift.driver_id == driver_id)
                  .order_by(DriverShift.started_at.desc())
                  .limit(60)
                  .all())
        rows = [{"shift": s, "summary": _shift_summary(db, s)} for s in shifts]
        return render_template(
            "driver_shifts.html",
            driver=drv,
            store_label=g.store_label,
            current_location=g.current_location,
            location_labels=LOCATION_LABELS,
            rows=rows,
            active="drivers_admin",
        )
    finally:
        db.close()


@store_bp.route("/ezcater-route/<int:order_id>", methods=["GET"])
def ezcater_route_playback(order_id: int):
    from app.services.ezcater_route_history import route_summary_for_order

    db = next(get_db())
    try:
        order = db.get(Order, order_id)
        if not order or not _store_can_view_order(order):
            abort(404)
        summary = route_summary_for_order(db, order_id)
        driver_id = summary.driver_id or order.assigned_driver_id
        driver = db.get(Driver, driver_id) if driver_id else None
        return render_template(
            "ezcater_route_playback.html",
            active="driver_tracking",
            order=order,
            driver=driver,
            summary=summary,
            route_track_url=url_for("store.ezcater_route_track", order_id=order_id),
            back_url=request.referrer or url_for("store.driver_tracking"),
            location_labels=LOCATION_LABELS,
            viewer="manager",
        )
    finally:
        db.close()


@store_bp.route("/ezcater-route/<int:order_id>/track.json", methods=["GET"])
def ezcater_route_track(order_id: int):
    from app.services.ezcater_route_history import route_point_dicts, route_summary_for_order

    db = next(get_db())
    try:
        order = db.get(Order, order_id)
        if not order or not _store_can_view_order(order):
            return jsonify({"error": "not found"}), 404
        summary = route_summary_for_order(db, order_id)
        return jsonify({
            "order_id": order_id,
            "order_number": order.external_order_id,
            "tracking_uuid": order.delivery_tracking_id,
            "summary": {
                "point_count": summary.point_count,
                "distance_miles": round(summary.distance_miles, 3),
                "extra_miles_over_20": round(summary.extra_miles_over_20, 3),
                "duration_minutes": summary.duration_minutes,
                "started_at": summary.started_at.isoformat() + "Z" if summary.started_at else None,
                "ended_at": summary.ended_at.isoformat() + "Z" if summary.ended_at else None,
                "driver_id": summary.driver_id,
                "driver_name": summary.driver_name,
                "status_key": summary.status_key,
            },
            "points": route_point_dicts(db, order_id),
        })
    finally:
        db.close()


@store_bp.route("/drivers/<int:driver_id>/shifts/<int:shift_id>", methods=["GET"])
def driver_shift_playback(driver_id: int, shift_id: int):
    db = next(get_db())
    try:
        drv = _scoped_driver(db, driver_id)
        if not drv:
            return redirect(url_for("store.drivers_admin", error="Driver not found at this store."))
        shift = db.get(DriverShift, shift_id)
        if not shift or shift.driver_id != driver_id:
            return redirect(url_for("store.driver_shifts", driver_id=driver_id))
        summary = _shift_summary(db, shift)
        return render_template(
            "driver_playback.html",
            driver=drv,
            shift=shift,
            summary=summary,
            store_label=g.store_label,
            location_labels=LOCATION_LABELS,
            active="drivers_admin",
        )
    finally:
        db.close()


@store_bp.route("/drivers/<int:driver_id>/shifts/<int:shift_id>/track.json", methods=["GET"])
def driver_shift_track(driver_id: int, shift_id: int):
    db = next(get_db())
    try:
        drv = _scoped_driver(db, driver_id)
        if not drv:
            return jsonify({"error": "not found"}), 404
        shift = db.get(DriverShift, shift_id)
        if not shift or shift.driver_id != driver_id:
            return jsonify({"error": "not found"}), 404
        pts = (db.query(DriverLocation)
               .filter(DriverLocation.shift_id == shift_id)
               .order_by(DriverLocation.captured_at.asc())
               .all())
        return jsonify({
            "shift_id": shift_id,
            "started_at": shift.started_at.isoformat() + "Z",
            "ended_at":   shift.ended_at.isoformat() + "Z" if shift.ended_at else None,
            "points": [
                {
                    "lat": p.lat, "lng": p.lng,
                    "order_id": p.order_id,
                    "captured_at": p.captured_at.isoformat() + "Z",
                    "accuracy_m": p.accuracy_m,
                    "speed_mps": p.speed_mps,
                }
                for p in pts
            ],
        })
    finally:
        db.close()


# ============================================================
# MANAGER + LEGAL placeholder routes (Sam #837 item 8 — sidebar
# restructure ships the entries before the features land).
# ============================================================
_MANAGER_PAGE_LABELS = {
    "daily-log":            "Daily Manager Log",
    "shift-handoff":        "Shift Handoff",
    "incident-reports":     "Incident Reports",
    "supply-requests":      "Supply Requests",
    "daily-goals":          "Daily Goals",
    "staff-feedback":       "Staff Feedback",
    "pre-shift-checklist":  "Pre-shift Checklist",
    "close-of-day-audit":   "Close-of-day Audit",
    "recipe-page":          "Recipe Page",
    "attendance":           "Attendance Tracking",
    "interview":            "Interview Surface",
    "training":             "Training Records",
    "maintenance":          "Maintenance Requests",
    "counseling":           "Employee Counseling",
}
_LEGAL_PAGE_LABELS = {
    "overview":   "Overview",
    "matters":    "Matters",
    "structure":  "Structure",
    "insurance":  "Insurance",
    "documents":  "Documents",
    "audit-log":  "Audit Log",
}


# ============================================================
# VENDORS — Recent Orders pages (Sam #837 items 9-12)
# ============================================================
_VENDOR_LABELS = {
    "webstaurant":      "Webstaurant",
    "performance-food": "Performance Food",
    "restaurant-depot": "Restaurant Depot",
    "specs":            "Specs",
}


@store_bp.route("/vendors/<vendor>/recent-orders", methods=["GET"])
def vendor_recent_orders(vendor: str):
    """Per-vendor Recent Orders list. Reads from VendorRecentOrder,
    filtered to this vendor + store scope. Until the parser for this
    vendor is fed sample emails by Sam, the table will render empty
    with a 'no orders yet' message — but the page itself is live so
    the navigation flow is intact.
    """
    label = _VENDOR_LABELS.get(vendor)
    if not label:
        abort(404)
    from app.models import VendorRecentOrder
    db = next(get_db())
    try:
        q = db.query(VendorRecentOrder).filter(VendorRecentOrder.vendor == vendor)
        if g.current_location in ("tomball", "copperfield"):
            q = q.filter(
                (VendorRecentOrder.store_scope == g.current_location) |
                (VendorRecentOrder.store_scope.is_(None))
            )
        rows = q.order_by(VendorRecentOrder.placed_at.desc().nullslast(),
                          VendorRecentOrder.created_at.desc()).limit(100).all()
        # Strip the reserved {"_meta": {...}} order-level entry from items_json so the
        # template's item loop (+ the data-items JS) only iterate real line items. samai's
        # d2e6416 stores order-level extras as a _meta entry INSIDE items_json; it has no
        # subtotal_cents, so the Jinja arithmetic (it.subtotal_cents / 100.0) hit Undefined
        # on it -> 500 on every tab with real orders (Sam #2762). In-memory only (no commit).
        for _r in rows:
            if isinstance(_r.items_json, list):
                _r.items_json = [it for it in _r.items_json
                                 if not (isinstance(it, dict) and "_meta" in it)]
        active_key = "vendor_recent_" + vendor.replace("-", "_")
        return render_template(
            "vendor_recent_orders.html",
            vendor_slug=vendor,
            vendor_label=label,
            vendor_chips=_VENDOR_LABELS,
            orders=rows,
            active=active_key,
        )
    finally:
        db.close()


# Slug → SQLAlchemy model class lookup. All 14 share ManagerLogMixin
# (see app/models.py). Sam #1102 + cena #1111 — approach A text-heavy
# v1, no per-page schema variations in this wave.
def _manager_model_for_slug(slug: str):
    from app.models import (
        DailyManagerLog, ShiftHandoff, IncidentReport, SupplyRequest,
        DailyGoals, StaffFeedback, PreShiftChecklist, CloseOfDayAudit,
        RecipePage, AttendanceTracking, InterviewSurface, TrainingRecord,
        MaintenanceRequest, EmployeeCounseling,
    )
    return {
        "daily-log":            DailyManagerLog,
        "shift-handoff":        ShiftHandoff,
        "incident-reports":     IncidentReport,
        "supply-requests":      SupplyRequest,
        "daily-goals":          DailyGoals,
        "staff-feedback":       StaffFeedback,
        "pre-shift-checklist":  PreShiftChecklist,
        "close-of-day-audit":   CloseOfDayAudit,
        "recipe-page":          RecipePage,
        "attendance":           AttendanceTracking,
        "interview":            InterviewSurface,
        "training":             TrainingRecord,
        "maintenance":          MaintenanceRequest,
        "counseling":           EmployeeCounseling,
    }.get(slug)


def _manager_dashboard_ok():
    return has_dashboard_access("dash.manager")


def _kitchen_dashboard_ok():
    return has_dashboard_access("dash.kitchen")


def _operations_full_access_ok():
    return has_dashboard_access("dash.operations") and not current_role_is("expo")


# ---- Daily Manager Log v3 (dck build-order #2, 2026-05-19) ----------
# The daily-log page diverged from the shared manager_log shape into a
# 12-day windowed, day-grouped view with structured entry fields +
# image attachments. These helpers + the image route serve it.

def _daily_log_image_dir():
    """Directory holding Daily Manager Log entry images — one subdir per
    entry id. Mirrors the dev-chat attachment storage convention."""
    base = os.getenv("DAILY_LOG_IMAGE_DIR")
    if not base:
        base = os.path.join(
            os.getenv("CHAT_ATTACHMENTS_DIR", "/var/data/chat-attachments"),
            "daily-log-images",
        )
    return base


_DAILY_LOG_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_DAILY_LOG_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _central_dt(dt):
    """A UTC-naive datetime -> America/Chicago naive, for display. The
    manager logs stamp created_at with datetime.utcnow(); rendering that
    raw read 5h ahead of Houston time (an 8:46 PM entry showed 1:46 AM)."""
    if dt is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return (dt.replace(tzinfo=ZoneInfo("UTC"))
                  .astimezone(ZoneInfo("America/Chicago"))
                  .replace(tzinfo=None))
    except Exception:
        return dt - timedelta(hours=5)


def _render_daily_log_v3(db, label, active_key):
    """Daily Manager Log v3 — a 12-day windowed, day-grouped view.
    ?date=<iso> sets the window end (default today)."""
    from app.models import DailyManagerLog
    raw = request.args.get("date") or ""
    try:
        sel = date.fromisoformat(raw) if raw else _local_today()
    except ValueError:
        sel = _local_today()
    today = _local_today()
    window = [sel - timedelta(days=i) for i in range(12)]  # reverse-chrono
    win_lo = window[-1]
    q = db.query(DailyManagerLog).filter(
        DailyManagerLog.entry_date >= win_lo,
        DailyManagerLog.entry_date <= sel,
    )
    if g.current_location in ("tomball", "copperfield"):
        q = q.filter(
            (DailyManagerLog.store_scope == g.current_location) |
            (DailyManagerLog.store_scope.is_(None))
        )
    rows = q.order_by(DailyManagerLog.created_at.desc()).all()
    by_date: dict = {}
    for r in rows:
        r.local_created = _central_dt(r.created_at)
        by_date.setdefault(r.entry_date, []).append(r)
    days = []
    for d in window:
        days.append({
            "iso": d.isoformat(),
            "dow": d.strftime("%a").upper(),
            "label": f"{d:%b} {d.day}",
            "is_today": d == today,
            "entries": by_date.get(d, []),
        })
    return render_template(
        "manager_daily_log.html",
        page_slug="daily-log",
        page_label=label,
        days=days,
        selected_date=sel.isoformat(),
        today_iso=today.isoformat(),
        active=active_key,
    )


def _create_daily_log_v3_entry(db, store_scope, user):
    """Create a Daily Manager Log v3 entry from the multipart form, and
    save any uploaded images as DailyLogEntryImage rows + files."""
    from app.models import DailyManagerLog, DailyLogEntryImage
    raw_date = request.form.get("entry_date") or ""
    try:
        entry_date = date.fromisoformat(raw_date) if raw_date else _local_today()
    except ValueError:
        entry_date = _local_today()

    def _field(name, default, cap):
        return ((request.form.get(name) or default).strip()[:cap] or default)

    row = DailyManagerLog(
        body=(request.form.get("body") or "").strip() or None,
        module=_field("module", "general", 20),
        subject=_field("subject", "general", 24),
        issue=_field("issue", "general", 16),
        priority=_field("priority", "low", 10),
        entry_date=entry_date,
        show_on_roster=(request.form.get("show_on_roster") == "1"),
        store_scope=store_scope,
        author_id=(user.id if user else None),
    )
    db.add(row)
    db.flush()  # need row.id for the image subdir + FK

    files = [f for f in request.files.getlist("images") if f and f.filename]
    if files:
        entry_dir = os.path.join(_daily_log_image_dir(), str(row.id))
        os.makedirs(entry_dir, exist_ok=True)
        pos = 0
        for f in files:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in _DAILY_LOG_IMAGE_EXTS:
                continue
            data = f.read()
            if not data or len(data) > _DAILY_LOG_MAX_IMAGE_BYTES:
                continue
            safe = secure_filename(f.filename) or f"image{ext}"
            target = os.path.join(entry_dir, f"{pos}_{safe}")
            with open(target, "wb") as fh:
                fh.write(data)
            db.add(DailyLogEntryImage(
                entry_id=row.id, storage_path=target, position=pos))
            pos += 1
    db.commit()


@store_bp.route("/manager/daily-log/image/<int:image_id>", methods=["GET"])
def daily_log_image(image_id: int):
    """Serve a Daily Manager Log entry image. Manager-tier gated."""
    if not _manager_dashboard_ok():
        abort(403)
    from app.models import DailyLogEntryImage
    db = next(get_db())
    try:
        img = db.get(DailyLogEntryImage, image_id)
        if img is None or not os.path.exists(img.storage_path):
            abort(404)
        return send_file(img.storage_path)
    finally:
        db.close()


# ============================================================
# INCIDENT REPORTS v3 — ck build-order Sam #10:11 + #10:15
# (2026-05-19). Replaces the v1 shared text-heavy shell for
# /dos/manager/incident-reports with the samai #6:27 + dck
# #6:39 v3 design (dashboard + filter strip + severity-coded
# cards). Schema additions per migration 33.
# ============================================================
def _render_incident_reports_v3(db, label, active_key, form_mode=None):
    from app.models import IncidentReport
    from datetime import timedelta, date as _date

    # New-entry form: render the v4 standalone "File new incident" page
    # (Sam dev chat #4:22 + #4:23 spec; ck build #4:32). Standalone — does
    # not extend base_dashboard. Form posts back to manager_page_create.
    if form_mode == "new":
        user = getattr(g, "current_user", None)
        today = _local_today()
        # Preview report-id: counts today's rows so the draft pill mirrors
        # what the server will assign on actual submit. May skew by 1 if a
        # peer files between preview and submit — harmless, the persisted
        # row carries the actual id.
        today_count = db.query(IncidentReport).filter(
            IncidentReport.created_at >= datetime.combine(today, datetime.min.time())
        ).count()
        preview_report_id = f"IR-{today.strftime('%Y-%m%d')}-{today_count + 1:03d}"
        try:
            from zoneinfo import ZoneInfo
            now_local = datetime.now(ZoneInfo("America/Chicago"))
        except Exception:  # pragma: no cover — zoneinfo missing on older runtimes
            now_local = datetime.utcnow()
        store_label_map = {"tomball": "UNO Tomball",
                           "copperfield": "UNO Copperfield"}
        store_label = store_label_map.get(g.current_location, "Cenas Kitchen")
        full_name = (getattr(user, "full_name", None)
                     or getattr(user, "name", None)
                     or "Unknown user")
        initials = "".join(p[:1].upper()
                           for p in (full_name.split() or ["?"]))[:2] or "?"
        role_label = (getattr(user, "role", None) or "Manager").replace("_", " ").title()
        # Cross-platform date/time formatting (Windows strftime doesn't
        # support %-d / %-I leading-zero strip).
        _h12 = ((now_local.hour - 1) % 12) + 1
        _ampm = "AM" if now_local.hour < 12 else "PM"
        _tz = getattr(now_local, "tzname", lambda: None)() or "CDT"
        date_label = now_local.strftime("%a, %b ") + f"{now_local.day}, {now_local.year}"
        time_label = f"{_h12}:{now_local.minute:02d} {_ampm} {_tz}"
        return render_template(
            "manager_incident_report_new.html",
            page_slug="incident-reports",
            page_label=label,
            active=active_key,
            user_full_name=full_name,
            user_initials=initials,
            user_role_label=role_label,
            store_label=store_label,
            now_date_label=date_label,
            now_time_label=time_label,
            today_iso=today.strftime("%Y-%m-%d"),
            report_id=preview_report_id,
            submit_url=url_for("store.manager_page_create", page="incident-reports"),
            list_url=url_for("store.manager_page_list", page="incident-reports"),
        )

    now = datetime.utcnow()
    cutoff_30 = now - timedelta(days=30)

    def _store_scope_filter(query):
        if g.current_location in ("tomball", "copperfield"):
            return query.filter(
                (IncidentReport.store_scope == g.current_location) |
                (IncidentReport.store_scope.is_(None))
            )
        return query

    # Rolling 30-day window — active (non-archived) rows. This is the
    # default view and the basis for the dashboard stat cards.
    q_30 = _store_scope_filter(
        db.query(IncidentReport).filter(IncidentReport.archived_at.is_(None))
    ).filter(IncidentReport.created_at >= cutoff_30)

    active_filter = (request.args.get("f") or "all").strip()
    query_text = (request.args.get("q") or "").strip()
    just_filed = (request.args.get("just_filed") or "").strip()[:40] or None

    # Archive date-range search (Sam #5:40 — "search archive" must let
    # you pick a day range and pull every report in it). A from/to date
    # or a text query switches to archive mode: the rolling-30-day window
    # is dropped and the FULL table is searched (archived rows included).
    # created_at is the range axis since it is always populated.
    def _parse_date_arg(s):
        s = (s or "").strip()
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None

    date_from = _parse_date_arg(request.args.get("from"))
    date_to = _parse_date_arg(request.args.get("to"))
    archive_mode = bool(date_from or date_to or query_text)

    # Stats always reflect the rolling 30-day window so the dashboard
    # cards stay stable regardless of any archive search.
    rows_30 = q_30.order_by(IncidentReport.created_at.desc()).limit(200).all()
    stats = {
        "open":     sum(1 for r in rows_30 if (r.status or "open") == "open"),
        "review":   sum(1 for r in rows_30 if (r.status or "") == "review"),
        "resolved": sum(1 for r in rows_30 if (r.status or "") == "resolved"),
        "last_30":  len(rows_30),
    }

    if archive_mode:
        aq = _store_scope_filter(db.query(IncidentReport))
        if date_from:
            aq = aq.filter(IncidentReport.created_at >= datetime.combine(
                date_from, datetime.min.time()))
        if date_to:
            # Inclusive of the whole 'to' day → < midnight of the next day.
            aq = aq.filter(IncidentReport.created_at < datetime.combine(
                date_to + timedelta(days=1), datetime.min.time()))
        if query_text:
            like = f"%{query_text}%"
            aq = aq.filter(
                IncidentReport.title.ilike(like) |
                IncidentReport.body.ilike(like) |
                IncidentReport.report_id.ilike(like) |
                IncidentReport.location_in_store.ilike(like) |
                IncidentReport.people_involved.ilike(like)
            )
        rows = aq.order_by(IncidentReport.created_at.desc()).limit(500).all()
    else:
        rows = rows_30

    return render_template(
        "manager_incident_reports.html",
        page_slug="incident-reports",
        page_label=label,
        entries=rows,
        entry=None,
        form_mode=form_mode,
        stats=stats,
        active_filter=active_filter,
        query=query_text,
        just_filed=just_filed,
        archive_mode=archive_mode,
        date_from=(date_from.strftime("%Y-%m-%d") if date_from else ""),
        date_to=(date_to.strftime("%Y-%m-%d") if date_to else ""),
        result_count=len(rows),
        active=active_key,
    )


def _create_incident_v3_entry(db, store_scope, user):
    """Create an IncidentReport with the v3 + v4 fields populated.

    v3 (2026-05-19): severity / status / incident_type / report_id.
    v4 (2026-05-20, Sam #4:22 + #4:23 spec; ck build #4:32): the rich
    new-incident form — date_of_incident / time_of_incident /
    location_in_store / people_involved / witnesses / description (body) /
    immediate_action, plus lock-on-submit (locked + locked_at). form_action
    distinguishes a full submit (locks the row) from a Save-draft (status
    stays 'open' but no lock).

    Returns the created row so the caller can redirect with the
    just_filed query param.
    """
    from app.models import IncidentReport
    from datetime import date as _date, datetime as _dt

    today = _local_today()
    today_count = db.query(IncidentReport).filter(
        IncidentReport.created_at >= datetime.combine(today, datetime.min.time())
    ).count()
    report_id = f"IR-{today.strftime('%Y-%m%d')}-{today_count + 1:03d}"

    # Severity: single-select but now allowed to be empty (Sam #5:08 made
    # the severity grid click-to-deselect-able). Persist NULL when blank
    # rather than silently substituting "moderate".
    sev_raw = (request.form.get("severity") or "").strip()[:20]
    sev = sev_raw if sev_raw in ("critical", "serious", "moderate", "minor") else "moderate"

    # Incident type: comma-separated since Sam #5:08 made the grid
    # multi-select ("should allow you to pick all 8 if they wanted").
    # Parse, validate each token against the known set, drop duplicates
    # while preserving click order, then rejoin.
    _VALID_TYPES = {"injury", "equipment", "food-safety", "customer",
                    "staff", "security", "vendor", "property"}
    raw_types = (request.form.get("incident_type") or "")
    seen = set()
    valid_types = []
    for t in raw_types.split(","):
        t = t.strip().lower()
        if t in _VALID_TYPES and t not in seen:
            seen.add(t)
            valid_types.append(t)
    inc_type = (",".join(valid_types))[:200] or None

    # v4 fields. Description -> body (kept on ManagerLogMixin); explicit
    # immediate_action is its own column. Date / time arrive as the HTML5
    # input strings "YYYY-MM-DD" and "HH:MM"; we parse defensively so a
    # blank or malformed value just stores NULL instead of 500-ing.
    def _parse_date(s: str):
        s = (s or "").strip()
        if not s:
            return None
        try:
            return _dt.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None

    def _parse_time(s: str):
        s = (s or "").strip()
        if not s:
            return None
        try:
            return _dt.strptime(s, "%H:%M").time()
        except ValueError:
            try:
                return _dt.strptime(s, "%H:%M:%S").time()
            except ValueError:
                return None

    description = (request.form.get("description") or "").strip()
    immediate_action = (request.form.get("immediate_action") or "").strip()
    location_in_store = (request.form.get("location_in_store") or "").strip()[:200]
    people_involved = (request.form.get("people_involved") or "").strip()[:500]
    witnesses = (request.form.get("witnesses") or "").strip()[:500]
    form_action = (request.form.get("form_action") or "submit").strip().lower()

    # Title surfaces in the list view: synthesize from incident_type +
    # severity + location when the form (v4) doesn't include a 'title'
    # input. Falls back to "Incident report" if everything is blank.
    title_parts = []
    if inc_type:
        title_parts.append(inc_type.replace("_", " ").title())
    if location_in_store:
        title_parts.append(location_in_store[:60])
    title = " — ".join(title_parts) if title_parts else "Incident report"

    is_locked = (form_action == "submit")
    locked_at = _dt.utcnow() if is_locked else None

    row = IncidentReport(
        title=title[:300],
        body=description or None,
        severity=sev,
        status="locked" if is_locked else "open",
        incident_type=inc_type,
        report_id=report_id,
        store_scope=store_scope,
        author_id=(user.id if user else None),
        date_of_incident=_parse_date(request.form.get("date_of_incident")),
        time_of_incident=_parse_time(request.form.get("time_of_incident")),
        location_in_store=location_in_store or None,
        people_involved=people_involved or None,
        witnesses=witnesses or None,
        immediate_action=immediate_action or None,
        locked=is_locked,
        locked_at=locked_at,
    )
    db.add(row)
    db.commit()
    return row


# ============================================================
# Attendance Tracking v3 — manager-operated daily time clock
# (dck, Sam #10:14). A per-employee-per-day roster board, not a
# log-entry list — diverged from the shared manager_log shape.
# ============================================================
_ATTN_TZ = "America/Chicago"      # both Cenas stores are in the Houston, TX area
_ATTN_LATE_GRACE_MIN = 5          # minutes past scheduled start before "late"


def _attn_now():
    """Store-local 'now' — clock punches + late-math must use the same
    clock the manager enters scheduled times in, regardless of the
    server tz. aick: if the app has a canonical local-time helper,
    swap it in here."""
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        return _dt.now(ZoneInfo(_ATTN_TZ)).replace(tzinfo=None)
    except Exception:
        return _dt.now()


def _attn_fmt(dt):
    """'10:14 AM' from a datetime, or None."""
    return dt.strftime("%I:%M %p").lstrip("0") if dt else None


def _attn_parse_time(raw, on_date):
    """'HH:MM' (from an <input type=time>) + a date -> naive datetime."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        hh, mm = raw.split(":")[:2]
        return datetime(on_date.year, on_date.month, on_date.day, int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def _attn_initials(name):
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _attn_hours(start, end):
    """'8h 2m' / '9h' between two datetimes, or None."""
    if not start or not end or end <= start:
        return None
    mins = int((end - start).total_seconds() // 60)
    h, m = mins // 60, mins % 60
    return f"{h}h" if m == 0 else f"{h}h {m}m"


# Employee Counseling v3 — per-employee counseling board
# (Sam #counseling render). Mirrors the Attendance board: lists
# every employee on the Toast roster; clicking one logs a
# counseling entry against them; each row surfaces whether the
# employee has prior counseling on file + the count.
#
# Storage: the shared EmployeeCounseling table (ManagerLogMixin)
# — no migration. One row per counseling entry, mapped onto the
# existing columns:
#   title    -> employee name
#   type_tag -> counseling level (verbal|coaching|written|final|positive)
#   body     -> structured detail block: "Category: ...\n\n<what
#               happened>\n\nExpected: ...\nResponse: ...\nFollow-up: ..."
#   store_scope / author_id / created_at -> as on every manager log.
# ============================================================
# Counseling levels the form offers, in escalation order. The first
# token is the canonical type_tag value persisted on the row.
_COUNSEL_LEVELS = ("verbal", "coaching", "written", "final", "positive")
_COUNSEL_LEVEL_LABELS = {
    "verbal": "Verbal", "coaching": "Coaching", "written": "Written",
    "final": "Final notice", "positive": "Recognition",
}
# Disciplinary levels (everything but recognition) count as an "issue"
# when deciding whether an employee has prior issues on file.
_COUNSEL_ISSUE_LEVELS = {"verbal", "coaching", "written", "final"}


def _counsel_level_norm(raw):
    """Map a stored type_tag to a canonical counseling level. Tolerates
    legacy / free-text tags so old rows still render under a level."""
    t = (raw or "").strip().lower()
    if t in _COUNSEL_LEVELS:
        return t
    if t in ("coach", "coaching conversation"):
        return "coaching"
    if t in ("final-notice", "final notice", "final written"):
        return "final"
    if t in ("recognition", "positive note", "praise"):
        return "positive"
    if t in ("written warning", "write-up"):
        return "written"
    if t in ("verbal warning", "spoken"):
        return "verbal"
    return "verbal"


def _counsel_body(category, what, expected, response, followup):
    """Compose the structured body block stored on the EmployeeCounseling
    row. Kept human-readable so the detail view (which reads `body`
    straight back) shows a clean record."""
    chunks = []
    if category:
        chunks.append(f"Category: {category}")
    if what:
        chunks.append(what)
    tail = []
    if expected:
        tail.append(f"Expected improvement: {expected}")
    if response:
        tail.append(f"Employee response: {response}")
    if followup:
        tail.append(f"Follow-up plan: {followup}")
    if tail:
        chunks.append("\n".join(tail))
    return "\n\n".join(chunks).strip()


# ============================================================
# Maintenance Requests v3 + Training Records v3 — dedicated
# renderers (Sam dev chat #9:16; samai build). Same pattern as
# _render_employee_counseling_v3: a rich v3 page off the shared
# ManagerLogMixin shape (title + type_tag + body) — NO migration.
# Structured detail is packed into `body` as leading "Key: value"
# header lines; _kv_body parses them and degrades gracefully so
# plain pre-v3 rows still render.
# ============================================================
import re as _re_mt

_MAINT_PRIORITY = {"urgent": "URGENT", "high": "HIGH",
                   "medium": "MEDIUM", "low": "LOW"}
_MAINT_PRIORITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
# status key -> (display label, status-pill class)
_MAINT_STATUS = {
    "open":        ("Open",          "alert"),
    "in_progress": ("In progress",   "warn"),
    "scheduled":   ("Scheduled",     "warn"),
    "parts":       ("Parts ordered", "warn"),
    "quoted":      ("Quoted",        ""),
    "closed":      ("Closed",        "ok"),
}
_KV_RE = _re_mt.compile(r"^([A-Za-z][A-Za-z /]{0,21}):\s+(.+)$")


def _kv_body(body):
    """(fields, description) from a manager-log body. Leading
    'Key: value' lines (key = letters/spaces/slash, <=22 chars)
    are collected case-insensitively; the first line that is not
    such a pair starts the free-text description. A plain body
    with no header parses to ({}, body) so pre-v3 rows render."""
    fields, desc, header = {}, [], True
    for ln in (body or "").replace("\r\n", "\n").split("\n"):
        m = _KV_RE.match(ln) if header else None
        if m:
            fields[m.group(1).strip().lower()] = m.group(2).strip()
        else:
            if header and not ln.strip():
                continue  # blank line(s) between header and description
            header = False
            desc.append(ln)
    return fields, "\n".join(desc).strip()


def _loose_date(s):
    """Tolerant date parse -> date or None. Accepts ISO, 'YYYY-MM',
    'Mon YYYY', 'Mon DD, YYYY', 'MM/DD/YYYY'."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%b %d, %Y", "%B %d, %Y",
                "%b %Y", "%B %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _store_uno_dos(scope):
    """ManagerLogMixin.store_scope -> (filter key, display label).
    Sam's #9:16 reference labels: UNO = Copperfield, DOS = Tomball."""
    if scope == "copperfield":
        return "uno", "UNO Copperfield"
    if scope == "tomball":
        return "dos", "DOS Tomball"
    return "", "Both stores"


def _time_since(dt):
    if not dt:
        return ""
    secs = (datetime.utcnow() - dt).total_seconds()
    if secs < 3600:
        return "%dm ago" % max(1, int(secs // 60))
    if secs < 86400:
        return "%dh ago" % int(secs // 3600)
    return "%dd ago" % int(secs // 86400)


_WARRANTY_JSON = None
_WARRANTY_JSON_MTIME = 0.0
_WARRANTY_USER_JSON = None
_WARRANTY_USER_JSON_MTIME = 0.0

# Sam #1185/#1193/#1203 — user-editable fields on the warranties table.
# Stored as a sidecar JSON keyed by "<item_number>|<order_number>" so
# edits survive deploys (Render's disk persists docs/) and the
# WebstaurantStore-derived equipment_warranties.json can be refreshed
# without touching the user-input data.
_WARRANTY_USER_FIELDS = (
    "serial_number",
    "warranty_email",
    "warranty_claim",
    "claim_reason",
    "contact_email",
    "contact_phone",
)


def _warranty_user_path():
    from pathlib import Path as _P
    return _P(__file__).resolve().parents[2] / "docs" / "equipment_warranty_user.json"


def _load_warranty_user() -> dict:
    """Load + cache user-editable warranty fields keyed by
    "<item_number>|<order_number>". Reloads when the JSON file changes
    on disk."""
    import json
    global _WARRANTY_USER_JSON, _WARRANTY_USER_JSON_MTIME
    p = _warranty_user_path()
    if not p.exists():
        return {}
    mt = p.stat().st_mtime
    if _WARRANTY_USER_JSON is None or mt != _WARRANTY_USER_JSON_MTIME:
        try:
            _WARRANTY_USER_JSON = json.loads(p.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            _WARRANTY_USER_JSON = {}
        _WARRANTY_USER_JSON_MTIME = mt
    return _WARRANTY_USER_JSON


def _save_warranty_user(key: str, fields: dict) -> dict:
    """Merge + persist user fields for one warranty row. Atomic rename
    so a partial write can't corrupt the JSON. Returns the resulting
    row record."""
    import json
    global _WARRANTY_USER_JSON, _WARRANTY_USER_JSON_MTIME
    p = _warranty_user_path()
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            data = {}
    row = dict(data.get(key) or {})
    for f in _WARRANTY_USER_FIELDS:
        if f in fields:
            row[f] = (fields.get(f) or "").strip()
    data[key] = row
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    _WARRANTY_USER_JSON = data
    _WARRANTY_USER_JSON_MTIME = p.stat().st_mtime
    return row


def _load_warranties() -> list[dict]:
    """Load + cache the WebstaurantStore + email-derived warranty roster
    from docs/equipment_warranties.json (parsed via
    scripts/parse_warranty_list.py from Sam dev-chat #1129).
    Reloads if the JSON file changed on disk."""
    import json
    from datetime import datetime as _dt
    from pathlib import Path as _P
    global _WARRANTY_JSON, _WARRANTY_JSON_MTIME
    p = _P(__file__).resolve().parents[2] / "docs" / "equipment_warranties.json"
    if not p.exists():
        return []
    mt = p.stat().st_mtime
    if _WARRANTY_JSON is None or mt != _WARRANTY_JSON_MTIME:
        _WARRANTY_JSON = json.loads(p.read_text(encoding="utf-8"))
        _WARRANTY_JSON_MTIME = mt
    return _WARRANTY_JSON or []


# Sam #1428: equipment-type categories for the warranty page's "Type"
# column. Keyword-matched against the item title, MOST SPECIFIC FIRST
# (first match wins), so e.g. "Commercial Microwave Oven" -> Microwave
# (not Oven) and "Rice Cooker / Warmer" -> Rice Cooker (not Food Warmer).
_EQUIP_TYPE_RULES = [
    ("ice cream", "Ice Cream Machine"),
    ("soft serve", "Soft Serve Machine"),
    ("slushy", "Frozen Drink Machine"),
    ("granita", "Frozen Drink Machine"),
    ("frozen drink", "Frozen Drink Machine"),
    ("ice machine", "Ice Maker"),
    ("ice maker", "Ice Maker"),
    ("rice cooker", "Rice Cooker"),
    ("rice warmer", "Rice Cooker"),
    ("griddle", "Griddle"),
    ("charbroiler", "Grill"),
    ("char broiler", "Grill"),
    ("microwave", "Microwave"),
    ("range", "Range"),
    ("hot plate", "Range"),
    ("conveyor oven", "Oven"),
    ("impinger", "Oven"),
    ("convection oven", "Oven"),
    ("oven", "Oven"),
    ("fryer", "Fryer"),
    ("steam table", "Steam Table"),
    ("steamer", "Steamer"),
    ("tortilla", "Tortilla Press"),
    ("dough press", "Tortilla Press"),
    ("food processor", "Food Processor"),
    ("immersion blender", "Blender"),
    ("blender", "Blender"),
    ("milkshake", "Drink Mixer"),
    ("drink mixer", "Drink Mixer"),
    ("meat tenderizer", "Meat Tenderizer"),
    ("tenderizer", "Meat Tenderizer"),
    ("planetary", "Mixer"),
    ("mixer", "Mixer"),
    ("grinder", "Mixer"),
    ("slicer", "Slicer"),
    ("scale", "Scale"),
    ("iced tea", "Beverage"),
    ("tea brewer", "Beverage"),
    ("beverage dispenser", "Beverage"),
    ("coffee", "Beverage"),
    ("brewer", "Beverage"),
    ("warmer", "Food Warmer"),
    ("dump station", "Food Warmer"),
    ("holding cabinet", "Holding Cabinet"),
    ("proofer", "Holding Cabinet"),
    ("water heater", "Water Heater"),
    ("prep table", "Prep Table"),
    ("prep rail", "Prep Table"),
    ("sandwich", "Prep Table"),
    ("chef base", "Refrigeration"),
    ("back bar", "Refrigeration"),
    ("refrigerator", "Refrigeration"),
    ("refrigerated", "Refrigeration"),
    ("froster", "Refrigeration"),
    ("plate chiller", "Refrigeration"),
    ("chiller", "Refrigeration"),
    ("merchandiser", "Refrigeration"),
    ("freezer", "Freezer"),
    ("cooler", "Cooler"),
    ("reach-in", "Refrigeration"),
    ("ceiling fan", "Fan"),
    ("nvr", "Security"),
    ("dvr", "Security"),
    ("camera", "Security"),
    ("grill", "Grill"),
]


def _equip_type(title: str) -> str:
    """Best-effort equipment category for the warranty page Type column."""
    t = (title or "").lower()
    for kw, cat in _EQUIP_TYPE_RULES:
        if kw in t:
            return cat
    return "Other"


def _warranty_for_template() -> tuple[list[dict], dict]:
    """Sort + label warranties for the maintenance page render. Returns
    (rows, kpis). Rows carry: title, item_number, order_number, status
    ('active'|'expired'|'portal'), status_label, expiration_date,
    expires_in_days (int or None), portal_only, source. Sorted: expired
    last, then by soonest expiry first, then alphabetically."""
    from datetime import datetime as _dt
    raw = _load_warranties()
    user_all = _load_warranty_user()
    out: list[dict] = []
    today = _dt.utcnow().date()
    k = {"total": 0, "active": 0, "expired": 0, "portal": 0,
         "expiring_30d": 0, "expiring_90d": 0, "no_warranty": 0}
    for r in raw:
        title = r.get("title") or ""
        item_no = r.get("item_number") or ""
        order_no = r.get("order_number") or ""
        user_key = f"{item_no}|{order_no}"
        user = user_all.get(user_key) or {}
        raw_status = (r.get("status") or "").strip().lower()
        exp_str = r.get("expiration_date")
        # Safeware-portal rows carry no expiration in the WebstaurantStore
        # source; backfill from the declaration PDF (Sam #1329) so the
        # Expires column + expiring-soon highlight work for them too.
        if not exp_str and user.get("safeware_expiration"):
            exp_str = user["safeware_expiration"]
        portal_only = bool(r.get("portal_only"))
        exp_dt = None
        if exp_str:
            try:
                exp_dt = _dt.strptime(exp_str, "%m/%d/%Y").date()
            except Exception:
                exp_dt = None
        if portal_only or raw_status == "safeware warranty":
            status, label, cls = "portal", "Safeware Portal", ""
        elif raw_status == "expired" or (exp_dt and exp_dt < today):
            status, label, cls = "expired", "Expired", "alert"
        else:
            status, label, cls = "active", "Active", "ok"
        days_left = (exp_dt - today).days if exp_dt else None
        k["total"] += 1
        k[status] += 1
        if days_left is not None and 0 <= days_left <= 30:
            k["expiring_30d"] += 1
        if days_left is not None and 0 <= days_left <= 90:
            k["expiring_90d"] += 1
        out.append({
            "title": title,
            "item_number": item_no,
            "order_number": order_no,
            "status": status,
            "status_label": label,
            "status_class": cls,
            "expiration_date": exp_str,
            "expires_in_days": days_left,
            "portal_only": portal_only,
            "source": r.get("source") or "",
            "user_key": user_key,
            "serial_number": user.get("serial_number", ""),
            "plan_number": user.get("plan_number", ""),
            "warranty_email": user.get("warranty_email", ""),
            "warranty_claim": user.get("warranty_claim", ""),
            "claim_reason": user.get("claim_reason", ""),
            "contact_email": user.get("contact_email", ""),
            "contact_phone": user.get("contact_phone", ""),
            "search": " ".join(x for x in [
                title.lower(), item_no.lower(), order_no.lower(),
                (user.get("serial_number") or "").lower(),
                (user.get("warranty_email") or "").lower(),
                (user.get("contact_email") or "").lower()] if x),
        })

    # Sam #1374: equipment photographed (serial tags) but NOT on the
    # Safeware/WebstaurantStore warranty list — list it anyway with
    # status "no warranty" so the full equipment inventory + serials
    # live in one place. Source: docs/equipment_extra.json.
    import json as _json
    from pathlib import Path as _P2
    extra_path = _P2(__file__).resolve().parents[2] / "docs" / "equipment_extra.json"
    if extra_path.exists():
        try:
            extra_items = _json.loads(extra_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            extra_items = []
        for e in extra_items or []:
            model = e.get("model") or ""
            base_serial = e.get("serial_number") or ""
            ekey = f"extra|{model}|{base_serial}"
            eu = user_all.get(ekey) or {}
            k["total"] += 1
            k["no_warranty"] += 1
            out.append({
                "title": e.get("title") or "(equipment)",
                "item_number": model,
                "order_number": "",
                "status": "no_warranty",
                "status_label": "No Warranty",
                "status_class": "none",
                "expiration_date": None,
                "expires_in_days": None,
                "portal_only": False,
                "source": e.get("source") or "photo serial tag",
                "user_key": ekey,
                "serial_number": eu.get("serial_number") or base_serial,
                "plan_number": eu.get("plan_number", ""),
                "warranty_email": eu.get("warranty_email", ""),
                "warranty_claim": eu.get("warranty_claim", ""),
                "claim_reason": eu.get("claim_reason", ""),
                "contact_email": eu.get("contact_email", ""),
                "contact_phone": eu.get("contact_phone", ""),
                "search": " ".join(x for x in [
                    (e.get("title") or "").lower(), model.lower(),
                    base_serial.lower()] if x),
            })

    # Sam #1428: tag each row with an equipment Type category.
    for row in out:
        row["type"] = _equip_type(row.get("title") or "")
        row["search"] = (row.get("search", "") + " " + row["type"].lower()).strip()

    # Sam #1428: one row per physical unit. When an order's serial field
    # holds several comma-joined serials (a multi-unit purchase), render
    # one row per serial instead of cramming them on a single line. This
    # also collapses the roster's redundant duplicate lines for the same
    # order (WebstaurantStore lists one line per unit, but every line
    # carries the same comma-joined serial blob). Rows are grouped by the
    # order's user_key; exploded rows share that key, so their serial is
    # shown read-only (editing a shared key would clobber sibling units);
    # the per-order fields (warranty email, claim, contact) stay editable.
    from collections import OrderedDict as _OD
    groups = _OD()
    for row in out:
        groups.setdefault(row.get("user_key", ""), []).append(row)
    regrouped: list[dict] = []
    for _key, members in groups.items():
        base = members[0]
        serials = [s.strip() for s in (base.get("serial_number") or "").split(",") if s.strip()]
        plans = [p.strip() for p in (base.get("plan_number") or "").split(",") if p.strip()]
        if len(serials) > 1:
            # one row per serial; never drop units if the roster happened
            # to list more lines than we have serials for.
            n_units = max(len(serials), len(members))
            for i in range(n_units):
                nr = dict(base)
                nr["serial_number"] = serials[i] if i < len(serials) else ""
                if i < len(plans):
                    nr["plan_number"] = plans[i]
                elif len(plans) <= 1:
                    nr["plan_number"] = base.get("plan_number", "")
                else:
                    nr["plan_number"] = ""
                nr["exploded"] = True
                nr["unit_index"] = i + 1
                nr["unit_count"] = n_units
                regrouped.append(nr)
        else:
            # 0 or 1 serial: leave the order's row(s) untouched (serial
            # stays editable, duplicate roster lines preserved as-is).
            for m in members:
                m["exploded"] = False
                regrouped.append(m)
    out = regrouped

    # Sam #1428: organize by Type (A-Z) then equipment name (A-Z),
    # keeping a single order's exploded units in serial order.
    out.sort(key=lambda x: (x.get("type", "").lower(),
                            (x.get("title") or "").lower(),
                            x.get("unit_index", 0)))
    return out, k


@store_bp.route("/manager/maintenance/warranty-detail/save",
                methods=["POST"])
def manager_warranty_detail_save():
    """Persist user-edited warranty fields (serial #, warranty email,
    warranty claim, claim reason, contact email/phone) for one row of
    the Equipment Warranties table. Keyed by "<item_number>|<order_number>"
    composite so duplicate webstaurantstore rows share user data (Sam
    #1185/#1193/#1203). JSON body or form-encoded both accepted.
    Returns the merged row record."""
    from flask import jsonify, request
    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form
    key = (payload.get("key") or "").strip()
    if not key or "|" not in key:
        return jsonify({"ok": False,
                        "error": "missing or invalid key"}), 400
    fields = {f: (payload.get(f) or "") for f in _WARRANTY_USER_FIELDS}
    saved = _save_warranty_user(key, fields)
    return jsonify({"ok": True, "key": key, "row": saved})


def _render_maintenance_v3(db, label, active_key):
    """Maintenance Requests v3 — equipment/facility repair board."""
    from app.models import MaintenanceRequest
    q = db.query(MaintenanceRequest)
    if g.current_location in ("tomball", "copperfield"):
        q = q.filter(
            (MaintenanceRequest.store_scope == g.current_location) |
            (MaintenanceRequest.store_scope.is_(None)))
    raw = q.order_by(MaintenanceRequest.created_at.desc()).limit(300).all()

    now = datetime.utcnow()
    requests = []
    kpis = {"open": 0, "urgent": 0, "in_progress": 0,
            "scheduled": 0, "closed_30d": 0}
    for r in raw:
        fields, desc = _kv_body(r.body)
        prio = (r.type_tag or fields.get("priority") or "medium").strip().lower()
        if prio not in _MAINT_PRIORITY:
            prio = "medium"
        status = (fields.get("status") or "open").strip().lower().replace(" ", "_")
        if status in ("parts_ordered", "ordered"):
            status = "parts"
        st_label, st_class = _MAINT_STATUS.get(status, ("Open", "alert"))
        store, store_label = _store_uno_dos(r.store_scope)
        local = _central_dt(r.created_at)
        area = fields.get("area") or fields.get("location") or ""
        reported = local.strftime("%b %d") if local else ""
        sub_bits = [b for b in (store_label or None, area or None,
                    ("reported " + reported) if reported else None) if b]
        meta_tags = []
        for key, prefix in (("reporter", "Reporter: "), ("vendor", "Vendor: "),
                            ("cost", "Cost: "), ("scheduled", "Scheduled: "),
                            ("photos", "")):
            if fields.get(key):
                meta_tags.append(prefix + fields[key])
        is_urgent = (prio == "urgent")
        age_days = int((now - r.created_at).total_seconds() // 86400) \
            if r.created_at else 0
        if status != "closed":
            kpis["open"] += 1
            if is_urgent:
                kpis["urgent"] += 1
        if status == "in_progress":
            kpis["in_progress"] += 1
        if status in ("scheduled", "parts"):
            kpis["scheduled"] += 1
        if status == "closed" and r.created_at and (now - r.created_at).days <= 30:
            kpis["closed_30d"] += 1
        requests.append({
            "id": r.id,
            "title": r.title or "(untitled request)",
            "priority": prio,
            "priority_label": _MAINT_PRIORITY[prio],
            "urgent": is_urgent,
            "sub": " · ".join(sub_bits),
            "desc": desc,
            "meta_tags": meta_tags,
            "status": status,
            "status_label": st_label,
            "status_class": st_class,
            "time_since": _time_since(r.created_at),
            "age_days": age_days,
            "store": store,
            "search": " ".join(x for x in [
                (r.title or "").lower(), desc.lower(),
                " ".join(meta_tags).lower(), store_label.lower()] if x),
            "detail_url": url_for("store.manager_page_detail",
                                  page="maintenance", entry_id=r.id),
        })
    # urgent first, closed last, then newest
    requests.sort(key=lambda x: (x["status"] == "closed",
                                 _MAINT_PRIORITY_ORDER.get(x["priority"], 2),
                                 -x["id"]))
    warranties, warranty_kpis = _warranty_for_template()
    return render_template(
        "maintenance_requests.html",
        page_slug="maintenance", page_label=label,
        requests=requests, kpis=kpis,
        warranties=warranties, warranty_kpis=warranty_kpis,
        new_url=url_for("store.manager_page_new", page="maintenance"),
        active=active_key,
    )


def _render_training_v3(db, label, active_key):
    """Training Records v3 — certification + renewal tracking."""
    from app.models import TrainingRecord
    q = db.query(TrainingRecord)
    if g.current_location in ("tomball", "copperfield"):
        q = q.filter(
            (TrainingRecord.store_scope == g.current_location) |
            (TrainingRecord.store_scope.is_(None)))
    raw = q.order_by(TrainingRecord.created_at.desc()).limit(400).all()

    user = getattr(g, "current_user", None)
    uid = getattr(user, "id", None)
    today = _local_today()
    now = datetime.utcnow()
    records = []
    kpis = {"total": 0, "current": 0, "expiring": 0, "overdue": 0}
    for r in raw:
        fields, desc = _kv_body(r.body)
        cert = (fields.get("certification") or fields.get("cert")
                or r.type_tag or "Certification").strip()
        issuer = fields.get("issuer") or fields.get("authority") or ""
        role = fields.get("role") or ""
        store, store_label = _store_uno_dos(r.store_scope)
        role_label = " · ".join(
            b for b in (role or None, store_label if store else None) if b
        ) or "Team member"
        issued = _loose_date(fields.get("issued"))
        expires = _loose_date(fields.get("expires"))
        issued_label = issued.strftime("%b %Y") if issued else "—"
        if expires:
            days = (expires - today).days
            if days < 0:
                status, st_label, st_class, row_class = (
                    "overdue", "Overdue · %d days" % abs(days),
                    "alert", "alert")
                expires_label = expires.strftime("%b %d, %Y")
            elif days <= 30:
                status, st_label, st_class, row_class = (
                    "expiring", "Expiring soon", "warn", "warn")
                expires_label = "%s · %d days" % (
                    expires.strftime("%b %Y"), days)
            else:
                status, st_label, st_class, row_class = (
                    "current", "Current", "ok", "")
                expires_label = expires.strftime("%b %Y")
        else:
            status, st_label, st_class, row_class = (
                "none", "No expiry", "", "")
            expires_label = "—"
        kpis["total"] += 1
        if status in kpis:
            kpis[status] += 1
        age_days = int((now - r.created_at).total_seconds() // 86400) \
            if r.created_at else 0
        records.append({
            "id": r.id,
            "employee": r.title or "(unnamed)",
            "role_label": role_label,
            "cert_name": cert,
            "cert_issuer": issuer or "—",
            "issued_label": issued_label,
            "expires_label": expires_label,
            "status": status, "status_label": st_label,
            "status_class": st_class, "row_class": row_class,
            "age_days": age_days,
            "mine": bool(uid) and r.author_id == uid,
            "_exp_sort": expires.toordinal() if expires else 10 ** 9,
            "search": " ".join(x for x in [
                (r.title or "").lower(), cert.lower(),
                issuer.lower(), role_label.lower()] if x),
            "detail_url": url_for("store.manager_page_detail",
                                  page="training", entry_id=r.id),
        })
    records.sort(key=lambda x: x["_exp_sort"])  # soonest-to-expire first
    for x in records:
        x.pop("_exp_sort", None)
    viewer_label = (getattr(user, "permission_level", None) or "Manager")
    viewer_label = viewer_label.replace("-", " ").replace("_", " ").title()
    return render_template(
        "training_records.html",
        page_slug="training", page_label=label,
        records=records, kpis=kpis, viewer_label=viewer_label,
        new_url=url_for("store.manager_page_new", page="training"),
        active=active_key,
    )


def _active_manager_roster(db, loc):
    """The ACTIVE Team Roster (the source of truth) for the manager boards
    — the same active-employee set the Operations Team page shows, via
    team_roster(). Returns [{employee_id, name, role, toast_name}] for active
    employees in scope, each mapped to their confirmed Toast name
    (CenaToastLink) so a board can overlay Toast clock-status. samai (Sam):
    Counseling + Attendance reflect THIS (active 93), not the raw ~113-row
    live Toast feed which includes inactive/terminated people."""
    from app.services.team_roster import team_roster
    from app.models import CenaToastLink
    scope = loc if loc in ("tomball", "copperfield") else "all"
    try:
        tr = team_roster(db, location=scope)
    except Exception:
        logging.getLogger(__name__).exception("active_manager_roster: team_roster failed")
        return []
    tn_by_eid = {}
    try:
        lq = db.query(CenaToastLink)
        if loc in ("tomball", "copperfield"):
            lq = lq.filter(CenaToastLink.store_key == loc)
        for lk in lq.all():
            if lk.toast_name and lk.cena_employee_id not in tn_by_eid:
                tn_by_eid[lk.cena_employee_id] = lk.toast_name
    except Exception:
        pass
    out, seen = [], set()
    for store in tr.get("stores", []):
        for e in store.get("employees", []):
            eid = e.get("id")
            if eid in seen:
                continue
            seen.add(eid)
            poss = e.get("positions") or []
            role = (poss[0]["name"] if poss else "") or e.get("domain") or ""
            out.append({
                "employee_id": eid,
                "name": (e.get("full_name") or "").strip(),
                "role": role,
                "toast_name": tn_by_eid.get(eid),
            })
    return out


def _render_employee_counseling_v3(db, label, active_key):
    """Employee Counseling v3 — per-employee board.

    Lists every employee (the Toast roster, same source the Attendance
    board uses) so a manager can click a person and log a counseling
    entry against them. Each roster row carries that employee's prior
    counseling history (matched by name on EmployeeCounseling.title) so
    the page shows who has had issues before, and the count.

    If Toast is unreachable the roster falls back to the distinct
    employee names already present in EmployeeCounseling — the same
    defensive pattern the Attendance board uses."""
    from app.models import EmployeeCounseling

    def _scoped(q):
        if g.current_location in ("tomball", "copperfield"):
            return q.filter(
                (EmployeeCounseling.store_scope == g.current_location) |
                (EmployeeCounseling.store_scope.is_(None)))
        return q

    # Every counseling row in scope, newest first — drives the per-
    # employee history and the dashboard KPIs.
    entries = _scoped(
        db.query(EmployeeCounseling)
    ).order_by(EmployeeCounseling.created_at.desc()).limit(500).all()

    # Group counseling history by employee name (the row's title).
    hist = {}
    for e in entries:
        nm = (e.title or "").strip()
        if not nm:
            continue
        hist.setdefault(nm, []).append(e)

    # Employee roster — every teammate Toast knows, name + role only
    # (clock status is irrelevant here). Falls back to the names already
    # carrying counseling history when Toast is down.
    # Roster = the ACTIVE Team Roster (the source of truth), NOT the raw
    # Toast feed. samai (Sam): Counseling reflects the Operations Team Roster
    # (active employees), so inactive/ex Toast-only people no longer appear.
    roster = {}              # display name (app full_name) -> role
    toast_name_by_name = {}  # display name -> confirmed Toast name (history match)
    counseling_notice = None
    for _emp in _active_manager_roster(db, g.current_location):
        nm = _emp["name"]
        if not nm:
            continue
        roster.setdefault(nm, _emp["role"] or "")
        if _emp.get("toast_name"):
            toast_name_by_name[nm] = _emp["toast_name"]

    def _people_row(rid, name, role):
        _tn = toast_name_by_name.get(name)
        ents = hist.get(name) or (hist.get(_tn, []) if _tn else []) or []
        levels = [_counsel_level_norm(e.type_tag) for e in ents]
        issue_count = sum(1 for lv in levels if lv in _COUNSEL_ISSUE_LEVELS)
        # Per-employee timeline, newest first — the slide-over history.
        records = []
        for e, lv in zip(ents, levels):
            records.append({
                "id": e.id,
                "level": lv,
                "level_label": _COUNSEL_LEVEL_LABELS.get(lv, "Verbal"),
                "date": (_central_dt(e.created_at).strftime("%b %d, %Y")
                         if e.created_at else ""),
                "reason": (e.body or "").strip().splitlines()[0]
                          if (e.body or "").strip() else "",
            })
        latest = records[0]["level"] if records else ""
        return {
            "rid": rid,
            "name": name,
            "initials": _attn_initials(name),
            "role": role or "Team member",
            "entry_count": len(ents),
            "issue_count": issue_count,
            "has_prior_issue": issue_count > 0,
            "has_recognition": any(lv == "positive" for lv in levels),
            "latest_level": latest,
            "records": records,
        }

    rows = []
    for rid, name in enumerate(sorted(roster, key=lambda n: n.lower())):
        rows.append(_people_row(rid, name, roster[name]))

    # Dashboard KPIs — counts by level across the in-scope history.
    def _lc(level):
        return sum(1 for e in entries
                   if _counsel_level_norm(e.type_tag) == level)
    kpis = {
        "total": len(entries),
        "verbal": _lc("verbal") + _lc("coaching"),
        "written": _lc("written"),
        "final": _lc("final"),
        "positive": _lc("positive"),
        "flagged": sum(1 for r in rows if r["has_prior_issue"]),
    }
    today = _local_today()
    return render_template(
        "employee_counseling.html",
        page_slug="counseling", page_label=label,
        rows=rows, kpis=kpis, counseling_notice=counseling_notice,
        today_iso=today.isoformat(), active=active_key,
    )


def _employee_counseling_v3_post(db, store_scope, user):
    """POST on the Employee Counseling page. Hidden form_action selects
    the op: add_entry logs a new counseling record for an employee.

    The record is written onto the shared EmployeeCounseling table
    (ManagerLogMixin) — title = employee name, type_tag = level,
    body = the structured detail block. No migration."""
    from app.models import EmployeeCounseling
    action = (request.form.get("form_action") or "add_entry").strip()
    uid = user.id if user else None

    if action == "add_entry":
        name = (request.form.get("name") or "").strip()[:300]
        if not name:
            return
        level = (request.form.get("level") or "").strip().lower()
        if level not in _COUNSEL_LEVELS:
            level = "verbal"
        category = (request.form.get("category") or "").strip()[:120]
        what = (request.form.get("what_happened") or "").strip()
        expected = (request.form.get("expected") or "").strip()
        response = (request.form.get("response") or "").strip()
        followup = (request.form.get("followup") or "").strip()
        body = _counsel_body(category, what, expected, response, followup)
        db.add(EmployeeCounseling(
            title=name,
            type_tag=level,
            body=(body or None),
            store_scope=store_scope,
            author_id=uid,
        ))
        db.commit()
        return


def _render_attendance_v3(db, label, active_key):
    """Attendance Tracking v3 — daily roster board. ?date=<iso> picks
    the day (default today).

    Roster + live clock status come from Toast: every teammate on the
    Toast employee roster is listed, and each one's clock-in/clock-out
    punches for the day decide On the Clock vs Off. Manager-logged
    events (late / no-show / callout / notes, stored as AttendanceShift
    + AttendanceEvent rows) are overlaid and win the status badge. If
    Toast is unreachable the board falls back to the manually-logged
    shifts so it never goes blank."""
    from app.models import AttendanceShift
    raw = request.args.get("date") or ""
    try:
        sel = date.fromisoformat(raw) if raw else _local_today()
    except ValueError:
        sel = _local_today()
    today = _local_today()
    loc = g.current_location

    def _scoped(q):
        if loc in ("tomball", "copperfield"):
            return q.filter((AttendanceShift.store_scope == loc) |
                            (AttendanceShift.store_scope.is_(None)))
        return q

    # Manager-logged shifts for the day, keyed by employee name.
    shifts = _scoped(
        db.query(AttendanceShift).filter(AttendanceShift.entry_date == sel)
    ).order_by(AttendanceShift.employee_name.asc()).all()
    shift_by_name = {}
    for s in shifts:
        shift_by_name.setdefault(s.employee_name, s)

    # 30-day attendance history, one query, grouped by employee name.
    win_lo = sel - timedelta(days=29)
    hist = {}
    for h in _scoped(db.query(AttendanceShift).filter(
            AttendanceShift.entry_date >= win_lo,
            AttendanceShift.entry_date <= sel)).all():
        hist.setdefault(h.employee_name, {})[h.entry_date] = h.status

    # Live clock-in / clock-out status from Toast (the same Toast API
    # behind the Labor reports). Falls back cleanly when Toast is down.
    toast_by_name = {}
    attendance_notice = None
    try:
        from app.services.toast_reports import attendance_clock_status
        tloc = loc if loc in ("tomball", "copperfield") else None
        for rec in attendance_clock_status(sel, location_filter=tloc):
            toast_by_name.setdefault(rec["name"], rec)
    except Exception as ex:
        logging.getLogger(__name__).warning(
            "attendance: Toast clock status unavailable: %s", ex)
        attendance_notice = ("Live Toast clock data is unavailable right now "
                             "- showing manually logged attendance only.")

    def _pattern(name):
        by_date = hist.get(name, {})
        cells, on_time, late_n, no_show_n = [], 0, 0, 0
        for i in range(30):
            d = win_lo + timedelta(days=i)
            st, state = by_date.get(d), ""
            if st in ("clocked-in", "break", "out"):
                state, on_time = "on-time", on_time + 1
            elif st == "late":
                state, late_n = "late", late_n + 1
            elif st == "no-show":
                state, no_show_n = "no-show", no_show_n + 1
            cells.append({"letter": d.strftime("%a")[0], "state": state,
                          "is_today": d == today})
        return cells, {"on_time": on_time, "late": late_n, "no_show": no_show_n}

    def _row(rid, name, shift, tz):
        """One roster row, merging the manager-logged shift (if any)
        with the live Toast clock record (if any) for `name`."""
        pattern, pcounts = _pattern(name)
        # Prior-day documented issues (strictly before the selected day)
        # drive the Issues view + the panel history.
        _ilabel = {"late": "Late", "no-show": "No-show", "callout": "Callout"}
        prior = []
        for d, st in sorted(hist.get(name, {}).items(), reverse=True):
            if d < sel and st in _ilabel:
                prior.append({"date": d.strftime("%b %d"), "kind": st,
                              "text": _ilabel[st]})

        manual_status = shift.status if shift else None
        toast_status = tz["status"] if tz else None

        # A logged manager judgment (late / no-show / callout / break)
        # wins; otherwise Toast's live clock truth; otherwise whatever
        # the manual shift says; otherwise off.
        if manual_status in ("late", "no-show", "callout", "break"):
            status = manual_status
        elif toast_status in ("clocked-in", "out"):
            status = toast_status
        elif manual_status:
            status = manual_status
        else:
            status = "off"

        # Clock punches: Toast is the source of truth; fall back to the
        # manually-entered shift's punches.
        if tz and (tz.get("clock_in") or tz.get("clock_out")):
            ci, co = tz.get("clock_in"), tz.get("clock_out")
        elif shift:
            ci, co = shift.clock_in, shift.clock_out
        else:
            ci, co = None, None

        ss = _attn_fmt(shift.scheduled_start) if shift else None
        se = _attn_fmt(shift.scheduled_end) if shift else None
        role = ((shift.role_title if shift else None)
                or (tz.get("job_title") if tz else None) or "Team member")
        section = (shift.section if shift else None) or "boh"
        events = ([{"time": _attn_fmt(e.at), "kind": e.kind, "text": e.text or ""}
                   for e in sorted(shift.events, key=lambda e: e.at, reverse=True)]
                  if shift else [])
        return {
            "rid": rid,
            "id": (shift.id if shift else None),
            "name": name,
            "initials": _attn_initials(name),
            "role": role,
            "section": section,
            "phone": (shift.phone if shift else None),
            "status": status,
            "sched": (f"{ss} - {se}" if (ss and se) else (ss or se or "")),
            "clock_in": _attn_fmt(ci),
            "clock_out": _attn_fmt(co),
            "late_minutes": ((shift.late_minutes or 0) if shift else 0),
            "hours_worked": _attn_hours(ci, co),
            "note": (shift.note if shift else None),
            "on_clock": status in ("clocked-in", "late", "break"),
            "is_off": status in ("scheduled", "no-show", "callout",
                                  "out", "off"),
            "has_prior_issue": len(prior) > 0,
            "history": prior,
            "events": events,
            "pattern": pattern,
            "pattern_counts": pcounts,
            "source": ("toast" if tz else "manual"),
        }

    # Roster = the ACTIVE Team Roster (the source of truth), NOT the raw
    # Toast feed. samai (Sam): Attendance reflects the Operations Team Roster
    # (active employees). Each active employee shows under their app name;
    # their Toast clock-status + any logged shift/30-day history is aliased
    # onto that app name via their confirmed Toast name so overlays match.
    active = _active_manager_roster(db, loc)
    role_by_name = {}
    if active:
        for _e in active:
            _an = _e["name"]
            _tn = _e.get("toast_name")
            if _e.get("role"):
                role_by_name[_an] = _e["role"]
            if _tn and _tn != _an:
                if _tn in toast_by_name:
                    toast_by_name.setdefault(_an, toast_by_name[_tn])
                if _tn in shift_by_name:
                    shift_by_name.setdefault(_an, shift_by_name[_tn])
                if _tn in hist:
                    hist.setdefault(_an, hist[_tn])
        names = sorted({_e["name"] for _e in active if _e["name"]},
                       key=lambda n: n.lower())
    else:
        # Defensive last-resort (active roster unavailable): the prior
        # Toast/shift/history union so the board never goes blank.
        names = sorted(set(toast_by_name) | set(shift_by_name) | set(hist),
                       key=lambda n: n.lower())

    rows = []
    for rid, name in enumerate(names):
        row = _row(rid, name, shift_by_name.get(name), toast_by_name.get(name))
        if name in role_by_name:
            row["role"] = role_by_name[name]
        rows.append(row)

    def _n(pred):
        return sum(1 for r in rows if pred(r))
    kpis = {
        "scheduled": _n(lambda r: r["status"] != "off"),
        "clocked_in": _n(lambda r: r["status"] in ("clocked-in", "late", "break")),
        "late": _n(lambda r: r["status"] == "late"),
        "no_show": _n(lambda r: r["status"] == "no-show"),
        "callouts": _n(lambda r: r["status"] == "callout"),
    }
    return render_template(
        "attendance_tracking.html",
        page_slug="attendance", page_label=label,
        rows=rows, kpis=kpis, attendance_notice=attendance_notice,
        selected_date=sel.isoformat(), today_iso=today.isoformat(),
        prev_date=(sel - timedelta(days=1)).isoformat(),
        next_date=(sel + timedelta(days=1)).isoformat(),
        date_display=f"{sel:%A, %B} {sel.day}, {sel.year}",
        is_today=(sel == today), active=active_key,
    )


def _attendance_v3_post(db, store_scope, user):
    """Every POST on the Attendance v3 page. Hidden form_action selects
    the op: add_shift | clock_in | clock_out | log_event."""
    from app.models import AttendanceShift, AttendanceEvent
    action = (request.form.get("form_action") or "").strip()
    uid = user.id if user else None

    if action == "add_shift":
        raw_date = request.form.get("entry_date") or ""
        try:
            d = date.fromisoformat(raw_date) if raw_date else _local_today()
        except ValueError:
            d = _local_today()
        name = (request.form.get("name") or "").strip()[:120]
        if not name:
            return
        section = (request.form.get("section") or "boh").strip().lower()
        if section not in ("boh", "foh"):
            section = "boh"
        db.add(AttendanceShift(
            entry_date=d, employee_name=name,
            role_title=((request.form.get("role") or "").strip()[:60] or None),
            section=section,
            phone=((request.form.get("phone") or "").strip()[:40] or None),
            scheduled_start=_attn_parse_time(request.form.get("sched_start"), d),
            scheduled_end=_attn_parse_time(request.form.get("sched_end"), d),
            status="scheduled", store_scope=store_scope, author_id=uid))
        db.commit()
        return

    if action == "add_entry":
        # Redesigned "Add Entry" flow (samai #5:03): name search + tag.
        # Finds or creates today's shift for the named teammate, then
        # logs the tagged attendance event. tag: late | ncns | ncl | switch.
        raw_date = request.form.get("entry_date") or ""
        try:
            d = date.fromisoformat(raw_date) if raw_date else _local_today()
        except ValueError:
            d = _local_today()
        name = (request.form.get("name") or "").strip()[:120]
        if not name:
            return
        tag = (request.form.get("kind") or "").strip().lower()
        reason = (request.form.get("reason") or "").strip()
        note = (request.form.get("note") or "").strip() or None
        shift = (db.query(AttendanceShift)
                 .filter(AttendanceShift.entry_date == d,
                         AttendanceShift.employee_name == name).first())
        if shift is None:
            shift = AttendanceShift(
                entry_date=d, employee_name=name, section="boh",
                status="scheduled", store_scope=store_scope, author_id=uid)
            db.add(shift)
            db.flush()
        _tagmap = {
            "late":   ("late",    "late",    "Late arrival"),
            "ncns":   ("no-show", "no-show", "No call / no show"),
            "ncl":    ("late",    "late",    "No call / late"),
            "switch": ("switch",  None,      "Switched shift without permission"),
        }
        ev_kind, new_status, base_text = _tagmap.get(tag, ("note", None, "Entry"))
        if new_status:
            shift.status = new_status
        text = base_text
        if reason and reason != base_text:
            text = f"{base_text} - {reason}"
        if note:
            text = f"{text} - {note}"
        db.add(AttendanceEvent(
            shift_id=shift.id, at=_attn_now(), kind=ev_kind, text=text,
            reason=(reason[:60] or None), counts_as_occurrence=True))
        shift.updated_at = _attn_now()
        db.commit()
        return

    # clock_in / clock_out / log_event target a shift. The redesigned
    # board posts against the selected teammate; when they have no
    # shift row for the day yet (a Toast-roster teammate not logged
    # before) we find-or-create one by name so the slide-over can log
    # an event against anyone on the board.
    try:
        shift_id = int(request.form.get("shift_id") or 0)
    except (TypeError, ValueError):
        shift_id = 0
    shift = db.get(AttendanceShift, shift_id) if shift_id else None
    if shift is None:
        name = (request.form.get("name") or "").strip()[:120]
        if not name:
            return
        raw_date = request.form.get("entry_date") or ""
        try:
            d = date.fromisoformat(raw_date) if raw_date else _local_today()
        except ValueError:
            d = _local_today()
        shift = (db.query(AttendanceShift)
                 .filter(AttendanceShift.entry_date == d,
                         AttendanceShift.employee_name == name).first())
        if shift is None:
            shift = AttendanceShift(
                entry_date=d, employee_name=name, section="boh",
                status="scheduled", store_scope=store_scope, author_id=uid)
            db.add(shift)
            db.flush()
    loc = g.current_location
    if loc in ("tomball", "copperfield") and shift.store_scope not in (loc, None):
        abort(403)
    now = _attn_now()

    if action == "clock_in":
        if shift.clock_in is None:
            shift.clock_in = now
            late = 0
            if shift.scheduled_start and now > shift.scheduled_start:
                late = int((now - shift.scheduled_start).total_seconds() // 60)
            if late > _ATTN_LATE_GRACE_MIN:
                shift.status, shift.late_minutes = "late", late
                db.add(AttendanceEvent(shift_id=shift.id, at=now, kind="late",
                                       text=f"Clocked in {late}m late"))
            else:
                shift.status, shift.late_minutes = "clocked-in", 0
                db.add(AttendanceEvent(shift_id=shift.id, at=now, kind="in",
                                       text="Clocked in"))

    elif action == "clock_out":
        if shift.clock_in is not None and shift.clock_out is None:
            shift.clock_out, shift.status = now, "out"
            db.add(AttendanceEvent(shift_id=shift.id, at=now, kind="out",
                                   text="Clocked out"))

    elif action == "log_event":
        kind = (request.form.get("kind") or "note").strip()
        note = (request.form.get("note") or "").strip() or None
        reason = (request.form.get("reason") or "").strip()[:60] or None
        occ = (request.form.get("occurrence") or "").strip().lower() == "yes"
        try:
            minutes = max(int(request.form.get("minutes") or 0), 0)
        except (TypeError, ValueError):
            minutes = 0
        if kind == "late":
            shift.status, shift.late_minutes = "late", minutes
            text = f"Late arrival logged — {minutes}m" + (f" · {reason}" if reason else "")
        elif kind == "no-show":
            shift.status = "no-show"
            text = "Marked no-show" + (f" · {reason}" if reason else "")
        elif kind == "callout":
            shift.status = "callout"
            text = "Callout recorded" + (f" · {reason}" if reason else "")
        elif kind == "break":
            shift.status = "break"
            text = "Started break"
        elif kind == "early-out":
            if shift.clock_in and shift.clock_out is None:
                shift.clock_out = now
            shift.status = "out"
            text = "Early out logged"
        else:
            kind, text = "note", (note or "Note added")
        db.add(AttendanceEvent(shift_id=shift.id, at=now, kind=kind, text=text,
                               reason=reason, counts_as_occurrence=occ))
        if note and kind != "note":
            shift.note = note

    shift.updated_at = now
    db.commit()


# ============================================================
# Prep List v3 — kitchen's daily prep board (dck, Sam build).
# Master PrepItem list left-joined to today's PrepEntry rows,
# grouped hot/cold/chop × item/sauce. recipe_id powers the detail
# panel's auto-pulled ingredient breakdown.
# ============================================================
_PREP_CATS = ("hot", "cold", "chop")
_PREP_KINDS = ("item", "sauce")
# (category, kind) -> (label, column). Left col = hot; right = cold + chop.
_PREP_SECTIONS = [
    ("hot",  "item",  "Hot Items",   "left"),
    ("hot",  "sauce", "Hot Sauces",  "left"),
    ("cold", "item",  "Cold Items",  "right"),
    ("cold", "sauce", "Cold Sauces", "right"),
    ("chop", "item",  "Chop",        "right"),
]


def _prep_initials(name):
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _prep_recipe_view(db, recipe_id):
    """(yield_label, prep_time, shelf_life, [ingredients]) for a linked
    Recipe, else (None, None, None, []). samai: wired onto the real Recipe
    schema — ingredients_json holds the bilingual
    [{name_en,name_es,qty_single,qty_double}] list; yield lives in
    batch_sizes_json. Prep board shows the English name + single-batch qty."""
    if not recipe_id:
        return None, None, None, []
    import json as _json
    from app.models import Recipe
    try:
        r = db.get(Recipe, recipe_id)
        if r is None:
            return None, None, None, []
        try:
            raw = _json.loads(r.ingredients_json) if r.ingredients_json else []
        except Exception:
            raw = []
        ingredients = []
        for ing in (raw or []):
            if not isinstance(ing, dict):
                continue
            ingredients.append({
                "name": ing.get("name_en") or ing.get("name") or "",
                "qty": ing.get("qty_single") or ing.get("qty") or "",
                "unit": ing.get("unit") or "",
            })
        y = None
        try:
            _bs = _json.loads(r.batch_sizes_json) if r.batch_sizes_json else None
            if isinstance(_bs, dict):
                y = _bs.get("yield_en") or _bs.get("yield_es")
        except Exception:
            y = None
        return (y, r.prep_time, r.shelf_life, ingredients)
    except Exception:
        # A recipe-load failure (e.g. a schema column not yet applied) must
        # never crash the prep board — degrade to no linked-recipe data.
        try:
            db.rollback()
        except Exception:
            pass
        return None, None, None, []


# Prep-item -> recipe auto-link by name (samai #9b). PrepItem.recipe_id is
# usually unset, so match the prep item's name to a Recipe name: exact first,
# then a small Spanish/English alias map, then a prefix fallback. Normalized
# lower/strip on both sides.
_PREP_RECIPE_ALIASES = {
    "refried": "refritos beans",
    "costillas": "pork ribs",
    "cochina": "cochinita pibil",
    "cochinita": "cochinita pibil",
    "black bean": "black beans homemade",
}


def _norm_recipe_name(s):
    return (s or "").strip().lower()


def _prep_recipe_name_map(db):
    """Return a resolver with .get(prep_name) -> recipe_id (or None),
    matching exact name, then alias, then prefix either direction."""
    from app.models import Recipe
    exact = {}
    names = []
    try:
        for rid, nm in db.query(Recipe.id, Recipe.name).all():
            key = _norm_recipe_name(nm)
            if key and key not in exact:
                exact[key] = rid
                names.append((key, rid))
    except Exception:
        pass

    class _NameMap:
        def get(self, raw_name):
            key = _norm_recipe_name(raw_name)
            if not key:
                return None
            if key in exact:
                return exact[key]
            alias = _PREP_RECIPE_ALIASES.get(key)
            if alias and alias in exact:
                return exact[alias]
            for nk, rid in names:
                if nk.startswith(key) or key.startswith(nk):
                    return rid
            return None

    return _NameMap()


def _prep_actor_name(user):
    if user is not None:
        return (
            getattr(user, "full_name", None)
            or getattr(user, "name", None)
            or getattr(user, "email", None)
            or getattr(user, "phone", None)
        )
    return session.get("employee_name") or session.get("user_name") or "Unknown"


def _prep_load_helpers(raw):
    import json as _json
    if not raw:
        return []
    try:
        data = _json.loads(raw)
    except Exception:
        data = [p.strip() for p in str(raw).split(",")]
    return [str(p).strip() for p in (data or []) if str(p).strip()]


def _prep_dump_helpers(names):
    import json as _json
    clean = []
    seen = set()
    for name in names or []:
        n = str(name or "").strip()[:120]
        key = n.lower()
        if n and key not in seen:
            clean.append(n)
            seen.add(key)
    return _json.dumps(clean) if clean else None


def _prep_log_event(db, entry, item, user, action, details=None):
    """Append one Developer-tab audit row. Failures here should never block
    the kitchen from saving the actual prep work."""
    import json as _json
    try:
        from app.models import PrepAuditLog
        db.add(PrepAuditLog(
            entry_id=(entry.id if entry else None),
            entry_date=(entry.entry_date if entry else _local_today()),
            store_scope=(entry.store_scope if entry else None),
            prep_item_id=(item.id if item else None),
            item_name=(item.name if item else None),
            actor_user_id=(getattr(user, "id", None) if user else None),
            actor_name=_prep_actor_name(user),
            action=action,
            details_json=_json.dumps(details or {}, default=str),
        ))
    except Exception:
        logging.getLogger(__name__).exception(
            "prep audit log failed (non-fatal)")


def _prep_status_label(status):
    status = {"done": "completed", "in-progress": "partly",
              "selected": "not-completed"}.get(status, status)
    return {
        "not-completed": "Not completed",
        "partly": "Partly completed",
        "completed": "Completed",
        "not-needed": "Not needed",
    }.get(status or "not-completed", "Not completed")


def _prep_time_label(dt):
    if not dt:
        return None
    return dt.strftime("%I:%M %p").lstrip("0")


def _prep_datetime_label(dt):
    if not dt:
        return None
    return f"{dt:%b} {dt.day}, {dt.year} {_prep_time_label(dt)}"


def _parse_qty(s):
    """Parse a quantity like '12 CUPS', '1/2 BAG', '1 1/2 GALL', '2.5 LB'
    into (float_value, unit). Returns (None, original) if the leading
    amount can't be parsed."""
    import re as _re
    s = (s or "").strip()
    if not s:
        return None, ""
    m = _re.match(r"^(\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)\s*(.*)$", s)
    if not m:
        return None, s
    num_str, unit = m.group(1), m.group(2).strip()
    try:
        if " " in num_str:
            whole, frac = num_str.split()
            n, d = frac.split("/")
            val = float(whole) + float(n) / float(d)
        elif "/" in num_str:
            n, d = num_str.split("/")
            val = float(n) / float(d)
        else:
            val = float(num_str)
    except Exception:
        return None, s
    return val, unit


def _fmt_qty(val):
    if abs(val - round(val)) < 1e-9:
        return str(int(round(val)))
    return f"{val:.2f}".rstrip("0").rstrip(".")


# Unit conversion for merging same-name ingredients across recipes. Units in
# the same family convert to a base; we sum then express in the largest unit
# that appeared among the merged entries. Unknown/non-convertible units (e.g.
# "bag", "can") can't be added to a "cup", so they stay grouped by their own
# unit and are shown alongside on the one ingredient row.
_UNIT_FAMILY = {
    "tsp": ("vol", 1.0), "teaspoon": ("vol", 1.0), "teaspoons": ("vol", 1.0),
    "tbsp": ("vol", 3.0), "tablespoon": ("vol", 3.0), "tablespoons": ("vol", 3.0),
    "cup": ("vol", 48.0), "cups": ("vol", 48.0),
    "pint": ("vol", 96.0), "pints": ("vol", 96.0), "pt": ("vol", 96.0),
    "quart": ("vol", 192.0), "quarts": ("vol", 192.0), "qt": ("vol", 192.0), "qts": ("vol", 192.0),
    "gallon": ("vol", 768.0), "gallons": ("vol", 768.0), "gal": ("vol", 768.0), "gall": ("vol", 768.0),
    "oz": ("wt", 1.0), "ounce": ("wt", 1.0), "ounces": ("wt", 1.0),
    "lb": ("wt", 16.0), "lbs": ("wt", 16.0), "pound": ("wt", 16.0), "pounds": ("wt", 16.0),
}
_FAMILY_DISPLAY = {
    "vol": [("GAL", 768.0), ("QT", 192.0), ("CUP", 48.0), ("TBSP", 3.0), ("TSP", 1.0)],
    "wt": [("LB", 16.0), ("OZ", 1.0)],
}


def _norm_unit(unit):
    """Normalize a unit to a lookup key: lowercase, fold 'tea spoon'/'table
    spoon', take the leading token (drops trailing notes like '(1bag)')."""
    u = (unit or "").strip().lower()
    if not u:
        return ""
    u = u.replace("tea spoon", "tsp").replace("teaspoon", "tsp")
    u = u.replace("table spoon", "tbsp").replace("tablespoon", "tbsp")
    parts = u.split()
    tok = parts[0] if parts else u
    return tok.strip("().,:;")


def _prep_total_ingredients(sel_views):
    """Sum ingredients across all selected items' linked recipes into ONE row
    per ingredient NAME (samai #14 — 'add up all the chopped onions'). Same-
    name quantities merge: compatible units (volume / weight) convert to a
    common base and sum into a single total, expressed in the largest unit
    present; non-convertible units (e.g. 'bag' vs 'cup') sum within their own
    unit and are shown together on the one row."""
    by_name = {}
    name_order = []
    for v in sel_views:
        for ing in (v.get("ingredients") or []):
            name = (ing.get("name") or "").strip()
            if not name:
                continue
            qty = (ing.get("qty") or "").strip()
            val, raw_unit = _parse_qty(qty)
            nkey = name.lower()
            if nkey not in by_name:
                by_name[nkey] = {"name": name, "groups": {}, "gorder": []}
                name_order.append(nkey)
            rec = by_name[nkey]
            norm = _norm_unit(raw_unit)
            fam = _UNIT_FAMILY.get(norm)
            if val is not None and fam is not None:
                gkey = ("fam", fam[0])
                g = rec["groups"].get(gkey)
                if g is None:
                    g = {"kind": "fam", "fam": fam[0], "base_total": 0.0, "seen": set()}
                    rec["groups"][gkey] = g
                    rec["gorder"].append(gkey)
                g["base_total"] += val * fam[1]
                g["seen"].add(fam[1])
            else:
                gkey = ("raw", norm)
                g = rec["groups"].get(gkey)
                if g is None:
                    g = {"kind": "raw", "unit": (raw_unit or "").strip(),
                         "total": 0.0, "ok": True, "raw": []}
                    rec["groups"][gkey] = g
                    rec["gorder"].append(gkey)
                g["raw"].append(qty or "?")
                if val is None:
                    g["ok"] = False
                else:
                    g["total"] += val

    out = []
    for nkey in name_order:
        rec = by_name[nkey]
        parts = []
        for gkey in rec["gorder"]:
            g = rec["groups"][gkey]
            if g["kind"] == "fam":
                disp_units = _FAMILY_DISPLAY[g["fam"]]
                biggest = max(g["seen"]) if g["seen"] else 1.0
                label, factor = disp_units[-1]
                for lbl, fac in disp_units:
                    if fac <= biggest + 1e-9:
                        label, factor = lbl, fac
                        break
                parts.append((f"{_fmt_qty(g['base_total'] / factor)} {label}").strip())
            elif g["ok"]:
                parts.append((f"{_fmt_qty(g['total'])} {g['unit']}").strip())
            else:
                parts.append(" + ".join(g["raw"]))
        out.append({"name": rec["name"], "qty": " + ".join(p for p in parts if p)})
    out.sort(key=lambda x: x["name"].lower())
    return out


def _render_prep_list_v3(db, label, active_key):
    """Prep List v3 board. ?date=<iso> picks the day (default today)."""
    from app.models import PrepItem, PrepEntry, PrepAuditLog
    raw = request.args.get("date") or ""
    try:
        sel = date.fromisoformat(raw) if raw else _local_today()
    except ValueError:
        sel = _local_today()
    today = _local_today()
    loc = g.current_location
    active_prep_tab = (request.args.get("tab") or "board").strip().lower()
    if active_prep_tab not in ("board", "recent", "performance", "developer"):
        active_prep_tab = "board"
    try:
        open_item_id = int(request.args.get("open") or 0)
    except (TypeError, ValueError):
        open_item_id = 0

    def _scoped(q, model):
        if loc in ("tomball", "copperfield"):
            return q.filter((model.store_scope == loc) |
                            (model.store_scope.is_(None)))
        return q

    items = _scoped(
        db.query(PrepItem).filter(PrepItem.active.is_(True)), PrepItem
    ).order_by(PrepItem.sort_order.asc(), PrepItem.name.asc()).all()

    entries = _scoped(
        db.query(PrepEntry).filter(PrepEntry.entry_date == sel), PrepEntry
    ).all()
    by_item = {e.prep_item_id: e for e in entries}
    locked = any(e.locked for e in entries) if entries else False

    # Auto-link prep items to recipes by name (samai #9b).
    recipe_by_name = _prep_recipe_name_map(db)
    # Full active-staff names for the assignee typeahead (#6) — names only,
    # no phone/email/ids (roster-privacy rule).
    all_staff = []
    try:
        from app.models import Employee
        all_staff = [
            n for (n,) in db.query(Employee.full_name)
            .filter(Employee.active.is_(True))
            .order_by(Employee.full_name.asc()).all() if n
        ]
    except Exception:
        all_staff = []

    _legacy_status = {"done": "completed", "in-progress": "partly"}

    def _item_view(pi):
        e = by_item.get(pi.id)
        rid = pi.recipe_id or recipe_by_name.get(pi.name)
        y, mins, shelf, ings = _prep_recipe_view(db, rid)
        status = (e.status if e else None) or "not-completed"
        status = _legacy_status.get(status, status)
        if status not in ("not-completed", "partly", "completed", "not-needed"):
            status = "not-completed"
        return {
            "id": pi.id,
            "entry_id": e.id if e else None,
            "name": pi.name,
            "category": pi.category,
            "kind": pi.kind,
            "selected": bool(e and e.selected),
            "on_hand": (e.on_hand if e else None),
            "prep_qty": (e.prep_qty if e else None),
            "assignee": (e.assignee_name if e else None),
            "assignee_initials": _prep_initials(e.assignee_name) if (e and e.assignee_name) else None,
            "helper_names": _prep_load_helpers(e.helper_names) if e else [],
            "status": status,
            "batch_size": (e.batch_size if e else None),
            "notes": (e.notes if e else None),
            "completed_by": (e.completed_by_name if e else None),
            "completed_at": (e.completed_at if e else None),
            "completed_at_label": _prep_time_label(e.completed_at) if e else None,
            "recipe_id": rid,
            "recipe_name": (pi.name if rid else None),
            "yield_label": y, "prep_minutes": mins, "shelf_days": shelf,
            "ingredients": ings,
        }

    views = [_item_view(pi) for pi in items]

    sections = []
    for cat, kind, sec_label, col in _PREP_SECTIONS:
        block = [v for v in views if v["category"] == cat and v["kind"] == kind]
        if not block:
            continue
        sections.append({
            "key": cat, "kind": kind, "label": sec_label, "col": col,
            "total": len(block),
            "selected": sum(1 for v in block if v["selected"]),
            "items": block,
        })

    sel_views = [v for v in views if v["selected"]]
    kpis = {
        "total": len(views),
        "selected": len(sel_views),
        "assigned": sum(1 for v in sel_views if v["assignee"]),
        "unassigned": sum(1 for v in sel_views if not v["assignee"]),
        "in_progress": sum(1 for v in sel_views if v["status"] == "partly"),
        "done": sum(1 for v in sel_views if v["status"] == "completed"),
        "partly": sum(1 for v in sel_views if v["status"] == "partly"),
        "completed": sum(1 for v in sel_views if v["status"] == "completed"),
        "not_needed": sum(1 for v in sel_views if v["status"] == "not-needed"),
        "not_completed": sum(1 for v in sel_views if v["status"] == "not-completed"),
    }

    # Prep team — driven by today's schedule rows with Position = Prep, then
    # annotated with today's prep assignments.
    assignment_map = {}
    for v in sel_views:
        if not v["assignee"]:
            continue
        t = assignment_map.setdefault(v["assignee"], {
            "name": v["assignee"], "initials": _prep_initials(v["assignee"]),
            "done": 0, "in_progress": 0, "assigned": 0})
        if v["status"] == "completed":
            t["done"] += 1
        elif v["status"] == "partly":
            t["in_progress"] += 1
        else:
            t["assigned"] += 1

    team_map = {}
    try:
        from app.models import Employee, Position, Schedule, Shift
        day_start = datetime.combine(sel, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        q = (
            db.query(Shift, Employee.full_name, Position.name)
            .join(Schedule, Shift.schedule_id == Schedule.id)
            .join(Employee, Shift.employee_id == Employee.id)
            .outerjoin(Position, Shift.position_id == Position.id)
            .filter(
                Shift.employee_id.isnot(None),
                Shift.start_at >= day_start,
                Shift.start_at < day_end,
            )
        )
        if loc in ("tomball", "copperfield"):
            q = q.filter(Schedule.store_key == loc)
        for shift, employee_name, position_name in q.all():
            if (position_name or "").strip().lower() != "prep":
                continue
            name = (employee_name or shift.display_name or "").strip()
            if not name:
                continue
            assigned = assignment_map.get(name, {})
            shift_label = ""
            if shift.start_at and shift.end_at:
                shift_label = f"{_prep_time_label(shift.start_at)}-{_prep_time_label(shift.end_at)}"
            row = team_map.setdefault(name, {
                "name": name,
                "initials": _prep_initials(name),
                "done": assigned.get("done", 0),
                "in_progress": assigned.get("in_progress", 0),
                "assigned": assigned.get("assigned", 0),
                "shift_labels": [],
                "position": "Prep",
            })
            if shift_label and shift_label not in row["shift_labels"]:
                row["shift_labels"].append(shift_label)
    except Exception:
        logging.getLogger(__name__).exception(
            "prep scheduled team rows failed (non-fatal)")
        team_map = {}

    team = []
    for row in team_map.values():
        total_active = row["done"] + row["in_progress"] + row["assigned"]
        row["has_assignment"] = total_active > 0
        row["assignment_count"] = total_active
        row["shift_label"] = ", ".join(row.pop("shift_labels", [])) or "Prep shift"
        team.append(row)
    team.sort(key=lambda t: t["name"])

    # ---- #14 (samai): aggregated views for the "Prep team today" strip ----
    assigned_today = [
        {"name": v["name"], "assignee": v["assignee"],
         "status": v["status"], "category": v["category"]}
        for v in sel_views if v["assignee"]
    ]
    today_status = {
        "selected": kpis["selected"], "assigned": kpis["assigned"],
        "unassigned": kpis["unassigned"], "not_completed": kpis["not_completed"],
        "partly": kpis["partly"], "completed": kpis["completed"],
        "not_needed": kpis["not_needed"],
    }
    total_ingredients = _prep_total_ingredients(sel_views)
    assignment_history = []
    try:
        from app.models import PrepEntry as _PE_hist
        _hrows = _scoped(
            db.query(_PE_hist).filter(
                _PE_hist.entry_date < sel,
                _PE_hist.entry_date >= (sel - timedelta(days=30)),
                _PE_hist.assignee_name.isnot(None),
            ), _PE_hist).all()
        _hd = {}
        for _e in _hrows:
            slot = _hd.setdefault(_e.entry_date, {"assigned": 0, "completed": 0})
            slot["assigned"] += 1
            if _e.status in ("completed", "done"):
                slot["completed"] += 1
        assignment_history = [
            {"date": d.isoformat(), "display": f"{d:%a, %b} {d.day}",
             "assigned": s["assigned"], "completed": s["completed"]}
            for d, s in sorted(_hd.items(), reverse=True)
        ]
    except Exception:
        assignment_history = []

    def _meaningful_entry(e):
        st = {"done": "completed", "in-progress": "partly",
              "selected": "not-completed"}.get(e.status, e.status)
        return bool(
            e.selected or e.on_hand is not None or e.prep_qty is not None
            or e.assignee_name or e.helper_names or e.notes
            or st in ("partly", "completed", "not-needed")
        )

    recent_entries = []
    try:
        recent_q = (
            db.query(PrepEntry, PrepItem)
            .join(PrepItem, PrepEntry.prep_item_id == PrepItem.id)
            .filter(
                PrepEntry.entry_date >= (sel - timedelta(days=14)),
                PrepEntry.entry_date <= sel,
            )
        )
        recent_rows = _scoped(recent_q, PrepEntry).order_by(
            PrepEntry.entry_date.desc(), PrepEntry.updated_at.desc()).all()
        for e, pi in recent_rows:
            if not _meaningful_entry(e):
                continue
            st = {"done": "completed", "in-progress": "partly",
                  "selected": "not-completed"}.get(e.status, e.status)
            helpers = _prep_load_helpers(e.helper_names)
            recent_entries.append({
                "entry_id": e.id,
                "item_id": pi.id,
                "item": pi.name,
                "date": e.entry_date.isoformat(),
                "date_label": f"{e.entry_date:%a}, {e.entry_date:%b} {e.entry_date.day}",
                "assignee": e.assignee_name,
                "helpers": helpers,
                "status": st or "not-completed",
                "status_label": _prep_status_label(st),
                "on_hand": e.on_hand,
                "prep_qty": e.prep_qty,
                "completed_by": e.completed_by_name or (
                    e.assignee_name if st == "completed" else None),
                "completed_at_label": _prep_time_label(e.completed_at),
                "notes": e.notes,
            })
            if len(recent_entries) >= 60:
                break
    except Exception:
        logging.getLogger(__name__).exception(
            "prep recent entries failed (non-fatal)")
        recent_entries = []

    performance_rows = []
    try:
        from app.models import AttendanceShift, Employee, Schedule, Shift
        perf = {}

        def _slot(name):
            key = (name or "").strip()
            if not key:
                return None
            return perf.setdefault(key, {
                "name": key, "assigned": 0, "started": 0, "partly": 0,
                "completed": 0, "helped": 0, "hours": 0.0,
            })

        for v in sel_views:
            s = _slot(v.get("assignee"))
            if s is not None:
                s["assigned"] += 1
                if (v.get("on_hand") is not None or v.get("prep_qty") is not None
                        or v.get("notes")):
                    s["started"] += 1
                if v["status"] == "partly":
                    s["partly"] += 1
                if v["status"] == "completed":
                    s["completed"] += 1
            for helper in v.get("helper_names") or []:
                hs = _slot(helper)
                if hs is not None:
                    hs["helped"] += 1

        attn_q = db.query(AttendanceShift).filter(AttendanceShift.entry_date == sel)
        attn_rows = _scoped(attn_q, AttendanceShift).all()
        for sh in attn_rows:
            s = _slot(sh.employee_name)
            if s is None:
                continue
            start = sh.clock_in or sh.scheduled_start
            end = sh.clock_out or sh.scheduled_end
            if start and end and end > start:
                s["hours"] += max((end - start).total_seconds() / 3600.0, 0.0)

        day_start = datetime.combine(sel, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        sched_q = (
            db.query(Shift, Employee.full_name)
            .join(Schedule, Shift.schedule_id == Schedule.id)
            .join(Employee, Shift.employee_id == Employee.id)
            .filter(
                Shift.employee_id.isnot(None),
                Shift.start_at >= day_start,
                Shift.start_at < day_end,
            )
        )
        if loc in ("tomball", "copperfield"):
            sched_q = sched_q.filter(Schedule.store_key == loc)
        for sh, emp_name in sched_q.all():
            s = _slot(emp_name)
            if s is None or s["hours"] > 0:
                continue
            mins = max((sh.end_at - sh.start_at).total_seconds() / 60.0, 0.0)
            mins = max(mins - float(sh.break_minutes or 0), 0.0)
            s["hours"] += mins / 60.0

        for name in all_staff:
            _slot(name)

        for row in perf.values():
            hours = row["hours"]
            completion_rate = (
                (row["completed"] / row["assigned"]) if row["assigned"] else 0.0
            )
            productivity = (row["completed"] / hours) if hours else 0.0
            row["hours_label"] = f"{hours:.1f}".rstrip("0").rstrip(".")
            row["completion_rate"] = int(round(completion_rate * 100))
            row["productivity_label"] = f"{productivity:.2f}".rstrip("0").rstrip(".")
            performance_rows.append(row)
        performance_rows.sort(
            key=lambda r: (-r["completed"], -r["partly"], r["name"].lower()))
    except Exception:
        logging.getLogger(__name__).exception(
            "prep performance rows failed (non-fatal)")
        performance_rows = []

    developer_events = []
    developer_entries = []
    try:
        import json as _json
        audit_q = db.query(PrepAuditLog).filter(
            PrepAuditLog.entry_date >= (sel - timedelta(days=30)))
        audit_rows = _scoped(audit_q, PrepAuditLog).order_by(
            PrepAuditLog.created_at.desc()).limit(120).all()
        for a in audit_rows:
            try:
                details = _json.loads(a.details_json) if a.details_json else {}
            except Exception:
                details = {"raw": a.details_json}
            developer_events.append({
                "at": _prep_datetime_label(a.created_at),
                "date": a.entry_date.isoformat(),
                "actor": a.actor_name or "Unknown",
                "action": a.action,
                "item": a.item_name or "Item",
                "details": details,
            })

        dev_q = (
            db.query(PrepEntry, PrepItem)
            .join(PrepItem, PrepEntry.prep_item_id == PrepItem.id)
            .filter(
                PrepEntry.entry_date >= (sel - timedelta(days=14)),
                PrepEntry.entry_date <= sel,
            )
        )
        for e, pi in _scoped(dev_q, PrepEntry).order_by(
                PrepEntry.updated_at.desc()).limit(120).all():
            if not _meaningful_entry(e):
                continue
            st = {"done": "completed", "in-progress": "partly",
                  "selected": "not-completed"}.get(e.status, e.status)
            developer_entries.append({
                "date": e.entry_date.isoformat(),
                "item": pi.name,
                "status": _prep_status_label(st),
                "on_hand": e.on_hand,
                "prep_qty": e.prep_qty,
                "assignee": e.assignee_name,
                "helpers": _prep_load_helpers(e.helper_names),
                "created": _prep_datetime_label(e.created_at),
                "updated": _prep_datetime_label(e.updated_at),
                "completed_by": e.completed_by_name,
                "completed_at": _prep_datetime_label(e.completed_at),
            })
    except Exception:
        logging.getLogger(__name__).exception(
            "prep developer rows failed (non-fatal)")
        developer_events = []
        developer_entries = []

    prep_tabs = [
        {"key": "board", "label": "Prep Board"},
        {"key": "recent", "label": "Recent Date"},
        {"key": "performance", "label": "Performance"},
        {"key": "developer", "label": "Developer"},
    ]

    return render_template(
        "prep_list.html",
        page_label=label, active=active_key,
        sections=sections, kpis=kpis, team=team,
        all_staff=all_staff,
        assigned_today=assigned_today, today_status=today_status,
        total_ingredients=total_ingredients,
        assignment_history=assignment_history,
        active_prep_tab=active_prep_tab,
        prep_tabs=prep_tabs,
        open_item_id=open_item_id,
        recent_entries=recent_entries,
        performance_rows=performance_rows,
        developer_events=developer_events,
        developer_entries=developer_entries,
        locked=locked, lock_author=None, lock_hours=None,
        selected_date=sel.isoformat(), today_iso=today.isoformat(),
        prev_date=(sel - timedelta(days=1)).isoformat(),
        next_date=(sel + timedelta(days=1)).isoformat(),
        date_display=f"{sel:%A, %B} {sel.day}, {sel.year}",
        is_today=(sel == today),
    )


def _prep_list_v3_post(db, store_scope, user):
    """Every POST on the Prep List page. Hidden form_action selects
    the op: toggle_select | set_on_hand | assign | set_status |
    save_detail | copy_yesterday | submit_lock."""
    from app.models import PrepItem, PrepEntry
    action = (request.form.get("form_action") or "").strip()
    uid = user.id if user else None

    raw_date = request.form.get("view_date") or ""
    try:
        d = date.fromisoformat(raw_date) if raw_date else _local_today()
    except ValueError:
        d = _local_today()
    loc = g.current_location

    def _entry_for(item_id, create=True):
        """Get-or-create today's PrepEntry for a PrepItem."""
        pi = db.get(PrepItem, item_id) if item_id else None
        if pi is None:
            return None
        if loc in ("tomball", "copperfield") and pi.store_scope not in (loc, None):
            abort(403)
        e = (db.query(PrepEntry)
             .filter(PrepEntry.entry_date == d,
                     PrepEntry.prep_item_id == item_id)
             .first())
        if e is None and create:
            e = PrepEntry(entry_date=d, prep_item_id=item_id,
                          store_scope=store_scope, author_id=uid,
                          status="not-completed", selected=False)
            db.add(e)
            db.flush()
        return e

    def _parse_nonneg_int(field):
        raw = (request.form.get(field) or "").strip()
        if raw == "":
            return None
        try:
            return max(0, min(int(raw), 9999))
        except (TypeError, ValueError):
            return None

    # submit_lock / copy_yesterday operate on the whole day, not one item.
    if action == "submit_lock":
        for e in db.query(PrepEntry).filter(PrepEntry.entry_date == d).all():
            if loc in ("tomball", "copperfield") and e.store_scope not in (loc, None):
                continue
            e.locked = True
            _prep_log_event(db, e, e.item, user, "submitted", {
                "locked": True,
            })
        db.commit()
        return

    if action == "copy_yesterday":
        prev = d - timedelta(days=1)
        prev_rows = db.query(PrepEntry).filter(
            PrepEntry.entry_date == prev, PrepEntry.selected.is_(True)).all()
        for src in prev_rows:
            if loc in ("tomball", "copperfield") and src.store_scope not in (loc, None):
                continue
            e = _entry_for(src.prep_item_id)
            if e and not e.locked:
                e.selected = True
                if e.status in ("selected", None):
                    e.status = "not-completed"
                _prep_log_event(db, e, e.item, user, "copied", {
                    "from_date": prev.isoformat(),
                })
        db.commit()
        return

    # Per-item ops.
    try:
        item_id = int(request.form.get("item_id") or 0)
    except (TypeError, ValueError):
        item_id = 0
    e = _entry_for(item_id)
    if e is None or e.locked:
        return
    pi = e.item or db.get(PrepItem, item_id)

    if action == "toggle_select":
        e.selected = not e.selected
        if not e.selected:
            e.status = "not-completed"
        _prep_log_event(db, e, pi, user, "selected" if e.selected else "unselected", {
            "selected": e.selected,
        })

    elif action == "set_on_hand":
        e.on_hand = _parse_nonneg_int("on_hand")
        _prep_log_event(db, e, pi, user, "on_hand", {
            "on_hand": e.on_hand,
        })

    elif action == "assign":
        name = (request.form.get("assignee_name") or "").strip()[:120]
        e.assignee_name = name or None
        if name:
            e.selected = True
        _prep_log_event(db, e, pi, user, "assigned", {
            "assignee": e.assignee_name,
        })

    elif action == "set_status":
        st = (request.form.get("status") or "not-completed").strip()
        if st in ("not-completed", "partly", "completed", "not-needed"):
            old_status = e.status
            e.status = st
            if st in ("partly", "completed"):
                e.selected = True
            if st == "completed":
                e.completed_by_name = _prep_actor_name(user)
                e.completed_at = datetime.utcnow()
            elif old_status == "completed":
                e.completed_by_name = None
                e.completed_at = None
            _prep_log_event(db, e, pi, user,
                            "completed" if st == "completed" else "status", {
                                "from": old_status,
                                "to": st,
                            })

    elif action == "save_detail":
        if "batch_size" in request.form:
            bs = (request.form.get("batch_size") or "").strip().lower()
            e.batch_size = bs if bs in ("single", "double") else None
        if "notes" in request.form:
            e.notes = (request.form.get("notes") or "").strip() or None
        e.selected = True
        _prep_log_event(db, e, pi, user, "detail", {
            "batch_size": e.batch_size,
            "notes": bool(e.notes),
        })

    elif action == "save_tracker":
        old = {
            "status": e.status,
            "on_hand": e.on_hand,
            "prep_qty": e.prep_qty,
            "assignee": e.assignee_name,
            "helpers": _prep_load_helpers(e.helper_names),
            "notes": bool(e.notes),
        }
        e.selected = True
        e.on_hand = _parse_nonneg_int("on_hand")
        e.prep_qty = _parse_nonneg_int("prep_qty")
        e.assignee_name = (request.form.get("assignee_name") or "").strip()[:120] or None
        helper_names = request.form.getlist("helper_names")
        if not helper_names:
            helper_names = [
                n.strip() for n in (request.form.get("helper_names_text") or "").split(",")
                if n.strip()
            ]
        e.helper_names = _prep_dump_helpers(helper_names)
        st = (request.form.get("status") or "not-completed").strip()
        if st not in ("not-completed", "partly", "completed"):
            st = "not-completed"
        e.status = st
        e.notes = (request.form.get("notes") or "").strip() or None
        if st == "completed":
            e.completed_by_name = _prep_actor_name(user)
            e.completed_at = datetime.utcnow()
        else:
            e.completed_by_name = None
            e.completed_at = None
        _prep_log_event(db, e, pi, user,
                        "completed" if st == "completed" else "updated", {
                            "from": old,
                            "to": {
                                "status": e.status,
                                "on_hand": e.on_hand,
                                "prep_qty": e.prep_qty,
                                "assignee": e.assignee_name,
                                "helpers": _prep_load_helpers(e.helper_names),
                                "notes": bool(e.notes),
                            },
                        })

    db.commit()


# ---- Manager dashboard (tabbed entry layer, Sam 2026-05-21, samai) --
# Structural twin of the Catering dashboard. The bottom-nav Manager tab
# no longer opens a sub-option popover — it links straight here. This
# route renders manager_dashboard.html: a tab strip across the seven
# manager pages, defaulting to the Daily Log tab.
#
# DESIGN-CHANGE rework (Sam 2026-05-21, samai): each tab no longer shows
# a read-only preview + an "Open full page" link. Instead each tab
# embeds the REAL, fully-functional manager page inline in an <iframe> —
# click a tab and the working page is right there. So this route no
# longer builds preview rows or pulls the manager-section tables; it
# only needs each tab's URL for the iframe to load. The existing
# manager pages and routes are untouched — they are simply iframed.

# Ordered tab spec: (tab key, caption). The key matches the active_tab
# values manager_dashboard.html expects. First entry is the default
# tab. The per-tab url is built per-request by _manager_dash_full_url
# since six tabs point at store-scoped /manager/<slug> routes and one
# (interview) is a flat /partner route.
_MANAGER_DASH_TABS = [
    ("log",         "Daily Log"),
    ("incidents",   "Incidents"),
    ("attendance",  "Attendance"),
    ("training",    "Training"),
    ("maintenance", "Maintenance"),
    ("counseling",  "Counseling"),
    ("interview",   "Interview"),
    # Sports Board (Sam 2026-06-13): the "What's On" sports tracker — six
    # category sub-tabs (Today / Live / Upcoming / Completed / Previous /
    # Favorites) rendered inside the tab's iframe by store.sports_dashboard.
    ("sports",      "Sports"),
]


def _manager_dash_full_url(tab_key):
    """Absolute href to the real manager page a tab embeds in its
    iframe.
      log         -> /<store>/manager/daily-log         (store.manager_page_list)
      incidents   -> /<store>/manager/incident-reports  (store.manager_page_list)
      attendance  -> /<store>/manager/attendance        (store.manager_page_list)
      training    -> /<store>/manager/training          (store.manager_page_list)
      maintenance -> /<store>/manager/maintenance       (store.manager_page_list)
      counseling  -> /<store>/manager/counseling        (store.manager_page_list)
      interview   -> /partner/interview-tracker         (flat, not store-scoped)
    The flat interview-tracker path is written as a literal because it
    lives outside the /<store> blueprint; url_for on a store endpoint
    would prepend the slug. Falls back to the daily-log page on an
    unknown key so the iframe src is never empty."""
    if tab_key == "log":
        return url_for("store.manager_page_list", page="daily-log")
    if tab_key == "incidents":
        return url_for("store.manager_page_list", page="incident-reports")
    if tab_key == "attendance":
        return url_for("store.manager_page_list", page="attendance")
    if tab_key == "training":
        return url_for("store.manager_page_list", page="training")
    if tab_key == "maintenance":
        return url_for("store.manager_page_list", page="maintenance")
    if tab_key == "counseling":
        return url_for("store.manager_page_list", page="counseling")
    if tab_key == "interview":
        return "/partner/interview-tracker"
    if tab_key == "sports":
        return url_for("store.sports_dashboard")
    return url_for("store.manager_page_list", page="daily-log")


@store_bp.route("/manager", methods=["GET"])
def manager_dashboard():
    """Tabbed Manager dashboard — the entry layer the bottom-nav
    Manager tab links to. Defaults to the Daily Log tab; ?tab=<key>
    deep-links another tab (an invalid tab falls back to Daily Log).
    Each tab embeds the real, fully-functional manager page inline in
    an iframe. Structural twin of catering_dashboard.

    This route is now a thin shell: it builds only the tab list (key,
    label, url) the template needs to point each iframe at its page. No
    DB session is opened — the iframed pages run their own queries when
    the browser loads them."""
    if not _manager_dashboard_ok():
        abort(403)
    valid = {key for key, _ in _MANAGER_DASH_TABS}
    active_tab = (request.args.get("tab") or "").strip().lower()
    if active_tab not in valid:
        active_tab = "log"
    tabs = [
        {
            "key": key,
            "label": caption,
            "url": _manager_dash_full_url(key),
        }
        for key, caption in _MANAGER_DASH_TABS
    ]
    label = g.store_label or "Cenas Kitchen"
    # Portable "Wed, May 21" — no %-d / %#d (platform-specific).
    today_label = _today_label()
    return render_template(
        "manager_dashboard.html",
        active="manager_dashboard",
        store_label=label,
        today_label=today_label,
        active_tab=active_tab,
        tabs=tabs,
    )


@store_bp.route("/sports", methods=["GET"])
def sports_dashboard():
    """Sports Board ("What's On") — the page the Manager dashboard's
    Sports tab embeds in its iframe. A self-contained scoreboard with
    six category sub-tabs (Today / Live / Upcoming / Completed /
    Previous / Favorites), sport filters, search, and a game card that
    ALWAYS shows the Houston DirecTV + Xfinity channel (number or
    "Not available") plus a details drawer. Houston / Central time.

    The board loads live games from the sibling /sports/data.json feed
    (real games across all sports from ESPN, Houston-broadcast-mapped);
    if that feed is unavailable it falls back to a built-in sample slate.
    Same audience gate as the rest of the Manager section."""
    if not _manager_dashboard_ok():
        abort(403)
    return render_template("sports_dashboard.html")


@store_bp.route("/sports/data.json", methods=["GET"])
def sports_data():
    """Live "what's on" feed for the Sports Board. Real games across all
    sports (soccer/World Cup, MLB, NBA, NHL, WNBA, golf, tennis, ...) pulled
    from ESPN's public scoreboard for a date window, normalized and tagged
    with each game's carrying networks (the board maps those to the Houston
    DirecTV / Xfinity channel). Cached ~60s in process. Same gate as the tab;
    on any failure returns ok:false so the board keeps its sample slate."""
    if not _manager_dashboard_ok():
        abort(403)
    try:
        from app.sports.live_feed import get_live_games
        games, meta = get_live_games()
        return jsonify({"ok": True, "sample": False, "games": games, "meta": meta})
    except Exception:
        logging.getLogger(__name__).exception("sports live feed failed")
        return jsonify({"ok": False, "sample": True, "games": [], "meta": {}})


@store_bp.route("/manager/<page>", methods=["GET"])
def manager_page_list(page: str):
    """List view for a manager-section page. Renders the shared
    manager_log.html template with rows newest first, store-scoped.
    Special-case: daily-log uses the v3 windowed day-grouped view
    (dck build-order #2 2026-05-19)."""
    Model = _manager_model_for_slug(page)
    label = _MANAGER_PAGE_LABELS.get(page)
    if Model is None or label is None:
        abort(404)
    if not _manager_dashboard_ok():
        abort(403)
    active_key = "manager_" + page.replace("-", "_")
    db = next(get_db())
    try:
        if page == "daily-log":
            return _render_daily_log_v3(db, label, active_key)
        if page == "incident-reports":
            return _render_incident_reports_v3(db, label, active_key)
        if page == "counseling":
            return _render_employee_counseling_v3(db, label, active_key)
        if page == "attendance":
            return _render_attendance_v3(db, label, active_key)
        if page == "maintenance":
            return _render_maintenance_v3(db, label, active_key)
        if page == "training":
            return _render_training_v3(db, label, active_key)
        q = db.query(Model)
        if g.current_location in ("tomball", "copperfield"):
            q = q.filter(
                (Model.store_scope == g.current_location) |
                (Model.store_scope.is_(None))
            )
        rows = q.order_by(Model.created_at.desc()).limit(100).all()
        return render_template(
            "manager_log.html",
            page_slug=page,
            page_label=label,
            entries=rows,
            entry=None,
            form_mode=None,
            active=active_key,
        )
    finally:
        db.close()


@store_bp.route("/manager/<page>/new", methods=["GET"])
def manager_page_new(page: str):
    """Render the create form for a manager-section page."""
    Model = _manager_model_for_slug(page)
    label = _MANAGER_PAGE_LABELS.get(page)
    if Model is None or label is None:
        abort(404)
    if not _manager_dashboard_ok():
        abort(403)
    active_key = "manager_" + page.replace("-", "_")
    if page == "incident-reports":
        db = next(get_db())
        try:
            return _render_incident_reports_v3(db, label, active_key, form_mode="new")
        finally:
            db.close()
    return render_template(
        "manager_log.html",
        page_slug=page,
        page_label=label,
        entries=[],
        entry=None,
        form_mode="new",
        active=active_key,
    )


@store_bp.route("/manager/<page>", methods=["POST"])
def manager_page_create(page: str):
    """Create a new entry on a manager-section page."""
    Model = _manager_model_for_slug(page)
    if Model is None:
        abort(404)
    if not _manager_dashboard_ok():
        abort(403)
    user = getattr(g, "current_user", None)
    db = next(get_db())
    try:
        store_scope = (g.current_location
                       if g.current_location in ("tomball", "copperfield")
                       else None)
        if page == "daily-log":
            _create_daily_log_v3_entry(db, store_scope, user)
            return redirect(url_for("store.manager_page_list", page=page))
        if page == "incident-reports":
            row = _create_incident_v3_entry(db, store_scope, user)
            # Drafts go back to the form; submitted rows go to the list
            # with ?just_filed so the dashboard can pulse the new entry
            # (Sam #4:23 spec item 8 — post-submit confirmation).
            form_action = (request.form.get("form_action") or "submit").strip().lower()
            if form_action == "draft":
                return redirect(url_for("store.manager_page_list", page=page))
            return redirect(url_for(
                "store.manager_page_list", page=page,
                just_filed=getattr(row, "report_id", None)))
        if page == "counseling":
            _employee_counseling_v3_post(db, store_scope, user)
            return redirect(url_for("store.manager_page_list", page=page))
        if page == "attendance":
            _attendance_v3_post(db, store_scope, user)
            return redirect(url_for(
                "store.manager_page_list", page=page,
                date=(request.form.get("view_date")
                      or request.form.get("entry_date") or None)))
        row = Model(
            title=(request.form.get("title") or "").strip()[:300] or None,
            body=(request.form.get("body") or "").strip() or None,
            type_tag=(request.form.get("type_tag") or "").strip()[:80] or None,
            store_scope=store_scope,
            author_id=(user.id if user else None),
        )
        db.add(row)
        db.commit()
        return redirect(url_for("store.manager_page_list", page=page))
    finally:
        db.close()


@store_bp.route("/manager/<page>/<int:entry_id>", methods=["GET"])
def manager_page_detail(page: str, entry_id: int):
    """Read view for a single manager-section entry."""
    Model = _manager_model_for_slug(page)
    label = _MANAGER_PAGE_LABELS.get(page)
    if Model is None or label is None:
        abort(404)
    if not _manager_dashboard_ok():
        abort(403)
    active_key = "manager_" + page.replace("-", "_")
    db = next(get_db())
    try:
        row = db.get(Model, entry_id)
        if row is None:
            abort(404)
        if g.current_location in ("tomball", "copperfield"):
            if row.store_scope not in (None, g.current_location):
                abort(404)
        return render_template(
            "manager_log.html",
            page_slug=page,
            page_label=label,
            entries=[],
            entry=row,
            form_mode=None,
            active=active_key,
        )
    finally:
        db.close()


@store_bp.route("/manager/<page>/<int:entry_id>/edit", methods=["GET"])
def manager_page_edit(page: str, entry_id: int):
    """Render the edit form for a manager-section entry. Author or
    partner/corporate only — KMs editing other KMs' entries is a v2
    concern."""
    Model = _manager_model_for_slug(page)
    label = _MANAGER_PAGE_LABELS.get(page)
    if Model is None or label is None:
        abort(404)
    if not _manager_dashboard_ok():
        abort(403)
    user = getattr(g, "current_user", None)
    db = next(get_db())
    try:
        row = db.get(Model, entry_id)
        if row is None:
            abort(404)
        if g.current_location in ("tomball", "copperfield"):
            if row.store_scope not in (None, g.current_location):
                abort(404)
        is_author = bool(user and row.author_id == user.id)
        is_partner = (getattr(user, "role", "") or "").lower() in {
            "partner", "corporate"}
        if not (is_author or is_partner):
            abort(403)
        active_key = "manager_" + page.replace("-", "_")
        return render_template(
            "manager_log.html",
            page_slug=page,
            page_label=label,
            entries=[],
            entry=row,
            form_mode="edit",
            active=active_key,
        )
    finally:
        db.close()


@store_bp.route("/manager/<page>/<int:entry_id>", methods=["POST"])
def manager_page_update(page: str, entry_id: int):
    """Update an existing manager-section entry."""
    Model = _manager_model_for_slug(page)
    if Model is None:
        abort(404)
    if not _manager_dashboard_ok():
        abort(403)
    user = getattr(g, "current_user", None)
    db = next(get_db())
    try:
        row = db.get(Model, entry_id)
        if row is None:
            abort(404)
        if g.current_location in ("tomball", "copperfield"):
            if row.store_scope not in (None, g.current_location):
                abort(404)
        is_author = bool(user and row.author_id == user.id)
        is_partner = (getattr(user, "role", "") or "").lower() in {
            "partner", "corporate"}
        if not (is_author or is_partner):
            abort(403)
        new_title = (request.form.get("title") or "").strip()[:300] or None
        new_body  = (request.form.get("body") or "").strip() or None
        new_tag   = (request.form.get("type_tag") or "").strip()[:80] or None
        row.title = new_title
        row.body = new_body
        row.type_tag = new_tag
        db.commit()
        return redirect(url_for(
            "store.manager_page_detail", page=page, entry_id=entry_id))
    finally:
        db.close()


# ============================================================
# RECIPES — Sam dev #3074 + cena #1209. Single Recipe table; 33
# recipes across 5 categories. Audience: everyone except expo
# (same gate as manager pages).
# ============================================================
_RECIPE_CATEGORIES = ["cold", "hot", "sauces", "marinated", "chop"]


@store_bp.route("/recipes", methods=["GET"])
def recipes_index():
    if not _kitchen_dashboard_ok():
        abort(403)
    from app.models import Recipe
    import json as _json
    db = next(get_db())
    try:
        rows = db.query(Recipe).order_by(Recipe.category, Recipe.name).all()
        recipes = []
        for r in rows:
            try:
                ings = _json.loads(r.ingredients_json) if r.ingredients_json else []
            except Exception:
                ings = []
            try:
                bsizes = _json.loads(r.batch_sizes_json) if r.batch_sizes_json else []
            except Exception:
                bsizes = []
            # yield (EN/ES) is stored as a dict in batch_sizes_json by the
            # samai bilingual seed; legacy rows may hold a list instead.
            _y_en = _y_es = ""
            if isinstance(bsizes, dict):
                _y_en = bsizes.get("yield_en") or ""
                _y_es = bsizes.get("yield_es") or ""
            recipes.append({
                "id": r.id, "code": r.code,
                "category": r.category, "name": r.name,
                "prep_time": r.prep_time, "prep_time_es": r.prep_time_es,
                "shelf_life": r.shelf_life, "shelf_life_es": r.shelf_life_es,
                "yield_en": _y_en, "yield_es": _y_es,
                "batch_sizes": bsizes if isinstance(bsizes, list) else [],
                "ingredients": ings,
                "english_instructions": r.english_instructions,
                "spanish_instructions": r.spanish_instructions,
                "notes": r.notes,
            })
        return render_template(
            "recipes.html", recipes=recipes, recipe=None, form_mode=None,
            categories=[{"slug": c, "label": c.title()} for c in _RECIPE_CATEGORIES],
            active="recipes",
        )
    finally:
        db.close()


@store_bp.route("/recipes/new", methods=["GET"])
def recipes_new():
    if not _kitchen_dashboard_ok():
        abort(403)
    return render_template(
        "recipes.html", recipes=[], recipe=None, form_mode="new",
        categories=[{"slug": c, "label": c.title()} for c in _RECIPE_CATEGORIES],
        active="recipes",
    )


@store_bp.route("/recipes", methods=["POST"])
def recipes_create():
    if not _kitchen_dashboard_ok():
        abort(403)
    import json as _json
    from app.models import Recipe
    db = next(get_db())
    try:
        ing_raw = (request.form.get("ingredients_json") or "").strip()
        bsz_raw = (request.form.get("batch_sizes_csv") or "").strip()
        try:
            ing = _json.loads(ing_raw) if ing_raw else []
        except Exception:
            ing = []
        bsz = [s.strip() for s in bsz_raw.split(",") if s.strip()] if bsz_raw else []
        row = Recipe(
            code=(request.form.get("code") or "").strip()[:20] or None,
            category=(request.form.get("category") or "").strip()[:40] or "hot",
            name=(request.form.get("name") or "").strip()[:200] or "Untitled",
            prep_time=(request.form.get("prep_time") or "").strip()[:80] or None,
            shelf_life=(request.form.get("shelf_life") or "").strip()[:80] or None,
            english_instructions=(request.form.get("english_instructions") or "").strip() or None,
            spanish_instructions=(request.form.get("spanish_instructions") or "").strip() or None,
            ingredients_json=_json.dumps(ing) if ing else None,
            batch_sizes_json=_json.dumps(bsz) if bsz else None,
            notes=(request.form.get("notes") or "").strip() or None,
        )
        db.add(row)
        db.commit()
        return redirect(url_for("store.recipes_index"))
    finally:
        db.close()


@store_bp.route("/recipes/<int:recipe_id>", methods=["GET"])
def recipes_detail(recipe_id: int):
    if not _kitchen_dashboard_ok():
        abort(403)
    import json as _json
    from app.models import Recipe
    db = next(get_db())
    try:
        r = db.get(Recipe, recipe_id)
        if r is None:
            abort(404)
        try:
            ings = _json.loads(r.ingredients_json) if r.ingredients_json else []
        except Exception:
            ings = []
        try:
            bsizes = _json.loads(r.batch_sizes_json) if r.batch_sizes_json else []
        except Exception:
            bsizes = []
        recipe = {
            "id": r.id, "code": r.code,
            "category": r.category, "name": r.name,
            "prep_time": r.prep_time, "shelf_life": r.shelf_life,
            "batch_sizes": bsizes, "ingredients": ings,
            "english_instructions": r.english_instructions,
            "spanish_instructions": r.spanish_instructions, "notes": r.notes,
        }
        return render_template(
            "recipes.html", recipes=[], recipe=recipe, form_mode=None,
            categories=[{"slug": c, "label": c.title()} for c in _RECIPE_CATEGORIES],
            active="recipes",
        )
    finally:
        db.close()


@store_bp.route("/recipes/<int:recipe_id>/edit", methods=["GET"])
def recipes_edit(recipe_id: int):
    if not _kitchen_dashboard_ok():
        abort(403)
    import json as _json
    from app.models import Recipe
    db = next(get_db())
    try:
        r = db.get(Recipe, recipe_id)
        if r is None:
            abort(404)
        try:
            ings = _json.loads(r.ingredients_json) if r.ingredients_json else []
        except Exception:
            ings = []
        try:
            bsizes = _json.loads(r.batch_sizes_json) if r.batch_sizes_json else []
        except Exception:
            bsizes = []
        recipe = {
            "id": r.id, "code": r.code,
            "category": r.category, "name": r.name,
            "prep_time": r.prep_time, "shelf_life": r.shelf_life,
            "batch_sizes": bsizes, "ingredients": ings,
            "english_instructions": r.english_instructions,
            "spanish_instructions": r.spanish_instructions, "notes": r.notes,
        }
        return render_template(
            "recipes.html", recipes=[], recipe=recipe, form_mode="edit",
            categories=[{"slug": c, "label": c.title()} for c in _RECIPE_CATEGORIES],
            active="recipes",
        )
    finally:
        db.close()


@store_bp.route("/recipes/<int:recipe_id>", methods=["POST"])
def recipes_update(recipe_id: int):
    if not _kitchen_dashboard_ok():
        abort(403)
    import json as _json
    from app.models import Recipe
    db = next(get_db())
    try:
        r = db.get(Recipe, recipe_id)
        if r is None:
            abort(404)
        ing_raw = (request.form.get("ingredients_json") or "").strip()
        bsz_raw = (request.form.get("batch_sizes_csv") or "").strip()
        try:
            ing = _json.loads(ing_raw) if ing_raw else []
        except Exception:
            ing = []
        bsz = [s.strip() for s in bsz_raw.split(",") if s.strip()] if bsz_raw else []
        r.code = (request.form.get("code") or "").strip()[:20] or r.code
        r.category = (request.form.get("category") or "").strip()[:40] or r.category
        r.name = (request.form.get("name") or "").strip()[:200] or r.name
        r.prep_time = (request.form.get("prep_time") or "").strip()[:80] or None
        r.shelf_life = (request.form.get("shelf_life") or "").strip()[:80] or None
        r.english_instructions = (request.form.get("english_instructions") or "").strip() or None
        r.spanish_instructions = (request.form.get("spanish_instructions") or "").strip() or None
        r.ingredients_json = _json.dumps(ing) if ing else None
        r.batch_sizes_json = _json.dumps(bsz) if bsz else None
        r.notes = (request.form.get("notes") or "").strip() or None
        db.commit()
        return redirect(url_for("store.recipes_detail", recipe_id=recipe_id))
    finally:
        db.close()


# ============================================================
# FRESH FOOD — Sam dev #3074 + /sam/chat #1120-#1144. Cross-store
# visibility (no store_scope filter on reads), audience = everyone
# except expo. Rolling 7-day grid for Place Order, ACTIVE/COMPLETED
# state on Recent Orders, rolling-30-day-avg backend-computed,
# CSV report export.
# ============================================================
_FRESH_FOOD_ITEMS = [
    ("MEAT",     ["beef-fajita", "chicken-fajita", "ribs", "cochinita", "ground-beef", "pollo-ranchero"]),
    ("SAUCES",   ["poblano", "queso-dzlf", "chili-gravy", "seafood", "ranchera", "bbq",
                  "tomatillo", "street-taco", "cilantro-ginger", "chipotle-mayo", "chipotle-cream"]),
    ("BEANS",    ["black", "charros", "charros-mix", "refried"]),
    ("MISC",     ["spinach", "mexican-butter", "steam-vegetables", "chicken-stock",
                  "masa-flour", "empanadas", "stuffed-jalapenos"]),
    ("FOH",      ["red-sauce", "green-sauce", "chips"]),
    ("NON PREP", ["burger-beef", "taco-crispy", "tamales", "sausage"]),
    ("SEAFOOD",  ["cancun", "shrimp-salad"]),
]


# Fresh-food PLACE-ORDER layout (samai #3, Sam Kitchen-dashboard batch):
# reorganized categories + a per-item Size label for the order page. Slugs
# reuse existing item slugs where the item matches (so rolling-avg history
# carries over); new items get new slugs. The legacy _FRESH_FOOD_ITEMS above
# is kept as-is for the recent-orders view + flat-slug helpers.
_FRESH_FOOD_ORDER_LAYOUT = [
    {"name": "MEAT", "items": [
        {"slug": "beef-fajita",    "label": "Beef Fajita",    "size": "Tray"},
        {"slug": "chicken-fajita", "label": "Chicken Fajita", "size": "Tray"},
        {"slug": "ribs",           "label": "Ribs",           "size": "Single"},
        {"slug": "cochinita",      "label": "Cochinita",      "size": "Tray"},
        {"slug": "ground-beef",    "label": "Ground Beef",    "size": "Bag"},
        {"slug": "pollo-ranchero", "label": "Pollo Ranchero", "size": "Bag"},
        {"slug": "cancun",         "label": "Cancun",         "size": "4-pack"},
        {"slug": "shrimp-salad",   "label": "Shrimp",         "size": "8-pack"},
        {"slug": "sausage",        "label": "Sausage",        "size": "Box"},
        {"slug": "burger-beef",    "label": "Burger Beef",    "size": "Single"},
    ]},
    {"name": "SAUCES", "items": [
        {"slug": "bbq",             "label": "BBQ",             "size": "Bag"},
        {"slug": "chipotle-cream",  "label": "Chipotle Cream",  "size": "Bag"},
        {"slug": "chipotle-mayo",   "label": "Chipotle Mayo",   "size": "Bag"},
        {"slug": "chili-gravy",     "label": "Chili Gravy",     "size": "Bag"},
        {"slug": "chicken-stock",   "label": "Chicken Stock",   "size": "Bag"},
        {"slug": "cilantro-ginger", "label": "Cilantro Ginger", "size": "Bag"},
        {"slug": "poblano",         "label": "Poblano",         "size": "Bag"},
        {"slug": "queso-dzlf",      "label": "Queso Dip",       "size": "Bag"},
        {"slug": "ranchera",        "label": "Ranchera",        "size": "Bag"},
        {"slug": "seafood",         "label": "Seafood",         "size": "Bag"},
        {"slug": "street-taco",     "label": "Street Taco",     "size": "Bag"},
        {"slug": "tomatillo",       "label": "Tomatillo",       "size": "Bag"},
    ]},
    {"name": "BEANS", "items": [
        {"slug": "black",       "label": "Black",       "size": "Bag"},
        {"slug": "charros",     "label": "Charros",     "size": "Bag"},
        {"slug": "charros-mix", "label": "Charros Mix", "size": "Bag"},
        {"slug": "refried",     "label": "Refried",     "size": "Bag"},
    ]},
    {"name": "DRESSING", "items": [
        {"slug": "ranchera-dressing", "label": "Ranchera",      "size": "Bag"},
        {"slug": "sweet-ginger",      "label": "Sweet Ginger",  "size": "Bag"},
        {"slug": "honey-mustard",     "label": "Honey Mustard", "size": "Bag"},
        {"slug": "avocado-ranch",     "label": "Avocado Ranch", "size": "Bag"},
    ]},
    {"name": "MISC", "items": [
        {"slug": "spinach",           "label": "Spinach",           "size": "Bag"},
        {"slug": "steam-vegetables",  "label": "Steam Vegetables",  "size": "Bag"},
        {"slug": "mexican-butter",    "label": "Mexican Butter",    "size": ""},
        {"slug": "empanadas",         "label": "Empanadas",         "size": "Single"},
        {"slug": "stuffed-jalapenos", "label": "Stuffed Jalapeños", "size": "Single"},
        {"slug": "masa-flour",        "label": "Masa Flour",        "size": "Tray"},
        {"slug": "taco-crispy",       "label": "Taco Crispy",       "size": "Box"},
        {"slug": "tamales",           "label": "Tamales",           "size": "Box"},
    ]},
    {"name": "FOH", "items": [
        {"slug": "red-sauce",   "label": "Red Sauce",   "size": "Bag"},
        {"slug": "green-sauce", "label": "Green Sauce", "size": "Bag"},
        {"slug": "chips",       "label": "Chips",       "size": "Box"},
    ]},
]


def _ff_items_flat():
    out = []
    for cat, items in _FRESH_FOOD_ITEMS:
        for slug in items:
            label = slug.replace("-", " ").title()
            out.append({"slug": slug, "label": label, "category": cat})
    return out


def _ff_rolling_avg_by_slug(db, days: int = 30):
    """Compute rolling N-day average OR quantity per item slug across
    all stores. Returns {slug: float_avg}."""
    from datetime import timedelta
    from app.models import FreshFoodOrderLine, FreshFoodOrder
    from sqlalchemy import func
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (db.query(FreshFoodOrderLine.item_slug,
                     func.avg(FreshFoodOrderLine.or_qty))
              .join(FreshFoodOrder, FreshFoodOrder.id == FreshFoodOrderLine.order_id)
              .filter(FreshFoodOrder.placed_at >= cutoff)
              .filter(FreshFoodOrderLine.or_qty.isnot(None))
              .group_by(FreshFoodOrderLine.item_slug)
              .all())
    return {slug: float(avg or 0) for slug, avg in rows}


def _ff_catalog_lookup():
    """Fresh-food item metadata keyed by slug, using the Place Order
    tracking layout first and the legacy recent-order groups as fallback."""
    lookup: dict = {}
    for cat in _FRESH_FOOD_ORDER_LAYOUT:
        for item in cat["items"]:
            lookup[item["slug"]] = {
                "label": item["label"],
                "category": cat["name"],
                "size": item.get("size") or "",
            }
    for cat, items in _FRESH_FOOD_ITEMS:
        for slug in items:
            lookup.setdefault(slug, {
                "label": slug.replace("-", " ").title(),
                "category": cat,
                "size": "",
            })
    return lookup


def _ff_target_order_date(raw: str | None = None):
    """Return (local_today, delivery_date). Default delivery is tomorrow."""
    today = _attn_now().date()
    delivery_date = today + timedelta(days=1)
    raw = (raw or "").strip()
    if raw:
        try:
            delivery_date = datetime.fromisoformat(raw).date()
        except Exception:
            pass
    return today, delivery_date


def _ff_prior_weekday_dates(target_date, days: int = 30):
    """Dates in the prior N-day window that match target_date's weekday."""
    start = target_date - timedelta(days=days)
    out = []
    cur = start
    while cur < target_date:
        if cur.weekday() == target_date.weekday():
            out.append(cur)
        cur += timedelta(days=1)
    return start, out


def _ff_daily_avg_by_slug(db, target_date, days: int = 30):
    """Day-specific 30-day usage average for planning the target delivery day.

    Formula: sum(COALESCE(sent_qty, or_qty)) for prior orders with the same
    delivery weekday, divided by the number of matching weekdays in the prior
    window. Missing-order days count as zero, which makes this a daily usage
    average instead of an average-of-orders.
    """
    from app.models import FreshFoodOrderLine, FreshFoodOrder

    window_start, source_days = _ff_prior_weekday_dates(target_date, days=days)
    divisor = max(len(source_days), 1)
    rows = (db.query(FreshFoodOrderLine, FreshFoodOrder)
              .join(FreshFoodOrder,
                    FreshFoodOrder.id == FreshFoodOrderLine.order_id)
              .filter(FreshFoodOrder.order_date >= window_start)
              .filter(FreshFoodOrder.order_date < target_date)
              .all())
    stats: dict = {}
    for ln, order in rows:
        if not order.order_date or order.order_date.weekday() != target_date.weekday():
            continue
        qty = ln.sent_qty if ln.sent_qty is not None else ln.or_qty
        if qty is None:
            continue
        slug = ln.item_slug
        d = stats.setdefault(slug, {
            "slug": slug,
            "total_usage": 0.0,
            "ordered_total": 0.0,
            "sent_total": 0.0,
            "line_count": 0,
            "order_ids": set(),
            "last_order_date": None,
        })
        d["total_usage"] += float(qty or 0)
        d["ordered_total"] += float(ln.or_qty or 0)
        d["sent_total"] += float(ln.sent_qty or 0)
        d["line_count"] += 1
        d["order_ids"].add(order.id)
        if order.order_date and (
            d["last_order_date"] is None or order.order_date > d["last_order_date"]
        ):
            d["last_order_date"] = order.order_date

    for d in stats.values():
        d["avg"] = d["total_usage"] / divisor
        d["order_count"] = len(d["order_ids"])
        d["source_day_count"] = len(source_days)
        d["order_ids"] = sorted(d["order_ids"])

    meta = {
        "days": days,
        "target_date": target_date,
        "target_weekday": target_date.strftime("%A"),
        "window_start": window_start,
        "window_end": target_date - timedelta(days=1),
        "source_days": source_days,
        "source_day_count": len(source_days),
        "formula": "SUM(COALESCE(sent_qty, or_qty)) / matching prior weekdays",
    }
    return stats, meta


def _ff_window_tracker(db, days: int):
    """For Recent Orders top section. Returns
    {slug: {'tomball': float, 'copperfield': float, 'total': float,
            'sent': float}} — sum(or_qty) per item per store, plus
    sum(sent_qty) per item across stores, over the window."""
    from datetime import timedelta
    from app.models import FreshFoodOrderLine, FreshFoodOrder
    from sqlalchemy import func
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (db.query(FreshFoodOrderLine.item_slug,
                     FreshFoodOrder.store_scope,
                     func.sum(FreshFoodOrderLine.or_qty))
              .join(FreshFoodOrder, FreshFoodOrder.id == FreshFoodOrderLine.order_id)
              .filter(FreshFoodOrder.placed_at >= cutoff)
              .filter(FreshFoodOrderLine.or_qty.isnot(None))
              .group_by(FreshFoodOrderLine.item_slug,
                        FreshFoodOrder.store_scope)
              .all())
    out: dict = {}
    for slug, store, total in rows:
        d = out.setdefault(slug, {'tomball': 0.0, 'copperfield': 0.0,
                                  'total': 0.0, 'sent': 0.0})
        t = float(total or 0)
        if store == 'tomball':
            d['tomball'] += t
        elif store == 'copperfield':
            d['copperfield'] += t
        d['total'] += t
    # SENT totals per item across stores — ck #8:46 added TOTAL SENT +
    # VARIANCE columns to the Recent Orders tracker.
    sent_rows = (db.query(FreshFoodOrderLine.item_slug,
                          func.sum(FreshFoodOrderLine.sent_qty))
                   .join(FreshFoodOrder,
                         FreshFoodOrder.id == FreshFoodOrderLine.order_id)
                   .filter(FreshFoodOrder.placed_at >= cutoff)
                   .filter(FreshFoodOrderLine.sent_qty.isnot(None))
                   .group_by(FreshFoodOrderLine.item_slug)
                   .all())
    for slug, sent in sent_rows:
        d = out.setdefault(slug, {'tomball': 0.0, 'copperfield': 0.0,
                                  'total': 0.0, 'sent': 0.0})
        d['sent'] += float(sent or 0)
    return out


def _ff_variance_rows(db, days: int = 30):
    """For Recent Orders middle section. Aggregates SENT vs ordered per
    item across fulfilled orders in window. Severity tag for flagging
    big gaps."""
    from datetime import timedelta
    from app.models import FreshFoodOrderLine, FreshFoodOrder
    from sqlalchemy import func
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (db.query(FreshFoodOrderLine.item_slug,
                     func.sum(FreshFoodOrderLine.or_qty),
                     func.sum(FreshFoodOrderLine.sent_qty))
              .join(FreshFoodOrder, FreshFoodOrder.id == FreshFoodOrderLine.order_id)
              .filter(FreshFoodOrder.placed_at >= cutoff)
              .filter(FreshFoodOrderLine.sent_qty.isnot(None))
              .group_by(FreshFoodOrderLine.item_slug)
              .all())
    out = []
    for slug, total_or, total_sent in rows:
        total_or = float(total_or or 0)
        total_sent = float(total_sent or 0)
        gap = total_sent - total_or
        gap_pct = (gap / total_or * 100.0) if total_or > 0 else 0.0
        sev = 'ok'
        if abs(gap_pct) >= 15:
            sev = 'high'
        elif abs(gap_pct) >= 5:
            sev = 'low'
        out.append({
            'slug': slug,
            'total_or': total_or,
            'total_sent': total_sent,
            'gap': gap,
            'gap_pct': gap_pct,
            'sev': sev,
        })
    out.sort(key=lambda r: -abs(r['gap']))
    return out


def _ff_lines_by_order(db, order_ids):
    """Returns {order_id: [lines]} for the given order_ids. Lines are
    plain dicts ready for JSON serialization (the fulfillment modal
    reads them via a data-attribute embed)."""
    from app.models import FreshFoodOrderLine
    if not order_ids:
        return {}
    rows = (db.query(FreshFoodOrderLine)
              .filter(FreshFoodOrderLine.order_id.in_(order_ids))
              .all())
    out: dict = {}
    for ln in rows:
        out.setdefault(ln.order_id, []).append({
            'id': ln.id,
            'item_slug': ln.item_slug,
            'item_category': ln.item_category or '',
            'inv_qty': ln.inv_qty,
            'or_qty': ln.or_qty,
            'sent_qty': ln.sent_qty,
        })
    return out


@store_bp.route("/fresh-food/place-order", methods=["GET"])
def fresh_food_place_order():
    if not _kitchen_dashboard_ok():
        abort(403)
    db = next(get_db())
    today, delivery_date = _ff_target_order_date(request.args.get("order_date"))
    try:
        avg_stats, avg_meta = _ff_daily_avg_by_slug(db, delivery_date, days=30)
        avg_by_slug = {slug: row["avg"] for slug, row in avg_stats.items()}
    finally:
        db.close()
    return render_template(
        "fresh_food_place_order.html",
        categories=_FRESH_FOOD_ORDER_LAYOUT,
        today_date=today,
        delivery_date=delivery_date,
        avg_meta=avg_meta,
        rolling_avg_by_slug=avg_by_slug,
        active="fresh_food_place_order",
    )


@store_bp.route("/fresh-food/place-order", methods=["POST"])
def fresh_food_place_order_submit():
    if not _kitchen_dashboard_ok():
        abort(403)
    from app.models import FreshFoodOrder, FreshFoodOrderLine
    body = request.get_json(silent=True) or {}
    od_str = (body.get("order_date") or "").strip()
    today, default_delivery_date = _ff_target_order_date()
    try:
        order_date = datetime.fromisoformat(od_str).date() if od_str else default_delivery_date
    except Exception:
        order_date = default_delivery_date
    lines = body.get("lines") or []
    user = getattr(g, "current_user", None)
    db = next(get_db())
    try:
        order = FreshFoodOrder(
            order_date=order_date,
            store_scope=(g.current_location if g.current_location in ("tomball", "copperfield") else None),
            placed_by_user_id=(user.id if user else None),
            placed_by_name=(getattr(user, "full_name", None) if user else None),
            status="active",
        )
        db.add(order)
        db.flush()
        for ln in lines:
            try:
                slug = (ln.get("item_slug") or "").strip()[:60]
                if not slug:
                    continue
                db.add(FreshFoodOrderLine(
                    order_id=order.id,
                    item_slug=slug,
                    item_category=(ln.get("category") or "").strip()[:40] or None,
                    inv_qty=_coerce_float(ln.get("inv")),
                    or_qty=_coerce_float(ln.get("or")),
                ))
            except Exception:
                continue
        db.commit()
        return jsonify({"ok": True, "order_id": order.id})
    finally:
        db.close()


@store_bp.route("/fresh-food/developer", methods=["GET"])
def fresh_food_developer():
    if not _kitchen_dashboard_ok():
        abort(403)
    from app.models import FreshFoodOrder, FreshFoodOrderLine

    today, delivery_date = _ff_target_order_date(request.args.get("order_date"))
    catalog = _ff_catalog_lookup()
    db = next(get_db())
    try:
        orders = (db.query(FreshFoodOrder)
                    .order_by(FreshFoodOrder.placed_at.desc())
                    .limit(100).all())
        lines_by_order = _ff_lines_by_order(db, [o.id for o in orders])
        avg_stats, avg_meta = _ff_daily_avg_by_slug(db, delivery_date, days=30)

        item_stats: dict = {}
        cutoff = datetime.utcnow() - timedelta(days=30)
        item_rows = (db.query(FreshFoodOrderLine, FreshFoodOrder)
                       .join(FreshFoodOrder,
                             FreshFoodOrder.id == FreshFoodOrderLine.order_id)
                       .filter(FreshFoodOrder.placed_at >= cutoff)
                       .all())
        for ln, order in item_rows:
            meta = catalog.get(ln.item_slug, {})
            d = item_stats.setdefault(ln.item_slug, {
                "slug": ln.item_slug,
                "label": meta.get("label") or ln.item_slug.replace("-", " ").title(),
                "category": meta.get("category") or ln.item_category or "",
                "size": meta.get("size") or "",
                "ordered": 0.0,
                "sent": 0.0,
                "usage": 0.0,
                "on_hand_entries": 0,
                "line_count": 0,
                "orders": set(),
                "last_placed_at": None,
            })
            ordered = float(ln.or_qty or 0)
            sent = float(ln.sent_qty or 0)
            usage = float((ln.sent_qty if ln.sent_qty is not None else ln.or_qty) or 0)
            d["ordered"] += ordered
            d["sent"] += sent
            d["usage"] += usage
            d["on_hand_entries"] += 1 if ln.inv_qty is not None else 0
            d["line_count"] += 1
            d["orders"].add(order.id)
            if order.placed_at and (
                d["last_placed_at"] is None or order.placed_at > d["last_placed_at"]
            ):
                d["last_placed_at"] = order.placed_at
        for d in item_stats.values():
            d["order_count"] = len(d["orders"])
            d["orders"] = sorted(d["orders"])
            d["last_placed_local"] = _central_dt(d["last_placed_at"])

        order_summaries = []
        orderer_stats: dict = {}
        hour_stats: dict = {}
        review_minutes = []
        total_units_ordered = 0.0
        total_units_sent = 0.0
        total_line_count = 0
        for o in orders:
            olines = lines_by_order.get(o.id, [])
            units_ordered = sum(float(ln.get("or_qty") or 0) for ln in olines)
            units_sent = sum(float(ln.get("sent_qty") or 0) for ln in olines)
            total_units_ordered += units_ordered
            total_units_sent += units_sent
            total_line_count += len(olines)
            placed_local = _central_dt(o.placed_at)
            completed_local = _central_dt(o.fulfilled_at)
            minutes = None
            if o.placed_at and o.fulfilled_at:
                minutes = round((o.fulfilled_at - o.placed_at).total_seconds() / 60.0, 1)
                review_minutes.append(minutes)
            if placed_local:
                hour_stats[placed_local.hour] = hour_stats.get(placed_local.hour, 0) + 1
            orderer = o.placed_by_name or "Unknown"
            od = orderer_stats.setdefault(orderer, {
                "name": orderer,
                "orders": 0,
                "items": 0,
                "units_ordered": 0.0,
                "units_sent": 0.0,
                "last_placed_local": None,
            })
            od["orders"] += 1
            od["items"] += len(olines)
            od["units_ordered"] += units_ordered
            od["units_sent"] += units_sent
            if placed_local and (
                od["last_placed_local"] is None or placed_local > od["last_placed_local"]
            ):
                od["last_placed_local"] = placed_local
            order_summaries.append({
                "id": o.id,
                "placed_local": placed_local,
                "order_date": o.order_date,
                "store_scope": o.store_scope or "both",
                "placed_by": orderer,
                "status": o.status,
                "completed_local": completed_local,
                "fulfilled_by": o.fulfilled_by_name or "",
                "sent_date": o.sent_date,
                "item_count": len(olines),
                "units_ordered": units_ordered,
                "units_sent": units_sent,
                "review_minutes": minutes,
            })

        avg_rows = []
        for cat in _FRESH_FOOD_ORDER_LAYOUT:
            for item in cat["items"]:
                st = avg_stats.get(item["slug"], {})
                avg_rows.append({
                    "slug": item["slug"],
                    "label": item["label"],
                    "category": cat["name"],
                    "size": item.get("size") or "",
                    "avg": float(st.get("avg") or 0),
                    "total_usage": float(st.get("total_usage") or 0),
                    "ordered_total": float(st.get("ordered_total") or 0),
                    "sent_total": float(st.get("sent_total") or 0),
                    "line_count": int(st.get("line_count") or 0),
                    "order_count": int(st.get("order_count") or 0),
                    "last_order_date": st.get("last_order_date"),
                })

        summary = {
            "order_count": len(orders),
            "active_count": sum(1 for o in orders if o.status == "active"),
            "completed_count": sum(1 for o in orders if o.status == "completed"),
            "line_count": total_line_count,
            "units_ordered": total_units_ordered,
            "units_sent": total_units_sent,
            "orderer_count": len(orderer_stats),
            "avg_review_minutes": (
                sum(review_minutes) / len(review_minutes) if review_minutes else None
            ),
        }
        hour_rows = [
            {"hour": hour, "label": f"{((hour - 1) % 12) + 1} {'AM' if hour < 12 else 'PM'}", "orders": count}
            for hour, count in sorted(hour_stats.items())
        ]
        item_rows_sorted = sorted(
            item_stats.values(),
            key=lambda r: (-r["usage"], r["label"]),
        )
        orderer_rows = sorted(
            orderer_stats.values(),
            key=lambda r: (-r["orders"], r["name"]),
        )
        return render_template(
            "fresh_food_developer.html",
            today_date=today,
            delivery_date=delivery_date,
            summary=summary,
            orders=order_summaries,
            item_rows=item_rows_sorted,
            orderer_rows=orderer_rows,
            hour_rows=hour_rows,
            avg_rows=avg_rows,
            avg_meta=avg_meta,
            active="fresh_food_developer",
        )
    finally:
        db.close()


@store_bp.route("/fresh-food/recent-orders", methods=["GET"])
def fresh_food_recent_orders():
    if not _kitchen_dashboard_ok():
        abort(403)
    from app.models import FreshFoodOrder
    db = next(get_db())
    try:
        rows = (db.query(FreshFoodOrder)
                  .order_by(FreshFoodOrder.placed_at.desc())
                  .limit(100).all())
        tracker_7d = _ff_window_tracker(db, days=7)
        tracker_30d = _ff_window_tracker(db, days=30)
        variance_rows = _ff_variance_rows(db, days=30)
        avg_by_slug = _ff_rolling_avg_by_slug(db, days=30)
        lines_by_order = _ff_lines_by_order(db, [o.id for o in rows])
        return render_template(
            "fresh_food_recent_orders.html",
            orders=rows,
            categories=_FRESH_FOOD_ITEMS,
            tracker_7d=tracker_7d,
            tracker_30d=tracker_30d,
            variance_rows=variance_rows,
            rolling_avg_by_slug=avg_by_slug,
            lines_by_order=lines_by_order,
            active="fresh_food_recent_orders",
        )
    finally:
        db.close()


@store_bp.route("/fresh-food/recent-orders/<int:order_id>/fulfill",
                methods=["POST"])
def fresh_food_recent_orders_fulfill(order_id: int):
    if not _kitchen_dashboard_ok():
        abort(403)
    from app.models import FreshFoodOrder, FreshFoodOrderLine
    body = request.get_json(silent=True) or {}
    user = getattr(g, "current_user", None)
    db = next(get_db())
    try:
        order = db.get(FreshFoodOrder, order_id)
        if order is None:
            abort(404)
        order.fulfilled_at = datetime.utcnow()
        order.fulfilled_by_user_id = (user.id if user else None)
        order.fulfilled_by_name = (
            body.get("fulfilled_by_name") or "").strip()[:120] or None
        sd = (body.get("sent_date") or "").strip()
        try:
            order.sent_date = datetime.fromisoformat(sd).date() if sd else None
        except Exception:
            order.sent_date = None
        sent_lines = body.get("sent_lines") or {}
        for line_id_str, sent_qty in sent_lines.items():
            try:
                line_id = int(line_id_str)
                ln = db.get(FreshFoodOrderLine, line_id)
                if ln and ln.order_id == order_id:
                    ln.sent_qty = _coerce_float(sent_qty)
            except Exception:
                continue
        # Mark COMPLETED if every line has a sent_qty
        all_filled = all(
            ln.sent_qty is not None
            for ln in db.query(FreshFoodOrderLine)
                        .filter(FreshFoodOrderLine.order_id == order_id).all()
        )
        if all_filled:
            order.status = "completed"
        db.commit()
        return jsonify({"ok": True, "status": order.status})
    finally:
        db.close()


@store_bp.route("/fresh-food/recent-orders/report.csv", methods=["GET"])
def fresh_food_recent_orders_report():
    if not _kitchen_dashboard_ok():
        abort(403)
    import csv
    import io as _io
    from app.models import FreshFoodOrder, FreshFoodOrderLine
    db = next(get_db())
    try:
        q = (db.query(FreshFoodOrderLine, FreshFoodOrder)
               .join(FreshFoodOrder, FreshFoodOrder.id == FreshFoodOrderLine.order_id))
        from_str = request.args.get("from", "").strip()
        to_str = request.args.get("to", "").strip()
        item = request.args.get("item", "").strip()
        if from_str:
            try:
                q = q.filter(FreshFoodOrder.placed_at >= datetime.fromisoformat(from_str))
            except Exception:
                pass
        if to_str:
            try:
                q = q.filter(FreshFoodOrder.placed_at <= datetime.fromisoformat(to_str))
            except Exception:
                pass
        if item:
            q = q.filter(FreshFoodOrderLine.item_slug == item)
        q = q.order_by(FreshFoodOrder.placed_at.desc()).limit(5000)
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(["placed_at", "order_date", "store", "placed_by",
                    "status", "item", "inv_qty", "or_qty", "sent_qty",
                    "fulfilled_at", "fulfilled_by"])
        for ln, order in q.all():
            w.writerow([
                order.placed_at.isoformat() if order.placed_at else "",
                order.order_date.isoformat() if order.order_date else "",
                order.store_scope or "",
                order.placed_by_name or "",
                order.status,
                ln.item_slug,
                ln.inv_qty if ln.inv_qty is not None else "",
                ln.or_qty if ln.or_qty is not None else "",
                ln.sent_qty if ln.sent_qty is not None else "",
                order.fulfilled_at.isoformat() if order.fulfilled_at else "",
                order.fulfilled_by_name or "",
            ])
        return Response(
            buf.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition":
                     "attachment; filename=fresh_food_orders.csv"})
    finally:
        db.close()


@store_bp.route("/legal/<page>", methods=["GET"])
def legal_placeholder(page: str):
    """Placeholder for the new Legal section per Sam #837 item 8.
    Same shape as manager_placeholder — sidebar entries shipped first,
    real pages follow."""
    label = _LEGAL_PAGE_LABELS.get(page)
    if not label:
        abort(404)
    active_key = "legal_" + page.replace("-", "_")
    return render_template(
        "coming_soon.html",
        section_label="Legal",
        page_label=label,
        active=active_key,
    )


# ============================================================
# KITCHEN placeholders (Sam #837 item 17 — sidebar entries ship
# before the underlying Fresh Food / Prep List / Recipes pages).
# ============================================================
_KITCHEN_PAGE_LABELS = {
    "fresh-food":  "Fresh Food",
    "prep-list":   "Prep List",
    "recipes":     "Recipes",
}


@store_bp.route("/kitchen/prep-list", methods=["GET"])
def kitchen_prep_list():
    """Prep List v3 — kitchen's daily prep board. Registered before
    the /kitchen/<page> placeholder so Flask matches it first."""
    db = next(get_db())
    try:
        return _render_prep_list_v3(db, "Prep List", "kitchen_prep_list")
    finally:
        db.close()


@store_bp.route("/kitchen/prep-list", methods=["POST"])
def kitchen_prep_list_post():
    """All POSTs from the Prep List page (hidden form_action)."""
    db = next(get_db())
    try:
        user = getattr(g, "current_user", None)
        store_scope = (g.current_location
                       if g.current_location in ("tomball", "copperfield")
                       else None)
        _prep_list_v3_post(db, store_scope, user)
    finally:
        db.close()
    redirect_args = {}
    view_date = request.form.get("view_date") or None
    if view_date:
        redirect_args["date"] = view_date
    tab = (request.form.get("active_tab") or "board").strip().lower()
    if tab in ("board", "recent", "performance", "developer"):
        redirect_args["tab"] = tab
    open_item = (request.form.get("open_item_id") or request.form.get("item_id") or "").strip()
    if open_item and tab == "board":
        redirect_args["open"] = open_item
    return redirect(url_for("store.kitchen_prep_list", **redirect_args))


@store_bp.route("/kitchen/<page>", methods=["GET"])
def kitchen_placeholder(page: str):
    """Placeholder for the new Kitchen branch under Operations."""
    label = _KITCHEN_PAGE_LABELS.get(page)
    if not label:
        abort(404)
    active_key = "kitchen_" + page.replace("-", "_")
    return render_template(
        "coming_soon.html",
        section_label="Kitchen",
        page_label=label,
        active=active_key,
    )


# ============================================================
# IN-HOUSE CATERING (Sam #837 item 16 + cena #1031 2026-05-19)
# Staff tool: build a custom-priced quote off the Cenas Fajitas
# Tomball menu (mirrored from ezCater, prices zeroed for staff
# to enter). Two checkout flows: Quote (email to customer) and
# Payment (Pay Now → ezOrder / Pay Later → placeholder fields).
# Frontend page is built by ck. Backend routes:
#   GET  /<store>/in-house-catering            — picker page
#   GET  /<store>/in-house-catering/menu.json  — menu data API
#   POST /<store>/in-house-catering/quote      — create quote + email
#   POST /<store>/in-house-catering/pay-now    — create ezOrder
# ============================================================
@store_bp.route("/in-house-catering", methods=["GET"])
def in_house_catering_page():
    """Render the In-House Catering picker page (ck-built template).
    Falls back to coming_soon if the template hasn't landed yet."""
    try:
        return render_template(
            "in_house_catering.html",
            active="in_house_catering",
        )
    except Exception:
        return render_template(
            "coming_soon.html",
            section_label="Catering",
            page_label="In-House",
            active="in_house_catering",
        )


@store_bp.route("/in-house-catering/menu.json", methods=["GET"])
def in_house_catering_menu_json():
    """Return the Cenas Fajitas Tomball menu as JSON for the picker UI."""
    from app.data.in_house_catering_menu import CATEGORIES
    return jsonify({"categories": CATEGORIES})


@store_bp.route("/in-house-catering/quote", methods=["POST"])
def in_house_catering_quote_create():
    """Create an InHouseCateringQuote from the picker's selection +
    customer info. If `send_email=True` in the body, also email the
    customer the quote summary. Returns the new quote id."""
    import json as _json
    from app.models import InHouseCateringQuote
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    db = next(get_db())
    try:
        q = InHouseCateringQuote(
            created_by_user_id = (g.current_user.id if getattr(g, "current_user", None) else None),
            store_scope        = (g.current_location if g.current_location in ("tomball","copperfield") else None),
            customer_name      = (body.get("customer_name") or "").strip() or None,
            customer_email     = (body.get("customer_email") or "").strip() or None,
            customer_phone     = (body.get("customer_phone") or "").strip() or None,
            event_address      = (body.get("event_address") or "").strip() or None,
            guest_count        = body.get("guest_count"),
            items_json         = _json.dumps(items, ensure_ascii=False),
            subtotal           = _coerce_float(body.get("subtotal")),
            notes              = (body.get("notes") or "").strip() or None,
            status             = "draft",
        )
        # Optional event_date (ISO string)
        ed = body.get("event_date")
        if ed:
            try:
                q.event_date = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            except Exception:
                pass
        db.add(q)
        db.commit()
        db.refresh(q)
        # Email send is a follow-up (cena #1031 mentioned email-on-Quote).
        # Wiring real SMTP send is iterative; the row exists either way.
        if body.get("send_email") and q.customer_email:
            try:
                _email_in_house_quote(q)
                q.status = "sent"
                q.email_sent_at = datetime.utcnow()
                db.commit()
            except Exception:
                # Quote row is preserved even if email fails; staff can resend.
                logging.getLogger(__name__).exception(
                    "in_house_catering: email send failed for quote %s", q.id)
        return jsonify({"ok": True, "quote_id": q.id, "status": q.status})
    finally:
        db.close()


@store_bp.route("/in-house-catering/pay-now", methods=["POST"])
def in_house_catering_pay_now():
    """Mark an existing InHouseCateringQuote as Pay Now pending. Body:
    {quote_id}. Full promotion to an ezOrder row (with the In-House
    indicator on /partner/orders) is a follow-up wave — for v1 this
    endpoint just transitions status so the frontend can confirm
    submission.  Returns the quote id."""
    from app.models import InHouseCateringQuote
    body = request.get_json(silent=True) or {}
    qid = body.get("quote_id")
    if not qid:
        return jsonify({"error": "quote_id required"}), 400
    db = next(get_db())
    try:
        q = db.get(InHouseCateringQuote, int(qid))
        if not q:
            return jsonify({"error": "quote not found"}), 404
        q.status = "pay_now_pending"
        db.commit()
        return jsonify({"ok": True, "quote_id": q.id, "status": q.status})
    finally:
        db.close()


def _coerce_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _email_in_house_quote(q):
    """Stub — wire the real SMTP send pipeline once the customer-facing
    email template + sending infra is finalized. For now, raise so the
    caller treats the email as not-yet-sent (quote stays in draft)."""
    raise NotImplementedError("in_house_catering email send not yet wired")


@store_bp.route("/drivers/<int:driver_id>/toggle-active", methods=["POST"])
def drivers_toggle_active(driver_id: int):
    db = next(get_db())
    try:
        row = db.get(Driver, driver_id)
        if not row or (g.current_location != "both" and row.location != g.current_location):
            return redirect(url_for("store.drivers_admin", error="Driver not found at this store."))
        row.active = not row.active
        db.commit()
        return redirect(url_for("store.drivers_admin"))
    finally:
        db.close()


# ============== OPERATIONS — SCHEDULE ==============

# ---- Weekly Schedule v3 (2026-05-20) --------------------------------
# The Weekly Schedule page is a 7-day grid built from AttendanceShift
# rows (manager_attendance_shift) — the same table behind the daily
# Attendance Tracking board. One AttendanceShift = one teammate's shift
# for one day; a week is those rows across Mon..Sun, grouped by
# teammate. Renders cleanly empty until shifts are logged.

# Position groups — map a free-text role_title onto one of the eight
# colour buckets the grid + cards use (mgmt/prep/line/utility/floor/
# bar/special/driver). Keys are lowercase substrings tested against the
# role title; first match wins, default 'floor'.
_SCHED_ROLE_GROUPS = [
    ("driver", "driver"),
    ("dishwash", "utility"),
    ("busser", "utility"),
    ("prep", "prep"),
    ("enchilada", "prep"),
    ("grill", "line"),
    ("expo", "line"),
    ("window", "special"),
    ("train", "special"),
    ("bartender", "bar"),
    ("well", "bar"),
    ("bar", "bar"),
    ("host", "floor"),
    ("cashier", "floor"),
    ("server", "floor"),
    ("manager", "mgmt"),
    ("gm", "mgmt"),
    ("km", "mgmt"),
    ("lead", "mgmt"),
]


def _sched_role_group(role_title):
    """Bucket a role title into one of the eight grid colour groups."""
    r = (role_title or "").strip().lower()
    for needle, group in _SCHED_ROLE_GROUPS:
        if needle in r:
            return group
    return "floor"


# Front-of-house vs back-of-house, derived from the colour group — used
# to section the grid when the source (Toast) gives only a job title,
# not an explicit FOH/BOH flag.
_SCHED_FOH_GROUPS = {"mgmt", "floor", "bar", "special"}


def _sched_section_for_group(group):
    return "foh" if group in _SCHED_FOH_GROUPS else "boh"


def _sched_week_start(d):
    """Monday of the week containing date `d`."""
    return d - timedelta(days=d.weekday())


def _sched_fmt_time(dt):
    """'10:00' / '7:00' from a datetime, or None — drops leading zero,
    no AM/PM (the grid is compact; the shift card shows a range)."""
    if not dt:
        return None
    return dt.strftime("%I:%M").lstrip("0")


def _sched_shift_hours(start, end):
    """Whole/short hour string ('9h', '8h 30m') between two datetimes,
    or None. Used for the per-shift and per-week hour totals."""
    if not start or not end or end <= start:
        return None
    mins = int((end - start).total_seconds() // 60)
    h, m = mins // 60, mins % 60
    return f"{h}h" if m == 0 else f"{h}h {m}m"


def _sched_minutes(start, end):
    if not start or not end or end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


@store_bp.route("/schedule/weekly")
def weekly_schedule():
    """Weekly Schedule — a 7-day (Mon..Sun) grid of who worked which
    days, sourced live from Toast (the same Toast labor API behind the
    Attendance board). ?date=<iso> picks any day in the target week
    (default: the current week). Manager-tier audience gate, expo +
    drivers excluded — same gate as the other manager pages. If Toast
    is unreachable the grid falls back to manually-logged
    AttendanceShift rows + a notice, so it is never blank."""
    if not _operations_full_access_ok():
        abort(403)

    raw = request.args.get("date") or ""
    try:
        anchor = date.fromisoformat(raw) if raw else _local_today()
    except ValueError:
        anchor = _local_today()
    week_start = _sched_week_start(anchor)
    week_end = week_start + timedelta(days=6)
    today = _local_today()
    loc = g.current_location

    # The 7 day columns.
    days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        days.append({
            "iso": d.isoformat(),
            "dow": d.strftime("%a"),
            "dom": d.day,
            "label": f"{d:%b} {d.day}",
            "is_today": d == today,
            "index": i,
        })
    days_by_iso = {d["iso"]: d for d in days}

    # Each teammate row carries a 7-slot `cells` list aligned to `days`
    # (None = day off / not worked).
    emp = {}
    schedule_notice = None

    # Primary source: live Toast clock data for the week.
    try:
        from app.services.toast_reports import weekly_schedule_status
        tloc = loc if loc in ("tomball", "copperfield") else None
        for t in weekly_schedule_status(week_start, location_filter=tloc):
            name = (t.get("name") or "").strip()
            if not name:
                continue
            group = _sched_role_group(t.get("job_title"))
            rec = emp.get(name)
            if rec is None:
                rec = emp[name] = {
                    "name": name,
                    "section": _sched_section_for_group(group),
                    "role_title": t.get("job_title") or "Team member",
                    "group": group,
                    "cells": [None] * 7,
                    "total_minutes": 0,
                    "shift_count": 0,
                }
            for diso, dd in (t.get("days") or {}).items():
                col = days_by_iso.get(diso)
                if col is None:
                    continue
                idx = col["index"]
                if rec["cells"][idx] is not None:
                    continue  # same teammate at 2 locations — keep first
                start_s = _sched_fmt_time(dd.get("clock_in"))
                end_s = _sched_fmt_time(dd.get("clock_out"))
                mins = dd.get("minutes") or 0
                hh, mm = mins // 60, mins % 60
                rec["cells"][idx] = {
                    "start": start_s,
                    "end": end_s,
                    "range": (f"{start_s} — {end_s}" if (start_s and end_s)
                              else (start_s or end_s
                                    or ("On the clock"
                                        if dd.get("status") == "open"
                                        else "Worked"))),
                    "hours": ((f"{hh}h" if mm == 0 else f"{hh}h {mm}m")
                              if mins else None),
                    "role": rec["role_title"],
                    "group": rec["group"],
                    "status": dd.get("status") or "worked",
                    "note": None,
                }
                rec["total_minutes"] += mins
                rec["shift_count"] += 1
    except Exception as ex:
        logging.getLogger(__name__).warning(
            "weekly_schedule: Toast unavailable, falling back to "
            "manually-logged shifts: %s", ex)
        schedule_notice = ("Live Toast clock data is unavailable right now "
                           "— showing manually logged shifts only.")
        emp = {}
        from app.models import AttendanceShift
        db = next(get_db())
        try:
            q = db.query(AttendanceShift).filter(
                AttendanceShift.entry_date >= week_start,
                AttendanceShift.entry_date <= week_end,
            )
            if loc in ("tomball", "copperfield"):
                q = q.filter((AttendanceShift.store_scope == loc) |
                             (AttendanceShift.store_scope.is_(None)))
            shifts = q.all()
        finally:
            db.close()
        for s in shifts:
            name = (s.employee_name or "").strip()
            if not name:
                continue
            rec = emp.get(name)
            if rec is None:
                rec = emp[name] = {
                    "name": name,
                    "section": (s.section or "boh").lower(),
                    "role_title": s.role_title or "Team member",
                    "group": _sched_role_group(s.role_title),
                    "cells": [None] * 7,
                    "total_minutes": 0,
                    "shift_count": 0,
                }
            idx = (s.entry_date - week_start).days
            if not (0 <= idx <= 6):
                continue
            start_s = _sched_fmt_time(s.scheduled_start)
            end_s = _sched_fmt_time(s.scheduled_end)
            mins = _sched_minutes(s.scheduled_start, s.scheduled_end)
            rec["cells"][idx] = {
                "start": start_s,
                "end": end_s,
                "range": (f"{start_s} — {end_s}" if (start_s and end_s)
                          else (start_s or end_s or "Shift")),
                "hours": _sched_shift_hours(s.scheduled_start,
                                            s.scheduled_end),
                "role": s.role_title or rec["role_title"],
                "group": _sched_role_group(s.role_title) or rec["group"],
                "status": s.status,
                "note": s.note,
            }
            rec["total_minutes"] += mins
            rec["shift_count"] += 1
            if s.role_title:
                rec["role_title"] = s.role_title
                rec["group"] = _sched_role_group(s.role_title)
            if s.section:
                rec["section"] = s.section.lower()

    # Per-teammate hour totals + sort: section (FOH first), then role
    # group, then name — so the grid reads grouped like the rendering.
    _GROUP_ORDER = {"mgmt": 0, "floor": 1, "bar": 2, "special": 3,
                    "prep": 4, "line": 5, "utility": 6, "driver": 7}
    employees = []
    for rec in emp.values():
        h, m = rec["total_minutes"] // 60, rec["total_minutes"] % 60
        rec["total_hours"] = (f"{h}h" if m == 0 else f"{h}h {m}m")
        rec["total_hours_num"] = h + (1 if m else 0)
        parts = [p for p in rec["name"].split() if p]
        if not parts:
            rec["initials"] = "?"
        elif len(parts) == 1:
            rec["initials"] = parts[0][:2].upper()
        else:
            rec["initials"] = (parts[0][0] + parts[-1][0]).upper()
        employees.append(rec)
    employees.sort(key=lambda r: (
        0 if r["section"] == "foh" else 1,
        _GROUP_ORDER.get(r["group"], 9),
        r["name"].lower(),
    ))

    # KPI strip + per-day coverage counts.
    total_minutes = sum(r["total_minutes"] for r in employees)
    total_shifts = sum(r["shift_count"] for r in employees)
    th, tm = total_minutes // 60, total_minutes % 60
    for d in days:
        d["coverage"] = sum(1 for r in employees
                            if r["cells"][d["index"]] is not None)
    kpis = {
        "total_hours": (f"{th}h" if tm == 0 else f"{th}h {tm}m"),
        "shifts": total_shifts,
        "people": len(employees),
        "foh": sum(1 for r in employees if r["section"] == "foh"),
        "boh": sum(1 for r in employees if r["section"] != "foh"),
    }

    return render_template(
        "manager_schedule.html",
        active="weekly_schedule",
        days=days,
        employees=employees,
        kpis=kpis,
        schedule_notice=schedule_notice,
        week_start=week_start.isoformat(),
        week_end=week_end.isoformat(),
        week_label=f"{week_start:%b} {week_start.day} — {week_end:%b} {week_end.day}, {week_end.year}",
        week_short=f"{week_start:%b} {week_start.day} – {week_end.day}",
        prev_week=(week_start - timedelta(days=7)).isoformat(),
        next_week=(week_start + timedelta(days=7)).isoformat(),
        is_this_week=(week_start == _sched_week_start(today)),
        today_iso=today.isoformat(),
    )


@store_bp.route("/schedule")
@store_bp.route("/schedule/<view>")
def schedule(view: str = "weekly"):
    """Schedule (Sling). Children: BOH Roster / FOH Roster / All Roster / Weekly.
    Phase 1: 'weekly' shows the current schedule report; the BOH/FOH/All roster
    children are wired in Phase 2 with role classification.

    NOTE: /schedule/weekly is served by weekly_schedule() above — Flask
    matches that static rule ahead of this <view> converter, so the
    Weekly Schedule subnav card lands on the real grid page."""
    from app.web.reports import schedule as schedule_view
    g.location_override = g.current_location if g.current_location != "both" else None
    return schedule_view()


@store_bp.route("/roster")
@store_bp.route("/roster/<view>")
def roster(view: str = "all"):
    """Roster — BOH / FOH / All. Phase 1 wires through a `roster_filter` query
    param the existing /reports/roster route doesn't yet honor; harmless for now."""
    from app.web.reports import roster as roster_view
    g.location_override = g.current_location if g.current_location != "both" else None
    g.roster_filter = view  # 'boh' / 'foh' / 'all' — Phase 2 will use this
    return roster_view()


# ============== INSIGHTS — PERFORMANCE / SALES / LABOR ==============

@store_bp.route("/reports/server-performance")
@store_bp.route("/reports/server-performance/<role>")
def server_performance(role: str = "all"):
    """Performance. Children: Server / Bartenders / Prep / All. Phase 1 renders
    the existing report; Phase 2 will filter by role."""
    from app.web.reports import server_performance as view
    g.location_override = g.current_location if g.current_location != "both" else None
    g.role_filter = role
    return view()


@store_bp.route("/reports/labor")
@store_bp.route("/reports/labor/<which>")
def labor(which: str = "all"):
    """Labor. Children: BOH / FOH / All."""
    from app.web.reports import labor as view
    g.location_override = g.current_location if g.current_location != "both" else None
    g.labor_filter = which
    return view()


@store_bp.route("/reports/sales")
@store_bp.route("/reports/sales/<channel>")
def sales(channel: str = "all"):
    """Sales. Children: Toast (in-store) / Online Toast / Ezcater / DoorDash / Uber / Total.
    Phase 1 shows the third-party report; Phase 2 wires per-channel."""
    from app.web.reports import third_party_sales as view
    g.location_override = g.current_location if g.current_location != "both" else None
    g.sales_channel = channel
    return view()


# ---- Catering dashboard (tabbed entry layer, Sam 2026-05-21, dck) ----
# Structural twin of the Manager dashboard above. The bottom-nav
# Catering tab no longer opens a sub-option popover — it links straight
# here. This route renders catering_dashboard.html: a tab strip across
# the catering surfaces, defaulting to the Ez Orders tab.
#
# DESIGN-CHANGE rework (Sam 2026-05-21, dck): each tab no longer shows a
# read-only preview + an "Open full page" link. Instead each tab embeds
# the REAL, fully-functional catering page inline in an <iframe> — click
# a tab and the working page is right there. So this route no longer
# builds preview rows or pulls the Order / Driver / quote tables; it
# only needs each tab's URL for the iframe to load. The existing
# catering pages and routes are untouched — they are simply iframed.

# Ordered tab spec: (tab key, caption). The key matches the active_tab
# values catering_dashboard.html expects. First entry is the default
# tab. The per-tab url is built per-request by _catering_dash_full_url
# since several tabs point at store-scoped routes and the rest are flat.
_CATERING_DASH_TABS = [
    ("ez-orders",  "Ez Orders"),
    ("ez-market",  "Ez Market"),
    ("ez-manage",  "Ez Manage"),
    ("ez-drivers", "Ez Drivers"),
    ("in-house",   "In-House"),
    ("live-map",   "Live Map"),
]


def _catering_dash_full_url(tab_key):
    """Absolute href to the real catering page a tab embeds in its
    iframe.
      ez-orders  -> /<store>/orders        (store.orders_list)
      ez-market  -> /ez-market             (flat, not store-scoped)
      ez-manage  -> /ez-manage             (flat, not store-scoped)
      ez-drivers -> /<store>/drivers       (store.drivers_admin)
      in-house   -> /<store>/in-house-catering (store.in_house_catering_page)
      live-map   -> /partner/developer/ezcater-live-map (read-only watcher)
    The flat ez-market / ez-manage paths are written as literals because
    they live outside the /<store> blueprint; url_for on a store endpoint
    would prepend the slug. Falls back to the orders page on an unknown
    key so the iframe src is never empty."""
    if tab_key == "ez-orders":
        return url_for("store.orders_list")
    if tab_key == "ez-market":
        return "/ez-market"
    if tab_key == "ez-manage":
        return "/ez-manage"
    if tab_key == "ez-drivers":
        return url_for("store.drivers_admin")
    if tab_key == "in-house":
        return url_for("store.in_house_catering_page")
    if tab_key == "live-map":
        return url_for("ezcater_tracking_watch.page")
    return url_for("store.orders_list")


def _orders_store_filter_arg(raw: str | None) -> str:
    val = (raw or "").strip().lower().replace("_", "-")
    aliases = {
        "": "",
        "all": "",
        "both": "",
        "combined": "",
        "tomball": "tomball",
        "dos": "tomball",
        "dos-mas": "tomball",
        "store-2": "tomball",
        "store-4": "tomball",
        "copperfield": "copperfield",
        "uno": "copperfield",
        "uno-mas": "copperfield",
        "store-1": "copperfield",
        "store-3": "copperfield",
    }
    return aliases.get(val, "")


@store_bp.route("/catering", methods=["GET"])
def catering_dashboard():
    """Tabbed Catering dashboard — the entry layer the bottom-nav
    Catering tab links to. Defaults to the Ez Orders tab; ?tab=<key>
    deep-links another tab (an invalid tab falls back to Ez Orders).
    Each tab embeds the real, fully-functional catering page inline in
    an iframe. Structural twin of manager_dashboard.

    This route is now a thin shell: it builds only the tab list (key,
    label, url) the template needs to point each iframe at its page. No
    DB session is opened — the iframed pages run their own queries when
    the browser loads them."""
    require_dashboard_access("dash.catering")
    valid = {key for key, _ in _CATERING_DASH_TABS}
    active_tab = (request.args.get("tab") or "").strip().lower()
    if active_tab not in valid:
        active_tab = _CATERING_DASH_TABS[0][0]   # 'ez-orders'
    tabs = [
        {
            "key": key,
            "label": caption,
            "url": _catering_dash_full_url(key),
        }
        for key, caption in _CATERING_DASH_TABS
    ]
    ez_manage_pending_count = 0
    db = next(get_db())
    try:
        from app.models import DeliveryRequest

        ez_manage_pending_count = (
            db.query(DeliveryRequest)
            .filter(DeliveryRequest.status == "pending")
            .count()
        )
    finally:
        db.close()
    label = g.store_label or "Cenas Kitchen"
    # Portable "Wed, May 21" — no %-d / %#d (platform-specific).
    today_label = _today_label()
    return render_template(
        "catering_dashboard.html",
        active="catering_dashboard",
        store_label=label,
        today_label=today_label,
        active_tab=active_tab,
        tabs=tabs,
        ez_manage_pending_count=ez_manage_pending_count,
    )


# ---- Operations dashboard (tabbed entry layer, Sam 2026-05-21, dck) --
# Structural twin of the Catering dashboard above (itself a twin of the
# Manager dashboard). The bottom-nav Operations tab no longer opens a
# sub-option popover — it links straight here. This route renders
# operations_dashboard.html: a tab strip across the seven operations
# surfaces, defaulting to the Team tab.
#
# DESIGN-CHANGE rework (Sam 2026-05-21, dck): each tab no longer shows a
# read-only preview + an "Open full page" link. Instead each tab embeds
# the REAL, fully-functional operations page inline in an <iframe> —
# click a tab and the working page is right there. So this route no
# longer builds preview rows or opens a DB session; it only needs each
# tab's URL for the iframe to load. The existing operations pages and
# routes are untouched — they are simply iframed.
#
# FORECASTS is the exception: it is not a built page, so it has no real
# page to embed. Its tab keeps the coming-soon state (no iframe, no
# link), exactly as before.

# Ordered tab spec: (tab key, caption, coming-soon flag). The key
# matches the active_tab values operations_dashboard.html expects. The
# first entry is the default tab. The per-tab url is built per-request
# by _operations_dash_full_url since the tabs point at a mix of
# store-scoped and flat routes, and the coming-soon tab gets no url.
_OPERATIONS_DASH_TABS = [
    ("team",        "Team",            False),
    ("corp-order",       "Corporate Order",  False),
    ("sales",       "Sales",           False),
    ("labor",       "Labor",           False),
    ("performance", "Performance",     False),
    ("sections",    "Sections",        False),   # Floor map / section assignment / host seating + reservations (ck Gate 2; docs/floor_contract.md)
    ("schedule-reports", "Schedule Reports", False),
    ("forecasts",   "Forecasts",       True),
]


def _operations_dash_full_url(tab_key):
    """Absolute href to the real operations page a tab embeds in its
    iframe.
      team        -> /partner/team                  (team.team_page)
      sales       -> /<store>/reports/sales          (store.sales)
      labor       -> /<store>/reports/labor          (store.labor)
      performance -> /<store>/reports/server-performance
                                                     (store.server_performance)
      schedule         -> /<store>/schedules-v2/     (store.sv2_week_page) V2 (samai #2156)
      schedule-reports -> /<store>/schedule           (store.schedule) old date-range report
      corp-order       -> /<store>/corporate-order    (corporate_order.view)
      forecasts   -> not built yet — returns "" (no iframe, coming-soon)
    team.team_page lives outside the /<store> blueprint, so url_for on
    it resolves to the flat /partner/team with no slug. corporate_order
    is reached with an explicit store_slug (the convention everywhere
    else this file links to it). store.* endpoints take the slug from
    the current request. Falls back to "" on an unknown key so an
    iframe src is never wrong."""
    if tab_key == "team":
        # The unified workspace is per-store (its Schedule/Market iframes need a
        # real store). Partner/corporate-level Operations have no single store --
        # url_for'ing /partner|/corporate/team would (a) collide with the legacy
        # team.team_page (the OLD user-list, which then shadows it) and (b) give
        # the per-store iframes no real store. So default those to a real store;
        # the roster still shows ALL stores (team_roster location=all). Real-store
        # views (dos/uno) open their own workspace. (Sam #2352-2359.)
        _ws_store = "dos" if g.current_store in ("partner", "corporate") else g.current_store
        return url_for("store.team_workspace", store_slug=_ws_store)
    if tab_key == "sales":
        return url_for("store.sales")
    if tab_key == "labor":
        return url_for("store.labor")
    if tab_key == "performance":
        return url_for("store.server_performance")
    if tab_key == "sections":
        # Sections/Floor page is store-scoped on the self-contained floor
        # blueprint; partner/corporate views open it with their own slug
        # (the page's in-app location switcher covers both stores).
        return url_for("floor.sections_page", store_slug=g.current_store)
    if tab_key == "schedule":
        return url_for("store.sv2_week_page")   # repointed (samai #2156): Operations > Schedule opens the actual V2 scheduling (week-view + its sub-nav cards)
    if tab_key == "schedule-reports":
        return url_for("store.schedule")        # the OLD date-range report - kept, just relabeled so it is not mistaken for scheduling
    if tab_key == "corp-order":
        return url_for("corporate_order.view", store_slug=g.current_store)
    return ""   # 'forecasts' (not built) or any unknown key


_KITCHEN_DASH_TABS = [
    ("fresh-food", "Fresh Food", False),
    ("prep-list",  "Prep List",  False),
    ("recipes",    "Recipes",    False),
]


def _kitchen_dash_full_url(tab_key):
    """Absolute href to the real kitchen page each tab iframes.
      fresh-food → /<store>/fresh-food/place-order
      prep-list  → /<store>/kitchen/prep-list
      recipes    → /<store>/recipes
    Falls back to '' on an unknown key."""
    if tab_key == "fresh-food":
        return url_for("store.fresh_food_place_order")
    if tab_key == "prep-list":
        return url_for("store.kitchen_prep_list")
    if tab_key == "recipes":
        return url_for("store.recipes_index")
    return ""


@store_bp.route("/kitchen", methods=["GET"])
def kitchen_dashboard():
    """Tabbed Kitchen dashboard (Sam #1066 TODO #2, 2026-05-26) — twin of
    operations_dashboard. Replaces the prior sidebar dropdown
    (Fresh Food / Prep List / Recipes were direct sub-items) with a
    single direct link to /<store>/kitchen that opens this tabbed page.
    Each tab embeds the real page inline via iframe."""
    require_dashboard_access("dash.kitchen")
    valid = {key for key, _, _ in _KITCHEN_DASH_TABS}
    active_tab = (request.args.get("tab") or "").strip().lower()
    if active_tab not in valid:
        active_tab = _KITCHEN_DASH_TABS[0][0]   # 'fresh-food'
    tabs = [
        {
            "key": key,
            "label": caption,
            "coming": coming,
            "url": "" if coming else _kitchen_dash_full_url(key),
        }
        for key, caption, coming in _KITCHEN_DASH_TABS
    ]
    label = g.store_label or "Cenas Kitchen"
    today_label = _today_label()
    return render_template(
        "kitchen_dashboard.html",
        active="kitchen_dashboard",
        store_label=label,
        today_label=today_label,
        active_tab=active_tab,
        tabs=tabs,
    )


@store_bp.route("/team", methods=["GET"])
def team_workspace():
    """Unified Team workspace (Sam #2261 unify) -- the Operations Team tab opens
    into [ Team | Schedule | Market ]. Team = the all-store roster (team_roster
    defaults location=all, so it's the ONE team list regardless of the URL
    store) + the Add form; Schedule + Market re-home the existing per-store
    manager pages (Week / Time-off / Availability / marketplace) as chrome-
    stripped iframes. Thin shell, same pattern as the other store-scoped
    dashboards: the roster loads client-side from
    GET /<store>/schedules-v2/team-roster; this route only renders the page
    with the store context (store_slug drives the iframe srcs + the fetch)."""
    if not has_dashboard_access("dash.operations"):
        abort(403)
    label = g.store_label or "Cenas Kitchen"
    today_label = _today_label()
    # +Add dropdown is gated to the positions THIS manager may add -- sourced
    # from addable_positions_for(), the SAME addable_roles()/position_role() the
    # 403 add-gate uses, so the FE can never drift from enforcement
    # (aick #2413; Sam #2381/#2404). Unknown/floor role -> [] -> "can't add".
    from app.db import SessionLocal
    from app.models import CANONICAL_POSITIONS
    from app.services.team_roster import addable_positions_for
    _role = (getattr(getattr(g, "current_user", None), "permission_level", None) or "")
    _db = SessionLocal()
    try:
        addable_positions = addable_positions_for(_role, _db)
    finally:
        _db.close()

    # Schedule store selector (Sam #2589): the Schedule sub-tab embeds the Week
    # Builder per CONCRETE store (schedules are stored by location key tomball/
    # copperfield -> served at /dos/ + /uno/; /partner/ + /corporate/ map to
    # 'both' and have NO schedule rows). So a user with reach to BOTH stores needs
    # to pick which store's schedule to build. Derive the concrete options from
    # the user's accessible stores: partner/corporate (or any mix covering both)
    # -> Tomball + Copperfield; a single-store manager -> just their store.
    from app.web.permissions import accessible_store_slugs
    _acc = accessible_store_slugs(getattr(g, "current_user", None))
    _CONCRETE = [("dos", "Tomball"), ("uno", "Copperfield")]
    if ("partner" in _acc) or ("corporate" in _acc):
        _concrete_slugs = ["dos", "uno"]
    else:
        _concrete_slugs = [s for s in ("dos", "uno") if s in _acc]
    if not _concrete_slugs:
        # Fallback (legacy/tooling session with no resolvable scope): offer both
        # so the Schedule tab is never dead -- the per-store gate still guards each
        # iframe request, so an out-of-scope pick just renders the friendly notice.
        _concrete_slugs = ["dos", "uno"]
    schedule_stores = [{"slug": s, "label": dict(_CONCRETE)[s]} for s in _concrete_slugs]
    # Default the embedded schedule to the URL's store when it's already concrete
    # (/dos/team or /uno/team), else the first option (a both-store user lands on
    # Tomball but can switch). g.current_store is the slug the page loaded under.
    _cur = getattr(g, "current_store", None)
    schedule_store_default = _cur if _cur in _concrete_slugs else _concrete_slugs[0]

    # Sam #2675: only the partner (owner) may confirm Cena<->Toast links -> pass a flag so
    # the Link tab shows Verify/Unlink to the partner only (the endpoints are partner-gated too).
    from app.web.permissions import load_current_user as _lcu
    _u = getattr(g, "current_user", None) or _lcu()
    is_partner = bool(_u and (getattr(_u, "permission_level", "") or "").lower() == "partner")

    from flask import make_response as _make_response
    _resp = _make_response(render_template(
        "team_workspace.html",
        active="partner_team",
        store_label=label,
        today_label=today_label,
        addable_positions=addable_positions,
        # Full canonical position-name list for the #tws-pos filter dropdown
        # (Sam roster-fix #3): the server matches by NAME (team_roster._passes),
        # so the dropdown must offer every canonical name -- including ones no
        # one currently holds -- not just positions seen on the shown rows.
        filter_positions=sorted(CANONICAL_POSITIONS),
        # Schedule store selector (Sam #2589): concrete options + the default pick.
        schedule_stores=schedule_stores,
        schedule_store_default=schedule_store_default,
        link_store_default=schedule_store_default,
        is_partner=is_partner,
    ))
    # Sam 2026-06-07: never serve a STALE Team Roster -- a cached old page is the
    # likely reason "+Add showed everything" / the pre-tabs layout persisted.
    _resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return _resp


@store_bp.route("/operations", methods=["GET"])
def operations_dashboard():
    """Tabbed Operations dashboard — the entry layer the bottom-nav
    Operations tab links to. Defaults to the Team tab; ?tab=<key>
    deep-links another tab (an invalid tab falls back to Team).
    Each tab embeds the real, fully-functional operations page inline
    in an iframe — except Forecasts, which is not built yet and shows a
    coming-soon state (no iframe). Structural twin of catering_dashboard.

    This route is now a thin shell: it builds only the tab list (key,
    label, url, coming) the template needs to point each iframe at its
    page. No DB session is opened — the iframed pages run their own
    queries when the browser loads them."""
    require_dashboard_access("dash.operations")
    dash_tab_specs = _OPERATIONS_DASH_TABS
    if current_role_is("expo"):
        dash_tab_specs = [
            tab for tab in _OPERATIONS_DASH_TABS
            if tab[0] in ("team", "corp-order")
        ]
    valid = {key for key, _, _ in dash_tab_specs}
    active_tab = (request.args.get("tab") or "").strip().lower()
    if active_tab not in valid:
        active_tab = dash_tab_specs[0][0]
    if active_tab == "schedule":
        # 'schedule' is now a LINK-OUT to the V2 area (samai #2165) - it has no
        # in-dash panel, so a direct ?tab=schedule hit goes straight to V2.
        return redirect(_operations_dash_full_url("schedule"))
    tabs = [
        {
            "key": key,
            "label": caption,
            "coming": coming,
            # Coming-soon surface has no real page to embed -> empty url.
            "url": "" if coming else _operations_dash_full_url(key),
            # 'schedule' is a LINK-OUT to the full V2 area (samai #2165): a real
            # full-page nav, NOT an in-dash iframe (the iframe chrome-strip would
            # hide V2 sub-nav cards like Add Staff). Every other tab stays iframe.
            "linkout": (key == "schedule"),
        }
        for key, caption, coming in dash_tab_specs
    ]
    label = g.store_label or "Cenas Kitchen"
    # Portable "Wed, May 21" — no %-d / %#d (platform-specific).
    today_label = _today_label()
    return render_template(
        "operations_dashboard.html",
        active="operations_dashboard",
        store_label=label,
        today_label=today_label,
        active_tab=active_tab,
        tabs=tabs,
    )


# ---- Today dashboard (tabbed entry layer, Sam 2026-05-21, samai) ----
# Structural twin of the Catering dashboard above. The bottom-nav Today
# tab no longer opens a sub-option popover — it links straight here.
# This route renders today_dashboard.html: a tab strip across the four
# Today surfaces, defaulting to the Dashboard tab.
#
# DESIGN-CHANGE rework (Sam 2026-05-21, samai): each tab no longer shows
# a read-only preview + an "Open full page" link. Instead each tab
# embeds the REAL, fully-functional Today page inline in an <iframe> —
# click a tab and the working page is right there. So this route no
# longer builds preview rows; it only needs each tab's URL for the
# iframe to load. The existing Today pages and routes are untouched —
# they are simply iframed.

# Ordered tab spec: (tab key, caption). The key matches the active_tab
# values today_dashboard.html expects. First entry is the default tab.
# The per-tab url is built per-request by _today_dash_full_url since
# one tab points at a store-scoped route and three are flat (outside
# the /<store> blueprint).
_TODAY_DASH_TABS = [
    ("dashboard",     "Dashboard"),
    ("notifications", "Notifications"),
    ("task-reports",  "Task Reports"),
    ("agents",        "Agents"),
    ("pass",          "Pass"),
    # Sam #240 (2026-05-23): consolidated docs surface — replaces the
    # prior /partner/developer/app/* sidebar section in one place.
    # The "page-info" tab (which iframed /partner/developer/app/page-
    # guide) was retired in the same batch per Sam #241 — its content
    # is now folded into /sam/docs.
    ("docs",          "Docs"),
    # Sam directive 2026-05-23: every automated job in one place —
    # Render crons + always-on services + scheduled tasks + IMAP
    # polling + gateway auto-mirrors + third-party API integrations.
    ("automation",    "Automation"),
]


def _today_dash_full_url(tab_key):
    """Absolute href to the real Today page a tab embeds in its iframe.
      dashboard     -> /<store>/                  (store.home)
      notifications -> /partner/notifications      (flat, not store-scoped)
      task-reports  -> /partner/team-reports/      (flat, not store-scoped)
      agents        -> /sam/agents                 (flat; Cena's roster)
      page-info     -> /partner/developer/app/page-guide  (flat; aick's page guide)
      pass          -> /sam/pass                   (flat; credential LOCATIONS only)
    The flat notifications / team-reports / agents /
    page-guide / pass paths are written as literals because they live
    outside the /<store> blueprint; url_for on a store endpoint would
    prepend the slug. Falls back to the store home page on an unknown
    key so the iframe src is never empty."""
    if tab_key == "dashboard":
        return url_for("store.home")
    if tab_key == "notifications":
        return url_for("store.notifications_page")
    if tab_key == "task-reports":
        return "/partner/team-reports/"
    if tab_key == "agents":
        return "/sam/agents"
    if tab_key == "pass":
        return "/sam/pass"
    if tab_key == "docs":
        return "/sam/docs"
    if tab_key == "automation":
        return "/sam/automation"
    return url_for("store.home")


@store_bp.route("/notifications", methods=["GET"])
def notifications_page():
    """Store-scoped Notifications page for the Today dashboard iframe."""
    require_dashboard_access("dash.today")
    from app.services.ribbon import RIBBON_CATEGORIES
    from app.web.ribbon_routes import ribbon_render_context

    store_scope = STORE_TO_LOCATION.get(getattr(g, "current_store", None))
    if store_scope == "both":
        store_scope = None
    user = getattr(g, "current_user", None)
    categories = ribbon_render_context(user, "notifications", store_scope)
    total_count = sum((cat.get("count") or 0) for cat in categories)
    return render_template(
        "notifications.html",
        active="notifications",
        categories=categories,
        category_meta=RIBBON_CATEGORIES,
        total_count=total_count,
    )


@store_bp.route("/today", methods=["GET"])
def today_dashboard():
    """Tabbed Today dashboard — the entry layer the bottom-nav Today
    tab links to. Defaults to the Dashboard tab; ?tab=<key> deep-links
    another tab (an invalid tab falls back to Dashboard). Each tab
    embeds the real, fully-functional Today page inline in an iframe.
    Structural twin of catering_dashboard.

    This route is a thin shell: it builds only the tab list (key,
    label, url) the template needs to point each iframe at its page. No
    DB session is opened — the iframed pages run their own queries when
    the browser loads them.

    Tab gating mirrors the sidebar: the Task Reports tab is omitted
    unless the viewer holds team_reports.view. The standalone /assistant
    page is reached from the AI animation, so it is not embedded here.
    Agents / Pass / Docs / Automation tabs remain Sam-only. Dashboard and Notifications are
    always present, so the default tab is always valid. Gating fails OPEN — if a gate
    helper raises, the tab is kept and its destination page enforces
    its own gate."""
    require_dashboard_access("dash.today")
    # Build the visible tab set, applying the same audience gates the
    # Today section's sidebar entries use.
    dash_tabs = []
    _SAM_ONLY_KEYS = {"agents", "pass", "docs", "automation"}
    for key, caption in _TODAY_DASH_TABS:
        if current_role_is("expo") and key != "notifications":
            continue
        if key == "task-reports":
            try:
                from app.services.permissions import has_permission
                if not has_permission("team_reports.view"):
                    continue
            except Exception:
                pass  # fail open — the team-reports page enforces its own gate
        elif key in _SAM_ONLY_KEYS:
            try:
                from app.web.sam_chat import is_sam_chat_user
                if not is_sam_chat_user():
                    continue
            except Exception:
                pass  # fail open — the destination page enforces its own gate
        dash_tabs.append((key, caption))

    valid = {key for key, _ in dash_tabs}
    active_tab = (request.args.get("tab") or "").strip().lower()
    if active_tab not in valid:
        active_tab = dash_tabs[0][0]
    tabs = [
        {
            "key": key,
            "label": caption,
            "url": _today_dash_full_url(key),
        }
        for key, caption in dash_tabs
    ]
    label = g.store_label or "Cenas Kitchen"
    # Portable "Wed, May 21" — no %-d / %#d (platform-specific).
    today_label = _today_label()
    return render_template(
        "today_dashboard.html",
        active="today_dashboard",
        store_label=label,
        today_label=today_label,
        active_tab=active_tab,
        tabs=tabs,
    )


# ---- Vendors dashboard (tabbed entry layer, Sam 2026-05-21, ck) -----
# Structural twin of the Catering / Operations / Today dashboards above
# (each a twin of the Manager dashboard). The bottom-nav Vendors tab no
# longer opens a sub-option popover - it links straight here. This
# route renders vendors_dashboard.html: a tab strip across the five
# vendor surfaces, defaulting to the Produce tab.
#
# Each tab embeds the REAL, fully-functional vendor page inline in an
# <iframe> - click a tab and the working page is right there. So this
# route is a thin shell: it only resolves each tab's URL for the iframe
# to load. The existing produce / vendor-recent-orders pages and routes
# are untouched - they are simply iframed. Every tab points at a built,
# live page (produce_root for Produce; vendor_recent_orders for the
# four supply vendors), so - unlike Operations' Forecasts - there is no
# coming-soon tab here.

# Ordered tab spec: (tab key, caption). The key matches the active_tab
# values vendors_dashboard.html expects; for the four supply-vendor
# tabs it is also the <vendor> slug store.vendor_recent_orders takes.
# The first entry is the default tab.
_VENDORS_DASH_TABS = [
    ("produce",          "Produce"),
    ("webstaurant",      "Webstaurant"),
    ("performance-food", "Performance Food"),
    ("restaurant-depot", "Restaurant Depot"),
    ("specs",            "Specs"),
]


def _vendors_dash_full_url(tab_key):
    """Absolute href to the real vendor page a tab embeds in its iframe.
      produce          -> /<store>/produce/   (store.produce_root)
      webstaurant      -> /<store>/vendors/webstaurant/recent-orders
      performance-food -> /<store>/vendors/performance-food/recent-orders
      restaurant-depot -> /<store>/vendors/restaurant-depot/recent-orders
      specs            -> /<store>/vendors/specs/recent-orders
    The four supply-vendor tabs share store.vendor_recent_orders with
    the tab key passed as the <vendor> slug (the tab keys are exactly
    the slugs that route's _VENDOR_LABELS accepts). All are store-scoped
    endpoints, so url_for fills the slug from the current request.
    Falls back to the produce page on an unknown key so the iframe src
    is never empty."""
    if tab_key == "produce":
        return url_for("store.produce_root")
    if tab_key in ("webstaurant", "performance-food",
                   "restaurant-depot", "specs"):
        return url_for("store.vendor_recent_orders", vendor=tab_key)
    return url_for("store.produce_root")


@store_bp.route("/vendors", methods=["GET"])
def vendors_dashboard():
    """Tabbed Vendors dashboard - the entry layer the bottom-nav
    Vendors tab links to. Defaults to the Produce tab; ?tab=<key>
    deep-links another tab (an invalid tab falls back to Produce).
    Each tab embeds the real, fully-functional vendor page inline in
    an iframe. Structural twin of catering_dashboard.

    This route is a thin shell: it builds only the tab list (key,
    label, url) the template needs to point each iframe at its page. No
    DB session is opened - the iframed pages run their own queries when
    the browser loads them."""
    require_dashboard_access("dash.vendors")
    valid = {key for key, _ in _VENDORS_DASH_TABS}
    active_tab = (request.args.get("tab") or "").strip().lower()
    if active_tab not in valid:
        active_tab = _VENDORS_DASH_TABS[0][0]   # 'produce'
    tabs = [
        {
            "key": key,
            "label": caption,
            "url": _vendors_dash_full_url(key),
        }
        for key, caption in _VENDORS_DASH_TABS
    ]
    label = g.store_label or "Cenas Kitchen"
    # Portable "Wed, May 21" - no %-d / %#d (platform-specific).
    today_label = _today_label()
    return render_template(
        "vendors_dashboard.html",
        active="vendors_dashboard",
        store_label=label,
        today_label=today_label,
        active_tab=active_tab,
        tabs=tabs,
    )


# ============== CATERING — DRIVER ASSIGNMENT (Sam #669) ==============
#
# Two endpoints backing the per-order driver dropdown on the catering
# Ez Orders page:
#
#   POST /<store>/catering/assign_driver
#     body: {order_id, new_driver, current_driver}
#     -> creates a driver_assignment_jobs row (status=pending), pings
#        the aick gateway to run the Selenium flow, returns {job_id}.
#
#   GET  /<store>/catering/assign_driver/status?job_id=X
#     -> {status: pending|running|completed|failed, error_message?,
#         new_driver, current_driver, gateway_processed, retry_count}
#
# Concurrency guard per Sam #669: a second job for the same order_id
# within 5 seconds of the first is rejected server-side (idempotency
# window). The frontend dropdown lock prevents this from the UI side
# too, but this guard catches double-clicks and races. The Selenium
# flow itself + amendment (verify via DOM re-read, not PDF parse)
# lives in app/services/ezcater_driver_assigner.py (Phase 2).

@store_bp.route("/catering/assign_driver", methods=["POST"])
def catering_assign_driver():
    from app.services.driver_assignment_jobs import (
        AssignmentAlreadyInProgress,
        create_assignment_job,
        wake_assignment_gateway,
    )
    payload = request.get_json(silent=True) or {}
    order_id = (payload.get("order_id") or "").strip()
    new_driver = (payload.get("new_driver") or "").strip()
    current_driver = (payload.get("current_driver") or "").strip() or None
    if not order_id or not new_driver:
        return jsonify({"ok": False, "error": "order_id + new_driver required"}), 400

    db = next(get_db())
    try:
        try:
            job = create_assignment_job(
                db,
                order_id=order_id,
                current_driver=current_driver,
                new_driver=new_driver,
            )
        except AssignmentAlreadyInProgress as exc:
            return jsonify({
                "ok": False, "error": "assignment already in progress",
                "job_id": exc.job.job_id,
            }), 409

        db.commit()
        job_id = job.job_id
    finally:
        db.close()

    # HTTP-wake the aick gateway (Sam #669 + 2026-05-24 architecture
    # choice b). dispatch_assignment_job sends the job payload to
    # CENA_GATEWAY_URL/jobs/driver-assign — aick runs the flow + POSTs
    # the result back to /catering/assign_driver/result. Gateway hop
    # out-of-band so a slow ezCater never blocks this HTTP response.
    wake_assignment_gateway(job_id, order_id, current_driver, new_driver)

    return jsonify({"ok": True, "job_id": job_id}), 202


@store_bp.route("/catering/assign_driver/status", methods=["GET"])
def catering_assign_driver_status():
    from app.models import DriverAssignmentJob
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id required"}), 400
    db = next(get_db())
    try:
        job = (db.query(DriverAssignmentJob)
                 .filter(DriverAssignmentJob.job_id == job_id)
                 .first())
        if not job:
            return jsonify({"ok": False, "error": "job not found"}), 404
        return jsonify({
            "ok": True,
            "job_id": job.job_id,
            "order_id": job.order_id,
            "status": job.status,
            "error_message": job.error_message,
            "current_driver": job.current_driver,
            "new_driver": job.new_driver,
            "gateway_processed": job.gateway_processed,
            "retry_count": job.retry_count,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        })
    finally:
        db.close()


