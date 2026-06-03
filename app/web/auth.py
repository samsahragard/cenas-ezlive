"""Simple shared-password gate for the EZLive UI.

The whole site is exposed publicly via Tailscale Funnel
(https://aick.tailb5e6ee.ts.net), so anyone with the URL can reach
every route. This middleware redirects unauthenticated visitors to
/login, where they enter the shared `EZLIVE_PASSWORD` (env var,
defaults to "cenas"). The session cookie keeps them logged in for
30 days unless they hit /logout.

Webhook + ingest endpoints are exempt because they have their own
authentication (HMAC / Bearer token) and need to be reachable
without a session cookie.
"""
from __future__ import annotations

import os
from datetime import timedelta
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

auth = Blueprint("auth", __name__)

# Paths that bypass the password gate. Match by prefix.
EXEMPT_PREFIXES = (
    "/ezcater/webhook",          # ezCater POSTs here; payload is the auth
    "/orders/ingest",            # legacy PDF ingest (token-protected)
    "/orders/ingest_structured", # API ingest (token-protected)
    "/produce/confirm/",         # Sam's tap-from-Telegram links (random order_id is the auth)
    "/produce/cancel/",          # Sam's tap-from-Telegram links (random order_id is the auth)
    "/produce/healthz",          # public liveness check
    "/produce/admin/",           # ingest-state diagnostic (Bearer-token gated inside)
    "/partner/schedules-v2/migration/run",  # B3 Sling import trigger - partner_auth_ok OR INGEST_TOKEN bearer gated inside the view (employee firewall still 403s employee sessions)
    "/static/",                  # static assets
    "/favicon.ico",
    "/login",
    "/logout",
    "/partner-login",            # second-factor for Partner — still gated by /login
    "/keypad-login",             # 2026-05-11 keypad auth (migration 13)
    "/keypad-logout",
    "/employee/login",           # Schedules V2 B2: employee SMS-login flow (request-code/verify-code) — fresh employees have no session yet
    "/employee/dashboard",       # Schedules V2 B2: the employee dashboard route checks session["employee_id"] itself and 302s to /employee/login when absent (not the staff keypad); a logged-in employee passes via auth_ok anyway
    "/employee/my-schedule",     # Schedules V2 B5: same as dashboard — the my_schedule_page route self-guards on session["employee_id"] and 302s to /employee/login (not the keypad). Prefix also covers aick's /employee/my-schedule/shifts data endpoint, which has its OWN employee_id guard (401/403); employee isolation is enforced there, not by this site gate, so the exemption only swaps the unauth response from a keypad redirect to a JSON 401
    "/employee/alarm-preferences",  # Schedules V2 B6: ckai's employee shift-alarm prefs API — self-guards session["employee_id"] (401 JSON), same treatment as /employee/my-schedule
    "/employee/profile",         # Schedules V2 B6: same pattern as my-schedule - the employee_profile_page route self-guards on session["employee_id"] and 302s to /employee/login (not the keypad). (ckai's prefs endpoint is /employee/alarm-preferences, EXEMPT-prefixed separately by ckai and returning its own 401 JSON; this prefix is only for the page.)
    "/employee/my-profile",      # Staff profile hub: self-guards on session["employee_id"] and 302s to /employee/login when absent.
    "/employee/time-off",        # Schedules V2 B7: one prefix covers ck's PAGE GET /employee/time-off (HTML; self-guards employee_id -> 302 /employee/login) AND ckai's DATA /employee/time-off/list + /request + DELETE /<id> (each self-guards employee_id -> 401 JSON). Same treatment as /employee/my-schedule.
    "/employee/availability",    # Schedules V2 B8: one prefix covers ck's PAGE GET /employee/availability (HTML; self-guards employee_id -> 302 /employee/login) AND ckai's DATA /employee/availability/list + /recurring + /block (each self-guards employee_id -> 401 JSON). Same treatment as /employee/my-schedule.
    "/employee/shift-offers",    # Schedules V2 B9: ckai's offer API (create/take/cancel + /list) - self-guards employee_id (401 JSON); a ck HTML page can own a bare child path under it.
    "/employee/shift-marketplace",  # Schedules V2 B9: ckai's marketplace DATA (/list) + ck's PAGE - self-guards employee_id (401 JSON).
    "/employee/shift-swaps",     # Schedules V2 B9: ckai's swap API (propose/accept/cancel + /list) - self-guards employee_id (401 JSON).
    "/employee/setup",           # email-pivot: the emailed one-time setup link (GET /employee/setup/<token> page + /info + /complete) - the invited employee has no session yet; the single-use expiring sha256 token IS the auth. ckai.
    "/change-passcode",          # post-keypad-login, before main app
    "/install",                  # public PWA install instructions (was dropped in cb0d482, restored)
    "/driver/app.apk",           # public APK download redirect — drivers need this BEFORE having an account
    "/privacy",                  # public privacy policy (Play Store + general audit requirement)
    "/request-access",           # public access-request form (gated approval inside)
    "/internal/scheduling/cron/",  # Schedules V2 B6: ckai's per-minute shift-alarm cron — own CRON_TOKEN Bearer check inside (fail-closed)
    "/cron/",                    # Render Cron Job endpoints — own CRON_TOKEN header check inside
    "/sam/cena/log",             # Cena gateway audit ingest — own X-Cena-Token header check inside
    "/sam/cena/usage-log",       # Cena gateway per-turn usage ingest — own X-Cena-Token header check inside
    "/sam/cena/db-probe/",       # Cena gateway read-only DB probe — own X-Cena-Token header check inside
    "/sam/cena/resolve/",        # Cena gateway OQ-5 resolve_* endpoints — own X-Cena-Token header check inside
    "/sam/cena/ezcater-order-full",  # Cena gateway ezcater_get_order_full_details (Sam #530 PDF pipeline) — own X-Cena-Token check inside
    "/sam/cena/dev-chat",        # Cena gateway dev-chat read — own X-Cena-Token header check inside
    "/sam/cena/sam-chat",        # dck observer read of /sam/chat — own X-Cena-Token header check inside (Track 8 per cena #1907)
    "/sam/cena/sam-chat-attachment/",  # binary attachment download for dev-team vision parity per Sam #837 item 5 — own X-Cena-Token check inside
    "/sam/cena/telegram-test-fire",  # Cena gateway Track 2 test-fire trigger — own X-Cena-Token check inside
    "/sam/cena/run-",                # Cena gateway one-shot script triggers (run-seed-test-drivers, run-flip-buildplan-approval) — own X-Cena-Token check inside
    "/docck/",                      # docck v1 multi-agent monitor — heartbeat/status/tick/admin all have own bearer-token auth inside
    "/sam/chat/todos/current",       # Cena gateway top-of-list TODO read (no-skip rule) — dual-gated: Sam session OR X-Cena-Token header
    "/ez-manage/pending-count.json",  # XHR poll endpoint — own MANAGER_ROLES check inside, returns 401 JSON on unauth (not 302) so the sidebar badge fetcher renders cleanly + samai's stateless gate-3 probe sees the canonical 401 JSON signal (Cena #1820 + samai #1787)
    "/catering/assign_driver/result",  # aick gateway callback after running driver re-assign — own X-Cena-Token check inside
)


