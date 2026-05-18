"""Probe ezCater GraphQL for a specific order. Per Sam /sam/chat #721:
before shipping Plan A (periodic pull of ezCater driver assignments) we
need to verify the actual field names on the Delivery type — guessing
risks shipping twice.

Sequence:
  1. order(id) -> deliveryId
  2. __type(name:"Delivery") introspection -> list of fields
  3. Query the Delivery with the candidate courier/driver fields
  4. Compare against our local DB state for the same order

Order ID is read from CENA_PROBE_ORDER_ID env var (caller sets it via
the trigger endpoint body).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import SessionLocal  # noqa: E402
from app.models import Order  # noqa: E402


EZCATER_API = "https://api.ezcater.com/graphql"


def _ez_token() -> str:
    """Render env (preferred) or local file fallback."""
    v = os.getenv("EZCATER_API_TOKEN")
    if v:
        return v.strip()
    p = Path(r"C:\Users\sam\.openclaw\.secrets\ezcater_api_token.txt")
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def gql(query: str, variables: dict | None = None) -> dict:
    token = _ez_token()
    if not token:
        return {"_error": "no EZCATER_API_TOKEN env var (and no local file)"}
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        EZCATER_API, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (cenaskitchen probe)",
            "Origin": "https://api.ezcater.com",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode(errors="replace")[:500]}
    except Exception as e:
        return {"_network": f"{type(e).__name__}: {e}"}


LOOKUP_Q = """
query($id: ID!) {
  order(id: $id) {
    uuid
    orderNumber
    deliveryId
    caterer { name }
  }
}
"""

INTROSPECT_Q = """
{
  __type(name: "Delivery") {
    name
    fields {
      name
      type { name kind ofType { name kind } }
    }
  }
}
"""


def candidate_field_names() -> list[str]:
    """Field names likely to hold the assigned courier/driver on a Delivery."""
    return [
        "assignedCourier", "courier", "courierAssignment",
        "assignedDriver", "driver", "driverAssignment",
        "deliveryAgent", "courierUser",
    ]


def main() -> int:
    order_id = os.getenv("CENA_PROBE_ORDER_ID", "").strip()
    if not order_id:
        print(json.dumps({"error": "CENA_PROBE_ORDER_ID env var required"}))
        return 1

    out: dict = {"order_id": order_id}

    candidates = [order_id]
    if "-" in order_id:
        candidates.append(order_id.replace("-", ""))
    lookup = None
    for cand in candidates:
        res = gql(LOOKUP_Q, {"id": cand})
        order = (res.get("data") or {}).get("order")
        if order and order.get("deliveryId"):
            lookup = order
            out["lookup_cand_used"] = cand
            break
        out.setdefault("lookup_attempts", []).append({"cand": cand, "raw": res})
    if not lookup:
        out["error"] = "could not find deliveryId for order"
        print(json.dumps(out, indent=2))
        return 1

    out["lookup"] = lookup
    delivery_id = lookup["deliveryId"]

    intro = gql(INTROSPECT_Q)
    delivery_type = (intro.get("data") or {}).get("__type") or {}
    fields = delivery_type.get("fields") or []
    field_names = [f.get("name") for f in fields]
    out["delivery_type_field_count"] = len(field_names)
    out["delivery_type_field_names"] = field_names

    matched = [n for n in field_names
               if any(c.lower() in (n or "").lower()
                      for c in ("courier", "driver", "agent"))]
    out["matched_courier_driver_fields"] = matched

    if matched:
        sub_selection = "id name "
        nested = " ".join(f"{m} {{ __typename id name email phone }}" for m in matched)
        query = "query($id: ID!) { delivery(id: $id) { id " + nested + " } }"
        try:
            delivery_res = gql(query, {"id": delivery_id})
            out["delivery_query"] = delivery_res
        except Exception as e:
            out["delivery_query_error"] = f"{type(e).__name__}: {e}"
    else:
        candidates_guess = " ".join(f"{m} {{ __typename }}" for m in candidate_field_names())
        query = "query($id: ID!) { delivery(id: $id) { id " + candidates_guess + " } }"
        out["delivery_query_guess"] = gql(query, {"id": delivery_id})

    db = SessionLocal()
    try:
        local = db.query(Order).filter(Order.external_order_id == order_id).first()
        if local:
            out["local_db"] = {
                "ezcater_driver_name": local.ezcater_driver_name,
                "assigned_driver": local.assigned_driver,
                "assigned_driver_id": local.assigned_driver_id,
                "delivery_date": local.delivery_date,
                "deliver_at": local.deliver_at,
                "status": local.status,
            }
        else:
            out["local_db"] = None
    finally:
        db.close()

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
