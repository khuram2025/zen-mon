"""SMS text in the GSM-7 alphabet.

One character outside GSM 03.38 (an em dash, a curly quote, "°") makes the
whole message go out as UCS-2: 70 characters per part instead of 160, so a
two-line alert became 2-4 billed parts. Every SMS is passed through
:func:`to_gsm7` at the send point; the default templates are written in GSM-7
already, so stored and previewed text match what is sent.
"""
from __future__ import annotations

import logging
import unicodedata

logger = logging.getLogger("zenplus.sms")

SEGMENT_CHARS = 160

# GSM 03.38 basic character set (each counts 1).
_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# Extension table (escape + char: each counts 2).
_EXTENDED = set("^{}\\[~]|€\f")

# Common typography and symbols -> their plain GSM spelling.
_TRANSLIT = {
    "—": "-", "–": "-", "‒": "-", "‐": "-", "‑": "-", "−": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "«": '"', "»": '"',
    "·": "-", "•": "-", "‧": "-",
    "×": "x", "≥": ">=", "≤": "<=", "≠": "!=",
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>",
    "…": "...",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", "​": "", "﻿": "",
    "°": "", "µ": "u", "©": "(c)", "®": "(R)", "™": "TM",
    "±": "+/-", "\t": " ",
}


def to_gsm7(message: str) -> str:
    """``message`` with every character in the GSM-7 alphabet.

    Typography is transliterated (— – -> "-", curly quotes -> straight,
    · -> "-", × -> "x", ≥ -> ">=", → -> "->", … -> "...", nbsp -> space); an
    accented letter outside GSM becomes its base letter; anything else is
    dropped. Idempotent.
    """
    out: list[str] = []
    for ch in str(message or ""):
        if ch in _BASIC or ch in _EXTENDED:
            out.append(ch)
            continue
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
            continue
        # "ç" -> "c"; a symbol with no GSM spelling is dropped.
        base = "".join(c for c in unicodedata.normalize("NFKD", ch)
                       if not unicodedata.combining(c))
        kept = "".join(c for c in base if c in _BASIC or c in _EXTENDED)
        if not kept and unicodedata.category(ch).startswith(("L", "N")):
            # A letter of another script (an Arabic site name, a user's own
            # template) has no ASCII spelling: dropping it would lose the
            # text, so it stays and that message goes out as UCS-2.
            kept = ch
        out.append(kept)
    return "".join(out)


def gsm7_length(message: str) -> int:
    """Septets the message needs (extension characters count twice)."""
    return sum(2 if ch in _EXTENDED else 1 for ch in str(message or ""))


def warn_if_long(message: str) -> None:
    """Log (never truncate) an SMS that needs more than one segment."""
    n = gsm7_length(message)
    if n > SEGMENT_CHARS:
        logger.warning("SMS is %d GSM-7 characters (more than one %d-character segment)",
                       n, SEGMENT_CHARS)
