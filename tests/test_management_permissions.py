"""Sam 2026-06-08 dashboard-access matrix.

Expo stays in the Management section and keeps a real route-tag baseline, but
its dashboard surface is now deliberately carved down: no Manager dashboard,
Today notifications only, and Operations Corporate Order only.
"""
from app.services.permission_catalog import default_role_map, ROLES
from app.services.permissions import ROLE_PERMISSIONS
from app.services.role_buckets import (
    SECTION_FOR_ROLE, SECTION_MANAGEMENT, SECTION_HOURLY)

DRM = default_role_map()
MGMT = [r["key"] for r in ROLES if SECTION_FOR_ROLE.get(r["key"]) == SECTION_MANAGEMENT]
HOURLY = [r["key"] for r in ROLES if SECTION_FOR_ROLE.get(r["key"]) == SECTION_HOURLY]

MANAGER6 = {
    "corporate", "corporate_chef", "gm", "km",
    "assistant_km", "foh_manager",
}


def test_the_seven_management_positions_are_exactly_sams_list():
    """Guard the set itself: the Management section is exactly Sam's 7 positions."""
    assert set(MGMT) == {
        "corporate", "corporate_chef", "gm", "km",
        "assistant_km", "foh_manager", "expo",
    }, MGMT


def test_dashboard_catalog_matches_sams_role_matrix():
    expected = {
        "dash.today": MANAGER6 | {"partner", "expo", "corporate_driver"},
        "dash.manager": MANAGER6 | {"partner"},
        "dash.catering": MANAGER6 | {"partner", "expo"},
        "dash.operations": MANAGER6 | {"partner", "expo", "corporate_driver"},
        "dash.vendors": MANAGER6 | {"partner", "expo"},
        "dash.kitchen": MANAGER6 | {"partner", "expo", "cook", "corporate_driver"},
        "dash.legal": {"partner"},
        "dash.dev_chat": {"partner"},
    }
    for key, roles in expected.items():
        actual = {role for role, perms in DRM.items() if key in perms}
        assert actual == roles, f"{key} roles drifted: {actual}"


def test_every_management_position_has_a_real_baseline_not_hourly():
    """Each management role's catalog default + route-tag set must be a REAL
    management baseline, not the tiny self-only hourly one."""
    for r in MGMT:
        cat = len(DRM.get(r, set()))
        tags = len(ROLE_PERMISSIONS.get(r, set()))
        assert cat >= 30, "%s catalog default too thin (%d) -- reads as hourly" % (r, cat)
        assert tags >= 20, "%s route tags too thin (%d) -- not a management baseline" % (r, tags)


def test_expo_regression_is_management_not_near_hourly():
    """Direct regression guard for the reported bug (Yessika / Expo). Sam
    2026-06-08 refined it: Expo gets the operational dashboards (today, catering,
    operations, vendors, kitchen) but NOT the Manager dashboard (spec 1.2)."""
    cat = DRM.get("expo", set())
    assert "dash.today" in cat
    assert "dash.manager" not in cat
    assert "dash.operations" in cat
    assert "dash.kitchen" in cat              # keeps its kitchen access too
    assert "dash.catering" in cat
    assert "dash.operations" in cat
    assert "dash.vendors" in cat
    assert len(cat) >= 30, len(cat)
    assert len(ROLE_PERMISSIONS.get("expo", set())) >= 20


def test_hourly_positions_stay_self_only():
    """The fix must NOT bleed into hourly: hourly roles stay self-only (tiny) and
    never get a management dashboard."""
    for r in HOURLY:
        tags = len(ROLE_PERMISSIONS.get(r, set()))
        assert tags <= 6, "%s (hourly) has %d route tags -- should be self-only" % (r, tags)
        assert "dash.manager" not in DRM.get(r, set()), "%s (hourly) should not get dash.manager" % r


def test_corporate_driver_is_not_ezcater_catering_default():
    cat = DRM.get("corporate_driver", set())
    assert {"dash.today", "dash.operations", "dash.kitchen"}.issubset(cat)
    assert "dash.catering" not in cat
    assert not any(key.startswith("catering.") for key in cat)
    assert not any(key.startswith("driver.") for key in cat)
