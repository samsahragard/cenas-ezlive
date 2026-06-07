# Prod Smoke Technical Review Response - 5b61c36

Date: 2026-06-07
Author: ck codex
Status: HOLD. No employees wave, no Corporate Order Phase A, no classifier flag-on.

## Spec Questions

1. Sales scope parsing

Date scope is parsed in the Render assistant route layer, not by the Toast analytics handler. `assistant_routes._toast_period_from_question()` maps `last week` to `last_week`, any `week` phrase to `week`, and everything else to `today`. `_approved_tool_handlers()` then calls `_toast_sales_summary_tool_payload(period)`, which forwards only that named period to `analytics_summary_payload()`.

`toast.sales_summary` does not accept an arbitrary date, yesterday, requested store, or restaurant id scope today. It accepts only `today`, `week`, and `last_week`; unknown periods normalize back to `today`.

The A1/A3 byte-identical answer is explained by the current helper: `period_to_ymd_range("week")` defines week as Sunday-through-today. The run was Sunday, 2026-06-07, so `week` and `today` both resolved to `20260607..20260607`. That may match the dashboard pill, but the answer must label the date range or the product should change the business definition.

2. Catering order lookup

`catering_order_lookup` does not receive a parsed order id. The router calls the handler with the whole question, and the handler calls `_find_order(orders, question)`. `catering_order_items_safe` uses the same helper.

The bug is `_lookup_token()`'s first regex. For `Look up catering order W7T-UF9`, it can match `catering order` and capture `order` as the token. `_find_order()` then fails to find that token and falls back to the latest sorted order, which is why B8 returned `XGC-T07`. The items prompt worked because `What items are on order W7T-UF9` lets the same regex capture `W7T-UF9`.

3. Open shift mismatch

`schedule.store_week` counts all shifts in the current local week, Monday through Sunday, and counts open as `status == "open" or employee_id is None`.

`schedule.open_shifts` counts only open/unassigned shifts whose `start_at.date() >= today`.

On Sunday, 2026-06-07, `store_week` can still report earlier-week open shifts while `open_shifts` reports zero future/today open shifts. This is a date-window mismatch and output-label problem, not necessarily a data-source breach. The authoritative answer needs to be defined: "open shifts this week" or "open shifts remaining from today forward."

4. `schedule.store_today` store/scope filter

`schedule.store_today` uses `_allowed_shifts() -> _allowed_schedules() -> _tool_store_filter(ctx)`.

Actual scope behavior:

- owner-operator: all schedule stores
- non-owner: normalized `ctx["store_slugs"]`
- no allowed stores: empty result
- it does not use `ctx["current_store"]`
- it does not parse a requested store from the question

For Sam as owner/operator, a Tomball-only answer means the schedule/shift rows available for that local date only contained Tomball shifts, or Sam was not treated as owner-operator in that session. The code does not use sales activity to infer that Copperfield should have schedule rows.

5. Labor/store/week/acceptance totals

The counts are different scopes:

- `labor.store_aggregate`: all allowed employees plus all shifts on published schedules, not limited to the current week. It also reports last-30 cached labor hours.
- `schedule.store_week`: current-week shifts by shift date, regardless of published/draft status.
- `schedule.shift_acceptance_summary`: all assigned allowed shifts, no week/date/status restriction, with missing acceptance rows treated as pending.

So 2342, 80, and 1980 can all be internally explainable, but the assistant answer does not say enough for a manager to reconcile them.

## Fix Plan Mapped To Sam's #1-#6

1. Sales scope ignored

- Add explicit scope args to `toast.sales_summary`: period, start date, end date, requested store/restaurant ids when supported.
- Support `yesterday`; if a scope is not implemented, queue with a specific unsupported-scope message.
- Add date-range labels to every Toast Analytics answer.
- Tighten A8 so "last Toast data" routes to webhook/freshness, not sales summary.
- Add a low-denominator guard for labor ratio in the formatter.

2. Order lookup ignores order id

- Replace `_lookup_token()` with a stricter extractor that prefers explicit external-id patterns and ignores structural words like `order`, `catering`, `ticket`.
- Pass parsed route args to handlers where exact lookup matters.
- Add answer assertion: requested order id must appear in the returned answer and payload order id.

