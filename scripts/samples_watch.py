"""Poll the samples-approval-events endpoint and emit state-change lines for dck.

Built by dck per cena #2682 design call (option-b: dedicated polling endpoint
keeps internal pipeline signals out of operational /partner/notifications).

Consumes (per ck #2736 scope + cena #2738 naming + cena #2740 cursor pattern):
    GET https://app.cenaskitchen.com/partner/developer/samples/approval-events?since=<iso8601>

Response shape:
    {"now": "2026-05-18T11:05:00Z",
     "events": [
        {"slug": "drivers-redesign-v2",
         "title": "Drivers Page Redesign",
         "status": "approved"|"rejected"|"pending",
         "notes": "..." or null,
         "marked_by_user_id": 1,
         "marked_at": "2026-05-18T10:00:00Z",
         "attachments": [{"id": int, "filename": str, "url": str}, ...]},
        ...
     ]}

CURSOR DISCIPLINE (cena #2740 race-window-close pattern):
    Store response.now as next-poll cursor. NOT items[-1].marked_at.
    Server-clock is source-of-truth, no skew math needed. Any approval
    landing during serialization gets picked up on the next poll.

EVENT-TYPE INFERENCE (consumer-side per ck #2736):
    status='approved'                  -> APPROVE
    status='rejected', 0 attachments   -> REJECT (text-only)
    status='rejected', N>0 attachments -> REJECT_WITH_IMAGE
    status='pending' (rare, undo flip) -> REVERT_TO_PENDING
    (notes-only edits show up as same status with newer marked_at — dck
     can suppress these in display if noisy, but they're real events)

EMITTED LINE FORMAT (one per event, consumable by Monitor loop):
    [marked_at] slug "title" STATUS_LABEL (user_id=N): notes  [+N imgs]

State: tracks server-side `now` cursor in ~/.dck-samples-watch-now so
reconnects don't replay history (and don't miss events landing during
serialization per the race-window pattern).

Auth: same dual-tier cookie chain as chat_tail.py (EZLIVE_PASSWORD +
PARTNER_PASSWORD).

Usage:
    python scripts/samples_watch.py                   # poll every 30s
    python scripts/samples_watch.py --interval 10
    python scripts/samples_watch.py --once
    python scripts/samples_watch.py --since 1970-01-01T00:00:00Z   # replay all
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
ENDPOINT = "/partner/developer/samples/approval-events"
COOKIE_FILE = Path.home() / ".cenas-chat-cookies"
STATE_FILE = Path.home() / ".dck-samples-watch-now"
EPOCH = "1970-01-01T00:00:00Z"


def _read_partner_secret() -> str | None:
    try:
        p = Path.home() / ".openclaw" / ".secrets" / "partner_password.txt"
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return None


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
        ("User-Agent", "dck-samples-watch/1.0"),
        ("Accept", "application/json"),
    ]
    op._jar = jar
    return op


def _save(opener):
    try:
        opener._jar.save(ignore_discard=True)
    except Exception:
        pass


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
    _save(opener)


def _fetch_events(opener, since_ts: str, site_pw: str, partner_pw: str) -> dict:
    url = f"{BASE}{ENDPOINT}?since={urllib.parse.quote(since_ts)}"
    req = urllib.request.Request(url)

    def _try_once():
        with opener.open(req, timeout=20) as r:
            return json.loads(r.read())

    try:
        return _try_once()
    except urllib.error.HTTPError as e:
        if e.code in (401, 302):
            _login(opener, site_pw, partner_pw)
            return _try_once()
        if e.code == 404:
            print(
                f"endpoint 404 — {ENDPOINT} not yet shipped (awaiting ck per cena #2682). Exiting.",
                file=sys.stderr,
            )
            sys.exit(3)
        raise
    except (json.JSONDecodeError, ValueError):
        _login(opener, site_pw, partner_pw)
        return _try_once()


def _load_cursor() -> str:
    try:
        return STATE_FILE.read_text(encoding="utf-8").strip() or EPOCH
    except Exception:
        return EPOCH


def _save_cursor(now_ts: str) -> None:
    try:
        STATE_FILE.write_text(now_ts, encoding="utf-8")
    except Exception:
        pass


def _classify(status: str, num_attachments: int) -> str:
    """Derive event type from current state per ck #2736 inference spec."""
    if status == "approved":
        return "APPROVE"
    if status == "rejected":
        return "REJECT_WITH_IMAGE" if num_attachments > 0 else "REJECT"
    if status == "pending":
        return "REVERT_TO_PENDING"
    return f"UNKNOWN({status})"


def _emit(event: dict) -> None:
    marked_at = event.get("marked_at", "")
    slug = event.get("slug", "?")
    title = event.get("title", slug)
    status = event.get("status", "?")
    notes = (event.get("notes") or "").replace("\n", " ").replace("\r", " ")
    actor = event.get("marked_by_user_id", "?")
    atts = event.get("attachments") or []
    label = _classify(status, len(atts))
    suffix = f": {notes}" if notes else ""
    atts_suffix = f"  [+{len(atts)} imgs]" if atts else ""
    print(f"[{marked_at}] {slug} \"{title}\" {label} (user_id={actor}){suffix}{atts_suffix}",
          flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--site-password",
        default=os.getenv("EZLIVE_PASSWORD", "cenas"),
    )
    ap.add_argument(
        "--partner-password",
        default=(
            os.getenv("PARTNER_PASSWORD")
            or _read_partner_secret()
            or "Cenas7234"
        ),
    )
    ap.add_argument("--interval", type=float, default=30.0,
                    help="poll seconds (default 30)")
    ap.add_argument("--since", default=None,
                    help="override cursor (iso8601). Default: state file.")
    ap.add_argument("--once", action="store_true", help="one fetch then exit")
    args = ap.parse_args()

    opener = _opener()
    _login(opener, args.site_password, args.partner_password)

    cursor = args.since if args.since is not None else _load_cursor()

    while True:
        try:
            d = _fetch_events(opener, cursor, args.site_password,
                              args.partner_password)
            events = d.get("events") or []
            # Cena #2740: cursor on server-side `now`, not items[-1].marked_at,
            # to close the race-window of items landing during serialization.
            next_cursor = d.get("now") or cursor
            for ev in events:
                _emit(ev)
            if events or next_cursor != cursor:
                cursor = next_cursor
                _save_cursor(cursor)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {e}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
