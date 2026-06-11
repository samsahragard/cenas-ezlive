"""Gate 3 - floor/Toast performance join, REPORT-ONLY (docs/floor_contract.md
section 11).

Joins, for one business date (America/Chicago, same convention as
app/web/floor_routes.py):

- floor DB: seatings (incl. cleared) + sections / section_tables
- Toast: orders via ToastClient.fetch_orders_for_date (READ-ONLY GET; this
  module never writes to Toast)

into a queryable view: per-server covers vs Toast checks/net-sales, per-section
sales, planned-vs-actual server per table, and unmatched-check buckets.

HARD RULE (contract section 11): this module must NOT import, modify, or feed
the existing server-performance scoring code (app/web/reports.py,
app/services/toast_reports.py, perf_* caches). It only REPORTS what the join
WOULD feed scoring. Sam approves that join separately.

Payload notes (defensive by design - Toast order shapes vary by channel):
- table GUID: check-level `table.guid` when present, else order-level
  `table.guid`; takeout/delivery checks have neither.
- server GUID: check-level `server.guid` when present, else order-level
  `server.guid`.
- net sales: `check.amount` (pre-tax net), the same field the existing
  reporting code treats as net sales. Voided/deleted orders and checks are
  skipped.

CLI:
    python -m app.floor_performance <uno|dos|copperfield|tomball> <YYYY-MM-DD>

No process-local app state: reads DB rows + Toast and returns a dict; nothing
is cached or persisted by this module.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date

from app.floor_models import (
    FloorSeating,
    FloorSection,
    FloorSectionTable,
    ToastTableCfg,
)
# Shared conventions (contract sections 1-2): location resolution and the
# UTC window for an America/Chicago business date come from floor_routes so
# this report can never drift from what the floor pages show.
from app.web.floor_routes import _utc_window_for_business_date, resolve_loc

log = logging.getLogger(__name__)

_KEY_TO_SLUG = {"copperfield": "uno", "tomball": "dos"}


# ---------------------------------------------------------------------------
# Resolution + small parsers
# ---------------------------------------------------------------------------

def _resolve_location(location_key_or_slug: str) -> dict:
    """Accepts a store slug (uno|dos) or a Toast location key
    (copperfield|tomball). Returns floor_routes' {slug,key,guid,label}."""
    raw = (location_key_or_slug or "").strip().lower()
    loc = resolve_loc(_KEY_TO_SLUG.get(raw, raw))
    if loc is None:
        raise ValueError(
            f"unknown location {location_key_or_slug!r} "
            "(use uno|dos|copperfield|tomball)")
    return loc


def _parse_date(date_str: str) -> date:
    try:
        return date.fromisoformat(str(date_str or "").strip())
    except ValueError:
        raise ValueError(f"bad date {date_str!r} (use YYYY-MM-DD)") from None


def _open_db():
    """Late-bound SessionLocal (same seam floor_routes/tests use)."""
    from app.db import SessionLocal
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL not set - no DB session available")
    return SessionLocal()


def _ref_guid(value) -> str | None:
    """Toast reference object -> guid string, tolerant of any shape."""
    if isinstance(value, dict):
        guid = str(value.get("guid") or "").strip()
        return guid or None
    return None


def _check_table_guid(order: dict, check: dict) -> str | None:
    return _ref_guid(check.get("table")) or _ref_guid(order.get("table"))


def _check_server_guid(order: dict, check: dict) -> str | None:
    return _ref_guid(check.get("server")) or _ref_guid(order.get("server"))


