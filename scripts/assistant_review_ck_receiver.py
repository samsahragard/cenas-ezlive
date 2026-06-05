"""CK-local receiver for in-app assistant review questions.

Run on Mini_IT13. Render's /assistant/ask posts blocked/unanswered questions
here so the durable approval queue lives on CK, not Render.

Environment:
  ASSISTANT_REVIEW_TOKEN   required token for POSTs
  ASSISTANT_REVIEW_DB      optional DB path; default:
                           C:\\Users\\sam\\cena-ai-assistant\\assistant_review.sqlite
  ASSISTANT_REVIEW_HOST    optional bind host; default 127.0.0.1
  ASSISTANT_REVIEW_PORT    optional port; default 8778
"""
from __future__ import annotations

import json
import os
import hashlib
import re
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_DB = r"C:\Users\sam\cena-ai-assistant\assistant_review.sqlite"
SCHEMA_FILE = Path(__file__).with_name("assistant_review_schema.sql")
POST_PATH = "/review/question"
LEGACY_POST_PATH = "/assistant/review-question"
ALLOWED_STATUSES = {"pending", "approved", "rejected", "needs_review", "archived"}
ALLOWED_RISKS = {"low", "normal", "high", "blocked"}
SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|api[_-]?key|password|passcode|pin|hash|salt|"
    r"phone|email|address|gps|lat|lng|longitude|latitude)",
    re.IGNORECASE,
)
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)\b("
    r"sk-[A-Za-z0-9_-]{12,}|"
    r"[A-Za-z0-9_./+-]{24,}\.[A-Za-z0-9_./+-]{12,}\.[A-Za-z0-9_./+-]{12,}|"
    r"(?:token|secret|api key|password|passcode|pin)\s*[:=]\s*\S+"
    r")"
)


def _db_path() -> Path:
    return Path(os.getenv("ASSISTANT_REVIEW_DB") or DEFAULT_DB)


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _normalize_question(question: str) -> str:
    return " ".join(question.casefold().strip().split())


def _question_hash(question: str) -> str:
    return hashlib.sha256(_normalize_question(question).encode("utf-8")).hexdigest()


def _stable_hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_text(value: str) -> str:
    return SENSITIVE_TEXT_RE.sub("[REDACTED]", value)


def _normalize_status(value: str | None) -> str:
    candidate = (value or "").strip().lower()
    return candidate if candidate in ALLOWED_STATUSES else "needs_review"


def _normalize_risk(value: str | None) -> str:
    candidate = (value or "").strip().lower()
    return candidate if candidate in ALLOWED_RISKS else "blocked"


def _sanitize(value):
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                clean[key] = "[REDACTED]"
            else:
                clean[key] = _sanitize(item)
        return clean
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _sensitivity_flags(row: dict) -> list[str]:
    haystack = json.dumps(row, ensure_ascii=False).casefold()
    checks = {
        "secret_or_token": ["token", "secret", "api key", "password", "passcode", "pin"],
        "customer_pii": ["customer", "phone", "email", "address"],
        "raw_gps": ["gps", "latitude", "longitude", " lat", " lng"],
        "pay_or_sales": ["payroll", "pay rate", "eligible_sales", "cashsales", "noncashsales", "sales"],
    }
    return [
        flag
        for flag, needles in checks.items()
        if any(needle in haystack for needle in needles)
    ]


def _risk_level(flags: list[str], reason: str) -> str:
    if "secret_or_token" in flags or "raw_gps" in flags or "customer_pii" in flags:
        return "high"
    if "pay_or_sales" in flags or "sensitive" in reason or "operational" in reason:
        return "medium"
    return "low"


def _init_db() -> None:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_FILE.read_text(encoding="utf-8")
    with sqlite3.connect(path) as con:
        con.executescript(schema)
        con.commit()


def _id_is_integer_pk(con: sqlite3.Connection, table: str) -> bool:
    for col in con.execute(f"PRAGMA table_info({table})").fetchall():
        if col[1] == "id":
            return str(col[2] or "").upper() == "INTEGER" and int(col[5] or 0) == 1
    return False


