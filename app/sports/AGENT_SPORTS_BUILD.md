# AGENT_SPORTS_BUILD.md — Sports Tracker (Manager Tabs)

Reference build package for the **Sports Board** manager tab. Drop the delivered
modules into the repo, then dispatch the subagents below through **ck** to wire
them to the live app, the live data feed, and the dashboard UI.

---

## Context (repeat to every agent — agents have zero memory of prior work)

- **App:** app.cenaskitchen.com — Flask / gunicorn, SQLite (Mini_IT13 runtime) + Postgres (Render). Repo: `github.com/samsahragard/cenas-ezlive`.
- **Goal:** a **Sports** nav item in Manager Tabs (mirror the existing "Sections" nav pattern) showing major games by category — **Today, Live, Upcoming, Completed, Previous, Favorites** — every time in **Houston / Central** time, with sport, league, teams, venue, status, score, TV network, **DirecTV channel (shown only when verified)**, and streaming platforms.
- **Operating intent:** this is an operations tool. The hero action is *"put it on channel X"* so a manager can set the TVs. Verified DirecTV numbers and the Live/Today buckets matter most.
- **Sequencing = gates only, never dates:** `freeze → run → green → unlock`. An agent freezes its territory, runs, proves green (tests pass / states render), then unlocks the next agent. No calendar scheduling.
- **All build work runs through `ck` + subagents.** Each agent message must be fully self-contained and repeat its territory, rules, and current state.

## Files delivered in this package (already written + verified)

| File | What it is | State |
|---|---|---|
| `sports_schema.sql` | games / favorites / sync_log tables; dedup key + indexes | ready |
| `sports_broadcast.py` | DirecTV map (verified national numbers) + streaming rules + `resolve_broadcast()` | green |
| `sports_core.py` | Houston-time conversion, content-hash, dedup upsert, 6 category queries, favorites, search | green |
| `sports_provider.py` | `SportsProvider` interface + `EspnProvider` (reference) + `FixtureProvider` | transform tested; live HTTP needs Render egress |
| `sports_routes.py` | Flask blueprint — all API endpoints + sync orchestrator | green (test client) |
| `test_sports_core.py` | offline test: dedup, idempotency, status transitions, CT bucketing, favorites, search | **22/22 pass** |
| `cenas_sports.html` | interactive UI prototype (mobile + desktop) — the living visual spec | **12/12 logic checks pass** |

**Verification run in this package:** 22 engine + 15 API + 12 front-end = **49 automated checks pass.** Not yet exercised: the live provider HTTP fetch (needs outbound network on Render) and rendering inside the real dashboard shell.

---

## Agent 1: Sports Data

```
Agent 1 — SPORTS DATA. You have zero memory of prior work; everything you need is here.

CONTEXT: Flask/gunicorn app at app.cenaskitchen.com, repo github.com/samsahragard/cenas-ezlive,
SQLite (Mini_IT13) + Postgres (Render). We are adding a Sports manager tab. A data-source layer
is already written: sports_provider.py defines SportsProvider (interface), EspnProvider (reference
fetcher against ESPN public scoreboard endpoints), and FixtureProvider (tests). The engine that
stores games is sports_core.upsert_games(); do not reimplement it.

YOUR TERRITORY (own these; do not edit other files):
  - sports_provider.py
  - the scheduled sync job that calls it

MISSION:
  1. Choose the production provider. EspnProvider is zero-cost but undocumented; SportRadar /
     API-Sports are paid and reliable. Keep the SportsProvider interface so it stays swappable.
  2. On Render, allowlist outbound egress to the provider host (e.g. site.api.espn.com) so the
     fetch path works — it does not work from the offline build sandbox.
  3. Wire a recurring sync that calls provider.fetch(league) for each league in scope
     (MLB, NBA, NHL, NFL, WNBA, 'FIFA World Cup', MLS, NCAAF as needed) and passes results to
     sports_core.upsert_games(conn, games, provider=<name>, league=<league>). Cadence: frequent
     for live windows, slower otherwise. Reuse the platform's existing watchdog/health pattern.
  4. Map each provider's raw status onto the normalized vocabulary in sports_core
     (scheduled/pre/in_progress/halftime/delayed/postponed/final/canceled).

RULES: self-contained; gates only (freeze sports_provider.py → run a real fetch → prove rows land
in sports_games via /api/sports/health → unlock). Do NOT touch sports_core, sports_routes, or the UI.

DONE / GATE: a live sync inserts real games for the in-season leagues, /api/sports/health shows
ok runs with non-zero games_seen, and a re-run reports games_updated/skipped (no duplicate rows).
```

## Agent 2: Broadcast / Streaming Mapping

