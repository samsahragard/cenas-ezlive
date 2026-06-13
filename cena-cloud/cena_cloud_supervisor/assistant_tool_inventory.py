"""Partner-level Cenas AI tool inventory catalog.

These entries are catalog access records, not executable implementations by
themselves. The assistant runtime still only runs tools that have approved,
sanitized code paths; catalog-only tools remain auditable/visible for partner
level and fall back to review until implemented.
"""
from __future__ import annotations

from typing import Any, Iterable


PARTNER_TOOL_IDS = frozenset(
    """
agent_restart
assistant.approved_answer_lookup
assistant.audit_lookup_self
assistant.feedback_capture
assistant.handoff_to_sam
assistant.permission_explain
assistant.review_queue_submit
assistant.tool_discovery
attendance.callout_summary
attendance.edit
attendance.late_summary
attendance.manager_board_summary
attendance.missed_punch_summary
attendance.no_show_summary
attendance.view
availability.manage
availability.update_employee
catering.assign_driver
catering.driver_perf
catering.edit
catering.print_pdf
catering.reassign_store
catering.revenue
catering.unassign
catering.view
catering.view_drivers
dash.catering
dash.cena_chat
dash.dev_chat
dash.kitchen
dash.legal
dash.manager
dash.operations
dash.today
dash.vendors
dev.agent_status
dev.archive_dev_chat
dev.assistant_policy_rules
dev.assistant_review_queue
dev.assistant_tool_catalog_snapshot
dev.cena_audit_log
dev.cleanup_attachments
dev.dev_chat_post
dev.dev_chat_read
dev.docck_health
dev.git_status
dev.github_pr_summary
dev.post_dev_chat
dev.render_deploy_status
dev.render_env_key_presence
dev.render_logs_read
dev.restart_agent
dev.run_git_command
dev.run_prod_sync
dev.run_script
dev.sentry_issue_summary
dev.set_render_env
dev.toggle_automation
dev.trigger_render_deploy
driver.approve_mileage
driver.submit_mileage
driver.update_others
driver.update_own
driver.view_all_queue
driver.view_earnings
driver.view_own_queue
drivers.active_delivery_queue
drivers.approve_delivery_request
drivers.approve_mileage
drivers.assignment_status
drivers.bonus_summary
drivers.close_paycheck
drivers.decline_delivery_request
drivers.delivery_completion_summary
drivers.driver_data_center_summary
drivers.driver_lookup
drivers.driver_profile_read
drivers.earnings_manager_safe
drivers.earnings_own
drivers.five_star_summary
drivers.live_location_summary
drivers.mileage_summary
drivers.parking_cost_summary
drivers.parking_receipt_summary
drivers.photo_completion_summary
drivers.reset_passcode
drivers.roster_summary
drivers.route_history_safe
drivers.score_summary
drivers.suspend_driver
drivers.tier_summary
drivers.unassigned_orders
drivers.update_profile
drivers.verify_parking
emp.add
emp.archive
emp.edit_dd
emp.edit_info
emp.edit_perf
emp.edit_tax
emp.edit_wages
emp.reset_passcode
emp.upload_onboarding
emp.view_dd
emp.view_directory
emp.view_onboarding
emp.view_perf
emp.view_tax_full
emp.view_tax_masked
emp.view_wages
employee.add
employee.deactivate
employee.edit_profile
employee.link_to_toast
employee.link_to_user
employee.my_attendance_summary
employee.my_availability.read
employee.my_contact.read
employee.my_day_breakdown
employee.my_open_shifts
employee.my_pay_summary
employee.my_performance_summary
employee.my_positions.read
employee.my_profile.read
employee.my_rank_explain
employee.my_rank_summary
employee.my_recent_shifts
employee.my_schedule.today
employee.my_schedule.week
employee.my_shift_alarm_settings
employee.my_stores.read
employee.my_time_off.status
employee.my_training.read
employee.reset_passcode
employees.link_status_summary
employees.needs_review_summary
employees.passcode_status_summary
employees.performance_safe_summary
employees.profile_completion_summary
employees.roster_gap_summary
employees.store_attendance_summary
employees.store_availability_read
employees.store_directory
employees.store_positions
employees.store_profile_lookup
employees.store_schedule_read
employees.store_time_off_summary
employees.store_training_summary
employees.toast_link_summary
equip.add
equip.edit
equip.view
equip.view_warranty
ezcater_get_order_full_details
fetch_url
file_delete
fin.approve_expense
fin.config_accounts
fin.config_sales_cat
fin.config_tips
fin.edit_ap
fin.edit_payroll
fin.view_accounts
fin.view_ap
fin.view_deposits
fin.view_instant_deposit
fin.view_payroll
fin.view_pnl
fin.view_tips
finance.ap_summary
finance.approve_expense
finance.deposit_summary
finance.instant_deposit_status
finance.mark_invoice_paid
finance.payroll_setup_summary
finance.pnl_summary
finance.tip_pool_summary
finance.vendor_payables_summary
get_current_todo
incident.create
incident.edit
incident.view
journal_read
journal_write
kitchen.catering_prep_breakdown
kitchen.fresh_edit
kitchen.fresh_food_recent
kitchen.fresh_food_today
kitchen.fresh_view
kitchen.inventory
kitchen.inventory_snapshot
kitchen.order_prep_needs
kitchen.prep_edit
kitchen.prep_entries_by_day
kitchen.prep_item_lookup
kitchen.prep_list_today
kitchen.prep_view
kitchen.recipe_lookup
kitchen.recipe_search
kitchen.recipes_view
kitchen.update_fresh_food
kitchen.update_prep_item
labor.store_aggregate
legal.company_structure_summary
legal.compliance_cal
legal.compliance_calendar
legal.document_search
legal.edit_insurance
legal.edit_licenses
legal.edit_matter
legal.edit_meta
legal.insurance_summary
legal.license_summary
legal.manage_notices
legal.matter_lookup
legal.matter_summary
legal.upload_docs
legal.upload_document
legal.view_docs
legal.view_insurance
legal.view_licenses
legal.view_notices
list_dir
maint.approve_spend
maint.close
maint.edit
maint.submit
maint.view
maintenance.close_request
maintenance.create_request
manager_log.write
manager.close_incident
manager.close_of_day_audit_summary
manager.create_daily_log
manager.create_incident
manager.daily_goals_summary
manager.daily_log_search
manager.daily_log_summary
manager.edit_incident
manager.employee_counseling_summary
manager.incident_lookup
manager.incident_summary
manager.maintenance_summary
manager.pre_shift_checklist_summary
manager.recipe_page_search
manager.send_team_notification
manager.shift_handoff_summary
manager.staff_feedback_summary
manager.supply_request_summary
manager.training_record_summary
orders.assign_driver
orders.catering_by_status
orders.catering_by_store
orders.catering_count
orders.catering_driver_assignment_summary
orders.catering_fees_summary
orders.catering_item_mix
orders.catering_late_risk
orders.catering_live_tracking
orders.catering_needs_driver
orders.catering_next_30_days
orders.catering_order_items_safe
orders.catering_order_lookup
orders.catering_payout_safe_summary
orders.catering_pdf_status
orders.catering_returning_customers_aggregate
orders.catering_today
orders.catering_tomorrow
orders.catering_tracking_missing
orders.catering_uuid_status
orders.catering_week
orders.create_in_house_quote
orders.in_house_quote_lookup
orders.in_house_quotes_summary
orders.mark_delivered
orders.mark_picked_up
orders.reassign_store
orders.refresh_ezcater_tracking
orders.run_ezcater_probe
orders.run_pwck_assignment
orders.send_quote_email
orders.unassign_driver
orders.update_status
orders.update_tracking_url
permissions.access_requests
permissions.approve_access_request
permissions.assign_role
permissions.create_role_template
permissions.denial_summary
permissions.deny_access_request
permissions.edit_role_template
permissions.my_permissions
permissions.override_summary
permissions.override_user_permission
permissions.permission_catalog
permissions.role_catalog
permissions.role_change_risk_check
permissions.user_audit_log
permissions.user_lookup
perms.assign
perms.assign_role
perms.create_role
perms.delete_role
perms.edit_role
perms.override
perms.view
post_to_dev_chat
post_to_sam_chat
produce.bulk_insert_orders
produce.ingest_vendor_emails
produce.scan_vendor_inbox
query_database
read_file
read_hub_inbox
remove_participant
render_deploy
render_env_get
render_env_set
reports.benchmark_summary
reports.benchmarks
reports.catering
reports.catering_item_mix
reports.catering_summary
reports.cross_store
reports.cross_store_summary
reports.driver_performance
reports.employee_performance_safe
reports.export
reports.export_prepare
reports.forecast_summary
reports.forecasts
reports.giftcard
reports.labor
reports.labor_by_store
reports.labor_summary
reports.marketing
reports.marketing_summary
reports.menu
reports.sales
reports.sales_by_channel
reports.sales_by_store
reports.sales_summary
reports.team_roster_summary
resolve_catering_order
resolve_employee
resolve_manager_log
resolve_menu_item
resolve_vendor
run_git
schedule.alarm_pending_summary
schedule.approve_shift_offer
schedule.approve_shift_swap
schedule.approve_time_off
schedule.availability_conflicts
schedule.configure
schedule.create_shift
schedule.delete_shift
schedule.deny_time_off
schedule.edit_shift
schedule.open_shifts
schedule.publish_week
schedule.send_shift_alarm
schedule.shift_acceptance_summary
schedule.shift_offer_summary
schedule.shift_swap_summary
schedule.store_today
schedule.store_week
schedule.time_off_pending
schedule.unavailability_blocks
schedule.view
screenshot_url
self_critique
shell_execute
sql_query
team.moderate_chat
team.notify
telegram_send
time.edit_others
time.view_all
time.view_own
timeoff.approve
timeoff.request
toast_live_tables
training.configure
training.edit
training.mark_complete
training.remind
training.upload
training.view_all
training.view_expiring
training.view_own
vendors.add
vendors.directory_lookup
vendors.edit
vendors.invoice_summary
vendors.item_price_lookup
vendors.mark_paid
vendors.pay
vendors.price_change_summary
vendors.price_snapshot
vendors.produce_order_summary
vendors.produce_quote_summary
vendors.spend_reports
vendors.spend_summary
vendors.upload_invoice
vendors.vendor_recent_orders
vendors.view
vendors.view_invoices
wake_on_hub
web_search
whatsapp_send
write_file
""".split()
)

