"""Bounded Windows event details, stable identities and lifecycle evidence.

Raw events are replaceable by identity, so retries and overlapping policies
cannot inflate grouped counts. Only a few lifecycle events enter PostgreSQL;
the high-volume stream stays in ClickHouse and shares its group-commit writer.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

LEVELS = {"critical", "error", "warning", "information", "verbose", "audit"}
CHANNEL_NAMES = {c.casefold():c for c in ("System", "Application", "Security", "Directory Service", "DNS Server", "DFS Replication")}
EVENT_COLUMNS = ["server_id", "agent_id", "timestamp", "received_at", "event_uid", "group_key",
                 "log_name", "provider", "event_id", "record_id", "level", "component", "message",
                 "fields", "message_truncated"]


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("event time is required")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def normalize(row: dict) -> dict:
    stamp = utc(row.get("event_time"))
    if stamp < datetime(2000, 1, 1, tzinfo=timezone.utc) or stamp > datetime.now(timezone.utc) + timedelta(days=1):
        raise ValueError("event time is outside the supported range")
    record = int(row.get("record_id") or 0)
    event_id = int(row.get("event_id") or 0)
    if not 0 < record < 2**64 or not 0 <= event_id < 2**31:
        raise ValueError("event identifiers are invalid")
    channel = str(row.get("log_name") or "")[:128]
    channel = CHANNEL_NAMES.get(channel.casefold(),channel.casefold())
    provider = str(row.get("provider") or "")[:256]
    if not channel or not provider:
        raise ValueError("event channel and provider are required")
    fields = row.get("fields") or {}
    fields = {str(k)[:64]: str(v)[:256] for k, v in list(fields.items())[:24]} if isinstance(fields, dict) else {}
    component_keys = ("ServiceName", "serviceName", "DeviceName", "Device", "VolumeName", "AppName",
                      "ApplicationName", "TargetUserName", "ObjectName", "DriverName", "param1")
    component = next((fields[k] for k in component_keys if fields.get(k)), "")[:256]
    if "TargetUserName" in fields:
        component = "/".join(fields.get(k, "") for k in ("TargetDomainName", "TargetUserName", "IpAddress"))[:256]
    message = str(row.get("message") or "")[:8192]
    # Never replace object identity with a generic event ID. With no known
    # component, identical messages group; different objects/messages stay apart.
    identity = json.dumps([channel, provider, event_id, row.get("level"), component or message or fields], sort_keys=True, ensure_ascii=False)
    uid = hashlib.sha256(f"{channel}\0{record}\0{stamp.isoformat()}".encode()).hexdigest()
    return {"timestamp": stamp, "event_uid": uid, "group_key": hashlib.sha256(identity.encode()).hexdigest(),
            "log_name": channel, "provider": provider, "event_id": event_id, "record_id": record,
            "level": row.get("level") if row.get("level") in LEVELS else "information",
            "component": component, "message": message, "fields": fields,
            "message_truncated": bool(row.get("message_truncated")) or len(str(row.get("message") or "")) > 8192}


def insert_details(client, agent_id: str, server_id: str, rows: list[dict]) -> int:
    if len(rows) > 1000:
        raise ValueError("at most 1000 event details per envelope")
    received = datetime.now(timezone.utc)
    events = {e["event_uid"]: e for e in (normalize(r) for r in rows)}
    data = [[server_id, agent_id, e["timestamp"], received, e["event_uid"], e["group_key"],
             e["log_name"], e["provider"], e["event_id"], e["record_id"], e["level"], e["component"],
             e["message"], json.dumps(e["fields"], ensure_ascii=False), int(e["message_truncated"])] for e in events.values()]
    if data:
        client.insert("host_event_details", data, column_names=EVENT_COLUMNS)
    return len(rows)


def lifecycle_kind(e: dict) -> str | None:
    if e["log_name"].lower() != "system":
        return None
    provider, eid = e["provider"].lower(), e["event_id"]
    if provider == "microsoft-windows-kernel-general" and eid == 12:
        return "restart"
    if provider == "microsoft-windows-kernel-general" and eid == 13:
        return "shutdown"
    if (provider == "microsoft-windows-kernel-power" and eid == 41) or (provider == "eventlog" and eid == 6008):
        return "unexpected_restart"
    if provider == "user32" and eid == 1074:
        action = e["fields"].get("param5", "").lower()
        return "restart_requested" if action in {"restart", "reboot"} else "shutdown_requested"
    return None


async def record_lifecycle(db, server_id: str, uid: str, kind: str, event_time: datetime, detail: dict):
    await db.execute(text("""INSERT INTO server_lifecycle_events (server_id,event_uid,kind,event_time,detail)
        VALUES (:sid,:uid,:kind,:stamp,CAST(:detail AS jsonb)) ON CONFLICT (server_id,event_uid) DO NOTHING"""),
        {"sid": server_id, "uid": uid, "kind": kind, "stamp": event_time, "detail": json.dumps(detail, default=str)})


async def ingest_state(db, server_id: str, agent_id: str, rows: list[dict], inventory: dict | None):
    for row in rows:
        e = normalize(row)
        kind = lifecycle_kind(e)
        if kind:
            await record_lifecycle(db, server_id, e["event_uid"], kind, e["timestamp"],
                                   {k: e[k] for k in ("provider", "event_id", "message", "fields")})
    status = (inventory or {}).get("event_collection")
    if isinstance(status, dict):
        queries = status.get("queries") or []
        if not isinstance(queries, list) or len(queries) > 256:
            raise ValueError("invalid event collection status")
        clean = {"queries": [{k: q[k] for k in ("channel", "query", "backlog", "gap", "collected", "error", "last_event_at") if k in q}
                             for q in queries if isinstance(q, dict)],
                 **{k: status[k] for k in ("bytes", "events", "max_events", "max_bytes", "interval_s") if k in status}}
        encoded = json.dumps(clean)
        if len(encoded) > 131072:
            raise ValueError("event collection status too large")
        await db.execute(text("""INSERT INTO server_event_collection (server_id,agent_id,checked_at,status,gap_at)
            VALUES (:sid,:aid,:stamp,CAST(:status AS jsonb),CASE WHEN :gap THEN NOW() END)
            ON CONFLICT (server_id) DO UPDATE SET agent_id=EXCLUDED.agent_id,
                checked_at=EXCLUDED.checked_at,received_at=NOW(),status=EXCLUDED.status,
                gap_at=COALESCE(EXCLUDED.gap_at,server_event_collection.gap_at)
            WHERE server_event_collection.checked_at <= EXCLUDED.checked_at"""),
            {"sid": server_id, "aid": agent_id, "stamp": utc(status.get("checked_at")),
             "status": encoded, "gap": any(q.get("gap") for q in clean["queries"])})


async def recent_lifecycle(db, now: float) -> dict:
    # One fleet query per pass, not one query per server/rule. Old backlog is
    # searchable history, but never pages operators when collection resumes.
    rows = (await db.execute(text("""SELECT server_id::text,kind,max(event_time),count(*)
        FROM server_lifecycle_events
        WHERE event_time >= :since AND event_time <= :until AND maintenance_suppressed=FALSE
        GROUP BY server_id,kind"""),
        {"since": datetime.fromtimestamp(now-900,timezone.utc), "until": datetime.fromtimestamp(now+60,timezone.utc)})).all()
    result = {}
    for sid,kind,stamp,count in rows:
        result.setdefault(sid,[]).append((kind,stamp,count))
    await db.execute(text("""DELETE FROM server_lifecycle_events WHERE (server_id,event_uid) IN
        (SELECT server_id,event_uid FROM server_lifecycle_events WHERE observed_at<NOW()-INTERVAL '90 days' LIMIT 1000)"""))
    return result


def lifecycle_value(metric: str, events: list) -> tuple:
    wanted = {"restart", "unexpected_restart", "restart_requested"} if metric == "host_restart" else {"shutdown", "shutdown_requested"}
    selected = [(k,t,n) for k,t,n in events if k in wanted]
    if not selected:
        return False, 0.0, "no recent lifecycle event"
    latest = max(selected,key=lambda x:x[1])
    descriptions = {"restart":"OS boot detected", "unexpected_restart":"unexpected shutdown/restart detected",
                    "restart_requested":"restart requested", "shutdown":"OS shutdown reported", "shutdown_requested":"shutdown or restart requested"}
    return True, float(sum(n for _,_,n in selected)), f"{descriptions[latest[0]]} at {latest[1].isoformat()}"


QUERY_SETTINGS = {"max_execution_time": 8, "max_memory_usage": 268435456}


def query_groups(client, sid: str, start: datetime, end: datetime, *, level: str | None,
                 channel: str | None, search: str, limit: int, offset: int) -> dict:
    where = ["server_id=%(sid)s", "timestamp >= %(start)s", "timestamp < %(end)s"]
    params = {"sid": sid, "start": start, "end": end, "limit": limit+1, "offset": offset}
    if level:
        where.append("level=%(level)s"); params["level"] = level
    if channel:
        where.append("log_name=%(channel)s"); params["channel"] = channel
    if search:
        where.append("(positionCaseInsensitiveUTF8(concat(provider,' ',component,' ',message,' ',toString(event_id)),%(search)s)>0)")
        params["search"] = search
    predicate = " AND ".join(where)
    rows = client.query(f"""SELECT group_key,log_name,provider,event_id,level,component,count(),
        min(timestamp),max(timestamp),argMax(message,tuple(timestamp,received_at)),max(message_truncated)
        FROM zenplus.host_event_details FINAL WHERE {predicate}
        GROUP BY group_key,log_name,provider,event_id,level,component
        ORDER BY max(timestamp) DESC,group_key LIMIT %(limit)s OFFSET %(offset)s""",
        parameters=params, settings=QUERY_SETTINGS).result_rows
    totals = client.query(f"""SELECT level,count(),uniqExact(group_key),groupUniqArray(log_name)
        FROM zenplus.host_event_details FINAL WHERE {predicate} GROUP BY level""",
        parameters=params,settings=QUERY_SETTINGS).result_rows
    keys = ("group_key", "log_name", "provider", "event_id", "level", "component", "count",
            "first_seen", "last_seen", "message", "message_truncated")
    return {"items": [dict(zip(keys,r)) for r in rows[:limit]], "has_more": len(rows)>limit,
            "total_events": sum(r[1] for r in totals), "total_groups": sum(r[2] for r in totals),
            "levels": {r[0]:r[1] for r in totals}, "channels": sorted({c for r in totals for c in r[3]}),
            "offset": offset, "limit": limit, "retention_days": 14}


def query_occurrences(client, sid: str, group: str, start: datetime, end: datetime, limit: int, offset: int):
    rows = client.query("""SELECT timestamp,record_id,event_id,provider,level,component,message,fields,message_truncated,event_uid
        FROM zenplus.host_event_details FINAL
        WHERE server_id=%(sid)s AND group_key=%(group)s AND timestamp >= %(start)s AND timestamp < %(end)s
        ORDER BY timestamp DESC,event_uid LIMIT %(limit)s OFFSET %(offset)s""",
        parameters={"sid":sid,"group":group,"start":start,"end":end,"limit":limit+1,"offset":offset},
        settings=QUERY_SETTINGS).result_rows
    keys=("timestamp","record_id","event_id","provider","level","component","message","fields","message_truncated","event_uid")
    items=[dict(zip(keys,r)) for r in rows[:limit]]
    for item in items:
        item["fields"]=json.loads(item["fields"])
    return {"items":items,"has_more":len(rows)>limit,"offset":offset,"limit":limit}
