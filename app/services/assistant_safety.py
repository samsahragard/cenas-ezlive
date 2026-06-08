"""Shared assistant routing safety helpers.

These helpers are imported by both the Render web proxy and the CK runtime so
the safety boundary stays identical on both sides of the assistant hop.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_EXCLUDED_ACTION_RE = re.compile(
    r"\b("
    r"exclude|dev\.[a-z0-9_.-]+|"
    r"shell|terminal|powershell|cmd(?:\.exe)?|command(?:\s+line)?|"
    r"deploy|render\s+(?:deploy|env|environment|restart)|"
    r"environment\s+variable|env\s+var|set\s+env|"
    r"restart(?:\s+the)?\s+(?:agent|server|app|runtime)|"
    r"read\s+(?:the\s+)?file|show\s+(?:me\s+)?(?:the\s+)?file|"
    r"list\s+(?:the\s+)?files|filesystem|"
    r"git\s+(?:pull|push|status|checkout|reset|merge)|"
    r"web\s+search|search\s+the\s+web|google\s+for|"
    r"whatsapp|telegram|"
    r"sql|select\s+\*|query\s+database|"
    r"delete\s+(?:the\s+)?file|remove\s+(?:the\s+)?file"
    r")\b",
    re.IGNORECASE,
)

_HARD_SENSITIVE_RE = re.compile(
    r"\b("
    r"password|passcode|token|secret|api\s+key|credential|pin|"
    r"phone|email|address|customer|"
    r"wage|payroll|pay\s+rate|hourly\s+rate|peer\s+pay|"
    r"eligible_sales|cashsales|noncashsales|guid|"
    r"all\s+employees|all\s+drivers|all\s+stores"
    r")\b",
    re.IGNORECASE,
)

_WRITE_ACTION_RE = re.compile(
    r"\b("
    r"update|change|set|mark|assign|reassign|unassign|send|email|"
    r"create|add|delete|remove|approve|deny|publish|cancel|void|"
    r"refresh|resync|sync"
    r")\b",
    re.IGNORECASE,
)

_WRITE_OBJECT_RE = re.compile(
    r"\b("
    r"order|orders|status|driver|quote|email|shift|schedule|"
    r"time[- ]off|availability|permission|role|expense|"
    r"tracking|ezcater"
    r")\b",
    re.IGNORECASE,
)

_UNSUPPORTED_SALES_SCOPE_RE = re.compile(
    r"\b(sales|revenue|net\s+sales|gross\s+sales|toast\s+analytics)\b"
    r".*\b(last\s+night|previous\s+day)\b|"
    r"\b(last\s+night|previous\s+day)\b"
    r".*\b(sales|revenue|net\s+sales|gross\s+sales|toast\s+analytics)\b",
    re.IGNORECASE,
)


def force_review_reason(question: str) -> str | None:
    """Return a review reason for prompts that must never route to read tools."""
    text = str(question or "").strip()
    if not text:
        return None
    if _EXCLUDED_ACTION_RE.search(text):
        return "data_question_needs_approved_tool"
    if _HARD_SENSITIVE_RE.search(text):
        return "sensitive_or_operational_question_needs_approved_tool"
    if _UNSUPPORTED_SALES_SCOPE_RE.search(text):
        return "data_question_needs_approved_tool"
    if _WRITE_ACTION_RE.search(text) and _WRITE_OBJECT_RE.search(text):
        return "data_question_needs_approved_tool"
    return None


def contextual_followup(question: str, previous_question: str) -> bool:
    """Broad contextual routing is disabled until the HOLD regressions pass.

    The UI may still send previous_question/previous_answer for audit and for
    narrow runtime helpers that read the prior answer directly, but routing must
    be based on the current prompt alone.
    """
    return False


def resolved_question(question: str, previous_question: str = "") -> str:
    """Question text used for routing/safety checks."""
    del previous_question
    return str(question or "").strip()


_READ_ONLY_CLASS = "read_only"
_DANGEROUS_TOOL_CLASSES = frozenset(
    {
        "action_confirmation",
        "write",
        "write_action",
        "mutation",
        "mutating",
        "destructive",
        "dangerous",
    }
)
_UNAVAILABLE_TOOL_STATUSES = frozenset(
    {
        "blocked",
        "disabled",
        "draft",
        "inactive",
        "review_gated",
        "stub",
    }
)


def tool_review_reason(tool: Mapping[str, Any] | None, question: str = "") -> str | None:
    """Return why a proposed tool call must be held for review.

    This is intentionally metadata-only: the route/runtime layers own tool
    execution and permission scoping, while this helper pins the safety rule
    that approved read-only tools are not review-gated merely for being data
    tools. Forced prompt-level review still wins over any stale routed tool.
    """
    forced = force_review_reason(question)
    if forced:
        return forced
    if not isinstance(tool, Mapping):
        return "tool_call_missing_metadata"

    read_write_class = str(tool.get("read_write_class") or "").strip().casefold()
    if read_write_class and read_write_class != _READ_ONLY_CLASS:
        return "tool_call_requires_sam_review"
    if read_write_class in _DANGEROUS_TOOL_CLASSES:
        return "tool_call_requires_sam_review"

    if tool.get("available") is False:
        return "tool_call_unavailable_needs_review"
    status = str(tool.get("status") or "").strip().casefold()
    if tool.get("available") is not True and status in _UNAVAILABLE_TOOL_STATUSES:
        return "tool_call_unavailable_needs_review"
    return None


def tool_call_is_review_gated(tool: Mapping[str, Any] | None, question: str = "") -> bool:
    """Boolean wrapper for callers/tests that only need the gate verdict."""
    return tool_review_reason(tool, question) is not None
