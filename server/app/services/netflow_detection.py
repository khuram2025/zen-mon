"""NetFlow detection engine (CR-11 / FE-06; NF-03, NF-07, NF-09, NF-21, NF-22,
X-09, X-31, X-32, X-34, X-36, X-37).

What replaced the five stateless heuristics that re-scanned raw flows on every
page load:

* **Detectors declare what they need.** Each entry of ``DETECTORS`` names its
  required fields (TCP flags, ICMP type, rollup history, threat-intel feeds)
  and its thresholds. Before a detector runs, the window's field coverage is
  measured from ``fields_present`` (the same "flagged" rule the
  ``flow_exporter_minutes`` view uses); a detector whose fields are not
  exported by any exporter in scope is ``unavailable`` -- never a clean
  result -- and one that ran on part of the exporters says which it could
  not see. Every detector reports ``ok | unavailable | no_coverage | failed |
  disabled`` with a reason, so a security screen can only claim "no
  candidates" for checks that actually ran with data (NF-03, NF-07).
* **Thresholds are per 5-minute bucket**, not per window, so a benign
  recurring pattern does not become an attack because the window got longer
  (NF-21, X-31). Rates use the real bucket length; ICMP packet rates are
  per minute with each record's packets spread over its duration.
* **Denominators are real** (NF-22): the dominant-sender share is taken of
  the exporter's whole guarded traffic in the bucket, and "volume above
  baseline" compares a host with its own hour-of-week history from the
  hourly rollup.
* **Every query goes through ``compile_filter``**: the caller's visibility
  scope and the volume guard (corrupt records, NSEL create/deny events) apply
  to every detector (X-36).
* **Findings persist.** ``netflow_detection_loop`` evaluates each closed
  5-minute bucket once (2 minutes after it closes, under a PostgreSQL advisory
  lock) and upserts candidates into ``netflow_findings`` by fingerprint
  (kind + exporter + subject), keeping first/last seen, occurrences (buckets
  with activity), the latest evidence segment and window, the detector
  filters, thresholds, observed values, baseline and confidence. Findings
  nobody saw again for ``quiet_minutes`` resolve themselves; a resolved
  finding that recurs reopens. Approved exceptions
  (``netflow_detector_exceptions``) never drop a match: it is stored and
  shown as suppressed, with the exception's reason. An exception may be
  limited to some exporters (``exporter_ips``, migrate-140; a tag-scoped
  manager's always is) and then matches findings from those exporters only.
* **Ad-hoc runs** (``run_adhoc``) evaluate the same detectors over any
  window up to 31 days of raw records for historical investigation; they are
  never persisted and are cached for a minute per scope.

Counts are never capped silently: each detector returns the number of
matching subjects (``match_count``) next to what it returned (X-34).

Fingerprints are per exporter: the same flow seen at two collection points
yields two findings, each with evidence from its own exporter (and the
visibility scope follows the exporter, contract §6).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("zenplus.netflow.detection")

# ── Constants ─────────────────────────────────────────────────────────────────

BUCKET_S = 300                          # evaluation bucket (5 minutes)
CLOSE_GRACE_S = 120                     # a bucket is closed this long after its end
MAX_CATCHUP = timedelta(hours=1)        # after downtime, evaluate at most this far back
STARTUP_DELAY_S = 60.0
ADVISORY_LOCK = 1515074405              # netflow detection engine (rollup uses ...392)
SETTINGS_KEY = "netflow_detectors"
STATE_KEY = "netflow_detection_state"
MAX_SEGMENTS = 24                       # latest occurrence segments kept per finding
MAX_EVIDENCE_SPAN = timedelta(days=31)  # Investigate window cap (raw-only forensics limit)
ENGINE_LIMIT = 200                      # subjects per detector per bucket (engine)
ADHOC_LIMIT = 50                        # subjects per detector returned by an ad-hoc run
ADHOC_CACHE_TTL_S = 60.0
ENGINE_STALE_S = 3 * BUCKET_S + CLOSE_GRACE_S + 60
MAX_COVERAGE_INTERVALS = 400
EVALUATED_KEEP = timedelta(days=93)

SEVERITIES = ("critical", "warning", "info")
SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}
CONFIDENCES = ("high", "medium", "low")
STATUSES = ("open", "acknowledged", "suppressed", "resolved")
DETECTOR_STATES = ("ok", "unavailable", "no_coverage", "failed", "disabled")

# "Flags were exported for this record": tracked records carry fields_present
# bit 0; a legacy record (fields_present = 0) proves presence only by a
# non-zero value. Identical to the flow_exporter_minutes view's rule.
FLAGGED = "if(fields_present = 0, tcp_flags != 0, bitTest(fields_present, 0))"
# ICMP type/code known: ICMP_TYPE (bit 11) or L4_PORTS (bit 2, v5/v9 carry
# type*256+code in dst_port) or a legacy record.
ICMP_TYPED = "(fields_present = 0 OR bitTest(fields_present, 11) OR bitTest(fields_present, 2))"
# Flow start/end times present and consistent (contract §3.4 duration rule).
DURATION_VALID = (
    "(first_switched_ms > 0 AND last_switched_ms >= first_switched_ms"
    " AND (first_switched_ms >= 1000000000000) = (last_switched_ms >= 1000000000000)"
    " AND last_switched_ms - first_switched_ms <= 86400000)"
)
ICMP_PROTOS = (1, 58)
ECHO_REQUEST_PORTS = "((protocol = 1 AND dst_port = 2048) OR (protocol = 58 AND dst_port = 32768))"

# RFC 6890 special-purpose ranges (plus multicast/reserved): never "public".
SPECIAL_USE: tuple[str, ...] = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "::/127", "100::/64", "2001::/23", "2001:db8::/32", "fc00::/7", "fe80::/10", "ff00::/8",
)
# Addresses treated as the inside of the network (sensitive-egress sources).
INTERNAL: tuple[str, ...] = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7",
)
_SPECIAL_NETS = tuple(ipaddress.ip_network(n) for n in SPECIAL_USE)
_INTERNAL_NETS = tuple(ipaddress.ip_network(n) for n in INTERNAL)


def is_public_ip(ip: str) -> bool:
    """True unless ``ip`` is in a special-use range (the SQL rule, in Python)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return not any(addr.version == n.version and addr in n for n in _SPECIAL_NETS)


def is_internal_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr.version == n.version and addr in n for n in _INTERNAL_NETS)


# ── Detector registry and settings ───────────────────────────────────────────

@dataclass(frozen=True)
class Param:
    """One configurable detector setting (rendered as a form by the UI)."""

    key: str
    label: str
    kind: str                 # 'int' | 'float' | 'ports' | 'severity' | 'bool'
    default: Any
    min: float | None = None
    max: float | None = None
    unit: str = ""
    help: str = ""

    def as_dict(self, value: Any) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind, "default": self.default,
                "min": self.min, "max": self.max, "unit": self.unit, "help": self.help, "value": value}


def _sev(default: str, label: str = "Severity") -> Param:
    return Param("severity", label, "severity", default)


@dataclass(frozen=True)
class DetectorSpec:
    kind: str
    label: str
    category: str             # scan | tcp | egress | flood | volume | threat_intel
    description: str
    method: str               # one sentence: what is measured and compared
    required: tuple[str, ...]  # tcp_flags | icmp_type | baseline | ti_feeds
    params: tuple[Param, ...]
    count_based: bool = False  # thresholds count sampled records (X-09)

    def defaults(self) -> dict:
        return {"enabled": True, **{p.key: p.default for p in self.params}}


SENSITIVE_PORTS_DEFAULT = [22, 23, 135, 139, 445, 1433, 3306, 3389, 5432, 5900]

DETECTORS: tuple[DetectorSpec, ...] = (
    DetectorSpec(
        "syn_scan", "Host sweep (TCP SYN)", "scan",
        "A source sends connection attempts (SYN without ACK) to many different hosts.",
        "Distinct destinations of SYN-only TCP records per source in each 5-minute bucket.",
        ("tcp_flags",),
        (Param("min_destinations", "Destinations per 5 min", "int", 100, 10, 100000, "hosts"),
         Param("critical_destinations", "Critical at", "int", 1000, 10, 1000000, "hosts"),
         _sev("warning")),
        count_based=True,
    ),
    DetectorSpec(
        "port_scan", "Port scan (TCP SYN)", "scan",
        "A source probes many ports of one host with connection attempts.",
        "Distinct destination ports of SYN-only TCP records per source/destination pair in each 5-minute bucket.",
        ("tcp_flags",),
        (Param("min_ports", "Ports per 5 min", "int", 100, 10, 65536, "ports"),
         Param("critical_ports", "Critical at", "int", 1000, 10, 65536, "ports"),
         _sev("warning")),
        count_based=True,
    ),
    DetectorSpec(
        "rst_flood", "High TCP reset ratio", "tcp",
        "A host answers most TCP connections with resets (refused connections, scan targets, broken services).",
        "Share of flagged TCP records carrying RST per source in each 5-minute bucket; only records whose "
        "flags were exported count, in the numerator and the denominator.",
        ("tcp_flags",),
        (Param("min_flows", "Flagged TCP records per 5 min", "int", 200, 10, 10000000, "records"),
         Param("min_ratio_pct", "RST share", "float", 50.0, 1.0, 100.0, "%"),
         _sev("info")),
        count_based=True,
    ),
    DetectorSpec(
        "sensitive_egress", "Sensitive service to the internet", "egress",
        "An internal host reaches a public address on a sensitive service port (remote access, file sharing, databases).",
        "Internal (RFC 1918, CGNAT, ULA) source to a public destination (outside every RFC 6890 special-use "
        "range) on a listed port, with evidence of an established session: ACK seen, or at least the minimum bytes.",
        (),
        (Param("ports", "Sensitive destination ports", "ports", SENSITIVE_PORTS_DEFAULT),
         Param("min_bytes", "Minimum bytes without ACK evidence", "int", 10000, 0, 10**12, "bytes"),
         _sev("warning")),
    ),
    DetectorSpec(
        "icmp_flood", "ICMP flood (source)", "flood",
        "A host sends ICMP at a sustained high packet rate.",
        "Per-minute ICMP packet rate per source (each record's packets spread over its duration), sustained for "
        "consecutive minutes above the threshold and above the host's own 7-day p95 hourly rate times the multiplier.",
        (),
        (Param("min_pps", "Packet rate", "int", 1000, 10, 100000000, "pps"),
         Param("min_minutes", "Sustained for", "int", 3, 1, 60, "minutes"),
         Param("baseline_multiplier", "Above own baseline ×", "float", 5.0, 1.0, 1000.0),
         _sev("warning")),
    ),
    DetectorSpec(
        "icmp_flood_target", "ICMP flood (target)", "flood",
        "A host receives ICMP at a sustained high packet rate, from one or many sources (distributed floods).",
        "Per-minute ICMP packet rate per destination, spread over record durations, sustained above the threshold "
        "and above the target's own 7-day p95 hourly rate times the multiplier.",
        (),
        (Param("min_pps", "Packet rate", "int", 1000, 10, 100000000, "pps"),
         Param("min_minutes", "Sustained for", "int", 3, 1, 60, "minutes"),
         Param("baseline_multiplier", "Above own baseline ×", "float", 5.0, 1.0, 1000.0),
         _sev("warning")),
    ),
    DetectorSpec(
        "icmp_sweep", "ICMP sweep", "scan",
        "A source sends echo requests to many different hosts (host discovery).",
        "Distinct destinations of ICMP/ICMPv6 echo requests per source in each 5-minute bucket.",
        ("icmp_type",),
        (Param("min_destinations", "Destinations per 5 min", "int", 100, 10, 100000, "hosts"),
         _sev("info")),
        count_based=True,
    ),
    DetectorSpec(
        "dominant_sender", "Dominant sender", "volume",
        "One source sends most of the traffic an exporter observed.",
        "A source's share of all guarded bytes the exporter observed in the same 5-minute bucket, with a "
        "minimum average rate so that quiet periods do not qualify.",
        (),
        (Param("min_share_pct", "Share of all traffic", "float", 50.0, 1.0, 100.0, "%"),
         Param("min_rate_mbps", "Minimum rate", "float", 25.0, 0.001, 1000000.0, "Mbps"),
         _sev("info")),
    ),
    DetectorSpec(
        "volume_spike", "Volume above baseline", "volume",
        "A host sends far more than it usually does at this hour of the week.",
        "A source's bytes per second in each 5-minute bucket against the median of the same hour-of-week over "
        "the previous 4 weeks (hourly rollup); with fewer than two weeks of history, against its 7-day p95 "
        "hourly rate (lower confidence).",
        ("baseline",),
        (Param("multiplier", "Above baseline ×", "float", 5.0, 1.5, 1000.0),
         Param("min_rate_mbps", "Minimum rate", "float", 10.0, 0.001, 1000000.0, "Mbps"),
         Param("min_baseline_hours", "History needed", "int", 24, 1, 2000, "hours"),
         _sev("info")),
    ),
    DetectorSpec(
        "threat_intel", "Threat-intel match", "threat_intel",
        "Traffic to or from an address listed by an enabled threat-intelligence feed.",
        "Distinct external addresses of each closed bucket looked up in an interval index of every enabled "
        "feed's IPv4/IPv6 prefixes.",
        ("ti_feeds",),
        (Param("severity_outbound", "Severity when an internal host sent to the address", "severity", "critical"),
         Param("severity_inbound", "Severity for inbound-only traffic", "severity", "info"),
         Param("include_inbound", "Report inbound-only traffic", "bool", True)),
    ),
)
DETECTOR_BY_KIND: dict[str, DetectorSpec] = {d.kind: d for d in DETECTORS}
KINDS: tuple[str, ...] = tuple(DETECTOR_BY_KIND)

DEFAULT_QUIET_MINUTES = 60


