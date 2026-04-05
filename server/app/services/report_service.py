"""
ZenPlus Network Monitoring - PDF Report Generator

Generates professional PDF reports for executive and technical audiences.
Uses fpdf2 for PDF construction and matplotlib for chart rendering.
"""

import io
import base64
import tempfile
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from fpdf import FPDF
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_clickhouse_client


def _safe(text) -> str:
    """Sanitize text for Helvetica (replace Unicode chars with ASCII equivalents)."""
    if text is None:
        return ""
    if isinstance(text, (bytes, bytearray)):
        s = text.decode("utf-8", errors="replace")
    else:
        s = str(text)
    s = s.replace("\u2014", "-").replace("\u2013", "-")
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2026", "...").replace("\u2022", "-").replace("\u00b7", "-")
    return s.encode("latin-1", errors="replace").decode("latin-1")


# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------
COLOR_PRIMARY = (99, 102, 241)       # #6366F1 indigo
COLOR_SUCCESS = (34, 197, 94)        # #22C55E green
COLOR_DANGER = (239, 68, 68)         # #EF4444 red
COLOR_WARNING = (249, 115, 22)       # #F97316 orange
COLOR_DEGRADED = (234, 179, 8)       # #EAB308 yellow
COLOR_TEXT = (26, 29, 39)            # #1A1D27 dark
COLOR_MUTED = (107, 114, 128)       # #6B7280 gray
COLOR_BG_TINT = (248, 250, 252)     # #F8FAFC light blue-gray
COLOR_WHITE = (255, 255, 255)

# Hex versions for matplotlib
HEX_PRIMARY = "#6366F1"
HEX_SUCCESS = "#22C55E"
HEX_DANGER = "#EF4444"
HEX_WARNING = "#F97316"
HEX_DEGRADED = "#EAB308"
HEX_TEXT = "#1A1D27"
HEX_MUTED = "#6B7280"
HEX_BG_TINT = "#F8FAFC"

