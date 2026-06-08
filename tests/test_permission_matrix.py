"""Parameterized per-role permission matrix tests.

Phase 0 / Block 4 long-tail — Sam (2026-05-13 20:25) directed this as
the regression net for everything the Block 4 chain just landed: the
10-role taxonomy, requires_permission decorator, has_permission Jinja
helper, ENFORCE flip, sidebar gate replacements, corporate grant for
anomaly.admin + access.team_admin, and the /unassign-courier route
shape with defense-in-depth cross-check.

The suite asserts five layered correctness properties:

  1. The (role × tag) MATRIX — for every canonical role and every tag
     in samai's spec §3.1, _user_has() returns the expected boolean
     against ROLE_PERMISSIONS. Drift in either direction (a role
     silently gaining or losing a tag) surfaces as a per-row failure
     with a name like ``test_gate__role=gm__tag=anomaly.admin__expected=denied``.

  2. The WILDCARD short-circuit asserted directly. A regression that
     silently dropped partner's "*" would be hard to catch from
     "all partner tests still pass" alone (everything else partner
     touches would still pass via fallthrough). Test the wildcard
     branch as its own case.

  3. The LEGACY-ALIAS pathway — User rows with the pre-spec values
     'manager' and 'corporate-driver' resolve to gm-equivalent and
     driver-equivalent sets respectively.

  4. The STORE-SCOPE check — _user_has(user, tag, store_id) honors
     User.store_scope CSV membership for store-scoped roles, lets
     no-scope roles (partner, corporate) pass any store_id, and
     denies when the store_id isn't in the user's assignment.

  5. The DEFENSE-IN-DEPTH cross-check on /unassign-courier — both the
     decorator and the handler's actual-scope verification matter.
     The decorator fires first (HTTP redirect to /access-denied); the
     handler's 403 is the inner gate that only triggers when the
     decorator has already let the request through. We assert which
     gate fires for each adversarial pattern so a future regression
     can't quietly demote one of them without surfacing here.

Plus a denial-log INSERT assertion — denied requests must persist a
PermissionDenial row with the right tag/route/actor/mode. Catches a
class of regression where the gate works but the audit trail silently
breaks.

A spec-drift INFORMATIONAL test surfaces any tag that's currently held
only by partner via wildcard (no explicit grant in any other role) as
a list — not a failure. Sam reviews and decides if that's intentional
or a spec gap.

Test IDs: see ``_test_id()`` — every parametrized case names itself
``role=R__tag=T__expected=granted/denied`` so a pytest failure points
at the exact combo without needing a stack trace.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from flask import g

from app.services import permissions as perm
from app.services.permissions import (
    ROLE_PERMISSIONS,
    _LEGACY_ALIASES,
    _user_has,
    _enforcing,
    requires_permission,
)


# ============================================================
# Helpers
# ============================================================

def _make_user(role: str, store_scope: str | None = None, user_id: int = 1) -> SimpleNamespace:
    """Lightweight User stand-in. _user_has only reads permission_level
    and store_scope; we ducktype rather than building a full SQLAlchemy
    row so the matrix tests stay cold (no DB)."""
    return SimpleNamespace(
        id=user_id,
        full_name=f"Test {role}",
        permission_level=role,
        store_scope=store_scope,
        active=True,
    )


@pytest.fixture(scope="session")
def _shared_app():
    """Session-scoped Flask app — every matrix test runs through the
    same instance. Creating the app per-test was burning ~150ms each;
    with 600+ matrix rows that's a 90-second tax for no behavior
    difference. Permission checking is stateless across requests."""
    os.environ.pop("PERMISSION_ENFORCE", None)  # default = enforce
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture(autouse=True)
def _request_context_for_user_has(_shared_app):
    """_user_has reads session.get('impersonating_user_id'), which
    requires an active Flask request context. Every test in this file
    runs inside one — we never populate the session (so the
    impersonation branch is a no-op), but the context has to exist
    for the lookup not to raise."""
    with _shared_app.test_request_context("/_test_setup"):
        yield


def _expected_grant(role: str, tag: str) -> bool:
    """Canonical answer derived from ROLE_PERMISSIONS — what _user_has
    SHOULD return for (role, tag) ignoring store-scope. Partner is the
    wildcard short-circuit. Legacy aliases ('manager', 'corporate-driver')
    resolve via _LEGACY_ALIASES to a canonical role's set."""
    canonical = role if role in ROLE_PERMISSIONS else _LEGACY_ALIASES.get(role)
    if canonical is None:
        return False
    role_perms = ROLE_PERMISSIONS[canonical]
    if "*" in role_perms:
        return True
    return tag in role_perms


