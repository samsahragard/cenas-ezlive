from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "templates"


def _read(path: str) -> str:
    return (TEMPLATES / path).read_text(encoding="utf-8")


def test_driver_orders_uses_mobile_cards_not_wide_table():
    template = _read("driver_orders.html")

    assert "<table" not in template
    assert "do-table" not in template
    assert "do-help" not in template
    assert "Start Delivery timestamps" not in template
    assert "do-card" in template
    assert "<details class=\"do-panel do-proof-dropdown\">" in template
    assert "<summary class=\"do-proof-summary\">Complete delivery</summary>" in template
    assert "do-proof-grid" in template
    assert ".ck-mobile-trigger" in template
    assert ".ck-topbar .topbar-right .dash-role-banner" in template


def test_ez_market_driver_stats_strip_removed():
    template = _read("ez_market.html")

    assert '<span class="word-primary">Ez</span> <span class="ck-accent">Market</span>' in template
    assert "Hi," not in template
    assert ".ck-topbar .menu-toggle" in template
    assert ".ck-topbar .topbar-right .dash-role-banner" in template
    assert "em-stats" not in template
    assert "em-disclaimer" not in template
    assert "Potential today" not in template
    assert "Potential week" not in template
    assert "Estimates only" not in template


def test_driver_bottom_nav_order_and_status_removed():
    template = _read("partials/_bottom_nav.html")

    expected_order = [
        "('/my-profile',  'my_profile'",
        "('/driver/orders','driver_orders'",
        "('/ez-market',   'ez_market'",
        "('/pay-history', 'pay_history'",
    ]
    positions = [template.index(marker) for marker in expected_order]

    assert positions == sorted(positions)
    assert "driver_logs" not in template
    assert "'Status'" not in template


def test_driver_sidebar_matches_driver_nav_order():
    template = _read("partials/sidebar.html")

    expected_order = [
        'href="/my-profile"',
        'href="/driver/orders"',
        'href="/ez-market"',
        'href="/pay-history"',
    ]
    positions = [template.index(marker) for marker in expected_order]

    assert positions == sorted(positions)
    assert 'href="/driver/logs"' not in template


def test_driver_profile_hides_role_badge():
    template = _read("my_profile.html")

    assert ".ck-topbar .topbar-right .dash-role-banner" in template
    assert "display: none !important;" in template
