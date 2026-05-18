"""Gate 3 — Playwright browser tests for Drivers Phase A (spec §8).

Three tests per spec §8 + charter amendment 7:
  test_drivers_active_tab_default   — /dos/drivers loads with Active tab
                                      highlighted; click Inactive → URL +
                                      tab state flip.
  test_drivers_hamburger_mobile     — 375x667 viewport: hamburger visible
                                      in topbar (not fixed), 48x48, opens
                                      sidebar on click.
  test_drivers_hamburger_desktop    — 1440x900 viewport: hamburger hidden
                                      (display:none or not visible).

Auth strategy: the fixture starts Flask locally on a free port, creates
a gm-level test user via the in-memory SQLite DB, injects a pre-baked
session cookie (via Flask test client session_transaction), and hands the
authenticated Playwright page to each test.  No production credentials
required — fully hermetic.
"""
from __future__ import annotations

import os
import socket
import threading

import pytest
from playwright.sync_api import Page, expect


# ── helpers ──────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def flask_app_with_db():
    """Session-scoped Flask app using an in-memory SQLite DB + a seeded
    gm/tomball test user.  TESTING=True, WTF_CSRF_ENABLED=False."""
    import os
    os.environ.setdefault("TESTING", "1")
    os.environ.setdefault("PERMISSION_ENFORCE", "0")   # log-and-permit in tests

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Base, Driver, User
    from werkzeug.security import generate_password_hash
    import app.db as app_db

    engine = create_engine("sqlite:///:memory:", future=True, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    sess = Session()

    # Patch the global DB session so routes see the same in-memory DB
    app_db.SessionLocal = Session

    # Seed a gm-level test user (store_scope=tomball → /dos/* access)
    u = User(
        name="Test GM",
        phone="5550001234",
        permission_level="gm",
        store_scope="tomball",
        active=True,
        session_version=1,
        passcode_hash=generate_password_hash("11111"),
    )
    sess.add(u)

    # Seed 1 active + 1 inactive driver so the tabs aren't empty
    sess.add(Driver(name="Active Driver",   location="tomball", active=True))
    sess.add(Driver(name="Inactive Driver", location="tomball", active=False))
    sess.commit()

    from app import create_app
    flask_app = create_app()
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="playwright-test-secret",
        WTF_CSRF_ENABLED=False,
        SERVER_NAME=None,
    )

    yield flask_app, sess, u

    sess.close()
    engine.dispose()


@pytest.fixture(scope="module")
def live_server(flask_app_with_db):
    """Start Flask on a free port in a daemon thread; yield the base URL."""
    flask_app, _, _ = flask_app_with_db
    port = _free_port()
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", port, flask_app)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture
def auth_page(browser, live_server, flask_app_with_db):
    """Playwright page pre-authenticated as the test GM user.

    Uses Flask test client to get a real (signed) session cookie, then
    injects it into a new Playwright browser context so every navigation
    in that context is authenticated."""
    flask_app, _, test_user = flask_app_with_db

    # Get a signed session cookie via the Flask test client
    with flask_app.test_client() as c:
        with c.session_transaction() as sess:
            sess["auth_ok"]              = True
            sess["user_id"]              = test_user.id
            sess["user_session_version"] = test_user.session_version
            sess.permanent               = True
        # Any request will now bake the session into the response cookies
        c.get("/login")          # light GET so the response carries the cookie
        cookie_jar = c.cookie_jar
        # Extract the Flask session cookie (named "session" by default)
        flask_cookie = None
        for cookie in cookie_jar:
            if cookie.name == "session":
                flask_cookie = cookie
                break

    ctx = browser.new_context()
    if flask_cookie:
        ctx.add_cookies([{
            "name":   flask_cookie.name,
            "value":  flask_cookie.value,
            "domain": "127.0.0.1",
            "path":   "/",
        }])
    page = ctx.new_page()
    yield page
    ctx.close()


# ── tests ─────────────────────────────────────────────────────────────────────

def test_drivers_active_tab_default(auth_page: Page, live_server: str):
    """Spec §8 Test A — Active tab is highlighted on default load;
    clicking Inactive flips both tab states + URL."""
    page = auth_page
    page.goto(f"{live_server}/dos/drivers")

    # Both tabs must be present
    active_tab   = page.locator(".da-tab", has_text="Active").first
    inactive_tab = page.locator(".da-tab", has_text="Inactive").first
    expect(active_tab).to_be_visible()
    expect(inactive_tab).to_be_visible()

    # Active tab has .is-active class; Inactive does not
    expect(active_tab).to_have_class(".*is-active.*")
    expect(inactive_tab).not_to_have_class(".*is-active.*")

    # Click Inactive tab
    inactive_tab.click()
    page.wait_for_url("**/dos/drivers?status=inactive")

    # After navigation: Inactive is active, Active is not
    expect(page.locator(".da-tab", has_text="Inactive").first).to_have_class(".*is-active.*")
    expect(page.locator(".da-tab", has_text="Active").first).not_to_have_class(".*is-active.*")


def test_drivers_hamburger_mobile(auth_page: Page, live_server: str):
    """Spec §8 Test B — 375x667 viewport: hamburger is visible INSIDE the
    topbar (not position:fixed), measures ~48px, opens sidebar on click."""
    page = auth_page
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(f"{live_server}/dos/drivers")

    btn = page.locator("button.menu-toggle").first

    # Hamburger must be visible at mobile size
    expect(btn).to_be_visible()

    # Must NOT carry position:fixed (it's now inside the topbar flex row)
    position = page.evaluate(
        "btn => window.getComputedStyle(btn).position",
        btn.element_handle(),
    )
    assert position != "fixed", f"menu-toggle must not be position:fixed; got {position!r}"

    # Computed size must be 48x48 (allow ±4px for sub-pixel rounding)
    box = btn.bounding_box()
    assert box is not None, "bounding_box() returned None — element may not be rendered"
    assert abs(box["width"]  - 48) <= 4, f"expected 48px wide, got {box['width']}"
    assert abs(box["height"] - 48) <= 4, f"expected 48px tall, got {box['height']}"

    # Clicking it should open the sidebar (body.sidebar-open or [data-open] on host)
    btn.click()
    page.wait_for_timeout(300)   # allow CSS transition
    sidebar_open = page.evaluate(
        "() => document.body.classList.contains('sidebar-open') "
        "|| !!document.querySelector('[data-open]')"
    )
    assert sidebar_open, "sidebar should be open after hamburger click"


def test_drivers_hamburger_desktop(auth_page: Page, live_server: str):
    """Spec §8 Test C — 1440x900 viewport: hamburger has display:none."""
    page = auth_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{live_server}/dos/drivers")

    btn = page.locator("button.menu-toggle").first
    display = page.evaluate(
        "btn => window.getComputedStyle(btn).display",
        btn.element_handle(),
    )
    assert display == "none", (
        f"menu-toggle should be display:none on desktop 1440px viewport; got {display!r}"
    )
