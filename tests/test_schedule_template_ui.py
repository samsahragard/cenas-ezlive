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
    assert controls.index('id="sv2-tagfilter-wrap"') < spacer
    assert controls.index('id="sv2-viewfilter-wrap"') < spacer
    assert 'id="sv2-today"' not in controls
    assert "This week" not in controls
    assert controls.index('id="sv2-status"') > spacer
    assert '<div class="sv2-filter-row">' in controls
    assert '<div class="sv2-state-row">' in controls
    assert '>Positions</button>' in controls
    assert '>Tags</button>' in controls
    assert '>Options</button>' in controls
    assert "<span>Visible shifts</span>" in controls


def test_schedule_grid_sorts_visible_people_rows_by_first_name():
    template = _read("schedules_v2_week.html")

    assert "function firstNameSort(a, b)" in template
    assert 'staffRows.sort(function (a, b) { return firstNameSort(a.emp, b.emp); });' in template
    assert template.index("staffRows.sort") < template.index(
        'rows += rowHtml(openEmp, "open");'
    )


def test_schedule_template_has_sling_style_tag_filter_and_create_controls():
    template = _read("schedules_v2_week.html")

    assert 'id="sv2-tagfilter-btn"' in template
    assert 'id="sv2-tagfilter-pop"' in template
    assert 'id="sv2-m-tagbox-btn"' in template
    assert 'id="sv2-m-tagbox-menu"' in template
    assert 'id="sv2-m-tag-new"' in template
    assert 'id="sv2-m-tag-add"' in template
    assert "Search or add tag" in template
    assert "state.tagFilter" in template
    assert "function buildTagFilter()" in template
    assert "function renderModalTags()" in template
    assert "function toggleTagDropdown()" in template
    assert "function createTagFromInput(input, selectInModal, selectInFilter)" in template
    assert "state.selTags[id] = true" in template
    assert "state.tagFilter.push(id)" in template
    assert "function shiftTags(s)" in template
    assert "sv2-chip-tag" in template


def test_schedule_template_renders_time_off_markers_on_grid():
    template = _read("schedules_v2_week.html")

    assert "time_off_requests" in template
    assert "unavailability_blocks" in template
    assert "function requestItemsFor(emp, isoDay)" in template
    assert "function requestChipHtml(item, isoDay)" in template
    assert "sv2-request-chip" in template
    assert "Time off -" in template
    assert "Unavailable" in template


def test_schedule_template_has_visible_shift_bulk_select_and_inline_position_tags():
    template = _read("schedules_v2_week.html")

    assert 'id="sv2-select-visible"' in template
    assert "function visibleShiftIds()" in template
    assert "function setVisibleSelected(on)" in template
    assert "function positionNamesForEmployee(emp)" in template
    assert "sv2-chip-pos-name" in template
    assert template.index('id="sv2-selcount"') < template.index('id="sv2-sel-edit"')


def test_schedule_position_filter_groups_roles_by_boh_and_foh():
    template = _read("schedules_v2_week.html")

    assert 'label: "BOH"' in template
    assert '"Assistant KM", "Cook", "Corporate Chef", "Dishwasher", "KM"' in template
    assert 'label: "FOH"' in template
    assert '"Bartender", "Busser", "Cashier", "Corporate", "Expo", "FOH Manager", "GM", "Host", "Partner", "Prep", "Server", "Training", "Well"' in template
    assert "function positionsForArea(positions, area)" in template
    assert "function renderAreaFilter(area, areaPositions)" in template
    assert "function selectedAreaLabel()" in template
    assert "sv2-pf-area" in template
    assert "sv2-posfilter-role" in template


def test_schedule_controls_and_bulk_actions_are_inside_stickybar():
    template = _read("schedules_v2_week.html")

    sticky = template[
        template.index('<div class="sv2-stickybar"'):
        template.index("<!-- position-filter people hint")
    ]
    grid_prefix = template[
        template.index('<div id="sv2-gridwrap"'):
        template.index('<table class="sv2-grid">')
    ]

    assert 'id="sv2-stickybar"' in sticky
    assert 'id="sv2-weeklabel"' in sticky
    assert 'id="sv2-posfilter-btn"' in sticky
    assert 'id="sv2-tagfilter-btn"' in sticky
    assert 'id="sv2-viewfilter-btn"' in sticky
    assert 'id="sv2-select-visible"' in sticky
    assert 'id="sv2-status"' in sticky
    assert 'id="sv2-today"' not in sticky
    assert 'id="sv2-seltoolbar"' in sticky
    assert 'id="sv2-sel-edit"' in sticky
    assert 'id="sv2-sel-copy"' in sticky
    assert 'id="sv2-sel-delete"' in sticky
    assert 'id="sv2-seltoolbar"' not in grid_prefix
    assert "top: var(--sv2-sticky-offset, 0px)" in template
    assert "function updateStickyOffset()" in template


