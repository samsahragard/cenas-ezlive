"""Isolated READ-ONLY full app-DB snapshot export (Sam 2026-06-10: finish the
appdb live-mirror lane - Cena's local appdb snapshot was still fed by the stale
dev_local.db; the CENA_L3_SRC_APPDB hook existed but nothing produced a live file).

Serves ONE gzipped SQLite file: a consistent `VACUUM INTO` copy of the live prod
DB with credential/PII columns scrubbed BEFORE any byte leaves the box
(sanitize-by-construction; driverdc precedent). Consumer: CK's pull_appdb_live.py
-> C:\\Users\\sam\\cena-appdb\\_live\\appdb_live.sqlite -> env CENA_L3_SRC_APPDB ->
the L3 snapshot refresh (cena_sql_executor.refresh_snapshots). The pulled file is
a LOCAL-ONLY raw operational mirror per the cena-l3data catalog data_policy.

ISOLATION (Sam #3178 pattern): imports ONLY stdlib + flask + app.db. No models,
never driver_system.

AUTH (aick #3182 fail-closed): dedicated APPDB_EXPORT_TOKEN when set; otherwise
falls back to CENA_GATEWAY_TOKEN (which CK already holds) so the lane works
without a new Render env var. Neither set -> 403 always (never fail-open).
Hardening later = set APPDB_EXPORT_TOKEN; no code change needed.

SCRUB LIST (mirrors the OQ-3 read denylist in cena.py, so the mirror never
contains what Cena is forbidden to read): users/drivers/employees contact PII +
credential columns NULLed; access_request.temp_passcode_one_shot NULLed;
legal_company_structure.ein NULLed; employee_setup_tokens + employee_sms_codes
rows deleted outright; backstop NULLs password_hash/passcode_hash/
temp_passcode_one_shot wherever else they appear. A post-scrub VACUUM rewrites
the file so scrubbed values do not survive in free pages.
"""
import gzip
import hashlib
import hmac
import io
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, jsonify, request

from app.db import engine

appdb_export_bp = Blueprint("appdb_export", __name__)

# Explicit per-table scrub map; checked against PRAGMA table_info so schema
# drift never breaks the export (missing table/column = skipped, not fatal).
_SCRUB_COLUMNS = {
    "users": ["email", "phone", "address", "passcode_hash", "password_hash",
              "failed_attempts", "lockout_until"],
    "drivers": ["email", "phone", "address", "passcode_hash", "password_hash",
                "failed_attempts", "lockout_until"],
    "employees": ["email", "phone", "address", "passcode_hash"],
    "access_request": ["temp_passcode_one_shot"],
    "legal_company_structure": ["ein"],
    # vendor_recent_orders is parsed from inbound vendor emails; the raw email
    # body/sender/subject can carry arbitrary forwarded PII. Drop them at the
    # source so they never reach the local reasoning mirror (sanitize-by-
    # construction; the analytic columns vendor/store_scope/total_cents/
    # placed_at/status/items_json stay).
    "vendor_recent_orders": ["raw_body", "from_addr", "subject",
                             "source_email_mid", "customer_or_caterer"],
}
# Whole-table row deletes (one-time secrets; useless in a mirror).
_SCRUB_DROP_ROWS = ("employee_setup_tokens", "employee_sms_codes")
# Backstop: these exact column names get NULLed in EVERY table.
_SCRUB_ANYWHERE = {"password_hash", "passcode_hash", "temp_passcode_one_shot"}

_MAX_EXPORT_BYTES = 250 * 1024 * 1024  # sanity ceiling on the gz payload


def _extract_token():
    """Authorization: Bearer <t> -> X-Appdb-Token header -> ?token= query."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Appdb-Token") or request.args.get("token")


def _expected_token():
    dedicated = (os.getenv("APPDB_EXPORT_TOKEN") or "").strip()
    if dedicated:
        return dedicated
    return (os.getenv("CENA_GATEWAY_TOKEN") or "").strip()


def _blank_for(col_type, notnull):
    """Scrub value: NULL when the column allows it, else a typed zero so a
    NOT NULL constraint (e.g. drivers.failed_attempts INTEGER NOT NULL) can't
    abort the scrub. Affinity by SQLite's type-name rules."""
    if not notnull:
        return "NULL"
    t = (col_type or "").upper()
    if "INT" in t or "REAL" in t or "FLOA" in t or "DOUB" in t or "NUM" in t \
            or "DEC" in t:
        return "0"
    return "''"


