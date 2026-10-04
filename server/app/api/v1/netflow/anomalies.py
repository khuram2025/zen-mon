"""NetFlow detection findings, detector status, exceptions and threat intel (B5).

Read side (``netflow.view``, scoped to the exporters the caller may see):

* ``GET /netflow/findings`` — persisted findings of the detection engine
  (``services/netflow_detection.py``) seen in the window, with lifecycle
  counts and per-detector status (contract §11). Detector status comes from
  the engine's last evaluated bucket, narrowed to the caller's exporters; the
  engine block says when it last ran and how much of the window it evaluated,
  so the page can only say "no candidates" for checks that ran (NF-03).
* ``GET /netflow/findings/run`` — the same detectors evaluated ad hoc over any
  window of raw records (≤ 31 days), for investigations before the engine
  ran or beyond what it stored; nothing is persisted.
* ``GET /netflow/findings/{id}`` and ``/timeline`` — one finding with its
  notes, and its per-minute evidence series from raw records.
* ``GET /netflow/anomalies`` — the legacy endpoint (``{total, findings,
  generated_at}``), now an ad-hoc run with honest counts plus ``detectors``,
  ``complete`` and ``window``.

Write side (every write audited):

* lifecycle — acknowledge / assign / resolve / reopen / note need
  ``alerts.acknowledge`` or ``netflow.manage``; suppress / unsuppress need
  ``netflow.manage`` (contract §6);
* detector settings (``system_settings`` ``netflow_detectors``), exceptions
  and threat-intel feeds need ``netflow.manage``.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.user import User
from app.services import netflow_detection as det
from app.services import netflow_ti as ti
from app.services.audit_service import write_audit_log

from .access import (
    MANAGE_PERMISSION,
    VIEW_PERMISSION,
    FlowVisibility,
    can_acknowledge_findings,
    canonical_ip,
    flow_visibility,
    require_view,
    visibility_for_user,
    require_manage,
    require_triage,
)
from .common import (
    BUDGET_FORENSICS,
    MAX_SPAN_RAW,
    MAX_SPAN_ROLLUP,
    RAW,
    Window,
    _to_iso_utc,
    current_meta,
    invalid_filter,
    now_utc,
    run_query,
    table_ref,
    window_params,
)
from .filters import (
    FlowFilter,
    compile_filter,
    flow_filter,
    parse_flow_filter,
    require_filter_exporters_visible,
)
from .sql import DST, SRC, floor_to

router = APIRouter()

# Filter keys a findings list can apply (the rest are reported as not applied).
FINDING_FILTER_KEYS = frozenset({"exporter", "talker", "src", "dst"})
# Keys an ad-hoc detector run applies: the exporter scope only -- a protocol
# or port filter would make a detector "run" on data it was never meant to
# judge, and report a clean result for it.
RUN_FILTER_KEYS = frozenset({"exporter"})
STATUS_FILTERS = {
    "active": ("open", "acknowledged"),
    "open": ("open",),
    "acknowledged": ("acknowledged",),
    "suppressed": ("suppressed",),
    "resolved": ("resolved",),
    "all": det.STATUSES,
}
SORTS = {
    "severity": "CASE severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END DESC, last_seen DESC",
    "last_seen": "last_seen DESC",
    "first_seen": "first_seen DESC",
    "occurrences": "occurrences DESC, last_seen DESC",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _visible(vis: FlowVisibility, f: FlowFilter | None) -> frozenset[str] | None:
    """Exporters the answer may cover: the caller's scope narrowed by the
    page's exporter filter (None = every exporter)."""
    scope = vis.exporters
    if f is not None and f.exporter:
        ip = canonical_ip(f.exporter)
        scope = frozenset({ip}) if scope is None else scope & {ip}
    return scope


def _forbid(message: str) -> HTTPException:
    return HTTPException(status_code=403, detail=message)


def _require_ack(vis: FlowVisibility) -> None:
    if not can_acknowledge_findings(vis):
        raise _forbid("Acknowledging, assigning and resolving findings needs alerts.acknowledge or netflow.manage")


def _require_manage(vis: FlowVisibility, what: str = "this change") -> None:
    if not vis.can(MANAGE_PERMISSION):
        raise _forbid(f"{what[:1].upper()}{what[1:]} needs the netflow.manage permission")


def _drop_unapplied(f: FlowFilter, allowed: frozenset[str]) -> FlowFilter:
    extra = f.active_keys() - allowed
    if extra:
        current_meta().dropped |= set(extra)
        f = f.without(*extra)
    return f


def _scope_sql(vis: FlowVisibility, f: FlowFilter, params: dict) -> list[str]:
    """PostgreSQL predicates: visibility + exporter/talker/src/dst filters."""
    where: list[str] = []
    if vis.exporters is not None:
        if not vis.exporters:
            return ["FALSE"]
        where.append("exporter_ip = ANY(CAST(:vis_exporters AS inet[]))")
        params["vis_exporters"] = sorted(vis.exporters)
        current_meta().restricted = True
    if f.exporter:
        where.append("exporter_ip = CAST(:f_exporter AS inet)")
        params["f_exporter"] = canonical_ip(f.exporter)
    if f.talker is not None:
        where.append("(src_ip <<= CAST(:f_talker AS inet) OR dst_ip <<= CAST(:f_talker AS inet))")
        params["f_talker"] = str(f.talker.network)
    if f.src is not None:
        where.append("src_ip <<= CAST(:f_src AS inet)")
        params["f_src"] = str(f.src.network)
    if f.dst is not None:
        where.append("dst_ip <<= CAST(:f_dst AS inet)")
        params["f_dst"] = str(f.dst.network)
    return where


async def _usernames(db: AsyncSession, rows: list[dict]) -> dict[str, str]:
    ids: set[str] = set()
    for r in rows:
        for k in ("assigned_to", "acknowledged_by", "suppressed_by"):
            if r.get(k):
                ids.add(str(r[k]))
    if not ids:
        return {}
    res = await db.execute(text("SELECT id::text, username FROM users WHERE id = ANY(CAST(:ids AS uuid[]))"),
                           {"ids": sorted(ids)})
    return {r[0]: r[1] for r in res.all()}


