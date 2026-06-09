"""Cenas Floor Pulse V2 - the employee performance data layer.

This is the V2 behavior layer on top of the pure calculators in
``employee_floor_metrics``. It owns the pieces the first Floor OS ship was
missing:

- **Date-true Today.** Today is keyed to the real local business date. If there
  are no checks for *today*, every Today number is 0 and the peer leaderboard is
  empty. Yesterday never rolls forward into Today.
- **Ranges.** today / week / month / last30. Week/Month/Last30 are stats-only
  (no table map, no ticket rail) - they show averages, technical/ops stats, and
  peer rankings.
- **Peer ranking.** Tipped employees only, ranked by a composite of normalized
  tips-per-hour and tip-percent. Unknown cash tips stay neutral (never counted
  as zero-performance). No divide-by-zero.
- **V2 ticket model** for the Tables tab (owner / status / tone / next action).

Everything here is pure (no Flask / DB / Toast imports) so it is unit-testable
and the same shapes flow whether the source is this demo fixture or a future
mapped Toast payload.

DEMO NOTE: the numbers below are a labelled demo fixture. Ratios (tip/hr, tip%)
are scale-invariant, so a peer's ranking is identical across week/month/last30 -
that is correct, not a bug: looking at a longer window does not change someone's
tips-per-hour. The UI always labels this "Demo mode" until Toast is wired.
"""
from __future__ import annotations

from datetime import date, timedelta

CURRENT_EMPLOYEE_ID = "kennya-garcia"
OPEN_TIP_ESTIMATE_RATE = 0.18
BASE_RATE = 2.13  # tipped-server base wage; demo only

RANGE_KEYS = ("today", "week", "month", "last30")
RANGE_LABELS = {
    "today": "Today",
    "week": "This week",
    "month": "This month",
    "last30": "Last 30 days",
}


# --- helpers ---------------------------------------------------------------

def _safe_div(a, b):
    b = float(b or 0)
    return float(a or 0) / b if b else 0.0


def _average(values):
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else 0.0


def local_business_date(today: date | None = None) -> str:
    """The real local business date as an ISO string. The caller passes
    `date.today()`; we keep the function pure (no implicit clock) so it stays
    deterministic in tests."""
    d = today or date.today()
    return d.isoformat()


