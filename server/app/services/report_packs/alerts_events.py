"""Alert and event sections: response performance (MTTA / MTTR), the open
backlog, notification delivery, escalations, rule noise, alerts per device,
SNMP traps and syslog.

Populations follow the Alert Center (alert_service / alert_workflow):
incidents are alert rows that are not notices (recovery notices,
dependency-suppressed and hold-cancelled records, ``wf.notice_sql``); only a
``recovered`` closure is a genuine recovery, so operator and administrative
closures never shorten time to recover. Every aggregate runs in the database
(percentiles with ``percentile_cont``); only grouped rows reach Python.

NetFlow alert messages are withheld from a requester without netflow.view
(X-01) through ``netflow_alert_service.redact_alert_payload``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text

from app.services.report_sections import (
    SectionCtx,
    _fmt_ms,
    _section,
    register_section,
    top_n_option,
)
from app.services.report_templates import S, template

SEVERITIES = ("critical", "warning", "info")
OPEN_SQL = "('pending', 'active', 'acknowledged')"
SEVERITY_OPTION = {
    "key": "severity", "label": "Severities", "type": "select", "default": "all",
    "choices": [["all", "All severities"], ["critical", "Critical only"],
                ["critical_warning", "Critical and warning"]],
}
SEVERITY_SETS = {"all": None, "critical": ["critical"], "critical_warning": ["critical", "warning"]}
SEVERITY_TITLES = {"critical": "Critical", "critical_warning": "Critical and Warning"}

# The alert's entity, as the Alert Center names it.
SOURCE_SQL = ("COALESCE(sc.name, d.hostname, s.display_name, s.hostname, sen.name,"
              " a.metadata->>'monitor_name', a.metadata->>'slo_name', a.metadata->>'service', 'System')")
# Acknowledgement target (minutes) for the alert's severity; NULL = no target.
TARGET_SQL = ("CASE a.severity WHEN 'critical' THEN CAST(:t_critical AS integer)"
              " WHEN 'warning' THEN CAST(:t_warning AS integer)"
              " WHEN 'info' THEN CAST(:t_info AS integer) END")

ZERO_UUID = "00000000-0000-0000-0000-000000000000"
# A trap whose source address matched no managed device. Legacy rows may carry
# the zero UUID instead of NULL; it is also scoping.NO_MATCH_ID, so a device
# filter must never match it.
TRAP_UNKNOWN_SQL = f"(ifNull(device_id, toUUID('{ZERO_UUID}')) = toUUID('{ZERO_UUID}'))"
TRAP_KNOWN_SQL = f"(ifNull(device_id, toUUID('{ZERO_UUID}')) != toUUID('{ZERO_UUID}'))"

SYSLOG_SEVERITY = ("Emergency", "Alert", "Critical", "Error", "Warning", "Notice",
                   "Informational", "Debug")
SYSLOG_FACILITY = {
    0: "kern", 1: "user", 2: "mail", 3: "daemon", 4: "auth", 5: "syslog", 6: "lpr", 7: "news",
    8: "uucp", 9: "cron", 10: "authpriv", 11: "ftp", 12: "ntp", 13: "log audit", 14: "log alert",
    15: "clock", **{16 + i: f"local{i}" for i in range(8)},
}
SYSLOG_LEVELS = {"critical": 2, "error": 3, "warning": 4}

DELIVERY_REASONS = {
    "timeout": "Timed out",
    "rejected": "Rejected by the provider (HTTP error)",
    "auth": "Authentication failed",
    "recipient": "Recipient refused",
    "connection": "Connection failed",
    "config": "Channel configuration",
    "error": "Other transport error",
    "other": "Unclassified",
}


# ─── formatting helpers ─────────────────────────────────────────────────────

def _dur(secs: Optional[float]) -> str:
    """Human duration ('—' when not measured)."""
    if secs is None:
        return "—"
    s = max(float(secs), 0.0)
    if s < 60:
        return f"{s:.0f} s"
    if s < 600:
        return f"{s / 60:.1f} min"
    if s < 3600:
        return f"{s / 60:.0f} min"
    if s < 86400:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} d"


def _num(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _flt(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _rate(n: int, d: int) -> str:
    return f"{n / d * 100:.2f}%" if d else "—"


def _dur_delta(cur: Optional[float], prev: Optional[float]) -> Optional[str]:
    if cur is None or prev is None:
        return None
    diff = cur - prev
    if abs(diff) < 1:
        return "No change vs previous period"
    return f"{'▲' if diff > 0 else '▼'} {_dur(abs(diff))} vs previous period"


def _count_delta(cur: int, prev: Optional[int]) -> Optional[str]:
    if prev is None:
        return None
    diff = cur - prev
    if diff == 0:
        return "No change vs previous period"
    pct = f" ({diff / prev * 100:+.1f}%)" if prev else ""
    return f"{'▲' if diff > 0 else '▼'} {abs(diff):,}{pct} vs previous period"


def _clip(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _top_title(base: str, shown: int, total: int) -> str:
    return f"{base} (top {shown} of {total})" if total > shown else base


def _sev_clause(ctx: SectionCtx, params: dict) -> tuple[str, Optional[list[str]]]:
    sev = SEVERITY_SETS.get(ctx.opt("severity") or "all")
    if not sev:
        return "", None
    params["sev"] = sev
    return " AND a.severity = ANY(CAST(:sev AS text[]))", sev


def _target_params(targets: dict) -> dict:
    return {f"t_{s}": (int(targets[s]) if targets.get(s) else None) for s in SEVERITIES}


def _targets_note(targets: dict) -> str:
    parts = [f"{s} {targets[s]} min" if targets.get(s) else f"{s} none" for s in SEVERITIES]
    return "Acknowledgement targets (Alert Center settings): " + ", ".join(parts) + "."


async def _workflow(ctx: SectionCtx) -> bool:
    """Whether migrate-135 (ownership, lifecycle events, delivery log) has run."""
    from app.services import alert_workflow as wf
    return await wf.supported(ctx.db)


async def _redactor(ctx: SectionCtx):
    """Message redaction for alert rows: NetFlow alert text is withheld from a
    requester without netflow.view (X-01)."""
    from app.services.netflow_alert_service import redact_alert_payload
    redact = await ctx.withholds_netflow_alerts()

    def message(msg: Any, source_kind: Any) -> str:
        payload = {"message": msg or "", "metadata": {"source": source_kind}}
        if redact:
            payload = redact_alert_payload(payload)
        return payload["message"] or ""
    return message


# ─── time buckets in the organisation timezone ──────────────────────────────

def _bucket_unit(ctx: SectionCtx) -> str:
    return "hour" if ctx.hours <= 48 else "day"


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _bucket_starts(ctx: SectionCtx, unit: str) -> list[int]:
    """Epoch seconds of every local hour/day bucket start in the window."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(ctx.tz_name)
    except Exception:
        tz = timezone.utc
    end = _aware(ctx.to)
    cur = _aware(ctx.frm).astimezone(tz)
    cur = cur.replace(minute=0, second=0, microsecond=0)
    if unit == "day":
        cur = cur.replace(hour=0)
    out: list[int] = []
    while cur <= end and len(out) < 2400:
        out.append(int(cur.timestamp()))
        if unit == "day":
            cur = (cur.replace(tzinfo=None) + timedelta(days=1)).replace(tzinfo=tz)
        else:
            cur = (cur.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(tz)
    return out


def _pg_bucket(column: str) -> str:
    """Epoch of the local bucket start (params :unit, :org_tz)."""
    return (f"EXTRACT(EPOCH FROM (date_trunc(:unit, {column} AT TIME ZONE :org_tz)"
            f" AT TIME ZONE :org_tz))")


def _time_series(ctx: SectionCtx, unit: str, counts: dict[str, dict[int, float]],
                 names: Iterable[tuple[str, str]], unit_label: str, color: str) -> dict:
    """A stacked series (one per name) plus total points; empty buckets are 0
    (no events were recorded), not gaps."""
    starts = _bucket_starts(ctx, unit)
    keys = set(starts)
    lo, hi = (starts[0], int(_aware(ctx.to).timestamp())) if starts else (None, None)
    for per in counts.values():
        # A bucket the database labelled differently (e.g. across a DST change)
        # is kept; anything outside the window is not.
        keys.update(k for k in per if lo is None or lo <= k <= hi)
    keys_sorted = sorted(keys)

    def iso(e: int) -> Optional[str]:
        return ctx.iso(datetime.fromtimestamp(e, timezone.utc))

    series = [{"name": label, "points": [{"t": iso(k), "v": counts.get(key, {}).get(k, 0)}
                                         for k in keys_sorted]}
              for key, label in names]
    totals = [{"t": iso(k), "v": sum(counts.get(key, {}).get(k, 0) for key, _l in names)}
              for k in keys_sorted]
    return {"kind": "stacked", "unit": unit_label, "color": color, "points": totals,
            "series": series}


# ─── Alert response performance ─────────────────────────────────────────────

async def _response_stats(ctx: SectionCtx, frm: datetime, to: datetime, targets: dict,
                          by_severity: bool) -> list[dict]:
    """Opened/closed counts, time to acknowledge/recover/close (p50/p95) and
    acknowledgement-target outcomes; one row per severity plus a total row
    (``is_total``)."""
    from app.services import alert_workflow as wf
    params: dict[str, Any] = {"f": frm, "t": to, **_target_params(targets)}
    sev_sql, _sev = _sev_clause(ctx, params)
    w = "BETWEEN :f AND :t"
    grouping = "GROUPING SETS ((x.severity), ())" if by_severity else "()"
    sev_col = "x.severity, GROUPING(x.severity) AS is_total" if by_severity else \
        "NULL AS severity, 1 AS is_total"
    p = "percentile_cont({q}) WITHIN GROUP (ORDER BY x.{col}) FILTER (WHERE {cond})"
    acked = f"x.acknowledged_at {w} AND x.tta >= 0"
    closed = f"x.status = 'resolved' AND x.resolved_at {w} AND x.ttc >= 0"
    recovered = f"{closed} AND x.reason = 'recovered'"
    # The population is the incidents opened in the period (triggered_at); the
    # deadline runs from when the alert was sent (ack_at: metadata.notified_at
    # after a hold), like the Alert Center's ack_due. tta/ttc stay on
    # triggered_at: an acknowledgement during a hold would otherwise be negative.
    judged = (f"x.triggered_at {w} AND x.status <> 'pending' AND x.target_min IS NOT NULL"
              " AND (x.acknowledged_at IS NOT NULL OR x.resolved_at IS NOT NULL"
              " OR NOW() > x.ack_at + make_interval(mins => x.target_min))")
    sql = f"""
        WITH x AS (
            SELECT a.severity, a.status, a.triggered_at, a.acknowledged_at, a.resolved_at,
                   {wf.ack_anchor_sql('a')} AS ack_at,
                   {wf.closure_reason_sql('a')} AS reason,
                   EXTRACT(EPOCH FROM (a.acknowledged_at - a.triggered_at)) AS tta,
                   EXTRACT(EPOCH FROM (a.resolved_at - a.triggered_at)) AS ttc,
                   {TARGET_SQL} AS target_min
            FROM alerts a
            WHERE NOT {wf.notice_sql('a')}{sev_sql}
              AND (a.triggered_at {w} OR a.resolved_at {w} OR a.acknowledged_at {w})
        )
        SELECT {sev_col},
               count(*) FILTER (WHERE x.triggered_at {w}) AS opened,
               count(*) FILTER (WHERE x.status = 'resolved' AND x.resolved_at {w}) AS closed,
               count(*) FILTER (WHERE {acked}) AS acked,
               {p.format(q=0.5, col='tta', cond=acked)} AS tta_p50,
               {p.format(q=0.95, col='tta', cond=acked)} AS tta_p95,
               count(*) FILTER (WHERE {recovered}) AS recovered,
               {p.format(q=0.5, col='ttc', cond=recovered)} AS ttr_p50,
               {p.format(q=0.95, col='ttc', cond=recovered)} AS ttr_p95,
               {p.format(q=0.5, col='ttc', cond=closed)} AS ttc_p50,
               {p.format(q=0.95, col='ttc', cond=closed)} AS ttc_p95,
               count(*) FILTER (WHERE {judged}) AS judged,
               count(*) FILTER (WHERE {judged} AND COALESCE(x.acknowledged_at, x.resolved_at, NOW())
                                > x.ack_at + make_interval(mins => x.target_min)) AS breached,
               count(*) FILTER (WHERE x.tta < 0 OR (x.status = 'resolved' AND x.resolved_at {w}
                                AND x.ttc < 0)) AS anomalies,
               count(*) FILTER (WHERE x.status = 'resolved' AND x.resolved_at {w}
                                AND x.reason = 'manual') AS r_manual,
               count(*) FILTER (WHERE x.status = 'resolved' AND x.resolved_at {w}
                                AND x.reason = 'recovered') AS r_recovered,
               count(*) FILTER (WHERE x.status = 'resolved' AND x.resolved_at {w}
                                AND x.reason = 'suppressed') AS r_suppressed,
               count(*) FILTER (WHERE x.status = 'resolved' AND x.resolved_at {w}
                                AND x.reason = 'superseded') AS r_superseded,
               count(*) FILTER (WHERE x.status = 'resolved' AND x.resolved_at {w}
                                AND x.reason = 'administrative') AS r_administrative
        FROM x GROUP BY {grouping}
    """
    return [dict(r) for r in (await ctx.db.execute(text(sql), params)).mappings().all()]


@register_section(
    "alert_response_metrics", title="Alert Response Performance", category="Alerting",
    description="Time to acknowledge, recover and close alert incidents (median and 95th "
                "percentile), acknowledgement targets met, and how incidents were closed.",
    scope="fleet", requires=("alerts.view",), options=[SEVERITY_OPTION],
)
async def alert_response_metrics(ctx: SectionCtx) -> dict:
    from app.services import alert_workflow as wf

    sid = "alert_response_metrics"
    sev_key = ctx.opt("severity") or "all"
    title = "Alert Response Performance" + (
        f" — {SEVERITY_TITLES[sev_key]}" if sev_key in SEVERITY_TITLES else "")
    targets = await wf.response_targets(ctx.db)
    workflow = await _workflow(ctx)

    rows = await _response_stats(ctx, ctx.frm, ctx.to, targets, by_severity=True)
    total = next((r for r in rows if _num(r.get("is_total"))), None) or {}
    pf, pt = ctx.prior_window()
    prior_rows = await _response_stats(ctx, pf, pt, targets, by_severity=False)
    prior = prior_rows[0] if prior_rows else None

    # Open backlog right now (acknowledgement overdue, unowned criticals).
    params: dict[str, Any] = _target_params(targets)
    sev_sql, _ = _sev_clause(ctx, params)
    owner = "a.assigned_to IS NULL" if workflow else "NULL::boolean"
    snap = (await ctx.db.execute(text(f"""
        SELECT count(*) FILTER (WHERE x.status = 'active' AND NOT x.sup AND x.target_min IS NOT NULL
                                AND x.ack_at < NOW() - make_interval(mins => x.target_min))
                   AS ack_overdue,
               count(*) FILTER (WHERE NOT x.sup AND x.severity = 'critical' AND x.unowned)
                   AS unassigned_critical
        FROM (SELECT a.status, a.severity, a.triggered_at, {wf.suppressed_sql('a')} AS sup,
                     {wf.ack_anchor_sql('a')} AS ack_at,
                     {owner} AS unowned, {TARGET_SQL} AS target_min
              FROM alerts a
              WHERE a.status IN ('active', 'acknowledged') AND NOT {wf.notice_sql('a')}{sev_sql}) x
    """), params)).mappings().first() or {}

    opened, closed, acked = _num(total.get("opened")), _num(total.get("closed")), _num(total.get("acked"))
    if not (opened or closed or acked):
        notes = ["No alert incidents were opened, acknowledged or closed in this period."]
    else:
        notes = []

    def kpi_dur(label: str, p50: str, p95: str, count_key: str, noun: str) -> dict:
        cur = _flt(total.get(p50))
        sub = f"p95 {_dur(_flt(total.get(p95)))} · {_num(total.get(count_key)):,} {noun}"
        delta = _dur_delta(cur, _flt(prior.get(p50)) if prior else None)
        return {"label": label, "value": _dur(cur), "accent": "info",
                "subtitle": f"{sub} · {delta}" if delta else sub}

    judged, breached = _num(total.get("judged")), _num(total.get("breached"))
    met_pct = (judged - breached) / judged * 100 if judged else None
    overdue = _num(snap.get("ack_overdue"))
    unowned = snap.get("unassigned_critical")
    kpi_opened = {"label": "Incidents opened", "value": f"{opened:,}", "accent": "primary"}
    opened_delta = _count_delta(opened, _num(prior.get("opened")) if prior else None)
    if opened_delta:
        kpi_opened["subtitle"] = opened_delta
    kpis = [
        kpi_opened,
        kpi_dur("Time to acknowledge (median)", "tta_p50", "tta_p95", "acked", "acknowledged"),
        kpi_dur("Time to recover (median)", "ttr_p50", "ttr_p95", "recovered", "recovered"),
        {"label": "Acknowledged within target",
         "value": "—" if met_pct is None else f"{met_pct:.2f}%",
         "accent": "neutral" if met_pct is None else
                   "success" if met_pct >= 95 else "warning" if met_pct >= 80 else "danger",
         "subtitle": f"{breached:,} of {judged:,} breached the target" if judged
                     else "No incident had a target to meet"},
        {"label": "Acknowledgement overdue now", "value": f"{overdue:,}",
         "accent": "danger" if overdue else "success",
         "subtitle": "Open and unacknowledged past target, as of report generation"},
        {"label": "Unassigned critical now",
         "value": "—" if unowned is None or not workflow else f"{_num(unowned):,}",
         "accent": "neutral" if unowned is None or not workflow else
                   "danger" if _num(unowned) else "success",
         "subtitle": "Open critical incidents with no owner" if workflow
                     else "Ownership is recorded from migration 135"},
    ]

    reasons = [(k, _num(total.get(f"r_{k}"))) for k in
               ("recovered", "manual", "superseded", "suppressed", "administrative")]
    charts = []
    if any(n for _k, n in reasons):
        charts.append({"title": "How incidents were closed",
                       "series": {"kind": "donut", "unit": "incidents",
                                  "points": [{"label": wf.CLOSURE_LABELS.get(k, k), "value": n}
                                             for k, n in reasons if n]}})

    by_sev = {str(r.get("severity")): r for r in rows if not _num(r.get("is_total"))}
    order = [s for s in SEVERITIES if s in by_sev] + sorted(s for s in by_sev if s not in SEVERITIES)
    table_rows = []
    for s in order:
        r = by_sev[s]
        j, b = _num(r.get("judged")), _num(r.get("breached"))
        table_rows.append([
            s, f"{_num(r.get('opened')):,}", f"{_num(r.get('closed')):,}",
            f"{_dur(_flt(r.get('tta_p50')))} / {_dur(_flt(r.get('tta_p95')))}",
            f"{_dur(_flt(r.get('ttr_p50')))} / {_dur(_flt(r.get('ttr_p95')))}",
            f"{_dur(_flt(r.get('ttc_p50')))} / {_dur(_flt(r.get('ttc_p95')))}",
            _rate(j - b, j),
        ])
    tables = [{"title": "Response times by severity (median / 95th percentile)",
               "headers": ["Severity", "Opened", "Closed", "Time to acknowledge",
                           "Time to recover", "Time to close", "Within ack target"],
               "styles": ["severity", "num", "num", "num", "num", "num", "pct"],
               "rows": table_rows}] if table_rows else []

    notes += [
        "Incidents only: recovery notices, dependency-suppressed and hold-cancelled records are "
        "excluded. Times cover incidents acknowledged or closed within the period.",
        "Time to recover counts incidents that cleared automatically; operator, snooze and "
        "administrative closures count toward time to close only.",
        _targets_note(targets),
    ]
    if _num(total.get("anomalies")):
        notes.append(f"{_num(total['anomalies']):,} incident(s) with an acknowledgement or closure "
                     "time earlier than their trigger time (clock skew) were left out of the times.")
    return _section(
        sid, title,
        description="How quickly alert incidents were acknowledged, recovered and closed over "
                    "the period, against the configured acknowledgement targets.",
        kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── Open alert backlog ─────────────────────────────────────────────────────

AGE_BUCKETS = (("lt_1h", "Under 1 hour"), ("h1_24", "1–24 hours"), ("d1_7", "1–7 days"),
               ("gt_7d", "Over 7 days"))


@register_section(
    "alert_backlog_aging", title="Open Alert Backlog", category="Alerting",
    description="Open alert incidents by age and severity, and the oldest incidents still "
                "open, as of report generation.",
    scope="fleet", requires=("alerts.view",), semantics="snapshot",
    options=[top_n_option(15, 100), SEVERITY_OPTION],
)
async def alert_backlog_aging(ctx: SectionCtx) -> dict:
    from app.services import alert_service as svc
    from app.services import alert_workflow as wf

    sid, title = "alert_backlog_aging", "Open Alert Backlog"
    targets = await wf.response_targets(ctx.db)
    workflow = await _workflow(ctx)
    params: dict[str, Any] = _target_params(targets)
    sev_sql, _ = _sev_clause(ctx, params)
    owner = "a.assigned_to IS NULL" if workflow else "FALSE"
    agg = (await ctx.db.execute(text(f"""
        SELECT x.severity, x.bucket,
               count(*) FILTER (WHERE x.status IN ('active', 'acknowledged') AND NOT x.sup) AS open,
               count(*) FILTER (WHERE x.status = 'active' AND NOT x.sup) AS unacked,
               count(*) FILTER (WHERE x.status IN ('active', 'acknowledged') AND x.sup) AS suppressed,
               count(*) FILTER (WHERE x.status = 'pending') AS pending,
               count(*) FILTER (WHERE x.status IN ('active', 'acknowledged') AND NOT x.sup
                                AND x.unowned) AS unassigned,
               count(*) FILTER (WHERE x.status = 'active' AND NOT x.sup AND x.target_min IS NOT NULL
                                AND x.ack_at < NOW() - make_interval(mins => x.target_min))
                   AS ack_overdue
        FROM (SELECT a.severity, a.status, a.triggered_at, {wf.suppressed_sql('a')} AS sup,
                     {wf.ack_anchor_sql('a')} AS ack_at,
                     {owner} AS unowned, {TARGET_SQL} AS target_min,
                     CASE WHEN a.triggered_at >= NOW() - interval '1 hour' THEN 'lt_1h'
                          WHEN a.triggered_at >= NOW() - interval '24 hours' THEN 'h1_24'
                          WHEN a.triggered_at >= NOW() - interval '7 days' THEN 'd1_7'
                          ELSE 'gt_7d' END AS bucket
              FROM alerts a
              WHERE a.status IN {OPEN_SQL} AND NOT {wf.notice_sql('a')}{sev_sql}) x
        GROUP BY x.severity, x.bucket
    """), params)).mappings().all()

    matrix: dict[str, dict[str, int]] = {}
    tot = {"open": 0, "unacked": 0, "suppressed": 0, "pending": 0, "unassigned": 0,
           "ack_overdue": 0}
    crit = 0
    for r in agg:
        sev = str(r.get("severity") or "info")
        if _num(r.get("open")):
            matrix.setdefault(sev, {})[str(r.get("bucket"))] = _num(r.get("open"))
        for k in tot:
            tot[k] += _num(r.get(k))
        if sev == "critical":
            crit += _num(r.get("open"))
    by_bucket = {b: sum(m.get(b, 0) for m in matrix.values()) for b, _l in AGE_BUCKETS}

    if not tot["open"]:
        notes = ["No open alert incidents when the report was generated."]
        if tot["suppressed"]:
            notes.append(f"{tot['suppressed']:,} open incident(s) are suppressed by a snooze.")
        if tot["pending"]:
            notes.append(f"{tot['pending']:,} alert(s) are pending confirmation.")
        return _section(sid, title, description="Open alert incidents as of report generation.",
                        notes=notes)

    # Oldest open incidents (active or acknowledged, not suppressed).
    top_n = int(ctx.opt("top_n"))
    owner_cols = ", COALESCE(u.full_name, u.username) AS owner" if workflow else ", NULL AS owner"
    owner_join = " LEFT JOIN users u ON u.id = a.assigned_to" if workflow else ""
    lparams: dict[str, Any] = {"n": top_n}
    sev_sql, _ = _sev_clause(ctx, lparams)
    oldest = (await ctx.db.execute(text(
        f"SELECT a.severity, a.message, a.metadata->>'source' AS source_kind, {SOURCE_SQL} AS source,"
        f" a.status, a.triggered_at, EXTRACT(EPOCH FROM (NOW() - a.triggered_at)) AS age_s{owner_cols}"
        f" {svc.base_from(workflow)}{owner_join}"
        f" WHERE a.status IN ('active', 'acknowledged') AND NOT {wf.notice_sql('a')}"
        f" AND NOT {wf.suppressed_sql('a')}{sev_sql}"
        " ORDER BY a.triggered_at ASC, a.id ASC LIMIT :n"), lparams)).mappings().all()
    message = await _redactor(ctx)

    kpis = [
        {"label": "Open incidents", "value": f"{tot['open']:,}", "accent": "primary",
         "subtitle": "Active or acknowledged, as of report generation"},
        {"label": "Critical", "value": f"{crit:,}", "accent": "danger" if crit else "success"},
        {"label": "Unacknowledged", "value": f"{tot['unacked']:,}",
         "accent": "warning" if tot["unacked"] else "success"},
        {"label": "Acknowledgement overdue", "value": f"{tot['ack_overdue']:,}",
         "accent": "danger" if tot["ack_overdue"] else "success", "subtitle": "Past target"},
        {"label": "Open longer than 7 days", "value": f"{by_bucket['gt_7d']:,}",
         "accent": "warning" if by_bucket["gt_7d"] else "neutral"},
        {"label": "Unassigned", "value": f"{tot['unassigned']:,}" if workflow else "—",
         "accent": "warning" if workflow and tot["unassigned"] else "neutral",
         "subtitle": "No owner" if workflow else "Ownership is recorded from migration 135"},
    ]
    order = [s for s in SEVERITIES if s in matrix] + sorted(s for s in matrix if s not in SEVERITIES)
    age_rows = [[s] + [f"{matrix[s].get(b, 0):,}" for b, _l in AGE_BUCKETS]
                + [f"{sum(matrix[s].values()):,}"] for s in order]
    old_rows = [[str(r.get("severity") or "info"), _clip(message(r.get("message"), r.get("source_kind")), 120),
                 str(r.get("source") or "—"), str(r.get("status") or "unknown"),
                 ctx.fmt_short(r.get("triggered_at")), _dur(_flt(r.get("age_s"))),
                 str(r.get("owner") or ("Unassigned" if workflow else "—"))]
                for r in oldest]
    notes = []
    if tot["suppressed"]:
        notes.append(f"{tot['suppressed']:,} further open incident(s) are suppressed by a snooze "
                     "and are not counted above.")
    if tot["pending"]:
        notes.append(f"{tot['pending']:,} alert(s) are pending confirmation and not yet incidents.")
    notes.append(_targets_note(targets))
    return _section(
        sid, title,
        description=f"{tot['open']:,} open alert incident(s) by age and severity, as of report "
                    "generation.",
        kpis=kpis,
        charts=[{"title": "Open incidents by age",
                 "series": {"kind": "rank", "unit": "incidents", "color": "warning",
                            "points": [{"label": label, "value": by_bucket[b]}
                                       for b, label in AGE_BUCKETS]}}],
        tables=[
            {"title": "Open incidents by age and severity",
             "headers": ["Severity"] + [label for _b, label in AGE_BUCKETS] + ["Total"],
             "styles": ["severity", "num", "num", "num", "num", "num"], "rows": age_rows},
            {"title": _top_title("Oldest open incidents", len(old_rows), tot["open"]),
             "headers": ["Severity", "Alert", "Source", "Status", "Opened", "Open for", "Owner"],
             "styles": ["severity", "text", "text", "status", "text", "num", "text"],
             "rows": old_rows},
        ],
        notes=notes)


# ─── Notification delivery ──────────────────────────────────────────────────

@register_section(
    "alert_notification_delivery", title="Notification Delivery", category="Alerting",
    description="Alert notifications accepted, failed and skipped per channel, delivery "
                "latency and the most common failure reasons.",
    scope="fleet", requires=("alerts.view",), options=[top_n_option(15, 100)],
)
async def alert_notification_delivery(ctx: SectionCtx) -> dict:
    sid, title = "alert_notification_delivery", "Notification Delivery"
    if not await _workflow(ctx):
        return _section(sid, title, notes=[
            "Notification delivery receipts are recorded from migration 135 onwards; none are "
            "available on this appliance yet."])
    params: dict[str, Any] = {"f": ctx.frm, "t": ctx.to}
    win = "dl.attempted_at BETWEEN :f AND :t"
    rows = (await ctx.db.execute(text(f"""
        SELECT x.channel, x.ctype, GROUPING(x.channel) AS is_total,
               count(*) AS attempts,
               count(*) FILTER (WHERE x.outcome = 'accepted') AS accepted,
               count(*) FILTER (WHERE x.outcome = 'failed') AS failed,
               count(*) FILTER (WHERE x.outcome = 'skipped') AS skipped,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY x.latency_ms)
                   FILTER (WHERE x.outcome = 'accepted' AND x.latency_ms IS NOT NULL) AS p50_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY x.latency_ms)
                   FILTER (WHERE x.outcome = 'accepted' AND x.latency_ms IS NOT NULL) AS p95_ms,
               max(x.attempted_at) FILTER (WHERE x.outcome = 'failed') AS last_failed,
               count(DISTINCT x.alert_id) FILTER (WHERE x.outcome = 'failed') AS alerts_failed
        FROM (SELECT COALESCE(NULLIF(dl.channel_name, ''), '(unnamed channel)') AS channel,
                     COALESCE(NULLIF(dl.channel_type, ''), '—') AS ctype,
                     dl.outcome, dl.latency_ms, dl.attempted_at, dl.alert_id
              FROM alert_deliveries dl WHERE {win}) x
        GROUP BY GROUPING SETS ((x.channel, x.ctype), ())
    """), params)).mappings().all()
    total = next((r for r in rows if _num(r.get("is_total"))), None)
    channels = [r for r in rows if not _num(r.get("is_total"))]
    if not total or not _num(total.get("attempts")):
        return _section(sid, title, description="Alert notification attempts per channel.",
                        notes=["No alert notifications were attempted in this period."])

    unit = _bucket_unit(ctx)
    trend = (await ctx.db.execute(text(
        f"SELECT {_pg_bucket('dl.attempted_at')} AS b, dl.outcome, count(*) AS n"
        f" FROM alert_deliveries dl WHERE {win} GROUP BY 1, 2"),
        {**params, "unit": unit, "org_tz": ctx.tz_name})).mappings().all()
    reasons = (await ctx.db.execute(text(f"""
        SELECT CASE WHEN lower(trim(split_part(dl.error, ':', 1))) IN
                         ('timeout', 'rejected', 'auth', 'recipient', 'connection', 'config', 'error')
                    THEN lower(trim(split_part(dl.error, ':', 1))) ELSE 'other' END AS category,
               count(*) AS n,
               count(DISTINCT COALESCE(dl.channel_name, '')) AS channels,
               (array_agg(dl.error ORDER BY dl.attempted_at DESC))[1] AS sample,
               max(dl.attempted_at) AS last_at
        FROM alert_deliveries dl WHERE dl.outcome = 'failed' AND {win}
        GROUP BY 1 ORDER BY n DESC LIMIT 10
    """), params)).mappings().all()

    attempts, accepted = _num(total.get("attempts")), _num(total.get("accepted"))
    failed, skipped = _num(total.get("failed")), _num(total.get("skipped"))
    sent = accepted + failed
    rate = accepted / sent * 100 if sent else None
    kpis = [
        {"label": "Notification attempts", "value": f"{attempts:,}", "accent": "primary",
         "subtitle": f"{len(channels):,} channel(s)"},
        {"label": "Delivered", "value": "—" if rate is None else f"{rate:.2f}%",
         "accent": "neutral" if rate is None else
                   "success" if rate >= 99 else "warning" if rate >= 95 else "danger",
         "subtitle": f"{accepted:,} accepted of {sent:,} sent"},
        {"label": "Failed", "value": f"{failed:,}", "accent": "danger" if failed else "success",
         "subtitle": f"{_num(total.get('alerts_failed')):,} alert(s) affected"},
        {"label": "Skipped", "value": f"{skipped:,}", "accent": "warning" if skipped else "neutral",
         "subtitle": "Channel disabled, no recipients or no gateway"},
        {"label": "Delivery latency (p95)", "value": _fmt_ms(_flt(total.get("p95_ms"))),
         "accent": "info", "subtitle": f"Median {_fmt_ms(_flt(total.get('p50_ms')))}"},
    ]

    counts: dict[str, dict[int, float]] = {}
    for r in trend:
        if r.get("b") is None:
            continue
        counts.setdefault(str(r.get("outcome")), {})[int(float(r["b"]))] = _num(r.get("n"))
    charts = [{"title": "Notification outcomes over time",
               "series": _time_series(ctx, unit, counts,
                                      [("accepted", "Accepted"), ("failed", "Failed"),
                                       ("skipped", "Skipped")], "notifications", "primary")}]

    channels.sort(key=lambda r: (-_num(r.get("failed")), -_num(r.get("attempts")), str(r.get("channel"))))
    top_n = int(ctx.opt("top_n"))
    ch_rows = []
    for r in channels[:top_n]:
        a, f = _num(r.get("accepted")), _num(r.get("failed"))
        ch_rows.append([str(r.get("channel")), str(r.get("ctype")), f"{_num(r.get('attempts')):,}",
                        f"{a:,}", f"{f:,}", f"{_num(r.get('skipped')):,}", _rate(a, a + f),
                        _fmt_ms(_flt(r.get("p95_ms"))), ctx.fmt_short(r.get("last_failed"))])
    tables = [{"title": _top_title("Delivery by channel", len(ch_rows), len(channels)),
               "headers": ["Channel", "Type", "Attempts", "Accepted", "Failed", "Skipped",
                           "Delivered", "Latency p95", "Last failure"],
               "styles": ["text", "text", "num", "num", "num", "num", "pct", "num", "text"],
               "rows": ch_rows}]
    if reasons:
        tables.append({"title": "Failure reasons",
                       "headers": ["Reason", "Failures", "Channels", "Latest error", "Last seen"],
                       "styles": ["text", "num", "num", "text", "text"],
                       "rows": [[DELIVERY_REASONS.get(str(r.get("category")), "Unclassified"),
                                 f"{_num(r.get('n')):,}", f"{_num(r.get('channels')):,}",
                                 _clip(r.get("sample"), 120) or "—", ctx.fmt_short(r.get("last_at"))]
                                for r in reasons]})
    return _section(
        sid, title,
        description="Alert notifications attempted per channel over the period: accepted, failed "
                    "and skipped, with delivery latency.",
        kpis=kpis, charts=charts, tables=tables,
        notes=["Delivered = accepted ÷ (accepted + failed); skipped attempts sent nothing and are "
               "reported separately. Latency is measured on accepted notifications.",
               "Channels are listed by the name they had when the notification was sent."])


# ─── Escalations ────────────────────────────────────────────────────────────

ESCALATED_CTE = """
    e AS (
        SELECT ev.alert_id,
               max(CASE WHEN (ev.detail->>'level') ~ '^[0-9]+$' THEN (ev.detail->>'level')::int END)
                   AS max_level,
               count(*) FILTER (WHERE COALESCE(ev.detail->>'repeat', 'false') <> 'true') AS escalations,
               count(*) FILTER (WHERE COALESCE(ev.detail->>'repeat', 'false') = 'true') AS repeats,
               min(ev.at) AS first_at
        FROM alert_events ev
        WHERE ev.kind = 'escalated' AND ev.at BETWEEN :f AND :t
        GROUP BY ev.alert_id
    )"""


@register_section(
    "alert_escalations", title="Alert Escalations", category="Alerting",
    description="Unacknowledged alerts escalated to further notification tiers, by level and "
                "rule, with the incidents that escalated furthest.",
    scope="fleet", requires=("alerts.view",), options=[top_n_option(10, 100)],
)
async def alert_escalations(ctx: SectionCtx) -> dict:
    from app.services import alert_service as svc
    from app.services import alert_workflow as wf

    sid, title = "alert_escalations", "Alert Escalations"
    if not await _workflow(ctx):
        return _section(sid, title, notes=[
            "Escalation history is recorded from migration 135 onwards; none is available on this "
            "appliance yet."])
    params: dict[str, Any] = {"f": ctx.frm, "t": ctx.to}
    levels = (await ctx.db.execute(text("""
        SELECT x.level, GROUPING(x.level) AS is_total,
               count(*) FILTER (WHERE NOT x.is_repeat) AS escalations,
               count(*) FILTER (WHERE x.is_repeat) AS repeats,
               count(DISTINCT x.alert_id) AS alerts
        FROM (SELECT COALESCE(NULLIF(ev.detail->>'level', ''), '?') AS level,
                     COALESCE(ev.detail->>'repeat', 'false') = 'true' AS is_repeat, ev.alert_id
              FROM alert_events ev
              WHERE ev.kind = 'escalated' AND ev.at BETWEEN :f AND :t) x
        GROUP BY GROUPING SETS ((x.level), ())
    """), params)).mappings().all()
    total = next((r for r in levels if _num(r.get("is_total"))), None)
    if not total or not (_num(total.get("escalations")) + _num(total.get("repeats"))):
        return _section(sid, title, description="Alert escalations over the period.",
                        notes=["No alerts were escalated in this period."])

    names = (await ctx.db.execute(text("""
        SELECT COALESCE(NULLIF(ev.detail->>'level', ''), '?') AS level, cn.name, count(*) AS n
        FROM alert_events ev
        CROSS JOIN LATERAL jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(ev.detail->'channel_names') = 'array'
                 THEN ev.detail->'channel_names' ELSE '[]'::jsonb END) AS cn(name)
        WHERE ev.kind = 'escalated' AND ev.at BETWEEN :f AND :t
        GROUP BY 1, 2 ORDER BY 1, n DESC
    """), params)).mappings().all()
    by_rule = (await ctx.db.execute(text(f"""
        WITH {ESCALATED_CTE}
        SELECT y.rid, GROUPING(y.rid) AS is_total, max(y.rule_name) AS rule_name,
               count(*) AS alerts, sum(y.escalations) AS escalations, sum(y.repeats) AS repeats,
               max(y.max_level) AS max_level,
               count(*) FILTER (WHERE y.acknowledged_at IS NOT NULL) AS acked,
               count(*) FILTER (WHERE y.status IN {OPEN_SQL}) AS still_open,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY y.ack_after_s)
                   FILTER (WHERE y.ack_after_s >= 0) AS ack_after_s
        FROM (SELECT {wf.effective_rule_sql('a')}::text AS rid,
                     COALESCE(r.name, a.metadata->'policy_snapshot'->>'name') AS rule_name,
                     e.escalations, e.repeats, e.max_level, a.acknowledged_at, a.status,
                     EXTRACT(EPOCH FROM (a.acknowledged_at - e.first_at)) AS ack_after_s
              FROM e JOIN alerts a ON a.id = e.alert_id
              LEFT JOIN alert_rules r ON r.id = {wf.effective_rule_sql('a')}) y
        GROUP BY GROUPING SETS ((y.rid), ())
    """), params)).mappings().all()
    rule_total = next((r for r in by_rule if _num(r.get("is_total"))), None) or {}
    rules = sorted((r for r in by_rule if not _num(r.get("is_total"))),
                   key=lambda r: (-_num(r.get("alerts")), -_num(r.get("escalations")),
                                  str(r.get("rule_name") or "")))
    top_n = int(ctx.opt("top_n"))
    worst = (await ctx.db.execute(text(f"""
        WITH {ESCALATED_CTE}
        SELECT a.severity, a.message, a.metadata->>'source' AS source_kind, {SOURCE_SQL} AS source,
               a.status, a.triggered_at, e.max_level, e.escalations + e.repeats AS pages,
               COALESCE(r.name, a.metadata->'policy_snapshot'->>'name') AS rule_name
        {svc.base_from(True)} JOIN e ON e.alert_id = a.id
        ORDER BY e.max_level DESC NULLS LAST, (e.escalations + e.repeats) DESC, a.triggered_at DESC
        LIMIT :n
    """), {**params, "n": top_n})).mappings().all()
    message = await _redactor(ctx)

    n_alerts = _num(total.get("alerts"))
    acked = _num(rule_total.get("acked"))
    max_level = _num(rule_total.get("max_level"))
    ack_after = _flt(rule_total.get("ack_after_s"))
    kpis = [
        {"label": "Escalations", "value": f"{_num(total.get('escalations')):,}", "accent": "warning",
         "subtitle": "Raised to a further notification tier"},
        {"label": "Repeat pages", "value": f"{_num(total.get('repeats')):,}", "accent": "neutral",
         "subtitle": "Re-sent at the same tier"},
        {"label": "Incidents escalated", "value": f"{n_alerts:,}", "accent": "primary"},
        {"label": "Highest level reached", "value": f"Level {max_level}" if max_level else "—",
         "accent": "danger" if max_level >= 3 else "warning" if max_level else "neutral"},
        {"label": "Acknowledged after escalation", "value": _rate(acked, n_alerts),
         "accent": "info",
         "subtitle": (f"Median {_dur(ack_after)} after the first escalation"
                      if ack_after is not None else f"{acked:,} of {n_alerts:,}")},
    ]

    per_level: dict[str, list[str]] = {}
    for r in names:
        per_level.setdefault(str(r.get("level")), []).append(str(r.get("name")))

    def level_key(r):
        lv = str(r.get("level"))
        return (0, int(lv)) if lv.isdigit() else (1, 0)
    level_rows = [[f"Level {r.get('level')}" if str(r.get("level")).isdigit() else "Unknown level",
                   f"{_num(r.get('escalations')):,}", f"{_num(r.get('repeats')):,}",
                   f"{_num(r.get('alerts')):,}",
                   ", ".join(per_level.get(str(r.get("level")), [])[:5]) or "—"]
                  for r in sorted((r for r in levels if not _num(r.get("is_total"))), key=level_key)]

    def rule_label(r) -> str:
        if r.get("rule_name"):
            return str(r["rule_name"])
        return "Deleted rule" if r.get("rid") else "No rule (built-in status alert)"
    rule_rows = [[rule_label(r), f"{_num(r.get('alerts')):,}", f"{_num(r.get('escalations')):,}",
                  f"{_num(r.get('repeats')):,}",
                  f"Level {_num(r.get('max_level'))}" if r.get("max_level") else "—",
                  f"{_num(r.get('acked')):,}", f"{_num(r.get('still_open')):,}"]
                 for r in rules[:top_n]]
    worst_rows = [[str(r.get("severity") or "info"),
                   _clip(message(r.get("message"), r.get("source_kind")), 110),
                   str(r.get("source") or "—"), str(r.get("rule_name") or "—"),
                   f"Level {_num(r.get('max_level'))}" if r.get("max_level") else "—",
                   f"{_num(r.get('pages')):,}", str(r.get("status") or "unknown"),
                   ctx.fmt_short(r.get("triggered_at"))] for r in worst]
    return _section(
        sid, title,
        description="Alerts that went unacknowledged long enough to page further tiers during "
                    "the period, by level and by rule.",
        kpis=kpis,
        charts=[{"title": "Escalated incidents by rule",
                 "series": {"kind": "rank", "unit": "incidents", "color": "warning",
                            "points": [{"label": rule_label(r), "value": _num(r.get("alerts"))}
                                       for r in rules[:10]]}}],
        tables=[
            {"title": "Escalations by level",
             "headers": ["Level", "Escalations", "Repeat pages", "Incidents", "Channels paged"],
             "styles": ["text", "num", "num", "num", "text"], "rows": level_rows},
            {"title": _top_title("Escalations by rule", len(rule_rows), len(rules)),
             "headers": ["Rule", "Incidents", "Escalations", "Repeats", "Highest level",
                         "Acknowledged", "Still open"],
             "styles": ["text", "num", "num", "num", "text", "num", "num"], "rows": rule_rows},
            {"title": "Incidents that escalated furthest",
             "headers": ["Severity", "Alert", "Source", "Rule", "Level", "Pages", "Status",
                         "Triggered"],
             "styles": ["severity", "text", "text", "text", "text", "num", "status", "text"],
             "rows": worst_rows},
        ],
        notes=["An escalation pages the next tier of a rule's escalation policy while an alert stays "
               "unacknowledged; repeat pages re-notify the same tier."])


# ─── Alert rule effectiveness (noise review) ────────────────────────────────

@register_section(
    "alert_rule_effectiveness", title="Alert Rule Noise Review", category="Alerting",
    description="Per alert rule: incidents raised, short-lived auto-recoveries, manual closures, "
                "median duration and flapping conditions, to tune noisy or stale rules.",
    scope="fleet", requires=("alerts.view",),
    options=[
        top_n_option(15, 100),
        {"key": "short_minutes", "label": "Short-lived when recovered within (minutes)",
         "type": "int", "default": 5, "min": 1, "max": 1440},
        {"key": "sort", "label": "Sort by", "type": "select", "default": "incidents",
         "choices": [["incidents", "Most incidents"], ["short_lived", "Most short-lived"],
                     ["flapping", "Most flapping conditions"], ["manual", "Most manual closures"]]},
    ],
)
async def alert_rule_effectiveness(ctx: SectionCtx) -> dict:
    from app.services import alert_workflow as wf

    sid, title = "alert_rule_effectiveness", "Alert Rule Noise Review"
    short = int(ctx.opt("short_minutes"))
    params: dict[str, Any] = {"f": ctx.frm, "t": ctx.to, "short": short, "org_tz": ctx.tz_name}
    rows = [dict(r) for r in (await ctx.db.execute(text(f"""
        WITH x AS (
            SELECT {wf.effective_rule_sql('a')} AS rid, a.status, a.triggered_at, a.resolved_at,
                   {wf.closure_reason_sql('a')} AS reason,
                   COALESCE(a.service_check_id, a.device_id, a.server_id, a.sensor_id)::text || ':'
                       || COALESCE(a.metadata->>'if_index', a.metadata->>'dedupe', '') AS cond,
                   a.metadata->'policy_snapshot'->>'name' AS snap_name
            FROM alerts a
            WHERE a.triggered_at BETWEEN :f AND :t AND NOT {wf.notice_sql('a')}
        ),
        flap AS (
            SELECT f.rid, count(DISTINCT f.cond) AS flapping
            FROM (SELECT rid, cond FROM x
                  GROUP BY rid, cond, date_trunc('day', triggered_at AT TIME ZONE :org_tz)
                  HAVING count(*) >= 3) f
            GROUP BY f.rid
        )
        SELECT x.rid::text AS rid, r.name AS rule_name, r.enabled AS enabled, r.metric AS metric,
               max(x.snap_name) AS snap_name,
               count(*) AS incidents,
               count(DISTINCT x.cond) AS conditions,
               count(*) FILTER (WHERE x.reason = 'recovered') AS auto_recovered,
               count(*) FILTER (WHERE x.reason = 'recovered' AND x.resolved_at >= x.triggered_at
                                AND x.resolved_at - x.triggered_at < make_interval(mins => :short))
                   AS short_lived,
               count(*) FILTER (WHERE x.reason = 'manual') AS manual,
               count(*) FILTER (WHERE x.status <> 'resolved') AS still_open,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (x.resolved_at - x.triggered_at)))
                   FILTER (WHERE x.status = 'resolved' AND x.resolved_at >= x.triggered_at) AS median_s,
               COALESCE(max(fl.flapping), 0) AS flapping
        FROM x
        LEFT JOIN alert_rules r ON r.id = x.rid
        LEFT JOIN flap fl ON fl.rid IS NOT DISTINCT FROM x.rid
        GROUP BY x.rid, r.name, r.enabled, r.metric
    """), params)).mappings().all()]
    fired_ids = [r["rid"] for r in rows if r.get("rid")]
    inv = (await ctx.db.execute(text(
        "SELECT count(*) FILTER (WHERE r.enabled) AS enabled,"
        " count(*) FILTER (WHERE NOT r.enabled) AS disabled,"
        " count(*) FILTER (WHERE r.enabled AND NOT (r.id::text = ANY(CAST(:fired AS text[])))) AS silent"
        " FROM alert_rules r"), {"fired": fired_ids})).mappings().first() or {}

    if not rows:
        notes = ["No alert incidents were raised in this period."]
        if _num(inv.get("enabled")):
            notes.append(f"All {_num(inv['enabled']):,} enabled rule(s) stayed quiet.")
        return _section(sid, title, description="Alert rule activity over the period.", notes=notes)

    def verdict(r: dict) -> str:
        n = _num(r.get("incidents"))
        if r.get("rid") and r.get("rule_name") is None:
            return "deleted"
        if r.get("enabled") is False:
            return "disabled"
        if _num(r.get("flapping")):
            return "flapping"
        if n >= 5 and _num(r.get("short_lived")) / n >= 0.5:
            return "noisy"
        if n >= 5 and _num(r.get("manual")) / n >= 0.5:
            return "review"
        return "ok"

    def label(r: dict) -> str:
        if r.get("rule_name"):
            return str(r["rule_name"])
        if r.get("rid"):
            return f"{r.get('snap_name') or 'Unnamed rule'} (deleted)"
        return "No rule (built-in status alerts)"

    sort = ctx.opt("sort") or "incidents"
    key = {"incidents": "incidents", "short_lived": "short_lived", "flapping": "flapping",
           "manual": "manual"}.get(sort, "incidents")
    rows.sort(key=lambda r: (-_num(r.get(key)), -_num(r.get("incidents")), label(r)))
    top_n = int(ctx.opt("top_n"))
    incidents = sum(_num(r.get("incidents")) for r in rows)
    short_lived = sum(_num(r.get("short_lived")) for r in rows)
    flapping = sum(_num(r.get("flapping")) for r in rows)
    stale = sum(_num(r.get("incidents")) for r in rows if verdict(r) in ("deleted", "disabled"))
    with_rule = sum(1 for r in rows if r.get("rid"))
    silent = inv.get("silent")
    kpis = [
        {"label": "Rules that fired", "value": f"{with_rule:,}", "accent": "primary",
         "subtitle": f"{incidents:,} incident(s)"},
        {"label": f"Short-lived (< {short} min)", "value": _rate(short_lived, incidents),
         "accent": "warning" if incidents and short_lived / incidents >= 0.3 else "info",
         "subtitle": f"{short_lived:,} recovered on their own"},
        {"label": "Flapping conditions", "value": f"{flapping:,}",
         "accent": "warning" if flapping else "success",
         "subtitle": "Raised 3+ times in one day"},
        {"label": "From disabled or deleted rules", "value": f"{stale:,}",
         "accent": "warning" if stale else "neutral", "subtitle": "Incidents"},
        {"label": "Enabled rules that did not fire", "value": "—" if silent is None else f"{_num(silent):,}",
         "accent": "neutral",
         "subtitle": f"of {_num(inv.get('enabled')):,} enabled" if silent is not None else "Not measured"},
    ]
    table_rows = [[label(r), f"{_num(r.get('incidents')):,}", f"{_num(r.get('auto_recovered')):,}",
                   f"{_num(r.get('short_lived')):,}", f"{_num(r.get('manual')):,}",
                   _dur(_flt(r.get("median_s"))), f"{_num(r.get('flapping')):,}", verdict(r)]
                  for r in rows[:top_n]]
    sort_label = {"incidents": "incidents", "short_lived": "short-lived incidents",
                  "flapping": "flapping conditions", "manual": "manual closures"}
    return _section(
        sid, title,
        description=f"Every alert rule that raised incidents over the period, ranked by "
                    f"{sort_label.get(sort, 'incidents')}.",
        kpis=kpis,
        charts=[{"title": "Incidents by rule",
                 "series": {"kind": "rank", "unit": "incidents", "color": "warning",
                            "points": [{"label": label(r), "value": _num(r.get("incidents"))}
                                       for r in sorted(rows, key=lambda r: -_num(r.get("incidents")))[:10]]}}],
        tables=[{"title": _top_title("Alert rules", len(table_rows), len(rows)),
                 "headers": ["Rule", "Incidents", "Auto-recovered", f"Short-lived (< {short} min)",
                             "Closed manually", "Median duration", "Flapping", "Verdict"],
                 "styles": ["text", "num", "num", "num", "num", "num", "num", "status"],
                 "rows": table_rows}],
        notes=[f"Short-lived: recovered automatically within {short} minute(s) — candidates for a "
               "longer confirmation delay or hold. Flapping: one condition (entity, interface) raised "
               "three or more incidents in a single day.",
               "Verdicts: noisy = at least half of 5+ incidents were short-lived; review = at least "
               "half were closed by hand; deleted/disabled = the rule no longer evaluates."])


# ─── Alerts per device ──────────────────────────────────────────────────────

@register_section(
    "alert_by_device", title="Alerts per Device", category="Alerting",
    description="Critical, warning and info alert incidents per device over the period, and "
                "incidents still open.",
    scope="device", requires=("alerts.view",),
    options=[top_n_option(15, 200),
             {"key": "sort", "label": "Sort by", "type": "select", "default": "total",
              "choices": [["total", "Most alerts"], ["critical", "Most critical"],
                          ["open", "Most open now"]]}],
)
async def alert_by_device(ctx: SectionCtx) -> dict:
    from app.services import alert_workflow as wf

    sid, title = "alert_by_device", "Alerts per Device"
    clause, dparams = ctx.pg_device_clause("a.device_id")
    params: dict[str, Any] = {"f": ctx.frm, "t": ctx.to, **dparams}
    w = "a.triggered_at BETWEEN :f AND :t"
    rows = [dict(r) for r in (await ctx.db.execute(text(f"""
        SELECT g.*, d.hostname, host(d.ip_address) AS ip, COALESCE(NULLIF(d.location, ''), '—') AS location
        FROM (SELECT a.device_id,
                     count(*) FILTER (WHERE {w}) AS total,
                     count(*) FILTER (WHERE {w} AND a.severity = 'critical') AS critical,
                     count(*) FILTER (WHERE {w} AND a.severity = 'warning') AS warning,
                     count(*) FILTER (WHERE {w} AND a.severity = 'info') AS info,
                     count(*) FILTER (WHERE a.status IN ('active', 'acknowledged')) AS open_now,
                     count(*) FILTER (WHERE a.status = 'active' AND a.severity = 'critical') AS active_critical,
                     max(a.triggered_at) FILTER (WHERE {w}) AS last_at
              FROM alerts a
              WHERE a.device_id IS NOT NULL AND NOT {wf.notice_sql('a')}
                AND ({w} OR a.status IN ('active', 'acknowledged')){clause}
              GROUP BY a.device_id) g
        LEFT JOIN devices d ON d.id = g.device_id
    """), params)).mappings().all()]
    pf, pt = ctx.prior_window()
    prior = (await ctx.db.execute(text(
        f"SELECT count(*) FROM alerts a WHERE a.device_id IS NOT NULL AND NOT {wf.notice_sql('a')}"
        f" AND a.triggered_at BETWEEN :pf AND :pt{clause}"), {"pf": pf, "pt": pt, **dparams})).scalar()

    if not rows:
        return _section(sid, title, description="Alert incidents per device over the period.",
                        notes=["No device raised an alert incident in this period, and none is "
                               "open now."])
    sort = ctx.opt("sort") or "total"
    key = {"total": "total", "critical": "critical", "open": "open_now"}.get(sort, "total")
    rows.sort(key=lambda r: (-_num(r.get(key)), -_num(r.get("total")), str(r.get("hostname") or "")))
    top_n = int(ctx.opt("top_n"))
    total = sum(_num(r.get("total")) for r in rows)
    with_alerts = sum(1 for r in rows if _num(r.get("total")))
    crit = sum(_num(r.get("critical")) for r in rows)
    open_now = sum(_num(r.get("open_now")) for r in rows)
    active_crit = sum(_num(r.get("active_critical")) for r in rows)

    def name(r: dict) -> str:
        return str(r.get("hostname") or r.get("ip") or str(r.get("device_id") or "")[:8] or "—")

    kpi_total = {"label": "Device alert incidents", "value": f"{total:,}", "accent": "primary"}
    if (d := _count_delta(total, _num(prior) if prior is not None else None)):
        kpi_total["subtitle"] = d
    kpis = [
        kpi_total,
        {"label": "Devices with alerts", "value": f"{with_alerts:,}", "accent": "info"},
        {"label": "Critical", "value": f"{crit:,}", "accent": "danger" if crit else "success"},
        {"label": "Open now", "value": f"{open_now:,}", "accent": "warning" if open_now else "success",
         "subtitle": f"{active_crit:,} critical unacknowledged · as of report generation"},
    ]
    shown = rows[:top_n]
    by_label = {"total": "alerts", "critical": "critical alerts", "open": "open alerts"}
    return _section(
        sid, title,
        description=f"Devices ranked by {by_label.get(sort, 'alerts')}: alert incidents raised over "
                    "the period and incidents still open.",
        kpis=kpis,
        charts=[{"title": f"Top devices by {by_label.get(sort, 'alerts')}",
                 "series": {"kind": "rank", "unit": "incidents",
                            "color": "danger" if sort == "critical" else "warning",
                            "points": [{"label": name(r), "value": _num(r.get(key))}
                                       for r in shown[:10] if _num(r.get(key))]}}],
        tables=[{"title": _top_title("Alerts per device", len(shown), len(rows)),
                 "headers": ["Device", "IP address", "Location", "Critical", "Warning", "Info",
                             "Total", "Open now", "Last alert"],
                 "styles": ["text", "mono", "text", "num", "num", "num", "num", "num", "text"],
                 "rows": [[name(r), str(r.get("ip") or "—"), str(r.get("location") or "—"),
                           f"{_num(r.get('critical')):,}", f"{_num(r.get('warning')):,}",
                           f"{_num(r.get('info')):,}", f"{_num(r.get('total')):,}",
                           f"{_num(r.get('open_now')):,}", ctx.fmt_short(r.get("last_at"))]
                          for r in shown]}],
        notes=["Incidents only (recovery and suppression notices excluded). Alerts on servers, "
               "service checks, sensors, applications and the appliance itself are not tied to a "
               "device and are not included."])


# ─── SNMP traps ─────────────────────────────────────────────────────────────

@register_section(
    "snmp_trap_summary", title="SNMP Trap Summary", category="Events",
    description="SNMP traps received over the period by severity, the most frequent trap types "
                "and the devices sending the most traps.",
    scope="device", requires=("devices.view",), options=[top_n_option(10, 100)],
)
async def snmp_trap_summary(ctx: SectionCtx) -> dict:
    sid, title = "snmp_trap_summary", "SNMP Trap Summary"
    dev_sql, dparams = ctx.ch_device_clause("device_id")
    filtered = bool(ctx.device_ids)
    # A device filter only ever matches traps attributed to a managed device.
    scope_sql = f" AND {TRAP_KNOWN_SQL}{dev_sql}" if filtered else ""
    base = f"FROM zenplus.snmp_traps WHERE timestamp >= %(f)s AND timestamp < %(t)s{scope_sql}"
    params: dict[str, Any] = {"f": ctx.frm, "t": ctx.to, **dparams}
    notes: list[str] = []
    if (n := ctx.window_note(30, "SNMP trap history")):
        notes.append(n)

    tot_rows = await ctx.ch_rows(
        "SELECT count(), countIf(severity = 'critical'), countIf(severity = 'warning'),"
        f" countIf(severity = 'info'), countIf({TRAP_UNKNOWN_SQL}),"
        f" uniqExactIf(device_id, {TRAP_KNOWN_SQL}), uniqExact(trap_oid),"
        f" uniqExactIf(source_ip_text, {TRAP_UNKNOWN_SQL}) {base}", params)
    tot = tot_rows[0] if tot_rows else (0,) * 8
    total = _num(tot[0])
    if not total:
        return _section(sid, title, description="SNMP traps received over the period.",
                        notes=["No SNMP traps were received in this period."] + notes)
    crit, warn, info, unknown = (_num(v) for v in tot[1:5])
    devices, kinds, unknown_sources = (_num(v) for v in tot[5:8])

    unit = _bucket_unit(ctx)
    bucket, bparams = ctx.ch_bucket("toDateTime(timestamp)", "1 HOUR" if unit == "hour" else "1 DAY")
    trend = await ctx.ch_rows(
        f"SELECT toUnixTimestamp(toDateTime({bucket}, %(org_tz)s)) AS b, severity, count() {base}"
        " GROUP BY b, severity ORDER BY b", {**params, **bparams})
    top_n = int(ctx.opt("top_n"))
    kinds_rows = await ctx.ch_rows(
        "SELECT trap_name, trap_oid, any(severity), count() AS n, uniqExact(source_ip_text),"
        f" max(timestamp) {base} GROUP BY trap_name, trap_oid ORDER BY n DESC LIMIT %(n)s",
        {**params, "n": top_n})
    dev_rows = await ctx.ch_rows(
        "SELECT toString(device_id) AS did, count() AS n, countIf(severity = 'critical'),"
        " countIf(severity = 'warning'), uniqExact(trap_oid), max(timestamp)"
        f" {base}{'' if filtered else ' AND ' + TRAP_KNOWN_SQL}"
        " GROUP BY did ORDER BY n DESC LIMIT %(n)s", {**params, "n": top_n})
    unknown_rows = []
    if not filtered and unknown:
        unknown_rows = await ctx.ch_rows(
            "SELECT source_ip_text, count() AS n, uniqExact(trap_oid), any(trap_name), max(timestamp)"
            f" {base} AND {TRAP_UNKNOWN_SQL} GROUP BY source_ip_text ORDER BY n DESC LIMIT %(n)s",
            {**params, "n": top_n})

    hosts: dict[str, tuple[str, str]] = {}
    ids = [str(r[0]) for r in dev_rows if r[0]]
    if ids:
        for r in (await ctx.db.execute(text(
                "SELECT id::text, hostname, host(ip_address) FROM devices WHERE id::text = ANY(:ids)"),
                {"ids": ids})).fetchall():
            hosts[str(r[0])] = (r[1] or r[2] or str(r[0])[:8], r[2] or "—")

    counts: dict[str, dict[int, float]] = {}
    for b, sev, n in trend:
        counts.setdefault(str(sev), {})[int(b)] = _num(n)
    kpis = [
        {"label": "Traps received", "value": f"{total:,}", "accent": "primary",
         "subtitle": f"{kinds:,} trap type(s)"},
        {"label": "Critical", "value": f"{crit:,}", "accent": "danger" if crit else "success"},
        {"label": "Warning", "value": f"{warn:,}", "accent": "warning" if warn else "neutral"},
        {"label": "Info", "value": f"{info:,}", "accent": "info"},
        {"label": "Devices sending traps", "value": f"{devices:,}", "accent": "info"},
    ]
    if not filtered:
        kpis.append({"label": "Unknown sources", "value": f"{unknown_sources:,}",
                     "accent": "warning" if unknown_sources else "neutral",
                     "subtitle": f"{unknown:,} trap(s) from unmanaged addresses"})
    tables = [
        {"title": _top_title("Most frequent traps", len(kinds_rows), kinds),
         "headers": ["Trap", "OID", "Severity", "Count", "Sources", "Last received"],
         "styles": ["text", "mono", "severity", "num", "num", "text"],
         "rows": [[str(r[0] or "—"), str(r[1] or "—"), str(r[2] or "info"), f"{_num(r[3]):,}",
                   f"{_num(r[4]):,}", ctx.fmt_short(r[5])] for r in kinds_rows]},
    ]
    if dev_rows:
        tables.append({
            "title": _top_title("Devices sending the most traps", len(dev_rows), devices),
            "headers": ["Device", "IP address", "Traps", "Critical", "Warning", "Trap types",
                        "Last received"],
            "styles": ["text", "mono", "num", "num", "num", "num", "text"],
            "rows": [[hosts.get(str(r[0]), (str(r[0])[:8], "—"))[0],
                      hosts.get(str(r[0]), ("", "—"))[1], f"{_num(r[1]):,}", f"{_num(r[2]):,}",
                      f"{_num(r[3]):,}", f"{_num(r[4]):,}", ctx.fmt_short(r[5])] for r in dev_rows]})
    if unknown_rows:
        tables.append({
            "title": _top_title("Traps from unknown sources", len(unknown_rows), unknown_sources),
            "headers": ["Source address", "Traps", "Trap types", "Example trap", "Last received"],
            "styles": ["mono", "num", "num", "text", "text"],
            "rows": [[str(r[0] or "—"), f"{_num(r[1]):,}", f"{_num(r[2]):,}", str(r[3] or "—"),
                      ctx.fmt_short(r[4])] for r in unknown_rows]})
        notes.append("Unknown sources sent traps from addresses that match no managed device — add "
                     "them as devices or stop them sending to this appliance.")
    if filtered:
        notes.append("Limited to the selected devices; traps from unmanaged addresses are not shown.")
    notes.append("Trap severity is a best-effort classification of well-known trap OIDs.")
    return _section(
        sid, title,
        description=f"{total:,} SNMP trap(s) received over the period, by severity, trap type and "
                    "device.",
        kpis=kpis,
        charts=[{"title": "Traps over time by severity",
                 "series": _time_series(ctx, unit, counts,
                                        [("critical", "Critical"), ("warning", "Warning"),
                                         ("info", "Info")], "traps", "warning")}],
        tables=tables, notes=notes)


# ─── Syslog ─────────────────────────────────────────────────────────────────

def _syslog_retention_days() -> int:
    try:
        return max(1, int(os.getenv("SYSLOG_RETENTION_DAYS", "30")))
    except ValueError:
        return 30


def _syslog_sev_style(sev: int) -> str:
    return "critical" if sev <= 2 else "major" if sev == 3 else "warning" if sev == 4 else "info"


@register_section(
    "syslog_summary", title="Syslog Events Summary", category="Events",
    description="Syslog messages received from network devices over the period by severity and "
                "facility, the busiest sources and applications, and the latest severe messages.",
    scope="device", requires=("devices.view",),
    options=[top_n_option(10, 100),
             {"key": "min_severity", "label": "List messages at or above", "type": "select",
              "default": "critical",
              "choices": [["critical", "Critical (emergency, alert, critical)"],
                          ["error", "Error and above"], ["warning", "Warning and above"]]}],
)
async def syslog_summary(ctx: SectionCtx) -> dict:
    sid, title = "syslog_summary", "Syslog Events Summary"
    clause, dparams = ctx.pg_device_clause("e.device_id")
    filtered = bool(ctx.device_ids)
    params: dict[str, Any] = {"f": ctx.frm, "t": ctx.to, **dparams}
    w = f"e.received_at BETWEEN :f AND :t{clause}"
    notes: list[str] = []
    if (n := ctx.window_note(_syslog_retention_days(), "Syslog history")):
        notes.append(n)

    groups = (await ctx.db.execute(text(f"""
        SELECT GROUPING(e.severity) AS g_sev, e.severity, e.facility, count(*) AS n
        FROM network_events e WHERE {w}
        GROUP BY GROUPING SETS ((e.severity), (e.facility))
    """), params)).mappings().all()
    by_sev: dict[int, int] = {}
    by_fac: dict[int, int] = {}
    for r in groups:
        if _num(r.get("g_sev")) == 0 and r.get("severity") is not None:
            by_sev[int(r["severity"])] = _num(r.get("n"))
        elif r.get("facility") is not None:
            by_fac[int(r["facility"])] = _num(r.get("n"))
    total = sum(by_sev.values())
    if not total:
        return _section(sid, title, description="Syslog messages received over the period.",
                        notes=["No syslog messages were received in this period."] + notes)

    top_n = int(ctx.opt("top_n"))
    sources = [dict(r) for r in (await ctx.db.execute(text(f"""
        SELECT g.device_id, g.source_ip, g.n, g.severe, g.errors, g.warnings, g.last_at,
               d.hostname, host(d.ip_address) AS ip
        FROM (SELECT e.device_id,
                     CASE WHEN e.device_id IS NULL THEN host(e.source_ip) END AS source_ip,
                     count(*) AS n,
                     count(*) FILTER (WHERE e.severity <= 2) AS severe,
                     count(*) FILTER (WHERE e.severity = 3) AS errors,
                     count(*) FILTER (WHERE e.severity = 4) AS warnings,
                     max(e.received_at) AS last_at
              FROM network_events e WHERE {w}
              GROUP BY 1, 2) g
        LEFT JOIN devices d ON d.id = g.device_id
        ORDER BY g.severe DESC, g.n DESC
    """), params)).mappings().all()]
    unit = _bucket_unit(ctx)
    trend = (await ctx.db.execute(text(f"""
        SELECT {_pg_bucket('e.received_at')} AS b,
               CASE WHEN e.severity <= 2 THEN 'severe' WHEN e.severity = 3 THEN 'error'
                    WHEN e.severity = 4 THEN 'warning' ELSE 'other' END AS cls,
               count(*) AS n
        FROM network_events e WHERE {w} GROUP BY 1, 2
    """), {**params, "unit": unit, "org_tz": ctx.tz_name})).mappings().all()
    apps = (await ctx.db.execute(text(f"""
        SELECT g.app, g.n, g.worst, count(*) OVER () AS total_apps
        FROM (SELECT COALESCE(NULLIF(trim(e.application), ''), '(none)') AS app, count(*) AS n,
                     min(e.severity) AS worst
              FROM network_events e WHERE {w} GROUP BY 1) g
        ORDER BY g.n DESC LIMIT :n
    """), {**params, "n": top_n})).mappings().all()
    level = SYSLOG_LEVELS.get(ctx.opt("min_severity") or "critical", 2)
    latest = (await ctx.db.execute(text(f"""
        SELECT e.received_at, e.severity, e.facility, e.application, e.message,
               COALESCE(d.hostname, NULLIF(e.reported_hostname, ''), host(e.source_ip)) AS source
        FROM network_events e LEFT JOIN devices d ON d.id = e.device_id
        WHERE {w} AND e.severity <= :lvl
        ORDER BY e.received_at DESC LIMIT :n
    """), {**params, "lvl": level, "n": top_n})).mappings().all()

    devices = [s for s in sources if s.get("device_id")]
    unattributed = [s for s in sources if not s.get("device_id")]
    severe = sum(v for k, v in by_sev.items() if k <= 2)
    kpis = [
        {"label": "Syslog messages", "value": f"{total:,}", "accent": "primary",
         "subtitle": f"{len(by_fac):,} facilit{'y' if len(by_fac) == 1 else 'ies'}"},
        {"label": "Emergency to critical", "value": f"{severe:,}",
         "accent": "danger" if severe else "success", "subtitle": "Severities 0–2"},
        {"label": "Errors", "value": f"{by_sev.get(3, 0):,}",
         "accent": "warning" if by_sev.get(3) else "neutral", "subtitle": "Severity 3"},
        {"label": "Warnings", "value": f"{by_sev.get(4, 0):,}", "accent": "info",
         "subtitle": "Severity 4"},
        {"label": "Devices reporting", "value": f"{len(devices):,}", "accent": "info"},
    ]
    if not filtered:
        kpis.append({"label": "Unmatched sources", "value": f"{len(unattributed):,}",
                     "accent": "warning" if unattributed else "neutral",
                     "subtitle": "Addresses that match no managed device"})

    counts: dict[str, dict[int, float]] = {}
    for r in trend:
        if r.get("b") is not None:
            counts.setdefault(str(r.get("cls")), {})[int(float(r["b"]))] = _num(r.get("n"))
    charts = [
        {"title": "Messages over time by severity",
         "series": _time_series(ctx, unit, counts,
                                [("severe", "Emergency–critical"), ("error", "Error"),
                                 ("warning", "Warning"), ("other", "Notice and below")],
                                "messages", "info")},
        {"title": "Messages by facility",
         "series": {"kind": "donut", "unit": "messages",
                    "points": [{"label": SYSLOG_FACILITY.get(k, str(k)), "value": v}
                               for k, v in sorted(by_fac.items(), key=lambda kv: -kv[1])[:8]]}},
    ]

    def src_name(s: dict) -> str:
        if s.get("device_id"):
            return str(s.get("hostname") or s.get("ip") or str(s["device_id"])[:8])
        return f"{s.get('source_ip') or '—'} (unmanaged)"
    shown = sources[:top_n]
    tables = [
        {"title": "Messages by severity",
         "headers": ["Severity", "Level", "Messages", "Share"],
         "styles": ["severity", "text", "num", "pct"],
         "rows": [[_syslog_sev_style(k), f"{k} · {SYSLOG_SEVERITY[k]}", f"{v:,}", _rate(v, total)]
                  for k, v in sorted(by_sev.items()) if 0 <= k <= 7]},
        {"title": _top_title("Busiest sources", len(shown), len(sources)),
         "headers": ["Source", "IP address", "Messages", "Emergency–critical", "Errors", "Warnings",
                     "Last message"],
         "styles": ["text", "mono", "num", "num", "num", "num", "text"],
         "rows": [[src_name(s), str(s.get("ip") or s.get("source_ip") or "—"), f"{_num(s.get('n')):,}",
                   f"{_num(s.get('severe')):,}", f"{_num(s.get('errors')):,}",
                   f"{_num(s.get('warnings')):,}", ctx.fmt_short(s.get("last_at"))] for s in shown]},
    ]
    if apps:
        n_apps = _num(apps[0].get("total_apps"))
        tables.append({
            "title": _top_title("Top applications", len(apps), n_apps),
            "headers": ["Application", "Messages", "Most severe"],
            "styles": ["text", "num", "severity"],
            "rows": [[str(r.get("app")), f"{_num(r.get('n')):,}", _syslog_sev_style(_num(r.get("worst")))]
                     for r in apps]})
    level_name = {2: "critical or worse", 3: "error or worse", 4: "warning or worse"}[level]
    if latest:
        tables.append({
            "title": f"Latest messages ({level_name})",
            "headers": ["Received", "Source", "Severity", "Facility", "Application", "Message"],
            "styles": ["text", "text", "severity", "text", "text", "text"],
            "rows": [[ctx.fmt_short(r.get("received_at")), str(r.get("source") or "—"),
                      _syslog_sev_style(_num(r.get("severity"))),
                      SYSLOG_FACILITY.get(_num(r.get("facility")), str(r.get("facility"))),
                      str(r.get("application") or "—"), _clip(r.get("message"), 200)]
                     for r in latest]})
    else:
        notes.append(f"No syslog messages {level_name} in this period.")
    if filtered:
        notes.append("Limited to the selected devices; messages from unmanaged addresses are not shown.")
    notes.append("Messages are shown as received (truncated to 200 characters); they can contain "
                 "operational detail from the sending device.")
    return _section(
        sid, title,
        description=f"{total:,} syslog message(s) received over the period, by severity, facility, "
                    "source and application.",
        kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── Built-in templates ─────────────────────────────────────────────────────

template(
    "alert_response_performance", "Alert Response Performance (MTTA / MTTR)",
    "How quickly alert incidents are acknowledged, recovered and closed against the "
    "acknowledgement targets, for all and for critical incidents, with the open backlog and "
    "escalations.",
    "alerts",
    [S("alert_response_metrics"),
     S("alert_response_metrics", "Critical Alert Response", severity="critical"),
     S("alert_backlog_aging"),
     S("alert_escalations")],
    audience=("IT manager", "NOC operator", "Executive"), tags=("mtta", "mttr", "response", "sla"),
    default_period="last_30d", frequency="monthly", icon="Gauge", featured=True,
)
template(
    "alert_open_backlog", "Open Alert Backlog",
    "Every open alert incident by age and severity, the oldest incidents still waiting, and the "
    "devices with the most open alerts.",
    "alerts",
    [S("alert_backlog_aging", None, top_n=25),
     S("alert_by_device", "Devices with Open Alerts", sort="open", top_n=15)],
    audience=("NOC operator", "IT manager"), tags=("backlog", "aging", "open alerts"),
    default_period="last_24h", frequency="daily", icon="Bell",
)
template(
    "alert_notification_health", "Notification Delivery Health",
    "Whether alert notifications reach people: accepted, failed and skipped deliveries per "
    "channel, latency, failure reasons and escalations.",
    "alerts",
    [S("alert_notification_delivery", None, top_n=25), S("alert_escalations")],
    audience=("NOC operator", "IT manager"), tags=("notifications", "email", "webhook", "delivery"),
    default_period="last_7d", frequency="weekly", icon="Activity",
)
template(
    "alert_escalation_review", "Alert Escalations",
    "Alerts that went unacknowledged long enough to page further tiers, by level and rule, with "
    "critical response times and notification delivery.",
    "alerts",
    [S("alert_escalations", None, top_n=15),
     S("alert_response_metrics", "Critical Alert Response", severity="critical"),
     S("alert_notification_delivery")],
    audience=("IT manager", "NOC operator"), tags=("escalation", "on-call", "paging"),
    default_period="last_30d", frequency="monthly", icon="Layers",
)
template(
    "alert_rule_noise_review", "Alert Rule Noise Review",
    "Tune alerting: rules ranked by incidents, short-lived auto-recoveries, flapping conditions "
    "and manual closures, with the noisiest sources and devices.",
    "alerts",
    [S("alert_rule_effectiveness", None, top_n=25),
     S("alert_rule_effectiveness", "Rules with the Most Short-Lived Alerts", sort="short_lived",
       top_n=10),
     S("noisy_sources"),
     S("alert_by_device", "Devices Raising the Most Alerts", top_n=10)],
    audience=("Network engineer", "NOC operator"), tags=("noise", "tuning", "flapping", "rules"),
    default_period="last_30d", frequency="monthly", icon="Wrench",
)
template(
    "alerts_per_device", "Alerts per Device",
    "Alert incidents per device — critical, warning and info counts, open incidents and the "
    "devices with the most critical alerts.",
    "alerts",
    [S("alert_by_device", "Top 25 Devices by Alert Count", top_n=25),
     S("alert_by_device", "Devices with the Most Critical Alerts", sort="critical", top_n=10)],
    audience=("NOC operator", "Network engineer"), tags=("devices", "alerts", "top-n"),
    default_period="last_7d", frequency="weekly", icon="Server",
)
template(
    "snmp_trap_report", "SNMP Trap Summary",
    "SNMP traps received by severity, the most frequent trap types, the devices sending the most "
    "traps and traps from unknown sources.",
    "alerts",
    [S("snmp_trap_summary", None, top_n=15)],
    audience=("Network engineer", "NOC operator"), tags=("snmp", "traps", "events"),
    default_period="last_7d", frequency="weekly", icon="Activity",
)
template(
    "syslog_events_summary", "Syslog Events Summary",
    "Syslog messages from network devices by severity and facility, the busiest sources and "
    "applications, and the latest critical messages.",
    "alerts",
    [S("syslog_summary", None, top_n=15)],
    audience=("Network engineer", "NOC operator"), tags=("syslog", "events", "logs"),
    default_period="last_7d", frequency="weekly", icon="FileText",
)
template(
    "network_event_log_review", "Network Device Event Log Review",
    "Security-oriented review of device event logs: error-and-above syslog messages, their "
    "sources and applications, and SNMP traps including unknown senders.",
    "security",
    [S("syslog_summary", "Syslog Errors and Above", min_severity="error", top_n=25),
     S("snmp_trap_summary")],
    audience=("Security & audit", "Network engineer"), tags=("syslog", "traps", "audit"),
    default_period="last_7d", frequency="weekly", icon="ShieldCheck",
)
template(
    "monthly_incident_review", "Monthly Incident Review",
    "Monthly review of alerting: volume and trend, response times against targets, the open "
    "backlog, noisy rules, the devices raising the most alerts, escalations and notification "
    "delivery.",
    "alerts",
    [S("alert_kpis"), S("alert_trend"), S("alert_response_metrics"), S("alert_backlog_aging"),
     S("alert_rule_effectiveness", "Noisiest Alert Rules", top_n=10),
     S("alert_by_device", "Devices with the Most Alerts", top_n=10),
     S("alert_escalations"), S("alert_notification_delivery")],
    audience=("IT manager", "Executive", "NOC operator"), tags=("monthly review", "incidents", "mttr"),
    default_period="previous_month", frequency="monthly", icon="FileCheck2",
)
