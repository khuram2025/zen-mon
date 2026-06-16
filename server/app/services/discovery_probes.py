"""Real network probes used by the Discovery executor.

Each function returns a dict shaped roughly like:

    {
        "responsive": bool,           # did the probe succeed?
        "protocol": "icmp|tcp|http|...",
        "data": {... protocol-specific facts ...},
        "error": str | None,
    }

The executor merges these into a single DiscoveryResult row.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import ssl
import subprocess
from typing import Any, Optional

import httpx


# ────────────────────────────────────────────────────────────────────
# ICMP
# ────────────────────────────────────────────────────────────────────
async def icmp_ping(ip: str, timeout_s: float = 1.0) -> dict[str, Any]:
    """Shell out to /usr/bin/ping. Returns alive + rtt_ms."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(max(1, int(timeout_s))), "-n", ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 1.5)
        if proc.returncode != 0:
            return {"responsive": False, "protocol": "icmp", "data": {}, "error": "no reply"}
        m = re.search(r"time=([\d.]+)\s*ms", stdout.decode("utf-8", "ignore"))
        rtt = round(float(m.group(1)), 2) if m else None
        return {"responsive": True, "protocol": "icmp", "data": {"rtt_ms": rtt}, "error": None}
    except FileNotFoundError:
        return {"responsive": False, "protocol": "icmp", "data": {}, "error": "ping binary missing"}
    except Exception as e:
        return {"responsive": False, "protocol": "icmp", "data": {}, "error": str(e)}


# ────────────────────────────────────────────────────────────────────
# TCP port probe
# ────────────────────────────────────────────────────────────────────
async def tcp_open(ip: str, port: int, timeout_s: float = 1.5) -> bool:
    """Return True if a TCP handshake completes."""
    try:
        fut = asyncio.open_connection(ip, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout_s)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def tcp_scan(ip: str, ports: list[int], timeout_s: float = 1.5) -> dict[str, Any]:
    """Probe a list of ports concurrently. Returns the open subset."""
    if not ports:
        return {"responsive": False, "protocol": "tcp", "data": {"open": []}, "error": None}
    results = await asyncio.gather(*[tcp_open(ip, p, timeout_s) for p in ports])
    open_ports = [p for p, ok in zip(ports, results) if ok]
    return {
        "responsive": bool(open_ports),
        "protocol": "tcp",
        "data": {"open": open_ports},
        "error": None,
    }


# ────────────────────────────────────────────────────────────────────
# Reverse DNS
# ────────────────────────────────────────────────────────────────────
async def reverse_dns(ip: str) -> Optional[str]:
    try:
        loop = asyncio.get_event_loop()
        host, _, _ = await loop.run_in_executor(None, socket.gethostbyaddr, ip)
        return host
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────
# HTTP / HTTPS fingerprint
# ────────────────────────────────────────────────────────────────────
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


async def http_probe(ip: str, port: int = 80, https: bool = False,
                     timeout_s: float = 3.0) -> dict[str, Any]:
    """GET / on an IP, extract Server header, title, and TLS cert details.

    Credential-less — fingerprinting only. Vendor identification happens in
    the heuristics layer.
    """
    scheme = "https" if https else "http"
    url = f"{scheme}://{ip}:{port}/"
    data: dict[str, Any] = {"port": port, "scheme": scheme}
    try:
        # verify=False because management UIs almost always use self-signed certs
        async with httpx.AsyncClient(
            timeout=timeout_s, verify=False, follow_redirects=False,
            headers={"User-Agent": "ZenPlus-Discovery/1.0"},
        ) as client:
            r = await client.get(url)
            data["status"] = r.status_code
            data["server"] = r.headers.get("server")
            data["powered_by"] = r.headers.get("x-powered-by")
            data["www_authenticate"] = r.headers.get("www-authenticate")
            data["content_type"] = r.headers.get("content-type", "")
            # Body parsing — keep small to avoid huge downloads
            body = r.text[:8192] if r.text else ""
            data["body_excerpt"] = body
            m = _TITLE_RE.search(body)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()
                data["title"] = title[:200] or None

        # TLS cert info for HTTPS
        if https:
            try:
                loop = asyncio.get_event_loop()
                cert = await loop.run_in_executor(None, _get_tls_cert, ip, port, timeout_s)
                if cert:
                    data["tls_subject"] = cert.get("subject")
                    data["tls_issuer"] = cert.get("issuer")
                    data["tls_san"] = cert.get("san", [])
            except Exception:
                pass

        return {"responsive": True, "protocol": scheme, "data": data, "error": None}
    except httpx.HTTPError as e:
        return {"responsive": False, "protocol": scheme, "data": data, "error": str(e)}
    except Exception as e:
        return {"responsive": False, "protocol": scheme, "data": data, "error": str(e)}


