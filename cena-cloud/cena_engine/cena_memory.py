"""C.E.N.A. Level 3 institutional memory.

Two kinds of durable, VERIFIED knowledge make next month's answers sharper than
this month's:

  * exemplars  - (question -> SQL plan) pairs that were eval-verified or
                 Sam-confirmed. recall() returns the top-k similar ones as
                 few-shot guidance, so the tenth labor question is sharper than
                 the first.
  * insights   - verified findings ("tomball dinner avg check runs ~$4 over
                 copperfield") with the evidence date and the SQL that proved
                 them, plus a staleness horizon. recall() surfaces relevant
                 ones; past their horizon they come back flagged needs_reverify
                 (re-prove, never assume).

Only VERIFIED material is admitted as exemplars/insights - no rumor compounding.
Every investigation is also appended to an audit log (verified or not) for the
eval/orchestrator, but the log never feeds recall().

Storage: %CENA_L3_DATA_DIR%\\memory\\cena_memory.db (env-overridable; tests point
it at a tmp dir). Pure stdlib sqlite3; keyword-overlap recall (no embeddings).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_STALENESS_DAYS = 30
_DEFAULT_DATA_DIR = r"C:\Users\sam\cena-l3data"
_STOPWORDS = {
    "the", "a", "an", "of", "for", "to", "in", "on", "at", "and", "or", "is",
    "are", "was", "were", "be", "do", "did", "does", "how", "what", "why",
    "which", "when", "who", "we", "our", "us", "it", "its", "this", "that",
    "by", "with", "as", "vs", "than", "then", "from", "have", "has", "had",
    "show", "tell", "me", "give", "get", "many", "much", "about", "over",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _today() -> date:
    override = os.getenv("CENA_L3_TODAY")
    if override:
        try:
            return date.fromisoformat(override)
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def _data_dir() -> Path:
    return Path(os.getenv("CENA_L3_DATA_DIR", _DEFAULT_DATA_DIR))


def _db_path() -> Path:
    # Cloud override (additive): CENA_MEMORY_DB, when set, IS the memory DB path
    # verbatim, so it can point at a writable location (e.g. /var/data/...).
    # Default (local runtime, no env) is unchanged: <DATA_DIR>/memory/cena_memory.db.
    direct = os.getenv("CENA_MEMORY_DB")
    if direct:
        return Path(direct)
    return _data_dir() / "memory" / "cena_memory.db"


def tokenize(text: str) -> set[str]:
    return {
        t for t in _TOKEN_RE.findall((text or "").lower())
        if len(t) > 1 and t not in _STOPWORDS
    }


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS exemplars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            question_norm TEXT NOT NULL,
            tokens TEXT NOT NULL,
            sql_plan TEXT NOT NULL,
            answer TEXT,
            verified_by TEXT,
            created_at TEXT NOT NULL,
            use_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            tokens TEXT NOT NULL,
            evidence_date TEXT,
            sql TEXT,
            staleness_days INTEGER NOT NULL DEFAULT 30,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS investigation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            answer TEXT,
            confidence TEXT,
            verified INTEGER NOT NULL DEFAULT 0,
            outcome TEXT,
            meta TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    return conn


def _overlap(q_tokens: set[str], row_tokens: set[str]) -> float:
    if not q_tokens or not row_tokens:
        return 0.0
    inter = q_tokens & row_tokens
    if not inter:
        return 0.0
    # length-normalized overlap, lightly favouring rarer (longer) shared tokens
    weight = sum(1.0 + min(len(t), 8) / 8.0 for t in inter)
    return weight / (len(q_tokens) ** 0.5 * len(row_tokens) ** 0.5)


def recall(question: str, *, k: int = 3) -> dict[str, list[dict[str, Any]]]:
    """Return {"exemplars": [...], "insights": [...]} most relevant to the
    question. Insights past their staleness horizon are included but flagged
    needs_reverify=True (re-prove, do not assume)."""
    q_tokens = tokenize(question)
    if not q_tokens:
        return {"exemplars": [], "insights": []}
    out_ex: list[dict[str, Any]] = []
    out_in: list[dict[str, Any]] = []
    conn = _connect()
    try:
        ex_rows = conn.execute(
            "SELECT id, question, tokens, sql_plan, answer, verified_by, use_count "
            "FROM exemplars"
        ).fetchall()
        scored_ex = []
        for rid, q, toks, plan, ans, vby, uc in ex_rows:
            s = _overlap(q_tokens, set(toks.split()))
            if s > 0:
                scored_ex.append((s, rid, q, plan, ans, vby, uc))
        scored_ex.sort(key=lambda r: r[0], reverse=True)
        used_ids = []
        for s, rid, q, plan, ans, vby, uc in scored_ex[:k]:
            used_ids.append(rid)
            out_ex.append({
                "id": rid, "question": q, "score": round(s, 4),
                "sql_plan": json.loads(plan) if plan else [],
                "answer": ans, "verified_by": vby, "use_count": uc,
            })
        if used_ids:
            now = _now_iso()
            conn.executemany(
                "UPDATE exemplars SET use_count = use_count + 1, last_used_at = ? "
                "WHERE id = ?",
                [(now, rid) for rid in used_ids],
            )
            conn.commit()

        in_rows = conn.execute(
            "SELECT id, text, tokens, evidence_date, sql, staleness_days FROM insights"
        ).fetchall()
        scored_in = []
        for rid, txt, toks, ev, sql, stale in in_rows:
            s = _overlap(q_tokens, set(toks.split()))
            if s > 0:
                scored_in.append((s, rid, txt, ev, sql, stale))
        scored_in.sort(key=lambda r: r[0], reverse=True)
        today = _today()
        for s, rid, txt, ev, sql, stale in scored_in[:k]:
            needs = False
            if ev:
                try:
                    age = (today - date.fromisoformat(ev)).days
                    needs = age > int(stale)
                except ValueError:
                    needs = False
            out_in.append({
                "id": rid, "text": txt, "score": round(s, 4),
                "evidence_date": ev, "sql": sql,
                "needs_reverify": needs,
            })
    finally:
        conn.close()
    return {"exemplars": out_ex, "insights": out_in}


def promote_exemplar(
    question: str, sql_plan: list[str], answer: str, verified_by: str = "eval"
) -> None:
    """Admit a VERIFIED (question -> SQL plan) pair as few-shot guidance.
    Deduplicates on the normalized question (keeps the freshest plan)."""
    norm = " ".join(sorted(tokenize(question)))
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT id FROM exemplars WHERE question_norm = ?", (norm,)
        ).fetchone()
        plan_json = json.dumps(list(sql_plan or []))
        toks = " ".join(sorted(tokenize(question)))
        if existing:
            conn.execute(
                "UPDATE exemplars SET sql_plan = ?, answer = ?, verified_by = ?, "
                "created_at = ? WHERE id = ?",
                (plan_json, answer, verified_by, _now_iso(), existing[0]),
            )
        else:
            conn.execute(
                "INSERT INTO exemplars (question, question_norm, tokens, sql_plan, "
                "answer, verified_by, created_at) VALUES (?,?,?,?,?,?,?)",
                (question, norm, toks, plan_json, answer, verified_by, _now_iso()),
            )
        conn.commit()
    finally:
        conn.close()


def add_insight(
    text: str,
    evidence_date: Optional[str] = None,
    sql: Optional[str] = None,
    staleness_days: int = DEFAULT_STALENESS_DAYS,
) -> None:
    """Record a VERIFIED finding with its proof. evidence_date defaults to today."""
    ev = evidence_date or _today().isoformat()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO insights (text, tokens, evidence_date, sql, staleness_days, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (text, " ".join(sorted(tokenize(text))), ev, sql, int(staleness_days),
             _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def record(
    question: str,
    answer: str,
    confidence: str,
    queries: Optional[list] = None,
    outcome: str = "",
    verified: bool = False,
    **meta: Any,
) -> None:
    """Called after each investigation. Always appends to the audit log; only
    when verified=True does the (question -> SQL plan) become an exemplar."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO investigation_log (question, answer, confidence, verified, "
            "outcome, meta, created_at) VALUES (?,?,?,?,?,?,?)",
            (question, answer, confidence, 1 if verified else 0, outcome,
             json.dumps(meta, default=str), _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    if verified:
        plan = [q.get("sql") for q in (queries or []) if isinstance(q, dict) and q.get("sql")]
        promote_exemplar(question, plan, answer, verified_by=str(meta.get("verified_by", "investigation")))


def stats() -> dict[str, int]:
    conn = _connect()
    try:
        return {
            "exemplars": conn.execute("SELECT COUNT(*) FROM exemplars").fetchone()[0],
            "insights": conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0],
            "investigations": conn.execute(
                "SELECT COUNT(*) FROM investigation_log"
            ).fetchone()[0],
        }
    finally:
        conn.close()