def default_settings() -> dict:
    return {"quiet_minutes": DEFAULT_QUIET_MINUTES, "detectors": {d.kind: d.defaults() for d in DETECTORS}}


class SettingsError(ValueError):
    def __init__(self, fields: dict[str, str]):
        self.fields = fields
        super().__init__("; ".join(f"{k}: {v}" for k, v in fields.items()))


def _coerce(p: Param, value: Any) -> Any:
    """A parameter value in its declared type and range; ValueError otherwise."""
    if p.kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("must be true or false")
    if p.kind == "severity":
        if value in SEVERITIES:
            return value
        raise ValueError(f"must be one of {', '.join(SEVERITIES)}")
    if p.kind == "ports":
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("must be a non-empty list of ports")
        out = []
        for v in value:
            if isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).strip().isdigit():
                raise ValueError(f"'{v}' is not a port number")
            n = int(v)
            if not 0 <= n <= 65535:
                raise ValueError(f"port {n} is outside 0-65535")
            out.append(n)
        out = sorted(set(out))
        if len(out) > 64:
            raise ValueError("at most 64 ports")
        return out
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be a number")
    if p.kind == "int":
        if float(value) != int(value):
            raise ValueError("must be a whole number")
        value = int(value)
    else:
        value = float(value)
    if p.min is not None and value < p.min:
        raise ValueError(f"must be at least {p.min:g}")
    if p.max is not None and value > p.max:
        raise ValueError(f"must be at most {p.max:g}")
    return value


def merge_settings(raw: Any, *, strict: bool = False) -> dict:
    """Stored/submitted settings merged over the defaults.

    ``strict``: unknown detectors or parameters and invalid values raise
    SettingsError (API writes); otherwise they fall back to the default
    (reading what is stored never breaks detection)."""
    out = default_settings()
    errors: dict[str, str] = {}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        if strict:
            raise SettingsError({"settings": "must be an object"})
        return out
    if "quiet_minutes" in raw:
        try:
            out["quiet_minutes"] = _coerce(Param("quiet_minutes", "", "int", 60, 10, 10080), raw["quiet_minutes"])
        except ValueError as exc:
            errors["quiet_minutes"] = str(exc)
    dets = raw.get("detectors") or {}
    if not isinstance(dets, dict):
        errors["detectors"] = "must be an object"
        dets = {}
    for kind, values in dets.items():
        spec = DETECTOR_BY_KIND.get(kind)
        if spec is None:
            errors[f"detectors.{kind}"] = "unknown detector"
            continue
        if not isinstance(values, dict):
            errors[f"detectors.{kind}"] = "must be an object"
            continue
        target = out["detectors"][kind]
        params = {p.key: p for p in spec.params}
        for key, value in values.items():
            if key == "enabled":
                if isinstance(value, bool):
                    target["enabled"] = value
                else:
                    errors[f"detectors.{kind}.enabled"] = "must be true or false"
                continue
            p = params.get(key)
            if p is None:
                errors[f"detectors.{kind}.{key}"] = "unknown setting"
                continue
            try:
                target[key] = _coerce(p, value)
            except ValueError as exc:
                errors[f"detectors.{kind}.{key}"] = str(exc)
    for kind in ("syn_scan", "port_scan"):
        d = out["detectors"][kind]
        lo, hi = ("min_destinations", "critical_destinations") if kind == "syn_scan" else ("min_ports", "critical_ports")
        if d[hi] < d[lo]:
            errors[f"detectors.{kind}.{hi}"] = "must not be below the detection threshold"
            d[hi] = d[lo]
    if errors and strict:
        raise SettingsError(errors)
    return out


async def load_settings(db: AsyncSession) -> dict:
    try:
        row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                                {"k": SETTINGS_KEY})).first()
    except Exception:
        logger.warning("netflow detection: settings unreadable; using defaults", exc_info=True)
        return default_settings()
    value = row[0] if row else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    return merge_settings(value)


async def save_settings(db: AsyncSession, submitted: Any, user_id: Any = None) -> dict:
    """Validate (strict) and store; the caller commits and audits."""
    current = await load_settings(db)
    if not isinstance(submitted, dict):
        raise SettingsError({"settings": "must be an object"})
    # A partial update: detectors/params not submitted keep their current values.
    merged_input = {"quiet_minutes": submitted.get("quiet_minutes", current["quiet_minutes"]),
                    "detectors": {k: dict(v) for k, v in current["detectors"].items()}}
    dets = submitted.get("detectors", {})
    if not isinstance(dets, dict):
        raise SettingsError({"detectors": "must be an object"})
    for kind, values in dets.items():
        if kind not in merged_input["detectors"]:
            merged_input["detectors"][kind] = values
        elif isinstance(values, dict):
            merged_input["detectors"][kind].update(values)
        else:
            merged_input["detectors"][kind] = values
    value = merge_settings(merged_input, strict=True)
    await db.execute(
        text("INSERT INTO system_settings (key, value, updated_at, updated_by) "
             "VALUES (:k, CAST(:v AS jsonb), NOW(), :u) "
             "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW(), "
             "updated_by = EXCLUDED.updated_by"),
        {"k": SETTINGS_KEY, "v": json.dumps(value), "u": user_id},
    )
    _adhoc_cache.clear()
    return value


def settings_payload(settings: dict) -> dict:
    """Registry + current values for the settings form."""
    return {
        "quiet_minutes": settings["quiet_minutes"],
        "detectors": [
            {
                "kind": d.kind, "label": d.label, "category": d.category, "description": d.description,
                "method": d.method, "required_fields": list(d.required),
                "enabled": settings["detectors"][d.kind]["enabled"],
                "params": [p.as_dict(settings["detectors"][d.kind][p.key]) for p in d.params],
            }
            for d in DETECTORS
        ],
    }


# ── Lazy imports of the API foundation (avoids an import cycle) ──────────────

def _nf():
    """The NetFlow API foundation modules (imported at call time: the API
    package imports this service while it initialises)."""
    from app.api.v1.netflow import access, common, filters, sql

    return common, sql, filters, access


# ── Run context, coverage ────────────────────────────────────────────────────

@dataclass
class ExporterCoverage:
    exporter: str
    records: int = 0
    tcp_records: int = 0
    tcp_flagged: int = 0
    icmp_records: int = 0
    icmp_typed: int = 0
    duration_valid: int = 0
    legacy_records: int = 0
    max_sampling: int = 1


@dataclass
class Coverage:
    exporters: dict[str, ExporterCoverage] = field(default_factory=dict)
    known: bool = True

    @property
    def records(self) -> int:
        return sum(e.records for e in self.exporters.values())

    def exporter_rows(self) -> list[dict]:
        """Per-exporter counters (JSON-safe), the form the engine state keeps."""
        return [asdict(e) for e in sorted(self.exporters.values(), key=lambda e: e.exporter)]

    @classmethod
    def from_rows(cls, rows: Iterable[Any], visible: frozenset[str] | None = None) -> "Coverage":
        """Coverage rebuilt from ``exporter_rows`` output, narrowed to
        ``visible`` (None = every exporter)."""
        cov = cls()
        names = {f.name for f in fields(ExporterCoverage)}
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("exporter"):
                continue
            if visible is not None and row["exporter"] not in visible:
                continue
            cov.exporters[row["exporter"]] = ExporterCoverage(**{k: v for k, v in row.items() if k in names})
        return cov

    def as_dict(self) -> dict:
        tcp = sum(e.tcp_records for e in self.exporters.values())
        flagged = sum(e.tcp_flagged for e in self.exporters.values())
        return {
            "records": self.records,
            "tcp_records": tcp,
            "tcp_flagged_records": flagged,
            "flag_coverage_pct": round(flagged / tcp * 100, 1) if tcp else None,
            "exporters": len(self.exporters),
            "sampled_exporters": sorted(e.exporter for e in self.exporters.values() if e.max_sampling > 1),
            "known": self.known,
        }


@dataclass
class RunContext:
    start: datetime                 # evaluation window, half-open
    end: datetime
    vis: Any                        # FlowVisibility
    base: Any                       # FlowFilter (exporter scope)
    settings: dict
    budget_s: float
    limit: int
    persist: bool
    coverage: Coverage = field(default_factory=Coverage)
    context_start: datetime | None = None   # ICMP run context (engine: minutes before the bucket)
    now: datetime | None = None
    baseline_hours: float | None = None      # rollup history before the window

    def cfg(self, kind: str) -> dict:
        return self.settings["detectors"][kind]

    def window_params(self, start: datetime | None = None) -> dict:
        return {"t0": start or self.start, "t1": self.end,
                "t0_s": int((start or self.start).timestamp()), "t1_s": int(self.end.timestamp())}

    def where(self, start: datetime | None = None) -> tuple[str, dict]:
        """Time range + visibility scope + volume guard + exporter scope on raw."""
        common, _sql, filters, _access = _nf()
        pred, params = filters.compile_filter(self.base, common.RAW, self.vis)
        params.update(self.window_params(start))
        return f"timestamp >= %(t0)s AND timestamp < %(t1)s AND {pred}", params


def _raw() -> str:
    common, *_ = _nf()
    return common.table_ref(common.RAW)


def _disp(alias: str) -> str:
    return f"replaceRegexpOne(toString({alias}), '^::ffff:', '')"


BUCKET_SQL = "toStartOfInterval(toDateTime(timestamp, 'UTC'), INTERVAL 5 MINUTE)"
# Seconds of a bucket that lie inside the window (edge buckets are partial).
BUCKET_SECONDS_SQL = (
    "(least(toUnixTimestamp(bucket) + 300, %(t1_s)s) - greatest(toUnixTimestamp(bucket), %(t0_s)s))"
)


async def load_coverage(ctx: RunContext) -> Coverage:
    """Per-exporter record counts and field presence over the window (raw
    ``fields_present``, the same rule the exporter-minutes view applies)."""
    common, sql, *_ = _nf()
    where, params = ctx.where()
    q = f"""
        SELECT {_disp('e')} AS exporter, count() AS recs,
               countIf(protocol = 6) AS tcp, countIf(protocol = 6 AND {FLAGGED}) AS flagged,
               countIf(protocol IN (1, 58)) AS icmp, countIf(protocol IN (1, 58) AND {ICMP_TYPED}) AS typed,
               countIf({DURATION_VALID}) AS dur_ok, countIf(fields_present = 0) AS legacy,
               max(sampling_interval) AS max_sampling
        FROM (SELECT {sql.EXPORTER} AS e, * FROM {_raw()} WHERE {where})
        GROUP BY e
    """
    res = await common.run_query(q, params, budget_s=ctx.budget_s, label="detect-coverage")
    cov = Coverage()
    for r in res.result_rows:
        cov.exporters[r[0]] = ExporterCoverage(
            exporter=r[0], records=int(r[1]), tcp_records=int(r[2]), tcp_flagged=int(r[3]),
            icmp_records=int(r[4]), icmp_typed=int(r[5]), duration_valid=int(r[6]),
            legacy_records=int(r[7]), max_sampling=max(1, int(r[8] or 1)),
        )
    return cov


# ── Candidates and outcomes ──────────────────────────────────────────────────

def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def fingerprint(kind: str, exporter: str, *subject: Any) -> str:
    raw = "|".join([kind, exporter or "", *[str(s) for s in subject]])
    return hashlib.sha1(raw.encode()).hexdigest()


@dataclass
class Candidate:
    """One detector match for a subject (host, pair, address) over the window."""

    kind: str
    exporter: str
    subject: tuple
    severity: str
    confidence: str
    title: str
    description: str
    first_seen: datetime
    last_seen: datetime
    latest: tuple[datetime, datetime]          # latest occurrence segment
    occurrences: int
    method: str
    threshold: dict
    observed: dict
    evidence_filter: dict                      # canonical FlowFilter keys
    segments: list[dict] = field(default_factory=list)
    baseline: dict | None = None
    src: str | None = None
    dst: str | None = None
    host: str | None = None                    # "all traffic for this host"
    metric: float | int | None = None
    metric_label: str | None = None
    flows: int | None = None
    bytes: int | None = None
    packets: int | None = None
    extra: dict = field(default_factory=dict)
    exception: dict | None = None               # matching approved exception

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.kind, self.exporter, *self.subject)

    def evidence(self) -> dict:
        start = max(self.first_seen, self.last_seen - MAX_EVIDENCE_SPAN)
        return {
            "window": {"from": _iso(start), "to": _iso(self.last_seen)},
            "latest": {"from": _iso(self.latest[0]), "to": _iso(self.latest[1])},
            "filter": self.evidence_filter,
            "host": self.host,
            "segments": self.segments[-MAX_SEGMENTS:],
            "truncated_span": start > self.first_seen,
        }

    def metrics(self) -> dict:
        spec = DETECTOR_BY_KIND[self.kind]
        return {
            "method": self.method, "threshold": self.threshold, "observed": self.observed,
            "baseline": self.baseline, "category": spec.category, "metric": self.metric,
            "metric_label": self.metric_label, "flows": self.flows, "bytes": self.bytes,
            "packets": self.packets, **self.extra,
        }


@dataclass
class Outcome:
    """What one detector did over the window."""

    kind: str
    status: str
    reason: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    match_count: int = 0
    truncated: bool = False
    coverage_pct: float | None = None
    exporters_seen: list[str] = field(default_factory=list)
    exporters_unavailable: list[str] = field(default_factory=list)
    reduced_sensitivity: list[dict] = field(default_factory=list)
    error_id: str | None = None
    query_ms: int | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self, ran_at: datetime | None = None) -> dict:
        spec = DETECTOR_BY_KIND[self.kind]
        return {
            "kind": self.kind, "label": spec.label, "category": spec.category,
            "status": self.status, "reason": self.reason,
            "last_run_at": _iso(ran_at),
            "match_count": self.match_count, "returned": len(self.candidates), "truncated": self.truncated,
            "coverage_pct": self.coverage_pct,
            "exporters_seen": self.exporters_seen,
            "exporters_unavailable": self.exporters_unavailable,
            "reduced_sensitivity": self.reduced_sensitivity,
            "required_fields": list(spec.required),
            "error_id": self.error_id, "query_ms": self.query_ms, "notes": self.notes,
        }


