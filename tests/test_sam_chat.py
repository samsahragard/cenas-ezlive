"""Sam Chat – tests for the standalone /sam/chat surface.

Covers:
  - The access gate (the security boundary): is_sam_chat_user / the
    route gates. SAM_CHAT_USER_ID unset -> dormant/denied for everyone;
    set -> only the matching user gets in, everyone else 302/403.
  - _process_attachments: image -> API block, PDF -> block, text ->
    appendix; 5MB/file + 20MB/total limits; unknown type rejected.
  - _estimate_cost: token usage -> Decimal.
  - Models: SamChatSession / SamChatMessage round-trip.
  - Routes: page gate, session create/list/load/rename/archive, and
    /sam/chat/send happy-path (Anthropic client mocked) -> persists the
    user + assistant turns + SSE-streams the reply.

External calls (Anthropic) are mocked – no network, no key.
"""
from __future__ import annotations

import io
import json
from datetime import datetime
from decimal import Decimal

import pytest

import app.web.sam_chat as sc
from app.models import SamChatSession, SamChatMessage


# ============================================================
# Fakes
# ============================================================

def _filestorage(filename, data, content_type="application/octet-stream"):
    from werkzeug.datastructures import FileStorage
    return FileStorage(stream=io.BytesIO(data), filename=filename,
                       content_type=content_type)


class _FakeUsage:
    input_tokens = 120
    output_tokens = 60


class _FakeFinal:
    usage = _FakeUsage()


class _FakeStream:
    def __init__(self, text):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        for i in range(0, len(self._text), 6):
            yield self._text[i:i + 6]

    def get_final_message(self):
        return _FakeFinal()


class _FakeClient:
    def __init__(self, text):
        self._text = text

    class _Messages:
        def __init__(self, text):
            self._text = text

        def stream(self, **kwargs):
            return _FakeStream(self._text)

    @property
    def messages(self):
        return _FakeClient._Messages(self._text)


# ============================================================
# _estimate_cost – pure
# ============================================================

def test_estimate_cost_opus():
    c = sc._estimate_cost("claude-opus-4-7", 1_000_000, 1_000_000)
    # opus rate: 5.0 in + 25.0 out per Mtok -> $30.0000
    assert c == Decimal("30.0000")


def test_estimate_cost_zero_and_unknown_model():
    assert sc._estimate_cost("claude-opus-4-7", 0, 0) == Decimal("0.0000")
    assert sc._estimate_cost("mystery-model", 5000, 5000) == Decimal("0.0000")


def test_estimate_cost_with_cache_tokens_opus():
    # opus rate: 5.0/M input. 1M uncached + 1M cache_creation @2x + 1M
    # cache_read @0.10x + 0 output. 5 + 10 + 0.5 = 15.5
    c = sc._estimate_cost("claude-opus-4-7", 1_000_000, 0,
                          cache_create_tok=1_000_000,
                          cache_read_tok=1_000_000)
    assert c == Decimal("15.5000")


def test_estimate_cost_cache_kwargs_default_zero_preserves_backcompat():
    # Old callers that don't pass cache_create_tok / cache_read_tok must
    # see exactly the pre-cache cost (regression guard for callers in
    # sam_chat.py + chart/cron paths). Opus: 1M in @5 + 1M out @25 = 30.
    c = sc._estimate_cost("claude-opus-4-7", 1_000_000, 1_000_000)
    assert c == Decimal("30.0000")


def test_estimate_cost_cache_read_is_cheaper_than_uncached():
    # The cache_read pricing should make 1M cache-read tokens 10x cheaper
    # than 1M uncached input tokens, all else equal.
    uncached = sc._estimate_cost("claude-opus-4-7", 1_000_000, 0)
    cached   = sc._estimate_cost("claude-opus-4-7", 0, 0,
                                 cache_read_tok=1_000_000)
    assert cached < uncached
    assert cached == Decimal("0.5000")  # 5.0 * 0.10 = 0.50
    assert uncached == Decimal("5.0000")


# ============================================================
# _strip_cena_tool_blocks — confabulation-substrate removal
# (Sam #2148 + samai #2154 hybrid spec, closes lesson family
# #1865/#2042/#2122/#2138)
# ============================================================

