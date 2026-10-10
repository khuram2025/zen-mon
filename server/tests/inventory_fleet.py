"""A deterministic seeded fleet for the Inventory read-model tests and the golden fixtures.

``build_fleet(db)`` fills a migrated database on the private cluster (tests/pg_fixture.py, never the
appliance database) with about 60 servers and 90 network devices: FortiOS, PAN-OS, IOS XE, classic IOS,
AOS-CX, Junos, NX-OS and TMOS gear; Windows Server 2016, 2019 and 2022, a few Windows 10 and 11 hosts,
Linux hosts and hosts with no known OS; about 25 software products with a realistic spread of versions
(including a security agent on most, not all, servers); stale, aging and never-reported assets; truncated
software lists; seven days of change events of every category (and a few older ones); five servers that are
also polled over SNMP (so the asset union has to count them once); and a user scoped to one tag.

Everything is deterministic: ids are uuid5 of the asset name, every timestamp is an offset from the pinned
``FIXED_NOW``, and identity rows are produced by the real normaliser. Run the query layer with
``iq.utcnow`` patched to ``FIXED_NOW`` (the ``pinned_clock`` fixture) and the same inputs always give the
same JSON, which is what the golden files rely on.

Used as::

    from inventory_fleet import fleet_db, fleet_info, pinned_clock   # noqa: F401  (fixtures)
    from pg_fixture import pg_cluster                                # noqa: F401

``fleet_db`` is one copy of the fleet per test module (read-only tests); ``fleet_db_fresh`` is a private copy per
test for tests that change data. ``api_client(db, role=..., permissions=[...], scope_tags=...)`` is a TestClient
without lifespan whose requests run against that database as a user of that role: the role row is created in the
database and the real ``roles`` lookup is used (see ``ensure_role``), so the permission chain and the tag scope are
the production ones. Two clients cannot be open at once (they share ``app.dependency_overrides``).
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator, Optional

import pytest

from app.services import inventory_normalize as norm
from app.services import inventory_query as iq

UTC = timezone.utc
FIXED_NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
SCOPED_TAG = "team-edge"
SCOPED_USERNAME = "edge.operator"
_NS = uuid.UUID("6f0d0c58-1a5e-4a52-9c1e-0a1b2c3d4e5f")


def aid(kind: str, name: str) -> uuid.UUID:
    """The id of a fleet asset: stable across runs and machines."""
    return uuid.uuid5(_NS, f"{kind}:{name}")


def ago(**kw) -> datetime:
    return FIXED_NOW - timedelta(**kw)


# ── Servers ──────────────────────────────────────────────────────────

_OS = {
    # key: (os_name, os_version, builds, architecture, os_type, mode)
    "w2022": ("Microsoft Windows Server 2022 Standard", "21H2",
              ["10.0.20348.587 Build 20348.587", "10.0.20348.2402 Build 20348.2402", "10.0.20348.2402 Build 20348.2402",
               "10.0.20348.2700 Build 20348.2700", "10.0.20348.2700 Build 20348.2700", "10.0.20348.2700 Build 20348.2700"],
              "x86_64", "windows", "agent"),
    "w2019": ("Microsoft Windows Server 2019 Standard", "1809",
              ["10.0.17763.5696 Build 17763.5696", "10.0.17763.6189 Build 17763.6189", "10.0.17763.6189 Build 17763.6189",
               "10.0.17763.6293 Build 17763.6293"], "x86_64", "windows", "agent"),
    "w2016": ("Microsoft Windows Server 2016 Standard", "1607",
              ["10.0.14393.6709 Build 14393.6709", "10.0.14393.7159 Build 14393.7159"], "x86_64", "windows", "agent"),
    "w10": ("Microsoft Windows 10 Pro", "22H2", ["10.0.19045.4529 Build 19045.4529"], "AMD64", "windows", "agent"),
    "w11": ("Microsoft Windows 11 Pro", "23H2", ["10.0.22631.4317 Build 22631.4317"], "AMD64", "windows", "agent"),
    "ubuntu": ("Ubuntu 22.04.4 LTS", "22.04", ["5.15.0-107-generic", "5.15.0-113-generic"], "x86_64", "linux", "ssh"),
    "rhel": ("Red Hat Enterprise Linux 9.4 (Plow)", "9.4", ["5.14.0-427.el9"], "x86_64", "linux", "ssh"),
    "unknown": (None, None, [None], None, "unknown", "none"),
}

# (name prefix, count, os key)
_SERVER_PLAN = (
    ("srv-w22", 18, "w2022"), ("srv-w19", 14, "w2019"), ("srv-w16", 9, "w2016"), ("ws-w10", 3, "w10"),
    ("ws-w11", 2, "w11"), ("srv-ub", 4, "ubuntu"), ("srv-rh", 2, "rhel"), ("srv-unk", 4, "unknown"),
    ("srv-new", 4, "w2022"),
)
_CPUS = ("Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz", "Intel(R) Xeon(R) Silver 4314 CPU @ 2.40GHz",
         "AMD EPYC 7543 32-Core Processor")
_MEM_GIB = (8, 16, 16, 32, 32, 32, 64, 64, 128, 256)
_SITES = ("HQ", "DR", "Branch-1")
EDGE_SERVERS = ("srv-w22-04", "srv-w19-03", "srv-w16-02", "srv-ub-02", "ws-w10-01", "srv-w22-11")
LINKED_SERVERS = ("srv-w22-02", "srv-w22-09", "srv-w19-03", "srv-w19-08", "srv-w16-05")
UNTAGGED_SERVERS = ("srv-w22-06", "srv-w19-05", "srv-w16-07", "srv-unk-03")
STALE_SERVERS = ("srv-w22-12", "srv-w19-10", "srv-w16-03", "srv-ub-03")
AGING_SERVERS = ("srv-w22-05", "srv-w22-14", "srv-w19-07", "srv-w16-08", "srv-rh-02")
TRUNCATED_SERVERS = ("srv-w22-08", "srv-w19-12")
NO_SECURITY_SERVERS = ("srv-w22-03", "srv-w22-13", "srv-w19-04", "srv-w19-11", "srv-w16-04", "srv-w16-09", "ws-w10-02")
# Endpoint protection (X1). Of the servers above: three carry only the ZenPlus Agent's own entry (not protection), two
# have no antivirus title but a running Microsoft Defender service (protected), one has a Defender for Endpoint sensor
# service that is stopped (not protection), the rest have nothing. The FortiClient-only servers (category 'security',
# a VPN client) are not protected either, and the two truncated servers lack the title because their list is cut.
ZENPLUS_ONLY_SERVERS = ("srv-w22-03", "srv-w22-13", "srv-w19-04")
# X9: a server that uploads software but never a service list (the APM-only agent profile) cannot be judged for built-in
# Defender, which shows only as a running service: endpoint protection 'unknown', reason 'no_service_list'. Every other
# baselined server below carries a service list (W32Time or cron), as the default agent profiles upload one with the software.
NO_SERVICE_LIST_SERVERS = ("srv-w16-09",)
DEFENDER_SERVICE_SERVERS = ("srv-w19-11", "ws-w10-02")
SENSE_STOPPED_SERVERS = ("srv-w16-04",)
# Servers in a collection mode that has no inventory upload and that never reported: freshness 'not_applicable'.
NOT_APPLICABLE_SERVERS = ("srv-unk-02", "srv-unk-04")
CONTROLLER_MANAGED_DEVICES_IDX = (13, 14)                # fg-13 and fg-14
CONTROLLER_MANAGED_DEVICES = ("fg-13", "fg-14")
CAPTURE_STARTED = FIXED_NOW - timedelta(days=45)        # system_settings 'inventory.capture_started_at'


def _server_specs() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    idx = 0
    for prefix, count, osk in _SERVER_PLAN:
        os_name, os_version, builds, arch, os_type, mode = _OS[osk]
        for n in range(1, count + 1):
            name = f"{prefix}-{n:02d}"
            never = prefix == "srv-new"
            tags: list[str] = []
            if name not in UNTAGGED_SERVERS:
                tags.append("prod" if idx % 3 != 2 else "dev")
                if idx % 5 == 0:
                    tags.append("pci")
                tags.append("team-a" if idx % 4 in (0, 1) else "team-b")
                if name in EDGE_SERVERS:
                    tags = [t for t in tags if not t.startswith("team-")] + [SCOPED_TAG]
            if never:
                updated = None
            elif name in STALE_SERVERS:
                updated = ago(days=9 + idx % 7, hours=idx % 5)
            elif name in AGING_SERVERS:
                updated = ago(hours=30 + (idx * 7) % 90)
            else:
                updated = ago(hours=1 + (idx * 37) % 20, minutes=(idx * 13) % 60)
            if osk == "unknown":
                updated = None if name in NOT_APPLICABLE_SERVERS else ago(hours=3 + idx % 9)
            if never:      # registered, never reported: the agent has told us nothing yet
                os_name = os_version = arch = None
                os_type, builds = "unknown", [None]
            specs.append({
                "name": name, "idx": idx, "os_key": osk, "os_name": os_name, "os_version": os_version,
                "kernel": builds[(idx * 5) % len(builds)], "arch": arch, "os_type": os_type, "mode": mode,
                "tags": tags, "site": None if idx % 11 == 4 else _SITES[idx % 3], "updated": updated,
                "never": never, "linked": name in LINKED_SERVERS,
                "truncated": name in TRUNCATED_SERVERS,
                "cpu": None if osk == "unknown" or never else _CPUS[idx % 3],
                "mem_gib": None if osk == "unknown" or never else _MEM_GIB[idx % len(_MEM_GIB)],
                "disks": 0 if osk == "unknown" or never else 1 + idx % 6,
                "virt": ("vmware" if idx % 3 == 0 else "hyper-v" if idx % 7 == 1 else "physical" if idx % 9 == 2 else None)
                        if osk != "unknown" and not never else None,
                "ip": f"10.10.{idx // 200}.{idx % 200 + 10}",
            })
            idx += 1
    return specs


# ── Software ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Product:
    title: str            # may contain {v}: titles that embed their version (Java, 7-Zip, VC++)
    vendor: str
    source: str
    versions: tuple[str, ...]
    weight: int           # percent of Windows servers that carry it
    # DisplayVersion differs from the title version for some products
    title_versions: Optional[tuple[str, ...]] = None


_PRODUCTS: tuple[Product, ...] = (
    Product("Google Chrome", "Google LLC", "hklm", ("150.0.7871.50", "151.0.7922.110", "151.0.7922.110", "152.0.8001.12", "152.0.8001.12", "152.0.8001.12"), 90),
    Product("Microsoft Edge", "Microsoft Corporation", "hklm", ("151.0.3200.62", "152.0.3250.20", "152.0.3250.20"), 88),
    Product("Mozilla Firefox ESR (x64 en-US)", "Mozilla", "hklm", ("128.3.1", "128.4.0", "140.1.0"), 35),
    Product("7-Zip {t} (x64)", "Igor Pavlov", "hklm", ("22.01", "23.01", "24.05", "24.05", "24.08"), 72,
            ("22.01", "23.01", "24.05", "24.05", "24.08")),
    Product("Notepad++ (64-bit x64)", "Notepad++ Team", "hklm", ("8.5.8", "8.6.9", "8.7.1", "8.7.1"), 40),
    Product("Visual Studio Code", "Microsoft Corporation", "hklm", ("1.93.1", "1.94.2", "1.94.2", "1.95.0"), 25),
    Product("Git", "The Git Development Community", "hklm", ("2.43.0", "2.46.2", "2.46.2", "2.47.0"), 30),
    Product("PuTTY release {t} (64-bit)", "Simon Tatham", "hklm", ("0.78", "0.80", "0.81", "0.81"), 45,
            ("0.78", "0.80", "0.81", "0.81")),
    Product("WinSCP {t}", "Martin Prikryl", "hklm", ("6.1.2", "6.3.3", "6.3.5"), 20, ("6.1.2", "6.3.3", "6.3.5")),
    Product("VLC media player", "VideoLAN", "hklm", ("3.0.20", "3.0.21"), 12),
    Product("Adobe Acrobat Reader DC", "Adobe", "hklm", ("24.002.20933", "24.004.20220", "24.004.20220"), 38),
    Product("Zoom", "Zoom Video Communications, Inc.", "hklm", ("6.0.11", "6.1.6", "6.2.0"), 15),
    Product("Python {t} (64-bit)", "Python Software Foundation", "hklm", ("3.11.9", "3.12.4", "3.12.6"), 22,
            ("3.11.9", "3.12.4", "3.12.6")),
    Product("Wireshark {t} x64", "The Wireshark developer community", "hklm", ("4.2.0", "4.2.5", "4.4.0"), 14,
            ("4.2.0", "4.2.5", "4.4.0")),
    Product("Java {t}", "Oracle Corporation", "hklm", ("8.0.3910.11", "8.0.4010.10", "8.0.4010.10", "8.0.4210.9"), 55,
            ("8 Update 391", "8 Update 401", "8 Update 401", "8 Update 421")),
    Product("Microsoft Visual C++ 2015-2022 Redistributable (x64) - {t}", "Microsoft Corporation", "hklm",
            ("14.36.32532", "14.38.33135", "14.40.33810"), 96, ("14.36.32532", "14.38.33135", "14.40.33810")),
    Product("Microsoft Visual C++ 2013 Redistributable (x86) - 12.0.40664", "Microsoft Corporation", "hklm", ("12.0.40664",), 60),
    Product("Microsoft .NET Runtime - {t} (x64)", "Microsoft Corporation", "hklm", ("6.0.31", "8.0.4", "8.0.8"), 52,
            ("6.0.31", "8.0.4", "8.0.8")),
    Product("Microsoft SQL Server 2019 (64-bit)", "Microsoft Corporation", "hklm", ("15.0.4375.4", "15.0.4385.2"), 10),
    Product("VMware Tools", "VMware, Inc.", "hklm", ("12.3.5.22544099", "12.4.5.23787635"), 33),
    Product("TeamViewer", "TeamViewer", "hklm", ("15.51.5", "15.55.3"), 8),
    Product("Veeam Backup & Replication", "Veeam Software Group GmbH", "hklm", ("12.1.2.172",), 6),
    Product("Splunk Universal Forwarder", "Splunk, Inc.", "hklm", ("9.1.2", "9.2.1", "9.2.1"), 18),
)
_SECURITY = (
    Product("CrowdStrike Falcon Sensor", "CrowdStrike, Inc.", "hklm", ("7.04.17605", "7.15.18513", "7.15.18513", "7.18.19103"), 100),
    Product("Microsoft Defender for Endpoint", "Microsoft Corporation", "hklm", ("10.8210.20348.2402",), 100),
    Product("FortiClient", "Fortinet Technologies Inc", "hklm", ("7.2.4.0972", "7.4.0.1658"), 100),
)
_NOISE = (
    ("Security Update for Microsoft Windows (KB5034439)", "Microsoft Corporation", "1.0"),
    ("Security Update for Microsoft Windows (KB5036896)", "Microsoft Corporation", "1.0"),
    ("Hotfix for Microsoft Windows (KB5003791)", "Microsoft", "1.0"),
    ("Intel(R) Chipset Device Software", "Intel Corporation", "10.1.19444.8378"),
    ("Microsoft Windows Language Pack - English", "Microsoft", "10.0.20348.1"),
)
_LINUX_PRODUCTS = (
    Product("git", "Ubuntu Developers", "dpkg", ("1:2.34.1-1ubuntu1.10", "1:2.34.1-1ubuntu1.11"), 100),
    Product("openssh-server", "Ubuntu Developers", "dpkg", ("1:8.9p1-3ubuntu0.7", "1:8.9p1-3ubuntu0.10"), 100),
)


def _title(p: Product, v_idx: int) -> tuple[str, str]:
    """(package title, DisplayVersion) of product ``p`` at version index ``v_idx``."""
    version = p.versions[v_idx % len(p.versions)]
    if "{t}" in p.title or "{v}" in p.title:
        tv = (p.title_versions or p.versions)[v_idx % len(p.versions)]
        return p.title.replace("{t}", tv).replace("{v}", tv), version
    return p.title, version


def _spread(idx: int, j: int) -> int:
    """A well-mixed non-negative integer for (server, product): ``% n`` is then an even-ish pick among n versions.
    (``idx * 3 + j`` was not: modulo 3 it is always ``j``, so every 3-version product sat on one version.)"""
    return ((idx * 2654435761 + j * 40503 + 12345) % 2**32) >> 7


def software_rows(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The software rows of one server spec, keyed the way ingest keys them (the real normaliser)."""
    if spec["never"] or spec["os_type"] == "unknown":
        return []
    rows: list[dict[str, Any]] = []
    idx = spec["idx"]
    if spec["os_type"] == "linux":
        for j, p in enumerate(_LINUX_PRODUCTS):
            title, version = _title(p, idx + j)
            rows.append(_row(spec, title, version, p.vendor, "dpkg", 0))
        return rows
    for j, p in enumerate(_PRODUCTS):
        if ((idx * 7 + j * 13 + 3) % 100) >= p.weight:
            continue
        title, version = _title(p, _spread(idx, j))
        rows.append(_row(spec, title, version, p.vendor, p.source, j))
    if spec["name"] not in NO_SECURITY_SERVERS:
        # CrowdStrike on most, Defender or FortiClient on a few: "present on most, not all".
        k = idx % 10
        sec = _SECURITY[0] if k < 7 else _SECURITY[1] if k < 9 else _SECURITY[2]
        title, version = _title(sec, _spread(idx, 99))
        rows.append(_row(spec, title, version, sec.vendor, sec.source, 99))
    if spec["name"] in ZENPLUS_ONLY_SERVERS:
        rows.append(_row(spec, "ZenPlus Agent", "1.23.35", "Zentryc LLC", "hklm", 98))
    for n, (title, vendor, version) in enumerate(_NOISE):
        if (idx + n) % 3 != 0:
            rows.append(_row(spec, title, version, vendor, "hklm", 200 + n))
    if spec["truncated"]:
        rows = rows[:24]
        # The agent cuts its alphabetically sorted list: Sophos, Symantec, Trend Micro, Windows Defender and the like
        # sort late, so the protection is exactly what a cut list loses.
        rows = [r for r in rows if r["category"] != "endpoint-protection"]
    # one row per (server, title): keep the first, as ingest's same-name dedupe does for equal titles
    seen, out = set(), []
    for r in rows:
        if r["package_name"] not in seen:
            seen.add(r["package_name"])
            out.append(r)
    return out


