from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _template(name: str) -> str:
    return (ROOT / "app" / "templates" / name).read_text(encoding="utf-8")


def test_fresh_food_mobile_tabs_use_five_phone_columns():
    for name in (
        "fresh_food_place_order.html",
        "fresh_food_recent_orders.html",
        "fresh_food_developer.html",
    ):
        source = _template(name)

        assert ".ff-tabs {\n      display: grid;" in source
        assert "grid-template-columns: repeat(5, minmax(0, 1fr));" in source
        assert "white-space: nowrap;" in source
        assert ">Dev</a>" in source
        assert ">Developer</a>" not in source


def test_ezcater_mobile_tabs_use_bounded_columns():
    source = _template("orders_by_store.html")

    assert ".ezo-page .ezo-tabs {\n      display: grid;" in source
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in source
    assert ".ezo-page .ezo-store-filters {\n      display: grid;" in source
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in source
    assert "white-space: normal;" in source


def test_prep_team_today_uses_compact_three_column_rows():
    source = _template("prep_list.html")

    assert "grid-template-columns: 32px minmax(0, 1fr) auto;" in source
    assert "grid-template-columns: 28px minmax(0, 1fr) auto;" in source
    assert "{{ member_initial }}" in source
    assert "{{ member_first_name }}" in source
    assert ">Assigned</span>" in source
    assert ">Not assigned</span>" in source
    assert "Not assigned to prep" not in source
    assert "Assigned to {{ member.assignment_count }}" not in source


def test_fresh_food_place_order_uses_compact_order_day_row():
    source = _template("fresh_food_place_order.html")

    assert "Order day" in source
    assert "ffpo-date-short" in source
    assert "ffpo-date-weekday" in source
    assert "ffpo-order-by" in source
    assert "orderer_first" in source
    assert "<i class=\"ti ti-send\"></i>Submit" in source
    assert "grid-template-columns: minmax(86px, 1fr) 52px 52px 46px 32px;" in source
    assert "Placed by" not in source
    assert "Submit order" not in source
    assert "delivery.strftime('%A, %B %d')" not in source
    assert "ff-today-date-label" not in source
