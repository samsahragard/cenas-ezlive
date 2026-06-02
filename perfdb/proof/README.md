# Phase 5.1 proof payloads (samai option-ii grep target)

The 6 files are the EXACT sanitized JSON that GET /employee/my-performance returns
for each pilot employee, captured from the local real-route harness (no prod push).
They are the agreed option-(ii) grep target for samai's independent leak audit.

Sanitization already verified (CK-subagent-B, independent): 0 sales / 0 eligible_sales /
0 GUID / 0 attribution / 0 hourly_rate across all 6; only the tip_percent RATIO leaves.

PRIVATE audit artifacts (real own-pay own-view). Strip perfdb/proof/ before any merge to
main -- these never ship to the deployed app.
