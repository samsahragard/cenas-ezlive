import os
import json
import sqlite3
import pytest
from unittest.mock import MagicMock

# Add cena-cloud to path so we can import cena_cloud
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cena-cloud")))
os.environ["CENA_CLOUD_TOKEN"] = "mock_token"
import cena_cloud

def test_sync_query_db_success(tmp_path, monkeypatch):
    # Set up temp databases
    root_dir = tmp_path / "cena-db"
    root_dir.mkdir()
    monkeypatch.setattr(cena_cloud, "CENA_CLOUD_ROOT", str(root_dir))
    
    webhook_db_path = root_dir / "toast_webhook.sqlite"
    conn = sqlite3.connect(webhook_db_path)
    conn.execute("CREATE TABLE toast_check_current (check_guid TEXT, amount REAL, closed_date TEXT, voided INTEGER, deleted INTEGER)")
    conn.execute("INSERT INTO toast_check_current VALUES ('check-1', 42.50, NULL, 0, 0)")
    conn.commit()
    conn.close()
    
    # Instantiate handler with mocked socket/address
    class TestHandler(cena_cloud.CenaCloudHandler):
        def __init__(self):
            pass
            
    handler = TestHandler()
    
    # Mock rfile and headers
    query_payload = json.dumps({"sqlQuery": "SELECT amount FROM toast_check_current"}).encode("utf-8")
    handler.headers = {"Content-Length": str(len(query_payload))}
    
    class MockRfile:
        def read(self, n):
            return query_payload[:n]
    handler.rfile = MockRfile()
    
    # Mock _send to capture response
    response_code = None
    response_body = None
    def mock_send(code, ctype, body):
        nonlocal response_code, response_body
        response_code = code
        response_body = json.loads(body.decode("utf-8"))
        
    handler._send = mock_send
    
    # Run query
    handler._sync_query_db()
    
    assert response_code == 200
    assert response_body["success"] is True
    assert len(response_body["results"]) == 1
    assert response_body["results"][0]["amount"] == 42.50

def test_sync_query_db_non_select(tmp_path, monkeypatch):
    root_dir = tmp_path / "cena-db"
    root_dir.mkdir()
    monkeypatch.setattr(cena_cloud, "CENA_CLOUD_ROOT", str(root_dir))
    
    class TestHandler(cena_cloud.CenaCloudHandler):
        def __init__(self):
            pass
            
    handler = TestHandler()
    query_payload = json.dumps({"sqlQuery": "DROP TABLE toast_check_current"}).encode("utf-8")
    handler.headers = {"Content-Length": str(len(query_payload))}
    
    class MockRfile:
        def read(self, n):
            return query_payload[:n]
    handler.rfile = MockRfile()
    
    response_code = None
    response_body = None
    def mock_send(code, ctype, body):
        nonlocal response_code, response_body
        response_code = code
        response_body = json.loads(body.decode("utf-8"))
    handler._send = mock_send
    
    handler._sync_query_db()
    
    assert response_code == 400
    assert response_body["success"] is False
    assert "Only SELECT or WITH" in response_body["error"]

def test_sync_query_db_missing_db(tmp_path, monkeypatch):
    root_dir = tmp_path / "cena-db"
    root_dir.mkdir()
    monkeypatch.setattr(cena_cloud, "CENA_CLOUD_ROOT", str(root_dir))
    
    class TestHandler(cena_cloud.CenaCloudHandler):
        def __init__(self):
            pass
            
    handler = TestHandler()
    query_payload = json.dumps({"sqlQuery": "SELECT * FROM toast_check_current"}).encode("utf-8")
    handler.headers = {"Content-Length": str(len(query_payload))}
    
    class MockRfile:
        def read(self, n):
            return query_payload[:n]
    handler.rfile = MockRfile()
    
    response_code = None
    response_body = None
    def mock_send(code, ctype, body):
        nonlocal response_code, response_body
        response_code = code
        response_body = json.loads(body.decode("utf-8"))
    handler._send = mock_send
    
    handler._sync_query_db()
    
    assert response_code == 404
    assert response_body["success"] is False
    assert "Database toast_webhook.sqlite not found" in response_body["error"]
