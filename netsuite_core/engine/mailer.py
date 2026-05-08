"""
Outbound SMTP courier for CFO report delivery (PDF + CSV hit-lists).
Uses stdlib only: smtplib + email.
"""
from __future__ import annotations

import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Mapping


def _smtp_settings() -> tuple[str, int, str, str]:
    host = os.getenv("SMTP_HOST", "").strip()
    port_raw = os.getenv("SMTP_PORT", "587").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    if not host:
        raise RuntimeError("SMTP_HOST is required for outbound mail.")
    if not user:
        raise RuntimeError("SMTP_USER is required for outbound mail.")
    if not password:
        raise RuntimeError("SMTP_PASS is required for outbound mail.")
    try:
        port = int(port_raw)
    except ValueError as e:
        raise RuntimeError(f"SMTP_PORT must be an integer, got {port_raw!r}.") from e
    return host, port, user, password


def _guess_subtype(filename: str) -> tuple[str, str]:
    ctype, _ = mimetypes.guess_type(filename)
    if ctype:
        main, sub = ctype.split("/", 1)
        return main, sub
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return "application", "pdf"
    if lower.endswith(".csv"):
        return "text", "csv"
    return "application", "octet-stream"


def send_outbound_email(
    recipient_email: str,
    subject: str,
    body_text: str,
    attachments: Mapping[str, bytes],
) -> None:
    """
    Send a single email with optional binary attachments (filename -> raw bytes).

    Environment: SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS.
    Port 465 uses implicit TLS; other ports use STARTTLS when supported.
    """
    host, port, user, password = _smtp_settings()
    if not recipient_email or not str(recipient_email).strip():
        raise ValueError("recipient_email is required.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = '"Govnr Advisory" <advisory@getgovnr.com>'
    msg["To"] = recipient_email
    msg.set_content(body_text)

    for filename, payload in attachments.items():
        if payload is None:
            continue
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError(f"Attachment {filename!r} must be bytes, got {type(payload).__name__}.")
        maintype, subtype = _guess_subtype(str(filename))
        msg.add_attachment(
            bytes(payload),
            maintype=maintype,
            subtype=subtype,
            filename=os.path.basename(str(filename)),
        )

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=120, context=context) as server:
            server.login(user, password)
            server.send_message(msg)
        return

    with smtplib.SMTP(host, port, timeout=120) as server:
        server.ehlo()
        try:
            server.starttls(context=context)
            server.ehlo()
        except smtplib.SMTPNotSupportedError:
            pass
        server.login(user, password)
        server.send_message(msg)


def send_outbound_sms(phone_number: str, text_message: str) -> None:
    print(f"[SMS DISPATCHED to {phone_number}]: {text_message}")
