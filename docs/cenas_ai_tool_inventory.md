# Cenas AI Tool Inventory

Date: 2026-06-05

Purpose: inventory what the mobile Cenas AI bubble has today, what Sam/operator Cena has in the documented gateway, and what tools Cenas AI can be expanded to have.

## Definitions

- **Cenas AI bubble**: the mobile/right-bottom assistant shown in the app UI. Source: `app/web/assistant_routes.py` and `scripts/assistant_ck_runtime.py`.
- **CK-local runtime**: assistant answer service on CK/Mini_IT13. It owns model calls and the review DB.
- **Sam/operator Cena**: `/sam/chat` operator AI, separate from the staff-facing Cenas AI bubble.
- **Active**: can execute now for an allowed user.
- **Review-gated**: registered but blocked until Sam approves policy and implementation.
- **Candidate**: not installed/created yet as an assistant tool, but the app already has data/routes/models that can back it.

## Current Cenas AI Bubble Tools

Normal staff/employee/driver sessions still have one active tool today:
`assistant.general_help`.

Sam/Masood owner-operator sessions additionally activate three sanitized,
read-only aggregate tools. These tools are not role-inherited by all partners;
they require the owner/operator user-id/name gate (`AI_ASSISTANT_OPERATOR_USER_IDS`,
`SAM_CHAT_USER_IDS`, `SAM_CHAT_USER_ID`, `MASOOD_CHAT_USER_ID`, or the configured
operator names fallback).

| Tool | Status | Scope | Permission | Notes |
|---|---|---|---|---|
| `assistant.general_help` | active | partner, staff, employee, driver | `ai.ask_claude_personal` | General navigation/workflow/policy help only. No private operational DB reads. |
| `employee.my_profile` | review-gated | employee, staff | `ai.ask_claude_personal` | Placeholder. Executable data tool not built yet. |
| `orders.store_summary` | active for owner-operator, otherwise review-gated | partner, staff | `ai.ask_claude`, `orders.view` | Sanitized aggregate order/catering counts: totals, today/upcoming, needs-driver count, tracking-link count, store split, status counts. No customer name/phone/address/order details. |
| `drivers.store_summary` | active for owner-operator, otherwise review-gated | partner, staff | `ai.ask_claude`, `drivers.view_roster` | Sanitized aggregate driver counts: total/active/inactive, on-shift count, active-order count, average score, store split. No driver phone/address/raw payout/order detail. |
| `labor.store_aggregate` | active for owner-operator, otherwise review-gated | partner, staff | `ai.ask_claude`, `labor.view_store_summary` | Sanitized aggregate employee/labor counts: employee totals, store assignment counts, published/open shifts, attendance status counts, last-30 cached hours. No individual pay, Toast GUIDs, sales, or PII. |

Current behavior: operational/data questions such as "how many caterings?" are
answered for Sam/Masood from approved aggregate tool payloads. The same question
from non-owner sessions remains queued for Sam review unless a future permission
gate activates the tool for that audience.

## Current CK-Local Runtime Capabilities

Active capabilities:

- Token-gated endpoint: `POST /assistant/answer`.
- Health endpoint: `GET /healthz`.
- Durable review save to CK-local `assistant_review.sqlite`.
- Sonnet first, Gemini fallback.
- Anthropic prompt caching on the stable policy block (`cache_control:
  {"type":"ephemeral"}`) so repeated assistant turns can reuse the policy
  prompt instead of paying to reprocess it every time.
- Approved owner/operator aggregate tool answers before model fallback:
  `orders.store_summary`, `drivers.store_summary`, and `labor.store_aggregate`.
- Question safety classifier for sensitive/data/operational questions.
- Redacted review queue for unanswered/blocked questions.
- Render proxy support when `AI_ASSISTANT_CK_RUNTIME_URL` and token are configured.

Not active in CK-local runtime:

- No broad DB query tool for the staff-facing assistant.
- No employee personal-profile reader yet.
- No order/customer-detail reader for the staff-facing assistant.
- No driver-detail reader for the staff-facing assistant.
- No labor-detail reader for the staff-facing assistant.
- No OpenAI provider yet.
- No write/action tools.
- No Sam approval UI for converting saved questions into allowed future answers.

