"""dck summon responder — auto-acks /sam/chat summons of dck.

Polls /sam/cena/sam-chat for new user turns from Sam. When a turn
mentions 'dck' (word-boundary, case-insensitive), posts a single
auto-ack via /sam/cena/sam-chat-post with role='dck' so Sam sees an
immediate response in the chat UI.

This is the bridge between Track 8b (write surface shipped + samai
PASS) and the full dck-Claude-instance auto-summon. Once dck-Claude
is running her own watcher on her machine, this responder either:
  (a) stays in place as the always-on stub layer (dck-Claude posts
      richer replies on top), or
  (b) gets retired in favor of the dck-Claude full responder.

State: ~/.dck-summon-responder-lastid — last sam-chat row id we
       processed. Cold start without state begins from "now"
       (whatever the most recent row is) so we don't ack the
       backlog.

Auth: dual-path same as scripts/post_sam_chat.py + read_sam_chat.py
      (X-Cena-Token preferred, partner-session fallback).

Usage:
    python scripts/dck_summon_responder.py                 # foreground
    python scripts/dck_summon_responder.py --interval 5    # poll faster
    python scripts/dck_summon_responder.py --once          # one pass

Summon pattern: anything that contains 'dck' as a word — 'dck',
'dck —', '@dck', 'dck?', 'hey dck'. Tightened so 'dock', 'ndck',
etc. don't match. Cena assistant turns ignored (we only react to
role='user' from Sam). Other dck-role turns ignored (no self-loops).
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
READ_ENDPOINT = "/sam/cena/sam-chat"
POST_ENDPOINT = "/sam/cena/sam-chat-post"
STATE_FILE = Path.home() / ".dck-summon-responder-lastid"
COOKIE_FILE = Path.home() / ".cenas-chat-cookies"

# Word-boundary 'dck' — won't match 'dock', 'ndck', etc.
_SUMMON_RE = re.compile(r"\bdck\b", re.IGNORECASE)

_DEFAULT_ACK = (
    "[auto-ack from scripts/dck_summon_responder.py] dck noted the "
    "summon. Live dck-Claude reply will follow when her watcher "
    "wakes. For sync-critical asks, also ping her in dev chat — her "
    "dev-chat-monitor is always on."
)


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


def _opener_with_jar():
    jar = LWPCookieJar(str(COOKIE_FILE))
    if COOKIE_FILE.exists():
        try:
            jar.load(ignore_discard=True)
        except Exception:
            pass
    handler = urllib.request.HTTPCookieProcessor(jar)
    op = urllib.request.build_opener(handler)
    op.addheaders = [("User-Agent", "dck-summon-responder/1.0"),
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


def _fetch_recent(token, opener, jar, site_pw, partner_pw, limit=30):
    params = {"limit": str(limit), "include_all": "true"}
    qs = urllib.parse.urlencode(params)
    url = f"{BASE}{READ_ENDPOINT}?{qs}"
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("X-Cena-Token", token)
    req.add_header("Accept", "application/json")

    def _open():
        if token:
            return urllib.request.urlopen(req, timeout=20)
        return opener.open(req, timeout=20)

    try:
        with _open() as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if not token and e.code in (401, 302, 403):
            _login(opener, site_pw, partner_pw)
            with opener.open(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        else:
            raise
    if not token:
        try:
            jar.save(ignore_discard=True)
        except Exception:
            pass
    return data


def _post_dck(token, opener, jar, site_pw, partner_pw,
              session_id, content):
    body = {"session_id": session_id, "content": content, "role": "dck"}
    data = json.dumps(body).encode("utf-8")
    url = f"{BASE}{POST_ENDPOINT}"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("X-Cena-Token", token)

    def _open():
        if token:
            return urllib.request.urlopen(req, timeout=20)
        return opener.open(req, timeout=20)

    try:
        with _open() as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if not token and e.code in (401, 302, 403):
            _login(opener, site_pw, partner_pw)
            with opener.open(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        raise


def _load_last_id() -> int:
    try:
        return int(STATE_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def _save_last_id(mid: int) -> None:
    try:
        STATE_FILE.write_text(str(mid), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: state save failed: {e}", file=sys.stderr)


def _should_ack(msg) -> bool:
    """True if the msg is a Sam (user) turn that summons dck.

    Skips assistant turns (Cena) AND dck turns (self-loop guard) AND
    system turns. Pattern: 'dck' as a standalone word. Long
    descriptions of dck won't match (the regex looks for whole-word
    'dck'); a turn that just mentions her in passing might trigger
    spuriously, accepted as a stopgap until dck-Claude's richer
    classifier replaces this."""
    if msg.get("role") != "user":
        return False
    body = (msg.get("content") or "").strip()
    if not body:
        return False
    return bool(_SUMMON_RE.search(body))


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
    ap.add_argument("--interval", type=float, default=10.0,
                    help="poll cadence in seconds (default 10)")
    ap.add_argument("--once", action="store_true",
                    help="single poll then exit (for testing)")
    ap.add_argument("--ack", default=_DEFAULT_ACK,
                    help="template ack body to post when a summon "
                         "is detected")
    ap.add_argument("--cold-start-from-now", action="store_true",
                    default=True,
                    help="ignore state file on first run; treat all "
                         "existing rows as already seen (default on)")
    args = ap.parse_args()

    token = args.token or ""
    opener = jar = None
    if not token:
        opener, jar = _opener_with_jar()

    last_id = _load_last_id()
    if last_id == 0 and args.cold_start_from_now:
        # On cold start, snap forward to the most recent row id so we
        # don't ack the backlog (every prior "dck" mention would
        # otherwise fire).
        try:
            data = _fetch_recent(token, opener, jar,
                                 args.site_password,
                                 args.partner_password, limit=1)
            msgs = data.get("messages") or []
            if msgs:
                last_id = int(msgs[-1]["id"])
                _save_last_id(last_id)
                print(f"cold-start snapped last_id to {last_id}",
                      flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"WARN: cold-start probe failed: {e}", file=sys.stderr)

    while True:
        try:
            data = _fetch_recent(token, opener, jar,
                                 args.site_password,
                                 args.partner_password, limit=30)
            for m in data.get("messages") or []:
                mid = int(m.get("id") or 0)
                if mid <= last_id:
                    continue
                if _should_ack(m):
                    sid = m.get("session_id")
                    if sid is None:
                        last_id = max(last_id, mid)
                        continue
                    try:
                        resp = _post_dck(token, opener, jar,
                                         args.site_password,
                                         args.partner_password,
                                         int(sid), args.ack)
                        print(f"acked summon: src #{mid} -> "
                              f"new dck row #{resp.get('id')} "
                              f"in session {sid}", flush=True)
                    except Exception as e:  # noqa: BLE001
                        print(f"WARN: ack post failed for #{mid}: {e}",
                              file=sys.stderr)
                last_id = max(last_id, mid)
            _save_last_id(last_id)
        except urllib.error.HTTPError as e:
            print(f"ERROR: HTTP {e.code} {e.reason}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {e}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
