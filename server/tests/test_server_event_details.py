"""Replay, scoping, collection budgets and lifecycle notification contracts."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import server_event_service as events, event_log_policy as policy
from app.services import host_alert_service as host
from app.schemas.agent import MetricSample
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401
from test_server_availability_alerts import add_server, add_rule, add_device, run, incidents, session


def event(**changes):
    return {"event_time": datetime.now(timezone.utc).isoformat(), "record_id": 99,
            "log_name": "System", "provider": "Service Control Manager", "event_id": 7031,
            "level": "error", "message": "SQL service failed", "fields": {"ServiceName": "SQL"}, **changes}


def test_event_identity_does_not_depend_on_language_and_groups_keep_component():
    row = event()
    a = events.normalize(row)
    b = events.normalize({**row, "message": "another localized rendering"})
    c = events.normalize({**row, "fields": {"ServiceName": "IIS"}})
    d = events.normalize({**row, "record_id": 100})
    assert a["event_uid"] == b["event_uid"] and a["group_key"] == b["group_key"]
    assert a["group_key"] != c["group_key"]
    assert a["event_uid"] != d["event_uid"] and a["group_key"] == d["group_key"]
    lower = events.normalize({**row,"log_name":"system"})
    assert lower["event_uid"] == a["event_uid"] and lower["group_key"] == a["group_key"]


def test_reused_record_id_after_log_clear_has_new_identity():
    now = datetime.now(timezone.utc)
    assert events.normalize(event(event_time=now))["event_uid"] != events.normalize(event(event_time=now-timedelta(hours=1)))["event_uid"]


def test_classic_provider_event_id_zero_is_valid():
    assert events.normalize(event(event_id=0))["event_id"] == 0


def test_source_is_required_for_lifecycle_classification():
    assert events.lifecycle_kind(events.normalize(event(event_id=41))) is None
    assert events.lifecycle_kind(events.normalize(event(provider="Microsoft-Windows-Kernel-Power",event_id=41))) == "unexpected_restart"
    assert events.lifecycle_kind(events.normalize(event(provider="EventLog",event_id=6005))) is None, "event-log service restart is not an OS restart"
    assert events.lifecycle_kind(events.normalize(event(provider="Microsoft-Windows-Kernel-General",event_id=13))) == "shutdown"
    assert events.lifecycle_kind(events.normalize(event(provider="User32",event_id=1074,fields={"param5":"restart"}))) == "restart_requested"


@pytest.mark.parametrize("metric,kind,phrase", [
    ("host_restart", "restart", "The server started"),
    ("host_restart", "restart_requested", "A server restart was requested"),
    ("host_restart", "unexpected_restart", "Windows reported an unexpected shutdown or restart"),
    ("host_shutdown", "shutdown", "The server began shutting down"),
    ("host_shutdown", "shutdown_requested", "A server shutdown was requested"),
])
def test_lifecycle_wording_is_readable_and_uses_configured_timezone(metric, kind, phrase):
    stamp = datetime(2026, 11, 1, 15, 30, 5, tzinfo=timezone.utc)
    breach, count, detail = events.lifecycle_value(metric, [(kind, stamp, 2)], "Asia/Riyadh")
    assert breach and count == 2
    assert detail.startswith(f"{phrase} on 01 Nov 2026 at 06:30:05 PM (Asia/Riyadh).")
    assert "T15:" not in detail
    assert ("does not confirm" in detail) == (metric == "host_shutdown")


def test_lifecycle_time_handles_dst_and_unknown_timezone():
    stamp = datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc)
    _, _, detail = events.lifecycle_value("host_restart", [("restart", stamp, 1)], "Europe/London")
    assert "04:30:00 PM (Europe/London)" in detail
    _, _, fallback = events.lifecycle_value("host_restart", [("restart", stamp, 1)], "Unknown/Zone")
    assert "03:30:00 PM (UTC)" in fallback
    assert events.lifecycle_value("host_restart", [("shutdown", stamp, 1)])[:2] == (False, 0.0)


def test_role_profiles_select_audit_failures_without_success_logon_stream():
    dc = policy.role_filters(1)
    security = next(f for f in dc if f["channel"] == "Security")
    assert security["levels"] == ["audit"] and 4625 in security["ids"] and 4624 not in security["ids"]
    assert policy.role_filters(0)[0]["levels"] == ["critical", "error"]
    assert any(f["channel"].startswith("Microsoft-Windows-IIS") for f in policy.role_filters(2))
    assert any(825 in f.get("ids",[]) for f in policy.role_filters(3))


@pytest.mark.parametrize("values", [{"event_max_events":1001},{"event_max_bytes":1},{"event_interval_s":5},{"event_profile":4},{"event_message_bytes":True}])
def test_policy_rejects_unbounded_or_invalid_traffic_settings(values):
    with pytest.raises(ValueError): policy.validated_limits(values)


def test_invalid_detail_rejected_before_durable_batch_claim():
    with pytest.raises(ValueError): MetricSample(kind="event_detail",timestamp=datetime.now(timezone.utc),data=event(record_id=0))


@pytest.fixture
def world(pg_db,monkeypatch):
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM alert_silences")
    sent=[]
    async def dispatch(db,channels,ctx): sent.append(ctx); return len(channels)
    monkeypatch.setattr(host,"dispatch_to_channels",dispatch)
    return SimpleNamespace(db=pg_db,sent=sent)


@pytest.mark.asyncio
async def test_replayed_lifecycle_batch_notifies_once_and_backfill_stays_quiet(world):
    now=datetime.now(timezone.utc).timestamp()
    sid=add_server(world,"events-01",heartbeat=now)
    aid=str(world.db.scalar("SELECT id FROM agents WHERE server_id=$1",sid))
    add_rule(world,"host_restart",recovery=False)
    row=event(provider="Microsoft-Windows-Kernel-Power",event_id=41)
    async with session(world.db) as db:
        await events.ingest_state(db,sid,aid,[row,row],None)
        await db.commit()
    assert world.db.scalar("SELECT count(*) FROM server_lifecycle_events")==1
    await run(world,now+1)
    await run(world,now+60)
    assert len(incidents(world,sid))==1 and len(world.sent)==1
    assert "unexpected" in incidents(world,sid)[0].message
    assert world.sent[0]["rule_name"]=="host_restart"
    async with session(world.db) as db:
        await events.ingest_state(db,sid,aid,[event(provider="Microsoft-Windows-Kernel-General",event_id=12,
                            record_id=100,event_time=datetime.fromtimestamp(now+70,timezone.utc))],None)
        await db.commit()
    await run(world,now+71)
    assert len(incidents(world,sid))==1 and len(world.sent)==1
    assert "The server started on" in incidents(world,sid)[0].message
    assert "unexpected" not in incidents(world,sid)[0].message
    await run(world,now+1000)
    assert incidents(world,sid)[0].status=="resolved" and len(world.sent)==1
    async with session(world.db) as db:
        await events.ingest_state(db,sid,aid,[event(provider="Microsoft-Windows-Kernel-General",event_id=12,
                            record_id=1,event_time=datetime.now(timezone.utc)-timedelta(hours=3))],None)
        await db.commit()
    await run(world,now+1001)
    assert len(incidents(world,sid))==1 and len(world.sent)==1


@pytest.mark.asyncio
async def test_shutdown_and_restart_have_separate_rules(world):
    now=datetime.now(timezone.utc).timestamp()
    sid=add_server(world,"events-02",heartbeat=now)
    aid=str(world.db.scalar("SELECT id FROM agents WHERE server_id=$1",sid))
    add_rule(world,"host_shutdown",recovery=False)
    add_rule(world,"host_restart",recovery=False)
    async with session(world.db) as db:
        await events.ingest_state(db,sid,aid,[event(provider="Microsoft-Windows-Kernel-General",event_id=13)],None)
        await db.commit()
    await run(world,now+1)
    assert len(world.sent)==1 and world.sent[0]["rule_name"]=="host_shutdown"


@pytest.mark.asyncio
async def test_maintenance_restart_stays_quiet_after_maintenance_ends(world):
    now=datetime.now(timezone.utc).timestamp()
    sid=add_server(world,"maintained-events",heartbeat=now,ip="192.0.2.80")
    did=add_device(world,"192.0.2.80","maintenance")
    aid=str(world.db.scalar("SELECT id FROM agents WHERE server_id=$1",sid))
    add_rule(world,"host_restart",recovery=False)
    async with session(world.db) as db:
        await events.ingest_state(db,sid,aid,[event(provider="Microsoft-Windows-Kernel-General",event_id=12)],None)
        await db.commit()
    await run(world,now+1)
    world.db.execute("UPDATE devices SET status='up' WHERE id=$1",did)
    await run(world,now+60)
    assert not world.sent and not incidents(world,sid)


@pytest.mark.asyncio
async def test_old_collection_status_cannot_overwrite_newer_status(world):
    now=datetime.now(timezone.utc)
    sid=add_server(world,"events-03",heartbeat=now.timestamp())
    aid=str(world.db.scalar("SELECT id FROM agents WHERE server_id=$1",sid))
    async with session(world.db) as db:
        for stamp,backlog in [(now,True),(now-timedelta(minutes=5),False)]:
            await events.ingest_state(db,sid,aid,[],{"event_collection":{"checked_at":stamp,"queries":[{"channel":"System","backlog":backlog,"gap":False}]}})
        await db.commit()
    status=world.db.scalar("SELECT status FROM server_event_collection WHERE server_id=$1",sid)
    assert status["queries"][0]["backlog"] is True


def test_grouped_routes_enforce_server_tag_scope_before_querying_store(world,monkeypatch):
    sid=add_server(world,"scoped-events",tags=("private",))
    def no_query(*args,**kw): raise AssertionError("out-of-scope event store was queried")
    monkeypatch.setattr(events,"query_groups",no_query)
    monkeypatch.setattr(events,"query_occurrences",no_query)
    with api_client(world.db,role="event-scoped-viewer",permissions=["servers.view"],scope_tags=("other",)) as client:
        assert client.get(f"/api/v1/servers/{sid}/event-groups").status_code==404
        assert client.get(f"/api/v1/servers/{sid}/event-groups/"+"a"*64).status_code==404


def test_event_group_api_validates_window_and_paginates(world,monkeypatch):
    sid=add_server(world,"events-page")
    seen=[]
    def groups(client,server,start,end,**kw):
        seen.append((server,kw))
        return {"items":[],"has_more":False,"total_events":0,"total_groups":0,"levels":{},"channels":[]}
    from app.core import database
    monkeypatch.setattr(database,"get_ch_client",lambda:object())
    monkeypatch.setattr(events,"query_groups",groups)
    with api_client(world.db,role="admin") as client:
        assert client.get(f"/api/v1/servers/{sid}/event-groups?limit=101").status_code==422
        assert client.get(f"/api/v1/servers/{sid}/event-groups?from=2026-01-01T00:00:00Z").status_code==422
        r=client.get(f"/api/v1/servers/{sid}/event-groups?limit=20&offset=40&level=audit&search=4625")
        assert r.status_code==200,r.text
        assert seen[-1][1]["limit"]==20 and seen[-1][1]["offset"]==40 and seen[-1][1]["search"]=="4625"


@pytest.mark.parametrize("platform,capable",[("windows",True),("windows",False),("linux",True)])
def test_config_negotiates_details_and_role_filters_without_breaking_old_agents(world,monkeypatch,platform,capable):
    from app.api.v1 import agents
    pid=str(world.db.scalar("SELECT id FROM agent_policies WHERE name='Windows Baseline'"))
    world.db.execute("UPDATE agent_policies SET event_log_filters='[]',cardinality_limits=$2::jsonb WHERE id=$1",pid,{"event_profile":1})
    async def authenticate(*args):
        return {"id":str(uuid4()),"platform":platform,"policy_id":pid,"update_ring":"stable",
                "capabilities":["event_details_v1"] if capable else []}
    monkeypatch.setattr(agents,"_authenticate",authenticate)
    with api_client(world.db,role="admin") as client:
        r=client.get("/api/v1/agents/config",headers={"X-Agent-Id":"test","Authorization":"Bearer test"})
        assert r.status_code==200,r.text
        data=r.json()
        assert data["feature_flags"]["event_details"] is (capable and platform=="windows")
        if capable and platform=="windows":assert any(f["channel"]=="Security" and f["levels"]==["audit"] for f in data["event_log_filters"])
        else:assert not any("audit" in f["levels"] for f in data["event_log_filters"])
        if platform=="linux":assert data["feature_flags"]["event_log"] is False
