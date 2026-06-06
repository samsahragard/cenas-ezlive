# Phase 0 Step 3 — Routing/Classifier Check-In

Date: 2026-06-06

## Scope Completed

- Added deterministic matcher scan support in registry priority order.
- Added Gemini route-classifier fallback plumbing behind `AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED`.
  - Default is OFF, including prod/Render unless explicitly enabled.
  - Classifier prompt is strict JSON only: `{"tool_id":"<id or NONE>"}`.
  - Classifier choices are canonicalized through the alias table and validated against the available implemented tool allowlist.
  - EXCLUDE ids and unavailable/catalog-only ids cannot route.
- Added the Phase 0.5 alias table now, including `toast_live_tables -> toast.table_activity` plus confirmed duplicate mappings.
- Added `route_path`, `routed_tool_id`, `final_tool_id`, classifier metadata, route latency, and classifier token-cost field to assistant mirror payload metadata.
- Added CK route-event telemetry table for per-question route path and classifier metadata.
- Changed route verification to candidate/learning evidence only during answer handling.
  - No silent auto-promotion during normal answers.
  - Manual verify/flag/reject action is available at `/review/route-action`.
  - Nightly verifier script: `scripts/assistant_route_auto_verify.py`.
  - Nightly auto-verify promotes only aged learning routes that already met `required_verifications`.

## Guardrails

- Runtime requires explicit `routed_tool_id` for approved data-tool answers.
- Runtime validates/formats the routed payload; it does not refetch missing/error/stale tool payloads.
- Classifier remains disabled unless `AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED=1`.
- Main/live branch remains untouched.

## Tests Run

- `python -m py_compile app\web\assistant_routes.py app\services\assistant_tool_registry.py scripts\assistant_ck_runtime.py scripts\assistant_review_ck_receiver.py scripts\assistant_route_auto_verify.py`
- `python -m pytest tests\test_assistant_tool_registry.py tests\test_assistant_routes.py tests\test_assistant_ck_runtime.py tests\test_assistant_review_receiver.py -q`
  - Result: `67 passed`

Pytest still emits the known Windows temp cleanup `PermissionError` after success; assertions pass.

## Stop Point

Phase 0.3 is ready for Sam review on the review branch. Classifier flag is OFF by default and has not been enabled in production.