def _check_amount(check: dict) -> float:
    try:
        return float(check.get("amount") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _iter_live_checks(orders):
    """Yields (order, check) for every non-voided, non-deleted check in
    non-voided, non-deleted orders. Tolerates junk entries at both levels."""
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        if order.get("voided") or order.get("deleted"):
            continue
        checks = order.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            if check.get("voided") or check.get("deleted"):
                continue
            yield order, check


def _employee_name_map(client, loc: dict) -> dict[str, str]:
    """guid -> display name, best effort. Names are display-only (contract
    section 1) so a Toast failure degrades to guid prefixes, never raises."""
    try:
        rows = client.fetch_employees(loc["key"], loc["guid"]) or []
    except Exception:
        log.warning("floor_performance: employee names unavailable for %s",
                    loc["key"])
        return {}
    out: dict[str, str] = {}
    for e in rows:
        if not isinstance(e, dict):
            continue
        guid = str(e.get("guid") or "").strip()
        if not guid:
            continue
        first = str(e.get("firstName") or e.get("chosenName") or "").strip()
        last = str(e.get("lastName") or "").strip()
        name = f"{first} {last}".strip() or str(e.get("email") or "").strip()
        out[guid] = name or guid[:8]
    return out


def _display_name(name_map: dict[str, str], guid: str | None) -> str:
    if not guid:
        return ""
    return name_map.get(guid) or guid[:8]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def performance_for_date(location_key_or_slug: str, date_str: str,
                         *, client=None) -> dict:
    """The Gate 3 queryable view for one location + business date.

    Returns:
        {date, location: {slug,key,guid,label},
         per_server: [{server_employee_guid, server_name, covers_seated,
                       tables_planned, toast_checks, toast_net_sales}],
         per_section: [{section_id, server_employee_guid, color, table_guids,
                        toast_net_sales, toast_checks}],
         planned_vs_actual: [{table_guid, table_name, planned_server_guid,
                              actual_server_guids, match}],
         unmatched: {checks_without_table, checks_on_unsectioned_tables}}

    match is True/False when both a planned server and at least one
    check-derived actual server exist for the table; None otherwise (no
    checks, no plan, or checks whose server is unknown).
    """
    loc = _resolve_location(location_key_or_slug)
    d = _parse_date(date_str)

    if client is None:
        from app.services.toast_client import ToastClient
        client = ToastClient.shared()

    # ---- floor side (DB rows only) ----
    start, end = _utc_window_for_business_date(d)
    db = _open_db()
    try:
        sections = (
            db.query(FloorSection)
            .filter(FloorSection.location_guid == loc["guid"],
                    FloorSection.shift_date == d)
            .order_by(FloorSection.id)
            .all()
        )
        sec_ids = [s.id for s in sections]
        tables_by_sec: dict[int, list[str]] = {sid: [] for sid in sec_ids}
        if sec_ids:
            for st in (db.query(FloorSectionTable)
                       .filter(FloorSectionTable.section_id.in_(sec_ids))
                       .order_by(FloorSectionTable.table_guid)
                       .all()):
                tables_by_sec.setdefault(st.section_id, []).append(st.table_guid)
        seatings = (
            db.query(FloorSeating)
            .filter(FloorSeating.location_guid == loc["guid"],
                    FloorSeating.seated_at >= start,
                    FloorSeating.seated_at < end)
            .all()
        )
        # Include soft-deleted config rows: names for historical GUIDs
        # (contract section 3). Display only.
        table_names = {
            t.guid: t.name
            for t in db.query(ToastTableCfg)
            .filter(ToastTableCfg.location_guid == loc["guid"]).all()
        }
    finally:
        db.close()

    # planned server per table: first section (by id) wins if a table was
    # accidentally placed in two sections.
    planned_server_by_table: dict[str, str] = {}
    planned_tables_by_server: dict[str, int] = {}
    for s in sections:
        tgs = tables_by_sec.get(s.id, [])
        planned_tables_by_server[s.server_employee_guid] = (
            planned_tables_by_server.get(s.server_employee_guid, 0) + len(tgs))
        for tg in tgs:
            planned_server_by_table.setdefault(tg, s.server_employee_guid)

    # covers per server from seatings (incl. cleared), party_size sum.
    covers_by_server: dict[str, int] = {}
    for r in seatings:
        sg = r.server_employee_guid_at_seat
        if sg:
            covers_by_server[sg] = covers_by_server.get(sg, 0) + (r.party_size or 0)

    # ---- Toast side (READ-ONLY GET) ----
    business_date = d.strftime("%Y%m%d")
    orders = client.fetch_orders_for_date(loc["key"], loc["guid"], business_date)

    toast_by_server: dict[str, dict] = {}
    actual_by_table: dict[str, dict] = {}
    checks_without_table = 0
    checks_on_unsectioned_tables = 0

    for order, check in _iter_live_checks(orders):
        table_guid = _check_table_guid(order, check)
        server_guid = _check_server_guid(order, check)
        amount = _check_amount(check)

        if server_guid:
            srec = toast_by_server.setdefault(
                server_guid, {"checks": 0, "net": 0.0})
            srec["checks"] += 1
            srec["net"] += amount

        if table_guid is None:
            checks_without_table += 1
            continue

        trec = actual_by_table.setdefault(
            table_guid, {"server_guids": [], "checks": 0, "net": 0.0})
        trec["checks"] += 1
        trec["net"] += amount
        if server_guid and server_guid not in trec["server_guids"]:
            trec["server_guids"].append(server_guid)

        if table_guid not in planned_server_by_table:
            checks_on_unsectioned_tables += 1

    # ---- assemble the view ----
    name_map = _employee_name_map(client, loc)

    per_section = []
    for s in sections:
        tgs = tables_by_sec.get(s.id, [])
        sec_checks = sum(
            actual_by_table[tg]["checks"] for tg in tgs if tg in actual_by_table)
        sec_net = sum(
            actual_by_table[tg]["net"] for tg in tgs if tg in actual_by_table)
        per_section.append({
            "section_id": s.id,
            "server_employee_guid": s.server_employee_guid,
            "color": s.color,
            "table_guids": tgs,
            "toast_net_sales": round(sec_net, 2),
            "toast_checks": sec_checks,
        })

    server_guids = (set(planned_tables_by_server)
                    | set(covers_by_server)
                    | set(toast_by_server))
    per_server = []
    for sg in server_guids:
        srec = toast_by_server.get(sg, {"checks": 0, "net": 0.0})
        per_server.append({
            "server_employee_guid": sg,
            "server_name": _display_name(name_map, sg),
            "covers_seated": covers_by_server.get(sg, 0),
            "tables_planned": planned_tables_by_server.get(sg, 0),
            "toast_checks": srec["checks"],
            "toast_net_sales": round(srec["net"], 2),
        })
    per_server.sort(key=lambda r: (-r["toast_net_sales"],
                                   r["server_employee_guid"]))

    all_table_guids = set(planned_server_by_table) | set(actual_by_table)
    planned_vs_actual = []
    for tg in sorted(all_table_guids,
                     key=lambda g: (table_names.get(g, ""), g)):
        planned = planned_server_by_table.get(tg)
        actual = actual_by_table.get(tg, {}).get("server_guids", [])
        match = (planned in actual) if (planned and actual) else None
        planned_vs_actual.append({
            "table_guid": tg,
            "table_name": table_names.get(tg, tg[:8]),
            "planned_server_guid": planned,
            "actual_server_guids": actual,
            "match": match,
        })

    return {
        "date": d.isoformat(),
        "location": loc,
        "per_server": per_server,
        "per_section": per_section,
        "planned_vs_actual": planned_vs_actual,
        "unmatched": {
            "checks_without_table": checks_without_table,
            "checks_on_unsectioned_tables": checks_on_unsectioned_tables,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE = ("usage: python -m app.floor_performance "
          "<uno|dos|copperfield|tomball> <YYYY-MM-DD>")


def _logs_to_stderr() -> None:
    """CLI only: importing the app package installs a stdout root log
    handler; stdout must stay pure JSON (the report is meant to be piped),
    so re-point any stdout handlers at stderr. No effect inside the web app
    (this is only called from main())."""
    for h in logging.getLogger().handlers:
        if (isinstance(h, logging.StreamHandler)
                and getattr(h, "stream", None) is sys.stdout):
            h.setStream(sys.stderr)


def main(argv: list[str] | None = None) -> int:
    _logs_to_stderr()
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print(_USAGE, file=sys.stderr)
        return 2
    try:
        report = performance_for_date(args[0], args[1])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