def _row(spec: dict[str, Any], title: str, version: str, vendor: str, source: str, j: int) -> dict[str, Any]:
    key = norm.normalize_software(title, vendor, version, source=source)
    updated = spec["updated"] or FIXED_NOW
    return {
        "server_id": str(aid("server", spec["name"])), "package_name": title, "version": version, "vendor": vendor,
        "install_date": iso_dt(ago(days=20 + (spec["idx"] * 11 + j * 5) % 700)),
        "updated_at": iso_dt(updated), "first_seen_at": iso_dt(ago(days=30 + (spec["idx"] + j) % 300)),
        "architecture": "x64" if source == "hklm" else None, "source": source,
        "name_key": key.name_key, "publisher_key": key.publisher_key, "category": key.category,
        "norm_rev": norm.RULEPACK_REV,
    }


def iso_dt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


# ── Network devices ──────────────────────────────────────────────────

_FG_VERSIONS = ("v7.2.5,build1517,230606 (GA.F)", "v7.2.8,build1639,240614 (GA.M)", "v7.2.8,build1639,240614 (GA.M)",
                "v7.4.4,build2662,240614 (GA.M)", "v7.4.4,build2662,240614 (GA.M)", "v6.4.14,build2093,231206 (GA)")
_PA_VERSIONS = ("10.2.3-h4", "10.2.7-h3", "10.2.7-h3", "11.1.2-h3", "11.1.4-h7", "11.1.4-h7")
_XE_VERSIONS = ("17.06.04", "17.09.04a", "17.09.04a", "17.09.04a", "17.12.03", "17.03.05")
_IOS_VERSIONS = ("15.2(7)E8", "15.2(7)E8", "15.2(4)E10", "15.2(4)E10", "15.2(7)E10")
_CX_VERSIONS = ("10.10.1100", "10.13.1000", "10.13.1000", "10.13.1010", "10.12.1010")
_JN_VERSIONS = ("21.4R3-S5.4", "21.4R3-S5.4", "22.2R3-S3.4", "20.4R3-S9.2", "21.4R3-S7.3")
_NX_VERSIONS = ("9.3(10)", "9.3(12)", "10.2(5)")
_F5_VERSIONS = ("15.1.10.4", "17.1.1.3", "17.1.1.3", "16.1.4.1")

