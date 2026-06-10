from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "templates"


def _read(path: str) -> str:
    return (TEMPLATES / path).read_text(encoding="utf-8")


def test_schedule_grid_uses_lighter_blue_cells_and_swapped_toolbar_controls():
    template = _read("schedules_v2_week.html")

    assert "background: rgba(145, 190, 225, 0.14);" in template
    assert ".sv2-cell:hover { background: rgba(165, 205, 238, .22); }" in template
    assert ".sv2-cell.is-today { background: rgba(154, 201, 242, .24); }" in template

    controls = template[
        template.index('<div class="sv2-controls">'):
        template.index("<!-- position-filter people hint")
    ]
    spacer = controls.index('<div class="sv2-spacer"></div>')
    assert controls.index('id="sv2-posfilter-wrap"') < spacer
    assert controls.index('id="sv2-today"') < spacer
    assert controls.index('id="sv2-status"') > spacer


def test_schedule_grid_sorts_visible_people_rows_by_first_name():
    template = _read("schedules_v2_week.html")

    assert "function firstNameSort(a, b)" in template
    assert 'staffRows.sort(function (a, b) { return firstNameSort(a.emp, b.emp); });' in template
    assert template.index("staffRows.sort") < template.index(
        'rows += rowHtml({ id: null, full_name: "Open shifts" }, "open");'
    )


def test_link_tab_sorts_cena_profile_column_before_rendering_each_store_panel():
    template = _read("team_workspace.html")

    assert "function _sortByCenaName(rows, getter)" in template
    assert "_sortByCenaName(uc, function (c) { return c.name; }).forEach" in template
    assert "var linked = _sortByCenaName(d.confirmed_links, function (m) { return m.cena_name; });" in template
    assert "var sugg = _sortByCenaName(d.suggestions, function (m) { return m.cena_name; });" in template
    assert "var uc = _sortByCenaName(d.unmatched_cena, function (c) { return c.name; });" in template
