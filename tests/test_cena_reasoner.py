"""Hermetic tests for the reasoning loop. The LLM is a scripted fake and the
executor is a stub, but validation uses the REAL cena_sql_validator so the
repair loop is exercised against genuine rejection reasons. No network, no DB."""
from __future__ import annotations

import json

import pytest

from app.services import cena_reasoner as R
from app.services.cena_sql_validator import validate_sql


class FakeLLM:
    """Routes by phase (detected from the system prompt) so tests don't depend on
    exact call counts. `act` may be a list (popped in order) or a callable."""

    def __init__(self, *, plan, act, verify=None):
        self.plan = plan
        self.act = list(act) if isinstance(act, list) else act
        self.verify = verify
        self.calls = []
        self._act_i = 0

    def __call__(self, prompt, system=None, timeout_s=25.0):
        system = system or ""
        self.calls.append((system, prompt))
        if "Classify" in system:
            return self.plan
        if "verify a headline" in system:
            return self.verify if self.verify is not None else json.dumps(
                {"sql": "SELECT 1", "purpose": "noop"}
            )
        # act phase (includes repair prompts, same system)
        if callable(self.act):
            return self.act(prompt, len(self.calls) - 1)
        if self._act_i < len(self.act):
            r = self.act[self._act_i]
            self._act_i += 1
            return r
        return json.dumps({"action": "answer", "answer": "done", "confidence": "low",
                           "confidence_reason": "exhausted", "headline_numbers": []})


class StubExec:
    def __init__(self, router):
        self.router = router
        self.calls = []

    def __call__(self, sql):
        self.calls.append(sql)
        return self.router(sql)


def _ok(rows, columns=("c",)):
    return {"rows": [tuple(r) for r in rows], "columns": list(columns),
            "row_count": len(rows), "truncated": False, "elapsed_ms": 1.0}


_NO_MEMORY = type("M", (), {"recall": staticmethod(lambda q: {"exemplars": [], "insights": []}),
                            "record": staticmethod(lambda *a, **k: None)})()


def _run(question, llm, executor, **kw):
    return R.investigate(question, llm=llm, executor=executor, schema_context="(schema)",
                         memory=_NO_MEMORY, _validate=validate_sql, **kw)


# --------------------------------------------------------------------------- #
def test_plan_emitted_and_diagnosis_playbook_selected():
    llm = FakeLLM(
        plan=json.dumps({"class": "diagnosis", "plan": ["traffic vs spend"], "notes": ""}),
        act=[json.dumps({"action": "answer", "answer": "Traffic fell.",
                         "confidence": "medium", "confidence_reason": "one read",
                         "headline_numbers": []})],
    )
    ex = StubExec(lambda s: _ok([]))
    res = _run("why are sales down at tomball?", llm, ex)
    plan_entries = [t for t in res["trace"] if t["type"] == "plan"]
    assert plan_entries and plan_entries[0]["question_class"] == "diagnosis"
    # the diagnosis playbook reached the act-phase system prompt
    act_systems = [s for (s, p) in llm.calls if "Classify" not in s and "verify" not in s]
    assert any("Traffic vs spend" in s or "Why are SALES down" in s for s in act_systems)


def test_repair_loop_feeds_reason_back():
    # first SQL references an EXCLUDED column -> real validator rejects -> repair
    bad = json.dumps({"action": "sql", "sql": "SELECT hourly_rate FROM toast.time_entry",
                      "purpose": "pay"})
    good = json.dumps({"action": "sql", "sql": "SELECT reg_hours FROM toast.time_entry",
                       "purpose": "hours"})
    answer = json.dumps({"action": "answer", "answer": "ok", "confidence": "low",
                         "confidence_reason": "x", "headline_numbers": []})
    llm = FakeLLM(plan=json.dumps({"class": "lookup", "plan": []}),
                  act=[bad, good, answer])
    ex = StubExec(lambda s: _ok([(40.0,)]))
    res = _run("show hours", llm, ex)
    repairs = [t for t in res["trace"] if t["type"] == "repair"]
    assert repairs, "repair entry expected"
    assert "excluded by policy" in repairs[0]["reason"]
    # the repair prompt actually contained the rejection reason (fed back)
    assert any("excluded by policy" in p for (s, p) in llm.calls)
    # the corrected query ran
    assert any("reg_hours" in q["sql"] for q in res["queries"] if q.get("ok"))


def test_verification_catches_planted_discrepancy():
    answer = json.dumps({"action": "answer", "answer": "Net sales were $1000.",
                         "confidence": "high", "confidence_reason": "looks right",
                         "headline_numbers": [{"label": "net sales", "value": "1000"}]})
    llm = FakeLLM(
        plan=json.dumps({"class": "lookup", "plan": []}),
        act=[json.dumps({"action": "sql",
                         "sql": "SELECT net_sales FROM daily_sales_summary",
                         "purpose": "net"}), answer],
        verify=json.dumps({"sql": "SELECT SUM(caterer_total_due) FROM ordersdc.dm_order",
                           "purpose": "recompute net from raw"}),
    )

    def router(sql):
        if "ordersdc" in sql:           # the cross-check disagrees
            return _ok([(2000.0,)])
        if "anomaly_flags" in sql:
            return _ok([])
        return _ok([(1000.0,)])

    res = _run("net sales", llm, StubExec(router))
    verifies = [t for t in res["trace"] if t["type"] == "verify"]
    assert verifies and verifies[0]["agree"] is False
    assert res["confidence"] == "low"
    assert "uncertain" in res["answer"].lower()