## Sam/Operator Cena Gateway Tools

Docs disagree between 21 and 22 tools. The accessible gateway copy at `C:\Users\sam\Desktop\cenas-local\_attn_work\docck\_cena_gateway_live.py` contains 22 tools:

| Tool | Type | Notes |
|---|---|---|
| `read_file` | filesystem | Read codebase file. |
| `write_file` | filesystem | Write/overwrite codebase file. |
| `list_dir` | filesystem | List files/directories. |
| `file_delete` | filesystem | Delete a file, refuses directories. |
| `run_git` | repo | Run allowlisted git commands. |
| `shell_execute` | execution | Run shell command on AiCk. |
| `fetch_url` | network | HTTP requests for APIs/live checks. |
| `render_env_get` | Render | Read Render env vars. Sensitive. |
| `render_env_set` | Render | Set Render env var and redeploy. Sensitive/action. |
| `render_deploy` | Render | Trigger manual deploy. |
| `telegram_send` | communication | Send/log Telegram-style message to Sam. |
| `post_to_dev_chat` | communication | Post to developer chat as Cena. |
| `read_hub_inbox` | communication | Read LAN hub inbox tail. |
| `post_to_sam_chat` | communication | Inject message into `/sam/chat`. |
| `journal_write` | memory | Persistent journal write. |
| `journal_read` | memory | Persistent journal read. |
| `sql_query` | database | Read-only production SQL with guards. Operator-only. |
| `self_critique` | model utility | Second-pass critique. |
| `web_search` | web | Anthropic web search tool. |
| `screenshot_url` | browser/vision | Headless screenshot and image return. |
| `ezcater_get_order_full_details` | app data | Full ezCater order detail view. |
| `agent_restart` | ops | Orphaned/dck-only restart tool, approval-gated. |

Additional documented/pending operator tools:

- `wake_on_hub`
- `remove_participant`
- `toast_live_tables`
- `whatsapp_send`
- `get_current_todo`
- `query_database`
- `resolve_employee`
- `resolve_menu_item`
- `resolve_vendor`
- `resolve_catering_order`
- `resolve_manager_log`

Important: these operator tools are not safe to give directly to normal staff. They are Sam/operator tools and include filesystem, shell, Render env, SQL, and deployment access.

## Current App Permission Catalog

The app already has 89 permission entries across 14 categories. These are not assistant tools yet, but they define what Cenas AI can map to.

### Dashboard Access

- `dash.today` - live - Access Today Dashboard
- `dash.manager` - live - Access Manager Dashboard
- `dash.catering` - live - Access Catering Dashboard
- `dash.operations` - live - Access Operations Dashboard
- `dash.vendors` - live - Access Vendors Dashboard
- `dash.kitchen` - live - Access Kitchen Dashboard
- `dash.legal` - reserved - Access Legal Dashboard
- `dash.cena_chat` - live - Access Partner Chat (Cena)
- `dash.dev_chat` - live - Access Dev Chat

### Time & Attendance

- `time.view_own` - reserved - View Own Time Entries
- `time.view_all` - reserved - View All Time Entries
- `time.edit_others` - reserved - Edit Other Employees' Time Entries
- `attendance.view` - reserved - View Attendance Records
- `attendance.edit` - reserved - Edit Attendance Records
- `schedule.configure` - reserved - Configure Schedules
- `schedule.view` - reserved - View Schedule
- `timeoff.request` - reserved - Request Time Off
- `timeoff.approve` - reserved - Approve Time Off Requests
- `availability.manage` - reserved - Set & Manage Availability

### Catering & EZ Orders

- `catering.view` - live - View Catering Orders
- `catering.edit` - live - Edit Catering Order Details
- `catering.assign_driver` - live - Assign Drivers to Catering Orders
- `catering.unassign` - live - Unassign Drivers
- `catering.view_drivers` - live - View Driver List
- `catering.revenue` - live - View Catering Revenue Reports
- `catering.print_pdf` - live - Print/Download Catering PDFs
- `catering.reassign_store` - live - Reassign Order Between Stores
- `catering.driver_perf` - reserved - View Driver Performance

