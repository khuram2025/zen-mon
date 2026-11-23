"""Combined server rules (multi-condition rules phase 1, migrate-211).

The real host evaluator against a disposable PostgreSQL with one synthetic
server and a fake ClickHouse history; notifications are captured.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException

from app.services import host_alert_service as has
from app.services import host_depth_metrics as hdm

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)
from test_host_alert_rules_depth import CHANNEL, History, T0, incidents, run, session  # noqa: F401

CPU = {"metric": "host_cpu_pct", "operator": ">", "threshold": 90}
MEM = {"metric": "host_memory_pct", "operator": ">", "threshold": 90}


@pytest.fixture
def world(pg_db, monkeypatch):
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM service_metric_alert_state")
    server = str(pg_db.scalar(
        "INSERT INTO servers (display_name, hostname, collection_mode, status, os_type)"
        " VALUES ('synthetic-a', 'srv-synthetic-a', 'agent', 'healthy', 'linux') RETURNING id"))
    history = History()
    sent: list[dict] = []

    async def dispatch(db, channel_ids, ctx):
        sent.append(ctx)
        return len(channel_ids)

    monkeypatch.setattr(has, "fetch_host_history", history.fetch)
    monkeypatch.setattr(has, "dispatch_to_channels", dispatch)
    hdm.reset_forecast_cache()
    yield NS(db=pg_db, server=server, history=history, sent=sent)
    hdm.reset_forecast_cache()


def add_tree_rule(w, tree, *, min_duration=0, clear_duration=0, recovery_alert=True):
    from app.api.v1.host_alert_rules import _tree_columns, _tree_or_422
    cols = _tree_columns(_tree_or_422(tree))
    return str(w.db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, min_duration, clear_duration,"
        " recovery_alert, notify_channels, cooldown, conditions, condition_logic, condition_tree, subject_type)"
        " VALUES ('Server under pressure', $1, $2, $3, 'warning', $4, $5, $6, $7::jsonb, 0, $8::jsonb, $9,"
        " $10::jsonb, 'server') RETURNING id",
        cols["metric"], cols["operator"], cols["threshold"], min_duration, clear_duration, recovery_alert,
        [CHANNEL], cols["conditions"], cols["condition_logic"], cols["condition_tree"]))


async def tick(w, t, **values):
    """One sample per metric at t, then a pass at t + 5."""
    for metric, value in values.items():
        if value is not None:
            w.history.add(metric, w.server, [(t, value)])
    await run(w, t + 5)


@pytest.mark.asyncio
async def test_and_rule_fires_only_when_both_breach_and_names_both(world):
    w = world
    add_tree_rule(w, {"logic": "AND", "items": [CPU, MEM]})
    await tick(w, T0, host_cpu_pct=95, host_memory_pct=50)
    assert incidents(w) == []
    await tick(w, T0 + 60, host_cpu_pct=95, host_memory_pct=93)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and meta["combined"] is True and meta["dedupe"].startswith("rule:")
    assert [m["metric"] for m in meta["matched"]] == ["host_cpu_pct", "host_memory_pct"]
    [trigger] = w.sent
    sentence = trigger["event_sentence"]
    assert "srv-synthetic-a" in sentence and sentence.count("(limit 90%)") == 2, sentence
    assert "and" in dict(trigger["details"])["Condition"]
    # CPU falls under its automatic 88% clear level: AND is false, the incident resolves.
    await tick(w, T0 + 120, host_cpu_pct=40, host_memory_pct=93)
    [(_, status, _)] = incidents(w)
    assert status == "resolved"
    recovery = w.sent[-1]
    assert recovery["is_recovery"] and "back within" in recovery["event_sentence"]
    assert "40%" in recovery["event_sentence"]


@pytest.mark.asyncio
async def test_owners_example_group_or_a_stopped_service(world):
    w = world
    add_tree_rule(w, {"logic": "OR", "items": [{"logic": "AND", "items": [CPU, MEM]},
                                               {"metric": "host_service_down", "target": "nginx"}]})
    w.db.execute("INSERT INTO server_service_inventory (server_id, service_name, start_mode, state)"
                 " VALUES ($1, 'nginx', 'auto', 'running')", w.server)
    await tick(w, T0, host_cpu_pct=20, host_memory_pct=30)
    assert incidents(w) == []
    w.db.execute("UPDATE server_service_inventory SET state = 'stopped' WHERE server_id = $1", w.server)
    await tick(w, T0 + 60, host_cpu_pct=20, host_memory_pct=30)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and [m["metric"] for m in meta["matched"]] == ["host_service_down"]
    assert "service nginx stopped" in w.sent[0]["event_sentence"]


@pytest.mark.asyncio
async def test_unknown_readings_never_raise(world):
    w = world
    add_tree_rule(w, {"logic": "AND", "items": [CPU, MEM]})
    await tick(w, T0, host_cpu_pct=99)                  # no memory sample at all
    await tick(w, T0 + 60, host_cpu_pct=99)
    assert incidents(w) == [] and w.sent == []


@pytest.mark.asyncio
async def test_breach_hold_counts_passes(world):
    w = world
    add_tree_rule(w, {"logic": "AND", "items": [CPU, MEM]}, min_duration=120)
    await tick(w, T0, host_cpu_pct=95, host_memory_pct=95)
    await tick(w, T0 + 60, host_cpu_pct=95, host_memory_pct=95)
    assert incidents(w) == []
    await tick(w, T0 + 120, host_cpu_pct=95, host_memory_pct=95)
    assert [s for _, s, _ in incidents(w)] == ["active"]
    # A dip restarts the hold for the next episode.
    hold = w.db.scalar("SELECT count(*) FROM service_metric_alert_state WHERE asset_key LIKE '%#tree'")
    assert hold == 1


@pytest.mark.asyncio
async def test_a_per_server_override_changes_one_condition(world):
    w = world
    rule = add_tree_rule(w, {"logic": "AND", "items": [CPU, MEM]})
    w.db.execute("INSERT INTO alert_rule_overrides (rule_id, server_id, metric, threshold, reason)"
                 " VALUES ($1, $2, 'host_memory_pct', 97, 'busy database host')", rule, w.server)
    await tick(w, T0, host_cpu_pct=95, host_memory_pct=95)
    assert incidents(w) == []                            # memory 95 < the server's 97
    await tick(w, T0 + 60, host_cpu_pct=95, host_memory_pct=98)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and "override" in meta


# ── API ──────────────────────────────────────────────────────────────────────

def test_tree_columns_mirror_the_first_condition():
    from app.api.v1.host_alert_rules import _tree_columns, _tree_or_422
    cols = _tree_columns(_tree_or_422({"logic": "OR", "items": [MEM, CPU]}))
    assert (cols["metric"], cols["operator"], cols["threshold"], cols["target"]) == ("host_memory_pct", ">", 90.0, None)
    assert json.loads(cols["condition_tree"])["items"][0]["id"] == "c1"
    with pytest.raises(HTTPException) as exc:
        _tree_or_422({"logic": "AND", "items": [CPU]})
    assert exc.value.status_code == 422


def test_sources_that_ignore_a_second_condition_refuse_it():
    from app.api.v1.alert_rules import _refuse_ignored_conditions
    two = lambda m: [{"metric": m, "operator": ">", "threshold": 1}] * 2  # noqa: E731
    for metric in ("apm_latency_p95", "udt_port_capacity_pct", "netpath_rtt", "service_status"):
        with pytest.raises(HTTPException) as exc:
            _refuse_ignored_conditions(two(metric))
        assert exc.value.status_code == 422 and "single condition" in exc.value.detail
    _refuse_ignored_conditions(two("cpu"))                 # device rules combine conditions
    _refuse_ignored_conditions(two("apm_latency_p95")[:1])  # one condition is fine
    # A rule stored before the check stays editable.
    _refuse_ignored_conditions(two("netpath_rtt"), existing={"conditions": json.dumps(two("netpath_rtt"))})
