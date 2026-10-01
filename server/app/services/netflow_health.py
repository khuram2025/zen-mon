"""NetFlow collector and exporter health (NF-01, FE-01 / CR-05).

One question, answered on the appliance clock only: *are this exporter's flows
reaching the collector, and can the collector use them?* Nothing here depends
on a dashboard window, a page filter or the browser clock (NF-01, X-04, X-24).

Sources, all written by the collector host (so every timestamp is the
appliance's):

* ``flow_exporter_minutes`` -- per minute of **receipt**: records persisted,
  field availability, first/last ``received_at`` and flow-time lag. Receipt age
  and the learned export cadence come from here.
* ``flow_collector_stats`` -- per 60 s interval and (exporter, observation
  domain, export protocol): datagrams, decoded/written records, drops by
  reason, parse errors, data sets waiting for a template, sequence gaps, the
  median exporter clock offset, sampling and templates; plus one process row
  (exporter ``''``) with queue, spool, insert failures/retries and kernel
  receive-buffer drops. Datagrams whose records could not be decoded (no
  template yet) still prove receipt.
* the collector's live ``/health`` JSON (``NETFLOW_COLLECTOR_HEALTH_URL``,
  default ``http://127.0.0.1:8091/health``): the precise last datagram per
  exporter and the process status (ok / degraded / down). Optional -- the
  persisted stats answer when it cannot be reached.

State (contract §11) per exporter, from the receipt age and the exporter's
expected interval (configured in ``netflow_exporter_settings``, else the p95 of
its observed inter-arrival gaps over 24 h, else 60 s):

    receiving  age <= late_after     late_after  = max(120, 2 x expected + 60) s
    late       age <= silent_after   silent_after = max(900, 10 x expected) s
    silent     older
    never      no receipt on record (a configured or mapped exporter that has
               not sent anything the collector kept)

Silence is exporter silence, never a device-reachability claim (NF-23): the
owner device's own polled status is reported next to it as ``device_status``.

``netflow_health_loop`` recomputes the status every minute under an advisory
lock, keeps the time each exporter entered its current state
(``state_since``, system setting ``netflow_exporter_state``) and warms the
per-process cache the API serves from.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.netflow.access import (
    FlowVisibility,
    canonical_ip,
    exporters_predicate,
    resolve_exporter_owners,
)
from app.api.v1.netflow.common import (
    BUDGET_BACKGROUND,
    BUDGET_INTERACTIVE,
    EXPORTER_MINUTES,
    INTERFACE_5M,
    RAW,
    NetflowError,
    _to_iso_utc,
    now_utc,
    run_query,
    table_ref,
)
from app.api.v1.netflow.sql import EXPORTER

logger = logging.getLogger("zenplus.netflow.health")

COLLECTOR_STATS = "flow_collector_stats"
TEMPLATES = "flow_templates"

# ── Configuration ────────────────────────────────────────────────────────────

COLLECTOR_HEALTH_URL_ENV = "NETFLOW_COLLECTOR_HEALTH_URL"
DEFAULT_COLLECTOR_HEALTH_URL = "http://127.0.0.1:8091/health"
COLLECTOR_TIMEOUT_S = 1.5
LIVE_CACHE_TTL_S = 3.0
STATUS_CACHE_TTL_S = 10.0

DEFAULT_NETFLOW_PORT = 2055
DEFAULT_SFLOW_PORT = 6343

# Receipt-state thresholds (seconds). Receipt minutes are whole minutes and the
# collector flushes every few seconds, so the late threshold carries a minute
# of slack on top of two missed intervals.
DEFAULT_EXPECTED_S = 60
MIN_LATE_S = 120
MIN_SILENT_S = 900
LEARN_MIN_GAPS = 10          # fewer inter-arrival samples than this: not learned
LEARN_WINDOW = timedelta(hours=24)
RECENT_WINDOW = timedelta(days=30)   # exporters listed without a settings/mapping row

# Exporter clock: offset = received - export header time (collector convention);
# negative = the exporter's clock is ahead of the appliance.
CLOCK_SUSPECT_MS = 60_000
CLOCK_GOOD_MS = 5_000
LOSS_WARN_PCT = 1.0

HEALTH_LOOP_INTERVAL_S = 60.0
HEALTH_LOOP_STARTUP_DELAY_S = 20.0
NETFLOW_HEALTH_ADVISORY_LOCK = 1515074616
STATE_SETTING_KEY = "netflow_exporter_state"

STATES = ("receiving", "late", "silent", "never")


def collector_health_url() -> str:
    """Where the collector serves its health JSON (``NETFLOW_COLLECTOR_HEALTH_URL``)."""
    url = os.environ.get(COLLECTOR_HEALTH_URL_ENV, "").strip()
    if not url:
        try:
            from app.core.config import settings

            url = str(getattr(settings, COLLECTOR_HEALTH_URL_ENV, "") or "").strip()
        except Exception:  # pragma: no cover - settings import failure
            url = ""
    return url or DEFAULT_COLLECTOR_HEALTH_URL


# ── Pure rules ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Thresholds:
    expected_s: int
    basis: str              # 'configured' | 'learned' | 'default'
    late_after_s: int
    silent_after_s: int


def thresholds_for(configured_s: int | None, learned_s: float | None) -> Thresholds:
    """Late/silent thresholds from the configured expected interval, else the
    learned p95 inter-arrival gap, else the 60 s default."""
    if configured_s:
        expected, basis = int(configured_s), "configured"
    elif learned_s:
        expected, basis = max(DEFAULT_EXPECTED_S, int(math.ceil(learned_s))), "learned"
    else:
        expected, basis = DEFAULT_EXPECTED_S, "default"
    late = max(MIN_LATE_S, 2 * expected + 60)
    silent = max(MIN_SILENT_S, 10 * expected, late + 60)
    return Thresholds(expected_s=expected, basis=basis, late_after_s=late, silent_after_s=silent)


DEFAULT_THRESHOLDS = thresholds_for(None, None)


def exporter_state(age_s: float | None, th: Thresholds) -> str:
    """'receiving' | 'late' | 'silent' | 'never' for a receipt age (seconds)."""
    if age_s is None:
        return "never"
    if age_s <= th.late_after_s:
        return "receiving"
    if age_s <= th.silent_after_s:
        return "late"
    return "silent"


def learned_interval(gaps_s: Iterable[float]) -> float | None:
    """p95 of the inter-arrival gaps (seconds), None with too few samples."""
    gaps = sorted(g for g in gaps_s if g and g > 0)
    if len(gaps) < LEARN_MIN_GAPS:
        return None
    return float(gaps[max(0, math.ceil(0.95 * len(gaps)) - 1)])


def loss_pct(gaps: int | None, units: int | None) -> float | None:
    """Sequence loss in percent: missing / (received + missing). None when
    nothing with a sequence number was received."""
    gaps = int(gaps or 0)
    units = int(units or 0)
    if units + gaps <= 0:
        return None
    return round(gaps / (units + gaps) * 100, 2)


def clock_assessment(offset_ms: int | float | None) -> tuple[bool | None, str | None]:
    """(suspect, direction) for an exporter clock offset (received - header)."""
    if offset_ms is None:
        return None, None
    if abs(offset_ms) < CLOCK_SUSPECT_MS:
        return False, None
    return True, ("ahead" if offset_ms < 0 else "behind")


# ── fields_present bits (contract §4.2) and the field registry ──────────────

FIELD_BITS: dict[int, str] = {
    0: "tcp_flags", 1: "tos", 2: "l4_ports", 3: "input_if", 4: "output_if",
    5: "flow_times", 6: "as", 7: "masks", 8: "next_hop", 9: "direction",
    10: "app_id", 11: "icmp_type", 12: "nat", 13: "fw_event", 14: "sampling_inline",
    15: "vlan", 27: "sampling_ambiguous", 28: "clock_clamped", 29: "sampled_sflow", 30: "biflow_reverse",
    31: "tracked",
}

# Capabilities shown per exporter: key -> (label, SQL: records it applies to,
# SQL: records that carried it, what depends on it). ``fp`` is fields_present.
CAPABILITIES: tuple[tuple[str, str, str, str, str], ...] = (
    ("tcp_flags", "TCP flags", "protocol = 6", "bitTest(fp, 0)",
     "TCP flag analysis, SYN-scan and RST detectors"),
    ("l4_ports", "Transport ports", "protocol IN (6, 17, 132)", "bitTest(fp, 2)",
     "services, applications and port views"),
    ("interfaces", "Interface indexes", "1", "bitTest(fp, 3) OR bitTest(fp, 4)",
     "interface views, capacity and exporter mapping"),
    ("flow_times", "Flow start/end times", "1", "bitTest(fp, 5)",
     "flow durations and time-spread capacity"),
    ("direction", "Flow direction (IE 61)", "1", "bitTest(fp, 9)",
     "automatic edge counting"),
    ("app_id", "Application ID (NBAR2 / App-ID)", "1", "bitTest(fp, 10)",
     "exporter-named applications"),
    ("tos", "ToS / DSCP", "1", "bitTest(fp, 1)", "DSCP / QoS views"),
    ("as", "BGP AS numbers", "1", "bitTest(fp, 6)", "AS views"),
    ("next_hop", "Next hop", "1", "bitTest(fp, 8)", "next-hop views"),
    ("nat", "NAT addresses", "1", "bitTest(fp, 12)", "post-NAT forensics"),
    ("fw_event", "Firewall events (NSEL)", "1", "bitTest(fp, 13)", "firewall event forensics"),
    ("vlan", "VLAN", "1", "bitTest(fp, 15)", "VLAN context in forensics"),
)
REQUIRED_CAPABILITIES = frozenset({"l4_ports", "interfaces", "flow_times", "tcp_flags"})


def capability_sql() -> str:
    """SELECT expressions: for each capability, (applicable, present) counts
    over tracked records (``fields_present`` bit 31; legacy rows say nothing)."""
    parts = []
    for key, _label, applies, present, _used in CAPABILITIES:
        parts.append(f"countIf(bitTest(fp, 31) AND ({applies})) AS {key}_n")
        parts.append(f"countIf(bitTest(fp, 31) AND ({applies}) AND ({present})) AS {key}_y")
    return ",\n               ".join(parts)


def capability_status(counts: dict[str, tuple[int, int]], tracked: int) -> list[dict]:
    """[{key, label, status, pct, used_for}]: 'all' / 'some' / 'none' records
    carried it, 'n/a' when no record it applies to arrived (no TCP traffic for
    TCP flags), 'unknown' without tracked (post-upgrade) records."""
    out = []
    for key, label, _a, _p, used_for in CAPABILITIES:
        n, y = counts.get(key, (0, 0))
        if not tracked:
            status, pct = "unknown", None
        elif n == 0:
            status, pct = "n/a", None
        else:
            pct = round(y / n * 100, 1)
            status = "all" if y == n else ("none" if y == 0 else "some")
        out.append({"key": key, "label": label, "status": status, "pct": pct, "used_for": used_for,
                    "recommended": key in REQUIRED_CAPABILITIES})
    return out


# IPFIX information elements / NetFlow v9 field types (the two coincide for
# these), with what the collector does with them.
IE_NAMES: dict[int, str] = {
    1: "octetDeltaCount", 2: "packetDeltaCount", 3: "deltaFlowCount", 4: "protocolIdentifier",
    5: "ipClassOfService", 6: "tcpControlBits", 7: "sourceTransportPort", 8: "sourceIPv4Address",
    9: "sourceIPv4PrefixLength", 10: "ingressInterface", 11: "destinationTransportPort",
    12: "destinationIPv4Address", 13: "destinationIPv4PrefixLength", 14: "egressInterface",
    15: "ipNextHopIPv4Address", 16: "bgpSourceAsNumber", 17: "bgpDestinationAsNumber",
    18: "bgpNextHopIPv4Address", 19: "postMCastPacketDeltaCount", 20: "postMCastOctetDeltaCount",
    21: "flowEndSysUpTime", 22: "flowStartSysUpTime", 23: "postOctetDeltaCount",
    24: "postPacketDeltaCount", 25: "minimumIpTotalLength", 26: "maximumIpTotalLength",
    27: "sourceIPv6Address", 28: "destinationIPv6Address", 29: "sourceIPv6PrefixLength",
    30: "destinationIPv6PrefixLength", 31: "flowLabelIPv6", 32: "icmpTypeCodeIPv4",
    33: "igmpType", 34: "samplingInterval", 35: "samplingAlgorithm", 36: "flowActiveTimeout",
    37: "flowIdleTimeout", 38: "engineType", 39: "engineId", 40: "exportedOctetTotalCount",
    41: "exportedMessageTotalCount", 42: "exportedFlowRecordTotalCount", 44: "sourceIPv4Prefix",
    45: "destinationIPv4Prefix", 46: "mplsTopLabelType", 47: "mplsTopLabelIPv4Address",
    48: "samplerId", 49: "samplerMode", 50: "samplerRandomInterval", 51: "classId",
    52: "minimumTTL", 53: "maximumTTL", 54: "fragmentIdentification", 55: "postIpClassOfService",
    56: "sourceMacAddress", 57: "postDestinationMacAddress", 58: "vlanId", 59: "postVlanId",
    60: "ipVersion", 61: "flowDirection", 62: "ipNextHopIPv6Address", 63: "bgpNextHopIPv6Address",
    64: "ipv6ExtensionHeaders", 70: "mplsTopLabelStackSection", 80: "destinationMacAddress",
    81: "postSourceMacAddress", 82: "interfaceName", 83: "interfaceDescription",
    85: "octetTotalCount", 86: "packetTotalCount", 88: "fragmentOffset", 89: "forwardingStatus",
    90: "mplsVpnRouteDistinguisher", 94: "applicationDescription", 95: "applicationId",
    96: "applicationName", 98: "postIpDiffServCodePoint", 128: "bgpNextAdjacentAsNumber",
    129: "bgpPrevAdjacentAsNumber", 130: "exporterIPv4Address", 131: "exporterIPv6Address",
    136: "flowEndReason", 137: "commonPropertiesId", 138: "observationPointId",
    139: "icmpTypeCodeIPv6", 144: "exportingProcessId", 148: "flowId", 149: "observationDomainId",
    150: "flowStartSeconds", 151: "flowEndSeconds", 152: "flowStartMilliseconds",
    153: "flowEndMilliseconds", 154: "flowStartMicroseconds", 155: "flowEndMicroseconds",
    156: "flowStartNanoseconds", 157: "flowEndNanoseconds", 158: "flowStartDeltaMicroseconds",
    159: "flowEndDeltaMicroseconds", 160: "systemInitTimeMilliseconds", 161: "flowDurationMilliseconds",
    176: "icmpTypeIPv4", 177: "icmpCodeIPv4", 178: "icmpTypeIPv6", 179: "icmpCodeIPv6",
    180: "udpSourcePort", 181: "udpDestinationPort", 182: "tcpSourcePort",
    183: "tcpDestinationPort", 184: "tcpSequenceNumber", 185: "tcpAcknowledgementNumber",
    186: "tcpWindowSize", 192: "ipTTL", 195: "ipDiffServCodePoint", 196: "ipPrecedence",
    197: "fragmentFlags", 198: "octetDeltaSumOfSquares", 205: "udpMessageLength",
    206: "isMulticast", 210: "paddingOctets", 224: "ipTotalLength", 225: "postNATSourceIPv4Address",
    226: "postNATDestinationIPv4Address", 227: "postNAPTSourceTransportPort",
    228: "postNAPTDestinationTransportPort", 230: "natEvent", 231: "initiatorOctets",
    232: "responderOctets", 233: "firewallEvent", 234: "ingressVRFID", 235: "egressVRFID",
    239: "biflowDirection", 243: "dot1qVlanId", 252: "ingressPhysicalInterface",
    253: "egressPhysicalInterface", 256: "ethernetType", 281: "postNATSourceIPv6Address",
    282: "postNATDestinationIPv6Address", 298: "initiatorPackets", 299: "responderPackets",
    302: "selectorId", 305: "samplingPacketInterval", 306: "samplingPacketSpace",
    309: "samplingSize", 310: "samplingPopulation", 311: "samplingProbability",
    323: "observationTimeMilliseconds", 346: "privateEnterpriseNumber", 372: "applicationCategoryName",
    40000: "NF_F_USERNAME (NSEL)", 40001: "NF_F_XLATE_SRC_ADDR_IPV4 (NSEL)",
    40002: "NF_F_XLATE_DST_ADDR_IPV4 (NSEL)", 40003: "NF_F_XLATE_SRC_PORT (NSEL)",
    40004: "NF_F_XLATE_DST_PORT (NSEL)", 40005: "NF_F_FW_EXT_EVENT (NSEL)",
}

# Fields the collector decodes into flow_records (anything else is ignored).
DECODED_IES = frozenset({
    1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 21, 22, 23, 24, 27, 28, 29, 30,
    32, 34, 35, 48, 49, 50, 61, 62, 63, 85, 86, 95, 136, 139, 150, 151, 152, 153, 154, 155, 156,
    157, 158, 159, 160, 176, 177, 178, 179, 180, 181, 182, 183, 195, 225, 226, 227, 228, 231, 232,
    233, 252, 253, 281, 282, 298, 299, 302, 305, 306, 309, 310, 311, 323, 40005,
})


def field_name(ie: int, enterprise: int = 0) -> str:
    if enterprise:
        return f"enterprise {enterprise} field {ie}"
    return IE_NAMES.get(int(ie), f"field {ie}")


def decode_bits(value: int) -> list[str]:
    return [name for bit, name in FIELD_BITS.items() if int(value) & (1 << bit)]


# ── Collector live health ────────────────────────────────────────────────────

@dataclass
class CollectorLive:
    reachable: bool
    doc: dict | None
    error: str | None      # 'unreachable' | 'timeout' | 'bad_response' | None
    fetched_at: datetime


_live_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _parse_ts(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        # Go RFC3339Nano: trim sub-microsecond digits for fromisoformat.
        m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", s)
        if m:
            frac = (m.group(2) or "")[:7]
            tz = m.group(3) or "Z"
            s = f"{m.group(1)}{frac}{'+00:00' if tz == 'Z' else tz}"
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return None if dt.year < 2000 else dt


async def fetch_collector_live(*, refresh: bool = False) -> CollectorLive:
    """The collector's health JSON (cached a few seconds). Never raises."""
    cached = _live_cache["value"]
    if not refresh and cached is not None and time.monotonic() - _live_cache["at"] < LIVE_CACHE_TTL_S:
        return cached
    import httpx

    now = now_utc()
    try:
        async with httpx.AsyncClient(timeout=COLLECTOR_TIMEOUT_S) as client:
            resp = await client.get(collector_health_url())
        try:
            doc = resp.json()
        except ValueError:
            doc = None
        # 503 still carries the document (status 'down').
        if not isinstance(doc, dict) or "status" not in doc:
            value = CollectorLive(False, None, "bad_response", now)
        else:
            value = CollectorLive(True, doc, None, now)
    except httpx.TimeoutException:
        value = CollectorLive(False, None, "timeout", now)
    except Exception:
        logger.debug("netflow collector health unreachable", exc_info=True)
        value = CollectorLive(False, None, "unreachable", now)
    _live_cache.update(at=time.monotonic(), value=value)
    return value