### Manager Powers

- `manager_log.write` - live - Daily Log Entry
- `incident.create` - live - Create Incident Reports
- `incident.view` - live - View Incident Reports
- `incident.edit` - live - Edit Incident Reports
- `team.notify` - live - Send Team Notifications
- `team.moderate_chat` - reserved - Moderate Team Chat

### Vendors & Purchasing

- `vendors.view` - live - View Vendors List
- `vendors.add` - live - Add New Vendors
- `vendors.edit` - live - Edit Vendor Info
- `vendors.view_invoices` - live - View Vendor Invoices
- `vendors.mark_paid` - reserved - Mark Invoices as Paid
- `vendors.pay` - reserved - Pay Vendor Invoices
- `vendors.spend_reports` - reserved - View Vendor Spend Reports

### Kitchen Operations

- `kitchen.fresh_view` - live - View Fresh Food List
- `kitchen.fresh_edit` - live - Edit Fresh Food List
- `kitchen.prep_view` - live - View Prep List
- `kitchen.prep_edit` - live - Edit Prep List
- `kitchen.recipes_view` - live - View Recipes
- `kitchen.inventory` - reserved - Update Inventory Counts

### Employee Management

- `emp.view_directory` - live - View Employee Directory
- `emp.add` - live - Add New Employees
- `emp.edit_info` - live - Edit Employee Info
- `emp.view_wages` - live - View Employee Wages
- `emp.edit_wages` - live - Edit Employee Wages
- `emp.archive` - live - Archive/Deactivate Employee
- `emp.reset_passcode` - live - Reset Employee Passcode
- `emp.view_tax_masked` - reserved - View Tax Identifiers (Masked)
- `emp.view_tax_full` - reserved - View Tax Identifiers (Full)
- `emp.edit_tax` - reserved - Edit Tax Identifiers
- `emp.view_dd` - reserved - View Direct Deposit Info
- `emp.edit_dd` - reserved - Edit Direct Deposit Info
- `emp.view_onboarding` - reserved - View Onboarding Documents
- `emp.upload_onboarding` - reserved - Upload Onboarding Documents
- `emp.view_perf` - reserved - View Employee Performance Notes
- `emp.edit_perf` - reserved - Edit Employee Performance Notes

### Training & Certifications

- `training.view_own` - live - View Own Training Records
- `training.view_all` - live - View All Training Records
- `training.view_expiring` - live - View Expiring/Overdue Certs
- `training.mark_complete` - live - Mark Training Complete
- `training.edit` - live - Add/Edit Training Records
- `training.upload` - live - Upload Certification Documents
- `training.configure` - reserved - Configure Training Requirements
- `training.remind` - reserved - Send Training Reminders

### Maintenance & Equipment

- `maint.view` - live - View Maintenance Requests
- `maint.submit` - live - Submit Maintenance Request
- `maint.edit` - live - Edit Maintenance Request
- `maint.close` - live - Close Maintenance Request
- `maint.approve_spend` - reserved - Approve Maintenance Spend
- `equip.view` - live - View Equipment Records
- `equip.add` - live - Add Equipment Records
- `equip.edit` - live - Edit Equipment Records
- `equip.view_warranty` - live - View Warranty Information

### Reporting

- `reports.sales` - live - View Sales Reports
- `reports.labor` - live - View Labor Reports
- `reports.menu` - reserved - View Menu Performance Reports
- `reports.catering` - live - View Catering Reports
- `reports.forecasts` - reserved - View Forecasts
- `reports.marketing` - reserved - View Marketing Reports
- `reports.giftcard` - reserved - View Gift Card / Rewards Reports
- `reports.cross_store` - live - View Cross-Store Reports
- `reports.export` - live - Export Reports to PDF/CSV
- `reports.benchmarks` - reserved - View Industry Benchmarks

### Legal & Compliance

