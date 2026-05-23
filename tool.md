# Cena Tool Reference

> Auto-generated 2026-05-23. This file lists every tool available to Cena at runtime.
> Lives at repo root, auto-loaded at session start alongside CENA_CHARTER.md / CENA.md / APP_STATUS.md / plan.md.

---

## Current Tools (19)

| # | Tool | What it does |
|---|---|---|
| 1 | `read_file` | Read any file from the codebase on AiCk. Path can be absolute or relative to repo root. |
| 2 | `write_file` | Write or overwrite a file in the codebase. Creates parent directories if needed. |
| 3 | `list_dir` | List files and directories at a given path. |
| 4 | `run_git` | Run a git command in the cenas-ezlive repo. Allowed: status, log, diff, show, add, commit, push, pull, branch, checkout. |
| 5 | `fetch_url` | Make an HTTP request to any URL. Used for ad-hoc API calls (Cloudflare, Twilio, etc.) and live-site checks. |
| 6 | `shell_execute` | Run a shell command on AiCk. Full shell access, admin power. cwd defaults to repo root. |
| 7 | `file_delete` | Delete a file. Refuses to delete directories. |
| 8 | `render_env_get` | Read all env vars on the cenas-ezlive Render service. **BANNED from /sam/chat** — dumps all secrets into chat history. Use targeted methods only. |
| 9 | `render_env_set` | Set one env var on the cenas-ezlive Render service. Triggers a redeploy. Use sparingly. |
| 10 | `render_deploy` | Trigger a manual deploy of the cenas-ezlive Render service. Returns deploy id and initial status. |
| 11 | `telegram_send` | Send a message to Sam via Telegram. Currently a stub — appends to local `cena_telegram_outbox.log` on AiCk. Real bidirectional Telegram is greenlit but not yet built. |
| 12 | `post_to_dev_chat` | Post a message to the /partner/developer/chat thread as author "cena". Post-only — no read side. |
| 13 | `journal_write` | Write an entry to Cena's persistent journal (SQLite on AiCk). Use to remember things across sessions. |
| 14 | `journal_read` | Read entries from Cena's persistent journal. Filter by topic, tag, or get most recent N. |
| 15 | `sql_query` | Run a read-only SELECT against the live production database. SELECT and WITH-prefixed queries only. Default 200 rows, max 1000. |
| 16 | `self_critique` | Pass a draft answer through a second Claude critic pass. Use before sending anything important to Sam. |
| 17 | `web_search` | Search the internet for up-to-date information. Max 5 uses per turn. |
| 18 | `screenshot_url` | Load any web page in headless Chrome on AiCk and capture a screenshot. Use to visually inspect the live site. |
| 19 | `post_to_sam_chat` | Inject a message into /sam/chat as role='cena'. Use ONLY when Sam explicitly asks from another channel. |

---

## Tools Approved but Not Yet Built

| # | Tool | What it does | Status |
|---|---|---|---|
| 20 | `read_hub_inbox` | Read messages from the local LAN chat hub inbox file (`C:\Users\sam\cena\cena_hub_inbox.jsonl`). Allows Cena to see team messages on demand. | **In progress — aick building** |
| 21 | `wake_on_hub` | Proactively wake Cena when new messages arrive on the local chat hub (`/cena/wake-on-hub` endpoint on cena_gateway.py). | **In progress — aick building (Option A approved by Sam)** |
| 22 | `remove_participant` | Remove a participant from the /sam/chat interface. | **Sam approved — team building** |
| 23 | `toast_live_tables` | Query Toast's live POS data for tables and open tickets in real time. | **Sam approved — urgent — team building** |

---

## Tools Greenlit but Not Yet Requested

| Tool | What it does | Status |
|---|---|---|
| WhatsApp send | Send WhatsApp messages via Baileys (346-462-0476, standalone Node process). | Baileys connection built by ck. QR scan pending from Sam. Send layer still to build. |
| Telegram receive | Receive Telegram messages from Sam via Cenasai bot (token: provided). | Greenlit, not built. |
| Voice | Voice interface for Cena. | Greenlit, not built. |

---

## Protocol — Acquiring New Tools

1. Cena identifies a missing tool and asks Sam for permission to acquire it.
2. Sam approves (or declines) in /sam/chat.
3. Cena instructs the team (via hub or dev chat) to build and deploy the tool.
4. Team builds, aick deploys to cena_gateway.py, samai reviews.
5. Tool is added to this file and to the startup auto-load list.

---

## Notes

- `render_env_get` is **permanently banned from /sam/chat** — it dumps all secret keys into chat history. Never run it in Sam-facing chat.
- `shell_execute` gives full shell access on AiCk — use carefully for destructive operations.
- `post_to_dev_chat` has been intermittently failing (silent failures noted 2026-05-22). Use `shell_execute` with `lanchat.py` as fallback for hub posts.
- `sql_query` hits the **production database** — read-only, but live data. Entity-resolve before querying per §4B.1.

---

*Last updated: 2026-05-23. Updated by Cena.*
