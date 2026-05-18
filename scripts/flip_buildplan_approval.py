"""One-shot: revert the Build Plan sample-approval row from REJECTED → PENDING.

Per Sam #2691 + cena #2685 + dck #2683: Sam accidentally clicked Reject on the
Build Plan card; he wants the approval state back to pending, card visible.

Run via:
    python scripts/flip_buildplan_approval.py

Idempotent: prints current state before + after.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datetime import datetime, timezone  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import SampleApproval  # noqa: E402


SLUG = "build-plan"


def main() -> int:
    db = SessionLocal()
    try:
        ap = (db.query(SampleApproval)
                .filter(SampleApproval.sample_slug == SLUG)
                .one_or_none())
        if ap is None:
            print(f"no SampleApproval row for slug={SLUG!r} — nothing to do (already at default pending).")
            return 0
        before = ap.status
        if before == "pending":
            print(f"slug={SLUG!r} already status=pending. no-op.")
            return 0
        ap.status = "pending"
        ap.updated_at = datetime.now(timezone.utc)
        db.commit()
        print(f"slug={SLUG!r}: {before} -> pending. updated_at={ap.updated_at.isoformat()}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
