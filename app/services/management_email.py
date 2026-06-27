"""Management email account adapters.

The dashboard email surface can read from either Gmail OAuth or a standard
IMAP/SMTP mailbox. Secrets come from environment variables or operator-owned
secret files; nothing is stored in the repository or database.
"""
from __future__ import annotations

import base64
import html
import imaplib
import json
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import or_


class MailConfigError(RuntimeError):
    """Mailbox is configured incompletely or has no usable credential."""


class MailProviderError(RuntimeError):
    """Remote mail provider rejected a read/send operation."""


_SECRET_DIRS = [
    Path(p)
    for p in (
        os.getenv("CENA_SECRETS_DIR"),
        r"C:\Users\sam\cena-secrets",
        r"C:\Users\sam\cena\.secrets",
        "/var/data/secrets",
    )
    if p
]


@dataclass(frozen=True)
class MailAccount:
    key: str
    label: str
    address: str
    provider: str
    config: dict[str, Any]

    @property
    def can_send(self) -> bool:
        if self.provider == "gmail_oauth":
            return bool(_gmail_refresh_token(self) and _gmail_client_id(self) and _gmail_client_secret(self))
        return bool(_account_secret(self, "smtp_password", ["smtp_password_file"]))

    @property
    def connected(self) -> bool:
        if self.provider == "gmail_oauth":
            return bool(_gmail_refresh_token(self) and _gmail_client_id(self) and _gmail_client_secret(self))
        return bool(_account_secret(self, "imap_password", ["imap_password_file"]))


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"br", "p", "div", "tr", "li"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if data:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        return "\n".join(line.rstrip() for line in raw.splitlines()).strip()


def _html_to_text(value: str) -> str:
    parser = _HTMLText()
    parser.feed(value or "")
    return html.unescape(parser.text())


def _parse_mail_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        clean = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _summary_datetime(item: dict[str, Any]) -> datetime | None:
    return _iso_to_datetime(item.get("date_iso")) or _parse_mail_datetime(item.get("date"))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _read_secret_file(path_or_name: str | None) -> str | None:
    if not path_or_name:
        return None
    candidates = [Path(path_or_name)]
    for base in _SECRET_DIRS:
        candidates.append(base / path_or_name)
    for path in candidates:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
    return None


def _account_secret(account: MailAccount, field: str, file_fields: list[str]) -> str | None:
    env_names: list[str] = []
    env_name = account.config.get(f"{field}_env")
    if env_name:
        env_names.append(str(env_name))
    extra_envs = account.config.get(f"{field}_envs") or []
    if isinstance(extra_envs, str):
        env_names.extend(x.strip() for x in extra_envs.split(",") if x.strip())
    elif isinstance(extra_envs, list):
        env_names.extend(str(x).strip() for x in extra_envs if str(x).strip())
    for name in env_names:
        if os.getenv(name):
            return os.getenv(name, "").strip()
    direct = account.config.get(field)
    if direct:
        return str(direct).strip()
    for file_field in file_fields:
        val = _read_secret_file(account.config.get(file_field))
        if val:
            return val
    return None


def _gmail_token_doc(account: MailAccount) -> dict[str, Any] | None:
    token_file = (
        account.config.get("gmail_token_file")
        or os.getenv("MANAGEMENT_GMAIL_TOKEN_FILE")
        or os.getenv("SAM_GMAIL_TOKEN_FILE")
    )
    if token_file:
        doc = _read_json(Path(str(token_file)))
        if doc:
            return doc
    return None


def _gmail_client_id(account: MailAccount) -> str | None:
    if os.getenv(str(account.config.get("gmail_client_id_env") or "")):
        return os.getenv(str(account.config.get("gmail_client_id_env")), "").strip()
    if account.config.get("gmail_client_id"):
        return str(account.config["gmail_client_id"]).strip()
    doc = _gmail_token_doc(account)
    return str(doc.get("client_id")).strip() if doc and doc.get("client_id") else None