_HARDBLOCK_TOOL_IDS = frozenset(
    {
        "agent_restart",
        "fetch_url",
        "file_delete",
        "get_current_todo",
        "journal_read",
        "journal_write",
        "list_dir",
        "post_to_dev_chat",
        "post_to_sam_chat",
        "query_database",
        "read_file",
        "read_hub_inbox",
        "remove_participant",
        "render_deploy",
        "render_env_get",
        "render_env_set",
        "run_git",
        "screenshot_url",
        "self_critique",
        "shell_execute",
        "sql_query",
        "telegram_send",
        "wake_on_hub",
        "web_search",
        "whatsapp_send",
        "write_file",
    }
)


def is_excluded_non_routable(tool_id: str) -> bool:
    """Return True for inventory ids that must never enter chat routing."""
    if tool_id in _HARDBLOCK_TOOL_IDS:
        return True
    if tool_id.startswith(("dev.", "dash.", "resolve_")):
        return True
    if tool_id.startswith("assistant.") and tool_id not in {
        "assistant.general_help",
        "assistant.tool_discovery",
        "assistant.session_context",
    }:
        return True
    return False


def iter_excluded_non_routable_tool_ids() -> Iterable[str]:
    for tool_id in sorted(PARTNER_TOOL_IDS):
        if is_excluded_non_routable(tool_id):
            yield tool_id

