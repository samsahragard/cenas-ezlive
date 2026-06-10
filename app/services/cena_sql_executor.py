"""C.E.N.A. Level 3 - read-only SQL execution engine + snapshot mechanism.

Contract: docs/cena_level3_contracts.md sections 1-3 (FROZEN).

Responsibilities:
- refresh_snapshots(): copy each configured source DB into
  %CENA_L3_DATA_DIR%\\snapshots\\{alias}.sqlite via the sqlite3 backup API
  (read-only source open, temp file + atomic os.replace), record per-source
  status in snapshot_meta.json, then attempt the analytics build
  (stub-tolerant while Subagent C builds cena_sql_analytics in parallel).
- snapshot_status(): per-alias path / exists / age, analytics db presence.
- run_readonly_sql(sql): defense-in-depth read-only execution with caps:
  own single-statement SELECT/WITH check (sqlglot; intentionally duplicated
  from cena_sql_validator - do NOT import it), MAIN = cena_analytics.db
  opened mode=ro&immutable=1 (or :memory: fallback), raw snapshots ATTACHed
  read-only, PRAGMA query_only=ON, 5s wall timeout via progress handler,
  hard row cap 1000 (LIMIT clamped/injected AND enforced at fetch), 2 MB
  total result size cap.
- run_readonly_pg(sql): optional Postgres path behind CENA_L3_PG_URL.

Thread safety: a fresh connection per call, no shared mutable state.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (contract section 1)
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR = r"C:\Users\sam\cena-l3data"
DATA_DIR_ENV = "CENA_L3_DATA_DIR"
ANALYTICS_DB_NAME = "cena_analytics.db"

# alias -> (env override, default source path)
SOURCES: dict[str, tuple[str, str]] = {
    "appdb": ("CENA_L3_SRC_APPDB", r"C:\Users\sam\cenas-ezlive\dev_local.db"),
    "toast": ("CENA_L3_SRC_TOAST", r"C:\Users\sam\cena-perfdb\perf.sqlite"),
    "toastdm": ("CENA_L3_SRC_TOASTDM", r"C:\Users\sam\cena-perfdb\datamart\datamart.sqlite"),
    "ordersdc": ("CENA_L3_SRC_ORDERSDC", r"C:\Users\sam\cena-driverdc\_live\ordersdc.sqlite"),
    "driverdc": ("CENA_L3_SRC_DRIVERDC", r"C:\Users\sam\cena-driverdc\_live\driverdc.sqlite"),
}

# Caps (contract section 2)
DEFAULT_TIMEOUT_S = 5.0
ROW_CAP = 1000
SIZE_CAP_BYTES = 2 * 1024 * 1024  # 2 MB, sum of len(str(row))
PROGRESS_HANDLER_N = 1000  # check elapsed every ~1000 VM steps

# Module the snapshot refresh hands off to (Subagent C, stub-tolerant).
_ANALYTICS_MODULE = "app.services.cena_sql_analytics"


class CenaSqlError(Exception):
    """Execution-engine error with a clean, actionable message in .reason."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Paths / URI helpers
# ---------------------------------------------------------------------------

def _data_dir(override: str | None = None) -> Path:
    return Path(override or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR)


def _snapshot_dir(override: str | None = None) -> Path:
    return _data_dir(override) / "snapshots"


def _source_path(alias: str) -> str:
    env_name, default = SOURCES[alias]
    return os.environ.get(env_name) or default


def _sqlite_uri(path: str | Path, params: str) -> str:
    """Build a file: URI sqlite accepts on Windows (forward slashes, %-escaped)."""
    posix = Path(path).resolve().as_posix()
    return "file:" + urllib.parse.quote(posix, safe="/:") + ("?" + params if params else "")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Snapshot mechanism (contract section 2)
# ---------------------------------------------------------------------------