async def _load_finding(db: AsyncSession, finding_id: uuid.UUID, vis: FlowVisibility, *, lock: bool = False) -> dict:
    row = (await db.execute(
        text(f"SELECT {det.FINDING_COLUMNS} FROM netflow_findings WHERE id = :id" + (" FOR UPDATE" if lock else "")),
        {"id": finding_id},
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    d = det.row_dict(row)
    exporter = det._inet(d.get("exporter_ip"))
    if vis.exporters is not None and (exporter is None or not vis.allows(exporter)):
        raise HTTPException(status_code=404, detail="Finding not found")
    return d


def _actor_note(user: User, text_: str, kind: str = "action") -> dict:
    return det._note(text_, by=str(user.id), by_name=getattr(user, "username", None), kind=kind)


# ── Findings (persisted) ─────────────────────────────────────────────────────

@router.get("/findings")
async def list_findings(
    win: Window = Depends(window_params(24, MAX_SPAN_ROLLUP)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
    status: str = Query(default="active", description="active | open | acknowledged | suppressed | resolved | all"),
    kind: str | None = Query(default=None, description="Comma-separated detector kinds"),
    severity: str | None = Query(default=None, description="Comma-separated severities"),
    q: str | None = Query(default=None, max_length=120, description="Search title, description or address"),
    sort: str = Query(default="severity"),
    limit: int = Query(default=100, ge=0, le=500),
    offset: int = Query(default=0, ge=0, le=100000),
    format: str = Query(default="json"),
):
    """Persisted findings seen in the window (``last_seen >= from`` and
    ``first_seen < to``), lifecycle counts and detector status (§11)."""
    require_filter_exporters_visible(f, vis)
    f = _drop_unapplied(f, FINDING_FILTER_KEYS)
    if status not in STATUS_FILTERS:
        raise invalid_filter(f"Unknown status '{status[:32]}'", {"status": "Unknown status"})
    if sort not in SORTS:
        raise invalid_filter(f"Unknown sort '{sort[:32]}'", {"sort": "Unknown sort"})
    if format not in ("json", "csv"):
        raise invalid_filter("format must be json or csv", {"format": "Unknown format"})
    kinds = [k.strip() for k in (kind or "").split(",") if k.strip()]
    for k in kinds:
        if k not in det.DETECTOR_BY_KIND:
            raise invalid_filter(f"Unknown detector '{k[:48]}'", {"kind": "Unknown detector"})
    severities = [s.strip() for s in (severity or "").split(",") if s.strip()]
    for s in severities:
        if s not in det.SEVERITIES:
            raise invalid_filter(f"Unknown severity '{s[:16]}'", {"severity": "Unknown severity"})

    meta = current_meta()
    meta.set_source("pg", win.start, win.end, None, retention_checked=True)
    params: dict[str, Any] = {"w0": win.start, "w1": win.end}
    base = ["last_seen >= :w0", "first_seen < :w1", *_scope_sql(vis, f, params)]
    # Active findings per detector over every detector (the detector picker's counts).
    by_kind_all = {
        r[0]: int(r[1]) for r in (await db.execute(text(
            f"SELECT kind, count(*) FROM netflow_findings WHERE {' AND '.join(base)} "
            "AND status IN ('open', 'acknowledged') GROUP BY kind"
        ), params)).all()
    }
    if kinds:
        base.append("kind = ANY(:kinds)")
        params["kinds"] = kinds
    if q and q.strip():
        term = q.strip()
        try:
            net = ipaddress.ip_network(term, strict=False)
            base.append("(src_ip <<= CAST(:q_net AS inet) OR dst_ip <<= CAST(:q_net AS inet) "
                        "OR exporter_ip <<= CAST(:q_net AS inet))")
            params["q_net"] = str(net)
        except ValueError:
            base.append("(title ILIKE :q_like OR description ILIKE :q_like)")
            params["q_like"] = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    where_all = " AND ".join(base)
    count_rows = (await db.execute(text(
        f"SELECT status, severity, kind, count(*) FROM netflow_findings WHERE {where_all} GROUP BY 1, 2, 3"
    ), params)).all()
    counts: dict[str, Any] = {s: 0 for s in det.STATUSES}
    # by_severity counts ACTIVE findings (open + acknowledged), as the
    # Anomalies tiles label it; by_severity_open counts open ones only, for
    # summaries that speak of "open findings" (the overview insight).
    by_severity = {s: 0 for s in det.SEVERITIES}
    by_severity_open = {s: 0 for s in det.SEVERITIES}
    by_kind: dict[str, int] = {}
    for st, sev, k, n in count_rows:
        counts[st] = counts.get(st, 0) + int(n)
        if st in ("open", "acknowledged"):
            if sev in by_severity:
                by_severity[sev] += int(n)
                if st == "open":
                    by_severity_open[sev] += int(n)
            by_kind[k] = by_kind.get(k, 0) + int(n)
    counts["by_severity"] = by_severity
    counts["by_severity_open"] = by_severity_open
    counts["by_kind"] = by_kind
    counts["by_kind_all"] = by_kind_all
    counts["active"] = counts["open"] + counts["acknowledged"]

    where = list(base)
    where.append("status = ANY(:statuses)")
    params["statuses"] = list(STATUS_FILTERS[status])
    if severities:
        where.append("severity = ANY(:sevs)")
        params["sevs"] = severities
    where_sql = " AND ".join(where)
    total = int((await db.execute(text(f"SELECT count(*) FROM netflow_findings WHERE {where_sql}"), params)).scalar() or 0)
    rows: list[dict] = []
    if limit:
        res = await db.execute(text(
            f"SELECT {det.FINDING_COLUMNS} FROM netflow_findings WHERE {where_sql} "
            f"ORDER BY {SORTS[sort]}, id LIMIT :lim OFFSET :off"
        ), {**params, "lim": limit, "off": offset})
        rows = [det.row_dict(r) for r in res.all()]
    users = await _usernames(db, rows)
    items = [det.finding_item(r, users) for r in rows]
    meta.set_truncated(limit, len(items), more=offset + len(items) < total)

    if format == "csv":
        return _csv_response(items, win)

    settings = await det.load_settings(db)
    state = await det.load_state(db)
    now = now_utc()
    visible = _visible(vis, f)
    engine = det.engine_status(state, now, visible)
    evaluated = det.evaluated_seconds(state.get("evaluated"), win.start, min(win.end, now))
    span = max(1, int((min(win.end, now) - win.start).total_seconds()))
    engine["evaluated_pct"] = round(min(100.0, evaluated / span * 100), 1)
    engine["gaps"] = state.get("gaps") or []
    detectors = det.scoped_detectors(state, settings, visible, now)
    enabled = [d for d in detectors if d["status"] != "disabled"]
    complete = (engine["status"] == "running" and bool(enabled)
                and all(d["status"] == "ok" for d in enabled) and engine["evaluated_pct"] >= 99.0)
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "counts": counts,
        "detectors": detectors,
        "engine": engine,
        "complete": complete,
        "window": {"from": _to_iso_utc(win.start), "to": _to_iso_utc(win.end)},
        "quiet_minutes": settings["quiet_minutes"],
    }


CSV_COLUMNS = ("id", "status", "severity", "confidence", "kind", "label", "title", "src", "dst", "exporter",
               "first_seen", "last_seen", "occurrences", "description", "assigned_to", "suppression_reason",
               "evidence_from", "evidence_to")


def _csv_response(items: list[dict], win: Window) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for it in items:
        ev = (it.get("evidence") or {}).get("window") or {}
        w.writerow([
            it["id"], it["status"], it["severity"], it["confidence"], it["kind"], it["label"], it["title"],
            it.get("src") or "", it.get("dst") or "", it.get("exporter") or "", it["first_seen"], it["last_seen"],
            it["occurrences"], it["description"],
            (it.get("assigned_to") or {}).get("username") or "",
            (it.get("suppression") or {}).get("reason") or "", ev.get("from") or "", ev.get("to") or "",
        ])
    name = f"netflow-findings-{win.start:%Y%m%dT%H%MZ}-{win.end:%Y%m%dT%H%MZ}.csv"
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _run_payload(result: det.RunResult, persisted: dict[str, dict], limit: int) -> dict:
    items = [det.candidate_item(c, persisted.get(c.fingerprint)) for c in result.candidates]
    items.sort(key=lambda i: (det.SEVERITY_RANK.get(i["severity"], 0), i["last_seen"] or ""), reverse=True)
    by_severity = {s: 0 for s in det.SEVERITIES}
    for it in items:
        if not it["suppressed"]:
            by_severity[it["severity"]] = by_severity.get(it["severity"], 0) + 1
    detectors = result.detectors()
    return {
        "items": items,
        "counts": {
            "candidate": sum(1 for i in items if not i["suppressed"]),
            "suppressed": sum(1 for i in items if i["suppressed"]),
            "by_severity": by_severity,
            "matches": sum(d["match_count"] for d in detectors),
            "truncated": any(d["truncated"] for d in detectors),
        },
        "detectors": detectors,
        "coverage": result.coverage.as_dict(),
        "complete": result.complete,
        "window": {"from": _to_iso_utc(result.start), "to": _to_iso_utc(result.end)},
        "generated_at": _to_iso_utc(result.ran_at),
        "persisted": False,
    }


async def _persisted_by_fingerprint(db: AsyncSession, fps: list[str]) -> dict[str, dict]:
    if not fps:
        return {}
    res = await db.execute(text("SELECT fingerprint, id::text, status FROM netflow_findings WHERE fingerprint = ANY(:f)"),
                           {"f": fps})
    return {r[0]: {"id": r[1], "status": r[2]} for r in res.all()}


def _kinds_param(kind: str | None) -> list[str] | None:
    kinds = [k.strip() for k in (kind or "").split(",") if k.strip()]
    for k in kinds:
        if k not in det.DETECTOR_BY_KIND:
            raise invalid_filter(f"Unknown detector '{k[:48]}'", {"kind": "Unknown detector"})
    return kinds or None


@router.get("/findings/run")
async def run_findings(
    win: Window = Depends(window_params(24, MAX_SPAN_RAW)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
    kind: str | None = Query(default=None, description="Comma-separated detector kinds (default all)"),
    limit: int = Query(default=det.ADHOC_LIMIT, ge=1, le=200, description="Subjects returned per detector"),
):
    """Evaluate the detectors ad hoc over raw records in the window (≤ 31
    days); nothing is persisted. Matches that an approved exception covers are
    returned as suppressed, never dropped."""
    require_filter_exporters_visible(f, vis)
    f = _drop_unapplied(f, RUN_FILTER_KEYS)
    current_meta().set_source("raw", win.start, win.end, det.BUCKET_S)
    result = await det.run_adhoc(db, win.start, win.end, vis, f, kinds=_kinds_param(kind), limit=limit)
    persisted = await _persisted_by_fingerprint(db, [c.fingerprint for c in result.candidates])
    payload = _run_payload(result, persisted, limit)
    current_meta().set_truncated(limit, len(payload["items"]), more=payload["counts"]["truncated"])
    return payload


@router.get("/anomalies")
async def netflow_anomalies(
    win: Window = Depends(window_params(24, MAX_SPAN_RAW)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Legacy shape ``{total, findings, generated_at}`` for existing consumers,
    now an ad-hoc detector run: ``total`` counts every match (not the
    returned subset), and ``detectors``/``complete`` say which checks ran."""
    require_filter_exporters_visible(f, vis)
    f = _drop_unapplied(f, RUN_FILTER_KEYS)
    current_meta().set_source("raw", win.start, win.end, det.BUCKET_S)
    result = await det.run_adhoc(db, win.start, win.end, vis, f)
    persisted = await _persisted_by_fingerprint(db, [c.fingerprint for c in result.candidates])
    payload = _run_payload(result, persisted, det.ADHOC_LIMIT)
    findings = [i for i in payload["items"] if not i["suppressed"]]
    return {
        "total": sum(d["match_count"] for d in payload["detectors"]) - payload["counts"]["suppressed"],
        "returned": len(findings),
        "findings": findings,
        "suppressed": [i for i in payload["items"] if i["suppressed"]],
        "detectors": payload["detectors"],
        "complete": payload["complete"],
        "coverage": payload["coverage"],
        "window": payload["window"],
        "generated_at": payload["generated_at"],
    }


@router.get("/findings/{finding_id}")
async def get_finding(
    finding_id: uuid.UUID,
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    d = await _load_finding(db, finding_id, vis)
    item = det.finding_item(d, await _usernames(db, [d]))
    notes = list(d.get("notes") or [])
    item["notes"] = notes
    spec = det.DETECTOR_BY_KIND.get(d.get("kind") or "")
    item["detector"] = {"kind": spec.kind, "label": spec.label, "description": spec.description,
                        "method": spec.method, "required_fields": list(spec.required)} if spec else None
    item["permissions"] = {"acknowledge": can_acknowledge_findings(vis), "suppress": vis.can(MANAGE_PERMISSION)}
    current_meta().set_source("pg")
    return item


@router.get("/findings/{finding_id}/timeline")
async def finding_timeline(
    finding_id: uuid.UUID,
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
):
    """Evidence series of a finding from raw records: its detector filter over
    the evidence window (the last 24 hours of it at most, padded by 15
    minutes), bucketed by minute up to 3 hours, 5 minutes up to 24 hours."""
    from .common import resolve_window

    d = await _load_finding(db, finding_id, vis)
    evidence = d.get("evidence") or {}
    ev_filter = evidence.get("filter") or {}
    try:
        f = parse_flow_filter({k: str(v) for k, v in ev_filter.items() if v not in (None, "")})
    except Exception:
        f = FlowFilter(exporter=det._inet(d.get("exporter_ip")))
    if from_ts or to_ts:
        w = resolve_window(None, None, from_ts, to_ts, None, max_span=timedelta(days=7))
        start, end = w.start, w.end
    else:
        window = evidence.get("window") or {}
        ev_from = det._parse(window["from"]) if window.get("from") else det._aware(d["first_seen"])
        ev_to = det._parse(window["to"]) if window.get("to") else det._aware(d["last_seen"])
        start = max(ev_from, ev_to - timedelta(hours=24)) - timedelta(minutes=15)
        end = min(ev_to + timedelta(minutes=15), now_utc())
        if end <= start:
            end = start + timedelta(minutes=30)
    span = (end - start).total_seconds()
    bucket_s = 60 if span <= 3 * 3600 else 300 if span <= 24 * 3600 else 3600
    start = floor_to(start, bucket_s)
    pred, params = compile_filter(f, RAW, vis)
    params.update(t0=start, t1=end, bs=bucket_s)
    sql_text = f"""
        SELECT toStartOfInterval(toDateTime(timestamp, 'UTC'), toIntervalSecond(%(bs)s)) AS t,
               count() AS records, sum(bytes) AS vol, sum(packets) AS pk,
               uniqExact({DST}) AS dsts, uniqExact({SRC}) AS srcs, uniqExact(dst_port) AS dports,
               countIf(protocol = 6 AND bitAnd(tcp_flags, 4) != 0) AS rsts
        FROM {table_ref(RAW)}
        WHERE timestamp >= %(t0)s AND timestamp < %(t1)s AND {pred}
        GROUP BY t ORDER BY t
    """
    res = await run_query(sql_text, params, budget_s=BUDGET_FORENSICS, label="finding-timeline")
    by_t = {det._aware(r[0]): r for r in res.result_rows}
    delivered = await _delivery_by_bucket(f, vis, start, end, bucket_s)
    points = []
    t = start
    while t < end:
        r = by_t.get(t)
        secs = max(1.0, (min(t + timedelta(seconds=bucket_s), end) - t).total_seconds())
        covered = None if delivered is None else delivered.get(t, 0) > 0
        if r is None and covered is False:
            # Nothing was received from the exporter: unknown, not zero (NF-03).
            points.append({"t": _to_iso_utc(t), "covered": False, "records": None, "bytes": None, "packets": None,
                           "pps": None, "bps": None, "dsts": None, "srcs": None, "dports": None, "rsts": None})
        elif r is None:
            points.append({"t": _to_iso_utc(t), "covered": covered, "records": 0, "bytes": 0, "packets": 0,
                           "pps": 0.0, "bps": 0.0, "dsts": 0, "srcs": 0, "dports": 0, "rsts": 0})
        else:
            points.append({"t": _to_iso_utc(t), "covered": True, "records": int(r[1]), "bytes": int(r[2]), "packets": int(r[3]),
                           "pps": round(int(r[3]) / secs, 2), "bps": round(int(r[2]) * 8 / secs, 1),
                           "dsts": int(r[4]), "srcs": int(r[5]), "dports": int(r[6]), "rsts": int(r[7])})
        t += timedelta(seconds=bucket_s)
    current_meta().set_source("raw", start, end, bucket_s)
    return {
        "finding_id": str(d["id"]),
        "window": {"from": _to_iso_utc(start), "to": _to_iso_utc(end)},
        "bucket_s": bucket_s,
        "filter": f.canonical(),
        "points": points,
        "segments": evidence.get("segments") or [],
        "metric": _timeline_metric(d.get("kind") or ""),
        "threshold": ((d.get("metrics") or {}).get("threshold")),
    }


async def _delivery_by_bucket(f: FlowFilter, vis: FlowVisibility, start: datetime, end: datetime,
                              bucket_s: int) -> dict[datetime, int] | None:
    """Minutes with delivered records per timeline bucket for the finding's
    exporter (``flow_exporter_minutes``, appliance receive clock); None when
    delivery was not tracked for the whole range (coverage unknown)."""
    from .common import EXPORTER_MINUTES, get_retention

    try:
        retention = await get_retention()
        earliest = retention.earliest.get(EXPORTER_MINUTES)
        if earliest is None or det._aware(earliest) > start:
            return None
        where, params = compile_filter(FlowFilter(exporter=f.exporter), EXPORTER_MINUTES, vis)
        params.update(c0=start, c1=end, bs=bucket_s)
        res = await run_query(
            f"SELECT toStartOfInterval(minute, toIntervalSecond(%(bs)s)) AS t, uniqExactIf(minute, records > 0) "
            f"FROM {table_ref(EXPORTER_MINUTES)} WHERE minute >= %(c0)s AND minute < %(c1)s AND {where} GROUP BY t",
            params, budget_s=BUDGET_FORENSICS, label="finding-timeline-coverage")
    except Exception:  # noqa: BLE001 - coverage is a refinement; the series stands without it
        return None
    return {det._aware(r[0]): int(r[1]) for r in res.result_rows}


def _timeline_metric(kind: str) -> dict:
    return {
        "syn_scan": {"key": "dsts", "label": "Distinct destinations"},
        "icmp_sweep": {"key": "dsts", "label": "Distinct destinations"},
        "port_scan": {"key": "dports", "label": "Distinct destination ports"},
        "rst_flood": {"key": "rsts", "label": "Records with RST"},
        "icmp_flood": {"key": "pps", "label": "Packets per second"},
        "icmp_flood_target": {"key": "pps", "label": "Packets per second"},
    }.get(kind, {"key": "bps", "label": "Bits per second"})


# ── Lifecycle ────────────────────────────────────────────────────────────────

class FindingAction(BaseModel):
    # Unknown keys are refused: {"action": "assign", "assignee": ...} must not
    # silently un-assign the finding.
    model_config = ConfigDict(extra="forbid")

    action: Literal["acknowledge", "assign", "suppress", "unsuppress", "resolve", "reopen", "note"]
    note: str | None = Field(default=None, max_length=4000)
    reason: str | None = Field(default=None, max_length=500)
    until: datetime | None = None
    hours: int | None = Field(default=None, ge=1, le=24 * 365)
    user_id: uuid.UUID | None = None


class BulkAction(FindingAction):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


MAX_SUPPRESS = timedelta(days=365)


async def _apply_action(db: AsyncSession, d: dict, body: FindingAction, user: User, vis: FlowVisibility) -> dict:
    """Validate and apply one lifecycle action to a locked finding row;
    returns the audit metadata. Raises 400/403/409."""
    action = body.action
    status = d["status"]
    now = now_utc()
    sets: dict[str, Any] = {}
    notes = list(d.get("notes") or [])
    meta: dict[str, Any] = {"from_status": status}
    if action in ("suppress", "unsuppress"):
        _require_manage(vis, "suppressing findings")
    else:
        _require_ack(vis)
    if action == "acknowledge":
        if status != "open":
            raise HTTPException(status_code=409, detail=f"Only open findings can be acknowledged (this one is {status})")
        sets.update(status="acknowledged", acknowledged_by=user.id, acknowledged_at=now)
        notes.append(_actor_note(user, "Acknowledged" + (f": {body.note}" if body.note else "")))
    elif action == "assign":
        if body.user_id is not None:
            target = await _assignable_user(db, body.user_id)
            if target is None:
                raise HTTPException(status_code=400, detail="The user to assign does not exist or is inactive")
            if not await _user_can_see_finding(db, target, d):
                raise HTTPException(status_code=400, detail=(
                    "The user to assign cannot see this finding: they need the netflow.view permission "
                    "and the finding's exporter in their scope"))
            exists = target.username
            notes.append(_actor_note(user, f"Assigned to {exists}"))
            meta["assigned_to"] = str(body.user_id)
        else:
            notes.append(_actor_note(user, "Unassigned"))
        sets["assigned_to"] = body.user_id
    elif action == "suppress":
        reason = (body.reason or "").strip()
        if not reason:
            raise HTTPException(status_code=400, detail="A reason is required to suppress a finding")
        if body.until is not None:
            until = body.until if body.until.tzinfo else body.until.replace(tzinfo=timezone.utc)
        elif body.hours is not None:
            until = now + timedelta(hours=body.hours)
        else:
            raise HTTPException(status_code=400, detail="Suppression needs an expiry (until or hours)")
        if until <= now or until - now > MAX_SUPPRESS:
            raise HTTPException(status_code=400, detail="The suppression expiry must be in the next 365 days")
        if status == "resolved":
            raise HTTPException(status_code=409, detail="A resolved finding cannot be suppressed; reopen it first")
        sets.update(status="suppressed", suppressed_until=until, suppressed_by=user.id, suppress_reason=reason)
        notes.append(_actor_note(user, f"Suppressed until {_to_iso_utc(until)}: {reason}"))
        meta.update(until=_to_iso_utc(until), reason=reason)
    elif action == "unsuppress":
        if status != "suppressed":
            raise HTTPException(status_code=409, detail="The finding is not suppressed")
        sets.update(status="open", suppressed_until=None, suppressed_by=None, suppress_reason=None)
        notes.append(_actor_note(user, "Suppression lifted" + (f": {body.note}" if body.note else "")))
    elif action == "resolve":
        if status == "resolved":
            raise HTTPException(status_code=409, detail="The finding is already resolved")
        if status == "suppressed" and not vis.can(MANAGE_PERMISSION):
            raise _forbid("Resolving a suppressed finding needs the netflow.manage permission")
        sets.update(status="resolved", resolved_at=now)
        notes.append(_actor_note(user, "Resolved" + (f": {body.note}" if body.note else "")))
    elif action == "reopen":
        if status == "open":
            raise HTTPException(status_code=409, detail="The finding is already open")
        if status == "suppressed":
            _require_manage(vis, "lifting a suppression")
        sets.update(status="open", resolved_at=None, acknowledged_by=None, acknowledged_at=None,
                    suppressed_until=None, suppressed_by=None, suppress_reason=None)
        notes.append(_actor_note(user, "Reopened" + (f": {body.note}" if body.note else "")))
    elif action == "note":
        text_ = (body.note or "").strip()
        if not text_:
            raise HTTPException(status_code=400, detail="The note is empty")
        notes.append(_actor_note(user, text_, kind="note"))
        meta["note"] = text_[:200]
    assignments = ", ".join(f"{k} = :{k}" for k in sets)
    params = {**sets, "id": d["id"], "notes": json.dumps(notes[-200:])}
    await db.execute(text(
        f"UPDATE netflow_findings SET {assignments + ', ' if assignments else ''}"
        "notes = CAST(:notes AS jsonb), updated_at = NOW() WHERE id = :id"
    ), params)
    meta["to_status"] = sets.get("status", status)
    return meta


@router.post("/findings/bulk")
async def bulk_findings(
    body: BulkAction,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_triage),
    db: AsyncSession = Depends(get_db),
):
    """Apply one action to several findings; each is validated on its own
    and reported (``done`` / ``skipped`` with the reason)."""
    if body.action == "assign" or body.action == "note":
        raise HTTPException(status_code=400, detail="Assign and note apply to one finding at a time")
    done, skipped = [], []
    for fid in dict.fromkeys(body.ids):
        try:
            d = await _load_finding(db, fid, vis, lock=True)
            meta = await _apply_action(db, d, body, user, vis)
        except HTTPException as exc:
            if exc.status_code == 403:
                await db.rollback()
                raise
            skipped.append({"id": str(fid), "reason": str(exc.detail)})
            continue
        await write_audit_log(db, actor=user, action=f"netflow.finding.{body.action}",
                              resource_type="netflow_finding", resource_id=str(fid), metadata={**meta, "bulk": True})
        done.append(str(fid))
    await db.commit()
    return {"done": done, "skipped": skipped}


@router.post("/findings/{finding_id}/actions")
async def finding_action(
    finding_id: uuid.UUID,
    body: FindingAction,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_triage),
    db: AsyncSession = Depends(get_db),
):
    d = await _load_finding(db, finding_id, vis, lock=True)
    meta = await _apply_action(db, d, body, user, vis)
    await write_audit_log(db, actor=user, action=f"netflow.finding.{body.action}",
                          resource_type="netflow_finding", resource_id=str(finding_id), metadata=meta)
    await db.commit()
    return await get_finding(finding_id, vis, db)


_ASSIGNEE_COLUMNS = "id, username, full_name, role, scope_tags"


async def _assignable_user(db: AsyncSession, user_id: uuid.UUID) -> Any:
    return (await db.execute(text(f"SELECT {_ASSIGNEE_COLUMNS} FROM users WHERE id = :u AND is_active"),
                             {"u": user_id})).first()


async def _user_visibility(db: AsyncSession, row: Any) -> FlowVisibility:
    return await visibility_for_user(db, SimpleNamespace(id=row.id, role=row.role, scope_tags=row.scope_tags))


async def _user_can_see_finding(db: AsyncSession, row: Any, finding: dict) -> bool:
    """A finding may only be assigned to someone who can open it: the
    netflow.view permission and the finding's exporter in their scope."""
    tv = await _user_visibility(db, row)
    if not tv.can(VIEW_PERMISSION):
        return False
    exporter = det._inet(finding.get("exporter_ip"))
    return tv.exporters is None or (exporter is not None and tv.allows(exporter))


@router.get("/findings-assignees")
async def finding_assignees(
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
    finding: uuid.UUID | None = Query(default=None, description="Only users who can see this finding"),
):
    """Active users a finding can be assigned to: holders of netflow.view who
    can see the finding's exporter (``finding``), or -- without one -- who
    share at least one exporter with a restricted caller. The directory of
    users outside that is not disclosed."""
    _require_ack(vis)
    target = await _load_finding(db, finding, vis) if finding is not None else None
    res = await db.execute(text(
        f"SELECT {_ASSIGNEE_COLUMNS} FROM users WHERE is_active ORDER BY lower(username) LIMIT 2000"
    ))
    out = []
    for r in res.all():
        tv = await _user_visibility(db, r)
        if not tv.can(VIEW_PERMISSION):
            continue
        if target is not None:
            if not await _user_can_see_finding(db, r, target):
                continue
        elif vis.exporters is not None and (tv.exporters is not None and not (tv.exporters & vis.exporters)):
            continue
        out.append({"id": str(r.id), "username": r.username, "full_name": r.full_name})
        if len(out) >= 1000:
            break
    current_meta().set_source("pg")
    return out


# ── Detector settings ────────────────────────────────────────────────────────

@router.get("/detectors")
async def get_detectors(
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Detector registry, current thresholds and the engine's last status."""
    settings = await det.load_settings(db)
    state = await det.load_state(db)
    now = now_utc()
    payload = det.settings_payload(settings)
    status = {d["kind"]: d for d in det.scoped_detectors(state, settings, vis.exporters, now)}
    for d in payload["detectors"]:
        s = status.get(d["kind"]) or {}
        d.update(status=s.get("status"), reason=s.get("reason"), last_run_at=s.get("last_run_at"),
                 last_ok_at=s.get("last_ok_at"), notes=s.get("notes") or [],
                 exporters_unavailable=s.get("exporters_unavailable") or [],
                 reduced_sensitivity=s.get("reduced_sensitivity") or [])
    payload["engine"] = det.engine_status(state, now, vis.exporters)
    payload["can_manage"] = vis.can(MANAGE_PERMISSION)
    current_meta().set_source("pg")
    return payload


@router.put("/detectors")
async def put_detectors(
    request: Request,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    """Update thresholds / enable flags (partial: only what is sent changes)."""
    _require_manage(vis, "changing detector settings")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="The body must be JSON") from None
    before = await det.load_settings(db)
    try:
        after = await det.save_settings(db, body, user.id)
    except det.SettingsError as exc:
        raise invalid_filter(str(exc), exc.fields) from None
    changes = _settings_diff(before, after)
    await write_audit_log(db, actor=user, action="netflow.detectors.update", resource_type="system_settings",
                          resource_id=det.SETTINGS_KEY, metadata={"changes": changes})
    await db.commit()
    return await get_detectors(vis, db)


def _settings_diff(before: dict, after: dict) -> list[dict]:
    out = []
    if before.get("quiet_minutes") != after.get("quiet_minutes"):
        out.append({"key": "quiet_minutes", "from": before.get("quiet_minutes"), "to": after.get("quiet_minutes")})
    for kind, values in after["detectors"].items():
        old = before["detectors"].get(kind, {})
        for k, v in values.items():
            if old.get(k) != v:
                out.append({"key": f"{kind}.{k}", "from": old.get(k), "to": v})
    return out


# ── Exceptions ───────────────────────────────────────────────────────────────
# Exporter scope (migrate-140): ``exporter_ips`` NULL = every exporter (a
# global exception; only an unrestricted manager creates one), otherwise the
# exception matches findings from those exporters only. A tag-scoped manager's
# exception is bound at creation to the exporters it may see (or the subset it
# names) and is never widened: PATCH changes the reason and expiry only.
# Reading: a scoped caller lists the exceptions whose scope intersects its
# exporters, plus the global ones. Those are policy that applies to the
# caller's own exporters, so their match criteria (CIDRs, port) are shown
# unmasked: they describe a pattern an administrator approved, not flows seen
# on another team's exporters, and a team must be able to tell why findings
# on its own exporters are suppressed. Of a scope, a caller sees only its own
# exporters (and that others exist). Changing or removing an exception needs
# its whole scope inside the caller's: out of sight is 404; global or partly
# foreign is 403 (it would lift suppression on other teams' exporters).

MAX_EXCEPTION_EXPORTERS = 500


class ExceptionIn(BaseModel):
    kind: str | None = Field(default=None, max_length=48)
    src_cidr: str | None = Field(default=None, max_length=64)
    dst_cidr: str | None = Field(default=None, max_length=64)
    dst_port: int | None = Field(default=None, ge=0, le=65535)
    reason: str = Field(min_length=1, max_length=500)
    expires_at: datetime | None = None
    apply_to_existing: bool = True
    # Limit the exception to these exporters. Omitted: every exporter for an
    # unrestricted manager, every exporter in scope for a tag-scoped one.
    exporters: list[str] | None = Field(default=None, max_length=MAX_EXCEPTION_EXPORTERS)


class ExceptionPatch(BaseModel):
    reason: str | None = Field(default=None, min_length=1, max_length=500)
    expires_at: datetime | None = None
    clear_expiry: bool = False


def _cidr(value: str | None, key: str) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        return str(ipaddress.ip_network(value.strip(), strict=False))
    except ValueError:
        raise invalid_filter(f"{key} must be an IP address or CIDR", {key: "Not an IP address or CIDR"}) from None


def _ip_key(ip: str) -> tuple:
    addr = ipaddress.ip_address(ip)
    return (addr.version, addr)


def _exception_can_change(vis: FlowVisibility, scope: frozenset[str] | None) -> bool:
    """Whether the caller may change or remove an exception with this scope:
    netflow.manage, and the whole scope inside its own (a global exception
    only for an unrestricted manager)."""
    if not vis.can(MANAGE_PERMISSION):
        return False
    if vis.exporters is None:
        return True
    return scope is not None and scope <= vis.exporters


def _exception_item(r: Any, users: dict[str, str] | None = None, vis: FlowVisibility | None = None) -> dict:
    """One exception as the caller may see it (``vis`` None = everything,
    e.g. for the audit log)."""
    d = dict(r._mapping)
    now = datetime.now(timezone.utc)
    exp = d.get("expires_at")
    scope = det.exception_exporters(d.get("exporter_ips"))
    if scope is None:
        exporters, outside = None, False
    else:
        shown = [e for e in scope if vis is None or vis.allows(e)]
        exporters, outside = sorted(shown, key=_ip_key), len(shown) < len(scope)
    return {
        "id": str(d["id"]), "kind": d.get("kind"),
        "kind_label": det.DETECTOR_BY_KIND[d["kind"]].label if d.get("kind") in det.DETECTOR_BY_KIND else None,
        "src_cidr": d.get("src_cidr"), "dst_cidr": d.get("dst_cidr"), "dst_port": d.get("dst_port"),
        "reason": d["reason"], "expires_at": _to_iso_utc(exp),
        "expired": bool(exp and det._aware(exp) <= now),
        "created_by": ({"id": str(d["created_by"]), "username": (users or {}).get(str(d["created_by"]))}
                       if d.get("created_by") else None),
        "created_at": _to_iso_utc(d.get("created_at")),
        "matches": int(d.get("matches") or 0),
        # Exporter scope: "global" (every exporter) or "exporters" (the ones
        # listed; ``exporters_outside_scope`` when it also covers exporters
        # the caller cannot see, which are never named).
        "scope": "global" if scope is None else "exporters",
        "exporters": exporters,
        "exporters_outside_scope": outside,
        "can_change": True if vis is None else _exception_can_change(vis, scope),
    }


def _exception_select(vis: FlowVisibility | None = None) -> tuple[str, dict]:
    """The exception rows with ``matches``: findings the exception
    suppressed, counted over the exporters the caller may see only."""
    scope, params = "", {}
    if vis is not None and vis.exporters is not None:
        if vis.exporters:
            scope = " AND nf.exporter_ip = ANY(CAST(:x_vis AS inet[]))"
            params["x_vis"] = sorted(vis.exporters)
        else:
            scope = " AND FALSE"
    return (
        "SELECT x.id, x.kind, x.src_cidr::text AS src_cidr, x.dst_cidr::text AS dst_cidr, x.dst_port, x.reason, "
        "x.expires_at, x.created_by, x.created_at, x.exporter_ips::text[] AS exporter_ips, "
        "(SELECT count(*) FROM netflow_findings nf WHERE nf.metrics->>'exception_id' = x.id::text"
        f"{scope}) AS matches "
        "FROM netflow_detector_exceptions x"
    ), params


def _exception_visible_where(vis: FlowVisibility) -> tuple[str, dict]:
    """The exceptions a caller may see: global ones, and those whose
    exporter scope intersects the caller's exporters."""
    if vis.exporters is None:
        return "TRUE", {}
    if not vis.exporters:
        return "x.exporter_ips IS NULL", {}
    return ("(x.exporter_ips IS NULL OR x.exporter_ips && CAST(:x_scope AS inet[]))",
            {"x_scope": sorted(vis.exporters)})


EXCEPTION_SELECT = _exception_select()[0]


async def _exception_for_change(db: AsyncSession, exception_id: uuid.UUID, vis: FlowVisibility, what: str) -> Any:
    """The exception row a write may touch: 403 without netflow.manage, 404
    when it is not there or out of the caller's sight, 403 when its scope
    reaches beyond the caller's exporters (a global one included)."""
    _require_manage(vis, what)
    row = (await db.execute(text(EXCEPTION_SELECT + " WHERE x.id = :id"), {"id": exception_id})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Exception not found")
    scope = det.exception_exporters(row._mapping.get("exporter_ips"))
    if vis.exporters is not None:
        if scope is not None and not scope & vis.exporters:
            raise HTTPException(status_code=404, detail="Exception not found")
        if not _exception_can_change(vis, scope):
            raise _forbid("This exception also applies to exporters outside your scope; "
                          "ask an administrator to change or remove it")
    return row


def _new_exception_scope(vis: FlowVisibility, requested: list[str] | None) -> list[str] | None:
    """The exporter scope a new exception is stored with (None = every
    exporter). A tag-scoped caller's is always bound to exporters it may
    see; named exporters must be in its scope."""
    if requested:
        out: set[str] = set()
        for raw in requested:
            text_ = str(raw or "").strip()
            ip = canonical_ip(text_) if "/" not in text_ else None
            if ip is None:
                raise invalid_filter(f"'{text_[:64]}' is not an exporter address",
                                     {"exporters": "Exporter IP addresses only"})
            if not vis.allows(ip):
                raise invalid_filter(f"Exporter {ip} is not one you can see",
                                     {"exporters": f"{ip} is not in your scope"})
            out.add(ip)
        return sorted(out, key=_ip_key)
    if vis.exporters is None:
        return None
    if not vis.exporters:
        raise invalid_filter("Your scope includes no flow exporters, so an exception would apply to nothing",
                             {"exporters": "No exporters in your scope"})
    return sorted(vis.exporters, key=_ip_key)


@router.get("/detector-exceptions")
async def list_exceptions(
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    select_sql, params = _exception_select(vis)
    where, where_params = _exception_visible_where(vis)
    rows = (await db.execute(text(f"{select_sql} WHERE {where} ORDER BY x.created_at DESC"),
                             {**params, **where_params})).all()
    ids = [{"created_by": r._mapping["created_by"]} for r in rows]
    users = await _usernames(db, [{"assigned_to": i["created_by"]} for i in ids])
    current_meta().set_source("pg")
    return {"items": [_exception_item(r, users, vis) for r in rows], "can_manage": vis.can(MANAGE_PERMISSION),
            # A tag-scoped caller's new exception applies to its exporters
            # only: how many that is (None = unrestricted).
            "scope_exporters": None if vis.exporters is None else len(vis.exporters)}


@router.post("/detector-exceptions", status_code=201)
async def create_exception(
    body: ExceptionIn,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    """Approve a pattern (e.g. a monitoring host's pings). Matches stay
    visible as suppressed findings with this reason."""
    _require_manage(vis, "adding detector exceptions")
    if body.kind is not None and body.kind not in det.DETECTOR_BY_KIND:
        raise invalid_filter(f"Unknown detector '{body.kind}'", {"kind": "Unknown detector"})
    src, dst = _cidr(body.src_cidr, "src_cidr"), _cidr(body.dst_cidr, "dst_cidr")
    if src is None and dst is None and body.dst_port is None:
        raise invalid_filter("An exception needs a source, destination or port — it cannot match everything",
                             {"src_cidr": "Give a source, destination or port"})
    now = now_utc()
    expires = body.expires_at
    if expires is not None:
        expires = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            raise invalid_filter("The expiry must be in the future", {"expires_at": "Must be in the future"})
    reason = body.reason.strip()
    if not reason:
        raise invalid_filter("A reason is required", {"reason": "Required"})
    scope = _new_exception_scope(vis, body.exporters)
    new_id = (await db.execute(text(
        "INSERT INTO netflow_detector_exceptions "
        "(kind, src_cidr, dst_cidr, dst_port, reason, expires_at, exporter_ips, created_by) "
        "VALUES (:kind, CAST(:src AS cidr), CAST(:dst AS cidr), :port, :reason, :exp, CAST(:exporters AS inet[]), :by) "
        "RETURNING id"
    ), {"kind": body.kind, "src": src, "dst": dst, "port": body.dst_port, "reason": reason, "exp": expires,
        "exporters": scope, "by": user.id})).scalar()
    applied = 0
    if body.apply_to_existing:
        rule = det.ExceptionRule(
            id=str(new_id), kind=body.kind, src=ipaddress.ip_network(src) if src else None,
            dst=ipaddress.ip_network(dst) if dst else None, dst_port=body.dst_port, reason=reason, expires_at=expires,
            exporters=None if scope is None else frozenset(scope),
        )
        applied = await det.suppress_matching(db, rule, now)
    await write_audit_log(db, actor=user, action="netflow.exception.create", resource_type="netflow_detector_exception",
                          resource_id=str(new_id), metadata={"kind": body.kind, "src_cidr": src, "dst_cidr": dst,
                                                             "dst_port": body.dst_port, "reason": reason,
                                                             "expires_at": _to_iso_utc(expires),
                                                             "exporters": scope,
                                                             "suppressed_findings": applied})
    await db.commit()
    det._adhoc_cache.clear()
    select_sql, params = _exception_select(vis)
    row = (await db.execute(text(select_sql + " WHERE x.id = :id"), {**params, "id": new_id})).first()
    item = _exception_item(row, {str(user.id): user.username}, vis)
    # A restricted manager is told about the findings it can see only.
    return {**item, "suppressed_findings": applied if vis.exporters is None else item["matches"]}


@router.patch("/detector-exceptions/{exception_id}")
async def update_exception(
    exception_id: uuid.UUID,
    body: ExceptionPatch,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    await _exception_for_change(db, exception_id, vis, "changing detector exceptions")
    sets, params = [], {"id": exception_id}
    if body.reason is not None:
        if not body.reason.strip():
            raise invalid_filter("A reason is required", {"reason": "Required"})
        sets.append("reason = :reason")
        params["reason"] = body.reason.strip()
    if body.clear_expiry:
        sets.append("expires_at = NULL")
    elif body.expires_at is not None:
        exp = body.expires_at if body.expires_at.tzinfo else body.expires_at.replace(tzinfo=timezone.utc)
        sets.append("expires_at = :exp")
        params["exp"] = exp
    if sets:
        await db.execute(text(f"UPDATE netflow_detector_exceptions SET {', '.join(sets)} WHERE id = :id"), params)
        await write_audit_log(db, actor=user, action="netflow.exception.update",
                              resource_type="netflow_detector_exception", resource_id=str(exception_id),
                              metadata={k: (_to_iso_utc(v) if isinstance(v, datetime) else v)
                                        for k, v in params.items() if k != "id"} | {"clear_expiry": body.clear_expiry})
        await db.commit()
        det._adhoc_cache.clear()
    select_sql, params = _exception_select(vis)
    row = (await db.execute(text(select_sql + " WHERE x.id = :id"), {**params, "id": exception_id})).first()
    creator = row._mapping.get("created_by") if row is not None else None
    return _exception_item(row, await _usernames(db, [{"assigned_to": creator}]) if creator else None, vis)


@router.delete("/detector-exceptions/{exception_id}")
async def delete_exception(
    exception_id: uuid.UUID,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    """Remove an exception; findings it suppressed return to open."""
    row = await _exception_for_change(db, exception_id, vis, "removing detector exceptions")
    note = json.dumps([_actor_note(user, "Exception removed; suppression lifted")])
    reopened = [r[0] for r in (await db.execute(text(
        "UPDATE netflow_findings SET status = 'open', suppressed_until = NULL, suppressed_by = NULL, "
        "suppress_reason = NULL, notes = notes || CAST(:note AS jsonb), updated_at = NOW() "
        "WHERE status = 'suppressed' AND metrics->>'exception_id' = :id RETURNING host(exporter_ip)"
    ), {"note": note, "id": str(exception_id)})).all()]
    await db.execute(text("DELETE FROM netflow_detector_exceptions WHERE id = :id"), {"id": exception_id})
    item = {k: v for k, v in _exception_item(row).items() if k not in ("can_change", "exporters_outside_scope")}
    await write_audit_log(db, actor=user, action="netflow.exception.delete", resource_type="netflow_detector_exception",
                          resource_id=str(exception_id), metadata={**item, "reopened_findings": len(reopened)})
    await db.commit()
    det._adhoc_cache.clear()
    # A restricted manager is told about the findings it can see only.
    visible_reopened = sum(1 for ip in reopened if ip and vis.allows(ip))
    return {"deleted": str(exception_id),
            "reopened_findings": len(reopened) if vis.exporters is None else visible_reopened}


# ── Threat intelligence ──────────────────────────────────────────────────────

class FeedIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source: Literal["url", "upload"]
    url: str | None = Field(default=None, max_length=2000)
    category: str = Field(default="malicious", max_length=48)
    enabled: bool = False
    refresh_hours: int = Field(default=24, ge=1, le=8760)
    content: str | None = Field(default=None, max_length=ti.MAX_UPLOAD_CHARS)


class FeedPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=48)
    enabled: bool | None = None
    refresh_hours: int | None = Field(default=None, ge=1, le=8760)


class FeedUpload(BaseModel):
    content: str = Field(min_length=1, max_length=ti.MAX_UPLOAD_CHARS)


def _check_category(value: str) -> str:
    v = value.strip().lower()
    if v not in ti.CATEGORIES:
        raise invalid_filter(f"Unknown category '{value[:48]}'", {"category": f"One of {', '.join(ti.CATEGORIES)}"})
    return v


def _check_url(value: str | None) -> str:
    v = (value or "").strip()
    if not v.lower().startswith(("http://", "https://")):
        raise invalid_filter("A feed URL must start with http:// or https://", {"url": "http(s) URL required"})
    try:
        ti.check_url_literal(v)      # internal addresses are refused again at every fetch
    except ti.FetchRefused as exc:
        raise invalid_filter(f"The feed URL is not allowed: {exc}", {"url": str(exc)}) from None
    return v


async def _feed_row(db: AsyncSession, feed_id: uuid.UUID) -> Any:
    row = (await db.execute(text(f"SELECT {ti.FEED_COLUMNS} FROM netflow_ti_feeds WHERE id = :id"),
                            {"id": feed_id})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    return row


@router.get("/ti/feeds")
async def list_feeds(
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(text(f"SELECT {ti.FEED_COLUMNS} FROM netflow_ti_feeds ORDER BY lower(name)"))).all()
    feeds = [ti.feed_item(r) for r in rows]
    # Active threat-intel findings per feed, in the caller's scope: what each
    # feed is currently flagging (the findings themselves are on the Findings tab).
    params: dict[str, Any] = {}
    where = ["kind = 'threat_intel'", "status IN ('open', 'acknowledged')", *_scope_sql(vis, FlowFilter(), params)]
    counts = {
        r[0]: int(r[1]) for r in (await db.execute(text(
            "SELECT f->>'id', count(DISTINCT nf.id) FROM netflow_findings nf, "
            "jsonb_array_elements(CASE WHEN jsonb_typeof(nf.metrics->'feeds') = 'array' "
            "THEN nf.metrics->'feeds' ELSE '[]'::jsonb END) f "
            f"WHERE {' AND '.join(where)} GROUP BY 1"
        ), params)).all()
    }
    for item in feeds:
        item["active_findings"] = counts.get(item["id"], 0)
    index = await ti.get_index(db)
    current_meta().set_source("pg")
    return {
        "items": feeds,
        "catalog": list(ti.CATALOG),
        "categories": list(ti.CATEGORIES),
        "index": {"prefixes": index.prefixes, "feeds": len(index.feeds)},
        "can_manage": vis.can(MANAGE_PERMISSION),
        "builtin": None,
        "builtin_note": "No reputation data ships with ZenPlus; add a URL feed from the catalog or upload a list.",
    }


@router.post("/ti/feeds", status_code=201)
async def create_feed(
    body: FeedIn,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    _require_manage(vis, "adding threat-intel feeds")
    name = body.name.strip()
    category = _check_category(body.category)
    url = _check_url(body.url) if body.source == "url" else None
    if body.source == "upload" and not (body.content or "").strip():
        raise invalid_filter("An uploaded feed needs its list content", {"content": "Required"})
    exists = (await db.execute(text("SELECT 1 FROM netflow_ti_feeds WHERE lower(name) = lower(:n)"), {"n": name})).first()
    if exists:
        raise HTTPException(status_code=409, detail=f"A feed named '{name}' already exists")
    feed_id = (await db.execute(text(
        "INSERT INTO netflow_ti_feeds (name, source, url, category, enabled, refresh_hours, created_by, updated_by) "
        "VALUES (:n, :s, :u, :c, :e, :r, :by, :by) RETURNING id"
    ), {"n": name, "s": body.source, "u": url, "c": category, "e": body.enabled, "r": body.refresh_hours,
        "by": user.id})).scalar()
    result = None
    if body.source == "upload":
        result = await ti.refresh_feed(db, feed_id, content=body.content)
    elif body.enabled:
        result = await ti.refresh_feed(db, feed_id)
    await write_audit_log(db, actor=user, action="netflow.ti_feed.create", resource_type="netflow_ti_feed",
                          resource_id=str(feed_id), metadata={"name": name, "source": body.source, "url": url,
                                                              "category": category, "enabled": body.enabled,
                                                              "result": (result or {}).get("status")})
    await db.commit()
    ti.invalidate_index()
    det._adhoc_cache.clear()
    return {**ti.feed_item(await _feed_row(db, feed_id)), "result": result}


@router.patch("/ti/feeds/{feed_id}")
async def update_feed(
    feed_id: uuid.UUID,
    body: FeedPatch,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    _require_manage(vis, "changing threat-intel feeds")
    row = await _feed_row(db, feed_id)
    before = ti.feed_item(row)
    sets, params = [], {"id": feed_id, "by": user.id}
    if body.name is not None:
        sets.append("name = :name")
        params["name"] = body.name.strip()
    if body.url is not None:
        if before["source"] != "url":
            raise invalid_filter("Only URL feeds have a URL", {"url": "Not a URL feed"})
        sets.append("url = :url")
        params["url"] = _check_url(body.url)
    if body.category is not None:
        sets.append("category = :cat")
        params["cat"] = _check_category(body.category)
    if body.enabled is not None:
        sets.append("enabled = :en")
        params["en"] = body.enabled
    if body.refresh_hours is not None:
        sets.append("refresh_hours = :rh")
        params["rh"] = body.refresh_hours
    result = None
    if sets:
        try:
            await db.execute(text(f"UPDATE netflow_ti_feeds SET {', '.join(sets)}, updated_by = :by, updated_at = NOW() "
                                  "WHERE id = :id"), params)
        except Exception:
            await db.rollback()
            raise HTTPException(status_code=409, detail="A feed with that name already exists") from None
        if body.enabled and not before["enabled"] and before["source"] == "url" and before["entry_count"] == 0:
            result = await ti.refresh_feed(db, feed_id)
        await write_audit_log(db, actor=user, action="netflow.ti_feed.update", resource_type="netflow_ti_feed",
                              resource_id=str(feed_id),
                              metadata={"before": {k: before.get(k) for k in ("name", "url", "category", "enabled",
                                                                               "refresh_hours")},
                                        "changes": {k: v for k, v in body.model_dump(exclude_none=True).items()}})
        await db.commit()
        ti.invalidate_index()
        det._adhoc_cache.clear()
    return {**ti.feed_item(await _feed_row(db, feed_id)), "result": result}


@router.delete("/ti/feeds/{feed_id}")
async def delete_feed(
    feed_id: uuid.UUID,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    _require_manage(vis, "removing threat-intel feeds")
    before = ti.feed_item(await _feed_row(db, feed_id))
    await db.execute(text("DELETE FROM netflow_ti_feeds WHERE id = :id"), {"id": feed_id})
    await write_audit_log(db, actor=user, action="netflow.ti_feed.delete", resource_type="netflow_ti_feed",
                          resource_id=str(feed_id), metadata=before)
    await db.commit()
    ti.invalidate_index()
    det._adhoc_cache.clear()
    return {"deleted": str(feed_id)}


@router.post("/ti/feeds/{feed_id}/refresh")
async def refresh_feed(
    feed_id: uuid.UUID,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    """Download a URL feed now (the previous entries stay if it fails)."""
    _require_manage(vis, "refreshing threat-intel feeds")
    item = ti.feed_item(await _feed_row(db, feed_id))
    if item["source"] != "url":
        raise invalid_filter("Only URL feeds can be refreshed; upload a new list instead", {"source": "Not a URL feed"})
    result = await ti.refresh_feed(db, feed_id)
    await write_audit_log(db, actor=user, action="netflow.ti_feed.refresh", resource_type="netflow_ti_feed",
                          resource_id=str(feed_id), metadata={"status": result.get("status")})
    await db.commit()
    ti.invalidate_index()
    det._adhoc_cache.clear()
    return {**ti.feed_item(await _feed_row(db, feed_id)), "result": result}


@router.post("/ti/feeds/{feed_id}/upload")
async def upload_feed(
    feed_id: uuid.UUID,
    body: FeedUpload,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    """Replace an uploaded feed's list (air-gapped appliances)."""
    _require_manage(vis, "uploading threat-intel lists")
    item = ti.feed_item(await _feed_row(db, feed_id))
    if item["source"] != "upload":
        raise invalid_filter("Only uploaded feeds take a list; URL feeds are downloaded", {"source": "Not an upload feed"})
    result = await ti.refresh_feed(db, feed_id, content=body.content)
    await write_audit_log(db, actor=user, action="netflow.ti_feed.upload", resource_type="netflow_ti_feed",
                          resource_id=str(feed_id), metadata={"status": result.get("status"),
                                                              "stats": result.get("stats")})
    await db.commit()
    ti.invalidate_index()
    det._adhoc_cache.clear()
    return {**ti.feed_item(await _feed_row(db, feed_id)), "result": result}


@router.get("/ti/feeds/{feed_id}/entries")
async def feed_entries(
    feed_id: uuid.UUID,
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    item = ti.feed_item(await _feed_row(db, feed_id))
    rows = (await db.execute(text(
        "SELECT prefix::text FROM netflow_ti_entries WHERE feed_id = :id ORDER BY prefix LIMIT :l OFFSET :o"
    ), {"id": feed_id, "l": limit, "o": offset})).all()
    current_meta().set_truncated(limit, len(rows), more=offset + len(rows) < item["entry_count"])
    return {"feed": item, "entries": [r[0] for r in rows], "total": item["entry_count"], "offset": offset}


@router.get("/ti/lookup")
async def ti_lookup(
    ip: str = Query(..., max_length=2000, description="Comma-separated addresses (≤ 100)"),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Which enabled feeds list these addresses (host profile, forensics)."""
    ips = [x.strip() for x in ip.split(",") if x.strip()][:100]
    for x in ips:
        try:
            ipaddress.ip_address(x)
        except ValueError:
            raise invalid_filter(f"'{x[:64]}' is not an IP address", {"ip": "Not an IP address"}) from None
    index = await ti.get_index(db)
    return {"results": {x: [f.as_dict() for f in index.lookup(x)] for x in ips},
            "index": {"prefixes": index.prefixes, "feeds": len(index.feeds)}}
