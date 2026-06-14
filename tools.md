# CENA Tools.md

Last updated: 2026-06-14

This file is the standalone CENA tool map. It defines what CENA may read and
write through the app, and which profile is allowed to use each level of power.

The full read/write/developer tool set is for Sam only. It is tied to Sam's
authenticated login, not to a chat claim, display name, shared browser, generic
partner profile, manager profile, employee profile, or driver profile.

## Required Sam Gate

CENA may expose full-control tools only when the server proves the active user
is Sam:

- `/sam/chat` must pass `is_sam_chat_user()` from `SAM_CHAT_USER_ID`; or
- an app assistant session must be tied to Sam's configured user id from
  `SAM_CHAT_USER_ID`, `SAM_CHAT_USER_IDS`, or `AI_ASSISTANT_OPERATOR_USER_IDS`.

For developer, file-write, deploy, environment, shell, git, permission, and
database-write tools, name matching is not enough. A partner wildcard role is
not enough by itself. The current authenticated Sam user id must be present.

If the Sam gate cannot be proven, CENA must block full-control tools and save
the request for Sam review.

## Tool Classes

| Class | Meaning | Sam | Managers | Employees | Drivers |
| --- | --- | --- | --- | --- | --- |
| `read_all` | Read app data, logs, snapshots, reports, profiles, and operational records | Yes | Assigned scope only | Self only | Self/route only |
| `write_app` | Create, edit, archive, approve, deny, import, publish, or update app records | Yes | Assigned permission only | Own requests only | Own route/bid actions only |
| `developer_write` | Change pages, templates, routes, services, CSS, JS, tests, docs, git, deploys, env keys, SQL, files, or scripts | Yes, Sam only | No | No | No |
| `secret_use` | Use secrets from env/secret stores without printing them | Yes, Sam only | No | No | No |
| `blocked` | Raw secrets, unrelated computer files, or actions outside approved app scope | No unless Sam explicitly expands scope | No | No | No |

## Sam Full-Control Tools

When the Sam gate passes, CENA may use all app-scoped read/write/developer
tools needed to operate or modify the Cenas Kitchen app.

Sam read tools include:

- All local and Render app data, including employees, schedules, attendance,
  sales, PMIX, labor, Toast data, corporate orders, catering, drivers,
  vendors, kitchen, maintenance, legal, manager logs, training, incidents,
  onboarding, permissions, reports, dashboards, audit logs, snapshots, and
  health checks.
- All app source files, templates, static assets, migrations, tests, docs,
  logs, deployment status, git status, and tool catalogs.

Sam write/developer tools include:

- Add, edit, or remove app pages, dashboard tabs, templates, forms, routes,
  services, models, migrations, tests, static files, docs, and tool handlers.
- Create, update, archive, approve, deny, import, publish, or backfill app
  records across stores.
- Run tests, compile checks, app diagnostics, SQL diagnostics, sync checks,
  health checks, git status, commits, pushes, Render deploy checks, and Render
  deploys when Sam asks.
- Update profile permissions and connect tools to Sam, manager, employee, and
  driver profile types.
- Post DevChat updates and append Desktop UPDATE notes when Sam asks.

Execution rules for Sam:

- Non-destructive reads and diagnostics may run directly after the Sam gate.
- Writes require Sam's current-session instruction.
- Destructive deletes, bulk database writes, permission changes, credential or
  environment changes, shell commands, git pushes, and deploys require clear
  confirmation in the active Sam session.
- Every write/developer action should leave an audit trail with what changed,
  where it changed, and how it was verified.
- Secrets may be used through env or secret stores, but raw tokens, API keys,
  passwords, full env dumps, and credential files must never be printed.

## Managers

Managers get scoped operational tools only. They may read and write records for
their role and assigned stores when the permission catalog allows it.

Manager tools include store/team/schedule/attendance/HR/onboarding/catering/
kitchen/vendor/maintenance/sports/reporting actions, but never developer
write tools, raw secrets, shell, git, Render env, unrestricted SQL, or full
company data unless Sam explicitly grants that tool through code and policy.

## Employees

Employees get self-service tools only. They may read their own schedule,
profile, training, availability, time off, attendance summaries, and approved
shift market actions. They may create their own requests where implemented.

Employees must not receive developer tools, manager notes, coworker private
data, payroll/pay-rate internals, raw analytics, raw Toast data, unrestricted
SQL, secrets, git, shell, deploy, or permission tools.

## Drivers

Drivers get delivery-focused self-service tools only. They may read their own
driver profile, bids, route history, assigned deliveries, status, mileage, and
approved payout/status summaries where implemented. Corporate driver tools must
still be scoped by role and assignment.

Drivers must not receive developer tools, other drivers' private records, full
customer PII, unrelated store analytics, secrets, git, shell, deploy, or
permission tools.

