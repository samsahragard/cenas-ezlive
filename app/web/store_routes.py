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

from flask import Blueprint, g, abort, request, render_template, redirect, url_for, session, jsonify

from datetime import datetime, timedelta

from app.db import get_db
from app.models import Driver, DriverShift, DriverLocation
from app.web.driver_routes import issue_temp_password, LOCATION_LABELS

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
        db = next(get_db())
        try:
            tom = list_orders_for_location(db, "tomball")
            cop = list_orders_for_location(db, "copperfield")
            combined = tom + cop
            groups = group_orders_by_date(combined)
            return render_template(
                "orders_by_store.html",
                location="both",
                location_label="All Orders",
                groups=groups,
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
def drivers_admin():
    """Per-store driver admin: list / reset PW / deactivate.

    Per-location stores see only their own drivers; corporate + partner see all.
    Anyone past the site `cenas` gate can reach /uno/, /dos/, /corporate/.
    Partner is additionally gated by the partner-auth before_request hook above.
    """
    from app.models import EzcaterKnownDriver
    from app.services.ezcater_known_drivers_seed import normalize_phone
    db = next(get_db())
    try:
        q = db.query(Driver)
        if g.current_location != "both":
            q = q.filter(Driver.location == g.current_location)
        rows = q.order_by(Driver.location, Driver.name).all()
        # latest shift per driver — drives the click-Active-to-see-location link
        from sqlalchemy import func
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
        known_phones = {p for (p,) in db.query(EzcaterKnownDriver.phone_e164).all()}
        verified_for = {}
        for d in rows:
            if d.phone:
                verified_for[d.id] = normalize_phone(d.phone) in known_phones
            else:
                verified_for[d.id] = False

        return render_template(
            "driver_admin.html",
            drivers=rows,
            latest_shift_for=latest_shift_for,
            verified_for=verified_for,
            store_label=g.store_label,
            current_location=g.current_location,
            location_labels=LOCATION_LABELS,
            temp_pw=request.args.get("temp_pw"),
            temp_for=request.args.get("temp_for"),
            error=request.args.get("error"),
            active="drivers_admin",
        )
    finally:
        db.close()


@store_bp.route("/drivers/<int:driver_id>/reset", methods=["POST"])
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
