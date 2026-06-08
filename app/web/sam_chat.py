"""Sam Chat — a standalone /sam/chat surface for Sam to converse with
Cenas AI through Gemini 2.5 Flash.

Deliberately ISOLATED from the agentic pipeline: no Cenas Kitchen
system prompt, no agent context, no reads/writes to AgentChatMessage /
AgentActionLog / any Phase 2 Block 3 table. Sam pastes context into
the conversation as needed; clean slate every session. Distinct from
the agent Developer Chat and from Block 3's manager-facing in-app
agent.

Access — hard-gated to ONE user:
  - SAM_CHAT_USER_ID env var holds Sam's User.id. The route checks
    g.current_user.id == SAM_CHAT_USER_ID directly — NOT via
    @requires_permission / ROLE_PERMISSIONS, so the sam_chat.access
    capability can never be role-inherited.
  - Until Sam sets SAM_CHAT_USER_ID, _sam_chat_user_id() returns None
    and is_sam_chat_user() is False for everyone — the feature is
    safe-closed/dormant (every hit -> access-denied, no sidebar link).
  - Anyone else -> 302 -> /access-denied?need=sam_chat.

Routes:
  GET  /sam/chat                          — the chat UI
  POST /sam/chat/send                     — send a message, SSE-stream
                                            Cenas AI's reply back
  GET  /sam/chat/sessions                 — list sessions (JSON)
  POST /sam/chat/sessions                 — create a new session (JSON)
  GET  /sam/chat/sessions/<id>            — load a session's messages
  POST /sam/chat/sessions/<id>/rename     — rename a session
  POST /sam/chat/sessions/<id>/archive    — archive a session

install(app) registers the blueprint + the is_sam_chat_user Jinja
global (the sidebar link uses it).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from flask import (
    Blueprint, Response, abort, g, has_app_context, jsonify, redirect, render_template,
    request, stream_with_context, url_for,
)

from app.db import SessionLocal
from app.models import (
    SamChatSession,
    SamChatMessage,
    SamChatSuggestion,
    _VALID_SAM_CHAT_ROLES,
    _VALID_SAM_CHAT_SUGGESTION_STATUS,
)

logger = logging.getLogger(__name__)

sam_chat_bp = Blueprint("sam_chat", __name__)


# ---- model routing ----
_DEFAULT_MODEL = "gemini-2.5-flash"
_PICKER_MODELS = (_DEFAULT_MODEL,)
_ALLOWED_MODELS = set(_PICKER_MODELS)
_MODEL_LABELS = {
    "gemini-2.5-flash":  "Gemini 2.5 Flash",
}
# Rough list-price estimates, USD per million tokens.
_MODEL_RATES = {
    "gemini-2.5-flash":  {"in": 0.15, "out": 0.60},
}


def _auto_select_model(text: str) -> str:
    """Coerce any stale/unknown browser value back to Gemini 2.5 Flash."""
    return _DEFAULT_MODEL


def _gemini_client():
    """Returns a google.genai Client or None if GEMINI_API_KEY is unset."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from google import genai  # type: ignore[import]
        return genai.Client(api_key=api_key)
    except ImportError:
        logger.warning("sam_chat: google-genai package not installed")
        return None


_MAX_OUTPUT_TOKENS = 8192
# Attachment limits (Sam's spec): 5MB per file, 20MB total per message.
_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
_MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024
# Soft context-window warning — well under Opus 4.7's 200K.
_CONTEXT_WARN_TOKENS = 180_000

_IMAGE_MEDIA = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "gif": "image/gif",
}
_TEXT_EXTS = {
    "txt", "md", "csv", "log", "json", "py", "js", "html", "css",
    "yaml", "yml", "xml", "tsv", "ini", "cfg", "sql",
}


# ============================================================
# Access gate — hard-bound to SAM_CHAT_USER_ID
# ============================================================

