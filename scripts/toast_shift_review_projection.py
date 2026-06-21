from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.toast_webhook_store import (
    DEFAULT_DB_PATH as DEFAULT_MIRROR_DB_PATH,
    business_dates_for_backfill,
)


DEFAULT_REVIEW_DB_PATH = (
    r"C:\Users\sam\cena-ai-assistant\toast_webhook\toast_shift_reviews.sqlite"
)
SCHEMA_VERSION = "1"
VALID_PAYMENT_STATUSES = ("CAPTURED", "PAID", "SETTLED")


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _review_id(store_key: str | None, business_date: str | None, employee_guid: str | None) -> str:
    basis = "|".join((store_key or "", business_date or "", employee_guid or ""))
    return "sr_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _json_warnings(cena_employee_id: Any, time_entry_count: Any, open_time_entry_count: Any) -> str:
    warnings: list[str] = []
    try:
        time_entries = int(time_entry_count or 0)
    except (TypeError, ValueError):
        time_entries = 0
    try:
        open_entries = int(open_time_entry_count or 0)
    except (TypeError, ValueError):
        open_entries = 0
    if cena_employee_id is None:
        warnings.append("missing_cena_employee_identity")
    if time_entries <= 0:
        warnings.append("no_toast_clock_entry")
    if open_entries > 0:
        warnings.append("open_toast_clock_entry")
    return json.dumps(warnings, separators=(",", ":"))