def _gmail_client_secret(account: MailAccount) -> str | None:
    if os.getenv(str(account.config.get("gmail_client_secret_env") or "")):
        return os.getenv(str(account.config.get("gmail_client_secret_env")), "").strip()
    if account.config.get("gmail_client_secret"):
        return str(account.config["gmail_client_secret"]).strip()
    doc = _gmail_token_doc(account)
    return str(doc.get("client_secret")).strip() if doc and doc.get("client_secret") else None


def _gmail_refresh_token(account: MailAccount) -> str | None:
    if os.getenv(str(account.config.get("gmail_refresh_token_env") or "")):
        return os.getenv(str(account.config.get("gmail_refresh_token_env")), "").strip()
    if account.config.get("gmail_refresh_token"):
        return str(account.config["gmail_refresh_token"]).strip()
    doc = _gmail_token_doc(account)
    return str(doc.get("refresh_token")).strip() if doc and doc.get("refresh_token") else None


def _gmail_access_token(account: MailAccount) -> str:
    client_id = _gmail_client_id(account)
    client_secret = _gmail_client_secret(account)
    refresh_token = _gmail_refresh_token(account)
    if not (client_id and client_secret and refresh_token):
        raise MailConfigError("Sam Gmail is missing OAuth client id, secret, or refresh token.")

    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if not resp.ok:
        raise MailProviderError("Gmail OAuth refresh failed.")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise MailProviderError("Gmail OAuth refresh returned no access token.")
    return str(token)


def _gmail_request(account: MailAccount, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    token = _gmail_access_token(account)
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/{path.lstrip('/')}"
    resp = requests.request(method, url, headers=headers, timeout=25, **kwargs)
    if not resp.ok:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except Exception:
            detail = resp.text[:160]
        raise MailProviderError(detail or f"Gmail request failed with HTTP {resp.status_code}.")
    if resp.content:
        return resp.json()
    return {}


def _b64url_decode(data: str | None) -> bytes:
    if not data:
        return b""
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    headers = payload.get("headers") or []
    return {
        str(h.get("name", "")).lower(): str(h.get("value", ""))
        for h in headers
        if h.get("name")
    }


def _walk_gmail_payload(part: dict[str, Any], text_parts: list[str],
                        attachments: list[dict[str, Any]]) -> None:
    mime_type = str(part.get("mimeType") or "").lower()
    body = part.get("body") or {}
    filename = str(part.get("filename") or "")
    attachment_id = body.get("attachmentId")

    if filename or attachment_id:
        attachments.append({
            "id": str(attachment_id or ""),
            "filename": filename or "attachment",
            "mime_type": part.get("mimeType") or "application/octet-stream",
            "size": int(body.get("size") or 0),
        })
    elif body.get("data"):
        raw = _b64url_decode(body.get("data"))
        text = raw.decode("utf-8", errors="replace")
        if mime_type == "text/plain":
            text_parts.append(text.strip())
        elif mime_type == "text/html":
            text_parts.append(_html_to_text(text))

    for child in part.get("parts") or []:
        _walk_gmail_payload(child, text_parts, attachments)


def _gmail_message_summary(raw: dict[str, Any]) -> dict[str, Any]:
    payload = raw.get("payload") or {}
    headers = _header_map(payload)
    text_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    _walk_gmail_payload(payload, text_parts, attachments)
    internal_ms = int(raw.get("internalDate") or 0)
    date_iso = None
    if internal_ms:
        date_iso = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc).isoformat()
    return {
        "id": raw.get("id"),
        "thread_id": raw.get("threadId"),
        "subject": headers.get("subject") or "(no subject)",
        "from": headers.get("from") or "",
        "to": headers.get("to") or "",
        "date": headers.get("date") or "",
        "date_iso": date_iso,
        "snippet": raw.get("snippet") or "",
        "unread": "UNREAD" in set(raw.get("labelIds") or []),
        "attachments": attachments,
        "attachment_count": len(attachments),
        "body_text": "\n\n".join(p for p in text_parts if p).strip(),
        "message_id": headers.get("message-id") or "",
        "references": headers.get("references") or "",
        "reply_to": headers.get("reply-to") or headers.get("from") or "",
    }


