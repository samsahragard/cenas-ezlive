"""C.E.N.A. Level 3 analytics builder (Subagent C territory).

Builds ``cena_analytics.db`` from the snapshot SQLite files that
``cena_sql_executor.refresh_snapshots()`` drops into
``%CENA_L3_DATA_DIR%\\snapshots``. Pure stdlib + sqlite3; aggregation is done
in Python (n is small: hundreds of orders / a few thousand time entries) so
the math is hand-checkable and the same code path is unit-testable against
synthetic fixtures.

Frozen contract: docs/cena_level3_contracts.md section 4. Public API:

    build_analytics_db(snapshot_dir: str | None = None) -> str
    SCHEMA_DOC: str
    TABLE_COLUMNS: dict[str, list[str]]

Build is atomic: writes into a temp file in the same directory, then
``os.replace`` onto ``cena_analytics.db`` so concurrent readers never see a
half-built database. Missing source snapshots are non-fatal: the affected
tables are created EMPTY with the correct (stable, allowlist-ready) schema
and ``build_meta`` records per-table source status.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import statistics
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants / configuration
# --------------------------------------------------------------------------

DEFAULT_DATA_DIR = r"C:\Users\sam\cena-l3data"
ANALYTICS_DB_NAME = "cena_analytics.db"

#: snapshot file names build_analytics_db() reads (read-only). Only ordersdc
#: and toast actually feed analytics tables today; the rest are listed for
#: completeness so build_meta can speak about them if that ever changes.
SALES_SNAPSHOT = "ordersdc.sqlite"
LABOR_SNAPSHOT = "toast.sqlite"

#: dm_order.status values treated as cancelled-type and EXCLUDED from all
#: sales/order aggregates. Verified vocabulary on the real ordersdc
#: (2026-06-09): imported / available / imported_performance / cancelled /
#: approved / delivered / no_show / requested. 'cancelled' and 'no_show' are
#: the cancelled-type ones; the extra synonyms are defensive.
CANCELLED_STATUSES = frozenset(
    {"cancelled", "canceled", "no_show", "noshow", "voided", "rejected", "declined"}
)

#: Fallback store-alias map (mirror of assistant_routing_shared.STORE_ALIASES)
#: used only if that module cannot be imported; keeps this module and its
#: tests hermetic.
_STORE_ALIASES_FALLBACK = {
    "1": "copperfield",
    "store_1": "copperfield",
    "store 1": "copperfield",
    "store_3": "copperfield",
    "store 3": "copperfield",
    "uno": "copperfield",
    "uno mas": "copperfield",
    "copperfield": "copperfield",
    "2": "tomball",
    "store_2": "tomball",
    "store 2": "tomball",
    "store_4": "tomball",
    "store 4": "tomball",
    "dos": "tomball",
    "dos mas": "tomball",
    "tomball": "tomball",
}

try:  # canonical normalizer per contract section 0
    from app.services.assistant_routing_shared import normalize_store_key as _normalize_store
except Exception:  # pragma: no cover - hermetic fallback

    def _normalize_store(raw: object) -> str:
        value = str(raw or "unknown").strip().casefold()
        return _STORE_ALIASES_FALLBACK.get(value, value or "unknown")


# --------------------------------------------------------------------------
# Public schema description (Subagent A embeds SCHEMA_DOC in the LLM context)
# --------------------------------------------------------------------------

TABLE_COLUMNS: dict[str, list[str]] = {
    "daily_sales_summary": [
        "store_key", "business_date", "net_sales", "gross_sales", "order_count",
        "check_count", "avg_check", "covers", "instore_net",
        "daypart_breakfast_net", "daypart_lunch_net", "daypart_dinner_net", "built_at",
    ],
    "daily_labor_summary": [
        "store_key", "business_date", "total_hours", "reg_hours", "ot_hours",
        "labor_cost", "net_sales", "labor_pct", "splh", "employee_count", "built_at",
    ],
    "weekly_rollups": [
        "store_key", "iso_week", "week_start", "net_sales", "order_count",
        "labor_cost", "labor_pct", "splh", "total_hours", "wow_net_sales_delta",
        "wow_net_sales_pct", "wow_labor_pct_delta", "built_at",
    ],
    "item_sales_summary": [
        "store_key", "business_date", "item_name", "category", "qty",
        "net_amount", "built_at",
    ],
    "daypart_comparison": [
        "store_key", "business_date", "daypart", "net_sales", "order_count", "built_at",
    ],
    "same_day_lastweek": [
        "store_key", "business_date", "day_of_week", "net_sales",
        "prev_week_net_sales", "delta", "pct_change",
    ],
    "anomaly_flags": [
        "store_key", "business_date", "metric", "value", "baseline_mean",
        "baseline_std", "z_score", "direction", "built_at",
    ],
    "build_meta": ["table_name", "source_status", "row_count", "built_at"],
}

SCHEMA_DOC = """\
== cena_analytics.db — pre-aggregated analytics C.E.N.A. builds for itself (rebuilt on snapshot refresh) ==
Conventions: store_key is canonical 'copperfield'|'tomball'; business_date is local ISO 'YYYY-MM-DD'
(America/Chicago); built_at is UTC ISO build timestamp; money is REAL USD. SALES = ezCater CATERING
orders only (source ordersdc.dm_order; business_date := delivery_date). Daily in-store restaurant
sales are NOT derivable from any source (period-based only), so instore_net is ALWAYS NULL.

