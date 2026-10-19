"""Built-in (rule-less) alerts reach the escalation matrix (1.24.21), on a
disposable PostgreSQL.

The platform raises some alerts without an alert rule (agent offline, remote
sensor offline/degraded, NCM backup failed / config changed, software
baseline drift, server health thresholds). Before 1.24.21 the policy sweep
joined the rule, so none of them could ever escalate. Covered here:

* each family is stamped with its registry priority at insert (agent offline
  P2 through the real staleness sweep, sensor offline P2, NCM backup failed
  P3, health critical P3);
* an agent-offline P2 incident escalates through the P2 default policy: the
  step-0 page names the family, has no rule id (never "None"), and
  explain_route shows the priority-default route;
* health thresholds are not routed by default, and are once enabled — only
  after 5 minutes open;
* a family disabled in the setting is not routed and explain_route says why;
* an NCM event's step 0 skips the device's own NCM channels (already
  notified by the NCM outbox) but still records them;
* a remote sensor incident is named after the sensor;
* a baseline alert's metadata.rule_id (a software_baseline_rules id) is never
  read as an alert rule;
* migration 166 stamps open unstamped built-in rows only, idempotently.

Only dispatch_to_channels is faked; nothing reaches an installed database.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import alert_escalation_service as esc
from app.services import builtin_alert_sources as bas
from app.services import host_alert_service as has
from app.services import ncm_events
from app.services import sensor_health_service as shs
from app.services import server_health_service as srv

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

MIGRATION_166 = Path(__file__).resolve().parents[2] / "scripts" / "migrate-166-builtin-alert-priority.sql"


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.fixture
def world(pg_db, monkeypatch):
    db = pg_db

    def channel(name):
        return str(db.scalar("INSERT INTO notification_channels (name, type, config) VALUES ($1, 'email',"
                             " $2::jsonb) RETURNING id", name, {"recipients": f"{name}@example.test"}))

    ch = NS(noc=channel("noc"), team=channel("team"), ncm=channel("ncm-device"))

    def policy(name, prios, steps):
        pid = db.scalar("INSERT INTO escalation_policies (name, default_priorities, fallback_targets)"
                        " VALUES ($1, $2::varchar[], '[]'::jsonb) RETURNING id", name, list(prios))
        for i, (delay, targets) in enumerate(steps):
            db.execute("INSERT INTO escalation_steps (policy_id, position, delay_minutes, name, targets)"
                       " VALUES ($1, $2, $3, $4, $5::jsonb)", pid, i, delay, f"Step {i}", targets)
        return str(pid)

    server = str(db.scalar("INSERT INTO servers (display_name, hostname) VALUES ('db-01', 'db-01.example')"
                           " RETURNING id"))

    pages: list[NS] = []

    async def dispatch(db_, channels, ctx):
        pages.append(NS(channels=list(channels), ctx=ctx))
        return 1

    monkeypatch.setattr(has, "dispatch_to_channels", dispatch)

    def alert(meta, *, minutes_ago=1, severity="critical", priority=None, server_id=server,
              sensor_id=None, device_id=None, status="active"):
        return str(db.scalar(
            "INSERT INTO alerts (server_id, sensor_id, device_id, severity, message, status, triggered_at,"
            " priority, metadata) VALUES ($1, $2, $3, $4, 'built-in', $5, now() - make_interval(mins => $6),"
            " $7, $8::jsonb) RETURNING id", server_id, sensor_id, device_id, severity, status, minutes_ago,
            priority, meta))

    def setting(families, since=None):
        # Routing enabled a day ago unless told otherwise: an established
        # install, so incidents raised in the test are new ones.
        since = since or (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        db.execute("INSERT INTO system_settings (key, value) VALUES ('builtin_alert_routing', $1::jsonb)"
                   " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                   {"enabled_families": list(families), "enabled_since": {k: since for k in families}})

    setting(bas.DEFAULT_ENABLED)

    def state(alert_id):
        return db.scalar("SELECT metadata->'escalation' FROM alerts WHERE id = $1", alert_id)

    return NS(db=db, ch=ch, policy=policy, server=server, pages=pages, alert=alert, setting=setting,
              state=state)


def chan(i):
    return {"type": "channel", "id": i}


async def sweep(w, *, plus_minutes: float = 0):
    async with session(w.db) as s:
        return await esc.sweep_policy_escalations(
            s, now=datetime.now(timezone.utc) + timedelta(minutes=plus_minutes))


async def route(w, alert_id):
    async with session(w.db) as s:
        return await esc.explain_route(s, alert_id)


# ── Registry ─────────────────────────────────────────────────────────────────

def test_registry_priorities_and_recognition():
    assert {k: bas.priority_for(k, "critical") for k in bas.KEYS} == {
        "agent_offline": "P2", "sensor_offline": "P2", "sensor_degraded": "P3", "ncm_backup_failed": "P3",
        "ncm_config_changed": "P4", "baseline_drift": "P4", "health_threshold": "P3"}
    assert bas.priority_for("health_threshold", "warning") == "P4"
    # Off by default: health thresholds (duplicate the server rules) and
    # configuration changes (one incident per change that never resolves).
    assert bas.DEFAULT_ENABLED == tuple(k for k in bas.KEYS if k not in ("health_threshold",
                                                                          "ncm_config_changed"))
    assert bas.BY_KEY["ncm_config_changed"].default_enabled is False
    assert "never resolves" in bas.BY_KEY["ncm_config_changed"].note
    assert bas.family_of({"source": "agent_offline"}) == "agent_offline"
    assert bas.family_of({"ncm": True, "ncm_event": "backup.failed"}) == "ncm_backup_failed"
    assert bas.family_of({"source": "host_metric"}) is None
    assert bas.family_of({"source": "agent_offline"}, rule_id="x") is None
    assert bas.routable_sql("a", []) == "FALSE"
    assert bas.clean_families(["health_threshold", "nope", "agent_offline"]) == ["agent_offline",
                                                                                 "health_threshold"]


# ── Escalation ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_offline_escalates_through_the_p2_default_policy(world):
    w = world
    pid = w.policy("P2 service degradation", ["P2"], [(0, [chan(w.ch.noc)]), (15, [chan(w.ch.team)])])
    w.db.execute("INSERT INTO agents (agent_uid, server_id, hostname, status, last_heartbeat_at)"
                 " VALUES ('agent-1', $1, 'db-01', 'online', now() - interval '30 minutes')", w.server)
    async with session(w.db) as s:
        assert (await srv.sweep_stale(s))["agents_offline"] == 1
    alert_id, priority, rule_id = w.db.query(
        "SELECT id, priority, rule_id FROM alerts WHERE metadata->>'source' = 'agent_offline'")[0]
    assert priority == "P2" and rule_id is None

    counts = await sweep(w)
    assert counts["escalated"] == 1 and len(w.pages) == 1
    page = w.pages[0]
    assert page.channels == [w.ch.noc] and page.ctx["delivery_event"] == "trigger:S0"
    assert page.ctx["rule_id"] is None and page.ctx["priority"] == "P2"
    assert page.ctx["rule_name"] == "Agent offline"
    assert "Agent offline" in page.ctx["subject"] and "db-01.example" in page.ctx["subject"]
    assert "None" not in page.ctx["subject"] and "None" not in page.ctx["message"]
    assert ("Built-in event", "Agent offline") in page.ctx["details"]
    assert w.state(alert_id)["policy_id"] == pid

    await sweep(w, plus_minutes=16)
    assert [p.ctx["delivery_event"] for p in w.pages] == ["trigger:S0", "escalation:S1:r0"]

    r = await route(w, alert_id)
    assert r["policy"] == {"id": pid, "name": "P2 service degradation", "source": "priority_default"}
    assert r["builtin"]["key"] == "agent_offline" and r["builtin"]["enabled"] is True
    assert r["priority"] == "P2" and [s["position"] for s in r["steps"]] == [0, 1]
    assert any("Built-in event Agent offline" in n for n in r["notes"])

    # Resolution: the all-clear has no rule id either.
    w.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now() WHERE id = $1", alert_id)
    assert (await sweep(w, plus_minutes=17))["closed"] == 1
    assert w.pages[-1].ctx["delivery_event"] == "all_clear" and w.pages[-1].ctx["rule_id"] is None


@pytest.mark.asyncio
async def test_health_thresholds_are_routed_only_when_enabled_and_after_five_minutes(world):
    w = world
    w.policy("P3 team queue", ["P3"], [(0, [chan(w.ch.team)])])
    async with session(w.db) as s:
        await srv.sync_health_alerts(s, w.server, [
            {"level": "critical", "reason": "CPU at 99.0% (≥ 98%)", "dedupe": "health:cpu"}])
        await s.commit()
    alert_id, priority = w.db.query(
        "SELECT id::text, priority FROM alerts WHERE metadata->>'source' = 'health_threshold'")[0]
    assert priority == "P3"                                  # critical health is P3, not P2
    w.db.execute("UPDATE alerts SET triggered_at = now() - interval '10 minutes' WHERE id = $1", alert_id)

    assert (await sweep(w))["checked"] == 0 and w.pages == []            # off by default
    r = await route(w, alert_id)
    assert r["policy"]["source"] == "none" and r["builtin"]["enabled"] is False
    assert any("routing is disabled in settings" in n for n in r["notes"])

    w.setting(list(bas.KEYS))
    young = w.alert({"source": "health_threshold", "dedupe": "health:memory"}, minutes_ago=2,
                    priority="P3")
    assert (await sweep(w))["escalated"] == 1
    assert w.pages[0].channels == [w.ch.team] and w.pages[0].ctx["alert_id"] == alert_id
    assert w.state(young) is None                            # under 5 minutes open: not yet
    note = " ".join((await route(w, young))["notes"])
    assert "once it has been open for 5 minutes" in note


@pytest.mark.asyncio
async def test_a_disabled_family_is_not_routed(world):
    w = world
    w.policy("P2", ["P2"], [(0, [chan(w.ch.noc)])])
    w.setting([k for k in bas.DEFAULT_ENABLED if k != "agent_offline"])
    alert_id = w.alert({"source": "agent_offline", "dedupe": "agent_offline"}, priority="P2")
    assert (await sweep(w))["checked"] == 0 and w.pages == []
    r = await route(w, alert_id)
    assert r["policy"]["source"] == "none" and r["builtin"] == {
        "key": "agent_offline", "label": "Agent offline", "enabled": False, "enabled_since": None,
        "min_age_minutes": 0}
    w.setting(list(bas.DEFAULT_ENABLED))
    assert (await sweep(w))["escalated"] == 1


@pytest.mark.asyncio
async def test_ncm_step_zero_skips_the_device_ncm_channels(world):
    w = world
    w.policy("P3 team queue", ["P3"], [(0, [chan(w.ch.ncm), chan(w.ch.team)])])
    device = str(w.db.scalar("INSERT INTO devices (hostname, ip_address) VALUES ('edge-1', '10.9.0.1'::inet)"
                             " RETURNING id"))
    w.db.execute("INSERT INTO device_ncm (device_id, notify_channels) VALUES ($1, $2::uuid[])", device,
                 [w.ch.ncm])
    async with session(w.db) as s:
        _, created = await ncm_events.open_incident(s, device, category="auth", message="Login failed")
        await s.commit()
    assert created
    alert_id, priority = w.db.query("SELECT id, priority FROM alerts WHERE device_id = $1", device)[0]
    assert priority == "P3"
    assert w.db.scalar("SELECT count(*) FROM ncm_notification_outbox WHERE channel_id = $1", w.ch.ncm) == 1

    assert (await sweep(w))["escalated"] == 1
    page, = w.pages
    assert page.channels == [w.ch.team]                       # the NCM channel already has it
    assert page.ctx["rule_name"] == "Configuration backup failed" and "edge-1" in page.ctx["subject"]
    assert set(w.state(alert_id)["channels"]) == {w.ch.ncm, w.ch.team}

    # The NCM incident announces its own recovery to the device channels, so
    # the all-clear leaves them out.
    w.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now() WHERE id = $1", alert_id)
    await sweep(w, plus_minutes=1)
    assert w.pages[-1].ctx["delivery_event"] == "all_clear" and w.pages[-1].channels == [w.ch.team]


@pytest.mark.asyncio
async def test_sensor_offline_is_stamped_p2_and_named_after_the_sensor(world):
    w = world
    w.policy("P2", ["P2"], [(0, [chan(w.ch.noc)])])
    sensor = str(w.db.scalar("INSERT INTO sensors (name) VALUES ('branch-riyadh') RETURNING id"))
    async with session(w.db) as s:
        assert await shs.create_sensor_alert(s, sensor_id=sensor, severity="critical",
                                             message="Remote sensor branch-riyadh is offline",
                                             source="sensor_offline")
        await s.commit()
    assert w.db.scalar("SELECT priority FROM alerts WHERE sensor_id = $1", sensor) == "P2"
    await sweep(w)
    assert "branch-riyadh" in w.pages[0].ctx["subject"] and w.pages[0].ctx["hostname"] == "branch-riyadh"


@pytest.mark.asyncio
async def test_a_baseline_rule_id_is_not_read_as_an_alert_rule(world):
    w = world
    rule = str(w.db.scalar("INSERT INTO alert_rules (name, metric, operator, threshold, severity, enabled)"
                           " VALUES ('Some rule', 'host_cpu_pct', 'gt', 90, 'warning', false) RETURNING id"))
    # A baseline rule id that happens to equal an alert rule id.
    alert_id = w.alert({"source": "baseline", "dedupe": f"baseline:{rule}", "rule_id": rule},
                       severity="warning", priority="P4")
    r = await route(w, alert_id)
    assert r["builtin"]["key"] == "baseline_drift"
    assert not any("rule is disabled" in n for n in r["notes"])


def test_migration_166_stamps_open_unstamped_builtin_rows_only(world):
    w = world
    rows = {
        "agent": w.alert({"source": "agent_offline"}),
        "health_crit": w.alert({"source": "health_threshold"}),
        "health_warn": w.alert({"source": "health_threshold"}, severity="warning"),
        "baseline": w.alert({"source": "baseline", "rule_id": "00000000-0000-0000-0000-000000000001"},
                            severity="critical"),
        "stamped": w.alert({"source": "agent_offline"}, priority="P1"),
        "resolved": w.alert({"source": "agent_offline"}, status="resolved"),
        "host_metric": w.alert({"source": "host_metric"}),
    }
    sensor = str(w.db.scalar("INSERT INTO sensors (name) VALUES ('s-1') RETURNING id"))
    rows["sensor_deg"] = w.alert({"source": "sensor_degraded"}, server_id=None, sensor_id=sensor,
                                 severity="warning")
    device = str(w.db.scalar("INSERT INTO devices (hostname, ip_address) VALUES ('d-1', '10.9.0.2'::inet)"
                             " RETURNING id"))
    rows["ncm_change"] = w.alert({"ncm": True, "ncm_event": "config.changed"}, server_id=None,
                                 device_id=device, severity="warning")
    for _ in range(2):                                       # idempotent
        w.db.psql_file(MIGRATION_166)
        got = {k: w.db.scalar("SELECT priority FROM alerts WHERE id = $1", v) for k, v in rows.items()}
        assert got == {"agent": "P2", "health_crit": "P3", "health_warn": "P4", "baseline": "P4",
                       "stamped": "P1", "resolved": None, "host_metric": None, "sensor_deg": "P3",
                       "ncm_change": "P4"}


def test_routable_sql_names_enabled_families_only():
    assert "r.id IS NULL AND FALSE" in esc._policy_sweep_sql(ids_only=True)
    sql = bas.routable_sql("a", ["agent_offline"])
    assert "agent_offline" in sql and "health_threshold" not in sql and "interval" not in sql
    assert "interval '300 seconds'" in bas.routable_sql("a", ["health_threshold"])


@pytest.mark.asyncio
async def test_incidents_open_before_routing_was_enabled_are_never_paged(world):
    """Upgrade / enabling a family must not page the backlog: an agent that
    has been offline for two weeks stays as it is; a new outage is routed."""
    w = world
    w.policy("P2 service degradation", ["P2"], [(0, [chan(w.ch.noc)])])
    w.setting(["agent_offline"], since=(datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat())
    old = w.alert({"source": "agent_offline"}, minutes_ago=60 * 24 * 14, priority="P2")
    new = w.alert({"source": "agent_offline"}, minutes_ago=5, priority="P2")
    await sweep(w)
    paged = {p.ctx.get("alert_id") for p in w.pages}
    assert new in paged and old not in paged
    r = await route(w, old)
    assert r["policy"]["source"] == "none" and "before routing" in " ".join(r.get("notes") or [])


@pytest.mark.asyncio
async def test_a_family_enabled_by_default_is_stamped_on_first_sweep(world):
    """No stored setting (fresh upgrade): the first sweep records each enabled
    family's start, so incidents already open are left alone."""
    w = world
    w.policy("P2 service degradation", ["P2"], [(0, [chan(w.ch.noc)])])
    w.db.execute("DELETE FROM system_settings WHERE key = 'builtin_alert_routing'")
    old = w.alert({"source": "agent_offline"}, minutes_ago=60, priority="P2")
    # Before the first sweep the family is enabled but has no start yet:
    # explain says routing starts with the next sweep and does not claim this
    # already-open incident is routed (and explaining never stamps the start).
    r = await route(w, old)
    assert r["policy"]["source"] == "none" and r["steps"] == []
    assert (r["builtin"]["enabled"], r["builtin"]["enabled_since"]) == (True, None)
    assert "starts with the next escalation sweep" in " ".join(r["notes"])
    assert "only incidents raised after that sweep are escalated, so this one is not" in " ".join(r["notes"])
    assert w.db.scalar("SELECT count(*) FROM system_settings WHERE key = 'builtin_alert_routing'") == 0
    await sweep(w)
    assert old not in {p.ctx.get("alert_id") for p in w.pages}
    r = await route(w, old)
    assert r["policy"]["source"] == "none" and r["builtin"]["enabled_since"]
    assert "raised before routing for this event type was enabled" in " ".join(r["notes"])
    value = w.db.scalar("SELECT value FROM system_settings WHERE key = 'builtin_alert_routing'")
    assert "agent_offline" in value["enabled_since"] and "health_threshold" not in value["enabled_since"]
