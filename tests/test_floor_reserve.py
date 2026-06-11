"""SA-5 self-tests: Reservations + Waitlist + History panel.

Verifies (contract docs/floor_contract.md sections 6, 8, 9, 10):
- the Jinja partial renders standalone and carries the frozen root element,
  sub-tab labels, add-reservation button and asset tags;
- the static assets exist, are non-empty, and expose the FloorReserve API;
- every ctx.api path reserve.js calls exists in the frozen contract route
  table (section 6);
- the mock fixture keys the panel consumes (reservations/waitlist/history)
  exist with the documented shapes;
- reserve.css stays .floor-reserve-scoped (no global selectors).

No Flask app import needed: the partial is a standalone include and all
network goes through the host-provided ctx.api.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import jinja2

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"
SECTIONS_STATIC = ROOT / "app" / "static" / "sections"
PARTIAL = TEMPLATES / "sections_reserve_panel.html"
RESERVE_JS = SECTIONS_STATIC / "reserve.js"
RESERVE_CSS = SECTIONS_STATIC / "reserve.css"
FIXTURE = SECTIONS_STATIC / "mock_fixture.json"
CONTRACT = ROOT / "docs" / "floor_contract.md"

# Frozen status enums (contract section 3).
RESERVATION_STATUSES = {"upcoming", "confirmed", "arrived", "seated", "no_show", "cancelled"}
WAITLIST_STATUSES = {"waiting", "notified", "seated", "left"}


def render_partial() -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
    )
    return env.get_template("sections_reserve_panel.html").render()


# --------------------------------------------------------------------------
# Partial markup
# --------------------------------------------------------------------------

def test_partial_renders_standalone():
    html = render_partial()
    assert html.strip(), "partial rendered empty"


def test_partial_root_element():
    html = render_partial()
    assert 'id="floorReservePanel"' in html
    root_match = re.search(r"<div[^>]*id=\"floorReservePanel\"[^>]*>", html)
    assert root_match, "root div missing"
    assert "floor-reserve" in root_match.group(0), "root must carry class floor-reserve"


def test_partial_is_a_partial_not_a_page():
    src = PARTIAL.read_text(encoding="utf-8")
    for forbidden in ("<html", "<head", "<body", "<!DOCTYPE", "{% extends"):
        assert forbidden not in src, f"partial must not contain {forbidden}"


def test_partial_subtab_labels():
    html = render_partial()
    subtabs = re.findall(r"<button[^>]*data-subtab=\"([a-z]+)\"[^>]*>(.*?)</button>", html, re.S)
    by_key = {key: re.sub(r"<[^>]+>", "", body).strip() for key, body in subtabs}
    assert set(by_key) == {"reservations", "waitlist", "history"}
    assert by_key["reservations"].startswith("Reservations")
    assert by_key["waitlist"].startswith("Waitlist")
    assert by_key["history"].startswith("History")
    # live count slots in the labels
    assert 'data-role="count-reservations"' in html
    assert 'data-role="count-waitlist"' in html
    # shared subtab classes from sections.css (SA-3)
    assert "floor-subtabs" in html
    assert "floor-subtab active" in html


def test_partial_add_reservation_button():
    html = render_partial()
    m = re.search(r"<button[^>]*data-role=\"add-reservation\"[^>]*>(.*?)</button>", html, re.S)
    assert m, "add-reservation button missing"
    assert "+ Add reservation" in m.group(1)
    # primary button class (blue primary comes from sections.css floor-btn)
    assert "floor-btn" in m.group(0)


def test_partial_asset_tags():
    html = render_partial()
    assert re.search(r"<script[^>]*src=\"/static/sections/reserve\.js\"", html)
    assert re.search(r"<link[^>]*href=\"/static/sections/reserve\.css\"", html)


def test_partial_search_and_datepager_present():
    html = render_partial()
    assert 'data-role="search"' in html
    assert "floor-search" in html
    assert 'data-role="datepager"' in html
    assert "floor-datepager" in html
    for role in ("date-prev", "date-today", "date-next"):
        assert f'data-role="{role}"' in html


# --------------------------------------------------------------------------
# Static assets
# --------------------------------------------------------------------------

def test_static_assets_exist_and_non_empty():
    for path in (RESERVE_JS, RESERVE_CSS):
        assert path.is_file(), f"{path} missing"
        assert path.stat().st_size > 0, f"{path} empty"


def test_reserve_js_exposes_mount_api():
    src = RESERVE_JS.read_text(encoding="utf-8")
    assert "window.FloorReserve" in src
    assert re.search(r"mount\s*[:(]", src), "mount entry point missing"
    # all network must go through ctx.api - no bare fetch()/XHR
    assert not re.search(r"\bfetch\s*\(", src), "reserve.js must not call fetch directly"
    assert "XMLHttpRequest" not in src


def test_no_emdash_characters_in_owned_files():
    for path in (PARTIAL, RESERVE_JS, RESERVE_CSS, Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert "\u2014" not in text, f"em-dash found in {path.name}"
        assert "\u2013" not in text, f"en-dash found in {path.name}"


# --------------------------------------------------------------------------
# Contract route table conformance
# --------------------------------------------------------------------------

def contract_api_paths() -> set[str]:
    text = CONTRACT.read_text(encoding="utf-8")
    paths = set()
    for m in re.finditer(r"(?:GET|POST|PUT|PATCH)\s+`?(/floor/api/[a-z_]+(?:/<id>)?)", text):
        paths.add(m.group(1))
    assert paths, "could not parse contract route table"
    return paths


def reserve_js_api_paths() -> set[str]:
    src = RESERVE_JS.read_text(encoding="utf-8")
    used = set()
    for m in re.finditer(r"/floor/api/[a-z_]+/?", src):
        p = m.group(0)
        if p.endswith("/"):
            p = p + "<id>"  # string-concat id segment ("/floor/api/x/" + id)
        used.add(p)
    return used


def test_every_api_path_used_is_in_contract():
    contract = contract_api_paths()
    used = reserve_js_api_paths()
    assert used, "reserve.js calls no /floor/api paths?"
    unknown = used - contract
    assert not unknown, f"reserve.js calls paths not in the contract: {sorted(unknown)}"


def test_expected_panel_paths_are_used():
    used = reserve_js_api_paths()
    for required in (
        "/floor/api/reservations",
        "/floor/api/reservations/<id>",
        "/floor/api/waitlist",
        "/floor/api/waitlist/<id>",
        "/floor/api/history",
        "/floor/api/seat",
    ):
        assert required in used, f"panel must call {required}"


def test_reserve_js_never_calls_manager_or_foreign_routes():
    used = reserve_js_api_paths()
    for forbidden in ("/floor/api/layout", "/floor/api/fixtures", "/floor/api/sync",
                      "/floor/api/sections"):
        assert forbidden not in used


# --------------------------------------------------------------------------
# Mock fixture conformance (contract section 9)
# --------------------------------------------------------------------------

def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_has_keys_panel_consumes():
    fix = load_fixture()
    for key in ("reservations", "waitlist", "history"):
        assert key in fix, f"mock_fixture.json missing key {key}"


def test_fixture_reservations_shape():
    fix = load_fixture()
    payload = fix["reservations"]
    assert payload.get("ok") is True
    rows = payload["reservations"]
    assert isinstance(rows, list) and rows
    for r in rows:
        for field in ("id", "guest_name", "phone", "party_size", "reserved_for", "status"):
            assert field in r
        assert r["status"] in RESERVATION_STATUSES


def test_fixture_waitlist_shape():
    fix = load_fixture()
    payload = fix["waitlist"]
    assert payload.get("ok") is True
    rows = payload["waitlist"]
    assert isinstance(rows, list) and rows
    for w in rows:
        for field in ("id", "guest_name", "phone", "party_size", "quoted_minutes",
                      "joined_at", "status"):
            assert field in w
        assert w["status"] in WAITLIST_STATUSES


def test_fixture_history_shape():
    fix = load_fixture()
    payload = fix["history"]
    assert payload.get("ok") is True
    for group in ("seatings", "reservations", "waitlist"):
        assert isinstance(payload.get(group), list), f"history missing group {group}"
    for s in payload["seatings"]:
        for field in ("seating_id", "table_guid", "table_name", "party_size",
                      "seated_at", "server_name"):
            assert field in s


# --------------------------------------------------------------------------
# CSS scoping (contract: reserve.css adds ONLY .floor-reserve-scoped rules)
# --------------------------------------------------------------------------

def css_top_level_selectors(text: str) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    selectors: list[str] = []
    depth = 0
    buf = ""
    in_at = False
    for ch in text:
        if ch == "{":
            sel = buf.strip()
            if depth == 0 and sel.startswith("@"):
                in_at = sel.startswith("@keyframes")
                if not sel.startswith(("@media", "@keyframes")):
                    selectors.append(sel)
            elif not in_at:
                if sel:
                    selectors.append(sel)
            depth += 1
            buf = ""
        elif ch == "}":
            depth -= 1
            if depth == 0:
                in_at = False
            buf = ""
        else:
            buf += ch
    return selectors


def test_reserve_css_is_floor_reserve_scoped():
    selectors = css_top_level_selectors(RESERVE_CSS.read_text(encoding="utf-8"))
    assert selectors, "no selectors parsed from reserve.css"
    for sel in selectors:
        for part in sel.split(","):
            part = part.strip()
            assert part.startswith(".floor-reserve"), (
                f"selector not scoped under .floor-reserve: {part!r}"
            )


# --------------------------------------------------------------------------
# Gate 4 (ck): duplicate-confirm flow, history days toggle, host badge wiring
# (contract section 12)
# --------------------------------------------------------------------------

HOST_JS = SECTIONS_STATIC / "host.js"


def test_reserve_js_duplicate_confirm_flow():
    src = RESERVE_JS.read_text(encoding="utf-8")
    # the 409 envelope code is recognized
    assert '"duplicate"' in src
    # confirm-step copy + its action button markup
    assert "Looks like a duplicate booking - add anyway?" in src
    assert 'data-action="confirm-duplicate"' in src
    # the resend path flips confirm:true on the held payload
    assert re.search(r"confirm\s*=\s*true", src), "resend must set confirm:true"
    # the held payload is dropped once consumed or the sheet closes
    assert "pendingReservation" in src


def test_reserve_js_history_days_toggle():
    src = RESERVE_JS.read_text(encoding="utf-8")
    # toggle button present in the History sub-tab rendering
    assert 'data-action="toggle-history-days"' in src
    assert 'data-role="history-days-toggle"' in src
    assert "Last 7 days" in src
    # fetch carries days=N only in multi-day mode; toggle flips 1 <-> 7
    assert re.search(r"&days=", src), "history fetch must pass days=N"
    assert re.search(r"historyDays\s*>\s*1\s*\?\s*1\s*:\s*7", src)
    # bucketed {ok, days:[...]} payload shape is handled
    assert re.search(r"\.days\b", src), "renderer must handle the days buckets"


def test_host_js_wires_reservation_badge():
    # Gate 4 surface check: host.js consumes shell.setBadge('host', n) from
    # the live payload's reservation_badge on every poll (n=0 hides).
    src = HOST_JS.read_text(encoding="utf-8")
    assert re.search(r"setBadge\(\s*[\"']host[\"']\s*,", src)
    assert "reservation_badge" in src
