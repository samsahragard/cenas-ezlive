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
    "/api/inbox/whatsapp",       # CK daemon WhatsApp mirror (Bearer-token gated)
    "/produce/confirm/",         # Sam's tap-from-Telegram links (random order_id is the auth)
    "/produce/cancel/",          # Sam's tap-from-Telegram links (random order_id is the auth)
    "/produce/healthz",          # public liveness check
    "/produce/admin/",           # ingest-state diagnostic (Bearer-token gated inside)
    "/static/",                  # static assets
    "/favicon.ico",
    "/login",
    "/logout",
    "/partner-login",            # second-factor for Partner — still gated by /login
    "/keypad-login",             # 2026-05-11 keypad auth (migration 13)
    "/keypad-logout",
    "/change-passcode",          # post-keypad-login, before main app
    "/install",                  # public PWA install instructions (was dropped in cb0d482, restored)
    "/privacy",                  # public privacy policy (Play Store + general audit requirement)
    "/request-access",           # public access-request form (gated approval inside)
    "/cron/",                    # Render Cron Job endpoints — own CRON_TOKEN header check inside
    "/sam/cena/log",             # Cena gateway audit ingest — own X-Cena-Token header check inside
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
