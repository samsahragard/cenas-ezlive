# app/sports/ — Sports Board backend kit (staged, inert)

Delivered + offline-verified modules for the **Sports Board** ("What's On")
manager tab. See `AGENT_SPORTS_BUILD.md` for the full build plan (Agents 1–8).

## Status

- **Phase 1 (LIVE):** the UI tab ships on self-contained SAMPLE DATA.
  - Template: `app/templates/sports_dashboard.html`
  - Route: `store.sports_dashboard` → `GET /<store>/sports` (in `app/web/store_routes.py`)
  - Surfaced as the **Sports** tab on the Manager dashboard (`/<store>/manager?tab=sports`).
- **Phase 2 (NOT wired yet — gated on Sam):** the live feed.
  - `sports_core.py` — Houston-time bucketing, dedup upsert, the 6 category queries, favorites, search. **22/22 engine tests pass on Linux** (`test_sports_core.py`). Note: `strftime("%-d")` is POSIX-only, so the test fails on Windows but runs on Render/Linux.
  - `sports_broadcast.py` — verified DirecTV channel map + streaming rules + `resolve_broadcast()`.
  - `sports_provider.py` — `SportsProvider` interface + `EspnProvider` (reference) + `FixtureProvider`.
  - `sports_routes.py` — Flask blueprint `create_sports_bp(get_conn, default_user, url_prefix)` exposing `/api/sports/*`.
  - `sports_schema.sql` — games / favorites / sync_log tables.

## To go live (next phase)

1. Apply `sports_schema.sql` to the runtime DB (Agent 4).
2. `app.register_blueprint(create_sports_bp(get_conn, default_user="manager"))` and wire `get_conn`; adapt `?` → `%s` if mounted on Postgres (Agent 5).
3. Allowlist provider egress on Render + schedule the sync (Agent 1).
4. Point the template's JS at `/api/sports/*` in place of the SAMPLE DATA array (Agent 6).

Nothing in this directory is imported by the app yet — it is staged foundation only.
