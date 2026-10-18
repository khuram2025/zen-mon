"""GET /devices/{id}/summary and GET /devices/{id}/interfaces on the real schema (disposable Postgres).

The unit tests in test_device_summary.py fake the database; this runs every query of the summary against the
migrated schema: template values, the chassis row, the normalised identity, interfaces with the ever-up baseline,
open alerts (incidents only), active maintenance and flap episodes, plus scope (404) and the alerts permission.
ClickHouse is a fake that answers nothing; nothing here reaches an installed database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db, pg_db_module  # noqa: F401  (fixtures)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FG = uuid.uuid4()
OTHER = uuid.uuid4()
PROFILE = uuid.uuid4()

TEMPLATE = [
    {"key": "system", "name": "System Health", "kind": "scalar", "metrics": [
        {"key": "fgt_cpu", "name": "CPU Usage", "type": "gauge", "unit": "%", "thresholds": {"warn": 80, "crit": 90}},
        {"key": "fgt_fw_version", "name": "Firmware", "type": "string"}]},
    {"key": "ha", "name": "High Availability", "kind": "scalar", "metrics": [
        {"key": "fgt_ha_mode", "name": "HA Mode", "type": "enum",
         "labels": {"1": {"text": "Standalone", "sev": "info"}, "3": {"text": "Active-Passive", "sev": "ok"}}}]},
    {"key": "ha_members", "name": "HA Members", "kind": "table", "metrics": [
        {"key": "fgt_ha_sync", "name": "Sync Status", "type": "enum",
         "labels": {"0": {"text": "Not Synchronized", "sev": "crit"}, "1": {"text": "Synchronized", "sev": "ok"}}}]},
    {"key": "ipsec", "name": "IPsec Tunnels", "kind": "table", "metrics": [
        {"key": "fgt_tun_status", "name": "Status", "type": "enum",
         "labels": {"1": {"text": "Down", "sev": "crit"}, "2": {"text": "Up", "sev": "ok"}}}]},
]


@pytest.fixture(scope="module")
def world(pg_db_module):
    db = pg_db_module
    db.execute("INSERT INTO device_profiles (id, name, vendor, oid_groups) VALUES ($1, 'Test FortiGate', 'Fortinet', $2::jsonb)",
               PROFILE, TEMPLATE)
    for dev, host, ip, tags, status in ((FG, "fg-101f", "10.20.0.1", ["team-a"], "up"),
                                        (OTHER, "other-fw", "10.20.0.2", ["team-b"], "down")):
        db.execute("INSERT INTO devices (id, hostname, ip_address, tags, status, ping_enabled, ping_interval, snmp_enabled,"
                   " profile_id, vendor, model, status_changed_at, device_type)"
                   " VALUES ($1, $2, $3::inet, $4::jsonb, $5, true, 60, true, $6, 'Fortinet', 'FGT_101F', $7, 'firewall')",
                   dev, host, ip, tags, status, PROFILE, NOW - timedelta(days=2))

    def val(group, metric, num=None, text="", instance="", label=""):
        db.execute("INSERT INTO device_template_values (device_id, group_key, metric_key, instance, series_key, label,"
                   " value_num, value_text, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                   FG, group, metric, instance, f"tpl_{metric}_{instance}", label, num, text, NOW)
    val("system", "fgt_cpu", 13)
    val("system", "fgt_fw_version", text="v7.6.7,build3704,260601 (GA.M)")
    val("ha", "fgt_ha_mode", 1)
    val("ha_members", "fgt_ha_sync", 0, instance="1", label="FG101F")
    val("ipsec", "fgt_tun_status", 1, instance="1", label="ACC_To_Amman")
    val("ipsec", "fgt_tun_status", 2, instance="2", label="ACC_To_Riyadh")
    db.execute("INSERT INTO device_entities (device_id, ent_index, parent_index, class, name, serial_number, model_name)"
               " VALUES ($1, 1, 0, 'chassis', 'FortiGate', 'FG101FTK23015380', 'FGT_101F')", FG)
    for idx, name, admin, oper, up in ((1, "wan1", "up", "up", True), (2, "port3", "up", "down", False),
                                       (3, "port4", "up", "down", True), (4, "dmz", "down", "down", True)):
        db.execute("INSERT INTO device_interfaces (device_id, if_index, if_name, if_descr, if_alias, if_type, if_speed,"
                   " admin_status, oper_status, monitored, last_up_at)"
                   " VALUES ($1, $2, $3, $3, '', 6, 1000000000, $4, $5, true, $6)",
                   FG, idx, name, admin, oper, NOW - timedelta(hours=1) if up else None)
    db.execute("INSERT INTO alerts (id, device_id, status, severity, message, triggered_at, metadata)"
               " VALUES ($1, $2, 'active', 'critical', '[ZenPlus CRITICAL] Tunnel rule fired', $3, '{}'::jsonb)",
               uuid.uuid4(), FG, NOW - timedelta(minutes=5))
    # A recovery notice is not an incident and never an issue.
    db.execute("INSERT INTO alerts (id, device_id, status, severity, message, triggered_at, metadata)"
               " VALUES ($1, $2, 'active', 'info', 'Recovered', $3, '{\"is_recovery\": \"true\"}'::jsonb)",
               uuid.uuid4(), FG, NOW - timedelta(minutes=1))
    db.execute("INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at, reason)"
               " VALUES ('device', $1, $2, $3, 'Firmware upgrade')", FG, NOW - timedelta(minutes=10), NOW + timedelta(hours=1))
    db.execute("INSERT INTO device_flap_episodes (device_id, started_at, ended_at, kind, start_changes, window_minutes,"
               " status_at_start, status_at_end, end_reason) VALUES ($1, $2, $3, 'reachability', 4, 30, 'up', 'up', 'stable')",
               FG, NOW - timedelta(days=1), NOW - timedelta(days=1) + timedelta(minutes=30))
    return db


@pytest.fixture(autouse=True)
def no_clickhouse(monkeypatch):
    from app.core import database
    from app.services import metric_service
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: SimpleNamespace(query=lambda *a, **k: SimpleNamespace(result_rows=[])))
    monkeypatch.setattr(metric_service, "get_clickhouse_client", lambda: SimpleNamespace(query=lambda *a, **k: SimpleNamespace(result_rows=[])), raising=False)


def test_summary_runs_on_the_real_schema(world):
    with api_client(world, role="t_sum_all", permissions=["devices.view", "alerts.view"]) as c:
        r = c.get(f"/api/v1/devices/{FG}/summary")
    assert r.status_code == 200, r.text[:500]
    body = r.json()
    assert body["status"] == "up"
    assert body["identity"]["os_version"] == {"value": "7.6.7 build 3704", "source": "template"}
    assert body["identity"]["serial_number"] == {"value": "FG101FTK23015380", "source": "entity"}
    assert [g["key"] for g in body["not_applicable"]] == ["ha_members"]
    titles = [i["title"] for i in body["issues"]]
    assert "IPsec Tunnels: ACC_To_Amman Down" in titles and "Tunnel rule fired" in titles
    assert "Recovered" not in titles
    assert not any("HA Members" in t for t in titles)
    assert body["issues"][-1]["source"] == "maintenance" and body["maintenance"]["active"] is True
    ifs = body["interfaces"]
    assert (ifs["up"], ifs["unexpected_down"], ifs["unused"], ifs["admin_down"]) == (1, 1, 1, 1)
    assert ifs["unexpected"][0]["name"] == "port4"
    assert body["alerts"]["active"] == 1
    assert len(body["recent"]["flap_episodes"]) == 1
    assert body["kpis"]["cpu_thresholds"] == {"warn": 80, "crit": 90}


def test_summary_respects_scope_and_the_alerts_permission(world):
    with api_client(world, role="t_sum_team_a", permissions=["devices.view"], scope_tags=("team-a",)) as c:
        assert c.get(f"/api/v1/devices/{OTHER}/summary").status_code == 404
        own = c.get(f"/api/v1/devices/{FG}/summary")
    assert own.status_code == 200
    assert own.json()["alerts"] is None
    assert not any(i["source"] == "alert" for i in own.json()["issues"])


def test_interfaces_report_the_ever_up_baseline(world):
    with api_client(world, role="t_sum_all2", permissions=["devices.view"]) as c:
        rows = c.get(f"/api/v1/devices/{FG}/interfaces").json()
    by = {r["if_name"]: r for r in rows}
    assert by["port3"]["ever_up"] is False and by["port4"]["ever_up"] is True and by["port4"]["last_up_at"]
