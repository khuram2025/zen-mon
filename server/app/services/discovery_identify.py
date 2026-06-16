"""Vendor / device-type identification from real probe outputs.

Given the collection of probe results for a single IP (icmp, tcp, http, https,
ssh, snmp, winrm), produce a single best-guess identity:

    {
        "vendor", "device_type", "model", "os", "os_version",
        "hostname", "sys_name", "sys_object_id", "serial_number",
        "mac_address", "open_ports", "protocols_detected",
        "response_time_ms", "confidence_score",
        "credential_status", "credential_used", "windows_credential_used",
        "raw_data": {...}
    }
"""

from __future__ import annotations

import re
from typing import Any, Optional


# ────────────────────────────────────────────────────────────────────
# sysObjectID prefix → vendor (subset; expand as needed)
# ────────────────────────────────────────────────────────────────────
_SYS_OBJECT_PREFIXES: list[tuple[str, str, str]] = [
    # prefix, vendor, default_device_type
    ("1.3.6.1.4.1.9.",        "Cisco",    "router"),
    ("1.3.6.1.4.1.2636.",     "Juniper",  "router"),
    ("1.3.6.1.4.1.12356.",    "Fortinet", "firewall"),
    ("1.3.6.1.4.1.14988.",    "MikroTik", "router"),
    ("1.3.6.1.4.1.14823.",    "Aruba",    "switch"),
    ("1.3.6.1.4.1.25461.",    "Palo Alto Networks", "firewall"),
    ("1.3.6.1.4.1.11.",       "HP",       "server"),
    ("1.3.6.1.4.1.232.",      "HP",       "server"),
    ("1.3.6.1.4.1.41112.",    "Ubiquiti", "access_point"),
    ("1.3.6.1.4.1.11863.",    "TP-Link",  "access_point"),
    ("1.3.6.1.4.1.674.",      "Dell",     "server"),
    ("1.3.6.1.4.1.311.",      "Microsoft","server"),
    ("1.3.6.1.4.1.8072.",     "Net-SNMP", "server"),       # generic snmpd
    ("1.3.6.1.4.1.6027.",     "Force10",  "switch"),
    ("1.3.6.1.4.1.4526.",     "Netgear",  "switch"),
    ("1.3.6.1.4.1.1991.",     "Brocade",  "switch"),
    ("1.3.6.1.4.1.890.",      "Zyxel",    "router"),
]


# ────────────────────────────────────────────────────────────────────
# sysDescr patterns → enrichment
# ────────────────────────────────────────────────────────────────────
_SYS_DESCR_PATTERNS: list[tuple[re.Pattern, dict[str, str]]] = [
    (re.compile(r"Cisco IOS-XE", re.I),       {"vendor": "Cisco", "os": "IOS-XE"}),
    (re.compile(r"Cisco IOS", re.I),          {"vendor": "Cisco", "os": "IOS"}),
    (re.compile(r"Cisco NX-OS", re.I),        {"vendor": "Cisco", "os": "NX-OS"}),
    (re.compile(r"Cisco Adaptive Security", re.I), {"vendor": "Cisco", "os": "ASA", "device_type": "firewall"}),
    (re.compile(r"Junos", re.I),              {"vendor": "Juniper", "os": "Junos"}),
    (re.compile(r"FortiGate", re.I),          {"vendor": "Fortinet", "os": "FortiOS", "device_type": "firewall"}),
    (re.compile(r"RouterOS", re.I),           {"vendor": "MikroTik", "os": "RouterOS", "device_type": "router"}),
    (re.compile(r"ArubaOS", re.I),            {"vendor": "Aruba", "os": "ArubaOS"}),
    (re.compile(r"UniFi", re.I),              {"vendor": "Ubiquiti", "os": "UniFi"}),
    (re.compile(r"HP ETHERNET", re.I),        {"vendor": "HP", "device_type": "printer"}),
    (re.compile(r"Linux", re.I),              {"os": "Linux", "device_type": "server"}),
    (re.compile(r"Windows", re.I),            {"os": "Windows", "device_type": "server"}),
    (re.compile(r"VMware ESX", re.I),         {"vendor": "VMware", "os": "ESXi", "device_type": "server"}),
    (re.compile(r"Synology", re.I),           {"vendor": "Synology", "device_type": "server"}),
    (re.compile(r"Brother", re.I),            {"vendor": "Brother", "device_type": "printer"}),
    (re.compile(r"Canon", re.I),              {"vendor": "Canon", "device_type": "printer"}),
    (re.compile(r"Epson", re.I),              {"vendor": "Epson", "device_type": "printer"}),
    (re.compile(r"PAN-OS", re.I),             {"vendor": "Palo Alto Networks", "os": "PAN-OS", "device_type": "firewall"}),
    (re.compile(r"Zyxel", re.I),              {"vendor": "Zyxel"}),
    (re.compile(r"DD-WRT|OpenWrt", re.I),     {"vendor": "OpenWrt", "device_type": "router"}),
]


