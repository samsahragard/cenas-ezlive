"""Hermetic tests for the provider-pluggable LLM client. No network: providers
are monkeypatched fakes."""
from __future__ import annotations

import pytest

import app.services.cena_llm as llm


@pytest.fixture(autouse=True)
def _reset():
    llm.reset_providers()
    yield
    llm.reset_providers()


def test_gemini_fatal_falls_through_to_anthropic(monkeypatch):
    def gemini(prompt, system, t):
        raise RuntimeError("403 PERMISSION_DENIED API_KEY_SERVICE_BLOCKED")

    def anthropic(prompt, system, t):
        return "ANSWER-FROM-ANTHROPIC"

    monkeypatch.setattr(llm, "_PROVIDERS", [("gemini", gemini), ("anthropic", anthropic)])
    out = llm.complete("hi")
    assert out == "ANSWER-FROM-ANTHROPIC"
    # gemini marked dead -> not retried on the next call
    assert llm._state["gemini"] is False


def test_dead_provider_skipped_second_call(monkeypatch):
    calls = {"gemini": 0, "anthropic": 0}

    def gemini(prompt, system, t):
        calls["gemini"] += 1
        raise RuntimeError("401 unauthorized")

    def anthropic(prompt, system, t):
        calls["anthropic"] += 1
        return "ok"

    monkeypatch.setattr(llm, "_PROVIDERS", [("gemini", gemini), ("anthropic", anthropic)])
    llm.complete("a")
    llm.complete("b")
    assert calls["gemini"] == 1  # not tried again after being marked dead
    assert calls["anthropic"] == 2


def test_transient_then_success_retries(monkeypatch):
    state = {"n": 0}

    def gemini(prompt, system, t):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("503 overloaded")
        return "recovered"

    monkeypatch.setattr(llm, "_PROVIDERS", [("gemini", gemini)])
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.complete("x") == "recovered"
    assert state["n"] == 2


def test_all_providers_fail_raises(monkeypatch):
    def boom(prompt, system, t):
        raise RuntimeError("403 blocked")

    monkeypatch.setattr(llm, "_PROVIDERS", [("gemini", boom), ("anthropic", boom)])
    with pytest.raises(llm.CenaLlmError):
        llm.complete("x")


def test_get_default_llm_is_complete():
    assert llm.get_default_llm() is llm.complete
