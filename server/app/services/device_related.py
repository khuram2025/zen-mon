"""The device page's Related panel: what this device depends on, what depends
on it, what it is drawn next to, and which devices changed status at the same
time.

* **Managed by / manages**: the controller that reports this device
  (``managed_by_device_id``) and the devices it reports.
* **Dependencies**: configured ``topology_dependencies`` upstream and
  downstream (the ones alert suppression already uses).
* **On maps**: the network maps that show this device, with the devices
  linked to it there (no LLDP/CDP neighbour table is collected, so map links
  are the neighbour source). Only for callers with ``maps.view``.
* **Same time**: devices whose *confirmed* status changed within ±2 minutes
  of one of this device's last confirmed transitions, from
  ``device_status_log``. It reads the log and never writes it.

Every listed device is filtered by the caller's tag scope.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

SAME_TIME_WINDOW_S = 120
OWN_TRANSITIONS = 5
LOOKBACK_DAYS = 30
MAX_SAME_TIME = 20
MAX_LISTED = 20

_STATUS_RANK = {"down": 0, "degraded": 1, "unknown": 2, "maintenance": 3, "up": 4}


def _ms(ts) -> int:
    if isinstance(ts, (int, float)):
        return int(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def same_time_matches(own: list[dict], others: Iterable[dict], window_s: int = SAME_TIME_WINDOW_S) -> list[dict]:
    """Group other devices' transitions that fall within ``window_s`` of one of
    ``own``'s. ``own`` and ``others`` carry ``ts`` (epoch ms), ``old_status``
    and ``new_status``; ``others`` also ``device_id``.

    Returns one entry per device: the matching transitions with their offset
    from the nearest own transition (seconds, negative = before), most
    matches first and then the closest."""
    if not own:
        return []
    window_ms = window_s * 1000
    by_device: dict[str, list[dict]] = {}
    for o in others:
        best = None
        for t in own:
            delta = o["ts"] - t["ts"]
            if abs(delta) <= window_ms and (best is None or abs(delta) < abs(best[0])):
                best = (delta, t)
        if best is None:
            continue
        delta, anchor = best
        by_device.setdefault(str(o["device_id"]), []).append({
            "at": _iso_ms(o["ts"]), "old_status": o["old_status"], "new_status": o["new_status"],
            "offset_s": round(delta / 1000),
            "anchor": {"at": _iso_ms(anchor["ts"]), "old_status": anchor["old_status"],
                       "new_status": anchor["new_status"]},
        })
    out = []
    for device_id, matches in by_device.items():
        matches.sort(key=lambda m: m["at"], reverse=True)
        anchors = {m["anchor"]["at"] for m in matches}
        out.append({"device_id": device_id, "matches": matches, "shared_transitions": len(anchors),
                    "closest_s": min(abs(m["offset_s"]) for m in matches)})
    out.sort(key=lambda d: (-d["shared_transitions"], d["closest_s"]))
    return out


def _ch_rows(sql: str, params: dict) -> list[tuple]:
    from app.core.database import get_clickhouse_client
    return get_clickhouse_client().query(sql, parameters=params).result_rows


def own_transitions(device_id: str, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rows = _ch_rows("""
        SELECT toUnixTimestamp64Milli(timestamp), old_status, new_status
        FROM zenplus.device_status_log
        WHERE device_id = %(id)s AND timestamp >= %(since)s
        ORDER BY timestamp DESC
        LIMIT %(limit)s
    """, {"id": device_id, "since": (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S"),
          "limit": OWN_TRANSITIONS})
    return [{"ts": int(r[0]), "old_status": r[1], "new_status": r[2]} for r in rows]


def transitions_near(device_id: str, own: list[dict], window_s: int = SAME_TIME_WINDOW_S) -> list[dict]:
    if not own:
        return []
    clauses, params = [], {"id": device_id}
    fmt = "%Y-%m-%d %H:%M:%S"
    for i, t in enumerate(own):
        at = datetime.fromtimestamp(t["ts"] / 1000, tz=timezone.utc)
        params[f"lo{i}"] = (at - timedelta(seconds=window_s + 1)).strftime(fmt)
        params[f"hi{i}"] = (at + timedelta(seconds=window_s + 1)).strftime(fmt)
        clauses.append(f"(timestamp >= %(lo{i})s AND timestamp <= %(hi{i})s)")
    rows = _ch_rows(f"""
        SELECT toString(device_id), toUnixTimestamp64Milli(timestamp), old_status, new_status
        FROM zenplus.device_status_log
        WHERE device_id != %(id)s AND ({' OR '.join(clauses)})
        ORDER BY timestamp
        LIMIT 2000
    """, params)
    return [{"device_id": r[0], "ts": int(r[1]), "old_status": r[2], "new_status": r[3]} for r in rows]


def device_brief(row) -> dict[str, Any]:
    return {"id": str(row["id"]), "hostname": row["hostname"],
            "ip_address": str(row["ip_address"]) if row["ip_address"] else None,
            "device_type": row["device_type"], "status": row["status"]}


def by_status_then_name(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda d: (_STATUS_RANK.get(d.get("status") or "", 5), (d.get("hostname") or "").lower()))
