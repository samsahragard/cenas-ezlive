"""dck's writer wrapper for /sam/chat — Track 8b per Sam #2236.

Pairs with scripts/read_sam_chat.py (the read tail). dck observes the
Sam<>Cena conversation via the tail, and when SUMMONED by name posts a
reply back through this script. The "only when called" discipline is
agent-side (dck's prompt judges whether a turn addresses her); this
script just hands the WRITE pathway to her.

Usage:
    python scripts/post_sam_chat.py --session-id 42 \\
        --content "yes, that approach matches what we discussed in #2210"

    # Read content from stdin (useful for longer multi-line replies):
    echo "long reply here" | python scripts/post_sam_chat.py \\
        --session-id 42 --stdin

    # Read content from a file:
    python scripts/post_sam_chat.py --session-id 42 \\
        --content-file C:/Users/sam/cena/dck_reply_draft.txt

Auth — same dual-path as read_sam_chat.py (Sam #2204):
    A. CENA_GATEWAY_TOKEN header (gateway-internal + token holders)
    B. Partner-session cookie (fallback for partner-tier observers
       like dck who self-auth via partner_password.txt + site cookie)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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
ENDPOINT = "/sam/cena/sam-chat-post"
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
    op.addheaders = [("User-Agent", "post-sam-chat/1.0"),
                     ("Accept", "application/json")]
    return op, jar


def _login(opener, site_pw: str, partner_pw: str) -> None:
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


def _post_token(token: str, body: dict) -> dict:
    url = f"{BASE}{ENDPOINT}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("X-Cena-Token", token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_cookie(opener, jar, site_pw: str, partner_pw: str,
                 body: dict) -> dict:
    url = f"{BASE}{ENDPOINT}"
    data = json.dumps(body).encode("utf-8")

    def _build_req():
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        return req

    try:
        with opener.open(_build_req(), timeout=20) as r:
            out = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 302, 403):
            _login(opener, site_pw, partner_pw)
            with opener.open(_build_req(), timeout=20) as r:
                out = json.loads(r.read().decode("utf-8"))
        else:
            raise
    try:
        jar.save(ignore_discard=True)
    except Exception:
        pass
    return out


def _resolve_content(args) -> str:
    if args.stdin:
        return sys.stdin.read()
    if args.content_file:
        return Path(args.content_file).read_text(encoding="utf-8")
    if args.content is not None:
        return args.content
    print("ERROR: need --content, --content-file, or --stdin",
          file=sys.stderr)
    sys.exit(2)


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
    ap.add_argument("--session-id", type=int, required=True,
                    help="SamChatSession.id to post into")
    ap.add_argument("--role", default="dck",
                    help="role for the row (default 'dck'; "
                         "server only allows dck via this endpoint)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--content", default=None,
                   help="message body as a single CLI string")
    g.add_argument("--stdin", action="store_true",
                   help="read message body from stdin")
    g.add_argument("--content-file", default=None,
                   help="read message body from a file path")
    args = ap.parse_args()

    content = _resolve_content(args)
    if not content.strip():
        print("ERROR: content is empty after read", file=sys.stderr)
        return 2

    body = {
        "session_id": args.session_id,
        "content": content,
        "role": args.role,
    }

    try:
        if args.token:
            data = _post_token(args.token, body)
        else:
            opener, jar = _opener_with_jar()
            data = _post_cookie(opener, jar, args.site_password,
                                args.partner_password, body)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        print(f"ERROR: HTTP {e.code} {e.reason} {err_body}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not data.get("ok"):
        print(f"ERROR: {data}", file=sys.stderr)
        return 1
    print(json.dumps(data), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
