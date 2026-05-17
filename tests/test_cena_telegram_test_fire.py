"""Tests for POST /sam/cena/telegram-test-fire — Track 2 gateway
trigger for the app-hosted Telegram sender.

Covers:
  - X-Cena-Token gate (missing/wrong -> 403)
  - text required + non-empty
  - text max length 1000
  - happy path forwards to telegram_send and returns its result
  - telegram_send=False returns ok:false (not 500)
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def app_with_gateway_token(monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")
    monkeypatch.setenv("CENA_GATEWAY_TOKEN", "testtoken")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _post(client, body, token="testtoken"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Cena-Token"] = token
    return client.post(
        "/sam/cena/telegram-test-fire",
        data=json.dumps(body),
        headers=headers,
    )


def test_missing_token_403(app_with_gateway_token):
    r = _post(app_with_gateway_token, {"text": "hi"}, token=None)
    assert r.status_code == 403


def test_wrong_token_403(app_with_gateway_token):
    r = _post(app_with_gateway_token, {"text": "hi"}, token="nope")
    assert r.status_code == 403


def test_text_required(app_with_gateway_token):
    r = _post(app_with_gateway_token, {})
    assert r.status_code == 400


def test_text_whitespace_rejected(app_with_gateway_token):
    r = _post(app_with_gateway_token, {"text": "   "})
    assert r.status_code == 400


def test_text_max_length(app_with_gateway_token):
    r = _post(app_with_gateway_token, {"text": "x" * 1001})
    assert r.status_code == 400


def test_happy_path_forwards_to_telegram_send(
        app_with_gateway_token, monkeypatch):
    """Mock telegram_send to verify the endpoint forwards correctly
    without actually hitting Telegram's API."""
    calls = []

    def _fake_send(text):
        calls.append(text)
        return True, {"ok": True, "result": {"message_id": 42}}

    import app.web.produce_order as po
    monkeypatch.setattr(po, "telegram_send", _fake_send)

    r = _post(app_with_gateway_token, {"text": "samai gate-3 probe"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["telegram_response"]["ok"] is True
    assert calls == ["samai gate-3 probe"]


def test_send_failure_returns_ok_false_not_500(
        app_with_gateway_token, monkeypatch):
    """When telegram_send returns False (e.g. no token configured),
    the endpoint reports ok:false with the upstream message —
    not a 500 — so samai can distinguish 'endpoint reachable but
    telegram path broken' from 'endpoint broken'."""
    def _fake_send_fail(text):
        return False, "no telegram token"

    import app.web.produce_order as po
    monkeypatch.setattr(po, "telegram_send", _fake_send_fail)

    r = _post(app_with_gateway_token, {"text": "probe"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is False
    assert "no telegram token" in body["telegram_response"]