def _normalize_account(data: dict[str, Any]) -> MailAccount:
    key = str(data.get("key") or data.get("address") or "mailbox").strip().lower()
    key = "".join(ch for ch in key if ch.isalnum() or ch in {"-", "_"}) or "mailbox"
    address = str(data.get("address") or data.get("email") or "").strip()
    return MailAccount(
        key=key,
        label=str(data.get("label") or address or key).strip(),
        address=address,
        provider=str(data.get("provider") or "imap_smtp").strip().lower(),
        config=dict(data),
    )


def _accounts_from_env_json() -> list[MailAccount]:
    raw = os.getenv("MANAGEMENT_EMAIL_ACCOUNTS_JSON") or os.getenv("MANAGEMENT_EMAIL_ACCOUNTS")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise MailConfigError("MANAGEMENT_EMAIL_ACCOUNTS_JSON is not valid JSON.") from exc
    if not isinstance(parsed, list):
        raise MailConfigError("MANAGEMENT_EMAIL_ACCOUNTS_JSON must be a list.")
    return [_normalize_account(item) for item in parsed if isinstance(item, dict)]


def _default_accounts() -> list[MailAccount]:
    gmail_token_file = (
        os.getenv("MANAGEMENT_GMAIL_TOKEN_FILE")
        or os.getenv("SAM_GMAIL_TOKEN_FILE")
        or r"C:\Users\sam\AppData\Roaming\gogcli\goog-token.json"
    )
    sam_provider = os.getenv("SAM_EMAIL_PROVIDER") or os.getenv("MANAGEMENT_EMAIL_PROVIDER") or "imap_smtp"

    accounts = [
        _normalize_account({
            "key": "sam",
            "label": os.getenv("SAM_EMAIL_LABEL", "sam@cenaskitchen.com"),
            "address": os.getenv("SAM_EMAIL_ADDRESS", "sam@cenaskitchen.com"),
            "provider": sam_provider,
            "gmail_token_file": gmail_token_file,
            "gmail_client_id_env": "SAM_GMAIL_CLIENT_ID",
            "gmail_client_secret_env": "SAM_GMAIL_CLIENT_SECRET",
            "gmail_refresh_token_env": "SAM_GMAIL_REFRESH_TOKEN",
            "imap_host": os.getenv("SAM_IMAP_HOST", os.getenv("MANAGEMENT_IMAP_HOST", "gvam1078.siteground.biz")),
            "imap_port": int(os.getenv("SAM_IMAP_PORT", os.getenv("MANAGEMENT_IMAP_PORT", "993"))),
            "imap_user": os.getenv("SAM_IMAP_USER", os.getenv("MANAGEMENT_IMAP_USER", "sam@cenaskitchen.com")),
            "imap_password_env": "SAM_EMAIL_PWD",
            "imap_password_envs": ["SAM_IMAP_PWD", "SAM_EMAIL_PASSWORD", "MANAGEMENT_EMAIL_PWD"],
            "imap_password_file": os.getenv("SAM_IMAP_PASSWORD_FILE", "sams_imap_pwd.txt"),
            "smtp_host": os.getenv("SAM_SMTP_HOST", os.getenv("MANAGEMENT_SMTP_HOST", "gvam1078.siteground.biz")),
            "smtp_port": int(os.getenv("SAM_SMTP_PORT", os.getenv("MANAGEMENT_SMTP_PORT", "465"))),
            "smtp_user": os.getenv("SAM_SMTP_USER", os.getenv("MANAGEMENT_SMTP_USER", "sam@cenaskitchen.com")),
            "smtp_password_env": "SAM_EMAIL_PWD",
            "smtp_password_envs": ["SAM_SMTP_PWD", "SAM_EMAIL_PASSWORD", "MANAGEMENT_EMAIL_PWD"],
            "smtp_password_file": os.getenv("SAM_SMTP_PASSWORD_FILE", "sams_smtp_pwd.txt"),
        })
    ]
    return accounts


