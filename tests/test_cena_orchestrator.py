"""Hermetic tests for the hybrid router. Investigation is stubbed; no network."""
from __future__ import annotations

from app.services import cena_sql_orchestrator as orch


def _inv_result():
    return {
        "answer": "Tomball had 111 catering orders in April.",
        "confidence": "high",
        "confidence_reason": "two independent computations agree",
        "trace": [
            {"type": "plan", "question_class": "lookup", "plan": ["count April orders"]},
            {"type": "query", "ok": True, "purpose": "count orders",
             "sql": "SELECT SUM(order_count) FROM daily_sales_summary", "row_count": 1},
            {"type": "verify", "headline": "orders", "agree": True},
        ],
        "queries": [{"sql": "SELECT SUM(order_count) FROM daily_sales_summary", "ok": True}],
    }


def test_investigation_path_packages_bubble():
    res = orch.answer_question("how many orders did tomball have in april",
                               investigate_fn=lambda q, c=None: _inv_result())
    assert res["ok"] is True
    assert res["route"] == "investigation"
    assert res["confidence"] == "high"
    assert "111" in res["answer"]
    # show-work trace is auditable
    assert "Plan" in res["show_work"]
    assert "daily_sales_summary" in res["show_work"]
    assert "agree" in res["show_work"].lower()


def test_deterministic_tool_wins_and_skips_investigation():
    called = {"investigate": False}

    def inv(q, c=None):
        called["investigate"] = True
        return _inv_result()

    def tool(q, principal, context):
        return {"answer": "You have 5 catering orders today.", "tool_id": "orders.store_summary"}

    res = orch.answer_question("orders today", deterministic_fn=tool, investigate_fn=inv)
    assert res["route"] == "deterministic"
    assert "5 catering orders" in res["answer"]
    assert called["investigate"] is False  # investigation never ran


def test_deterministic_miss_falls_through_to_investigation():
    res = orch.answer_question("why are sales down",
                               deterministic_fn=lambda q, p, c: None,
                               investigate_fn=lambda q, c=None: _inv_result())
    assert res["route"] == "investigation"


def test_investigation_failure_is_clean_not_a_crash():
    def boom(q, c=None):
        raise RuntimeError("executor exploded")

    res = orch.answer_question("anything", investigate_fn=boom)
    assert res["ok"] is False
    assert res["route"] == "error"
    assert "couldn't pull that" in res["answer"].lower()
    assert "exploded" not in res["answer"]          # no stack trace leaked
    assert res["confidence"] == "low"


def test_empty_question_handled():
    res = orch.answer_question("   ", investigate_fn=lambda q, c=None: _inv_result())
    assert res["route"] == "error"
    assert res["ok"] is False


def test_show_work_renders_repair_and_discard_and_flag():
    result = {
        "answer": "x", "confidence": "medium", "confidence_reason": "ok",
        "trace": [
            {"type": "repair", "attempt": 1, "reason": "column 'tips' is excluded by policy"},
            {"type": "discard", "note": "checked discounting - flat, not the cause"},
            {"type": "flag", "note": "copperfield labor spiked Saturday"},
            {"type": "limit", "note": "budget reached"},
        ],
        "queries": [],
    }
    sw = orch.format_show_work(result)
    assert "Repair" in sw and "excluded by policy" in sw
    assert "Ruled out" in sw and "discounting" in sw
    assert "Noticed" in sw and "copperfield" in sw
    assert "Medium confidence" in sw
