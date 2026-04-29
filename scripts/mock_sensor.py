#!/usr/bin/env python3
"""ZenPlus mock sensor — Phase 1 testing harness.

Simulates a remote sensor end-to-end against the central server's
sensor API. The "real" sensor is a Go binary in remote-mode (Phase 2);
this script implements the same protocol in Python so we can validate
the API surface, dashboard wiring, and the overall data flow today.

Behavior:
    1. POST /sensor/enroll  — exchange enrollment token for an api_key.
       Persists state to ~/.zenplus-sensor/state.json so reruns skip enroll.
    2. Loop forever:
       * Heartbeat every 30s (POST /sensor/heartbeat)
       * Pull config every 60s (GET /sensor/config, ETag-aware)
       * For each assigned device: tcp/icmp probe → POST /sensor/results/ping
       * For each assigned http service-check: HTTP GET → POST /sensor/results/service
       * For each assigned tcp service-check: TCP connect → POST /sensor/results/service

Required env:
    ZENPLUS_SERVER_URL           e.g. http://10.12.50.81
    ZENPLUS_ENROLLMENT_TOKEN     one-time token from "Add Sensor" dialog
    ZENPLUS_SENSOR_NAME          purely cosmetic (display)

Optional env:
    ZENPLUS_STATE_DIR            default ~/.zenplus-sensor
    ZENPLUS_VERIFY_TLS           default 1; set 0 to skip cert verify
"""

from __future__ import annotations

import json
import os
import platform
import random
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

VERSION = "mock-0.1.0"
HEARTBEAT_S = 30
CONFIG_S = 60
PING_BATCH_S = 30
SERVICE_BATCH_S = 30


def env(key: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(key, default)
    if required and not v:
        die(f"Missing required env var: {key}")
    return v or ""


def die(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr)
    sys.exit(1)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── HTTP helpers (stdlib only, so no pip install needed) ─────────────

class Client:
    def __init__(self, base_url: str, sensor_id: str | None = None, api_key: str | None = None, verify_tls: bool = True):
        self.base_url = base_url.rstrip("/")
        self.sensor_id = sensor_id
        self.api_key = api_key
        self.ctx = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()

    def _request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict | None, dict]:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if self.sensor_id:
            req.add_header("X-Sensor-Id", self.sensor_id)
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=15, context=self.ctx) as resp:
                raw = resp.read()
                resp_headers = dict(resp.getheaders())
                if not raw:
                    return resp.status, None, resp_headers
                try:
                    return resp.status, json.loads(raw.decode("utf-8")), resp_headers
                except json.JSONDecodeError:
                    return resp.status, {"raw": raw.decode("utf-8", errors="replace")}, resp_headers
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
            except Exception:
                err_body = None
            return e.code, err_body, dict(e.headers or {})

    def post(self, path: str, body: dict, headers: dict | None = None):
        return self._request("POST", path, body, headers)

    def get(self, path: str, headers: dict | None = None):
        return self._request("GET", path, None, headers)


# ── State persistence ────────────────────────────────────────────────

class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                self.data = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))


# ── Probes ───────────────────────────────────────────────────────────

def tcp_probe(host: str, port: int, timeout: float = 3.0) -> tuple[bool, float]:
    """Returns (ok, ms). Used as our ICMP stand-in (no raw socket needed)."""
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ms = (time.monotonic() - t0) * 1000.0
            return True, ms
    except Exception:
        return False, 0.0