# ────────────────────────────────────────────────────────────────────
# HTTP/HTTPS Server header → vendor
# ────────────────────────────────────────────────────────────────────
_HTTP_SERVER_PATTERNS: list[tuple[re.Pattern, dict[str, str]]] = [
    (re.compile(r"Microsoft-IIS",   re.I), {"vendor": "Microsoft", "os": "Windows", "device_type": "server"}),
    (re.compile(r"nginx",            re.I), {"device_type": "server"}),
    (re.compile(r"Apache",           re.I), {"device_type": "server"}),
    (re.compile(r"lighttpd",         re.I), {"device_type": "server"}),
    (re.compile(r"Cisco-IOS",        re.I), {"vendor": "Cisco", "os": "IOS"}),
    (re.compile(r"FortiOS",          re.I), {"vendor": "Fortinet", "os": "FortiOS", "device_type": "firewall"}),
    (re.compile(r"RouterOS",         re.I), {"vendor": "MikroTik", "device_type": "router"}),
    (re.compile(r"hp",               re.I), {"vendor": "HP"}),
    (re.compile(r"Synology",         re.I), {"vendor": "Synology", "device_type": "server"}),
]

# Body title hints for management UIs
_TITLE_PATTERNS: list[tuple[re.Pattern, dict[str, str]]] = [
    (re.compile(r"FortiGate", re.I),           {"vendor": "Fortinet", "device_type": "firewall"}),
    (re.compile(r"Cisco",     re.I),           {"vendor": "Cisco"}),
    (re.compile(r"MikroTik|RouterOS|WebFig", re.I), {"vendor": "MikroTik", "device_type": "router"}),
    (re.compile(r"pfSense",   re.I),           {"vendor": "Netgate", "os": "pfSense", "device_type": "firewall"}),
    (re.compile(r"OPNsense",  re.I),           {"vendor": "Deciso", "os": "OPNsense", "device_type": "firewall"}),
    (re.compile(r"Ubiquiti|UniFi", re.I),      {"vendor": "Ubiquiti", "device_type": "access_point"}),
    (re.compile(r"Synology",  re.I),           {"vendor": "Synology", "device_type": "server"}),
    (re.compile(r"TP-Link",   re.I),           {"vendor": "TP-Link"}),
    (re.compile(r"Netgear",   re.I),           {"vendor": "Netgear"}),
    (re.compile(r"Aruba",     re.I),           {"vendor": "Aruba"}),
    (re.compile(r"VMware",    re.I),           {"vendor": "VMware", "device_type": "server"}),
    (re.compile(r"Proxmox",   re.I),           {"vendor": "Proxmox", "device_type": "server"}),
    (re.compile(r"OpenWrt|LuCI", re.I),        {"vendor": "OpenWrt", "device_type": "router"}),
    (re.compile(r"ZenPlus",   re.I),           {"vendor": "ZenPlus", "device_type": "server"}),
    (re.compile(r"OpenSSH|Welcome to nginx", re.I), {"device_type": "server"}),
]


# ────────────────────────────────────────────────────────────────────
# SSH banner → vendor
# ────────────────────────────────────────────────────────────────────
_SSH_PATTERNS: list[tuple[re.Pattern, dict[str, str]]] = [
    (re.compile(r"OpenSSH.*Ubuntu", re.I),  {"os": "Ubuntu", "device_type": "server"}),
    (re.compile(r"OpenSSH.*Debian", re.I),  {"os": "Debian", "device_type": "server"}),
    (re.compile(r"OpenSSH",          re.I), {"device_type": "server"}),
    (re.compile(r"Cisco",            re.I), {"vendor": "Cisco"}),
    (re.compile(r"ROSSSH|RouterOS",  re.I), {"vendor": "MikroTik", "device_type": "router"}),
    (re.compile(r"dropbear",         re.I), {"device_type": "router"}),  # common on embedded
    (re.compile(r"Juniper",          re.I), {"vendor": "Juniper"}),
    (re.compile(r"SSH.*FortiGate",   re.I), {"vendor": "Fortinet", "device_type": "firewall"}),
]


