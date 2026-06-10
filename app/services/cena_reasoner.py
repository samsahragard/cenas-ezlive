"""C.E.N.A. Level 3 reasoning engine - a bounded agentic investigation loop.

investigate(question) does NOT translate text to one SQL query. It plans like an
analyst: classifies the question, follows the matching CENA_METHODS playbook,
emits read-only SELECTs one at a time (validating and self-repairing on
rejection), folds the rows back into a scratchpad, decides whether it is done,
CROSS-CHECKS every headline number a different way before stating it, attaches a
confidence with a reason, optionally surfaces one unasked-for anomaly, and stays
honest when the data can't answer the question.

All heavy dependencies are injectable so the loop is unit-testable with a scripted
fake LLM and a stub executor and never needs a network or real database.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

_METHODS_PATH = Path(__file__).resolve().parents[1] / "assistant_context" / "CENA_METHODS.md"
_VERIFY_REL_TOL = 0.01      # 1% relative
_VERIFY_ABS_TOL = 0.5       # for percentage-point / small-count metrics
_MAX_STEP_GUARD = 24        # hard backstop on total LLM action turns
_SCRATCH_ROWS = 15

_NUM_RE = re.compile(r"-?\$?\(?-?\d[\d,]*\.?\d*\)?%?")


# --------------------------------------------------------------------------- #
# lazy real-module wiring (overridable for tests)
# --------------------------------------------------------------------------- #
def _default_llm() -> Callable[..., str]:
    from app.services.cena_llm import get_default_llm

    return get_default_llm()


def _default_executor() -> Callable[[str], dict]:
    from app.services.cena_sql_executor import run_readonly_sql

    return run_readonly_sql


def _default_validate() -> Callable[[str], tuple[bool, str]]:
    from app.services.cena_sql_validator import validate_sql

    return validate_sql


def _default_schema_context() -> str:
    try:
        from app.services.cena_sql_schema import get_schema_context

        return get_schema_context()
    except Exception:
        return ""


def _default_memory():
    from app.services import cena_memory

    return cena_memory


def _executor_error_types() -> tuple:
    try:
        from app.services.cena_sql_executor import CenaSqlError

        return (CenaSqlError,)
    except Exception:  # pragma: no cover
        return ()


def _llm_error_types() -> tuple:
    try:
        from app.services.cena_llm import CenaLlmError

        return (CenaLlmError,)
    except Exception:  # pragma: no cover
        return ()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_methods() -> str:
    try:
        return _METHODS_PATH.read_text(encoding="utf-8")
    except Exception:
        return ""


def _playbook_for(methods: str, qclass: str) -> str:
    """STANDARDS block + the section for this class."""
    if not methods:
        return ""
    blocks = re.split(r"\n## (?=CLASS:|STANDARDS)", methods)
    standards = ""
    klass = ""
    target = f"CLASS: {qclass}".lower()
    for b in blocks:
        head = b.strip().splitlines()[0].lower() if b.strip() else ""
        if head.startswith("standards"):
            standards = "## " + b.strip()
        elif head.startswith(target):
            klass = "## " + b.strip()
    return (standards + "\n\n" + klass).strip()


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first balanced {...} object out of an LLM reply (tolerates code
    fences and surrounding prose)."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    depth = 0
    start = -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(s[start : i + 1])
                except Exception:
                    start = -1
    return None


def _ask_json(llm, system: str, prompt: str, timeout_s: float) -> Optional[dict]:
    raw = llm(prompt, system=system, timeout_s=timeout_s)
    return _extract_json(raw)


def _compact(result: dict) -> dict:
    rows = result.get("rows", []) or []
    return {
        "columns": result.get("columns", []),
        "rows": [list(r) for r in rows[:_SCRATCH_ROWS]],
        "row_count": result.get("row_count", len(rows)),
        "truncated": result.get("truncated", False),
    }


def _scratch_text(scratchpad: list[dict]) -> str:
    if not scratchpad:
        return "(no observations yet)"
    out = []
    for i, s in enumerate(scratchpad, 1):
        if "error" in s:
            out.append(f"[{i}] purpose: {s.get('purpose','')}\n    SQL: {s.get('sql','')}\n    ERROR: {s['error']}")
        else:
            r = s.get("rows", {})
            out.append(
                f"[{i}] purpose: {s.get('purpose','')}\n    SQL: {s.get('sql','')}\n"
                f"    columns: {r.get('columns')}\n    rows({r.get('row_count')}): {r.get('rows')}"
                + ("  [truncated]" if r.get("truncated") else "")
            )
    return "\n".join(out)


def _to_float(val: Any) -> Optional[float]:
    if isinstance(val, (int, float)):
        return float(val)
    if not isinstance(val, str):
        return None
    m = _NUM_RE.search(val)
    if not m:
        return None
    tok = m.group(0)
    neg = tok.startswith("(") and tok.endswith(")")
    tok = tok.strip("()").replace("$", "").replace(",", "").replace("%", "")
    try:
        f = float(tok)
        return -f if neg else f
    except ValueError:
        return None


_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_WEEK_RE = re.compile(r"\b\d{4}-W\d{1,2}\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _derive_headlines(answer: str) -> list[dict]:
    """Pull up to 2 prominent figures out of an answer that omitted
    headline_numbers, so they still get cross-checked. Dates/weeks/years are
    stripped so they don't masquerade as figures."""
    text = _YEAR_RE.sub(" ", _WEEK_RE.sub(" ", _DATE_RE.sub(" ", answer or "")))
    seen: list[float] = []
    for m in _NUM_RE.finditer(text):
        tok = m.group(0)
        if not re.search(r"\d", tok):
            continue
        neg = tok.startswith("(") and tok.endswith(")")
        clean = tok.strip("()").replace("$", "").replace(",", "").replace("%", "")
        try:
            f = -float(clean) if neg else float(clean)
        except ValueError:
            continue
        if abs(f) < 1 or f in seen:
            continue
        seen.append(f)
    seen.sort(key=abs, reverse=True)
    return [{"label": "stated figure", "value": str(v)} for v in seen[:2]]


