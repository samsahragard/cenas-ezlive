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
    assert "<summary class=\"do-proof-summary\">UPLOAD PHOTOS</summary>" in template
    assert "color: #FFFFFF;" in template
    assert "do-proof-grid" in template
    assert ".ck-mobile-trigger" in template
    assert ".ck-topbar .topbar-right .dash-role-banner" in template
    assert "driver.driver_logout" not in template
    assert "Sign out" not in template
    assert "do-address" not in template
    assert "do-client" not in template
    assert "do-panel-title" not in template
    assert "Order details pending" not in template
    assert "driver.driver_order_start" not in template
    assert "Start Delivery" not in template
    assert "do-btn secondary" not in template
    assert "do-start-status" in template
    assert '<span class="do-badge{% if o.status == \'delivered\' %} done{% endif %}">' in template
    assert ".do-card {\n    background: linear-gradient(145deg, #6B241B 0%, #491511 100%);" in template
    assert "border-left: 0;\n    border-right: 0;\n    border-radius: 0;" in template
    assert "background: rgba(28,8,6,0.38);" in template
    assert ".do-date { color: #FFD970; font-weight: 800; font-size: 15px;" in template
    assert ".do-time { color: #FAF6EC; font-weight: 700; font-size: 15px;" in template
    assert ".do-link { color: #FFD970; text-decoration: none; font-weight: 800; font-size: 15px;" in template


def test_driver_pay_history_hides_header_menu():
    template = _read("pay_history.html")
    partial = _read("partials/_paycheck_periods.html")

    assert ".ck-topbar .menu-toggle" in template
    assert ".ck-mobile-trigger" in template
    assert "display: none !important;" in template
    assert ".ph-disclaimer," in template
    assert ".ph-check," in template
    assert ".ph-period-summary," in template
    assert ".ph-table-scroll," in template
    assert "background: linear-gradient(145deg, #6B241B 0%, #491511 100%) !important;" in template
    assert "class=\"ph-period-summary\"" in partial
    assert "class=\"table-scroll ph-table-scroll\"" in partial
    assert "class=\"log-table log-table-view ph-log-table\"" in partial
    assert "border-left: 0 !important;" in template
    assert "border-right: 0 !important;" in template
    assert "border-radius: 0 !important;" in template


def test_driver_order_photo_uploads_allow_photo_library_on_mobile():
    template = _read("driver_orders.html")

    assert '<input type="file" name="delivery_photo" accept="image/*">' in template
    assert '<input type="file" name="parking_photo" accept="image/*">' in template
    assert "capture=" not in template


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
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in template
    assert ".em-tab {\n    display: inline-flex;" in template
    assert "background: linear-gradient(145deg, #6B241B 0%, #491511 100%);" in template
    assert "em-card-head" in template
    assert ".em-card {\n    background: linear-gradient(145deg, #6B241B 0%, #491511 100%);" in template
    assert "border: 0.5px solid rgba(243,111,77,0.58);" in template
    assert "border-left: 0;\n    border-right: 0;\n    border-radius: 0;" in template
    assert "border-left:0; border-right:0; border-radius:0;" in template
    assert ".em-date" in template
    assert "font-size: 18px;" in template
    assert ".em-card-head .em-time {\n    font-size: 18px;" in template
    assert ".em-card-head .em-payout { font-size: 18px;" in template
    assert "em-route-row" in template
    assert "em-route-pickup" in template
    assert "em-route-address" in template
    assert "grid-template-columns: minmax(0, 1fr) max-content minmax(0, 1fr);" in template
    assert "<div class=\"em-date\">{{ o.delivery_date or 'No date' }}</div>" in template
    assert "<div class=\"em-time\">{{ o.deliver_at or '—' }}</div>" in template
    assert "{{ (pickup_label(o) or 'n/a')|replace(' Kitchen', '') }}" in template
    assert '<div class="em-route-address">{{ o.delivery_address or \'n/a\' }}</div>' in template
    available_block = template.split('<div id="tab-available">', 1)[1].split('{% if not public_demo|default(false) %}', 1)[0]
    assert "mi from pickup" not in available_block
    assert "{{ o.headcount or '—' }} heads" not in available_block
    assert "em-trip-leg" not in available_block


def test_driver_bottom_nav_order_and_status_removed():
    template = _read("partials/_bottom_nav.html")

    expected_order = [
        "('/my-profile',  'my_profile'",
        "('/driver/orders','driver_orders'",
        "('/ez-market',   'ez_market'",
        "('/pay-history', 'pay_history'",
        "('/info',        'driver_info'",
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
        'href="/info"',
    ]
    positions = [template.index(marker) for marker in expected_order]

    assert positions == sorted(positions)
    assert 'href="/driver/logs"' not in template


def test_driver_profile_hides_role_badge():
    template = _read("my_profile.html")
    base = _read("base_dashboard.html")

    assert ".ck-topbar .topbar-right .dash-role-banner" in template
    assert "display: none !important;" in template
    assert ".mp-hero { background: linear-gradient(145deg, #6B241B 0%, #491511 100%)" in template
    assert "border-left: 0; border-right: 0; border-radius: 0;" in template
    assert '<div class="mp-score-block">' in template
    foot_block = template.split('<div class="mp-hero-foot">', 1)[1].split("</div>\n</div>", 1)[0]
    assert "more points to unlock" in foot_block
    assert "Score details" in foot_block
    assert "mp-unlock" in template
    assert "mp-hub-right" in template
    assert "mp-hub-count" in template
    assert ".mp-hub-kpi:last-child { text-align: right; }" in template
    assert "body.ck-role-driver .ck-topbar" in base
    assert "background-image: linear-gradient(145deg, #6B241B 0%, #491511 100%)" in base
    assert "border-left: 0;\n      border-right: 0;\n      border-radius: 0;" in base
    assert "html:has(body.ck-role-driver)" in base
    assert "--app-bg: #05060a;" in base
    assert "radial-gradient(circle at 10% 20%, rgba(143, 178, 214, 0.10), transparent 24%)" in base


def test_driver_profile_is_hub_and_info_holds_reference_sections():
    profile = _read("my_profile.html")
    info = _read("driver_info.html")

    assert "mp-hub-card" in profile
    assert "mp-hub-right" in profile
    assert "mp-hub-kpi-line" in profile
    assert "Score details" in profile
    assert "active order" not in profile
    assert "estimated for this pay period" not in profile
    assert "Score breakdown" not in profile
    assert "How your pay works" not in profile
    assert "The rules" not in profile

    assert "Score breakdown" in info
    assert "How your pay works" in info
    assert "The rules" in info
    assert "Unlock at" in info
    assert ".mp-info-band" in info
    assert "background: linear-gradient(145deg, #6B241B 0%, #491511 100%);" in info
    assert "border-left: 0;\n    border-right: 0;\n    border-radius: 0;" in info
    assert '<div class="mp-score-box mp-info-band">' in info
    assert '<div class="mp-pay-box mp-info-band">' in info
    assert '<div class="mp-rules mp-info-band">' in info
