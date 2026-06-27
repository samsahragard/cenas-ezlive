import base64
from types import SimpleNamespace

from app.services import management_email as mail


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_gmail_message_summary_extracts_body_and_attachments():
    raw = {
        "id": "abc123",
        "threadId": "thread123",
        "internalDate": "1717000000000",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Preview",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Catering update"},
                {"name": "From", "value": "sender@example.com"},
                {"name": "To", "value": "sam@example.com"},
                {"name": "Message-ID", "value": "<m1@example.com>"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("Hello from the inbox.")},
                },
                {
                    "filename": "invoice.pdf",
                    "mimeType": "application/pdf",
                    "body": {"attachmentId": "att1", "size": 42},
                },
            ],
        },
    }

    summary = mail._gmail_message_summary(raw)

    assert summary["id"] == "abc123"
    assert summary["unread"] is True
    assert summary["subject"] == "Catering update"
    assert summary["body_text"] == "Hello from the inbox."
    assert summary["attachment_count"] == 1
    assert summary["attachments"][0]["filename"] == "invoice.pdf"


def test_public_accounts_uses_json_without_leaking_secrets(monkeypatch):
    monkeypatch.setenv("MANAGEMENT_EMAIL_ALLOW_MULTIPLE", "1")
    monkeypatch.setenv(
        "MANAGEMENT_EMAIL_ACCOUNTS_JSON",
        '[{"key":"ops","label":"Ops","address":"ops@example.com",'
        '"provider":"imap_smtp","imap_password":"secret"}]',
    )

    accounts = mail.public_accounts(SimpleNamespace(email="ops@example.com"))

    assert accounts == [
        {
            "key": "ops",
            "label": "Ops",
            "address": "ops@example.com",
            "provider": "imap_smtp",
            "connected": True,
            "can_send": False,
            "cached_count": 0,
            "last_imported_at": None,
            "last_cached_date": None,
        }
    ]


def test_public_accounts_filters_to_current_login(monkeypatch):
    monkeypatch.setenv("MANAGEMENT_EMAIL_ACCOUNTS_JSON", """
    [
      {"key":"sam","label":"Sam","address":"sam@cenaskitchen.com",
       "provider":"imap_smtp","imap_password":"secret",
       "login_aliases":["sam@cenaskitchen.com","samsahragard@gmail.com"],
       "login_names":["Sam Sahragard"]},
      {"key":"masood","label":"Masood","address":"masood@cenaskitchen.com",
       "provider":"imap_smtp","imap_password":"secret",
       "login_names":["Masood Sahragard"]}
    ]
    """)

    sam_accounts = mail.public_accounts(SimpleNamespace(email="samsahragard@gmail.com"))
    masood_accounts = mail.public_accounts(SimpleNamespace(email=None, full_name="Masood Sahragard"))
    no_accounts = mail.public_accounts(SimpleNamespace(email="javier@cenaskitchen.com"))

    assert [a["key"] for a in sam_accounts] == ["sam"]
    assert [a["key"] for a in masood_accounts] == ["masood"]
    assert no_accounts == []
