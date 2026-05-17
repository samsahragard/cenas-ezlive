"""dck's continuous chat-tail Monitor wrapper.

Reuses scripts/chat_tail.py's auth/fetch/emit plumbing, but fixes the two
things that make a bare `python chat_tail.py` unsuitable for a long-lived
Monitor:

  1. chat_tail.py hardcodes last_id = 0 — a cold start replays the ENTIRE
     backlog (~1400 messages), which floods the Monitor and trips its
     too-many-events auto-stop. This wrapper starts from a --since id (or a
     persisted state file) so it only ever surfaces genuinely-new messages.
  2. On an SSH-level drop the outer command exits; the caller wraps this in
     a reconnect loop. The state file (~/.dck-chat-monitor-lastid) means a
     reconnect RESUMES from the last-seen id instead of re-replaying.

Sibling of scripts/samai_chat_monitor.py — same shape, different state
file name (so dck and samai don't collide on a shared filesystem if
their home dirs ever overlap).

Does NOT modify chat_tail.py (ck owns that file). chat_tail.py's _emit is a
nested function inside main() — NOT importable — so this wrapper inlines an
equivalent _emit() below (same single-line / newline-collapse / chunking
behaviour, including ck's 26dde0d newline-collapse fix). If chat_tail's
_emit ever changes, mirror the change here.

Usage: python scripts/dck_chat_monitor.py [--since N] [--interval S]
Run under the Monitor tool, wrapped in an ssh-reconnect loop. Each new chat
message prints one (chunked) single-line event to stdout.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat_tail  # noqa: E402

_STATE = Path.home() / ".dck-chat-monitor-lastid"

_CHUNK_SIZE = 460
_NO_CHUNK = os.getenv("CHAT_TAIL_NO_CHUNK") == "1"


def _emit(t: str, a: str, body: str, suffix: str) -> None:
    """One chat message -> one or more single-line stdout events.

    Inlined equivalent of chat_tail.main()'s nested _emit (which is not
    importable). Collapses embedded CR/LF to spaces so a multi-line body
    stays one line per event — a line-anchored Monitor filter otherwise
    keeps only the first line of each chunk and silently drops the rest.
    Bodies over _CHUNK_SIZE split into "(cont N/M)" events; an attachment
    suffix rides the last chunk.
    """
    body = body.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    if not body:
        print(f"[{t}] {a}:{suffix.lstrip()}", flush=True)
        return
    if _NO_CHUNK or (len(body) + len(suffix) <= _CHUNK_SIZE):
        print(f"[{t}] {a}: {body}{suffix}", flush=True)
        return
    chunks: list[str] = []
    i = 0
    while i < len(body):
        chunks.append(body[i:i + _CHUNK_SIZE])
        i += _CHUNK_SIZE
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        tail = suffix if idx == total else ""
        print(f"[{t}] {a} (cont {idx}/{total}): {chunk}{tail}", flush=True)


def _load_last(default: int) -> int:
    try:
        return max(int(_STATE.read_text(encoding="utf-8").strip()), default)
    except Exception:
        return default


def _save_last(n: int) -> None:
    try:
        _STATE.write_text(str(n), encoding="utf-8")
    except Exception as e:
        print(f"[monitor] _save_last failed (id={n}): {e}",
              file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=0,
                    help="initial since_id when no state file exists")
    ap.add_argument("--interval", type=float, default=5.0, help="poll seconds")
    args = ap.parse_args()

    site_pw = os.getenv("EZLIVE_PASSWORD", "cenas")
    partner_pw = (os.getenv("PARTNER_PASSWORD")
                  or chat_tail._read_partner_secret() or "Cenas7234")

    opener = chat_tail._opener()
    chat_tail._login(opener, site_pw, partner_pw)

    last_id = _load_last(args.since)
    print(f"[monitor] dck chat-tail live - tailing from #{last_id} "
          f"(poll {args.interval}s)", flush=True)

    while True:
        try:
            d = chat_tail._fetch_messages(opener, last_id, site_pw, partner_pw)
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
                mid = m.get("id", 0) or 0
                if mid > last_id:
                    last_id = mid
            _save_last(last_id)
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] transient error: {e}", file=sys.stderr,
                  flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
