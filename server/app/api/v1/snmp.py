"""SNMP discovery sweep + MIB upload endpoints.

Routes:
    POST   /api/v1/snmp/discover           create a sweep job (async)
    GET    /api/v1/snmp/discover           list jobs (newest first)
    GET    /api/v1/snmp/discover/{id}      job detail
    GET    /api/v1/snmp/discover/{id}/results  staged results
    POST   /api/v1/snmp/discover/{id}/import   move selected results into devices
    DELETE /api/v1/snmp/discover/{id}      cancel/delete a job

    POST   /api/v1/snmp/mibs               upload a MIB file
    GET    /api/v1/snmp/mibs               list uploaded MIBs
    DELETE /api/v1/snmp/mibs/{mib_id}      remove an uploaded MIB

The sweep shells out to `snmpget` / `ping` to avoid pulling a new
Python SNMP dependency. Both tools are already installed on the host
(net-snmp + iputils-ping).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.snmp import (
    DiscoveryImportRequest,
    DiscoveryImportResponse,
    DiscoveryJobCreate,
    DiscoveryJobResponse,
    DiscoveryResultResponse,
    MibUploadResponse,
    ProfileCreate,
    ProfileUpdate,
    ProfileResponse,
)

router = APIRouter(prefix="/snmp", tags=["SNMP"])

# --------------------------------------------------------------------
# Discovery sweep
# --------------------------------------------------------------------

MIB_DIR = Path(os.getenv("SNMP_MIBS_DIR", "/opt/zenplus/data/mibs"))
MAX_CONCURRENT_PROBES = 64
MAX_CIDR_HOSTS = 1024  # safety cap — /22 is the biggest we allow in one job


async def _ping_once(ip: str, timeout_s: float = 1.0) -> bool:
    """Shell out to /bin/ping. Returns True if the host answered."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(max(1, int(timeout_s))), "-n", ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await proc.wait()
        return rc == 0
    except Exception:
        return False