# --- demo peer fixture -----------------------------------------------------
# Daily base rows for one representative shift. Kennya Garcia is the signed-in
# employee. cash_unknown marks a check whose cash tip is unknown -- it is shown
# neutrally and never dragged to zero.
_PEER_BASE = [
    {"id": "melissa-aguilera", "name": "Melissa D Almaguer Aguilera", "hours": 4.10, "tickets": 8,  "sales": 623.14,  "tips": 124.62, "avg_drink": 12.65, "avg_apps": 4.42,  "avg_entree": 18.32, "gap": 8.12,  "duration": 64, "cash_unknown": 1},
    {"id": "geidis-alarcon",   "name": "Geidis Dailen Alarcon",       "hours": 5.50, "tickets": 11, "sales": 532.42,  "tips": 96.44,  "avg_drink": 16.88, "avg_apps": None,  "avg_entree": 4.10,  "gap": None,  "duration": 45, "cash_unknown": 0},
    {"id": "melanie-diaz",     "name": "Melanie Diaz",                 "hours": 7.00, "tickets": 28, "sales": 1190.40, "tips": 218.44, "avg_drink": 3.05,  "avg_apps": 0.77,  "avg_entree": 4.82,  "gap": 1.47,  "duration": 48, "cash_unknown": 0},
    {"id": "valentina-diaz",   "name": "Valentina Arrazola Diaz",      "hours": 4.90, "tickets": 16, "sales": 533.76,  "tips": 111.14, "avg_drink": 6.24,  "avg_apps": 5.20,  "avg_entree": 11.03, "gap": 4.22,  "duration": 52, "cash_unknown": 0},
    {"id": CURRENT_EMPLOYEE_ID, "name": "Kennya Garcia",              "hours": 6.35, "tickets": 38, "sales": 1848.13, "tips": 332.62, "avg_drink": 2.07,  "avg_apps": 10.25, "avg_entree": 15.02, "gap": 12.13, "duration": 65, "cash_unknown": 0},
    {"id": "kristal-garcia",   "name": "Kristal Castillo Garcia",      "hours": 7.50, "tickets": 44, "sales": 1769.22, "tips": 348.86, "avg_drink": 1.63,  "avg_apps": 3.17,  "avg_entree": 19.63, "gap": 8.12,  "duration": 88, "cash_unknown": 0},
    {"id": "meher-hayr",       "name": "Meher Hayr",                   "hours": 1.10, "tickets": 1,  "sales": 35.68,   "tips": 0.0,    "avg_drink": None,  "avg_apps": 1.52,  "avg_entree": 1.52,  "gap": None,  "duration": 18, "cash_unknown": 1},
    {"id": "yadira-hernandez", "name": "Yadira Romer Hernandez",       "hours": 6.75, "tickets": 24, "sales": 1340.50, "tips": 292.70, "avg_drink": 3.75,  "avg_apps": 7.25,  "avg_entree": 12.70, "gap": 6.70,  "duration": 58, "cash_unknown": 0},
    {"id": "pavel-lira",       "name": "Pavel Lira",                   "hours": 5.10, "tickets": 22, "sales": 982.44,  "tips": 146.36, "avg_drink": 4.20,  "avg_apps": 6.18,  "avg_entree": 14.10, "gap": 7.40,  "duration": 53, "cash_unknown": 0},
    {"id": "jaslyn-pamith",    "name": "Jaslyn Pamith",                "hours": 5.70, "tickets": 19, "sales": 825.80,  "tips": 157.12, "avg_drink": 5.40,  "avg_apps": 4.42,  "avg_entree": 13.20, "gap": 8.30,  "duration": 59, "cash_unknown": 0},
    {"id": "alexa-rodriguez",  "name": "Alexa Rodriguez",              "hours": 6.20, "tickets": 31, "sales": 1456.40, "tips": 302.21, "avg_drink": 3.80,  "avg_apps": 5.10,  "avg_entree": 11.90, "gap": 5.80,  "duration": 51, "cash_unknown": 0},
    {"id": "jessica-sanchez",  "name": "Jessica Sanchez",              "hours": 5.80, "tickets": 27, "sales": 1222.90, "tips": 248.70, "avg_drink": 3.40,  "avg_apps": 4.30,  "avg_entree": 10.20, "gap": 4.90,  "duration": 47, "cash_unknown": 0},
]


def _decorate(row: dict) -> dict:
    """Attach derived ratio fields (scale-invariant) to a peer row."""
    out = dict(row)
    out["tip_pct"] = _safe_div(row["tips"], row["sales"]) * 100.0
    out["tip_per_hour"] = _safe_div(row["tips"], row["hours"])
    return out


def peer_rows_for_range(range_key: str) -> list[dict]:
    """Peer rows for the selected range.

    today  -> [] (no checks have posted for the real local date; this is the
              date-reset behavior - Today is zero and unranked).
    others -> the full peer fixture (decorated with tip%/tip-per-hour).
    """
    if range_key == "today":
        return []
    return [_decorate(r) for r in _PEER_BASE]


# --- aggregation + employee stats ------------------------------------------

def aggregate_rows(rows: list[dict]) -> dict:
    """Sum a set of peer rows into one stat block. Ratios use safe division."""
    hours = sum(r.get("hours") or 0 for r in rows)
    tickets = sum(r.get("tickets") or 0 for r in rows)
    sales = sum(r.get("sales") or 0 for r in rows)
    tips = sum(r.get("tips") or 0 for r in rows)
    cash_unknown = sum(r.get("cash_unknown") or 0 for r in rows)
    base_pay = hours * BASE_RATE
    return {
        "hours": round(hours, 2),
        "tickets": tickets,
        "sales": round(sales, 2),
        "tips": round(tips, 2),
        "base_pay": round(base_pay, 2),
        "take_home": round(tips + base_pay, 2),
        "cash_unknown": cash_unknown,
        "tip_pct": _safe_div(tips, sales) * 100.0,
        "tip_per_hour": _safe_div(tips, hours),
        "avg_check": _safe_div(sales, tickets),
        "avg_drink": _average([r.get("avg_drink") for r in rows]),
        "avg_apps": _average([r.get("avg_apps") for r in rows]),
        "avg_entree": _average([r.get("avg_entree") for r in rows]),
        "gap": _average([r.get("gap") for r in rows]),
        "duration": _average([r.get("duration") for r in rows]),
    }


