"""Cenas Floor Pulse - end-to-end smoke for the 5 employee tabs (REAL data).

Today + Tables hydrate client-side from the existing sanitized, session-scoped
endpoints (/employee/performance-center, /employee/my-performance,
/employee/tables/data). The test employee has NO CenaToastLink, so those
endpoints return linked:false and the pages render the honest "connect Toast"
shell -- which is exactly what we assert here (we can't exercise real Toast
numbers without a live link, but we CAN prove the shell, the wiring, the
honest states, the absence of any demo/placeholder data, and the logout).

The fixture builds an isolated SQLite DB so this file does not leak into the
other employee suites in the same pytest process.
"""
import os
import tempfile

import pytest


@pytest.fixture()
def app_emp():
    tmp = os.path.join(tempfile.gettempdir(), "_floor_os_smoke.db")
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    os.environ["ALLOW_DEV_SECRET"] = "1"
    os.environ["DATABASE_URL"] = "sqlite:///" + tmp.replace("\\", "/")
    from app import create_app
    app = create_app()
    from app.db import SessionLocal
    from app.models import Employee
    db = SessionLocal()
    e = Employee(full_name="Maria Lopez", active=True, session_version=1, passcode_hash="x")
    db.add(e)
    db.commit()
    eid = e.id
    db.close()
    yield app, eid
    try:
        os.remove(tmp)
    except OSError:
        pass


def _login(client, eid):
    with client.session_transaction() as s:
        s.clear()
        s["employee_id"] = eid
        s["auth_ok"] = True


def _assert_shell(html: str):
    """Every tab wears the shared chrome + visual layer + the topbar logout."""
    assert "employee_console.css" in html
    assert "cc-shell" in html
    assert "cc-bottom-tabs" in html
    assert html.count('class="cc-tab') == 6   # Sam 2026-06-13: + Sports tab
    assert 'aria-current="page"' in html
    # Logout reachable from every page (topbar) -- Sam's ask.
    assert 'id="cc-logout"' in html


# No demo/fixture strings may appear on any employee surface anymore.
_FORBIDDEN = ("Demo mode", "demo mode", "Kennya Garcia", "Kristal", "Yadira", "Meher Hayr")


def _assert_no_demo(html: str):
    for token in _FORBIDDEN:
        assert token not in html, f"forbidden demo token leaked: {token!r}"


def test_today_tab_real_wiring_and_no_demo(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/dashboard")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_shell(html)
    _assert_no_demo(html)
    # Wired to the REAL sanitized endpoints (not a demo fixture):
    assert "/employee/performance-center" in html
    assert "/employee/my-performance" in html
    # The V2 hero + the four ranges are present; numbers hydrate client-side.
    assert "cfp-hero-money" in html
    for rng in ('today', 'current_week', 'last_week', 'current_month', 'last_month'):
        assert f'data-range="{rng}"' in html
    # Clickable hero cards target the on-page sections.
    assert 'href="#cfp-earnings"' in html
    assert 'href="#cfp-leaderboard"' in html
    assert 'href="#cfp-technical"' in html
    # Technical averages live on Today now and follow the selected range.
    assert 'id="cfp-technical"' in html
    assert 'id="cfp-tech-scope"' in html
    assert 'id="cfp-tech-detail"' in html
    assert "data-tech-target" in html
    assert 'key:"hours"' in html
    assert 'if(v === null || v === undefined || v === "") return "--";' in html
    assert "Technical averages" in html
    assert "Avg drink" in html
    assert "Performance not available" not in html


def test_today_range_param_selects_tab(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    html = c.get("/employee/dashboard?range=current_week").get_data(as_text=True)
    # The current-week tab is pre-selected server-side (client hydrates the same payload).
    assert 'data-range="current_week"' in html
    assert 'aria-selected="true"' in html


def test_today_legacy_week_range_aliases_to_current_week(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    html = c.get("/employee/dashboard?range=week").get_data(as_text=True)
    assert 'data-range="current_week"' in html
    assert 'class="cfp-seg-btn is-on"' in html


def test_tables_tab_real_wiring_and_no_demo(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/tables")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_shell(html)
    _assert_no_demo(html)
    # Wired to the REAL Toast timelines endpoint.
    assert "/employee/tables/data" in html
    assert "cfp-table-map" in html
    assert "cfp-ticket-list" in html
    # Day toggle present.
    assert "/employee/tables?day=yesterday" in html


def test_shifts_tab_renders_and_no_demo(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/my-schedule")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_shell(html)
    _assert_no_demo(html)
    assert 'id="cf-timeoff"' in html
    assert 'href="/employee/roster"' in html
    assert "Roster" in html


def test_inbox_tab_renders_and_no_demo(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/messages")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_shell(html)
    _assert_no_demo(html)
    assert "/employee/alarm-preferences" in html


def test_you_tab_has_logout_button_and_no_demo(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/my-profile")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_shell(html)
    _assert_no_demo(html)
    assert "Maria" in html  # the real session employee's name reaches the page
    # Both the You-page logout button and its handler are present.
    assert 'id="cf-logout"' in html
    assert "Log out" in html
    assert 'id="cfp-technical"' not in html
    assert "/employee/my-performance" not in html


def test_logout_clears_session(app_emp):
    """POST /employee/logout clears the employee session -> a subsequent tab
    hit bounces to /employee/login."""
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.post("/employee/logout")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}
    with c.session_transaction() as s:
        assert "employee_id" not in s
    # And the gate now bounces.
    after = c.get("/employee/dashboard")
    assert after.status_code == 302
    assert "/employee/login" in (after.headers.get("Location") or "")


def test_each_tab_bounces_logged_out_caller_to_employee_login(app_emp):
    app, _ = app_emp
    c = app.test_client()
    with c.session_transaction() as s:
        s.clear()
    for path in (
        "/employee/dashboard",
        "/employee/tables",
        "/employee/my-schedule",
        "/employee/messages",
        "/employee/my-profile",
    ):
        r = c.get(path)
        assert r.status_code == 302, f"{path} should redirect when logged-out, got {r.status_code}"
        assert "/employee/login" in (r.headers.get("Location") or ""), (
            f"{path} should bounce to employee login, got {r.headers.get('Location')}"
        )
