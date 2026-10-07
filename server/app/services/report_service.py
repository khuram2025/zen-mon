"""
ZenPlus Network Monitoring - PDF Report Generator

Enterprise document engine for every PDF the appliance produces (on-demand
downloads and scheduled email attachments, both the legacy report types and
the section-based engine in ``report_sections.py``).

Design system
-------------
- Embedded Liberation Sans (+ DejaVu Sans Mono for addresses/identifiers) with
  full Unicode; graceful fallback to core Helvetica when the system fonts are
  missing.
- A dark branded cover page, optional table of contents with PDF outline
  bookmarks, slim running header/footer, numbered section headings.
- Style-aware tables (right-aligned numerics, status pills, inline percentage
  bars, repeated headers across page breaks, content-fit column widths).
- Print-grade matplotlib figures rendered at their exact physical size.
"""

import io
import warnings
import logging
import glob
import re
import statistics
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from math import ceil

from fpdf import FPDF
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import matplotlib.font_manager as _fm
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_ch_client


# ---------------------------------------------------------------------------
# Fonts \u2014 embed real TTFs so reports carry proper typography and Unicode
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = {
    "sans": {
        "": ["/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
        "B": ["/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
        "I": ["/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf"],
        "BI": ["/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf"],
    },
    "mono": {
        "": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
        "B": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"],
    },
}


def _find_font(paths: list[str]) -> str | None:
    for p in paths:
        if glob.os.path.exists(p):
            return p
    return None


_SANS_FILES = {st: _find_font(ps) for st, ps in _FONT_CANDIDATES["sans"].items()}
_MONO_FILES = {st: _find_font(ps) for st, ps in _FONT_CANDIDATES["mono"].items()}
UNICODE_FONTS = all(_SANS_FILES.values())

# Register the same faces with matplotlib so figures match the document.
for _path in [*_SANS_FILES.values(), *_MONO_FILES.values()]:
    if _path:
        try:
            _fm.fontManager.addfont(_path)
        except Exception:
            pass


class _OncePerMessage(logging.Filter):
    """An SVG logo is drawn on every page of every report; fpdf2 repeats its
    'unsupported SVG element' warnings each time. Log each one once."""

    def __init__(self):
        super().__init__()
        self._seen: set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            key = record.getMessage()
        except Exception:
            return True
        if key in self._seen:
            return False
        if len(self._seen) < 256:
            self._seen.add(key)
        return True


logging.getLogger("fpdf.svg").addFilter(_OncePerMessage())


def _safe(text) -> str:
    """Sanitize text for PDF output.

    With embedded Unicode fonts only control characters are stripped; on the
    Helvetica fallback, common typographic characters are folded to ASCII.
    """
    if text is None:
        return ""
    if isinstance(text, (bytes, bytearray)):
        s = text.decode("utf-8", errors="replace")
    else:
        s = str(text)
    s = s.replace("\r", "").replace("\t", "  ").replace("\n", " ")
    if UNICODE_FONTS:
        return "".join(ch for ch in s if ch >= " " or ch == "\n")
    s = s.replace("\u2014", "-").replace("\u2013", "-")
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2026", "...").replace("\u2022", "-").replace("\u00b7", "-")
    return s.encode("latin-1", errors="replace").decode("latin-1")


# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------

# Ink (text) scale
COLOR_TEXT = (15, 23, 42)            # #0F172A headings / primary ink
COLOR_BODY = (51, 65, 85)            # #334155 body copy
COLOR_MUTED = (100, 116, 139)        # #64748B secondary
COLOR_FAINT = (148, 163, 184)        # #94A3B8 tertiary / footer
COLOR_HAIRLINE = (226, 232, 240)     # #E2E8F0 rules
COLOR_HAIRLINE_SOFT = (235, 240, 246)  # #EBF0F6 row separators
COLOR_BG_TINT = (247, 249, 252)      # #F7F9FC panels
COLOR_WHITE = (255, 255, 255)

# Brand
COLOR_PRIMARY = (79, 107, 246)       # #4F6BF6 ZenPlus indigo
COLOR_NAVY = (16, 25, 51)            # #101933 cover ground
COLOR_NAVY_PANEL = (28, 40, 74)      # #1C284A cover panels/rules
COLOR_NAVY_TEXT = (166, 178, 214)    # #A6B2D6 cover secondary text
COLOR_NAVY_LABEL = (122, 136, 176)   # #7A88B0 cover labels

# Status (print-legible steps; pills pair them with tinted grounds)
COLOR_SUCCESS = (22, 128, 61)        # #16803D
COLOR_DANGER = (185, 28, 28)         # #B91C1C
COLOR_WARNING = (180, 83, 9)         # #B45309
COLOR_DEGRADED = (161, 98, 7)        # #A16207
COLOR_INFO = (7, 89, 133)            # #075985

TINT_SUCCESS = (232, 246, 238)
TINT_DANGER = (253, 236, 236)
TINT_WARNING = (254, 243, 226)
TINT_INFO = (232, 244, 251)
TINT_NEUTRAL = (238, 242, 247)

# Hex versions for matplotlib marks (slightly brighter than text steps)
HEX_PRIMARY = "#4F6BF6"
HEX_SUCCESS = "#16A34A"
HEX_DANGER = "#DC2626"
HEX_WARNING = "#D97706"
HEX_DEGRADED = "#CA8A04"
HEX_INFO = "#0891B2"
HEX_TEXT = "#0F172A"
HEX_MUTED = "#64748B"
HEX_FAINT = "#94A3B8"
HEX_GRID = "#E9EDF4"
HEX_AXIS = "#C7D0DD"
HEX_BG_TINT = "#F7F9FC"

# Validated categorical palette for identity encodings (donut slices etc.);
# adjacent-pair CVD-checked on white. "Other" folds into neutral gray.
CATEGORICAL_HEX = ["#2A78D6", "#EB6834", "#1BAF7A", "#EDA100", "#E87BA4", "#4A3AA7"]
OTHER_HEX = "#94A3B8"

STATUS_COLORS_HEX = {
    "up": HEX_SUCCESS,
    "online": HEX_SUCCESS,
    "healthy": HEX_SUCCESS,
    "down": HEX_DANGER,
    "offline": HEX_DANGER,
    "degraded": HEX_DEGRADED,
    "warning": HEX_WARNING,
    "critical": HEX_DANGER,
    "info": HEX_PRIMARY,
    "active": HEX_DANGER,
    "acknowledged": HEX_WARNING,
    "resolved": HEX_SUCCESS,
    "unknown": HEX_FAINT,
}

STATUS_COLORS_RGB = {
    "up": COLOR_SUCCESS,
    "online": COLOR_SUCCESS,
    "down": COLOR_DANGER,
    "offline": COLOR_DANGER,
    "degraded": COLOR_DEGRADED,
    "warning": COLOR_WARNING,
    "critical": COLOR_DANGER,
    "info": COLOR_PRIMARY,
    "unknown": COLOR_MUTED,
}

# Pill semantics: text value -> (text_rgb, tint_rgb)
_PILL_KINDS = {
    "success": (COLOR_SUCCESS, TINT_SUCCESS),
    "danger": (COLOR_DANGER, TINT_DANGER),
    "warning": (COLOR_WARNING, TINT_WARNING),
    "info": (COLOR_INFO, TINT_INFO),
    "neutral": (COLOR_MUTED, TINT_NEUTRAL),
}

_PILL_VOCAB = {
    "up": "success", "online": "success", "healthy": "success", "ok": "success",
    "success": "success", "resolved": "success", "on track": "success",
    "passing": "success", "connected": "success",
    "down": "danger", "offline": "danger", "critical": "danger",
    "breaching": "danger", "expired": "danger", "failed": "danger",
    "error": "danger", "failing": "danger",
    "degraded": "warning", "warning": "warning", "at risk": "warning",
    "acknowledged": "warning", "stale": "warning", "paused": "warning",
    "active": "info", "info": "info", "maintenance": "info", "pending": "info",
    "unknown": "neutral", "disabled": "neutral", "none": "neutral",
    # severities and verdicts used by section packs
    "major": "danger", "high": "danger", "poor": "danger", "non-compliant": "danger",
    "medium": "warning", "minor": "warning", "needs work": "warning",
    "needs improvement": "warning",
    "low": "info", "good": "success", "compliant": "success", "validated": "success",
    "not measured": "neutral", "no data": "neutral", "n/a": "neutral",
}

# Classification labels (Settings → Company → confidentiality): pill colours
# that clear 4.5:1 with white text on light and dark grounds alike.
CLASSIFICATION_RGB = {
    "public": (21, 128, 61),
    "internal": (180, 83, 9),
    "confidential": (194, 65, 12),
    "restricted": (185, 28, 28),
}

# Page geometry. PAGE_W/PAGE_H/CONTENT_W are the A4 defaults kept for chart
# sizing and older callers; documents use ZenPlusReport.page_w / page_h /
# body_w, which follow the configured paper size (A4 or Letter).
PAGE_W = 210
PAGE_H = 297
MARGIN_L = 16
MARGIN_R = 16
MARGIN_T = 24
MARGIN_B = 18
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R  # 178mm
PAPER_FORMATS = {"A4": "A4", "LETTER": "Letter"}


def paper_format(paper_size: str | None) -> str:
    """'A4' or 'Letter' (anything else falls back to A4)."""
    return PAPER_FORMATS.get(str(paper_size or "").strip().upper(), "A4")


# ---------------------------------------------------------------------------
# Brand colour helpers (WCAG relative luminance / contrast)
# ---------------------------------------------------------------------------

def _lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb) -> float:
    r, g, b = (_lin(float(v)) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mix_rgb(a, b, t: float) -> tuple[int, int, int]:
    """Blend a → b by t (0 = a, 1 = b)."""
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def rgb_to_hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(int(max(0, min(255, v))) for v in rgb))


def _valid_rgb(rgb) -> tuple[int, int, int]:
    try:
        r, g, b = (int(v) for v in rgb)
        if all(0 <= v <= 255 for v in (r, g, b)):
            return r, g, b
    except (TypeError, ValueError):
        pass
    return COLOR_PRIMARY


def accent_on_white(rgb, minimum: float = 3.0) -> tuple[int, int, int]:
    """The brand colour, darkened just enough to reach ``minimum`` contrast on
    white so rules, numbers and icons that carry meaning stay visible
    (WCAG 1.4.11 non-text contrast)."""
    rgb = _valid_rgb(rgb)
    white = (255, 255, 255)
    if contrast_ratio(rgb, white) >= minimum:
        return rgb
    for i in range(1, 41):
        cand = mix_rgb(rgb, (0, 0, 0), i * 0.025)
        if contrast_ratio(cand, white) >= minimum:
            return cand
    return COLOR_TEXT


def text_on(rgb) -> tuple[int, int, int]:
    """White or ink, whichever reads better on ``rgb`` (white when it clears 4.5:1)."""
    rgb = _valid_rgb(rgb)
    if contrast_ratio(rgb, (255, 255, 255)) >= 4.5:
        return (255, 255, 255)
    return (255, 255, 255) if contrast_ratio(rgb, (255, 255, 255)) > contrast_ratio(rgb, COLOR_TEXT) \
        else COLOR_TEXT


def legible_on(fg, bg, minimum: float = 4.5) -> tuple[int, int, int]:
    """``fg`` lightened (on a dark ground) or darkened (on a light one) until it
    reaches ``minimum`` contrast against ``bg``."""
    fg, bg = _valid_rgb(fg), _valid_rgb(bg)
    target = (255, 255, 255) if relative_luminance(bg) < 0.4 else (0, 0, 0)
    for i in range(0, 41):
        cand = mix_rgb(fg, target, i * 0.025)
        if contrast_ratio(cand, bg) >= minimum:
            return cand
    return target


def brand_tokens(rgb) -> dict:
    """Every colour a branded document derives from the organisation's brand
    colour: the accent (≥3:1 on white), readable text on the brand, the dark
    cover ground tinted with the brand and its secondary inks."""
    brand = _valid_rgb(rgb)
    accent = accent_on_white(brand)
    ground = mix_rgb(brand, (10, 14, 28), 0.80)
    return {
        "brand": brand,
        "accent": accent,
        "on_brand": text_on(brand),
        "on_accent": text_on(accent),
        "tint": mix_rgb(accent, (255, 255, 255), 0.92),
        "tint_strong": mix_rgb(accent, (255, 255, 255), 0.84),
        "ground": ground,
        "ground_panel": mix_rgb(ground, (255, 255, 255), 0.10),
        "ground_motif": mix_rgb(ground, brand, 0.30),
        "ground_label": legible_on(mix_rgb(ground, (255, 255, 255), 0.45), ground, 4.5),
        "ground_text": legible_on(mix_rgb(ground, (255, 255, 255), 0.70), ground, 7.0),
        "ground_eyebrow": legible_on(mix_rgb(brand, (255, 255, 255), 0.35), ground, 4.5),
        "ground_highlight": legible_on(brand, ground, 3.0),
    }


def classification_rgb(label: str | None) -> tuple[int, int, int]:
    return CLASSIFICATION_RGB.get(str(label or "").strip().lower(), COLOR_MUTED)


# Header names that force a column style regardless of cell contents
_HDR_STYLE_HINTS = {
    "status": "status", "health": "status", "state": "status",
    "severity": "severity",
    "ip": "mono", "ip address": "mono",
}

_NUMISH_RE = re.compile(
    r"^\s*[<>≥≤~]?\s*-?[\d.,]+\s*"
    r"(%|ms|s|m|h|d|min|bps|kbps|mbps|gbps|b|kb|mb|gb|tb|pb|x)?\s*$",
    re.IGNORECASE,
)
_NEUTRAL_CELL = {"", "-", "—", "–", "n/a", "none"}
COLUMN_STYLES = ("text", "num", "mono", "pct", "pct-bar", "status", "severity")


def infer_column_styles(headers: list, rows: list, col_styles: list | None) -> list[str]:
    """Column styles for a table: the declared style, else a header hint
    (status/severity/IP), else 'num' when most cells look numeric."""
    n = len(headers)
    declared = list(col_styles or [])
    out = []
    for ci in range(n):
        s = declared[ci] if ci < len(declared) else None
        if s in COLUMN_STYLES:
            out.append(s)
            continue
        hint = _HDR_STYLE_HINTS.get(str(headers[ci]).strip().lower())
        if hint:
            out.append(hint)
            continue
        vals = ["" if ci >= len(r) or r[ci] is None else str(r[ci]).strip() for r in rows[:80]]
        vals = [v for v in vals if v.lower() not in _NEUTRAL_CELL]
        if vals and sum(1 for v in vals if _NUMISH_RE.match(v)) >= 0.7 * len(vals):
            out.append("num")
        else:
            out.append("text")
    return out


def pill_kind(value) -> str:
    """success | danger | warning | info | neutral for a status/severity word."""
    return _PILL_VOCAB.get(str(value or "").strip().lower(), "neutral")


# ---------------------------------------------------------------------------
# Helpers - time range
# ---------------------------------------------------------------------------

def _resolve_period(
    period: str,
    from_time: datetime | None,
    to_time: datetime | None,
) -> tuple[datetime, datetime, str]:
    """Return (start, end, human-readable label)."""
    now = datetime.now(timezone.utc)
    if period == "custom" and from_time and to_time:
        # Ensure tz-aware; if user picked a date the time is 00:00 so
        # extend to_time to end-of-day (23:59:59) to include the full day.
        start = from_time.replace(tzinfo=timezone.utc) if from_time.tzinfo is None else from_time
        end = to_time.replace(tzinfo=timezone.utc) if to_time.tzinfo is None else to_time
        # If to_time has no meaningful time component (midnight), extend to end of day
        if end.hour == 0 and end.minute == 0 and end.second == 0:
            end = end.replace(hour=23, minute=59, second=59)
        label = f"{start:%Y-%m-%d} - {end:%Y-%m-%d} UTC"
        return start, end, label
    mapping = {
        "last_24h": (timedelta(hours=24), "Last 24 Hours"),
        "last_7d": (timedelta(days=7), "Last 7 Days"),
        "last_30d": (timedelta(days=30), "Last 30 Days"),
        "last_90d": (timedelta(days=90), "Last 90 Days"),
    }
    delta, label = mapping.get(period, (timedelta(hours=24), "Last 24 Hours"))
    return now - delta, now, label


def _fmt_ms(val: float | None) -> str:
    if val is None:
        return "—" if UNICODE_FONTS else "-"
    return f"{val:.1f} ms"


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "—" if UNICODE_FONTS else "-"
    return f"{val:.1f}%"


def _fmt_duration(seconds: float | None) -> str:
    """Compact two-unit duration: 42s, 12m 30s, 5h 54m, 2d 7h."""
    if seconds is None or seconds <= 0:
        return "—" if UNICODE_FONTS else "-"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, rs = divmod(s, 60)
        return f"{m}m {rs}s" if rs else f"{m}m"
    if s < 86400:
        h, rm = divmod(s // 60, 60)
        return f"{h}h {rm}m" if rm else f"{h}h"
    d, rh = divmod(s // 3600, 24)
    return f"{d}d {rh}h" if rh else f"{d}d"


# ---------------------------------------------------------------------------
# Matplotlib chart helpers — print-grade figures rendered at exact size
# ---------------------------------------------------------------------------

_CHART_DPI = 200

_MPL_RC = {
    "font.family": ["Liberation Sans", "DejaVu Sans", "sans-serif"],
    "font.size": 8.0,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": HEX_AXIS,
    "axes.linewidth": 0.7,
    "axes.grid": True,
    "axes.grid.axis": "y",
    "grid.color": HEX_GRID,
    "grid.linewidth": 0.7,
    "grid.linestyle": "-",
    "axes.axisbelow": True,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.spines.left": False,
    "axes.spines.bottom": True,
    "xtick.color": HEX_MUTED,
    "ytick.color": HEX_MUTED,
    "xtick.labelsize": 7.0,
    "ytick.labelsize": 7.0,
    "xtick.major.size": 0,
    "ytick.major.size": 0,
    "xtick.major.pad": 3,
    "ytick.major.pad": 2,
    "axes.labelcolor": HEX_MUTED,
    "axes.labelsize": 7.2,
    "legend.frameon": False,
    "legend.fontsize": 7.2,
    "text.color": HEX_TEXT,
}


# matplotlib's rcParams are process-global: every chart (these helpers and
# report_charts.chart_png) renders under this lock so concurrent renders in
# worker threads never see each other's style.
MPL_LOCK = threading.RLock()


@contextmanager
def _figure(w_mm: float, h_mm: float, layout: str | None = "constrained"):
    """A figure of exact physical size drawn with the document style. It is
    built with the object API (no pyplot registry), so nothing is retained
    after rendering, even when drawing fails half-way."""
    with MPL_LOCK, matplotlib.rc_context(_MPL_RC):
        fig = Figure(figsize=(w_mm / 25.4, h_mm / 25.4), dpi=_CHART_DPI, layout=layout)
        FigureCanvasAgg(fig)
        try:
            yield fig
        finally:
            release_figure(fig)


def release_figure(fig: Figure) -> None:
    """Free a rendered figure now: its artists and the Agg pixel buffer
    (several MB at 200 dpi), which would otherwise wait for the cyclic
    garbage collector (figure <-> canvas)."""
    try:
        fig.clear()
        canvas = fig.canvas
        if getattr(canvas, "renderer", None) is not None:
            canvas.renderer = None
            canvas._lastKey = None
    except Exception:
        pass


def _finish(fig: Figure) -> bytes:
    """Serialize at fixed DPI without bbox cropping so physical size is exact."""
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="AutoDateLocator was unable", category=UserWarning)
        fig.savefig(buf, format="png", dpi=_CHART_DPI, facecolor="white", edgecolor="none")
    return buf.getvalue()


def _empty_chart(w_mm: float = CONTENT_W, h_mm: float = 34,
                 label: str = "No data available for this period") -> bytes:
    """A quiet tinted panel used wherever a figure has nothing to show."""
    with _figure(w_mm, h_mm, layout=None) as fig:
        ax = fig.add_axes([0, 0, 1, 1])
        ax.axis("off")
        ax.add_patch(Rectangle((0.004, 0.03), 0.992, 0.94, transform=ax.transAxes,
                               facecolor=HEX_BG_TINT, edgecolor=HEX_GRID,
                               linewidth=0.8, zorder=0))
        ax.text(0.5, 0.5, label, ha="center", va="center", fontsize=7.6,
                color=HEX_MUTED, transform=ax.transAxes)
        return _finish(fig)


def _fmt_axis_val(v, _pos=None) -> str:
    if v is None:
        return ""
    av = abs(v)
    if av >= 1e9:
        return f"{v / 1e9:g}B"
    if av >= 1e6:
        return f"{v / 1e6:g}M"
    if av >= 1e4:
        return f"{v / 1e3:g}k"
    if av >= 100 or float(v).is_integer():
        return f"{v:,.0f}"
    return f"{v:g}"


def _downsample(timestamps: list, values: list, target: int = 560):
    """Bucketed mean + min/max band for large series. Returns
    (ts, mean, band_min, band_max, reduced?)."""
    n = len(values)
    if n <= target:
        return timestamps, values, None, None, False
    step = ceil(n / target)
    ts_out, mean_out, min_out, max_out = [], [], [], []
    for i in range(0, n, step):
        chunk = values[i:i + step]
        ts_out.append(timestamps[min(i + step // 2, n - 1)])
        mean_out.append(sum(chunk) / len(chunk))
        min_out.append(min(chunk))
        max_out.append(max(chunk))
    return ts_out, mean_out, min_out, max_out, True


def _style_time_axis(ax, narrow: bool = False, tz=None):
    """Concise date ticks; ``tz`` (a tzinfo) draws them in that zone (UTC if None)."""
    locator = mdates.AutoDateLocator(maxticks=5 if narrow else 7, tz=tz)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=tz))
    with warnings.catch_warnings():
        # A single-bucket range makes AutoDateLocator fall back to its default.
        warnings.filterwarnings("ignore", message="AutoDateLocator was unable", category=UserWarning)
        labels = ax.get_xticklabels()
    for lbl in labels:
        lbl.set_rotation(0)
        lbl.set_ha("center")
    try:
        ax.xaxis.get_offset_text().set_size(6.6)
        ax.xaxis.get_offset_text().set_color(HEX_MUTED)
    except Exception:
        pass


def _fmt_point(v: float, unit: str = "") -> str:
    if unit == "%":
        return f"{v:,.1f}%"
    txt = _fmt_axis_val(v)
    return f"{txt} {unit}".strip()


def _make_line_chart(timestamps: list[datetime], values: list[float],
                     ylabel: str = "", color: str = HEX_PRIMARY,
                     w_mm: float = CONTENT_W, h_mm: float = 50,
                     unit: str = "", y_domain: tuple | None = None, tz=None) -> bytes:
    if not timestamps or not values or len(timestamps) < 2:
        return _empty_chart(w_mm, h_mm)
    with _figure(w_mm, h_mm) as fig:
        ax = fig.add_subplot()
        ts, mean, bmin, bmax, reduced = _downsample(list(timestamps), list(values))
        if reduced:
            ax.fill_between(ts, bmin, bmax, color=color, alpha=0.12, linewidth=0)
        else:
            ax.fill_between(ts, mean, color=color, alpha=0.09, linewidth=0)
        ax.plot(ts, mean, color=color, linewidth=1.7,
                solid_capstyle="round", solid_joinstyle="round", zorder=4)
        # endpoint marker + selective direct label
        ax.plot([ts[-1]], [mean[-1]], "o", ms=4.4, color=color,
                markeredgecolor="white", markeredgewidth=1.1, zorder=5)
        x0, x1 = mdates.date2num(ts[0]), mdates.date2num(ts[-1])
        span = max(x1 - x0, 1e-6)
        ax.set_xlim(x0 - span * 0.005, x1 + span * 0.10)
        ax.annotate(_fmt_point(mean[-1], unit or ("%" if "%" in ylabel else "")),
                    xy=(ts[-1], mean[-1]), xytext=(5, 0), textcoords="offset points",
                    fontsize=6.8, fontweight="bold", color=HEX_TEXT, va="center")
        vmin = min(bmin) if reduced else min(mean)
        vmax = max(bmax) if reduced else max(mean)
        if y_domain:
            lo = min(y_domain[0], vmin)
            hi = max(y_domain[1], vmax)
        else:
            pad = (vmax - vmin) * 0.12 or max(abs(vmax), 1) * 0.1
            lo = 0 if vmin >= 0 and vmin < vmax * 0.35 else vmin - pad
            hi = vmax + pad
        if lo == hi:
            hi = lo + 1
        ax.set_ylim(lo, hi)
        is_pct = unit == "%" or "%" in ylabel
        all_int = all(float(v).is_integer() for v in mean)
        if is_pct and hi >= 99 and hi <= 101 and hi - lo > 40:
            ax.set_yticks([t for t in (0, 25, 50, 75, 100) if lo <= t <= hi])
        elif all_int and vmax - vmin < 8:
            ax.set_ylim(max(0, min(lo, vmin - 1)), hi + 1)
            ax.yaxis.set_major_locator(mticker.MaxNLocator(nbins=4, integer=True))
        else:
            ax.yaxis.set_major_locator(mticker.MaxNLocator(nbins=4))
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_axis_val))
        if ylabel:
            ax.set_ylabel(ylabel, labelpad=2)
        _style_time_axis(ax, narrow=w_mm < 100, tz=tz)
        return _finish(fig)


def _make_time_bar_chart(timestamps: list[datetime], values: list[float],
                         color: str = HEX_PRIMARY, ylabel: str = "",
                         w_mm: float = CONTENT_W, h_mm: float = 46, tz=None) -> bytes:
    if not timestamps or not any((v or 0) > 0 for v in values):
        return _empty_chart(w_mm, h_mm)
    with _figure(w_mm, h_mm) as fig:
        ax = fig.add_subplot()
        if len(timestamps) > 1:
            deltas = sorted((timestamps[i + 1] - timestamps[i]).total_seconds()
                            for i in range(len(timestamps) - 1))
            interval_d = max(deltas[len(deltas) // 2], 60) / 86400
        else:
            interval_d = 1
            ax.set_xlim(mdates.date2num(timestamps[0]) - 1.6,
                        mdates.date2num(timestamps[0]) + 1.6)
        ax.bar(timestamps, values, width=interval_d * 0.72, color=color,
               edgecolor="white", linewidth=0.6, zorder=3)
        vmax = max(values)
        ax.set_ylim(0, vmax * 1.18)
        # selectively label the peak only
        peak_i = values.index(vmax)
        ax.annotate(f"{vmax:,.0f}", xy=(timestamps[peak_i], vmax),
                    xytext=(0, 2.5), textcoords="offset points", ha="center",
                    fontsize=6.6, color=HEX_MUTED)
        ax.yaxis.set_major_locator(mticker.MaxNLocator(nbins=4, integer=True))
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_axis_val))
        if ylabel:
            ax.set_ylabel(ylabel, labelpad=2)
        _style_time_axis(ax, narrow=w_mm < 100, tz=tz)
        return _finish(fig)


def _make_bar_chart(labels: list[str], values: list[float],
                    colors: list[str] | str = HEX_PRIMARY,
                    ylabel: str = "", w_mm: float = CONTENT_W,
                    h_mm: float | None = None) -> bytes:
    """Ranked horizontal bars — the readable form for named items."""
    if not labels or not values:
        return _empty_chart(w_mm, h_mm or 40)
    pairs = sorted(zip(labels, values), key=lambda p: p[1], reverse=True)
    labels = [p[0] for p in pairs]
    values = [p[1] for p in pairs]
    n = len(labels)
    if h_mm is None:
        h_mm = max(26.0, 7.2 * n + 10)
    color = colors if isinstance(colors, str) else (colors[0] if colors else HEX_PRIMARY)
    with _figure(w_mm, h_mm) as fig:
        ax = fig.add_subplot()
        short = [l if len(l) <= 26 else l[:25] + "…" for l in labels]
        ax.barh(range(n), values, height=0.62, color=color, zorder=3)
        ax.set_yticks(range(n), labels=short)
        ax.invert_yaxis()
        ax.tick_params(axis="y", labelsize=7.2, labelcolor=HEX_TEXT)
        ax.grid(axis="x")
        ax.grid(False, axis="y")
        vmax = max(values) or 1
        ax.set_xlim(0, vmax * 1.14)
        for i, v in enumerate(values):
            ax.annotate(f"{v:,.0f}", xy=(v, i), xytext=(3, 0),
                        textcoords="offset points", va="center",
                        fontsize=6.8, color=HEX_TEXT)
        ax.xaxis.set_major_locator(mticker.MaxNLocator(nbins=4, integer=True))
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(_fmt_axis_val))
        if ylabel:
            ax.set_xlabel(ylabel, labelpad=2)
        return _finish(fig)


