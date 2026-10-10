"""Inventory read model: the asset union, filters, facets and aggregates behind /api/v1/inventory.

Everything the Inventory pages show is read through this module, so the pages cannot
disagree with each other. It owns:

* the asset union of doc 02 section 3.1: one row per machine (every server, plus every
  device that is not the SNMP side of a server), built as a CTE in Python so it can change
  without a migration; the tag scope is applied INSIDE the union, which is the single
  choke point (a caller cannot forget it);
* the filter vocabulary shared by the list, the facets, the OS and hardware drill-downs
  and (next wave) the export: ``AssetFilters`` plus ``_conditions``;
* the read functions the thin route handlers call: ``list_assets``, ``asset_facets``,
  ``asset_detail``, ``summary``, ``os_tree``, ``hardware`` and ``changes``.

Rules every function here follows (they are the Inventory principles, not style):

* every number is the size of a list. A count returned here is the ``total`` of
  ``list_assets`` with the filters named in the contract, and the tests assert it;
* unknown is a value: an asset with no identity row, or an unreadable OS, is counted as
  ``unknown`` and can be drilled to; it is never dropped or folded into a healthy bucket. The same holds for
  endpoint protection (a truncated, not yet normalised or absent software list is ``unknown``, not ``missing``) and
  for freshness (an asset that cannot report by design is ``not_applicable``, not ``never``);
* groups are formed on the key the matching filter compares: names case-insensitively (``Cisco`` and ``CISCO`` are one
  vendor, labelled with the most common spelling), versions by ``versioning.equivalence_key`` (``7.2`` and ``7.2.0`` are
  one version), so the number on a row is the total of the list its link opens;
* scope fails closed. ``scope`` is the output of ``scoping.visible_tags`` (``None`` means
  unrestricted); a restricted scope only ever sees the assets whose tags it shares;
* SQL is raw text with bind parameters. Interpolated fragments are constants or come from
  an allowlist in this file, never from input;
* time is read once per call from ``utcnow()`` and bound as ``:now`` instead of calling
  ``now()`` in SQL, so a test (or a golden fixture) can pin the clock.

Query shapes that matter for the budgets of doc 02 section 13 (5,000 servers x 400 packages,
10,000 devices, 1 M events):

* the union is a few thousand rows: filters, facets and groupings are evaluated over it
  once (``MATERIALIZED`` when several statements read it);
* a ``product`` filter is a semi-join on ``idx_ssi_name_key``, never a per-server EXISTS;
* endpoint protection (``sec_state``) is three joins over small sets: the servers that have a software row of the
  category ``endpoint-protection`` (partial index), the servers with a running Defender service (partial index) and
  the servers that still have software rows the normaliser has not keyed (partial index on ``norm_rev IS NULL``);
* change events for a restricted scope are ASSET-DRIVEN: the visible asset set is built
  first (tags live on the assets, not on events) and events are fetched through
  ``idx_inv_events_asset`` with a lateral join, because the natural form walks the whole
  time index (doc 02 section 14).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.services import inventory_normalize, versioning
from app.services.inventory_service import (
    TEMPLATE_METRIC_KEYS,
    get_appliance_zone,
    get_inventory_settings,
    merge_inventory_settings,
    software_eligible_sql,
)

UTC = timezone.utc
UTC_ZONE = ZoneInfo("UTC")
logger = logging.getLogger("zenplus.inventory_query")
CAPTURE_KEY = "inventory.capture_started_at"      # system_settings key written by migration 158


class InvalidFilter(ValueError):
    """A filter value outside the vocabulary. The route turns it into a 422."""


# ── Vocabulary ───────────────────────────────────────────────────────

KINDS = ("server", "device")
# not_applicable: the asset cannot report an inventory by design (a ping-only device, a controller-managed child,
# a server whose collection mode has no inventory upload) and never has. It is listed and counted, but it is not
# "never reported", it is not in the fresh percentage and it does not stand in the way of the all-clear.
FRESHNESS = ("fresh", "aging", "stale", "never", "not_applicable")
FRESHNESS_FILTERS = FRESHNESS + ("stale_or_never",)
HEALTH_KEYS = (
    "missing_os_version", "missing_model", "missing_serial", "unknown_vendor",
    "stale", "never", "software_truncated", "missing_security_agent",
)
SECURITY_AGENT_VALUES = ("present", "missing", "unknown")
# Why a server's endpoint protection is 'unknown' (X9, X10), in the order the rules are tried:
#   no_software_list  no complete software report was ever accepted (not baselined), or the server holds unkeyed rows the
#                     normalise loop never selects (no source on a server that is not a Windows machine): no list to read
#   list_truncated    the agent cut its software list at its cap, so a product beyond it could be the missing one
#   normalising       rows the loop will still key (or re-key after a rule-pack bump) are waiting
#   no_service_list   the software list is complete and shows no endpoint-protection title, but the server has NO service
#                     rows at all (the APM-only agent profile, or services switched off): built-in Defender, which has no
#                     Uninstall entry and shows only as a running service, cannot be judged
SECURITY_UNKNOWN_REASONS = ("no_software_list", "list_truncated", "normalising", "no_service_list")
# The software category that answers "is this server protected": antivirus, endpoint-protection-platform and EDR
# engines and sensors only. The broader category 'security' (VPN clients, scanners, password managers, management
# agents, Sysmon, the ZenPlus Agent's own entry in some rule packs) never counts.
ENDPOINT_PROTECTION_CATEGORY = "endpoint-protection"
# Built-in Microsoft Defender has no Uninstall entry, so a running service is the only evidence the agent can give:
# WinDefend (Microsoft Defender Antivirus) and Sense (Microsoft Defender for Endpoint sensor).
ENDPOINT_PROTECTION_SERVICES = ("WinDefend", "Sense")
ORDERS = ("asc", "desc")
UNKNOWN = "unknown"            # the one sentinel for "the value is missing" (see _conditions)
SOURCES = ("agent", "snmp", "wmi", "winrm", "ssh", "controller", "icmp")

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 50
MAX_PAGE = 100_000
MAX_TEXT = 200

CHANGE_CATEGORIES = ("software", "os", "firmware", "hardware")
CHANGE_ACTIONS = ("added", "removed", "changed")
CHANGE_LABELS = ("upgrade", "downgrade")

# Memory buckets are binary gigabytes: a "16 GB" machine reports a little under 16 GiB.
MEMORY_BUCKETS: tuple[tuple[str, str, int], ...] = (
    ("le8", "Up to 8 GB", 8 * 2**30),
    ("le16", "9 to 16 GB", 16 * 2**30),
    ("le32", "17 to 32 GB", 32 * 2**30),
    ("le64", "33 to 64 GB", 64 * 2**30),
    ("le128", "65 to 128 GB", 128 * 2**30),
    ("gt128", "Over 128 GB", 0),
)
DISK_BUCKETS: tuple[tuple[str, str], ...] = (
    ("1", "1 disk"), ("2", "2 disks"), ("3-4", "3 to 4 disks"), ("5-8", "5 to 8 disks"), ("ge9", "9 or more disks"),
)

# ENTITY-MIB classes that are real hardware. Ports, sensors and containers are noise here.
MODULE_CLASSES = ("chassis", "backplane", "module", "powerSupply", "fan", "cpu", "stack")
MODULE_CLASS_LABELS = {
    "chassis": "Chassis", "backplane": "Backplane", "module": "Module", "powerSupply": "Power supply",
    "fan": "Fan", "cpu": "CPU", "stack": "Stack",
}

_MODULE_CLASS_SQL = ", ".join(f"'{c}'" for c in MODULE_CLASSES)      # constants only, never input

OS_FAMILY_LABELS = {
    "windows": "Windows", "linux": "Linux", "macos": "macOS", "bsd": "BSD", "fortios": "FortiOS",
    "fortiswitchos": "FortiSwitchOS", "fortiap": "FortiAP", "panos": "PAN-OS", "ios": "IOS", "ios-xe": "IOS XE",
    "nx-os": "NX-OS", "asa": "ASA", "junos": "Junos", "tmos": "TMOS", "aos-cx": "AOS-CX", "arubaos": "ArubaOS",
    "dellos10": "Dell OS10", "routeros": "RouterOS", "eos": "EOS", "other": "Other", "unknown": "Unknown",
}

_TEMPLATE_VERSION_KEYS = tuple(k for k in TEMPLATE_METRIC_KEYS if k.endswith("version"))
# HA state lives in monitoring-template values (migrate-062). Numeric enums are decoded here;
# PAN-OS stores literal strings.
_HA_KEYS = ("fgt_ha_mode", "fgt_vd_ha_state", "pan_ha_mode", "pan_ha_state", "pan_ha_peer", "f5_failover", "f5_sync")
_HA_ENUMS = {
    "fgt_ha_mode": {1: "Standalone", 2: "Active-Active", 3: "Active-Passive"},
    "fgt_vd_ha_state": {1: "Primary", 2: "Secondary", 3: "Standalone"},
    "f5_failover": {0: "Unknown", 1: "Offline", 2: "Forced offline", 3: "Standby", 4: "Active"},
    "f5_sync": {0: "Unknown", 1: "Syncing", 2: "Manual sync needed", 3: "In sync", 4: "Sync failed",
                5: "Disconnected", 6: "Standalone", 7: "Awaiting initial sync", 8: "Incompatible version",
                9: "Partial sync"},
}


# ── Clock, caches, small helpers ─────────────────────────────────────

def utcnow() -> datetime:
    """The clock every function here reads (tests and golden fixtures replace it)."""
    return datetime.now(UTC)


def iso(value: Optional[datetime]) -> Optional[str]:
    """UTC ISO-8601 with a Z suffix and whole seconds, or None."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_s(value: Optional[datetime], now: datetime) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0, int((now - value).total_seconds()))


def _j(value: Any) -> Any:
    """A jsonb value as Python, whether the driver decoded it or not."""
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _clean(value: Any, max_len: int = MAX_TEXT) -> Optional[str]:
    """Trimmed text, None when empty. NUL bytes are dropped: PostgreSQL text cannot hold them, so a driver error
    (a 500) would otherwise be one query-string character away."""
    if value is None:
        return None
    out = str(value).encode("utf-8", "replace").decode("utf-8").replace("\x00", "").strip()   # no lone surrogates either
    if not out:
        return None
    return out[:max_len]


def _like(value: str) -> str:
    """``value`` as a contains pattern for ``ILIKE ... ESCAPE '\\'``."""
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _natural_key(value: Optional[str]) -> tuple:
    """Sort key that orders '7.2' < '7.10' < '17.9' and puts missing values last. Total (NB-01, NQ-04): it is
    ``versioning.natural_key``, which never converts a digit run to an integer and reads ASCII digits only."""
    if value is None:
        return (1, ())
    return (0, versioning.natural_key(value))


def scope_block(scope: Optional[Sequence[str]]) -> dict[str, Any]:
    return {"restricted": scope is not None, "tags": sorted(scope) if scope is not None else []}


def _scope_params(scope: Optional[Sequence[str]]) -> dict[str, Any]:
    return {scoping.SCOPE_PARAM: list(scope)} if scope is not None else {}


def _scope_pred(column: str, scope: Optional[Sequence[str]]) -> str:
    """The tag-scope predicate over a jsonb tag column. A column that is not an array (a scalar or an object written
    by some other writer) counts as no tags, so it is hidden from a scoped user instead of failing the statement."""
    if scope is None:
        return "TRUE"
    return scoping.jsonb_tags_visible(f"CASE WHEN jsonb_typeof({column}) = 'array' THEN {column} ELSE '[]'::jsonb END")


def parse_asset_ref(kind: str, asset_id: str) -> Optional[tuple[str, uuid.UUID]]:
    """``(kind, uuid)`` for a well-formed asset address, else None."""
    if kind not in KINDS:
        return None
    try:
        return kind, uuid.UUID(str(asset_id))
    except (ValueError, AttributeError, TypeError):
        return None


def parse_asset_key(value: str) -> tuple[str, uuid.UUID]:
    """``'<kind>:<uuid>'`` -> ``(kind, uuid)``; raises InvalidFilter."""
    kind, _, rest = (value or "").partition(":")
    ref = parse_asset_ref(kind, rest)
    if ref is None:
        raise InvalidFilter("asset must look like '<kind>:<uuid>' with kind server or device")
    return ref


_CACHE_MAX = 64
_SUMMARY_TTL_S = 60.0
_summary_cache: dict[tuple, tuple[float, dict]] = {}
_summary_inflight: dict[tuple, "asyncio.Task[dict]"] = {}
# (database url, rule-pack revision) for which no software row keyed by an older revision exists (see
# _software_rev_pending). Process-wide and monotonic: only a rule-pack bump, which ships with a restart, creates
# such rows, and writers always stamp the current revision.
_rev_clear: Optional[tuple[str, int]] = None


def _monotonic() -> float:
    return time.monotonic()


def clear_caches() -> None:
    """Forget cached summaries (tests, and anything that needs a fresh read)."""
    global _rev_clear
    _summary_cache.clear()
    _summary_inflight.clear()
    _rev_clear = None


# ── Filters ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AssetFilters:
    """The asset filter vocabulary. The same names are query parameters of the list, facets,
    export and the URL of the Assets page. Empty strings count as unset."""

    q: Optional[str] = None
    kind: Optional[str] = None
    os_family: Optional[str] = None
    os_product: Optional[str] = None
    os_release: Optional[str] = None
    os_version: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    site: Optional[str] = None
    tag: tuple[str, ...] = ()
    source: Optional[str] = None
    freshness: Optional[str] = None
    health: Optional[str] = None
    product: Optional[str] = None
    product_version: Optional[str] = None
    # Server hardware drill-downs (Hardware page). Servers only; a device never matches.
    cpu_model: Optional[str] = None
    memory: Optional[str] = None
    disks: Optional[str] = None
    virtualization: Optional[str] = None
    security_agent: Optional[str] = None
    # Network module drill-downs (the Hardware page's ENTITY-MIB groups). Devices only; a server never matches.
    module_class: Optional[str] = None
    module_model: Optional[str] = None

    def normalized(self) -> "AssetFilters":
        """Trimmed values, empty strings dropped, vocabulary checked. Raises InvalidFilter."""
        out: dict[str, Any] = {}
        for f in ("q", "os_family", "os_product", "os_release", "os_version", "vendor", "model", "site", "source",
                  "product", "product_version", "cpu_model", "memory", "disks", "virtualization", "module_model"):
            out[f] = _clean(getattr(self, f))
        out["kind"] = _enum(_clean(self.kind), KINDS, "kind")
        out["freshness"] = _enum(_clean(self.freshness), FRESHNESS_FILTERS, "freshness")
        out["health"] = _enum(_clean(self.health), HEALTH_KEYS, "health")
        out["security_agent"] = _enum(_clean(self.security_agent), SECURITY_AGENT_VALUES, "security_agent")
        out["module_class"] = _enum(_clean(self.module_class), MODULE_CLASSES, "module_class")
        mem_keys = tuple(b[0] for b in MEMORY_BUCKETS) + (UNKNOWN,)
        out["memory"] = _enum(out["memory"], mem_keys, "memory")
        disk_keys = tuple(b[0] for b in DISK_BUCKETS) + (UNKNOWN,)
        out["disks"] = _enum(out["disks"], disk_keys, "disks")
        tags = []
        for t in self.tag or ():
            t = _clean(t)
            if t and t.lower() not in [x.lower() for x in tags]:
                tags.append(t)
        out["tag"] = tuple(tags[:20])
        if out["os_family"]:
            out["os_family"] = out["os_family"].lower()
        if out["source"]:
            out["source"] = out["source"].lower()
        if out["product_version"] and not out["product"]:
            raise InvalidFilter("product_version needs product")
        return replace(self, **out)

    def as_params(self) -> dict[str, Any]:
        """The non-empty filters under their public names (what a drill link carries)."""
        out: dict[str, Any] = {}
        for f in ("q", "kind", "os_family", "os_product", "os_release", "os_version", "vendor", "model", "site",
                  "source", "freshness", "health", "product", "product_version", "cpu_model", "memory", "disks",
                  "virtualization", "security_agent", "module_class", "module_model"):
            v = getattr(self, f)
            if v:
                out[f] = v
        if self.tag:
            out["tag"] = list(self.tag)
        return out


def _enum(value: Optional[str], allowed: Sequence[str], name: str) -> Optional[str]:
    if value is None:
        return None
    if value not in allowed:
        raise InvalidFilter(f"{name} must be one of: {', '.join(allowed)}")
    return value


# The health predicates, over the asset union alias ``a`` (see asset_union_sql). S3: model,
# serial and vendor describe network devices only; servers have no hardware identity until
# phase 3 and counting them would be noise. X3: an asset that cannot report an inventory by design
# (freshness 'not_applicable') cannot have a missing OS version, model, serial or vendor either, so the four
# identity predicates skip it; it is counted on its own line (``not_collected_by_design``) instead.
# missing_security_agent is the 'missing' state of ``sec_state`` (see _server_branch): a server whose software
# list is complete and normalised and that shows no endpoint protection. A truncated, not yet normalised or
# absent list is 'unknown' there, never 'missing'.
_REPORTS = "a.freshness <> 'not_applicable'"
NOT_COLLECTED_SQL = "(a.freshness = 'not_applicable')"
HEALTH_SQL: dict[str, str] = {
    "missing_os_version": f"(a.os_version IS NULL AND {_REPORTS})",
    "missing_model": f"(a.kind = 'device' AND a.model IS NULL AND {_REPORTS})",
    "missing_serial": f"(a.kind = 'device' AND a.serial IS NULL AND {_REPORTS})",
    "unknown_vendor": f"(a.kind = 'device' AND a.vendor IS NULL AND {_REPORTS})",
    "stale": "(a.freshness = 'stale')",
    "never": "(a.freshness = 'never')",
    "software_truncated": "(a.kind = 'server' AND a.software_truncated)",
    "missing_security_agent": "(a.sec_state = 'missing')",
}

# Which filter keys a facet ignores when it counts (so a facet shows what selecting each value
# WOULD give, with every other filter kept).
_FACET_SKIPS: dict[str, frozenset[str]] = {
    name: frozenset({name}) for name in
    ("kind", "os_family", "os_product", "vendor", "model", "site", "source", "freshness", "health")
}
# Tags are the one repeatable filter and every tag narrows (an asset must carry all of them), so choosing
# another tag intersects with the ones already chosen: the tag facet keeps its own filter.
_FACET_SKIPS["tag"] = frozenset()


def _vkey(column: str) -> str:
    """SQL for the version equivalence key of ``column`` (``versioning.equivalence_key`` evaluated by PostgreSQL)."""
    return versioning.EQUIVALENCE_KEY_SQL.format(c=column)


