"""C.E.N.A. Level 3 performance bench.

Two layers matter and are measured separately:

  * executor latency   - run_readonly_sql p50/p95 over a representative query mix
                         (analytics, raw, join, aggregate). This is the fast path.
  * investigation cost - full investigate() wall-clock p50/p95. The multi-query
                         reasoning loop is the REAL latency a manager feels;
                         skipped cleanly with --skip-investigations or when no LLM
                         provider is configured.

Also checks concurrency: N threads each running the executor suite at once (must
all succeed - no worker pinning), and a couple of concurrent investigations when
live.

Usage:
  python scripts/cena_sql_bench.py
  python scripts/cena_sql_bench.py --runs 50 --skip-investigations
  python scripts/cena_sql_bench.py --questions 3 --threads 6
"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("cena_sql_bench")

_EXEC_QUERIES = [
    "SELECT store_key, net_sales FROM daily_sales_summary WHERE business_date='2026-05-06'",
    "SELECT SUM(net_sales), SUM(order_count) FROM daily_sales_summary WHERE net_sales IS NOT NULL",
    "SELECT iso_week, net_sales FROM weekly_rollups WHERE store_key='tomball' ORDER BY iso_week",
    "SELECT metric, direction, z_score FROM anomaly_flags ORDER BY ABS(z_score) DESC LIMIT 10",
    "SELECT item_name, qty FROM item_sales_summary WHERE store_key='copperfield' ORDER BY qty DESC LIMIT 10",
    "SELECT COUNT(*) FROM ordersdc.dm_order WHERE status='imported'",
    "SELECT store_key, ROUND(SUM(total_hours),1) FROM daily_labor_summary GROUP BY 1",
    "SELECT d.name, COUNT(*) c FROM driverdc.dm_delivery dl JOIN driverdc.dm_driver d ON d.driver_id=dl.driver_id GROUP BY 1 ORDER BY c DESC LIMIT 5",
    "SELECT business_date, SUM(qty) FROM item_sales_summary GROUP BY 1 ORDER BY 1 DESC LIMIT 20",
    "SELECT o.store_key, COUNT(*) FROM ordersdc.dm_order o WHERE o.caterer_total_due IS NOT NULL GROUP BY 1",
]

_BENCH_QUESTIONS = [
    "What were Copperfield's net sales in April 2026?",
    "Which store had more catering orders in April 2026?",
    "Why did Tomball's net sales fall from ISO week 2026-W12 to 2026-W13?",
    "How many overtime hours did Tomball work between 2026-05-11 and 2026-05-17?",
]


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[idx]


def bench_executor(runs: int) -> dict:
    from app.services.cena_sql_executor import run_readonly_sql

    lat: list[float] = []
    for _ in range(runs):
        for sql in _EXEC_QUERIES:
            t = time.perf_counter()
            run_readonly_sql(sql)
            lat.append((time.perf_counter() - t) * 1000.0)
    return {"n": len(lat), "p50_ms": round(_pct(lat, 0.5), 2),
            "p95_ms": round(_pct(lat, 0.95), 2),
            "max_ms": round(max(lat), 2) if lat else 0.0}


def bench_concurrency(threads: int, runs: int) -> dict:
    from app.services.cena_sql_executor import run_readonly_sql

    def worker() -> tuple[int, float]:
        errs = 0
        lat: list[float] = []
        for _ in range(runs):
            for sql in _EXEC_QUERIES:
                t = time.perf_counter()
                try:
                    run_readonly_sql(sql)
                except Exception:
                    errs += 1
                lat.append((time.perf_counter() - t) * 1000.0)
        return errs, _pct(lat, 0.5)

    with ThreadPoolExecutor(max_workers=threads) as ex:
        results = list(ex.map(lambda _: worker(), range(threads)))
    total_errs = sum(r[0] for r in results)
    return {"threads": threads, "errors": total_errs,
            "per_thread_p50_ms": [round(r[1], 2) for r in results]}


def bench_investigations(n: int, concurrent: bool = True) -> dict:
    from app.services.cena_reasoner import investigate
    from app.services.cena_llm import CenaLlmError

    qs = _BENCH_QUESTIONS[:n]

    def one(q: str) -> float:
        t = time.perf_counter()
        try:
            investigate(q)
        except CenaLlmError:
            return float("nan")
        return time.perf_counter() - t

    seq = [one(q) for q in qs]
    seq = [s for s in seq if s == s]  # drop NaN
    out = {"n": len(seq),
           "p50_s": round(_pct(seq, 0.5), 1) if seq else None,
           "p95_s": round(_pct(seq, 0.95), 1) if seq else None}
    if concurrent and len(qs) >= 2:
        t = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(one, qs[:2]))
        out["two_concurrent_wall_s"] = round(time.perf_counter() - t, 1)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="C.E.N.A. Level 3 bench")
    p.add_argument("--runs", type=int, default=30)
    p.add_argument("--questions", type=int, default=4)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--skip-investigations", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    print("\n========== C.E.N.A. L3 BENCH ==========")
    ex = bench_executor(args.runs)
    print(f"executor    : p50 {ex['p50_ms']}ms  p95 {ex['p95_ms']}ms  max {ex['max_ms']}ms  "
          f"(n={ex['n']})")
    cc = bench_concurrency(args.threads, max(2, args.runs // 6))
    print(f"concurrency : {cc['threads']} threads, {cc['errors']} errors, "
          f"per-thread p50 {cc['per_thread_p50_ms']}ms")
    if not args.skip_investigations:
        inv = bench_investigations(args.questions)
        if inv["n"]:
            extra = (f", 2-concurrent wall {inv['two_concurrent_wall_s']}s"
                     if "two_concurrent_wall_s" in inv else "")
            print(f"investigate : p50 {inv['p50_s']}s  p95 {inv['p95_s']}s  "
                  f"(n={inv['n']}){extra}")
        else:
            print("investigate : skipped (no LLM provider available)")
    print("=======================================\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