def _all_canonical_tags() -> list[str]:
    """Flattened sorted list of every tag in ROLE_PERMISSIONS — the
    full universe the matrix is checked against. Partner's '*' is
    excluded; we test the wildcard separately."""
    tags: set[str] = set()
    for role, perms in ROLE_PERMISSIONS.items():
        for t in perms:
            if t != "*":
                tags.add(t)
    return sorted(tags)


_ALL_ROLES = sorted(set(ROLE_PERMISSIONS.keys()) | set(_LEGACY_ALIASES.keys()))
_ALL_TAGS = _all_canonical_tags()


def _test_id(role: str, tag: str) -> str:
    """Generate a self-describing pytest ID for the matrix parametrize.
    Format: role=X__tag=Y__expected=granted/denied. When a test fails
    the ID alone tells you the exact combo without reading the stack."""
    expected = "granted" if _expected_grant(role, tag) else "denied"
    return f"role={role}__tag={tag}__expected={expected}"


# ============================================================
# 1. (role × tag) MATRIX
# ============================================================

@pytest.mark.parametrize(
    "role,tag",
    [(r, t) for r in _ALL_ROLES for t in _ALL_TAGS],
    ids=[_test_id(r, t) for r in _ALL_ROLES for t in _ALL_TAGS],
)
def test_gate_role_tag_matrix(role, tag):
    """Every (role × tag) combo asserts the canonical gate decision.
    _user_has should match what _expected_grant computes from
    ROLE_PERMISSIONS. Drift in either direction surfaces here."""
    user = _make_user(role)
    expected = _expected_grant(role, tag)
    actual = _user_has(user, tag, None)
    assert actual is expected, (
        f"gate decision drift: role={role!r} tag={tag!r} "
        f"expected={'granted' if expected else 'denied'} "
        f"actual={'granted' if actual else 'denied'}"
    )


# ============================================================
# 2. WILDCARD short-circuit (asserted directly)
# ============================================================

def test_partner_wildcard_grants_arbitrary_tag():
    """Partner's '*' should grant any tag, including ones nobody else
    has and ones that don't exist at all. Without this direct check a
    regression that silently dropped '*' from partner's set would
    still pass the matrix (because partner has every real tag through
    other means in the dict — wait, no it doesn't; partner ONLY has
    '*'). So actually this is the cleanest case to demonstrate the
    wildcard fires."""
    partner = _make_user("partner")
    assert _user_has(partner, "this_tag_does_not_exist_anywhere", None) is True
    assert _user_has(partner, "orders.assign_driver", None) is True
    assert _user_has(partner, "legal.view", None) is True


def test_partner_wildcard_grants_arbitrary_tag_with_store_id():
    """Wildcard also bypasses the store-scope check — partner has all
    stores implicitly even when store_scope is NULL on the User row."""
    partner = _make_user("partner", store_scope=None)
    assert _user_has(partner, "orders.unassign_driver", "tomball") is True
    assert _user_has(partner, "orders.unassign_driver", "unknown") is True


def test_non_partner_without_tag_does_NOT_pass_wildcard():
    """Belt: a non-partner role without the tag in its set returns
    False — the wildcard is partner-specific, not generic."""
    expo = _make_user("expo")
    assert _user_has(expo, "anomaly.admin", None) is False
    assert _user_has(expo, "legal.view", None) is False


# ============================================================
# 3. LEGACY ALIAS pathway
# ============================================================

@pytest.mark.parametrize(
    "legacy_role,canonical_role",
    list(_LEGACY_ALIASES.items()),
    ids=[f"legacy={k}__canonical={v}" for k, v in _LEGACY_ALIASES.items()],
)
def test_legacy_alias_resolves_to_canonical_set(legacy_role, canonical_role):
    """User rows with legacy values ('manager' / 'corporate-driver')
    must resolve to the canonical role's tag set so dark-launch +
    pre-Phase-2-cleanup users aren't blanket-denied."""
    legacy_user = _make_user(legacy_role)
    for tag in ROLE_PERMISSIONS[canonical_role]:
        if tag == "*":
            continue
        assert _user_has(legacy_user, tag, None) is True, (
            f"legacy alias {legacy_role!r} should inherit {canonical_role!r}'s "
            f"tag {tag!r}"
        )


def test_legacy_alias_inherits_correct_denial():
    """Conversely: a legacy 'manager' user should be DENIED tags that
    gm doesn't have. Confirms the alias is full-set inheritance, not
    a free wildcard."""
    legacy_manager = _make_user("manager")
    assert _user_has(legacy_manager, "anomaly.admin", None) is False
    assert _user_has(legacy_manager, "access.team_admin", None) is False


# ============================================================
# 4. STORE-SCOPE check
# ============================================================