def _conditions(f: AssetFilters, p: dict[str, Any], skip: frozenset[str] = frozenset()) -> list[str]:
    """SQL conditions over the union alias ``a`` for ``f``. Binds into ``p`` under stable names
    (``f_*``), so several statements can share one parameter dict. ``skip`` drops filters by
    name (facets).

    ``unknown`` is the one sentinel for a missing value (os_product, os_release, os_version, vendor, model,
    cpu_model, memory, disks, virtualization); os_family stores it natively. Site has no sentinel. ``vendor=unknown``
    and ``model=unknown`` select every asset with no vendor / model, the rows of the Hardware page; the health
    predicates unknown_vendor and missing_model are the worklists: the same assets, minus those that cannot report
    by design (freshness not_applicable).
    """
    c: list[str] = []

    def want(name: str) -> bool:
        return name not in skip and bool(getattr(f, name))

    if want("kind"):
        p["f_kind"] = f.kind
        c.append("a.kind = :f_kind")
    if want("os_family"):
        p["f_os_family"] = f.os_family
        c.append("lower(a.os_family) = :f_os_family")
    if want("os_product"):
        if f.os_product.lower() == UNKNOWN:
            c.append("a.os_product IS NULL")
        else:
            p["f_os_product"] = f.os_product
            c.append("lower(a.os_product) = lower(:f_os_product)")
    if want("os_release"):
        if f.os_release.lower() == UNKNOWN:
            c.append("a.os_release IS NULL")
        else:
            p["f_os_release"] = f.os_release
            c.append("lower(a.os_release) = lower(:f_os_release)")
    if want("os_version"):
        if f.os_version.lower() == UNKNOWN:
            c.append("a.os_version IS NULL")
        else:
            # Spellings of one version are one version (7.2 and 7.2.0): the same key the OS tree groups by.
            p["f_os_version"] = f.os_version
            c.append(f"{_vkey('a.os_version')} = {_vkey('CAST(:f_os_version AS text)')}")
    for name in ("vendor", "model", "site"):
        if want(name):
            if name != "site" and getattr(f, name).lower() == UNKNOWN:
                c.append(f"a.{name} IS NULL")          # the asset reports none (whatever its freshness)
            else:
                p[f"f_{name}"] = getattr(f, name)
                c.append(f"lower(a.{name}) = lower(:f_{name})")
    if "tag" not in skip:
        for i, t in enumerate(f.tag):
            p[f"f_tag{i}"] = t.lower()
            c.append(f"EXISTS (SELECT 1 FROM jsonb_array_elements_text(a.tags) _ft{i} WHERE lower(_ft{i}) = :f_tag{i})")
    if want("source"):
        p["f_source"] = f.source
        c.append("(:f_source = ANY(a.sources))")
    if want("freshness"):
        if f.freshness == "stale_or_never":
            c.append("a.freshness IN ('stale', 'never')")
        else:
            p["f_freshness"] = f.freshness
            c.append("a.freshness = :f_freshness")
    if want("health"):
        c.append(HEALTH_SQL[f.health])
    if want("security_agent"):
        # present | missing | unknown: the three states of a.sec_state (servers only; a device has none)
        p["f_security_agent"] = f.security_agent
        c.append("(a.sec_state = CAST(:f_security_agent AS text))")
    if want("product"):
        p["f_product"] = f.product
        if f.product_version:
            p["f_product_version"] = f.product_version
        c.append("(a.kind = 'server' AND a.has_product)")      # the union joins the product's servers once
    if want("cpu_model"):
        if f.cpu_model.lower() == UNKNOWN:
            c.append("(a.kind = 'server' AND a.cpu_model IS NULL)")
        else:
            p["f_cpu_model"] = f.cpu_model
            c.append("lower(a.cpu_model) = lower(:f_cpu_model)")
    if want("memory"):
        if f.memory == UNKNOWN:
            c.append("(a.kind = 'server' AND a.memory_bucket IS NULL)")
        else:
            p["f_memory"] = f.memory
            c.append("a.memory_bucket = :f_memory")
    if want("disks"):
        if f.disks == UNKNOWN:
            c.append("(a.kind = 'server' AND a.disk_bucket IS NULL)")
        else:
            p["f_disks"] = f.disks
            c.append("a.disk_bucket = :f_disks")
    if want("virtualization"):
        if f.virtualization.lower() == UNKNOWN:
            c.append("(a.kind = 'server' AND a.virtualization IS NULL)")
        else:
            p["f_virtualization"] = f.virtualization
            c.append("lower(a.virtualization) = lower(:f_virtualization)")
    if want("module_class") or want("module_model"):
        parts = ["e.device_id = a.id"]
        if want("module_class"):
            p["f_module_class"] = f.module_class
            parts.append("e.class = :f_module_class")
        else:
            parts.append(f"e.class IN ({_MODULE_CLASS_SQL})")
        if want("module_model"):
            if f.module_model.lower() == UNKNOWN:
                parts.append("NULLIF(btrim(e.model_name), '') IS NULL")
            else:
                p["f_module_model"] = f.module_model
                parts.append("NULLIF(btrim(e.model_name), '') = :f_module_model")
        c.append("(a.kind = 'device' AND EXISTS (SELECT 1 FROM device_entities e WHERE " + " AND ".join(parts) + "))")
    if want("q"):
        p["f_q"] = _like(f.q)
        c.append(
            "(a.name ILIKE :f_q ESCAPE '\\' OR host(a.ip) ILIKE :f_q ESCAPE '\\' OR a.serial ILIKE :f_q ESCAPE '\\'"
            " OR a.model ILIKE :f_q ESCAPE '\\' OR a.vendor ILIKE :f_q ESCAPE '\\'"
            " OR a.os_product ILIKE :f_q ESCAPE '\\' OR a.os_version ILIKE :f_q ESCAPE '\\')"
        )
    return c


def _needs_security(f: AssetFilters) -> bool:
    return f.health == "missing_security_agent" or bool(f.security_agent)


def _needs_server_hw(f: AssetFilters) -> bool:
    return bool(f.cpu_model or f.memory or f.disks or f.virtualization)


def _product_mode(f: AssetFilters) -> Optional[str]:
    """How the union has to join a product filter: None (no filter), 'product' or 'version'."""
    return ("version" if f.product_version else "product") if f.product else None


# ── The asset union ──────────────────────────────────────────────────

def _fresh_case(column: str, fresh_param: str) -> str:
    return (
        f"CASE WHEN {column} IS NULL THEN 'never' "
        f"WHEN {column} >= CAST(:now AS timestamptz) - make_interval(hours => CAST(:{fresh_param} AS integer)) THEN 'fresh' "
        f"WHEN {column} >= CAST(:now AS timestamptz) - make_interval(days => CAST(:stale_days AS integer)) THEN 'aging' "
        f"ELSE 'stale' END"
    )


_MEMORY_BUCKET_SQL = (
    "CASE WHEN s.memory_total_bytes IS NULL OR s.memory_total_bytes <= 0 THEN NULL "
    + " ".join(f"WHEN s.memory_total_bytes <= {b[2]} THEN '{b[0]}'" for b in MEMORY_BUCKETS[:-1])
    + f" ELSE '{MEMORY_BUCKETS[-1][0]}' END"
)
_DISK_COUNT_SQL = "CASE WHEN jsonb_typeof(s.physical_disks) = 'array' THEN NULLIF(jsonb_array_length(s.physical_disks), 0) END"
_DISK_BUCKET_SQL = (
    f"CASE WHEN {_DISK_COUNT_SQL} IS NULL THEN NULL WHEN {_DISK_COUNT_SQL} = 1 THEN '1' "
    f"WHEN {_DISK_COUNT_SQL} = 2 THEN '2' WHEN {_DISK_COUNT_SQL} <= 4 THEN '3-4' "
    f"WHEN {_DISK_COUNT_SQL} <= 8 THEN '5-8' ELSE 'ge9' END"
)
_SERVER_MODE_SQL = "CASE s.collection_mode WHEN 'agentless_wmi' THEN 'wmi' WHEN 'agentless_winrm' THEN 'winrm' ELSE s.collection_mode END"
_SERVER_SOURCES_SQL = (
    "CASE WHEN s.collection_mode = 'none' THEN CASE WHEN s.device_id IS NOT NULL THEN ARRAY['snmp']::text[] ELSE ARRAY[]::text[] END "
    f"WHEN s.device_id IS NOT NULL AND s.collection_mode <> 'snmp' THEN ARRAY[{_SERVER_MODE_SQL}, 'snmp']::text[] "
    f"ELSE ARRAY[{_SERVER_MODE_SQL}]::text[] END"
)
_DEVICE_SOURCES_SQL = (
    "CASE WHEN d.managed_by_device_id IS NOT NULL THEN ARRAY[CASE WHEN d.snmp_enabled THEN 'snmp' ELSE 'icmp' END, 'controller']::text[] "
    "ELSE ARRAY[CASE WHEN d.snmp_enabled THEN 'snmp' ELSE 'icmp' END]::text[] END"
)
_TAGS_SQL = "CASE WHEN jsonb_typeof({c}) = 'array' THEN {c} ELSE '[]'::jsonb END"
_OS_COLS = (
    "COALESCE(NULLIF(i.os_family, ''), 'unknown') AS os_family, NULLIF(i.os_product, '') AS os_product, "
    "NULLIF(i.os_release, '') AS os_release, NULLIF(i.os_version, '') AS os_version, "
    "NULLIF(i.os_build, '') AS os_build, NULLIF(i.os_edition, '') AS os_edition, NULLIF(i.os_source, '') AS os_source"
)


def _template_raw_sql() -> str:
    keys = ", ".join(f"'{k}'" for k in _TEMPLATE_VERSION_KEYS)
    return (f"(SELECT t.value_text FROM device_template_values t WHERE t.device_id = d.id "
            f"AND t.metric_key IN ({keys}) AND btrim(t.value_text) <> '' ORDER BY t.updated_at DESC LIMIT 1)")


def _product_join(with_version: bool) -> str:
    """Servers that have the software product ``:f_product`` (optionally at ``:f_product_version``). One
    distinct set over the product's own rows (``idx_ssi_name_key``), joined once: a per-asset EXISTS is
    planned as a nested loop of 5,000 bitmap probes, and a facet query repeats it ten times. A version matches by
    its equivalence key (``7.2`` and ``7.2.0`` are one version), evaluated on the product's rows only."""
    ver = f" AND {_vkey('w.version')} = {_vkey('CAST(:f_product_version AS text)')}" if with_version else ""
    return ("LEFT JOIN (SELECT DISTINCT w.server_id FROM server_software_inventory w "
            f"WHERE w.name_key = :f_product{ver}) pf ON pf.server_id = s.id")


def _security_joins(stale_rev: bool) -> str:
    """The three sets behind ``sec_state``: servers with endpoint-protection software (``ep``), servers with a running
    Defender service (``df``) and servers with software rows that are not (re)keyed (``pend``: ``waiting`` rows the
    normaliser will still key and ``stuck`` unkeyed rows it never selects, see ``software_eligible_sql``).
    ``stale_rev`` adds the eligible rows keyed by an older rule-pack revision to ``waiting``: that needs a scan of the
    whole software table (no index), so it is only asked while such rows are known to exist (see
    ``_software_rev_pending``). A row keyed by an older revision that the loop would never select is not counted: the
    first build's loop, the only one that could have written one, never shipped (see ``data_status``)."""
    services = ", ".join(f"'{n}'" for n in ENDPOINT_PROTECTION_SERVICES)        # constants, never input
    eligible = software_eligible_sql("w", server="sv")
    stale = f" OR (w.norm_rev < {int(inventory_normalize.RULEPACK_REV)} AND {eligible})" if stale_rev else ""
    return (
        f"LEFT JOIN (SELECT DISTINCT w.server_id FROM server_software_inventory w "
        f"WHERE w.category = '{ENDPOINT_PROTECTION_CATEGORY}') ep ON ep.server_id = s.id "
        f"LEFT JOIN (SELECT DISTINCT v.server_id FROM server_service_inventory v "
        f"WHERE v.service_name IN ({services}) AND lower(v.state) = 'running') df ON df.server_id = s.id "
        f"LEFT JOIN (SELECT w.server_id, bool_or({eligible}) AS waiting, bool_or(NOT {eligible}) AS stuck "
        f"FROM server_software_inventory w JOIN servers sv ON sv.id = w.server_id "
        f"WHERE w.norm_rev IS NULL{stale} GROUP BY w.server_id) pend ON pend.server_id = s.id"
    )


# Endpoint protection, per server (X1, X9, X10). 'present': a software row of the endpoint-protection category or a running
# Defender service. Otherwise 'unknown' when the question cannot be answered from what we hold (the reasons are listed at
# SECURITY_UNKNOWN_REASONS): no software list, a list the agent cut at its cap, rows the normaliser has not keyed yet, or no
# service list to look for Defender in. Only a complete, normalised software list plus a service list that show neither is
# 'missing'. A device has no state (NULL). The reason is the same CASE, so the two cannot drift; the Python twin is
# ``security_assessment`` and the snapshot's is ``inventory_service._ASSETS_HEALTH_CTE``.
_SEC_PRESENT = "(ep.server_id IS NOT NULL OR df.server_id IS NOT NULL)"
_SEC_NO_LIST = "(s.inventory_baselined_at IS NULL OR COALESCE(pend.stuck, false))"
_SEC_TRUNCATED = "s.software_truncated"
_SEC_WAITING = "COALESCE(pend.waiting, false)"
_SEC_NO_SERVICES = "NOT EXISTS (SELECT 1 FROM server_service_inventory v WHERE v.server_id = s.id)"
_SEC_STATE_SQL = (
    f"CASE WHEN {_SEC_PRESENT} THEN 'present' "
    f"WHEN {_SEC_NO_LIST} OR {_SEC_TRUNCATED} OR {_SEC_WAITING} OR {_SEC_NO_SERVICES} THEN 'unknown' "
    "ELSE 'missing' END"
)
_SEC_REASON_SQL = (
    f"CASE WHEN {_SEC_PRESENT} THEN NULL "
    f"WHEN {_SEC_NO_LIST} THEN 'no_software_list' WHEN {_SEC_TRUNCATED} THEN 'list_truncated' "
    f"WHEN {_SEC_WAITING} THEN 'normalising' WHEN {_SEC_NO_SERVICES} THEN 'no_service_list' END"
)

# X3. A server can only report an inventory through the agent upload (the one writer of inventory_updated_at; the
# agentless and SSH collection modes have no inventory collector in this release). A server in another mode that
# never reported is not "missing a report": it cannot send one.
_SERVER_FRESHNESS_SQL = (
    "CASE WHEN s.inventory_updated_at IS NULL AND s.collection_mode <> 'agent' THEN 'not_applicable' ELSE {fresh} END"
)
# A device reports through its own SNMP poll. A ping-only device (SNMP off) and a child managed through a controller
# (its identity arrives from the controller sync, which does not stamp identity_updated_at) cannot, and never did.
_DEVICE_FRESHNESS_SQL = (
    "CASE WHEN d.identity_updated_at IS NULL AND (NOT COALESCE(d.snmp_enabled, false) "
    "OR d.managed_by_device_id IS NOT NULL OR d.poll_mode = 'via_controller') THEN 'not_applicable' ELSE {fresh} END"
)


def _server_branch(scope: Optional[Sequence[str]], security: bool, raw_os: bool, server_hw: bool,
                   product: Optional[str] = None, stale_rev: bool = False) -> str:
    sec_join = _security_joins(stale_rev) if security else ""
    sec_state = _SEC_STATE_SQL if security else "NULL::text"
    prod_join = _product_join(product == "version") if product else ""
    has_prod = "(pf.server_id IS NOT NULL)" if product else "false"
    where = _scope_pred("s.tags", scope)
    hw_cols = (
        f"NULLIF(btrim(s.cpu_model), '') AS cpu_model, {_MEMORY_BUCKET_SQL} AS memory_bucket, "
        f"{_DISK_BUCKET_SQL} AS disk_bucket, NULLIF(btrim(s.virtualization), '') AS virtualization"
        if server_hw else
        "NULL::text AS cpu_model, NULL::text AS memory_bucket, NULL::text AS disk_bucket, NULL::text AS virtualization"
    )
    freshness = _SERVER_FRESHNESS_SQL.format(fresh=_fresh_case('s.inventory_updated_at', 'fresh_srv'))
    return f"""
    SELECT 'server'::text AS kind, s.id AS id, s.display_name AS name, s.primary_ip AS ip,
           s.os_type AS asset_type, s.status AS status,
           COALESCE(NULLIF(i.vendor_label, ''), NULLIF(i.hw_vendor, ''), NULLIF(l.vendor_label, ''), NULLIF(l.hw_vendor, '')) AS vendor,
           COALESCE(NULLIF(i.hw_model, ''), NULLIF(l.hw_model, '')) AS model,
           COALESCE(NULLIF(i.hw_serial, ''), NULLIF(l.hw_serial, '')) AS serial,
           CASE WHEN COALESCE(NULLIF(i.hw_model, ''), NULLIF(i.hw_serial, ''), NULLIF(i.vendor_label, '')) IS NOT NULL
                THEN NULLIF(i.hw_source, '') ELSE NULLIF(l.hw_source, '') END AS hw_source,
           {_OS_COLS},
           NULLIF(btrim(st.name), '') AS site, {_TAGS_SQL.format(c='s.tags')} AS tags, {_SERVER_SOURCES_SQL} AS sources,
           s.last_seen AS last_seen, s.inventory_updated_at AS inventory_at,
           {freshness} AS freshness,
           s.software_truncated AS software_truncated, (s.inventory_baselined_at IS NOT NULL) AS baselined,
           {sec_state} AS sec_state, {has_prod} AS has_product, s.device_id AS linked_device_id,
           {hw_cols}
           {", NULL::text AS raw_os" if raw_os else ""}
      FROM servers s
      LEFT JOIN inventory_identity i ON i.asset_kind = 'server' AND i.asset_id = s.id
      LEFT JOIN inventory_identity l ON l.asset_kind = 'device' AND l.asset_id = s.device_id
      LEFT JOIN sites st ON st.id = s.site_id
      {sec_join}
      {prod_join}
     WHERE {where}"""


def _device_branch(scope: Optional[Sequence[str]], raw_os: bool) -> str:
    where = _scope_pred("d.tags", scope)
    raw = ""
    if raw_os:
        raw = (", CASE i.os_source WHEN 'profile' THEN NULLIF(btrim(d.os_version), '') "
               "WHEN 'entity' THEN NULLIF(btrim(d.firmware_version), '') "
               f"WHEN 'template' THEN {_template_raw_sql()} WHEN 'template_stale' THEN {_template_raw_sql()} END AS raw_os")
    freshness = _DEVICE_FRESHNESS_SQL.format(fresh=_fresh_case('d.identity_updated_at', 'fresh_dev'))
    return f"""
    SELECT 'device'::text AS kind, d.id AS id, d.hostname AS name, COALESCE(d.ip_address, d.managed_ip) AS ip,
           d.device_type AS asset_type, d.status AS status,
           COALESCE(NULLIF(i.vendor_label, ''), NULLIF(i.hw_vendor, '')) AS vendor,
           NULLIF(i.hw_model, '') AS model, NULLIF(i.hw_serial, '') AS serial, NULLIF(i.hw_source, '') AS hw_source,
           {_OS_COLS},
           NULLIF(btrim(d.location), '') AS site, {_TAGS_SQL.format(c='d.tags')} AS tags, {_DEVICE_SOURCES_SQL} AS sources,
           d.last_seen AS last_seen, d.identity_updated_at AS inventory_at,
           {freshness} AS freshness,
           false AS software_truncated, false AS baselined, NULL::text AS sec_state, false AS has_product,
           NULL::uuid AS linked_device_id,
           NULL::text AS cpu_model, NULL::text AS memory_bucket, NULL::text AS disk_bucket, NULL::text AS virtualization
           {raw}
      FROM devices d
      LEFT JOIN inventory_identity i ON i.asset_kind = 'device' AND i.asset_id = d.id
     WHERE NOT EXISTS (SELECT 1 FROM servers sx WHERE sx.device_id = d.id)
       AND {where}"""


