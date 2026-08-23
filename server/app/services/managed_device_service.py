"""Controller-managed child device sync.

Vendor packs can mark a table group as `children` (see OidGroupChildren in
schemas/snmp.py): rows of that group describe devices the polled controller
manages — FortiGate's FortiAPs and FortiLink switches, a wireless
controller's thin APs. For every controller with promote_managed enabled
this service materializes those rows as first-class child devices, so alert
rules, maintenance windows, tags and reports apply per AP/switch.

Design rules (the ones professional NMSes converge on):
  * The controller is the inventory source of truth — children are created,
    re-named and re-parented from its tables, never hand-added.
  * Identity is serial number when the pack collects one, else the stable
    (controller, group, table-row) triple. A serial that matches a device we
    already poll directly just *links* the records — the direct device keeps
    its polling and status untouched.
  * Children are poll_mode='via_controller': ping/SNMP stay disabled, status
    is translated from the vendor's enum via the pack's status_map, and a
    child that disappears from the controller decays to 'unknown' after a
    grace period instead of being deleted (history survives; an AP moving
    between controllers is re-parented, not recreated).
  * Every child gets a 'controller' row in topology_dependencies, so the
    existing dependency-aware suppression turns a controller outage into one
    root-cause alert instead of one alert per AP.
  * First sight of a child sets a status baseline silently; only *observed
    transitions* fire the alert engine. Promoting a fleet never replays a
    storm of stale 'down' alerts.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal

logger = logging.getLogger("zenplus.managed_devices")

SYNC_INTERVAL_S = 60
# A child unreported for this long goes 'unknown'. Template polls default to
# 60 s, so this rides out ~10 missed polls or a controller reboot.
STALE_AFTER_MINUTES = 10
# udt_sweeper uses ...391; any distinct constant works.
CHILD_SYNC_ADVISORY_LOCK = 1515074392

_ALERTABLE = ("down", "degraded")


def _as_code(value_num) -> str | None:
    if value_num is None:
        return None
    try:
        return str(int(value_num))
    except (TypeError, ValueError):
        return None


def _clean_ip(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        ip = ipaddress.ip_address(raw.strip())
    except ValueError:
        return None
    if ip.is_unspecified:
        return None
    return str(ip)


def _clean(raw: str | None, cap: int) -> str | None:
    if raw is None:
        return None
    out = raw.strip()
    return out[:cap] if out else None


def _child_specs(oid_groups) -> dict[str, dict]:
    """{group_key: children-spec} for table groups that declare children."""
    if isinstance(oid_groups, str):
        oid_groups = json.loads(oid_groups)
    specs: dict[str, dict] = {}
    for gr in oid_groups or []:
        if gr.get("kind") == "table" and isinstance(gr.get("children"), dict):
            specs[gr["key"]] = gr["children"]
    return specs


async def _controller_report(db: AsyncSession, controller_id, specs: dict[str, dict]) -> list[dict]:
    """Translate the controller's latest template rows into child records."""
    vals = (await db.execute(
        text("""
            SELECT group_key, metric_key, instance, label, value_num, value_text, updated_at
            FROM device_template_values
            WHERE device_id = :cid AND group_key = ANY(:keys)
        """),
        {"cid": str(controller_id), "keys": list(specs.keys())},
    )).mappings().all()

    rows: dict[tuple[str, str], dict] = {}
    for v in vals:
        key = (v["group_key"], v["instance"])
        row = rows.setdefault(key, {"label": "", "cells": {}, "seen_at": v["updated_at"]})
        if v["label"]:
            row["label"] = v["label"]
        if v["updated_at"] and v["updated_at"] > row["seen_at"]:
            row["seen_at"] = v["updated_at"]
        row["cells"][v["metric_key"]] = (v["value_num"], v["value_text"])

    report = []
    for (group_key, instance), row in rows.items():
        spec = specs[group_key]
        cells = row["cells"]

        def cell_text(key_name: str | None) -> str | None:
            if not key_name or key_name not in cells:
                return None
            return cells[key_name][1] or None

        status = None
        if spec.get("status_key"):
            code = _as_code(cells.get(spec["status_key"], (None, ""))[0])
            status = spec.get("status_map", {}).get(code, "unknown") if code is not None else "unknown"

        report.append({
            "group_key": group_key,
            "instance": _clean(instance, 160) or instance,
            "hostname": _clean(row["label"], 255) or f"{spec.get('device_type', 'device')}-{instance}",
            "device_type": spec.get("device_type", "other"),
            "vendor": _clean(spec.get("vendor"), 100),
            "status": status,
            "model": _clean(cell_text(spec.get("model_key")), 255),
            "os_version": _clean(cell_text(spec.get("os_version_key")), 255),
            "serial": _clean(cell_text(spec.get("serial_key")), 128),
            "managed_ip": _clean_ip(cell_text(spec.get("ip_key"))),
            "seen_at": row["seen_at"],
        })
    return report


