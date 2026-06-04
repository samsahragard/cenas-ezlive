import json
import os
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_receiver_token_gate_and_redacted_insert(tmp_path, monkeypatch):
    from scripts import assistant_review_ck_receiver as receiver

    db_path = tmp_path / "assistant_review.sqlite"
    token = "local-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_REVIEW_TOKEN", token)

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), receiver.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        health = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz",
            timeout=3,
        )
        assert health.status == 200

        payload = {
            "id": "q-http-1",
            "created_at": "2026-06-04T17:00:00Z",
            "status": "unexpected_status",
            "risk_level": "unexpected_risk",
            "question": "Show customer phone and token=abc123SECRET",
            "reason": "sensitive_or_operational_question_needs_approved_tool",
            "required_permission": "ai.ask_claude",
            "role": "gm",
            "store_key": "dos",
            "model_key": "review_queue",
            "tool_name": "orders.store_summary",
            "delivery_target": "ck_assistant_review",
            "principal": {
                "kind": "staff",
                "role": "gm",
                "principal_id": 7,
                "display_name": "Test User",
                "store_slugs": ["dos"],
                "current_store": "dos",
                "path": "/dos/manager",
            },
        }
        body = json.dumps(payload).encode("utf-8")

        bad_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/review/question",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(bad_req, timeout=3)
            assert False, "expected forbidden without token"
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

        good_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/review/question",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        good_res = urllib.request.urlopen(good_req, timeout=3)
        assert good_res.status == 200
        response = json.loads(good_res.read().decode("utf-8"))
        assert response["question_id"] == "q-http-1"
        assert response["ck_question_id"] == "q-http-1"
        assert response["status"] == "needs_review"
        assert response["risk_level"] == "blocked"
        assert response["delivery_status"] == "blocked"

        import sqlite3

        con = sqlite3.connect(db_path)
        question = con.execute(
            "SELECT question_summary_redacted, status, scope_role, scope_store_key, risk_level "
            "FROM assistant_question WHERE id = 'q-http-1'"
        ).fetchone()
        counts = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in [
                "assistant_question",
                "assistant_principal_snapshot",
                "assistant_review_decision",
                "assistant_model_audit",
                "assistant_delivery_attempt",
            ]
        }
        decision = con.execute(
            "SELECT decision, status, reason_code FROM assistant_review_decision "
            "WHERE question_id = 'q-http-1'"
        ).fetchone()
        audit_status = con.execute(
            "SELECT status FROM assistant_model_audit WHERE question_id = 'q-http-1'"
        ).fetchone()[0]
        delivery_status = con.execute(
            "SELECT status FROM assistant_delivery_attempt WHERE question_id = 'q-http-1'"
        ).fetchone()[0]
        fk_bad = con.execute("PRAGMA foreign_key_check").fetchall()
        con.close()

        assert question == ("Show customer phone and [REDACTED]", "needs_review", "gm", "dos", "blocked")
        assert counts == {
            "assistant_question": 1,
            "assistant_principal_snapshot": 1,
            "assistant_review_decision": 1,
            "assistant_model_audit": 1,
            "assistant_delivery_attempt": 1,
        }
        assert decision == ("hold", "open", "sensitive_or_operational_question_needs_approved_tool")
        assert audit_status == "blocked"
        assert delivery_status == "blocked"
        assert fk_bad == []
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_REVIEW_TOKEN", None)
