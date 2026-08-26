"""Appliance reachability, DNS, TLS, local-port, and clock diagnostics."""

from __future__ import annotations

import configparser
import json
import shutil
import socket
import ssl
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import CollectorContext, CollectorResult


LOCAL_PORTS = (
    ("nginx_https", "127.0.0.1", 443),
    ("api", "127.0.0.1", 8000),
    ("postgres", "127.0.0.1", 5432),
    ("redis", "127.0.0.1", 6379),
    ("clickhouse_http", "127.0.0.1", 8123),
)


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="reachability")

    local = [_tcp_probe(name, host, port, timeout=2) for name, host, port in LOCAL_PORTS]
    result.files["reachability/local-ports.json"] = _dump({"probes": local})
    if any(not row["reachable"] for row in local):
        result.warn("one or more local appliance ports are unreachable")

    controller = _controller_probe(ctx)
    result.files["reachability/update-controller.json"] = _dump(controller)
    if controller.get("configured") and not controller.get("tcp_reachable", False):
        result.warn("configured update controller is unreachable")

    result.files["reachability/time-sync.json"] = _dump(_time_sync())
    result.files["reachability/hostname-resolution.json"] = _dump(_hostname_resolution())
    return result


def _tcp_probe(name: str, host: str, port: int, *, timeout: int) -> dict:
    started = datetime.now(timezone.utc)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            return {
                "name": name, "host": host, "port": port,
                "reachable": True, "connect_ms": elapsed,
            }
    except OSError as exc:
        return {
            "name": name, "host": host, "port": port,
            "reachable": False, "error": f"{exc.__class__.__name__}: {exc}",
        }


def _controller_probe(ctx: CollectorContext) -> dict:
    conf_path = ctx.updater_root / "config" / "agent.conf"
    if not conf_path.exists():
        return {"configured": False, "error": f"missing {conf_path}"}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(conf_path, encoding="utf-8")
        raw_url = parser.get("server", "url", fallback="").strip()
    except (OSError, configparser.Error) as exc:
        return {"configured": False, "error": f"cannot parse agent.conf: {exc}"}
    try:
        parsed = urlsplit(raw_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("controller URL must use http or https and include a host")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, TypeError) as exc:
        return {"configured": False, "error": str(exc)}

    # Never copy URL userinfo, query strings, or paths into diagnostics.
    safe_origin = f"{parsed.scheme}://{host}:{port}"
    out = {
        "configured": True,
        "origin": safe_origin,
        "host": host,
        "port": port,
        "dns": _dns_probe(host),
    }
    tcp = _tcp_probe("update_controller", host, port, timeout=5)
    out["tcp_reachable"] = tcp["reachable"]
    out["connect_ms"] = tcp.get("connect_ms")
    if "error" in tcp:
        out["tcp_error"] = tcp["error"]
    if tcp["reachable"] and parsed.scheme == "https":
        out["tls"] = _tls_probe(host, port)
    return out


def _dns_probe(host: str) -> dict:
    try:
        rows = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        addresses = sorted({row[4][0] for row in rows})
        return {"resolved": bool(addresses), "addresses": addresses[:20]}
    except OSError as exc:
        return {"resolved": False, "addresses": [], "error": f"{exc.__class__.__name__}: {exc}"}


def _tls_probe(host: str, port: int) -> dict:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=host) as wrapped:
                cert = wrapped.getpeercert()
                cipher = wrapped.cipher()
                return {
                    "trusted": True,
                    "protocol": wrapped.version(),
                    "cipher": cipher[0] if cipher else None,
                    "not_before": cert.get("notBefore"),
                    "not_after": cert.get("notAfter"),
                    "subject_alt_names": [value for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"][:20],
                }
    except (OSError, ssl.SSLError) as exc:
        return {"trusted": False, "error": f"{exc.__class__.__name__}: {exc}"}


def _time_sync() -> dict:
    return {
        "utc_now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timedatectl": _command([
            "timedatectl", "show", "--property=Timezone", "--property=NTPSynchronized",
            "--property=NTP", "--property=TimeUSec",
        ], timeout=5),
        "chronyc_tracking": _command(["chronyc", "tracking"], timeout=5),
        "chronyc_sources": _command(["chronyc", "sources", "-n"], timeout=5),
    }


def _hostname_resolution() -> dict:
    hostname = socket.gethostname()
    fqdn = socket.getfqdn()
    return {"hostname": hostname, "fqdn": fqdn, "dns": _dns_probe(fqdn or hostname)}


def _command(cmd: list[str], *, timeout: int) -> dict:
    if not shutil.which(cmd[0]):
        return {"available": False, "command": " ".join(cmd), "error": "not found"}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "available": True,
            "command": " ".join(cmd),
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-64 * 1024 :].strip(),
            "stderr": proc.stderr[-16 * 1024 :].strip(),
        }
    except subprocess.TimeoutExpired:
        return {"available": True, "command": " ".join(cmd), "exit_code": -1, "error": "timeout"}


def _dump(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