def _port_of(listen: str | None) -> int | None:
    if not listen:
        return None
    m = re.search(r":(\d+)$", str(listen).strip())
    return int(m.group(1)) if m else None


def live_exporters(live: CollectorLive | None) -> dict[str, dict]:
    """{canonical exporter -> merged live stream facts} from the health JSON."""
    out: dict[str, dict] = {}
    if not live or not live.doc:
        return out
    for s in live.doc.get("exporters") or []:
        ip = canonical_ip(s.get("exporter"))
        if ip is None:
            continue
        cur = out.setdefault(ip, {"last_datagram_at": None, "clock_offset_ms": None, "streams": []})
        at = _parse_ts(s.get("last_datagram_at"))
        if at and (cur["last_datagram_at"] is None or at > cur["last_datagram_at"]):
            cur["last_datagram_at"] = at
            if s.get("clock_offset_ms") is not None:
                cur["clock_offset_ms"] = int(s["clock_offset_ms"])
        cur["streams"].append(s)
    return out


# ── Settings, owners and stored state (PostgreSQL) ───────────────────────────

async def load_exporter_settings(db: AsyncSession) -> dict[str, dict]:
    """{canonical exporter -> netflow_exporter_settings row}."""
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT host(exporter_ip) AS ip, display_name, role, count_direction, "
                "expected_interval_s, note, updated_at, updated_by::text AS updated_by "
                "FROM netflow_exporter_settings"
            ))).mappings().all()
    except Exception:
        logger.warning("netflow_exporter_settings unavailable", exc_info=True)
        return {}
    out = {}
    for r in rows:
        ip = canonical_ip(r["ip"])
        if ip:
            out[ip] = dict(r) | {"ip": ip}
    return out


