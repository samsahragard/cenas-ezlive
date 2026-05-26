"""Toast POS reports blueprint.

Two routes:
    /reports/labor              — labor-by-position with % of net sales
    /reports/server-performance — per-server tip % + service timing

Both accept ?start=YYYY-MM-DD&end=YYYY-MM-DD&location=tomball|copperfield|both
query params. If start/end omitted, renders just the form. If a fetch fails,
the error surfaces inline so the user knows what to do (most likely
TOAST_API_KEY missing or expired).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, request, g

from app.services import toast_reports, sling_reports, produce_history

log = logging.getLogger(__name__)

reports = Blueprint("reports", __name__, url_prefix="/reports")

# Restaurant is in Central Time; Render runs UTC. Date "today" must be the
# restaurant's calendar date or businessDate lookups miss data after ~7pm.
_CT = timezone(timedelta(hours=-5))


def _today_ct():
    return datetime.now(_CT).date()


def _period_to_dates(period: str) -> tuple[datetime | None, datetime | None, str | None]:
    """Map a period preset to (start_dt, end_dt, label).

    period:
        "today"     -> just today
        "week"      -> Mon → today (current week, partial)
        "prev_week" -> previous Mon → Sun
    Returns (None, None, None) for unknown periods.
    """
    today = _today_ct()
    if period == "today":
        d = today
        return (datetime.combine(d, datetime.min.time()),
                datetime.combine(d, datetime.min.time()),
                d.strftime("%a, %b %d").replace(" 0", " "))
    if period == "week":
        # Sun -> today (Sam 2026-05-11 — week starts on Sunday for sales/labor).
        start = today - timedelta(days=(today.weekday() + 1) % 7)
        return (datetime.combine(start, datetime.min.time()),
                datetime.combine(today, datetime.min.time()),
                f"{start.strftime('%b %d')} – {today.strftime('%b %d')}".replace(" 0", " "))
    if period == "prev_week":
        # Last full Sun -> Sat.
        this_sun = today - timedelta(days=(today.weekday() + 1) % 7)
        end = this_sun - timedelta(days=1)
        start = end - timedelta(days=6)
        return (datetime.combine(start, datetime.min.time()),
                datetime.combine(end, datetime.min.time()),
                f"{start.strftime('%b %d')} – {end.strftime('%b %d')}".replace(" 0", " "))
    return None, None, None


def _resolve_period(start, end, err):
    """Honour ?period= shortcut + auto-default to today on bare URL.

    Returns (start, end, err, active_period, period_label). active_period is
    'today' / 'week' / 'prev_week' if a preset is in effect, '' otherwise.
    """
    period = (request.args.get("period") or "").strip().lower()
    if period in ("today", "week", "prev_week"):
        s, e, label = _period_to_dates(period)
        return s, e, None, period, label
    if start is None and end is None and not err:
        s, e, label = _period_to_dates("today")
        return s, e, None, "today", label
    return start, end, err, "", None


def _last_name_key(name: str) -> tuple[str, str]:
    """Sort key: lower-case last name, then first name. Robust to single-word names."""
    parts = (name or "").strip().split()
    if not parts:
        return ("", "")
    last = parts[-1].lower()
    rest = " ".join(parts[:-1]).lower() if len(parts) > 1 else ""
    return (last, rest)


def _parse_date_range() -> tuple[datetime | None, datetime | None, str | None]:
    """Read start/end from query string. Defaults to last 7 days if either is given."""
    start_raw = (request.args.get("start") or "").strip()
    end_raw = (request.args.get("end") or "").strip()
    if not start_raw and not end_raw:
        return None, None, None
    today = datetime.now().date()
    try:
        end = datetime.strptime(end_raw, "%Y-%m-%d") if end_raw else datetime.combine(today, datetime.min.time())
        start = datetime.strptime(start_raw, "%Y-%m-%d") if start_raw \
                else end - timedelta(days=6)
    except ValueError:
        return None, None, "Invalid date format — use YYYY-MM-DD."
    if start > end:
        return None, None, "Start date must be on or before end date."
    if (end - start).days > 92:
        return None, None, "Range too long (> 92 days). Pick a smaller window."
    return start, end, None


def _location_filter() -> str:
    """Determine which location to scope this report to.

    Priority:
      1. `g.location_override` set by store_routes.py URL prefix layer
         (e.g., visiting /dos/reports/labor sets it to 'tomball')
      2. ?location= query param (legacy top-level URLs)
      3. 'both' default
    """
    override = getattr(g, "location_override", None)
    if override and override in {"tomball", "copperfield", "both"}:
        return override
    loc = (request.args.get("location") or "both").strip().lower()
    return loc if loc in {"both", "tomball", "copperfield"} else "both"


def _default_dates() -> tuple[str, str]:
    """Default form values for past-data reports: last 7 days ending yesterday."""
    today = datetime.now().date()
    end = today - timedelta(days=1)
    start = end - timedelta(days=6)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _default_dates_future() -> tuple[str, str]:
    """Default form values for forward-looking reports (schedule): today + next 7 days."""
    today = datetime.now().date()
    end = today + timedelta(days=6)
    return today.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


@reports.route("/produce-orders")
def produce_orders():
    """Produce orders + price history page (Vendors → Produce → Orders).

    Shows: latest pricing (Alvarado vs J. Luna with winner per item),
    biggest movers (week-over-week price moves >= 5%), per-item price
    history chart (Chart.js).
    """
    selected_item = (request.args.get("item") or "").strip()
    days = max(7, min(int(request.args.get("days") or 90), 365))
    movers_threshold = float(request.args.get("threshold") or 5.0)

    ctx = {
        "active": "produce_orders",
        "page_title": "Produce Orders & Price History",
        "selected_item": selected_item,
        "days": days,
        "movers_threshold": movers_threshold,
        "error": None,
    }
    try:
        ctx["latest"] = produce_history.latest_prices()
        ctx["movers"] = produce_history.biggest_movers(threshold_pct=movers_threshold)
        ctx["available_items"] = produce_history.list_distinct_items()
        if selected_item:
            # parse "Name|Size" or just "Name"
            parts = selected_item.split("|", 1)
            name = parts[0].strip()
            size = parts[1].strip() if len(parts) > 1 else None
            ctx["history"] = produce_history.history_for_item(name, size, days=days)
    except Exception as ex:
        log.exception("produce-orders report failed")
        ctx["error"] = f"Could not load produce history: {ex}"
    return render_template("reports_produce_orders.html", **ctx)


@reports.route("/third-party-sales")
def third_party_sales():
    start, end, err = _parse_date_range()
    start, end, err, active_period, period_label = _resolve_period(start, end, err)
    location = _location_filter()
    default_start, default_end = _default_dates()

    # Multi-channel selection. ?channels=toast,doordash,ezcater  (or 'all').
    # When unset, default to All channels — matches Sam's spec for the
    # store-scoped /uno/sales / /dos/sales view.
    channels_csv = (request.args.get("channels") or "").strip()
    channel_keys: list[str] = []
    if channels_csv:
        channel_keys = [c.strip().lower() for c in channels_csv.split(",") if c.strip()]
    if not channel_keys:
        channel_keys = ["all"]
    available_channels = [
        ("all",          "All"),
        ("toast",        "Toast (in-store)"),
        ("online",       "Toast Online"),
        ("doordash",     "DoorDash"),
        ("uber",         "Uber Eats"),
        ("toast_local",  "Toast Local"),
        ("toast_pickup", "Toast Pickup"),
        ("ezcater",      "ezCater"),
    ]

    ctx = {
        "active": "third_party_sales",
        "page_title": "Sales",
        "form_default_start": request.args.get("start") or (start.strftime("%Y-%m-%d") if start else default_start),
        "form_default_end": request.args.get("end") or (end.strftime("%Y-%m-%d") if end else default_end),
        "form_location": location,
        "active_period": active_period,
        "period_label": period_label,
        "selected_channels": channel_keys,
        "available_channels": available_channels,
        "error": err,
        "report": None,
    }
    channel_filter = getattr(g, "sales_channel", None)
    if channel_filter not in ("toast", "online", "doordash", "uber", "total", "ezcater", "all"):
        channel_filter = None
    page_label = {"toast": " · In-Store", "online": " · Online", "doordash": " · DoorDash",
                  "uber": " · Uber Eats", "total": " · Total", "ezcater": " · Ezcater"}.get(channel_filter, "")
    active_key = {"toast": "sales_toast", "online": "sales_online", "doordash": "sales_doordash",
                  "uber": "sales_uber", "total": "sales_total",
                  "ezcater": "sales_ezcater"}.get(channel_filter, "third_party_sales")
    ctx["page_title"] = "Third-Party Sales" + page_label if not page_label else ("Sales" + page_label)
    ctx["active"] = active_key
    ctx["channel_filter"] = channel_filter
    if channel_filter == "ezcater":
        # ezCater isn't in Toast — it lives in our Order DB. Wiring this up to
        # actual sales totals requires per-item pricing we don't have yet.
        ctx["error"] = ("EzCater sales report coming in Phase 3. "
                        "EzCater orders are tracked in the Order DB but per-item pricing "
                        "isn't yet captured at the order level — we currently store "
                        "headcount but not the order total.")
        return render_template("reports_third_party_sales.html", **ctx)
    if start and end and not err:
        try:
            # Multi-channel takes precedence; fall back to legacy single
            # channel_filter (used by the per-channel /reports/sales/<channel>
            # store-bp wrapper).
            ctx["report"] = toast_reports.third_party_sales_report(
                start, end, location,
                channel_filter=channel_filter,
                channels=channel_keys if not channel_filter else None,
            )
        except Exception as ex:
            log.exception("third-party sales report failed")
            ctx["error"] = f"Could not generate report: {ex}"
    return render_template("reports_third_party_sales.html", **ctx)


@reports.route("/labor")
def labor():
    start, end, err = _parse_date_range()
    start, end, err, active_period, period_label = _resolve_period(start, end, err)
    location = _location_filter()
    default_start, default_end = _default_dates()
    role_filter = getattr(g, "labor_filter", None)  # 'boh' / 'foh' / None (= all)
    if role_filter not in ("boh", "foh"):
        role_filter = None
    active_key = {"boh": "boh_labor", "foh": "foh_labor"}.get(role_filter, "labor")
    role_subtitle = {"boh": " — BOH only (Cook / Prep / Grill / Dish / Enchilada / Kitchen Mgr)",
                     "foh": " — FOH only (Server / Bartender / Host / Cashier / Busser / Expo / Floor Mgr)"
                     }.get(role_filter, "")
    # Privacy: only Partner view shows full management labor data.
    is_partner = getattr(g, "current_store", None) == "partner"
    redact_management = not is_partner
    ctx = {
        "active": active_key,
        "page_title": "Labor Report" + (role_subtitle and (" · " + role_filter.upper())),
        "role_subtitle": role_subtitle,
        "role_filter": role_filter,
        "redact_management": redact_management,
        "is_partner": is_partner,
        "form_default_start": request.args.get("start") or (start.strftime("%Y-%m-%d") if start else default_start),
        "form_default_end": request.args.get("end") or (end.strftime("%Y-%m-%d") if end else default_end),
        "form_location": location,
        "active_period": active_period,
        "period_label": period_label,
        "error": err,
        "report": None,
    }
    if start and end and not err:
        try:
            ctx["report"] = toast_reports.labor_report(
                start, end, location,
                role_filter=role_filter,
                redact_management=redact_management,
            )
        except Exception as ex:
            log.exception("labor report failed")
            ctx["error"] = f"Could not generate report: {ex}"
    return render_template("reports_labor.html", **ctx)


@reports.route("/roster")
def roster():
    location = _location_filter()
    position = (request.args.get("position") or "all").strip()
    include_inactive = request.args.get("include_inactive") == "1"
    role_filter = getattr(g, "roster_filter", None)  # 'boh' / 'foh' / 'all' / None
    if role_filter not in ("boh", "foh", "all"):
        role_filter = None
    active_key = {"boh": "boh_roster", "foh": "foh_roster", "all": "all_roster"}.get(role_filter, "roster")
    role_subtitle = {"boh": " · BOH only", "foh": " · FOH only"}.get(role_filter, "")
    ctx = {
        "active": active_key,
        "page_title": "Roster" + role_subtitle,
        "role_filter": role_filter,
        "form_location": location,
        "form_position": position,
        "form_include_inactive": include_inactive,
        "error": None,
        "report": None,
    }
    try:
        ctx["report"] = sling_reports.roster_report(
            location_filter=location,
            position_filter=None if position == "all" else position,
            role_filter=role_filter if role_filter in ("boh", "foh") else None,
            include_inactive=include_inactive,
        )
    except Exception as ex:
        log.exception("roster report failed")
        ctx["error"] = f"Could not generate roster: {ex}"
    return render_template("reports_roster.html", **ctx)


@reports.route("/schedule")
def schedule():
    start, end, err = _parse_date_range()
    location = _location_filter()
    default_start, default_end = _default_dates_future()
    today = datetime.now().date()
    # Preset quick-picks. Users can also override via the date pickers.
    def _fmt(d): return d.strftime("%Y-%m-%d")
    presets = [
        {"label": "This week",     "start": _fmt(today),                     "end": _fmt(today + timedelta(days=6))},
        {"label": "Next 2 weeks",  "start": _fmt(today),                     "end": _fmt(today + timedelta(days=13))},
        {"label": "Next 4 weeks",  "start": _fmt(today),                     "end": _fmt(today + timedelta(days=27))},
        {"label": "Past week",     "start": _fmt(today - timedelta(days=7)), "end": _fmt(today - timedelta(days=1))},
    ]
    ctx = {
        "active": "weekly_schedule",
        "page_title": "Schedule",
        "form_default_start": request.args.get("start") or default_start,
        "form_default_end": request.args.get("end") or default_end,
        "form_location": location,
        "presets": presets,
        "error": err,
        "report": None,
    }
    if start and end and not err:
        # Cap the range at 4 weeks (28 days) per Sam's spec
        if (end - start).days > 28:
            ctx["error"] = "Range too long — pick 4 weeks (28 days) or less."
        else:
            try:
                # Sam #1018 (2026-05-26) — schedule now reads from Toast
                # /labor/v1/shifts instead of Sling. Copperfield will show
                # zero shifts until Toast Scheduling is enabled there.
                ctx["report"] = toast_reports.schedule_report(start, end, location)
            except Exception as ex:
                log.exception("schedule report failed")
                ctx["error"] = f"Could not generate schedule: {ex}"
    return render_template("reports_schedule.html", **ctx)


@reports.route("/server-performance")
def server_performance():
    start, end, err = _parse_date_range()
    location = _location_filter()

    # Active period pill. If start/end are explicit + don't match a preset,
    # we still highlight 'today' for visual consistency. URL-driven state
    # via ?period=today|week|prev_week takes precedence + auto-fills dates
    # so first-load shows real data without the user clicking Run.
    period = (request.args.get("period") or "").strip().lower()
    if period in ("today", "week", "prev_week"):
        start, end, period_label = _period_to_dates(period)
        err = None
    elif start is None and end is None and not err:
        # No explicit dates AND no period param → default to today.
        period = "today"
        start, end, period_label = _period_to_dates(period)
    else:
        period_label = None

    default_start, default_end = _default_dates()
    role_filter = getattr(g, "role_filter", None)
    if role_filter not in ("server", "bartenders", "all"):
        role_filter = None
    active_key = {"server": "perf_server", "bartenders": "perf_bartenders", "all": "perf_all"}.get(role_filter, "server_perf")
    role_label = {"server": " · Servers", "bartenders": " · Bartenders", "all": " · All FOH"}.get(role_filter, "")
    ctx = {
        "active": active_key,
        "page_title": "Server Performance" + role_label,
        "role_filter": role_filter,
        "form_default_start": request.args.get("start") or (start.strftime("%Y-%m-%d") if start else default_start),
        "form_default_end": request.args.get("end") or (end.strftime("%Y-%m-%d") if end else default_end),
        "form_location": location,
        "active_period": period or "",
        "period_label": period_label,
        "error": err,
        "report": None,
    }
    if start and end and not err:
        try:
            report = toast_reports.server_perf_report(start, end, location, role_filter=role_filter)
            # Sort rows alphabetically by last name within each location section.
            for loc_key, data in (report.get("by_location") or {}).items():
                rows = data.get("rows") or []
                rows.sort(key=lambda r: _last_name_key(r.get("name") or ""))
                data["rows"] = rows
            ctx["report"] = report
        except Exception as ex:
            log.exception("server perf report failed")
            ctx["error"] = f"Could not generate report: {ex}"
    return render_template(
        "reports_server_perf.html",
        fmt_duration=toast_reports.fmt_duration,
        **ctx,
    )
