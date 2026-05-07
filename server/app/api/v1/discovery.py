import asyncio
import ipaddress
import struct
import socket
import os
import time
from typing import Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from app.core.security import require_operator_user
from app.models.user import User

router = APIRouter(prefix="/discovery", tags=["Discovery"])


class ScanRequest(BaseModel):
    subnets: list[str] = Field(..., min_length=1, max_length=10)
    timeout: float = Field(default=1.0, ge=0.2, le=5.0)
    count: int = Field(default=2, ge=1, le=5)


class DiscoveredHost(BaseModel):
    ip: str
    rtt_ms: Optional[float] = None
    hostname: Optional[str] = None
    is_alive: bool = False


class ScanResult(BaseModel):
    subnet: str
    total_hosts: int
    alive_hosts: int
    hosts: list[DiscoveredHost]
    scan_time_sec: float


class ScanResponse(BaseModel):
    results: list[ScanResult]
    total_scanned: int
    total_alive: int


def _checksum(data: bytes) -> int:
    s = 0
    n = len(data) % 2
    for i in range(0, len(data) - n, 2):
        s += (data[i] << 8) + data[i + 1]
    if n:
        s += data[-1] << 8
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


async def ping_host(ip: str, timeout: float = 1.0, count: int = 2) -> DiscoveredHost:
    """Ping a single host using subprocess ping (works without raw socket issues)."""
    result = DiscoveredHost(ip=ip)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", str(count), "-W", str(int(timeout)), "-q", ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout * count + 2)
        if proc.returncode == 0:
            result.is_alive = True
            output = stdout.decode()
            for line in output.split("\n"):
                if "rtt" in line or "round-trip" in line:
                    parts = line.split("=")[-1].strip().split("/")
                    if len(parts) >= 2:
                        result.rtt_ms = round(float(parts[1]), 2)
                    break
    except (asyncio.TimeoutError, Exception):
        pass

    # Try reverse DNS
    if result.is_alive:
        try:
            hostname = await asyncio.get_event_loop().run_in_executor(
                None, lambda: socket.gethostbyaddr(ip)[0]
            )
            result.hostname = hostname
        except (socket.herror, socket.gaierror, OSError):
            pass

    return result


async def scan_subnet(subnet_str: str, timeout: float, count: int) -> ScanResult:
    """Scan all hosts in a subnet concurrently."""
    start = time.monotonic()
    try:
        network = ipaddress.ip_network(subnet_str, strict=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid subnet: {subnet_str} - {e}")

    # Limit to /20 (4094 hosts) max
    if network.prefixlen < 20:
        raise HTTPException(status_code=400, detail=f"Subnet {subnet_str} is too large. Maximum is /20 (4094 hosts)")

    hosts = [str(ip) for ip in network.hosts()]

    # Scan in batches of 50 concurrent pings
    batch_size = 50
    all_results: list[DiscoveredHost] = []
    for i in range(0, len(hosts), batch_size):
        batch = hosts[i:i + batch_size]
        tasks = [ping_host(ip, timeout, count) for ip in batch]
        batch_results = await asyncio.gather(*tasks)
        all_results.extend(batch_results)

    elapsed = round(time.monotonic() - start, 2)
    alive = [h for h in all_results if h.is_alive]

    # Sort: alive first (by IP), then dead
    alive_sorted = sorted(alive, key=lambda h: ipaddress.ip_address(h.ip))
    dead_sorted = sorted([h for h in all_results if not h.is_alive], key=lambda h: ipaddress.ip_address(h.ip))

    return ScanResult(
        subnet=subnet_str,
        total_hosts=len(hosts),
        alive_hosts=len(alive),
        hosts=alive_sorted + dead_sorted,
        scan_time_sec=elapsed,
    )


@router.post("/scan", response_model=ScanResponse)
async def run_scan(
    req: ScanRequest,
    user: User = Depends(require_operator_user),
):
    """Scan one or more subnets for live hosts via ICMP ping."""
    results = []
    total_scanned = 0
    total_alive = 0

    for subnet in req.subnets:
        result = await scan_subnet(subnet.strip(), req.timeout, req.count)
        results.append(result)
        total_scanned += result.total_hosts
        total_alive += result.alive_hosts

    return ScanResponse(
        results=results,
        total_scanned=total_scanned,
        total_alive=total_alive,
    )
