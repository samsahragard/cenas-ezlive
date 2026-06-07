"""Unit tests for app.services.role_buckets (S2 section classifier).

Pure / DB-free: role_buckets imports only the pure dict + position_role from
permission_catalog, so this test runs without a database or Flask app.
"""
from app.services.permission_catalog import ROLES
from app.services.role_buckets import (
    SECTION_DRIVER,
    SECTION_FOR_ROLE,
    SECTION_HOURLY,
    SECTION_MANAGEMENT,
    SECTIONS,
    section_for_position,
    section_for_role,
)

# The intentional tier-above role -> section None. corporate moved INTO Management
# (Sam 2026-06-07); only partner stays section-less.
TIER_ABOVE = {"partner"}


def test_every_catalog_role_maps_to_one_section_or_is_tier_above():
    """Each permission_catalog role either maps to exactly one real section or
    is an intentional tier-above (partner/corporate) -> None. No role is
    unclassified by accident."""
    for r in ROLES:
        key = r["key"]
        sec = section_for_role(key)
        if key in TIER_ABOVE:
            assert sec is None, f"{key} should be tier-above (None), got {sec!r}"
        else:
            assert sec in SECTIONS, (
                f"{key} must map to exactly one section, got {sec!r}"
            )


def test_only_partner_is_tier_above():
    """partner is intentionally ABSENT from SECTION_FOR_ROLE (tier-above -> None);
    corporate is now a Management-section role (Sam 2026-06-07)."""
    assert "partner" not in SECTION_FOR_ROLE
    assert section_for_role("partner") is None
    assert "corporate" in SECTION_FOR_ROLE
    assert section_for_role("corporate") == SECTION_MANAGEMENT


def test_management_roles():
    assert section_for_role("gm") == SECTION_MANAGEMENT
    assert section_for_role("km") == SECTION_MANAGEMENT
    assert section_for_role("foh_manager") == SECTION_MANAGEMENT
    assert section_for_role("assistant_km") == SECTION_MANAGEMENT
    assert section_for_role("corporate_chef") == SECTION_MANAGEMENT
    # expo -> management (Sam's approved bucket)
    assert section_for_role("expo") == SECTION_MANAGEMENT
    # corporate -> management (Sam 2026-06-07: addable in the Management section)
    assert section_for_role("corporate") == SECTION_MANAGEMENT


def test_hourly_roles():
    for k in ("bartender", "busser", "cashier", "cook", "server", "well", "host"):
        assert section_for_role(k) == SECTION_HOURLY, k
    # explicit spot-checks called out in the task
    assert section_for_role("well") == SECTION_HOURLY
    assert section_for_role("host") == SECTION_HOURLY
    assert section_for_role("server") == SECTION_HOURLY
    assert section_for_role("cook") == SECTION_HOURLY


def test_driver_role():
    assert section_for_role("corporate_driver") == SECTION_DRIVER


def test_unknown_and_none_role():
    assert section_for_role("totally_made_up") is None
    assert section_for_role("") is None
    assert section_for_role(None) is None


def test_case_and_whitespace_tolerant():
    assert section_for_role("  GM  ") == SECTION_MANAGEMENT
    assert section_for_role("Expo") == SECTION_MANAGEMENT
    assert section_for_role("WELL") == SECTION_HOURLY


def test_section_for_position():
    # position_role() resolves canonical position names then we classify.
    assert section_for_position("GM") == SECTION_MANAGEMENT
    assert section_for_position("KM") == SECTION_MANAGEMENT
    assert section_for_position("FOH Manager") == SECTION_MANAGEMENT
    assert section_for_position("Hostess") == SECTION_HOURLY  # Hostess -> host
    assert section_for_position("Well") == SECTION_HOURLY
    assert section_for_position("Server") == SECTION_HOURLY
    assert section_for_position("Cook") == SECTION_HOURLY
    assert section_for_position("Prep") == SECTION_HOURLY
    assert section_for_position("Dishwasher") == SECTION_HOURLY
    # Corporate is now a Management-section role (Sam 2026-06-07); Partner stays tier-above.
    assert section_for_position("Corporate") == SECTION_MANAGEMENT
    assert section_for_position("Partner") is None
    # unknown / None
    assert section_for_position("nope") is None
    assert section_for_position(None) is None


def test_every_mapped_value_is_a_real_section():
    assert set(SECTION_FOR_ROLE.values()) <= set(SECTIONS)
