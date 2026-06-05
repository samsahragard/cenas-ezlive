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
    r"how many|how amny|count|total|report|summary|list|show me|who|which|"
    r"order|orders|driver|drivers|employee|employees|staff|team|"
    r"schedule|shift|roster|attendance|incident|write up|"
    r"tip|tips|labor|inventory|vendor|customer|ezcater|catering|"
    r"late|tracking|delivery|deliveries|pay|bonus|fee|fees"
    r")\b",
    re.IGNORECASE,
)
_OPERATIONAL_NOUN_RE = re.compile(
    r"\b("
    r"catering|caterings|order|orders|delivery|deliveries|"
    r"driver|drivers|labor|employee|employees|staff|team"
    r")\b",
    re.IGNORECASE,
)
_FOLLOWUP_RE = re.compile(
    r"\b("
    r"what about|how about|what baout|earlier|morning|afternoon|"
    r"evening|tonight|today|tomorrow|yesterday|this week|"
    r"tomball|dos|dos mas|copperfield|uno|uno mas"
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


def _tool_available(tools: list[dict], tool_id: str) -> bool:
    for tool in tools:
        if isinstance(tool, dict) and tool.get("tool_id") == tool_id and tool.get("available") is True:
            return True
    return False


def _wants_order_summary(question: str) -> bool:
    text = question.casefold()
    if re.search(r"\borders?\b.*\bdriver\s+attention\b", text):
        return True
    if re.search(r"\borders?\b.*\b(?:need|needs|needing)\s+(?:a\s+)?driver\b", text):
        return True
    return bool(
        re.search(r"\b(how (?:many|amny)|count|total|summary|report)\b", text)
        and re.search(r"\b(catering|caterings|order|orders|delivery|deliveries)\b", text)
    )


def _wants_driver_summary(question: str) -> bool:
    text = question.casefold()
    if re.search(r"\borders?\b.*\bdriver\s+attention\b", text):
        return False
    if re.search(r"\borders?\b.*\b(?:need|needs|needing)\s+(?:a\s+)?driver\b", text):
        return False
    return bool(
        re.search(
            r"\b(how many|count|total|summary|report|active|score|current|"
            r"coverage|availability|aggregate|roster|staffing|location|"
            r"on shift|active orders)\b",
            text,
        )
        and re.search(r"\b(driver|drivers)\b", text)
    )


def _wants_labor_summary(question: str) -> bool:
    text = question.casefold()
    return bool(
        re.search(r"\b(how many|count|total|summary|report|schedule|attendance|labor|employee|employees|staff|team)\b", text)
        and re.search(r"\b(labor|employee|employees|staff|team|schedule|attendance|shift|shifts)\b", text)
    )


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


def _requested_store(question: str) -> str | None:
    text = question.casefold()
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


def _requested_today_window(question: str) -> tuple[str, str] | None:
    text = question.casefold()
    if "earlier this morning" in text or "this morning" in text or re.search(r"\bmorning\b", text):
        return "morning", "earlier this morning"
    if "earlier today" in text:
        return "earlier_today", "earlier today"
    if re.search(r"\bafternoon\b", text):
        return "afternoon", "this afternoon"
    if re.search(r"\b(evening|tonight)\b", text):
        return "evening", "tonight"
    return None


def _store_count(mapping: dict, store: str | None, default_total: int) -> int:
    if not store:
        return default_total
    return int((mapping or {}).get(store, 0) or 0)


def _store_split(mapping: dict) -> str:
    return "; ".join(
        f"{store}: {count}" for store, count in sorted((mapping or {}).items())
    )


def _orders_summary_answer(summary: dict, question: str = "") -> str:
    requested_store = _requested_store(question)
    requested_window = _requested_today_window(question)
    today_date = str(summary.get("today") or "").strip()
    if requested_window:
        window_key, label = requested_window
        window_counts = summary.get("today_time_windows") or {}
        window_by_store = summary.get("today_time_windows_by_store") or {}
        count = int(window_counts.get(window_key) or 0)
        store_counts = window_by_store.get(window_key) or {}
        if requested_store:
            count = int(store_counts.get(requested_store) or 0)
        date_suffix = f" ({today_date})" if today_date else ""
        if requested_store:
            answer = (
                f"For {label}{date_suffix}, {requested_store} has "
                f"{count} {_plural(count, 'catering')}."
            )
        else:
            answer = f"For {label}{date_suffix}, there are {count} {_plural(count, 'catering')}."
        split = _store_split(store_counts)
        if split and not requested_store:
            answer += " Store split: " + split + "."
        return answer

    today_orders = int(summary.get("today_orders") or 0)
    upcoming_orders = int(summary.get("upcoming_orders") or 0)
    needs_driver = int(summary.get("needs_driver_orders") or 0)
    live_tracking = int(summary.get("live_tracking_orders") or 0)
    active_tracking = int(summary.get("active_tracking_orders") or 0)
    by_store = summary.get("today_by_store") or summary.get("by_store") or {}
    today_orders = _store_count(by_store, requested_store, today_orders)
    store_bits = [f"{store}: {count}" for store, count in sorted(by_store.items())]
    if requested_store:
        answer = f"{requested_store} has {today_orders} {_plural(today_orders, 'catering')} today."
    else:
        answer = (
            f"You have {today_orders} {_plural(today_orders, 'catering')} today"
            f" and {upcoming_orders} upcoming {_plural(upcoming_orders, 'order')} in the current view."
        )
    if needs_driver:
        answer += f" {needs_driver} still {_plural(needs_driver, 'need', 'need')} driver attention."
    else:
        answer += " No orders currently need driver attention."
    answer += f" {live_tracking} {_plural(live_tracking, 'order')} have tracking links"
    if active_tracking:
        answer += f", with {active_tracking} currently active"
    answer += "."
    if store_bits:
        answer += " Store split: " + "; ".join(store_bits) + "."
    return answer


def _drivers_summary_answer(summary: dict) -> str:
    total = int(summary.get("total_drivers") or 0)
    active = int(summary.get("active_drivers") or 0)
    on_shift = int(summary.get("drivers_on_shift") or 0)
    on_orders = int(summary.get("drivers_on_active_orders") or 0)
    average_score = summary.get("average_score")
    answer = (
        f"There are {total} {_plural(total, 'driver')} in the current view; "
        f"{active} {_plural(active, 'driver')} are active."
    )
    answer += f" {on_shift} {_plural(on_shift, 'driver')} are on shift"
    answer += f" and {on_orders} {_plural(on_orders, 'driver')} are tied to active orders."
    if average_score is not None:
        answer += f" Average current score is {average_score}."
    by_store = summary.get("by_store") or {}
    if by_store:
        answer += " Store split: " + "; ".join(
            f"{store}: {count}" for store, count in sorted(by_store.items())
        ) + "."
    return answer


def _labor_summary_answer(summary: dict) -> str:
    total = int(summary.get("total_employees") or 0)
    active = int(summary.get("active_employees") or 0)
    published = int(summary.get("published_shifts") or 0)
    open_shifts = int(summary.get("open_shifts") or 0)
    hours = float(summary.get("last30_cached_hours") or 0.0)
    answer = (
        f"There are {total} {_plural(total, 'employee')} in the current view; "
        f"{active} are active. Published schedule has {published} assigned "
        f"{_plural(published, 'shift')} and {open_shifts} open {_plural(open_shifts, 'shift')}."
    )
    if hours:
        answer += f" The last-30 cached labor total is {hours:g} hours."
    statuses = summary.get("today_attendance_statuses") or {}
    if statuses:
        answer += " Today's attendance statuses: " + "; ".join(
            f"{status}: {count}" for status, count in sorted(statuses.items())
        ) + "."
    return answer


def _contextual_followup(question: str, previous_question: str) -> bool:
    if not previous_question.strip():
        return False
    if re.search(r"^\s*(what about|how about|what baout|and\b|earlier|this morning|this afternoon|tonight)", question, re.IGNORECASE):
        return True
    if _OPERATIONAL_NOUN_RE.search(question):
        return False
    return bool(_FOLLOWUP_RE.search(question) or _DATA_TOOL_RE.search(question))


def _resolved_question(question: str, previous_question: str = "") -> str:
    question = str(question or "").strip()
    previous_question = str(previous_question or "").strip()
    if _contextual_followup(question, previous_question):
        return f"{previous_question}\nFollow-up: {question}"
    return question


def _approved_tool_answer(
    question: str,
    previous_question: str,
    principal: dict,
    tools: list[dict],
    tool_data: dict,
) -> dict | None:
    if not principal.get("is_owner_operator"):
        return None
    resolved_question = _resolved_question(question, previous_question)
    if _tool_available(tools, "drivers.store_summary"):
        driver_summary = tool_data.get("drivers.store_summary") if isinstance(tool_data, dict) else None
        if isinstance(driver_summary, dict) and _wants_driver_summary(resolved_question):
            return {
                "ok": True,
                "answer": _drivers_summary_answer(driver_summary),
                "queued": False,
                "storage": "operational_tool",
                "tool_id": "drivers.store_summary",
                "generated_at": driver_summary.get("generated_at"),
            }
    if _tool_available(tools, "labor.store_aggregate"):
        labor_summary = tool_data.get("labor.store_aggregate") if isinstance(tool_data, dict) else None
        if isinstance(labor_summary, dict) and _wants_labor_summary(resolved_question):
            return {
                "ok": True,
                "answer": _labor_summary_answer(labor_summary),
                "queued": False,
                "storage": "operational_tool",
                "tool_id": "labor.store_aggregate",
                "generated_at": labor_summary.get("generated_at"),
            }
    if _tool_available(tools, "orders.store_summary"):
        summary = tool_data.get("orders.store_summary") if isinstance(tool_data, dict) else None
        if isinstance(summary, dict) and _wants_order_summary(resolved_question):
            return {
                "ok": True,
                "answer": _orders_summary_answer(summary, resolved_question),
                "queued": False,
                "storage": "operational_tool",
                "tool_id": "orders.store_summary",
                "generated_at": summary.get("generated_at"),
            }
    return None


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


def _queued_answer(reason: str) -> str:
    if reason in {
        "sensitive_or_operational_question_needs_approved_tool",
        "data_question_needs_approved_tool",
    }:
        return "I do not have the approved Cenas data tool for that yet, so I saved it for Sam review."
    if reason == "not_authenticated":
        return "Please sign in first. I saved the question for Sam review."
    return "I can't safely answer that from your current permissions yet, so I saved it for Sam review."


def _system_prompt(principal: dict) -> str:
    return (
        _stable_policy_prompt()
        + "\n\n"
        + _session_prompt(principal)
    )


def _stable_policy_prompt() -> str:
    return (
        "You are the Cenas Kitchen in-app assistant running on CK. Answer only "
        "within the current user's role and permissions. You do not reveal "
        "secrets, passcodes, tokens, customer PII, unauthorized payroll, raw "
        "peer pay, sales internals, GUIDs, or cross-store data. This first "
        "version answers operational data questions only from approved, "
        "sanitized read-only tool payloads. If a question needs a tool that is "
        "not available, say it needs Sam review and do not guess."
    )


def _session_prompt(principal: dict) -> str:
    return (
        f"Current session: role={_role(principal)}, kind={principal.get('kind')}, "
        f"stores={principal.get('store_slugs')}, path={principal.get('path')}."
    )


def _anthropic_system_blocks(principal: dict) -> list[dict]:
    return [
        {
            "type": "text",
            "text": _stable_policy_prompt(),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": _session_prompt(principal),
        },
    ]


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
        system=_anthropic_system_blocks(principal),
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
    previous_question = str(payload.get("previous_question") or "").strip()[:_MAX_QUESTION_CHARS]
    principal = payload.get("principal") or {}
    tools = payload.get("tools") or []
    tool_data = payload.get("tool_data") or {}
    source = str(payload.get("source") or "cenas_app")
    if not question:
        return {"ok": False, "error": "question required"}, 400

    resolved_question = _resolved_question(question, previous_question)
    approved = _approved_tool_answer(question, previous_question, principal, tools, tool_data)
    if approved is not None:
        return approved, 200

    should_queue, reason, required = _should_queue(resolved_question, principal)
    if should_queue:
        row = _queue_for_review(question, principal, reason, required, source)
        return {
            "ok": True,
            "answer": _queued_answer(reason),
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
