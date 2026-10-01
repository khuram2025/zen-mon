"""NetFlow collector and exporter health, exporter settings, counting and
onboarding (B6: NF-01, FE-01 / CR-05, FE-11, FE-12 / CR-16, CR-06).

    GET    /exporters/status              receipt state of every visible exporter (contract §11)
    GET    /exporters/{ip}/health         one exporter: streams, templates, fields, series
    GET    /exporters/{ip}/check          onboarding live check for one exporter address
    GET    /collector/health              collector process + persisted statistics
    GET    /exporters/counting            duplicate-observation estimate + counting modes
    GET    /exporters/settings            exporter settings (role, counting direction, interval)
    PUT    /exporters/{ip}/settings       create / change one exporter's settings
    DELETE /exporters/{ip}/settings       back to defaults
    GET    /setup/info                    appliance addresses and collector ports for configs

Everything here runs on the appliance clock and ignores page filters and
windows: health is a property of the exporter, not of a traffic slice (X-04).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.user import User
from app.services import netflow_health as nh

from .access import (
    FlowVisibility,
    canonical_ip,
    flow_visibility,
    require_exporter_visible,
    require_manage,
)
from .common import (
    RAW,
    _resolve_interface_names,
    _to_iso_utc,
    current_meta,
    now_utc,
    resolve_window,
    run_query,
    table_ref,
)
from .filters import FlowFilter, compile_filter, invalidate_counting_cache, load_counting_policy
from .meta import invalidate_expected_intervals
from .sql import DST, EXPORTER, SRC, display

router = APIRouter()

MAX_STATS_HOURS = 720      # flow_collector_stats keeps 30 days


def _disclose(meta_warnings: list[dict]) -> None:
    meta = current_meta()
    for w in meta_warnings:
        meta.warn(w["code"], w["message"])


def _stats_window(hours: int, tz: str | None = None):
    """The statistics window (half-open, appliance clock). Not recorded as the
    response's flow window: collector statistics are not a flow source, and the
    body states its own period and bucket size (``tz`` only labels it)."""
    if not 1 <= hours <= MAX_STATS_HOURS:
        raise HTTPException(status_code=400, detail=f"hours must be between 1 and {MAX_STATS_HOURS}")
    win = resolve_window(hours=hours, tz=tz, max_span=timedelta(hours=MAX_STATS_HOURS), record=False)
    return win, nh.series_granularity(win.end - win.start)


# ── Exporter status (contract §11) ───────────────────────────────────────────

@router.get("/exporters/status")
async def exporters_status(
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Receipt state of every exporter the caller may see.

    Independent of page filters and windows: receipt age is measured from the
    last datagram or record the collector received (appliance clock) against
    each exporter's expected interval -- configured, else learned from its
    own cadence. ``state_since`` says when the current state began (null when
    unknown). ``device_status`` is the owner device's own polled status:
    exporter silence never implies the device is down.
    """
    status = await nh.status_for(db, vis)
    current_meta().restricted = vis.restricted
    _disclose(status.get("warnings") or [])
    return status


# ── Collector ────────────────────────────────────────────────────────────────

def _live_summary(live: nh.CollectorLive, vis: FlowVisibility) -> dict:
    doc = live.doc or {}
    started = nh._parse_ts(doc.get("started_at"))
    server = nh._parse_ts(doc.get("server_time"))
    exporters = []
    for s in doc.get("exporters") or []:
        ip = canonical_ip(s.get("exporter"))
        if ip is None or not vis.allows(ip):
            continue
        exporters.append({**s, "exporter": ip,
                          "last_datagram_at": _to_iso_utc(nh._parse_ts(s.get("last_datagram_at")))})
    return {
        "reachable": live.reachable,
        "error": live.error,
        "fetched_at": _to_iso_utc(live.fetched_at),
        "status": doc.get("status") if live.reachable else "unknown",
        "reasons": list(doc.get("reasons") or []),
        "collector_id": doc.get("collector_id"),
        "started_at": _to_iso_utc(started),
        "uptime_s": int((server - started).total_seconds()) if started and server else None,
        "listen": doc.get("listen"),
        "listening": doc.get("listening"),
        "sflow_listen": doc.get("sflow_listen"),
        "clock_policy": doc.get("clock_policy"),
        "rcvbuf_bytes": doc.get("rcvbuf_bytes"),
        "socket_drops": doc.get("socket_drops"),
        "queue": doc.get("queue"),
        "delivery": doc.get("delivery"),
        # Appliance-wide cumulative counters include every exporter; a scoped
        # user gets the per-exporter streams it may see instead.
        "counters": None if vis.restricted else doc.get("counters"),
        "process": doc.get("process"),
        "exporters": exporters,
    }


