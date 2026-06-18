"""Compute labor + server-performance reports from Toast POS data.

Pure aggregation logic — no I/O of its own; uses ToastClient for fetching
and returns structured dicts ready for template rendering.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.toast_client import ToastClient, restaurant_guids
from app.services.role_classifier import classify_role, is_management_position, is_owner_position

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=-5))  # CDT for our date range; avoids Windows tzdata issue

SERVICE_JOB_TITLES = {"server", "server trainee", "bartender", "host"}

# Sam #1500/#1504 (2026-05-28): salaried managers report $0 hourlyWage in Toast,
# which understates labor %. Normalize every management position to a flat rate
# (no OT premium) so labor % is accurate WITHOUT exposing any individual's real
# pay. Applies to all permission sets (partners included) — see is_management_position.
MANAGEMENT_LABOR_RATE = 17.25

# Role filter sub-sets for the per-role Performance pages (Sidebar > Insights > Performance)
PERF_ROLE_TITLES = {
    "server":     {"server", "server trainee", "lead server"},
    "bartenders": {"bartender"},
    "all":        SERVICE_JOB_TITLES,
}

# Word-boundary keyword fallback when item GUID isn't in the categories lookup.
DRINK_KEYWORD_WORDS = {
    "DRINK", "DRINKS", "BEER", "WINE", "COCKTAIL", "MARG", "MARGARITA", "RITA", "MOJITO",
    "PALOMA", "SODA", "JUICE", "TEA", "COFFEE", "WATER", "FOUNTAIN", "BOTTLE",
    "BOTTLED", "BUCKET", "PITCHER", "MICHELADA", "SANGRIA", "TEQUILA", "CORONA",
    "MODELO", "MILLER", "SHOT", "SHOTS", "RUM", "VODKA", "WHISKEY", "BOURBON",
    "GIN", "ESPRESSO", "LATTE", "CAPPUCCINO", "AMERICANO", "MILK", "LEMONADE",
    "AGUA", "FRESCA", "ICED", "RANCHWATER",
}

NON_SERVER_NAMES = {
    "DEFAULT ONLINE ORDERING", "TO GO TO GO", "TOGO  SERVER", "TOGO SERVER",
}


def _load_item_categories() -> dict:
    """Load static item-categories lookup bundled with the repo."""
    here = Path(__file__).resolve().parent.parent
    path = here / "static" / "data" / "item_categories.json"
    if not path.exists():
        log.warning("toast: %s not found — falling back to keyword heuristic", path)
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _classify_selection(sel: dict, item_categories: dict) -> str:
    """Return 'drink' | 'appetizer' | 'entree'."""
    item_guid = (sel.get("item") or {}).get("guid")
    if item_guid:
        entry = item_categories.get(item_guid)
        if entry:
            return entry["category"]
    name = sel.get("displayName") or ""
    words = set(re.findall(r"[A-Z]+", name.upper()))
    if words & DRINK_KEYWORD_WORDS:
        return "drink"
    return "entree"


def _parse_iso(s: str | None) -> datetime | None:
    if not s or s.startswith("1970"):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _resolve_locations(location_filter: str | None) -> dict[str, str]:
    """Return {location: restaurant_guid} respecting the filter."""
    all_locs = restaurant_guids()
    if not all_locs:
        raise ValueError(
            "Toast credentials not configured. Set TOAST_CLIENT_ID, "
            "TOAST_CLIENT_SECRET, TOAST_RESTAURANT_GUID_TOMBALL, and "
            "TOAST_RESTAURANT_GUID_COPPERFIELD in the Render env vars."
        )
    if not location_filter or location_filter == "both":
        return all_locs
    if location_filter in all_locs:
        return {location_filter: all_locs[location_filter]}
    raise ValueError(f"unknown location {location_filter!r}; "
                     f"valid: {list(all_locs.keys()) + ['both']}")


# ============== LABOR REPORT ==============

def labor_report(start: datetime, end: datetime,
                 location_filter: str | None = None,
                 role_filter: str | None = None,
                 redact_management: bool = False,
                 refresh: bool = False) -> dict:
    """Compute labor-by-position report for [start, end] inclusive.

    role_filter: 'boh' / 'foh' / 'all' (or None) — restricts which Toast job
    titles are included. The aggregator still runs over everything (so the
    in-store-context net-sales denominator is unchanged), then filtered rows
    are removed at render time.

    Management labor cost is always normalized to a flat MANAGEMENT_LABOR_RATE
    per hour (Sam #1500/#1504) — salaried managers report $0 hourlyWage in Toast
    which understated labor %. This applies to every permission set so the labor
    % is accurate while no individual manager's real pay is ever read or shown.

    Owner / partner titles (role_classifier.is_owner_position) are excluded from
    the report ENTIRELY (Sam #1516) — owners never clock in and must show no
    labor info at all, so their time entries are filtered out before aggregation
    in every view. ('owner' still matches is_management_position so it stays
    redacted anywhere else it could surface; only labor_report drops it.)

    redact_management: when True, management positions (Kitchen Manager,
    Floor Manager, etc. — see role_classifier.is_management_position) only
    show their % Net Sales — people count, hours, dollar amounts, and the
    per-person detail list are zeroed/cleared. Used in Tomball / Copperfield
    / Corporate views. Partner view (owners only) sets this False.

    Returns dict shaped for direct template consumption.
    """
    client = ToastClient.shared()
    locations = _resolve_locations(location_filter)

    all_jobs: dict[str, dict] = {}      # job_guid -> {title, location, deleted}
    all_employees: dict[str, str] = {}   # emp_guid -> "First Last"
    entries_by_loc: dict[str, list] = {}

    for loc, rg in locations.items():
        for e in client.fetch_employees(loc, rg, refresh=refresh):
            full = " ".join(filter(None, [e.get("firstName"), e.get("lastName")])).strip() \
                   or e.get("email") or e.get("guid", "?")[:8]
            all_employees[e["guid"]] = full
        for j in client.fetch_jobs(loc, rg, refresh=refresh):
            all_jobs[j["guid"]] = {
                "title": (j.get("title") or "?").strip(),
                "location": loc,
                "deleted": j.get("deleted", False),
            }
        entries_by_loc[loc] = client.fetch_time_entries(loc, rg, start, end, refresh=refresh)

    # Supplement from db
    try:
        from app.db import SessionLocal
        from app.models import Employee
        db = SessionLocal()
        try:
            db_emps = db.query(Employee).filter(Employee.toast_employee_guid.isnot(None)).all()
            for emp in db_emps:
                all_employees[emp.toast_employee_guid] = emp.toast_employee_name or emp.full_name
        finally:
            db.close()
    except Exception:
        log.exception("labor_report: DB employee seed failed (non-fatal)")

    # Net sales: sum of check.amount across cached order files for the date range.
    # Orders cache is populated by server_perf_report (or refresh=True here pulls them).
    net_sales = 0.0
    sales_files_missing: list[str] = []
    cur = start
    while cur <= end:
        bd = cur.strftime("%Y%m%d")
        for loc, rg in locations.items():
            try:
                orders = client.fetch_orders_for_date(loc, rg, bd, refresh=refresh)
            except Exception as ex:
                log.warning("toast: skipping orders %s/%s: %s", loc, bd, ex)
                sales_files_missing.append(f"{loc}/{bd}")
                continue
            for o in orders:
                if o.get("voided"):
                    continue
                for c in o.get("checks") or []:
                    if c.get("voided") or c.get("deleted"):
                        continue
                    net_sales += float(c.get("amount") or 0)
        cur += timedelta(days=1)

    # Add ezCater revenue to the denominator. ezCater orders never go through
    # Toast (they hit our webhook + own Order DB), so they were previously
    # excluded — making the labor % look worse than reality. Pull totals
    # straight from the Order table for the same date window + location set.
    ezcater_sales = 0.0
    try:
        from app.services.ezcater_revenue import total_ezcater_revenue
        ezc_loc = location_filter if location_filter in ("tomball", "copperfield") else "both"
        ezcater_sales = total_ezcater_revenue(start.date(), end.date(), ezc_loc)
        net_sales += ezcater_sales
    except Exception:
        log.exception("labor_report: ezCater revenue add failed (non-fatal)")

    # Aggregate by job title (collapsing same titles across locations)
    by_job: dict = defaultdict(lambda: {
        "regular_hours": 0.0, "overtime_hours": 0.0, "labor_cost": 0.0,
        "people": defaultdict(lambda: {"hours": 0.0, "cost": 0.0, "locations": set()}),
        "shifts": 0,
    })
    for loc, entries in entries_by_loc.items():
        for e in entries:
            if e.get("deleted"):
                continue
            job_guid = (e.get("jobReference") or {}).get("guid")
            emp_guid = (e.get("employeeReference") or {}).get("guid")
            if not job_guid or not emp_guid:
                continue
            title = all_jobs.get(job_guid, {}).get("title") or "(unknown job)"
            if is_owner_position(title):
                # Sam #1516 (2026-05-28): owners never clock in and must never
                # have any labor info shown. Exclude owner entries entirely —
                # before cost/aggregation — so there is no owner row, cost, %,
                # or per-person detail in ANY permission view.
                continue
            person = all_employees.get(emp_guid) or emp_guid[:8]
            reg = float(e.get("regularHours") or 0)
            ot = float(e.get("overtimeHours") or 0)
            if is_management_position(title):
                # Management normalized to a flat rate (Sam #1500/#1504). Real
                # hourlyWage is never read for managers, so no individual's pay
                # is exposed; flat rate, no OT premium.
                cost = (reg + ot) * MANAGEMENT_LABOR_RATE
            else:
                wage = float(e.get("hourlyWage") or 0)
                cost = reg * wage + ot * wage * 1.5

            slot = by_job[title]
            slot["regular_hours"] += reg
            slot["overtime_hours"] += ot
            slot["labor_cost"] += cost
            slot["shifts"] += 1
            p = slot["people"][person]
            p["hours"] += reg + ot
            p["cost"] += cost
            p["locations"].add(loc)

    total_hours = sum(s["regular_hours"] + s["overtime_hours"] for s in by_job.values())
    total_cost = sum(s["labor_cost"] for s in by_job.values())
    total_shifts = sum(s["shifts"] for s in by_job.values())
    overall_pct = (total_cost / net_sales * 100) if net_sales > 0 else 0.0

    # Apply role filter if requested. We keep the unfiltered totals (computed
    # above) intact since they reflect actual labor cost; the filtered subset
    # is what's RENDERED. We also recompute filtered totals so the KPI strip
    # matches what's in the table.
    role_keep = None
    if role_filter in ("boh", "foh"):
        role_keep = role_filter

    rows = []
    filtered_cost = 0.0
    filtered_hours = 0.0
    filtered_shifts = 0
    for title, s in sorted(by_job.items(), key=lambda kv: -kv[1]["labor_cost"]):
        if role_keep and classify_role(title) != role_keep:
            continue
        hrs = s["regular_hours"] + s["overtime_hours"]
        is_mgmt = is_management_position(title)
        # Compute the row's pct_net_sales BEFORE we accumulate filtered totals.
        # This is the only field that survives redaction.
        row_pct_net_sales = (s["labor_cost"] / net_sales * 100) if net_sales > 0 else 0.0

        if redact_management and is_mgmt:
            # Privacy: people / hours / cost / names hidden in non-Partner views
            rows.append({
                "title": title,
                "role": classify_role(title),
                "redacted": True,
                "people_count": None,
                "hours": None,
                "labor_cost": None,
                "pct_net_sales": row_pct_net_sales,
                "pct_of_labor": None,
                "shifts": None,
                "people": [],   # detail list hidden
            })
            # Still contribute to filtered totals (so the KPI strip is correct)
            # but we hide them per-row.
            filtered_cost += s["labor_cost"]
            filtered_hours += hrs
            filtered_shifts += s["shifts"]
            continue

        people_list = sorted(
            (
                {"name": name, "hours": p["hours"], "cost": p["cost"],
                 "locations": sorted(p["locations"])}
                for name, p in s["people"].items()
            ),
            key=lambda r: -r["cost"],
        )
        rows.append({
            "title": title,
            "role": classify_role(title),
            "redacted": False,
            "people_count": len(s["people"]),
            "hours": hrs,
            "labor_cost": s["labor_cost"],
            "pct_net_sales": row_pct_net_sales,
            "pct_of_labor": (s["labor_cost"] / total_cost * 100) if total_cost else 0.0,
            "shifts": s["shifts"],
            "people": people_list,
        })
        filtered_cost += s["labor_cost"]
        filtered_hours += hrs
        filtered_shifts += s["shifts"]
    # When filtering, surface filtered totals; otherwise use the full ones.
    if role_keep:
        labor_cost_to_show = filtered_cost
        hours_to_show = filtered_hours
        shifts_to_show = filtered_shifts
        labor_pct_to_show = (filtered_cost / net_sales * 100) if net_sales > 0 else 0.0
    else:
        labor_cost_to_show = total_cost
        hours_to_show = total_hours
        shifts_to_show = total_shifts
        labor_pct_to_show = overall_pct

    warnings: list[str] = []
    if sales_files_missing:
        warnings.append(
            f"Orders cache missed {len(sales_files_missing)} location-day(s); "
            f"net sales (and % Net Sales) may be incomplete: "
            f"{', '.join(sales_files_missing[:5])}"
            + ("..." if len(sales_files_missing) > 5 else "")
        )

    return {
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "locations": sorted(locations.keys()),
        "role_filter": role_filter or "all",
        "totals": {
            "net_sales": net_sales,
            "labor_cost": labor_cost_to_show,
            "hours": hours_to_show,
            "shifts": shifts_to_show,
            "labor_pct_of_sales": labor_pct_to_show,
            # Always carry the unfiltered totals as well, so a BOH/FOH page
            # can show "BOH = X% of total labor cost" context.
            "labor_cost_unfiltered": total_cost,
            "hours_unfiltered": total_hours,
        },
        "by_position": rows,
        "warnings": warnings,
    }


# ============== SERVER PERFORMANCE REPORT ==============

def _analyze_check(check: dict, order: dict, item_categories: dict) -> dict | None:
    if check.get("voided") or check.get("deleted"):
        return None
    selections = [s for s in (check.get("selections") or []) if not s.get("voided")]
    payments = [p for p in (check.get("payments") or []) if not p.get("voided")]

    order_opened = _parse_iso(order.get("openedDate"))
    check_opened = _parse_iso(check.get("openedDate"))
    candidates = [d for d in (order_opened, check_opened) if d is not None]
    if not candidates:
        return None
    opened = min(candidates)
    closed = _parse_iso(check.get("closedDate"))

    first_drink = first_app = first_entree = None
    for sel in sorted(selections, key=lambda s: s.get("createdDate") or ""):
        sel_dt = _parse_iso(sel.get("createdDate"))
        if sel_dt is None:
            continue
        cat = _classify_selection(sel, item_categories)
        if cat == "drink" and first_drink is None:
            first_drink = sel_dt
        elif cat == "appetizer" and first_app is None:
            first_app = sel_dt
        elif cat == "entree" and first_entree is None:
            first_entree = sel_dt

    cc_subtotal = 0.0
    cc_tips = 0.0
    cash_amount = 0.0
    cc_ran = None
    for p in payments:
        ptype = p.get("type") or ""
        amt = float(p.get("amount") or 0)
        tip = float(p.get("tipAmount") or 0)
        if ptype == "CREDIT":
            cc_subtotal += amt
            cc_tips += tip
            pd = _parse_iso(p.get("paidDate"))
            if pd and (cc_ran is None or pd < cc_ran):
                cc_ran = pd
        elif ptype == "CASH":
            cash_amount += amt

    return {
        "server_guid": (order.get("server") or {}).get("guid"),
        "opened": opened, "closed": closed,
        "first_drink": first_drink, "first_appetizer": first_app, "first_entree": first_entree,
        "cc_ran": cc_ran,
        "cc_subtotal": cc_subtotal, "cc_tips": cc_tips, "cash_amount": cash_amount,
    }


def server_perf_report(start: datetime, end: datetime,
                       location_filter: str | None = None,
                       role_filter: str | None = None,
                       refresh: bool = False) -> dict:
    """Compute per-server performance report for [start, end] inclusive.

    role_filter: 'server' (Server + Trainee + Lead Server), 'bartenders'
    (Bartender), 'all' / None (current behavior — all FOH service: Server +
    Trainee + Bartender + Host)."""
    client = ToastClient.shared()
    locations = _resolve_locations(location_filter)
    item_categories = _load_item_categories()
    titles_to_keep = PERF_ROLE_TITLES.get(role_filter or "all", SERVICE_JOB_TITLES)

    # Pre-fetch employees + jobs to build the service-employee filter
    employee_lookup: dict[str, str] = {}
    service_employee_guids: set[str] = set()
    for loc, rg in locations.items():
        emps = client.fetch_employees(loc, rg, refresh=refresh)
        jobs = client.fetch_jobs(loc, rg, refresh=refresh)
        service_job_guids_loc = {
            j["guid"] for j in jobs
            if (j.get("title") or "").strip().lower() in titles_to_keep and not j.get("deleted")
        }
        for e in emps:
            full = " ".join(filter(None, [e.get("firstName"), e.get("lastName")])).strip() \
                   or e.get("email") or e.get("guid", "?")[:8]
            employee_lookup[e["guid"]] = full
            for jr in (e.get("jobReferences") or []):
                if jr.get("guid") in service_job_guids_loc:
                    service_employee_guids.add(e["guid"])
                    break

    # Supplement from db
    try:
        from app.db import SessionLocal
        from app.models import Employee
        db = SessionLocal()
        try:
            db_emps = db.query(Employee).filter(Employee.toast_employee_guid.isnot(None)).all()
            for emp in db_emps:
                employee_lookup[emp.toast_employee_guid] = emp.toast_employee_name or emp.full_name
        finally:
            db.close()
    except Exception:
        log.exception("server_perf_report: DB employee seed failed (non-fatal)")

    # Fetch orders per date × location
    dates = []
    d = start
    while d <= end:
        dates.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)

    by_location_out: dict[str, dict] = {}
    for loc, rg in locations.items():
        all_orders = []
        for bd in dates:
            try:
                all_orders.extend(client.fetch_orders_for_date(loc, rg, bd, refresh=refresh))
            except Exception as ex:
                log.warning("toast: skipping orders %s/%s: %s", loc, bd, ex)

        checks = []
        for o in all_orders:
            if o.get("voided"):
                continue
            for c in o.get("checks") or []:
                ac = _analyze_check(c, o, item_categories)
                if ac:
                    checks.append(ac)

        # Aggregate by server
        by_server: dict = {}
        for c in checks:
            sg = c["server_guid"] or "unknown"
            name = employee_lookup.get(sg, sg[:8] if sg != "unknown" else "(unknown)")
            if name.upper() in NON_SERVER_NAMES:
                continue
            if service_employee_guids and sg not in service_employee_guids:
                continue
            if sg not in by_server:
                by_server[sg] = {
                    "name": name, "tickets": 0,
                    "drink_secs": [], "app_secs": [], "entree_secs": [],
                    "gap_secs": [], "duration_secs": [],
                    "cc_subtotal": 0.0, "cc_tips": 0.0, "cash_amount": 0.0,
                }
            s = by_server[sg]
            s["tickets"] += 1
            s["cc_subtotal"] += c["cc_subtotal"]
            s["cc_tips"] += c["cc_tips"]
            s["cash_amount"] += c["cash_amount"]
            if c["first_drink"]:
                s["drink_secs"].append((c["first_drink"] - c["opened"]).total_seconds())
            if c["first_appetizer"]:
                s["app_secs"].append((c["first_appetizer"] - c["opened"]).total_seconds())
            if c["first_entree"]:
                s["entree_secs"].append((c["first_entree"] - c["opened"]).total_seconds())
            if c["first_drink"] and c["first_entree"]:
                s["gap_secs"].append((c["first_entree"] - c["first_drink"]).total_seconds())
            if c["closed"]:
                s["duration_secs"].append((c["closed"] - c["opened"]).total_seconds())

        rows = []
        for sg, s in by_server.items():
            if s["tickets"] == 0:
                continue
            rows.append({
                "name": s["name"],
                "tickets": s["tickets"],
                "avg_drink_secs": statistics.mean(s["drink_secs"]) if s["drink_secs"] else None,
                "avg_app_secs": statistics.mean(s["app_secs"]) if s["app_secs"] else None,
                "app_count": len(s["app_secs"]),
                "avg_entree_secs": statistics.mean(s["entree_secs"]) if s["entree_secs"] else None,
                "avg_gap_secs": statistics.mean(s["gap_secs"]) if s["gap_secs"] else None,
                "avg_duration_secs": statistics.mean(s["duration_secs"]) if s["duration_secs"] else None,
                "cc_subtotal": s["cc_subtotal"],
                "cc_tips": s["cc_tips"],
                "tip_pct": (s["cc_tips"] / s["cc_subtotal"] * 100) if s["cc_subtotal"] > 0 else None,
            })
        rows.sort(key=lambda r: -r["tickets"])

        by_location_out[loc] = {
            "label": loc.title(),
            "total_checks": len(checks),
            "rows": rows,
        }

    return {
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "role_filter": role_filter or "all",
        "by_location": by_location_out,
    }


def server_perf_metrics_for_guid(start: datetime, end: datetime, server_guid: str, location_filter: str | None = None) -> dict:
    """Compute performance metrics (averages) for a specific server GUID in [start, end] range."""
    client = ToastClient.shared()
    locations = _resolve_locations(location_filter)
    item_categories = _load_item_categories()

    dates = []
    d = start
    while d <= end:
        dates.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)

    s = {
        "tickets": 0,
        "drink_secs": [], "app_secs": [], "entree_secs": [],
        "gap_secs": [], "duration_secs": [],
        "cc_subtotal": 0.0, "cc_tips": 0.0, "cash_amount": 0.0,
    }

    for loc, rg in locations.items():
        all_orders = []
        for bd in dates:
            try:
                # refresh is False by default for range cache fetches
                all_orders.extend(client.fetch_orders_for_date(loc, rg, bd, refresh=False))
            except Exception as ex:
                log.warning("toast: server_perf_metrics_for_guid skip orders %s/%s: %s", loc, bd, ex)

        for o in all_orders:
            if o.get("voided"):
                continue
            for c in o.get("checks") or []:
                ac = _analyze_check(c, o, item_categories)
                if ac and ac.get("server_guid") == server_guid:
                    s["tickets"] += 1
                    s["cc_subtotal"] += ac["cc_subtotal"]
                    s["cc_tips"] += ac["cc_tips"]
                    s["cash_amount"] += ac["cash_amount"]
                    if ac["first_drink"]:
                        s["drink_secs"].append((ac["first_drink"] - ac["opened"]).total_seconds())
                    if ac["first_appetizer"]:
                        s["app_secs"].append((ac["first_appetizer"] - ac["opened"]).total_seconds())
                    if ac["first_entree"]:
                        s["entree_secs"].append((ac["first_entree"] - ac["opened"]).total_seconds())
                    if ac["first_drink"] and ac["first_entree"]:
                        s["gap_secs"].append((ac["first_entree"] - ac["first_drink"]).total_seconds())
                    if ac["closed"]:
                        s["duration_secs"].append((ac["closed"] - ac["opened"]).total_seconds())

    return {
        "avg_drink_secs": statistics.mean(s["drink_secs"]) if s["drink_secs"] else None,
        "avg_app_secs": statistics.mean(s["app_secs"]) if s["app_secs"] else None,
        "avg_entree_secs": statistics.mean(s["entree_secs"]) if s["entree_secs"] else None,
        "avg_gap_secs": statistics.mean(s["gap_secs"]) if s["gap_secs"] else None,
        "avg_duration_secs": statistics.mean(s["duration_secs"]) if s["duration_secs"] else None,
    }


def server_tips_for_guids(server_guids, location_filter, business_date,
                          refresh: bool = False) -> dict:
    """LIVE credit-card tips for a SPECIFIC set of Toast server guids on ONE
    business_date -- the scoped primitive behind the employee self-view's live
    'today' tips. Sums cc_tips / cc_subtotal for ONLY the given guids' checks, so
    the caller receives this employee's own aggregate and ZERO cross-employee
    data (the B2 isolation guarantee). Same source + math as server_perf_report
    (payment.tipAmount on CREDIT payments via _analyze_check); cash is excluded
    because Toast does not track cash tips.

    Reads the shared 30-min orders cache (refresh=False) so N employees loading
    their perf tab piggyback the same cache the manager Server-Performance page
    populates -- no extra Toast load. business_date is 'YYYYMMDD' (Central). Any
    fetch error degrades that location to empty (never raises) so the caller can
    fall back to the finalized completed-shift cache."""
    guids = {g for g in (server_guids or set()) if g}
    empty = {"cc_tips": 0.0, "cc_subtotal": 0.0, "tip_pct": None, "tickets": 0}
    if not guids:
        return empty
    client = ToastClient.shared()
    locations = _resolve_locations(location_filter)
    item_categories = _load_item_categories()
    cc_tips = 0.0
    cc_subtotal = 0.0
    tickets = 0
    for loc, rg in locations.items():
        try:
            orders = client.fetch_orders_for_date(loc, rg, business_date, refresh=refresh)
        except Exception as ex:
            log.warning("toast: server_tips skip %s/%s: %s", loc, business_date, ex)
            continue
        for o in orders:
            if o.get("voided"):
                continue
            for c in o.get("checks") or []:
                ac = _analyze_check(c, o, item_categories)
                if not ac or ac.get("server_guid") not in guids:
                    continue
                cc_tips += ac["cc_tips"]
                cc_subtotal += ac["cc_subtotal"]
                tickets += 1
    return {
        "cc_tips": round(cc_tips, 2),
        "cc_subtotal": round(cc_subtotal, 2),
        "tip_pct": (round(cc_tips / cc_subtotal * 100, 1) if cc_subtotal > 0 else None),
        "tickets": tickets,
    }


def _activity_table_label(order: dict, check: dict, table_map: dict[str, str] | None = None) -> str | None:
    table_map = table_map or {}
    table = order.get("table")
    if isinstance(table, dict):
        guid = str(table.get("guid") or "").strip()
        if guid and table_map.get(guid):
            return table_map[guid]
        for key in ("name", "tableNumber", "number"):
            value = str(table.get(key) or "").strip()
            if value:
                return value
    elif table:
        return str(table).strip() or None
    for source in (check, order):
        for key in ("tableName", "table_name", "displayNumber"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return None


def _activity_table_map(client, location: str, restaurant_guid: str) -> dict[str, str]:
    try:
        tables = client.fetch_tables(location, restaurant_guid)
    except Exception as ex:
        log.warning("toast: table lookup skip %s: %s", location, ex)
        return {}
    out: dict[str, str] = {}
    for table in tables or []:
        if not isinstance(table, dict):
            continue
        guid = str(table.get("guid") or "").strip()
        name = str(table.get("name") or "").strip()
        if guid and name:
            out[guid] = name
    return out


def _seconds_between(later: datetime | None, earlier: datetime | None) -> int | None:
    if later is None or earlier is None:
        return None
    return max(0, int(round((later - earlier).total_seconds())))


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def server_activity_for_guids(server_guids, location_filter, business_date,
                              refresh: bool = False, limit: int = 16) -> dict:
    """LIVE same-day service activity for one employee's confirmed Toast guids.

    This is the employee-safe companion to server_perf_report: it filters first
    by the caller's confirmed Toast server guid(s), returns ticket/timing/tip
    metrics and recent table/check activity, and intentionally does NOT expose
    raw card subtotal / sales dollars. The employee self-view can show "how am I
    doing today?" without widening their data scope beyond their own checks.
    """
    guids = {g for g in (server_guids or set()) if g}
    empty = {
        "business_date": business_date,
        "locations": [],
        "tickets": 0,
        "open_checks": 0,
        "closed_checks": 0,
        "cc_tips": 0.0,
        "tip_pct": None,
        "avg_drink_secs": None,
        "avg_app_secs": None,
        "app_count": 0,
        "avg_entree_secs": None,
        "avg_gap_secs": None,
        "avg_duration_secs": None,
        "activities": [],
    }
    if not guids:
        return empty

    client = ToastClient.shared()
    locations = _resolve_locations(location_filter)
    item_categories = _load_item_categories()
    now_utc = datetime.now(timezone.utc)
    totals = {
        "tickets": 0,
        "open_checks": 0,
        "closed_checks": 0,
        "cc_tips": 0.0,
        "cc_subtotal": 0.0,
        "drink_secs": [],
        "app_secs": [],
        "entree_secs": [],
        "gap_secs": [],
        "duration_secs": [],
    }
    activities: list[dict] = []

    for loc, rg in locations.items():
        table_map = _activity_table_map(client, loc, rg)
        try:
            orders = client.fetch_orders_for_date(loc, rg, business_date, refresh=refresh)
        except Exception as ex:
            log.warning("toast: server_activity skip %s/%s: %s", loc, business_date, ex)
            continue
        for order in orders or []:
            if not isinstance(order, dict) or order.get("voided") or order.get("deleted"):
                continue
            for check in order.get("checks") or []:
                if not isinstance(check, dict):
                    continue
                ac = _analyze_check(check, order, item_categories)
                if not ac or ac.get("server_guid") not in guids:
                    continue
                totals["tickets"] += 1
                totals["cc_tips"] += ac["cc_tips"]
                totals["cc_subtotal"] += ac["cc_subtotal"]
                if ac["closed"]:
                    totals["closed_checks"] += 1
                else:
                    totals["open_checks"] += 1
                opened = ac["opened"]
                drink_secs = _seconds_between(ac["first_drink"], opened)
                app_secs = _seconds_between(ac["first_appetizer"], opened)
                entree_secs = _seconds_between(ac["first_entree"], opened)
                gap_secs = _seconds_between(ac["first_entree"], ac["first_drink"])
                duration_secs = _seconds_between(ac["closed"], opened)
                open_for_secs = None if ac["closed"] else _seconds_between(now_utc, opened)
                if drink_secs is not None:
                    totals["drink_secs"].append(drink_secs)
                if app_secs is not None:
                    totals["app_secs"].append(app_secs)
                if entree_secs is not None:
                    totals["entree_secs"].append(entree_secs)
                if gap_secs is not None:
                    totals["gap_secs"].append(gap_secs)
                if duration_secs is not None:
                    totals["duration_secs"].append(duration_secs)
                activities.append({
                    "location": loc,
                    "table_name": _activity_table_label(order, check, table_map),
                    "status": "closed" if ac["closed"] else "open",
                    "opened_at": _iso_utc(opened),
                    "closed_at": _iso_utc(ac["closed"]),
                    "first_drink_at": _iso_utc(ac["first_drink"]),
                    "first_appetizer_at": _iso_utc(ac["first_appetizer"]),
                    "first_entree_at": _iso_utc(ac["first_entree"]),
                    "drink_secs": drink_secs,
                    "app_secs": app_secs,
                    "entree_secs": entree_secs,
                    "gap_secs": gap_secs,
                    "duration_secs": duration_secs,
                    "open_for_secs": open_for_secs,
                    "cc_tips": round(float(ac["cc_tips"] or 0.0), 2),
                    "cc_gross": round(float(ac["cc_subtotal"] or 0.0), 2),
                    "tip_pct": (
                        round(float(ac["cc_tips"] or 0.0) / float(ac["cc_subtotal"] or 0.0) * 100, 1)
                        if float(ac["cc_subtotal"] or 0.0) > 0 else None
                    ),
                    "tip_kind": (
                        "cash" if float(ac.get("cash_amount") or 0.0) > 0
                        and float(ac["cc_tips"] or 0.0) <= 0 else "credit"
                    ),
                })

    activities.sort(key=lambda row: row.get("opened_at") or "", reverse=True)
    return {
        "business_date": business_date,
        "locations": sorted(locations.keys()),
        "tickets": totals["tickets"],
        "open_checks": totals["open_checks"],
        "closed_checks": totals["closed_checks"],
        "cc_tips": round(totals["cc_tips"], 2),
        "tip_pct": (
            round(totals["cc_tips"] / totals["cc_subtotal"] * 100, 1)
            if totals["cc_subtotal"] > 0 else None
        ),
        "avg_drink_secs": statistics.mean(totals["drink_secs"]) if totals["drink_secs"] else None,
        "avg_app_secs": statistics.mean(totals["app_secs"]) if totals["app_secs"] else None,
        "app_count": len(totals["app_secs"]),
        "avg_entree_secs": statistics.mean(totals["entree_secs"]) if totals["entree_secs"] else None,
        "avg_gap_secs": statistics.mean(totals["gap_secs"]) if totals["gap_secs"] else None,
        "avg_duration_secs": statistics.mean(totals["duration_secs"]) if totals["duration_secs"] else None,
        "activities": activities[:max(0, int(limit or 0))],
    }


def _payment_method_type(payment: dict) -> str | None:
    value = str(payment.get("type") or "").strip().upper()
    if not value:
        return None
    if "CREDIT" in value or value in {"VISA", "MASTERCARD", "AMEX", "DISCOVER"}:
        return "Credit"
    if "CASH" in value:
        return "Cash"
    if "GIFT" in value:
        return "Gift card"
    return value.title()


def _payment_status(payment: dict) -> str | None:
    value = str(payment.get("paymentStatus") or "").strip()
    return value.title() if value else None


def _selection_group(selection: dict, item_categories: dict) -> str:
    if not str(selection.get("displayName") or selection.get("name") or "").strip():
        return "other"
    cat = _classify_selection(selection, item_categories)
    if cat == "drink":
        return "drink"
    if cat in {"appetizer", "entree"}:
        return "food"
    return "other"


def _safe_selection_row(selection: dict, item_categories: dict) -> dict | None:
    if not isinstance(selection, dict) or selection.get("voided") or selection.get("deleted"):
        return None
    name = str(selection.get("displayName") or selection.get("name") or "").strip()
    if not name:
        return None
    created = _parse_iso(selection.get("createdDate"))
    try:
        qty = float(selection.get("quantity") or 1)
    except (TypeError, ValueError):
        qty = 1.0
    return {
        "name": name,
        "quantity": qty,
        "created_at": _iso_utc(created),
        "group": _selection_group(selection, item_categories),
    }


def _selection_counts(selections: list[dict]) -> dict[str, int]:
    counts = {"food": 0, "drink": 0, "other": 0}
    for selection in selections:
        group = selection.get("group") or "other"
        counts[group if group in counts else "other"] += 1
    return counts


def server_table_timelines_for_guids(server_guids, location_filter, business_date,
                                     refresh: bool = False, limit: int = 20) -> dict:
    """Employee-safe table/check/item timelines for confirmed Toast server guids.

    Returns names and timestamps needed for the employee "Tables" page while
    omitting raw Toast ids, customer data, card details, sales totals, prices,
    payment amounts, tax, discounts, and tips.
    """
    guids = {g for g in (server_guids or set()) if g}
    empty = {
        "business_date": business_date,
        "locations": [],
        "tickets": 0,
        "timelines": [],
        "raw_payloads_included": False,
    }
    if not guids:
        return empty

    client = ToastClient.shared()
    locations = _resolve_locations(location_filter)
    item_categories = _load_item_categories()
    timelines: list[dict] = []

    for loc, rg in locations.items():
        table_map = _activity_table_map(client, loc, rg)
        try:
            orders = client.fetch_orders_for_date(loc, rg, business_date, refresh=refresh)
        except Exception as ex:
            log.warning("toast: server table timeline skip %s/%s: %s", loc, business_date, ex)
            continue
        for order in orders or []:
            if not isinstance(order, dict) or order.get("voided") or order.get("deleted"):
                continue
            for check in order.get("checks") or []:
                if not isinstance(check, dict):
                    continue
                ac = _analyze_check(check, order, item_categories)
                if not ac or ac.get("server_guid") not in guids:
                    continue
                selections = []
                for selection in sorted(check.get("selections") or [], key=lambda s: s.get("createdDate") or ""):
                    safe = _safe_selection_row(selection, item_categories)
                    if safe:
                        selections.append(safe)
                payment_methods: list[dict] = []
                seen_methods: set[tuple[str | None, str | None]] = set()
                for payment in check.get("payments") or []:
                    if not isinstance(payment, dict) or payment.get("voided") or payment.get("deleted"):
                        continue
                    method = _payment_method_type(payment)
                    status = _payment_status(payment)
                    key = (method, status)
                    if not method or key in seen_methods:
                        continue
                    seen_methods.add(key)
                    payment_methods.append({
                        "method": method,
                        "status": status,
                        "paid_at": _iso_utc(_parse_iso(payment.get("paidDate"))),
                    })
                opened = ac["opened"]
                closed = ac["closed"]
                timelines.append({
                    "location": loc,
                    "table_name": _activity_table_label(order, check, table_map),
                    "display_number": str(check.get("displayNumber") or order.get("displayNumber") or "").strip() or None,
                    "status": "closed" if closed else "open",
                    "opened_at": _iso_utc(opened),
                    "closed_at": _iso_utc(closed),
                    "duration_secs": _seconds_between(closed, opened),
                    "drink_rang_at": _iso_utc(ac["first_drink"]),
                    "food_rang_at": _iso_utc(ac["first_appetizer"] or ac["first_entree"]),
                    "selections": selections,
                    "selection_groups": _selection_counts(selections),
                    "payment_methods": payment_methods,
                })

    timelines.sort(key=lambda row: row.get("opened_at") or "", reverse=True)
    return {
        "business_date": business_date,
        "locations": sorted(locations.keys()),
        "tickets": len(timelines),
        "timelines": timelines[:max(0, int(limit or 0))],
        "raw_payloads_included": False,
    }


# ============== formatting helpers (used by templates) ==============

# ============== THIRD-PARTY SALES REPORT ==============

def _channel_for_order(order: dict) -> tuple[str, str]:
    """Classify a Toast order into (channel_key, channel_label).

    Handles the known channels Cenas Kitchen has seen plus generic detection
    of new third-party providers via deliveryInfo placeholder addresses.
    Returns ("in_store", "In Store") for dine-in (caller filters those out).
    """
    src = (order.get("source") or "").strip()
    if src == "In Store" or not src:
        return "in_store", "In Store"
    # Source 'API' is the integration channel (DoorDash, future UE/GH).
    # Disambiguate via deliveryInfo placeholder address that the third-party
    # uses (DoorDash uses '1 DoorDash Value', SF, 94103; Uber uses similar).
    if src == "API":
        di = order.get("deliveryInfo") or {}
        addr = (di.get("address1") or "").lower()
        city = (di.get("city") or "").lower()
        if "doordash" in addr:
            return "doordash", "DoorDash"
        if "uber" in addr:
            return "uber_eats", "Uber Eats"
        if "grubhub" in addr:
            return "grubhub", "Grubhub"
        if city == "san francisco":  # DD HQ city, fallback signal
            return "doordash", "DoorDash"
        # New third party we haven't seen — surface the channelGuid so it's
        # obvious in the UI and we can label it later.
        cg = (order.get("channelGuid") or "?")[:8]
        return f"api_{cg}", f"Third-party ({cg})"
    if src == "Online":
        return "online", "Toast Online Ordering"
    if src == "Toast Local":
        return "toast_local", "Toast Local"
    if src == "Toast Pickup App":
        return "toast_pickup", "Toast Pickup App"
    # Unknown — keep visible
    return src.lower().replace(" ", "_"), src


# Channel filter → set of allowed channel keys (output of _channel_for_order)
SALES_CHANNEL_FILTERS = {
    "toast":         {"in_store"},
    "online":        {"online"},
    "doordash":      {"doordash"},
    "uber":          {"uber_eats"},
    "toast_local":   {"toast_local"},
    "toast_pickup":  {"toast_pickup"},
    "ezcater":       {"ezcater"},
    "total":         None,    # include EVERYTHING (in-store + all third-party + ezCater)
    "all":           None,    # legacy: keep treating 'all' as third-party only
}


def third_party_sales_report(start: datetime, end: datetime,
                             location_filter: str | None = None,
                             channel_filter: str | None = None,
                             channels: list[str] | None = None,
                             refresh: bool = False) -> dict:
    """Sales by channel for [start, end] inclusive.

    Default behavior (channel_filter=None or 'all') excludes In Store
    (dine-in) orders — that's the legacy "Third-Party Sales" semantics.

    With channel_filter='toast' returns ONLY in-store orders.
    With channel_filter='online' / 'doordash' / 'uber' returns just that
    one channel (DoorDash address-placeholder detection still applies).
    With channel_filter='total' returns ALL channels including in-store.

    `channels` (NEW): a list of channel keys to include — multi-select.
    Built from SALES_CHANNEL_FILTERS keys (toast/online/doordash/uber/ezcater
    plus 'total' or 'all' meaning include everything). When provided, takes
    precedence over channel_filter.

    Pulls Toast orders day-by-day per location, classifies each order into
    a channel, and aggregates: order count, sales, by-day, by-location,
    top items.
    """
    client = ToastClient.shared()
    locations = _resolve_locations(location_filter)

    cur = start
    dates = []
    while cur <= end:
        dates.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)

    # by_channel[key] = { meta + accumulators }
    by_channel: dict = defaultdict(lambda: {
        "label": "?",
        "orders": 0,
        "sales": 0.0,
        "by_day": defaultdict(lambda: {"orders": 0, "sales": 0.0}),
        "by_location": defaultdict(lambda: {"orders": 0, "sales": 0.0}),
        "items": defaultdict(lambda: {"qty": 0, "revenue": 0.0}),
    })

    overall_orders = 0
    overall_sales = 0.0
    grand_total_in_store = 0  # for context

    # Resolve channel filter — multi-channel `channels` takes precedence.
    selected_channels: set[str] | None = None
    if channels:
        # Normalize. 'all' or 'total' anywhere = include everything.
        if any(c.lower() in ("all", "total") for c in channels):
            selected_channels = None  # no restriction
        else:
            allowed = set()
            for c in channels:
                key_set = SALES_CHANNEL_FILTERS.get(c.lower())
                if isinstance(key_set, set):
                    allowed |= key_set
            selected_channels = allowed if allowed else set()
    elif channel_filter:
        if channel_filter == "all":
            channel_filter = None
        if channel_filter:
            ks = SALES_CHANNEL_FILTERS.get(channel_filter, "__legacy__")
            selected_channels = ks if isinstance(ks, set) else None
            if ks == "__legacy__":
                selected_channels = None
    allowed_keys = selected_channels

    # Whether to count In Store orders.
    if channels:
        # Multi-select: include in-store iff 'toast' or 'total'/'all' present
        include_in_store = (
            allowed_keys is None or "in_store" in (allowed_keys or set())
        )
    else:
        # Legacy single-filter behavior
        include_in_store = channel_filter in ("toast", "total")

    for loc, rg in locations.items():
        for bd in dates:
            try:
                orders = client.fetch_orders_for_date(loc, rg, bd, refresh=refresh)
            except Exception as ex:
                log.warning("toast: skipping orders %s/%s: %s", loc, bd, ex)
                continue
            day_iso = f"{bd[:4]}-{bd[4:6]}-{bd[6:8]}"
            for o in orders:
                if o.get("voided"):
                    continue
                key, label = _channel_for_order(o)
                if key == "in_store":
                    grand_total_in_store += 1
                    if not include_in_store:
                        continue
                # Apply channel filter (allowed_keys=None means "no filter beyond
                # the in_store skip already done above")
                if allowed_keys is not None and key not in allowed_keys:
                    continue
                # Net sales for this order = sum of non-voided check.amount
                amt = sum(float(c.get("amount") or 0)
                          for c in (o.get("checks") or [])
                          if not c.get("voided") and not c.get("deleted"))

                slot = by_channel[key]
                slot["label"] = label
                slot["orders"] += 1
                slot["sales"] += amt
                slot["by_day"][day_iso]["orders"] += 1
                slot["by_day"][day_iso]["sales"] += amt
                slot["by_location"][loc]["orders"] += 1
                slot["by_location"][loc]["sales"] += amt
                # Item rollup
                for c in (o.get("checks") or []):
                    if c.get("voided") or c.get("deleted"):
                        continue
                    for sel in (c.get("selections") or []):
                        if sel.get("voided"):
                            continue
                        name = (sel.get("displayName") or "?").strip()
                        qty = float(sel.get("quantity") or 1)
                        price = float(sel.get("price") or 0)
                        slot["items"][name]["qty"] += qty
                        slot["items"][name]["revenue"] += price

                overall_orders += 1
                overall_sales += amt

    # ezCater channel: pulled from our own Order DB (the webhook pipeline,
    # not Toast). Same date range + location filter; only included unless the
    # channel filter explicitly excludes ezcater.
    if allowed_keys is None or "ezcater" in allowed_keys:
        try:
            from app.services.ezcater_revenue import fetch_ezcater_orders
            ezc_loc = location_filter if location_filter in ("tomball", "copperfield") else "both"
            ezc_rows = fetch_ezcater_orders(start.date(), end.date(), ezc_loc)
            if ezc_rows:
                slot = by_channel["ezcater"]
                slot["label"] = "ezCater"
                for r in ezc_rows:
                    slot["orders"] += 1
                    slot["sales"] += r["amount"]
                    slot["by_day"][r["date"]]["orders"] += 1
                    slot["by_day"][r["date"]]["sales"] += r["amount"]
                    slot["by_location"][r["location"]]["orders"] += 1
                    slot["by_location"][r["location"]]["sales"] += r["amount"]
                    overall_orders += 1
                    overall_sales += r["amount"]
        except Exception:
            log.exception("third_party_sales_report: ezCater add failed (non-fatal)")

    # Render-friendly: sort channels by sales desc, build sorted by_day + top items
    channels_out = []
    for key, slot in by_channel.items():
        days_list = [
            {"date": d, "orders": v["orders"], "sales": v["sales"]}
            for d, v in sorted(slot["by_day"].items())
        ]
        loc_list = [
            {"location": loc_key, "label": LOCATION_KEYS_TO_LABEL.get(loc_key, loc_key.title()),
             "orders": v["orders"], "sales": v["sales"]}
            for loc_key, v in sorted(slot["by_location"].items(), key=lambda kv: -kv[1]["sales"])
        ]
        items_list = sorted(
            ({"name": n, "qty": v["qty"], "revenue": v["revenue"]} for n, v in slot["items"].items()),
            key=lambda r: -r["revenue"],
        )[:10]
        channels_out.append({
            "key": key,
            "label": slot["label"],
            "orders": slot["orders"],
            "sales": slot["sales"],
            "avg_ticket": (slot["sales"] / slot["orders"]) if slot["orders"] else 0.0,
            "by_day": days_list,
            "by_location": loc_list,
            "top_items": items_list,
        })
    channels_out.sort(key=lambda r: -r["sales"])

    return {
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "locations": sorted(locations.keys()),
        "channel_filter": channel_filter or "all",
        "by_channel": channels_out,
        "totals": {
            "orders": overall_orders,
            "sales": overall_sales,
            "in_store_orders_for_context": grand_total_in_store,
        },
    }


# Light location label map used by the third-party report
LOCATION_KEYS_TO_LABEL = {"tomball": "Tomball", "copperfield": "Copperfield"}


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 0:
        return "(neg)"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


# ============== ATTENDANCE CLOCK STATUS ==============

def attendance_clock_status(day, location_filter=None, refresh=False):
    """Live clock-in / clock-out status from Toast for a single day.

    Powers the manager Attendance Tracking board. `day` is a date.
    Returns one dict per Toast employee at the resolved location(s):
        name        -> "First Last"
        first, last -> name parts
        job_title   -> primary job title (or "")
        location    -> 'tomball' | 'copperfield'
        status      -> 'clocked-in' (an open punch, still on the clock)
                       | 'out' (clocked in and back out) | 'off' (no punch)
        clock_in    -> naive CDT datetime of the first punch-in, or None
        clock_out   -> naive CDT datetime of the last punch-out, or None
        on_clock    -> bool (status == 'clocked-in')

    Raises ValueError when Toast credentials are not configured, so the
    caller can catch it and fall back to manually-logged attendance.
    """
    client = ToastClient.shared()
    locations = _resolve_locations(location_filter)
    start = datetime(day.year, day.month, day.day)
    end = start

    def _local(dt):
        # Toast inDate/outDate are tz-aware; normalize to naive CDT so
        # the punch times line up with the manually-logged clock times.
        return dt.astimezone(TZ).replace(tzinfo=None) if dt else None

    out: list = []
    for loc, rg in locations.items():
        job_title = {}
        for j in client.fetch_jobs(loc, rg, refresh=refresh):
            job_title[j.get("guid")] = (j.get("title") or "").strip()

        emps = {}
        for e in client.fetch_employees(loc, rg, refresh=refresh):
            if e.get("deleted"):
                continue
            first = (e.get("firstName") or "").strip()
            last = (e.get("lastName") or "").strip()
            full = " ".join(filter(None, [first, last])).strip() \
                or e.get("email") or (e.get("guid") or "?")[:8]
            title = ""
            for jr in (e.get("jobReferences") or []):
                t = job_title.get(jr.get("guid"))
                if t:
                    title = t
                    break
            emps[e.get("guid")] = {
                "first": first, "last": last, "name": full,
                "job_title": title, "location": loc,
            }

        # Collapse the day's time entries per employee. A punch with no
        # clock-out means the teammate is still on the clock right now.
        punches: dict = {}
        for te in client.fetch_time_entries(loc, rg, start, end, refresh=refresh):
            if te.get("deleted"):
                continue
            eg = (te.get("employeeReference") or {}).get("guid")
            if not eg:
                continue
            in_dt = _local(_parse_iso(te.get("inDate")))
            out_dt = _local(_parse_iso(te.get("outDate")))
            p = punches.setdefault(eg, {"in": None, "out": None, "open": False})
            if in_dt and (p["in"] is None or in_dt < p["in"]):
                p["in"] = in_dt
            if in_dt and out_dt is None:
                p["open"] = True
            if out_dt and (p["out"] is None or out_dt > p["out"]):
                p["out"] = out_dt

        for eg, meta in emps.items():
            p = punches.get(eg)
            if not p or p["in"] is None:
                status, ci, co = "off", None, None
            elif p["open"]:
                status, ci, co = "clocked-in", p["in"], None
            else:
                status, ci, co = "out", p["in"], p["out"]
            rec = dict(meta)
            rec.update({"status": status, "clock_in": ci, "clock_out": co,
                        "on_clock": status == "clocked-in"})
            out.append(rec)
    return out


def weekly_schedule_status(week_start, location_filter=None, refresh=False):
    """Per-employee Toast clock data across the Mon..Sun week starting
    `week_start` (a date). Powers the Weekly Schedule grid.

    Built on the same Toast labor API as attendance_clock_status, but
    ranged over a whole week in one fetch_time_entries call per
    location (fetch_time_entries already accepts an inclusive range).

    Returns one dict per Toast employee at the resolved location(s):
        name        -> "First Last"
        first, last -> name parts
        job_title   -> primary job title (or "")
        location    -> 'tomball' | 'copperfield'
        days        -> { 'YYYY-MM-DD': {clock_in, clock_out, minutes,
                                        status} }
                       status: 'open' (still on the clock) | 'worked'.
                       Days with no punch are simply absent from the map.

    Raises ValueError when Toast credentials are not configured, so the
    caller can catch it and fall back to manually-logged shifts.
    """
    client = ToastClient.shared()
    locations = _resolve_locations(location_filter)
    week_end = week_start + timedelta(days=6)
    start = datetime(week_start.year, week_start.month, week_start.day)
    end = datetime(week_end.year, week_end.month, week_end.day)

    def _local(dt):
        # Toast inDate/outDate are tz-aware; normalize to naive CDT.
        return dt.astimezone(TZ).replace(tzinfo=None) if dt else None

    out: list = []
    for loc, rg in locations.items():
        job_title = {}
        for j in client.fetch_jobs(loc, rg, refresh=refresh):
            job_title[j.get("guid")] = (j.get("title") or "").strip()

        emps = {}
        for e in client.fetch_employees(loc, rg, refresh=refresh):
            if e.get("deleted"):
                continue
            first = (e.get("firstName") or "").strip()
            last = (e.get("lastName") or "").strip()
            full = " ".join(filter(None, [first, last])).strip() \
                or e.get("email") or (e.get("guid") or "?")[:8]
            title = ""
            for jr in (e.get("jobReferences") or []):
                t = job_title.get(jr.get("guid"))
                if t:
                    title = t
                    break
            emps[e.get("guid")] = {
                "first": first, "last": last, "name": full,
                "job_title": title, "location": loc,
            }

        # Collapse the week's time entries per employee per calendar day.
        # day_punch: employee_guid -> { iso_date -> {in, out, open} }
        day_punch: dict = {}
        for te in client.fetch_time_entries(loc, rg, start, end, refresh=refresh):
            if te.get("deleted"):
                continue
            eg = (te.get("employeeReference") or {}).get("guid")
            if not eg:
                continue
            in_dt = _local(_parse_iso(te.get("inDate")))
            out_dt = _local(_parse_iso(te.get("outDate")))
            anchor = in_dt or out_dt
            if not anchor:
                continue
            diso = anchor.date().isoformat()
            d = day_punch.setdefault(eg, {}).setdefault(
                diso, {"in": None, "out": None, "open": False})
            if in_dt and (d["in"] is None or in_dt < d["in"]):
                d["in"] = in_dt
            if in_dt and out_dt is None:
                d["open"] = True
            if out_dt and (d["out"] is None or out_dt > d["out"]):
                d["out"] = out_dt

        for eg, meta in emps.items():
            days = {}
            for diso, p in (day_punch.get(eg) or {}).items():
                if p["in"] is None and p["out"] is None:
                    continue
                mins = 0
                if p["in"] and p["out"] and p["out"] > p["in"]:
                    mins = int((p["out"] - p["in"]).total_seconds() // 60)
                days[diso] = {
                    "clock_in": p["in"], "clock_out": p["out"],
                    "minutes": mins,
                    "status": "open" if p["open"] else "worked",
                }
            rec = dict(meta)
            rec["days"] = days
            out.append(rec)
    return out


# ============== SCHEDULE REPORT (Sling → Toast switch, Sam #1018) ==============
#
# Mirror of app.services.sling_reports.schedule_report — same output shape so
# the /reports/schedule template renders unchanged. Source is Toast's
# /labor/v1/shifts (scheduled shifts, not time-entries / clock-ins).

# Toast's scheduled-shift in/out are UTC ISO; render in Central.
_SCHEDULE_TZ = timezone(timedelta(hours=-5))

# Sling-compatible location labels (the /reports/schedule template uses these).
TOAST_SCHEDULE_LOCATIONS = {
    "tomball": ("tomball", "Tomball"),
    "copperfield": ("copperfield", "Copperfield"),
}


def _parse_toast_iso(s):
    """Parse Toast's '2026-05-24T20:00:00.000+0000' into a tz-aware datetime,
    then convert to Central for display. Returns None on falsy / bad input."""
    if not s:
        return None
    try:
        # Handle both '+0000' and '+00:00' forms.
        if s.endswith("+0000"):
            s = s[:-5] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt.astimezone(_SCHEDULE_TZ)
    except Exception:
        return None


def schedule_report(start: datetime, end: datetime,
                    location_filter=None, refresh: bool = False) -> dict:
    """Build the /reports/schedule shape from Toast shifts.
    location_filter: 'both' | 'tomball' | 'copperfield' | None (=both).
    Returns the same dict shape as sling_reports.schedule_report:
      days[].{date,weekday,label,shifts[],shift_count,hours_total}
      by_position[].{title,shifts,hours,people_count}
      by_location{key:{label,shifts,hours}}
      totals{shifts,hours,open_shifts}
      open_shifts[]
    """
    import os
    client = ToastClient.shared()
    rests = restaurant_guids()  # {location_key: guid}
    if not rests:
        raise RuntimeError(
            "TOAST_RESTAURANT_GUID_COPPERFIELD / _TOMBALL not set in env; "
            "schedule_report needs the per-store guids to fetch shifts."
        )

    if location_filter and location_filter != "both":
        wanted_locations = {location_filter}
    else:
        wanted_locations = set(rests.keys())

    rows_by_date: dict = defaultdict(list)
    by_position: dict = defaultdict(lambda: {"shifts": 0, "hours": 0.0,
                                              "people": set()})
    by_location: dict = defaultdict(lambda: {"shifts": 0, "hours": 0.0})
    open_shifts: list = []

    for loc_key, guid in rests.items():
        if loc_key not in wanted_locations:
            continue
        loc_label = TOAST_SCHEDULE_LOCATIONS.get(loc_key, (loc_key, loc_key))[1]

        # Pull employees + jobs once per restaurant (cached).
        try:
            employees = client.fetch_employees(loc_key, guid)
        except Exception as ex:
            log.warning("toast: schedule_report skipping %s employees: %s",
                        loc_key, ex)
            employees = []
        try:
            jobs = client.fetch_jobs(loc_key, guid)
        except Exception as ex:
            log.warning("toast: schedule_report skipping %s jobs: %s",
                        loc_key, ex)
            jobs = []
        try:
            shifts = client.fetch_shifts(loc_key, guid, start, end,
                                         refresh=refresh)
        except Exception as ex:
            log.warning("toast: schedule_report skipping %s shifts: %s",
                        loc_key, ex)
            continue

        # Build lookups (Toast cross-refs by guid).
        emp_by_guid = {e.get("guid"): e for e in (employees or [])}
        job_by_guid = {j.get("guid"): j for j in (jobs or [])}

        for shift in (shifts or []):
            if shift.get("deleted"):
                continue
            in_dt = _parse_toast_iso(shift.get("inDate"))
            out_dt = _parse_toast_iso(shift.get("outDate"))
            if not in_dt:
                continue
            # Range filter (template renders in Central; in_dt is Central).
            if in_dt.date() < start.date() or in_dt.date() > end.date():
                continue

            # Employee name lookup.
            emp_ref = shift.get("employeeReference") or {}
            emp_guid = emp_ref.get("guid")
            emp = emp_by_guid.get(emp_guid) if emp_guid else None
            if emp and not emp.get("deleted"):
                first = (emp.get("firstName") or emp.get("chosenName") or "").strip()
                last = (emp.get("lastName") or "").strip()
                name = (f"{first} {last}".strip()
                        or emp.get("email") or f"emp-{emp_guid[:8]}")
            else:
                name = None  # → open shift

            # Job title (Sling calls this "position").
            job_ref = shift.get("jobReference") or {}
            job_guid = job_ref.get("guid")
            job = job_by_guid.get(job_guid) if job_guid else None
            position = (job.get("title") if job else None) or "(no position)"

            # Hours; Toast scheduled shifts don't carry break duration in
            # the public API the way Sling does — use the raw in/out span.
            hours = ((out_dt - in_dt).total_seconds() / 3600.0
                     if (in_dt and out_dt) else 0.0)
            hours = max(0.0, hours)

            row = {
                "id": shift.get("guid"),
                "status": "published",  # Toast shifts in the API are live
                "in_dt": in_dt,
                "out_dt": out_dt,
                "user_id": emp_guid,
                "name": name,
                "position": position,
                "location_key": loc_key,
                "location_label": loc_label,
                "hours": hours,
                "break_minutes": 0,
                "is_open": name is None,
            }
            if name is None:
                open_shifts.append({**row, "slots": 1})
                continue
            rows_by_date[in_dt.date()].append(row)
            by_position[position]["shifts"] += 1
            by_position[position]["hours"] += hours
            by_position[position]["people"].add(name)
            by_location[loc_key]["shifts"] += 1
            by_location[loc_key]["hours"] += hours

    # Render-friendly shape (identical to sling_reports.schedule_report).
    days = []
    for day in sorted(rows_by_date.keys()):
        shifts_on_day = sorted(rows_by_date[day],
                               key=lambda r: (r["in_dt"], r["position"],
                                              r["name"] or ""))
        days.append({
            "date": day.isoformat(),
            "weekday": day.strftime("%A"),
            "label": day.strftime("%a, %b %d"),
            "shifts": shifts_on_day,
            "shift_count": len(shifts_on_day),
            "hours_total": sum(s["hours"] for s in shifts_on_day),
        })

    by_position_sorted = []
    for title, s in sorted(by_position.items(),
                           key=lambda kv: -kv[1]["hours"]):
        by_position_sorted.append({
            "title": title,
            "shifts": s["shifts"],
            "hours": s["hours"],
            "people_count": len(s["people"]),
        })

    by_location_out = {}
    for key, data in by_location.items():
        _, label = TOAST_SCHEDULE_LOCATIONS.get(key, (key, key))
        by_location_out[key] = {"label": label, **data}

    return {
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "location_filter": location_filter or "both",
        "days": days,
        "by_position": by_position_sorted,
        "by_location": by_location_out,
        "totals": {
            "shifts": sum(d["shift_count"] for d in days),
            "hours": sum(d["hours_total"] for d in days),
            "open_shifts": len(open_shifts),
        },
        "open_shifts": sorted(open_shifts, key=lambda r: r["in_dt"]),
    }
