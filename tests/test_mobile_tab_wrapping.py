from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _template(name: str) -> str:
    return (ROOT / "app" / "templates" / name).read_text(encoding="utf-8")


def test_fresh_food_mobile_tabs_wrap_instead_of_scrolling_sideways():
    for name in (
        "fresh_food_place_order.html",
        "fresh_food_recent_orders.html",
        "fresh_food_developer.html",
    ):
        source = _template(name)

        assert ".ff-tabs {\n      flex-wrap: wrap;" in source
        assert "flex: 1 1 calc(33.333% - 6px);" in source
        assert "white-space: normal;" in source


def test_ezcater_mobile_tabs_use_bounded_columns():
    source = _template("orders_by_store.html")

    assert ".ezo-page .ezo-tabs {\n      display: grid;" in source
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in source
    assert ".ezo-page .ezo-store-filters {\n      display: grid;" in source
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in source
    assert "white-space: normal;" in source
