from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from app.services.toast_webhook_security import (
    ToastWebhookAuthError,
    load_toast_signing_secrets,
    redacted_headers,
    verify_relay_token,
    verify_toast_signature,
)
from app.services.toast_webhook_store import ToastWebhookStore


log = logging.getLogger("toast_webhook_receiver")


def create_receiver_app(store: ToastWebhookStore | None = None) -> Flask:
    app = Flask(__name__)
    webhook_store = store or ToastWebhookStore()
    webhook_store.init_schema()
    if os.getenv("TOAST_WEBHOOK_SEED_ON_START", "1") != "0":
        try:
            counts = webhook_store.seed_employee_profiles_and_identity()
            log.info("toast webhook identity/profile seed complete: %s", counts)
        except Exception:  # noqa: BLE001 - service should still accept raw events.
            log.exception("toast webhook identity/profile seed failed")

    @app.get("/healthz")
    def healthz():
        return jsonify(webhook_store.health())

    @app.post("/ingest/toast/webhook")
    def ingest_toast_webhook():
        raw_body = request.get_data(cache=False)
        headers_for_auth = {k: v for k, v in request.headers.items()}
        safe_headers = redacted_headers(headers_for_auth)

        if os.getenv("TOAST_RECEIVER_REQUIRE_RELAY_TOKEN", "1") != "0":
            try:
                verify_relay_token(headers_for_auth, request.args.get("token"))
            except ToastWebhookAuthError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 403

        try:
            payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Toast webhook body must be a JSON object")
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"invalid JSON body: {exc}"}), 400

        try:
            signature_verified = verify_toast_signature(
                raw_body=raw_body,
                timestamp=str(payload.get("timestamp") or ""),
                provided_signature=headers_for_auth.get("Toast-Signature"),
                secrets=load_toast_signing_secrets(),
                required=os.getenv("TOAST_WEBHOOK_REQUIRE_SIGNATURE", "1") != "0",
            )
        except ToastWebhookAuthError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 401

        try:
            result = webhook_store.store_webhook_event(
                payload=payload,
                raw_body=raw_body,
                headers=safe_headers,
                signature_verified=signature_verified,
                source="ck_receiver",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("toast webhook store failed")
            return jsonify({"ok": False, "error": str(exc)[:400]}), 500
        return jsonify(result), 200

    return app


def _serve_one(app: Flask, host: str, port: int) -> None:
    server = make_server(host, port, app, threaded=True)
    log.info("toast webhook receiver listening on http://%s:%s", host, port)
    server.serve_forever()


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = create_receiver_app()
    hosts = [
        host.strip()
        for host in os.getenv("TOAST_WEBHOOK_HOSTS", os.getenv("ASSISTANT_RUNTIME_HOSTS", "127.0.0.1")).split(",")
        if host.strip()
    ]
    if not hosts:
        hosts = ["127.0.0.1"]
    port = int(os.getenv("TOAST_WEBHOOK_PORT", "8784"))

    threads: list[threading.Thread] = []
    for host in hosts[:-1]:
        thread = threading.Thread(target=_serve_one, args=(app, host, port), daemon=False)
        thread.start()
        threads.append(thread)
    _serve_one(app, hosts[-1], port)


if __name__ == "__main__":
    main()
