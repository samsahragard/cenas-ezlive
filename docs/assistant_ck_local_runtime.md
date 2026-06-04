# CK-Local Personalized Assistant Runtime

## Current Direction

The personalized in-app assistant runtime belongs on CK/Mini_IT13. Render should not host model execution for this assistant, and Render should not become the durable assistant database location.

The web app can keep the small assistant bubble and route surface, but production answer execution should proxy to CK only when `AI_ASSISTANT_CK_RUNTIME_URL` and `AI_ASSISTANT_CK_RUNTIME_TOKEN` are configured. Without that CK runtime configuration, the assistant remains hidden/disabled on Render even if the code is deployed.

## CK Folder Layout

Target root:

```text
C:\Users\sam\cena-ai-assistant\
  assistant_review.sqlite
  logs\
  secrets\
    assistant_runtime_token.txt
```

Repo-managed scripts to copy or run from the app repo:

```text
scripts\assistant_ck_runtime.py
scripts\assistant_ck_runtime_run.ps1
scripts\assistant_review_ck_receiver.py
scripts\assistant_review_schema.sql
```

## Services

### Runtime Service

- Default port: `8782`
- Endpoint: `POST /assistant/answer`
- Health: `GET /healthz`
- Token env: `ASSISTANT_RUNTIME_TOKEN`
- DB env: `ASSISTANT_REVIEW_DB`
- Bind env: `ASSISTANT_RUNTIME_HOSTS`

Recommended CK-private binds:

```text
127.0.0.1,100.73.38.82
```

Do not bind to `0.0.0.0`.

### Review Receiver

The prior receiver remains available on port `8778` for review-only ingestion:

```text
POST /review/question
GET /healthz
```

The runtime uses the same `assistant_review.sqlite` schema and writer so blocked/unanswered questions still land in the approved 7-table hash/redacted database.

## Render App Guard

On Render, `/assistant/context` reports the assistant disabled unless:

- `AI_ASSISTANT_ENABLED=1`, and
- `AI_ASSISTANT_CK_RUNTIME_URL` or `ASSISTANT_RUNTIME_URL` is set, and
- `AI_ASSISTANT_CK_RUNTIME_TOKEN` or `ASSISTANT_RUNTIME_TOKEN` is set.

Render direct model calls stay blocked unless `AI_ASSISTANT_ALLOW_RENDER_MODELS=1` is explicitly set. That override should remain unset for Sam's CK-local direction.

## Runtime Behavior

The CK runtime:

- receives the current role, store scope, permission list, and question,
- applies the permission and question-safety gate before model calls,
- answers only general app-help/policy questions in the first version,
- saves operational, sensitive, missing-tool, or model-unavailable questions to CK SQLite,
- calls Sonnet first and Gemini as fallback,
- reads model keys from CK-local env vars or CK-local key files,
- does not send notifications,
- does not write profiles, links, orders, driver data, schedules, or production app rows,
- does not activate data tools until Sam approves each tool policy.

## CK Key Files

The runtime checks env vars first, then local files:

```text
ANTHROPIC_API_KEY
ANTHROPIC_API_KEY_FILE
C:\Users\sam\cena-secrets\anthropic_api_key.txt

GEMINI_API_KEY
GEMINI_API_KEY_FILE
C:\Users\sam\cena-secrets\gemini_api_key.txt
C:\Users\sam\cena\.secrets\gemini_api_key.txt
C:\Users\sam\cena-secrets\google_api_key.txt
```

Never paste key values into chat or docs.

## Local Run Command

From PowerShell on CK:

```powershell
.\scripts\assistant_ck_runtime_run.ps1 `
  -RepoRoot "C:\Users\sam\Desktop\cenas-ezlive-tracking-live-work" `
  -ProjectRoot "C:\Users\sam\cena-ai-assistant" `
  -Hosts "127.0.0.1,100.73.38.82" `
  -Port 8782
```

The token file must already exist at:

```text
C:\Users\sam\cena-ai-assistant\secrets\assistant_runtime_token.txt
```

## Verification

Run locally before enabling the web app:

```powershell
python -m py_compile scripts\assistant_ck_runtime.py scripts\assistant_review_ck_receiver.py
python -m pytest tests\test_assistant_routes.py tests\test_assistant_review_receiver.py tests\test_assistant_ck_runtime.py
```

CK proof should report only:

- bind addresses,
- `/healthz` status,
- row counts,
- provider key presence booleans,
- token file exists/ACL status,
- no raw question,
- no token/key value.