def asset_union_sql(scope: Optional[Sequence[str]], *, kinds: Sequence[str] = KINDS, security: bool = False,
                    raw_os: bool = False, server_hw: bool = False, product: Optional[str] = None,
                    stale_rev: bool = False) -> str:
    """The asset union (doc 02 section 3.1) as a SELECT, one row per machine.

    Servers are all servers; devices exclude any device a server links to through
    ``servers.device_id`` (the SNMP side of a server counts once, and enriches the server's
    missing vendor, model and serial). The scope predicate is applied here, to each branch.
    ``kinds`` prunes a branch the caller does not need; ``security`` adds ``sec_state`` ('present', 'missing' or
    'unknown' for a server, NULL for a device; three joins over the software and service tables, ``stale_rev`` also
    counts rows keyed by an older rule-pack revision as not yet normalised); ``raw_os`` adds the raw
    firmware string; ``server_hw`` fills cpu_model, memory_bucket, disk_bucket and virtualization (NULL otherwise,
    so queries that do not filter or group on them skip the work); ``product`` ('product' or 'version') fills
    has_product from the filter's ``:f_product`` (and ``:f_product_version``) binds. Bind ``:now``, ``:fresh_srv``,
    ``:fresh_dev`` and ``:stale_days`` (see ``time_params``) and the scope parameter (see ``_scope_params``).

    ``freshness`` is fresh, aging, stale, never or not_applicable (the asset cannot report by design: see
    ``_SERVER_FRESHNESS_SQL`` and ``_DEVICE_FRESHNESS_SQL``).
    """
    branches = []
    if "server" in kinds:
        branches.append(_server_branch(scope, security, raw_os, server_hw, product, stale_rev))
    if "device" in kinds:
        branches.append(_device_branch(scope, raw_os))
    if not branches:
        raise ValueError("no asset kind selected")
    return "\n    UNION ALL\n".join(branches)


