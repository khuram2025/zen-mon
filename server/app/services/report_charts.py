"""Branded chart images for report documents (PDF, HTML, email, XLSX).

:func:`chart_png` turns a section's neutral chart ``series`` (the same dict the
dashboard viewer draws natively) into a print-grade PNG::

    {"kind": "area" | "line" | "bars" | "stacked" | "rank" | "donut",
     "unit": "%", "color": "primary" | "success" | ... | "#RRGGBB",
     "y_domain": [lo, hi],
     "points": [{"t": iso, "v": number | None}] | [{"label": str, "value": number}],
     "series": [{"name": str, "color"?: str, "points": [...]}]}   # multi-series

Design rules (one system for every output):

- one categorical palette in a fixed order (validated for colour-vision
  deficiency on white); identity never relies on colour alone: two or more
  series always get a legend;
- status colours (up/down/degraded/unknown, good/poor, ...) are reserved for
  status categories and never used as "series 4";
- the organisation's brand colour is an accent only (the title tick), never
  a data colour, so a red or yellow brand cannot read as an alarm;
- time axes are in the organisation timezone with its date/time format, the
  zone named under the axis; units sit on the value axis;
- missing values and monitoring gaps are drawn as gaps, never as zero;
- an empty series renders a quiet "No data" panel instead of empty axes.

Rendering uses the object-oriented Agg API (no pyplot state), a fixed
millimetre layout (no constrained-layout passes) and a small LRU cache, so the
same chart drawn for the HTML artifact and the PDF attachment of one run is
rendered once.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import warnings
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402

# Importing report_service registers the embedded document fonts with
# matplotlib and provides the shared design tokens.
from app.services.report_service import (  # noqa: E402
    _MPL_RC,
    CATEGORICAL_HEX,
    MPL_LOCK,
    HEX_BG_TINT,
    HEX_DANGER,
    HEX_DEGRADED,
    HEX_FAINT,
    HEX_GRID,
    HEX_INFO,
    HEX_MUTED,
    HEX_SUCCESS,
    HEX_TEXT,
    HEX_WARNING,
    OTHER_HEX,
    accent_on_white,
    release_figure,
    rgb_to_hex,
)

DPI = 200
KINDS = ("area", "line", "bars", "stacked", "rank", "donut")
TIME_KINDS = ("area", "line", "bars", "stacked")
MAX_SERIES = len(CATEGORICAL_HEX)      # past this, fold into "Other"
MAX_RANK_BARS = 20
MAX_POINTS = 720                       # downsample longer series (mean + min/max band)
_NICE_STEPS = [1, 2, 2.5, 5, 10]       # y-axis tick steps (no 1.5 or 3 steps)

# Named colours a section may ask for. "primary" is the first categorical slot,
# not the brand colour: the brand is an accent, never a data colour.
MARK_HEX = {
    "primary": CATEGORICAL_HEX[0],
    "success": HEX_SUCCESS,
    "warning": HEX_WARNING,
    "danger": HEX_DANGER,
    "info": HEX_INFO,
    "neutral": HEX_MUTED,
    "degraded": HEX_DEGRADED,
}

# Reserved status colours: a category whose name is a status word is drawn in
# its status colour (and only status categories are).
_STATUS_HEX = {
    **{w: HEX_SUCCESS for w in (
        "up", "online", "healthy", "ok", "good", "success", "succeeded", "passing", "passed",
        "resolved", "on track", "connected", "compliant", "validated", "protected")},
    **{w: HEX_DANGER for w in (
        "down", "offline", "critical", "failed", "failure", "failing", "error", "errors",
        "poor", "breaching", "expired", "non-compliant", "noncompliant", "major", "high")},
    **{w: HEX_WARNING for w in (
        "warning", "warn", "needs work", "needs improvement", "at risk", "acknowledged",
        "stale", "paused", "medium", "minor")},
    **{w: HEX_DEGRADED for w in ("degraded",)},
    **{w: HEX_INFO for w in ("info", "maintenance", "pending", "low", "active")},
    **{w: HEX_FAINT for w in ("unknown", "not measured", "no data", "disabled", "none", "n/a")},
}

_INK = HEX_TEXT
_MUTED = HEX_MUTED
_CHAR_MM = {6.2: 1.16, 6.8: 1.27, 7.0: 1.31, 7.2: 1.35}  # avg glyph width (mm) per pt size

# rcParams are process-global: share the document engine's matplotlib lock
# (it also guards the LRU cache below).
_LOCK = MPL_LOCK
_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_CACHE_MAX = 48


# ─── Public API ─────────────────────────────────────────────────────────────

def chart_png(series: dict, *, title: str = "", branding=None,
              width_mm: float = 178, height_mm: float = 60) -> bytes:
    """Render ``series`` as a PNG at 200 dpi and exactly ``width_mm`` wide.

    ``height_mm`` is honoured for time and donut charts; ranked bars size
    themselves to their rows. ``branding`` (app.services.branding.Branding)
    supplies the timezone, date/time format and accent; None means UTC and
    the product defaults. Never raises for bad data: anything unreadable
    becomes the "No data" panel."""
    s = series if isinstance(series, dict) else {}
    w = float(min(max(width_mm or 178, 30), 400))
    h = float(min(max(height_mm or 60, 18), 300))
    key = _cache_key(s, title, branding, w, h)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
        with matplotlib.rc_context(_MPL_RC), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                png = _render(s, title or "", _Ctx(branding), w, h)
            except Exception:
                # A malformed series must never fail a report.
                png = _placeholder(_Ctx(branding), title or "", w, min(h, 40),
                                   "Chart unavailable for this data")
        _CACHE[key] = png
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
        return png


def png_size_mm(png: bytes, dpi: int = DPI) -> tuple[float, float]:
    """Physical size (mm) of a PNG rendered at ``dpi``."""
    w = int.from_bytes(png[16:20], "big")
    h = int.from_bytes(png[20:24], "big")
    return w / dpi * 25.4, h / dpi * 25.4


def status_color(label: Any) -> Optional[str]:
    """The reserved status colour for a status word, or None."""
    return _STATUS_HEX.get(str(label or "").strip().lower())


# ─── Context ────────────────────────────────────────────────────────────────

class _Ctx:
    """Branding facts a chart needs (works without a Branding object)."""

    def __init__(self, branding):
        self.branding = branding
        tz_name = getattr(branding, "timezone", None) or "UTC"
        try:
            self.tz = ZoneInfo(tz_name)
        except Exception:
            self.tz, tz_name = ZoneInfo("UTC"), "UTC"
        self.tz_name = tz_name
        self.twelve_hour = getattr(branding, "time_format", "24h") == "12h"
        df = getattr(branding, "date_format", "YYYY-MM-DD") or "YYYY-MM-DD"
        self.month_first = df.startswith("MM") or df.startswith("MMM") or df == "YYYY-MM-DD"
        rgb = getattr(branding, "brand_rgb", None)
        self.accent = rgb_to_hex(accent_on_white(rgb)) if rgb else CATEGORICAL_HEX[0]

    def tz_label(self, at: Optional[datetime]) -> str:
        b = self.branding
        if b is not None and hasattr(b, "tz_label"):
            try:
                return b.tz_label(at)
            except Exception:
                pass
        return "UTC"

    def fmt_date(self, dt: datetime) -> str:
        b = self.branding
        if b is not None and hasattr(b, "fmt_date"):
            try:
                return b.fmt_date(dt)
            except Exception:
                pass
        return dt.astimezone(self.tz).strftime("%Y-%m-%d")


def _cache_key(s: dict, title: str, branding, w: float, h: float) -> str:
    brand = (getattr(branding, "timezone", "UTC"), getattr(branding, "time_format", ""),
             getattr(branding, "date_format", ""), getattr(branding, "brand_color", ""))
    raw = json.dumps([s, title, brand, round(w, 2), round(h, 2)], sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


# ─── Data normalisation ─────────────────────────────────────────────────────

def _num(v: Any) -> float:
    if v is None or isinstance(v, bool):
        return math.nan
    try:
        f = float(v)
    except (TypeError, ValueError):
        return math.nan
    return f if math.isfinite(f) else math.nan


def _parse_t(t: Any) -> Optional[datetime]:
    if isinstance(t, datetime):
        dt = t
    elif isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t):
        secs = t / 1000.0 if abs(t) > 1e11 else float(t)
        try:
            dt = datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(t, str) and t.strip():
        try:
            dt = datetime.fromisoformat(t.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _mark_hex(color: Any, default: str) -> str:
    if isinstance(color, str):
        c = color.strip()
        if c in MARK_HEX:
            return MARK_HEX[c]
        if len(c) == 7 and c.startswith("#"):
            try:
                int(c[1:], 16)
                return c
            except ValueError:
                pass
    return default


def _time_points(points: Any) -> tuple[list[datetime], np.ndarray]:
    pairs = []
    for p in points or ():
        if not isinstance(p, dict):
            continue
        t = _parse_t(p.get("t"))
        if t is None:
            continue
        pairs.append((t, _num(p.get("v", p.get("value")))))
    pairs.sort(key=lambda x: x[0])
    return [p[0] for p in pairs], np.array([p[1] for p in pairs], dtype=float)


def _label_points(points: Any) -> list[tuple[str, float]]:
    out = []
    for p in points or ():
        if not isinstance(p, dict):
            continue
        label = p.get("label")
        if label is None:
            label = p.get("name")
        if label is None:
            continue
        out.append((str(label), _num(p.get("value", p.get("v")))))
    return out


def _series_list(s: dict) -> list[dict]:
    """[{name, color, points}] — the multi-series list, or the single series."""
    multi = s.get("series")
    if isinstance(multi, list) and multi:
        out = []
        for i, m in enumerate(multi):
            if isinstance(m, dict):
                out.append({"name": str(m.get("name") or f"Series {i + 1}"),
                            "color": m.get("color"), "points": m.get("points") or []})
        if out:
            return out
    return [{"name": "", "color": s.get("color"), "points": s.get("points") or []}]


def _has_time(series: list[dict]) -> bool:
    for m in series:
        for p in m["points"]:
            if isinstance(p, dict) and p.get("t") not in (None, ""):
                return True
    return False


def _all_status(names: list[str]) -> bool:
    return bool(names) and all(status_color(n) for n in names)


def _series_colors(series: list[dict], single_default: str) -> list[str]:
    names = [m["name"] for m in series]
    if len(series) == 1:
        return [_mark_hex(series[0]["color"], single_default)]
    status = _all_status(names)
    out = []
    for i, m in enumerate(series):
        if m["name"] == "Other":
            out.append(OTHER_HEX)
        elif m.get("color"):
            out.append(_mark_hex(m["color"], CATEGORICAL_HEX[i % MAX_SERIES]))
        elif status:
            out.append(status_color(m["name"]))
        else:
            out.append(CATEGORICAL_HEX[min(i, MAX_SERIES - 1)])
    return out


def _with_gaps(ts: list[datetime], vs: np.ndarray) -> tuple[list[datetime], np.ndarray]:
    """Insert a gap (NaN) where samples are missing: a spacing well beyond the
    series' usual step is a monitoring gap, not a straight line."""
    if len(ts) < 3:
        return ts, vs
    secs = np.array([t.timestamp() for t in ts])
    d = np.diff(secs)
    pos = d[d > 0]
    if not len(pos):
        return ts, vs
    step = float(np.median(pos))
    if step <= 0 or not np.any(d > step * 2.5):
        return ts, vs
    out_t, out_v = [ts[0]], [vs[0]]
    for i in range(1, len(ts)):
        if d[i - 1] > step * 2.5:
            mid = datetime.fromtimestamp((secs[i - 1] + secs[i]) / 2, tz=timezone.utc)
            out_t.append(mid)
            out_v.append(math.nan)
        out_t.append(ts[i])
        out_v.append(vs[i])
    return out_t, np.array(out_v, dtype=float)