# Well-known ports → protocol hints
_PORT_PROTOCOLS = {
    21: "ftp",
    22: "ssh",
    23: "telnet",
    25: "smtp",
    53: "dns",
    80: "http",
    110: "pop3",
    111: "rpcbind",
    135: "msrpc",
    139: "smb",
    143: "imap",
    161: "snmp",
    389: "ldap",
    443: "https",
    445: "smb",
    514: "syslog",
    515: "lpd",
    554: "rtsp",
    631: "ipp",
    902: "vmware",
    993: "imaps",
    995: "pop3s",
    1433: "mssql",
    1521: "oracle",
    1883: "mqtt",
    2049: "nfs",
    3000: "http-alt",
    3128: "proxy",
    3306: "mysql",
    3389: "rdp",
    4443: "https-alt",
    5060: "sip",
    5432: "postgres",
    5601: "kibana",
    5900: "vnc",
    5985: "winrm",
    5986: "winrm-https",
    6379: "redis",
    8000: "http-alt",
    8080: "http-alt",
    8083: "http-alt",
    8086: "influxdb",
    8088: "http-alt",
    8123: "clickhouse",
    8443: "https-alt",
    8530: "wsus",
    8728: "mikrotik-api",
    8729: "mikrotik-api-ssl",
    8888: "http-alt",
    9000: "http-alt",
    9090: "prometheus",
    9100: "raw-print",
    9200: "elasticsearch",
}


def _device_type_from_ports(ports: list[int]) -> Optional[str]:
    """Guess device type from open ports."""
    s = set(ports)
    if 9100 in s or 631 in s or 515 in s:
        return "printer"
    if 161 in s and any(p in s for p in (22, 23, 80, 443)) and not (5985 in s or 5986 in s):
        return None  # leave for SNMP to refine
    if 5985 in s or 5986 in s or 3389 in s or 445 in s:
        return "server"
    if 1433 in s or 3306 in s or 5432 in s or 6379 in s or 9200 in s:
        return "server"
    return None


