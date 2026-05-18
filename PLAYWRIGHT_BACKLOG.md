# Playwright Test Backlog

Per Sam #2547: Playwright tests for milestone-tier commits are
batched across multiple milestones and run together in one session
when Sam says it's time. Cena charter amendment 7 (Playwright
required, Sam-only waiver) STILL stands — this backlog is the
batching mechanism, not a waiver.

When Sam calls the batch session: write + run + samai-gate every
listed test in one cycle. Drain entries when shipped.

## Pending tests

### 7d55a08 — Drivers Phase A (2026-05-17)
Spec: /partner/developer/app/spec-drivers-redesign §8

- **Test A — Tab interaction**: GET /dos/drivers → Active tab
  highlighted gold + Inactive not. Click Inactive → URL becomes
  `?status=inactive` + Inactive now highlighted + Active not.
- **Test B — Mobile hamburger render+open**: viewport 375x667 →
  hamburger visible in topbar (not position:fixed) computed
  48x48px. Click → sidebar drawer opens.
- **Test C — Desktop hamburger hidden**: viewport 1440x900 →
  hamburger has `display: none`.

samai gate-3 was annotated "Playwright deferred to batch session
per Sam direction" per cena #2548.

### TBD — Samples approval workflow (2026-05-17)
Spec: /partner/developer/app/spec-samples-approval-workflow §12
File written: tests/test_sample_approval_playwright.py

- **Test A — Approve persists**: Sam-session loads /partner/developer/samples,
  clicks Approve on drivers-redesign-v2 card, types notes, clicks Save.
  Status pill flips to APPROVED. Notes survive page reload.
- **Test B — Attach chip**: Sam-session uploads a screenshot via the attach
  zone (file input set_input_files), clicks Save. Chip with filename appears.
  Persists after reload.
- **Test C — Non-sam read-only**: gm-tier session loads page. approve/reject/
  save buttons + notes textarea NOT in DOM. Read-only status pill + chips
  remain visible.

ck #2549 Item 3 lane. Tests written but deferred per Sam #2547 batch model.

### 2f9e26a — Samples approval-events endpoint (2026-05-18)
Spec: scope ck #2736 + cena #2738/#2741 (route shape + cursor monotonicity + partner gate)

- **Test A — Cursor monotonicity across polls**: Sam-session GETs
  /partner/developer/samples/approval-events?since=2026-01-01,
  captures response.now. Sam-session approves a new sample.
  GET ?since=<captured-now> → returns ONLY the new approval, sorted
  by marked_at desc.
- **Test B — Reject-with-image vs reject-text-only path**: Sam rejects
  one sample with text-only notes, rejects another with an image
  attachment. GET endpoint → both events present; the attachment-bearing
  event has attachments[].id + filename + url populated, the text-only
  event has attachments: [].
- **Test C — Latest-state-only per slug (single-row trade-off)**: Sam
  approves a sample, then rejects it within one polling window.
  GET → single event with status='rejected' (NOT two events). Matches
  scope #2736 §PERSISTENCE trade-off documentation.
- **Test D — Partner-auth gate**: unauthenticated GET → 302/401, not
  200. Authenticated GET → 200 + valid payload.

aick #2745 lane. Tests deferred per Sam #2547 batch model.

---

---

## How to append

When a new milestone-tier commit lands without its Playwright tests
run, add an entry here with format:

```
### <SHA> — <description> (<date>)
Spec: <path-to-spec-or-file>

- **Test N — <name>**: <what to verify>
- ...
```

When the batch session runs, transcribe each entry into a real
test file under `tests/playwright/`, drain the entry, and let
samai canonical-three-gate the batch.
