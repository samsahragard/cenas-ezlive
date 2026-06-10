"""C.E.N.A. Level 3 evaluation harness.

Scores the reasoning engine against data/cena_gold_questions.json on three axes:

  * accuracy        - numeric/text answers match the hand-derived ground truth
  * diagnosis       - "why" answers NAME the planted dominant driver
  * verification    - did the reasoner cross-check its headline numbers a second
                      way (inspected from result['trace'])

Ground truth lives in the gold file (baked) and can be re-derived from the real
snapshots with --rederive (reference SQL run DIRECTLY on the data, never via the
reasoner). The eval is also the TRAINER: questions the reasoner gets RIGHT are
auto-promoted into the memory exemplar store; Sam-corrected misses become new
gold entries via promote_to_gold().

Workflow when an answer is wrong:
  1. The miss is printed with the reasoner's queries vs the reference SQL.
  2. If Sam corrects it, call promote_to_gold(question, corrected_answer,
     reference_sql) -> the question becomes a permanent regression entry.
  3. Recurring miss patterns are the signal to add a new deterministic tool or a
     new analytics table (cheaper + unambiguous than re-deriving each time).

Usage:
  python scripts/cena_sql_eval.py                 # full live eval (needs an LLM)
  python scripts/cena_sql_eval.py --rederive      # validate gold vs snapshots only
  python scripts/cena_sql_eval.py --only-class diagnosis --limit 3
  python scripts/cena_sql_eval.py --json-out run.json --no-promote
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLD_PATH = ROOT / "data" / "cena_gold_questions.json"
logger = logging.getLogger("cena_sql_eval")

_NUM_RE = re.compile(r"-?\$?\(?-?\d[\d,]*\.?\d*\)?%?")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_WEEK_RE = re.compile(r"\b\d{4}-W\d{1,2}\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_SOURCES = {
    "appdb": "appdb.sqlite", "toast": "toast.sqlite", "toastdm": "toastdm.sqlite",
    "ordersdc": "ordersdc.sqlite", "driverdc": "driverdc.sqlite",
}


# --------------------------------------------------------------------------- #
# scoring primitives
# --------------------------------------------------------------------------- #
def _strip_noise(text: str) -> str:
    text = _DATE_RE.sub(" ", text or "")
    text = _WEEK_RE.sub(" ", text)
    text = _YEAR_RE.sub(" ", text)
    return text


def extract_numbers(text: str) -> list[float]:
    out: list[float] = []
    for m in _NUM_RE.finditer(_strip_noise(text)):
        tok = m.group(0)
        if not re.search(r"\d", tok):
            continue
        neg = tok.startswith("(") and tok.endswith(")")
        tok = tok.strip("()").replace("$", "").replace(",", "").replace("%", "")
        try:
            f = float(tok)
            out.append(-f if neg else f)
        except ValueError:
            continue
    return out


def number_match(expected: float, tol: float, candidates: list[float]) -> bool:
    for c in candidates:
        if tol == 0:
            if abs(c - expected) < 0.5:        # counts / exact dollars
                return True
        else:
            denom = max(abs(expected), 1e-9)
            if abs(c - expected) / denom <= tol:
                return True
    return False


def text_match(accept: list[str], answer: str) -> bool:
    low = (answer or "").lower()
    return any(a.lower() in low for a in accept)


def score_answer(entry: dict, answer: str) -> tuple[bool, str]:
    """Return (correct, detail)."""
    exp = entry["expected"]
    etype = exp["type"]
    if etype == "number":
        cands = extract_numbers(answer)
        ok = number_match(float(exp["value"]), float(exp.get("tolerance", 0)), cands)
        return ok, f"expected {exp['value']} (tol {exp.get('tolerance', 0)}); found {cands[:8]}"
    if etype in ("text", "driver"):
        ok = text_match(exp.get("accept", [exp["value"]]), answer)
        return ok, f"expected one of {exp.get('accept')}"
    if etype == "no_data":  # legacy alias; gold uses class no_data with text expected
        ok = text_match(exp.get("accept", []), answer)
        return ok, "expected an honest decline"
    return False, f"unknown expected.type {etype}"


def stated_a_number(answer: str) -> bool:
    return len(extract_numbers(answer)) > 0


def trace_has_verification(result: dict) -> bool:
    return any(t.get("type") == "verify" and t.get("agree") for t in result.get("trace", []))


# --------------------------------------------------------------------------- #
# reference re-derivation (DIRECT on snapshots, independent of the reasoner)
# --------------------------------------------------------------------------- #
def _snap_dir() -> Path:
    return Path(os.getenv("CENA_L3_DATA_DIR", r"C:\Users\sam\cena-l3data")) / "snapshots"


def _open_reference_conn() -> sqlite3.Connection:
    snap = _snap_dir()
    main = snap / "cena_analytics.db"
    if main.exists():
        uri = "file:" + main.resolve().as_posix() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect("file::memory:?cache=private", uri=True)
    for alias, fn in _SOURCES.items():
        p = snap / fn
        if p.exists():
            conn.execute(
                f"ATTACH DATABASE 'file:{p.resolve().as_posix()}?mode=ro' AS {alias}"
            )
    return conn


def rederive(entry: dict, conn: sqlite3.Connection) -> list:
    rows: list = []
    for sql in entry.get("reference_sql", []):
        rows = conn.execute(sql).fetchall()
    return rows


def _rederive_all(gold: list[dict]) -> int:
    conn = _open_reference_conn()
    fails = 0
    try:
        for e in gold:
            try:
                rows = rederive(e, conn)
                flat = [c for r in rows for c in r]
                exp = e["expected"]
                ok = True
                detail = ""
                if exp["type"] == "number":
                    nums = [float(x) for x in flat if isinstance(x, (int, float))]
                    ok = number_match(float(exp["value"]), float(exp.get("tolerance", 0)), nums)
                    detail = f"ref={nums[:6]} expected={exp['value']}"
                else:
                    text = " ".join(str(x) for x in flat)
                    # for text/driver/no_data the reference is context, not a strict
                    # string equality - just confirm the query RUNS and returns rows
                    detail = f"ref rows={rows[:4]}"
                status = "ok " if ok else "MISMATCH"
                if not ok:
                    fails += 1
                logger.info("[%s] %-9s %s | %s", e["id"], e["class"], status, detail)
            except Exception as ex:
                fails += 1
                logger.error("[%s] reference SQL FAILED: %s", e.get("id"), ex)
    finally:
        conn.close()
    return fails


# --------------------------------------------------------------------------- #
# memory promotion
# --------------------------------------------------------------------------- #
def _promote_correct(entry: dict, result: dict) -> None:
    try:
        from app.services import cena_memory

        plan = [q.get("sql") for q in result.get("queries", []) if q.get("ok") and q.get("sql")]
        cena_memory.promote_exemplar(entry["question"], plan, result.get("answer", ""),
                                     verified_by="eval")
    except Exception:  # memory optional / may be mid-build
        logger.debug("promotion skipped", exc_info=True)


def promote_to_gold(question: str, corrected_answer: dict, reference_sql: list[str],
                    gold_path: Path = GOLD_PATH) -> str:
    """Append a Sam-corrected miss as a new permanent gold entry (regression test).
    corrected_answer is an `expected` dict ({type, value, tolerance, accept})."""
    data = json.loads(gold_path.read_text(encoding="utf-8"))
    qs = data["questions"]
    new_id = f"G{len(qs) + 1}"
    qs.append({
        "id": new_id, "class": corrected_answer.get("class", "lookup"),
        "store_scope": corrected_answer.get("store_scope"),
        "question": question, "expected": corrected_answer,
        "reference_sql": reference_sql, "notes": "promoted from a corrected miss",
    })
    gold_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return new_id


# --------------------------------------------------------------------------- #
# main eval
# --------------------------------------------------------------------------- #
def run_eval(gold: list[dict], reasoner: Optional[Callable[[str], dict]] = None,
             promote: bool = True) -> dict:
    if reasoner is None:
        from app.services.cena_reasoner import investigate as reasoner  # type: ignore

    per_class: dict[str, list[bool]] = {}
    diagnosis_results: list[bool] = []
    number_answers = 0
    verified_answers = 0
    misses: list[dict] = []
    t0 = time.perf_counter()
    total_queries = 0

    for e in gold:
        result = reasoner(e["question"])
        answer = result.get("answer", "")
        total_queries += len(result.get("queries", []))
        correct, detail = score_answer(e, answer)
        per_class.setdefault(e["class"], []).append(correct)
        if e["class"] == "diagnosis":
            diagnosis_results.append(correct)
        if e["class"] != "no_data" and stated_a_number(answer):
            number_answers += 1
            if trace_has_verification(result):
                verified_answers += 1
        if correct and promote:
            _promote_correct(e, result)
        if not correct:
            misses.append({
                "id": e["id"], "class": e["class"], "question": e["question"],
                "expected": e["expected"], "detail": detail,
                "answer": answer, "confidence": result.get("confidence"),
                "reasoner_queries": [q.get("sql") for q in result.get("queries", [])],
                "reference_sql": e.get("reference_sql"),
            })

    elapsed = time.perf_counter() - t0
    total = sum(len(v) for v in per_class.values())
    correct_total = sum(sum(v) for v in per_class.values())
    diag_total = len(diagnosis_results)
    return {
        "total": total,
        "correct": correct_total,
        "accuracy": correct_total / total if total else 0.0,
        "diagnosis_total": diag_total,
        "diagnosis_correct": sum(diagnosis_results),
        "diagnosis_accuracy": (sum(diagnosis_results) / diag_total) if diag_total else None,
        "number_answers": number_answers,
        "verified_answers": verified_answers,
        "verification_rate": (verified_answers / number_answers) if number_answers else None,
        "per_class": {k: (sum(v), len(v)) for k, v in per_class.items()},
        "elapsed_s": round(elapsed, 1),
        "total_queries": total_queries,
        "misses": misses,
    }


def _print_report(rep: dict) -> None:
    print("\n================ C.E.N.A. L3 EVAL ================")
    print(f"accuracy            : {rep['correct']}/{rep['total']} = {rep['accuracy']*100:.1f}%")
    if rep["diagnosis_accuracy"] is not None:
        print(f"diagnosis-correct   : {rep['diagnosis_correct']}/{rep['diagnosis_total']} "
              f"= {rep['diagnosis_accuracy']*100:.1f}%  (named the planted driver)")
    if rep["verification_rate"] is not None:
        print(f"verification-rate   : {rep['verified_answers']}/{rep['number_answers']} "
              f"= {rep['verification_rate']*100:.1f}%  (headline numbers cross-checked)")
    print("per-class           : " + ", ".join(
        f"{k} {c}/{n}" for k, (c, n) in rep["per_class"].items()))
    print(f"wall time           : {rep['elapsed_s']}s over {rep['total_queries']} queries")
    if rep["misses"]:
        print(f"\n---- {len(rep['misses'])} MISS(ES) ----")
        for m in rep["misses"]:
            print(f"\n[{m['id']}] {m['class']}: {m['question']}")
            print(f"  expected : {m['expected']}  ({m['detail']})")
            print(f"  answer   : {m['answer'][:300]}")
            print(f"  reasoner : {m['reasoner_queries']}")
            print(f"  reference: {m['reference_sql']}")
    print("=================================================\n")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="C.E.N.A. Level 3 eval harness")
    p.add_argument("--gold", default=str(GOLD_PATH))
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--only-class", default="")
    p.add_argument("--json-out", default="")
    p.add_argument("--rederive", action="store_true",
                   help="validate gold ground truth vs snapshots; no reasoner")
    p.add_argument("--no-promote", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    data = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    gold = data["questions"]
    if args.only_class:
        gold = [g for g in gold if g["class"] == args.only_class]
    if args.limit:
        gold = gold[: args.limit]

    if args.rederive:
        fails = _rederive_all(gold)
        print(f"\nREDERIVE: {len(gold) - fails}/{len(gold)} reference queries reconcile.")
        return 1 if fails else 0

    rep = run_eval(gold, promote=not args.no_promote)
    _print_report(rep)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