def _severity_for(value: float, critical_at: float | None, base: str) -> str:
    if critical_at is not None and value >= critical_at:
        return "critical"
    return base


def _fmt_int(n: float | int | None) -> str:
    return "—" if n is None else f"{int(round(n)):,}"


def _fmt_bytes(n: float | int | None) -> str:
    if n is None:
        return "—"
    v = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB", "PB"):
        if abs(v) < 1000 or unit == "PB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.3g} {unit}"
        v /= 1000
    return f"{v:.3g} PB"


def _fmt_bps(n: float | None) -> str:
    if n is None:
        return "—"
    v = float(n)
    for unit in ("bps", "kbps", "Mbps", "Gbps", "Tbps"):
        if abs(v) < 1000 or unit == "Tbps":
            return f"{v:.3g} {unit}"
        v /= 1000
    return f"{v:.3g} Tbps"


def _proto_for(ip: str | None) -> int:
    return 58 if ip and ":" in ip else 1


def _segments_from_buckets(buckets: Iterable[datetime], ctx: RunContext, metric: Callable[[datetime], Any] | None = None) -> list[dict]:
    """Merge consecutive buckets into segments clipped to the window."""
    segs: list[dict] = []
    for b in sorted(_aware(x) for x in buckets):
        s, e = max(b, ctx.start), min(b + timedelta(seconds=BUCKET_S), ctx.end)
        if segs and _parse(segs[-1]["to"]) >= s:
            segs[-1]["to"] = _iso(max(_parse(segs[-1]["to"]), e))
            if metric is not None:
                segs[-1]["peak"] = max(segs[-1].get("peak") or 0, metric(b) or 0)
        else:
            seg = {"from": _iso(s), "to": _iso(e)}
            if metric is not None:
                seg["peak"] = metric(b)
            segs.append(seg)
    return segs


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _span(ctx: RunContext, first_bucket: datetime, last_bucket: datetime) -> tuple[datetime, datetime, tuple[datetime, datetime]]:
    fb, lb = _aware(first_bucket), _aware(last_bucket)
    first = max(fb, ctx.start)
    last = min(lb + timedelta(seconds=BUCKET_S), ctx.end)
    return first, last, (max(lb, ctx.start), last)


def _flag_availability(cov: Coverage | RunContext, out: Outcome) -> bool:
    """Apply the TCP-flag requirement: False (and ``out`` unavailable) when no
    exporter with TCP traffic exported flags. Takes the coverage (or a run
    context carrying it)."""
    cov = getattr(cov, "coverage", cov)
    exps = [e for e in cov.exporters.values() if e.tcp_records > 0]
    out.exporters_seen = sorted(e.exporter for e in exps)
    if not exps:
        out.notes.append("No TCP records in this window")
        return True
    unavailable = sorted(e.exporter for e in exps if e.tcp_flagged == 0)
    tcp = sum(e.tcp_records for e in exps)
    flagged = sum(e.tcp_flagged for e in exps)
    out.coverage_pct = round(flagged / tcp * 100, 1) if tcp else None
    out.exporters_unavailable = unavailable
    if len(unavailable) == len(exps):
        out.status = "unavailable"
        out.reason = (f"TCP flags are not exported by {_list(unavailable)} (the template has no TCP flags field); "
                      "SYN and RST checks cannot run")
        return False
    if unavailable:
        out.notes.append(f"TCP flags are not exported by {_list(unavailable)}; their traffic is not evaluated")
    elif out.coverage_pct is not None and out.coverage_pct < 99.5:
        out.notes.append(f"TCP flags exported for {out.coverage_pct:g}% of TCP records; the rest are not evaluated")
    return True


def _list(items: list[str], n: int = 3) -> str:
    if len(items) <= n:
        return ", ".join(items)
    return f"{', '.join(items[:n])} and {len(items) - n} more"


def _sampling_note(cov: Coverage | RunContext, out: Outcome) -> None:
    cov = getattr(cov, "coverage", cov)
    # Most heavily sampled first (they lose the most), so a shortened note
    # never leaves out the worst one; the note says how many more there are
    # and reduced_sensitivity lists every one of them (X-09).
    sampled = sorted((e for e in cov.exporters.values() if e.max_sampling > 1),
                     key=lambda e: (-e.max_sampling, e.exporter))
    if sampled:
        out.reduced_sensitivity = [{"exporter": e.exporter, "sampling": e.max_sampling} for e in sampled]
        out.notes.append(
            "Reduced sensitivity on sampled exporters ("
            + _list([f"{e.exporter} 1:{e.max_sampling}" for e in sampled])
            + "): record-count thresholds see only the sampled share of the traffic"
        )



_FLAG_KINDS = frozenset({"syn_scan", "port_scan", "rst_flood"})
_ICMP_RATE_KINDS = frozenset({"icmp_flood", "icmp_flood_target"})


def annotate_coverage(kind: str, cov: Coverage, out: Outcome) -> bool:
    """The coverage-derived part of a detector's outcome: which exporters it
    saw, which lack the fields it needs (``unavailable`` when all do), its
    field coverage and the notes about them. Returns False when the detector
    cannot run on this coverage.

    The one definition used both when a detector runs and when the engine's
    stored status is narrowed to a restricted caller (``scoped_detectors``):
    a scoped viewer gets these values recomputed over the exporters it may
    see, never the fleet-wide ones that name other teams' exporters.
    """
    if kind in _FLAG_KINDS:
        if not _flag_availability(cov, out):
            return False
        _sampling_note(cov, out)
        return True
    if kind == "icmp_sweep":
        exps = [e for e in cov.exporters.values() if e.icmp_records]
        out.exporters_seen = sorted(e.exporter for e in exps)
        if not exps:
            out.notes.append("No ICMP records in this window")
            return False
        untyped = sorted(e.exporter for e in exps if e.icmp_typed == 0)
        out.exporters_unavailable = untyped
        if len(untyped) == len(exps):
            out.status = "unavailable"
            out.reason = f"ICMP type and code are not exported by {_list(untyped)}; echo requests cannot be told apart"
            return False
        if untyped:
            out.notes.append(f"ICMP type is not exported by {_list(untyped)}; their traffic is not evaluated")
        _sampling_note(cov, out)
        return True
    if kind in _ICMP_RATE_KINDS:
        out.exporters_seen = sorted(e.exporter for e in cov.exporters.values() if e.icmp_records)
        if not out.exporters_seen:
            out.notes.append("No ICMP records in this window")
            return False
        untimed = [e.exporter for e in cov.exporters.values() if e.icmp_records and e.duration_valid == 0]
        if untimed:
            out.notes.append(f"Flow start times are not exported by {_list(sorted(untimed))}; "
                             "their records are assumed to last one minute")
        return True
    out.exporters_seen = sorted(cov.exporters)
    return True


# ── Detectors ────────────────────────────────────────────────────────────────

async def _q(ctx: RunContext, sql_text: str, params: dict, kind: str):
    common, *_ = _nf()
    return await common.run_query(sql_text, params, budget_s=ctx.budget_s, label=f"detect-{kind}")


async def detect_syn_scan(ctx: RunContext, out: Outcome) -> None:
    if not annotate_coverage("syn_scan", ctx.coverage, out):
        return
    cfg = ctx.cfg("syn_scan")
    _common, sql, *_ = _nf()
    where, params = ctx.where()
    params.update(min_d=cfg["min_destinations"], lim=ctx.limit)
    q = f"""
        SELECT {_disp('e')}, {_disp('src')}, count() AS nb, min(bucket), max(bucket),
               max(dsts) AS peak, sum(recs), sum(pk), sum(vol), groupUniqArrayArray(12)(ports),
               argMax(dsts, bucket), max(nports), groupArray(tuple(bucket, dsts)),
               count() OVER () AS total
        FROM (
            SELECT e, bucket, src, uniqExact(dst) AS dsts, uniqExact(dst_port) AS nports, count() AS recs,
                   sum(packets) AS pk, sum(bytes) AS vol, groupUniqArray(12)(dst_port) AS ports
            FROM (SELECT {sql.EXPORTER} AS e, {BUCKET_SQL} AS bucket, {sql.SRC} AS src, {sql.DST} AS dst,
                         dst_port, packets, bytes
                  FROM {_raw()} WHERE {where} AND protocol = 6 AND {FLAGGED}
                    AND bitAnd(tcp_flags, 2) != 0 AND bitAnd(tcp_flags, 16) = 0)
            GROUP BY e, bucket, src
            HAVING dsts >= %(min_d)s
        )
        GROUP BY e, src
        ORDER BY peak DESC
        LIMIT %(lim)s
    """
    res = await _q(ctx, q, params, "syn_scan")
    rows = res.result_rows
    out.match_count = int(rows[0][13]) if rows else 0
    out.truncated = out.match_count > len(rows)
    for r in rows:
        exp, src, nb, fb, lb, peak = r[0], r[1], int(r[2]), r[3], r[4], int(r[5])
        per_bucket = {_aware(b): int(v) for b, v in r[12]}
        first, last, latest = _span(ctx, fb, lb)
        ports = sorted(int(p) for p in r[9])
        sev = _severity_for(peak, cfg["critical_destinations"], cfg["severity"])
        out.candidates.append(Candidate(
            kind="syn_scan", exporter=exp, subject=(src,), severity=sev,
            confidence="high" if peak >= 2 * cfg["min_destinations"] else "medium",
            title="TCP host sweep",
            description=(f"{src} sent connection attempts (SYN without ACK) to {peak:,} different hosts "
                         f"within 5 minutes (threshold {cfg['min_destinations']:,})"
                         + (f", in {nb} five-minute periods" if nb > 1 else "")),
            first_seen=first, last_seen=last, latest=latest, occurrences=nb,
            method=DETECTOR_BY_KIND["syn_scan"].method,
            threshold={"destinations_per_5min": cfg["min_destinations"], "critical_at": cfg["critical_destinations"]},
            observed={"peak_destinations": peak, "latest_destinations": int(r[10]), "peak_ports": int(r[11]),
                      "sample_ports": ports},
            evidence_filter={"exporter": exp, "src": src, "protocol": "6", "tcpflag": "syn_only"},
            segments=_segments_from_buckets(per_bucket, ctx, per_bucket.get),
            src=src, host=src, metric=peak, metric_label="destinations",
            flows=int(r[6]), packets=int(r[7]), bytes=int(r[8]),
        ))


async def detect_port_scan(ctx: RunContext, out: Outcome) -> None:
    if not annotate_coverage("port_scan", ctx.coverage, out):
        return
    cfg = ctx.cfg("port_scan")
    _common, sql, *_ = _nf()
    where, params = ctx.where()
    params.update(min_p=cfg["min_ports"], lim=ctx.limit)
    q = f"""
        SELECT {_disp('e')}, {_disp('src')}, {_disp('dst')}, count() AS nb, min(bucket), max(bucket),
               max(nports) AS peak, sum(recs), sum(pk), sum(vol), argMax(nports, bucket),
               min(lo), max(hi), groupArray(tuple(bucket, nports)), count() OVER () AS total
        FROM (
            SELECT e, bucket, src, dst, uniqExact(dst_port) AS nports, count() AS recs, sum(packets) AS pk,
                   sum(bytes) AS vol, min(dst_port) AS lo, max(dst_port) AS hi
            FROM (SELECT {sql.EXPORTER} AS e, {BUCKET_SQL} AS bucket, {sql.SRC} AS src, {sql.DST} AS dst,
                         dst_port, packets, bytes
                  FROM {_raw()} WHERE {where} AND protocol = 6 AND {FLAGGED}
                    AND bitAnd(tcp_flags, 2) != 0 AND bitAnd(tcp_flags, 16) = 0)
            GROUP BY e, bucket, src, dst
            HAVING nports >= %(min_p)s
        )
        GROUP BY e, src, dst
        ORDER BY peak DESC
        LIMIT %(lim)s
    """
    res = await _q(ctx, q, params, "port_scan")
    rows = res.result_rows
    out.match_count = int(rows[0][14]) if rows else 0
    out.truncated = out.match_count > len(rows)
    for r in rows:
        exp, src, dst, nb, peak = r[0], r[1], r[2], int(r[3]), int(r[6])
        per_bucket = {_aware(b): int(v) for b, v in r[13]}
        first, last, latest = _span(ctx, r[4], r[5])
        sev = _severity_for(peak, cfg["critical_ports"], cfg["severity"])
        out.candidates.append(Candidate(
            kind="port_scan", exporter=exp, subject=(src, dst), severity=sev,
            confidence="high" if peak >= 2 * cfg["min_ports"] else "medium",
            title="TCP port scan",
            description=(f"{src} probed {peak:,} ports on {dst} within 5 minutes "
                         f"(ports {int(r[11])}–{int(r[12])}; threshold {cfg['min_ports']:,})"),
            first_seen=first, last_seen=last, latest=latest, occurrences=nb,
            method=DETECTOR_BY_KIND["port_scan"].method,
            threshold={"ports_per_5min": cfg["min_ports"], "critical_at": cfg["critical_ports"]},
            observed={"peak_ports": peak, "latest_ports": int(r[10]), "port_low": int(r[11]),
                      "port_high": int(r[12])},
            evidence_filter={"exporter": exp, "src": src, "dst": dst, "protocol": "6", "tcpflag": "syn_only"},
            segments=_segments_from_buckets(per_bucket, ctx, per_bucket.get),
            src=src, dst=dst, host=src, metric=peak, metric_label="ports",
            flows=int(r[7]), packets=int(r[8]), bytes=int(r[9]),
        ))


