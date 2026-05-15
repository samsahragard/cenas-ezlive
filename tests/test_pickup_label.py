"""Tests for the pickup_label helper + the ez-market display fix
(Sam #1486, samai #1488, 2026-05-15).

Coverage:
- pickup_label(order) maps store_1 / store_2 to the physical-kitchen
  label string with the em dash glyph byte-asserted.
- pickup_label(order) falls back to Order.reported_store for unknown /
  None origin_store_id.
- pickup_label is registered as a Jinja global on the app.
- Driver-facing templates (ez_market.html, ez_manage.html) call
  pickup_label(o) at the renaming sites.
- Audit-surface templates (order_view.html, orders_by_store.html,
  review_detail.html, review_queue.html) still render the raw
  reported_store, with the audit-comment Jinja annotation in place.
- _ensure_miles_for_visible lazily re-computes pickup_miles for
  pre-cutoff legacy orders, is idempotent across views within a
  process, and skips post-cutoff orders.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.domain.normalize import pickup_label

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "app" / "templates"

# Expected display strings — byte-asserted to ensure the em dash
# (U+2014) is not silently replaced with a hyphen-minus (U+002D).
_COPPERFIELD = "Copperfield Kitchen — 15650 FM 529, Houston, TX 77095"
_TOMBALL = "Tomball Kitchen — 27727 Tomball Pkwy, Tomball, TX 77375"


def _order(origin_store_id=None, reported_store=None):
    """Minimal duck-typed Order. pickup_label only reads two attrs."""
    return SimpleNamespace(
        origin_store_id=origin_store_id,
        reported_store=reported_store,
    )


# ============================================================
# 1. pickup_label() unit tests
# ============================================================

def test_pickup_label_store_1_copperfield():
    """store_1 returns the Copperfield kitchen label with em dash."""
    out = pickup_label(_order(origin_store_id="store_1",
                              reported_store="Cenas Fajitas - 3733 Westheimer Rd"))
    assert out == _COPPERFIELD
    # Em dash is U+2014, not hyphen-minus U+002D.
    assert "—" in out
    assert " — " in out  # exact glyph string
    # Byte-assertion: the em dash takes 3 bytes in UTF-8 (e2 80 94).
    assert out.encode("utf-8").count(b"\xe2\x80\x94") == 1


def test_pickup_label_store_2_tomball():
    """store_2 returns the Tomball kitchen label with em dash."""
    out = pickup_label(_order(origin_store_id="store_2",
                              reported_store="Cenas Fajitas - 2162 Spring Stuebner Rd"))
    assert out == _TOMBALL
    assert "—" in out
    assert out.encode("utf-8").count(b"\xe2\x80\x94") == 1


def test_pickup_label_fallback_none_origin_returns_reported_store():
    """origin_store_id is None -> fall back to the raw ezCater text."""
    raw = "Cenas Fajitas - some legacy text"
    out = pickup_label(_order(origin_store_id=None, reported_store=raw))
    assert out == raw


def test_pickup_label_fallback_unknown_origin_returns_reported_store():
    """origin_store_id is not in {store_1, store_2} -> fall back."""
    raw = "Cenas Fajitas - 3733 Westheimer Rd"
    out = pickup_label(_order(origin_store_id="store_99", reported_store=raw))
    assert out == raw


def test_pickup_label_fallback_no_reported_store_returns_empty_string():
    """Both fields empty -> empty string (template's `or '—'` handles
    the empty render)."""
    out = pickup_label(_order(origin_store_id=None, reported_store=None))
    assert out == ""


# ============================================================
# 2. Jinja global registration
# ============================================================

def test_pickup_label_registered_as_jinja_global():
    """The app factory registers pickup_label in jinja_env.globals so
    templates can call {{ pickup_label(o) }} without an import."""
    from app import create_app
    app = create_app()
    assert "pickup_label" in app.jinja_env.globals
    # The exact function: not a wrapper or alias to something else.
    assert app.jinja_env.globals["pickup_label"] is pickup_label


def test_pickup_label_renders_via_jinja_environment():
    """Round-trip through the Jinja env to verify the global is callable
    inside a template, not just present in the namespace."""
    from app import create_app
    app = create_app()
    tpl = app.jinja_env.from_string("{{ pickup_label(o) }}")
    out = tpl.render(o=_order(origin_store_id="store_1", reported_store="ghost"))
    assert out == _COPPERFIELD


# ============================================================
# 3. Template integration — static contract checks
# ============================================================
# Confirms the swap is in place at every site samai specified. We
# don't full-render the templates here (they need partner-auth +
# user context); the build-specific browser-verify (rule 2) in the
# three-gate covers end-to-end rendering against committed code.

@pytest.mark.parametrize("template,line_excerpt", [
    ("ez_market.html",  "pickup_label(o) or 'n/a'"),
    ("ez_market.html",  "<strong>Pickup:</strong> {{ pickup_label(o) or '—' }}"),
    ("ez_market.html",  "external_order_id }} · {{ pickup_label(o) or '—' }}"),
    ("ez_manage.html",  "pickup_label(o) or '—' }} → {{ o.delivery_address"),
])
def test_driver_facing_templates_call_pickup_label(template, line_excerpt):
    """Each driver-facing render site must invoke pickup_label, not
    o.reported_store. Catches a regression where someone reverts the
    swap (or "fixes" an audit-side surface by mistake)."""
    content = (TEMPLATES / template).read_text(encoding="utf-8")
    assert line_excerpt in content, (
        f"{template} missing pickup_label call: {line_excerpt!r}"
    )


@pytest.mark.parametrize("template,marker_excerpt", [
    ("order_view.html",       "{# audit surface — raw ezCater storefront, intentionally pre-collapse #}"),
    ("orders_by_store.html",  "{# audit surface — raw ezCater storefront, intentionally pre-collapse #}"),
    ("review_detail.html",    "{# audit surface — raw ezCater storefront, intentionally pre-collapse #}"),
    ("review_queue.html",     "{# audit surface — raw ezCater storefront, intentionally pre-collapse #}"),
])
def test_audit_templates_keep_reported_store_with_annotation(template, marker_excerpt):
    """Audit / review surfaces deliberately render the raw ezCater
    storefront-of-record. The Jinja audit-comment annotation
    documents the intent so future contributors don't `helpfully`
    swap them to pickup_label."""
    content = (TEMPLATES / template).read_text(encoding="utf-8")
    assert marker_excerpt in content, (
        f"{template} missing audit-comment annotation"
    )
    # And still references reported_store (didn't get swapped by accident).
    assert "reported_store" in content


# ============================================================
# 4. Lazy legacy pickup_miles recompute
# ============================================================
# Migration 11 backfilled some pickup_miles from ezCater XLSX (origin =
# ghost storefront, which is wrong per Sam's policy). _ensure_miles_for_visible
# re-computes these once per process via Google Routes.

@pytest.fixture(autouse=True)
def _reset_legacy_cache(monkeypatch):
    """Each test starts with an empty _recomputed_legacy_ids cache so
    idempotency assertions reflect this test's own calls, not leftover
    state from another test."""
    import app.web.driver_system as ds
    monkeypatch.setattr(ds, "_recomputed_legacy_ids", set())
    yield


def _legacy_order(order_id, miles=42.0, created=None):
    """Pre-cutoff order with stored (suspect) pickup_miles."""
    if created is None:
        created = datetime(2026, 5, 10, 12, 0, 0)  # before cutoff
    return SimpleNamespace(
        id=order_id,
        created_at=created,
        origin_store_id="store_3",
        pickup_kitchen="copperfield",
        pickup_miles=miles,
        delivery_address="100 Test Rd, Houston, TX 77095",
    )


def _modern_order(order_id, miles=None, created=None):
    """Post-cutoff order — no recompute needed."""
    if created is None:
        created = datetime(2026, 5, 14, 12, 0, 0)  # after cutoff
    return SimpleNamespace(
        id=order_id,
        created_at=created,
        origin_store_id="store_1",
        pickup_kitchen="copperfield",
        pickup_miles=miles,
        delivery_address="200 New Rd, Houston, TX 77065",
    )


class _NullDB:
    def commit(self): pass


def test_lazy_recompute_overwrites_legacy_miles():
    """A pre-cutoff order with a stale (ezCater-XLSX) pickup_miles
    gets re-computed via Google Routes and overwritten."""
    from app.web import driver_system as ds
    order = _legacy_order(1, miles=42.0)
    with patch("app.services.ezcater_miles.compute_one_way_miles",
               return_value=15.7) as m:
        ds._ensure_miles_for_visible(_NullDB(), [order])
    assert order.pickup_miles == 15.7
    assert m.call_count == 1
    assert m.call_args.args == ("copperfield", "100 Test Rd, Houston, TX 77095")


def test_lazy_recompute_idempotent_within_process():
    """Same legacy order viewed twice in the same process triggers the
    Routes API exactly once. Per-process cache prevents waste."""
    from app.web import driver_system as ds
    order = _legacy_order(2, miles=42.0)
    with patch("app.services.ezcater_miles.compute_one_way_miles",
               return_value=15.7) as m:
        ds._ensure_miles_for_visible(_NullDB(), [order])
        ds._ensure_miles_for_visible(_NullDB(), [order])
    assert m.call_count == 1


def test_post_cutoff_order_not_recomputed():
    """An order created after the migration-11 cutoff already has
    correct miles (or None) and must not be force-recomputed."""
    from app.web import driver_system as ds
    order = _modern_order(3, miles=10.0)
    with patch("app.services.ezcater_miles.compute_one_way_miles",
               return_value=99.9) as m:
        ds._ensure_miles_for_visible(_NullDB(), [order])
    # pickup_miles untouched (was already non-None, post-cutoff, so
    # neither the original miles-None backfill loop nor the legacy
    # recompute loop applies).
    assert order.pickup_miles == 10.0
    assert m.call_count == 0


def test_legacy_recompute_with_routes_failure_still_marks_cache():
    """If Routes returns None (transient API error), pickup_miles is
    left as-is but the id is added to the recomputed cache so we don't
    hammer Routes on every view in the same process. Retry on restart."""
    from app.web import driver_system as ds
    order = _legacy_order(4, miles=42.0)
    with patch("app.services.ezcater_miles.compute_one_way_miles",
               return_value=None) as m:
        ds._ensure_miles_for_visible(_NullDB(), [order])
        ds._ensure_miles_for_visible(_NullDB(), [order])
    assert order.pickup_miles == 42.0  # unchanged on API failure
    assert m.call_count == 1  # second view skipped
    assert 4 in ds._recomputed_legacy_ids
