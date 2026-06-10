"""Hermetic tests for app/services/cena_sql_analytics.py (Subagent C).

All fixtures are synthetic SQLite files in tmp_path with HAND-COMPUTED
expected values. CENA_L3_TODAY pins 'today' to 2026-06-09 (a Tuesday) so
future-date filtering and week math are deterministic.
"""
from __future__ import annotations

import os
import sqlite3
import statistics
import threading
import time

import pytest

from app.services import cena_sql_analytics as mod
from app.services.cena_sql_analytics import (
    SCHEMA_DOC,
    TABLE_COLUMNS,
    build_analytics_db,
)

TODAY = "2026-06-09"  # Tuesday, ISO week 2026-W24 (week starts Mon 2026-06-08)

REAL_ORDERSDC = os.environ.get(
    "CENA_L3_SRC_ORDERSDC", r"C:\Users\sam\cena-driverdc\_live\ordersdc.sqlite"
)
REAL_TOAST = os.environ.get(
    "CENA_L3_SRC_TOAST", r"C:\Users\sam\cena-perfdb\perf.sqlite"
)


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    monkeypatch.setenv("CENA_L3_TODAY", TODAY)


# --------------------------------------------------------------------------
# Synthetic snapshot builders
# --------------------------------------------------------------------------

def make_snapshots(snap_dir, orders=None, items=(), time_entries=None):
    """Create ordersdc.sqlite / toast.sqlite snapshot fixtures.

    orders: (external_order_id, store_key, delivery_date, window_start,
             status, headcount, ezcater_total, caterer_total_due) or None to
             omit the ordersdc file entirely.
    items: (external_order_id, name, category, qty, line_total)
    time_entries: (store_key, business_date, reg_hours, ot_hours,
                   hourly_rate, cena_employee_id) or None to omit toast file.
    """
    snap_dir.mkdir(parents=True, exist_ok=True)
    if orders is not None:
        con = sqlite3.connect(snap_dir / "ordersdc.sqlite")
        con.executescript(
            """
            CREATE TABLE dm_order(
                external_order_id TEXT, store_key TEXT, delivery_date TEXT,
                window_start TEXT, window_end TEXT, status TEXT,
                headcount INTEGER, ezcater_total REAL, caterer_total_due REAL,
                customer_hash TEXT, generated_at TEXT);
            CREATE TABLE dm_order_item(
                external_order_id TEXT, item_key TEXT, name TEXT,
                category TEXT, menu_group TEXT, qty REAL, unit_price REAL,
                line_total REAL, generated_at TEXT);
            """
        )
        con.executemany(
            "INSERT INTO dm_order(external_order_id, store_key, delivery_date,"
            " window_start, status, headcount, ezcater_total, caterer_total_due)"
            " VALUES (?,?,?,?,?,?,?,?)",
            orders,
        )
        con.executemany(
            "INSERT INTO dm_order_item(external_order_id, name, category, qty,"
            " line_total) VALUES (?,?,?,?,?)",
            items,
        )
        con.commit()
        con.close()
    if time_entries is not None:
        con = sqlite3.connect(snap_dir / "toast.sqlite")
        con.executescript(
            """
            CREATE TABLE time_entry(
                id INTEGER PRIMARY KEY, cena_employee_id INTEGER,
                toast_employee_id TEXT, store_key TEXT, business_date TEXT,
                clock_in TEXT, clock_out TEXT, reg_hours REAL, ot_hours REAL,
                hourly_rate REAL, tips REAL, tips_declared INTEGER,
                needs_review INTEGER, review_reason TEXT, source TEXT);
            """
        )
        con.executemany(
            "INSERT INTO time_entry(store_key, business_date, reg_hours,"
            " ot_hours, hourly_rate, cena_employee_id) VALUES (?,?,?,?,?,?)",
            time_entries,
        )
        con.commit()
        con.close()
    return snap_dir


