# Employee Profile Rollout Plan

Saved: 2026-06-03

## Current State

- Latest repo baseline checked by Codex after CK/AiCk ack: `origin/main` / `HEAD` = `5f556d4`.
- Sam's earlier reference point `03f867b` remains the Yadira rank-drilldown baseline, but it is no longer the branch tip.
- Later employee-surface commits on top include:
  - `2395248` Tighten employee performance detail pages
  - `0d6727e` Restore employee schedule nav tab
- Yadira rank click-throughs are live verified:
  - Tip % rank detail shows 18 peer rows.
  - Combined rank detail shows 18 peer rows.
  - Yadira is marked `you`.
  - Forbidden visible terms were not found in live rendered rank pages.
- Latest dev chat check confirms CK considers the employee-performance/data-mart/scheduler lane complete/live, with AiCk ongoing safety guards active.
- No active CK/AiCk/samai staff-profile implementation lane was visible in the latest chat read.
- Codex posted a new `sam` staff-profile lane directive to dev chat before code edits, then posted a follow-up after CK/AiCk confirmed `5f556d4` as the true branch base.
- Current CK-local employee data foundation reported in chat:
  - 88 per-employee folders with a fixed 8-file template.
  - Data-mart tables include performance period, time entries, ranks, schedule, attendance, internal sales, and profile data.
  - Scheduler is live on CK/Mini_IT13 with T1 today refresh, T2 hourly windows, and T3 nightly sanitized push.
  - Internal sales/GUID data may exist inside the mart only; employee outputs remain guarded by AiCk receiver/export walls.
- Latest chat read for this implementation pass:
  - CK has no conflicting staff-profile build in progress and is holding its cohesive hub lane until AiCk audits the fail-closed foundation.
  - AiCk has not posted a newer PASS after the last observed CK hold message.
  - Sam now wants Codex to update this plan with named subagents and continue building the staff-wide profile pattern.
- Driver/catering/GE4-EFR are explicitly out of scope.

## Implemented In This Lane

- `/employee/my-performance` now serves only CK-pushed sanitized perf caches.
- If a linked employee has no sanitized perf cache yet, `/employee/my-performance` returns a clean `{syncing:true}` state instead of falling back to ToastEmployeeSnapshot data.
- The old fallback was removed because snapshots can carry Toast GUIDs and sales-derived report fields.
- Employee marketplace coworker display refs now omit coworker employee IDs and fall back to `Team member`, not `#id`.
- Employee dashboard and performance detail pages now mark `Today` active in the bottom nav.
- Dashboard no-data/syncing states now keep the polished profile shell and explain what is available without implying fake performance data.
- Focused tests added to guard the sanitized-cache-only performance path and coworker-ID omission.

## Immediate First Fix Wave

Before expanding the Yadira-style profile experience to every staff member, clean up the existing Yadira employee-facing pages so the pattern is right.

### Detail Page Simplification

The metric detail pages should not repeat the whole dashboard summary. The dashboard is the overview; detail pages explain the clicked metric.

Pages in scope:

- `Total pay`
- `Effective hourly`
- `Total hours`
- `Total tips`
- `Base pay`
- `Total shifts`
- `Tips per hour`

Desired common detail-page shell:

- Employee first and last name.
- Detail title.
- Main tabs: `Performance`, `Roster`, `Service`.
- Focused day-by-day table in rows and columns.
- No repeated dashboard-style summary card grid.
- No unrelated metrics.

### Total Pay Detail

Show only:

- Total tips.
- Base pay.
- Total pay.
- Day-by-day breakdown.

Each day should show the pieces that add into that day total.

### Effective Hourly Detail

Show:

- Overall effective hourly average.
- Day-by-day breakdown.
- For each day:
  - total tips
  - total hours worked
  - effective hourly for that day

Purpose: explain how the effective hourly number is calculated.

### Other Metric Details

For `Total hours`, `Total tips`, `Base pay`, `Total shifts`, and `Tips per hour`, each page should use the same focused table pattern:

