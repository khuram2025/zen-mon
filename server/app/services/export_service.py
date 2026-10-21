"""
ZenPlus Network Monitoring - Excel / CSV Export Service

Generates professional Excel (.xlsx) and CSV exports for network monitoring
reports, reusing the data-fetching layer from report_service.

Exports carry the organisation's branding (Settings → General → Company):
the company name and period on a cover sheet (Excel) or a header block (CSV),
timestamps in the organisation's timezone with the zone in the column header,
and the brand colour on header rows. Text cells are protected against
spreadsheet formula injection.
"""

import csv
import io
import ipaddress
import logging
import math
import uuid as _uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import report_colors as rc
from app.services.branding import Branding
from app.services.report_xlsx import excel_datetime_format, safe_text

logger = logging.getLogger(__name__)


def _coerce_cell(v):
    """Coerce non-primitive values (IPv4Address, UUID, Decimal, set) to forms
    openpyxl can serialize; text is made spreadsheet-safe (control characters
    removed, formula triggers neutralised)."""
    if v is None:
        return None
    if isinstance(v, (ipaddress.IPv4Address, ipaddress.IPv6Address, _uuid.UUID)):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (set, frozenset, tuple)):
        return safe_text(", ".join(str(x) for x in v))
    if isinstance(v, (list, dict)):
        return safe_text(str(v))
    if isinstance(v, str):
        return safe_text(v)
    return v


def _csv_cell(v):
    """CSV cell: same injection protection as the workbook."""
    v = _coerce_cell(v)
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return "" if v is None else v

from app.services.report_service import (
    _resolve_period,
    _fetch_devices,
    _fetch_service_checks,
    _fetch_alerts,
    _fetch_ping_metrics,
    _fetch_service_metrics,
    _fetch_device_status_log,
    _fetch_service_status_log,
    _device_uptime_pct,
    _device_rtt_stats,
    _service_uptime_pct,
    _mttr_seconds,
    _fmt_ms,
    _fmt_pct,
    _fmt_duration,
)


def _fmt_uptime(val: float | None) -> str:
    """Uptime with two decimals, truncated like the availability headlines of
    the Availability page and the device uptime stats: any downtime never
    reads 100.00%."""
    if val is None:
        return _fmt_pct(None)
    if val >= 100:
        return "100.00%"
    return f"{min(math.floor(val * 100 + 1e-7) / 100, 99.99):.2f}%"

# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------
_ALT_ROW_FILL = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
_DEFAULT_FONT = Font(name="Calibri", size=11)
_KPI_LABEL_FONT = Font(name="Calibri", bold=True, size=12)
_MUTED_FONT = Font(name="Calibri", size=10, color="64748B")
_THIN_BORDER = Border(
    bottom=Side(style="thin", color="E2E8F0"),
)

VALID_REPORT_TYPES = {
    "executive_summary",
    "device_health",
    "service_health",
    "alert_analysis",
    "full_report",
}

_REPORT_TITLES = {
    "executive_summary": "Executive Summary",
    "device_health": "Device Health Report",
    "service_health": "Service Health Report",
    "alert_analysis": "Alert Analysis Report",
    "full_report": "Full Network Report",
}


@dataclass(frozen=True)
class _XlStyle:
    """Per-workbook styling derived from the organisation's branding."""
    header_fill: PatternFill
    header_font: Font
    title_font: Font
    kpi_value_font: Font
    datetime_format: str

    @classmethod
    def for_branding(cls, branding: Optional[Branding]) -> "_XlStyle":
        b = branding or Branding()
        brand = rc.normalize(b.brand_color)
        ink = rc.ink(brand, 3.0)[1:]
        return cls(
            header_fill=PatternFill(start_color=brand[1:], end_color=brand[1:], fill_type="solid"),
            header_font=Font(name="Calibri", bold=True, color=rc.text_on(brand)[1:], size=11),
            title_font=Font(name="Calibri", bold=True, size=14, color=ink),
            kpi_value_font=Font(name="Calibri", bold=True, size=14, color=ink),
            datetime_format=excel_datetime_format(b, seconds=True),
        )


_DEFAULT_STYLE = _XlStyle.for_branding(None)


