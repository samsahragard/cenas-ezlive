"""Tests for app.services.cena_sql_executor (Subagent B territory).

Hermetic: synthetic SQLite fixtures in tmp_path, monkeypatched
CENA_L3_DATA_DIR / CENA_L3_SRC_* env vars, no network, no secrets.
One optional real-data smoke test is guarded by skipif(file-missing).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from app.services import cena_db_catalog, cena_sql_executor as ex
from app.services.cena_sql_executor import (
    CenaSqlError,
    refresh_snapshots,
    run_readonly_sql,
    snapshot_status,
)

REAL_ORDERSDC = r"C:\Users\sam\cena-driverdc\_live\ordersdc.sqlite"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_source(path, ddl_rows):
    conn = sqlite3.connect(str(path))
    for ddl, rows in ddl_rows:
        conn.execute(ddl)
        if rows:
            table = ddl.split("(")[0].split()[-1]
            ph = ",".join("?" * len(rows[0]))
            conn.executemany(f"INSERT INTO {table} VALUES ({ph})", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def snap_env(tmp_path, monkeypatch):
    """Synthetic sources for all 5 aliases + monkeypatched env. No refresh yet."""
    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CENA_L3_DATA_DIR", str(data_dir))

    _make_source(src_dir / "appdb.db", [
        ("CREATE TABLE orders (id INTEGER, store_key TEXT, total REAL)",
         [(1, "copperfield", 100.0), (2, "tomball", 50.0)]),
    ])
    _make_source(src_dir / "toast.db", [
        ("CREATE TABLE time_entry (id INTEGER, order_id INTEGER, hours REAL)",
         [(10, 1, 7.5), (11, 2, 6.0)]),
    ])
    _make_source(src_dir / "toastdm.db", [
        ("CREATE TABLE dm_schedule (id INTEGER, position TEXT)", [(1, "line")]),
    ])
    _make_source(src_dir / "ordersdc.db", [
        ("CREATE TABLE dm_order (id INTEGER, customer_hash TEXT)", [(1, "abc")]),
    ])
    _make_source(src_dir / "driverdc.db", [
        ("CREATE TABLE dm_driver (id INTEGER, name TEXT)", [(1, "d1")]),
    ])

    env_map = {
        "CENA_L3_SRC_APPDB": src_dir / "appdb.db",
        "CENA_L3_SRC_TOAST": src_dir / "toast.db",
        "CENA_L3_SRC_TOASTDM": src_dir / "toastdm.db",
        "CENA_L3_SRC_ORDERSDC": src_dir / "ordersdc.db",
        "CENA_L3_SRC_DRIVERDC": src_dir / "driverdc.db",
    }
    for k, v in env_map.items():
        monkeypatch.setenv(k, str(v))
    return {"data_dir": data_dir, "src_dir": src_dir, "env_map": env_map}


@pytest.fixture
def refreshed(snap_env):
    """snap_env with snapshots actually built (analytics module stub-tolerated)."""
    result = refresh_snapshots()
    return {**snap_env, "refresh": result}


# ---------------------------------------------------------------------------
# refresh_snapshots / snapshot_status / meta
# ---------------------------------------------------------------------------

def test_refresh_copies_sources_and_writes_meta(snap_env):
    result = refresh_snapshots()
    snap_dir = snap_env["data_dir"] / "snapshots"
    for alias in ("appdb", "toast", "toastdm", "ordersdc", "driverdc"):
        assert (snap_dir / f"{alias}.sqlite").exists(), alias
        status = result["sources"][alias]
        assert status["ok"] is True
        assert status["copied_at"]
        assert status["source"] == str(snap_env["env_map"][f"CENA_L3_SRC_{alias.upper()}"])
        assert status["row_hint"] >= 1
    # snapshot copy is faithful
    conn = sqlite3.connect(str(snap_dir / "appdb.sqlite"))
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
    conn.close()
    # meta written and parseable
    meta = json.loads((snap_dir / "snapshot_meta.json").read_text(encoding="utf-8"))
    assert meta["sources"]["toast"]["ok"] is True
    assert "analytics" in meta
    # analytics build attempted, never fatal (module may or may not exist yet)
    assert isinstance(result["analytics"], dict)
    assert "ok" in result["analytics"]
    # organized DB overlay is derived from the same refresh and maintained with it
    db_root = snap_env["data_dir"] / "DB"
    assert result["db_overlay"]["ok"] is True
    assert (db_root / "catalog.json").exists()
    assert (db_root / "central" / "cena_points.sqlite").exists()
    catalog = json.loads((db_root / "catalog.json").read_text(encoding="utf-8"))
    assert catalog["canonical"]["snapshots"] == str(snap_dir)
    assert catalog["automation"]["refreshes_overlay"] is True
    assert catalog["data_policy"]["central_sanitized"] is True
    assert catalog["data_policy"]["raw_operational_mirrors_local_only"] is True
    assert set(catalog["folders"]) == {
        "central", "analytics", "toast/emp", "toast/labor", "orders/ezcater",
        "drivers", "app", "memory",
    }
    files_by_alias = {entry["alias"]: entry for entry in catalog["files"]}
    for alias in ("appdb", "toast", "toastdm", "ordersdc", "driverdc", "analytics"):
        assert files_by_alias[alias]["copied"] is True
        assert files_by_alias[alias]["exists"] is True
    assert catalog["central"]["data_points"] >= 0


def test_refresh_tolerates_missing_source(snap_env, monkeypatch):
    monkeypatch.setenv("CENA_L3_SRC_TOAST", str(snap_env["src_dir"] / "nope.db"))
    result = refresh_snapshots()
    assert result["sources"]["toast"]["ok"] is False
    assert "error" in result["sources"]["toast"]
    assert not (snap_env["data_dir"] / "snapshots" / "toast.sqlite").exists()
    # others still fine
    assert result["sources"]["appdb"]["ok"] is True
    meta = json.loads(
        (snap_env["data_dir"] / "snapshots" / "snapshot_meta.json").read_text(encoding="utf-8")
    )
    assert meta["sources"]["toast"]["ok"] is False


def test_refresh_removes_stale_db_overlay_copy_for_failed_source(snap_env, monkeypatch):
    refresh_snapshots()
    stale_overlay = snap_env["data_dir"] / "DB" / "toast" / "labor" / "toast.sqlite"
    assert stale_overlay.exists()

    monkeypatch.setenv("CENA_L3_SRC_TOAST", str(snap_env["src_dir"] / "nope.db"))
    result = refresh_snapshots()
    assert result["sources"]["toast"]["ok"] is False
    assert not stale_overlay.exists()
    files_by_alias = {entry["alias"]: entry for entry in result["db_overlay"]["files"]}
    assert files_by_alias["toast"]["copied"] is False
    assert files_by_alias["toast"]["exists"] is False
    assert files_by_alias["toast"]["removed_stale"] is True
    assert result["db_overlay"]["source_refresh"]["failed_aliases"] == ["toast"]


def test_refresh_tolerates_missing_analytics_module(snap_env, monkeypatch):
    monkeypatch.setattr(ex, "_ANALYTICS_MODULE", "app.services._no_such_module_xyz")
    result = refresh_snapshots()
    assert result["analytics"]["ok"] is False
    assert "unavailable" in result["analytics"]["error"]
    assert result["sources"]["appdb"]["ok"] is True  # copy still happened


def test_refresh_tolerates_raising_analytics_build(snap_env, monkeypatch):
    import types, sys
    mod = types.ModuleType("app.services._boom_analytics")
    def _boom(snapshot_dir=None):
        raise RuntimeError("kapow")
    mod.build_analytics_db = _boom
    monkeypatch.setitem(sys.modules, "app.services._boom_analytics", mod)
    monkeypatch.setattr(ex, "_ANALYTICS_MODULE", "app.services._boom_analytics")
    result = refresh_snapshots()
    assert result["analytics"]["ok"] is False
    assert "kapow" in result["analytics"]["error"]


def test_refresh_tolerates_raising_db_overlay_build(snap_env, monkeypatch):
    import types, sys
    mod = types.ModuleType("app.services._boom_db_overlay")

    def _boom(data_dir=None):
        raise RuntimeError("overlay kapow")

    mod.build_db_overlay = _boom
    monkeypatch.setitem(sys.modules, "app.services._boom_db_overlay", mod)
    monkeypatch.setattr(ex, "_DB_CATALOG_MODULE", "app.services._boom_db_overlay")
    result = refresh_snapshots()
    assert result["db_overlay"]["ok"] is False
    assert "overlay kapow" in result["db_overlay"]["error"]
    assert result["sources"]["appdb"]["ok"] is True


def test_db_overlay_extracts_analytics_data_points(tmp_path):
    data_dir = tmp_path / "data"
    snap_dir = data_dir / "snapshots"
    snap_dir.mkdir(parents=True)
    analytics = snap_dir / "cena_analytics.db"
    conn = sqlite3.connect(str(analytics))
    conn.execute(
        """
        CREATE TABLE daily_sales_summary (
          store_key TEXT,
          business_date TEXT,
          net_sales REAL,
          gross_sales REAL,
          order_count INTEGER,
          check_count INTEGER,
          avg_check REAL,
          covers INTEGER,
          instore_net REAL,
          daypart_breakfast_net REAL,
          daypart_lunch_net REAL,
          daypart_dinner_net REAL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO daily_sales_summary VALUES
          ('copperfield', '2026-06-09', 100.0, 120.0, 3, 3, 40.0, 12, NULL, 0, 20, 100)
        """
    )
    conn.commit()
    conn.close()

    result = cena_db_catalog.build_db_overlay(data_dir=str(data_dir))
    central = Path(result["central"]["path"])
    conn = sqlite3.connect(str(central))
    try:
        metrics = {
            row[0]
            for row in conn.execute(
                "SELECT metric FROM data_points WHERE source_table = 'daily_sales_summary'"
            )
        }
    finally:
        conn.close()
    assert {"net_sales", "gross_sales", "covers", "daypart_dinner_net"} <= metrics
    assert "instore_net" not in metrics
    assert result["central"]["data_points"] == 9


def test_snapshot_status_reports_ages_and_missing(refreshed, monkeypatch):
    status = snapshot_status()
    assert status["missing"] == []
    for alias in ("appdb", "toast", "toastdm", "ordersdc", "driverdc"):
        entry = status["snapshots"][alias]
        assert entry["exists"] is True
        assert entry["age_seconds"] >= 0
    assert "exists" in status["analytics"]
    assert status["db_overlay"]["catalog"]["exists"] is True
    assert status["db_overlay"]["central"]["exists"] is True
    # delete one -> shows up in missing
    os.remove(status["snapshots"]["driverdc"]["path"])
    status2 = snapshot_status()
    assert status2["missing"] == ["driverdc"]
    assert status2["snapshots"]["driverdc"]["age_seconds"] is None


def test_refresh_data_dir_param_overrides_env(snap_env, tmp_path):
    alt = tmp_path / "alt_data"
    result = refresh_snapshots(data_dir=str(alt))
    assert (alt / "snapshots" / "appdb.sqlite").exists()
    assert result["data_dir"] == str(alt)


# ---------------------------------------------------------------------------
# run_readonly_sql - happy paths
# ---------------------------------------------------------------------------

def test_basic_select_result_shape(refreshed):
    out = run_readonly_sql("SELECT id, store_key FROM appdb.orders ORDER BY id")
    assert out["columns"] == ["id", "store_key"]
    assert out["rows"] == [(1, "copperfield"), (2, "tomball")]
    assert out["row_count"] == 2
    assert out["truncated"] is False
    assert isinstance(out["elapsed_ms"], float) and out["elapsed_ms"] >= 0


def test_cross_schema_join(refreshed):
    out = run_readonly_sql(
        "SELECT o.store_key, t.hours FROM appdb.orders o "
        "JOIN toast.time_entry t ON t.order_id = o.id ORDER BY o.id"
    )
    assert out["rows"] == [("copperfield", 7.5), ("tomball", 6.0)]


def test_unqualified_analytics_table_when_analytics_db_present(refreshed):
    snap_dir = refreshed["data_dir"] / "snapshots"
    conn = sqlite3.connect(str(snap_dir / "cena_analytics.db"))
    # the real cena_sql_analytics builder may have already created this table
    # during refresh_snapshots(); rebuild it deterministically for this test
    conn.execute("DROP TABLE IF EXISTS daily_sales_summary")
    conn.execute("CREATE TABLE daily_sales_summary (store_key TEXT, net_sales REAL)")
    conn.execute("INSERT INTO daily_sales_summary VALUES ('copperfield', 123.0)")
    conn.commit()
    conn.close()
    out = run_readonly_sql("SELECT store_key, net_sales FROM daily_sales_summary")
    assert out["rows"] == [("copperfield", 123.0)]
    # qualified raw tables still reachable alongside analytics MAIN
    out2 = run_readonly_sql("SELECT COUNT(*) FROM ordersdc.dm_order")
    assert out2["rows"] == [(1,)]


def test_with_cte_allowed(refreshed):
    out = run_readonly_sql(
        "WITH t AS (SELECT id FROM appdb.orders) SELECT COUNT(*) FROM t"
    )
    assert out["rows"] == [(2,)]


def test_no_snapshots_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CENA_L3_DATA_DIR", str(tmp_path / "empty"))
    with pytest.raises(CenaSqlError) as ei:
        run_readonly_sql("SELECT 1")
    assert "no snapshots available" in str(ei.value)
    assert "refresh_snapshots" in ei.value.reason


# ---------------------------------------------------------------------------
# Caps: rows, size, timeout
# ---------------------------------------------------------------------------

@pytest.fixture
def big_snap(snap_env):
    _make_source(snap_env["src_dir"] / "appdb.db", [
        ("CREATE TABLE big (n INTEGER, pad TEXT)",
         [(i, "x" * 50) for i in range(1500)]),
    ])
    refresh_snapshots()
    return snap_env


def test_row_cap_enforced_with_truncated_flag(big_snap):
    out = run_readonly_sql("SELECT n FROM appdb.big ORDER BY n")
    assert out["row_count"] == 1000
    assert len(out["rows"]) == 1000
    assert out["truncated"] is True


def test_explicit_large_limit_clamped(big_snap):
    out = run_readonly_sql("SELECT n FROM appdb.big ORDER BY n LIMIT 1400")
    assert out["row_count"] == 1000
    assert out["truncated"] is True


def test_small_limit_kept_not_truncated(big_snap):
    out = run_readonly_sql("SELECT n FROM appdb.big ORDER BY n LIMIT 5")
    assert out["row_count"] == 5
    assert out["truncated"] is False


def test_exact_rowcount_not_falsely_truncated(refreshed):
    # 2 rows, cap 2: no third row exists -> truncated must stay False
    out = run_readonly_sql("SELECT id FROM appdb.orders", _row_cap=2)
    assert out["row_count"] == 2
    assert out["truncated"] is False


def test_size_cap_truncates(big_snap):
    out = run_readonly_sql("SELECT n, pad FROM appdb.big", _size_cap=2000)
    assert out["truncated"] is True
    assert out["row_count"] < 1000


def test_slow_query_times_out_cleanly(refreshed):
    t0 = time.perf_counter()
    with pytest.raises(CenaSqlError) as ei:
        run_readonly_sql(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT COUNT(*) FROM c",
            _timeout_s=0.2,
        )
    assert "timeout" in str(ei.value)
    assert time.perf_counter() - t0 < 5.0  # interrupted well before default cap


# ---------------------------------------------------------------------------
# SQL gate: non-SELECT rejected BEFORE execution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_sql", [
    "INSERT INTO appdb.orders VALUES (3, 'x', 1.0)",
    "UPDATE appdb.orders SET total = 0",
    "DELETE FROM appdb.orders",
    "DROP TABLE appdb.orders",
    "PRAGMA journal_mode",
    "ATTACH 'evil.db' AS evil",
    "SELECT 1; SELECT 2",
    "SELECT 1; DROP TABLE appdb.orders",
    "CREATE TABLE t (x)",
    "VALUES (1, 2)",
    "",
    "   ",
])
def test_non_select_rejected(tmp_path, monkeypatch, bad_sql):
    # empty data dir: rejection must fire BEFORE any connection is opened,
    # so we must NOT see the 'no snapshots available' error here.
    monkeypatch.setenv("CENA_L3_DATA_DIR", str(tmp_path / "empty"))
    with pytest.raises(CenaSqlError) as ei:
        run_readonly_sql(bad_sql)
    assert "no snapshots" not in str(ei.value)


def test_rejection_reasons_are_specific(refreshed):
    with pytest.raises(CenaSqlError, match="Insert"):
        run_readonly_sql("INSERT INTO appdb.orders VALUES (9, 'x', 0)")
    with pytest.raises(CenaSqlError, match="one statement"):
        run_readonly_sql("SELECT 1; SELECT 2")
    with pytest.raises(CenaSqlError, match="Pragma"):
        run_readonly_sql("PRAGMA table_info(orders)")


# ---------------------------------------------------------------------------
# Writes fail at the CONNECTION level (bypassing the SQL gate)
# ---------------------------------------------------------------------------

def test_write_fails_at_connection_level_on_attached(refreshed):
    conn = ex._open_connection()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO appdb.orders VALUES (99, 'evil', 0)")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE toast.time_entry SET hours = 0")
    finally:
        conn.close()


def test_write_fails_at_connection_level_on_analytics_main(refreshed):
    snap_dir = refreshed["data_dir"] / "snapshots"
    c = sqlite3.connect(str(snap_dir / "cena_analytics.db"))
    # tolerate the real analytics builder having created the table already
    c.execute("DROP TABLE IF EXISTS daily_sales_summary")
    c.execute("CREATE TABLE daily_sales_summary (store_key TEXT)")
    c.commit()
    c.close()
    conn = ex._open_connection()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO daily_sales_summary VALUES ('evil')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE main.evil (x)")
    finally:
        conn.close()


def test_memory_main_still_write_blocked(refreshed):
    # no analytics db -> MAIN is :memory:, but query_only=ON blocks even main
    conn = ex._open_connection()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE main.scratch (x)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_queries_all_succeed(refreshed):
    def worker(i):
        out = run_readonly_sql("SELECT COUNT(*) FROM appdb.orders")
        return out["rows"][0][0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(4)))
    assert results == [2, 2, 2, 2]


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------

def test_cli_status_mode(refreshed, capsys):
    from scripts.refresh_cena_snapshots import main
    rc = main(["--status", "--data-dir", str(refreshed["data_dir"])])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["missing"] == []
    assert out["snapshots"]["appdb"]["exists"] is True


def test_cli_refresh_mode(snap_env, capsys):
    from scripts.refresh_cena_snapshots import main
    rc = main(["--data-dir", str(snap_env["data_dir"])])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sources"]["appdb"]["ok"] is True


# ---------------------------------------------------------------------------
# Optional Postgres path (skipped unless env set) + real-data smoke
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get("CENA_L3_PG_URL"), reason="CENA_L3_PG_URL not set")
def test_pg_readonly_path():
    out = ex.run_readonly_pg("SELECT 1 AS one")
    assert out["rows"] == [(1,)]
    with pytest.raises(CenaSqlError):
        ex.run_readonly_pg("DROP TABLE anything")


@pytest.mark.skipif(not os.path.exists(REAL_ORDERSDC), reason="real ordersdc.sqlite not present")
def test_real_ordersdc_smoke(tmp_path, monkeypatch):
    """Read-only smoke against the real ordersdc mart, snapshotted into tmp."""
    monkeypatch.setenv("CENA_L3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CENA_L3_SRC_ORDERSDC", REAL_ORDERSDC)
    for k in ("APPDB", "TOAST", "TOASTDM", "DRIVERDC"):
        monkeypatch.setenv(f"CENA_L3_SRC_{k}", str(tmp_path / "absent.db"))
    result = refresh_snapshots()
    assert result["sources"]["ordersdc"]["ok"] is True
    out = run_readonly_sql("SELECT COUNT(*) FROM ordersdc.dm_order")
    assert out["rows"][0][0] > 0
