#!/usr/bin/env bash
# Render start command for cenas-ezlive — joins the Tailscale tailnet
# (tailb5e6ee.ts.net) in userspace mode, then execs the Flask app under
# gunicorn. Replaces Render's auto-detected default startCommand so the
# /sam/chat surface can reach the Cena gateway on AiCk (100.108.119.19:8765).
#
# Required env vars (set on the service):
#   RENDER_TS_AUTHKEY   — one-shot Tailscale auth key (ephemeral, 24h)
#   CENA_GATEWAY_URL    — http://100.108.119.19:8765
#   CENA_GATEWAY_TOKEN  — shared X-Cena-Token (matches cena_token.txt on AiCk)
#   PORT                — injected by Render
#
# Build dependency: the static tailscale binaries are downloaded into
# /opt/render/project/src/bin during the build phase (see buildCommand).
# install.sh is NOT usable on Render's build env because it requires sudo.

set -euo pipefail

TS_BIN="/opt/render/project/src/bin"
TS_SOCK="/tmp/tailscaled.sock"
TS_STATE="/tmp/tailscaled.state"

# 1) tailscaled in userspace networking mode. Render containers don't
#    expose /dev/net/tun, so userspace stack is mandatory. --socks5-server
#    on localhost:1055 lets in-process clients (httpx, requests) reach
#    tailnet IPs via the SOCKS5 proxy — required because userspace mode
#    doesn't intercept OS syscalls.
"$TS_BIN/tailscaled" \
    --tun=userspace-networking \
    --socks5-server=localhost:1055 \
    --socket="$TS_SOCK" \
    --state="$TS_STATE" \
    >/tmp/tailscaled.log 2>&1 &

# 2) Wait for the daemon to come up (up to ~10s).
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if "$TS_BIN/tailscale" --socket="$TS_SOCK" status >/dev/null 2>&1; then break; fi
    sleep 1
done

# 3) Join the tailnet. Ephemeral node — auto-removed when the container exits.
#    NON-FATAL on failure (2026-05-16 prod incident): a stale/expired
#    RENDER_TS_AUTHKEY took the entire app down because the original
#    `tailscale up` was a hard gate under `set -e`. Cena tunnel breaks
#    without tailnet, but the rest of the app (driver portal, ez-market,
#    /partner/*) must keep serving. If you need tailnet again, rotate
#    the key in Tailscale admin + restart.
if ! "$TS_BIN/tailscale" --socket="$TS_SOCK" up \
    --authkey="${RENDER_TS_AUTHKEY:-MISSING}" \
    --hostname="cenas-ezlive" \
    --accept-routes; then
    echo "WARN: tailscale up failed (auth key invalid or expired?); continuing without tailnet — Cena gateway path will be degraded" >&2
fi

# Optional sanity log — visible in Render's deploy logs.
"$TS_BIN/tailscale" --socket="$TS_SOCK" ip -4 || true

# 4) Exec the Flask app under gunicorn. Threaded workers keep the app
#    responsive when /sam/chat SSE streams pin long-lived requests.
WEB_WORKERS="${WEB_CONCURRENCY:-2}"
WEB_THREADS="${GUNICORN_THREADS:-4}"
exec gunicorn wsgi:app \
    --bind "0.0.0.0:${PORT}" \
    --worker-class gthread \
    --workers "${WEB_WORKERS}" \
    --threads "${WEB_THREADS}" \
    --timeout 300