daily_sales_summary(store_key, business_date, net_sales, gross_sales, order_count, check_count,
  avg_check, covers, instore_net, daypart_breakfast_net, daypart_lunch_net, daypart_dinner_net, built_at)
  One row per store/date having >=1 non-cancelled catering order. net_sales = SUM(caterer_total_due)
  = what Cenas receives (THE net business number). gross_sales = SUM(ezcater_total) = customer-paid
  gross incl fees/taxes. check_count = order_count (catering: 1 check per order). avg_check =
  net_sales/order_count. covers = SUM(headcount). Excluded statuses: cancelled, no_show.
  GOTCHAS: (1) Table INCLUDES FUTURE business_dates — real future bookings kept for pipeline
  visibility; filter business_date <= date('now','localtime') when reporting actuals.
  (2) Only the economics ingest lane (status 'imported') carries caterer_total_due; other live
  statuses (available/approved/requested/delivered/imported_performance) still count in order_count
  but contribute NULL to net_sales, so avg_check UNDERSTATES per-order net on mixed days.
  (3) headcount lives on the lifecycle lane, not the economics lane — covers and net_sales come from
  different subsets of orders. (4) NULL net_sales = no order had economics that day, not $0.
  Dayparts (by order window_start local time): breakfast <10:30, lunch 10:30-14:59, dinner >=15:00;
  unknown/NULL time -> lunch. GOTCHA: current source window_start values are date-only midnight
  placeholders (no real time-of-day), treated as unknown -> nearly all net lands in
  daypart_lunch_net. Treat daypart splits as LOW-CONFIDENCE until real window times flow.

daily_labor_summary(store_key, business_date, total_hours, reg_hours, ot_hours, labor_cost,
  net_sales, labor_pct, splh, employee_count, built_at)
  Source toast.time_entry (canonical per-shift labor), one row per store/date with >=1 shift.
  labor_cost = SUM(reg_hours*hourly_rate + 1.5*ot_hours*hourly_rate) — AGGREGATE ONLY; individual
  hourly_rate is never exposed. total_hours = reg+ot. employee_count = COUNT(DISTINCT employee).
  net_sales joined from daily_sales_summary. labor_pct = labor_cost/net_sales*100 (NULL when
  net_sales NULL/0). splh = net_sales/total_hours (sales-per-labor-hour).
  GOTCHAS: (1) labor covers the WHOLE store but net_sales is catering-only, so labor_pct is
  labor-vs-CATERING-net — describe it that way, it is NOT a true labor percentage. (2) shifts with
  missing hourly_rate (small minority) add hours but no cost -> labor_cost slightly understated on
  those days. (3) splh shares gotcha (1): catering net over whole-store hours.

