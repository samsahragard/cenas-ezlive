"""SA-3 self-test: shared canvas engine + Map Setup tab (docs/floor_contract.md
sections 4, 5, 7, 8, 9).

Covers (per the SA-3 verification contract):
- sections_map.html Jinja-renders with the frozen context and carries the
  frozen root element / data attributes;
- all SA-3 static assets exist and are non-empty;
- every fixture key map.js fetches exists in the frozen mock_fixture.json;
- canvas.js carries the frozen palette / API names / exact cover-count text;
- sections.css rules are all scoped under .floor-app (nothing leaks).
"""
from __future__ import annotations

import json
import os
import re

import pytest
from flask import Flask, render_template

WT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMPLATES = os.path.join(WT, "app", "templates")
STATIC = os.path.join(WT, "app", "static")
SECTIONS = os.path.join(STATIC, "sections")

FROZEN_CONTEXT = {
    "store_slug": "uno",
    "active_tab": "map",
    "locations_json": json.dumps(
        [
            {"slug": "uno", "key": "copperfield", "label": "Copperfield"},
            {"slug": "dos", "key": "tomball", "label": "Tomball"},
        ]
    ),
    "loc_default": "uno",
    "is_manager": True,
    "attention_minutes": 90,
    "user_name": "",
}


def _read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture()
def map_html() -> str:
    app = Flask(__name__, template_folder=TEMPLATES, static_folder=STATIC)
    with app.test_request_context("/floor/uno/sections?tab=map&mock=1"):
        return render_template("sections_map.html", **FROZEN_CONTEXT)


# ---------------------------------------------------------------- template


def test_map_template_renders_root_element_and_data_attributes(map_html):
    html = map_html
    assert 'id="floorApp"' in html
    assert 'class="floor-app"' in html
    assert 'data-store="uno"' in html
    assert 'data-active-tab="map"' in html
    assert 'data-loc-default="uno"' in html
    assert 'data-is-manager="1"' in html
    assert 'data-attention-minutes="90"' in html
    # locations_json lands in data-locations (autoescaped quotes are fine)
    assert "data-locations=" in html
    assert "copperfield" in html and "tomball" in html
    # shell + panel hosts
    assert 'id="floorShell"' in html
    assert 'id="floorPanel"' in html


def test_map_template_links_all_sa3_assets(map_html):
    html = map_html
    for asset in (
        "sections/sections.css",
        "sections/map.css",
        "sections/canvas.js",
        "sections/map.js",
    ):
        assert asset in html, f"missing asset link: {asset}"


def test_map_template_is_standalone_and_has_tool_ids(map_html):
    html = map_html
    assert html.lstrip().startswith("<!DOCTYPE html>")
    # no dashboard chrome
    assert "base_dashboard" not in html
    for el_id in (
        "mapTray",
        "mapSave",
        "mapStatus",
        "mapRotate",
        "mapRemove",
        "mapDrawWall",
        "mapDrawLabel",
        "mapShape-square",
        "mapShape-rect",
        "mapShape-circle",
        "mapShape-diamond",
    ):
        assert f'id="{el_id}"' in html, f"missing element id: {el_id}"


def test_map_tab_hidden_for_non_manager_handled_by_shell(map_html):
    # the template itself never hardcodes manager-only markup; the shell does
    # the hiding from data-is-manager. Non-manager render must still work.
    app = Flask(__name__, template_folder=TEMPLATES, static_folder=STATIC)
    ctx = dict(FROZEN_CONTEXT, is_manager=False)
    with app.test_request_context("/floor/uno/sections?tab=map"):
        html = render_template("sections_map.html", **ctx)
    assert 'data-is-manager="0"' in html


# ------------------------------------------------------------ static files


def test_static_assets_exist_and_are_non_empty():
    for name in ("sections.css", "canvas.js", "map.js", "map.css", "mock_fixture.json"):
        path = os.path.join(SECTIONS, name)
        assert os.path.isfile(path), f"missing static asset: {name}"
        assert os.path.getsize(path) > 0, f"empty static asset: {name}"


# ------------------------------------------------------------ mock fixture


def test_fixture_keys_fetched_by_map_js_exist():
    fixture = json.loads(_read(SECTIONS, "mock_fixture.json"))
    # map.js resolves reads by key "floor" (contract section 9)
    assert "floor" in fixture
    floor = fixture["floor"]
    for key in ("tables", "unplaced", "fixtures", "service_areas"):
        assert key in floor, f"floor fixture missing key: {key}"
    for t in floor["tables"]:
        for key in ("guid", "name", "service_area_guid", "x", "y", "w", "h", "shape", "rotation"):
            assert key in t, f"placed table missing key: {key}"
    for t in floor["unplaced"]:
        for key in ("guid", "name"):
            assert key in t, f"unplaced table missing key: {key}"
    for f in floor["fixtures"]:
        for key in ("type", "x", "y", "w", "h", "rotation", "label"):
            assert key in f, f"fixture missing key: {key}"