async def _snmpget_detail(
    ip: str, community: str, version: str, port: int, timeout_ms: int, oids: list[str],
    v3_username: str | None = None, v3_security_level: str | None = None,
    v3_auth_protocol: str | None = None, v3_auth_passphrase: str | None = None,
    v3_priv_protocol: str | None = None, v3_priv_passphrase: str | None = None,
    v3_context: str | None = None,
) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Shell out to snmpget and parse ``OID = TYPE: value`` output.

    Returns ``(values_dict, None)`` on success or ``(None, error_string)``
    on failure. The error string distinguishes "binary missing" from
    "auth failure" from "timeout" so the caller can surface a real
    diagnostic to the user.
    """
    if shutil.which("snmpget") is None:
        return None, "snmpget binary not installed in the API runtime"
    timeout_s = max(1, timeout_ms // 1000)
    args = [
        "snmpget",
        "-v", version,
        "-r", "1",
        "-t", str(timeout_s),
        "-Oqv",   # quick + value-only per OID
        "-OU",    # no MIB name substitution
    ]
    if version == "3":
        # SNMPv3 auth arguments
        _SEC_MAP = {"noAuthNoPriv": "noAuthNoPriv", "authNoPriv": "authNoPriv", "authPriv": "authPriv"}
        sec_level = _SEC_MAP.get(v3_security_level or "authPriv", "authPriv")
        args += ["-l", sec_level]
        if v3_username:
            args += ["-u", v3_username]
        if v3_context:
            args += ["-n", v3_context]
        if sec_level in ("authNoPriv", "authPriv"):
            if v3_auth_protocol:
                # net-snmp accepts MD5|SHA|SHA-224|SHA-256|SHA-384|SHA-512.
                # Device config stores them without the dash (e.g. SHA256).
                _AUTH_MAP = {
                    "SHA224": "SHA-224", "SHA256": "SHA-256",
                    "SHA384": "SHA-384", "SHA512": "SHA-512",
                }
                args += ["-a", _AUTH_MAP.get(v3_auth_protocol, v3_auth_protocol)]
            if v3_auth_passphrase:
                args += ["-A", v3_auth_passphrase]
        if sec_level == "authPriv":
            if v3_priv_protocol:
                # net-snmp uses AES for AES-128, AES-192, AES-256; DES for 1DES.
                _PRIV_MAP = {
                    "AES128": "AES", "AES192": "AES-192", "AES256": "AES-256",
                    "AES": "AES",
                }
                args += ["-x", _PRIV_MAP.get(v3_priv_protocol, v3_priv_protocol)]
            if v3_priv_passphrase:
                args += ["-X", v3_priv_passphrase]
    else:
        args += ["-c", community]
    args += [f"{ip}:{port}", *oids]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        if proc.returncode != 0:
            err = (err_b.decode("utf-8", errors="replace").strip()
                   or out_b.decode("utf-8", errors="replace").strip()
                   or f"snmpget exited with code {proc.returncode}")
            return None, err
        raw = out_b.decode("utf-8", errors="replace").strip()
        # Parse -Oqv output which may contain multi-line quoted strings.
        # Each OID value is either a single unquoted line or a quoted
        # block that starts with " and ends with " on a later line.
        values: list[str] = []
        lines = raw.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith('"') and not line.endswith('"'):
                # Multi-line quoted value — accumulate until closing quote
                parts = [line]
                i += 1
                while i < len(lines):
                    parts.append(lines[i])
                    if lines[i].endswith('"'):
                        i += 1
                        break
                    i += 1
                joined = "\n".join(parts)
                values.append(joined.strip('"'))
            else:
                val = line.strip()
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                values.append(val)
                i += 1
        if len(values) != len(oids):
            return None, f"snmpget returned {len(values)} values, expected {len(oids)}"
        result: dict[str, str] = {}
        for oid, val in zip(oids, values):
            result[oid] = val
        return result, None
    except Exception as e:
        return None, f"snmpget invocation failed: {e}"


async def _snmpget(
    ip: str, community: str, version: str, port: int, timeout_ms: int, oids: list[str],
    v3_username: str | None = None, v3_security_level: str | None = None,
    v3_auth_protocol: str | None = None, v3_auth_passphrase: str | None = None,
    v3_priv_protocol: str | None = None, v3_priv_passphrase: str | None = None,
    v3_context: str | None = None,
) -> Optional[dict[str, str]]:
    """Backwards-compatible wrapper around :func:`_snmpget_detail`."""
    result, _err = await _snmpget_detail(
        ip=ip, community=community, version=version, port=port, timeout_ms=timeout_ms,
        oids=oids, v3_username=v3_username, v3_security_level=v3_security_level,
        v3_auth_protocol=v3_auth_protocol, v3_auth_passphrase=v3_auth_passphrase,
        v3_priv_protocol=v3_priv_protocol, v3_priv_passphrase=v3_priv_passphrase,
        v3_context=v3_context,
    )
    return result


SYS_OBJECT_OID = "1.3.6.1.2.1.1.2.0"
SYS_DESCR_OID = "1.3.6.1.2.1.1.1.0"
SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"


async def _classify_from_db(
    db: AsyncSession, sys_object_id: str, sys_descr: str
) -> tuple[Optional[uuid.UUID], str, str, str]:
    """Match a discovered device against seeded device_profiles.

    Mirrors poller/internal/checker/snmp/profile.go Match() semantics:
    longest sys_object_id_prefixes wins, sys_descr_regex is a fallback.
    The match_rules extractors are applied in a best-effort way; if a
    regex is missing or doesn't match, the field is left blank.
    """
    rows = (await db.execute(
        text(
            "SELECT id, vendor, match_rules FROM device_profiles "
            "ORDER BY length(name) DESC"
        )
    )).mappings().all()

    clean_oid = (sys_object_id or "").lstrip(".")

    best_id: Optional[uuid.UUID] = None
    best_len = -1
    best_rules: dict = {}
    best_vendor = ""

    # 1. sysObjectID prefix — longest wins.
    for row in rows:
        rules = row["match_rules"] or {}
        for pref in rules.get("sys_object_id_prefixes", []) or []:
            pref = pref.lstrip(".")
            if clean_oid and clean_oid.startswith(pref) and len(pref) > best_len:
                best_id = row["id"]
                best_len = len(pref)
                best_rules = rules
                best_vendor = row["vendor"] or rules.get("default_vendor", "")
                break

    # 2. sys_descr fallback.
    if best_id is None:
        for row in rows:
            rules = row["match_rules"] or {}
            pat = rules.get("sys_descr_regex")
            if pat:
                try:
                    if re.search(pat, sys_descr or ""):
                        best_id = row["id"]
                        best_rules = rules
                        best_vendor = row["vendor"] or rules.get("default_vendor", "")
                        break
                except re.error:
                    continue

    if best_id is None:
        return None, "", "", ""

    # Extract vendor/model/os_version
    vendor = best_vendor or best_rules.get("default_vendor", "")
    model = best_rules.get("default_model", "")
    os_version = ""

    def _extract(pattern: str) -> str:
        if not pattern:
            return ""
        try:
            m = re.search(pattern, sys_descr or "")
            if not m:
                return ""
            if m.groupdict():
                for v in m.groupdict().values():
                    if v:
                        return v.strip()
            if m.groups():
                for v in m.groups():
                    if v:
                        return v.strip()
            return m.group(0).strip()
        except re.error:
            return ""

    if best_rules.get("extract_vendor"):
        v = _extract(best_rules["extract_vendor"])
        if v:
            vendor = v
    if best_rules.get("extract_model"):
        m = _extract(best_rules["extract_model"])
        if m:
            model = m
    os_version = _extract(best_rules.get("extract_os_version", ""))

    return best_id, vendor, model, os_version


async def _probe_host(
    ip: str,
    community: str,
    version: str,
    port: int,
    timeout_ms: int,
    v3_username: str | None = None,
    v3_security_level: str | None = None,
    v3_auth_protocol: str | None = None,
    v3_auth_passphrase: str | None = None,
    v3_priv_protocol: str | None = None,
    v3_priv_passphrase: str | None = None,
    v3_context: str | None = None,
) -> dict:
    """Ping + SNMP probe a single host; returns a plain dict ready for insert."""
    out = {
        "ip_address": ip,
        "is_reachable": False,
        "snmp_responded": False,
        "sys_object_id": None,
        "sys_descr": None,
        "sys_name": None,
        "hostname_guess": None,
        "error_message": None,
    }

    out["is_reachable"] = await _ping_once(ip, timeout_s=max(1, timeout_ms / 1000))
    if not out["is_reachable"]:
        return out

    snmp = await _snmpget(
        ip, community, version, port, timeout_ms,
        [SYS_OBJECT_OID, SYS_DESCR_OID, SYS_NAME_OID],
        v3_username=v3_username, v3_security_level=v3_security_level,
        v3_auth_protocol=v3_auth_protocol, v3_auth_passphrase=v3_auth_passphrase,
        v3_priv_protocol=v3_priv_protocol, v3_priv_passphrase=v3_priv_passphrase,
        v3_context=v3_context,
    )
    if snmp is None:
        out["error_message"] = "snmpget failed or timed out"
        return out

    out["snmp_responded"] = True
    out["sys_object_id"] = snmp.get(SYS_OBJECT_OID) or None
    out["sys_descr"] = snmp.get(SYS_DESCR_OID) or None
    out["sys_name"] = snmp.get(SYS_NAME_OID) or None
    out["hostname_guess"] = out["sys_name"] or ip
    return out


async def _run_discovery_job(job_id: uuid.UUID) -> None:
    """Background worker. Opens its own DB session so it survives after
    the HTTP request that kicked it off returns."""
    async with AsyncSessionLocal() as db:
        job = (await db.execute(
            text("SELECT * FROM discovery_jobs WHERE id = :id"), {"id": job_id}
        )).mappings().first()
        if job is None:
            return

        try:
            net = ipaddress.ip_network(job["cidr"], strict=False)
        except ValueError as e:
            await db.execute(
                text("UPDATE discovery_jobs SET status='failed', error_message=:e, completed_at=NOW() WHERE id=:id"),
                {"e": f"invalid CIDR: {e}", "id": job_id},
            )
            await db.commit()
            return

        hosts = [str(h) for h in net.hosts()] if net.num_addresses > 1 else [str(net.network_address)]
        if len(hosts) > MAX_CIDR_HOSTS:
            await db.execute(
                text("UPDATE discovery_jobs SET status='failed', error_message=:e, completed_at=NOW() WHERE id=:id"),
                {"e": f"CIDR too large ({len(hosts)} > {MAX_CIDR_HOSTS})", "id": job_id},
            )
            await db.commit()
            return

        await db.execute(
            text("UPDATE discovery_jobs SET status='running', total_hosts=:t, started_at=NOW() WHERE id=:id"),
            {"t": len(hosts), "id": job_id},
        )
        await db.commit()

        sem = asyncio.Semaphore(MAX_CONCURRENT_PROBES)
        responding = 0
        scanned = 0

        async def worker(ip: str) -> dict:
            async with sem:
                return await _probe_host(
                    ip, job["community"] or "public", job["snmp_version"],
                    job["snmp_port"], job["timeout_ms"],
                    v3_username=job.get("v3_username"),
                    v3_security_level=job.get("v3_security_level"),
                    v3_auth_protocol=job.get("v3_auth_protocol"),
                    v3_auth_passphrase=job.get("v3_auth_passphrase"),
                    v3_priv_protocol=job.get("v3_priv_protocol"),
                    v3_priv_passphrase=job.get("v3_priv_passphrase"),
                    v3_context=job.get("v3_context"),
                )

        tasks = [asyncio.create_task(worker(ip)) for ip in hosts]

        for coro in asyncio.as_completed(tasks):
            probe = await coro
            scanned += 1

            matched_id = matched_vendor = matched_model = matched_os = None
            if probe["snmp_responded"] and probe["sys_object_id"]:
                matched_id, matched_vendor, matched_model, matched_os = await _classify_from_db(
                    db, probe["sys_object_id"] or "", probe["sys_descr"] or ""
                )
                responding += 1

            # Is this IP already a monitored device?
            existing = (await db.execute(
                text("SELECT id FROM devices WHERE ip_address = CAST(:ip AS inet)"),
                {"ip": probe["ip_address"]},
            )).first()

            await db.execute(
                text("""
                    INSERT INTO discovery_results (
                        job_id, ip_address, is_reachable, snmp_responded,
                        sys_object_id, sys_descr, sys_name, hostname_guess,
                        matched_profile_id, matched_vendor, matched_model, matched_os_version,
                        already_known, error_message, scanned_at
                    ) VALUES (
                        :job_id, CAST(:ip AS inet), :reach, :snmp,
                        :oid, :descr, :name, :hguess,
                        :mid, :mv, :mm, :mo,
                        :known, :err, NOW()
                    )
                    ON CONFLICT (job_id, ip_address) DO NOTHING
                """),
                {
                    "job_id": job_id,
                    "ip": probe["ip_address"],
                    "reach": probe["is_reachable"],
                    "snmp": probe["snmp_responded"],
                    "oid": probe["sys_object_id"],
                    "descr": probe["sys_descr"],
                    "name": probe["sys_name"],
                    "hguess": probe["hostname_guess"],
                    "mid": matched_id,
                    "mv": matched_vendor,
                    "mm": matched_model,
                    "mo": matched_os,
                    "known": existing is not None,
                    "err": probe["error_message"],
                },
            )

            await db.execute(
                text("UPDATE discovery_jobs SET scanned_hosts=:s, responding_hosts=:r WHERE id=:id"),
                {"s": scanned, "r": responding, "id": job_id},
            )
            await db.commit()

        await db.execute(
            text("UPDATE discovery_jobs SET status='completed', completed_at=NOW() WHERE id=:id"),
            {"id": job_id},
        )
        await db.commit()


@router.post("/discover", response_model=DiscoveryJobResponse, status_code=201)
async def create_discovery_job(
    data: DiscoveryJobCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        net = ipaddress.ip_network(data.cidr, strict=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid CIDR: {e}")
    if net.num_addresses > MAX_CIDR_HOSTS + 2:
        raise HTTPException(
            status_code=400,
            detail=f"CIDR too large: {net.num_addresses} hosts (max {MAX_CIDR_HOSTS})",
        )

    # Resolve credential: if credential_id is given, look it up and use its settings
    community = data.community
    snmp_version = data.snmp_version
    snmp_port = data.snmp_port
    timeout_ms = data.timeout_ms
    v3_username = v3_context = v3_security_level = None
    v3_auth_protocol = v3_auth_passphrase = None
    v3_priv_protocol = v3_priv_passphrase = None

    if data.credential_id:
        cred = (await db.execute(
            text("SELECT * FROM snmp_credentials WHERE id = :id"),
            {"id": data.credential_id},
        )).mappings().first()
        if not cred:
            raise HTTPException(status_code=404, detail="Credential not found")
        community = cred["community"] or "public"
        snmp_version = cred["snmp_version"]
        snmp_port = cred.get("port", 161) or 161
        timeout_ms = cred.get("timeout_ms", 2000) or 2000
        if snmp_version == "3":
            v3_username = cred.get("v3_username")
            v3_context = cred.get("v3_context")
            v3_security_level = cred.get("v3_security_level")
            v3_auth_protocol = cred.get("v3_auth_protocol")
            v3_auth_passphrase = cred.get("v3_auth_passphrase")
            v3_priv_protocol = cred.get("v3_priv_protocol")
            v3_priv_passphrase = cred.get("v3_priv_passphrase")

    row = (await db.execute(
        text("""
            INSERT INTO discovery_jobs (
                cidr, community, snmp_version, snmp_port, timeout_ms, created_by,
                v3_username, v3_context, v3_security_level,
                v3_auth_protocol, v3_auth_passphrase,
                v3_priv_protocol, v3_priv_passphrase
            )
            VALUES (
                :cidr, :community, :version, :port, :timeout, :uid,
                :v3user, :v3ctx, :v3sec,
                :v3auth, :v3authpw,
                :v3priv, :v3privpw
            )
            RETURNING id, cidr, community, snmp_version, snmp_port, timeout_ms, status,
                      total_hosts, scanned_hosts, responding_hosts, error_message,
                      started_at, completed_at, created_at
        """),
        {
            "cidr": data.cidr, "community": community,
            "version": snmp_version, "port": snmp_port,
            "timeout": timeout_ms, "uid": user.id,
            "v3user": v3_username, "v3ctx": v3_context, "v3sec": v3_security_level,
            "v3auth": v3_auth_protocol, "v3authpw": v3_auth_passphrase,
            "v3priv": v3_priv_protocol, "v3privpw": v3_priv_passphrase,
        },
    )).mappings().first()
    await db.commit()

    # Kick off the background sweep. We intentionally don't await it.
    asyncio.create_task(_run_discovery_job(row["id"]))

    return DiscoveryJobResponse(**dict(row))


@router.get("/discover", response_model=list[DiscoveryJobResponse])
async def list_discovery_jobs(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        text("""
            SELECT id, cidr, community, snmp_version, snmp_port, timeout_ms, status,
                   total_hosts, scanned_hosts, responding_hosts, error_message,
                   started_at, completed_at, created_at
            FROM discovery_jobs
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"limit": limit},
    )).mappings().all()
    return [DiscoveryJobResponse(**dict(r)) for r in rows]