def test_strip_tool_announcements_from_prior_assistant():
    """A real-shape assistant turn with one tool block + continuation
    has the block stripped + the terminal marker appended."""
    content = (
        "Checking dev chat for replies first.\n\n"
        "[read_dev_chat(limit=10)]\n"
        "→ 10 messages | start_point=2026-05-16T22:04Z\n"
        "[#2120 2026-05-17T20:00Z] aick: hi\n"
        "[#2121 2026-05-17T20:01Z] cena: hello\n\n"
        "Based on the chat, here's the summary..."
    )
    out = sc._strip_cena_tool_blocks(content)
    assert "[read_dev_chat(limit=10)]" not in out
    assert "→ 10 messages" not in out
    assert "[#2120" not in out
    assert "Checking dev chat for replies first." in out
    assert "Based on the chat" in out
    assert "stripped from context" in out


def test_strip_preserves_natural_language():
    """Assistant turn with no tool blocks is returned unchanged
    (no terminal marker)."""
    content = (
        "Got it. The driver workflow looks healthy.\n\n"
        "Three things to flag: (1) order 652 is approved, (2) "
        "Cooper is on shift, (3) keypad timed out at 14:02."
    )
    out = sc._strip_cena_tool_blocks(content)
    assert out == content  # exact identity, no edit
    assert "stripped from context" not in out


def test_strip_multi_tool_turn():
    """A turn with TWO tool blocks gets both stripped and ONE terminal
    marker appended (per samai #2154: 1-per-turn cap, not N-per-turn)."""
    content = (
        "First I'll check.\n\n"
        "[read_dev_chat(limit=5)]\n"
        "→ 5 messages\n[#1] author: body\n\n"
        "Then I'll post.\n\n"
        "[post_to_dev_chat(message='ok')]\n"
        "→ Posted to dev chat.\n\n"
        "Done."
    )
    out = sc._strip_cena_tool_blocks(content)
    assert "[read_dev_chat" not in out
    assert "[post_to_dev_chat" not in out
    assert "First I'll check." in out
    assert "Then I'll post." in out
    assert "Done." in out
    # Exactly one terminal marker even though two blocks stripped
    assert out.count("stripped from context") == 1


def test_terminal_marker_appended_when_strip_occurred():
    """Terminal marker presence is the signal that strip happened."""
    content = "Pre-text.\n\n[fetch_url(url='x')]\n→ response\n\nPost-text."
    out = sc._strip_cena_tool_blocks(content)
    assert out.endswith(sc._CENA_TOOL_STRIP_MARKER.rstrip()) or \
           sc._CENA_TOOL_STRIP_MARKER.strip() in out


def test_no_marker_when_no_strip():
    """No terminal marker appended when no blocks matched."""
    content = "Plain reasoning. Nothing tool-shaped here."
    out = sc._strip_cena_tool_blocks(content)
    assert "stripped from context" not in out
    assert out == content


# ============================================================
# _build_api_messages_from_rows — Track 8b dck mapping + merge
# (Sam #2236)
# ============================================================

class _RowLike:
    """Tiny stand-in for a SamChatMessage row in the mapper unit tests
    so we don't need a DB fixture for pure-function behavior."""
    def __init__(self, role, content):
        self.role = role
        self.content = content


def test_build_api_messages_user_assistant_passthrough():
    rows = [
        _RowLike("user", "hi"),
        _RowLike("assistant", "hello back"),
        _RowLike("user", "how are you?"),
    ]
    out = sc._build_api_messages_from_rows(rows)
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello back"},
        {"role": "user", "content": "how are you?"},
    ]


def test_build_api_messages_dck_maps_to_user_with_prefix():
    """A dck row alone becomes a user-side turn with '[dck]: ' prefix
    so Cena can see who spoke."""
    rows = [_RowLike("dck", "summon me when you need a second opinion")]
    out = sc._build_api_messages_from_rows(rows)
    assert out == [{"role": "user",
                    "content": "[dck]: summon me when you need a "
                               "second opinion"}]


