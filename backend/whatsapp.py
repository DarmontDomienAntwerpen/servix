"""
whatsapp.py — Twilio WhatsApp API integration for Servix.
Handles sending messages, downloading media, verifying webhook signatures,
and parsing incoming webhook payloads.
"""

import os
import logging
import httpx
from typing import Optional

from twilio.rest import Client
from twilio.request_validator import RequestValidator

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "")  # e.g. "whatsapp:+14155238886"

# Without Twilio credentials the WhatsApp channel is disabled, but the rest of
# the app (web chat, uploads, RAG) still starts.
_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None
_validator = RequestValidator(TWILIO_AUTH_TOKEN)


# ── Sending ───────────────────────────────────────────────────────────────────

async def send_message(to: str, text: str) -> None:
    """
    Send a WhatsApp text message via Twilio.

    Args:
        to: Recipient phone number, with or without the 'whatsapp:' prefix
        text: Message body to send
    """
    if _client is None:
        raise RuntimeError("WhatsApp is disabled: set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env")
    to_number = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
    try:
        _client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=to_number, body=text)
    except Exception as e:
        logger.error(f"WhatsApp send failed for {to}: {e}")
        raise


# ── Media download ────────────────────────────────────────────────────────────

async def download_media(media_url: str) -> bytes:
    """
    Download a media file (image or audio) from a Twilio media URL.

    Args:
        media_url: The MediaUrl0 value from the incoming webhook payload

    Returns:
        Raw bytes of the media file
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        response.raise_for_status()
        return response.content


# ── Verification ──────────────────────────────────────────────────────────────

def validate_signature(url: str, params: dict, signature: str) -> bool:
    """
    Verify that an incoming webhook request actually came from Twilio.

    Args:
        url: The full public URL Twilio sent the request to
        params: The form-encoded POST params from the request
        signature: Value of the X-Twilio-Signature header

    Returns:
        True if the signature is valid
    """
    return _validator.validate(url, params, signature)


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_incoming_message(form: dict) -> Optional[dict]:
    """
    Extract the relevant fields from a Twilio WhatsApp webhook payload.

    Args:
        form: The parsed form-encoded body from the POST /webhook request

    Returns:
        Dict with keys: from, type, text, media_url — or None if not a user message
    """
    sender = form.get("From")
    if not sender:
        return None

    sender = sender.replace("whatsapp:", "")
    num_media = int(form.get("NumMedia", "0") or "0")
    body = form.get("Body", "")

    if num_media > 0:
        content_type = form.get("MediaContentType0", "")
        if content_type.startswith("image/"):
            msg_type = "image"
        elif content_type.startswith("audio/"):
            msg_type = "audio"
        else:
            msg_type = "unknown"
        return {"from": sender, "type": msg_type, "text": body, "media_url": form.get("MediaUrl0")}

    return {"from": sender, "type": "text", "text": body, "media_url": None}
