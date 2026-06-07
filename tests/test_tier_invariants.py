"""Unit tests for app.services.tier_invariants (S6a tier guards).

Pure / DB-free: tier_invariants imports only os + typing and operates on plain
values (User-like dicts or email strings), so this test runs without a database
or Flask app. The escape-hatch env var is read at call time, so tests use
monkeypatch.setenv / delenv freely.
"""
from __future__ import annotations

import pytest

from app.services import tier_invariants as ti
from app.services.tier_invariants import (
    MASOOD_EMAIL,
    MASOOD_PHONE,
    SAM_EMAIL,
    TierInvariantError,
    assert_corporate_both_stores,
    assert_partner_change_allowed,
    assert_tier_invariants,
    can_be_partner,
    is_safe_mode,
    normalize_store_scope,
    partner_identities,
    partner_phones,
)


# ---- small helpers / fixtures ----------------------------------------------
def _user(level=None, store_scope=None, email=None):
    """A minimal User-like dict the pure guards accept."""
    return {"permission_level": level, "store_scope": store_scope, "email": email}


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Default every test to NO env override unless it sets one explicitly."""
    monkeypatch.delenv(ti.PARTNER_EMAILS_ENV, raising=False)
    monkeypatch.delenv(ti.PARTNER_PHONES_ENV, raising=False)


@pytest.fixture
def masood_pinned(monkeypatch):
    """Pin BOTH partners via the escape hatch so we're OUT of safe mode."""
    monkeypatch.setenv(ti.PARTNER_EMAILS_ENV,
                       f"{SAM_EMAIL},masood@cenaskitchen.com")
    return "masood@cenaskitchen.com"


@pytest.fixture
def unpinned_masood(monkeypatch):
    """Simulate the original SAFE MODE: clear Masood's email + phone pins and any
    env override, leaving only Sam pinned (the second slot empty)."""
    monkeypatch.setattr(ti, "MASOOD_EMAIL", None)
    monkeypatch.setattr(ti, "MASOOD_PHONE", None)
    monkeypatch.delenv(ti.PARTNER_EMAILS_ENV, raising=False)
    monkeypatch.delenv(ti.PARTNER_PHONES_ENV, raising=False)


# ---- Masood pinned + default allow-list ------------------------------------
def test_masood_pinned_by_default():
    """Sam pinned Masood 2026-06-07: email + phone are set (the PIN is a
    credential and is NEVER stored in this module)."""
    assert MASOOD_EMAIL == "masood@cenaskitchen.com"
    assert MASOOD_PHONE == "8322832219"
    assert can_be_partner("masood@cenaskitchen.com") is True


def test_default_allowlist_is_both_partners_not_safe_mode():
    """With both pins set (default), allow-list = {Sam, Masood} and NOT safe mode."""
    ids = partner_identities()
    assert ids == frozenset({SAM_EMAIL.lower(), "masood@cenaskitchen.com"})
    assert is_safe_mode() is False


# ---- can_be_partner --------------------------------------------------------
def test_sam_can_be_partner():
    assert can_be_partner(SAM_EMAIL) is True
    # case-insensitive
    assert can_be_partner("SamSahragard@Gmail.com") is True
    assert can_be_partner(_user(email=SAM_EMAIL, level="partner")) is True


def test_third_party_cannot_be_partner():
    assert can_be_partner("randos@example.com") is False
    assert can_be_partner(_user(email="randos@example.com")) is False


def test_no_email_cannot_be_partner():
    assert can_be_partner(None) is False
    assert can_be_partner(_user(email=None)) is False
    assert can_be_partner("") is False


# ---- phone pinning (Masood logs in by phone) -------------------------------
def test_masood_recognized_by_phone_only_user():
    """A User identified by PHONE (no email) is still a pinned partner -- robust
    to a phone-login partner whose User row email is blank/different."""
    masood_by_phone = {"permission_level": "partner", "store_scope": None,
                       "email": None, "phone": "8322832219"}
    assert can_be_partner(masood_by_phone) is True
    # formatted phone variants normalize the same way (last 10 digits)
    assert can_be_partner({"phone": "(832) 283-2219"}) is True
    assert can_be_partner({"phone": "+1 832-283-2219"}) is True