def test_build_api_messages_merges_consecutive_user_after_dck():
    """Sam → dck → Sam (after mapping) is three consecutive user
    rows. The API requires alternation, so they merge into ONE user
    turn joined by blank lines."""
    rows = [
        _RowLike("user", "Sam asks Cena something"),
        _RowLike("dck", "dck chimes in unprompted"),
        _RowLike("user", "Sam follows up"),
    ]
    out = sc._build_api_messages_from_rows(rows)
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert "Sam asks Cena something" in out[0]["content"]
    assert "[dck]: dck chimes in unprompted" in out[0]["content"]
    assert "Sam follows up" in out[0]["content"]


def test_build_api_messages_assistant_then_dck_then_user_alternates():
    """assistant → dck → user becomes assistant → user(merged) — the
    dck and Sam turns merge into one user turn after the assistant."""
    rows = [
        _RowLike("user", "first sam ask"),
        _RowLike("assistant", "Cena replies"),
        _RowLike("dck", "dck weighs in"),
        _RowLike("user", "Sam's follow-up"),
    ]
    out = sc._build_api_messages_from_rows(rows)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[2]["content"] == "[dck]: dck weighs in\n\nSam's follow-up"


def test_build_api_messages_strips_tool_blocks_on_assistant():
    """Mapper still applies _strip_cena_tool_blocks to assistant
    content. Regression guard so the Track 8b refactor didn't lose
    the strip path."""
    rows = [
        _RowLike("user", "hi"),
        _RowLike("assistant",
                 "thinking.\n\n[read_dev_chat(limit=5)]\n→ 5 msgs\n"
                 "[#1] author: body\n\nDone."),
    ]
    out = sc._build_api_messages_from_rows(rows)
    assert out[0] == {"role": "user", "content": "hi"}
    # Assistant should have the tool block stripped + marker appended
    assert "[read_dev_chat" not in out[1]["content"]
    assert "stripped from context" in out[1]["content"]


def test_build_api_messages_drops_system_rows():
    """Non-user/assistant/dck rows are dropped from the API list."""
    rows = [
        _RowLike("user", "hi"),
        _RowLike("system", "internal note"),
        _RowLike("assistant", "ok"),
    ]
    out = sc._build_api_messages_from_rows(rows)
    assert [m["role"] for m in out] == ["user", "assistant"]


def test_build_api_messages_empty_returns_empty():
    assert sc._build_api_messages_from_rows([]) == []


# ============================================================
# _process_attachments – types + limits
# ============================================================

def test_process_attachments_image_to_block():
    blocks, appendix = sc._process_attachments(
        [_filestorage("shot.png", b"\x89PNG fake", "image/png")])
    assert appendix == ""
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[0]["source"]["type"] == "base64"


def test_process_attachments_pdf_to_block():
    blocks, appendix = sc._process_attachments(
        [_filestorage("doc.pdf", b"%PDF-1.7 fake", "application/pdf")])
    assert len(blocks) == 1
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"]["media_type"] == "application/pdf"


def test_process_attachments_text_to_appendix():
    blocks, appendix = sc._process_attachments(
        [_filestorage("notes.txt", b"hello from a text file", "text/plain")])
    assert blocks == []
    assert "notes.txt" in appendix
    assert "hello from a text file" in appendix


def test_process_attachments_oversize_file_rejected():
    big = b"x" * (sc._MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(ValueError, match="per-file limit"):
        sc._process_attachments([_filestorage("big.png", big, "image/png")])


def test_process_attachments_total_limit_rejected():
    each = b"x" * (4 * 1024 * 1024)  # 4MB each, 6 files = 24MB > 20MB
    files = [_filestorage(f"f{i}.png", each, "image/png") for i in range(6)]
    with pytest.raises(ValueError, match="20MB"):
        sc._process_attachments(files)


def test_process_attachments_unknown_type_rejected():
    with pytest.raises(ValueError, match="unsupported type"):
        sc._process_attachments(
            [_filestorage("malware.exe", b"MZ", "application/octet-stream")])


# ============================================================
# Models round-trip
# ============================================================

def test_sam_chat_models_roundtrip(db_session):
    now = datetime(2026, 5, 14, 12, 0, 0)
    s = SamChatSession(started_at=now, last_message_at=now, title="First chat")
    db_session.add(s)
    db_session.flush()
    db_session.add_all([
        SamChatMessage(session_id=s.id, role="user", content="hi",
                       created_at=now),
        SamChatMessage(session_id=s.id, role="assistant", content="hello",
                       model="claude-opus-4-7", cost_input_tokens=10,
                       cost_output_tokens=5, cost_usd=Decimal("0.0012"),
                       created_at=now),
    ])
    db_session.commit()

    sess = db_session.query(SamChatSession).one()
    assert sess.title == "First chat"
    assert sess.is_archived is False
    msgs = (db_session.query(SamChatMessage)
            .order_by(SamChatMessage.id).all())
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].model == "claude-opus-4-7"
    assert msgs[1].cost_usd == Decimal("0.0012")