weekly_rollups(store_key, iso_week, week_start, net_sales, order_count, labor_cost, labor_pct,
  splh, total_hours, wow_net_sales_delta, wow_net_sales_pct, wow_labor_pct_delta, built_at)
  ISO weeks (iso_week like '2026-W23', week_start = Monday ISO date), summed from the daily tables,
  ONLY business_date <= today local (future bookings never inflate weeklies). labor_pct/splh are
  recomputed from weekly sums. wow_* compare the SAME store's previous ISO week: delta = this-prev,
  pct = delta/prev*100; NULL when there is no prior-week row (or prev value NULL/0).
  GOTCHA: the current in-progress week is included as a PARTIAL week — its totals and WoW deltas
  reflect only days elapsed so far.

item_sales_summary(store_key, business_date, item_name, category, qty, net_amount, built_at)
  From ordersdc.dm_order_item joined to its order (cancelled-type orders excluded). qty =
  SUM(qty) per store/date/item. net_amount = SUM(line_total).
  GOTCHA: in current source data line_total, unit_price and category are ALWAYS NULL (exporter
  gap) -> net_amount and category are NULL; item_name + qty are the usable signals. Do not infer
  $0 from NULL net_amount.

daypart_comparison(store_key, business_date, daypart, net_sales, order_count, built_at)
  Long-form daypart split of daily_sales_summary; daypart in ('breakfast','lunch','dinner'); rows
  exist only for dayparts with >=1 order. Same daypart gotcha as daily_sales_summary (midnight
  placeholders -> nearly everything is 'lunch').

same_day_lastweek — VIEW (store_key, business_date, day_of_week, net_sales, prev_week_net_sales,
  delta, pct_change)
  daily_sales_summary self-joined to date(business_date,'-7 day') for the same store: this weekday
  vs the same weekday last week. day_of_week is the weekday name ('Monday'..'Sunday').
  prev/delta/pct_change NULL when last week has no row (pct also NULL when prev is 0).

anomaly_flags(store_key, business_date, metric, value, baseline_mean, baseline_std, z_score,
  direction, built_at)
  metric in ('net_sales','labor_pct','avg_check'). Baseline per store/metric = trailing 8 SAME-
  WEEKDAY prior values (>=4 required, sample std > 0); z = (value-mean)/std; row emitted only when
  |z| > 2; direction 'high'|'low'. Only business_date <= today local — future booked orders NEVER
  produce anomalies. Empty table = nothing unusual, not missing data (check build_meta).

build_meta(table_name, source_status, row_count, built_at)
  Build provenance per analytics table: source_status 'ok' | 'partial:...' | 'missing:<snapshot>'
  | 'error:...'. Check here before declaring data absent.
