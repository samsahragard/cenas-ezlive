# APP_STATUS.md

> Live operational status for the Cenas Kitchen app.
> Read this at session start. Keep it current — if something changes, update it here.

---

## Dev Agent Roles + Pipeline

Canonical source: `/partner/developer/app/session-start` + `/partner/developer/app/methodology-rules`

### The Team

**aick** — Backend + integration. Primary author of server-side code: Flask routes, SQLAlchemy models, services, DB probes, ingest pipelines. Lives on AiCk (Mini_IT12, always-on Windows desktop). Working dir: `C:/Users/sam/Desktop/cenas-kitchen-claude`. Has direct push access to origin/main. Also runs and restarts the Cena gateway on AiCk (port 8765). Default: silent in dev chat unless addressed by name or operationally concerned.

**ck** — Frontend, UI, templates. Templates, sidebar, navigation, CSS, client-side JS, doc pages. Lives on CK (Mini_IT13, secondary mini PC). **Has own SSH deploy-key push access since 2026-05-20** — ck pushes direct, does not route through aick. Fetch + check origin divergence before every push per staged-diff discipline.

**samai** — Spec author. Writes detailed specs in the samai-spec lane when present. **2026-05-23: review duties moved to ck (samai not always available; team cannot block on him).** Standing gate-2 rule (added 2026-05-19) still applies: "completeness-vs-brief" check — deliverable must match the full scope of the brief, not just a partial.

**ck** (now also) — Review owner. Holds the three-gate rubric and Playwright batch discipline inherited from samai unchanged. Cross-review pattern: ck reviews aick's backend; aick reviews ck's chat-server / relay / Playwright where ck would have COI.

**dck** — Design lead. Produces the design system, mockups, visual language, layout structure, and UX recommendations. Lives on the Design machine. All dck work is reviewed and green-lit by Cena before ck implements. Mockups and pattern references live at `/partner/developer/samples`.

### Repo

- SSH: `git@github.com:samsahragard/cenas-ezlive.git`
- HTTPS: `https://github.com/samsahragard/cenas-ezlive.git`
- Branch: `main` (push triggers Render auto-deploy)

### Three-Gate Review — held by ck since 2026-05-23 (every behavior-touching commit must clear all three)

- **Gate 1 — Local pytest:** full suite passes on the working tree.
- **Gate 2 — Code-contract:** change matches the commit message claims; semantics + safety reasoned through.
- **Gate 3 — Build-specific deploy probe:** confirms *that commit* is live — not just "service is up" but something unique to the new commit is verified in production.

**samai PASS = shipped.** Merged ≠ shipped. samai's three-gate clear is what makes it done.

### Playwright Testing — Batch Model (Sam direction 2026-05-18; batch ownership moved to ck on 2026-05-23)

Playwright tests are required for every milestone. However Sam's direction is to batch them: tests accumulate in PLAYWRIGHT_BACKLOG.md as milestones ship, and the team runs the full batch together when Sam calls a Playwright session. Individual milestone commits land with "Playwright deferred to batch session per Sam direction" annotation. **Only Sam can waive a test from the backlog entirely.**

### samai's 6 Standing Rules (codified in methodology_rules.html)

1. Audience-eligibility-before-mutation
2. Verify-against-prod-not-local
3. Doc-registration-checks-both-route-AND-sidebar
4. Amendments-against-git-show-HEAD
5–6. See methodology_rules.html for rules 5 and 6.

### The Flow

```
aick/ck author + commit → push direct to origin/main (each has own credentials) → Render auto-deploy
→ ck three-gates (samai backstop when available) → PASS = done
```

---

## Charter File Locations

All charters are repo-tracked at the repo root. Canonical addresses:

| Charter | File Path | Status |
|---|---|---|
| CENA_CHARTER.md | `C:/Users/sam/Desktop/cenas-kitchen-claude/CENA_CHARTER.md` | ✅ Live, auto-loaded |
| CENA.md | `C:/Users/sam/Desktop/cenas-kitchen-claude/CENA.md` | ✅ Live, auto-loaded |
| APP_STATUS.md | `C:/Users/sam/Desktop/cenas-kitchen-claude/APP_STATUS.md` | ✅ Live, auto-loaded |
| plan.md | `C:/Users/sam/Desktop/cenas-kitchen-claude/plan.md` | ✅ Live, auto-loaded |
| tool.md | `C:/Users/sam/Desktop/cenas-kitchen-claude/tool.md` | ✅ Live, auto-loaded |
| AICK_CHARTER.md | `C:/Users/sam/Desktop/cenas-kitchen-claude/AICK_CHARTER.md` | ✅ Live (8c88552) |
| CK_CHARTER.md | `C:/Users/sam/Desktop/cenas-kitchen-claude/CK_CHARTER.md` | ✅ Live (8ab61cd) |
| SAMAI_CHARTER.md | `C:/Users/sam/Desktop/cenas-kitchen-claude/SAMAI_CHARTER.md` | ✅ Live (repo root) |
| DCK_CHARTER.md | `C:/Users/sam/Desktop/cenas-kitchen-claude/DCK_CHARTER.md` | ✅ Live, repo-tracked |
| PLAYWRIGHT_BACKLOG.md | `C:/Users/sam/Desktop/cenas-kitchen-claude/PLAYWRIGHT_BACKLOG.md` | ✅ Live (f412617) |

---

## Human-Style Testing — Required Before Calling Anything Done

