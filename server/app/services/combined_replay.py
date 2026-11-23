"""Replay a combined rule over stored history: "Test against the last 7 days"
(multi-condition rules phase 3, docs/multi-condition-rules.md).

The draft tree is judged by the real engine (``rule_conditions.judge`` and
``step``, with the rule's breach hold, clear hold and per-condition holds) at
every 5-minute bucket of the window, for each subject the rule would cover
(at most ``MAX_SUBJECTS``). Readings come from the stored history, bucket
averages for levels and the confirmed status logs for states:

* servers: CPU, memory, filesystem (per mount);
* devices: CPU, memory (SNMP), down and degraded (status log);
* service checks: response time (5-minute rollup), down and degraded
  (status log).

Any other condition has no replayable history and reads as unknown; the
result names those conditions, since an AND with an unknown condition can
never be true. Nothing is written: this only reads.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import rule_conditions as rc

logger = logging.getLogger("zenplus.combined_replay")

STEP_S = 300
DEFAULT_DAYS = 7
MAX_DAYS = 14
MAX_SUBJECTS = 200
MAX_EPISODES = 100
LEVELS = {
    "host_cpu_pct": ("host_cpu_metrics", "server_id", "''", "cpu_total_pct"),
    "host_memory_pct": ("host_memory_metrics", "server_id", "''", "used_pct"),
    "host_filesystem_pct": ("host_filesystem_metrics", "server_id", "mount", "used_pct"),
}
STATES = {"device_down": "down", "device_degraded": "degraded", "service_down": "down",
          "service_degraded": "degraded"}
REPLAYABLE = set(LEVELS) | {"cpu", "memory", "service_response_ms"} | set(STATES)


@dataclass
class Series:
    """Bucketed levels {asset: {dimension: {bucket: value}}} or status
    transitions {asset: [(epoch, status)]} for one metric."""
    levels: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)


def _bucket(t: float) -> int:
    return int(t // STEP_S * STEP_S)


def _query(sql: str, params: dict) -> list:
    from app.core.database import get_clickhouse_client
    return get_clickhouse_client().query(sql, parameters=params).result_rows


def _load_levels(metric: str, ids: list[str], since: datetime, until: datetime) -> dict:
    if metric in LEVELS:
        table, id_col, dim, col = LEVELS[metric]
        extra = ""
        if metric == "host_filesystem_pct":
            from app.services.filesystem_monitoring import ch_capacity_filter
            extra = f" AND {ch_capacity_filter()}"
        sql = (f"SELECT toString({id_col}), {dim}, toUnixTimestamp(toStartOfFiveMinutes(timestamp)) AS b, avg({col}) "
               f"FROM zenplus.{table} WHERE timestamp >= %(s)s AND timestamp < %(u)s "
               f"AND toString({id_col}) IN %(ids)s{extra} GROUP BY {id_col}, {dim}, b")
    elif metric in ("cpu", "memory"):
        sql = ("SELECT toString(device_id), '', toUnixTimestamp(toStartOfFiveMinutes(timestamp)) AS b, avg(value) "
               "FROM zenplus.snmp_metrics WHERE timestamp >= %(s)s AND timestamp < %(u)s AND metric_key = %(k)s "
               "AND toString(device_id) IN %(ids)s GROUP BY device_id, b")
    else:   # service_response_ms
        sql = ("SELECT toString(service_check_id), '', toUnixTimestamp(timestamp) AS b, "
               "sum(avg_response_ms * sample_count) / nullIf(sum(sample_count), 0) "
               "FROM zenplus.service_metrics_5m WHERE timestamp >= %(s)s AND timestamp < %(u)s "
               "AND toString(service_check_id) IN %(ids)s GROUP BY service_check_id, b")
    out: dict = {}
    for asset, dim, b, value in _query(sql, {"s": since, "u": until, "ids": ids, "k": metric}):
        if value is not None:
            out.setdefault(str(asset), {}).setdefault(str(dim or ""), {})[int(b)] = float(value)
    return out


def _load_states(metric: str, ids: list[str], since: datetime, until: datetime) -> dict:
    table, col = (("device_status_log", "device_id") if metric.startswith("device_")
                  else ("service_status_log", "service_check_id"))
    params = {"s": since, "u": until, "ids": ids}
    prior = _query(f"SELECT toString({col}), toUnixTimestamp64Milli(timestamp) / 1000, new_status "
                   f"FROM zenplus.{table} WHERE timestamp < %(s)s AND toString({col}) IN %(ids)s "
                   f"ORDER BY {col}, timestamp DESC LIMIT 1 BY {col}", params)
    rows = _query(f"SELECT toString({col}), toUnixTimestamp64Milli(timestamp) / 1000, new_status "
                  f"FROM zenplus.{table} WHERE timestamp >= %(s)s AND timestamp < %(u)s "
                  f"AND toString({col}) IN %(ids)s ORDER BY {col}, timestamp", params)
    out: dict = {}
    for asset, ts, status in [*prior, *rows]:
        out.setdefault(str(asset), []).append((float(ts), str(status or "").lower()))
    for trans in out.values():
        trans.sort()
    return out


def _state_at(transitions: list, t: float, bad: str) -> Optional[float]:
    status = None
    for ts, st in transitions:
        if ts > t:
            break
        status = st
    if status is None or status == "unknown":
        return None
    return 1.0 if status == bad else 0.0


@dataclass
class Target:
    key: str
    name: str
    ids: dict            # {"self": id, "device": id, "server": id}


async def _targets(db: AsyncSession, rule, subject: str) -> list[Target]:
    """The subjects the draft rule covers, with their linked assets."""
    from app.services import combined_readers as cr
    from app.services import combined_rule_service as crs
    if subject == "server":
        from app.services import host_alert_service as has
        from app.services import alert_rule_exclusions as rx
        servers = await has._servers(db)
        ids = [str(rule.server_id)] if getattr(rule, "server_id", None) else list(servers)
        if not getattr(rule, "server_id", None) and (getattr(rule, "scope_tag", None) or getattr(rule, "group_id", None)):
            rows = await has._server_scope_rows(db)
            ids = [sid for sid in ids if rx.server_scope_covers(rule, rows.get(sid) or {"id": sid})]
        links = await cr.server_device_links(db)
        return [Target(f"server:{sid}", servers.get(sid, "server"), {"self": sid, "device": links.get(sid)})
                for sid in ids[:MAX_SUBJECTS]]
    if subject not in ("device", "service_check"):
        return []
    cache: dict = {}
    subjects = await crs._subjects(db, rule, cache)
    await crs._links(db, rule, subjects, cache)
    out = []
    for k, s in list(subjects.items())[:MAX_SUBJECTS]:
        own = s.device_id if s.kind == "device" else s.service_check_id
        out.append(Target(k, s.label, {"self": own, "device": s.link_device, "server": s.link_server}))
    return out


async def replay(db: AsyncSession, rule, tree: dict, subject: str, *, days: int = DEFAULT_DAYS,
                 now: Optional[float] = None) -> dict:
    """Episodes the draft rule would have raised over the last ``days``."""
    from app.services.network_conditions import clear_hold_seconds
    days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    until_b = _bucket(now)
    start_b = until_b - days * 86400
    since, until = (datetime.fromtimestamp(start_b, timezone.utc), datetime.fromtimestamp(until_b, timezone.utc))
    targets = await _targets(db, rule, subject)
    conditions = rc.leaves(tree)
    not_replayed = [c["id"] for c in conditions if c["metric"] not in REPLAYABLE]
    # One query per condition for every target at once.
    series: dict[str, Series] = {}
    for c in conditions:
        if c["metric"] not in REPLAYABLE:
            continue
        side = c.get("on") or "self"
        ids = sorted({t.ids.get(side) for t in targets if t.ids.get(side)})
        if not ids:
            series[c["id"]] = Series()
            continue
        try:
            if c["metric"] in STATES:
                series[c["id"]] = Series(states=await asyncio.to_thread(_load_states, c["metric"], ids, since, until))
            else:
                series[c["id"]] = Series(levels=await asyncio.to_thread(_load_levels, c["metric"], ids, since, until))
        except Exception:
            logger.exception("replay: history for %s unavailable", c["metric"])
            not_replayed.append(c["id"])
    hold_s = int(getattr(rule, "min_duration", 0) or 0)
    clear_hold_s = clear_hold_seconds(getattr(rule, "clear_duration", None))
    episodes: list[dict] = []
    total = 0
    affected = set()
    skipped = set(not_replayed)
    for tgt in targets:
        open_since: Optional[float] = None
        matched: list = []
        hold = rc.Step("none", None, None)
        leaf_since: dict = {}
        for t in range(start_b, until_b, STEP_S):
            readings = {}
            for c in conditions:
                asset = tgt.ids.get(c.get("on") or "self")
                s = series.get(c["id"])
                if s is None or c["id"] in skipped or not asset:
                    readings[c["id"]] = rc.LeafReading(known=False)
                elif c["metric"] in STATES:
                    trans = s.states.get(asset)
                    v = _state_at(trans, t + STEP_S, STATES[c["metric"]]) if trans else None
                    readings[c["id"]] = rc.LeafReading({"": v})
                else:
                    dims = s.levels.get(asset)
                    if not dims:
                        readings[c["id"]] = rc.LeafReading(known=False)
                        continue
                    target = (c.get("target") or "").strip()
                    if target:
                        dims = {target: dims.get(target, {})}
                    readings[c["id"]] = rc.LeafReading({d: b.get(t) for d, b in dims.items()})
            res = rc.judge(tree, readings, active=open_since is not None, leaf_since=leaf_since, now=float(t))
            leaf_since = res.leaf_since
            at = datetime.fromtimestamp(t, timezone.utc)
            hold = rc.step(res.state, open_incident=open_since is not None, breach_since=hold.breach_since,
                           clear_since=hold.clear_since, hold_s=hold_s, clear_hold_s=clear_hold_s, now=at)
            if hold.action == "raise":
                open_since, matched = float(t), rc.matched_metadata(tree, res)
                hold = rc.Step("none", None, None)
            elif hold.action == "resolve" and open_since is not None:
                total += 1
                affected.add(tgt.key)
                if len(episodes) < MAX_EPISODES:
                    episodes.append({"subject": tgt.key, "name": tgt.name, "start": open_since, "end": float(t),
                                     "matched": matched})
                open_since, hold = None, rc.Step("none", None, None)
        if open_since is not None:
            total += 1
            affected.add(tgt.key)
            if len(episodes) < MAX_EPISODES:
                episodes.append({"subject": tgt.key, "name": tgt.name, "start": open_since, "end": None,
                                 "matched": matched})
    episodes.sort(key=lambda e: e["start"], reverse=True)
    return {"from": start_b, "to": until_b, "step_s": STEP_S, "subjects": len(targets),
            "subjects_capped": len(targets) >= MAX_SUBJECTS, "episodes": episodes, "episode_count": total,
            "subjects_affected": len(affected), "not_replayed": sorted(skipped),
            "not_replayed_labels": [rc.leaf_label(c) for c in conditions if c["id"] in skipped],
            "replayable": subject in ("server", "device", "service_check")}