STATUS_COLORS_HEX = {
    "up": HEX_SUCCESS,
    "online": HEX_SUCCESS,
    "down": HEX_DANGER,
    "offline": HEX_DANGER,
    "degraded": HEX_DEGRADED,
    "warning": HEX_WARNING,
    "critical": HEX_DANGER,
    "info": HEX_PRIMARY,
    "active": HEX_DANGER,
    "acknowledged": HEX_WARNING,
    "resolved": HEX_SUCCESS,
    "unknown": HEX_MUTED,
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

# Page geometry (A4)
PAGE_W = 210
PAGE_H = 297
MARGIN_L = 15
MARGIN_R = 15
MARGIN_T = 20
MARGIN_B = 20
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R  # 180mm


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
        label = f"{from_time:%Y-%m-%d %H:%M} - {to_time:%Y-%m-%d %H:%M} UTC"
        return from_time, to_time, label
    mapping = {
        "last_24h": (timedelta(hours=24), "Last 24 Hours"),
        "last_7d": (timedelta(days=7), "Last 7 Days"),
        "last_30d": (timedelta(days=30), "Last 30 Days"),
    }
    delta, label = mapping.get(period, (timedelta(hours=24), "Last 24 Hours"))
    return now - delta, now, label


def _fmt_ms(val: float | None) -> str:
    if val is None:
        return "-"
    return f"{val:.1f} ms"


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "-"
    return f"{val:.1f}%"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# ---------------------------------------------------------------------------
# Matplotlib chart helpers
# ---------------------------------------------------------------------------

def _setup_chart_style():
    """Apply a clean minimalist style to the current axes."""
    ax = plt.gca()
    ax.set_facecolor("white")
    ax.grid(True, axis="y", color="#E5E7EB", linewidth=0.6, linestyle="--")
    ax.grid(False, axis="x")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#D1D5DB")
    ax.spines["bottom"].set_color("#D1D5DB")
    ax.tick_params(colors=HEX_MUTED, labelsize=8)


def _chart_to_bytes(fig: plt.Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _make_donut(labels: list[str], values: list[float],
                colors: list[str], center_text: str = "") -> bytes:
    fig, ax = plt.subplots(figsize=(4, 4))
    wedges, _ = ax.pie(
        values, labels=None, colors=colors,
        startangle=90, wedgeprops=dict(width=0.45, edgecolor="white", linewidth=2),
    )
    ax.legend(
        wedges,
        [f"{l}  ({v:,.0f})" for l, v in zip(labels, values)],
        loc="lower center",
        bbox_to_anchor=(0.5, -0.12),
        fontsize=8,
        frameon=False,
        ncol=min(len(labels), 3),
    )
    if center_text:
        ax.text(0, 0, center_text, ha="center", va="center",
                fontsize=18, fontweight="bold", color=HEX_TEXT)
    ax.set_aspect("equal")
    return _chart_to_bytes(fig)


def _make_line_chart(timestamps: list[datetime], values: list[float],
                     ylabel: str = "", color: str = HEX_PRIMARY) -> bytes:
    fig, ax = plt.subplots(figsize=(8, 3.5))
    _setup_chart_style()
    ax.plot(timestamps, values, color=color, linewidth=1.4, alpha=0.9)
    ax.fill_between(timestamps, values, alpha=0.08, color=color)
    ax.set_ylabel(ylabel, fontsize=8, color=HEX_MUTED)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate(rotation=30, ha="right")
    return _chart_to_bytes(fig)


def _make_bar_chart(labels: list[str], values: list[float],
                    colors: list[str] | str = HEX_PRIMARY,
                    ylabel: str = "") -> bytes:
    fig, ax = plt.subplots(figsize=(8, 3.5))
    _setup_chart_style()
    bars = ax.bar(labels, values, color=colors, width=0.6, edgecolor="white", linewidth=0.5)
    ax.set_ylabel(ylabel, fontsize=8, color=HEX_MUTED)
    for bar, val in zip(bars, values):
        if val > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                    f"{val:,.0f}", ha="center", va="bottom", fontsize=7,
                    color=HEX_MUTED)
    return _chart_to_bytes(fig)


def _make_time_bar_chart(timestamps: list[datetime], values: list[float],
                         color: str = HEX_PRIMARY, ylabel: str = "") -> bytes:
    fig, ax = plt.subplots(figsize=(8, 3.5))
    _setup_chart_style()
    if not timestamps:
        ax.text(0.5, 0.5, "No data", ha="center", va="center",
                fontsize=12, color=HEX_MUTED, transform=ax.transAxes)
        return _chart_to_bytes(fig)
    width = 0.8 * (max(1, (timestamps[-1] - timestamps[0]).total_seconds() / len(timestamps))) / 86400
    ax.bar(timestamps, values, width=max(width, 0.02), color=color, edgecolor="white", linewidth=0.3)
    ax.set_ylabel(ylabel, fontsize=8, color=HEX_MUTED)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.autofmt_xdate(rotation=30, ha="right")
    return _chart_to_bytes(fig)


# ---------------------------------------------------------------------------
# ZenPlusReport - FPDF subclass
# ---------------------------------------------------------------------------

class ZenPlusReport(FPDF):

    def __init__(self, title: str = "ZenPlus Report",
                 company_name: str = "ZenPlus",
                 logo_bytes: bytes | None = None,
                 period_label: str = "",
                 generated_at: datetime | None = None):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_auto_page_break(auto=True, margin=MARGIN_B + 5)
        self.set_margins(MARGIN_L, MARGIN_T, MARGIN_R)
        self.report_title = title
        self.company_name = company_name
        self.period_label = period_label
        self.generated_at = generated_at or datetime.now(timezone.utc)
        self._logo_path: str | None = None
        if logo_bytes:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.write(logo_bytes)
            tmp.flush()
            self._logo_path = tmp.name
        self.set_font("Helvetica", size=10)

    # ----- header / footer -----

    def header(self):
        # colored top bar
        self.set_fill_color(*COLOR_PRIMARY)
        self.rect(0, 0, PAGE_W, 16, "F")

        # logo
        x = MARGIN_L
        if self._logo_path:
            try:
                self.image(self._logo_path, x=x, y=2, h=12)
                x += 16
            except Exception:
                pass

        # company name
        self.set_xy(x, 3)
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(255, 255, 255)
        self.cell(0, 5, _safe(self.company_name), new_x="LMARGIN")

        # report title
        self.set_xy(x, 9)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(220, 220, 255)
        self.cell(0, 4, _safe(self.report_title), new_x="LMARGIN")

        # period label right-aligned
        if self.period_label:
            self.set_xy(PAGE_W - MARGIN_R - 80, 3)
            self.set_font("Helvetica", "", 8)
            self.set_text_color(220, 220, 255)
            self.cell(80, 5, _safe(self.period_label), align="R", new_x="LMARGIN")

        self.set_y(MARGIN_T + 2)
        self.set_text_color(*COLOR_TEXT)

    def footer(self):
        self.set_y(-MARGIN_B)
        self.set_draw_color(*COLOR_PRIMARY)
        self.line(MARGIN_L, PAGE_H - MARGIN_B, PAGE_W - MARGIN_R, PAGE_H - MARGIN_B)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*COLOR_MUTED)
        self.cell(0, 8,
                  f"Generated by ZenPlus  ·  {self.generated_at:%Y-%m-%d %H:%M UTC}",
                  new_x="LMARGIN")
        self.set_xy(PAGE_W - MARGIN_R - 30, PAGE_H - MARGIN_B)
        self.cell(30, 8, f"Page {self.page_no()}/{{nb}}", align="R")

    # ----- building blocks -----

    def section_title(self, title: str):
        """Colored left-border section heading."""
        self._check_page_space(14)
        self.ln(4)
        y = self.get_y()
        self.set_fill_color(*COLOR_PRIMARY)
        self.rect(MARGIN_L, y, 3, 9, "F")
        self.set_xy(MARGIN_L + 6, y)
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(*COLOR_TEXT)
        self.cell(0, 9, _safe(title), new_x="LMARGIN")
        self.ln(12)

    def sub_heading(self, text_str: str):
        self._check_page_space(10)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*COLOR_TEXT)
        self.cell(0, 7, _safe(text_str), new_x="LMARGIN")
        self.ln(8)

    def body_text(self, text_str: str):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*COLOR_TEXT)
        self.multi_cell(CONTENT_W, 5, _safe(text_str))
        self.ln(2)

    def muted_text(self, text_str: str):
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*COLOR_MUTED)
        self.multi_cell(CONTENT_W, 4, _safe(text_str))
        self.ln(1)

    def kpi_row(self, kpis: list[tuple[str, str, tuple]]):
        """
        Render a row of KPI boxes.
        Each kpi: (label, value, accent_color_rgb)
        """
        self._check_page_space(30)
        n = len(kpis)
        if n == 0:
            return
        gap = 4
        box_w = (CONTENT_W - gap * (n - 1)) / n
        box_h = 24
        y_start = self.get_y()

        for i, (label, value, color) in enumerate(kpis):
            x = MARGIN_L + i * (box_w + gap)
            # background
            self.set_fill_color(*COLOR_BG_TINT)
            self.rect(x, y_start, box_w, box_h, "F")
            # accent top line
            self.set_fill_color(*color)
            self.rect(x, y_start, box_w, 2, "F")
            # value
            self.set_xy(x + 3, y_start + 4)
            self.set_font("Helvetica", "B", 16)
            self.set_text_color(*color)
            self.cell(box_w - 6, 8, _safe(str(value)), align="C")
            # label
            self.set_xy(x + 3, y_start + 14)
            self.set_font("Helvetica", "", 7)
            self.set_text_color(*COLOR_MUTED)
            self.cell(box_w - 6, 5, _safe(label), align="C")

        self.set_y(y_start + box_h + 4)

    def data_table(self, headers: list[str], rows: list[list[str]],
                   col_widths: list[float] | None = None,
                   max_rows: int = 50):
        """Alternating-row table."""
        self._check_page_space(20)
        n_cols = len(headers)
        if col_widths is None:
            col_widths = [CONTENT_W / n_cols] * n_cols
        row_h = 6

        # header
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(*COLOR_PRIMARY)
        self.set_text_color(255, 255, 255)
        x_start = MARGIN_L
        for i, h in enumerate(headers):
            self.set_xy(x_start + sum(col_widths[:i]), self.get_y())
            self.cell(col_widths[i], row_h + 1, _safe(h), border=0, fill=True)
        self.ln(row_h + 1)

        # rows
        self.set_font("Helvetica", "", 7.5)
        for ri, row in enumerate(rows[:max_rows]):
            if self.get_y() + row_h > PAGE_H - MARGIN_B - 5:
                self.add_page()
            fill = ri % 2 == 1
            if fill:
                self.set_fill_color(*COLOR_BG_TINT)
            self.set_text_color(*COLOR_TEXT)
            for i, cell_val in enumerate(row):
                self.set_xy(x_start + sum(col_widths[:i]), self.get_y())
                self.cell(col_widths[i], row_h, _safe(str(cell_val)[:40]), border=0, fill=fill)
            self.ln(row_h)

        self.ln(3)

    def add_chart(self, chart_png: bytes, w: float = 160, caption: str = ""):
        """Embed a matplotlib chart PNG in the PDF."""
        needed = 90 if not caption else 95
        self._check_page_space(needed)
        x = MARGIN_L + (CONTENT_W - w) / 2
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(chart_png)
        tmp.flush()
        self.image(tmp.name, x=x, y=self.get_y(), w=w)
        # estimate height based on aspect - default charts are ~8:3.5
        h_est = w * 0.5
        self.set_y(self.get_y() + h_est + 2)
        if caption:
            self.set_font("Helvetica", "I", 7)
            self.set_text_color(*COLOR_MUTED)
            self.cell(CONTENT_W, 4, _safe(caption), align="C", new_x="LMARGIN")
            self.ln(5)

    def add_donut_pair(self, left_png: bytes, right_png: bytes,
                       left_caption: str = "", right_caption: str = ""):
        """Place two donut charts side by side."""
        self._check_page_space(80)
        donut_w = 75
        gap = 10
        y_start = self.get_y()
        x_left = MARGIN_L + (CONTENT_W / 2 - donut_w) / 2
        x_right = MARGIN_L + CONTENT_W / 2 + (CONTENT_W / 2 - donut_w) / 2

        for png_bytes, x, cap in [
            (left_png, x_left, left_caption),
            (right_png, x_right, right_caption),
        ]:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.write(png_bytes)
            tmp.flush()
            self.image(tmp.name, x=x, y=y_start, w=donut_w)

        h_est = donut_w * 1.05
        self.set_y(y_start + h_est + 2)
        if left_caption or right_caption:
            self.set_font("Helvetica", "I", 7)
            self.set_text_color(*COLOR_MUTED)
            self.set_x(MARGIN_L)
            self.cell(CONTENT_W / 2, 4, _safe(left_caption), align="C")
            self.cell(CONTENT_W / 2, 4, _safe(right_caption), align="C", new_x="LMARGIN")
            self.ln(6)

    def status_badge(self, status: str, x: float | None = None, y: float | None = None):
        """Render a small colored status badge inline."""
        color = STATUS_COLORS_RGB.get(status.lower(), COLOR_MUTED)
        if x is not None and y is not None:
            self.set_xy(x, y)
        self.set_fill_color(*color)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 7)
        self.cell(18, 5, _safe(status.upper()), align="C", fill=True, new_x="END")
        self.set_text_color(*COLOR_TEXT)

    def _check_page_space(self, needed_mm: float):
        if self.get_y() + needed_mm > PAGE_H - MARGIN_B - 5:
            self.add_page()


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