def test_random_phone_not_partner():
    assert can_be_partner({"phone": "5551234567"}) is False
    assert partner_phones() == frozenset({"8322832219"})


def test_removing_masood_by_phone_rejected():
    """Demoting/removing the phone-identified Masood is rejected (he is pinned)."""
    with pytest.raises(TierInvariantError):
        assert_partner_change_allowed(
            actor=_user(email=SAM_EMAIL),
            target={"phone": "832-283-2219", "permission_level": "partner"},
            action="remove",
        )


# ---- 3rd-partner rejected (create/promote) ---------------------------------
def test_third_partner_create_rejected():
    """Promoting/creating a non-allow-listed identity as partner is rejected --
    even in safe mode."""
    with pytest.raises(TierInvariantError):
        assert_partner_change_allowed(
            actor=_user(email=SAM_EMAIL, level="partner"),
            target=_user(email="thirdguy@example.com"),
            action="create",
        )
    with pytest.raises(TierInvariantError):
        assert_partner_change_allowed(
            actor=_user(email=SAM_EMAIL),
            target="thirdguy@example.com",
            action="promote",
        )


def test_allowlisted_create_allowed():
    """Creating Sam (already on the list) as partner is fine -> no raise."""
    assert assert_partner_change_allowed(
        actor=_user(email=SAM_EMAIL),
        target=_user(email=SAM_EMAIL),
        action="create",
    ) is None


def test_masood_create_allowed_once_pinned(masood_pinned):
    """Once Masood is pinned (env), creating him as partner is allowed."""
    assert assert_partner_change_allowed(
        actor=_user(email=SAM_EMAIL),
        target=_user(email=masood_pinned),
        action="promote",
    ) is None
    # ...and a THIRD is still rejected even with two pinned.
    with pytest.raises(TierInvariantError):
        assert_partner_change_allowed(
            actor=_user(email=SAM_EMAIL),
            target="fourth@example.com",
            action="create",
        )


# ---- removing Sam (a pinned partner) rejected ------------------------------
def test_removing_sam_rejected():
    """Demoting/removing a pinned partner (Sam) is rejected -- even in safe mode."""
    for action in ("demote", "remove", "delete", "deactivate"):
        with pytest.raises(TierInvariantError):
            assert_partner_change_allowed(
                actor=_user(email=SAM_EMAIL),
                target=_user(email=SAM_EMAIL, level="partner"),
                action=action,
            )


def test_removing_masood_rejected_once_pinned(masood_pinned):
    """Once Masood is pinned, removing him is also rejected."""
    with pytest.raises(TierInvariantError):
        assert_partner_change_allowed(
            actor=_user(email=SAM_EMAIL),
            target=masood_pinned,
            action="remove",
        )


# ---- SAFE MODE: unpinned slot not locked out -------------------------------
def test_safe_mode_does_not_lock_out_unpinned_slot(unpinned_masood):
    """In safe mode (Masood unpinned), demoting/removing a NON-pinned identity
    is allowed -- we don't hard-block the empty second slot."""
    assert is_safe_mode() is True
    # removing some non-pinned person from the partner tier -> allowed (no raise)
    assert assert_partner_change_allowed(
        actor=_user(email=SAM_EMAIL),
        target=_user(email="someone-not-pinned@example.com", level="partner"),
        action="remove",
    ) is None
    # but the no-3rd-partner rule STILL holds in safe mode (tested above), and
    # Sam is STILL protected in safe mode (tested above).


