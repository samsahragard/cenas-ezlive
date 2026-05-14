"""Phase 1 / Block 6 — morning-brief email dispatch.

Separate module from brief_composer.py so the composer stays focused on
gather → LLM → validate → persist, and dispatch is its own surface for
samai to review. Wired into compose_all_briefs() at the bottom of
brief_composer.py — dispatch failures are caught and logged so they
never block composition.

Recipient model (spec §11):
  - partner  → sam@cenaskitchen.com (hardcoded; partner is the single
               recipient for the partner brief)
  - everyone else → the user's User.email if set
  - missing email → logged + skipped (not an error)

Subject (spec §11):
  "Cenas brief — {brief_date} — {alert_count} alerts / {warn_count} warns"

Body: multipart/alternative with plain + HTML. HTML reuses
templates/email/morning_brief.html, which the future /partner/briefs
in-app surface (Phase 1.5) can extend.

Dark-launch flag:
  - BRIEF_EMAIL_DISPATCH=1 to actually send
  - Anything else (default unset) = dry-run: log what would be sent, no
    SMTP call. This mirrors ck's PERMISSION_ENFORCE pattern from Phase 0
    Block 4 so we can ship the code, watch logs for a day, then flip.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)


# ---- SMTP config (reuses the orders@cenaskitchen.com mailbox per spec) ----
SMTP_HOST = os.getenv("ORDERS_SMTP_HOST", "gvam1078.siteground.biz")
SMTP_PORT = int(os.getenv("ORDERS_SMTP_PORT", "465"))
SMTP_USER = os.getenv("ORDERS_SMTP_USER", "orders@cenaskitchen.com")
FROM_NAME = "Cenas Kitchen Briefs"

PARTNER_EMAIL = os.getenv("PARTNER_BRIEF_EMAIL", "sam@cenaskitchen.com")

_AICK_SECRETS = Path(r"C:\Users\sam\.openclaw\.secrets")


def _email_pwd() -> str:
    """SMTP password for orders@cenaskitchen.com. Same path produce_order
    uses — single source of truth on Render via ORDERS_EMAIL_PWD."""
    val = os.getenv("ORDERS_EMAIL_PWD")
    if val:
        return val.strip()
    f = _AICK_SECRETS / "orders_smtp_pwd.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    raise RuntimeError("missing ORDERS_EMAIL_PWD env var and fallback file")


def _dispatch_enabled() -> bool:
    """Dark-launch flag. Off by default — flip BRIEF_EMAIL_DISPATCH=1 to
    actually send. Tests and the dry-run cron still exercise the full
    rendering path; only the SMTP call is gated."""
    return os.getenv("BRIEF_EMAIL_DISPATCH", "").strip().lower() in ("1", "true", "yes", "on")


# ---- counting + formatting ----

@dataclass
class _SeverityCounts:
    alert: int
    warn: int
    info: int


def _count_severities(brief_body: dict) -> _SeverityCounts:
    """Walk the brief sections and tally items by section_kind.
    Per spec §3 the sections are pre-grouped (alerts / warns / wins /
    lookahead / info_aggregate / calibration) so the count is just len()
    of each section's items list. info_aggregate is a single line, not a
    list of items — its count is rendered into the line text by the LLM
    and we don't double-count here.
    """
    counts = _SeverityCounts(alert=0, warn=0, info=0)
    sections = brief_body.get("sections", []) or []
    for sec in sections:
        kind = sec.get("section_kind")
        items = sec.get("items", []) or []
        if kind == "alerts":
            counts.alert = len(items)
        elif kind == "warns":
            counts.warn = len(items)
    return counts


def format_subject(brief_date: date | str, counts: _SeverityCounts) -> str:
    """Spec §11 subject line."""
    if isinstance(brief_date, date):
        date_str = brief_date.isoformat()
    else:
        date_str = str(brief_date)
    return (
        f"Cenas brief — {date_str} — "
        f"{counts.alert} alerts / {counts.warn} warns"
    )


# ---- recipient resolution ----

def resolve_recipient(audience, db=None) -> tuple[str | None, str]:
    """Returns (email, reason). reason is "ok" if email present, else
    a short tag for logging ("no_email" / "skipped_role" / ...).

    Partner role → PARTNER_EMAIL (sam@cenaskitchen.com).
    Other enrolled roles → User.email lookup via audience.user_id.
    """
    role = (audience.role or "").lower()
    if role == "partner":
        return PARTNER_EMAIL, "ok"

    # Look up the user's email. Audience carries user_id but not the full
    # User row; we re-query so callers don't have to thread the row.
    if db is None:
        from app.db import SessionLocal
        db = SessionLocal()
        close_db = True
    else:
        close_db = False
    try:
        from app.models import User
        u = db.get(User, audience.user_id)
        if u is None:
            return None, "user_missing"
        email = (u.email or "").strip()
        if not email:
            return None, "no_email"
        return email, "ok"
    finally:
        if close_db:
            db.close()


# ---- rendering ----

def render_plain(brief_body: dict, audience) -> str:
    """Plain-text rendering — fallback for clients that don't render
    HTML, and the part that screen readers will read. Mirrors the brief
    structure top-to-bottom."""
    lines: list[str] = []
    g = brief_body.get("greeting") or f"Good morning, {audience.user_name.split()[0]}."
    lines.append(g)
    lines.append("")
    hl = brief_body.get("headline")
    if hl:
        lines.append(hl)
        lines.append("")
    for sec in brief_body.get("sections", []) or []:
        kind = sec.get("section_kind", "")
        heading = sec.get("heading", kind.title())
        lines.append(heading)
        lines.append("-" * len(heading))
        intro = sec.get("intro")
        if intro:
            lines.append(intro)
        items = sec.get("items", []) or []
        if kind == "info_aggregate":
            # info_aggregate's items is usually empty; the intro holds the line
            if not intro and items:
                lines.append(items[0].get("one_line", ""))
        else:
            for it in items:
                bullet = f"  - {it.get('one_line', '').strip()}"
                lines.append(bullet)
                action = (it.get("action") or "").strip()
                if action:
                    lines.append(f"      Action: {action}")
        lines.append("")
    closing = brief_body.get("closing")
    if closing:
        lines.append(closing)
    return "\n".join(lines).rstrip() + "\n"


def render_html(brief_body: dict, audience) -> str:
    """Render the brief through templates/email/morning_brief.html. The
    in-app /partner/briefs surface (Phase 1.5) re-uses this same partial
    per spec §11."""
    # Lazy import — Flask must be available; this runs inside the cron
    # request context (and the test fixture builds an app context).
    from flask import current_app, render_template
    if current_app:
        return render_template(
            "email/morning_brief.html",
            brief=brief_body,
            audience=audience,
        )
    # Fallback: minimal inline render so the dispatch path doesn't
    # crash when called outside a request context (e.g. shell scripts).
    # Mirrors the plain text but wrapped in <pre>.
    plain = render_plain(brief_body, audience)
    import html as _html
    return f"<html><body><pre style='font-family:Arial,sans-serif'>{_html.escape(plain)}</pre></body></html>"


# ---- SMTP send ----

def _smtp_send(to_addr: str, subject: str, plain: str) -> None:
    """SMTP_SSL via the same SiteGround mailbox produce_order uses.

    Plain-text only per Sam 2026-05-13 19:39 + 20:13 — phone notification
    preview was using the plain part anyway; the multipart wrapper + html
    branch was over-implementation relative to his 18:26 directive
    ("plain text with section headers, easier to read on phone notification
    preview"). render_html() is retained in this module — Phase 1.5 in-app
    /partner/briefs UI uses the same Jinja partial per spec §11 — but no
    longer fires from the email path.
    """
    pwd = _email_pwd()
    msg = MIMEText(plain, "plain")
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"] = to_addr

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as srv:
        srv.login(SMTP_USER, pwd)
        srv.sendmail(SMTP_USER, [to_addr], msg.as_string())


# ---- main entrypoint ----

def dispatch_brief(brief_row, audience, db=None) -> dict:
    """Send (or dry-run) the morning brief email for one audience.

    Returns a dict suitable for embedding into the cron summary:
      {
        "user_id": int,
        "role": str,
        "to": str | None,
        "status": "sent" | "dry_run" | "skipped" | "error",
        "reason": short tag,
      }
    Never raises — recipient lookup errors, render errors, and SMTP
    errors are all caught, logged, and surfaced via the return dict.
    """
    out: dict = {
        "user_id": audience.user_id,
        "role": audience.role,
        "to": None,
        "status": "error",
        "reason": "",
    }
    try:
        to_addr, reason = resolve_recipient(audience, db)
        out["to"] = to_addr
        if to_addr is None:
            out["status"] = "skipped"
            out["reason"] = reason
            logger.info(
                "brief dispatch skipped user_id=%s role=%s reason=%s",
                audience.user_id, audience.role, reason)
            return out

        body = dict(brief_row.body or {})
        counts = _count_severities(body)
        subject = format_subject(audience.brief_date, counts)
        plain = render_plain(body, audience)

        if not _dispatch_enabled():
            out["status"] = "dry_run"
            out["reason"] = "flag_off"
            logger.info(
                "brief dispatch dry-run user_id=%s to=%s subject=%r "
                "alerts=%d warns=%d (set BRIEF_EMAIL_DISPATCH=1 to send)",
                audience.user_id, to_addr, subject,
                counts.alert, counts.warn,
            )
            return out

        _smtp_send(to_addr, subject, plain)
        out["status"] = "sent"
        out["reason"] = "ok"
        logger.info(
            "brief dispatch sent user_id=%s to=%s subject=%r",
            audience.user_id, to_addr, subject)
        return out
    except Exception as e:  # noqa: BLE001 — dispatch must never raise
        logger.exception(
            "brief dispatch failed user_id=%s role=%s: %s",
            audience.user_id, audience.role, e)
        out["status"] = "error"
        out["reason"] = type(e).__name__
        return out