# ============================================================
# Route fixture – mirrors test_briefs_routes.app_with_user
# ============================================================

@pytest.fixture
def app_with_sam(db_session, monkeypatch):
    """Flask app bound to the in-memory db_session, seeded with Sam
    (id=1, partner) + a non-Sam user (id=2). SAM_CHAT_USER_ID=1."""
    from app.models import User
    db_session.add_all([
        User(id=1, full_name="Sam Sahragard", email="sam@x.test",
             passcode_hash="x", permission_level="partner",
             active=True, first_login_done=True),
        User(id=2, full_name="Not Sam", email="notsam@x.test",
             passcode_hash="x", permission_level="gm",
             store_scope="tomball", active=True, first_login_done=True),
    ])
    db_session.commit()

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")
    monkeypatch.setenv("SAM_CHAT_USER_ID", "1")

    import app.db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(sc, "SessionLocal", lambda: db_session)

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True

    def _client_for(user_id):
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["auth_ok"] = True
            sess["partner_auth_ok"] = True
            sess["user_id"] = user_id
            sess["user_session_version"] = 1
        return c

    yield app, _client_for, db_session


# ============================================================
# The access gate – the security boundary
# ============================================================

def test_gate_blocks_non_sam_from_page(app_with_sam):
    _app, client_for, _db = app_with_sam
    # user 2 is not SAM_CHAT_USER_ID -> redirect to access-denied
    r = client_for(2).get("/sam/chat")
    assert r.status_code == 302
    assert "/access-denied" in r.headers["Location"]
    assert "need=sam_chat" in r.headers["Location"]


def test_gate_blocks_non_sam_from_api(app_with_sam):
    _app, client_for, _db = app_with_sam
    r = client_for(2).get("/sam/chat/sessions")
    assert r.status_code == 403


def test_gate_allows_sam(app_with_sam):
    _app, client_for, _db = app_with_sam
    r = client_for(1).get("/sam/chat")
    assert r.status_code == 200


def test_gate_dormant_when_env_unset(app_with_sam, monkeypatch):
    # SAM_CHAT_USER_ID unset -> nobody gets in, not even Sam (id=1).
    _app, client_for, _db = app_with_sam
    monkeypatch.delenv("SAM_CHAT_USER_ID", raising=False)
    r = client_for(1).get("/sam/chat")
    assert r.status_code == 302
    assert "/access-denied" in r.headers["Location"]


# ============================================================
# Model-picker enforcement: when Cena tools are wired (gateway
# URL set), the picker must exclude non-Claude models. Non-Claude
# models pattern-match bracketed tool-format text and emit a
# fabricated tool-call trail without any real tool firing.
# ============================================================

def test_model_picker_excludes_non_claude_for_cena_tool_conversation(
        app_with_sam, monkeypatch):
    _app, client_for, _db = app_with_sam
    monkeypatch.setenv("CENA_GATEWAY_URL", "https://cena.example.test")
    r = client_for(1).get("/sam/chat")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "claude-opus-4-7" in body
    assert "claude-sonnet-4-6" in body
    assert "gemini-2.5-flash" not in body


def test_model_picker_includes_full_list_for_non_cena_conversation(
        app_with_sam, monkeypatch):
    _app, client_for, _db = app_with_sam
    monkeypatch.delenv("CENA_GATEWAY_URL", raising=False)
    r = client_for(1).get("/sam/chat")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "claude-opus-4-7" in body
    assert "claude-sonnet-4-6" in body
    assert "gemini-2.5-flash" in body