def test_schedule_mobile_toolbar_uses_three_column_rows():
    template = _read("schedules_v2_week.html")

    assert ".sv2-filter-row, .sv2-state-row {" in template
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in template
    assert ".sv2-filter, .sv2-select-visible, .sv2-state-row .sv2-pill, .sv2-publish-slot .sv2-btn" in template
    assert "var todayBtn = $(\"sv2-today\");" in template
    assert 'var n = state.posFilter.length, label = "Positions";' in template
    assert 'var n = state.tagFilter.length, label = "Tags";' in template
    assert 'elViewBtn.textContent = n === 4 ? "Options" : "Options (" + n + ")";' in template


def test_team_roster_controls_are_inside_stickybar():
    template = _read("team_workspace.html")

    sticky = template[
        template.index('<div class="tws-stickybar"'):
        template.index("{# ---- TEAM panel")
    ]
    team_panel = template[
        template.index('<div class="tws-panel" id="tws-panel-team"'):
        template.index("{# ---- SCHEDULE panel")
    ]
    schedule_panel = template[
        template.index('<div class="tws-panel active" id="tws-panel-schedule"'):
        template.index("{# ---- MARKET panel")
    ]

    assert 'id="tws-stickybar"' in sticky
    assert sticky.index('data-sub="schedule"') < sticky.index('data-sub="team"')
    assert 'data-sub="team"' in sticky
    assert 'data-sub="schedule"' in sticky
    assert 'data-sub="market"' in sticky
    assert 'data-sub="link"' in sticky
    assert 'data-sub="schedule-reports"' in sticky
    assert 'data-sub="settings"' in sticky
    assert 'id="tws-pills"' in sticky
    assert 'id="tws-pos-btn"' in sticky
    assert 'id="tws-inactive"' in sticky
    assert 'id="tws-team-storetabs"' in sticky
    assert 'id="tws-team-storemeta"' in sticky
    assert 'id="tws-roster"' in team_panel
    assert 'data-sub-panel="schedule"' in schedule_panel
    assert 'var initial = (new URLSearchParams(location.search)).get("sub") || "schedule";' in template
    assert 'id="tws-pos-btn"' not in team_panel
    assert 'id="tws-team-storetabs"' not in team_panel
    assert "position: sticky; top: 0;" in template
    assert 'root.classList.toggle("team-active", sub === "team");' in template
    assert "function renderActiveStoreMeta()" in template
    assert "var elStoreTabs = document.getElementById(\"tws-team-storetabs\");" in template
    assert "state.storeMeta = metaByStore;" in template


def test_schedule_visible_selection_skips_open_shifts_and_copy_reports_skips():
    template = _read("schedules_v2_week.html")

    assert 'if (sh && sh.employee_id == null) continue;' in template
    assert 'j.skipped_open' in template
    assert 'j.duplicates' in template


def test_schedule_template_has_view_options_for_empty_unpublished_hours_and_conflicts():
    template = _read("schedules_v2_week.html")

    assert 'id="sv2-viewfilter-btn"' in template
    assert 'id="sv2-viewfilter-pop"' in template
    assert 'data-view-option="emptyRows"' in template
    assert 'data-view-option="unpublished"' in template
    assert 'data-view-option="hours"' in template
    assert 'data-view-option="conflicts"' in template
    assert "function buildViewFilter()" in template
    assert "function conflictShiftsFor(emp, isoDay)" in template
    assert "function hasVisibleShifts(emp)" in template
    assert "function shiftHours(s)" in template
    assert "function hoursForWeek(emp)" in template
    assert "sv2-chip-conflict" in template
    assert "state.viewOptions" in template


