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

from flask import Blueprint, Response, g, abort, request, render_template, redirect, url_for, session, jsonify

from datetime import datetime, timedelta

from app.db import get_db
from app.models import Driver, DriverShift, DriverLocation
from app.web.driver_routes import issue_temp_password, LOCATION_LABELS
# Phase 0 Block 4 (ck, 2026-05-13): permission gating per samai's spec.
from app.services.permissions import requires_permission

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
    if u is None:
        return None  # legacy auth_ok sessions (tooling) skip this
    if u.permission_level in ("partner", "corporate"):
        return None

    # (2) Expo: deny any Insights-section URL outright.
    if u.permission_level == "expo":
        path = (request.path or "").lower()
        insights_paths = ("/reports/sales", "/reports/labor", "/reports/server-performance",
                          "/labor", "/sales", "/performance", "/forecast")
        if any(p in path for p in insights_paths):
            return ("Forbidden — Expo accounts don't have Insights access.", 403)

    # (1) Per-store scope block.
    target = getattr(g, "current_store", None)
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
        {"label": "Weekly Schedule", "icon": "📅", "href": f"/{store_slug}/schedule/weekly",
         "active": ["weekly_schedule"],
         "sub": "Sling-sourced schedule for the week."},
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


@store_bp.route("/vendors")
def vendors_landing():
    cards = _vendors_subnav_cards(g.current_store)
    return _render_landing("vendors", "Vendors", f"{g.store_label} · supply ops & catalogs", cards)


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
        from app.web.orders_browse import list_orders_for_location, group_orders_by_date
        from app.services.orders_query import rotated_dispatch_letters
        db = next(get_db())
        try:
            tom = list_orders_for_location(db, "tomball")
            cop = list_orders_for_location(db, "copperfield")
            combined = tom + cop
            groups = group_orders_by_date(combined)
            display_drivers = rotated_dispatch_letters(groups)
            return render_template(
                "orders_by_store.html",
                location="both",
                location_label="All Orders",
                groups=groups,
                display_drivers=display_drivers,
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
        cur_start, _, _ = period_containing(_date.today())
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
        )
    finally:
        db.close()


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
    from app.services.ezcater_known_drivers_seed import normalize_phone
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
        known_by_phone = {kd.phone_e164: kd for kd in
                          db.query(EzcaterKnownDriver).all()}
        verified_for = {}
        ezcater_name_for = {}
        for d in rows:
            if d.phone:
                norm = normalize_phone(d.phone)
                kd = known_by_phone.get(norm)
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
            else:
                verified_for[d.id] = False

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
            seconds_ago = max(0, int((now - latest.captured_at).total_seconds()))
            results.append({
                "driver_id":      drv.id,
                "name":           drv.name,
                "location":       drv.location,
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
        active_key = "vendor_recent_" + vendor.replace("-", "_")
        return render_template(
            "vendor_recent_orders.html",
            vendor_slug=vendor,
            vendor_label=label,
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


# Audience gate for the Manager section: per Sam #1109 + #1115, every
# manager page is visible to all manager-tier roles + partner/corporate
# above. Expo and drivers excluded. Using the existing User.permission_level
# values (partner / corporate / gm / manager / expo / corporate-driver
# per User model docstring) — no new role / helper / hierarchy needed.
# Sam's user.permission_level == 'partner' grants access via this gate.
_MANAGER_ROLES_DENIED = {"expo", "corporate-driver", "driver"}


def _manager_role_ok():
    user = getattr(g, "current_user", None)
    if user is None:
        return False
    role = (getattr(user, "permission_level", None) or "").strip().lower()
    if not role:
        return False
    return role not in _MANAGER_ROLES_DENIED


@store_bp.route("/manager/<page>", methods=["GET"])
def manager_page_list(page: str):
    """List view for a manager-section page. Renders the shared
    manager_log.html template with rows newest first, store-scoped.
    Special-case: daily-log uses Sam's custom-designed standalone
    template (Sam dev #2952 2026-05-19)."""
    Model = _manager_model_for_slug(page)
    label = _MANAGER_PAGE_LABELS.get(page)
    if Model is None or label is None:
        abort(404)
    if not _manager_role_ok():
        abort(403)
    active_key = "manager_" + page.replace("-", "_")
    db = next(get_db())
    try:
        q = db.query(Model)
        if g.current_location in ("tomball", "copperfield"):
            q = q.filter(
                (Model.store_scope == g.current_location) |
                (Model.store_scope.is_(None))
            )
        rows = q.order_by(Model.created_at.desc()).limit(100).all()
        template_name = ("manager_daily_log.html"
                         if page == "daily-log" else "manager_log.html")
        return render_template(
            template_name,
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
    if not _manager_role_ok():
        abort(403)
    active_key = "manager_" + page.replace("-", "_")
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
    if not _manager_role_ok():
        abort(403)
    user = getattr(g, "current_user", None)
    db = next(get_db())
    try:
        store_scope = (g.current_location
                       if g.current_location in ("tomball", "copperfield")
                       else None)
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
    if not _manager_role_ok():
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
    if not _manager_role_ok():
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
    if not _manager_role_ok():
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
    if not _manager_role_ok():
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
            recipes.append({
                "id": r.id, "category": r.category, "name": r.name,
                "prep_time": r.prep_time, "shelf_life": r.shelf_life,
                "batch_sizes": bsizes, "ingredients": ings,
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
    if not _manager_role_ok():
        abort(403)
    return render_template(
        "recipes.html", recipes=[], recipe=None, form_mode="new",
        categories=[{"slug": c, "label": c.title()} for c in _RECIPE_CATEGORIES],
        active="recipes",
    )


@store_bp.route("/recipes", methods=["POST"])
def recipes_create():
    if not _manager_role_ok():
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
            category=(request.form.get("category") or "").strip()[:40] or "hot",
            name=(request.form.get("name") or "").strip()[:200] or "Untitled",
            prep_time=(request.form.get("prep_time") or "").strip()[:80] or None,
            shelf_life=(request.form.get("shelf_life") or "").strip()[:80] or None,
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
    if not _manager_role_ok():
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
            "id": r.id, "category": r.category, "name": r.name,
            "prep_time": r.prep_time, "shelf_life": r.shelf_life,
            "batch_sizes": bsizes, "ingredients": ings,
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
    if not _manager_role_ok():
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
            "id": r.id, "category": r.category, "name": r.name,
            "prep_time": r.prep_time, "shelf_life": r.shelf_life,
            "batch_sizes": bsizes, "ingredients": ings,
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
    if not _manager_role_ok():
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
        r.category = (request.form.get("category") or "").strip()[:40] or r.category
        r.name = (request.form.get("name") or "").strip()[:200] or r.name
        r.prep_time = (request.form.get("prep_time") or "").strip()[:80] or None
        r.shelf_life = (request.form.get("shelf_life") or "").strip()[:80] or None
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


def _ff_window_tracker(db, days: int):
    """For Recent Orders top section. Returns
    {slug: {'tomball': float, 'copperfield': float, 'total': float}}
    aggregating sum(or_qty) per item per store across window."""
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
        d = out.setdefault(slug, {'tomball': 0.0, 'copperfield': 0.0, 'total': 0.0})
        t = float(total or 0)
        if store == 'tomball':
            d['tomball'] += t
        elif store == 'copperfield':
            d['copperfield'] += t
        d['total'] += t
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
    if not _manager_role_ok():
        abort(403)
    db = next(get_db())
    try:
        avg_by_slug = _ff_rolling_avg_by_slug(db, days=30)
    finally:
        db.close()
    from datetime import timedelta
    today = datetime.utcnow().date()
    rolling_days = [today + timedelta(days=i - 3) for i in range(7)]
    return render_template(
        "fresh_food_place_order.html",
        categories=_FRESH_FOOD_ITEMS,
        rolling_days=rolling_days,
        rolling_avg_by_slug=avg_by_slug,
        active="fresh_food_place_order",
    )


@store_bp.route("/fresh-food/place-order", methods=["POST"])
def fresh_food_place_order_submit():
    if not _manager_role_ok():
        abort(403)
    from app.models import FreshFoodOrder, FreshFoodOrderLine
    body = request.get_json(silent=True) or {}
    od_str = (body.get("order_date") or "").strip()
    try:
        order_date = datetime.fromisoformat(od_str).date() if od_str else datetime.utcnow().date()
    except Exception:
        order_date = datetime.utcnow().date()
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


@store_bp.route("/fresh-food/recent-orders", methods=["GET"])
def fresh_food_recent_orders():
    if not _manager_role_ok():
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
    if not _manager_role_ok():
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
    if not _manager_role_ok():
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

@store_bp.route("/schedule")
@store_bp.route("/schedule/<view>")
def schedule(view: str = "weekly"):
    """Schedule (Sling). Children: BOH Roster / FOH Roster / All Roster / Weekly.
    Phase 1: 'weekly' shows the current schedule report; the BOH/FOH/All roster
    children are wired in Phase 2 with role classification."""
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