def _insert_row(
    con: sqlite3.Connection,
    table: str,
    text_id: str,
    columns: list[str],
    values: list,
) -> str:
    if _id_is_integer_pk(con, table):
        placeholders = ", ".join("?" for _ in columns)
        con.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        return str(con.execute("SELECT last_insert_rowid()").fetchone()[0])

    placeholders = ", ".join("?" for _ in ["id", *columns])
    con.execute(
        f"INSERT OR REPLACE INTO {table} (id, {', '.join(columns)}) VALUES ({placeholders})",
        [text_id, *values],
    )
    return text_id


def _save_question(row: dict) -> str:
    _init_db()
    requested_qid = str(row.get("id") or "")
    if not requested_qid:
        raise ValueError("missing id")
    principal = row.get("principal") or {}
    raw_question = str(row.get("question") or "")
    question = _redact_text(raw_question)
    received_at = _now_iso()
    sensitivity_flags = _sensitivity_flags(row)
    reason = str(row.get("reason") or "blocked_review")
    role = str(row.get("role") or principal.get("role") or "unknown")
    store_key = str(row.get("store_key") or principal.get("current_store") or "")
    if not store_key:
        stores = principal.get("store_slugs") or []
        store_key = str(stores[0]) if stores else ""
    principal_hash = _stable_hash({
        "kind": principal.get("kind"),
        "role": role,
        "principal_id": principal.get("principal_id"),
        "display_name": principal.get("display_name"),
    })
    scope_hash = _stable_hash({
        "role": role,
        "store_key": store_key,
        "stores": principal.get("store_slugs") or [],
        "required_permission": row.get("required_permission"),
    })
    question_hash = _question_hash(raw_question)
    status = _normalize_status(str(row.get("status") or ""))
    risk_level = _normalize_risk(str(row.get("risk_level") or ""))
    model_key = str(row.get("model_key") or "review_queue")
    tool_name = str(row.get("tool_name") or "assistant_review")
    delivery_target = str(row.get("delivery_target") or _db_path().name)
    risk_flags_hash = _stable_hash({
        "risk_level": risk_level,
        "sensitivity_flags": sensitivity_flags,
        "reason": reason,
    })
    with sqlite3.connect(_db_path()) as con:
        con.execute("PRAGMA foreign_keys = ON")
        question_ref_is_int = _id_is_integer_pk(con, "assistant_question")
        question_columns = [
            "question_hash",
            "question_summary_redacted",
            "status",
            "requested_by_hash",
            "scope_role",
            "scope_store_key",
            "scope_hash",
            "risk_level",
            "created_at",
            "updated_at",
        ]
        question_values = [
            question_hash,
            question[:500],
            status,
            principal_hash,
            role,
            store_key,
            scope_hash,
            risk_level or _risk_level(sensitivity_flags, reason),
            str(row.get("created_at") or received_at),
            received_at,
        ]
        try:
            qid = _insert_row(
                con,
                "assistant_question",
                requested_qid,
                question_columns,
                question_values,
            )
        except sqlite3.IntegrityError as exc:
            if "assistant_question.question_hash" not in str(exc):
                raise
            existing = con.execute(
                "SELECT id FROM assistant_question WHERE question_hash = ?",
                (question_hash,),
            ).fetchone()
            if not existing:
                raise
            qid = str(existing[0])
            question_pk = int(qid) if question_ref_is_int else qid
            con.execute(
                """
                UPDATE assistant_question
                   SET question_summary_redacted = ?,
                       status = ?,
                       requested_by_hash = ?,
                       scope_role = ?,
                       scope_store_key = ?,
                       scope_hash = ?,
                       risk_level = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                [
                    question[:500],
                    status,
                    principal_hash,
                    role,
                    store_key,
                    scope_hash,
                    risk_level or _risk_level(sensitivity_flags, reason),
                    received_at,
                    question_pk,
                ],
            )
        question_fk = int(qid) if question_ref_is_int else qid
        con.execute("DELETE FROM assistant_principal_snapshot WHERE question_id = ?", (question_fk,))
        _insert_row(
            con,
            "assistant_principal_snapshot",
            qid + ":principal",
            [
                "question_id",
                "principal_hash",
                "role",
                "store_key",
                "permission_level",
                "scope_hash",
                "captured_at",
            ],
            [
                question_fk,
                principal_hash,
                role,
                store_key,
                str(principal.get("permission_level") or role),
                scope_hash,
                received_at,
            ],
        )
        _insert_row(
            con,
            "assistant_review_decision",
            qid + ":review",
            [
                "question_id",
                "decision",
                "status",
                "reviewer_hash",
                "reason_code",
                "notes_redacted",
                "decided_at",
            ],
            [
                question_fk,
                "hold",
                "open",
                None,
                reason,
                None,
                received_at,
            ],
        )
        _insert_row(
            con,
            "assistant_model_audit",
            qid + ":model",
            [
                "question_id",
                "model_key_hash",
                "prompt_hash",
                "response_hash",
                "status",
                "risk_flags_hash",
                "reviewed_by_hash",
                "created_at",
            ],
            [
                question_fk,
                _stable_hash(model_key),
                _stable_hash({"question_hash": question_hash, "scope_hash": scope_hash}),
                _stable_hash("blocked_for_review"),
                "blocked",
                risk_flags_hash,
                None,
                received_at,
            ],
        )
        _insert_row(
            con,
            "assistant_delivery_attempt",
            qid + ":delivery:" + received_at,
            [
                "question_id",
                "tool_name_hash",
                "status",
                "delivery_target_hash",
                "attempt_count",
                "last_error_code",
                "created_at",
                "updated_at",
            ],
            [
                question_fk,
                _stable_hash(row.get("origin") or "render_to_ck"),
                "blocked",
                _stable_hash(delivery_target),
                int(row.get("retry_count") or 0),
                None,
                received_at,
                received_at,
            ],
        )
        con.commit()
    return qid


def _row_counts() -> dict[str, int]:
    _init_db()
    tables = [
        "assistant_question",
        "assistant_principal_snapshot",
        "assistant_review_decision",
        "assistant_model_audit",
        "assistant_delivery_attempt",
        "assistant_policy_rule",
        "assistant_tool_catalog_snapshot",
    ]
    with sqlite3.connect(_db_path()) as con:
        return {
            table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "CenasAssistantReview/1.0"

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        expected = os.getenv("ASSISTANT_REVIEW_TOKEN", "").strip()
        if not expected:
            return False
        auth = self.headers.get("Authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        token = token or self.headers.get("X-Ai-Assistant-Token", "").strip()
        return token == expected

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/healthz":
            self._json(404, {"ok": False, "error": "not_found"})
            return
        self._json(200, {"ok": True, "db": str(_db_path()), "row_counts": _row_counts()})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {POST_PATH, LEGACY_POST_PATH}:
            self._json(404, {"ok": False, "error": "not_found"})
            return
        if not self._authorized():
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 1024 * 256:
                self._json(400, {"ok": False, "error": "bad_length"})
                return
            row = json.loads(self.rfile.read(length).decode("utf-8"))
            qid = _save_question(row)
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        principal = row.get("principal") or {}
        role = str(row.get("role") or principal.get("role") or "unknown")
        store_key = str(row.get("store_key") or principal.get("current_store") or "")
        if not store_key:
            stores = principal.get("store_slugs") or []
            store_key = str(stores[0]) if stores else ""
        raw_question = str(row.get("question") or "")
        principal_hash = _stable_hash({
            "kind": principal.get("kind"),
            "role": role,
            "principal_id": principal.get("principal_id"),
            "display_name": principal.get("display_name"),
        })
        self._json(200, {
            "ok": True,
            "question_id": qid,
            "ck_question_id": qid,
            "question_hash": _question_hash(raw_question),
            "principal_hash": principal_hash,
            "role": role,
            "store_key": store_key,
            "risk_level": _normalize_risk(str(row.get("risk_level") or "")),
            "status": _normalize_status(str(row.get("status") or "")),
            "delivery_status": "blocked",
        })


def main() -> None:
    _init_db()
    host = os.getenv("ASSISTANT_REVIEW_HOST") or "127.0.0.1"
    port = int(os.getenv("ASSISTANT_REVIEW_PORT") or "8778")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"assistant review receiver listening on http://{host}:{port}")
    print(f"db: {_db_path()}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