_ACTION_WORDS = frozenset(
    {
        "add",
        "approve",
        "archive",
        "assign",
        "bulk_insert",
        "cleanup",
        "close",
        "configure",
        "create",
        "deactivate",
        "decline",
        "delete",
        "deny",
        "edit",
        "file_delete",
        "ingest",
        "link",
        "mark",
        "override",
        "pay",
        "post",
        "publish",
        "remove",
        "render_deploy",
        "render_env_set",
        "reset",
        "restart",
        "run",
        "scan",
        "send",
        "set",
        "shell_execute",
        "submit",
        "suspend",
        "telegram_send",
        "toggle",
        "trigger",
        "unassign",
        "update",
        "upload",
        "verify",
        "whatsapp_send",
        "write",
        "write_file",
    }
)


def _title_from_tool_id(tool_id: str) -> str:
    normalized = tool_id.replace("_", " ").replace(".", " ")
    return " ".join(part.capitalize() for part in normalized.split())


def _read_write_class(tool_id: str) -> str:
    parts = tool_id.replace(".", " ").replace("_", " ").replace("-", " ").split()
    if tool_id in _ACTION_WORDS or any(part in _ACTION_WORDS for part in parts):
        return "action_confirmation"
    return "read_only"


def _data_class(tool_id: str) -> str:
    if "." in tool_id:
        return tool_id.split(".", 1)[0]
    if "_" in tool_id:
        return tool_id.split("_", 1)[0]
    return "partner_tool"