- `legal.view_docs` - reserved - View Legal Documents
- `legal.upload_docs` - reserved - Upload Legal Documents
- `legal.edit_meta` - reserved - Edit Legal Document Metadata
- `legal.view_licenses` - reserved - View Licenses & Permits
- `legal.edit_licenses` - reserved - Edit Licenses & Permits
- `legal.view_insurance` - reserved - View Insurance Information
- `legal.edit_insurance` - reserved - Edit Insurance Information
- `legal.view_notices` - reserved - View Legal Notifications
- `legal.manage_notices` - reserved - Manage Legal Notifications
- `legal.compliance_cal` - reserved - View Compliance Calendar

### Financial

- `fin.view_accounts` - reserved - View Financial Accounts
- `fin.config_accounts` - reserved - Configure Financial Accounts
- `fin.view_deposits` - reserved - View Daily Deposits
- `fin.view_payroll` - reserved - View Payroll Setup
- `fin.edit_payroll` - reserved - Edit Payroll Setup
- `fin.view_ap` - reserved - View Accounts Payable
- `fin.edit_ap` - reserved - Edit Accounts Payable
- `fin.approve_expense` - reserved - Approve Large Expenses
- `fin.view_pnl` - reserved - View Profit & Loss
- `fin.config_sales_cat` - reserved - Configure Sales Categories for Accounting
- `fin.view_tips` - reserved - View Tip Pool / Tip Out Records
- `fin.config_tips` - reserved - Configure Tip Pool / Tip Out Rules
- `fin.view_instant_deposit` - reserved - View Instant Deposit Status

### User Permissions

- `perms.view` - live - View User Permissions
- `perms.assign` - live - Assign Permissions to Users
- `perms.create_role` - live - Create Role Templates
- `perms.edit_role` - live - Edit Role Templates
- `perms.delete_role` - live - Delete Role Templates
- `perms.assign_role` - live - Assign Roles to Users
- `perms.override` - live - Override Role Permissions Per User

### Driver-Specific

- `driver.view_own_queue` - live - View Own Driver Queue
- `driver.view_all_queue` - live - View All Driver Queues
- `driver.update_own` - live - Update Own Delivery Status
- `driver.update_others` - live - Update Other Drivers' Status
- `driver.view_earnings` - live - View Driver Earnings
- `driver.submit_mileage` - live - Submit Mileage / Expense Reports
- `driver.approve_mileage` - live - Approve Mileage / Expense Reports

## Candidate Read-Only Cenas AI Tools To Build

These should be safe first because they read already-existing data and can be permission/store scoped.

### Core Assistant

- `assistant.tool_discovery` - tell user which tools are available for their role.
- `assistant.permission_explain` - explain why a question is blocked.
- `assistant.review_queue_submit` - save unanswerable question for Sam review.
- `assistant.approved_answer_lookup` - answer from Sam-approved FAQ/policy memory.
- `assistant.feedback_capture` - save whether answer helped.
- `assistant.audit_lookup_self` - show a user their own assistant history.
- `assistant.handoff_to_sam` - prepare escalation packet for Sam, no direct notification unless approved.

### Employee Self Tools

- `employee.my_profile.read`
- `employee.my_contact.read`
- `employee.my_positions.read`
- `employee.my_stores.read`
- `employee.my_schedule.today`
- `employee.my_schedule.week`
- `employee.my_open_shifts`
- `employee.my_shift_alarm_settings`
- `employee.my_time_off.status`
- `employee.my_availability.read`
- `employee.my_training.read`
- `employee.my_attendance_summary`
- `employee.my_recent_shifts`
- `employee.my_pay_summary`
- `employee.my_performance_summary`
- `employee.my_rank_summary`
- `employee.my_rank_explain`
- `employee.my_day_breakdown`

### Manager/Store Employee Tools

- `employees.store_directory`
- `employees.store_profile_lookup`
- `employees.store_positions`
- `employees.store_schedule_read`
- `employees.store_availability_read`
- `employees.store_time_off_summary`
- `employees.store_training_summary`
- `employees.store_attendance_summary`
- `employees.needs_review_summary`
- `employees.passcode_status_summary`
- `employees.profile_completion_summary`
- `employees.link_status_summary`
- `employees.toast_link_summary`
- `employees.performance_safe_summary`
- `employees.roster_gap_summary`

