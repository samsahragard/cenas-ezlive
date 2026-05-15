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
#    expose /dev/net/tun, so userspace stack is mandatory.
"$TS_BIN/tailscaled" \
    --tun=userspace-networking \
    --socket="$TS_SOCK" \
    --state="$TS_STATE" \
    >/tmp/tailscaled.log 2>&1 &

# 2) Wait for the daemon to come up (up to ~10s).
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if "$TS_BIN/tailscale" --socket="$TS_SOCK" status >/dev/null 2>&1; then break; fi
    sleep 1
done

# 3) Join the tailnet. Ephemeral node — auto-removed when the container exits.
"$TS_BIN/tailscale" --socket="$TS_SOCK" up \
    --authkey="${RENDER_TS_AUTHKEY:?RENDER_TS_AUTHKEY not set}" \
    --hostname="cenas-ezlive" \
    --accept-routes

# Optional sanity log — visible in Render's deploy logs.
"$TS_BIN/tailscale" --socket="$TS_SOCK" ip -4 || true

# 4) Exec the Flask app under gunicorn. 2 workers + 120s timeout sized for
#    the /sam/chat SSE streams (each stream pins a worker until done).
exec gunicorn wsgi:app \
    --bind "0.0.0.0:${PORT}" \
    --workers 2 \
    --timeout 120
