import base64
from email.message import EmailMessage
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


def test_html_to_text_skips_email_css():
    html = """
    <html>
      <head><style>body { background: #fff; } .hide { display:none; }</style></head>
      <body>
        <p>Ready Kitchen Warranty</p>
        <p>Warranty Claim 109985 needs additional information.</p>
      </body>
    </html>
    """

    text = mail._html_to_text(html)

    assert "background" not in text
    assert "display:none" not in text
    assert "Ready Kitchen Warranty" in text
    assert "Warranty Claim 109985" in text


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


def test_send_reply_allows_attachment_only(monkeypatch):
    account = mail.MailAccount(
        key="ops",
        label="Ops",
        address="ops@example.com",
        provider="imap_smtp",
        config={"smtp_password": "secret"},
    )
    sent = {}

    monkeypatch.setattr(mail, "get_account", lambda *args, **kwargs: account)
    monkeypatch.setattr(
        mail,
        "get_message",
        lambda *args, **kwargs: {
            "from": "sender@example.com",
            "subject": "Invoice",
        },
    )
    monkeypatch.setattr(
        mail,
        "_imap_send_reply",
        lambda _account, _original, body, attachments=None: sent.update({
            "body": body,
            "attachments": attachments,
        }),
    )

    mail.send_reply(
        "ops",
        "m1",
        "",
        attachments=[{
            "filename": "invoice.pdf",
            "mime_type": "application/pdf",
            "content": b"%PDF",
        }],
    )

    assert sent["body"] == ""
    assert sent["attachments"][0]["filename"] == "invoice.pdf"


def test_reply_attachments_are_added_to_mime_message():
    msg = EmailMessage()
    msg.set_content("See attached.")

    mail._add_reply_attachments(msg, [{
        "filename": "invoice.pdf",
        "mime_type": "application/pdf",
        "content": b"%PDF",
    }])

    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "invoice.pdf"
    assert attachments[0].get_content_type() == "application/pdf"
