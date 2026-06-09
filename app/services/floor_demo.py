"""Cenas Floor OS demo data fixture.

A frozen, hand-built dataset that mirrors the design-reference prototype
(`CenasFloorCommand.jsx`). The employee console falls back to this fixture when
Toast is not connected, so the Floor OS visual system has rich, realistic
content to render against on a developer machine or a test client.

Every consumer surface (Today/Tables/Shifts/Inbox/You) reads from these helpers
through the same `EmployeeFloor*` dataclasses defined in
`app.services.employee_floor_metrics`. Anything wired to Toast in production
flows through the same shape so the templates do not branch.

Demo mode MUST be labelled honestly: when this module supplies the data, the
template should render the "Demo mode" badge instead of "live".
"""
from __future__ import annotations

from app.services.employee_floor_metrics import (
    EmployeeFloorCheck,
    EmployeeFloorEmployee,
    EmployeeFloorItem,
    EmployeeFloorLocation,
    EmployeeFloorShift,
    EmployeeFloorTable,
)

PENDING_TIP_RATE = 0.18


def _t(hours: int, minutes: int = 0) -> int:
    """Time-of-day expressed as minutes since midnight (matches the JS prototype)."""
    return hours * 60 + minutes


DEMO_EMPLOYEE = EmployeeFloorEmployee(
    id=None,
    name="Kennya Garcia",
    role="Server",
    location="Copperfield",
)

DEMO_LOCATION = EmployeeFloorLocation(key="copperfield", label="Copperfield")

DEMO_TODAY_SHIFT = EmployeeFloorShift(
    starts_at=_t(16, 30),
    ends_at=_t(22, 30),
    hours=4.85,
    base_pay=4.85 * 2.13,
)

DEMO_INITIALS = "KG"
DEMO_SECTION = "Patio - 60s"
DEMO_MANAGER = "Brittany"
DEMO_ON_SINCE = "4:30p"
DEMO_TARGET_TIPS = 220.0
DEMO_NOW_MINUTES = _t(20, 52)  # 8:52p, matches the prototype clock


def _items(*pairs):
    """Compact builder: (qty, name, kind) tuples -> EmployeeFloorItem list."""
    return [EmployeeFloorItem(name=name, quantity=qty, kind=kind) for qty, name, kind in pairs]