```
Agent 2 — BROADCAST / STREAMING MAPPING. Zero memory; full context below.

CONTEXT: Same app/repo/stack. sports_broadcast.py is the curated "encoded knowledge" layer: a
DirecTV national-channel map (verified June 2026), a network->streaming map, league default
packages, and resolve_broadcast(league, networks) which returns {tv, directv[], streaming[]}.
The data feed tells us which network carries a game; this module enriches it with channel numbers
and streaming homes. The UI shows a DirecTV number ONLY when verified=True.

YOUR TERRITORY (own this; do not edit other files):
  - sports_broadcast.py

MISSION:
  1. Confirm/extend DirecTV numbers. National numbers are verified (ESPN 206, ESPN2 209, FS1 219,
     FS2 618, TNT 245, TBS 247, NBA TV 216, NFL Network 212, MLB Network 213, NHL Network 215,
     CBS Sports Network 221, SEC 611, ACC 612, Big Ten 610, Golf 218, Tennis 217). Local affiliates
     (ABC/CBS/FOX/NBC/Telemundo/Universo) are verified=False because the number varies by market —
     resolve the Houston (DMA 618) numbers ONCE and set verified=True for those, keeping verified=False
     as the national default.
  2. Keep league packages current: NBA = ABC/ESPN(ESPN App), NBC(Peacock), Prime Video — TNT is OUT;
     World Cup = FOX/FS1 + FOX One + Tubi(select free) + Telemundo/Universo + Peacock(Spanish);
     NFL = CBS->Paramount+, FOX->FOX One, NBC SNF->Peacock, ESPN/ABC->ESPN App, Amazon TNF->Prime,
     Netflix->Christmas; MLB = MLB.TV + Apple TV+ Fridays + national FOX/TBS/ESPN; NHL = ESPN/ABC +
     TNT/TBS(Max) + ESPN+. Mark matchup-dependent options predicted=True.

RULES: self-contained; gates only. Do NOT touch the data feed, engine, routes, or UI. Edit THIS
file when a rights deal or channel number changes — never hardcode broadcast logic elsewhere.

DONE / GATE: `python3 sports_broadcast.py` prints correct mappings for World Cup / MLB / NBA / NHL,
and every verified DirecTV number is double-checked against a current DirecTV lineup source.
```

## Agent 3: Timezone

```
Agent 3 — TIMEZONE. Zero memory; full context below.

CONTEXT: Same app/repo/stack. All timestamps are stored as UTC ISO-8601 strings. Display and
bucketing convert to Houston/Central via zoneinfo America/Chicago in sports_core.py (to_central,
central_now, central_today, central_fields, _central_day_bounds). "Today" is computed against the
current Central date, so it resets at Central midnight with no cron. DST (CST/CDT) is automatic.

YOUR TERRITORY (own these; do not edit other files):
  - the time helpers + _central_day_bounds in sports_core.py
  - a DST-edge test file

MISSION:
  1. Ensure the Render image has tz data (the `tzdata` package or system zoneinfo) so
     ZoneInfo("America/Chicago") resolves in production — not just on Mini_IT13.
  2. Add tests for DST boundaries (March spring-forward, November fall-back): a game at 11:30pm CT
     buckets into the correct Central day on both sides of a transition, and central_fields emits
     CST vs CDT correctly.
  3. Confirm the front-end clock + labels (Intl 'America/Chicago' in cenas_sports.html) agree with
     the server's central_fields for the same instant.

RULES: self-contained; gates only. Storage stays UTC — never store local time. Do NOT change the
dedup path, routes, or UI beyond verifying time agreement.

DONE / GATE: DST-edge tests pass and a sample instant renders the same CT label on server and client.
```

## Agent 4: Duplicate Checking

```
Agent 4 — DUPLICATE CHECKING. Zero memory; full context below.

CONTEXT: Same app/repo/stack. Dedup is enforced by UNIQUE(provider, provider_game_id) in
sports_schema.sql plus content_hash in sports_core.upsert_games(): a game already shown is UPDATED
in place, never re-inserted, and an unchanged game is a no-op (skipped). Every run writes a row to
sports_sync_log. Categories: Previous = final games before today (Houston) so past games stay
visible; Completed = all finals newest-first; live games update via re-sync.

YOUR TERRITORY (own these; do not edit other files):
  - the upsert/dedup path in sports_core.py
  - schema migration / backfill for sports_games
  - dedup tests

MISSION:
  1. Apply sports_schema.sql to the runtime DB (CREATE TABLE IF NOT EXISTS — safe to re-run).
  2. If any sports games are ALREADY shown in the app from a prior mechanism, reconcile them to
     (provider, provider_game_id) so the first live sync updates rather than duplicates them. If no
     stable external id exists for legacy rows, define a deterministic fallback key
     (e.g. provider='legacy' + slug of league|date|home|away) and document it.
  3. Verify idempotency at scale: a double-sync of the same payload yields 0 inserted / N skipped
     and the row count is unchanged.

RULES: self-contained; gates only (freeze the upsert path → run double-sync → prove zero dupes →
unlock Agents 1 and 5). Do NOT change broadcast logic, time helpers, routes, or UI.

DONE / GATE: test_sports_core.py passes (it already asserts no duplicate rows after re-sync and
after updates), and a production double-sync shows games_inserted=0 on the second run.
```

