"""Shareable Services reports and portable import (B27, shareable reports).

Every report states its calculation basis (confirmed availability, maintenance
excluded), timezone and the requested / effective / maintenance / unknown
durations so the denominator is explainable. Nothing secret is exported.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core import scoping
from app.models.service_check import ServiceCheck, ServiceCheckGroup, ServiceCredential

FAILED_PROBE_LIMIT = 500


def _aware(ts):
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _local(ts, zone: ZoneInfo):
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return _aware(ts).astimezone(zone).isoformat()


def definition_summary(sc) -> dict:
    return {
        "id": str(sc.id),
        "name": sc.name,
        "check_type": sc.check_type,
        "level": sc.level,
        "target": sc.target_url or (f"{sc.target_host}:{sc.target_port}" if sc.target_port else sc.target_host),
        "enabled": bool(sc.enabled),
        "check_interval": sc.check_interval,
        "timeout": sc.timeout,
        "retry_count": sc.retry_count,
        "retry_delay_s": sc.retry_delay_s,
        "down_check_interval": sc.down_check_interval,
        "tags": list(sc.tags or []),
        "group_name": sc.group.name if getattr(sc, "group", None) else None,
        "criticality": getattr(sc, "criticality", None),
        "business_owner": getattr(sc, "business_owner", None),
        "owner_team": getattr(sc, "owner_team", None),
        "runbook_url": getattr(sc, "runbook_url", None),
        "created_at": _aware(sc.created_at).isoformat() if sc.created_at else None,
    }


def failed_probes(ch, check_id, start: datetime, end: datetime, limit: int = FAILED_PROBE_LIMIT) -> list[dict]:
    rows = ch.query(f"""
        SELECT timestamp, response_ms, status_code, error_message
        FROM zenplus.service_metrics
        WHERE service_check_id = %(id)s AND is_up = 0
          AND timestamp >= %(f)s AND timestamp < %(t)s
        ORDER BY timestamp DESC
        LIMIT {int(limit)}
    """, parameters={"id": str(check_id), "f": start.strftime("%Y-%m-%d %H:%M:%S"),
                     "t": end.strftime("%Y-%m-%d %H:%M:%S")}).result_rows
    return [{"timestamp": _aware(r[0]).isoformat(), "response_ms": r[1], "status_code": r[2],
             "error_message": r[3] or None} for r in rows]


async def check_report(db, sc, from_dt: datetime, to_dt: datetime, tz_name: str = "UTC",
                       visible_tags=None) -> dict:
    """Selected-window evidence for one check (confirmed basis)."""
    from app.core.database import get_clickhouse_client
    from app.services import service_check_service, service_maintenance

    sla = await service_check_service.get_service_sla(
        db, sc.id, 720, from_dt, to_dt, basis="confirmed", visible_tags=visible_tags)
    if sla.get("error") == "not_found":
        return {"error": "not_found"}
    start = datetime.fromisoformat(sla["requested_from"])
    end = datetime.fromisoformat(sla["requested_to"])
    windows = await service_maintenance.windows_for_check(db, sc, start, end)
    probes = []
    try:
        probes = failed_probes(get_clickhouse_client(), sc.id, start, end)
    except Exception:
        probes = []
    return {
        "report": "service_check_availability",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": tz_name,
        "basis": "confirmed",
        "basis_note": ("Confirmed availability: only confirmed incidents count as downtime; isolated failed "
                       "probes are diagnostic evidence. Maintenance is excluded; monitoring gaps are unknown."),
        "check": definition_summary(sc),
        "window": {
            "requested_from": sla["requested_from"], "requested_to": sla["requested_to"],
            "effective_from": sla["effective_from"], "effective_to": sla["effective_to"],
            "requested_sec": sla["requested_sec"], "covered_sec": sla["covered_sec"],
            "maintenance_sec": sla["maintenance_sec"], "unknown_sec": sla["unknown_sec"],
            "coverage_pct": sla["coverage_pct"],
        },
        "availability": {
            "confirmed_uptime_pct": sla["uptime_pct"],
            "probe_success_pct": sla["probe_uptime_pct"],
            "downtime_sec": sla["total_downtime_sec"],
            "incident_count": sla["incident_count"],
            "completed_incident_count": sla["completed_incident_count"],
            "mttr_sec": sla["mttr_sec"],
            "mtbf_sec": sla["mtbf_sec"],
            "sample_count": sla["sample_count"],
        },
        "objective": {k: sla.get(k) for k in (
            "target_pct", "target_source", "error_budget_sec", "budget_used_sec", "error_budget_used_pct",
            "error_budget_remaining_sec", "burn_rate", "meets_target")},
        "latency": {k: sla.get(k) for k in (
            "avg_response_ms", "p95_response_ms", "max_response_ms", "min_response_ms", "latency_sample_count")},
        "incidents": sla["incidents"],
        "incidents_truncated": sla["incidents_truncated"],
        "maintenance_windows": windows,
        "failed_probes": probes,
        "failed_probes_limit": FAILED_PROBE_LIMIT,
    }


def check_report_csv(report: dict) -> str:
    zone = ZoneInfo(report["timezone"])
    buf = io.StringIO()
    w = csv.writer(buf)

    def section(title, header, rows):
        w.writerow([f"# {title}"])
        w.writerow(header)
        for r in rows:
            w.writerow(r)
        w.writerow([])

    c = report["check"]
    section("Monitor", ["field", "value"], [[k, "|".join(v) if isinstance(v, list) else v] for k, v in c.items()])
    win = report["window"]
    section("Window", ["field", "value"], [
        ["timezone", report["timezone"]], ["basis", report["basis"]],
        ["requested_from", _local(win["requested_from"], zone)], ["requested_to", _local(win["requested_to"], zone)],
        ["effective_from", _local(win["effective_from"], zone)], ["effective_to", _local(win["effective_to"], zone)],
        ["requested_sec", win["requested_sec"]], ["covered_sec", win["covered_sec"]],
        ["maintenance_sec", win["maintenance_sec"]], ["unknown_sec", win["unknown_sec"]],
        ["coverage_pct", win["coverage_pct"]],
    ])
    section("Availability", ["field", "value"], [[k, v] for k, v in report["availability"].items()])
    section("Objective", ["field", "value"], [[k, v] for k, v in report["objective"].items()])
    section("Latency (successful probes)", ["field", "value"], [[k, v] for k, v in report["latency"].items()])
    section("Incidents", ["start", "end", "ongoing", "starts_before_window", "downtime_sec", "duration_sec"], [
        [_local(i["start"], zone), _local(i["end"], zone), i["ongoing"], i["starts_before_window"],
         i["downtime_sec"], i["duration_sec"]] for i in report["incidents"]])
    section("Maintenance windows", ["scope", "starts_at", "effective_ends_at", "state", "reason"], [
        [m["scope_label"], _local(m["starts_at"], zone), _local(m["effective_ends_at"], zone), m["state"],
         m.get("reason")] for m in report["maintenance_windows"]])
    section(f"Failed probes (newest {report['failed_probes_limit']})",
            ["timestamp", "response_ms", "status_code", "error_message"], [
        [_local(p["timestamp"], zone), p["response_ms"], p["status_code"], p["error_message"]]
        for p in report["failed_probes"]])
    return buf.getvalue()


async def fleet_report(db, from_dt: datetime, to_dt: datetime, tz_name: str = "UTC",
                       visible_tags=None) -> dict:
    """One row per visible check over the window (confirmed basis)."""
    import asyncio
    from app.core.database import get_clickhouse_client
    from app.services import service_maintenance
    from app.services.availability_policy import load_default_target, resolve_target
    from app.services.report_data_service import _compliance
    from app.services.service_availability import read_fleet_segments, summarize, incident_intervals
    from app.services.service_check_service import _scope_where, _finite

    now = datetime.now(timezone.utc)
    start, end = _aware(from_dt), min(_aware(to_dt), now)
    if end <= start:
        end = start
    checks = list((await db.execute(_scope_where(select(ServiceCheck), visible_tags)
                                    .order_by(ServiceCheck.name))).scalars().all())
    checks = [c for c in checks if scoping.entity_visible(c.tags, visible_tags)]
    policy_target, policy_source = await load_default_target(db)
    maintenance = await service_maintenance.intervals_for_checks(db, checks, start, end) if checks else {}
    spec = {str(c.id): (c.created_at, c.check_interval or 60) for c in checks}

    def collect():
        ch = get_clickhouse_client()
        confirmed = {}
        for cid, segments, count, observed_start in read_fleet_segments(ch, spec, start, end):
            excluded = maintenance.get(cid)
            confirmed[cid] = (summarize(segments, observed_start, end, excluded),
                              len(incident_intervals(segments, observed_start, end, excluded,
                                                     merge_gap_sec=2 * spec[cid][1], now=now)),
                              count)
        probes = {}
        if spec:
            rows = ch.query("""
                SELECT toString(service_check_id), countIf(is_up = 1), count()
                FROM zenplus.service_metrics
                WHERE toString(service_check_id) IN %(ids)s AND timestamp >= %(f)s AND timestamp < %(t)s
                GROUP BY service_check_id
            """, parameters={"ids": list(spec), "f": start.strftime("%Y-%m-%d %H:%M:%S"),
                             "t": end.strftime("%Y-%m-%d %H:%M:%S")}).result_rows
            probes = {r[0]: (int(r[1] or 0), int(r[2] or 0)) for r in rows}
        return confirmed, probes

    confirmed, probes = await asyncio.to_thread(collect) if checks else ({}, {})
    rows = []
    for c in checks:
        cid = str(c.id)
        summary, incidents, samples = confirmed.get(cid, (None, 0, 0))
        avail = _finite(summary["uptime_pct"]) if summary else None
        covered = summary["covered_sec"] if summary else 0.0
        downtime = summary["total_downtime_sec"] if summary and avail is not None else None
        target, source = resolve_target(getattr(c, "sla_target_pct", None), policy_target, policy_source)
        meets, compliance, budget_used = _compliance(avail, target, downtime, covered)
        ok, total = probes.get(cid, (0, 0))
        rows.append({
            "service_check_id": cid,
            "name": c.name,
            "check_type": c.check_type,
            "group_name": c.group.name if getattr(c, "group", None) else None,
            "status": c.status,
            "enabled": bool(c.enabled),
            "confirmed_uptime_pct": avail,
            "probe_success_pct": (ok / total * 100.0) if total else None,
            "probe_count": total,
            "coverage_pct": summary["coverage_pct"] if summary else None,
            "covered_sec": covered,
            "maintenance_sec": summary["maintenance_sec"] if summary else 0.0,
            "unknown_sec": summary["unknown_sec"] if summary else None,
            "downtime_sec": downtime,
            "incident_count": incidents,
            "target_pct": target,
            "target_source": source,
            "meets_target": meets,
            "compliance": compliance,
            "error_budget_used_pct": _finite(budget_used),
            "criticality": getattr(c, "criticality", None),
            "business_owner": getattr(c, "business_owner", None),
            "owner_team": getattr(c, "owner_team", None),
        })
    return {
        "report": "services_fleet_availability",
        "generated_at": now.isoformat(),
        "timezone": tz_name,
        "basis": "confirmed",
        "maintenance_excluded": True,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "rows": rows,
    }


FLEET_COLUMNS = ("name", "check_type", "group_name", "status", "enabled", "confirmed_uptime_pct",
                 "probe_success_pct", "probe_count", "coverage_pct", "covered_sec", "maintenance_sec",
                 "unknown_sec", "downtime_sec", "incident_count", "target_pct", "target_source",
                 "meets_target", "compliance", "error_budget_used_pct", "criticality", "business_owner",
                 "owner_team", "service_check_id")


def fleet_report_csv(report: dict) -> str:
    zone = ZoneInfo(report["timezone"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# Services availability report"])
    w.writerow(["basis", report["basis"], "maintenance_excluded", report["maintenance_excluded"]])
    w.writerow(["timezone", report["timezone"], "from", _local(report["from"], zone), "to", _local(report["to"], zone)])
    w.writerow([])
    w.writerow(FLEET_COLUMNS)
    for r in report["rows"]:
        w.writerow([r.get(k) for k in FLEET_COLUMNS])
    return buf.getvalue()


# ── Import ───────────────────────────────────────────────────────────────────

REDACTED = "__REDACTED__"


def _strip_redacted(item: dict) -> tuple[dict, list[str]]:
    """Drop redacted secret placeholders; report what the operator must re-enter."""
    warnings = []
    headers = dict(item.get("http_headers") or {})
    for k in [k for k, v in headers.items() if v == REDACTED]:
        headers.pop(k)
        warnings.append(f"header '{k}' was redacted")
    item["http_headers"] = headers
    if item.get("http_body") == REDACTED:
        item["http_body"] = None
        warnings.append("request body was redacted")
    steps = []
    for i, step in enumerate(item.get("workflow_steps") or []):
        step = dict(step)
        sh = dict(step.get("headers") or {})
        for k in [k for k, v in sh.items() if v == REDACTED]:
            sh.pop(k)
            warnings.append(f"step {i + 1} header '{k}' was redacted")
        step["headers"] = sh
        if step.get("body") == REDACTED:
            step["body"] = None
            warnings.append(f"step {i + 1} body was redacted")
        steps.append(step)
    item["workflow_steps"] = steps
    return item, warnings


_CREATE_FIELDS = {
    "name", "check_type", "enabled", "description", "tags", "target_host", "target_port", "target_url",
    "http_method", "http_headers", "http_body", "http_expected_status", "http_expected_statuses",
    "http_content_match", "http_follow_redirects", "http_ignore_tls_errors", "http_allow_insecure_auth",
    "config", "retry_count", "retry_delay_s", "down_check_interval", "check_interval", "timeout",
    "tls_warn_days", "tls_critical_days", "workflow_operator", "workflow_steps", "sla_target_pct",
    "criticality", "business_owner", "owner_team", "runbook_url",
}


async def import_checks(db, document: dict, user, visible_tags=None, dry_run: bool = True) -> dict:
    """Create checks from a portable (schema_version 2) document.

    Existing names are skipped; group/credential/device/parent resolve by name
    (parents after every create); secrets are never imported — a check whose
    secret-bearing fields were redacted is created paused with a warning.
    The caller commits (real run) or rolls back (dry run)."""
    from pydantic import ValidationError
    from app.models.device import Device
    from app.schemas.service_check import ServiceCheckCreate
    from app.services import service_check_service

    if not isinstance(document, dict) or document.get("schema_version") != 2 or not isinstance(document.get("checks"), list):
        raise service_check_service.ServiceCheckValidationError(
            "Expected a portable export document (schema_version 2 with a checks list)")
    items = document["checks"]
    if len(items) > 1000:
        raise service_check_service.ServiceCheckValidationError("At most 1000 checks per import")

    existing = {r[0]: r[1] for r in (await db.execute(select(ServiceCheck.name, ServiceCheck.id))).all()}
    existing_tags = {}
    for name, tags in (await db.execute(select(ServiceCheck.name, ServiceCheck.tags))).all():
        existing_tags[name] = tags
    groups = {r[0]: r[1] for r in (await db.execute(select(ServiceCheckGroup.name, ServiceCheckGroup.id))).all()}
    creds = {r[0]: r[1] for r in (await db.execute(select(ServiceCredential.name, ServiceCredential.id))).all()}

    results = []
    created: dict[str, object] = {}
    parents: list[tuple[object, str, str]] = []
    seen_names: set[str] = set()
    for raw in items:
        name = (raw or {}).get("name") if isinstance(raw, dict) else None
        entry = {"name": name, "action": "create"}
        if not isinstance(raw, dict) or not name:
            results.append({"name": name, "action": "error", "error": "Each check needs a name"})
            continue
        if name in existing or name in seen_names:
            results.append({"name": name, "action": "skip_existing"})
            continue
        seen_names.add(name)
        item, warnings = _strip_redacted(dict(raw))
        fields = {k: v for k, v in item.items() if k in _CREATE_FIELDS and v is not None}
        cred_name = item.get("credential_name")
        if cred_name:
            if cred_name not in creds:
                results.append({"name": name, "action": "error",
                                "error": f"Credential '{cred_name}' does not exist; create it first"})
                continue
            fields["credential_id"] = creds[cred_name]
        group_name = item.get("group_name")
        if group_name:
            if group_name not in groups:
                if dry_run:
                    warnings.append(f"group '{group_name}' will be created")
                g = ServiceCheckGroup(name=group_name)
                db.add(g)
                await db.flush()
                groups[group_name] = g.id
            fields["group_id"] = groups[group_name]
        host = item.get("device_hostname")
        if host:
            dev = (await db.execute(select(Device.id, Device.tags).where(Device.hostname == host))).first()
            if dev is not None and scoping.entity_visible(dev[1], visible_tags):
                fields["device_id"] = dev[0]
            else:
                warnings.append(f"device '{host}' not found; imported without a device link")
        if warnings and any("redacted" in w for w in warnings):
            fields["enabled"] = False
            warnings.append("imported paused: re-enter the redacted values, then resume")
        try:
            data = ServiceCheckCreate(**fields)
            sc = await service_check_service.create_service_check(
                db, data, getattr(user, "id", None), visible_tags=visible_tags, commit=False)
        except ValidationError as exc:
            results.append({"name": name, "action": "error",
                            "error": "; ".join(e.get("msg", "invalid") for e in exc.errors()[:3])})
            continue
        except ValueError as exc:
            results.append({"name": name, "action": "error", "error": str(exc)})
            continue
        created[name] = sc.id
        if item.get("parent_check_name"):
            parents.append((sc, name, item["parent_check_name"]))
        if warnings:
            entry["warnings"] = warnings
        results.append(entry)

    for sc, name, parent_name in parents:
        pid = created.get(parent_name)
        if pid is None and parent_name in existing and scoping.entity_visible(existing_tags.get(parent_name), visible_tags):
            pid = existing[parent_name]
        entry = next(r for r in results if r["name"] == name and r["action"] == "create")
        if pid is None:
            entry.setdefault("warnings", []).append(f"parent '{parent_name}' not found; dependency not set")
        else:
            sc.parent_check_id = pid
    await db.flush()
    summary = {"create": 0, "skip_existing": 0, "error": 0}
    for r in results:
        summary[r["action"]] += 1
    return {"dry_run": dry_run, "results": results, "summary": summary}