# ────────────────────────────────────────────────────────────────────
# Aggregator
# ────────────────────────────────────────────────────────────────────
def identify(probes: dict[str, Any], protocols_requested: list[str]) -> dict[str, Any]:
    """Combine all probe outputs into a single device identity dict."""
    out: dict[str, Any] = {
        "vendor": None,
        "device_type": None,
        "model": None,
        "os": None,
        "os_version": None,
        "hostname": None,
        "sys_name": None,
        "sys_object_id": None,
        "serial_number": None,
        "mac_address": None,
        "open_ports": [],
        "protocols_detected": [],
        "response_time_ms": None,
        "confidence_score": 0,
        "credential_status": "not_tested",
        "credential_used": None,
        "windows_credential_used": None,
        "fqdn": None,
        "raw_data": {},
    }

    icmp = probes.get("icmp")
    tcp = probes.get("tcp")
    http_results = probes.get("http", [])      # list — one per port we tried
    https_results = probes.get("https", [])
    ssh = probes.get("ssh")
    snmp_results = probes.get("snmp", [])      # list — one per credential tried
    winrm_results = probes.get("winrm", [])
    rdns = probes.get("rdns")
    mac = probes.get("mac")

    confidence = 0

    # ── ICMP ──
    if icmp and icmp.get("responsive"):
        out["response_time_ms"] = icmp["data"].get("rtt_ms")
        out["protocols_detected"].append("icmp")
        confidence += 10

    # ── TCP ──
    if tcp:
        ports = tcp["data"].get("open", []) or []
        out["open_ports"] = sorted(ports)
        if ports:
            confidence += 10
            for p in ports:
                proto = _PORT_PROTOCOLS.get(p)
                if proto and proto not in out["protocols_detected"]:
                    out["protocols_detected"].append(proto)

    # ── Reverse DNS / MAC ──
    if rdns:
        out["fqdn"] = rdns
        out["hostname"] = rdns.split(".")[0]
        confidence += 5
    if mac:
        out["mac_address"] = mac.upper()
        confidence += 10

    # ── SSH banner ──
    if ssh and ssh.get("responsive"):
        if "ssh" not in out["protocols_detected"]:
            out["protocols_detected"].append("ssh")
        banner = ssh["data"].get("banner")
        if banner:
            out["raw_data"]["ssh_banner"] = banner
            for pat, hints in _SSH_PATTERNS:
                if pat.search(banner):
                    for k, v in hints.items():
                        if out[k] is None:
                            out[k] = v
                    break
            confidence += 10

    # ── HTTP / HTTPS ──
    for http_r in [*http_results, *https_results]:
        if not http_r or not http_r.get("responsive"):
            continue
        scheme = http_r["protocol"]
        if scheme not in out["protocols_detected"]:
            out["protocols_detected"].append(scheme)
        d = http_r["data"]
        out["raw_data"].setdefault("http", []).append({
            "port": d.get("port"),
            "scheme": scheme,
            "server": d.get("server"),
            "title": d.get("title"),
        })
        server = d.get("server") or ""
        title = d.get("title") or ""
        for pat, hints in _HTTP_SERVER_PATTERNS:
            if pat.search(server):
                for k, v in hints.items():
                    if out[k] is None:
                        out[k] = v
        for pat, hints in _TITLE_PATTERNS:
            if pat.search(title):
                for k, v in hints.items():
                    if out[k] is None:
                        out[k] = v
        if d.get("tls_subject"):
            # Try to extract CN as hostname hint
            m = re.search(r"CN=([^,]+)", d["tls_subject"])
            if m and out["hostname"] is None:
                cn = m.group(1).strip()
                if "." in cn:
                    out["fqdn"] = out["fqdn"] or cn
                    out["hostname"] = cn.split(".")[0]
                else:
                    out["hostname"] = cn
        confidence += 15

    # ── SNMP (the gold standard for network gear) ──
    for snmp_r in snmp_results:
        if not snmp_r or not snmp_r.get("responsive"):
            continue
        if "snmp" not in out["protocols_detected"]:
            out["protocols_detected"].append("snmp")
        d = snmp_r["data"]
        out["sys_object_id"] = out["sys_object_id"] or d.get("sys_object_id")
        out["sys_name"] = out["sys_name"] or d.get("sys_name")
        if d.get("sys_name") and not out["hostname"]:
            out["hostname"] = d["sys_name"]
        out["raw_data"]["sys_descr"] = d.get("sys_descr")
        out["raw_data"]["sys_location"] = d.get("sys_location")
        out["credential_status"] = "valid"
        out["credential_used"] = d.get("credential_id")
        confidence += 30
        # Vendor from sysObjectID
        oid = d.get("sys_object_id") or ""
        for prefix, vendor, dtype in _SYS_OBJECT_PREFIXES:
            if oid.startswith(prefix):
                out["vendor"] = out["vendor"] or vendor
                out["device_type"] = out["device_type"] or dtype
                break
        # Enrich from sysDescr
        descr = d.get("sys_descr") or ""
        for pat, hints in _SYS_DESCR_PATTERNS:
            if pat.search(descr):
                for k, v in hints.items():
                    if out[k] is None:
                        out[k] = v
        break  # First valid SNMP cred wins

    # ── WinRM (Windows) ──
    for w in winrm_results:
        if not w or not w.get("responsive"):
            # Track the first non-permission error so the UI can surface it
            if w and w.get("state") in ("invalid", "permission_issue"):
                out["credential_status"] = w["state"]
            continue
        d = w["data"]
        if "winrm" not in out["protocols_detected"]:
            out["protocols_detected"].append("winrm")
        out["vendor"] = out["vendor"] or d.get("vendor") or "Microsoft"
        out["model"] = out["model"] or d.get("model")
        out["os"] = out["os"] or d.get("os") or "Windows"
        out["os_version"] = out["os_version"] or d.get("os_version")
        out["serial_number"] = out["serial_number"] or d.get("serial")
        out["hostname"] = out["hostname"] or d.get("hostname")
        out["device_type"] = out["device_type"] or "server"
        out["windows_credential_used"] = d.get("credential_id")
        if out["credential_status"] == "not_tested":
            out["credential_status"] = "valid"
        confidence += 30
        break

    # ── Default device type from open ports if still unknown ──
    if out["device_type"] is None:
        dt = _device_type_from_ports(out["open_ports"])
        if dt:
            out["device_type"] = dt

    # ── If SNMP was requested but no credential worked ──
    if "snmp" in protocols_requested and snmp_results \
            and not any(r.get("responsive") for r in snmp_results):
        # SNMP port open but no credentials valid → mark partial/invalid
        any_invalid = any(r and r.get("state") in ("invalid", "permission_issue") for r in snmp_results)
        if any_invalid:
            out["credential_status"] = "invalid"

    out["confidence_score"] = min(confidence, 100)
    return out
