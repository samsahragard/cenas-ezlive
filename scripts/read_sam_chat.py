"""dck's read-only view into /sam/chat — the Sam<>Cena conversation
surface. Mirrors the chat_tail.py shape but reads from the Sam Chat
table via /sam/cena/sam-chat (Sam #1906 + cena #1907 Track 8 spec,
observer-only).

Usage:
    python scripts/read_sam_chat.py                  # poll every 15s, print new messages
    python scripts/read_sam_chat.py --once           # one fetch, exit
    python scripts/read_sam_chat.py --since "2026-05-17T22:00:00Z"
    python scripts/read_sam_chat.py --session-id 42  # restrict to one session
    python scripts/read_sam_chat.py --limit 50

Auth — TWO PATHS supported (server-side gate per Sam #2204 accepts
either):
    A. CENA_GATEWAY_TOKEN header (gateway-internal callers + ck-style
       direct script use). Resolution order:
         1. --token CLI arg
         2. CENA_GATEWAY_TOKEN env var
         3. ~/.openclaw/.secrets/cena_token.txt (legacy path)
         4. C:/Users/sam/cena/cena_token.txt (post-Track-4 path)
    B. Partner-session cookie (chat_tail-style EZLIVE+PARTNER login).
       Used as fallback when no token is found. Same shape as
       scripts/chat_tail.py: site_pw + partner_pw resolve via env
       vars / secrets / fallback constants. Lets dck (partner-tier
       observer, no cena gateway token) self-auth using just
       partner_password.txt — no cross-user token copy required.

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
from http.cookiejar import LWPCookieJar
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = "https://app.cenaskitchen.com"
ENDPOINT = "/sam/cena/sam-chat"
COOKIE_FILE = Path.home() / ".cenas-chat-cookies"


def _read_token_secret() -> str | None:
    for p in (Path.home() / ".openclaw" / ".secrets" / "cena_token.txt",
              Path(r"C:\Users\sam\cena\cena_token.txt")):
        try:
            if p.exists():
                return p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return None


def _read_partner_secret() -> str | None:
    try:
        p = Path.home() / ".openclaw" / ".secrets" / "partner_password.txt"
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return None


def _opener_with_jar() -> tuple[urllib.request.OpenerDirector, LWPCookieJar]:
    jar = LWPCookieJar(str(COOKIE_FILE))
    if COOKIE_FILE.exists():
        try:
            jar.load(ignore_discard=True)
        except Exception:
            pass
    handler = urllib.request.HTTPCookieProcessor(jar)
    op = urllib.request.build_opener(handler)
    op.addheaders = [("User-Agent", "read-sam-chat/1.0"),
                     ("Accept", "application/json")]
    return op, jar


def _login(opener, site_pw: str, partner_pw: str) -> None:
    """Site (Tier-1) + partner (Tier-2) login. Same shape as
    chat_tail.py's _login."""
    for path, pw, label in (
        ("/login", site_pw, "site"),
        ("/partner-login", partner_pw, "partner"),
    ):
        data = urllib.parse.urlencode({"password": pw}).encode()
        req = urllib.request.Request(f"{BASE}{path}", data=data)
        try:
            opener.open(req).read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print(f"ERROR: {label} login failed", file=sys.stderr)
                sys.exit(2)
            raise


def _fetch_token(token: str, params: dict) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items()
                                 if v is not None})
    url = f"{BASE}{ENDPOINT}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Cena-Token", token)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch_cookie(opener, jar, site_pw: str, partner_pw: str,
                  params: dict) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items()
                                 if v is not None})
    url = f"{BASE}{ENDPOINT}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, method="GET")
    try:
        with opener.open(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 302, 403):
            _login(opener, site_pw, partner_pw)
            with opener.open(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        else:
            raise
    try:
        jar.save(ignore_discard=True)
    except Exception:
        pass
    return data


def _emit(m: dict) -> None:
    """Format-aligned with read_dev_chat (#2031 post-fix shape):
        [#sc<id> <ts> <role>(<model>)] content
    Per samai #2199 (b). The #sc prefix distinguishes sam-chat row
    ids from dev-chat row ids when both surfaces appear in a single
    tail."""
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
    ap.add_argument("--site-password",
                    default=os.getenv("EZLIVE_PASSWORD", "cenas"))
    ap.add_argument("--partner-password",
                    default=(os.getenv("PARTNER_PASSWORD")
                             or _read_partner_secret() or "Cenas7234"))
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--since", default=None,
                    help="ISO datetime (default: last 24h via server)")
    ap.add_argument("--include-all", action="store_true",
                    help="ignore the default-window filter")
    ap.add_argument("--session-id", type=int, default=None)
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    use_token = bool(args.token)
    if use_token:
        opener = jar = None
    else:
        opener, jar = _opener_with_jar()

    last_id = 0
    while True:
        try:
            params = {
                "limit": args.limit,
                "since": args.since,
                "include_all": ("true" if args.include_all else None),
                "session_id": args.session_id,
            }
            if use_token:
                data = _fetch_token(args.token, params)
            else:
                data = _fetch_cookie(opener, jar, args.site_password,
                                     args.partner_password, params)
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
