"""Block 1B — universal ribbon component tests.

Phase 2 / Block 1 / sub-block 1B (ck, 2026-05-14). Coverage per the
1B spec §9:

  1. ribbon_render_context against the stub router → seven empty
     categories, fixed order, no error.
  2. ribbon_render_context groups real render-contract items into the
     correct category buckets; X/Check flags + styling_class carry
     through; an item with an unknown category is dropped (not fatal);
     an item whose render_for() raises is dropped (not fatal).
  3. THE MOST IMPORTANT TEST (spec §9): a router that RAISES degrades
     to an empty ribbon — ribbon_render_context never propagates the
     exception, so the partial it feeds can't 500 the page it's
     embedded in. Asserted both at the wrapper level and through an
     actual render_template() of the partial.
  4. The collapse-toggle endpoint: first POST creates a collapsed
     row, second POST flips it back, invalid category → 400,
     unauthenticated → redirect to keypad login.
  5. The partial renders all seven category headers + empty-states
     against the stub, and renders the §6.2 X/Check markup contract
     exactly when fed items.

These run cold — in-memory SQLite via the db_session / ribbon_db
fixtures, no real DB, no network. ribbon_db additionally binds the
global SessionLocal (which ribbon_routes.py + ribbon.py capture via a
module-level `from app.db import SessionLocal`) at the test session,
so the suite is hermetic whether or not a local .env sets DATABASE_URL
— the local-vs-CI discrepancy that made five of these tests pass
locally but fail on a clean CI checkout.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import g, render_template

from app.services.ribbon import (
    RIBBON_CATEGORIES,
    RIBBON_CATEGORY_SLUGS,
    ribbon_items_for,
)
from app.web import ribbon_routes
from app.web.ribbon_routes import ribbon_render_context
from app.models import RibbonCategoryPreference


# ---- fixtures + helpers ----

@pytest.fixture(scope="session")
def app():
    """Session-scoped Flask app. Permission/ribbon checking is
    stateless across requests so one instance is fine."""
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def ribbon_db(db_session, monkeypatch):
    """Bind the global SessionLocal at the in-memory test session.

    app/db.py builds SessionLocal at import time from DATABASE_URL; on a
    clean checkout with no .env (CI) DATABASE_URL is unset, so
    app.db.SessionLocal is None. ribbon_routes.py AND ribbon.py each did
    `from app.db import SessionLocal` at module load — capturing that
    value into their own namespace — so ribbon_render_context's
    collapse-pref query and the real ribbon_items_for's source queries
    both hit a None and the ribbon silently degrades to empty. Patching
    the name in both modules makes these tests hermetic: they pass
    identically whether or not a local .env happens to set DATABASE_URL.
    Returns the session so a test can seed rows into the same in-memory
    DB the code under test reads."""
    monkeypatch.setattr("app.web.ribbon_routes.SessionLocal",
                        lambda: db_session)
    monkeypatch.setattr("app.services.ribbon.SessionLocal",
                        lambda: db_session)
    return db_session


def _make_user(user_id=1, role="gm", store_scope="tomball"):
    return SimpleNamespace(
        id=user_id, full_name=f"Test {role}",
        permission_level=role, store_scope=store_scope, active=True,
    )


class _FakeRibbonItem:
    """Stand-in for 1C's RibbonItem — implements the §2 render
    contract. ribbon_render_context only touches these attributes +
    render_for(), so we ducktype rather than importing 1C's class
    (which doesn't exist yet — 1B builds against the contract)."""

    def __init__(self, category, *, severity="info", item_type="task",
                 item_id=1, can_dismiss=True, can_check=True,
                 text="Test item", sub_text=None,
                 styling_class="ribbon-item--info", render_raises=False):
        self.category = category
        self.severity = severity
        self.item_type = item_type
        self.item_id = item_id
        self.deadline_at = None
        self.can_dismiss = can_dismiss
        self.can_check = can_check
        self._text = text
        self._sub_text = sub_text
        self._styling_class = styling_class
        self._render_raises = render_raises

    def render_for(self, user):
        if self._render_raises:
            raise RuntimeError("render_for boom")
        return {
            "text": self._text,
            "sub_text": self._sub_text,
            "styling_class": self._styling_class,
        }


# ============================================================
# 1. ribbon_render_context — against the stub
# ============================================================

def test_render_context_stub_returns_seven_empty_categories(app, ribbon_db):
    """The stub ribbon_items_for returns [] — render context should be
    seven categories, fixed order, all empty, all expanded."""
    with app.test_request_context("/"):
        cats = ribbon_render_context(_make_user(), "dashboard", "tomball")
    assert [c["slug"] for c in cats] == [s for s, _ in RIBBON_CATEGORIES]
    assert all(c["count"] == 0 for c in cats)
    assert all(c["entries"] == [] for c in cats)
    assert all(c["is_collapsed"] is False for c in cats)


# ============================================================
# 2. ribbon_render_context — grouping + per-item resilience
# ============================================================

def test_render_context_groups_items_into_categories(app, ribbon_db, monkeypatch):
    items = [
        _FakeRibbonItem("todo", item_id=1, text="Place SPECS order"),
        _FakeRibbonItem("todo", item_id=2, text="Call vendor"),
        _FakeRibbonItem("vendors", item_id=3, text="Invoice variance"),
        _FakeRibbonItem("sales", item_id=4, text="95F today",
                        can_dismiss=False, can_check=True),
    ]
    monkeypatch.setattr(
        "app.services.ribbon.ribbon_items_for",
        lambda page_slug, user, store_scope, category=None: items,
    )
    with app.test_request_context("/"):
        cats = ribbon_render_context(_make_user(), "dashboard", "tomball")
    by_slug = {c["slug"]: c for c in cats}
    assert by_slug["todo"]["count"] == 2
    assert by_slug["vendors"]["count"] == 1
    assert by_slug["sales"]["count"] == 1
    assert by_slug["employee"]["count"] == 0
    # Per-item payload carried through.
    todo_first = by_slug["todo"]["entries"][0]
    assert todo_first["item_type"] == "task"
    assert todo_first["item_id"] == 1
    assert todo_first["text"] == "Place SPECS order"
    assert todo_first["can_dismiss"] is True
    # The sales item had can_dismiss=False.
    assert by_slug["sales"]["entries"][0]["can_dismiss"] is False
    assert by_slug["sales"]["entries"][0]["can_check"] is True


def test_render_context_drops_item_with_unknown_category(app, ribbon_db, monkeypatch):
    items = [
        _FakeRibbonItem("todo", item_id=1),
        _FakeRibbonItem("bogus_category", item_id=2),
    ]
    monkeypatch.setattr(
        "app.services.ribbon.ribbon_items_for",
        lambda page_slug, user, store_scope, category=None: items,
    )
    with app.test_request_context("/"):
        cats = ribbon_render_context(_make_user(), "dashboard", "tomball")
    by_slug = {c["slug"]: c for c in cats}
    # The bad item is dropped; the good one survives.
    assert by_slug["todo"]["count"] == 1
    assert sum(c["count"] for c in cats) == 1


def test_render_context_drops_item_whose_render_for_raises(app, ribbon_db, monkeypatch):
    items = [
        _FakeRibbonItem("todo", item_id=1, text="good"),
        _FakeRibbonItem("todo", item_id=2, render_raises=True),
        _FakeRibbonItem("todo", item_id=3, text="also good"),
    ]
    monkeypatch.setattr(
        "app.services.ribbon.ribbon_items_for",
        lambda page_slug, user, store_scope, category=None: items,
    )
    with app.test_request_context("/"):
        cats = ribbon_render_context(_make_user(), "dashboard", "tomball")
    by_slug = {c["slug"]: c for c in cats}
    # The raising item is dropped; the two good ones survive — a single
    # bad item can't take out its whole category.
    assert by_slug["todo"]["count"] == 2
    texts = [it["text"] for it in by_slug["todo"]["entries"]]
    assert "good" in texts and "also good" in texts


# ============================================================
# 3. THE MOST IMPORTANT TEST — a raising router cannot 500 the page
# ============================================================

def test_render_context_raising_router_degrades_to_empty(app, monkeypatch):
    """1B spec §9: a router that RAISES degrades to an empty ribbon.
    ribbon_render_context must NEVER propagate the exception."""
    def _boom(page_slug, user, store_scope, category=None):
        raise RuntimeError("router exploded")
    monkeypatch.setattr("app.services.ribbon.ribbon_items_for", _boom)
    with app.test_request_context("/"):
        # Must not raise.
        cats = ribbon_render_context(_make_user(), "dashboard", "tomball")
    # Degrades to the seven-empty-categories fallback.
    assert [c["slug"] for c in cats] == [s for s, _ in RIBBON_CATEGORIES]
    assert all(c["count"] == 0 for c in cats)
    assert all(c["is_collapsed"] is False for c in cats)


def test_partial_renders_when_router_raises(app, monkeypatch):
    """Same property, one layer up: rendering the actual _ribbon.html
    partial with a raising router produces HTML (an empty ribbon), not
    a TemplateError / 500. This is the proof the base_dashboard.html
    include is safe on every authenticated page."""
    def _boom(page_slug, user, store_scope, category=None):
        raise RuntimeError("router exploded")
    monkeypatch.setattr("app.services.ribbon.ribbon_items_for", _boom)
    with app.test_request_context("/"):
        g.current_user = _make_user()
        html = render_template("partials/_ribbon.html",
                               active="dashboard", store_slug="tomball")
    assert 'class="ck-ribbon"' in html
    # All seven category labels still present even though the router blew up.
    for _slug, label in RIBBON_CATEGORIES:
        assert label in html


# ============================================================
# 4. Collapse-toggle endpoint
# ============================================================
#
# The endpoint is tested by invoking the view function directly inside
# a test_request_context — NOT through the test client. Going through
# the client would run the app's existing auth before_request chain
# (site-password gate + keypad_auth's g.current_user seating), which
# would redirect or null out the user before the endpoint ran. Direct
# invocation isolates the unit under test: we control g.current_user
# and db.SessionLocal, and read the view's return value straight.

def _call_collapse(app, db_session, monkeypatch, category, user):
    """Invoke ribbon_collapse(category) directly. Returns
    (response, status_code), normalizing the view's two return shapes:
    a bare Response (jsonify, 200) or a (Response, status) tuple.

    Note the monkeypatch target: ribbon_routes.py does
    `from app.db import SessionLocal` at module load, so the name
    `SessionLocal` lives in the ribbon_routes namespace — patching
    app.db.SessionLocal would NOT redirect the endpoint's lookup.
    Patch the name where the endpoint resolves it."""
    monkeypatch.setattr(
        "app.web.ribbon_routes.SessionLocal", lambda: db_session)
    from app.web.ribbon_routes import ribbon_collapse
    with app.test_request_context(
        f"/partner/ribbon/collapse/{category}", method="POST",
    ):
        if user is not None:
            g.current_user = user
        result = ribbon_collapse(category)
    if isinstance(result, tuple):
        body, status = result
        return body, status
    return result, result.status_code


def test_collapse_first_post_creates_collapsed_row(app, db_session, monkeypatch):
    user = _make_user(user_id=99)
    resp, status = _call_collapse(app, db_session, monkeypatch, "todo", user)
    assert status == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["category"] == "todo"
    assert data["is_collapsed"] is True
    rows = db_session.query(RibbonCategoryPreference).all()
    assert len(rows) == 1
    assert rows[0].category == "todo"
    assert rows[0].is_collapsed is True
    assert rows[0].user_id == 99


def test_collapse_second_post_flips_back_to_false(app, db_session, monkeypatch):
    user = _make_user(user_id=99)
    _call_collapse(app, db_session, monkeypatch, "vendors", user)
    resp, status = _call_collapse(app, db_session, monkeypatch, "vendors", user)
    assert status == 200
    assert resp.get_json()["is_collapsed"] is False
    rows = (db_session.query(RibbonCategoryPreference)
            .filter_by(category="vendors").all())
    assert len(rows) == 1  # upsert, not a second row
    assert rows[0].is_collapsed is False


def test_collapse_invalid_category_400(app, db_session, monkeypatch):
    user = _make_user(user_id=99)
    resp, status = _call_collapse(
        app, db_session, monkeypatch, "not_a_category", user)
    assert status == 400
    assert resp.get_json()["ok"] is False
    # No row written for a rejected category.
    assert db_session.query(RibbonCategoryPreference).count() == 0


def test_collapse_unauthenticated_redirects(app, db_session, monkeypatch):
    """No g.current_user → redirect to keypad login, not a 500/200."""
    resp, status = _call_collapse(
        app, db_session, monkeypatch, "todo", user=None)
    assert status in (301, 302)
    location = resp.headers.get("Location", "").lower()
    assert "keypad" in location or "login" in location


# ============================================================
# 5. The partial — seven categories + the §6.2 X/Check markup contract
# ============================================================

def test_partial_renders_seven_categories_against_stub(app, ribbon_db):
    """Against the real stub (returns []), the partial renders all
    seven category headers + seven empty-states, no error."""
    with app.test_request_context("/"):
        g.current_user = _make_user()
        html = render_template("partials/_ribbon.html",
                               active="dashboard", store_slug="tomball")
    for _slug, label in RIBBON_CATEGORIES:
        assert label in html
    # Seven empty-state lines (the stub returns no items).
    assert html.count("Nothing here right now") == 7


def test_partial_renders_xcheck_markup_contract(app, ribbon_db, monkeypatch):
    """The §6.2 markup contract: data-item-type + data-item-id on the
    .ribbon-item wrapper, .ribbon-item__x present iff can_dismiss,
    .ribbon-item__check present iff can_check, styling_class applied.
    1D's ribbon.js wires against exactly this — it must match."""
    items = [
        _FakeRibbonItem("todo", item_type="task", item_id=7,
                        can_dismiss=True, can_check=True,
                        text="Has both controls",
                        styling_class="ribbon-item--alert"),
        _FakeRibbonItem("vendors", item_type="signal", item_id=12,
                        can_dismiss=False, can_check=True,
                        text="Check only"),
        _FakeRibbonItem("sales", item_type="sales_insight", item_id=3,
                        can_dismiss=True, can_check=False,
                        text="Dismiss only", sub_text="valid until 6pm"),
    ]
    monkeypatch.setattr(
        "app.services.ribbon.ribbon_items_for",
        lambda page_slug, user, store_scope, category=None: items,
    )
    with app.test_request_context("/"):
        g.current_user = _make_user()
        html = render_template("partials/_ribbon.html",
                               active="dashboard", store_slug="tomball")
    # Wrapper data-attributes.
    assert 'data-item-type="task"' in html
    assert 'data-item-id="7"' in html
    assert 'data-item-type="signal"' in html
    assert 'data-item-type="sales_insight"' in html
    # styling_class applied to the wrapper.
    assert 'class="ribbon-item ribbon-item--alert"' in html
    # Item 7: both controls.
    # Item 12 (signal, check-only): check button, no X.
    # Item 3 (sales_insight, dismiss-only): X button, no check, has sub_text.
    assert html.count('data-action="dismiss"') == 2   # items 7 + 3
    assert html.count('data-action="check"') == 2     # items 7 + 12
    assert 'class="ribbon-item__sub"' in html
    assert "valid until 6pm" in html


def test_partial_collapse_state_reflected(app, ribbon_db):
    """A RibbonCategoryPreference row with is_collapsed=True makes that
    category render with data-collapsed="true"; absent → "false".

    The ribbon_db fixture binds the global SessionLocal — which both
    ribbon_routes.py and ribbon.py captured via a module-level
    `from app.db import SessionLocal` — at the in-memory test session,
    so ribbon_render_context's collapse-pref query AND the real
    ribbon_items_for run hermetically."""
    user = _make_user(user_id=55)
    ribbon_db.add(RibbonCategoryPreference(
        user_id=55, category="employee", is_collapsed=True))
    ribbon_db.commit()
    with app.test_request_context("/"):
        g.current_user = user
        html = render_template("partials/_ribbon.html",
                               active="dashboard", store_slug="tomball")
    # employee category collapsed, todo (no row) expanded.
    assert 'data-category="employee"' in html
    assert 'data-collapsed="true"' in html
    # exactly one category collapsed.
    assert html.count('data-collapsed="true"') == 1
    assert html.count('data-collapsed="false"') == 6
