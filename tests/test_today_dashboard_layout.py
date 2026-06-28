from pathlib import Path


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
