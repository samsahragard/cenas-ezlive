"""Sam 2026-06-07: the Team Roster is the SOURCE OF TRUTH. EVERY position in the
Management SECTION must grant the management profile (dashboards + a real
permission baseline) -- not read as near-hourly.

The reported bug was EXPO: it's in the Management section (role_buckets) but was
left out of the catalog manager tier (MGR_UP/GM_UP in permission_catalog.py), so
its default was only dash.kitchen (9 perms, vs 46-87 for the other 6 management
roles). A person added as Expo therefore got ~no permissions.

These tests pin ALL 7 management positions (Corporate, Corporate Chef, GM, KM,
Assistant KM, FOH Manager, Expo) to the management profile, and pin hourly to
self-only, so neither side can silently regress.
"""
from app.services.permission_catalog import default_role_map, ROLES
from app.services.permissions import ROLE_PERMISSIONS
from app.services.role_buckets import (
    SECTION_FOR_ROLE, SECTION_MANAGEMENT, SECTION_HOURLY)

DRM = default_role_map()
MGMT = [r["key"] for r in ROLES if SECTION_FOR_ROLE.get(r["key"]) == SECTION_MANAGEMENT]
HOURLY = [r["key"] for r in ROLES if SECTION_FOR_ROLE.get(r["key"]) == SECTION_HOURLY]

# The "run a shift" core: any real management profile must grant these dashboards.
CORE_MGMT_DASHBOARDS = {"dash.today", "dash.manager"}


def test_the_seven_management_positions_are_exactly_sams_list():
    """Guard the set itself: the Management section is exactly Sam's 7 positions."""
    assert set(MGMT) == {
        "corporate", "corporate_chef", "gm", "km",
        "assistant_km", "foh_manager", "expo",
    }, MGMT


def test_every_management_position_gets_the_manager_dashboards():
    """All 7 management-section roles grant dash.today + dash.manager by catalog
    default -- the exact thing Expo was missing."""
    for r in MGMT:
        missing = CORE_MGMT_DASHBOARDS - DRM.get(r, set())
        assert not missing, "%s (management section) missing %s" % (r, missing)


def test_every_management_position_has_a_real_baseline_not_hourly():
    """Each management role's catalog default + route-tag set must be a REAL
    management baseline, not the tiny self-only hourly one."""
    for r in MGMT:
        cat = len(DRM.get(r, set()))
        tags = len(ROLE_PERMISSIONS.get(r, set()))
        assert cat >= 30, "%s catalog default too thin (%d) -- reads as hourly" % (r, cat)
        assert tags >= 20, "%s route tags too thin (%d) -- not a management baseline" % (r, tags)


def test_expo_regression_is_management_not_near_hourly():
    """Direct regression guard for the reported bug (Yessika / Expo)."""
    cat = DRM.get("expo", set())
    assert "dash.manager" in cat
    assert "dash.today" in cat
    assert "dash.kitchen" in cat              # keeps its kitchen access too
    assert len(cat) >= 30, len(cat)
    assert len(ROLE_PERMISSIONS.get("expo", set())) >= 20


def test_hourly_positions_stay_self_only():
    """The fix must NOT bleed into hourly: hourly roles stay self-only (tiny) and
    never get a management dashboard."""
    for r in HOURLY:
        tags = len(ROLE_PERMISSIONS.get(r, set()))
        assert tags <= 6, "%s (hourly) has %d route tags -- should be self-only" % (r, tags)
        assert "dash.manager" not in DRM.get(r, set()), "%s (hourly) should not get dash.manager" % r
