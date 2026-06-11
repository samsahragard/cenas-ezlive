"""Tests for the Sam-only write gate on the three db-probe WRITE endpoints
(Sam 2026-06-10 directive: prod writes only with Sam's permission).

Covers, for deactivate-users / deactivate-drivers / set-order-status:
  - SAM_OPS_TOKEN unset  -> 403 "writes disabled" even with a valid X-Cena-Token
  - SAM_OPS_TOKEN set, missing/wrong X-Sam-Ops-Token -> 403
  - SAM_OPS_TOKEN set + correct header -> request passes the gate
  - gateway token still required FIRST (sam token alone is not enough)
  - READ probes are untouched (work with no SAM_OPS_TOKEN at all)
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client(db_session, monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")
    monkeypatch.setenv("CENA_GATEWAY_TOKEN", "testtoken")
    monkeypatch.setenv("SAM_CHAT_USER_ID", "1")
    monkeypatch.delenv("SAM_OPS_TOKEN", raising=False)

    import app.db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    import app.web.cena as cena_mod
    monkeypatch.setattr(cena_mod, "SessionLocal", lambda: db_session)

    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


_WRITES = [
    ("/sam/cena/db-probe/deactivate-users", {"ids": [], "dry_run": True}),
    ("/sam/cena/db-probe/deactivate-drivers", {"ids": [999999], "dry_run": True}),
    ("/sam/cena/db-probe/set-order-status",
     {"id": 999999, "status": "cancelled", "dry_run": True}),
]


def _post(client, path, body, cena="testtoken", sam=None):
    headers = {}
    if cena is not None:
        headers["X-Cena-Token"] = cena
    if sam is not None:
        headers["X-Sam-Ops-Token"] = sam
    return client.post(path, json=body, headers=headers)


@pytest.mark.parametrize("path,body", _WRITES)
def test_write_disabled_when_sam_ops_unset(client, path, body):
    r = _post(client, path, body)
    assert r.status_code == 403
    assert "writes disabled" in r.get_json()["error"]


@pytest.mark.parametrize("path,body", _WRITES)
def test_write_403_without_sam_header(client, monkeypatch, path, body):
    monkeypatch.setenv("SAM_OPS_TOKEN", "sam-secret")
    r = _post(client, path, body)
    assert r.status_code == 403
    assert "sam approval required" in r.get_json()["error"]


@pytest.mark.parametrize("path,body", _WRITES)
def test_write_403_with_wrong_sam_header(client, monkeypatch, path, body):
    monkeypatch.setenv("SAM_OPS_TOKEN", "sam-secret")
    r = _post(client, path, body, sam="not-the-secret")
    assert r.status_code == 403


@pytest.mark.parametrize("path,body", _WRITES)
def test_gateway_token_still_required_first(client, monkeypatch, path, body):
    monkeypatch.setenv("SAM_OPS_TOKEN", "sam-secret")
    r = _post(client, path, body, cena=None, sam="sam-secret")
    assert r.status_code == 403


def test_write_passes_gate_with_both_tokens(client, monkeypatch):
    monkeypatch.setenv("SAM_OPS_TOKEN", "sam-secret")
    # deactivate-users: empty ids dry-run -> harmless 200 once past the gate.
    r = _post(client, "/sam/cena/db-probe/deactivate-users",
              {"ids": [], "dry_run": True}, sam="sam-secret")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    # deactivate-drivers: unknown id dry-run -> 200 with the id skipped.
    r = _post(client, "/sam/cena/db-probe/deactivate-drivers",
              {"ids": [999999], "dry_run": True}, sam="sam-secret")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    # set-order-status: gate passed -> endpoint logic runs -> 404 unknown order.
    r = _post(client, "/sam/cena/db-probe/set-order-status",
              {"id": 999999, "status": "cancelled", "dry_run": True},
              sam="sam-secret")
    assert r.status_code == 404


def test_reads_untouched_without_sam_ops(client):
    # READ probe with only the gateway token must keep working.
    r = _post(client, "/sam/cena/db-probe/list-drivers", {"active_only": True})
    assert r.status_code == 200 and r.get_json()["ok"] is True