def test_fixture_has_all_contract_top_level_keys():
    fixture = json.loads(_read(SECTIONS, "mock_fixture.json"))
    for key in (
        "floor",
        "employees",
        "employees_on_shift",
        "sections",
        "live",
        "reservations",
        "waitlist",
        "history",
    ):
        assert key in fixture, f"mock fixture missing contract key: {key}"


# ---------------------------------------------------------------- canvas.js


def test_canvas_js_palette_matches_frozen_backend_palette():
    from app.floor_models import FLOOR_PALETTE

    js = _read(SECTIONS, "canvas.js")
    # all 8 hexes present, in contract order
    last = -1
    for entry in FLOOR_PALETTE:
        pos = js.find(entry["hex"])
        assert pos != -1, f"palette hex missing from canvas.js: {entry['hex']}"
        assert pos > last, f"palette order broken at {entry['key']}"
        last = pos
        assert f'"{entry["key"]}"' in js


def test_canvas_js_exposes_contract_api_names():
    js = _read(SECTIONS, "canvas.js")
    for name in (
        "window.FloorApp",
        "PALETTE",
        "initials",
        "Shell",
        "Canvas",
        "mount",
        "canvasHost",
        "currentLoc",
        "currentArea",
        "setServers",
        "setAreas",
        "setBadge",
        "setFloor",
        "setTableStates",
        "filterArea",
        "setSelected",
        "tableTap",
        "lassoSelect",
        "change",
        "addTable",
        "getLayout",
        "getFixtures",
        "startFixtureDraw",
        "rotateSelected",
        "setShapeOfSelected",
        "removeSelected",
    ):
        assert name in js, f"canvas.js missing contract API name: {name}"


def test_canvas_js_frozen_visual_constants():
    js = _read(SECTIONS, "canvas.js")
    assert '"0 0 1000 620"' in js or "0 0 \" + FLOOR_W + \" \" + FLOOR_H" in js
    for hexcode in ("#FFFFFF", "#111317", "#F5B81C", "#6B7280", "#E8E8E8"):
        assert hexcode in js, f"frozen visual color missing: {hexcode}"
    # EXACT cover-count text pattern: "<live> live | <today> today"
    assert '" live | "' in js
    assert '" today"' in js


# -------------------------------------------------------------- sections.css


def _top_level_selectors(css: str):
    """Yield top-level selector strings (handles one level of @media nesting;
    sections.css intentionally has no @keyframes)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    i, n = 0, len(css)
    buf = []
    depth = 0
    while i < n:
        ch = css[i]
        if ch == "{":
            sel = "".join(buf).strip()
            buf = []
            if sel.startswith("@media") or sel.startswith("@supports"):
                depth += 1  # recurse into the block at the same scanner
            else:
                if sel:
                    yield sel
                # skip the declaration block
                level = 1
                i += 1
                while i < n and level:
                    if css[i] == "{":
                        level += 1
                    elif css[i] == "}":
                        level -= 1
                    i += 1
                continue
        elif ch == "}":
            if depth:
                depth -= 1
            buf = []
        else:
            buf.append(ch)
        i += 1


def test_sections_css_every_rule_scoped_under_floor_app():
    css = _read(SECTIONS, "sections.css")
    selectors = list(_top_level_selectors(css))
    assert selectors, "no selectors parsed from sections.css"
    for sel in selectors:
        for part in sel.split(","):
            assert ".floor-app" in part, f"unscoped sections.css selector: {part.strip()}"


def test_sections_css_defines_shared_class_api_and_tokens():
    css = _read(SECTIONS, "sections.css")
    for cls in (
        ".floor-card",
        ".floor-btn",
        ".floor-btn--ghost",
        ".floor-btn--danger",
        ".floor-pill",
        ".floor-pill--upcoming",
        ".floor-pill--confirmed",
        ".floor-pill--arrived",
        ".floor-pill--seated",
        ".floor-pill--no_show",
        ".floor-pill--cancelled",
        ".floor-pill--left",
        ".floor-pill--waiting",
        ".floor-pill--notified",
        ".floor-sheet",
        ".floor-input",
        ".floor-stepper",
        ".floor-avatar",
        ".floor-list-row",
        ".floor-subtabs",
        ".floor-subtab",
        ".floor-subtab.active",
        ".floor-search",
        ".floor-datepager",
    ):
        assert cls in css, f"sections.css missing shared API class: {cls}"
    # frozen design tokens
    for token in ("#111317", "#1C1F24", "#2F3338", "#9AA0A8", "#2F6FED", "14px"):
        assert token in css, f"sections.css missing frozen token: {token}"
    # mobile/desktop split breakpoint
    assert "min-width: 768px" in css


# ------------------------------------------------------------------- map.js


def test_map_js_uses_contract_endpoints_and_mock_key():
    js = _read(SECTIONS, "map.js")
    assert "/static/sections/mock_fixture.json" in js
    assert "/floor/api/floor" in js
    assert "/floor/api/layout" in js
    assert "/floor/api/fixtures" in js
    assert "mock" in js
    # mock reads resolve by the "floor" key
    assert "data.floor" in js or '["floor"]' in js