async def _device_status(db: AsyncSession, ids: Iterable[str]) -> dict[str, dict]:
    import uuid

    uuids = sorted({uuid.UUID(str(i)) for i in ids if i})
    if not uuids:
        return {}
    rows = (await db.execute(text(
        "SELECT id::text AS id, status, last_seen, host(ip_address) AS ip FROM devices "
        "WHERE id = ANY(:ids)"
    ), {"ids": uuids})).mappings().all()
    return {r["id"]: dict(r) for r in rows}


_state_cache: dict[str, Any] = {"at": 0.0, "value": None}


async def load_state_since(db: AsyncSession, *, refresh: bool = False) -> dict[str, dict]:
    cached = _state_cache["value"]
    if not refresh and cached is not None and time.monotonic() - _state_cache["at"] < 15.0:
        return cached
    try:
        async with db.begin_nested():
            value = (await db.execute(
                text("SELECT value FROM system_settings WHERE key = :k"), {"k": STATE_SETTING_KEY}
            )).scalar()
    except Exception:
        value = None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    states = (value or {}).get("exporters") if isinstance(value, dict) else None
    result = states if isinstance(states, dict) else {}
    _state_cache.update(at=time.monotonic(), value=result)
    return result


# ── ClickHouse readers ───────────────────────────────────────────────────────

async def _receipts(since: datetime, t15: datetime, t60: datetime, *, budget: float,
                    exporters: list[str] | None = None) -> dict[str, dict]:
    """Per exporter, from flow_exporter_minutes since ``since``."""
    pred, params = ("1", {})
    if exporters is not None:
        pred, params = exporters_predicate(exporters, param="nf_hx")
    params.update({"since": since, "t15": t15, "t60": t60})
    res = await run_query(
        f"""
        SELECT {EXPORTER} AS e,
               max(last_received_at) AS last_rx,
               min(first_received_at) AS first_rx,
               sumIf(records, minute >= %(t15)s) AS rec15,
               sumIf(records, minute >= %(t60)s) AS rec60,
               sumIf(tcp_records, minute >= %(t60)s) AS tcp60,
               sumIf(tcp_flagged_records, minute >= %(t60)s) AS flagged60,
               sumIf(legacy_records, minute >= %(t60)s) AS legacy60,
               sumIf(sampled_records, minute >= %(t60)s) AS sampled60,
               maxIfOrNull(max_sampling, minute >= %(t60)s) AS maxsamp60,
               minIfOrNull(lag_ms_min, minute >= %(t15)s) AS lagmin15,
               groupUniqArray(observation_domain) AS domains,
               sumIf(v6_records, minute >= %(t60)s) AS v660
        FROM {table_ref(EXPORTER_MINUTES)}
        WHERE minute >= %(since)s AND {pred}
        GROUP BY e
        """,
        params, budget_s=budget, label="exporter-receipts",
    )
    out = {}
    for r in res.result_rows:
        ip = canonical_ip(r[0])
        if ip is None:
            continue
        out[ip] = {
            "last_rx": _parse_ts(r[1]), "first_rx": _parse_ts(r[2]),
            "rec15": int(r[3] or 0), "rec60": int(r[4] or 0),
            "tcp60": int(r[5] or 0), "flagged60": int(r[6] or 0), "legacy60": int(r[7] or 0),
            "sampled60": int(r[8] or 0), "maxsamp60": int(r[9]) if r[9] is not None else None,
            "lagmin15": int(r[10]) if r[10] is not None else None,
            "domains": sorted(int(d) for d in (r[11] or [])), "v660": int(r[12] or 0),
        }
    return out


