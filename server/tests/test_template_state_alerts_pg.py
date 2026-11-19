"""State rules on device-template enum metrics (alerting plan G-3) on a disposable PostgreSQL.

* each tunnel / power supply is its own incident (rule + device + component), named
  with its row label, the template's metric name and the value label, and recovers
  on its own with "... is Up again";
* temperature rules evaluate each ENTITY-SENSOR-MIB sensor (temperature_<index>);
* the built-in templates recommend state rules (accept writes every condition, P2,
  2 minute hold, recovery on) and never recommend rules on FortiAPs / FortiSwitches;
* the API refuses unmapped and retired metric keys with the reason, keeps old rules
  on retired keys editable, and refuses a state code a template does not know.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401

T0 = 1_800_000_000.0
MANAGER = ["alerts.view", "alerts.manage", "devices.view", "devices.manage", "servers.view"]


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def builtin(db, key):
    return str(db.scalar("SELECT id FROM device_profiles WHERE builtin_key = $1", key))


def add_device(db, name, ip, profile, device_type):
    return str(db.scalar(
        "INSERT INTO devices (hostname, ip_address, status, snmp_enabled, snmp_poll_interval, profile_id, device_type)"
        " VALUES ($1, $2::inet, 'up', TRUE, 60, $3, $4) RETURNING id", name, ip, profile, device_type))


def add_rule(db, name, conditions, *, priority=None, clear_duration=0):
    c0 = conditions[0]
    return str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, conditions, condition_logic, severity,"
        " priority, min_duration, clear_duration, recovery_alert, notify_channels)"
        " VALUES ($1, $2, $3, $4, $5::jsonb, 'AND', 'critical', $6, 0, $7, true,"
        " '[\"00000000-0000-0000-0000-00000000c0de\"]'::jsonb) RETURNING id",
        name, c0["metric"], c0["operator"], c0["threshold"], json.dumps(conditions), priority, clear_duration))


def add_row(db, device, group, metric, instance, label):
    db.execute("INSERT INTO device_template_values (device_id, group_key, metric_key, instance, series_key, label)"
               " VALUES ($1, $2, $3, $4, $5, $6)", device, group, metric, instance, f"tpl_{metric}_{instance}", label)


def manager(db, **kw):
    kw.setdefault("permissions", MANAGER)
    return api_client(db, role=kw.pop("role", "rule-manager"), persist_user=True,
                      username=kw.pop("username", "noc-lead"), **kw)


@pytest.fixture
def world(pg_db):
    db = pg_db
    db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM device_template_values; "
               "DELETE FROM device_sensors; DELETE FROM device_entities; DELETE FROM devices")
    return NS(db=db, fgt=builtin(db, "fortinet_fortigate"), ios=builtin(db, "cisco_ios"))


def _capture(sent):
    async def dispatch(db, channels, payload, *a, **k):
        sent.append(payload)
        return 1
    return dispatch


def _series(values):
    """{series key: code} -> five fresh polls of each."""
    return {k: [(T0 + i * 60, v) for i in range(5)] for k, v in values.items()}


async def _pass(db, monkeypatch, history, now):
    from app.services import network_alert_service as worker
    from app.services import network_history
    monkeypatch.setattr(network_history, "fetch_history", lambda m, s, u: (history, {}))
    async with session(db) as s:
        return await worker.evaluate_network_rules(s, now=now)


@pytest.mark.asyncio
async def test_each_tunnel_is_its_own_incident_named_with_template_labels(world, monkeypatch):
    from app.services import network_alert_service as worker
    db = world.db
    fw = add_device(db, "FGT-01", "192.0.2.1", world.fgt, "firewall")
    for inst, label in (("1", "Branch-12"), ("2", "Branch-14"), ("3", "DC-Backup")):
        add_row(db, fw, "ipsec", "fgt_tun_status", inst, label)
    rule = add_rule(db, "Tunnel down", [dict(metric="tpl_fgt_tun_status", operator="==", threshold=1)])
    sent: list[dict] = []
    monkeypatch.setattr(worker, "dispatch_to_channels", _capture(sent))

    codes = {"tpl_fgt_tun_status_1": 1.0, "tpl_fgt_tun_status_2": 2.0, "tpl_fgt_tun_status_3": 1.0}
    out = await _pass(db, monkeypatch, {fw: _series(codes)}, T0 + 245)
    assert out["raised"] == 2
    rows = {json.loads(r[1])["if_index"]: (r[0], json.loads(r[1])) for r in db.query(
        "SELECT message, metadata::text FROM alerts WHERE rule_id = $1 AND status = 'active'", rule)}
    assert set(rows) == {"series:tpl_fgt_tun_status_1", "series:tpl_fgt_tun_status_3"}
    msg, meta = rows["series:tpl_fgt_tun_status_1"]
    assert msg == "Tunnel down: IPsec tunnel Branch-12 on Firewall FGT-01 (192.0.2.1) is Down."
    assert (meta["component"], meta["state"], meta["state_metric"]) == ("IPsec tunnel Branch-12", "Down", "Status")
    assert rows["series:tpl_fgt_tun_status_3"][0].endswith("IPsec tunnel DC-Backup on Firewall FGT-01 (192.0.2.1) is Down.")
    assert len(sent) == 2
    for p in sent:
        assert "tpl_" not in p["body"] and "tpl_" not in p["message"] and "tpl_" not in p["subject"]
        assert p["headline_metric"] == {"label": "Status", "value": "Down"}
        assert dict(p["details"])["Component"] in {"IPsec tunnel Branch-12", "IPsec tunnel DC-Backup"}
    assert {p["body"] for p in sent} == {
        "IPsec tunnel Branch-12 on Firewall FGT-01 (192.0.2.1) is Down.",
        "IPsec tunnel DC-Backup on Firewall FGT-01 (192.0.2.1) is Down."}

    # Branch-12 comes back; DC-Backup stays down; a missing poll never resolves.
    sent.clear()
    later = {"tpl_fgt_tun_status_1": [(T0 + 300 + i * 60, 2.0) for i in range(3)],
             "tpl_fgt_tun_status_2": [(T0 + 300 + i * 60, 2.0) for i in range(3)],
             "tpl_fgt_tun_status_3": [(T0 + 300 + i * 60, 1.0) for i in range(3)]}
    out = await _pass(db, monkeypatch, {fw: later}, T0 + 425)
    assert out["resolved"] == 1
    assert db.scalar("SELECT count(*) FROM alerts WHERE rule_id = $1 AND status = 'active'", rule) == 1
    assert len(sent) == 1 and sent[0]["is_recovery"]
    assert sent[0]["body"].startswith("IPsec tunnel Branch-12 on Firewall FGT-01 (192.0.2.1) is Up again. Lasted ")
    gone = {"tpl_fgt_tun_status_3": [(T0, 1.0)]}          # DC-Backup's poll is long stale
    await _pass(db, monkeypatch, {fw: gone}, T0 + 2000)
    assert db.scalar("SELECT count(*) FROM alerts WHERE rule_id = $1 AND status = 'active'", rule) == 1


@pytest.mark.asyncio
async def test_power_supply_state_rule_ignores_an_empty_bay(world, monkeypatch):
    from app.services import network_alert_service as worker
    db = world.db
    sw = add_device(db, "core-1", "192.0.2.2", world.ios, "switch")
    for inst, label in (("1", "PS1"), ("2", "PS2"), ("3", "PS3")):
        add_row(db, sw, "psu", "cisco_psu_state", inst, label)
    rule = add_rule(db, "PSU failed", [dict(metric="tpl_cisco_psu_state", operator=">=", threshold=3),
                                       dict(metric="tpl_cisco_psu_state", operator="!=", threshold=5)],
                    priority="P2")
    sent: list[dict] = []
    monkeypatch.setattr(worker, "dispatch_to_channels", _capture(sent))
    codes = {"tpl_cisco_psu_state_1": 1.0, "tpl_cisco_psu_state_2": 3.0, "tpl_cisco_psu_state_3": 5.0}
    await _pass(db, monkeypatch, {sw: _series(codes)}, T0 + 245)
    rows = db.query("SELECT message, priority FROM alerts WHERE rule_id = $1", rule)
    assert [tuple(r) for r in rows] == [("PSU failed: Power supply PS2 on Switch core-1 (192.0.2.2) is Critical.", "P2")]
    assert sent[0]["body"] == "Power supply PS2 on Switch core-1 (192.0.2.2) is Critical."


@pytest.mark.asyncio
async def test_temperature_rule_evaluates_each_sensor(world, monkeypatch):
    from app.services import network_alert_service as worker
    db = world.db
    sw = add_device(db, "core-2", "192.0.2.3", world.ios, "switch")
    for idx, name in ((5, "Inlet"), (6, "Hotspot")):
        db.execute("INSERT INTO device_sensors (device_id, sensor_index, sensor_type) VALUES ($1, $2, 'celsius')", sw, idx)
        db.execute("INSERT INTO device_entities (device_id, ent_index, name) VALUES ($1, $2, $3)", sw, idx, name)
    rule = add_rule(db, "Hot", [dict(metric="temperature", operator=">", threshold=70)])
    monkeypatch.setattr(worker, "dispatch_to_channels", _capture([]))
    await _pass(db, monkeypatch, {sw: _series({"temperature_5": 41.0, "temperature_6": 78.0})}, T0 + 245)
    rows = [(json.loads(r[0]), r[1]) for r in db.query(
        "SELECT metadata::text, message FROM alerts WHERE rule_id = $1", rule)]
    assert len(rows) == 1
    meta, msg = rows[0]
    assert meta["if_index"] == "series:temperature_6" and meta["component"] == "Temperature sensor Hotspot"
    assert "core-2 (192.0.2.3) / Temperature sensor Hotspot" in msg


def test_builtin_templates_recommend_state_rules_and_nothing_for_managed_children(world):
    db = world.db
    with manager(db) as c:
        fgt = {i["key"]: i for i in c.get(f"/api/v1/snmp/profiles/{world.fgt}/recommended").json()["items"]}
        assert fgt["fgt_ha_sync:state"]["priority"] == "P2" and fgt["fgt_tun_status:state"]["priority"] == "P3"
        for key in ("fgt_ap_cpu:crit", "fgt_ap_cpu:warn", "fgt_ap_mem:crit", "fgt_sw_cpu:crit",
                    "fgt_sw_mem:warn", "fgt_ap_status:state", "fgt_sw_status:state"):
            assert key not in fgt, key
        assert not [k for k in fgt if k.startswith(("fgt_ap_", "fgt_sw_", "fgt_swp_", "fgt_radio"))]
        assert db.scalar("SELECT count(*) FROM alert_rules") == 0          # recommendations only

        r = c.post(f"/api/v1/snmp/profiles/{world.ios}/recommended/cisco_psu_state:state/accept", json={})
        assert r.status_code == 201, r.text
        rule = r.json()
        assert [(x["operator"], x["threshold"]) for x in rule["conditions"]] == [(">=", 3.0), ("!=", 5.0)]
        assert (rule["min_duration"], rule["severity"], rule["priority"], rule["recovery_alert"]) == \
            (120, "critical", "P2", True)
        ios = {i["key"]: i for i in c.get(f"/api/v1/snmp/profiles/{world.ios}/recommended").json()["items"]}
        assert ios["cisco_psu_state:state"]["status"] == "accepted"
        assert ios["cisco_fan_state:state"]["priority"] == "P3"


def test_api_refuses_unmapped_and_retired_keys_with_the_reason(world):
    db = world.db
    with manager(db) as c:
        def create(metric, operator=">", threshold=1, **extra):
            return c.post("/api/v1/alert-rules", json={"name": f"r-{metric}", "metric": metric,
                                                        "operator": operator, "threshold": threshold, **extra})
        r = create("psu_state", "==", 0)
        assert r.status_code == 422 and "tpl_cisco_psu_state" in r.json()["detail"]
        r = create("jitter", ">", 30)
        assert r.status_code == 422 and "do not measure jitter" in r.json()["detail"]
        r = create("session_count", ">", 1000)
        assert r.status_code == 422 and "tpl_fgt_sessions" in r.json()["detail"]
        assert create("temperature", ">", 70).status_code == 201
        r = create("tpl_fgt_tun_status", "==", 7)
        assert r.status_code == 422 and "1 = Down, 2 = Up" in r.json()["detail"], r.text
        assert create("tpl_fgt_tun_status", "==", 1).status_code == 201

        # An old jitter rule keeps loading and stays editable.
        old = add_rule(db, "Old jitter", [dict(metric="jitter", operator=">", threshold=30)])
        r = c.put(f"/api/v1/alert-rules/{old}", json={"name": "Old jitter (kept)", "threshold": 40,
                                                     "conditions": [{"metric": "jitter", "operator": ">", "threshold": 40}]})
        assert r.status_code == 200, r.text
        r = c.put(f"/api/v1/alert-rules/{old}", json={"conditions": [
            {"metric": "jitter", "operator": ">", "threshold": 40},
            {"metric": "session_count", "operator": ">", "threshold": 1}]})
        assert r.status_code == 422
        assert c.get("/api/v1/alert-rules").status_code == 200
