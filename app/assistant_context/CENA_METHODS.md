# C.E.N.A. Investigation Methods

Operator instinct encoded as structured playbooks. The reasoning planner SELECTS the
playbook matching the question class and FOLLOWS its decomposition. These are not
rigid scripts — they are the default investigation path; adapt when the data argues
for it, but never skip the cross-checks or the same-weekday anchoring.

Sections are machine-extracted by the `## CLASS:` markers — keep those headers exact.

## STANDARDS (apply to every investigation)

- **Prime cost** target is ~55-60% of sales for full-service; flag sustained excursions.
- **Labor is never one number.** Report labor % WITH SPLH (sales per labor hour) AND OT
  exposure together. Labor % alone misdiagnoses a busy week as a labor problem; SPLH
  normalizes for volume.
- **Anchor comparisons same-day-of-week and same-day-part.** Monday vs Monday, lunch vs
  lunch. Never compare a Saturday to a Tuesday and call the gap a trend.
- **Theoretical vs actual variance > 1-2 points = a leak** worth chasing (waste, theft,
  portioning, comps).
- **Distinguish "the data says" from "industry benchmark says"** in every answer. A
  benchmark is context, not a measurement of this business.
- **Name the dominant driver with its share of the delta.** "Sales fell $3.2k; ~70% of
  that is fewer orders (traffic), ~30% lower average check" beats "sales fell."
- **Cross-check every headline number a different way** before stating it. A weekly figure
  must equal the sum of its dailies; a ratio must reconcile against its components.
- **Honesty over completeness.** If the data window doesn't cover the question, say so and
  say what you *can* answer. Never fabricate a number to fill a gap.

## CLASS: lookup

A direct fact ("how many catering orders did tomball have last week", "net sales on
2026-04-15"). Plan:
1. Identify the metric, the store scope (None = both), and the exact date window. Convert
   relative dates ("last week", "April") to ISO ranges; for actuals filter
   `business_date <= date('now','localtime')` (ordersdc carries FUTURE bookings).
2. Prefer the pre-aggregated analytics table (daily_sales_summary, daily_labor_summary,
   item_sales_summary) — one query, hard to misread. Drop to raw only for detail the marts
   don't carry.
3. VERIFY: recompute the headline a second way (e.g. analytics value vs SUM over the raw
   source for the same window). State the number with high confidence only if they agree.
4. If the window has no data (e.g. net sales after 2026-05-09, or any labor_pct today),
   say it is unavailable and why — do not return 0 as if it were a measurement.

## CLASS: comparison

Two things measured against each other (store vs store, week vs week, day-part vs
day-part). Plan:
1. Pin BOTH sides to the same metric and the same anchoring (same weekday / same ISO week /
   same day-part). weekly_rollups and the same_day_lastweek view exist for exactly this.
2. Compute each side, then the delta AND the percentage change.
3. Decompose the delta into its drivers if it is material (see the diagnosis playbooks).
4. VERIFY both sides independently (sum-of-dailies vs weekly figure). Report the comparison
   with the driver of the gap, not just the two numbers.

## CLASS: diagnosis

A "why" question. This is where operator instinct lives. Pick the sub-tree by subject.

### Why are SALES down (or up)?
1. **Traffic vs spend.** Split the move into order/check COUNT (traffic) vs AVERAGE CHECK
   (spend per order) from daily_sales_summary / weekly_rollups. Compute each component's
   SHARE of the total delta. This is the first and most important fork.
2. Drill the mover. If traffic moved: look at day-part distribution (note: day-part signal
   is weak in current data — almost all catering lands in lunch — say so). If average check
   moved: look at category/item MIX from item_sales_summary (qty-based; item dollar columns
   are NULL today, so reason on quantities and item names).
3. **Anchor to same-weekday trailing windows** (same_day_lastweek, weekly_rollups WoW) so a
   calendar artifact isn't mistaken for a trend.
4. Check `anomaly_flags` for the store/date range — a coincident flagged event often *is*
   the story (a one-day collapse, a spike).
5. Name the dominant driver with its share. Note hypotheses you CHECKED AND DISCARDED
   ("avg check was flat, so this is a traffic story, not a pricing story").

### Why is LABOR high?
1. **Rate vs hours.** Labor cost = rate x hours. Individual rates aren't exposed; work from
   total labor_cost and total hours (daily_labor_summary).
2. **Hours: scheduled vs worked.** Compare toastdm.dm_schedule (scheduled) against
   toast.time_entry (worked) for over-scheduling or run-over. Quantify OT exposure
   (ot_hours vs reg_hours) — OT is 1.5x and a common silent driver.
3. **Normalize against sales (SPLH).** A high labor week on a high sales week is not a labor
   problem. BUT: in the current data the sales actuals window (~2026-03..05-09) and the
   labor window (2026-05-11+) DO NOT OVERLAP, so labor_pct/splh are NULL — say plainly that
   the labor-vs-sales ratio is unavailable today and answer on hours/OT alone.
4. Report labor %, SPLH, and OT together (when available) — never labor % alone.

### Why is <metric> anomalous / what happened on <date>?
1. Pull the `anomaly_flags` rows for the store/date; read metric, direction, z-score.
2. Reconstruct the day from daily_sales_summary / daily_labor_summary and compare to the
   same-weekday baseline the flag used.
3. Decompose as in the sales/labor trees above; name the driver.

## CLASS: recommendation

"What should we push / cut / change?" Plan:
1. Build volume x consistency quadrants from item_sales_summary (qty-based): high-volume +
   steady = **plowhorses** (protect), high-volume + erratic = **puzzles** (fix), low-volume
   that still recurs = **stars** if signature / **dogs** if not.
2. CAVEAT explicitly: there is NO plate-cost data, so true MARGIN cannot be computed — these
   are volume/popularity quadrants, not profit quadrants. Say so; do not imply margin
   precision you don't have.
3. Anchor to a recent, complete window (filter out future-dated bookings). Cross-check the
   top movers' quantities against the raw item rows.
4. Recommend with the evidence and the caveat attached. One or two concrete moves, not a
   lecture.
