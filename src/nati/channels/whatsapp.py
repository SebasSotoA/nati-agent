"""WhatsApp Cloud API adapter: verify, allowlist, send.

The agent loop is unchanged. This module turns a Meta webhook payload into
plain text and sends the reply back as one or more text messages.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from typing import Any

GRAPH_VERSION = "v21.0"
WA_LIMIT = 4000


def admin_phone() -> str:
    return _digits(os.environ.get("NATI_ADMIN_PHONE", ""))


def verify_token() -> str:
    return os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip()


def app_secret() -> str:
    return os.environ.get("WHATSAPP_APP_SECRET", "").strip()


def access_token() -> str:
    return os.environ.get("WHATSAPP_TOKEN", "").strip()


def phone_number_id() -> str:
    return os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()


def configured() -> bool:
    phone = canonicalize_phone(os.environ.get("NATI_ADMIN_PHONE", ""))
    return bool(
        len(phone) >= 12
        and verify_token() and app_secret()
        and access_token() and phone_number_id()
    )


def _digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def canonicalize_phone(value: str) -> str:
    """Colombia: 10-digit national → 57XXXXXXXXXX. Otherwise digits as-is."""
    d = _digits(value)
    if len(d) == 10:
        return "57" + d
    return d


def same_phone(incoming: str, allowed: str) -> bool:
    """Exact match after canonicalizing. No suffix/prefix tricks.

    WhatsApp `from` is E.164 without '+'. A 10-digit NATI_ADMIN_PHONE is
    treated as Colombian (+57). A 12-digit 57… form matches that. A different
    country prefix with the same last 10 digits does not.
    """
    a = canonicalize_phone(incoming)
    b = canonicalize_phone(allowed)
    if len(a) < 12 or len(b) < 12:
        return False
    return a == b


def signature_ok(body: bytes, header: str, secret: str) -> bool:
    """True when X-Hub-Signature-256 matches HMAC-SHA256(body, app secret)."""
    if not secret or not header.startswith("sha256="):
        return False
    given = header[len("sha256="):].strip()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(given, expected)


def extract_text(payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """Returns (from_phone, text, message_id) for the first inbound text."""
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            messages = value.get("messages") or []
            if not messages:
                continue
            msg = messages[0]
            if not isinstance(msg, dict):
                continue
            if msg.get("type") != "text":
                continue
            text = ((msg.get("text") or {}) if isinstance(msg.get("text"), dict) else {})
            body = str(text.get("body") or "").strip()
            sender = str(msg.get("from") or "").strip()
            msg_id = str(msg.get("id") or "").strip()
            if body and sender:
                return sender, body, msg_id
    return None


def split_reply(text: str, limit: int = WA_LIMIT) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while rest:
        parts.append(rest[:limit])
        rest = rest[limit:]
    return parts


def send_text(to: str, body: str) -> None:
    """POST a text message to Graph. Failures are logged by the caller."""
    url = (
        f"https://graph.facebook.com/{GRAPH_VERSION}/"
        f"{phone_number_id()}/messages"
    )
    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {access_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def send_reply(to: str, body: str) -> None:
    last_error: Exception | None = None
    for chunk in split_reply(body):
        try:
            send_text(to, chunk)
            last_error = None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error


def compose_outbound(reply: str, steps: list[dict[str, Any]]) -> str:
    """WhatsApp body: model reply plus any confirmation summaries she must see."""
    parts: list[str] = []
    text = (reply or "").strip()
    if text:
        parts.append(text)
    for step in steps:
        summary = (step.get("summary") or "").strip()
        if summary:
            parts.append(summary)
    return "\n\n".join(parts)