def configured_accounts() -> list[MailAccount]:
    accounts = _accounts_from_env_json() or _default_accounts()
    allow_multiple = os.getenv("MANAGEMENT_EMAIL_ALLOW_MULTIPLE", "").strip().lower() in {"1", "true", "yes", "on"}
    if not allow_multiple:
        only_address = os.getenv("MANAGEMENT_EMAIL_ONLY", "sam@cenaskitchen.com").strip().lower()
        accounts = [
            account for account in accounts
            if account.key == "sam" or account.address.strip().lower() == only_address
        ]
        if not accounts:
            accounts = _default_accounts()
    deduped: dict[str, MailAccount] = {}
    for account in accounts:
        deduped[account.key] = account
    return list(deduped.values())


def public_accounts() -> list[dict[str, Any]]:
    accounts = []
    for account in configured_accounts():
        meta = _cached_account_meta(account)
        accounts.append({
            "key": account.key,
            "label": account.label,
            "address": account.address,
            "provider": account.provider,
            "connected": account.connected,
            "can_send": account.can_send,
            "cached_count": meta["cached_count"],
            "last_imported_at": meta["last_imported_at"],
        })
    return accounts


def default_account_key() -> str | None:
    accounts = configured_accounts()
    for account in accounts:
        if account.connected:
            return account.key
    return accounts[0].key if accounts else None


def get_account(key: str | None) -> MailAccount:
    accounts = configured_accounts()
    target = (key or default_account_key() or "").strip().lower()
    for account in accounts:
        if account.key == target:
            return account
    raise MailConfigError("Unknown mailbox.")


def _gmail_list_messages(account: MailAccount, query: str, limit: int) -> list[dict[str, Any]]:
    q = (query or "").strip()
    gmail_q = f"in:inbox {q}".strip()
    data = _gmail_request(account, "GET", "messages", params={
        "maxResults": max(1, min(limit, 50)),
        "q": gmail_q,
    })
    items = data.get("messages") or []
    messages: list[dict[str, Any]] = []
    for item in items:
        msg = _gmail_request(account, "GET", f"messages/{item['id']}", params={"format": "full"})
        summary = _gmail_message_summary(msg)
        summary.pop("body_text", None)
        messages.append(summary)
    return messages


def _gmail_import_messages(account: MailAccount, cutoff: datetime) -> list[dict[str, Any]]:
    cutoff_date = cutoff.strftime("%Y/%m/%d")
    gmail_q = f"in:anywhere after:{cutoff_date}"
    max_results = max(1, min(int(os.getenv("MANAGEMENT_EMAIL_IMPORT_PAGE_SIZE", "100")), 500))
    page_token = None
    messages: list[dict[str, Any]] = []
    while True:
        params: dict[str, Any] = {"maxResults": max_results, "q": gmail_q}
        if page_token:
            params["pageToken"] = page_token
        data = _gmail_request(account, "GET", "messages", params=params)
        for item in data.get("messages") or []:
            msg = _gmail_request(account, "GET", f"messages/{item['id']}", params={"format": "full"})
            summary = _gmail_message_summary(msg)
            summary["mailbox"] = "Gmail"
            messages.append(summary)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return messages


def _gmail_get_message(account: MailAccount, message_id: str) -> dict[str, Any]:
    msg = _gmail_request(account, "GET", f"messages/{message_id}", params={"format": "full"})
    return _gmail_message_summary(msg)


def _gmail_attachment(account: MailAccount, message_id: str, attachment_id: str) -> bytes:
    data = _gmail_request(account, "GET", f"messages/{message_id}/attachments/{attachment_id}")
    return _b64url_decode(data.get("data"))