# ---------------------------------------------------------------------------
# Time formatting (organisation timezone)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Clock:
    """Converts stored UTC timestamps to the organisation's timezone. Excel
    receives real (naive local) datetimes formatted in the organisation's
    date format; CSV receives 'YYYY-MM-DD HH:MM:SS' local strings. The zone
    is named in each timestamp column header."""
    branding: Branding
    label: str
    excel: bool

    def __call__(self, val):
        if val is None or val == "":
            return ""
        if isinstance(val, str):
            try:
                val = datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                return val
        if isinstance(val, datetime):
            local = self.branding.localize(val).replace(tzinfo=None)
            return local if self.excel else local.strftime("%Y-%m-%d %H:%M:%S")
        return str(val)

    def col(self, name: str) -> str:
        return f"{name} ({self.label})"


def _clock(branding: Optional[Branding], end: Optional[datetime], *, excel: bool) -> _Clock:
    b = branding or Branding()
    return _Clock(b, b.tz_label(end), excel)


_UTC_CLOCK = _Clock(Branding(), "UTC", False)


def _ts(val) -> str:
    """Format a timestamp as UTC text (legacy helper; exports use _Clock)."""
    if val is None:
        return ""
    if isinstance(val, datetime):
        return _UTC_CLOCK(val)
    return str(val)


async def _load_branding(db: AsyncSession) -> Branding:
    try:
        from app.services.branding import load_branding
        return await load_branding(db, with_logos=False)
    except Exception:
        logger.debug("branding unavailable for export", exc_info=True)
        return Branding()


# ---------------------------------------------------------------------------
# Worksheet helpers
# ---------------------------------------------------------------------------

def _apply_header_style(ws, num_cols: int, style: _XlStyle = _DEFAULT_STYLE) -> None:
    """Style the first row as a branded header and freeze it."""
    for col_idx in range(1, num_cols + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = style.header_fill
        cell.font = style.header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"


def _apply_alternating_rows(ws, style: _XlStyle = _DEFAULT_STYLE) -> None:
    """Alternating row fill from row 2; timestamps get the organisation's
    date/time format."""
    for row_idx, row in enumerate(ws.iter_rows(min_row=2, max_row=ws.max_row), start=0):
        if row_idx % 2 == 1:
            for cell in row:
                cell.fill = _ALT_ROW_FILL
        for cell in row:
            cell.font = _DEFAULT_FONT
            cell.border = _THIN_BORDER
            if isinstance(cell.value, datetime):
                cell.number_format = style.datetime_format


def _auto_fit_columns(ws, min_width: int = 10, max_width: int = 50) -> None:
    """Adjust column widths based on content."""
    for col_cells in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            if cell.value is not None:
                n = 19 if isinstance(cell.value, datetime) else len(str(cell.value))
                max_len = max(max_len, n)
        width = max(min_width, min(max_len + 3, max_width))
        ws.column_dimensions[col_letter].width = width


def _write_sheet(ws, headers: list[str], rows: list[list], style: _XlStyle = _DEFAULT_STYLE) -> None:
    """Write headers + data rows, then apply styling."""
    ws.append([_coerce_cell(h) for h in headers])
    for row in rows:
        ws.append([_coerce_cell(c) for c in row])
    _apply_header_style(ws, len(headers), style)
    _apply_alternating_rows(ws, style)
    _auto_fit_columns(ws)
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(max(len(headers), 1))}{len(rows) + 1}"


def _write_kpi_sheet(ws, kpis: list[tuple[str, str]], title: str = "Report Summary",
                     style: _XlStyle = _DEFAULT_STYLE) -> None:
    """Write a vertical KPI summary sheet with label/value pairs."""
    ws.append([title])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=2)
    ws.cell(row=1, column=1).font = style.title_font
    ws.append([])  # spacer row

    for label, value in kpis:
        row_num = ws.max_row + 1
        ws.cell(row=row_num, column=1, value=_coerce_cell(label)).font = _KPI_LABEL_FONT
        ws.cell(row=row_num, column=2, value=_coerce_cell(value)).font = style.kpi_value_font

    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 25