**Don't just check logs or HTTP status codes.** Actually log into the app as a real user would and click through the affected surface. If something looks broken to a person using it, it's broken — regardless of what the probe says.

When delegating test coverage, know the current live sessions:
- **aick** is logged in as a **driver** on his PC (AiCk / Mini_IT12).
- **ck** is logged in with **partner-level access** on his PC (Mini_IT13).

---

## Secrets Policy

All credentials live in **1Password** and **Render env vars** only. No credential files on machines (Track 4 migration completed 2026-05-18). No credentials in chat surfaces, repo, or any .txt/.secrets files. Sam's personal credentials are never requested, stored, or relayed through any agent.

---

## Infrastructure Status

| Surface | Status | Notes |
|---|---|---|
| Live app | ✅ Up | https://app.cenaskitchen.com |
| Render auto-deploy | ✅ Active | Triggered on push to origin/main |
| Cena gateway (port 8765) | ✅ Live | Non-elevated, aick-restartable |
| `/sam/chat` Cena surface | ✅ Live | Gated by `SAM_CHAT_USER_ID` |
| `CenaActionLog` + `/sam/cena-audit/` | ✅ Live | Every tool call logged |
| `post_to_dev_chat` tool | ✅ Live | Must-execute nudge wired |
| `read_dev_chat` tool (start-point filter) | ✅ Live | Cena reads from session-start forward only |
| Auto-load of CENA.md / CENA_CHARTER.md / APP_STATUS.md / plan.md | ✅ Live | All four appended to system context at session start |
| `session_id` + `message_id` threading | ✅ Live | Commit `aa6074b` |
| Sam Chat participant strip (Sam + Cena only) | ✅ Live | dck chip + summon daemon removed per Sam direction 2026-05-18 |
| Sam Chat message queue | ✅ Live | 8ca4199 — input stays open during streaming, queued msgs auto-drain |
| Samples page | ✅ Live | `/partner/developer/samples` — dck's canonical mockup + pattern reference home |
| Samples page approval workflow | ⏳ In progress | dck adding checkmark/X + text + image attachment to all mockups; ck wiring backend |
| plan.md on Samples page | ⏳ In progress | ck building formatted plan.md view on Samples page |
| PLAYWRIGHT_BACKLOG.md | ✅ Live | f412617 — Drivers Phase A tests queued |
| All 5 agent charters repo-tracked | ✅ Live | See charter file locations table above |
| Track 1 (ezCater IMAP → Render) | ✅ Closed | Cancelled — ezCater already runs through app API |
| Track 2 (Telegram → Render) | ✅ Closed | Test fire confirmed, OpenClaw duplicates disabled, samai-gated |
| Track 3 (WhatsApp removal) | ✅ Closed | Code removed, DB tables dropped, process killed, token deleted, samai-gated |
| Track 4 (Secrets migration) | ✅ Closed | 1Password CLI installed on AiCk, credentials vaulted |
| Track 5 (OpenClaw uninstall) | ⏳ In progress | Waiting on Track 4 full confirm before final uninstall on both machines |
| Track 7 (Caching) | ✅ Closed | samai-gated |
| Track 8 (dck full activation) | ✅ Closed | dck reads/writes Sam Chat; summon daemon later removed per Sam direction |
| Track 9 (dck design audit) | ✅ Closed | Design baseline doc + 10 recommendations shipped, samai-gated |
| Track 10 (512Mi OOM) | ✅ Closed | Sam confirmed fixed |
| Driver login redirect loop fix | ✅ Live | 43b4699 |
| Drivers page Phase A | ✅ Live | 7d55a08 — Active/Inactive tabs, hamburger fix, STATUS inline. samai PASS pending visual confirm from Sam. Playwright deferred to batch. |
| Cena gateway wake-on-post (Tailscale) | ⚠️ Degraded | Tunnel dropped on redeploy; 30s watcher fallback active. Permanent fix deferred. |
| Right-side menu bar mockup (plan.md-based) | ⏳ Queued | dck to produce after plan.md confirmed final |
| **`CenaJournal` table** | ⏳ Not built | Coming with aick Part 4 |
| **`cena_reference/` doc set** | ⏳ Not built | Built as Sam feeds context |
| **Agent heartbeat / online-signal** | ⏳ Not built | No clean signal for "is samai/aick/cena online." Propose post-Part-4. |
| **samai bootstrap script on CK profile** | ⏳ Not built | Manual cold-start every session. |

---

## Open Threads

- **Track 5 (OpenClaw full uninstall)** — still pending. Both machines. Final step of the cutover.
- **Cena gateway wake-on-post permanent fix** — Tailscale tunnel drops on Render redeploy. Deferred.
- **plan.md final team review** — posted to dev chat, awaiting aick/ck/samai/dck responses.
- **Samples page approval workflow** — dck + ck building.
- **plan.md on Samples page** — ck building.
- **Right-side menu bar mockup** — dck queued, starts after plan.md confirmed.
- **Playwright batch session** — when Sam calls it, team runs all backlogged tests at once.
- **Agent heartbeat mechanism** — surface to Sam post-Part-4.
- **samai bootstrap automation on CK profile** — samai-spec lane ticket.
- **samai-side Render diagnostic access** — pairs with gateway-in-repo.
- **SSH-to-aick + gh CLI for samai** — structural upgrade queued.

---

*Last updated: 2026-05-23 — corrected stale "aick sole pusher" line; ck has own push access since 2026-05-20. Repo URL clarified: samsahragard/cenas-ezlive.*