def test_verification_agreement_keeps_confidence():
    answer = json.dumps({"action": "answer", "answer": "Net sales were $1000.",
                         "confidence": "high", "confidence_reason": "two reads",
                         "headline_numbers": [{"label": "net sales", "value": "1000"}]})
    llm = FakeLLM(
        plan=json.dumps({"class": "lookup", "plan": []}),
        act=[json.dumps({"action": "sql", "sql": "SELECT net_sales FROM daily_sales_summary",
                         "purpose": "net"}), answer],
        verify=json.dumps({"sql": "SELECT SUM(caterer_total_due) FROM ordersdc.dm_order",
                           "purpose": "recompute"}),
    )

    def router(sql):
        if "ordersdc" in sql:
            return _ok([(1000.4,)])     # agrees within 1%
        if "anomaly_flags" in sql:
            return _ok([])
        return _ok([(1000.0,)])

    res = _run("net sales", llm, StubExec(router))
    assert res["confidence"] == "high"
    assert any(t["type"] == "verify" and t["agree"] for t in res["trace"])


def test_diagnosis_decomposition_runs_queries_in_order():
    acts = [
        json.dumps({"action": "sql", "sql": "SELECT order_count FROM daily_sales_summary",
                    "purpose": "traffic"}),
        json.dumps({"action": "sql", "sql": "SELECT avg_check FROM daily_sales_summary",
                    "purpose": "spend"}),
        json.dumps({"action": "sql",
                    "sql": "SELECT metric FROM anomaly_flags",
                    "purpose": "coincident events"}),
        json.dumps({"action": "answer", "answer": "Traffic drove it.",
                    "confidence": "medium", "confidence_reason": "decomposed",
                    "headline_numbers": []}),
    ]
    llm = FakeLLM(plan=json.dumps({"class": "diagnosis", "plan": ["traffic", "spend", "events"]}),
                  act=acts)
    res = _run("why are sales down?", llm, StubExec(lambda s: _ok([(1,)])))
    ran = [t["sql"] for t in res["trace"] if t["type"] == "query"]
    assert ran == ["SELECT order_count FROM daily_sales_summary",
                   "SELECT avg_check FROM daily_sales_summary",
                   "SELECT metric FROM anomaly_flags"]


def test_budget_wall_enforced():
    sql_action = json.dumps({"action": "sql", "sql": "SELECT net_sales FROM daily_sales_summary",
                             "purpose": "keep digging"})
    llm = FakeLLM(plan=json.dumps({"class": "lookup", "plan": []}),
                  act=lambda prompt, i: sql_action)  # never answers

    def router(sql):
        if "anomaly_flags" in sql:
            return _ok([])
        return _ok([(1.0,)])

    res = _run("count forever", llm, StubExec(router), max_queries=6)
    executed = [t for t in res["trace"] if t["type"] == "query" and t.get("ok")]
    assert len(executed) == 6
    assert res["confidence"] == "low"
    assert "budget" in res["answer"].lower()


def test_executor_timeout_is_handled_cleanly():
    from app.services.cena_sql_executor import CenaSqlError

    def router(sql):
        if "anomaly_flags" in sql:
            return _ok([])
        raise CenaSqlError("query timeout after 5.0s")

    llm = FakeLLM(
        plan=json.dumps({"class": "lookup", "plan": []}),
        act=[json.dumps({"action": "sql", "sql": "SELECT net_sales FROM daily_sales_summary",
                         "purpose": "net"}),
             json.dumps({"action": "answer", "answer": "Couldn't pull it.",
                         "confidence": "low", "confidence_reason": "query failed",
                         "headline_numbers": []})],
    )
    res = _run("net sales today", llm, StubExec(router))
    assert res["answer"]  # no crash
    failed = [t for t in res["trace"] if t["type"] == "query" and not t.get("ok")]
    assert failed and "timeout" in failed[0]["error"].lower()


def test_llm_unavailable_is_honest_not_a_crash():
    from app.services.cena_llm import CenaLlmError

    def dead_llm(prompt, system=None, timeout_s=25.0):
        raise CenaLlmError("all LLM providers failed")

    res = _run("anything", dead_llm, StubExec(lambda s: _ok([])))
    assert res["confidence"] == "low"
    assert "couldn't" in res["answer"].lower() or "could not" in res["answer"].lower()
    assert res["queries"] == []


def test_proactive_flag_surfaces_one_anomaly():
    answer = json.dumps({"action": "answer", "answer": "Copperfield netted $2,541.",
                         "confidence": "medium", "confidence_reason": "one read",
                         "headline_numbers": []})
    llm = FakeLLM(plan=json.dumps({"class": "lookup", "plan": []}),
                  act=[json.dumps({"action": "sql",
                                   "sql": "SELECT store_key, net_sales FROM daily_sales_summary",
                                   "purpose": "net"}), answer])

    def router(sql):
        if "anomaly_flags" in sql:
            return _ok([("copperfield", "2026-05-30", "labor_pct", "high", 3.1)],
                       columns=("store_key", "business_date", "metric", "direction", "z_score"))
        return _ok([("copperfield", 2541.0)], columns=("store_key", "net_sales"))

    res = _run("copperfield net sales", llm, StubExec(router))
    assert "Separately" in res["answer"]
    assert any(t["type"] == "flag" for t in res["trace"])
