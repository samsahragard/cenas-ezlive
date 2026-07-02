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


def test_prep_list_mobile_controls_and_item_cards_are_compact():
    source = _template("prep_list.html")

    assert ".prep-mobile-strip" in source
    assert "grid-template-columns: minmax(96px, 1.75fr) repeat(4, minmax(42px, 0.72fr));" in source
    assert "date_display_mobile" in source
    assert "prep-export-btn" in source
    assert ".prep-section-body" in source
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in source
    assert "{% for word in it.name.split() %}<span>{{ word }}</span>{% endfor %}" in source
    assert "prep-sheet-scrim" in source
    assert "prep-sheet-head" in source
    assert "data-prep-close" in source
    assert "body.prep-sheet-open" in source
    assert "<div class=\"prep-panel-title\">Perform</div>" in source


def test_fresh_food_place_order_uses_compact_order_day_row():
    source = _template("fresh_food_place_order.html")

    assert "Order day" in source
    assert "ffpo-date-short" in source
    assert "ffpo-date-weekday" in source
    assert "ffpo-order-by" in source
    assert "orderer_first" in source
    assert "<i class=\"ti ti-send\"></i>Submit" in source
    assert ".ff-cat-table {\n      display: grid;" in source
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in source
    assert ".ff-mobile-card.has-inv,\n    .ff-mobile-card.has-order" in source
    assert ".ff-mobile-card.open.has-inv,\n    .ff-mobile-card.open.has-order" in source
    assert "data-ff-mobile-toggle" in source
    assert "ff-mobile-panel" in source
    assert "data-ff-mobile-kind=\"inv\"" in source
    assert "data-ff-mobile-kind=\"or\"" in source
    assert "body.ff-mobile-sheet-open" in source
    assert "body.ff-mobile-sheet-open .ffpo-submit-row" in source
    assert "bottom: calc(76px + env(safe-area-inset-bottom, 0px));" in source
    assert "body.ff-mobile-sheet-open .ck-bnav" in source
    assert "appearance: textfield;" in source
    assert ".ff-mobile-num::-webkit-inner-spin-button" in source
    assert "text-align: center;" in source
    assert "background: linear-gradient(180deg, #7b2f24 0%, #5a1c16 100%);" in source
    assert "cenas:fresh-food-place-order:v1:" in source
    assert "window.localStorage.setItem(draftKey" in source
    assert "window.localStorage.removeItem(draftKey)" in source
    assert "Placed by" not in source
    assert "Submit order" not in source
    assert "delivery.strftime('%A, %B %d')" not in source
    assert "ff-today-date-label" not in source
