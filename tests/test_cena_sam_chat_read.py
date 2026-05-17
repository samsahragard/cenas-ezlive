"""Tests for GET /sam/cena/sam-chat — dck's read-only Sam Chat surface.

Covers:
  - Auth gate: missing/wrong token -> 403
  - Default window (now - SAM_CHAT_READ_DEFAULT_HOURS) bounds the read
  - include_all=true ignores window
  - explicit since= overrides default
  - session_id filter restricts to one session
  - limit enforced (default 30, max 200)
  - response shape carries id + session_id + role + content +
    model + created_at
  - content truncation at 2000 chars
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.models import SamChatSession, SamChatMessage


_NOW = datetime(2026, 5, 17, 22, 0, 0)
_T_OLD = _NOW - timedelta(hours=48)
_T_RECENT = _NOW - timedelta(hours=1)


@pytest.fixture
def app_with_sam_chat_data(db_session, monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")
    monkeypatch.setenv("CENA_GATEWAY_TOKEN", "testtoken")
    monkeypatch.setenv("SAM_CHAT_USER_ID", "1")

    import app.db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    import app.web.cena as cena_mod
    monkeypatch.setattr(cena_mod, "SessionLocal", lambda: db_session)

    s1 = SamChatSession(started_at=_T_OLD, last_message_at=_T_RECENT)
    s2 = SamChatSession(started_at=_T_OLD, last_message_at=_T_RECENT)
    db_session.add_all([s1, s2])
    db_session.flush()

    # Spread messages: some old (outside default window), some recent.
    db_session.add_all([
        SamChatMessage(session_id=s1.id, role="user",
                       content="old user msg", created_at=_T_OLD),
        SamChatMessage(session_id=s1.id, role="assistant",
                       content="old reply", model="claude-sonnet-4-6",
                       created_at=_T_OLD + timedelta(minutes=1)),
        SamChatMessage(session_id=s1.id, role="user",
                       content="recent user", created_at=_T_RECENT),
        SamChatMessage(session_id=s1.id, role="assistant",
                       content="recent reply", model="claude-sonnet-4-6",
                       created_at=_T_RECENT + timedelta(minutes=1)),
        SamChatMessage(session_id=s2.id, role="user",
                       content="other-session msg",
                       created_at=_T_RECENT + timedelta(minutes=2)),
    ])
    db_session.commit()

    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client(), s1.id, s2.id


def _get(client, path, token="testtoken", **params):
    headers = {"X-Cena-Token": token} if token else {}
    return client.get(path, query_string=params, headers=headers)


def test_missing_token_403(app_with_sam_chat_data):
    client, _s1, _s2 = app_with_sam_chat_data
    r = _get(client, "/sam/cena/sam-chat", token=None)
    assert r.status_code == 403


def test_wrong_token_403(app_with_sam_chat_data):
    client, _s1, _s2 = app_with_sam_chat_data
    r = _get(client, "/sam/cena/sam-chat", token="nope")
    assert r.status_code == 403


def test_default_window_excludes_old_messages(app_with_sam_chat_data, monkeypatch):
    # Default SAM_CHAT_READ_DEFAULT_HOURS=24; old=48h, recent=1h.
    # Old messages should be EXCLUDED. Only the 3 recent ones returned.
    client, _s1, _s2 = app_with_sam_chat_data
    r = _get(client, "/sam/cena/sam-chat")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    contents = [m["content"] for m in body["messages"]]
    assert "old user msg" not in contents
    assert "old reply" not in contents
    assert "recent user" in contents
    assert "recent reply" in contents
    assert "other-session msg" in contents
    assert body["default_window_hours"] == 24


def test_include_all_returns_old_messages(app_with_sam_chat_data):
    client, _s1, _s2 = app_with_sam_chat_data
    r = _get(client, "/sam/cena/sam-chat", include_all="true")
    assert r.status_code == 200
    body = r.get_json()
    contents = [m["content"] for m in body["messages"]]
    assert "old user msg" in contents
    assert "recent user" in contents
    assert body["window_start"] is None


def test_session_id_filter_restricts_to_one_session(app_with_sam_chat_data):
    client, s1, s2 = app_with_sam_chat_data
    r = _get(client, "/sam/cena/sam-chat",
             include_all="true", session_id=s2)
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 1
    assert body["messages"][0]["session_id"] == s2
    assert body["messages"][0]["content"] == "other-session msg"


def test_limit_clamped_to_max_200(app_with_sam_chat_data):
    client, _s1, _s2 = app_with_sam_chat_data
    r = _get(client, "/sam/cena/sam-chat",
             include_all="true", limit="1000")
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] <= 200


def test_response_shape_has_required_fields(app_with_sam_chat_data):
    client, _s1, _s2 = app_with_sam_chat_data
    r = _get(client, "/sam/cena/sam-chat", include_all="true", limit=1)
    body = r.get_json()
    assert body["count"] == 1
    msg = body["messages"][0]
    assert set(msg.keys()) >= {
        "id", "session_id", "role", "content", "model", "created_at"
    }


def test_content_truncation_at_8000_chars_with_explicit_marker(
        app_with_sam_chat_data, db_session):
    """Per samai #2199 — cap at 8000ch + include explicit
    [truncated 8000/N chars] marker so dck-side reasoning can detect
    incomplete context (anti-confabulation discipline)."""
    client, s1, _s2 = app_with_sam_chat_data
    long_content = "x" * 12000  # 4000 over cap
    db_session.add(SamChatMessage(
        session_id=s1, role="assistant",
        content=long_content, created_at=_T_RECENT + timedelta(minutes=5)))
    db_session.commit()
    r = _get(client, "/sam/cena/sam-chat", limit=1)
    body = r.get_json()
    msg = body["messages"][0]
    assert "[truncated 8000/12000 chars]" in msg["content"]
    assert msg["content"].count("x") == 8000


def test_content_not_truncated_under_cap(app_with_sam_chat_data, db_session):
    """Messages under 8000ch don't get the marker — only the literal
    body is returned, exactly as stored."""
    client, s1, _s2 = app_with_sam_chat_data
    short_content = "y" * 500
    db_session.add(SamChatMessage(
        session_id=s1, role="assistant",
        content=short_content, created_at=_T_RECENT + timedelta(minutes=6)))
    db_session.commit()
    r = _get(client, "/sam/cena/sam-chat", limit=1)
    body = r.get_json()
    msg = body["messages"][0]
    assert msg["content"] == short_content
    assert "[truncated" not in msg["content"]


def test_session_id_invalid_returns_400(app_with_sam_chat_data):
    client, _s1, _s2 = app_with_sam_chat_data
    r = _get(client, "/sam/cena/sam-chat", session_id="not-int")
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False


def test_partner_session_auth_alternative(app_with_sam_chat_data):
    """Per Sam #2204 + Track 8 dck-self-auth: the endpoint accepts
    EITHER X-Cena-Token OR a partner-authenticated session. dck (no
    cena gateway token) self-auths via the partner_password she has.
    Validates the dual-path gate works for the partner path."""
    client, _s1, _s2 = app_with_sam_chat_data
    # Establish a partner-auth session without sending X-Cena-Token
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
        sess["partner_auth_ok"] = True
    r = client.get("/sam/cena/sam-chat?limit=3&include_all=true")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["count"] >= 1


def test_no_token_and_no_partner_session_still_403(app_with_sam_chat_data):
    """No X-Cena-Token AND no partner-auth -> 403. Closes the path
    that the partner-auth fallback adds."""
    client, _s1, _s2 = app_with_sam_chat_data
    r = client.get("/sam/cena/sam-chat")
    assert r.status_code == 403
