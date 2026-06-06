"""Sanitized Toast webhook and employee-profile payloads for the assistant.

This module is intentionally read-only. It summarizes the CK Toast webhook
SQLite store and the per-employee Toast profile SQLite files without exposing
raw webhook JSON, Toast secrets, or raw GUID-heavy payloads to the model.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


DEFAULT_DB_PATH = r"C:\Users\sam\cena-ai-assistant\toast_webhook\toast_webhook.sqlite"
DEFAULT_EMPLOYEE_PROFILE_DB_DIR = r"C:\Users\sam\cena-ai-assistant\employee_profiles\toast"
MAX_ROWS = 12
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

try:
    _CT = ZoneInfo("America/Chicago") if ZoneInfo else timezone(timedelta(hours=-5))
except Exception:  # pragma: no cover
    _CT = timezone(timedelta(hours=-5))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _today_business_date() -> str:
    return _now_utc().astimezone(_CT).strftime("%Y%m%d")


def _business_date_from_question(question: str | None) -> str | None:
    text = str(question or "").casefold()
    today = _now_utc().astimezone(_CT).date()
    if re.search(r"\b(last night|yesterday|previous night)\b", text):
        return (today - timedelta(days=1)).strftime("%Y%m%d")
    if re.search(r"\b(today|tonight|right now|live|current)\b", text):
        return today.strftime("%Y%m%d")
    return None


def _connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    db_path = Path(path or os.getenv("TOAST_WEBHOOK_DB") or DEFAULT_DB_PATH)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _connect_profile(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row else 0


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _limit(value: int | None) -> int:
    try:
        parsed = int(value or MAX_ROWS)
    except (TypeError, ValueError):
        parsed = MAX_ROWS
    return max(1, min(parsed, MAX_ROWS))


def _normalize_store(store_key: str | None) -> str | None:
    value = str(store_key or "").strip().casefold()
    aliases = {
        "dos": "tomball",
        "dos mas": "tomball",
        "tomball": "tomball",
        "uno": "copperfield",
        "uno mas": "copperfield",
        "copperfield": "copperfield",
        "both": None,
        "all": None,
        "all_locations": None,
    }
    return aliases.get(value, value or None)


def requested_store(question: str | None) -> str | None:
    text = str(question or "").casefold()
    aliases = {
        "tomball": "tomball",
        "dos mas": "tomball",
        "dos": "tomball",
        "copperfield": "copperfield",
        "uno mas": "copperfield",
        "uno": "copperfield",
    }
    for alias, store in aliases.items():
        escaped = re.escape(alias).replace(r"\ ", r"\s+")
        if re.search(rf"\b{escaped}\b", text):
            return store
    return None


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        decoded = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _safe_label(value: Any, *, max_len: int = 180) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    if _UUID_RE.search(text):
        return None
    if (
        text.startswith("{")
        or "entitytype" in lowered
        or "externalid" in lowered
        or "'guid'" in lowered
        or '"guid"' in lowered
    ):
        return None
    return text[:max_len]


def _safe_table_label(value: Any) -> str | None:
    label = _safe_label(value, max_len=80)
    if not label:
        return None
    return label


def _profile_name(profile: dict[str, Any]) -> str | None:
    for key in ("full_name", "name", "display_name"):
        value = _safe_label(profile.get(key), max_len=120)
        if value:
            return value
    first = _safe_label(profile.get("first_name") or profile.get("firstName"), max_len=60) or ""
    last = _safe_label(profile.get("last_name") or profile.get("lastName"), max_len=60) or ""
    full = " ".join(part for part in (first, last) if part).strip()
    return full or None


def _profile_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    profile = _json_obj(row["profile_json"])
    employee_id = int(row["cena_employee_id"])
    return {
        "cena_employee_id": employee_id,
        "name": _profile_name(profile) or f"Employee {employee_id}",
        "active": profile.get("active"),
        "primary_store_key": profile.get("primary_store_key"),
        "positions": [
            label
            for label in (
                _safe_label(position, max_len=80)
                for position in (profile.get("positions") if isinstance(profile.get("positions"), list) else [])
            )
            if label
        ],
        "source": row["source"],
        "generated_at": row["generated_at"],
    }


def _profile_by_id(conn: sqlite3.Connection, employee_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT cena_employee_id, profile_json, source, generated_at
          FROM employee_profile_current
         WHERE cena_employee_id = ?
        """,
        (employee_id,),
    ).fetchone()
    return _profile_row(row)