def build_and_connect(snap_dir):
    path = build_analytics_db(str(snap_dir))
    return sqlite3.connect(path)


def fetch(con, sql, params=()):
    return con.execute(sql, params).fetchall()


# --------------------------------------------------------------------------
# daily_sales_summary / daypart_comparison
# --------------------------------------------------------------------------

SALES_ORDERS = [
    # copperfield (raw store_1) 2026-06-01: daypart boundary cases
    ("a1", "store_1", "2026-06-01", "2026-06-01T10:29:00", "imported", None, 120.0, 100.0),  # breakfast
    ("a2", "store_1", "2026-06-01", "2026-06-01T10:30:00", "imported", 10, 240.0, 200.0),    # lunch
    ("a3", "store_1", "2026-06-01", "2026-06-01T14:59:00", "imported", 5, 60.0, 50.0),       # lunch
    ("a4", "store_1", "2026-06-01", "2026-06-01T15:00:00", "imported", None, 90.0, 80.0),    # dinner
    ("a5", "store_1", "2026-06-01", None, "cancelled", 99, 999.0, 999.0),                    # EXCLUDED
    ("a6", "store_1", "2026-06-01", None, "available", 20, 70.0, None),                      # NULL window -> lunch
    # copperfield 2026-06-02: midnight placeholder -> lunch
    ("b1", "store_1", "2026-06-02", "2026-06-02T00:00:00", "imported", None, 66.0, 60.0),
    # copperfield 2026-06-03: order with no economics at all
    ("c1", "store_1", "2026-06-03", None, "available", 8, None, None),
    # tomball (raw store_2) 2026-06-01: bare HH:MM window format
    ("t1", "store_2", "2026-06-01", "11:00", "imported", 12, 330.0, 300.0),
]

SALES_ITEMS = [
    ("a2", "Fajita Pack", None, 10.0, None),
    ("a2", "Sopapillas", None, 2.0, None),
    ("a3", "Fajita Pack", None, 5.0, None),
    ("a4", "Tableware", "Supplies", 1.0, 12.5),
    ("a5", "Ghost Item", None, 9.0, 9.0),  # on cancelled order -> EXCLUDED
]

SALES_TIME_ENTRIES = [
    ("copperfield", "2026-06-01", 8.0, 0.0, 20.0, 1),   # 160
    ("copperfield", "2026-06-01", 5.0, 2.0, 10.0, 2),   # 50 + 1.5*2*10 = 80
    ("copperfield", "2026-06-01", 4.0, 0.0, None, 3),   # hours only, NULL rate
    ("tomball", "2026-06-03", 6.0, 0.0, 15.0, 9),       # labor day with zero sales
]


@pytest.fixture()
def sales_db(tmp_path):
    snap = make_snapshots(tmp_path / "snapshots", orders=SALES_ORDERS,
                          items=SALES_ITEMS, time_entries=SALES_TIME_ENTRIES)
    con = build_and_connect(snap)
    yield con
    con.close()


def test_daily_sales_summary_math(sales_db):
    row = fetch(
        sales_db,
        "SELECT net_sales, gross_sales, order_count, check_count, avg_check,"
        " covers, instore_net, daypart_breakfast_net, daypart_lunch_net,"
        " daypart_dinner_net FROM daily_sales_summary"
        " WHERE store_key='copperfield' AND business_date='2026-06-01'",
    )
    assert row == [(430.0, 580.0, 5, 5, 86.0, 35, None, 100.0, 250.0, 80.0)]


def test_daily_sales_midnight_placeholder_goes_to_lunch(sales_db):
    row = fetch(
        sales_db,
        "SELECT net_sales, daypart_breakfast_net, daypart_lunch_net,"
        " daypart_dinner_net FROM daily_sales_summary"
        " WHERE store_key='copperfield' AND business_date='2026-06-02'",
    )
    assert row == [(60.0, None, 60.0, None)]


