"""Virtualisation alert rules (alerting plan G-11, migrate-201).

Pure parts (clear levels, readers, labels, sentences, scope) run without a
database; the evaluator lifecycle, the linked device's maintenance, the rule
API validator and the migration run on a disposable PostgreSQL (pg_fixture).
Notifications are captured by replacing host_alert_service.dispatch_to_channels.
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import host_alert_service
from app.services import virtualization_alert_service as vma

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
CHANNEL = "00000000-0000-0000-0000-00000000c0de"
CID = "11111111-1111-1111-1111-111111111111"


def host(rid="host-4", name="esx-04", connection="connected", maintenance=None):
    return {"id": f"host:{rid}", "external_id": rid, "kind": "host", "name": name, "metrics": {},
            "attributes": {"connection": connection, **({"maintenance": maintenance} if maintenance else {})}}


def store(rid="datastore-1", name="DS-01", pct=92.0):
    return {"id": f"storage:{rid}", "external_id": rid, "kind": "storage", "name": name,
            "metrics": {} if pct is None else {"storage_usage_percent": pct}, "attributes": {}}


def alarm(aid="alarm-7.host-4", title="Host CPU usage", severity="critical", resource="host:host-4", **kw):
    return {"id": aid, "title": title, "severity": severity, "resolved": False, "acknowledged": False,
            "resource_id": resource, **kw}


def connector(resources=(), alerts=(), *, last=T0, provider="vmware", name="vc-01", **kw):
    return {"id": CID, "name": name, "provider": provider, "endpoint": "https://vc-01.example.test",
            "tags": ["prod"], "enabled": True, "interval_seconds": 300, "last_success_at": last,
            "snapshot": {"resources": list(resources), "alerts": list(alerts)}, "linked": None, **kw}


def cond(metric="vm_datastore_pct", op=">", thr=85.0, reset=None):
    return {"metric": metric, "operator": op, "threshold": thr, "reset_threshold": reset}


# ── Conditions, readers, labels ─────────────────────────────────────────────

def test_clear_levels():
    assert vma.clear_level(cond()) == 83.0                     # 2 points on the healthy side
    assert vma.clear_level(cond(op="<", thr=10)) == 12.0
    assert vma.clear_level(cond(reset=80)) == 80.0
    assert vma.clear_level(cond("vm_host_disconnected", "==", 1)) == 1.0
    assert vma.clear_level(cond("vm_alarm", ">=", 2)) == 2.0
    assert vma.lifecycle_condition(cond())["reset_threshold"] == 83.0


def test_readers():
    assert vma.host_state(host()) == (0.0, "connected")
    assert vma.host_state(host(connection="disconnected")) == (1.0, "disconnected")
    assert vma.host_state(host(connection="notResponding")) == (1.0, "not_responding")
    assert vma.host_state({"attributes": {}}) == (None, "")     # Nutanix / Hyper-V: no connection state
    assert vma.datastore_pct(store(pct=91.25)) == 91.25
    assert vma.datastore_pct(store(pct=None)) is None
    assert vma.alarm_level(alarm()) == 2.0 and vma.alarm_level(alarm(severity="warning")) == 1.0
    assert vma.alarm_level(alarm(severity="healthy")) == 0.0
    assert vma.alarm_level(alarm(resolved=True)) == 0.0


def test_labels_and_freshness():
    c = connector()
    assert vma.source_label(c) == "vCenter vc-01"
    assert vma.source_label({**c, "name": "Prod vCenter"}) == "Prod vCenter"
    assert vma.source_label({**c, "provider": "nutanix_pc", "name": "pc-01"}) == "Prism Central pc-01"
    assert vma.resource_label(c, store()) == "Datastore DS-01"
    assert vma.resource_label({**c, "provider": "nutanix_pe"}, store()) == "Storage container DS-01"
    assert vma.resource_label(c, host()) == "ESXi host esx-04"
    assert vma.snapshot_current(c, T0 + timedelta(seconds=630))
    assert not vma.snapshot_current(c, T0 + timedelta(seconds=631))   # 2 polls + 30 s
    assert not vma.snapshot_current({**c, "enabled": False}, T0)
    assert vma.pct_text(92.0, 85) == "92%" and vma.pct_text(84.6, 85) == "84.6%"
    assert vma.pct_text(91.5, 85) == "92%" and vma.pct_text(88.4, 85) == "88.4%"
    assert vma.pct_text(84.96, 85) == "84.96%" and vma.limit_text(85) == "85%"


def test_sentences_from_the_brief():
    c = connector()
    [ds] = vma.readings_for(vma.DATASTORE_PCT, connector([store()]), T0, {})
    assert vma.sentence(vma.DATASTORE_PCT, ds, cond()) == "Datastore DS-01 on vCenter vc-01 is 92% full (limit 85%)"
    ds.value = 78.0
    assert vma.sentence(vma.DATASTORE_PCT, ds, cond(), recovery=True) == \
        "Datastore DS-01 on vCenter vc-01 is back to 78% full (limit 85%)"
    [h] = vma.readings_for(vma.HOST_DISCONNECTED, connector([host(connection="disconnected")]), T0, {})
    assert vma.sentence(vma.HOST_DISCONNECTED, h, cond("vm_host_disconnected", "==", 1)) == \
        "ESXi host esx-04 is disconnected from vCenter vc-01"
    assert vma.sentence(vma.HOST_DISCONNECTED, h, {}, recovery=True) == "ESXi host esx-04 is connected to vCenter vc-01 again"
    h.state = "not_responding"
    assert vma.sentence(vma.HOST_DISCONNECTED, h, {}) == "ESXi host esx-04 is not responding to vCenter vc-01"
    [a] = vma.readings_for(vma.ALARM, connector([host()], [alarm()]), T0, {})
    assert vma.sentence(vma.ALARM, a, cond("vm_alarm", ">=", 1)) == \
        'vCenter vc-01 raised the red alarm "Host CPU usage" on ESXi host esx-04'
    a.value = 0.0
    assert vma.sentence(vma.ALARM, a, {}, recovery=True) == \
        'The alarm "Host CPU usage" on ESXi host esx-04 cleared in vCenter vc-01'
    assert c["name"] == "vc-01"


def test_readings_missing_data_and_cleared_alarms():
    resources = [host(), host("host-5", "esx-05", "disconnected", maintenance="yes"), store()]
    c = connector(resources, [alarm()])
    hosts = vma.readings_for(vma.HOST_DISCONNECTED, c, T0, {})
    assert [r.dimension for r in hosts] == ["esx-04"]           # vCenter maintenance mode: not evaluated
    # A stale snapshot: every reading is missing.
    stale = vma.readings_for(vma.DATASTORE_PCT, c, T0 + timedelta(hours=1), {})
    assert [r.value for r in stale] == [None]
    # Open incidents whose dimension left a current snapshot.
    gone = {f"vc:{CID}:storage:datastore-9": {"resource": "Datastore DS-09", "dimension": "DS-09"},
            f"vc:{CID}:alarm:alarm-1": {"alarm": "Datastore usage", "resource": "Datastore DS-01"},
            "vc:other:alarm:x": {}}
    ds = vma.readings_for(vma.DATASTORE_PCT, c, T0, gone)
    assert {r.key: r.value for r in ds}[f"vc:{CID}:storage:datastore-9"] is None   # missing, never resolves
    alarms = {r.key: r.value for r in vma.readings_for(vma.ALARM, c, T0, gone)}
    assert alarms == {f"vc:{CID}:alarm:alarm-7.host-4": 2.0, f"vc:{CID}:alarm:alarm-1": 0.0}   # cleared
    # ... but not while the snapshot is stale.
    late = {r.key: r.value for r in vma.readings_for(vma.ALARM, c, T0 + timedelta(hours=1), gone)}
    assert late[f"vc:{CID}:alarm:alarm-1"] is None


def test_rule_scope():
    c = connector(linked={"id": "d1"})
    rule = NS(target=None, device_id=None, scope_tag=None)
    assert vma.rule_covers(rule, c)
    assert vma.rule_covers(NS(target=CID, device_id=None, scope_tag=None), c)
    assert not vma.rule_covers(NS(target="other", device_id=None, scope_tag=None), c)
    assert vma.rule_covers(NS(target=None, device_id="d1", scope_tag="PROD"), c)
    assert not vma.rule_covers(NS(target=None, device_id="d2", scope_tag=None), c)
    assert not vma.rule_covers(NS(target=None, device_id=None, scope_tag="lab"), c)


# ── Evaluator on PostgreSQL ─────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def dispatch(db, channel_ids, ctx):
        calls.append((list(channel_ids), ctx))
        return len(channel_ids)

    monkeypatch.setattr(host_alert_service, "dispatch_to_channels", dispatch)
    return calls


def add_connector(db, resources=(), alerts=(), *, last=T0, endpoint="https://10.20.0.5", name="vc-01",
                  tags=("prod",)):
    return str(db.scalar(
        "INSERT INTO virtualization_connectors (id, name, provider, endpoint, config, tags, interval_seconds, "
        "status, last_success_at, snapshot) VALUES (gen_random_uuid(), $1, 'vmware', $2, '{}'::jsonb, $3::jsonb, "
        "300, 'ok', $4, $5::jsonb) RETURNING id",
        name, endpoint, list(tags), last, {"resources": list(resources), "alerts": list(alerts)}))


def set_snapshot(db, cid, resources=(), alerts=(), last=T0):
    db.execute("UPDATE virtualization_connectors SET snapshot = $1::jsonb, last_success_at = $2 WHERE id = $3",
               {"resources": list(resources), "alerts": list(alerts)}, last, cid)


def add_rule(db, metric, op, thr, **cols):
    cols.setdefault("notify_channels", [CHANNEL])
    cols.setdefault("recovery_alert", True)
    names = "".join(f", {c}" for c in cols)
    vals = "".join(f", ${i + 5}" + ("::jsonb" if c in ("notify_channels", "conditions") else "")
                   for i, c in enumerate(cols))
    return str(db.scalar(
        f"INSERT INTO alert_rules (name, metric, operator, threshold, severity{names}) "
        f"VALUES ($1, $2, $3, $4, 'warning'{vals}) RETURNING id",
        f"{metric} rule", metric, op, thr, *cols.values()))


def alerts(db, rule):
    rows = db.query("SELECT status, message, metadata, device_id::text, priority "
                    "FROM alerts WHERE rule_id = $1 ORDER BY triggered_at", rule)
    return [(s, m, md if isinstance(md, dict) else json.loads(md), d, p) for s, m, md, d, p in rows]


async def run(db, now):
    async with session(db) as s:
        return await vma.evaluate_virtualization_rules(s, now=now)


@pytest.mark.asyncio
async def test_datastore_lifecycle_hold_raise_missing_recover(pg_db, sent):
    db = pg_db
    cid = add_connector(db, [store(pct=92.0)])
    rule = add_rule(db, "vm_datastore_pct", ">", 85, min_duration=600)

    assert (await run(db, T0))["raised"] == 0                       # breach hold starts
    assert db.scalar("SELECT breach_since FROM virtualization_alert_state WHERE rule_id = $1", rule) == T0
    assert (await run(db, T0 + timedelta(seconds=600)))["raised"] == 1
    [(status, message, meta, device, priority)] = alerts(db, rule)
    assert status == "active" and device is None and priority == "P3"
    assert message == "Datastore DS-01 on vCenter vc-01 is 92% full (limit 85%)."
    assert meta["virt_alert_key"] == f"vc:{cid}:storage:datastore-1" and meta["dimension"] == "DS-01"
    assert meta["notified"] is True and meta["clear_level"] == 83.0
    [(channels, ctx)] = sent
    assert channels == [CHANNEL] and ctx["delivery_event"] == "trigger" and ctx["alert_id"]
    assert ctx["event_family"] == "virtualization" and ctx["groupable"] is True
    assert ctx["body"] == message and "Datastore DS-01 on vCenter vc-01" in ctx["subject"]

    await run(db, T0 + timedelta(seconds=660))                      # still breaching: one incident, one send
    assert len(alerts(db, rule)) == 1 and len(sent) == 1

    # The connector stops polling: the snapshot goes stale, nothing resolves.
    set_snapshot(db, cid, [store(pct=50.0)], last=T0)
    await run(db, T0 + timedelta(hours=2))
    assert alerts(db, rule)[0][0] == "active" and alerts(db, rule)[0][2]["data_state"] == "missing"

    # 84 % is past the trigger but not the 83 % clear level: still open.
    t = T0 + timedelta(hours=3)
    set_snapshot(db, cid, [store(pct=84.0)], last=t)
    await run(db, t)
    assert alerts(db, rule)[0][0] == "active"
    set_snapshot(db, cid, [store(pct=78.0)], last=t)
    await run(db, t + timedelta(seconds=60))                        # clear hold starts (default 300 s)
    assert alerts(db, rule)[0][0] == "active"
    set_snapshot(db, cid, [store(pct=78.0)], last=t + timedelta(seconds=300))
    out = await run(db, t + timedelta(seconds=360))
    assert out["resolved"] == 1 and alerts(db, rule)[0][0] == "resolved"
    _, rctx = sent[-1]
    assert rctx["delivery_event"] == "recovery" and rctx["is_recovery"] is True
    assert rctx["body"].startswith("Datastore DS-01 on vCenter vc-01 is back to 78% full (limit 85%).")


@pytest.mark.asyncio
async def test_host_disconnected_linked_device_and_maintenance(pg_db, sent):
    db = pg_db
    dev = str(db.scalar("INSERT INTO devices (hostname, ip_address, status) VALUES ('vc-01', '10.20.0.5', 'up') "
                        "RETURNING id"))
    cid = add_connector(db, [host(connection="disconnected"), host("host-6", "esx-06")])
    rule = add_rule(db, "vm_host_disconnected", "==", 1, device_id=dev, clear_duration=0)
    other = add_rule(db, "vm_host_disconnected", "==", 1, scope_tag="lab")
    await run(db, T0)
    [(status, message, meta, device, _p)] = alerts(db, rule)
    assert message == "ESXi host esx-04 is disconnected from vCenter vc-01." and device == dev
    assert meta["connection"] == "disconnected" and alerts(db, other) == []
    assert sent[0][1]["hostname"] == "vc-01" and sent[0][1]["ip_address"] == "10.20.0.5"

    # The linked device in maintenance pauses the whole source: nothing resolves.
    db.execute("UPDATE devices SET status = 'maintenance' WHERE id = $1", dev)
    set_snapshot(db, cid, [host(), host("host-6", "esx-06")], last=T0 + timedelta(minutes=5))
    await run(db, T0 + timedelta(minutes=5))
    assert alerts(db, rule)[0][0] == "active"
    db.execute("UPDATE devices SET status = 'up' WHERE id = $1", dev)
    await run(db, T0 + timedelta(minutes=6))
    assert alerts(db, rule)[0][0] == "resolved"
    assert sent[-1][1]["body"].startswith("ESXi host esx-04 is connected to vCenter vc-01 again.")


@pytest.mark.asyncio
async def test_alarm_raises_per_alarm_and_clears_when_gone(pg_db, sent):
    db = pg_db
    cid = add_connector(db, [host(), store()], [alarm(), alarm("alarm-9.datastore-1", "Datastore usage on disk",
                                                                "warning", "storage:datastore-1")])
    red = add_rule(db, "vm_alarm", ">=", 2, target=cid, clear_duration=0)
    any_ = add_rule(db, "vm_alarm", ">=", 1, clear_duration=0)
    await run(db, T0)
    assert [m for _s, m, *_ in alerts(db, red)] == [
        'vCenter vc-01 raised the red alarm "Host CPU usage" on ESXi host esx-04.']
    assert sorted(m for _s, m, *_ in alerts(db, any_)) == [
        'vCenter vc-01 raised the red alarm "Host CPU usage" on ESXi host esx-04.',
        'vCenter vc-01 raised the yellow alarm "Datastore usage on disk" on Datastore DS-01.']
    # The red alarm clears in vCenter (it leaves triggeredAlarmState).
    set_snapshot(db, cid, [host(), store()], [alarm("alarm-9.datastore-1", "Datastore usage on disk",
                                                    "warning", "storage:datastore-1")],
                 last=T0 + timedelta(minutes=1))
    await run(db, T0 + timedelta(minutes=1))
    assert alerts(db, red)[0][0] == "resolved"
    assert {s for s, *_ in alerts(db, any_)} == {"resolved", "active"}
    assert sent[-1][1]["body"].startswith('The alarm "Host CPU usage" on ESXi host esx-04 cleared in vCenter vc-01.')


def test_migration_accepts_the_metrics_and_creates_state(pg_db):
    for metric in sorted(vma.METRICS):
        add_rule(pg_db, metric, ">=", 1)
    assert pg_db.scalar("SELECT count(*) FROM alert_rules WHERE metric = ANY($1::text[])",
                        sorted(vma.METRICS)) == 3
    # Earlier widenings survive.
    add_rule(pg_db, "service_tls_days", "<", 30)
    assert pg_db.scalar("SELECT has_table_privilege('zenplus', 'virtualization_alert_state', 'INSERT')")
    assert pg_db.scalar("SELECT has_table_privilege('zenplus', 'ncm_alert_state', 'INSERT')")


# ── Rule API validator and scope ────────────────────────────────────────────

def test_rule_api_validates_the_virtualisation_metrics(pg_db):
    db = pg_db
    cid = add_connector(db)
    group = str(db.scalar("INSERT INTO device_groups (name) VALUES ('core') RETURNING id"))
    base = {"name": "Datastore", "metric": "vm_datastore_pct", "operator": ">", "threshold": 85,
            "severity": "warning", "notify_channels": []}
    with api_client(db, role="admin", persist_user=True, username="admin-1") as c:
        r = c.post("/api/v1/alert-rules", json={**base, "target": cid})
        assert r.status_code == 201, r.text
        assert r.json()["family"] == "virtualization"
        ok = [
            {**base, "conditions": [{"metric": "vm_datastore_pct", "operator": ">", "threshold": 85,
                                     "reset_threshold": 75}]},
            {**base, "metric": "vm_host_disconnected", "operator": "==", "threshold": 1, "scope_tag": "prod"},
            {**base, "metric": "vm_alarm", "operator": ">=", "threshold": 2},
        ]
        for body in ok:
            assert c.post("/api/v1/alert-rules", json=body).status_code == 201, body
        bad = [
            {**base, "operator": "=="},
            {**base, "threshold": 120},
            {**base, "conditions": [{"metric": "vm_datastore_pct", "operator": ">", "threshold": 85,
                                     "reset_threshold": 90}]},
            {**base, "conditions": [{"metric": "vm_datastore_pct", "operator": ">", "threshold": 85},
                                    {"metric": "rtt", "operator": ">", "threshold": 5}]},
            {**base, "metric": "vm_host_disconnected", "operator": "==", "threshold": 0},
            {**base, "metric": "vm_alarm", "operator": ">=", "threshold": 3},
            {**base, "metric": "vm_alarm", "operator": "<", "threshold": 1},
            {**base, "group_id": group},
            {**base, "trigger_on": "down"},
            {**base, "target": "00000000-0000-0000-0000-000000000001"},
            {**base, "target": "not-a-uuid"},
        ]
        for body in bad:
            assert c.post("/api/v1/alert-rules", json=body).status_code == 422, body
        rid = r.json()["id"]
        assert c.put(f"/api/v1/alert-rules/{rid}", json={"operator": "<"}).status_code == 200
        assert c.put(f"/api/v1/alert-rules/{rid}", json={"threshold": 101}).status_code == 422
        impact = c.get(f"/api/v1/alert-rules/{rid}/impact")
        assert impact.status_code == 200, impact.text
        assert impact.json()["scope"]["label"] == "Virtualisation source vc-01"
        assert impact.json()["affected"] == {"count": 1, "noun": "virtualisation source", "shared": False}


def test_rule_scope_family_and_labels():
    from app.services import alert_rule_scope as rs
    from app.services import notification_grouping as ng
    rule = {"metric": "vm_alarm", "target": CID, "scope_tag": "prod"}
    assert rs.family(rule) == "virtualization"
    assert rs.describe_scope(rule, {"virtualization_source": "vc-01"})["label"] == \
        "Virtualisation source vc-01, tagged prod"
    assert rs.describe_scope({"metric": "vm_datastore_pct"})["label"] == "All virtualisation sources"
    assert ng.event_family("vm_datastore_pct") == "virtualization"
