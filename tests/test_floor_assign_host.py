"""SA-4 self-tests: Assign + Host tab templates, static assets, and the
mock-fixture key mapping (docs/floor_contract.md sections 7, 9, 10).

Templates are rendered with a plain jinja2 Environment over app/templates
(the app factory is not needed) using the frozen Gate-0 context.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import jinja2
import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = ROOT / "app" / "templates"
SECTIONS_DIR = ROOT / "app" / "static" / "sections"

# Slot marker + live include (the contract section 7 comment was swapped for
# the real include at Gate 2 by the orchestrator).
RESERVE_SLOT_MARKER = "FLOOR_RESERVE_PANEL_INCLUDE_SLOT"
RESERVE_SLOT_INCLUDE = '{% include "sections_reserve_panel.html" %}'

FROZEN_CONTEXT = {
    "store_slug": "uno",
    "active_tab": "assign",
    "locations_json": json.dumps(
        [
            {"slug": "uno", "key": "copperfield", "label": "Copperfield"},
            {"slug": "dos", "key": "tomball", "label": "Tomball"},
        ]
    ),
    "loc_default": "uno",
    "is_manager": True,
    "attention_minutes": 90,
    "user_name": "Test Manager",
}

SA4_STATIC_FILES = ["assign.js", "assign.css", "host.js", "host.css"]


def _fake_url_for(endpoint, **kwargs):
    if endpoint == "static":
        return "/static/" + kwargs.get("filename", "")
    return "/" + endpoint


def _env() -> jinja2.Environment:
    loader = jinja2.ChoiceLoader(
        [
            jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
            # SA-5's partial may not be on disk while lanes build in
            # parallel; the slot comment in sections_host.html still runs
            # the include at render time, so give Jinja a fallback.
            jinja2.DictLoader(
                {"sections_reserve_panel.html": "<!-- reserve stub -->"}
            ),
        ]
    )
    env = jinja2.Environment(loader=loader, autoescape=True)
    env.globals["url_for"] = _fake_url_for
    return env


def _render(template_name: str, **overrides) -> str:
    ctx = dict(FROZEN_CONTEXT)
    ctx.update(overrides)
    return _env().get_template(template_name).render(**ctx)


def _read(relpath: Path) -> str:
    return relpath.read_text(encoding="utf-8")


# ------------------------------------------------------------- templates --


def test_assign_template_renders_root_contract():
    html = _render("sections_assign.html")
    assert 'id="floorApp"' in html
    assert 'class="floor-app"' in html
    assert 'data-store="uno"' in html
    assert 'data-active-tab="assign"' in html
    assert "data-locations=" in html and "uno" in html and "dos" in html
    assert 'data-loc-default="uno"' in html
    assert 'data-is-manager="1"' in html
    assert 'data-attention-minutes="90"' in html
    assert 'id="floorShell"' in html
    assert 'id="floorPanel"' in html


def test_assign_template_non_manager_flag():
    html = _render("sections_assign.html", is_manager=False)
    assert 'data-is-manager="0"' in html


def test_assign_asset_tags_present_and_ordered():
    html = _render("sections_assign.html")
    assert "/static/sections/sections.css" in html
    assert "/static/sections/assign.css" in html
    canvas_at = html.index("/static/sections/canvas.js")
    assign_at = html.index("/static/sections/assign.js")
    assert canvas_at < assign_at, "canvas.js (engine) must load before assign.js"


def test_host_template_renders_root_contract():
    html = _render("sections_host.html", active_tab="host")
    assert 'id="floorApp"' in html
    assert 'data-store="uno"' in html
    assert 'data-active-tab="host"' in html
    assert 'data-loc-default="uno"' in html
    assert 'data-is-manager="1"' in html
    assert 'data-attention-minutes="90"' in html
    assert 'id="floorShell"' in html
    assert 'id="floorPanel"' in html
    # wrapper for the reservations panel slot
    assert 'id="hostReserveSlot"' in html


def test_host_asset_tags_present_and_ordered():
    html = _render("sections_host.html", active_tab="host")
    assert "/static/sections/sections.css" in html
    assert "/static/sections/host.css" in html
    canvas_at = html.index("/static/sections/canvas.js")
    host_at = html.index("/static/sections/host.js")
    assert canvas_at < host_at, "canvas.js (engine) must load before host.js"


def test_host_reserve_slot_wired_live():
    """Gate-2 state: the slot marker remains AND the include is live (not
    inside an HTML comment) so the reservations panel renders in the page."""
    src = _read(TEMPLATES_DIR / "sections_host.html")
    assert RESERVE_SLOT_MARKER in src
    assert RESERVE_SLOT_INCLUDE in src
    slot_at = src.index(RESERVE_SLOT_INCLUDE)
    assert "<!--" not in src[max(0, slot_at - 120):slot_at], (
        "include must not be commented out"
    )


# ---------------------------------------------------------------- statics --


@pytest.mark.parametrize("name", SA4_STATIC_FILES)
def test_static_files_exist_and_non_empty(name):
    p = SECTIONS_DIR / name
    assert p.exists(), f"{name} missing"
    assert p.stat().st_size > 0, f"{name} is empty"


# ------------------------------------------------------ fixture key wiring --


def _mock_key_values(js_text: str, js_name: str):
    m = re.search(r"MOCK_KEY_BY_ENDPOINT\s*=\s*\{(.*?)\}", js_text, re.S)
    assert m, f"MOCK_KEY_BY_ENDPOINT object not found in {js_name}"
    pairs = re.findall(r'"([\w-]+)"\s*:\s*"(\w+)"', m.group(1))
    assert pairs, f"no endpoint->key pairs parsed from {js_name}"
    return pairs


@pytest.mark.parametrize("js_name", ["assign.js", "host.js"])
def test_every_fixture_key_referenced_by_js_exists(js_name):
    fixture = json.loads(_read(SECTIONS_DIR / "mock_fixture.json"))
    js_text = _read(SECTIONS_DIR / js_name)
    for endpoint, key in _mock_key_values(js_text, js_name):
        assert key in fixture, (
            f"{js_name} maps endpoint '{endpoint}' to fixture key '{key}' "
            f"which is missing from mock_fixture.json"
        )


def test_assign_js_reads_required_endpoints():
    js_text = _read(SECTIONS_DIR / "assign.js")
    for path in ['api("/floor")', '"/employees-on-shift?date="', '"/sections?date="']:
        assert path in js_text, f"assign.js missing read of {path}"


def test_host_js_reads_required_endpoints():
    js_text = _read(SECTIONS_DIR / "host.js")
    assert 'api("/live")' in js_text
    for path in ['api("/floor")', '"/sections?date="', '"/employees-on-shift?date="']:
        assert path in js_text, f"host.js missing read of {path}"


# ------------------------------------------------------------- behaviors --


def test_host_live_poll_interval_is_15000():
    js_text = _read(SECTIONS_DIR / "host.js")
    assert re.search(r"LIVE_POLL_MS\s*=\s*15000\b", js_text)
    assert "setInterval(pollLive, LIVE_POLL_MS)" in js_text
    assert "visibilitychange" in js_text


def test_host_party_stepper_bounds():
    js_text = _read(SECTIONS_DIR / "host.js")
    assert re.search(r"PARTY_MIN\s*=\s*1\b", js_text)
    assert re.search(r"PARTY_MAX\s*=\s*20\b", js_text)
    assert re.search(r"PARTY_DEFAULT\s*=\s*2\b", js_text)


def test_host_seat_clear_and_reserve_mount_wiring():
    js_text = _read(SECTIONS_DIR / "host.js")
    assert '"/seat"' in js_text
    assert '"/clear"' in js_text
    assert "window.FloorReserve" in js_text
    assert 'getElementById("floorReservePanel")' in js_text


def test_assign_overwrite_confirm_flow():
    js_text = _read(SECTIONS_DIR / "assign.js")
    assert "Overwrite tonight" in js_text  # confirm sheet copy
    assert '"exists"' in js_text  # 409 envelope code from POST sections
    assert "confirm:" in js_text or '"confirm"' in js_text or "confirm =" in js_text
    assert "save(true)" in js_text  # resend with confirm:true


def test_assign_seat_capacity_heuristic_present():
    js_text = _read(SECTIONS_DIR / "assign.js")
    assert "1600" in js_text  # seats = clamp(round(w*h/1600), 2, 12)
    assert re.search(r"Math\.min\(12,\s*Math\.max\(2,", js_text)
