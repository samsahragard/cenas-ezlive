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
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request

from app.services import toast_reports, sling_reports

log = logging.getLogger(__name__)

reports = Blueprint("reports", __name__, url_prefix="/reports")


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


@reports.route("/third-party-sales")
def third_party_sales():
    start, end, err = _parse_date_range()
    location = _location_filter()
    default_start, default_end = _default_dates()
    ctx = {
        "active": "third_party_sales",
        "page_title": "Third-Party Sales",
        "form_default_start": request.args.get("start") or default_start,
        "form_default_end": request.args.get("end") or default_end,
        "form_location": location,
        "error": err,
        "report": None,
    }
    if start and end and not err:
        try:
            ctx["report"] = toast_reports.third_party_sales_report(start, end, location)
        except Exception as ex:
            log.exception("third-party sales report failed")
            ctx["error"] = f"Could not generate report: {ex}"
    return render_template("reports_third_party_sales.html", **ctx)


@reports.route("/labor")
def labor():
    start, end, err = _parse_date_range()
    location = _location_filter()
    default_start, default_end = _default_dates()
    ctx = {
        "active": "labor",
        "page_title": "Labor Report",
        "form_default_start": request.args.get("start") or default_start,
        "form_default_end": request.args.get("end") or default_end,
        "form_location": location,
        "error": err,
        "report": None,
    }
    if start and end and not err:
        try:
            ctx["report"] = toast_reports.labor_report(start, end, location)
        except Exception as ex:
            log.exception("labor report failed")
            ctx["error"] = f"Could not generate report: {ex}"
    return render_template("reports_labor.html", **ctx)


@reports.route("/roster")
def roster():
    location = _location_filter()
    position = (request.args.get("position") or "all").strip()
    include_inactive = request.args.get("include_inactive") == "1"
    ctx = {
        "active": "roster",
        "page_title": "Roster",
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
    ctx = {
        "active": "schedule",
        "page_title": "Schedule",
        "form_default_start": request.args.get("start") or default_start,
        "form_default_end": request.args.get("end") or default_end,
        "form_location": location,
        "error": err,
        "report": None,
    }
    if start and end and not err:
        try:
            ctx["report"] = sling_reports.schedule_report(start, end, location)
        except Exception as ex:
            log.exception("schedule report failed")
            ctx["error"] = f"Could not generate schedule: {ex}"
    return render_template("reports_schedule.html", **ctx)


@reports.route("/server-performance")
def server_performance():
    start, end, err = _parse_date_range()
    location = _location_filter()
    default_start, default_end = _default_dates()
    ctx = {
        "active": "server_perf",
        "page_title": "Server Performance",
        "form_default_start": request.args.get("start") or default_start,
        "form_default_end": request.args.get("end") or default_end,
        "form_location": location,
        "error": err,
        "report": None,
    }
    if start and end and not err:
        try:
            ctx["report"] = toast_reports.server_perf_report(start, end, location)
        except Exception as ex:
            log.exception("server perf report failed")
            ctx["error"] = f"Could not generate report: {ex}"
    return render_template(
        "reports_server_perf.html",
        fmt_duration=toast_reports.fmt_duration,
        **ctx,
    )
