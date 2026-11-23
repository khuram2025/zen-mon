"""Combined rules, phase 3: events in a window on devices, APM services,
virtualisation sources, the appliance, replay over history and template
rule packs. The real combined pass against a disposable PostgreSQL, with fake
ClickHouse readings and captured notifications."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from app.services import combined_readers as cr
from app.services import combined_replay as replay
from app.services import rule_conditions as rc

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)
from test_combined_rules_pg import add_rule, incidents, run, session, world  # noqa: F401
from test_host_alert_rules_depth import CHANNEL  # noqa: F401


@pytest.mark.asyncio
async def test_device_down_after_a_trap_and_syslog_errors(world, monkeypatch):
    w = world
    monkeypatch.setattr(cr, "_trap_counts", lambda ids, prefix, since:
                        {w.device: 2} if prefix == "1.3.6.1.6.3.1.1.5.3" else {})
    add_rule(w, {"logic": "AND", "items": [
        {"metric": "trap_count", "operator": ">=", "threshold": 1, "target": "1.3.6.1.6.3.1.1.5.3"},
        {"metric": "syslog_count", "operator": ">=", "threshold": 3, "window_s": 600}]}, "device")
    for _ in range(2):
        w.db.execute("INSERT INTO network_events (id, source_ip, device_id, facility, severity, message)"
                     " VALUES (gen_random_uuid(), '192.0.2.10', $1, 1, 3, 'link down')", w.device)
    await run(w)
    assert incidents(w) == []                              # 2 syslog errors < 3
    w.db.execute("INSERT INTO network_events (id, source_ip, device_id, facility, severity, message)"
                 " VALUES (gen_random_uuid(), '192.0.2.10', $1, 1, 2, 'critical fan')", w.device)
    await run(w)
    [(_, status, _, _, meta)] = incidents(w)
    assert status == "active"
    sentence = w.sent[0]["event_sentence"]
    assert "2 traps in 5 min (limit 1)" in sentence and "3 syslog errors in 10 min (limit 3)" in sentence


@pytest.mark.asyncio
async def test_configuration_changes_count_new_versions_only(world):
    w = world
    for h in ("a", "a", "b", "c"):
        w.db.execute("INSERT INTO device_configs (device_id, content, content_hash, captured_at)"
                     " VALUES ($1, 'cfg', $2, now() - interval '1 minute')", w.device, h)
    async with session(w.db) as s:
        out = await cr.event_counts(s, [w.device], [{"id": "c1", "metric": "config_changes", "window_s": 3600}],
                                    now=time.time())
    assert out == {"c1": {w.device: 2}}                    # a -> b -> c; the repeated a is no change


@pytest.mark.asyncio
async def test_apm_service_latency_and_errors(world, monkeypatch):
    w = world
    from app.services import apm_alert_service
    fleet = {"checkout": {"apm_latency_p95": 1400.0, "apm_error_rate": 0.02}}
    monkeypatch.setattr(apm_alert_service, "_service_fleet", lambda window: fleet)
    add_rule(w, {"logic": "AND", "items": [{"metric": "apm_latency_p95", "operator": ">", "threshold": 1000},
                                           {"metric": "apm_error_rate", "operator": ">", "threshold": 0.05}]},
             "apm_service")
    await run(w)
    assert incidents(w) == []
    fleet["checkout"]["apm_error_rate"] = 0.09
    await run(w)
    [(_, status, did, cid, meta)] = incidents(w)
    assert status == "active" and did is None and cid is None and meta["subject_key"] == "apm:checkout"
    assert w.sent[0]["event_sentence"].startswith("APM service checkout: ")
    # No traffic now: no reading, the incident stays open (not "out of scope").
    fleet.clear()
    await run(w)
    [(_, status, _, _, _)] = incidents(w)
    assert status == "active"


@pytest.mark.asyncio
async def test_appliance_disk_or_backup(world, monkeypatch):
    w = world
    from app.services import appliance_alert_service as aas
    readings = {"appliance_disk_pct": [aas.Reading(70.0)], "appliance_backup_failed": [aas.Reading(0.0)]}

    async def collect(db, metrics, now, sources):
        return {m: readings.get(m) for m in metrics}
    monkeypatch.setattr(aas, "collect", collect)
    add_rule(w, {"logic": "OR", "items": [{"metric": "appliance_disk_pct", "operator": ">", "threshold": 85},
                                          {"metric": "appliance_backup_failed"}]}, "appliance")
    await run(w)
    assert incidents(w) == []
    readings["appliance_backup_failed"] = [aas.Reading(1.0)]
    await run(w)
    [(_, status, _, _, meta)] = incidents(w)
    assert status == "active" and meta["subject_key"] == "appliance"
    assert "scheduled backup failed or overdue" in w.sent[0]["event_sentence"]


@pytest.mark.asyncio
async def test_virtualisation_datastore_and_alarm(world, monkeypatch):
    w = world
    from app.services import virtualization_alert_service as vas
    connector = {"id": "11111111-1111-1111-1111-111111111111", "name": "vc-01", "provider": "vmware",
                 "endpoint": "https://vc-01", "tags": [], "enabled": True, "interval_seconds": 300,
                 "last_success_at": datetime.now(timezone.utc), "linked": {"id": w.device, "hostname": "core-sw",
                                                                           "ip": "192.0.2.10"},
                 "snapshot": {"resources": [{"id": "ds1", "kind": "storage", "name": "DS-01",
                                             "metrics": {"storage_usage_percent": 93}}],
                              "alerts": [{"id": "a1", "severity": "critical", "title": "Host CPU usage",
                                          "resource_id": "ds1"}]}}

    async def load(db):
        return [connector]
    monkeypatch.setattr(vas, "load_connectors", load)
    add_rule(w, {"logic": "AND", "items": [{"metric": "vm_datastore_pct", "operator": ">", "threshold": 90},
                                           {"metric": "vm_alarm", "operator": ">=", "threshold": 2}]},
             "virtualization")
    await run(w)
    [(_, status, did, _, meta)] = incidents(w)
    assert status == "active" and str(did) == w.device and meta["subject_key"].startswith("vc:")
    assert "platform alarm Host CPU usage critical" in w.sent[0]["event_sentence"]


@pytest.mark.asyncio
async def test_replay_finds_the_episodes_a_rule_would_have_raised(world, monkeypatch):
    w = world
    now = 1_800_000_000.0
    start = replay._bucket(now) - 86400
    high = {replay._bucket(start + 3600 + i * 300) for i in range(6)}             # 30 minutes high
    cpu = {b: (95.0 if b in high else 40.0) for b in range(start, start + 86400, 300)}
    mem = {b: (92.0 if b in high else 50.0) for b in cpu}

    def query(sql, params):
        if "snmp_metrics" in sql:
            data = cpu if params["k"] == "cpu" else mem
            return [(w.device, "", b, v) for b, v in data.items()]
        return []
    monkeypatch.setattr(replay, "_query", query)
    tree = rc.validate_tree({"logic": "AND", "items": [{"metric": "cpu", "operator": ">", "threshold": 85},
                                                       {"metric": "memory", "operator": ">", "threshold": 85},
                                                       {"metric": "trap_count", "operator": ">=", "threshold": 1}]},
                            "device")
    from types import SimpleNamespace
    rule = SimpleNamespace(id=None, subject_type="device", condition_tree=tree, min_duration=600, clear_duration=0,
                           device_id=w.device, group_id=None, device_type=None, location=None, scope_tag=None,
                           profile_id=None, service_check_id=None, service_check_group_id=None, target=None)
    async with session(w.db) as s:
        res = await replay.replay(s, rule, tree, "device", days=1, now=now)
    # Traps have no stored history: they read as unknown, so the AND never holds.
    assert res["episode_count"] == 0 and res["not_replayed_labels"] == ["At least 1 trap in 5 min"]
    tree2 = {**tree, "items": tree["items"][:2]}
    async with session(w.db) as s:
        res = await replay.replay(s, rule, tree2, "device", days=1, now=now)
    assert res["subjects"] == 1 and res["episode_count"] == 1
    [ep] = res["episodes"]
    assert ep["start"] == start + 3600 + 600 and ep["end"] == start + 3600 + 1800    # the 10-minute hold


def test_template_rule_pack_carries_a_combined_device_rule():
    from app.services import rule_packs
    [item] = rule_packs.validate_pack([{
        "key": "overloaded", "name": "Overloaded", "severity": "warning",
        "condition_tree": {"logic": "AND", "items": [{"metric": "cpu", "operator": ">", "threshold": 85},
                                                     {"metric": "memory", "operator": ">", "threshold": 85}]}}])
    assert item["metric"] == "combined" and item["condition_tree"]["items"][0]["id"] == "c1"
    fields = rule_packs.rule_fields(item)
    assert fields["subject_type"] == "device" and fields["condition_tree"] == item["condition_tree"]
    with pytest.raises(rule_packs.PackError, match="cannot be combined"):
        rule_packs.validate_pack([{"key": "x", "condition_tree": {"logic": "AND", "items": [
            {"metric": "host_cpu_pct", "operator": ">", "threshold": 1}, {"metric": "cpu", "operator": ">", "threshold": 1}]}}])