async def _fetch_company_info(db: AsyncSession) -> dict:
    """Get company name and logo from system_settings."""
    result = await db.execute(
        text("SELECT value FROM system_settings WHERE key = 'company'")
    )
    row = result.fetchone()
    info = {"company_name": "ZenPlus", "logo_bytes": None, "timezone": "UTC"}
    if row and row[0]:
        val = row[0] if isinstance(row[0], dict) else {}
        info["company_name"] = val.get("company_name", "ZenPlus")
        info["timezone"] = val.get("timezone", "UTC")

    result2 = await db.execute(
        text("SELECT value FROM system_settings WHERE key = 'company_logo'")
    )
    row2 = result2.fetchone()
    if row2 and row2[0]:
        val2 = row2[0] if isinstance(row2[0], dict) else {}
        logo_b64 = val2.get("data") or val2.get("logo") or ""
        if isinstance(row2[0], str):
            logo_b64 = row2[0]
        if logo_b64:
            try:
                # strip data URI prefix if present
                if "," in logo_b64:
                    logo_b64 = logo_b64.split(",", 1)[1]
                info["logo_bytes"] = base64.b64decode(logo_b64)
            except Exception:
                pass
    return info


def _build_device_filter_sql(device_ids: list[str] | None,
                             group_ids: list[str] | None,
                             alias: str = "d") -> tuple[str, dict]:
    """Build WHERE clause fragments and params for device filtering."""
    clauses = []
    params: dict = {}
    if device_ids:
        clauses.append(f"{alias}.id = ANY(:device_ids)")
        params["device_ids"] = device_ids
    if group_ids:
        clauses.append(f"{alias}.group_id = ANY(:group_ids)")
        params["group_ids"] = group_ids
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
                         group_ids: list[str] | None = None) -> list[dict]:
    filt, params = _build_device_filter_sql(device_ids, group_ids, "d")
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