def _make_donut(labels: list[str], values: list[float],
                colors: list[str] | None = None, center_text: str = "",
                center_sub: str = "", w_mm: float = 86, h_mm: float = 54,
                unit: str = "",
                empty_label: str = "No data available for this period") -> bytes:
    """Part-to-whole donut with a proper side legend (label · value · share)."""
    pairs = [(l, v) for l, v in zip(labels or [], values or []) if (v or 0) > 0]
    if not pairs or sum(v for _, v in pairs) <= 0:
        return _empty_chart(w_mm, h_mm, empty_label)
    pairs.sort(key=lambda p: p[1], reverse=True)
    if colors and len(colors) >= len(pairs):
        # caller-supplied colors follow the original label order
        cmap = {l: c for l, c in zip(labels, colors)}
        slice_colors = [cmap.get(l, OTHER_HEX) for l, _ in pairs]
    else:
        slice_colors = [CATEGORICAL_HEX[i % len(CATEGORICAL_HEX)] for i in range(len(pairs))]
    if len(pairs) > 6:
        head, tail = pairs[:5], pairs[5:]
        pairs = head + [("Other", sum(v for _, v in tail))]
        slice_colors = slice_colors[:5] + [OTHER_HEX]
    total = sum(v for _, v in pairs)

    with _figure(w_mm, h_mm, layout=None) as fig:
        ax = fig.add_axes([0.01, 0.04, 0.44, 0.92])
        ax.pie([v for _, v in pairs], colors=slice_colors, startangle=90,
               counterclock=False,
               wedgeprops=dict(width=0.38, edgecolor="white", linewidth=1.6))
        ax.set_aspect("equal")
        if center_text:
            ax.text(0, 0.06 if center_sub else 0, center_text, ha="center",
                    va="center", fontsize=11.5, fontweight="bold", color=HEX_TEXT)
        if center_sub:
            ax.text(0, -0.24, center_sub, ha="center", va="center",
                    fontsize=5.8, color=HEX_MUTED)
        # legend column
        n = len(pairs)
        row_h = min(0.16, 0.84 / max(n, 1))
        y0 = 0.5 + row_h * (n - 1) / 2
        for i, ((lab, val), col) in enumerate(zip(pairs, slice_colors)):
            y = y0 - i * row_h
            fig.add_artist(Rectangle(
                (0.485, y - 0.028), 0.022, 0.062, transform=fig.transFigure,
                facecolor=col, edgecolor="none"))
            lab_s = lab if len(lab) <= 18 else lab[:17] + "…"
            fig.text(0.522, y, lab_s, ha="left", va="center",
                     fontsize=7.2, color=HEX_TEXT)
            vtxt = f"{val:,.1f}" if isinstance(val, float) and not float(val).is_integer() else f"{val:,.0f}"
            if unit:
                vtxt = f"{vtxt} {unit}"
            share = f"{val / total * 100:.0f}%"
            fig.text(0.99, y, f"{vtxt} · {share}", ha="right", va="center",
                     fontsize=6.8, color=HEX_MUTED)
        return _finish(fig)


