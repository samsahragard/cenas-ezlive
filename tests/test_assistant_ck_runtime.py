import json
import os
import socket
import sys
import threading
import types
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


def _available_tool(tool_id: str) -> dict:
    return {
        "tool_id": tool_id,
        "label": tool_id,
        "available": True,
        "status": "active",
        "deny_reason": None,
        "read_write_class": "read_only",
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


def test_runtime_answers_operator_catering_count_with_approved_tool(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_anthropic_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("approved tool answer must not call model")),
    )
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("approved tool answer must not call model")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "How many caterings do we have today?",
                "principal": principal,
                "tools": [_available_tool("orders.store_summary")],
                "tool_data": {
                    "orders.store_summary": {
                        "generated_at": "2026-06-05T17:00:00Z",
                        "total_orders": 12,
                        "today_orders": 3,
                        "upcoming_orders": 8,
                        "needs_driver_orders": 2,
                        "live_tracking_orders": 1,
                        "by_store": {"copperfield": 2, "tomball": 1},
                    }
                },
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert res.status == 200
        assert data["ok"] is True
        assert data["queued"] is False
        assert data["storage"] == "operational_tool"
        assert data["tool_id"] == "orders.store_summary"
        assert "3 caterings today" in data["answer"]
        assert "2 still need driver attention" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_operator_toast_sales_with_approved_tool(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_anthropic_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("approved tool answer must not call model")),
    )
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("approved tool answer must not call model")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "what are Toast sales today?",
                "principal": principal,
                "tools": [_available_tool("toast.sales_summary")],
                "tool_data": {
                    "toast.sales_summary": {
                        "generated_at": "2026-06-05T17:00:00Z",
                        "label": "Today",
                        "scope_note": "2 locations included.",
                        "sales": {
                            "net": 1234.56,
                            "gross": 1300.00,
                            "discount": 10.00,
                            "void": 0.00,
                            "refund": 5.00,
                            "avg_order": 48.41,
                            "sales_per_labor_hour": 111.11,
                            "orders": 26,
                            "guests": 42,
                        },
                        "labor": {
                            "hours": 11.11,
                            "cost": 444.44,
                            "ratio_pct": 36.0,
                            "by_job": [],
                        },
                        "menu": {},
                    }
                },
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["storage"] == "toast_analytics_tool"
        assert data["tool_id"] == "toast.sales_summary"
        assert "net sales are $1,234.56" in data["answer"]
        assert "2 locations included" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_operator_toast_table_activity_with_approved_tool(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_anthropic_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("approved tool answer must not call model")),
    )
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("approved tool answer must not call model")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "what was the most recent talbe opened in tomball",
                "principal": principal,
                "tools": [_available_tool("toast.table_activity")],
                "tool_data": {
                    "toast.table_activity": {
                        "generated_at": "2026-06-05T23:22:00Z",
                        "location": "tomball",
                        "location_label": "Tomball",
                        "latest": {
                            "location": "tomball",
                            "location_label": "Tomball",
                            "table_name": "106",
                            "opened_at_local": "2026-06-05 6:20 PM CT",
                            "table_config_available": True,
                        },
                    }
                },
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["storage"] == "toast_table_activity_tool"
        assert data["tool_id"] == "toast.table_activity"
        assert "table 106" in data["answer"]
        assert "6:20 PM CT" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_operator_catering_followup_with_previous_question(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_anthropic_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("follow-up tool answer must not call model")),
    )
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("follow-up tool answer must not call model")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "what baout earlier this morning?",
                "previous_question": "how amny caterings do we have today?",
                "principal": principal,
                "tools": [_available_tool("orders.store_summary")],
                "tool_data": {
                    "orders.store_summary": {
                        "today": "2026-06-05",
                        "today_orders": 3,
                        "upcoming_orders": 8,
                        "needs_driver_orders": 0,
                        "live_tracking_orders": 0,
                        "today_time_windows": {
                            "morning": 2,
                            "afternoon": 1,
                            "evening": 0,
                            "earlier_today": 2,
                            "unknown_time": 0,
                        },
                        "today_time_windows_by_store": {
                            "morning": {"copperfield": 1, "tomball": 1}
                        },
                        "today_by_store": {"copperfield": 2, "tomball": 1},
                    }
                },
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert res.status == 200
        assert data["queued"] is False
        assert data["storage"] == "operational_tool"
        assert data["tool_id"] == "orders.store_summary"
        assert "earlier this morning (2026-06-05)" in data["answer"]
        assert "2 caterings" in data["answer"]
        assert "copperfield: 1" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_operator_driver_summary_with_approved_tool(tmp_path, monkeypatch):
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
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "How many active drivers do we have?",
                "principal": principal,
                "tools": [_available_tool("drivers.store_summary")],
                "tool_data": {
                    "drivers.store_summary": {
                        "total_drivers": 5,
                        "active_drivers": 5,
                        "drivers_on_shift": 3,
                        "drivers_on_active_orders": 2,
                        "average_score": 100.0,
                        "by_store": {"copperfield": 5},
                    }
                },
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["storage"] == "operational_tool"
        assert data["tool_id"] == "drivers.store_summary"
        assert "5 drivers" in data["answer"]
        assert "Average current score is 100.0" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_owner_tool_matcher_covers_live_sweep_phrases():
    from scripts import assistant_ck_runtime as runtime

    principal = _principal("partner")
    principal["kind"] = "partner"
    principal["is_owner_operator"] = True
    tools = [
        _available_tool("orders.store_summary"),
        _available_tool("drivers.store_summary"),
        _available_tool("labor.store_aggregate"),
    ]
    tool_data = {
        "orders.store_summary": {
            "today_orders": 4,
            "upcoming_orders": 12,
            "needs_driver_orders": 17,
            "live_tracking_orders": 2,
        },
        "drivers.store_summary": {
            "total_drivers": 45,
            "active_drivers": 25,
            "drivers_on_shift": 0,
            "drivers_on_active_orders": 3,
            "average_score": 100.0,
        },
        "labor.store_aggregate": {
            "total_employees": 119,
            "active_employees": 95,
            "published_shifts": 2323,
            "open_shifts": 0,
        },
    }

    cases = {
        "caterings today?": "orders.store_summary",
        "what caterings are today": "orders.store_summary",
        "how many active tracking links": "orders.store_summary",
        "what is the store split for caterings today": "orders.store_summary",
        "do we have caterings today": "orders.store_summary",
        "today order totals": "orders.store_summary",
        "orders needing driver attention": "orders.store_summary",
        "current drivers by location": "drivers.store_summary",
        "do we have drivers on shift": "drivers.store_summary",
        "show me the driver aggregate": "drivers.store_summary",
        "driver coverage today": "drivers.store_summary",
        "current staffing summary": "labor.store_aggregate",
    }

    for question, expected_tool in cases.items():
        data = runtime._approved_tool_answer(question, "", principal, tools, tool_data)
        assert data is not None, question
        assert data["queued"] is False
        assert data["storage"] == "operational_tool"
        assert data["tool_id"] == expected_tool