def test_daily_sales_no_economics_day(sales_db):
    row = fetch(
        sales_db,
        "SELECT net_sales, gross_sales, order_count, avg_check, covers"
        " FROM daily_sales_summary"
        " WHERE store_key='copperfield' AND business_date='2026-06-03'",
    )
    assert row == [(None, None, 1, None, 8)]


def test_daily_sales_store_normalization_and_hhmm_window(sales_db):
    row = fetch(
        sales_db,
        "SELECT net_sales, gross_sales, order_count, avg_check, covers,"
        " daypart_lunch_net FROM daily_sales_summary"
        " WHERE store_key='tomball' AND business_date='2026-06-01'",
    )
    assert row == [(300.0, 330.0, 1, 300.0, 12, 300.0)]
    # no raw store_N keys leak through
    assert fetch(sales_db,
                 "SELECT COUNT(*) FROM daily_sales_summary WHERE store_key LIKE 'store%'"
                 ) == [(0,)]


def test_daypart_comparison_long_form(sales_db):
    rows = fetch(
        sales_db,
        "SELECT daypart, net_sales, order_count FROM daypart_comparison"
        " WHERE store_key='copperfield' AND business_date='2026-06-01'"
        " ORDER BY daypart",
    )
    assert rows == [("breakfast", 100.0, 1), ("dinner", 80.0, 1), ("lunch", 250.0, 3)]
    # day with one no-economics order: lunch row exists with NULL net
    assert fetch(
        sales_db,
        "SELECT daypart, net_sales, order_count FROM daypart_comparison"
        " WHERE store_key='copperfield' AND business_date='2026-06-03'",
    ) == [("lunch", None, 1)]


def test_item_sales_summary(sales_db):
    rows = fetch(
        sales_db,
        "SELECT item_name, category, qty, net_amount FROM item_sales_summary"
        " WHERE store_key='copperfield' AND business_date='2026-06-01'"
        " ORDER BY item_name",
    )
    assert rows == [
        ("Fajita Pack", None, 15.0, None),
        ("Sopapillas", None, 2.0, None),
        ("Tableware", "Supplies", 1.0, 12.5),
    ]
    # items on the cancelled order are excluded
    assert fetch(sales_db,
                 "SELECT COUNT(*) FROM item_sales_summary WHERE item_name='Ghost Item'"
                 ) == [(0,)]


# --------------------------------------------------------------------------
# daily_labor_summary
# --------------------------------------------------------------------------

def test_daily_labor_summary_math(sales_db):
    row = fetch(
        sales_db,
        "SELECT total_hours, reg_hours, ot_hours, labor_cost, net_sales,"
        " labor_pct, splh, employee_count FROM daily_labor_summary"
        " WHERE store_key='copperfield' AND business_date='2026-06-01'",
    )
    # hand math: cost = 8*20 + (5*10 + 1.5*2*10) = 160 + 80 = 240; NULL-rate
    # shift adds 4 hours but no cost. net joined = 430.
    assert len(row) == 1
    total, reg, ot, cost, net, labor_pct, splh, emps = row[0]
    assert (total, reg, ot, cost, net, emps) == (19.0, 17.0, 2.0, 240.0, 430.0, 3)
    assert labor_pct == pytest.approx(240.0 / 430.0 * 100.0)
    assert splh == pytest.approx(430.0 / 19.0)


def test_daily_labor_zero_sales_day_nulls(sales_db):
    row = fetch(
        sales_db,
        "SELECT total_hours, labor_cost, net_sales, labor_pct, splh,"
        " employee_count FROM daily_labor_summary"
        " WHERE store_key='tomball' AND business_date='2026-06-03'",
    )
    assert row == [(6.0, 90.0, None, None, None, 1)]


# --------------------------------------------------------------------------
# weekly_rollups / same_day_lastweek
# --------------------------------------------------------------------------