def _all_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        profile
        for profile in (
            _profile_row(row)
            for row in _rows(
                conn,
                """
                SELECT cena_employee_id, profile_json, source, generated_at
                  FROM employee_profile_current
                 ORDER BY cena_employee_id
                """,
            )
        )
        if profile is not None
    ]


def employee_id_from_question(question: str | None, conn: sqlite3.Connection) -> int | None:
    text = str(question or "")
    for pattern in (
        r"\bcena_employee_(\d+)\b",
        r"\bemployee(?:\s+id)?\s*#?\s*(\d+)\b",
        r"\bemp(?:loyee)?\s*#\s*(\d+)\b",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))

    norm = re.sub(r"\s+", " ", text.casefold())
    candidates: list[tuple[int, str]] = []
    for profile in _all_profiles(conn):
        name = str(profile.get("name") or "").casefold()
        if not name:
            continue
        parts = [part for part in re.split(r"\s+", name) if len(part) > 2]
        if name in norm:
            return int(profile["cena_employee_id"])
        if parts and all(re.search(rf"\b{re.escape(part)}\b", norm) for part in parts[:2]):
            candidates.append((int(profile["cena_employee_id"]), name))
        elif len(parts) == 1 and re.search(rf"\b{re.escape(parts[0])}\b", norm):
            candidates.append((int(profile["cena_employee_id"]), name))
    if len(candidates) == 1:
        return candidates[0][0]
    return None


def _summary_json(raw: Any) -> dict[str, Any]:
    source = _json_obj(raw)
    allowed = {
        "source",
        "table",
        "display_number",
        "payment_status",
        "total_amount",
        "name",
        "quantity",
        "price",
        "voided",
        "payment_type",
        "amount",
        "tip_amount",
    }
    out: dict[str, Any] = {}
    for key in allowed:
        value = source.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, str):
            safe_value = _safe_table_label(value) if key == "table" else _safe_label(value)
            if safe_value:
                out[key] = safe_value
        elif isinstance(value, (int, float, bool)):
            out[key] = value
    return out