def install(app):
    """Register the auth blueprint and the global before_request gate."""
    # 30-day session lifetime so staff don't have to re-enter the password constantly
    app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(days=30))
    app.register_blueprint(auth)

    @app.before_request
    def _gate():
        path = request.path or "/"
        if any(path.startswith(p) for p in EXEMPT_PREFIXES):
            return None
        # 2026-05-11: accept either the new keypad session (session.user_id)
        # OR the legacy shared-password session (session.auth_ok) so the
        # chat-tail/post tooling keeps working unchanged.
        if session.get("user_id") or session.get("auth_ok"):
            return None
        # Human visitors land on the keypad. Tools that want the legacy
        # password form can hit /login directly.
        return redirect(url_for("keypad_auth.login", next=path))


@auth.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = (request.form.get("password") or "").strip()
        expected = os.getenv("EZLIVE_PASSWORD", "cenas")
        if pw == expected:
            session["auth_ok"] = True
            session.permanent = True
            nxt = request.args.get("next") or "/"
            # Don't allow open-redirect to other domains
            if not nxt.startswith("/"):
                nxt = "/"
            return redirect(nxt)
        return render_template("login.html", error="Wrong password."), 401
    return render_template("login.html", error=None)


@auth.route("/logout")
def logout():
    session.pop("auth_ok", None)
    session.pop("partner_auth_ok", None)
    return redirect(url_for("auth.login"))


