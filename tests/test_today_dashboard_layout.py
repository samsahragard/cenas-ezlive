from pathlib import Path


def test_mobile_dashboard_ribbons_are_hidden_without_removing_tabs():
    dashboards = [
        (
            "today_dashboard.html",
            ".tdydash .tdyd-ribbon { display: none; }",
            '<div class="tdyd-tabs-row">',
            '<div class="tdyd-tabs" id="tdydTabs" role="tablist">',
        ),
        (
            "manager_dashboard.html",
            ".mgrdash .mgd-ribbon { display: none; }",
            '<div class="mgd-tabs" id="mgdTabs" role="tablist">',
        ),
        (
            "catering_dashboard.html",
            ".catdash .catd-ribbon { display: none; }",
            '<div class="catd-tabs" id="catdTabs" role="tablist">',
        ),
        (
            "operations_dashboard.html",
            ".opsdash .opsd-ribbon { display: none; }",
            '<div class="opsd-tabs" id="opsdTabs" role="tablist">',
        ),
        (
            "kitchen_dashboard.html",
            ".kitdash .kitd-ribbon { display: none; }",
            '<div class="kitd-tabs" id="kitdTabs" role="tablist">',
        ),
        (
            "vendors_dashboard.html",
            ".vendash .vend-ribbon { display: none; }",
            '<div class="vend-tabs" id="vendTabs" role="tablist">',
        ),
        (
            "legal_dashboard.html",
            ".legdash .legd-ribbon { display: none; }",
            '<div class="legd-tabs" id="legdTabs" role="tablist">',
        ),
    ]

    for template_name, hidden_rule, *tab_markers in dashboards:
        template = Path(f"app/templates/{template_name}").read_text(encoding="utf-8")

        assert "@media (max-width: 760px)" in template
        assert hidden_rule in template
        for marker in tab_markers:
            assert marker in template


def test_mobile_dashboard_top_navs_use_red_bar_with_gold_tabs():
    dashboards = [
        ("today_dashboard.html", ".tdydash .tdyd-tabs-row", ".tdydash .tdyd-tab"),
        ("manager_dashboard.html", ".mgrdash .mgd-tabs", ".mgrdash .mgd-tab"),
        ("catering_dashboard.html", ".catdash .catd-tabs", ".catdash .catd-tab"),
        ("operations_dashboard.html", ".opsdash .opsd-tabs", ".opsdash .opsd-tab"),
        ("kitchen_dashboard.html", ".kitdash .kitd-tabs", ".kitdash .kitd-tab"),
        ("vendors_dashboard.html", ".vendash .vend-tabs", ".vendash .vend-tab"),
        ("legal_dashboard.html", ".legdash .legd-tabs", ".legdash .legd-tab"),
    ]

    for template_name, nav_selector, tab_selector in dashboards:
        template = Path(f"app/templates/{template_name}").read_text(encoding="utf-8")

        assert "@media (max-width: 760px)" in template
        assert nav_selector in template
        assert "background: linear-gradient(180deg, #940812 0%, #620309 100%);" in template
        assert "position: fixed;" in template
        assert "top: 0;" in template
        assert "margin-top: calc(0px - var(--ck-main-pad-top, 28px));" in template
        assert "flex-direction: column-reverse;" in template
        assert f"{tab_selector} {{" in template
        assert "color: #d4af37;" in template
        assert "text-shadow: 0 0 6px rgba(212, 175, 55, 0.45);" in template
        assert "border-bottom-color: #ffffff;" in template


def test_today_mobile_nav_uses_short_labels():
    template = Path("app/templates/today_dashboard.html").read_text(encoding="utf-8")

    assert "'dashboard': 'Dash'" in template
    assert "'notifications': 'Notice'" in template
    assert "'task-reports': 'Tasks'" in template
    assert 'data-mobile-label="{{ _tdyd_mobile_labels.get(t.key, t.label) }}"' in template
    assert "content: attr(data-mobile-label);" in template


def test_today_menu_button_starts_tab_strip():
    template = Path("app/templates/today_dashboard.html").read_text(encoding="utf-8")

    assert template.count('class="menu-toggle tdyd-menu"') == 1

    title_row_start = template.index('<div class="tdyd-titlerow">')
    title_row_end = template.index('<div class="tdyd-rule">', title_row_start)
    assert "tdyd-menu" not in template[title_row_start:title_row_end]

    row_start = template.index('<div class="tdyd-tabs-row">')
    tab_strip_start = template.index('<div class="tdyd-tabs" id="tdydTabs" role="tablist">', row_start)
    menu_index = template.index('<button class="menu-toggle tdyd-menu"', row_start)
    first_tab_index = template.index("{% for t in tabs %}", tab_strip_start)

    assert row_start < menu_index < tab_strip_start < first_tab_index
    assert ".tdydash .tdyd-tabs-row .tdyd-menu" in template


def test_catering_ez_orders_top_tab_resets_iframe_to_orders_list():
    template = Path("app/templates/catering_dashboard.html").read_text(encoding="utf-8")

    assert "function resetFrameToCanonical(key)" in template
    assert 'frame.getAttribute("data-src")' in template
    assert "frame.contentWindow.location.replace(target)" in template
    assert 'activate(key, { reset: key === "ez-orders" });' in template
