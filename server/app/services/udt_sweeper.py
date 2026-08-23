"""UDT background sweeper.

Runs every 60 s (Postgres advisory-locked, so multi-worker safe):
  1. close stale endpoint-location sessions and IP bindings
  2. seed the OUI vendor table when empty (best-effort download)
  2b. apply classification rules, then vendor/hostname heuristics
  3. apply ignore rules
  4. apply allow rules -> rogue flagging (+ rogue_detected events)
  5. apply watch rules (+ watch_seen events on reappearance)
  6. reverse-DNS hostname enrichment (budgeted)
  7. daily port-capacity snapshots
  8. retention pruning (90 days of history)
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import socket
import time
import urllib.request
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.services.udt_service import apply_class_rules, classify_endpoint, rules_where

logger = logging.getLogger("zenplus.udt")

SWEEP_INTERVAL_S = 60
SESSION_GRACE = "15 minutes"     # ~3 missed 5-minute UDT polls
IP_GRACE = "24 hours"
RETENTION = "90 days"
DNS_BUDGET_PER_TICK = 100
DNS_TIMEOUT_S = 1.5
WATCH_SEEN_DEDUPE = "1 hour"

UDT_SWEEP_ADVISORY_LOCK = 1515074391


async def _close_stale(db: AsyncSession) -> None:
    await db.execute(text(
        f"UPDATE udt_endpoint_locations SET active = FALSE, closed_at = NOW() "
        f"WHERE active AND last_seen < NOW() - INTERVAL '{SESSION_GRACE}'"
    ))
    await db.execute(text(
        f"UPDATE udt_ip_history SET active = FALSE "
        f"WHERE active AND last_seen < NOW() - INTERVAL '{IP_GRACE}'"
    ))


# ── OUI vendor seeding ───────────────────────────────────────────────
# Fresh installs ship with an empty udt_oui table; without it every
# endpoint shows an unknown vendor and the heuristic classifier is
# blind. Best-effort: when the table is empty, download the IEEE
# registry (Wireshark manuf as fallback) at most once per retry window.

OUI_RETRY_S = 6 * 3600
_OUI_SOURCES = (
    "https://standards-oui.ieee.org/oui/oui.csv",
    "https://www.wireshark.org/download/automated/data/manuf",
)
_oui_next_attempt = 0.0


def _download_ouis() -> dict[str, str]:
    out: dict[str, str] = {}
    for url in _OUI_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "zenplus-udt/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            logger.warning("OUI download failed (%s): %s", url, exc)
            continue
        if url.endswith(".csv"):
            reader = csv.reader(io.StringIO(raw))
            header = next(reader, None)
            if header and "Assignment" in header:
                ai, oi = header.index("Assignment"), header.index("Organization Name")
                for row in reader:
                    if len(row) > max(ai, oi) and len(row[ai].strip()) == 6 and row[oi].strip():
                        out[row[ai].strip().lower()] = row[oi].strip()[:255]
        else:  # wireshark manuf: "aa:bb:cc<TAB>Short<TAB>Vendor name"
            for line in raw.splitlines():
                parts = line.strip().split("\t")
                if len(parts) < 2 or parts[0].startswith("#") or "/" in parts[0]:
                    continue
                prefix = parts[0].replace(":", "").replace("-", "").lower()
                vendor = (parts[2] if len(parts) > 2 else parts[1]).strip()
                if len(prefix) == 6 and vendor:
                    out[prefix] = vendor[:255]
        if len(out) > 1000:
            break
    return out


async def _seed_oui_if_empty(db: AsyncSession) -> None:
    global _oui_next_attempt
    if (await db.execute(text("SELECT EXISTS (SELECT 1 FROM udt_oui)"))).scalar():
        return
    now = time.monotonic()
    if now < _oui_next_attempt:
        return
    _oui_next_attempt = now + OUI_RETRY_S
    ouis = await asyncio.to_thread(_download_ouis)
    if len(ouis) < 100:
        return
    prefixes, vendors = zip(*sorted(ouis.items()))
    await db.execute(text(
        "INSERT INTO udt_oui (prefix, vendor) "
        "SELECT * FROM unnest(CAST(:p AS varchar[]), CAST(:v AS varchar[])) "
        "ON CONFLICT (prefix) DO UPDATE SET vendor = EXCLUDED.vendor"
    ), {"p": list(prefixes), "v": list(vendors)})
    await db.execute(text(
        "UPDATE udt_endpoints e SET vendor = o.vendor, updated_at = NOW() "
        "FROM udt_oui o WHERE e.vendor IS NULL "
        "AND o.prefix = replace(substring(e.mac::text, 1, 8), ':', '')"
    ))
    logger.info("seeded udt_oui with %d vendor prefixes", len(ouis))


async def _classify(db: AsyncSession) -> None:
    await apply_class_rules(db)
    rows = (await db.execute(text(
        """SELECT e.id, e.vendor, e.hostname, d.device_type
           FROM udt_endpoints e
           LEFT JOIN devices d ON d.id = e.device_id
           WHERE e.endpoint_type = 'unknown' AND e.type_source = 'auto'
             AND (e.vendor IS NOT NULL OR e.hostname IS NOT NULL OR e.device_id IS NOT NULL)
           LIMIT 2000"""
    ))).mappings().all()
    for r in rows:
        etype = classify_endpoint(r["vendor"], r["hostname"], r["device_type"])
        if etype != "unknown":
            await db.execute(text(
                "UPDATE udt_endpoints SET endpoint_type = :t, updated_at = NOW() WHERE id = :id"
            ), {"t": etype, "id": r["id"]})


async def _load_rules(db: AsyncSession, list_type: str) -> list[dict]:
    rows = (await db.execute(text(
        "SELECT id, match_type, pattern FROM udt_rules WHERE enabled AND list_type = :lt"
    ), {"lt": list_type})).mappings().all()
    return [dict(r) for r in rows]


async def _apply_ignore(db: AsyncSession) -> None:
    # Manually-ignored endpoints (ignored_manual) are never touched here.
    rules = await _load_rules(db, "ignore")
    params: dict = {}
    cond = rules_where(rules, params)
    if cond is None:
        await db.execute(text(
            "UPDATE udt_endpoints SET ignored = FALSE WHERE ignored AND NOT ignored_manual"
        ))
        return
    await db.execute(text(
        f"UPDATE udt_endpoints e SET ignored = TRUE, updated_at = NOW() "
        f"WHERE NOT e.ignored AND NOT e.ignored_manual AND {cond}"
    ), params)
    await db.execute(text(
        f"UPDATE udt_endpoints e SET ignored = FALSE, updated_at = NOW() "
        f"WHERE e.ignored AND NOT e.ignored_manual AND NOT {cond}"
    ), params)


async def _apply_allow(db: AsyncSession) -> None:
    """Rogue detection: with at least one allow rule configured, any
    non-ignored endpoint matching no allow rule is flagged rogue."""
    # Endpoints with authorized_override set were pinned by an operator and
    # are excluded from every auto-update below.
    rules = await _load_rules(db, "allow")
    params: dict = {}
    cond = rules_where(rules, params)
    if cond is None:
        # No allow list configured -> tri-state back to unclassified.
        await db.execute(text(
            "UPDATE udt_endpoints SET authorized = NULL, updated_at = NOW() "
            "WHERE authorized IS NOT NULL AND authorized_override IS NULL"
        ))
        return
    await db.execute(text(
        f"UPDATE udt_endpoints e SET authorized = TRUE, updated_at = NOW() "
        f"WHERE e.authorized_override IS NULL AND COALESCE(e.authorized, FALSE) IS DISTINCT FROM TRUE AND {cond}"
    ), params)
    newly_rogue = (await db.execute(text(
        f"UPDATE udt_endpoints e SET authorized = FALSE, updated_at = NOW() "
        f"WHERE e.authorized_override IS NULL AND e.authorized IS DISTINCT FROM FALSE "
        f"  AND NOT e.ignored AND NOT {cond} "
        f"RETURNING e.id, e.mac::text AS mac"
    ), params)).mappings().all()
    for r in newly_rogue:
        loc = (await db.execute(text(
            "SELECT device_id, if_index FROM udt_endpoint_locations "
            "WHERE endpoint_id = :id AND active ORDER BY is_direct DESC, last_seen DESC LIMIT 1"
        ), {"id": r["id"]})).first()
        await db.execute(text(
            "INSERT INTO udt_events (event_type, endpoint_id, device_id, if_index, details) "
            "VALUES ('rogue_detected', :id, :dev, :ifx, CAST(:dj AS jsonb))"
        ), {"id": r["id"], "dev": loc[0] if loc else None,
            "ifx": loc[1] if loc else None,
            "dj": f'{{"mac": "{r["mac"]}"}}'})


async def _apply_watch(db: AsyncSession) -> None:
    rules = await _load_rules(db, "watch")
    params: dict = {}
    cond = rules_where(rules, params)
    if cond is not None:
        await db.execute(text(
            f"UPDATE udt_endpoints e SET is_watched = TRUE, updated_at = NOW() "
            f"WHERE NOT e.is_watched AND {cond}"
        ), params)
    # watch_seen: a watched endpoint with an active session and no
    # watch_seen event within the dedupe window.
    seen = (await db.execute(text(
        f"""SELECT e.id, e.mac::text AS mac, l.device_id, l.if_index
            FROM udt_endpoints e
            JOIN LATERAL (
                SELECT device_id, if_index FROM udt_endpoint_locations
                WHERE endpoint_id = e.id AND active
                ORDER BY is_direct DESC, last_seen DESC LIMIT 1
            ) l ON TRUE
            WHERE e.is_watched AND NOT e.ignored
              AND NOT EXISTS (
                SELECT 1 FROM udt_events ev
                WHERE ev.endpoint_id = e.id AND ev.event_type = 'watch_seen'
                  AND ev.created_at > NOW() - INTERVAL '{WATCH_SEEN_DEDUPE}'
              )"""
    ))).mappings().all()
    for r in seen:
        await db.execute(text(
            "INSERT INTO udt_events (event_type, endpoint_id, device_id, if_index, details) "
            "VALUES ('watch_seen', :id, :dev, :ifx, CAST(:dj AS jsonb))"
        ), {"id": r["id"], "dev": r["device_id"], "ifx": r["if_index"],
            "dj": f'{{"mac": "{r["mac"]}"}}'})


async def _enrich_dns(db: AsyncSession) -> None:
    rows = (await db.execute(text(
        """SELECT id, host(ip_address) AS ip FROM udt_endpoints
           WHERE ip_address IS NOT NULL AND hostname IS NULL
             AND last_seen > NOW() - INTERVAL '1 day'
           ORDER BY last_seen DESC LIMIT :lim"""
    ), {"lim": DNS_BUDGET_PER_TICK})).mappings().all()
    if not rows:
        return
    loop = asyncio.get_running_loop()

    async def resolve(ip: str) -> str | None:
        try:
            name, _ = await asyncio.wait_for(
                loop.getnameinfo((ip, 0), socket.NI_NAMEREQD), timeout=DNS_TIMEOUT_S
            )
            return name
        except Exception:
            return None

    results = await asyncio.gather(*(resolve(r["ip"]) for r in rows))
    for r, name in zip(rows, results):
        if name and name != r["ip"]:
            await db.execute(text(
                "UPDATE udt_endpoints SET hostname = :h, updated_at = NOW() WHERE id = :id"
            ), {"h": name[:255], "id": r["id"]})


async def _capacity_snapshot(db: AsyncSession) -> None:
    await db.execute(text(
        """INSERT INTO udt_port_capacity_daily (device_id, day, total_ports, used_ports, active_ports, uplink_ports)
           SELECT di.device_id, CURRENT_DATE,
                  COUNT(*) FILTER (WHERE di.if_type IS NULL OR di.if_type IN (6, 117)),
                  COUNT(*) FILTER (WHERE di.oper_status = 'up' AND (di.if_type IS NULL OR di.if_type IN (6, 117))),
                  COUNT(*) FILTER (WHERE ps.last_endpoint_seen > NOW() - INTERVAL '1 day'),
                  COUNT(*) FILTER (WHERE ps.is_uplink)
           FROM device_interfaces di
           LEFT JOIN udt_port_state ps ON ps.device_id = di.device_id AND ps.if_index = di.if_index
           WHERE EXISTS (SELECT 1 FROM udt_port_state p2 WHERE p2.device_id = di.device_id)
           GROUP BY di.device_id
           ON CONFLICT (device_id, day) DO UPDATE SET
               total_ports = EXCLUDED.total_ports,
               used_ports = EXCLUDED.used_ports,
               active_ports = EXCLUDED.active_ports,
               uplink_ports = EXCLUDED.uplink_ports"""
    ))


async def _prune(db: AsyncSession) -> None:
    await db.execute(text(
        f"DELETE FROM udt_events WHERE created_at < NOW() - INTERVAL '{RETENTION}'"
    ))
    await db.execute(text(
        f"DELETE FROM udt_endpoint_locations WHERE NOT active AND last_seen < NOW() - INTERVAL '{RETENTION}'"
    ))
    await db.execute(text(
        f"DELETE FROM udt_user_logins WHERE event_time < NOW() - INTERVAL '{RETENTION}'"
    ))
    await db.execute(text(
        f"DELETE FROM udt_ip_history WHERE NOT active AND last_seen < NOW() - INTERVAL '{RETENTION}'"
    ))


async def run_sweep_once(db: AsyncSession) -> bool:
    """One sweep pass under a transaction-scoped advisory lock. Returns
    False when another worker holds the lock.

    A *transaction*-scoped lock (pg_try_advisory_xact_lock) is used, not a
    session-scoped one: with a pooled connection a session-scoped lock is
    bound to the physical connection, and the commit below would return
    that connection to the pool so a later unlock would run on a different
    connection and silently leak the lock. The xact lock is released
    automatically by the commit (success) or rollback (error) that ends
    this single transaction.
    """
    got = (await db.execute(
        text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": UDT_SWEEP_ADVISORY_LOCK}
    )).scalar()
    if not got:
        await db.rollback()
        return False
    try:
        await _close_stale(db)
        await _seed_oui_if_empty(db)
        await _classify(db)
        await _apply_ignore(db)
        await _apply_allow(db)
        await _apply_watch(db)
        await _enrich_dns(db)
        await _capacity_snapshot(db)
        await _prune(db)
        await db.commit()  # releases the xact lock
        return True
    except Exception:
        await db.rollback()  # releases the xact lock
        raise


async def udt_sweeper_loop() -> None:
    await asyncio.sleep(20)
    logger.info("UDT sweeper started (interval %ss)", SWEEP_INTERVAL_S)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await run_sweep_once(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("UDT sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_S)
