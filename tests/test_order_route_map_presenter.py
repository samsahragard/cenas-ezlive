from datetime import datetime
from pathlib import Path

from jinja2 import Environment

from app.models import Order
from app.services.order_route_map_presenter import (
    build_route_map_payload,
    kitchen_slug_for_order,
    route_map_order_payload,
)


def test_route_map_uses_physical_kitchen_from_origin_store():
    copperfield = Order(
        external_order_id="CFD-123",
        origin_store_id="store_3",
        delivery_address="100 Test Rd, Houston, TX",
    )
    tomball = Order(
        external_order_id="TOM-456",
        origin_store_id="store_4",
        delivery_address="200 Test Rd, Tomball, TX",
    )

    assert kitchen_slug_for_order(copperfield) == "copperfield"
    assert kitchen_slug_for_order(tomball) == "tomball"


def test_route_map_prefers_pickup_kitchen_when_present():
    order = Order(
        external_order_id="PCK-789",
        origin_store_id="store_1",
        pickup_kitchen="tomball",
        delivery_address="300 Test Rd, Tomball, TX",
    )

    payload = route_map_order_payload(order)

    assert payload["kitchen_slug"] == "tomball"
    assert payload["kitchen"]["label"] == "Tomball"
    assert payload["is_routable"] is True


def test_route_map_marks_orders_without_address_or_pickup_as_unroutable():
    order = Order(
        external_order_id="BAD-001",
        origin_store_id="unknown",
        delivery_address="",
    )

    payload = route_map_order_payload(order)

    assert payload["is_routable"] is False
    assert "Pickup kitchen is unknown." in payload["issues"]
    assert "Delivery address is missing." in payload["issues"]


def test_route_map_payload_includes_current_order_details():
    order = Order(
        external_order_id="1KP-QAJ",
        origin_store_id="store_2",
        delivery_date="2026-06-11",
        deliver_at="11:15 AM",
        client="Christopher Gordillo",
        upon_delivery_ask_for="Chris",
        delivery_address="21401 Park Row Blvd, Katy, TX 77449",
        pickup_miles=18.4,
        updated_at=datetime(2026, 6, 10, 19, 45),
    )

    payload = build_route_map_payload([order])

    assert payload["orders"][0]["order_id"] == "1KP-QAJ"
    assert payload["orders"][0]["deliver_at"] == "11:15 AM"
    assert payload["orders"][0]["pickup_miles"] == 18.4
    assert payload["orders"][0]["kitchen"]["label"] == "Tomball"
    assert payload["kitchens"]["tomball"]["address"]


def test_route_map_template_keeps_google_directions_contract():
    html = Path("app/templates/order_route_map.html").read_text(encoding="utf-8")

    Environment().parse(html)

    assert "DirectionsRenderer" in html
    assert "DirectionsService" in html
    assert "duration_in_traffic" in html
    assert "Refresh Routes" in html
    assert "DATA_URL" in html
