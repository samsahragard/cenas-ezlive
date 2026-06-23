import json

import pytest

from app.services.toast_webhook_security import compute_toast_signature
from app.services.toast_webhook_store import ToastWebhookStore
from scripts import toast_webhook_receiver as receiver_mod
from scripts.toast_webhook_receiver import create_receiver_app


def test_receiver_requires_relay_token_and_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("TOAST_WEBHOOK_SEED_ON_START", "0")
    monkeypatch.setenv("TOAST_RELAY_TOKEN", "relay-secret")
    monkeypatch.setenv("TOAST_WEBHOOK_SIGNING_SECRET", "toast-secret")
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_TOMBALL", "restaurant-tomball")
    store = ToastWebhookStore(tmp_path / "toast.sqlite")
    app = create_receiver_app(store)
    client = app.test_client()

    payload = {
        "timestamp": "2026-06-05T23:00:00.000Z",
        "eventCategory": "order_updated",
        "eventType": "order_updated",
        "guid": "event-1",
        "details": {
            "restaurantGuid": "restaurant-tomball",
            "order": {
                "guid": "order-1",
                "server": {"guid": "toast-server-1"},
                "businessDate": 20260605,
                "checks": [],
            },
        },
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    signature = compute_toast_signature(raw, payload["timestamp"], "toast-secret")

    missing_token = client.post(
        "/ingest/toast/webhook",
        data=raw,
        headers={"Toast-Signature": signature, "Content-Type": "application/json"},
    )
    assert missing_token.status_code == 403

    bad_signature = client.post(
        "/ingest/toast/webhook",
        data=raw,
        headers={
            "Toast-Signature": "bad",
            "X-Cena-Relay-Token": "relay-secret",
            "Content-Type": "application/json",
        },
    )
    assert bad_signature.status_code == 401

    ok = client.post(
        "/ingest/toast/webhook",
        data=raw,
        headers={
            "Toast-Signature": signature,
            "Toast-Attempt-Number": "1",
            "X-Cena-Relay-Token": "relay-secret",
            "Content-Type": "application/json",
        },
    )
    assert ok.status_code == 200
    assert ok.get_json()["stored"] is True


def test_receiver_healthz_reports_store_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("TOAST_WEBHOOK_SEED_ON_START", "0")
    store = ToastWebhookStore(tmp_path / "toast.sqlite")
    app = create_receiver_app(store)
    client = app.test_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_receiver_retries_bind_when_network_address_is_late(monkeypatch):
    class Done(Exception):
        pass

    class FakeServer:
        def serve_forever(self):
            raise Done

    attempts = []
    sleeps = []

    def fake_make_server(host, port, app, threaded):
        attempts.append((host, port, threaded))
        if len(attempts) == 1:
            raise OSError("requested address is not valid in its context")
        return FakeServer()

    monkeypatch.setattr(receiver_mod, "make_server", fake_make_server)
    monkeypatch.setattr(receiver_mod.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(Done):
        receiver_mod._serve_one(object(), "100.73.38.82", 8784)

    assert attempts == [
        ("100.73.38.82", 8784, True),
        ("100.73.38.82", 8784, True),
    ]
    assert sleeps == [15]
