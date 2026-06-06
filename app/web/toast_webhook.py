from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from flask import Blueprint, jsonify, request

from app.services.toast_webhook_security import (
    ToastWebhookAuthError,
    load_toast_signing_secrets,
    read_secret_value,
    redacted_headers,
    verify_toast_signature,
)


toast_webhook_bp = Blueprint("toast_webhook", __name__)
log = logging.getLogger(__name__)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _spool_dir() -> Path:
    base = os.getenv("TOAST_WEBHOOK_RELAY_SPOOL_DIR")
    if base:
        path = Path(base)
    elif Path("/var/data").exists():
        path = Path("/var/data/toast_webhook_relay_spool")
    else:
        path = Path.cwd() / "toast_webhook_relay_spool"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _spool_event(raw_body: bytes, safe_headers: dict[str, str]) -> str:
    digest = hashlib.sha256(raw_body).hexdigest()
    path = _spool_dir() / f"{_utc_stamp()}_{digest[:16]}.json"
    path.write_text(
        json.dumps(
            {
                "received_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "payload_sha256": digest,
                "headers": safe_headers,
                "body": raw_body.decode("utf-8", errors="replace"),
                "forwarded": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return str(path)


def _mark_spooled_forwarded(path: str, status_code: int) -> None:
    try:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["forwarded"] = True
        data["forwarded_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        data["forward_status_code"] = status_code
        p.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.exception("toast webhook relay spool mark-forwarded failed")


def _forward_to_ck(raw_body: bytes, headers: dict[str, str]) -> tuple[bool, int | None, str | None]:
    url = os.getenv("TOAST_WEBHOOK_CK_INGEST_URL", "http://100.73.38.82:8784/ingest/toast/webhook")
    relay_token = read_secret_value(os.getenv("TOAST_RELAY_TOKEN"), os.getenv("TOAST_RELAY_TOKEN_FILE"))
    if not url or not relay_token:
        return False, None, "relay URL or token is not configured"
    outbound = {
        "Content-Type": headers.get("Content-Type", "application/json"),
        "X-Cena-Relay-Token": relay_token,
    }
    for key, value in headers.items():
        if key.lower().startswith("toast-"):
            outbound[key] = value
    try:
        with httpx.Client(timeout=1.2) as client:
            response = client.post(url, content=raw_body, headers=outbound)
        if 200 <= response.status_code < 300:
            return True, response.status_code, None
        return False, response.status_code, response.text[:300]
    except Exception as exc:  # noqa: BLE001
        return False, None, str(exc)[:300]


@toast_webhook_bp.post("/api/toast/webhook")
def toast_webhook_relay():
    raw_body = request.get_data(cache=False)
    headers = {k: v for k, v in request.headers.items()}
    safe_headers = redacted_headers(headers)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Toast webhook body must be a JSON object")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"invalid JSON body: {exc}"}), 400

    try:
        verify_toast_signature(
            raw_body=raw_body,
            timestamp=str(payload.get("timestamp") or ""),
            provided_signature=headers.get("Toast-Signature"),
            secrets=load_toast_signing_secrets(),
            required=os.getenv("TOAST_WEBHOOK_REQUIRE_SIGNATURE", "1") != "0",
        )
    except ToastWebhookAuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401

    try:
        spool_path = _spool_event(raw_body, safe_headers)
    except Exception as exc:  # noqa: BLE001
        log.exception("toast webhook relay spool failed")
        return jsonify({"ok": False, "error": f"spool failed: {exc}"}), 500

    forwarded, status_code, error = _forward_to_ck(raw_body, headers)
    if forwarded and status_code is not None:
        _mark_spooled_forwarded(spool_path, status_code)
        return jsonify({"ok": True, "spooled": True, "forwarded": True, "status_code": status_code}), 200

    log.warning("toast webhook relayed to spool only; status=%s error=%s", status_code, error)
    return jsonify({
        "ok": True,
        "spooled": True,
        "forwarded": False,
        "forward_status_code": status_code,
        "forward_error": error,
    }), 202