class TestStoreScope:
    """Behavior of the optional store_id arg to _user_has()."""

    def test_store_scoped_role_matching_store_passes(self):
        gm = _make_user("gm", store_scope="tomball")
        assert _user_has(gm, "orders.unassign_driver", "tomball") is True

    def test_store_scoped_role_other_store_denied(self):
        gm = _make_user("gm", store_scope="tomball")
        assert _user_has(gm, "orders.unassign_driver", "copperfield") is False

    def test_store_scoped_role_multi_store_passes_both(self):
        chef = _make_user("corporate_chef", store_scope="tomball,copperfield")
        assert _user_has(chef, "orders.unassign_driver", "tomball") is True
        assert _user_has(chef, "orders.unassign_driver", "copperfield") is True

    def test_corporate_no_scope_passes_any_store(self):
        """Corporate has the tag but NULL store_scope — the inner
        store_id check is bypassed (assignment to every store
        implicit). Compare against a store-scoped role above."""
        corp = _make_user("corporate", store_scope=None)
        assert _user_has(corp, "orders.unassign_driver", "tomball") is True
        assert _user_has(corp, "orders.unassign_driver", "copperfield") is True
        assert _user_has(corp, "orders.unassign_driver", "unknown") is True

    def test_partner_wildcard_passes_any_store(self):
        partner = _make_user("partner")
        assert _user_has(partner, "orders.unassign_driver", "tomball") is True
        assert _user_has(partner, "orders.unassign_driver", "anything") is True

    def test_role_without_tag_denied_regardless_of_store(self):
        """A role is denied a tag it lacks even within its own store. NOTE
        (aick 2026-06-08): expo's management-profile upgrade (Sam 2026-06-07)
        granted it orders.* + drivers.*, so the prior 'orders.unassign_driver'
        assertion went stale and failed on main. labor.view_all_stores is a
        corporate-tier tag expo genuinely lacks -- the real 'missing tag denies'
        case."""
        expo = _make_user("expo", store_scope="tomball")
        assert _user_has(expo, "labor.view_all_stores", "tomball") is False


# ============================================================
# 5. DEFENSE-IN-DEPTH on /unassign-courier
# ============================================================

@pytest.fixture
def app(_shared_app):
    """Real Flask app for route-level tests. Re-uses the session-scoped
    instance to keep matrix runtime in the ~3s bucket."""
    return _shared_app


def test_decorator_fires_before_handler_on_scope_mismatch(app):
    """A Tomball GM hitting a Copperfield order URL must be denied by
    the DECORATOR (URL-claimed scope != user's scope), not by the
    handler's actual-scope cross-check. The decorator runs first;
    a regression that moved scope-checking entirely into the handler
    would still deny the request but via a 403 JSON instead of the
    /access-denied redirect — observable here."""
    from app.services.permissions import requires_permission

    @requires_permission("orders.unassign_driver", store_arg="store_scope")
    def fake_view(store_scope, external_order_id):
        # Inner function should NEVER be reached on a denial; if it is,
        # this test fails because we'd see a 200 instead of a 302.
        return "INNER REACHED"

    with app.test_request_context(
        "/copperfield/orders/view/X-1/unassign-courier",
        method="POST",
    ):
        g.current_user = _make_user("gm", store_scope="tomball")
        result = fake_view(store_scope="copperfield", external_order_id="X-1")

    # Decorator denial → 302 redirect to /access-denied with the
    # missing tag in ?need=. Confirms the decorator fired first
    # (before the inner handler).
    assert result.status_code == 302
    assert "/access-denied" in result.location
    assert "need=orders.unassign_driver" in result.location


def test_decorator_passes_then_inner_can_apply_actual_scope_check(app):
    """When URL-claimed scope MATCHES the user's assignment, the
    decorator passes — the inner handler is responsible for the
    actual-scope check against the order's origin_store_id. Confirms
    the decorator doesn't over-block; defense-in-depth needs both
    layers to fire on the right cases."""
    from app.services.permissions import requires_permission

    @requires_permission("orders.unassign_driver", store_arg="store_scope")
    def fake_view(store_scope, external_order_id):
        return "INNER REACHED"

    with app.test_request_context(
        "/tomball/orders/view/X-1/unassign-courier",
        method="POST",
    ):
        g.current_user = _make_user("gm", store_scope="tomball")
        result = fake_view(store_scope="tomball", external_order_id="X-1")

    assert result == "INNER REACHED"


