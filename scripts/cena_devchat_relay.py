"""Cena Dev-Chat Relay — every-minute one-shot.

ck build-order #4 (2026-05-19, Sam dev chat #6:28 + #7:01 green-light).
Mechanical relay: fetch new messages from the Developer Chat REST endpoint
and append each one as a JSONL line to data/cena/cena_devchat_inbox.jsonl.
sam_chat.py reads the latest N entries from that file on each Cena turn
and threads them as a system note so Cena always has fresh dev-chat
context.

Designed to run as a Windows Scheduled Task on aick's box every 1 minute.
Stateless aside from a single last-seen-id integer in
%USERPROFILE%\\.openclaw\\.state\\cena_devchat_lastid.txt.

Idempotent + retry-safe: a 502 swap window costs at most one missed
poll; the next fire picks up where it left off via since_id.

Usage:
    python scripts/cena_devchat_relay.py
    python scripts/cena_devchat_relay.py --once       # alias of default
    python scripts/cena_devchat_relay.py --dry-run    # fetch, don't write
    python scripts/cena_devchat_relay.py --reset      # clear state to 0

Auth:
    EZLIVE_PASSWORD       (default 'cenas')
    PARTNER_PASSWORD env  OR ~/.openclaw/.secrets/partner_password.txt
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import LWPCookieJar
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cena_devchat_relay")

BASE = "https://app.cenaskitchen.com"
COOKIE_FILE = Path.home() / ".cenas-chat-cookies-cena-relay"
STATE_FILE = Path.home() / ".openclaw" / ".state" / "cena_devchat_lastid.txt"

# Repo-relative inbox path. The script resolves it against the repo root
# walked up from its own location: <repo>/scripts/cena_devchat_relay.py
# → <repo>/data/cena/cena_devchat_inbox.jsonl.
INBOX_FILE = (Path(__file__).resolve().parent.parent
              / "data" / "cena" / "cena_devchat_inbox.jsonl")

# Cap the inbox at this many lines to avoid unbounded growth. Cena reads
# only the latest N anyway; older entries can be pruned to a rotated file
# later if the team wants longer history.
INBOX_MAX_LINES = 5000


def _opener() -> urllib.request.OpenerDirector:
    jar = LWPCookieJar(str(COOKIE_FILE))
    if COOKIE_FILE.exists():
        try:
            jar.load(ignore_discard=True)
        except Exception:
            pass
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [
        ("User-Agent", "cena-devchat-relay/1.0"),
        ("Accept", "application/json, text/html"),
    ]
    op._jar = jar  # type: ignore[attr-defined]
    return op


def _save(op: urllib.request.OpenerDirector) -> None:
    try:
        op._jar.save(ignore_discard=True)  # type: ignore[attr-defined]
    except Exception:
        pass


def _load_partner_password() -> str:
    val = os.environ.get("PARTNER_PASSWORD")
    if val:
        return val.strip()
    secret = Path.home() / ".openclaw" / ".secrets" / "partner_password.txt"
    if secret.exists():
        return secret.read_text(encoding="utf-8").strip()
    raise SystemExit(
        "ERROR: PARTNER_PASSWORD not set and "
        "~/.openclaw/.secrets/partner_password.txt missing")


def _login(op: urllib.request.OpenerDirector,
           site_pw: str, partner_pw: str) -> None:
    for endpoint, pw, label in (
        ("/login", site_pw, "site"),
        ("/partner-login", partner_pw, "partner"),
    ):
        data = urllib.parse.urlencode({"password": pw}).encode()
        req = urllib.request.Request(f"{BASE}{endpoint}", data=data)
        try:
            op.open(req).read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise SystemExit(f"ERROR: {label} login failed (401)")
            if e.code != 302:
                raise
    _save(op)


def _fetch(op: urllib.request.OpenerDirector, since_id: int,
           site_pw: str, partner_pw: str) -> dict:
    url = f"{BASE}/partner/developer/chat/messages.json?since_id={since_id}"
    req = urllib.request.Request(url)

    def _go():
        with op.open(req, timeout=20) as r:
            return json.loads(r.read())

    try:
        return _go()
    except urllib.error.HTTPError as e:
        if e.code in (401, 302):
            _login(op, site_pw, partner_pw)
            return _go()
        raise
    except (json.JSONDecodeError, ValueError):
        # urllib auto-followed a 302 to the HTML login page.
        _login(op, site_pw, partner_pw)
        return _go()


def _read_state() -> int:
    if not STATE_FILE.exists():
        return 0
    try:
        return int(STATE_FILE.read_text(encoding="utf-8").strip() or "0")
    except (ValueError, OSError):
        return 0


def _write_state(last_id: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(str(last_id), encoding="utf-8")


def _append_inbox(rows: list[dict]) -> None:
    if not rows:
        return
    INBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    with INBOX_FILE.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # Optional cap: trim to the most-recent INBOX_MAX_LINES lines.
    try:
        if INBOX_FILE.stat().st_size > 0:
            lines = INBOX_FILE.read_text(encoding="utf-8").splitlines()
            if len(lines) > INBOX_MAX_LINES:
                INBOX_FILE.write_text(
                    "\n".join(lines[-INBOX_MAX_LINES:]) + "\n",
                    encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-password",
                    default=os.environ.get("EZLIVE_PASSWORD", "cenas"))
    ap.add_argument("--partner-password", default=None)
    ap.add_argument("--once", action="store_true",
                    help="(alias of default — kept for symmetry with chat_tail)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch + count, do not write inbox or state")
    ap.add_argument("--reset", action="store_true",
                    help="Clear state to 0 (next run re-pulls everything)")
    args = ap.parse_args()

    if args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        logger.info("state reset")
        return 0

    partner_pw = args.partner_password or _load_partner_password()
    op = _opener()
    since_id = _read_state()
    logger.info("polling /partner/developer/chat/messages.json since_id=%d",
                since_id)

    try:
        resp = _fetch(op, since_id, args.site_password, partner_pw)
    except Exception as e:
        logger.error("fetch failed: %s", e)
        return 2

    msgs = resp.get("messages") or []
    new_max = since_id
    rows: list[dict] = []
    for m in msgs:
        mid = int(m.get("id") or 0)
        if mid <= since_id:
            continue
        rows.append({
            "id": mid,
            "author": m.get("author") or "",
            "body": m.get("body") or "",
            "created_at": m.get("created_at") or "",
        })
        if mid > new_max:
            new_max = mid

    logger.info("fetched %d total / %d new", len(msgs), len(rows))

    if args.dry_run:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return 0

    if rows:
        _append_inbox(rows)
        _write_state(new_max)
        logger.info("appended %d to %s; state -> %d",
                    len(rows), INBOX_FILE, new_max)
    else:
        logger.info("no new messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
