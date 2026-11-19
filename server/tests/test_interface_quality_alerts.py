"""Interface quality alerts (alerting plan G-12).

The field complaint: one access port raised 29 "Interface errors high" incidents
reading "826 errors in the last 90 s" — a count, not a share of traffic, on a
port moving millions of packets. Nothing watched a port that keeps going up and
down, or one that renegotiated down to 100 Mb/s.

* ``if_error_rate_pct`` / ``if_discard_rate_pct``: errors (discards) as a share
  of the packets in the same poll interval, the worse direction; fewer than
  1,000 packets in a direction is no reading, never a breach.
* ``if_flaps``: oper-status changes in the rule's window (``min_duration``,
  default 15 minutes); clears once a whole window holds no change.
* ``if_speed_changed``: 1 for ``clear_duration`` (default 15 minutes) after an
  up interface's speed changed (migrate-205 trigger). Duplex is not collected.

ClickHouse is replaced by in-memory rows; PostgreSQL is the disposable
pg_fixture cluster. Nothing touches an appliance database.
"""
from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import alert_phrasing as ap
from app.services import network_history as nh
from app.services import network_alert_service as worker
from app.services.network_conditions import (auto_reset_threshold, evaluate,
                                             validate_conditions)

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = 1_800_000_000.0
MIN = 60.0


def _row(stamp, *, oper=1, errors=(0, 0), discards=(0, 0), pkts=(0, 0), device="d", if_index=1,
         octets=None):
    n = int((stamp - T0) // MIN) + 1
    in_oct, out_oct = octets or (1000 * n, 1000 * n)
    return (device, if_index, stamp, 1e6, 1e6, errors[0], errors[1], discards[0], discards[1],
            oper, in_oct, out_oct, pkts[0], pkts[1])


def _fake_clickhouse(monkeypatch, rows):
    from app.core import database

    def query(sql, parameters=None, **_):
        since = parameters["since"].timestamp()
        until = parameters["until"].timestamp()
        return NS(result_rows=[r for r in rows if since <= r[2] <= until])

    monkeypatch.setattr(database, "get_clickhouse_client", lambda: NS(query=query))


def _history(monkeypatch, rows, metrics=("if_error_rate_pct",)):
    _fake_clickhouse(monkeypatch, rows)
    _, interfaces = nh.fetch_history(list(metrics), 0, T0 + 100_000)
    return interfaces[("d", 1)]


def _counting(per_poll_errors, per_poll_pkts, *, polls=15, out_errors=0, out_pkts=None):
    """Cumulative counters growing by a fixed amount each poll."""
    out_pkts = per_poll_pkts if out_pkts is None else out_pkts
    return [_row(T0 + i * MIN, errors=(per_poll_errors * i, out_errors * i),
                 pkts=(per_poll_pkts * i, out_pkts * i)) for i in range(polls)]


# ── Error / discard rate ────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_in,bad_out,pkts_in,pkts_out,expected", [
    (3000, 0, 97_000, 50_000, (3.0, 0.0, 3.0)),          # inbound denominator adds the errors
    (0, 500, 50_000, 10_000, (0.0, 5.0, 5.0)),           # worse direction wins
    (50, 0, 60, 50_000, (None, 0.0, 0.0)),               # a quiet direction is not judged
    (50, 50, 60, 900, (None, None, None)),               # both quiet: no reading
    (None, 0, 50_000, 50_000, (None, 0.0, None)),        # unknown direction: unknown worst
    (5000, 9000, 50_000, 5_000, (5000 * 100 / 55_000, 100.0, 100.0)),   # capped at 100 %
])
def test_packet_rates(bad_in, bad_out, pkts_in, pkts_out, expected):
    got = nh.packet_rates(bad_in, bad_out, pkts_in, pkts_out)
    assert got == pytest.approx(expected) if None not in expected else got == expected


def test_the_field_example_is_a_tiny_share_and_never_breaches(monkeypatch):
    """826 errors per 90 s on a port moving 5 million packets per poll is
    0.017 %: the old count rule paged 29 times, a 1 % rate rule never fires."""
    rows = [_row(T0 + i * 90, errors=(800 * i, 26 * i), pkts=(5_000_000 * i, 4_000_000 * i))
            for i in range(20)]
    hist = _history(monkeypatch, rows, ("if_error_rate_pct", "if_errors"))
    assert [v for _, v in hist["if_errors"]][-1] == 826
    rate = hist["if_error_rate_pct"][-1][1]
    assert rate == pytest.approx(800 * 100 / 5_000_800)
    cond = [{"metric": "if_error_rate_pct", "operator": ">", "threshold": 1}]
    assert evaluate(cond, "AND", hist, now=T0 + 19 * 90, hold=600, max_gap=270).breach is False


