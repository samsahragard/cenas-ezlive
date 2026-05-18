"""Scan orders@cenaskitchen.com IMAP inbox for vendor emails (Sam #837
items 9-12 + Sam /sam/chat #871).

Per Sam #871 "there is already emails in there. look at them" — instead
of waiting on Sam to forward samples, this script connects to the same
IMAP inbox produce_ingest.py polls, scans the last N messages, and
returns sample subjects + truncated bodies grouped by sender domain.

Output: JSON dump with {sender_domains: [{domain, count, recent: [{from,
subject, date, body_preview}]}], scanned: N}. Used as the input for
writing per-vendor body parsers (each vendor's emails look different
so we need real shapes, not guesses).

Runs via /sam/cena/run-scan-vendor-inbox trigger so it has access to
ORDERS_EMAIL_PWD env on Render.
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import sys
from collections import defaultdict
from email.utils import parseaddr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Reuse the produce_ingest env vars + password resolver so this matches
# the working IMAP config exactly.
from app.services.produce_ingest import (  # noqa: E402
    IMAP_HOST, IMAP_PORT, IMAP_USER, _email_pwd, _decode_h,
)


VENDOR_DOMAIN_HINTS = {
    "webstaurant":       ("webstaurantstore.com", "webstaurant.com"),
    "performance-food":  ("performancefoodgroup.com", "performancefoodservice.com",
                          "pfgc.com", "performancefood.com"),
    "restaurant-depot":  ("restaurantdepot.com", "rd-online.com", "jetro.com"),
    "specs":             ("specsfoodservice.com", "specsonline.com", "specs.com"),
}


def _classify_domain(addr: str) -> str | None:
    """Return vendor slug if the address matches any known vendor hint."""
    a = (addr or "").lower()
    for slug, hints in VENDOR_DOMAIN_HINTS.items():
        for h in hints:
            if h in a:
                return slug
    return None


def _body_preview(msg, max_chars: int = 600) -> str:
    """Pull a plain-text preview of the email body. Tries text/plain
    first, falls back to text/html stripped to text-ish."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                try:
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode("utf-8", errors="replace")[:max_chars]
                except Exception:
                    continue
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                try:
                    payload = part.get_payload(decode=True) or b""
                    raw = payload.decode("utf-8", errors="replace")
                    # crude tag strip just for preview
                    import re
                    stripped = re.sub(r"<[^>]+>", " ", raw)
                    stripped = re.sub(r"\s+", " ", stripped).strip()
                    return f"[HTML stripped] {stripped[:max_chars]}"
                except Exception:
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def main() -> int:
    scan_limit = int(os.getenv("CENA_INBOX_SCAN_LIMIT", "300"))
    pwd = _email_pwd()
    if not pwd:
        print(json.dumps({"ok": False,
                          "error": "ORDERS_EMAIL_PWD not set on this env"}))
        return 1

    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        M.login(IMAP_USER, pwd)
        M.select("INBOX", readonly=True)
        typ, data = M.search(None, "ALL")
        if typ != "OK":
            print(json.dumps({"ok": False, "error": "imap search failed"}))
            return 1
        ids = data[0].split()
        if not ids:
            print(json.dumps({"ok": True, "scanned": 0,
                              "vendor_matches": {}, "all_domains": {}}))
            return 0

        recent_ids = ids[-scan_limit:]

        all_domain_counts: dict[str, int] = defaultdict(int)
        vendor_matches: dict[str, list[dict]] = {k: [] for k in VENDOR_DOMAIN_HINTS}

        for nid in recent_ids:
            typ, hdr = M.fetch(nid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK":
                continue
            hdr_text = hdr[0][1].decode("utf-8", errors="replace") if hdr and hdr[0] else ""
            msg_hdr = email.message_from_string(hdr_text)
            from_raw = _decode_h(msg_hdr.get("From", ""))
            name, addr = parseaddr(from_raw)
            domain = addr.rsplit("@", 1)[-1].lower() if addr and "@" in addr else "(unknown)"
            all_domain_counts[domain] += 1

            slug = _classify_domain(addr)
            if slug and len(vendor_matches[slug]) < 5:
                subject = _decode_h(msg_hdr.get("Subject", ""))
                date_h = _decode_h(msg_hdr.get("Date", ""))
                typ2, full = M.fetch(nid, "(RFC822)")
                preview = ""
                if typ2 == "OK" and full and full[0]:
                    try:
                        full_msg = email.message_from_bytes(full[0][1])
                        preview = _body_preview(full_msg)
                    except Exception as e:  # noqa: BLE001
                        preview = f"[body fetch error: {e}]"
                vendor_matches[slug].append({
                    "mid": nid.decode() if isinstance(nid, bytes) else str(nid),
                    "from": from_raw,
                    "addr": addr,
                    "subject": subject,
                    "date": date_h,
                    "body_preview": preview,
                })

        top_domains = sorted(all_domain_counts.items(), key=lambda kv: -kv[1])[:25]
        print(json.dumps({
            "ok": True,
            "scanned": len(recent_ids),
            "all_domains_top25": [{"domain": d, "count": c} for d, c in top_domains],
            "vendor_matches": {k: v for k, v in vendor_matches.items() if v},
            "vendor_no_matches": [k for k, v in vendor_matches.items() if not v],
            "hint_domains_searched": VENDOR_DOMAIN_HINTS,
        }, indent=2, default=str))
        return 0
    finally:
        try: M.close()
        except Exception: pass
        try: M.logout()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main())
