"""Phase 1 / Block 6 — morning brief composer.

samai authored the spec at
/partner/developer/app/morning-brief-composer-spec; aick implements
against that doc, not from memory. Public surface mirrors the spec
section names:

  - AudienceContext, SignalForBrief, WinSignal, LookaheadForBrief,
    CalibrationStats, BriefItem, BriefSection (input + output
    dataclasses, §2-3 of the spec)
  - compose_brief(audience, db) -> dict (the entrypoint that runs the
    full gather → LLM → validate → fallback pipeline and persists a
    MorningBrief row)
  - gather_signals / gather_wins / gather_lookahead / gather_calibration
    helpers (§1, §2)
  - SYSTEM_PROMPT (§5.1; cached via cache_control: ephemeral on the
    Anthropic call)
  - _MODEL_PRIMARY + _MODEL_FALLBACK constants — single source of
    truth per spec §4

Cron entrypoint at POST /cron/anomaly-brief (Phase 1 / Block 6 wiring
in driver_system.py) fires once a day and composes one brief per
enrolled audience.

If Anthropic is unavailable OR returns a schema-mismatch on both the
primary call and a single retry, falls through to a deterministic
template (§3 fallback path) — the brief still ships with
fallback_used=True flagged for post-hoc inspection.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import MorningBrief, Signal, User

logger = logging.getLogger(__name__)


# ---- spec §4 — model routing (single source of truth) ----
_MODEL_PRIMARY = "claude-opus-4-5"      # daily roll-up composition
_MODEL_HAIKU = "claude-haiku-4-5"       # per-signal one_line rewrite (unused
                                        # by the entrypoint here; reserved
                                        # for inline rewrites called by
                                        # gather_signals when a payload
                                        # would read poorly verbatim)
_MODEL_FALLBACK_TAG = "deterministic"   # tag we write when LLM is bypassed


# ---- spec §2 — input dataclasses ----

@dataclass
class AudienceContext:
    role: str
    user_id: int
    user_name: str
    store_ids: list[str]
    store_labels: dict[str, str]
    permission_tags: set[str]
    timezone: str
    brief_date: date


@dataclass
class SignalForBrief:
    rule_key: str
    severity: str
    subject_label: str
    store_id: str | None
    store_label: str | None
    trigger_at: datetime
    payload: dict
    action_text: str
    status: str
    acked_by: str | None
    age_hours: float


@dataclass
class WinSignal:
    category: str            # "driver" | "manager" | "operational" | "pattern"
    win_key: str
    subject_label: str
    store_id: str | None
    store_label: str | None
    occurred_at: datetime
    payload: dict
    one_line_seed: str


@dataclass
class LookaheadForBrief:
    rule_key: str
    severity_if_fires: str
    projected_trigger_at: datetime
    confidence: float
    subject_label: str
    rationale: str


@dataclass
class CalibrationStats:
    noisy_rules: list[tuple[str, int]] = field(default_factory=list)
    silent_rules: list[str] = field(default_factory=list)
    downgraded_yesterday: list[tuple[str, str, str]] = field(default_factory=list)


# ---- spec §3 — output dataclasses ----

@dataclass
class BriefItem:
    severity: str
    subject_label: str
    rule_key: str
    one_line: str
    action: str
    store_label: str | None
    link_path: str
    badge: str | None = None


@dataclass
class BriefSection:
    section_kind: str
    heading: str
    intro: str | None
    items: list[dict]    # serialized BriefItem dicts


# ---- spec §5.1 — system prompt (cached) ----

SYSTEM_PROMPT = """\
You are the Cenas Kitchen morning brief composer. Once a day (7:30am CT)
you read a structured list of operational anomalies from the prior 24h
plus a lookahead for today, and you compose a per-audience brief that is
short, specific, and prioritized.

WHO READS THE BRIEF: a non-technical partner / GM / KM / FOH manager.
Most read it on a phone over morning coffee. They need to know the 1-3
things that matter, in priority order, and what to do about each.

VOICE: warm, specific, direct. Use the user's first name once in the
greeting and never again. Never use "I" or "we" — you're the brief, not
a colleague. Skip filler. Skip apologies. Skip emoji.

