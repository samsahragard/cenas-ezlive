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
from app.services.role_classifier import classify_role, is_management_position

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=-5))  # CDT for our date range; avoids Windows tzdata issue

SERVICE_JOB_TITLES = {"server", "server trainee", "bartender", "host"}

# Role filter sub-sets for the per-role Performance pages (Sidebar > Insights > Performance)
PERF_ROLE_TITLES = {
    "server":     {"server", "server trainee", "lead server"},
    "bartenders": {"bartender"},
    "all":        SERVICE_JOB_TITLES,
}

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
                 role_filter: str | None = None,
                 redact_management: bool = False,
                 refresh: bool = False) -> dict:
    """Compute labor-by-position report for [start, end] inclusive.

    role_filter: 'boh' / 'foh' / 'all' (or None) — restricts which Toast job
    titles are included. The aggregator still runs over everything (so the
    in-store-context net-sales denominator is unchanged), then filtered rows
    are removed at render time.

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
    "toast":     {"in_store"},
    "online":    {"online"},
    "doordash":  {"doordash"},
    "uber":      {"uber_eats"},
    "ezcater":   {"ezcater"},
    "total":     None,        # include EVERYTHING (in-store + all third-party + ezCater)
    "all":       None,        # legacy: keep treating 'all' as third-party only
}


def third_party_sales_report(start: datetime, end: datetime,
                             location_filter: str | None = None,
                             channel_filter: str | None = None,
                             refresh: bool = False) -> dict:
    """Sales by channel for [start, end] inclusive.

    Default behavior (channel_filter=None or 'all') excludes In Store
    (dine-in) orders — that's the legacy "Third-Party Sales" semantics.

    With channel_filter='toast' returns ONLY in-store orders.
    With channel_filter='online' / 'doordash' / 'uber' returns just that
    one channel (DoorDash address-placeholder detection still applies).
    With channel_filter='total' returns ALL channels including in-store.

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

    # Resolve channel filter
    if channel_filter == "all":
        channel_filter = None
    allowed_keys = SALES_CHANNEL_FILTERS.get(channel_filter, "__legacy__") if channel_filter else None
    if allowed_keys == "__legacy__":
        allowed_keys = None  # unknown filter, default to legacy third-party-only

    # Whether to count In Store orders. For default + 'all' = no (legacy).
    # For 'toast' or 'total' = yes.
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
