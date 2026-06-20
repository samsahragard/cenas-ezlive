from __future__ import annotations

from pathlib import Path

from app.domain.grid_builder import build_all_view_grids
from app.domain.kitchen_engine import build_kitchen_result
from app.domain.master_sheet_map import build_all_outputs
from app.domain.menu_catalog import MENU_CATALOG, MenuCatalog
from app.domain.ticket_context import build_ticket_context
from app.services.catering_calculation_presenter import build_catering_calculation_detail


def _item(
    item_key: str,
    package_type: str,
    qty: int,
    *,
    packaging: str = "tray",
    beans: str = "none",
    tortillas: str = "none",
    extras: list[dict] | None = None,
    container: str | None = None,
) -> dict:
    return {
        "item_key": item_key,
        "package_type": package_type,
        "qty": qty,
        "choices": {
            "packaging": packaging,
            "beans": beans,
            "tortillas": tortillas,
            "with_ice": None,
        },
        "extras": extras or [],
        "container": container,
        "source": {"raw_alias": item_key, "raw_qty": qty, "raw_line_items": []},
        "flags": [],
    }


def _order(items: list[dict], headcount: int = 0) -> dict:
    return {
        "order_id": "DEV-123",
        "date": "2026-06-16",
        "deliver_at": "11:00 AM",
        "reported_store": "Tomball",
        "reported_store_id": "store_2",
        "origin_store_id": "store_2",
        "headcount": headcount,
        "client": "Client",
        "upon_delivery_ask_for": "",
        "customer_phone": "",
        "delivery_address": "",
        "delivery_window": {"start": "", "end": ""},
        "delivery_instructions": None,
        "setup_required": None,
        "notes": None,
        "normalized_items": items,
        "flags": [],
    }


def _detail(items: list[dict], headcount: int = 0) -> dict:
    order = _order(items, headcount=headcount)
    result = build_kitchen_result(order)
    ctx = build_ticket_context(order, result, {
        "kitchen_ready_time": "10:00 AM",
        "driver_departure_time": "10:30 AM",
        "assigned_driver": "Driver A",
    })
    views = build_all_outputs(order, result, ctx, MenuCatalog(MENU_CATALOG))
    bundle = {
        "order_id": order["order_id"],
        "normalized_order": order,
        "kitchen_result": result,
        "ticket_context": ctx,
        "views": views,
        "dispatch": {},
    }
    grids = build_all_view_grids([bundle], collapse_empty_rows=True)
    return build_catering_calculation_detail(bundle, grids)


def _row(detail: dict, label: str) -> dict:
    return next(row for row in detail["rows"] if row["label"] == label)


def test_developer_calculation_explains_bulk_food_and_tableware():
    detail = _detail([
        _item("fajitas_mixed", "fajitas", 80, beans="charro", tortillas="flour"),
        _item("tableware", "non_food_items", 1),
    ])

    chicken = _row(detail, "Chicken (Lb)")
    silverware = _row(detail, "Silverware")
    plates = _row(detail, "Plates")
    tortillas = _row(detail, "Flour (pkts of 2)")

    assert chicken["amount"] == "12.5"
    assert chicken["calculation"] == "80 x 2.5oz = 200oz = 12.5lb"
    assert silverware["amount"] == "83"
    assert silverware["calculation"] == "80 bulk + 3 buffer = 83"
    assert plates["amount"] == "83"
    assert plates["calculation"] == "80 bulk + 3 buffer = 83"
    assert "80 x 2.5 tortillas" in tortillas["calculation"]


def test_developer_calculation_adds_tableware_without_pdf_tableware_line():
    detail = _detail([
        _item("fajitas_mixed", "fajitas", 65, beans="charro", tortillas="flour"),
        _item("cenas_exec_spread", "premium", 10, packaging="tray", tortillas="flour"),
    ])

    silverware = _row(detail, "Silverware")
    plates = _row(detail, "Plates")
    large_spoons = _row(detail, "Black Large Spoons")
    small_spoons = _row(detail, "Black Small Spoons")
    black_tongs = _row(detail, "Black Tongs")

    assert silverware["amount"] == "78"
    assert silverware["calculation"] == "75 bulk + 3 buffer = 78"
    assert plates["amount"] == "78"
    assert plates["calculation"] == "75 bulk + 3 buffer = 78"
    assert large_spoons["amount"] != ""
    assert small_spoons["amount"] != ""
    assert black_tongs["amount"] != ""


def test_developer_calculation_explains_individual_tableware():
    detail = _detail([
        _item("fajitas_mixed", "fajitas", 30, packaging="individual", beans="charro", tortillas="flour"),
        _item("tableware", "non_food_items", 1),
    ])

    individual = _row(detail, "Beef & Chicken")
    silverware = _row(detail, "Silverware")
    plates = _row(detail, "Plates")

    assert individual["amount"] == "30"
    assert individual["calculation"] == "30 ordered as individual packages"
    assert silverware["amount"] == "33"
    assert silverware["calculation"] == "30 individual + 3 buffer = 33"
    assert plates["amount"] == "0"
    assert plates["calculation"] == "individual packaging uses 0 plates"


def test_developer_calculation_explains_salad_dressing_and_side_units():
    detail = _detail([
        _item(
            "cobb_salad",
            "salads",
            9,
            extras=[
                {"name": "dressing", "raw_text": "Most Popular"},
                {"name": "protein", "raw_text": "chicken"},
            ],
        ),
        _item("queso_and_chips", "sides", 1, container="quart"),
        _item("tableware", "non_food_items", 1),
    ])

    chicken = _row(detail, "Chicken Diced (Lb)")
    dressing = _row(detail, "Salad Dressing (oz)")
    queso = _row(detail, "Queso")

    assert chicken["amount"] == "1.12"
    assert chicken["calculation"] == "9 x 2oz = 18oz = 1.12lb"
    assert dressing["amount"] == "27 Most Popular"
    assert dressing["calculation"] == "9 x 3oz Most Popular = 27oz"
    assert queso["amount"] == "1 quart"
    assert queso["calculation"] == "1 ordered; unit shown as quart"


def test_catering_dashboard_exposes_developer_tab():
    route_source = Path("app/web/store_routes.py").read_text(encoding="utf-8")
    dash_template = Path("app/templates/catering_dashboard.html").read_text(encoding="utf-8")
    orders_template = Path("app/templates/orders_by_store.html").read_text(encoding="utf-8")

    assert '("developer",  "Developer")' in route_source
    assert "store.catering_developer" in route_source
    assert "'developer':" in dash_template
    assert "/catering/developer/orders/" in orders_template
