"""dck's read-only view into /sam/chat — the Sam<>Cena conversation
surface. Mirrors the chat_tail.py shape but reads from the Sam Chat
table via /sam/cena/sam-chat (X-Cena-Token gated, observer-only by
Sam #1906 + cena #1907 Track 8 spec).

Usage:
    python scripts/read_sam_chat.py                  # poll every 15s, print new messages
    python scripts/read_sam_chat.py --once           # one fetch, exit
    python scripts/read_sam_chat.py --since "2026-05-17T22:00:00Z"
    python scripts/read_sam_chat.py --session-id 42  # restrict to one session
    python scripts/read_sam_chat.py --limit 50

Auth: needs CENA_GATEWAY_TOKEN — read order:
    1. --token CLI arg
    2. CENA_GATEWAY_TOKEN env var
    3. ~/.openclaw/.secrets/cena_token.txt (legacy path)
    4. C:/Users/sam/cena/cena_token.txt (post-Track-4 migration path)

dck reads continuously; she does NOT post to /sam/chat. Posting is
gated to the Sam-summon path (cena #1907 spec) which lands separately.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = "https://app.cenaskitchen.com"
ENDPOINT = "/sam/cena/sam-chat"


def _read_token_secret() -> str | None:
    for p in (Path.home() / ".openclaw" / ".secrets" / "cena_token.txt",
              Path(r"C:\Users\sam\cena\cena_token.txt")):
        try:
            if p.exists():
                return p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return None


def _fetch(token: str, params: dict) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items()
                                 if v is not None})
    url = f"{BASE}{ENDPOINT}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Cena-Token", token)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _emit(m: dict) -> None:
    """Format-aligned with read_dev_chat (#2031 post-fix shape):
        [#XXXX YYYY-MM-DDTHH:MM:SSZ role(model)] content
    Per samai #2199 (b). The #sc prefix distinguishes sam-chat
    row ids from dev-chat row ids when both surfaces appear in
    a single tail."""
    mid = m.get("id", "?")
    ts = m.get("created_at", "?")
    role = m.get("role", "?")
    model = (f"({m['model']})" if m.get("model") else "")
    body = (m.get("content") or "").replace(
        "\r\n", " ").replace("\r", " ").replace("\n", " ")
    print(f"[#sc{mid} {ts} {role}{model}] {body}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token",
                    default=(os.getenv("CENA_GATEWAY_TOKEN")
                             or _read_token_secret() or ""))
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--since", default=None,
                    help="ISO datetime (default: last 24h via server)")
    ap.add_argument("--include-all", action="store_true",
                    help="ignore the default-window filter")
    ap.add_argument("--session-id", type=int, default=None)
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if not args.token:
        print("FATAL: CENA_GATEWAY_TOKEN not configured "
              "(env var or ~/.openclaw/.secrets/cena_token.txt or "
              "C:/Users/sam/cena/cena_token.txt)",
              file=sys.stderr)
        return 2

    last_id = 0
    while True:
        try:
            data = _fetch(args.token, {
                "limit": args.limit,
                "since": args.since,
                "include_all": ("true" if args.include_all else None),
                "session_id": args.session_id,
            })
            for m in data.get("messages") or []:
                mid = int(m.get("id") or 0)
                if mid <= last_id:
                    continue
                _emit(m)
                if mid > last_id:
                    last_id = mid
        except urllib.error.HTTPError as e:
            print(f"ERROR: HTTP {e.code} {e.reason}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {e}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