@router.get("/discover/{job_id}", response_model=DiscoveryJobResponse)
async def get_discovery_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (await db.execute(
        text("""
            SELECT id, cidr, community, snmp_version, snmp_port, timeout_ms, status,
                   total_hosts, scanned_hosts, responding_hosts, error_message,
                   started_at, completed_at, created_at
            FROM discovery_jobs WHERE id = :id
        """),
        {"id": job_id},
    )).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="discovery job not found")
    return DiscoveryJobResponse(**dict(row))


@router.get("/discover/{job_id}/results", response_model=list[DiscoveryResultResponse])
async def list_discovery_results(
    job_id: uuid.UUID,
    responding_only: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sql = """
        SELECT id, job_id, host(ip_address)::text AS ip_address, is_reachable, snmp_responded,
               sys_object_id, sys_descr, sys_name, hostname_guess,
               matched_profile_id, matched_vendor, matched_model, matched_os_version,
               already_known, imported, imported_device_id, error_message, scanned_at
        FROM discovery_results WHERE job_id = :id
    """
    if responding_only:
        sql += " AND snmp_responded = TRUE"
    sql += " ORDER BY ip_address"
    rows = (await db.execute(text(sql), {"id": job_id})).mappings().all()
    return [DiscoveryResultResponse(**dict(r)) for r in rows]


@router.post("/discover/{job_id}/import", response_model=DiscoveryImportResponse)
async def import_discovery_results(
    job_id: uuid.UUID,
    data: DiscoveryImportRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        text("""
            SELECT id, host(ip_address)::text AS ip_address, hostname_guess,
                   matched_profile_id, matched_vendor, matched_model, matched_os_version,
                   sys_object_id, already_known, imported
            FROM discovery_results
            WHERE job_id = :job AND id = ANY(:ids)
        """),
        {"job": job_id, "ids": data.result_ids},
    )).mappings().all()

    created: list[uuid.UUID] = []
    skipped = 0
    errors: list[str] = []

    for r in rows:
        if r["already_known"] or r["imported"]:
            skipped += 1
            continue
        try:
            dev = (await db.execute(
                text("""
                    INSERT INTO devices (
                        hostname, ip_address, device_type, group_id,
                        ping_enabled, snmp_enabled, snmp_version, snmp_community,
                        profile_id, sys_object_id, vendor, model, os_version, created_by
                    ) VALUES (
                        :hostname, CAST(:ip AS inet), 'other', :gid,
                        TRUE, TRUE, '2c', 'public',
                        :pid, :oid, :vendor, :model, :os, :uid
                    )
                    RETURNING id
                """),
                {
                    "hostname": r["hostname_guess"] or r["ip_address"],
                    "ip": r["ip_address"],
                    "gid": data.default_group_id,
                    "pid": r["matched_profile_id"],
                    "oid": r["sys_object_id"],
                    "vendor": r["matched_vendor"],
                    "model": r["matched_model"],
                    "os": r["matched_os_version"],
                    "uid": user.id,
                },
            )).mappings().first()
            created.append(dev["id"])
            await db.execute(
                text("UPDATE discovery_results SET imported=TRUE, imported_device_id=:d WHERE id=:i"),
                {"d": dev["id"], "i": r["id"]},
            )
        except Exception as e:
            errors.append(f"{r['ip_address']}: {e}")

    await db.commit()
    return DiscoveryImportResponse(
        created=len(created), skipped=skipped, errors=errors, device_ids=created
    )


