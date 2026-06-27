"""Dashboard email workspace routes."""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, request, send_file

from app.services.management_email import (
    MailConfigError,
    MailProviderError,
    attachment_stream,
    default_account_key,
    get_message,
    import_recent_messages,
    list_messages,
    public_accounts,
    send_reply,
)
from app.services.permissions import has_permission


management_email_bp = Blueprint(
    "management_email",
    __name__,
    url_prefix="/dashboard/email",
)


def _require_email_view() -> None:
    if not (
        has_permission("email.view_shared_mailbox")
        or has_permission("email.view_own_mailbox")
    ):
        abort(403)


def _require_email_send() -> None:
    if not has_permission("email.send"):
        abort(403)


def _mail_error(exc: Exception):
    status = 409 if isinstance(exc, MailConfigError) else 502
    return jsonify({
        "ok": False,
        "error": "mailbox_not_connected" if status == 409 else "mail_provider_error",
        "detail": str(exc),
    }), status


@management_email_bp.route("/accounts", methods=["GET"])
def email_accounts():
    _require_email_view()
    return jsonify({
        "ok": True,
        "default_account": default_account_key(),
        "accounts": public_accounts(),
    })


@management_email_bp.route("/messages", methods=["GET"])
def email_messages():
    _require_email_view()
    account = (request.args.get("account") or "").strip() or None
    query = (request.args.get("q") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit") or 25), 50))
    except ValueError:
        limit = 25
    try:
        return jsonify({
            "ok": True,
            "messages": list_messages(account, query=query, limit=limit),
        })
    except (MailConfigError, MailProviderError) as exc:
        return _mail_error(exc)


@management_email_bp.route("/import", methods=["POST"])
def email_import():
    _require_email_view()
    data = request.get_json(silent=True) or {}
    account = (data.get("account") or request.args.get("account") or "").strip() or None
    try:
        days = int(data.get("days") or request.args.get("days") or 60)
    except (TypeError, ValueError):
        days = 60
    try:
        return jsonify({
            "ok": True,
            "import": import_recent_messages(account, days=days),
        })
    except (MailConfigError, MailProviderError) as exc:
        return _mail_error(exc)


@management_email_bp.route("/messages/<path:message_id>", methods=["GET"])
def email_message(message_id: str):
    _require_email_view()
    account = (request.args.get("account") or "").strip() or None
    try:
        return jsonify({
            "ok": True,
            "message": get_message(account, message_id),
        })
    except (MailConfigError, MailProviderError) as exc:
        return _mail_error(exc)


@management_email_bp.route("/attachment", methods=["GET"])
def email_attachment():
    _require_email_view()
    account = (request.args.get("account") or "").strip() or None
    message_id = (request.args.get("message_id") or "").strip()
    attachment_id = (request.args.get("attachment_id") or "").strip()
    filename = (request.args.get("filename") or "attachment").strip() or "attachment"
    mime_type = (request.args.get("mime_type") or "application/octet-stream").strip()
    if not message_id or not attachment_id:
        return jsonify({"ok": False, "error": "missing_attachment_id"}), 400
    try:
        return send_file(
            attachment_stream(account, message_id, attachment_id),
            mimetype=mime_type,
            as_attachment=True,
            download_name=filename,
        )
    except (MailConfigError, MailProviderError, ValueError) as exc:
        return _mail_error(exc)


@management_email_bp.route("/reply", methods=["POST"])
def email_reply():
    _require_email_view()
    _require_email_send()
    data = request.get_json(silent=True) or {}
    account = (data.get("account") or "").strip() or None
    message_id = (data.get("message_id") or "").strip()
    body = (data.get("body") or "").strip()
    if not message_id:
        return jsonify({"ok": False, "error": "missing_message_id"}), 400
    try:
        send_reply(account, message_id, body)
        return jsonify({"ok": True})
    except (MailConfigError, MailProviderError) as exc:
        return _mail_error(exc)
