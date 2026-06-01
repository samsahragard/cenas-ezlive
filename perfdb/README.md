# cena-perfdb -- CK-local employee performance database (Sam #2896/#2901)

The **source of truth lives on CK / Mini_IT13** at
`C:\Users\sam\cena-perfdb\perf.sqlite` -- NOT in this repo, NOT on Render.
These files are the schema + build + refresh **code**, versioned here for audit.
The app reads a sanitized last-good snapshot that CK pushes token-gated; the app
never reads CK live and never calls Toast per employee-page request.

## Phase 0 (DONE) -- schema + empty DB
- `schema_v1.sql`   -- SQLite schema, version 1.
- `build_perfdb.py` -- creates/versions `perf.sqlite` (idempotent) + prints proof.
- **`schema_v1.sql` sha256 = `3d14ce2ea0f2b2090e1d3ff1982b6cc8dd8feeac6c0b1a6a56a5af8d42dfc6c0`**
  (matches the Phase 0 proof posted to the hub).

### Sales isolation (sanitize-by-construction)
Restaurant sales dollars live ONLY in table `perf_internal`. The sanitized push
and the employee payload are built from `perf_period` / `time_entry` ONLY, so
sales has **no structural path** into any employee-facing payload -- stronger than
a runtime filter. (samai guardrail #1.)

## Phase 2 prep -- `toast_perf_refresh.py`
- `dry_run()`  -- COMPLETE. Normalizes a sample Toast labor payload for Yadira
  across today/week/month/last30, writes a throwaway temp DB, and PROVES every
  employee payload greps clean for `sales|revenue|...` and that `perf_period` has
  zero sales columns.
- `real_run()` -- STUB, pending (1) Toast creds on CK, (2) aick Phase 1 field map,
  so the live field/endpoint wiring matches what is actually deployed.

## Audit
`sha256sum perfdb/schema_v1.sql` should equal the hash above. `build_perfdb.py`
is inert (imported by nothing in the app); this branch is not `main`, so it does
not trigger a Render deploy.
