from app.services.order_view_presenter import build_combined_order_card_views
from app.domain.master_sheet_map import MASTER_ROWS
from app.services.orders_query import _clock_minutes


def test_master_print_header_rows_start_with_dispatch_timing_setup():
    assert [row["key"] for row in MASTER_ROWS[:7]] == [
        "meta.order_id",
        "meta.date",
        "meta.driver",
        "meta.deliver_at",
        "meta.kitchen_ready",
        "meta.driver_depart",
        "meta.setup_required",
    ]


def test_order_view_template_prints_only_active_copy_and_hides_empty_rows():
    from pathlib import Path

    html = Path("app/templates/order_view.html").read_text(encoding="utf-8")

    assert ".grid-view { display: none !important; }" in html
    assert ".grid-view.is-active" in html
    assert ".order-view-header { display: none !important;" in html
    assert "print-empty-row" in html
    assert 'class="grid-view{% if active_view == view_name %} is-active{% endif %}' in html
    assert "order-print-landscape" in html
    assert "size: letter landscape;" in html
    assert "grid-auto-flow: row !important;" in html
    assert "grid-template-columns: repeat(4, minmax(0, 1fr)) !important;" in html
    assert '<div class="order-card-eyebrow">Order</div>' not in html


def _row(key: str, label: str, section: str = "Header") -> dict[str, object]:
    return {"key": key, "label": label, "section": section, "sort": 1}


def test_combined_order_cards_hide_empty_rows_per_order_and_skip_total_column():
    grids = {
        "master": {
            "view": "master",
            "rows": [
                _row("meta.order_id", "Order #"),
                _row("meta.client", "Client"),
                _row("meta.ask_for", "Ask For"),
                _row("meta.setup", "Setup"),
                _row("meta.no_value", "No Value"),
            ],
            "columns": [
                {
                    "order_id": "00W-8QH",
                    "values": {
                        "meta.order_id": "00W-8QH",
                        "meta.client": "Alpha Clinic",
                        "meta.ask_for": "",
                        "meta.setup": "No",
                        "meta.no_value": "N/A",
                    },
                },
                {
                    "order_id": "2RJ-547",
                    "values": {
                        "meta.order_id": "2RJ-547",
                        "meta.client": "Beta Office",
                        "meta.ask_for": "Maria",
                        "meta.setup": "0",
                        "meta.no_value": None,
                    },
                },
            ],
        },
        "kitchen": {
            "view": "kitchen",
            "rows": [
                _row("meta.order_id", "Order #"),
                _row("chips.total_oz", "Chips oz", "Cold"),
            ],
            "columns": [
                {"order_id": "Total", "values": {"chips.total_oz": "72"}},
                {"order_id": "00W-8QH", "values": {"chips.total_oz": "36"}},
            ],
        },
    }

    cards = build_combined_order_card_views(grids)

    assert [card["order_id"] for card in cards["master"]] == ["00W-8QH", "2RJ-547"]
    assert [field["label"] for field in cards["master"][0]["fields"]] == [
        "Client",
        "Setup",
    ]
    assert cards["master"][0]["fields"][1]["value"] == "No"
    assert [field["label"] for field in cards["master"][1]["fields"]] == [
        "Client",
        "Ask For",
    ]
    assert [card["order_id"] for card in cards["kitchen"]] == ["00W-8QH"]


def test_combined_order_cards_preserve_grid_column_order_for_all_tabs():
    grids = {
        "driver": {
            "view": "driver",
            "rows": [_row("meta.driver", "Dispatch")],
            "columns": [
                {"order_id": "EARLY", "values": {"meta.driver": "DRIVER A"}},
                {"order_id": "LATE", "values": {"meta.driver": "DRIVER B"}},
            ],
        },
        "prep_expo": {
            "view": "prep_expo",
            "rows": [_row("items.taco", "Tacos", "Hot")],
            "columns": [
                {"order_id": "EARLY", "values": {"items.taco": "12"}},
                {"order_id": "LATE", "values": {"items.taco": "24"}},
            ],
        },
    }

    cards = build_combined_order_card_views(grids)

    assert [card["order_id"] for card in cards["driver"]] == ["EARLY", "LATE"]
    assert [card["order_id"] for card in cards["prep_expo"]] == ["EARLY", "LATE"]


def test_combined_order_card_header_uses_kitchen_ready_and_dropdown_driver():
    grids = {
        "master": {
            "view": "master",
            "rows": [
                _row("meta.order_id", "Order #"),
                _row("meta.deliver_at", "Deliver At"),
                _row("meta.client", "Client"),
                _row("meta.kitchen_ready", "Kitchen Ready"),
                _row("meta.driver", "Dispatch"),
            ],
            "columns": [
                {
                    "order_id": "8PY-57Y",
                    "values": {
                        "meta.order_id": "8PY-57Y",
                        "meta.deliver_at": "11:45 AM",
                        "meta.client": "Dr Malik",
                        "meta.kitchen_ready": "10:40 AM",
                        "meta.driver": "DRIVER D",
                    },
                },
            ],
        },
    }

    cards = build_combined_order_card_views(
        grids,
        header_driver_by_order={"8PY-57Y": "Sam CK #2"},
    )

    assert cards["master"][0]["header_fields"] == ["10:40 AM", "Sam CK #2"]


def test_clock_minutes_sorts_am_times_by_clock_not_string_order():
    ordered = sorted(["10:40 AM", "9:44 AM", "9:00 AM"], key=_clock_minutes)

    assert ordered == ["9:00 AM", "9:44 AM", "10:40 AM"]
