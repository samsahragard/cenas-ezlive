"""Employee-to-employee in-app messaging (the MESSAGE portal surface).

GET  /employee/messages                          -> ck-style HTML page (self-guards session['employee_id'] -> 302 /employee/login)
GET  /employee/messages/directory                -> {ok, employees:[{id,name}]}  active peers for the recipient picker
GET  /employee/messages/conversations            -> {ok, conversations:[...]}    one row per peer, latest msg + unread count
GET  /employee/messages/thread/<other_id>        -> {ok, peer:{...}, messages:[...]}  full thread (both directions)
POST /employee/messages/send {to_employee_id, body}                              -> 201 {ok, message:{...}}
POST /employee/messages/thread/<other_id>/read                                   -> {ok, marked:N}  stamps read_at on inbound unread

This is a dedicated blueprint (registered in app/__init__.py next to the other
employee blueprints) rather than an attach onto employee_auth, so it stays
self-contained. AUTH / ISOLATION mirrors the rest of /employee/*: the PAGE
self-guards session['employee_id'] and 302s to /employee/login (the employee
door, not the staff keypad); every JSON endpoint self-guards via _require_emp()
(401 JSON) and every read/write is scoped to the session employee. The
/employee/messages prefix is added to auth.py EXEMPT_PREFIXES so a session-less
hit reaches these views (page 302, data 401) instead of the staff-keypad
redirect; isolation is enforced by the employee_id scope on every query, not by
the site gate.

SCOPE: peer-to-peer among ACTIVE employees (Employee.active is True) - any active
employee may message any other active employee. NOT store-scoped (see lane_or_risk).
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)
from sqlalchemy import and_, or_

from app.db import SessionLocal
from app.models import Employee, Message

employee_messages_bp = Blueprint("employee_messages", __name__)

MAX_BODY = 2000  # generous cap; matches the textarea maxlength in the template


def _require_emp():
    """(employee_id, None) for a logged-in employee, else (None, (json, 401)).
    Same idiom as app/web/employee_time_off.py._require_emp."""
    eid = session.get("employee_id")
    if not eid:
        return None, (jsonify({"ok": False, "error": "login required"}), 401)
    return eid, None


def _name(emp):
    return (emp.full_name or "").strip() if emp is not None else ""


def _serialize(m, me_id):
    """One message row, with a 'mine' flag relative to the session employee."""
    return {
        "id": m.id,
        "from_employee_id": m.from_employee_id,
        "to_employee_id": m.to_employee_id,
        "body": m.body,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "read_at": m.read_at.isoformat() if m.read_at else None,
        "mine": (m.from_employee_id == me_id),
    }


@employee_messages_bp.route("/employee/messages", methods=["GET"])
def employee_messages_page():
    """Render the messaging page for the session employee. No employee session
    -> 302 to /employee/login (the employee door, not the staff keypad). Only
    the employee's own name/id is read here; threads load via fetch()."""
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect("/employee/login")

    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == emp_id).first()
        if emp is None:
            for k in ("employee_id", "employee_session_version", "auth_ok"):
                session.pop(k, None)
            return redirect("/employee/login")
        full_name = _name(emp)
        first_name = full_name.split(" ")[0] if full_name else None
        view = SimpleNamespace(
            id=emp.id, first_name=first_name, full_name=full_name or None
        )
    finally:
        db.close()

    config = {
        "meId": view.id,
        "directoryUrl": "/employee/messages/directory",
        "conversationsUrl": "/employee/messages/conversations",
        "threadBase": "/employee/messages/thread",   # GET <base>/<id>; POST <base>/<id>/read
        "sendUrl": "/employee/messages/send",
        "dashboardUrl": "/employee/dashboard",
        "loginUrl": "/employee/login",
        "pollMs": 10000,
    }
    return render_template(
        "employee_messages.html",
        employee=view,
        config_json=json.dumps(config),
        dashboard_url="/employee/dashboard",
        login_url="/employee/login",
    )


@employee_messages_bp.route("/employee/messages/directory", methods=["GET"])
def employee_messages_directory():
    """Active employees other than me, for the recipient picker."""
    me_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        rows = (
            db.query(Employee.id, Employee.full_name)
              .filter(Employee.active.is_(True), Employee.id != me_id)
              .order_by(Employee.full_name.asc())
              .all()
        )
        people = [
            {"id": rid, "name": (nm or "").strip()}
            for rid, nm in rows if (nm or "").strip()
        ]
        return jsonify({"ok": True, "employees": people}), 200
    finally:
        db.close()