async def _fetch_service_checks(db: AsyncSession) -> list[dict]:
    q = text("""
        SELECT id, name, check_type, target_host, target_url, status,
               last_check_at, last_response_ms, last_error,
               tls_days_remaining, tls_expiry_date
        FROM service_checks
        ORDER BY name
    """)
    result = await db.execute(q)
    return [dict(r._mapping) for r in result.fetchall()]


async def _fetch_alerts(db: AsyncSession, start: datetime, end: datetime,
                        device_ids: list[str] | None = None) -> list[dict]:
    dev_clause = ""
    params: dict = {"start": start, "end": end}
    if device_ids:
        dev_clause = "AND a.device_id = ANY(:device_ids)"
        params["device_ids"] = device_ids
    q = text(f"""
        SELECT a.id, a.device_id, a.status, a.severity, a.message,
               a.triggered_at, a.acknowledged_at, a.resolved_at,
               d.hostname
        FROM alerts a
        LEFT JOIN devices d ON d.id = a.device_id
        WHERE a.triggered_at >= :start AND a.triggered_at <= :end
        {dev_clause}
        ORDER BY a.triggered_at DESC
    """)
    result = await db.execute(q, params)
    return [dict(r._mapping) for r in result.fetchall()]


def _fetch_ping_metrics(start: datetime, end: datetime,
                        device_ids: list[str] | None = None) -> list[dict]:
    client = get_clickhouse_client()
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


def _fetch_service_metrics(start: datetime, end: datetime,
                           service_ids: list[str] | None = None) -> list[dict]:
    client = get_clickhouse_client()
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
    client = get_clickhouse_client()
    dev_filt = _ch_device_filter(device_ids)
    q = f"""
        SELECT device_id, timestamp, old_status, new_status, reason, duration_sec
        FROM zenplus.device_status_log
        WHERE timestamp >= %(start)s AND timestamp <= %(end)s
        {dev_filt}
        ORDER BY timestamp
    """
    result = client.query(q, parameters={"start": start, "end": end})
    cols = result.column_names
    return [dict(zip(cols, row)) for row in result.result_rows]


def _fetch_service_status_log(start: datetime, end: datetime,
                              service_ids: list[str] | None = None) -> list[dict]:
    client = get_clickhouse_client()
    svc_filt = _ch_service_filter(service_ids)
    q = f"""
        SELECT service_check_id, timestamp, old_status, new_status, reason, duration_sec
        FROM zenplus.service_status_log
        WHERE timestamp >= %(start)s AND timestamp <= %(end)s
        {svc_filt}
        ORDER BY timestamp
    """
    result = client.query(q, parameters={"start": start, "end": end})
    cols = result.column_names
    return [dict(zip(cols, row)) for row in result.result_rows]


# ---------------------------------------------------------------------------
# Metric aggregation helpers
# ---------------------------------------------------------------------------

def _device_uptime_pct(ping_rows: list[dict], device_id: str) -> float:
    rows = [r for r in ping_rows if str(r["device_id"]) == str(device_id)]
    if not rows:
        return 0.0
    up = sum(1 for r in rows if r.get("is_up"))
    return (up / len(rows)) * 100


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


def _service_uptime_pct(svc_rows: list[dict], svc_id: str) -> float:
    rows = [r for r in svc_rows if str(r["service_check_id"]) == str(svc_id)]
    if not rows:
        return 0.0
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
# Report section builders
# ---------------------------------------------------------------------------