## Agent 5: Backend / API

```
Agent 5 — BACKEND / API. Zero memory; full context below. UNLOCKS AFTER Agent 4 (schema live).

CONTEXT: Same app/repo/stack. sports_routes.create_sports_bp(get_conn, default_user, url_prefix)
returns a Flask blueprint exposing every endpoint (see API reference at the bottom of this doc).
get_conn is a zero-arg callable returning a DB connection with row_factory = sqlite3.Row.
Placeholders are SQLite-style (?). Sports tables are read-mostly cache data and sit naturally on
the SQLite runtime DB.

YOUR TERRITORY (own these; do not edit other files):
  - sports_routes.py
  - blueprint registration in the app factory

MISSION:
  1. Register the blueprint: app.register_blueprint(create_sports_bp(get_conn, default_user="manager")).
  2. Wire get_conn to the app's DB layer. If you mount the tables on Postgres instead of SQLite,
     adapt placeholders to %s (or route through the existing DB wrapper) — keep the queries otherwise
     identical to sports_core.
  3. Apply the dashboard's existing auth/role gating to the write endpoints (POST/DELETE /favorite,
     POST /sync). /sync is admin-only.
  4. Confirm /games (each tab), /game/<id>, /search, /counts, /favorites, /favorite, /sync, /health
     all return correctly against live data.

RULES: self-contained; gates only. Do NOT change the engine math, the broadcast map, or the UI.

DONE / GATE: every endpoint returns 200 with live data behind the dashboard's auth, and /health
reports total_games and recent_syncs. Unlocks Agent 6.
```

## Agent 6: Frontend / UI

```
Agent 6 — FRONTEND / UI. Zero memory; full context below. UNLOCKS AFTER Agent 5 (API green).

CONTEXT: Same app/repo/stack. cenas_sports.html is the approved visual prototype: sticky header with
a live Houston clock, six category tabs with counts, sport filter chips, a search box, game cards
with a signature DirecTV "channel tile", a details drawer (right slide on desktop, bottom sheet on
mobile), favorites, and full mobile/desktop responsiveness. It currently runs on SAMPLE DATA with
bucketing + broadcast mapping that exactly mirror the backend.

YOUR TERRITORY (own these; do not edit other files):
  - the Sports tab view/components in the dashboard
  - the "Sports" nav item registration (mirror the "Sections" nav pattern)

MISSION:
  1. Port cenas_sports.html into the dashboard's actual component/template system (match the repo's
     existing conventions — do not bolt on a foreign framework).
  2. Replace the SAMPLE DATA array with live calls to the Agent 5 API:
       tabs/filters/search -> GET /api/sports/games?tab=&league=&user= and /search?q=
       tab badges          -> GET /api/sports/counts
       card click          -> GET /api/sports/game/<id> for the drawer
       star + drawer fav    -> POST/DELETE /api/sports/favorite
     Keep the client-side display formatting (the server already returns ct_label/ct_time, etc.).
  3. Either adopt the prototype's Sports identity (scoreboard amber + signal red + Space Mono digits)
     or remap to the dashboard's tokens — keep the channel tile and the live treatment as the signature.
  4. Preserve the quality floor: responsive to mobile, visible keyboard focus, prefers-reduced-motion.

RULES: self-contained; gates only. Do NOT change the API contract, the engine, or the broadcast map.

DONE / GATE: the Sports tab loads live data on mobile + desktop, tabs/filters/search/favorites/drawer
all work against the API, and "today" reflects the current Houston day.
```

## Agent 7: Rendering / Mockup

```
Agent 7 — RENDERING / MOCKUP. Zero memory; full context below. Runs alongside Agent 6.

CONTEXT: Same app/repo/stack. cenas_sports.html is the living visual spec. It must keep rendering
correctly in isolation so design changes can be reviewed without the full app, and so Agent 6 has a
source of truth to port from.

YOUR TERRITORY (own this; do not edit other files):
  - cenas_sports.html (the prototype) + rendered state captures

MISSION:
  1. Produce sign-off renders of each state: Today (mixed), Live-only, Upcoming, Completed, Previous,
     Favorites, the empty state, and the details drawer — at desktop and mobile widths.
  2. Verify the signature reads at a glance: verified channel tile (amber number + check), unverified
     local-affiliate tile, and streaming-only tile (e.g. the Dynamo / Apple TV case).
  3. Confirm reduced-motion disables animation and keyboard focus is visible.

RULES: self-contained; gates only. The prototype stays a single self-contained file (Google Fonts
CDN is the only external dependency). No localStorage. Do NOT wire it to the live API — that is Agent 6.

DONE / GATE: all states render correctly at both breakpoints and are captured for sign-off.
```

