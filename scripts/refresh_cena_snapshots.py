"""CLI wrapper for the C.E.N.A. Level 3 snapshot refresh.

Usage:
    python scripts/refresh_cena_snapshots.py                 # refresh all snapshots
    python scripts/refresh_cena_snapshots.py --status        # report only, no copy
    python scripts/refresh_cena_snapshots.py --data-dir D:\\x # override CENA_L3_DATA_DIR

Copies each configured source DB (contract section 1 env map) into
%CENA_L3_DATA_DIR%\\snapshots\\{alias}.sqlite via the sqlite3 backup API and
then attempts the analytics build. Missing/locked sources are recorded in the
printed JSON and snapshot_meta.json - never fatal. Exit code 0 always unless
the refresh itself blows up unexpectedly.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Allow running directly from the repo: python scripts/refresh_cena_snapshots.py
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.cena_sql_executor import refresh_snapshots, snapshot_status  # noqa: E402

logger = logging.getLogger("refresh_cena_snapshots")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh C.E.N.A. Level 3 SQLite snapshots (read-only source copies)."
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Override CENA_L3_DATA_DIR (snapshots land in <data-dir>\\snapshots).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print snapshot status (paths, ages, analytics presence) without copying.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    if args.status:
        result = snapshot_status(data_dir=args.data_dir)
    else:
        result = refresh_snapshots(data_dir=args.data_dir)
        ok = sum(1 for s in result["sources"].values() if s.get("ok"))
        logger.info(
            "refresh complete: %d/%d sources copied, analytics %s",
            ok,
            len(result["sources"]),
            "ok" if result.get("analytics", {}).get("ok") else "unavailable",
        )

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