def _zero_stats() -> dict:
    return {
        "hours": 0.0, "tickets": 0, "sales": 0.0, "tips": 0.0,
        "base_pay": 0.0, "take_home": 0.0, "cash_unknown": 0,
        "tip_pct": 0.0, "tip_per_hour": 0.0, "avg_check": 0.0,
        "avg_drink": 0.0, "avg_apps": 0.0, "avg_entree": 0.0,
        "gap": 0.0, "duration": 0.0,
    }


def employee_stats(range_key: str, employee_id: str = CURRENT_EMPLOYEE_ID) -> dict:
    """The signed-in employee's stats for a range.

    today -> all zero plus today's live ticket overlay (open-check estimate,
             owned tables, attention counts) computed from the V2 ticket list,
             which is empty today (date reset) so those overlays are zero too.
    """
    rows = [r for r in peer_rows_for_range(range_key) if r["id"] == employee_id]
    base = aggregate_rows(rows) if rows else _zero_stats()

    if range_key == "today":
        tickets = today_tickets()  # empty on a real "no checks today"
    else:
        tickets = []

    open_tickets = [t for t in tickets if t["status"] in ("open", "attention")]
    settled = sum(t.get("tip") or 0 for t in tickets if t["status"] == "closed")
    open_estimate = sum((t["amount"] or 0) * OPEN_TIP_ESTIMATE_RATE for t in open_tickets)

    base.update({
        "settled_ticket_tips": round(settled, 2),
        "open_tip_estimate": round(open_estimate, 2),
        "projected_take_home": round(base["take_home"] + open_estimate, 2),
        "open_tickets": len(open_tickets),
        "owned_tables": sum(1 for t in tickets if t.get("owner")),
        "attention": sum(1 for t in tickets if t["status"] == "attention"),
        "new_seated": sum(1 for t in tickets if (t.get("opened_mins") or 999) <= 20),
        "total_tables": len(tickets),
        "next_move": next((t for t in tickets if t["status"] == "attention"),
                          open_tickets[0] if open_tickets else None),
    })
    return base


# --- peer leaderboard ------------------------------------------------------

def _is_cash_pending(row: dict) -> bool:
    """A server whose ONLY tip signal is unknown cash: zero recorded (card)
    tips but a cash-unknown check on the shift. Ranking such a row on a
    recorded-tips-only composite would score it 0 and push it to last place -
    i.e. punish the unknown cash, which is exactly what we must NOT do. These
    rows are held out of the ranked cohort and surfaced as "cash tips pending"
    instead (neutral, never last)."""
    return (row.get("tips") or 0) == 0 and (row.get("cash_unknown") or 0) > 0


