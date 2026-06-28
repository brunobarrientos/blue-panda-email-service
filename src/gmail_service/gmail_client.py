"""Gmail API client — send, list, search, get messages/threads."""

from __future__ import annotations

import base64
import binascii
import email.encoders
import email.mime.base
import email.mime.multipart
import email.mime.text
import logging
import mimetypes
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)


def _build_service(creds: Credentials) -> Any:
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_body(payload: dict) -> str:
    """Best-effort decode of a message body from Gmail payload."""
    if not payload:
        return ""

    # Direct body
    body = payload.get("body", {})
    data = body.get("data")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Multipart — look for text/plain or text/html
    parts = payload.get("parts", [])
    for preferred in ("text/plain", "text/html"):
        for part in parts:
            if part.get("mimeType") == preferred:
                part_data = part.get("body", {}).get("data")
                if part_data:
                    return base64.urlsafe_b64decode(part_data).decode("utf-8", errors="replace")

    # Fallback: recurse into first part
    if parts:
        return _decode_body(parts[0])

    return ""


def _collect_attachments(payload: dict) -> list[dict]:
    """Collect attachment metadata from a Gmail payload without downloading bytes."""
    if not payload:
        return []

    found: list[dict] = []
    filename = payload.get("filename") or ""
    body = payload.get("body", {}) or {}
    if filename:
        found.append({
            "filename": filename,
            "mime_type": payload.get("mimeType", ""),
            "attachment_id": body.get("attachmentId"),
            "size": body.get("size"),
        })

    for part in payload.get("parts", []) or []:
        found.extend(_collect_attachments(part))

    return found


def _normalize_message(msg: dict) -> dict:
    """Normalize a Gmail API message into a clean dict."""
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "label_ids": msg.get("labelIds", []),
        "snippet": msg.get("snippet"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "body": _decode_body(payload),
        "attachments": _collect_attachments(payload),
    }


class GmailClient:
    def __init__(self, creds: Credentials):
        self._service = _build_service(creds)

    # ── Profile ───────────────────────────────────────────────────────────

    def get_profile(self) -> dict:
        """Return the authenticated Gmail profile."""
        profile = self._service.users().getProfile(userId="me").execute()
        return {
            "email_address": profile.get("emailAddress", ""),
            "messages_total": profile.get("messagesTotal"),
            "threads_total": profile.get("threadsTotal"),
        }

    # ── Send ──────────────────────────────────────────────────────────────

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html: bool = False,
        cc: str = "",
        bcc: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict:
        """Send an email and return the sent message metadata.

        Attachments are accepted as dictionaries with:
        - filename: display filename
        - content_base64: standard or URL-safe base64 payload
        - mime_type: optional MIME type; guessed from filename when omitted
        """
        attachments = attachments or []

        if attachments:
            msg = email.mime.multipart.MIMEMultipart("mixed")
            body_part = email.mime.multipart.MIMEMultipart("alternative")
        else:
            msg = email.mime.multipart.MIMEMultipart("alternative")
            body_part = msg

        msg["to"] = to
        msg["subject"] = subject
        if cc:
            msg["cc"] = cc
        if bcc:
            msg["bcc"] = bcc

        subtype = "html" if html else "plain"
        body_part.attach(email.mime.text.MIMEText(body, subtype, "utf-8"))
        if attachments:
            msg.attach(body_part)

        for attachment in attachments:
            filename = attachment.get("filename") or "attachment"
            content_base64 = attachment.get("content_base64") or ""
            mime_type = attachment.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            try:
                payload = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError):
                payload = base64.urlsafe_b64decode(content_base64 + "=" * (-len(content_base64) % 4))

            maintype, subtype_name = (mime_type.split("/", 1) + ["octet-stream"])[:2]
            part = email.mime.base.MIMEBase(maintype, subtype_name)
            part.set_payload(payload)
            email.encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

        try:
            sent = (
                self._service.users()
                .messages()
                .send(userId="me", body={"raw": raw})
                .execute()
            )
            logger.info("Sent email to %s, messageId=%s", to, sent.get("id"))
            return {"success": True, "message_id": sent.get("id"), "thread_id": sent.get("threadId")}
        except HttpError as exc:
            logger.error("Failed to send email: %s", exc)
            raise

    # ── List / Search ─────────────────────────────────────────────────────

    def list_inbox(self, limit: int = 20, page_token: str | None = None) -> dict:
        """List recent emails from the inbox."""
        params: dict[str, Any] = {"userId": "me", "maxResults": min(limit, 100), "q": "in:inbox"}
        if page_token:
            params["pageToken"] = page_token

        result = self._service.users().messages().list(**params).execute()
        messages = result.get("messages", [])
        next_token = result.get("nextPageToken")

        return {
            "messages": [{"id": m["id"], "thread_id": m["threadId"]} for m in messages],
            "next_page_token": next_token,
            "result_size_estimate": result.get("resultSizeEstimate", 0),
        }

    def search_emails(self, query: str, limit: int = 20) -> dict:
        """Search emails using Gmail query syntax."""
        result = (
            self._service.users()
            .messages()
            .list(userId="me", q=query, maxResults=min(limit, 100))
            .execute()
        )
        messages = result.get("messages", [])
        next_token = result.get("nextPageToken")

        return {
            "messages": [{"id": m["id"], "thread_id": m["threadId"]} for m in messages],
            "next_page_token": next_token,
            "result_size_estimate": result.get("resultSizeEstimate", 0),
        }

    # ── Get ───────────────────────────────────────────────────────────────

    def get_message(self, message_id: str) -> dict:
        """Fetch and decode a single message."""
        msg = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        return _normalize_message(msg)

    def get_thread(self, thread_id: str) -> dict:
        """Fetch all messages in a thread."""
        thread = (
            self._service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
        return {
            "thread_id": thread.get("id"),
            "history_id": thread.get("historyId"),
            "messages": [_normalize_message(m) for m in thread.get("messages", [])],
        }