### Catering/EZ Order Tools

- `orders.catering_count`
- `orders.catering_today`
- `orders.catering_tomorrow`
- `orders.catering_week`
- `orders.catering_next_30_days`
- `orders.catering_by_store`
- `orders.catering_by_status`
- `orders.catering_needs_driver`
- `orders.catering_late_risk`
- `orders.catering_live_tracking`
- `orders.catering_tracking_missing`
- `orders.catering_uuid_status`
- `orders.catering_driver_assignment_summary`
- `orders.catering_order_lookup`
- `orders.catering_order_items_safe`
- `orders.catering_item_mix`
- `orders.catering_fees_summary`
- `orders.catering_payout_safe_summary`
- `orders.catering_returning_customers_aggregate`
- `orders.catering_pdf_status`
- `orders.in_house_quotes_summary`
- `orders.in_house_quote_lookup`

### Driver Tools

- `drivers.roster_summary`
- `drivers.driver_lookup`
- `drivers.active_delivery_queue`
- `drivers.unassigned_orders`
- `drivers.assignment_status`
- `drivers.live_location_summary`
- `drivers.route_history_safe`
- `drivers.delivery_completion_summary`
- `drivers.photo_completion_summary`
- `drivers.parking_receipt_summary`
- `drivers.parking_cost_summary`
- `drivers.mileage_summary`
- `drivers.bonus_summary`
- `drivers.five_star_summary`
- `drivers.score_summary`
- `drivers.tier_summary`
- `drivers.earnings_own`
- `drivers.earnings_manager_safe`
- `drivers.driver_profile_read`
- `drivers.driver_data_center_summary`

### Schedule/Attendance Tools

- `schedule.store_today`
- `schedule.store_week`
- `schedule.open_shifts`
- `schedule.shift_acceptance_summary`
- `schedule.shift_offer_summary`
- `schedule.shift_swap_summary`
- `schedule.time_off_pending`
- `schedule.availability_conflicts`
- `schedule.unavailability_blocks`
- `schedule.alarm_pending_summary`
- `attendance.manager_board_summary`
- `attendance.late_summary`
- `attendance.no_show_summary`
- `attendance.callout_summary`
- `attendance.missed_punch_summary`

### Manager Log/Operations Tools

- `manager.daily_log_search`
- `manager.daily_log_summary`
- `manager.shift_handoff_summary`
- `manager.incident_summary`
- `manager.incident_lookup`
- `manager.supply_request_summary`
- `manager.daily_goals_summary`
- `manager.staff_feedback_summary`
- `manager.pre_shift_checklist_summary`
- `manager.close_of_day_audit_summary`
- `manager.recipe_page_search`
- `manager.training_record_summary`
- `manager.maintenance_summary`
- `manager.employee_counseling_summary`

### Kitchen Tools

- `kitchen.fresh_food_today`
- `kitchen.fresh_food_recent`
- `kitchen.prep_list_today`
- `kitchen.prep_entries_by_day`
- `kitchen.recipe_lookup`
- `kitchen.recipe_search`
- `kitchen.prep_item_lookup`
- `kitchen.catering_prep_breakdown`
- `kitchen.order_prep_needs`
- `kitchen.inventory_snapshot`

### Vendor/Produce Tools

- `vendors.directory_lookup`
- `vendors.vendor_recent_orders`
- `vendors.invoice_summary`
- `vendors.price_snapshot`
- `vendors.price_change_summary`
- `vendors.produce_order_summary`
- `vendors.produce_quote_summary`
- `vendors.item_price_lookup`
- `vendors.spend_summary`

### Reporting/Analytics Tools

- `reports.sales_summary`
- `reports.sales_by_store`
- `reports.sales_by_channel`
- `reports.labor_summary`
- `reports.labor_by_store`
- `reports.catering_summary`
- `reports.catering_item_mix`
- `reports.driver_performance`
- `reports.employee_performance_safe`
- `reports.team_roster_summary`
- `reports.cross_store_summary`
- `reports.export_prepare`
- `reports.benchmark_summary`
- `reports.forecast_summary`
- `reports.marketing_summary`