_GROUPS = ("Firewalls", "Switching", "Load balancing")
_PROFILES = ("FortiGate", "Palo Alto PAN-OS", "Cisco IOS-XE", "Cisco IOS", "Aruba AOS-CX", "Juniper Junos", "Cisco NX-OS",
             "F5 BIG-IP", "Generic SNMP")
EDGE_DEVICES = ("fg-03", "fg-04", "pa-02", "sw-c9-05", "sw-c9-06", "sw-cx-02", "sw-jn-03", "sw-misc-02", "sw-c9-11")
UNTAGGED_DEVICES = ("sw-misc-04", "sw-c29-03", "lb-04")


def _device_specs() -> list[dict[str, Any]]:
    """(name, kind of gear, raw columns) for every device, including the five SNMP twins of servers."""
    out: list[dict[str, Any]] = []

    def add(name: str, group: str, profile: str, **raw: Any) -> None:
        out.append({"name": name, "group": group, "profile": profile, **raw})

    for i in range(1, 15):
        model = ("FortiGate-60F", "FortiGate-100F", "FortiGate-200F", "FortiGate-600E")[i % 4]
        ver = _FG_VERSIONS[i % len(_FG_VERSIONS)]
        add(f"fg-{i:02d}", "Firewalls", "FortiGate", vendor="Fortinet", model=model, os_version=None, firmware_version=None,
            serial=f"FG{model.split('-')[1]}TK2000{i:04d}", oid="1.3.6.1.4.1.12356.101.1.1000",
            descr=f"{model} {ver}", template={"fgt_fw_version": ver}, kind="fortigate",
            # fg-13 and fg-14 stand for gear managed through a controller: its identity arrives from the controller
            # sync, which stamps nothing, so they cannot report on their own (freshness 'not_applicable').
            parent="fg-01" if i in CONTROLLER_MANAGED_DEVICES_IDX else None)
    for i in range(1, 9):
        model = ("PA-440", "PA-3220", "PA-5220", "PA-3220")[i % 4]
        ver = _PA_VERSIONS[i % len(_PA_VERSIONS)]
        add(f"pa-{i:02d}", "Firewalls", "Palo Alto PAN-OS", vendor="Palo Alto Networks", model=model, os_version=ver,
            firmware_version=None, serial=f"0151010{i:05d}", oid="1.3.6.1.4.1.25461.2.3.34",
            descr=f"Palo Alto Networks {model} series firewall", template={"pan_sw_version": ver}, kind="palo")
    for i in range(1, 19):
        model = ("C9300-48P", "C9300-24T", "C9200L-24P-4G")[i % 3]
        ver = _XE_VERSIONS[i % len(_XE_VERSIONS)]
        add(f"sw-c9-{i:02d}", "Switching", "Cisco IOS-XE", vendor="Cisco", model=model, os_version=ver, firmware_version=ver,
            serial=f"FCW2312L{i:03d}", oid="1.3.6.1.4.1.9.1.2530",
            descr=f"Cisco IOS Software [Cupertino], Catalyst L3 Switch Software (CAT9K_IOSXE), Version {ver}, RELEASE SOFTWARE (fc3)",
            template={}, kind="iosxe")
    for i in range(1, 7):
        ver = _IOS_VERSIONS[i % len(_IOS_VERSIONS)]
        add(f"sw-c29-{i:02d}", "Switching", "Cisco IOS", vendor="Cisco", model="WS-C2960X-48TS-L", os_version=ver,
            firmware_version=None, serial=f"FOC1234X{i:03d}", oid="1.3.6.1.4.1.9.1.1208",
            descr=f"Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version {ver}, RELEASE SOFTWARE (fc3)",
            template={}, kind="ios")
    for i in range(1, 11):
        model = ("6300M", "6200F", "6300M")[i % 3]
        ver = _CX_VERSIONS[i % len(_CX_VERSIONS)]
        add(f"sw-cx-{i:02d}", "Switching", "Aruba AOS-CX", vendor="HPE Aruba", model=model, os_version=ver, firmware_version=None,
            serial=f"SG12{i:06d}", oid="1.3.6.1.4.1.47196.4.1.1.1.1",
            descr=f"Aruba JL658A {model} 24SFP+ 4SFP56 Swch FL.{ver}", template={}, kind="aoscx")
    for i in range(1, 9):
        model = ("EX4300-48T", "QFX5120-48Y", "EX4300-48T", "EX2300-24T")[i % 4]
        ver = _JN_VERSIONS[i % len(_JN_VERSIONS)]
        add(f"sw-jn-{i:02d}", "Switching", "Juniper Junos", vendor="Juniper Networks", model=model, os_version=ver,
            firmware_version=None, serial=f"PE37{i:08d}", oid="1.3.6.1.4.1.2636.1.1.1.2.114",
            descr=f"Juniper Networks, Inc. {model.lower()} Ethernet Switch, kernel JUNOS {ver}", template={}, kind="junos")
    for i in range(1, 5):
        ver = _NX_VERSIONS[i % len(_NX_VERSIONS)]
        add(f"sw-nx-{i:02d}", "Switching", "Cisco NX-OS", vendor="Cisco", model="N9K-C93180YC-FX", os_version=ver,
            firmware_version=None, serial=f"FDO2234{i:04d}", oid="1.3.6.1.4.1.9.12.3.1.3.1",
            descr=f"Cisco NX-OS(tm) nxos.{ver}.bin", template={}, kind="nxos")
    for i in range(1, 5):
        ver = _F5_VERSIONS[i % len(_F5_VERSIONS)]
        add(f"lb-{i:02d}", "Load balancing", "F5 BIG-IP", vendor="F5 Networks", model="BIG-IP i2800", os_version=None,
            firmware_version=None, serial=f"f5-bf{i:02d}-abcd", oid="1.3.6.1.4.1.3375.2.1.3.4.1",
            descr="Linux bigip 3.10.0-862.14.4.el7.ve.x86_64 #1 SMP", template={"f5_version": ver}, kind="f5")
    # A mixed bag: another vendor, a ping-only device, and devices that tell us nothing.
    for i in range(1, 6):
        add(f"sw-misc-{i:02d}", "Switching", "Generic SNMP", vendor="Ubiquiti", model=f"USW-24-PoE", os_version="6.5.59",
            firmware_version=None, serial=f"U7{i:08d}", oid="1.3.6.1.4.1.41112.1.6", descr="USW-24-PoE, 6.5.59",
            template={}, kind="other")
    for i in range(1, 9):
        add(f"edge-unk-{i:02d}", "Switching", "Generic SNMP", vendor=None, model=None, os_version=None, firmware_version=None,
            serial=None, oid=None, descr=None, template={}, kind="empty", ping_only=(i % 2 == 0))
    # SNMP twins of five servers: the same machines, polled over SNMP too.
    for n, srv in enumerate(LINKED_SERVERS, start=1):
        add(f"twin-{srv}", "Switching", "Generic SNMP", vendor="Dell", model="PowerEdge R650", os_version=None,
            firmware_version=None, serial=f"DELL{n:05d}", oid="1.3.6.1.4.1.674.10892.5", descr="Dell PowerEdge R650",
            template={}, kind="twin", twin_of=srv)
    return out


