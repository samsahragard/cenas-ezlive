"""Playwright tests for samples approval workflow per spec_samples_approval_workflow §12.

Three tests (queued in PLAYWRIGHT_BACKLOG.md per Sam #2547 batch model):
  Test A - test_samples_approve_persists_after_reload:
    Sam-session: click Approve on a card, type notes, click Save.
    Status pill flips to APPROVED. Notes survive page reload.

  Test B - test_samples_attach_zone_drag_drop_chip_visible:
    Sam-session: drag-drop screenshot onto attach zone. Chip with filename
    appears after Save. Persists after reload.

  Test C - test_samples_non_sam_sees_readonly:
    Non-sam (gm) session: approve/reject/save buttons + notes textarea NOT
    in DOM. Read-only status pill visible.

Auth strategy: matches test_drivers_playwright pattern — Flask test client
session_transaction → cookie injection into Playwright context. SAM_CHAT_USER_ID
env var matched against test user id for Sam-only branch.
"""
from __future__ import annotations

import io
import os
import socket
import threading

import pytest
from playwright.sync_api import Page, expect


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def flask_app_with_db_sam():
    """Session-scoped Flask app with an in-memory SQLite DB + a Sam-id
    user (id matches SAM_CHAT_USER_ID env) + a non-Sam gm user for the
    read-only test."""
    os.environ.setdefault("TESTING", "1")
    os.environ.setdefault("PERMISSION_ENFORCE", "0")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Base, User
    from werkzeug.security import generate_password_hash
    import app.db as app_db

    engine = create_engine("sqlite:///:memory:", future=True, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    sess = Session()
    app_db.SessionLocal = Session

    sam_user = User(
        name="Sam (test)",
        phone="5550000001",
        permission_level="partner",
        store_scope="both",
        active=True,
        session_version=1,
        passcode_hash=generate_password_hash("11111"),
    )
    sess.add(sam_user)
    sess.commit()
    os.environ["SAM_CHAT_USER_ID"] = str(sam_user.id)

    other_user = User(
        name="GM Test",
        phone="5550000002",
        permission_level="gm",
        store_scope="tomball",
        active=True,
        session_version=1,
        passcode_hash=generate_password_hash("22222"),
    )
    sess.add(other_user)
    sess.commit()

    from app import create_app
    flask_app = create_app()
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="playwright-test-secret",
        WTF_CSRF_ENABLED=False,
        SERVER_NAME=None,
    )

    # Isolate attachment storage to a temp dir
    import tempfile
    tmp = tempfile.mkdtemp(prefix="sample-approval-att-")
    os.environ["SAMPLE_APPROVAL_ATTACHMENTS_DIR"] = tmp

    yield flask_app, sess, sam_user, other_user

    sess.close()
    engine.dispose()


@pytest.fixture(scope="module")
def live_server(flask_app_with_db_sam):
    flask_app, _, _, _ = flask_app_with_db_sam
    port = _free_port()
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", port, flask_app)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _auth_page_for(browser, flask_app, user):
    """Build an authenticated Playwright page for a given user."""
    with flask_app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["user_id"] = user.id
            s["user_session_version"] = user.session_version
            s["partner_auth_ok"] = True
            s.permanent = True
        c.get("/login")
        flask_cookie = None
        for cookie in c.cookie_jar:
            if cookie.name == "session":
                flask_cookie = cookie
                break
    ctx = browser.new_context()
    if flask_cookie:
        ctx.add_cookies([{
            "name": flask_cookie.name,
            "value": flask_cookie.value,
            "domain": "127.0.0.1",
            "path": "/",
        }])
    return ctx, ctx.new_page()


@pytest.fixture
def sam_page(browser, flask_app_with_db_sam):
    flask_app, _, sam_user, _ = flask_app_with_db_sam
    ctx, page = _auth_page_for(browser, flask_app, sam_user)
    yield page
    ctx.close()


@pytest.fixture
def gm_page(browser, flask_app_with_db_sam):
    flask_app, _, _, other_user = flask_app_with_db_sam
    ctx, page = _auth_page_for(browser, flask_app, other_user)
    yield page
    ctx.close()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_samples_approve_persists_after_reload(sam_page: Page, live_server: str):
    """Test A: Sam clicks Approve + types notes + Save → status pill flips +
    notes persist after reload."""
    page = sam_page
    page.goto(f"{live_server}/partner/developer/samples")

    card = page.locator('.sample-card[data-slug="drivers-redesign-v2"]').first
    expect(card).to_be_visible()

    pill = card.locator(".dvs-status-pill").first
    expect(pill).to_have_text("PENDING APPROVAL", use_inner_text=True)

    notes = card.locator('textarea[name="notes"]').first
    notes.fill("Approve — ship it.")

    card.locator('[data-action="approve"]').first.click()

    # Wait for the pill to update via JS
    expect(card.locator(".dvs-status-approved").first).to_be_visible()

    # Reload and assert state persists
    page.goto(f"{live_server}/partner/developer/samples")
    card2 = page.locator('.sample-card[data-slug="drivers-redesign-v2"]').first
    expect(card2.locator(".dvs-status-approved").first).to_be_visible()
    expect(card2.locator('textarea[name="notes"]').first).to_have_value("Approve — ship it.")


def test_samples_attach_zone_chip_visible_after_save(sam_page: Page, live_server: str):
    """Test B: Sam attaches a screenshot, clicks Save, chip appears with
    filename. Persists after reload."""
    page = sam_page
    page.goto(f"{live_server}/partner/developer/samples")
    card = page.locator('.sample-card[data-slug="right-sidebar-plan-v1"]').first
    expect(card).to_be_visible()

    # Inject a small PNG into the hidden file input
    fi = card.locator('input[type="file"]').first
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63600000000200015f9b3c0a0000000049454e44ae426082"
    )
    fi.set_input_files({"name": "correction.png", "mimeType": "image/png", "buffer": png_bytes})

    card.locator('[data-action="notes"]').first.click()

    chip = card.locator(".dvs-attach-chip", has_text="correction.png").first
    expect(chip).to_be_visible(timeout=5000)

    # Reload and assert chip persists
    page.goto(f"{live_server}/partner/developer/samples")
    card2 = page.locator('.sample-card[data-slug="right-sidebar-plan-v1"]').first
    expect(card2.locator(".dvs-attach-chip", has_text="correction.png").first).to_be_visible()


def test_samples_non_sam_sees_readonly(gm_page: Page, live_server: str):
    """Test C: non-Sam (gm) session — approve/reject/save buttons + notes
    textarea NOT in DOM. Read-only display + status pill visible."""
    page = gm_page
    page.goto(f"{live_server}/partner/developer/samples")
    card = page.locator('.sample-card[data-slug="drivers-redesign-v2"]').first
    expect(card).to_be_visible()

    # Status pill IS visible
    expect(card.locator(".dvs-status-pill").first).to_be_visible()

    # Sam-only controls are NOT in the DOM for non-sam viewers
    assert card.locator('[data-action="approve"]').count() == 0
    assert card.locator('[data-action="reject"]').count() == 0
    assert card.locator('[data-action="notes"]').count() == 0
    assert card.locator('textarea[name="notes"]').count() == 0
    assert card.locator('.dvs-attach-zone').count() == 0
