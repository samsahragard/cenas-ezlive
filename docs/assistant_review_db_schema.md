# Cenas In-App AI Assistant Review DB

Status: created on CK/Mini_IT13 on 2026-06-04; CK-local receiver smoke passed on 2026-06-04
Owner: CK/Mini_IT13
Path: `C:\Users\sam\cena-ai-assistant\assistant_review.sqlite`
Reserved receiver port: `8778`

## Purpose

This database is the CK-local durable review queue for Cenas in-app AI assistant questions that cannot be answered safely yet. It stores redacted question summaries, hashes, role/store scope, review decisions, delivery attempts, and future policy/tool catalog facts.

CK/Mini_IT13 hosts both the assistant runtime and the authoritative review store. Render may keep the web app bubble/proxy surface, but it should not host this assistant's model execution or durable assistant database.

## Current Proof

CK Codex created the database schema first with no receiver, scheduler, token persistence, production deploy, notification, link write, or profile write enabled.

CK proof reported:

- file: `C:\Users\sam\cena-ai-assistant\assistant_review.sqlite`
- size: `126,976`
- mtime: `2026-06-04T16:53:34` local
- tables: 7
- row counts: all 0 at creation
- schema-forbidden scan: 0 hits for secrets, tokens, passcodes, raw GPS, cleartext customer PII, driverdc salt, raw tool payloads, special instructions, gate codes, and cleartext customer/client/display-name fields

CK receiver enablement smoke, CK-local only:

- receiver: running on `127.0.0.1:8778` against `C:\Users\sam\cena-ai-assistant\assistant_review.sqlite`
- token: generated/stored locally on CK with restricted ACL; not pasted in chat
- `/healthz`: `200 OK`, `ok=true`
- test row counts after one blocked-question POST:
  - `assistant_question=1`
  - `assistant_principal_snapshot=1`
  - `assistant_review_decision=1`
  - `assistant_model_audit=1`
  - `assistant_delivery_attempt=1`
  - `assistant_policy_rule=0`
  - `assistant_tool_catalog_snapshot=0`
- test proof: one question hash, role `km`, store `CK-TEST`, status `needs_review`, risk/status proof reported without raw question
- schema-forbidden scan after test: all 0 for secrets, tokens, passcodes, raw GPS, cleartext customer PII, driverdc salt, raw payloads, special instructions, gate codes, and cleartext customer/client/display-name fields
- no scheduler, production deploy, Render env change, notifications, profile/link writes, or data-tool activation

CK receiver contract alignment:

- `GET /healthz` returns `{ok, db, row_counts}`.
- `POST /review/question` accepts the local bearer token.
- Clean app payload fields: `question`, `principal`, `role`; `store_key` is recommended; `status`, `risk_level`, `model_key`, `tool_name`, and `delivery_target` are accepted.
- The CK-local answer runtime is `POST /assistant/answer`; the review-only receiver remains `POST /review/question`.
- Render `AI_ASSISTANT_CK_RUNTIME_URL` may be either the full `/assistant/answer` endpoint or the CK runtime base URL; the app normalizes a base URL to `/assistant/answer`.
- The runtime token belongs in `AI_ASSISTANT_CK_RUNTIME_TOKEN`, never in the URL.
- CK-compatible runtime alias env vars are supported too: `ASSISTANT_RUNTIME_URL`, `ASSISTANT_RUNTIME_TOKEN`, and `ASSISTANT_REVIEW_TIMEOUT_SECONDS`.
- Render-to-CK delivery uses `httpx` and honors `CENA_PROXY` when configured, so private Tailscale runtime URLs use the same route as the existing Cenas private endpoints.
- On Render, the assistant remains hidden/disabled unless `AI_ASSISTANT_ENABLED=1` and the CK runtime URL/token are configured. Render direct model calls require the emergency override `AI_ASSISTANT_ALLOW_RENDER_MODELS=1`, which should remain unset for Sam's CK-local direction.
- `assistant_question.status` normalizes to one of `pending`, `approved`, `rejected`, `needs_review`, `archived`; invalid or missing becomes `needs_review`.
- `risk_level` normalizes to one of `low`, `normal`, `high`, `blocked`; invalid or missing becomes `blocked`.
- Success response shape: `{ok, question_id, question_hash, principal_hash, role, store_key, risk_level, status, delivery_status}` plus `ck_question_id` for app compatibility.
- Per POST, the current CK receiver writes one row each to `assistant_question`, `assistant_principal_snapshot`, `assistant_review_decision`, `assistant_model_audit`, and `assistant_delivery_attempt`.
- It does not write `assistant_policy_rule` or `assistant_tool_catalog_snapshot`.

