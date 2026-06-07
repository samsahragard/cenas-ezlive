"""S6b — HOURLY roles are SELF-ONLY (Sam, 2026-06-07).

Sam's directive: "hourly has NO company permissions." Sam #1063 (2026-05-26)
had leaked driver-administration tags (drivers.admin / drivers.view_roster /
drivers.reset_passcode) into every non-driver role, including the five hourly
roles (cook / server / busser / host / bartender). S6b strips them back so an
hourly user's EFFECTIVE perms are the self-only surface only — own profile +
own rank, which on the tag side is the personal-AI + transcripts baseline that
expo + driver already share.

This suite is the regression net for that strip. It asserts, for every hourly
role, that the role's set in ROLE_PERMISSIONS contains:

  * NONE of the leaked driver-admin tags
    (drivers.admin / drivers.view_roster / drivers.reset_passcode);
  * NO company / admin / labor / sales / orders / produce / manager_log /
    email / kds / team_reports / access / anomaly / developer / briefs tag —
    i.e. nothing that reads or mutates anyone else's data or any store/company
    surface;
  * ONLY the explicit self-only allow-list (the personal-AI + transcripts
    baseline).

The matrix test in test_permission_matrix.py derives its expectations FROM
ROLE_PERMISSIONS, so it stays self-consistent across this change; this file is
the independent assertion that the self-only PROPERTY actually holds, so a
future re-leak (someone re-adding a company tag to an hourly role) fails here
even though the matrix would silently absorb it.

Two layers:
  1. Pure dict assertions against ROLE_PERMISSIONS (DB-free, app-free).
  2. A behavioral check through _user_has() (the real gate) confirming an
     hourly user is DENIED each leaked/forbidden tag — mirrors the ducktyped
     SimpleNamespace + request-context pattern from test_permission_matrix.py.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.services.permissions import ROLE_PERMISSIONS, _user_has


# The five hourly-tier roles (role_buckets => SECTION_HOURLY on the catalog
# side; here we name the ROLE_PERMISSIONS keys that exist as hourly identities).
HOURLY_ROLES = ("cook", "server", "busser", "host", "bartender")

# The exact self-only surface an hourly role may hold: the personal-AI +
# transcripts baseline. These tags read ONLY the acting user's own data; none
# grants reach into roster / labor / sales / another person's record.
SELF_ONLY_TAGS = frozenset({
    "ai.ask_claude_personal",
    "ai.view_transcripts",
    "transcripts.search",
    "transcripts.read",
})

# The specific tags Sam #1063 leaked in — must be gone post-S6b.
LEAKED_TAGS = ("drivers.admin", "drivers.view_roster", "drivers.reset_passcode")

# Tag-prefix families that are company/admin/labor/sales surface — an hourly
# role must hold NONE of these. (ai.ask_claude_personal is self-only and is
# explicitly NOT in this list; the broader ai.ask_claude is a management tag.)
FORBIDDEN_PREFIXES = (
    "drivers.",        # driver administration (the #1063 leak)
    "labor.",          # wage / hours / cost surfaces for others / stores
    "sales.",          # sales reporting
    "orders.",         # order ops / driver assignment / payout
    "produce.",        # produce ordering / invoicing
    "manager_log.",    # manager log read/write
    "team_reports.",   # personnel reports
    "access.",         # access-request approval / team admin
    "anomaly.",        # anomaly rule admin
    "developer.",      # dev chat / app docs
    "briefs.",         # calibration briefs
    "email.",          # mailbox access
    "kds.",            # kitchen display / recipes / alerts
    "legal.",          # legal docs / insurance
)

# ai.ask_claude (the NON-personal, full assistant tag) is a management-tier
# grant and must never appear on an hourly role; ai.ask_claude_personal is the
# allowed self-only variant. Listed explicitly because a bare "ai." prefix
# would wrongly catch the allowed personal tag.
FORBIDDEN_EXACT = ("ai.ask_claude",)


@pytest.mark.parametrize("role", HOURLY_ROLES)
def test_hourly_role_has_no_leaked_driver_tags(role):
    """The #1063 leak is reversed: no hourly role holds drivers.admin /
    drivers.view_roster / drivers.reset_passcode."""
    perms = ROLE_PERMISSIONS[role]
    leaked_held = [t for t in LEAKED_TAGS if t in perms]
    assert leaked_held == [], (
        f"hourly role {role!r} still holds leaked driver-admin tags "
        f"{leaked_held!r}; S6b requires hourly to be self-only"
    )


@pytest.mark.parametrize("role", HOURLY_ROLES)
def test_hourly_role_holds_no_company_or_admin_tag(role):
    """No hourly role holds ANY company / admin / labor / sales tag — nothing
    that reaches another person's data or a store/company surface."""
    perms = ROLE_PERMISSIONS[role]
    offenders = sorted(
        t for t in perms
        if t.startswith(FORBIDDEN_PREFIXES) or t in FORBIDDEN_EXACT
    )
    assert offenders == [], (
        f"hourly role {role!r} holds company/admin tags {offenders!r}; "
        f"S6b requires hourly to be self-only (own profile + own rank)"
    )


@pytest.mark.parametrize("role", HOURLY_ROLES)
def test_hourly_role_is_exactly_the_self_only_surface(role):
    """Lock the set down: an hourly role holds EXACTLY the self-only allow-list
    and nothing else. Catches a re-leak in either direction (an extra company
    tag, or accidental loss of the self-only baseline)."""
    perms = set(ROLE_PERMISSIONS[role])
    assert perms == set(SELF_ONLY_TAGS), (
        f"hourly role {role!r} effective set drifted from the self-only "
        f"surface.\n  extra (forbidden): {sorted(perms - SELF_ONLY_TAGS)!r}\n"
        f"  missing (self-only): {sorted(set(SELF_ONLY_TAGS) - perms)!r}"
    )


# ------------------------------------------------------------------
# Behavioral layer: the real gate (_user_has) must DENY each forbidden tag
# for an hourly user. Mirrors test_permission_matrix.py's ducktyped user +
# request-context pattern (no DB row, no position join -> _effective_perms
# falls back to the role baseline, which is exactly what we assert against).
# ------------------------------------------------------------------

@pytest.fixture(scope="module")
def _app():
    os.environ.pop("PERMISSION_ENFORCE", None)  # default = enforce
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture(autouse=True)
def _request_context(_app):
    # _user_has reads session.get(...) which needs an active request context.
    with _app.test_request_context("/_test_hourly_selfonly"):
        yield


def _hourly_user(role):
    return SimpleNamespace(
        id=1, full_name=f"Test {role}", permission_level=role,
        store_scope="tomball", active=True,
    )


@pytest.mark.parametrize("role", HOURLY_ROLES)
@pytest.mark.parametrize("tag", LEAKED_TAGS)
def test_user_has_denies_leaked_tag_for_hourly(role, tag):
    """Through the real gate: an hourly user is DENIED each leaked driver-admin
    tag (both store-scoped and unscoped checks)."""
    user = _hourly_user(role)
    assert _user_has(user, tag, None) is False
    assert _user_has(user, tag, "tomball") is False


@pytest.mark.parametrize("role", HOURLY_ROLES)
def test_user_has_grants_self_only_tags_for_hourly(role):
    """Belt: the self-only baseline still works — an hourly user CAN use their
    own personal-AI + transcripts surface (we stripped company perms, not the
    self-only ones)."""
    user = _hourly_user(role)
    for tag in SELF_ONLY_TAGS:
        assert _user_has(user, tag, None) is True, (
            f"hourly {role!r} lost self-only tag {tag!r}"
        )