def _apply_device_variation(specs: list[dict[str, Any]]) -> None:
    """Gaps and ages: some devices lack a model or serial, some have never been polled, some are stale."""
    for i, d in enumerate(specs):
        d["idx"] = i
        d["tags"] = []
        d.setdefault("ping_only", False)
        if d["kind"] in ("twin",):
            d["updated"] = ago(minutes=30 + i)
            d["status"] = "up"
            d["ip"] = f"10.60.1.{i}"
            # One twin carries the scoped tag although its server does not (it must stay invisible: the server owns it).
            d["tags"] = [SCOPED_TAG] if d["twin_of"] == "srv-w22-09" else []
            continue
        if d["kind"] == "empty":
            d["updated"] = None
            d["status"] = "unknown" if i % 3 == 0 else "up"
        elif i % 23 == 7:
            d["updated"] = None
            for k in ("vendor", "model", "os_version", "firmware_version", "serial", "oid", "descr"):
                d[k] = None
            d["template"] = {}
            d["status"] = "down"
        elif i % 19 == 3:
            d["updated"] = ago(days=8 + i % 6)
            d["status"] = "down"
        elif i % 8 == 6:
            d["updated"] = ago(hours=3 + (i * 5) % 70)
            d["status"] = "up"
        else:
            d["updated"] = ago(minutes=4 + (i * 7) % 100)
            d["status"] = "up"
        if d["updated"] is not None:
            if i % 17 == 5:
                d["model"] = None
            if i % 9 == 4:
                d["serial"] = None
        d["ip"] = f"10.{20 + i // 250}.{(i // 25) % 10}.{i % 25 + 10}"
        if d["name"] not in UNTAGGED_DEVICES:
            tags = ["prod" if i % 4 else "dev", "dc-east" if i % 2 else "dc-west"]
            if d["kind"] in ("fortigate", "palo"):
                tags.append("pci")
            tags.append("team-net" if i % 3 else "team-a")
            if d["name"] in EDGE_DEVICES:
                tags = [t for t in tags if t != "team-net" and t != "team-a"] + [SCOPED_TAG]
            d["tags"] = tags
        if d.get("parent"):                            # managed through a controller: last_seen is recent, nothing stamps identity
            d["updated"] = ago(minutes=3 + i)
            d["status"] = "up"
            d["identity_at"] = None
            d["ping_only"] = True


# ── Change events ────────────────────────────────────────────────────

