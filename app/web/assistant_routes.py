"""Permission-scoped in-app assistant.

This is the manager/employee/driver-facing assistant surface, distinct from
Sam's operator-only /sam/chat. It deliberately starts read-only and
queue-first: if a question needs a data tool that is not approved for the
current role/session, it is saved for Sam review instead of guessed.

Durable review and model-runtime ownership: CK/Mini_IT13. In production the
web app should proxy assistant turns to the CK-local runtime; Render direct
model calls are blocked unless explicitly overridden for an emergency.
"""
from __future__ import annotations

import json
import logging
import os
import re
import hashlib
import threading
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, g, jsonify, request, session

from app.web.permissions import accessible_store_slugs
from app.services.permissions import ROLE_PERMISSIONS, has_permission

log = logging.getLogger(__name__)

assistant_bp = Blueprint("assistant", __name__)

_QUEUE_LOCK = threading.Lock()
_MAX_QUESTION_CHARS = 2000
_DEFAULT_MODEL = "claude-sonnet-4-6"
_REVIEW_STATUS = "needs_review"
_CK_REVIEW_PATH = "/review/question"
_CK_RUNTIME_PATH = "/assistant/answer"
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
_SECRET_TEXT_RE = re.compile(
    r"(?i)\b("
    r"sk-[A-Za-z0-9_-]{12,}|"
    r"[A-Za-z0-9_./+-]{24,}\.[A-Za-z0-9_./+-]{12,}\.[A-Za-z0-9_./+-]{12,}|"
    r"(?:token|secret|api key|password|passcode|pin)\s*[:=]\s*\S+"
    r")"
)


