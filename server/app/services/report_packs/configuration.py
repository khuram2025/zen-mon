"""Configuration (NCCM) and lifecycle sections: firmware/OS versions,
hardware serial inventory, compliance trend and exceptions, change audit,
restore readiness, backup-failure incidents and credential hygiene.

The original NCCM sections (ncm_protection, ncm_breakdown, ncm_outcomes,
ncm_at_risk, ncm_compliance, ncm_changes) live in report_sections and are
reused by the templates at the bottom of this module.

Shared rules in this pack:
- Configuration text is never read: no query references the content columns
  of device_configs, and connection profiles expose only whether a secret is
  saved — never usernames, secrets, keys or fingerprints.
- Device-scoped sections carry ctx.pg_device_clause (or the same dev_ids
  binding) in every query.
- ncm_compliance_snapshots hold one fleet-wide row per policy per UTC day,
  so the compliance trend is a fleet section.
- No end-of-life / end-of-support data source exists; version reports say
  so instead of inferring lifecycle status.
- Attribution in change events (reported_user) is whatever the device or the
  calling system wrote; it is shown as unverified.
- NCCM SSH host-key trust is warn-only (app.services.ssh_host_keys): the
  first key a device presents is recorded in ncm_host_keys and a changed key
  is audited as backup.host_key_changed, never blocked. ncm_credentials.
  ssh_auto_trust remains unused, and no report section reads either table.
- Status cells use words both the dashboard and the PDF/HTML renderers tone
  (ok, stale, warning, failed, expired, compliant, non-compliant, unknown,
  open, resolved); anything else renders neutral.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from app.services.report_sections import (
    SectionCtx,
    _fmt_pct,
    _local_bucket_starts,
    _section,
    register_section,
    top_n_option,
)
from app.services.report_templates import S, template

DEVICE_NAME = "COALESCE(NULLIF(d.hostname, ''), host(d.ip_address), d.id::text)"

# Policy rule severities -> the report severity vocabulary.
SEVERITY_MAP = {"critical": "critical", "high": "major", "medium": "warning",
                "low": "minor", "info": "info"}

FAILURE_LABELS = {
    "blocked": "Blocked by readiness checks", "not_enrolled": "Not enrolled",
    "credentials": "Credentials unavailable", "authentication": "Authentication rejected",
    "unreachable": "SSH unreachable", "timeout": "Timed out",
    "negotiation": "SSH negotiation failed", "privilege": "Insufficient privilege",
    "driver": "Platform driver not resolved", "unsupported_type": "Unsupported capture type",
    "incomplete": "Incomplete output", "format": "Unexpected output format",
    "interrupted": "Session interrupted", "archive": "Archive or storage failure",
    "cancelled": "Cancelled", "busy": "Backup already running", "unknown": "Unknown cause",
    # Retired with SSH host trust (1.23.29); older incidents keep these causes.
    "host_key_untrusted": "SSH host key not trusted (retired check)",
    "host_key_changed": "SSH host key changed (retired check)",
}


def _failure_label(category: Any) -> str:
    cat = str(category or "unknown")
    return FAILURE_LABELS.get(cat) or cat.replace("_", " ").capitalize()


SOURCE_LABELS = {"syslog": "Syslog", "trap": "SNMP trap", "api": "API"}
TRIGGER_LABELS = {"schedule": "Scheduled", "manual": "Manual", "event": "Change event"}
METHOD_LABELS = {"lab_device": "Lab device", "isolated_replica": "Isolated replica",
                 "production_rollback": "Production rollback", "vendor_tool": "Vendor tool"}

NO_CONFIG_TEXT_NOTE = "Configuration text is never included in reports."


# ─── Shared helpers ─────────────────────────────────────────────────────────

def _aware(dt: Any) -> Optional[datetime]:
    """Aware UTC datetime (naive values are UTC)."""
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _n(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _clip(value: Any, limit: int) -> str:
    s = " ".join(str(value or "").split())
    if not s:
        return "—"
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _kpi(label: str, value: str, accent: str, subtitle: Optional[str] = None) -> dict:
    out = {"label": label, "value": value, "accent": accent}
    if subtitle:
        out["subtitle"] = subtitle
    return out


def _truncated(title: str, shown: int, total: int) -> str:
    return f"{title} — top {shown} of {total:,}" if total > shown else title


def _duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = max(0.0, float(seconds))
    if s < 60:
        return f"{s:.0f} s"
    m = int(round(s / 60))
    if m < 60:
        return f"{m} min"
    if m < 1440:
        h, mm = divmod(m, 60)
        return f"{h} h {mm:02d} min"
    d, rem = divmod(m, 1440)
    return f"{d} d {rem // 60} h"


def _days(seconds: float) -> str:
    d = seconds / 86400
    return "< 1 d" if d < 1 else f"{int(d):,} d"


def _fmt_day(ctx: SectionCtx, day: date) -> str:
    """A calendar date in the organisation's date format, without shifting it
    through a timezone (snapshot days are already calendar days)."""
    return ctx.fmt_date(datetime(day.year, day.month, day.day, 12, tzinfo=ctx._brand().tz))


def _pg_day(column: str) -> str:
    """Epoch of the organisation-local day containing ``column`` (param :org_tz)."""
    return (f"EXTRACT(EPOCH FROM (date_trunc('day', {column} AT TIME ZONE CAST(:org_tz AS text))"
            f" AT TIME ZONE CAST(:org_tz AS text)))::bigint")


def _daily_points(ctx: SectionCtx, counts: dict[int, int]) -> list[dict]:
    """Counts per organisation-local day of the window. A day without records
    is 0: these are event counts, not samples, so nothing is missing."""
    keys = {int(d.timestamp()) for d in _local_bucket_starts(ctx, "day")}
    if keys:
        lo, hi = min(keys), int(_aware(ctx.to).timestamp())
        keys.update(k for k in counts if lo <= k <= hi)
    return [{"t": ctx.iso(datetime.fromtimestamp(k, timezone.utc)), "v": counts.get(k, 0)}
            for k in sorted(keys)]


def _local_days(ctx: SectionCtx) -> tuple[date, date]:
    """First and last organisation-local calendar day the window covers."""
    brand = ctx._brand()
    first = brand.localize(_aware(ctx.frm)).date()
    last = brand.localize(_aware(ctx.to) - timedelta(microseconds=1)).date()
    return first, max(first, last)


def _change(curr: Optional[float], prev: Optional[float], unit: str, digits: int,
            since: str) -> Optional[str]:
    if curr is None or prev is None:
        return None
    d = round(curr - prev, digits)
    if d == 0:
        return f"No change {since}"
    return f"{'▲' if d > 0 else '▼'} {abs(d):,.{digits}f}{unit} {since}"


_TOKEN_RE = re.compile(r"\d+|[A-Za-z]+")


def _version_key(version: str) -> tuple:
    """Natural sort key: '15.2(7)E3' -> 15, 2, 7, 'e', 3. Numbers sort before
    letters within a position; a longer version with an equal prefix is newer."""
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t.lower())
                 for t in _TOKEN_RE.findall(version or ""))


# ─── Firmware and OS versions ───────────────────────────────────────────────

VERSION_CHOICES = [["os", "OS version (firmware revision when absent)"],
                   ["firmware", "Chassis firmware revision"]]
VERSION_TITLES = {"os": "Firmware and OS Versions", "firmware": "Chassis Firmware Revisions"}
LIST_CHOICES = [["flagged", "Devices on older or minority versions"], ["all", "Every device"]]


@register_section(
    "firmware_inventory", title="Firmware and OS Versions", category="Inventory",
    description=("Software and firmware versions by vendor and model as of report generation, "
                 "flagging devices on older or minority versions."),
    scope="device", requires=("devices.view",), semantics="snapshot",
    options=[
        {"key": "version", "label": "Version", "type": "select", "default": "os",
         "choices": VERSION_CHOICES},
        {"key": "list", "label": "Devices to list", "type": "select", "default": "flagged",
         "choices": LIST_CHOICES},
        {"key": "minority_pct", "label": "Minority version below (% of the model's devices)",
         "type": "number", "default": 20, "min": 1, "max": 50},
        top_n_option(100, 1000),
    ],
)
async def firmware_inventory(ctx: SectionCtx) -> dict:
    source = ctx.opt("version")
    title = VERSION_TITLES.get(source, VERSION_TITLES["os"])
    what = "OS version" if source == "os" else "firmware revision"
    what_plural = "OS and firmware versions" if source == "os" else "Chassis firmware revisions"
    version_sql = ("COALESCE(NULLIF(btrim(d.os_version), ''), NULLIF(btrim(d.firmware_version), ''))"
                   if source == "os" else "NULLIF(btrim(d.firmware_version), '')")
    clause, params = ctx.pg_device_clause("d.id")
    rows = (await ctx.db.execute(text(
        f"SELECT d.id::text, {DEVICE_NAME}, COALESCE(host(d.ip_address), ''), "
        "NULLIF(btrim(d.vendor), ''), NULLIF(btrim(d.model), ''), "
        f"{version_sql} FROM devices d WHERE TRUE{clause} ORDER BY 2"), params)).fetchall()
    eol_note = ("No end-of-life or end-of-support data source exists in ZenPlus, so this report "
                "does not state whether a version is still supported by its vendor.")
    if not rows:
        return _section("firmware_inventory", title,
                        description=f"{what_plural} by vendor and model, as of report generation.",
                        notes=["No devices in scope.", eol_note])

    threshold = float(ctx.opt("minority_pct"))
    total = len(rows)
    groups: dict[tuple[str, str], dict[str, list]] = {}
    missing = []
    for r in rows:
        if not r[5]:
            missing.append(r)
            continue
        key = (r[3] or "Unknown vendor", r[4] or "Unknown model")
        groups.setdefault(key, {}).setdefault(str(r[5]), []).append(r)

    dist_rows: list[list[str]] = []
    device_rows: list[tuple] = []          # (sort key, row)
    by_vendor_version: dict[str, int] = {}
    older_n = minority_n = 0
    for (vendor, model), versions in sorted(groups.items(),
                                            key=lambda i: (i[0][0].lower(), i[0][1].lower())):
        members = sum(len(v) for v in versions.values())
        newest = max(versions, key=_version_key)
        multi = len(versions) > 1
        for version in sorted(versions, key=_version_key, reverse=True):
            devs = versions[version]
            n = len(devs)
            share = n / members * 100
            older = multi and _version_key(version) < _version_key(newest)
            minority = multi and share < threshold
            findings = []
            if older:
                findings.append(f"Older than {_clip(newest, 40)} (newest on this model)")
            if minority:
                findings.append(f"Minority: {n} of {members} devices")
            if not findings:
                findings.append("Newest on this model" if multi else "Only version on this model")
            finding = "; ".join(findings)
            status = "stale" if older else "warning" if minority else "ok"
            older_n += n if older else 0
            minority_n += n if minority else 0
            label = _clip(f"{vendor} {version}", 48)
            by_vendor_version[label] = by_vendor_version.get(label, 0) + n
            dist_rows.append([vendor, model, _clip(version, 60), f"{n:,}", f"{share:.1f}%",
                              finding, status])
            rank = 0 if older else 1 if minority else 2
            for r in devs:
                device_rows.append(((rank, vendor.lower(), model.lower(), (r[1] or "").lower()),
                                    [r[1], r[2] or "—", vendor, model, _clip(version, 60),
                                     _clip(newest, 60), finding, status]))
    show_all = ctx.opt("list") == "all"
    if show_all:
        for r in missing:
            device_rows.append(((3, (r[3] or "").lower(), (r[4] or "").lower(), (r[1] or "").lower()),
                                [r[1], r[2] or "—", r[3] or "Unknown vendor", r[4] or "Unknown model",
                                 "—", "—", "Not reported", "unknown"]))
    else:
        device_rows = [d for d in device_rows if d[0][0] < 2]
    device_rows.sort(key=lambda d: d[0])
    top_n = int(ctx.opt("top_n"))
    listed = [d[1] for d in device_rows[:top_n]]
    reporting = total - len(missing)

    kpis = [
        _kpi("Devices reporting a version", f"{reporting:,} of {total:,}",
             "primary" if not missing else "warning",
             f"{len(missing):,} not reported" if missing else None),
        _kpi("Distinct versions", f"{len(dist_rows):,}", "info",
             f"across {len(groups):,} vendor/model families"),
        _kpi("On older versions", f"{older_n:,}", "warning" if older_n else "success",
             "below the newest version seen on the same model"),
        _kpi("On minority versions", f"{minority_n:,}", "warning" if minority_n else "success",
             f"under {threshold:g}% of their model's devices"),
    ]
    top_versions = sorted(by_vendor_version.items(), key=lambda i: (-i[1], i[0]))[:10]
    charts = ([{"title": "Most common versions",
                "series": {"kind": "rank", "unit": "devices", "color": "primary",
                           "points": [{"label": k, "value": v} for k, v in top_versions]}}]
              if top_versions else [])
    tables = []
    if dist_rows:
        tables.append({"title": _truncated("Version distribution by vendor and model",
                                           min(top_n, len(dist_rows)), len(dist_rows)),
                       "headers": ["Vendor", "Model", "Version", "Devices", "Share of model",
                                   "Finding", "Status"],
                       "styles": ["text", "text", "mono", "num", "pct", "text", "status"],
                       "rows": dist_rows[:top_n]})
    if listed:
        base = "Devices and their versions" if show_all else "Devices on older or minority versions"
        tables.append({"title": _truncated(base, len(listed), len(device_rows)),
                       "headers": ["Device", "IP address", "Vendor", "Model", "Version",
                                   "Newest on model", "Finding", "Status"],
                       "styles": ["text", "mono", "text", "text", "mono", "mono", "text", "status"],
                       "rows": listed})

    notes = [eol_note,
             ("Versions are compared as reported, without normalising vendor naming schemes: "
              "'older' means the version sorts below the newest version seen on the same vendor "
              "and model in this inventory, and a minority version runs on fewer than "
              f"{threshold:g}% of that model's devices."),
             ("Versions are the last values each device reported: the poller keeps a reported "
              "version until the device reports a different one, so an unreachable device shows its "
              "last known version.")]
    if source == "os":
        notes.append("The OS version is identified over SNMP by the device's vendor profile; when a "
                     "device reports none, its chassis firmware revision (ENTITY-MIB) is used.")
    if missing:
        notes.append(f"{len(missing):,} device(s) report no {what}; they are not measured, never "
                     "counted as an old version.")
    if not show_all and not listed and dist_rows:
        notes.append("No device runs an older or minority version.")
    return _section(
        "firmware_inventory", title,
        description=(f"{what_plural} of {total:,} device(s) by vendor and model, as of report "
                     "generation."),
        kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── Hardware serial number inventory ───────────────────────────────────────

COMPONENT_CHOICES = [["chassis", "Chassis only"],
                     ["fru", "Chassis, modules, power supplies and fans"],
                     ["all", "Every component with a serial number or model"]]
COMPONENT_CLASSES = {"chassis": ("chassis",),
                     "fru": ("chassis", "module", "powerSupply", "fan", "stack"),
                     "all": None}
CLASS_LABELS = {"chassis": "Chassis", "module": "Module", "powerSupply": "Power supply",
                "fan": "Fan", "stack": "Stack", "backplane": "Backplane", "container": "Container",
                "sensor": "Sensor", "port": "Port", "cpu": "CPU", "other": "Other",
                "unknown": "Unknown"}
# An entity whose last_seen trails its device's newest entity row by more
# than this was not reported by the latest inventory walk.
ABSENT_SQL = "e.last_seen < w.walk - interval '1 hour'"


@register_section(
    "hardware_serial_inventory", title="Hardware Serial Number Inventory", category="Inventory",
    description=("Chassis and component serial numbers, models and hardware/firmware revisions "
                 "from ENTITY-MIB, as of report generation."),
    scope="device", requires=("devices.view",), semantics="snapshot",
    options=[
        {"key": "components", "label": "Components", "type": "select", "default": "fru",
         "choices": COMPONENT_CHOICES},
        {"key": "include_absent", "label": "Include components missing from the latest inventory walk",
         "type": "bool", "default": False},
        top_n_option(500, 5000),
    ],
)
async def hardware_serial_inventory(ctx: SectionCtx) -> dict:
    title = "Hardware Serial Number Inventory"
    components = ctx.opt("components")
    include_absent = bool(ctx.opt("include_absent"))
    clause, params = ctx.pg_device_clause("d.id")
    walk_clause, _p = ctx.pg_device_clause("device_id")
    where = ["(e.class = 'chassis' OR NULLIF(btrim(e.serial_number), '') IS NOT NULL "
             "OR NULLIF(btrim(e.model_name), '') IS NOT NULL)"]
    classes = COMPONENT_CLASSES.get(components)
    if classes:
        where.append("e.class = ANY(:classes)")
        params["classes"] = list(classes)
    if not include_absent:
        where.append(f"NOT ({ABSENT_SQL})")
    base = (" FROM device_entities e JOIN devices d ON d.id = e.device_id "
            "JOIN (SELECT device_id, max(last_seen) AS walk FROM device_entities "
            f"WHERE TRUE{walk_clause} GROUP BY device_id) w ON w.device_id = e.device_id "
            "WHERE " + " AND ".join(where) + clause)

    scope_row = (await ctx.db.execute(text(
        "SELECT count(*), count(*) FILTER (WHERE d.snmp_enabled) "
        f"FROM devices d WHERE TRUE{clause}"), params)).first()
    in_scope = _n(scope_row[0]) if scope_row else 0
    snmp_n = _n(scope_row[1]) if scope_row else 0
    stats = (await ctx.db.execute(text(
        "SELECT e.class, GROUPING(e.class) AS is_total, count(*), count(DISTINCT e.device_id), "
        "count(*) FILTER (WHERE NULLIF(btrim(e.serial_number), '') IS NULL), "
        f"count(*) FILTER (WHERE {ABSENT_SQL})"
        f"{base} GROUP BY GROUPING SETS ((e.class), ())"), params)).fetchall()
    totals = next((r for r in stats if r[1]), None)
    by_class = sorted(((r[0] or "unknown", _n(r[2])) for r in stats if not r[1]),
                      key=lambda i: -i[1])
    listed_total = _n(totals[2]) if totals else 0
    if not listed_total:
        return _section(
            "hardware_serial_inventory", title,
            description="Hardware inventory reported by the devices in scope.",
            kpis=[_kpi("Devices with hardware inventory", f"0 of {in_scope:,}", "neutral",
                       f"{snmp_n:,} polled over SNMP")],
            notes=["No hardware inventory has been collected for the devices in scope. Inventory "
                   "comes from each device's ENTITY-MIB walk over SNMP; devices without SNMP or "
                   "ENTITY-MIB support have none."])

    top_n = int(ctx.opt("top_n"))
    lp = dict(params, lim=top_n)
    listing = (await ctx.db.execute(text(
        f"SELECT {DEVICE_NAME}, e.class, e.name, e.model_name, e.serial_number, e.hw_revision, "
        f"e.fw_revision, e.last_seen, ({ABSENT_SQL}){base} "
        "ORDER BY 1, e.device_id, e.ent_index LIMIT :lim"), lp)).fetchall()
    dupes = (await ctx.db.execute(text(
        f"SELECT btrim(e.serial_number), count(DISTINCT e.device_id), "
        f"string_agg(DISTINCT {DEVICE_NAME}, ', ') "
        "FROM device_entities e JOIN devices d ON d.id = e.device_id "
        f"WHERE e.class = 'chassis' AND NULLIF(btrim(e.serial_number), '') IS NOT NULL{clause} "
        "GROUP BY 1 HAVING count(DISTINCT e.device_id) > 1 ORDER BY 2 DESC, 1 LIMIT 25"),
        params)).fetchall()

    devices_with = _n(totals[3])
    no_serial = _n(totals[4])
    absent = _n(totals[5])
    chassis = next((n for c, n in by_class if c == "chassis"), 0)

    def cell(v: Any, limit: int = 48) -> str:
        return _clip(v, limit)

    rows = []
    for r in listing:
        row = [r[0], CLASS_LABELS.get(r[1] or "", r[1] or "—"), cell(r[2]), cell(r[3]),
               cell(r[4], 64), cell(r[5], 24), cell(r[6], 32), ctx.fmt_short(r[7])]
        if include_absent:
            row.append("stale" if r[8] else "ok")
        rows.append(row)
    headers = ["Device", "Class", "Component", "Model", "Serial number", "HW revision",
               "FW revision", "Last seen"]
    styles = ["text", "text", "text", "text", "mono", "mono", "mono", "num"]
    if include_absent:
        headers.append("State")
        styles.append("status")
    tables = [{"title": _truncated("Hardware components", len(rows), listed_total),
               "headers": headers, "styles": styles, "rows": rows}]
    if dupes:
        tables.append({"title": "Chassis serial numbers reported by more than one device",
                       "headers": ["Serial number", "Devices", "Reported by"],
                       "styles": ["mono", "num", "text"],
                       "rows": [[cell(d[0], 64), f"{_n(d[1]):,}", _clip(d[2], 160)] for d in dupes]})

    kpis = [
        _kpi("Devices with hardware inventory", f"{devices_with:,} of {in_scope:,}",
             "primary" if devices_with >= snmp_n else "warning",
             f"{snmp_n:,} polled over SNMP"),
        _kpi("Chassis", f"{chassis:,}", "info"),
        _kpi("Components listed", f"{listed_total:,}", "primary"),
        _kpi("Missing serial numbers", f"{no_serial:,}", "warning" if no_serial else "success"),
        _kpi("Duplicate chassis serials", f"{len(dupes):,}", "warning" if dupes else "success",
             "same serial on more than one device" if dupes else None),
    ]
    if include_absent:
        kpis.append(_kpi("Missing from latest walk", f"{absent:,}", "warning" if absent else "success"))
    notes = ["Hardware inventory comes from each device's ENTITY-MIB walk over SNMP; serial numbers "
             "and revisions are shown as the device reports them (— when blank)."]
    notes.append("Components marked stale were not reported by the device's latest inventory walk "
                 "and may have been removed." if include_absent else
                 "Components missing from a device's latest inventory walk (possibly removed) are "
                 "not listed.")
    if dupes:
        notes.append("A chassis serial reported by more than one device usually means the same "
                     "hardware was added twice; review those devices.")
    scope_label = dict(COMPONENT_CHOICES).get(components, "").lower()
    return _section(
        "hardware_serial_inventory", title,
        description=f"{scope_label.capitalize()} reported by {devices_with:,} device(s), as of report "
                    "generation.",
        kpis=kpis,
        charts=[{"title": "Components by class",
                 "series": {"kind": "donut", "unit": "components",
                            "points": [{"label": CLASS_LABELS.get(c, c), "value": n}
                                       for c, n in by_class]}}] if len(by_class) > 1 else [],
        tables=tables, notes=notes)


# ─── Compliance trend (fleet) ───────────────────────────────────────────────

def _pass_rate(passed: int, failed: int) -> Optional[float]:
    return passed / (passed + failed) * 100 if passed + failed else None


TREND_CHARTS = [["both", "Pass rate and check outcomes"], ["rate", "Pass rate"],
                ["outcomes", "Check outcomes per day"]]


@register_section(
    "ncm_compliance_trend", title="Compliance Trend", category="Configuration",
    description=("Daily configuration-compliance outcomes across every assessed device and policy, "
                 "with the change over the period."),
    scope="fleet", requires=("ncm.view",), semantics="window",
    options=[{"key": "chart", "label": "Charts", "type": "select", "default": "both",
              "choices": TREND_CHARTS}],
)
async def ncm_compliance_trend(ctx: SectionCtx) -> dict:
    title = "Compliance Trend"
    d0, d1 = _local_days(ctx)
    # Snapshot days are UTC days; a day that has not started in UTC yet cannot
    # have one.
    d1 = max(d0, min(d1, _now().date()))
    rows = (await ctx.db.execute(text(
        "SELECT s.day, p.name, s.devices, s.passed, s.failed, s.unknown, s.excepted, "
        "s.compliant_devices FROM ncm_compliance_snapshots s "
        "JOIN ncm_policies p ON p.id = s.policy_id "
        "WHERE s.day BETWEEN :d0 AND :d1 ORDER BY s.day, p.name"), {"d0": d0, "d1": d1})).fetchall()
    notes = []
    if ctx.device_ids:
        notes.append("Compliance snapshots are fleet-wide; the device selection does not apply to "
                     "this section.")
    if not rows:
        notes.insert(0, "No compliance snapshots were recorded in this period. A snapshot is taken "
                        "whenever policies are evaluated (at least once a day while NCCM compliance "
                        "is in use); days before the first evaluation have no data.")
        return _section("ncm_compliance_trend", title,
                        description="Daily configuration-compliance outcomes, fleet-wide.",
                        notes=notes)

    days: dict[date, list[int]] = {}
    policies: dict[str, list[tuple]] = {}
    for r in rows:
        day = r[0] if isinstance(r[0], date) and not isinstance(r[0], datetime) else _aware(r[0]).date()
        tot = days.setdefault(day, [0, 0, 0, 0, 0, 0])
        for i, v in enumerate(r[2:8]):
            tot[i] += _n(v)
        policies.setdefault(str(r[1]), []).append((day, *[_n(v) for v in r[2:8]]))

    all_days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    first_day, last_day = min(days), max(days)
    first, last = days[first_day], days[last_day]
    rate_first, rate_last = _pass_rate(first[1], first[2]), _pass_rate(last[1], last[2])
    since = f"since {_fmt_day(ctx, first_day)}"
    compare = first_day != last_day

    def noon(day: date) -> Optional[str]:
        return ctx.iso(datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc))

    kpis = [
        _kpi("Pass rate", _fmt_pct(rate_last),
             "neutral" if rate_last is None else "success" if not last[2]
             else "warning" if rate_last >= 90 else "danger",
             _change(rate_last, rate_first, " pts", 2, since) if compare else
             f"as of {_fmt_day(ctx, last_day)}"),
        _kpi("Failed checks", f"{last[2]:,}", "danger" if last[2] else "success",
             _change(last[2], first[2], "", 0, since) if compare else None),
        _kpi("Not assessable", f"{last[3]:,}", "info" if last[3] else "neutral",
             "no validated configuration"),
        _kpi("Excepted checks", f"{last[4]:,}", "warning" if last[4] else "neutral",
             "accepted risk with an owner"),
        _kpi("Compliant device–policy pairs", f"{last[5]:,} of {last[0]:,}", "primary",
             "every check passed or excepted"),
        _kpi("Days recorded", f"{len(days):,} of {len(all_days):,}",
             "success" if len(days) >= len(all_days) else "warning"),
    ]

    charts = []
    choice = ctx.opt("chart")
    if choice in ("both", "rate"):
        points = []
        for d in all_days:
            rate = _pass_rate(days[d][1], days[d][2]) if d in days else None
            points.append({"t": noon(d), "v": round(rate, 2) if rate is not None else None})
        values = [p["v"] for p in points if p["v"] is not None]
        lo = max(0.0, (min(values) // 5) * 5 - 5) if values else 0.0
        charts.append({"title": "Pass rate per day",
                       "series": {"kind": "line", "unit": "%", "color": "primary",
                                  "y_domain": [lo, 100], "points": points}})
    if choice in ("both", "outcomes"):
        def pts(i: int) -> list[dict]:
            return [{"t": noon(d), "v": days[d][i] if d in days else None} for d in all_days]
        totals = [{"t": noon(d), "v": sum(days[d][1:5]) if d in days else None} for d in all_days]
        charts.append({"title": "Check outcomes per day",
                       "series": {"kind": "stacked", "unit": "checks", "points": totals,
                                  "series": [{"name": "Passed", "color": "success", "points": pts(1)},
                                             {"name": "Failed", "color": "danger", "points": pts(2)},
                                             {"name": "Not assessable", "color": "neutral",
                                              "points": pts(3)},
                                             {"name": "Excepted", "color": "warning",
                                              "points": pts(4)}]}})

    policy_rows = []
    for name, series in policies.items():
        f, l_ = series[0], series[-1]
        r_first, r_last = _pass_rate(f[2], f[3]), _pass_rate(l_[2], l_[3])
        delta = (f"{r_last - r_first:+.2f} pts" if r_first is not None and r_last is not None
                 and f[0] != l_[0] else "—")
        status = "non-compliant" if l_[3] else "unknown" if l_[4] else "compliant"
        policy_rows.append([_clip(name, 80), f"{l_[1]:,}", _fmt_pct(r_first), _fmt_pct(r_last),
                            delta, f"{l_[3]:,}", f"{l_[4]:,}", f"{l_[5]:,}", status])
    policy_rows.sort(key=lambda r: ({"non-compliant": 0, "unknown": 1}.get(r[8], 2), r[0].lower()))
    daily_rows = [[_fmt_day(ctx, d), f"{t[0]:,}", f"{t[1]:,}", f"{t[2]:,}", f"{t[3]:,}",
                   f"{t[4]:,}", _fmt_pct(_pass_rate(t[1], t[2]))]
                  for d, t in sorted(days.items(), reverse=True)]

    notes[:0] = [
        "Pass rate = passed ÷ (passed + failed) checks. Checks that could not be assessed (no "
        "validated configuration) and excepted checks are shown separately and excluded from the "
        "rate.",
        "One point per UTC day: the state recorded by the last evaluation that day. Days without "
        "an evaluation have no data and are drawn as gaps, never as zero.",
        "Totals add up every policy, so a device assessed by two policies is counted in both.",
    ]
    return _section(
        "ncm_compliance_trend", title,
        description=(f"Configuration-compliance outcomes per day from {_fmt_day(ctx, d0)} to "
                     f"{_fmt_day(ctx, d1)}, fleet-wide."),
        kpis=kpis, charts=charts,
        tables=[{"title": "By policy (first and last recorded day in the period)",
                 "headers": ["Policy", "Devices", "Pass rate (first)", "Pass rate (last)",
                             "Change", "Failed", "Not assessable", "Excepted", "Status"],
                 "styles": ["text", "num", "pct", "pct", "num", "num", "num", "num", "status"],
                 "rows": policy_rows},
                {"title": "Daily snapshots (newest first)",
                 "headers": ["Day", "Device–policy pairs", "Passed", "Failed", "Not assessable",
                             "Excepted", "Pass rate"],
                 "styles": ["text", "num", "num", "num", "num", "num", "pct"],
                 "rows": daily_rows}],
        notes=notes)


# ─── Compliance exceptions ──────────────────────────────────────────────────

def _exception_sql(covered: str, where: str) -> str:
    return ("SELECT ru.title, ru.severity, p.name, e.device_id IS NULL, "
            "CASE WHEN e.device_id IS NULL THEN NULL ELSE " + DEVICE_NAME + " END, "
            "e.owner, e.reason, e.created_at, e.expires_at, "
            "(SELECT count(*) FROM ncm_compliance_results r WHERE r.exception_id = e.id "
            f"AND r.status = 'excepted'{covered}) "
            "FROM ncm_compliance_exceptions e JOIN ncm_policy_rules ru ON ru.id = e.rule_id "
            "JOIN ncm_policies p ON p.id = ru.policy_id LEFT JOIN devices d ON d.id = e.device_id "
            f"WHERE e.revoked_at IS NULL AND {where}")


@register_section(
    "ncm_compliance_exceptions", title="Compliance Exceptions", category="Configuration",
    description=("Active compliance exceptions with their owner, reason and expiry, as of report "
                 "generation, highlighting those that expire soon and those that recently lapsed."),
    scope="device", requires=("ncm.view",), semantics="snapshot",
    options=[{"key": "days", "label": "Expiring within (days)", "type": "int", "default": 14,
              "min": 1, "max": 180},
             top_n_option(50, 500)],
)
async def ncm_compliance_exceptions(ctx: SectionCtx) -> dict:
    title = "Compliance Exceptions"
    ids = ctx.device_ids
    within = int(ctx.opt("days"))
    params: dict[str, Any] = {"days": within}
    dev = covered = ""
    if ids:
        # Fleet-wide exceptions (no device) apply to the selected devices too,
        # exactly as the compliance workspace lists them.
        dev = " AND (e.device_id IS NULL OR e.device_id::text = ANY(:dev_ids))"
        covered = " AND r.device_id::text = ANY(:dev_ids)"
        params["dev_ids"] = ids
    rows = (await ctx.db.execute(text(
        _exception_sql(covered, f"e.expires_at > now(){dev}") + " ORDER BY e.expires_at, ru.title"),
        params)).fetchall()
    # Lapsed = expired by date (not revoked) in the last N days: the excepted
    # checks count as failed again unless the exception is renewed.
    lapsed = (await ctx.db.execute(text(
        _exception_sql(covered, "e.expires_at <= now() AND e.expires_at > now() - "
                                f"make_interval(days => CAST(:days AS integer)){dev}")
        + " ORDER BY e.expires_at DESC, ru.title"), params)).fetchall()
    if not rows and not lapsed:
        return _section("ncm_compliance_exceptions", title,
                        description="Active compliance exceptions, as of report generation.",
                        kpis=[_kpi("Active exceptions", "0", "success")],
                        notes=["No active compliance exceptions, and none lapsed in the last "
                               f"{within} days."])

    now = _now()
    top_n = int(ctx.opt("top_n"))

    def applies(r) -> str:
        return "All devices" if r[3] else (r[4] or "—")

    expiring = fleet_wide = severe = checks = 0
    out = []
    for r in rows:
        left = (_aware(r[8]) - now).total_seconds() if r[8] else None
        soon = left is not None and left <= within * 86400
        expiring += soon
        fleet_wide += bool(r[3])
        severity = SEVERITY_MAP.get((r[1] or "").lower(), "info")
        severe += severity in ("critical", "major")
        checks += _n(r[9])
        out.append([f"{_clip(r[0], 80)} ({_clip(r[2], 40)})", severity, applies(r),
                    _clip(r[5], 40), _clip(r[6], 80), ctx.fmt_date(r[8]),
                    _days(left) if left is not None else "—", f"{_n(r[9]):,}",
                    "expiring" if soon else "active"])
    tables = []
    if out:
        tables.append({"title": _truncated("Active exceptions (soonest expiry first)",
                                           min(top_n, len(out)), len(out)),
                       "headers": ["Rule (policy)", "Severity", "Applies to", "Owner", "Reason",
                                   "Expires", "Days left", "Checks excepted", "State"],
                       "styles": ["text", "severity", "text", "text", "text", "num", "num",
                                  "num", "status"],
                       "rows": out[:top_n]})
    if lapsed:
        tables.append({"title": _truncated(f"Lapsed in the last {within} days",
                                           min(top_n, len(lapsed)), len(lapsed)),
                       "headers": ["Rule (policy)", "Severity", "Applies to", "Owner", "Reason",
                                   "Expired", "State"],
                       "styles": ["text", "severity", "text", "text", "text", "num", "status"],
                       "rows": [[f"{_clip(r[0], 80)} ({_clip(r[2], 40)})",
                                 SEVERITY_MAP.get((r[1] or "").lower(), "info"), applies(r),
                                 _clip(r[5], 40), _clip(r[6], 80), ctx.fmt_date(r[8]), "expired"]
                                for r in lapsed[:top_n]]})
    notes = ["An exception is an accepted risk with a named owner and an expiry: while it is active "
             "the excepted check does not count as failed. Checks excepted is the number of "
             "device/rule results the exception covers in the latest assessment."]
    if lapsed:
        notes.append("A lapsed exception no longer applies: its checks count as failed or not "
                     "assessable again from the next evaluation unless it is renewed.")
    if ids and (fleet_wide or any(r[3] for r in lapsed)):
        notes.append("Fleet-wide exceptions apply to every device, including the selected ones.")
    if not rows:
        notes.insert(0, "No active compliance exceptions.")
    return _section(
        "ncm_compliance_exceptions", title,
        description=f"{len(rows):,} active compliance exception(s), as of report generation.",
        kpis=[
            _kpi("Active exceptions", f"{len(rows):,}", "info" if rows else "success"),
            _kpi(f"Expiring within {within} days", f"{expiring:,}",
                 "warning" if expiring else "success"),
            _kpi(f"Lapsed in the last {within} days", f"{len(lapsed):,}",
                 "warning" if lapsed else "success", "expired without renewal"),
            _kpi("Fleet-wide", f"{fleet_wide:,}", "warning" if fleet_wide else "neutral",
                 "apply to every device"),
            _kpi("Critical or major rules", f"{severe:,}", "danger" if severe else "success",
                 "among active exceptions"),
            _kpi("Checks excepted", f"{checks:,}", "primary", "latest assessment"),
        ],
        tables=tables, notes=notes)


# ─── Configuration change audit ─────────────────────────────────────────────

CHANGED_RUN = ("r.status = 'success' AND r.validated AND r.is_change "
               "AND COALESCE(r.finished_at, r.started_at) BETWEEN :f AND :t")
# A device's first capture is flagged is_change but is not a change.
FIRST_CAPTURE = ("(NOT EXISTS (SELECT 1 FROM ncm_backup_runs p WHERE p.device_id = r.device_id "
                 "AND p.config_type = r.config_type AND p.status = 'success' AND p.validated "
                 "AND p.started_at < r.started_at) "
                 "AND NOT EXISTS (SELECT 1 FROM device_configs pc WHERE pc.device_id = r.device_id "
                 "AND pc.config_type = r.config_type AND pc.captured_at < r.started_at))")
JOB_CHANGED = ("EXISTS (SELECT 1 FROM ncm_backup_runs jr WHERE jr.job_id = e.job_id "
               "AND jr.status = 'success' AND jr.validated AND jr.is_change)")


def _event_outcome(processed: bool, has_job: bool, job_status: Optional[str],
                   changed: Optional[bool]) -> str:
    if not processed:
        return "Awaiting backup"
    if not has_job:
        return "Not captured: device not ready"
    if changed is True:
        return "Change captured"
    if changed is False:
        return "No difference found"
    if job_status in ("queued", "running"):
        return "Backup in progress"
    if job_status == "failed":
        return "Backup failed"
    if job_status == "cancelled":
        return "Backup cancelled"
    return "Not captured"


@register_section(
    "ncm_change_audit", title="Configuration Change Audit", category="Configuration",
    description=("Configuration-change notifications (syslog, SNMP trap, API) with the reported user "
                 "and whether a validated backup captured the change."),
    scope="device", requires=("ncm.view",), semantics="window",
    options=[{"key": "source", "label": "Notification source", "type": "select", "default": "all",
              "choices": [["all", "All sources"], ["syslog", "Syslog"], ["trap", "SNMP traps"],
                          ["api", "API (change management)"]]},
             top_n_option(50, 500)],
)
async def ncm_change_audit(ctx: SectionCtx) -> dict:
    title = "Configuration Change Audit"
    clause, params = ctx.pg_device_clause("d.id")
    params.update({"f": ctx.frm, "t": ctx.to, "org_tz": ctx.tz_name})
    source = ctx.opt("source")
    src = ""
    if source != "all":
        src = " AND e.source = :source"
        params["source"] = source
    ev_where = f" FROM ncm_change_events e JOIN devices d ON d.id = e.device_id " \
               f"WHERE e.received_at BETWEEN :f AND :t{src}{clause}"
    run_where = (" FROM ncm_backup_runs r JOIN devices d ON d.id = r.device_id "
                 f"WHERE {CHANGED_RUN}{clause}")

    ev = (await ctx.db.execute(text(
        "SELECT count(*), count(DISTINCT e.device_id), "
        "count(*) FILTER (WHERE e.reported_user IS NOT NULL), "
        "count(*) FILTER (WHERE e.processed_at IS NULL), "
        f"count(*) FILTER (WHERE {JOB_CHANGED}), "
        "count(*) FILTER (WHERE e.source = 'syslog'), count(*) FILTER (WHERE e.source = 'trap'), "
        "count(*) FILTER (WHERE e.source = 'api')" + ev_where), params)).first()
    runs = (await ctx.db.execute(text(
        f"SELECT count(*) FILTER (WHERE NOT {FIRST_CAPTURE}), "
        f"count(*) FILTER (WHERE {FIRST_CAPTURE}), "
        f"count(DISTINCT r.device_id) FILTER (WHERE NOT {FIRST_CAPTURE}), "
        f"count(*) FILTER (WHERE NOT {FIRST_CAPTURE} AND NOT EXISTS (SELECT 1 FROM ncm_change_events x "
        "WHERE x.job_id = r.job_id))" + run_where), params)).first()
    events_n, ev_devices, with_user, pending, confirmed = (_n(v) for v in (ev or [0] * 5)[:5])
    by_source = [_n(v) for v in (ev or [0] * 8)[5:8]] if ev else [0, 0, 0]
    changes_n, initial_n, change_devices, unannounced = (_n(v) for v in (runs or [0] * 4)[:4])

    note_user = ("Reported user is the account named in the device's (or the calling system's) own "
                 "message. It is not a verified identity, and it is blank when the message names "
                 "nobody.")
    if not events_n and not changes_n:
        notes = ["No configuration-change notifications or captured changes in this period.",
                 note_user, NO_CONFIG_TEXT_NOTE]
        if initial_n:
            notes.insert(1, f"{initial_n:,} first capture(s) were taken; a device's first capture "
                            "is not a change.")
        return _section("ncm_change_audit", title,
                        description="Configuration-change notifications and captured changes.",
                        notes=notes)

    ev_daily = (await ctx.db.execute(text(
        f"SELECT {_pg_day('e.received_at')}, count(*){ev_where} GROUP BY 1"), params)).fetchall()
    run_daily = (await ctx.db.execute(text(
        f"SELECT {_pg_day('COALESCE(r.finished_at, r.started_at)')}, count(*){run_where} "
        f"AND NOT {FIRST_CAPTURE} GROUP BY 1"), params)).fetchall()
    top_n = int(ctx.opt("top_n"))
    lp = dict(params, lim=top_n)
    ev_rows = (await ctx.db.execute(text(
        f"SELECT e.received_at, {DEVICE_NAME}, e.source, e.summary, e.reported_user, "
        "e.processed_at IS NOT NULL, e.job_id IS NOT NULL, j.status, "
        "(SELECT bool_or(COALESCE(jr.is_change, false)) FROM ncm_backup_runs jr "
        "WHERE jr.job_id = e.job_id AND jr.status = 'success' AND jr.validated), "
        "(SELECT max(COALESCE(jr.finished_at, jr.started_at)) FROM ncm_backup_runs jr "
        "WHERE jr.job_id = e.job_id AND jr.status = 'success' AND jr.validated) "
        "FROM ncm_change_events e JOIN devices d ON d.id = e.device_id "
        "LEFT JOIN ncm_jobs j ON j.id = e.job_id "
        f"WHERE e.received_at BETWEEN :f AND :t{src}{clause} "
        "ORDER BY e.received_at DESC LIMIT :lim"), lp)).fetchall()
    run_rows = (await ctx.db.execute(text(
        f"SELECT COALESCE(r.finished_at, r.started_at), {DEVICE_NAME}, r.config_type, r.trigger, "
        "(SELECT count(*) FROM ncm_change_events x WHERE x.job_id = r.job_id), "
        "(SELECT string_agg(DISTINCT x.reported_user, ', ') FROM ncm_change_events x "
        "WHERE x.job_id = r.job_id AND x.reported_user IS NOT NULL)"
        f"{run_where} AND NOT {FIRST_CAPTURE} "
        "ORDER BY 1 DESC LIMIT :lim"), lp)).fetchall()

    ev_counts = {_n(r[0]): _n(r[1]) for r in ev_daily}
    run_counts = {_n(r[0]): _n(r[1]) for r in run_daily}
    chart = {"title": "Change notifications and captured changes per day",
             "series": {"kind": "bars", "unit": "changes",
                        "points": _daily_points(ctx, ev_counts),
                        "series": [{"name": "Notifications", "color": "info",
                                    "points": _daily_points(ctx, ev_counts)},
                                   {"name": "Captured changes", "color": "primary",
                                    "points": _daily_points(ctx, run_counts)}]}}

    src_label = {"all": "all sources", "syslog": "syslog", "trap": "SNMP traps",
                 "api": "the API"}[source] if source in ("all", "syslog", "trap", "api") else source
    kpis = [
        _kpi("Change notifications", f"{events_n:,}", "info",
             (f"syslog {by_source[0]:,} · trap {by_source[1]:,} · API {by_source[2]:,}"
              if source == "all" else f"from {src_label}")),
        _kpi("Devices notifying", f"{ev_devices:,}", "primary"),
        _kpi("Confirmed by backup", f"{confirmed:,} of {events_n:,}",
             "success" if events_n and confirmed == events_n else "warning" if events_n else "neutral",
             "a validated capture found a difference"),
        _kpi("Captured changes", f"{changes_n:,}", "primary",
             f"on {change_devices:,} device(s)"),
        _kpi("Changes without a notification", f"{unannounced:,}",
             "warning" if unannounced else "success", "found only by a backup"),
        _kpi("With a reported user", f"{with_user:,} of {events_n:,}", "neutral", "unverified"),
    ]
    tables = []
    if ev_rows:
        tables.append({
            "title": _truncated("Change notifications", len(ev_rows), events_n),
            "headers": ["Received", "Device", "Source", "Summary", "Reported user (unverified)",
                        "Backup outcome", "Backup completed"],
            "styles": ["num", "text", "text", "text", "text", "text", "num"],
            "rows": [[ctx.fmt_short(r[0]), r[1], SOURCE_LABELS.get(r[2], r[2] or "—"),
                      _clip(r[3], 80), _clip(r[4], 40),
                      _event_outcome(bool(r[5]), bool(r[6]), r[7], r[8]),
                      ctx.fmt_short(r[9])] for r in ev_rows]})
    if run_rows:
        tables.append({
            "title": _truncated("Captured configuration changes", len(run_rows), changes_n),
            "headers": ["Captured", "Device", "Type", "Trigger", "Notifications",
                        "Reported user (unverified)"],
            "styles": ["num", "text", "text", "text", "num", "text"],
            "rows": [[ctx.fmt_short(r[0]), r[1], (r[2] or "—").capitalize(),
                      TRIGGER_LABELS.get(r[3], r[3] or "—"), f"{_n(r[4]):,}", _clip(r[5], 60)]
                     for r in run_rows]})
    notes = [note_user,
             "A change is proven only by a validated backup that found a configuration difference; "
             "a notification without a captured change can be a reverted or unsaved edit, or a "
             "message that did not alter the configuration.",
             NO_CONFIG_TEXT_NOTE]
    if pending:
        notes.append(f"{pending:,} notification(s) are still waiting for their debounced backup.")
    if initial_n:
        notes.append(f"{initial_n:,} first capture(s) in this period are not counted as changes.")
    if source != "all":
        notes.append("Captured changes and changes without a notification are counted whatever "
                     "the notification source; only the notifications are limited to "
                     f"{src_label}.")
    return _section(
        "ncm_change_audit", title,
        description=(f"Configuration-change notifications from {src_label} and changes captured by "
                     "validated backups in the period."),
        kpis=kpis, charts=[chart], tables=tables, notes=notes)


# ─── Restore readiness ──────────────────────────────────────────────────────

READINESS_ORDER = {"failed": 0, "warning": 1, "untested": 2, "stale": 3, "ok": 4}


@register_section(
    "ncm_restore_readiness", title="Restore Readiness", category="Configuration",
    description=("The latest recorded restore test of every device with an enabled configuration-"
                 "backup enrollment, its age and the changes since the tested version, as of report "
                 "generation."),
    scope="device", requires=("ncm.view",), semantics="snapshot",
    options=[{"key": "max_age_days", "label": "A passed test is current for (days)", "type": "int",
              "default": 180, "min": 7, "max": 730},
             top_n_option(50, 1000)],
)
async def ncm_restore_readiness(ctx: SectionCtx) -> dict:
    title = "Restore Readiness"
    clause, params = ctx.pg_device_clause("d.id")
    rows = (await ctx.db.execute(text(
        f"SELECT {DEVICE_NAME}, t.result, t.tested_at, t.method, t.actor_name, "
        "c.captured_at, c.config_type, "
        "CASE WHEN c.id IS NULL THEN NULL ELSE (SELECT count(*) FROM device_configs x "
        "WHERE x.device_id = d.id AND x.config_type = c.config_type AND x.validated "
        "AND x.is_change AND x.captured_at > c.captured_at) END "
        "FROM device_ncm n JOIN devices d ON d.id = n.device_id "
        "LEFT JOIN LATERAL (SELECT rt.result, rt.tested_at, rt.method, rt.actor_name, rt.version_id "
        "FROM ncm_restore_tests rt WHERE rt.device_id = d.id "
        "ORDER BY rt.tested_at DESC LIMIT 1) t ON TRUE "
        "LEFT JOIN device_configs c ON c.id = t.version_id "
        f"WHERE n.enabled{clause} ORDER BY 1"), params)).fetchall()
    proof_note = ("Only a recorded restore test demonstrates recovery: a validated backup proves the "
                  "configuration was collected, not that it can be restored. Tests are performed and "
                  "recorded by operators; ZenPlus never writes to a device to test.")
    if not rows:
        return _section("ncm_restore_readiness", title,
                        description="Restore evidence per enrolled device, as of report generation.",
                        notes=["No devices in scope have an enabled configuration-backup enrollment.",
                               proof_note])

    max_age = int(ctx.opt("max_age_days"))
    now = _now()
    out: list[tuple] = []
    counts = {"ok": 0, "stale": 0, "warning": 0, "failed": 0, "untested": 0}
    changed_since = 0
    for r in rows:
        result = (r[1] or "").lower()
        tested = _aware(r[2])
        age_s = (now - tested).total_seconds() if tested else None
        if not result:
            status, label = "untested", "Never tested"
        elif result == "failed":
            status, label = "failed", "Failed"
        elif result == "partial":
            status, label = "warning", "Partial"
        elif age_s is not None and age_s > max_age * 86400:
            status, label = "stale", "Passed"
        else:
            status, label = "ok", "Passed"
        counts[status] += 1
        newer = None if r[7] is None else _n(r[7])
        changed_since += bool(newer)
        version = (f"{(r[6] or 'running').capitalize()} · {ctx.fmt_short(r[5])}" if r[5] else "—")
        out.append(((READINESS_ORDER[status], -(age_s or 0), (r[0] or "").lower()),
                    [r[0], label, ctx.fmt_date(tested) if tested else "—",
                     _days(age_s) if age_s is not None else "—",
                     METHOD_LABELS.get(r[3], r[3] or "—") if result else "—", version,
                     f"{newer:,}" if newer is not None else "—", _clip(r[4], 32) if result else "—",
                     status]))
    out.sort(key=lambda o: o[0])
    total = len(rows)
    top_n = int(ctx.opt("top_n"))
    ready = counts["ok"]
    return _section(
        "ncm_restore_readiness", title,
        description=(f"Restore evidence for {total:,} device(s) with an enabled configuration-backup "
                     "enrollment, as of report generation."),
        kpis=[
            _kpi("Restore-tested and current", f"{ready:,} of {total:,}",
                 "success" if ready == total else "warning" if ready else "danger",
                 f"{ready / total * 100:.1f}% passed within {max_age} days"),
            _kpi("Never tested", f"{counts['untested']:,}", "warning" if counts["untested"] else "success"),
            _kpi(f"Passed over {max_age} days ago", f"{counts['stale']:,}",
                 "warning" if counts["stale"] else "success"),
            _kpi("Partial or failed", f"{counts['warning'] + counts['failed']:,}",
                 "danger" if counts["failed"] else "warning" if counts["warning"] else "success"),
            _kpi("Changed since tested version", f"{changed_since:,}",
                 "warning" if changed_since else "neutral",
                 "newer validated changes exist"),
        ],
        charts=[{"title": "Devices by restore evidence",
                 "series": {"kind": "donut", "unit": "devices",
                            "points": [{"label": lbl, "value": counts[k]} for k, lbl in (
                                ("ok", "Passed (current)"), ("stale", f"Passed (over {max_age} d)"),
                                ("warning", "Partial"), ("failed", "Failed"),
                                ("untested", "Never tested")) if counts[k]]}}],
        tables=[{"title": _truncated("Restore evidence by device (least ready first)",
                                     min(top_n, len(out)), len(out)),
                 "headers": ["Device", "Latest result", "Tested", "Age", "Method", "Version tested",
                             "Changes since", "Recorded by", "Readiness"],
                 "styles": ["text", "text", "num", "num", "text", "text", "num", "text", "status"],
                 "rows": [o[1] for o in out[:top_n]]}],
        notes=[proof_note,
               "Changes since counts validated configuration changes captured after the tested "
               "version that are still retained; older versions may have been pruned, so it is a "
               "lower bound."])


# ─── Backup-failure incidents ───────────────────────────────────────────────

@register_section(
    "ncm_backup_incidents", title="Backup Failure Incidents", category="Configuration",
    description=("Configuration-backup failure incidents open during the period, their causes, "
                 "recommended actions and mean time to recovery."),
    scope="device", requires=("ncm.view",), semantics="window",
    options=[top_n_option(50, 500)],
)
async def ncm_backup_incidents(ctx: SectionCtx) -> dict:
    title = "Backup Failure Incidents"
    clause, params = ctx.pg_device_clause("d.id")
    params.update({"f": ctx.frm, "t": ctx.to})
    where = (" FROM ncm_incidents i JOIN devices d ON d.id = i.device_id "
             "WHERE i.kind = 'backup_failure' AND i.opened_at <= :t "
             f"AND (i.recovered_at IS NULL OR i.recovered_at >= :f){clause}")
    recovered = "i.recovered_at BETWEEN :f AND :t"
    ttr = "EXTRACT(EPOCH FROM (i.recovered_at - i.opened_at))"
    agg = (await ctx.db.execute(text(
        "SELECT count(*), count(*) FILTER (WHERE i.opened_at >= :f), "
        "count(*) FILTER (WHERE i.recovered_at IS NULL OR i.recovered_at > :t), "
        f"count(*) FILTER (WHERE {recovered}), avg({ttr}) FILTER (WHERE {recovered}), "
        f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {ttr}) FILTER (WHERE {recovered}), "
        "count(DISTINCT i.device_id), COALESCE(sum(i.occurrences), 0)" + where), params)).first()
    pf, pt = ctx.prior_window()
    prior = (await ctx.db.execute(text(
        "SELECT count(*) FROM ncm_incidents i JOIN devices d ON d.id = i.device_id "
        f"WHERE i.kind = 'backup_failure' AND i.opened_at >= :pf AND i.opened_at < :pt{clause}"),
        dict(params, pf=pf, pt=pt))).scalar()
    total = _n(agg[0]) if agg else 0
    if not total:
        return _section("ncm_backup_incidents", title,
                        description="Configuration-backup failure incidents in the period.",
                        kpis=[_kpi("Incidents", "0", "success")],
                        notes=["No configuration-backup failure incidents were open in this period."])

    opened, open_end, rec_n = _n(agg[1]), _n(agg[2]), _n(agg[3])
    mttr = float(agg[4]) if agg[4] is not None else None
    median = float(agg[5]) if agg[5] is not None else None
    devices, attempts = _n(agg[6]), _n(agg[7])

    causes = (await ctx.db.execute(text(
        "SELECT COALESCE(i.failure_category, 'unknown'), count(*), count(DISTINCT i.device_id)"
        + where + " GROUP BY 1 ORDER BY 2 DESC, 1"), params)).fetchall()
    top_n = int(ctx.opt("top_n"))
    listing = (await ctx.db.execute(text(
        f"SELECT {DEVICE_NAME}, i.opened_at, i.recovered_at, i.occurrences, i.failure_category, "
        "i.last_message" + where +
        " ORDER BY (i.recovered_at IS NULL OR i.recovered_at > :t) DESC, i.opened_at DESC "
        "LIMIT :lim"), dict(params, lim=top_n))).fetchall()
    try:
        from app.services.ncm_errors import GUIDANCE
    except Exception:  # the collector stack is optional for rendering
        GUIDANCE = {}

    end = min(_aware(ctx.to), _now())
    rows = []
    for r in listing:
        op, rec = _aware(r[1]), _aware(r[2])
        closed = rec is not None and rec <= _aware(ctx.to)
        dur = _duration(((rec if closed else end) - op).total_seconds()) if op else "—"
        rows.append([r[0], ctx.fmt_short(op), ctx.fmt_short(rec) if closed else "Open",
                     dur if closed else f"{dur} (ongoing)", f"{_n(r[3]):,}",
                     _failure_label(r[4]), _clip(r[5], 100), "resolved" if closed else "open"])
    cause_rows = [[_failure_label(c[0]), f"{_n(c[1]):,}", f"{_n(c[2]):,}",
                   GUIDANCE.get(c[0]) or "—"] for c in causes]
    prior_n = _n(prior) if prior is not None else None
    return _section(
        "ncm_backup_incidents", title,
        description=(f"{total:,} backup-failure incident(s) on {devices:,} device(s) were open during "
                     "the period."),
        kpis=[
            _kpi("Incidents in period", f"{total:,}", "warning", f"on {devices:,} device(s)"),
            _kpi("Opened in period", f"{opened:,}", "warning" if opened else "success",
                 _change(opened, prior_n, "", 0, "vs previous period")),
            _kpi("Open at period end", f"{open_end:,}", "danger" if open_end else "success"),
            _kpi("Recovered in period", f"{rec_n:,}", "success" if rec_n else "neutral"),
            _kpi("Mean time to recovery", _duration(mttr), "info",
                 f"median {_duration(median)}" if median is not None else "no recoveries in period"),
            _kpi("Failed backup jobs", f"{attempts:,}", "neutral", "across these incidents"),
        ],
        charts=[{"title": "Incidents by cause",
                 "series": {"kind": "rank", "unit": "incidents", "color": "warning",
                            "points": [{"label": _failure_label(c[0]), "value": _n(c[1])}
                                       for c in causes[:10]]}}] if causes else [],
        tables=[{"title": "Causes and recommended actions",
                 "headers": ["Cause", "Incidents", "Devices", "Recommended action"],
                 "styles": ["text", "num", "num", "text"], "rows": cause_rows},
                {"title": _truncated("Incidents (open first)", len(rows), total),
                 "headers": ["Device", "Opened", "Recovered", "Duration", "Failed jobs", "Cause",
                             "Last error", "Status"],
                 "styles": ["text", "num", "num", "num", "num", "text", "text", "status"],
                 "rows": rows}],
        notes=["One incident collects a device's repeated failed backup jobs and closes on its next "
               "successful validated capture. Time to recovery is measured from the first failure to "
               "that capture; mean and median cover incidents recovered within the period.",
               "Cause is the category of the incident's most recent failure."])


# ─── Credential hygiene (fleet) ─────────────────────────────────────────────

# Same test as the NCCM overview's profile_configured: a username and the
# secret its authentication method needs (only presence is read).
PROFILE_CONFIGURED = ("(COALESCE(c.username, '') <> '' AND (COALESCE(octet_length(c.password_enc), 0) > 0 "
                      "OR (c.auth_method = 'key' AND c.private_key_enc IS NOT NULL)))")
CREDENTIAL_PROBLEMS = {
    "no_profile": "No connection profile assigned",
    "no_secret": "Profile has no saved secret",
    "authentication": "Authentication rejected by the device",
    "credentials": "Saved secret could not be decrypted",
}
SSH_TRUST_NOTE = ("SSH host keys are not verified for NCCM connections (since release 1.23.29 the key "
                  "a device presents is accepted), so a device impersonating another on the management "
                  "network could receive a profile's credentials. Keep the management network "
                  "restricted, use dedicated least-privilege backup accounts and rotate shared "
                  "profiles.")


@register_section(
    "ncm_credential_hygiene", title="NCM Credential Hygiene", category="Security",
    description=("NCCM connection profiles overdue for secret rotation or using cleartext or legacy "
                 "SSH settings, and enrolled devices whose backups fail on credentials, as of report "
                 "generation."),
    scope="fleet", requires=("ncm.view",), semantics="snapshot",
    options=[{"key": "due_days", "label": "Rotation due within (days)", "type": "int", "default": 14,
              "min": 1, "max": 90},
             top_n_option(50, 500)],
)
async def ncm_credential_hygiene(ctx: SectionCtx) -> dict:
    title = "NCM Credential Hygiene"
    profiles = (await ctx.db.execute(text(
        "SELECT c.name, COALESCE(c.protocol, 'ssh'), COALESCE(c.auth_method, 'password'), "
        "c.secret_rotated_at, c.rotation_days, c.is_default, COALESCE(c.ssh_legacy_compat, false), "
        f"{PROFILE_CONFIGURED}, "
        "(SELECT count(*) FROM device_ncm n WHERE n.credential_id = c.id), "
        "(SELECT count(*) FROM device_ncm n WHERE n.credential_id = c.id AND n.enabled) "
        "FROM ncm_credentials c ORDER BY c.name"))).fetchall()
    top_n = int(ctx.opt("top_n"))
    problem = ("CASE WHEN n.credential_id IS NULL OR c.id IS NULL THEN 'no_profile' "
               f"WHEN NOT {PROFILE_CONFIGURED} THEN 'no_secret' "
               "WHEN n.last_status = 'failed' AND n.last_failure_category IN "
               "('authentication', 'credentials') THEN n.last_failure_category END")
    devices = (await ctx.db.execute(text(
        f"SELECT * FROM (SELECT {DEVICE_NAME} AS device, host(d.ip_address) AS ip, c.name AS profile, "
        f"{problem} AS problem, n.last_attempt_at, n.last_success_at "
        "FROM device_ncm n JOIN devices d ON d.id = n.device_id "
        "LEFT JOIN ncm_credentials c ON c.id = n.credential_id WHERE n.enabled) x "
        "WHERE x.problem IS NOT NULL"))).fetchall()
    enrolled = _n((await ctx.db.execute(text(
        "SELECT count(*) FROM device_ncm n WHERE n.enabled"))).scalar())

    secrets_note = ("Secrets, usernames and key material are never included: this section reports "
                    "rotation dates, settings and failure categories only.")
    notes = [secrets_note]
    if ctx.device_ids:
        notes.append("Connection profiles are shared across the fleet; the device selection does "
                     "not apply to this section.")
    if not profiles and not devices:
        notes.insert(0, "No NCCM connection profiles are configured and no enrolled device is "
                        "waiting for one.")
        return _section("ncm_credential_hygiene", title,
                        description="NCCM connection profiles, as of report generation.",
                        notes=notes)

    due_days = int(ctx.opt("due_days"))
    now = _now()
    overdue = due_soon = untracked = weak = 0
    rows: list[tuple] = []
    for r in profiles:
        name, protocol, auth, rotated, rot_days = r[0], str(r[1] or "ssh"), r[2], _aware(r[3]), r[4]
        used, enabled = _n(r[8]), _n(r[9])
        next_due = rotated + timedelta(days=int(rot_days)) if rotated and rot_days else None
        policy = f"{int(rot_days):,} d" if rot_days else "Not set"
        if not rot_days:
            status = "untracked"
            untracked += 1
        elif not rotated:
            status = "unknown"
            untracked += 1
        elif (now - rotated).days >= int(rot_days):
            status = "expired"
            overdue += 1
        elif next_due - now <= timedelta(days=due_days):
            status = "warning"
            due_soon += 1
        else:
            status = "ok"
        flags = []
        if r[5]:
            flags.append("Default profile")
        if protocol.lower() != "ssh":
            flags.append(f"{protocol.upper()} (cleartext)")
        if r[6]:
            flags.append("Legacy SSH algorithms")
        if not r[7]:
            flags.append("No secret saved")
        weak += bool(r[6] or protocol.lower() != "ssh")
        rank = {"expired": 0, "warning": 1, "unknown": 2, "untracked": 3, "ok": 4}[status]
        rows.append(((rank, -used, (name or "").lower()),
                     [_clip(name, 60), "SSH key" if auth == "key" else "Password",
                      f"{enabled:,} of {used:,}" if used != enabled else f"{used:,}",
                      ctx.fmt_date(rotated) if rotated else "Not recorded", policy,
                      ctx.fmt_date(next_due) if next_due else "—", "; ".join(flags) or "—", status]))
    rows.sort(key=lambda x: x[0])
    in_use = sum(1 for r in profiles if _n(r[8]))

    order = list(CREDENTIAL_PROBLEMS)
    devices = sorted(devices, key=lambda d: (order.index(d[3]) if d[3] in order else 99,
                                             str(d[0] or "").lower()))
    tables = []
    if rows:
        tables.append({"title": _truncated("Connection profiles (most urgent first)",
                                           min(top_n, len(rows)), len(rows)),
                       "headers": ["Profile", "Authentication", "Devices (enabled)", "Last rotated",
                                   "Rotate every", "Next due", "Flags", "Status"],
                       "styles": ["text", "text", "num", "num", "num", "num", "text", "status"],
                       "rows": [x[1] for x in rows[:top_n]]})
    if devices:
        tables.append({"title": _truncated("Enrolled devices with credential problems",
                                           min(top_n, len(devices)), len(devices)),
                       "headers": ["Device", "IP address", "Profile", "Problem", "Last attempt",
                                   "Last successful backup"],
                       "styles": ["text", "mono", "text", "text", "num", "num"],
                       "rows": [[d[0], d[1] or "—", _clip(d[2], 60),
                                 CREDENTIAL_PROBLEMS.get(d[3], str(d[3])),
                                 ctx.fmt_short(d[4]), ctx.fmt_short(d[5]) if d[5] else "never"]
                                for d in devices[:top_n]]})
    notes[:0] = [
        "Rotation is measured from the last time a profile's password or key was replaced in "
        "ZenPlus. A profile without a rotation policy, or without a recorded rotation date, is "
        "not tracked.",
        "Credential problems come from each enabled enrollment's latest backup attempt "
        "(authentication rejected, or a secret that could not be decrypted) and from enrollments "
        "without a usable profile.",
        SSH_TRUST_NOTE,
    ]
    if not profiles:
        notes.insert(0, "No NCCM connection profiles are configured.")
    return _section(
        "ncm_credential_hygiene", title,
        description=(f"{len(profiles):,} NCCM connection profile(s) and the credentials of "
                     f"{enrolled:,} enabled enrollment(s), as of report generation."),
        kpis=[
            _kpi("Connection profiles", f"{len(profiles):,}", "primary", f"{in_use:,} in use"),
            _kpi("Overdue for rotation", f"{overdue:,}", "danger" if overdue else "success"),
            _kpi(f"Due within {due_days} days", f"{due_soon:,}", "warning" if due_soon else "success"),
            _kpi("Rotation not tracked", f"{untracked:,}", "warning" if untracked else "success",
                 "no policy or no rotation date"),
            _kpi("Cleartext or legacy SSH", f"{weak:,}", "warning" if weak else "success",
                 "profiles"),
            _kpi("Devices with credential problems", f"{len(devices):,} of {enrolled:,}",
                 "danger" if devices else "success", "enabled enrollments"),
        ],
        tables=tables, notes=notes)


# ─── Templates ──────────────────────────────────────────────────────────────

template(
    "ncm_backup_status_report", "Configuration Backup Status",
    "Which devices have a fresh, validated configuration backup, which do not and why: coverage, "
    "devices needing action, daily backup outcomes, backup-failure incidents with mean time to "
    "recovery, and coverage by vendor and site.",
    "configuration",
    ["ncm_protection", "ncm_at_risk", "ncm_outcomes", "ncm_backup_incidents", "ncm_breakdown"],
    audience=("Network engineer", "NOC operator", "IT manager"),
    tags=("ncm", "backup", "coverage", "incidents"), default_period="last_7d", frequency="weekly",
    icon="HardDrive")

template(
    "ncm_change_audit_report", "Configuration Change Audit",
    "Who changed what and when: configuration-change notifications from syslog, SNMP traps and "
    "change management with the (unverified) reported user, changes confirmed by validated backups, "
    "the most changed devices and drift from approved baselines. Never includes configuration text.",
    "configuration",
    ["ncm_change_audit", "ncm_changes"],
    audience=("Network engineer", "Security & audit"),
    tags=("ncm", "changes", "audit", "change management"), default_period="last_7d",
    frequency="weekly", icon="FileText")

template(
    "ncm_compliance_trend_report", "Compliance Trend",
    "Configuration-compliance pass rate and check outcomes day by day, per-policy movement over the "
    "period, and the policies and rules failing most often today.",
    "configuration",
    ["ncm_compliance_trend", "ncm_compliance"],
    audience=("Network engineer", "Security & audit", "IT manager"),
    tags=("ncm", "compliance", "trend"), default_period="last_30d", frequency="monthly",
    icon="TrendingUp")

template(
    "ncm_compliance_exceptions_report", "Compliance Exceptions",
    "Every active compliance exception with its owner, reason and expiry — those expiring in the "
    "next 30 days and those that lapsed in the last 30 days first — alongside the current policy "
    "results they affect.",
    "security",
    [S("ncm_compliance_exceptions", days=30, top_n=200), "ncm_compliance"],
    audience=("Security & audit", "IT manager"),
    tags=("ncm", "compliance", "exceptions", "risk acceptance"), default_period="last_30d",
    frequency="monthly", icon="ShieldCheck")

template(
    "ncm_restore_readiness_report", "Restore Readiness",
    "Evidence that configurations can be recovered: the latest restore test per device and its age, "
    "devices never tested, changes since the tested version, and current backup protection.",
    "configuration",
    ["ncm_restore_readiness", "ncm_protection", "ncm_at_risk"],
    audience=("IT manager", "Network engineer", "Security & audit"),
    tags=("ncm", "restore", "recovery", "disaster recovery"), default_period="last_30d",
    frequency="monthly", icon="FileCheck2")

template(
    "firmware_os_versions_report", "Firmware and OS Versions",
    "Software versions by vendor and model with the devices on older or minority versions, plus "
    "chassis firmware revisions — the starting point for upgrade planning. End-of-life status is "
    "not included (no lifecycle data source).",
    "inventory",
    [S("firmware_inventory", None, version="os"),
     S("firmware_inventory", "Chassis Firmware Revisions", version="firmware")],
    audience=("Network engineer", "IT manager"),
    tags=("inventory", "firmware", "os version", "lifecycle"), default_period="last_30d",
    frequency="monthly", icon="Layers")

template(
    "hardware_serial_report", "Hardware Serial Number Inventory",
    "Chassis, module, power-supply and fan serial numbers, models and hardware/firmware revisions, "
    "with every device's software version, for asset, warranty and support-contract records.",
    "inventory",
    [S("hardware_serial_inventory", None, components="fru"),
     S("firmware_inventory", "Software Versions by Device", list="all", top_n=1000)],
    audience=("IT manager", "Network engineer"),
    tags=("inventory", "serial numbers", "assets", "warranty"), default_period="last_30d",
    frequency="monthly", icon="Boxes")

template(
    "ncm_credential_hygiene_report", "NCM Credential Hygiene",
    "Connection profiles overdue for secret rotation or using cleartext or legacy SSH settings, and "
    "enrolled devices whose backups fail on credentials. Never includes secrets or usernames.",
    "security",
    ["ncm_credential_hygiene"],
    audience=("Security & audit", "Network engineer"),
    tags=("ncm", "credentials", "rotation", "ssh"), default_period="last_30d", frequency="monthly",
    icon="ShieldCheck")

template(
    "monthly_config_compliance_review", "Monthly Configuration Compliance Review",
    "Monthly review of configuration governance: backup protection, compliance trend and current "
    "failures, active and lapsed exceptions, audited changes and baseline drift, backup-failure "
    "incidents, restore evidence and credential hygiene.",
    "configuration",
    ["ncm_protection", "ncm_compliance_trend", "ncm_compliance", "ncm_compliance_exceptions",
     "ncm_change_audit", "ncm_changes", "ncm_backup_incidents", "ncm_restore_readiness",
     "ncm_credential_hygiene"],
    audience=("IT manager", "Security & audit", "Network engineer"),
    tags=("ncm", "compliance", "monthly review", "audit"), default_period="previous_month",
    frequency="monthly", icon="FileCheck2", featured=True)
