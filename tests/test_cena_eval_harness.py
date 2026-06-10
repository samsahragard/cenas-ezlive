"""Hermetic tests for the eval harness scoring + loop + promotion. No live LLM:
the reasoner is a stub. The real gold file is also schema-checked."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import scripts.cena_sql_eval as ev


# ---- number extraction & matching --------------------------------------- #
def test_extract_numbers_formats():
    assert 1234.56 in ev.extract_numbers("the total was $1,234.56 net")
    assert 12.3 in ev.extract_numbers("labor was 12.3% of sales")
    nums = ev.extract_numbers("a swing of (250) dollars")
    assert -250.0 in nums


def test_extract_numbers_strips_dates_and_years():
    # the date/year digits must NOT pollute the candidate list
    nums = ev.extract_numbers("On 2026-04-15 in 2026 there were 4 orders")
    assert 4.0 in nums
    assert 2026.0 not in nums
    assert 15.0 not in nums  # came from the date


def test_number_match_tolerance():
    assert ev.number_match(34905.35, 0.01, [34905.35])
    assert ev.number_match(34905.35, 0.01, [34900.0])      # within 1%
    assert not ev.number_match(34905.35, 0.01, [30000.0])
    assert ev.number_match(111, 0, [111.0])                # exact count
    assert not ev.number_match(111, 0, [112.0])


def test_text_and_driver_match():
    assert ev.text_match(["tomball", "111"], "Tomball had more, 111 orders.")
    assert not ev.text_match(["tomball"], "Copperfield led.")


def test_score_no_data_requires_honest_decline():
    entry = {"class": "no_data",
             "expected": {"type": "text", "value": "unavailable",
                          "accept": ["no data", "unavailable", "can't"]}}
    ok, _ = ev.score_answer(entry, "No data is available for that week.")
    assert ok
    bad, _ = ev.score_answer(entry, "Net sales were $5,000.")
    assert not bad


def test_trace_verification_detection():
    assert ev.trace_has_verification({"trace": [{"type": "verify", "agree": True}]})
    assert not ev.trace_has_verification({"trace": [{"type": "verify", "agree": False}]})
    assert not ev.trace_has_verification({"trace": [{"type": "query"}]})


# ---- harness loop with a stub reasoner ---------------------------------- #
def _stub_gold():
    return [
        {"id": "T1", "class": "lookup", "store_scope": None,
         "question": "count", "expected": {"type": "number", "value": 10, "tolerance": 0},
         "reference_sql": ["SELECT 10"]},
        {"id": "T2", "class": "lookup", "store_scope": None,
         "question": "miss", "expected": {"type": "number", "value": 99, "tolerance": 0},
         "reference_sql": ["SELECT 99"]},
        {"id": "T3", "class": "no_data", "store_scope": None,
         "question": "nope", "expected": {"type": "text", "value": "unavailable",
                                          "accept": ["no data", "unavailable"]},
         "reference_sql": ["SELECT 0"]},
    ]


def test_run_eval_counts_with_stub_reasoner():
    def stub(question):
        if question == "count":
            return {"answer": "There were 10.", "confidence": "high",
                    "trace": [{"type": "verify", "agree": True}], "queries": [{"sql": "SELECT 10", "ok": True}]}
        if question == "miss":
            return {"answer": "There were 3.", "confidence": "low", "trace": [], "queries": []}
        return {"answer": "No data available for that.", "confidence": "low",
                "trace": [], "queries": []}

    rep = ev.run_eval(_stub_gold(), reasoner=stub, promote=False)
    assert rep["total"] == 3
    assert rep["correct"] == 2                      # T1 hit, T3 honest decline, T2 miss
    assert rep["per_class"]["lookup"] == (1, 2)
    assert len(rep["misses"]) == 1 and rep["misses"][0]["id"] == "T2"
    # both T1 and T2 state a number; only T1 cross-checked -> 1/2
    assert rep["verification_rate"] == 0.5


def test_promotion_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CENA_L3_DATA_DIR", str(tmp_path))
    import app.services.cena_memory as m
    importlib.reload(m)

    def stub(question):
        return {"answer": "There were 10.", "confidence": "high",
                "trace": [{"type": "verify", "agree": True}],
                "queries": [{"sql": "SELECT order_count FROM daily_sales_summary", "ok": True}]}

    gold = [_stub_gold()[0]]
    ev.run_eval(gold, reasoner=stub, promote=True)
    # the correct answer was promoted into the exemplar store
    out = m.recall("count")
    assert out["exemplars"], "correct eval answer should become an exemplar"


def test_promote_to_gold_appends_valid_json(tmp_path):
    gp = tmp_path / "gold.json"
    gp.write_text(json.dumps({"questions": [
        {"id": "L1", "class": "lookup", "question": "q", "expected": {"type": "number", "value": 1}}
    ]}), encoding="utf-8")
    new_id = ev.promote_to_gold(
        "a corrected question",
        {"type": "number", "value": 42, "tolerance": 0, "class": "lookup", "store_scope": None},
        ["SELECT 42"], gold_path=gp)
    data = json.loads(gp.read_text(encoding="utf-8"))
    assert len(data["questions"]) == 2
    assert any(q["id"] == new_id and q["question"] == "a corrected question"
               for q in data["questions"])


# ---- the real gold file is well-formed ---------------------------------- #
def test_real_gold_file_schema():
    data = json.loads((ev.GOLD_PATH).read_text(encoding="utf-8"))
    qs = data["questions"]
    assert len(qs) >= 25
    classes = {}
    for q in qs:
        for key in ("id", "class", "question", "expected", "reference_sql"):
            assert key in q, f"{q.get('id')} missing {key}"
        assert q["expected"].get("type") in ("number", "text", "driver", "no_data")
        assert isinstance(q["reference_sql"], list) and q["reference_sql"]
        classes[q["class"]] = classes.get(q["class"], 0) + 1
    assert classes.get("diagnosis", 0) >= 6, "need >=6 diagnosis questions"
    assert "no_data" in classes and "comparison" in classes and "lookup" in classes
