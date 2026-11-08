"""Server availability alerting: one Down rule for devices and servers.

The field report: a Windows database server watched only by its agent was
down for about 80 minutes overnight and nobody was paged. "Device Down" read
ping_status from the poller, which never sees an agent server, and the
staleness sweep's built-in "agent offline" alert had no rule — so no channel
and no escalation policy — and only appeared 10 minutes after the last
heartbeat.

Now (server_availability, host_alert_service):

* an unscoped Device Down rule (ping_status, triggered on down) also pages
  for agent servers whose heartbeat is silent and that ping cannot vouch for;
* a silent agent on a server that still answers ping is "Agent offline", a
  separate P3 rule, never "down";
* a server whose linked ping device is down is left to the device path, and
  one in maintenance to nobody;
* the silence allowance is the rule's min_duration, else 3 minutes;
* the built-in alert is raised only for a server no rule covers.

The decision table is pinned on pure functions, then replayed through the
real host evaluator and staleness sweep against a disposable PostgreSQL.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import alert_rule_scope as rs
from app.services import host_alert_service as has
from app.services import server_availability as sa
from app.services import server_health_service as shs

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = 1_800_000_000.0   # synthetic epoch seconds; nothing reads the wall clock
CHANNEL = "00000000-0000-0000-0000-00000000c0de"


def sig(*, age=None, ping=None, tags=()):
    hb = None if age is None else datetime.fromtimestamp(T0 - age, timezone.utc)
    return sa.ServerSignals(server_id="s1", hostname="db3", tags=set(tags), heartbeat_at=hb, ping=ping)


DOWN = {"metric": "ping_status", "trigger_on": "down"}
SEEDED = {"metric": "ping_status", "trigger_on": "any", "operator": "eq", "threshold": 0}
AGENT = {"metric": "host_agent_offline"}


# ══ The decision table ══════════════════════════════════════════════════════

@pytest.mark.parametrize("age, ping, state", [
    (60, None, "up"),
    (60, "down", "up"),                     # a reporting agent proves the host is up
    (200, None, "down"),
    (200, "unknown", "down"),
    (200, "up", "agent_offline"),
    (200, "degraded", "agent_offline"),
    (200, "down", "device_down"),
    (200, "maintenance", "maintenance"),
    (None, None, "unknown"),                # never heartbeated: not down
])
def test_judge(age, ping, state):
    assert sa.judge(sig(age=age, ping=ping), T0, 180) == state


def test_silence_allowance_is_min_duration_else_three_minutes_with_a_floor():
    assert sa.down_after_seconds({"min_duration": 0}) == 180
    assert sa.down_after_seconds({"min_duration": None}) == 180
    assert sa.down_after_seconds({"min_duration": 600}) == 600
    assert sa.down_after_seconds({"min_duration": 30}) == sa.MIN_DOWN_AFTER_S


@pytest.mark.parametrize("rule, covers", [
    (DOWN, True),
    ({**DOWN, "scope_tag": "prod"}, True),
    ({**DOWN, "trigger_on": "any"}, False),
    ({**DOWN, "trigger_on": "up"}, False),
    ({**DOWN, "group_id": "g"}, False),
    ({**DOWN, "device_id": "d"}, False),
    ({**DOWN, "device_type": "switch"}, False),
    ({**DOWN, "location": "Riyadh"}, False),
    ({**DOWN, "service_check_id": "c"}, False),
    ({"metric": "rtt", "trigger_on": "down"}, False),
    # The seeded Device Down is an 'any' rule selecting down by ping_status == 0.
    (SEEDED, True),
    ({**SEEDED, "operator": "=="}, True),
    ({**SEEDED, "trigger_on": None}, True),
    ({**SEEDED, "operator": "lt", "threshold": 1}, True),
    ({**SEEDED, "threshold": 1}, False),                    # "comes up"
    ({**SEEDED, "operator": "neq"}, False),
    ({**SEEDED, "conditions": [{"metric": "ping_status", "operator": "eq", "threshold": 0}]}, True),
    ({**SEEDED, "conditions": [{"metric": "ping_status", "operator": "eq", "threshold": 0},
                               {"metric": "rtt", "operator": "gt", "threshold": 100}]}, False),
    ({**SEEDED, "group_id": "g"}, False),
])
def test_which_device_rules_also_cover_servers(rule, covers):
    assert sa.is_down_rule(rule) is covers


@pytest.mark.parametrize("rule, state, breach", [
    (DOWN, "down", True), (DOWN, "up", False), (DOWN, "agent_offline", False),
    (DOWN, "device_down", None), (DOWN, "maintenance", None), (DOWN, "unknown", None),
    (AGENT, "agent_offline", True), (AGENT, "up", False), (AGENT, "down", None),
    (AGENT, "device_down", None), (AGENT, "maintenance", None),
])
def test_each_rule_owns_only_its_state(rule, state, breach):
    assert sa.breach_for(rule, state) is breach


def test_rule_scope_by_server_and_tag():
    s = sig(age=200, tags=("prod",))
    assert sa.rule_covers({**DOWN, "scope_tag": "Prod "}, s)
    assert not sa.rule_covers({**DOWN, "scope_tag": "lab"}, s)
    assert sa.rule_covers({**AGENT, "server_id": "s1"}, s)
    assert not sa.rule_covers({**AGENT, "server_id": "s2"}, s)


def test_coverage_depends_on_the_state():
    s = sig(age=200)
    assert sa.covered([DOWN], s, "down")
    assert sa.covered([DOWN], s, "device_down")
    assert not sa.covered([DOWN], s, "agent_offline")
    assert sa.covered([AGENT], s, "agent_offline")
    assert not sa.covered([AGENT], s, "down")
    assert sa.covered([], s, "maintenance")
    assert not sa.covered([DOWN, AGENT], s, "unknown")


def test_scope_label_says_servers_are_covered():
    assert rs.describe_scope(DOWN)["label"] == "All devices and servers"
    assert rs.describe_scope({**DOWN, "scope_tag": "prod"})["label"] == "All devices and servers tagged prod"
    assert rs.describe_scope({**DOWN, "group_id": "g"}, {"group": "Core"})["label"] == "All devices in group Core"


# ══ The evaluator on a disposable PostgreSQL ════════════════════════════════

@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def test_migration_seeds_agent_offline_and_accepts_the_metric(pg_db):
    rule = pg_db.query("SELECT enabled, severity, priority, recovery_alert, server_id, notify_channels"
                       " FROM alert_rules WHERE metric = 'host_agent_offline'")
    assert len(rule) == 1
    enabled, severity, priority, recovery, server_id, channels = rule[0]
    assert (enabled, severity, priority, recovery, server_id) == (True, "warning", "P3", True, None)
    assert list(channels) == []     # routed by the P3 default policy


@pytest.fixture
def world(pg_db, monkeypatch):
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM alert_silences")
    sent: list[dict] = []

    async def dispatch(db, channel_ids, ctx):
        sent.append(ctx)
        return len(channel_ids)

    monkeypatch.setattr(has, "dispatch_to_channels", dispatch)
    paging_since(pg_db, T0 - 86_400)   # paging began a day before the synthetic clock
    return NS(db=pg_db, sent=sent)


def paging_since(db, epoch):
    db.execute("INSERT INTO system_settings (key, value) VALUES ($1, to_jsonb($2::float8))"
               " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", sa.SINCE_KEY, epoch)


def add_server(w, name, *, heartbeat=T0, ip=None, tags=()):
    sid = str(w.db.scalar(
        "INSERT INTO servers (display_name, hostname, collection_mode, status, primary_ip, tags)"
        " VALUES ($1, $1, 'agent', 'healthy', $2::inet, $3::jsonb) RETURNING id",
        name, ip, list(tags)))
    w.db.execute("INSERT INTO agents (server_id, agent_uid, hostname, status, last_heartbeat_at)"
                 " VALUES ($1, $2, $3, 'online', to_timestamp($4))", sid, f"uid-{name}", name, heartbeat)
    return sid


def beat(w, sid, at):
    w.db.execute("UPDATE agents SET last_heartbeat_at = to_timestamp($2) WHERE server_id = $1", sid, at)


def add_device(w, ip, status):
    return str(w.db.scalar(
        "INSERT INTO devices (hostname, ip_address, status, ping_enabled)"
        " VALUES ($1, $2::inet, $3, TRUE) RETURNING id", f"dev-{ip}", ip, status))


def add_rule(w, metric, *, name=None, severity="critical", min_duration=0, recovery=True,
             trigger=None, **scope):
    ping = metric == "ping_status"
    cols = "".join(f", {c}" for c in scope)
    vals = "".join(f", ${i + 9}" for i in range(len(scope)))
    return str(w.db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, min_duration,"
        f" recovery_alert, notify_channels, trigger_on{cols})"
        f" VALUES ($1, $2, $3, 0, $4, $5, $6, $7::jsonb, $8{vals}) RETURNING id",
        name or metric, metric, "eq" if ping else "gt", severity, min_duration, recovery,
        [CHANNEL], trigger or ("down" if ping else "any"), *scope.values()))


async def run(w, now):
    async with session(w.db) as s:
        return await has.evaluate_host_rules(s, now=now)


def incidents(w, sid):
    rows = w.db.query("SELECT rule_id::text, status, severity, message, metadata FROM alerts"
                      " WHERE server_id = $1 ORDER BY triggered_at, id", sid)
    return [NS(rule_id=r[0], status=r[1], severity=r[2], message=r[3], metadata=r[4]) for r in rows]


@pytest.mark.asyncio
async def test_field_replay_agent_only_server_down_pages_device_down_once(world):
    w = world
    down = add_rule(w, "ping_status", name="Device Down")
    db3 = add_server(w, "OCI-Payment-DB3", heartbeat=T0)

    await run(w, T0 + 60)
    assert incidents(w, db3) == [] and w.sent == []

    # Last heartbeat at T0: three minutes later the Device Down rule pages.
    await run(w, T0 + 185)
    [inc] = incidents(w, db3)
    assert (inc.rule_id, inc.status, inc.severity) == (down, "active", "critical")
    assert inc.metadata["availability"] == "down"
    assert "OCI-Payment-DB3" in inc.message and "ping not monitored" in inc.message
    [page] = w.sent
    assert page["rule_id"] == down and not page["is_recovery"]
    # The agent stopped reporting and there is no ICMP measurement.
    assert dict(page["details"])["Condition"] == "Server down (agent silent, reachability unknown)"
    assert page["body"] == ("Server OCI-Payment-DB3 has stopped reporting: its agent is silent and"
                            " no ping check can confirm whether it is up.")

    # Still down: the same incident, nothing re-sent.
    await run(w, T0 + 1800)
    assert len(incidents(w, db3)) == 1 and len(w.sent) == 1

    # The server comes back: resolved, with a recovery notice.
    beat(w, db3, T0 + 4800)
    await run(w, T0 + 4810)
    [inc] = incidents(w, db3)
    assert inc.status == "resolved"
    assert len(w.sent) == 2 and w.sent[1]["is_recovery"]


@pytest.mark.asyncio
async def test_the_seeded_any_change_device_down_also_pages_for_servers(world):
    # seed-devices.sql creates Device Down as trigger 'any' + ping_status == 0,
    # which is how the dev appliance still has it.
    w = world
    down = add_rule(w, "ping_status", name="Device Down", trigger="any")
    srv = add_server(w, "seeded1", heartbeat=T0)
    await run(w, T0 + 200)
    [inc] = incidents(w, srv)
    assert inc.rule_id == down and inc.status == "active"


@pytest.mark.asyncio
async def test_an_outage_older_than_paging_is_recorded_but_never_paged(world):
    # Upgrade day: lab boxes silent for months must not all page at once.
    w = world
    add_rule(w, "ping_status", name="Device Down")
    old = add_server(w, "lab-dead", heartbeat=T0 - 90 * 86_400)
    paging_since(w.db, T0)
    await run(w, T0 + 60)
    [inc] = incidents(w, old)
    assert inc.status == "active" and inc.metadata["paging"].startswith("held")
    assert str(inc.metadata["notified"]).lower() == "false"      # suppressed, not sent
    await run(w, T0 + 3600)                      # no late send while it stays silent
    assert w.sent == []
    beat(w, old, T0 + 7200)
    await run(w, T0 + 7210)                      # back: resolved, no orphan all-clear
    assert incidents(w, old)[0].status == "resolved" and w.sent == []
    await run(w, T0 + 7210 + 600)                # a new outage after paging began pages
    assert len(w.sent) == 1 and not w.sent[0]["is_recovery"]


@pytest.mark.asyncio
async def test_paging_since_is_recorded_once():
    from types import SimpleNamespace

    class DB:
        def __init__(self):
            self.value, self.calls = None, 0

        async def execute(self, stmt, params=None):
            self.calls += 1
            if str(stmt).startswith("INSERT") and self.value is None:
                self.value = 1234.5
            return SimpleNamespace(scalar=lambda: self.value)

    db = DB()
    assert await sa.paging_since(db) == 1234.5
    db.value = 1234.5
    assert await sa.paging_since(db) == 1234.5


@pytest.mark.asyncio
async def test_a_rule_incident_replaces_the_open_builtin_alert(world):
    w = world
    add_rule(w, "ping_status", name="Device Down")
    srv = add_server(w, "had-builtin", heartbeat=T0)
    w.db.execute("INSERT INTO alerts (server_id, severity, message, status, metadata)"
                 " VALUES ($1, 'critical', 'Agent offline', 'active',"
                 " '{\"source\":\"agent_offline\",\"dedupe\":\"agent_offline\"}'::jsonb)", srv)
    await run(w, T0 + 200)
    rows = {(i.rule_id is None, i.status) for i in incidents(w, srv)}
    assert rows == {(True, "resolved"), (False, "active")}


@pytest.mark.asyncio
async def test_a_pingable_server_with_a_silent_agent_is_agent_offline_not_down(world):
    w = world
    down = add_rule(w, "ping_status", name="Device Down")
    agent = add_rule(w, "host_agent_offline", name="Agent offline", severity="warning")
    srv = add_server(w, "app1", heartbeat=T0, ip="10.0.0.5")
    add_device(w, "10.0.0.5", "up")

    await run(w, T0 + 200)
    [inc] = incidents(w, srv)
    assert (inc.rule_id, inc.severity) == (agent, "warning")
    assert inc.metadata["availability"] == "agent_offline"
    assert [p["rule_id"] for p in w.sent] == [agent]
    assert down not in {i.rule_id for i in incidents(w, srv)}


@pytest.mark.asyncio
async def test_a_server_whose_ping_device_is_down_is_left_to_the_device_path(world):
    w = world
    add_rule(w, "ping_status", name="Device Down")
    add_rule(w, "host_agent_offline", severity="warning")
    srv = add_server(w, "app2", heartbeat=T0, ip="10.0.0.6")
    add_device(w, "10.0.0.6", "down")

    await run(w, T0 + 600)
    assert incidents(w, srv) == [] and w.sent == []


@pytest.mark.asyncio
async def test_maintenance_on_the_linked_device_pages_nobody(world):
    w = world
    add_rule(w, "ping_status", name="Device Down")
    srv = add_server(w, "app3", heartbeat=T0, ip="10.0.0.7")
    add_device(w, "10.0.0.7", "maintenance")

    await run(w, T0 + 600)
    assert incidents(w, srv) == []


@pytest.mark.asyncio
async def test_device_scoped_down_rules_stay_device_only(world):
    w = world
    group = str(w.db.scalar("INSERT INTO device_groups (name) VALUES ('Core') RETURNING id"))
    add_rule(w, "ping_status", name="Core down", group_id=group)
    srv = add_server(w, "app4", heartbeat=T0)

    await run(w, T0 + 600)
    assert incidents(w, srv) == []


@pytest.mark.asyncio
async def test_tag_scope_narrows_servers_like_devices(world):
    w = world
    add_rule(w, "ping_status", name="Prod down", scope_tag="prod")
    prod = add_server(w, "prod1", heartbeat=T0, tags=("prod",))
    lab = add_server(w, "lab1", heartbeat=T0, tags=("lab",))

    await run(w, T0 + 600)
    assert len(incidents(w, prod)) == 1
    assert incidents(w, lab) == []


@pytest.mark.asyncio
async def test_min_duration_sets_the_silence_allowance(world):
    w = world
    add_rule(w, "ping_status", name="Device Down", min_duration=600)
    srv = add_server(w, "slow1", heartbeat=T0)

    await run(w, T0 + 300)
    assert incidents(w, srv) == []
    await run(w, T0 + 605)
    assert len(incidents(w, srv)) == 1


# ══ The staleness sweep's built-in alert is only a fallback ═════════════════

def stale_server(w, name):
    sid = str(w.db.scalar(
        "INSERT INTO servers (display_name, hostname, collection_mode, status, last_seen)"
        " VALUES ($1, $1, 'agent', 'healthy', NOW() - interval '20 minutes') RETURNING id", name))
    w.db.execute("INSERT INTO agents (server_id, agent_uid, hostname, status, last_heartbeat_at)"
                 " VALUES ($1, $2, $3, 'online', NOW() - interval '20 minutes')", sid, f"uid-{name}", name)
    return sid


def builtin(w, sid):
    return w.db.query("SELECT rule_id FROM alerts WHERE server_id = $1"
                      " AND metadata->>'dedupe' = 'agent_offline'", sid)


@pytest.mark.asyncio
async def test_sweep_skips_the_unrouted_builtin_alert_for_a_covered_server(world):
    w = world
    add_rule(w, "ping_status", name="Device Down")
    srv = stale_server(w, "covered1")
    async with session(w.db) as s:
        out = await shs.sweep_stale(s)
    assert out["agents_offline"] == 1
    assert builtin(w, srv) == []


@pytest.mark.asyncio
async def test_sweep_keeps_the_builtin_alert_when_nothing_covers_the_server(world):
    w = world
    add_rule(w, "host_agent_offline", severity="warning")   # does not cover a server that is down
    srv = stale_server(w, "uncovered1")
    async with session(w.db) as s:
        await shs.sweep_stale(s)
    assert len(builtin(w, srv)) == 1
