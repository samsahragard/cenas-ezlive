"""Tail the Developer Chat at app.cenaskitchen.com from the command line.

Usage:
    python chat_tail.py                       # poll every 5s, print new messages
    python chat_tail.py --post "hello sam"    # post one message as 'aick-claude'
    python chat_tail.py --author ck-claude --post "..."   # post as a different name
    python chat_tail.py --interval 10         # change poll interval

Auth: needs both the EZLIVE_PASSWORD (defaults to 'cenas') and the
PARTNER_PASSWORD (Cenas7234, or whatever PARTNER_PASSWORD env var is set to).
Override with --site-password / --partner-password if needed. The script
caches a session cookie at ~/.cenas-chat-cookies and re-uses it across runs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
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
COOKIE_FILE = Path.home() / ".cenas-chat-cookies"


def _opener():
    jar = LWPCookieJar(str(COOKIE_FILE))
    if COOKIE_FILE.exists():
        try:
            jar.load(ignore_discard=True)
        except Exception:
            pass
    handler = urllib.request.HTTPCookieProcessor(jar)
    op = urllib.request.build_opener(handler)
    op.addheaders = [
        ("User-Agent", "cenas-chat-tail/1.0"),
        ("Accept", "application/json, text/html"),
    ]
    op._jar = jar  # stash so we can save later
    return op


def _save(opener):
    try:
        opener._jar.save(ignore_discard=True)
    except Exception:
        pass


def _login(opener, site_pw: str, partner_pw: str) -> None:
    """Log in to the site (Tier 1) AND through the Partner gate (Tier 2)."""
    # Tier 1
    data = urllib.parse.urlencode({"password": site_pw}).encode()
    req = urllib.request.Request(f"{BASE}/login", data=data)
    try:
        opener.open(req).read()
    except urllib.error.HTTPError as e:
        if e.code != 401: raise
        print("ERROR: site login failed (wrong EZLIVE_PASSWORD)", file=sys.stderr); sys.exit(2)
    # Tier 2
    data = urllib.parse.urlencode({"password": partner_pw}).encode()
    req = urllib.request.Request(f"{BASE}/partner-login", data=data)
    try:
        opener.open(req).read()
    except urllib.error.HTTPError as e:
        if e.code != 401: raise
        print("ERROR: partner login failed (wrong PARTNER_PASSWORD)", file=sys.stderr); sys.exit(2)
    _save(opener)


def _fetch_messages(opener, since_id: int, site_pw: str, partner_pw: str) -> dict:
    url = f"{BASE}/partner/developer/chat/messages.json?since_id={since_id}"
    req = urllib.request.Request(url)

    def _try_once():
        with opener.open(req, timeout=20) as r:
            body = r.read()
        return json.loads(body)

    try:
        return _try_once()
    except urllib.error.HTTPError as e:
        if e.code in (401, 302):
            _login(opener, site_pw, partner_pw)
            return _try_once()
        raise
    except (json.JSONDecodeError, ValueError):
        # urllib auto-followed a 302 to the HTML login page after the session
        # expired (common right after a Render redeploy). Re-auth and retry.
        _login(opener, site_pw, partner_pw)
        return _try_once()


def _post(opener, author: str, body: str, site_pw: str, partner_pw: str) -> None:
    payload = urllib.parse.urlencode({"author": author, "body": body}).encode()
    url = f"{BASE}/partner/developer/chat/post"
    req = urllib.request.Request(url, data=payload)
    try:
        opener.open(req).read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 302):
            _login(opener, site_pw, partner_pw)
            opener.open(req).read()
        else:
            raise
    _save(opener)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-password", default=os.getenv("EZLIVE_PASSWORD", "cenas"))
    ap.add_argument("--partner-password", default=os.getenv("PARTNER_PASSWORD") or _read_partner_secret() or "Cenas7234")
    ap.add_argument("--interval", type=float, default=5.0, help="poll seconds")
    ap.add_argument("--author", default="aick-claude", help="who to post as (--post)")
    ap.add_argument("--post", help="post a single message and exit (no polling)")
    ap.add_argument("--once", action="store_true", help="fetch new messages once, then exit")
    args = ap.parse_args()

    opener = _opener()
    # Always re-auth at startup. Login endpoints are idempotent (just re-set
    # the session cookie); the cookie cache below avoids spurious failures
    # when the server-side session was reset by a deploy or the cookie file
    # is older than the session expiry.
    _login(opener, args.site_password, args.partner_password)
    if args.post is not None:
        _post(opener, args.author, args.post, args.site_password, args.partner_password)
        print(f"posted as {args.author}: {args.post[:80]}")
        return 0

    # Chunk threshold: harness display layers tend to truncate long stdout
    # lines at ~600 chars when they relay events into the agent context.
    # Splitting long bodies into multiple "[ts] author (cont N/M): ..."
    # lines keeps each event under the cap so nothing silently drops.
    # Set CHAT_TAIL_NO_CHUNK=1 to disable. (Sam, 2026-05-13.)
    CHUNK_SIZE = 460
    NO_CHUNK = os.getenv("CHAT_TAIL_NO_CHUNK") == "1"

    def _emit(t: str, a: str, body: str, suffix: str) -> None:
        # Collapse embedded newlines/CRs to spaces: a multi-line body
        # splits into multiple terminal lines, and a line-anchored
        # Monitor filter (^[|ERROR) then keeps only the first line of
        # each chunk — silently dropping the rest. Keep every emitted
        # event single-line. (ck, 2026-05-14)
        body = body.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        if not body:
            # Attachment-only message — one short line.
            print(f"[{t}] {a}:{suffix.lstrip()}", flush=True)
            return
        if NO_CHUNK or (len(body) + len(suffix) <= CHUNK_SIZE):
            print(f"[{t}] {a}: {body}{suffix}", flush=True)
            return
        # Split body into chunks. Each chunk's print line is roughly
        # CHUNK_SIZE chars (the prefix is ~40 chars on top, well under
        # the cap). Suffix (attachments) attaches to the LAST chunk.
        chunks: list[str] = []
        i = 0
        while i < len(body):
            chunks.append(body[i:i + CHUNK_SIZE])
            i += CHUNK_SIZE
        total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            tail = suffix if idx == total else ""
            print(f"[{t}] {a} (cont {idx}/{total}): {chunk}{tail}", flush=True)

    last_id = 0
    while True:
        try:
            d = _fetch_messages(opener, last_id, args.site_password, args.partner_password)
            for m in d.get("messages") or []:
                t = m.get("created_at_display", "")
                a = m.get("author", "?")
                b = (m.get("body") or "").rstrip()
                atts = m.get("attachments") or []
                suffix = ""
                if atts:
                    names = ", ".join(x.get("filename", "?") for x in atts)
                    suffix = f"  [\U0001f4ce {len(atts)}: {names}]"
                _emit(t, a, b, suffix)
                last_id = max(last_id, m.get("id", 0))
            if not d.get("messages"):
                # Quietly skip — only print initial sync message
                pass
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


def _read_partner_secret() -> str | None:
    """Look for a saved Partner password in ~/.openclaw/.secrets/partner_password.txt."""
    try:
        p = Path.home() / ".openclaw" / ".secrets" / "partner_password.txt"
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return None


if __name__ == "__main__":
    raise SystemExit(main())
