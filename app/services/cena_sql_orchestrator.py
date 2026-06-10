"""Hybrid router for the Cenas assistant: deterministic tools first, then the
C.E.N.A. Level 3 investigation engine.

The runtime already runs its fast, proven deterministic tool match first. When no
tool matches, what used to become a "saved for Sam review / no tool" turn becomes
"investigate the data": answer_question() runs cena_reasoner.investigate() and
packages a bubble payload - answer, confidence, and a human-readable "show work"
trace (the plan, every query, what was checked and discarded, the verification).
That trace is the trust product: a top manager can audit any number down to its
SQL.

Graceful failure is a hard rule: a clean "couldn't pull that" message, never a
stack trace, never a fabricated number.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

_CONF_LABEL = {"high": "High confidence", "medium": "Medium confidence", "low": "Low confidence"}


def _default_investigate() -> Callable[..., dict]:
    from app.services.cena_reasoner import investigate

    return investigate


def format_show_work(result: dict) -> str:
    """Render the investigation trace as an auditable, human-readable path."""
    trace = result.get("trace", []) or []
    lines: list[str] = []
    for t in trace:
        kind = t.get("type")
        if kind == "plan":
            lines.append(f"Plan ({t.get('question_class', '?')}): "
                         + "; ".join(t.get("plan", []) or []) or "Plan: (none)")
        elif kind == "query":
            status = "ok" if t.get("ok") else f"FAILED: {t.get('error', '')}"
            rc = t.get("row_count")
            lines.append(f"Query [{status}] - {t.get('purpose', '')}\n    {t.get('sql', '')}"
                         + (f"\n    -> {rc} row(s)" if rc is not None else ""))
        elif kind == "repair":
            lines.append(f"Repair (attempt {t.get('attempt')}): validator said "
                         f"\"{t.get('reason', '')}\" - corrected and retried.")
        elif kind == "verify":
            if t.get("agree") is True:
                lines.append(f"Verified \"{t.get('headline')}\" a second way - the "
                             "two computations agree.")
            elif t.get("agree") is False:
                lines.append(f"Cross-check of \"{t.get('headline')}\" DISAGREED - "
                             "reported as uncertain.")
            else:
                lines.append(f"Tried to cross-check \"{t.get('headline')}\": "
                             f"{t.get('note') or t.get('error', 'no valid query')}.")
        elif kind == "discard":
            lines.append(f"Ruled out: {t.get('note', '')}")
        elif kind == "flag":
            lines.append(f"Noticed: {t.get('note', '')}")
        elif kind == "limit":
            lines.append(f"Limit: {t.get('note', '')}")
    conf = result.get("confidence", "medium")
    lines.append("")
    lines.append(f"{_CONF_LABEL.get(conf, conf)} - {result.get('confidence_reason', '')}")
    return "\n".join(lines).strip()


def answer_question(
    question: str,
    principal: Optional[dict] = None,
    context: Optional[dict] = None,
    *,
    deterministic_fn: Optional[Callable[[str, Optional[dict], Optional[dict]], Optional[dict]]] = None,
    investigate_fn: Optional[Callable[..., dict]] = None,
) -> dict:
    """Route a question to an answer.

    Returns a bubble-ready dict:
      {ok, answer, confidence, confidence_reason, route, trace, queries, show_work}
    route is "deterministic" | "investigation" | "error".

    deterministic_fn (optional) lets the caller try a fast proven tool first; if
    it returns a dict, that wins and no investigation runs. The CK runtime already
    does its own tool match upstream, so it calls this only on the no-tool path and
    leaves deterministic_fn unset.
    """
    q = (question or "").strip()
    if not q:
        return {"ok": False, "answer": "Ask me a question about the business and I'll "
                "dig into the data.", "confidence": "low", "confidence_reason": "empty "
                "question", "route": "error", "trace": [], "queries": [], "show_work": ""}

    if deterministic_fn is not None:
        try:
            tool = deterministic_fn(q, principal, context)
        except Exception:
            tool = None
        if isinstance(tool, dict) and tool.get("answer"):
            tool.setdefault("route", "deterministic")
            tool.setdefault("confidence", "high")
            tool.setdefault("confidence_reason", "deterministic tool with a fixed query")
            tool.setdefault("trace", [])
            tool.setdefault("queries", [])
            tool.setdefault("show_work", "Answered by a verified deterministic tool "
                            "(fixed query, no model reasoning).")
            tool.setdefault("ok", True)
            return tool

    investigate = investigate_fn or _default_investigate()
    try:
        result = investigate(q, context)
    except Exception as e:  # never leak a stack trace into the bubble
        return {
            "ok": False,
            "answer": "I couldn't pull that one safely right now. Nothing was made up - "
                      "try rephrasing, or I can save it for a closer look.",
            "confidence": "low",
            "confidence_reason": f"investigation error: {type(e).__name__}",
            "route": "error",
            "trace": [],
            "queries": [],
            "show_work": "",
        }
    return {
        "ok": True,
        "answer": result.get("answer", ""),
        "confidence": result.get("confidence", "medium"),
        "confidence_reason": result.get("confidence_reason", ""),
        "route": "investigation",
        "trace": result.get("trace", []),
        "queries": result.get("queries", []),
        "show_work": format_show_work(result),
    }
