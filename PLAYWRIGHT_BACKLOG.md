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
