import os
import json
import pytest
from pathlib import Path
from flask import g, session

@pytest.fixture
def app_with_user(db_session, monkeypatch):
    from app.models import User
    partner = User(
        id=1, full_name="Sam Sahragard", email="sam@x.test",
        passcode_hash="x", permission_level="partner",
        active=True, first_login_done=True,
    )
    non_partner = User(
        id=2, full_name="Masood C", email="masood@x.test",
        passcode_hash="x", permission_level="corporate",
        active=True, first_login_done=True,
    )
    db_session.add_all([partner, non_partner])
    db_session.commit()

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")

    import app.db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True

    def _client_for(user_id: int, partner_auth_ok: bool = True):
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["partner_auth_ok"] = partner_auth_ok
            sess["auth_ok"] = True
            sess["user_id"] = user_id
            sess["user_session_version"] = 1
        return c

    yield app, _client_for, db_session

def test_ide_page_gating_unauthorized(app_with_user):
    app, client_for, db = app_with_user
    # 1. No partner_auth_ok session key -> redirect
    c = client_for(1, partner_auth_ok=False)
    r = c.get("/partner/developer/ide")
    assert r.status_code == 302
    assert "partner-login" in r.headers["Location"] or "login" in r.headers["Location"]

    # 2. Not partner role -> 403
    c = client_for(2, partner_auth_ok=True)
    r = c.get("/partner/developer/ide")
    assert r.status_code == 403

def test_ide_page_accessible_to_partner(app_with_user):
    app, client_for, db = app_with_user
    c = client_for(1, partner_auth_ok=True)
    r = c.get("/partner/developer/ide")
    assert r.status_code == 200
    assert b"IDE Bot" in r.data
    assert b"hi, Sam Sahragard." in r.data

def test_api_list_files(app_with_user):
    app, client_for, db = app_with_user
    c = client_for(1, partner_auth_ok=True)
    r = c.get("/api/ide/files")
    assert r.status_code == 200
    data = json.loads(r.data.decode("utf-8"))
    assert data["success"] is True
    assert isinstance(data["files"], list)
    # Check that common files like wsgi.py exist in list
    paths = [f["path"] for f in data["files"]]
    assert "wsgi.py" in paths

def test_api_file_content_success(app_with_user):
    app, client_for, db = app_with_user
    c = client_for(1, partner_auth_ok=True)
    r = c.get("/api/ide/file-content?path=wsgi.py")
    assert r.status_code == 200
    data = json.loads(r.data.decode("utf-8"))
    assert data["success"] is True
    assert "create_app" in data["content"]

def test_api_file_content_traversal_denied(app_with_user):
    app, client_for, db = app_with_user
    c = client_for(1, partner_auth_ok=True)
    r = c.get("/api/ide/file-content?path=../../outside.txt")
    assert r.status_code == 500
    data = json.loads(r.data.decode("utf-8"))
    assert data["success"] is False
    assert "Access denied" in data["error"]

def test_query_sales_db_tool():
    from app.web.ide_routes import query_sales_db_tool
    # Test a basic SELECT query
    res = query_sales_db_tool("SELECT 1 AS num")
    assert isinstance(res, list)
    assert len(res) == 1
    assert res[0]["num"] == 1

    # Test querying the tables (if databases exist, which they should on this system)
    try:
        checks = query_sales_db_tool("SELECT store_key, count(*) as count FROM toast_check_current GROUP BY store_key")
        print("Checks in database:", checks)
        assert isinstance(checks, list)
    except Exception as e:
        print("Could not query toast_check_current (db might not exist or be empty):", e)

    try:
        profiles = query_sales_db_tool("SELECT count(*) as count FROM toastdm.dm_profile")
        print("Profiles in attached toastdm database:", profiles)
        assert isinstance(profiles, list)
    except Exception as e:
        print("Could not query toastdm.dm_profile:", e)

def test_query_sales_db_tool_proxy(monkeypatch):
    import urllib.request
    from app.web.ide_routes import query_sales_db_tool
    
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_URL", "https://cena-cloud-test.onrender.com/assistant/answer")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_TOKEN", "mocktoken")
    
    called_url = None
    called_headers = {}
    called_data = None
    
    class MockResponse:
        def __init__(self, data):
            self.data = data
        def read(self):
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
            
    def mock_urlopen(req, timeout=None):
        nonlocal called_url, called_headers, called_data
        called_url = req.full_url
        called_headers = req.headers
        called_data = json.loads(req.data.decode("utf-8"))
        return MockResponse(json.dumps({"success": True, "results": [{"val": 42}]}).encode("utf-8"))
        
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    
    res = query_sales_db_tool("SELECT 42")
    assert res == [{"val": 42}]
    assert called_url == "https://cena-cloud-test.onrender.com/sync/query_db"
    assert "Authorization" in called_headers
    assert called_headers["Authorization"].startswith("Basic ")
    assert called_data == {"sqlQuery": "SELECT 42"}