@auth.route("/partner-login", methods=["GET", "POST"])
def partner_login():
    """Second-factor gate for /partner/* — Partner is owner-only and shows
    private data (full management labor, future legal/financial sections).

    Requires the user to already be past the site-level `cenas` gate. The
    `PARTNER_PASSWORD` env var is the second-factor password (separate
    from EZLIVE_PASSWORD so co-managers can be in the site without seeing
    Partner data)."""
    if request.method == "POST":
        pw = (request.form.get("password") or "").strip()
        expected = os.getenv("PARTNER_PASSWORD", "")
        if expected and pw == expected:
            session["partner_auth_ok"] = True
            session.permanent = True
            return redirect("/partner/")
        return render_template("partner_login.html", error="Wrong password."), 401
    return render_template("partner_login.html", error=None)


@auth.route("/access-denied")
def access_denied():
    """Generic permission-denied landing. requires_permission redirects
    here with ?need=<tag>&next=<path> when the user lacks the required
    permission AND PERMISSION_ENFORCE=1. Dark-launch (the default) logs
    denials instead of redirecting, so this page only renders once the
    enforcing flag flips on.

    The page reads the query string and tells the user which tag they
    need + offers a link to /request-access pre-filled. Phase 0 Block 4
    (ck, 2026-05-13)."""
    need = (request.args.get("need") or "").strip()[:80] or None
    nxt = (request.args.get("next") or "").strip()[:200] or None
    return render_template(
        "access_denied.html", need=need, next_path=nxt,
    ), 403


@auth.route("/install", strict_slashes=False)
def install_page():
    """Public install/share page — no auth required so the link works for
    fresh visitors. The dashboard is already a PWA (manifest.webmanifest +
    apple-touch-icon + theme-color all wired in base_dashboard.html), so
    users just need clear instructions to Add-to-Home-Screen.

    strict_slashes=False so /install AND /install/ both work; Chrome on
    some devices auto-appends a slash when the URL is retyped.
    """
    return render_template("install.html")


@auth.route("/")
def store_picker():
    """Bare-URL landing. Sam's 2026-05-11 spec: nobody should ever see the
    4-dashboard picker — every visitor either gets bounced to /keypad-login
    (no session) or auto-routed to their role landing (signed in).

    The before_request gate handles the unauthed case by redirecting to
    /keypad-login. When this view actually runs, the user is authenticated
    one way or another:
      • Keypad session (session.user_id + g.current_user) → role landing.
      • Legacy auth_ok-only session (chat tools / etc.) → Partner if
        partner_auth_ok, otherwise we fall through to /partner-login.
    """
    from flask import g, redirect
    from app.web.keypad_auth import _landing_for_user

    u = getattr(g, "current_user", None)
    if u is not None:
        return redirect(_landing_for_user(u))
    # Legacy tool session — keep prior behavior of letting them through to
    # /partner/ since that's what tools target.
    if session.get("partner_auth_ok"):
        return redirect("/partner/")
    # Tier-1 only (auth_ok without partner) — go through partner-login.
    return redirect(url_for("auth.partner_login"))
