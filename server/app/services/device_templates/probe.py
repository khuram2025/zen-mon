"""Test a template against one device before publishing it.

Runs the same requests the poller would (a GET per scalar metric, a walk per
table column, ``max_rows`` capped) from the API host, stores nothing and
changes nothing. Only devices polled by this appliance can be tested: a device
polled through a remote sensor is not reachable from here, and a
controller-managed child is read through its controller.

One test per device at a time and at most two at once per API worker.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PREVIEW_ROWS = 25
MAX_TABLE_ROWS = 200
MAX_OIDS = 400
_global = asyncio.Semaphore(2)
_busy: set[str] = set()


def _coerce(metric: dict, raw: Optional[str]):
    if raw is None:
        return None
    value = raw.strip()
    mtype = metric.get("type") or "gauge"
    vmap = metric.get("value_map") or {}
    if value in vmap:
        return vmap[value]
    if mtype in ("gauge", "counter", "enum"):
        try:
            num = float(value.split()[0])
        except (ValueError, IndexError):
            return value
        if metric.get("scale") not in (None, 0):
            num = num * float(metric["scale"])
        return int(num) if num.is_integer() else round(num, 4)
    return value


def _label(metric: dict, value) -> Optional[str]:
    labels = metric.get("labels") or {}
    if value is None:
        return None
    key = str(int(value)) if isinstance(value, (int, float)) and float(value).is_integer() else str(value)
    entry = labels.get(key)
    return entry.get("text") if isinstance(entry, dict) else None


async def run(db: AsyncSession, device_id, groups: list[dict]) -> dict:
    from app.api.v1.devices import _device_snmp_settings
    from app.api.v1.snmp import snmp_auth_args, snmp_get_map, snmp_walk
    from app.services import device_service

    device = await device_service.get_device(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.managed_by_device_id is not None or getattr(device, "poll_mode", "direct") == "via_controller":
        raise HTTPException(status_code=409, detail="Controller-managed devices are read through their controller's "
                                                    "template and cannot be tested directly")
    if not device.snmp_enabled:
        raise HTTPException(status_code=409, detail="SNMP is disabled for this device")
    central = (await db.execute(text("""
        SELECT EXISTS (SELECT 1 FROM device_monitoring_vantages v WHERE v.device_id = :id AND v.poller_id = 'central')
    """), {"id": device_id})).scalar()
    if not central:
        raise HTTPException(status_code=409, detail="This device is polled through a remote sensor; a test from the "
                                                    "appliance would not reach it the way the poller does")
    total_oids = sum(len(g.get("metrics") or []) for g in groups)
    if total_oids > MAX_OIDS:
        raise HTTPException(status_code=422, detail=f"Test at most {MAX_OIDS} metrics at once; pick fewer groups")
    key = str(device_id)
    if key in _busy:
        raise HTTPException(status_code=429, detail="A template test is already running for this device")

    cfg = await _device_snmp_settings(db, device)
    auth = ["-v", cfg["version"], *snmp_auth_args(
        cfg["version"], cfg["community"], cfg["v3_username"], cfg["v3_security_level"], cfg["v3_auth_protocol"],
        cfg["v3_auth_passphrase"], cfg["v3_priv_protocol"], cfg["v3_priv_passphrase"], cfg["v3_context"])]
    ip, port = str(device.ip_address), int(cfg["port"] or 161)
    timeout_ms = max(3000, int(cfg["timeout_ms"] or 2000) * 2)

    _busy.add(key)
    started = time.monotonic()
    try:
        async with _global:
            results = []
            for g in groups:
                results.append(await _group(g, ip, port, timeout_ms, auth, snmp_get_map, snmp_walk))
    finally:
        _busy.discard(key)
    answered = sum(1 for g in results for m in g["metrics"] if m["ok"])
    defined = sum(len(g["metrics"]) for g in results)
    return {"device": {"id": key, "hostname": device.hostname, "ip": ip},
            "duration_ms": int((time.monotonic() - started) * 1000),
            "answered": answered, "defined": defined,
            "coverage_pct": round(100.0 * answered / defined, 1) if defined else None,
            "groups": results}


async def _group(g: dict, ip, port, timeout_ms, auth, get_map, walk) -> dict:
    t0 = time.monotonic()
    metrics = g.get("metrics") or []
    out = {"key": g.get("key"), "name": g.get("name"), "kind": g.get("kind") or "scalar",
           "error": None, "rows": None, "truncated": False, "metrics": []}
    if (g.get("kind") or "scalar") != "table":
        values: dict = {}
        for i in range(0, len(metrics), 16):
            chunk = [m.get("oid", "").lstrip(".") for m in metrics[i:i + 16]]
            got, err = await get_map(ip, port, timeout_ms, chunk, auth)
            if err:
                out["error"] = err
                break
            values.update(got)
        for m in metrics:
            raw = values.get(m.get("oid", "").lstrip("."))
            val = _coerce(m, raw)
            out["metrics"].append({"key": m.get("key"), "name": m.get("name"), "oid": m.get("oid"),
                                   "ok": raw is not None, "value": val, "raw": raw, "label": _label(m, val),
                                   "unit": m.get("unit"),
                                   "error": None if raw is not None or out["error"] else "not implemented by the agent"})
    else:
        max_rows = min(int((g.get("table") or {}).get("max_rows") or 500), MAX_TABLE_ROWS)
        labels: dict[str, str] = {}
        label_oid = (g.get("table") or {}).get("label_oid")
        if label_oid:
            rows, err, _ = await walk(ip, port, timeout_ms, label_oid.lstrip("."), auth, max_rows)
            labels = dict(rows) if not err else {}
        indexes: set[str] = set(labels)
        for m in metrics:
            rows, err, truncated = await walk(ip, port, timeout_ms, m.get("oid", "").lstrip("."), auth, max_rows)
            out["truncated"] = out["truncated"] or truncated
            indexes.update(i for i, _ in rows)
            sample = [{"index": i, "label": labels.get(i), "value": _coerce(m, v)} for i, v in rows[:PREVIEW_ROWS]]
            out["metrics"].append({"key": m.get("key"), "name": m.get("name"), "oid": m.get("oid"),
                                   "ok": bool(rows), "rows": len(rows), "sample": sample, "unit": m.get("unit"),
                                   "error": err or (None if rows else "no rows returned")})
        out["rows"] = len(indexes)
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return out
