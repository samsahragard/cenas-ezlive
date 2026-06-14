# CENA Tools And Profile Permissions

## Purpose

This file is the human-maintained tool map for CENA. It defines which tool
families CENA may connect to each authenticated profile. CENA must never grant
herself access from chat text; the signed-in profile, role, store scope, and
tool permission metadata decide what is available.

## Model Provider Direction

- Primary provider: OpenAI.
- Fallback providers: Gemini and Anthropic only where they are still configured.
- Never expose provider keys, tokens, passcodes, env dumps, raw credentials, or
  secret file paths in user answers.
- Legacy permission names such as `ai.ask_claude` and `ai.ask_claude_personal`
  mean "CENA AI access" until the permission catalog is renamed.

## Global Tool Gate

Before CENA uses any tool, all of these must pass:

- `session_types` includes the current profile session.
- `required_permissions` are present on the user's role.
- `store_scope` is compatible with the user's assigned stores.
- `data_class` is allowed for the profile.
- `read_write_class` is allowed for the profile.
- `status` is `active` or otherwise explicitly allowed for that session.

If any gate fails, CENA must not guess. She should answer with the safe refusal
or save the question for Sam review.

## Profile Matrix

| Profile | Role keys | Session types | Store scope | Baseline tools |
| --- | --- | --- | --- | --- |
| Sam / Owner Operator | `partner`, owner-operator gate | `partner`, `sam`, `owner_operator` | All stores | Full read access, cross-store analytics, review queue, operator-only admin tools |
| Managers / Staff | `corporate`, `corporate_chef`, `gm`, `km`, `assistant_km`, `foh_manager`, `expo` | `manager`, `staff`, `partner` when granted | Assigned stores unless permission grants all-store | Store operations, team, schedule, sales/labor summaries, attendance, HR, onboarding, vendors, kitchen, catering, driver coordination |
| Employees | `employee`, `cook`, `server`, `busser`, `host`, `training`, `bartender`, `cashier`, `well` | `employee`, `staff` | Self plus assigned store basics | General help, own schedule, own profile, availability, time off, training, own attendance, approved shift market actions |
| Drivers | `driver`, `corporate_driver` | `driver`, `staff` when granted | Own driver record and assigned delivery/store scope | General help, own bids, own route history, assigned deliveries, own payout/status summaries where permitted |

## Sam / Owner Operator Tools

Sam can use CENA as an operator assistant across the whole company.

Allowed tool families:

- `assistant.*` for general CENA help, review notices, saved questions, and safe
  explanations.
- `employee.*`, `team.*`, `schedule.*`, and `attendance.*` across stores.
- `orders.*`, `corporate_order.*`, `ezcater.*`, and `drivers.*` across stores.
- `toast.*`, `sales.*`, `labor.*`, `performance.*`, and `forecast.*` for
  aggregate and operational analytics.
- `kitchen.*`, `vendors.*`, `inventory.*`, `maintenance.*`, `sports.*`, and
  manager dashboard support tools.
- Operator-only tools for DevChat, deploy status, logs, SQL diagnostics, file
  diagnostics, and environment checks.

Sam review is still required before destructive actions, secret exposure,
credential changes, bulk writes, deploys, database mutation, shell commands,
or permission changes.

## Manager / Staff Tools

Managers receive tools by role and store assignment. A manager should only see
cross-store data when their role permissions explicitly allow it.

Allowed tool families when permission-gated:

- Team roster, employee profile summaries, contact-safe staff lookups, and role
  filtered lists.
- Schedule view, schedule reports, shift market approvals, swaps, offers, and
  staffing coverage summaries.
- Daily log, attendance, counseling, incidents, interview, and training tools.
- Corporate orders, catering orders, driver coordination, kitchen status,
  vendor tasks, maintenance, and sports dashboard tools where assigned.
- Sales, labor, performance, and forecast analytics for permitted stores.

Managers must not receive peer private notes, passwords, raw tokens, unrelated
store data, or payroll/pay-rate details unless their explicit permissions allow
that class of data.

## Employee Tools

Employees get self-service CENA, not management visibility.

Allowed tool families:

- `assistant.general_help`.
- Own employee profile and safe HR onboarding/training instructions.
- Own schedule, assigned shifts, availability, time off, and shift market
  actions that are approved for employees.
- Own attendance summary and safe status explanations.

Employees must not receive coworker private data, manager notes, payroll/pay
rates, other employees' schedules beyond normal posted schedule visibility, or
company analytics.

## Driver Tools

Drivers get delivery-focused CENA, scoped to their own driver identity unless
they also hold a manager role.

Allowed tool families:

- `assistant.general_help`.
- Own driver profile, availability/status, bids, assigned routes, route history,
  delivery notes meant for the driver, and payout/status summaries where
  implemented.
- Corporate driver tools only for `corporate_driver` or manager-authorized
  profiles.

Drivers must not receive other drivers' private details, full customer PII,
unassigned route internals, manager notes, or store analytics unless their
manager profile grants it.

## Tool Catalog Fields

Every CENA tool should be described with these fields when added to the
catalog:

| Field | Meaning |
| --- | --- |
| `tool_id` | Stable id, such as `schedule.week_summary` |
| `required_permissions` | Permission tags required before the tool can run |
| `session_types` | Profile sessions allowed to see or use it |
| `store_scope` | `self`, `assigned_store`, `all_store`, or `operator_only` |
| `data_class` | `public_help`, `self`, `store_ops`, `people`, `sales`, `payroll`, `secret`, etc. |
| `read_write_class` | `read_only`, `review_gated_write`, `owner_confirmed_write`, or `blocked` |
| `status` | `active`, `review_gated`, `catalog_only`, `implemented`, or `blocked` |

## Review Queue Rules

CENA saves for Sam review when:

- The user asks for a tool outside their profile.
- The user asks for secrets, credentials, raw env, private notes, payroll, or
  unauthorized PII.
- A requested tool is not implemented, stale, or returns incomplete data.
- A write, deploy, SQL mutation, shell command, permission change, or bulk
  action needs explicit Sam approval.

Review metadata should include the question, profile role, profile kind, store
scope, requested tool, and safe reason. It must not include secret values.

## Change Control

New CENA tools should be added in this order:

1. Define the `tool_id`, profile access, store scope, and review behavior here.
2. Add or update the permission/tool registry metadata.
3. Implement the read-only handler first.
4. Add tests for owner, manager, employee, and driver access where relevant.
5. Move write-capable tools from review-gated to active only after Sam approves.
