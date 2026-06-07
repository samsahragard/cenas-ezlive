# Cenas Corporate Order Module — Build Spec v1

Owner: Sam. Implementer: CK runtime agent. Date: 2026-06-06.
Companion file: `corporate_order_prototype.html` — working reference UI (store + corporate views, desktop + mobile, seeded with the real catalog). Port its markup/flows into the platform; do not redesign.

## Recon findings (live site, 2026-06-06)

`cenaskitchen.com/shop_items` redirects to `/login?next=/shop_items` with the stock Flask-Login "Please log in" flash — the old shop is a **separate Flask app** with email/password auth, static assets at `/static/images/`, routes `/shop`, `/cart`, `/orders`, `/training`, `/admin`, and its own Postgres (`cenas_db`, per the platform tab's own error message). The platform's Corporate Order tab is already scaffolded and expects `CORPORATE_DB_URL` = the cenas_db connection string. Same stack family as the platform → this is a port + upgrade, not a rebuild. Catalog observed: ~39+ products across 14 categories (1-3 Compartment Containers, Aluminum Foil Pans & Containers, Bar, Cleaning Supplies, Foam Cups and Lids, Host & Togo, Office, Plastic & Paper Bags, Portion Cup & Lids, Server, Spices, Supplies Available at Corporate, Togo & Catering, Uniforms), stock badges with low threshold ≈5, Manage Products admin with ID gaps (10–13 missing = deleted rows exist; importer must not assume contiguous IDs).

## End state

The old shop is retired. Stores (Copperfield, Tomball) and Corporate run everything inside the platform's Corporate Order tab using their normal platform login. Stores submit weekly orders on an OH/OR/AVG sheet from phone or desktop. Corporate fulfills with per-line sent quantities and mandatory short-notes, stamped who/when. Corporate inventory is a ledger: decremented by fulfillment, incremented by Webstaurant receipts parsed from email. Usage per store per product is computed continuously and visible at order time.

---

## 1. Migration path (three phases)

**Phase A — Connect (immediate, unblocks the tab):** set `CORPORATE_DB_URL` on Render to the cenas_db Postgres string, redeploy, confirm the scaffold reads it. This gives read visibility while building.

**Phase B — Build + import:** create the module tables below in the platform DB. One-time importer from cenas_db: products (preserve names, images, stock, low thresholds; copy image files into platform static), order history (if old schema permits — this powers AVG from day one), users (map old shop emails → platform accounts; unmatched ones go to a review list for Sam). **Category consolidation (Sam, 2026-06-06): "1-3 Compartment Containers", "Aluminum Foil Pans & Containers", "Plastic & Paper Bags", and "Togo & Catering" merge into "Containers/Bags"; "Foam Cups and Lids" and "Portion Cup & Lids" merge into "Cup/Lids"; "Bar" and "Server" merge into "Bar/Server"; "Uniforms" and "Office" merge into "Uniforms/Office"; "Spices" and "Supplies Available at Corporate" merge into "Spices/Supplies". All merges apply on every surface — store sheet, corporate inventory, usage, reports. Importer remaps; keep the original category in a legacy_category column for traceability.** Old site stays live read-only during parallel run.

**Phase C — Cutover:** one full weekly order cycle runs through the new module for both stores. Then old site's order routes are disabled and `/shop*` redirects to the platform tab. Keep cenas_db as a frozen backup for 90 days.

## 2. Auth, roles, stores

**Auth model (revised by Sam, 2026-06-06): three location logins, not personal accounts** — **Copperfield**, **Tomball**, **Corporate** — each gated by a 4-digit passcode. Initial codes: Copperfield 7745, Tomball 8804, Corporate 9404; rotate after launch. Production requirements: codes verified server-side and stored hashed (never in client JS — the prototype hardcodes them for demo only), 5 failed attempts = 10-minute lockout per location, sessions expire nightly. The "who" on orders and fulfillments is the location principal; per-person attribution within a store is explicitly out of scope for v1. Corporate can change any code from the Users screen. The permission family below still governs authorization, bound to the location principal:

- `corporder.store_order` (store-scoped: create/submit orders for own store)
- `corporder.view_own_orders` (store-scoped)
- `corporder.fulfill` (corporate)
- `corporder.manage_inventory`, `corporder.manage_products` (corporate)
- `corporder.view_usage` (corporate; stores see their own AVG inline regardless)
- `corporder.manage_users` (corporate: grant/revoke the above on platform accounts)

Tab renders the **store view** or **corporate view** based on the location principal. Two stores, one corporate, exactly as today. The Users screen inside the tab manages the three location passcodes (change/rotate, view lockout state); named per-person users are a possible v2 if attribution ever matters.

## 3. Data model

- `corp_product`: id, name, category, case_size_label, image_path, low_threshold (default 5), active, created_at. Import preserves old IDs where possible.
- `corp_inventory_txn` (ledger — never mutate a bare count): id, product_id, delta, reason (`receipt` | `fulfillment` | `adjustment` | `migration`), ref_type/ref_id, actor_user_id, note, created_at. **Corporate on-hand = SUM(delta).** Migration seeds opening balances from old stock counts.
- `corp_order`: id, store_id, created_by, status (`draft` | `submitted` | `sent` | `partially_sent` | `received`), submitted_at, fulfilled_by, fulfilled_at, received_by, received_at.
- `corp_order_line`: order_id, product_id, **oh_qty** (store's on-hand at order time, required when order_qty > 0), **order_qty**, **avg_snapshot** (AVG shown at submit, frozen for history), **sent_qty** (nullable until fulfilled), **short_note** (required when sent_qty < order_qty).
- `corp_receipt`: id, source (`webstaurant_email` | `manual`), vendor_order_no, email_msg_id, status (`pending` | `applied` | `dismissed`), parsed_at, applied_by, applied_at.
- `corp_receipt_line`: receipt_id, raw_description, sku, qty_parsed, qty_applied, product_id (nullable until mapped).
- `corp_sku_map`: vendor_sku/raw_description_hash → product_id (map once, remembered forever).
- `corp_usage`: store_id, product_id, avg_weekly_auto, avg_weekly_override (nullable; override wins), datapoints_count, computed_at. Nightly job.

## 4. OH / OR / AVG — the order sheet logic

Columns the store sees per item: **AVG** (read-only), **OH** (input), **OR** (input/stepper).

- **OH is mandatory when OR > 0.** Submit blocks until every ordered line has an OH count. This is the whole point — it forces a weekly shelf count and gives true consumption data.
- **AVG** = 90-day average weekly usage, shown as `X/wk` with `~Y/day` subtext. Computation, per store per product:
  - **Consumption-based (preferred, once ≥2 OH datapoints exist):** for each pair of consecutive orders, usage = OH_prev + sent_qty_between − OH_now; weekly rate = usage ÷ weeks between; AVG = mean of weekly rates across the trailing 90 days. Negative intervals (miscount) are dropped from the mean and logged.
  - **Order-based fallback (while history accrues / after import):** total sent_qty in trailing 90 days ÷ 12.857.
  - **Corporate override wins** when set (Sam said corporate fills it; the system computes, corporate can overwrite, clearing the override returns to auto). Overridden values render with a marker.
- avg_snapshot is stamped on each line at submit so history reads true later.
- **Deviation flag:** OR > 2× AVG (or AVG null and OR large) renders a soft "≫ avg" tag on the line and in the corporate queue — pattern tracking that catches over-ordering without blocking it.

## 5. Store flow

Order Sheet (search + category chips + availability badges from corporate ledger) → fill OH/OR → sticky cart bar → Review (validation: missing OH listed by name) → Submit (status `submitted`). My Orders: status chips, per-line Ordered vs Sent with corporate notes, who sent and when, **Confirm Received** button (status `received`; received confirmation also timestamps the delivery interval used by the AVG job). Out-of-stock items render Unavailable (old behavior preserved; see assumption 4).

## 6. Corporate flow

Queue of `submitted` orders by store. Open order → fulfillment sheet: per line shows Ordered, store's reported OH, corporate on-hand, **Send** input defaulting to min(ordered, on-hand). Any line with Send < Ordered requires a **note** (hard validation — this is the "leave notes for that item" rule). **Mark Sent** stamps fulfilled_by + fulfilled_at, writes `fulfillment` ledger txns (−sent_qty each), sets status `sent` or `partially_sent`. Everything lands in the activity feed.

## 7. Inventory + Webstaurant email ingestion

Inventory screen = ledger view: on-hand, low badge, Receive and Adjust actions (both write audited txns with actor).

**Webstaurant pipeline:** IMAP scan of the corporate inbox (same pattern as `produce.scan_vendor_inbox` — reuse that ingestion service, new parser). Order-confirmation and shipping emails parse into a `pending` corp_receipt with lines. Unmapped lines sit in a mapping queue; corporate maps a SKU to a product once and `corp_sku_map` remembers it. Corporate opens the pending receipt, edits quantities if the box came up short or damaged, taps **Apply** → `receipt` ledger txns (+qty), status `applied`. Email never silently mutates inventory — pending → human apply is the safety. Manual "Log receipt" exists for walk-in/other-vendor stock. Nightly scan + manual refresh button.

## 8. Usage & ordering patterns

Corporate Usage screen: per store, table of product / computed AVG / override / last reported OH / last OR / deviation count. Per-user ordering log (who submitted what, when) comes free from corp_order.created_by. These views are read models over data the flow already captures — no extra entry work for anyone.

## 9. UI requirements

The prototype HTML is the reference: dark ops-platform theme matching the tab it lives in, single column that scales from 380px phones to desktop, 44px touch targets, `inputmode="numeric"` on all count fields, sticky cart bar with safe-area padding, category chips horizontal-scroll on mobile. Port as Jinja templates + the platform's static pipeline; behavior and validation rules must match the prototype exactly.

## 10. Assistant tie-in (later wave, free leverage)

Once live, register read tools in the assistant registry: `corporder.queue_summary`, `corporder.store_order_status`, `corporder.inventory_lookup`, `corporder.usage_summary` — "did corporate send Tomball's order," "what's on-hand on sternos," answered in chat. They follow the standard handler contract from the wiring spec.

## 11. Tests

OH-required validation (submit blocked, per-line errors). Short-note-required validation. Ledger invariants (on-hand never drifts from txn sum; fulfillment can't send below zero without an adjustment). AVG math fixtures: consumption-based with 3 order cycles, fallback path, override precedence, negative-interval drop. Receipt apply idempotency (double-tap can't double-credit). Store scoping (Copperfield session can never read Tomball orders — adversarial test). Importer round-trip counts vs cenas_db.

## 12. Assumptions (override with one line each)

1. Weekly cadence, but the system doesn't enforce an order day — any day works, AVG math uses actual intervals.
2. Order history from the old shop is importable; if the old schema can't yield it, AVG starts in fallback mode and matures after ~3 cycles of OH data.
3. Old shop users map to platform accounts by email; unmatched go to Sam for manual mapping.
4. Out-of-stock items stay Unavailable to stores (old behavior). Alternative — allow ordering as a backorder request that auto-shorts — is a one-flag change if you want it.
5. Received-confirmation by stores is encouraged but not blocking; unconfirmed orders auto-close after 7 days for AVG purposes.
6. Bar syrup bottles and uniforms participate in OH/OR like everything else; AVG will simply be sparse for as-needed items.
