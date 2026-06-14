import os
import json
import pytest
from pathlib import Path
from flask import g, session
from app.models import User

@pytest.fixture
def app_with_user(db_session, monkeypatch):
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

def test_assistant_page_gating_unauthorized(app_with_user):
    app, client_for, db = app_with_user
    # 1. No partner_auth_ok session key but is partner -> redirect
    c = client_for(1, partner_auth_ok=False)
    r = c.get("/assistant")
    assert r.status_code == 302
    assert "partner-login" in r.headers["Location"]

    # 2. General logged in (non-partner) -> works without partner_auth_ok
    c = client_for(2, partner_auth_ok=False)
    r = c.get("/assistant")
    assert r.status_code == 200

def test_assistant_page_accessible_to_partner(app_with_user):
    app, client_for, db = app_with_user
    c = client_for(1, partner_auth_ok=True)
    r = c.get("/assistant")
    assert r.status_code == 200
    assert b"Cenas AI" in r.data
    assert b"hi, Sam Sahragard." in r.data

def test_api_list_files(app_with_user):
    app, client_for, db = app_with_user
    c = client_for(1, partner_auth_ok=True)
    r = c.get("/api/assistant/files")
    assert r.status_code == 200
    data = json.loads(r.data.decode("utf-8"))
    assert data["success"] is True
    assert isinstance(data["files"], list)
    paths = [f["path"] for f in data["files"]]
    assert "wsgi.py" in paths

def test_api_file_content_success(app_with_user):
    app, client_for, db = app_with_user
    c = client_for(1, partner_auth_ok=True)
    r = c.get("/api/assistant/file-content?path=wsgi.py")
    assert r.status_code == 200
    data = json.loads(r.data.decode("utf-8"))
    assert data["success"] is True
    assert "create_app" in data["content"]

def test_api_file_content_traversal_denied(app_with_user):
    app, client_for, db = app_with_user
    c = client_for(1, partner_auth_ok=True)
    r = c.get("/api/assistant/file-content?path=../../outside.txt")
    assert r.status_code == 500
    data = json.loads(r.data.decode("utf-8"))
    assert data["success"] is False
    assert "Access denied" in data["error"]

def test_query_sales_db_tool():
    from app.web.assistant_routes import query_sales_db_tool
    # Test a basic SELECT query
    res = query_sales_db_tool("SELECT 1 AS num")
    assert isinstance(res, list)
    assert len(res) == 1
    assert res[0]["num"] == 1

    try:
        checks = query_sales_db_tool("SELECT store_key, count(*) as count FROM toast_check_current GROUP BY store_key")
        assert isinstance(checks, list)
    except Exception as e:
        print("Could not query toast_check_current:", e)

def test_query_sales_db_tool_gating(app_with_user):
    app, client_for, db = app_with_user
    from app.web.assistant_routes import query_sales_db_tool
    
    # 1. Partner user: should be able to query labor cost/pay rate
    with app.test_request_context():
        g.current_user = db.query(User).filter_by(permission_level="partner").first()
        try:
            # Table might not exist in test env, but it shouldn't raise a PermissionError
            query_sales_db_tool("SELECT base_pay FROM toastdm.dm_time_entry LIMIT 1")
        except PermissionError:
            pytest.fail("Partner should be able to query labor pay/costs.")
        except Exception:
            pass
            
    # 2. Manager user (non-partner): should NOT be able to query labor pay/costs
    with app.test_request_context():
        g.current_user = db.query(User).filter_by(permission_level="corporate").first()
        with pytest.raises(PermissionError):
            query_sales_db_tool("SELECT base_pay FROM toastdm.dm_time_entry LIMIT 1")
        with pytest.raises(PermissionError):
            query_sales_db_tool("SELECT tips FROM toastdm.dm_time_entry LIMIT 1")
        with pytest.raises(PermissionError):
            query_sales_db_tool("SELECT * FROM toastdm.dm_time_entry LIMIT 1")
        # Should be able to query non-pay/tips columns on dm_time_entry
        try:
            query_sales_db_tool("SELECT cena_employee_id, clock_in FROM toastdm.dm_time_entry LIMIT 1")
        except PermissionError:
            pytest.fail("Manager should be able to query non-pay columns of dm_time_entry.")
        except Exception:
            pass
            
    # 3. Hourly user (e.g. driver): should NOT be able to query sales tables or others' schedules
    hourly_user = User(
        id=3, full_name="Hourly Worker", email="hourly@x.test",
        passcode_hash="x", permission_level="driver",
        active=True, first_login_done=True,
    )
    db.add(hourly_user)
    db.commit()
    
    with app.test_request_context():
        g.current_user = hourly_user
        # Querying sales table should raise PermissionError
        with pytest.raises(PermissionError):
            query_sales_db_tool("SELECT * FROM toast_check_current")
            
        # Querying schedules without filtering on cena_employee_id should raise PermissionError
        with pytest.raises(PermissionError):
            query_sales_db_tool("SELECT * FROM toastdm.dm_schedule")

