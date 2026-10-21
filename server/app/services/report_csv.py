"""CSV export of section-engine (template) reports.

One file for the whole report, laid out like the legacy CSV export: a header
block (organisation, report, period, timezone, generated, scope), then each
section in report order -- its title, its headline figures as
``label,value,subtitle`` rows and each table with its headers and rows --
with a blank line between blocks. Charts have no CSV form; their data is in
the tables beside them.

UTF-8 with a byte-order mark so Excel opens non-Latin text correctly. Every
text cell goes through report_xlsx.safe_text (formula triggers neutralised,
control characters removed), like the workbook and the legacy CSV.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from app.services.report_xlsx import safe_text

BOM = "﻿"


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple, set)):
        value = ", ".join(str(v) for v in value)
    return safe_text(value)


def _header_lines(meta: dict) -> list[tuple[str, str]]:
    tz = str(meta.get("timezone") or "")
    label = str(meta.get("tz_label") or "")
    zone = label if not tz or tz in ("UTC", label) else (f"{tz} ({label})" if label else tz)
    facts = [
        ("Organisation", meta.get("company_name") or "ZenPlus"),
        ("Report", meta.get("title") or "Report"),
        ("Period", meta.get("period_label") or ""),
        ("Timezone", zone),
        ("Generated", meta.get("generated_label") or ""),
        ("Scope", meta.get("scope_label") or ""),
        ("Classification", meta.get("confidentiality") or ""),
    ]
    return [(k, str(v)) for k, v in facts if v]


def render_csv(meta: dict, sections: list[dict]) -> bytes:
    """The report as CSV bytes (UTF-8 with BOM)."""
    meta = meta or {}
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    for label, value in _header_lines(meta):
        w.writerow([_cell(f"{label}: {value}")])
    for s in sections or ():
        w.writerow([])
        w.writerow([_cell(s.get("title") or s.get("id") or "Section")])
        notes = [str(n) for n in (s.get("notes") or []) if n]
        if s.get("restricted") or s.get("error"):
            # Withheld or failed: say why, never a silent gap.
            w.writerow([_cell(notes[0] if notes else
                              "Not included" if s.get("restricted") else "This section could not be generated")])
            continue
        if s.get("description"):
            w.writerow([_cell(s["description"])])
        kpis = [k for k in (s.get("kpis") or []) if isinstance(k, dict) and k.get("label") is not None]
        if kpis:
            w.writerow([])
            w.writerow(["Figure", "Value", "Detail"])
            for k in kpis:
                w.writerow([_cell(k.get("label")), _cell(k.get("value")), _cell(k.get("subtitle"))])
        for t in s.get("tables") or ():
            headers = list(t.get("headers") or [])
            rows = list(t.get("rows") or [])
            w.writerow([])
            if t.get("title"):
                w.writerow([_cell(t["title"])])
            if headers:
                w.writerow([_cell(h) for h in headers])
            for row in rows:
                w.writerow([_cell(c) for c in row])
        if notes:
            w.writerow([])
            for n in notes:
                w.writerow([_cell(f"Note: {n}")])
    return (BOM + out.getvalue()).encode("utf-8")