async def detect_rst_flood(ctx: RunContext, out: Outcome) -> None:
    if not annotate_coverage("rst_flood", ctx.coverage, out):
        return
    cfg = ctx.cfg("rst_flood")
    _common, sql, *_ = _nf()
    where, params = ctx.where()
    params.update(min_f=cfg["min_flows"], ratio=cfg["min_ratio_pct"] / 100.0, lim=ctx.limit)
    q = f"""
        SELECT {_disp('e')}, {_disp('src')}, count() AS nb, min(bucket), max(bucket), max(ratio) AS peak,
               sum(rsts), sum(recs), argMax(ratio, bucket), groupArray(tuple(bucket, ratio)),
               count() OVER () AS total
        FROM (
            SELECT e, bucket, src, count() AS recs, countIf(bitAnd(tcp_flags, 4) != 0) AS rsts,
                   rsts / recs AS ratio
            FROM (SELECT {sql.EXPORTER} AS e, {BUCKET_SQL} AS bucket, {sql.SRC} AS src, tcp_flags
                  FROM {_raw()} WHERE {where} AND protocol = 6 AND {FLAGGED})
            GROUP BY e, bucket, src
            HAVING recs >= %(min_f)s AND rsts >= recs * %(ratio)s
        )
        GROUP BY e, src
        ORDER BY sum(rsts) DESC
        LIMIT %(lim)s
    """
    res = await _q(ctx, q, params, "rst_flood")
    rows = res.result_rows
    out.match_count = int(rows[0][10]) if rows else 0
    out.truncated = out.match_count > len(rows)
    for r in rows:
        exp, src, nb = r[0], r[1], int(r[2])
        rsts, recs = int(r[6]), int(r[7])
        per_bucket = {_aware(b): round(float(v) * 100, 1) for b, v in r[9]}
        first, last, latest = _span(ctx, r[3], r[4])
        pct = rsts / recs * 100 if recs else 0.0
        out.candidates.append(Candidate(
            kind="rst_flood", exporter=exp, subject=(src,), severity=cfg["severity"],
            confidence="high" if recs >= 5 * cfg["min_flows"] else "medium",
            title="High TCP reset ratio",
            description=(f"{src} set RST on {rsts:,} of {recs:,} flagged TCP records ({pct:.0f}%) "
                         f"in {nb} five-minute period{'s' if nb != 1 else ''} (threshold {cfg['min_ratio_pct']:g}% "
                         f"of at least {cfg['min_flows']:,} records)"),
            first_seen=first, last_seen=last, latest=latest, occurrences=nb,
            method=DETECTOR_BY_KIND["rst_flood"].method,
            threshold={"min_flows_per_5min": cfg["min_flows"], "min_ratio_pct": cfg["min_ratio_pct"]},
            observed={"rst_records": rsts, "flagged_tcp_records": recs, "ratio_pct": round(pct, 1),
                      "peak_ratio_pct": round(float(r[5]) * 100, 1)},
            evidence_filter={"exporter": exp, "src": src, "protocol": "6", "tcpflag": "rst"},
            segments=_segments_from_buckets(per_bucket, ctx, per_bucket.get),
            src=src, host=src, metric=rsts, metric_label="RSTs", flows=recs,
        ))


def _in_ranges(side: str, ranges: Iterable[str]) -> str:
    _common, sql, *_ = _nf()
    terms = [sql.side_in_network(side, ipaddress.ip_network(r), None, None) for r in ranges]
    return "(" + " OR ".join(terms) + ")"


async def detect_sensitive_egress(ctx: RunContext, out: Outcome) -> None:
    cfg = ctx.cfg("sensitive_egress")
    _common, sql, *_ = _nf()
    where, params = ctx.where()
    ports = [int(p) for p in cfg["ports"]]
    params.update(ports=ports, min_b=cfg["min_bytes"], lim=ctx.limit)
    annotate_coverage("sensitive_egress", ctx.coverage, out)
    q = f"""
        SELECT {_disp('e')}, {_disp('src')}, {_disp('dst')}, count() AS nb, min(bucket), max(bucket),
               sum(vol) AS total, sum(recs), sum(acked), groupUniqArrayArray(16)(dports), sum(pk),
               groupArray(tuple(bucket, vol)), count() OVER () AS n
        FROM (
            SELECT e, bucket, src, dst, sum(bytes) AS vol, count() AS recs, sum(packets) AS pk,
                   countIf(protocol = 6 AND {FLAGGED} AND bitAnd(tcp_flags, 16) != 0) AS acked,
                   groupUniqArray(16)(dst_port) AS dports
            FROM (SELECT {sql.EXPORTER} AS e, {BUCKET_SQL} AS bucket, {sql.SRC} AS src, {sql.DST} AS dst,
                         dst_port, protocol, fields_present, tcp_flags, bytes, packets
                  FROM {_raw()} WHERE {where} AND protocol IN (6, 17) AND dst_port IN %(ports)s
                    AND {_in_ranges('src', INTERNAL)} AND NOT {_in_ranges('dst', SPECIAL_USE)})
            GROUP BY e, bucket, src, dst
            HAVING acked > 0 OR vol >= %(min_b)s
        )
        GROUP BY e, src, dst
        ORDER BY total DESC
        LIMIT %(lim)s
    """
    res = await _q(ctx, q, params, "sensitive_egress")
    rows = res.result_rows
    out.match_count = int(rows[0][12]) if rows else 0
    out.truncated = out.match_count > len(rows)
    for r in rows:
        exp, src, dst, nb = r[0], r[1], r[2], int(r[3])
        total, recs, acked = int(r[6]), int(r[7]), int(r[8])
        dports = sorted(int(p) for p in r[9])
        per_bucket = {_aware(b): int(v) for b, v in r[11]}
        first, last, latest = _span(ctx, r[4], r[5])
        basis = "ACK seen" if acked else f"{_fmt_bytes(total)} transferred"
        out.candidates.append(Candidate(
            kind="sensitive_egress", exporter=exp, subject=(src, dst), severity=cfg["severity"],
            confidence="high" if acked else "medium",
            title="Sensitive service reached on the internet",
            description=(f"{src} reached public address {dst} on port{'s' if len(dports) > 1 else ''} "
                         f"{', '.join(str(p) for p in dports)} ({basis}; {_fmt_bytes(total)} in {recs:,} records)"),
            first_seen=first, last_seen=last, latest=latest, occurrences=nb,
            method=DETECTOR_BY_KIND["sensitive_egress"].method,
            threshold={"ports": ports, "min_bytes_without_ack": cfg["min_bytes"]},
            observed={"ports": dports, "bytes": total, "records": recs, "acked_records": acked},
            evidence_filter={"exporter": exp, "src": src, "dst": dst, "dst_ports": ",".join(str(p) for p in dports)},
            segments=_segments_from_buckets(per_bucket, ctx, per_bucket.get),
            src=src, dst=dst, host=src, metric=total, metric_label="bytes", flows=recs, bytes=total,
            packets=int(r[10]),
        ))