def test_query_sales_db_tool_proxy(monkeypatch):
    import urllib.request
    from app.web.assistant_routes import query_sales_db_tool
    
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

def test_fetch_toast_live_data_tool_gating(app_with_user, monkeypatch):
    app, client_for, db = app_with_user
    from app.web.assistant_routes import fetch_toast_live_data_tool
    
    # Mock ToastClient and restaurant_guids
    monkeypatch.setattr("app.services.toast_client.restaurant_guids", lambda: {"tomball": "guid-tomball", "copperfield": "guid-copperfield"})
    
    class FakeToast:
        def fetch_orders_for_date(self, *args, **kwargs):
            return []
        def fetch_time_entries(self, *args, **kwargs):
            return []
        def fetch_employees(self, *args, **kwargs):
            return []
        def fetch_jobs(self, *args, **kwargs):
            return []
        def fetch_tables(self, *args, **kwargs):
            return []
            
    from app.services.toast_client import ToastClient
    monkeypatch.setattr(ToastClient, "shared", lambda: FakeToast())
    
    # 1. Partner user: should access successfully
    with app.test_request_context():
        g.current_user = db.query(User).filter_by(permission_level="partner").first()
        res = fetch_toast_live_data_tool("sales", "tomball")
        assert isinstance(res, dict)
        
    # 2. Manager user (corporate): should access successfully
    with app.test_request_context():
        g.current_user = db.query(User).filter_by(permission_level="corporate").first()
        res = fetch_toast_live_data_tool("sales", "tomball")
        assert isinstance(res, dict)
        
    # 3. Hourly user: should raise PermissionError
    hourly_user = User(
        id=4, full_name="Hourly Worker Live Test", email="hourly2@x.test",
        passcode_hash="x", permission_level="driver",
        active=True, first_login_done=True,
    )
    db.add(hourly_user)
    db.commit()
    with app.test_request_context():
        g.current_user = hourly_user
        with pytest.raises(PermissionError):
            fetch_toast_live_data_tool("sales", "tomball")

def test_fetch_toast_live_data_tool_tables(app_with_user, monkeypatch):
    app, client_for, db = app_with_user
    from app.web.assistant_routes import fetch_toast_live_data_tool
    
    monkeypatch.setattr("app.services.toast_client.restaurant_guids", lambda: {"tomball": "guid-tomball"})
    
    class FakeToast:
        def fetch_tables(self, *args, **kwargs):
            return [{"guid": "table-101", "name": "101"}]
        def fetch_employees(self, *args, **kwargs):
            return [{"guid": "server-1", "firstName": "Alice", "lastName": "Smith"}]
        def fetch_orders_for_date(self, *args, **kwargs):
            return [{
                "guid": "order-1",
                "source": "In Store",
                "table": {"guid": "table-101", "name": "101"},
                "openedDate": "2026-06-05T23:00:00.000+0000",
                "checks": [{
                    "guid": "check-1",
                    "displayNumber": "7",
                    "openedDate": "2026-06-05T23:00:00.000+0000",
                    "closedDate": None, # open check
                    "amount": 25.5,
                    "server": {"guid": "server-1"},
                    "selections": [{
                        "guid": "sel-1",
                        "displayName": "Tacos",
                        "quantity": 2,
                        "price": 10.0,
                        "netAmount": 20.0
                    }]
                }]
            }]
            
    from app.services.toast_client import ToastClient
    monkeypatch.setattr(ToastClient, "shared", lambda: FakeToast())
    
    with app.test_request_context():
        g.current_user = db.query(User).filter_by(permission_level="partner").first()
        res = fetch_toast_live_data_tool("tables", "tomball")
        assert len(res) == 1
        assert res[0]["table_name"] == "101"
        assert res[0]["check_number"] == "7"
        assert res[0]["assigned_server"] == "Alice Smith"
        assert res[0]["amount_so_far"] == 25.5
        assert len(res[0]["items_rung_in"]) == 1
        assert res[0]["items_rung_in"][0]["name"] == "Tacos"
        assert res[0]["items_rung_in"][0]["quantity"] == 2

