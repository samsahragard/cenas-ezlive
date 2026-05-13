"""Phase 1 / Block 5 — registry shape tests.

Asserts every rule defined in app.services.anomaly_rules is registered
properly: valid bucket, valid severity, non-empty surfaces +
audience_roles + action_text, and that each rule function is callable
and returns a list when given a Session.
"""
from __future__ import annotations

import pytest

import app.services.anomaly_rules  # noqa: F401 — register-on-import
from app.services.anomaly_engine import REGISTRY


VALID_BUCKETS = {"every_5m", "every_15m", "hourly",
                 "daily", "weekly", "on_write"}
VALID_SEVERITIES = {"info", "warn", "alert"}


def test_registry_has_expected_rule_count():
    """32 rules total per samai's anomaly_rules spec — 2 seed rules in
    anomaly_engine.py + 30 in anomaly_rules.py."""
    # We check >= 30 (the floor) — extra rules added later are fine.
    assert len(REGISTRY) >= 30, f"only {len(REGISTRY)} rules registered"


@pytest.mark.parametrize("rule_name", sorted(REGISTRY.keys()))
def test_rule_spec_shape(rule_name):
    spec = REGISTRY[rule_name]
    assert spec.name == rule_name, "name mismatch in REGISTRY"
    assert spec.bucket in VALID_BUCKETS, f"bad bucket {spec.bucket!r}"
    assert spec.severity_default in VALID_SEVERITIES, \
        f"bad severity {spec.severity_default!r}"
    assert spec.surfaces, "surfaces list must be non-empty"
    assert spec.audience_roles, "audience_roles list must be non-empty"
    assert spec.action_text and spec.action_text.strip(), \
        "action_text must be non-empty"
    assert callable(spec.fn), "fn must be callable"


@pytest.mark.parametrize("rule_name", sorted(REGISTRY.keys()))
def test_rule_function_returns_list(rule_name, db_session):
    """Every rule function must accept a Session and return a list
    (possibly empty for stubs or when no condition is met)."""
    spec = REGISTRY[rule_name]
    out = spec.fn(db_session)
    assert isinstance(out, list), \
        f"rule {rule_name} returned {type(out).__name__}, expected list"


def test_seed_rules_still_registered():
    """The 2 rules shipped in Block 1 seed must stay in REGISTRY after
    anomaly_rules.py import."""
    assert "orders.no_driver_30min_before" in REGISTRY
    assert "orders.late_delivery" in REGISTRY