def _sam_chat_user_id() -> int | None:
    """Sam's User.id from the SAM_CHAT_USER_ID env var, or None if unset
    / unparseable. None => the feature is dormant (safe-closed)."""
    raw = (os.getenv("SAM_CHAT_USER_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("sam_chat: SAM_CHAT_USER_ID=%r is not an integer", raw)
        return None


def is_sam_chat_user() -> bool:
    """True iff the current keypad-authenticated user is Sam (the
    SAM_CHAT_USER_ID match). Registered as a Jinja global so the sidebar
    link renders for Sam only. False for everyone when SAM_CHAT_USER_ID
    is unset."""
    sam_id = _sam_chat_user_id()
    if sam_id is None:
        return False
    user = getattr(g, "current_user", None)
    return user is not None and getattr(user, "id", None) == sam_id


def _require_sam_page():
    """Gate for the HTML page route — redirect non-Sam to access-denied.
    Returns a redirect Response to short-circuit, or None to proceed."""
    if not is_sam_chat_user():
        return redirect(url_for("auth.access_denied",
                                need="sam_chat", next=request.path))
    return None


def _require_sam_api():
    """Gate for the JSON/SSE API routes — 403 JSON for non-Sam.
    Returns a 403 Response to short-circuit, or None to proceed."""
    if not is_sam_chat_user():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return None


# ============================================================
# Cenas AI / gateway plumbing
# ============================================================

def _cena_gateway_url() -> str | None:
    """URL of Cena's gateway server on aick, e.g.
    https://cena-api.cenaskitchen.com  (set via CENA_GATEWAY_URL env var).
    When set, sam_chat routes to Cena instead of calling Gemini directly.
    Returns None when the env var is absent — falls back to direct Gemini."""
    url = (os.getenv("CENA_GATEWAY_URL") or "").strip().rstrip("/")
    return url or None


def _mirror_to_cena_sam_chat(author: str, body: str) -> None:
    """Best-effort POST of a /sam/chat message into the private
    cena_sam_chat surface at aick:8770. Sam directive #292 (2026-05-23):
    cena_sam_chat is the unified VIEW; every Sam-typed message and every
    Cena reply on /sam/chat mirrors there so Sam sees one stream.

    Author MUST start with 'sam-online' or 'cena-online' so the
    cena_sam_chat wake-on-sam trigger (which fires only on literal
    'sam') is never re-fired by a mirror (that would double-trigger
    Cena, who already replied on the /sam/chat side).

    Routes via CENA_PROXY (Render's userspace tailscaled) just like
    /cena/stream — the Tailscale endpoint 100.108.119.19:8770 is not
    reachable from Render without it. Token comes from the
    CENA_SAM_CHAT_TOKEN env var; unset = silent skip. Never raises:
    a failed mirror must not break the user's chat turn."""
    if not body or not author:
        return
    if author not in ("sam-online", "cena-online"):
        return
    url = (os.getenv("CENA_SAM_CHAT_URL")
           or "http://100.108.119.19:8770").rstrip("/")
    token = (os.getenv("CENA_SAM_CHAT_TOKEN") or "").strip()
    if not token:
        return
    proxy = os.getenv("CENA_PROXY") or None
    try:
        import httpx
        client_kwargs: dict = {"timeout": httpx.Timeout(5.0, connect=3.0)}
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as hx:
            hx.post(url + "/post",
                    json={"author": author, "body": body},
                    headers={"X-Auth-Token": token,
                             "Content-Type": "application/json"})
    except Exception:  # noqa: BLE001
        # silent — Sam sees nothing if the mirror fails; the primary
        # chat path is unaffected. (We log via logger but no chat
        # surface noise.)
        try:
            logger.exception("sam_chat: cena_sam_chat mirror failed")
        except Exception:  # noqa: BLE001
            pass


def _gateway_active_model_get() -> str:
    """GET the canonical active model from the gateway. Sam directive #276
    (2026-05-23) — the gateway holds the source-of-truth so /sam/chat
    and cena_sam_chat agree on which model Cena is running. Returns
    _DEFAULT_MODEL on any failure (network, gateway down, auth, etc.) so
    the chat page always renders — silent degradation, not a crash."""
    url = _cena_gateway_url()
    if not url:
        return _DEFAULT_MODEL
    token = os.getenv("CENA_GATEWAY_TOKEN", "")
    proxy = os.getenv("CENA_PROXY") or None
    try:
        import httpx
        client_kwargs: dict = {"timeout": httpx.Timeout(5.0, connect=3.0)}
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as hx:
            r = hx.get(url + "/cena/active-model",
                       headers={"X-Cena-Token": token})
            if r.status_code != 200:
                return _DEFAULT_MODEL
            m = (r.json() or {}).get("model")
            if isinstance(m, str) and m in _ALLOWED_MODELS:
                return m
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_MODEL


def _gateway_active_model_set(model: str) -> tuple[bool, str]:
    """POST a new active-model selection to the gateway. Returns
    (ok, model_or_error). Same gateway-down silent-degradation
    behavior as the GET twin — but here we surface the error to the
    caller so /sam/chat can show Sam a status, not silently swallow."""
    url = _cena_gateway_url()
    if not url:
        return False, "gateway URL unset"
    if model not in _ALLOWED_MODELS:
        return False, f"model {model!r} not in allowed set"
    token = os.getenv("CENA_GATEWAY_TOKEN", "")
    proxy = os.getenv("CENA_PROXY") or None
    try:
        import httpx
        client_kwargs: dict = {"timeout": httpx.Timeout(5.0, connect=3.0)}
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as hx:
            r = hx.post(url + "/cena/active-model",
                        headers={"X-Cena-Token": token,
                                 "Content-Type": "application/json"},
                        json={"model": model})
            if r.status_code != 200:
                return False, f"gateway returned {r.status_code}"
            m = (r.json() or {}).get("model")
            return True, (m or model)
    except Exception as e:  # noqa: BLE001
        return False, f"gateway error: {e}"


# ============================================================
# Phase 2 §9 — async gateway message layer
# ============================================================
# /sam/chat can run in two modes:
#   - SSE-streaming (the original /sam/chat/send): one held connection
#     streams the reply; lost if the tab closes mid-answer.
#   - async (cena2 Phase 2 §9): /sam/chat/async/* proxy a gateway's
#     /cena/messages/* queue endpoints — send returns immediately, the
#     worker on the gateway produces the answer, the UI polls /status
#     every 2s, /history restores the thread on page load. Tab closed
#     mid-think → the answer still completes and waits on the gateway.
#
# Async mode is gated by SAM_CHAT_ASYNC, default OFF — the same
# safe-closed/dormant pattern this module uses for SAM_CHAT_USER_ID.
# Deploying this code changes nothing until the flag is set, so it can
# land before the §11 Flask↔gateway reachability work is finished and
# be switched on once both gateways are reachable. [ck]

def _sam_chat_async_enabled() -> bool:
    """True iff SAM_CHAT_ASYNC is set truthy — switches /sam/chat to the
    Phase 2 async queue model (spec §9). Default off (safe-closed)."""
    return (os.getenv("SAM_CHAT_ASYNC") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _message_gateways() -> list[dict]:
    """The gateways /sam/chat may route a queued message to (spec §10.4).
    Ordered list of {name, url, token}; the caller tries them in order
    and uses the first that responds — automatic failover, no routing
    config. A gateway appears only when its URL env var is configured."""
    out: list[dict] = []
    aick_url = (os.getenv("CENA_GATEWAY_URL") or "").strip().rstrip("/")
    if aick_url:
        out.append({"name": "aick", "url": aick_url,
                    "token": os.getenv("CENA_GATEWAY_TOKEN", "")})
    cena2_url = (os.getenv("CENA2_GATEWAY_URL") or "").strip().rstrip("/")
    if cena2_url:
        out.append({"name": "cena2", "url": cena2_url,
                    "token": os.getenv("CENA2_GATEWAY_TOKEN", "")})
    return out


def _gw_message_call(method: str, path: str, json_body=None, params=None):
    """Call a gateway's /cena/messages/* endpoint, trying each gateway in
    _message_gateways() order and using the first that RESPONDS (spec
    §10.4 first-responder routing — a transport error or a 5xx falls
    through to the next gateway; a <500 response is a definitive answer
    from a reachable gateway and is returned as-is). Routes through
    CENA_PROXY when set (Render's userspace tailscaled SOCKS5 — the same
    path the /cena/stream call uses). Returns (gateway_name, status,
    json) — status is None when no gateway is reachable at all."""
    import httpx
    proxy = os.getenv("CENA_PROXY") or None
    # 20s overall, but a short 5s CONNECT timeout so a dead primary
    # gateway fails over to the next in ~5s instead of eating the full
    # 20s (samai §13 review — §10.4 health-aware-routing intent).
    client_kwargs: dict = {"timeout": httpx.Timeout(20.0, connect=5.0)}
    if proxy:
        client_kwargs["proxy"] = proxy
    last = (None, None, None)
    for gw in _message_gateways():
        try:
            with httpx.Client(**client_kwargs) as hx:
                r = hx.request(
                    method, gw["url"] + path,
                    json=json_body if json_body is not None else None,
                    params=params or None,
                    headers={"X-Cena-Token": gw["token"]})
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = {}
            if r.status_code < 500:
                return gw["name"], r.status_code, body
            last = (gw["name"], r.status_code, body)  # 5xx — try the next
        except Exception as e:  # noqa: BLE001
            logger.warning("sam_chat async: gateway %s unreachable: %s",
                           gw["name"], e)
    return last


def _estimate_cost(model: str, in_tok: int, out_tok: int,
                   cache_create_tok: int = 0,
                   cache_read_tok: int = 0) -> Decimal:
    """Rough USD cost estimate from token usage. Quantized to 4 places
    for the Numeric(10,4) column. Best-effort — see _MODEL_RATES.

    Cache-token inputs are accepted for gateway compatibility. Direct
    Gemini sends usually leave these at zero."""
    rates = _MODEL_RATES.get(model, {"in": 0.0, "out": 0.0})
    in_rate = rates["in"]
    usd = ((in_tok or 0) * in_rate
           + (cache_create_tok or 0) * in_rate * 2.0
           + (cache_read_tok or 0) * in_rate * 0.10
           + (out_tok or 0) * rates["out"]) / 1_000_000
    return Decimal(str(usd)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# Tool-block strip for model context (Sam #2148 + samai #2154 hybrid
# spec). Cena's gateway streams tool calls inline in the assistant turn
# text using a fixed format:
#   "\n\n[<tool_name>(<args>)]\n→ <preview up to 301 chars>\n"
# (cena_gateway.py:857 announce, :884 result_notice). The full streamed
# text is persisted into SamChatMessage.content. On the next turn,
# rebuilding api_messages from those rows feeds prior tool blocks BACK
# to the model — and it can latch onto the in-context prior result
# instead of re-firing the tool. That substrate is the confabulation
# vector documented in lessons #1865/#2042/#2122/#2138. (B) system-
# prompt nudge alone is insufficient to override the in-context pull.
# This strip removes the substrate from the API-bound version while
# leaving the UI/DB version intact.
#
# Boundary heuristic: a tool block runs from the announce-prefix until
# the next double-newline (paragraph break) or end-of-string. Cena's
# response style includes paragraph breaks after tool calls, so this
# bounds cleanly in practice. Some natural continuation text may be
# lost on the way; that's the conservative trade per samai #2154.
#
# Format coupling: the regex is in lockstep with cena_gateway.py:857 +
# :884. If cena_gateway changes the announce/result format, the regex
# stops matching and prior tool blocks pass through unstripped — tests
# below catch a known-shape sample to detect drift.
_CENA_TOOL_BLOCK_RE = re.compile(
    r'\n\n\[[a-zA-Z_]\w*\([^\]]*\)\][\s\S]*?(?=\n\n|\Z)'
)
_CENA_TOOL_STRIP_MARKER = (
    "\n\n[earlier tool calls this turn stripped from context — "
    "re-fire any tool to get current data]"
)
_ASSISTANT_REVIEW_SESSION_TITLE = "Cenas AI Review"
_ASSISTANT_REVIEW_SESSION_PREFIX = "Cenas AI Review: "
_ASSISTANT_REVIEW_SESSION_SUFFIX = " - Cenas AI"
_ASSISTANT_REVIEW_MODEL = "assistant-review-mirror"


def _is_assistant_review_session(s: SamChatSession | None) -> bool:
    title = str(getattr(s, "title", "") or "")
    return (
        title == _ASSISTANT_REVIEW_SESSION_TITLE
        or title.startswith(_ASSISTANT_REVIEW_SESSION_PREFIX)
        or title.endswith(_ASSISTANT_REVIEW_SESSION_SUFFIX)
    )


def _assistant_review_context(content: str) -> str:
    text = str(content or "")
    if not text:
        return ""
    payload = None
    if text.startswith("CENAS_ASSISTANT_REVIEW_V2\n"):
        try:
            payload = json.loads(text.split("\n", 1)[1])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
    if isinstance(payload, dict):
        actor = payload.get("actor") or {}
        turn = payload.get("turn") or {}
        previous = turn.get("previous") or {}
        result = payload.get("result") or {}
        tool = payload.get("tool") or {}
        parts = [
            "Cenas AI review context:",
            f"Asked at: {payload.get('asked_at') or ''}",
            f"Asked by: {actor.get('display_name') or 'Unknown'}",
            f"Question: {turn.get('question') or ''}",
        ]
        if previous.get("question"):
            parts.append(f"Previous question: {previous.get('question')}")
        if previous.get("answer"):
            parts.append(f"Previous answer: {previous.get('answer')}")
        parts.extend([
            f"Answer: {turn.get('answer') or ''}",
            f"Status: {result.get('status') or ''}",
        ])
        if tool.get("id"):
            parts.append(f"Tool: {tool.get('id')}")
        return "\n".join(part for part in parts if part.strip())
    if text.startswith("Cenas AI assistant review"):
        return "Cenas AI review context:\n" + text
    return ""


def _current_sam_display_name() -> str | None:
    if not has_app_context():
        return None
    user = getattr(g, "current_user", None)
    name = str(getattr(user, "full_name", "") or "").strip()
    return re.sub(r"\s+", " ", name) if name else None


def _canonical_review_subject(name: str | None) -> str | None:
    clean = re.sub(r"\s+", " ", str(name or "").strip())
    if not clean:
        return None
    current = _current_sam_display_name()
    if current:
        first = current.split()[0].casefold()
        if clean.casefold() in {current.casefold(), first}:
            return current
    return clean


def _review_subject_from_title(title: str) -> str | None:
    if title.startswith(_ASSISTANT_REVIEW_SESSION_PREFIX):
        return title[len(_ASSISTANT_REVIEW_SESSION_PREFIX):]
    if title.endswith(_ASSISTANT_REVIEW_SESSION_SUFFIX):
        return title[:-len(_ASSISTANT_REVIEW_SESSION_SUFFIX)]
    return None


def _review_subject_from_content(content: str) -> str | None:
    text = str(content or "")
    payload = None
    for prefix in ("CENAS_ASSISTANT_REVIEW_V2\n",
                   "CENAS_ASSISTANT_REVIEW_V1\n"):
        if text.startswith(prefix):
            try:
                payload = json.loads(text[len(prefix):])
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            break
    if isinstance(payload, dict):
        actor = payload.get("actor") or payload.get("sender") or {}
        return _canonical_review_subject(
            actor.get("display_name") or actor.get("name"))
    if text.startswith("Cenas AI assistant review"):
        m = re.search(r"^Name:\s*(.+)$", text, flags=re.MULTILINE)
        if m:
            return _canonical_review_subject(m.group(1))
    return None


def _review_subject_for_session(db, s: SamChatSession,
                                messages: list[SamChatMessage] | None = None
                                ) -> str | None:
    if not _is_assistant_review_session(s):
        return None
    title = str(s.title or "")
    subject = _canonical_review_subject(_review_subject_from_title(title))
    if subject:
        return subject
    for msg in reversed(messages or []):
        if msg.model != _ASSISTANT_REVIEW_MODEL:
            continue
        subject = _review_subject_from_content(msg.content)
        if subject:
            return subject
    try:
        msg = (db.query(SamChatMessage)
               .filter(SamChatMessage.session_id == s.id)
               .filter(SamChatMessage.model == _ASSISTANT_REVIEW_MODEL)
               .order_by(SamChatMessage.id.desc())
               .first())
    except Exception:  # noqa: BLE001
        msg = None
    if msg is not None:
        subject = _review_subject_from_content(msg.content)
        if subject:
            return subject
    if title == _ASSISTANT_REVIEW_SESSION_TITLE:
        return _current_sam_display_name()
    return None


def _strip_cena_tool_blocks(content: str) -> str:
    """Remove cena-gateway tool announcements + result previews from a
    prior assistant turn's content before it's passed back to the model.
    Appends ONE terminal marker if any block was stripped.

    See _CENA_TOOL_BLOCK_RE comment block for design rationale (Sam
    #2148 + samai #2154 cleanup of the #1865 lesson family)."""
    if not content:
        return content
    stripped, n = _CENA_TOOL_BLOCK_RE.subn('', content)
    if n == 0:
        return content
    return stripped.rstrip() + _CENA_TOOL_STRIP_MARKER


def _merge_content(prev, curr):
    """Combine two same-role turns into one. str+str joins with a blank
    line; if either side is a content-block list (a turn carrying image
    blocks), both are normalized to block lists and concatenated — the
    merge never yields two consecutive same-role API messages."""
    if isinstance(prev, str) and isinstance(curr, str):
        return prev + "\n\n" + curr

    def _as_blocks(x):
        return [{"type": "text", "text": x}] if isinstance(x, str) else list(x)

    return _as_blocks(prev) + _as_blocks(curr)


def _build_api_messages_from_rows(rows, images_by_msg=None) -> list[dict]:
    """Map persisted SamChatMessage rows to user/assistant
    message list, applying the conversation-flow rules /sam/chat needs:

    - 'user' rows pass through as user turns
    - 'assistant' rows pass through as assistant turns (with cena tool
      blocks stripped — see _strip_cena_tool_blocks)
    - 'dck' rows (Track 8b per Sam #2236) map to USER turns with a
      '[dck]: ' prefix. Treating dck as a user-side participant (rather
      than another assistant) is closer to how multi-party chat works
      in practice — Cena sees dck as a third voice in the room, not a
      separate Cena saying contradictory things. The prefix marks the
      speaker so Cena can address dck specifically when summoned.
    - Other roles are dropped (defensive — model whitelist allows them
      but the API layer ignores anything not user/assistant/system).

    Consecutive same-role turns are merged into one, joined by '\\n\\n'.
    Without merging, a Sam->dck->Sam sequence becomes user->user->user."""
    mapped: list[dict] = []
    for m in rows:
        if m.role == "user":
            # Re-attach any images Sam sent on this turn so Cena can see
            # a screenshot referenced from an earlier message, not just
            # the current one (Sam #5:01 image-reading).
            _imgs = (images_by_msg or {}).get(getattr(m, "id", None)) or []
            mapped.append({"role": "user", "content": (
                [{"type": "text", "text": m.content}] + _imgs
                if _imgs else m.content)})
        elif m.role == "assistant":
            mapped.append({"role": "assistant",
                           "content": _strip_cena_tool_blocks(m.content)})
        elif (
            m.role == "system"
            and getattr(m, "model", None) == _ASSISTANT_REVIEW_MODEL
        ):
            context = _assistant_review_context(m.content)
            if context:
                mapped.append({"role": "user", "content": context})
        elif m.role == "dck":
            mapped.append({"role": "user",
                           "content": f"[dck]: {m.content}"})
        elif m.role == "aick":
            # Per Sam direct ask 2026-05-18: aick joins /sam/chat as a
            # third participant. Render as a user-tagged turn (parallel
            # to dck pattern) so Cena reads aick's posts as team-member
            # contributions to react to, not as her own prior assistant
            # turns. Behavior is summon-only by default (no auto-watcher
            # on aick side at commit time); follow-up could add a
            # /sam/chat poller on the aick side mirroring dck's
            # samples_watch.py pattern if Sam wants full-participant
            # auto-wake semantics (cena #2775 (a) + (b) still open).
            mapped.append({"role": "user",
                           "content": f"[aick]: {m.content}"})
        elif m.role == "cena":
            # Cena-injected posts from dev-chat-watcher-triggered turns
            # (per Sam #2533: cena uses post_to_sam_chat to surface
            # cross-channel acks). Treat as assistant turns from Cena
            # since she IS the canonical assistant in /sam/chat; tag
            # the body so future Cena reads see it as her own prior
            # post, not user input.
            mapped.append({"role": "assistant",
                           "content": _strip_cena_tool_blocks(
                               f"[cena (cross-channel)] {m.content}")})
        # else: skip (system rows etc. aren't sent in the conversation list)

    merged: list[dict] = []
    for msg in mapped:
        if merged and merged[-1]["role"] == msg["role"]:
            prev = merged[-1]["content"]
            curr = msg["content"]
            merged[-1]["content"] = _merge_content(prev, curr)
        else:
            merged.append(msg)
    return merged


def _load_prior_images(db, message_ids):
    """Return {message_id: [image content block, ...]} for prior /sam/chat
    turns that carried image attachments, so _build_api_messages_from_rows
    can re-attach them — Cena then sees screenshots from earlier in the
    chat, not just the current turn (Sam #5:01). Capped at the 8 most
    recent image attachments to bound per-turn token cost."""
    if not message_ids:
        return {}
    try:
        from app.models import SamChatAttachment as _SCA
        rows = (db.query(_SCA)
                .filter(_SCA.message_id.in_(message_ids))
                .filter(_SCA.content_type.like("image/%"))
                .order_by(_SCA.id.desc())
                .limit(8)
                .all())
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for a in rows:
        blk = {"type": "image", "source": {
            "type": "base64", "media_type": a.content_type,
            "data": a.data_base64}}
        out.setdefault(a.message_id, []).insert(0, blk)
    return out


def _estimate_tokens(text: str) -> int:
    """Very rough token estimate (~4 chars/token) for the soft
    context-window warning. Not precise — the SSE 'done' event carries
    the real per-turn usage once a turn completes."""
    return len(text or "") // 4


# ============================================================
# Serialization helpers
# ============================================================

def _session_json(s: SamChatSession, review_subject: str | None = None) -> dict:
    is_review = _is_assistant_review_session(s)
    title = s.title or "New chat"
    subject = _canonical_review_subject(
        review_subject or _review_subject_from_title(title))
    return {
        "id": s.id,
        "title": title,
        "is_assistant_review": is_review,
        "review_subject": subject,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "last_message_at": (s.last_message_at.isoformat()
                            if s.last_message_at else None),
        "is_archived": bool(s.is_archived),
    }


def _message_json(m: SamChatMessage) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "model": m.model,
        "is_assistant_review": m.model == _ASSISTANT_REVIEW_MODEL,
        "cost_usd": (str(m.cost_usd) if m.cost_usd is not None else None),
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _optional_int(raw) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _suggestion_json(row: SamChatSuggestion) -> dict:
    return {
        "id": row.id,
        "source_session_id": row.source_session_id,
        "source_message_id": row.source_message_id,
        "source_label": row.source_label,
        "summary": row.summary,
        "details": row.details,
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


def _suggestions_json(db, limit: int = 100) -> list[dict]:
    rows = (db.query(SamChatSuggestion)
            .order_by(SamChatSuggestion.id.desc())
            .limit(limit)
            .all())
    status_rank = {"pending": 0, "approved": 1, "denied": 2}
    rows.sort(key=lambda r: (status_rank.get(r.status, 9), -r.id))
    return [_suggestion_json(r) for r in rows]


def _session_cost(db, session_id: int) -> str:
    """Total cost_usd across a session's messages, as a string."""
    rows = (db.query(SamChatMessage.cost_usd)
            .filter(SamChatMessage.session_id == session_id)
            .filter(SamChatMessage.cost_usd.isnot(None))
            .all())
    total = sum((r[0] for r in rows), Decimal("0"))
    return str(total.quantize(Decimal("0.0001")))


def _cost_last_30d(db) -> str:
    """Total cost_usd across all messages in the last 30 days."""
    cutoff = datetime.utcnow() - timedelta(days=30)
    rows = (db.query(SamChatMessage.cost_usd)
            .filter(SamChatMessage.created_at >= cutoff)
            .filter(SamChatMessage.cost_usd.isnot(None))
            .all())
    total = sum((r[0] for r in rows), Decimal("0"))
    return str(total.quantize(Decimal("0.01")))


def _session_token_estimate(db, session_id: int) -> int:
    rows = (db.query(SamChatMessage.content)
            .filter(SamChatMessage.session_id == session_id)
            .all())
    return sum(_estimate_tokens(r[0]) for r in rows)


# ============================================================
# Attachment handling
# ============================================================

def _process_attachments(files):
    """Turn uploaded files into (api_blocks, text_appendix).

    - images (png/jpg/webp/gif) -> base64 image content blocks
    - PDFs                      -> base64 document blocks
    - text files                -> decoded + returned as a text appendix
      (the directive: "read content + paste into the user message")

    Enforces 5MB/file and 20MB/total. Raises ValueError on an oversize
    file or an unsupported type — the caller turns that into a 400.
    """
    api_blocks: list[dict] = []
    text_parts: list[str] = []
    total = 0
    for f in files:
        if not f or not f.filename:
            continue
        data = f.read()
        size = len(data)
        if size > _MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"{f.filename} is {size // 1024}KB — over the 5MB per-file limit")
        total += size
        if total > _MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("attachments exceed the 20MB per-message total")
        ext = (f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename
               else "")
        if ext in _IMAGE_MEDIA:
            api_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _IMAGE_MEDIA[ext],
                    "data": base64.b64encode(data).decode("ascii"),
                },
            })
        elif ext == "pdf":
            api_blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(data).decode("ascii"),
                },
            })
        elif ext in _TEXT_EXTS:
            try:
                body = data.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                raise ValueError(f"{f.filename}: could not read as text")
            text_parts.append(
                f"\n\n--- attached file: {f.filename} ---\n{body}\n--- end {f.filename} ---")
        else:
            raise ValueError(
                f"{f.filename}: unsupported type .{ext} "
                "(images, PDFs, and text files only)")
    return api_blocks, "".join(text_parts)