def _downsample(ts: list[datetime], vs: np.ndarray):
    """NaN-aware bucket mean with a min/max band for long series."""
    n = len(vs)
    if n <= MAX_POINTS:
        return ts, vs, None, None
    step = math.ceil(n / MAX_POINTS)
    t_out, mean, lo, hi = [], [], [], []
    for i in range(0, n, step):
        chunk = vs[i:i + step]
        t_out.append(ts[min(i + step // 2, n - 1)])
        if np.all(np.isnan(chunk)):
            mean.append(math.nan)
            lo.append(math.nan)
            hi.append(math.nan)
        else:
            mean.append(float(np.nanmean(chunk)))
            lo.append(float(np.nanmin(chunk)))
            hi.append(float(np.nanmax(chunk)))
    return t_out, np.array(mean), np.array(lo), np.array(hi)


# ─── Formatting ─────────────────────────────────────────────────────────────

def _compact(v: float) -> str:
    av = abs(v)
    if av >= 1e12:
        return f"{v / 1e12:.3g}T"
    if av >= 1e9:
        return f"{v / 1e9:.3g}B"
    if av >= 1e6:
        return f"{v / 1e6:.3g}M"
    if av >= 1e4:
        return f"{v / 1e3:.3g}k"
    if av >= 100 or float(v).is_integer():
        return f"{v:,.0f}"
    if av >= 10:
        return f"{v:.1f}"
    return f"{v:.2g}" if av < 1 else f"{v:.2f}".rstrip("0").rstrip(".")


def _fmt_value(v: float, unit: str) -> str:
    if v is None or not math.isfinite(v):
        return "—"
    if unit == "%":
        return f"{v:.0f}%" if float(v).is_integer() else f"{v:.2f}%"
    txt = _compact(v)
    return f"{txt} {unit}".strip() if unit else txt


def _tick_fmt(unit: str):
    def fmt(v, _pos=None):
        txt = _compact(v) if v else "0"
        return f"{txt}%" if unit == "%" else txt
    return mticker.FuncFormatter(fmt)


def _text_mm(txt: str, size: float = 7.0) -> float:
    per = _CHAR_MM.get(size, size * 0.187)
    return len(txt) * per


def _ellipsize(txt: str, limit: int) -> str:
    return txt if len(txt) <= limit else txt[:max(limit - 1, 1)] + "…"


# ─── Layout helpers ─────────────────────────────────────────────────────────

def _new_fig(w_mm: float, h_mm: float) -> Figure:
    fig = Figure(figsize=(w_mm / 25.4, h_mm / 25.4), dpi=DPI, facecolor="white")
    FigureCanvasAgg(fig)
    return fig


def _save(fig: Figure) -> bytes:
    buf = io.BytesIO()
    try:
        with warnings.catch_warnings():
            # Very short ranges (a single bucket) make AutoDateLocator fall back
            # to its default interval; harmless, and noisy in logs.
            warnings.filterwarnings("ignore", message="AutoDateLocator was unable", category=UserWarning)
            fig.savefig(buf, format="png", dpi=DPI, facecolor="white", edgecolor="none")
    finally:
        release_figure(fig)
    return buf.getvalue()


def _title(fig: Figure, ctx: _Ctx, title: str, w_mm: float, h_mm: float) -> float:
    """Draw the chart title with the brand accent tick; return its height (mm)."""
    if not title:
        return 0.0
    top = h_mm - 1.4
    fig.add_artist(Rectangle((0.8 / w_mm, (top - 3.6) / h_mm), 0.9 / w_mm, 3.2 / h_mm,
                             transform=fig.transFigure, facecolor=ctx.accent,
                             edgecolor="none"))
    fig.text(3.0 / w_mm, (top - 2.0) / h_mm, _ellipsize(title, int(w_mm / 1.6)),
             ha="left", va="center", fontsize=8.4, fontweight="bold", color=_INK)
    return 6.2


def _placeholder(ctx: _Ctx, title: str, w_mm: float, h_mm: float, label: str) -> bytes:
    h_mm = max(h_mm, 22.0)
    fig = _new_fig(w_mm, h_mm)
    top = _title(fig, ctx, title, w_mm, h_mm)
    pad = 0.9
    box_h = h_mm - top - 2 * pad
    fig.add_artist(FancyBboxPatch(
        (pad / w_mm, pad / h_mm), (w_mm - 2 * pad) / w_mm, box_h / h_mm,
        boxstyle="round,pad=0,rounding_size=0.012", transform=fig.transFigure,
        facecolor=HEX_BG_TINT, edgecolor=HEX_GRID, linewidth=0.8))
    fig.text(0.5, (pad + box_h / 2) / h_mm, label, ha="center", va="center",
             fontsize=7.6, color=_MUTED)
    return _save(fig)


def _axes(fig: Figure, w_mm: float, h_mm: float, *, left: float, right: float,
          top: float, bottom: float):
    ax = fig.add_axes([left / w_mm, bottom / h_mm,
                       max(w_mm - left - right, 5) / w_mm,
                       max(h_mm - top - bottom, 5) / h_mm])
    return ax


def _legend(fig: Figure, names: list[str], colors: list[str], w_mm: float, h_mm: float,
            y_mm: float, x_mm: float, kind: str) -> None:
    """One-row legend (swatch + name in ink) at y_mm from the top."""
    x = x_mm
    for name, col in zip(names, colors):
        label = _ellipsize(name, 26)
        if kind in ("line", "area"):
            fig.add_artist(Rectangle((x / w_mm, (h_mm - y_mm - 0.35) / h_mm), 3.4 / w_mm,
                                     0.7 / h_mm, transform=fig.transFigure,
                                     facecolor=col, edgecolor="none"))
        else:
            fig.add_artist(FancyBboxPatch((x / w_mm, (h_mm - y_mm - 1.1) / h_mm), 2.2 / w_mm,
                                          2.2 / h_mm, boxstyle="round,pad=0,rounding_size=0.002",
                                          transform=fig.transFigure, facecolor=col,
                                          edgecolor="none"))
        fig.text((x + 4.2) / w_mm, (h_mm - y_mm) / h_mm, label, ha="left", va="center",
                 fontsize=7.0, color=_INK)
        x += 4.2 + _text_mm(label, 7.0) + 4.0
        if x > w_mm - 12:
            break


def _y_ticks_width(lo: float, hi: float, unit: str, integer: bool) -> float:
    loc = mticker.MaxNLocator(nbins=4, integer=integer, min_n_ticks=3, steps=_NICE_STEPS)
    ticks = [t for t in loc.tick_values(lo, hi) if lo - 1e-9 <= t <= hi + 1e-9]
    fmt = _tick_fmt(unit)
    longest = max((len(fmt(t)) for t in ticks), default=3)
    return longest * _CHAR_MM[6.8] + 2.0


def _style_value_axis(ax, lo: float, hi: float, unit: str, integer: bool, axis: str = "y"):
    loc = mticker.MaxNLocator(nbins=4, integer=integer, min_n_ticks=3, steps=_NICE_STEPS)
    target = ax.yaxis if axis == "y" else ax.xaxis
    target.set_major_locator(loc)
    target.set_major_formatter(_tick_fmt(unit))
    if axis == "y":
        ax.set_ylim(lo, hi)
    else:
        ax.set_xlim(lo, hi)
    ax.tick_params(axis=axis, labelsize=6.8, colors=_MUTED, length=0)


def _style_time_axis(ax, ctx: _Ctx, axis_w_mm: float) -> None:
    hm = "%-I:%M %p" if ctx.twelve_hour else "%H:%M"
    day = "%b %d" if ctx.month_first else "%d %b"
    loc = mdates.AutoDateLocator(tz=ctx.tz, minticks=3, maxticks=max(4, int(axis_w_mm / 17)))
    fmt = mdates.ConciseDateFormatter(
        loc, tz=ctx.tz, show_offset=False,
        formats=["%Y", "%b", "%d", hm, hm, "%S"],
        zero_formats=["", "%Y", day, day, hm, hm])
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(fmt)
    ax.tick_params(axis="x", labelsize=6.8, colors=_MUTED, length=0, pad=3)


def _time_context(ctx: _Ctx, t0: datetime, t1: datetime) -> str:
    """Date context + zone printed under a time axis ('Oct 07, 2026 · GMT+3')."""
    a, b = t0.astimezone(ctx.tz), t1.astimezone(ctx.tz)
    if a.date() == b.date():
        when = ctx.fmt_date(t0)
    elif a.year == b.year:
        when = str(a.year)
    else:
        when = f"{a.year}–{b.year}"
    return f"{when} · times in {ctx.tz_label(t1)}"


# ─── Renderers ──────────────────────────────────────────────────────────────

def _render(s: dict, title: str, ctx: _Ctx, w: float, h: float) -> bytes:
    kind = s.get("kind") if s.get("kind") in KINDS else "area"
    unit = str(s.get("unit") or "")
    series = _series_list(s)
    if kind == "donut":
        return _donut(s, series, title, ctx, w, h, unit)
    if kind == "rank":
        return _rank(s, series, title, ctx, w, h, unit)
    if not _has_time(series):
        return _categorical(s, series, title, ctx, w, h, unit, kind)
    if kind in ("bars", "stacked"):
        return _time_bars(s, series, title, ctx, w, h, unit, stacked=(kind == "stacked"))
    return _time_lines(s, series, title, ctx, w, h, unit, fill=(kind == "area"))


def _y_range(vals: list[np.ndarray], y_domain: Any, zero_floor: bool) -> tuple[float, float]:
    finite = np.concatenate([v[np.isfinite(v)] for v in vals]) if vals else np.array([])
    vmin, vmax = float(finite.min()), float(finite.max())
    if isinstance(y_domain, (list, tuple)) and len(y_domain) == 2:
        lo_d, hi_d = _num(y_domain[0]), _num(y_domain[1])
        lo = min(lo_d, vmin) if math.isfinite(lo_d) else vmin
        hi = max(hi_d, vmax) if math.isfinite(hi_d) else vmax
    else:
        span = vmax - vmin
        pad = span * 0.12 if span else max(abs(vmax), 1.0) * 0.1
        lo = 0.0 if zero_floor or (vmin >= 0 and vmin < vmax * 0.35) else vmin - pad
        hi = vmax + pad
    if hi <= lo:
        hi = lo + 1.0
    return lo, hi


def _time_lines(s, series, title, ctx, w, h, unit, fill: bool) -> bytes:
    prepared = []
    for m in series:
        ts, vs = _time_points(m["points"])
        if len(ts) and np.any(np.isfinite(vs)):
            prepared.append((m, ts, vs))
    if not prepared:
        return _placeholder(ctx, title, w, h, "No data for this period")
    dropped = 0
    if len(prepared) > MAX_SERIES:
        prepared.sort(key=lambda p: -float(np.nansum(np.abs(p[2]))))
        dropped = len(prepared) - MAX_SERIES
        prepared = prepared[:MAX_SERIES]
    colors = _series_colors([p[0] for p in prepared], CATEGORICAL_HEX[0])
    multi = len(prepared) > 1

    lines = []
    for (m, ts, vs) in prepared:
        ts, vs = _with_gaps(ts, vs)
        ts, mean, band_lo, band_hi = _downsample(ts, vs)
        lines.append((m, ts, mean, band_lo, band_hi))
    all_vals = [ln[2] for ln in lines] + [b for ln in lines for b in (ln[3], ln[4]) if b is not None]
    lo, hi = _y_range(all_vals, s.get("y_domain"), zero_floor=False)
    integer = all(float(v).is_integer() for ln in lines for v in ln[2][np.isfinite(ln[2])]) \
        and hi - lo >= 3
    if unit == "%" and lo >= 0 and hi > 100 and max(float(np.nanmax(v)) for v in all_vals) <= 100:
        hi = 100.0

    fig = _new_fig(w, h)
    top = _title(fig, ctx, title, w, h)
    legend_h = 5.0 if multi else 0.0
    ylabel_w = 4.2 if unit and unit != "%" else 0.0
    left = 1.2 + ylabel_w + _y_ticks_width(lo, hi, unit, integer)
    end_label = "" if multi else _fmt_value(_last_finite(lines[0][2]), unit)
    right = (_text_mm(end_label, 6.8) + 4.5) if end_label else 3.0
    ax = _axes(fig, w, h, left=left, right=right, top=top + legend_h + 2.0, bottom=10.0)
    ax.grid(axis="y", color=HEX_GRID, linewidth=0.6)
    ax.grid(False, axis="x")

    for (m, ts, mean, band_lo, band_hi), col in zip(lines, colors):
        x = mdates.date2num(ts)
        if band_lo is not None and not multi:
            ax.fill_between(x, band_lo, band_hi, color=col, alpha=0.14, linewidth=0, zorder=2)
        elif fill:
            ax.fill_between(x, mean, lo, color=col, alpha=0.06 if multi else 0.10,
                            linewidth=0, zorder=2)
        ax.plot(x, mean, color=col, linewidth=1.5, solid_capstyle="round",
                solid_joinstyle="round", zorder=4)
        finite = np.where(np.isfinite(mean))[0]
        if len(finite) == 1 or (len(finite) and not multi):
            i = finite[-1]
            ax.plot([x[i]], [mean[i]], "o", ms=3.6, color=col, markeredgecolor="white",
                    markeredgewidth=1.0, zorder=5, clip_on=False)
    t0 = min(ln[1][0] for ln in lines)
    t1 = max(ln[1][-1] for ln in lines)
    x0, x1 = mdates.date2num(t0), mdates.date2num(t1)
    if x1 <= x0:
        x0, x1 = x0 - 0.5 / 24, x1 + 0.5 / 24
    ax.set_xlim(x0, x1)
    if end_label:
        m0, _ts, mean0 = lines[0][0], lines[0][1], lines[0][2]
        i = np.where(np.isfinite(mean0))[0][-1]
        ax.annotate(end_label, xy=(mdates.date2num(lines[0][1][i]), mean0[i]),
                    xytext=(4, 0), textcoords="offset points", fontsize=6.8,
                    fontweight="bold", color=_INK, va="center", annotation_clip=False)
    _style_value_axis(ax, lo, hi, unit, integer)
    if unit == "%" and lo <= 0 and hi >= 99.9 and hi - lo > 40:
        ax.set_yticks([t for t in (0, 25, 50, 75, 100) if lo <= t <= hi])
    _style_time_axis(ax, ctx, w - left - right)
    if ylabel_w:
        ax.set_ylabel(unit, fontsize=6.8, color=_MUTED, labelpad=2)
    fig.text(1 - right / w, 1.2 / h, _time_context(ctx, t0, t1), ha="right", va="bottom",
             fontsize=6.2, color=HEX_FAINT)
    if multi:
        names = [ln[0]["name"] for ln in lines] + ([f"+{dropped} more"] if dropped else [])
        _legend(fig, names, colors + ([OTHER_HEX] if dropped else []), w, h,
                top + 2.6, left, "line")
    return _save(fig)


def _last_finite(arr: np.ndarray) -> float:
    idx = np.where(np.isfinite(arr))[0]
    return float(arr[idx[-1]]) if len(idx) else math.nan


def _align(series_pts: list[tuple[list[datetime], np.ndarray]]):
    """Union of timestamps; each series' value per timestamp (NaN if absent)."""
    keys = sorted({t for ts, _ in series_pts for t in ts})
    index = {t: i for i, t in enumerate(keys)}
    rows = []
    for ts, vs in series_pts:
        row = np.full(len(keys), math.nan)
        for t, v in zip(ts, vs):
            row[index[t]] = v if not math.isfinite(row[index[t]]) else row[index[t]] + v
        rows.append(row)
    return keys, rows


def _time_bars(s, series, title, ctx, w, h, unit, stacked: bool) -> bytes:
    prepared = [(m, *_time_points(m["points"])) for m in series]
    prepared = [p for p in prepared if len(p[1])]
    if not prepared or not any(np.any(np.isfinite(p[2])) for p in prepared):
        return _placeholder(ctx, title, w, h, "No data for this period")
    if len(prepared) > MAX_SERIES:
        prepared.sort(key=lambda p: -float(np.nansum(np.abs(p[2]))))
        head, tail = prepared[:MAX_SERIES - 1], prepared[MAX_SERIES - 1:]
        keys, rows = _align([(p[1], p[2]) for p in tail])
        other = np.nansum(np.vstack(rows), axis=0) if rows else np.array([])
        prepared = head + [({"name": "Other", "color": None, "points": []}, keys, other)]
        stacked = True
    keys, rows = _align([(p[1], p[2]) for p in prepared])
    if not np.any(np.nan_to_num(np.vstack(rows))):
        return _placeholder(ctx, title, w, h, "All values are zero for this period")
    colors = _series_colors([p[0] for p in prepared], CATEGORICAL_HEX[0])
    multi = len(prepared) > 1
    grouped = multi and not stacked and len(prepared) <= 3 and len(keys) <= 31
    stacked = multi and not grouped

    data = np.vstack([np.nan_to_num(r) for r in rows])
    totals = data.sum(axis=0) if stacked else data.max(axis=0)
    vmax = float(np.nanmax(totals)) if len(totals) else 0.0
    vmin = float(np.nanmin(data)) if data.size else 0.0
    lo = min(0.0, vmin * 1.15)
    hi = vmax * 1.18 if vmax > 0 else 1.0
    integer = bool(np.all(np.equal(np.mod(data, 1), 0)))

    fig = _new_fig(w, h)
    top = _title(fig, ctx, title, w, h)
    legend_h = 5.0 if multi else 0.0
    ylabel_w = 4.2 if unit and unit != "%" else 0.0
    left = 1.2 + ylabel_w + _y_ticks_width(lo, hi, unit, integer)
    right = 3.0
    ax = _axes(fig, w, h, left=left, right=right, top=top + legend_h + 2.0, bottom=10.0)
    ax.grid(axis="y", color=HEX_GRID, linewidth=0.6)
    ax.grid(False, axis="x")

    x = mdates.date2num(keys)
    if len(x) > 1:
        step = float(np.median(np.diff(x)))
    else:
        step = 1 / 24 if (keys[0].minute or keys[0].hour) else 1.0
    axis_w = max(w - left - right, 10)
    span = (x[-1] - x[0]) + step if len(x) else 1
    slot_mm = axis_w * step / span
    frac = min(0.72, 4.6 / max(slot_mm, 0.1))
    width = step * frac
    if grouped:
        sub = width / len(prepared)
        for i, (row, col) in enumerate(zip(data, colors)):
            off = (i - (len(prepared) - 1) / 2) * sub
            ax.bar(x + off, row, width=sub * 0.92, color=col, linewidth=0, zorder=3)
    else:
        base = np.zeros(len(x))
        for row, col in zip(data, colors):
            ax.bar(x, row, width=width, bottom=base, color=col, linewidth=0.5 if stacked else 0,
                   edgecolor="white" if stacked else col, zorder=3)
            base = base + row
    ax.set_xlim(x[0] - step * 0.6, x[-1] + step * 0.6)
    if not multi and vmax > 0:
        peak = int(np.argmax(totals))
        ax.annotate(_fmt_value(float(totals[peak]), "" if unit != "%" else "%"),
                    xy=(x[peak], totals[peak]), xytext=(0, 2.2), textcoords="offset points",
                    ha="center", va="bottom", fontsize=6.6, color=_INK)
    _style_value_axis(ax, lo, hi, unit, integer)
    _style_time_axis(ax, ctx, axis_w)
    _bar_ticks(ax, ctx, keys, x, step, slot_mm)
    if ylabel_w:
        ax.set_ylabel(unit, fontsize=6.8, color=_MUTED, labelpad=2)
    fig.text(1 - right / w, 1.2 / h, _time_context(ctx, keys[0], keys[-1]), ha="right",
             va="bottom", fontsize=6.2, color=HEX_FAINT)
    if multi:
        _legend(fig, [p[0]["name"] for p in prepared], colors, w, h, top + 2.6, left, "bars")
    return _save(fig)


def _bar_ticks(ax, ctx: _Ctx, keys: list[datetime], x: np.ndarray, step_days: float,
               slot_mm: float) -> None:
    """Ticks under the bars themselves: every bar when the slots are wide
    enough for a label, else every k-th bar with k a calendar-friendly step
    (aligned to the local clock for sub-day buckets)."""
    n = len(x)
    if n < 2 or n > 96 or slot_mm <= 0:
        return
    sub_day = step_days < 0.99
    label_mm = 11.5 if (sub_day and ctx.twelve_hour) else 8.5
    need = math.ceil(label_mm / slot_mm)
    nice = (1, 2, 3, 4, 6, 8, 12, 24) if sub_day else (1, 2, 7, 14)
    k = next((c for c in nice if c >= need), None)
    if k is None:
        return
    idx = list(range(0, n, k))
    if sub_day and k > 1:
        period = int(round(step_days * 1440)) * k
        if period and 1440 % period == 0:
            aligned = []
            for i, t in enumerate(keys):
                local = t.astimezone(ctx.tz)
                if (local.hour * 60 + local.minute) % period == 0:
                    aligned.append(i)
            if len(aligned) >= 2:
                idx = aligned
    if len(idx) < 2:
        return  # keep the date locator's ticks
    ax.set_xticks([x[i] for i in idx])


def _categorical(s, series, title, ctx, w, h, unit, kind) -> bytes:
    """Vertical columns (or a line) over named categories (no timestamps)."""
    m = series[0]
    pts = [(lab, v) for lab, v in _label_points(m["points"])]
    if not pts or not any(math.isfinite(v) for _, v in pts):
        return _placeholder(ctx, title, w, h, "No data for this period")
    labels = [_ellipsize(lab, 14) for lab, _ in pts]
    vals = np.array([v for _, v in pts], dtype=float)
    col = _mark_hex(m["color"], CATEGORICAL_HEX[0])
    status = _all_status([lab for lab, _ in pts])
    colors = [status_color(lab) if status else col for lab, _ in pts]
    lo, hi = _y_range([vals], s.get("y_domain"), zero_floor=kind in ("bars", "stacked"))
    if kind in ("bars", "stacked"):
        lo = min(lo, 0.0)
        hi = max(hi, float(np.nanmax(vals)) * 1.15 if np.nanmax(vals) > 0 else 1.0)
    integer = all(float(v).is_integer() for v in vals[np.isfinite(vals)]) and hi - lo >= 3
    fig = _new_fig(w, h)
    top = _title(fig, ctx, title, w, h)
    ylabel_w = 4.2 if unit and unit != "%" else 0.0
    left = 1.2 + ylabel_w + _y_ticks_width(lo, hi, unit, integer)
    many = len(labels) > max(4, int((w - left) / 16))
    ax = _axes(fig, w, h, left=left, right=3.0, top=top + 2.0, bottom=14.0 if many else 7.0)
    ax.grid(axis="y", color=HEX_GRID, linewidth=0.6)
    ax.grid(False, axis="x")
    x = np.arange(len(labels))
    if kind in ("bars", "stacked"):
        ax.bar(x, np.nan_to_num(vals), width=min(0.62, 0.62 * 10 / max(len(x), 1) + 0.2),
               color=colors, linewidth=0, zorder=3)
    else:
        ax.plot(x, vals, color=col, linewidth=1.5, marker="o", ms=3.0, zorder=4)
        if kind == "area":
            ax.fill_between(x, vals, lo, color=col, alpha=0.10, linewidth=0)
    ax.set_xticks(x, labels=labels)
    ax.tick_params(axis="x", labelsize=6.6, colors=_INK, length=0,
                   labelrotation=35 if many else 0)
    if many:
        for lbl in ax.get_xticklabels():
            lbl.set_ha("right")
    _style_value_axis(ax, lo, hi, unit, integer)
    if ylabel_w:
        ax.set_ylabel(unit, fontsize=6.8, color=_MUTED, labelpad=2)
    return _save(fig)


def _rank(s, series, title, ctx, w, h, unit) -> bytes:
    m = series[0]
    pts = [(lab, v) for lab, v in _label_points(m["points"]) if math.isfinite(v) and v > 0]
    if not pts:
        return _placeholder(ctx, title, w, min(h, 34), "No data for this period")
    pts.sort(key=lambda p: -p[1])
    more = max(len(pts) - MAX_RANK_BARS, 0)
    pts = pts[:MAX_RANK_BARS]
    n = len(pts)
    top_title = 6.2 if title else 0.0
    h = min(max(7.0 * n + 8.0 + top_title + (3.6 if more else 0), 24.0), 160.0)
    label_chars = max(10, min(32, int(w * 0.40 / _CHAR_MM[7.2])))
    labels = [_ellipsize(lab, label_chars) for lab, _ in pts]
    vals = [v for _, v in pts]
    texts = [_fmt_value(v, unit) for v in vals]
    col = _mark_hex(m["color"], CATEGORICAL_HEX[0])
    status = _all_status([lab for lab, _ in pts])
    colors = [status_color(lab) if status else col for lab, _ in pts]

    fig = _new_fig(w, h)
    top = _title(fig, ctx, title, w, h)
    left = min(max(_text_mm(lab, 7.2) for lab in labels) + 3.0, w * 0.45)
    right = max(_text_mm(t, 6.8) for t in texts) + 3.5
    ax = _axes(fig, w, h, left=left, right=right, top=top + 1.5,
               bottom=2.0 + (3.6 if more else 0))
    y = np.arange(n)
    bar_h = min(0.64, 4.6 / max((h - top - 4) / n, 0.1))
    ax.barh(y, vals, height=bar_h, color=colors, linewidth=0, zorder=3)
    ax.set_yticks(y, labels=labels)
    ax.invert_yaxis()
    ax.tick_params(axis="y", labelsize=7.2, labelcolor=_INK, length=0, pad=3)
    ax.set_xlim(0, max(vals) * 1.02)
    ax.xaxis.set_visible(False)
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.axvline(0, color=HEX_GRID, linewidth=0.8, zorder=2)
    for yi, v, t in zip(y, vals, texts):
        ax.annotate(t, xy=(v, yi), xytext=(3, 0), textcoords="offset points", va="center",
                    fontsize=6.8, color=_INK, annotation_clip=False)
    if more:
        fig.text(1 - 1.0 / w, 1.0 / h, f"+{more} more not shown", ha="right", va="bottom",
                 fontsize=6.2, color=HEX_FAINT)
    if unit and unit != "%":
        fig.text(left / w, 1.0 / h, unit, ha="left", va="bottom", fontsize=6.2, color=HEX_FAINT)
    return _save(fig)


def _donut(s, series, title, ctx, w, h, unit) -> bytes:
    m = series[0]
    pts = [(lab, v) for lab, v in _label_points(m["points"]) if math.isfinite(v) and v > 0]
    if not pts:
        return _placeholder(ctx, title, w, min(h, 40), "No data for this period")
    status = _all_status([lab for lab, _ in pts])
    if status:
        colors = [status_color(lab) for lab, _ in pts]
    else:
        colors = [CATEGORICAL_HEX[min(i, MAX_SERIES - 1)] for i in range(len(pts))]
    if len(pts) > MAX_SERIES:
        order = sorted(range(len(pts)), key=lambda i: -pts[i][1])
        keep = sorted(order[:MAX_SERIES - 1])
        rest = sum(pts[i][1] for i in order[MAX_SERIES - 1:])
        pts = [pts[i] for i in keep] + [("Other", rest)]
        colors = ([status_color(p[0]) for p in pts[:-1]] if status
                  else [CATEGORICAL_HEX[i] for i in range(len(pts) - 1)]) + [OTHER_HEX]
    total = sum(v for _, v in pts)

    fig = _new_fig(w, h)
    top = _title(fig, ctx, title, w, h)
    avail_h = h - top - 2.0
    d = max(min(avail_h, w * 0.45), 12.0)
    legend_w = min(max(w - d - 10, 30.0), 72.0)
    group = d + 7.0 + legend_w
    x0 = max((w - group) / 2, 1.0) if w > group + 6 else 1.0
    y0 = (avail_h - d) / 2 + 1.0
    ax = fig.add_axes([x0 / w, y0 / h, d / w, d / h])
    ax.pie([v for _, v in pts], colors=colors, startangle=90, counterclock=False,
           wedgeprops=dict(width=0.36, edgecolor="white", linewidth=1.2))
    ax.set_aspect("equal")
    ax.text(0, 0.08, _compact(total), ha="center", va="center", fontsize=10.5 if d > 30 else 8.5,
            fontweight="bold", color=_INK)
    if unit:
        ax.text(0, -0.22, _ellipsize(unit, 12), ha="center", va="center", fontsize=6.0,
                color=_MUTED)
    lx = x0 + d + 7.0
    n = len(pts)
    row = min(5.2, (avail_h - 2) / max(n, 1))
    ly = y0 + d / 2 + row * (n - 1) / 2
    label_chars = max(8, int((legend_w - 26) / _CHAR_MM[7.0]))
    for i, ((lab, val), col) in enumerate(zip(pts, colors)):
        yy = ly - i * row
        fig.add_artist(FancyBboxPatch((lx / w, (yy - 1.1) / h), 2.2 / w, 2.2 / h,
                                      boxstyle="round,pad=0,rounding_size=0.002",
                                      transform=fig.transFigure, facecolor=col,
                                      edgecolor="none"))
        fig.text((lx + 3.8) / w, yy / h, _ellipsize(lab, label_chars), ha="left", va="center",
                 fontsize=7.0, color=_INK)
        share = val / total * 100 if total else 0
        fig.text((lx + legend_w) / w, yy / h,
                 f"{_fmt_value(val, unit if unit == '%' else '')} · {share:.0f}%",
                 ha="right", va="center", fontsize=6.8, color=_MUTED)
    return _save(fig)
