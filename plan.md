# plan.md — Cenas Kitchen Operations Platform
Build Specification for a Two-Location Tex-Mex Restaurant Operations System

*Last updated: 2026-05-18. Consolidated amendments from team review (aick, ck, samai, dck) + Sam direction.*

---

## 0. CONTEXT FOR THE BUILDER

You are building an operations management system for a small Tex-Mex restaurant business with two locations in the Houston area. The business is owned by Sam Sahragard and his brother Masood Sahragard, co-owners with full visibility into all operations. The system must replace a patchwork of manual processes, spreadsheets, and disconnected SaaS tools with a single coherent surface that the owner, managers, and drivers all use.

The system has the following user audiences with different needs:
- **Sam + Masood (partner-owners)** — full visibility, all decisions, no operational restrictions. Sam is technical lead; Masood is co-owner with full operational access.
- **Partners** — same tier as owners for system purposes
- **Corporate/GM tier** — multi-store visibility, hiring/firing/scheduling authority, performance oversight
- **Store-level managers** (KM, assistant KM, prep manager, FOH manager, expo) — single-store operational tools, daily ops
- **Drivers** — independent contractors who bid on catering deliveries, see their own data only

The system also has:
- A **manager-facing AI agent layer** (an in-app conversational assistant for managers and partners) — part of the product
- A **Sam-facing AI partner (Cena)** — a live today partner-tier surface at `/sam/chat`, gated to Sam and Masood. Predates Block 3 and is architecturally separate from the manager agent. Cena coordinates the engineering team (aick, ck, samai, dck) via the dev chat at `/partner/developer/chat`.
- An **engineering AI team** (aick, ck, samai, dck) — agents who build and maintain the system, coordinate via the dev chat surface. Not part of the restaurant-facing product but part of the operational infrastructure.

The two physical locations are referred to throughout as:
- **Tomball / "DOS MAS"** (kitchen at 27727 Tomball Pkwy)
- **Copperfield / "UNO MAS"** (kitchen at 15650 FM 529)

Build everything assuming both stores from day one. Single codebase, single database, store-scoped data access enforced server-side.

---

## 1. TECHNICAL FOUNDATION

### 1.1 Stack
- **Backend:** Python 3.12+ with Flask
- **Database:** PostgreSQL in production (SQLite acceptable for local dev)
- **ORM:** SQLAlchemy
- **Migrations:** Alembic patterns; boot-time idempotent backfills for schema-light changes
- **Frontend:** Server-rendered Jinja2 templates. Minimal JavaScript — only where genuinely needed (real-time updates, interactive maps, GPS tracking). No SPA framework.
- **Hosting:** Single web service on Render. Single PostgreSQL instance attached.
- **Background jobs:** Scheduled crons for daily synthesis tasks. No separate worker queue required at this scale. All background jobs run inside cenas-ezlive on Render — not as separate processes on local machines.
- **Static assets:** Served directly by Flask in production; no CDN needed at this scale.

### 1.2 Authentication architecture

Four distinct auth surfaces, intentionally separated:

**A. Site password gate** — a single shared password (env var EZLIVE_PASSWORD) gates the entire app except a small allowlist of public routes. Sets session[auth_ok]. Anti-scraping coarse-grain protection, not user identity.

**B. Keypad PIN auth (for users)** — 5-digit numeric PIN. Users are records in a users table with role-based permissions. PIN is hashed (scrypt). Sets session[user_id] + session[session_version] for invalidation control. PIN is the only mechanism for user-tier login. No email/password login for users.

**C. Phone+PIN auth (for drivers)** — drivers are a separate table from users with their own auth flow. Drivers register with phone + a 5-digit PIN they choose. Sets session[driver_id]. Drivers never become users; users never become drivers. The two tables are architecturally separate and must stay that way.