"""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _snapshot_dir(snapshot_dir: str | None) -> Path:
    if snapshot_dir:
        return Path(snapshot_dir)
    base = os.environ.get("CENA_L3_DATA_DIR") or DEFAULT_DATA_DIR
    return Path(base) / "snapshots"


def _today_local() -> date:
    """Local business 'today' (America/Chicago); env CENA_L3_TODAY overrides (tests)."""
    override = os.environ.get("CENA_L3_TODAY")
    if override:
        return date.fromisoformat(override.strip())
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:  # pragma: no cover - tz db unavailable
        return date.today()


def _ro_uri(path: Path) -> str:
    return "file:" + urllib.parse.quote(path.as_posix(), safe="/:") + "?mode=ro"


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_ro_uri(path), uri=True)


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})")


def _parse_window_time(window_start: object) -> tuple[int, int] | None:
    """Extract (hour, minute) from window_start; None when unknown.

    Accepts 'HH:MM[:SS]', ISO 'YYYY-MM-DDTHH:MM:SS' and 'YYYY-MM-DD HH:MM:SS'.
    A time of exactly 00:00 is treated as UNKNOWN: verified on the real
    ordersdc (2026-06-09) that 100% of non-NULL window_start values are
    date-at-midnight placeholders carrying no time-of-day information.
    """
    if window_start is None:
        return None
    s = str(window_start).strip()
    if not s:
        return None
    if "T" in s:
        s = s.split("T", 1)[1]
    elif " " in s and "-" in s.split(" ", 1)[0]:
        s = s.split(" ", 1)[1]
    m = _TIME_RE.match(s)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    if hour == 0 and minute == 0:  # midnight placeholder == unknown
        return None
    return hour, minute


def _daypart(window_start: object) -> str:
    """breakfast < 10:30 <= lunch < 15:00 <= dinner; unknown/NULL -> lunch."""
    parsed = _parse_window_time(window_start)
    if parsed is None:
        return "lunch"
    hm = parsed[0] * 60 + parsed[1]
    if hm < 10 * 60 + 30:
        return "breakfast"
    if hm < 15 * 60:
        return "lunch"
    return "dinner"


def _is_cancelled(status: object) -> bool:
    return str(status or "").strip().casefold() in CANCELLED_STATUSES


def _nsum(current: float | None, value: float | None) -> float | None:
    """SQL-SUM-like add: NULLs skipped; all-NULL stays NULL."""
    if value is None:
        return current
    return (current or 0.0) + value


def _iso_week(d: date) -> tuple[str, str]:
    """Return ('2026-W23', '2026-06-01' Monday week start) for a date."""
    year, week, weekday = d.isocalendar()
    return f"{year}-W{week:02d}", (d - timedelta(days=weekday - 1)).isoformat()


# --------------------------------------------------------------------------
# Aggregation (pure functions over plain row tuples)
# --------------------------------------------------------------------------

def _agg_sales(order_rows, built_at: str):
    """order_rows: (store_key, delivery_date, status, caterer_total_due,
    ezcater_total, headcount, window_start) -> (daily_sales rows, daypart rows)."""
    daily: dict[tuple[str, str], dict] = {}
    for store_raw, ddate, status, net, gross, headcount, wstart in order_rows:
        if not ddate or _is_cancelled(status):
            continue
        key = (_normalize_store(store_raw), str(ddate)[:10])
        acc = daily.setdefault(
            key,
            {
                "net": None, "gross": None, "orders": 0, "covers": None,
                "dp_net": {"breakfast": None, "lunch": None, "dinner": None},
                "dp_orders": {"breakfast": 0, "lunch": 0, "dinner": 0},
            },
        )
        acc["orders"] += 1
        acc["net"] = _nsum(acc["net"], net)
        acc["gross"] = _nsum(acc["gross"], gross)
        if headcount is not None:
            acc["covers"] = int((acc["covers"] or 0) + headcount)
        dp = _daypart(wstart)
        acc["dp_orders"][dp] += 1
        acc["dp_net"][dp] = _nsum(acc["dp_net"][dp], net)

    sales_rows, daypart_rows = [], []
    for (store, d), acc in sorted(daily.items()):
        avg = acc["net"] / acc["orders"] if (acc["net"] is not None and acc["orders"]) else None
        sales_rows.append(
            (store, d, acc["net"], acc["gross"], acc["orders"], acc["orders"], avg,
             acc["covers"], None, acc["dp_net"]["breakfast"], acc["dp_net"]["lunch"],
             acc["dp_net"]["dinner"], built_at)
        )
        for dp in ("breakfast", "lunch", "dinner"):
            if acc["dp_orders"][dp]:
                daypart_rows.append((store, d, dp, acc["dp_net"][dp], acc["dp_orders"][dp], built_at))
    return sales_rows, daypart_rows


def _agg_items(item_rows, built_at: str):
    """item_rows: (store_key, delivery_date, status, name, category, qty, line_total)."""
    agg: dict[tuple, dict] = {}
    for store_raw, ddate, status, name, category, qty, line_total in item_rows:
        if not ddate or _is_cancelled(status):
            continue
        key = (_normalize_store(store_raw), str(ddate)[:10], name or "", category)
        acc = agg.setdefault(key, {"qty": None, "net": None})
        acc["qty"] = _nsum(acc["qty"], qty)
        acc["net"] = _nsum(acc["net"], line_total)
    return [
        (store, d, name, category, acc["qty"], acc["net"], built_at)
        for (store, d, name, category), acc in sorted(
            agg.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2], kv[0][3] or "")
        )
    ]


def _agg_labor(time_rows, net_by_store_date: dict[tuple[str, str], float | None], built_at: str):
    """time_rows: (store_key, business_date, reg_hours, ot_hours, hourly_rate, cena_employee_id)."""
    daily: dict[tuple[str, str], dict] = {}
    for store_raw, bdate, reg, ot, rate, emp in time_rows:
        if not bdate:
            continue
        key = (_normalize_store(store_raw), str(bdate)[:10])
        acc = daily.setdefault(key, {"reg": 0.0, "ot": 0.0, "cost": None, "emps": set()})
        reg = reg or 0.0
        ot = ot or 0.0
        acc["reg"] += reg
        acc["ot"] += ot
        if rate is not None:
            # rows with NULL hourly_rate add hours but no cost (SQL-SUM semantics)
            acc["cost"] = (acc["cost"] or 0.0) + reg * rate + 1.5 * ot * rate
        if emp is not None:
            acc["emps"].add(emp)

    rows = []
    for (store, d), acc in sorted(daily.items()):
        total = acc["reg"] + acc["ot"]
        net = net_by_store_date.get((store, d))
        cost = acc["cost"]
        labor_pct = cost / net * 100.0 if (cost is not None and net) else None
        splh = net / total if (net is not None and total) else None
        rows.append((store, d, total, acc["reg"], acc["ot"], cost, net, labor_pct, splh,
                     len(acc["emps"]), built_at))
    return rows


def _agg_weekly(sales_daily, labor_daily, today: date, built_at: str):
    """sales_daily: {(store, date): (net, order_count)}; labor_daily:
    {(store, date): (labor_cost, total_hours)}. Only dates <= today contribute."""
    weeks: dict[tuple[str, str], dict] = {}
    today_iso = today.isoformat()

    def _acc(store: str, d: str):
        label, week_start = _iso_week(date.fromisoformat(d))
        return weeks.setdefault(
            (store, week_start),
            {"iso_week": label, "net": None, "orders": 0, "cost": None, "hours": 0.0},
        )

    for (store, d), (net, orders) in sales_daily.items():
        if d > today_iso:
            continue
        acc = _acc(store, d)
        acc["net"] = _nsum(acc["net"], net)
        acc["orders"] += orders
    for (store, d), (cost, hours) in labor_daily.items():
        if d > today_iso:
            continue
        acc = _acc(store, d)
        acc["cost"] = _nsum(acc["cost"], cost)
        acc["hours"] += hours or 0.0

    computed: dict[tuple[str, str], dict] = {}
    for (store, week_start), acc in weeks.items():
        net, cost, hours = acc["net"], acc["cost"], acc["hours"]
        computed[(store, week_start)] = {
            "iso_week": acc["iso_week"],
            "net": net,
            "orders": acc["orders"],
            "cost": cost,
            "hours": hours,
            "labor_pct": cost / net * 100.0 if (cost is not None and net) else None,
            "splh": net / hours if (net is not None and hours) else None,
        }

    rows = []
    for (store, week_start), c in sorted(computed.items()):
        prev_start = (date.fromisoformat(week_start) - timedelta(days=7)).isoformat()
        prev = computed.get((store, prev_start))
        wow_delta = wow_pct = wow_lp_delta = None
        if prev is not None:
            if c["net"] is not None and prev["net"] is not None:
                wow_delta = c["net"] - prev["net"]
                if prev["net"]:
                    wow_pct = wow_delta / prev["net"] * 100.0
            if c["labor_pct"] is not None and prev["labor_pct"] is not None:
                wow_lp_delta = c["labor_pct"] - prev["labor_pct"]
        rows.append((store, c["iso_week"], week_start, c["net"], c["orders"], c["cost"],
                     c["labor_pct"], c["splh"], c["hours"], wow_delta, wow_pct,
                     wow_lp_delta, built_at))
    return rows


def _detect_anomalies(series: dict[tuple[str, str], list[tuple[str, float]]], built_at: str):
    """series: {(store, metric): [(business_date, value), ...]} — values non-NULL,
    dates already filtered to <= today. Trailing 8 same-weekday prior values,
    >=4 required, sample std > 0; flag |z| > 2."""
    rows = []
    for (store, metric), points in sorted(series.items()):
        by_weekday: dict[int, list[float]] = {}
        for d_iso, value in sorted(points):
            weekday = date.fromisoformat(d_iso).weekday()
            history = by_weekday.setdefault(weekday, [])
            baseline = history[-8:]
            if len(baseline) >= 4:
                mean = statistics.fmean(baseline)
                std = statistics.stdev(baseline)
                if std > 0:
                    z = (value - mean) / std
                    if abs(z) > 2:
                        rows.append((store, d_iso, metric, value, mean, std, z,
                                     "high" if z > 0 else "low", built_at))
            history.append(value)
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return rows


# --------------------------------------------------------------------------
# Schema DDL
# --------------------------------------------------------------------------

_DDL = """
CREATE TABLE daily_sales_summary(
    store_key TEXT NOT NULL, business_date TEXT NOT NULL,
    net_sales REAL, gross_sales REAL, order_count INTEGER, check_count INTEGER,
    avg_check REAL, covers INTEGER, instore_net REAL,
    daypart_breakfast_net REAL, daypart_lunch_net REAL, daypart_dinner_net REAL,
    built_at TEXT NOT NULL,
    PRIMARY KEY(store_key, business_date));