# ---------------------------------------------------------------------------
# ZenPlusReport - FPDF subclass (the document design system)
# ---------------------------------------------------------------------------

def _png_size(png: bytes) -> tuple[int, int]:
    """Pixel dimensions from the PNG IHDR header."""
    return (int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big"))


# Accent colors used on KPI cards / section chrome (brighter mark steps)
ACCENT_RGB_CARD = {
    "primary": (79, 107, 246),
    "success": (22, 163, 74),
    "warning": (217, 119, 6),
    "danger": (220, 38, 38),
    "info": (8, 145, 178),
    "neutral": (100, 116, 139),
}


def _render_toc(pdf: "ZenPlusReport", outline) -> None:
    """Contents page: numbered entries, dotted leaders, clickable page refs."""
    right = pdf.page_w - MARGIN_R
    pdf.set_y(pdf.t_margin + 4)
    pdf._sans("B", 16)
    pdf.set_text_color(*COLOR_TEXT)
    pdf.cell(0, 9, "Contents", new_x="LMARGIN")
    y = pdf.get_y() + 10
    pdf.set_fill_color(*pdf.accent)
    pdf.rect(pdf.l_margin, y, 11, 0.85, "F")
    pdf._hairline(pdf.l_margin + 13, y + 0.42, right)
    pdf.set_y(y + 7)

    title_w = pdf.body_w - 10 - 20
    entries = [o for o in outline if o.level == 0]
    muted = getattr(pdf, "_toc_muted", set())
    for i, o in enumerate(entries, 1):
        if pdf.get_y() + 8 > pdf.page_break_trigger:
            pdf.add_page()
        y = pdf.get_y()
        x = pdf.l_margin
        off = i in muted
        pdf._sans("B", 8.6)
        pdf.set_text_color(*(COLOR_FAINT if off else pdf.accent))
        pdf.set_xy(x, y)
        pdf.cell(9, 6.6, f"{i:02d}")
        suffix = "(not included)" if off else ""
        pdf._sans("", 8.2)
        suffix_w = pdf.get_string_width(suffix) + 2 * pdf.c_margin if suffix else 0.0
        pdf._sans("", 9.2)
        pdf.set_text_color(*(COLOR_MUTED if off else COLOR_TEXT))
        title = pdf._fit(_safe(o.name), title_w - suffix_w)
        # cells advance x to their right edge; text sits c_margin inside
        pdf.set_xy(x + 10 - pdf.c_margin, y)
        pdf.cell(pdf.get_string_width(title) + 2 * pdf.c_margin, 6.6, title)
        if suffix:
            pdf._sans("", 8.2)
            pdf.set_text_color(*COLOR_FAINT)
            pdf.cell(suffix_w, 6.6, suffix)
        t_end = pdf.get_x() + 1.0
        pdf._sans("", 8.6)
        pdf.set_text_color(*COLOR_MUTED)
        pdf.set_xy(right - 12, y)
        pdf.cell(12, 6.6, str(o.page_number), align="R")
        # dotted leader
        pdf.set_draw_color(*COLOR_FAINT)
        pdf.set_line_width(0.25)
        pdf.set_dash_pattern(dash=0.4, gap=1.4)
        pdf.line(t_end, y + 4.7, right - 13.5, y + 4.7)
        pdf.set_dash_pattern()
        lk = pdf.add_link()
        pdf.set_link(lk, page=o.page_number)
        pdf.link(x, y, pdf.body_w, 6.6, lk)
        pdf.set_y(y + 7.4)


class ZenPlusReport(FPDF):
    """Enterprise report document: branded cover, contents, slim chrome and a
    small set of typographic building blocks shared by every PDF report."""

    def __init__(self, title: str = "ZenPlus Report",
                 company_name: str = "ZenPlus",
                 logo_bytes: bytes | None = None,
                 period_label: str = "",
                 generated_at: datetime | None = None,
                 subtitle: str = "",
                 category: str = "",
                 scope_label: str = "",
                 accent: tuple = COLOR_PRIMARY,
                 *,
                 paper_size: str = "A4",
                 cover_logo_bytes: bytes | None = None,
                 cover_logo_on_dark: bool = False,
                 doc_label: str = "",
                 classification: str = "",
                 footer_text: str = "",
                 timezone_name: str = "UTC",
                 tz_label: str = "UTC",
                 generated_label: str = "",
                 disclaimer: str = "",
                 contact_line: str = "",
                 branding=None):
        """``accent`` is the organisation's brand colour (RGB); the document
        uses a contrast-adjusted accent derived from it. ``logo_bytes`` is the
        light-background logo (running header); ``cover_logo_bytes`` the logo
        for the dark cover (``cover_logo_on_dark`` False puts it on a white
        tile, for organisations that only uploaded a light-background logo)."""
        self.paper_size = paper_format(paper_size)
        super().__init__(orientation="P", unit="mm", format=self.paper_size)
        # Page geometry follows the paper size; never use the A4 module
        # constants for layout.
        self.page_w = self.w
        self.page_h = self.h
        self.body_w = self.page_w - MARGIN_L - MARGIN_R
        self.set_auto_page_break(auto=True, margin=MARGIN_B)
        self.set_margins(MARGIN_L, MARGIN_T, MARGIN_R)
        self.report_title = title
        self.company_name = company_name or "ZenPlus"
        self.period_label = period_label
        self.generated_at = generated_at or datetime.now(timezone.utc)
        self.subtitle = subtitle
        self.category = category
        self.scope_label = scope_label
        self.tokens = brand_tokens(accent or COLOR_PRIMARY)
        self.brand = self.tokens["brand"]
        self.accent = self.tokens["accent"]
        self.branding = branding
        self.doc_label = doc_label or category or "Report"
        self.classification = (classification or "").strip()
        self.footer_text = (footer_text or "").strip()
        self.timezone_name = timezone_name or "UTC"
        self.tz_label = tz_label or "UTC"
        self.generated_label = generated_label or self._fmt_generated(self.generated_at)
        self.disclaimer = (disclaimer or "").strip()
        self.contact_line = (contact_line or "").strip()
        self._logo = logo_bytes
        self._cover_logo = cover_logo_bytes or logo_bytes
        self._cover_logo_on_dark = bool(cover_logo_bytes) and cover_logo_on_dark
        self._header_logo_failed = False
        self._image_sizes: dict = {}
        self._toc_muted: set[int] = set()   # contents entries shown as "not included"
        self._decor = True          # running header/footer on content pages
        self._outline_on = True     # record sections into the PDF outline
        self._section_n = 0
        self._in_column = False
        self._register_fonts()
        self.alias_nb_pages()
        self._sans("", 9)
        # Document properties (shown by PDF viewers and indexers).
        try:
            self.set_title(str(title or "Report"))
            self.set_author(str(self.company_name))
            self.set_creator("ZenPlus")
            if subtitle:
                self.set_subject(str(subtitle)[:300])
            self.set_lang("en")
        except Exception:
            pass

    @classmethod
    def from_meta(cls, meta: dict, **overrides) -> "ZenPlusReport":
        """A document configured from report_sections.build_report_meta()."""
        branding = meta.get("branding")
        cover_on_dark = bool(getattr(branding, "logo_dark", None)) if branding is not None \
            else bool(meta.get("cover_logo_on_dark"))
        rgb = meta.get("brand_rgb")
        if not rgb and meta.get("brand_color"):
            from app.services.branding import hex_to_rgb
            rgb = hex_to_rgb(meta["brand_color"])
        kwargs = dict(
            title=meta.get("title") or "Report",
            company_name=meta.get("company_name") or "ZenPlus",
            logo_bytes=meta.get("logo_bytes"),
            period_label=meta.get("period_label") or "",
            generated_at=meta.get("generated_at"),
            subtitle=meta.get("description") or "",
            category=meta.get("category") or "",
            scope_label=meta.get("scope_label") or "",
            accent=tuple(rgb) if rgb else COLOR_PRIMARY,
            paper_size=meta.get("paper_size") or "A4",
            cover_logo_bytes=meta.get("cover_logo_bytes"),
            cover_logo_on_dark=cover_on_dark,
            doc_label=meta.get("category") or "",
            classification=meta.get("confidentiality") or "",
            footer_text=meta.get("footer_text") or "",
            timezone_name=meta.get("timezone") or "UTC",
            tz_label=meta.get("tz_label") or "UTC",
            generated_label=meta.get("generated_label") or "",
            disclaimer=meta.get("disclaimer") or "",
            contact_line=meta.get("contact_line") or "",
            branding=branding,
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    def _fmt_generated(self, at: datetime) -> str:
        b = getattr(self, "branding", None)
        if b is not None and hasattr(b, "fmt_datetime"):
            try:
                return b.fmt_datetime(at, with_tz=True)
            except Exception:
                pass
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        return f"{at.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC"

    @property
    def tz_note(self) -> str:
        """'All times GMT+3 (Asia/Riyadh)', or 'All times UTC'."""
        if self.timezone_name in ("UTC", "Etc/UTC") or self.tz_label == self.timezone_name:
            return f"All times {self.tz_label}"
        return f"All times {self.tz_label} ({self.timezone_name})"

    # ----- fonts -----

    def _register_fonts(self):
        if UNICODE_FONTS:
            self.F_SANS, self.F_MONO = "ZSans", "ZMono"
            for style, path in _SANS_FILES.items():
                self.add_font("ZSans", style, path)
            mono_r = _MONO_FILES.get("")
            if mono_r:
                self.add_font("ZMono", "", mono_r)
                self.add_font("ZMono", "B", _MONO_FILES.get("B") or mono_r)
            else:
                self.F_MONO = "Courier"
        else:
            self.F_SANS, self.F_MONO = "Helvetica", "Courier"

    def _sans(self, style: str = "", size: float = 9):
        self.set_font(self.F_SANS, style, size)

    def _mono(self, style: str = "", size: float = 8):
        self.set_font(self.F_MONO, style, size)

    # ----- small drawing helpers -----

    @property
    def content_w(self) -> float:
        return self.w - self.l_margin - self.r_margin

    def _hairline(self, x1: float, y: float, x2: float,
                  color: tuple = COLOR_HAIRLINE, width: float = 0.25):
        self.set_draw_color(*color)
        self.set_line_width(width)
        self.line(x1, y, x2, y)

    def _fit(self, txt: str, max_w: float) -> str:
        """Ellipsize txt to fit max_w using the current font."""
        if self.get_string_width(txt) <= max_w:
            return txt
        ell = "…" if UNICODE_FONTS else "..."
        while txt and self.get_string_width(txt + ell) > max_w:
            txt = txt[:-1]
        return txt + ell

    def _eyebrow(self, x: float, y: float, txt: str, color: tuple,
                 size: float = 6.6, align: str = "L", w: float = 0):
        """Letter-spaced small-caps label."""
        self._sans("B", size)
        self.set_text_color(*color)
        try:
            self.set_char_spacing(0.55)
        except Exception:
            pass
        self.set_xy(x, y)
        self.cell(w or self.get_string_width(txt.upper()) + 6, 3.6,
                  _safe(txt.upper()), align=align)
        try:
            self.set_char_spacing(0)
        except Exception:
            pass

    def _gradient_bar(self, x: float, y: float, w: float, h: float,
                      c1: tuple, c2: tuple, steps: int = 48):
        for i in range(steps):
            t = i / max(steps - 1, 1)
            self.set_fill_color(round(c1[0] + (c2[0] - c1[0]) * t),
                                round(c1[1] + (c2[1] - c1[1]) * t),
                                round(c1[2] + (c2[2] - c1[2]) * t))
            self.rect(x + w * i / steps, y, w / steps + 0.15, h, "F")

    # ----- images (logos) -----

    @staticmethod
    def _norm_image(data: bytes) -> bytes:
        """fpdf2 recognises SVG only when it starts with '<svg ' or '<?xml ':
        drop a BOM, comments or doctype before the root element."""
        if not data or data[:4] == b"\x89PNG" or data[:3] == b"\xff\xd8\xff" or data[:4] == b"RIFF":
            return data
        i = data[:4096].find(b"<svg")
        if i < 0:
            return data
        body = data[i:]
        if body[4:5] != b" ":
            body = b"<svg " + body[4:].lstrip()
        return body

    def _image_size(self, data: bytes) -> tuple[float, float] | None:
        """Intrinsic (width, height) of a logo (pixels or SVG units). Parsed
        once per document: the running header asks on every page."""
        cache = self._image_sizes
        key = (len(data or b""), hash(data))
        if key in cache:
            return cache[key]
        from fpdf.image_parsing import preload_image
        size = None
        try:
            _name, img, info = preload_image(self.image_cache, io.BytesIO(self._norm_image(data)))
            vb = getattr(img, "viewbox", None)
            if vb and len(vb) == 4 and vb[2] and vb[3]:
                size = float(vb[2]), float(vb[3])
            else:
                w, h = float(info.width), float(info.height)
                size = (w, h) if w > 0 and h > 0 else None
        except Exception:
            size = None
        cache[key] = size
        return size

    def _place_image(self, data: bytes, x: float, y: float, max_w: float, max_h: float,
                     align: str = "L") -> tuple[float, float] | None:
        """Draw a logo inside max_w × max_h at its aspect ratio (vertically
        centred, left/right aligned). Returns the drawn size, or None when the
        image cannot be read (callers fall back to the company name)."""
        size = self._image_size(data)
        if not size:
            return None
        iw, ih = size
        scale = min(max_w / iw, max_h / ih)
        w, h = iw * scale, ih * scale
        dx = x if align == "L" else x + max_w - w
        try:
            self.image(io.BytesIO(self._norm_image(data)), x=dx, y=y + (max_h - h) / 2, w=w, h=h)
        except Exception:
            logging.getLogger(__name__).debug("logo could not be drawn", exc_info=True)
            return None
        return w, h

    def _classification_pill(self, x: float, y: float, h: float = 4.6, size: float = 6.2,
                             align: str = "R") -> float:
        """Classification label pill ending at ``x`` (align R) or starting at it
        (align L); returns its width."""
        label = _safe(self.classification.upper())
        self._sans("B", size)
        spacing = 0.45
        w = self.get_string_width(label) + spacing * max(len(label) - 1, 0) + 5.2
        x0 = x - w if align == "R" else x
        self.set_fill_color(*classification_rgb(self.classification))
        self.rect(x0, y, w, h, "F", round_corners=True, corner_radius=h / 2)
        self.set_text_color(255, 255, 255)
        try:
            self.set_char_spacing(spacing)
        except Exception:
            pass
        self.set_xy(x0, y)
        self.cell(w, h, label, align="C")
        try:
            self.set_char_spacing(0)
        except Exception:
            pass
        return w

    # ----- header / footer -----

    def header(self):
        if not self._decor:
            return
        y_rule = 15.2
        x = MARGIN_L
        right = self.page_w - MARGIN_R
        tx = x
        if self._logo and not self._header_logo_failed:
            dims = self._place_image(self._logo, x, 6.3, 30, 6.2)
            if dims:
                tx = x + dims[0] + 3.2
            else:
                self._header_logo_failed = True
        if tx == x:
            self._sans("B", 8.2)
            self.set_text_color(*COLOR_TEXT)
            self.set_xy(x, 7.6)
            name = self._fit(_safe(self.company_name), 60)
            self.cell(self.get_string_width(name) + 1, 4.4, name)
            self._sans("", 8.2)
            self.set_text_color(*COLOR_MUTED)
            self.cell(4, 4.4, "·", align="C")
            tx = self.get_x()
        right_w = 0.0
        if self.classification:
            right_w = self._classification_pill(right, 7.5) + 3
        elif self.period_label:
            self._sans("", 7.4)
            right_w = min(self.get_string_width(_safe(self.period_label)) + 2, self.body_w * 0.42)
            self.set_text_color(*COLOR_MUTED)
            self.set_xy(right - right_w, 7.7)
            self.cell(right_w, 4.2, self._fit(_safe(self.period_label), right_w), align="R")
            right_w += 3
        self._sans("", 8.2)
        self.set_text_color(*COLOR_MUTED)
        title_w = max(right - right_w - tx, 20)
        self.set_xy(tx, 7.6)
        self.cell(title_w, 4.4, self._fit(_safe(self.report_title), title_w))
        self._hairline(x, y_rule, right)
        self.set_fill_color(*self.accent)
        self.rect(x, y_rule - 0.35, 11, 0.7, "F")
        self.set_text_color(*COLOR_TEXT)
        self.set_y(self.t_margin)

    def footer(self):
        # footer() runs when a page is closed (often after cover() returned),
        # so the cover page must be excluded by number, not by the decor flag.
        if not self._decor or self.page_no() == getattr(self, "_cover_page", 0):
            return
        y = self.page_h - 12.6
        x = MARGIN_L
        right = self.page_w - MARGIN_R
        self._hairline(x, y, right)
        third = self.body_w / 3
        self._sans("", 6.8)
        self.set_text_color(*COLOR_FAINT)
        self.set_xy(x, y + 1.7)
        self.cell(third, 4, self._fit(_safe(self.footer_text or self.company_name), third - 2))
        self.set_xy(x + third, y + 1.7)
        self.cell(third, 4, self._fit(_safe(self.tz_note), third - 2), align="C")
        self.set_xy(right - third, y + 1.7)
        self.cell(third, 4, f"Page {self.page_no()} of {{nb}}", align="R")
        if self.classification:
            page_w = self.get_string_width(f"Page {self.page_no()} of 99") + 2.5
            self._sans("B", 6.6)
            self.set_text_color(*classification_rgb(self.classification))
            label = _safe(self.classification.upper()) + "  ·"
            lw = self.get_string_width(label) + 1
            self.set_xy(right - page_w - lw, y + 1.7)
            self.cell(lw, 4, label, align="R")

    # ----- cover page -----

    # Constellation accent in the upper-right quadrant of the cover (A4
    # coordinates; shifted to the right edge on wider paper).
    _MOTIF_NODES = [
        (122, 52), (141, 46), (163, 50), (184, 44), (194, 60),
        (179, 69), (156, 64), (132, 71), (144, 82), (167, 84),
        (189, 79), (121, 88), (156, 92), (183, 91), (136, 58), (174, 56),
    ]
    _MOTIF_EDGES = [
        (0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 0),
        (6, 1), (5, 2), (7, 8), (8, 9), (9, 10), (10, 4), (8, 12),
        (12, 13), (13, 10), (11, 8), (11, 7), (14, 1), (14, 7), (15, 2),
        (15, 5), (9, 12),
    ]

    def _cover_motif(self):
        dx = self.page_w - PAGE_W
        t = self.tokens
        self.set_draw_color(*t["ground_motif"])
        self.set_line_width(0.22)
        for a, b in self._MOTIF_EDGES:
            x1, y1 = self._MOTIF_NODES[a]
            x2, y2 = self._MOTIF_NODES[b]
            self.line(x1 + dx, y1, x2 + dx, y2)
        node = mix_rgb(t["ground"], t["brand"], 0.55)
        for i, (x, y) in enumerate(self._MOTIF_NODES):
            if i in (1, 9, 15):
                self.set_fill_color(*t["ground_highlight"])
                r = 1.25
            else:
                self.set_fill_color(*node)
                r = 0.85
            self.ellipse(x + dx - r, y - r, 2 * r, 2 * r, "F")

    def _cover_brand(self, x: float, y: float) -> bool:
        """Cover logo at its aspect ratio in a ~50×16 mm box. A dark-background
        logo sits on the cover ground; a light-background one on a white tile."""
        data = self._cover_logo
        if not data:
            return False
        if self._cover_logo_on_dark:
            return self._place_image(data, x, y, 50, 16) is not None
        size = self._image_size(data)
        if not size:
            return False
        iw, ih = size
        scale = min(46 / iw, 12 / ih)
        w, h = iw * scale, ih * scale
        self.set_fill_color(255, 255, 255)
        self.rect(x, y + (16 - h - 4) / 2, w + 4, h + 4, "F", round_corners=True,
                  corner_radius=2.2)
        return self._place_image(data, x + 2, y + (16 - h) / 2, w, h) is not None

    def cover(self, doc_label: str | None = None, report_ref: str | None = None):
        """Dark branded cover page (ground tinted with the brand colour)."""
        t = self.tokens
        pw, ph = self.page_w, self.page_h
        right = pw - MARGIN_R
        bw = self.body_w
        prev = self._decor
        self._decor = False
        prev_apb = self.auto_page_break
        self.set_auto_page_break(False)
        self.add_page()
        self._cover_page = self.page_no()
        self.set_fill_color(*t["ground"])
        self.rect(0, 0, pw, ph, "F")
        self._cover_motif()
        self._gradient_bar(0, 0, pw, 1.7, t["brand"], mix_rgb(t["brand"], (255, 255, 255), 0.45))

        # brand row: logo (or the company name) and the document label
        y = 14
        if not self._cover_brand(MARGIN_L, y):
            self._sans("B", 13)
            self.set_text_color(255, 255, 255)
            self.set_xy(MARGIN_L, y + 4.6)
            self.cell(bw - 70, 6.6, self._fit(_safe(self.company_name), bw - 72))
        self._eyebrow(right - 70, y + 6.2, doc_label or self.doc_label,
                      t["ground_label"], size=6.6, align="R", w=70)
        self._hairline(MARGIN_L, 36, right, t["ground_panel"], 0.3)

        # title block
        ty = 96
        if self.classification:
            self._classification_pill(MARGIN_L, ty, h=5.6, size=7.0, align="L")
            ty += 10
        self._sans("B", 26)
        self.set_text_color(255, 255, 255)
        self.set_xy(MARGIN_L, ty)
        self.multi_cell(bw, 11.5, _safe(self.report_title))
        if self.subtitle:
            self.set_xy(MARGIN_L, self.get_y() + 3)
            self._sans("", 9.6)
            self.set_text_color(*t["ground_text"])
            sub = self.subtitle if len(self.subtitle) <= 420 else self.subtitle[:419] + "…"
            self.multi_cell(min(150, bw), 5.4, _safe(sub))

        # meta grid
        my = ph - 92
        self._hairline(MARGIN_L, my, right, t["ground_panel"], 0.3)
        local = self.generated_at
        if self.branding is not None and hasattr(self.branding, "localize"):
            try:
                local = self.branding.localize(self.generated_at)
            except Exception:
                pass
        ref = report_ref or f"RPT-{local:%Y%m%d-%H%M%S}"
        meta = [
            ("Reporting period", self.period_label or "—"),
            ("Generated", self.generated_label),
            ("Scope", self.scope_label or "All monitored infrastructure"),
            ("Time zone", self.tz_note),
            ("Prepared by", self.company_name),
            ("Reference", ref),
        ]
        col_w = bw / 2
        for i, (label, value) in enumerate(meta):
            cx = MARGIN_L + (i % 2) * col_w
            cy = my + 7 + (i // 2) * 15
            self._eyebrow(cx, cy, label, t["ground_label"], size=6.2)
            self._sans("", 9.4)
            self.set_text_color(255, 255, 255)
            self.set_xy(cx, cy + 4.4)
            self.cell(col_w - 6, 5.4, self._fit(_safe(value), col_w - 8))

        # bottom band
        by = ph - 18
        self._hairline(MARGIN_L, by, right, t["ground_panel"], 0.3)
        self._sans("", 7)
        self.set_text_color(*t["ground_label"])
        self.set_xy(MARGIN_L, by + 2.5)
        left_txt = self.footer_text or self.contact_line
        self.cell(bw - 50, 4.4, self._fit(_safe(left_txt), bw - 52))
        self._sans("", 6.4)
        self.set_xy(right - 48, by + 2.5)
        self.cell(48, 4.4, "Generated by ZenPlus", align="R")

        self.set_auto_page_break(prev_apb, MARGIN_B)
        self._decor = prev
        self.set_text_color(*COLOR_TEXT)

    # ----- table of contents -----

    def toc(self, expected_entries: int):
        pages = 1 if expected_entries <= 28 else 2
        self.add_page()
        try:
            self.insert_toc_placeholder(_render_toc, pages=pages, allow_extra_pages=True)
        except TypeError:  # older fpdf2 without allow_extra_pages
            self.insert_toc_placeholder(_render_toc, pages=pages)

    # ----- structure blocks -----

    def section_title(self, title: str, description: str | None = None,
                      category: str | None = None, number=("auto",),
                      keep_with: float = 36.0):
        """Numbered section heading with accent rule; records a PDF bookmark.

        ``keep_with`` is the height (mm) of the first block that follows: the
        heading moves to the next page with it rather than sit orphaned at
        the bottom of a page."""
        if description:
            self._sans("", 8.2)
            desc_h = self.multi_cell(self.content_w, 4.4, _safe(description),
                                     dry_run=True, output="HEIGHT") + 1.2
        else:
            desc_h = 1.4
        head_h = 3 + (4.6 if category else 0) + 9.6 + 3.4 + desc_h
        self._check_page_space(head_h + min(max(keep_with, 0.0), 150.0))
        if number == ("auto",):
            self._section_n += 1
            num = self._section_n
        else:
            num = number
        if self._outline_on and not self._in_column:
            try:
                self.start_section(_safe(title), level=0)
            except Exception:
                pass
        self.ln(3)
        x = self.l_margin
        y = self.get_y()
        if category:
            self._eyebrow(x, y, category, COLOR_FAINT, size=6.2)
            y += 4.6
        tx = x
        if num is not None:
            self._sans("B", 10.5)
            self.set_text_color(*self.accent)
            self.set_xy(x, y + 1.9)
            ns = f"{num:02d}"
            self.cell(self.get_string_width(ns) + 1, 6, ns)
            tx = x + self.get_string_width(ns) + 3.4
        self._sans("B", 13.5)
        self.set_text_color(*COLOR_TEXT)
        self.set_xy(tx, y)
        self.cell(self.content_w - (tx - x), 8, self._fit(_safe(title), self.content_w - (tx - x)))
        y2 = y + 9.6
        self.set_fill_color(*self.accent)
        self.rect(x, y2, 11, 0.85, "F")
        self._hairline(x + 13, y2 + 0.42, x + self.content_w)
        self.set_y(y2 + 3.4)
        if description:
            self._sans("", 8.2)
            self.set_text_color(*COLOR_MUTED)
            self.set_x(x)
            self.multi_cell(self.content_w, 4.4, _safe(description))
            self.set_y(self.get_y() + 1.2)
        else:
            self.set_y(self.get_y() + 1.4)

    def sub_heading(self, text_str: str, keep_with: float = 32.0):
        """Small heading kept on the page of the first ``keep_with`` mm of
        what follows (by default a table header and its first rows)."""
        self._check_page_space(9.2 + keep_with)
        self.ln(1.6)
        x = self.l_margin
        y = self.get_y()
        self.set_fill_color(*self.accent)
        self.rect(x, y + 1.15, 2.1, 3.3, "F")
        self._sans("B", 9.6)
        self.set_text_color(*COLOR_TEXT)
        self.set_xy(x + 4.2, y)
        self.cell(self.content_w - 4.2, 5.6, self._fit(_safe(text_str), self.content_w - 4.2))
        self.set_y(y + 7.6)

    def body_text(self, text_str: str):
        self._sans("", 8.8)
        self.set_text_color(*COLOR_BODY)
        self.set_x(self.l_margin)
        self.multi_cell(self.content_w, 4.7, _safe(text_str))
        self.ln(1.6)

    def muted_text(self, text_str: str):
        self._sans("", 7.8)
        self.set_text_color(*COLOR_MUTED)
        self.set_x(self.l_margin)
        self.multi_cell(self.content_w, 4.2, _safe(text_str))
        self.ln(1.2)

    def note(self, text_str: str, kind: str = "info", label: str = ""):
        """Callout box with accent bar. info: brand accent; warning: amber;
        error: red with a bold lead (a section that failed to generate)."""
        text_str = _safe(text_str)
        bar, bg = {
            "warning": (COLOR_WARNING, TINT_WARNING),
            "error": (COLOR_DANGER, TINT_DANGER),
        }.get(kind, (self.accent, COLOR_BG_TINT))
        inner_w = self.content_w - 11
        lead_h = 0.0
        if label:
            lead_h = 4.6
        self._sans("", 7.9)
        h_text = self.multi_cell(inner_w, 4.2, text_str, dry_run=True, output="HEIGHT")
        box_h = h_text + lead_h + 5.2
        self._check_page_space(box_h + 3)
        x = self.l_margin
        y = self.get_y()
        self.set_fill_color(*bg)
        self.rect(x, y, self.content_w, box_h, "F", round_corners=True, corner_radius=1.8)
        self.set_fill_color(*bar)
        self.rect(x + 3, y + 2.4, 1.15, box_h - 4.8, "F", round_corners=True, corner_radius=0.5)
        if label:
            self._sans("B", 7.9)
            self.set_text_color(*(bar if kind == "error" else COLOR_TEXT))
            self.set_xy(x + 7.4, y + 2.4)
            self.cell(inner_w, 4.2, self._fit(_safe(label), inner_w))
            self._sans("", 7.9)
        self.set_text_color(*COLOR_BODY)
        self.set_xy(x + 7.4, y + 2.6 + lead_h)
        self.multi_cell(inner_w, 4.2, text_str)
        self.set_y(y + box_h + 2.6)

    def restricted_card(self, text_str: str):
        """Muted 'Not included' card for a section the reader may not see."""
        text_str = _safe(text_str)
        inner_w = self.content_w - 14
        self._sans("", 7.8)
        h_text = self.multi_cell(inner_w, 4.1, text_str, dry_run=True, output="HEIGHT")
        box_h = h_text + 11.4
        self._check_page_space(box_h + 3)
        x = self.l_margin
        y = self.get_y()
        self.set_fill_color(*COLOR_BG_TINT)
        self.set_draw_color(*COLOR_HAIRLINE)
        self.set_line_width(0.3)
        self.set_dash_pattern(dash=1.2, gap=0.9)
        self.rect(x, y, self.content_w, box_h, "DF", round_corners=True, corner_radius=2)
        self.set_dash_pattern()
        # padlock glyph
        lx, ly = x + 4.2, y + 3.4
        self.set_draw_color(*COLOR_FAINT)
        self.set_line_width(0.45)
        self.arc(lx + 0.55, ly - 0.2, 2.2, 2.6, 180, 360)
        self.line(lx + 0.55, ly + 1.1, lx + 0.55, ly + 1.7)
        self.line(lx + 2.75, ly + 1.1, lx + 2.75, ly + 1.7)
        self.set_fill_color(*COLOR_FAINT)
        self.rect(lx, ly + 1.6, 3.3, 2.5, "F", round_corners=True, corner_radius=0.4)
        self._eyebrow(x + 10, y + 3.4, "Not included", COLOR_MUTED, size=6.4)
        self._sans("", 7.8)
        self.set_text_color(*COLOR_MUTED)
        self.set_xy(x + 10, y + 7.8)
        self.multi_cell(inner_w, 4.1, text_str)
        self.set_y(y + box_h + 2.6)

    def disclaimer_block(self, text_str: str, contact: str = "", address: str = ""):
        """Closing disclaimer (printed once, at the end of the document)."""
        text_str = _safe(text_str)
        extra = " · ".join(_safe(p) for p in (address, contact) if p)
        self._sans("", 7.4)
        h_text = self.multi_cell(self.content_w, 3.9, text_str, dry_run=True, output="HEIGHT") \
            if text_str else 0
        need = h_text + (5 if extra else 0) + 14
        self._check_page_space(need)
        self.ln(4)
        x = self.l_margin
        y = self.get_y()
        self._hairline(x, y, x + self.content_w)
        self._eyebrow(x, y + 3, "Disclaimer" if text_str else "About this report",
                      COLOR_FAINT, size=6.2)
        self.set_y(y + 8)
        if text_str:
            self._sans("", 7.4)
            self.set_text_color(*COLOR_MUTED)
            self.set_x(x)
            self.multi_cell(self.content_w, 3.9, text_str)
        if extra:
            self._sans("", 7.0)
            self.set_text_color(*COLOR_FAINT)
            self.set_x(x)
            self.multi_cell(self.content_w, 3.8, extra)
        self.ln(2)

    def empty_state(self, text_str: str = "No data available for this period."):
        self._check_page_space(15)
        x = self.l_margin
        y = self.get_y()
        h = 11.5
        self.set_fill_color(*COLOR_BG_TINT)
        self.set_draw_color(*COLOR_HAIRLINE)
        self.set_line_width(0.3)
        self.rect(x, y, self.content_w, h, "DF", round_corners=True, corner_radius=2)
        self._sans("", 7.8)
        self.set_text_color(*COLOR_MUTED)
        self.set_xy(x, y + (h - 4.2) / 2)
        self.cell(self.content_w, 4.2, _safe(text_str), align="C")
        self.set_y(y + h + 3)

    # ----- KPI cards -----

    def kpi_row(self, kpis: list, per_row: int | None = None):
        """Row(s) of KPI cards. Accepts (label, value, rgb) tuples or dicts
        {label, value, accent, subtitle?} where accent is a name or rgb."""
        items = []
        for k in kpis:
            if isinstance(k, dict):
                acc = k.get("accent") or "primary"
                rgb = self.accent if acc == "primary" else \
                    ACCENT_RGB_CARD.get(acc, acc if isinstance(acc, tuple) else self.accent)
                items.append((str(k.get("label") or ""), str(k.get("value") or "—"),
                              rgb, str(k.get("subtitle") or "")))
            else:
                label, value, rgb = k[0], k[1], (k[2] if len(k) > 2 else self.accent)
                items.append((str(label), str(value), rgb, ""))
        if not items:
            return
        n = len(items)
        cols = per_row or (n if n <= 5 else 4)
        gap = 3.6
        cw = self.content_w
        for start in range(0, n, cols):
            chunk = items[start:start + cols]
            row_cols = cols if n > cols else len(chunk)
            card_w = (cw - gap * (row_cols - 1)) / row_cols
            if not self._in_column:
                card_w = min(card_w, 48.0)
            has_sub = any(c[3] for c in chunk)
            card_h = 23.0 if has_sub else 19.6
            self._check_page_space(card_h + 4)
            y = self.get_y()
            for i, (label, value, rgb, sub) in enumerate(chunk):
                x = self.l_margin + i * (card_w + gap)
                self.set_draw_color(*COLOR_HAIRLINE)
                self.set_line_width(0.3)
                self.rect(x, y, card_w, card_h, "D", round_corners=True, corner_radius=2.2)
                # label (shrink-to-fit before truncating)
                lbl = _safe(label.upper())
                lbl_size = 6.1
                self._sans("B", lbl_size)
                spacing_mm = 0.14
                while lbl_size > 5.0 and \
                        self.get_string_width(lbl) + spacing_mm * max(len(lbl) - 1, 0) > card_w - 8:
                    lbl_size -= 0.3
                    self._sans("B", lbl_size)
                if self.get_string_width(lbl) + spacing_mm * max(len(lbl) - 1, 0) > card_w - 8:
                    lbl = self._fit(lbl, card_w - 8 - spacing_mm * len(lbl))
                self.set_text_color(*COLOR_MUTED)
                try:
                    self.set_char_spacing(0.4)
                except Exception:
                    pass
                self.set_xy(x + 4, y + 3.1)
                self.cell(card_w - 8, 3.2, lbl)
                try:
                    self.set_char_spacing(0)
                except Exception:
                    pass
                # value (auto-shrink)
                size = 14.5
                self._sans("B", size)
                vtxt = _safe(value)
                while size > 9 and self.get_string_width(vtxt) > card_w - 8:
                    size -= 0.5
                    self._sans("B", size)
                self.set_text_color(*COLOR_TEXT)
                self.set_xy(x + 4, y + 7.4)
                self.cell(card_w - 8, 6.6, vtxt)
                # accent bar
                self.set_fill_color(*rgb)
                self.rect(x + 4, y + 15.1, 7, 1.05, "F", round_corners=True, corner_radius=0.5)
                if sub:
                    self._sans("", 6.2)
                    self.set_text_color(*COLOR_MUTED)
                    self.set_xy(x + 4, y + 17.6)
                    self.cell(card_w - 8, 3.4, self._fit(_safe(sub), card_w - 8))
            self.set_y(y + card_h + 3.6)

    # ----- tables -----

    def _infer_styles(self, headers: list[str], rows: list[list[str]],
                      col_styles: list | None) -> list[str]:
        # 'pct' (a plain percentage) lays out like any right-aligned number.
        return ["num" if s == "pct" else s
                for s in infer_column_styles(headers, rows, col_styles)]

    def _cell_font(self, style: str):
        if style == "mono":
            self._mono("", 7.0)
        elif style in ("status", "severity"):
            self._sans("B", 6.0)
        else:
            self._sans("", 7.6)

    def _cell_nat_width(self, style: str, txt: str) -> float:
        self._cell_font(style)
        if style in ("status", "severity"):
            return self.get_string_width(txt.upper()) + 7.4
        if style == "pct-bar":
            return self.get_string_width(txt) + 18.5
        return self.get_string_width(txt) + 4.6

    def _auto_widths(self, headers, rows, styles) -> list[float]:
        cw = self.content_w
        naturals = []
        for ci, h in enumerate(headers):
            self._sans("B", 6.8)
            w_h = self.get_string_width(h.upper()) + 5.6
            samples = sorted(
                self._cell_nat_width(styles[ci], r[ci]) for r in rows[:60] if ci < len(r)
            ) or [10]
            w_c = samples[min(int(len(samples) * 0.95), len(samples) - 1)]
            lo = 15 if styles[ci] != "pct-bar" else 26
            naturals.append(max(min(max(w_h, w_c), 78), lo))
        total = sum(naturals)
        if total > cw:
            # shrink flexible text columns first
            flex = [i for i, s in enumerate(styles) if s in ("text", "mono")]
            excess = total - cw
            flex_w = sum(naturals[i] for i in flex) or 1
            for i in flex:
                naturals[i] = max(16.0, naturals[i] - excess * naturals[i] / flex_w)
            total = sum(naturals)
            if total > cw:
                naturals = [w * cw / total for w in naturals]
        elif total < cw:
            grow = [i for i, s in enumerate(styles) if s in ("text", "mono")] or list(range(len(naturals)))
            extra = (cw - total) / len(grow)
            for i in grow:
                naturals[i] += extra
        return naturals

    def _pill(self, x: float, y: float, w_cell: float, row_h: float, raw: str):
        label = _safe(raw.strip())
        kind = _PILL_VOCAB.get(label.lower(), "neutral")
        tc, bg = _PILL_KINDS[kind]
        self._sans("B", 6.0)
        label = self._fit(label.upper(), w_cell - 6)
        pw = min(self.get_string_width(label) + 4.8, w_cell - 1.2)
        ph = 4.0
        py = y + (row_h - ph) / 2
        self.set_fill_color(*bg)
        self.rect(x, py, pw, ph, "F", round_corners=True, corner_radius=2.0)
        self.set_text_color(*tc)
        self.set_xy(x, py - 0.05)
        self.cell(pw, ph + 0.1, label, align="C")

    def _pct_cell(self, x: float, y: float, w: float, row_h: float, raw: str):
        bar_w = 13.5
        txt_w = w - bar_w - 4.6
        self._sans("", 7.6)
        self.set_text_color(*COLOR_BODY)
        self.set_xy(x, y)
        self.cell(txt_w, row_h, self._fit(_safe(raw), txt_w), align="R")
        m = re.match(r"\s*([\d.]+)\s*%", raw)
        if m:
            try:
                pct = min(max(float(m.group(1)), 0.0), 100.0)
            except ValueError:
                return
            bx = x + txt_w + 2.4
            by = y + (row_h - 1.6) / 2
            self.set_fill_color(*COLOR_HAIRLINE_SOFT)
            self.rect(bx, by, bar_w, 1.6, "F", round_corners=True, corner_radius=0.8)
            if pct > 0:
                self.set_fill_color(*self.accent)
                self.rect(bx, by, max(bar_w * pct / 100, 0.9), 1.6, "F",
                          round_corners=True, corner_radius=0.8)

    def data_table(self, headers: list[str], rows: list[list[str]],
                   col_widths: list[float] | None = None,
                   max_rows: int = 200,
                   col_styles: list | None = None):
        """Document-grade table: uppercase header with strong rule, hairline row
        separators, style-aware cells (num right-aligned, status pills, inline
        percentage bars, mono identifiers), header repeated across page breaks."""
        headers = [_safe(str(h)) for h in headers]
        rows = [["" if c is None else _safe(str(c)) for c in r] for r in rows]
        if not rows:
            self.empty_state("No records in this window.")
            return
        styles = self._infer_styles(headers, rows, col_styles)
        if col_widths:
            k = self.content_w / sum(col_widths)
            widths = [w * k for w in col_widths]
        else:
            widths = self._auto_widths(headers, rows, styles)

        x0 = self.l_margin
        header_h = 6.2
        row_h = 6.0
        shown = rows[:max_rows]

        def draw_header():
            y = self.get_y()
            self._sans("B", 6.8)
            self.set_text_color(71, 85, 105)
            try:
                self.set_char_spacing(0.3)
            except Exception:
                pass
            cx = x0
            for ci, h in enumerate(headers):
                align = "R" if styles[ci] in ("num", "pct", "pct-bar") else "L"
                pad_r = 2.2 if align == "R" and styles[ci] == "num" else 0
                self.set_xy(cx, y)
                self.cell(widths[ci] - pad_r, header_h, self._fit(h.upper(), widths[ci] - 2.4),
                          align=align)
                cx += widths[ci]
            try:
                self.set_char_spacing(0)
            except Exception:
                pass
            self._hairline(x0, y + header_h, x0 + sum(widths), (71, 85, 105), 0.45)
            self.set_y(y + header_h + 0.9)

        self._check_page_space(header_h + row_h * min(len(shown), 3) + 6)
        draw_header()
        for row in shown:
            if self.get_y() + row_h > self.page_break_trigger:
                self.add_page()
                draw_header()
            y = self.get_y()
            cx = x0
            for ci in range(len(headers)):
                val = row[ci] if ci < len(row) else ""
                st = styles[ci]
                w = widths[ci]
                if st in ("status", "severity"):
                    if val.strip() and val.strip().lower() not in _NEUTRAL_CELL:
                        self._pill(cx + 0.4, y, w, row_h, val)
                    else:
                        self._sans("", 7.6)
                        self.set_text_color(*COLOR_FAINT)
                        self.set_xy(cx, y)
                        self.cell(w, row_h, "—" if UNICODE_FONTS else "-")
                elif st == "pct-bar":
                    self._pct_cell(cx, y, w, row_h, val)
                elif st in ("num", "pct"):
                    self._sans("", 7.6)
                    self.set_text_color(*COLOR_BODY)
                    self.set_xy(cx, y)
                    self.cell(w - 2.2, row_h, self._fit(val, w - 3.4), align="R")
                elif st == "mono":
                    self._mono("", 7.0)
                    self.set_text_color(*COLOR_BODY)
                    self.set_xy(cx + 0.4, y)
                    self.cell(w - 0.4, row_h, self._fit(val, w - 2.6))
                else:
                    self._sans("", 7.6)
                    self.set_text_color(*COLOR_BODY)
                    self.set_xy(cx + 0.4, y)
                    self.cell(w - 0.4, row_h, self._fit(val, w - 2.6))
                cx += w
            self._hairline(x0, y + row_h, x0 + sum(widths), COLOR_HAIRLINE_SOFT, 0.18)
            self.set_y(y + row_h + 0.35)
        if len(rows) > max_rows:
            self._sans("I", 6.8)
            self.set_text_color(*COLOR_FAINT)
            self.set_x(x0)
            self.cell(self.content_w, 4.4,
                      _safe(f"Showing the first {max_rows:,} of {len(rows):,} rows; "
                            "the Excel (XLSX) export lists every row."))
            self.ln(4.6)
        self.ln(2.6)

    # ----- charts -----

    def add_chart(self, chart_png: bytes, w: float | None = None,
                  caption: str = "", title: str = ""):
        """Place a chart PNG at its exact physical width (or scaled to w)."""
        if not chart_png:
            return
        pw, ph = _png_size(chart_png)
        intrinsic_w = pw / _CHART_DPI * 25.4
        w = min(w or intrinsic_w, self.content_w)
        h = w * ph / pw
        block = h + (6.2 if title else 0) + (4.6 if caption else 0) + 2
        self._check_page_space(min(block, 190))
        if title:
            self._sans("B", 8.4)
            self.set_text_color(*COLOR_TEXT)
            self.set_x(self.l_margin)
            self.cell(self.content_w, 4.6, self._fit(_safe(title), self.content_w))
            self.ln(5.8)
        x = self.l_margin + (self.content_w - w) / 2
        self.image(io.BytesIO(chart_png), x=x, y=self.get_y(), w=w)
        self.set_y(self.get_y() + h + 1.2)
        if caption:
            self._sans("I", 6.8)
            self.set_text_color(*COLOR_FAINT)
            self.set_x(self.l_margin)
            self.cell(self.content_w, 4, _safe(caption), align="C")
            self.ln(4.6)
        else:
            self.ln(1.2)

    def add_chart_pair(self, left_png: bytes, right_png: bytes,
                       left_title: str = "", right_title: str = ""):
        """Two figures side by side on a shared baseline."""
        gap = 6
        col_w = (self.content_w - gap) / 2
        heights = []
        for png in (left_png, right_png):
            pw, ph = _png_size(png)
            heights.append(col_w * ph / pw)
        titled = bool(left_title or right_title)
        block = max(heights) + (6 if titled else 0) + 3
        self._check_page_space(min(block, 190))
        y0 = self.get_y()
        for i, (png, title) in enumerate(((left_png, left_title), (right_png, right_title))):
            x = self.l_margin + i * (col_w + gap)
            y = y0
            if titled:
                self._sans("B", 8.4)
                self.set_text_color(*COLOR_TEXT)
                self.set_xy(x, y)
                self.cell(col_w, 4.6, self._fit(_safe(title or ""), col_w))
                y += 6
            self.image(io.BytesIO(png), x=x, y=y, w=col_w)
        self.set_y(y0 + (6 if titled else 0) + max(heights) + 3)

    # legacy alias
    def add_donut_pair(self, left_png: bytes, right_png: bytes,
                       left_caption: str = "", right_caption: str = ""):
        self.add_chart_pair(left_png, right_png, left_caption, right_caption)

    def status_badge(self, status: str, x: float | None = None, y: float | None = None):
        """Inline status pill at the current (or given) position."""
        if x is None or y is None:
            x, y = self.get_x(), self.get_y()
        self._pill(x, y, 30, 5, status)
        self.set_text_color(*COLOR_TEXT)

    # ----- device/service spec strip -----

    def spec_strip(self, pairs: list[tuple[str, str]]):
        """One-line label/value strip: LABEL value   LABEL value ..."""
        self._check_page_space(8)
        x = self.l_margin
        y = self.get_y()
        for label, value in pairs:
            self._sans("", 6.3)
            self.set_text_color(*COLOR_MUTED)
            lw = self.get_string_width(label.upper()) + 1.6
            self.set_xy(x, y + 0.55)
            self.cell(lw, 3.6, _safe(label.upper()))
            x += lw
            self._sans("B", 8.0)
            self.set_text_color(*COLOR_TEXT)
            vw = self.get_string_width(_safe(value)) + 6.5
            self.set_xy(x, y)
            self.cell(vw, 4.6, _safe(value))
            x += vw
            if x > self.l_margin + self.content_w - 24:
                break
        self.set_y(y + 6.4)

    # ----- columns (half-width sections) -----

    def begin_column(self, x: float, w: float):
        self._saved_lr = (self.l_margin, self.r_margin)
        self._saved_apb = self.auto_page_break
        self.set_auto_page_break(False)
        self.set_left_margin(x)
        self.set_right_margin(self.page_w - x - w)
        self.set_x(x)
        self._in_column = True

    def end_column(self):
        self.set_left_margin(self._saved_lr[0])
        self.set_right_margin(self._saved_lr[1])
        self.set_auto_page_break(self._saved_apb, MARGIN_B)
        self._in_column = False

    def _check_page_space(self, needed_mm: float):
        if self._in_column:
            return
        if self.get_y() + needed_mm > self.page_break_trigger:
            self.add_page()


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

async def _fetch_company_info(db: AsyncSession) -> dict:
    """Company name, light logo (bytes + sniffed MIME) and timezone.

    Kept for older callers; it reads the same sanitised values as every
    renderer (app.services.branding.load_branding), so an empty stored name
    falls back to the product name and an unreadable logo is dropped."""
    from app.services.branding import load_branding
    branding = await load_branding(db)
    logo = branding.logo
    return {
        "company_name": branding.display_name,
        "logo_bytes": logo.data if logo else None,
        "logo_mime": logo.content_type if logo else None,
        "timezone": branding.timezone,
        "branding": branding,
    }


def _build_device_filter_sql(device_ids: list[str] | None,
                             group_ids: list[str] | None,
                             alias: str = "d",
                             locations: list[str] | None = None,
                             device_types: list[str] | None = None) -> tuple[str, dict]:
    """Build WHERE clause fragments and params for device filtering."""
    clauses = []
    params: dict = {}
    if device_ids:
        clauses.append(f"{alias}.id = ANY(:device_ids)")
        params["device_ids"] = device_ids
    if group_ids:
        clauses.append(f"{alias}.group_id = ANY(:group_ids)")
        params["group_ids"] = group_ids
    if locations:
        clauses.append(f"{alias}.location = ANY(:locations)")
        params["locations"] = locations
    if device_types:
        clauses.append(f"{alias}.device_type = ANY(:device_types)")
        params["device_types"] = device_types
    where = " AND ".join(clauses)
    return where, params


def _ch_device_filter(device_ids: list[str] | None) -> str:
    if not device_ids:
        return ""
    ids = ", ".join(f"'{d}'" for d in device_ids)
    return f"AND device_id IN ({ids})"


def _ch_service_filter(service_ids: list[str] | None) -> str:
    if not service_ids:
        return ""
    ids = ", ".join(f"'{s}'" for s in service_ids)
    return f"AND service_check_id IN ({ids})"


async def _fetch_devices(db: AsyncSession,
                         device_ids: list[str] | None = None,
                         group_ids: list[str] | None = None,
                         locations: list[str] | None = None,
                         device_types: list[str] | None = None,
                         visible_tags: list[str] | None = None) -> list[dict]:
    filt, params = _build_device_filter_sql(device_ids, group_ids, "d",
                                            locations=locations, device_types=device_types)
    if visible_tags is not None:
        filt = " AND ".join(c for c in (filt, scoping.jsonb_tags_visible("d.tags")) if c)
        params[scoping.SCOPE_PARAM] = visible_tags
    where = f"WHERE {filt}" if filt else ""
    q = text(f"""
        SELECT d.id, d.hostname, d.ip_address, d.device_type, d.location,
               d.status, d.last_seen, d.last_rtt_ms, d.ping_interval,
               dg.name AS group_name, dg.color AS group_color
        FROM devices d
        LEFT JOIN device_groups dg ON dg.id = d.group_id
        {where}
        ORDER BY d.hostname
    """)
    result = await db.execute(q, params)
    rows = result.fetchall()
    return [dict(r._mapping) for r in rows]


async def _fetch_service_checks(db: AsyncSession,
                                visible_tags: list[str] | None = None) -> list[dict]:
    where, params = "", {}
    if visible_tags is not None:
        where = f"WHERE {scoping.text_tags_visible('sc.tags')}"
        params[scoping.SCOPE_PARAM] = visible_tags
    q = text(f"""
        SELECT sc.id, sc.name, sc.check_type, sc.target_host, sc.target_url, sc.status,
               sc.last_check_at, sc.last_response_ms, sc.last_error,
               sc.tls_days_remaining, sc.tls_expiry_date,
               sc.tls_warn_days, sc.tls_critical_days, sc.enabled,
               sc.created_at, sc.check_interval, sc.group_id,
               scg.name AS group_name,
               sc.sla_target_pct, sc.criticality, sc.business_owner, sc.tags
        FROM service_checks sc
        LEFT JOIN service_check_groups scg ON scg.id = sc.group_id
        {where}
        ORDER BY sc.name
    """)
    result = await db.execute(q, params)
    return [dict(r._mapping) for r in result.fetchall()]


async def _fetch_alerts(db: AsyncSession, start: datetime, end: datetime,
                        device_ids: list[str] | None = None,
                        visible_tags: list[str] | None = None,
                        *, redact_netflow: bool = False) -> list[dict]:
    """Alerts triggered in the window. ``metadata`` carries the alert's
    ``source`` only. With ``redact_netflow`` (the report's requester lacks
    netflow.view) a NetFlow alert's flow-derived message is replaced the way
    the Alert Center does it (``netflow_alert_service.redact_alert_payload``),
    so no report table, sample or grouping key can show it."""
    dev_clause = ""
    params: dict = {"start": start, "end": end}
    if device_ids:
        dev_clause = "AND a.device_id = ANY(:device_ids)"
        params["device_ids"] = device_ids
    if visible_tags is not None:
        dev_clause += f" AND {scoping.alert_visible('a')}"
        params[scoping.SCOPE_PARAM] = visible_tags
    q = text(f"""
        SELECT a.id, a.device_id, a.service_check_id, a.status, a.severity, a.message,
               a.triggered_at, a.acknowledged_at, a.resolved_at,
               d.hostname,
               jsonb_build_object('source', a.metadata->'source') AS metadata
        FROM alerts a
        LEFT JOIN devices d ON d.id = a.device_id
        WHERE a.triggered_at >= :start AND a.triggered_at <= :end
        {dev_clause}
        ORDER BY a.triggered_at DESC
    """)
    result = await db.execute(q, params)
    rows = [dict(r._mapping) for r in result.fetchall()]
    if redact_netflow:
        from app.services.netflow_alert_service import redact_alert_payload

        rows = [redact_alert_payload(r) for r in rows]
    return rows


def _fetch_ping_metrics(start: datetime, end: datetime,
                        device_ids: list[str] | None = None) -> list[dict]:
    client = get_ch_client()
    dev_filt = _ch_device_filter(device_ids)
    q = f"""
        SELECT device_id, timestamp, rtt_ms, packet_loss, jitter_ms, is_up,
               min_rtt_ms, max_rtt_ms
        FROM zenplus.ping_metrics
        WHERE timestamp >= %(start)s AND timestamp <= %(end)s
        {dev_filt}
        ORDER BY timestamp
    """
    result = client.query(q, parameters={"start": start, "end": end})
    cols = result.column_names
    return [dict(zip(cols, row)) for row in result.result_rows]


def _to_naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


async def _fetch_device_maintenance_windows(
    db: AsyncSession, start: datetime, end: datetime,
) -> dict[str, list[tuple[datetime, datetime]]]:
    """device_id -> [(start, end), ...] maintenance intervals overlapping the
    report range, clamped to it. Naive-UTC datetimes to match ClickHouse rows.
    Tolerates the table not existing yet (pre-migrate-053 appliances)."""
    try:
        rows = (await db.execute(
            text("""
                SELECT d.id AS device_id,
                       GREATEST(m.starts_at, :start_ts) AS s,
                       LEAST(m.ends_at, :end_ts) AS e
                FROM device_maintenance m
                JOIN devices d ON (
                       (m.scope_type = 'device' AND m.scope_device_id = d.id)
                    OR (m.scope_type = 'group'  AND m.scope_group_id = d.group_id)
                    OR (m.scope_type = 'tag'    AND jsonb_exists(COALESCE(d.tags, '[]'::jsonb), m.scope_tag))
                    OR (m.scope_type = 'all')
                )
                WHERE m.starts_at < :end_ts AND m.ends_at > :start_ts
            """),
            {
                "start_ts": start if start.tzinfo else start.replace(tzinfo=timezone.utc),
                "end_ts": end if end.tzinfo else end.replace(tzinfo=timezone.utc),
            },
        )).all()
    except Exception:
        return {}
    out: dict[str, list[tuple[datetime, datetime]]] = {}
    for r in rows:
        out.setdefault(str(r.device_id), []).append((_to_naive_utc(r.s), _to_naive_utc(r.e)))
    return out


def _exclude_maintenance_samples(
    ping_rows: list[dict],
    windows: dict[str, list[tuple[datetime, datetime]]],
) -> list[dict]:
    """Drop ping samples inside maintenance windows so uptime %, outage
    episodes and RTT stats reflect only SLA-relevant time."""
    if not windows:
        return ping_rows
    out = []
    for r in ping_rows:
        wins = windows.get(str(r["device_id"]))
        if wins:
            ts = _to_naive_utc(r["timestamp"]) if isinstance(r["timestamp"], datetime) else r["timestamp"]
            if any(s <= ts <= e for s, e in wins):
                continue
        out.append(r)
    return out


def _fetch_service_metrics(start: datetime, end: datetime,
                           service_ids: list[str] | None = None) -> list[dict]:
    client = get_ch_client()
    svc_filt = _ch_service_filter(service_ids)
    q = f"""
        SELECT service_check_id, timestamp, response_ms, is_up, status_code, error_message
        FROM zenplus.service_metrics
        WHERE timestamp >= %(start)s AND timestamp <= %(end)s
        {svc_filt}
        ORDER BY timestamp
    """
    result = client.query(q, parameters={"start": start, "end": end})
    cols = result.column_names
    return [dict(zip(cols, row)) for row in result.result_rows]


def _fetch_device_status_log(start: datetime, end: datetime,
                             device_ids: list[str] | None = None) -> list[dict]:
    client = get_ch_client()
    dev_filt = _ch_device_filter(device_ids)
    q = f"""
        SELECT device_id, timestamp, old_status, new_status, reason, duration_sec
        FROM zenplus.device_status_log
        WHERE timestamp >= %(start)s AND timestamp <= %(end)s
        {dev_filt}
        ORDER BY timestamp
    """
    try:
        result = client.query(q, parameters={"start": start, "end": end})
        cols = result.column_names
        return [dict(zip(cols, row)) for row in result.result_rows]
    except Exception:
        # Table may not exist on older deployments — return empty list so
        # report sections render an empty state instead of 500-ing.
        return []


def _fetch_service_status_log(start: datetime, end: datetime,
                              service_ids: list[str] | None = None) -> list[dict]:
    client = get_ch_client()
    svc_filt = _ch_service_filter(service_ids)
    q = f"""
        SELECT service_check_id, timestamp, old_status, new_status, reason, duration_sec
        FROM zenplus.service_status_log
        WHERE timestamp >= %(start)s AND timestamp <= %(end)s
        {svc_filt}
        ORDER BY timestamp
    """
    try:
        result = client.query(q, parameters={"start": start, "end": end})
        cols = result.column_names
        return [dict(zip(cols, row)) for row in result.result_rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Metric aggregation helpers
# ---------------------------------------------------------------------------

def _device_uptime_pct(ping_rows: list[dict], device_id: str) -> float | None:
    """None when unmeasured: no samples is not zero availability."""
    rows = [r for r in ping_rows if str(r["device_id"]) == str(device_id)]
    if not rows:
        return None
    up = sum(1 for r in rows if r.get("is_up"))
    return (up / len(rows)) * 100


def _ping_outage_episodes(ping_rows: list[dict], device_id: str) -> int:
    """Count consecutive is_up=false spans — aligned with sample-based uptime %."""
    rows = sorted(
        [r for r in ping_rows if str(r["device_id"]) == str(device_id)],
        key=lambda r: r["timestamp"],
    )
    if not rows:
        return 0
    episodes = 0
    in_outage = False
    for r in rows:
        up = bool(r.get("is_up"))
        if not up:
            if not in_outage:
                episodes += 1
                in_outage = True
        else:
            in_outage = False
    return episodes


def _device_rtt_stats(ping_rows: list[dict], device_id: str) -> dict:
    rows = [r for r in ping_rows if str(r["device_id"]) == str(device_id) and r.get("rtt_ms") is not None]
    rtts = [r["rtt_ms"] for r in rows if r["rtt_ms"] is not None and r["rtt_ms"] > 0]
    if not rtts:
        return {"avg": None, "min": None, "max": None, "p95": None}
    rtts_sorted = sorted(rtts)
    p95_idx = int(len(rtts_sorted) * 0.95)
    return {
        "avg": statistics.mean(rtts),
        "min": min(rtts),
        "max": max(rtts),
        "p95": rtts_sorted[min(p95_idx, len(rtts_sorted) - 1)],
    }


def _service_uptime_pct(svc_rows: list[dict], svc_id: str) -> float | None:
    rows = [r for r in svc_rows if str(r["service_check_id"]) == str(svc_id)]
    if not rows:
        return None
    up = sum(1 for r in rows if r.get("is_up"))
    return (up / len(rows)) * 100


def _mttr_seconds(alerts: list[dict]) -> float | None:
    """Mean Time To Resolve for resolved alerts."""
    durations = []
    for a in alerts:
        if a.get("resolved_at") and a.get("triggered_at"):
            d = (a["resolved_at"] - a["triggered_at"]).total_seconds()
            if d > 0:
                durations.append(d)
    if not durations:
        return None
    return statistics.mean(durations)


# ---------------------------------------------------------------------------
# Report section builders (legacy report types)
# ---------------------------------------------------------------------------

def _avail_accent(pct: float | None, target: float = 99.0) -> str:
    if pct is None:
        return "neutral"
    if pct >= 99.9:
        return "success"
    if pct >= target:
        return "warning"
    return "danger"


def _hourly_availability(ping_rows: list[dict]) -> tuple[list[datetime], list[float]]:
    """Bucket raw ping samples into an hourly fleet-availability series."""
    buckets: dict[datetime, list[int]] = {}
    for r in ping_rows:
        ts = r.get("timestamp")
        if not hasattr(ts, "replace"):
            continue
        key = ts.replace(minute=0, second=0, microsecond=0)
        b = buckets.setdefault(key, [0, 0])
        b[1] += 1
        if r.get("is_up"):
            b[0] += 1
    keys = sorted(buckets)
    return keys, [buckets[k][0] / buckets[k][1] * 100 for k in keys if buckets[k][1]]


def _device_block_header(pdf: ZenPlusReport, name: str, status: str, meta_line: str):
    """Compact per-item header: bold name + status pill, muted meta line."""
    pdf._check_page_space(36)
    x = pdf.l_margin
    y = pdf.get_y()
    pdf._sans("B", 9.8)
    pdf.set_text_color(*COLOR_TEXT)
    name_fit = pdf._fit(_safe(name), pdf.content_w - 30)
    pdf.set_xy(x, y)
    pdf.cell(pdf.get_string_width(name_fit) + 2, 5.4, name_fit)
    if status:
        pdf._pill(x + pdf.get_string_width(name_fit) + 4, y + 0.55, 28, 4.4, status)
    if meta_line:
        pdf._sans("", 7.2)
        pdf.set_text_color(*COLOR_MUTED)
        pdf.set_xy(x, y + 5.8)
        pdf.cell(pdf.content_w, 3.9, pdf._fit(_safe(meta_line), pdf.content_w))
        pdf.set_y(y + 11.2)
    else:
        pdf.set_y(y + 6.8)


def _block_separator(pdf: ZenPlusReport):
    pdf.ln(1.8)
    if pdf.get_y() < pdf.page_break_trigger - 6:
        pdf._hairline(pdf.l_margin, pdf.get_y(), pdf.l_margin + pdf.content_w,
                      COLOR_HAIRLINE_SOFT, 0.2)
    pdf.ln(3.4)


_STATUS_LOG_HEADERS = ["Time", "From", "To", "Reason", "Duration"]
_STATUS_LOG_STYLES = ["num", "status", "status", "text", "num"]


def _pdf_tz(pdf: ZenPlusReport):
    """The document's organisation timezone (tzinfo), or None for UTC."""
    b = getattr(pdf, "branding", None)
    return getattr(b, "tz", None) if b is not None else None


def _fmt_when(pdf_or_branding, ts, short: bool = False) -> str:
    """A timestamp in the organisation timezone and date/time format
    (naive values are UTC)."""
    b = getattr(pdf_or_branding, "branding", pdf_or_branding)
    if not isinstance(ts, datetime):
        return str(ts)[:16] if ts else "—"
    if b is not None and hasattr(b, "fmt_datetime"):
        return b.fmt_short(ts) if short else b.fmt_datetime(ts)
    return ts.strftime("%m-%d %H:%M" if short else "%Y-%m-%d %H:%M")


def _status_log_rows(entries: list[dict], id_key: str | None = None,
                     limit: int = 10, branding=None) -> list[list[str]]:
    rows = []
    for e in entries[:limit]:
        ts = e.get("timestamp")
        rows.append([
            _fmt_when(branding, ts),
            (e.get("old_status") or "—"),
            (e.get("new_status") or "—"),
            (e.get("reason") or "—")[:70],
            _fmt_duration(e.get("duration_sec")),
        ])
    return rows


async def _build_executive_summary(pdf: ZenPlusReport, db: AsyncSession,
                                   start: datetime, end: datetime,
                                   device_ids: list[str] | None,
                                   group_ids: list[str] | None):
    devices = await _fetch_devices(db, device_ids, group_ids)
    services = await _fetch_service_checks(db)
    filtered_device_ids = [str(d["id"]) for d in devices] if devices else device_ids
    alerts = await _fetch_alerts(db, start, end, filtered_device_ids)
    maint = await _fetch_device_maintenance_windows(db, start, end)
    ping_rows = _exclude_maintenance_samples(
        _fetch_ping_metrics(start, end, filtered_device_ids), maint)

    pdf.add_page()
    pdf.section_title(
        "Executive Summary", category="Overview",
        description="Availability, alert load and response performance across the "
                    "monitored estate for the reporting period.")

    # --- KPI cards ---
    total_devices = len(devices)
    online_devices = sum(1 for d in devices
                         if (d.get("status") or "").lower() in ("up", "online"))
    up_samples = sum(1 for r in ping_rows if r.get("is_up"))
    fleet_avail = (up_samples / len(ping_rows) * 100) if ping_rows else None

    all_rtts = [r["rtt_ms"] for r in ping_rows if r.get("rtt_ms") and r["rtt_ms"] > 0]
    avg_rtt = statistics.mean(all_rtts) if all_rtts else None

    total_alerts = len(alerts)
    critical_alerts = sum(1 for a in alerts if (a.get("severity") or "").lower() == "critical")
    mttr = _mttr_seconds(alerts)

    pdf.kpi_row([
        {"label": "Devices monitored", "value": f"{total_devices:,}", "accent": "primary"},
        {"label": "Online now", "value": f"{online_devices} of {total_devices}",
         "accent": "success" if online_devices == total_devices else
                   ("warning" if total_devices and online_devices >= total_devices * 0.9 else "danger")},
        {"label": "Fleet availability", "value": _fmt_pct(fleet_avail) if fleet_avail is not None else "—",
         "accent": _avail_accent(fleet_avail)},
        {"label": "Alerts triggered", "value": f"{total_alerts:,}",
         "accent": "success" if total_alerts == 0 else ("danger" if critical_alerts else "warning")},
        {"label": "Mean time to resolve", "value": _fmt_duration(mttr), "accent": "info"},
    ])
    pdf.ln(1)

    # --- Status distribution donuts ---
    dev_status_counts: dict[str, int] = {}
    for d in devices:
        s = (d.get("status") or "unknown").lower()
        dev_status_counts[s] = dev_status_counts.get(s, 0) + 1
    online_pct = (online_devices / total_devices * 100) if total_devices else 0
    dev_labels = [l.capitalize() for l in dev_status_counts]
    dev_colors = [STATUS_COLORS_HEX.get(l, HEX_FAINT) for l in dev_status_counts]
    dev_png = _make_donut(dev_labels, list(dev_status_counts.values()), dev_colors,
                          center_text=f"{online_pct:.0f}%", center_sub="online")

    svc_status_counts: dict[str, int] = {}
    for s in services:
        st = (s.get("status") or "unknown").lower()
        svc_status_counts[st] = svc_status_counts.get(st, 0) + 1
    svc_up = sum(1 for s in services if (s.get("status") or "").lower() in ("up", "online"))
    svc_pct = (svc_up / len(services) * 100) if services else 0
    svc_png = _make_donut([l.capitalize() for l in svc_status_counts],
                          list(svc_status_counts.values()),
                          [STATUS_COLORS_HEX.get(l, HEX_FAINT) for l in svc_status_counts],
                          center_text=f"{svc_pct:.0f}%" if services else "",
                          center_sub="passing" if services else "",
                          empty_label="No service checks configured")
    pdf.add_chart_pair(dev_png, svc_png, "Device status", "Service checks")

    # --- Fleet availability trend ---
    if ping_rows:
        ts, vals = _hourly_availability(ping_rows)
        if len(ts) >= 3:
            png = _make_line_chart(ts, vals, ylabel="availability %", color=HEX_SUCCESS,
                                   h_mm=42, unit="%", y_domain=(min(vals) - 2, 100),
                                   tz=_pdf_tz(pdf))
            pdf.add_chart(png, title="Fleet availability trend")

    # --- Top problematic devices ---
    pdf.section_title(
        "Top Problematic Devices", category="Risk",
        description="Ranked by alert volume and accumulated downtime; use this list "
                    "to prioritise remediation.")
    device_alert_count: dict[str, int] = {}
    for a in alerts:
        did = str(a.get("device_id", ""))
        device_alert_count[did] = device_alert_count.get(did, 0) + 1

    # Downtime from paired down→recovery transitions: the status log's
    # duration_sec column is always 0.
    from app.services.report_data_service import device_outage_episodes
    device_downtime: dict[str, float] = {}
    try:
        for e in device_outage_episodes(start, end, [str(d["id"]) for d in devices], maint):
            device_downtime[e["entity_id"]] = device_downtime.get(e["entity_id"], 0) + e["in_window_s"]
    except Exception:
        logging.getLogger(__name__).warning("outage episodes unavailable", exc_info=True)

    problem_scores: dict[str, float] = {}
    for did in (str(d["id"]) for d in devices):
        score = device_alert_count.get(did, 0) * 10 + device_downtime.get(did, 0) / 60
        if score > 0:
            problem_scores[did] = score

    top5 = sorted(problem_scores.items(), key=lambda x: x[1], reverse=True)[:5]
    if top5:
        dev_map = {str(d["id"]): d for d in devices}
        rows = []
        for did, _score in top5:
            d = dev_map.get(did, {})
            uptime = _device_uptime_pct(ping_rows, did)
            stats = _device_rtt_stats(ping_rows, did)
            rows.append([
                d.get("hostname", "—"),
                d.get("ip_address", "—"),
                (d.get("status") or "—"),
                _fmt_pct(uptime),
                str(device_alert_count.get(did, 0)),
                _fmt_duration(device_downtime.get(did, 0)),
                _fmt_ms(stats["avg"]),
            ])
        pdf.data_table(
            ["Device", "IP address", "Status", "Availability", "Alerts", "Downtime", "Avg RTT"],
            rows,
            col_styles=["text", "mono", "status", "pct-bar", "num", "num", "num"],
        )
    else:
        pdf.empty_state("No problem devices — no alerts or logged downtime in this period.")

    # --- Alert summary ---
    pdf.section_title("Alert Summary", category="Alerting",
                      description="Alert volume by severity for the reporting period.")
    if not alerts:
        pdf.empty_state("No alerts were triggered in this period.")
        return
    sev_counts: dict[str, int] = {}
    for a in alerts:
        sev = (a.get("severity") or "info").lower()
        sev_counts[sev] = sev_counts.get(sev, 0) + 1
    pdf.data_table(
        ["Severity", "Alerts", "Share of total"],
        [[sev.capitalize(), f"{sev_counts.get(sev, 0):,}",
          _fmt_pct(sev_counts.get(sev, 0) / max(total_alerts, 1) * 100)]
         for sev in ("critical", "warning", "info")],
        col_styles=["severity", "num", "pct-bar"],
    )


async def _build_device_health(pdf: ZenPlusReport, db: AsyncSession,
                               start: datetime, end: datetime,
                               device_ids: list[str] | None,
                               group_ids: list[str] | None):
    devices = await _fetch_devices(db, device_ids, group_ids)
    filtered_device_ids = [str(d["id"]) for d in devices] if devices else device_ids
    ping_rows = _exclude_maintenance_samples(
        _fetch_ping_metrics(start, end, filtered_device_ids),
        await _fetch_device_maintenance_windows(db, start, end),
    )
    status_log = _fetch_device_status_log(start, end, filtered_device_ids)

    pdf.add_page()
    pdf.section_title(
        "Device Health", category="Availability",
        description="Availability, latency and stability for every monitored device.")

    if not devices:
        pdf.empty_state("No devices match the report filters.")
        return

    # --- Fleet KPIs ---
    up_samples = sum(1 for r in ping_rows if r.get("is_up"))
    fleet_avail = (up_samples / len(ping_rows) * 100) if ping_rows else None
    online_now = sum(1 for d in devices if (d.get("status") or "").lower() in ("up", "online"))
    all_rtts = [r["rtt_ms"] for r in ping_rows if r.get("rtt_ms") and r["rtt_ms"] > 0]
    total_outages = sum(_ping_outage_episodes(ping_rows, str(d["id"])) for d in devices)

    pdf.kpi_row([
        {"label": "Devices", "value": f"{len(devices):,}", "accent": "primary"},
        {"label": "Online now", "value": f"{online_now} of {len(devices)}",
         "accent": "success" if online_now == len(devices) else "danger"},
        {"label": "Fleet availability",
         "value": _fmt_pct(fleet_avail) if fleet_avail is not None else "—",
         "accent": _avail_accent(fleet_avail)},
        {"label": "Outage episodes", "value": f"{total_outages:,}",
         "accent": "warning" if total_outages else "success"},
        {"label": "Average RTT",
         "value": _fmt_ms(statistics.mean(all_rtts)) if all_rtts else "—",
         "accent": "info"},
    ])

    # --- Group summary ---
    group_summary: dict[str, dict] = {}
    for d in devices:
        gn = d.get("group_name") or "Ungrouped"
        info = group_summary.setdefault(gn, {"total": 0, "online": 0})
        info["total"] += 1
        if (d.get("status") or "").lower() in ("up", "online"):
            info["online"] += 1
    pdf.sub_heading("Group summary")
    pdf.data_table(
        ["Group", "Devices", "Online", "Online share"],
        [[gn, str(i["total"]), str(i["online"]),
          _fmt_pct(i["online"] / max(i["total"], 1) * 100)]
         for gn, i in sorted(group_summary.items())],
        col_styles=["text", "num", "num", "pct-bar"],
    )

    # --- Per-device summary table (worst availability first) ---
    def dev_sort_key(d):
        did = str(d["id"])
        has = any(str(r["device_id"]) == did for r in ping_rows)
        return _device_uptime_pct(ping_rows, did) if has else 101.0

    devices_sorted = sorted(devices, key=dev_sort_key)
    pdf.section_title(
        "Device Summary", category="Availability",
        description="All devices in scope, worst availability first.")
    summary_rows = []
    for d in devices_sorted:
        did = str(d["id"])
        has = any(str(r["device_id"]) == did for r in ping_rows)
        stats = _device_rtt_stats(ping_rows, did)
        summary_rows.append([
            d.get("hostname") or "—",
            (d.get("ip_address") or "—"),
            d.get("group_name") or "—",
            (d.get("status") or "—"),
            _fmt_pct(_device_uptime_pct(ping_rows, did)) if has else "—",
            _fmt_ms(stats["avg"]),
            _fmt_ms(stats["p95"]),
            str(_ping_outage_episodes(ping_rows, did)),
        ])
    pdf.data_table(
        ["Device", "IP address", "Group", "Status", "Availability", "Avg RTT", "P95 RTT", "Outages"],
        summary_rows,
        col_styles=["text", "mono", "text", "status", "pct-bar", "num", "num", "num"],
    )

    # --- Per-device detail ---
    pdf.section_title(
        "Device Detail", category="Availability",
        description="Latency trend and status transitions for each device.")

    for d in devices_sorted:
        did = str(d["id"])
        hostname = d.get("hostname") or "Unknown"
        meta_bits = [b for b in (d.get("ip_address"), d.get("device_type"),
                                 d.get("group_name"), d.get("location")) if b]
        _device_block_header(pdf, hostname, d.get("status") or "", "  ·  ".join(map(str, meta_bits)))

        dev_pings = [r for r in ping_rows if str(r["device_id"]) == did]
        has = bool(dev_pings)
        stats = _device_rtt_stats(ping_rows, did)
        pdf.spec_strip([
            ("Availability", _fmt_pct(_device_uptime_pct(ping_rows, did)) if has else "—"),
            ("Avg RTT", _fmt_ms(stats["avg"])),
            ("P95 RTT", _fmt_ms(stats["p95"])),
            ("Max RTT", _fmt_ms(stats["max"])),
            ("Outages", str(_ping_outage_episodes(ping_rows, did))),
        ])

        rtt_pts = [r for r in dev_pings if r.get("rtt_ms")]
        if len(rtt_pts) > 2:
            png = _make_line_chart([r["timestamp"] for r in rtt_pts],
                                   [r["rtt_ms"] for r in rtt_pts],
                                   ylabel="RTT (ms)", h_mm=34, unit="ms", tz=_pdf_tz(pdf))
            pdf.add_chart(png)

        dev_log = [e for e in status_log if str(e["device_id"]) == did]
        if dev_log:
            pdf.data_table(_STATUS_LOG_HEADERS, _status_log_rows(dev_log, limit=10, branding=pdf.branding),
                           col_styles=_STATUS_LOG_STYLES)
        _block_separator(pdf)


async def _build_service_health(pdf: ZenPlusReport, db: AsyncSession,
                                start: datetime, end: datetime):
    services = await _fetch_service_checks(db)
    svc_ids = [str(s["id"]) for s in services]
    svc_rows = _fetch_service_metrics(start, end, svc_ids)
    svc_log = _fetch_service_status_log(start, end, svc_ids)

    pdf.add_page()
    pdf.section_title(
        "Service Health", category="Services",
        description="HTTP, TCP and TLS service checks: uptime, response time and "
                    "certificate expiry.")

    if not services:
        pdf.empty_state("No service checks are configured.")
        return

    # --- KPIs ---
    up_now = sum(1 for s in services if (s.get("status") or "").lower() in ("up", "online"))
    uptimes = [u for u in (_service_uptime_pct(svc_rows, str(s["id"])) for s in services)
               if u is not None]
    avg_uptime = statistics.mean(uptimes) if uptimes else None
    all_resp = [r["response_ms"] for r in svc_rows if r.get("response_ms")]
    tls_soon = sum(1 for s in services
                   if s.get("tls_days_remaining") is not None and s["tls_days_remaining"] <= 30)

    pdf.kpi_row([
        {"label": "Service checks", "value": f"{len(services):,}", "accent": "primary"},
        {"label": "Passing now", "value": f"{up_now} of {len(services)}",
         "accent": "success" if up_now == len(services) else "danger"},
        {"label": "Average availability",
         "value": _fmt_pct(avg_uptime) if avg_uptime is not None else "—",
         "accent": _avail_accent(avg_uptime)},
        {"label": "Average response",
         "value": _fmt_ms(statistics.mean(all_resp)) if all_resp else "—", "accent": "info"},
        {"label": "TLS expiring ≤30d", "value": str(tls_soon),
         "accent": "warning" if tls_soon else "success"},
    ])

    # --- Overview table ---
    pdf.sub_heading("Service overview")
    overview_rows = []
    for s in services:
        sid = str(s["id"])
        uptime = _service_uptime_pct(svc_rows, sid)
        resp_times = [r["response_ms"] for r in svc_rows
                      if str(r["service_check_id"]) == sid and r.get("response_ms")]
        tls_info = "—"
        if s.get("tls_days_remaining") is not None:
            days = s["tls_days_remaining"]
            tls_info = f"{days}d" if days >= 0 else "Expired"
        overview_rows.append([
            s.get("name", "—"),
            (s.get("check_type") or "—").upper(),
            (s.get("status") or "—"),
            _fmt_pct(uptime),
            _fmt_ms(statistics.mean(resp_times)) if resp_times else "—",
            tls_info,
        ])
    pdf.data_table(
        ["Service", "Type", "Status", "Availability", "Avg response", "TLS expiry"],
        overview_rows,
        col_styles=["text", "text", "status", "pct-bar", "num", "num"],
    )

    # --- Per-service detail ---
    pdf.section_title(
        "Service Detail", category="Services",
        description="Response-time trend and status transitions for each check.")
    for s in services:
        sid = str(s["id"])
        target = s.get("target_url") or s.get("target_host") or ""
        meta = "  ·  ".join(b for b in ((s.get("check_type") or "").upper(), target) if b)
        _device_block_header(pdf, s.get("name", "Unknown"), s.get("status") or "", meta)

        uptime = _service_uptime_pct(svc_rows, sid)
        resp_times = [r["response_ms"] for r in svc_rows
                      if str(r["service_check_id"]) == sid and r.get("response_ms")]
        p95 = None
        if resp_times:
            rs = sorted(resp_times)
            p95 = rs[min(int(len(rs) * 0.95), len(rs) - 1)]
        specs = [
            ("Availability", _fmt_pct(uptime) if resp_times or uptime else "—"),
            ("Avg response", _fmt_ms(statistics.mean(resp_times)) if resp_times else "—"),
            ("P95 response", _fmt_ms(p95)),
        ]
        if s.get("tls_days_remaining") is not None:
            days = s["tls_days_remaining"]
            expiry = s.get("tls_expiry_date")
            if isinstance(expiry, datetime) and pdf.branding is not None:
                exp_str = pdf.branding.fmt_date(expiry)
            else:
                exp_str = expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry or "")
            specs.append(("TLS expiry", f"{days}d ({exp_str})" if exp_str else f"{days}d"))
        pdf.spec_strip(specs)

        svc_metrics = [r for r in svc_rows
                       if str(r["service_check_id"]) == sid and r.get("response_ms")]
        if len(svc_metrics) > 2:
            png = _make_line_chart([r["timestamp"] for r in svc_metrics],
                                   [r["response_ms"] for r in svc_metrics],
                                   ylabel="response (ms)", h_mm=34, unit="ms", tz=_pdf_tz(pdf))
            pdf.add_chart(png)

        s_log = [e for e in svc_log if str(e["service_check_id"]) == sid]
        if s_log:
            pdf.data_table(_STATUS_LOG_HEADERS, _status_log_rows(s_log, limit=8, branding=pdf.branding),
                           col_styles=_STATUS_LOG_STYLES)
        _block_separator(pdf)


async def _build_alert_analysis(pdf: ZenPlusReport, db: AsyncSession,
                                start: datetime, end: datetime,
                                device_ids: list[str] | None,
                                redact_netflow: bool = False):
    alerts = await _fetch_alerts(db, start, end, device_ids, redact_netflow=redact_netflow)

    pdf.add_page()
    pdf.section_title(
        "Alert Analysis", category="Alerting",
        description="Alert volume, severity mix, noisiest sources and time to "
                    "resolution for the reporting period.")

    if not alerts:
        pdf.empty_state("No alerts were triggered in the selected period.")
        return

    total = len(alerts)
    active = sum(1 for a in alerts if (a.get("status") or "").lower() == "active")
    acked = sum(1 for a in alerts if (a.get("status") or "").lower() == "acknowledged")
    resolved = sum(1 for a in alerts if (a.get("status") or "").lower() == "resolved")
    mttr = _mttr_seconds(alerts)

    pdf.kpi_row([
        {"label": "Alerts triggered", "value": f"{total:,}", "accent": "primary"},
        {"label": "Still active", "value": f"{active:,}",
         "accent": "danger" if active else "success"},
        {"label": "Acknowledged", "value": f"{acked:,}", "accent": "warning"},
        {"label": "Resolved", "value": f"{resolved:,}", "accent": "success"},
        {"label": "Mean time to resolve", "value": _fmt_duration(mttr), "accent": "info"},
    ])
    pdf.ln(1)

    # --- Severity donut + daily volume, side by side ---
    sev_counts: dict[str, int] = {}
    for a in alerts:
        sev = (a.get("severity") or "info").lower()
        sev_counts[sev] = sev_counts.get(sev, 0) + 1
    donut_png = _make_donut(
        [l.capitalize() for l in sev_counts], list(sev_counts.values()),
        [STATUS_COLORS_HEX.get(l, HEX_FAINT) for l in sev_counts],
        center_text=f"{total:,}", center_sub="alerts")

    day_counts: dict[str, int] = {}
    for a in alerts:
        t = a.get("triggered_at")
        if t:
            if isinstance(t, datetime) and pdf.branding is not None:
                t = pdf.branding.localize(t)  # calendar days in the organisation timezone
            day_key = t.strftime("%Y-%m-%d") if hasattr(t, "strftime") else str(t)[:10]
            day_counts[day_key] = day_counts.get(day_key, 0) + 1
    sorted_days = sorted(day_counts)
    bars_png = _make_time_bar_chart(
        [datetime.strptime(dk, "%Y-%m-%d").replace(tzinfo=_pdf_tz(pdf) or timezone.utc)
         for dk in sorted_days],
        [day_counts[dk] for dk in sorted_days],
        color=HEX_WARNING, w_mm=86, h_mm=54, tz=_pdf_tz(pdf))
    pdf.add_chart_pair(donut_png, bars_png, "Alerts by severity", "Daily alert volume")

    # --- Top alerting devices ---
    host_counts: dict[str, int] = {}
    for a in alerts:
        h = a.get("hostname") or "Unknown"
        host_counts[h] = host_counts.get(h, 0) + 1
    top_hosts = sorted(host_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    if top_hosts:
        png = _make_bar_chart([h for h, _ in top_hosts], [c for _, c in top_hosts],
                              colors=HEX_PRIMARY, ylabel="alerts")
        pdf.add_chart(png, title="Top alerting devices")

    # --- MTTR by severity ---
    mttr_by_sev: dict[str, list[float]] = {}
    for a in alerts:
        if a.get("resolved_at") and a.get("triggered_at"):
            dur = (a["resolved_at"] - a["triggered_at"]).total_seconds()
            if dur > 0:
                mttr_by_sev.setdefault((a.get("severity") or "info").lower(), []).append(dur)
    pdf.sub_heading("Resolution time by severity")
    pdf.data_table(
        ["Severity", "Resolved", "Avg time", "Fastest", "Slowest"],
        [[sev.capitalize(), str(len(ds)),
          _fmt_duration(statistics.mean(ds)) if ds else "—",
          _fmt_duration(min(ds)) if ds else "—",
          _fmt_duration(max(ds)) if ds else "—"]
         for sev, ds in ((s, mttr_by_sev.get(s, [])) for s in ("critical", "warning", "info"))],
        col_styles=["severity", "num", "num", "num", "num"],
    )

    # --- Recent alerts ---
    pdf.section_title("Recent Alerts", category="Alerting",
                      description="Latest alerts in the period, newest first.")
    alert_rows = []
    for a in alerts[:30]:
        triggered = a.get("triggered_at")
        trig = _fmt_when(pdf, triggered, short=True)
        alert_rows.append([
            trig,
            a.get("hostname") or "—",
            (a.get("severity") or "—"),
            (a.get("status") or "—"),
            (a.get("message") or "—")[:90],
        ])
    pdf.data_table(
        ["Triggered", "Source", "Severity", "Status", "Message"],
        alert_rows,
        col_styles=["num", "text", "severity", "status", "text"],
        max_rows=30,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

_REPORT_TITLES = {
    "executive_summary": "Executive Summary",
    "device_health": "Device Health Report",
    "service_health": "Service Health Report",
    "alert_analysis": "Alert Analysis Report",
    "full_report": "Full Network Report",
}

_REPORT_SUBTITLES = {
    "executive_summary": "Leadership view of estate availability, alert load and "
                         "operational response.",
    "device_health": "Per-device uptime, latency trends and status history across "
                     "the monitored network.",
    "service_health": "Service check availability, response times and TLS "
                      "certificate posture.",
    "alert_analysis": "Alert trends, severity mix, noisiest sources and time to "
                      "resolution.",
    "full_report": "Complete operational picture: devices, services and alerting "
                   "in a single document.",
}


def _legacy_scope_label(device_ids, group_ids, locations, device_types) -> str:
    parts = []
    if device_ids:
        parts.append(f"{len(device_ids)} selected device(s)")
    if group_ids:
        parts.append(f"{len(group_ids)} device group(s)")
    if locations:
        parts.append(f"{len(locations)} location(s)")
    if device_types:
        parts.append(f"{len(device_types)} device type(s)")
    return ", ".join(parts) if parts else "All monitored infrastructure"


async def generate_report(
    db: AsyncSession,
    report_type: str,          # 'executive_summary' | 'device_health' | 'service_health' | 'alert_analysis' | 'full_report'
    period: str,               # 'last_24h' | 'last_7d' | 'last_30d' | 'custom'
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    device_ids: list[str] | None = None,
    group_ids: list[str] | None = None,
    locations: list[str] | None = None,
    device_types: list[str] | None = None,
    *,
    redact_netflow: bool = False,  # the requester lacks netflow.view
) -> bytes:
    """Generate a professional PDF report and return the raw PDF bytes."""

    start, end, preset_label = _resolve_period(period, from_time, to_time)

    # Organisation branding: name, logos, brand colour, paper size, timezone
    # and date/time format (Settings → General → Company).
    from app.services.branding import Branding, load_branding
    try:
        branding = await load_branding(db)
    except Exception:
        logging.getLogger(__name__).warning("branding unavailable for legacy report", exc_info=True)
        branding = Branding()
    window = branding.period_label(start, end)
    period_label = window if period == "custom" else f"{preset_label} · {window}"
    generated_at = datetime.now(timezone.utc)
    light, cover_logo = branding.logo, branding.cover_logo()

    title = _REPORT_TITLES.get(report_type, "ZenPlus Report")

    pdf = ZenPlusReport(
        title=title,
        company_name=branding.display_name,
        logo_bytes=light.data if light else None,
        period_label=period_label,
        generated_at=generated_at,
        subtitle=_REPORT_SUBTITLES.get(report_type, ""),
        category="Network Operations",
        scope_label=_legacy_scope_label(device_ids, group_ids, locations, device_types),
        accent=branding.brand_rgb,
        paper_size=branding.paper_size,
        cover_logo_bytes=cover_logo.data if cover_logo else None,
        cover_logo_on_dark=branding.logo_dark is not None,
        doc_label="Network Operations Report",
        classification=branding.confidentiality,
        footer_text=branding.report_footer,
        timezone_name=branding.timezone,
        tz_label=branding.tz_label(end),
        generated_label=branding.fmt_datetime(generated_at, with_tz=True),
        disclaimer=branding.report_disclaimer,
        contact_line=branding.contact_line,
        branding=branding,
    )
    pdf.cover()

    # Resolve filtered device IDs from all filter criteria
    resolved_device_ids = device_ids
    has_filters = group_ids or locations or device_types
    if has_filters and not device_ids:
        devices = await _fetch_devices(db, device_ids=None, group_ids=group_ids,
                                       locations=locations, device_types=device_types)
        resolved_device_ids = [str(d["id"]) for d in devices]

    # Build requested sections
    if report_type == "executive_summary":
        await _build_executive_summary(pdf, db, start, end, resolved_device_ids, group_ids)

    elif report_type == "device_health":
        await _build_device_health(pdf, db, start, end, resolved_device_ids, group_ids)

    elif report_type == "service_health":
        await _build_service_health(pdf, db, start, end)

    elif report_type == "alert_analysis":
        await _build_alert_analysis(pdf, db, start, end, resolved_device_ids, redact_netflow)

    elif report_type == "full_report":
        pdf.toc(expected_entries=12)
        await _build_executive_summary(pdf, db, start, end, resolved_device_ids, group_ids)
        await _build_device_health(pdf, db, start, end, resolved_device_ids, group_ids)
        await _build_service_health(pdf, db, start, end)
        await _build_alert_analysis(pdf, db, start, end, resolved_device_ids, redact_netflow)

    else:
        pdf.add_page()
        pdf.note(f"Unknown report type: {report_type}", kind="warning")

    if pdf.disclaimer:
        pdf.disclaimer_block(pdf.disclaimer, contact=pdf.contact_line,
                             address=branding.company_address)

    return pdf.output()