# ---- escape hatch (ROSTER_PARTNER_EMAILS) ----------------------------------
def test_escape_hatch_replaces_allowlist(monkeypatch):
    """The env var fully REPLACES the built-in list (Sam can be swapped out via
    the documented escape hatch so nobody is locked out)."""
    monkeypatch.setenv(ti.PARTNER_EMAILS_ENV, "newpartner@x.com, second@y.com")
    ids = partner_identities()
    assert ids == frozenset({"newpartner@x.com", "second@y.com"})
    assert is_safe_mode() is False  # two pinned -> out of safe mode
    # the new partners can be partner; old built-in Sam no longer can
    assert can_be_partner("newpartner@x.com") is True
    assert can_be_partner("second@y.com") is True
    assert can_be_partner(SAM_EMAIL) is False
    # creating one of the new partners is allowed; a non-listed one rejected
    assert assert_partner_change_allowed(
        actor="newpartner@x.com", target="second@y.com", action="create") is None
    with pytest.raises(TierInvariantError):
        assert_partner_change_allowed(
            actor="newpartner@x.com", target="third@z.com", action="create")


def test_escape_hatch_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(ti.PARTNER_EMAILS_ENV, "Mixed.Case@Example.COM")
    assert can_be_partner("mixed.case@example.com") is True
    assert partner_identities() == frozenset({"mixed.case@example.com"})


def test_empty_env_falls_back_to_builtin(monkeypatch):
    """An empty / whitespace-only env var is treated as 'no override' -> the
    built-in {Sam, Masood} allow-list."""
    monkeypatch.setenv(ti.PARTNER_EMAILS_ENV, "   ,  ")
    assert partner_identities() == frozenset({SAM_EMAIL.lower(), "masood@cenaskitchen.com"})


# ---- corporate store_scope NULL enforced -----------------------------------
def test_corporate_null_scope_ok():
    """Corporate with store_scope NULL (both stores) passes."""
    assert assert_corporate_both_stores(
        _user(level="corporate", store_scope=None)) is None
    # explicit 'both' token also normalizes to all-stores -> passes
    assert assert_corporate_both_stores(
        _user(level="corporate", store_scope="both")) is None
    assert assert_corporate_both_stores(
        _user(level="corporate", store_scope="")) is None


def test_corporate_single_store_rejected():
    """Corporate pinned to ONE store violates the both-stores invariant."""
    with pytest.raises(TierInvariantError):
        assert_corporate_both_stores(
            _user(level="corporate", store_scope="tomball"))
    with pytest.raises(TierInvariantError):
        assert_corporate_both_stores(
            _user(level="corporate", store_scope="copperfield"))


def test_non_corporate_scope_is_noop():
    """The corporate invariant does not touch non-corporate users -- a GM scoped
    to a single store is fine here."""
    assert assert_corporate_both_stores(
        _user(level="gm", store_scope="tomball")) is None
    assert assert_corporate_both_stores(
        _user(level="km", store_scope="copperfield")) is None
    assert assert_corporate_both_stores(_user(level=None)) is None


def test_normalize_store_scope():
    assert normalize_store_scope(None) is None
    assert normalize_store_scope("") is None
    assert normalize_store_scope("both") is None
    assert normalize_store_scope("BOTH") is None
    assert normalize_store_scope(" Tomball ") == "tomball"
    assert normalize_store_scope("copperfield") == "copperfield"


# ---- aggregate state guard -------------------------------------------------
def test_assert_tier_invariants_corporate():
    with pytest.raises(TierInvariantError):
        assert_tier_invariants(_user(level="corporate", store_scope="tomball"))
    assert assert_tier_invariants(
        _user(level="corporate", store_scope=None)) is None


def test_assert_tier_invariants_stored_third_partner():
    """A stored row claiming partner that isn't on the allow-list is a violation."""
    with pytest.raises(TierInvariantError):
        assert_tier_invariants(_user(level="partner", email="ghost@example.com"))
    # Sam stored as partner is fine
    assert assert_tier_invariants(
        _user(level="partner", email=SAM_EMAIL)) is None