@router.get("/collector/health")
async def collector_health(
    hours: int = Query(default=1, description="Statistics window in hours (1..720)"),
    tz: str | None = Query(default=None, description="IANA time zone the window is labelled in"),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """The collector process (live health endpoint) and its persisted
    statistics over the window: datagrams, decoded / written records, drops by
    reason, parse errors, data sets waiting for templates, sequence gaps,
    queue, spool, insert failures and retries, kernel receive-buffer drops."""
    win, g = _stats_window(hours, tz)
    current_meta().restricted = vis.restricted
    exporters = vis.exporter_list()
    live, totals, series = await asyncio.gather(
        nh.fetch_collector_live(),
        nh.collector_totals(win.start, win.end, exporters),
        nh.stats_series(win.start, win.end, g, exporters),
    )
    if totals is None:
        current_meta().warn("collector_stats_unavailable",
                            "Collector statistics could not be read from the flow database")
    elif totals["process"] is None:
        current_meta().warn("collector_stats_missing",
                            "No collector statistics were recorded in this window; the collector may be stopped "
                            "or running a version that does not write them")
    if not live.reachable:
        current_meta().warn("collector_unreachable",
                            "The collector's live health endpoint did not answer")
    return {
        "server_time": _to_iso_utc(now_utc()),
        "window": {**win.as_meta(), "granularity_s": g},
        "live": _live_summary(live, vis),
        "stats": totals,
        "series": series,
    }


# ── One exporter ─────────────────────────────────────────────────────────────

@router.get("/exporters/{exporter}/health")
async def exporter_health(
    exporter: str,
    hours: int = Query(default=1, description="Statistics window in hours (1..720)"),
    tz: str | None = Query(default=None, description="IANA time zone the window is labelled in"),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Everything the collector knows about one exporter: the status row, per
    observation-domain / protocol streams over the window, the series, the
    template inventory with field names, what its recent records actually
    carried, the interfaces it reports, its settings and owner."""
    ip = require_exporter_visible(vis, exporter)
    win, g = _stats_window(hours, tz)
    now = now_utc()
    row, streams, series, templates, fields, ifaces = await asyncio.gather(
        nh.exporter_row(db, ip, now=now),
        nh.exporter_streams(ip, win.start, win.end),
        nh.stats_series(win.start, win.end, g, [ip], process=False),
        nh.exporter_templates(ip),
        nh.exporter_fields(ip, now),
        nh.exporter_ifindexes(ip, now - timedelta(hours=24), now),
    )
    names = await _resolve_interface_names([(ip, i["ifindex"]) for i in ifaces or []], db)
    speeds = await nh.interface_speeds(db, ip, row.get("device_id"))
    for i in ifaces or []:
        meta = names.get((ip, i["ifindex"]))
        sp = speeds.get(i["ifindex"]) or {}
        i["if_name"] = (meta or {}).get("if_name") or (meta or {}).get("if_descr") or sp.get("override_name")
        # One speed rule everywhere: configured, else NetFlow override, else SNMP.
        i["if_speed"] = sp.get("speed")
        i["speed_source"] = sp.get("source") or "unknown"
        i["resolved"] = meta is not None
    settings = (await nh.load_exporter_settings(db)).get(ip)
    if settings:
        names_by_id = await _users(db, {settings.get("updated_by")})
        settings = {**settings, "updated_by_name": names_by_id.get(settings.get("updated_by") or "")}
    if streams is None:
        current_meta().warn("collector_stats_unavailable", "Collector statistics could not be read")
    return {
        "server_time": _to_iso_utc(now),
        "window": {**win.as_meta(), "granularity_s": g},
        "status": row,
        "streams": streams,
        "series": series,
        "templates": templates,
        "fields": fields,
        "interfaces": ifaces,
        "settings": ({**_settings_payload(settings), "updated_by_name": settings.get("updated_by_name")}
                     if settings else None),
    }


# ── Onboarding check (CR-06) ─────────────────────────────────────────────────

def _check(cid: str, label: str, status: str, detail: str, hint: str | None = None) -> dict:
    return {"id": cid, "label": label, "status": status, "detail": detail, "hint": hint}


def _age_text(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{int(seconds)} s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} days ago"


def _interval_text(seconds: float | None) -> str:
    if not seconds:
        return "unknown"
    return f"late after {int(seconds)} s" if seconds < 120 else f"late after {seconds / 60:g} min"


def build_checks(row: dict, fields: dict | None, templates: list[dict] | None, live: dict,
                 owner_interfaces: dict | None) -> list[dict]:
    """The onboarding checklist for one exporter (pure; see exporter_check)."""
    checks: list[dict] = []
    port = nh._port_of(live.get("listen")) or nh.DEFAULT_NETFLOW_PORT
    sflow_port = nh._port_of(live.get("sflow_listen")) or nh.DEFAULT_SFLOW_PORT
    if live.get("reachable"):
        if live.get("listening") is False:
            checks.append(_check("collector", "Collector listening", "fail",
                                 "The collector is running but not listening for flow datagrams.",
                                 "Check the collector service log (journalctl -u zenplus-netflow-collector)."))
        elif live.get("status") == "ok":
            checks.append(_check("collector", "Collector listening", "pass",
                                 f"Listening on UDP {port}; delivery to the flow database is healthy."))
        else:
            checks.append(_check("collector", "Collector listening", "warn",
                                 f"Collector status is {live.get('status')}: " + "; ".join(live.get("reasons") or [])))
    else:
        checks.append(_check("collector", "Collector listening", "warn",
                             "The collector's health endpoint did not answer; results below come from stored "
                             "statistics.", "Check that zenplus-netflow-collector is running."))

    ip = row["exporter"]
    seen = row.get("last_datagram_at") or row.get("last_record_at")
    issues = {i["code"] for i in row.get("issues") or []}
    # Something arrived once is not "flows arrive": only a receipt within the
    # exporter's expected interval (state 'receiving', the same verdict as the
    # Health page) passes. A late exporter warns; a silent one fails with the
    # time it stopped, and the checks below that need fresh records say so.
    state = row.get("state") or ("receiving" if seen else "never")
    receiving = bool(seen) and state == "receiving"
    age = _age_text(row.get("receipt_age_s"))
    last_at = re.sub(r"\.\d+", "", str(row.get("last_received_at") or seen)).replace("T", " ").replace("Z", " UTC")
    if seen and state == "receiving":
        checks.append(_check("datagrams", "Datagrams received", "pass",
                             f"Last datagram from {ip} {age}."))
    elif seen and state == "late":
        checks.append(_check("datagrams", "Datagrams received", "warn",
                             f"Late: the last datagram from {ip} arrived {age}, longer than its expected "
                             f"interval ({_interval_text(row.get('late_after_s'))}).",
                             "Check that the exporter is still sending (export configuration, ACLs on the path, "
                             "the device itself)."))
    elif seen:
        checks.append(_check("datagrams", "Datagrams received", "fail",
                             f"Not receiving: nothing from {ip} since {last_at} ({age}).",
                             f"The exporter has stopped sending to UDP {port} (sFlow: {sflow_port}), or something on "
                             "the path drops it. Check the export configuration, the export source address and ACLs."))
    else:
        checks.append(_check("datagrams", "Datagrams received", "pending",
                             f"Nothing has arrived from {ip} yet.",
                             f"Point the exporter at this appliance on UDP {port} (sFlow: {sflow_port}), make sure the "
                             f"export source address is {ip}, and that no ACL or firewall drops UDP on the way."))
    if "rejected_allowlist" in issues:
        checks.append(_check("allowlist", "Exporter accepted", "fail",
                             "The collector drops this exporter because NETFLOW_ALLOWED_EXPORTERS does not list it.",
                             "Add the address to NETFLOW_ALLOWED_EXPORTERS (or clear the list) and restart the collector."))

    protocols = set(row.get("export_protocols") or [])
    templated = protocols & {"netflow9", "ipfix"}
    data_templates = [t for t in templates or [] if not t["is_options"]]
    if not seen:
        checks.append(_check("templates", "Templates received", "pending", "Waiting for the first datagram."))
    elif templated:
        if data_templates:
            checks.append(_check("templates", "Templates received", "pass",
                                 f"{len(data_templates)} data template(s) and "
                                 f"{len(templates or []) - len(data_templates)} options template(s) known."))
        elif "waiting_template" in issues:
            checks.append(_check("templates", "Templates received", "fail",
                                 "Data arrives but its template has not been received, so nothing can be decoded.",
                                 "Lower the template refresh / timeout on the exporter (60 s is typical) or wait "
                                 "for the next refresh."))
        else:
            checks.append(_check("templates", "Templates received", "pending",
                                 "No data template recorded yet."))
    else:
        checks.append(_check("templates", "Templates received", "na",
                             "NetFlow v5 and sFlow carry a fixed format; no templates are needed."))

    rec = row.get("records_15m")
    if rec:
        checks.append(_check("records", "Flow records stored", "pass",
                             f"{rec:,} records stored in the last 15 minutes."))
    elif seen and not receiving:
        checks.append(_check("records", "Flow records stored", "fail" if state == "silent" else "warn",
                             f"No flow records stored in the last 15 minutes: the exporter is "
                             f"{'not sending' if state == 'silent' else 'late'} (last receipt {age})."))
    elif seen:
        checks.append(_check("records", "Flow records stored", "fail" if "waiting_template" in issues else "pending",
                             "Datagrams arrive but no flow records were stored in the last 15 minutes."))
    else:
        checks.append(_check("records", "Flow records stored", "pending", "Waiting for the first datagram."))

    if not seen or (not receiving and not rec):
        # Nothing recent to inspect: one line instead of a column of "waiting".
        checks.append(_check("fields", "Fields, sampling and clock", "pending",
                             "Checked as soon as the first flow records arrive." if not seen else
                             "Checked again when flow records arrive from this exporter."))
        _mapping_check(checks, row, owner_interfaces)
        return checks

    caps = {c["key"]: c for c in (fields or {}).get("capabilities") or []}
    for key, label, missing_hint in (
        ("tcp_flags", "TCP flags exported", "Add the TCP flags field (tcpControlBits, IE 6) to the flow record."),
        ("l4_ports", "Transport ports exported", "Add source and destination transport ports (IE 7, 11)."),
        ("interfaces", "Interface indexes exported", "Add input and output interface (IE 10, 14) to the record."),
        ("flow_times", "Flow start/end times exported", "Add flow start/end times (IE 21/22 or 152/153)."),
        ("direction", "Flow direction exported", "Optional: add flowDirection (IE 61) for automatic edge counting."),
    ):
        c = caps.get(key)
        if c is None or c["status"] == "unknown":
            checks.append(_check(f"field_{key}", label, "pending", "No records received yet to check."))
        elif c["status"] == "all":
            checks.append(_check(f"field_{key}", label, "pass", "Present in every record."))
        elif c["status"] == "some":
            checks.append(_check(f"field_{key}", label, "warn", f"Present in {c['pct']:g}% of records.", missing_hint))
        elif c["status"] == "n/a":
            checks.append(_check(f"field_{key}", label, "info", "No traffic this field applies to arrived yet."))
        else:
            checks.append(_check(f"field_{key}", label, "info" if key == "direction" else "warn",
                                 "Not present in the records received.", missing_hint))

    samp = (fields or {}).get("max_sampling") or row.get("sampling_interval")
    if samp is None:
        checks.append(_check("sampling", "Sampling rate known", "pending", "No records yet."))
    elif samp <= 1:
        checks.append(_check("sampling", "Sampling rate known", "pass", "Unsampled (1:1): volumes are exact."))
    else:
        checks.append(_check("sampling", "Sampling rate known", "info",
                             f"Sampled 1-in-{samp}: volumes are scaled up by the exporter's rate."))

    offset = row.get("clock_offset_ms")
    if offset is None:
        checks.append(_check("clock", "Exporter clock", "pending", "No export timestamps received yet."))
    else:
        sec = abs(offset) / 1000
        direction = "ahead of" if offset < 0 else "behind"
        if abs(offset) <= nh.CLOCK_GOOD_MS:
            checks.append(_check("clock", "Exporter clock", "pass",
                                 f"{abs(offset):,} ms from the appliance clock (within {nh.CLOCK_GOOD_MS // 1000} s)."))
        elif abs(offset) < nh.CLOCK_SUSPECT_MS:
            checks.append(_check("clock", "Exporter clock", "warn", f"{sec:.0f} s {direction} the appliance.",
                                 "Synchronise the exporter with NTP."))
        else:
            checks.append(_check("clock", "Exporter clock", "fail", f"{sec:.0f} s {direction} the appliance; "
                                 "flow times will be misplaced.", "Synchronise the exporter (and the appliance) with NTP."))

    loss = row.get("loss_pct_1h")
    if loss is not None:
        checks.append(_check("loss", "Export loss", "pass" if loss < nh.LOSS_WARN_PCT else "warn",
                             f"{loss:g}% of export packets/records missing in the last hour (sequence gaps).",
                             None if loss < nh.LOSS_WARN_PCT else
                             "Check for congestion or policing between the exporter and the appliance, and the "
                             "collector's kernel receive buffer."))

    _mapping_check(checks, row, owner_interfaces)
    return checks


def _mapping_check(checks: list[dict], row: dict, owner_interfaces: dict | None) -> None:
    if row.get("device_id"):
        ifc = owner_interfaces or {}
        if not ifc.get("interfaces"):
            checks.append(_check("mapping", "Linked to a device", "warn",
                                 f"Linked to {row.get('device_hostname')}, which has no SNMP interface inventory; "
                                 "interface names and speeds cannot resolve.",
                                 "Enable SNMP polling on the device so its interfaces are discovered."))
        else:
            checks.append(_check("mapping", "Linked to a device", "pass",
                                 f"Linked to {row.get('device_hostname')} ({row.get('mapping_source')}); "
                                 f"{ifc.get('interfaces')} interfaces known, {ifc.get('with_speed')} with a speed."))
    else:
        checks.append(_check("mapping", "Linked to a device", "warn",
                             "Not linked to a monitored device.",
                             "Link the exporter to its device so interface names, speeds and team scoping apply."))


def overall_check_status(checks: list[dict]) -> str:
    """'stopped' (flows arrived before but the exporter is silent now), 'late'
    (the last receipt is older than the expected interval), 'failing',
    'waiting' (nothing ever arrived), 'partial' or 'ready'. A historical
    receipt never makes an exporter 'ready' or 'partial' (working)."""
    by_id = {c["id"]: c["status"] for c in checks}
    statuses = set(by_id.values())
    if by_id.get("datagrams") == "fail":
        return "stopped"
    if by_id.get("datagrams") == "warn":
        return "late"
    if "fail" in statuses:
        return "failing"
    if by_id.get("datagrams") == "pending":
        return "waiting"
    if by_id.get("records") == "pending" or "pending" in statuses:
        return "partial"
    if "warn" in statuses:
        return "partial"
    return "ready"


async def _owner_interfaces(db: AsyncSession, device_id: str | None) -> dict | None:
    if not device_id:
        return None
    r = (await db.execute(text(
        "SELECT count(*) AS n, count(*) FILTER (WHERE COALESCE(configured_speed_bps, if_speed, 0) > 0) AS s "
        "FROM device_interfaces WHERE device_id = CAST(:d AS uuid)"
    ), {"d": device_id})).first()
    return {"interfaces": int(r.n or 0), "with_speed": int(r.s or 0)}


@router.get("/exporters/{exporter}/check")
async def exporter_check(
    exporter: str,
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Onboarding live check for one exporter address: collector listening,
    first datagram, allowlist, templates, stored records, required fields,
    sampling, exporter clock, loss and device mapping. Computed fresh on every
    call (no cache), so a setup page can poll it while the device is being
    configured."""
    ip = require_exporter_visible(vis, exporter)
    now = now_utc()
    row, templates, fields, live = await asyncio.gather(
        nh.exporter_row(db, ip, now=now),
        nh.exporter_templates(ip),
        # Field presence over the newest records of this exporter only, shared
        # by the polls of the next 30 s (the Setup page polls every few seconds).
        nh.cached_exporter_fields(ip, now, timedelta(minutes=15)),
        nh.fetch_collector_live(),
    )
    live_doc = _live_summary(live, vis)
    checks = build_checks(row, fields, templates, live_doc, await _owner_interfaces(db, row.get("device_id")))
    return {
        "server_time": _to_iso_utc(now),
        "exporter": ip,
        "overall": overall_check_status(checks),
        "status": row,
        "checks": checks,
    }


# ── Setup information (CR-06) ────────────────────────────────────────────────

@router.get("/setup/info")
async def setup_info(vis: FlowVisibility = Depends(flow_visibility)):
    """What an exporter configuration needs: this appliance's addresses and
    the collector's listening ports, plus the recommended export timers."""
    live = await nh.fetch_collector_live()
    doc = live.doc or {}
    netflow_port = nh._port_of(doc.get("listen")) or nh.DEFAULT_NETFLOW_PORT
    sflow_port = nh._port_of(doc.get("sflow_listen")) or nh.DEFAULT_SFLOW_PORT
    return {
        "server_time": _to_iso_utc(now_utc()),
        "addresses": await asyncio.to_thread(nh.appliance_addresses),
        "collector": {
            "reachable": live.reachable,
            "status": doc.get("status") if live.reachable else "unknown",
            "listen": doc.get("listen"),
            "netflow_port": netflow_port,
            "sflow_port": sflow_port,
            "sflow_listening": (bool(doc.get("sflow_listen")) if live.reachable and "sflow_listen" in doc else None),
            "port_source": "collector" if doc.get("listen") else "default",
        },
        "recommended": {
            "active_timeout_s": 60,
            "inactive_timeout_s": 15,
            "template_refresh_s": 60,
            "sflow_sampling_rate": 1000,
            "sflow_polling_interval_s": 30,
        },
    }


# ── Counting (FE-12 / CR-16) ─────────────────────────────────────────────────

@router.get("/exporters/counting")
async def exporters_counting(
    minutes: int = Query(default=15, description="Recent window to measure, 5..60 minutes"),
    tz: str | None = Query(default=None, description="IANA time zone the window is labelled in"),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """How much of the traffic is observed at more than one collection point.

    Over a short recent window of raw records (visible exporters, volume
    guard applied), flows are keyed by (source, destination, ports, protocol,
    minute). A key reported by several exporters is the same traffic seen at
    several points; ``count=all`` adds every observation, ``count=edge`` keeps
    only edge-role exporters and their configured direction. The estimate is
    an approximation: NAT, sampling and flows that straddle a minute change
    keys. Original observations are never modified.
    """
    if not 5 <= minutes <= 60:
        raise HTTPException(status_code=400, detail="minutes must be between 5 and 60")
    win = resolve_window(minutes=minutes, tz=tz, max_span=timedelta(hours=1))
    meta = current_meta()
    meta.set_source("raw", win.start, win.end, 60)
    where, params = compile_filter(FlowFilter(), RAW, vis)
    policy = await load_counting_policy(db)
    edge_where, edge_params = compile_filter(FlowFilter(count="edge", counting=policy), RAW, vis)
    params.update({"start": win.start, "end": win.end})
    edge_params.update({"start": win.start, "end": win.end})
    raw = table_ref(RAW)
    inner = f"""
        SELECT cityHash64({SRC}, {DST}, src_port, dst_port, protocol, toStartOfMinute(timestamp)) AS key,
               {EXPORTER} AS e, count() AS r, sum(bytes) AS b
        FROM {raw}
        WHERE timestamp >= %(start)s AND timestamp < %(end)s AND {where}
        GROUP BY key, e
    """
    summary_q = run_query(f"""
        SELECT count() AS keys, countIf(n > 1) AS multi_keys, sum(recs) AS records,
               sumIf(recs, n > 1) AS multi_records, sum(ball) AS bytes, sum(bmax) AS bytes_unique,
               sumIf(ball, n > 1) AS multi_bytes, countIf(n = 1 AND recs > 1) AS repeat_keys,
               sumIf(recs, n = 1 AND recs > 1) AS repeat_records
        FROM (SELECT key, uniqExact(e) AS n, sum(r) AS recs, sum(b) AS ball, max(b) AS bmax
              FROM ({inner}) GROUP BY key)
    """, params, label="counting-summary")
    per_exporter_q = run_query(f"""
        SELECT {display('e')} AS exp, sum(r) AS records, sumIf(r, n > 1) AS shared_records,
               sum(b) AS bytes, sumIf(b, n > 1) AS shared_bytes
        FROM (SELECT e, r, b, count() OVER (PARTITION BY key) AS n FROM ({inner}))
        GROUP BY e ORDER BY records DESC LIMIT 200
    """, params, label="counting-exporters")
    pairs_q = run_query(f"""
        SELECT {display('p.1')} AS a, {display('p.2')} AS b, count() AS keys
        FROM (SELECT key, arraySort(groupUniqArray(e)) AS ex
              FROM ({inner}) GROUP BY key HAVING length(ex) BETWEEN 2 AND 16)
        ARRAY JOIN arrayFlatten(arrayMap(i -> arrayMap(j -> tuple(ex[i], ex[j]), range(i + 1, length(ex) + 1)),
                                         range(1, length(ex)))) AS p
        GROUP BY a, b ORDER BY keys DESC LIMIT 20
    """, params, label="counting-pairs")
    directions_q = run_query(f"""
        SELECT {display(EXPORTER)} AS exp, countIf(flow_direction = 0) AS ingress,
               countIf(flow_direction = 1) AS egress, countIf(flow_direction NOT IN (0, 1)) AS unknown
        FROM {raw}
        WHERE timestamp >= %(start)s AND timestamp < %(end)s AND {where}
        GROUP BY exp
    """, params, label="counting-directions")
    edge_q = run_query(f"""
        SELECT count() AS records, sum(bytes) AS bytes
        FROM {raw}
        WHERE timestamp >= %(start)s AND timestamp < %(end)s AND {edge_where}
    """, edge_params, label="counting-edge")
    summary, per_exp, pairs, directions, edge = await asyncio.gather(
        summary_q, per_exporter_q, pairs_q, directions_q, edge_q)

    s = summary.result_rows[0] if summary.result_rows else None
    records = int(s[2] or 0) if s else 0
    keys = int(s[0] or 0) if s else 0
    bytes_all = int(s[4] or 0) if s else 0
    bytes_unique = int(s[5] or 0) if s else 0

    def share(part, whole):
        return round(part / whole * 100, 1) if whole else None

    settings = await nh.load_exporter_settings(db)
    dirs = {canonical_ip(r[0]): {"ingress": int(r[1]), "egress": int(r[2]), "unknown": int(r[3])}
            for r in directions.result_rows}
    exporters = []
    for r in per_exp.result_rows:
        ip = canonical_ip(r[0])
        st = settings.get(ip) or {}
        exporters.append({
            "exporter": ip,
            "display_name": st.get("display_name"),
            "role": st.get("role") or "edge",
            "count_direction": st.get("count_direction") or "auto",
            "records": int(r[1] or 0), "shared_records": int(r[2] or 0),
            "shared_pct": share(int(r[2] or 0), int(r[1] or 0)),
            "bytes": int(r[3] or 0), "shared_bytes": int(r[4] or 0),
            "directions": dirs.get(ip),
            "counted_in_edge_mode": (st.get("role") or "edge") == "edge",
        })
    e = edge.result_rows[0] if edge.result_rows else (0, 0)
    edge_records, edge_bytes = int(e[0] or 0), int(e[1] or 0)
    return {
        "server_time": _to_iso_utc(now_utc()),
        "window": win.as_meta(),
        "records": records,
        "flow_keys": keys,
        "duplicate_observation_pct": share(records - keys, records),
        "multi_point_keys": int(s[1] or 0) if s else 0,
        "multi_point_key_pct": share(int(s[1] or 0) if s else 0, keys),
        "multi_point_records": int(s[3] or 0) if s else 0,
        "same_point_repeat_records": int(s[8] or 0) if s else 0,
        "bytes": bytes_all,
        "bytes_unique_estimate": bytes_unique,
        "byte_inflation_pct": share(bytes_all - bytes_unique, bytes_unique) if bytes_unique else None,
        "modes": {
            "all": {"records": records, "bytes": bytes_all},
            "edge": {"records": edge_records, "bytes": edge_bytes,
                     "records_pct": share(edge_records, records), "bytes_pct": share(edge_bytes, bytes_all),
                     "direction_known": policy.direction_known},
        },
        "exporters": exporters,
        "pairs": [{"a": canonical_ip(r[0]), "b": canonical_ip(r[1]), "flow_keys": int(r[2] or 0)}
                  for r in pairs.result_rows],
        "method": ("Flows keyed by source, destination, ports, protocol and minute; a key reported by more than "
                   "one exporter counts as a duplicate observation. NAT, sampling and minute boundaries make "
                   "this an estimate."),
    }


# ── Exporter settings (FE-12) ────────────────────────────────────────────────

class ExporterSettingsIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    role: Literal["edge", "core", "internal", "excluded"] = "edge"
    count_direction: Literal["auto", "ingress", "egress", "both"] = "auto"
    expected_interval_s: int | None = Field(default=None, ge=1, le=86400)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("display_name", "note")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


def _settings_payload(row: dict) -> dict:
    return {
        "exporter": canonical_ip(row.get("ip") or row.get("exporter_ip")),
        "display_name": row.get("display_name"),
        "role": row.get("role"),
        "count_direction": row.get("count_direction"),
        "expected_interval_s": row.get("expected_interval_s"),
        "note": row.get("note"),
        "updated_at": _to_iso_utc(row.get("updated_at")),
        "updated_by": row.get("updated_by"),
    }


def _valid_exporter(exporter: str) -> str:
    ip = canonical_ip(exporter)
    if ip is None:
        raise HTTPException(status_code=400, detail="Invalid exporter address")
    addr = ipaddress.ip_address(ip)
    if addr.is_unspecified or addr.is_multicast:
        raise HTTPException(status_code=400, detail="Invalid exporter address")
    return ip


@router.get("/exporters/settings")
async def list_exporter_settings(
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Every exporter settings row the caller may see (no row = edge, auto
    direction, learned interval)."""
    rows = await nh.load_exporter_settings(db)
    visible = [r for ip, r in sorted(rows.items(), key=lambda kv: nh._ip_sort_key(kv[0])) if vis.allows(ip)]
    names = await _users(db, {r.get("updated_by") for r in visible})
    return [{**_settings_payload(r), "updated_by_name": names.get(r.get("updated_by") or "")} for r in visible]


async def _users(db: AsyncSession, ids) -> dict[str, str]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    import uuid

    rows = (await db.execute(text("SELECT id::text AS id, username FROM users WHERE id = ANY(:ids)"),
                             {"ids": [uuid.UUID(i) for i in ids]})).all()
    return {r.id: r.username for r in rows}


@router.put("/exporters/{exporter}/settings")
async def put_exporter_settings(
    exporter: str,
    body: ExporterSettingsIn,
    user: User = Depends(require_manage),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Create or replace one exporter's settings (``netflow.manage``; the
    exporter must be visible to the caller). Audited with before/after."""
    ip = _valid_exporter(exporter)
    require_exporter_visible(vis, ip)
    before = (await nh.load_exporter_settings(db)).get(ip)
    row = (await db.execute(text("""
        INSERT INTO netflow_exporter_settings
            (exporter_ip, display_name, role, count_direction, expected_interval_s, note, created_by, updated_by)
        VALUES (CAST(:ip AS inet), :name, :role, :dir, :interval, :note, CAST(:uid AS uuid), CAST(:uid AS uuid))
        ON CONFLICT (exporter_ip) DO UPDATE SET
            display_name = EXCLUDED.display_name, role = EXCLUDED.role,
            count_direction = EXCLUDED.count_direction, expected_interval_s = EXCLUDED.expected_interval_s,
            note = EXCLUDED.note, updated_by = EXCLUDED.updated_by, updated_at = NOW()
        RETURNING host(exporter_ip) AS ip, display_name, role, count_direction, expected_interval_s, note,
                  updated_at, updated_by::text AS updated_by
    """), {"ip": ip, "name": body.display_name, "role": body.role, "dir": body.count_direction,
           "interval": body.expected_interval_s, "note": body.note, "uid": str(user.id)})).mappings().first()
    from app.services.audit_service import write_audit_log

    after = _settings_payload(dict(row))
    await write_audit_log(
        db, actor=user, action="update", resource_type="netflow_exporter_settings", resource_id=ip,
        metadata={"before": _settings_payload(before) if before else None,
                  "after": {k: v for k, v in after.items() if k not in ("updated_at", "updated_by")}},
    )
    await db.commit()
    invalidate_counting_cache()
    nh.invalidate_status_cache()
    invalidate_expected_intervals()
    return after


@router.delete("/exporters/{exporter}/settings", status_code=204)
async def delete_exporter_settings(
    exporter: str,
    user: User = Depends(require_manage),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Remove one exporter's settings (back to edge / auto / learned). 404
    when it has none; nothing is audited then."""
    ip = _valid_exporter(exporter)
    require_exporter_visible(vis, ip)
    before = (await nh.load_exporter_settings(db)).get(ip)
    if before is None:
        raise HTTPException(status_code=404, detail="This exporter has no settings")
    await db.execute(text("DELETE FROM netflow_exporter_settings WHERE exporter_ip = CAST(:ip AS inet)"), {"ip": ip})
    from app.services.audit_service import write_audit_log

    await write_audit_log(db, actor=user, action="delete", resource_type="netflow_exporter_settings",
                          resource_id=ip, metadata={"before": _settings_payload(before)})
    await db.commit()
    invalidate_counting_cache()
    nh.invalidate_status_cache()
    invalidate_expected_intervals()
