# Phase 0 Step 2 — Registry Shape Check-In

Date: 2026-06-06

## What Changed

- Extracted the nine current assistant registrations from `app/web/assistant_routes.py` into `app/services/assistant_tool_registry.py`.
- Added registry metadata fields for `handler`, `matcher`, `formatter`, and `priority`.
- Kept handler callables bound in `assistant_routes.py` for this step so behavior changes stay tightly scoped.
- Flipped approved tool payload execution from eager to lazy:
  - route question to one approved tool id first,
  - check current session availability,
  - execute only that tool's handler,
  - send only that tool payload to CK runtime.
- Added EXCLUDE handling in `assistant_tool_inventory.py`:
  - infra/operator/internal ids are skipped from partner chat catalog generation,
  - route layer hard-skips excluded ids as a second guard,
  - tests assert excluded sentinels cannot appear/routable.
- Fixed the inventory write classifier so underscore verbs are recognized:
  `orders.update_status`, `orders.mark_delivered`, `drivers.reset_passcode`, etc.

## Registry Entries Migrated

- `assistant.general_help`
- `employee.my_profile`
- `orders.store_summary`
- `drivers.store_summary`
- `labor.store_aggregate`
- `toast.sales_summary`
- `toast.table_activity`
- `toast.webhook_activity`
- `toast.employee_profiles`

## Manifest V2

Output: `audit_outputs/cenas_tool_manifest_v2.csv`

Rows: 413

- `catalog_only_assumed`: 340
- `excluded_non_routable`: 70
- `implemented_runtime_branch`: 3

Important corrections included:

- `orders.update_status` is `write`, `P2-LOW`, `LOW`.
- `toast_live_tables` aliases to `toast.table_activity` and remains catalog-only until alias handling ships.
- `dev.*`, `dash.*`, `resolve_*`, assistant-internal entries, and infra primitives are `excluded_non_routable`.
- `assistant.tool_discovery`, `assistant.session_context`, and `labor.store_aggregate` are marked `implemented_runtime_branch`.

## Tests Run

- `python -m py_compile app\web\assistant_routes.py app\services\assistant_tool_inventory.py app\services\assistant_tool_registry.py`
- `python -m pytest tests\test_assistant_tool_registry.py tests\test_assistant_routes.py -q`
  - Result: `26 passed`
- `python -m pytest tests\test_assistant_ck_runtime.py::test_runtime_tool_discovery_reports_partner_catalog tests\test_assistant_ck_runtime.py::test_runtime_does_not_use_toast_payload_when_tool_unavailable -q`
  - Result: `2 passed`

Pytest emitted a Windows temp cleanup `PermissionError` after success; test assertions completed green.

## Stop Point

Phase 0.2 registry/lazy/EXCLUDE shape is ready for Sam review.
No Phase 0.3 classifier, verified-route promotion, or Gemini classifier work has started.