async def _sync_controller(db: AsyncSession, ctrl) -> dict:
    """Upsert one controller's children. Writes, no commit."""
    specs = _child_specs(ctrl["oid_groups"])
    summary = {"created": 0, "updated": 0, "reported": 0, "transitions": []}
    if not specs:
        return summary

    report = await _controller_report(db, ctrl["id"], specs)
    summary["reported"] = len(report)
    if not report:
        return summary

    existing = (await db.execute(
        text("""
            SELECT id, hostname, status, poll_mode, serial_number,
                   managed_source, managed_instance
            FROM devices WHERE managed_by_device_id = :cid
        """),
        {"cid": str(ctrl["id"])},
    )).mappings().all()
    by_key = {(r["managed_source"], r["managed_instance"]): r for r in existing}

    serials = [e["serial"] for e in report if e["serial"]]
    by_serial = {}
    if serials:
        rows = (await db.execute(
            text("""
                SELECT id, hostname, status, poll_mode, serial_number,
                       managed_source, managed_instance
                FROM devices WHERE serial_number = ANY(:serials)
            """),
            {"serials": serials},
        )).mappings().all()
        by_serial = {r["serial_number"]: r for r in rows}

    for entry in report:
        target = by_serial.get(entry["serial"]) if entry["serial"] else None
        if target is None:
            target = by_key.get((entry["group_key"], entry["instance"]))

        if target is None:
            created = (await db.execute(
                text("""
                    INSERT INTO devices
                        (hostname, ip_address, device_type, group_id, tags,
                         ping_enabled, snmp_enabled, status, last_seen,
                         vendor, model, os_version, serial_number,
                         poll_mode, managed_by_device_id, managed_ip,
                         managed_source, managed_instance, managed_last_seen)
                    VALUES
                        (:hostname, NULL, :device_type, :group_id, '[]'::jsonb,
                         FALSE, FALSE, :status, :last_seen,
                         :vendor, :model, :os_version, :serial,
                         'via_controller', :cid, :managed_ip,
                         :group_key, :instance, :seen_at)
                    ON CONFLICT (managed_by_device_id, managed_source, managed_instance)
                        WHERE managed_instance IS NOT NULL
                        DO NOTHING
                    RETURNING id
                """),
                {
                    "hostname": entry["hostname"],
                    "device_type": entry["device_type"],
                    "group_id": ctrl["group_id"],
                    "status": entry["status"] or "unknown",
                    "last_seen": entry["seen_at"] if entry["status"] == "up" else None,
                    "vendor": entry["vendor"],
                    "model": entry["model"],
                    "os_version": entry["os_version"],
                    "serial": entry["serial"],
                    "cid": str(ctrl["id"]),
                    "managed_ip": entry["managed_ip"],
                    "group_key": entry["group_key"],
                    "instance": entry["instance"],
                    "seen_at": entry["seen_at"],
                },
            )).first()
            if created:
                summary["created"] += 1
            # First sight is a baseline, deliberately no status-change event.
            continue

        is_child = target["poll_mode"] == "via_controller"
        sets = [
            "managed_by_device_id = :cid",
            "managed_source = :group_key",
            "managed_instance = :instance",
            "managed_last_seen = :seen_at",
            "updated_at = NOW()",
        ]
        params = {
            "id": str(target["id"]),
            "cid": str(ctrl["id"]),
            "group_key": entry["group_key"],
            "instance": entry["instance"],
            "seen_at": entry["seen_at"],
        }
        for col, val in (
            ("vendor", entry["vendor"]),
            ("model", entry["model"]),
            ("os_version", entry["os_version"]),
            ("serial_number", entry["serial"]),
            ("managed_ip", entry["managed_ip"]),
        ):
            if val is not None:
                sets.append(f"{col} = :{col}")
                params[col] = val

        # The controller names its children; a rename there follows here.
        # Direct devices keep their operator-chosen hostname.
        if is_child and entry["hostname"] and entry["hostname"] != target["hostname"]:
            sets.append("hostname = :hostname")
            params["hostname"] = entry["hostname"]

        old_status = target["status"]
        new_status = entry["status"]
        if is_child and new_status and new_status != old_status and old_status != "maintenance":
            sets.append("status = :status")
            params["status"] = new_status
            if new_status == "up":
                sets.append("last_seen = :seen_at2")
                params["seen_at2"] = entry["seen_at"]
            if new_status in _ALERTABLE or (new_status == "up" and old_status in _ALERTABLE):
                summary["transitions"].append({
                    "device_id": str(target["id"]),
                    "hostname": params.get("hostname", target["hostname"]),
                    "ip_address": entry["managed_ip"] or "",
                    "old_status": old_status,
                    "new_status": new_status,
                    "device_type": entry["device_type"],
                })

        await db.execute(
            text(f"UPDATE devices SET {', '.join(sets)} WHERE id = :id"),
            params,
        )
        summary["updated"] += 1

    # Parent→child dependency edges make the existing suppression logic
    # collapse a controller outage into a single root-cause alert.
    await db.execute(
        text("""
            INSERT INTO topology_dependencies
                (parent_device_id, child_device_id, dependency_type,
                 suppress_alerts, enabled, notes)
            SELECT :cid, d.id, 'controller', TRUE, TRUE, 'auto: managed-device sync'
            FROM devices d
            WHERE d.managed_by_device_id = :cid AND d.poll_mode = 'via_controller'
            ON CONFLICT (parent_device_id, child_device_id, dependency_type) DO NOTHING
        """),
        {"cid": str(ctrl["id"])},
    )
    return summary


