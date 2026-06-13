"""Vendored minimal shim of app.services.assistant_routing_shared.

The CENA engine optionally imports two helpers from the CK runtime module
``app.services.assistant_routing_shared``:

  * ``read_secret(name)``           - used by cena_llm to find the LLM API key.
  * ``normalize_store_key(raw)`` /  - used by cena_sql_analytics to canonicalize
    ``STORE_ALIASES``                 raw store_N values to copperfield/tomball.

Both call sites already fall back gracefully when the import fails, but the
fallbacks lose behavior (read_secret -> env-var only; normalize_store_key -> a
smaller alias map). Vendoring this shim keeps the cloud engine's behavior
identical to the runtime's. Only these two surfaces are reproduced - this is NOT
the full routing module. Logic is copied verbatim from the runtime.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Secret-file fallbacks (verbatim from the runtime). read_secret() prefers the
# env var, then NAME_FILE, then these defaults. On Render the key is supplied as
# the GEMINI_API_KEY env var, so the file paths simply will not exist - harmless.
SECRET_DEFAULTS = {
    "GEMINI_API_KEY": [
        r"C:\Users\sam\cena-secrets\gemini_api_key.txt",
        r"C:\Users\sam\cena\.secrets\gemini_api_key.txt",
        r"C:\Users\sam\cena-secrets\google_api_key.txt",
    ],
}

STORE_ALIASES = {
    "1": "copperfield",
    "store_1": "copperfield",
    "store 1": "copperfield",
    "store_3": "copperfield",
    "store 3": "copperfield",
    "uno": "copperfield",
    "uno mas": "copperfield",
    "copperfield": "copperfield",
    "2": "tomball",
    "store_2": "tomball",
    "store 2": "tomball",
    "store_4": "tomball",
    "store 4": "tomball",
    "dos": "tomball",
    "dos mas": "tomball",
    "tomball": "tomball",
}


def read_secret(name: str) -> str | None:
    value = (os.getenv(name) or "").strip()
    if value:
        return value
    file_value = (os.getenv(name + "_FILE") or "").strip()
    candidates = [file_value] if file_value else []
    candidates.extend(SECRET_DEFAULTS.get(name, []))
    for raw_path in candidates:
        if not raw_path:
            continue
        try:
            path = Path(raw_path)
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return None


def normalize_store_key(raw_store: Any) -> str:
    value = str(raw_store or "unknown").strip().casefold()
    return STORE_ALIASES.get(value, value or "unknown")