def test_a_real_error_rate_breaches_after_its_hold_with_direction_detail(monkeypatch):
    hist = _history(monkeypatch, _counting(3000, 97_000, polls=13))
    assert hist["if_error_rate_pct"][-1][1] == pytest.approx(3.0)
    cond = [{"metric": "if_error_rate_pct", "operator": ">", "threshold": 1}]
    assert evaluate(cond, "AND", hist, now=T0 + 6 * MIN, hold=600, max_gap=180).breach is None
    assert evaluate(cond, "AND", hist, now=T0 + 12 * MIN, hold=600, max_gap=180).breach is True
    ctx = nh.rate_context(hist, "if_error_rate_pct", T0 + 12 * MIN)
    assert ctx["interval_s"] == 60 and ctx["in"] == pytest.approx(3.0) and ctx["out"] == 0
    assert ctx["in_packets"] == 97_000 and ctx["in_count"] == 3000
    assert worker.rate_detail("if_error_rate_pct", ctx) == (
        "inbound 3% (3000 errors in 97000 packets), outbound 0% (0 errors in 97000 packets) "
        "in the last 60 s")
    assert ap.reading_text("if_error_rate_pct", 2.4, ">", 1) == "2.4% of packets"


def test_a_quiet_port_gives_no_reading_and_restarts_the_hold(monkeypatch):
    """50 errors in 300 packets is a quiet port, not a 14 % error rate."""
    hist = _history(monkeypatch, _counting(50, 300))
    assert {v for _, v in hist["if_error_rate_pct"]} == {None}
    cond = [{"metric": "if_error_rate_pct", "operator": ">", "threshold": 1}]
    assert evaluate(cond, "AND", hist, now=T0 + 14 * MIN, hold=0, max_gap=180).breach is None


def test_a_packet_counter_reset_is_unknown_never_a_rate(monkeypatch):
    rows = _counting(10, 100_000, polls=6)
    # Counters cleared at the 5th poll: every delta of that poll is unknown.
    rows[4] = _row(T0 + 4 * MIN, errors=(1, 0), pkts=(5, 5), octets=(1, 1))
    hist = _history(monkeypatch, rows)
    by_t = dict(hist["if_error_rate_pct"])
    assert by_t[T0 + 3 * MIN] == pytest.approx(10 * 100 / 100_010)
    assert by_t[T0 + 4 * MIN] is None


def test_rows_without_packet_columns_read_unknown(monkeypatch):
    rows = [r[:12] for r in _counting(10, 100_000, polls=4)]
    hist = _history(monkeypatch, rows, ("if_error_rate_pct", "if_errors"))
    assert {v for _, v in hist["if_error_rate_pct"]} == {None}
    assert [v for _, v in hist["if_errors"]] == [10, 10, 10]


def test_discard_rate_uses_the_discard_counters(monkeypatch):
    rows = [_row(T0 + i * MIN, discards=(0, 600 * i), pkts=(50_000 * i, 20_000 * i)) for i in range(4)]
    hist = _history(monkeypatch, rows, ("if_discard_rate_pct",))
    assert hist["if_discard_rate_pct"][-1][1] == pytest.approx(3.0)
    assert hist["if_error_rate_pct"][-1][1] == 0


# ── Flapping ────────────────────────────────────────────────────────────────

def _flapping_rows(*, until_min=40, flaps=(15, 16, 17, 18, 19, 20), down_codes=(2,)):
    """Oper up, toggling at the given minutes (down at odd positions), then stable up."""
    rows, state = [], 1
    for m in range(until_min + 1):
        if m in flaps:
            state = 1 if state != 1 else down_codes[len(rows) % len(down_codes)]
        rows.append(_row(T0 + m * MIN, oper=state))
    return rows


def test_flap_series_counts_changes_in_the_window_once_it_is_covered(monkeypatch):
    hist = _history(monkeypatch, _flapping_rows(), ("if_flaps",))
    series = dict(nh.flap_series(hist[nh.OPER_CHANGES], 900))
    assert min(series) == T0 + 15 * MIN               # no count until 15 minutes are covered
    assert series[T0 + 20 * MIN] == 6
    assert series[T0 + 30 * MIN] == 5                 # the change at minute 15 left the window
    assert series[T0 + 35 * MIN] == 0