CREATE TABLE daily_labor_summary(
    store_key TEXT NOT NULL, business_date TEXT NOT NULL,
    total_hours REAL, reg_hours REAL, ot_hours REAL, labor_cost REAL,
    net_sales REAL, labor_pct REAL, splh REAL, employee_count INTEGER,
    built_at TEXT NOT NULL,
    PRIMARY KEY(store_key, business_date));
CREATE TABLE weekly_rollups(
    store_key TEXT NOT NULL, iso_week TEXT NOT NULL, week_start TEXT NOT NULL,
    net_sales REAL, order_count INTEGER, labor_cost REAL, labor_pct REAL,
    splh REAL, total_hours REAL, wow_net_sales_delta REAL, wow_net_sales_pct REAL,
    wow_labor_pct_delta REAL, built_at TEXT NOT NULL,
    PRIMARY KEY(store_key, iso_week));
CREATE TABLE item_sales_summary(
    store_key TEXT NOT NULL, business_date TEXT NOT NULL, item_name TEXT,
    category TEXT, qty REAL, net_amount REAL, built_at TEXT NOT NULL);
CREATE TABLE daypart_comparison(
    store_key TEXT NOT NULL, business_date TEXT NOT NULL, daypart TEXT NOT NULL,
    net_sales REAL, order_count INTEGER, built_at TEXT NOT NULL,
    PRIMARY KEY(store_key, business_date, daypart));
