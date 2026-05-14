"""Sam Chat — tests for the standalone /sam/chat surface.

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

External calls (Anthropic) are mocked — no network, no key.
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
# _estimate_cost — pure
# ============================================================

def test_estimate_cost_opus():
    c = sc._estimate_cost("claude-opus-4-7", 1_000_000, 1_000_000)
    # opus rate: 5.0 in + 25.0 out per Mtok -> $30.0000
    assert c == Decimal("30.0000")


def test_estimate_cost_zero_and_unknown_model():
    assert sc._estimate_cost("claude-opus-4-7", 0, 0) == Decimal("0.0000")
    assert sc._estimate_cost("mystery-model", 5000, 5000) == Decimal("0.0000")


# ============================================================
# _process_attachments — types + limits
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
# Route fixture — mirrors test_briefs_routes.app_with_user
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
# The access gate — the security boundary
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
# /sam/chat/send — happy path (Anthropic mocked)
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


def test_send_rejects_bad_model(app_with_sam):
    _app, client_for, _db = app_with_sam
    r = client_for(1).post("/sam/chat/send", data={
        "message": "hi", "model": "gpt-4"})
    assert r.status_code == 400


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