def test_down_to_lower_layer_down_is_not_a_flap(monkeypatch):
    rows = [_row(T0 + m * MIN, oper=(2 if m % 2 else 7)) for m in range(20)]
    hist = _history(monkeypatch, rows, ("if_flaps",))
    assert {v for _, v in nh.flap_series(hist[nh.OPER_CHANGES], 900)} == {0.0}


def test_flapping_fires_holds_and_clears_after_a_stable_window(monkeypatch):
    hist = _history(monkeypatch, _flapping_rows(), ("if_flaps",))
    hist["if_flaps"] = nh.flap_series(hist[nh.OPER_CHANGES], 900)
    cond = [{"metric": "if_flaps", "operator": ">=", "threshold": 5}]
    assert auto_reset_threshold(cond[0]) == 1.0
    assert evaluate(cond, "AND", hist, now=T0 + 20 * MIN + 5, max_gap=180).breach is True
    # Open incident: fewer than 5 changes but not yet a quiet window -> still open.
    assert evaluate(cond, "AND", hist, now=T0 + 34 * MIN + 5, max_gap=180, active=True).breach is True
    assert evaluate(cond, "AND", hist, now=T0 + 35 * MIN + 5, max_gap=180, active=True).breach is False
    # Polls stop: missing samples never resolve.
    gone = {k: [(t, v) for t, v in s if t <= T0 + 25 * MIN] for k, s in hist.items()}
    assert evaluate(cond, "AND", gone, now=T0 + 40 * MIN, max_gap=180, active=True).breach is None


def test_interface_window_defaults():
    rule = NS(metric="if_flaps", min_duration=None, clear_duration=None)
    assert worker.interface_window(rule) == (0, 0, 900)
    assert worker.interface_window(NS(metric="if_flaps", min_duration=300, clear_duration=120)) == (0, 120, 300)
    assert worker.interface_window(NS(metric="if_speed_changed", min_duration=600, clear_duration=None)) == (0, 0, 900)
    assert worker.interface_window(NS(metric="if_speed_changed", min_duration=0, clear_duration=60)) == (0, 0, 60)


# ── Validation and wording ──────────────────────────────────────────────────

@pytest.mark.parametrize("conditions,message", [
    ([{"metric": "if_flaps", "operator": ">=", "threshold": 5},
      {"metric": "if_util_pct", "operator": ">", "threshold": 80}], "rule of their own"),
    ([{"metric": "if_flaps", "operator": "<", "threshold": 5}], "use > or >="),
    ([{"metric": "if_speed_changed", "operator": "==", "threshold": 0}], "use == 1"),
    ([{"metric": "if_speed_changed", "operator": "!=", "threshold": 1}], "use == 1"),
])
def test_window_and_event_conditions_are_validated(conditions, message):
    with pytest.raises(ValueError, match=message):
        validate_conditions(conditions)


def test_valid_interface_quality_conditions():
    validate_conditions([{"metric": "if_flaps", "operator": ">=", "threshold": 5}])
    validate_conditions([{"metric": "if_speed_changed", "operator": "==", "threshold": 1}])
    validate_conditions([{"metric": "if_speed_changed", "operator": ">=", "threshold": 1}])
    validate_conditions([{"metric": "if_error_rate_pct", "operator": ">", "threshold": 1},
                         {"metric": "if_util_pct", "operator": ">", "threshold": 50}])


def test_sentences():
    who = "Switch core-1 (10.0.0.1)"
    flap = {"kind": "flaps", "window_s": 900, "count": 6, "oper": 1}
    assert worker.window_sentence(flap, iface="Gi1/0/24", who=who, recovered=False)["sentence"] == (
        "Interface Gi1/0/24 on Switch core-1 (10.0.0.1) changed state 6 times in 15 minutes; it is up now.")
    rec = worker.window_sentence({**flap, "count": 0, "oper": 2}, iface="Gi1/0/24", who=who, recovered=True)
    assert rec["sentence"] == ("Interface Gi1/0/24 on Switch core-1 (10.0.0.1) has not changed state "
                               "for 15 minutes; it is down now.")
    speed = {"kind": "speed", "window_s": 900, "speed_bps": 100_000_000, "previous_speed_bps": 1_000_000_000}
    s = worker.window_sentence(speed, iface="Gi1/0/24", who=who, recovered=False)
    assert s["sentence"] == "Interface Gi1/0/24 on Switch core-1 (10.0.0.1) now runs at 100 Mb/s (was 1 Gb/s)."
    assert (s["metric_name"], s["value_label"]) == ("Interface speed", "100 Mb/s (was 1 Gb/s)")
    assert worker.window_sentence(speed, iface="Gi1/0/24", who=who, recovered=True)["sentence"] == (
        "Interface Gi1/0/24 on Switch core-1 (10.0.0.1) has stayed at 100 Mb/s for 15 minutes "
        "with no further speed change.")
    assert [worker.link_speed_text(v) for v in (1e10, 2.5e9, 1e7, 0, None)] == [
        "10 Gb/s", "2.5 Gb/s", "10 Mb/s", "an unknown speed", "an unknown speed"]


