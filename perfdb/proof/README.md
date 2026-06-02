# Phase 5.1 proof payloads (samai option-ii grep target)

The 6 files are the EXACT sanitized JSON that GET /employee/my-performance returns
for each pilot employee, captured from the local real-route harness (no prod push).
They are the agreed option-(ii) grep target for samai's independent leak audit.

Sanitization already verified (CK-subagent-B, independent): 0 sales / 0 eligible_sales /
0 GUID / 0 attribution / 0 hourly_rate across all 6; only the tip_percent RATIO leaves.

PRIVATE audit artifacts (real own-pay own-view). Strip perfdb/proof/ before any merge to
main -- these never ship to the deployed app.

## UI leak-audit artifacts (samai #3024 -- same private channel)
- phase5_rendered_yadira.html: the rendered DOM of Yadira's dashboard (richest leak surface
  -- the only non-gated leaderboard, n=4). samai's UI leak-grep target. CK-subagent-B + the
  harness already grep it = 0 sales/eligible_sales/GUID/internal; only the tip% ratio renders.
- _phase5_yadira_rank.png / _yadira_top.png / _damaris_rank.png / _phase5_desktop.png:
  the ranking surface (Yadira rich rank + Damaris BOH-gated). Visual review.
All private audit artifacts (real own-pay own-view); strip perfdb/proof/ before any main merge.