def _icmp_series_sql(ctx: RunContext, key: str, peer: str, extra_where: str = "") -> tuple[str, dict]:
    """Per-minute ICMP packet rate per (exporter, key), records spread over
    their durations (invalid duration: one minute; zero: the end minute)."""
    _common, sql, *_ = _nf()
    start = ctx.context_start or ctx.start
    where, params = ctx.where(start)
    params["m0"] = int(start.timestamp()) // 60
    params["m1"] = -(-int(ctx.end.timestamp()) // 60)
    key_expr, peer_expr = (sql.SRC, sql.DST) if key == "src" else (sql.DST, sql.SRC)
    q = f"""
        SELECT {_disp('e')} AS de, {_disp('k')} AS dk, mi, sum(share) / 60 AS pps, uniqExact(peer) AS peers,
               sum(vshare) AS vol, count() AS recs, sum(dur_ok) AS timed
        FROM (
            SELECT e, k, peer, dur_ok, mi,
                   if(d_ms = 0, toFloat64(packets),
                      packets * (least(end_ms, (mi + 1) * 60000) - greatest(start_ms, mi * 60000)) / d_ms) AS share,
                   if(d_ms = 0, toFloat64(bytes),
                      bytes * (least(end_ms, (mi + 1) * 60000) - greatest(start_ms, mi * 60000)) / d_ms) AS vshare
            FROM (
                SELECT {sql.EXPORTER} AS e, {key_expr} AS k, {peer_expr} AS peer, packets, bytes,
                       toUnixTimestamp64Milli(timestamp) AS end_ms,
                       {DURATION_VALID} AS dur_ok,
                       toInt64(if(dur_ok, least(last_switched_ms - first_switched_ms, 3600000), 60000)) AS d_ms,
                       end_ms - d_ms AS start_ms,
                       arrayJoin(range(intDiv(start_ms, 60000), intDiv(end_ms, 60000) + 1)) AS mi
                FROM {_raw()} WHERE {where} AND protocol IN (1, 58) {extra_where}
            )
            WHERE mi >= %(m0)s AND mi < %(m1)s
        )
        GROUP BY e, k, mi
        HAVING pps >= %(min_pps)s
        ORDER BY e, k, mi
        LIMIT %(row_cap)s
    """
    return q, params


ICMP_ROW_CAP = 50000


async def _icmp_baselines(ctx: RunContext, key: str, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """7-day p95 hourly packet rate per (exporter, key) from the hourly rollup."""
    if not keys:
        return {}
    common, sql, filters, _access = _nf()
    hour0 = sql.floor_to(ctx.context_start or ctx.start, 3600)
    pred, params = filters.compile_filter(ctx.base, common.CONVERSATIONS_1H, ctx.vis)
    key_expr = sql.SRC if key == "src" else sql.DST
    params.update(b0=hour0 - timedelta(days=7), b1=hour0,
                  ks=sorted({sql.ip_param(k) for _e, k in keys}),
                  es=sorted({sql.ip_param(e) for e, _k in keys}))
    q = f"""
        SELECT {_disp('e')}, {_disp('k')}, quantileExact(0.95)(hp) / 3600 AS p95_pps, count() AS hours,
               max(hp) / 3600 AS max_pps
        FROM (
            SELECT {sql.EXPORTER} AS e, {key_expr} AS k, timestamp, sum(packets) AS hp
            FROM {common.table_ref(common.CONVERSATIONS_1H)}
            WHERE timestamp >= %(b0)s AND timestamp < %(b1)s AND {pred} AND protocol IN (1, 58)
              AND {key_expr} IN %(ks)s AND {sql.EXPORTER} IN %(es)s
            GROUP BY e, k, timestamp
        )
        GROUP BY e, k
    """
    res = await _q(ctx, q, params, f"icmp-baseline-{key}")
    return {(r[0], r[1]): {"p95_pps": float(r[2]), "hours": int(r[3]), "max_hourly_pps": float(r[4]),
                           "source": "7-day p95 of hourly ICMP packets (hourly rollup)"}
            for r in res.result_rows}


def _runs(minutes: list[int], min_len: int) -> list[tuple[int, int]]:
    """Consecutive-minute runs (inclusive bounds) of at least ``min_len``."""
    runs: list[tuple[int, int]] = []
    start = prev = None
    for m in sorted(set(minutes)):
        if start is None:
            start = prev = m
        elif m == prev + 1:
            prev = m
        else:
            if prev - start + 1 >= min_len:
                runs.append((start, prev))
            start = prev = m
    if start is not None and prev - start + 1 >= min_len:
        runs.append((start, prev))
    return runs


def _minute(m: int) -> datetime:
    return datetime.fromtimestamp(m * 60, tz=timezone.utc)


async def _detect_icmp_flood(ctx: RunContext, out: Outcome, kind: str, key: str) -> None:
    cfg = ctx.cfg(kind)
    if not annotate_coverage(kind, ctx.coverage, out):
        return
    # Exporters without flow start times: their records are assumed to last a
    # minute, so a match on them is never high confidence (noted by annotate_coverage).
    untimed = {e.exporter for e in ctx.coverage.exporters.values() if e.icmp_records and e.duration_valid == 0}
    q, params = _icmp_series_sql(ctx, key, "dst" if key == "src" else "src")
    params.update(min_pps=cfg["min_pps"], row_cap=ICMP_ROW_CAP + 1)
    res = await _q(ctx, q, params, kind)
    rows = res.result_rows
    if len(rows) > ICMP_ROW_CAP:
        out.truncated = True
        out.notes.append("More over-threshold minutes than can be evaluated at once; narrow the window")
        rows = rows[:ICMP_ROW_CAP]
    series: dict[tuple[str, str], dict[int, tuple]] = {}
    for de, dk, mi, pps, peers, vol, recs, timed in rows:
        series.setdefault((de, dk), {})[int(mi)] = (float(pps), int(peers), float(vol), int(recs), int(timed))
    baselines = await _icmp_baselines(ctx, key, list(series))
    t0_min = int(ctx.start.timestamp()) // 60
    matches: list[Candidate] = []
    for (exp, subj), mins in series.items():
        base = baselines.get((exp, subj))
        required = float(cfg["min_pps"])
        if base is not None and base["hours"] > 0:
            required = max(required, cfg["baseline_multiplier"] * base["p95_pps"])
        qualifying = [m for m, v in mins.items() if v[0] >= required]
        runs = [r for r in _runs(qualifying, cfg["min_minutes"]) if r[1] >= t0_min]
        if not runs:
            continue
        in_runs = [m for a, b in runs for m in range(a, b + 1)]
        peak = max(mins[m][0] for m in in_runs)
        packets = sum(mins[m][0] * 60 for m in in_runs)
        vol = sum(mins[m][2] for m in in_runs)
        peers = max(mins[m][1] for m in in_runs)
        longest = max(b - a + 1 for a, b in runs)
        timed = sum(mins[m][4] for m in in_runs)
        first_run, last_run = runs[0], runs[-1]
        first = max(_minute(first_run[0]), ctx.context_start or ctx.start)
        last = _minute(last_run[1] + 1)
        latest = (_minute(last_run[0]), last)
        if base is None or base["hours"] == 0:
            confidence = "low"
        elif base["hours"] >= 72 and peak >= 2 * required and timed > 0:
            confidence = "high"
        else:
            confidence = "medium"
        if exp in untimed and confidence == "high":
            confidence = "medium"
        proto = _proto_for(subj)
        sev = cfg["severity"]
        if kind == "icmp_flood":
            title = "ICMP flood from a host"
            desc = (f"{subj} sent ICMP at up to {_fmt_int(peak)} packets/s for {longest} consecutive minute"
                    f"{'s' if longest != 1 else ''} to {peers:,} destination{'s' if peers != 1 else ''} "
                    f"(threshold {_fmt_int(required)} pps for {cfg['min_minutes']} min)")
            filt = {"exporter": exp, "src": subj, "protocol": str(proto)}
            src, dst = subj, None
        else:
            title = "ICMP flood toward a host"
            desc = (f"{subj} received ICMP at up to {_fmt_int(peak)} packets/s from {peers:,} source"
                    f"{'s' if peers != 1 else ''} for {longest} consecutive minute{'s' if longest != 1 else ''} "
                    f"(threshold {_fmt_int(required)} pps for {cfg['min_minutes']} min)")
            filt = {"exporter": exp, "dst": subj, "protocol": str(proto)}
            src, dst = None, subj
        if base is not None and base["hours"] > 0:
            desc += f"; its 7-day p95 hourly rate is {base['p95_pps']:.1f} pps"
        else:
            desc += "; no ICMP history to compare with (low confidence)"
        segments = [{"from": _iso(max(_minute(a), ctx.context_start or ctx.start)), "to": _iso(_minute(b + 1)),
                     "peak": round(max(mins[m][0] for m in range(a, b + 1)), 1)} for a, b in runs]
        matches.append(Candidate(
            kind=kind, exporter=exp, subject=(subj,), severity=sev, confidence=confidence,
            title=title, description=desc, first_seen=first, last_seen=last, latest=latest,
            occurrences=len(runs), method=DETECTOR_BY_KIND[kind].method,
            threshold={"min_pps": cfg["min_pps"], "effective_pps": round(required, 1),
                       "min_minutes": cfg["min_minutes"], "baseline_multiplier": cfg["baseline_multiplier"]},
            observed={"peak_pps": round(peak, 1), "sustained_minutes": longest, "packets": int(packets),
                      "bytes": int(vol), "peers": peers, "runs": len(runs)},
            baseline=base, evidence_filter=filt, segments=segments,
            src=src, dst=dst, host=subj, metric=round(peak, 1), metric_label="packets/s",
            packets=int(packets), bytes=int(vol),
        ))
    matches.sort(key=lambda c: c.observed["peak_pps"], reverse=True)
    out.match_count = len(matches)
    out.truncated = out.truncated or len(matches) > ctx.limit
    out.candidates = matches[: ctx.limit]


def _plain(value: Any) -> str:
    text_ = str(value)
    return text_[7:] if text_.startswith("::ffff:") and "." in text_ else text_


async def detect_icmp_flood(ctx: RunContext, out: Outcome) -> None:
    await _detect_icmp_flood(ctx, out, "icmp_flood", "src")


async def detect_icmp_flood_target(ctx: RunContext, out: Outcome) -> None:
    await _detect_icmp_flood(ctx, out, "icmp_flood_target", "dst")


async def detect_icmp_sweep(ctx: RunContext, out: Outcome) -> None:
    if not annotate_coverage("icmp_sweep", ctx.coverage, out):
        return
    cfg = ctx.cfg("icmp_sweep")
    _common, sql, *_ = _nf()
    where, params = ctx.where()
    params.update(min_d=cfg["min_destinations"], lim=ctx.limit)
    q = f"""
        SELECT {_disp('e')}, {_disp('src')}, count() AS nb, min(bucket), max(bucket), max(dsts) AS peak,
               sum(recs), sum(pk), groupArray(tuple(bucket, dsts)), any(proto), count() OVER () AS total
        FROM (
            SELECT e, bucket, src, uniqExact(dst) AS dsts, count() AS recs, sum(packets) AS pk,
                   any(protocol) AS proto
            FROM (SELECT {sql.EXPORTER} AS e, {BUCKET_SQL} AS bucket, {sql.SRC} AS src, {sql.DST} AS dst,
                         packets, protocol
                  FROM {_raw()} WHERE {where} AND {ECHO_REQUEST_PORTS})
            GROUP BY e, bucket, src
            HAVING dsts >= %(min_d)s
        )
        GROUP BY e, src
        ORDER BY peak DESC
        LIMIT %(lim)s
    """
    res = await _q(ctx, q, params, "icmp_sweep")
    rows = res.result_rows
    out.match_count = int(rows[0][10]) if rows else 0
    out.truncated = out.match_count > len(rows)
    for r in rows:
        exp, src, nb, peak = r[0], r[1], int(r[2]), int(r[5])
        per_bucket = {_aware(b): int(v) for b, v in r[8]}
        first, last, latest = _span(ctx, r[3], r[4])
        out.candidates.append(Candidate(
            kind="icmp_sweep", exporter=exp, subject=(src,), severity=cfg["severity"],
            confidence="high" if peak >= 2 * cfg["min_destinations"] else "medium",
            title="ICMP sweep",
            description=(f"{src} sent echo requests to {peak:,} different hosts within 5 minutes "
                         f"(threshold {cfg['min_destinations']:,})"),
            first_seen=first, last_seen=last, latest=latest, occurrences=nb,
            method=DETECTOR_BY_KIND["icmp_sweep"].method,
            threshold={"destinations_per_5min": cfg["min_destinations"]},
            observed={"peak_destinations": peak},
            evidence_filter={"exporter": exp, "src": src, "protocol": str(int(r[9]))},
            segments=_segments_from_buckets(per_bucket, ctx, per_bucket.get),
            src=src, host=src, metric=peak, metric_label="destinations", flows=int(r[6]), packets=int(r[7]),
        ))


async def detect_dominant_sender(ctx: RunContext, out: Outcome) -> None:
    cfg = ctx.cfg("dominant_sender")
    _common, sql, *_ = _nf()
    annotate_coverage("dominant_sender", ctx.coverage, out)
    where, params = ctx.where()
    params.update(share=cfg["min_share_pct"] / 100.0, rate=cfg["min_rate_mbps"] * 1e6)
    q = f"""
        SELECT {_disp('e')}, {_disp('src')}, bucket, b, tot, secs
        FROM (
            SELECT e, bucket, src, b, tot, {BUCKET_SECONDS_SQL} AS secs
            FROM (
                SELECT e, bucket, src, sum(bytes) AS b, sum(sum(bytes)) OVER (PARTITION BY e, bucket) AS tot
                FROM (SELECT {sql.EXPORTER} AS e, {BUCKET_SQL} AS bucket, {sql.SRC} AS src, bytes
                      FROM {_raw()} WHERE {where})
                GROUP BY e, bucket, src
            )
        )
        WHERE b >= tot * %(share)s AND secs > 0 AND b * 8 >= %(rate)s * secs
        ORDER BY bucket
        LIMIT 100000
    """
    res = await _q(ctx, q, params, "dominant_sender")
    per_key: dict[tuple[str, str], list[tuple]] = {}
    for e, src, bucket, b, tot, secs in res.result_rows:
        per_key.setdefault((e, src), []).append((_aware(bucket), int(b), int(tot), int(secs)))
    cands: list[Candidate] = []
    for (exp, src), items in per_key.items():
        b_sum = sum(i[1] for i in items)
        tot_sum = sum(i[2] for i in items)
        secs = sum(i[3] for i in items)
        peak_share = max(i[1] / i[2] for i in items if i[2]) * 100
        share = b_sum / tot_sum * 100 if tot_sum else 0.0
        rate = b_sum * 8 / secs if secs else None
        buckets = {i[0]: round(i[1] / i[2] * 100, 1) for i in items if i[2]}
        first, last, latest = _span(ctx, items[0][0], items[-1][0])
        cands.append(Candidate(
            kind="dominant_sender", exporter=exp, subject=(src,), severity=cfg["severity"],
            confidence="high" if len(items) >= 3 else "medium",
            title="Dominant sender",
            description=(f"{src} sent {_fmt_bytes(b_sum)}, {share:.0f}% of the {_fmt_bytes(tot_sum)} exporter {exp} "
                         f"observed in the {len(items)} five-minute period{'s' if len(items) != 1 else ''} it dominated "
                         f"(average {_fmt_bps(rate)}; threshold {cfg['min_share_pct']:g}% and {cfg['min_rate_mbps']:g} Mbps)"),
            first_seen=first, last_seen=last, latest=latest, occurrences=len(items),
            method=DETECTOR_BY_KIND["dominant_sender"].method,
            threshold={"min_share_pct": cfg["min_share_pct"], "min_rate_mbps": cfg["min_rate_mbps"]},
            observed={"share_pct": round(share, 1), "peak_share_pct": round(peak_share, 1), "bytes": b_sum,
                      "total_bytes": tot_sum, "rate_bps": round(rate, 1) if rate else None},
            evidence_filter={"exporter": exp, "src": src},
            segments=_segments_from_buckets(buckets, ctx, buckets.get),
            src=src, host=src, metric=round(share, 1), metric_label="% of traffic", bytes=b_sum,
        ))
    cands.sort(key=lambda c: c.bytes or 0, reverse=True)
    out.match_count = len(cands)
    out.truncated = len(cands) > ctx.limit
    out.candidates = cands[: ctx.limit]


VOLUME_CANDIDATE_CAP = 5000
VOLUME_HOST_CAP = 500


async def detect_volume_spike(ctx: RunContext, out: Outcome) -> None:
    cfg = ctx.cfg("volume_spike")
    common, sql, filters, _access = _nf()
    annotate_coverage("volume_spike", ctx.coverage, out)
    history_h = ctx.baseline_hours
    if history_h is not None and history_h < cfg["min_baseline_hours"]:
        out.status = "no_coverage"
        out.reason = (f"Baselines need {cfg['min_baseline_hours']} hours of hourly-rollup history before the window; "
                      f"{max(0.0, history_h):.0f} hours are available")
        return
    where, params = ctx.where()
    params.update(rate=cfg["min_rate_mbps"] * 1e6, cap=VOLUME_CANDIDATE_CAP + 1)
    q = f"""
        SELECT {_disp('e')}, {_disp('src')}, bucket, b, secs
        FROM (
            SELECT e, bucket, src, sum(bytes) AS b, {BUCKET_SECONDS_SQL} AS secs
            FROM (SELECT {sql.EXPORTER} AS e, {BUCKET_SQL} AS bucket, {sql.SRC} AS src, bytes
                  FROM {_raw()} WHERE {where})
            GROUP BY e, bucket, src
            HAVING secs > 0 AND b * 8 >= %(rate)s * secs
        )
        ORDER BY b DESC
        LIMIT %(cap)s
    """
    res = await _q(ctx, q, params, "volume_spike")
    rows = res.result_rows
    if len(rows) > VOLUME_CANDIDATE_CAP:
        out.truncated = True
        out.notes.append(f"Only the {VOLUME_CANDIDATE_CAP:,} busiest host-periods were compared with baselines")
        rows = rows[:VOLUME_CANDIDATE_CAP]
    per_host: dict[tuple[str, str], list[tuple]] = {}
    for e, src, bucket, b, secs in rows:
        per_host.setdefault((e, src), []).append((_aware(bucket), int(b), int(secs)))
    if not per_host:
        return
    hosts = sorted(per_host, key=lambda k: -max(i[1] for i in per_host[k]))[:VOLUME_HOST_CAP]
    if len(per_host) > VOLUME_HOST_CAP:
        out.truncated = True
    hour0 = sql.floor_to(ctx.start, 3600)
    pred, bparams = filters.compile_filter(ctx.base, common.CONVERSATIONS_1H, ctx.vis)
    bparams.update(b0=hour0 - timedelta(days=28), b1=hour0, w0=hour0 - timedelta(days=7),
                   ks=sorted({sql.ip_param(k) for _e, k in hosts}),
                   es=sorted({sql.ip_param(e) for e, _k in hosts}))
    bq = f"""
        SELECT {_disp('e')}, {_disp('k')}, how, count() AS n, median(hb) AS med,
               countIf(timestamp >= %(w0)s) AS n7, quantileExactIf(0.95)(hb, timestamp >= %(w0)s) AS p95_7
        FROM (
            SELECT {sql.EXPORTER} AS e, {sql.SRC} AS k, timestamp,
                   (toDayOfWeek(timestamp) - 1) * 24 + toHour(timestamp) AS how, sum(bytes) AS hb
            FROM {common.table_ref(common.CONVERSATIONS_1H)}
            WHERE timestamp >= %(b0)s AND timestamp < %(b1)s AND {pred}
              AND {sql.SRC} IN %(ks)s AND {sql.EXPORTER} IN %(es)s
            GROUP BY e, k, timestamp
        )
        GROUP BY e, k, how
    """
    bres = await _q(ctx, bq, bparams, "volume-baseline")
    how_med: dict[tuple[str, str, int], tuple[int, float]] = {}
    week_p95: dict[tuple[str, str], list[tuple[int, float]]] = {}
    host_hours: dict[tuple[str, str], int] = {}
    for e, k, how, n, med, n7, p95_7 in bres.result_rows:
        how_med[(e, k, int(how))] = (int(n), float(med))
        host_hours[(e, k)] = host_hours.get((e, k), 0) + int(n)
        if n7:
            week_p95.setdefault((e, k), []).append((int(n7), float(p95_7)))
    cands: list[Candidate] = []
    for key in hosts:
        exp, src = key
        hits: list[tuple] = []
        baseline_used: dict | None = None
        for bucket, b, secs in per_host[key]:
            rate = b * 8 / secs
            how = (bucket.isoweekday() - 1) * 24 + bucket.hour
            n, med = how_med.get((exp, src, how), (0, 0.0))
            if n >= 2:
                base_bps, basis, conf = med * 8 / 3600, "hour_of_week_median", "high" if n >= 3 else "medium"
            else:
                p = week_p95.get(key)
                hours7 = sum(x[0] for x in p) if p else 0
                if not p or hours7 < cfg["min_baseline_hours"]:
                    continue
                base_bps = max(x[1] for x in p) * 8 / 3600
                basis, conf = "7_day_p95", "low"
            if base_bps <= 0:
                continue
            if rate >= cfg["multiplier"] * base_bps and rate >= cfg["min_rate_mbps"] * 1e6:
                hits.append((bucket, b, secs, rate, base_bps, basis, conf))
        if not hits:
            continue
        peak = max(hits, key=lambda h: h[3] / h[4])
        baseline_used = {"basis": peak[5], "rate_bps": round(peak[4], 1), "history_hours": host_hours.get(key, 0),
                         "source": "hourly rollup, previous 4 weeks"}
        b_sum = sum(h[1] for h in hits)
        first, last, latest = _span(ctx, hits[0][0], hits[-1][0])
        ratio = peak[3] / peak[4]
        buckets = {h[0]: round(h[3] / h[4], 1) for h in hits}
        cands.append(Candidate(
            kind="volume_spike", exporter=exp, subject=(src,), severity=cfg["severity"], confidence=peak[6],
            title="Volume above baseline",
            description=(f"{src} sent at {_fmt_bps(peak[3])}, {ratio:.1f}× its usual {_fmt_bps(peak[4])} "
                         f"({'same hour of the week' if peak[5] == 'hour_of_week_median' else '7-day p95'}) "
                         f"in {len(hits)} five-minute period{'s' if len(hits) != 1 else ''} "
                         f"(threshold {cfg['multiplier']:g}× and {cfg['min_rate_mbps']:g} Mbps)"),
            first_seen=first, last_seen=last, latest=latest, occurrences=len(hits),
            method=DETECTOR_BY_KIND["volume_spike"].method,
            threshold={"multiplier": cfg["multiplier"], "min_rate_mbps": cfg["min_rate_mbps"],
                       "min_baseline_hours": cfg["min_baseline_hours"]},
            observed={"peak_rate_bps": round(peak[3], 1), "ratio": round(ratio, 2), "bytes": b_sum},
            baseline=baseline_used, evidence_filter={"exporter": exp, "src": src},
            segments=_segments_from_buckets(buckets, ctx, buckets.get),
            src=src, host=src, metric=round(ratio, 2), metric_label="× baseline", bytes=b_sum,
        ))
    cands.sort(key=lambda c: c.observed["ratio"], reverse=True)
    out.match_count = len(cands)
    out.truncated = out.truncated or len(cands) > ctx.limit
    out.candidates = cands[: ctx.limit]


async def detect_threat_intel(ctx: RunContext, out: Outcome) -> None:
    from app.services import netflow_ti

    await netflow_ti.detect(ctx, out)


DETECTOR_FUNCS: dict[str, Callable[[RunContext, Outcome], Awaitable[None]]] = {
    "syn_scan": detect_syn_scan,
    "port_scan": detect_port_scan,
    "rst_flood": detect_rst_flood,
    "sensitive_egress": detect_sensitive_egress,
    "icmp_flood": detect_icmp_flood,
    "icmp_flood_target": detect_icmp_flood_target,
    "icmp_sweep": detect_icmp_sweep,
    "dominant_sender": detect_dominant_sender,
    "volume_spike": detect_volume_spike,
    "threat_intel": detect_threat_intel,
}


# ── Running the detectors ────────────────────────────────────────────────────

@dataclass
class RunResult:
    start: datetime
    end: datetime
    outcomes: list[Outcome]
    coverage: Coverage
    ran_at: datetime
    coverage_error: str | None = None

    @property
    def candidates(self) -> list[Candidate]:
        return [c for o in self.outcomes for c in o.candidates]

    def detectors(self) -> list[dict]:
        return [o.as_dict(self.ran_at) for o in self.outcomes]

    @property
    def complete(self) -> bool:
        """Every enabled detector ran on data (the only state that may be
        described as "no candidates")."""
        enabled = [o for o in self.outcomes if o.status != "disabled"]
        return bool(enabled) and all(o.status == "ok" for o in enabled)


async def _baseline_hours(ctx: RunContext) -> float | None:
    common, *_ = _nf()
    try:
        retention = await common.get_retention()
    except Exception:
        return None
    earliest = retention.earliest.get(common.CONVERSATIONS_1H)
    if earliest is None:
        return 0.0 if retention.known else None
    return max(0.0, (ctx.start - _aware(earliest)).total_seconds() / 3600)


async def run_detectors(ctx: RunContext, kinds: Iterable[str] | None = None) -> RunResult:
    """Coverage first, then every enabled detector concurrently; a detector
    that fails is reported ``failed`` with an error id, never as a clean run."""
    common, *_ = _nf()
    ran_at = ctx.now or common.now_utc()
    wanted = [k for k in (kinds or KINDS) if k in DETECTOR_BY_KIND]
    outcomes = {k: Outcome(kind=k, status="ok") for k in wanted}
    coverage_error = None
    try:
        ctx.coverage = await load_coverage(ctx)
    except Exception as exc:  # noqa: BLE001 - reported per detector
        coverage_error = getattr(exc, "error_id", None) or common.new_error_id()
        if not getattr(exc, "error_id", None):
            logger.exception("netflow detection: coverage query failed [%s]", coverage_error)
        ctx.coverage = Coverage(known=False)
    ctx.baseline_hours = await _baseline_hours(ctx)

    runnable: list[str] = []
    for kind in wanted:
        out = outcomes[kind]
        if not ctx.settings["detectors"][kind]["enabled"]:
            out.status, out.reason = "disabled", "Turned off in detection settings"
        elif coverage_error:
            out.status, out.reason, out.error_id = "failed", "Field coverage could not be measured", coverage_error
        elif ctx.coverage.records == 0:
            out.status = "no_coverage"
            out.reason = "No flow records in this window from the exporters in scope"
        else:
            runnable.append(kind)

    async def run_one(kind: str) -> None:
        out = outcomes[kind]
        started = time.monotonic()
        try:
            await DETECTOR_FUNCS[kind](ctx, out)
        except Exception as exc:  # noqa: BLE001 - the detector is reported failed
            error_id = getattr(exc, "error_id", None)
            if not error_id:
                error_id = common.new_error_id()
                logger.exception("netflow detection: detector %s failed [%s]", kind, error_id)
            out.status, out.reason, out.error_id = "failed", "The detector query failed", error_id
            out.candidates = []
            out.match_count = 0
        finally:
            out.query_ms = int((time.monotonic() - started) * 1000)

    await asyncio.gather(*(run_one(k) for k in runnable))
    return RunResult(ctx.start, ctx.end, [outcomes[k] for k in wanted], ctx.coverage, ran_at, coverage_error)


# ── Exceptions ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExceptionRule:
    id: str
    kind: str | None
    src: Any              # ip_network | None
    dst: Any
    dst_port: int | None
    reason: str
    expires_at: datetime | None
    # Canonical exporter addresses the exception applies to; None = every
    # exporter (migrate-140). A tag-scoped manager's exception is bound to
    # the exporters it could see, so it never suppresses another team's.
    exporters: frozenset[str] | None = None

    def matches(self, c: Candidate, now: datetime) -> bool:
        if self.expires_at is not None and self.expires_at <= now:
            return False
        if self.kind and self.kind != c.kind:
            return False
        if self.exporters is not None and _canonical_exporter(c.exporter) not in self.exporters:
            return False
        if self.src is not None and not _addr_in(c.src or c.host, self.src):
            return False
        if self.dst is not None and not _addr_in(c.dst, self.dst):
            return False
        if self.dst_port is not None:
            ports = c.observed.get("ports") or c.observed.get("sample_ports") or []
            if self.dst_port not in ports:
                return False
        return True

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "reason": self.reason,
                "expires_at": _iso(self.expires_at),
                "src_cidr": str(self.src) if self.src is not None else None,
                "dst_cidr": str(self.dst) if self.dst is not None else None,
                "dst_port": self.dst_port}


def _canonical_exporter(value: Any) -> str | None:
    """An exporter address in the access module's canonical spelling."""
    _common, _sql, _filters, access = _nf()
    return access.canonical_ip(value)


def exception_exporters(values: Any) -> frozenset[str] | None:
    """A stored ``exporter_ips`` array as canonical addresses (None = every
    exporter). Unparsable elements are dropped; a scope left empty by that
    matches nothing (it never turns into "every exporter")."""
    if values is None:
        return None
    out = set()
    for v in values:
        ip = _canonical_exporter(v)
        if ip is not None:
            out.add(ip)
    return frozenset(out)


def _addr_in(ip: str | None, net: Any) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.version == net.version and addr in net


async def load_exceptions(db: AsyncSession, *, include_expired: bool = False) -> list[ExceptionRule]:
    rows = (await db.execute(text(
        "SELECT id, kind, src_cidr::text, dst_cidr::text, dst_port, reason, expires_at, exporter_ips::text[] "
        "FROM netflow_detector_exceptions ORDER BY created_at"
    ))).all()
    now = datetime.now(timezone.utc)
    out: list[ExceptionRule] = []
    for r in rows:
        exp = _aware(r[6]) if r[6] else None
        if exp is not None and exp <= now and not include_expired:
            continue
        out.append(ExceptionRule(
            id=str(r[0]), kind=r[1],
            src=ipaddress.ip_network(r[2], strict=False) if r[2] else None,
            dst=ipaddress.ip_network(r[3], strict=False) if r[3] else None,
            dst_port=r[4], reason=r[5], expires_at=exp, exporters=exception_exporters(r[7]),
        ))
    return out


def apply_exceptions(candidates: list[Candidate], rules: list[ExceptionRule], now: datetime) -> None:
    for c in candidates:
        for rule in rules:
            if rule.matches(c, now):
                c.exception = rule.as_dict()
                break


# ── Serialisation ────────────────────────────────────────────────────────────

def candidate_item(c: Candidate, persisted: dict | None = None) -> dict:
    """An ad-hoc match in the finding item shape (§11), not persisted."""
    spec = DETECTOR_BY_KIND[c.kind]
    status = "suppressed" if c.exception else "candidate"
    item = {
        "id": c.fingerprint,
        "fingerprint": c.fingerprint,
        "persisted_id": persisted.get("id") if persisted else None,
        "persisted_status": persisted.get("status") if persisted else None,
        "kind": c.kind, "label": spec.label, "category": spec.category,
        "severity": c.severity, "confidence": c.confidence, "status": status,
        "title": c.title, "description": c.description,
        "src": c.src, "dst": c.dst, "exporter": c.exporter, "host": c.host,
        "first_seen": _iso(c.first_seen), "last_seen": _iso(c.last_seen), "occurrences": c.occurrences,
        "evidence": c.evidence(), "metrics": c.metrics(),
        "method": c.method, "threshold": c.threshold, "observed": c.observed, "baseline": c.baseline,
        "suppressed": bool(c.exception),
        "suppression": ({"reason": f"Exception: {c.exception['reason']}", "until": c.exception.get("expires_at"),
                         "by": None, "exception_id": c.exception["id"]} if c.exception else None),
        "assigned_to": None, "acknowledged_by": None, "acknowledged_at": None, "resolved_at": None,
        "notes_count": 0, "persisted": False,
        # Legacy /anomalies fields
        "metric": c.metric, "metric_label": c.metric_label, "flows": c.flows, "bytes": c.bytes,
    }
    return item


def finding_item(row: dict, users: dict[str, str] | None = None) -> dict:
    """A persisted ``netflow_findings`` row in the finding item shape (§11)."""
    users = users or {}
    kind = row.get("kind") or ""
    spec = DETECTOR_BY_KIND.get(kind)
    metrics = row.get("metrics") or {}
    evidence = row.get("evidence") or {}
    notes = row.get("notes") or []

    def user(uid):
        if not uid:
            return None
        return {"id": str(uid), "username": users.get(str(uid))}

    status = row.get("status") or "open"
    return {
        "id": str(row["id"]),
        "fingerprint": row.get("fingerprint"),
        "kind": kind, "label": spec.label if spec else kind, "category": spec.category if spec else metrics.get("category"),
        "severity": row.get("severity"), "confidence": row.get("confidence"), "status": status,
        "title": row.get("title"), "description": row.get("description"),
        "src": _inet(row.get("src_ip")), "dst": _inet(row.get("dst_ip")),
        "exporter": _inet(row.get("exporter_ip")), "host": evidence.get("host"),
        "first_seen": _iso(row.get("first_seen")), "last_seen": _iso(row.get("last_seen")),
        "occurrences": row.get("occurrences"),
        "evidence": evidence, "metrics": metrics,
        "method": metrics.get("method"), "threshold": metrics.get("threshold"),
        "observed": metrics.get("observed"), "baseline": metrics.get("baseline"),
        "suppressed": status == "suppressed",
        "suppression": ({"reason": row.get("suppress_reason"), "until": _iso(row.get("suppressed_until")),
                         "by": user(row.get("suppressed_by")), "exception_id": metrics.get("exception_id")}
                        if status == "suppressed" else None),
        "assigned_to": user(row.get("assigned_to")),
        "acknowledged_by": user(row.get("acknowledged_by")),
        "acknowledged_at": _iso(row.get("acknowledged_at")),
        "resolved_at": _iso(row.get("resolved_at")),
        "alert_id": str(row["alert_id"]) if row.get("alert_id") else None,
        "notes_count": len(notes),
        "persisted": True,
        "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at")),
        "metric": metrics.get("metric"), "metric_label": metrics.get("metric_label"),
        "flows": metrics.get("flows"), "bytes": metrics.get("bytes"),
    }