def test_rule_api_and_phrasing_know_the_metrics():
    import re
    from app.api.v1.alert_rules import _CONDITION_METRICS
    for m in ("if_error_rate_pct", "if_discard_rate_pct", "if_flaps", "if_speed_changed"):
        assert re.fullmatch(f"({_CONDITION_METRICS})", m)
        assert m in worker.INTERFACE_METRICS and m in ap.METRICS
    assert ap.condition_label("if_error_rate_pct", ">", 1) == "Interface error rate above 1% of packets"
    assert ap.condition_label("if_speed_changed", "==", 1) == "Interface speed changed"


# ── Evaluator and migration on the disposable PostgreSQL ────────────────────

@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


async def _no_dispatch(*a, **k):
    return 0


@pytest.fixture
def world(pg_db, monkeypatch):
    db = pg_db
    db.execute("DELETE FROM alerts; DELETE FROM alert_rule_overrides; DELETE FROM alert_rule_exclusions; "
               "DELETE FROM alert_rules; DELETE FROM device_interfaces; DELETE FROM devices")
    dev = str(db.scalar(
        "INSERT INTO devices (hostname, ip_address, status, snmp_enabled, snmp_poll_interval, device_type)"
        " VALUES ('core-1', '10.0.0.1'::inet, 'up', TRUE, 60, 'switch') RETURNING id"))
    db.execute("INSERT INTO device_interfaces (device_id, if_index, if_name, if_speed, admin_status, oper_status,"
               " monitored, last_up_at) VALUES ($1, 1, 'Gi1/0/24', 1000000000, 'up', 'up', TRUE, now()),"
               " ($1, 2, 'Gi1/0/1', 1000000000, 'up', 'up', TRUE, now())", dev)
    monkeypatch.setattr(worker, "dispatch_to_channels", _no_dispatch)
    rows = {"rows": []}

    def query(sql, parameters=None, **_):
        since, until = parameters["since"].timestamp(), parameters["until"].timestamp()
        return NS(result_rows=[r for r in rows["rows"] if since <= r[2] <= until])

    from app.core import database
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: NS(query=query))
    return NS(db=db, dev=dev, rows=rows)


def add_rule(db, name, metric, operator, threshold, **cols):
    names = "".join(f", {c}" for c in cols)
    vals = "".join(f", ${i + 5}" for i in range(len(cols)))
    return str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, recovery_alert, notify_channels"
        f"{names}) VALUES ($1, $2, $3, $4, 'warning', true, '[]'::jsonb{vals}) RETURNING id",
        name, metric, operator, threshold, *cols.values()))


async def run(world, now):
    async with session(world.db) as s:
        return await worker.evaluate_network_rules(s, now=now)


def incidents(world, rule):
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in world.db.query(
        "SELECT status, metadata->>'if_index', message, metadata::text FROM alerts WHERE rule_id = $1"
        " ORDER BY triggered_at", rule)]


@pytest.mark.asyncio
async def test_flapping_interface_raises_one_incident_and_clears(world):
    flapping = _flapping_rows(until_min=45)
    steady = [_row(T0 + m * MIN, if_index=2, device=world.dev) for m in range(46)]
    world.rows["rows"] = [r[:0] + (world.dev,) + r[1:] for r in flapping] + steady
    rule = add_rule(world.db, "Interface flapping", "if_flaps", ">=", 5, min_duration=900)
    await run(world, T0 + 20 * MIN + 5)
    await run(world, T0 + 25 * MIN + 5)
    [(status, idx, message, meta)] = incidents(world, rule)
    assert (status, idx) == ("active", "1")
    assert message == ("Interface flapping: Interface Gi1/0/24 on Switch core-1 (10.0.0.1) changed state "
                       "6 times in 15 minutes; it is up now.")
    assert meta["flap_count"] == 6 and meta["flap_window_s"] == 900
    await run(world, T0 + 34 * MIN + 5)
    assert incidents(world, rule)[0][0] == "active"           # one change still in the window
    await run(world, T0 + 35 * MIN + 5)
    assert [i[0] for i in incidents(world, rule)] == ["resolved"]


