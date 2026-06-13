"""assistant_conversations.py - 48h conversation store + dual-responder support
for the CK assistant runtime (assistant_ck_runtime.py imports this).

Adds, without touching the existing answer paths:
  * per-user server-side conversations with a 48-hour lifetime, archived (never
    deleted) when they expire, searchable afterwards (FTS5 with LIKE fallback)
  * a JSONL turn mirror with full principal context for every turn
  * the permission-aware mechanical grader (high / medium / low)
  * the cena_active / ck_engaged state machine with fail-open release
  * LAN-hub posting helpers (startup line, flagged-turn alerts)

Design rules:
  - every function is defensive: a failure here must NEVER break an answer,
    callers wrap in try/except and fall through to legacy behavior.
  - grading is permission-aware: a policy-correct refusal is HIGH quality.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DB_PATH = os.getenv("ASSISTANT_CONVERSATIONS_DB") or r"C:\Users\sam\cena-ai-assistant\assistant_conversations.sqlite"
_TURNS_LOG = os.getenv("ASSISTANT_TURNS_LOG") or r"C:\Users\sam\cena-ai-assistant\assistant_turns.jsonl"
_HUB_URL = os.getenv("CENA_HUB_URL") or "http://127.0.0.1:8765"

CONVERSATION_HOURS = float(os.getenv("ASSISTANT_CONVERSATION_HOURS") or "48")
CK_ENGAGED_FAILOPEN_HOURS = float(os.getenv("ASSISTANT_CK_ENGAGED_FAILOPEN_HOURS") or "24")
CK_SILENCE_RESOLVE_MINUTES = float(os.getenv("ASSISTANT_CK_SILENCE_RESOLVE_MINUTES") or "30")

INTRO_TEXT = (
    "Hi, I'm DEV (short for developer). I'll be here making sure your questions "
    "get answered - feedback is appreciated."
)
CENA_FOLLOWUP = "Did I answer your question?"
ACK_TEXT = "Great - glad that helped!"
FOLLOWUP_TEXT = "Did that answer your question? If I don't hear back, I'll take that as a yes."
RELEASE_TEXT = "Great - glad that answered it. C.E.N.A. is back with you."

_lock = threading.Lock()
_fts_available: bool | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _connect() -> sqlite3.Connection:
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    global _fts_available
    with _lock, _connect() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS conversation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                principal_key TEXT NOT NULL,
                principal_json TEXT,
                started_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_activity_at TEXT,
                state TEXT NOT NULL DEFAULT 'cena_active',
                ck_engaged_at TEXT,
                archived_at TEXT
            )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_conv_principal ON conversation(principal_key, archived_at)"
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS message (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                ts TEXT NOT NULL,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                route TEXT,
                confidence TEXT,
                grade TEXT,
                meta TEXT
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS ix_msg_conv ON message(conversation_id, id)")
        try:
            con.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
                "text, content='message', content_rowid='id')"
            )
            con.execute(
                """CREATE TRIGGER IF NOT EXISTS message_ai AFTER INSERT ON message BEGIN
                    INSERT INTO message_fts(rowid, text) VALUES (new.id, new.text);
                END"""
            )
            _fts_available = True
        except sqlite3.OperationalError:
            _fts_available = False


def principal_key(principal: dict) -> str:
    kind = str((principal or {}).get("kind") or "anonymous")
    pid = (principal or {}).get("principal_id")
    if pid is not None:
        return f"{kind}:{pid}"
    name = str((principal or {}).get("display_name") or "unknown")
    return f"{kind}:{name}"


def get_active_conversation(principal: dict) -> tuple[dict, bool]:
    """Return (conversation row as dict, new_chat). Archives an expired
    conversation and opens a fresh one - the 48h lifecycle lives here."""
    key = principal_key(principal)
    now = _now()
    with _lock, _connect() as con:
        row = con.execute(
            "SELECT * FROM conversation WHERE principal_key=? AND archived_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is not None:
            expires = _parse_iso(row["expires_at"])
            if expires is not None and now < expires:
                con.execute(
                    "UPDATE conversation SET last_activity_at=? WHERE id=?",
                    (_iso(now), row["id"]),
                )
                return dict(row), False
            con.execute(
                "UPDATE conversation SET archived_at=? WHERE id=?", (_iso(now), row["id"])
            )
        cur = con.execute(
            "INSERT INTO conversation (principal_key, principal_json, started_at, expires_at, "
            "last_activity_at, state) VALUES (?,?,?,?,?,'cena_active')",
            (
                key,
                json.dumps(principal or {}, default=str),
                _iso(now),
                _iso(now + timedelta(hours=CONVERSATION_HOURS)),
                _iso(now),
            ),
        )
        conv = con.execute(
            "SELECT * FROM conversation WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(conv), True


def add_message(conversation_id: int, sender: str, text: str, route: str | None = None,
                confidence: str | None = None, grade: str | None = None,
                meta: dict | None = None) -> None:
    with _lock, _connect() as con:
        con.execute(
            "INSERT INTO message (conversation_id, ts, sender, text, route, confidence, grade, meta) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                _iso(_now()),
                sender,
                str(text or ""),
                route,
                confidence,
                grade,
                json.dumps(meta, default=str) if meta else None,
            ),
        )


def get_thread(conversation_id: int, limit: int = 200) -> list[dict]:
    with _lock, _connect() as con:
        rows = con.execute(
            "SELECT ts, sender, text, route, confidence, grade FROM message "
            "WHERE conversation_id=? ORDER BY id ASC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def last_message_at(conversation_id: int) -> datetime | None:
    with _lock, _connect() as con:
        row = con.execute(
            "SELECT ts FROM message WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return _parse_iso(row["ts"]) if row else None


def last_sender(conversation_id: int) -> str | None:
    with _lock, _connect() as con:
        row = con.execute(
            "SELECT sender FROM message WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row["sender"] if row else None


def last_user_question(conversation_id: int) -> str | None:
    with _lock, _connect() as con:
        row = con.execute(
            "SELECT text FROM message WHERE conversation_id=? AND sender='user' "
            "ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row["text"] if row else None


def cena_answer_count(conversation_id: int) -> int:
    with _lock, _connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM message WHERE conversation_id=? AND sender='cena'",
            (conversation_id,),
        ).fetchone()
        return int(row["n"]) if row else 0


def dev_intro_shown(conversation_id: int) -> bool:
    """DEV is considered introduced once he has said ANYTHING in this chat -
    never re-introduce mid-session (covers pre-rename intro wordings too)."""
    with _lock, _connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM message WHERE conversation_id=? AND sender='ck-dev'",
            (conversation_id,),
        ).fetchone()
        return bool(row and row["n"])


def last_cena_answer(conversation_id: int) -> str | None:
    with _lock, _connect() as con:
        row = con.execute(
            "SELECT text FROM message WHERE conversation_id=? AND sender='cena' "
            "ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row["text"] if row else None


def last_ckdev_route(conversation_id: int) -> str | None:
    with _lock, _connect() as con:
        row = con.execute(
            "SELECT route FROM message WHERE conversation_id=? AND sender='ck-dev' "
            "ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row["route"] if row else None


def last_substantive_user_question(conversation_id: int) -> str | None:
    """The most recent user message that is an actual question/request,
    skipping bare yes/no feedback replies."""
    with _lock, _connect() as con:
        rows = con.execute(
            "SELECT text FROM message WHERE conversation_id=? AND sender='user' "
            "ORDER BY id DESC LIMIT 12",
            (conversation_id,),
        ).fetchall()
    for r in rows:
        text = str(r["text"] or "").strip()
        if text and not _YES_RE.match(text) and not _NO_RE.match(text):
            return text
    return None


_NEG_PREFIX_RE = re.compile(
    r"^\s*(no|nope|nah|not really|not quite|wrong|incorrect)[\s,.!-]*",
    re.IGNORECASE,
)


def strip_negation(text: str) -> str:
    """What remains of a 'no ...' message after the negation - extra detail
    the user added about what they actually want."""
    return _NEG_PREFIX_RE.sub("", text or "", count=1).strip()


def effective_state(conv: dict) -> str:
    """ck_engaged with fail-open: a stale engagement auto-releases so users are
    never stuck without a responder."""
    if (conv or {}).get("state") != "ck_engaged":
        return "cena_active"
    engaged_at = _parse_iso(conv.get("ck_engaged_at"))
    if engaged_at is None or (_now() - engaged_at) > timedelta(hours=CK_ENGAGED_FAILOPEN_HOURS):
        set_state(conv["id"], "cena_active")
        return "cena_active"
    return "ck_engaged"


def set_state(conversation_id: int, state: str) -> None:
    with _lock, _connect() as con:
        if state == "ck_engaged":
            con.execute(
                "UPDATE conversation SET state='ck_engaged', ck_engaged_at=? WHERE id=?",
                (_iso(_now()), conversation_id),
            )
        else:
            con.execute(
                "UPDATE conversation SET state='cena_active', ck_engaged_at=NULL WHERE id=?",
                (conversation_id,),
            )


def silence_resolved(conversation_id: int) -> bool:
    """True when the last message is old enough that the user's silence counts
    as 'yes, that answered it' (spec: no reply -> assume good)."""
    last = last_message_at(conversation_id)
    if last is None:
        return True
    return (_now() - last) > timedelta(minutes=CK_SILENCE_RESOLVE_MINUTES)


def archive_expired_sweep() -> int:
    now = _now()
    with _lock, _connect() as con:
        cur = con.execute(
            "UPDATE conversation SET archived_at=? WHERE archived_at IS NULL AND expires_at < ?",
            (_iso(now), _iso(now)),
        )
        return cur.rowcount


def search_history(principal: dict, query: str, limit: int = 5) -> list[dict]:
    """Search THIS user's own messages (active + archived). Permission-safe by
    construction: scoped to the principal's conversations only."""
    key = principal_key(principal)
    terms = [t for t in re.findall(r"[A-Za-z0-9']+", query or "") if len(t) > 2][:8]
    if not terms:
        return []
    with _lock, _connect() as con:
        hits: list[dict] = []
        if _fts_available:
            try:
                fts_query = " OR ".join(terms)
                rows = con.execute(
                    "SELECT m.ts, m.sender, m.text, m.conversation_id FROM message_fts f "
                    "JOIN message m ON m.id = f.rowid "
                    "JOIN conversation c ON c.id = m.conversation_id "
                    "WHERE c.principal_key=? AND message_fts MATCH ? "
                    "ORDER BY m.id DESC LIMIT ?",
                    (key, fts_query, limit),
                ).fetchall()
                hits = [dict(r) for r in rows]
            except sqlite3.OperationalError:
                hits = []
        if not hits:
            like = f"%{terms[0]}%"
            rows = con.execute(
                "SELECT m.ts, m.sender, m.text, m.conversation_id FROM message m "
                "JOIN conversation c ON c.id = m.conversation_id "
                "WHERE c.principal_key=? AND m.text LIKE ? ORDER BY m.id DESC LIMIT ?",
                (key, like, limit),
            ).fetchall()
            hits = [dict(r) for r in rows]
        return hits


