"""CK-local runtime for the permission-scoped in-app assistant.

Run on Mini_IT13. The web app sends the authenticated principal context and
question here; CK does model calls and durable review storage locally.

Environment:
  ASSISTANT_RUNTIME_TOKEN        required token for /assistant/answer
  ASSISTANT_REVIEW_DB            optional DB path; defaults to CK review DB
  ASSISTANT_RUNTIME_HOSTS        optional comma-separated bind hosts
  ASSISTANT_RUNTIME_HOST         optional single bind host; default 127.0.0.1
  ASSISTANT_RUNTIME_PORT         optional port; default 8782
  ANTHROPIC_API_KEY              optional Sonnet key
  ANTHROPIC_API_KEY_FILE         optional file path for Sonnet key
  GEMINI_API_KEY                 optional Gemini key
  GEMINI_API_KEY_FILE            optional file path for Gemini key
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from scripts import assistant_review_ck_receiver as review_receiver
except ImportError:  # pragma: no cover - allows running from scripts dir
    import assistant_review_ck_receiver as review_receiver  # type: ignore


log = logging.getLogger(__name__)

ANSWER_PATH = "/assistant/answer"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_MAX_QUESTION_CHARS = 2000
_REVIEW_STATUS = "needs_review"
_SECRET_DEFAULTS = {
    "ANTHROPIC_API_KEY": [
        r"C:\Users\sam\cena-secrets\anthropic_api_key.txt",
        r"C:\Users\sam\.openclaw\.secrets\anthropic_api_key.txt",
    ],
    "GEMINI_API_KEY": [
        r"C:\Users\sam\cena-secrets\gemini_api_key.txt",
        r"C:\Users\sam\cena\.secrets\gemini_api_key.txt",
        r"C:\Users\sam\cena-secrets\google_api_key.txt",
    ],
}
_SENSITIVE_RE = re.compile(
    r"\b("
    r"password|passcode|token|secret|api key|credential|pin|"
    r"phone|email|address|customer|"
    r"wage|payroll|pay rate|hourly rate|peer pay|"
    r"sales|revenue|eligible_sales|cashsales|noncashsales|guid|"
    r"all employees|all drivers|all stores"
    r")\b",
    re.IGNORECASE,
)
_DATA_TOOL_RE = re.compile(
    r"\b("
    r"how many|count|total|report|summary|list|show me|who|which|"
    r"order|orders|driver|drivers|employee|employees|staff|team|"
    r"schedule|shift|roster|attendance|incident|write up|"
    r"tip|tips|labor|inventory|vendor|customer|ezcater|catering|"
    r"late|tracking|delivery|deliveries|pay|bonus|fee|fees"
    r")\b",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _read_secret(env_name: str) -> str:
    value = (os.getenv(env_name) or "").strip()
    if value:
        return value
    file_value = (os.getenv(env_name + "_FILE") or "").strip()
    candidates = [file_value] if file_value else []
    candidates.extend(_SECRET_DEFAULTS.get(env_name, []))
    for raw_path in candidates:
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            if path.exists():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            continue
    return ""


def _role(principal: dict) -> str:
    return str(principal.get("role") or "unknown")


def _can_ask_personal(principal: dict) -> bool:
    if principal.get("can_ask_personal") is True:
        return True
    role = _role(principal)
    permissions = set(principal.get("permissions") or [])
    return role in {"partner", "driver", "employee"} or bool(
        {"ai.ask_claude", "ai.ask_claude_personal"} & permissions
    )


def _can_ask_operational(principal: dict) -> bool:
    if principal.get("can_ask_operational") is True:
        return True
    role = _role(principal)
    permissions = set(principal.get("permissions") or [])
    return role == "partner" or "ai.ask_claude" in permissions


def _review_risk_level(reason: str | None) -> str:
    reason_text = (reason or "").casefold()
    if any(term in reason_text for term in ("sensitive", "operational", "data", "permission")):
        return "blocked"
    return "normal"


def _should_queue(question: str, principal: dict) -> tuple[bool, str, str | None]:
    if str(principal.get("kind") or "") == "anonymous":
        return True, "not_authenticated", "ai.ask_claude_personal"
    if not _can_ask_personal(principal):
        return True, "missing_ai_permission", "ai.ask_claude_personal"
    if _SENSITIVE_RE.search(question):
        needed = "ai.ask_claude"
        if not _can_ask_operational(principal):
            return True, "sensitive_or_operational_question_requires_higher_permission", needed
        return True, "sensitive_or_operational_question_needs_approved_tool", needed
    if _DATA_TOOL_RE.search(question):
        needed = "ai.ask_claude"
        if not _can_ask_operational(principal):
            return True, "data_question_requires_higher_permission", needed
        return True, "data_question_needs_approved_tool", needed
    return False, "", None


def _queue_for_review(question: str, principal: dict, reason: str,
                      required_permission: str | None, source: str) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "created_at": _now_iso(),
        "status": _REVIEW_STATUS,
        "risk_level": _review_risk_level(reason),
        "question": question,
        "reason": reason,
        "required_permission": required_permission,
        "role": _role(principal),
        "store_key": principal.get("current_store") or ((principal.get("store_slugs") or [None])[0]),
        "model_key": "ck_runtime_review_queue",
        "tool_name": required_permission or "assistant.general_help",
        "delivery_target": "ck_assistant_review",
        "origin": source or "ck_runtime",
        "principal": principal,
    }
    qid = review_receiver._save_question(row)
    row["ck_question_id"] = qid
    return row


def _system_prompt(principal: dict) -> str:
    return (
        "You are the Cenas Kitchen in-app assistant running on CK. Answer only "
        "within the current user's role and permissions. You do not reveal "
        "secrets, passcodes, tokens, customer PII, unauthorized payroll, raw "
        "peer pay, sales internals, GUIDs, or cross-store data. This first "
        "version has no approved active database tools yet, so answer only "
        "general app-help or policy questions. If a question needs operational "
        "data, say it needs Sam review and do not guess.\n\n"
        f"Current session: role={_role(principal)}, kind={principal.get('kind')}, "
        f"stores={principal.get('store_slugs')}, path={principal.get('path')}."
    )


def _anthropic_answer(question: str, principal: dict) -> tuple[str | None, str | None]:
    key = _read_secret("ANTHROPIC_API_KEY")
    if not key:
        return None, None
    try:
        import anthropic  # type: ignore[import]
    except ImportError:
        log.warning("assistant runtime: anthropic package not installed")
        return None, None

    model = os.getenv("AI_ASSISTANT_ANTHROPIC_MODEL", _DEFAULT_MODEL)
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model,
        max_tokens=800,
        temperature=0.2,
        system=_system_prompt(principal),
        messages=[{"role": "user", "content": question}],
    )
    text = "".join(
        block.text for block in getattr(msg, "content", [])
        if getattr(block, "type", None) == "text"
    ).strip()
    return text or None, model


def _gemini_answer(question: str, principal: dict) -> tuple[str | None, str | None]:
    key = _read_secret("GEMINI_API_KEY")
    if not key:
        return None, None
    try:
        from google import genai  # type: ignore[import]
    except ImportError:
        log.warning("assistant runtime: google-genai package not installed")
        return None, None

    model = os.getenv("AI_ASSISTANT_GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=key)
    prompt = _system_prompt(principal) + "\n\nUser question:\n" + question
    resp = client.models.generate_content(model=model, contents=prompt)
    text = (getattr(resp, "text", None) or "").strip()
    return text or None, model


def _answer(payload: dict) -> tuple[dict, int]:
    question = str(payload.get("question") or "").strip()[:_MAX_QUESTION_CHARS]
    principal = payload.get("principal") or {}
    source = str(payload.get("source") or "cenas_app")
    if not question:
        return {"ok": False, "error": "question required"}, 400

    should_queue, reason, required = _should_queue(question, principal)
    if should_queue:
        row = _queue_for_review(question, principal, reason, required, source)
        return {
            "ok": True,
            "answer": "I can't safely answer that from your current permissions yet, so I saved it for Sam review.",
            "queued": True,
            "queue_id": row["id"],
            "storage": "ck",
            "ck_question_id": row["ck_question_id"],
            "reason": reason,
        }, 200

    answer = None
    model = None
    for provider_name, provider in (
        ("anthropic", _anthropic_answer),
        ("gemini", _gemini_answer),
    ):
        try:
            answer, model = provider(question, principal)
        except Exception:  # noqa: BLE001
            log.exception("assistant runtime %s answer failed", provider_name)
            answer = None
            model = None
        if answer:
            break

    if not answer:
        row = _queue_for_review(question, principal, "model_unavailable_or_no_answer", None, source)
        return {
            "ok": True,
            "answer": "I saved that for Sam review. The assistant model is not available right now.",
            "queued": True,
            "queue_id": row["id"],
            "storage": "ck",
            "ck_question_id": row["ck_question_id"],
            "reason": "model_unavailable_or_no_answer",
        }, 200

    return {
        "ok": True,
        "answer": answer,
        "queued": False,
        "model": model,
        "storage": "ck_runtime",
    }, 200


class Handler(BaseHTTPRequestHandler):
    server_version = "CenasAssistantRuntime/1.0"

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        expected = (os.getenv("ASSISTANT_RUNTIME_TOKEN") or os.getenv("ASSISTANT_REVIEW_TOKEN") or "").strip()
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
        self._json(200, {
            "ok": True,
            "service": "cenas_assistant_runtime",
            "db": str(review_receiver._db_path()),
            "row_counts": review_receiver._row_counts(),
            "providers": {
                "anthropic": bool(_read_secret("ANTHROPIC_API_KEY")),
                "gemini": bool(_read_secret("GEMINI_API_KEY")),
            },
        })

    def do_POST(self) -> None:
        if urlparse(self.path).path != ANSWER_PATH:
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
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            body, status = _answer(payload)
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": str(exc)})
            return
        self._json(status, body)

    def log_message(self, fmt, *args) -> None:
        return


def main() -> None:
    review_receiver._init_db()
    raw_hosts = os.getenv("ASSISTANT_RUNTIME_HOSTS") or os.getenv("ASSISTANT_RUNTIME_HOST") or "127.0.0.1"
    hosts = [host.strip() for host in raw_hosts.split(",") if host.strip()]
    port = int(os.getenv("ASSISTANT_RUNTIME_PORT") or "8782")
    servers = [ThreadingHTTPServer((host, port), Handler) for host in hosts]
    for httpd, host in zip(servers, hosts, strict=True):
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        print(f"assistant runtime listening on http://{host}:{port}")
    print(f"db: {review_receiver._db_path()}")
    threading.Event().wait()


if __name__ == "__main__":
    main()