@pytest.mark.asyncio
async def test_an_override_hold_is_the_flap_window_and_exclusions_still_apply(world):
    world.rows["rows"] = [r[:0] + (world.dev,) + r[1:] for r in _flapping_rows(until_min=25)]
    rule = add_rule(world.db, "Interface flapping", "if_flaps", ">=", 5, min_duration=900)
    # Over 3 minutes the port changed state at most 4 times: no incident.
    world.db.execute("INSERT INTO alert_rule_overrides (rule_id, device_id, reason, position, min_duration)"
                     " VALUES ($1, $2, 'Short window', 100, 180)", rule, world.dev)
    await run(world, T0 + 20 * MIN + 5)
    assert incidents(world, rule) == []
    world.db.execute("DELETE FROM alert_rule_overrides")
    world.db.execute("INSERT INTO alert_rule_exclusions (rule_id, asset_kind, device_id, reason)"
                     " VALUES ($1, 'device', $2, 'Lab port')",
                     rule, world.dev)
    await run(world, T0 + 20 * MIN + 5)
    assert incidents(world, rule) == []
    world.db.execute("DELETE FROM alert_rule_exclusions")
    await run(world, T0 + 20 * MIN + 5)
    assert len(incidents(world, rule)) == 1


@pytest.mark.asyncio
async def test_error_rate_rule_on_one_interface_names_the_rate_and_direction(world):
    busy = [_row(T0 + i * MIN, device=world.dev, errors=(3000 * i, 0), pkts=(97_000 * i, 50_000 * i))
            for i in range(14)]
    # The neighbour port counts 826 errors per poll among 5 million packets.
    quiet = [_row(T0 + i * MIN, device=world.dev, if_index=2, errors=(826 * i, 0),
                  pkts=(5_000_000 * i, 5_000_000 * i)) for i in range(14)]
    world.rows["rows"] = busy + quiet
    rule = add_rule(world.db, "Interface error rate high", "if_error_rate_pct", ">", 1, min_duration=600)
    await run(world, T0 + 13 * MIN)
    [(status, idx, message, meta)] = incidents(world, rule)
    assert (status, idx) == ("active", "1")
    assert message == (
        "Interface error rate high: core-1 (10.0.0.1) / Gi1/0/24 — Interface error rate 3% of packets "
        "(limit 1% of packets): inbound 3% (3000 errors in 97000 packets), outbound 0% "
        "(0 errors in 50000 packets) in the last 60 s")


@pytest.mark.asyncio
async def test_speed_change_raises_and_auto_clears(world):
    db = world.db
    world.rows["rows"] = [_row(T0 + m * MIN, device=world.dev) for m in range(40)]
    rule = add_rule(db, "Interface speed changed", "if_speed_changed", "==", 1)
    await run(world, T0 + 10 * MIN + 5)
    assert incidents(world, rule) == []
    # What the migrate-205 trigger records when the port renegotiates (see below).
    db.execute("UPDATE device_interfaces SET up_speed_bps = 100000000, previous_speed_bps = 1000000000,"
               " speed_changed_at = to_timestamp($1) WHERE device_id = $2 AND if_index = 1",
               T0 + 10 * MIN + 20, world.dev)
    await run(world, T0 + 10 * MIN + 30)
    [(status, idx, message, meta)] = incidents(world, rule)
    assert (status, idx) == ("active", "1")
    assert message == ("Interface speed changed: Interface Gi1/0/24 on Switch core-1 (10.0.0.1) now runs at "
                       "100 Mb/s (was 1 Gb/s).")
    assert (meta["speed_bps"], meta["previous_speed_bps"]) == (100_000_000, 1_000_000_000)
    await run(world, T0 + 24 * MIN)
    assert incidents(world, rule)[0][0] == "active"
    await run(world, T0 + 26 * MIN)
    assert [i[0] for i in incidents(world, rule)] == ["resolved"]


