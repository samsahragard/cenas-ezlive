from __future__ import annotations

from datetime import date, datetime

from app.models import ProducePriceSnapshot, VendorRecentOrder
from app.services.vendor_reports import build_produce_report, build_supply_report


def test_supply_report_normalizes_item_shapes_and_price_changes(db_session):
    db_session.add_all([
        VendorRecentOrder(
            vendor="webstaurant",
            store_scope="tomball",
            order_number="W-1",
            placed_at=datetime(2099, 6, 1, 10, 0),
            total_cents=2500,
            status="confirmed",
            parse_status="parsed",
            items_json=[{
                "name": "Foil Pan",
                "sku": "PAN-1",
                "qty": "2 cases",
                "unit_price_cents": 1000,
                "subtotal_cents": 2000,
            }],
        ),
        VendorRecentOrder(
            vendor="webstaurant",
            store_scope="tomball",
            order_number="W-2",
            placed_at=datetime(2099, 6, 8, 10, 0),
            total_cents=1200,
            status="confirmed",
            parse_status="parsed",
            items_json={"items": [{
                "name": "Foil Pan",
                "sku": "PAN-1",
                "qty": 1,
                "unit_price_cents": 1200,
                "subtotal_cents": 1200,
            }]},
        ),
    ])
    db_session.commit()

    report = build_supply_report(
        db_session,
        "webstaurant",
        date(2099, 6, 1),
        date(2099, 6, 30),
        "tomball",
    )

    assert report["summary"]["orders"] == 2
    assert report["summary"]["spend"] == "$37.00"
    assert report["summary"]["units"] == "3"
    assert report["top_items"][0]["name"] == "Foil Pan"
    assert report["top_items"][0]["latest_unit"] == "$12.00"
    assert report["price_watch"][0]["price_delta_pct_display"] == "+20.0%"


def test_produce_report_builds_price_matrix_and_watch(db_session):
    db_session.add_all([
        ProducePriceSnapshot(
            snapshot_date="2099-06-01",
            vendor="alvarado",
            canonical_name="Limes",
            canonical_size="150ct",
            price=20.0,
        ),
        ProducePriceSnapshot(
            snapshot_date="2099-06-08",
            vendor="alvarado",
            canonical_name="Limes",
            canonical_size="150ct",
            price=24.0,
        ),
        ProducePriceSnapshot(
            snapshot_date="2099-06-08",
            vendor="jluna",
            canonical_name="Limes",
            canonical_size="150ct",
            price=23.0,
        ),
    ])
    db_session.commit()

    report = build_produce_report(
        db_session,
        date(2099, 6, 1),
        date(2099, 6, 30),
        "both",
    )

    assert report["summary"]["tracked_quote_items"] == 1
    assert report["summary"]["price_snapshots"] == 3
    assert report["price_rows"][0]["cheaper"] == "J. Luna"
    assert report["price_watch"][0]["name"] == "Limes"
    assert report["price_watch"][0]["price_delta_pct_display"] == "+20.0%"
