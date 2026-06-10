"""Integration tests for the Wave 3 hybrid-router seam in the CK runtime and the
Flask route. The reasoning engine is stubbed via the orchestrator import; no
network, no live model, no snapshots."""
from __future__ import annotations

import importlib

import pytest


# --------------------------------------------------------------------------- #
# CK runtime seam
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def rt():
    return importlib.import_module("scripts.assistant_ck_runtime")


def test_data_question_gate(rt):
    assert rt._looks_like_data_question("What were net sales in April?")
    assert rt._looks_like_data_question("why are tomball orders down")
    assert rt._looks_like_data_question("how many deliveries did the driver make")
    assert not rt._looks_like_data_question("hi there, who are you?")
    assert not rt._looks_like_data_question("thanks!")


def _canned(**over):
    base = {"ok": True, "answer": "Tomball netted $46,053.85 in April.",
            "confidence": "high", "confidence_reason": "two computations agree",
            "route": "investigation", "trace": [{"type": "plan"}],
            "queries": [{"sql": "SELECT ...", "ok": True}],
            "show_work": "Plan (lookup): ...\nVerified the figure a second way."}
    base.update(over)
    return base


def test_investigation_answer_runs_for_data_question(rt, monkeypatch):
    import app.services.cena_sql_orchestrator as orch
    monkeypatch.setattr(orch, "answer_question", lambda q, p=None, **k: _canned())
    out = rt._investigation_answer("what were net sales in April", {"role": "partner"})
    assert out and out["answer"].startswith("Tomball")
    assert out["show_work"]


def test_investigation_answer_skips_non_data(rt, monkeypatch):
    import app.services.cena_sql_orchestrator as orch
    monkeypatch.setattr(orch, "answer_question",
                        lambda q, p=None, **k: pytest.fail("should not investigate"))
    assert rt._investigation_answer("hello, how are you?", {}) is None


def test_investigation_answer_swallows_failure(rt, monkeypatch):
    import app.services.cena_sql_orchestrator as orch

    def boom(q, p=None, **k):
        raise RuntimeError("engine down")

    monkeypatch.setattr(orch, "answer_question", boom)
    # data question, but engine failure -> None so the caller falls through
    assert rt._investigation_answer("net sales in April", {}) is None


def test_answer_routes_data_question_to_investigation(rt, monkeypatch):
    import app.services.cena_sql_orchestrator as orch
    monkeypatch.setattr(orch, "answer_question", lambda q, p=None, **k: _canned())
    # neutralize the gates around the seam
    monkeypatch.setattr(rt, "_shared_force_review_reason", lambda q: None)
    monkeypatch.setattr(rt, "_approved_tool_answer", lambda *a, **k: None)
    monkeypatch.setattr(rt, "_should_queue", lambda q, p: (False, "", None))

    resp, status = rt._answer({"question": "what were tomball net sales in April?",
                               "principal": {"role": "partner"}})
    assert status == 200
    assert resp["route_path"] == "investigation"
    assert resp["queued"] is False
    assert resp["confidence"] == "high"
    assert resp["show_work"]
    assert "46,053" in resp["answer"]


def test_answer_non_data_does_not_investigate(rt, monkeypatch):
    import app.services.cena_sql_orchestrator as orch
    monkeypatch.setattr(orch, "answer_question",
                        lambda q, p=None, **k: pytest.fail("should not investigate"))
    monkeypatch.setattr(rt, "_shared_force_review_reason", lambda q: None)
    monkeypatch.setattr(rt, "_approved_tool_answer", lambda *a, **k: None)
    monkeypatch.setattr(rt, "_should_queue", lambda q, p: (False, "", None))
    monkeypatch.setattr(rt, "_gemini_answer", lambda q, p: ("hello yourself", "fake-model"))

    resp, status = rt._answer({"question": "hi, who are you?", "principal": {}})
    assert status == 200
    assert resp["route_path"] == "general"        # fell through to conversational
    assert resp["answer"] == "hello yourself"


# --------------------------------------------------------------------------- #
# Flask route helper seam
# --------------------------------------------------------------------------- #
def test_routes_l3_helper(monkeypatch):
    ar = importlib.import_module("app.web.assistant_routes")
    import app.services.cena_sql_orchestrator as orch
    monkeypatch.setattr(orch, "answer_question", lambda q, **k: _canned())
    assert ar._l3_investigation_answer("net sales in April")["confidence"] == "high"
    assert ar._l3_investigation_answer("hello there") is None