# ---- start files panel (Sam dev chat 2026-05-19 4:12pm — pin cena's
# auto-loaded start docs at the top of /sam/chat so the most recent
# copy is always visible at chat open). Read at request time from
# project root, embed server-side; collapsed by default so they don't
# dominate the view. List matches CENA_CHARTER.md autoload section.
_START_FILES = (
    "CENA_CHARTER.md",
    "CENA.md",
    "APP_STATUS.md",
    "plan.md",
    "tool.md",
)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _read_start_files() -> list[dict]:
    out: list[dict] = []
    for name in _START_FILES:
        p = _PROJECT_ROOT / name
        try:
            stat = p.stat()
            text = p.read_text(encoding="utf-8", errors="replace")
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            out.append({
                "name": name,
                "size_h": f"{stat.st_size:,} bytes",
                "lines": text.count("\n") + 1,
                "mtime_h": mtime,
                "content": text,
                "missing": False,
            })
        except FileNotFoundError:
            out.append({"name": name, "size_h": "—", "lines": 0,
                        "mtime_h": "—", "content": "", "missing": True})
        except Exception as e:  # noqa: BLE001
            out.append({"name": name, "size_h": "?", "lines": 0,
                        "mtime_h": "?", "content": f"(read failed: {e})",
                        "missing": False})
    return out