@employee_messages_bp.route("/employee/messages/conversations", methods=["GET"])
def employee_messages_conversations():
    """One entry per peer the session employee has exchanged messages with:
    peer identity, the latest message snippet/time, and the count of inbound
    unread messages from that peer. Newest conversation first."""
    me_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        rows = (
            db.query(Message)
              .filter(or_(Message.from_employee_id == me_id,
                          Message.to_employee_id == me_id))
              .order_by(Message.created_at.asc(), Message.id.asc())
              .all()
        )
        # Fold to one bucket per peer (preserving last-message wins for snippet).
        by_peer = {}
        for m in rows:
            peer = m.to_employee_id if m.from_employee_id == me_id else m.from_employee_id
            b = by_peer.get(peer)
            if b is None:
                b = by_peer[peer] = {"peer_id": peer, "unread": 0,
                                     "last_body": "", "last_at": None,
                                     "last_mine": False}
            b["last_body"] = m.body
            b["last_at"] = m.created_at.isoformat() if m.created_at else None
            b["last_mine"] = (m.from_employee_id == me_id)
            if m.to_employee_id == me_id and m.read_at is None:
                b["unread"] += 1
        if not by_peer:
            return jsonify({"ok": True, "conversations": []}), 200
        # Attach peer names (active or not - a past peer may have been deactivated).
        names = dict(
            db.query(Employee.id, Employee.full_name)
              .filter(Employee.id.in_(list(by_peer.keys())))
              .all()
        )
        convos = []
        for peer_id, b in by_peer.items():
            b["peer_name"] = (names.get(peer_id) or "").strip() or "Unknown"
            convos.append(b)
        convos.sort(key=lambda c: (c["last_at"] or ""), reverse=True)
        return jsonify({"ok": True, "conversations": convos}), 200
    finally:
        db.close()


@employee_messages_bp.route("/employee/messages/thread/<int:other_id>", methods=["GET"])
def employee_messages_thread(other_id):
    """The full thread between the session employee and <other_id>, oldest first.
    Does not auto-mark read (the client posts /read explicitly)."""
    me_id, err = _require_emp()
    if err:
        return err
    if other_id == me_id:
        return jsonify({"ok": False, "error": "cannot message yourself"}), 400
    db = SessionLocal()
    try:
        other = db.query(Employee).filter(Employee.id == other_id).first()
        if other is None:
            return jsonify({"ok": False, "error": "employee not found"}), 404
        msgs = (
            db.query(Message)
              .filter(or_(
                  and_(Message.from_employee_id == me_id,
                       Message.to_employee_id == other_id),
                  and_(Message.from_employee_id == other_id,
                       Message.to_employee_id == me_id),
              ))
              .order_by(Message.created_at.asc(), Message.id.asc())
              .all()
        )
        peer = {"id": other.id, "name": _name(other) or "Unknown",
                "active": bool(other.active)}
        return jsonify({
            "ok": True,
            "peer": peer,
            "messages": [_serialize(m, me_id) for m in msgs],
        }), 200
    finally:
        db.close()


@employee_messages_bp.route("/employee/messages/send", methods=["POST"])
def employee_messages_send():
    """Send a message from the session employee to {to_employee_id}. Recipient
    must be a DIFFERENT, ACTIVE employee. Body required, trimmed, capped."""
    me_id, err = _require_emp()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        to_id = int(data.get("to_employee_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "to_employee_id required"}), 400
    if to_id == me_id:
        return jsonify({"ok": False, "error": "cannot message yourself"}), 400
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"ok": False, "error": "message body required"}), 400
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY]

    db = SessionLocal()
    try:
        recipient = (
            db.query(Employee)
              .filter(Employee.id == to_id, Employee.active.is_(True))
              .first()
        )
        if recipient is None:
            return jsonify({"ok": False, "error": "recipient not available"}), 404
        m = Message(
            from_employee_id=me_id,
            to_employee_id=to_id,
            body=body,
            created_at=datetime.utcnow(),
        )
        db.add(m)
        db.commit()
        return jsonify({"ok": True, "message": _serialize(m, me_id)}), 201
    finally:
        db.close()


@employee_messages_bp.route("/employee/messages/thread/<int:other_id>/read", methods=["POST"])
def employee_messages_mark_read(other_id):
    """Stamp read_at on every still-unread INBOUND message from <other_id> to the
    session employee. Idempotent; returns how many rows were marked."""
    me_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        marked = (
            db.query(Message)
              .filter(Message.to_employee_id == me_id,
                      Message.from_employee_id == other_id,
                      Message.read_at.is_(None))
              .update({Message.read_at: now}, synchronize_session=False)
        )
        db.commit()
        return jsonify({"ok": True, "marked": int(marked or 0)}), 200
    finally:
        db.close()