def http_probe(url: str, method: str = "GET", timeout: float = 5.0) -> tuple[bool, float, int | None, str]:
    """Returns (ok, ms, status_code, error)."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, method=method or "GET")
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            ms = (time.monotonic() - t0) * 1000.0
            return 200 <= resp.status < 400, ms, resp.status, ""
    except urllib.error.HTTPError as e:
        ms = (time.monotonic() - t0) * 1000.0
        return False, ms, e.code, str(e)
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000.0
        return False, ms, None, str(e)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Main loop ────────────────────────────────────────────────────────

def main() -> None:
    server_url = env("ZENPLUS_SERVER_URL", required=True)
    sensor_name = env("ZENPLUS_SENSOR_NAME", "mock-sensor")
    enrollment_token = env("ZENPLUS_ENROLLMENT_TOKEN")
    verify_tls = env("ZENPLUS_VERIFY_TLS", "1") not in ("0", "false", "no")

    state_dir = Path(env("ZENPLUS_STATE_DIR", str(Path.home() / ".zenplus-sensor")))
    state = State(state_dir / f"{sensor_name}.json")

    client = Client(server_url, verify_tls=verify_tls)

    # Enroll if we don't have an api key yet.
    if not state.data.get("api_key") or not state.data.get("sensor_id"):
        if not enrollment_token:
            die("No saved api_key and no ZENPLUS_ENROLLMENT_TOKEN provided")
        log(f"Enrolling with {server_url} as '{sensor_name}'…")
        status, body, _ = client.post("/api/v1/sensor/enroll", {
            "enrollment_token": enrollment_token,
            "hostname": socket.gethostname(),
            "os_info": f"{platform.system()} {platform.release()}",
            "version": VERSION,
        })
        if status != 200 or not body:
            die(f"Enroll failed: {status} {body}")
        state.data["sensor_id"] = body["sensor_id"]
        state.data["api_key"] = body["api_key"]
        state.save()
        log(f"Enrolled. sensor_id={state.data['sensor_id']}")

    client.sensor_id = state.data["sensor_id"]
    client.api_key = state.data["api_key"]

    # Initial state for ping/service loops
    started_at = time.time()
    last_heartbeat = 0.0
    last_config = 0.0
    last_ping = 0.0
    last_service = 0.0
    config_etag: str | None = None
    config: dict = {"devices": [], "service_checks": []}

    log(f"Running. server={server_url} name={sensor_name} version={VERSION}")
    log("Ctrl-C to stop.")

    while True:
        now = time.time()

        # Heartbeat
        if now - last_heartbeat >= HEARTBEAT_S:
            status, body, _ = client.post("/api/v1/sensor/heartbeat", {
                "version": VERSION,
                "uptime_seconds": int(now - started_at),
                "queue_depth": 0,
                "queue_dropped_count": 0,
                "hostname": socket.gethostname(),
                "os_info": f"{platform.system()} {platform.release()}",
            })
            if status == 200:
                log(f"heartbeat ok (server etag={body.get('config_etag') if body else '?'})")
            else:
                log(f"heartbeat FAIL {status}: {body}")
                if status in (401, 403):
                    log("auth lost — wiping state, will re-enroll on next run")
                    state.data = {}
                    state.save()
                    return
            last_heartbeat = now

        # Config sync
        if now - last_config >= CONFIG_S:
            headers = {}
            if config_etag:
                headers["If-None-Match"] = config_etag
            status, body, resp_headers = client.get("/api/v1/sensor/config", headers=headers)
            if status == 304:
                pass
            elif status == 200 and body:
                config_etag = body.get("etag")
                config = {
                    "devices": body.get("devices", []),
                    "service_checks": body.get("service_checks", []),
                }
                log(f"config: {len(config['devices'])} devices, {len(config['service_checks'])} checks")
            else:
                log(f"config FAIL {status}: {body}")
            last_config = now

        # Push ping results
        if now - last_ping >= PING_BATCH_S and config["devices"]:
            items = []
            for d in config["devices"]:
                if not d.get("ping_enabled"):
                    continue
                ip = d["ip_address"]
                # Try a TCP connect on common ports as ICMP stand-in.
                # Falls back to assume "up" if any of these connect.
                up = False
                rtt = 0.0
                for p in (80, 443, 22, 53):
                    ok, ms = tcp_probe(ip, p, timeout=1.5)
                    if ok:
                        up, rtt = True, ms
                        break
                if not up:
                    # Last resort: DNS resolve + tiny rand RTT to keep timeline lively.
                    try:
                        socket.gethostbyaddr(ip)
                        up = True
                        rtt = random.uniform(1.0, 5.0)
                    except Exception:
                        up = False
                        rtt = 0.0
                items.append({
                    "device_id": d["id"],
                    "timestamp": now_iso(),
                    "is_up": up,
                    "rtt_ms": rtt,
                    "ip_address": ip,
                })
            if items:
                status, body, _ = client.post("/api/v1/sensor/results/ping", {"items": items})
                log(f"ping batch: {len(items)} items → {status}")
            last_ping = now

        # Push service-check results
        if now - last_service >= SERVICE_BATCH_S and config["service_checks"]:
            items = []
            for sc in config["service_checks"]:
                if not sc.get("enabled", True):
                    continue
                ct = sc["check_type"]
                if ct == "http":
                    url = sc.get("target_url") or f"http://{sc.get('target_host')}:{sc.get('target_port') or 80}"
                    ok, ms, code, err = http_probe(url, method=(sc.get("http_method") or "GET"), timeout=(sc.get("timeout") or 5))
                    items.append({
                        "service_check_id": sc["id"],
                        "timestamp": now_iso(),
                        "check_type": "http",
                        "is_up": ok,
                        "response_ms": ms,
                        "status_code": code,
                        "error": err,
                    })
                elif ct == "tcp":
                    host = sc.get("target_host") or "127.0.0.1"
                    port = sc.get("target_port") or 80
                    ok, ms = tcp_probe(host, port, timeout=(sc.get("timeout") or 5))
                    items.append({
                        "service_check_id": sc["id"],
                        "timestamp": now_iso(),
                        "check_type": "tcp",
                        "is_up": ok,
                        "response_ms": ms,
                        "status_code": None,
                        "error": "" if ok else "connect refused/timeout",
                    })
                else:
                    # dns/tls/icmp — stub: report up with random rtt
                    items.append({
                        "service_check_id": sc["id"],
                        "timestamp": now_iso(),
                        "check_type": ct,
                        "is_up": True,
                        "response_ms": random.uniform(5, 50),
                        "status_code": None,
                        "error": "",
                    })
            if items:
                status, body, _ = client.post("/api/v1/sensor/results/service", {"items": items})
                log(f"service batch: {len(items)} items → {status}")
            last_service = now

        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted, exiting")
