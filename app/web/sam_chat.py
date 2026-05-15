"""Sam Chat — a standalone /sam/chat surface for Sam to converse with
Claude directly via the Anthropic API (Sam request 2026-05-14).

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
                                            Claude's reply back
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
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from flask import (
    Blueprint, Response, abort, g, jsonify, redirect, render_template,
    request, stream_with_context, url_for,
)

from app.db import SessionLocal
from app.models import SamChatSession, SamChatMessage, _VALID_SAM_CHAT_ROLES

logger = logging.getLogger(__name__)

sam_chat_bp = Blueprint("sam_chat", __name__)


# ---- model routing ----
# Sam's spec: Opus 4.7 default, Sonnet 4.6 as the faster/cheaper option.
# These are Sam's explicitly-chosen strings for this surface — NOT the
# codebase's claude-opus-4-5 / claude-haiku-4-5 (those are the agentic
# pipeline's; Sam Chat is Sam's personal surface, his model choice).
_DEFAULT_MODEL = "claude-opus-4-7"
_ALLOWED_MODELS = {"claude-opus-4-7", "claude-sonnet-4-6"}
_MODEL_LABELS = {
    "claude-opus-4-7": "Opus 4.7",
    "claude-sonnet-4-6": "Sonnet 4.6",
}
# Rough list-price estimates, USD per million tokens — for the cost
# display. NOT billing-grade (exact pricing for these models may not be
# public yet); update when known. cost_usd is stored per the spec so
# Sam has a running tally; treat it as "approximately".
_MODEL_RATES = {
    "claude-opus-4-7":   {"in": 5.0, "out": 25.0},
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0},
}

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
# Anthropic plumbing
# ============================================================

def _anthropic_client():
    """An anthropic.Anthropic client, or None if the SDK is missing or
    ANTHROPIC_API_KEY is unset."""
    try:
        import anthropic
    except ImportError:
        return None
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)

def _cena_gateway_url() -> str | None:
    """URL of Cena's gateway server on aick, e.g.
    https://cena-api.cenaskitchen.com  (set via CENA_GATEWAY_URL env var).
    When set, sam_chat routes to Cena instead of calling Anthropic directly.
    Returns None when the env var is absent — falls back to Anthropic."""
    url = (os.getenv("CENA_GATEWAY_URL") or "").strip().rstrip("/")
    return url or None


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> Decimal:
    """Rough USD cost estimate from token usage. Quantized to 4 places
    for the Numeric(10,4) column. Best-effort — see _MODEL_RATES."""
    rates = _MODEL_RATES.get(model, {"in": 0.0, "out": 0.0})
    usd = ((in_tok or 0) * rates["in"]
           + (out_tok or 0) * rates["out"]) / 1_000_000
    return Decimal(str(usd)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _estimate_tokens(text: str) -> int:
    """Very rough token estimate (~4 chars/token) for the soft
    context-window warning. Not precise — the SSE 'done' event carries
    the real per-turn usage once a turn completes."""
    return len(text or "") // 4


# ============================================================
# Serialization helpers
# ============================================================

def _session_json(s: SamChatSession) -> dict:
    return {
        "id": s.id,
        "title": s.title or "New chat",
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
        "cost_usd": (str(m.cost_usd) if m.cost_usd is not None else None),
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


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

    - images (png/jpg/webp/gif) -> base64 Anthropic image content blocks
    - PDFs                      -> base64 Anthropic document blocks
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

        return render_template(
            "sam_chat.html",
            active="sam_chat",
            sessions=[_session_json(s) for s in sessions],
            current_session=(_session_json(current) if current else None),
            messages=[_message_json(m) for m in messages],
            models=[{"id": m, "label": _MODEL_LABELS[m]}
                    for m in ("claude-opus-4-7", "claude-sonnet-4-6")],
            default_model=_DEFAULT_MODEL,
            session_cost=session_cost,
            cost_30d=_cost_last_30d(db),
            token_estimate=token_estimate,
            context_warn_tokens=_CONTEXT_WARN_TOKENS,
        )
    finally:
        db.close()


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
                        "sessions": [_session_json(s) for s in sessions]})
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
            "session": _session_json(s),
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
    """Send a user message to Claude and SSE-stream the reply back.

    multipart/form-data:
      session_id  — optional; a new session is created when absent
      message     — the user's text (required unless attachments present)
      model       — claude-opus-4-7 | claude-sonnet-4-6
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
        return jsonify({"ok": False, "error": f"unknown model {model!r}"}), 400
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

    client = _anthropic_client()
    if client is None:
        return jsonify({
            "ok": False,
            "error": "Anthropic API is not configured (ANTHROPIC_API_KEY).",
        }), 503

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

        # Prior turns -> Anthropic message list (user/assistant only;
        # the API takes 'system' separately and Sam Chat creates none).
        prior = (db.query(SamChatMessage)
                 .filter(SamChatMessage.session_id == session_id)
                 .order_by(SamChatMessage.created_at.asc(),
                           SamChatMessage.id.asc())
                 .all())
        api_messages = [{"role": m.role, "content": m.content}
                        for m in prior if m.role in ("user", "assistant")]

        # Persist the user message.
        db.add(SamChatMessage(session_id=session_id, role="user",
                              content=stored_content, created_at=now))
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

    # The new user turn for the API: content blocks when there are
    # image/PDF attachments, else a plain string.
    if api_blocks:
        new_content = ([{"type": "text", "text": stored_content}]
                       + api_blocks)
    else:
        new_content = stored_content
    api_messages.append({"role": "user", "content": new_content})

    # --- the SSE generator: stream, then persist the assistant turn ---
    def generate():
        full = ""
        in_tok = out_tok = 0
        gateway_url = _cena_gateway_url()
        try:
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
                    with hx.stream(
                        "POST", gateway_url + "/cena/stream",
                        json={"messages": api_messages, "model": model,
                              "max_tokens": _MAX_OUTPUT_TOKENS},
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
                            elif evt.get("type") == "error":
                                raise RuntimeError(
                                    evt.get("error", "Cena gateway error"))
            else:
                # ---- Direct Anthropic API (original path) ----
                with client.messages.stream(
                    model=model,
                    max_tokens=_MAX_OUTPUT_TOKENS,
                    messages=api_messages,
                ) as stream:
                    for chunk in stream.text_stream:
                        full += chunk
                        yield _sse({"type": "delta", "text": chunk})
                    final = stream.get_final_message()
                usage = getattr(final, "usage", None)
                in_tok = getattr(usage, "input_tokens", 0) or 0
                out_tok = getattr(usage, "output_tokens", 0) or 0
        except Exception as e:  # noqa: BLE001
            logger.exception("sam_chat: stream failed")
            # Persist whatever streamed before the failure so the turn
            # isn't silently lost; flag it.
            if full.strip():
                _persist_assistant(session_id, full, model, in_tok, out_tok)
            yield _sse({"type": "error",
                        "error": f"stream failed: {e}"})
            return

        cost = _estimate_cost(model, in_tok, out_tok)
        msg_id = _persist_assistant(session_id, full, model, in_tok,
                                    out_tok, cost)
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


def _persist_assistant(session_id: int, content: str, model: str,
                       in_tok: int, out_tok: int,
                       cost: Decimal | None = None) -> int | None:
    """Append the assistant SamChatMessage + bump the session. Its own
    session — the assistant turn is recorded independent of the request
    transaction. Returns the new message id (or None on failure)."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        row = SamChatMessage(
            session_id=session_id, role="assistant",
            content=content or "(empty response)", model=model,
            cost_input_tokens=in_tok or None,
            cost_output_tokens=out_tok or None,
            cost_usd=cost, created_at=now,
        )
        db.add(row)
        s = db.get(SamChatSession, session_id)
        if s is not None:
            s.last_message_at = now
            s.updated_at = now
        db.commit()
        db.refresh(row)
        return row.id
    except Exception:  # noqa: BLE001
        logger.exception("sam_chat: failed to persist assistant message")
        db.rollback()
        return None
    finally:
        db.close()


def install(app):
    """Register the blueprint + the is_sam_chat_user Jinja global (the
    sidebar link uses it). Mirrors the auth / keypad / perms / ribbon
    install-pattern."""
    app.register_blueprint(sam_chat_bp)
    app.jinja_env.globals["is_sam_chat_user"] = is_sam_chat_user
