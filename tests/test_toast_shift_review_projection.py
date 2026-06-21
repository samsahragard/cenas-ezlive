import sqlite3

from scripts.toast_shift_review_projection import project_shift_reviews


def _init_source_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE employee_toast_identity_map (
            store_key TEXT,
            toast_employee_guid TEXT,
            cena_employee_id INTEGER,
            source TEXT,
            verified INTEGER,
            confidence REAL,
            first_seen TEXT,
            last_seen TEXT
        );
        CREATE TABLE toast_employee_current (
            store_key TEXT,
            employee_guid TEXT,
            external_employee_id TEXT,
            first_name TEXT,
            chosen_name TEXT,
            last_name TEXT,
            email TEXT,
            phone TEXT,
            deleted INTEGER,
            employee_json TEXT,
            source TEXT,
            updated_at TEXT
        );
        CREATE TABLE toast_job_current (
            store_key TEXT,
            job_guid TEXT,
            title TEXT,
            wage_type TEXT,
            deleted INTEGER,
            job_json TEXT,
            source TEXT,
            updated_at TEXT
        );
        CREATE TABLE toast_time_entry_current (
            store_key TEXT,
            time_entry_guid TEXT,
            employee_guid TEXT,
            job_guid TEXT,
            business_date TEXT,
            clock_in TEXT,
            clock_out TEXT,
            regular_hours REAL,
            overtime_hours REAL,
            total_hours REAL,
            deleted INTEGER,
            time_entry_json TEXT,
            source TEXT,
            updated_at TEXT
        );
        CREATE TABLE toast_shift_current (
            store_key TEXT,
            shift_guid TEXT,
            employee_guid TEXT,
            job_guid TEXT,
            business_date TEXT,
            scheduled_in TEXT,
            scheduled_out TEXT,
            deleted INTEGER,
            shift_json TEXT,
            source TEXT,
            updated_at TEXT
        );
        CREATE TABLE toast_order_current (
            order_guid TEXT,
            event_guid TEXT,
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
            order_json TEXT,
            updated_at TEXT
        );
        CREATE TABLE toast_check_current (
            check_guid TEXT,
            order_guid TEXT,
            event_guid TEXT,
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
            voided INTEGER,
            deleted INTEGER,
            check_json TEXT,
            updated_at TEXT
        );
        CREATE TABLE toast_payment_current (
            payment_guid TEXT,
            check_guid TEXT,
            order_guid TEXT,
            event_guid TEXT,
            store_key TEXT,
            business_date TEXT,
            payment_type TEXT,
            payment_status TEXT,
            amount REAL,
            tip_amount REAL,
            paid_date TEXT,
            payment_json TEXT,
            updated_at TEXT
        );
        CREATE TABLE toast_selection_current (
            selection_guid TEXT,
            check_guid TEXT,
            order_guid TEXT,
            event_guid TEXT,
            store_key TEXT,
            business_date TEXT,
            display_name TEXT,
            quantity REAL,
            price REAL,
            voided INTEGER,
            selection_json TEXT,
            updated_at TEXT
        );
        """
    )
    return conn


def test_shift_review_projection_calculates_settlement_both_directions(tmp_path):
    source_db = tmp_path / "toast_webhook.sqlite"
    review_db = tmp_path / "toast_shift_reviews.sqlite"
    conn = _init_source_db(source_db)
    conn.executemany(
        """
        INSERT INTO employee_toast_identity_map
        VALUES (?, ?, ?, 'test', 1, 1.0, '2026-06-20T00:00:00Z', '2026-06-20T00:00:00Z')
        """,
        [
            ("copperfield", "emp-owed", 63),
            ("copperfield", "emp-owes", 64),
        ],
    )
    conn.executemany(
        """
        INSERT INTO toast_employee_current
        VALUES (?, ?, NULL, ?, NULL, ?, NULL, NULL, 0, '{}', 'test', '2026-06-20T00:00:00Z')
        """,
        [
            ("copperfield", "emp-owed", "Alexa", "Rodriguez"),
            ("copperfield", "emp-owes", "Cash", "Heavy"),
        ],
    )
    conn.execute(
        """
        INSERT INTO toast_job_current
        VALUES ('copperfield', 'job-server', 'Server', NULL, 0, '{}', 'test', '2026-06-20T00:00:00Z')
        """
    )
    conn.executemany(
        """
        INSERT INTO toast_time_entry_current
        VALUES (?, ?, ?, 'job-server', '20260620', '2026-06-20T21:00:00.000+0000',
                '2026-06-21T03:00:00.000+0000', 6.0, 0.0, 6.0, 0, '{}', 'test',
                '2026-06-21T03:01:00Z')
        """,
        [
            ("copperfield", "time-1", "emp-owed"),
            ("copperfield", "time-2", "emp-owes"),
        ],
    )
    conn.execute(
        """
        INSERT INTO toast_shift_current
        VALUES ('copperfield', 'shift-1', 'emp-owed', 'job-server', '2026-06-20',
                '2026-06-20T21:00:00.000+0000', '2026-06-21T03:00:00.000+0000',
                0, '{}', 'test', '2026-06-20T10:00:00Z')
        """
    )
    conn.executemany(
        """
        INSERT INTO toast_order_current
        VALUES (?, 'event-1', 'rest-1', 'copperfield', '20260620', 'In Store',
                NULL, 'APPROVED', '2026-06-20T21:10:00.000+0000',
                '2026-06-20T22:00:00.000+0000', '2026-06-20T22:00:00.000+0000',
                '2026-06-20T22:00:00.000+0000', ?, NULL, NULL, '{}',
                '2026-06-20T22:01:00Z')
        """,
        [
            ("order-owed", "emp-owed"),
            ("order-owes", "emp-owes"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO toast_check_current
        VALUES (?, ?, 'event-1', 'copperfield', '20260620', ?, 'CLOSED',
                ?, ?, ?, '2026-06-20T21:10:00.000+0000',
                '2026-06-20T22:00:00.000+0000', '2026-06-20T22:00:00.000+0000',
                '2026-06-20T22:00:00.000+0000', 0, 0, '{}',
                '2026-06-20T22:01:00Z')
        """,
        [
            ("check-owed", "order-owed", "10", 100.0, 130.0, 10.0),
            ("check-owes", "order-owes", "11", 100.0, 110.0, 10.0),
        ],
    )
    conn.executemany(
        """
        INSERT INTO toast_payment_current
        VALUES (?, ?, ?, 'event-1', 'copperfield', '20260620', ?, 'CAPTURED',
                ?, ?, '2026-06-20T22:00:00.000+0000', '{}', '2026-06-20T22:01:00Z')
        """,
        [
            ("pay-credit", "check-owed", "order-owed", "CREDIT", 100.0, 20.0),
            ("pay-cash-offset", "check-owed", "order-owed", "CASH", 15.0, 0.0),
            ("pay-credit-small", "check-owes", "order-owes", "CREDIT", 50.0, 10.0),
            ("pay-cash-large", "check-owes", "order-owes", "CASH", 50.0, 0.0),
        ],
    )
    conn.executemany(
        """
        INSERT INTO toast_selection_current
        VALUES (?, ?, ?, 'event-1', 'copperfield', '20260620', ?, 1.0,
                ?, ?, '{}', '2026-06-20T22:01:00Z')
        """,
        [
            ("sel-1", "check-owed", "order-owed", "Taco", 10.0, 0),
            ("sel-2", "check-owed", "order-owed", "Void Item", 1.0, 1),
            ("sel-3", "check-owes", "order-owes", "Plate", 10.0, 0),
        ],
    )
    conn.commit()
    conn.close()

    result = project_shift_reviews(
        source_db=source_db,
        review_db=review_db,
        dates=["20260620"],
    )
    assert result["ok"] is True
    assert result["review_count"] == 2

    out = sqlite3.connect(review_db)
    out.row_factory = sqlite3.Row
    owed = out.execute(
        """
        SELECT * FROM toast_employee_shift_review_current
        WHERE employee_toast_guid = 'emp-owed'
        """
    ).fetchone()
    owes = out.execute(
        """
        SELECT * FROM toast_employee_shift_review_current
        WHERE employee_toast_guid = 'emp-owes'
        """
    ).fetchone()

    assert owed["owed_to_employee_amount"] == 5.0
    assert owed["owed_to_restaurant_amount"] == 0.0
    assert owed["credit_tip_amount"] == 20.0
    assert owed["cash_payment_amount"] == 15.0
    assert owed["selection_count"] == 1
    assert owed["voided_selection_count"] == 1
    assert owed["scheduled_shift_count"] == 1

    assert owes["owed_to_employee_amount"] == 0.0
    assert owes["owed_to_restaurant_amount"] == 40.0
    assert owes["credit_tip_amount"] == 10.0
    assert owes["cash_payment_amount"] == 50.0

    assert (
        out.execute(
            """
            SELECT COUNT(*) FROM toast_employee_shift_review_payment
            WHERE review_id = ?
            """,
            (owed["review_id"],),
        ).fetchone()[0]
        == 2
    )
    assert (
        out.execute(
            """
            SELECT COUNT(*) FROM toast_employee_shift_review_scheduled_shift
            WHERE review_id = ? AND business_date = '20260620'
            """,
            (owed["review_id"],),
        ).fetchone()[0]
        == 1
    )
    out.close()
