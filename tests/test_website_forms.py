from __future__ import annotations

import io

from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, WebsiteFormSubmission
from app.web import website_forms as wf


def _test_app(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(wf, "SessionLocal", SessionLocal)
    monkeypatch.setenv("FORM_UPLOAD_DIR", str(tmp_path / "uploads"))

    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(wf.website_forms_bp)
    return app, SessionLocal


def test_public_career_submission_persists_fields_and_upload(monkeypatch, tmp_path):
    app, SessionLocal = _test_app(monkeypatch, tmp_path)

    response = app.test_client().post(
        "/public/forms/career",
        data={
            "location": "Tomball",
            "desired_position": "Server",
            "first_name": "Maria",
            "last_name": "Lopez",
            "email": "maria@example.com",
            "mobile": "555-111-2222",
            "days_available": ["Mon", "Tue"],
            "resume": (io.BytesIO(b"resume bytes"), "resume.txt"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 303
    db = SessionLocal()
    try:
        row = db.query(WebsiteFormSubmission).one()
        assert row.form_type == "career"
        assert row.location == "Tomball"
        assert row.position == "Server"
        assert row.applicant_name == "Maria Lopez"
        assert row.email == "maria@example.com"
        assert row.phone == "555-111-2222"
        assert row.fields["days_available"] == ["Mon", "Tue"]
        assert row.attachments[0]["field"] == "resume"
        assert row.attachments[0]["filename"] == "resume.txt"
    finally:
        db.close()


def test_public_contact_submission_uses_subject_summary(monkeypatch, tmp_path):
    app, SessionLocal = _test_app(monkeypatch, tmp_path)

    response = app.test_client().post(
        "/public/forms/contact",
        data={
            "name": "Alex Guest",
            "email": "alex@example.com",
            "subject": "Feedback",
            "message": "The fajitas were great.",
        },
    )

    assert response.status_code == 303
    db = SessionLocal()
    try:
        row = db.query(WebsiteFormSubmission).one()
        assert row.form_type == "contact"
        assert row.subject == "Feedback"
        assert row.applicant_name == "Alex Guest"
        assert row.fields["message"] == "The fajitas were great."
    finally:
        db.close()


def test_public_submit_rejects_scheme_relative_next(monkeypatch, tmp_path):
    app, _SessionLocal = _test_app(monkeypatch, tmp_path)

    response = app.test_client().post(
        "/public/forms/contact",
        data={
            "_next": "//example.com/steal",
            "name": "Alex Guest",
            "email": "alex@example.com",
            "subject": "Feedback",
            "message": "The fajitas were great.",
        },
    )

    assert response.status_code == 303
    assert response.headers["Location"].startswith("/public/forms/thanks")
