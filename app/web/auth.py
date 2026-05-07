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
    "/static/",                  # static assets
    "/favicon.ico",
    "/login",
    "/logout",
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
        if session.get("auth_ok"):
            return None
        # Preserve the intended destination so we redirect back after login
        return redirect(url_for("auth.login", next=path))


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
    return redirect(url_for("auth.login"))
