"""Provider-pluggable LLM client for C.E.N.A. Level 3.

The reasoning loop needs a single ``complete(prompt, system=...) -> str`` call.
Providers are tried in order and a provider that fails auth/permission/quota is
marked dead for the rest of the process so we don't keep paying its latency:

  1. OpenAI   - HTTPS Chat Completions, key via read_secret('OPENAI_API_KEY'),
                model env AI_ASSISTANT_OPENAI_MODEL (default gpt-4.1-mini).
  2. Gemini   - google.genai, key via read_secret('GEMINI_API_KEY'),
                model env AI_ASSISTANT_GEMINI_MODEL (default gemini-3.5-flash).
  3. Anthropic - env ANTHROPIC_API_KEY or C:\\Users\\sam\\cena-secrets\\anthropic_api_key.txt,
                model env CENA_L3_ANTHROPIC_MODEL (default claude-haiku-4-5-20251001).

On this machine the Gemini key is 403-blocked, so the chain falls through to
Anthropic automatically. Everything is lazy-imported; nothing constructs a
client at module import. Tests inject their own callable and never touch these.
"""
from __future__ import annotations

import os
import time
import json
from pathlib import Path
from typing import Callable, Optional
import urllib.error
import urllib.request

DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
_ANTHROPIC_KEY_FILE = r"C:\Users\sam\cena-secrets\anthropic_api_key.txt"
_TRANSIENT = ("timeout", "timed out", "503", "502", "529", "overloaded", "rate limit")
_FATAL = (
    "permission",
    "api_key",
    "unauthorized",
    "401",
    "403",
    "invalid x-api-key",
    "blocked",
    "quota",
)


class CenaLlmError(RuntimeError):
    """No usable LLM provider, or all providers failed."""


def _read_key(name: str) -> Optional[str]:
    try:
        from cena_engine.assistant_routing_shared import read_secret

        val = read_secret(name)
        if val:
            return val.strip()
    except Exception:
        pass
    val = os.getenv(name)
    return val.strip() if val else None


# provider liveness for this process (None = untried, True = ok, False = dead)
_state: dict[str, Optional[bool]] = {"openai": None, "gemini": None, "anthropic": None}


def reset_providers() -> None:
    """Test/maintenance hook: forget which providers were marked dead."""
    _state["openai"] = None
    _state["gemini"] = None
    _state["anthropic"] = None


def _openai_complete(prompt: str, system: Optional[str], timeout_s: float) -> str:
    key = _read_key("OPENAI_API_KEY")
    if not key:
        raise CenaLlmError("no OpenAI key")
    model = os.getenv("AI_ASSISTANT_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    try:
        max_tokens = int(os.getenv("AI_ASSISTANT_OPENAI_MAX_TOKENS", "2048"))
    except ValueError:
        max_tokens = 2048
    try:
        temperature = float(os.getenv("AI_ASSISTANT_OPENAI_TEMPERATURE", "0.2"))
    except ValueError:
        temperature = 0.2
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max(128, min(max_tokens, 8192)),
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise CenaLlmError(f"OpenAI HTTP {exc.code}: {detail}") from exc
    text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
    text = (text or "").strip()
    if not text:
        raise CenaLlmError("empty OpenAI response")
    return text


def _gemini_complete(prompt: str, system: Optional[str], timeout_s: float) -> str:
    key = _read_key("GEMINI_API_KEY")
    if not key:
        raise CenaLlmError("no Gemini key")
    from google import genai  # lazy

    model = os.getenv("AI_ASSISTANT_GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    client = genai.Client(api_key=key, http_options={"timeout": int(timeout_s * 1000)})
    contents = prompt if not system else f"{system}\n\n{prompt}"
    resp = client.models.generate_content(model=model, contents=contents)
    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        raise CenaLlmError("empty Gemini response")
    return text


def _anthropic_complete(prompt: str, system: Optional[str], timeout_s: float) -> str:
    key = _read_key("ANTHROPIC_API_KEY")
    if not key and Path(_ANTHROPIC_KEY_FILE).exists():
        key = Path(_ANTHROPIC_KEY_FILE).read_text(encoding="utf-8").strip()
    if not key:
        raise CenaLlmError("no Anthropic key")
    import anthropic  # lazy

    model = os.getenv("CENA_L3_ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
    max_tokens = int(os.getenv("CENA_L3_ANTHROPIC_MAX_TOKENS", "2048"))
    client = anthropic.Anthropic(api_key=key, timeout=timeout_s)
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    msg = client.messages.create(**kwargs)
    parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
    text = "".join(parts).strip()
    if not text:
        raise CenaLlmError("empty Anthropic response")
    return text


_PROVIDERS: list[tuple[str, Callable[[str, Optional[str], float], str]]] = [
    ("openai", _openai_complete),
    ("gemini", _gemini_complete),
    ("anthropic", _anthropic_complete),
]


def _is_fatal(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(tok in msg for tok in _FATAL)


def _is_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(tok in msg for tok in _TRANSIENT)


def complete(prompt: str, *, system: Optional[str] = None, timeout_s: float = 25.0) -> str:
    """Return the model's text. Tries providers in order; marks a provider dead on
    a fatal (auth/permission/quota) error and falls through; one retry on transient
    errors. Raises CenaLlmError if no provider yields text."""
    errors: list[str] = []
    for name, fn in _PROVIDERS:
        if _state.get(name) is False:
            continue
        for attempt in (1, 2):
            try:
                text = fn(prompt, system, timeout_s)
                _state[name] = True
                return text
            except CenaLlmError as e:
                # configuration miss (no key / empty) - skip provider, not retry
                errors.append(f"{name}: {e}")
                break
            except Exception as e:  # provider SDK error
                if _is_fatal(e):
                    _state[name] = False
                    errors.append(f"{name}: fatal {type(e).__name__}: {e}")
                    break
                if _is_transient(e) and attempt == 1:
                    time.sleep(0.4)
                    continue
                errors.append(f"{name}: {type(e).__name__}: {e}")
                break
    raise CenaLlmError("all LLM providers failed: " + " | ".join(errors))


def get_default_llm() -> Callable[..., str]:
    """Return a callable with the complete() signature (the reasoner's default)."""
    return complete