### Legal/Finance Tools

These must be partner/corporate only and should start read-only.

- `legal.matter_summary`
- `legal.matter_lookup`
- `legal.document_search`
- `legal.license_summary`
- `legal.insurance_summary`
- `legal.company_structure_summary`
- `legal.compliance_calendar`
- `finance.deposit_summary`
- `finance.ap_summary`
- `finance.vendor_payables_summary`
- `finance.payroll_setup_summary`
- `finance.pnl_summary`
- `finance.tip_pool_summary`
- `finance.instant_deposit_status`

### Permissions/Access Tools

These are partner-only except narrow self explanations.

- `permissions.my_permissions`
- `permissions.user_lookup`
- `permissions.role_catalog`
- `permissions.permission_catalog`
- `permissions.denial_summary`
- `permissions.access_requests`
- `permissions.user_audit_log`
- `permissions.override_summary`
- `permissions.role_change_risk_check`

### Developer/System Tools

These should stay operator/partner-only.

- `dev.dev_chat_read`
- `dev.dev_chat_post`
- `dev.agent_status`
- `dev.docck_health`
- `dev.render_deploy_status`
- `dev.render_logs_read`
- `dev.render_env_key_presence`
- `dev.sentry_issue_summary`
- `dev.git_status`
- `dev.github_pr_summary`
- `dev.cena_audit_log`
- `dev.assistant_review_queue`
- `dev.assistant_policy_rules`
- `dev.assistant_tool_catalog_snapshot`

## Candidate Approval-Gated Action Tools

These can exist, but should never run automatically just because the model wants to.

### Catering/Driver Actions

- `orders.assign_driver`
- `orders.unassign_driver`
- `orders.mark_picked_up`
- `orders.mark_delivered`
- `orders.update_status`
- `orders.update_tracking_url`
- `orders.refresh_ezcater_tracking`
- `orders.run_pwck_assignment`
- `orders.run_ezcater_probe`
- `orders.reassign_store`
- `orders.create_in_house_quote`
- `orders.send_quote_email`
- `drivers.approve_delivery_request`
- `drivers.decline_delivery_request`
- `drivers.suspend_driver`
- `drivers.reset_passcode`
- `drivers.update_profile`
- `drivers.approve_mileage`
- `drivers.verify_parking`
- `drivers.close_paycheck`

### Employee/Schedule Actions

- `employee.add`
- `employee.edit_profile`
- `employee.deactivate`
- `employee.reset_passcode`
- `employee.link_to_user`
- `employee.link_to_toast`
- `schedule.create_shift`
- `schedule.edit_shift`
- `schedule.delete_shift`
- `schedule.publish_week`
- `schedule.approve_time_off`
- `schedule.deny_time_off`
- `schedule.approve_shift_offer`
- `schedule.approve_shift_swap`
- `schedule.send_shift_alarm`
- `availability.update_employee`

### Manager/Kitchen/Vendor Actions

- `manager.create_daily_log`
- `manager.create_incident`
- `manager.edit_incident`
- `manager.close_incident`
- `manager.send_team_notification`
- `maintenance.create_request`
- `maintenance.close_request`
- `training.mark_complete`
- `kitchen.update_prep_item`
- `kitchen.update_fresh_food`
- `vendors.add`
- `vendors.edit`
- `vendors.upload_invoice`
- `vendors.mark_paid`
- `produce.ingest_vendor_emails`
- `produce.scan_vendor_inbox`
- `produce.bulk_insert_orders`

### Legal/Finance/Permissions Actions

- `legal.upload_document`
- `legal.edit_matter`
- `legal.edit_insurance`
- `finance.mark_invoice_paid`
- `finance.approve_expense`
- `permissions.approve_access_request`
- `permissions.deny_access_request`
- `permissions.assign_role`
- `permissions.override_user_permission`
- `permissions.create_role_template`
- `permissions.edit_role_template`