WEEKLY_ORDERS = [
    ("w1", "store_1", "2026-05-25", None, "imported", None, 110.0, 100.0),
    ("w2", "store_1", "2026-05-26", None, "imported", None, 220.0, 200.0),
    ("w3", "store_1", "2026-06-01", None, "imported", None, 495.0, 450.0),
    ("w4", "store_1", "2026-06-02", None, "imported", None, 165.0, 150.0),
    ("w5", "store_1", "2026-06-08", None, "imported", None, 55.0, 50.0),   # current partial week
    ("w6", "store_1", "2026-07-01", None, "imported", None, 550.0, 500.0),  # FUTURE booking
]

WEEKLY_TIME_ENTRIES = [
    ("copperfield", "2026-05-25", 10.0, 0.0, 10.0, 1),  # cost 100
    ("copperfield", "2026-06-01", 15.0, 0.0, 10.0, 1),  # cost 150
]


@pytest.fixture()
def weekly_db(tmp_path):
    snap = make_snapshots(tmp_path / "snapshots", orders=WEEKLY_ORDERS,
                          time_entries=WEEKLY_TIME_ENTRIES)
    con = build_and_connect(snap)
    yield con
    con.close()


def test_weekly_rollups_wow_math(weekly_db):
    rows = {
        r[0]: r
        for r in fetch(
            weekly_db,
            "SELECT iso_week, week_start, net_sales, order_count, labor_cost,"
            " labor_pct, splh, total_hours, wow_net_sales_delta,"
            " wow_net_sales_pct, wow_labor_pct_delta FROM weekly_rollups"
            " WHERE store_key='copperfield'",
        )
    }
    w22 = rows["2026-W22"]
    assert w22[1:8] == ("2026-05-25", 300.0, 2, 100.0,
                        pytest.approx(100.0 / 300.0 * 100.0),
                        pytest.approx(30.0), 10.0)
    assert w22[8:] == (None, None, None)  # no prior week -> NULL WoW

    w23 = rows["2026-W23"]
    assert w23[1:8] == ("2026-06-01", 600.0, 2, 150.0, pytest.approx(25.0),
                        pytest.approx(40.0), 15.0)
    assert w23[8] == pytest.approx(300.0)             # 600 - 300
    assert w23[9] == pytest.approx(100.0)             # +100% WoW
    assert w23[10] == pytest.approx(25.0 - 100.0 / 3.0)  # 25 - 33.333...


def test_weekly_includes_current_partial_week_excludes_future(weekly_db):
    rows = {
        r[0]: r
        for r in fetch(
            weekly_db,
            "SELECT iso_week, net_sales, order_count FROM weekly_rollups"
            " WHERE store_key='copperfield'",
        )
    }
    assert rows["2026-W24"][1:] == (50.0, 1)   # partial current week included
    assert "2026-W25" not in rows
    assert "2026-W27" not in rows              # future booking week excluded
    # but the future booking IS visible in the daily table (pipeline visibility)
    assert fetch(
        weekly_db,
        "SELECT net_sales FROM daily_sales_summary"
        " WHERE store_key='copperfield' AND business_date='2026-07-01'",
    ) == [(500.0,)]


def test_same_day_lastweek_view(weekly_db):
    row = fetch(
        weekly_db,
        "SELECT day_of_week, net_sales, prev_week_net_sales, delta, pct_change"
        " FROM same_day_lastweek"
        " WHERE store_key='copperfield' AND business_date='2026-06-01'",
    )
    assert len(row) == 1
    dow, net, prev, delta, pct = row[0]
    assert (dow, net, prev) == ("Monday", 450.0, 100.0)
    assert delta == pytest.approx(350.0)
    assert pct == pytest.approx(350.0)
    # earliest row has no prior week
    assert fetch(
        weekly_db,
        "SELECT prev_week_net_sales, delta, pct_change FROM same_day_lastweek"
        " WHERE store_key='copperfield' AND business_date='2026-05-25'",
    ) == [(None, None, None)]