def _events(servers: list[dict[str, Any]], devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Seven days of events of every category, and a few older ones. ``at`` is a timedelta before FIXED_NOW."""
    ev: list[dict[str, Any]] = []
    sv = {s["name"]: s for s in servers}
    dv = {d["name"]: d for d in devices}

    def add(at: timedelta, kind: str, name: str, category: str, action: str, subject: str, key: Optional[str] = None,
            old: Optional[str] = None, new: Optional[str] = None, detail: Optional[dict] = None,
            asset_id: Optional[uuid.UUID] = None) -> None:
        ev.append({"at": FIXED_NOW - at, "kind": kind, "id": str(asset_id or aid(kind, name)), "name": name,
                   "category": category, "action": action, "subject": subject, "key": key, "old": old, "new": new,
                   "detail": detail or {}})

    chrome = ("Google Chrome", "google-chrome", {"vendor": "Google LLC", "architecture": "x64", "source": "hklm"})
    fresh_servers = [s["name"] for s in servers if s["updated"] and not s["never"] and s["os_type"] == "windows"
                     and s["name"] not in STALE_SERVERS and s["name"] not in TRUNCATED_SERVERS]
    # Chrome upgrades on twelve servers over four days, one downgrade, one pair we cannot order.
    for n, name in enumerate(fresh_servers[:12]):
        add(timedelta(hours=3 + n * 7, minutes=n * 3, seconds=n), "server", name, "software", "changed", chrome[0], chrome[1],
            "151.0.7922.110", "152.0.8001.12", chrome[2])
    add(timedelta(hours=40, minutes=12), "server", fresh_servers[12], "software", "changed", chrome[0], chrome[1],
        "152.0.8001.12", "151.0.7922.110", chrome[2])
    add(timedelta(hours=55, minutes=1), "server", fresh_servers[13], "software", "changed", "Notepad++ (64-bit x64)", "notepad-plus-plus",
        "nightly-build", "release-candidate", {"source": "hklm"})
    # 7-Zip and Java embed their version in the title: an upgrade is a removal plus an installation at one instant.
    for n, name in enumerate(fresh_servers[14:20]):
        at = timedelta(hours=10 + n * 9, minutes=17 + n, seconds=3 * n)
        add(at, "server", name, "software", "removed", "Java 8 Update 391", "java-se-runtime", "8.0.3910.11", None,
            {"vendor": "Oracle Corporation", "architecture": "x64", "source": "hklm"})
        add(at, "server", name, "software", "added", "Java 8 Update 401", "java-se-runtime", None, "8.0.4010.10",
            {"vendor": "Oracle Corporation", "architecture": "x64", "source": "hklm"})
    at = timedelta(hours=70, minutes=5)
    add(at, "server", fresh_servers[20], "software", "removed", "7-Zip 24.05 (x64)", "7-zip", "24.05", None, {"source": "hklm"})
    add(at, "server", fresh_servers[20], "software", "added", "7-Zip 23.01 (x64)", "7-zip", None, "23.01", {"source": "hklm"})
    # Plain installs and removals.
    for n, name in enumerate(fresh_servers[21:24]):
        add(timedelta(hours=20 + n * 11, minutes=40), "server", name, "software", "added", "TeamViewer", "teamviewer",
            None, "15.55.3", {"vendor": "TeamViewer", "architecture": "x64", "source": "hklm"})
    for n, name in enumerate(fresh_servers[24:26]):
        add(timedelta(hours=48 + n * 6, minutes=20), "server", name, "software", "removed", "AnyDesk", "anydesk",
            "8.0.8", None, {"vendor": "philandro Software GmbH", "architecture": "x64", "source": "hklm"})
    add(timedelta(hours=6, minutes=45), "server", fresh_servers[26], "software", "added", "CrowdStrike Falcon Sensor",
        "crowdstrike-falcon-sensor", None, "7.15.18513", {"vendor": "CrowdStrike, Inc.", "architecture": "x64", "source": "hklm"})
    # A list cut at the agent's cap, and a server over its change budget.
    for n, name in enumerate(TRUNCATED_SERVERS):
        add(timedelta(hours=12 + n * 20, minutes=2), "server", name, "software", "removed", "Wireshark 4.2.0 x64", "wireshark",
            "4.2.0", None, {"snapshot_truncated": True, "source": "hklm"})
    add(timedelta(hours=30, minutes=30), "server", fresh_servers[27], "software", "changed", "(change budget exceeded)", None,
        None, None, {"budget_exceeded": True, "limit": 2000})
    # Operating system builds.
    for n, name in enumerate(("srv-w22-01", "srv-w22-07", "srv-w19-01", "srv-w16-01")):
        osk = sv[name]["os_key"]
        old, new, rel = (("10.0.20348.2402", "10.0.20348.2700", "2022") if osk == "w2022" else
                         ("10.0.17763.6189", "10.0.17763.6293", "2019") if osk == "w2019" else
                         ("10.0.14393.6709", "10.0.14393.7159", "2016"))
        add(timedelta(hours=26 + n * 17, minutes=8), "server", name, "os", "changed", "Windows Server", None, old, new,
            {"os_family": "windows", "os_release": rel, "os_source": "agent"})
    # Firmware.
    fw = (("fg-01", "FortiOS", "7.2.5", "7.2.8", {"os_family": "fortios", "os_release": "7.2", "os_source": "template"}, 5),
          ("fg-05", "FortiOS", "7.2.5", "7.2.8", {"os_family": "fortios", "os_release": "7.2", "os_source": "template"}, 15),
          ("fg-06", "FortiOS", "7.4.1", "7.2.9", {"os_family": "fortios", "os_release": "7.2", "os_source": "template"}, 31),
          ("fg-09", "FortiOS", "7.2.8", "7.4.4", {"os_family": "fortios", "os_release": "7.4", "os_source": "template"}, 49),
          ("pa-02", "PAN-OS", "10.2.3-h4", "10.2.7-h3", {"os_family": "panos", "os_release": "10.2", "os_source": "template"}, 8),
          ("sw-c9-04", "IOS XE", "17.6.4", "17.9.4a", {"os_family": "ios-xe", "os_release": "17.9", "os_source": "profile"}, 61),
          ("sw-jn-01", "Junos OS", "20.4R3-S9.2", "21.4R3-S5.4", {"os_family": "junos", "os_release": "21.4", "os_source": "profile"}, 90),
          ("sw-cx-03", "AOS-CX", "10.10.1100", "10.13.1000", {"os_family": "aos-cx", "os_release": "10.13", "os_source": "profile"}, 120))
    for name, subject, old, new, detail, hrs in fw:
        add(timedelta(hours=hrs, minutes=33), "device", name, "firmware", "changed", subject, None, old, new, detail)
    # The SNMP twin of an edge server follows its server; the edge-tagged twin of a non-edge server does not.
    add(timedelta(hours=14, minutes=10), "device", f"twin-{LINKED_SERVERS[2]}", "firmware", "changed", "os_version", None,
        "1.0.0", "1.1.0", {"os_source": "profile"})
    add(timedelta(hours=16, minutes=20), "device", f"twin-{LINKED_SERVERS[1]}", "firmware", "changed", "os_version", None,
        "2.0.0", "2.1.0", {"os_source": "profile"})
    # Hardware: an RMA (new serial) and a chassis swap (new model).
    add(timedelta(hours=33, minutes=3), "device", "sw-c9-02", "hardware", "changed", "hw_serial", None, "FCW2312L002", "FCW2399X777",
        {"hw_vendor": "Cisco", "hw_model": "C9300-24T", "hw_source": "device"})
    add(timedelta(hours=100, minutes=44), "device", "pa-03", "hardware", "changed", "hw_model", None, "PA-3220", "PA-5220",
        {"hw_vendor": "Palo Alto Networks", "hw_source": "device"})
    # Older history (window tests) and an asset that no longer exists.
    add(timedelta(days=10, hours=2), "server", "srv-w22-01", "software", "added", "Zoom", "zoom", None, "6.1.6",
        {"vendor": "Zoom Video Communications, Inc.", "architecture": "x64", "source": "hklm"})
    add(timedelta(days=40), "device", "fg-02", "firmware", "changed", "FortiOS", None, "7.2.2", "7.2.5",
        {"os_family": "fortios", "os_release": "7.2", "os_source": "template"})
    add(timedelta(days=3, hours=5), "server", "srv-retired-01", "software", "removed", "Git", "git", "2.43.0", None,
        {"source": "hklm"}, aid("server", "srv-retired-01"))
    # Events of the scoped team's assets (so its feed is not empty), including a twin's firmware change.
    add(timedelta(hours=9, minutes=30), "server", EDGE_SERVERS[0], "software", "added", "Wireshark 4.4.0 x64", "wireshark",
        None, "4.4.0", {"vendor": "The Wireshark developer community", "architecture": "x64", "source": "hklm"})
    add(timedelta(hours=44, minutes=15), "device", "sw-cx-02", "firmware", "changed", "AOS-CX", None, "10.12.1010", "10.13.1000",
        {"os_family": "aos-cx", "os_release": "10.13", "os_source": "profile"})
    ev.sort(key=lambda e: (e["at"], e["id"], e["action"], e["subject"]))
    return ev


# ── Daily snapshots ──────────────────────────────────────────────────

SNAPSHOT_DAYS = 14               # the last 14 local days up to and including FIXED_NOW's date
SNAPSHOT_GAP_DAY = 5             # this many days before the last one nobody snapshotted (a gap, never a zero)


def snapshot_rows() -> list[dict[str, Any]]:
    """Deterministic ``inventory_daily_snapshots`` rows for the trend endpoint: the families and dimensions the
    snapshot loop writes, a slow upward drift per dimension, and one missing day."""
    last = FIXED_NOW.date()
    dims: dict[str, list[tuple[str, int, int]]] = {       # metric -> (dimension, value on the last day, drift per day)
        "assets": [("server", 60, 1), ("device", 85, 2)],
        "coverage": [("fresh", 109, 1), ("aging", 12, 0), ("stale", 8, 0), ("never", 16, -1)],
        "health": [("missing_os_version", 20, 0), ("missing_model", 17, 0), ("missing_serial", 20, 0), ("unknown_vendor", 9, 0),
                   ("software_truncated", 2, 0), ("stale", 8, 0), ("never", 16, -1)],
        "software_products": [("products", 26, 1), ("installs", 456, 7)],
        "os_release": [("windows/Windows Server/2022", 18, 1), ("windows/Windows Server/2019", 14, 0),
                       ("fortios/FortiOS/7.2", 7, 0), ("unknown//", 17, 0)],
        "changes": [("software/added", 4, 0), ("software/removed", 3, 0), ("software/changed", 6, 0),
                    ("os/changed", 1, 0), ("firmware/changed", 2, 0), ("hardware/changed", 0, 0)],
    }
    out = []
    for back in range(SNAPSHOT_DAYS):
        if back == SNAPSHOT_GAP_DAY:
            continue
        day = last - timedelta(days=back)
        for metric, items in dims.items():
            for dimension, final, drift in items:
                out.append({"day": day.isoformat(), "metric": metric, "dimension": dimension,
                            "value": max(0, final - drift * back + (back * 7 + len(dimension)) % 3 * (1 if metric == "changes" else 0))})
    return out


# ── The builder ──────────────────────────────────────────────────────

@dataclass
class FleetInfo:
    now: datetime = FIXED_NOW
    server_ids: dict[str, str] = field(default_factory=dict)
    device_ids: dict[str, str] = field(default_factory=dict)
    linked_device_ids: dict[str, str] = field(default_factory=dict)       # server name -> its SNMP twin id
    expected: dict[str, Any] = field(default_factory=dict)
    scoped_user: dict[str, Any] = field(default_factory=dict)
    event_count: int = 0

    @property
    def assets_total(self) -> int:
        return self.expected["servers"] + self.expected["devices_in_union"]


_COLS = {
    "servers": [
        ("id", "uuid"), ("display_name", "text"), ("hostname", "text"), ("primary_ip", "inet"), ("site_id", "uuid"),
        ("device_id", "uuid"), ("os_type", "text"), ("os_name", "text"), ("os_version", "text"), ("kernel_or_build", "text"),
        ("architecture", "text"), ("collection_mode", "text"), ("status", "text"), ("environment", "text"), ("tags", "jsonb"),
        ("last_seen", "timestamptz"), ("cpu_model", "text"), ("cpu_cores", "int"), ("cpu_physical_cores", "int"),
        ("memory_total_bytes", "bigint"), ("physical_disks", "jsonb"), ("virtualization", "text"),
        ("inventory_updated_at", "timestamptz"), ("inventory_collected_at", "timestamptz"),
        ("inventory_baselined_at", "timestamptz"), ("software_truncated", "boolean"), ("boot_time", "timestamptz"),
    ],
    "devices": [
        ("id", "uuid"), ("hostname", "text"), ("ip_address", "inet"), ("device_type", "text"), ("location", "text"),
        ("group_id", "uuid"), ("tags", "jsonb"), ("snmp_enabled", "boolean"), ("status", "text"), ("last_seen", "timestamptz"),
        ("sys_object_id", "text"), ("vendor", "text"), ("model", "text"), ("os_version", "text"), ("profile_id", "uuid"),
        ("serial_number", "text"), ("firmware_version", "text"), ("sys_descr", "text"), ("identity_updated_at", "timestamptz"),
        ("managed_by_device_id", "uuid"), ("poll_mode", "text"),
    ],
    "server_software_inventory": [
        ("server_id", "uuid"), ("package_name", "text"), ("version", "text"), ("vendor", "text"), ("install_date", "timestamptz"),
        ("updated_at", "timestamptz"), ("first_seen_at", "timestamptz"), ("architecture", "text"), ("source", "text"),
        ("name_key", "text"), ("publisher_key", "text"), ("category", "text"), ("norm_rev", "int"),
    ],
    "inventory_change_events": [
        ("observed_at", "timestamptz"), ("asset_kind", "text"), ("asset_id", "uuid"), ("asset_name", "text"), ("category", "text"),
        ("action", "text"), ("subject", "text"), ("subject_key", "text"), ("old_value", "text"), ("new_value", "text"),
        ("detail", "jsonb"),
    ],
    "inventory_daily_snapshots": [("day", "date"), ("metric", "text"), ("dimension", "text"), ("value", "bigint")],
    "server_service_inventory": [
        ("server_id", "uuid"), ("service_name", "text"), ("display_name", "text"), ("start_mode", "text"), ("state", "text"),
        ("updated_at", "timestamptz"),
    ],
}


async def _bulk_insert(conn, table: str, rows: list[dict[str, Any]], columns: Optional[list[tuple[str, str]]] = None) -> None:
    if not rows:
        return
    cols = columns or _COLS[table]
    names = ", ".join(c for c, _t in cols)
    defs = ", ".join(f"{c} {t}" for c, t in cols)
    await conn.execute(
        f"INSERT INTO {table} ({names}) SELECT {names} FROM jsonb_to_recordset($1::jsonb) AS r({defs})", rows)


def _server_identity(spec: dict[str, Any]) -> norm.Identity:
    return norm.normalize_server(spec["os_name"], spec["os_version"], spec["kernel"], spec["arch"], cpu_model=spec["cpu"])


def _device_identity(d: dict[str, Any]) -> norm.Identity:
    return norm.normalize_device(
        vendor=d["vendor"], model=d["model"], os_version=d["os_version"], firmware_version=d["firmware_version"],
        serial_number=d["serial"], sys_object_id=d["oid"], sys_descr=d["descr"], profile_name=d["profile"],
        template_values=d["template"] or None)


async def _build(conn) -> FleetInfo:
    info = FleetInfo()
    servers = _server_specs()
    devices = _device_specs()
    _apply_device_variation(devices)
    twins = {d["twin_of"]: d for d in devices if d["kind"] == "twin"}

    async with conn.transaction():
        # Every capture trigger stays quiet while the fixture is loaded; the events below are explicit.
        await conn.execute("SET LOCAL zenplus.inventory_suppress = 'on'")
        # The migrations seed demo devices (monitoring templates); the fleet is the whole estate.
        await conn.execute("DELETE FROM servers")
        await conn.execute("DELETE FROM devices")
        await conn.execute("DELETE FROM inventory_identity")

        site_ids = {}
        for name in _SITES:
            site_ids[name] = aid("site", name)
            await conn.execute("INSERT INTO sites (id, name) VALUES ($1, $2)", site_ids[name], name)
        group_ids = {}
        for name in _GROUPS:
            group_ids[name] = aid("group", name)
            await conn.execute("INSERT INTO device_groups (id, name) VALUES ($1, $2)", group_ids[name], name)
        profile_ids = {}
        for name in _PROFILES:
            profile_ids[name] = aid("profile", name)
            await conn.execute("INSERT INTO device_profiles (id, name, vendor, version) VALUES ($1, $2, NULL, 9001)",
                               profile_ids[name], name)

        # Devices first (servers reference the SNMP twins).
        dev_rows = []
        for d in devices:
            did = aid("device", d["name"])
            info.device_ids[d["name"]] = str(did)
            dev_rows.append({
                "id": str(did), "hostname": d["name"], "ip_address": d["ip"],
                "device_type": {"fortigate": "firewall", "palo": "firewall", "f5": "other", "twin": "server"}.get(d["kind"], "switch"),
                "location": None if d["idx"] % 6 == 5 else f"{('HQ', 'DR', 'Branch-1')[d['idx'] % 3]} / rack {d['idx'] % 9 + 1}",
                "group_id": str(group_ids[d["group"]]), "tags": d["tags"], "snmp_enabled": not d["ping_only"],
                "status": d["status"], "last_seen": iso_dt(d["updated"]) if d["updated"] else None,
                "sys_object_id": d["oid"], "vendor": d["vendor"], "model": d["model"], "os_version": d["os_version"],
                "profile_id": str(profile_ids[d["profile"]]), "serial_number": d["serial"],
                "firmware_version": d["firmware_version"], "sys_descr": d["descr"],
                "identity_updated_at": iso_dt(d["identity_at"] if "identity_at" in d else d["updated"]) if (
                    d["identity_at"] if "identity_at" in d else d["updated"]) else None,
                "managed_by_device_id": str(aid("device", d["parent"])) if d.get("parent") else None,
                "poll_mode": "via_controller" if d.get("parent") else "direct",
            })
        await _bulk_insert(conn, "devices", dev_rows)

        srv_rows, sw_rows = [], []
        for s in servers:
            sid = aid("server", s["name"])
            info.server_ids[s["name"]] = str(sid)
            twin = twins.get(s["name"])
            if twin:
                info.linked_device_ids[s["name"]] = info.device_ids[twin["name"]]
            mem = s["mem_gib"] * 2**30 if s["mem_gib"] else None
            baselined = (not s["never"]) and s["os_type"] != "unknown"
            srv_rows.append({
                "id": str(sid), "display_name": s["name"], "hostname": s["name"], "primary_ip": s["ip"],
                "site_id": str(site_ids[s["site"]]) if s["site"] else None,
                "device_id": info.device_ids[twin["name"]] if twin else None,
                "os_type": s["os_type"], "os_name": s["os_name"], "os_version": s["os_version"], "kernel_or_build": s["kernel"],
                "architecture": s["arch"], "collection_mode": s["mode"],
                "status": "stale" if s["updated"] is None else "healthy", "environment": "production" if "prod" in s["tags"] else None,
                "tags": s["tags"], "last_seen": iso_dt(s["updated"]) if s["updated"] else None,
                "cpu_model": s["cpu"], "cpu_cores": s["disks"] * 2 + 2 if s["cpu"] else None,
                "cpu_physical_cores": s["disks"] + 1 if s["cpu"] else None, "memory_total_bytes": mem,
                "physical_disks": [{"model": f"Disk {k}", "size_bytes": 480103981056 * (k % 3 + 1)} for k in range(s["disks"])],
                "virtualization": s["virt"],
                "inventory_updated_at": iso_dt(s["updated"]) if s["updated"] and s["os_type"] != "unknown" else (
                    iso_dt(s["updated"]) if s["updated"] else None),
                "inventory_collected_at": iso_dt(s["updated"]) if s["updated"] else None,
                "inventory_baselined_at": iso_dt(s["updated"]) if baselined else None,
                "software_truncated": s["truncated"], "boot_time": iso_dt(ago(days=3 + s["idx"] % 40)) if s["updated"] else None,
            })
            sw_rows.extend(software_rows(s))
        await _bulk_insert(conn, "servers", srv_rows)
        await _bulk_insert(conn, "server_software_inventory", sw_rows)

        # Services: the running Microsoft Defender service is the only evidence of built-in protection (it has no
        # Uninstall entry); a stopped Defender for Endpoint sensor is not protection.
        svc_rows = []
        by_name = {s["name"]: s for s in servers}
        for names, service, display, state in (
                (DEFENDER_SERVICE_SERVERS, "WinDefend", "Microsoft Defender Antivirus Service", "running"),
                (SENSE_STOPPED_SERVERS, "Sense", "Windows Defender Advanced Threat Protection Service", "stopped")):
            for name in names:
                at = iso_dt(by_name[name]["updated"] or FIXED_NOW)
                svc_rows.append({"server_id": str(aid("server", name)), "service_name": service, "display_name": display,
                                 "start_mode": "auto", "state": state, "updated_at": at})
                svc_rows.append({"server_id": str(aid("server", name)), "service_name": "W32Time",
                                 "display_name": "Windows Time", "start_mode": "auto", "state": "running", "updated_at": at})
        # Every other baselined server uploaded a service list with its software (X9): a plain one without Defender.
        have = {r["server_id"] for r in svc_rows}
        for sp in servers:
            sid = str(aid("server", sp["name"]))
            if sp["updated"] is None or sid in have or sp["name"] in NO_SERVICE_LIST_SERVERS or sp["never"]:
                continue
            if sp["os_type"] not in ("windows", "linux"):
                continue
            plain = ("W32Time", "Windows Time") if sp["os_type"] == "windows" else ("cron", "Regular background program processing daemon")
            svc_rows.append({"server_id": sid, "service_name": plain[0], "display_name": plain[1], "start_mode": "auto",
                             "state": "running", "updated_at": iso_dt(sp["updated"])})
        await _bulk_insert(conn, "server_service_inventory", svc_rows)
        # When change capture began (migration 158 records the real time of the migration; the fleet pins it).
        await conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ($1, to_jsonb($2::text)) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", iq.CAPTURE_KEY, iq.iso(CAPTURE_STARTED))

        # Identity rows, as the normalise loop would leave them.
        ident = []
        for s in servers:
            i = _server_identity(s)
            ident.append(("server", aid("server", s["name"]), i, s["updated"]))
        for d in devices:
            i = _device_identity(d)
            ident.append(("device", aid("device", d["name"]), i, d["updated"]))
        for kind, uid, i, at in ident:
            await conn.execute(
                """INSERT INTO inventory_identity (asset_kind, asset_id, vendor_key, vendor_label, os_family, os_product,
                       os_release, os_version, os_build, os_edition, os_source, hw_vendor, hw_model, hw_serial, hw_source,
                       norm_rev, values_rev, source_changed_at, normalized_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$16,$17,$17)
                   ON CONFLICT (asset_kind, asset_id) DO UPDATE SET vendor_key = EXCLUDED.vendor_key,
                       vendor_label = EXCLUDED.vendor_label, os_family = EXCLUDED.os_family, os_product = EXCLUDED.os_product,
                       os_release = EXCLUDED.os_release, os_version = EXCLUDED.os_version, os_build = EXCLUDED.os_build,
                       os_edition = EXCLUDED.os_edition, os_source = EXCLUDED.os_source, hw_vendor = EXCLUDED.hw_vendor,
                       hw_model = EXCLUDED.hw_model, hw_serial = EXCLUDED.hw_serial, hw_source = EXCLUDED.hw_source,
                       norm_rev = EXCLUDED.norm_rev, values_rev = EXCLUDED.values_rev,
                       source_changed_at = EXCLUDED.source_changed_at, normalized_at = EXCLUDED.normalized_at""",
                kind, uid, i.vendor_key, i.vendor_label, i.os_family, i.os_product, i.os_release, i.os_version, i.os_build,
                i.os_edition, i.os_source, i.hw_vendor, i.hw_model, i.hw_serial, i.hw_source, norm.RULEPACK_REV,
                at or FIXED_NOW)

        # ENTITY-MIB: chassis for every polled device, line cards and power supplies where the gear has them.
        ents = []
        for d in devices:
            if d["updated"] is None or d["kind"] in ("empty", "twin"):
                continue
            did = aid("device", d["name"])
            ents.append((did, 1, 0, "chassis", "Chassis", d["serial"], d["model"], None))
            if d["kind"] == "iosxe":
                ents.append((did, 2, 1, "powerSupply", "Switch 1 - Power Supply A", f"DCA{d['idx']:05d}A", "PWR-C1-715WAC-P", None))
                ents.append((did, 3, 1, "powerSupply", "Switch 1 - Power Supply B", f"DCA{d['idx']:05d}B" if d["idx"] % 4 else None,
                             "PWR-C1-715WAC-P", None))
                if d["model"] == "C9300-48P":
                    ents.append((did, 4, 1, "module", "Network Module", f"FOC2322{d['idx']:04d}", "C3850-NM-4-10G", None))
            elif d["kind"] == "palo":
                ents.append((did, 2, 1, "fan", "Fan Tray 1", None, "PA-3200-FAN", None))
            elif d["kind"] == "junos":
                ents.append((did, 2, 1, "module", "FPC 0", f"BUILTIN{d['idx']:04d}", "EX4300-48T", None))
                ents.append((did, 3, 1, "powerSupply", "Power Supply 0", f"1GA{d['idx']:05d}", "JPSU-350-AC-AFO", None))
            elif d["kind"] == "nxos":
                ents.append((did, 2, 1, "module", "48x10/25G + 6x40/100G", f"FOC2334{d['idx']:04d}", "N9K-C93180YC-FX", None))
            ents.append((did, 90, 1, "port", "port 1", None, None, None))     # noise the module view must ignore
        for e in ents:
            await conn.execute(
                "INSERT INTO device_entities (device_id, ent_index, parent_index, class, name, serial_number, model_name, fw_revision)"
                " VALUES ($1,$2,$3,$4,$5,$6,$7,$8)", *e)

        # Template values: firmware strings (the freshest source for FortiOS, PAN-OS and TMOS) and HA state.
        for d in devices:
            if d["updated"] is None:
                continue
            did = aid("device", d["name"])
            at = d["updated"]
            for key, value in d["template"].items():
                await conn.execute(
                    "INSERT INTO device_template_values (device_id, group_key, metric_key, instance, series_key, value_text, updated_at)"
                    " VALUES ($1,'system',$2,'',$2,$3,$4)", did, key, value, at)
            n = int(d["name"].rsplit("-", 1)[-1])      # fg-02, fg-04 ... and pa-01, pa-03 ... are HA members
            if d["kind"] == "fortigate" and n % 2 == 0:
                await conn.execute(
                    "INSERT INTO device_template_values (device_id, group_key, metric_key, instance, series_key, value_num, updated_at)"
                    " VALUES ($1,'ha','fgt_ha_mode','','fgt_ha_mode',3,$2), ($1,'vdoms','fgt_vd_ha_state','root','fgt_vd_ha_state_root',1,$2)",
                    did, at)
            if d["kind"] == "palo" and n % 2 == 1:
                await conn.execute(
                    "INSERT INTO device_template_values (device_id, group_key, metric_key, instance, series_key, value_text, updated_at)"
                    " VALUES ($1,'ha','pan_ha_mode','','pan_ha_mode','active-passive',$2),"
                    " ($1,'ha','pan_ha_state','','pan_ha_state','active',$2), ($1,'ha','pan_ha_peer','','pan_ha_peer','passive',$2)",
                    did, at)

        # Inserting the template values above marked their devices dirty again (dirty-marking is never suppressed); the
        # identities were computed from those very values, so the fleet is a settled system: nothing is waiting for the loop.
        await conn.execute("UPDATE inventory_identity SET norm_rev = $1 WHERE norm_rev IS NULL", norm.RULEPACK_REV)

        events = _events(servers, devices)
        await _bulk_insert(conn, "inventory_change_events", [{
            "observed_at": iso_dt(e["at"]), "asset_kind": e["kind"], "asset_id": e["id"], "asset_name": e["name"],
            "category": e["category"], "action": e["action"], "subject": e["subject"], "subject_key": e["key"],
            "old_value": e["old"], "new_value": e["new"], "detail": e["detail"]} for e in events])
        info.event_count = len(events)
        await _bulk_insert(conn, "inventory_daily_snapshots", snapshot_rows())

        # A role-less user is enough for the HTTP tests (they inject the user object), but the fleet has one
        # in the database too, so queries that join users see a realistic row.
        await conn.execute(
            "INSERT INTO users (id, username, email, password_hash, role, scope_tags) VALUES ($1,$2,$3,'!',$4,$5)",
            aid("user", SCOPED_USERNAME), SCOPED_USERNAME, "edge.operator@example.test", "viewer", [SCOPED_TAG])

    info.scoped_user = {"username": SCOPED_USERNAME, "role": "viewer", "scope_tags": [SCOPED_TAG]}
    twin_count = len(twins)
    info.expected = {
        "servers": len(servers),
        "devices": len(devices),
        "devices_in_union": len(devices) - twin_count,
        "linked": twin_count,
        "events": len(events),
    }
    return info


def build_fleet(db) -> FleetInfo:
    """Fill the migrated database ``db`` (a pg_fixture.PgDatabase) with the fleet. Returns what the tests need to know."""
    return db._with_conn(_build)


def add_software_rows(db, rows: list[dict[str, Any]]) -> None:
    """Insert extra software rows into ``db`` (use ``fleet_db_fresh``: the shared copies are read-only by convention).

    Each row is ``{server: <fleet name>, title, vendor, version, source='hklm', first_seen_days=60, install_days=30}``;
    keys come from the real normaliser exactly as for the fleet's own rows. The change-capture triggers are
    suppressed, so the rows add no events."""
    full = []
    for r in rows:
        source = r.get("source", "hklm")
        key = norm.normalize_software(r["title"], r.get("vendor"), r.get("version"), source=source)
        full.append({
            "server_id": str(aid("server", r["server"])), "package_name": r["title"], "version": r.get("version"),
            "vendor": r.get("vendor"), "install_date": iso_dt(ago(days=r.get("install_days", 30))),
            "updated_at": iso_dt(ago(hours=1)), "first_seen_at": iso_dt(ago(days=r.get("first_seen_days", 60))),
            "architecture": r.get("architecture", "x64" if source == "hklm" else None), "source": source,
            "name_key": key.name_key, "publisher_key": key.publisher_key, "category": key.category,
            "norm_rev": norm.RULEPACK_REV,
        })

    async def work(conn):
        async with conn.transaction():
            await conn.execute("SET LOCAL zenplus.inventory_suppress = 'on'")
            await _bulk_insert(conn, "server_software_inventory", full)

    db._with_conn(work)


def add_assets(db, devices: Optional[list[dict[str, Any]]] = None, servers: Optional[list[dict[str, Any]]] = None) -> None:
    """Insert extra devices and servers WITH their identity rows exactly as given (no normaliser), for tests that need a
    specific spelling or a specific gap (use ``fleet_db_fresh``). The capture triggers are suppressed.

    A device spec: ``name`` plus any of ``vendor, model, serial, os_family, os_product, os_release, os_version, location,
    tags, snmp_enabled (default True), identity_age_hours (default 1; None = never reported), managed_by (a device name)``.
    A server spec: ``name`` plus any of ``os_family, os_product, os_release, os_version, tags, mode (default 'agent'),
    baselined (default False), updated_age_hours (default 1; None = never reported), device (a device name to link)``.
    The identity rows get the current rule-pack revision, so nothing is pending. ``tags`` go to the jsonb parameter as a
    LIST: the connection's jsonb codec encodes it, so a pre-dumped string would be stored as a JSON string and no tag
    predicate would ever match (NF-3)."""
    devices = devices or []
    servers = servers or []

    async def work(conn):
        async with conn.transaction():
            await conn.execute("SET LOCAL zenplus.inventory_suppress = 'on'")
            for i, d in enumerate(devices):
                did = aid("device", d["name"])
                age = d.get("identity_age_hours", 1)
                at = ago(hours=age) if age is not None else None
                await conn.execute(
                    "INSERT INTO devices (id, hostname, ip_address, device_type, location, tags, snmp_enabled, status, last_seen, "
                    "identity_updated_at, managed_by_device_id, poll_mode) VALUES ($1,$2,$3,'switch',$4,$5::jsonb,$6,'up',$7,$7,$8,$9)",
                    did, d["name"], d.get("ip", f"10.99.{i // 250}.{i % 250 + 1}"), d.get("location"), list(d.get("tags", [])),
                    d.get("snmp_enabled", True), at, aid("device", d["managed_by"]) if d.get("managed_by") else None,
                    "via_controller" if d.get("managed_by") else "direct")
                await _put_identity(conn, "device", did, d)
            for i, sv in enumerate(servers):
                sid = aid("server", sv["name"])
                age = sv.get("updated_age_hours", 1)
                at = ago(hours=age) if age is not None else None
                await conn.execute(
                    "INSERT INTO servers (id, display_name, hostname, primary_ip, os_type, collection_mode, status, tags, last_seen, "
                    "inventory_updated_at, inventory_baselined_at, device_id) VALUES ($1,$2,$2,$3,'windows',$4,'healthy',$5::jsonb,$6,$6,$7,$8)",
                    sid, sv["name"], sv.get("ip", f"10.98.{i // 250}.{i % 250 + 1}"), sv.get("mode", "agent"),
                    list(sv.get("tags", [])), at, at if sv.get("baselined") else None,
                    aid("device", sv["device"]) if sv.get("device") else None)
                await _put_identity(conn, "server", sid, sv)

    db._with_conn(work)


async def _put_identity(conn, kind: str, uid: uuid.UUID, spec: dict[str, Any]) -> None:
    model, version = spec.get("model"), spec.get("os_version")
    await conn.execute(
        """INSERT INTO inventory_identity (asset_kind, asset_id, vendor_label, os_family, os_product, os_release, os_version,
               hw_vendor, hw_model, hw_serial, hw_source, os_source, norm_rev, values_rev, source_changed_at, normalized_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$3,$8,$9,$10,$11,$12,$12,$13,$13)
           ON CONFLICT (asset_kind, asset_id) DO UPDATE SET vendor_label = EXCLUDED.vendor_label, os_family = EXCLUDED.os_family,
               os_product = EXCLUDED.os_product, os_release = EXCLUDED.os_release, os_version = EXCLUDED.os_version,
               hw_vendor = EXCLUDED.hw_vendor, hw_model = EXCLUDED.hw_model, hw_serial = EXCLUDED.hw_serial,
               hw_source = EXCLUDED.hw_source, os_source = EXCLUDED.os_source, norm_rev = EXCLUDED.norm_rev,
               values_rev = EXCLUDED.values_rev, normalized_at = EXCLUDED.normalized_at""",
        kind, uid, spec.get("vendor"), spec.get("os_family", "unknown"), spec.get("os_product"), spec.get("os_release"),
        version, model, spec.get("serial"), "device" if model is not None else None, "profile" if version is not None else None,
        norm.RULEPACK_REV, FIXED_NOW)


# ── pytest fixtures ──────────────────────────────────────────────────

_lock = threading.Lock()
_templates: dict[int, tuple[Any, FleetInfo]] = {}
_TEMPLATE_DB = "zp_inv_fleet_tpl"


def get_fleet_template(cluster) -> tuple[Any, FleetInfo]:
    """The fleet, built once per process into a template database; fixtures clone it in milliseconds."""
    with _lock:
        hit = _templates.get(id(cluster))
        if hit is not None:
            return hit
        db = cluster.create_database(_TEMPLATE_DB)
        info = build_fleet(db)
        _templates[id(cluster)] = (db, info)
        return db, info


@pytest.fixture(scope="session")
def fleet_template(pg_cluster):
    return get_fleet_template(pg_cluster)


@pytest.fixture(scope="module")
def fleet_info(fleet_template) -> FleetInfo:
    return fleet_template[1]


@pytest.fixture(scope="module")
def fleet_db(pg_cluster, fleet_template):
    """A private copy of the fleet database, shared by the tests of one module. Tests that change data use ``fleet_db_fresh``."""
    db = pg_cluster.create_database(template=fleet_template[0].name)
    try:
        yield db
    finally:
        db.drop()


@pytest.fixture
def fleet_db_fresh(pg_cluster, fleet_template):
    db = pg_cluster.create_database(template=fleet_template[0].name)
    try:
        yield db
    finally:
        db.drop()


def clear_inventory_caches() -> None:
    """Every in-process cache of the Inventory read model (the summary, the software catalog tables)."""
    from app.services import inventory_software_query as isq
    iq.clear_caches()
    isq.clear_caches()


@pytest.fixture
def pinned_clock(monkeypatch):
    """Every Inventory read sees the clock stop at FIXED_NOW, and no summary or catalog cached by another test."""
    monkeypatch.setattr(iq, "utcnow", lambda: FIXED_NOW)
    clear_inventory_caches()
    yield FIXED_NOW
    clear_inventory_caches()


# ── Calling the service layer and the HTTP API against a fleet database ──

@contextlib.asynccontextmanager
async def sessions(db) -> Any:
    """An async session factory on ``db`` (NullPool, so it is safe across event loops)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def make_user(role: str = "viewer", scope_tags: tuple[str, ...] = (), username: str = "tester",
              user_id: Optional[uuid.UUID] = None) -> SimpleNamespace:
    return SimpleNamespace(id=user_id or uuid.uuid4(), username=username, email=f"{username}@example.test", full_name=username,
                           role=role, is_active=True, last_login=None, password_hash="", scope_tags=list(scope_tags))


def ensure_role(db, name: str, permissions: list[str], scope_tags: tuple[str, ...] = ()) -> None:
    """Create (or reset) a role row in ``db``. The tests' autouse shim answers built-in role names from
    the code definitions and everything else with nothing; ``api_client`` bypasses it for ``db`` so the
    real ``roles`` lookup (permissions and role-level ``scope_tags``) is what the request exercises."""
    db.execute(
        "INSERT INTO roles (name, display_name, description, permissions, scope_tags, is_system)"
        " VALUES ($1, $1, '', $2::jsonb, $3::jsonb, false)"
        " ON CONFLICT (name) DO UPDATE SET permissions = EXCLUDED.permissions, scope_tags = EXCLUDED.scope_tags",
        name, list(permissions), list(scope_tags))


def ensure_user(db, user_id: uuid.UUID, username: str, role: str) -> None:
    """A ``users`` row for the request's user. The audit trail and ``system_settings.updated_by`` reference ``users(id)``
    and the audit writer is best-effort (a failed insert is swallowed), so a test that asserts an audit entry needs
    the user to exist. ``audit_logs.actor_role`` is ``varchar(50)`` (migration 159), the same width as ``roles.name``."""
    db.execute(
        "INSERT INTO users (id, username, email, password_hash, role, scope_tags) VALUES ($1, $2, $3, '!', $4, '[]'::jsonb)"
        " ON CONFLICT DO NOTHING", user_id, username, f"{username}@example.test", role)


@contextlib.contextmanager
def api_client(db, *, role: str = "viewer", scope_tags: tuple[str, ...] = (), username: str = "tester",
               permissions: Optional[list[str]] = None, role_scope_tags: tuple[str, ...] = (),
               persist_user: bool = False) -> Iterator[Any]:
    """A TestClient (no lifespan: no background loops, no startup connections) whose requests run against
    ``db`` as a user with ``role`` and ``scope_tags``. Never touches the appliance database.

    The role is resolved from the ``roles`` table of ``db`` (the real lookup). With ``permissions`` the role
    row is created first (``role_scope_tags`` is its role-level visibility scope); without it the role must
    already exist there (``admin``, ``operator`` and ``viewer`` do, from the migrations). With ``persist_user`` the
    user is also inserted into ``users`` (see ``ensure_user``), which audit-trail assertions need."""
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.database import get_db
    from app.core.security import get_current_user, invalidate_role_cache
    from app.main import app

    if permissions is not None:
        ensure_role(db, role, permissions, role_scope_tags)

    async def _db():
        engine = create_async_engine(db.url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                session.answers_role_lookup = True          # see tests/conftest.py: use the real roles lookup
                yield session
        finally:
            await engine.dispose()

    user_id = aid("user", username) if persist_user else uuid.uuid4()       # one id per username: the row is shared across clients
    if persist_user:
        ensure_user(db, user_id, username, role)

    async def _user():
        return make_user(role, scope_tags, username, user_id)

    invalidate_role_cache()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        invalidate_role_cache()
