"""Colour helpers shared by the spreadsheet, email and legacy HTML outputs.

The organisation's brand colour is free-form (Settings → General → Company),
so any text drawn on it, and any brand-coloured text drawn on white, must be
picked for contrast rather than assumed: a pale brand colour gets dark text
on top of it and is darkened before it is used as a text colour.
"""

from __future__ import annotations

from typing import Optional

from app.services.branding import DEFAULT_BRAND_COLOR, valid_hex_color

WHITE = "#FFFFFF"
DARK_TEXT = "#111827"

# Accent colours for text on white/tinted backgrounds. The section accents
# (report_sections.ACCENT_HEX) are tuned for fills and fail 4.5:1 as text.
ACCENT_INK = {
    "success": "#15803D",
    "warning": "#B45309",
    "danger": "#B91C1C",
    "info": "#0E7490",
}

# Classification banner backgrounds (white text on each passes 4.5:1).
CLASSIFICATION_BG = {
    "public": "#15803D",
    "internal": "#1D4ED8",
    "confidential": "#B45309",
    "restricted": "#B91C1C",
}


def normalize(color: Optional[str], default: str = DEFAULT_BRAND_COLOR) -> str:
    """'#RRGGBB' upper-case, or ``default`` for anything else."""
    return color.upper() if isinstance(color, str) and valid_hex_color(color) else default


def _rgb(color: str) -> tuple[int, int, int]:
    c = normalize(color)
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def luminance(color: str) -> float:
    """WCAG relative luminance."""
    def lin(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(color)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def text_on(background: str) -> str:
    """White or near-black, whichever reads better on ``background``."""
    bg = normalize(background)
    return WHITE if contrast(WHITE, bg) >= contrast(DARK_TEXT, bg) else DARK_TEXT


def ink(color: Optional[str], min_ratio: float = 4.5, background: str = WHITE) -> str:
    """``color`` darkened just enough to be readable as text on ``background``."""
    c = normalize(color)
    r, g, b = _rgb(c)
    for step in range(0, 21):
        f = 1 - step * 0.05
        cand = "#{:02X}{:02X}{:02X}".format(round(r * f), round(g * f), round(b * f))
        if contrast(cand, background) >= min_ratio:
            return cand
    return DARK_TEXT


def accent_ink(accent: Optional[str], brand: Optional[str] = None) -> str:
    """Text colour for a KPI accent ('primary' follows the brand colour)."""
    if accent in ACCENT_INK:
        return ACCENT_INK[accent]
    return ink(brand)


def classification_bg(level: Optional[str]) -> str:
    return CLASSIFICATION_BG.get((level or "").strip().lower(), "#374151")