def _get_tls_cert(ip: str, port: int, timeout_s: float) -> Optional[dict[str, Any]]:
    """Synchronous helper to fetch a TLS cert and extract CN/SAN/issuer."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((ip, port), timeout=timeout_s) as raw:
            with ctx.wrap_socket(raw, server_hostname=ip) as s:
                cert = s.getpeercert()
    except Exception:
        return None
    if not cert:
        # Some servers don't return parsed cert when verify=NONE — fall back
        # to raw DER → x509 parse.
        try:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend
            with socket.create_connection((ip, port), timeout=timeout_s) as raw:
                with ctx.wrap_socket(raw, server_hostname=ip) as s:
                    der = s.getpeercert(binary_form=True)
            x = x509.load_der_x509_certificate(der, default_backend())
            subj = x.subject.rfc4514_string()
            issuer = x.issuer.rfc4514_string()
            try:
                san_ext = x.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                san = [str(n) for n in san_ext.value]
            except Exception:
                san = []
            return {"subject": subj, "issuer": issuer, "san": san}
        except Exception:
            return None
    # cert dict format: ((('CN','foo'),),), ...
    def _flatten(rdns):
        out = []
        for rdn in rdns or ():
            for k, v in rdn:
                out.append(f"{k}={v}")
        return ", ".join(out)
    san = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
    return {
        "subject": _flatten(cert.get("subject")),
        "issuer": _flatten(cert.get("issuer")),
        "san": san,
    }


# ────────────────────────────────────────────────────────────────────
# SSH banner
# ────────────────────────────────────────────────────────────────────
async def ssh_banner(ip: str, port: int = 22, timeout_s: float = 2.0) -> dict[str, Any]:
    """Open a TCP connection to the SSH port and read the server banner.

    Real SSH servers send their identification string immediately upon
    connect, e.g. "SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.1\\r\\n". No
    handshake or credentials required.
    """
    try:
        fut = asyncio.open_connection(ip, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout_s)
        banner = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        decoded = banner.decode("utf-8", "ignore").strip()
        if not decoded.startswith("SSH-"):
            return {"responsive": False, "protocol": "ssh", "data": {"raw": decoded},
                    "error": "not an SSH banner"}
        # Format: SSH-<protocol>-<software>
        parts = decoded.split("-", 2)
        software = parts[2] if len(parts) > 2 else ""
        return {
            "responsive": True,
            "protocol": "ssh",
            "data": {"banner": decoded, "software": software},
            "error": None,
        }
    except Exception as e:
        return {"responsive": False, "protocol": "ssh", "data": {}, "error": str(e)}


def _ssh_auth_check_sync(
    host: str,
    port: int,
    username: str,
    password: str,
    enable: str,
    protocol: str,
    timeout_s: float,
) -> tuple[bool, str | None, str | None, str | None]:
    """Blocking SSH/Telnet login test — run via asyncio.to_thread."""
    try:
        from netmiko import ConnectHandler, SSHDetect
    except ImportError:
        return False, None, None, "netmiko not installed"

    conn_timeout = max(5, min(int(timeout_s), 30))
    base: dict[str, Any] = {
        "host": host,
        "username": username or "",
        "password": password or "",
        "port": port or 22,
        "conn_timeout": conn_timeout,
        "timeout": conn_timeout + 10,
        "fast_cli": False,
    }
    if enable:
        base["secret"] = enable

    try:
        if (protocol or "ssh").lower() == "telnet":
            device_type = "cisco_ios_telnet"
            conn = ConnectHandler(**{**base, "device_type": device_type})
        else:
            guesser = SSHDetect(**{**base, "device_type": "autodetect"})
            device_type = guesser.autodetect() or "linux"
            conn = ConnectHandler(**{**base, "device_type": device_type})
        try:
            if enable:
                try:
                    conn.enable()
                except Exception:
                    pass
            try:
                prompt = conn.find_prompt()
            except Exception:
                prompt = ""
            return True, device_type, prompt or None, None
        finally:
            try:
                conn.disconnect()
            except Exception:
                pass
    except Exception as e:
        msg = str(e).splitlines()[0][:400] if str(e) else "authentication failed"
        return False, None, None, msg


async def ssh_auth_probe(ip: str, credential: dict, timeout_s: float = 8.0) -> dict[str, Any]:
    """Try a saved NCM connection profile against an IP via SSH or Telnet."""
    port = int(credential.get("port") or 22)
    ok, platform, prompt, err = await asyncio.to_thread(
        _ssh_auth_check_sync,
        ip,
        port,
        credential.get("username") or "",
        credential.get("password") or "",
        credential.get("enable_password") or "",
        credential.get("protocol") or "ssh",
        timeout_s,
    )
    if ok:
        return {
            "responsive": True,
            "protocol": "ssh",
            "data": {
                "credential_id": credential.get("id"),
                "credential_name": credential.get("name"),
                "platform": platform,
                "prompt": prompt,
                "authenticated": True,
            },
            "error": None,
            "state": "valid",
        }
    return {
        "responsive": False,
        "protocol": "ssh",
        "data": {"credential_id": credential.get("id")},
        "error": err,
        "state": "invalid",
    }


# ────────────────────────────────────────────────────────────────────
# SNMP — wraps the existing /api/v1/snmp.py helpers
# ────────────────────────────────────────────────────────────────────
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT = "1.3.6.1.2.1.1.2.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_LOCATION = "1.3.6.1.2.1.1.6.0"


async def snmp_probe(ip: str, credential: dict, timeout_s: float = 2.0) -> dict[str, Any]:
    """Try a single SNMP credential against an IP.

    ``credential`` is the row from snmp_credentials, with passphrases already
    decrypted by the caller.
    """
    from app.api.v1.snmp import _snmpget_detail

    version = credential.get("snmp_version", "2c")
    port = credential.get("port", 161)
    timeout_ms = int(timeout_s * 1000)
    oids = [SYS_DESCR, SYS_OBJECT, SYS_NAME, SYS_LOCATION]
    try:
        result, err = await _snmpget_detail(
            ip=ip,
            community=credential.get("community"),
            version=version,
            port=port,
            timeout_ms=timeout_ms,
            oids=oids,
            v3_username=credential.get("v3_username"),
            v3_security_level=credential.get("v3_security_level"),
            v3_auth_protocol=credential.get("v3_auth_protocol"),
            v3_auth_passphrase=credential.get("v3_auth_passphrase"),
            v3_priv_protocol=credential.get("v3_priv_protocol"),
            v3_priv_passphrase=credential.get("v3_priv_passphrase"),
            v3_context=credential.get("v3_context"),
        )
        if err:
            # Classify the error so the UI can show the right state.
            low = err.lower()
            if "timeout" in low or "no response" in low:
                state = "no_response"
            elif "authent" in low or "authorization" in low or "no such" in low:
                state = "invalid"
            else:
                state = "invalid"
            return {
                "responsive": False, "protocol": "snmp",
                "data": {"credential_id": credential.get("id")},
                "error": err, "state": state,
            }
        sys_descr = result.get(SYS_DESCR) if result else None
        sys_object = result.get(SYS_OBJECT) if result else None
        sys_name = result.get(SYS_NAME) if result else None
        sys_loc = result.get(SYS_LOCATION) if result else None
        return {
            "responsive": True,
            "protocol": "snmp",
            "data": {
                "credential_id": credential.get("id"),
                "sys_descr": sys_descr,
                "sys_object_id": sys_object,
                "sys_name": sys_name,
                "sys_location": sys_loc,
            },
            "error": None,
            "state": "valid",
        }
    except Exception as e:
        return {"responsive": False, "protocol": "snmp", "data": {}, "error": str(e), "state": "invalid"}


# ────────────────────────────────────────────────────────────────────
# WinRM — real Windows discovery via pywinrm
# ────────────────────────────────────────────────────────────────────
async def winrm_probe(ip: str, credential: dict, timeout_s: float = 5.0) -> dict[str, Any]:
    """Try a single Windows credential against an IP via WinRM.

    Pulls the OS caption, hostname, and a few inventory fields if the
    credential is valid.
    """
    try:
        import winrm  # provided by pywinrm
        from winrm.exceptions import (
            InvalidCredentialsError,
            WinRMTransportError,
            WinRMOperationTimeoutError,
        )
    except ImportError:
        return {
            "responsive": False, "protocol": "winrm", "data": {},
            "error": "pywinrm not installed", "state": "invalid",
        }

    username = credential.get("username", "")
    domain = credential.get("domain") or ""
    full_user = f"{domain}\\{username}" if domain else username
    password = credential.get("password", "")
    transport_choice = credential.get("transport", "http")
    port = credential.get("port", 5985 if transport_choice == "http" else 5986)
    auth = credential.get("auth_method", "ntlm")

    endpoint = f"{transport_choice}://{ip}:{port}/wsman"

    # PowerShell command — single round trip, returns CSV
    cmd = (
        "$o = Get-CimInstance Win32_OperatingSystem; "
        "$c = Get-CimInstance Win32_ComputerSystem; "
        "$b = Get-CimInstance Win32_BIOS; "
        "[pscustomobject]@{ "
        "  Hostname=$env:COMPUTERNAME; "
        "  OS=$o.Caption; "
        "  Version=$o.Version; "
        "  Arch=$o.OSArchitecture; "
        "  Vendor=$c.Manufacturer; "
        "  Model=$c.Model; "
        "  Serial=$b.SerialNumber; "
        "  Domain=$c.Domain; "
        "} | ConvertTo-Json -Compress"
    )

    def _run():
        # pywinrm is synchronous — wrap in thread executor
        s = winrm.Session(
            endpoint, auth=(full_user, password),
            transport=auth,
            server_cert_validation="ignore" if not credential.get("ssl_verify") else "validate",
            operation_timeout_sec=int(timeout_s),
            read_timeout_sec=int(timeout_s + 2),
        )
        return s.run_ps(cmd)

    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=timeout_s + 3)
        if result.status_code != 0:
            err = result.std_err.decode("utf-8", "ignore")[:500] if result.std_err else "non-zero exit"
            return {"responsive": False, "protocol": "winrm", "data": {},
                    "error": err, "state": "permission_issue"}
        import json
        out = result.std_out.decode("utf-8", "ignore").strip()
        info = json.loads(out) if out else {}
        return {
            "responsive": True, "protocol": "winrm",
            "data": {
                "credential_id": credential.get("id"),
                "hostname": info.get("Hostname"),
                "os": info.get("OS"),
                "os_version": info.get("Version"),
                "arch": info.get("Arch"),
                "vendor": info.get("Vendor"),
                "model": info.get("Model"),
                "serial": info.get("Serial"),
                "domain": info.get("Domain"),
            },
            "error": None, "state": "valid",
        }
    except InvalidCredentialsError as e:
        return {"responsive": False, "protocol": "winrm", "data": {},
                "error": str(e), "state": "invalid"}
    except (WinRMTransportError, WinRMOperationTimeoutError) as e:
        return {"responsive": False, "protocol": "winrm", "data": {},
                "error": str(e), "state": "no_response"}
    except asyncio.TimeoutError:
        return {"responsive": False, "protocol": "winrm", "data": {},
                "error": "WinRM timeout", "state": "no_response"}
    except Exception as e:
        return {"responsive": False, "protocol": "winrm", "data": {},
                "error": str(e), "state": "invalid"}


# ────────────────────────────────────────────────────────────────────
# ARP — MAC address lookup from kernel table
# ────────────────────────────────────────────────────────────────────
def _read_arp_table() -> dict[str, str]:
    """Read /proc/net/arp into {ip: mac}. Linux-only."""
    out: dict[str, str] = {}
    try:
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:  # skip header
                parts = line.split()
                if len(parts) >= 4:
                    ip, _hwtype, flags, mac = parts[0], parts[1], parts[2], parts[3]
                    if mac != "00:00:00:00:00:00" and flags != "0x0":
                        out[ip] = mac.lower()
    except Exception:
        pass
    return out


_ARP_CACHE: dict[str, str] = {}
_ARP_CACHE_AT: float = 0.0


async def arp_lookup(ip: str) -> Optional[str]:
    """Best-effort MAC resolution.

    First refreshes the kernel ARP table by sending a tiny UDP packet
    (this triggers an ARP exchange on most kernels for L2-local hosts),
    then reads /proc/net/arp.
    """
    import time as _time
    global _ARP_CACHE, _ARP_CACHE_AT

    # Touch the host so the kernel populates ARP
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _arp_touch, ip)
    except Exception:
        pass

    now = _time.monotonic()
    if now - _ARP_CACHE_AT > 2.0:
        _ARP_CACHE = _read_arp_table()
        _ARP_CACHE_AT = now
    return _ARP_CACHE.get(ip)


def _arp_touch(ip: str):
    """Open and close a UDP socket on a closed port to trigger ARP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        try:
            s.sendto(b"\x00", (ip, 9))
        except Exception:
            pass
        s.close()
    except Exception:
        pass
