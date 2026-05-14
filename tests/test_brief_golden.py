"""Phase 1 / Block 6 follow-up — golden fixtures for morning brief.

The fixtures live in tests/fixtures/morning_brief/ as JSON files, each
with an `input` (audience + signals + wins) and `expected` (structural
assertions). Tests parameterize over every *.json in that directory so
adding a new scenario is just a matter of dropping a new file.

What we assert (per spec §13 review checklist items 8-12):
  - Output passes _validate_brief()
  - Section ordering matches expected sequence
  - Section item counts match expected
  - Headline contains at least one of the expected substrings
  - Greeting matches expected prefix
  - Closing matches expected
  - No item.one_line exceeds 35 words (spec §5.1 length budget)
  - Total brief body word count within [min_total_words, max_total_words]

We use the deterministic fallback path so the tests run without
Anthropic. The LLM path is tested separately (and softly — the spec
explicitly says we DON'T golden the LLM output verbatim, only that it
parses + obeys structure).

samai review hook: every fixture file is self-documenting via the
top-level `scenario` + `description` keys.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app.services.brief_composer import (
    AudienceContext,
    SignalForBrief,
    WinSignal,
    _fallback_brief,
    _validate_brief,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "morning_brief"

# Section ordering per spec §5.1 STRUCTURE: alerts, warns, wins,
# lookahead, info_aggregate, calibration.
_SECTION_ORDER = ["alerts", "warns", "wins", "lookahead",
                  "info_aggregate", "calibration"]


def _all_fixtures() -> list[Path]:
    if not FIXTURE_DIR.exists():
        return []
    return sorted(FIXTURE_DIR.glob("*.json"))


def _load_fixture(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _hydrate_audience(d: dict) -> AudienceContext:
    return AudienceContext(
        role=d["role"],
        user_id=d["user_id"],
        user_name=d["user_name"],
        store_ids=list(d["store_ids"]),
        store_labels=dict(d["store_labels"]),
        permission_tags=set(d["permission_tags"]),
        timezone=d["timezone"],
        brief_date=date.fromisoformat(d["brief_date"]),
    )


def _hydrate_signal(d: dict) -> SignalForBrief:
    return SignalForBrief(
        rule_key=d["rule_key"],
        severity=d["severity"],
        subject_label=d["subject_label"],
        store_id=d.get("store_id"),
        store_label=d.get("store_label"),
        trigger_at=datetime.fromisoformat(d["trigger_at"]),
        payload=d.get("payload", {}),
        action_text=d["action_text"],
        status=d.get("status", "open"),
        acked_by=d.get("acked_by"),
        age_hours=d.get("age_hours", 0.0),
    )


def _hydrate_win(d: dict) -> WinSignal:
    return WinSignal(
        category=d["category"],
        win_key=d["win_key"],
        subject_label=d["subject_label"],
        store_id=d.get("store_id"),
        store_label=d.get("store_label"),
        occurred_at=datetime.fromisoformat(d["occurred_at"]),
        payload=d.get("payload", {}),
        one_line_seed=d["one_line_seed"],
    )


def _word_count(text: str) -> int:
    return len([w for w in (text or "").split() if w.strip()])


def _total_body_words(brief: dict) -> int:
    """Words across headline + all section items' one_line + action.
    Excludes greeting + closing (per spec §5.1 'length budget')."""
    n = _word_count(brief.get("headline", ""))
    for sec in brief.get("sections", []):
        for item in sec.get("items", []):
            n += _word_count(item.get("one_line", ""))
            n += _word_count(item.get("action", ""))
    return n


# ---- the actual parameterized test ----

_FIXTURES = _all_fixtures()


def test_fixture_dir_is_populated():
    """Belt-and-suspenders: if someone deletes the fixtures dir, the
    parameterized test below would silently pass with zero cases. This
    one fails loudly."""
    assert _FIXTURES, f"No fixtures found in {FIXTURE_DIR}"
    assert len(_FIXTURES) >= 4, (
        f"Spec asks for 4-6 frozen scenarios; found {len(_FIXTURES)}")


@pytest.mark.parametrize("fixture_path", _FIXTURES, ids=lambda p: p.stem)
def test_golden_fixture(fixture_path: Path):
    spec = _load_fixture(fixture_path)
    inp = spec["input"]
    exp = spec["expected"]

    audience = _hydrate_audience(inp["audience"])
    signals = [_hydrate_signal(s) for s in inp.get("signals", [])]
    wins = [_hydrate_win(w) for w in inp.get("wins", [])]

    brief = _fallback_brief(audience, signals, wins)

    # 1. Schema validates
    assert _validate_brief(brief), (
        f"{fixture_path.name}: _validate_brief rejected the output")

    # 2. Section ordering — kinds match expected verbatim
    actual_kinds = [s["section_kind"] for s in brief["sections"]]
    expected_kinds = exp["section_kinds"]
    assert actual_kinds == expected_kinds, (
        f"{fixture_path.name}: section_kinds mismatch. "
        f"expected={expected_kinds} actual={actual_kinds}")

    # 3. Section ordering also obeys the spec-defined canonical order
    # (independent check — guards against future expected_kinds typos)
    last_pos = -1
    for k in actual_kinds:
        pos = _SECTION_ORDER.index(k)
        assert pos > last_pos, (
            f"{fixture_path.name}: section {k!r} appears out of canonical "
            f"order {_SECTION_ORDER}")
        last_pos = pos

    # 4. Section item counts match expected
    for kind, expected_count in exp.get("section_counts", {}).items():
        sec = next((s for s in brief["sections"] if s["section_kind"] == kind), None)
        assert sec is not None, f"{fixture_path.name}: missing {kind!r} section"
        assert len(sec["items"]) == expected_count, (
            f"{fixture_path.name}: {kind!r} item count mismatch. "
            f"expected={expected_count} actual={len(sec['items'])}")

    # 5. Headline assertion
    headline = brief["headline"]
    contains_options = exp["headline_contains_any_of"]
    assert any(opt.lower() in headline.lower() for opt in contains_options), (
        f"{fixture_path.name}: headline {headline!r} doesn't contain any of "
        f"{contains_options}")

    # 6. Greeting + closing
    assert brief["greeting"].startswith(exp["greeting_prefix"]), (
        f"{fixture_path.name}: greeting {brief['greeting']!r} doesn't start "
        f"with {exp['greeting_prefix']!r}")
    assert brief["closing"] == exp["closing"], (
        f"{fixture_path.name}: closing mismatch")
    assert brief["fallback_used"] is exp["fallback_used"]

    # 7. Each item.one_line ≤ 35 words (spec §5.1)
    max_words = exp.get("max_words_per_one_line", 35)
    for sec in brief["sections"]:
        for item in sec["items"]:
            wc = _word_count(item.get("one_line", ""))
            assert wc <= max_words, (
                f"{fixture_path.name}: item.one_line in {sec['section_kind']!r} "
                f"is {wc} words (cap {max_words}). text={item['one_line']!r}")

    # 8. Total body word count within bounds
    total = _total_body_words(brief)
    assert exp["min_total_words"] <= total <= exp["max_total_words"], (
        f"{fixture_path.name}: total body {total} words outside "
        f"[{exp['min_total_words']}, {exp['max_total_words']}]")


# ---- ordering invariant tests (independent of fixtures) ----

def test_canonical_section_order_constants_match_spec():
    """If the spec ever reorders, this test fails first."""
    assert _SECTION_ORDER == ["alerts", "warns", "wins", "lookahead",
                              "info_aggregate", "calibration"]
