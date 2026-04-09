"""
tools/gmail_tools.py
Wraps Gmail API as LangChain @tool functions.

n8n equivalents:
  poll_gmail_inbox()   →  Gmail Trigger node
  send_gmail_reply()   →  Reply node  (step 4)
"""

from __future__ import annotations
import base64
import os
import email.mime.multipart
import email.mime.text
from typing import List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from langchain_core.tools import tool


# ── helpers ──────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",  # needed to mark read
]

TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", "credentials/token.json")


def _get_gmail_service():
    """Load saved OAuth token and return an authenticated Gmail service."""
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_body(payload: dict) -> str:
    """Recursively decode the email body (plain-text preferred)."""
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime_type == "text/plain" and body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    # multipart: recurse into parts
    for part in payload.get("parts", []):
        result = _decode_body(part)
        if result:
            return result
    return ""


# ── tools ────────────────────────────────────────────────────────────────────

@tool
def poll_gmail_inbox(query: str = "is:unread subject:collection request") -> List[dict]:
    """
    Poll Gmail inbox for unread collection-request emails.

    Args:
        query: Gmail search query string (default looks for unread pickup emails).

    Returns:
        List of dicts with keys: thread_id, message_id, sender_email, subject, body.
    """
    service = _get_gmail_service()
    results = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=10,
    ).execute()

    messages_meta = results.get("messages", [])
    emails = []

    for meta in messages_meta:
        msg = service.users().messages().get(
            userId="me", id=meta["id"], format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        body = _decode_body(msg["payload"])

        emails.append({
            "thread_id": msg["threadId"],
            "message_id": msg["id"],
            "sender_email": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "body": body,
        })

        # Mark as read so we don't process it again
        service.users().messages().modify(
            userId="me",
            id=meta["id"],
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()

    return emails


@tool
def send_gmail_reply(thread_id: str, to: str, subject: str, html_body: str) -> str:
    """
    Send an HTML reply email inside an existing Gmail thread.

    Args:
        thread_id:  Gmail thread ID to reply in.
        to:         Recipient email address.
        subject:    Email subject line.
        html_body:  Full HTML content of the reply.

    Returns:
        Confirmation string with sent message ID.
    """
    service = _get_gmail_service()

    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = f"Re: {subject}" if not subject.startswith("Re:") else subject
    msg["In-Reply-To"] = thread_id
    msg["References"] = thread_id

    msg.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    sent = service.users().messages().send(
        userId="me",
        body={"raw": raw, "threadId": thread_id},
    ).execute()

    return f"Reply sent successfully. Message ID: {sent['id']}"
