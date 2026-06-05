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


def _post(port: int, payload: dict, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/assistant/answer",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    return urllib.request.urlopen(req, timeout=3)


def _principal(role: str = "gm") -> dict:
    return {
        "kind": "staff",
        "role": role,
        "principal_id": 7,
        "display_name": "Test User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/dos/manager",
        "permissions": ["ai.ask_claude", "ai.ask_claude_personal"],
        "can_ask_personal": True,
        "can_ask_operational": role in {"partner", "gm", "km"},
    }


def test_runtime_token_gate_and_blocked_question_save(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        payload = {
            "question": "Show me customer phone numbers for catering orders",
            "principal": _principal("gm"),
            "tools": [],
            "source": "test",
        }

        try:
            _post(port, payload)
            assert False, "expected token gate to reject missing token"
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

        res = _post(port, payload, token)
        assert res.status == 200
        data = json.loads(res.read().decode("utf-8"))

        assert data["ok"] is True
        assert data["queued"] is True
        assert data["storage"] == "ck"
        assert data["answer"] == "I do not have the approved Cenas data tool for that yet, so I saved it for Sam review."
        assert data["reason"] == "sensitive_or_operational_question_needs_approved_tool"

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
        question = con.execute(
            "SELECT status, scope_role, scope_store_key, risk_level FROM assistant_question"
        ).fetchone()
        con.close()

        assert counts == {
            "assistant_question": 1,
            "assistant_principal_snapshot": 1,
            "assistant_review_decision": 1,
            "assistant_model_audit": 1,
            "assistant_delivery_attempt": 1,
        }
        assert question == ("needs_review", "gm", "dos", "blocked")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_missing_permission_keeps_permission_wording(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("expo")
        principal["can_ask_personal"] = False
        principal["can_ask_operational"] = False
        principal["permissions"] = []
        res = _post(
            port,
            {
                "question": "How do I move around this page?",
                "principal": principal,
                "tools": [],
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert res.status == 200
        assert data["queued"] is True
        assert data["answer"].startswith("I can't safely answer")
        assert data["reason"] == "missing_ai_permission"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_general_question_from_ck_model(monkeypatch, tmp_path):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_anthropic_answer",
        lambda question, principal: ("Use the Orders tab to review catering requests.", "claude-sonnet-4-6"),
    )
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda question, principal: (_ for _ in ()).throw(AssertionError("Gemini fallback should not run when Sonnet answers")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        res = _post(
            port,
            {
                "question": "How do I move around this page?",
                "principal": _principal("gm"),
                "tools": [],
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert res.status == 200
        assert data == {
            "ok": True,
            "answer": "Use the Orders tab to review catering requests.",
            "queued": False,
            "model": "claude-sonnet-4-6",
            "storage": "ck_runtime",
        }
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_falls_back_to_gemini_when_sonnet_errors(monkeypatch, tmp_path):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_anthropic_answer",
        lambda question, principal: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda question, principal: ("Open the Orders tab for catering requests.", "gemini-2.5-flash"),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        res = _post(
            port,
            {
                "question": "How do I move around this page?",
                "principal": _principal("gm"),
                "tools": [],
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert res.status == 200
        assert data == {
            "ok": True,
            "answer": "Open the Orders tab for catering requests.",
            "queued": False,
            "model": "gemini-2.5-flash",
            "storage": "ck_runtime",
        }
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)