def test_migration_records_speed_changes_of_up_interfaces_only(world):
    db = world.db

    def poll(oper, speed):
        db.execute("INSERT INTO device_interfaces (device_id, if_index, if_name, if_speed, admin_status,"
                   " oper_status, monitored) VALUES ($1, 1, 'Gi1/0/24', $2, 'up', $3, TRUE)"
                   " ON CONFLICT (device_id, if_index) DO UPDATE SET if_speed = EXCLUDED.if_speed,"
                   " oper_status = EXCLUDED.oper_status", world.dev, speed, oper)
        return db.query("SELECT up_speed_bps, previous_speed_bps, speed_changed_at IS NOT NULL"
                        " FROM device_interfaces WHERE device_id = $1 AND if_index = 1", world.dev)[0]

    # The fixture row was inserted up at 1 Gb/s: that is the baseline, not a change.
    assert poll("up", 1_000_000_000) == (1_000_000_000, None, False)
    # Down links often report a default speed: never compared.
    assert poll("down", 10_000_000) == (1_000_000_000, None, False)
    assert poll("up", 1_000_000_000) == (1_000_000_000, None, False)
    assert poll("up", 100_000_000) == (100_000_000, 1_000_000_000, True)
    assert poll("up", 0) == (100_000_000, 1_000_000_000, True)      # no speed reported: kept
    # A row inserted down has no baseline; its first up poll sets one.
    db.execute("INSERT INTO device_interfaces (device_id, if_index, if_name, if_speed, oper_status)"
               " VALUES ($1, 9, 'Gi1/0/9', 10000000, 'down')", world.dev)
    db.execute("UPDATE device_interfaces SET oper_status = 'up', if_speed = 1000000000"
               " WHERE device_id = $1 AND if_index = 9", world.dev)
    assert db.query("SELECT up_speed_bps, speed_changed_at FROM device_interfaces"
                    " WHERE device_id = $1 AND if_index = 9", world.dev)[0] == (1_000_000_000, None)
    # The metric vocabulary accepts the new keys.
    for m in ("if_error_rate_pct", "if_discard_rate_pct", "if_flaps", "if_speed_changed"):
        add_rule(db, m, m, ">=", 1)


# ── Rule API and recommended rules ──────────────────────────────────────────

@pytest.fixture
def api(world):
    from fastapi.testclient import TestClient
    from app.core.database import get_db
    from app.core.security import get_current_user
    from app.main import app
    from test_monitoring_settings_policy import add_user
    admin = add_user(world.db)

    async def db_session():
        engine = create_async_engine(world.db.url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                yield s
        finally:
            await engine.dispose()

    async def signed_in():
        return admin

    app.dependency_overrides[get_db] = db_session
    app.dependency_overrides[get_current_user] = signed_in
    try:
        yield NS(client=TestClient(app), db=world.db)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


def test_rule_api_accepts_and_refuses_interface_quality_rules(api):
    base = {"severity": "warning", "notify_channels": []}
    ok = api.client.post("/api/v1/alert-rules", json={**base, "name": "Flapping", "metric": "if_flaps",
                                                      "operator": ">=", "threshold": 5, "min_duration": 900,
                                                      "if_role": "access"})
    assert ok.status_code in (200, 201), ok.text
    mixed = api.client.post("/api/v1/alert-rules", json={
        **base, "name": "Mixed", "metric": "if_flaps", "operator": ">=", "threshold": 5,
        "conditions": [{"metric": "if_flaps", "operator": ">=", "threshold": 5},
                       {"metric": "if_util_pct", "operator": ">", "threshold": 80}]})
    assert mixed.status_code == 422 and "rule of their own" in mixed.text
    rate = api.client.post("/api/v1/alert-rules", json={**base, "name": "Rate", "metric": "if_discard_rate_pct",
                                                        "operator": ">", "threshold": 0.5, "target": "Gi1/0/24"})
    assert rate.status_code in (200, 201), rate.text


def test_recommended_interface_rules_are_accepted(api):
    for key, metric, op, thr, hold in (("interface_error_rate", "if_error_rate_pct", ">", 1, 600),
                                       ("interface_flapping", "if_flaps", ">=", 5, 900)):
        r = api.client.post(f"/api/v1/alert-rules/recommended/{key}/accept", json={})
        assert r.status_code == 201, (key, r.text)
        [(m, o, t, h, prio, sev)] = api.db.query(
            "SELECT metric, operator, threshold, min_duration, priority, severity FROM alert_rules"
            " WHERE pack_key = $1", "default:" + key)
        assert (m, o, t, h, prio, sev) == (metric, op, thr, hold, "P3", "warning")
