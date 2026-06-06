PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS assistant_question (
  id TEXT PRIMARY KEY,
  question_hash TEXT NOT NULL,
  question_summary_redacted TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'needs_sam_review',
  requested_by_hash TEXT,
  scope_role TEXT,
  scope_store_key TEXT,
  scope_hash TEXT,
  risk_level TEXT NOT NULL DEFAULT 'unknown',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assistant_question_hash
  ON assistant_question(question_hash);
CREATE INDEX IF NOT EXISTS idx_assistant_question_role
  ON assistant_question(scope_role, scope_store_key);
CREATE INDEX IF NOT EXISTS idx_assistant_question_status
  ON assistant_question(status, created_at);

CREATE TABLE IF NOT EXISTS assistant_principal_snapshot (
  id TEXT PRIMARY KEY,
  question_id TEXT NOT NULL,
  principal_hash TEXT,
  role TEXT,
  store_key TEXT,
  permission_level TEXT,
  scope_hash TEXT,
  captured_at TEXT NOT NULL,
  FOREIGN KEY (question_id) REFERENCES assistant_question(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_principal_hash
  ON assistant_principal_snapshot(principal_hash);
CREATE INDEX IF NOT EXISTS idx_principal_question
  ON assistant_principal_snapshot(question_id);
CREATE INDEX IF NOT EXISTS idx_principal_role
  ON assistant_principal_snapshot(role, store_key);

CREATE TABLE IF NOT EXISTS assistant_review_decision (
  id TEXT PRIMARY KEY,
  question_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  reviewer_hash TEXT,
  reason_code TEXT,
  notes_redacted TEXT,
  decided_at TEXT,
  FOREIGN KEY (question_id) REFERENCES assistant_question(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_review_decision_question
  ON assistant_review_decision(question_id);
CREATE INDEX IF NOT EXISTS idx_review_decision_status
  ON assistant_review_decision(status, decided_at);

CREATE TABLE IF NOT EXISTS assistant_policy_rule (
  id TEXT PRIMARY KEY,
  rule_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  role_scope TEXT,
  tool_scope TEXT,
  rule_hash TEXT NOT NULL,
  description_redacted TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policy_rule_role
  ON assistant_policy_rule(role_scope);
CREATE INDEX IF NOT EXISTS idx_policy_rule_status
  ON assistant_policy_rule(status, created_at);
CREATE INDEX IF NOT EXISTS idx_policy_rule_tool
  ON assistant_policy_rule(tool_scope);

CREATE TABLE IF NOT EXISTS assistant_model_audit (
  id TEXT PRIMARY KEY,
  question_id TEXT,
  model_key_hash TEXT,
  prompt_hash TEXT,
  response_hash TEXT,
  status TEXT NOT NULL,
  risk_flags_hash TEXT,
  reviewed_by_hash TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (question_id) REFERENCES assistant_question(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_model_audit_question
  ON assistant_model_audit(question_id);
CREATE INDEX IF NOT EXISTS idx_model_audit_status
  ON assistant_model_audit(status, created_at);

CREATE TABLE IF NOT EXISTS assistant_delivery_attempt (
  id TEXT PRIMARY KEY,
  question_id TEXT,
  tool_name_hash TEXT,
  status TEXT NOT NULL,
  delivery_target_hash TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (question_id) REFERENCES assistant_question(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_delivery_attempt_question
  ON assistant_delivery_attempt(question_id);
CREATE INDEX IF NOT EXISTS idx_delivery_attempt_status
  ON assistant_delivery_attempt(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_delivery_attempt_tool
  ON assistant_delivery_attempt(tool_name_hash);

CREATE TABLE IF NOT EXISTS assistant_tool_catalog_snapshot (
  id TEXT PRIMARY KEY,
  tool_name_hash TEXT NOT NULL,
  tool_label_redacted TEXT,
  role_scope TEXT,
  status TEXT NOT NULL,
  schema_hash TEXT,
  risk_level TEXT,
  captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_catalog_role
  ON assistant_tool_catalog_snapshot(role_scope);
CREATE INDEX IF NOT EXISTS idx_tool_catalog_status
  ON assistant_tool_catalog_snapshot(status, captured_at);
CREATE INDEX IF NOT EXISTS idx_tool_catalog_tool
  ON assistant_tool_catalog_snapshot(tool_name_hash);

CREATE TABLE IF NOT EXISTS assistant_verified_tool_route (
  id TEXT PRIMARY KEY,
  route_key_hash TEXT NOT NULL UNIQUE,
  role_scope TEXT,
  store_scope TEXT,
  tool_id TEXT NOT NULL,
  route_kind TEXT NOT NULL,
  route_args_redacted TEXT,
  status TEXT NOT NULL DEFAULT 'learning',
  verification_count INTEGER NOT NULL DEFAULT 0,
  required_verifications INTEGER NOT NULL DEFAULT 3,
  answer_hash TEXT,
  payload_hash TEXT,
  first_seen_at TEXT NOT NULL,
  last_verified_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_verified_tool_route_status
  ON assistant_verified_tool_route(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_verified_tool_route_tool
  ON assistant_verified_tool_route(tool_id, route_kind);
CREATE INDEX IF NOT EXISTS idx_verified_tool_route_scope
  ON assistant_verified_tool_route(role_scope, store_scope);

CREATE TABLE IF NOT EXISTS assistant_route_event (
  id TEXT PRIMARY KEY,
  route_key_hash TEXT,
  tool_id TEXT,
  route_kind TEXT,
  route_path TEXT NOT NULL,
  event_type TEXT NOT NULL,
  classifier_model TEXT,
  classifier_latency_ms INTEGER,
  classifier_token_cost_usd REAL,
  metadata_redacted TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_route_event_route
  ON assistant_route_event(route_key_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_route_event_path
  ON assistant_route_event(route_path, event_type, created_at);