CREATE TABLE anomaly_flags(
    store_key TEXT NOT NULL, business_date TEXT NOT NULL, metric TEXT NOT NULL,
    value REAL, baseline_mean REAL, baseline_std REAL, z_score REAL,
    direction TEXT, built_at TEXT NOT NULL,
    PRIMARY KEY(store_key, business_date, metric));
CREATE TABLE build_meta(
    table_name TEXT PRIMARY KEY, source_status TEXT NOT NULL,
    row_count INTEGER NOT NULL, built_at TEXT NOT NULL);
CREATE VIEW same_day_lastweek AS
    SELECT a.store_key, a.business_date,
           CASE strftime('%w', a.business_date)
                WHEN '0' THEN 'Sunday' WHEN '1' THEN 'Monday' WHEN '2' THEN 'Tuesday'
                WHEN '3' THEN 'Wednesday' WHEN '4' THEN 'Thursday'
                WHEN '5' THEN 'Friday' WHEN '6' THEN 'Saturday' END AS day_of_week,
           a.net_sales,
           b.net_sales AS prev_week_net_sales,
           a.net_sales - b.net_sales AS delta,
           CASE WHEN b.net_sales IS NOT NULL AND b.net_sales <> 0
                THEN (a.net_sales - b.net_sales) * 100.0 / b.net_sales END AS pct_change
    FROM daily_sales_summary a
    LEFT JOIN daily_sales_summary b
      ON b.store_key = a.store_key
     AND b.business_date = date(a.business_date, '-7 day');