# --------------------------------------------------------------------------
# anomaly_flags
# --------------------------------------------------------------------------

BASELINE_MONDAYS = ["2026-04-13", "2026-04-20", "2026-04-27", "2026-05-04",
                    "2026-05-11", "2026-05-18", "2026-05-25", "2026-06-01"]
BASELINE_VALUES = [100.0, 102.0, 98.0, 100.0, 101.0, 99.0, 100.0, 100.0]


def _monday_orders(store_raw, prefix, values, spike):
    orders = [
        (f"{prefix}{i}", store_raw, d, None, "imported", None, v + 10.0, v)
        for i, (d, v) in enumerate(zip(BASELINE_MONDAYS, values))
    ]
    orders.append((f"{prefix}x", store_raw, "2026-06-08", None, "imported",
                   None, spike + 10.0, spike))
    return orders


@pytest.fixture()
def anomaly_db(tmp_path):
    orders = _monday_orders("store_1", "cf", BASELINE_VALUES, spike=300.0)
    orders += _monday_orders("store_2", "tb", BASELINE_VALUES, spike=101.0)  # quiet
    # future Monday spike must NEVER flag
    orders.append(("fut", "store_1", "2026-06-15", None, "imported", None,
                   1009.0, 999.0))
    snap = make_snapshots(tmp_path / "snapshots", orders=orders, time_entries=[])
    con = build_and_connect(snap)
    yield con
    con.close()


def test_anomaly_spike_flagged_high(anomaly_db):
    rows = fetch(
        anomaly_db,
        "SELECT business_date, value, baseline_mean, baseline_std, z_score,"
        " direction FROM anomaly_flags"
        " WHERE store_key='copperfield' AND metric='net_sales'",
    )
    assert len(rows) == 1
    bdate, value, mean, std, z, direction = rows[0]
    exp_std = statistics.stdev(BASELINE_VALUES)
    assert (bdate, value, direction) == ("2026-06-08", 300.0, "high")
    assert mean == pytest.approx(100.0)
    assert std == pytest.approx(exp_std)
    assert z == pytest.approx((300.0 - 100.0) / exp_std)
    assert z > 2


def test_anomaly_quiet_series_not_flagged(anomaly_db):
    assert fetch(anomaly_db,
                 "SELECT COUNT(*) FROM anomaly_flags WHERE store_key='tomball'"
                 ) == [(0,)]


def test_anomaly_future_dates_never_flagged(anomaly_db):
    assert fetch(anomaly_db,
                 "SELECT COUNT(*) FROM anomaly_flags WHERE business_date > ?",
                 (TODAY,)) == [(0,)]
    assert fetch(anomaly_db,
                 "SELECT COUNT(*) FROM anomaly_flags WHERE business_date='2026-06-15'"
                 ) == [(0,)]


def test_anomaly_requires_min_4_baseline_points(tmp_path):
    orders = [
        (f"s{i}", "store_1", d, None, "imported", None, v + 10.0, v)
        for i, (d, v) in enumerate(
            zip(["2026-05-18", "2026-05-25", "2026-06-01"], [100.0, 105.0, 95.0])
        )
    ]
    orders.append(("spike", "store_1", "2026-06-08", None, "imported", None,
                   510.0, 500.0))  # only 3 same-weekday priors
    snap = make_snapshots(tmp_path / "snapshots", orders=orders, time_entries=[])
    con = build_and_connect(snap)
    try:
        assert fetch(con, "SELECT COUNT(*) FROM anomaly_flags") == [(0,)]
    finally:
        con.close()


def test_anomaly_zero_std_guard(tmp_path):
    # 8 identical baseline values -> std 0 -> never flag (no ZeroDivisionError)
    orders = _monday_orders("store_1", "cf", [100.0] * 8, spike=300.0)
    snap = make_snapshots(tmp_path / "snapshots", orders=orders, time_entries=[])
    con = build_and_connect(snap)
    try:
        assert fetch(con, "SELECT COUNT(*) FROM anomaly_flags") == [(0,)]
    finally:
        con.close()


