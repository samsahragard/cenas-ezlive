from __future__ import annotations
from typing import Any

def validate_raw_order(raw: dict[str, Any]) -> list[str]:
    warnings = []

    if not raw.get("order_id"):
        warnings.append("Missing Order ID")
    if not raw.get("client"):
        warnings.append("Missing Client Name")
    if not raw.get("date"):
        warnings.append("Missing Date")
    if not raw.get("deliver_at"):
        warnings.append("Missing Delivery Time")
    if not raw.get("delivery_address"):
        warnings.append("Missing Delivery Address")
    
    headcount = raw.get("headcount")
    if not headcount or headcount <= 0:
        warnings.append("Headcount is zero or missing")
    elif headcount > 200:
        warnings.append(f"Headcount Unusually Large ({headcount})")
    
    items = raw.get("raw_items") or []
    if not items:
        warnings.append("No Items Found in Order")
    else:
        for item in items:
            if item.get("qty", 0) <= 0:
                warnings.append(f"Item '{item.get('alias', '?')}' has zero or negative quantity")
    
    confidence = raw.get("extraction_confidence")
    uncertain = raw.get("uncertain_fields") or []
    notes = raw.get("extraction_notes")

    if confidence in ("low", "medium"):
        warnings.append(f"Claude flagged {confidence} confidence on this extraction")
    if uncertain:
        warnings.append(f"Claude uncertain about: {', '.join(uncertain)}")
    if notes:
        warnings.append(f"Extraction note: {notes}")
    
    return warnings

def validate_normalized_order(order: dict[str, Any]) -> list[str]:
    warnings = []

    items = order.get("normalized_items") or []
    if not items:
        warnings.append("No items after normalization - check raw extraction")
    else:
        for item in items:
            if item.get("qty", 0) <= 0:
                warnings.append(f"Normalized item '{item.get('item_key', '?')}' has zero or negative quantity")
            for flag in item.get("flags", []):
                if flag.startswith("UNKNOWN_ITEM:"):
                    raw = flag[len("UNKNOWN_ITEM:"):]
                    warnings.append(f"Item not picked up — please check the PDF for: {raw}")

    return warnings