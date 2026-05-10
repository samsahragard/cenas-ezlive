"""Claude-backed resolver for ezCater extraction warnings.

Last line of defense in the auto-resolve pipeline. After the webhook handler
ingests an order and finds warnings, AND a fresh re-pull from ezCater's
Partner API doesn't clear them, we ask Claude (via the same `anthropic`
SDK the PDF processor uses) to look at the order data + warnings and decide
whether each warning is a real problem or a false positive.

If Claude clears all warnings, the order is silently passed through (no
review queue entry). If Claude says any warning is real, the order's
`needs_review` flag stays True and surfaces in the Partner →
Developer → Ezcater queue page.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

_CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # fast + cheap for this triage


def try_claude_resolve(raw_order: dict[str, Any] | None,
                       raw_warnings: list[str] | None,
                       normalized_order: dict[str, Any] | None = None,
                       norm_warnings: list[str] | None = None) -> tuple[bool, str]:
    """Ask Claude whether the supplied warnings are real issues for this order.

    Returns (cleared, notes) where:
        cleared = True   → all warnings are false positives, order can pass through
        cleared = False  → at least one warning is real and a human should look
        notes            → Claude's reasoning (kept for the queue display)

    Network or auth failures default to cleared=False so the order falls back
    to manual review rather than silently passing.
    """
    warnings = list(raw_warnings or []) + list(norm_warnings or [])
    if not warnings:
        return True, "no warnings to evaluate"

    try:
        import anthropic
        from app.config import Config
    except Exception:
        log.exception("ezcater_resolver: anthropic SDK unavailable")
        return False, "Claude SDK not available — flagged for manual review"

    api_key = getattr(Config, "ANTHROPIC_API_KEY", None) or None
    if not api_key:
        log.warning("ezcater_resolver: ANTHROPIC_API_KEY not configured; defaulting to manual review")
        return False, "Claude API key missing — flagged for manual review"

    # Compact view of the order so Claude has enough context but the prompt
    # doesn't balloon. Prefer the normalized order (it's what the kitchen sees);
    # fall back to raw if normalization itself failed.
    summary = _summarize_order(raw_order, normalized_order)
    prompt = f"""You are auditing an automatic catering-order ingest. The system
flagged the following warnings during extraction/normalization. Decide whether
each warning is a REAL problem a human should fix, or a FALSE POSITIVE that's
safe to ignore (e.g. a quirky-but-valid order, an expected blank field, a
known item the menu list just hasn't seen yet).

Order summary:
{summary}

Warnings raised:
{chr(10).join(f"  - {w}" for w in warnings)}

Respond in JSON only, with this shape:
{{
    "cleared": <true|false>,
    "notes": "<one or two sentences explaining your reasoning, plain English>"
}}

Cleared=true means none of these warnings should block kitchen prep — pass the
order through silently. Cleared=false means at least one warning is real and
the order should appear on the manual review queue."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in (msg.content or []):
            if getattr(block, "type", "") == "text":
                text += getattr(block, "text", "") or ""
        text = text.strip()
        # Trim to first/last brace in case Claude wrapped in prose
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            text = text[s:e + 1]
        parsed = json.loads(text)
        cleared = bool(parsed.get("cleared"))
        notes = (parsed.get("notes") or "").strip()[:500]
        log.info("ezcater_resolver: cleared=%s notes=%s", cleared, notes[:80])
        return cleared, notes
    except Exception as ex:
        log.exception("ezcater_resolver: Claude call failed")
        return False, f"Claude triage failed: {ex}"


def _summarize_order(raw: dict | None, norm: dict | None) -> str:
    """Compact, prompt-friendly view of an order. Doesn't include item-level
    detail unless an item flag is involved (saves tokens)."""
    o = norm or raw or {}
    bits = []
    for k in ("order_id", "client", "delivery_date", "deliver_at",
              "delivery_address", "headcount", "extraction_confidence"):
        if k in o and o[k] is not None:
            bits.append(f"  {k}: {o[k]!r}")
    items = (o.get("normalized_items") if norm else None) or o.get("raw_items") or []
    if items:
        bits.append(f"  items: {len(items)} line(s)")
        # Surface the first 5 items so Claude can sanity-check
        for it in items[:5]:
            name = it.get("item_key") or it.get("alias") or it.get("name") or "?"
            qty = it.get("qty") or "?"
            flags = it.get("flags") or []
            flag_str = f"  flags={','.join(flags)}" if flags else ""
            bits.append(f"    - {qty}× {name}{flag_str}")
    return "\n".join(bits) or "(no order fields available)"