def time_params(settings: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    fresh = settings["fresh_hours"]
    return {"now": now, "fresh_srv": int(fresh["server"]), "fresh_dev": int(fresh["device"]),
            "stale_days": int(settings["stale_days"])}


def _kinds_for(f: AssetFilters) -> tuple[str, ...]:
    """Union branches a filter set can possibly match (a product or hardware filter is servers only)."""
    if f.kind:
        return (f.kind,)
    if f.product or f.cpu_model or f.memory or f.disks or f.virtualization or f.security_agent:
        return ("server",)
    if f.module_class or f.module_model:
        return ("device",)
    return KINDS


async def _settings(db: AsyncSession, settings: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    return merge_inventory_settings(settings) if settings is not None else await get_inventory_settings(db)


async def _run(db: AsyncSession, sql: str, params: Mapping[str, Any]):
    return (await db.execute(text(sql), dict(params))).mappings().all()


async def _tune(db: AsyncSession) -> None:
    """Planner settings for the statements of this module, for the current transaction only.

    * ``jit = off``: the union and its correlated lookups give the planner big cost estimates for
      statements that touch a few thousand rows, which switches JIT compilation on and adds 100 to
      500 ms to a 15 ms query. These are short read statements; JIT never pays off here.
    * ``plan_cache_mode = force_custom_plan``: the driver prepares every statement, and after five
      executions PostgreSQL may switch to a generic plan that was costed without the bound values.
      For a scope array of 2,000 server ids that plan is several times slower than the custom one
      (measured: a 10 ms statement became 580 ms once an earlier user had a tiny scope), so the same
      statement would be fast or slow depending on who asked first.
    """
    await db.execute(text("SELECT set_config('jit', 'off', true), set_config('plan_cache_mode', 'force_custom_plan', true)"))


# ── Normalisation backlog (data_status) ──────────────────────────────

def _db_key(db: AsyncSession) -> str:
    bind = getattr(db, "bind", None)
    return str(getattr(bind, "url", "") or "")


async def _software_rev_pending(db: AsyncSession) -> bool:
    """Whether any software row the normalise loop will pick up is still keyed by an older rule-pack revision than the
    running one (a row it never selects is not backlog: ``software_eligible_sql``).

    Those rows have no index (only ``norm_rev IS NULL`` has), so the answer is an EXISTS that stops at the first
    such row, and it is remembered once it is "none": after that the question costs nothing for the life of the
    process (see ``_rev_clear``). While rows exist, the callers that need it (the endpoint-protection state and the
    backlog counts) pay one scan of the table; that is the short window right after an upgrade.
    """
    global _rev_clear
    rev = int(inventory_normalize.RULEPACK_REV)
    mark = (_db_key(db), rev)
    if _rev_clear == mark:
        return False
    found = await _run(db, f"SELECT EXISTS (SELECT 1 FROM server_software_inventory s WHERE s.norm_rev < :rev "
                           f"AND {software_eligible_sql('s')}) AS e", {"rev": rev})
    if found[0]["e"]:
        return True
    _rev_clear = mark
    return False


async def data_status(db: AsyncSession, *, scope: Optional[Sequence[str]]) -> dict[str, Any]:
    """How much of the data the pages read is still being normalised (X2, X10).

    ``pending_software_rows`` counts the software rows the normalise loop will still pick up: no normalised key
    (``norm_rev`` NULL, which has a partial index) or keyed by an older rule-pack revision, and eligible by the loop's
    own rule (``inventory_service.software_eligible_sql``, shared with ``normalise_backlog`` and the endpoint-protection
    state): a row the loop never selects is not backlog, or the message would never clear. ``pending_identity_rows``
    counts the same for the identity rows of the assets the scope can see. ``normalising`` is true while either is above
    zero, and the figures that depend on them (software, endpoint protection, OS) are then incomplete: the pages say
    so. Scope-aware, and cheap: the only scan of the software table is the one-off stale-revision probe of
    ``_software_rev_pending``.

    Pages that return catalog figures call this and put the result in their response as ``data_status``.
    """
    rev = int(inventory_normalize.RULEPACK_REV)
    stale = await _software_rev_pending(db)
    pending = "(w.norm_rev IS NULL OR w.norm_rev < :rev)" if stale else "w.norm_rev IS NULL"
    params: dict[str, Any] = {"rev": rev, **_scope_params(scope)}
    if scope is None:
        # The join form of the shared rule: with a million unkeyed rows (the minutes after an upgrade) it is twice as fast as
        # a correlated EXISTS per row (96 against 210 ms for 2 M rows measured), and the same rows.
        sw_sql = (f"SELECT count(*) AS n FROM server_software_inventory w JOIN servers sx ON sx.id = w.server_id "
                  f"WHERE {pending} AND {software_eligible_sql('w', server='sx')}")
        id_sql = ("SELECT count(*) AS n FROM inventory_identity i WHERE i.asset_kind IN ('server', 'device') "
                  "AND (i.norm_rev IS NULL OR i.norm_rev < :rev)")
    else:
        sw_sql = (f"SELECT count(*) AS n FROM server_software_inventory w JOIN servers sx ON sx.id = w.server_id "
                  f"WHERE {pending} AND {software_eligible_sql('w', server='sx')} AND {_scope_pred('sx.tags', scope)}")
        id_sql = ("SELECT count(*) AS n FROM inventory_identity i WHERE (i.norm_rev IS NULL OR i.norm_rev < :rev) AND ("
                  f"(i.asset_kind = 'server' AND EXISTS (SELECT 1 FROM servers sv WHERE sv.id = i.asset_id AND {_scope_pred('sv.tags', scope)})) "
                  f"OR (i.asset_kind = 'device' AND (EXISTS (SELECT 1 FROM devices d WHERE d.id = i.asset_id AND {_scope_pred('d.tags', scope)}) "
                  f"OR EXISTS (SELECT 1 FROM servers sx WHERE sx.device_id = i.asset_id AND {_scope_pred('sx.tags', scope)}))))")
    if not stale:
        params_sw = {k: v for k, v in params.items() if k != "rev"}
    else:
        params_sw = params
    n_sw = int((await _run(db, sw_sql, params_sw))[0]["n"])
    n_id = int((await _run(db, id_sql, params))[0]["n"])
    return {"normalising": n_sw > 0 or n_id > 0, "pending_software_rows": n_sw, "pending_identity_rows": n_id}


# ── Rows ─────────────────────────────────────────────────────────────

def _asset_row(r: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    ip = r["ip"]
    return {
        "key": f"{r['kind']}:{r['id']}",
        "kind": r["kind"],
        "id": str(r["id"]),
        "name": r["name"],
        "ip": str(ip).split("/")[0] if ip is not None else None,
        "asset_type": r["asset_type"],
        "status": r["status"],
        "vendor": r["vendor"],
        "model": r["model"],
        "serial": r["serial"],
        "os_family": r["os_family"],
        "os_product": r["os_product"],
        "os_release": r["os_release"],
        "os_version": r["os_version"],
        "os_build": r["os_build"],
        "os_edition": r["os_edition"],
        "os_source": r["os_source"],
        "hw_source": r["hw_source"],
        "site": r["site"],
        "tags": [str(t) for t in (_j(r["tags"]) or [])],
        "sources": list(r["sources"] or []),
        "last_seen": iso(r["last_seen"]),
        "inventory_at": iso(r["inventory_at"]),
        "inventory_age_s": _age_s(r["inventory_at"], now),
        "freshness": r["freshness"],
        "software_truncated": bool(r["software_truncated"]),
    }


def _num_order(column: str) -> str:
    """A sort key that orders dotted versions by their numeric parts (7.2 < 7.10 < 17.9), NULL for a missing value so
    NULLS LAST applies. The digit runs of the value become a numeric array, which PostgreSQL compares part by part;
    the text itself is the next tie-break. This is the comparators' order for every plain dotted version; a tail
    such as a pre-release marker only breaks ties by text."""
    return f"CASE WHEN {column} IS NULL THEN NULL ELSE ARRAY(SELECT _m[1]::numeric FROM regexp_matches({column}, '[0-9]+', 'g') AS _m) END"


_SORTS: dict[str, tuple[str, ...]] = {
    "name": ("lower(a.name)",),
    "kind": ("a.kind",),
    "ip": ("a.ip",),
    "asset_type": ("a.asset_type",),
    "status": ("a.status",),
    "vendor": ("lower(a.vendor)",),
    "model": ("lower(a.model)",),
    "serial": ("a.serial",),
    "os_family": ("a.os_family",),
    "os_product": ("lower(a.os_product)",),
    "os_release": (_num_order("a.os_release"), "a.os_release"),
    "os_version": (_num_order("a.os_version"), "a.os_version"),
    "os": ("lower(a.os_product)", _num_order("a.os_version"), "a.os_version"),
    "site": ("lower(a.site)",),
    "last_seen": ("a.last_seen",),
    "inventory_at": ("a.inventory_at",),
    "freshness": ("CASE a.freshness WHEN 'fresh' THEN 0 WHEN 'aging' THEN 1 WHEN 'stale' THEN 2 WHEN 'never' THEN 3 ELSE 4 END",),
}
SORT_KEYS = tuple(_SORTS)


def _order_sql(sort: str, order: str) -> str:
    if sort not in _SORTS:
        raise InvalidFilter(f"sort must be one of: {', '.join(SORT_KEYS)}")
    if order not in ORDERS:
        raise InvalidFilter("order must be asc or desc")
    direction = "DESC" if order == "desc" else "ASC"
    # NULLS LAST in both directions, then the asset address: a total order, so pages never overlap.
    cols = [f"{c} {direction} NULLS LAST" for c in _SORTS[sort]]
    return ", ".join(cols + ["a.kind ASC", "a.id ASC"])


def _page_args(page: int, page_size: int) -> tuple[int, int]:
    if not isinstance(page, int) or page < 1 or page > MAX_PAGE:
        raise InvalidFilter(f"page must be between 1 and {MAX_PAGE}")
    if not isinstance(page_size, int) or page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise InvalidFilter(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    return page, page_size


_ROW_COLUMNS = (
    "a.kind, a.id, a.name, a.ip, a.asset_type, a.status, a.vendor, a.model, a.serial, a.hw_source, "
    "a.os_family, a.os_product, a.os_release, a.os_version, a.os_build, a.os_edition, a.os_source, "
    "a.site, a.tags, a.sources, a.last_seen, a.inventory_at, a.freshness, a.software_truncated"
)


# ── Asset list ───────────────────────────────────────────────────────

async def list_assets(db: AsyncSession, filters: AssetFilters, *, scope: Optional[Sequence[str]],
                      sort: str = "name", order: str = "asc", page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
                      settings: Optional[Mapping[str, Any]] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """One page of the asset union, server-side filtered, sorted and paged.

    Ordering is total (the requested key, then kind and id), so paging a changing list never
    repeats or skips a row that did not itself change.
    """
    f = filters.normalized()
    page, page_size = _page_args(page, page_size)
    order_sql = _order_sql(sort, order)
    now = now or utcnow()
    await _tune(db)
    settings = await _settings(db, settings)
    p: dict[str, Any] = {**time_params(settings, now), **_scope_params(scope)}
    conds = _conditions(f, p)
    p["limit"], p["offset"] = page_size, (page - 1) * page_size
    security = _needs_security(f)
    union = asset_union_sql(scope, kinds=_kinds_for(f), security=security, server_hw=_needs_server_hw(f),
                            product=_product_mode(f), stale_rev=security and await _software_rev_pending(db))
    where = " AND ".join(conds) if conds else "TRUE"
    rows = await _run(db, f"""
        WITH a AS ({union})
        SELECT {_ROW_COLUMNS}, count(*) OVER () AS _total
          FROM a WHERE {where}
         ORDER BY {order_sql}
         LIMIT :limit OFFSET :offset""", p)
    if rows:
        total = int(rows[0]["_total"])
    elif page > 1:
        total = int((await _run(db, f"WITH a AS ({union}) SELECT count(*) AS n FROM a WHERE {where}", p))[0]["n"])
    else:
        total = 0
    return {
        "generated_at": iso(now),
        "items": [_asset_row(r, now) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "sort": sort,
        "order": order,
    }


# ── Facets ───────────────────────────────────────────────────────────

FACET_NAMES = ("kind", "os_family", "os_product", "vendor", "model", "site", "tag", "source", "freshness", "health")
FACET_LIMITS = {"kind": 10, "os_family": 40, "os_product": 60, "vendor": 60, "model": 100, "site": 100,
                "tag": 100, "source": 10, "freshness": 10, "health": 20}


def _facet_label(name: str, value: str, label: Optional[str]) -> str:
    if name == "os_family":
        return OS_FAMILY_LABELS.get(value, value)
    if name == "freshness" and value == "not_applicable":
        return "Not applicable"
    if name == "os_product" and value == UNKNOWN:
        return "Unknown"
    return label or value


async def asset_facets(db: AsyncSession, filters: AssetFilters, *, scope: Optional[Sequence[str]],
                       settings: Optional[Mapping[str, Any]] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """Counts per facet value. Each facet counts over every active filter EXCEPT its own, so a
    count is exactly what the list would total after choosing that value (CSAM-style), and the
    header ``total`` is the list total for all filters. One statement, one evaluation of the union.
    """
    f = filters.normalized()
    now = now or utcnow()
    await _tune(db)
    settings = await _settings(db, settings)
    p: dict[str, Any] = {**time_params(settings, now), **_scope_params(scope)}

    def where(skip: frozenset[str] = frozenset(), extra: str = "") -> str:
        conds = _conditions(f, p, skip)
        if extra:
            conds.append(extra)
        return " AND ".join(conds) if conds else "TRUE"

    health_cols = ", ".join(f"count(*) FILTER (WHERE {HEALTH_SQL[k]}) AS {k}" for k in HEALTH_KEYS)
    health_values = ", ".join(f"('{k}', t.{k})" for k in HEALTH_KEYS)
    stale_rev = await _software_rev_pending(db)

    def ci(value_sql: str, skip_name: str, not_null: Optional[str] = None) -> str:
        """Counts of a text column grouped on the key its filter compares (``lower``), labelled with the most common
        spelling (ties: the first in byte order), so a row's count is the total of the list it links to."""
        cond = f"{not_null} IS NOT NULL AND " if not_null else ""
        return (f'SELECT (array_agg(g.v ORDER BY g.n DESC, g.v COLLATE "C"))[1] AS v, NULL::text AS lbl, sum(g.n)::bigint AS c '
                f"FROM (SELECT {value_sql} AS v, count(*) AS n FROM a WHERE {cond}{where(_FACET_SKIPS[skip_name])} GROUP BY 1) g "
                f"GROUP BY lower(g.v)")

    sql = f"""
    WITH a AS MATERIALIZED ({asset_union_sql(scope, kinds=_kinds_for(replace(f, kind=None)), security=True, server_hw=_needs_server_hw(f), product=_product_mode(f), stale_rev=stale_rev)}),
    f_total AS (SELECT count(*) AS c FROM a WHERE {where()}),
    f_kind AS (SELECT a.kind AS v, NULL::text AS lbl, count(*) AS c FROM a WHERE {where(_FACET_SKIPS['kind'])} GROUP BY 1),
    f_os_family AS (SELECT a.os_family AS v, NULL::text AS lbl, count(*) AS c FROM a
                     WHERE {where(_FACET_SKIPS['os_family'])} GROUP BY 1),
    f_os_product AS ({ci("COALESCE(a.os_product, 'unknown')", "os_product")}),
    f_vendor AS ({ci("a.vendor", "vendor", "a.vendor")}),
    f_model AS ({ci("a.model", "model", "a.model")}),
    f_site AS ({ci("a.site", "site", "a.site")}),
    f_tag AS (SELECT lower(t.tag) AS v, min(t.tag) AS lbl, count(DISTINCT a.kind || a.id::text) AS c
                FROM a CROSS JOIN LATERAL jsonb_array_elements_text(a.tags) AS t(tag)
               WHERE {where(_FACET_SKIPS['tag'])} GROUP BY 1),
    f_source AS (SELECT s.src AS v, NULL::text AS lbl, count(*) AS c
                   FROM a CROSS JOIN LATERAL unnest(a.sources) AS s(src)
                  WHERE {where(_FACET_SKIPS['source'])} GROUP BY 1),
    f_freshness AS (SELECT a.freshness AS v, NULL::text AS lbl, count(*) AS c FROM a
                     WHERE {where(_FACET_SKIPS['freshness'])} GROUP BY 1),
    f_health AS (SELECT {health_cols} FROM a WHERE {where(_FACET_SKIPS['health'])})
    SELECT 'total' AS facet, NULL::text AS v, NULL::text AS lbl, c FROM f_total
    UNION ALL SELECT 'kind', v, lbl, c FROM f_kind
    UNION ALL SELECT 'os_family', v, lbl, c FROM f_os_family
    UNION ALL SELECT 'os_product', v, lbl, c FROM f_os_product
    UNION ALL SELECT 'vendor', v, lbl, c FROM f_vendor
    UNION ALL SELECT 'model', v, lbl, c FROM f_model
    UNION ALL SELECT 'site', v, lbl, c FROM f_site
    UNION ALL SELECT 'tag', v, lbl, c FROM f_tag
    UNION ALL SELECT 'source', v, lbl, c FROM f_source
    UNION ALL SELECT 'freshness', v, lbl, c FROM f_freshness
    UNION ALL SELECT 'health', h.k, NULL::text, h.c FROM f_health t CROSS JOIN LATERAL (VALUES {health_values}) AS h(k, c)"""
    rows = await _run(db, sql, p)

    by: dict[str, list[dict[str, Any]]] = {n: [] for n in FACET_NAMES}
    total = 0
    for r in rows:
        if r["facet"] == "total":
            total = int(r["c"])
        else:
            by[r["facet"]].append({"value": r["v"], "label": _facet_label(r["facet"], r["v"], r["lbl"]), "count": int(r["c"])})
    # Fixed vocabularies list every value, zero counts included: a missing row would read as "not a thing".
    present = {x["value"] for x in by["kind"]}
    by["kind"] += [{"value": k, "label": k.capitalize(), "count": 0} for k in KINDS if k not in present]
    present = {x["value"] for x in by["freshness"]}
    by["freshness"] += [{"value": k, "label": _facet_label("freshness", k, None), "count": 0} for k in FRESHNESS if k not in present]
    for x in by["health"]:
        x["label"] = x["value"]
    facets: dict[str, dict[str, Any]] = {}
    for name in FACET_NAMES:
        items = by[name]
        if name in ("kind", "freshness", "health"):
            order = {"kind": KINDS, "freshness": FRESHNESS, "health": HEALTH_KEYS}[name]
            items.sort(key=lambda x: order.index(x["value"]))
        else:
            items.sort(key=lambda x: (-x["count"], str(x["label"]).lower(), str(x["value"])))
        facets[name] = {"distinct": len(items), "items": items[: FACET_LIMITS[name]]}
    return {"generated_at": iso(now), "total": total, "facets": facets}


# ── Version ordering helpers (OS tree, change labels) ────────────────

_SCHEME_OVERRIDES = {"fortiswitchos": "fortios", "fortiap": "fortios", "asa": "cisco_asa"}


def scheme_for(os_family: Optional[str], os_product: Optional[str] = None) -> str:
    """The versioning scheme for a normalised OS family (and product, for distro packages)."""
    if os_family in _SCHEME_OVERRIDES:
        return _SCHEME_OVERRIDES[os_family]
    if os_product:
        by_product = versioning.scheme_for(os_product)
        if by_product != "semverish":
            return by_product
    return versioning.scheme_for(os_family)


@functools.lru_cache(maxsize=65536)
def version_label(old: Optional[str], new: Optional[str]) -> Optional[str]:
    """'upgrade' or 'downgrade' for a software version change, None when the two versions
    are equal or cannot be ordered (the comparator refuses to guess)."""
    if not old or not new:
        return None
    c = versioning.compare("semverish", old, new)
    return "upgrade" if c == -1 else "downgrade" if c == 1 else None


def _spelling_rank(text_value: str) -> tuple[int, str]:
    """Order of preference between spellings of one value: the longer first (``7.2.0`` before ``7.2``, the order
    ``inventory_software_query.rank_versions`` uses), then byte order, so the choice never depends on row order."""
    return (-len(text_value), text_value)


def _pick_spelling(counts: Mapping[str, int]) -> str:
    """The label of a group of case variants: the most common spelling, ties in byte order."""
    return min(counts, key=lambda v: (-counts[v], v))


def _currency(scheme: str, versions: Sequence[tuple[Optional[str], int]]) -> tuple[Optional[dict[str, Any]], list[Optional[str]]]:
    """Build-currency split for one release: assets on the latest observed version, on the one
    before it (N-1), and on anything older. Returns ``(currency, ordered_versions)``;
    ``currency`` is None when the observed versions cannot all be ordered (the comparator
    returns None for some pair), in which case the order falls back to a natural sort.

    ``versions`` has one entry per version EQUIVALENCE KEY (7.2 and 7.2.0 are one entry, see
    ``versioning.equivalence_key``): the tree groups by it and the Assets filter ``os_version`` matches by it, so
    a level's count is exactly the total of the list the number links to. The comparator only orders the levels
    (and breaks the rare tie of two different keys it considers equal by the longer spelling, then byte order).

    Only versions observed on assets count: this is "latest observed", not the vendor's latest.
    Assets without a version, or with one that does not parse, are ``unknown`` here.
    """
    known = [(v, n) for v, n in versions if v is not None]
    ok = [(v, n) for v, n in known if versioning.parse(scheme, v) is not None]
    bad = [(v, n) for v, n in known if versioning.parse(scheme, v) is None]
    unknown = sum(n for v, n in versions if v is None) + sum(n for _v, n in bad)
    natural = sorted(known, key=lambda x: _natural_key(x[0]), reverse=True)
    ordered_natural = [v for v, _n in natural] + [v for v, _n in versions if v is None]
    if not ok:
        return None, ordered_natural
    if versioning.highest(scheme, [v for v, _ in ok]) is None:
        return None, ordered_natural

    def cmp(a, b):
        c = versioning.compare(scheme, a[0], b[0])
        if c:
            return c
        # equal for the comparator (or not comparable, which `highest` has ruled out): a total order anyway.
        return -1 if _spelling_rank(a[0]) > _spelling_rank(b[0]) else 1 if _spelling_rank(a[0]) < _spelling_rank(b[0]) else 0

    levels = sorted(ok, key=functools.cmp_to_key(cmp), reverse=True)
    currency = {
        "latest": levels[0][1],
        "previous": levels[1][1] if len(levels) > 1 else 0,
        "older": sum(n for _v, n in levels[2:]),
        "unknown": unknown,
        "latest_version": levels[0][0],
        "previous_version": levels[1][0] if len(levels) > 1 else None,
    }
    ordered = [v for v, _n in levels] + [v for v, _n in sorted(bad, key=lambda x: _natural_key(x[0]), reverse=True)]
    ordered += [v for v, _n in versions if v is None]
    return currency, ordered


def version_bucket(scheme: str, version: Optional[str], currency: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The build-currency bucket of one version node of a release: 'latest', 'previous', 'older' or 'unknown', None when the
    release has no currency (its versions cannot all be ordered). One rule for the OS page and the OS export (NF-BE-01): a
    missing version and a version the scheme cannot parse are 'unknown' (``_currency`` counts them so), whatever their position."""
    if currency is None:
        return None
    if version is None or versioning.parse(scheme, version) is None:
        return "unknown"
    if version == currency["latest_version"]:
        return "latest"
    if version == currency["previous_version"]:
        return "previous"
    return "older"


# ── Operating systems ────────────────────────────────────────────────

async def os_tree(db: AsyncSession, *, scope: Optional[Sequence[str]], kind: Optional[str] = None,
                  settings: Optional[Mapping[str, Any]] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """The family -> product -> release -> version tree with asset counts.

    ``kind`` limits the tree to servers or network devices. Each release carries the latest
    observed version, the build-currency split (latest, N-1, older) and, for firmware, one raw
    string example ('v7.2.5,build1517,230606 (GA.F)') so the normalisation is visible.
    Every node's ``count`` is the total of the Assets list filtered by the path to the node
    (os_family, os_product, os_release, os_version; 'unknown' for a missing level) and ``kind``.

    A node is one value of the key its filter compares: the product and the release case-insensitively
    (``IOS XE`` and ``ios xe`` are one product, labelled with the most common spelling), the version by its
    equivalence key (``7.2`` and ``7.2.0`` are one version, labelled with the longer spelling). A missing level
    is a node of its own (family ``unknown``, product / release / version ``null``); the Overview's family and
    release figures count exactly these nodes.
    """
    f = AssetFilters(kind=kind).normalized()
    now = now or utcnow()
    await _tune(db)
    settings = await _settings(db, settings)
    p: dict[str, Any] = {**time_params(settings, now), **_scope_params(scope)}
    union = asset_union_sql(scope, kinds=_kinds_for(f), raw_os=True)
    rows = await _run(db, f"""
        WITH a AS ({union})
        SELECT lower(a.os_family) AS fk, lower(a.os_product) AS pk, lower(a.os_release) AS rk,
               {_vkey('a.os_version')} AS vk, a.os_product, a.os_release, a.os_version, count(*) AS n,
               min(a.raw_os) FILTER (WHERE a.raw_os IS NOT NULL AND a.raw_os <> a.os_version) AS raw_example
          FROM a GROUP BY 1, 2, 3, 4, 5, 6, 7""", p)

    # family key -> product key -> release key -> version key -> spellings; the labels are picked once everything is in.
    fams: dict[str, dict[Optional[str], dict[Optional[str], dict[Optional[str], dict[str, Any]]]]] = {}
    prod_names: dict[tuple, dict[str, int]] = {}
    rel_names: dict[tuple, dict[str, int]] = {}
    for r in rows:
        n = int(r["n"])
        node = fams.setdefault(r["fk"], {}).setdefault(r["pk"], {}).setdefault(r["rk"], {}).setdefault(
            r["vk"], {"n": 0, "names": {}, "raw": []})
        node["n"] += n
        if r["os_version"] is not None:
            node["names"][r["os_version"]] = node["names"].get(r["os_version"], 0) + n
        if r["raw_example"] is not None:
            node["raw"].append(r["raw_example"])
        if r["os_product"] is not None:
            names = prod_names.setdefault((r["fk"], r["pk"]), {})
            names[r["os_product"]] = names.get(r["os_product"], 0) + n
        if r["os_release"] is not None:
            names = rel_names.setdefault((r["fk"], r["pk"], r["rk"]), {})
            names[r["os_release"]] = names.get(r["os_release"], 0) + n

    out_families = []
    total = 0
    for family, products_in in fams.items():
        products = []
        fam_count = 0
        scheme_family = family
        for pk, releases_in in products_in.items():
            product = _pick_spelling(prod_names[(family, pk)]) if pk is not None else None
            scheme = scheme_for(scheme_family, product)
            releases = []
            prod_count = 0
            for rk, versions_in in releases_in.items():
                release = _pick_spelling(rel_names[(family, pk, rk)]) if rk is not None else None
                entries: list[tuple[Optional[str], int]] = []
                examples: dict[Optional[str], Optional[str]] = {}
                for vk, node in versions_in.items():
                    label = min(node["names"], key=_spelling_rank) if node["names"] else None
                    entries.append((label, node["n"]))
                    examples[label] = min(node["raw"]) if node["raw"] else None
                currency, order = _currency(scheme, entries)
                by_version = dict(entries)
                rel_count = sum(n for _v, n in entries)
                releases.append({
                    "release": release,
                    "count": rel_count,
                    "latest_version": currency["latest_version"] if currency else None,
                    "distinct_versions": sum(1 for v in order if v is not None),
                    "currency": currency,
                    "versions": [{"version": v, "count": by_version[v], "raw_example": examples[v]} for v in order],
                })
                prod_count += rel_count
            # Newest release first; a missing release (unknown) always last.
            known = sorted((x for x in releases if x["release"] is not None),
                           key=lambda x: _natural_key(x["release"]), reverse=True)
            releases = known + [x for x in releases if x["release"] is None]
            products.append({"product": product, "count": prod_count, "releases": releases})
            fam_count += prod_count
        products.sort(key=lambda x: (x["product"] is None, -x["count"], str(x["product"]).lower()))
        out_families.append({"family": family, "label": OS_FAMILY_LABELS.get(family, family), "count": fam_count,
                             "products": products})
        total += fam_count
    out_families.sort(key=lambda x: (x["family"] == UNKNOWN, -x["count"], x["family"]))
    return {"generated_at": iso(now), "scope": scope_block(scope), "kind": f.kind, "total": total,
            "families": out_families}


# ── Hardware ─────────────────────────────────────────────────────────

def _coverage_pct(reporting: int, missing: int) -> Optional[float]:
    """Share of the ``reporting`` assets that are not ``missing``, in percent with one decimal. ``reporting`` counts
    the assets that can report at all; None when there are none (nothing to cover). Never rounds up to 100.0 while
    one asset is still missing, nor down to 0.0 while one is covered, so a pill and its 'N missing' link agree."""
    if not reporting:
        return None
    if missing <= 0:
        return 100.0
    covered = reporting - missing
    if covered <= 0:
        return 0.0
    return min(99.9, max(0.1, round(100.0 * covered / reporting, 1)))


async def hardware(db: AsyncSession, *, scope: Optional[Sequence[str]], vendor: Optional[str] = None,
                   settings: Optional[Mapping[str, Any]] = None, now: Optional[datetime] = None,
                   cpu_limit: int = 25) -> dict[str, Any]:
    """Network vendors and models, a model x release matrix, ENTITY-MIB module groups and server
    hardware distributions.

    Network hardware describes devices only (S3: servers have no hardware identity until phase 3).
    A vendor row counts every device of that vendor; its model rows add up to it, with an
    unknown-model row (``model`` null, ``unknown`` true) for devices that report none. Devices with no vendor are
    one ``vendor`` null row and no model rows. ``models_known`` and ``unknown_model_devices`` say how many models the
    vendor (and the whole estate: ``network.models_known``, ``network.unknown_model_devices``) has and how many
    devices have no model, so a page can print "5 models plus 4 devices with no model" without counting rows.

    Names are grouped on the key their filter compares (``lower``): ``Cisco`` and ``CISCO`` are one vendor, labelled
    with the most common spelling. ``serial_missing`` is the total of the Assets list ``health=missing_serial`` for the
    row, which skips the devices that cannot report by design (``not_collected``); the serial coverage percentage is
    over the devices that can report, and None when none can. Server distributions (CPU model, memory, disks,
    virtualisation) count servers; ``virtualization`` is null until some server reports it.
    """
    now = now or utcnow()
    await _tune(db)
    settings = await _settings(db, settings)
    vendor = _clean(vendor)
    p: dict[str, Any] = {**time_params(settings, now), **_scope_params(scope)}

    dev_union = asset_union_sql(scope, kinds=("device",))
    models = await _run(db, f"""
        WITH a AS ({dev_union}),
        m AS (SELECT e.device_id, count(*) AS n FROM device_entities e
               WHERE e.class = 'module' AND e.device_id IN (SELECT id FROM a) GROUP BY e.device_id)
        SELECT lower(a.vendor) AS vk, lower(a.model) AS mk, a.vendor, a.model, count(*) AS n,
               count(*) FILTER (WHERE {HEALTH_SQL['missing_serial']}) AS serial_missing,
               count(*) FILTER (WHERE {NOT_COLLECTED_SQL}) AS not_collected,
               COALESCE(sum(m.n), 0)::bigint AS modules
          FROM a LEFT JOIN m ON m.device_id = a.id
         GROUP BY 1, 2, 3, 4""", p)
    # distinct firmware versions (by equivalence key) per vendor and per (vendor, model); a version shared by two
    # models still counts once for the vendor, so ask the database instead of adding rows up.
    ver_rows = await _run(db, f"""
        WITH a AS ({dev_union})
        SELECT lower(a.vendor) AS vk, lower(a.model) AS mk, grouping(lower(a.model)) AS vendor_level,
               count(DISTINCT {_vkey('a.os_version')}) AS n
          FROM a WHERE a.os_version IS NOT NULL
         GROUP BY GROUPING SETS ((lower(a.vendor), lower(a.model)), (lower(a.vendor)))""", p)
    versions_by_model = {(r["vk"], r["mk"]): int(r["n"]) for r in ver_rows if not r["vendor_level"]}
    versions_by_vendor = {r["vk"]: int(r["n"]) for r in ver_rows if r["vendor_level"]}

    # labels: the most common spelling of each vendor and of each (vendor, model)
    vendor_spell: dict[Optional[str], dict[str, int]] = {}
    model_spell: dict[tuple, dict[str, int]] = {}
    groups: dict[tuple, dict[str, int]] = {}
    for r in models:
        n = int(r["n"])
        if r["vendor"] is not None:
            vs = vendor_spell.setdefault(r["vk"], {})
            vs[r["vendor"]] = vs.get(r["vendor"], 0) + n
        if r["model"] is not None:
            ms = model_spell.setdefault((r["vk"], r["mk"]), {})
            ms[r["model"]] = ms.get(r["model"], 0) + n
        g = groups.setdefault((r["vk"], r["mk"]), {"n": 0, "serial_missing": 0, "not_collected": 0, "modules": 0})
        g["n"] += n
        g["serial_missing"] += int(r["serial_missing"])
        g["not_collected"] += int(r["not_collected"])
        g["modules"] += int(r["modules"])

    def vlabel(vk: Optional[str]) -> Optional[str]:
        return _pick_spelling(vendor_spell[vk]) if vk is not None else None

    vendors: dict[Optional[str], dict[str, Any]] = {}
    model_rows: list[dict[str, Any]] = []
    devices = 0
    unknown_model_total = 0
    for (vk, mk), g in groups.items():
        n = g["n"]
        devices += n
        v = vendors.setdefault(vk, {"count": 0, "models": 0, "unknown_model": 0, "serial_missing": 0, "not_collected": 0,
                                    "modules": 0})
        v["count"] += n
        v["serial_missing"] += g["serial_missing"]
        v["not_collected"] += g["not_collected"]
        v["modules"] += g["modules"]
        if mk is not None:
            v["models"] += 1
        else:
            v["unknown_model"] += n
            unknown_model_total += n
        if vk is not None:
            model_rows.append({
                "vendor": vlabel(vk), "model": _pick_spelling(model_spell[(vk, mk)]) if mk is not None else None,
                "unknown": mk is None, "count": n, "firmware_versions": versions_by_model.get((vk, mk), 0),
                "serial_missing": g["serial_missing"], "not_collected": g["not_collected"],
                "serial_coverage_pct": _coverage_pct(n - g["not_collected"], g["serial_missing"]),
                "modules": g["modules"],
            })
    vendor_rows = [{
        "vendor": vlabel(vk), "count": v["count"], "models": v["models"], "models_known": v["models"],
        "unknown_model_devices": v["unknown_model"], "firmware_versions": versions_by_vendor.get(vk, 0),
        "serial_missing": v["serial_missing"], "not_collected": v["not_collected"],
        "serial_coverage_pct": _coverage_pct(v["count"] - v["not_collected"], v["serial_missing"]),
        "modules": v["modules"],
    } for vk, v in vendors.items()]
    vendor_rows.sort(key=lambda x: (x["vendor"] is None, -x["count"], str(x["vendor"]).lower()))
    model_rows.sort(key=lambda x: (str(x["vendor"]).lower(), x["model"] is None, -x["count"], str(x["model"]).lower()))
    models_known = sum(1 for m in model_rows if m["model"] is not None)

    matrix = None
    if vendor:
        p_m = {**p, "mx_vendor": vendor}
        cells = await _run(db, f"""
            WITH a AS ({dev_union})
            SELECT lower(a.vendor) AS vk, lower(a.model) AS mk, lower(COALESCE(a.os_release, 'unknown')) AS rk,
                   a.vendor, a.model, COALESCE(a.os_release, 'unknown') AS release, count(*) AS n
              FROM a WHERE lower(a.vendor) = lower(:mx_vendor) GROUP BY 1, 2, 3, 4, 5, 6""", p_m)
        mx_vendor_spell: dict[str, int] = {}
        mx_model_spell: dict[Optional[str], dict[str, int]] = {}
        mx_release_spell: dict[str, dict[str, int]] = {}
        by_model: dict[Optional[str], dict[str, int]] = {}
        canonical_key: Optional[str] = None
        for r in cells:
            n = int(r["n"])
            canonical_key = r["vk"]
            mx_vendor_spell[r["vendor"]] = mx_vendor_spell.get(r["vendor"], 0) + n
            if r["model"] is not None:
                ms = mx_model_spell.setdefault(r["mk"], {})
                ms[r["model"]] = ms.get(r["model"], 0) + n
            rs = mx_release_spell.setdefault(r["rk"], {})
            rs[r["release"]] = rs.get(r["release"], 0) + n
            cell = by_model.setdefault(r["mk"], {})
            cell[r["rk"]] = cell.get(r["rk"], 0) + n
        canonical = _pick_spelling(mx_vendor_spell) if mx_vendor_spell else vendor
        rel_label = {rk: _pick_spelling(names) for rk, names in mx_release_spell.items()}
        ordered_keys = sorted((rk for rk in rel_label if rk != UNKNOWN), key=lambda rk: _natural_key(rel_label[rk]), reverse=True)
        ordered_keys += [UNKNOWN] if UNKNOWN in rel_label else []
        mrows = [{"model": _pick_spelling(mx_model_spell[mk]) if mk is not None else None, "unknown": mk is None,
                  "count": sum(c.values()),
                  "cells": [{"release": rel_label[rk], "count": c[rk]} for rk in ordered_keys if rk in c]}
                 for mk, c in by_model.items()]
        mrows.sort(key=lambda x: (x["model"] is None, -x["count"], str(x["model"]).lower()))
        matrix = {"vendor": canonical, "releases": [rel_label[rk] for rk in ordered_keys], "models": mrows}

    module_rows = await _run(db, f"""
        WITH a AS ({dev_union})
        SELECT e.class, NULLIF(btrim(e.model_name), '') AS model, count(*) AS n, count(DISTINCT e.device_id) AS devices,
               count(*) FILTER (WHERE NULLIF(btrim(e.serial_number), '') IS NOT NULL) AS with_serial
          FROM device_entities e JOIN a ON a.id = e.device_id
         WHERE e.class IN ({_MODULE_CLASS_SQL})
         GROUP BY 1, 2""", p)
    modules = [{
        "class": r["class"], "class_label": MODULE_CLASS_LABELS.get(r["class"], r["class"]), "model": r["model"],
        "count": int(r["n"]), "devices": int(r["devices"]), "with_serial": int(r["with_serial"]),
    } for r in module_rows]
    order = {c: i for i, c in enumerate(MODULE_CLASSES)}
    modules.sort(key=lambda x: (order.get(x["class"], 99), x["model"] is None, -x["count"], str(x["model"]).lower()))

    srv_union = asset_union_sql(scope, kinds=("server",), server_hw=True)
    srv = await _run(db, f"""
        WITH a AS ({srv_union})
        SELECT 'total' AS dim, NULL::text AS k, NULL::text AS v, count(*) AS n FROM a
        UNION ALL SELECT 'cpu', lower(a.cpu_model), a.cpu_model, count(*) FROM a GROUP BY a.cpu_model
        UNION ALL SELECT 'memory', a.memory_bucket, a.memory_bucket, count(*) FROM a GROUP BY a.memory_bucket
        UNION ALL SELECT 'disks', a.disk_bucket, a.disk_bucket, count(*) FROM a GROUP BY a.disk_bucket
        UNION ALL SELECT 'virt', lower(a.virtualization), a.virtualization, count(*) FROM a GROUP BY a.virtualization""", p)
    # dim -> grouping key -> spelling -> servers
    dist: dict[str, dict[Optional[str], dict[Optional[str], int]]] = {"cpu": {}, "memory": {}, "disks": {}, "virt": {}}
    server_total = 0
    for r in srv:
        if r["dim"] == "total":
            server_total = int(r["n"])
        else:
            spellings = dist[r["dim"]].setdefault(r["k"], {})
            spellings[r["v"]] = spellings.get(r["v"], 0) + int(r["n"])

    def count_of(dim: str, key: Optional[str]) -> int:
        return sum(dist[dim].get(key, {}).values())

    def fixed(dim: str, buckets: Sequence[tuple]) -> list[dict[str, Any]]:
        items = [{"value": b[0], "label": b[1], "count": count_of(dim, b[0])} for b in buckets]
        if count_of(dim, None):
            items.append({"value": UNKNOWN, "label": "Unknown", "count": count_of(dim, None)})
        return items

    def known_sorted(dim: str) -> list[tuple[str, int]]:
        rows = [(_pick_spelling({k: n for k, n in sp.items() if k is not None}), sum(sp.values()))
                for key, sp in dist[dim].items() if key is not None]
        return sorted(rows, key=lambda x: (-x[1], x[0].lower(), x[0]))

    cpu_known = known_sorted("cpu")
    cpu = [{"value": k, "label": k, "count": n} for k, n in cpu_known[:cpu_limit]]
    if count_of("cpu", None):
        cpu.append({"value": UNKNOWN, "label": "Unknown", "count": count_of("cpu", None)})
    virt_known = known_sorted("virt")
    virtualization = None
    if virt_known:
        virtualization = [{"value": k, "label": k, "count": n} for k, n in virt_known]
        if count_of("virt", None):
            virtualization.append({"value": UNKNOWN, "label": "Unknown", "count": count_of("virt", None)})
    return {
        "generated_at": iso(now),
        "scope": scope_block(scope),
        "network": {"devices": devices, "models_known": models_known, "unknown_model_devices": unknown_model_total,
                    "vendors": vendor_rows, "models": model_rows},
        "matrix": matrix,
        "modules": modules,
        "servers": {
            "total": server_total,
            "cpu_models": cpu,
            "cpu_models_more": {"models": max(0, len(cpu_known) - cpu_limit),
                                "servers": sum(n for _k, n in cpu_known[cpu_limit:])},
            "memory": fixed("memory", [(b[0], b[1]) for b in MEMORY_BUCKETS]),
            "disks": fixed("disks", DISK_BUCKETS),
            "virtualization": virtualization,
        },
    }


# ── Change history ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ChangeFilters:
    """Filters of GET /inventory/changes. ``since`` defaults to seven days before now and
    ``until`` is open-ended; ``until`` is exclusive. ``asset`` is '<kind>:<uuid>' and also
    matches the events of the SNMP device a server links to. ``label`` selects software
    version changes that are upgrades or downgrades by comparator."""

    category: Optional[str] = None
    action: Optional[str] = None
    kind: Optional[str] = None
    asset: Optional[str] = None
    subject_key: Optional[str] = None
    q: Optional[str] = None
    label: Optional[str] = None
    since: Optional[datetime] = None
    until: Optional[datetime] = None

    def normalized(self) -> "ChangeFilters":
        out = {
            "category": _enum(_clean(self.category), CHANGE_CATEGORIES, "category"),
            "action": _enum(_clean(self.action), CHANGE_ACTIONS, "action"),
            "kind": _enum(_clean(self.kind), KINDS, "kind"),
            "label": _enum(_clean(self.label), CHANGE_LABELS, "label"),
            "asset": _clean(self.asset),
            "subject_key": _clean(self.subject_key, 160),
            "q": _clean(self.q),
            "since": _aware(self.since),
            "until": _aware(self.until),
        }
        if out["asset"]:
            parse_asset_key(out["asset"])
        for name in ("since", "until"):
            at = out[name]
            if at is not None and not (WINDOW_MIN <= at <= WINDOW_MAX):
                raise InvalidFilter(f"{name} must be between {WINDOW_MIN.date().isoformat()} and {WINDOW_MAX.date().isoformat()}")
        return replace(self, **out)


# The window of the change feed. Wider values are a typo (a date picker at year 0002, a script's 9999-12-31) and
# would overflow the calendar arithmetic of the per-day series, so they are a 422, not a 500.
WINDOW_MIN = datetime(1970, 1, 1, tzinfo=UTC)
WINDOW_MAX = datetime(2100, 1, 1, tzinfo=UTC)


_INSTANT_ADAPTER = TypeAdapter(datetime)


def parse_instant(name: str, raw: Optional[str]) -> Optional[datetime]:
    """The ONE parser of a date-time query parameter of any Inventory route (BN-08).

    A route takes the parameter as text and parses it here, instead of declaring ``Optional[datetime]``: pydantic
    raises a bare ``ValueError`` for a year it cannot build (``0000-01-01``), from inside FastAPI's own validation and
    before any route code runs, which was HTTP 500. The same parser (pydantic's lax ``datetime``: ISO 8601 with or
    without offset, a date, or a Unix timestamp) now answers every failure with the existing :class:`InvalidFilter`
    (422): a ``ValueError`` of any kind, an ``OverflowError`` and a type error all mean the value is not a date-time.
    None stays None; the range check (1970 to 2100) is :meth:`ChangeFilters.normalized`."""
    if raw is None:
        return None
    try:
        return _INSTANT_ADAPTER.validate_python(raw)
    except (ValueError, OverflowError, TypeError):
        raise InvalidFilter(f"{name} must be an ISO 8601 date-time between {WINDOW_MIN.year} and {WINDOW_MAX.year}")


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """UTC value of ``value`` (a naive one is taken as UTC). A value the calendar cannot convert is out of range."""
    if value is None:
        return None
    try:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise InvalidFilter("since and until must be dates between 1970 and 2100")


_EV_COLS = ("e.id, e.observed_at, e.asset_kind, e.asset_id, e.asset_name, e.category, e.action, e.subject, "
            "e.subject_key, e.old_value, e.new_value, e.detail")
MAX_SERIES_DAYS = 800


def _event_asset_pairs(cf: ChangeFilters, linked_device: Optional[uuid.UUID]) -> Optional[list[tuple[str, uuid.UUID]]]:
    """The (kind, id) pairs the asset filter addresses: the asset, plus the SNMP device a server links to."""
    if not cf.asset:
        return None
    kind, uid = parse_asset_key(cf.asset)
    pairs = [(kind, uid)]
    if kind == "server" and linked_device is not None:
        pairs.append(("device", linked_device))
    if cf.kind:
        pairs = [x for x in pairs if x[0] == cf.kind]
    return pairs


def _kind_asset_conds(cf: ChangeFilters, pairs: Optional[list[tuple[str, uuid.UUID]]], p: dict[str, Any],
                      kind_col: str, id_col: str) -> list[str]:
    c: list[str] = []
    if cf.kind:
        p["e_kind"] = cf.kind
        c.append(f"{kind_col} = :e_kind")
    if pairs is not None:
        if not pairs:
            c.append("FALSE")
        else:
            terms = []
            for i, (k, uid) in enumerate(pairs):
                p[f"e_ak{i}"], p[f"e_ai{i}"] = k, uid
                terms.append(f"({kind_col} = :e_ak{i} AND {id_col} = :e_ai{i})")
            c.append("(" + " OR ".join(terms) + ")")
    return c


def _event_q_cond(cf: ChangeFilters, p: dict[str, Any]) -> Optional[str]:
    """The free-text condition over event columns (alias ``e``), or None without ``q``."""
    if not cf.q:
        return None
    p["e_q"] = _like(cf.q)
    return ("(e.asset_name ILIKE :e_q ESCAPE '\\' OR e.subject ILIKE :e_q ESCAPE '\\' "
            "OR e.subject_key ILIKE :e_q ESCAPE '\\' OR e.old_value ILIKE :e_q ESCAPE '\\' "
            "OR e.new_value ILIKE :e_q ESCAPE '\\')")


def _event_base_conds(cf: ChangeFilters, since: datetime, p: dict[str, Any], *, text_filter: bool = True) -> list[str]:
    """Window, product and text filters: the ones the stats and the series keep. ``text_filter=False`` leaves the
    free-text condition out (the pairing statistic applies it to the pair, not to each half)."""
    c = ["e.observed_at >= :e_since"]
    p["e_since"] = since
    if cf.until is not None:
        p["e_until"] = cf.until
        c.append("e.observed_at < :e_until")
    if cf.subject_key:
        p["e_subject_key"] = cf.subject_key
        c.append("e.subject_key = :e_subject_key")
    if text_filter:
        q_cond = _event_q_cond(cf, p)
        if q_cond:
            c.append(q_cond)
    return c


def _event_class_conds(cf: ChangeFilters, p: dict[str, Any], label_pairs: Optional[list[tuple[str, str]]]) -> list[str]:
    """Category, action and label: what the stats strip offers as chips."""
    c: list[str] = []
    if cf.category:
        p["e_category"] = cf.category
        c.append("e.category = :e_category")
    if cf.action:
        p["e_action"] = cf.action
        c.append("e.action = :e_action")
    if cf.label:
        if label_pairs:
            p["e_pairs"] = json.dumps([{"o": o, "n": n} for o, n in label_pairs])
            c.append("(e.category = 'software' AND e.action = 'changed' AND (e.old_value, e.new_value) IN "
                     "(SELECT x.o, x.n FROM jsonb_to_recordset(CAST(:e_pairs AS jsonb)) AS x(o text, n text)))")
        else:
            c.append("FALSE")
    return c


def _visible_events_sql(scope: Sequence[str], kinds: Sequence[str]) -> str:
    """The visible asset set events are fetched for, as (kind, id). Tags live on the assets, not
    on events, so a restricted scope starts here and reaches events through ``idx_inv_events_asset``
    (doc 02 section 14). A device that is the SNMP side of a server follows the server's tags.
    Events of an asset that no longer exists are not reachable this way, so a restricted scope never
    sees them: their tags are gone, and unknown visibility fails closed.

    Every branch names its columns (a UNION takes its column names from the first branch, and which branch comes
    first depends on ``kinds``), and the union is DISTINCT: a device that two server records link to is one asset,
    so its events must be fetched once."""
    parts = []
    if "server" in kinds:
        parts.append(f"SELECT 'server'::text AS kind, s.id AS id FROM servers s WHERE {_scope_pred('s.tags', scope)}")
    if "device" in kinds:
        parts.append("SELECT 'device'::text AS kind, s.device_id AS id FROM servers s "
                     f"WHERE s.device_id IS NOT NULL AND {_scope_pred('s.tags', scope)}")
        parts.append("SELECT 'device'::text AS kind, d.id AS id FROM devices d "
                     "WHERE NOT EXISTS (SELECT 1 FROM servers sx WHERE sx.device_id = d.id) "
                     f"AND {_scope_pred('d.tags', scope)}")
    return " UNION ".join(parts)


# A restricted scope that reaches more than this share of the estate is served by a hash semi-join over the
# time window instead of one index probe per asset: at that size the probes read a large part of the table at
# random, which costs more than reading all of it in order. Measured at 15,000 assets and 1 M events over 400
# days: a scope of 10 percent of the assets took 386 ms per asset and 355 ms as a semi-join over the whole
# retention, a scope of 40 percent 1,145 ms and 362 ms; the reverse holds for a narrow scope (a semi-join for 100
# assets took 2.1 s where the per-asset form took 25 ms), so the semi-join is never used below this share.
WIDE_SCOPE_FRACTION = 0.15
WIDE_SCOPE_MIN_ASSETS = 50          # below this the choice does not matter and the extra count is not worth it


class Visible(list):
    """The (kind, id) of every asset a restricted scope can see; ``wide`` says how events are reached."""

    wide: bool = False


async def _visible_assets(db: AsyncSession, scope: Optional[Sequence[str]], cf: "ChangeFilters",
                          pairs: Optional[list[tuple[str, uuid.UUID]]]) -> Optional[Visible]:
    """(kind, id) of every asset the scope can see, narrowed by the kind and asset filters; None when unrestricted."""
    if scope is None:
        return None
    rows = await _run(db, _visible_events_sql(scope, _kinds_of(cf)), _scope_params(scope))
    out = Visible(dict.fromkeys((r["kind"], r["id"]) for r in rows))     # distinct, in database order
    if pairs is not None:
        wanted = set(pairs)
        out = Visible(x for x in out if x in wanted)
    if len(out) > WIDE_SCOPE_MIN_ASSETS:
        total = (await _run(db, "SELECT (SELECT count(*) FROM servers) + (SELECT count(*) FROM devices) AS n", {}))[0]["n"]
        out.wide = len(out) > WIDE_SCOPE_FRACTION * max(int(total), 1)
    return out


def _events_sql(visible: Optional[Visible], *, outer: str, conds: Sequence[str], p: dict[str, Any],
                inner_limit: str = "", tail: str = "") -> str:
    """One SELECT over change events, in the unscoped or the asset-driven (restricted) form.

    ``conds`` constrain events (alias ``e``). For a restricted scope ``visible`` is the asset set
    (bound as two arrays into ``p``). A narrow scope fetches events per asset through
    ``idx_inv_events_asset`` in a lateral subquery; the OFFSET 0 keeps the planner from flattening that
    subquery into a join, where it prefers hashing the whole time window (a sequential scan of up to
    millions of events) over a handful of index probes. ``inner_limit`` bounds the events fetched per asset
    when only the newest few are needed. A wide scope (``visible.wide``) is a semi-join of the window's
    events against the asset set instead.
    """
    where = " AND ".join(conds) if conds else "TRUE"
    if visible is None:
        return f"SELECT {outer} FROM inventory_change_events e WHERE {where} {tail}"
    p["v_kinds"] = [k for k, _i in visible]
    p["v_ids"] = [i for _k, i in visible]
    if getattr(visible, "wide", False):
        return (f"SELECT {outer} FROM inventory_change_events e WHERE {where} AND (e.asset_kind, e.asset_id) IN "
                f"(SELECT v.kind, v.id FROM unnest(CAST(:v_kinds AS text[]), CAST(:v_ids AS uuid[])) AS v(kind, id)) {tail}")
    return (f"SELECT {outer} FROM unnest(CAST(:v_kinds AS text[]), CAST(:v_ids AS uuid[])) AS v(kind, id) "
            f"CROSS JOIN LATERAL (SELECT {_EV_COLS} FROM inventory_change_events e "
            f"WHERE e.asset_kind = v.kind AND e.asset_id = v.id AND {where} {inner_limit or 'OFFSET 0'}) e {tail}")


def _kinds_of(cf: ChangeFilters) -> tuple[str, ...]:
    return (cf.kind,) if cf.kind else KINDS


def _event_row(r: Mapping[str, Any]) -> dict[str, Any]:
    detail = _j(r["detail"]) or {}
    label = version_label(r["old_value"], r["new_value"]) if r["category"] == "software" and r["action"] == "changed" else None
    return {
        "id": int(r["id"]),
        "observed_at": iso(r["observed_at"]),
        "_at": r["observed_at"],   # exact instant, for pairing; removed by _decorate_events
        "asset_key": f"{r['asset_kind']}:{r['asset_id']}",
        "asset_kind": r["asset_kind"],
        "asset_id": str(r["asset_id"]),
        "asset_name": r["asset_name"],
        "asset_present": True,
        "server_id": None,
        "category": r["category"],
        "action": r["action"],
        "subject": r["subject"],
        "subject_key": r["subject_key"],
        "old_value": r["old_value"],
        "new_value": r["new_value"],
        "label": label,
        "pair": None,
        "marker": bool(detail.get("budget_exceeded")),
        "snapshot_truncated": bool(detail.get("snapshot_truncated")),
        "detail": detail,
    }


_PRESENCE_SQL = """
SELECT 'server'::text AS kind, s.id AS id, NULL::uuid AS server_id FROM servers s WHERE s.id = ANY(CAST(:sids AS uuid[]))
UNION ALL
SELECT 'device'::text, d.id, (SELECT sx.id FROM servers sx WHERE sx.device_id = d.id ORDER BY sx.id LIMIT 1)
  FROM devices d WHERE d.id = ANY(CAST(:dids AS uuid[]))"""

_PAIR_SQL = """
SELECT e.id, e.asset_kind, e.asset_id, COALESCE(e.subject_key, e.subject) AS pkey, e.observed_at, e.action, e.old_value, e.new_value
  FROM jsonb_to_recordset(CAST(:keys AS jsonb)) AS k(kind text, id uuid, at timestamptz)
  JOIN inventory_change_events e ON e.asset_kind = k.kind AND e.asset_id = k.id AND e.observed_at = k.at
 WHERE e.category = 'software' AND e.action IN ('added', 'removed')"""


async def _decorate_events(db: AsyncSession, items: list[dict[str, Any]]) -> None:
    """Fill in what needs a second look at the page's rows, in two batched statements: whether each
    asset still exists (and the server a linked device belongs to), and the upgrade or downgrade
    pairing of 'added' + 'removed' software events written in the same transaction (a product whose
    name embeds its version is stored as a new row, doc 02 section 14). Pairing needs the exact
    instant (events of one transaction share it to the microsecond), which is why rows carry ``_at``
    until this has run."""
    try:
        if not items:
            return
        sids = sorted({x["asset_id"] for x in items if x["asset_kind"] == "server"})
        dids = sorted({x["asset_id"] for x in items if x["asset_kind"] == "device"})
        present = {(r["kind"], str(r["id"])): (str(r["server_id"]) if r["server_id"] else None)
                   for r in await _run(db, _PRESENCE_SQL, {"sids": sids, "dids": dids})}
        for x in items:
            k = (x["asset_kind"], x["asset_id"])
            x["asset_present"] = k in present
            x["server_id"] = present.get(k)
        halves = [x for x in items if x["category"] == "software" and x["action"] in ("added", "removed")]
        if not halves:
            return
        keys = sorted({(x["asset_kind"], x["asset_id"], x["_at"].isoformat()) for x in halves})
        rows = await _run(db, _PAIR_SQL, {"keys": json.dumps([{"kind": k, "id": i, "at": a} for k, i, a in keys])})
        groups: dict[tuple, list[Mapping[str, Any]]] = {}
        for r in rows:
            groups.setdefault((r["asset_kind"], str(r["asset_id"]), r["pkey"], r["observed_at"]), []).append(r)
        for x in halves:
            g = groups.get((x["asset_kind"], x["asset_id"], x["subject_key"] or x["subject"], x["_at"]), [])
            added = [r for r in g if r["action"] == "added"]
            removed = [r for r in g if r["action"] == "removed"]
            if len(added) == 1 and len(removed) == 1:
                other = removed[0] if x["action"] == "added" else added[0]
                x["pair"] = {"event_id": int(other["id"]),
                             "label": version_label(removed[0]["old_value"], added[0]["new_value"])}
    finally:
        for x in items:
            x.pop("_at", None)


async def _window_stats(db: AsyncSession, visible: Optional[Visible], cf: ChangeFilters,
                        pairs: Optional[list[tuple[str, uuid.UUID]]], since: datetime,
                        tz: Optional[str] = None, with_pairs: bool = True) -> dict[str, Any]:
    """Counts over the window for the filters other than category, action and label.

    Three small aggregates, each over the window's events and each on its own connection (the first on
    ``db``), because one grouping-sets statement sorts the whole window twice and is several times slower:

    * counts by (category, action), and by local day too when ``tz`` is given (the ``series``);
    * the distinct (old, new) version pairs of software 'changed' rows with their counts, from which the
      upgraded/downgraded counts and the pairs the ``label`` filter selects come;
    * with ``with_pairs``, the 'added' plus 'removed' pairs of one product on one asset at one instant,
      read as upgrades or downgrades (``stats.software.paired``).

    ``visible`` is the asset set of a restricted scope (see ``_events_sql``).
    """
    p: dict[str, Any] = {}
    conds = _event_base_conds(cf, since, p)
    # The pairing statistic reads both halves of a pair whether or not the free text matches each of them (the
    # list labels a row by its partner, found without the text filter), and counts the pair when either half matches.
    pair_conds = _event_base_conds(cf, since, p, text_filter=False)
    if visible is None:
        scope_conds = _kind_asset_conds(cf, pairs, p, "e.asset_kind", "e.asset_id")
        conds += scope_conds
        pair_conds += scope_conds
    if tz:
        p["e_tz"] = tz
    day = "(timezone(CAST(:e_tz AS text), e.observed_at))::date" if tz else "NULL::date"
    count_inner = _events_sql(visible, p=p, conds=conds, outer=f"e.category, e.action, {day} AS day")
    count_sql = f"SELECT category, action, day, count(*) AS n FROM ({count_inner}) t GROUP BY category, action, day"
    ver_inner = _events_sql(visible, p=p, conds=conds + ["e.category = 'software'", "e.action = 'changed'"],
                            outer="e.old_value AS ov, e.new_value AS nv")
    ver_sql = f"SELECT ov, nv, count(*) AS n FROM ({ver_inner}) t GROUP BY ov, nv"
    pair_outer = ("min(e.old_value) FILTER (WHERE e.action = 'removed') AS ov, "
                  "min(e.new_value) FILTER (WHERE e.action = 'added') AS nv")
    q_cond = _event_q_cond(cf, p)
    pair_tail = ("GROUP BY e.asset_kind, e.asset_id, COALESCE(e.subject_key, e.subject), e.observed_at "
                 "HAVING count(*) FILTER (WHERE e.action = 'added') = 1 AND count(*) FILTER (WHERE e.action = 'removed') = 1"
                 + (f" AND bool_or({q_cond})" if q_cond else ""))
    pair_inner = _events_sql(visible, outer=pair_outer, p=p,
                             conds=pair_conds + ["e.category = 'software'", "e.action IN ('added', 'removed')"], tail=pair_tail)
    pair_sql = f"SELECT g.ov, g.nv, count(*) AS n FROM ({pair_inner}) g GROUP BY 1, 2"

    async def run_counts(s):
        return await _run(s, count_sql, p)

    async def run_versions(s):
        return await _run(s, ver_sql, p)

    async def run_pairs(s):
        return await _run(s, pair_sql, p)

    jobs = [run_counts, run_versions] + ([run_pairs] if with_pairs else [])
    results = await _concurrently(db, *jobs)
    rows, ver_rows = results[0], results[1]
    pair_rows = results[2] if with_pairs else []

    sw = {"added": 0, "removed": 0, "changed": 0, "upgraded": 0, "downgraded": 0}
    other = {"os": 0, "firmware": 0, "hardware": 0}
    series: dict[date, dict[str, int]] = {}
    for r in rows:
        n = int(r["n"])
        if tz:
            series.setdefault(r["day"], dict.fromkeys(CHANGE_CATEGORIES, 0))[r["category"]] += n
        if r["category"] == "software":
            sw[r["action"]] += n
        else:
            other[r["category"]] += n
    label_pairs: dict[str, list[tuple[str, str]]] = {"upgrade": [], "downgrade": []}
    for r in ver_rows:
        lab = version_label(r["ov"], r["nv"])
        if lab:
            sw["upgraded" if lab == "upgrade" else "downgraded"] += int(r["n"])
            label_pairs[lab].append((r["ov"], r["nv"]))

    # An 'added' plus a 'removed' of one product on one asset at one instant is an upgrade or a downgrade.
    paired = {"upgraded": 0, "downgraded": 0}
    for r in pair_rows:
        lab = version_label(r["ov"], r["nv"])
        if lab:
            paired["upgraded" if lab == "upgrade" else "downgraded"] += int(r["n"])
    total = sum(sw[k] for k in ("added", "removed", "changed")) + sum(other.values())
    return {"total": total, "software": {**sw, "paired": paired}, **other, "_label_pairs": label_pairs,
            "_series": series}


# Zone ids a browser reports (Intl resolves the user's zone to the CLDR default, which for these is the old id) that a
# system tzdata without the "backward" links (Ubuntu 24.04 without tzdata-legacy) cannot open. Each maps to the zone
# the tz database itself links it to, so those users keep their local days. Anything else the host cannot open falls
# back to UTC (see ``resolve_zone``).
_ZONE_ALIASES = {
    "Africa/Asmera": "Africa/Asmara", "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Catamarca": "America/Argentina/Catamarca", "America/Cordoba": "America/Argentina/Cordoba",
    "America/Godthab": "America/Nuuk", "America/Indianapolis": "America/Indiana/Indianapolis",
    "America/Jujuy": "America/Argentina/Jujuy", "America/Louisville": "America/Kentucky/Louisville",
    "America/Mendoza": "America/Argentina/Mendoza", "Asia/Calcutta": "Asia/Kolkata", "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon", "Asia/Saigon": "Asia/Ho_Chi_Minh", "Atlantic/Faeroe": "Atlantic/Faroe",
    "Europe/Kiev": "Europe/Kyiv", "Pacific/Enderbury": "Pacific/Kanton", "Pacific/Ponape": "Pacific/Pohnpei",
    "Pacific/Truk": "Pacific/Chuuk",
}
# An IANA zone id: letters, digits, underscore, plus and minus in at most three slash-separated parts.
_ZONE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,2}$")
_zone_fallbacks_logged: set[str] = set()


def resolve_zone(name: str) -> tuple[str, ZoneInfo, bool]:
    """``(zone name, zone, fell_back)`` for a requested IANA zone id.

    A malformed id raises ``InvalidFilter`` (422). A well-formed id the host's time zone database cannot open (a legacy
    alias on an image without the backward links, or a zone newer than the image) is not the caller's mistake: a known
    alias is read as the zone it links to, anything else falls back to UTC with ``fell_back`` set, and the response
    says so (``window.tz_fallback``) instead of failing the whole page.
    """
    if not isinstance(name, str) or len(name) > 64 or not _ZONE_ID.match(name):
        raise InvalidFilter("tz must be an IANA time zone name")
    try:
        return name, ZoneInfo(name), False
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass
    alias = _ZONE_ALIASES.get(name)
    if alias:
        try:
            return alias, ZoneInfo(alias), False
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    if name not in _zone_fallbacks_logged:
        _zone_fallbacks_logged.add(name)
        logger.debug("time zone %r is not known to this host's tz database; using UTC", name)
    return "UTC", UTC_ZONE, True


async def _capture_started_at(db: AsyncSession) -> Optional[datetime]:
    """When change capture began on this installation (migration 158 records the time migration 157 armed the triggers)."""
    rows = await _run(db, "SELECT value #>> '{}' AS at FROM system_settings WHERE key = :k", {"k": CAPTURE_KEY})
    if not rows or not rows[0]["at"]:
        return None
    try:
        return _aware(datetime.fromisoformat(str(rows[0]["at"]).replace("Z", "+00:00")))
    except ValueError:
        return None


async def changes(db: AsyncSession, *, scope: Optional[Sequence[str]], params: Optional[ChangeFilters] = None,
                  page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, tz: Optional[str] = None,
                  now: Optional[datetime] = None, settings: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Change events, newest first, with window stats and a per-day series by category.

    A restricted scope is served asset-first (see ``_visible_events_sql``); the unrestricted
    path reads the time index directly. Stats and the series ignore category, action and label
    (they are the chips and the chart that offer those filters) and honour the window, kind,
    asset, product and text filters. ``total`` is the length of the filtered list.

    ``history_start`` is when change capture began on this installation (install-wide, not the date of the first
    event: a quiet estate records nothing for days) and ``retention_days`` how long events are kept, so a page can say
    "history starts X; changes older than N days are removed". ``window.tz`` is the zone the series was cut in and
    ``window.tz_fallback`` is true when the requested zone could not be opened and UTC was used (see ``resolve_zone``).
    """
    cf = (params or ChangeFilters()).normalized()
    page, page_size = _page_args(page, page_size)
    now = now or utcnow()
    await _tune(db)
    since = cf.since or (now - timedelta(days=7))
    if cf.until is not None and cf.until <= since:
        raise InvalidFilter("until must be after since")
    if tz is None:
        zone = await get_appliance_zone(db)           # the host's own zone: it opens by definition
        zone_name, fell_back = getattr(zone, "key", None) or "UTC", False
    else:
        zone_name, zone, fell_back = resolve_zone(tz)
    settings = await _settings(db, settings)

    linked = None
    if cf.asset and parse_asset_key(cf.asset)[0] == "server":
        row = await _run(db, "SELECT device_id FROM servers WHERE id = :i", {"i": parse_asset_key(cf.asset)[1]})
        linked = row[0]["device_id"] if row else None
    pairs = _event_asset_pairs(cf, linked)
    visible = await _visible_assets(db, scope, cf, pairs)
    stats = await _window_stats(db, visible, cf, pairs, since, zone_name)
    label_pairs = stats.pop("_label_pairs")
    per_day = stats.pop("_series")

    p: dict[str, Any] = {"e_tz": zone_name}
    base = _event_base_conds(cf, since, p)
    classes = _event_class_conds(cf, p, label_pairs.get(cf.label) if cf.label else None)
    scope_side = _kind_asset_conds(cf, pairs, p, "e.asset_kind", "e.asset_id") if visible is None else []
    conds = base + classes + scope_side

    if cf.category or cf.action or cf.label:
        total = int((await _run(db, _events_sql(visible, outer="count(*) AS n", conds=conds, p=p), p))[0]["n"])
    else:
        total = stats["total"]
    offset = (page - 1) * page_size
    lp = dict(p)
    lp["limit"], lp["offset"], lp["e_inner"] = page_size, offset, offset + page_size
    rows = await _run(db, _events_sql(
        visible, outer=_EV_COLS, conds=conds, p=lp,
        inner_limit="ORDER BY e.observed_at DESC, e.id DESC LIMIT :e_inner",
        tail="ORDER BY e.observed_at DESC, e.id DESC LIMIT :limit OFFSET :offset"), lp)
    items = [_event_row(r) for r in rows]
    await _decorate_events(db, items)

    # Per-day series by category, in the requested zone, one point per local day (zero days included). Events are never
    # in the future, so a far-off `until` ends the series at the present; at most MAX_SERIES_DAYS days are drawn.
    series_end = min(cf.until, now) if cf.until else now
    first_day = since.astimezone(zone).date()
    last_day = (series_end - timedelta(microseconds=1)).astimezone(zone).date() if cf.until and cf.until <= now else series_end.astimezone(zone).date()
    span = (last_day - first_day).days + 1
    if span > MAX_SERIES_DAYS:
        first_day = last_day - timedelta(days=MAX_SERIES_DAYS - 1)
        span = MAX_SERIES_DAYS
    series = []
    for i in range(max(span, 0)):
        d = first_day + timedelta(days=i)
        row = per_day.get(d, dict.fromkeys(CHANGE_CATEGORIES, 0))
        series.append({"day": d.isoformat(), **row, "total": sum(row.values())})

    return {
        "generated_at": iso(now),
        "window": {"since": iso(since), "until": iso(cf.until), "tz": zone_name, "tz_fallback": fell_back},
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "stats": stats,
        "series": series,
        "history_start": iso(await _capture_started_at(db)),
        "retention_days": int(settings["change_retention_days"]),
    }


# ── Software helpers ─────────────────────────────────────────────────

def _visible_servers_pred(scope: Optional[Sequence[str]], col: str = "w.server_id") -> str:
    """Restrict software rows to the servers a restricted scope can see (empty for no scope). The ids
    come from ``_visible_server_ids`` bound as ``:vis_sids``: one hashed array test per row is far
    cheaper over millions of rows than joining the servers table and its tag predicate."""
    if scope is None:
        return ""
    return f"AND {col} = ANY(CAST(:vis_sids AS uuid[]))"


async def _visible_server_ids(db: AsyncSession, scope: Optional[Sequence[str]]) -> dict[str, Any]:
    """Bind parameters for ``_visible_servers_pred`` (empty for no scope)."""
    if scope is None:
        return {}
    rows = await _run(db, f"SELECT sv.id FROM servers sv WHERE {_scope_pred('sv.tags', scope)}", _scope_params(scope))
    return {"vis_sids": [r["id"] for r in rows]}


def product_display_name(name_key: str, package_name: Optional[str], vendor: Optional[str],
                         source: Optional[str]) -> str:
    """The human name of a software product: the normaliser's product name for a sample raw
    title (a pack rule's canonical name, or the cleaned residual), else the raw title, else the key."""
    if package_name:
        try:
            key = inventory_normalize.normalize_software(package_name, vendor, None, source=source)
            if key.product_name:
                return key.product_name
        except Exception:  # noqa: BLE001 - a display name must never fail a request
            pass
        return package_name
    return name_key


async def _product_names(db: AsyncSession, keys: Sequence[str], scope: Optional[Sequence[str]],
                         vis: Mapping[str, Any]) -> dict[str, str]:
    """Display names for a handful of product keys, from one raw title each (the first by version,
    then name, on a server the scope can see): a pack rule gives every title of a key the same product
    name, and a fallback key is the cleaned title itself."""
    if not keys:
        return {}
    p = {"keys": list(keys), **vis}
    rows = await _run(db, f"""
        SELECT k.key AS name_key, w.package_name, w.vendor, w.source
          FROM unnest(CAST(:keys AS text[])) AS k(key)
          CROSS JOIN LATERAL (
              SELECT w.package_name, w.vendor, w.source FROM server_software_inventory w
               WHERE w.name_key = k.key {_visible_servers_pred(scope)}
               ORDER BY w.version, w.package_name LIMIT 1) w""", p)
    best = {r["name_key"]: r for r in rows}
    return {k: product_display_name(k, best[k]["package_name"], best[k]["vendor"], best[k]["source"]) if k in best else k
            for k in keys}


# Candidate keys (most installed first) the exact host counts are computed for, widening until the host
# ranking is provably exact.
_CANDIDATE_STEPS = (12, 64, 192)
# Up to this many visible software rows every product's hosts are counted exactly, in one statement (the work is
# bounded: about 60 ms at 60,000 rows); above it the candidate scheme above keeps the call within the budget.
_EXACT_SOFTWARE_ROWS = 60_000
# Distinct versions are counted from one pass over (product, version) pairs. Up to this many visible rows that pass
# covers every product, so the sprawl ranking is exact (about 0.15 ms per thousand rows). Above it the pass covers the
# most installed products that together hold ``_SPRAWL_ROW_SHARE`` of the rows (at least ``_SPRAWL_MIN_CANDIDATES``,
# at most ``_SPRAWL_MAX_CANDIDATES``) and the response says so (``sprawl_basis``): reading all 2 million rows of the
# design size costs 0.3 s, which alone is the whole cold budget of the summary (measured, see the fix-round report).
_SPRAWL_EXACT_ROWS = 400_000
_SPRAWL_ROW_SHARE = 0.25
_SPRAWL_MIN_CANDIDATES = 64
_SPRAWL_MAX_CANDIDATES = 2_000


async def _software_block(db: AsyncSession, scope: Optional[Sequence[str]], hidden: Sequence[str],
                          servers_with_software: int, top_n: int = 8) -> dict[str, Any]:
    """Catalog totals, the products installed on most hosts, and version sprawl.

    One pass over ``server_software_inventory`` counts installs per product key, with its category and publisher (a few
    hundred to a few thousand groups). Hosts and distinct versions need more:

    * hosts need ``count(DISTINCT server_id)``, which is expensive over millions of rows. Up to ``_EXACT_SOFTWARE_ROWS``
      visible rows they are counted for every product in one statement; above it for a candidate set of the most
      installed keys, which is provably enough: a key outside the set has no more installs than any key inside it and
      hosts <= installs, so the host ranking is exact once the 8th best candidate has at least min(installs of the last
      candidate, servers_with_software) hosts (candidates are taken in (installs desc, key) order, which is also the
      tie-break), and the set widens (12, 64, 192 keys) until that holds;
    * distinct versions come from one pass over (product, version) pairs: for every product up to
      ``_SPRAWL_EXACT_ROWS`` visible rows, above it for the most installed products holding ``_SPRAWL_ROW_SHARE`` of the
      rows. A version is counted by its equivalence key (``7.2`` and ``7.2.0`` are one version,
      ``versioning.equivalence_key``) and a missing or blank version is not a version, as in the catalog's
      ``version_count``. ``sprawl_basis`` says which of the two happened and how many products were ranked.

    Ranking rules: top = (hosts desc, installs desc, key); sprawl = (distinct versions desc, installs desc, key) over
    ranked products with more than one version. ``publishers`` counts distinct publisher keys, plus one when some product
    has none (the publisher facet lists those under 'unknown', and its ``distinct`` must equal this).
    """
    visp = await _visible_server_ids(db, scope)
    p: dict[str, Any] = {"hidden": list(hidden), **visp}
    vis = _visible_servers_pred(scope)
    # Every statement below reads the SAME rows: the ones of a category that is not hidden (NF-BE-02). The distinct-version
    # and host counts of the candidate path used to skip this, so a key whose rows have two categories counted its hidden rows
    # there and not in 'installs'.
    not_hidden = "COALESCE(w.category, 'other') <> ALL(CAST(:hidden AS text[]))"
    rows = await _run(db, f"""
        SELECT w.name_key, max(w.category) AS category, max(w.publisher_key) AS publisher_key, count(*) AS installs
          FROM server_software_inventory w
         WHERE w.name_key IS NOT NULL AND {not_hidden} {vis}
         GROUP BY w.name_key""", p)
    installs = {r["name_key"]: int(r["installs"]) for r in rows}
    category = {r["name_key"]: r["category"] for r in rows}
    publisher = {r["name_key"]: (r["publisher_key"] or None) for r in rows}
    block: dict[str, Any] = {
        "products": len(rows),
        "installs": sum(installs.values()),
        "publishers": len({v for v in publisher.values() if v}) + (1 if any(v is None for v in publisher.values()) else 0),
        "hidden_categories": list(hidden),
        "top": [],
        "sprawl": [],
        "sprawl_basis": {"scope": "all_products", "candidates": len(rows), "products": len(rows)},
    }
    if not rows:
        return block
    ordered = sorted(installs.items(), key=lambda kv: (-kv[1], kv[0]))
    hosts: dict[str, int] = {}
    versions: dict[str, int] = {}

    async def count_hosts(keys: Sequence[str]) -> None:
        if not keys:
            return
        for r in await _run(db, f"""
                SELECT g.name_key, count(*) AS n FROM (
                    SELECT w.name_key, w.server_id FROM server_software_inventory w
                     WHERE w.name_key = ANY(CAST(:keys AS text[])) AND {not_hidden} {vis} GROUP BY 1, 2) g GROUP BY 1""",
                {"keys": list(keys), **p}):
            hosts[r["name_key"]] = int(r["n"])
        for k in keys:
            hosts.setdefault(k, 0)

    async def count_versions(keys: Optional[Sequence[str]]) -> None:
        """Distinct versions of ``keys`` (None: every product) by equivalence key, counted by the database over the
        (product, version) pairs (tens of thousands of them) so that only one row per product comes back."""
        want = "w.name_key IS NOT NULL" if keys is None else "w.name_key = ANY(CAST(:keys AS text[]))"
        params = dict(p) if keys is None else {"keys": list(keys), **p}
        got = {r["name_key"]: int(r["n"]) for r in await _run(db, f"""
                SELECT g.name_key, count(DISTINCT {_vkey('g.version')}) AS n FROM (
                    SELECT w.name_key, w.version FROM server_software_inventory w
                     WHERE {want} AND w.version IS NOT NULL AND {not_hidden} {vis} GROUP BY 1, 2) g
                 WHERE NOT ({versioning.BLANK_VERSION_SQL.format(c='g.version')})
                 GROUP BY g.name_key""", params)}
        for k in (installs if keys is None else keys):
            if k in installs:
                versions[k] = got.get(k, 0)

    if block["installs"] <= _EXACT_SOFTWARE_ROWS:
        for r in await _run(db, f"""
                SELECT w.name_key, count(DISTINCT w.server_id) AS hosts
                  FROM server_software_inventory w
                 WHERE w.name_key IS NOT NULL AND {not_hidden} {vis}
                 GROUP BY w.name_key""", p):
            hosts[r["name_key"]] = int(r["hosts"])
        ranked = sorted(installs, key=lambda k: (-hosts[k], -installs[k], k))
    else:
        ranked = []
        for size in _CANDIDATE_STEPS:
            cands = [k for k, _n in ordered[:size]]
            await count_hosts([k for k in cands if k not in hosts])
            ranked = sorted(cands, key=lambda k: (-hosts[k], -installs[k], k))
            if len(cands) >= len(ordered):
                break
            bound = min(installs[cands[-1]], servers_with_software)
            if len(ranked) >= top_n and hosts[ranked[top_n - 1]] >= bound:
                break

    if block["installs"] <= _SPRAWL_EXACT_ROWS:
        pool = list(installs)
        await count_versions(None)
    else:
        pool, covered = [], 0
        target = _SPRAWL_ROW_SHARE * block["installs"]
        for k, n in ordered:
            pool.append(k)
            covered += n
            if len(pool) >= _SPRAWL_MAX_CANDIDATES or (len(pool) >= _SPRAWL_MIN_CANDIDATES and covered >= target):
                break
        await count_versions(pool)
        block["sprawl_basis"] = {"scope": "top_installed", "candidates": len(pool), "products": len(rows)}
    top = ranked[:top_n]
    sprawl = sorted((k for k in pool if versions.get(k, 0) > 1), key=lambda k: (-versions[k], -installs[k], k))[:top_n]
    missing_versions = [k for k in top if k not in versions]
    if missing_versions:
        await count_versions(missing_versions)
    await count_hosts([k for k in sprawl if k not in hosts])
    names = await _product_names(db, sorted(set(top) | set(sprawl)), scope, visp)
    block["top"] = [{"product_key": k, "name": names.get(k, k), "category": category.get(k),
                     "hosts": hosts[k], "versions": versions.get(k, 0)} for k in top]
    block["sprawl"] = [{"product_key": k, "name": names.get(k, k), "category": category.get(k),
                        "versions": versions[k], "hosts": hosts[k]} for k in sprawl]
    return block


# ── Summary ──────────────────────────────────────────────────────────

SUMMARY_TOP_OS = 10
SUMMARY_TOP_MODELS = 8


def _settings_fingerprint(settings: Mapping[str, Any]) -> str:
    keep = {k: settings[k] for k in ("fresh_hours", "stale_days", "hide_categories_by_default")}
    return json.dumps(keep, sort_keys=True)


# Requests that fan out to extra connections at the same moment, at most. The pool is shared with the rest of the
# application (20 + 10 connections): if every request held one connection and waited for two more, enough of them at
# once would wait on each other until the pool timeout. Past this number a request runs its blocks one after another
# on its own connection, which is slower and cannot starve anyone.
MAX_FANOUT = 2
_fanout_active = 0


PINNED_KEY = "inventory.pinned"        # AsyncSession.info flag, see ``engine_of``


def engine_of(db: AsyncSession) -> Optional[Any]:
    """The engine a helper may open further sessions from on behalf of ``db``, or None when the work must stay on ``db``
    itself, one statement after another, and ``db`` must not be closed: a session that has no engine (a test double) and a
    PINNED one. The export pins its session (``info[PINNED_KEY]``): all of a file's reads belong to its one REPEATABLE READ
    read-only transaction, which a fan-out to another connection would leave (a different snapshot) and a ``close()`` to
    wait for a shared build would end (NB-03)."""
    info = getattr(db, "info", None)
    if isinstance(info, dict) and info.get(PINNED_KEY):
        return None
    return getattr(db, "bind", None)


async def _concurrently(db: AsyncSession, *fns):
    """Run read-only blocks on separate connections at once (the first on ``db`` itself). Falls back
    to running them one after another on ``db`` when the session has no engine to open more from (or is pinned, see
    ``engine_of``), or when ``MAX_FANOUT`` other requests are already doing so."""
    global _fanout_active
    bind = engine_of(db)
    if bind is None or len(fns) < 2 or _fanout_active >= MAX_FANOUT:
        return [await fn(db) for fn in fns]
    _fanout_active += 1
    extra = [AsyncSession(bind=bind, expire_on_commit=False) for _ in fns[1:]]

    async def run(fn, session):
        if session is not db:
            await _tune(session)
        return await fn(session)

    try:
        results = await asyncio.gather(*(run(fn, s) for fn, s in zip(fns, [db, *extra])), return_exceptions=True)
    finally:
        _fanout_active -= 1
        for s in extra:
            await s.close()
    for r in results:
        if isinstance(r, BaseException):
            raise r
    return list(results)


async def _summary_core(db: AsyncSession, scope: Optional[Sequence[str]], settings: Mapping[str, Any],
                        now: datetime) -> dict[str, Any]:
    p: dict[str, Any] = {**time_params(settings, now), **_scope_params(scope)}
    health_cols = ", ".join(f"count(*) FILTER (WHERE {HEALTH_SQL[k]}) AS h_{k}" for k in HEALTH_KEYS)
    stale_rev = await _software_rev_pending(db)
    sql = f"""
    WITH a AS MATERIALIZED ({asset_union_sql(scope, security=True, stale_rev=stale_rev)})
    SELECT
      (SELECT to_jsonb(t) FROM (
         SELECT count(*) AS total, count(*) FILTER (WHERE kind = 'server') AS server,
                count(*) FILTER (WHERE kind = 'device') AS device,
                count(*) FILTER (WHERE freshness = 'fresh') AS fresh, count(*) FILTER (WHERE freshness = 'aging') AS aging,
                count(*) FILTER (WHERE freshness = 'stale') AS stale, count(*) FILTER (WHERE freshness = 'never') AS never,
                count(*) FILTER (WHERE freshness = 'not_applicable') AS not_applicable,
                count(*) FILTER (WHERE sec_state = 'present') AS ep_present,
                count(*) FILTER (WHERE sec_state = 'missing') AS ep_missing,
                count(*) FILTER (WHERE sec_state = 'unknown') AS ep_unknown,
                {health_cols}
           FROM a) t) AS counts,
      (SELECT COALESCE(jsonb_agg(to_jsonb(g)), '[]'::jsonb) FROM (
         SELECT lower(os_family) AS fk, lower(os_product) AS pk, lower(os_release) AS rk, os_product AS product,
                os_release AS release, count(*) AS n FROM a GROUP BY 1, 2, 3, 4, 5) g) AS os,
      (SELECT COALESCE(jsonb_agg(to_jsonb(g)), '[]'::jsonb) FROM (
         SELECT lower(vendor) AS vk, lower(model) AS mk, vendor, model, count(*) AS n
           FROM a WHERE kind = 'device' GROUP BY 1, 2, 3, 4) g) AS hw,
      (SELECT count(DISTINCT s.device_id) FROM servers s WHERE s.device_id IS NOT NULL AND {_scope_pred('s.tags', scope)}) AS linked"""
    r = (await _run(db, sql, p))[0]
    counts = _j(r["counts"])
    os_groups = _j(r["os"])
    hw_groups = _j(r["hw"])

    # Operating systems: one node per (family, product, release) on the keys the filters compare, so the figures are
    # the row counts of the OS page and every count is the total of the list behind it.
    os_nodes: dict[tuple, int] = {}
    prod_names: dict[tuple, dict[str, int]] = {}
    rel_names: dict[tuple, dict[str, int]] = {}
    for g in os_groups:
        key = (g["fk"], g["pk"], g["rk"])
        n = int(g["n"])
        os_nodes[key] = os_nodes.get(key, 0) + n
        if g["product"] is not None:
            names = prod_names.setdefault((g["fk"], g["pk"]), {})
            names[g["product"]] = names.get(g["product"], 0) + n
        if g["release"] is not None:
            names = rel_names.setdefault(key, {})
            names[g["release"]] = names.get(g["release"], 0) + n

    def os_label(key: tuple) -> tuple[str, Optional[str], Optional[str]]:
        fk, pk, rk = key
        return (fk, _pick_spelling(prod_names[(fk, pk)]) if pk is not None else None,
                _pick_spelling(rel_names[key]) if rk is not None else None)

    known_nodes = [(os_label(k), n) for k, n in os_nodes.items() if k[0] != UNKNOWN]
    unknown = sum(n for k, n in os_nodes.items() if k[0] == UNKNOWN)
    top = sorted(known_nodes, key=lambda x: (-x[1], x[0][0], str(x[0][1]), str(x[0][2])))[:SUMMARY_TOP_OS]

    # Hardware: vendors and models on the lower-case key, labelled with the most common spelling.
    vendor_names: dict[str, dict[str, int]] = {}
    model_names: dict[tuple, dict[str, int]] = {}
    hw_nodes: dict[tuple, int] = {}
    for g in hw_groups:
        n = int(g["n"])
        hw_nodes[(g["vk"], g["mk"])] = hw_nodes.get((g["vk"], g["mk"]), 0) + n
        if g["vendor"] is not None:
            names = vendor_names.setdefault(g["vk"], {})
            names[g["vendor"]] = names.get(g["vendor"], 0) + n
        if g["model"] is not None and g["vendor"] is not None:
            names = model_names.setdefault((g["vk"], g["mk"]), {})
            names[g["model"]] = names.get(g["model"], 0) + n
    models = [(_pick_spelling(vendor_names[vk]), _pick_spelling(model_names[(vk, mk)]), n)
              for (vk, mk), n in hw_nodes.items() if vk is not None and mk is not None]
    top_models = sorted(models, key=lambda x: (-x[2], x[0].lower(), x[1].lower()))[:SUMMARY_TOP_MODELS]
    total = int(counts["total"])
    not_applicable = int(counts["not_applicable"])
    reporting = total - not_applicable
    return {
        "assets": {"total": total, "server": int(counts["server"]), "device": int(counts["device"]),
                   "linked_devices": int(r["linked"])},
        "coverage": {
            "fresh": int(counts["fresh"]), "aging": int(counts["aging"]), "stale": int(counts["stale"]),
            "never": int(counts["never"]), "not_applicable": not_applicable,
            # over the assets that can report: one that cannot, by design, is neither fresh nor a gap
            "fresh_pct": _coverage_pct(reporting, reporting - int(counts["fresh"])),
            "fresh_hours": dict(settings["fresh_hours"]), "stale_days": int(settings["stale_days"]),
        },
        "os": {
            "families": len({k[0] for k in os_nodes}),
            "releases": len(os_nodes),
            "unknown": unknown,
            "top": [{"family": fam, "product": product, "release": release, "count": n}
                    for (fam, product, release), n in top],
        },
        "hardware": {
            "devices": int(counts["device"]),
            "vendors": len({vk for (vk, _mk) in hw_nodes if vk is not None}),
            "models": len(models),
            "top_models": [{"vendor": v, "model": m, "count": n} for v, m, n in top_models],
        },
        "health": {**{k: int(counts[f"h_{k}"]) for k in HEALTH_KEYS}, "not_collected_by_design": {"count": not_applicable}},
        "_security": {"covered": int(counts["ep_present"]), "missing": int(counts["ep_missing"]),
                      "unknown": int(counts["ep_unknown"]), "total": int(counts["server"])},
    }


async def _summary_changes(db: AsyncSession, scope: Optional[Sequence[str]], now: datetime) -> dict[str, Any]:
    since = now - timedelta(days=7)
    cf = ChangeFilters()
    visible = await _visible_assets(db, scope, cf, None)
    st = await _window_stats(db, visible, cf, None, since, with_pairs=False)
    sw = st["software"]
    return {"since": iso(since), "total": st["total"],
            "software": {k: sw[k] for k in ("added", "removed", "changed", "upgraded", "downgraded")},
            "os": st["os"], "firmware": st["firmware"], "hardware": st["hardware"]}


async def _build_summary(db: AsyncSession, scope: Optional[Sequence[str]], settings: Mapping[str, Any],
                         now: datetime) -> dict[str, Any]:
    """One uncached build of the Overview block on ``db`` (its blocks run on up to three connections at once)."""
    hidden = list(settings["hide_categories_by_default"])
    await _tune(db)

    async def core(s):
        return await _summary_core(s, scope, settings, now)

    async def chg(s):
        return await _summary_changes(s, scope, now)

    async def sw(s):
        n = await _run(s, "SELECT count(*) AS n FROM servers sv WHERE sv.inventory_baselined_at IS NOT NULL AND "
                       + _scope_pred("sv.tags", scope), _scope_params(scope))
        block = await _software_block(s, scope, hidden, int(n[0]["n"]))
        block["_data_status"] = await data_status(s, scope=scope)
        return block

    c, ch, soft = await _concurrently(db, core, chg, sw)
    data = soft.pop("_data_status")
    soft["security_agent_coverage"] = c.pop("_security")
    return {
        "generated_at": iso(now),
        "scope": scope_block(scope),
        "assets": c["assets"],
        "coverage": c["coverage"],
        "os": c["os"],
        "software": soft,
        "hardware": c["hardware"],
        "changes_7d": ch,
        "health": c["health"],
        "data_status": data,
        "lifecycle": None,
    }


async def _wait_for_shared(db: AsyncSession, task: "asyncio.Task[dict]") -> dict[str, Any]:
    """Wait for a build another request (or this one) started, without holding a pooled connection while waiting.

    The build runs on a session of its own and needs a connection from the same pool; callers that each kept theirs
    while they waited could use the pool up and starve the build they are waiting for. Closing the session returns
    the connection; objects already loaded stay readable (the application's sessions do not expire on commit), and
    the session can be used again afterwards. ``shield`` keeps a caller that goes away from cancelling the shared build.
    A pinned session (``engine_of``) is not closed: its transaction is the point of pinning it."""
    if engine_of(db) is not None:
        try:
            await db.close()
        except Exception:   # noqa: BLE001 - failing to release must not fail the request
            logger.debug("could not release the session before waiting for the shared summary build", exc_info=True)
    return await asyncio.shield(task)


async def summary(db: AsyncSession, *, scope: Optional[Sequence[str]], settings: Optional[Mapping[str, Any]] = None,
                  now: Optional[datetime] = None, use_cache: bool = True) -> dict[str, Any]:
    """The Overview block (doc 02 section 8, extended; see 05-P1-API-CONTRACT.md).

    ``os.families`` and ``os.releases`` are the numbers of family and release NODES of the OS page's tree for all kinds
    (the unknown family and a missing release are nodes of their own and are counted), so the figure and the page
    agree; ``os.unknown`` is the number of assets whose family is unknown. ``coverage.fresh_pct`` is over the assets
    that can report (``assets.total - coverage.not_applicable``). ``software.security_agent_coverage`` partitions the
    servers into covered, missing and unknown. ``software.sprawl_basis`` says what the sprawl ranking covered.

    Cached for 60 s per (scope, settings) in this process, and built once for concurrent callers: the first caller
    after the expiry starts the build, the others wait for the same one (each releases its database connection
    first), so a burst of N cold requests costs one build, not N. Every count is the total of the Assets
    (or Changes) list for the filters named in the contract; ``scope`` says whether the figures cover
    the whole estate or only the caller's visible assets. Passing ``now`` bypasses the cache.
    """
    settings = await _settings(db, settings)
    explicit_now = now is not None
    key = (tuple(sorted(scope)) if scope is not None else None, _settings_fingerprint(settings))
    if not use_cache or explicit_now:
        return await _build_summary(db, scope, settings, now or utcnow())
    hit = _summary_cache.get(key)
    if hit is not None and _monotonic() - hit[0] < _SUMMARY_TTL_S:
        return hit[1]
    loop = asyncio.get_running_loop()
    task = _summary_inflight.get(key)
    if task is None or task.done() or task.get_loop() is not loop:
        bind = getattr(db, "bind", None)
        started = utcnow()

        async def build() -> dict[str, Any]:
            if bind is None:      # a session without an engine to open another from (a test double): build on it
                return await _build_summary(db, scope, settings, started)
            async with AsyncSession(bind=bind, expire_on_commit=False) as own:
                return await _build_summary(own, scope, settings, started)

        task = loop.create_task(build())
        _summary_inflight[key] = task

        def done(t: "asyncio.Task[dict]") -> None:
            if _summary_inflight.get(key) is t:
                _summary_inflight.pop(key, None)
            if t.cancelled() or t.exception() is not None:
                return
            if len(_summary_cache) >= _CACHE_MAX:
                oldest = min(_summary_cache, key=lambda k: _summary_cache[k][0])
                _summary_cache.pop(oldest, None)
            _summary_cache[key] = (_monotonic(), t.result())

        task.add_done_callback(done)
    if getattr(db, "bind", None) is None:
        return await asyncio.shield(task)
    return await _wait_for_shared(db, task)


# ── Asset detail (the flyout) ────────────────────────────────────────

# Stored os_source / hw_source values -> the source badge the UI draws.
_SOURCE_BADGE = {
    "agent": "agent", "profile": "snmp", "device": "snmp", "sysdescr": "snmp", "managed": "snmp",
    "entity": "entity-mib", "template": "template", "template_stale": "template", "ssh": "ssh",
}
_IDENTITY_FIELDS = (
    ("vendor", "Vendor", "hw"), ("model", "Model", "hw"), ("serial", "Serial number", "hw"),
    ("os_family", "OS family", "os"), ("os_product", "Operating system", "os"), ("os_release", "Release", "os"),
    ("os_version", "Version", "os"), ("os_build", "Build", "os"), ("os_edition", "Edition", "os"),
)
RECENT_CHANGES = 10
MAX_MODULE_ROWS = 500
MAX_SERVER_SOFTWARE = 5001


def _ha_label(key: str, num: Any, txt: Any) -> Optional[str]:
    text_value = (txt or "").strip() if isinstance(txt, str) else ""
    if text_value:
        return text_value
    if num is None:
        return None
    return _HA_ENUMS.get(key, {}).get(int(num), str(int(num)) if float(num).is_integer() else str(num))


def _ha_block(rows: Sequence[Mapping[str, Any]], now: datetime) -> Optional[dict[str, Any]]:
    """HA mode and role from monitoring-template values (FortiGate, PAN-OS, F5), or None."""
    vals: dict[str, list[tuple[str, Optional[str], datetime]]] = {}
    for r in rows:
        if r["metric_key"] in _HA_KEYS:
            vals.setdefault(r["metric_key"], []).append(
                (r["instance"] or "", _ha_label(r["metric_key"], r["value_num"], r["value_text"]), r["updated_at"]))
    if not vals:
        return None

    def first(*keys: str) -> Optional[str]:
        for k in keys:
            for _inst, label, _at in sorted(vals.get(k, []), key=lambda x: x[0]):
                if label:
                    return label
        return None

    observed = max(at for items in vals.values() for _i, _l, at in items)
    return {
        "mode": first("fgt_ha_mode", "pan_ha_mode"),
        "role": first("fgt_vd_ha_state", "pan_ha_state", "f5_failover"),
        "peer": first("pan_ha_peer"),
        "sync": first("f5_sync"),
        "observed_at": iso(observed),
        "age_s": _age_s(observed, now),
    }


def _identity_list(row: Mapping[str, Any], observed: Mapping[str, Optional[datetime]], now: datetime) -> list[dict[str, Any]]:
    out = []
    for field_name, label, group in _IDENTITY_FIELDS:
        value = row[field_name]
        if group == "os":
            detail = row["os_source"]
        else:
            detail = row["hw_source"]
        if field_name == "os_family" and value == UNKNOWN:
            value, detail = None, None
        has = value is not None
        at = observed.get(group) if has else None
        out.append({
            "field": field_name,
            "label": label,
            "value": value,
            "source": _SOURCE_BADGE.get(detail, detail) if has else None,
            "source_detail": detail if has else None,
            "observed_at": iso(at),
            "age_s": _age_s(at, now),
        })
    return out


def security_assessment(*, endpoint_rows: int, defender_services: int, baselined: bool, truncated: bool, pending_rows: int,
                        unkeyable_rows: int, service_rows: int) -> tuple[str, Optional[str]]:
    """The endpoint-protection ``(state, reason)`` of one server: 'present', 'missing' or 'unknown' and, when unknown, one of
    ``SECURITY_UNKNOWN_REASONS`` (None otherwise). The Python twin of ``_SEC_STATE_SQL`` and ``_SEC_REASON_SQL`` (the
    flyout reads one server's rows instead of joining the whole table, and a test holds the two to the same answer for
    every server of the fleet). ``pending_rows`` are rows the normaliser will still key, ``unkeyable_rows`` unkeyed rows it
    never selects, ``service_rows`` all of the server's service rows."""
    if endpoint_rows > 0 or defender_services > 0:
        return "present", None
    if not baselined or unkeyable_rows > 0:
        return "unknown", "no_software_list"
    if truncated:
        return "unknown", "list_truncated"
    if pending_rows > 0:
        return "unknown", "normalising"
    if service_rows == 0:
        return "unknown", "no_service_list"
    return "missing", None


def security_state(**kw: Any) -> str:
    """Only the state of ``security_assessment`` (same keyword arguments)."""
    return security_assessment(**kw)[0]


async def _server_body(db: AsyncSession, uid: uuid.UUID, hidden: Sequence[str], now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    srv = (await _run(db, """
        SELECT s.os_name, s.os_version, s.kernel_or_build, s.architecture, s.cpu_model, s.cpu_cores, s.cpu_physical_cores,
               s.memory_total_bytes, s.physical_disks, s.virtualization, s.bios_version, s.bios_date, s.hw_manufacturer,
               s.hw_model, s.hw_serial, s.inventory_updated_at, s.inventory_collected_at, s.inventory_baselined_at,
               s.boot_time, s.environment, s.fqdn, s.hostname, s.software_truncated
          FROM servers s WHERE s.id = :aid""", {"aid": uid}))[0]
    sw = await _run(db, f"""
        SELECT w.package_name, w.version, w.vendor, w.name_key, w.category, w.architecture, w.source, w.install_date,
               w.first_seen_at
          FROM server_software_inventory w WHERE w.server_id = :aid ORDER BY w.package_name LIMIT {MAX_SERVER_SOFTWARE}""",
        {"aid": uid})
    services = ", ".join(f"'{n}'" for n in ENDPOINT_PROTECTION_SERVICES)           # constants, never input
    defender = await _run(db, f"""
        SELECT v.service_name FROM server_service_inventory v
         WHERE v.server_id = :aid AND v.service_name IN ({services}) AND lower(v.state) = 'running'
         ORDER BY v.service_name""", {"aid": uid})
    # The endpoint-protection answer reads ALL of the server's rows (the list shown above is cut at MAX_SERVER_SOFTWARE).
    endpoint = await _run(db, "SELECT w.package_name FROM server_software_inventory w WHERE w.server_id = :aid "
                          "AND w.category = :cat ORDER BY w.package_name", {"aid": uid, "cat": ENDPOINT_PROTECTION_CATEGORY})
    # Rows waiting for the normaliser, and unkeyed rows it never selects (software_eligible_sql: one rule everywhere), and
    # whether the server ever sent services (Defender can only be seen there).
    stale_rev = await _software_rev_pending(db)
    rev = int(inventory_normalize.RULEPACK_REV)
    eligible = software_eligible_sql("w")
    counts = (await _run(db, f"""
        SELECT count(*) FILTER (WHERE {eligible} AND (w.norm_rev IS NULL{' OR w.norm_rev < :rev' if stale_rev else ''})) AS pending,
               count(*) FILTER (WHERE w.norm_rev IS NULL AND NOT {eligible}) AS stuck,
               (SELECT count(*) FROM server_service_inventory v WHERE v.server_id = :aid) AS services
          FROM server_software_inventory w WHERE w.server_id = :aid""", {"aid": uid, "rev": rev}))[0]
    pending_rows, unkeyable_rows, service_rows = int(counts["pending"]), int(counts["stuck"]), int(counts["services"])
    hidden_set = set(hidden)
    visible = [x for x in sw if (x["category"] or "other") not in hidden_set]
    ranked = sorted(visible, key=lambda x: (x["category"] != ENDPOINT_PROTECTION_CATEGORY, x["category"] != "security",
                                            x["package_name"].lower(), x["package_name"]))
    top = [{
        "package_name": x["package_name"], "product_key": x["name_key"],
        "product": product_display_name(x["name_key"] or x["package_name"], x["package_name"], x["vendor"], x["source"]),
        "version": x["version"], "publisher": x["vendor"], "category": x["category"],
        "architecture": x["architecture"], "source": x["source"],
        "install_date": iso(x["install_date"]), "first_seen_at": iso(x["first_seen_at"]),
    } for x in ranked[:RECENT_CHANGES]]
    baselined = srv["inventory_baselined_at"] is not None
    truncated = bool(srv["software_truncated"])
    state, reason = security_assessment(endpoint_rows=len(endpoint), defender_services=len(defender), baselined=baselined,
                                        truncated=truncated, pending_rows=pending_rows, unkeyable_rows=unkeyable_rows,
                                        service_rows=service_rows)
    disks = _j(srv["physical_disks"])
    body = {
        "os_name": srv["os_name"], "architecture": srv["architecture"], "hostname": srv["hostname"], "fqdn": srv["fqdn"],
        "cpu_model": _clean(srv["cpu_model"]), "cpu_cores": srv["cpu_cores"], "cpu_physical_cores": srv["cpu_physical_cores"],
        "memory_total_bytes": int(srv["memory_total_bytes"]) if srv["memory_total_bytes"] is not None else None,
        "disks": disks if isinstance(disks, list) else [],
        "virtualization": _clean(srv["virtualization"]),
        "bios_version": srv["bios_version"], "bios_date": srv["bios_date"].isoformat() if srv["bios_date"] else None,
        "boot_time": iso(srv["boot_time"]),
        "baselined": baselined,
        "software": {
            "total": len(sw), "shown": len(top), "hidden": len(sw) - len(visible), "truncated": truncated,
            "top": top,
            # state: the same answer as the Assets filter security_agent (present | missing | unknown). present is a
            # bool for the old readers: true / false / null (unknown). reason says why it is unknown; services lists
            # the running Defender services that count as protection when no software row does.
            "security_agent": {
                "present": {"present": True, "missing": False}.get(state),
                "state": state,
                "reason": reason,
                "products": sorted({x["package_name"] for x in endpoint}),
                "services": [r["service_name"] for r in defender],
            },
        },
    }
    raw = {"os_name": srv["os_name"], "os_version": srv["os_version"], "kernel_or_build": srv["kernel_or_build"],
           "hw_manufacturer": srv["hw_manufacturer"], "hw_model": srv["hw_model"], "hw_serial": srv["hw_serial"]}
    times = {"os": srv["inventory_updated_at"], "hw": srv["inventory_updated_at"]}
    return body, {"raw": raw, "times": times}


async def _device_body(db: AsyncSession, uid: uuid.UUID, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    dev = (await _run(db, """
        SELECT d.vendor, d.model, d.os_version, d.firmware_version, d.serial_number, d.sys_object_id,
               left(d.sys_descr, 512) AS sys_descr, d.managed_by_device_id, d.poll_mode, d.snmp_enabled, d.identity_updated_at,
               p.name AS profile, g.name AS group_name
          FROM devices d LEFT JOIN device_profiles p ON p.id = d.profile_id LEFT JOIN device_groups g ON g.id = d.group_id
         WHERE d.id = :aid""", {"aid": uid}))[0]
    ents = await _run(db, f"""
        SELECT e.ent_index, e.class, e.name, e.serial_number, e.model_name, e.hw_revision, e.fw_revision
          FROM device_entities e WHERE e.device_id = :aid AND e.class IN ({_MODULE_CLASS_SQL})
         ORDER BY e.ent_index LIMIT {MAX_MODULE_ROWS + 1}""", {"aid": uid})
    tpl = await _run(db, """
        SELECT t.metric_key, t.instance, t.value_num, t.value_text, t.updated_at FROM device_template_values t
         WHERE t.device_id = :aid AND t.metric_key = ANY(CAST(:keys AS text[]))""",
        {"aid": uid, "keys": list(_HA_KEYS) + list(TEMPLATE_METRIC_KEYS)})
    truncated = len(ents) > MAX_MODULE_ROWS
    groups: dict[tuple[str, Optional[str]], dict[str, Any]] = {}
    for e in ents[:MAX_MODULE_ROWS]:
        model = _clean(e["model_name"])
        g = groups.setdefault((e["class"], model), {
            "class": e["class"], "class_label": MODULE_CLASS_LABELS.get(e["class"], e["class"]), "model": model, "items": []})
        g["items"].append({"ent_index": e["ent_index"], "name": _clean(e["name"]), "serial": _clean(e["serial_number"]),
                           "hw_revision": _clean(e["hw_revision"]), "fw_revision": _clean(e["fw_revision"])})
    order = {c: i for i, c in enumerate(MODULE_CLASSES)}
    modules = sorted(groups.values(), key=lambda g: (order.get(g["class"], 99), g["model"] is None, str(g["model"]).lower()))
    for g in modules:
        g["count"] = len(g["items"])
    ver_times = [t["updated_at"] for t in tpl if t["metric_key"] in _TEMPLATE_VERSION_KEYS]
    ser_times = [t["updated_at"] for t in tpl if t["metric_key"] == "f5_serial"]
    body = {
        "profile": dev["profile"], "group": dev["group_name"], "device_type": None,
        "sys_object_id": dev["sys_object_id"], "controller_managed": dev["managed_by_device_id"] is not None,
        "modules": modules, "modules_truncated": truncated,
        "ha": _ha_block(tpl, now),
    }
    raw = {"vendor": dev["vendor"], "model": dev["model"], "os_version": dev["os_version"],
           "firmware_version": dev["firmware_version"], "serial_number": dev["serial_number"],
           "sys_descr": dev["sys_descr"]}
    times = {"os": dev["identity_updated_at"], "hw": dev["identity_updated_at"],
             "template_os": max(ver_times) if ver_times else None, "template_hw": max(ser_times) if ser_times else None}
    return body, {"raw": raw, "times": times}


async def asset_detail(db: AsyncSession, kind: str, asset_id: str, *, scope: Optional[Sequence[str]],
                       settings: Optional[Mapping[str, Any]] = None, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """The flyout payload for one asset, or None when the id is malformed, unknown, OR outside
    the caller's scope (the caller must answer all three with the same 404: no existence oracle)."""
    ref = parse_asset_ref(kind, asset_id)
    if ref is None:
        return None
    kind, uid = ref
    now = now or utcnow()
    await _tune(db)
    settings = await _settings(db, settings)
    p: dict[str, Any] = {**time_params(settings, now), **_scope_params(scope), "aid": uid}
    union = asset_union_sql(scope, kinds=(kind,))
    rows = await _run(db, f"WITH a AS ({union}) SELECT {_ROW_COLUMNS}, a.linked_device_id FROM a WHERE a.id = :aid", p)
    if not rows:
        return None
    r = rows[0]
    asset = _asset_row(r, now)
    linked = r["linked_device_id"]
    if kind == "server":
        body, ctx = await _server_body(db, uid, settings["hide_categories_by_default"], now)
        server, device = body, None
    else:
        body, ctx = await _device_body(db, uid, now)
        body["device_type"] = r["asset_type"]
        server, device = None, body
    times = dict(ctx["times"])
    # A template-sourced value is as old as the template sample, not as old as the last system poll.
    if kind == "device":
        if r["os_source"] in ("template", "template_stale") and times.get("template_os"):
            times["os"] = times["template_os"]
        if r["hw_source"] == "template" and times.get("template_hw"):
            times["hw"] = times["template_hw"]
    identity = _identity_list(r, times, now)

    pairs = [(kind, uid)] + ([("device", linked)] if kind == "server" and linked else [])
    ep: dict[str, Any] = {}
    conds = _kind_asset_conds(ChangeFilters(), pairs, ep, "e.asset_kind", "e.asset_id")
    recent_rows = await _run(db, _events_sql(None, outer=_EV_COLS, conds=conds, p=ep,
                                             tail="ORDER BY e.observed_at DESC, e.id DESC LIMIT :n"), {**ep, "n": RECENT_CHANGES})
    totals = (await _run(db, _events_sql(None, outer="count(*) AS n, min(e.observed_at) AS first_at", conds=conds, p=ep), ep))[0]
    recent_total = int(totals["n"])
    recent = [_event_row(x) for x in recent_rows]
    await _decorate_events(db, recent)
    return {
        "generated_at": iso(now),
        "asset": asset,
        "identity": identity,
        "raw": ctx["raw"],
        "server": server,
        "device": device,
        # total counts every recorded event of the asset (and of its SNMP twin); first_change_at is the oldest of them
        # (null when there are none), so a link to the Changes page with since=first_change_at lists exactly `total`.
        "changes": {"items": recent, "total": recent_total, "first_change_at": iso(totals["first_at"])},
        "links": {"server_id": str(uid) if kind == "server" else None,
                  "device_id": str(uid) if kind == "device" else (str(linked) if linked else None)},
    }