**D. Partner password** — a second site-level password (env var PARTNER_PASSWORD) gates /partner/* routes for partner-tier surfaces. Layered on top of A.

Auth is enforced via a Flask before_request hook in app/web/auth.py. Maintain an explicit allowlist of unauthenticated routes (login pages, public driver signup, public access request, health checks, ezCater webhook endpoint).

### 1.3 Permission model

Permissions live in two layers:

**Layer 1 — role on user record.** Each user has a single permission_level from a fixed taxonomy:
```
partner, corporate, corporate_chef, gm, km, assistant_km,
prep_manager, foh_manager, expo
```
Nine roles total. driver is NOT on this list — drivers live in the separate drivers table.

**Layer 2 — granular permission grants.** Each role has default permissions (defined in code), and individual users can have grants/revocations layered on top (in a permission_grants table).

Permission check signature: `user_has(user, "permission.key")` returns bool. Use this gate everywhere — route handlers, template rendering, JSON endpoints.

Store-scope is a separate dimension. Users have a store_scope field that determines which stores they can act in. Partners and corporate have store_scope='all'; managers have their specific store slug. Scope is enforced server-side on every data query.

### 1.4 Routing structure

Routes group by audience and purpose:
- `/` — store picker (DOS / UNO / Corporate / Partner tiles)
- `/<store_slug>/...` — store-scoped manager/operational pages
- `/driver/...` — driver-facing pages
- `/partner/...` — partner-only surfaces
- `/sam/chat` — Sam + Masood only AI partner surface (Cena)
- `/keypad/login, /driver/login, /login, /partner-login` — auth surfaces
- `/api/...` — JSON endpoints for in-page interactivity
- Webhook endpoints at predictable, gated paths

URL pattern conventions:
- Slugs are kebab-case (`/store/produce/orders`)
- Active state in nav matches snake_case keys
- Doc pages use a registry-driven dynamic route (`/partner/developer/app/<slug>`)

---

## 2. DATA MODEL (CORE TABLES)

### 2.1 Users and auth

**users** — id, full_name, email (nullable), phone (nullable), permission_level (enum: nine roles), store_scope, passcode_hash (scrypt), active (bool), session_version (int), first_login_done, failed_attempts, last_login_at, created_at, updated_at

**permission_grants** — layered grants/revocations per user

**user_audit_log** — append-only record of every user-state change

**drivers** — id, full_name, phone, email, store_scope, passcode_hash, first_login_done, failed_attempts, session_version, tier (new/trusted/rockstar/top_rockstar), vehicle_info, payment_info (encrypted), active, timestamps

### 2.2 Orders (catering, primary subsystem)

**orders** — id, external_id (indexed), store_slug, reported_store, customer_name/phone/email, delivery_address/lat/lng, pickup_at, delivery_at, status (enum), total_revenue, subtotal, tax, tip, commission, miles_to_delivery (Google Routes authoritative), assigned_driver_id (fk), notes, source (webhook/pdf_ingest/xlsx_import/manual), needs_review (bool), timestamps

**order_items** — line items per order

**driver_requests** — bid pool requests (one pending per driver-order pair)

**driver_logs** — GPS breadcrumbs + shift tracking

### 2.3 Tasks (Block 1A)

**tasks** — id, title, description, created_by_user_id, assigned_to_user_id, store_scope, status (open/in_progress/complete/cancelled/escalated), priority, deadline_at, escalated_to_user_id, parent_task_id, timestamps

**task_audit_log** — append-only state changes

**ribbon_item_dismissals** — per-user dismissal tracking

### 2.4 Operational data

**developer_chat_messages** — id, author, body, created_at, attachment_count

**developer_chat_attachments** — id, message_id, path, mime_type, size_bytes

**sam_chat_messages** — id, session_id, message_id, role, content, created_at (partner-only Cena conversation history)

**ambient_signals** — category (weather/outage/traffic/vendor_status/equipment), store_scope, severity (info/warning/critical), summary, details_json, expires_at, created_at

**sales_insight** — 5am cron output; date, store_scope, summary_text, data_json

### 2.5 Legal (partner-only subsystem)

legal_matters, legal_documents, legal_structure, insurance_policies, legal_audit_log — see §6.6.

### 2.6 Vendors (produce-ordering subsystem)

produce_orders, produce_price_snapshots — see §6.4.

### 2.7 AI agent infrastructure (Block 3)

agent_action_log, agent_journal — see §7.

---

## 3. BUSINESS LOGIC

### 3.1 The catering pipeline

Five entry paths. **Path A (webhook) is canonical and primary.** Paths B, C, and D are legacy back-compat fallbacks — they exist for edge cases and historical imports, not for normal operations.

**Path A — ezCater webhook (canonical, primary).** ezCater POSTs to /ezcater/webhook. Pipeline:
1. Validate webhook signature
2. Fetch full order detail from ezCater Partner API
3. Determine which kitchen serves it (lookup by ezCater's reported store against KITCHEN_ADDRESSES map)
4. Compute mileage via Google Routes API (authoritative)
5. Run auto-resolver (Claude vision/text) — flag if anomalous
6. If clean: persist with status='available'; if flagged: needs_review=true
7. Telegram notification to operations channel

**Path B — PDF ingest fallback.** Manual upload when webhook fails. Claude vision API extracts structure. Same downstream pipeline from step 4. Not a normal path.

**Path C — XLSX bulk import.** Partner-only, historical backfill only. Idempotent upsert by external_id. Skip auto-resolver, skip Telegram.

**Path D — Manual entry.** Partner-only fallback. Marked source='manual'.

**Path E — Live tracking ingest.** Background process polls ezCater GPS API for in-flight deliveries, updates driver_logs rows.

### 3.2 Order status lifecycle

```
available → requested → approved → assigned → picked_up → delivered
                     ↘ declined (back to available)
                     ↘ expired (back to available)
Any state → cancelled (partner override only, audit logged)
```

### 3.3 Driver bidding system (Ez Market)

- Shows all orders in status='available' for the driver's store_scope
- Per-tier pending request caps: new=1, trusted=2, rockstar=3, top_rockstar=5
- Manager approves/declines via /ez-manage
- Approval → order moves to approved, driver gets Telegram notification
- Requests older than 2 hours auto-expire

### 3.4 Driver payroll calculation

Per driver, per pay period: payout = base_rate + (miles × per_mile_rate) + tip. Mileage from orders.miles_to_delivery (Google Routes-computed). CSV export for payroll processing.

### 3.5 Schedule pulling

Sling API → user record via name match → shift grid. 15-min TTL cache.

### 3.6 Sales reporting

Toast orders API across channels: in-store dine-in, online Toast, DoorDash via Toast, Uber Eats via Toast, ezCater (from local orders table, not Toast). 4-hour TTL for historical, 5-min for current day.

### 3.7 Labor reporting

Toast labor API: hours per employee, wages per role, BOH/FOH split. Tomball-only for Toast (Copperfield uses Sling-only).

---

## 4. INTEGRATIONS

### 4.1 Toast (POS)
Auth: API key + restaurant GUID. In-store sales, online sales, third-party routed orders, labor data. Tomball only — Copperfield not yet integrated.

### 4.2 ezCater Partner API
Auth: Partner API key + webhook secret. Webhook is the canonical ingest path. Webhook endpoint must be public (gated by signature, not site password). Idempotency: dedupe on external_id.

### 4.3 Sling
Auth: API key. Staff roster (both stores), schedule grid (both stores). 15-minute TTL cache.

### 4.4 Google Routes API
Auth: Google Cloud service account. ALWAYS authoritative for mileage. Cache by (kitchen_address, delivery_address) tuple.

### 4.5 OpenWeatherMap
Auth: API key. Filter at producer level: only severe weather alerts surface to ribbon. Routine forecasts discarded.

### 4.6 Twilio (SMS) — specced, not built; credentials on file
Status: SPECCED, NOT BUILT. Credentials are on file in 1Password; the integration has not been wired into the app yet. Come back to it when we build. Do not remove from the plan.

Auth (when built): Account SID + auth token + from-number. Escalation alerts, driver notifications. Requires A2P 10DLC registration.

### 4.7 Telegram
Auth: bot token + chat IDs. Ops notifications (new catering orders, anomalies, system alerts). Runs inside cenas-ezlive on Render — not as a separate process on local machines. Async, non-blocking — failures must not crash the request.

### 4.8 Anthropic API
Default: Sonnet for routine work, Opus for complex reasoning, Haiku for high-volume cheap operations. Prompt caching required on agent surfaces. Tool use protocol for the agent surface. No non-Anthropic models in the agent picker.

### 4.9 Render API
Auth: API key. Programmatic deploys, env var management. Restricted to partner-tier agent actions.

### 4.10 Cena gateway + dev chat bridge
The Cena gateway runs on AiCk (port 8765) and bridges Render (where the Flask app lives) to AiCk (where Cena's Claude process runs). The gateway handles: receiving Cena's tool calls from Render, executing them on AiCk, and returning results. The cena_chat_watcher monitors the dev chat and wakes Cena when new messages arrive. The wake-on-post hook in developer_chat.py fires the gateway on every dev chat post. This is live production infrastructure — not a development tool.

---

## 5. USER-FACING SURFACES (PAGE INVENTORY)

### 5.0 Design system
The app has a unified design language anchored to the brand palette. All surfaces must conform. The design system reference doc lives at `/partner/developer/samples` and is maintained by dck (design lead).

**Brand palette:**
- `#C73B36` — Cenas red (primary brand, in-store dominant)
- `#D9A436` / `#E4B340` — gold (active states, section accents)
- `#2EA39F` — teal (third channel color)
- `#FAF6EC` — cream text (base); `#FFFFFF` on hover
- Navy + royal purple (from bull in logo)

**CSS token vocabulary:** All colors, spacing, breakpoints, and radii use `--ck-*` CSS variables. No hardcoded hex values in component CSS. New components must use tokens; deviations require dck sign-off.

**Design pipeline:** dck produces mockups and posts them to the Samples page. Cena reviews and approves structure and organization. ck implements. samai gates the commit. Nothing ships without this sequence.

### 5.1 Root and auth
- `/` — store picker
- `/login`, `/partner-login`, `/keypad/login`, `/driver/login`, `/driver/signup`, `/request-access`, `/logout`

### 5.2 Store dashboard
`/<store_slug>/` — today's catering schedule, pending driver requests, active deliveries, contextual ribbon, quick stats.

### 5.3 Operations tab

**EzCater:**
- `/<store_slug>/orders` — paginated catering order list
- `/orders/view/<id>` — full order detail
- `/ez-market` — driver bid pool
- `/ez-manage` — manager approval queue
- `/<store_slug>/driver-tracking` — driver payroll calculation
- `/<store_slug>/drivers` — driver directory with Active/Inactive tabs at top, defaulting to Active view
- `/ezcater/webhook` — order ingest webhook (canonical path)
- `/orders/ingest` — PDF upload fallback

**Corporate Order, Vendors, Schedule** — per standard spec.

### 5.4 Manager Tools tab (Block 2 — specced, mostly not built)

Daily Manager Log, Shift Handoff, Incident Reports, Supply Requests, Daily Goals, Staff Feedback, Pre-shift Checklist, Close-of-day Audit, Recipe Page.

### 5.5 Insights tab

Performance, Sales, Labor, Forecasts — per standard spec.

### 5.6 Admin tab (partner-tier)
`/partner/team` — user directory, role + permission management.

### 5.7 Developer tab (partner-only)
- `/partner/developer/chat` — AI engineering team coordination surface (aick, ck, samai, dck, Sam, Masood, Cena)
- `/partner/developer/samples` — dck's canonical design mockup and pattern reference home. Every mockup includes a checkbox (approve to implement) and rejection box (text + image attachment for corrections). plan.md is also accessible here.
- `/partner/developer/app/<slug>` — doc pages (specs, architecture, methodology, handoffs)

### 5.8 Sam Chat (partner-owner only)
- `/sam/chat` — Cena AI partner surface, gated to Sam and Masood user IDs only. Live today. Separate from Block 3 manager agent.

### 5.9 Legal (partner-only)
`/partner/legal` and sub-routes — matters, documents, structure, insurance, audit.

### 5.10 Driver app
`/driver/shifts`, `/driver/logs`, `/my-profile`, `/pay-history`, `/driver/change-passcode`

### 5.11 Briefs and anomalies
`/partner/briefs`, `/partner/anomalies/rules`

### 5.12 Manager-facing AI agent surface (Block 3)
`/<store_slug>/assistant`, `/partner/assistant` — chat interface, streaming responses, conversation history persisted per user.

---

## 6. KEY FEATURES IN DETAIL

### 6.1 Task system (Block 1A)
Assignment patterns, deadline + escalation, X/Check controls — per standard spec.

### 6.2 The contextual ribbon (Block 1B/1C)
Persistent strip at top of every authenticated manager page. Categories: open tasks, escalation targets, ambient signals, sales insights, X/Check failures. Per-user dismissal. Filter discipline at producer level — no noise.

### 6.3 Anomaly detection
Rules-based engine watching orders, sales, labor. Configurable at /partner/anomalies/rules. Anomalies → ambient signals → ribbon → optional Telegram alert.

### 6.4 Produce ordering pipeline
IMAP price ingestion → catalog browse → order placement → vendor email with confirm/cancel token → vendor reply → status timeline.

### 6.5 Pay masking (Block 1H)
Query-time filter on labor reports. Each manager sees their own pay and their team aggregate. Partners see everything.

### 6.6 Legal subsystem
Matters, documents, structure, insurance policies, append-only audit log. Partner-only.

### 6.7 Morning brief (Block 1F downstream)
5am cron: pulls Toast sales, labor, catering revenue, weather, local events → Anthropic synthesis → stored in sales_insight → posted to /partner/briefs. Calibration feedback loop.

---

## 7A. SAM-FACING AI PARTNER (CENA) — LIVE TODAY

This is a separate platform from Block 3, predating it and serving a different audience. Documented here as a first-class section so the plan reflects the actual built system.

### 7A.1 What Cena is
Cena is Sam's AI partner — a partner-tier conversational agent gated to Sam and Masood. Lives at `/sam/chat` (see §5.8). Has broader system access than the Block 3 manager agent: she coordinates the engineering team (aick, ck, samai, dck) via dev chat, edits CENA_CHARTER.md / CENA.md / APP_STATUS.md (her lane), monitors infrastructure, and can call Render/Cloudflare/Toast tooling on Sam's behalf.

### 7A.2 Surfaces
- **Primary surface:** `/sam/chat` — Sam-initiated conversation thread, SamChatMessage history persisted per session.
- **Coordination surface:** `/partner/developer/chat` — Cena posts and reads as a participant alongside the engineering AI team.
- **Cross-channel injection:** Cena can use `post_to_sam_chat` (with role='cena') to inject a message into a `/sam/chat` session from a watcher-triggered turn — used when Sam asks her to respond in `/sam/chat` from outside that channel.

### 7A.3 Runtime architecture
- The Cena gateway runs on AiCk (a Windows mini PC in Sam's home office) on port 8765 — see §4.10.
- Render bridges to AiCk via the gateway endpoint (CENA_GATEWAY_URL env var).
- `cena_chat_watcher.py` (also on AiCk) polls dev chat every 2s, fires Cena via the gateway on every new dev-chat post (coalesced with recent history for context continuity). When the Render→AiCk wake-on-post hook (developer_chat.py POST handler) is healthy it fires immediately; the watcher is the always-on belt-and-suspenders backstop.
- Session-start auto-load: CENA.md + CENA_CHARTER.md + APP_STATUS.md + plan.md are loaded into Cena's system prompt at every gateway start, cached via Anthropic prompt caching.

### 7A.4 Distinction from Block 3
- Cena is partner-tier with broader access. Block 3 manager-facing agent (§7) is store-scoped with narrower access.
- Cena's tools include `render_*`, `git_*`, `post_to_dev_chat`, `read_dev_chat`, `post_to_sam_chat`, `telegram_send`, file ops. Block 3 manager agent's tool surface is narrower (approve/decline driver requests, create tasks, query store data).
- Cena predates Block 3. Block 3 is the productized manager-facing AI surface; Cena is the founder-facing partner.

### 7A.5 Discipline rules specific to Cena
- Per cena #2470 charter amendment 7: Playwright interactive testing required for every milestone; only Sam can waive.
- Per Sam #2310 standing rule: agents ping cena by name on task complete / question / result; aick checks Cena connection if she's silent past 20s.
- Per Sam #2342 + #2554: every new dev-chat post wakes Cena (no @cena-mention filter); coalesced + history-aware via watcher v2.

---

## 7. THE MANAGER-FACING AI AGENT SURFACE (Block 3)

### 7.1 Surface
Chat interface at `/<store_slug>/assistant` (manager) and `/partner/assistant` (partner). Streaming responses via SSE. Conversation history persisted per user.

Note: The Sam-facing Cena surface (`/sam/chat`) is architecturally related but separate — see §7A for the full Cena platform. Block 3 is the manager-facing product layer.

### 7.2 Capabilities
Answer questions about store data, take audited actions (approve driver requests, create tasks, post to manager log, log incidents, send Telegram notifications, pull from integrations), suggest actions without taking them, pull historical context.

### 7.3 What the agent cannot do (without partner override)
Hire/fire, change permissions, cross store boundaries, access legal records, modify financial records, run payroll, delete data, make production deploys.

### 7.4 Audit
Every agent action writes to agent_action_log. Viewable at `/partner/agent-audit/` and `/<store_slug>/agent-audit/`.

### 7.5 System prompt structure
Identity, current user context, tool definitions, discipline rules, boundaries. Auto-loaded at session start. Cached via Anthropic prompt caching.

### 7.6 Model selection
Default: Sonnet for routine work. Partners can escalate to Opus. No non-Anthropic models in the agent picker — tool use reliability depends on Anthropic's protocol.

### 7.7 The journal
Per-store journal (agent_journal table) of operational learnings. Agent-authored, manager-visible, partner-editable. Loaded into agent context at session start with size discipline.

---

## 8. CRITICAL CONSTRAINTS AND DESIGN RULES

### 8.1 Mileage policy (locked)
Mileage on catering orders is ALWAYS computed via Google Routes API. NEVER trust ezCater XLSX miles, customer-supplied miles, or manual entry without recomputation. Driver pay depends on it.

### 8.2 Driver-user separation (locked)
Drivers and users are architecturally separate tables. Never merge. Team management forms must not offer driver as a role option.

### 8.3 Store scope enforcement
Every data query MUST be scoped by the requesting user's store_scope. Enforced at the query layer, not the template layer.

### 8.4 Audit trail integrity
Append-only logs are append-only. No deletes, no updates. Write corrective entries; never modify originals.

### 8.5 Never-delete rule for user-facing content
Once a route exists, a sidebar entry appears, a doc page is published — don't remove them. Rename, deprecate, redirect. Remove only after explicit owner approval.

### 8.6 Permission checks server-side
Don't rely on hiding UI elements as a security mechanism. Every action must check permissions server-side.

### 8.7 Credentials and secrets policy (locked)
All credentials live in 1Password and Render env vars only. No credential files on local machines. No credentials in the repo. No credentials through any chat surface — ever. If a workflow tempts someone to paste a credential, the workflow is wrong.

### 8.8 Cost discipline
Prompt caching on system prompt + auto-loaded context. Model defaulting (Sonnet default; escalate only when needed). Batch API for scheduled cron workloads. Per-turn cost visibility in agent UI.

### 8.9 Idempotency on external integrations
Every external integration entry point must be idempotent. Use external_id as dedupe key.

### 8.10 Test discipline — THREE-GATE + PLAYWRIGHT (non-negotiable)

Every behavior change ships with tests. The verification pattern:

- **Gate 1:** Local test suite passes
- **Gate 2:** CI passes
- **Gate 3:** Build-specific verification on production after deploy — hit the URL, query the DB, verify the actual changed surface. Per-change. Do not reuse last week's verification.

**Playwright interactive testing is required for every milestone and every project.** A Playwright test must be written, run, and pass visually in an actual browser before any milestone is called done. The team must see it pass with their own eyes. This is not optional and cannot be waived by Cena, aick, ck, samai, or dck. It can only be waived by Sam directly and explicitly.

### 8.11 Per-agent attribution discipline
Every agent post in the dev chat is signed by name. Every commit message identifies the authoring agent. Proxy pushes are tagged as such. This is an audit requirement, not a style preference.

---

## 9. ORDER OF CONSTRUCTION

Build in this sequence. The design system runs as a standing parallel track — not a one-time step and not deferred to Phase 7.

**Design system track (standing, parallel to all phases)**
dck maintains the design system reference and reviews all user-visible changes. Design mockups live on the Samples page. Implementation requires Cena structural approval + dck's sign-off before ck builds. This track never closes — it runs alongside every phase.

**Phase 1 — Foundation**
1. Database schema (users, drivers, orders, order_items, audit logs)
2. Auth system (all four auth surfaces, before_request hook, allowlist)
3. Permission system (roles, grants, the user_has() helper)
4. Store picker + basic routing
5. Basic admin (team page, add/edit users)
6. Site navigation skeleton

**Phase 2 — Core operations**
1. ezCater webhook ingest + PDF fallback + orders table
2. Ez Market bid pool + driver_requests
3. Ez Manage approval queue
4. Driver app (signup, login, shifts, pay history)
5. Drivers admin (Active/Inactive tabs, default Active)
6. Toast integration + sales reports
7. Sling integration + roster + schedule
8. Labor reports
9. Mileage via Google Routes + driver payroll

**Phase 3 — Block 1**
1. Task system
2. Ribbon component
3. X/Check controls
4. Sales insights cron
5. Team reports
6. Pay masking
7. SMS via Twilio + escalation engine

**Phase 4 — Block 2 (manager tools)**
Daily Manager Log, Shift Handoff, Incident Reports, Supply Requests, Daily Goals, Staff Feedback, Pre-shift Checklist, Close-of-day Audit, Recipe Page.

**Phase 5 — Block 3 (manager agent surface)**
1. Agent action log
2. Tool definitions
3. Agent chat surface per manager
4. Partner agent surface
5. Agent journal
6. Audit page

**Phase 6 — Auxiliary subsystems**
1. Legal subsystem (full)
2. Anomaly detection engine
3. Morning brief composer + calibration loop
4. Produce ordering pipeline
5. Webstaurant, vendor performance, specs

**Phase 7 — Polish and integrations**
1. Forecasts (depends on sales insights)
2. Live GPS tracking surface
3. Customer-facing surfaces (catering booking, status check)
4. KDS (Kitchen Display System)
5. Recipes ↔ inventory loop

---

## 10. SUCCESS CRITERIA

The system is functioning correctly when:
- Catering orders flow from ezCater webhook to driver delivery without manual intervention
- Drivers self-serve their bid pool, get approved/declined cleanly, get paid correctly
- Managers see their day's operational picture at a glance via the ribbon
- Partners can audit any state change at any time via the audit logs
- The manager agent surface answers questions correctly and takes actions reliably
- Cost per turn on the agent surface stays under target (caching + model defaulting working)
- Schedule, sales, and labor reports reflect Toast/Sling data with reasonable freshness
- Drivers are paid the correct amount (mileage + base) every period
- No credentials leak through any chat surface
- Three-gate + Playwright test discipline holds for every change
- The design system is consistent across all surfaces — no rogue colors, no hardcoded hex, no out-of-vocab tokens

---

## 11. NON-GOALS (EXPLICITLY OUT OF SCOPE)

- Not a POS system (Toast does that)
- Not a payment processor (handled by Toast / Stripe / external)
- Not an accounting system (export data to QuickBooks/equivalent)
- Not a marketing platform
- Not a multi-tenant SaaS (one business, two stores)
- Not a developer platform (engineering team tooling is separate from the product)
- WhatsApp integration — removed; not used by the business

---

## 12. CLOSING NOTES TO THE BUILDER

Build for the operator who will use this every day, not for the engineer who maintains it. Pages should be fast to scan, actions should be one click, errors should be specific and actionable. The owner has strong opinions about UX — when in doubt, ask, don't guess.

The audit trails matter as much as the features. The system's value compounds because it remembers everything that happened — who did what, when, why.

Performance matters but not at the expense of correctness. A 200ms page load that shows wrong data is worse than a 600ms page load that shows correct data.

The agent surface is the long-term differentiator. It's not a chatbot — it's a coworker. Tool definitions are part of the agent's API; design them carefully.

The design system is infrastructure, not decoration. Consistency compounds — every surface that matches the design system looks intentional; every surface that doesn't erodes trust in the product.

When you find ambiguity in this spec, raise it. Don't assume. The owner has been thinking about this system for a long time and has answers that aren't always written down.
