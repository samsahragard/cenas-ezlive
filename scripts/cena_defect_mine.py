"""cena_defect_mine.py - nightly learning loop for the C.E.N.A. supervisor lane.

Reads the turn mirror (assistant_turns.jsonl) + conversations DB and produces:
  * a dated defect report in cenas-kitchen-runtime\\audit_outputs\\
  * exemplar/eval candidates (every medium/low turn, with its CK rescue)
    appended to cena-ai-assistant\\cena_exemplar_candidates.jsonl
  * a one-line digest posted to the LAN hub

Runs two ways:
  - standalone:  python scripts\\cena_defect_mine.py [--since-hours 24]
  - in-process:  the runtime's sweeper thread calls run_defect_mine() daily
    (see assistant_conversations.start_sweeper) - no scheduled task needed.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

TURNS_LOG = Path(os.getenv("ASSISTANT_TURNS_LOG") or r"C:\Users\sam\cena-ai-assistant\assistant_turns.jsonl")
CANDIDATES = Path(r"C:\Users\sam\cena-ai-assistant\cena_exemplar_candidates.jsonl")
REPORT_DIR = Path(r"C:\Users\sam\cenas-kitchen-runtime\audit_outputs")


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


_L3_DB_ROOT = Path(r"C:\Users\sam\cena-l3data\DB")
_ALIAS_PATHS = {
    "appdb": _L3_DB_ROOT / "app" / "appdb.sqlite",
    "toast": _L3_DB_ROOT / "toast" / "labor" / "toast.sqlite",
    "toastdm": _L3_DB_ROOT / "toast" / "emp" / "toastdm.sqlite",
    "ordersdc": _L3_DB_ROOT / "orders" / "ezcater" / "ordersdc.sqlite",
    "driverdc": _L3_DB_ROOT / "drivers" / "driverdc.sqlite",
}


def schema_coverage() -> dict:
    """Diff the tables that actually hold data in Cena's DB tree against the
    curated allowlist her engine reasons from (cena_sql_schema). Non-empty
    tables missing from the allowlist are invisible to her - flag them so new
    domains get wired instead of silently ignored."""
    import sqlite3
    import sys

    try:
        try:
            from app.services import cena_sql_schema as schema
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from app.services import cena_sql_schema as schema
        allow = schema.get_allowlist()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"allowlist unavailable: {exc}"}

    curated: dict[str, set[str]] = {}
    for key in allow:
        if "." in key:
            alias, table = key.split(".", 1)
            curated.setdefault(alias, set()).add(table)

    unmapped: list[str] = []
    for alias, path in _ALIAS_PATHS.items():
        if not path.exists():
            continue
        try:
            con = sqlite3.connect(str(path))
            for (t,) in con.execute("select name from sqlite_master where type='table'"):
                if t.startswith("sqlite_") or t in curated.get(alias, set()):
                    continue
                try:
                    n = con.execute(f'select count(*) from "{t}"').fetchone()[0]
                except Exception:  # noqa: BLE001
                    continue
                if n > 0:
                    unmapped.append(f"{alias}.{t} ({n} rows)")
            con.close()
        except Exception:  # noqa: BLE001
            continue
    return {"curated_tables": sum(len(v) for v in curated.values()),
            "unmapped_nonempty": unmapped}


def run_defect_mine(since_hours: float = 24.0, hub_post=None) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    turns: list[dict] = []
    if TURNS_LOG.exists():
        for line in TURNS_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = _parse_ts(str(rec.get("ts") or ""))
            if ts is not None and ts >= cutoff:
                turns.append(rec)

    grades = Counter(str(t.get("grade") or "ungraded") for t in turns)
    reasons = Counter(str(t.get("grade_reason") or "-") for t in turns
                      if t.get("grade") in ("medium", "low"))
    routes = Counter(str(t.get("route") or "-") for t in turns)
    flagged = [t for t in turns if t.get("grade") in ("medium", "low")]

    # exemplar/eval candidates: flagged turns where CK produced a rescue
    new_candidates = 0
    if flagged:
        CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
        with CANDIDATES.open("a", encoding="utf-8") as fh:
            for t in flagged:
                if t.get("ck_answer"):
                    fh.write(json.dumps({
                        "ts": t.get("ts"),
                        "question": t.get("question"),
                        "grade_reason": t.get("grade_reason"),
                        "cena_answer": t.get("cena_answer"),
                        "ck_answer": t.get("ck_answer"),
                        "ck_mode": t.get("ck_mode"),
                        "status": "candidate",
                    }, default=str) + "\n")
                    new_candidates += 1

    coverage = schema_coverage()
    summary = {
        "window_hours": since_hours,
        "turns": len(turns),
        "grades": dict(grades),
        "flag_reasons": dict(reasons),
        "routes": dict(routes),
        "exemplar_candidates_appended": new_candidates,
        "schema_coverage": coverage,
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_path = REPORT_DIR / f"cena_supervisor_daily_{stamp}.md"
    lines = [
        f"# C.E.N.A. supervisor daily mine - {stamp}",
        "",
        f"Turns in window ({since_hours:.0f}h): {len(turns)}",
        f"Grades: {dict(grades)}",
        f"Flag reasons: {dict(reasons)}",
        f"Routes: {dict(routes)}",
        f"Exemplar/eval candidates appended: {new_candidates}",
        "",
        "## Schema coverage (tables with data Cena can NOT see)",
        f"Curated tables: {coverage.get('curated_tables', '?')}",
    ] + [f"- UNMAPPED: {t}" for t in coverage.get("unmapped_nonempty", [])[:60]] + [
        "",
        "## Flagged turns",
    ]
    for t in flagged[:50]:
        lines.append(
            f"- [{t.get('ts')}] ({t.get('grade')}/{t.get('grade_reason')}) "
            f"{str(t.get('question'))[:140]} -> ck_mode={t.get('ck_mode')}"
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary["report"] = str(report_path)

    if hub_post is not None and turns:
        try:
            hub_post(
                f"[cena-mine] last {since_hours:.0f}h: {len(turns)} turns, "
                f"grades {dict(grades)}, {new_candidates} exemplar candidates. "
                f"Report: audit_outputs\\{report_path.name}",
                author="cena-supervisor",
            )
        except Exception:
            pass
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-hours", type=float, default=24.0)
    args = ap.parse_args()
    try:
        import assistant_conversations as conv
        poster = conv.hub_post
    except ImportError:
        poster = None
    print(json.dumps(run_defect_mine(args.since_hours, hub_post=poster), indent=1))