def _gmail_send_reply(account: MailAccount, original: dict[str, Any], body: str) -> None:
    msg = EmailMessage()
    msg["From"] = account.address
    msg["To"] = original.get("reply_to") or original.get("from") or ""
    subject = original.get("subject") or ""
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if original.get("message_id"):
        msg["In-Reply-To"] = original["message_id"]
        refs = original.get("references") or original["message_id"]
        msg["References"] = refs
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    payload = {"raw": raw}
    if original.get("thread_id"):
        payload["threadId"] = original["thread_id"]
    _gmail_request(account, "POST", "messages/send", json=payload)


def _imap_connect(account: MailAccount) -> imaplib.IMAP4_SSL:
    password = _account_secret(account, "imap_password", ["imap_password_file"])
    if not password:
        raise MailConfigError(f"{account.label} is missing an IMAP password.")
    host = str(account.config.get("imap_host") or "gvam1078.siteground.biz")
    port = int(account.config.get("imap_port") or 993)
    user = str(account.config.get("imap_user") or account.address)
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(user, password)
    return conn


def _imap_message_id(mailbox: str, uid: str) -> str:
    raw = base64.urlsafe_b64encode(mailbox.encode("utf-8")).decode("ascii").rstrip("=")
    return f"imap-{raw}:{uid}"


def _imap_message_parts(message_id: str) -> tuple[str, str]:
    value = str(message_id or "")
    if value.startswith("imap-") and ":" in value:
        mailbox_part, uid = value[5:].split(":", 1)
        try:
            mailbox = _b64url_decode(mailbox_part).decode("utf-8")
        except Exception:
            mailbox = "INBOX"
        return mailbox or "INBOX", uid
    return "INBOX", value


def _imap_mailboxes(account: MailAccount) -> list[str]:
    raw = account.config.get("imap_mailboxes") or os.getenv("SAM_IMAP_MAILBOXES") or os.getenv("MANAGEMENT_IMAP_MAILBOXES")
    if not raw:
        return ["INBOX"]
    if isinstance(raw, list):
        boxes = [str(x).strip() for x in raw if str(x).strip()]
    else:
        boxes = [x.strip() for x in str(raw).split(",") if x.strip()]
    return boxes or ["INBOX"]


def _decode_addr(value: str | None) -> str:
    return str(value or "").strip()


def _imap_parse_message(uid: str, raw: bytes, include_body: bool, mailbox: str = "INBOX") -> dict[str, Any]:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    attachments: list[dict[str, Any]] = []
    for idx, part in enumerate(msg.iter_attachments()):
        payload = part.get_payload(decode=True) or b""
        attachments.append({
            "id": str(idx),
            "filename": part.get_filename() or f"attachment-{idx + 1}",
            "mime_type": part.get_content_type(),
            "size": len(payload),
        })

    body_text = ""
    if include_body:
        body_part = msg.get_body(preferencelist=("plain", "html"))
        if body_part is not None:
            content = body_part.get_content()
            body_text = _html_to_text(content) if body_part.get_content_type() == "text/html" else str(content).strip()

    date_text = str(msg.get("date") or "")
    date_at = _parse_mail_datetime(date_text)
    return {
        "id": _imap_message_id(mailbox, uid),
        "thread_id": _imap_message_id(mailbox, uid),
        "mailbox": mailbox,
        "subject": str(msg.get("subject") or "(no subject)"),
        "from": _decode_addr(msg.get("from")),
        "to": _decode_addr(msg.get("to")),
        "date": date_text,
        "date_iso": _datetime_to_iso(date_at),
        "snippet": (body_text[:180] if body_text else ""),
        "unread": False,
        "attachments": attachments,
        "attachment_count": len(attachments),
        "body_text": body_text,
        "message_id": str(msg.get("message-id") or ""),
        "references": str(msg.get("references") or ""),
        "reply_to": str(msg.get("reply-to") or msg.get("from") or ""),
    }