def _numbers_in_result(result: dict) -> list[float]:
    nums: list[float] = []
    for row in (result.get("rows", []) or []):
        for cell in row:
            f = _to_float(cell)
            if f is not None:
                nums.append(f)
    return nums


def _agrees(target: float, candidates: list[float]) -> bool:
    for c in candidates:
        if abs(c - target) <= _VERIFY_ABS_TOL:
            return True
        denom = max(abs(target), abs(c), 1e-9)
        if abs(c - target) / denom <= _VERIFY_REL_TOL:
            return True
    return False


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #
_PLAN_SYS = (
    "You are C.E.N.A., a restaurant operations analyst for Cenas Kitchen. You "
    "investigate questions against a read-only SQLite analytics surface. Classify "
    "the question and write a short investigation plan. Respond with STRICT JSON "
    "only: {\"class\": one of lookup|comparison|diagnosis|recommendation, "
    "\"plan\": [\"step\", ...], \"notes\": \"what would change the answer\"}."
)

_ACT_SYS = (
    "You are C.E.N.A., investigating with read-only SQL. Each turn emit STRICT "
    "JSON for ONE action and nothing else.\n"
    "To run a query: {\"action\":\"sql\",\"sql\":\"SELECT ...\",\"purpose\":\"...\"}\n"
    "To finish:      {\"action\":\"answer\",\"answer\":\"...\",\"confidence\":"
    "\"high|medium|low\",\"confidence_reason\":\"...\",\"headline_numbers\":"
    "[{\"label\":\"...\",\"value\":\"...\"}],\"discarded\":[\"hypothesis you "
    "checked and ruled out\"]}\n"
    "Rules: exactly ONE read-only SELECT per sql action (analytics tables are "
    "unqualified; raw tables are schema-qualified like ordersdc.dm_order). Prefer "
    "the pre-aggregated analytics tables. Never invent numbers - every figure in "
    "your answer must come from query rows you observed. If the data window does "
    "not cover the question, say so honestly rather than guessing.\n"
    "ANSWER PROMPTLY: you have a HARD budget of 6 queries. As soon as the "
    "observations support a conclusion, emit the answer action - do NOT keep "
    "cross-checking a simple lookup. A single analytics-table row is often the "
    "whole answer; the loop will still cross-check your headline numbers afterward."
)


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def investigate(
    question: str,
    context: Optional[dict] = None,
    *,
    llm: Optional[Callable[..., str]] = None,
    executor: Optional[Callable[[str], dict]] = None,
    schema_context: Optional[str] = None,
    memory=None,
    max_queries: int = 6,
    time_budget_s: float = 60.0,
    _validate: Optional[Callable[[str], tuple[bool, str]]] = None,
    _methods: Optional[str] = None,
    _llm_timeout_s: float = 25.0,
) -> dict:
    """Run a bounded investigation. Returns
    {answer, confidence, confidence_reason, trace, queries}."""
    llm = llm or _default_llm()
    executor = executor or _default_executor()
    validate = _validate or _default_validate()
    schema = schema_context if schema_context is not None else _default_schema_context()
    mem = memory if memory is not None else _default_memory()
    methods = _methods if _methods is not None else _load_methods()
    exec_errs = _executor_error_types()
    llm_errs = _llm_error_types()

    t0 = time.perf_counter()

    def elapsed() -> float:
        return time.perf_counter() - t0

    def over_budget() -> bool:
        return elapsed() > time_budget_s

    trace: list[dict] = []
    queries: list[dict] = []
    scratchpad: list[dict] = []
    step = 0

    def _record_query(sql, purpose, res=None, err=None):
        if err is None:
            queries.append({"sql": sql, "purpose": purpose, "ok": True,
                            "row_count": res.get("row_count"), "elapsed_ms": res.get("elapsed_ms")})
        else:
            queries.append({"sql": sql, "purpose": purpose, "ok": False, "error": err})

    # ---- recall institutional memory ----
    try:
        recalled = mem.recall(question)
    except Exception:
        recalled = {"exemplars": [], "insights": []}

    def _recall_block() -> str:
        ex = recalled.get("exemplars", [])
        ins = recalled.get("insights", [])
        if not ex and not ins:
            return ""
        lines = []
        if ex:
            lines.append("Similar past investigations (verified) — reuse their shape:")
            for e in ex[:3]:
                lines.append(f"  Q: {e.get('question')}\n     plan: {e.get('sql_plan')}")
        if ins:
            lines.append("Verified institutional insights:")
            for i in ins[:3]:
                tag = " (STALE — re-verify, do not assume)" if i.get("needs_reverify") else ""
                lines.append(f"  - {i.get('text')}{tag}")
        return "\n".join(lines)

    # ---- PLAN ----
    plan_prompt = (
        f"SCHEMA:\n{schema}\n\n"
        + (f"MEMORY:\n{_recall_block()}\n\n" if _recall_block() else "")
        + (f"CONVERSATION CONTEXT: {json.dumps(context)}\n\n" if context else "")
        + f"QUESTION: {question}\n\nClassify and plan."
    )
    try:
        plan_obj = _ask_json(llm, _PLAN_SYS, plan_prompt, _llm_timeout_s) or {}
    except llm_errs as e:
        return _honest_failure(question, trace, queries,
                               f"the reasoning model is unavailable ({e})")
    except Exception as e:
        return _honest_failure(question, trace, queries,
                               f"planning failed ({type(e).__name__})")

    qclass = str(plan_obj.get("class", "") or "").lower().strip() or "lookup"
    if qclass not in ("lookup", "comparison", "diagnosis", "recommendation"):
        qclass = "lookup"
    plan_steps = plan_obj.get("plan", []) or []
    trace.append({"step": step, "type": "plan", "question_class": qclass,
                  "plan": plan_steps, "notes": plan_obj.get("notes", "")})
    playbook = _playbook_for(methods, qclass)

    act_sys = _ACT_SYS + ("\n\nPLAYBOOK FOR THIS QUESTION:\n" + playbook if playbook else "")

    # ---- ACT / OBSERVE / REVISE ----
    answer_obj: Optional[dict] = None
    executed = 0
    while executed < max_queries and step < _MAX_STEP_GUARD:
        step += 1
        if over_budget():
            trace.append({"step": step, "type": "limit",
                          "note": f"wall-clock budget {time_budget_s}s reached after "
                                  f"{executed} queries"})
            break
        last_call = executed >= max_queries - 1
        nudge = ("\n\nThis is your LAST available query - after it you MUST answer. If "
                 "the observations already support a conclusion, emit the ANSWER action "
                 "NOW instead of querying." if last_call else "")
        act_prompt = (
            f"SCHEMA:\n{schema}\n\nQUESTION: {question}\nCLASS: {qclass}\n"
            f"PLAN: {plan_steps}\n\nOBSERVATIONS SO FAR:\n{_scratch_text(scratchpad)}\n\n"
            f"Queries used: {executed}/{max_queries}. Emit the next action as JSON."
            + nudge
        )
        try:
            action = _ask_json(llm, act_sys, act_prompt, _llm_timeout_s) or {}
        except llm_errs:
            break
        except Exception:
            break

        kind = str(action.get("action", "")).lower()
        if kind == "answer":
            answer_obj = action
            break
        if kind != "sql":
            # malformed action - nudge by recording and retry within step guard
            scratchpad.append({"purpose": "(malformed action)", "error": "no valid action returned"})
            continue

        sql = (action.get("sql") or "").strip()
        purpose = action.get("purpose", "")
        ok, reason = validate(sql)
        repairs = 0
        while not ok and repairs < 2:
            repairs += 1
            trace.append({"step": step, "type": "repair", "attempt": repairs,
                          "rejected_sql": sql, "reason": reason})
            repair_prompt = (
                f"SCHEMA:\n{schema}\n\nYour SQL was REJECTED by the safety validator.\n"
                f"SQL: {sql}\nREASON: {reason}\n\nReturn corrected JSON "
                "{\"action\":\"sql\",\"sql\":\"...\",\"purpose\":\"...\"} that fixes the "
                "exact reason (qualify columns, drop excluded columns, single SELECT)."
            )
            try:
                fix = _ask_json(llm, act_sys, repair_prompt, _llm_timeout_s) or {}
            except Exception:
                break
            sql = (fix.get("sql") or sql).strip()
            ok, reason = validate(sql)
        if not ok:
            trace.append({"step": step, "type": "discard",
                          "note": f"abandoned a query — could not satisfy validator: {reason}"})
            scratchpad.append({"purpose": purpose, "sql": sql, "error": f"invalid SQL: {reason}"})
            continue  # validation failures don't burn the query budget

        try:
            res = executor(sql)
            trace.append({"step": step, "type": "query", "sql": sql, "purpose": purpose,
                          "ok": True, "row_count": res.get("row_count"),
                          "elapsed_ms": res.get("elapsed_ms"),
                          "truncated": res.get("truncated", False)})
            _record_query(sql, purpose, res=res)
            scratchpad.append({"purpose": purpose, "sql": sql, "rows": _compact(res)})
        except exec_errs as e:
            reason = getattr(e, "reason", str(e))
            trace.append({"step": step, "type": "query", "sql": sql, "purpose": purpose,
                          "ok": False, "error": reason})
            _record_query(sql, purpose, err=reason)
            scratchpad.append({"purpose": purpose, "sql": sql, "error": reason})
        executed += 1

    # ---- budget wall: synthesize a final answer from what we already pulled ----
    forced = False
    if answer_obj is None:
        forced = True
        answer_obj = _final_synthesis(llm, question, scratchpad, _llm_timeout_s, llm_errs) \
            or _forced_answer(scratchpad, executed, max_queries)
        trace.append({"step": step, "type": "limit",
                      "note": "reached the query budget - concluded from the observations "
                              "already gathered"})

    answer_text = str(answer_obj.get("answer", "")).strip()
    confidence = str(answer_obj.get("confidence", "medium")).lower().strip() or "medium"
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    confidence_reason = str(answer_obj.get("confidence_reason", "")).strip()
    for d in answer_obj.get("discarded", []) or []:
        trace.append({"step": step, "type": "discard", "note": d})

    headlines = answer_obj.get("headline_numbers", []) or []
    # backstop: if the answer states figures but the model gave no headline_numbers,
    # derive up to 2 so numbers still get cross-checked (never present an unverified
    # number as fact).
    if not headlines and not forced:
        headlines = _derive_headlines(answer_text)

    # ---- VERIFY headline numbers a DIFFERENT way ----
    verify_outcomes: list[bool] = []
    for h in headlines[:2]:
        if over_budget() or step >= _MAX_STEP_GUARD:
            break
        step += 1
        target = _to_float(h.get("value"))
        if target is None:
            continue
        vsys = (
            "You verify a headline number by computing it a DIFFERENT way (e.g. a "
            "weekly figure as the sum of dailies, or an analytics value recomputed "
            "from the raw source). Return STRICT JSON "
            "{\"sql\":\"SELECT ...\",\"purpose\":\"...\"} — one read-only SELECT."
        )
        vprompt = (
            f"SCHEMA:\n{schema}\n\nQUESTION: {question}\n"
            f"ANSWER GIVEN: {answer_text}\n"
            f"HEADLINE TO CROSS-CHECK: {h.get('label')} = {h.get('value')}\n\n"
            f"OBSERVATIONS:\n{_scratch_text(scratchpad)}\n\n"
            "Write ONE different query that should reproduce that number."
        )
        try:
            vobj = _ask_json(llm, vsys, vprompt, _llm_timeout_s) or {}
        except Exception:
            continue
        vsql = (vobj.get("sql") or "").strip()
        if not vsql:
            continue
        ok, reason = validate(vsql)
        if not ok:
            try:
                fix = _ask_json(llm, vsys,
                                f"REJECTED: {reason}\nFix this SQL: {vsql}", _llm_timeout_s) or {}
                vsql = (fix.get("sql") or "").strip()
                ok, reason = validate(vsql)
            except Exception:
                ok = False
        if not ok or not vsql:
            trace.append({"step": step, "type": "verify", "headline": h.get("label"),
                          "ok": False, "note": "no valid cross-check query"})
            continue
        try:
            vres = executor(vsql)
            _record_query(vsql, f"verify: {h.get('label')}", res=vres)
            agree = _agrees(target, _numbers_in_result(vres))
            verify_outcomes.append(agree)
            trace.append({"step": step, "type": "verify", "headline": h.get("label"),
                          "target": target, "cross_check_sql": vsql, "agree": agree,
                          "cross_check_values": _numbers_in_result(vres)[:8]})
        except exec_errs as e:
            trace.append({"step": step, "type": "verify", "headline": h.get("label"),
                          "ok": False, "error": getattr(e, "reason", str(e))})

    # ---- confidence reconciliation from verification ----
    if verify_outcomes:
        if all(verify_outcomes):
            if not forced and confidence != "low":
                if not confidence_reason:
                    confidence_reason = "two independent computations agree"
        elif not any(verify_outcomes):
            confidence = "low"
            confidence_reason = (confidence_reason + " " if confidence_reason else "") + \
                "cross-check disagreed — the figure is reported as uncertain"
            answer_text += ("\n\n(Note: I could not independently confirm the figure "
                            "above by a second method — treat it as uncertain.)")
        else:
            if confidence == "high":
                confidence = "medium"
            confidence_reason = (confidence_reason + " " if confidence_reason else "") + \
                "cross-checks were mixed"
    else:
        if headlines and confidence == "high":
            confidence = "medium"
            confidence_reason = (confidence_reason + " " if confidence_reason else "") + \
                "no independent cross-check was available"
    if forced and confidence == "high":
        # a budget-wall synthesis can be solid but was not allowed a fresh
        # cross-check pass - cap at medium, never claim high
        confidence = "medium"
        confidence_reason = (confidence_reason + " " if confidence_reason else "") + \
            "concluded at the query budget"

    # ---- D4 proactive synthesis: ONE unasked-for anomaly, if budget remains ----
    if not over_budget() and step < _MAX_STEP_GUARD:
        step += 1
        flag = _proactive_flag(executor, validate, exec_errs, scratchpad)
        if flag:
            answer_text += "\n\n" + flag
            trace.append({"step": step, "type": "flag", "note": flag})

    # ---- record to memory (verified only when every headline cross-checked clean) ----
    verified = bool(verify_outcomes) and all(verify_outcomes) and not forced
    try:
        mem.record(question, answer_text, confidence, queries,
                   outcome=qclass, verified=verified, verified_by="reasoner")
    except Exception:
        pass

    if not confidence_reason:
        confidence_reason = "single computation; not independently cross-checked"

    return {
        "answer": answer_text or "I could not determine an answer from the available data.",
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "trace": trace,
        "queries": queries,
    }


