"""cena_cloud_supervisor - vendored CENA supervisor lane for cena-cloud.

A near-verbatim port of the CK-local runtime answer logic
(cenas-kitchen-runtime/scripts/assistant_ck_runtime.py) plus its sibling
modules (assistant_conversations, assistant_review_ck_receiver,
assistant_routing_shared, assistant_safety, assistant_context,
assistant_tool_inventory). Only import paths were rewritten so it runs inside
the cena-cloud package with no `app`/`scripts` package present.

Public entry point used by cena_cloud.py:
    from cena_cloud_supervisor import answer
    body, status = answer(payload)   # full supervisor-shaped dict + http status
"""
from __future__ import annotations

from .supervisor import _answer as answer

__all__ = ["answer"]