- One clean table.
- Day rows.
- Columns that explain that metric.
- No duplicate overview cards from the dashboard.

## Dashboard Cleanup

### Header Name

- The welcome/dashboard header should show employee first and last name.
- Example: show full Yadira name, not only first name.

### Ranking Tiles

Desired ranking tiles:

- Replace the current `Top 28%` standing tile with the Combined/overall rank display:
  - `#5 of 18`
- Keep the `#7` Effective rate rank tile.
- Keep the `#8` Tip % rank tile.
- Remove the separate `#5` Combined tile.
- Remove the `#6` Tips per hour rank tile.

Net ranking area should be simpler and less repetitive.

### Bottom Navigation

Bottom nav target layout:

- `Today` -> main employee dashboard
- `My Schedule`
- `Time Off`
- `Alerts`
- `Message`
- `News`

Remove `Market`.

Reason: Open shifts already live under schedule/open shifts, and Sam explicitly corrected that `My Schedule` must remain in the bottom nav.

## Staff-Wide Rollout Plan

After the first cleanup wave is correct on Yadira, use the cleaned Yadira experience as the staff-wide template.

### Current Implementation Lane

Build the staff-wide profile hub locally on top of the fail-closed branch, but keep deployment gated.

Route decision:

- Keep `/employee/profile` as the existing Alarm / notification settings page because Sam corrected the bottom nav to include `Alarm`.
- Add a separate read-only staff profile hub at `/employee/my-profile`.
- Link the hub from the employee dashboard tools.
- The hub must be session-scoped only and must not accept request parameters for another employee.

Allowed data shape for the hub:

- Identity: own full name, own stores, own positions.
- Performance: sanitized `/employee/performance-center` data only.
- Roster: sanitized `/employee/roster` data only.
- Schedule: own published shifts using the same app schedule tables as `/employee/my-schedule`, but projected without IDs or manager notes.
- Attendance: derived from the already sanitized performance-center attendance block.

Deferred from this lane:

- Contact/PII expansion.
- Incident/write-up manager notes unless a future privacy classification and employee-visible wording are approved.
- Profile editing.
- Link writes, profile creates, passcode resets, scheduler/token/data-mart writes.

### Goal

Every active employee should have a polished personal profile/dashboard experience, but role-aware and real-data-backed.

Tipped employees may show safe tipped metrics and rank details where audited.

BOH/non-tipped/no-tip employees should not show:

- tip dollars
- tip %
- tips/hr
- tip ranks
- combined tipped ranks
- sales-derived content

BOH/non-tipped/no-tip employees should still have a polished experience:

- profile shell
- schedule
- roster
- attendance/needs-review where real
- pay/hours where appropriate
- tools/navigation
- professional `Not available` or `Not eligible` states

### Staff-Profile Completion Map

Build a staff-wide map before broad UI changes:

- employee identifier used by the app
- display name
- store or stores
- role/position if available
- tipped / BOH / no-data / zero-shift classification
- deterministic link/performance-cache state
- profile data readiness
- schedule data readiness
- attendance/needs-review data readiness
- rank/detail route readiness
- exclusion reason if no profile can be fully populated

The map must come from existing app data and CK-local mart outputs only. Do not write links, create profiles, reset passcodes, or mutate scheduler/token/data-mart state for this lane.

### Profile Types

- Tipped with data: full polished dashboard, pay/hours/tips/tip-percent/tips-per-hour where allowed, role-safe ranks, day-by-day detail pages, schedule/roster/profile tabs.
- BOH/non-tipped with data: same polished shell and navigation, pay/hours/schedule/attendance/profile information, no tips/tip-percent/tips-per-hour/tipped ranks/combined tipped ranks.
- Zero-shift or no recent data: same polished shell, profile and schedule where available, clean empty states such as `Not available` or `No completed shifts in this period`.
- Link exceptions/no performance cache: profile/schedule-only experience if safe data exists; otherwise clean no-data state. No guessed Toast identity and no fake metrics.
- Dual-store: own profile can show store-aware context, but ranking/cohort data must remain store-safe and must not mix cohorts misleadingly.