async def _learned_gaps(since: datetime, *, budget: float, exporters: list[str] | None = None) -> dict[str, float | None]:
    """Learned export cadence: p95 of the gaps between receipt minutes, where
    a minute counts when records were stored (flow_exporter_minutes) or
    datagrams arrived (flow_collector_stats -- data waiting for a template is
    still a delivery)."""
    pred, params = ("1", {})
    if exporters is not None:
        pred, params = exporters_predicate(exporters, param="nf_hx")
    params["since"] = since
    minutes_q = run_query(
        f"""
        SELECT {EXPORTER} AS e, groupUniqArray(toUInt32(minute)) AS m
        FROM {table_ref(EXPORTER_MINUTES)}
        WHERE minute >= %(since)s AND {pred}
        GROUP BY e
        """,
        params, budget_s=budget, label="exporter-intervals",
    )
    sparams: dict = {"since": since}
    spred = _exporter_pred(exporters, sparams)
    stats_q = run_query(
        f"""
        SELECT exporter, groupUniqArray(toUInt32(toStartOfMinute(ts))) AS m
        FROM {table_ref(COLLECTOR_STATS)}
        WHERE ts >= %(since)s AND datagrams > 0 AND {spred}
        GROUP BY exporter
        """,
        sparams, budget_s=budget, label="exporter-intervals-stats",
    )
    minutes_res, stats_res = await asyncio.gather(minutes_q, stats_q, return_exceptions=True)
    if isinstance(minutes_res, BaseException):
        raise minutes_res
    merged: dict[str, set[int]] = {}
    for res in (minutes_res, stats_res):
        if isinstance(res, BaseException):
            continue
        for r in res.result_rows:
            ip = canonical_ip(r[0])
            if ip is not None:
                merged.setdefault(ip, set()).update(int(x) for x in (r[1] or []))
    out = {}
    for ip, minutes in merged.items():
        ordered = sorted(minutes)
        out[ip] = learned_interval(b - a for a, b in zip(ordered, ordered[1:]))
    return out


_STATS_COLUMNS = (
    "datagrams", "datagram_bytes", "records_decoded", "records_written",
    "records_dropped_implausible", "records_dropped_queue", "records_dropped_unknown_exporter",
    "records_unsupported", "parse_errors", "data_sets_waiting_template", "options_records",
    "sequence_gaps", "clock_clamped_records", "insert_failures", "insert_retries",
    "kernel_rcvbuf_errors", "sampling_ambiguous_records",
)

# Sequence units (the loss denominator): v9 and sFlow count datagrams, v5 and
# IPFIX count records as the exporter numbered them. The collector writes that
# count as sequence_units; records_decoded is only a fallback for rows written
# before the column existed, because a bidirectional record (IPFIX responder or
# RFC 5103 reverse counters) is decoded into two rows but numbered as one.
_SEQ_UNITS = ("if(sequence_units > 0, sequence_units, "
              "if(export_protocol IN ('netflow9', 'sflow5'), datagrams, "
              "records_decoded + options_records + records_unsupported))")
# sFlow carries no export time, so its streams write clock_offset_ms = 0: never
# evidence of a clock, in either direction.
_HAS_CLOCK = "datagrams > 0 AND export_protocol != 'sflow5'"


async def _collector_stats(since: datetime, t15: datetime, t60: datetime, *, budget: float,
                           exporters: list[str] | None = None) -> dict[str, dict] | None:
    """Per exporter from flow_collector_stats; None when the table cannot be read."""
    params: dict = {"since": since, "t15": t15, "t60": t60}
    pred = "exporter != ''"
    if exporters is not None:
        pred += " AND exporter IN %(nf_hx)s"
        params["nf_hx"] = list(exporters) or [""]
    try:
        res = await run_query(
            f"""
            SELECT exporter,
                   groupUniqArrayIf(export_protocol, datagrams > 0) AS protocols,
                   groupUniqArrayIf(observation_domain, datagrams > 0) AS domains,
                   maxIfOrNull(ts, datagrams > 0) AS last_dgram,
                   sumIf(datagrams, ts >= %(t15)s) AS dg15,
                   sumIf(records_decoded, ts >= %(t15)s) AS dec15,
                   sumIf(records_written, ts >= %(t15)s) AS wr15,
                   sumIf(data_sets_waiting_template, ts >= %(t15)s) AS wait15,
                   sumIf(records_dropped_unknown_exporter, ts >= %(t15)s) AS unk15,
                   sumIf(parse_errors, ts >= %(t60)s) AS perr60,
                   sumIf(records_unsupported, ts >= %(t60)s) AS unsup60,
                   sumIf(records_dropped_implausible, ts >= %(t60)s) AS impl60,
                   sumIf(records_dropped_queue, ts >= %(t60)s) AS q60,
                   sumIf(sequence_gaps, ts >= %(t60)s) AS gaps60,
                   sumIf({_SEQ_UNITS}, ts >= %(t60)s) AS units60,
                   sumIf(clock_clamped_records, ts >= %(t60)s) AS clamped60,
                   quantileExactIfOrNull(0.5)(clock_offset_ms, ts >= %(t15)s AND {_HAS_CLOCK}) AS offset15,
                   maxIfOrNull(sampling_interval, ts >= %(t60)s AND datagrams > 0) AS samp60,
                   sumIf(datagrams, ts >= %(t60)s) AS dg60,
                   sumIf(records_decoded, ts >= %(t60)s) AS dec60,
                   sumIf(records_written, ts >= %(t60)s) AS wr60,
                   sumIf(sampling_ambiguous_records, ts >= %(t60)s) AS amb60
            FROM {table_ref(COLLECTOR_STATS)}
            WHERE ts >= %(since)s AND {pred}
            GROUP BY exporter
            """,
            params, budget_s=budget, label="collector-stats",
        )
    except NetflowError:
        logger.warning("netflow: flow_collector_stats unreadable", exc_info=True)
        return None
    out = {}
    for r in res.result_rows:
        ip = canonical_ip(r[0])
        if ip is None:
            continue
        cur = out.setdefault(ip, {})
        cur.update({
            "protocols": sorted(set(cur.get("protocols", [])) | {p for p in (r[1] or []) if p}),
            "domains": sorted(set(cur.get("domains", [])) | {int(d) for d in (r[2] or [])}),
            "last_dgram": _parse_ts(r[3]),
            "dg15": int(r[4] or 0), "dec15": int(r[5] or 0), "wr15": int(r[6] or 0),
            "wait15": int(r[7] or 0), "unk15": int(r[8] or 0), "perr60": int(r[9] or 0),
            "unsup60": int(r[10] or 0), "impl60": int(r[11] or 0), "q60": int(r[12] or 0),
            "gaps60": int(r[13] or 0), "units60": int(r[14] or 0), "clamped60": int(r[15] or 0),
            "offset15": int(r[16]) if r[16] is not None else None,
            "samp60": int(r[17]) if r[17] is not None else None,
            "dg60": int(r[18] or 0), "dec60": int(r[19] or 0), "wr60": int(r[20] or 0),
            "amb60": int(r[21] or 0) if len(r) > 21 else 0,
        })
    return out


async def _template_flags(*, budget: float, exporters: list[str] | None = None) -> dict[str, dict]:
    """Per exporter: data-template count and whether any/all carry TCP flags."""
    params: dict = {}
    pred = "is_options = 0"
    if exporters is not None:
        pred += " AND exporter IN %(nf_hx)s"
        params["nf_hx"] = list(exporters) or [""]
    try:
        res = await run_query(
            f"""
            SELECT exporter, count() AS n, sum(has_tcp_flags) AS with_flags, max(last_seen) AS last_seen
            FROM {table_ref(TEMPLATES)} FINAL
            WHERE {pred}
            GROUP BY exporter
            """,
            params, budget_s=budget, label="template-flags",
        )
    except NetflowError:
        return {}
    out = {}
    for r in res.result_rows:
        ip = canonical_ip(r[0])
        if ip:
            out[ip] = {"templates": int(r[1] or 0), "with_flags": int(r[2] or 0),
                       "last_template": _parse_ts(r[3])}
    return out


# ── Status assembly ──────────────────────────────────────────────────────────