"""


# --------------------------------------------------------------------------
# Source readers (read-only)
# --------------------------------------------------------------------------

def _read_sales_source(path: Path):
    """Return (order_rows, item_rows, status) reading ordersdc snapshot read-only."""
    if not path.exists():
        return [], [], f"missing:{path.name}"
    try:
        con = _connect_ro(path)
        try:
            order_rows = con.execute(
                "SELECT store_key, delivery_date, status, caterer_total_due,"
                " ezcater_total, headcount, window_start FROM dm_order"
            ).fetchall()
            item_rows = con.execute(
                "SELECT o.store_key, o.delivery_date, o.status, i.name, i.category,"
                " i.qty, i.line_total"
                " FROM dm_order_item i JOIN dm_order o"
                " ON o.external_order_id = i.external_order_id"
            ).fetchall()
        finally:
            con.close()
        return order_rows, item_rows, "ok"
    except sqlite3.Error as exc:
        log.warning("ordersdc snapshot unreadable (%s): %s", path, exc)
        return [], [], f"error:{exc}"


def _read_labor_source(path: Path):
    """Return (time_rows, status) reading toast snapshot read-only."""
    if not path.exists():
        return [], f"missing:{path.name}"
    try:
        con = _connect_ro(path)
        try:
            time_rows = con.execute(
                "SELECT store_key, business_date, reg_hours, ot_hours, hourly_rate,"
                " cena_employee_id FROM time_entry"
            ).fetchall()
        finally:
            con.close()
        return time_rows, "ok"
    except sqlite3.Error as exc:
        log.warning("toast snapshot unreadable (%s): %s", path, exc)
        return [], f"error:{exc}"


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def _atomic_replace(tmp: Path, final: Path) -> None:
    """os.replace with a bounded retry window for Windows sharing violations.

    SQLite's win32 VFS opens database files WITHOUT FILE_SHARE_DELETE, so the
    replace cannot land while another connection holds cena_analytics.db open.
    Production readers (run_readonly_sql) open per-query and close within the
    5s query cap, so we retry for up to ~5s and then fail loudly. The final db
    is never deleted or truncated: readers always see either the old or the
    new fully-built file.
    """
    last_error: BaseException | None = None
    delay = 0.02
    deadline = time.monotonic() + 5.0
    while True:
        try:
            os.replace(tmp, final)
            return
        except PermissionError as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(delay)
            delay = min(delay * 1.5, 0.25)
    raise PermissionError(
        f"could not atomically replace {final}: a reader is holding it open "
        f"(Windows blocks replacing an open SQLite file); last error: {last_error}"
    ) from last_error


def build_analytics_db(snapshot_dir: str | None = None) -> str:
    """Build cena_analytics.db from snapshot files; returns the built db path.

    Atomic: builds into a temp file in the same directory then os.replace()s
    it over cena_analytics.db, so concurrent readers never see a partial db.
    Missing sources are non-fatal (empty tables + build_meta records status).
    """
    snap = _snapshot_dir(snapshot_dir)
    snap.mkdir(parents=True, exist_ok=True)
    final = snap / ANALYTICS_DB_NAME
    tmp = snap / f"{ANALYTICS_DB_NAME}.tmp-{os.getpid()}"
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = _today_local()

    order_rows, item_rows, sales_status = _read_sales_source(snap / SALES_SNAPSHOT)
    time_rows, labor_status = _read_labor_source(snap / LABOR_SNAPSHOT)

    sales_rows, daypart_rows = _agg_sales(order_rows, built_at)
    item_summary_rows = _agg_items(item_rows, built_at)

    net_by_store_date = {(r[0], r[1]): r[2] for r in sales_rows}
    labor_rows = _agg_labor(time_rows, net_by_store_date, built_at)

    sales_daily = {(r[0], r[1]): (r[2], r[4]) for r in sales_rows}
    labor_daily = {(r[0], r[1]): (r[5], r[2]) for r in labor_rows}
    weekly_rows = _agg_weekly(sales_daily, labor_daily, today, built_at)

    today_iso = today.isoformat()
    series: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for r in sales_rows:
        store, d, net, avg = r[0], r[1], r[2], r[6]
        if d > today_iso:
            continue
        if net is not None:
            series.setdefault((store, "net_sales"), []).append((d, net))
        if avg is not None:
            series.setdefault((store, "avg_check"), []).append((d, avg))
    for r in labor_rows:
        store, d, labor_pct = r[0], r[1], r[7]
        if d > today_iso or labor_pct is None:
            continue
        series.setdefault((store, "labor_pct"), []).append((d, labor_pct))
    anomaly_rows = _detect_anomalies(series, built_at)

    # combined statuses for derived tables
    if sales_status == "ok" and labor_status == "ok":
        derived_status = "ok"
    elif sales_status != "ok" and labor_status != "ok":
        derived_status = f"missing:{SALES_SNAPSHOT}+{LABOR_SNAPSHOT}"
    else:
        bad = sales_status if sales_status != "ok" else labor_status
        derived_status = f"partial:{bad}"
    labor_table_status = labor_status if labor_status != "ok" else (
        "ok" if sales_status == "ok" else f"partial:{sales_status} (net_sales NULL)"
    )

    if tmp.exists():
        tmp.unlink()
    out = sqlite3.connect(tmp)
    try:
        out.executescript("PRAGMA journal_mode=MEMORY;" + _DDL)
        out.executemany(
            "INSERT INTO daily_sales_summary VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", sales_rows
        )
        out.executemany(
            "INSERT INTO daily_labor_summary VALUES (?,?,?,?,?,?,?,?,?,?,?)", labor_rows
        )
        out.executemany(
            "INSERT INTO weekly_rollups VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", weekly_rows
        )
        out.executemany(
            "INSERT INTO item_sales_summary VALUES (?,?,?,?,?,?,?)", item_summary_rows
        )
        out.executemany(
            "INSERT INTO daypart_comparison VALUES (?,?,?,?,?,?)", daypart_rows
        )
        out.executemany(
            "INSERT INTO anomaly_flags VALUES (?,?,?,?,?,?,?,?,?)", anomaly_rows
        )
        view_count = out.execute("SELECT COUNT(*) FROM same_day_lastweek").fetchone()[0]
        meta = [
            ("daily_sales_summary", sales_status, len(sales_rows)),
            ("daily_labor_summary", labor_table_status, len(labor_rows)),
            ("weekly_rollups", derived_status, len(weekly_rows)),
            ("item_sales_summary", sales_status, len(item_summary_rows)),
            ("daypart_comparison", sales_status, len(daypart_rows)),
            ("same_day_lastweek", sales_status, view_count),
            ("anomaly_flags", derived_status, len(anomaly_rows)),
        ]
        out.executemany(
            "INSERT INTO build_meta VALUES (?,?,?,?)",
            [(name, status, count, built_at) for name, status, count in meta],
        )
        out.commit()
    finally:
        out.close()

    try:
        _atomic_replace(tmp, final)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    log.info(
        "built %s (sales=%s labor=%s weekly=%d anomalies=%d)",
        final, sales_status, labor_status, len(weekly_rows), len(anomaly_rows),
    )
    return str(final)