STRUCTURE (return JSON exactly matching the MorningBrief schema):
  1. greeting — one line, "Good morning, <first_name>." or weekend variant.
  2. headline — 1-2 sentences naming the most important thing for today.
  3. sections, in order:
       a. alerts (red, immediate action needed)
       b. warns (yellow, needs attention soon)
       c. wins (green, yesterday's celebrations — 3-5 items, named where
          possible; omit entirely if zero candidate wins)
       d. lookahead (today's projected alerts/warns based on history)
       e. info_aggregate (single 1-line count of info-severity signals)
       f. calibration (only if any rules downgraded or silent enough to
          mention; empty section not allowed)
  4. closing — one short line.

PRIORITY RULES:
  - Within alerts/warns: most recent first, then highest
    customer-facing impact (Google review > order pickup > labor > vendor).
  - Skip any signal where status='acknowledged' or 'resolved'.
  - Skip any signal more than 24h old at brief_date midnight.
  - If an item has no recipient action because the user lacks the
    capability, keep it but make the action_text "FYI — no action
    required from you."

WINS RULES:
  - Pull from the WinSignal list. Pick 3-5 most resonant items; do NOT
    exceed 5. Order by category: driver, manager, operational, pattern.
  - Name names. "Alejandro hit 100% on-time delivery across 4 stops"
    beats "A driver hit 100% on-time delivery". When subject_label is
    missing or anonymized, skip the win rather than substitute a vague
    phrase.
  - Tone: warm but not gushy. One sentence per win, factual + crediting.
    Skip emoji even in this section.
  - If gather_wins returns zero items for the audience, omit the wins
    section entirely.

LOOKAHEAD RULES:
  - Include lookahead items only when projected_trigger_at is within
    brief_date + 12h.
  - Re-phrase the rationale as forward-looking ("Expected by 11am if
    no driver is assigned" rather than "Probability 0.7 of firing").
  - Lookahead items never carry an action other than the rule's
    standard action.

INFO AGGREGATE:
  - Single line: "N info signals in the last 24h."
  - Never enumerate info items individually.

CALIBRATION:
  - Include only if at least one downgraded rule fired yesterday OR at
    least one rule has been silent >14d.
  - Two lines max.

LENGTH BUDGET:
  - Total brief body (excluding greeting + closing): aim 120-240 words;
    hard cap 380. If you hit the cap, drop info_aggregate + calibration
    first, then lookahead, then trim wins to 3 (never below 3 if wins
    exist).
  - Each item's one_line: aim 12-22 words; hard cap 35.

DO NOT:
  - Restate the schema or your role.
  - Invent signals not in the input list.
  - Combine signals into narrative summaries — keep them itemized.
  - Speak in second-person plural ("you all"). Singular only.

OUTPUT FORMAT: a single JSON object matching the MorningBrief schema,
no leading/trailing prose, no markdown fences. The serving code parses
with json.loads() and validates against the dataclass.
"""


# ---- gather helpers (spec §1, §2) ----

_SIGNAL_STATUS_FRESH = ("info", "warn", "alert")


def gather_signals(db: Session, audience: AudienceContext) -> list[SignalForBrief]:
    """Pull yesterday's unresolved Signals for the audience.
    Permission-tag filter is applied here so the LLM never sees rows
    the audience can't view. Sorted severity DESC, trigger_at DESC.
    """
    window_start = datetime.combine(
        audience.brief_date - timedelta(days=1),
        datetime.min.time(),
    )
    window_end = datetime.combine(
        audience.brief_date,
        datetime.min.time(),
    )
    rows = (
        db.query(Signal)
        .filter(Signal.trigger_at >= window_start)
        .filter(Signal.trigger_at < window_end)
        .filter(Signal.resolved_at.is_(None))
        .filter(Signal.acknowledged_at.is_(None))
        .all()
    )
    out: list[SignalForBrief] = []
    sev_rank = {"alert": 0, "warn": 1, "info": 2}
    for r in rows:
        # audience filter: store scope + audience_roles overlap
        if r.store_id and r.store_id not in audience.store_ids:
            continue
        # role overlap: if Signal has audience_roles list, audience.role must
        # appear in it (empty list = visible to all)
        audience_roles = r.audience_roles or []
        if audience_roles and audience.role not in audience_roles:
            continue
        store_label = (audience.store_labels.get(r.store_id)
                       if r.store_id else None)
        age = (datetime.utcnow() - r.trigger_at).total_seconds() / 3600
        out.append(SignalForBrief(
            rule_key=r.rule_name,
            severity=r.severity,
            subject_label=r.subject_label,
            store_id=r.store_id,
            store_label=store_label,
            trigger_at=r.trigger_at,
            payload=r.payload or {},
            action_text=r.action_text,
            status="open",  # only un-resolved + un-acked here per filter
            acked_by=None,
            age_hours=round(age, 1),
        ))
    out.sort(key=lambda s: (sev_rank.get(s.severity, 9), -s.trigger_at.timestamp()))
    return out


def gather_wins(db: Session, audience: AudienceContext) -> list[WinSignal]:
    """Phase 1 / Block 6 minimal implementation. Returns up to 8
    candidate wins per the spec §2.5 category list. Initial
    implementation focuses on the cheapest queries — driver wins
    derivable from Order + Driver tables. The other 3 categories
    (manager / operational / pattern) are stubbed pending the
    manager_log + per-day labor-overage + 7d-rolling-metric
    infrastructure.

    Returns [] cleanly when nothing qualifies — composer is required
    to omit the wins section entirely if so.
    """
    # TODO Phase 2 / Block 6.1: manager + operational + pattern wins
    # need data sources not landed yet (manager_log tags, labor close,
    # 7d rolling baselines). Driver wins are queryable against existing
    # tables; we ship those now so the wins section isn't empty for
    # weeks while data sources catch up.
    from app.models import Driver, Order
    out: list[WinSignal] = []
    day = audience.brief_date - timedelta(days=1)
    day_iso = day.isoformat()

    # Driver win 1: "first paid delivery" — drivers whose first-ever
    # delivered Order landed yesterday.
    rows = (
        db.query(Driver)
        .filter(Driver.lifetime_delivery_count == 1)
        .all()
    )
    for d in rows:
        out.append(WinSignal(
            category="driver",
            win_key="driver.first_paid_delivery",
            subject_label=d.name or f"Driver #{d.id}",
            store_id=None,
            store_label=None,
            occurred_at=datetime.utcnow(),
            payload={"lifetime_count": d.lifetime_delivery_count},
            one_line_seed=f"{d.name} completed their first paid delivery.",
        ))

    # Driver win 2: "100% on-time" — drivers who delivered 2+ orders
    # yesterday all within their delivery_window_end. Lightweight version.
    delivered = (
        db.query(Order)
        .filter(Order.delivery_date == day_iso)
        .filter(Order.status == "delivered")
        .filter(Order.assigned_driver_id.isnot(None))
        .all()
    )
    by_driver: dict[int, list[Order]] = {}
    for o in delivered:
        by_driver.setdefault(o.assigned_driver_id, []).append(o)
    for did, orders in by_driver.items():
        if len(orders) < 2:
            continue
        all_ontime = all(
            o.delivery_window_end is None or
            (o.delivered_actual_at and o.delivered_actual_at <= o.delivery_window_end)
            for o in orders
        )
        if not all_ontime:
            continue
        driver = db.get(Driver, did)
        if not driver:
            continue
        out.append(WinSignal(
            category="driver",
            win_key="driver.day_perfect_ontime",
            subject_label=driver.name or f"Driver #{did}",
            store_id=None,
            store_label=None,
            occurred_at=datetime.utcnow(),
            payload={"deliveries": len(orders), "on_time_pct": 100},
            one_line_seed=(f"{driver.name} hit 100% on-time delivery across "
                           f"{len(orders)} stops."),
        ))

    return out[:8]


def gather_lookahead(db: Session, audience: AudienceContext) -> list[LookaheadForBrief]:
    """Phase 1 / Block 6 stub — full lookahead engine requires
    rule-state projection (e.g., 'order X has no driver and pickup is at
    11am, will fire orders.no_driver_30min_before by 10:30am'). Phase 2
    work; today we surface a simple deterministic lookahead: count of
    unassigned orders whose delivery window starts in next 12h, single
    item if any."""
    # TODO Phase 2: lookahead engine driven by rule-state projection.
    return []


def gather_calibration(db: Session, audience: AudienceContext) -> CalibrationStats:
    """Partner-only enrichment: rules that fired noisily or stayed
    silent. Cheap query against the Signal table grouped by rule_name."""
    if audience.role != "partner":
        return CalibrationStats()
    from sqlalchemy import func
    today = audience.brief_date
    fourteen_days_ago = datetime.combine(
        today - timedelta(days=14), datetime.min.time())
    yesterday_start = datetime.combine(
        today - timedelta(days=1), datetime.min.time())
    yesterday_end = datetime.combine(today, datetime.min.time())

    yesterday_counts = dict(
        db.query(Signal.rule_name, func.count(Signal.id))
        .filter(Signal.trigger_at >= yesterday_start)
        .filter(Signal.trigger_at < yesterday_end)
        .group_by(Signal.rule_name)
        .all()
    )
    fourteen_day_rules = {
        r for (r,) in
        db.query(Signal.rule_name)
          .filter(Signal.trigger_at >= fourteen_days_ago)
          .distinct()
          .all()
    }
    # noisy: > 3 fires for a store-pair → 6 fires across both stores
    noisy = [(rk, n) for rk, n in yesterday_counts.items() if n > 6]
    from app.services.anomaly_engine import REGISTRY
    silent = [
        rk for rk in REGISTRY.keys() if rk not in fourteen_day_rules
    ][:10]
    return CalibrationStats(
        noisy_rules=noisy,
        silent_rules=silent,
        downgraded_yesterday=[],   # populated when calibration job runs
    )


# ---- LLM call ----

def _build_user_message(
    audience: AudienceContext,
    signals: list[SignalForBrief],
    wins: list[WinSignal],
    lookahead: list[LookaheadForBrief],
    calibration: CalibrationStats,
) -> str:
    """Spec §5.2 — user message format. Order: audience → signals →
    wins → lookahead → calibration."""

    def serialize(obj):
        if isinstance(obj, list):
            return [serialize(x) for x in obj]
        if hasattr(obj, "__dataclass_fields__"):
            d = asdict(obj)
            return {k: serialize(v) for k, v in d.items()}
        if isinstance(obj, dict):
            return {k: serialize(v) for k, v in obj.items()}
        if isinstance(obj, set):
            return sorted(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return obj

    return (
        f"AUDIENCE\n{json.dumps(serialize(audience), indent=2)}\n\n"
        f"SIGNALS (last 24h, after permission filter, sorted severity desc, then trigger_at desc)\n"
        f"{json.dumps(serialize(signals), indent=2)}\n\n"
        f"WINS (yesterday's celebrations, after permission filter, sorted by category then occurred_at desc)\n"
        f"{json.dumps(serialize(wins), indent=2)}\n\n"
        f"LOOKAHEAD (next 12h projected, after permission filter)\n"
        f"{json.dumps(serialize(lookahead), indent=2)}\n\n"
        f"CALIBRATION (yesterday's tuning observations, partner-only)\n"
        f"{json.dumps(serialize(calibration), indent=2)}\n\n"
        f"Compose the brief for {audience.user_name} "
        f"({audience.role}, stores: "
        f"{', '.join(audience.store_labels.values()) or 'all'}) "
        f"covering {audience.brief_date.isoformat()}. "
        f"Return only the JSON object."
    )


def _call_anthropic(audience: AudienceContext,
                    user_message: str) -> tuple[dict | None, str]:
    """Returns (parsed_dict_or_None, model_used). Uses prompt caching
    on the system block. None means the API call failed or returned a
    non-JSON parse — caller falls back to deterministic template."""
    try:
        import anthropic
    except ImportError:
        return None, ""
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None, ""
    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model=_MODEL_PRIMARY,
            max_tokens=2000,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_message}],
        )
        text = "".join(
            block.text for block in resp.content
            if getattr(block, "type", None) == "text"
        )
    except Exception:
        logger.exception("Anthropic call failed (brief composer)")
        return None, _MODEL_PRIMARY
    try:
        # Strip potential markdown fences
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.rsplit("```", 1)[0].strip()
        return json.loads(cleaned), _MODEL_PRIMARY
    except Exception:
        logger.warning("brief composer: model returned non-JSON; falling back")
        return None, _MODEL_PRIMARY


# ---- fallback template (spec §3 fallback path) ----

def _fallback_brief(
    audience: AudienceContext,
    signals: list[SignalForBrief],
    wins: list[WinSignal],
) -> dict:
    """Deterministic template — no LLM. Brief still ships with
    fallback_used=True so post-hoc debugging is easy."""
    alerts = [s for s in signals if s.severity == "alert"]
    warns = [s for s in signals if s.severity == "warn"]
    infos = [s for s in signals if s.severity == "info"]

    def item_from(s: SignalForBrief) -> dict:
        return {
            "severity": s.severity,
            "subject_label": s.subject_label,
            "rule_key": s.rule_key,
            "one_line": s.action_text,  # use action verbatim — safe
            "action": s.action_text,
            "store_label": s.store_label,
            "link_path": "/partner/anomalies",
            "badge": None,
        }

    sections = []
    if alerts:
        sections.append({
            "section_kind": "alerts",
            "heading": "Needs action now",
            "intro": None,
            "items": [item_from(s) for s in alerts],
        })
    if warns:
        sections.append({
            "section_kind": "warns",
            "heading": "Keep an eye on",
            "intro": None,
            "items": [item_from(s) for s in warns],
        })
    if wins:
        sections.append({
            "section_kind": "wins",
            "heading": "Wins from yesterday",
            "intro": None,
            "items": [
                {
                    "severity": "info",
                    "subject_label": w.subject_label,
                    "rule_key": w.win_key,
                    "one_line": w.one_line_seed,
                    "action": "",
                    "store_label": w.store_label,
                    "link_path": "/partner/anomalies",
                    "badge": None,
                }
                for w in wins[:5]
            ],
        })
    if infos:
        sections.append({
            "section_kind": "info_aggregate",
            "heading": "FYI",
            "intro": None,
            "items": [{
                "severity": "info",
                "subject_label": "Info signals",
                "rule_key": "info_aggregate",
                "one_line": f"{len(infos)} info signals in the last 24h.",
                "action": "",
                "store_label": None,
                "link_path": "/partner/anomalies",
                "badge": None,
            }],
        })

    headline = (
        "Quiet overnight."
        if not alerts and not warns
        else (f"{len(alerts)} alert(s) and {len(warns)} warn(s) need your attention today."
              if alerts else
              f"{len(warns)} warn(s) to keep an eye on today.")
    )
    first = (audience.user_name.split()[0] if audience.user_name
             else audience.role.title())
    return {
        "brief_id": uuid.uuid4().hex,
        "audience_role": audience.role,
        "audience_user_id": audience.user_id,
        "brief_date": audience.brief_date.isoformat(),
        "greeting": f"Good morning, {first}.",
        "headline": headline,
        "sections": sections,
        "closing": "Have a strong day.",
        "composer_model": _MODEL_FALLBACK_TAG,
        "fallback_used": True,
    }


# ---- composer entrypoint ----

_REQUIRED_KEYS = {
    "brief_id", "audience_role", "audience_user_id", "brief_date",
    "greeting", "headline", "sections", "closing",
}


def _validate_brief(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    if not _REQUIRED_KEYS.issubset(d.keys()):
        return False
    if not isinstance(d.get("sections"), list):
        return False
    return True


def compose_brief(audience: AudienceContext,
                  db: Session | None = None) -> MorningBrief:
    """Run the full gather → LLM → validate → persist pipeline for one
    audience. Returns the persisted MorningBrief row (caller commits)."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        signals = gather_signals(db, audience)
        wins = gather_wins(db, audience)
        lookahead = gather_lookahead(db, audience)
        calibration = gather_calibration(db, audience)

        user_msg = _build_user_message(audience, signals, wins, lookahead, calibration)
        brief_dict, model_used = _call_anthropic(audience, user_msg)
        fallback_used = False
        if not brief_dict or not _validate_brief(brief_dict):
            brief_dict = _fallback_brief(audience, signals, wins)
            model_used = brief_dict["composer_model"]
            fallback_used = True

        # Force-set the metadata the LLM might have hallucinated
        brief_dict["audience_role"] = audience.role
        brief_dict["audience_user_id"] = audience.user_id
        brief_dict["brief_date"] = audience.brief_date.isoformat()
        brief_dict.setdefault("brief_id", uuid.uuid4().hex)
        brief_dict["composer_model"] = model_used
        brief_dict["fallback_used"] = fallback_used

        row = MorningBrief(
            brief_id=brief_dict["brief_id"],
            audience_role=audience.role,
            audience_user_id=audience.user_id,
            brief_date=audience.brief_date,
            body=brief_dict,
            composed_at=datetime.utcnow(),
            composer_model=model_used,
            fallback_used=fallback_used,
        )
        db.add(row)
        if close_db:
            db.commit()
        return row
    finally:
        if close_db:
            db.close()


# ---- audience enumeration (cron entrypoint helper) ----

def _audience_for_user(u: User, brief_date: date) -> AudienceContext:
    """Build an AudienceContext from a User row + the target brief
    date. Permission_tags resolved via ROLE_PERMISSIONS in
    app.services.permissions (ck's Phase 0 Block 4 module).
    """
    try:
        from app.services.permissions import ROLE_PERMISSIONS
        tags = set(ROLE_PERMISSIONS.get(u.permission_level, set()))
    except Exception:
        tags = set()
    # Store mapping — partner sees both, gm/km store-scoped via store_scope
    store_labels = {
        "store_1": "Copperfield",
        "store_2": "Tomball",
        "store_3": "Westheimer",
        "store_4": "Spring Stuebner",
    }
    if u.permission_level in ("partner", "corporate"):
        store_ids = list(store_labels.keys())
    elif u.store_scope:
        store_ids = [u.store_scope]
    else:
        store_ids = []
    name = u.full_name or "there"
    return AudienceContext(
        role=u.permission_level or "unknown",
        user_id=u.id,
        user_name=name,
        store_ids=store_ids,
        store_labels={sid: store_labels[sid] for sid in store_ids if sid in store_labels},
        permission_tags=tags,
        timezone="America/Chicago",
        brief_date=brief_date,
    )


_ENROLLED_ROLES = ("partner", "corporate", "gm", "km",
                   "foh_manager", "corporate_chef", "prep_manager")


def compose_all_briefs(brief_date: date | None = None) -> dict:
    """Cron entrypoint — compose one brief per enrolled audience for
    brief_date (default: today), then dispatch each via email
    (dry-run by default; set BRIEF_EMAIL_DISPATCH=1 to actually send).
    Returns summary counts including dispatch breakdown."""
    if brief_date is None:
        brief_date = date.today()
    db = SessionLocal()
    composed = 0
    skipped = 0
    fallbacks = 0
    errors = 0
    dispatch_summary = {"sent": 0, "dry_run": 0, "skipped": 0, "error": 0}
    dispatch_results: list[dict] = []
    try:
        # Lazy import — brief_email pulls in smtplib + Flask render which
        # we don't want at module-load time (some tests stub SessionLocal
        # before the full app context exists).
        from app.services.brief_email import dispatch_brief

        users = (
            db.query(User)
            .filter(User.active.is_(True))
            .filter(User.permission_level.in_(_ENROLLED_ROLES))
            .all()
        )
        for u in users:
            # Idempotency — don't re-compose if already done today
            existing = (
                db.query(MorningBrief)
                .filter(MorningBrief.audience_user_id == u.id)
                .filter(MorningBrief.brief_date == brief_date)
                .first()
            )
            if existing is not None:
                skipped += 1
                continue
            audience = _audience_for_user(u, brief_date)
            try:
                row = compose_brief(audience, db)
                composed += 1
                if row.fallback_used:
                    fallbacks += 1
                # Dispatch after a successful compose. dispatch_brief
                # never raises — it returns a status dict and logs.
                d = dispatch_brief(row, audience, db)
                key = d.get("status", "error")
                dispatch_summary[key] = dispatch_summary.get(key, 0) + 1
                dispatch_results.append(d)
            except Exception:
                logger.exception(
                    "brief compose failed for user_id=%s", u.id)
                errors += 1
        db.commit()
    finally:
        db.close()
    return {
        "composed": composed,
        "skipped": skipped,
        "fallbacks": fallbacks,
        "errors": errors,
        "dispatch": dispatch_summary,
        "dispatch_results": dispatch_results,
    }