def test_fetch_toast_live_data_tool_clockins(app_with_user, monkeypatch):
    app, client_for, db = app_with_user
    from app.web.assistant_routes import fetch_toast_live_data_tool
    
    monkeypatch.setattr("app.services.toast_client.restaurant_guids", lambda: {"tomball": "guid-tomball"})
    
    class FakeToast:
        def fetch_employees(self, *args, **kwargs):
            return [{"guid": "emp-1", "firstName": "Bob", "lastName": "Jones"}]
        def fetch_jobs(self, *args, **kwargs):
            return [{"guid": "job-1", "title": "Cook"}]
        def fetch_time_entries(self, *args, **kwargs):
            return [{
                "guid": "te-1",
                "employeeReference": {"guid": "emp-1"},
                "jobReference": {"guid": "job-1"},
                "inDate": "2026-06-05T09:00:00.000-0500",
                "outDate": None, # clocked in
                "regularHours": 4.5,
                "overtimeHours": 0.0,
                "hourlyWage": 15.0 # should be masked!
            }]
            
    from app.services.toast_client import ToastClient
    monkeypatch.setattr(ToastClient, "shared", lambda: FakeToast())
    
    with app.test_request_context():
        g.current_user = db.query(User).filter_by(permission_level="partner").first()
        res = fetch_toast_live_data_tool("clockins", "tomball")
        assert len(res) == 1
        assert res[0]["employee_name"] == "Bob Jones"
        assert res[0]["position"] == "Cook"
        assert res[0]["regular_hours"] == 4.5
        # Ensure wage info is NOT leaked
        assert "hourlyWage" not in res[0]
        assert "wage" not in res[0]
        assert "rate" not in res[0]

def test_fetch_toast_live_data_tool_sales(app_with_user, monkeypatch):
    app, client_for, db = app_with_user
    from app.web.assistant_routes import fetch_toast_live_data_tool
    
    monkeypatch.setattr("app.services.toast_client.restaurant_guids", lambda: {"tomball": "guid-tomball"})
    
    class FakeToast:
        def fetch_orders_for_date(self, *args, **kwargs):
            return [{
                "guid": "order-1",
                "customerCount": 2,
                "checks": [
                    {
                        "guid": "check-1",
                        "closedDate": "2026-06-05T23:06:00.000+0000", # closed
                        "amount": 25.5,
                        "selections": [{"displayName": "Fajitas", "quantity": 1}]
                    },
                    {
                        "guid": "check-2",
                        "closedDate": None, # open
                        "amount": 10.0,
                        "selections": [{"displayName": "Fajitas", "quantity": 2}, {"displayName": "Rita", "quantity": 1}]
                    }
                ]
            }]
            
    from app.services.toast_client import ToastClient
    monkeypatch.setattr(ToastClient, "shared", lambda: FakeToast())
    
    with app.test_request_context():
        g.current_user = db.query(User).filter_by(permission_level="partner").first()
        res = fetch_toast_live_data_tool("sales", "tomball")
        assert res["check_counts"] == 2
        assert res["closed_checks"] == 1
        assert res["open_checks"] == 1
        assert res["net_sales_closed"] == 25.5
        assert res["total_guests"] == 4 # 2 checks x 2 guests (default/sum)
        assert len(res["top_items_rung_in"]) == 2
        assert res["top_items_rung_in"][0]["name"] == "Fajitas"
        assert res["top_items_rung_in"][0]["quantity"] == 3