def test_shift_modal_position_choices_follow_selected_employee_positions():
    template = _read("schedules_v2_week.html")

    assert "function positionChoicesForEmployee(empId, keepPosId)" in template
    assert "emp.position_ids" in template
    assert "emp.onchange = function ()" in template
    assert "fillPositionSelectForEmployee(emp.value" in template


def test_link_tab_sorts_cena_profile_column_before_rendering_each_store_panel():
    template = _read("team_workspace.html")

    assert "function _sortByCenaName(rows, getter)" in template
    assert "_sortByCenaName(uc, function (c) { return c.name; }).forEach" in template
    assert "var linked = _sortByCenaName(d.confirmed_links, function (m) { return m.cena_name; });" in template
    assert "var sugg = _sortByCenaName(d.suggestions, function (m) { return m.cena_name; });" in template
    assert "var uc = _sortByCenaName(d.unmatched_cena, function (c) { return c.name; });" in template


def test_team_workspace_has_schedule_reports_beside_link():
    template = _read("team_workspace.html")

    assert 'data-sub="link"' in template
    assert 'data-sub="schedule-reports"' in template
    assert template.index('data-sub="link"') < template.index('data-sub="schedule-reports"')
    assert 'data-embed-frame="schedule-reports"' in template
    assert 'data-src="{{ schedule_reports_url }}?embed=1"' in template
    assert 'loadFrame("schedule-reports")' in template


def test_market_iframe_skips_auto_height_feedback_loop():
    template = _read("team_workspace.html")

    assert 'var isMarketFrame = frameKey.indexOf("market-") === 0;' in template
    assert ".ck-page-body>.sv2-mk,.sv2-mk{display:block!important;flex:0 0 auto!important" in template
    assert "overflow-x:hidden!important" in template
    assert ".sv2-mk:only-child" not in template
    assert "var isPhoneMarket = frame.getBoundingClientRect && frame.getBoundingClientRect().width <= 560;" in template
    assert 'var EMBED_HEIGHT = "var(--tws-embed-height)";' in template
    assert 'if (!isPhoneMarket) {\n          frame.style.setProperty("height", EMBED_HEIGHT, "important");' in template
    assert "new ResizeObserver(fitMarket).observe(box)" in template
    assert "reveal(frame);\n        return;\n      }\n    }\n    reveal(frame);" in template
    assert ".tws-stickybar .tws-tabs" in template
    assert "flex-wrap: nowrap;" in template
    assert ".tws-stickybar .tws-tab {\n      flex: 1 1 0;" in template
    assert "flex-direction: column;" in template
    assert "min-height: 44px;" in template
    assert ".tws-stickybar .tws-tab::after {\n      content: attr(data-mobile-label);" in template
    assert ".tws-stickybar .tws-tab .ti {\n      display: block;" in template
    assert "#tws-panel-market, #tws-panel-market .tws-store-shell, #tws-panel-market .tws-embed-wrap { max-width: 100%; overflow-x: hidden; }" in template


def test_team_workspace_market_panel_omits_repeated_store_headings():
    template = _read("team_workspace.html")

    market_panel = template[
        template.index('<div class="tws-panel" id="tws-panel-market"'):
        template.index("{# ---- LINK panel")
    ]

    assert 'data-embed-frame="market-{{ st.slug }}"' in market_panel
    assert 'data-src="/{{ st.slug }}/schedules-v2/marketplace?embed=1"' in market_panel
    assert "{% for st in schedule_stores %}" in market_panel
    assert "tws-sched-storehead" not in market_panel


def test_schedule_iframes_skip_body_scrollheight_feedback_loop():
    template = _read("team_workspace.html")

    assert 'var isScheduleFrame = /^(week|timeoff|availability)-/.test(frameKey);' in template
    assert 'var isScheduleReportFrame = frameKey === "schedule-reports";' in template
    assert "if (isScheduleFrame || isScheduleReportFrame)" in template
    assert 'frame.style.setProperty("height", EMBED_HEIGHT, "important");' in template
    assert "--tws-embed-height: clamp(820px, 92vh, 1180px);" in template
    assert "Week Builder, making the page grow forever" in template
    assert "new ResizeObserver(fit).observe(doc.body)" not in template
    assert "doc.body.scrollHeight" not in template