_TOOL_REGISTRY: list[dict[str, Any]] = [
    {
        "tool_id": "assistant.general_help",
        "label": "General app help",
        "description": "Answer general navigation, workflow, and policy-style questions without private operational data.",
        "required_permissions": ["ai.ask_claude_personal"],
        "session_types": ["partner", "staff", "employee", "driver"],
        "store_scope": "none",
        "data_class": "general",
        "read_write_class": "read_only",
        "status": "active",
    },
    {
        "tool_id": "employee.my_profile",
        "label": "Own employee profile",
        "description": "Future read-only personal profile answers scoped to the current employee.",
        "required_permissions": ["ai.ask_claude_personal"],
        "session_types": ["employee", "staff"],
        "store_scope": "own_profile",
        "data_class": "personal_profile",
        "read_write_class": "read_only",
        "status": "review_gated",
    },
    {
        "tool_id": "orders.store_summary",
        "label": "Store order summary",
        "description": "Future store-scoped order and catering summaries from approved marts.",
        "required_permissions": ["ai.ask_claude", "orders.view"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "operations",
        "read_write_class": "read_only",
        "status": "review_gated",
    },
    {
        "tool_id": "drivers.store_summary",
        "label": "Driver summary",
        "description": "Future driver performance and delivery summaries from CK driver marts.",
        "required_permissions": ["ai.ask_claude", "drivers.view_roster"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "driver_operations",
        "read_write_class": "read_only",
        "status": "review_gated",
    },
    {
        "tool_id": "labor.store_aggregate",
        "label": "Labor aggregate",
        "description": "Future aggregate-only labor answers from approved employee marts.",
        "required_permissions": ["ai.ask_claude", "labor.view_store_summary"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "labor_aggregate",
        "read_write_class": "read_only",
        "status": "review_gated",
    },
]


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_text(value: str) -> str:
    return _SECRET_TEXT_RE.sub("[REDACTED]", value)


def _env_truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _assistant_enabled() -> bool:
    if os.getenv("RENDER") and not _env_truthy("AI_ASSISTANT_ENABLED"):
        return False
    return not _env_truthy("AI_ASSISTANT_DISABLED")


def _review_risk_level(reason: str | None) -> str:
    reason_text = (reason or "").casefold()
    if any(term in reason_text for term in ("sensitive", "operational", "data", "permission")):
        return "blocked"
    return "normal"


def _queue_path() -> Path:
    raw = (
        os.getenv("AI_ASSISTANT_PENDING_PATH")
        or os.getenv("ASSISTANT_PENDING_QUEUE_PATH")
        or "/var/data/assistant_review_retry_outbox.jsonl"
    )
    return Path(raw)


def _env_first(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _review_timeout_seconds() -> float:
    raw = _env_first("ASSISTANT_REVIEW_TIMEOUT_SECONDS", "AI_ASSISTANT_CK_REVIEW_TIMEOUT_SECONDS")
    if not raw:
        return 8.0
    try:
        value = float(raw)
    except ValueError:
        return 8.0
    return max(1.0, min(value, 30.0))


def _ck_review_url(raw_url: str) -> str:
    return _normalize_service_url(raw_url, _CK_REVIEW_PATH)


def _ck_runtime_url(raw_url: str) -> str:
    return _normalize_service_url(raw_url, _CK_RUNTIME_PATH)


def _normalize_service_url(raw_url: str, default_path: str) -> str:
    url = raw_url.strip()
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme and parts.netloc and parts.path.rstrip("/") in {"", "/"}:
        return urllib.parse.urlunsplit((
            parts.scheme,
            parts.netloc,
            default_path,
            parts.query,
            parts.fragment,
        ))
    return url


def _runtime_configured() -> bool:
    url = _ck_runtime_url(_env_first("AI_ASSISTANT_CK_RUNTIME_URL", "ASSISTANT_RUNTIME_URL"))
    token = _env_first("AI_ASSISTANT_CK_RUNTIME_TOKEN", "ASSISTANT_RUNTIME_TOKEN")
    return bool(url and token)


def _assistant_available_for_context(ctx: dict[str, Any]) -> bool:
    if not _assistant_enabled() or not ctx.get("can_ask_personal"):
        return False
    if os.getenv("RENDER") and not _runtime_configured() and not _env_truthy("AI_ASSISTANT_ALLOW_RENDER_MODELS"):
        return False
    return True


def _extract_token() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Ai-Assistant-Token") or request.args.get("token")


def _role_permissions(role: str | None) -> set[str]:
    if not role:
        return set()
    perms = ROLE_PERMISSIONS.get(role)
    if perms == {"*"}:
        return {"*"}
    return set(perms or ())


def _store_scope_key(raw_store: Any) -> str | None:
    if raw_store is None:
        return None
    if isinstance(raw_store, str):
        return raw_store
    for attr in ("slug", "store_key", "key", "id"):
        value = getattr(raw_store, attr, None)
        if value:
            return str(value)
    return str(raw_store)


def _has_all_permissions(ctx: dict[str, Any], permissions: list[str]) -> bool:
    granted = set(ctx.get("permissions") or [])
    return "*" in granted or all(perm in granted for perm in permissions)


def _tool_catalog_for(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    session_type = ctx.get("kind")
    catalog: list[dict[str, Any]] = []
    for tool in _TOOL_REGISTRY:
        allowed_session = session_type in set(tool["session_types"])
        allowed_permissions = _has_all_permissions(ctx, tool["required_permissions"])
        status = tool["status"]
        available = bool(status == "active" and allowed_session and allowed_permissions)
        reason = None
        if not allowed_session:
            reason = "session_type_not_allowed"
        elif not allowed_permissions:
            reason = "missing_permission"
        elif status != "active":
            reason = "needs_sam_review"
        catalog.append({
            **tool,
            "available": available,
            "deny_reason": reason,
        })
    return catalog


def _principal_context() -> dict[str, Any]:
    user = getattr(g, "current_user", None)
    if session.get("driver_id"):
        role = "driver"
        kind = "driver"
        display_name = session.get("driver_name") or "Driver"
        principal_id = session.get("driver_id")
        stores: list[str] = []
    elif session.get("employee_id") and not user:
        role = "employee"
        kind = "employee"
        display_name = session.get("employee_name") or "Employee"
        principal_id = session.get("employee_id")
        stores = []
    elif user is not None:
        role = getattr(user, "permission_level", None) or "unknown"
        kind = "partner" if session.get("partner_auth_ok") else "staff"
        display_name = getattr(user, "full_name", None) or "User"
        principal_id = getattr(user, "id", None)
        stores = accessible_store_slugs(user)
    else:
        role = "anonymous"
        kind = "anonymous"
        display_name = "Anonymous"
        principal_id = None
        stores = []

    return {
        "kind": kind,
        "role": role,
        "principal_id": principal_id,
        "display_name": display_name,
        "store_slugs": stores,
        "current_store": _store_scope_key(getattr(g, "current_store", None)),
        "path": request.headers.get("X-Current-Path") or request.referrer or request.path,
        "permissions": sorted(_role_permissions(role)),
        "can_ask_personal": bool(
            role == "partner"
            or has_permission("ai.ask_claude_personal")
            or has_permission("ai.ask_claude")
            or role in {"driver", "employee"}
        ),
        "can_ask_operational": bool(role == "partner" or has_permission("ai.ask_claude")),
    }


def _append_pending_question(row: dict[str, Any]) -> None:
    row = _outbox_record(row)
    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _QUEUE_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _outbox_record(row: dict[str, Any]) -> dict[str, Any]:
    principal = row.get("principal") or {}
    question = _redact_text(str(row.get("question") or ""))
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "status": row.get("status") or _REVIEW_STATUS,
        "question_hash": _stable_hash({"question": question}),
        "question_summary_redacted": question[:500],
        "reason": row.get("reason"),
        "required_permission": row.get("required_permission"),
        "risk_level": row.get("risk_level") or _review_risk_level(row.get("reason")),
        "principal_hash": _stable_hash({
            "kind": principal.get("kind"),
            "role": principal.get("role"),
            "principal_id": principal.get("principal_id"),
            "display_name": principal.get("display_name"),
        }),
        "scope_role": principal.get("role"),
        "scope_store_key": (
            principal.get("current_store")
            or ((principal.get("store_slugs") or [None])[0])
        ),
        "source_path": principal.get("path"),
        "storage": "render_retry_outbox_redacted",
        "ck_target": "assistant_review.sqlite",
    }


def _post_to_ck_review(row: dict[str, Any]) -> tuple[bool, str | None]:
    """Review-only durable persistence path for blocked questions.

    CK owns the review DB. Configure AI_ASSISTANT_CK_REVIEW_URL to a CK-local
    endpoint, for example a Tailscale URL on Mini_IT13. Token can be supplied
    with AI_ASSISTANT_CK_REVIEW_TOKEN. Production answer execution should use
    the CK runtime path instead; this receiver path remains for local review
    ingestion and compatibility tests.
    """
    url = _ck_review_url(_env_first("AI_ASSISTANT_CK_REVIEW_URL", "ASSISTANT_REVIEW_RECEIVER_URL"))
    if not url:
        return False, None
    headers = {"Content-Type": "application/json"}
    token = _env_first("AI_ASSISTANT_CK_REVIEW_TOKEN", "ASSISTANT_REVIEW_RECEIVER_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Ai-Assistant-Token"] = token
    try:
        import httpx

        proxy = (os.getenv("CENA_PROXY") or "").strip() or None
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(_review_timeout_seconds(), connect=min(3.0, _review_timeout_seconds())),
        }
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as hx:
            resp = hx.post(url, json=row, headers=headers)
        data = resp.json() if resp.content else {}
        if 200 <= resp.status_code < 300 and data.get("ok", True):
            ck_id = data.get("ck_question_id") or data.get("question_id") or data.get("id")
            return True, str(ck_id) if ck_id is not None else None
    except Exception:  # noqa: BLE001
        log.exception("assistant: CK review save failed")
    return False, None


def _runtime_principal(ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": ctx["kind"],
        "role": ctx["role"],
        "principal_id": ctx["principal_id"],
        "display_name": ctx["display_name"],
        "store_slugs": ctx["store_slugs"],
        "current_store": ctx["current_store"],
        "path": ctx["path"],
        "permissions": ctx["permissions"],
        "can_ask_personal": ctx["can_ask_personal"],
        "can_ask_operational": ctx["can_ask_operational"],
    }


def _post_to_ck_runtime(question: str, ctx: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
    """Send the assistant turn to the CK-local runtime.

    The runtime is the execution boundary for production: model calls, durable
    question storage, and future data tools stay on CK. Render is only a
    signed web/session proxy when this URL is configured.
    """
    url = _ck_runtime_url(_env_first("AI_ASSISTANT_CK_RUNTIME_URL", "ASSISTANT_RUNTIME_URL"))
    token = _env_first("AI_ASSISTANT_CK_RUNTIME_TOKEN", "ASSISTANT_RUNTIME_TOKEN")
    if not url or not token:
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Ai-Assistant-Token": token,
        "Content-Type": "application/json",
    }
    payload = {
        "question": question,
        "principal": _runtime_principal(ctx),
        "tools": _tool_catalog_for(ctx),
        "source": "cenas_app",
        "requested_at": _now_iso(),
    }
    try:
        import httpx

        proxy = (os.getenv("CENA_PROXY") or "").strip() or None
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(_review_timeout_seconds(), connect=min(3.0, _review_timeout_seconds())),
        }
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as hx:
            resp = hx.post(url, json=payload, headers=headers)
        data = resp.json() if resp.content else {}
        if 200 <= resp.status_code < 300:
            return data, resp.status_code
        return {
            "ok": False,
            "error": "ck_runtime_rejected",
            "answer": "I could not reach the CK assistant safely right now.",
            "queued": False,
        }, 503
    except Exception:  # noqa: BLE001
        log.exception("assistant: CK runtime call failed")
        return {
            "ok": False,
            "error": "ck_runtime_unavailable",
            "answer": "I could not reach the CK assistant safely right now.",
            "queued": False,
        }, 503


def _queue_for_review(question: str, ctx: dict[str, Any], reason: str,
                      required_permission: str | None = None) -> dict[str, Any]:
    row = {
        "id": str(uuid.uuid4()),
        "created_at": _now_iso(),
        "status": _REVIEW_STATUS,
        "risk_level": _review_risk_level(reason),
        "question": question,
        "reason": reason,
        "required_permission": required_permission,
        "role": ctx["role"],
        "store_key": ctx["current_store"] or ((ctx["store_slugs"] or [None])[0]),
        "model_key": "review_queue",
        "tool_name": required_permission or "assistant.general_help",
        "delivery_target": "ck_assistant_review",
        "principal": {
            "kind": ctx["kind"],
            "role": ctx["role"],
            "principal_id": ctx["principal_id"],
            "display_name": ctx["display_name"],
            "store_slugs": ctx["store_slugs"],
            "current_store": ctx["current_store"],
            "path": ctx["path"],
            "permissions": ctx["permissions"],
        },
    }
    saved_on_ck, ck_id = _post_to_ck_review(row)
    if saved_on_ck:
        row["storage"] = "ck"
        row["ck_question_id"] = ck_id
    else:
        row["storage"] = "render_retry_outbox"
        row["outbox_note"] = (
            "CK review ingress unavailable or not configured; CK remains "
            "the authoritative target."
        )
        _append_pending_question(row)
    return row


def _anthropic_answer(question: str, ctx: dict[str, Any]) -> tuple[str | None, str | None]:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None, None
    try:
        import anthropic  # type: ignore[import]
    except ImportError:
        log.warning("assistant: anthropic package not installed")
        return None, None

    model = os.getenv("AI_ASSISTANT_ANTHROPIC_MODEL", _DEFAULT_MODEL)
    client = anthropic.Anthropic(api_key=key)
    system = _system_prompt(ctx)
    msg = client.messages.create(
        model=model,
        max_tokens=800,
        temperature=0.2,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    text = "".join(
        block.text for block in getattr(msg, "content", [])
        if getattr(block, "type", None) == "text"
    ).strip()
    return text or None, model


def _gemini_answer(question: str, ctx: dict[str, Any]) -> tuple[str | None, str | None]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None, None
    try:
        from google import genai  # type: ignore[import]
    except ImportError:
        log.warning("assistant: google-genai package not installed")
        return None, None

    model = os.getenv("AI_ASSISTANT_GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=key)
    prompt = _system_prompt(ctx) + "\n\nUser question:\n" + question
    resp = client.models.generate_content(model=model, contents=prompt)
    text = (getattr(resp, "text", None) or "").strip()
    return text or None, model


def _system_prompt(ctx: dict[str, Any]) -> str:
    return (
        "You are the Cenas Kitchen in-app assistant. Answer only within the "
        "current user's role and permissions. You do not reveal secrets, "
        "passcodes, tokens, customer PII, unauthorized payroll, raw peer pay, "
        "sales internals, GUIDs, or cross-store data. This first version has no "
        "approved active database tools yet, so answer only general app-help or policy "
        "questions. Review-gated tools are visible to the server but not available "
        "to you for answers. If the user asks for private data or operational facts that "
        "require a tool, say the question needs Sam review and do not guess.\n\n"
        f"Current session: role={ctx['role']}, kind={ctx['kind']}, "
        f"stores={ctx['store_slugs']}, path={ctx['path']}."
    )


def _should_queue(question: str, ctx: dict[str, Any]) -> tuple[bool, str, str | None]:
    if ctx["kind"] == "anonymous":
        return True, "not_authenticated", "ai.ask_claude_personal"
    if not ctx["can_ask_personal"]:
        return True, "missing_ai_permission", "ai.ask_claude_personal"
    if _SENSITIVE_RE.search(question):
        needed = "ai.ask_claude"
        if not ctx["can_ask_operational"]:
            return True, "sensitive_or_operational_question_requires_higher_permission", needed
        return True, "sensitive_or_operational_question_needs_approved_tool", needed
    if _DATA_TOOL_RE.search(question):
        needed = "ai.ask_claude"
        if not ctx["can_ask_operational"]:
            return True, "data_question_requires_higher_permission", needed
        return True, "data_question_needs_approved_tool", needed
    return False, "", None


@assistant_bp.route("/assistant/context", methods=["GET"])
def assistant_context():
    ctx = _principal_context()
    tools = _tool_catalog_for(ctx)
    enabled = _assistant_available_for_context(ctx)
    return jsonify({
        "ok": ctx["kind"] != "anonymous",
        "principal": {
            "kind": ctx["kind"],
            "role": ctx["role"],
            "display_name": ctx["display_name"],
            "store_slugs": ctx["store_slugs"],
        },
        "enabled": bool(enabled),
        "tools": [
            {
                "tool_id": tool["tool_id"],
                "label": tool["label"],
                "status": tool["status"],
                "available": tool["available"],
                "deny_reason": tool["deny_reason"],
            }
            for tool in tools
        ],
    })


@assistant_bp.route("/assistant/tools", methods=["GET"])
def assistant_tools():
    ctx = _principal_context()
    if ctx["kind"] == "anonymous":
        return jsonify({"ok": False, "error": "not_authenticated"}), 401
    return jsonify({
        "ok": True,
        "generated_at": _now_iso(),
        "tools": _tool_catalog_for(ctx),
    })


@assistant_bp.route("/assistant/ask", methods=["POST"])
def assistant_ask():
    if not _assistant_enabled():
        return jsonify({"ok": False, "error": "assistant_disabled"}), 503

    ctx = _principal_context()
    if not _assistant_available_for_context(ctx):
        return jsonify({"ok": False, "error": "assistant_unavailable"}), 503

    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or "").strip()[:_MAX_QUESTION_CHARS]
    if not question:
        return jsonify({"ok": False, "error": "question required"}), 400

    runtime_response = _post_to_ck_runtime(question, ctx)
    if runtime_response is not None:
        data, status = runtime_response
        return jsonify(data), status

    if os.getenv("RENDER") and not _env_truthy("AI_ASSISTANT_ALLOW_RENDER_MODELS"):
        return jsonify({"ok": False, "error": "ck_runtime_required"}), 503

    should_queue, reason, required = _should_queue(question, ctx)
    if should_queue:
        row = _queue_for_review(question, ctx, reason, required)
        return jsonify({
            "ok": True,
            "answer": "I can't safely answer that from your current permissions yet, so I saved it for Sam review.",
            "queued": True,
            "queue_id": row["id"],
            "storage": row.get("storage"),
            "ck_question_id": row.get("ck_question_id"),
            "reason": reason,
        })

    try:
        answer, model = _anthropic_answer(question, ctx)
        if answer is None:
            answer, model = _gemini_answer(question, ctx)
    except Exception:
        log.exception("assistant answer failed")
        answer = None
        model = None

    if not answer:
        row = _queue_for_review(question, ctx, "model_unavailable_or_no_answer", None)
        return jsonify({
            "ok": True,
            "answer": "I saved that for Sam review. The assistant model is not available right now.",
            "queued": True,
            "queue_id": row["id"],
            "storage": row.get("storage"),
            "ck_question_id": row.get("ck_question_id"),
            "reason": "model_unavailable_or_no_answer",
        })

    return jsonify({"ok": True, "answer": answer, "queued": False, "model": model})


@assistant_bp.route("/cron/assistant-questions-export", methods=["GET"])
def assistant_questions_export():
    expected = os.getenv("AI_ASSISTANT_EXPORT_TOKEN")
    if not expected or _extract_token() != expected:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    path = _queue_path()
    rows: list[dict[str, Any]] = []
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return jsonify({"ok": True, "generated_at": _now_iso(), "rows": rows})