def build_leaderboard(range_key: str, employee_id: str = CURRENT_EMPLOYEE_ID) -> list[dict]:
    """Tipped employees ranked by a composite of normalized tips-per-hour and
    tip-percent (50/50). Returns [] for the today range (no checks posted yet).

    - Only tipped employees with hours and (tips or sales) are ranked.
    - Unknown cash tips are NOT punished: a server whose only tip is unknown
      cash (zero recorded tips) is held OUT of the ranked cohort (see
      cash_pending_peers) rather than scored 0 and ranked last. A genuine
      zero-tipper (no tips AND no unknown cash) still ranks honestly.
    - No divide-by-zero: the normalizers are floored at 1.
    - Deterministic order: ties break on recorded tips, then id, so a peer's
      "#N of M" never wobbles between refreshes.
    """
    rows = peer_rows_for_range(range_key)
    tipped = [
        r for r in rows
        if (r.get("hours") or 0) > 0
        and ((r.get("tips") or 0) > 0 or (r.get("sales") or 0) > 0)
        and not _is_cash_pending(r)
    ]
    if not tipped:
        return []

    max_tip_hr = max([r["tip_per_hour"] for r in tipped] + [1.0])
    max_tip_pct = max([r["tip_pct"] for r in tipped] + [1.0])

    scored = []
    for r in tipped:
        score = _safe_div(r["tip_per_hour"], max_tip_hr) * 50.0 + _safe_div(r["tip_pct"], max_tip_pct) * 50.0
        scored.append({**r, "score": score})

    scored.sort(key=lambda r: (-r["score"], -(r.get("tips") or 0), r["id"]))
    total = len(scored)
    out = []
    for i, r in enumerate(scored):
        out.append({**r, "rank": i + 1, "of": total, "is_me": r["id"] == employee_id})
    return out


def cash_pending_peers(range_key: str) -> list[dict]:
    """Servers held out of the ranked cohort because their only tip signal is
    unknown cash. Shown as a neutral "cash tips pending" note, not ranked."""
    return [r for r in peer_rows_for_range(range_key)
            if (r.get("hours") or 0) > 0 and _is_cash_pending(r)]


def my_rank(leaderboard: list[dict]):
    """The signed-in employee's row from a leaderboard, or None."""
    return next((r for r in leaderboard if r.get("is_me")), None)


# --- technical / ops-style rows --------------------------------------------

def technical_rows(stats: dict) -> list[tuple[str, str]]:
    """Operations-style technical averages for the Week/Month/Last30 views."""
    def m(v):
        return "--" if v in (None, 0) else f"{round(v)}m"

    return [
        ("Tickets", str(stats.get("tickets") or 0)),
        ("Avg drink", m(stats.get("avg_drink"))),
        ("Avg apps", m(stats.get("avg_apps"))),
        ("Avg entree", m(stats.get("avg_entree"))),
        ("Drink-entree gap", m(stats.get("gap"))),
        ("Avg duration", m(stats.get("duration"))),
        ("CC tabs", f"${stats.get('sales', 0):,.2f}"),
        ("CC tips", f"${stats.get('tips', 0):,.2f}"),
        ("Tip %", f"{stats.get('tip_pct', 0):.1f}%"),
    ]


