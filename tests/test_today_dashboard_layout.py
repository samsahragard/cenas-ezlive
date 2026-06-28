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
