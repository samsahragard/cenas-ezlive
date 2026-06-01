"""LLM-driven parser for vendor order emails (Sam #837 items 9-12).

Rather than write four hand-tuned regex parsers (Webstaurant /
Performance Food / Restaurant Depot / Specs each have a different
email shape), this hands the raw email body to Claude with a
structured-JSON prompt and asks for the same field set across all
four vendors. Output is normalized + dedup'd into the
vendor_recent_orders table by (vendor, source_email_mid).

Called from /sam/cena/run-ingest-vendor-emails which scans the inbox,
classifies sender + body, parses each match, and upserts the row.
"""
from __future__ import annotations

import json
import os
import re

from app.services.produce_ingest import _email_pwd  # noqa: F401 — env consistency check


def _anthropic_client():
    try:
        import anthropic
    except ImportError:
        return None
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


_PARSE_PROMPT = """You extract structured order data from a vendor order email.

Vendor: {vendor}

Return ONE JSON object only (no prose, no markdown fences). Capture EVERY field the
email actually shows — vendors differ, so use null / [] for anything not present.
DO NOT invent data. DO NOT output anything but the JSON object.

Order-level fields:
- order_number (string or null) — the vendor's order number
- placed_at (ISO 8601 string or null) — when the order was placed/sent
- total_cents (integer or null) — order TOTAL in CENTS (dollars * 100, rounded)
- subtotal_cents, tax_cents, shipping_cents (integer or null) — the total breakdown if shown
- status (string or null) — "placed"/"confirmed"/"shipped"/"delivered"/"receipt"/"update"/etc
- customer_or_caterer (string or null) — the caterer / business name on the order
- shipping_type (string or null) — e.g. "Common Carrier w/ Liftgate", "Pickup", "UPS Ground"
- est_delivery (string or null) — estimated delivery / ship date the order shows, if any
- ship_to (object or null) — {{"name": str|null, "company": str|null, "address": str|null, "city_state_zip": str|null, "phone": str|null}}

- items (array) — ONE object per line on the order, INCLUDING products, protection
  plans / warranties, services, and fees. Each item:
    {{"name": str,
      "sku": str|null,                 # the vendor item / model number, e.g. "7156650CX"
      "qty": str|null,
      "unit_price_cents": int|null,
      "subtotal_cents": int|null,      # the line total
      "ship_estimate": str|null,       # e.g. "Usually Ships in 5-10 Bus. Days"
      "notes": str|null,               # e.g. "Special Order", "Virtual Start-up"
      "kind": str,                     # "product"|"protection_plan"|"service"|"fee"|"shipping"|"discount"
      "provider": str|null,            # e.g. "Safeware" for a protection plan
      "ref": str|null}}                # e.g. "PWI:462976"

- tracking_links (array) — list of {{"carrier": str or null, "label": str, "url": str}}
- store_scope (string or null) — "tomball" if the address mentions Tomball or 27727 Tomball Pkwy;
  "copperfield" if Copperfield or 15650 FM 529; otherwise null

EMAIL BODY:
{body}
"""


def llm_parse_vendor_order(vendor: str, body_text: str) -> dict | None:
    """Returns parsed dict or None on failure. Truncates body to 6000
    chars for the prompt — long footers / tracking-link blobs are
    safe to drop, the structured data lives near the top."""
    client = _anthropic_client()
    if client is None:
        return None
    prompt = _PARSE_PROMPT.format(vendor=vendor, body=body_text[:6000])
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",  # cheap for parser duty
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)
    except Exception:
        return None
    s = text.find("{")
    e = text.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        return json.loads(text[s : e + 1])
    except Exception:
        # try to strip trailing commas / fence noise
        cleaned = re.sub(r",\s*([\]}])", r"\1", text[s : e + 1])
        try:
            return json.loads(cleaned)
        except Exception:
            return None