def _context_lines(report_type: str, data: dict, branding: Branding) -> list[tuple[str, str]]:
    """Company / report / period / generated facts for the cover or CSV header."""
    start, end = data.get("start"), data.get("end")
    window = branding.period_label(start, end) if isinstance(start, datetime) and isinstance(end, datetime) else ""
    period = data.get("period_label") or ""
    if window and period and period != window:
        period = f"{period} ({window})"
    else:
        period = period or window
    label = branding.tz_label(end if isinstance(end, datetime) else None)
    facts = [
        ("Organisation", branding.display_name),
        ("Report", _REPORT_TITLES.get(report_type, "Report")),
        ("Period", period),
        ("Timezone", label if branding.timezone in ("UTC", label) else f"{branding.timezone} ({label})"),
        ("Generated", branding.fmt_datetime(datetime.now(timezone.utc), with_tz=True)),
        ("Scope", data.get("scope_label") or ""),
        ("Classification", branding.confidentiality),
    ]
    return [(k, v) for k, v in facts if v]


def _add_cover_sheet(wb: Workbook, report_type: str, data: dict, branding: Branding,
                     style: _XlStyle) -> None:
    ws = wb.create_sheet("Cover", 0)
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 80
    ws.cell(row=1, column=1, value=safe_text(branding.display_name)).font = style.title_font
    ws.cell(row=2, column=1, value=safe_text(_REPORT_TITLES.get(report_type, "Report"))).font = \
        Font(name="Calibri", bold=True, size=18, color="0F172A")
    row = 4
    for label, value in _context_lines(report_type, data, branding):
        ws.cell(row=row, column=1, value=label).font = Font(name="Calibri", bold=True, size=11, color="64748B")
        ws.cell(row=row, column=2, value=safe_text(value)).font = _DEFAULT_FONT
        row += 1
    row += 1
    for text_ in (branding.report_footer, branding.report_disclaimer):
        if text_:
            c = ws.cell(row=row, column=1, value=safe_text(text_))
            c.font = _MUTED_FONT
            c.alignment = Alignment(wrap_text=True, vertical="top")
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
            lines = max(1, len(text_) // 95 + 1)
            if lines > 1:
                ws.row_dimensions[row].height = min(15 * lines + 2, 409)
            row += 1
    ws.sheet_properties.tabColor = rc.normalize(branding.brand_color)[1:]


# ---------------------------------------------------------------------------
# Data-assembly helpers (shared by Excel and CSV paths)
# ---------------------------------------------------------------------------

async def _collect_data(
    db: AsyncSession,
    report_type: str,
    period: str,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    device_ids: Optional[list[str]] = None,
    group_ids: Optional[list[str]] = None,
    locations: Optional[list[str]] = None,
    device_types: Optional[list[str]] = None,
    redact_netflow: bool = False,
) -> dict:
    """Fetch all data needed for *report_type* and return as a dict.
    ``redact_netflow``: the requester lacks netflow.view, so NetFlow alert
    messages are withheld (``_fetch_alerts``).

    Device-derived data (metrics, alerts, status changes) is always confined
    to the devices the filter resolved to. When it resolved to none — e.g. a
    scoped account whose filter is ``[scoping.NO_MATCH_ID]`` — there is no
    device data at all; an empty id list never means "every device"."""

    if report_type not in VALID_REPORT_TYPES:
        raise ValueError(f"Unknown report type: {report_type!r}. "
                         f"Must be one of {VALID_REPORT_TYPES}")

    start, end, period_label = _resolve_period(period, from_time, to_time)

    data: dict = {
        "start": start,
        "end": end,
        "period_label": period_label,
    }

    need_devices = report_type in ("executive_summary", "device_health", "full_report")
    # The executive export lists devices and alerts only; services are read
    # by the service health and full reports.
    need_services = report_type in ("service_health", "full_report")
    need_alerts = report_type in ("executive_summary", "alert_analysis", "full_report")
    need_device_detail = report_type in ("device_health", "full_report")
    need_service_detail = report_type in ("service_health", "full_report")

    # --- Devices ----------------------------------------------------------
    devices: list[dict] = []
    if need_devices or need_alerts:
        devices = await _fetch_devices(db, device_ids, group_ids,
                                       locations=locations, device_types=device_types)
        data["devices"] = devices
    d_ids = [d["id"] for d in devices]
    if need_devices or need_alerts:
        # An empty id list means "no filter" to the metric helpers, so they
        # are only called with the resolved devices.
        data["ping_rows"] = _fetch_ping_metrics(start, end, d_ids) if d_ids else []

    # --- Services ---------------------------------------------------------
    if need_services or need_service_detail:
        services = await _fetch_service_checks(db)
        data["services"] = services
        svc_ids = [s["id"] for s in services]
        data["svc_rows"] = _fetch_service_metrics(start, end, svc_ids) if svc_ids else []
    else:
        services = []

    # --- Alerts -----------------------------------------------------------
    if need_alerts:
        data["alerts"] = (await _fetch_alerts(db, start, end, d_ids, redact_netflow=redact_netflow)
                          if d_ids else [])

    # --- Status logs ------------------------------------------------------
    if need_device_detail:
        data["device_status_log"] = _fetch_device_status_log(start, end, d_ids) if d_ids else []

    if need_service_detail:
        svc_ids = [s["id"] for s in services]
        data["service_status_log"] = _fetch_service_status_log(start, end, svc_ids) if svc_ids else []

    return data


# ---------------------------------------------------------------------------
# Device / service row builders
# ---------------------------------------------------------------------------

def _build_device_rows(devices: list[dict], ping_rows: list[dict], *, full: bool = False,
                       ts=_ts) -> list[list]:
    """Build tabular rows for device overview."""
    rows = []
    for d in devices:
        uptime = _device_uptime_pct(ping_rows, d["id"])
        stats = _device_rtt_stats(ping_rows, d["id"])
        row = [
            d.get("hostname", ""),
            d.get("ip_address", ""),
            d.get("device_type", ""),
            d.get("group_name", ""),
        ]
        if full:
            row.append(d.get("location", ""))
        row += [
            d.get("status", ""),
            _fmt_uptime(uptime),
            _fmt_ms(stats["avg"]),
        ]
        if full:
            row += [
                _fmt_ms(stats["min"]),
                _fmt_ms(stats["max"]),
                _fmt_ms(stats["p95"]),
            ]
        row.append(ts(d.get("last_seen")))
        rows.append(row)
    return rows


def _device_headers(*, full: bool = False, clock: Optional[_Clock] = None) -> list[str]:
    headers = ["Hostname", "IP Address", "Type", "Group"]
    if full:
        headers.append("Location")
    headers += ["Status", "Uptime %", "Avg RTT"]
    if full:
        headers += ["Min RTT", "Max RTT", "P95 RTT"]
    headers.append(clock.col("Last Seen") if clock else "Last Seen")
    return headers


def _build_service_rows(services: list[dict], svc_rows: list[dict]) -> list[list]:
    rows = []
    for s in services:
        uptime = _service_uptime_pct(svc_rows, s["id"])
        matching = [r for r in svc_rows if str(r.get("service_check_id")) == str(s["id"])]
        response_vals = [r["response_ms"] for r in matching if r.get("response_ms") is not None]
        avg_resp = sum(response_vals) / len(response_vals) if response_vals else None
        rows.append([
            s.get("name", ""),
            s.get("check_type", ""),
            s.get("target_url") or s.get("target_host", ""),
            s.get("status", ""),
            _fmt_uptime(uptime),
            _fmt_ms(avg_resp),
            s.get("tls_days_remaining", ""),
        ])
    return rows


_SERVICE_HEADERS = ["Name", "Type", "Target", "Status", "Uptime %", "Avg Response", "TLS Days"]


def _build_alert_rows(alerts: list[dict], ts=_ts) -> list[list]:
    rows = []
    for a in alerts:
        resolved_at = a.get("resolved_at")
        triggered_at = a.get("triggered_at")
        if resolved_at and triggered_at:
            try:
                delta = (resolved_at - triggered_at).total_seconds()
                resolution = _fmt_duration(delta)
            except Exception:
                resolution = ""
        else:
            resolution = ""
        rows.append([
            ts(a.get("triggered_at")),
            a.get("hostname", ""),
            a.get("severity", ""),
            a.get("status", ""),
            a.get("message", ""),
            ts(a.get("acknowledged_at")),
            ts(a.get("resolved_at")),
            resolution,
        ])
    return rows


_ALERT_HEADERS = [
    "Triggered At", "Device", "Severity", "Status", "Message",
    "Acknowledged At", "Resolved At", "Resolution Time",
]


def _alert_headers(clock: Optional[_Clock] = None) -> list[str]:
    if clock is None:
        return list(_ALERT_HEADERS)
    return [clock.col(h) if h.endswith(" At") else h for h in _ALERT_HEADERS]


# ---------------------------------------------------------------------------
# Excel sheet builders
# ---------------------------------------------------------------------------

def _excel_clock(data: dict) -> _Clock:
    return data.get("_clock") or _clock(None, data.get("end"), excel=True)


def _excel_style(data: dict) -> _XlStyle:
    return data.get("_style") or _DEFAULT_STYLE


def _add_executive_summary_sheets(wb: Workbook, data: dict) -> None:
    devices = data.get("devices", [])
    ping_rows = data.get("ping_rows", [])
    alerts = data.get("alerts", [])
    clock, style = _excel_clock(data), _excel_style(data)

    # --- Summary KPIs ---
    total = len(devices)
    online = sum(1 for d in devices if d.get("status") in ("online", "up"))
    online_pct = (online / total * 100) if total else 0
    rtt_vals = []
    for d in devices:
        stats = _device_rtt_stats(ping_rows, d["id"])
        if stats["avg"] is not None:
            rtt_vals.append(stats["avg"])
    avg_rtt = sum(rtt_vals) / len(rtt_vals) if rtt_vals else None
    mttr = _mttr_seconds(alerts)

    ws_summary = wb.active
    ws_summary.title = "Summary"
    _write_kpi_sheet(ws_summary, [
        ("Period", data["period_label"]),
        ("Total Devices", str(total)),
        ("Online %", _fmt_pct(online_pct)),
        ("Average RTT", _fmt_ms(avg_rtt)),
        ("Total Alerts", str(len(alerts))),
        ("MTTR", _fmt_duration(mttr)),
    ], style=style)

    # --- Device Status sheet ---
    ws_dev = wb.create_sheet("Device Status")
    _write_sheet(ws_dev, _device_headers(full=False, clock=clock),
                 _build_device_rows(devices, ping_rows, full=False, ts=clock), style)

    # --- Alert Summary sheet ---
    ws_alert = wb.create_sheet("Alert Summary")
    sev_counter = Counter(a.get("severity", "unknown") for a in alerts)
    sev_rows = [[sev, count] for sev, count in sev_counter.most_common()]
    _write_sheet(ws_alert, ["Severity", "Count"], sev_rows, style)

    # Top alerting devices (append below with a gap)
    device_counter = Counter(a.get("hostname", "unknown") for a in alerts)
    top_devices = device_counter.most_common(10)
    gap_row = ws_alert.max_row + 2
    ws_alert.cell(row=gap_row, column=1, value="Top Alerting Devices").font = style.title_font
    ws_alert.cell(row=gap_row + 1, column=1, value="Hostname").font = style.header_font
    ws_alert.cell(row=gap_row + 1, column=1).fill = style.header_fill
    ws_alert.cell(row=gap_row + 1, column=2, value="Alert Count").font = style.header_font
    ws_alert.cell(row=gap_row + 1, column=2).fill = style.header_fill
    for i, (hostname, count) in enumerate(top_devices):
        r = gap_row + 2 + i
        ws_alert.cell(row=r, column=1, value=_coerce_cell(hostname)).font = _DEFAULT_FONT
        ws_alert.cell(row=r, column=2, value=count).font = _DEFAULT_FONT
        if i % 2 == 1:
            ws_alert.cell(row=r, column=1).fill = _ALT_ROW_FILL
            ws_alert.cell(row=r, column=2).fill = _ALT_ROW_FILL


def _add_device_health_sheets(wb: Workbook, data: dict, *, is_standalone: bool = True) -> None:
    devices = data.get("devices", [])
    ping_rows = data.get("ping_rows", [])
    clock, style = _excel_clock(data), _excel_style(data)

    # --- Device Overview ---
    if is_standalone:
        ws = wb.active
        ws.title = "Device Overview"
    else:
        ws = wb.create_sheet("Device Overview")
    _write_sheet(ws, _device_headers(full=True, clock=clock),
                 _build_device_rows(devices, ping_rows, full=True, ts=clock), style)

    # --- Group Summary ---
    groups: dict[str, list[dict]] = defaultdict(list)
    for d in devices:
        groups[d.get("group_name") or "Ungrouped"].append(d)
    group_rows = []
    for name, members in sorted(groups.items()):
        total = len(members)
        online = sum(1 for m in members if m.get("status") in ("online", "up"))
        pct = (online / total * 100) if total else 0
        group_rows.append([name, total, online, _fmt_pct(pct)])
    ws_grp = wb.create_sheet("Group Summary")
    _write_sheet(ws_grp, ["Group", "Total Devices", "Online", "Online %"], group_rows, style)

    # --- Status Changes ---
    status_log = data.get("device_status_log", [])
    hostname_map = {str(d["id"]): d.get("hostname", "") for d in devices}
    sc_rows = []
    for entry in status_log:
        sc_rows.append([
            clock(entry.get("timestamp")),
            hostname_map.get(str(entry.get("device_id")), str(entry.get("device_id", ""))),
            entry.get("old_status", ""),
            entry.get("new_status", ""),
            entry.get("reason", ""),
            _fmt_duration(entry.get("duration_sec")),
        ])
    ws_sc = wb.create_sheet("Status Changes")
    _write_sheet(ws_sc, [clock.col("Timestamp"), "Device", "Old Status", "New Status", "Reason", "Duration"],
                 sc_rows, style)


def _add_service_health_sheets(wb: Workbook, data: dict, *, is_standalone: bool = True) -> None:
    services = data.get("services", [])
    svc_rows_data = data.get("svc_rows", [])
    clock, style = _excel_clock(data), _excel_style(data)

    # --- Service Overview ---
    if is_standalone:
        ws = wb.active
        ws.title = "Service Overview"
    else:
        ws = wb.create_sheet("Service Overview")
    _write_sheet(ws, _SERVICE_HEADERS, _build_service_rows(services, svc_rows_data), style)

    # --- Service Metrics ---
    ws_met = wb.create_sheet("Service Metrics")
    svc_name_map = {str(s["id"]): s.get("name", "") for s in services}
    met_rows = []
    for r in svc_rows_data:
        met_rows.append([
            svc_name_map.get(str(r.get("service_check_id")), ""),
            clock(r.get("timestamp")),
            _fmt_ms(r.get("response_ms")),
            "Yes" if r.get("is_up") else "No",
            r.get("status_code", ""),
            r.get("error_message", ""),
        ])
    _write_sheet(ws_met,
                 ["Service", clock.col("Timestamp"), "Response", "Up", "Status Code", "Error"],
                 met_rows, style)

    # --- Service Status Changes ---
    status_log = data.get("service_status_log", [])
    sc_rows = []
    for entry in status_log:
        sc_rows.append([
            clock(entry.get("timestamp")),
            svc_name_map.get(str(entry.get("service_check_id")), str(entry.get("service_check_id", ""))),
            entry.get("old_status", ""),
            entry.get("new_status", ""),
            entry.get("reason", ""),
            _fmt_duration(entry.get("duration_sec")),
        ])
    ws_sc = wb.create_sheet("Service Status Changes")
    _write_sheet(ws_sc, [clock.col("Timestamp"), "Service", "Old Status", "New Status", "Reason", "Duration"],
                 sc_rows, style)


def _add_alert_analysis_sheets(wb: Workbook, data: dict, *, is_standalone: bool = True) -> None:
    alerts = data.get("alerts", [])
    clock, style = _excel_clock(data), _excel_style(data)

    # --- Alert List ---
    if is_standalone:
        ws = wb.active
        ws.title = "Alert List"
    else:
        ws = wb.create_sheet("Alert List")
    _write_sheet(ws, _alert_headers(clock), _build_alert_rows(alerts, ts=clock), style)

    # --- Severity Summary ---
    sev_counter = Counter(a.get("severity", "unknown") for a in alerts)
    total_alerts = len(alerts)
    sev_rows = []
    for sev, count in sev_counter.most_common():
        pct = (count / total_alerts * 100) if total_alerts else 0
        sev_rows.append([sev, count, _fmt_pct(pct)])
    ws_sev = wb.create_sheet("Severity Summary")
    _write_sheet(ws_sev, ["Severity", "Count", "Percentage"], sev_rows, style)

    # --- Top Alerting Devices ---
    device_counter = Counter(a.get("hostname", "unknown") for a in alerts)
    # Approximate total downtime per device from resolved alerts
    device_downtime: dict[str, float] = defaultdict(float)
    for a in alerts:
        hostname = a.get("hostname", "unknown")
        triggered = a.get("triggered_at")
        resolved = a.get("resolved_at")
        if triggered and resolved:
            try:
                device_downtime[hostname] += (resolved - triggered).total_seconds()
            except Exception:
                pass
    top_rows = []
    for hostname, count in device_counter.most_common(20):
        top_rows.append([
            hostname,
            count,
            _fmt_duration(device_downtime.get(hostname) or 0),
        ])
    ws_top = wb.create_sheet("Top Alerting Devices")
    _write_sheet(ws_top, ["Hostname", "Alert Count", "Total Downtime"], top_rows, style)


def _add_full_report_sheets(wb: Workbook, data: dict) -> None:
    """Combine all report type sheets into one workbook."""
    # The first sheet is a summary (executive)
    _add_executive_summary_sheets(wb, data)
    _add_device_health_sheets(wb, data, is_standalone=False)
    _add_service_health_sheets(wb, data, is_standalone=False)
    _add_alert_analysis_sheets(wb, data, is_standalone=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate_excel_report(
    db: AsyncSession,
    report_type: str,
    period: str,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    device_ids: Optional[list[str]] = None,
    group_ids: Optional[list[str]] = None,
    locations: Optional[list[str]] = None,
    device_types: Optional[list[str]] = None,
    *,
    redact_netflow: bool = False,  # the requester lacks netflow.view
    branding: Optional[Branding] = None,
) -> bytes:
    """Generate a styled Excel (.xlsx) workbook and return it as bytes."""

    if branding is None:
        branding = await _load_branding(db)
    data = await _collect_data(db, report_type, period, from_time, to_time,
                               device_ids, group_ids, locations, device_types,
                               redact_netflow=redact_netflow)
    data["_clock"] = _clock(branding, data.get("end"), excel=True)
    data["_style"] = style = _XlStyle.for_branding(branding)

    wb = Workbook()

    if report_type == "executive_summary":
        _add_executive_summary_sheets(wb, data)
    elif report_type == "device_health":
        _add_device_health_sheets(wb, data, is_standalone=True)
    elif report_type == "service_health":
        _add_service_health_sheets(wb, data, is_standalone=True)
    elif report_type == "alert_analysis":
        _add_alert_analysis_sheets(wb, data, is_standalone=True)
    elif report_type == "full_report":
        _add_full_report_sheets(wb, data)

    _add_cover_sheet(wb, report_type, data, branding, style)
    wb.active = 0
    wb.properties.title = _REPORT_TITLES.get(report_type, "Report")
    wb.properties.creator = safe_text(branding.display_name)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


async def generate_csv_report(
    db: AsyncSession,
    report_type: str,
    period: str,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    device_ids: Optional[list[str]] = None,
    group_ids: Optional[list[str]] = None,
    locations: Optional[list[str]] = None,
    device_types: Optional[list[str]] = None,
    *,
    redact_netflow: bool = False,  # the requester lacks netflow.view
    branding: Optional[Branding] = None,
) -> bytes:
    """Generate a CSV export and return it as UTF-8 encoded bytes.

    A short header block (organisation, report, period with timezone,
    generated time) precedes the table, separated by a blank line."""

    if branding is None:
        branding = await _load_branding(db)
    data = await _collect_data(db, report_type, period, from_time, to_time,
                               device_ids, group_ids, locations, device_types,
                               redact_netflow=redact_netflow)
    clock = _clock(branding, data.get("end"), excel=False)

    sio = io.StringIO()
    writer = csv.writer(sio)
    for label, value in _context_lines(report_type, data, branding):
        writer.writerow([_csv_cell(f"{label}: {value}")])
    writer.writerow([])

    if report_type in ("executive_summary", "device_health", "full_report"):
        devices = data.get("devices", [])
        ping_rows = data.get("ping_rows", [])
        full = report_type in ("device_health", "full_report")
        writer.writerow([_csv_cell(h) for h in _device_headers(full=full, clock=clock)])
        writer.writerows([_csv_cell(c) for c in row]
                         for row in _build_device_rows(devices, ping_rows, full=full, ts=clock))

    elif report_type == "service_health":
        services = data.get("services", [])
        svc_rows_data = data.get("svc_rows", [])
        writer.writerow(_SERVICE_HEADERS)
        writer.writerows([_csv_cell(c) for c in row]
                         for row in _build_service_rows(services, svc_rows_data))

    elif report_type == "alert_analysis":
        alerts = data.get("alerts", [])
        writer.writerow([_csv_cell(h) for h in _alert_headers(clock)])
        writer.writerows([_csv_cell(c) for c in row] for row in _build_alert_rows(alerts, ts=clock))

    return sio.getvalue().encode("utf-8")
