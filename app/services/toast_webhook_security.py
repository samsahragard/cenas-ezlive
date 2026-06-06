from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Iterable


SENSITIVE_HEADER_PARTS = ("authorization", "token", "secret", "cookie", "signature")


class ToastWebhookAuthError(ValueError):
    """Raised when a webhook cannot be authenticated."""


def read_secret_value(value: str | None = None, file_path: str | None = None) -> str | None:
    """Read a secret from an explicit value or a file path.

    File paths are preferred in production so process listings and logs never carry
    secret material.
    """
    if value:
        value = value.strip()
        if value:
            return value
    if file_path:
        path = Path(file_path)
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
    return None


def load_toast_signing_secrets() -> list[str]:
    """Return all configured Toast webhook signing secrets.

    Toast creates a separate signing secret per webhook subscription. The common
    v1 path uses one secret file, but the JSON variant lets us add category-specific
    subscriptions without changing code.
    """
    secrets: list[str] = []
    one = read_secret_value(
        os.getenv("TOAST_WEBHOOK_SIGNING_SECRET"),
        os.getenv("TOAST_WEBHOOK_SIGNING_SECRET_FILE"),
    )
    if one:
        secrets.append(one)

    json_text = read_secret_value(
        os.getenv("TOAST_WEBHOOK_SIGNING_SECRETS_JSON"),
        os.getenv("TOAST_WEBHOOK_SIGNING_SECRETS_FILE"),
    )
    if json_text:
        try:
            decoded = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise ToastWebhookAuthError("Toast signing secret JSON is invalid") from exc
        if isinstance(decoded, dict):
            values = decoded.values()
        elif isinstance(decoded, list):
            values = decoded
        else:
            values = []
        for item in values:
            if isinstance(item, str) and item.strip():
                secrets.append(item.strip())

    # Preserve order, drop duplicates.
    out: list[str] = []
    seen: set[str] = set()
    for secret in secrets:
        if secret not in seen:
            out.append(secret)
            seen.add(secret)
    return out


def compute_toast_signature(raw_body: bytes, timestamp: str, secret: str) -> str:
    """Compute Toast's webhook signature.

    Per Toast docs, the signed string is the exact request body followed by the
    webhook timestamp, HMAC-SHA256 signed with the subscription secret and base64
    encoded.
    """
    body_and_ts = raw_body + str(timestamp or "").encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), body_and_ts, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_toast_signature(
    *,
    raw_body: bytes,
    timestamp: str | None,
    provided_signature: str | None,
    secrets: Iterable[str] | None = None,
    required: bool = True,
) -> bool:
    """Verify a Toast webhook signature with constant-time comparison."""
    if not required and not provided_signature:
        return False
    if not timestamp:
        if required:
            raise ToastWebhookAuthError("Toast webhook timestamp is missing")
        return False
    if not provided_signature:
        if required:
            raise ToastWebhookAuthError("Toast-Signature header is missing")
        return False

    candidates = list(secrets if secrets is not None else load_toast_signing_secrets())
    if not candidates:
        if required:
            raise ToastWebhookAuthError("Toast webhook signing secret is not configured")
        return False

    for secret in candidates:
        expected = compute_toast_signature(raw_body, timestamp, secret)
        if hmac.compare_digest(expected, provided_signature.strip()):
            return True
    if required:
        raise ToastWebhookAuthError("Toast-Signature did not match")
    return False


def extract_bearer_token(headers: dict[str, str], query_token: str | None = None) -> str | None:
    auth = (headers.get("Authorization") or headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (
        headers.get("X-Cena-Relay-Token")
        or headers.get("x-cena-relay-token")
        or headers.get("X-Toast-Relay-Token")
        or headers.get("x-toast-relay-token")
        or query_token
    )


def verify_relay_token(headers: dict[str, str], query_token: str | None = None) -> None:
    expected = read_secret_value(os.getenv("TOAST_RELAY_TOKEN"), os.getenv("TOAST_RELAY_TOKEN_FILE"))
    if not expected:
        raise ToastWebhookAuthError("Toast relay token is not configured")
    provided = extract_bearer_token(headers, query_token)
    if not provided or not hmac.compare_digest(expected, provided):
        raise ToastWebhookAuthError("Toast relay token did not match")


def redacted_headers(headers: dict[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    items = headers.items() if isinstance(headers, dict) else headers
    out: dict[str, str] = {}
    for key, value in items:
        lower = key.lower()
        if any(part in lower for part in SENSITIVE_HEADER_PARTS):
            out[key] = "[redacted]"
        elif lower.startswith("toast-") or lower in {
            "content-type",
            "content-length",
            "user-agent",
            "x-forwarded-for",
            "x-real-ip",
            "host",
        }:
            out[key] = str(value)
    return out