def _event_filter(store_key: str | None, business_date: str | None = None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if store_key:
        clauses.append("store_key = ?")
        params.append(store_key)
    if business_date:
        clauses.append("business_date = ?")
        params.append(business_date)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _profile_dir(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or os.getenv("TOAST_EMPLOYEE_PROFILE_DB_DIR") or DEFAULT_EMPLOYEE_PROFILE_DB_DIR)


def toast_webhook_activity_payload(
    question: str | None = None,
    *,
    store_key: str | None = None,
    business_date: str | None = None,
    limit: int | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return central Toast webhook activity without raw event JSON."""
    requested = requested_store(question)
    store = _normalize_store(store_key or requested)
    bd = business_date or _business_date_from_question(question) or _today_business_date()
    row_limit = _limit(limit)

    db_file = Path(db_path or os.getenv("TOAST_WEBHOOK_DB") or DEFAULT_DB_PATH)
    if not db_file.exists():
        return {
            "ok": False,
            "generated_at": _now_iso(),
            "data_class": "toast_webhook_activity_sanitized",
            "error": "toast_webhook_db_missing",
        }

    with _connect(db_file) as conn:
        counts = {
            "events": _scalar(conn, "SELECT COUNT(*) FROM toast_webhook_event"),
            "orders": _scalar(conn, "SELECT COUNT(*) FROM toast_order_current"),
            "checks": _scalar(conn, "SELECT COUNT(*) FROM toast_check_current"),
            "selections": _scalar(conn, "SELECT COUNT(*) FROM toast_selection_current"),
            "payments": _scalar(conn, "SELECT COUNT(*) FROM toast_payment_current"),
            "employee_facts": _scalar(conn, "SELECT COUNT(*) FROM employee_toast_fact"),
            "employee_profiles": _scalar(conn, "SELECT COUNT(*) FROM employee_profile_current"),
            "identity_links": _scalar(conn, "SELECT COUNT(*) FROM employee_toast_identity_map"),
            "unmatched_employee_refs": _scalar(conn, "SELECT COUNT(*) FROM employee_toast_unmatched"),
        }

        event_categories = [
            {"event_category": row["event_category"] or "unknown", "count": int(row["n"] or 0)}
            for row in _rows(
                conn,
                """
                SELECT event_category, COUNT(*) AS n
                  FROM toast_webhook_event
                 GROUP BY event_category
                 ORDER BY n DESC, event_category
                 LIMIT 12
                """,
            )
        ]

        recent_threshold = (_now_utc() - timedelta(minutes=60)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        recent_last_hour = _scalar(
            conn,
            "SELECT COUNT(*) FROM toast_webhook_event WHERE received_at >= ?",
            (recent_threshold,),
        )

        today_where, today_params = _event_filter(store, bd)
        store_rows = _rows(
            conn,
            """
            SELECT COALESCE(store_key, 'unknown') AS store_key,
                   COUNT(*) AS events,
                   MAX(received_at) AS latest_received_at
              FROM toast_webhook_event
             WHERE date(substr(received_at, 1, 10)) >= date('now', '-2 day')
             GROUP BY COALESCE(store_key, 'unknown')
             ORDER BY store_key
            """,
        )
        store_activity = [
            {
                "store_key": row["store_key"],
                "recent_events": int(row["events"] or 0),
                "latest_received_at": row["latest_received_at"],
            }
            for row in store_rows
        ]

        fact_types = [
            {"fact_type": row["fact_type"], "count": int(row["n"] or 0)}
            for row in _rows(
                conn,
                f"""
                SELECT fact_type, COUNT(*) AS n
                  FROM employee_toast_fact
                  {today_where}
                 GROUP BY fact_type
                 ORDER BY n DESC, fact_type
                 LIMIT 12
                """,
                tuple(today_params),
            )
        ]

        latest_events = [
            {
                "event_category": row["event_category"] or "unknown",
                "event_type": row["event_type"] or "unknown",
                "store_key": row["store_key"],
                "toast_timestamp": row["toast_timestamp"],
                "received_at": row["received_at"],
                "processing_status": row["processing_status"],
            }
            for row in _rows(
                conn,
                """
                SELECT event_category, event_type, store_key, toast_timestamp,
                       received_at, processing_status
                  FROM toast_webhook_event
                 WHERE (? IS NULL OR store_key = ?)
                 ORDER BY received_at DESC
                 LIMIT ?
                """,
                (store, store, row_limit),
            )
        ]

        latest_orders = []
        order_rows = _rows(
            conn,
            """
            SELECT order_guid, store_key, business_date, table_name, payment_status,
                   approval_status, opened_date, modified_date, closed_date,
                   updated_at, server_toast_guid
              FROM toast_order_current
             WHERE (? IS NULL OR store_key = ?)
               AND (? IS NULL OR business_date = ?)
             ORDER BY COALESCE(modified_date, updated_at, opened_date) DESC
             LIMIT ?
            """,
            (store, store, bd, bd, row_limit),
        )
        for row in order_rows:
            server_employee_id = None
            server_name = None
            if row["server_toast_guid"] and row["store_key"]:
                identity = conn.execute(
                    """
                    SELECT cena_employee_id
                      FROM employee_toast_identity_map
                     WHERE store_key = ? AND toast_employee_guid = ?
                    """,
                    (row["store_key"], row["server_toast_guid"]),
                ).fetchone()
                if identity:
                    server_employee_id = int(identity["cena_employee_id"])
                    profile = _profile_by_id(conn, server_employee_id)
                    server_name = profile.get("name") if profile else None
            order_guid = row["order_guid"]
            latest_orders.append({
                "store_key": row["store_key"],
                "business_date": row["business_date"],
                "table_name": _safe_table_label(row["table_name"]),
                "payment_status": row["payment_status"],
                "approval_status": row["approval_status"],
                "opened_date": row["opened_date"],
                "modified_date": row["modified_date"],
                "closed_date": row["closed_date"],
                "server_cena_employee_id": server_employee_id,
                "server_name": server_name,
                "check_count": _scalar(conn, "SELECT COUNT(*) FROM toast_check_current WHERE order_guid = ?", (order_guid,)),
                "selection_count": _scalar(conn, "SELECT COUNT(*) FROM toast_selection_current WHERE order_guid = ?", (order_guid,)),
                "payment_count": _scalar(conn, "SELECT COUNT(*) FROM toast_payment_current WHERE order_guid = ?", (order_guid,)),
            })

    return {
        "ok": True,
        "generated_at": _now_iso(),
        "data_class": "toast_webhook_activity_sanitized",
        "scope": {"store_key": store, "business_date": bd},
        "counts": counts,
        "recent_last_hour_events": recent_last_hour,
        "event_categories": event_categories,
        "store_activity": store_activity,
        "fact_types_for_scope": fact_types,
        "latest_events": latest_events,
        "latest_orders": latest_orders,
        "raw_payloads_included": False,
    }


def _profile_db_path(employee_id: int, profile_dir: Path) -> Path:
    return profile_dir / f"cena_employee_{int(employee_id)}.sqlite"


def toast_employee_profile_payload(
    question: str | None = None,
    *,
    employee_id: int | None = None,
    limit: int | None = None,
    db_path: str | os.PathLike[str] | None = None,
    profile_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return per-employee Toast profile DB data, sanitized for assistant use."""
    row_limit = _limit(limit)
    db_file = Path(db_path or os.getenv("TOAST_WEBHOOK_DB") or DEFAULT_DB_PATH)
    out_dir = _profile_dir(profile_dir)
    if not db_file.exists():
        return {
            "ok": False,
            "generated_at": _now_iso(),
            "data_class": "toast_employee_profiles_sanitized",
            "error": "toast_webhook_db_missing",
        }

    with _connect(db_file) as conn:
        target_id = employee_id or employee_id_from_question(question, conn)
        profile_count = _scalar(conn, "SELECT COUNT(*) FROM employee_profile_current")
        identity_count = _scalar(conn, "SELECT COUNT(*) FROM employee_toast_identity_map")
        fact_count = _scalar(conn, "SELECT COUNT(*) FROM employee_toast_fact")
        unmatched_count = _scalar(conn, "SELECT COUNT(*) FROM employee_toast_unmatched")
        files = sorted(out_dir.glob("cena_employee_*.sqlite")) if out_dir.exists() else []

        if target_id is None:
            top_rows = _rows(
                conn,
                """
                SELECT f.cena_employee_id, COUNT(*) AS fact_count,
                       MAX(COALESCE(f.occurred_at, f.created_at)) AS latest_activity_at,
                       p.profile_json, p.source, p.generated_at
                  FROM employee_toast_fact f
             LEFT JOIN employee_profile_current p
                    ON p.cena_employee_id = f.cena_employee_id
                 WHERE f.cena_employee_id IS NOT NULL
                 GROUP BY f.cena_employee_id
                 ORDER BY fact_count DESC, latest_activity_at DESC
                 LIMIT ?
                """,
                (row_limit,),
            )
            top_employees = []
            for row in top_rows:
                profile = _profile_row(row)
                top_employees.append({
                    "cena_employee_id": int(row["cena_employee_id"]),
                    "name": (profile or {}).get("name") or f"Employee {row['cena_employee_id']}",
                    "fact_count": int(row["fact_count"] or 0),
                    "latest_activity_at": row["latest_activity_at"],
                })
            return {
                "ok": True,
                "generated_at": _now_iso(),
                "data_class": "toast_employee_profiles_sanitized",
                "scope": "overview",
                "profile_db_count": len(files),
                "central_counts": {
                    "employee_profiles": profile_count,
                    "identity_links": identity_count,
                    "employee_facts": fact_count,
                    "unmatched_employee_refs": unmatched_count,
                },
                "top_employees_by_toast_facts": top_employees,
                "raw_payloads_included": False,
            }

        profile = _profile_by_id(conn, int(target_id))
        central_fact_types = [
            {"fact_type": row["fact_type"], "count": int(row["n"] or 0)}
            for row in _rows(
                conn,
                """
                SELECT fact_type, COUNT(*) AS n
                  FROM employee_toast_fact
                 WHERE cena_employee_id = ?
                 GROUP BY fact_type
                 ORDER BY n DESC, fact_type
                """,
                (int(target_id),),
            )
        ]

    profile_db = _profile_db_path(int(target_id), out_dir)
    personal: dict[str, Any] = {
        "exists": profile_db.exists(),
        "file_name": profile_db.name,
    }
    if profile_db.exists():
        with _connect_profile(profile_db) as conn:
            metadata = {
                row["key"]: row["value"]
                for row in _rows(conn, "SELECT key, value FROM metadata")
                if row["key"] in {"schema_version", "generated_at", "source", "raw_payloads_included"}
            }
            personal["metadata"] = metadata
            personal["toast_fact_count"] = _scalar(conn, "SELECT COUNT(*) FROM toast_fact")
            personal["identity_count"] = _scalar(conn, "SELECT COUNT(*) FROM toast_identity_map")
            personal["related_orders"] = _scalar(conn, "SELECT COUNT(*) FROM related_order_current")
            personal["related_checks"] = _scalar(conn, "SELECT COUNT(*) FROM related_check_current")
            personal["related_selections"] = _scalar(conn, "SELECT COUNT(*) FROM related_selection_current")
            personal["related_payments"] = _scalar(conn, "SELECT COUNT(*) FROM related_payment_current")
            personal["fact_type_counts"] = [
                {
                    "fact_type": row["fact_type"],
                    "count": int(row["fact_count"] or 0),
                    "first_occurred_at": row["first_occurred_at"],
                    "last_occurred_at": row["last_occurred_at"],
                }
                for row in _rows(
                    conn,
                    """
                    SELECT fact_type, fact_count, first_occurred_at, last_occurred_at
                      FROM toast_fact_type_count
                     ORDER BY fact_count DESC, fact_type
                    """,
                )
            ]
            personal["latest_facts"] = [
                {
                    "fact_type": row["fact_type"],
                    "store_key": row["store_key"],
                    "entity_type": row["entity_type"],
                    "business_date": row["business_date"],
                    "occurred_at": row["occurred_at"],
                    "summary": _summary_json(row["summary_json"]),
                }
                for row in _rows(
                    conn,
                    """
                    SELECT fact_type, store_key, entity_type, business_date,
                           occurred_at, summary_json
                      FROM toast_fact
                     ORDER BY COALESCE(occurred_at, created_at) DESC, id DESC
                     LIMIT ?
                    """,
                    (row_limit,),
                )
            ]

    return {
        "ok": True,
        "generated_at": _now_iso(),
        "data_class": "toast_employee_profiles_sanitized",
        "scope": "employee",
        "employee": profile or {
            "cena_employee_id": int(target_id),
            "name": f"Employee {int(target_id)}",
        },
        "central_fact_type_counts": central_fact_types,
        "personal_db": personal,
        "raw_payloads_included": False,
    }
