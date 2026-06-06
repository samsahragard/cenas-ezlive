from app.services.toast_webhook_security import compute_toast_signature, verify_toast_signature


def test_toast_signature_uses_raw_body_plus_timestamp():
    raw_body = b'{"timestamp":"2026-06-05T18:00:00.000Z","guid":"evt-1"}'
    timestamp = "2026-06-05T18:00:00.000Z"
    secret = "unit-test-secret"

    signature = compute_toast_signature(raw_body, timestamp, secret)

    assert verify_toast_signature(
        raw_body=raw_body,
        timestamp=timestamp,
        provided_signature=signature,
        secrets=[secret],
    )


def test_toast_signature_rejects_changed_body():
    raw_body = b'{"timestamp":"2026-06-05T18:00:00.000Z","guid":"evt-1"}'
    timestamp = "2026-06-05T18:00:00.000Z"
    secret = "unit-test-secret"
    signature = compute_toast_signature(raw_body, timestamp, secret)

    assert not verify_toast_signature(
        raw_body=raw_body + b" ",
        timestamp=timestamp,
        provided_signature=signature,
        secrets=[secret],
        required=False,
    )
