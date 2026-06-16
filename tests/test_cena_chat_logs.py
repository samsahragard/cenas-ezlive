from __future__ import annotations

import os
import shutil
from pathlib import Path
import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app.models import User, Employee, CenaChatLog
import app.web.assistant_routes as assistant_mod
import app.web.cena as cena_mod

# -------------------------------------------------------------
# Mock objects for Google GenAI Client
# -------------------------------------------------------------
class MockCandidate:
    def __init__(self):
        self.content = "mocked candidate content"

class MockResponse:
    def __init__(self, text="Mocked response text", function_calls=None):
        self.text = text
        self.function_calls = function_calls or []
        self.candidates = [MockCandidate()]

class MockModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        # Inspect contents to simulate playbook search if needed
        last_msg = ""
        if contents:
            for content in reversed(contents):
                if hasattr(content, "parts"):
                    for part in content.parts:
                        if hasattr(part, "text") and part.text:
                            last_msg = part.text
                            break
                if last_msg:
                    break
        
        # Simulating first turn where model requests a tool
        if "playbook" in last_msg.lower() and len(self.calls) == 1:
            class MockCall:
                name = "search_manager_playbooks_tool"
                args = {"query": "influence"}
            return MockResponse(text="", function_calls=[MockCall()])
            
        return MockResponse(text="Mocked CENA answer", function_calls=[])

class MockGenAIClient:
    def __init__(self, api_key=None):
        self.models = MockModels()

