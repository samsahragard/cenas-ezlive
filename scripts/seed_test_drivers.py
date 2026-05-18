"""Seed 10 driver test accounts for Sam's full-flow test (cena #2685 + #2687).

One-shot seed script. Out of the production path. Run via:
    python scripts/seed_test_drivers.py

Behavior:
- Creates 10 drivers named "Test Driver 01" ... "Test Driver 10".
- Phones: 713-555-0101 ... 713-555-0110 (reserved fictional block).
- 5-digit PINs: random, distinct, scrypt-hashed via werkzeug.
- Store split: 5 tomball (DOS) + 5 copperfield (UNO).
- Tier mix: 3 new / 3 trusted / 2 rockstar / 2 top_rockstar.
- Idempotent on phone: if a Test Driver row already exists, prints
  "(exists)" and reuses the existing PIN-hash position rather than
  re-seeding. (Hash is one-way — we can't recover the PIN. Re-runs
  rotate the PIN and print the new one.)
- Output: clean markdown-style table to stdout for Sam to paste/use.

Cleanup discipline (cena #2685 step 8): after Sam's done, soft-delete
via `UPDATE drivers SET active=false WHERE name LIKE 'Test Driver %'`
rather than hard-delete, so audit + any test driver_requests survive.
"""
from __future__ import annotations

import random
import sys
from datetime import date, datetime
from pathlib import Path

# Allow run from repo root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from werkzeug.security import generate_password_hash  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Driver  # noqa: E402


# (name_suffix, phone, location, home_store_id, tier)
SEED_PLAN: list[tuple[int, str, str, str, str]] = [
    (1,  "713-555-0101", "tomball",     "dos", "new"),
    (2,  "713-555-0102", "tomball",     "dos", "trusted"),
    (3,  "713-555-0103", "tomball",     "dos", "trusted"),
    (4,  "713-555-0104", "tomball",     "dos", "rockstar"),
    (5,  "713-555-0105", "tomball",     "dos", "top_rockstar"),
    (6,  "713-555-0106", "copperfield", "uno", "new"),
    (7,  "713-555-0107", "copperfield", "uno", "new"),
    (8,  "713-555-0108", "copperfield", "uno", "trusted"),
    (9,  "713-555-0109", "copperfield", "uno", "rockstar"),
    (10, "713-555-0110", "copperfield", "uno", "top_rockstar"),
]


def random_pin(seen: set[str]) -> str:
    while True:
        p = f"{random.randint(0, 99999):05d}"
        if p not in seen and p != "00000" and p != "12345" and p != "11111":
            seen.add(p)
            return p


def main() -> int:
    db = SessionLocal()
    rows: list[dict] = []
    seen_pins: set[str] = set()
    try:
        today = date.today()
        for suffix, phone, location, home_store, tier in SEED_PLAN:
            name = f"Test Driver {suffix:02d}"
            existing = (
                db.query(Driver)
                .filter(Driver.name == name)
                .filter(Driver.location == location)
                .one_or_none()
            )
            pin = random_pin(seen_pins)
            pwd = generate_password_hash(pin)
            if existing is not None:
                # rotate the PIN (only stored as hash — can't recover the old one)
                existing.phone = phone
                existing.passcode_hash = pwd
                existing.first_login_done = False
                existing.active = True
                existing.failed_attempts = 0
                existing.lockout_until = None
                existing.status = "active"
                existing.current_tier = tier
                existing.home_store_id = home_store
                if existing.joined_at is None:
                    existing.joined_at = today
                action = "rotated"
                d = existing
            else:
                d = Driver(
                    name=name,
                    location=location,
                    phone=phone,
                    email=None,
                    passcode_hash=pwd,
                    first_login_done=False,
                    active=True,
                    failed_attempts=0,
                    lockout_until=None,
                    status="active",
                    joined_at=today,
                    lifetime_delivery_count=0,
                    current_score=None,
                    current_tier=tier,
                    home_store_id=home_store,
                )
                db.add(d)
                db.flush()
                action = "created"
            rows.append({
                "n": suffix,
                "name": name,
                "phone": phone,
                "pin": pin,
                "store": location,
                "tier": tier,
                "action": action,
                "id": d.id,
            })
        db.commit()
    finally:
        db.close()

    # Output: clean markdown table for Sam
    print()
    print("| #  | Name            | Phone         | PIN   | Store       | Tier         | id  |")
    print("|----|-----------------|---------------|-------|-------------|--------------|-----|")
    for r in rows:
        print(
            f"| {r['n']:02d} | {r['name']:<15} | {r['phone']} | {r['pin']} | "
            f"{r['store']:<11} | {r['tier']:<12} | {r['id']:<3} |"
        )
    print()
    created = sum(1 for r in rows if r["action"] == "created")
    rotated = sum(1 for r in rows if r["action"] == "rotated")
    print(f"summary: {created} created, {rotated} rotated. total {len(rows)} test drivers ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