async def _build_executive_summary(pdf: ZenPlusReport, db: AsyncSession,
                                   start: datetime, end: datetime,
                                   device_ids: list[str] | None,
                                   group_ids: list[str] | None):
    devices = await _fetch_devices(db, device_ids, group_ids)
    services = await _fetch_service_checks(db)
    filtered_device_ids = [str(d["id"]) for d in devices] if devices else device_ids
    alerts = await _fetch_alerts(db, start, end, filtered_device_ids)
    ping_rows = _fetch_ping_metrics(start, end, filtered_device_ids)
    svc_ids = [str(s["id"]) for s in services]
    svc_rows = _fetch_service_metrics(start, end, svc_ids)

    pdf.add_page()
    pdf.section_title("Executive Summary")

    # --- KPI boxes ---
    total_devices = len(devices)
    online_devices = sum(1 for d in devices if (d.get("status") or "").lower() in ("up", "online"))
    online_pct = (online_devices / total_devices * 100) if total_devices else 0

    all_rtts = [r["rtt_ms"] for r in ping_rows if r.get("rtt_ms") and r["rtt_ms"] > 0]
    avg_rtt = statistics.mean(all_rtts) if all_rtts else 0

    total_alerts = len(alerts)
    mttr = _mttr_seconds(alerts)

    pdf.kpi_row([
        ("Total Devices", str(total_devices), COLOR_PRIMARY),
        ("Online", _fmt_pct(online_pct), COLOR_SUCCESS),
        ("Avg RTT", _fmt_ms(avg_rtt), COLOR_PRIMARY),
        ("Alerts", str(total_alerts), COLOR_DANGER if total_alerts > 0 else COLOR_MUTED),
        ("MTTR", _fmt_duration(mttr), COLOR_WARNING),
    ])
    pdf.ln(4)

    # --- Donut charts: device status + service status ---
    # device status distribution
    dev_status_counts: dict[str, int] = {}
    for d in devices:
        s = (d.get("status") or "unknown").lower()
        dev_status_counts[s] = dev_status_counts.get(s, 0) + 1

    dev_labels = list(dev_status_counts.keys())
    dev_values = list(dev_status_counts.values())
    dev_colors = [STATUS_COLORS_HEX.get(l, HEX_MUTED) for l in dev_labels]
    dev_center = f"{online_pct:.0f}%"
    dev_donut_png = _make_donut(
        [l.capitalize() for l in dev_labels], dev_values, dev_colors, dev_center
    )

    # service status distribution
    svc_status_counts: dict[str, int] = {}
    for s in services:
        st = (s.get("status") or "unknown").lower()
        svc_status_counts[st] = svc_status_counts.get(st, 0) + 1

    svc_labels = list(svc_status_counts.keys())
    svc_values = list(svc_status_counts.values())
    svc_colors = [STATUS_COLORS_HEX.get(l, HEX_MUTED) for l in svc_labels]
    svc_up = sum(1 for s in services if (s.get("status") or "").lower() in ("up", "online"))
    svc_pct = (svc_up / len(services) * 100) if services else 0
    svc_donut_png = _make_donut(
        [l.capitalize() for l in svc_labels], svc_values, svc_colors, f"{svc_pct:.0f}%"
    )

    pdf.sub_heading("Status Distribution")
    pdf.add_donut_pair(dev_donut_png, svc_donut_png,
                       "Device Status", "Service Status")

    # --- Top 5 problematic devices ---
    pdf.section_title("Top Problematic Devices")
    device_alert_count: dict[str, int] = {}
    device_hostname: dict[str, str] = {}
    for a in alerts:
        did = str(a.get("device_id", ""))
        device_alert_count[did] = device_alert_count.get(did, 0) + 1
        if a.get("hostname"):
            device_hostname[did] = a["hostname"]

    # also factor in downtime from status log
    status_log = _fetch_device_status_log(start, end, filtered_device_ids)
    device_downtime: dict[str, float] = {}
    for entry in status_log:
        did = str(entry["device_id"])
        if (entry.get("new_status") or "").lower() in ("down", "offline"):
            device_downtime[did] = device_downtime.get(did, 0) + (entry.get("duration_sec") or 0)

    # score = alerts * 10 + downtime_minutes
    problem_scores: dict[str, float] = {}
    all_device_ids_set = set(str(d["id"]) for d in devices)
    for did in all_device_ids_set:
        score = device_alert_count.get(did, 0) * 10 + device_downtime.get(did, 0) / 60
        if score > 0:
            problem_scores[did] = score

    top5 = sorted(problem_scores.items(), key=lambda x: x[1], reverse=True)[:5]
    if top5:
        dev_map = {str(d["id"]): d for d in devices}
        headers = ["Hostname", "IP", "Status", "Alerts", "Downtime", "Avg RTT"]
        rows = []
        for did, score in top5:
            d = dev_map.get(did, {})
            stats = _device_rtt_stats(ping_rows, did)
            rows.append([
                d.get("hostname", "-"),
                d.get("ip_address", "-"),
                (d.get("status") or "-").upper(),
                str(device_alert_count.get(did, 0)),
                _fmt_duration(device_downtime.get(did, 0)),
                _fmt_ms(stats["avg"]),
            ])
        pdf.data_table(headers, rows, col_widths=[40, 30, 20, 20, 30, 30])
    else:
        pdf.muted_text("No problematic devices detected in this period.")

    # --- Alert summary ---
    pdf.section_title("Alert Summary")
    sev_counts: dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    for a in alerts:
        sev = (a.get("severity") or "info").lower()
        sev_counts[sev] = sev_counts.get(sev, 0) + 1

    pdf.data_table(
        ["Severity", "Count", "Percentage"],
        [
            ["CRITICAL", str(sev_counts.get("critical", 0)),
             _fmt_pct(sev_counts.get("critical", 0) / max(total_alerts, 1) * 100)],
            ["WARNING", str(sev_counts.get("warning", 0)),
             _fmt_pct(sev_counts.get("warning", 0) / max(total_alerts, 1) * 100)],
            ["INFO", str(sev_counts.get("info", 0)),
             _fmt_pct(sev_counts.get("info", 0) / max(total_alerts, 1) * 100)],
        ],
        col_widths=[60, 60, 60],
    )


