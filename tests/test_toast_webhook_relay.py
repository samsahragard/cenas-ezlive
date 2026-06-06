import json

from flask import Flask

from app.services.toast_webhook_security import compute_toast_signature
from app.web import toast_webhook
from app.web.toast_webhook import toast_webhook_bp


def test_public_relay_spools_and_forwards(tmp_path, monkeypatch):
    monkeypatch.setenv("TOAST_WEBHOOK_SIGNING_SECRET", "toast-secret")
    monkeypatch.setenv("TOAST_RELAY_TOKEN", "relay-secret")
    monkeypatch.setenv("TOAST_WEBHOOK_RELAY_SPOOL_DIR", str(tmp_path))
    forwarded = {}

    def fake_forward(raw_body, headers):
        forwarded["raw_body"] = raw_body
        forwarded["headers"] = headers
        return True, 200, None

    monkeypatch.setattr(toast_webhook, "_forward_to_ck", fake_forward)
    app = Flask(__name__)
    app.register_blueprint(toast_webhook_bp)
    client = app.test_client()
    payload = {
        "timestamp": "2026-06-05T23:00:00.000Z",
        "eventCategory": "order_updated",
        "eventType": "order_updated",
        "guid": "event-1",
        "details": {"restaurantGuid": "restaurant-tomball", "order": {"guid": "order-1"}},
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    signature = compute_toast_signature(raw, payload["timestamp"], "toast-secret")

    response = client.post(
        "/api/toast/webhook",
        data=raw,
        headers={
            "Toast-Signature": signature,
            "Toast-Attempt-Number": "1",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["forwarded"] is True
    assert forwarded["raw_body"] == raw
    spooled = list(tmp_path.glob("*.json"))
    assert len(spooled) == 1
    assert json.loads(spooled[0].read_text(encoding="utf-8"))["forwarded"] is True


def test_public_relay_rejects_bad_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("TOAST_WEBHOOK_SIGNING_SECRET", "toast-secret")
    monkeypatch.setenv("TOAST_WEBHOOK_RELAY_SPOOL_DIR", str(tmp_path))
    app = Flask(__name__)
    app.register_blueprint(toast_webhook_bp)
    client = app.test_client()
    payload = {"timestamp": "2026-06-05T23:00:00.000Z", "guid": "event-1"}
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")

    response = client.post(
        "/api/toast/webhook",
        data=raw,
        headers={"Toast-Signature": "bad", "Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert list(tmp_path.glob("*.json")) == []
