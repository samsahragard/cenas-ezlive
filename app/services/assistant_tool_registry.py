"""Registry metadata for the Cenas in-app assistant.

The route layer binds these metadata entries to local handler callables. Keeping
the metadata here lets catalog generation and deterministic routing grow without
adding another hand-written block to ``assistant_routes.py``.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable


BUILTIN_TOOL_REGISTRY: list[dict[str, Any]] = [
    {
        "tool_id": "assistant.general_help",
        "label": "General app help",
        "description": "Answer general navigation, workflow, and policy-style questions without private operational data.",
        "required_permissions": ["ai.ask_claude_personal"],
        "session_types": ["partner", "staff", "employee", "driver"],
        "store_scope": "none",
        "data_class": "general",
        "read_write_class": "read_only",
        "status": "active",
        "priority": 900,
    },
    {
        "tool_id": "employee.my_profile",
        "label": "Own employee profile",
        "description": "Future read-only personal profile answers scoped to the current employee.",
        "required_permissions": ["ai.ask_claude_personal"],
        "session_types": ["employee", "staff"],
        "store_scope": "own_profile",
        "data_class": "personal_profile",
        "read_write_class": "read_only",
        "status": "review_gated",
        "priority": 900,
    },
    {
        "tool_id": "orders.store_summary",
        "label": "Store order summary",
        "description": "Store-scoped order and catering summaries from approved marts.",
        "required_permissions": ["ai.ask_claude", "orders.view"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "operations",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
        "handler": "orders_store_summary",
        "matcher": "orders_store_summary",
        "formatter": "orders_store_summary",
        "priority": 300,
    },
    {
        "tool_id": "drivers.store_summary",
        "label": "Driver summary",
        "description": "Driver performance and delivery summaries from CK driver marts.",
        "required_permissions": ["ai.ask_claude", "drivers.view_roster"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "driver_operations",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
        "handler": "drivers_store_summary",
        "matcher": "drivers_store_summary",
        "formatter": "drivers_store_summary",
        "priority": 320,
    },
    {
        "tool_id": "labor.store_aggregate",
        "label": "Labor aggregate",
        "description": "Aggregate-only labor answers from approved employee marts.",
        "required_permissions": ["ai.ask_claude", "labor.view_store_summary"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "labor_aggregate",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
        "handler": "labor_store_aggregate",
        "matcher": "labor_store_aggregate",
        "formatter": "labor_store_aggregate",
        "priority": 330,
    },
    {
        "tool_id": "toast.sales_summary",
        "label": "Toast sales summary",
        "description": "Read-only Toast Analytics sales, order, labor, and menu aggregates for an approved period.",
        "required_permissions": ["ai.ask_claude", "sales.view_today"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "sales_aggregate",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
        "handler": "toast_sales_summary",
        "matcher": "toast_sales_summary",
        "formatter": "toast_sales_summary",
        "priority": 100,
    },
    {
        "tool_id": "toast.table_activity",
        "label": "Toast table activity",
        "description": "Read-only latest in-store Toast table open activity with sanitized table labels and timestamps.",
        "required_permissions": ["ai.ask_claude", "sales.view_today"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "table_activity",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
        "handler": "toast_table_activity",
        "matcher": "toast_table_activity",
        "formatter": "toast_table_activity",
        "priority": 80,
    },
    {
        "tool_id": "toast.webhook_activity",
        "label": "Toast webhook activity",
        "description": "Read-only live Toast webhook/order/check/item/payment activity from the CK central webhook database.",
        "required_permissions": ["ai.ask_claude", "sales.view_today"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "toast_webhook_activity",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
        "handler": "toast_webhook_activity",
        "matcher": "toast_webhook_activity",
        "formatter": "toast_webhook_activity",
        "priority": 70,
    },
    {
        "tool_id": "toast.employee_profiles",
        "label": "Toast employee profiles",
        "description": "Read-only employee-specific Toast facts from CK's per-employee Toast profile databases.",
        "required_permissions": ["ai.ask_claude", "labor.view_store_summary"],
        "session_types": ["partner", "staff"],
        "store_scope": "partner_all_stores",
        "data_class": "toast_employee_profiles",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
        "handler": "toast_employee_profiles",
        "matcher": "toast_employee_profiles",
        "formatter": "toast_employee_profiles",
        "priority": 60,
    },
]


TOOL_ALIAS_MAP: dict[str, str] = {
    "catering.assign_driver": "orders.assign_driver",
    "catering.reassign_store": "orders.reassign_store",
    "catering.unassign": "orders.unassign_driver",
    "catering.view_drivers": "drivers.roster_summary",
    "driver.approve_mileage": "drivers.approve_mileage",
    "driver.view_all_queue": "drivers.active_delivery_queue",
    "driver.view_earnings": "drivers.earnings_own",
    "driver.view_own_queue": "drivers.active_delivery_queue",
    "emp.add": "employee.add",
    "emp.archive": "employee.deactivate",
    "emp.edit_info": "employee.edit_profile",
    "emp.reset_passcode": "employee.reset_passcode",
    "emp.view_directory": "employees.store_directory",
    "emp.view_perf": "employees.performance_safe_summary",
    "fin.approve_expense": "finance.approve_expense",
    "fin.view_ap": "finance.ap_summary",
    "fin.view_deposits": "finance.deposit_summary",
    "fin.view_instant_deposit": "finance.instant_deposit_status",
    "fin.view_payroll": "finance.payroll_setup_summary",
    "fin.view_pnl": "finance.pnl_summary",
    "fin.view_tips": "finance.tip_pool_summary",
    "incident.create": "manager.create_incident",
    "incident.edit": "manager.edit_incident",
    "incident.view": "manager.incident_summary",
    "kitchen.fresh_edit": "kitchen.update_fresh_food",
    "kitchen.prep_edit": "kitchen.update_prep_item",
    "legal.compliance_cal": "legal.compliance_calendar",
    "legal.upload_docs": "legal.upload_document",
    "legal.view_docs": "legal.document_search",
    "legal.view_insurance": "legal.insurance_summary",
    "legal.view_licenses": "legal.license_summary",
    "maint.close": "maintenance.close_request",
    "maint.submit": "maintenance.create_request",
    "maint.view": "manager.maintenance_summary",
    "manager_log.write": "manager.create_daily_log",
    "perms.assign": "permissions.assign_role",
    "perms.assign_role": "permissions.assign_role",
    "perms.create_role": "permissions.create_role_template",
    "perms.edit_role": "permissions.edit_role_template",
    "perms.override": "permissions.override_user_permission",
    "perms.view": "permissions.permission_catalog",
    "reports.benchmarks": "reports.benchmark_summary",
    "reports.catering": "reports.catering_summary",
    "reports.catering_item_mix": "orders.catering_item_mix",
    "reports.cross_store": "reports.cross_store_summary",
    "reports.forecasts": "reports.forecast_summary",
    "reports.labor": "reports.labor_summary",
    "reports.marketing": "reports.marketing_summary",
    "reports.sales": "reports.sales_summary",
    "team.notify": "manager.send_team_notification",
    "timeoff.approve": "schedule.approve_time_off",
    "toast_live_tables": "toast.table_activity",
}


def canonical_tool_id(tool_id: str) -> str:
    current = str(tool_id or "").strip()
    seen: set[str] = set()
    while current in TOOL_ALIAS_MAP and current not in seen:
        seen.add(current)
        current = TOOL_ALIAS_MAP[current]
    return current


def iter_tool_aliases() -> Iterable[tuple[str, str]]:
    for alias, canonical in sorted(TOOL_ALIAS_MAP.items()):
        yield alias, canonical


def iter_builtin_tool_registrations() -> Iterable[dict[str, Any]]:
    for entry in BUILTIN_TOOL_REGISTRY:
        yield deepcopy(entry)