def _issue(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def _flags_exported(rc: dict | None, tpl: dict | None, protocols: list[str]) -> tuple[bool | None, float | None]:
    if rc and rc["tcp60"] > 0:
        pct = round(rc["flagged60"] / rc["tcp60"] * 100, 1)
        return rc["flagged60"] > 0, pct
    if tpl and tpl["templates"]:
        return tpl["with_flags"] > 0, None
    if "netflow5" in protocols or "sflow5" in protocols:
        return True, None
    return None, None


def build_row(
    ip: str,
    *,
    now: datetime,
    rc: dict | None,
    st: dict | None,
    live: dict | None,
    learned: float | None,
    tpl: dict | None,
    setting: dict | None,
    owner,
    device: dict | None,
    since: dict | None,
) -> dict:
    """One contract §11 row (plus B6 extras) from the per-source facts."""
    last_record = rc["last_rx"] if rc else None
    last_dgram = None
    if live and live.get("last_datagram_at"):
        last_dgram = live["last_datagram_at"]
    elif st and st.get("last_dgram"):
        last_dgram = st["last_dgram"]
    candidates = [t for t in (last_record, last_dgram) if t is not None]
    last_rx = max(candidates) if candidates else None
    age = max(0.0, (now - last_rx).total_seconds()) if last_rx else None
    th = thresholds_for(setting.get("expected_interval_s") if setting else None, learned)
    state = exporter_state(age, th)

    offset = None
    if live and live.get("clock_offset_ms") is not None:
        offset = live["clock_offset_ms"]
    elif st and st.get("offset15") is not None:
        offset = st["offset15"]
    elif rc and rc.get("lagmin15") is not None and rc["lagmin15"] < -CLOCK_SUSPECT_MS:
        offset = rc["lagmin15"]           # flows stamped in the future: at least this far ahead
    suspect, direction = clock_assessment(offset)

    protocols = sorted(set((st or {}).get("protocols") or []))
    if live:
        protocols = sorted(set(protocols) | {s.get("export_protocol") for s in live.get("streams", []) if s.get("export_protocol")})
    flags, flag_pct = _flags_exported(rc, tpl, protocols)
    loss = loss_pct(st["gaps60"], st["units60"]) if st else None
    sampling = None
    if st and st.get("samp60"):
        sampling = st["samp60"]
    if rc and rc.get("maxsamp60"):
        sampling = max(sampling or 0, rc["maxsamp60"])

    issues: list[dict] = []
    if st:
        if st["unk15"] > 0:
            issues.append(_issue("rejected_allowlist", "danger",
                                 "The collector rejects this exporter: it is not in NETFLOW_ALLOWED_EXPORTERS."))
        if st["dg15"] > 0 and st["wait15"] > 0 and st["dec15"] == 0:
            issues.append(_issue("waiting_template", "warning",
                                 "Datagrams arrive but no template has been received for them yet, so their "
                                 "records cannot be decoded. The exporter re-sends templates on its template "
                                 "refresh timer."))
        elif st["wait15"] > 0:
            issues.append(_issue("template_gaps", "info",
                                 f"{st['wait15']} data sets arrived before their template in the last 15 minutes."))
        if st["perr60"] > 0:
            issues.append(_issue("parse_errors", "warning",
                                 f"{st['perr60']} datagrams could not be parsed in the last hour."))
        if st["clamped60"] > 0:
            issues.append(_issue("clock_clamped", "warning",
                                 f"{st['clamped60']} records carried flow times far from the appliance clock and were "
                                 "placed at their receipt time."))
        if st.get("amb60", 0) > 0:
            # X-08: the collector will not scale a record by an arbitrary one
            # of several sampler rates; it stores it 1 in 1 and counts it.
            issues.append(_issue("sampling_ambiguous", "warning",
                                 f"{st['amb60']} records in the last hour named no sampler while the exporter "
                                 "announced samplers with different rates and no default rate; they are stored "
                                 "unscaled (1 in 1), so their volume is not a sampling estimate. Include the sampler "
                                 "ID (IE 48 / 302) in the flow template, or announce one rate for the exporter."))
    if loss is not None and loss >= LOSS_WARN_PCT:
        issues.append(_issue("sequence_loss", "warning",
                             f"About {loss:g}% of export packets or records were lost in transit in the last hour "
                             "(sequence-number gaps)."))
    if suspect:
        issues.append(_issue("clock_skew", "warning",
                             f"The exporter clock is about {abs(offset) / 1000:.0f} s {direction} of the appliance; "
                             "flow times may be misplaced."))
    if flags is False:
        issues.append(_issue("no_tcp_flags", "info",
                             "The exporter does not send TCP flags; flag-based views and detectors cannot use it."))
    if owner is None:
        issues.append(_issue("unmapped", "info",
                             "Not linked to a monitored device: interface names and speeds cannot resolve, and "
                             "tag-scoped users cannot see this exporter."))
    # When the current state began: late/silent follow from the last receipt
    # and the thresholds; a receiving run's start is the transition the health
    # loop recorded (unknown when it was already receiving at first sight).
    state_since = None
    if state == "late" and last_rx is not None:
        state_since = _to_iso_utc(last_rx + timedelta(seconds=th.late_after_s))
    elif state == "silent" and last_rx is not None:
        state_since = _to_iso_utc(last_rx + timedelta(seconds=th.silent_after_s))
    elif state == "receiving":
        entry = (since or {}).get(ip)
        if entry and entry.get("state") == "receiving":
            state_since = entry.get("since")
    return {
        "exporter": ip,
        "display_name": (setting or {}).get("display_name") or None,
        "device_id": owner.device_id if owner else None,
        "device_hostname": owner.hostname if owner else None,
        "device_status": (device or {}).get("status") if owner else None,
        "mapping_source": owner.source if owner else None,
        "last_received_at": _to_iso_utc(last_rx),
        "last_record_at": _to_iso_utc(last_record),
        "last_datagram_at": _to_iso_utc(last_dgram),
        "receipt_age_s": round(age, 1) if age is not None else None,
        "state": state,
        "state_since": state_since,
        "records_15m": rc["rec15"] if rc else (0 if last_rx else None),
        "records_1h": rc["rec60"] if rc else (0 if last_rx else None),
        "datagrams_15m": st["dg15"] if st else None,
        "expected_interval_s": th.expected_s,
        "interval_basis": th.basis,
        "learned_interval_s": learned,
        "late_after_s": th.late_after_s,
        "silent_after_s": th.silent_after_s,
        "clock_offset_ms": offset,
        "clock_suspect": suspect,
        "clock_direction": direction,
        "export_protocols": protocols,
        "observation_domains": sorted(set((rc or {}).get("domains") or []) | set((st or {}).get("domains") or [])),
        "loss_pct_1h": loss,
        "flags_exported": flags,
        "flag_coverage_pct": flag_pct,
        "sampling_interval": sampling,
        "role": (setting or {}).get("role") or "edge",
        "count_direction": (setting or {}).get("count_direction") or "auto",
        "has_settings": setting is not None,
        "issues": issues,
    }


_status_cache: dict[str, Any] = {"at": 0.0, "value": None}


def invalidate_status_cache() -> None:
    _status_cache.update(at=0.0, value=None)


async def compute_status(db: AsyncSession, *, now: datetime | None = None,
                         budget: float = BUDGET_INTERACTIVE, use_live: bool = True) -> dict:
    """Receipt status of every exporter (unrestricted; callers filter by
    visibility). Raises NetflowError when flow_exporter_minutes is unreadable."""
    now = now or now_utc()
    t15, t60 = now - timedelta(minutes=15), now - timedelta(hours=1)
    receipts_task = _receipts(now - RECENT_WINDOW, t15, t60, budget=budget)
    learned_task = _learned_gaps(now - LEARN_WINDOW, budget=budget)
    stats_task = _collector_stats(now - timedelta(hours=24), t15, t60, budget=budget)
    tpl_task = _template_flags(budget=budget)
    live_task = fetch_collector_live() if use_live else asyncio.sleep(0, result=None)
    receipts, learned, stats, templates, live = await asyncio.gather(
        receipts_task, learned_task, stats_task, tpl_task, live_task)

    settings = await load_exporter_settings(db)
    mapped = await _mapped_exporters(db)
    live_by_ip = live_exporters(live)
    universe = set(receipts) | set(stats or {}) | set(settings) | set(mapped) | set(live_by_ip)

    # Configured or mapped exporters with nothing in the recent window: look
    # for an older receipt before calling them 'never'.
    older = sorted(ip for ip in universe if ip not in receipts and ip not in (stats or {}) and ip not in live_by_ip)
    if older:
        receipts.update(await _receipts(datetime(2000, 1, 1, tzinfo=timezone.utc), t15, t60,
                                        budget=budget, exporters=older))

    owners = await resolve_exporter_owners(db, sorted(universe))
    devices = await _device_status(db, [o.device_id for o in owners.values()])
    since = await load_state_since(db)
    rows = []
    for ip in sorted(universe, key=_ip_sort_key):
        owner = owners.get(ip)
        rows.append(build_row(
            ip, now=now, rc=receipts.get(ip), st=(stats or {}).get(ip), live=live_by_ip.get(ip),
            learned=learned.get(ip), tpl=templates.get(ip), setting=settings.get(ip), owner=owner,
            device=devices.get(owner.device_id) if owner else None, since=since,
        ))
    warnings = []
    if stats is None:
        warnings.append({"code": "collector_stats_unavailable",
                         "message": "Collector statistics could not be read; loss, clock and decode checks are unknown"})
    if use_live and live is not None and not live.reachable:
        warnings.append({"code": "collector_unreachable",
                         "message": "The collector's live health endpoint did not answer; receipt times come from "
                                    "persisted statistics (up to a minute coarser)"})
    return {
        "server_time": _to_iso_utc(now),
        "thresholds": {"stale_s": DEFAULT_THRESHOLDS.late_after_s, "silent_s": DEFAULT_THRESHOLDS.silent_after_s,
                       "default_expected_s": DEFAULT_EXPECTED_S},
        "collector": {
            "reachable": bool(live and live.reachable),
            "status": (live.doc or {}).get("status") if live and live.doc else "unknown",
            "error": live.error if live else None,
        },
        "exporters": rows,
        "warnings": warnings,
    }


async def _mapped_exporters(db: AsyncSession) -> set[str]:
    try:
        async with db.begin_nested():
            rows = (await db.execute(text("SELECT host(exporter_ip) FROM netflow_exporter_devices"))).scalars().all()
    except Exception:
        return set()
    return {ip for ip in (canonical_ip(r) for r in rows) if ip}


def _ip_sort_key(ip: str):
    try:
        a = ipaddress.ip_address(ip)
        return (a.version, int(a))
    except ValueError:
        return (9, 0)


async def cached_status(db: AsyncSession, *, refresh: bool = False) -> dict:
    cached = _status_cache["value"]
    if not refresh and cached is not None and time.monotonic() - _status_cache["at"] < STATUS_CACHE_TTL_S:
        return cached
    value = await compute_status(db)
    _status_cache.update(at=time.monotonic(), value=value)
    return value


def filter_status(status: dict, vis: FlowVisibility) -> dict:
    """The status document restricted to what ``vis`` may see."""
    if vis.exporters is None:
        return status
    return {**status, "exporters": [r for r in status["exporters"] if vis.allows(r["exporter"])]}


async def status_for(db: AsyncSession, vis: FlowVisibility, *, refresh: bool = False) -> dict:
    return filter_status(await cached_status(db, refresh=refresh), vis)


async def exporter_row(db: AsyncSession, ip: str, *, now: datetime | None = None) -> dict:
    """The status row of one exporter, computed fresh (no cache): the setup
    check polls it right after an exporter is configured."""
    now = now or now_utc()
    t15, t60 = now - timedelta(minutes=15), now - timedelta(hours=1)
    ips = [ip]
    receipts, learned, stats, templates, live = await asyncio.gather(
        _receipts(datetime(2000, 1, 1, tzinfo=timezone.utc), t15, t60, budget=BUDGET_INTERACTIVE, exporters=ips),
        _learned_gaps(now - LEARN_WINDOW, budget=BUDGET_INTERACTIVE, exporters=ips),
        _collector_stats(now - timedelta(hours=24), t15, t60, budget=BUDGET_INTERACTIVE, exporters=ips),
        _template_flags(budget=BUDGET_INTERACTIVE, exporters=ips),
        fetch_collector_live(),
    )
    settings = await load_exporter_settings(db)
    owners = await resolve_exporter_owners(db, ips)
    owner = owners.get(ip)
    devices = await _device_status(db, [owner.device_id] if owner else [])
    return build_row(
        ip, now=now, rc=receipts.get(ip), st=(stats or {}).get(ip), live=live_exporters(live).get(ip),
        learned=learned.get(ip), tpl=templates.get(ip), setting=settings.get(ip), owner=owner,
        device=devices.get(owner.device_id) if owner else None, since=await load_state_since(db),
    )


# ── Collector and per-exporter detail readers ────────────────────────────────

def series_granularity(span: timedelta) -> int:
    """Bucket size for a statistics series (the stats interval is 60 s)."""
    hours = span.total_seconds() / 3600
    if hours <= 3:
        return 60
    if hours <= 24:
        return 300
    if hours <= 24 * 7:
        return 3600
    return 4 * 3600


def _sums_sql() -> str:
    # Aliased apart from the column names: a later ``datagrams > 0`` in the
    # same SELECT must mean the column, not this sum (ILLEGAL_AGGREGATION).
    return ", ".join(f"sum({c}) AS t_{c}" for c in _STATS_COLUMNS)


def _exporter_pred(exporters: list[str] | None, params: dict) -> str:
    """Stats rows of these exporters (canonical text); None = every exporter."""
    if exporters is None:
        return "exporter != ''"
    params["nf_hx"] = list(exporters) or [""]
    return "exporter IN %(nf_hx)s"


async def collector_totals(since: datetime, until: datetime, exporters: list[str] | None) -> dict | None:
    """Window totals: per-exporter counters summed (visible exporters only) and
    the process row (queue, spool, inserts, kernel drops). None when the stats
    table cannot be read."""
    params: dict = {"since": since, "until": until}
    pred = _exporter_pred(exporters, params)
    try:
        exp, proc, collectors = await asyncio.gather(
            run_query(
                f"SELECT {_sums_sql()}, uniqExactIf(exporter, datagrams > 0) AS exporters "
                f"FROM {table_ref(COLLECTOR_STATS)} WHERE ts >= %(since)s AND ts < %(until)s AND {pred}",
                params, label="collector-totals"),
            run_query(
                f"""SELECT sum(insert_failures), sum(insert_retries), sum(kernel_rcvbuf_errors),
                           max(queue_len), max(queue_cap), max(spool_records), max(spool_bytes),
                           sum(records_dropped_queue), sum(records_dropped_implausible),
                           sum(records_dropped_unknown_exporter), sum(parse_errors), sum(datagrams),
                           argMax(queue_len, ts), argMax(spool_records, ts), max(ts), count()
                    FROM {table_ref(COLLECTOR_STATS)}
                    WHERE ts >= %(since)s AND ts < %(until)s AND exporter = ''""",
                {"since": since, "until": until}, label="collector-process"),
            run_query(
                f"SELECT collector_id, max(ts) FROM {table_ref(COLLECTOR_STATS)} "
                f"WHERE ts >= %(since)s AND ts < %(until)s GROUP BY collector_id ORDER BY collector_id",
                {"since": since, "until": until}, label="collector-ids"),
        )
    except NetflowError:
        logger.warning("netflow: collector statistics unreadable", exc_info=True)
        return None
    row = exp.result_rows[0] if exp.result_rows else None
    totals = {c: int((row[i] if row else 0) or 0) for i, c in enumerate(_STATS_COLUMNS)}
    totals["exporters"] = int(row[len(_STATS_COLUMNS)] or 0) if row else 0
    p = proc.result_rows[0] if proc.result_rows else None
    has_proc = bool(p and p[15])
    process = {
        "insert_failures": int(p[0] or 0), "insert_retries": int(p[1] or 0),
        "kernel_rcvbuf_errors": int(p[2] or 0), "max_queue_len": int(p[3] or 0),
        "queue_cap": int(p[4] or 0) or None, "max_spool_records": int(p[5] or 0),
        "max_spool_bytes": int(p[6] or 0), "records_dropped_queue": int(p[7] or 0),
        "records_dropped_implausible": int(p[8] or 0),
        "records_dropped_unknown_exporter": int(p[9] or 0), "unattributed_parse_errors": int(p[10] or 0),
        "unattributed_datagrams": int(p[11] or 0), "last_queue_len": int(p[12] or 0),
        "last_spool_records": int(p[13] or 0), "last_stats_at": _to_iso_utc(_parse_ts(p[14])),
        "intervals": int(p[15] or 0),
    } if has_proc else None
    return {
        "totals": totals,
        "process": process,
        "collectors": [{"collector_id": r[0], "last_stats_at": _to_iso_utc(_parse_ts(r[1]))}
                       for r in collectors.result_rows],
    }


async def stats_series(since: datetime, until: datetime, granularity: int,
                       exporters: list[str] | None, *, process: bool = True) -> list[dict] | None:
    """Per-bucket counters (half-open buckets on the stats interval start)."""
    params: dict = {"since": since, "until": until, "g": int(granularity)}
    pred = _exporter_pred(exporters, params)
    drops = ("records_dropped_implausible + records_dropped_queue + "
             "records_dropped_unknown_exporter + records_unsupported")
    proc_cols = ("sumIf(insert_failures, exporter = '') AS s_insert_failures, "
                 "maxIf(queue_len, exporter = '') AS s_queue_len") if process else "0, 0"
    where = f"(({pred}) OR exporter = '')" if process else pred
    try:
        res = await run_query(
            f"""
            SELECT toStartOfInterval(ts, toIntervalSecond(%(g)s)) AS b,
                   sumIf(datagrams, exporter != '') AS s_datagrams,
                   sumIf(records_decoded, exporter != '') AS s_records_decoded,
                   sumIf(records_written, exporter != '') AS s_records_written,
                   sumIf({drops}, exporter != '') AS s_dropped,
                   sumIf(data_sets_waiting_template, exporter != '') AS s_waiting,
                   sumIf(sequence_gaps, exporter != '') AS s_sequence_gaps,
                   sumIf(parse_errors, exporter != '') AS s_parse_errors,
                   {proc_cols}
            FROM {table_ref(COLLECTOR_STATS)}
            WHERE ts >= %(since)s AND ts < %(until)s AND {where}
            GROUP BY b ORDER BY b
            """,
            params, label="collector-series",
        )
    except NetflowError:
        logger.warning("netflow: collector statistics series unreadable", exc_info=True)
        return None
    keys = ("datagrams", "records_decoded", "records_written", "dropped", "waiting",
            "sequence_gaps", "parse_errors", "insert_failures", "queue_len")
    return [{"ts": _to_iso_utc(_parse_ts(r[0])), **{k: int(r[i + 1] or 0) for i, k in enumerate(keys)}}
            for r in res.result_rows]


async def exporter_streams(ip: str, since: datetime, until: datetime) -> list[dict] | None:
    """Per (observation domain, export protocol) totals for one exporter."""
    try:
        res = await run_query(
            f"""
            SELECT observation_domain, export_protocol, {_sums_sql()},
                   sum({_SEQ_UNITS}) AS units,
                   quantileExactIfOrNull(0.5)(clock_offset_ms, {_HAS_CLOCK}) AS offset,
                   maxIfOrNull(sampling_interval, datagrams > 0) AS sampling,
                   argMaxIf(templates_active, ts, datagrams > 0) AS templates_active,
                   maxIfOrNull(ts, datagrams > 0) AS last_seen, count() AS intervals
            FROM {table_ref(COLLECTOR_STATS)}
            WHERE ts >= %(since)s AND ts < %(until)s AND exporter = %(ip)s
            GROUP BY observation_domain, export_protocol
            ORDER BY observation_domain, export_protocol
            """,
            {"since": since, "until": until, "ip": ip}, label="exporter-streams",
        )
    except NetflowError:
        logger.warning("netflow: exporter streams unreadable", exc_info=True)
        return None
    n = len(_STATS_COLUMNS)
    out = []
    for r in res.result_rows:
        counters = {c: int(r[2 + i] or 0) for i, c in enumerate(_STATS_COLUMNS)}
        units = int(r[2 + n] or 0)
        out.append({
            "observation_domain": int(r[0]), "export_protocol": r[1] or None,
            **counters,
            "loss_pct": loss_pct(counters["sequence_gaps"], units),
            "clock_offset_ms": int(r[3 + n]) if r[3 + n] is not None else None,
            "sampling_interval": int(r[4 + n]) if r[4 + n] is not None else None,
            "templates_active": int(r[5 + n] or 0),
            "last_datagram_at": _to_iso_utc(_parse_ts(r[6 + n])),
        })
    return out


async def exporter_templates(ip: str) -> list[dict] | None:
    """Template inventory of one exporter with named fields."""
    try:
        res = await run_query(
            f"""
            SELECT observation_domain, export_protocol, template_id, field_ids, field_lengths,
                   enterprise_ids, is_options, has_tcp_flags, has_ipv6, has_ifindex32,
                   has_direction, has_app_id, has_sampling, has_nat, has_fw_event,
                   first_seen, last_seen, collector_id
            FROM {table_ref(TEMPLATES)} FINAL
            WHERE exporter = %(ip)s
            ORDER BY observation_domain, export_protocol, template_id
            """,
            {"ip": ip}, label="exporter-templates",
        )
    except NetflowError:
        logger.warning("netflow: template inventory unreadable", exc_info=True)
        return None
    out = []
    for r in res.result_rows:
        ids, lengths, ents = list(r[3] or []), list(r[4] or []), list(r[5] or [])
        fields = []
        for i, fid in enumerate(ids):
            ent = int(ents[i]) if i < len(ents) else 0
            fields.append({
                "id": int(fid), "enterprise": ent or None,
                "length": int(lengths[i]) if i < len(lengths) else None,
                "name": field_name(int(fid), ent),
                "decoded": ent == 0 and int(fid) in DECODED_IES,
            })
        flags = {k: bool(r[7 + i]) for i, k in enumerate(
            ("tcp_flags", "ipv6", "ifindex32", "direction", "app_id", "sampling", "nat", "fw_event"))}
        out.append({
            "observation_domain": int(r[0]), "export_protocol": r[1], "template_id": int(r[2]),
            "is_options": bool(r[6]), "fields": fields, "flags": flags,
            "first_seen": _to_iso_utc(_parse_ts(r[15])), "last_seen": _to_iso_utc(_parse_ts(r[16])),
            "collector_id": r[17],
        })
    return out


def _empty_fields(window: timedelta) -> dict:
    return {
        "window_s": int(window.total_seconds()),
        "records": 0, "tracked": 0, "legacy": 0, "v6": 0,
        "max_sampling": None, "min_sampling": None, "protocols": [], "versions": [],
        "direction": {"ingress": 0, "egress": 0}, "clock_clamped": 0, "biflow_reverse": 0,
        "capabilities": capability_status({}, 0), "sampled_records": False,
    }


async def _fields_flow_span(ip: str, since: datetime, until: datetime) -> tuple[int, datetime | None, datetime | None] | None:
    """(records, earliest flow time, latest flow time) of the exporter's
    records received since ``since``, from flow_exporter_minutes (keyed by
    receipt minute): bounds the raw scan to the flow times these records
    actually carry. None when the rollup cannot answer."""
    pred, params = exporters_predicate([ip], param="nf_hx")
    params.update({"since_min": since.replace(second=0, microsecond=0), "until": until})
    try:
        res = await run_query(
            f"""
            SELECT sum(records) + sum(legacy_records) AS n, count() AS minutes,
                   min(min_flow_ts) AS lo, max(max_flow_ts) AS hi
            FROM {table_ref(EXPORTER_MINUTES)}
            WHERE minute >= %(since_min)s AND minute <= %(until)s AND {pred}
            """,
            params, label="exporter-fields-span",
        )
    except NetflowError:
        return None
    r = res.result_rows[0] if res.result_rows else None
    if not r or not int(r[1] or 0):
        return (0, None, None)
    return (int(r[0] or 0), _parse_ts(r[2]), _parse_ts(r[3]))


async def exporter_fields(ip: str, now: datetime, window: timedelta = timedelta(hours=1), *,
                          max_records: int | None = None) -> dict:
    """What the exporter's records actually carried (fields_present) among the
    records received in the last ``window``, per capability, plus shares.

    The raw scan is bounded to this exporter's own flow-time span in the
    receipt window (flow_exporter_minutes), so it no longer reads every
    exporter's records of the last three hours; an exporter that sent nothing
    in the window costs no raw scan at all. ``max_records`` limits it to the
    newest records (the shares then describe that sample: ``sampled_records``)."""
    since = now - window
    span = await _fields_flow_span(ip, since, now + timedelta(minutes=1))
    if span is not None and span[0] == 0 and span[1] is None:
        return _empty_fields(window)
    pred, params = exporters_predicate([ip], param="nf_hx")
    if span is not None and span[1] is not None and span[2] is not None:
        # Whole seconds: datetime parameters are bound at second precision.
        ts_since = span[1].replace(microsecond=0)
        ts_until = span[2].replace(microsecond=0) + timedelta(seconds=1)
    else:
        # Rollup unavailable: flow times sit up to 2 h before receipt (clock
        # policy) plus the flow's own duration; bound the scan generously.
        ts_since, ts_until = since - timedelta(hours=3), now + timedelta(minutes=5)
    params.update({"since": since, "ts_since": ts_since, "until": ts_until})
    limit_sql = ""
    if max_records:
        limit_sql = f"ORDER BY timestamp DESC LIMIT {int(max_records)}"
    res = await run_query(
        f"""
        SELECT count() AS records, countIf(bitTest(fp, 31)) AS tracked, countIf(fp = 0) AS legacy,
               countIf(ip_version = 6) AS v6, max(sampling_interval) AS max_sampling,
               min(sampling_interval) AS min_sampling, groupUniqArray(export_protocol) AS protocols,
               groupUniqArray(flow_version) AS versions,
               countIf(flow_direction = 0) AS ingress, countIf(flow_direction = 1) AS egress,
               countIf(bitTest(fp, 28)) AS clamped, countIf(bitTest(fp, 30)) AS reverse,
               {capability_sql()}
        FROM (SELECT fields_present AS fp, protocol, ip_version, sampling_interval, export_protocol,
                     flow_version, flow_direction
              FROM {table_ref(RAW)}
              WHERE timestamp >= %(ts_since)s AND timestamp < %(until)s
                AND received_at >= %(since)s AND {pred}
              {limit_sql})
        """,
        params, label="exporter-fields",
    )
    r = res.result_rows[0] if res.result_rows else None
    base = 12
    counts = {}
    if r is not None:
        for i, (key, *_rest) in enumerate(CAPABILITIES):
            counts[key] = (int(r[base + 2 * i] or 0), int(r[base + 2 * i + 1] or 0))
    records = int(r[0] or 0) if r else 0
    tracked = int(r[1] or 0) if r else 0
    return {
        "window_s": int(window.total_seconds()),
        "records": records, "tracked": tracked, "legacy": int(r[2] or 0) if r else 0,
        "v6": int(r[3] or 0) if r else 0,
        "max_sampling": int(r[4]) if r and records else None,
        "min_sampling": int(r[5]) if r and records else None,
        "protocols": sorted(p for p in (r[6] if r else []) if p),
        "versions": sorted(int(v) for v in (r[7] if r else [])),
        "direction": {"ingress": int(r[8] or 0), "egress": int(r[9] or 0)} if r else None,
        "clock_clamped": int(r[10] or 0) if r else 0,
        "biflow_reverse": int(r[11] or 0) if r else 0,
        "capabilities": capability_status(counts, tracked),
        "sampled_records": bool(max_records and records >= max_records),
    }


# The Setup page polls its live check every few seconds while a device is
# being configured. Field presence changes slowly (a template edit), so one
# scan per exporter serves every poll for a short while.
FIELDS_CACHE_TTL_S = 30.0
CHECK_FIELDS_MAX_RECORDS = 50_000
_fields_cache: dict[tuple, tuple[float, dict]] = {}


async def cached_exporter_fields(ip: str, now: datetime, window: timedelta, *,
                                 max_records: int | None = CHECK_FIELDS_MAX_RECORDS) -> dict:
    key = (ip, int(window.total_seconds()), max_records)
    hit = _fields_cache.get(key)
    mono = time.monotonic()
    if hit and mono - hit[0] < FIELDS_CACHE_TTL_S:
        return hit[1]
    value = await exporter_fields(ip, now, window, max_records=max_records)
    # An exporter with nothing yet costs no raw scan (the rollup says so) and
    # must show its first records at the next poll: only results with records
    # are kept.
    if value.get("records"):
        if len(_fields_cache) > 256:
            _fields_cache.clear()
        _fields_cache[key] = (mono, value)
    return value


async def exporter_ifindexes(ip: str, since: datetime, until: datetime, limit: int = 50) -> list[dict] | None:
    """Interface indexes the exporter reported (5-minute interface rollup)."""
    pred, params = exporters_predicate([ip], param="nf_hx")
    params.update({"since": since, "until": until, "lim": int(limit)})
    try:
        res = await run_query(
            f"""
            SELECT ifindex, sumIf(bytes, direction = 0) AS rx, sumIf(bytes, direction = 1) AS tx
            FROM {table_ref(INTERFACE_5M)}
            WHERE timestamp >= %(since)s AND timestamp < %(until)s AND ifindex != 0 AND {pred}
            GROUP BY ifindex ORDER BY rx + tx DESC LIMIT %(lim)s
            """,
            params, label="exporter-ifindexes",
        )
    except NetflowError:
        return None
    return [{"ifindex": int(r[0]), "rx_bytes": float(r[1] or 0), "tx_bytes": float(r[2] or 0)}
            for r in res.result_rows]


async def interface_speeds(db: AsyncSession, ip: str, device_id: str | None) -> dict[int, dict]:
    """{ifindex -> {speed, source, override_name}} for one exporter by the one
    speed rule (services/interface_speed.py): the owner device's configured
    circuit speed, else the NetFlow interface override, else SNMP. Overrides
    also name interfaces of exporters that have no device."""
    import uuid

    from app.services.interface_speed import effective_speed_source

    overrides: dict[int, tuple[int | None, str | None]] = {}
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT ifindex, GREATEST(COALESCE(speed_in_bps, 0), COALESCE(speed_out_bps, 0)) AS sp, display_name "
                "FROM netflow_interface_overrides WHERE exporter_ip = CAST(:ip AS inet)"
            ), {"ip": ip})).all()
        overrides = {int(r[0]): (int(r[1]) if r[1] else None, r[2]) for r in rows}
    except Exception:
        logger.debug("netflow_interface_overrides unavailable", exc_info=True)
    device: dict[int, tuple[int | None, int | None]] = {}
    if device_id:
        rows = (await db.execute(text(
            "SELECT if_index, configured_speed_bps, if_speed FROM device_interfaces WHERE device_id = :d"
        ), {"d": uuid.UUID(str(device_id))})).all()
        device = {int(r[0]): (r[1], r[2]) for r in rows}
    out: dict[int, dict] = {}
    for idx in set(overrides) | set(device):
        configured, snmp = device.get(idx, (None, None))
        override, name = overrides.get(idx, (None, None))
        speed, source = effective_speed_source(configured, override, snmp)
        out[idx] = {"speed": speed, "source": source, "override_name": name}
    return out


