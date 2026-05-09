"""Schedule report from Sling data.

Given a date range + optional location filter, returns a structured dict
the template can render: per-day shift listings, by-position rollup,
totals.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from app.services.sling_client import SlingClient

log = logging.getLogger(__name__)

# Sling location id → display key + label (matches the existing Tomball/Copperfield convention)
LOCATION_MAP = {
    9679304:  ("tomball", "Tomball"),
    15986138: ("copperfield", "Copperfield"),
}

# Reverse: 'tomball'|'copperfield' → sling group id
LOCATION_KEY_TO_ID = {v[0]: k for k, v in LOCATION_MAP.items()}


def _parse_iso(s: str | None):
    if not s:
        return None
    # Sling returns "2026-05-14T16:00:00-05:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def schedule_report(start: datetime, end: datetime,
                    location_filter: Optional[str] = None,
                    refresh: bool = False) -> dict:
    """Compute schedule report for [start, end) inclusive.

    `location_filter` is one of 'both', 'tomball', 'copperfield', or None (= both).
    """
    client = SlingClient.shared()
    groups = client.fetch_groups(refresh=refresh)
    users = client.fetch_users(refresh=refresh)
    calendar = client.fetch_calendar(start, end + timedelta(days=1), refresh=refresh)

    # Lookups
    user_lookup = {}
    for u in users:
        full = " ".join(filter(None, [u.get("name"), u.get("lastname")])).strip() \
               or u.get("legalName") or u.get("email") or f"id-{u.get('id')}"
        user_lookup[u["id"]] = full

    position_lookup = {g["id"]: (g.get("name") or "(no position)").strip()
                       for g in groups if g.get("type") == "position"}
    location_lookup = {g["id"]: (g.get("name") or "?").strip()
                       for g in groups if g.get("type") == "location"}

    # Filter calendar entries
    if location_filter and location_filter != "both":
        wanted_loc_id = LOCATION_KEY_TO_ID.get(location_filter)
        if wanted_loc_id is None:
            raise ValueError(f"unknown location filter {location_filter!r}; "
                             f"expected one of: tomball, copperfield, both")
        wanted_loc_ids = {wanted_loc_id}
    else:
        wanted_loc_ids = set(LOCATION_MAP.keys())

    # Group by date
    rows_by_date: dict = defaultdict(list)
    by_position: dict = defaultdict(lambda: {"shifts": 0, "hours": 0.0, "people": set()})
    by_location: dict = defaultdict(lambda: {"shifts": 0, "hours": 0.0})
    open_shifts: list = []  # shifts where user is None

    for entry in calendar:
        if entry.get("type") != "shift":
            continue
        loc_id = (entry.get("location") or {}).get("id")
        if loc_id not in wanted_loc_ids:
            continue
        # Range filter (inclusive on start)
        in_dt = _parse_iso(entry.get("dtstart"))
        out_dt = _parse_iso(entry.get("dtend"))
        if not in_dt:
            continue
        if in_dt.date() < start.date() or in_dt.date() > end.date():
            continue

        user_id = (entry.get("user") or {}).get("id")
        position_id = (entry.get("position") or {}).get("id")
        name = user_lookup.get(user_id) if user_id else None
        position = position_lookup.get(position_id, "(no position)")
        loc_name = location_lookup.get(loc_id, "?")
        # Friendly location key
        loc_key, loc_label = LOCATION_MAP.get(loc_id, (None, loc_name))

        hours = ((out_dt - in_dt).total_seconds() / 3600.0) if (in_dt and out_dt) else 0.0
        # Subtract break duration (Sling stores in minutes)
        break_min = entry.get("breakDuration") or 0
        hours = max(0.0, hours - (break_min / 60.0))

        row = {
            "id": entry.get("id"),
            "status": entry.get("status"),
            "in_dt": in_dt,
            "out_dt": out_dt,
            "user_id": user_id,
            "name": name,
            "position": position,
            "location_key": loc_key,
            "location_label": loc_label,
            "hours": hours,
            "break_minutes": break_min,
            "is_open": user_id is None,
        }
        if user_id is None:
            slots = entry.get("slots") or 1
            open_shifts.append({**row, "slots": slots})
            continue
        rows_by_date[in_dt.date()].append(row)
        by_position[position]["shifts"] += 1
        by_position[position]["hours"] += hours
        if name:
            by_position[position]["people"].add(name)
        if loc_key:
            by_location[loc_key]["shifts"] += 1
            by_location[loc_key]["hours"] += hours

    # Render-friendly shape
    days = []
    for day in sorted(rows_by_date.keys()):
        shifts_on_day = sorted(rows_by_date[day], key=lambda r: (r["in_dt"], r["position"], r["name"] or ""))
        days.append({
            "date": day.isoformat(),
            "weekday": day.strftime("%A"),
            "label": day.strftime("%a, %b %d"),
            "shifts": shifts_on_day,
            "shift_count": len(shifts_on_day),
            "hours_total": sum(s["hours"] for s in shifts_on_day),
        })

    by_position_sorted = []
    for title, s in sorted(by_position.items(), key=lambda kv: -kv[1]["hours"]):
        by_position_sorted.append({
            "title": title,
            "shifts": s["shifts"],
            "hours": s["hours"],
            "people_count": len(s["people"]),
        })

    by_location_out = {}
    for key, data in by_location.items():
        _, label = LOCATION_MAP.get(LOCATION_KEY_TO_ID.get(key, 0), (key, key))
        by_location_out[key] = {"label": label, **data}

    return {
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "location_filter": location_filter or "both",
        "days": days,
        "by_position": by_position_sorted,
        "by_location": by_location_out,
        "totals": {
            "shifts": sum(d["shift_count"] for d in days),
            "hours": sum(d["hours_total"] for d in days),
            "open_shifts": len(open_shifts),
        },
        "open_shifts": sorted(open_shifts, key=lambda r: r["in_dt"]),
    }


# ============== ROSTER ==============

def roster_report(location_filter: Optional[str] = None,
                  position_filter: Optional[str] = None,
                  include_inactive: bool = False,
                  refresh: bool = False) -> dict:
    """Compute a per-location employee roster with positions held.

    location_filter: 'both' (default), 'tomball', 'copperfield'.
    position_filter: position name string, or None for all.
    include_inactive: by default only active employees are shown.
    """
    client = SlingClient.shared()
    groups = client.fetch_groups(refresh=refresh)

    # Build reverse index: user_id -> set of position-names they're in.
    position_groups = [g for g in groups
                       if g.get("type") == "position" and not g.get("archivedAt")]
    user_to_positions: dict[int, set[str]] = {}
    for pg in position_groups:
        members = client.fetch_group_members(pg["id"], refresh=refresh)
        title = (pg.get("name") or "?").strip()
        for m in members:
            user_to_positions.setdefault(m["id"], set()).add(title)

    # Active position names list (for the dropdown)
    available_positions = sorted({(pg.get("name") or "?").strip() for pg in position_groups})

    # Pick locations
    if location_filter and location_filter != "both":
        wanted = {location_filter}
    else:
        wanted = {"tomball", "copperfield"}

    by_location_out: dict = {}
    total_shown = 0
    total_active = 0
    for loc_key, loc_id in LOCATION_KEY_TO_ID.items():
        if loc_key not in wanted:
            continue
        loc_label = LOCATION_MAP[loc_id][1]
        members = client.fetch_group_members(loc_id, refresh=refresh)
        rows = []
        for u in members:
            uid = u["id"]
            is_active = bool(u.get("active"))
            if not include_inactive and not is_active:
                continue
            positions = sorted(user_to_positions.get(uid, set()))
            # Apply position filter if any
            if position_filter and position_filter != "all":
                if position_filter not in positions:
                    continue
            full = " ".join(filter(None, [u.get("name"), u.get("lastname")])).strip() \
                   or u.get("legalName") or u.get("email") or f"id-{uid}"
            email = u.get("email") or ""
            rows.append({
                "id": uid,
                "name": full,
                "positions": positions,
                "email": email,
                "active": is_active,
                "has_toast_guid": bool(u.get("hasToastGuid")),
            })
            if is_active:
                total_active += 1
        rows.sort(key=lambda r: (not r["active"], (r["name"].split()[-1] if r["name"] else "").lower(), r["name"].lower()))
        total_shown += len(rows)
        by_location_out[loc_key] = {
            "label": loc_label,
            "people": rows,
            "count": len(rows),
            "active_count": sum(1 for r in rows if r["active"]),
        }

    return {
        "location_filter": location_filter or "both",
        "position_filter": position_filter or "all",
        "include_inactive": include_inactive,
        "available_positions": available_positions,
        "by_location": by_location_out,
        "totals": {
            "shown": total_shown,
            "active": total_active,
        },
    }