def _imap_fetch_raw(conn: imaplib.IMAP4_SSL, uid: str) -> bytes:
    typ, data = conn.uid("FETCH", uid, "(RFC822)")
    if typ != "OK":
        raise MailProviderError("IMAP fetch failed.")
    for item in data:
        if isinstance(item, tuple) and item[1]:
            return item[1]
    raise MailProviderError("IMAP returned no message body.")


def _imap_list_messages(account: MailAccount, query: str, limit: int) -> list[dict[str, Any]]:
    conn = _imap_connect(account)
    try:
        mailbox = "INBOX"
        conn.select(mailbox)
        typ, data = conn.uid("SEARCH", None, "ALL")
        if typ != "OK":
            raise MailProviderError("IMAP search failed.")
        uids = (data[0] or b"").decode("ascii", errors="ignore").split()
        q = (query or "").strip().lower()
        messages: list[dict[str, Any]] = []
        for uid in reversed(uids[-max(limit * 4, limit):]):
            raw = _imap_fetch_raw(conn, uid)
            item = _imap_parse_message(uid, raw, include_body=bool(q), mailbox=mailbox)
            haystack = " ".join([item.get("subject") or "", item.get("from") or "", item.get("body_text") or ""]).lower()
            if q and q not in haystack:
                continue
            item.pop("body_text", None)
            messages.append(item)
            if len(messages) >= limit:
                break
        return messages
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _imap_import_messages(account: MailAccount, cutoff: datetime) -> list[dict[str, Any]]:
    conn = _imap_connect(account)
    since = cutoff.strftime("%d-%b-%Y")
    messages: list[dict[str, Any]] = []
    try:
        for mailbox in _imap_mailboxes(account):
            typ, _ = conn.select(mailbox)
            if typ != "OK":
                continue
            typ, data = conn.uid("SEARCH", None, "SINCE", since)
            if typ != "OK":
                raise MailProviderError("IMAP search failed.")
            uids = (data[0] or b"").decode("ascii", errors="ignore").split()
            for uid in uids:
                raw = _imap_fetch_raw(conn, uid)
                item = _imap_parse_message(uid, raw, include_body=True, mailbox=mailbox)
                date_at = _summary_datetime(item)
                if date_at and date_at < cutoff:
                    continue
                messages.append(item)
        return messages
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _imap_get_message(account: MailAccount, uid: str) -> dict[str, Any]:
    mailbox, message_uid = _imap_message_parts(uid)
    conn = _imap_connect(account)
    try:
        conn.select(mailbox)
        return _imap_parse_message(message_uid, _imap_fetch_raw(conn, message_uid), include_body=True, mailbox=mailbox)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _imap_attachment(account: MailAccount, uid: str, attachment_id: str) -> bytes:
    mailbox, message_uid = _imap_message_parts(uid)
    conn = _imap_connect(account)
    try:
        conn.select(mailbox)
        raw = _imap_fetch_raw(conn, message_uid)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    attachments = list(parsed.iter_attachments())
    idx = int(attachment_id)
    if idx < 0 or idx >= len(attachments):
        raise MailProviderError("Attachment not found.")
    return attachments[idx].get_payload(decode=True) or b""


def _imap_send_reply(account: MailAccount, original: dict[str, Any], body: str) -> None:
    password = _account_secret(account, "smtp_password", ["smtp_password_file"])
    if not password:
        raise MailConfigError(f"{account.label} is missing an SMTP password.")
    msg = EmailMessage()
    msg["From"] = account.address
    msg["To"] = original.get("reply_to") or original.get("from") or ""
    subject = original.get("subject") or ""
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if original.get("message_id"):
        msg["In-Reply-To"] = original["message_id"]
        msg["References"] = original.get("references") or original["message_id"]
    msg.set_content(body)

    host = str(account.config.get("smtp_host") or "gvam1078.siteground.biz")
    port = int(account.config.get("smtp_port") or 465)
    user = str(account.config.get("smtp_user") or account.address)
    with smtplib.SMTP_SSL(host, port) as server:
        server.login(user, password)
        server.send_message(msg)