async def sync_all_controllers(db: AsyncSession, only_controller: UUID | None = None) -> dict:
    """Sync children for every promoted controller. Writes, no commit —
    callers own the transaction (the sweeper commits, tests roll back)."""
    where = "d.promote_managed = TRUE"
    params: dict = {}
    if only_controller is not None:
        where += " AND d.id = :only"
        params["only"] = str(only_controller)

    controllers = (await db.execute(
        text(f"""
            SELECT d.id, d.hostname, d.group_id, p.oid_groups
            FROM devices d
            JOIN device_profiles p ON p.id = d.profile_id
            WHERE {where}
        """),
        params,
    )).mappings().all()

    summary = {"controllers": 0, "created": 0, "updated": 0, "reported": 0,
               "stale": 0, "transitions": []}
    for ctrl in controllers:
        try:
            one = await _sync_controller(db, ctrl)
        except Exception:
            logger.exception("managed-device sync failed for controller %s", ctrl["hostname"])
            continue
        summary["controllers"] += 1
        for k in ("created", "updated", "reported"):
            summary[k] += one[k]
        summary["transitions"].extend(one["transitions"])

    # Children the controller stopped reporting (or whose controller stopped
    # being synced) decay to 'unknown' — never deleted, history stays.
    if only_controller is None:
        stale = (await db.execute(
            text("""
                UPDATE devices SET status = 'unknown', updated_at = NOW()
                WHERE poll_mode = 'via_controller'
                  AND status NOT IN ('unknown', 'maintenance')
                  AND (managed_last_seen IS NULL
                       OR managed_last_seen < NOW() - make_interval(mins => :grace))
                RETURNING id
            """),
            {"grace": STALE_AFTER_MINUTES},
        )).all()
        summary["stale"] = len(stale)

    return summary


async def _fire_transitions(transitions: list[dict]) -> None:
    """Feed observed child status changes through the same alert pipeline the
    poller uses, one fresh session per event (the engine commits per event)."""
    if not transitions:
        return
    from app.api.v1.alert_engine import StatusChangeEvent, evaluate_status_change

    for tr in transitions:
        try:
            async with AsyncSessionLocal() as db:
                await evaluate_status_change(StatusChangeEvent(**tr), db)
        except Exception:
            logger.exception("managed-device alert dispatch failed for %s", tr.get("hostname"))


async def run_child_sync_once(db: AsyncSession) -> dict | None:
    """One locked sync pass. Returns None when another worker holds the lock.

    Transaction-scoped advisory lock for the same reason as udt_sweeper: the
    commit that ends this pass releases it on whichever pooled connection ran
    it, so nothing can leak."""
    got = (await db.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": CHILD_SYNC_ADVISORY_LOCK},
    )).scalar()
    if not got:
        return None
    summary = await sync_all_controllers(db)
    await db.commit()
    return summary


async def sync_controller_now(controller_id: UUID) -> dict:
    """Immediate single-controller sync (used when promote_managed turns on,
    so the UI reflects children without waiting for the sweeper)."""
    async with AsyncSessionLocal() as db:
        summary = await sync_all_controllers(db, only_controller=controller_id)
        await db.commit()
    await _fire_transitions(summary["transitions"])
    return summary


async def managed_sync_loop() -> None:
    await asyncio.sleep(25)
    logger.info("managed-device sync started (interval %ss)", SYNC_INTERVAL_S)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                summary = await run_child_sync_once(db)
            if summary:
                if summary["created"] or summary["stale"] or summary["transitions"]:
                    logger.info("managed-device sync: %s", {
                        k: v for k, v in summary.items() if k != "transitions"})
                await _fire_transitions(summary["transitions"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("managed-device sync pass failed")
        await asyncio.sleep(SYNC_INTERVAL_S)