@router.delete("/discover/{job_id}", status_code=204)
async def delete_discovery_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    res = await db.execute(text("DELETE FROM discovery_jobs WHERE id = :id"), {"id": job_id})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="discovery job not found")


# --------------------------------------------------------------------
# MIB upload
# --------------------------------------------------------------------

_MIB_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@router.post("/mibs", response_model=MibUploadResponse, status_code=201)
async def upload_mib(
    file: UploadFile = File(...),
    name: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Filename must be safe — no path traversal.
    raw_name = file.filename or ""
    safe_name = os.path.basename(raw_name)
    if not safe_name or not _MIB_NAME_RE.match(safe_name):
        raise HTTPException(status_code=400, detail="invalid filename")

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="empty file")
    if len(content) > 4 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="MIB file too large (max 4 MB)")

    MIB_DIR.mkdir(parents=True, exist_ok=True)
    target = MIB_DIR / safe_name
    target.write_bytes(content)

    sha = hashlib.sha256(content).hexdigest()
    mib_name = name or safe_name.rsplit(".", 1)[0]

    row = (await db.execute(
        text("""
            INSERT INTO snmp_mibs (name, filename, size_bytes, sha256, uploaded_by, description)
            VALUES (:name, :filename, :size, :sha, :uid, :desc)
            ON CONFLICT (name) DO UPDATE SET
                filename = EXCLUDED.filename,
                size_bytes = EXCLUDED.size_bytes,
                sha256 = EXCLUDED.sha256,
                uploaded_at = NOW(),
                description = EXCLUDED.description
            RETURNING id, name, filename, size_bytes, sha256, uploaded_at
        """),
        {
            "name": mib_name, "filename": safe_name,
            "size": len(content), "sha": sha,
            "uid": user.id, "desc": description,
        },
    )).mappings().first()
    await db.commit()
    return MibUploadResponse(**dict(row))


