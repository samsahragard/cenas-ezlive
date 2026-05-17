# APP_STATUS.md

> Live operational status for the Cenas Kitchen app.
> Read this at session start. Keep it current — if something changes, update it here.

---

## Dev Agent Roles + Pipeline

Canonical source: `/partner/developer/app/session-start` + `/partner/developer/app/methodology-rules`

### The Team

**aick** — Backend + integration. Primary author of server-side code: Flask routes, SQLAlchemy models, services, DB probes, ingest pipelines. Lives on AiCk (Mini_IT12, always-on Windows desktop). Working dir: `C:/Users/sam/Desktop/cenas-kitchen-claude`. **Only Claude with wincredman GitHub credentials** — push orchestrator for the whole team. ck and samai commit locally, then ping aick in dev chat (`aick — push <SHA>`); aick verifies the diff and pushes to origin/main (which triggers Render auto-deploy). Also runs and restarts the Cena gateway on AiCk (port 8765). Default: silent in dev chat unless addressed by name or operationally concerned.

**ck** — Frontend, UI, templates. Templates, sidebar, navigation, CSS, client-side JS, doc pages. Lives on CK (Mini_IT13, secondary mini PC). SSH/Tailscale access to AiCk for shared repo editing. Authors locally, commits on AiCk's working tree, asks aick to push.

**samai** — Spec + review. Authors specs (samai-spec lane) and gates every behavior-touching commit via the three-gate review rubric.

### samai's Three-Gate Review (every behavior-touching commit must clear all three)

- **Gate 1 — Local pytest:** full suite passes on the working tree.
- **Gate 2 — Code-contract:** change matches the commit message claims; semantics + safety reasoned through.
- **Gate 3 — Build-specific deploy probe:** confirms *that commit* is live — not just "service is up" but something unique to the new commit is verified in production.

**samai PASS = shipped.** Merged ≠ shipped. samai's three-gate clear is what makes it done.

### samai's 6 Standing Rules (codified in methodology_rules.html)

1. Audience-eligibility-before-mutation
2. Verify-against-prod-not-local
3. Doc-registration-checks-both-route-AND-sidebar
4. Amendments-against-git-show-HEAD
5–6. See methodology_rules.html for rules 5 and 6.

### The Flow

```
aick/ck author + commit → aick pushes to origin/main → Render auto-deploy
→ samai three-gates → samai PASS = done
```

Cena's recent tool additions (`post_to_dev_chat` / `read_dev_chat`) inherit the same pipeline: samai still three-gates the code commits even though Cena now originates instructions.

---

## Human-Style Testing — Required Before Calling Anything Done

**Don't just check logs or HTTP status codes.** Actually log into the app as a real user would and click through the affected surface. If something looks broken to a person using it, it's broken — regardless of what the probe says.

When delegating test coverage, know the current live sessions:
- **aick** is logged in as a **driver** on his PC (AiCk / Mini_IT12).
- **ck** is logged in with **partner-level access** on his PC (Mini_IT13).

Use this when assigning test tasks — aick can verify driver-facing flows, ck can verify partner/admin-facing flows without either of them having to log in fresh.

---

## Infrastructure Status

| Surface | Status | Notes |
|---|---|---|
| Live app | ✅ Up | https://app.cenaskitchen.com |
| Render auto-deploy | ✅ Active | Triggered on push to origin/main |
| Cena gateway (port 8765) | ✅ Live | PID 10484, non-elevated, aick-restartable |
| `/sam/chat` Cena surface | ✅ Live | Gated by `SAM_CHAT_USER_ID` |
| `CenaActionLog` + `/sam/cena-audit/` | ✅ Live | Every tool call logged |
| `post_to_dev_chat` tool | ✅ Live | Must-execute nudge wired |
| `read_dev_chat` tool (start-point filter) | ✅ Live | Cena reads from session-start forward only |
| Auto-load of CENA.md / CENA_CHARTER.md / APP_STATUS.md | ✅ Live | Appended to system context at session start |
| `session_id` + `message_id` threading | ✅ Live | Commit `aa6074b` |
| **`CenaJournal` table** | ⏳ Not built | Coming with aick Part 4 |
| **`cena_reference/` doc set** | ⏳ Not built | Built as Sam feeds context |
| **`cena_gateway.py` repo-tracked** | ⚠️ Not done | samai flagged — file not version-controlled, collision risk |
| **`cena_setup_task.ps1` repo-tracked** | ⚠️ Not done | Same risk — aick/ck had a silent-edit collision 2026-05-16 |
| **Agent heartbeat / online-signal** | ⏳ Not built | No clean signal today for "is samai/aick/cena online." samai flagged 2026-05-17 (#1931). Sam-surface as proposal post-Part-4. |
| **samai bootstrap script on CK profile** | ⏳ Not built | Manual cold-start every Claude Code session (git fetch, arm monitor, post restart). Same friction class aick solved with scheduled tasks. samai-spec lane ticket when bandwidth allows. |
| **samai-side Render diagnostic read** | ⏳ Not built | pipeline_minutes_exhausted on 2026-05-17 blocked samai's gate-3 probe. Second incident-time opacity hit. Pairs with gateway-in-repo close. |

---

## Open Threads

- Track `cena_gateway.py` + `cena_setup_task.ps1` in the repo (samai flagged, collision risk). Partial close 2026-05-17: 3 bootstrap scripts now repo-tracked (f6dede4 + 28b3e5b + 84ae397); gateway runtime file itself still not.
- Verify Claude's May-12 sidebar redesign ship status — did `sidebar.css` / `sidebar.js` / `sidebar.html` partial land?
- 24-hour rolling review routine — propose post-Part-4.
- **Agent heartbeat mechanism** — surface to Sam post-Part-4 (samai #1931).
- **samai bootstrap automation on CK profile** — samai-spec lane ticket (samai #1931).
- **samai-side Render diagnostic access** — pairs with gateway-in-repo (samai #1931).
- **SSH-to-aick + gh CLI for samai** (cena #1785 path (a)) — structural upgrade to enable canonical gate 1+2 instead of evidence-reviewer mode. Queued for Sam-surfacing.

---

*Last updated: 2026-05-17*
