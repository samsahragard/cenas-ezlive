"""ezCater Partner API → Cenas Kitchen site ingest.

Replaces the fragile PDF-capture path. Calls api.ezcater.com/graphql
with the saved Partner API token, maps the response into the same
RawOrder shape the existing PDF pipeline produces, and POSTs to the
local Flask app's /orders/ingest_structured endpoint.

Usage:
  python ezcater_api_ingest.py --delivery-id 291371988

Tokens read from:
  C:\\Users\\sam\\.openclaw\\.secrets\\ezcater_api_token.txt   (Partner API)
  C:\\Users\\sam\\.openclaw\\.secrets\\ingest_token.txt        (Cenas Kitchen site)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import os

EZCATER_API = "https://api.ezcater.com/graphql"
# Configurable via env so this works on AiCk (default localhost:5000) AND on
# Render (set INGEST_URL=http://127.0.0.1:$PORT/orders/ingest_structured).
INGEST_URL = os.getenv("INGEST_URL", "http://127.0.0.1:5000/orders/ingest_structured")
# Token files - kept as fallback for AiCk; env vars win when set (Render).
EZ_TOKEN_FILE = Path(r"C:\Users\sam\.openclaw\.secrets\ezcater_api_token.txt")
INGEST_TOKEN_FILE = Path(r"C:\Users\sam\.openclaw\.secrets\ingest_token.txt")
# Log file: skip file handler if AiCk path doesn't exist (Render etc.)
_aick_log_dir = Path(r"C:\Users\sam\.openclaw\scripts")
LOG = _aick_log_dir / "ezcater_api_ingest.log" if _aick_log_dir.exists() else None


def _read_token(env_var: str, fallback_file: Path) -> str:
    """Prefer env var (Render), fall back to file (AiCk)."""
    val = os.getenv(env_var)
    if val:
        return val.strip()
    if fallback_file.exists():
        return fallback_file.read_text(encoding="utf-8").strip()
    raise RuntimeError(f"missing both env {env_var} and file {fallback_file}")


_log_handlers = [logging.StreamHandler(sys.stdout)]
if LOG is not None:
    _log_handlers.append(logging.FileHandler(LOG, encoding="utf-8"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_log_handlers,
)
log = logging.getLogger(__name__)


ORDER_QUERY = """
{
  order(id: "%s") {
    uuid
    orderNumber
    deliveryId
    orderSourceType
    caterer { name address { street city state zip } }
    event {
      headcount
      catererHandoffFoodTime
      timeZoneIdentifier
      orderType
      address { street city state zip }
      contact { name phone }
      customerProvidedName
    }
    orderCustomer { firstName lastName fullName }
    catererCart {
      foodLineItems { name quantity size }
      orderItems {
        name quantity menuItemSizeName specialInstructions
        customizations { customizationTypeName name quantity }
      }
      tableware { specialInstructions tablewareChoices { name isIncluded itemCount } }
    }
  }
}
"""


def gql_pull(delivery_id: str, token: str) -> dict:
    body = json.dumps({"query": ORDER_QUERY % delivery_id}).encode()
    req = urllib.request.Request(
        EZCATER_API, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Origin": "https://api.ezcater.com",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"ezCater API HTTP {e.code}: {body}") from e


def _format_order_number(raw: str) -> str:
    """API returns 'XZ3A05'; pipeline expects 'XZ3-A05'. Insert dash after pos 3."""
    if not raw:
        return raw
    if "-" in raw:
        return raw
    if len(raw) >= 4:
        return f"{raw[:3]}-{raw[3:]}"
    return raw


def _format_address(addr: dict | None) -> str:
    if not addr: return ""
    parts = [addr.get("street", ""), addr.get("city", ""),
             f'{addr.get("state","")} {addr.get("zip","")}'.strip()]
    return ", ".join(p for p in parts if p)


def _format_customizations_as_line_items(customizations: list[dict]) -> list[str]:
    """Turn the API's customization shape into the 'Type: Value' line item
    strings the existing parser (_parse_choices, etc.) expects to see."""
    out = []
    for c in customizations or []:
        t = c.get("customizationTypeName", "").strip()
        n = c.get("name", "").strip()
        if not n:
            continue
        if t:
            out.append(f"{t}: {n}")
        else:
            out.append(n)
    return out


def _format_tableware_line_items(choices: list[dict]) -> list[str]:
    """Mirror the format _parse_tableware_extras() expects: "Component: QTY"."""
    out = []
    for tc in choices or []:
        if not tc.get("isIncluded"):
            continue
        name = tc.get("name", "").strip()
        cnt = tc.get("itemCount", 0)
        if name:
            out.append(f"{name}: {cnt}")
    return out


def map_to_raw_order(api_order: dict) -> dict:
    """Map ezCater Partner API response → internal RawOrder shape.

    The shape returned here matches what Claude vision would emit in the
    existing PDF pipeline (see app/domain/schemas.py:RawOrder)."""
    cart = api_order.get("catererCart") or {}
    event = api_order.get("event") or {}
    caterer = api_order.get("caterer") or {}
    customer = api_order.get("orderCustomer") or {}
    contact = event.get("contact") or {}

    # Map ISO + tz → local-formatted date/time (matches existing extractor output)
    deliver_dt_local = None
    iso = event.get("catererHandoffFoodTime")
    tz = event.get("timeZoneIdentifier") or "America/Chicago"
    if iso:
        utc = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
        deliver_dt_local = utc.astimezone(ZoneInfo(tz))
    if deliver_dt_local:
        date_str = f"{deliver_dt_local.strftime('%B')} {deliver_dt_local.day}, {deliver_dt_local.year}"
        deliver_at = deliver_dt_local.strftime("%I:%M %p").lstrip("0")
    else:
        date_str = ""
        deliver_at = ""

    # Build raw_items: one per orderItem (preferring customizations) + a Tableware entry.
    raw_items = []
    for it in cart.get("orderItems", []):
        line_items = _format_customizations_as_line_items(it.get("customizations", []))
        if it.get("specialInstructions"):
            line_items.append(it["specialInstructions"])
        raw_items.append({
            "alias": it.get("name", ""),
            "qty": it.get("quantity", 0),
            "line_items": line_items,
        })

    tableware = cart.get("tableware") or {}
    tw_lines = _format_tableware_line_items(tableware.get("tablewareChoices", []))
    if tw_lines:
        raw_items.append({"alias": "Tableware", "qty": 1, "line_items": tw_lines})

    # store string must include the caterer street; normalize_store_id() in
    # the app uses substring matches like "15650" / "27727" / "tomball" /
    # "westheimer" / "spring stuebner" to map to store_1..store_4.
    store_name = caterer.get("name", "")
    store_addr = (caterer.get("address") or {}).get("street", "")
    store_str = f"{store_name} - {store_addr}".strip(" -") if store_addr else store_name

    return {
        "order_id": _format_order_number(api_order.get("orderNumber") or ""),
        # Underscore-prefixed extras: not passed through normalize_order, but
        # the ingest endpoint plucks them out to persist on the Order row.
        "_external_delivery_id": api_order.get("deliveryId"),
        "client": (customer.get("fullName") or "").strip()
                   or (event.get("customerProvidedName") or "").strip(),
        "upon_delivery_ask_for": (contact.get("name") or "").strip(),
        "store": store_str,
        "headcount": event.get("headcount") or 0,
        "customer_phone": (contact.get("phone") or "").strip(),
        "date": date_str,
        "deliver_at": deliver_at,
        # Partner API doesn't expose an order delivery window — use the handoff
        # time as both endpoints. The existing normalizer tolerates an empty
        # delivery_window dict.
        "delivery_window": {"start": deliver_at, "end": deliver_at},
        "delivery_address": _format_address(event.get("address")),
        "delivery_instructions": None,
        "setup_required": None,
        "notes": None,
        "raw_items": raw_items,
        "extraction_confidence": "high",
        "uncertain_fields": [],
        "extraction_notes": None,  # source is implicit (Partner API has no OCR ambiguity)
    }


def post_ingest(payload: dict, ingest_token: str) -> dict:
    log.info("POST %s (order_id=%s, %d raw items)",
             INGEST_URL, payload.get("order_id"), len(payload.get("raw_items", [])))
    r = requests.post(
        INGEST_URL,
        headers={"Authorization": f"Bearer {ingest_token}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    log.info("ingest -> HTTP %d  body=%s", r.status_code, r.text[:300])
    r.raise_for_status()
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delivery-id", required=True, help="ezManage internal delivery ID (e.g. 291371988)")
    ap.add_argument("--dump-only", action="store_true", help="print the mapped RawOrder JSON and exit (no POST)")
    args = ap.parse_args()

    try:
        ez_token = _read_token("EZCATER_API_TOKEN", EZ_TOKEN_FILE)
    except RuntimeError as e:
        log.error(str(e)); return 2

    log.info("pulling delivery_id=%s from ezCater API", args.delivery_id)
    resp = gql_pull(args.delivery_id, ez_token)
    if "errors" in resp:
        log.error("GraphQL errors: %s", json.dumps(resp["errors"])[:500]); return 3
    api_order = resp.get("data", {}).get("order")
    if not api_order:
        log.error("API returned no order data"); return 4

    payload = map_to_raw_order(api_order)
    if args.dump_only:
        print(json.dumps(payload, indent=2))
        return 0

    try:
        ingest_token = _read_token("INGEST_TOKEN", INGEST_TOKEN_FILE)
    except RuntimeError as e:
        log.error(str(e)); return 2
    try:
        result = post_ingest(payload, ingest_token)
    except Exception as e:
        log.error("POST failed: %s", e); return 5

    if result.get("success"):
        log.info("OK: order_id=%s view_url=%s needs_review=%s",
                 result.get("order_id"), result.get("view_url"), result.get("needs_review"))
        return 0
    log.error("ingest reported failure: %s", result); return 6


if __name__ == "__main__":
    sys.exit(main())