def iter_partner_tool_definitions(*, include_excluded: bool = False) -> Iterable[dict[str, Any]]:
    for tool_id in sorted(PARTNER_TOOL_IDS):
        if not include_excluded and is_excluded_non_routable(tool_id):
            continue
        yield {
            "tool_id": tool_id,
            "label": _title_from_tool_id(tool_id),
            "description": (
                "Partner-level Cenas AI catalog entry from the approved tool "
                "inventory. Executable behavior is used only when an approved "
                "implementation exists; otherwise the request is saved for review."
            ),
            "required_permissions": ["ai.ask_claude"],
            "session_types": ["partner"],
            "store_scope": "partner_all_stores",
            "data_class": _data_class(tool_id),
            "read_write_class": _read_write_class(tool_id),
            "status": "review_gated",
            "partner_catalog_enabled": True,
            "implementation_status": "catalog_only",
        }


READONLY_OPERATIONAL_TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "tool_id": "employee.my_profile.read",
        "description": "Read the current employee's safe profile facts.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}}, "required": ["employee_id"]},
        "reads": ["employees", "employee_store_assignments", "employee_positions", "positions"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_contact.read",
        "description": "Read the current employee's own contact fields and secondary phones.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}}, "required": ["employee_id"]},
        "reads": ["employees", "employee_phones"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_stores.read",
        "description": "Read the stores assigned to the current employee.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}}, "required": ["employee_id"]},
        "reads": ["employee_store_assignments"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_positions.read",
        "description": "Read the current employee's positions by store.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}}, "required": ["employee_id"]},
        "reads": ["employee_positions", "positions"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_schedule.today",
        "description": "Read the current employee's published or assigned shifts for one day.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}, "date": {"type": "string", "format": "date"}}, "required": ["employee_id"]},
        "reads": ["schedules", "shifts", "positions"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_schedule.week",
        "description": "Read the current employee's shifts for a seven-day window.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}, "week_start": {"type": "string", "format": "date"}}, "required": ["employee_id"]},
        "reads": ["schedules", "shifts", "positions"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_recent_shifts",
        "description": "Read the current employee's recent shift history.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["employee_id"]},
        "reads": ["schedules", "shifts", "positions"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_open_shifts",
        "description": "Read open shifts in stores where the current employee works.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["employee_id"]},
        "reads": ["employee_store_assignments", "schedules", "shifts", "positions"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_availability.read",
        "description": "Read the current employee's recurring availability windows.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}}, "required": ["employee_id"]},
        "reads": ["employee_availability"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_time_off.status",
        "description": "Read the current employee's time-off request statuses.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["employee_id"]},
        "reads": ["time_off_requests"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_shift_alarm_settings",
        "description": "Read the current employee's shift reminder preferences and pending alarms.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}}, "required": ["employee_id"]},
        "reads": ["employee_alarm_preferences", "shift_alarms"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_attendance_summary",
        "description": "Read attendance status counts for the current employee by name.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}, "days": {"type": "integer"}}, "required": ["employee_id"]},
        "reads": ["employees", "manager_attendance_shift"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "employee.my_day_breakdown",
        "description": "Read one day's schedule and attendance breakdown for the current employee.",
        "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}, "date": {"type": "string", "format": "date"}}, "required": ["employee_id"]},
        "reads": ["employees", "schedules", "shifts", "positions", "manager_attendance_shift"],
        "intended_roles": ["employee", "staff", "partner"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "schedules.today_view",
        "description": "Read today's store schedule view.",
        "input_schema": {"type": "object", "properties": {"store": {"type": "string"}, "date": {"type": "string", "format": "date"}}},
        "reads": ["schedules", "shifts", "employees", "positions"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "schedules.week_view",
        "description": "Read a store schedule week view.",
        "input_schema": {"type": "object", "properties": {"store": {"type": "string"}, "week_start": {"type": "string", "format": "date"}}},
        "reads": ["schedules", "shifts", "employees", "positions"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "kitchen.recipe_search",
        "description": "Search recipe names, codes, categories, and instructions.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "category": {"type": "string"}, "limit": {"type": "integer"}}},
        "reads": ["recipes"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "kitchen.recipe_lookup",
        "description": "Read one recipe by id, code, or exact-ish name.",
        "input_schema": {"type": "object", "properties": {"recipe_id": {"type": "integer"}, "code": {"type": "string"}, "name": {"type": "string"}}},
        "reads": ["recipes"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "kitchen.prep_list_today",
        "description": "Read today's prep list rows for a store.",
        "input_schema": {"type": "object", "properties": {"store": {"type": "string"}, "date": {"type": "string", "format": "date"}}},
        "reads": ["kitchen_prep_entry", "kitchen_prep_item"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "kitchen.prep_entries_by_day",
        "description": "Read prep entries for a chosen day and store.",
        "input_schema": {"type": "object", "properties": {"store": {"type": "string"}, "date": {"type": "string", "format": "date"}}},
        "reads": ["kitchen_prep_entry", "kitchen_prep_item"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "vendors.vendor_recent_orders",
        "description": "Read recent parsed vendor orders by vendor/store.",
        "input_schema": {"type": "object", "properties": {"vendor": {"type": "string"}, "store": {"type": "string"}, "limit": {"type": "integer"}}},
        "reads": ["vendor_recent_orders"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "attendance.manager_board_summary",
        "description": "Read attendance board status counts and rows for one store/day.",
        "input_schema": {"type": "object", "properties": {"store": {"type": "string"}, "date": {"type": "string", "format": "date"}}},
        "reads": ["manager_attendance_shift", "manager_attendance_event"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "attendance.late_summary",
        "description": "Read late attendance rows for one store/day.",
        "input_schema": {"type": "object", "properties": {"store": {"type": "string"}, "date": {"type": "string", "format": "date"}}},
        "reads": ["manager_attendance_shift"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "attendance.no_show_summary",
        "description": "Read no-show attendance rows for one store/day.",
        "input_schema": {"type": "object", "properties": {"store": {"type": "string"}, "date": {"type": "string", "format": "date"}}},
        "reads": ["manager_attendance_shift"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "attendance.callout_summary",
        "description": "Read callout attendance rows for one store/day.",
        "input_schema": {"type": "object", "properties": {"store": {"type": "string"}, "date": {"type": "string", "format": "date"}}},
        "reads": ["manager_attendance_shift"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
    {
        "tool_id": "attendance.missed_punch_summary",
        "description": "Read attendance rows that appear to have missing punches for one store/day.",
        "input_schema": {"type": "object", "properties": {"store": {"type": "string"}, "date": {"type": "string", "format": "date"}}},
        "reads": ["manager_attendance_shift"],
        "intended_roles": ["partner", "staff"],
        "read_write_class": "read_only",
        "implementation": "assistant_operational_tools.run_operational_tool",
    },
)


def iter_readonly_operational_tool_specs() -> Iterable[dict[str, Any]]:
    for spec in READONLY_OPERATIONAL_TOOL_SPECS:
        yield dict(spec)
