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

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=-5))  # CDT for our date range; avoids Windows tzdata issue

SERVICE_JOB_TITLES = {"server", "server trainee", "bartender", "host"}

# Word-boundary keyword fallback when item GUID isn't in the categories lookup.
DRINK_KEYWORD_WORDS = {
    "DRINK", "DRINKS", "BEER", "WINE", "COCKTAIL", "MARG", "MARGARITA", "MOJITO",
    "PALOMA", "SODA", "JUICE", "TEA", "COFFEE", "WATER", "FOUNTAIN", "BOTTLE",
    "BOTTLED", "BUCKET", "PITCHER", "MICHELADA", "SANGRIA", "TEQUILA", "CORONA",
    "MODELO", "MILLER", "SHOT", "SHOTS", "RUM", "VODKA", "WHISKEY", "BOURBON",
    "GIN", "ESPRESSO", "LATTE", "CAPPUCCINO", "AMERICANO", "MILK", "LEMONADE",
    "AGUA", "FRESCA", "ICED",
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
                 refresh: bool = False) -> dict:
    """Compute labor-by-position report for [start, end] inclusive.

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
            person = all_employees.get(emp_guid) or emp_guid[:8]
            reg = float(e.get("regularHours") or 0)
            ot = float(e.get("overtimeHours") or 0)
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

    rows = []
    for title, s in sorted(by_job.items(), key=lambda kv: -kv[1]["labor_cost"]):
        hrs = s["regular_hours"] + s["overtime_hours"]
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
            "people_count": len(s["people"]),
            "hours": hrs,
            "labor_cost": s["labor_cost"],
            "pct_net_sales": (s["labor_cost"] / net_sales * 100) if net_sales > 0 else 0.0,
            "pct_of_labor": (s["labor_cost"] / total_cost * 100) if total_cost else 0.0,
            "shifts": s["shifts"],
            "people": people_list,
        })

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
        "totals": {
            "net_sales": net_sales,
            "labor_cost": total_cost,
            "hours": total_hours,
            "shifts": total_shifts,
            "labor_pct_of_sales": overall_pct,
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
                       refresh: bool = False) -> dict:
    """Compute per-server performance report for [start, end] inclusive."""
    client = ToastClient.shared()
    locations = _resolve_locations(location_filter)
    item_categories = _load_item_categories()

    # Pre-fetch employees + jobs to build the service-employee filter
    employee_lookup: dict[str, str] = {}
    service_employee_guids: set[str] = set()
    for loc, rg in locations.items():
        emps = client.fetch_employees(loc, rg, refresh=refresh)
        jobs = client.fetch_jobs(loc, rg, refresh=refresh)
        service_job_guids_loc = {
            j["guid"] for j in jobs
            if (j.get("title") or "").strip().lower() in SERVICE_JOB_TITLES and not j.get("deleted")
        }
        for e in emps:
            full = " ".join(filter(None, [e.get("firstName"), e.get("lastName")])).strip() \
                   or e.get("email") or e.get("guid", "?")[:8]
            employee_lookup[e["guid"]] = full
            for jr in (e.get("jobReferences") or []):
                if jr.get("guid") in service_job_guids_loc:
                    service_employee_guids.add(e["guid"])
                    break

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
        "by_location": by_location_out,
    }


# ============== formatting helpers (used by templates) ==============

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
