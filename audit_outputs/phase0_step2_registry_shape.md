# Phase 0 Step 2 — Registry Shape Check-In

Date: 2026-06-06

## What Changed

- Extracted the nine current assistant web registrations from `app/web/assistant_routes.py` into `app/services/assistant_tool_registry.py`.
- Added registry metadata fields for `handler`, `matcher`, `formatter`, and `priority`.
- Kept handler callables bound in `assistant_routes.py` for this step so behavior changes stay tightly scoped.
- Accepted migration debt: by Wave 1, handler callables should move to `app/services/assistant_handlers/<domain>.py`, with the registry importing service metadata instead of route-local functions.
- Flipped approved tool payload execution from eager to lazy:
  - route question to one approved tool id first,
  - check current session availability,
  - execute only that tool's handler,
  - send only that tool payload to CK runtime,
  - pass `routed_tool_id` and `route_path=deterministic` to CK runtime so runtime formats/validates the selected tool instead of re-routing through legacy data matchers.
- Tightened CK runtime validation:
  - no `routed_tool_id` means no approved data-tool answer, even if `tool_data` contains one tool payload,
  - missing/error/stale routed payloads fall through to the existing review path instead of CK refetching data.
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

Rows: 421

- Identity: `412 inventory ids + 11 assistant/runtime built-ins - 2 overlaps = 421 rows`
- Built-ins counted: 9 web registry rows plus `assistant.tool_discovery` and `assistant.session_context` runtime support rows.
- Overlaps: `assistant.tool_discovery`, `labor.store_aggregate`

- `catalog_only_assumed`: 117
- `catalog_only_confirmed`: 223
- `excluded_non_routable`: 70
- `implemented_runtime_branch`: 10
- `registered_partial_review_gated`: 1

Granularity note: the 250 Phase 0.1 audited inventory rows are no longer downgraded to `_assumed`. The 162 inventory ids added after that audit break down as 117 catalog-only assumed rows, 44 excluded rows, and 1 implemented runtime support row.

Important corrections included:

- `orders.update_status` is `write`, `P2-LOW`, `LOW`.
- `toast_live_tables` aliases to `toast.table_activity` and remains catalog-only until alias handling ships.
- `dev.*`, `dash.*`, `resolve_*`, assistant-internal entries, and infra primitives are `excluded_non_routable`.
- `assistant.general_help`, `assistant.tool_discovery`, `assistant.session_context`, `orders.store_summary`, `drivers.store_summary`, `labor.store_aggregate`, `toast.sales_summary`, `toast.table_activity`, `toast.webhook_activity`, and `toast.employee_profiles` are marked `implemented_runtime_branch`.
- `employee.my_profile` is marked `registered_partial_review_gated` because metadata exists but no handler is wired in Phase 0.2.

## Tests Run

- `python -m py_compile app\web\assistant_routes.py app\services\assistant_tool_inventory.py app\services\assistant_tool_registry.py`
- `python -m pytest tests\test_assistant_tool_registry.py tests\test_assistant_routes.py -q`
  - Result before sign-off fixes: `26 passed`
- `python -m pytest tests\test_assistant_tool_registry.py tests\test_assistant_routes.py tests\test_assistant_ck_runtime.py -q`
  - Result after sign-off fixes: `57 passed`

Pytest emitted a Windows temp cleanup `PermissionError` after success; test assertions completed green.

## Stop Point

Phase 0.2 registry/lazy/EXCLUDE shape is ready for Sam review.
No Phase 0.3 classifier, verified-route promotion, or Gemini classifier work has started.
