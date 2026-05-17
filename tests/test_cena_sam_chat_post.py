"""Tests for POST /sam/cena/sam-chat-post — Track 8b dck write path.

Per Sam #2236: dck reads /sam/chat (Track 8a) AND now writes back
when summoned. This endpoint inserts a SamChatMessage row with
role='dck'; the conversation-flow side (mapping dck → user with
'[dck]: ' prefix when feeding Anthropic) is covered in
test_sam_chat.py.

Covers:
  - Dual-auth (X-Cena-Token OR partner session)
  - role='dck' default + only allowed role via this endpoint
  - session_id required + must exist
  - content required + non-empty + max length
  - last_message_at bump on the session
  - row appears in the subsequent GET /sam/cena/sam-chat read
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.models import SamChatSession, SamChatMessage


_NOW = datetime(2026, 5, 17, 22, 0, 0)


@pytest.fixture
def app_with_session(db_session, monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")
    monkeypatch.setenv("CENA_GATEWAY_TOKEN", "testtoken")
    monkeypatch.setenv("SAM_CHAT_USER_ID", "1")

    import app.db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    import app.web.cena as cena_mod
    monkeypatch.setattr(cena_mod, "SessionLocal", lambda: db_session)

    s1 = SamChatSession(started_at=_NOW, last_message_at=_NOW)
    db_session.add(s1)
    db_session.commit()

    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client(), s1.id, db_session


def _post(client, body, token="testtoken"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Cena-Token"] = token
    return client.post(
        "/sam/cena/sam-chat-post",
        data=json.dumps(body),
        headers=headers,
    )


def test_missing_auth_403(app_with_session):
    client, sid, _db = app_with_session
    r = _post(client, {"session_id": sid, "content": "hello"}, token=None)
    assert r.status_code == 403


def test_wrong_token_403(app_with_session):
    client, sid, _db = app_with_session
    r = _post(client, {"session_id": sid, "content": "hello"}, token="nope")
    assert r.status_code == 403


def test_partner_session_auth_alternative(app_with_session):
    """Mirrors test_partner_session_auth_alternative from the read
    endpoint — dck self-auths via partner session, no X-Cena-Token."""
    client, sid, _db = app_with_session
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
        sess["partner_auth_ok"] = True
    r = client.post(
        "/sam/cena/sam-chat-post",
        data=json.dumps({"session_id": sid, "content": "dck partner-auth hi"}),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["role"] == "dck"


def test_happy_path_creates_dck_row(app_with_session):
    client, sid, db = app_with_session
    r = _post(client, {"session_id": sid,
                       "content": "yes, that matches #2210"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["role"] == "dck"
    assert body["session_id"] == sid
    assert "id" in body
    # Row should be persisted with role='dck'
    row = db.get(SamChatMessage, body["id"])
    assert row is not None
    assert row.role == "dck"
    assert row.content == "yes, that matches #2210"


def test_session_id_required(app_with_session):
    client, _sid, _db = app_with_session
    r = _post(client, {"content": "hi"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_session_id_must_exist(app_with_session):
    client, _sid, _db = app_with_session
    r = _post(client, {"session_id": 999999, "content": "hi"})
    assert r.status_code == 404


def test_content_required(app_with_session):
    client, sid, _db = app_with_session
    r = _post(client, {"session_id": sid})
    assert r.status_code == 400


def test_content_whitespace_rejected(app_with_session):
    client, sid, _db = app_with_session
    r = _post(client, {"session_id": sid, "content": "   \n\t  "})
    assert r.status_code == 400


def test_content_max_length_30000(app_with_session):
    client, sid, _db = app_with_session
    too_long = "x" * 30001
    r = _post(client, {"session_id": sid, "content": too_long})
    assert r.status_code == 400


def test_content_at_cap_accepted(app_with_session):
    client, sid, _db = app_with_session
    at_cap = "x" * 30000
    r = _post(client, {"session_id": sid, "content": at_cap})
    assert r.status_code == 200


def test_role_user_rejected(app_with_session):
    """Per endpoint spec — only 'dck' is allowed here. user/assistant
    rows go through the streaming pathway, not this side-door."""
    client, sid, _db = app_with_session
    r = _post(client, {"session_id": sid, "content": "hi", "role": "user"})
    assert r.status_code == 400


def test_role_assistant_rejected(app_with_session):
    client, sid, _db = app_with_session
    r = _post(client, {"session_id": sid, "content": "hi",
                       "role": "assistant"})
    assert r.status_code == 400


def test_role_garbage_rejected(app_with_session):
    client, sid, _db = app_with_session
    r = _post(client, {"session_id": sid, "content": "hi",
                       "role": "haxxor"})
    assert r.status_code == 400


def test_session_last_message_at_bumped(app_with_session):
    client, sid, db = app_with_session
    original_last = db.get(SamChatSession, sid).last_message_at
    r = _post(client, {"session_id": sid, "content": "ping"})
    assert r.status_code == 200
    # Refetch (the endpoint closed its db session, detaching the row)
    updated_last = db.get(SamChatSession, sid).last_message_at
    assert updated_last >= original_last


def test_dck_row_appears_in_read_endpoint(app_with_session):
    """End-to-end: POST a dck row, then GET via the read endpoint
    confirms the row is visible."""
    client, sid, _db = app_with_session
    r = _post(client, {"session_id": sid,
                       "content": "dck observed reply"})
    assert r.status_code == 200
    posted_id = r.get_json()["id"]

    g = client.get(
        f"/sam/cena/sam-chat?session_id={sid}&include_all=true",
        headers={"X-Cena-Token": "testtoken"},
    )
    assert g.status_code == 200
    body = g.get_json()
    matched = [m for m in body["messages"] if m["id"] == posted_id]
    assert len(matched) == 1
    assert matched[0]["role"] == "dck"
    assert matched[0]["content"] == "dck observed reply"