async def _build_device_health(pdf: ZenPlusReport, db: AsyncSession,
                               start: datetime, end: datetime,
                               device_ids: list[str] | None,
                               group_ids: list[str] | None):
    devices = await _fetch_devices(db, device_ids, group_ids)
    filtered_device_ids = [str(d["id"]) for d in devices] if devices else device_ids
    ping_rows = _fetch_ping_metrics(start, end, filtered_device_ids)
    status_log = _fetch_device_status_log(start, end, filtered_device_ids)

    pdf.add_page()
    pdf.section_title("Device Health Report")

    if not devices:
        pdf.muted_text("No devices found matching the filter criteria.")
        return

    # --- Group summary table ---
    group_summary: dict[str, dict] = {}
    for d in devices:
        gn = d.get("group_name") or "Ungrouped"
        if gn not in group_summary:
            group_summary[gn] = {"total": 0, "online": 0}
        group_summary[gn]["total"] += 1
        if (d.get("status") or "").lower() in ("up", "online"):
            group_summary[gn]["online"] += 1

    pdf.sub_heading("Group Summary")
    pdf.data_table(
        ["Group", "Total Devices", "Online", "Online %"],
        [
            [gn, str(info["total"]), str(info["online"]),
             _fmt_pct(info["online"] / max(info["total"], 1) * 100)]
            for gn, info in sorted(group_summary.items())
        ],
        col_widths=[55, 40, 40, 45],
    )

    # --- Per-device details ---
    pdf.section_title("Device Details")

    for d in devices:
        did = str(d["id"])
        pdf._check_page_space(40)

        # device info line
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*COLOR_TEXT)
        hostname = d.get("hostname") or "Unknown"
        ip = d.get("ip_address") or ""
        dtype = d.get("device_type") or ""
        location = d.get("location") or ""
        group = d.get("group_name") or ""

        pdf.cell(CONTENT_W, 6,
                 f"{hostname}  |  {ip}  |  {dtype}  |  {group}  |  {location}",
                 new_x="LMARGIN")
        pdf.ln(7)

        # stats
        uptime = _device_uptime_pct(ping_rows, did)
        stats = _device_rtt_stats(ping_rows, did)

        pdf.kpi_row([
            ("Uptime", _fmt_pct(uptime), COLOR_SUCCESS if uptime >= 99 else COLOR_WARNING),
            ("Avg RTT", _fmt_ms(stats["avg"]), COLOR_PRIMARY),
            ("Min RTT", _fmt_ms(stats["min"]), COLOR_SUCCESS),
            ("Max RTT", _fmt_ms(stats["max"]), COLOR_DANGER),
            ("P95 RTT", _fmt_ms(stats["p95"]), COLOR_WARNING),
        ])

        # RTT line chart
        dev_pings = [r for r in ping_rows if str(r["device_id"]) == did and r.get("rtt_ms")]
        if len(dev_pings) > 2:
            ts = [r["timestamp"] for r in dev_pings]
            rtt_vals = [r["rtt_ms"] for r in dev_pings]
            chart_png = _make_line_chart(ts, rtt_vals, ylabel="RTT (ms)")
            pdf.add_chart(chart_png, w=160, caption=f"RTT over time - {hostname}")

        # status change log for this device
        dev_log = [e for e in status_log if str(e["device_id"]) == did]
        if dev_log:
            pdf._check_page_space(20)
            pdf.sub_heading(f"Status Changes - {hostname}")
            pdf.data_table(
                ["Time", "From", "To", "Reason", "Duration"],
                [
                    [
                        e["timestamp"].strftime("%Y-%m-%d %H:%M") if hasattr(e["timestamp"], "strftime") else str(e["timestamp"]),
                        (e.get("old_status") or "-").upper(),
                        (e.get("new_status") or "-").upper(),
                        (e.get("reason") or "-")[:30],
                        _fmt_duration(e.get("duration_sec")),
                    ]
                    for e in dev_log[:15]
                ],
                col_widths=[35, 25, 25, 55, 30],
                max_rows=15,
            )

        pdf.ln(4)


