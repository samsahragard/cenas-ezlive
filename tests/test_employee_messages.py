"""Employee-to-employee messaging: core send/read, isolation (an employee can
only read their OWN DMs), validation, and auth. Regression coverage for
app/web/employee_messages.py."""
import os
import tempfile

import pytest


@pytest.fixture()
def app_emps():
    tmp = os.path.join(tempfile.gettempdir(), "_msg_pytest.db")
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    os.environ["ALLOW_DEV_SECRET"] = "1"
    os.environ["DATABASE_URL"] = "sqlite:///" + tmp.replace("\\", "/")
    from app import create_app
    app = create_app()
    from app.db import SessionLocal
    from app.models import Employee
    db = SessionLocal()
    ids = {}
    for n in ("Alice A", "Bob B", "Carol C"):
        e = Employee(full_name=n, active=True, session_version=1, passcode_hash="x")
        db.add(e)
        db.commit()
        ids[n.split()[0]] = e.id
    db.close()
    yield app, ids
    try:
        os.remove(tmp)
    except OSError:
        pass


def _as(c, eid):
    with c.session_transaction() as s:
        s.clear()
        s["employee_id"] = eid
        s["auth_ok"] = True


def test_send_and_read(app_emps):
    app, ids = app_emps
    c = app.test_client()
    _as(c, ids["Alice"])
    r = c.post("/employee/messages/send",
               json={"to_employee_id": ids["Bob"], "body": "hi Bob"})
    assert r.status_code == 201
    t = c.get("/employee/messages/thread/%d" % ids["Bob"]).get_json()
    assert [m["body"] for m in t["messages"]] == ["hi Bob"]
    assert t["messages"][0]["mine"] is True
    convos = c.get("/employee/messages/conversations").get_json()["conversations"]
    assert [x["peer_name"] for x in convos] == ["Bob B"]


def test_isolation_cannot_read_others_dms(app_emps):
    app, ids = app_emps
    c = app.test_client()
    _as(c, ids["Bob"])
    c.post("/employee/messages/send",
           json={"to_employee_id": ids["Carol"], "body": "SECRET B to C"})
    _as(c, ids["Alice"])
    # Alice has no messages with Carol -> empty thread, and the B<->C convo
    # must NOT appear in Alice's conversation list.
    assert c.get("/employee/messages/thread/%d" % ids["Carol"]).get_json()["messages"] == []
    assert c.get("/employee/messages/conversations").get_json()["conversations"] == []


def test_validation(app_emps):
    app, ids = app_emps
    c = app.test_client()
    _as(c, ids["Alice"])
    assert c.post("/employee/messages/send",
                  json={"to_employee_id": ids["Alice"], "body": "x"}).status_code == 400
    assert c.post("/employee/messages/send",
                  json={"to_employee_id": ids["Bob"], "body": "   "}).status_code == 400


def test_auth_required(app_emps):
    app, _ = app_emps
    c = app.test_client()
    with c.session_transaction() as s:
        s.clear()
    assert c.get("/employee/messages/conversations").status_code == 401
    assert c.get("/employee/messages").status_code == 302