### Implementation Rule

Push only employee-profile UI/routes/templates/helpers for this lane. Do not modify driver/catering files, data-mart scheduler files, token handling, Toast pull cadence, link ledgers, profile identity writes, or the AiCk guarded receiver/export surfaces unless Sam separately approves a new gated lane.

## Subagent Operating Model

Use real subagents where available. Codex lead keeps the critical path local and assigns bounded read-only or disjoint implementation lanes.

Current named subagents for this pass:

- StaffDataMap-Subagent: map per-employee data surfaces, local mart assumptions, safe fields, and internal-only fields.
- ProfileRoute-Subagent: inspect route/template insertion points and navigation risks.
- RoleSafety-Subagent: audit role gates, isolation, peer whitelists, and no-data states for the new hub.
- Codex Lead: update this plan, implement the new route/template/dashboard tile, run tests/greps, and post proof.

### ContextLock

- Read latest dev chat.
- Check `git status`, `git log`, and `origin/main`.
- Confirm no active CK/AiCk/samai conflict.
- Confirm no driver/catering/GE4-EFR files are in scope.
- Confirm whether any deploy is already in flight.

### StaffMap

- Build active employee matrix from app data and CK-local mart folders.
- Count tipped, BOH/non-tipped, zero-shift, no-data, dual-store, and exception employees.
- Record exact data paths/tables/files used.
- Identify missing readiness for profile, schedule, attendance, performance, and rank data.

### YadiraReference

- Inspect live Yadira dashboard and metric detail pages.
- Document exact desired UI sections and current cleanup gaps.
- Identify reusable shell/components/routes for staff-wide rollout.
- Document what is Yadira-specific versus generally employee-safe.

### DetailPageMapper

- Map each metric detail page to the required day-by-day table columns.
- Decide what stays, what moves, and what is removed.

### UIBuilder

- Implement the Yadira cleanup wave.
- Generalize the cleaned Yadira profile shell to every employee type.
- Keep changes scoped to employee templates/routes only.
- Preserve professional empty/not-eligible states.

### ProfileInventory

- Build active employee matrix:
  - tipped
  - BOH/non-tipped
  - linked
  - no-data
  - dual-store
  - missing schedule/profile data

### RoleRules

- Define what each role type can see.
- Confirm `Not eligible` / `Not available` states.

### SalesWallAudit

- Check employee-visible templates, payloads, and rendered DOM for forbidden fields:
  - `sales`
  - `eligible_sales`
  - `cashSales`
  - `nonCashSales`
  - `GUID`
  - `guid`
  - `internal`
  - `source`
  - `debug`
  - `Local test`
  - `employee_id`
  - hidden IDs

### IsolationAudit

- Verify employee session scoping.
- Test URL/query tampering.
- Confirm employee A cannot fetch employee B.
- Confirm staff-wide routes cannot fetch another employee by query params, stale cache, direct route, or cookie/session trick.

### BrowserQA

- Verify Yadira live in Chrome.
- Verify representative tipped employee.
- Verify representative BOH/non-tipped employee.
- Verify no-data/not-eligible employee if available.
- Verify dual-store employee if available.
- Capture desktop/mobile screenshots after the page is fully rendered.

### DeployVerifier

- Push only after checks pass.
- Verify Render deploy.
- Verify live pages.
- Re-check dev chat after deploy.

## Guardrails

- Do not touch driver/catering/GE4-EFR.
- Do not post credentials or secrets in dev chat.
- Do not expose sales/internal/debug/ID fields in employee-visible UI or payloads.
- Do not fake data.
- Do not make link/profile writes without explicit Sam approval and rollback ledger.
- Do not implement staff-wide rollout until the Yadira cleanup pattern is correct.