async def _build_service_health(pdf: ZenPlusReport, db: AsyncSession,
                                start: datetime, end: datetime):
    services = await _fetch_service_checks(db)
    svc_ids = [str(s["id"]) for s in services]
    svc_rows = _fetch_service_metrics(start, end, svc_ids)
    svc_log = _fetch_service_status_log(start, end, svc_ids)

    pdf.add_page()
    pdf.section_title("Service Health Report")

    if not services:
        pdf.muted_text("No service checks configured.")
        return

    # --- Overview table ---
    pdf.sub_heading("Service Overview")
    overview_rows = []
    for s in services:
        sid = str(s["id"])
        uptime = _service_uptime_pct(svc_rows, sid)
        resp_times = [r["response_ms"] for r in svc_rows
                      if str(r["service_check_id"]) == sid and r.get("response_ms")]
        avg_resp = statistics.mean(resp_times) if resp_times else None
        tls_info = ""
        if s.get("tls_days_remaining") is not None:
            days = s["tls_days_remaining"]
            tls_info = f"{days}d" if days >= 0 else "EXPIRED"
        overview_rows.append([
            s.get("name", "-"),
            (s.get("check_type") or "-").upper(),
            (s.get("status") or "-").upper(),
            _fmt_pct(uptime),
            _fmt_ms(avg_resp),
            tls_info or "-",
        ])

    pdf.data_table(
        ["Name", "Type", "Status", "Uptime", "Avg Response", "TLS"],
        overview_rows,
        col_widths=[40, 22, 20, 25, 30, 25],
    )

    # --- Per-service details ---
    pdf.section_title("Service Details")

    for s in services:
        sid = str(s["id"])
        pdf._check_page_space(30)

        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*COLOR_TEXT)
        target = s.get("target_url") or s.get("target_host") or ""
        pdf.cell(CONTENT_W, 6,
                 f"{s.get('name', 'Unknown')}  |  {(s.get('check_type') or '').upper()}  |  {target}",
                 new_x="LMARGIN")
        pdf.ln(7)

        uptime = _service_uptime_pct(svc_rows, sid)
        resp_times = [r["response_ms"] for r in svc_rows
                      if str(r["service_check_id"]) == sid and r.get("response_ms")]
        avg_resp = statistics.mean(resp_times) if resp_times else None

        kpis = [
            ("Uptime", _fmt_pct(uptime), COLOR_SUCCESS if uptime >= 99 else COLOR_WARNING),
            ("Avg Response", _fmt_ms(avg_resp), COLOR_PRIMARY),
        ]
        if s.get("tls_days_remaining") is not None:
            days = s["tls_days_remaining"]
            color = COLOR_SUCCESS if days > 30 else (COLOR_WARNING if days > 7 else COLOR_DANGER)
            expiry = s.get("tls_expiry_date")
            exp_str = expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry or "")
            kpis.append(("TLS Expiry", f"{days}d ({exp_str})", color))

        pdf.kpi_row(kpis)

        # response time chart
        svc_metrics = [r for r in svc_rows
                       if str(r["service_check_id"]) == sid and r.get("response_ms")]
        if len(svc_metrics) > 2:
            ts = [r["timestamp"] for r in svc_metrics]
            vals = [r["response_ms"] for r in svc_metrics]
            chart_png = _make_line_chart(ts, vals, ylabel="Response (ms)", color=HEX_PRIMARY)
            pdf.add_chart(chart_png, w=160,
                          caption=f"Response time - {s.get('name', '')}")

        # status log
        s_log = [e for e in svc_log if str(e["service_check_id"]) == sid]
        if s_log:
            pdf._check_page_space(20)
            pdf.data_table(
                ["Time", "From", "To", "Reason", "Duration"],
                [
                    [
                        e["timestamp"].strftime("%Y-%m-%d %H:%M") if hasattr(e["timestamp"], "strftime") else str(e["timestamp"]),
                        (e.get("old_status") or "-").upper(),
                        (e.get("new_status") or "-").upper(),
                        (e.get("reason") or "-")[:30],
                        _fmt_duration(e.get("duration_sec")),
                    ]
                    for e in s_log[:10]
                ],
                col_widths=[35, 25, 25, 55, 30],
                max_rows=10,
            )

        pdf.ln(3)