# ── Appliance addresses (onboarding) ─────────────────────────────────────────

_VIRTUAL_IF = re.compile(r"^(lo|docker\d*|br-|veth|virbr|vnet|cni|flannel|cali|tun|tap|wg|zt|kube)", re.I)


def appliance_addresses() -> list[dict]:
    """Global addresses of this host for the exporter configuration, physical
    interfaces first. Loopback and link-local are left out."""
    out: list[dict] = []
    try:
        raw = subprocess.run(["ip", "-j", "addr", "show"], capture_output=True, text=True, timeout=2, check=False).stdout
        data = json.loads(raw or "[]")
    except Exception:
        data = []
    for iface in data:
        name = iface.get("ifname") or ""
        for a in iface.get("addr_info") or []:
            addr = a.get("local")
            if not addr:
                continue
            try:
                ipa = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if ipa.is_loopback or ipa.is_link_local or ipa.is_multicast:
                continue
            out.append({"address": str(ipa), "interface": name, "family": ipa.version,
                        "virtual": bool(_VIRTUAL_IF.match(name))})
    out.sort(key=lambda x: (x["virtual"], x["family"], x["interface"]))
    return out


# ── Background loop ──────────────────────────────────────────────────────────

def next_states(previous: dict[str, dict], rows: list[dict], now_iso: str) -> tuple[dict[str, dict], list[tuple[str, str | None, str]]]:
    """New state map and the transitions (exporter, old, new)."""
    current: dict[str, dict] = {}
    transitions = []
    for row in rows:
        ip, state = row["exporter"], row["state"]
        prev = previous.get(ip)
        if prev and prev.get("state") == state:
            current[ip] = prev
        else:
            # First sight of an exporter: when its current state began is
            # unknown (it may have been receiving for months).
            current[ip] = {"state": state, "since": now_iso if prev else None}
            transitions.append((ip, prev.get("state") if prev else None, state))
    return current, transitions