def _scrub(con):
    """Neutralize credential/PII columns and drop one-time-secret rows in the
    COPY. Returns {table: what_was_scrubbed} for the response header."""
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    report = {}
    for t in _SCRUB_DROP_ROWS:
        if t in tables:
            con.execute(f'DELETE FROM "{t}"')
            report[t] = ["<rows deleted>"]
    for t in sorted(tables):
        if t.startswith("sqlite_"):
            continue
        info = list(con.execute(f'PRAGMA table_info("{t}")'))  # cid,name,type,notnull,...
        want = set(_SCRUB_COLUMNS.get(t, ())) | _SCRUB_ANYWHERE
        sets, hit = [], []
        for _cid, name, col_type, notnull, *_ in info:
            if name in want:
                sets.append(f'"{name}"={_blank_for(col_type, notnull)}')
                hit.append(name)
        if sets:
            con.execute(f'UPDATE "{t}" SET ' + ", ".join(sets))
            report.setdefault(t, []).extend(hit)
    con.commit()
    return report


@appdb_export_bp.route("/cron/appdb-export", methods=["GET"])
def appdb_export():
    # FAIL-CLOSED (aick #3182): unset/empty expected token MUST 403.
    expected = _expected_token()
    got = _extract_token() or ""
    if not expected or not hmac.compare_digest(got, expected):
        abort(403)

    if engine is None or engine.url.get_backend_name() != "sqlite":
        return jsonify({"ok": False,
                        "error": "appdb export supports sqlite only"}), 501
    src = engine.url.database
    if not src or not os.path.exists(src):
        return jsonify({"ok": False,
                        "error": "prod db file not found"}), 500

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tmp_db = os.path.join(tempfile.gettempdir(),
                          f"appdb_export_{os.getpid()}_{ts}.sqlite")
    stage = "init"
    try:
        # Online-consistent snapshot via the SQLite backup API - works on a
        # live WAL database under writers (no VACUUM-in-transaction pitfall,
        # no bound-param VACUUM INTO quirk). busy_timeout rides out brief locks.
        stage = "snapshot"
        src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
        dst_con = sqlite3.connect(tmp_db)
        try:
            src_con.execute("PRAGMA busy_timeout=60000")
            src_con.backup(dst_con)
        finally:
            dst_con.close()
            src_con.close()

        stage = "scrub"
        con = sqlite3.connect(tmp_db)
        try:
            scrub_report = _scrub(con)
            con.execute("VACUUM")  # drop scrubbed values from free pages
            table_count = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        finally:
            con.close()

        stage = "gzip"
        buf = io.BytesIO()
        with open(tmp_db, "rb") as f, \
                gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            shutil.copyfileobj(f, gz)
        data = buf.getvalue()
    except Exception as e:  # noqa: BLE001 - report the failing stage to the puller
        return jsonify({"ok": False,
                        "error": f"export failed at {stage}: "
                                 f"{type(e).__name__}: {e}"}), 500
    finally:
        try:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)
        except OSError:
            pass

    if len(data) > _MAX_EXPORT_BYTES:
        return jsonify({"ok": False,
                        "error": "export exceeds size ceiling"}), 507

    resp = Response(data, mimetype="application/gzip")
    resp.headers["Content-Disposition"] = (
        f"attachment; filename=appdb_{ts}.sqlite.gz")
    resp.headers["X-Appdb-Generated"] = ts
    resp.headers["X-Appdb-Tables"] = str(table_count)
    resp.headers["X-Appdb-Sha256"] = hashlib.sha256(data).hexdigest()
    resp.headers["X-Appdb-Scrubbed"] = ",".join(sorted(scrub_report))
    return resp
