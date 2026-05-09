"""Produce price history queries — feeds the /produce/orders price tracking
charts and "biggest movers" callout.

Reads `produce_price_snapshot` rows (populated by produce_ingest.py every
time a vendor email is parsed) and returns aggregated, chart-ready data.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from app.db import SessionLocal
from app.models import ProducePriceSnapshot

log = logging.getLogger(__name__)

VENDORS = ("alvarado", "jluna")


def _item_key(canonical_name: str, canonical_size: Optional[str]) -> tuple[str, Optional[str]]:
    return (canonical_name, canonical_size)


def latest_prices() -> dict:
    """For each item × vendor, return the most recent snapshot.

    Useful for the "current week pricing" view + winner detection."""
    db = SessionLocal()
    try:
        rows = (
            db.query(ProducePriceSnapshot)
            .order_by(ProducePriceSnapshot.snapshot_date.desc())
            .all()
        )
    finally:
        db.close()
    # First-seen per (item, vendor) wins (since we sorted desc)
    seen: dict = {}
    for r in rows:
        k = (r.canonical_name, r.canonical_size, r.vendor)
        if k in seen:
            continue
        seen[k] = {
            "snapshot_date": r.snapshot_date,
            "price": r.price,
            "date_range": r.date_range,
        }
    # Re-shape: item → {vendor: latest}
    by_item: dict = defaultdict(dict)
    for (name, size, vendor), v in seen.items():
        by_item[(name, size)][vendor] = v
    out = []
    for (name, size), per_vendor in sorted(by_item.items()):
        prices = {v: per_vendor.get(v) for v in VENDORS}
        # Determine winner among vendors that quoted
        avail = {v: p for v, p in prices.items() if p}
        winner = min(avail, key=lambda v: avail[v]["price"]) if avail else None
        out.append({
            "name": name,
            "size": size,
            "alvarado": prices.get("alvarado"),
            "jluna": prices.get("jluna"),
            "winner": winner,
        })
    # Use 'rows' instead of 'items' — Jinja's dot accessor prefers dict
    # methods like .items() over key lookup, so {{ x.items }} would return
    # the bound method rather than this list.
    return {"rows": out, "total": len(out)}


def history_for_item(canonical_name: str, canonical_size: Optional[str] = None,
                     days: int = 90) -> dict:
    """Per-vendor time series for one item, for charting (Chart.js)."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    db = SessionLocal()
    try:
        q = (
            db.query(ProducePriceSnapshot)
            .filter(ProducePriceSnapshot.canonical_name == canonical_name)
            .filter(ProducePriceSnapshot.snapshot_date >= cutoff)
        )
        if canonical_size is not None:
            q = q.filter(ProducePriceSnapshot.canonical_size == canonical_size)
        rows = q.order_by(ProducePriceSnapshot.snapshot_date).all()
    finally:
        db.close()
    # Build vendor → [{x: date, y: price}, ...]
    series: dict = {v: [] for v in VENDORS}
    all_dates: set = set()
    for r in rows:
        if r.vendor not in series:
            continue
        series[r.vendor].append({"x": r.snapshot_date, "y": r.price})
        all_dates.add(r.snapshot_date)
    return {
        "name": canonical_name,
        "size": canonical_size,
        "vendors": series,
        "snapshot_dates": sorted(all_dates),
    }