def _inet(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s.split("/")[0] if s.endswith("/32") or s.endswith("/128") else s


# ── Persistence ──────────────────────────────────────────────────────────────

FINDING_COLUMNS = (
    "id, fingerprint, kind, severity, confidence, status, title, description, src_ip, dst_ip, exporter_ip, "
    "first_seen, last_seen, occurrences, evidence, metrics, alert_id, assigned_to, acknowledged_by, "
    "acknowledged_at, suppressed_until, suppressed_by, suppress_reason, resolved_at, notes, created_at, updated_at"
)


def _jsonb(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def row_dict(row: Any) -> dict:
    d = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    for k in ("evidence", "metrics", "notes"):
        if k in d:
            d[k] = _jsonb(d[k])
    return d


def _note(text_: str, *, by: str | None = None, by_name: str | None = None, kind: str = "system",
          at: datetime | None = None) -> dict:
    return {"id": uuid.uuid4().hex[:12], "at": _iso(at or datetime.now(timezone.utc)), "by": by,
            "by_name": by_name, "kind": kind, "text": text_}


def _merge_segments(old: list[dict], new: list[dict]) -> list[dict]:
    segs = {(s.get("from"), s.get("to")): s for s in (old or [])}
    for s in new:
        segs[(s.get("from"), s.get("to"))] = s
    ordered = sorted(segs.values(), key=lambda s: s.get("from") or "")
    merged: list[dict] = []
    for s in ordered:
        if merged and (merged[-1].get("to") or "") >= (s.get("from") or ""):
            last = merged[-1]
            last["to"] = max(last.get("to") or "", s.get("to") or "")
            if s.get("peak") is not None:
                last["peak"] = max(last.get("peak") or 0, s["peak"])
        else:
            merged.append(dict(s))
    return merged[-MAX_SEGMENTS:]


def _max_sev(a: str | None, b: str | None) -> str:
    return max((a or "info", b or "info"), key=lambda s: SEVERITY_RANK.get(s, 0))


async def upsert_findings(db: AsyncSession, candidates: list[Candidate], now: datetime) -> dict:
    """Insert new findings, update recurring ones (last seen, occurrences,
    evidence, metrics), reopen resolved ones and keep acknowledged/suppressed
    ones in their state. Returns counts per action."""
    stats = {"inserted": 0, "updated": 0, "reopened": 0, "suppressed": 0}
    if not candidates:
        return stats
    fps = [c.fingerprint for c in candidates]
    rows = (await db.execute(
        text(f"SELECT {FINDING_COLUMNS} FROM netflow_findings WHERE fingerprint = ANY(:fps) FOR UPDATE"),
        {"fps": fps},
    )).all()
    existing = {r._mapping["fingerprint"]: row_dict(r) for r in rows}
    for c in candidates:
        ev = c.evidence()
        metrics = c.metrics()
        if c.exception:
            metrics["exception_id"] = c.exception["id"]
        row = existing.get(c.fingerprint)
        if row is None:
            status = "suppressed" if c.exception else "open"
            notes = [_note(f"Suppressed by exception: {c.exception['reason']}", at=now)] if c.exception else []
            await db.execute(text("""
                INSERT INTO netflow_findings (fingerprint, kind, severity, confidence, status, title, description,
                    src_ip, dst_ip, exporter_ip, first_seen, last_seen, occurrences, evidence, metrics,
                    suppressed_until, suppress_reason, notes)
                VALUES (:fp, :kind, :sev, :conf, :status, :title, :descr, CAST(:src AS inet), CAST(:dst AS inet),
                    CAST(:exp AS inet), :first, :last, :occ, CAST(:ev AS jsonb), CAST(:metrics AS jsonb),
                    :sup_until, :sup_reason, CAST(:notes AS jsonb))
                ON CONFLICT (fingerprint) DO NOTHING
            """), {
                "fp": c.fingerprint, "kind": c.kind, "sev": c.severity, "conf": c.confidence, "status": status,
                "title": c.title, "descr": c.description, "src": c.src, "dst": c.dst, "exp": c.exporter,
                "first": c.first_seen, "last": c.last_seen, "occ": c.occurrences, "ev": json.dumps(ev),
                "metrics": json.dumps(metrics),
                "sup_until": _parse(c.exception["expires_at"]) if c.exception and c.exception.get("expires_at") else None,
                "sup_reason": f"Exception: {c.exception['reason']}" if c.exception else None,
                "notes": json.dumps(notes),
            })
            stats["inserted"] += 1
            if c.exception:
                stats["suppressed"] += 1
            continue
        status = row["status"]
        notes = list(row.get("notes") or [])
        sup_until = row.get("suppressed_until")
        sup_reason = row.get("suppress_reason")
        suppressed_by = row.get("suppressed_by")
        resolved_at = row.get("resolved_at")
        ack_by, ack_at = row.get("acknowledged_by"), row.get("acknowledged_at")
        severity = _max_sev(row.get("severity"), c.severity)
        first_seen = min(_aware(row["first_seen"]), c.first_seen)
        if status == "resolved":
            status, resolved_at, ack_by, ack_at = "open", None, None, None
            severity = c.severity
            # first_seen keeps covering every occurrence the finding counts
            # (occurrences and segments carry the history across reopenings),
            # so the card, the timeline and Investigate -- whose evidence
            # window starts at first_seen -- describe the same set; "Latest
            # occurrence only" stays the separate narrow link.
            notes.append(_note(
                f"Reopened: the detector matched again (last active {_iso(_aware(row['last_seen']))})", at=now))
            stats["reopened"] += 1
        elif status == "suppressed" and sup_until is not None and _aware(sup_until) <= now:
            status, sup_until, sup_reason, suppressed_by = "open", None, None, None
            notes.append(_note("Suppression expired and the detector matched again", at=now))
        if c.exception and status == "open":
            status = "suppressed"
            sup_until = _parse(c.exception["expires_at"]) if c.exception.get("expires_at") else None
            sup_reason, suppressed_by = f"Exception: {c.exception['reason']}", None
            notes.append(_note(f"Suppressed by exception: {c.exception['reason']}", at=now))
            stats["suppressed"] += 1
        old_ev = row.get("evidence") or {}
        ev["segments"] = _merge_segments(old_ev.get("segments") or [], ev.get("segments") or [])
        start = max(first_seen, max(_aware(row["last_seen"]), c.last_seen) - MAX_EVIDENCE_SPAN)
        last_seen = max(_aware(row["last_seen"]), c.last_seen)
        ev["window"] = {"from": _iso(start), "to": _iso(last_seen)}
        ev["truncated_span"] = start > first_seen
        # Keep peaks across occurrences.
        old_obs = (row.get("metrics") or {}).get("observed") or {}
        for key, value in list((metrics.get("observed") or {}).items()):
            if key.startswith("peak") and isinstance(value, (int, float)) and isinstance(old_obs.get(key), (int, float)):
                metrics["observed"][key] = max(value, old_obs[key])
        await db.execute(text("""
            UPDATE netflow_findings SET severity = :sev, confidence = :conf, status = :status, title = :title,
                description = :descr, first_seen = :first, last_seen = :last,
                src_ip = CAST(:src AS inet), dst_ip = CAST(:dst AS inet),
                occurrences = occurrences + :occ, evidence = CAST(:ev AS jsonb), metrics = CAST(:metrics AS jsonb),
                suppressed_until = :sup_until, suppress_reason = :sup_reason, suppressed_by = :sup_by,
                resolved_at = :resolved_at, acknowledged_by = :ack_by, acknowledged_at = :ack_at,
                notes = CAST(:notes AS jsonb), updated_at = NOW()
            WHERE id = :id
        """), {
            "sev": severity, "conf": c.confidence, "status": status, "title": c.title, "descr": c.description,
            "src": c.src, "dst": c.dst,
            "first": first_seen, "last": last_seen, "occ": c.occurrences, "ev": json.dumps(ev),
            "metrics": json.dumps(metrics), "sup_until": sup_until, "sup_reason": sup_reason,
            "sup_by": suppressed_by, "resolved_at": resolved_at, "ack_by": ack_by, "ack_at": ack_at,
            "notes": json.dumps(notes[-200:]), "id": row["id"],
        })
        stats["updated"] += 1
    return stats


async def auto_resolve(db: AsyncSession, horizon: datetime, quiet_minutes: int, now: datetime) -> int:
    """Resolve findings with no occurrence for ``quiet_minutes`` before the
    engine's evaluated horizon; expired suppressions fall back to open."""
    cutoff = horizon - timedelta(minutes=quiet_minutes)
    note = json.dumps([_note(f"Resolved automatically: no new activity for {quiet_minutes} minutes", at=now)])
    res = await db.execute(text("""
        UPDATE netflow_findings
           SET status = 'resolved', resolved_at = :now, updated_at = NOW(),
               suppressed_until = NULL, notes = notes || CAST(:note AS jsonb)
         WHERE last_seen < :cutoff
           AND (status IN ('open', 'acknowledged')
                OR (status = 'suppressed' AND suppressed_until IS NOT NULL AND suppressed_until <= :now))
    """), {"now": now, "cutoff": cutoff, "note": note})
    reopened = json.dumps([_note("Suppression expired", at=now)])
    await db.execute(text("""
        UPDATE netflow_findings
           SET status = 'open', suppressed_until = NULL, suppressed_by = NULL, suppress_reason = NULL,
               updated_at = NOW(), notes = notes || CAST(:note AS jsonb)
         WHERE status = 'suppressed' AND suppressed_until IS NOT NULL AND suppressed_until <= :now
    """), {"now": now, "note": reopened})
    return int(res.rowcount or 0)


async def suppress_matching(db: AsyncSession, rule: ExceptionRule, now: datetime) -> int:
    """Apply a new exception to active findings it matches (shown as suppressed);
    an exporter-scoped exception only touches findings from its exporters."""
    if rule.exporters is not None and not rule.exporters:
        return 0
    where, params = ["status IN ('open', 'acknowledged')"], {}
    if rule.kind:
        where.append("kind = :kind")
        params["kind"] = rule.kind
    if rule.exporters is not None:
        where.append("exporter_ip = ANY(CAST(:x_exp AS inet[]))")
        params["x_exp"] = sorted(rule.exporters)
    rows = (await db.execute(text(
        f"SELECT {FINDING_COLUMNS} FROM netflow_findings WHERE {' AND '.join(where)}"
    ), params)).all()
    n = 0
    for r in rows:
        d = row_dict(r)
        metrics = d.get("metrics") or {}
        c = Candidate(
            kind=d["kind"], exporter=_inet(d.get("exporter_ip")) or "", subject=(), severity=d.get("severity") or "info",
            confidence=d.get("confidence") or "low", title="", description="", first_seen=now, last_seen=now,
            latest=(now, now), occurrences=0, method="", threshold={}, observed=metrics.get("observed") or {},
            evidence_filter={}, src=_inet(d.get("src_ip")), dst=_inet(d.get("dst_ip")),
            host=(d.get("evidence") or {}).get("host"),
        )
        if not rule.matches(c, now):
            continue
        metrics["exception_id"] = rule.id
        notes = list(d.get("notes") or []) + [_note(f"Suppressed by exception: {rule.reason}", at=now)]
        await db.execute(text("""
            UPDATE netflow_findings SET status = 'suppressed', suppressed_until = :until, suppressed_by = NULL,
                suppress_reason = :reason, metrics = CAST(:m AS jsonb), notes = CAST(:n AS jsonb), updated_at = NOW()
            WHERE id = :id
        """), {"until": rule.expires_at, "reason": f"Exception: {rule.reason}", "m": json.dumps(metrics),
               "n": json.dumps(notes[-200:]), "id": d["id"]})
        n += 1
    return n


# ── Engine state ─────────────────────────────────────────────────────────────

async def load_state(db: AsyncSession) -> dict:
    try:
        row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"), {"k": STATE_KEY})).first()
    except Exception:
        logger.warning("netflow detection: state unreadable", exc_info=True)
        return {}
    value = _jsonb(row[0]) if row else {}
    return value if isinstance(value, dict) else {}


async def save_state(db: AsyncSession, state: dict) -> None:
    await db.execute(
        text("INSERT INTO system_settings (key, value, updated_at) VALUES (:k, CAST(:v AS jsonb), NOW()) "
             "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"),
        {"k": STATE_KEY, "v": json.dumps(state, default=str)},
    )


def add_evaluated(intervals: list[list[str]], start: datetime, end: datetime, now: datetime) -> list[list[str]]:
    """Merge [start, end) into the evaluated-interval list (ISO pairs), dropping
    what is older than EVALUATED_KEEP."""
    items = [(_parse(a), _parse(b)) for a, b in intervals or []]
    items.append((start, end))
    items.sort()
    merged: list[tuple[datetime, datetime]] = []
    for a, b in items:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    floor = now - EVALUATED_KEEP
    merged = [(max(a, floor), b) for a, b in merged if b > floor]
    return [[_iso(a), _iso(b)] for a, b in merged[-MAX_COVERAGE_INTERVALS:]]


def evaluated_seconds(intervals: list[list[str]] | None, start: datetime, end: datetime) -> int:
    total = 0
    for a, b in intervals or []:
        s, e = max(_parse(a), start), min(_parse(b), end)
        if e > s:
            total += int((e - s).total_seconds())
    return total


# ── Engine ───────────────────────────────────────────────────────────────────

def _closed_end(now: datetime) -> datetime:
    _common, sql, *_ = _nf()
    return sql.floor_to(now - timedelta(seconds=CLOSE_GRACE_S), BUCKET_S)


async def evaluate_bucket(db: AsyncSession, start: datetime, settings: dict, rules: list[ExceptionRule],
                          now: datetime) -> tuple[RunResult, dict]:
    """Run every detector on one closed bucket and persist its matches."""
    _common, _sql, filters, access = _nf()
    ctx = RunContext(
        start=start, end=start + timedelta(seconds=BUCKET_S), vis=access.UNRESTRICTED_SYSTEM,
        base=filters.FlowFilter(), settings=settings, budget_s=_common.BUDGET_BACKGROUND, limit=ENGINE_LIMIT,
        persist=True, context_start=start - timedelta(minutes=10), now=now,
    )
    result = await run_detectors(ctx)
    cands = result.candidates
    apply_exceptions(cands, rules, now)
    stats = await upsert_findings(db, cands, now)
    return result, stats


def _detector_state(result: RunResult, previous: dict) -> dict:
    out: dict = {}
    for o in result.outcomes:
        d = o.as_dict(result.ran_at)
        prev = (previous or {}).get(o.kind) or {}
        d["last_ok_at"] = _iso(result.ran_at) if o.status == "ok" else prev.get("last_ok_at")
        d["window"] = {"from": _iso(result.start), "to": _iso(result.end)}
        out[o.kind] = d
    return out


async def run_engine_once(now: datetime | None = None, *, session_factory=None) -> dict:
    """Evaluate every closed bucket not evaluated yet (at most MAX_CATCHUP),
    persist findings, auto-resolve quiet ones. Returns a summary; ``{"skipped":
    "locked"}`` when another worker holds the engine lock."""
    from app.core.database import AsyncSessionLocal, engine as pg_engine

    common, *_ = _nf()
    factory = session_factory or AsyncSessionLocal
    now = now or common.now_utc()
    summary: dict = {"buckets": 0, "inserted": 0, "updated": 0, "reopened": 0, "resolved": 0}
    async with pg_engine.connect() as lock_conn:
        got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK})).scalar()
        await lock_conn.commit()
        if not got:
            return {"skipped": "locked"}
        try:
            async with factory() as db:
                settings = await load_settings(db)
                state = await load_state(db)
                closed_end = _closed_end(now)
                last = state.get("last_bucket_end")
                last_dt = _parse(last) if last else closed_end - timedelta(seconds=BUCKET_S)
                if last_dt > closed_end:
                    last_dt = closed_end  # clock moved backwards: nothing to evaluate
                if closed_end - last_dt > MAX_CATCHUP:
                    state.setdefault("gaps", []).append([_iso(last_dt), _iso(closed_end - MAX_CATCHUP)])
                    state["gaps"] = state["gaps"][-50:]
                    last_dt = closed_end - MAX_CATCHUP
                rules = await load_exceptions(db)
                bucket = last_dt
                while bucket < closed_end:
                    try:
                        result, stats = await evaluate_bucket(db, bucket, settings, rules, now)
                    except Exception:
                        await db.rollback()
                        logger.exception("netflow detection: bucket %s failed", _iso(bucket))
                        state["last_error"] = {"at": _iso(now), "bucket": _iso(bucket)}
                        await save_state(db, state)
                        await db.commit()
                        break
                    for k in ("inserted", "updated", "reopened"):
                        summary[k] += stats.get(k, 0)
                    summary["buckets"] += 1
                    bucket_end = bucket + timedelta(seconds=BUCKET_S)
                    state["last_bucket_end"] = _iso(bucket_end)
                    state["last_run_at"] = _iso(common.now_utc())
                    state["detectors"] = _detector_state(result, state.get("detectors") or {})
                    state["coverage"] = result.coverage.as_dict()
                    state["exporters"] = sorted(result.coverage.exporters)
                    # Per exporter, so a restricted caller's coverage and
                    # notes can be recomputed over its own exporters only.
                    state["coverage_exporters"] = result.coverage.exporter_rows()
                    state["complete"] = result.complete
                    if result.complete or any(o.status == "ok" for o in result.outcomes):
                        state["evaluated"] = add_evaluated(state.get("evaluated") or [], bucket, bucket_end, now)
                    state["runs"] = int(state.get("runs") or 0) + 1
                    state.pop("last_error", None)
                    await save_state(db, state)
                    await db.commit()
                    bucket = bucket_end
                horizon = _parse(state["last_bucket_end"]) if state.get("last_bucket_end") else None
                if horizon is not None:
                    summary["resolved"] = await auto_resolve(db, horizon, settings["quiet_minutes"], now)
                    state["last_checked_at"] = _iso(common.now_utc())
                    await save_state(db, state)
                    await db.commit()
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK})
            await lock_conn.commit()
    if summary["buckets"] or summary["resolved"]:
        logger.info("netflow detection: %s", summary)
    return summary


