"""Organized local C.E.N.A. DB overlay.

The canonical Level 3 data remains ``%CENA_L3_DATA_DIR%\\snapshots`` plus
``%CENA_L3_DATA_DIR%\\memory``. This module builds a durable, human-readable
``DB`` folder on every snapshot refresh so C.E.N.A. has one organized local
data home without moving the files the executor already trusts.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

DEFAULT_DATA_DIR = r"C:\Users\sam\cena-l3data"
DATA_DIR_ENV = "CENA_L3_DATA_DIR"

_LAYOUT: dict[str, str] = {
    "central": "Prime derived data-point DB for broad C.E.N.A. reasoning.",
    "analytics": "Sanitized analytics SQLite copied from snapshots.",
    "toast/emp": "Local-only raw Toast employee/profile data mart mirror.",
    "toast/labor": "Local-only raw Toast labor/time-entry mirror.",
    "orders/ezcater": "Local-only raw ezCater/order data-cube mirror.",
    "drivers": "Local-only raw driver data-cube mirror.",
    "app": "Local-only raw Cenas app operational mirror.",
    "memory": "Local-only C.E.N.A. exemplar and investigation memory mirror.",
}

_COPY_MAP: tuple[tuple[str, str, str], ...] = (
    ("analytics", "cena_analytics.db", "analytics/cena_analytics.db"),
    ("toast", "toast.sqlite", "toast/labor/toast.sqlite"),
    ("toastdm", "toastdm.sqlite", "toast/emp/toastdm.sqlite"),
    ("ordersdc", "ordersdc.sqlite", "orders/ezcater/ordersdc.sqlite"),
    ("driverdc", "driverdc.sqlite", "drivers/driverdc.sqlite"),
    ("appdb", "appdb.sqlite", "app/appdb.sqlite"),
)

_SALES_METRICS = (
    "net_sales", "gross_sales", "order_count", "check_count", "avg_check",
    "covers", "daypart_breakfast_net", "daypart_lunch_net",
    "daypart_dinner_net",
)
_LABOR_METRICS = (
    "total_hours", "reg_hours", "ot_hours", "labor_cost", "net_sales",
    "labor_pct", "splh", "employee_count",
)
_WEEKLY_METRICS = (
    "net_sales", "order_count", "labor_cost", "labor_pct", "splh",
    "total_hours", "wow_net_sales_delta", "wow_net_sales_pct",
    "wow_labor_pct_delta",
)


def _data_dir(override: str | None = None) -> Path:
    return Path(override or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _replace_with_retry(src: Path, dst: Path, *, attempts: int = 3) -> None:
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            last_error = e
            if attempt == attempts - 1:
                break
            time.sleep(0.2 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _remove_if_present(path: Path) -> bool:
    if not path.exists():
        return False
    path.unlink()
    return True


def _atomic_copy(src: Path, dst: Path, *, enabled: bool = True) -> dict[str, Any]:
    status: dict[str, Any] = {"copied": False, "removed_stale": False, "error": None}
    if not enabled or not src.exists():
        try:
            status["removed_stale"] = _remove_if_present(dst)
        except OSError as e:
            status["error"] = f"{type(e).__name__}: {e}"
        return status
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.tmp-{os.getpid()}")
    try:
        if tmp.exists():
            tmp.unlink()
        shutil.copy2(src, tmp)
        _replace_with_retry(tmp, dst)
        status["copied"] = True
    except OSError as e:
        status["error"] = f"{type(e).__name__}: {e}"
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return status


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table','view')",
        (table,),
    ).fetchone()
    return row is not None


def _row_count(path: Path, table: str) -> int | None:
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            if not _table_exists(conn, table):
                return None
            return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _insert_point(
    conn: sqlite3.Connection,
    *,
    domain: str,
    entity: str,
    store_key: Any,
    business_date: Any,
    period: Any,
    metric: str,
    value: Any,
    value_text: Any = None,
    source_table: str,
    source_key: str,
    generated_at: str,
) -> None:
    if value is None and value_text is None:
        return
    value_num = None
    if value is not None:
        try:
            value_num = float(value)
        except (TypeError, ValueError):
            value_text = value
    conn.execute(
        """
        INSERT INTO data_points
          (domain, entity, store_key, business_date, period, metric, value_num,
           value_text, source_table, source_key, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain,
            entity,
            str(store_key or "") or None,
            str(business_date or "") or None,
            str(period or "") or None,
            metric,
            value_num,
            None if value_text is None else str(value_text),
            source_table,
            source_key,
            generated_at,
        ),
    )