def test_partner_wildcard_passes_any_url_scope(app):
    """Partner's '*' grants the decorator regardless of URL scope.
    The handler's actual-scope cross-check is still responsible for
    rejecting a partner who somehow claimed the wrong scope — but
    that's the inner gate, not this one."""
    from app.services.permissions import requires_permission

    @requires_permission("orders.unassign_driver", store_arg="store_scope")
    def fake_view(store_scope, external_order_id):
        return "INNER REACHED"

    with app.test_request_context(
        "/copperfield/orders/view/X-1/unassign-courier",
        method="POST",
    ):
        g.current_user = _make_user("partner")
        result = fake_view(store_scope="copperfield", external_order_id="X-1")

    assert result == "INNER REACHED"


# ============================================================
# 6. DENIAL LOG insert
# ============================================================

def test_denial_logs_permissiondenial_row_with_correct_fields(app, db_session):
    """When a denial fires under PERMISSION_ENFORCE!=0, _log_denial
    inserts a PermissionDenial row with the right tag, route,
    user_id, user_role, and mode='ENFORCING'. Belt: catches a
    regression where the gate denies but the audit trail breaks."""
    from app.models import PermissionDenial
    from app.services.permissions import requires_permission
    from app.services import permissions as perm_mod
    from app import db as db_mod

    # Re-route SessionLocal inside _log_denial to our in-memory test
    # session so the INSERT lands in the same DB this test reads from.
    original_SessionLocal = db_mod.SessionLocal
    db_mod.SessionLocal = lambda: db_session

    @requires_permission("anomaly.admin")
    def fake_view():
        return "INNER REACHED"

    try:
        with app.test_request_context(
            "/partner/anomalies/rules",
            method="GET",
        ):
            g.current_user = _make_user("gm", store_scope="tomball", user_id=42)
            result = fake_view()

        # Verify denial fired (gm doesn't have anomaly.admin).
        assert result.status_code == 302
        assert "/access-denied" in result.location

        # Verify the PermissionDenial row landed.
        rows = db_session.query(PermissionDenial).all()
        assert len(rows) == 1, f"expected 1 denial row, got {len(rows)}"
        denial = rows[0]
        assert denial.tag == "anomaly.admin"
        assert denial.route == "/partner/anomalies/rules"
        assert denial.user_id == 42
        assert denial.user_role == "gm"
        assert denial.mode == "ENFORCING", (
            "denial mode should be ENFORCING under the current default "
            "PERMISSION_ENFORCE=1 (set by the flip in 3373a28)"
        )
    finally:
        db_mod.SessionLocal = original_SessionLocal


# ============================================================
# 7. SPEC DRIFT (informational, not a fail)
# ============================================================

def test_surface_implicit_wildcard_only_tags():
    """Informational: print any tag that's currently in NO non-partner
    role's set (granted only via partner wildcard). These are
    candidates for explicit promotion to corporate's set or
    documentation as partner-only — see samai's spec §2.2 partner+
    corporate-tier rule and the future-tags-follow-same-pattern
    note in ROLE_PERMISSIONS['corporate']'s comment block.

    Doesn't fail — surfacing the list each test run is the value.
    Sam reviews + decides per-tag if drift is intentional."""
    implicit_only: list[str] = []
    for tag in _ALL_TAGS:
        non_partner_holders = [
            role for role, perms in ROLE_PERMISSIONS.items()
            if role != "partner" and tag in perms
        ]
        if not non_partner_holders:
            implicit_only.append(tag)

    if implicit_only:
        print(
            "\n[spec drift] tags currently held only by partner via "
            f"wildcard ({len(implicit_only)}):"
        )
        for tag in sorted(implicit_only):
            print(f"  - {tag}")
        print(
            "Decide per tag: promote to corporate set (samai spec §2.2 "
            "global-config-tag rule) or document as partner-only."
        )
    # No assertion — the list is informational.


# ============================================================
# 8. ENFORCEMENT default sanity
# ============================================================

def test_enforcement_default_is_on_when_env_unset(monkeypatch):
    """Sanity: post-3373a28 flip, _enforcing() returns True when
    PERMISSION_ENFORCE is unset. The default is the live behavior on
    prod; tests that bypass that default by setting the env var
    explicitly are doing the right thing — this one confirms the
    UNSET-env case still defaults to enforce."""
    monkeypatch.delenv("PERMISSION_ENFORCE", raising=False)
    assert _enforcing() is True


def test_enforcement_off_only_with_explicit_zero(monkeypatch):
    """The escape hatch: PERMISSION_ENFORCE=0 falls back to
    dark-launch. Any other value (1, true, anything) enforces."""
    monkeypatch.setenv("PERMISSION_ENFORCE", "0")
    assert _enforcing() is False
    monkeypatch.setenv("PERMISSION_ENFORCE", "1")
    assert _enforcing() is True
    monkeypatch.setenv("PERMISSION_ENFORCE", "true")
    assert _enforcing() is True