def _email_db():
    from app.db import SessionLocal

    if SessionLocal is None:
        raise MailConfigError("Email import database is not configured.")
    return SessionLocal()


def _row_to_message(row: Any, include_body: bool = False) -> dict[str, Any]:
    item = {
        "id": row.provider_message_id,
        "thread_id": row.thread_id,
        "mailbox": row.mailbox or "",
        "subject": row.subject or "(no subject)",
        "from": row.from_addr or "",
        "to": row.to_addr or "",
        "date": row.date_text or "",
        "date_iso": _datetime_to_iso(row.date_at),
        "snippet": row.snippet or "",
        "unread": bool(row.unread),
        "attachments": row.attachments_json or [],
        "attachment_count": int(row.attachment_count or 0),
        "message_id": row.message_id_header or "",
        "references": row.references_header or "",
        "reply_to": row.reply_to or row.from_addr or "",
        "cached": True,
    }
    if include_body:
        item["body_text"] = row.body_text or ""
    return item


def _cached_account_meta(account: MailAccount) -> dict[str, Any]:
    try:
        from app.models import ManagementEmailMessage

        db = _email_db()
    except Exception:
        return {"cached_count": 0, "last_imported_at": None}
    try:
        q = db.query(ManagementEmailMessage).filter(
            ManagementEmailMessage.account_key == account.key
        )
        last = q.order_by(ManagementEmailMessage.imported_at.desc()).first()
        return {
            "cached_count": q.count(),
            "last_imported_at": _datetime_to_iso(last.imported_at) if last else None,
        }
    finally:
        db.close()


def _cached_messages(account: MailAccount, query: str, limit: int) -> list[dict[str, Any]] | None:
    try:
        from app.models import ManagementEmailMessage

        db = _email_db()
    except Exception:
        return None
    try:
        q = db.query(ManagementEmailMessage).filter(
            ManagementEmailMessage.account_key == account.key
        )
        needle = (query or "").strip()
        if needle:
            like = f"%{needle}%"
            q = q.filter(or_(
                ManagementEmailMessage.subject.ilike(like),
                ManagementEmailMessage.from_addr.ilike(like),
                ManagementEmailMessage.to_addr.ilike(like),
                ManagementEmailMessage.body_text.ilike(like),
            ))
        rows = q.order_by(
            ManagementEmailMessage.date_at.desc(),
            ManagementEmailMessage.id.desc(),
        ).limit(max(1, min(limit, 200))).all()
        has_cache = db.query(ManagementEmailMessage.id).filter(
            ManagementEmailMessage.account_key == account.key
        ).first() is not None
        if not rows and not has_cache:
            return None
        return [_row_to_message(row, include_body=False) for row in rows]
    finally:
        db.close()


def _cached_message(account: MailAccount, message_id: str) -> dict[str, Any] | None:
    try:
        from app.models import ManagementEmailMessage

        db = _email_db()
    except Exception:
        return None
    try:
        row = db.query(ManagementEmailMessage).filter(
            ManagementEmailMessage.account_key == account.key,
            ManagementEmailMessage.provider_message_id == message_id,
        ).first()
        return _row_to_message(row, include_body=True) if row else None
    finally:
        db.close()