def biggest_movers(threshold_pct: float = 5.0, lookback_weeks: int = 8) -> dict:
    """Items where the price moved >= threshold_pct (vendor-on-vendor, week-over-week).

    For each (item, vendor), find the two most recent snapshots; compute
    pct change. Return items sorted by largest absolute move first."""
    db = SessionLocal()
    cutoff = (date.today() - timedelta(weeks=lookback_weeks)).isoformat()
    try:
        rows = (
            db.query(ProducePriceSnapshot)
            .filter(ProducePriceSnapshot.snapshot_date >= cutoff)
            .order_by(ProducePriceSnapshot.snapshot_date.desc())
            .all()
        )
    finally:
        db.close()

    # Group by (vendor, name, size). Take first two distinct dates per group.
    grouped: dict = defaultdict(list)
    for r in rows:
        k = (r.vendor, r.canonical_name, r.canonical_size)
        # Keep only first two distinct snapshot_dates per key (they're
        # already ordered desc by date)
        existing_dates = {x.snapshot_date for x in grouped[k]}
        if r.snapshot_date in existing_dates:
            continue
        if len(grouped[k]) >= 2:
            continue
        grouped[k].append(r)

    movers: list = []
    for (vendor, name, size), pair in grouped.items():
        if len(pair) < 2:
            continue
        new, old = pair[0], pair[1]   # desc order: index 0 newest
        if old.price <= 0:
            continue
        pct = (new.price - old.price) / old.price * 100
        if abs(pct) < threshold_pct:
            continue
        movers.append({
            "vendor": vendor,
            "name": name,
            "size": size,
            "old_date": old.snapshot_date,
            "old_price": old.price,
            "new_date": new.snapshot_date,
            "new_price": new.price,
            "pct_change": pct,
            "direction": "up" if pct > 0 else "down",
        })
    movers.sort(key=lambda m: -abs(m["pct_change"]))
    return {"threshold_pct": threshold_pct, "lookback_weeks": lookback_weeks, "rows": movers}


def list_distinct_items() -> list[dict]:
    """Distinct (canonical_name, canonical_size) pairs in the snapshot table —
    populates the item-selector dropdown on the price chart."""
    db = SessionLocal()
    try:
        rows = (
            db.query(ProducePriceSnapshot.canonical_name,
                     ProducePriceSnapshot.canonical_size)
            .distinct()
            .order_by(ProducePriceSnapshot.canonical_name,
                      ProducePriceSnapshot.canonical_size)
            .all()
        )
    finally:
        db.close()
    return [{"name": r[0], "size": r[1]} for r in rows]


def bootstrap_from_current_jsons(state_dir) -> dict:
    """Read alvarado.json + jluna.json from the produce state dir and seed the
    snapshot table. Per-row commits so a race with the IMAP poller (or another
    gunicorn worker also bootstrapping) doesn't roll back everything — just
    skips the conflicting row."""
    from pathlib import Path
    import json as jsonlib
    from sqlalchemy.exc import IntegrityError

    state_dir = Path(state_dir)
    inserted = 0
    skipped = 0
    db = SessionLocal()
    try:
        for vendor in VENDORS:
            f = state_dir / f"{vendor}.json"
            if not f.exists():
                continue
            payload = jsonlib.loads(f.read_text(encoding="utf-8"))
            from datetime import date as _date
            import re as _re
            today_iso = _date.today().isoformat()
            snapshot_date = today_iso
            dr = (payload.get("date_range") or "").strip()
            if dr:
                m = _re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", dr)
                if m:
                    mo = int(m.group(1)); dy = int(m.group(2))
                    yr_raw = m.group(3)
                    yr = int(yr_raw) if yr_raw else _date.today().year
                    if yr < 100:
                        yr += 2000
                    try:
                        snapshot_date = _date(yr, mo, dy).isoformat()
                    except ValueError:
                        pass
            for it in payload.get("items") or []:
                cn = (it.get("canonical_name") or "").strip()
                cs = (it.get("canonical_size") or "").strip() or None
                price = it.get("price")
                if not cn or price is None:
                    continue
                row = ProducePriceSnapshot(
                    snapshot_date=snapshot_date, vendor=vendor,
                    canonical_name=cn, canonical_size=cs,
                    price=float(price),
                    raw_item_name=(it.get("vendor_name") or it.get("name")),
                    parsed_at=payload.get("parsed_at"),
                    date_range=dr or None,
                )
                db.add(row)
                try:
                    db.commit()
                    inserted += 1
                except IntegrityError:
                    db.rollback()
                    skipped += 1
    finally:
        db.close()
    return {"inserted": inserted, "skipped": skipped}