# --------------------------------------------------------------------------
# missing sources / build_meta / schema stability
# --------------------------------------------------------------------------

def test_missing_all_sources_empty_tables_with_schema(tmp_path):
    snap = tmp_path / "snapshots"
    snap.mkdir()
    con = sqlite3.connect(build_analytics_db(str(snap)))
    try:
        for table, expected_cols in TABLE_COLUMNS.items():
            cols = [r[1] for r in fetch(con, f"PRAGMA table_info({table})")]
            assert cols == expected_cols, table
            if table != "build_meta":
                assert fetch(con, f"SELECT COUNT(*) FROM {table}") == [(0,)], table
        meta = dict(fetch(con, "SELECT table_name, source_status FROM build_meta"))
        assert meta["daily_sales_summary"] == "missing:ordersdc.sqlite"
        assert meta["daily_labor_summary"] == "missing:toast.sqlite"
        assert meta["weekly_rollups"] == "missing:ordersdc.sqlite+toast.sqlite"
        assert meta["anomaly_flags"] == "missing:ordersdc.sqlite+toast.sqlite"
        assert set(meta) == set(TABLE_COLUMNS) - {"build_meta"}
        counts = dict(fetch(con, "SELECT table_name, row_count FROM build_meta"))
        assert all(c == 0 for c in counts.values())
    finally:
        con.close()


def test_partial_sources_labor_only(tmp_path):
    snap = make_snapshots(tmp_path / "snapshots", orders=None,
                          time_entries=SALES_TIME_ENTRIES)
    con = build_and_connect(snap)
    try:
        assert fetch(con, "SELECT COUNT(*) FROM daily_sales_summary") == [(0,)]
        row = fetch(
            con,
            "SELECT labor_cost, net_sales, labor_pct FROM daily_labor_summary"
            " WHERE store_key='copperfield' AND business_date='2026-06-01'",
        )
        assert row == [(240.0, None, None)]  # labor built, sales join NULL
        meta = dict(fetch(con, "SELECT table_name, source_status FROM build_meta"))
        assert meta["daily_sales_summary"] == "missing:ordersdc.sqlite"
        assert meta["daily_labor_summary"].startswith("partial:")
        assert meta["weekly_rollups"].startswith("partial:")
        # weekly still rolls up labor
        wk = fetch(con,
                   "SELECT net_sales, labor_cost FROM weekly_rollups"
                   " WHERE store_key='copperfield' AND iso_week='2026-W23'")
        assert wk == [(None, 240.0)]
    finally:
        con.close()


def test_schema_doc_and_table_columns_cover_everything():
    for table, cols in TABLE_COLUMNS.items():
        assert table in SCHEMA_DOC, table
        for col in cols:
            assert col in SCHEMA_DOC, f"{table}.{col} missing from SCHEMA_DOC"
    # key gotchas an LLM analyst must see
    for marker in ("instore_net", "FUTURE", "lunch", "catering", "partial",
                   "caterer_total_due", "ezcater_total"):
        assert marker in SCHEMA_DOC


# --------------------------------------------------------------------------
# atomicity / idempotency
# --------------------------------------------------------------------------

def _dump_without_built_at(con):
    dump = {}
    for table, cols in TABLE_COLUMNS.items():
        keep = [c for c in cols if c != "built_at"]
        dump[table] = fetch(
            con, f"SELECT {', '.join(keep)} FROM {table} ORDER BY {keep[0]}, {keep[1]}"
        )
    return dump


