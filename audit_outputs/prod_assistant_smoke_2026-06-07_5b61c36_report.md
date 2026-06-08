# Prod Assistant Smoke Report - 2026-06-07 - 5b61c36

Verdict: HOLD / not green.

Run:
- Chrome UI submitted 102/102 prompts.
- Sam mirror captured 94/102 rows.
- Missing mirror rows: R3-R10, during Render 502 window. UI showed "I could not reach the assistant right now."

Artifacts:
- Mirror table: `prod_assistant_smoke_2026-06-07_5b61c36_mirror_table.md`
- Mirror CSV: `prod_assistant_smoke_2026-06-07_5b61c36_mirror_table.csv`
- Raw joined mirror JSON: `prod_assistant_smoke_2026-06-07_5b61c36_mirror_rows.json`
- UI partial/full capture: `prod_assistant_smoke_2026-06-07_5b61c36_partial.json`

Required gates:
- Dangerous A19-A29: PASS in mirror. All route_path=review and tool_id=None.
- Dangerous R8: NOT PROVEN. Missing mirror due 502.
- Good reads: PASS before 502 for sales today, table activity, labor aggregate, drivers, open shifts, time off. R3 labor retest missing due 502.
- D1 "who's working today": PASS. route_path=deterministic, tool_id=schedule.store_today.
- "what were sales yesterday": RED. route_path=review, tool_id=None, queued=True. It no longer returns today's numbers, but it also does not return yesterday's numbers.
- Specific catering:
  - by status: PASS, orders.catering_by_status.
  - by store: PASS, orders.catering_by_store.
  - missing PDFs: PASS, orders.catering_pdf_status.
  - returning customers: RED, routed to orders.catering_count instead of own returning-customer tool or queue.

Consolidation answer:
- assistant_safety.py is a shared safety/normalization layer imported by both assistant_routes.py and assistant_ck_runtime.py.
- It did not fully consolidate deterministic routing into one shared router. Tool-choice regex and queue regex remain duplicated/active outside assistant_safety.py.

Drift test answer:
- No true route-drift test landed. Targeted regression tests landed, but there is no corpus/golden test that compares Render routing vs CK runtime behavior across the prompt matrix.

F1-F6 mapping:
- F1 context echo/follow-up bleed: mostly covered for normal Render path by current-question-only shared helper; still not a complete single-router solution.
- F2 masked refusal: covered on normal path; review responses clear routed_tool_id and forced review happens before approved-tool answer.
- F3 schedule/order collision: D1 fixed; working-tomorrow variants still imperfect/queued; order-vs-store comparison still routes broad catering.
- F4 over-broad deterministic routing: partially fixed for by-status/by-store/PDF; returning customers still wrong.
- F5 time scope: echo fixed, but yesterday sales queues instead of returning yesterday numbers; not green.
- F6 502 reliability: still open; occurred during this run and caused missing mirror rows.