# ============================================================
# Session CRUD routes
# ============================================================

def test_session_create_list_load(app_with_sam):
    _app, client_for, db = app_with_sam
    c = client_for(1)

    r = c.post("/sam/chat/sessions")
    assert r.status_code == 200
    sid = r.get_json()["session"]["id"]

    r = c.get("/sam/chat/sessions")
    assert r.status_code == 200
    assert any(s["id"] == sid for s in r.get_json()["sessions"])

    r = c.get(f"/sam/chat/sessions/{sid}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["messages"] == []


def test_session_rename_and_archive(app_with_sam):
    _app, client_for, db = app_with_sam
    c = client_for(1)
    sid = c.post("/sam/chat/sessions").get_json()["session"]["id"]

    r = c.post(f"/sam/chat/sessions/{sid}/rename", data={"title": "Renamed"})
    assert r.status_code == 200
    assert r.get_json()["session"]["title"] == "Renamed"

    r = c.post(f"/sam/chat/sessions/{sid}/archive")
    assert r.status_code == 200
    # archived sessions drop out of the list
    assert all(s["id"] != sid for s in
               c.get("/sam/chat/sessions").get_json()["sessions"])


def test_load_unknown_session_404(app_with_sam):
    _app, client_for, _db = app_with_sam
    r = client_for(1).get("/sam/chat/sessions/99999")
    assert r.status_code == 404


# ============================================================
# /sam/chat/send – happy path (Anthropic mocked)
# ============================================================

def test_send_streams_and_persists(app_with_sam, monkeypatch):
    _app, client_for, db = app_with_sam
    monkeypatch.setattr(sc, "_anthropic_client",
                        lambda: _FakeClient("Hello from Claude, Sam."))
    c = client_for(1)

    r = c.post("/sam/chat/send", data={
        "message": "What is 2+2?",
        "model": "claude-opus-4-7",
    })
    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"

    # Parse the SSE frames out of the response body.
    frames = [json.loads(line[5:].strip())
              for line in r.get_data(as_text=True).split("\n\n")
              if line.startswith("data:")]
    types = [f["type"] for f in frames]
    assert "delta" in types
    assert types[-1] == "done"
    done = frames[-1]
    assert done["session_title"]
    assert done["cost_usd"]

    # The streamed text reassembles to the full reply.
    streamed = "".join(f["text"] for f in frames if f["type"] == "delta")
    assert streamed == "Hello from Claude, Sam."

    # Both turns persisted.
    msgs = (db.query(SamChatMessage)
            .order_by(SamChatMessage.id).all())
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == "What is 2+2?"
    assert msgs[1].content == "Hello from Claude, Sam."
    assert msgs[1].model == "claude-opus-4-7"
    assert msgs[1].cost_usd is not None


def test_send_coerces_unknown_model(app_with_sam, monkeypatch):
    """Unknown/non-allowed models are auto-selected rather than rejected.
    The request succeeds (200) and the persisted assistant message uses
    a valid model."""
    _app, client_for, db = app_with_sam
    monkeypatch.setattr(sc, "_anthropic_client",
                        lambda: _FakeClient("Auto-selected reply."))
    r = client_for(1).post("/sam/chat/send", data={
        "message": "hi", "model": "gpt-4"})
    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    # Drain the SSE stream so the generator runs to completion and the
    # assistant turn actually gets persisted (mirrors the pattern in
    # test_send_streams_and_persists).
    r.get_data(as_text=True)
    msgs = (db.query(SamChatMessage)
            .order_by(SamChatMessage.id).all())
    # Last row is the assistant turn; user rows carry model=None.
    assert msgs[-1].role == "assistant"
    assert msgs[-1].model in sc._ALLOWED_MODELS


def test_send_rejects_empty_message(app_with_sam):
    _app, client_for, _db = app_with_sam
    r = client_for(1).post("/sam/chat/send", data={
        "message": "   ", "model": "claude-opus-4-7"})
    assert r.status_code == 400


def test_send_blocked_for_non_sam(app_with_sam):
    _app, client_for, _db = app_with_sam
    r = client_for(2).post("/sam/chat/send", data={
        "message": "hi", "model": "claude-opus-4-7"})
    assert r.status_code == 403