### Dev/Ops Actions

- `dev.trigger_render_deploy`
- `dev.set_render_env`
- `dev.run_script`
- `dev.run_git_command`
- `dev.post_dev_chat`
- `dev.archive_dev_chat`
- `dev.cleanup_attachments`
- `dev.restart_agent`
- `dev.toggle_automation`
- `dev.run_prod_sync`

## External / Not Yet Installed Tool Families

These are not Cenas AI tools today, but they are practical additions.

### AI Providers and Model Utilities

- OpenAI Responses/Assistants/Agents SDK provider.
- OpenAI embeddings for approved knowledge base retrieval.
- Anthropic Sonnet/Opus provider with tool use.
- Gemini provider with function calling.
- Model router and fallback policy.
- Prompt/version registry.
- Evaluation harness for tool answers.
- Red-team safety checker.
- Answer citation/source attachment.
- Approved-answer memory.
- Vector database over Cenas docs and policies.
- OCR/image understanding for uploaded receipts/photos.
- Speech-to-text for voice questions.
- Text-to-speech responses.

### Browser / Automation

- Safe browser screenshot tool.
- Safe browser DOM inspect tool.
- pwck ezManage watcher.
- pwck tracking UUID collector.
- ezCater order page parser.
- Toast web report downloader.
- Render health page checker.
- Mobile viewport screenshot verifier.

### Maps / GPS / Routing

- Google Maps geocoding.
- Google Maps Directions API.
- Google Maps Distance Matrix / Routes API.
- Traffic-aware ETA.
- Map tile rendering.
- Static map image generation.
- Route replay from driver GPS fixes.
- Late-risk prediction from current location and delivery window.
- Geofence arrival/departure detection.

### Communications

- Twilio SMS send.
- Twilio SMS receive.
- Email send.
- Email inbox scan.
- WhatsApp send.
- Telegram send.
- Push notifications.
- In-app manager alerts.
- Staff announcements.
- Escalation queue to Sam.

### Integrations

- ezCater Orders API.
- ezCater webhooks if available/approved.
- ezCater delivery tracking page parser.
- ezCater Partner GraphQL backup.
- Toast labor reports.
- Toast employee roster.
- Toast time entries.
- Toast sales reports, partner/corporate only.
- Sling schedule import if still needed.
- Google Drive/Docs/Sheets.
- OneDrive folder sync.
- Render API.
- GitHub API.
- Cloudflare API.
- Sentry API.
- CircleCI/GitHub Actions status.
- QuickBooks/accounting export.
- Stripe/payment status if used.
- Bank/deposit read-only integration if approved.

## Required Guardrails

- Normal staff Cenas AI should never get raw shell, raw SQL, raw filesystem, Render env, deployment, or secret tools.
- Every tool needs a declared permission, role scope, store scope, read/write class, data class, audit log, and refusal path.
- Read-only tools can be broadly enabled faster.
- Write/action tools require explicit confirmation and audit.
- Payroll, sales, customer PII, legal, financial, and permission changes require stricter role gates.
- Employee sessions must stay self-scoped.
- Driver sessions must stay own-driver scoped.
- Store managers must stay store-scoped.
- Partner/corporate can see broader data but still should not receive secrets or raw credentials.
- If a question cannot be answered safely, save it for Sam review with role/scope/source path.

## Recommended Build Order

1. Activate read-only Cenas AI data tools:
   - `orders.catering_count`
   - `orders.catering_today`
   - `orders.catering_needs_driver`
   - `orders.catering_live_tracking`
   - `employee.my_profile.read`
   - `employee.my_schedule.week`
   - `drivers.roster_summary`
   - `drivers.active_delivery_queue`
   - `labor.store_aggregate`
2. Add tool discovery so the bubble shows all active tools by role.
3. Add approved-answer lookup and Sam review UI.
4. Add read-only manager/kitchen/vendor/reporting tools.
5. Add approval-gated actions only after read-only tools are stable.
6. Add OpenAI provider and embeddings after the permission/tool layer is correct.