def test_runtime_owner_identity_uses_authenticated_session_context(monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_anthropic_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("session context answer must not call model")),
    )
    principal = _principal("partner")
    principal["kind"] = "partner"
    principal["is_owner_operator"] = True

    data = runtime._approved_tool_answer("i am sam.", "", principal, [], {})

    assert data is not None
    assert data["queued"] is False
    assert data["storage"] == "session_context"
    assert "already marked as an owner-operator session" in data["answer"]


def test_runtime_answers_operator_labor_summary_with_approved_tool(tmp_path, monkeypatch):
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
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "Give me a labor and employee summary",
                "principal": principal,
                "tools": [_available_tool("labor.store_aggregate")],
                "tool_data": {
                    "labor.store_aggregate": {
                        "total_employees": 95,
                        "active_employees": 91,
                        "published_shifts": 22,
                        "open_shifts": 4,
                        "last30_cached_hours": 1234.5,
                        "today_attendance_statuses": {"clocked-in": 12, "late": 1},
                    }
                },
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["storage"] == "operational_tool"
        assert data["tool_id"] == "labor.store_aggregate"
        assert "95 employees" in data["answer"]
        assert "1234.5 hours" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_anthropic_answer_marks_stable_policy_for_prompt_cache(monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    captured = {}

    class FakeBlock:
        type = "text"
        text = "cached answer"

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(content=[FakeBlock()])

    class FakeAnthropic:
        def __init__(self, api_key):
            self.api_key = api_key
            self.messages = FakeMessages()

    fake_module = types.SimpleNamespace(Anthropic=FakeAnthropic)
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    answer, model = runtime._anthropic_answer("How do I use the page?", _principal("gm"))

    assert answer == "cached answer"
    assert model == "claude-sonnet-4-6"
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Current session" in captured["system"][1]["text"]
    assert "owner_operator=False" in captured["system"][1]["text"]


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
