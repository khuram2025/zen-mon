"""Discovery v2 scan executor — real network probes.

For each target IP, runs the protocols selected on the profile:
- ICMP (real ping)
- TCP port scan (handshake)
- HTTP / HTTPS fingerprint (Server header, title, TLS cert)
- SSH banner grab
- SNMP via net-snmp (using saved credentials)
- WinRM via pywinrm (using Windows credentials)
- ARP table lookup for MAC

Each probe returns a structured result. The identifier module aggregates
them into a single device identity (vendor, model, OS, etc.).

The scan walks through the canonical phases (preparing → validating →
scanning → identifying → matching → applying_rules → reporting → done)
so the UI's progress page accurately reflects what's happening.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt, decrypt_secret
from app.core.database import AsyncSessionLocal
from app.models.discovery_v2 import (
    DiscoveryProfile,
    DiscoveryResultV2,
    DiscoveryRun,
)
from app.services import discovery_probes as probes
from app.services.discovery_identify import identify


# Background-task handles (run_id → asyncio.Task) for cancellation.
_RUNNING_TASKS: dict[uuid.UUID, asyncio.Task] = {}


# ────────────────────────────────────────────────────────────────────
# Target expansion (CIDR / range / single)
# ────────────────────────────────────────────────────────────────────
def expand_targets(targets: Iterable[str], exclusions: Iterable[str] | None = None,
                   cap: int = 4096) -> list[str]:
    excl: set[str] = set()
    for ex in exclusions or []:
        for ip in _expand_one(ex):
            excl.add(ip)

    out: list[str] = []
    seen: set[str] = set()
    for t in targets:
        for ip in _expand_one(t):
            if ip in excl or ip in seen:
                continue
            seen.add(ip)
            out.append(ip)
            if len(out) >= cap:
                return out
    return out


def _expand_one(s: str) -> list[str]:
    s = (s or "").strip()
    if not s:
        return []
    if "/" in s:
        try:
            net = ipaddress.ip_network(s, strict=False)
            if net.num_addresses > 4096:
                hosts = list(net.hosts())[:4096]
            else:
                hosts = list(net.hosts()) if net.num_addresses > 2 else [net.network_address]
            return [str(h) for h in hosts]
        except ValueError:
            return []
    if "-" in s:
        try:
            left, right = s.split("-", 1)
            left = left.strip()
            right = right.strip()
            if "." not in right:
                base = left.rsplit(".", 1)[0]
                right = f"{base}.{right}"
            a = ipaddress.ip_address(left)
            b = ipaddress.ip_address(right)
            if int(b) < int(a):
                a, b = b, a
            count = int(b) - int(a) + 1
            if count > 4096:
                count = 4096
            return [str(ipaddress.ip_address(int(a) + i)) for i in range(count)]
        except (ValueError, IndexError):
            return []
    try:
        ipaddress.ip_address(s)
        return [s]
    except ValueError:
        return []


# ────────────────────────────────────────────────────────────────────
# Credential loading
# ────────────────────────────────────────────────────────────────────
async def _load_snmp_credentials(db: AsyncSession, ids: list[str]) -> list[dict]:
    """Load saved SNMP credentials and decrypt v3 passphrases."""
    if not ids:
        return []
    rows = (await db.execute(
        text("""SELECT id, name, snmp_version, community, port, timeout_ms,
                       v3_username, v3_context, v3_security_level,
                       v3_auth_protocol, v3_auth_passphrase,
                       v3_priv_protocol, v3_priv_passphrase
                FROM snmp_credentials WHERE id = ANY(:ids)"""),
        {"ids": ids},
    )).mappings().all()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["v3_auth_passphrase"] = decrypt_secret(d.get("v3_auth_passphrase"))
        d["v3_priv_passphrase"] = decrypt_secret(d.get("v3_priv_passphrase"))
        d["id"] = str(d["id"])
        out.append(d)
    return out


async def _load_windows_credentials(db: AsyncSession, ids: list[str]) -> list[dict]:
    """Load saved Windows credentials and decrypt passwords."""
    if not ids:
        return []
    rows = (await db.execute(
        text("""SELECT id, name, username, domain, password_enc,
                       auth_method, transport, port, ssl_verify
                FROM windows_credentials WHERE id = ANY(:ids)"""),
        {"ids": ids},
    )).mappings().all()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["password"] = decrypt(d.pop("password_enc")) if d.get("password_enc") else ""
        except Exception:
            d["password"] = ""
        d["id"] = str(d["id"])
        out.append(d)
    return out


async def _load_ssh_credentials(db: AsyncSession, ids: list[str]) -> list[dict]:
    """Load NCM connection profiles for SSH/Telnet discovery probes."""
    if not ids:
        return []
    rows = (await db.execute(
        text("""SELECT id, name, protocol, port, username,
                       password_enc, enable_password_enc
                FROM ncm_credentials WHERE id = ANY(:ids)"""),
        {"ids": ids},
    )).mappings().all()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["password"] = decrypt(d.pop("password_enc")) if d.get("password_enc") else ""
        except Exception:
            d["password"] = ""
        try:
            d["enable_password"] = (
                decrypt(d.pop("enable_password_enc")) if d.get("enable_password_enc") else ""
            )
        except Exception:
            d["enable_password"] = ""
        d["id"] = str(d["id"])
        out.append(d)
    return out


# ────────────────────────────────────────────────────────────────────
# Existing-device match (deduplication)
# ────────────────────────────────────────────────────────────────────
async def _match_existing_device(db: AsyncSession, ip: str, mac: str | None,
                                  hostname: str | None) -> tuple[uuid.UUID | None, str | None]:
    row = (await db.execute(
        text("SELECT id, hostname, ip_address::text AS ip FROM devices WHERE ip_address = :ip"),
        {"ip": ip},
    )).mappings().first()
    if row:
        if hostname and row["hostname"] and hostname.lower() != row["hostname"].lower():
            return uuid.UUID(str(row["id"])), "same_ip_diff_hostname"
        return uuid.UUID(str(row["id"])), None
    if hostname:
        row = (await db.execute(
            text("SELECT id, ip_address::text AS ip FROM devices WHERE hostname = :h LIMIT 1"),
            {"h": hostname},
        )).mappings().first()
        if row:
            return uuid.UUID(str(row["id"])), "same_hostname_diff_ip"
    return None, None


# ────────────────────────────────────────────────────────────────────
# Activity log helper
# ────────────────────────────────────────────────────────────────────
def _log_event(activity: list[dict], message: str, level: str = "info") -> list[dict]:
    activity.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "msg": message,
    })
    if len(activity) > 200:
        del activity[: len(activity) - 200]
    return activity


# ────────────────────────────────────────────────────────────────────
# Per-IP probe pipeline
# ────────────────────────────────────────────────────────────────────
_HTTP_PORTS = [80, 8080, 8000, 8888, 8123, 9000]
_HTTPS_PORTS = [443, 8443, 8983, 5601]


async def _probe_ip(
    ip: str,
    protocols_requested: list[str],
    custom_ports: list[int],
    snmp_creds: list[dict],
    windows_creds: list[dict],
    ssh_creds: list[dict],
    timeout_ms: int,
) -> dict[str, Any]:
    """Run every requested probe against a single IP. Returns the
    consolidated identity dict (or None if the host doesn't respond at all).
    """
    timeout_s = max(0.5, timeout_ms / 1000.0)
    p: dict[str, Any] = {}

    # 1. ICMP — always run; it cheaply confirms liveness even if it's
    #    not listed in protocols (firewalls often block ping but allow
    #    services).
    p["icmp"] = await probes.icmp_ping(ip, timeout_s=timeout_s)

    # 2. Decide which ports to TCP-probe based on selected protocols.
    candidate_ports: set[int] = set(custom_ports or [])
    candidate_ports.update([22])  # SSH banner is always worth a try if reachable
    # SNMP uses UDP/161 — probed separately, not via TCP scan.
    if "http" in protocols_requested:
        candidate_ports.update(_HTTP_PORTS)
    if "https" in protocols_requested:
        candidate_ports.update(_HTTPS_PORTS)
    if "ssh" in protocols_requested:
        candidate_ports.add(22)
    if "wmi" in protocols_requested or "winrm" in protocols_requested:
        candidate_ports.update([5985, 5986])
    if "tcp" in protocols_requested:
        # If user only selects TCP, default to a small common set
        candidate_ports.update([22, 80, 443, 3306, 3389, 5985, 8080])
    candidate_ports.discard(0)
    p["tcp"] = await probes.tcp_scan(ip, sorted(candidate_ports), timeout_s=timeout_s)

    # Short-circuit: if neither ICMP nor any TCP port responded, the host
    # is treated as unreachable and we don't bother with auth probes.
    if not p["icmp"]["responsive"] and not p["tcp"]["responsive"]:
        return {"_responsive": False, "_probes": p}

    open_ports = set(p["tcp"]["data"].get("open", []))

    # 3. Reverse DNS + ARP MAC in parallel
    p["rdns"], p["mac"] = await asyncio.gather(
        probes.reverse_dns(ip),
        probes.arp_lookup(ip),
    )

    # 4. SSH — authenticated probe when profiles selected, else banner grab
    if "ssh" in protocols_requested:
        if ssh_creds:
            ssh_result = None
            for cred in ssh_creds:
                port = int(cred.get("port") or 22)
                if port not in open_ports and not p["icmp"]["responsive"]:
                    continue
                r = await probes.ssh_auth_probe(
                    ip, cred, timeout_s=max(timeout_s, 8.0),
                )
                if r.get("responsive"):
                    p["ssh"] = r
                    break
                ssh_result = r
            if "ssh" not in p:
                if ssh_result:
                    p["ssh"] = ssh_result
                elif 22 in open_ports:
                    p["ssh"] = await probes.ssh_banner(ip, port=22, timeout_s=timeout_s)
                else:
                    p["ssh"] = {
                        "responsive": False, "protocol": "ssh", "data": {},
                        "error": "no SSH credential succeeded", "state": "invalid",
                    }
        elif 22 in open_ports:
            p["ssh"] = await probes.ssh_banner(ip, port=22, timeout_s=timeout_s)
        else:
            p["ssh"] = {
                "responsive": False, "protocol": "ssh", "data": {},
                "error": "port 22 closed", "state": "no_response",
            }

    # 5. HTTP probes — only on ports that actually opened
    if "http" in protocols_requested:
        ports = [pp for pp in _HTTP_PORTS if pp in open_ports]
        if ports:
            p["http"] = await asyncio.gather(*[probes.http_probe(ip, pp, https=False, timeout_s=timeout_s) for pp in ports])

    # 6. HTTPS probes
    if "https" in protocols_requested:
        ports = [pp for pp in _HTTPS_PORTS if pp in open_ports]
        if ports:
            p["https"] = await asyncio.gather(*[probes.http_probe(ip, pp, https=True, timeout_s=timeout_s) for pp in ports])

    # 7. SNMP — UDP/161; run when requested if the host responded to ICMP/TCP.
    if "snmp" in protocols_requested and snmp_creds:
        snmp_results = []
        for cred in snmp_creds:
            r = await probes.snmp_probe(ip, cred, timeout_s=timeout_s)
            snmp_results.append(r)
            if r.get("responsive"):
                break
        p["snmp"] = snmp_results
    elif "snmp" in protocols_requested and not snmp_creds:
        p["snmp"] = [{"responsive": False, "protocol": "snmp", "data": {},
                       "error": "no SNMP credential configured", "state": "not_tested"}]

    # 8. WinRM
    if ("winrm" in protocols_requested or "wmi" in protocols_requested) \
            and (5985 in open_ports or 5986 in open_ports) and windows_creds:
        winrm_results = []
        for cred in windows_creds:
            r = await probes.winrm_probe(ip, cred, timeout_s=timeout_s)
            winrm_results.append(r)
            if r.get("responsive"):
                break
        p["winrm"] = winrm_results
    elif ("winrm" in protocols_requested or "wmi" in protocols_requested) \
            and (5985 in open_ports or 5986 in open_ports) and not windows_creds:
        p["winrm"] = [{"responsive": False, "protocol": "winrm", "data": {},
                        "error": "no Windows credential configured", "state": "not_tested"}]

    identity = identify(p, protocols_requested)
    identity["_responsive"] = True
    identity["_probes"] = p
    return identity


# ────────────────────────────────────────────────────────────────────
# Main run loop
# ────────────────────────────────────────────────────────────────────
async def execute_run(run_id: uuid.UUID) -> None:
    start = time.monotonic()
    async with AsyncSessionLocal() as db:
        run = await db.get(DiscoveryRun, run_id)
        if not run:
            return
        profile = await db.get(DiscoveryProfile, run.profile_id)
        if not profile:
            run.status = "failed"
            run.error_details = "Discovery profile not found"
            run.phase = "done"
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return

        # Snapshot config so future edits don't change history
        run.config_snapshot = {
            "name": profile.name,
            "scope_type": profile.scope_type,
            "targets": profile.targets,
            "exclusions": profile.exclusions,
            "protocols": profile.protocols,
            "custom_ports": profile.custom_ports,
            "snmp_credential_ids": [str(c) for c in (profile.snmp_credential_ids or [])],
            "windows_credential_ids": [str(c) for c in (profile.windows_credential_ids or [])],
            "ssh_credential_ids": [str(c) for c in (profile.ssh_credential_ids or [])],
            "max_concurrency": profile.max_concurrency,
            "scan_timeout_ms": profile.scan_timeout_ms,
        }
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        run.phase = "preparing"
        run.progress_pct = 1
        activity = list(run.activity_log or [])
        _log_event(activity, f"Run started by trigger={run.trigger_type}")
        run.activity_log = activity
        await db.commit()

    # Phase 1: prepare targets
    targets = expand_targets(profile.targets or [], profile.exclusions or [])
    if not targets:
        async with AsyncSessionLocal() as db:
            run = await db.get(DiscoveryRun, run_id)
            run.status = "failed"
            run.phase = "done"
            run.error_details = "No targets resolved from scope"
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()
        return
    if len(targets) > 4096:
        targets = targets[:4096]

    protocols_requested = [p.lower() for p in (profile.protocols or ["icmp"])]
    custom_ports = profile.custom_ports or []
    timeout_ms = int(profile.scan_timeout_ms or 2000)
    max_concurrency = max(1, min(int(profile.max_concurrency or 32), 128))

    # Phase 2: load credentials
    async with AsyncSessionLocal() as db:
        snmp_creds = await _load_snmp_credentials(
            db, [str(c) for c in (profile.snmp_credential_ids or [])]
        )
        windows_creds = await _load_windows_credentials(
            db, [str(c) for c in (profile.windows_credential_ids or [])]
        )
        ssh_creds = await _load_ssh_credentials(
            db, [str(c) for c in (profile.ssh_credential_ids or [])]
        )
        await db.execute(
            update(DiscoveryRun).where(DiscoveryRun.id == run_id).values(
                total_targets=len(targets),
                phase="validating",
                progress_pct=3,
            )
        )
        await db.commit()

    async with AsyncSessionLocal() as db:
        run = await db.get(DiscoveryRun, run_id)
        activity = list(run.activity_log or [])
        _log_event(activity, f"Loaded {len(snmp_creds)} SNMP credential(s), {len(windows_creds)} Windows credential(s), {len(ssh_creds)} SSH credential(s)")
        _log_event(
            activity,
            f"Scanning {len(targets)} target(s) with protocols: {', '.join(protocols_requested)}",
        )
        run.activity_log = activity
        run.phase = "scanning"
        run.progress_pct = 6
        await db.commit()

    # Phase 3: scanning — probe each IP under a concurrency limit
    sem = asyncio.Semaphore(max_concurrency)
    identities: dict[str, dict] = {}
    completed = 0
    responding = 0
    failed = 0

    async def _do(ip: str):
        async with sem:
            try:
                ident = await _probe_ip(
                    ip, protocols_requested, custom_ports,
                    snmp_creds, windows_creds, ssh_creds, timeout_ms,
                )
                return ip, ident
            except Exception as e:
                return ip, {"_responsive": False, "_probes": {},
                            "_error": str(e)}

    tasks = [asyncio.create_task(_do(ip)) for ip in targets]
    batch_pulse = max(1, len(tasks) // 25)
    for i, fut in enumerate(asyncio.as_completed(tasks)):
        ip, ident = await fut
        identities[ip] = ident
        completed += 1
        if ident.get("_responsive"):
            responding += 1
        else:
            failed += 1
        if completed % batch_pulse == 0 or completed == len(tasks):
            pct = 6 + int((completed / len(tasks)) * 76)
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(DiscoveryRun).where(DiscoveryRun.id == run_id).values(
                        completed_targets=completed,
                        responding_targets=responding,
                        failed_targets=failed,
                        progress_pct=min(pct, 82),
                    )
                )
                await db.commit()

    # Phase 4: identifying — already done inline. Pause for UX feel.
    async with AsyncSessionLocal() as db:
        run = await db.get(DiscoveryRun, run_id)
        activity = list(run.activity_log or [])
        _log_event(activity, f"Identified {responding} responding host(s)")
        run.activity_log = activity
        run.phase = "identifying"
        run.progress_pct = 86
        await db.commit()
    await asyncio.sleep(0.2)

    # Phase 5: matching existing inventory
    async with AsyncSessionLocal() as db:
        run = await db.get(DiscoveryRun, run_id)
        run.phase = "matching"
        run.progress_pct = 90
        await db.commit()

    # Pre-load ignored set
    async with AsyncSessionLocal() as db:
        ignored_rows = (await db.execute(text(
            "SELECT ip_address::text AS ip FROM discovery_ignored_devices"
        ))).mappings().all()
    ignored_ips = {r["ip"] for r in ignored_rows}

    new_count = existing_count = changed_count = unknown_count = 0
    ignored_count = cred_failures = duplicates = ready_to_import = 0

    async with AsyncSessionLocal() as db:
        for ip, ident in identities.items():
            is_responding = bool(ident.get("_responsive"))
            error_message = ident.get("_error") if ident.get("_error") else None
            cred_status = ident.get("credential_status", "not_tested")
            if cred_status in ("invalid", "permission_issue"):
                cred_failures += 1

            matched_id: uuid.UUID | None = None
            conflict_type: str | None = None
            status = "unknown"

            if ip in ignored_ips:
                status = "ignored"
                ignored_count += 1
            elif not is_responding:
                status = "failed"
            else:
                matched_id, conflict_type = await _match_existing_device(
                    db, ip, ident.get("mac_address"), ident.get("hostname"),
                )
                if matched_id:
                    if conflict_type:
                        status = "changed"
                        changed_count += 1
                        duplicates += 1
                    else:
                        status = "existing"
                        existing_count += 1
                elif cred_status in ("invalid", "permission_issue"):
                    status = "unknown"
                    unknown_count += 1
                else:
                    status = "new"
                    new_count += 1

            import_ready = (
                status == "new" and is_responding
                and cred_status in ("valid", "not_tested")
            )
            if import_ready:
                ready_to_import += 1

            ip_str = ip
            probes = ident.get("_probes") or {}
            raw = dict(ident.get("raw_data") or {})
            raw["protocol_status"] = _extract_protocol_status(
                probes, protocols_requested, is_responding,
            )

            row = DiscoveryResultV2(
                run_id=run_id,
                profile_id=profile.id,
                ip_address=ip_str,
                mac_address=ident.get("mac_address"),
                hostname=ident.get("hostname"),
                fqdn=ident.get("fqdn"),
                sys_name=ident.get("sys_name"),
                sys_object_id=ident.get("sys_object_id"),
                serial_number=ident.get("serial_number"),
                vendor=ident.get("vendor"),
                device_type=ident.get("device_type"),
                model=ident.get("model"),
                os=ident.get("os"),
                os_version=ident.get("os_version"),
                protocols_detected=ident.get("protocols_detected") or [],
                open_ports=ident.get("open_ports") or [],
                response_time_ms=ident.get("response_time_ms"),
                credential_status=cred_status,
                credential_used=_uuid_or_none(ident.get("credential_used")),
                windows_credential_used=_uuid_or_none(ident.get("windows_credential_used")),
                status=status,
                matched_device_id=matched_id,
                matched_template_id=profile.default_template_id,
                suggested_group_id=profile.default_group_id,
                suggested_tags=profile.default_tags or [],
                confidence_score=ident.get("confidence_score") or 0,
                conflict_type=conflict_type,
                conflict_with_id=matched_id if conflict_type else None,
                import_ready=import_ready,
                error_message=error_message,
                raw_data=_compact_raw(raw),
            )
            db.add(row)
        await db.commit()

    # Phase 6: applying rules (placeholder — rules are evaluated at import)
    async with AsyncSessionLocal() as db:
        run = await db.get(DiscoveryRun, run_id)
        activity = list(run.activity_log or [])
        _log_event(activity, f"Matched {existing_count} existing, {new_count} new, {changed_count} changed")
        if cred_failures:
            _log_event(activity, f"{cred_failures} credential failure(s)", "warning")
        if duplicates:
            _log_event(activity, f"{duplicates} duplicate candidate(s)", "warning")
        run.activity_log = activity
        run.phase = "applying_rules"
        run.progress_pct = 95
        await db.commit()
    await asyncio.sleep(0.1)

    # Phase 7: reporting / done
    duration_ms = int((time.monotonic() - start) * 1000)
    async with AsyncSessionLocal() as db:
        run = await db.get(DiscoveryRun, run_id)
        activity = list(run.activity_log or [])
        _log_event(activity, f"Scan complete in {duration_ms} ms")
        run.activity_log = activity
        run.phase = "done"
        run.progress_pct = 100
        run.new_devices = new_count
        run.existing_devices = existing_count
        run.changed_devices = changed_count
        run.unknown_devices = unknown_count
        run.ignored_devices = ignored_count
        run.credential_failures = cred_failures
        run.duplicate_candidates = duplicates
        run.ready_to_import = ready_to_import
        # status: partial if there were credential failures relative to responding hosts
        if responding == 0 and len(targets) > 0:
            run.status = "completed"  # not failed — empty network is a valid result
        elif cred_failures > 0 and cred_failures == responding:
            run.status = "partial"
        else:
            run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = duration_ms
        await db.commit()

        await db.execute(
            update(DiscoveryProfile).where(DiscoveryProfile.id == profile.id).values(
                last_run_id=run_id,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()


def _uuid_or_none(v) -> uuid.UUID | None:
    if not v:
        return None
    try:
        return uuid.UUID(str(v))
    except Exception:
        return None


def _extract_protocol_status(
    probes: dict[str, Any],
    protocols_requested: list[str],
    responsive: bool,
) -> dict[str, dict[str, Any]]:
    """Compact per-protocol probe outcome for UI (green = responsive, red = failed)."""
    out: dict[str, dict[str, Any]] = {}
    requested = [p.lower() for p in (protocols_requested or [])]

    def _entry(proto: str, ok: bool, error: str | None = None) -> None:
        item: dict[str, Any] = {"responsive": ok}
        if error:
            item["error"] = error[:500]
        out[proto] = item

    icmp = probes.get("icmp") or {}
    _entry("icmp", bool(icmp.get("responsive")), icmp.get("error"))

    if not responsive:
        for proto in requested:
            if proto not in out:
                _entry(proto, False, "host unreachable")
        return out

    open_ports = set((probes.get("tcp") or {}).get("data", {}).get("open", []) or [])

    if "ssh" in requested:
        ssh = probes.get("ssh")
        if ssh:
            _entry("ssh", bool(ssh.get("responsive")), ssh.get("error"))
        else:
            _entry("ssh", False, "port 22 closed" if 22 not in open_ports else "no SSH banner")

    if "snmp" in requested:
        snmp_list = probes.get("snmp") or []
        if snmp_list:
            ok = any(r.get("responsive") for r in snmp_list)
            err = next(
                (r.get("error") for r in snmp_list if r and not r.get("responsive")),
                None,
            )
            _entry("snmp", ok, None if ok else err)
        else:
            _entry("snmp", False, "SNMP not probed")

    if "http" in requested:
        http_list = probes.get("http") or []
        ok = any(r and r.get("responsive") for r in http_list)
        err = next(
            (r.get("error") for r in http_list if r and not r.get("responsive")),
            None,
        )
        if http_list:
            _entry("http", ok, None if ok else err)
        else:
            _entry("http", False, "no HTTP ports open")

    if "https" in requested:
        https_list = probes.get("https") or []
        ok = any(r and r.get("responsive") for r in https_list)
        err = next(
            (r.get("error") for r in https_list if r and not r.get("responsive")),
            None,
        )
        if https_list:
            _entry("https", ok, None if ok else err)
        else:
            _entry("https", False, "no HTTPS ports open")

    winrm_requested = "winrm" in requested or "wmi" in requested
    if winrm_requested:
        winrm_list = probes.get("winrm") or []
        if winrm_list:
            ok = any(r.get("responsive") for r in winrm_list)
            err = next(
                (r.get("error") for r in winrm_list if r and not r.get("responsive")),
                None,
            )
            if "winrm" in requested:
                _entry("winrm", ok, None if ok else err)
            if "wmi" in requested:
                _entry("wmi", ok, None if ok else err)
        else:
            msg = (
                "no Windows credential configured"
                if (5985 in open_ports or 5986 in open_ports)
                else "WinRM ports closed"
            )
            if "winrm" in requested:
                _entry("winrm", False, msg)
            if "wmi" in requested:
                _entry("wmi", False, msg)

    if "tcp" in requested:
        tcp = probes.get("tcp") or {}
        ports = tcp.get("data", {}).get("open", []) or []
        _entry("tcp", bool(ports), None if ports else "no open ports")

    return out


def _compact_raw(raw: dict) -> dict:
    """Trim raw_data to a manageable size before persisting."""
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        if k == "protocol_status":
            out[k] = v
            continue
        if isinstance(v, str) and len(v) > 4000:
            out[k] = v[:4000] + "…"
        elif isinstance(v, list):
            out[k] = v[:10]
        else:
            out[k] = v
    return out


# ────────────────────────────────────────────────────────────────────
# Task wrapper / cancel
# ────────────────────────────────────────────────────────────────────
def start_run_task(run_id: uuid.UUID) -> None:
    if run_id in _RUNNING_TASKS and not _RUNNING_TASKS[run_id].done():
        return

    async def _wrap():
        try:
            await execute_run(run_id)
        except asyncio.CancelledError:
            async with AsyncSessionLocal() as db:
                run = await db.get(DiscoveryRun, run_id)
                if run and run.status in ("running", "queued"):
                    run.status = "cancelled"
                    run.phase = "done"
                    run.completed_at = datetime.now(timezone.utc)
                    await db.commit()
            raise
        except Exception as e:
            async with AsyncSessionLocal() as db:
                run = await db.get(DiscoveryRun, run_id)
                if run:
                    run.status = "failed"
                    run.phase = "done"
                    run.error_details = f"Executor crashed: {e}"
                    run.completed_at = datetime.now(timezone.utc)
                    await db.commit()
        finally:
            _RUNNING_TASKS.pop(run_id, None)

    task = asyncio.create_task(_wrap())
    _RUNNING_TASKS[run_id] = task


def cancel_run(run_id: uuid.UUID) -> bool:
    task = _RUNNING_TASKS.get(run_id)
    if task and not task.done():
        task.cancel()
        return True
    return False