## Current Registry Conventions

The app registry uses these fields:

| Field | Meaning |
| --- | --- |
| `tool_id` | Stable id, such as `schedule.create_shift` |
| `required_permissions` | Permission tags required before the tool can run |
| `session_types` | Profile sessions allowed to see/use it |
| `store_scope` | Self, assigned store, all stores, partner all stores, or operator only |
| `data_class` | Data family such as employee, schedule, orders, reports, dev, db |
| `read_write_class` | `read_only` or `action_confirmation` today; Sam-only developer tools should use `developer_write` or `owner_confirmed_write` when added |
| `status` | `active` or `review_gated` |
| `implementation_status` | `implemented` or `catalog_only` |

If a tool is `catalog_only`, CENA may describe it and create implementation
work for Sam, but should not pretend the executable handler already exists.

## Current Read Tools

These tool ids are read-only or primarily read/summary/discovery tools in the
current CENA registry or runtime:

```text
assistant.general_help
assistant.approved_answer_lookup
assistant.audit_lookup_self
assistant.permission_explain
assistant.session_context
assistant.tool_discovery
attendance.callout_summary
attendance.late_summary
attendance.manager_board_summary
attendance.missed_punch_summary
attendance.no_show_summary
attendance.view
catering.driver_perf
catering.revenue
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
dev.assistant_policy_rules
dev.assistant_review_queue
dev.assistant_tool_catalog_snapshot
dev.cena_audit_log
dev.dev_chat_read
dev.docck_health
dev.git_status
dev.github_pr_summary
dev.render_deploy_status
dev.render_env_key_presence
dev.render_logs_read
dev.sentry_issue_summary
driver.view_all_queue
driver.view_earnings
driver.view_own_queue
drivers.active_delivery_queue
drivers.assignment_status
drivers.bonus_summary
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
drivers.roster_summary
drivers.route_history_safe
drivers.score_summary
drivers.store_summary
drivers.tier_summary
drivers.unassigned_orders
emp.view_dd
emp.view_directory
emp.view_onboarding
emp.view_perf
emp.view_tax_full
emp.view_tax_masked
emp.view_wages
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
equip.view
equip.view_warranty
ezcater_get_order_full_details
fin.view_accounts
fin.view_ap
fin.view_deposits
fin.view_instant_deposit
fin.view_payroll
fin.view_pnl
fin.view_tips
finance.ap_summary
finance.deposit_summary
finance.instant_deposit_status
finance.payroll_setup_summary
finance.pnl_summary
finance.tip_pool_summary
finance.vendor_payables_summary
get_current_todo
incident.view
journal_read
kitchen.catering_prep_breakdown
kitchen.fresh_food_recent
kitchen.fresh_food_today
kitchen.fresh_view
kitchen.inventory
kitchen.inventory_snapshot
kitchen.order_prep_needs
kitchen.prep_entries_by_day
kitchen.prep_item_lookup
kitchen.prep_list_today
kitchen.prep_view
kitchen.recipe_lookup
kitchen.recipe_search
kitchen.recipes_view
labor.store_aggregate
legal.company_structure_summary
legal.compliance_cal
legal.compliance_calendar
legal.document_search
legal.insurance_summary
legal.license_summary
legal.matter_lookup
legal.matter_summary
legal.view_docs
legal.view_insurance
legal.view_licenses
legal.view_notices
list_dir
maint.view
manager.close_of_day_audit_summary
manager.daily_goals_summary
manager.daily_log_search
manager.daily_log_summary
manager.employee_counseling_summary
manager.incident_lookup
manager.incident_summary
manager.maintenance_summary
manager.pre_shift_checklist_summary
manager.recipe_page_search
manager.shift_handoff_summary
manager.staff_feedback_summary
manager.supply_request_summary
manager.training_record_summary
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
orders.in_house_quote_lookup
orders.in_house_quotes_summary
orders.store_summary
permissions.access_requests
permissions.denial_summary
permissions.my_permissions
permissions.override_summary
permissions.permission_catalog
permissions.role_catalog
permissions.role_change_risk_check
permissions.user_audit_log
permissions.user_lookup
perms.view
query_database
read_file
read_hub_inbox
render_env_get
reports.benchmark_summary
reports.benchmarks
reports.catering
reports.catering_item_mix
reports.catering_summary
reports.cross_store
reports.cross_store_summary
reports.driver_performance
reports.employee_performance_safe
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
schedule.alarm_pending_summary
schedule.availability_conflicts
schedule.open_shifts
schedule.shift_acceptance_summary
schedule.shift_offer_summary
schedule.shift_swap_summary
schedule.store_today
schedule.store_week
schedule.time_off_pending
schedule.unavailability_blocks
schedule.view
schedules.today_view
schedules.week_view
sql_query
time.view_all
time.view_own
toast.employee_profiles
toast.sales_summary
toast.table_activity
toast.webhook_activity
toast_live_tables
training.view_all
training.view_expiring
training.view_own
vendors.directory_lookup
vendors.invoice_summary
vendors.item_price_lookup
vendors.price_change_summary
vendors.price_snapshot
vendors.produce_order_summary
vendors.produce_quote_summary
vendors.spend_reports
vendors.spend_summary
vendors.vendor_recent_orders
vendors.view
vendors.view_invoices
web_search
```

