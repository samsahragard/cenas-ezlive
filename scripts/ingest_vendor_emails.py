"""Scan orders@cenaskitchen.com (and ezcater@ for Tomball Specs) +
parse vendor order emails into VendorRecentOrder rows.

Per Sam #837 items 9-12 + Sam /sam/chat #906 — Sam forwarded all
recent vendor emails to the inbox; this batch-ingests them.

Idempotent on (vendor, source_email_mid): re-runs upsert by mid so
parser fixes can be re-applied without duplicating rows.
"""
from __future__ import annotations

import email
import imaplib
import json
import sys
from collections import defaultdict
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import SessionLocal  # noqa: E402
from app.models import VendorRecentOrder  # noqa: E402
from app.services.produce_ingest import (  # noqa: E402
    IMAP_HOST, IMAP_PORT, IMAP_USER, _email_pwd, _decode_h,
)
from app.services.vendor_order_parser import llm_parse_vendor_order  # noqa: E402

# Re-use the classifier so this stays in sync with the scanner
sys.path.insert(0, str(ROOT / "scripts"))
from scan_vendor_inbox import (  # noqa: E402
    VENDOR_DOMAIN_HINTS, VENDOR_BODY_HINTS,
    _classify_domain, _classify_by_body, _body_preview,
)


def _process_inbox(M, source_inbox_label: str, store_default: str | None = None) -> list[dict]:
    """Walk an open IMAP connection, find vendor matches, parse +
    return list of dicts to upsert."""
    typ, data = M.search(None, "ALL")
    if typ != "OK":
        return []
    ids = data[0].split()
    if not ids:
        return []
    # Last 500 messages is enough for the forwarded backlog Sam sent
    recent = ids[-500:]

    out: list[dict] = []
    for nid in recent:
        typ, hdr = M.fetch(nid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if typ != "OK":
            continue
        hdr_text = hdr[0][1].decode("utf-8", errors="replace") if hdr and hdr[0] else ""
        msg_hdr = email.message_from_string(hdr_text)
        from_raw = _decode_h(msg_hdr.get("From", ""))
        _, addr = parseaddr(from_raw)
        subject = _decode_h(msg_hdr.get("Subject", ""))
        date_h = _decode_h(msg_hdr.get("Date", ""))

        # First classify by sender; fall back to body for forwarded mail
        slugs: set[str] = set()
        d = _classify_domain(addr)
        if d:
            slugs.add(d)
        typ2, full = M.fetch(nid, "(RFC822)")
        body = ""
        if typ2 == "OK" and full and full[0]:
            try:
                full_msg = email.message_from_bytes(full[0][1])
                body = _body_preview(full_msg, max_chars=8000)
            except Exception:
                body = ""
        slugs |= _classify_by_body(subject, body)
        if not slugs:
            continue

        # Parse date for placed_at fallback
        placed_dt = None
        try:
            placed_dt = parsedate_to_datetime(date_h) if date_h else None
            if placed_dt and placed_dt.tzinfo is None:
                placed_dt = placed_dt.replace(tzinfo=None)
            elif placed_dt:
                placed_dt = placed_dt.astimezone().replace(tzinfo=None)
        except Exception:
            placed_dt = None

        for slug in slugs:
            parsed = llm_parse_vendor_order(slug, body) or {}
            placed_iso = parsed.get("placed_at")
            placed_at = None
            if placed_iso:
                try:
                    placed_at = datetime.fromisoformat(placed_iso.replace("Z", ""))
                except Exception:
                    placed_at = placed_dt
            else:
                placed_at = placed_dt

            store_scope = parsed.get("store_scope") or store_default

            mid_str = nid.decode() if isinstance(nid, bytes) else str(nid)
            out.append({
                "vendor":              slug,
                "store_scope":         store_scope,
                "order_number":        parsed.get("order_number"),
                "customer_or_caterer": parsed.get("customer_or_caterer"),
                "placed_at":           placed_at,
                "total_cents":         parsed.get("total_cents"),
                "status":              parsed.get("status"),
                "items_json":          parsed.get("items") or None,
                "tracking_links_json": parsed.get("tracking_links") or None,
                "source_email_mid":    f"{source_inbox_label}:{mid_str}",
                "subject":             subject,
                "from_addr":           addr,
                "raw_body":            body[:8000],
                "parse_status":        "parsed" if parsed else "unparsed",
            })
    return out


def main() -> int:
    pwd = _email_pwd()
    if not pwd:
        print(json.dumps({"ok": False, "error": "ORDERS_EMAIL_PWD unset"}))
        return 1

    rows: list[dict] = []
    inboxes = [
        (IMAP_USER, "orders@cenaskitchen.com", None),
        ("ezcater@cenaskitchen.com", "ezcater@cenaskitchen.com", "tomball"),
    ]
    inbox_summary = []
    for user, label, store_default in inboxes:
        try:
            M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            try:
                M.login(user, pwd)
                M.select("INBOX", readonly=True)
                inbox_rows = _process_inbox(M, label, store_default=store_default)
                rows.extend(inbox_rows)
                inbox_summary.append({"inbox": label, "matched": len(inbox_rows)})
            finally:
                try: M.close()
                except Exception: pass
                try: M.logout()
                except Exception: pass
        except Exception as e:  # noqa: BLE001
            inbox_summary.append({"inbox": label, "error": str(e)[:200]})

    db = SessionLocal()
    inserted = 0
    updated = 0
    try:
        for r in rows:
            existing = (db.query(VendorRecentOrder)
                .filter(VendorRecentOrder.vendor == r["vendor"])
                .filter(VendorRecentOrder.source_email_mid == r["source_email_mid"])
                .first())
            if existing:
                for k, v in r.items():
                    if k != "source_email_mid":
                        setattr(existing, k, v)
                updated += 1
            else:
                db.add(VendorRecentOrder(**r))
                inserted += 1
        db.commit()
    finally:
        db.close()

    print(json.dumps({
        "ok": True,
        "inserted": inserted,
        "updated": updated,
        "total_processed": len(rows),
        "inbox_summary": inbox_summary,
        "by_vendor": {
            v: sum(1 for r in rows if r["vendor"] == v)
            for v in {r["vendor"] for r in rows}
        },
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