def _copy_one_source(alias: str, source: str, snap_dir: Path) -> dict:
    """Backup one source DB into snap_dir/{alias}.sqlite. Never raises."""
    status: dict = {"source": source, "ok": False}
    dest = snap_dir / f"{alias}.sqlite"
    tmp = snap_dir / f"{alias}.sqlite.tmp-{os.getpid()}"
    src_conn = dst_conn = None
    try:
        if not os.path.exists(source):
            raise FileNotFoundError(f"source file not found: {source}")
        src_conn = sqlite3.connect(_sqlite_uri(source, "mode=ro"), uri=True)
        if tmp.exists():
            tmp.unlink()
        dst_conn = sqlite3.connect(str(tmp))
        src_conn.backup(dst_conn)
        # cheap row hint: total rows across user tables of the (small) copy
        try:
            tables = [
                r[0] for r in dst_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
            status["row_hint"] = sum(
                dst_conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for t in tables
            )
            status["table_count"] = len(tables)
        except sqlite3.Error:
            status["row_hint"] = None
        dst_conn.close()
        dst_conn = None
        src_conn.close()
        src_conn = None
        os.replace(tmp, dest)
        status["ok"] = True
        status["copied_at"] = _utc_now_iso()
        status["snapshot_path"] = str(dest)
    except Exception as e:  # noqa: BLE001 - missing/locked sources are recorded, never fatal
        status["error"] = f"{type(e).__name__}: {e}"
        logger.warning("snapshot copy failed for %s: %s", alias, status["error"])
    finally:
        for c in (src_conn, dst_conn):
            if c is not None:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return status


def _run_analytics_build(snap_dir: Path) -> dict:
    """Attempt the Subagent-C analytics build. STUB-TOLERANT: never raises."""
    try:
        mod = importlib.import_module(_ANALYTICS_MODULE)
        path = mod.build_analytics_db(snapshot_dir=str(snap_dir))
        return {"ok": True, "path": str(path)}
    except Exception as e:  # noqa: BLE001 - module missing or raising while C builds in parallel
        detail = f"unavailable: {type(e).__name__}: {e}"
        logger.info("analytics build skipped: %s", detail)
        return {"ok": False, "error": detail}


def refresh_snapshots(data_dir: str | None = None) -> dict:
    """Copy every configured source DB into the snapshots dir; build analytics.

    Returns per-source status. Missing/locked sources are recorded in the
    returned dict and in snapshot_meta.json - never fatal.
    `data_dir` (beyond-contract keyword) overrides %CENA_L3_DATA_DIR%.
    """
    snap_dir = _snapshot_dir(data_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "data_dir": str(_data_dir(data_dir)),
        "snapshot_dir": str(snap_dir),
        "refreshed_at": _utc_now_iso(),
        "sources": {},
    }
    for alias in SOURCES:
        result["sources"][alias] = _copy_one_source(alias, _source_path(alias), snap_dir)

    result["analytics"] = _run_analytics_build(snap_dir)

    meta_path = snap_dir / "snapshot_meta.json"
    tmp_meta = snap_dir / f"snapshot_meta.json.tmp-{os.getpid()}"
    try:
        tmp_meta.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_meta, meta_path)
        result["meta_path"] = str(meta_path)
    except OSError as e:
        result["meta_error"] = f"{type(e).__name__}: {e}"
    return result


def snapshot_status(data_dir: str | None = None) -> dict:
    """Per-alias snapshot path / exists / age_seconds, analytics presence, missing list."""
    snap_dir = _snapshot_dir(data_dir)
    now = time.time()

    def _entry(path: Path) -> dict:
        exists = path.exists()
        return {
            "path": str(path),
            "exists": exists,
            "age_seconds": round(now - path.stat().st_mtime, 1) if exists else None,
        }

    snapshots = {alias: _entry(snap_dir / f"{alias}.sqlite") for alias in SOURCES}
    analytics = _entry(snap_dir / ANALYTICS_DB_NAME)
    return {
        "snapshot_dir": str(snap_dir),
        "snapshots": snapshots,
        "analytics": analytics,
        "missing": [a for a, s in snapshots.items() if not s["exists"]],
    }


# ---------------------------------------------------------------------------
# SQL gate (own check - intentional duplication of the validator's core;
# do NOT import cena_sql_validator here: defense in depth)
# ---------------------------------------------------------------------------

_FORBIDDEN_NODES = tuple(
    t for t in (
        getattr(exp, name, None)
        for name in (
            "Insert", "Update", "Delete", "Create", "Drop", "Alter", "Merge",
            "Attach", "Detach", "Pragma", "Command", "Transaction", "Commit",
            "Rollback", "TruncateTable", "Grant",
        )
    )
    if t is not None
)


def _check_select(sql: str) -> exp.Expression:
    """Own single-statement SELECT/WITH gate. Returns the parsed root or raises."""
    if not isinstance(sql, str) or not sql.strip():
        raise CenaSqlError("empty SQL - provide a single SELECT statement")
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except Exception as e:  # sqlglot ParseError and friends
        raise CenaSqlError(f"SQL parse error: {e}") from e
    if len(statements) != 1:
        raise CenaSqlError(
            f"exactly one statement required, got {len(statements)} - "
            "remove extra statements/semicolons"
        )
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.SetOperation)):
        raise CenaSqlError(
            f"only a single SELECT/WITH statement is allowed, got {type(stmt).__name__}"
        )
    for node in stmt.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise CenaSqlError(
                f"forbidden operation {type(node).__name__} inside query - "
                "only read-only SELECT is allowed"
            )
    return stmt


def _clamp_limit(stmt: exp.Expression, row_cap: int) -> str:
    """Inject/clamp LIMIT to row_cap+1 (the +1 lets fetch detect truncation)."""
    sentinel = row_cap + 1
    existing = stmt.args.get("limit")
    if existing is not None:
        lit = existing.args.get("expression")
        try:
            current = int(str(lit.this)) if isinstance(lit, exp.Literal) else None
        except (TypeError, ValueError):
            current = None
        if current is not None and current <= row_cap:
            return stmt.sql(dialect="sqlite")  # user limit within cap - keep as-is
    return stmt.limit(sentinel).sql(dialect="sqlite")


