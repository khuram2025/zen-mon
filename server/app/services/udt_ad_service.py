"""UDT Active Directory user-login correlation.

Agentless: polls the Security event log of each configured domain
controller over WinRM (pywinrm) with a saved Windows credential and
records logon events (4768 Kerberos TGT, 4769 service ticket, 4624
interactive/network logon). Each event maps user -> workstation
IP/hostname, which is correlated to a UDT endpoint by IP or hostname.

This is a superset of SolarWinds UDT, which only reads 4768/4769; we
also consume 4624 so NTLM-only and non-Kerberos environments still
attribute users to endpoints.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.core.database import AsyncSessionLocal, engine

logger = logging.getLogger("zenplus.udt_ad")

POLL_TICK_S = 30            # scheduler granularity; per-DC interval respected
DEFAULT_LOOKBACK_MIN = 30   # first poll window when no checkpoint exists
MAX_EVENTS = 500
UDT_AD_ADVISORY_LOCK = 1515074392

# PowerShell: emit recent logon events as a compact JSON array. We ask
# for 4624 (logon) + 4768/4769 (Kerberos). TargetUserName/IpAddress are
# extracted from the event XML. Machine/service accounts (name ends $)
# and ANONYMOUS/system are filtered out.
_PS_TEMPLATE = r"""
$since = [DateTime]::Parse('{since_iso}', $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
$ids = @(4624,4768,4769)
try {{
  $events = Get-WinEvent -FilterHashtable @{{ LogName='Security'; Id=$ids; StartTime=$since }} -MaxEvents {max_events} -ErrorAction Stop
}} catch {{
  if ($_.Exception.Message -match 'No events were found') {{ '[]'; exit 0 }} else {{ throw }}
}}
$out = foreach ($e in $events) {{
  $x = [xml]$e.ToXml()
  $d = @{{}}
  foreach ($n in $x.Event.EventData.Data) {{ $d[$n.Name] = $n.'#text' }}
  $user = $d['TargetUserName']
  if (-not $user) {{ continue }}
  if ($user.EndsWith('$')) {{ continue }}
  if ($user -in @('ANONYMOUS LOGON','SYSTEM','LOCAL SERVICE','NETWORK SERVICE')) {{ continue }}
  $ip = $d['IpAddress']
  if (-not $ip) {{ $ip = $d['ClientAddress'] }}
  if ($ip -in @('-','::1','127.0.0.1','0.0.0.0')) {{ $ip = $null }}
  [pscustomobject]@{{
    Id = $e.Id
    Time = $e.TimeCreated.ToUniversalTime().ToString('o')
    User = $user
    Domain = $d['TargetDomainName']
    Ip = $ip
    Workstation = $d['WorkstationName']
    LogonType = $d['LogonType']
  }}
}}
if ($out) {{ $out | ConvertTo-Json -Compress -Depth 3 }} else {{ '[]' }}
"""


async def _due_controllers(db: AsyncSession) -> list:
    return (await db.execute(text(
        """SELECT dc.id, dc.name, dc.host, dc.poll_interval_s, dc.last_event_time,
                  wc.username, wc.domain, wc.password_enc, wc.auth_method, wc.transport,
                  wc.port, wc.ssl_verify
           FROM udt_domain_controllers dc
           JOIN windows_credentials wc ON wc.id = dc.windows_credential_id
           WHERE dc.enabled
             AND (dc.last_poll_at IS NULL
                  OR dc.last_poll_at < NOW() - make_interval(secs => dc.poll_interval_s))"""
    ))).mappings().all()


def _run_winrm(host: str, cred: dict, since_iso: str) -> list[dict]:
    import winrm

    username = cred["username"]
    domain = cred.get("domain") or ""
    full_user = f"{domain}\\{username}" if domain else username
    transport = cred.get("transport", "http")
    port = cred.get("port") or (5985 if transport == "http" else 5986)
    endpoint = f"{transport}://{host}:{port}/wsman"
    ps = _PS_TEMPLATE.format(since_iso=since_iso, max_events=MAX_EVENTS)
    session = winrm.Session(
        endpoint,
        auth=(full_user, cred.get("password", "")),
        transport=cred.get("auth_method", "ntlm"),
        server_cert_validation="validate",
        message_encryption="always" if endpoint.startswith("http://") else "auto",
        operation_timeout_sec=30,
        read_timeout_sec=40,
    )
    result = session.run_ps(ps)
    if result.status_code != 0:
        err = (result.std_err or b"").decode("utf-8", "ignore")[:400]
        raise RuntimeError(err or "winrm non-zero exit")
    raw = (result.std_out or b"").decode("utf-8", "ignore").strip() or "[]"
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    return data


async def _record_events(db: AsyncSession, dc, events: list[dict]) -> tuple[int, datetime | None]:
    inserted = 0
    newest: datetime | None = None
    for ev in events:
        try:
            ts = datetime.fromisoformat(ev["Time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if newest is None or ts > newest:
            newest = ts
        user = (ev.get("User") or "").strip()
        if not user:
            continue
        ip = ev.get("Ip")
        # A DC may report a non-IP value (or a hostname) in IpAddress; drop
        # anything that isn't a valid inet so a single bad event can't fail
        # the whole batch on the ::inet cast below.
        if ip:
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                ip = None
        hostname = ev.get("Workstation")
        try:
            event_id = int(ev.get("Id")) if ev.get("Id") is not None else None
        except (TypeError, ValueError):
            event_id = None
        try:
            logon_type = int(ev.get("LogonType")) if ev.get("LogonType") else None
        except (TypeError, ValueError):
            logon_type = None

        # Correlate to an endpoint by active IP binding, then hostname.
        endpoint_id = None
        if ip:
            row = (await db.execute(text(
                """SELECT e.id FROM udt_endpoints e
                   WHERE e.ip_address = CAST(:ip AS inet)
                      OR EXISTS (SELECT 1 FROM udt_ip_history h
                                 WHERE h.endpoint_id = e.id AND h.active AND h.ip = CAST(:ip AS inet))
                   ORDER BY e.last_seen DESC LIMIT 1"""
            ), {"ip": ip})).first()
            if row:
                endpoint_id = row[0]
        if endpoint_id is None and hostname:
            row = (await db.execute(text(
                "SELECT id FROM udt_endpoints WHERE LOWER(hostname) = LOWER(:h) ORDER BY last_seen DESC LIMIT 1"
            ), {"h": hostname})).first()
            if row:
                endpoint_id = row[0]

        res = await db.execute(text(
            """INSERT INTO udt_user_logins
                 (user_name, user_domain, endpoint_id, ip, hostname, event_id, logon_type, dc_id, event_time)
               VALUES (:u, :dom, :eid, :ip, :host, :evid, :lt, :dc, :ts)
               ON CONFLICT (dc_id, event_time, user_name, COALESCE(ip, '0.0.0.0'::inet), COALESCE(event_id, 0))
               DO NOTHING"""
        ), {"u": user, "dom": ev.get("Domain"), "eid": endpoint_id,
            "ip": ip, "host": hostname, "evid": event_id, "lt": logon_type,
            "dc": str(dc["id"]), "ts": ts})
        if res.rowcount:
            inserted += 1
            if endpoint_id is not None:
                await db.execute(text(
                    """UPDATE udt_endpoints
                       SET user_name = :u, user_domain = :dom, user_seen_at = :ts, updated_at = NOW()
                       WHERE id = :id AND (user_seen_at IS NULL OR user_seen_at < :ts)"""
                ), {"u": user, "dom": ev.get("Domain"), "ts": ts, "id": endpoint_id})
                await db.execute(text(
                    "INSERT INTO udt_events (event_type, endpoint_id, details) "
                    "VALUES ('user_login', :id, CAST(:dj AS jsonb))"
                ), {"id": endpoint_id, "dj": json.dumps({"user": user, "event_id": event_id})})
    return inserted, newest


async def poll_controller(db: AsyncSession, dc) -> dict:
    """Poll one DC. Returns a status dict; also updates the DC row."""
    from datetime import timedelta

    # Basic auth puts the domain password on the wire base64-encoded, with no
    # message-level protection — NTLM and Kerberos seal the payload even over
    # plain HTTP, Basic does not. Refuse rather than leak it.
    if (dc["auth_method"] or "").lower() == "basic" and (dc["transport"] or "http").lower() != "https":
        await db.execute(text(
            "UPDATE udt_domain_controllers SET last_poll_at = NOW(), last_status = 'error', "
            "last_error = :e WHERE id = :id"
        ), {"e": "credential uses Basic authentication over HTTP, which would send the "
                 "password in the clear — switch the credential to NTLM/Kerberos, or to HTTPS",
            "id": str(dc["id"])})
        return {"status": "error", "error": "Basic auth over HTTP refused"}

    since = dc["last_event_time"] or (datetime.now(timezone.utc) - timedelta(minutes=DEFAULT_LOOKBACK_MIN))
    since_iso = since.astimezone(timezone.utc).isoformat()
    cred = {
        "username": dc["username"], "domain": dc["domain"],
        "password": decrypt(dc["password_enc"]) if dc["password_enc"] else "",
        "auth_method": dc["auth_method"], "transport": dc["transport"],
        "port": dc["port"], "ssl_verify": dc["ssl_verify"],
    }
    try:
        import winrm  # noqa: F401
    except ImportError:
        await db.execute(text(
            "UPDATE udt_domain_controllers SET last_poll_at = NOW(), last_status = 'error', "
            "last_error = 'pywinrm not installed on the ZenPlus server' WHERE id = :id"
        ), {"id": str(dc["id"])})
        return {"status": "error", "error": "pywinrm not installed"}

    loop = asyncio.get_running_loop()
    try:
        events = await asyncio.wait_for(
            loop.run_in_executor(None, _run_winrm, dc["host"], cred, since_iso), timeout=60
        )
    except Exception as exc:  # noqa: BLE001
        await db.execute(text(
            "UPDATE udt_domain_controllers SET last_poll_at = NOW(), last_status = 'error', last_error = :e WHERE id = :id"
        ), {"e": str(exc)[:500], "id": str(dc["id"])})
        return {"status": "error", "error": str(exc)}

    inserted, newest = await _record_events(db, dc, events)
    await db.execute(text(
        """UPDATE udt_domain_controllers
           SET last_poll_at = NOW(), last_status = 'ok', last_error = NULL,
               last_event_time = GREATEST(COALESCE(last_event_time, 'epoch'::timestamptz), COALESCE(:newest, last_event_time, 'epoch'::timestamptz))
           WHERE id = :id"""
    ), {"newest": newest, "id": str(dc["id"])})
    return {"status": "ok", "events": len(events), "inserted": inserted}


async def udt_ad_poller_loop() -> None:
    await asyncio.sleep(35)
    logger.info("UDT AD login poller started (tick %ss)", POLL_TICK_S)
    while True:
        try:
            # The lock is held on its OWN dedicated connection for the whole
            # poll, because poll_controller commits per-DC on the work session
            # — a session-scoped lock taken on the work session would be
            # released the moment that connection returned to the pool. The
            # lock connection is separate and stays checked out, so the lock
            # survives the per-DC commits and is released only when it closes.
            async with engine.connect() as lock_conn:
                got = (await lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": UDT_AD_ADVISORY_LOCK}
                )).scalar()
                if not got:
                    continue
                try:
                    async with AsyncSessionLocal() as db:
                        controllers = await _due_controllers(db)
                        for dc in controllers:
                            try:
                                res = await poll_controller(db, dc)
                                await db.commit()
                                if res.get("inserted"):
                                    logger.info("UDT AD %s: %d new logins", dc["name"], res["inserted"])
                            except Exception:
                                await db.rollback()
                                logger.exception("UDT AD poll failed for %s", dc["name"])
                finally:
                    # Session-scoped lock: a pool rollback does NOT release it,
                    # so unlock explicitly on the same dedicated connection.
                    await lock_conn.execute(
                        text("SELECT pg_advisory_unlock(:k)"), {"k": UDT_AD_ADVISORY_LOCK}
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("UDT AD poll failed")
        await asyncio.sleep(POLL_TICK_S)