async def netflow_detection_loop() -> None:
    """Background loop (contract §9.4): evaluate each bucket 2 minutes after it closes."""
    await asyncio.sleep(STARTUP_DELAY_S)
    while True:
        try:
            await run_engine_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("netflow detection: engine run failed")
        now = time.time()
        next_close = (now - CLOSE_GRACE_S) // BUCKET_S * BUCKET_S + BUCKET_S + CLOSE_GRACE_S
        await asyncio.sleep(max(15.0, next_close - now + 2))


def scoped_coverage(state: dict, visible: frozenset[str] | None) -> Coverage | None:
    """The engine's last field coverage over ``visible`` exporters only.
    None when the stored state predates per-exporter coverage (a restricted
    caller then gets no coverage figures rather than fleet-wide ones)."""
    rows = state.get("coverage_exporters")
    if not isinstance(rows, list):
        return None
    return Coverage.from_rows(rows, visible)


def engine_status(state: dict, now: datetime, visible: frozenset[str] | None = None) -> dict:
    """Engine status. With ``visible`` (a restricted caller, or a page narrowed
    to some exporters) the coverage block counts only those exporters, so no
    other exporter's address, sampling or record count is disclosed."""
    last_run = state.get("last_run_at")
    if not last_run:
        status = "never"
        age = None
    else:
        age = int((now - _parse(last_run)).total_seconds())
        status = "stale" if age > ENGINE_STALE_S else "running"
    return {
        "status": status,
        "last_run_at": last_run,
        "last_bucket_end": state.get("last_bucket_end"),
        "age_s": age,
        "complete": state.get("complete"),
        "coverage": _scoped_coverage_dict(state, visible),
        "last_error": state.get("last_error"),
        "bucket_s": BUCKET_S,
        "grace_s": CLOSE_GRACE_S,
    }