def _build_points_db(analytics_path: Path, dest: Path, generated_at: str) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(str(tmp))
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=OFF;
            CREATE TABLE source_index (
              domain TEXT NOT NULL,
              logical_path TEXT NOT NULL,
              db_file TEXT NOT NULL,
              source_table TEXT NOT NULL,
              row_count INTEGER,
              generated_at TEXT NOT NULL,
              PRIMARY KEY (domain, logical_path, source_table)
            );
            CREATE TABLE data_points (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              domain TEXT NOT NULL,
              entity TEXT NOT NULL,
              store_key TEXT,
              business_date TEXT,
              period TEXT,
              metric TEXT NOT NULL,
              value_num REAL,
              value_text TEXT,
              source_table TEXT NOT NULL,
              source_key TEXT NOT NULL,
              generated_at TEXT NOT NULL
            );
            CREATE INDEX ix_data_points_metric ON data_points(metric);
            CREATE INDEX ix_data_points_store_date ON data_points(store_key, business_date);
            CREATE INDEX ix_data_points_domain_entity ON data_points(domain, entity);
            """
        )
        tables_written = 0
        if analytics_path.exists():
            src = sqlite3.connect(f"file:{analytics_path.as_posix()}?mode=ro", uri=True)
            src.row_factory = sqlite3.Row
            try:
                if _table_exists(src, "daily_sales_summary"):
                    for row in src.execute("SELECT * FROM daily_sales_summary"):
                        key = f"{row['store_key']}:{row['business_date']}"
                        for metric in _SALES_METRICS:
                            _insert_point(
                                conn,
                                domain="sales",
                                entity="store_day",
                                store_key=row["store_key"],
                                business_date=row["business_date"],
                                period=None,
                                metric=metric,
                                value=row[metric],
                                source_table="daily_sales_summary",
                                source_key=key,
                                generated_at=generated_at,
                            )
                    tables_written += 1
                if _table_exists(src, "daily_labor_summary"):
                    for row in src.execute("SELECT * FROM daily_labor_summary"):
                        key = f"{row['store_key']}:{row['business_date']}"
                        for metric in _LABOR_METRICS:
                            _insert_point(
                                conn,
                                domain="labor",
                                entity="store_day",
                                store_key=row["store_key"],
                                business_date=row["business_date"],
                                period=None,
                                metric=metric,
                                value=row[metric],
                                source_table="daily_labor_summary",
                                source_key=key,
                                generated_at=generated_at,
                            )
                    tables_written += 1
                if _table_exists(src, "weekly_rollups"):
                    for row in src.execute("SELECT * FROM weekly_rollups"):
                        key = f"{row['store_key']}:{row['iso_week']}"
                        for metric in _WEEKLY_METRICS:
                            _insert_point(
                                conn,
                                domain="weekly",
                                entity="store_week",
                                store_key=row["store_key"],
                                business_date=row["week_start"],
                                period=row["iso_week"],
                                metric=metric,
                                value=row[metric],
                                source_table="weekly_rollups",
                                source_key=key,
                                generated_at=generated_at,
                            )
                    tables_written += 1
                if _table_exists(src, "item_sales_summary"):
                    for row in src.execute("SELECT * FROM item_sales_summary"):
                        key = f"{row['store_key']}:{row['business_date']}:{row['item_name']}"
                        for metric in ("qty", "net_amount"):
                            _insert_point(
                                conn,
                                domain="menu",
                                entity=str(row["item_name"] or "item"),
                                store_key=row["store_key"],
                                business_date=row["business_date"],
                                period=None,
                                metric=metric,
                                value=row[metric],
                                value_text=row["category"],
                                source_table="item_sales_summary",
                                source_key=key,
                                generated_at=generated_at,
                            )
                    tables_written += 1
                if _table_exists(src, "anomaly_flags"):
                    for row in src.execute("SELECT * FROM anomaly_flags"):
                        key = f"{row['store_key']}:{row['business_date']}:{row['metric']}"
                        _insert_point(
                            conn,
                            domain="anomaly",
                            entity=str(row["metric"] or "metric"),
                            store_key=row["store_key"],
                            business_date=row["business_date"],
                            period=None,
                            metric="z_score",
                            value=row["z_score"],
                            value_text=f"{row['direction']} value={row['value']}",
                            source_table="anomaly_flags",
                            source_key=key,
                            generated_at=generated_at,
                        )
                    tables_written += 1
            finally:
                src.close()
        point_count = int(conn.execute("SELECT COUNT(*) FROM data_points").fetchone()[0])
        conn.execute(
            """
            INSERT INTO source_index
              (domain, logical_path, db_file, source_table, row_count, generated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "central",
                "DB/central/cena_points.sqlite",
                str(dest),
                "data_points",
                point_count,
                generated_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    _replace_with_retry(tmp, dest)
    return {
        "path": str(dest),
        "exists": dest.exists(),
        "data_points": _row_count(dest, "data_points") or 0,
        "source_tables_loaded": tables_written,
    }


def _source_refresh_ok(source_statuses: Mapping[str, Mapping[str, Any]] | None, alias: str) -> bool:
    if source_statuses is None:
        return True
    status = source_statuses.get(alias)
    return bool(status and status.get("ok"))


def _load_latest_source_statuses(data_root: Path) -> dict[str, Mapping[str, Any]] | None:
    meta_path = data_root / "snapshots" / "snapshot_meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    statuses: dict[str, Mapping[str, Any]] = {}
    sources = meta.get("sources", {})
    if isinstance(sources, dict):
        for alias, status in sources.items():
            if isinstance(status, dict):
                statuses[str(alias)] = status
    analytics = meta.get("analytics")
    if isinstance(analytics, dict):
        statuses["analytics"] = analytics
    return statuses or None


def build_db_overlay(data_dir: str | None = None) -> dict[str, Any]:
    """Build ``%CENA_L3_DATA_DIR%\\DB`` from the current snapshots.

    The overlay is derived and safe to regenerate. It never replaces canonical
    snapshots or memory.
    """
    source_statuses = _load_latest_source_statuses(_data_dir(data_dir))
    data_root = _data_dir(data_dir)
    snapshot_root = data_root / "snapshots"
    db_root = data_root / "DB"
    generated_at = _utc_now_iso()
    started = time.perf_counter()
    source_refresh = {
        "provided": source_statuses is not None,
        "successful_aliases": sorted(
            alias
            for alias, status in (source_statuses or {}).items()
            if status.get("ok")
        ),
        "failed_aliases": sorted(
            alias
            for alias, status in (source_statuses or {}).items()
            if not status.get("ok")
        ),
    }

    for rel in _LAYOUT:
        (db_root / rel).mkdir(parents=True, exist_ok=True)

    files: list[dict[str, Any]] = []
    for alias, snapshot_name, rel_dest in _COPY_MAP:
        src = snapshot_root / snapshot_name
        dst = db_root / rel_dest
        copy_status = _atomic_copy(
            src,
            dst,
            enabled=_source_refresh_ok(source_statuses, alias),
        )
        files.append({
            "alias": alias,
            "source": str(src),
            "path": str(dst),
            "exists": dst.exists(),
            "copied": copy_status["copied"],
            "removed_stale": copy_status["removed_stale"],
            "error": copy_status["error"],
            "bytes": dst.stat().st_size if dst.exists() else 0,
        })

    memory_src = data_root / "memory" / "cena_memory.db"
    memory_dst = db_root / "memory" / "cena_memory.db"
    memory_status = _atomic_copy(memory_src, memory_dst, enabled=memory_src.exists())
    files.append({
        "alias": "memory",
        "source": str(memory_src),
        "path": str(memory_dst),
        "exists": memory_dst.exists(),
        "copied": memory_status["copied"],
        "removed_stale": memory_status["removed_stale"],
        "error": memory_status["error"],
        "bytes": memory_dst.stat().st_size if memory_dst.exists() else 0,
    })

    analytics_ok = _source_refresh_ok(source_statuses, "analytics")
    central_path = db_root / "central" / "cena_points.sqlite"
    if analytics_ok:
        central = _build_points_db(
            snapshot_root / "cena_analytics.db",
            central_path,
            generated_at,
        )
    else:
        removed_stale_central = _remove_if_present(central_path)
        central = {
            "path": str(central_path),
            "exists": False,
            "data_points": 0,
            "source_tables_loaded": 0,
            "removed_stale": removed_stale_central,
            "error": "analytics refresh did not succeed",
        }

    catalog = {
        "version": 1,
        "generated_at": generated_at,
        "data_root": str(data_root),
        "db_root": str(db_root),
        "canonical": {
            "snapshots": str(snapshot_root),
            "memory": str(data_root / "memory"),
            "analytics_db": str(snapshot_root / "cena_analytics.db"),
        },
        "data_policy": {
            "central_sanitized": True,
            "raw_operational_mirrors_local_only": True,
            "raw_mirror_aliases": [
                "appdb", "toast", "toastdm", "ordersdc", "driverdc", "memory",
            ],
            "note": (
                "DB/central is the derived reasoning DB. Other DB folders mirror "
                "local operational snapshots for C.E.N.A. only and must not be "
                "published or used as public sanitized exports."
            ),
        },
        "folders": {
            key: {"path": str(db_root / key), "purpose": purpose}
            for key, purpose in _LAYOUT.items()
        },
        "central": central,
        "files": files,
        "source_refresh": source_refresh,
        "automation": {
            "refresh_script": "scripts/refresh_cena_snapshots.py",
            "scheduled_task": r"\Cena\CENA-L3-Snapshots",
            "refreshes_overlay": True,
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    catalog_path = db_root / "catalog.json"
    tmp_catalog = catalog_path.with_name(f"catalog.json.tmp-{os.getpid()}")
    tmp_catalog.write_text(json.dumps(catalog, indent=2, sort_keys=True), encoding="utf-8")
    _replace_with_retry(tmp_catalog, catalog_path)
    catalog["catalog_path"] = str(catalog_path)
    return {"ok": True, **catalog}