# ---------------------------------------------------------------------------
# Connection layout (contract section 2)
# ---------------------------------------------------------------------------

def _open_connection(data_dir: str | None = None) -> sqlite3.Connection:
    """Open the read-only query connection (test seam - writes must fail HERE).

    MAIN = snapshots/cena_analytics.db (mode=ro&immutable=1); if missing but
    raw snapshots exist, MAIN = :memory: so qualified queries still work.
    Every existing raw snapshot is ATTACHed read-only, then query_only=ON.
    Raises CenaSqlError when no snapshots exist at all.
    """
    snap_dir = _snapshot_dir(data_dir)
    analytics_path = snap_dir / ANALYTICS_DB_NAME
    raw = {a: snap_dir / f"{a}.sqlite" for a in SOURCES}
    existing_raw = {a: p for a, p in raw.items() if p.exists()}

    if analytics_path.exists():
        conn = sqlite3.connect(
            _sqlite_uri(analytics_path, "mode=ro&immutable=1"), uri=True
        )
    elif existing_raw:
        # uri=True keeps SQLITE_OPEN_URI set so the ATTACH file: URIs below work
        conn = sqlite3.connect("file::memory:?cache=private", uri=True)
    else:
        raise CenaSqlError("no snapshots available - run refresh_snapshots()")

    try:
        for alias, path in existing_raw.items():
            uri = _sqlite_uri(path, "mode=ro&immutable=1")
            # alias names are fixed module constants - trusted setup code only
            conn.execute(f"ATTACH DATABASE '{uri}' AS {alias}")
        conn.execute("PRAGMA query_only=ON")
    except Exception:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# run_readonly_sql (contract section 3)
# ---------------------------------------------------------------------------

def run_readonly_sql(
    sql: str,
    *,
    _timeout_s: float = DEFAULT_TIMEOUT_S,
    _data_dir: str | None = None,
    _row_cap: int = ROW_CAP,
    _size_cap: int = SIZE_CAP_BYTES,
) -> dict:
    """Execute one read-only SELECT against the snapshot connection layout.

    Returns {"rows": list[tuple], "columns": list[str], "row_count": int,
             "truncated": bool, "elapsed_ms": float}.
    Underscore keywords are beyond-contract testability overrides.
    """
    stmt = _check_select(sql)  # raises CenaSqlError before any connection is opened
    sql_to_run = _clamp_limit(stmt, _row_cap)

    conn = _open_connection(_data_dir)
    t0 = time.perf_counter()

    def _progress() -> int:
        return 1 if (time.perf_counter() - t0) > _timeout_s else 0

    conn.set_progress_handler(_progress, PROGRESS_HANDLER_N)
    try:
        try:
            cur = conn.execute(sql_to_run)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows: list[tuple] = []
            truncated = False
            total_size = 0
            while True:
                batch = cur.fetchmany(256)
                if not batch:
                    break
                for row in batch:
                    if len(rows) >= _row_cap:
                        truncated = True  # the LIMIT row_cap+1 sentinel row arrived
                        break
                    rows.append(row)
                    total_size += len(str(row))
                    if total_size > _size_cap:
                        truncated = True
                        break
                if truncated:
                    break
        except sqlite3.OperationalError as e:
            if "interrupt" in str(e).lower():
                raise CenaSqlError(f"query timeout after {_timeout_s:.1f}s") from e
            raise CenaSqlError(f"sqlite error: {e}") from e
        except sqlite3.Error as e:
            raise CenaSqlError(f"sqlite error: {e}") from e
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "rows": rows,
            "columns": columns,
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Optional Postgres path (contract section 3 - wired but optional)
# ---------------------------------------------------------------------------

def run_readonly_pg(sql: str) -> dict:
    """Read-only Postgres twin of run_readonly_sql, behind CENA_L3_PG_URL."""
    url = os.environ.get("CENA_L3_PG_URL")
    if not url:
        raise CenaSqlError("CENA_L3_PG_URL not set - postgres path disabled")
    _check_select(sql)  # same single-statement SELECT gate
    try:
        import psycopg  # lazy: optional dependency
    except ImportError as e:
        raise CenaSqlError("psycopg not installed - postgres path unavailable") from e
    t0 = time.perf_counter()
    options = "-c default_transaction_read_only=on -c statement_timeout=5000"
    try:
        with psycopg.connect(url, options=options) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchmany(ROW_CAP + 1)
            conn.rollback()
    except psycopg.Error as e:
        raise CenaSqlError(f"postgres error: {e}") from e
    truncated = len(rows) > ROW_CAP
    rows = [tuple(r) for r in rows[:ROW_CAP]]
    return {
        "rows": rows,
        "columns": columns,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
    }