# --------------------------------------------------------------------------- #
# honesty rails / helpers
# --------------------------------------------------------------------------- #
def _honest_failure(question, trace, queries, why: str) -> dict:
    trace = list(trace) + [{"type": "limit", "note": why}]
    return {
        "answer": f"I couldn't investigate that right now — {why}. No numbers are "
                  "reported rather than risk a wrong one.",
        "confidence": "low",
        "confidence_reason": why,
        "trace": trace,
        "queries": queries,
    }


def _final_synthesis(llm, question, scratchpad, timeout, llm_errs) -> Optional[dict]:
    """At the query budget, ask the model to conclude from the observations it
    ALREADY gathered (no new SQL). Recovers cases where the answer was in hand but
    the loop kept over-investigating. Never invents a number."""
    if not scratchpad:
        return None
    sys = (
        "You are OUT of query budget. Using ONLY the observations below, give your "
        "best answer now - no more SQL. Respond with STRICT JSON {\"answer\":\"...\","
        "\"confidence\":\"medium|low\",\"confidence_reason\":\"...\",\"headline_numbers\":"
        "[{\"label\":\"...\",\"value\":\"...\"}],\"discarded\":[...]}. If the observations "
        "support a conclusion, state it; if they do not, say what you could and could "
        "not determine. NEVER invent a number that isn't in the observations."
    )
    prompt = (f"QUESTION: {question}\n\nOBSERVATIONS:\n{_scratch_text(scratchpad)}\n\n"
              "Answer now from these observations.")
    try:
        obj = _ask_json(llm, sys, prompt, timeout)
    except llm_errs:
        return None
    except Exception:
        return None
    if obj and str(obj.get("answer", "")).strip():
        return obj
    return None