def _scoped_coverage_dict(state: dict, visible: frozenset[str] | None) -> dict | None:
    if visible is None:
        return state.get("coverage")
    cov = scoped_coverage(state, visible)
    if cov is None:
        return None
    return {**cov.as_dict(), "known": bool((state.get("coverage") or {}).get("known", True))}


_TRUNCATED_NOTE = "The last evaluation was capped; not every candidate was evaluated"


def scoped_detectors(state: dict, settings: dict, visible: frozenset[str] | None, now: datetime) -> list[dict]:
    """Per-detector status from the engine's last run, narrowed to the
    exporters the caller may see (and the page's exporter filter).

    Narrowed status never carries fleet-wide values: coverage, notes, the
    exporters without a field and reduced-sensitivity entries are recomputed
    with ``annotate_coverage`` over the visible exporters' share of the last
    run's coverage (notes about the whole run, such as candidate caps or
    feed-wide address counts, are replaced by a generic cap note)."""
    eng = engine_status(state, now)
    stored = state.get("detectors") or {}
    cov = scoped_coverage(state, visible) if visible is not None else None
    out = []
    for spec in DETECTORS:
        d = dict(stored.get(spec.kind) or {})
        base = {
            "kind": spec.kind, "label": spec.label, "category": spec.category,
            "required_fields": list(spec.required),
            "enabled": settings["detectors"][spec.kind]["enabled"],
            "last_run_at": d.get("last_run_at"), "last_ok_at": d.get("last_ok_at"),
            "coverage_pct": d.get("coverage_pct"), "notes": d.get("notes") or [],
            "reduced_sensitivity": d.get("reduced_sensitivity") or [],
            "exporters_unavailable": d.get("exporters_unavailable") or [],
        }
        if visible is not None:
            base.update(_scoped_annotations(spec.kind, d, cov, visible))
        if not base["enabled"]:
            out.append({**base, "status": "disabled", "reason": "Turned off in detection settings"})
            continue
        if eng["status"] == "never" or not d:
            out.append({**base, "status": "no_coverage", "reason": "The detection engine has not evaluated any data yet"})
            continue
        status, reason = d.get("status") or "failed", d.get("reason")
        if eng["status"] == "stale":
            status = "failed"
            reason = f"The detection engine has not run since {eng['last_run_at']}"
        elif visible is not None:
            if status in ("ok", "unavailable"):
                if not set(state.get("exporters") or []) & visible:
                    status = "no_coverage"
                    reason = "No flow records from the exporters in scope in the last evaluated period"
                elif not _field_based(spec):
                    pass
                elif cov is not None:
                    probe = Outcome(kind=spec.kind, status="ok")
                    annotate_coverage(spec.kind, cov, probe)
                    if probe.status == "unavailable":
                        status, reason = "unavailable", probe.reason
                    elif status == "unavailable":
                        status, reason = "ok", None
                else:
                    seen = set(d.get("exporters_seen") or []) & visible
                    unavailable = set(base["exporters_unavailable"])
                    if seen and seen <= unavailable:
                        status = "unavailable"
                        reason = f"Not exported by {_list(sorted(unavailable))}"
                    elif status == "unavailable" and seen - unavailable:
                        status, reason = "ok", None
                    elif status == "unavailable":
                        reason = "The required fields are not exported by the exporters in scope"
        out.append({**base, "status": status, "reason": reason})
    return out


def _field_based(spec: DetectorSpec) -> bool:
    """Detectors whose availability depends on exported fields per exporter."""
    return spec.kind in _FLAG_KINDS or spec.kind == "icmp_sweep"


def _scoped_annotations(kind: str, stored: dict, cov: Coverage | None, visible: frozenset[str]) -> dict:
    """coverage_pct / notes / exporters_unavailable / reduced_sensitivity of
    one detector over the visible exporters only."""
    if cov is None:
        # State from before per-exporter coverage was kept: drop what cannot
        # be narrowed instead of showing the fleet's.
        return {
            "coverage_pct": None, "notes": [],
            "exporters_unavailable": sorted(set(stored.get("exporters_unavailable") or []) & visible),
            "reduced_sensitivity": [r for r in (stored.get("reduced_sensitivity") or [])
                                    if isinstance(r, dict) and r.get("exporter") in visible],
        }
    probe = Outcome(kind=kind, status="ok")
    if cov.exporters:
        annotate_coverage(kind, cov, probe)
    notes = list(probe.notes)
    if stored.get("truncated"):
        notes.append(_TRUNCATED_NOTE)
    return {
        "coverage_pct": probe.coverage_pct, "notes": notes,
        "exporters_unavailable": list(probe.exporters_unavailable),
        "reduced_sensitivity": list(probe.reduced_sensitivity),
    }


# ── Ad-hoc runs ──────────────────────────────────────────────────────────────

_adhoc_cache: dict[tuple, tuple[float, Any]] = {}


def _cache_key(start: datetime, end: datetime, base: Any, vis: Any, kinds: tuple[str, ...], limit: int) -> tuple:
    canonical = tuple(sorted(base.canonical().items())) if base is not None else ()
    exporters = tuple(sorted(vis.exporters)) if vis.exporters is not None else None
    return (int(start.timestamp()) // 60, int(end.timestamp()) // 60, canonical, exporters, kinds, limit)


async def run_adhoc(db: AsyncSession, start: datetime, end: datetime, vis: Any, base: Any, *,
                    kinds: Iterable[str] | None = None, limit: int = ADHOC_LIMIT, use_cache: bool = True) -> RunResult:
    """Evaluate the detectors over an arbitrary window (≤ 31 days of raw),
    without persisting. Cached for a minute per (window minute, scope)."""
    common, *_ = _nf()
    kinds_t = tuple(k for k in (kinds or KINDS) if k in DETECTOR_BY_KIND)
    key = _cache_key(start, end, base, vis, kinds_t, limit)
    hit = _adhoc_cache.get(key)
    if use_cache and hit and time.monotonic() - hit[0] < ADHOC_CACHE_TTL_S:
        return hit[1]
    settings = await load_settings(db)
    ctx = RunContext(start=start, end=end, vis=vis, base=base, settings=settings,
                     budget_s=common.BUDGET_FORENSICS, limit=limit, persist=False, now=common.now_utc())
    result = await run_detectors(ctx, kinds_t)
    apply_exceptions(result.candidates, await load_exceptions(db), common.now_utc())
    if len(_adhoc_cache) > 64:
        _adhoc_cache.clear()
    _adhoc_cache[key] = (time.monotonic(), result)
    return result


__all__ = [
    "DETECTORS", "DETECTOR_BY_KIND", "KINDS", "SEVERITIES", "STATUSES", "DETECTOR_STATES",
    "Param", "DetectorSpec", "SettingsError", "default_settings", "merge_settings", "load_settings",
    "save_settings", "settings_payload", "RunContext", "Candidate", "Outcome", "RunResult", "Coverage",
    "run_detectors", "run_adhoc", "load_exceptions", "apply_exceptions", "ExceptionRule", "exception_exporters",
    "candidate_item",
    "finding_item", "upsert_findings", "auto_resolve", "suppress_matching", "run_engine_once",
    "netflow_detection_loop", "engine_status", "scoped_detectors", "load_state", "evaluated_seconds",
    "is_public_ip", "is_internal_ip", "fingerprint", "FINDING_COLUMNS", "row_dict", "annotate_coverage",
    "scoped_coverage",
]