## Current Write And Action Tools

These tool ids create, edit, delete, approve, deny, send, import, export,
deploy, mutate, or otherwise act. They require profile permissions, and the
developer/system ones are Sam-only:

```text
agent_restart
assistant.feedback_capture
assistant.handoff_to_sam
assistant.review_queue_submit
attendance.edit
availability.manage
availability.update_employee
catering.assign_driver
catering.edit
catering.print_pdf
catering.reassign_store
catering.unassign
dev.archive_dev_chat
dev.cleanup_attachments
dev.dev_chat_post
dev.post_dev_chat
dev.restart_agent
dev.run_git_command
dev.run_prod_sync
dev.run_script
dev.set_render_env
dev.toggle_automation
dev.trigger_render_deploy
driver.approve_mileage
driver.submit_mileage
driver.update_others
driver.update_own
drivers.approve_delivery_request
drivers.approve_mileage
drivers.close_paycheck
drivers.decline_delivery_request
drivers.reset_passcode
drivers.suspend_driver
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
employee.add
employee.deactivate
employee.edit_profile
employee.link_to_toast
employee.link_to_user
employee.reset_passcode
equip.add
equip.edit
file_delete
fin.approve_expense
fin.config_accounts
fin.config_sales_cat
fin.config_tips
fin.edit_ap
fin.edit_payroll
finance.approve_expense
finance.mark_invoice_paid
fetch_url
incident.create
incident.edit
journal_write
kitchen.fresh_edit
kitchen.prep_edit
kitchen.update_fresh_food
kitchen.update_prep_item
legal.edit_insurance
legal.edit_licenses
legal.edit_matter
legal.edit_meta
legal.manage_notices
legal.upload_docs
legal.upload_document
maint.approve_spend
maint.close
maint.edit
maint.submit
maintenance.close_request
maintenance.create_request
manager.close_incident
manager.create_daily_log
manager.create_incident
manager.edit_incident
manager.send_team_notification
manager_log.write
orders.assign_driver
orders.create_in_house_quote
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
permissions.approve_access_request
permissions.assign_role
permissions.create_role_template
permissions.deny_access_request
permissions.edit_role_template
permissions.override_user_permission
perms.assign
perms.assign_role
perms.create_role
perms.delete_role
perms.edit_role
perms.override
post_to_dev_chat
post_to_sam_chat
produce.bulk_insert_orders
produce.ingest_vendor_emails
produce.scan_vendor_inbox
remove_participant
render_deploy
render_env_set
reports.export
reports.export_prepare
run_git
schedule.approve_shift_offer
schedule.approve_shift_swap
schedule.approve_time_off
schedule.configure
schedule.create_shift
schedule.delete_shift
schedule.deny_time_off
schedule.edit_shift
schedule.publish_week
schedule.send_shift_alarm
screenshot_url
self_critique
shell_execute
team.moderate_chat
team.notify
telegram_send
time.edit_others
timeoff.approve
timeoff.request
training.configure
training.edit
training.mark_complete
training.remind
training.upload
vendors.add
vendors.edit
vendors.mark_paid
vendors.pay
vendors.upload_invoice
wake_on_hub
whatsapp_send
write_file
```

## Sam-Only Developer/System Tools

These are always Sam-only, even if the registry exposes a catalog entry:

```text
agent_restart
dev.*
file_delete
list_dir
query_database
read_file
render_deploy
render_env_get
render_env_set
run_git
screenshot_url
shell_execute
sql_query
web_search
write_file
```

These tools can change the app or inspect sensitive infrastructure. They must
use the Sam gate, current-session instruction, and audit trail rules above.

## Permission Wiring Rule

When adding or changing a CENA tool:

1. Add the `tool_id` here under read or write/action.
2. Add registry metadata with `required_permissions`, `session_types`,
   `store_scope`, `data_class`, `read_write_class`, `status`, and
   `implementation_status`.
3. For Sam-only developer tools, require the Sam user id gate in both catalog
   visibility and runtime execution.
4. Implement a read path first when possible.
5. Add tests for Sam, manager, employee, and driver access.
6. Verify writes with an audit log and a visible result in the app.