3. Schedule open-shift contradiction

- Pick one authoritative definition for "open shifts":
  - remaining open shifts from today forward, or
  - all open shifts in the current week.
- Rename/label outputs so `store_week.open_shift_count` and `open_shifts.count` do not appear contradictory.
- Add reconciliation assertion for same-scope questions.

4. Unguarded labor ratio

- Suppress or qualify labor percent when orders/sales are below a floor or day is too early.
- Example output: "Labor percent is not shown yet because only 5 orders are posted today."

5. Aggregate questions answered by arbitrary row

- Ensure aggregate intents hit aggregate tools only.
- For `items ordered most`, require `orders.catering_item_mix` to return aggregate counts, not one order's line items.
- If aggregate support is absent or data is inadequate, queue instead of falling back to lookup/latest order.

6. Unreconciled counts

- Add inline scope to count answers: current view, all visible orders, current week, remaining today-forward, all published schedules, last 30 cached hours.
- Add source/count sanity checks for by-status and by-store totals; allow known differences only if the answer labels exclusions.

## Upgraded Smoke Corpus Shape

Each prompt needs both routing and output assertions.

```json
{
  "id": "A1",
  "question": "What were sales today?",
  "expected_route_path": "deterministic",
  "expected_tool_id": "toast.sales_summary",
  "assertions": [
    {"type": "answer_contains", "value": "Today Toast Analytics"},
    {"type": "payload_period", "value": "today"},
    {"type": "answer_contains_date_range", "scope": "today"},
    {"type": "labor_ratio_guard", "min_orders": 20}
  ]
}
```

Required assertion classes:

- `mirror_present`: every prompt has a Sam mirror row with route_path and tool_id.
- `no_502`: UI answer is not "could not reach assistant"; runtime health is clean.
- `dangerous_review_only`: dangerous prompts have `route_path=review`, `tool_id=null`, queued true.
- `expected_tool`: exact route/tool match.
- `payload_scope`: period/date/store/window args match the prompt.
- `answer_echoes_requested_id`: order lookup/item lookup answers include the requested external id.
- `answer_not_prior_turn`: answer cannot match the previous prompt's answer.
- `answer_not_generic_fallback`: specific intents cannot fall to store summary unless allowed.
- `aggregate_not_single_row`: aggregate questions cannot answer with one arbitrary order.
- `count_reconciles`: same-scope counts match; different-scope counts require inline labels.
- `grammar`: singular/plural manager-facing text.

Seed prompts that must be upgraded first:

- A1 sales today: `toast.sales_summary`, today date range, labor ratio guard.
- A2 sales yesterday: route to scoped sales if implemented, otherwise specific unsupported-scope queue.
- A3 sales this week: week date range; on Sunday either explain "week-to-date is today only" or change week-start definition.
- A8 last Toast data: `toast.webhook_activity` or queue, never sales summary.
- B8 order lookup W7T-UF9: answer and payload must echo W7T-UF9.
- B13 most ordered catering items: aggregate tool with ranked counts or queue.
- B14 items on W7T-UF9: answer echoes W7T-UF9 and item rows.
- B16 missing PDFs: `orders.catering_pdf_status`, not store summary.
- B20 returning customers: `orders.catering_returning_customers_aggregate` or queue.
- D1 who's working today: `schedule.store_today`, scope label includes all stores/current store.
- D2 this week's schedule and D3 open shifts: same-scope counts reconcile or labels explain different windows.
- D4 pending time off: `schedule.time_off_pending`, current scope label.
- A19-A29 plus R8 dangerous prompts: `route_path=review`, `tool_id=null`.

## Consolidation / Drift Status

`assistant_safety.py` is shared by Render and CK runtime for force-review and contextual-follow-up disabling. Deterministic tool-choice is not fully consolidated yet: route matchers still live in `assistant_routes.py` plus handler modules, and CK runtime still has its own period parsing, route args, answer formatting, and weak answer verification. I did not find a true Render-vs-CK golden drift test; the next gate needs one before employees wave.
