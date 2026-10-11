"""
language.py — Lightweight language detection for technician messages.
Servix answers in Dutch by default and switches to English when a message is clearly English.
"""

import re

_ENGLISH_MARKERS = {"the", "what", "how", "does", "is", "my", "should", "check", "error", "fault", "shows", "and", "it", "mean"}
_DUTCH_MARKERS = {"de", "het", "wat", "hoe", "is", "mijn", "moet", "ik", "geeft", "een", "storing", "en", "betekent", "controleren"}


def detect_language(text: str) -> str:
    """Return "en" when a message is clearly English, else "nl" (the default)."""
    words = set(re.findall(r"[a-zà-ÿ]+", text.lower()))
    return "en" if len(words & _ENGLISH_MARKERS) > len(words & _DUTCH_MARKERS) else "nl"
