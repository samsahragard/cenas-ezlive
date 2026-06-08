import ast
from pathlib import Path

import pytest

from app.services import assistant_routing_shared as shared
from app.services import assistant_operational_tools
from app.services import toast_table_activity
from app.services.assistant_handlers import orders as order_handlers
from app.web import assistant_routes as routes
from scripts import assistant_ck_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]
ROUTING_SOURCES = [
    ROOT / "app" / "web" / "assistant_routes.py",
    ROOT / "scripts" / "assistant_ck_runtime.py",
]

SHARED_ALIAS_NAMES = {
    "_DATA_TOOL_RE",
    "_DEFAULT_GEMINI_MODEL",
    "_FOLLOWUP_RE",
    "_MAX_QUESTION_CHARS",
    "_OPERATIONAL_NOUN_RE",
    "_REVIEW_STATUS",
    "_SECRET_DEFAULTS",
    "_SENSITIVE_RE",
    "_TOAST_DATA_FRESHNESS_RE",
    "_TOAST_EMPLOYEE_PROFILE_RE",
    "_TOAST_SALES_RE",
    "_TOAST_SALES_UNSUPPORTED_SCOPE_RE",
    "_TOAST_TABLE_ACTIVITY_RE",
    "_TOAST_WEBHOOK_ACTIVITY_RE",
    "_has_unsupported_toast_sales_scope",
    "_now_iso",
    "_provider_timeout_ms",
    "_queued_answer",
    "_read_secret",
    "_requested_store",
    "_review_reason_label",
    "_review_risk_level",
    "_stable_hash",
    "_toast_period_from_question",
    "_toast_table_business_date_from_question",
    "_today_ct",
    "_wants_toast_data_freshness",
    "_wants_toast_employee_profiles",
    "_wants_toast_sales_summary",
    "_wants_toast_table_activity",
    "_wants_toast_webhook_activity",
}


SHARED_OBJECTS = {
    "_DATA_TOOL_RE": "DATA_TOOL_RE",
    "_DEFAULT_GEMINI_MODEL": "DEFAULT_GEMINI_MODEL",
    "_FOLLOWUP_RE": "FOLLOWUP_RE",
    "_MAX_QUESTION_CHARS": "MAX_QUESTION_CHARS",
    "_OPERATIONAL_NOUN_RE": "OPERATIONAL_NOUN_RE",
    "_REVIEW_STATUS": "REVIEW_STATUS",
    "_SECRET_DEFAULTS": "SECRET_DEFAULTS",
    "_SENSITIVE_RE": "SENSITIVE_RE",
    "_TOAST_DATA_FRESHNESS_RE": "TOAST_DATA_FRESHNESS_RE",
    "_TOAST_EMPLOYEE_PROFILE_RE": "TOAST_EMPLOYEE_PROFILE_RE",
    "_TOAST_SALES_RE": "TOAST_SALES_RE",
    "_TOAST_SALES_UNSUPPORTED_SCOPE_RE": "TOAST_SALES_UNSUPPORTED_SCOPE_RE",
    "_TOAST_TABLE_ACTIVITY_RE": "TOAST_TABLE_ACTIVITY_RE",
    "_TOAST_WEBHOOK_ACTIVITY_RE": "TOAST_WEBHOOK_ACTIVITY_RE",
    "_has_unsupported_toast_sales_scope": "has_unsupported_toast_sales_scope",
    "_now_iso": "now_iso",
    "_provider_timeout_ms": "provider_timeout_ms",
    "_queued_answer": "queued_answer",
    "_read_secret": "read_secret",
    "_requested_store": "requested_store",
    "_review_reason_label": "review_reason_label",
    "_review_risk_level": "review_risk_level",
    "_stable_hash": "stable_hash",
    "_toast_period_from_question": "toast_period_from_question",
    "_toast_table_business_date_from_question": "toast_table_business_date_from_question",
    "_today_ct": "today_ct",
    "_wants_toast_data_freshness": "wants_toast_data_freshness",
    "_wants_toast_employee_profiles": "wants_toast_employee_profiles",
    "_wants_toast_sales_summary": "wants_toast_sales_summary",
    "_wants_toast_table_activity": "wants_toast_table_activity",
    "_wants_toast_webhook_activity": "wants_toast_webhook_activity",
}


def _module_local_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


@pytest.mark.parametrize("alias, shared_name", sorted(SHARED_OBJECTS.items()))
def test_route_and_runtime_use_shared_routing_objects(alias, shared_name):
    expected = getattr(shared, shared_name)

    assert getattr(routes, alias) is expected
    assert getattr(runtime, alias) is expected


@pytest.mark.parametrize("path", ROUTING_SOURCES, ids=lambda path: str(path.relative_to(ROOT)))
def test_shared_routing_objects_are_not_redeclared_locally(path):
    local_defs = _module_local_defs(path)

    assert not (local_defs & SHARED_ALIAS_NAMES)


def test_shared_store_aliases_drive_both_route_layers():
    assert routes._requested_store("sales at dos mas") == "tomball"
    assert runtime._requested_store("sales at dos mas") == "tomball"
    assert runtime._requested_store_list("Copperfield vs Tomball") == ["copperfield", "tomball"]
    assert routes._requested_store is shared.requested_store
    assert runtime._requested_store_list is shared.requested_store_list


def test_shared_store_aliases_drive_tool_handlers():
    assert order_handlers._STORE_ALIASES is shared.STORE_ALIASES
    assert order_handlers._normalize_store("dos mas") == "tomball"
    assert order_handlers._normalize_store("uno") == "copperfield"

    assert toast_table_activity.STORE_ALIASES is shared.STORE_ALIASES
    assert toast_table_activity.normalize_location("all") is None
    assert toast_table_activity.normalize_location("both") is None
    assert toast_table_activity.normalize_location("dos") == "tomball"
    assert toast_table_activity.normalize_location("uno mas") == "copperfield"

    assert assistant_operational_tools.STORE_ALIASES is shared.STORE_ALIASES
    assert assistant_operational_tools._store("both") is None
    assert assistant_operational_tools._store("dos mas") == "tomball"


def test_runtime_forced_review_returns_without_notice_model(tmp_path, monkeypatch):
    db_path = tmp_path / "assistant_review.sqlite"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setattr(
        runtime,
        "_gemini_review_notice",
        lambda *_: (_ for _ in ()).throw(AssertionError("review notice must stay off request path")),
    )
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("forced review must not call answer model")),
    )

    payload, status = runtime._answer({
        "question": "Run this SQL query: select * from employees",
        "principal": {
            "kind": "partner",
            "role": "partner",
            "permissions": ["*"],
            "can_ask_personal": True,
            "can_ask_operational": True,
        },
        "tools": [],
        "tool_data": {},
        "route_path": "deterministic",
        "routed_tool_id": "labor.store_aggregate",
    })

    assert status == 200
    assert payload["queued"] is True
    assert payload["route_path"] == "review"
    assert payload["routed_tool_id"] is None
    assert payload["reason"] == "data_question_needs_approved_tool"
    assert "review_notice_model" not in payload
