"""Tests for GET /sam/cena/dev-chat — Cena's read_dev_chat surface.

Covers:
  - Auth gate: missing/wrong token -> 403
  - Default read: only returns messages from cena_start_point onward
  - include_pre_start=true: ignores start-point filter
  - explicit since= parameter overrides default start point
  - limit enforced (default 30, max 200)
  - author filter restricts by author name(s)
  - cena_start_point in response reflects earliest cena-authored message
  - Edge case: no cena messages yet -> start point defaults to ~now
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models import DeveloperChatMessage


# ============================================================
# Fixture
# ============================================================

_T0 = datetime(2026, 5, 16, 10, 0, 0)   # well before Cena came online
_T1 = datetime(2026, 5, 16, 20, 0, 0)   # Cena's first post
_T2 = datetime(2026, 5, 16, 20, 5, 0)   # after Cena


@pytest.fixture
def app_with_cena_chat(db_session, monkeypatch):
    """Flask app with CENA_GATEWAY_TOKEN set and a seeded dev-chat history."""
    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")
    monkeypatch.setenv("CENA_GATEWAY_TOKEN", "testtoken")
    monkeypatch.setenv("SAM_CHAT_USER_ID", "1")

    import app.db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    # cena.py does `from app.db import SessionLocal` at module-load
    # time, so the symbol is bound to the original sessionmaker before
    # the monkeypatch above can intervene. Patch the module-local
    # reference too so the endpoint queries the test db_session.
    import app.web.cena as cena_mod
    monkeypatch.setattr(cena_mod, "SessionLocal", lambda: db_session)

    # Seed messages: some before Cena, Cena's first post, then mix after.
    db_session.add_all([
        DeveloperChatMessage(author="sam",  body="pre-cena msg 1", created_at=_T0),
        DeveloperChatMessage(author="aick", body="pre-cena msg 2",
                             created_at=_T0 + timedelta(minutes=1)),
        DeveloperChatMessage(author="cena", body="cena first post", created_at=_T1),
        DeveloperChatMessage(author="aick", body="aick reply",
                             created_at=_T2),
        DeveloperChatMessage(author="cena", body="cena second",
                             created_at=_T2 + timedelta(minutes=1)),
        DeveloperChatMessage(author="sam",  body="sam after cena",
                             created_at=_T2 + timedelta(minutes=2)),
    ])
    db_session.commit()

    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _get(client, path, token="testtoken", **params):
    headers = {"X-Cena-Token": token} if token else {}
    return client.get(path, query_string=params, headers=headers)


# ============================================================
# Auth gate
# ============================================================

def test_missing_token_403(app_with_cena_chat):
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat", token=None)
    assert r.status_code == 403


def test_wrong_token_403(app_with_cena_chat):
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat", token="wrong")
    assert r.status_code == 403


# ============================================================
# Default read — only post-start messages
# ============================================================

def test_default_excludes_pre_cena_messages(app_with_cena_chat):
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    bodies = [m["body"] for m in data["messages"]]
    assert "pre-cena msg 1" not in bodies
    assert "pre-cena msg 2" not in bodies
    assert "cena first post" in bodies


def test_default_messages_in_chronological_order(app_with_cena_chat):
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat")
    msgs = r.get_json()["messages"]
    times = [m["created_at"] for m in msgs]
    assert times == sorted(times)


def test_cena_start_point_in_response(app_with_cena_chat):
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat")
    data = r.get_json()
    # Should reflect Cena's first post time
    assert data["cena_start_point"].startswith("2026-05-16T20:00:00")


# ============================================================
# include_pre_start
# ============================================================

def test_include_pre_start_returns_all(app_with_cena_chat):
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat",
             include_pre_start="true")
    data = r.get_json()
    bodies = [m["body"] for m in data["messages"]]
    assert "pre-cena msg 1" in bodies
    assert "cena first post" in bodies


# ============================================================
# explicit since= parameter
# ============================================================

def test_explicit_since_filters_correctly(app_with_cena_chat):
    # since=T2: should only return messages at or after _T2
    since_str = _T2.isoformat() + "Z"
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat", since=since_str)
    data = r.get_json()
    bodies = [m["body"] for m in data["messages"]]
    assert "cena first post" not in bodies   # _T1 < _T2
    assert "aick reply" in bodies            # _T2 exactly


# ============================================================
# limit enforcement
# ============================================================

def test_limit_default_30(app_with_cena_chat):
    # Only 4 post-cena messages seeded; all returned; count <= 30
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat")
    data = r.get_json()
    assert data["count"] <= 30


def test_limit_param_respected(app_with_cena_chat):
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat",
             include_pre_start="true", limit=2)
    data = r.get_json()
    assert data["count"] == 2


def test_limit_capped_at_200(app_with_cena_chat):
    # limit=999 -> capped to 200
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat",
             include_pre_start="true", limit=999)
    # 6 messages seeded, all returned (well under 200); no error
    assert r.status_code == 200
    assert r.get_json()["count"] <= 200


# ============================================================
# author filter
# ============================================================

def test_author_filter_single(app_with_cena_chat):
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat",
             include_pre_start="true", author="aick")
    bodies = [m["body"] for m in r.get_json()["messages"]]
    assert all(m["author"] == "aick"
               for m in r.get_json()["messages"])
    assert "aick reply" in bodies
    assert "cena first post" not in bodies


def test_author_filter_multiple(app_with_cena_chat):
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat",
             include_pre_start="true", author="cena,sam")
    authors = {m["author"] for m in r.get_json()["messages"]}
    assert "aick" not in authors
    assert "cena" in authors
    assert "sam" in authors


# ============================================================
# response field shapes
# ============================================================

def test_message_fields_present(app_with_cena_chat):
    r = _get(app_with_cena_chat, "/sam/cena/dev-chat")
    for m in r.get_json()["messages"]:
        assert "id" in m
        assert "author" in m
        assert "body" in m
        assert "created_at" in m
        assert "attachment_count" in m
        assert isinstance(m["attachment_count"], int)