def _date_filter(dates: list[str]) -> tuple[str, list[str]]:
    if not dates:
        raise ValueError("at least one business date is required")
    return ",".join("?" for _ in dates), dates


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS toast_employee_shift_review_current (
            review_id TEXT PRIMARY KEY,
            store_key TEXT NOT NULL,
            business_date TEXT NOT NULL,
            employee_toast_guid TEXT NOT NULL,
            cena_employee_id INTEGER,
            employee_display_name TEXT,
            employee_first_name TEXT,
            employee_last_name TEXT,
            job_titles TEXT,
            time_entry_count INTEGER NOT NULL DEFAULT 0,
            open_time_entry_count INTEGER NOT NULL DEFAULT 0,
            clock_in_first TEXT,
            clock_out_last TEXT,
            regular_hours REAL NOT NULL DEFAULT 0,
            overtime_hours REAL NOT NULL DEFAULT 0,
            total_hours REAL NOT NULL DEFAULT 0,
            scheduled_shift_count INTEGER NOT NULL DEFAULT 0,
            scheduled_in_first TEXT,
            scheduled_out_last TEXT,
            order_count INTEGER NOT NULL DEFAULT 0,
            check_count INTEGER NOT NULL DEFAULT 0,
            zero_check_count INTEGER NOT NULL DEFAULT 0,
            payment_count INTEGER NOT NULL DEFAULT 0,
            selection_count INTEGER NOT NULL DEFAULT 0,
            voided_selection_count INTEGER NOT NULL DEFAULT 0,
            check_subtotal_amount REAL NOT NULL DEFAULT 0,
            check_tax_amount REAL NOT NULL DEFAULT 0,
            check_total_amount REAL NOT NULL DEFAULT 0,
            payment_amount REAL NOT NULL DEFAULT 0,
            payment_tip_amount REAL NOT NULL DEFAULT 0,
            total_collected_with_tips_amount REAL NOT NULL DEFAULT 0,
            cash_payment_amount REAL NOT NULL DEFAULT 0,
            credit_payment_amount REAL NOT NULL DEFAULT 0,
            other_payment_amount REAL NOT NULL DEFAULT 0,
            cash_tip_amount REAL NOT NULL DEFAULT 0,
            credit_tip_amount REAL NOT NULL DEFAULT 0,
            other_tip_amount REAL NOT NULL DEFAULT 0,
            non_cash_tip_amount REAL NOT NULL DEFAULT 0,
            settlement_net_amount REAL NOT NULL DEFAULT 0,
            owed_to_employee_amount REAL NOT NULL DEFAULT 0,
            owed_to_restaurant_amount REAL NOT NULL DEFAULT 0,
            settlement_formula TEXT NOT NULL,
            has_sales INTEGER NOT NULL DEFAULT 0,
            has_clock INTEGER NOT NULL DEFAULT 0,
            has_open_clock INTEGER NOT NULL DEFAULT 0,
            data_warnings_json TEXT NOT NULL DEFAULT '[]',
            first_order_opened_at TEXT,
            last_order_closed_at TEXT,
            first_payment_paid_at TEXT,
            last_payment_paid_at TEXT,
            source_mirror_db TEXT NOT NULL,
            source_updated_at TEXT,
            calculated_at TEXT NOT NULL,
            UNIQUE(store_key, business_date, employee_toast_guid)
        );
        CREATE INDEX IF NOT EXISTS ix_shift_review_employee_date
            ON toast_employee_shift_review_current(
                employee_toast_guid, business_date, store_key
            );
        CREATE INDEX IF NOT EXISTS ix_shift_review_cena_date
            ON toast_employee_shift_review_current(
                cena_employee_id, business_date, store_key
            );

        CREATE TABLE IF NOT EXISTS toast_employee_shift_review_time_entry (
            review_id TEXT NOT NULL,
            store_key TEXT NOT NULL,
            business_date TEXT,
            employee_toast_guid TEXT,
            time_entry_guid TEXT NOT NULL,
            job_guid TEXT,
            job_title TEXT,
            clock_in TEXT,
            clock_out TEXT,
            regular_hours REAL,
            overtime_hours REAL,
            total_hours REAL,
            deleted INTEGER NOT NULL DEFAULT 0,
            source_updated_at TEXT,
            PRIMARY KEY(review_id, store_key, time_entry_guid),
            FOREIGN KEY(review_id)
                REFERENCES toast_employee_shift_review_current(review_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS toast_employee_shift_review_scheduled_shift (
            review_id TEXT NOT NULL,
            store_key TEXT NOT NULL,
            business_date TEXT,
            employee_toast_guid TEXT,
            shift_guid TEXT NOT NULL,
            job_guid TEXT,
            job_title TEXT,
            scheduled_in TEXT,
            scheduled_out TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            source_updated_at TEXT,
            PRIMARY KEY(review_id, store_key, shift_guid),
            FOREIGN KEY(review_id)
                REFERENCES toast_employee_shift_review_current(review_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS toast_employee_shift_review_order (
            review_id TEXT NOT NULL,
            order_guid TEXT NOT NULL,
            store_key TEXT,
            business_date TEXT,
            source TEXT,
            payment_status TEXT,
            approval_status TEXT,
            opened_date TEXT,
            closed_date TEXT,
            paid_date TEXT,
            table_guid TEXT,
            table_name TEXT,
            source_updated_at TEXT,
            PRIMARY KEY(review_id, order_guid),
            FOREIGN KEY(review_id)
                REFERENCES toast_employee_shift_review_current(review_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS toast_employee_shift_review_check (
            review_id TEXT NOT NULL,
            check_guid TEXT NOT NULL,
            order_guid TEXT NOT NULL,
            store_key TEXT,
            business_date TEXT,
            display_number TEXT,
            payment_status TEXT,
            amount REAL,
            tax_amount REAL,
            total_amount REAL,
            opened_date TEXT,
            closed_date TEXT,
            paid_date TEXT,
            voided INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            source_updated_at TEXT,
            PRIMARY KEY(review_id, check_guid),
            FOREIGN KEY(review_id)
                REFERENCES toast_employee_shift_review_current(review_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS toast_employee_shift_review_payment (
            review_id TEXT NOT NULL,
            payment_guid TEXT NOT NULL,
            check_guid TEXT,
            order_guid TEXT NOT NULL,
            store_key TEXT,
            business_date TEXT,
            payment_type TEXT,
            payment_status TEXT,
            amount REAL,
            tip_amount REAL,
            paid_date TEXT,
            source_updated_at TEXT,
            PRIMARY KEY(review_id, payment_guid),
            FOREIGN KEY(review_id)
                REFERENCES toast_employee_shift_review_current(review_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS toast_employee_shift_review_selection (
            review_id TEXT NOT NULL,
            selection_guid TEXT NOT NULL,
            check_guid TEXT,
            order_guid TEXT NOT NULL,
            store_key TEXT,
            business_date TEXT,
            display_name TEXT,
            quantity REAL,
            price REAL,
            voided INTEGER NOT NULL DEFAULT 0,
            source_updated_at TEXT,
            PRIMARY KEY(review_id, selection_guid),
            FOREIGN KEY(review_id)
                REFERENCES toast_employee_shift_review_current(review_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS toast_employee_shift_review_run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            source_db TEXT NOT NULL,
            review_db TEXT NOT NULL,
            scope_start TEXT,
            scope_end TEXT,
            ok INTEGER NOT NULL DEFAULT 0,
            review_count INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        ("schema_version", SCHEMA_VERSION),
    )
    conn.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        ("settlement_formula", "non_cash_tip_amount + cash_tip_amount - cash_payment_amount"),
    )


def project_shift_reviews(
    *,
    source_db: str | os.PathLike[str],
    review_db: str | os.PathLike[str],
    dates: list[str],
) -> dict[str, Any]:
    source_path = Path(source_db)
    review_path = Path(review_db)
    if not source_path.exists():
        raise FileNotFoundError(f"Toast mirror DB not found: {source_path}")
    review_path.parent.mkdir(parents=True, exist_ok=True)
    date_placeholders, date_params = _date_filter(dates)
    scope_start = min(dates)
    scope_end = max(dates)
    started_at = _utc_now()
    calculated_at = started_at

    conn = sqlite3.connect(str(review_path))
    conn.row_factory = sqlite3.Row
    conn.create_function("ck_shift_review_id", 3, _review_id)
    conn.create_function("ck_shift_review_warnings", 3, _json_warnings)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        init_schema(conn)
        conn.execute("ATTACH DATABASE ? AS src", (str(source_path),))
        conn.execute(
            """
            INSERT INTO toast_employee_shift_review_run_log(
                started_at, source_db, review_db, scope_start, scope_end
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (started_at, str(source_path), str(review_path), scope_start, scope_end),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        try:
            for table in (
                "toast_employee_shift_review_selection",
                "toast_employee_shift_review_payment",
                "toast_employee_shift_review_check",
                "toast_employee_shift_review_order",
                "toast_employee_shift_review_scheduled_shift",
                "toast_employee_shift_review_time_entry",
                "toast_employee_shift_review_current",
            ):
                conn.execute(
                    f"DELETE FROM {table} WHERE business_date IN ({date_placeholders})",
                    date_params,
                )

            insert_params: list[Any] = [
                *(date_params * 11),
                str(source_path),
                calculated_at,
            ]
            conn.execute(
                f"""
                WITH employee_keys AS (
                    SELECT store_key, business_date, employee_guid AS employee_toast_guid
                    FROM src.toast_time_entry_current
                    WHERE business_date IN ({date_placeholders})
                      AND employee_guid IS NOT NULL
                      AND COALESCE(deleted, 0) = 0
                    UNION
                    SELECT
                        store_key,
                        REPLACE(business_date, '-', '') AS business_date,
                        employee_guid AS employee_toast_guid
                    FROM src.toast_shift_current
                    WHERE REPLACE(business_date, '-', '') IN ({date_placeholders})
                      AND employee_guid IS NOT NULL
                      AND COALESCE(deleted, 0) = 0
                    UNION
                    SELECT store_key, business_date, server_toast_guid AS employee_toast_guid
                    FROM src.toast_order_current
                    WHERE business_date IN ({date_placeholders})
                      AND server_toast_guid IS NOT NULL
                ),
                time_summary AS (
                    SELECT
                        te.store_key,
                        te.business_date,
                        te.employee_guid AS employee_toast_guid,
                        COUNT(*) AS time_entry_count,
                        SUM(CASE WHEN te.clock_out IS NULL OR te.clock_out = '' THEN 1 ELSE 0 END)
                            AS open_time_entry_count,
                        MIN(te.clock_in) AS clock_in_first,
                        MAX(te.clock_out) AS clock_out_last,
                        ROUND(COALESCE(SUM(te.regular_hours), 0), 4) AS regular_hours,
                        ROUND(COALESCE(SUM(te.overtime_hours), 0), 4) AS overtime_hours,
                        ROUND(COALESCE(SUM(te.total_hours), 0), 4) AS total_hours,
                        MAX(te.updated_at) AS source_updated_at
                    FROM src.toast_time_entry_current te
                    WHERE te.business_date IN ({date_placeholders})
                      AND te.employee_guid IS NOT NULL
                      AND COALESCE(te.deleted, 0) = 0
                    GROUP BY te.store_key, te.business_date, te.employee_guid
                ),
                scheduled_summary AS (
                    SELECT
                        sh.store_key,
                        REPLACE(sh.business_date, '-', '') AS business_date,
                        sh.employee_guid AS employee_toast_guid,
                        COUNT(*) AS scheduled_shift_count,
                        MIN(sh.scheduled_in) AS scheduled_in_first,
                        MAX(sh.scheduled_out) AS scheduled_out_last,
                        MAX(sh.updated_at) AS source_updated_at
                    FROM src.toast_shift_current sh
                    WHERE REPLACE(sh.business_date, '-', '') IN ({date_placeholders})
                      AND sh.employee_guid IS NOT NULL
                      AND COALESCE(sh.deleted, 0) = 0
                    GROUP BY sh.store_key, REPLACE(sh.business_date, '-', ''), sh.employee_guid
                ),
                job_summary AS (
                    SELECT
                        jobs.store_key,
                        jobs.business_date,
                        jobs.employee_toast_guid,
                        GROUP_CONCAT(DISTINCT COALESCE(j.title, jobs.job_guid)) AS job_titles
                    FROM (
                        SELECT store_key, business_date, employee_guid AS employee_toast_guid, job_guid
                        FROM src.toast_time_entry_current
                        WHERE business_date IN ({date_placeholders})
                          AND employee_guid IS NOT NULL
                          AND job_guid IS NOT NULL
                          AND COALESCE(deleted, 0) = 0
                        UNION
                        SELECT
                            store_key,
                            REPLACE(business_date, '-', '') AS business_date,
                            employee_guid AS employee_toast_guid,
                            job_guid
                        FROM src.toast_shift_current
                        WHERE REPLACE(business_date, '-', '') IN ({date_placeholders})
                          AND employee_guid IS NOT NULL
                          AND job_guid IS NOT NULL
                          AND COALESCE(deleted, 0) = 0
                    ) jobs
                    LEFT JOIN src.toast_job_current j
                      ON j.store_key = jobs.store_key
                     AND j.job_guid = jobs.job_guid
                    GROUP BY jobs.store_key, jobs.business_date, jobs.employee_toast_guid
                ),
                order_summary AS (
                    SELECT
                        o.store_key,
                        o.business_date,
                        o.server_toast_guid AS employee_toast_guid,
                        COUNT(*) AS order_count,
                        MIN(o.opened_date) AS first_order_opened_at,
                        MAX(o.closed_date) AS last_order_closed_at,
                        MAX(o.updated_at) AS source_updated_at
                    FROM src.toast_order_current o
                    WHERE o.business_date IN ({date_placeholders})
                      AND o.server_toast_guid IS NOT NULL
                    GROUP BY o.store_key, o.business_date, o.server_toast_guid
                ),
                check_summary AS (
                    SELECT
                        o.store_key,
                        o.business_date,
                        o.server_toast_guid AS employee_toast_guid,
                        COUNT(DISTINCT c.check_guid) AS check_count,
                        SUM(
                            CASE
                                WHEN ROUND(COALESCE(c.amount, 0), 2) = 0
                                 AND ROUND(COALESCE(c.total_amount, 0), 2) = 0
                                THEN 1 ELSE 0
                            END
                        ) AS zero_check_count,
                        ROUND(COALESCE(SUM(CASE WHEN COALESCE(c.voided, 0) = 0
                                                 AND COALESCE(c.deleted, 0) = 0
                                                THEN c.amount ELSE 0 END), 0), 2)
                            AS check_subtotal_amount,
                        ROUND(COALESCE(SUM(CASE WHEN COALESCE(c.voided, 0) = 0
                                                 AND COALESCE(c.deleted, 0) = 0
                                                THEN c.tax_amount ELSE 0 END), 0), 2)
                            AS check_tax_amount,
                        ROUND(COALESCE(SUM(CASE WHEN COALESCE(c.voided, 0) = 0
                                                 AND COALESCE(c.deleted, 0) = 0
                                                THEN c.total_amount ELSE 0 END), 0), 2)
                            AS check_total_amount,
                        MAX(c.updated_at) AS source_updated_at
                    FROM src.toast_check_current c
                    JOIN src.toast_order_current o
                      ON o.order_guid = c.order_guid
                    WHERE c.business_date IN ({date_placeholders})
                      AND o.server_toast_guid IS NOT NULL
                    GROUP BY o.store_key, o.business_date, o.server_toast_guid
                ),
                payment_summary AS (
                    SELECT
                        o.store_key,
                        o.business_date,
                        o.server_toast_guid AS employee_toast_guid,
                        COUNT(DISTINCT p.payment_guid) AS payment_count,
                        ROUND(COALESCE(SUM(p.amount), 0), 2) AS payment_amount,
                        ROUND(COALESCE(SUM(p.tip_amount), 0), 2) AS payment_tip_amount,
                        ROUND(COALESCE(SUM(COALESCE(p.amount, 0) + COALESCE(p.tip_amount, 0)), 0), 2)
                            AS total_collected_with_tips_amount,
                        ROUND(COALESCE(SUM(CASE WHEN UPPER(COALESCE(p.payment_type, '')) = 'CASH'
                                                THEN p.amount ELSE 0 END), 0), 2)
                            AS cash_payment_amount,
                        ROUND(COALESCE(SUM(CASE WHEN UPPER(COALESCE(p.payment_type, '')) = 'CREDIT'
                                                THEN p.amount ELSE 0 END), 0), 2)
                            AS credit_payment_amount,
                        ROUND(COALESCE(SUM(CASE WHEN UPPER(COALESCE(p.payment_type, '')) NOT IN ('CASH', 'CREDIT')
                                                THEN p.amount ELSE 0 END), 0), 2)
                            AS other_payment_amount,
                        ROUND(COALESCE(SUM(CASE WHEN UPPER(COALESCE(p.payment_type, '')) = 'CASH'
                                                THEN p.tip_amount ELSE 0 END), 0), 2)
                            AS cash_tip_amount,
                        ROUND(COALESCE(SUM(CASE WHEN UPPER(COALESCE(p.payment_type, '')) = 'CREDIT'
                                                THEN p.tip_amount ELSE 0 END), 0), 2)
                            AS credit_tip_amount,
                        ROUND(COALESCE(SUM(CASE WHEN UPPER(COALESCE(p.payment_type, '')) NOT IN ('CASH', 'CREDIT')
                                                THEN p.tip_amount ELSE 0 END), 0), 2)
                            AS other_tip_amount,
                        ROUND(COALESCE(SUM(CASE WHEN UPPER(COALESCE(p.payment_type, '')) <> 'CASH'
                                                THEN p.tip_amount ELSE 0 END), 0), 2)
                            AS non_cash_tip_amount,
                        MIN(p.paid_date) AS first_payment_paid_at,
                        MAX(p.paid_date) AS last_payment_paid_at,
                        MAX(p.updated_at) AS source_updated_at
                    FROM src.toast_payment_current p
                    JOIN src.toast_order_current o
                      ON o.order_guid = p.order_guid
                    WHERE p.business_date IN ({date_placeholders})
                      AND o.server_toast_guid IS NOT NULL
                      AND UPPER(COALESCE(p.payment_status, '')) IN {VALID_PAYMENT_STATUSES}
                    GROUP BY o.store_key, o.business_date, o.server_toast_guid
                ),
                selection_summary AS (
                    SELECT
                        o.store_key,
                        o.business_date,
                        o.server_toast_guid AS employee_toast_guid,
                        SUM(CASE WHEN COALESCE(s.voided, 0) = 0 THEN 1 ELSE 0 END)
                            AS selection_count,
                        SUM(CASE WHEN COALESCE(s.voided, 0) <> 0 THEN 1 ELSE 0 END)
                            AS voided_selection_count,
                        MAX(s.updated_at) AS source_updated_at
                    FROM src.toast_selection_current s
                    JOIN src.toast_order_current o
                      ON o.order_guid = s.order_guid
                    WHERE s.business_date IN ({date_placeholders})
                      AND o.server_toast_guid IS NOT NULL
                    GROUP BY o.store_key, o.business_date, o.server_toast_guid
                )
                INSERT INTO toast_employee_shift_review_current(
                    review_id, store_key, business_date, employee_toast_guid,
                    cena_employee_id, employee_display_name, employee_first_name,
                    employee_last_name, job_titles, time_entry_count,
                    open_time_entry_count, clock_in_first, clock_out_last,
                    regular_hours, overtime_hours, total_hours,
                    scheduled_shift_count, scheduled_in_first, scheduled_out_last,
                    order_count, check_count, zero_check_count, payment_count,
                    selection_count, voided_selection_count, check_subtotal_amount,
                    check_tax_amount, check_total_amount, payment_amount,
                    payment_tip_amount, total_collected_with_tips_amount,
                    cash_payment_amount, credit_payment_amount, other_payment_amount,
                    cash_tip_amount, credit_tip_amount, other_tip_amount,
                    non_cash_tip_amount, settlement_net_amount,
                    owed_to_employee_amount, owed_to_restaurant_amount,
                    settlement_formula, has_sales, has_clock, has_open_clock,
                    data_warnings_json, first_order_opened_at,
                    last_order_closed_at, first_payment_paid_at,
                    last_payment_paid_at, source_mirror_db, source_updated_at,
                    calculated_at
                )
                SELECT
                    ck_shift_review_id(ek.store_key, ek.business_date, ek.employee_toast_guid),
                    ek.store_key,
                    ek.business_date,
                    ek.employee_toast_guid,
                    id.cena_employee_id,
                    TRIM(COALESCE(NULLIF(te.chosen_name, ''), te.first_name, '') || ' ' || COALESCE(te.last_name, '')),
                    te.first_name,
                    te.last_name,
                    js.job_titles,
                    COALESCE(ts.time_entry_count, 0),
                    COALESCE(ts.open_time_entry_count, 0),
                    ts.clock_in_first,
                    ts.clock_out_last,
                    COALESCE(ts.regular_hours, 0),
                    COALESCE(ts.overtime_hours, 0),
                    COALESCE(ts.total_hours, 0),
                    COALESCE(ss.scheduled_shift_count, 0),
                    ss.scheduled_in_first,
                    ss.scheduled_out_last,
                    COALESCE(os.order_count, 0),
                    COALESCE(cs.check_count, 0),
                    COALESCE(cs.zero_check_count, 0),
                    COALESCE(ps.payment_count, 0),
                    COALESCE(sels.selection_count, 0),
                    COALESCE(sels.voided_selection_count, 0),
                    COALESCE(cs.check_subtotal_amount, 0),
                    COALESCE(cs.check_tax_amount, 0),
                    COALESCE(cs.check_total_amount, 0),
                    COALESCE(ps.payment_amount, 0),
                    COALESCE(ps.payment_tip_amount, 0),
                    COALESCE(ps.total_collected_with_tips_amount, 0),
                    COALESCE(ps.cash_payment_amount, 0),
                    COALESCE(ps.credit_payment_amount, 0),
                    COALESCE(ps.other_payment_amount, 0),
                    COALESCE(ps.cash_tip_amount, 0),
                    COALESCE(ps.credit_tip_amount, 0),
                    COALESCE(ps.other_tip_amount, 0),
                    COALESCE(ps.non_cash_tip_amount, 0),
                    ROUND(
                        COALESCE(ps.non_cash_tip_amount, 0)
                        + COALESCE(ps.cash_tip_amount, 0)
                        - COALESCE(ps.cash_payment_amount, 0),
                        2
                    ),
                    MAX(
                        ROUND(
                            COALESCE(ps.non_cash_tip_amount, 0)
                            + COALESCE(ps.cash_tip_amount, 0)
                            - COALESCE(ps.cash_payment_amount, 0),
                            2
                        ),
                        0
                    ),
                    MAX(
                        ROUND(
                            COALESCE(ps.cash_payment_amount, 0)
                            - COALESCE(ps.non_cash_tip_amount, 0)
                            - COALESCE(ps.cash_tip_amount, 0),
                            2
                        ),
                        0
                    ),
                    'non_cash_tip_amount + cash_tip_amount - cash_payment_amount',
                    CASE WHEN COALESCE(os.order_count, 0) > 0
                           OR COALESCE(ps.payment_count, 0) > 0
                         THEN 1 ELSE 0 END,
                    CASE WHEN COALESCE(ts.time_entry_count, 0) > 0 THEN 1 ELSE 0 END,
                    CASE WHEN COALESCE(ts.open_time_entry_count, 0) > 0 THEN 1 ELSE 0 END,
                    ck_shift_review_warnings(
                        id.cena_employee_id,
                        COALESCE(ts.time_entry_count, 0),
                        COALESCE(ts.open_time_entry_count, 0)
                    ),
                    os.first_order_opened_at,
                    os.last_order_closed_at,
                    ps.first_payment_paid_at,
                    ps.last_payment_paid_at,
                    ?,
                    MAX(
                        COALESCE(ts.source_updated_at, ''),
                        COALESCE(ss.source_updated_at, ''),
                        COALESCE(os.source_updated_at, ''),
                        COALESCE(cs.source_updated_at, ''),
                        COALESCE(ps.source_updated_at, ''),
                        COALESCE(sels.source_updated_at, '')
                    ),
                    ?
                FROM employee_keys ek
                LEFT JOIN src.employee_toast_identity_map id
                  ON id.store_key = ek.store_key
                 AND id.toast_employee_guid = ek.employee_toast_guid
                LEFT JOIN src.toast_employee_current te
                  ON te.store_key = ek.store_key
                 AND te.employee_guid = ek.employee_toast_guid
                LEFT JOIN time_summary ts
                  ON ts.store_key = ek.store_key
                 AND ts.business_date = ek.business_date
                 AND ts.employee_toast_guid = ek.employee_toast_guid
                LEFT JOIN scheduled_summary ss
                  ON ss.store_key = ek.store_key
                 AND ss.business_date = ek.business_date
                 AND ss.employee_toast_guid = ek.employee_toast_guid
                LEFT JOIN job_summary js
                  ON js.store_key = ek.store_key
                 AND js.business_date = ek.business_date
                 AND js.employee_toast_guid = ek.employee_toast_guid
                LEFT JOIN order_summary os
                  ON os.store_key = ek.store_key
                 AND os.business_date = ek.business_date
                 AND os.employee_toast_guid = ek.employee_toast_guid
                LEFT JOIN check_summary cs
                  ON cs.store_key = ek.store_key
                 AND cs.business_date = ek.business_date
                 AND cs.employee_toast_guid = ek.employee_toast_guid
                LEFT JOIN payment_summary ps
                  ON ps.store_key = ek.store_key
                 AND ps.business_date = ek.business_date
                 AND ps.employee_toast_guid = ek.employee_toast_guid
                LEFT JOIN selection_summary sels
                  ON sels.store_key = ek.store_key
                 AND sels.business_date = ek.business_date
                 AND sels.employee_toast_guid = ek.employee_toast_guid
                """,
                insert_params,
            )

            common_params = [*date_params]
            conn.execute(
                f"""
                INSERT INTO toast_employee_shift_review_time_entry(
                    review_id, store_key, business_date, employee_toast_guid,
                    time_entry_guid, job_guid, job_title, clock_in, clock_out,
                    regular_hours, overtime_hours, total_hours, deleted,
                    source_updated_at
                )
                SELECT
                    r.review_id, te.store_key, te.business_date, te.employee_guid,
                    te.time_entry_guid, te.job_guid, j.title, te.clock_in,
                    te.clock_out, te.regular_hours, te.overtime_hours,
                    te.total_hours, te.deleted, te.updated_at
                FROM src.toast_time_entry_current te
                JOIN toast_employee_shift_review_current r
                  ON r.store_key = te.store_key
                 AND r.business_date = te.business_date
                 AND r.employee_toast_guid = te.employee_guid
                LEFT JOIN src.toast_job_current j
                  ON j.store_key = te.store_key
                 AND j.job_guid = te.job_guid
                WHERE te.business_date IN ({date_placeholders})
                  AND te.employee_guid IS NOT NULL
                """,
                common_params,
            )
            conn.execute(
                f"""
                INSERT INTO toast_employee_shift_review_scheduled_shift(
                    review_id, store_key, business_date, employee_toast_guid,
                    shift_guid, job_guid, job_title, scheduled_in,
                    scheduled_out, deleted, source_updated_at
                )
                SELECT
                    r.review_id, sh.store_key, REPLACE(sh.business_date, '-', ''), sh.employee_guid,
                    sh.shift_guid, sh.job_guid, j.title, sh.scheduled_in,
                    sh.scheduled_out, sh.deleted, sh.updated_at
                FROM src.toast_shift_current sh
                JOIN toast_employee_shift_review_current r
                  ON r.store_key = sh.store_key
                 AND r.business_date = REPLACE(sh.business_date, '-', '')
                 AND r.employee_toast_guid = sh.employee_guid
                LEFT JOIN src.toast_job_current j
                  ON j.store_key = sh.store_key
                 AND j.job_guid = sh.job_guid
                WHERE REPLACE(sh.business_date, '-', '') IN ({date_placeholders})
                  AND sh.employee_guid IS NOT NULL
                """,
                common_params,
            )
            conn.execute(
                f"""
                INSERT INTO toast_employee_shift_review_order(
                    review_id, order_guid, store_key, business_date, source,
                    payment_status, approval_status, opened_date, closed_date,
                    paid_date, table_guid, table_name, source_updated_at
                )
                SELECT
                    r.review_id, o.order_guid, o.store_key, o.business_date,
                    o.source, o.payment_status, o.approval_status, o.opened_date,
                    o.closed_date, o.paid_date, o.table_guid, o.table_name,
                    o.updated_at
                FROM src.toast_order_current o
                JOIN toast_employee_shift_review_current r
                  ON r.store_key = o.store_key
                 AND r.business_date = o.business_date
                 AND r.employee_toast_guid = o.server_toast_guid
                WHERE o.business_date IN ({date_placeholders})
                  AND o.server_toast_guid IS NOT NULL
                """,
                common_params,
            )
            conn.execute(
                f"""
                INSERT INTO toast_employee_shift_review_check(
                    review_id, check_guid, order_guid, store_key, business_date,
                    display_number, payment_status, amount, tax_amount,
                    total_amount, opened_date, closed_date, paid_date, voided,
                    deleted, source_updated_at
                )
                SELECT
                    r.review_id, c.check_guid, c.order_guid, c.store_key,
                    c.business_date, c.display_number, c.payment_status,
                    c.amount, c.tax_amount, c.total_amount, c.opened_date,
                    c.closed_date, c.paid_date, c.voided, c.deleted,
                    c.updated_at
                FROM src.toast_check_current c
                JOIN src.toast_order_current o
                  ON o.order_guid = c.order_guid
                JOIN toast_employee_shift_review_current r
                  ON r.store_key = o.store_key
                 AND r.business_date = o.business_date
                 AND r.employee_toast_guid = o.server_toast_guid
                WHERE c.business_date IN ({date_placeholders})
                  AND o.server_toast_guid IS NOT NULL
                """,
                common_params,
            )
            conn.execute(
                f"""
                INSERT INTO toast_employee_shift_review_payment(
                    review_id, payment_guid, check_guid, order_guid, store_key,
                    business_date, payment_type, payment_status, amount,
                    tip_amount, paid_date, source_updated_at
                )
                SELECT
                    r.review_id, p.payment_guid, p.check_guid, p.order_guid,
                    p.store_key, p.business_date, p.payment_type,
                    p.payment_status, p.amount, p.tip_amount, p.paid_date,
                    p.updated_at
                FROM src.toast_payment_current p
                JOIN src.toast_order_current o
                  ON o.order_guid = p.order_guid
                JOIN toast_employee_shift_review_current r
                  ON r.store_key = o.store_key
                 AND r.business_date = o.business_date
                 AND r.employee_toast_guid = o.server_toast_guid
                WHERE p.business_date IN ({date_placeholders})
                  AND o.server_toast_guid IS NOT NULL
                """,
                common_params,
            )
            conn.execute(
                f"""
                INSERT INTO toast_employee_shift_review_selection(
                    review_id, selection_guid, check_guid, order_guid, store_key,
                    business_date, display_name, quantity, price, voided,
                    source_updated_at
                )
                SELECT
                    r.review_id, s.selection_guid, s.check_guid, s.order_guid,
                    s.store_key, s.business_date, s.display_name, s.quantity,
                    s.price, s.voided, s.updated_at
                FROM src.toast_selection_current s
                JOIN src.toast_order_current o
                  ON o.order_guid = s.order_guid
                JOIN toast_employee_shift_review_current r
                  ON r.store_key = o.store_key
                 AND r.business_date = o.business_date
                 AND r.employee_toast_guid = o.server_toast_guid
                WHERE s.business_date IN ({date_placeholders})
                  AND o.server_toast_guid IS NOT NULL
                """,
                common_params,
            )

            review_count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM toast_employee_shift_review_current
                WHERE business_date IN ({date_placeholders})
                """,
                date_params,
            ).fetchone()[0]
            conn.execute(
                """
                UPDATE toast_employee_shift_review_run_log
                SET finished_at = ?, ok = 1, review_count = ?
                WHERE id = ?
                """,
                (_utc_now(), int(review_count), run_id),
            )
            conn.commit()
        except Exception as exc:
            conn.execute(
                """
                UPDATE toast_employee_shift_review_run_log
                SET finished_at = ?, ok = 0, error = ?
                WHERE id = ?
                """,
                (_utc_now(), f"{type(exc).__name__}: {exc}", run_id),
            )
            conn.commit()
            raise

        return {
            "ok": True,
            "source_db": str(source_path),
            "review_db": str(review_path),
            "scope_start": scope_start,
            "scope_end": scope_end,
            "review_count": int(review_count),
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build local Toast employee shift-review projection DB."
    )
    parser.add_argument("--source-db", default=os.getenv("TOAST_WEBHOOK_DB") or DEFAULT_MIRROR_DB_PATH)
    parser.add_argument(
        "--review-db",
        default=os.getenv("TOAST_SHIFT_REVIEW_DB") or DEFAULT_REVIEW_DB_PATH,
    )
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument(
        "--business-date",
        action="append",
        default=[],
        help="Specific YYYYMMDD business date. Can be passed more than once.",
    )
    args = parser.parse_args()

    dates = list(dict.fromkeys(args.business_date or business_dates_for_backfill(args.days)))
    result = project_shift_reviews(
        source_db=args.source_db,
        review_db=args.review_db,
        dates=dates,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