def demo_today() -> dict:
    """The "today" floor day for the demo server. Matches the JSX prototype."""
    return {
        "label": "Today",
        "sub": "Mon - Jun 8",
        "source": "Demo mode",
        "is_live": False,
        "hours_worked": 4.85,
        "target_tips": DEMO_TARGET_TIPS,
        "tables": [
            EmployeeFloorTable(
                name="62B",
                checks=[
                    EmployeeFloorCheck(
                        id=113, status="open", covers=6,
                        seated_at=_t(19, 32), drinks_fired_at=_t(19, 38), kitchen_fired_at=_t(19, 55),
                        total=219.59,
                        items=_items(
                            (1, "Cena Rita", "cocktail"),
                            (1, "Monday Rita", "cocktail"),
                            (1, "Reposado Old Fashioned", "cocktail"),
                            (1, "Watermelon Ranchwater", "cocktail"),
                            (2, "Bottled Drink", "nonAlcoholic"),
                            (2, "Laredo Combo", "food"),
                            (1, "Chicken Fajita Enchiladas", "food"),
                            (2, "Pollo Con Mango", "food"),
                            (1, "Chicken Mazatlan For One", "food"),
                            (1, "Pico de Gallo", "side"),
                        ),
                    ),
                ],
            ),
            EmployeeFloorTable(
                name="61B",
                checks=[
                    EmployeeFloorCheck(
                        id=110, status="open", covers=2,
                        seated_at=_t(20, 18), drinks_fired_at=_t(20, 23), kitchen_fired_at=_t(20, 34),
                        total=39.45,
                        items=_items(
                            (2, "Monday Rita", "cocktail"),
                            (2, "Water", "nonAlcoholic"),
                            (1, "Chicken Fajita Quesadillas", "food"),
                            (1, "Tortilla Soup", "food"),
                            (1, "Guacamole", "side"),
                        ),
                    ),
                    EmployeeFloorCheck(
                        id=109, status="closed", covers=2,
                        seated_at=_t(19, 5), drinks_fired_at=_t(19, 10), kitchen_fired_at=_t(19, 20),
                        closed_at=_t(20, 12),
                        payment_method="credit", total=50.64, tip=10.13,
                        items=_items(
                            (1, "Monday Rita", "cocktail"),
                            (1, "Water", "nonAlcoholic"),
                            (1, "Tostada Dinner", "food"),
                            (1, "Mexican Wrap", "food"),
                        ),
                    ),
                    EmployeeFloorCheck(
                        id=104, status="closed", covers=1,
                        seated_at=_t(18, 30), drinks_fired_at=None, kitchen_fired_at=_t(18, 36),
                        closed_at=_t(18, 58),
                        payment_method="cash", total=11.34, tip=None,  # cash-tip unknown stays neutral
                        items=_items((1, "Tortilla Soup", "food")),
                    ),
                ],
            ),
            EmployeeFloorTable(
                name="A1",
                checks=[
                    EmployeeFloorCheck(
                        id=117, status="closed", covers=3,
                        seated_at=_t(19, 48), drinks_fired_at=_t(19, 54), kitchen_fired_at=_t(20, 5),
                        closed_at=_t(20, 46),
                        payment_method="credit", total=70.30, tip=14.06,
                        items=_items(
                            (1, "Coffee", "nonAlcoholic"),
                            (1, "Milk", "nonAlcoholic"),
                            (1, "Fountain Drink", "nonAlcoholic"),
                            (2, "Water", "nonAlcoholic"),
                            (1, "Beef Fajita Enchiladas", "food"),
                            (1, "Tacos Al Carbon", "food"),
                            (1, "Queso Dip", "side"),
                            (1, "Beans & Rice", "side"),
                        ),
                    ),
                ],
            ),
            EmployeeFloorTable(
                name="63",
                checks=[
                    EmployeeFloorCheck(
                        id=119, status="closed", covers=4,
                        seated_at=_t(18, 40), drinks_fired_at=_t(18, 45), kitchen_fired_at=_t(18, 57),
                        closed_at=_t(20, 1),
                        payment_method="credit", total=92.78, tip=20.41,
                        items=_items(
                            (2, "Monday Rita", "cocktail"),
                            (1, "Kids Drink", "nonAlcoholic"),
                            (1, "Mix Fajitas For Two", "food"),
                            (1, "Fish El Rey", "food"),
                            (1, "Nuggets", "food"),
                            (1, "Birthday Churros", "dessert"),
                        ),
                    ),
                    EmployeeFloorCheck(
                        id=95, status="closed", covers=1,
                        seated_at=_t(15, 20), drinks_fired_at=_t(15, 23), kitchen_fired_at=_t(15, 31),
                        closed_at=_t(16, 5),
                        payment_method="credit", total=25.55, tip=4.60,
                        items=_items(
                            (1, "Ice Tea", "nonAlcoholic"),
                            (1, "Pollo Con Mango", "food"),
                        ),
                    ),
                ],
            ),
            EmployeeFloorTable(
                name="51",
                checks=[
                    EmployeeFloorCheck(
                        id=87, status="closed", covers=3,
                        seated_at=_t(18, 10), drinks_fired_at=_t(18, 15), kitchen_fired_at=_t(18, 26),
                        closed_at=_t(19, 31),
                        payment_method="credit", total=104.14, tip=22.91,
                        items=_items(
                            (3, "Cena Rita", "cocktail"),
                            (2, "Water", "nonAlcoholic"),
                            (1, "Mix Fajitas For Two", "food"),
                            (1, "Grilled Jalapenos", "side"),
                        ),
                    ),
                ],
            ),
            EmployeeFloorTable(
                name="31",
                checks=[
                    EmployeeFloorCheck(
                        id=69, status="closed", covers=2,
                        seated_at=_t(17, 46), drinks_fired_at=_t(17, 50), kitchen_fired_at=_t(17, 59),
                        closed_at=_t(18, 52),
                        payment_method="credit", total=52.80, tip=10.56,
                        items=_items(
                            (2, "House Rita", "cocktail"),
                            (4, "Monday Rita", "cocktail"),
                            (2, "Water", "nonAlcoholic"),
                            (2, "Plato Catrina", "food"),
                        ),
                    ),
                ],
            ),
            EmployeeFloorTable(
                name="41",
                checks=[
                    EmployeeFloorCheck(
                        id=62, status="closed", covers=2,
                        seated_at=_t(11, 38), drinks_fired_at=_t(11, 42), kitchen_fired_at=_t(11, 51),
                        closed_at=_t(12, 34),
                        payment_method="credit", total=46.41, tip=10.21,
                        items=_items(
                            (1, "Fountain Drink", "nonAlcoholic"),
                            (1, "Water", "nonAlcoholic"),
                            (1, "Fajita Salad", "food"),
                            (1, "Tejano Bacon Cheese", "food"),
                        ),
                    ),
                    EmployeeFloorCheck(
                        id=133, status="closed", covers=1,
                        seated_at=_t(14, 20), drinks_fired_at=_t(14, 22), kitchen_fired_at=None,
                        closed_at=_t(14, 31),
                        payment_method="cash", total=3.51, tip=None,
                        items=_items((1, "Bottled Drink", "nonAlcoholic")),
                    ),
                ],
            ),
        ],
    }