## Guardrails

- Store hashes and redacted summaries, not raw prompts or raw tool payloads.
- Do not store secrets, API keys, tokens, password hashes, passcodes, PINs, raw GPS, cleartext customer PII, driverdc HMAC salt, or raw database rows.
- First production version is read-only.
- The model never decides permissions. Permissions come from Cenas role/store/session checks and approved assistant policy rules.
- If role, store, permission, tool, or data freshness is unclear, fail closed and save the question for review.
- Assistant app-data reads must use the main app models/permission layer. Do not import driverdc export modules and do not read `DRIVERDC_HMAC_SALT`.
- Customer analytics, when later approved, must use HMAC pseudonyms and k-anon thresholds.
- Economics answers are aggregates, not raw peer rows, unless Sam explicitly approves a narrower rule.

## Tables

### assistant_question

Columns:

- `id`
- `question_hash`
- `question_summary_redacted`
- `status`
- `requested_by_hash`
- `scope_role`
- `scope_store_key`
- `scope_hash`
- `risk_level`
- `created_at`
- `updated_at`

Indexes:

- `idx_assistant_question_hash`
- `idx_assistant_question_role`
- `idx_assistant_question_status`

### assistant_principal_snapshot

Columns:

- `id`
- `question_id`
- `principal_hash`
- `role`
- `store_key`
- `permission_level`
- `scope_hash`
- `captured_at`

Foreign key:

- `question_id` -> `assistant_question.id` with cascade delete

Indexes:

- `idx_principal_hash`
- `idx_principal_question`
- `idx_principal_role`

### assistant_review_decision

Columns:

- `id`
- `question_id`
- `decision`
- `status`
- `reviewer_hash`
- `reason_code`
- `notes_redacted`
- `decided_at`

Foreign key:

- `question_id` -> `assistant_question.id` with cascade delete

Indexes:

- `idx_review_decision_question`
- `idx_review_decision_status`

### assistant_policy_rule

Columns:

- `id`
- `rule_key`
- `status`
- `role_scope`
- `tool_scope`
- `rule_hash`
- `description_redacted`
- `created_at`

Indexes:

- `idx_policy_rule_role`
- `idx_policy_rule_status`
- `idx_policy_rule_tool`

### assistant_model_audit

Columns:

- `id`
- `question_id`
- `model_key_hash`
- `prompt_hash`
- `response_hash`
- `status`
- `risk_flags_hash`
- `reviewed_by_hash`
- `created_at`

Foreign key:

- `question_id` -> `assistant_question.id` with cascade delete

Indexes:

- `idx_model_audit_question`
- `idx_model_audit_status`

### assistant_delivery_attempt

Columns:

- `id`
- `question_id`
- `tool_name_hash`
- `status`
- `delivery_target_hash`
- `attempt_count`
- `last_error_code`
- `created_at`
- `updated_at`

Foreign key:

- `question_id` -> `assistant_question.id` with cascade delete

Indexes:

- `idx_delivery_attempt_question`
- `idx_delivery_attempt_status`
- `idx_delivery_attempt_tool`

### assistant_tool_catalog_snapshot

Columns:

- `id`
- `tool_name_hash`
- `tool_label_redacted`
- `role_scope`
- `status`
- `schema_hash`
- `risk_level`
- `captured_at`

Indexes:

- `idx_tool_catalog_role`
- `idx_tool_catalog_status`
- `idx_tool_catalog_tool`

## Receiver Contract

`POST /review/question`

Required behavior:

- Fail closed when `ASSISTANT_REVIEW_TOKEN` is missing or wrong.
- Reject payloads larger than 256 KB.
- Store redacted question summaries and hashes.
- Never echo sensitive values in errors or chat proof.
- Return only hash/id/scope/status metadata on success; never return raw question text.

The local receiver draft in `scripts/assistant_review_ck_receiver.py` has been aligned to this CK schema and CK receiver contract, then validated against a disposable SQLite database.
