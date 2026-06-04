import json
import os
import socket
import threading
from http.server import ThreadingHTTPServer

from app.web import assistant_routes as ar


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_tool_catalog_only_general_help_active_for_operational_role():
    ctx = {
        "kind": "staff",
        "role": "gm",
        "permissions": [
            "ai.ask_claude",
            "ai.ask_claude_personal",
            "orders.view",
            "drivers.view_roster",
            "labor.view_store_summary",
        ],
    }

    tools = {tool["tool_id"]: tool for tool in ar._tool_catalog_for(ctx)}

    assert tools["assistant.general_help"]["available"] is True
    assert tools["orders.store_summary"]["available"] is False
    assert tools["orders.store_summary"]["deny_reason"] == "needs_sam_review"
    assert tools["drivers.store_summary"]["available"] is False
    assert tools["labor.store_aggregate"]["available"] is False


def test_tool_catalog_respects_missing_permission():
    ctx = {
        "kind": "staff",
        "role": "expo",
        "permissions": ["ai.ask_claude_personal"],
    }

    tools = {tool["tool_id"]: tool for tool in ar._tool_catalog_for(ctx)}

    assert tools["assistant.general_help"]["available"] is True
    assert tools["orders.store_summary"]["available"] is False
    assert tools["orders.store_summary"]["deny_reason"] == "missing_permission"


def test_retry_outbox_record_is_redacted_and_hashed():
    row = {
        "id": "q1",
        "created_at": "2026-06-04T16:00:00Z",
        "question": "Show token=abc123SECRET and customer phone",
        "reason": "sensitive_or_operational_question_needs_approved_tool",
        "required_permission": "ai.ask_claude",
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

    record = ar._outbox_record(row)
    encoded = json.dumps(record, sort_keys=True)

    assert record["question_summary_redacted"] == "Show [REDACTED] and customer phone"
    assert "abc123SECRET" not in encoded
    assert "Test User" not in encoded
    assert record["principal_hash"]
    assert record["status"] == "needs_review"
    assert record["risk_level"] == "blocked"
    assert record["storage"] == "render_retry_outbox_redacted"


def test_store_scope_key_accepts_objects():
    class Store:
        slug = "tomball"

    assert ar._store_scope_key(Store()) == "tomball"
    assert ar._store_scope_key("dos") == "dos"
    assert ar._store_scope_key(None) is None


def test_assistant_enabled_is_off_by_default_on_render(monkeypatch):
    monkeypatch.setenv("RENDER", "1")
    monkeypatch.delenv("AI_ASSISTANT_ENABLED", raising=False)
    monkeypatch.delenv("AI_ASSISTANT_DISABLED", raising=False)

    assert ar._assistant_enabled() is False

    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "1")
    assert ar._assistant_enabled() is True


def test_post_to_ck_review_uses_contract_path_for_base_url(tmp_path, monkeypatch):
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
        monkeypatch.setenv("ASSISTANT_REVIEW_RECEIVER_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("ASSISTANT_REVIEW_RECEIVER_TOKEN", token)
        monkeypatch.setenv("ASSISTANT_REVIEW_TIMEOUT_SECONDS", "5")

        saved, ck_id = ar._post_to_ck_review({
            "id": "q-route-1",
            "created_at": "2026-06-04T18:00:00Z",
            "status": "needs_review",
            "risk_level": "blocked",
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
        })

        assert saved is True
        assert ck_id == "q-route-1"

        import sqlite3

        con = sqlite3.connect(db_path)
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
        con.close()

        assert counts == {
            "assistant_question": 1,
            "assistant_principal_snapshot": 1,
            "assistant_review_decision": 1,
            "assistant_model_audit": 1,
            "assistant_delivery_attempt": 1,
        }
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_REVIEW_TOKEN", None)
        os.environ.pop("AI_ASSISTANT_CK_REVIEW_URL", None)
        os.environ.pop("AI_ASSISTANT_CK_REVIEW_TOKEN", None)
        os.environ.pop("ASSISTANT_REVIEW_RECEIVER_URL", None)
        os.environ.pop("ASSISTANT_REVIEW_RECEIVER_TOKEN", None)
        os.environ.pop("ASSISTANT_REVIEW_TIMEOUT_SECONDS", None)
