"""Tests for GET /cron/appdb-export (full app-DB snapshot for Cena's local
mirror, Sam 2026-06-10).

Covers:
  - fail-closed auth: no token env / missing / wrong -> 403
  - CENA_GATEWAY_TOKEN fallback works when APPDB_EXPORT_TOKEN unset
  - dedicated APPDB_EXPORT_TOKEN takes precedence (gateway token rejected)
  - payload is a valid gzipped sqlite file
  - scrub: credential/PII columns NULLed, one-time-secret rows deleted,
    business columns survive
"""
from __future__ import annotations

import gzip
import sqlite3

import pytest


def _make_source_db(path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT,
            email TEXT, phone TEXT, passcode_hash TEXT, password_hash TEXT,
            failed_attempts INTEGER NOT NULL DEFAULT 0);
        INSERT INTO users VALUES
            (1, 'Test User', 'u@example.com', '555-1111', 'HASH-A', 'HASH-B', 3);
        CREATE TABLE employee_setup_tokens (id INTEGER PRIMARY KEY, token TEXT);
        INSERT INTO employee_setup_tokens VALUES (1, 'one-time-secret');
        CREATE TABLE legal_company_structure (id INTEGER PRIMARY KEY, ein TEXT);
        INSERT INTO legal_company_structure VALUES (1, '12-3456789');
        CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT,
            total_amount REAL);
        INSERT INTO orders VALUES (1, 'delivered', 123.45);
        CREATE TABLE vendor_recent_orders (id INTEGER PRIMARY KEY, vendor TEXT,
            total_cents INTEGER, raw_body TEXT, from_addr TEXT, subject TEXT,
            customer_or_caterer TEXT);
        INSERT INTO vendor_recent_orders VALUES
            (1, 'webstaurant', 5000, 'RAW EMAIL BODY', 'orders@vendor.com',
             'Your order', 'Cena Copperfield');
        """
    )
    con.commit()
    con.close()


@pytest.fixture
def client(db_session, monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")
    monkeypatch.setenv("CENA_GATEWAY_TOKEN", "testtoken")
    monkeypatch.delenv("APPDB_EXPORT_TOKEN", raising=False)

    src = tmp_path / "prod_copy.sqlite"
    _make_source_db(str(src))

    from sqlalchemy.engine.url import make_url
    import types
    stub = types.SimpleNamespace(
        url=make_url("sqlite:///" + str(src).replace("\\", "/")))
    import app.web.appdb_export_routes as mod
    monkeypatch.setattr(mod, "engine", stub)

    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _get(client, token=None, header="X-Appdb-Token"):
    headers = {header: token} if token else {}
    return client.get("/cron/appdb-export", headers=headers)


def _open_payload(resp, tmp_path):
    raw = gzip.decompress(resp.data)
    out = tmp_path / "pulled.sqlite"
    out.write_bytes(raw)
    return sqlite3.connect(str(out))


def test_no_token_403(client):
    assert _get(client).status_code == 403


def test_wrong_token_403(client):
    assert _get(client, "nope").status_code == 403


def test_fail_closed_when_no_token_env(client, monkeypatch):
    monkeypatch.delenv("CENA_GATEWAY_TOKEN", raising=False)
    assert _get(client, "testtoken").status_code == 403


def test_dedicated_token_takes_precedence(client, monkeypatch):
    monkeypatch.setenv("APPDB_EXPORT_TOKEN", "dedicated")
    assert _get(client, "testtoken").status_code == 403
    assert _get(client, "dedicated").status_code == 200


def test_bearer_header_accepted(client):
    r = client.get("/cron/appdb-export",
                   headers={"Authorization": "Bearer testtoken"})
    assert r.status_code == 200


def test_export_is_scrubbed_sqlite(client, tmp_path):
    r = _get(client, "testtoken")
    assert r.status_code == 200
    assert r.mimetype == "application/gzip"
    assert int(r.headers["X-Appdb-Tables"]) >= 4
    con = _open_payload(r, tmp_path)
    try:
        email, phone, ph, pw, name, fa = con.execute(
            "SELECT email, phone, passcode_hash, password_hash, full_name, "
            "failed_attempts FROM users").fetchone()
        assert email is None and phone is None
        assert ph is None and pw is None
        assert name == "Test User"  # non-sensitive survives
        assert fa == 0  # NOT NULL column scrubbed to typed-zero, not NULL
        assert con.execute(
            "SELECT COUNT(*) FROM employee_setup_tokens").fetchone()[0] == 0
        assert con.execute(
            "SELECT ein FROM legal_company_structure").fetchone()[0] is None
        status, total = con.execute(
            "SELECT status, total_amount FROM orders").fetchone()
        assert status == "delivered" and total == 123.45
        # vendor email-content columns scrubbed; analytic columns survive.
        vendor, cents, body, addr, subj, cust = con.execute(
            "SELECT vendor, total_cents, raw_body, from_addr, subject, "
            "customer_or_caterer FROM vendor_recent_orders").fetchone()
        assert vendor == "webstaurant" and cents == 5000
        assert body is None and addr is None and subj is None and cust is None
    finally:
        con.close()
