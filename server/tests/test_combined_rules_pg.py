"""Combined rules on devices and service checks (multi-condition rules phase 2,
migrate-212): the real combined pass against a disposable PostgreSQL, with
fake ClickHouse readings and captured notifications.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException

from app.services import combined_readers as cr
from app.services import combined_rule_service as crs
from app.services import host_alert_service as has
from app.services import rule_conditions as rc

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)
from test_host_alert_rules_depth import CHANNEL, History, session  # noqa: F401


@pytest.fixture
def world(pg_db, monkeypatch):
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM combined_rule_state;"
                  " DELETE FROM service_checks; DELETE FROM device_maintenance")
    device = str(pg_db.scalar("INSERT INTO devices (hostname, ip_address, status, last_rtt_ms, last_seen)"
                              " VALUES ('core-sw', '192.0.2.10', 'up', 4, now()) RETURNING id"))
    snmp: dict = {}
    sent: list[dict] = []
    history = History()

    async def dispatch(db, channel_ids, ctx):
        sent.append(ctx)
        return len(channel_ids)

    def latest(device_ids, keys, since):
        return {(d, k): (v, time.time() - 10) for (d, k), v in snmp.items() if d in device_ids and k in keys}

    monkeypatch.setattr(has, "dispatch_to_channels", dispatch)
    monkeypatch.setattr(has, "fetch_host_history", history.fetch)
    monkeypatch.setattr(cr, "_latest_snmp", latest)
    yield NS(db=pg_db, device=device, snmp=snmp, sent=sent, history=history)


def add_rule(w, tree, subject, *, min_duration=0, clear_duration=0, device_id=None, service_check_id=None):
    tree = rc.validate_tree(tree, subject)
    return str(w.db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, min_duration, clear_duration,"
        " recovery_alert, notify_channels, cooldown, condition_tree, subject_type, device_id, service_check_id)"
        " VALUES ('Combined', 'combined', '>', 0, 'critical', $1, $2, true, $3::jsonb, 0, $4::jsonb, $5,"
        " $6, $7) RETURNING id",
        min_duration, clear_duration, [CHANNEL], json.dumps(tree), subject, device_id, service_check_id))


def add_check(w, status="up", *, response=120.0, device=None):
    return str(w.db.scalar(
        "INSERT INTO service_checks (name, check_type, target_host, status, enabled, last_check_at,"
        " last_response_ms, check_interval, device_id) VALUES ('Shop HTTPS', 'http', 'shop.example.test', $1,"
        " true, now(), $2, 60, $3) RETURNING id", status, response, device))


async def run(w, now=None):
    async with session(w.db) as s:
        return await crs.evaluate_combined_rules(s, now=now or time.time())


def incidents(w):
    return w.db.query("SELECT id, status, device_id, service_check_id, metadata FROM alerts"
                      " WHERE metadata->>'source' = 'combined_rule' ORDER BY triggered_at, id")


@pytest.mark.asyncio
async def test_device_cpu_and_memory(world):
    w = world
    add_rule(w, {"logic": "AND", "items": [{"metric": "cpu", "operator": ">", "threshold": 85},
                                           {"metric": "memory", "operator": ">", "threshold": 85}]}, "device")
    w.snmp.update({(w.device, "cpu"): 95.0, (w.device, "memory"): 40.0})
    await run(w)
    assert incidents(w) == []
    w.snmp[(w.device, "memory")] = 91.0
    await run(w)
    [(_, status, did, _, meta)] = incidents(w)
    assert status == "active" and str(did) == w.device and meta["subject_key"] == f"device:{w.device}"
    [trigger] = w.sent
    assert trigger["event_sentence"].startswith("Device core-sw (192.0.2.10): ")
    assert "CPU 95% (limit 85%) and memory 91% (limit 85%)" in trigger["event_sentence"]
    assert trigger["delivery_event"] == "trigger" and trigger["alert_id"]
    w.snmp[(w.device, "cpu")] = 20.0
    await run(w)
    [(_, status, _, _, _)] = incidents(w)
    assert status == "resolved" and w.sent[-1]["is_recovery"]


@pytest.mark.asyncio
async def test_real_outage_check_down_while_device_answers(world):
    w = world
    check = add_check(w, "up", device=w.device)
    add_rule(w, {"logic": "AND", "items": [{"metric": "service_down"},
                                           {"metric": "device_down", "on": "device"}]}, "service_check",
             service_check_id=check)
    add_rule(w, {"logic": "AND", "items": [{"metric": "service_down"},
                                           {"metric": "rtt", "operator": "<", "threshold": 50, "on": "device"}]},
             "service_check", service_check_id=check)
    w.db.execute("UPDATE service_checks SET status = 'down' WHERE id = $1", check)
    await run(w)
    rows = incidents(w)
    # Device up (not down): the first rule's AND is false; the second fires (check down, device answering).
    assert len(rows) == 1 and str(rows[0][3]) == check
    assert "linked device core-sw latency 4 ms" in w.sent[0]["event_sentence"]
    assert w.sent[0]["event_sentence"].startswith("Service check Shop HTTPS: ")


@pytest.mark.asyncio
async def test_linked_device_in_maintenance_is_unknown(world):
    w = world
    check = add_check(w, "down", device=w.device)
    add_rule(w, {"logic": "AND", "items": [{"metric": "service_down"},
                                           {"metric": "rtt", "operator": "<", "threshold": 50, "on": "device"}]},
             "service_check", service_check_id=check)
    w.db.execute("INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at)"
                 " VALUES ('device', $1, now() - interval '5 minutes', now() + interval '1 hour')", w.device)
    await run(w)
    # The check's own device is in maintenance: the check is not judged at all.
    assert incidents(w) == []


@pytest.mark.asyncio
async def test_check_slow_while_its_server_is_busy(world):
    w = world
    server = str(w.db.scalar("INSERT INTO servers (display_name, hostname, collection_mode, status, os_type,"
                             " device_id) VALUES ('web', 'web-01', 'agent', 'healthy', 'linux', $1) RETURNING id",
                             w.device))
    check = add_check(w, "up", response=2600, device=w.device)
    add_rule(w, {"logic": "AND", "items": [{"metric": "service_response_ms", "operator": ">", "threshold": 2000},
                                           {"metric": "host_cpu_pct", "operator": ">", "threshold": 90,
                                            "on": "server"}]}, "service_check")
    now = time.time()
    w.history.add("host_cpu_pct", server, [(now - 20, 96.0)])
    await run(w, now)
    [(_, status, _, cid, meta)] = incidents(w)
    assert status == "active" and str(cid) == check
    assert "linked server web-01 host CPU 96%" in w.sent[0]["event_sentence"]
    assert {m["on"] for m in meta["matched"]} == {"self", "server"}


@pytest.mark.asyncio
async def test_per_condition_hold_and_exclusion(world):
    w = world
    rule = add_rule(w, {"logic": "AND", "items": [{"metric": "cpu", "operator": ">", "threshold": 85, "for_s": 120},
                                                  {"metric": "device_down"}]}, "device")
    w.db.execute("UPDATE devices SET status = 'down' WHERE id = $1", w.device)
    w.snmp[(w.device, "cpu")] = 95.0
    t = time.time()
    await run(w, t)
    await run(w, t + 60)
    assert incidents(w) == []                              # CPU's own hold still running
    await run(w, t + 125)
    [(_, status, _, _, _)] = incidents(w)
    assert status == "active"
    # Excluding the device for this rule resolves its incident silently.
    w.db.execute("INSERT INTO alert_rule_exclusions (rule_id, asset_kind, device_id, reason)"
                 " VALUES ($1, 'device', $2, 'lab')",
                 rule, w.device)
    sent = len(w.sent)
    await run(w, t + 185)
    [(_, status, _, _, meta)] = incidents(w)
    assert status == "resolved" and meta["resolved_by"] == "out_of_scope" and len(w.sent) == sent


@pytest.mark.asyncio
async def test_single_condition_evaluators_never_load_combined_rules(world):
    w = world
    add_rule(w, {"logic": "AND", "items": [{"metric": "service_down"}, {"metric": "service_degraded"}]},
             "service_check", service_check_id=add_check(w))
    from app.api.v1 import alert_engine
    async with session(w.db) as s:
        assert [r for r in await alert_engine._load_device_rules(s) if r.metric == "combined"] == []


def test_api_validation_for_combined_rules():
    from app.api.v1.alert_rules import _combined_or_422
    tree = {"logic": "AND", "items": [{"metric": "service_down"}, {"metric": "service_response_ms",
                                                                    "operator": ">", "threshold": 900}]}
    assert rc.leaves(_combined_or_422(tree, "service_check", {}))[0]["id"] == "c1"
    with pytest.raises(HTTPException) as exc:
        _combined_or_422(tree, "service_check", {"device_id": "x"})
    assert "cannot be scoped by device_id" in exc.value.detail
    with pytest.raises(HTTPException):
        _combined_or_422(tree, None, {})
    with pytest.raises(HTTPException):
        _combined_or_422(tree, "device", {})          # service metrics on a device rule
