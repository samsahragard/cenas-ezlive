"""ezCater webhook receiver + live-mode auto-pipeline.

Endpoint: POST /ezcater/webhook
Public URL: https://aick.tailb5e6ee.ts.net/ezcater/webhook (Tailscale Funnel)

Modes:
  - WEBHOOK_TEST_MODE=1: log-only. Webhook payload is recorded; no actions taken.
  - WEBHOOK_TEST_MODE=0: live. Pulls full order, assigns driver, ingests into UI,
    sends Telegram. ezCater's own auto-accept handles the actual order acceptance.

Live-mode flow (per Order.submitted webhook):
    1. Pull full order detail via Partner API (existing helper).
    2. Map caterer.uuid -> store_1..4.
    3. Distance check (Google Routes API) -> detect store-mismatch exceptions.
    4. courierAssign for Masood (stores 1/3) or Sam (stores 2/4).
    5. POST RawOrder to local /orders/ingest_structured -> kitchen UI.
    6. Telegram notification (success or failure).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
webhook = Blueprint("ezcater_webhook", __name__)

# ---------------------------------------------------------------------------
# Constants / config
# ---------------------------------------------------------------------------

# Path resolution: works on AiCk (uses ~/.openclaw/scripts) AND on Render
# (uses bundled scripts/ in the repo). The bundled copy is preferred when both
# exist because env vars can be configured to override file-reads anyway.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BUNDLED_SCRIPTS = _REPO_ROOT / "scripts"
_AICK_SCRIPTS = Path(r"C:\Users\sam\.openclaw\scripts")
OPENCLAW_SCRIPTS = _AICK_SCRIPTS if _AICK_SCRIPTS.exists() else _BUNDLED_SCRIPTS

# Webhook log: writable location. Falls back to /tmp on Render (or wherever
# the disk is mounted).
_log_dir = OPENCLAW_SCRIPTS if OPENCLAW_SCRIPTS.exists() else Path(os.getenv("WEBHOOK_LOG_DIR", "/tmp"))
WEBHOOK_LOG = _log_dir / "ezcater_webhook.jsonl"

# Secrets: env vars first (Render), file fallback (AiCk).
_AICK_SECRETS = Path(r"C:\Users\sam\.openclaw\.secrets")
EZ_TOKEN_FILE = _AICK_SECRETS / "ezcater_api_token.txt"
INGEST_TOKEN_FILE = _AICK_SECRETS / "ingest_token.txt"
OPENCLAW_JSON = Path(r"C:\Users\sam\.openclaw\openclaw.json")

# Distance check: prefer bundled script, fall back to AiCk's path.
DISTANCE_SCRIPT = _BUNDLED_SCRIPTS / "ezcater_distance.py" if (_BUNDLED_SCRIPTS / "ezcater_distance.py").exists() else _AICK_SCRIPTS / "ezcater_distance.py"

# Self-call URL for ingest. On Render, $PORT is the bound port. On AiCk, 5000.
INGEST_URL = os.getenv("INGEST_URL") or f"http://127.0.0.1:{os.getenv('PORT', '5000')}/orders/ingest_structured"
EZCATER_API = "https://api.ezcater.com/graphql"

# Caterer UUID -> internal store id (matches normalize.py:resolve_origin_store_id).
CATERER_UUID_TO_STORE = {
    "c3c83ab2-f267-4944-bbb8-4499750b2942": "store_1",  # Copperfield 15650 FM 529
    "e52a169a-9074-464c-8f9c-8aabe4255227": "store_2",  # Tomball 27727
    "67e45dbe-d282-4309-ab9a-9a8f73a9b282": "store_3",  # Westheimer
    "a5cb611e-3e60-43dc-a03b-6229d4f43b10": "store_4",  # Spring Stuebner
}

# "Driver" = label that tells managers which physical kitchen preps the order.
# Stores 1 + 3 are prepped at Copperfield kitchen — Masood works there ("Masood CK #1").
# Stores 2 + 4 are prepped at Tomball kitchen — Sam works there ("Sam CK #2").
# Stores 3 + 4 are ghost-storefront listings on ezCater (no physical kitchen).
# The "#1" / "#2" suffix matches the kitchen number, not the person's seniority.
SAM    = {"id": "sam-ck-2",    "firstName": "Sam",    "lastName": "CK #2",
          "phone": "+17133661208", "providerSource": "IN_HOUSE"}
MASOOD = {"id": "masood-ck-1", "firstName": "Masood", "lastName": "CK #1",
          "phone": "+18322832219", "providerSource": "IN_HOUSE"}
DRIVER_FOR_STORE = {
    "store_1": MASOOD, "store_3": MASOOD,  # Copperfield kitchen (Cenas Kitchen #1)
    "store_2": SAM,    "store_4": SAM,     # Tomball kitchen   (Cenas Kitchen #2)
}

# Sam's chat. (Could read from openclaw.json allowFrom; hardcoded for clarity.)
SAM_TG_CHAT_ID = "8612324971"

# Reuse the existing AiCk helper module for order pull + RawOrder mapping.
sys.path.insert(0, str(OPENCLAW_SCRIPTS))
try:
    from ezcater_api_ingest import gql_pull, map_to_raw_order  # type: ignore
except ImportError:
    logger.exception("could not import ezcater_api_ingest; live mode disabled")
    gql_pull = None
    map_to_raw_order = None


# ---------------------------------------------------------------------------
# Logging incoming webhook events
# ---------------------------------------------------------------------------

def _log_event(payload: dict, headers: dict, source: str = "POST") -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "remote_addr": request.remote_addr,
        "headers": headers,
        "payload": payload,
    }
    try:
        OPENCLAW_SCRIPTS.mkdir(parents=True, exist_ok=True)
        with WEBHOOK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        logger.exception("failed to append webhook log")


def _is_test_mode() -> bool:
    return os.getenv("WEBHOOK_TEST_MODE", "1").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# ezCater GraphQL client
# ---------------------------------------------------------------------------

def _ez_token() -> str:
    """ezCater Partner API token. Env var (Render) wins over file (AiCk)."""
    val = os.getenv("EZCATER_API_TOKEN")
    if val:
        return val.strip()
    return EZ_TOKEN_FILE.read_text(encoding="utf-8").strip()


def _ez_gql(query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        EZCATER_API, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {_ez_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (cenaskitchen webhook handler)",
            "Origin": "https://api.ezcater.com",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        return {"_http_error": e.code, "_body": body}


# ---------------------------------------------------------------------------
# Distance check (existing standalone script)
# ---------------------------------------------------------------------------

def _distance_check(drop_off_address: str, order_store_num: int) -> dict | None:
    if not drop_off_address:
        return None
    try:
        out = subprocess.run(
            [sys.executable, str(DISTANCE_SCRIPT), drop_off_address,
             "--order-store", str(order_store_num)],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            logger.warning("distance check rc=%d: %s", out.returncode, out.stderr[:200])
            return None
        return json.loads(out.stdout)
    except Exception:
        logger.exception("distance check failed")
        return None


# ---------------------------------------------------------------------------
# Telegram (uses AiCk gateway's bot token from openclaw.json)
# ---------------------------------------------------------------------------

def _tg_token() -> str | None:
    """Telegram bot token. Env var (Render) wins over openclaw.json (AiCk)."""
    val = os.getenv("TELEGRAM_BOT_TOKEN")
    if val:
        return val.strip()
    try:
        if OPENCLAW_JSON.exists():
            cfg = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8-sig"))
            return ((cfg.get("channels") or {}).get("telegram") or {}).get("botToken")
    except Exception:
        logger.exception("could not read telegram token from openclaw.json")
    return None


def _tg_send(text: str) -> None:
    token = _tg_token()
    if not token:
        return
    body = json.dumps({
        "chat_id": SAM_TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception:
        logger.exception("telegram send failed")


# ---------------------------------------------------------------------------
# courierAssign + ingest
# ---------------------------------------------------------------------------

def _assign_courier(delivery_uuid: str, courier: dict) -> tuple[bool, str]:
    """Returns (ok, error_msg)."""
    res = _ez_gql("""
    mutation Assign($input: CourierAssignInput!) {
      courierAssign(input: $input) {
        delivery { id }
        userErrors {
          __typename
          ... on UserError { message }
          ... on DeliveryValidationError { message }
        }
      }
    }
    """, {"input": {"deliveryId": delivery_uuid, "courier": courier}})
    if "_http_error" in res:
        return False, f"HTTP {res['_http_error']}: {res.get('_body', '')[:120]}"
    if "errors" in res:
        return False, "; ".join(e.get("message", "?") for e in res["errors"])[:200]
    payload = (res.get("data") or {}).get("courierAssign") or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        msgs = [e.get("message", "?") for e in user_errors if isinstance(e, dict)]
        return False, "; ".join(msgs)[:200]
    return True, ""


def _unassign_courier(delivery_uuid: str, courier_id: str) -> tuple[bool, str]:
    """Returns (ok, error_msg). Mirrors _assign_courier — calls courierUnassign
    on the delivery's currently-assigned courier so the ezCater portal driver
    field opens up. Used right after _assign_courier in the webhook flow so
    managers don't have to manually click "Unassign Courier" in the kitchen
    UI before going to the ezCater portal to set the real driver."""
    res = _ez_gql("""
    mutation Unassign($input: CourierUnassignInput!) {
      courierUnassign(input: $input) {
        delivery { id }
        userErrors {
          __typename
          ... on UserError { message }
        }
      }
    }
    """, {"input": {"deliveryId": delivery_uuid, "courierId": courier_id}})
    if "_http_error" in res:
        return False, f"HTTP {res['_http_error']}: {res.get('_body', '')[:120]}"
    if "errors" in res:
        return False, "; ".join(e.get("message", "?") for e in res["errors"])[:200]
    payload = (res.get("data") or {}).get("courierUnassign") or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        msgs = [e.get("message", "?") for e in user_errors if isinstance(e, dict)]
        return False, "; ".join(msgs)[:200]
    return True, ""


def _ingest_into_ezlive(raw_order_payload: dict) -> tuple[bool, dict]:
    """POST to local /orders/ingest_structured. Returns (ok, response_dict)."""
    try:
        ingest_token = (os.getenv("INGEST_TOKEN") or
                        (INGEST_TOKEN_FILE.read_text(encoding="utf-8").strip()
                         if INGEST_TOKEN_FILE.exists() else "")).strip()
        if not ingest_token:
            return False, {"error": "INGEST_TOKEN not configured"}
        body = json.dumps(raw_order_payload).encode()
        req = urllib.request.Request(
            INGEST_URL, data=body,
            headers={
                "Authorization": f"Bearer {ingest_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return True, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        return False, {"http_error": e.code, "body": body}
    except Exception as e:
        return False, {"error": str(e)}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@webhook.route("/ezcater/webhook", methods=["GET", "POST"])
def receive():
    if request.method == "GET":
        return jsonify({
            "ok": True,
            "endpoint": "ezcater webhook receiver",
            "test_mode": _is_test_mode(),
            "log_file": str(WEBHOOK_LOG),
            "exempt_from_auth": True,
        })

    payload = request.get_json(silent=True) or {}
    headers = {k: v for k, v in request.headers.items()}
    raw_body = request.get_data(as_text=True)[:2000]

    _log_event(
        {"_parsed_json": payload, "_raw_body_excerpt": raw_body},
        headers,
        source="POST",
    )
    logger.info("ezCater webhook: bytes=%d, key=%s, test_mode=%s",
                len(raw_body), payload.get("key"), _is_test_mode())

    if _is_test_mode():
        return jsonify({
            "ok": True, "mode": "TEST", "received": True,
            "note": "logged but not processed; set WEBHOOK_TEST_MODE=0 to enable",
        }), 200

    # LIVE event routing.
    entity_type = payload.get("entity_type")
    event_key = payload.get("key")
    entity_id = payload.get("entity_id")
    parent_id = payload.get("parent_id")

    if entity_type == "Order" and entity_id:
        if event_key == "submitted":
            threading.Thread(target=_process_submitted_safe,
                             args=(entity_id, parent_id), daemon=True).start()
            return jsonify({"ok": True, "queued": entity_id, "key": "submitted"}), 200
        if event_key == "updated":
            # Re-pull + re-ingest. Persistence layer does delete-and-recreate
            # on matching external_order_id, so this overwrites cleanly.
            # Driver + distance get recomputed naturally.
            threading.Thread(target=_process_submitted_safe,
                             args=(entity_id, parent_id), daemon=True).start()
            return jsonify({"ok": True, "queued": entity_id, "key": "updated"}), 200
        if event_key == "cancelled":
            threading.Thread(target=_process_cancelled_safe,
                             args=(entity_id,), daemon=True).start()
            return jsonify({"ok": True, "queued": entity_id, "key": "cancelled"}), 200
        # succeeded etc. — log only, no action yet.

    return jsonify({"ok": True, "received": True, "acted_on": False,
                    "note": f"event {entity_type}/{event_key} logged only"}), 200


def _process_submitted_safe(entity_id: str, parent_id: str | None) -> None:
    try:
        _process_submitted(entity_id, parent_id)
    except Exception:
        logger.exception("LIVE-MODE failed for entity_id=%s", entity_id)
        _tg_send(f"❌ Webhook auto-pipeline crashed for entity {entity_id[:8]} — check logs")


def _process_cancelled_safe(entity_id: str) -> None:
    try:
        _process_cancelled(entity_id)
    except Exception:
        logger.exception("CANCELLATION handler failed for entity_id=%s", entity_id)
        _tg_send(f"❌ Cancellation handler crashed for entity {entity_id[:8]} — check logs")


def _process_cancelled(entity_id: str) -> None:
    """Mark the corresponding Cenas EZLive Order row as cancelled and notify."""
    if gql_pull is None:
        _tg_send(f"❌ Cancellation: helpers missing for {entity_id[:8]}")
        return
    api_resp = gql_pull(entity_id, _ez_token())
    api_order = (api_resp.get("data") or {}).get("order") if isinstance(api_resp, dict) else None
    if not api_order:
        _tg_send(f"❌ Cancellation: could not pull order {entity_id[:8]} from API")
        return

    raw_num = api_order.get("orderNumber") or ""
    external_id = raw_num if "-" in raw_num else (
        f"{raw_num[:3]}-{raw_num[3:]}" if len(raw_num) >= 4 else raw_num
    )
    caterer_uuid = (api_order.get("caterer") or {}).get("uuid") or ""
    origin_store = CATERER_UUID_TO_STORE.get(caterer_uuid, "?")

    # Update DB row in-place — no re-ingest needed.
    from app.db import get_db
    from app.models import Order
    db = next(get_db())
    try:
        order = db.query(Order).filter_by(external_order_id=external_id).first()
        if not order:
            _tg_send(f"⚠️ Cancellation: order {external_id} not found in Cenas EZLive — cannot mark")
            return
        order.status = "cancelled"
        db.commit()
        _tg_send(f"❌ Order {external_id} ({origin_store}) — CANCELLED by customer\nRemoved from Cenas EZLive listings.")
        logger.info("cancelled order %s (entity %s)", external_id, entity_id)
    finally:
        db.close()


def _clear_order_review_flag(external_order_id: str | None) -> None:
    """Set Order.needs_review=False after Claude resolver clears warnings.
    Used to silently pass an order through when the auto-resolver decides
    the warnings were false positives."""
    if not external_order_id:
        return
    try:
        from app.db import get_db
        from app.models import Order
        db = next(get_db())
        try:
            o = db.query(Order).filter_by(external_order_id=external_order_id).first()
            if o and o.needs_review:
                o.needs_review = False
                db.commit()
                logger.info("auto-resolver: cleared needs_review on %s", external_order_id)
        finally:
            db.close()
    except Exception:
        logger.exception("auto-resolver: could not clear needs_review on %s", external_order_id)


def _process_submitted(entity_id: str, parent_id: str | None) -> None:
    """LIVE-MODE: assign driver + ingest into kitchen UI + Telegram."""
    import time as _time

    if gql_pull is None or map_to_raw_order is None:
        _tg_send(f"❌ Webhook handler missing helpers; cannot process {entity_id[:8]}")
        return

    # Step 1: pull full order. ezCater sometimes fires Order/submitted before
    # the deliveryId is populated on their side (a few-second race). Retry
    # twice with a delay if deliveryId comes back empty.
    api_order = None
    for attempt in range(3):
        if attempt > 0:
            _time.sleep(20)  # 20s between attempts (total worst case ~40s)
        api_resp = gql_pull(entity_id, _ez_token())
        if "errors" in api_resp:
            if attempt == 2:
                msg = json.dumps(api_resp["errors"])[:200]
                _tg_send(f"❌ Webhook: pull failed for {entity_id[:8]}: {msg}")
                return
            continue
        api_order = (api_resp.get("data") or {}).get("order")
        if not api_order:
            if attempt == 2:
                _tg_send(f"❌ Webhook: no order in API response for {entity_id[:8]}")
                return
            continue
        if api_order.get("deliveryId"):
            break  # got everything we need
        # deliveryId still missing — retry unless we've exhausted attempts
        if attempt < 2:
            logger.info("deliveryId not yet populated for %s; retrying in 20s (attempt %d)", entity_id[:8], attempt + 1)

    if not api_order:
        _tg_send(f"❌ Webhook: could not pull {entity_id[:8]} after 3 attempts")
        return

    order_number_raw = api_order.get("orderNumber") or "?"
    order_number = order_number_raw if "-" in order_number_raw \
        else (f"{order_number_raw[:3]}-{order_number_raw[3:]}" if len(order_number_raw) >= 4 else order_number_raw)

    delivery_uuid = api_order.get("deliveryId")
    caterer_uuid = ((api_order.get("caterer") or {}).get("uuid")) or parent_id
    origin_store = CATERER_UUID_TO_STORE.get(caterer_uuid)

    # Hard-fail only when origin_store is unknown — without it we can't route at all.
    if not origin_store:
        _tg_send(
            f"⚠️ Order {order_number}: cannot route\n"
            f"caterer={caterer_uuid[:8] if caterer_uuid else '?'} not in known store map"
        )
        return

    # Soft-fail when delivery_uuid is missing: still ingest into Cenas EZLive
    # (so it appears on the listing) but skip the API assignment. This handles
    # the race where ezCater hasn't populated deliveryId yet, AND any future
    # edge case (e.g. pickup orders) where there's no delivery to assign to.
    delivery_missing = not delivery_uuid

    driver = DRIVER_FOR_STORE[origin_store]
    store_num = int(origin_store.split("_")[1])

    # Step 2: distance check (informational; doesn't block)
    event = api_order.get("event") or {}
    addr = event.get("address") or {}
    drop_off = ", ".join(p for p in [
        addr.get("street", ""), addr.get("city", ""),
        f'{addr.get("state","")} {addr.get("zip","")}'.strip()
    ] if p)
    dist = _distance_check(drop_off, store_num)
    exception_flag = bool(dist and dist.get("exception"))

    # Step 3: assign courier (skip if delivery_uuid was missing — handled below)
    # Then immediately unassign so the ezCater portal driver field is open
    # for the manager to set the real driver, eliminating the manual unhook.
    if delivery_missing:
        assign_ok = False
        assign_err = "skipped — ezCater hadn't populated deliveryId after 3 retries"
        unassign_ok = False
        unassign_err = "skipped (assign skipped)"
    else:
        assign_ok, assign_err = _assign_courier(delivery_uuid, driver)
        if assign_ok:
            unassign_ok, unassign_err = _unassign_courier(delivery_uuid, driver["id"])
            if not unassign_ok:
                logger.warning("auto-unassign failed for %s: %s — manager will need to click Unassign Courier manually",
                               delivery_uuid[:8], unassign_err[:200])
            else:
                logger.info("auto-unassigned %s from delivery %s — portal driver field is open",
                            driver["id"], delivery_uuid[:8])
        else:
            unassign_ok = False
            unassign_err = "skipped (assign failed)"

    # Step 4: ingest into EZLive
    raw_order = map_to_raw_order(api_order)
    ingest_ok, ingest_resp = _ingest_into_ezlive(raw_order)

    # Step 4b: AUTO-RESOLVER. Three-tier resolution if the first ingest
    # came back with warnings:
    #   (1) wait 5s + re-pull from Partner API + re-ingest (handles ezCater
    #       backend lag where field values arrive milliseconds late)
    #   (2) if warnings still remain, ask Claude (haiku) whether each warning
    #       is a real problem or a false positive — Claude knows what valid
    #       catering data usually looks like
    #   (3) if Claude can't clear it, set Order.needs_review=True so the
    #       order surfaces in the Partner → Developer → Ezcater queue
    warnings = (ingest_resp or {}).get("warnings") or []
    claude_notes = ""
    if ingest_ok and warnings:
        logger.info("auto-resolver: %d warnings on %s; re-pulling in 5s", len(warnings), order_number)
        _time.sleep(5)
        api_resp2 = gql_pull(entity_id, _ez_token())
        if "errors" not in api_resp2:
            api_order2 = (api_resp2.get("data") or {}).get("order")
            if api_order2:
                raw_order2 = map_to_raw_order(api_order2)
                ingest_ok2, ingest_resp2 = _ingest_into_ezlive(raw_order2)
                if ingest_ok2:
                    warnings = (ingest_resp2 or {}).get("warnings") or []
                    ingest_resp = ingest_resp2
                    raw_order = raw_order2

        if warnings:
            # Tier 3: ask Claude
            try:
                from app.services.ezcater_resolver import try_claude_resolve
                cleared, claude_notes = try_claude_resolve(
                    raw_order=raw_order,
                    raw_warnings=warnings,
                )
                logger.info("auto-resolver: Claude verdict for %s — cleared=%s notes=%s",
                            order_number, cleared, claude_notes[:80])
                if cleared:
                    # Persist the cleared decision: flip needs_review off on the
                    # Order row so the queue page won't list it.
                    _clear_order_review_flag(raw_order.get("order_id"))
                    warnings = []  # for downstream Telegram so we don't re-mention them
            except Exception:
                logger.exception("auto-resolver: Claude tier failed; leaving needs_review=True")

    # Step 5: Telegram
    lines = []
    lines.append(f"{'✅' if (assign_ok and ingest_ok) else '⚠️'} Order {order_number} (store_{store_num})")
    delivery_dt = event.get("catererHandoffFoodTime")
    if delivery_dt:
        lines.append(f"Delivery: {delivery_dt}")
    if drop_off:
        lines.append(f"Drop-off: {drop_off}")
    lines.append(f"Driver: {driver['firstName']} {driver['lastName']} {driver['phone']}")
    if dist:
        d1 = dist.get("store1_miles")
        d2 = dist.get("store2_miles")
        lines.append(f"Distance: store1={d1}mi  store2={d2}mi  closer={dist.get('closer_store')}")
        if exception_flag:
            lines.append(f"⚠️ EXCEPTION: drop-off is closer to store {dist.get('closer_store')} than the order's store {store_num}")
    lines.append(f"Assign: {'OK' if assign_ok else 'FAILED — ' + (assign_err or '?')}")
    if ingest_ok:
        view_url = ingest_resp.get("view_url") or ""
        # Don't re-include warnings in the Telegram — the auto-resolver
        # has either cleared them (silent pass-through) or queued them
        # to /partner/developer/ezcater for review. Sam asked for no
        # Telegram on warnings.
        lines.append(f"EZLive: ingested {view_url}")
    else:
        lines.append(f"EZLive: FAILED — {json.dumps(ingest_resp)[:200]}")

    _tg_send("\n".join(lines))
    logger.info("auto-pipeline complete for %s: assign=%s ingest=%s exception=%s",
                order_number, assign_ok, ingest_ok, exception_flag)