@router.get("/mibs", response_model=list[MibUploadResponse])
async def list_mibs(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        text("SELECT id, name, filename, size_bytes, sha256, uploaded_at FROM snmp_mibs ORDER BY name")
    )).mappings().all()
    return [MibUploadResponse(**dict(r)) for r in rows]


@router.delete("/mibs/{mib_id}", status_code=204)
async def delete_mib(
    mib_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (await db.execute(
        text("SELECT filename FROM snmp_mibs WHERE id = :id"), {"id": mib_id}
    )).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="MIB not found")
    target = MIB_DIR / row["filename"]
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass
    await db.execute(text("DELETE FROM snmp_mibs WHERE id = :id"), {"id": mib_id})
    await db.commit()


# --------------------------------------------------------------------
# Read endpoints backing the dashboard
# --------------------------------------------------------------------


@router.get("/profiles", response_model=list[ProfileResponse])
async def list_profiles(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all SNMP device profiles with device count."""
    rows = (await db.execute(
        text("""
            SELECT p.id, p.name, p.vendor, p.version, p.builtin, p.description,
                   p.match_rules, p.oid_groups, p.created_at, p.updated_at,
                   COALESCE(dc.cnt, 0) AS device_count
            FROM device_profiles p
            LEFT JOIN (
                SELECT profile_id, COUNT(*) AS cnt FROM devices
                WHERE profile_id IS NOT NULL GROUP BY profile_id
            ) dc ON dc.profile_id = p.id
            ORDER BY p.name
        """)
    )).mappings().all()
    return [ProfileResponse(**dict(r)) for r in rows]


@router.get("/profiles/{profile_id}", response_model=ProfileResponse)
async def get_profile(
    profile_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (await db.execute(
        text("""
            SELECT p.id, p.name, p.vendor, p.version, p.builtin, p.description,
                   p.match_rules, p.oid_groups, p.created_at, p.updated_at,
                   COALESCE(dc.cnt, 0) AS device_count
            FROM device_profiles p
            LEFT JOIN (
                SELECT profile_id, COUNT(*) AS cnt FROM devices
                WHERE profile_id IS NOT NULL GROUP BY profile_id
            ) dc ON dc.profile_id = p.id
            WHERE p.id = :id
        """),
        {"id": profile_id},
    )).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return ProfileResponse(**dict(row))


@router.post("/profiles", response_model=ProfileResponse, status_code=201)
async def create_profile(
    data: ProfileCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import json
    row = (await db.execute(
        text("""
            INSERT INTO device_profiles (name, vendor, description, match_rules, oid_groups, builtin)
            VALUES (:name, :vendor, :desc, CAST(:match_rules AS jsonb), CAST(:oid_groups AS jsonb), FALSE)
            RETURNING id, name, vendor, version, builtin, description,
                      match_rules, oid_groups, created_at, updated_at
        """),
        {
            "name": data.name,
            "vendor": data.vendor,
            "desc": data.description,
            "match_rules": json.dumps(data.match_rules.model_dump()),
            "oid_groups": json.dumps([g.model_dump() for g in data.oid_groups]),
        },
    )).mappings().first()
    await db.commit()
    return ProfileResponse(**dict(row), device_count=0)


@router.put("/profiles/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    profile_id: uuid.UUID,
    data: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import json
    existing = (await db.execute(
        text("SELECT id, builtin FROM device_profiles WHERE id = :id"),
        {"id": profile_id},
    )).mappings().first()
    if existing is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    sets = ["updated_at = NOW()"]
    params: dict = {"id": profile_id}

    if data.name is not None:
        sets.append("name = :name")
        params["name"] = data.name
    if data.vendor is not None:
        sets.append("vendor = :vendor")
        params["vendor"] = data.vendor
    if data.description is not None:
        sets.append("description = :desc")
        params["desc"] = data.description
    if data.match_rules is not None:
        sets.append("match_rules = CAST(:match_rules AS jsonb)")
        params["match_rules"] = json.dumps(data.match_rules.model_dump())
    if data.oid_groups is not None:
        sets.append("oid_groups = CAST(:oid_groups AS jsonb)")
        params["oid_groups"] = json.dumps([g.model_dump() for g in data.oid_groups])

    await db.execute(
        text(f"UPDATE device_profiles SET {', '.join(sets)} WHERE id = :id"),
        params,
    )
    await db.commit()

    return await get_profile(profile_id, db, user)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    existing = (await db.execute(
        text("SELECT id, builtin FROM device_profiles WHERE id = :id"),
        {"id": profile_id},
    )).mappings().first()
    if existing is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    if existing["builtin"]:
        raise HTTPException(status_code=400, detail="Cannot delete built-in profiles")
    # Unlink devices referencing this profile
    await db.execute(
        text("UPDATE devices SET profile_id = NULL WHERE profile_id = :id"),
        {"id": profile_id},
    )
    await db.execute(
        text("DELETE FROM device_profiles WHERE id = :id"),
        {"id": profile_id},
    )
    await db.commit()


@router.get("/traps")
async def list_traps(
    limit: int = 200,
    hours: int = 24,
    device_id: Optional[uuid.UUID] = None,
    user: User = Depends(get_current_user),
):
    """Recent traps across all devices (or one device when device_id is set).

    Pulls from ClickHouse zenplus.snmp_traps. Returns a plain list the
    UI can render as-is.
    """
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    where = ["timestamp >= now() - INTERVAL %(hours)s HOUR"]
    params: dict = {"hours": hours, "limit": limit}
    if device_id is not None:
        where.append("device_id = %(device_id)s")
        params["device_id"] = str(device_id)

    sql = f"""
        SELECT toString(device_id) AS device_id,
               toString(source_ip) AS source_ip,
               trap_oid, trap_name, severity, message,
               bindings, toUnixTimestamp64Milli(timestamp) AS ts_ms
        FROM zenplus.snmp_traps
        WHERE {' AND '.join(where)}
        ORDER BY timestamp DESC
        LIMIT %(limit)s
    """
    try:
        res = client.query(sql, parameters=params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {e}")

    rows = []
    for r in res.result_rows:
        rows.append({
            "device_id": r[0] if r[0] != "00000000-0000-0000-0000-000000000000" else None,
            "source_ip": r[1],
            "trap_oid": r[2],
            "trap_name": r[3],
            "severity": r[4],
            "message": r[5],
            "bindings": r[6],
            "timestamp": datetime.fromtimestamp(r[7] / 1000, tz=timezone.utc).isoformat(),
        })
    return rows