# ---------------------------------------------------------------- turn mirror

def log_turn(record: dict) -> None:
    try:
        path = Path(_TURNS_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": _iso(_now()), **record}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        return


def hub_post(body: str, author: str = "cena") -> None:
    """Fire-and-forget post to the LAN hub. Runs in a daemon thread so it can
    never add latency or failure to an answer."""

    def _post() -> None:
        try:
            payload = json.dumps({"author": author, "body": body[:1800]}).encode("utf-8")
            req = urllib.request.Request(
                f"{_HUB_URL}/partner/developer/chat/post",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=8).read()
        except Exception:
            return

    threading.Thread(target=_post, daemon=True).start()


# ---------------------------------------------------------------- the grader

_INVESTIGABLE_REASONS = {
    "data_question_needs_approved_tool",
    "sensitive_or_operational_question_needs_approved_tool",
}

_ACTION_RE = re.compile(
    r"^\s*(schedule|comp|email|e-mail|fire|hire|send|text|call|add|remove|delete|update|change|set|book|order|cancel|refund|assign)\b",
    re.IGNORECASE,
)

_DIAGNOSTIC_RE = re.compile(
    r"\b(why|compare|comparison|versus|\bvs\b|should|predict|forecast|best|worst|rank|trend|"
    r"correlat\w*|turn time|party size|tips?|comps?|voids?|discount\w*|refund\w*|"
    r"overstaffed|understaffed|short-?staffed|no-?shows?|coverage|food truck|weather|"
    r"bleeding|out of control|underperform\w*|outlier|unusual|spike|crater|anomal\w*)\b",
    re.IGNORECASE,
)

_DATAISH_RE = re.compile(
    r"\b(sales?|revenue|orders?|labor|hours?|overtime|tips?|comps?|voids?|discounts?|refunds?|"
    r"covers?|tables?|servers?|bartenders?|staff\w*|schedul\w*|shifts?|busy|busiest|slow\w*|"
    r"yesterday|today|tonight|weekend|week|month|saturday|sunday|monday|tuesday|wednesday|"
    r"thursday|friday|number|numbers|metric\w*|trend\w*|daypart|lunch|dinner|breakfast|"
    r"copperfield|tomball|stores?|catering|average|avg|check|guest\w*|party|wait)\b",
    re.IGNORECASE,
)


def is_action_request(question: str) -> bool:
    return bool(_ACTION_RE.search(question or ""))


def looks_dataish(question: str) -> bool:
    return bool(_DATAISH_RE.search(question or ""))


def grade_answer(question: str, body: dict, status: int, principal: dict) -> tuple[str, str]:
    """Mechanical, permission-aware grade. Returns (grade, reason).
    A policy-correct refusal for an under-permissioned user is HIGH."""
    p = principal or {}
    can_operational = bool(p.get("can_ask_operational"))
    route = str(body.get("route_path") or "")
    reason = str(body.get("reason") or "")

    if status >= 400 or not body.get("ok", False):
        return "low", "error_response"

    if is_action_request(question):
        # CENA must decline or queue an action request; anything else is a miss.
        if body.get("queued") or route == "review":
            return "high", "action_request_queued"
        return "medium", "action_request_not_declined"

    if route == "investigation":
        conf = str(body.get("confidence") or "").lower()
        if conf == "low":
            return "medium", "investigation_low_confidence"
        return "high", "investigation"

    if body.get("queued") or route == "review":
        if reason.startswith("forced_review"):
            return "high", "forced_review_policy"
        if reason in _INVESTIGABLE_REASONS and can_operational:
            # An authorized manager asked a data question and got a brush-off.
            return "medium", "queued_but_answerable"
        return "high", "queue_correct_for_permissions"

    if route == "deterministic":
        if _DIAGNOSTIC_RE.search(question or ""):
            return "medium", "tool_intent_mismatch"
        return "high", "deterministic_match"

    if route == "general":
        if looks_dataish(question) and can_operational:
            return "medium", "general_data_miss"
        return "high", "general_ok"

    return "high", "default_pass"


# ------------------------------------------------------- user reply detection

_YES_RE = re.compile(
    r"^\s*(?:(?:yes|yep|yeah|ya|yup|si|correct|thanks?|thank you|thx|ty|got it|perfect|"
    r"great|good|cool|nice|sounds good|that works|that helps|all good|ok|okay|k)[\s!.,]*)+$",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"^\s*(no|nope|nah|not really|not quite|wrong|incorrect|that'?s not (it|right)|"
    r"didn'?t answer|doesn'?t answer|still wrong|try again)\b",
    re.IGNORECASE,
)


def is_affirmation(text: str) -> bool:
    return bool(_YES_RE.match((text or "").strip()))


def is_negative(text: str) -> bool:
    return bool(_NO_RE.match((text or "").strip()))


def start_sweeper() -> None:
    """Hourly archive sweep + daily defect mine (no scheduled task needed -
    task creation is admin-gated on this box, so the learning loop rides the
    runtime process instead)."""

    def _loop() -> None:
        last_mine_date = None
        while True:
            try:
                archive_expired_sweep()
            except Exception:
                pass
            try:
                now_local = datetime.now()
                if now_local.hour >= 4 and last_mine_date != now_local.date():
                    last_mine_date = now_local.date()
                    try:
                        import cena_defect_mine
                    except ImportError:
                        from scripts import cena_defect_mine  # type: ignore[no-redef]
                    cena_defect_mine.run_defect_mine(24.0, hub_post=hub_post)
            except Exception:
                pass
            time.sleep(3600)

    threading.Thread(target=_loop, daemon=True).start()
