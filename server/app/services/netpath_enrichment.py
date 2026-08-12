"""NetPath hop enrichment sweeper.

The poller records every hop IP it sees into ``netpath_hop_meta`` with a NULL
``enriched_at``. This loop fills in reverse DNS, ASN / AS-name (via the vendored
MaxMind reader), country, RFC1918/configured internal classification, and — the
integration that is SolarWinds NetPath's real moat — a match to a monitored
ZenPlus device, so an internal hop links straight to its CPU/interface page.

Advisory-locked so it is safe to start in every Uvicorn worker.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.services import geoip

logger = logging.getLogger("netpath.enrichment")

NETPATH_ENRICH_LOCK = 1515074394
ENRICH_INTERVAL_S = 30
BATCH = 200
# re-check long-lived enrichment (device came online, rDNS changed) occasionally
REFRESH_HOURS = 24


async def _reverse_dns(ip: str) -> str | None:
    def _lookup() -> str | None:
        try:
            socket.setdefaulttimeout(2.0)
            host, _, _ = socket.gethostbyaddr(ip)
            return host
        except Exception:
            return None

    return await asyncio.to_thread(_lookup)


def _is_internal(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False


async def _enrich_one(db: AsyncSession, ip: str) -> None:
    hostname = await _reverse_dns(ip)
    info = geoip.enrich(ip)  # {country, country_name, asn, as_name}
    internal = _is_internal(ip)

    # match a monitored device by IP (the NetPath "drill into your own hop" moat).
    # A device match is a separate highlight from the internal/external boundary —
    # a monitored node can be either — so it does not force is_internal.
    device_id = (await db.execute(text(
        "SELECT id FROM devices WHERE ip_address = CAST(:ip AS inet) LIMIT 1"
    ), {"ip": ip})).scalar()

    await db.execute(text("""
        UPDATE netpath_hop_meta
           SET hostname = :hostname, asn = :asn, as_name = :as_name, country = :country,
               is_internal = :internal, device_id = :device_id,
               enriched_at = NOW(), updated_at = NOW()
         WHERE ip = CAST(:ip AS inet)
    """), {
        "hostname": hostname, "asn": info.get("asn"),
        "as_name": (info.get("as_name") or None), "country": info.get("country"),
        "internal": internal, "device_id": device_id, "ip": ip,
    })


async def run_enrichment_once(db: AsyncSession) -> int:
    got = (await db.execute(text(
        "SELECT pg_try_advisory_xact_lock(:k)"), {"k": NETPATH_ENRICH_LOCK})).scalar()
    if not got:
        await db.rollback()
        return 0
    rows = (await db.execute(text("""
        SELECT host(ip)::text AS ip FROM netpath_hop_meta
        WHERE enriched_at IS NULL
           OR enriched_at < NOW() - make_interval(hours => :rh)
        ORDER BY enriched_at NULLS FIRST
        LIMIT :lim
    """), {"rh": REFRESH_HOURS, "lim": BATCH})).mappings().all()
    n = 0
    for r in rows:
        try:
            await _enrich_one(db, r["ip"])
            n += 1
        except Exception:
            logger.exception("netpath enrich failed for %s", r["ip"])
    await db.commit()
    return n


async def netpath_enrichment_loop() -> None:
    await asyncio.sleep(25)
    logger.info("NetPath enrichment sweeper started (interval %ss)", ENRICH_INTERVAL_S)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await run_enrichment_once(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("netpath enrichment sweep failed")
        await asyncio.sleep(ENRICH_INTERVAL_S)