async def run_health_once() -> list[tuple[str, str | None, str]]:
    """Recompute status, record state transitions, warm the cache."""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        acquired = bool((await db.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": NETFLOW_HEALTH_ADVISORY_LOCK}
        )).scalar())
        status = await compute_status(db, budget=BUDGET_BACKGROUND)
        _status_cache.update(at=time.monotonic(), value=status)
        if not acquired:
            await db.rollback()
            return []
        previous = await load_state_since(db, refresh=True)
        current, transitions = next_states(previous, status["exporters"], status["server_time"])
        if transitions or set(previous) != set(current):
            await db.execute(text(
                "INSERT INTO system_settings (key, value, updated_at) VALUES (:k, CAST(:v AS jsonb), NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
            ), {"k": STATE_SETTING_KEY, "v": json.dumps({"exporters": current})})
            _state_cache.update(at=time.monotonic(), value=current)
        await db.commit()
    for ip, old, new in transitions:
        if old is not None:
            logger.info("netflow exporter %s: %s -> %s", ip, old, new)
    return transitions


async def netflow_health_loop() -> None:
    """Every minute: exporter status + state transitions (contract §9.4)."""
    await asyncio.sleep(HEALTH_LOOP_STARTUP_DELAY_S)
    while True:
        try:
            await run_health_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("netflow health pass failed", exc_info=True)
        await asyncio.sleep(HEALTH_LOOP_INTERVAL_S)