## Agent 8: QA

```
Agent 8 — QA. Zero memory; full context below. Gates the final merge.

CONTEXT: Same app/repo/stack. Test assets exist: test_sports_core.py (engine), the Flask test-client
checks (API), and the Node logic harness (front-end). This package already passes 49 automated checks.

YOUR TERRITORY (own these; read-only on everything else):
  - the test suites + the manual QA checklist below

MISSION:
  1. Run test_sports_core.py and the API/front-end harnesses; all must stay green.
  2. Walk the manual QA checklist (below) against the integrated app on mobile and desktop.
  3. Block the merge on any failure; report results in the freeze→run→green→unlock format.

RULES: self-contained; gates only. Do NOT fix code in place — file the failure back to the owning
agent (1–7). QA verifies; it does not patch other territories.

DONE / GATE: automated suites green + manual checklist complete → unlock merge.
```

---

## API reference (Agent 5 / Agent 6)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/sports/games?tab=&league=&user=` | List a category (today\|live\|upcoming\|completed\|previous\|favorites) |
| GET | `/api/sports/game/<provider_game_id>` | One game, full broadcast payload (for the drawer) |
| GET | `/api/sports/search?q=` | Search team / league / venue / network |
| GET | `/api/sports/counts?user=&league=` | Tab badge counts |
| GET | `/api/sports/favorites?user=` | Raw favorite rows for a user |
| POST | `/api/sports/favorite` | `{user, scope:'game'|'team'|'league', ref}` → pin |
| DELETE | `/api/sports/favorite` | `{user, scope, ref}` → unpin |
| POST | `/api/sports/sync` | Admin: `{provider:'espn', leagues:[...]}` → fetch + upsert |
| GET | `/api/sports/health` | total games + last 20 sync runs + server CT |

## Data-source notes

- **ESPN (reference):** `https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard` — free, no key, undocumented. Good to ship on, but pin **SportRadar / API-Sports** for a contractual SLA. Allowlist the host on Render egress.
- **DirecTV numbers** live only in `sports_broadcast.py`; the UI shows a number only when `verified=True`. Resolve Houston local-affiliate numbers once (Agent 2).
- **Streaming** marked `predicted=True` is a league-package guess that depends on the matchup — the UI dims it and labels it.

---

## QA checklist

**Automated (must stay green)**
- [ ] `python3 sports_broadcast.py` prints correct maps (World Cup / MLB / NBA / NHL).
- [ ] `python3 test_sports_core.py` → 22/22 (dedup, idempotency, status transitions, CT bucketing, favorites, search).
- [ ] Flask test-client → all endpoints 200 / correct error codes (15 checks).
- [ ] Front-end logic harness → 12/12 (bucketing + mapping parity with backend).
- [ ] DST-edge tests pass (Agent 3).

**Data & dedup**
- [ ] A live sync inserts in-season games; a second sync inserts 0 (updates/skips only) — no duplicate rows.
- [ ] Legacy/already-shown games reconcile to provider ids (no doubles after first sync).
- [ ] Live games update score/status on re-sync; finals stop updating.
- [ ] `/api/sports/health` shows ok runs with sane seen/inserted/updated/skipped.

**Time (Houston / Central)**
- [ ] Every displayed time is Central (CST/CDT label correct).
- [ ] "Today" matches the current Houston calendar day and rolls over at Central midnight.
- [ ] A game near midnight CT lands in the correct day's bucket.

**Categories**
- [ ] Live shows only in-progress games; they also appear under Today.
- [ ] Upcoming = future scheduled within the horizon; ordered soonest-first.
- [ ] Completed = all finals, newest first (includes today's finals).
- [ ] Previous = finals before today; past games stay visible there.
- [ ] Favorites unions pinned games + favorited teams + favorited leagues.

**Broadcast / channels**
- [ ] Verified DirecTV numbers render on the tile + drawer with a check; unverified ones never show a fake number.
- [ ] Local affiliates flagged "varies by market" until Houston numbers are set.
- [ ] Streaming chips correct; predicted (matchup-dependent) ones visibly dimmed/labeled.
- [ ] Streaming-only games (e.g. MLS on Apple TV) show "Streaming only" + a stream tile, no channel.

**UI / device**
- [ ] Tabs, filters, search, favorites, and the drawer all work on desktop and mobile.
- [ ] Drawer = right slide on desktop, bottom sheet on mobile; closes on scrim / × / Esc.
- [ ] Keyboard focus visible; prefers-reduced-motion disables animation.
- [ ] Empty states show the right message per tab.
- [ ] Write actions gated by the dashboard's auth; `/sync` admin-only.
