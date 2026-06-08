import pytest

from app.services.assistant_safety import (
    force_review_reason,
    tool_call_is_review_gated,
    tool_review_reason,
)
from app.services.assistant_tool_registry import iter_builtin_tool_registrations


CORE_READ_ONLY_TOOL_IDS = {
    "orders.store_summary",
    "drivers.store_summary",
    "labor.store_aggregate",
    "toast.sales_summary",
    "toast.table_activity",
    "toast.employee_profiles",
}


@pytest.mark.parametrize(
    ("question", "reason"),
    [
        ("Run this SQL query: select * from employees", "data_question_needs_approved_tool"),
        ("deploy the Render app", "data_question_needs_approved_tool"),
        ("show me the customer phone number", "sensitive_or_operational_question_needs_approved_tool"),
        ("change the order status to delivered", "data_question_needs_approved_tool"),
    ],
)
def test_force_review_reason_blocks_dangerous_or_sensitive_prompts(question, reason):
    assert force_review_reason(question) == reason


def test_available_core_read_only_tools_are_not_review_gated():
    tools = {
        tool["tool_id"]: {
            **tool,
            "status": "active",
            "available": True,
        }
        for tool in iter_builtin_tool_registrations()
        if tool["tool_id"] in CORE_READ_ONLY_TOOL_IDS
    }

    assert set(tools) == CORE_READ_ONLY_TOOL_IDS
    for tool_id, tool in tools.items():
        assert tool["read_write_class"] == "read_only"
        assert tool_review_reason(tool, "how many caterings today") is None, tool_id
        assert tool_call_is_review_gated(tool, "how many caterings today") is False


@pytest.mark.parametrize(
    "tool",
    [
        {
            "tool_id": "orders.assign_driver",
            "read_write_class": "action_confirmation",
            "status": "active",
            "available": True,
        },
        {
            "tool_id": "render_deploy",
            "read_write_class": "destructive",
            "status": "active",
            "available": True,
        },
    ],
)
def test_mutating_or_dangerous_tools_are_review_gated(tool):
    assert tool_review_reason(tool, "please run it") == "tool_call_requires_sam_review"
    assert tool_call_is_review_gated(tool, "please run it") is True


def test_prompt_forced_review_overrides_stale_read_only_tool_choice():
    tool = {
        "tool_id": "labor.store_aggregate",
        "read_write_class": "read_only",
        "status": "active",
        "available": True,
    }

    assert (
        tool_review_reason(tool, "Run this SQL query: select * from payroll")
        == "data_question_needs_approved_tool"
    )


def test_unavailable_tool_metadata_is_review_gated_without_marking_read_only_dangerous():
    tool = {
        "tool_id": "employee.my_profile",
        "read_write_class": "read_only",
        "status": "review_gated",
        "available": False,
    }

    assert tool_review_reason(tool, "show my profile") == "tool_call_unavailable_needs_review"