def demo_yesterday() -> dict:
    """The "yesterday" floor day for the day-toggle in Tables."""
    return {
        "label": "Yesterday",
        "sub": "Sun - Jun 7",
        "source": "Demo mode",
        "is_live": False,
        "hours_worked": 6.2,
        "target_tips": 180.0,
        "tables": [
            EmployeeFloorTable(
                name="63",
                checks=[
                    EmployeeFloorCheck(
                        id="y5", status="closed", covers=4,
                        seated_at=_t(17, 20), drinks_fired_at=_t(17, 25), kitchen_fired_at=_t(17, 38),
                        closed_at=_t(18, 45),
                        payment_method="credit", total=120.50, tip=26.51,
                        items=_items(
                            (3, "Cena Rita", "cocktail"),
                            (2, "Water", "nonAlcoholic"),
                            (2, "Laredo Combo", "food"),
                            (1, "Beans & Rice", "side"),
                        ),
                    ),
                    EmployeeFloorCheck(
                        id="y6", status="closed", covers=2,
                        seated_at=_t(18, 55), drinks_fired_at=_t(18, 59), kitchen_fired_at=_t(19, 9),
                        closed_at=_t(19, 58),
                        payment_method="credit", total=52.30, tip=11.51,
                        items=_items(
                            (2, "Monday Rita", "cocktail"),
                            (1, "Beef Fajita Enchiladas", "food"),
                        ),
                    ),
                ],
            ),
            EmployeeFloorTable(
                name="52",
                checks=[
                    EmployeeFloorCheck(
                        id="y2", status="closed", covers=3,
                        seated_at=_t(13, 40), drinks_fired_at=_t(13, 45), kitchen_fired_at=_t(13, 56),
                        closed_at=_t(14, 58),
                        payment_method="credit", total=98.40, tip=21.65,
                        items=_items(
                            (4, "Monday Rita", "cocktail"),
                            (1, "Mix Fajitas For Two", "food"),
                            (1, "Queso Dip", "side"),
                        ),
                    ),
                ],
            ),
            EmployeeFloorTable(
                name="31",
                checks=[
                    EmployeeFloorCheck(
                        id="y7", status="closed", covers=2,
                        seated_at=_t(19, 10), drinks_fired_at=_t(19, 15), kitchen_fired_at=_t(19, 25),
                        closed_at=_t(20, 20),
                        payment_method="credit", total=76.85, tip=16.91,
                        items=_items(
                            (1, "Reposado Old Fashioned", "cocktail"),
                            (1, "Watermelon Ranchwater", "cocktail"),
                            (1, "Chicken Mazatlan For One", "food"),
                            (1, "Pollo Con Mango", "food"),
                        ),
                    ),
                ],
            ),
        ],
    }