def _forced_answer(scratchpad: list[dict], executed: int, max_queries: int) -> dict:
    checked = "; ".join(s.get("purpose", "") for s in scratchpad if s.get("purpose"))
    return {
        "answer": (
            f"I reached my query budget ({executed}/{max_queries}) before fully "
            "concluding. Here is what I could establish: "
            + (checked or "no conclusive observations") + ". I have NOT verified a "
            "headline figure, so I'm not stating one as fact."
        ),
        "confidence": "low",
        "confidence_reason": "budget exhausted before a verified conclusion",
        "headline_numbers": [],
        "discarded": [],
    }


def _proactive_flag(executor, validate, exec_errs, scratchpad) -> str:
    """One short flag line if anomaly_flags carries something material. Best-effort:
    never raises into the answer."""
    stores = set()
    for s in scratchpad:
        for row in (s.get("rows", {}).get("rows", []) or []):
            for cell in row:
                if cell in ("copperfield", "tomball"):
                    stores.add(cell)
    where = ""
    if len(stores) == 1:
        where = f" WHERE store_key = '{next(iter(stores))}'"
    sql = ("SELECT store_key, business_date, metric, direction, z_score "
           "FROM anomaly_flags" + where +
           " ORDER BY ABS(z_score) DESC LIMIT 1")
    ok, _ = validate(sql)
    if not ok:
        return ""
    try:
        res = executor(sql)
    except exec_errs:
        return ""
    rows = res.get("rows", []) or []
    if not rows:
        return ""
    r = rows[0]
    try:
        store, day, metric, direction, z = r[0], r[1], r[2], r[3], r[4]
    except Exception:
        return ""
    return (f"Separately - {store} {metric} ran unusually {direction} on {day} "
            f"(z={float(z):.1f}); worth a look.")