# -------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------
@pytest.fixture
def chat_app(db_session, monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")
    monkeypatch.setenv("SAM_CHAT_USER_ID", "1")
    monkeypatch.setenv("DISABLE_DOCCK_TICKER", "1")
    
    # Mock SessionLocal for blueprints
    import app.db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(cena_mod, "SessionLocal", lambda: db_session)
    
    # Mock GenAI client class
    monkeypatch.setattr(assistant_mod.genai, "Client", MockGenAIClient)
    
    # Setup mock workspace and manager playbooks directory
    orig_workspace = assistant_mod.workspace_path
    monkeypatch.setattr(assistant_mod, "workspace_path", tmp_path)
    
    playbooks_dir = tmp_path / "docs" / "manager_playbooks"
    playbooks_dir.mkdir(parents=True, exist_ok=True)
    
    test_playbook = playbooks_dir / "leadership_laws.md"
    test_playbook.write_text(
        "# Leadership Playbook\n\n"
        "Law of the Lid: Leadership ability determines a person's level of effectiveness.\n\n"
        "Law of Influence: The true measure of leadership is influence.",
        encoding="utf-8"
    )
    
    # Seed users
    # Sam (Partner, User ID = 1)
    db_session.add(User(
        id=1,
        full_name="Sam Partner",
        email="sam@test.local",
        passcode_hash=generate_password_hash("12345"),
        permission_level="partner",
        store_scope=None,
        active=True,
        first_login_done=True,
        session_version=1
    ))
    # Another Partner (User ID = 2)
    db_session.add(User(
        id=2,
        full_name="Other Partner",
        email="other@test.local",
        passcode_hash=generate_password_hash("12345"),
        permission_level="partner",
        store_scope=None,
        active=True,
        first_login_done=True,
        session_version=1
    ))
    # Manager (User ID = 3)
    db_session.add(User(
        id=3,
        full_name="Jane Manager",
        email="jane@test.local",
        passcode_hash=generate_password_hash("12345"),
        permission_level="manager",
        store_scope="tomball",
        active=True,
        first_login_done=True,
        session_version=1
    ))
    # Hourly employee (User ID = 4)
    db_session.add(User(
        id=4,
        full_name="John Hourly",
        email="john@test.local",
        passcode_hash=generate_password_hash("12345"),
        permission_level="staff",
        store_scope="tomball",
        active=True,
        first_login_done=True,
        session_version=1
    ))
    # Seed corresponding Employee profile for Hourly
    db_session.add(Employee(
        id=104,
        user_id=4,
        full_name="John Hourly",
        active=True,
        session_version=1
    ))
    
    db_session.commit()
    
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    
    return flask_app, db_session

def _login_client(flask_app, user_id, is_partner=False):
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_session_version"] = 1
        sess["auth_ok"] = True
        if is_partner:
            sess["partner_auth_ok"] = True
    return client

# -------------------------------------------------------------
# Test Cases
# -------------------------------------------------------------
def test_assistant_chat_logs_turn_and_playbook_tool_for_partner(chat_app):
    flask_app, db = chat_app
    client = _login_client(flask_app, 1, is_partner=True)
    
    # 1. Normal Chat turn
    res = client.post("/api/assistant/chat", json={
        "message": "Hello CENA",
        "history": []
    })
    assert res.status_code == 200
    body = res.get_json()
    assert body["success"] is True
    assert body["text"] == "Mocked CENA answer"
    
    # Verify log is written to DB
    logs = db.query(CenaChatLog).all()
    assert len(logs) == 1
    assert logs[0].user_id == 1
    assert logs[0].question == "Hello CENA"
    assert logs[0].response == "Mocked CENA answer"
    assert logs[0].user_tier == "partner"
    assert logs[0].feedback_status == "unreviewed"

    # 2. Chat triggering RAG Playbook Search
    res_rag = client.post("/api/assistant/chat", json={
        "message": "How does leadership playbook law of influence work?",
        "history": []
    })
    assert res_rag.status_code == 200
    body_rag = res_rag.get_json()
    assert body_rag["success"] is True
    
    # Verify tools executed logs returned
    tools = body_rag.get("toolsExecuted", [])
    assert any(t["name"] == "search_manager_playbooks" for t in tools)
    
    # Verify second log was written
    logs = db.query(CenaChatLog).order_by(CenaChatLog.id.desc()).all()
    assert len(logs) == 2
    assert logs[0].question == "How does leadership playbook law of influence work?"


def test_assistant_chat_playbook_tool_access_restrictions(chat_app):
    flask_app, db = chat_app
    
    # 1. Partner can use playbook tool
    client_partner = _login_client(flask_app, 1, is_partner=True)
    res_partner = client_partner.post("/api/assistant/chat", json={
        "message": "playbook rules on lid",
        "history": []
    })
    assert res_partner.status_code == 200
    assert any(t["name"] == "search_manager_playbooks" for t in res_partner.get_json().get("toolsExecuted", []))

    # 2. Manager can use playbook tool
    client_manager = _login_client(flask_app, 3, is_partner=False)
    res_manager = client_manager.post("/api/assistant/chat", json={
        "message": "playbook rules on lid",
        "history": []
    })
    assert res_manager.status_code == 200
    assert any(t["name"] == "search_manager_playbooks" for t in res_manager.get_json().get("toolsExecuted", []))

    # 3. Hourly employee is firewalled from playbook tool
    client_hourly = _login_client(flask_app, 4, is_partner=False)
    res_hourly = client_hourly.post("/api/assistant/chat", json={
        "message": "playbook rules on lid",
        "history": []
    })
    assert res_hourly.status_code == 200
    # Check that search_manager_playbooks failed execution
    tools_hourly = res_hourly.get_json().get("toolsExecuted", [])
    assert any(t["name"] == "search_manager_playbooks" and t["success"] is False for t in tools_hourly)


def test_direct_playbook_search_tool_function(chat_app):
    # Test direct python function
    res = assistant_mod.search_manager_playbooks_tool("influence")
    assert len(res) > 0
    assert "leadership_laws.md" in res[0]["file"]
    assert "influence" in res[0]["content"].lower()

    # Query with no matches
    res_none = assistant_mod.search_manager_playbooks_tool("invalidkeywordthatmatchesnothing")
    assert len(res_none) == 0


def test_rating_endpoint_access_constraints(chat_app):
    flask_app, db = chat_app
    
    # Insert a dummy log row
    log_entry = CenaChatLog(
        user_id=4,
        employee_id=104,
        user_name="John Hourly",
        user_tier="hourly",
        question="How do I view schedule?",
        response="Use profile menu.",
        feedback_status="unreviewed"
    )
    db.add(log_entry)
    db.commit()
    log_id = log_entry.id

    # 1. Non-logged in gets redirected/denied
    client_anon = flask_app.test_client()
    res_anon = client_anon.post(f"/api/assistant/chat-logs/{log_id}/rate", json={"status": "good", "notes": "Approved"})
    assert res_anon.status_code in (302, 401, 403)

    # 2. Hourly user gets redirected/denied
    client_hourly = _login_client(flask_app, 4, is_partner=False)
    res_hourly = client_hourly.post(f"/api/assistant/chat-logs/{log_id}/rate", json={"status": "good", "notes": "Approved"})
    print("HOURLY STATUS CODE:", res_hourly.status_code)
    print("HOURLY RESPONSE HEADERS:", dict(res_hourly.headers))
    print("HOURLY RESPONSE DATA:", res_hourly.get_data(as_text=True))
    assert res_hourly.status_code in (302, 403)

    # 3. Manager gets redirected/denied
    client_manager = _login_client(flask_app, 3, is_partner=False)
    res_manager = client_manager.post(f"/api/assistant/chat-logs/{log_id}/rate", json={"status": "good", "notes": "Approved"})
    assert res_manager.status_code in (302, 403)

    # 4. Another Partner (User ID = 2) gets denied (gated strictly to Sam ID = 1)
    client_partner2 = _login_client(flask_app, 2, is_partner=True)
    res_partner2 = client_partner2.post(f"/api/assistant/chat-logs/{log_id}/rate", json={"status": "good", "notes": "Approved"})
    assert res_partner2.status_code == 403
    assert "restricted to Sam" in res_partner2.get_json()["error"]

    # 5. Sam (User ID = 1) succeeds and updates database
    client_sam = _login_client(flask_app, 1, is_partner=True)
    res_sam = client_sam.post(f"/api/assistant/chat-logs/{log_id}/rate", json={"status": "good", "notes": "Outstanding response"})
    assert res_sam.status_code == 200
    assert res_sam.get_json()["success"] is True

    # Check database status was updated
    updated_log = db.query(CenaChatLog).filter_by(id=log_id).first()
    assert updated_log.feedback_status == "good"
    assert updated_log.review_notes == "Outstanding response"


def test_audit_page_access_constraints(chat_app):
    flask_app, db = chat_app
    
    # 1. Anonymous gets redirected/denied
    client_anon = flask_app.test_client()
    res_anon = client_anon.get("/sam/cena-chat-audit")
    assert res_anon.status_code in (302, 403)

    # 2. Hourly employee gets redirected/denied
    client_hourly = _login_client(flask_app, 4, is_partner=False)
    res_hourly = client_hourly.get("/sam/cena-chat-audit")
    print("AUDIT PAGE HOURLY STATUS:", res_hourly.status_code)
    print("AUDIT PAGE HOURLY DATA:", res_hourly.get_data(as_text=True))
    assert res_hourly.status_code in (302, 403)

    # 3. Partner other than Sam gets redirected/denied (gated strictly to Sam ID == 1)
    client_partner2 = _login_client(flask_app, 2, is_partner=True)
    res_partner2 = client_partner2.get("/sam/cena-chat-audit")
    assert res_partner2.status_code in (302, 403)

    # 4. Sam (User ID = 1) succeeds
    client_sam = _login_client(flask_app, 1, is_partner=True)
    res_sam = client_sam.get("/sam/cena-chat-audit")
    assert res_sam.status_code == 200
    assert "CENA Chat Audit" in res_sam.get_data(as_text=True)