def demo_ledger() -> list[dict]:
    """The daily-ledger rows for the Today tab. The `live=True` marker is reserved
    for the row that matches the demo's current shift; everything else is past."""
    return [
        {"day": "Mon - Jun 8", "base": 10.33, "tips": 151.42, "hours": 4.85, "live": True},
        {"day": "Sun - Jun 7", "base": 13.21, "tips": 178.15, "hours": 6.2, "live": False},
        {"day": "Sat - Jun 6", "base": 25.84, "tips": 137.29, "hours": 7.4, "live": False},
        {"day": "Fri - Jun 5", "base": 0.0,   "tips": 0.0,    "hours": 0.0, "live": False},
        {"day": "Thu - Jun 4", "base": 28.10, "tips": 121.40, "hours": 7.8, "live": False},
    ]


def best_next_action(day: dict, stats: dict) -> dict:
    """Coaching message; mirrors the JSX `bestNextAction` heuristic."""
    # The "hot" attention state in the prototype was hand-tagged. In production,
    # the Toast adapter (see floor_toast_adapter.py) is responsible for marking
    # it. For demo we mark 62B (the first open table) hot.
    open_checks = [c for table in day.get("tables", []) for c in table.checks if c.status == "open"]
    if open_checks:
        table_name = next(
            (table.name for table in day["tables"]
             if any(c.status == "open" for c in table.checks)),
            None,
        )
        return {
            "title": "Best next move",
            "body": (
                f"{table_name} has food fired and a strong check. Pre-bus, refill, "
                "and ask for dessert before dropping the check."
            ),
        }
    dessert_pct = stats.get("dessert_attach_pct") or 0
    if dessert_pct < 18:
        return {
            "title": "Dessert opportunity",
            "body": (
                f"Dessert attach is {dessert_pct:.0f}%. Add one churro or dessert ask "
                "per table to lift tips without adding much time."
            ),
        }
    to_drink = stats.get("seat_to_first_drink_avg_min")
    pace = round(to_drink) if to_drink is not None else 0
    return {
        "title": "Keep the pace",
        "body": f"Drink speed is {pace}m. Stay close to that pace and close checks cleanly.",
    }


def table_attention(table) -> str:
    """Color attention bucket for the station-map chip.
    hot   = open check that has both drinks and kitchen fired
    close = open check that is nearly done (drinks but no kitchen, or vice versa)
    good  = closed with tip percent >= 20%
    paid  = closed otherwise (or cash-unknown)
    idle  = no checks yet
    """
    if not table.checks:
        return "idle"
    open_checks = [c for c in table.checks if c.status == "open"]
    if open_checks:
        for c in open_checks:
            if c.drinks_fired_at is not None and c.kitchen_fired_at is not None:
                return "hot"
        return "close"
    # closed only
    best_pct = None
    for c in table.checks:
        if c.tip is not None and c.total:
            pct = c.tip / c.total * 100
            best_pct = pct if best_pct is None or pct > best_pct else best_pct
    if best_pct is not None and best_pct >= 20:
        return "good"
    return "paid"


def table_summary(table) -> dict:
    """Compact chip data for the station map."""
    open_check = next((c for c in table.checks if c.status == "open"), None)
    total = sum(c.total or 0 for c in table.checks)
    if open_check:
        return {"label": f"open - ${open_check.total:.0f}"}
    if any(c.payment_method == "cash" and c.tip is None for c in table.checks):
        return {"label": "cash unknown"}
    if table.checks and all(c.status == "closed" for c in table.checks):
        return {"label": f"paid - ${total:.0f}"}
    return {"label": "ready"}


def clock(minutes: int | None) -> str:
    """8:52p style clock label, matches the JSX `toClock`."""
    if minutes is None:
        return ""
    h, m = divmod(int(minutes), 60)
    ap = "p" if h >= 12 else "a"
    hh = 12 if h % 12 == 0 else h % 12
    return f"{hh}:{m:02d}{ap}"


def ago(minutes: int | None, now: int) -> str:
    """Relative time, e.g. '57m ago'. Matches the JSX `ago`."""
    if minutes is None:
        return ""
    diff = max(0, int(now) - int(minutes))
    if diff < 1:
        return "just now"
    if diff < 60:
        return f"{diff}m ago"
    return f"{diff // 60}h {diff % 60}m ago"