# --- V2 ticket model (Tables tab) ------------------------------------------
# Yesterday carries the full ticket rail; today is empty (date reset).
def _v2_tickets() -> list[dict]:
    return [
        {"table_id": "62B", "ticket_id": "113", "owner": True,  "status": "open",      "tone": "warn",   "covers": 6, "amount": 228.13, "opened_mins": 52, "seated": "7:32p", "drinks": "7:38p", "kitchen": "7:55p", "closed": None,   "tip": None,  "payment": None,        "next_action": "Pre-bus, refill, offer dessert before check drop.", "items": ["2 Laredo Combo", "2 Pollo Con Mango", "Cena Rita", "Watermelon Ranchwater", "Pico de Gallo"]},
        {"table_id": "61B", "ticket_id": "110", "owner": True,  "status": "open",      "tone": "warn",   "covers": 2, "amount": 39.45,  "opened_mins": 18, "seated": "8:18p", "drinks": "8:23p", "kitchen": "8:34p", "closed": None,   "tip": None,  "payment": None,        "next_action": "Food fired. Check on drinks and keep the table warm.", "items": ["2 Monday Rita", "Chicken Fajita Quesadillas", "Tortilla Soup", "Guacamole"]},
        {"table_id": "A1",  "ticket_id": "117", "owner": True,  "status": "closed",    "tone": "good",   "covers": 3, "amount": 70.30,  "opened_mins": 58, "seated": "7:48p", "drinks": "7:54p", "kitchen": "8:05p", "closed": "8:46p", "tip": 14.06, "payment": "Mastercard", "next_action": "Closed strong at 20%.", "items": ["Beef Fajita Enchiladas", "Tacos Al Carbon", "Queso Dip", "Beans & Rice", "Coffee"]},
        {"table_id": "63",  "ticket_id": "119", "owner": False, "status": "closed",    "tone": "good",   "covers": 4, "amount": 92.78,  "opened_mins": 81, "seated": "6:40p", "drinks": "6:45p", "kitchen": "6:57p", "closed": "8:01p", "tip": 20.41, "payment": "Visa",       "next_action": "Closed at 22% with dessert attached.", "items": ["Mix Fajitas For Two", "Fish El Rey", "Birthday Churros", "2 Monday Rita"]},
        {"table_id": "32",  "ticket_id": "133", "owner": True,  "status": "attention", "tone": "danger", "covers": 2, "amount": 58.00,  "opened_mins": 25, "seated": "8:05p", "drinks": "8:10p", "kitchen": "8:19p", "closed": None,   "tip": None,  "payment": None,        "next_action": "Needs bussing before entrees land.", "items": ["House Rita", "Tex-Mex Tacos", "Tortilla Soup", "Sauces"]},
        {"table_id": "23",  "ticket_id": "123", "owner": True,  "status": "closed",    "tone": "good",   "covers": 2, "amount": 43.67,  "opened_mins": 48, "seated": "1:05p", "drinks": "1:09p", "kitchen": "1:18p", "closed": "1:58p", "tip": 7.86,  "payment": "Mastercard", "next_action": "Closed. Good pace.", "items": ["2 Ice Tea", "Tex-Mex Tacos", "Tortilla Soup", "Sauces"]},
        {"table_id": "51",  "ticket_id": "087", "owner": True,  "status": "closed",    "tone": "good",   "covers": 3, "amount": 104.14, "opened_mins": 81, "seated": "6:10p", "drinks": "6:15p", "kitchen": "6:26p", "closed": "7:31p", "tip": 22.91, "payment": "Visa",       "next_action": "Closed at 22%.", "items": ["3 Cena Rita", "Mix Fajitas For Two", "Grilled Jalapenos"]},
    ]


def today_tickets() -> list[dict]:
    """Tickets for the real local date. Empty: today has no posted checks yet
    (date reset). This drives Today's zero state and the Tables "Today" empty
    state."""
    return []


def yesterday_tickets() -> list[dict]:
    """Yesterday's completed ticket rail (the demo's sample detail)."""
    return _v2_tickets()


def tickets_for_day(day_key: str) -> list[dict]:
    return yesterday_tickets() if day_key == "yesterday" else today_tickets()


def filter_tickets(tickets: list[dict], flt: str) -> list[dict]:
    if flt == "mine":
        return [t for t in tickets if t.get("owner")]
    if flt == "open":
        return [t for t in tickets if t["status"] in ("open", "attention")]
    if flt == "attention":
        return [t for t in tickets if t["status"] == "attention"]
    if flt == "new":
        return [t for t in tickets if (t.get("opened_mins") or 999) <= 20]
    return list(tickets)


def filter_counts(tickets: list[dict]) -> dict:
    return {
        "all": len(tickets),
        "mine": sum(1 for t in tickets if t.get("owner")),
        "open": sum(1 for t in tickets if t["status"] != "closed"),
        "attention": sum(1 for t in tickets if t["status"] == "attention"),
        "new": sum(1 for t in tickets if (t.get("opened_mins") or 999) <= 20),
    }


def ticket_view(t: dict) -> dict:
    """Presentation extras for a ticket card: status label + est/actual tip."""
    status_label = {"attention": "Needs attention", "open": "Open"}.get(t["status"], "Closed")
    if t["status"] == "closed":
        est_tip = t.get("tip")
        tip_pct = (_safe_div(t["tip"], t["amount"]) * 100.0) if t.get("tip") else None
    else:
        est_tip = (t["amount"] or 0) * OPEN_TIP_ESTIMATE_RATE
        tip_pct = None
    return {**t, "status_label": status_label, "est_tip": est_tip, "tip_pct": tip_pct}
