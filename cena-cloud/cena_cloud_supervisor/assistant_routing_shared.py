"""Shared routing/runtime constants for the Cenas in-app assistant.

The Flask assistant surface and the CK runtime both import this module so
small routing helpers, review labels, store aliases, and provider settings
cannot silently drift between the two processes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .assistant_safety import (
    contextual_followup,
    force_review_reason,
    resolved_question,
)


MAX_QUESTION_CHARS = 2000
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_PROVIDER_TIMEOUT_MS = 20_000
REVIEW_STATUS = "needs_review"

SECRET_DEFAULTS = {
    "GEMINI_API_KEY": [
        r"C:\Users\sam\cena-secrets\gemini_api_key.txt",
        r"C:\Users\sam\cena\.secrets\gemini_api_key.txt",
        r"C:\Users\sam\cena-secrets\google_api_key.txt",
    ],
}

STORE_ALIASES = {
    "1": "copperfield",
    "store_1": "copperfield",
    "store 1": "copperfield",
    "store_3": "copperfield",
    "store 3": "copperfield",
    "uno": "copperfield",
    "uno mas": "copperfield",
    "copperfield": "copperfield",
    "2": "tomball",
    "store_2": "tomball",
    "store 2": "tomball",
    "store_4": "tomball",
    "store 4": "tomball",
    "dos": "tomball",
    "dos mas": "tomball",
    "tomball": "tomball",
}

REVIEW_REASON_LABELS = {
    "not_authenticated": "the user is not signed in",
    "missing_ai_permission": "the current user does not have assistant permission",
    "sensitive_or_operational_question_requires_higher_permission": (
        "the question needs higher operational permission"
    ),
    "sensitive_or_operational_question_needs_approved_tool": (
        "the question needs an approved Cenas data tool"
    ),
    "data_question_requires_higher_permission": (
        "the question needs higher data permission"
    ),
    "data_question_needs_approved_tool": (
        "the question needs an approved Cenas data tool"
    ),
}

SENSITIVE_RE = re.compile(
    r"\b("
    r"password|passcode|token|secret|api key|credential|pin|"
    r"phone|email|address|customer|"
    r"wage|payroll|pay rate|hourly rate|peer pay|"
    r"sales|revenue|eligible_sales|cashsales|noncashsales|guid|"
    r"all employees|all drivers|all stores"
    r")\b",
    re.IGNORECASE,
)
DATA_TOOL_RE = re.compile(
    r"\b("
    r"how many|how amny|count|total|report|summary|list|show me|who|which|"
    r"order|orders|driver|drivers|employee|employees|staff|team|"
    r"schedule|shift|roster|attendance|incident|write up|"
    r"tip|tips|labor|staffing|inventory|vendor|customer|ezcater|catering|caterings|"
    r"late|tracking|tracking link|tracking links|delivery|deliveries|toast|"
    r"table|tables|talbe|floor|opened|open|pay|bonus|fee|fees|"
    r"tool|tools|file|files|filesystem|shell|sql|render|deploy|git|env|"
    r"log|logs|restart|dev chat|sam chat|permission|permissions"
    r")\b",
    re.IGNORECASE,
)
TOAST_SALES_RE = re.compile(
    r"\b("
    r"toast|sales|revenue|net sales|gross sales|"
    r"average order|avg order|labor percent|labor ratio|sales per labor"
    r")\b",
    re.IGNORECASE,
)
TOAST_TABLE_ACTIVITY_RE = re.compile(
    r"\b("
    r"table|tables|talbe|floor|seated|seat|opened|open check|"
    r"check|ticket|waiter|server|opened by|opened it|"
    r"most recent.*open|latest.*open"
    r")\b",
    re.IGNORECASE,
)
TOAST_WEBHOOK_ACTIVITY_RE = re.compile(
    r"\b("
    r"toast\s+webhook|webhooks?|live\s+toast|toast\s+live|"
    r"event|events|order_updated|ordering_schedule|restaurant_availability|"
    r"menus?|stock|packaging|checks?|items?|plates?|payments?|closeouts?|"
    r"rang\s+in|rung\s+in|voids?|closed\s+checks?"
    r")\b",
    re.IGNORECASE,
)
CATERING_ITEM_AGGREGATE_RE = re.compile(
    r"\b("
    r"what\s+items?\s+get\s+ordered\s+most|"
    r"items?\s+(?:get\s+)?ordered\s+most|"
    r"most\s+ordered|"
    r"ordered\s+most|"
    r"most\s+popular|"
    r"popular\s+items?|"
    r"best[-\s]+selling|"
    r"top[-\s]+selling"
    r")\b",
    re.IGNORECASE,
)
TOAST_DATA_FRESHNESS_RE = re.compile(
    r"\bwhen\s+(?:did|was|were)\s+(?:we\s+)?last\b|"
    r"\b(?:last|latest|most\s+recent)\s+(?:toast\s+)?(?:data|webhook|webhooks?|events?|sync|update)\b|"
    r"\btoast\s+(?:data|webhook|webhooks?)\b.*\b(?:fresh|freshness|stale|updated?|sync(?:ed)?|working|connected|last)\b|"
    r"\b(?:fresh|freshness|stale|updated?|sync(?:ed)?|working|connected)\b.*\btoast\s+(?:data|webhook|webhooks?)\b",
    re.IGNORECASE,
)
TOAST_SALES_UNSUPPORTED_SCOPE_RE = re.compile(
    r"\b("
    r"yesterday|last\s+night|previous\s+day|"
    r"last\s+month|this\s+month|month\s+to\s+date|mtd|"
    r"ytd|year\s+to\s+date|last\s+year|this\s+year|"
    r"last\s+\d+\s+days|past\s+\d+\s+days|"
    r"between|from\s+.+\s+to\s+|"
    r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
    r"(?:last|this)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r")\b",
    re.IGNORECASE,
)
CENA_L3_BUSINESS_DATE_SCOPE_RE = re.compile(
    r"\b("
    r"yesterday|last\s+week|previous\s+week|last\s+month|previous\s+month|"
    r"month[-\s]+to[-\s]+date|mtd|week[-\s]+to[-\s]+date|wtd|"
    r"last\s+(?:sun(?:day)?|mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|"
    r"thu(?:rsday)?|fri(?:day)?|sat(?:urday)?)|"
    r"past\s+\d+\s+days?|last\s+\d+\s+days?|between|from\s+\d{1,4}[-/]\d{1,2}|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    r")\b",
    re.IGNORECASE,
)
CENA_L3_BUSINESS_METRIC_RE = re.compile(
    r"\b("
    r"net\s+sales|gross\s+sales|total\s+sales|combined\s+sales|sales\s+total|"
    r"revenue|avg(?:erage)?\s+(?:check|ticket|order)|check\s+average|"
    r"covers?|checks?|tickets?|daypart|lunch|dinner|breakfast|"
    r"busiest|slowest|sales\s+day|labor\s+(?:percent|percentage|ratio|cost)|"
    r"splh|sales\s+per\s+labor"
    r")\b",
    re.IGNORECASE,
)
CENA_L3_BUSINESS_SCOPE_RE = re.compile(
    r"\b("
    r"both\s+stores|across\s+both|combined|all\s+stores|each\s+store|"
    r"by\s+store|compare|comparison|vs\.?|versus|which\s+store|trend"
    r")\b",
    re.IGNORECASE,
)
CENA_L3_ORDER_HISTORY_RE = re.compile(
    r"\b("
    r"orders?|checks?|tickets?|ring\s+up|rang\s+up|rung\s+up|rang|rung|served|"
    r"catering|caterings|ezcater"
    r")\b",
    re.IGNORECASE,
)
TOAST_EMPLOYEE_PROFILE_RE = re.compile(
    r"\b("
    r"toast\s+employee|employee\s+toast|employee\s+profile|profile\s+db|"
    r"personal(?:ized)?\s+db|employee\s+database|employee\s+files?|"
    r"cena_employee_\d+|employee\s+(?:id\s*)?#?\s*\d+|"
    r"toast\s+facts?|server\s+activity|tables\s+served|checks?\s+(?:opened|closed)|"
    r"items?\s+r(?:ang|ung)\s+in|payments?\s+handled"
    r")\b",
    re.IGNORECASE,
)
OPERATIONAL_NOUN_RE = re.compile(
    r"\b("
    r"catering|caterings|order|orders|delivery|deliveries|"
    r"driver|drivers|labor|employee|employees|staff|team|"
    r"table|tables|talbe|floor|"
    r"schedule|schedules|shift|shifts|roster|attendance|"
    r"availability|unavailability|time[- ]off|alarm|reminder|reminders"
    r")\b",
    re.IGNORECASE,
)
FOLLOWUP_RE = re.compile(
    r"\b("
    r"what about|how about|what baout|earlier|morning|afternoon|"
    r"evening|tonight|today|tomorrow|yesterday|last night|this week|"
    r"tomball|dos|dos mas|copperfield|uno|uno mas"
    r")\b",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_ct() -> date:
    # Preserve the app's existing Toast report date handling on Windows hosts.
    return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def stable_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def read_secret(name: str) -> str | None:
    value = (os.getenv(name) or "").strip()
    if value:
        return value
    file_value = (os.getenv(name + "_FILE") or "").strip()
    candidates = [file_value] if file_value else []
    candidates.extend(SECRET_DEFAULTS.get(name, []))
    for raw_path in candidates:
        if not raw_path:
            continue
        try:
            path = Path(raw_path)
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return None


def provider_timeout_ms() -> int:
    raw = (os.getenv("AI_ASSISTANT_PROVIDER_TIMEOUT_MS") or "").strip()
    if not raw:
        return DEFAULT_PROVIDER_TIMEOUT_MS
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_PROVIDER_TIMEOUT_MS
    return max(1_000, min(value, 90_000))


def review_risk_level(reason: str | None) -> str:
    reason_text = (reason or "").casefold()
    if any(term in reason_text for term in ("sensitive", "operational", "data", "permission")):
        return "blocked"
    return "normal"


def review_reason_label(reason: str) -> str:
    return REVIEW_REASON_LABELS.get(reason, "the current permissions or tooling require Sam review")


def queued_answer(reason: str) -> str:
    if reason in {
        "sensitive_or_operational_question_needs_approved_tool",
        "data_question_needs_approved_tool",
    }:
        return "I do not have the approved Cenas data tool for that yet, so I saved it for Sam review."
    if reason == "not_authenticated":
        return "Please sign in first. I saved the question for Sam review."
    return "I can't safely answer that from your current permissions yet, so I saved it for Sam review."


def normalize_store_key(raw_store: Any) -> str:
    value = str(raw_store or "unknown").strip().casefold()
    return STORE_ALIASES.get(value, value or "unknown")


def requested_store(question: str) -> str | None:
    text = str(question or "").casefold()
    for alias, store in STORE_ALIASES.items():
        escaped = re.escape(alias).replace(r"\ ", r"\s+")
        if re.search(rf"\b{escaped}\b", text):
            return store
    return None


def requested_store_list(question: str) -> list[str]:
    text = str(question or "").casefold()
    hits: list[tuple[int, str]] = []
    for alias, store in STORE_ALIASES.items():
        escaped = re.escape(alias).replace(r"\ ", r"\s+")
        match = re.search(rf"\b{escaped}\b", text)
        if match:
            hits.append((match.start(), store))
    ordered: list[str] = []
    for _pos, store in sorted(hits):
        if store not in ordered:
            ordered.append(store)
    return ordered


def toast_table_business_date_from_question(question: str) -> str | None:
    text = str(question or "").casefold()
    today = today_ct()
    if re.search(r"\b(last night|yesterday|previous night)\b", text):
        return (today - timedelta(days=1)).strftime("%Y%m%d")
    if re.search(r"\b(today|tonight)\b", text):
        return today.strftime("%Y%m%d")
    return None


def toast_period_from_question(question: str) -> str:
    text = str(question or "").casefold()
    if "last week" in text or "previous week" in text:
        return "last_week"
    if "yesterday" in text:
        return "yesterday"
    if "this week" in text or re.search(r"\bweek\b", text):
        return "week"
    return "today"


def wants_toast_data_freshness(question: str) -> bool:
    text = str(question or "")
    if not re.search(r"\b(toast|webhook)\b", text, re.IGNORECASE):
        return False
    return bool(
        TOAST_DATA_FRESHNESS_RE.search(text)
        and re.search(r"\b(toast|webhook|data|events?|sync|update)\b", text, re.IGNORECASE)
    )


def has_unsupported_toast_sales_scope(question: str) -> bool:
    text = str(question or "")
    if not TOAST_SALES_RE.search(text):
        return False
    if re.search(r"\b(today|yesterday|this\s+week|last\s+week|previous\s+week)\b", text, re.IGNORECASE):
        return False
    return bool(TOAST_SALES_UNSUPPORTED_SCOPE_RE.search(text))


def wants_cena_l3_business_analytics(question: str) -> bool:
    text = str(question or "")
    if not text.strip() or wants_toast_data_freshness(text):
        return False
    has_date_scope = bool(CENA_L3_BUSINESS_DATE_SCOPE_RE.search(text))
    has_metric = bool(CENA_L3_BUSINESS_METRIC_RE.search(text))
    has_cross_store_scope = bool(CENA_L3_BUSINESS_SCOPE_RE.search(text))

    if has_date_scope and has_metric:
        return True
    if has_date_scope and has_cross_store_scope and re.search(r"\b(sales|revenue)\b", text, re.IGNORECASE):
        return True
    if re.search(r"\b(month[-\s]+to[-\s]+date|mtd)\b", text, re.IGNORECASE) and re.search(
        r"\b(sales|revenue|check|checks?|covers?|orders?)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if has_date_scope and CENA_L3_ORDER_HISTORY_RE.search(text):
        return True
    return False


def wants_toast_sales_summary(question: str) -> bool:
    text = str(question or "")
    if (
        wants_toast_data_freshness(text)
        or has_unsupported_toast_sales_scope(text)
        or wants_cena_l3_business_analytics(text)
        or requested_store(text)
        or TOAST_WEBHOOK_ACTIVITY_RE.search(text)
        or TOAST_EMPLOYEE_PROFILE_RE.search(text)
    ):
        return False
    return bool(TOAST_SALES_RE.search(text))


def wants_toast_table_activity(question: str) -> bool:
    text = str(question or "")
    if TOAST_TABLE_ACTIVITY_RE.search(text) and re.search(
        r"\b(who\s+opened|waiter|server|opened\s+by|opened\s+it)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    return bool(
        TOAST_TABLE_ACTIVITY_RE.search(text)
        and re.search(
            r"\b(tomball|dos|dos mas|copperfield|uno|uno mas|today|"
            r"yesterday|last night|tonight|latest|recent|activity|activities|open|opened)\b",
            text,
            re.IGNORECASE,
        )
    )


def wants_toast_webhook_activity(question: str) -> bool:
    text = str(question or "")
    if re.search(r"\bwhat\s+was\s+on\s+order\b|\border\s+[A-Za-z0-9][A-Za-z0-9_-]{2,}\b", text, re.IGNORECASE):
        return False
    if CATERING_ITEM_AGGREGATE_RE.search(text) and not re.search(
        r"\b(toast|webhooks?|live|events?|checks?|payments?|plates?|closeouts?|voids?|rang|rung|menus?|stock|packaging)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    if (
        re.search(r"\b(catering|caterings|ezcater|in[- ]house|quotes?)\b", text, re.IGNORECASE)
        and not re.search(
            r"\b(toast|webhooks?|rang|rung|checks?|payments?|closeouts?|voids?)\b",
            text,
            re.IGNORECASE,
        )
    ):
        return False
    if TOAST_EMPLOYEE_PROFILE_RE.search(text):
        return False
    if wants_toast_data_freshness(text):
        return True
    return bool(
        TOAST_WEBHOOK_ACTIVITY_RE.search(text)
        and re.search(
            r"\b(toast|webhook|live|events?|orders?|checks?|items?|plates?|"
            r"payments?|closeouts?|closed|rang|rung|void|menus?|stock|packaging)\b",
            text,
            re.IGNORECASE,
        )
    )


def wants_toast_employee_profiles(question: str) -> bool:
    text = str(question or "")
    return bool(
        TOAST_EMPLOYEE_PROFILE_RE.search(text)
        or (
            re.search(r"\b(employee|server|waiter|cashier|staff)\b", text, re.IGNORECASE)
            and re.search(
                r"\b(toast|tables?|checks?|items?|plates?|payments?|rang|rung|served|profile|facts?)\b",
                text,
                re.IGNORECASE,
            )
        )
    )