# Cena dev-chat feed (ck build-order #4, Sam dev chat #6:28 + #7:01).
# A Windows Task on aick's box appends each new Developer-Chat message
# to data/cena/cena_devchat_inbox.jsonl every minute via
# scripts/cena_devchat_relay.py. Each Cena turn reads the most-recent
# entries here and threads them as a system note so Cena always has
# fresh context without Sam relaying by hand.
_DEVCHAT_INBOX = _PROJECT_ROOT / "data" / "cena" / "cena_devchat_inbox.jsonl"
_DEVCHAT_FEED_N = 30


def _read_devchat_feed(n: int = _DEVCHAT_FEED_N) -> str:
    """Return the last `n` Developer-Chat messages as a formatted
    system-note string, or empty string if the inbox is missing/empty.

    Read-only + fast: tails the file, ignores parse errors per line.
    """
    try:
        if not _DEVCHAT_INBOX.exists():
            return ""
        lines = _DEVCHAT_INBOX.read_text(
            encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return ""
        tail = lines[-n:]
        rendered: list[str] = []
        for ln in tail:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except (ValueError, TypeError):
                continue
            mid = row.get("id")
            author = (row.get("author") or "").strip() or "?"
            body = (row.get("body") or "").strip()
            created = (row.get("created_at") or "").strip()
            head = f"#{mid} {author}" if mid is not None else author
            if created:
                head = f"[{created}] {head}"
            rendered.append(f"{head}: {body}")
        if not rendered:
            return ""
        return (
            "DEV CHAT FEED — auto-loaded for context "
            f"(latest {len(rendered)} of /partner/developer/chat). "
            "These messages were posted by Sam, aick, ck, dck, samai, "
            "and any other agents in the dev chat. Use as ambient "
            "context for what the team is doing right now; surface to "
            "Sam anything actionable.\n\n"
            + "\n".join(rendered)
        )
    except OSError:
        return ""


# ============================================================
# Routes
# ============================================================

@sam_chat_bp.route("/sam/chat", methods=["GET"])
def sam_chat_page():
    """The chat UI. Hard-gated to Sam. Preloads the session list + the
    requested (or most-recent) session's messages."""
    gate = _require_sam_page()
    if gate is not None:
        return gate

    db = SessionLocal()
    try:
        sessions = (db.query(SamChatSession)
                    .filter(SamChatSession.is_archived.is_(False))
                    .order_by(SamChatSession.last_message_at.desc())
                    .all())
        requested = request.args.get("session", type=int)
        current = None
        if requested is not None:
            current = db.get(SamChatSession, requested)
        if current is None and sessions:
            current = sessions[0]

        messages = []
        token_estimate = 0
        session_cost = "0.0000"
        if current is not None:
            messages = (db.query(SamChatMessage)
                        .filter(SamChatMessage.session_id == current.id)
                        .order_by(SamChatMessage.created_at.asc(),
                                  SamChatMessage.id.asc())
                        .all())
            token_estimate = _session_token_estimate(db, current.id)
            session_cost = _session_cost(db, current.id)

        # Source-of-truth model (Sam directive #276 2026-05-23): this
        # surface now exposes Gemini 2.5 Flash only. Gateway state is
        # still read for compatibility, but stale non-Gemini values fall
        # back to _DEFAULT_MODEL.
        return render_template(
            "sam_chat.html",
            active="sam_chat",
            sessions=[
                _session_json(s, _review_subject_for_session(db, s))
                for s in sessions
            ],
            current_session=(
                _session_json(
                    current,
                    _review_subject_for_session(db, current, messages),
                )
                if current else None
            ),
            messages=[_message_json(m) for m in messages],
            models=[{"id": m, "label": _MODEL_LABELS[m]}
                    for m in _PICKER_MODELS],
            default_model=_gateway_active_model_get(),
            session_cost=session_cost,
            cost_30d=_cost_last_30d(db),
            token_estimate=token_estimate,
            context_warn_tokens=_CONTEXT_WARN_TOKENS,
            start_files=_read_start_files(),
            suggestions=_suggestions_json(db),
            # Phase 2 §9 — when on, the page's JS uses the async queue
            # flow (send / poll-2s / history-restore) instead of SSE.
            sam_chat_async=_sam_chat_async_enabled(),
        )
    finally:
        db.close()


@sam_chat_bp.route("/sam/combined", methods=["GET"])
def sam_chat_combined():
    """Retired Cena + Dev combined chat surface."""
    return redirect("/assistant")


@sam_chat_bp.route("/sam/agents", methods=["GET"])
def sam_agents_page():
    """Cena-page tab: roster of every agent on the system - who they are,
    what they do, where they run, and their starting docs. Sam directive
    2026-05-22 #188 + #200. Cena owns the canonical content (#189), the
    template assembles the profiles from the hub posts the team filed.
    Hard-gated to Sam (this surface is internal-only)."""
    gate = _require_sam_page()
    if gate is not None:
        return gate
    return render_template("sam_agents.html", active="sam_agents")


@sam_chat_bp.route("/sam/pass", methods=["GET"])
def sam_pass_page():
    """Cena-page tab: the credentials index - every login, API token,
    and communication channel + WHERE the secret lives (1Password / env
    file / TBD). NEVER the secret values themselves; only the location
    tag. Sam directive 2026-05-22 #200. Inventory folds aick's #204
    sweep (25 L1 files + 6 L2 uniques). Hard-gated to Sam."""
    gate = _require_sam_page()
    if gate is not None:
        return gate
    return render_template("sam_pass.html", active="sam_pass")


@sam_chat_bp.route("/sam/automation", methods=["GET"])
def sam_automation_page():
    """Cena-page tab: every automated job currently running - Render
    cron jobs, always-on background services, Windows scheduled tasks,
    IMAP polling, gateway auto-mirrors, and third-party API
    integrations. For each entry: trigger, runner, secret/API touched,
    purpose. Sam directive 2026-05-23. Hard-gated to Sam."""
    gate = _require_sam_page()
    if gate is not None:
        return gate
    return render_template("sam_automation.html", active="sam_automation")


@sam_chat_bp.route("/sam/chat/active-model", methods=["GET"])
def sam_chat_active_model_get():
    """Return the canonical active model the dropdown should reflect.
    Sam directive #276 (2026-05-23): one knob, propagates everywhere.
    Sam-only audience."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    return jsonify({"ok": True, "model": _gateway_active_model_get()})


@sam_chat_bp.route("/sam/chat/active-model", methods=["POST"])
def sam_chat_active_model_set():
    """Persist Sam's new model selection through the gateway. JS calls
    this when the user changes the picker so the cena_sam_chat handler
    (and any other surface that reads cena_active_model.txt) picks up
    the change on the very next turn."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    body = request.get_json(silent=True) or {}
    raw = body.get("model")
    if not isinstance(raw, str) or not raw.strip():
        return jsonify({"ok": False, "error": "model required"}), 400
    m = raw.strip()
    if m not in _ALLOWED_MODELS:
        return jsonify({"ok": False,
                        "error": f"model {m!r} not in allowed set"}), 400
    ok, info = _gateway_active_model_set(m)
    if not ok:
        return jsonify({"ok": False, "error": info}), 502
    return jsonify({"ok": True, "model": info})


# ============================================================
# Fix / improve suggestions under /sam/chat.
# These rows are approval candidates generated from chat observations.
# They do not become TODOs unless Sam approves or manually moves them.
# ============================================================

@sam_chat_bp.route("/sam/chat/suggestions", methods=["GET"])
def sam_chat_suggestions_list():
    gate = _require_sam_api()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        return jsonify({"ok": True, "suggestions": _suggestions_json(db)})
    finally:
        db.close()


@sam_chat_bp.route("/sam/chat/suggestions", methods=["POST"])
def sam_chat_suggestions_add():
    gate = _require_sam_api()
    if gate is not None:
        return gate
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = request.form.to_dict()
    summary = (body.get("summary") or "").strip()
    details = (body.get("details") or "").strip()
    source_label = (body.get("source_label") or "").strip()
    created_by = (body.get("created_by")
                  or _current_sam_display_name()
                  or "Sam").strip()
    if not summary:
        return jsonify({"ok": False, "error": "summary required"}), 400
    row = SamChatSuggestion(
        source_session_id=_optional_int(body.get("source_session_id")),
        source_message_id=_optional_int(body.get("source_message_id")),
        source_label=source_label[:160] or None,
        summary=summary[:220],
        details=details or None,
        status="pending",
        created_by=created_by[:80] or None,
    )
    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
        return jsonify({"ok": True,
                        "suggestion": _suggestion_json(row)}), 201
    finally:
        db.close()


@sam_chat_bp.route("/sam/chat/suggestions/<int:suggestion_id>/decision",
                   methods=["POST"])
def sam_chat_suggestions_decide(suggestion_id: int):
    gate = _require_sam_api()
    if gate is not None:
        return gate
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = request.form.to_dict()
    status = (body.get("status") or "").strip().lower()
    if (status not in _VALID_SAM_CHAT_SUGGESTION_STATUS
            or status == "pending"):
        return jsonify({"ok": False,
                        "error": "status must be approved or denied"}), 400
    db = SessionLocal()
    try:
        row = db.get(SamChatSuggestion, suggestion_id)
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        row.status = status
        row.decided_at = datetime.utcnow()
        row.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(row)
        return jsonify({"ok": True,
                        "suggestion": _suggestion_json(row)})
    finally:
        db.close()


# ============================================================
# TODO list under /sam/chat (Sam directive 2026-05-23 #563).
# Sam writes items in for the team to complete; Cena must work them
# top-down (no skipping). All fields Sam-filled — the routes refuse
# empty details and empty date_added on add. UI surfaces ▲▼ for
# reorder, a check for done, and an inline edit.
#
# Auth: Sam-only for mutations + the full list. The /current endpoint
# is dual-gated (Sam session OR X-Cena-Token) so Cena's gateway tool
# can read the top priority without Sam's browser session cookie.
# ============================================================

def _todo_render(t) -> dict:
    """Serialise a SamChatTodo row to JSON-safe dict for the UI."""
    return {
        "id": t.id,
        "details": t.details,
        "date_added": t.date_added.isoformat() if t.date_added else None,
        "date_completed": (t.date_completed.isoformat()
                           if t.date_completed else None),
        "position": t.position,
        "status": t.status,
    }


def _parse_iso_date(raw):
    """date|None from an ISO yyyy-mm-dd string. Returns None on empty
    or unparseable input rather than raising — the route turns None
    into a 400 itself when the field is required."""
    from datetime import date as _date_cls
    if raw is None:
        return None
    s = (raw or "").strip() if isinstance(raw, str) else ""
    if not s:
        return None
    try:
        return _date_cls.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _renumber_active(db) -> None:
    """Pull active todos' positions tight (1,2,3,...) after any
    insert / delete / status change / move. Keeps get_current_todo
    deterministic — the top is always position=1."""
    from app.models import SamChatTodo as _SCTD
    rows = (db.query(_SCTD)
            .filter(_SCTD.status == "active")
            .order_by(_SCTD.position.asc(), _SCTD.id.asc())
            .all())
    for new_pos, row in enumerate(rows, start=1):
        if row.position != new_pos:
            row.position = new_pos


def _cena_token_value() -> str:
    """Read CENA_GATEWAY_TOKEN env (Cena's shared gateway auth) for
    the dual-gate path on /sam/chat/todos/current. Same env var the
    rest of /sam/cena/* checks."""
    raw = (os.getenv("CENA_GATEWAY_TOKEN") or "").strip()
    return raw


def _require_sam_or_cena_token():
    """Gate for /sam/chat/todos/current — accept Sam's session OR a
    matching X-Cena-Token header. Returns a Response to short-circuit
    or None to proceed."""
    if is_sam_chat_user():
        return None
    want = _cena_token_value()
    got = request.headers.get("X-Cena-Token", "")
    if want and got == want:
        return None
    return jsonify({"ok": False, "error": "forbidden"}), 403


@sam_chat_bp.route("/sam/chat/todos", methods=["GET"])
def sam_chat_todos_list():
    """List Sam's TODOs. Returns {active: [...], done: [...]} so the UI
    can render the two sections cleanly. Active sorted by position;
    done sorted by date_completed desc."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    from app.models import SamChatTodo
    db = SessionLocal()
    try:
        active = (db.query(SamChatTodo)
                  .filter(SamChatTodo.status == "active")
                  .order_by(SamChatTodo.position.asc(),
                            SamChatTodo.id.asc())
                  .all())
        done = (db.query(SamChatTodo)
                .filter(SamChatTodo.status == "done")
                .order_by(SamChatTodo.date_completed.desc().nullslast(),
                          SamChatTodo.id.desc())
                .all())
        return jsonify({
            "ok": True,
            "active": [_todo_render(t) for t in active],
            "done": [_todo_render(t) for t in done],
        })
    finally:
        db.close()


@sam_chat_bp.route("/sam/chat/todos", methods=["POST"])
def sam_chat_todos_add():
    """Add a new TODO. details + date_added required (Sam's literal
    'everything has to be filled out by me'); date_completed is set
    later via PATCH. New row goes to the bottom of the active list
    (largest position); Sam can ▲ it to the top as needed."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    body = request.get_json(silent=True) or {}
    details = (body.get("details") or "").strip()
    date_added = _parse_iso_date(body.get("date_added"))
    if not details:
        return jsonify({"ok": False,
                        "error": "details required"}), 400
    if date_added is None:
        return jsonify({"ok": False,
                        "error": "date_added required (yyyy-mm-dd)"}), 400
    from app.models import SamChatTodo
    db = SessionLocal()
    try:
        # New row appends to the bottom of the active list.
        max_pos = (db.query(SamChatTodo.position)
                   .filter(SamChatTodo.status == "active")
                   .order_by(SamChatTodo.position.desc())
                   .limit(1).scalar()) or 0
        row = SamChatTodo(
            details=details,
            date_added=date_added,
            position=max_pos + 1,
            status="active",
        )
        db.add(row)
        db.flush()
        # No renumber needed for a clean append, but call it to defend
        # against any prior holes from out-of-band edits.
        _renumber_active(db)
        db.commit()
        db.refresh(row)
        return jsonify({"ok": True, "todo": _todo_render(row)}), 201
    finally:
        db.close()


@sam_chat_bp.route("/sam/chat/todos/<int:tid>", methods=["PATCH"])
def sam_chat_todos_edit(tid: int):
    """Edit any field on a TODO. Setting date_completed to a real date
    flips status to 'done' and renumbers active. Setting it back to
    null flips status to 'active' and appends to the bottom of the
    active list."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    body = request.get_json(silent=True) or {}
    from app.models import SamChatTodo, _VALID_SAM_CHAT_TODO_STATUS
    db = SessionLocal()
    try:
        row = db.get(SamChatTodo, tid)
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        # details
        if "details" in body:
            new_details = (body.get("details") or "").strip()
            if not new_details:
                return jsonify({"ok": False,
                                "error": "details cannot be empty"}), 400
            row.details = new_details
        # date_added
        if "date_added" in body:
            new_added = _parse_iso_date(body.get("date_added"))
            if new_added is None:
                return jsonify({"ok": False,
                                "error": "date_added must be yyyy-mm-dd"}), 400
            row.date_added = new_added
        # date_completed (drives status)
        if "date_completed" in body:
            raw = body.get("date_completed")
            if raw in (None, "", False):
                # Cleared — flip back to active, position to bottom.
                row.date_completed = None
                row.status = "active"
                max_pos = (db.query(SamChatTodo.position)
                           .filter(SamChatTodo.status == "active",
                                   SamChatTodo.id != row.id)
                           .order_by(SamChatTodo.position.desc())
                           .limit(1).scalar()) or 0
                row.position = max_pos + 1
            else:
                parsed = _parse_iso_date(raw)
                if parsed is None:
                    return jsonify({"ok": False,
                                    "error": "date_completed must be yyyy-mm-dd"}), 400
                row.date_completed = parsed
                row.status = "done"
                # position is meaningless once done — leave the value
                # alone (audit trail), renumber will tighten the
                # remaining actives.
        # status override (rarely used; PATCH date_completed is the
        # primary path). Validate against the allow-list.
        if "status" in body:
            st = (body.get("status") or "").strip().lower()
            if st not in _VALID_SAM_CHAT_TODO_STATUS:
                return jsonify({"ok": False,
                                "error": f"status must be one of {sorted(_VALID_SAM_CHAT_TODO_STATUS)}"}), 400
            row.status = st
        _renumber_active(db)
        db.commit()
        db.refresh(row)
        return jsonify({"ok": True, "todo": _todo_render(row)})
    finally:
        db.close()


@sam_chat_bp.route("/sam/chat/todos/<int:tid>/move", methods=["POST"])
def sam_chat_todos_move(tid: int):
    """Reorder one TODO up or down by one slot in the active list.
    Body: {"direction": "up" | "down"}. A no-op (already at the
    boundary) is OK — returns the unchanged row so the UI re-renders
    the same state without erroring."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    body = request.get_json(silent=True) or {}
    direction = (body.get("direction") or "").strip().lower()
    if direction not in ("up", "down"):
        return jsonify({"ok": False,
                        "error": "direction must be 'up' or 'down'"}), 400
    from app.models import SamChatTodo
    db = SessionLocal()
    try:
        row = db.get(SamChatTodo, tid)
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        if row.status != "active":
            return jsonify({"ok": False,
                            "error": "only active todos reorder"}), 400
        # Find the neighbor in the move direction.
        if direction == "up":
            neighbor = (db.query(SamChatTodo)
                        .filter(SamChatTodo.status == "active",
                                SamChatTodo.position < row.position)
                        .order_by(SamChatTodo.position.desc())
                        .first())
        else:
            neighbor = (db.query(SamChatTodo)
                        .filter(SamChatTodo.status == "active",
                                SamChatTodo.position > row.position)
                        .order_by(SamChatTodo.position.asc())
                        .first())
        if neighbor is not None:
            row.position, neighbor.position = neighbor.position, row.position
        _renumber_active(db)
        db.commit()
        db.refresh(row)
        return jsonify({"ok": True, "todo": _todo_render(row)})
    finally:
        db.close()


@sam_chat_bp.route("/sam/chat/todos/<int:tid>", methods=["DELETE"])
def sam_chat_todos_delete(tid: int):
    """Remove a TODO outright. Renumbers active so the list stays
    tight. (Use cases: Sam adds something by mistake; Sam decides a
    done item shouldn't even live in the Done section.)"""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    from app.models import SamChatTodo
    db = SessionLocal()
    try:
        row = db.get(SamChatTodo, tid)
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        db.delete(row)
        _renumber_active(db)
        db.commit()
        return jsonify({"ok": True, "deleted": tid})
    finally:
        db.close()


@sam_chat_bp.route("/sam/chat/todos/current", methods=["GET"])
def sam_chat_todos_current():
    """Return the single top active TODO (position=1) or null if the
    list is empty. Dual-gated: Sam session OR X-Cena-Token, because
    Cena's gateway tool calls this from outside Sam's browser context
    to enforce the no-skip rule (work on position=1 only)."""
    gate = _require_sam_or_cena_token()
    if gate is not None:
        return gate
    from app.models import SamChatTodo
    db = SessionLocal()
    try:
        row = (db.query(SamChatTodo)
               .filter(SamChatTodo.status == "active")
               .order_by(SamChatTodo.position.asc(),
                         SamChatTodo.id.asc())
               .first())
        if row is None:
            return jsonify({"ok": True, "current": None,
                            "note": "no active TODOs"})
        return jsonify({"ok": True, "current": _todo_render(row)})
    finally:
        db.close()


@sam_chat_bp.route("/sam/docs", methods=["GET"])
def sam_docs_page():
    """Cena-page tab: consolidated project documentation. Sam directive
    #240 (2026-05-23) — the new docs surface that replaces the prior
    /partner/developer/app/* sidebar section. 10 nested tabs, content
    rendered inline as Jinja includes from app/templates/sam_docs/.
    Zero agent attribution by design. Hard-gated to Sam."""
    gate = _require_sam_page()
    if gate is not None:
        return gate
    return render_template("sam_docs.html", active="sam_docs")


@sam_chat_bp.route("/sam/chat/sessions", methods=["GET"])
def sam_chat_list_sessions():
    """JSON list of non-archived sessions, most-recent first."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        sessions = (db.query(SamChatSession)
                    .filter(SamChatSession.is_archived.is_(False))
                    .order_by(SamChatSession.last_message_at.desc())
                    .all())
        return jsonify({"ok": True,
                        "sessions": [
                            _session_json(s, _review_subject_for_session(db, s))
                            for s in sessions
                        ]})
    finally:
        db.close()


@sam_chat_bp.route("/sam/chat/sessions", methods=["POST"])
def sam_chat_new_session():
    """Create a fresh empty session. Returns its id."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        s = SamChatSession(started_at=now, last_message_at=now)
        db.add(s)
        db.commit()
        db.refresh(s)
        return jsonify({"ok": True, "session": _session_json(s)})
    finally:
        db.close()


@sam_chat_bp.route("/sam/chat/sessions/<int:session_id>", methods=["GET"])
def sam_chat_load_session(session_id: int):
    """JSON: a session + its messages, oldest-first."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        s = db.get(SamChatSession, session_id)
        if s is None:
            return jsonify({"ok": False, "error": "session not found"}), 404
        messages = (db.query(SamChatMessage)
                    .filter(SamChatMessage.session_id == session_id)
                    .order_by(SamChatMessage.created_at.asc(),
                              SamChatMessage.id.asc())
                    .all())
        return jsonify({
            "ok": True,
            "session": _session_json(
                s,
                _review_subject_for_session(db, s, messages),
            ),
            "messages": [_message_json(m) for m in messages],
            "session_cost": _session_cost(db, session_id),
            "token_estimate": _session_token_estimate(db, session_id),
        })
    finally:
        db.close()


@sam_chat_bp.route("/sam/chat/sessions/<int:session_id>/rename",
                   methods=["POST"])
def sam_chat_rename_session(session_id: int):
    """Rename a session (form field `title`)."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    title = (request.form.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "title is required"}), 400
    db = SessionLocal()
    try:
        s = db.get(SamChatSession, session_id)
        if s is None:
            return jsonify({"ok": False, "error": "session not found"}), 404
        s.title = title[:120]
        s.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "session": _session_json(s)})
    finally:
        db.close()


