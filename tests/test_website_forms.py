from __future__ import annotations

import io
import re
from pathlib import Path

from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, User, WebsiteFormSubmission
from app.web import website_forms as wf


def _test_app(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(wf, "SessionLocal", SessionLocal)
    monkeypatch.setenv("FORM_UPLOAD_DIR", str(tmp_path / "uploads"))

    template_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
    static_dir = Path(__file__).resolve().parents[1] / "app" / "static"
    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir),
    )
    app.secret_key = "test"
    app.jinja_env.globals["current_user_stores"] = lambda: [
        ("dos", "Tomball"),
        ("uno", "Copperfield"),
    ]
    app.jinja_env.globals["has_dashboard_access"] = lambda *_args, **_kwargs: True
    app.jinja_env.globals["has_permission"] = lambda *_args, **_kwargs: True
    app.jinja_env.globals["subnav_for"] = lambda *_args, **_kwargs: []
    app.jinja_env.globals["anomaly_signals_for"] = lambda *_args, **_kwargs: []
    app.register_blueprint(wf.website_forms_bp)
    return app, SessionLocal


def _make_user(
    SessionLocal,
    *,
    full_name="Test Manager",
    email=None,
    role="gm",
    scope="tomball",
):
    db = SessionLocal()
    try:
        row = User(
            full_name=full_name,
            email=email,
            passcode_hash="x",
            permission_level=role,
            store_scope=scope,
            first_login_done=True,
            active=True,
            session_version=1,
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_session_version"] = 1


def _make_submission(SessionLocal, **overrides):
    data = {
        "form_type": "career",
        "status": "new",
        "location": "Tomball",
        "position": "Server",
        "applicant_name": "Test Applicant",
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


def test_public_email_list_submission_persists_email(monkeypatch, tmp_path):
    app, SessionLocal = _test_app(monkeypatch, tmp_path)

    response = app.test_client().post(
        "/public/forms/newsletter",
        data={
            "_source_page": "https://www.cenaskitchen.com/#footer",
            "email": "guest@example.com",
        },
    )

    assert response.status_code == 303
    db = SessionLocal()
    try:
        row = db.query(WebsiteFormSubmission).one()
        assert row.form_type == "email-list"
        assert row.subject == "Email list signup"
        assert row.email == "guest@example.com"
        assert row.source_page == "https://www.cenaskitchen.com/#footer"
        assert row.fields == {"email": "guest@example.com"}
    finally:
        db.close()


def test_public_email_list_submission_requires_email(monkeypatch, tmp_path):
    app, _SessionLocal = _test_app(monkeypatch, tmp_path)

    response = app.test_client().post("/public/forms/email-list", data={})

    assert response.status_code == 400


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


def test_full_access_user_sees_unshared_submissions_and_share_controls(monkeypatch, tmp_path):
    app, SessionLocal = _test_app(monkeypatch, tmp_path)
    sam_id = _make_user(
        SessionLocal,
        full_name="Sam Sahragard",
        email="sam@cenaskitchen.com",
        role="partner",
        scope=None,
    )
    _make_submission(
        SessionLocal,
        location="Copperfield",
        position="Server",
        applicant_name="Codex Career Test",
        fields={"additional_comments": "private until shared"},
    )

    client = app.test_client()
    _login(client, sam_id)
    response = client.get("/partner/website-forms?type=career")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Codex Career Test" in body
    assert "Share with" in body
    assert "Not shared" in body
    assert 'aria-label="Submission location"' in body
    assert "Live website inbox" not in body
    assert "Cenas / Website / Submissions" not in body


def test_email_list_tab_renders_for_full_access_user(monkeypatch, tmp_path):
    app, SessionLocal = _test_app(monkeypatch, tmp_path)
    sam_id = _make_user(
        SessionLocal,
        full_name="Sam Sahragard",
        email="sam@cenaskitchen.com",
        role="partner",
        scope=None,
    )
    _make_submission(
        SessionLocal,
        form_type="email-list",
        location=None,
        position=None,
        subject="Email list signup",
        applicant_name=None,
        email="guest@example.com",
        fields={"email": "guest@example.com"},
    )

    client = app.test_client()
    _login(client, sam_id)
    response = client.get("/partner/website-forms?type=email-list")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Email List" in body
    assert "guest@example.com" in body
    assert '<span class="wf-tab-label">Email List</span>' in body
    assert "<small>1</small>" in body
    assert 'aria-label="Submission status"' in body
    assert 'aria-label="Submission location"' in body
    assert "All statuses" not in body
    assert "All locations" not in body


def test_location_tabs_filter_all_form_tab_counts(monkeypatch, tmp_path):
    app, SessionLocal = _test_app(monkeypatch, tmp_path)
    sam_id = _make_user(
        SessionLocal,
        full_name="Sam Sahragard",
        email="sam@cenaskitchen.com",
        role="partner",
        scope=None,
    )
    _make_submission(
        SessionLocal,
        form_type="career",
        location="Copperfield",
        position="Server",
        applicant_name="Copper Career",
    )
    _make_submission(
        SessionLocal,
        form_type="career",
        location="Tomball",
        position="Server",
        applicant_name="Tomball Career",
    )
    _make_submission(
        SessionLocal,
        form_type="catering",
        location="Copperfield",
        subject="Catering request",
        applicant_name="Copper Catering",
    )
    _make_submission(
        SessionLocal,
        form_type="catering",
        location="Tomball",
        subject="Catering request",
        applicant_name="Tomball Catering",
    )

    client = app.test_client()
    _login(client, sam_id)
    response = client.get("/partner/website-forms?type=career&location=Copperfield")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Copper Career" in body
    assert "Tomball Career" not in body
    assert 'href="/partner/website-forms?type=catering&amp;status=&amp;location=Copperfield"' in body
    assert re.search(r'<span class="wf-tab-short">Career</span>\s*<small>1</small>', body)
    assert re.search(r'<span class="wf-tab-short">Catering</span>\s*<small>1</small>', body)


def test_full_access_user_can_soft_delete_submission(monkeypatch, tmp_path):
    app, SessionLocal = _test_app(monkeypatch, tmp_path)
    sam_id = _make_user(
        SessionLocal,
        full_name="Sam Sahragard",
        email="sam@cenaskitchen.com",
        role="partner",
        scope=None,
    )
    submission_id = _make_submission(
        SessionLocal,
        form_type="career",
        location="Copperfield",
        position="Server",
        applicant_name="Delete Candidate",
    )

    client = app.test_client()
    _login(client, sam_id)
    initial_response = client.get("/partner/website-forms?type=career")
    initial_body = initial_response.get_data(as_text=True)
    assert initial_response.status_code == 200
    assert "Deleted" in initial_body
    assert 'name="status" value="deleted">Delete</button>' in initial_body

    response = client.post(
        f"/partner/website-forms/{submission_id}/status",
        data={"status": "deleted"},
    )
    assert response.status_code == 303
    db = SessionLocal()
    try:
        row = db.get(WebsiteFormSubmission, submission_id)
        assert row.status == "deleted"
        assert row.status_changed_by_user_id == sam_id
        assert row.status_changed_at is not None
    finally:
        db.close()

    all_response = client.get("/partner/website-forms?type=career")
    all_body = all_response.get_data(as_text=True)
    assert all_response.status_code == 200
    assert "Delete Candidate" not in all_body

    deleted_response = client.get("/partner/website-forms?type=career&status=deleted")
    deleted_body = deleted_response.get_data(as_text=True)
    assert deleted_response.status_code == 200
    assert "Delete Candidate" in deleted_body
    assert '<span class="wf-badge deleted">deleted</span>' in deleted_body
    assert re.search(r"Deleted\s+by\s+Sam Sahragard", deleted_body)


def test_manager_sees_own_store_careers_without_share_and_can_archive(monkeypatch, tmp_path):
    app, SessionLocal = _test_app(monkeypatch, tmp_path)
    tomball_manager_id = _make_user(
        SessionLocal,
        full_name="Adriana Herrera",
        role="foh_manager",
        scope="tomball",
    )
    tomball_submission_id = _make_submission(
        SessionLocal,
        form_type="career",
        location="Tomball",
        position="Server",
        applicant_name="Tomball Career Applicant",
    )
    _make_submission(
        SessionLocal,
        form_type="career",
        location="Copperfield",
        position="Server",
        applicant_name="Copperfield Career Applicant",
    )
    client = app.test_client()

    _login(client, tomball_manager_id)
    response = client.get("/partner/website-forms?type=career")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Tomball Career Applicant" in body
    assert "Copperfield Career Applicant" not in body
    assert "Share with" not in body
    assert 'name="status" value="archived">Archive</button>' in body
    assert 'name="status" value="deleted">Delete</button>' in body
    assert "Mark reviewed" not in body

    response = client.post(
        f"/partner/website-forms/{tomball_submission_id}/status",
        data={"status": "archived"},
    )
    assert response.status_code == 303
    db = SessionLocal()
    try:
        row = db.get(WebsiteFormSubmission, tomball_submission_id)
        assert row.status == "archived"
        assert row.status_changed_by_user_id == tomball_manager_id
        assert row.status_changed_at is not None
    finally:
        db.close()

    response = client.get("/partner/website-forms?type=career&status=archived")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Tomball Career Applicant" in body
    assert re.search(r"Archived\s+by\s+Adriana Herrera", body)


def test_manager_only_sees_shared_non_career_for_their_store(monkeypatch, tmp_path):
    app, SessionLocal = _test_app(monkeypatch, tmp_path)
    tomball_manager_id = _make_user(
        SessionLocal,
        full_name="Tomball Manager",
        role="gm",
        scope="tomball",
    )
    _make_submission(
        SessionLocal,
        form_type="catering",
        location="Tomball",
        subject="Catering request",
        applicant_name="Tomball Catering Lead",
        shared_locations=["tomball"],
    )
    _make_submission(
        SessionLocal,
        form_type="catering",
        location="Copperfield",
        subject="Catering request",
        applicant_name="Copperfield Catering Lead",
        shared_locations=["tomball", "copperfield"],
    )
    _make_submission(
        SessionLocal,
        form_type="catering",
        location="Tomball",
        subject="Catering request",
        applicant_name="Unshared Tomball Catering Lead",
        shared_locations=[],
    )
    client = app.test_client()
    _login(client, tomball_manager_id)

    response = client.get("/partner/website-forms?type=catering")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Tomball Catering Lead" in body
    assert "Copperfield Catering Lead" not in body
    assert "Unshared Tomball Catering Lead" not in body
    assert "Share with" not in body
    assert 'name="status" value="archived">Archive</button>' not in body


def test_expo_cannot_access_website_forms(monkeypatch, tmp_path):
    app, SessionLocal = _test_app(monkeypatch, tmp_path)
    expo_id = _make_user(
        SessionLocal,
        full_name="Expo User",
        role="expo",
        scope="tomball",
    )
    client = app.test_client()
    _login(client, expo_id)

    response = client.get("/partner/website-forms?type=career")

    assert response.status_code == 403


def test_manager_cannot_share_non_career_or_change_other_store_status(monkeypatch, tmp_path):
    app, SessionLocal = _test_app(monkeypatch, tmp_path)
    manager_id = _make_user(
        SessionLocal,
        full_name="Copperfield Manager",
        role="gm",
        scope="copperfield",
    )
    catering_id = _make_submission(
        SessionLocal,
        form_type="catering",
        location="Copperfield",
        subject="Catering request",
        shared_locations=["copperfield"],
    )
    other_store_career_id = _make_submission(
        SessionLocal,
        form_type="career",
        location="Tomball",
        applicant_name="Other Store Career",
    )
    client = app.test_client()
    _login(client, manager_id)

    share_response = client.post(
        f"/partner/website-forms/{catering_id}/share",
        data={"share_target": "both"},
    )
    non_career_status_response = client.post(
        f"/partner/website-forms/{catering_id}/status",
        data={"status": "archived"},
    )
    other_store_status_response = client.post(
        f"/partner/website-forms/{other_store_career_id}/status",
        data={"status": "archived"},
    )

    assert share_response.status_code == 403
    assert non_career_status_response.status_code == 403
    assert other_store_status_response.status_code == 403


def test_sub_form_select_options_use_readable_dark_colors():
    template = Path(__file__).resolve().parents[1] / "app" / "templates" / "website_forms.html"
    source = template.read_text(encoding="utf-8")

    assert ".wf-share select option" in source
    assert "background: var(--wf-card);" in source
    assert "color: var(--wf-cream);" in source
    assert ".wf-share select option:checked" in source


def test_sub_form_mobile_tabs_fit_one_row_without_horizontal_scroll():
    template = Path(__file__).resolve().parents[1] / "app" / "templates" / "website_forms.html"
    source = template.read_text(encoding="utf-8")

    assert ".wf-status-tabs {\n      display: grid;" in source
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in source
    assert ".wf-location-tabs {\n      display: grid;" in source
    assert ".wf-location-tab" in source
    assert ".wf-tabs {\n      display: grid;" in source
    assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in source
    assert "overflow: visible;" in source
    assert ".wf-tab {\n      display: inline-flex;" in source
    assert ".wf-tab-label { display: none; }" in source
    assert ".wf-tab-short { display: inline; }" in source
    assert "wf-filters" not in source
    assert "wf-access-note" not in source
