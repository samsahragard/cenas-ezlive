from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services.toast_client import restaurant_guids


DEFAULT_DB_PATH = r"C:\Users\sam\cena-ai-assistant\toast_webhook\toast_webhook.sqlite"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json_object(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_json_value(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _ref_guid(value: Any) -> str | None:
    if isinstance(value, dict):
        guid = str(value.get("guid") or "").strip()
        return guid or None
    return None


def _guid_from_any(value: Any) -> str | None:
    if isinstance(value, dict):
        return _ref_guid(value)
    guid = str(value or "").strip()
    return guid or None


def _text(value: Any, limit: int | None = None) -> str | None:
    if value is None:
        return None
    out = str(value).strip()
    if not out:
        return None
    return out[:limit] if limit else out


def _amount(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _business_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return text
    return text or None


def _order_from_payload(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    details = payload.get("details")
    if isinstance(details, dict):
        restaurant_guid = _text(details.get("restaurantGuid"))
        order = details.get("order")
        if isinstance(order, dict):
            return order, restaurant_guid
        if details.get("entityType") == "Order" or details.get("checks") is not None:
            return details, restaurant_guid
    if payload.get("entityType") == "Order" or payload.get("checks") is not None:
        return payload, None
    return None, None


def _table_guid(order: dict[str, Any]) -> str | None:
    return _ref_guid(order.get("table"))


def _table_name(order: dict[str, Any]) -> str | None:
    table = order.get("table")
    if isinstance(table, dict):
        for key in ("name", "tableNumber", "number"):
            value = _text(table.get(key), 80)
            if value:
                return value
    return _text(table, 80)


def _server_guid(order: dict[str, Any]) -> str | None:
    return _ref_guid(order.get("server")) or _ref_guid(order.get("openedBy"))


def _selection_map(order: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for check in order.get("checks") or []:
        if not isinstance(check, dict):
            continue
        check_guid = _text(check.get("guid"))
        for sel in check.get("selections") or []:
            if not isinstance(sel, dict):
                continue
            guid = _text(sel.get("guid"))
            if guid:
                out[guid] = {"check_guid": check_guid, "selection": sel}
    return out


def _payment_map(order: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for check in order.get("checks") or []:
        if not isinstance(check, dict):
            continue
        check_guid = _text(check.get("guid"))
        for payment in check.get("payments") or []:
            if not isinstance(payment, dict):
                continue
            guid = _text(payment.get("guid"))
            if guid:
                out[guid] = {"check_guid": check_guid, "payment": payment}
    return out


def _check_map(order: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for check in order.get("checks") or []:
        if isinstance(check, dict):
            guid = _text(check.get("guid"))
            if guid:
                out[guid] = check
    return out


class ToastWebhookStore:
    """SQLite-backed Toast webhook event store and employee projection builder."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = Path(db_path or os.getenv("TOAST_WEBHOOK_DB") or DEFAULT_DB_PATH)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            self._init_schema(conn)

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS toast_webhook_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_guid TEXT NOT NULL UNIQUE,
                event_category TEXT,
                event_type TEXT,
                restaurant_guid TEXT,
                store_key TEXT,
                toast_timestamp TEXT,
                received_at TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                signature_verified INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                attempt_number INTEGER,
                raw_json TEXT,
                redacted_headers_json TEXT,
                processing_status TEXT NOT NULL DEFAULT 'stored',
                processing_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_toast_event_type_time
                ON toast_webhook_event(event_type, received_at);
            CREATE INDEX IF NOT EXISTS ix_toast_event_store_time
                ON toast_webhook_event(store_key, received_at);

            CREATE TABLE IF NOT EXISTS toast_webhook_delivery_attempt (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_guid TEXT,
                received_at TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                attempt_number INTEGER,
                source TEXT NOT NULL,
                signature_verified INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_toast_attempt_event
                ON toast_webhook_delivery_attempt(event_guid, received_at);

            CREATE TABLE IF NOT EXISTS toast_order_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_guid TEXT NOT NULL UNIQUE,
                order_guid TEXT NOT NULL,
                restaurant_guid TEXT,
                store_key TEXT,
                business_date TEXT,
                opened_date TEXT,
                modified_date TEXT,
                closed_date TEXT,
                paid_date TEXT,
                server_toast_guid TEXT,
                table_guid TEXT,
                table_name TEXT,
                order_json TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_toast_order_snapshot_order
                ON toast_order_snapshot(order_guid, received_at);

            CREATE TABLE IF NOT EXISTS toast_order_current (
                order_guid TEXT PRIMARY KEY,
                event_guid TEXT NOT NULL,
                restaurant_guid TEXT,
                store_key TEXT,
                business_date TEXT,
                source TEXT,
                payment_status TEXT,
                approval_status TEXT,
                opened_date TEXT,
                modified_date TEXT,
                closed_date TEXT,
                paid_date TEXT,
                server_toast_guid TEXT,
                table_guid TEXT,
                table_name TEXT,
                order_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_toast_order_current_store_date
                ON toast_order_current(store_key, business_date);

            CREATE TABLE IF NOT EXISTS toast_check_current (
                check_guid TEXT PRIMARY KEY,
                order_guid TEXT NOT NULL,
                event_guid TEXT NOT NULL,
                store_key TEXT,
                business_date TEXT,
                display_number TEXT,
                payment_status TEXT,
                amount REAL,
                total_amount REAL,
                tax_amount REAL,
                opened_date TEXT,
                modified_date TEXT,
                closed_date TEXT,
                paid_date TEXT,
                voided INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                check_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_toast_check_order
                ON toast_check_current(order_guid);

            CREATE TABLE IF NOT EXISTS toast_selection_current (
                selection_guid TEXT PRIMARY KEY,
                check_guid TEXT,
                order_guid TEXT NOT NULL,
                event_guid TEXT NOT NULL,
                store_key TEXT,
                business_date TEXT,
                display_name TEXT,
                quantity REAL,
                price REAL,
                voided INTEGER NOT NULL DEFAULT 0,
                selection_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_toast_selection_order
                ON toast_selection_current(order_guid);

            CREATE TABLE IF NOT EXISTS toast_payment_current (
                payment_guid TEXT PRIMARY KEY,
                check_guid TEXT,
                order_guid TEXT NOT NULL,
                event_guid TEXT NOT NULL,
                store_key TEXT,
                business_date TEXT,
                payment_type TEXT,
                payment_status TEXT,
                amount REAL,
                tip_amount REAL,
                paid_date TEXT,
                payment_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_toast_payment_order
                ON toast_payment_current(order_guid);

            CREATE TABLE IF NOT EXISTS toast_fulfillment_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_guid TEXT NOT NULL UNIQUE,
                restaurant_guid TEXT,
                store_key TEXT,
                order_guid TEXT,
                status TEXT,
                occurred_at TEXT,
                details_json TEXT NOT NULL,
                received_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS toast_dimension_item (
                domain TEXT NOT NULL,
                store_key TEXT NOT NULL,
                toast_guid TEXT NOT NULL,
                name TEXT,
                payload_json TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(domain, store_key, toast_guid)
            );

            CREATE TABLE IF NOT EXISTS employee_toast_identity_map (
                store_key TEXT NOT NULL,
                toast_employee_guid TEXT NOT NULL,
                cena_employee_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0.5,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                PRIMARY KEY(store_key, toast_employee_guid)
            );
            CREATE INDEX IF NOT EXISTS ix_employee_toast_identity_cena
                ON employee_toast_identity_map(cena_employee_id, store_key);

            CREATE TABLE IF NOT EXISTS employee_toast_fact (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cena_employee_id INTEGER,
                store_key TEXT,
                toast_employee_guid TEXT,
                fact_type TEXT NOT NULL,
                entity_type TEXT,
                entity_guid TEXT,
                order_guid TEXT,
                check_guid TEXT,
                event_guid TEXT,
                business_date TEXT,
                occurred_at TEXT,
                summary_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(cena_employee_id, fact_type, entity_type, entity_guid, event_guid)
            );
            CREATE INDEX IF NOT EXISTS ix_employee_toast_fact_employee_time
                ON employee_toast_fact(cena_employee_id, occurred_at);

            CREATE TABLE IF NOT EXISTS employee_toast_unmatched (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_key TEXT,
                toast_employee_guid TEXT NOT NULL,
                event_guid TEXT,
                context TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1,
                UNIQUE(store_key, toast_employee_guid, context)
            );

            CREATE TABLE IF NOT EXISTS employee_profile_current (
                cena_employee_id INTEGER PRIMARY KEY,
                profile_json TEXT NOT NULL,
                source TEXT NOT NULL,
                generated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS toast_dimension_sync (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                store_key TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                ok INTEGER NOT NULL DEFAULT 0,
                row_count INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            """
        )

    def store_webhook_event(
        self,
        *,
        payload: dict[str, Any],
        raw_body: bytes,
        headers: dict[str, str],
        signature_verified: bool,
        source: str,
    ) -> dict[str, Any]:
        """Persist a webhook and project it if this event GUID is new."""
        self.init_schema()
        received_at = _utc_now()
        payload_sha = hashlib.sha256(raw_body).hexdigest()
        event_guid = _text(payload.get("guid")) or f"missing:{payload_sha}"
        event_category = _text(payload.get("eventCategory"), 100)
        event_type = _text(payload.get("eventType"), 100)
        toast_timestamp = _text(payload.get("timestamp"), 80)
        order, order_restaurant_guid = _order_from_payload(payload)
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        restaurant_guid = (
            _text(order_restaurant_guid)
            or _text(details.get("restaurantGuid") if isinstance(details, dict) else None)
            or _text(headers.get("Toast-Restaurant-External-ID"))
        )
        store_key = self.store_key_for_restaurant(restaurant_guid)
        attempt_number = None
        try:
            attempt_number = int(headers.get("Toast-Attempt-Number") or "0") or None
        except ValueError:
            attempt_number = None

        with self.connect() as conn:
            self._init_schema(conn)
            conn.execute(
                """
                INSERT INTO toast_webhook_delivery_attempt
                    (event_guid, received_at, payload_sha256, attempt_number, source,
                     signature_verified, status, error)
                VALUES (?, ?, ?, ?, ?, ?, 'received', NULL)
                """,
                (event_guid, received_at, payload_sha, attempt_number, source, int(signature_verified)),
            )
            inserted = False
            try:
                conn.execute(
                    """
                    INSERT INTO toast_webhook_event
                        (event_guid, event_category, event_type, restaurant_guid, store_key,
                         toast_timestamp, received_at, payload_sha256, signature_verified,
                         source, attempt_number, raw_json, redacted_headers_json,
                         processing_status, processing_error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stored', NULL, ?, ?)
                    """,
                    (
                        event_guid,
                        event_category,
                        event_type,
                        restaurant_guid,
                        store_key,
                        toast_timestamp,
                        received_at,
                        payload_sha,
                        int(signature_verified),
                        source,
                        attempt_number,
                        _as_json(payload) if os.getenv("TOAST_WEBHOOK_STORE_RAW_JSON", "1") != "0" else None,
                        _as_json(headers),
                        received_at,
                        received_at,
                    ),
                )
                inserted = True
            except sqlite3.IntegrityError:
                conn.execute(
                    """
                    UPDATE toast_webhook_event
                    SET updated_at = ?, attempt_number = COALESCE(?, attempt_number)
                    WHERE event_guid = ?
                    """,
                    (received_at, attempt_number, event_guid),
                )

            projection_error = None
            if inserted:
                try:
                    self._project_event(conn, payload, event_guid, restaurant_guid, store_key, received_at)
                    conn.execute(
                        """
                        UPDATE toast_webhook_event
                        SET processing_status = 'projected', updated_at = ?
                        WHERE event_guid = ?
                        """,
                        (_utc_now(), event_guid),
                    )
                except Exception as exc:  # noqa: BLE001 - keep ingest durable, record error.
                    projection_error = str(exc)[:600]
                    conn.execute(
                        """
                        UPDATE toast_webhook_event
                        SET processing_status = 'error', processing_error = ?, updated_at = ?
                        WHERE event_guid = ?
                        """,
                        (projection_error, _utc_now(), event_guid),
                    )
            conn.commit()
        return {
            "ok": True,
            "event_guid": event_guid,
            "stored": inserted,
            "duplicate": not inserted,
            "projected": inserted and projection_error is None,
            "projection_error": projection_error,
        }

    def _project_event(
        self,
        conn: sqlite3.Connection,
        payload: dict[str, Any],
        event_guid: str,
        restaurant_guid: str | None,
        store_key: str | None,
        received_at: str,
    ) -> None:
        event_type = _text(payload.get("eventType"), 100) or ""
        event_category = _text(payload.get("eventCategory"), 100) or ""
        order, order_restaurant_guid = _order_from_payload(payload)
        if order is not None:
            restaurant_guid = restaurant_guid or order_restaurant_guid
            store_key = store_key or self.store_key_for_restaurant(restaurant_guid)
            self._project_order(conn, event_guid, order, restaurant_guid, store_key, received_at)
            return
        if "guestOrderStatus" in event_type or "guestOrderStatus" in event_category:
            self._project_fulfillment(conn, event_guid, payload, restaurant_guid, store_key, received_at)
            return
        self._project_event_signal(conn, event_guid, payload, restaurant_guid, store_key, received_at)

    def _project_order(
        self,
        conn: sqlite3.Connection,
        event_guid: str,
        order: dict[str, Any],
        restaurant_guid: str | None,
        store_key: str | None,
        received_at: str,
    ) -> None:
        order_guid = _text(order.get("guid"))
        if not order_guid:
            return
        business_date = _business_date(order.get("businessDate"))
        server_guid = _server_guid(order)
        table_guid = _table_guid(order)
        table_name = _table_name(order)
        opened_date = _text(order.get("openedDate") or order.get("createdDate"), 80)
        modified_date = _text(order.get("modifiedDate"), 80)
        closed_date = _text(order.get("closedDate"), 80)
        paid_date = _text(order.get("paidDate"), 80)
        previous_row = conn.execute(
            "SELECT order_json FROM toast_order_current WHERE order_guid = ?",
            (order_guid,),
        ).fetchone()
        previous = _parse_json_object(previous_row["order_json"]) if previous_row else None

        conn.execute(
            """
            INSERT OR REPLACE INTO toast_order_snapshot
                (event_guid, order_guid, restaurant_guid, store_key, business_date,
                 opened_date, modified_date, closed_date, paid_date, server_toast_guid,
                 table_guid, table_name, order_json, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_guid,
                order_guid,
                restaurant_guid,
                store_key,
                business_date,
                opened_date,
                modified_date,
                closed_date,
                paid_date,
                server_guid,
                table_guid,
                table_name,
                _as_json(order),
                received_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO toast_order_current
                (order_guid, event_guid, restaurant_guid, store_key, business_date,
                 source, payment_status, approval_status, opened_date, modified_date,
                 closed_date, paid_date, server_toast_guid, table_guid, table_name,
                 order_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_guid) DO UPDATE SET
                event_guid = excluded.event_guid,
                restaurant_guid = excluded.restaurant_guid,
                store_key = excluded.store_key,
                business_date = excluded.business_date,
                source = excluded.source,
                payment_status = excluded.payment_status,
                approval_status = excluded.approval_status,
                opened_date = excluded.opened_date,
                modified_date = excluded.modified_date,
                closed_date = excluded.closed_date,
                paid_date = excluded.paid_date,
                server_toast_guid = excluded.server_toast_guid,
                table_guid = excluded.table_guid,
                table_name = excluded.table_name,
                order_json = excluded.order_json,
                updated_at = excluded.updated_at
            """,
            (
                order_guid,
                event_guid,
                restaurant_guid,
                store_key,
                business_date,
                _text(order.get("source"), 80),
                _text(order.get("paymentStatus"), 80),
                _text(order.get("approvalStatus"), 80),
                opened_date,
                modified_date,
                closed_date,
                paid_date,
                server_guid,
                table_guid,
                table_name,
                _as_json(order),
                _utc_now(),
            ),
        )

        conn.execute("DELETE FROM toast_check_current WHERE order_guid = ?", (order_guid,))
        conn.execute("DELETE FROM toast_selection_current WHERE order_guid = ?", (order_guid,))
        conn.execute("DELETE FROM toast_payment_current WHERE order_guid = ?", (order_guid,))
        self._rewrite_order_children(conn, event_guid, order_guid, store_key, business_date, order)
        self._project_order_employee_facts(
            conn,
            event_guid,
            order_guid,
            store_key,
            business_date,
            order,
            previous,
            received_at,
        )

    def _rewrite_order_children(
        self,
        conn: sqlite3.Connection,
        event_guid: str,
        order_guid: str,
        store_key: str | None,
        business_date: str | None,
        order: dict[str, Any],
    ) -> None:
        now = _utc_now()
        for check in order.get("checks") or []:
            if not isinstance(check, dict):
                continue
            check_guid = _text(check.get("guid"))
            if not check_guid:
                continue
            conn.execute(
                """
                INSERT INTO toast_check_current
                    (check_guid, order_guid, event_guid, store_key, business_date,
                     display_number, payment_status, amount, total_amount, tax_amount,
                     opened_date, modified_date, closed_date, paid_date, voided, deleted,
                     check_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    check_guid,
                    order_guid,
                    event_guid,
                    store_key,
                    business_date,
                    _text(check.get("displayNumber"), 80),
                    _text(check.get("paymentStatus"), 80),
                    _amount(check.get("amount")),
                    _amount(check.get("totalAmount")),
                    _amount(check.get("taxAmount")),
                    _text(check.get("openedDate"), 80),
                    _text(check.get("modifiedDate"), 80),
                    _text(check.get("closedDate"), 80),
                    _text(check.get("paidDate"), 80),
                    int(bool(check.get("voided"))),
                    int(bool(check.get("deleted"))),
                    _as_json(check),
                    now,
                ),
            )
            for sel in check.get("selections") or []:
                if not isinstance(sel, dict):
                    continue
                selection_guid = _text(sel.get("guid"))
                if not selection_guid:
                    continue
                conn.execute(
                    """
                    INSERT INTO toast_selection_current
                        (selection_guid, check_guid, order_guid, event_guid, store_key,
                         business_date, display_name, quantity, price, voided,
                         selection_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        selection_guid,
                        check_guid,
                        order_guid,
                        event_guid,
                        store_key,
                        business_date,
                        _text(sel.get("displayName") or sel.get("name"), 180),
                        _amount(sel.get("quantity")),
                        _amount(sel.get("price")),
                        int(bool(sel.get("voided"))),
                        _as_json(sel),
                        now,
                    ),
                )
            for payment in check.get("payments") or []:
                if not isinstance(payment, dict):
                    continue
                payment_guid = _text(payment.get("guid"))
                if not payment_guid:
                    continue
                conn.execute(
                    """
                    INSERT INTO toast_payment_current
                        (payment_guid, check_guid, order_guid, event_guid, store_key,
                         business_date, payment_type, payment_status, amount, tip_amount,
                         paid_date, payment_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payment_guid,
                        check_guid,
                        order_guid,
                        event_guid,
                        store_key,
                        business_date,
                        _text(payment.get("type"), 80),
                        _text(payment.get("paymentStatus"), 80),
                        _amount(payment.get("amount")),
                        _amount(payment.get("tipAmount")),
                        _text(payment.get("paidDate"), 80),
                        _as_json(payment),
                        now,
                    ),
                )

    def _project_order_employee_facts(
        self,
        conn: sqlite3.Connection,
        event_guid: str,
        order_guid: str,
        store_key: str | None,
        business_date: str | None,
        order: dict[str, Any],
        previous: dict[str, Any] | None,
        received_at: str,
    ) -> None:
        server_guid = _server_guid(order)
        if not server_guid:
            return
        table_label = _table_name(order)
        if previous is None:
            self._record_employee_fact_for_guid(
                conn,
                store_key=store_key,
                toast_employee_guid=server_guid,
                fact_type="order_created",
                entity_type="order",
                entity_guid=order_guid,
                order_guid=order_guid,
                check_guid=None,
                event_guid=event_guid,
                business_date=business_date,
                occurred_at=_text(order.get("openedDate") or order.get("createdDate"), 80) or received_at,
                summary={
                    "source": _text(order.get("source"), 80),
                    "table": table_label,
                    "display_number": _text(order.get("displayNumber"), 80),
                },
            )

        prev_checks = _check_map(previous or {})
        for check_guid, check in _check_map(order).items():
            prev_check = prev_checks.get(check_guid)
            if prev_check is None:
                self._record_employee_fact_for_guid(
                    conn,
                    store_key=store_key,
                    toast_employee_guid=server_guid,
                    fact_type="check_opened",
                    entity_type="check",
                    entity_guid=check_guid,
                    order_guid=order_guid,
                    check_guid=check_guid,
                    event_guid=event_guid,
                    business_date=business_date,
                    occurred_at=_text(check.get("openedDate"), 80) or received_at,
                    summary={
                        "table": table_label,
                        "display_number": _text(check.get("displayNumber"), 80),
                        "payment_status": _text(check.get("paymentStatus"), 80),
                    },
                )
            if prev_check and not prev_check.get("closedDate") and check.get("closedDate"):
                self._record_employee_fact_for_guid(
                    conn,
                    store_key=store_key,
                    toast_employee_guid=server_guid,
                    fact_type="check_closed",
                    entity_type="check",
                    entity_guid=check_guid,
                    order_guid=order_guid,
                    check_guid=check_guid,
                    event_guid=event_guid,
                    business_date=business_date,
                    occurred_at=_text(check.get("closedDate"), 80) or received_at,
                    summary={
                        "table": table_label,
                        "display_number": _text(check.get("displayNumber"), 80),
                        "total_amount": _amount(check.get("totalAmount")),
                    },
                )

        prev_selections = _selection_map(previous or {})
        for selection_guid, item in _selection_map(order).items():
            if selection_guid in prev_selections:
                continue
            sel = item["selection"]
            self._record_employee_fact_for_guid(
                conn,
                store_key=store_key,
                toast_employee_guid=server_guid,
                fact_type="item_added",
                entity_type="selection",
                entity_guid=selection_guid,
                order_guid=order_guid,
                check_guid=item.get("check_guid"),
                event_guid=event_guid,
                business_date=business_date,
                occurred_at=received_at,
                summary={
                    "name": _text(sel.get("displayName") or sel.get("name"), 180),
                    "quantity": _amount(sel.get("quantity")),
                    "price": _amount(sel.get("price")),
                    "voided": bool(sel.get("voided")),
                    "table": table_label,
                },
            )

        prev_payments = _payment_map(previous or {})
        for payment_guid, item in _payment_map(order).items():
            if payment_guid in prev_payments:
                continue
            payment = item["payment"]
            self._record_employee_fact_for_guid(
                conn,
                store_key=store_key,
                toast_employee_guid=server_guid,
                fact_type="payment_added",
                entity_type="payment",
                entity_guid=payment_guid,
                order_guid=order_guid,
                check_guid=item.get("check_guid"),
                event_guid=event_guid,
                business_date=business_date,
                occurred_at=_text(payment.get("paidDate"), 80) or received_at,
                summary={
                    "payment_type": _text(payment.get("type"), 80),
                    "payment_status": _text(payment.get("paymentStatus"), 80),
                    "amount": _amount(payment.get("amount")),
                    "tip_amount": _amount(payment.get("tipAmount")),
                    "table": table_label,
                },
            )

    def _record_employee_fact_for_guid(
        self,
        conn: sqlite3.Connection,
        *,
        store_key: str | None,
        toast_employee_guid: str,
        fact_type: str,
        entity_type: str,
        entity_guid: str,
        order_guid: str | None,
        check_guid: str | None,
        event_guid: str,
        business_date: str | None,
        occurred_at: str | None,
        summary: dict[str, Any],
    ) -> None:
        cena_employee_id = self.resolve_employee(conn, store_key, toast_employee_guid)
        if cena_employee_id is None:
            self.record_unmatched_employee(conn, store_key, toast_employee_guid, event_guid, fact_type)
        conn.execute(
            """
            INSERT OR IGNORE INTO employee_toast_fact
                (cena_employee_id, store_key, toast_employee_guid, fact_type, entity_type,
                 entity_guid, order_guid, check_guid, event_guid, business_date,
                 occurred_at, summary_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cena_employee_id,
                store_key,
                toast_employee_guid,
                fact_type,
                entity_type,
                entity_guid,
                order_guid,
                check_guid,
                event_guid,
                business_date,
                occurred_at,
                _as_json({k: v for k, v in summary.items() if v is not None}),
                _utc_now(),
            ),
        )

    def _project_fulfillment(
        self,
        conn: sqlite3.Connection,
        event_guid: str,
        payload: dict[str, Any],
        restaurant_guid: str | None,
        store_key: str | None,
        received_at: str,
    ) -> None:
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        order_guid = (
            _text(details.get("orderGuid") if isinstance(details, dict) else None)
            or _guid_from_any(details.get("order") if isinstance(details, dict) else None)
        )
        status = _text(details.get("status") if isinstance(details, dict) else None, 80)
        conn.execute(
            """
            INSERT OR REPLACE INTO toast_fulfillment_status
                (event_guid, restaurant_guid, store_key, order_guid, status, occurred_at,
                 details_json, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_guid,
                restaurant_guid,
                store_key,
                order_guid,
                status,
                _text(payload.get("timestamp"), 80) or received_at,
                _as_json(details),
                received_at,
            ),
        )

    def _project_event_signal(
        self,
        conn: sqlite3.Connection,
        event_guid: str,
        payload: dict[str, Any],
        restaurant_guid: str | None,
        store_key: str | None,
        received_at: str,
    ) -> None:
        domain = _text(payload.get("eventCategory"), 80) or "webhook"
        event_type = _text(payload.get("eventType"), 80) or domain
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        guid = _text(details.get("guid") if isinstance(details, dict) else None) or event_guid
        name = _text(details.get("name") if isinstance(details, dict) else None, 180) or event_type
        conn.execute(
            """
            INSERT INTO toast_dimension_item
                (domain, store_key, toast_guid, name, payload_json, source, updated_at)
            VALUES (?, ?, ?, ?, ?, 'webhook', ?)
            ON CONFLICT(domain, store_key, toast_guid) DO UPDATE SET
                name = excluded.name,
                payload_json = excluded.payload_json,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (domain, store_key or self.store_key_for_restaurant(restaurant_guid) or "unknown", guid, name, _as_json(details), received_at),
        )

    def resolve_employee(
        self,
        conn: sqlite3.Connection,
        store_key: str | None,
        toast_employee_guid: str | None,
    ) -> int | None:
        if not store_key or not toast_employee_guid:
            return None
        row = conn.execute(
            """
            SELECT cena_employee_id FROM employee_toast_identity_map
            WHERE store_key = ? AND toast_employee_guid = ?
            """,
            (store_key, toast_employee_guid),
        ).fetchone()
        return int(row["cena_employee_id"]) if row else None

    def record_unmatched_employee(
        self,
        conn: sqlite3.Connection,
        store_key: str | None,
        toast_employee_guid: str,
        event_guid: str | None,
        context: str,
    ) -> None:
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO employee_toast_unmatched
                (store_key, toast_employee_guid, event_guid, context, first_seen, last_seen, seen_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(store_key, toast_employee_guid, context) DO UPDATE SET
                last_seen = excluded.last_seen,
                event_guid = excluded.event_guid,
                seen_count = seen_count + 1
            """,
            (store_key, toast_employee_guid, event_guid, context, now, now),
        )

    def store_key_for_restaurant(self, restaurant_guid: str | None) -> str | None:
        if not restaurant_guid:
            return None
        for store_key, guid in restaurant_guids().items():
            if guid and guid.lower() == restaurant_guid.lower():
                return store_key
        return None

    def upsert_dimension_item(
        self,
        *,
        domain: str,
        store_key: str,
        toast_guid: str,
        name: str | None,
        payload: dict[str, Any],
        source: str = "api",
    ) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO toast_dimension_item
                    (domain, store_key, toast_guid, name, payload_json, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain, store_key, toast_guid) DO UPDATE SET
                    name = excluded.name,
                    payload_json = excluded.payload_json,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (domain, store_key, toast_guid, name, _as_json(payload), source, _utc_now()),
            )
            conn.commit()

    def seed_employee_profiles_and_identity(
        self,
        *,
        app_db_path: str | None = None,
        datamart_db_path: str | None = None,
        perf_db_path: str | None = None,
    ) -> dict[str, int]:
        """Seed employee profile rows and Toast identity links from CK data marts."""
        counts = {"profiles": 0, "identity_links": 0}
        datamart_db_path = datamart_db_path or os.getenv(
            "CENA_DATAMART_DB", r"C:\Users\sam\cena-perfdb\datamart\datamart.sqlite"
        )
        perf_db_path = perf_db_path or os.getenv("CENA_PERFDB_DB", r"C:\Users\sam\cena-perfdb\perf.sqlite")
        app_db_path = app_db_path or os.getenv("CENAS_APP_SQLITE_DB")
        self.init_schema()
        with self.connect() as conn:
            counts["profiles"] += self._seed_profiles_from_datamart(conn, datamart_db_path)
            counts["identity_links"] += self._seed_identity_from_app(conn, app_db_path)
            counts["identity_links"] += self._seed_identity_from_perf(conn, perf_db_path, "perf_period", 0.85)
            counts["identity_links"] += self._seed_identity_from_perf(conn, perf_db_path, "time_entry", 0.75)
            conn.commit()
        return counts

    def _seed_profiles_from_datamart(self, conn: sqlite3.Connection, datamart_db_path: str | None) -> int:
        if not datamart_db_path or not Path(datamart_db_path).exists():
            return 0
        src = sqlite3.connect(datamart_db_path)
        src.row_factory = sqlite3.Row
        count = 0
        try:
            rows = src.execute(
                """
                SELECT cena_employee_id, full_name, active, primary_store_key,
                       positions_json, hire_date, source, generated_at
                FROM dm_profile
                """
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            src.close()
        now = _utc_now()
        for row in rows:
            profile = {
                "cena_employee_id": row["cena_employee_id"],
                "full_name": row["full_name"],
                "active": bool(row["active"]),
                "primary_store_key": row["primary_store_key"],
                "positions": _parse_json_value(row["positions_json"]),
                "hire_date": row["hire_date"],
            }
            conn.execute(
                """
                INSERT INTO employee_profile_current
                    (cena_employee_id, profile_json, source, generated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cena_employee_id) DO UPDATE SET
                    profile_json = excluded.profile_json,
                    source = excluded.source,
                    generated_at = excluded.generated_at
                """,
                (
                    row["cena_employee_id"],
                    _as_json(profile),
                    row["source"] or "datamart",
                    row["generated_at"] or now,
                ),
            )
            count += 1
        return count

    def _seed_identity_from_app(self, conn: sqlite3.Connection, app_db_path: str | None) -> int:
        if not app_db_path or not Path(app_db_path).exists():
            return 0
        src = sqlite3.connect(app_db_path)
        src.row_factory = sqlite3.Row
        try:
            rows = src.execute(
                """
                SELECT cena_employee_id, store_key, toast_id
                FROM cena_toast_link
                WHERE toast_id IS NOT NULL AND TRIM(toast_id) <> ''
                """
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            src.close()
        count = 0
        for row in rows:
            if self._upsert_identity(
                conn,
                store_key=row["store_key"],
                toast_employee_guid=row["toast_id"],
                cena_employee_id=row["cena_employee_id"],
                source="cena_toast_link",
                verified=True,
                confidence=1.0,
            ):
                count += 1
        return count

    def _seed_identity_from_perf(
        self,
        conn: sqlite3.Connection,
        perf_db_path: str | None,
        table_name: str,
        confidence: float,
    ) -> int:
        if not perf_db_path or not Path(perf_db_path).exists():
            return 0
        src = sqlite3.connect(perf_db_path)
        src.row_factory = sqlite3.Row
        try:
            rows = src.execute(
                f"""
                SELECT DISTINCT store_key, toast_employee_id, cena_employee_id
                FROM {table_name}
                WHERE toast_employee_id IS NOT NULL AND TRIM(toast_employee_id) <> ''
                  AND cena_employee_id IS NOT NULL
                  AND store_key IS NOT NULL AND TRIM(store_key) <> ''
                """
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            src.close()
        count = 0
        for row in rows:
            if self._upsert_identity(
                conn,
                store_key=row["store_key"],
                toast_employee_guid=row["toast_employee_id"],
                cena_employee_id=row["cena_employee_id"],
                source=table_name,
                verified=False,
                confidence=confidence,
            ):
                count += 1
        return count

    def _upsert_identity(
        self,
        conn: sqlite3.Connection,
        *,
        store_key: str | None,
        toast_employee_guid: str | None,
        cena_employee_id: int | None,
        source: str,
        verified: bool,
        confidence: float,
    ) -> bool:
        if not store_key or not toast_employee_guid or cena_employee_id is None:
            return False
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO employee_toast_identity_map
                (store_key, toast_employee_guid, cena_employee_id, source, verified,
                 confidence, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(store_key, toast_employee_guid) DO UPDATE SET
                cena_employee_id = CASE
                    WHEN excluded.verified >= employee_toast_identity_map.verified
                    THEN excluded.cena_employee_id
                    ELSE employee_toast_identity_map.cena_employee_id
                END,
                source = CASE
                    WHEN excluded.verified >= employee_toast_identity_map.verified
                    THEN excluded.source
                    ELSE employee_toast_identity_map.source
                END,
                verified = MAX(employee_toast_identity_map.verified, excluded.verified),
                confidence = MAX(employee_toast_identity_map.confidence, excluded.confidence),
                last_seen = excluded.last_seen
            """,
            (
                str(store_key).strip().lower(),
                str(toast_employee_guid).strip(),
                int(cena_employee_id),
                source,
                int(verified),
                confidence,
                now,
                now,
            ),
        )
        return True

    def health(self) -> dict[str, Any]:
        self.init_schema()
        with self.connect() as conn:
            counts = {
                "events": conn.execute("SELECT COUNT(*) FROM toast_webhook_event").fetchone()[0],
                "orders": conn.execute("SELECT COUNT(*) FROM toast_order_current").fetchone()[0],
                "employee_facts": conn.execute("SELECT COUNT(*) FROM employee_toast_fact").fetchone()[0],
                "identity_links": conn.execute("SELECT COUNT(*) FROM employee_toast_identity_map").fetchone()[0],
                "unmatched": conn.execute("SELECT COUNT(*) FROM employee_toast_unmatched").fetchone()[0],
            }
        return {"ok": True, "db_path": str(self.db_path), "counts": counts}


def synthetic_event_guid(prefix: str, payload: dict[str, Any]) -> str:
    return f"{prefix}:{hashlib.sha256(_as_json(payload).encode('utf-8')).hexdigest()[:32]}"


def business_dates_for_backfill(days: int) -> list[str]:
    today_ct = (datetime.utcnow() - timedelta(hours=5)).date()
    return [(today_ct - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(days)]
