from __future__ import annotations

from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, WebsiteFormSubmission
from app.web import cena


def _cleanup_app(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(cena, "SessionLocal", SessionLocal)
    monkeypatch.setenv("CENA_GATEWAY_TOKEN", "secret")

    app = Flask(__name__)
    app.register_blueprint(cena.cena_bp)
    return app, SessionLocal


def _make_submission(SessionLocal, **overrides):
    data = {
        "form_type": "career",
        "status": "new",
        "location": "Copperfield",
        "position": "Server",
        "applicant_name": "Actual Applicant",
        "email": "guest@example.com",
        "fields": {},
        "attachments": [],
        "shared_locations": [],
    }
    data.update(overrides)
    db = SessionLocal()
    try:
        row = WebsiteFormSubmission(**data)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_cleanup_codex_website_forms_dry_run_then_delete(monkeypatch):
    app, SessionLocal = _cleanup_app(monkeypatch)
    _make_submission(
        SessionLocal,
        applicant_name="Codex Career Test",
        email="codex.qa+subform20260629@cenaskitchen.test",
        attachments=[{"filename": "codex-career-test.txt"}],
    )
    _make_submission(
        SessionLocal,
        form_type="donation",
        organization="School fundraiser",
        contact_name="Dana Real",
        email="dana@example.com",
        fields={"message": "CODEX LIVE TEST 20260629 donation flow"},
    )
    normal_id = _make_submission(
        SessionLocal,
        form_type="catering",
        subject="Catering request",
        applicant_name="Actual Customer",
        email="customer@example.com",
    )
    ignored_id = _make_submission(
        SessionLocal,
        form_type="internal",
        applicant_name="Codex Internal Test",
        email="codex@example.com",
    )

    client = app.test_client()
    headers = {"X-Cena-Token": "secret"}
    dry_response = client.post(
        "/sam/cena/run-cleanup-codex-website-forms",
        json={},
        headers=headers,
    )

    assert dry_response.status_code == 200
    dry_data = dry_response.get_json()
    assert dry_data["dry_run"] is True
    assert dry_data["matched"] == 2
    assert dry_data["deleted"] == 0

    delete_response = client.post(
        "/sam/cena/run-cleanup-codex-website-forms",
        json={"dry_run": False},
        headers=headers,
    )

    assert delete_response.status_code == 200
    delete_data = delete_response.get_json()
    assert delete_data["dry_run"] is False
    assert delete_data["matched"] == 2
    assert delete_data["deleted"] == 2

    db = SessionLocal()
    try:
        remaining_ids = {row.id for row in db.query(WebsiteFormSubmission).all()}
    finally:
        db.close()
    assert remaining_ids == {normal_id, ignored_id}