def test_rebuild_is_idempotent_and_atomic(tmp_path):
    snap = make_snapshots(tmp_path / "snapshots", orders=SALES_ORDERS,
                          items=SALES_ITEMS, time_entries=SALES_TIME_ENTRIES)
    path = build_analytics_db(str(snap))

    first = sqlite3.connect(path)
    before = _dump_without_built_at(first)
    first.close()
    assert before["daily_sales_summary"]  # non-empty
    expected_sales_rows = len(before["daily_sales_summary"])

    # A second connection hammers open/read/close (the production reader
    # pattern) while we rebuild repeatedly. It must ALWAYS see a complete db:
    # 7 build_meta rows and the full daily_sales_summary - never a half-built
    # or missing file.
    stop = threading.Event()
    errors: list[str] = []

    def hammer():
        while not stop.is_set():
            try:
                con = sqlite3.connect(path)
                meta_n = con.execute("SELECT COUNT(*) FROM build_meta").fetchone()[0]
                sales_n = con.execute(
                    "SELECT COUNT(*) FROM daily_sales_summary"
                ).fetchone()[0]
                con.close()
                if meta_n != 7 or sales_n != expected_sales_rows:
                    errors.append(f"partial read: meta={meta_n} sales={sales_n}")
            except Exception as exc:  # noqa: BLE001 - we record everything
                errors.append(repr(exc))
            time.sleep(0.002)

    thread = threading.Thread(target=hammer)
    thread.start()
    try:
        for _ in range(5):
            assert build_analytics_db(str(snap)) == path  # rebuild mid-reads
    finally:
        stop.set()
        thread.join()
    assert errors == []

    fresh = sqlite3.connect(path)
    try:
        after = _dump_without_built_at(fresh)
        assert after == before  # idempotent rebuild
    finally:
        fresh.close()
    # no temp build files left behind
    leftovers = [p.name for p in snap.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


# --------------------------------------------------------------------------
# optional real-data smoke (read-only backup copies into tmp_path)
# --------------------------------------------------------------------------

@pytest.mark.skipif(
    not (os.path.exists(REAL_ORDERSDC) and os.path.exists(REAL_TOAST)),
    reason="real source DBs not present on this machine",
)
def test_real_data_smoke_daily_sales_matches_raw(tmp_path, monkeypatch):
    monkeypatch.delenv("CENA_L3_TODAY", raising=False)  # use real today
    snap = tmp_path / "snapshots"
    snap.mkdir()
    for src, name in ((REAL_ORDERSDC, "ordersdc.sqlite"), (REAL_TOAST, "toast.sqlite")):
        source = sqlite3.connect(f"file:{src.replace(chr(92), '/')}?mode=ro", uri=True)
        dest = sqlite3.connect(snap / name)
        source.backup(dest)
        dest.close()
        source.close()

    con = build_and_connect(snap)
    try:
        # sample: the store/date with the most orders
        store, bdate, net, orders = fetch(
            con,
            "SELECT store_key, business_date, net_sales, order_count"
            " FROM daily_sales_summary ORDER BY order_count DESC LIMIT 1",
        )[0]
        raw = sqlite3.connect(snap / "ordersdc.sqlite")
        ref_net, ref_orders = raw.execute(
            "SELECT SUM(caterer_total_due), COUNT(*) FROM dm_order"
            " WHERE delivery_date = ?"
            " AND lower(coalesce(status,'')) NOT IN"
            " ('cancelled','canceled','no_show','noshow','voided','rejected','declined')"
            " AND (CASE WHEN store_key IN ('store_1','store_3') THEN 'copperfield'"
            "      WHEN store_key IN ('store_2','store_4') THEN 'tomball'"
            "      ELSE store_key END) = ?",
            (bdate, store),
        ).fetchone()
        raw.close()
        assert orders == ref_orders
        if ref_net is None:
            assert net is None
        else:
            assert net == pytest.approx(ref_net)
        # build_meta says both primary sources were ok
        meta = dict(fetch(con, "SELECT table_name, source_status FROM build_meta"))
        assert meta["daily_sales_summary"] == "ok"
        assert meta["daily_labor_summary"] == "ok"
    finally:
        con.close()