def _upsert_cached_messages(account: MailAccount, messages: list[dict[str, Any]]) -> dict[str, Any]:
    from app.models import ManagementEmailMessage

    now = datetime.utcnow()
    stats = {"scanned": len(messages), "inserted": 0, "updated": 0, "skipped": 0}
    db = _email_db()
    try:
        for item in messages:
            provider_message_id = str(item.get("id") or "").strip()
            if not provider_message_id:
                stats["skipped"] += 1
                continue
            row = db.query(ManagementEmailMessage).filter(
                ManagementEmailMessage.account_key == account.key,
                ManagementEmailMessage.provider_message_id == provider_message_id,
            ).first()
            if row is None:
                row = ManagementEmailMessage(
                    account_key=account.key,
                    account_address=account.address,
                    provider=account.provider,
                    provider_message_id=provider_message_id,
                    imported_at=now,
                )
                db.add(row)
                stats["inserted"] += 1
            else:
                stats["updated"] += 1

            attachments = item.get("attachments") or []
            body_text = str(item.get("body_text") or "")
            snippet = str(item.get("snippet") or (body_text[:180] if body_text else ""))
            row.account_address = account.address
            row.provider = account.provider
            row.thread_id = str(item.get("thread_id") or "")
            row.mailbox = str(item.get("mailbox") or "")
            row.subject = str(item.get("subject") or "(no subject)")[:500]
            row.from_addr = str(item.get("from") or "")
            row.to_addr = str(item.get("to") or "")
            row.date_text = str(item.get("date") or "")
            row.date_at = _summary_datetime(item)
            row.snippet = snippet
            row.body_text = body_text
            row.unread = bool(item.get("unread"))
            row.attachments_json = attachments
            row.attachment_count = int(item.get("attachment_count") or len(attachments))
            row.message_id_header = str(item.get("message_id") or "")[:500]
            row.references_header = str(item.get("references") or "")
            row.reply_to = str(item.get("reply_to") or item.get("from") or "")
            row.imported_at = now
            row.updated_at = now
        db.commit()
        stats["cached_count"] = _cached_account_meta(account)["cached_count"]
        return stats
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def import_recent_messages(account_key: str | None = None, days: int = 60) -> dict[str, Any]:
    account = get_account(account_key)
    if not account.connected:
        raise MailConfigError(f"{account.label} is not connected yet.")
    safe_days = max(1, min(int(days or 60), 365))
    cutoff = datetime.utcnow() - timedelta(days=safe_days)
    if account.provider == "gmail_oauth":
        messages = _gmail_import_messages(account, cutoff)
    else:
        messages = _imap_import_messages(account, cutoff)
    stats = _upsert_cached_messages(account, messages)
    stats.update({
        "account": account.address,
        "account_key": account.key,
        "days": safe_days,
        "cutoff": _datetime_to_iso(cutoff),
    })
    return stats


def list_messages(account_key: str | None = None, query: str = "", limit: int = 25) -> list[dict[str, Any]]:
    account = get_account(account_key)
    cached = _cached_messages(account, query, limit)
    if cached is not None:
        return cached
    if not account.connected:
        raise MailConfigError(f"{account.label} is not connected yet.")
    if account.provider == "gmail_oauth":
        return _gmail_list_messages(account, query, limit)
    return _imap_list_messages(account, query, limit)


def get_message(account_key: str | None, message_id: str) -> dict[str, Any]:
    account = get_account(account_key)
    cached = _cached_message(account, message_id)
    if cached is not None:
        return cached
    if not account.connected:
        raise MailConfigError(f"{account.label} is not connected yet.")
    if account.provider == "gmail_oauth":
        return _gmail_get_message(account, message_id)
    return _imap_get_message(account, message_id)


def get_attachment(account_key: str | None, message_id: str, attachment_id: str) -> bytes:
    account = get_account(account_key)
    if not account.connected:
        raise MailConfigError(f"{account.label} is not connected yet.")
    if account.provider == "gmail_oauth":
        return _gmail_attachment(account, message_id, attachment_id)
    return _imap_attachment(account, message_id, attachment_id)


def attachment_stream(account_key: str | None, message_id: str, attachment_id: str) -> BytesIO:
    return BytesIO(get_attachment(account_key, message_id, attachment_id))


def send_reply(account_key: str | None, message_id: str, body: str) -> None:
    clean_body = (body or "").strip()
    if not clean_body:
        raise MailConfigError("Reply body is required.")
    account = get_account(account_key)
    if not account.can_send:
        raise MailConfigError(f"{account.label} is not configured for sending.")
    original = get_message(account.key, message_id)
    if account.provider == "gmail_oauth":
        _gmail_send_reply(account, original, clean_body)
    else:
        _imap_send_reply(account, original, clean_body)
