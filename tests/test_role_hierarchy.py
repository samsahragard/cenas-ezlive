"""Phase 2 / Block 1 precondition — role_hierarchy.py tests.

Covers app/services/role_hierarchy.py (the new org-tier/domain module)
+ the five new hourly ROLE_PERMISSIONS entries, per the precondition
spec §4:

  - ROLE_TIER: all 15 roles map to expected tiers; the 5 new hourly
    roles are tier 1; legacy aliases resolve; unknown/None → 0; never
    raises
  - role_domain: all 15 roles map to expected domains; cook→kitchen,
    server/busser/host/bartender→foh; legacy aliases resolve;
    unknown/None → "foh"; never raises
  - consistency with role_classifier: the hourly roles' role_domain
    agrees with role_classifier.classify_role's BOH/FOH verdict —
    catches future drift between the two classifiers
  - ROLE_PERMISSIONS: the 5 new roles each resolve via _user_has to the
    minimal baseline set, not None

The existing test_permission_matrix.py (2255c1d) auto-extends — its
_ALL_ROLES picks up the 5 new keys, so the matrix grows by
5 × |_ALL_TAGS| rows. That's verified by the full-suite run, not
re-asserted here.
"""
from __future__ import annotations

import os

import pytest

from app.services.role_hierarchy import (
    ROLE_TIER,
    role_tier,
    role_domain,
)
from app.services.permissions import ROLE_PERMISSIONS, _user_has
from app.services.role_classifier import classify_role


# _user_has reads session.get('impersonating_user_id'), which needs an
# active Flask request context — same setup test_permission_matrix.py
# uses. Only the one _user_has test below needs it, but autouse keeps it
# simple; the pure role_hierarchy functions don't care either way.
@pytest.fixture(scope="module")
def _shared_app():
    os.environ.pop("PERMISSION_ENFORCE", None)
    os.environ.setdefault("ALLOW_DEV_SECRET", "1")
    os.environ.setdefault("SECRET_KEY", "devkey")
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture(autouse=True)
def _request_context(_shared_app):
    with _shared_app.test_request_context("/_test_setup"):
        yield


# The 5 §5.3 Path A additions.
_NEW_HOURLY = ["cook", "server", "busser", "host", "bartender"]

_HOURLY_BASELINE = {
    "ai.ask_claude_personal", "ai.view_transcripts",
    "transcripts.search", "transcripts.read",
}


# ---- ROLE_TIER ----

def test_role_tier_all_fifteen_roles():
    expected = {
        "partner": 5,
        "corporate": 4, "corporate_chef": 4,
        "gm": 3, "prep_manager": 3,
        "km": 2, "assistant_km": 2, "foh_manager": 2,
        "expo": 1, "driver": 1,
        "cook": 1, "server": 1, "busser": 1, "host": 1, "bartender": 1,
    }
    assert ROLE_TIER == expected, "ROLE_TIER drifted from precond spec §2.1"
    assert len(ROLE_TIER) == 15


def test_role_tier_new_hourly_roles_are_tier_1():
    for r in _NEW_HOURLY:
        assert role_tier(r) == 1, f"{r} should be hourly tier 1"


def test_role_tier_resolves_legacy_aliases():
    # 'manager' → gm → 3 ; 'corporate-driver' → driver → 1
    assert role_tier("manager") == 3
    assert role_tier("corporate-driver") == 1


def test_role_tier_unknown_and_none_return_zero():
    assert role_tier("nonexistent_role") == 0
    assert role_tier(None) == 0
    assert role_tier("") == 0


def test_role_tier_never_raises():
    # A grab-bag of junk inputs — none should raise.
    for junk in (None, "", "  ", "Cook", "PARTNER", "🙂", "123", "gm "):
        try:
            role_tier(junk)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"role_tier({junk!r}) raised {e!r}")


# ---- role_domain ----

def test_role_domain_all_roles():
    expected = {
        "partner": "both", "corporate": "both", "gm": "both",
        "corporate_chef": "kitchen", "prep_manager": "kitchen",
        "km": "kitchen", "assistant_km": "kitchen",
        "foh_manager": "foh",
        "cook": "kitchen",
        "server": "foh", "busser": "foh", "host": "foh", "bartender": "foh",
        "expo": "foh", "driver": "foh",
    }
    for role, dom in expected.items():
        assert role_domain(role) == dom, f"{role} domain mismatch"


def test_role_domain_new_hourly_split():
    assert role_domain("cook") == "kitchen"
    for r in ("server", "busser", "host", "bartender"):
        assert role_domain(r) == "foh", f"{r} should be foh"


def test_role_domain_resolves_legacy_aliases():
    assert role_domain("manager") == "both"        # → gm → both
    assert role_domain("corporate-driver") == "foh"  # → driver → foh


def test_role_domain_unknown_and_none_return_foh():
    assert role_domain("nonexistent_role") == "foh"
    assert role_domain(None) == "foh"
    assert role_domain("") == "foh"


def test_role_domain_never_raises():
    for junk in (None, "", "  ", "Cook", "PARTNER", "🙂", "123"):
        try:
            role_domain(junk)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"role_domain({junk!r}) raised {e!r}")


# ---- consistency with role_classifier (drift guard) ----

def test_role_domain_agrees_with_role_classifier_on_hourly_staff():
    """role_hierarchy.role_domain (keyed by app role) and
    role_classifier.classify_role (keyed by Toast job title) must agree
    on the same staff: cook = kitchen/boh; server/busser/host =
    foh/foh. Catches future drift between the two classifiers."""
    # role_classifier returns "boh"/"foh"; role_hierarchy returns
    # "kitchen"/"foh"/"both". Map kitchen↔boh for the comparison.
    _DOMAIN_TO_CLASS = {"kitchen": "boh", "foh": "foh"}
    for role in ("cook", "server", "busser", "host"):
        rh = role_domain(role)
        rc = classify_role(role)
        assert _DOMAIN_TO_CLASS[rh] == rc, (
            f"{role}: role_hierarchy says {rh!r} "
            f"({_DOMAIN_TO_CLASS[rh]!r}), role_classifier says {rc!r}")


# ---- ROLE_PERMISSIONS entries for the new hourly roles ----

def test_new_hourly_roles_have_baseline_permission_set():
    for r in _NEW_HOURLY:
        assert r in ROLE_PERMISSIONS, f"{r} missing from ROLE_PERMISSIONS"
        assert ROLE_PERMISSIONS[r] == _HOURLY_BASELINE, (
            f"{r} tag set drifted from the minimal hourly baseline")


def test_new_hourly_roles_resolve_via_user_has():
    """_user_has must resolve the 5 new roles to a real set (not None) —
    the taxonomy-completeness property Sam wants."""
    class _FakeUser:
        def __init__(self, level):
            self.permission_level = level
            self.store_scope = None

    for r in _NEW_HOURLY:
        u = _FakeUser(r)
        # A tag in the baseline → True
        assert _user_has(u, "ai.ask_claude_personal") is True, (
            f"{r} should have ai.ask_claude_personal")
        # A tag NOT in the baseline → False (resolved, not None-fallback)
        assert _user_has(u, "orders.assign_driver") is False, (
            f"{r} should NOT have orders.assign_driver")