@sam_chat_bp.route("/sam/chat/sessions/<int:session_id>/archive",
                   methods=["POST"])
def sam_chat_archive_session(session_id: int):
    """Archive a session — drops it from the history sidebar."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        s = db.get(SamChatSession, session_id)
        if s is None:
            return jsonify({"ok": False, "error": "session not found"}), 404
        s.is_archived = True
        s.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


def _sse(event: dict) -> str:
    """Format one Server-Sent Events frame."""
    return f"data: {json.dumps(event)}\n\n"


@sam_chat_bp.route("/sam/chat/send", methods=["POST"])
def sam_chat_send():
    """Send a user message to Cenas AI and SSE-stream the reply back.

    multipart/form-data:
      session_id  — optional; a new session is created when absent
      message     — the user's text (required unless attachments present)
      model       — gemini-2.5-flash
      attachments — 0..N files (images / PDFs / text)

    The user message + attachment text are persisted BEFORE streaming
    (so a stream failure never loses the user's turn). The assistant
    message + its token cost are persisted when the stream completes.
    """
    gate = _require_sam_api()
    if gate is not None:
        return gate

    message = (request.form.get("message") or "").strip()
    model = (request.form.get("model") or _DEFAULT_MODEL).strip()
    if model not in _ALLOWED_MODELS:
        model = _auto_select_model(message)
    raw_session_id = (request.form.get("session_id") or "").strip()

    # Attachments -> API content blocks + a text appendix.
    try:
        api_blocks, text_appendix = _process_attachments(
            request.files.getlist("attachments"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if not message and not api_blocks and not text_appendix:
        return jsonify({"ok": False, "error": "message is empty"}), 400

    # The persisted user content = typed text + any text-file bodies.
    # Images/PDFs are send-time only (not persisted — Sam Chat model
    # spec; flagged in SamChatMessage's docstring).
    stored_content = (message + text_appendix) if text_appendix else message
    if not stored_content:
        stored_content = "(attachments only)"

    # --- persist the user turn + build the API history (before stream) ---
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        session_row = None
        if raw_session_id:
            try:
                session_row = db.get(SamChatSession, int(raw_session_id))
            except ValueError:
                pass
        if session_row is None:
            session_row = SamChatSession(started_at=now, last_message_at=now)
            db.add(session_row)
            db.flush()
        session_id = session_row.id

        # Prior turns -> model message list (user/assistant only;
        # the API takes 'system' separately and Sam Chat creates none).
        # dck rows map to user-side turns with '[dck]: ' prefix and
        # consecutive same-role turns are merged for API alternation —
        # see _build_api_messages_from_rows for the full mapping rules
        # (Track 8b per Sam #2236). Assistant content has cena-gateway
        # tool blocks stripped via _strip_cena_tool_blocks — see that
        # function for the confabulation-substrate rationale (Sam #2148,
        # samai #2154).
        prior = (db.query(SamChatMessage)
                 .filter(SamChatMessage.session_id == session_id)
                 .order_by(SamChatMessage.created_at.asc(),
                           SamChatMessage.id.asc())
                 .all())
        _prior_imgs = _load_prior_images(db, [m.id for m in prior])
        api_messages = _build_api_messages_from_rows(prior, _prior_imgs)

        # Persist the user message and capture its id so the Cena audit
        # log rows can link back to this exact chat turn.
        user_row = SamChatMessage(session_id=session_id, role="user",
                                  content=stored_content, created_at=now)
        db.add(user_row)
        db.flush()  # assigns user_row.id
        user_message_id = user_row.id

        # Per Sam #837 item 5 — persist any image/PDF attachments so the
        # dev-team agents (aick/ck/samai) can fetch them via the
        # /sam/cena/sam-chat read endpoint. Cena saw them inline at the
        # API layer (api_blocks above), but they were thrown away after
        # the turn until this row landed. Cap at 5MB pre-base64.
        try:
            from app.models import SamChatAttachment as _SCA
            for blk in (api_blocks or []):
                src = blk.get("source") or {}
                if src.get("type") != "base64":
                    continue
                data_b64 = src.get("data") or ""
                if not data_b64:
                    continue
                if len(data_b64) > 7_500_000:  # ~5.5MB binary
                    continue
                media_type = src.get("media_type") or "application/octet-stream"
                db.add(_SCA(
                    message_id=user_message_id,
                    filename=None,
                    content_type=media_type,
                    data_base64=data_b64,
                ))
        except Exception:  # noqa: BLE001
            pass
        # Auto-title a fresh session from its first user message.
        if not session_row.title:
            session_row.title = (message or stored_content)[:60].strip() \
                or "New chat"
        session_row.last_message_at = now
        session_row.updated_at = now
        db.commit()
        session_title = session_row.title
    finally:
        db.close()

    # Sam #292 (2026-05-23): mirror Sam's typed message into the
    # cena_sam_chat surface so the local view shows the full /sam/chat
    # turn alongside the LAN-hub + dev-chat mirrors. Author is forced to
    # "sam-online" so the cena_sam_chat wake-on-sam trigger DOES NOT
    # re-fire (Cena already gets the message via the /sam/chat path
    # below). Best-effort; mirror failure never blocks the chat.
    _mirror_to_cena_sam_chat("sam-online", stored_content)

    # The new user turn for the API: content blocks when there are
    # image/PDF attachments, else a plain string.
    if api_blocks:
        new_content = ([{"type": "text", "text": stored_content}]
                       + api_blocks)
    else:
        new_content = stored_content
    # If the last prior turn was already user-role (e.g. a recent dck
    # post mapped to user via _build_api_messages_from_rows), merge the
    # new Sam turn into it to preserve API alternation. String concat
    # for str+str; otherwise append as a separate entry.
    if api_messages and api_messages[-1]["role"] == "user":
        api_messages[-1]["content"] = _merge_content(
            api_messages[-1]["content"], new_content)
    else:
        api_messages.append({"role": "user", "content": new_content})

    # --- the SSE generator: stream, then persist the assistant turn ---
    def generate():
        full = ""
        in_tok = out_tok = 0
        cache_create_tok = cache_read_tok = 0
        gateway_url = _cena_gateway_url()
        try:
            # Cena dev-chat feed — auto-loaded per turn so Cena always
            # has fresh context without Sam relaying by hand. Empty
            # string when the inbox file is missing (e.g. before the
            # Windows Task wires up on aick) — falls through to no-op.
            cena_devchat_feed = _read_devchat_feed()

            if gateway_url:
                # ---- Cena gateway: route to aick ----
                # CENA_PROXY (e.g. socks5h://localhost:1055) routes the
                # outbound call through Render's userspace tailscaled —
                # required because userspace mode doesn't intercept OS
                # syscalls, so a direct TCP connect to a 100.x tailnet IP
                # would time out. Unset for local dev where the gateway
                # is reachable directly.
                import httpx
                cena_token = os.getenv("CENA_GATEWAY_TOKEN", "")
                _proxy = os.getenv("CENA_PROXY") or None
                _client_kwargs = {"timeout": 120.0}
                if _proxy:
                    _client_kwargs["proxy"] = _proxy
                with httpx.Client(**_client_kwargs) as hx:
                    _gw_body = {"messages": api_messages, "model": model,
                                "max_tokens": _MAX_OUTPUT_TOKENS,
                                "session_id": session_id,
                                "message_id": user_message_id}
                    if cena_devchat_feed:
                        _gw_body["system"] = cena_devchat_feed
                    with hx.stream(
                        "POST", gateway_url + "/cena/stream",
                        # session_id + message_id let the gateway link
                        # each CenaActionLog row back to this chat turn.
                        # system carries the auto-loaded dev-chat feed.
                        json=_gw_body,
                        headers={"X-Cena-Token": cena_token,
                                 "Content-Type": "application/json"},
                    ) as r:
                        for line in r.iter_lines():
                            if not line.startswith("data: "):
                                continue
                            try:
                                evt = json.loads(line[6:])
                            except Exception:
                                continue
                            if evt.get("type") == "delta":
                                chunk = evt.get("text", "")
                                full += chunk
                                yield _sse({"type": "delta", "text": chunk})
                            elif evt.get("type") == "done":
                                in_tok = evt.get("in_tokens", 0) or 0
                                out_tok = evt.get("out_tokens", 0) or 0
                                cache_create_tok = evt.get(
                                    "cache_creation_input_tokens", 0) or 0
                                cache_read_tok = evt.get(
                                    "cache_read_input_tokens", 0) or 0
                            elif evt.get("type") == "error":
                                raise RuntimeError(
                                    evt.get("error", "Cena gateway error"))
            else:
                # ---- Google Gemini: direct API (gateway-down fallback) ----
                # Normal production uses the Cena gateway so tool/context
                # orchestration stays centralized. This direct API path is
                # the fallback for local dev or an unwired gateway.
                gc = _gemini_client()
                if gc is None:
                    yield _sse({"type": "error",
                                "error": "GEMINI_API_KEY not configured"})
                    return
                from google.genai import types as _gtypes  # type: ignore[import]

                def _gemini_parts(raw) -> list:
                    if not isinstance(raw, list):
                        return [_gtypes.Part.from_text(text=str(raw))]
                    parts = []
                    for block in raw:
                        if not isinstance(block, dict):
                            continue
                        kind = block.get("type")
                        if kind == "text":
                            parts.append(_gtypes.Part.from_text(
                                text=str(block.get("text") or "")))
                            continue
                        if kind not in ("image", "document"):
                            continue
                        source = block.get("source") or {}
                        if source.get("type") != "base64":
                            continue
                        try:
                            parts.append(_gtypes.Part.from_bytes(
                                data=base64.b64decode(source.get("data") or ""),
                                mime_type=(
                                    source.get("media_type")
                                    or "application/octet-stream"
                                ),
                            ))
                        except Exception:  # noqa: BLE001
                            continue
                    return parts or [_gtypes.Part.from_text(text="")]

                gemini_contents = []
                for _m in api_messages:
                    _role = "model" if _m["role"] == "assistant" else "user"
                    gemini_contents.append(_gtypes.Content(
                        role=_role,
                        parts=_gemini_parts(_m["content"]),
                    ))
                _gemini_cfg_kwargs: dict = {
                    "max_output_tokens": _MAX_OUTPUT_TOKENS,
                }
                if cena_devchat_feed:
                    _gemini_cfg_kwargs["system_instruction"] = cena_devchat_feed
                for _chunk in gc.models.generate_content_stream(
                    model=model,
                    contents=gemini_contents,
                    config=_gtypes.GenerateContentConfig(**_gemini_cfg_kwargs),
                ):
                    if _chunk.text:
                        full += _chunk.text
                        yield _sse({"type": "delta", "text": _chunk.text})
                # Gemini streaming doesn't expose per-chunk usage;
                # rough estimate from character counts (÷4 ≈ tokens).
                in_tok = sum(len(str(_m.get("content", ""))) // 4
                             for _m in api_messages)
                out_tok = len(full) // 4
        except Exception as e:  # noqa: BLE001
            logger.exception("sam_chat: stream failed")
            # Persist whatever streamed before the failure so the turn
            # isn't silently lost; flag it.
            if full.strip():
                _persist_assistant(session_id, full, model, in_tok, out_tok,
                                   cache_create_tok=cache_create_tok,
                                   cache_read_tok=cache_read_tok)
            yield _sse({"type": "error",
                        "error": f"stream failed: {e}"})
            return

        if not full.strip():
            yield _sse({"type": "error",
                        "error": "no text received — please try again"})
            return

        cost = _estimate_cost(model, in_tok, out_tok,
                              cache_create_tok=cache_create_tok,
                              cache_read_tok=cache_read_tok)
        msg_id = _persist_assistant(session_id, full, model, in_tok,
                                    out_tok, cost,
                                    cache_create_tok=cache_create_tok,
                                    cache_read_tok=cache_read_tok)
        # Final event — metadata for the cost display + history refresh.
        d = SessionLocal()
        try:
            yield _sse({
                "type": "done",
                "message_id": msg_id,
                "session_id": session_id,
                "session_title": session_title,
                "model": model,
                "cost_usd": str(cost),
                "session_cost": _session_cost(d, session_id),
                "cost_30d": _cost_last_30d(d),
                "token_estimate": _session_token_estimate(d, session_id),
            })
        finally:
            d.close()

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream")


# ============================================================
# Phase 2 §9 — async routes (active only when SAM_CHAT_ASYNC is set)
# ============================================================

@sam_chat_bp.route("/sam/chat/async/send", methods=["POST"])
def sam_chat_async_send():
    """Phase 2 §9 — async send. Enqueues the user's message on a gateway
    and returns IMMEDIATELY with {message_id, conversation_id}; the
    gateway's background worker produces the answer and the UI polls
    /sam/chat/async/poll for it. The message bodies live in the
    gateway's cena_message_log (spec §3) — the SamChatSession row is
    kept only for the sidebar (title / archive)."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    if not _sam_chat_async_enabled():
        return jsonify({"ok": False, "error": "async mode disabled"}), 409
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    model = str(body.get("model") or _DEFAULT_MODEL).strip()
    if model not in _ALLOWED_MODELS:
        model = _auto_select_model(message)
    if not message:
        return jsonify({"ok": False, "error": "message is empty"}), 400
    raw_session_id = body.get("session_id")

    # Resolve / create the SamChatSession (sidebar metadata only).
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        session_row = None
        if raw_session_id:
            try:
                session_row = db.get(SamChatSession, int(raw_session_id))
            except (ValueError, TypeError):
                pass
        if session_row is None:
            session_row = SamChatSession(started_at=now, last_message_at=now)
            db.add(session_row)
            db.flush()
        session_id = session_row.id
        if not session_row.title:
            session_row.title = message[:60].strip() or "New chat"
        session_row.last_message_at = now
        session_row.updated_at = now
        db.commit()
    finally:
        db.close()

    conversation_id = f"samchat-{session_id}"
    message_id = uuid.uuid4().hex
    name, status, jbody = _gw_message_call(
        "POST", "/cena/messages/send", json_body={
            "message_id": message_id,
            "idempotency_key": message_id,
            "conversation_id": conversation_id,
            "user_id": str(_sam_chat_user_id() or "sam"),
            "model": model,
            "content": message})
    if status is None:
        return jsonify({"ok": False,
                        "error": "no gateway reachable"}), 502
    if status >= 400:
        return jsonify({"ok": False, "error": (jbody or {}).get(
            "error", f"gateway returned {status}")}), 502
    return jsonify({"ok": True, "message_id": message_id,
                    "conversation_id": conversation_id,
                    "session_id": session_id, "gateway": name})


@sam_chat_bp.route("/sam/chat/async/poll", methods=["GET"])
def sam_chat_async_poll():
    """Phase 2 §9 — the 2-second poll. Proxies the gateway's
    GET /cena/messages/status for the conversation; the UI calls this
    every 2s while a message is in flight, then stops."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    if not _sam_chat_async_enabled():
        return jsonify({"ok": False, "error": "async mode disabled"}), 409
    conv = (request.args.get("conversation_id") or "").strip()
    if not conv:
        return jsonify({"ok": False,
                        "error": "conversation_id required"}), 400
    name, status, jbody = _gw_message_call(
        "GET", "/cena/messages/status",
        params={"conversation_id": conv,
                "since": request.args.get("since", "")})
    if status is None:
        return jsonify({"ok": False, "error": "no gateway reachable"}), 502
    return jsonify(jbody if isinstance(jbody, dict)
                   else {"ok": False}), status


@sam_chat_bp.route("/sam/chat/async/history", methods=["GET"])
def sam_chat_async_history():
    """Phase 2 §9 — thread restore on page load. Proxies the gateway's
    GET /cena/messages/history so reopening /sam/chat shows the full
    conversation, including a message still being worked."""
    gate = _require_sam_api()
    if gate is not None:
        return gate
    if not _sam_chat_async_enabled():
        return jsonify({"ok": False, "error": "async mode disabled"}), 409
    conv = (request.args.get("conversation_id") or "").strip()
    if not conv:
        return jsonify({"ok": False,
                        "error": "conversation_id required"}), 400
    name, status, jbody = _gw_message_call(
        "GET", "/cena/messages/history",
        params={"conversation_id": conv})
    if status is None:
        return jsonify({"ok": False, "error": "no gateway reachable"}), 502
    return jsonify(jbody if isinstance(jbody, dict)
                   else {"ok": False}), status


def _persist_assistant(session_id: int, content: str, model: str,
                       in_tok: int, out_tok: int,
                       cost: Decimal | None = None,
                       cache_create_tok: int = 0,
                       cache_read_tok: int = 0) -> int | None:
    """Append the assistant SamChatMessage + bump the session. Its own
    session — the assistant turn is recorded independent of the request
    transaction. Returns the new message id (or None on failure).

    Sam #292 (2026-05-23): mirror Cena's reply into cena_sam_chat too
    so the local unified view sees the full conversation. Done AFTER
    the DB commit so a mirror failure can never lose the reply itself."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        row = SamChatMessage(
            session_id=session_id, role="assistant",
            content=content or "(empty response)", model=model,
            cost_input_tokens=in_tok or None,
            cost_output_tokens=out_tok or None,
            cost_cache_creation_tokens=cache_create_tok or None,
            cost_cache_read_tokens=cache_read_tok or None,
            cost_usd=cost, created_at=now,
        )
        db.add(row)
        s = db.get(SamChatSession, session_id)
        if s is not None:
            s.last_message_at = now
            s.updated_at = now
        db.commit()
        db.refresh(row)
        result_id = row.id
    except Exception:  # noqa: BLE001
        logger.exception("sam_chat: failed to persist assistant message")
        db.rollback()
        return None
    finally:
        db.close()
    # Post-commit mirror to cena_sam_chat. Outside the try/except so a
    # mirror error doesn't poison the return value (the row IS already
    # persisted at this point). Author "cena-online" so cena_sam_chat
    # renders it as a Cena turn but does NOT trigger any wake.
    _mirror_to_cena_sam_chat("cena-online",
                             content or "(empty response)")
    return result_id


def install(app):
    """Register the blueprint + the is_sam_chat_user Jinja global (the
    sidebar link uses it). Mirrors the auth / keypad / perms / ribbon
    install-pattern."""
    app.register_blueprint(sam_chat_bp)
    app.jinja_env.globals["is_sam_chat_user"] = is_sam_chat_user