async def _build_alert_analysis(pdf: ZenPlusReport, db: AsyncSession,
                                start: datetime, end: datetime,
                                device_ids: list[str] | None):
    alerts = await _fetch_alerts(db, start, end, device_ids)

    pdf.add_page()
    pdf.section_title("Alert Analysis")

    if not alerts:
        pdf.muted_text("No alerts found for the selected period and filters.")
        return

    total = len(alerts)
    active = sum(1 for a in alerts if (a.get("status") or "").lower() == "active")
    acked = sum(1 for a in alerts if (a.get("status") or "").lower() == "acknowledged")
    resolved = sum(1 for a in alerts if (a.get("status") or "").lower() == "resolved")
    mttr = _mttr_seconds(alerts)

    pdf.kpi_row([
        ("Total Alerts", str(total), COLOR_PRIMARY),
        ("Active", str(active), COLOR_DANGER),
        ("Acknowledged", str(acked), COLOR_WARNING),
        ("Resolved", str(resolved), COLOR_SUCCESS),
        ("MTTR", _fmt_duration(mttr), COLOR_PRIMARY),
    ])
    pdf.ln(2)

    # --- Severity breakdown pie ---
    sev_counts: dict[str, int] = {}
    for a in alerts:
        sev = (a.get("severity") or "info").lower()
        sev_counts[sev] = sev_counts.get(sev, 0) + 1

    if sev_counts:
        labels = list(sev_counts.keys())
        values = list(sev_counts.values())
        colors = [STATUS_COLORS_HEX.get(l, HEX_MUTED) for l in labels]
        donut_png = _make_donut([l.capitalize() for l in labels], values, colors)
        pdf.sub_heading("Severity Breakdown")
        pdf.add_chart(donut_png, w=75, caption="Alert Severity Distribution")

    # --- Alert volume over time ---
    pdf.sub_heading("Alert Volume Over Time")
    # bucket by day
    day_counts: dict[str, int] = {}
    for a in alerts:
        t = a.get("triggered_at")
        if t:
            day_key = t.strftime("%Y-%m-%d") if hasattr(t, "strftime") else str(t)[:10]
            day_counts[day_key] = day_counts.get(day_key, 0) + 1

    if day_counts:
        sorted_days = sorted(day_counts.keys())
        ts = [datetime.strptime(d, "%Y-%m-%d") for d in sorted_days]
        vals = [day_counts[d] for d in sorted_days]
        chart_png = _make_time_bar_chart(ts, vals, color=HEX_DANGER, ylabel="Alerts")
        pdf.add_chart(chart_png, w=160, caption="Daily alert volume")

    # --- Top alerting devices ---
    pdf.sub_heading("Top Alerting Devices")
    host_counts: dict[str, int] = {}
    for a in alerts:
        h = a.get("hostname") or "Unknown"
        host_counts[h] = host_counts.get(h, 0) + 1

    top_hosts = sorted(host_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    if top_hosts:
        labels_bar = [h for h, _ in top_hosts]
        vals_bar = [c for _, c in top_hosts]
        chart_png = _make_bar_chart(labels_bar, vals_bar, colors=HEX_DANGER, ylabel="Alerts")
        pdf.add_chart(chart_png, w=160, caption="Alerts by device")

    # --- MTTR analysis ---
    pdf.sub_heading("MTTR Analysis")
    # MTTR per severity
    mttr_by_sev: dict[str, list[float]] = {}
    for a in alerts:
        if a.get("resolved_at") and a.get("triggered_at"):
            dur = (a["resolved_at"] - a["triggered_at"]).total_seconds()
            if dur > 0:
                sev = (a.get("severity") or "info").lower()
                mttr_by_sev.setdefault(sev, []).append(dur)

    mttr_rows = []
    for sev in ("critical", "warning", "info"):
        durations = mttr_by_sev.get(sev, [])
        if durations:
            mttr_rows.append([
                sev.upper(),
                str(len(durations)),
                _fmt_duration(statistics.mean(durations)),
                _fmt_duration(min(durations)),
                _fmt_duration(max(durations)),
            ])
        else:
            mttr_rows.append([sev.upper(), "0", "-", "-", "-"])

    pdf.data_table(
        ["Severity", "Resolved", "Avg MTTR", "Min MTTR", "Max MTTR"],
        mttr_rows,
        col_widths=[36, 36, 36, 36, 36],
    )

    # --- Alert details table ---
    pdf.section_title("Recent Alerts")
    alert_rows = []
    for a in alerts[:30]:
        triggered = a.get("triggered_at")
        trig_str = triggered.strftime("%m-%d %H:%M") if hasattr(triggered, "strftime") else str(triggered)[:16]
        alert_rows.append([
            trig_str,
            a.get("hostname") or "-",
            (a.get("severity") or "-").upper(),
            (a.get("status") or "-").upper(),
            (a.get("message") or "-")[:45],
        ])

    pdf.data_table(
        ["Triggered", "Device", "Severity", "Status", "Message"],
        alert_rows,
        col_widths=[28, 35, 22, 25, 70],
        max_rows=30,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def generate_report(
    db: AsyncSession,
    report_type: str,          # 'executive_summary' | 'device_health' | 'service_health' | 'alert_analysis' | 'full_report'
    period: str,               # 'last_24h' | 'last_7d' | 'last_30d' | 'custom'
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    device_ids: list[str] | None = None,
    group_ids: list[str] | None = None,
) -> bytes:
    """Generate a professional PDF report and return the raw PDF bytes."""

    start, end, period_label = _resolve_period(period, from_time, to_time)

    # Fetch company branding
    company = await _fetch_company_info(db)

    title_map = {
        "executive_summary": "Executive Summary",
        "device_health": "Device Health Report",
        "service_health": "Service Health Report",
        "alert_analysis": "Alert Analysis Report",
        "full_report": "Full Network Report",
    }
    title = title_map.get(report_type, "ZenPlus Report")

    pdf = ZenPlusReport(
        title=title,
        company_name=company["company_name"],
        logo_bytes=company["logo_bytes"],
        period_label=period_label,
    )
    pdf.alias_nb_pages()

    # Resolve filtered device IDs for group-based filtering
    resolved_device_ids = device_ids
    if group_ids and not device_ids:
        devices = await _fetch_devices(db, device_ids=None, group_ids=group_ids)
        resolved_device_ids = [str(d["id"]) for d in devices]

    # Build requested sections
    if report_type == "executive_summary":
        await _build_executive_summary(pdf, db, start, end, resolved_device_ids, group_ids)

    elif report_type == "device_health":
        await _build_device_health(pdf, db, start, end, resolved_device_ids, group_ids)

    elif report_type == "service_health":
        await _build_service_health(pdf, db, start, end)

    elif report_type == "alert_analysis":
        await _build_alert_analysis(pdf, db, start, end, resolved_device_ids)

    elif report_type == "full_report":
        await _build_executive_summary(pdf, db, start, end, resolved_device_ids, group_ids)
        await _build_device_health(pdf, db, start, end, resolved_device_ids, group_ids)
        await _build_service_health(pdf, db, start, end)
        await _build_alert_analysis(pdf, db, start, end, resolved_device_ids)

    else:
        pdf.add_page()
        pdf.body_text(f"Unknown report type: {report_type}")

    return pdf.output()
