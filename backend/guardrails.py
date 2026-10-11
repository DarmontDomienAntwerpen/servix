"""
guardrails.py — Output validation for Servix AI responses.
Checks every response before sending: source citation, safety warnings, length, and web source disclaimer.
"""

import re
import logging

logger = logging.getLogger(__name__)

MAX_RESPONSE_LENGTH = 1500

DANGEROUS_KEYWORDS = [
    "gasleiding", "gasslang", "gasmeter", "gasafsluiter",
    "230v", "400v", "condensator",
    "brandgevaar", "ontploffing", "koolmonoxide",
    "aardlek",
    # English equivalents (Servix answers in the technician's language)
    "gas line", "gas pipe", "gas valve", "high voltage", "capacitor",
    "fire hazard", "explosion", "carbon monoxide",
]

SAFETY_WARNING = (
    "\n⚡ *VEILIGHEID:* Schakel het toestel uit en sluit gas/stroom af "
    "vóór u aan deze onderdelen werkt. Bij twijfel: raadpleeg een erkend technicus."
)

WEB_DISCLAIMER = (
    "\n\n⚠️ *Let op:* Deze informatie komt van het internet en is mogelijk "
    "niet volledig of up-to-date. Raadpleeg altijd de officiële handleiding."
)


# ── Checks ────────────────────────────────────────────────────────────────────

def _has_source_citation(text: str) -> bool:
    """Return True if the response contains a source reference."""
    return bool(re.search(r"(📖\s*(BRON|SOURCE)|bron:|source:|handleiding|manual|pagina\s*\d+|page\s*\d+|p\.\s*\d+)", text, re.IGNORECASE))


def _needs_safety_warning(text: str) -> bool:
    """Return True if the response mentions dangerous actions without a safety warning."""
    has_danger = any(kw in text.lower() for kw in DANGEROUS_KEYWORDS)
    has_warning = bool(re.search(r"(⚡|veiligheid|waarschuwing|voorzichtig|schakel uit|safety|warning|caution)", text, re.IGNORECASE))
    return has_danger and not has_warning


def _is_too_long(text: str) -> bool:
    return len(text) > MAX_RESPONSE_LENGTH


def _compress(text: str) -> str:
    """Truncate response and add a note that it was shortened."""
    return text[:MAX_RESPONSE_LENGTH] + "\n\n_(Antwoord ingekort. Stel een specifiekere vraag voor meer details.)_"


# ── Public API ────────────────────────────────────────────────────────────────

def validate_response(text: str, from_web: bool = False, requires_source: bool = True) -> str:
    """
    Validate and patch an AI response before sending to the technician.

    Checks performed:
    1. Source citation present (if technical advice)
    2. Safety warning added for dangerous procedures
    3. Web disclaimer added if info came from internet
    4. Response compressed if too long

    Args:
        text: The raw AI response text
        from_web: True if the response is based on web search results
        requires_source: False for conversational replies (greetings, follow-up questions)

    Returns:
        Validated and patched response text, or a blocked message if unfixable
    """
    if not text or not text.strip():
        return "Ik kon geen antwoord genereren. Probeer de vraag opnieuw te formuleren."

    # Add safety warning if missing
    if _needs_safety_warning(text):
        logger.info("Guardrail: adding missing safety warning.")
        text = text + SAFETY_WARNING

    # Add web disclaimer
    if from_web:
        text = text + WEB_DISCLAIMER

    # Block technical answers without source (unless conversational)
    if requires_source and not from_web and not _has_source_citation(text):
        logger.warning("Guardrail: blocked response — no source citation.")
        return (
            "⚠️ Ik heb onvoldoende betrouwbare informatie gevonden in de handleidingen "
            "om hierop te antwoorden.\n\n"
            "Wil je dat ik het internet afzoek op officiële fabrikantssites? "
            "Merk op dat die informatie mogelijk niet volledig of up-to-date is."
        )

    # Compress if too long
    if _is_too_long(text):
        logger.info("Guardrail: compressing long response.")
        text = _compress(text)

    return text


def is_conversational(text: str) -> bool:
    """
    Detect whether a message is a simple conversational reply vs technical advice.

    Technical content always requires a source, regardless of length.
    Only short greetings/confirmations without technical terms are conversational.
    """
    lower = text.lower()

    # Any technical content → never conversational
    if re.search(
        r"(foutcode|diagnose|stappen|vervang|controleer|waterdruk|temperatuur|"
        r"gasdruk|spanning|reset|ketel|pomp|klep|sensor|storing|f\.\d|u\d|e\.\d)",
        lower,
    ):
        return False

    greeting_markers = [
        "goedemorgen", "goedemiddag", "goedenavond", "hallo", "dag ",
        "begrepen", "duidelijk", "bedankt", "geen probleem", "ok ", "prima",
    ]
    return len(text) < 150 and any(m in lower for m in greeting_markers)
