"""Device page Phase 3 routes on the real schema (disposable Postgres, migrate-165 included):
owner and notes, server-saved widget layouts, the Related panel and condition-named alert titles in the summary.
ClickHouse is faked; nothing here reaches an installed database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db, pg_db_module  # noqa: F401  (fixtures)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FG = uuid.uuid4()          # the device under test (team-a)
CTRL = uuid.uuid4()        # its managing controller (team-a)
AP = uuid.uuid4()          # a device it manages (team-a)
UPLINK = uuid.uuid4()      # upstream dependency (team-a)
SIBLING = uuid.uuid4()     # changed status at the same time (team-a)
HIDDEN = uuid.uuid4()      # changed at the same time but out of team-a's scope
MAP = uuid.uuid4()


@pytest.fixture(scope="module")
def world(pg_db_module):
    db = pg_db_module
    rows = ((FG, "fg-101f", "10.30.0.1", ["team-a"], "up", None),
            (CTRL, "fmg", "10.30.0.2", ["team-a"], "up", None),
            (AP, "ap-7", None, ["team-a"], "down", FG),
            (UPLINK, "core-sw", "10.30.0.4", ["team-a"], "up", None),
            (SIBLING, "branch-rtr", "10.30.0.5", ["team-a"], "down", None),
            (HIDDEN, "other-team", "10.30.0.6", ["team-b"], "down", None))
    for dev, host, ip, tags, status, managed_by in rows:
        db.execute("INSERT INTO devices (id, hostname, ip_address, tags, status, ping_enabled, ping_interval,"
                   " snmp_enabled, device_type, vendor, managed_by_device_id, poll_mode)"
                   " VALUES ($1, $2, $3::inet, $4::jsonb, $5, true, 60, false, 'firewall', 'Fortinet', $6, $7)",
                   dev, host, ip, tags, status, managed_by, "via_controller" if managed_by else "direct")
    db.execute("UPDATE devices SET managed_by_device_id = $1 WHERE id = $2", CTRL, FG)
    db.execute("INSERT INTO topology_dependencies (parent_device_id, child_device_id, dependency_type)"
               " VALUES ($1, $2, 'uplink')", UPLINK, FG)
    db.execute("INSERT INTO manual_maps (id, name) VALUES ($1, 'Riyadh core')", MAP)
    nodes = {}
    for dev in (FG, UPLINK, HIDDEN):
        nodes[dev] = uuid.uuid4()
        db.execute("INSERT INTO manual_map_nodes (id, map_id, device_id) VALUES ($1, $2, $3)", nodes[dev], MAP, dev)
    db.execute("INSERT INTO manual_map_links (map_id, source_node_id, target_node_id) VALUES ($1, $2, $3)",
               MAP, nodes[FG], nodes[UPLINK])
    db.execute("INSERT INTO manual_map_links (map_id, source_node_id, target_node_id) VALUES ($1, $2, $3)",
               MAP, nodes[HIDDEN], nodes[FG])
    db.execute("INSERT INTO alerts (id, device_id, status, severity, message, triggered_at, metadata)"
               " VALUES ($1, $2, 'active', 'warning', 'Packet loss 33% (limit 10%) — fg-101f (10.30.0.1)', $3,"
               " '{\"new_status\": \"degraded\", \"condition\": \"Packet loss 33% (limit 10%)\"}'::jsonb)",
               uuid.uuid4(), FG, NOW - timedelta(minutes=3))
    db.execute("INSERT INTO alerts (id, device_id, status, severity, message, triggered_at, metadata)"
               " VALUES ($1, $2, 'active', 'warning', 'High Latency: fg-101f (10.30.0.1) is DEGRADED', $3,"
               " '{\"new_status\": \"degraded\"}'::jsonb)",
               uuid.uuid4(), FG, NOW - timedelta(minutes=30))
    return db


@pytest.fixture(autouse=True)
def fake_clickhouse(monkeypatch):
    from app.core import database
    from app.services import device_related, metric_service
    empty = lambda: SimpleNamespace(query=lambda *a, **k: SimpleNamespace(result_rows=[]))  # noqa: E731
    monkeypatch.setattr(database, "get_clickhouse_client", empty)
    monkeypatch.setattr(metric_service, "get_clickhouse_client", empty, raising=False)
    t0 = int((NOW - timedelta(hours=2)).timestamp() * 1000)

    def rows(sql, params):
        if "device_id != " in sql:
            return [(str(SIBLING), t0 + 40_000, "up", "down"), (str(HIDDEN), t0 - 10_000, "up", "down"),
                    (str(CTRL), t0 + 600_000, "up", "down")]
        return [(t0, "up", "down")]
    monkeypatch.setattr(device_related, "_ch_rows", rows)


def test_owner_and_notes_are_saved_and_blank_clears(world):
    with api_client(world, role="t_p3_mgr", permissions=["devices.view", "devices.manage"]) as c:
        r = c.put(f"/api/v1/devices/{FG}", json={"owner": "  NOC team ", "notes": "Circuit RUH-4471. Reboot after 22:00."})
        assert r.status_code == 200, r.text[:300]
        assert (r.json()["owner"], r.json()["notes"]) == ("NOC team", "Circuit RUH-4471. Reboot after 22:00.")
        assert c.get(f"/api/v1/devices/{FG}").json()["owner"] == "NOC team"
        cleared = c.put(f"/api/v1/devices/{FG}", json={"owner": "   "}).json()
        assert cleared["owner"] is None and cleared["notes"] == "Circuit RUH-4471. Reboot after 22:00."
    with api_client(world, role="t_p3_view", permissions=["devices.view"]) as c:
        assert c.put(f"/api/v1/devices/{FG}", json={"owner": "me"}).status_code == 403


def test_widget_layouts_are_per_user_device_and_tab(world):
    items = [{"id": "cpu", "x": 0, "y": 0, "w": 6, "h": 4}, {"id": "absent-widget", "x": 6, "y": 0, "w": 6, "h": 4}]
    with api_client(world, role="t_p3_lay", permissions=["devices.view"], username="layout-a", persist_user=True) as c:
        base = f"/api/v1/devices/{FG}/widget-layouts"
        assert c.get(base, params={"vendor": "Fortinet", "device_type": "firewall"}).json() == {
            "device": {}, "template": {}, "template_key": {"vendor": "fortinet", "device_type": "firewall"}}
        # Saving one's own layout needs only devices.view.
        r = c.put(f"{base}/overview", json={"scope": "device", "items": items})
        assert r.status_code == 200, r.text[:300]
        c.put(f"{base}/overview", json={"scope": "device", "items": items[:1]})          # replaces, no duplicate
        c.put(f"{base}/metrics", json={"scope": "device", "items": []})                  # "standard layout" override
        r = c.put(f"{base}/overview", json={"scope": "template", "vendor": " FORTINET ", "device_type": "Firewall",
                                            "items": items})
        assert r.status_code == 200
        assert c.put(f"{base}/overview", json={"scope": "template", "vendor": "unknown", "device_type": "firewall",
                                               "items": []}).status_code == 400
        assert c.put(f"{base}/Bad Section", json={"items": []}).status_code == 400
        got = c.get(base, params={"vendor": "Fortinet", "device_type": "firewall"}).json()
        assert got["device"]["overview"]["items"] == items[:1]
        assert got["device"]["metrics"]["items"] == []
        assert got["template"]["overview"]["items"] == items   # the absent widget keeps its entry
        # The template applies to every device of the vendor/type for this user.
        other = c.get(f"/api/v1/devices/{CTRL}/widget-layouts", params={"vendor": "fortinet", "device_type": "firewall"}).json()
        assert other["device"] == {} and other["template"]["overview"]["items"] == items
        assert c.delete(f"{base}/overview").json() == {"deleted": 1}
        assert "overview" not in c.get(base).json()["device"]
    with api_client(world, role="t_p3_lay", permissions=["devices.view"], username="layout-b", persist_user=True) as c:
        assert c.get(f"/api/v1/devices/{FG}/widget-layouts", params={"vendor": "Fortinet", "device_type": "firewall"}).json()["template"] == {}
    with api_client(world, role="t_p3_lay_b", permissions=["devices.view"], scope_tags=("team-b",),
                    username="layout-c", persist_user=True) as c:
        assert c.put(f"/api/v1/devices/{FG}/widget-layouts/overview", json={"items": []}).status_code == 404


def test_related_panel(world):
    with api_client(world, role="t_p3_rel", permissions=["devices.view", "maps.view"], scope_tags=("team-a",)) as c:
        r = c.get(f"/api/v1/devices/{FG}/related")
    assert r.status_code == 200, r.text[:500]
    body = r.json()
    assert body["parent"]["hostname"] == "fmg"
    assert body["children"]["total"] == 1 and body["children"]["by_status"] == {"down": 1}
    assert [d["hostname"] for d in body["dependencies"]["upstream"]] == ["core-sw"]
    assert body["dependencies"]["upstream"][0]["dependency_type"] == "uplink"
    assert body["maps"]["maps"] == [{"id": str(MAP), "name": "Riyadh core"}]
    assert [d["hostname"] for d in body["maps"]["neighbours"]] == ["core-sw"]      # other-team is out of scope
    same = body["same_time"]
    assert same["window_s"] == 120 and len(same["transitions"]) == 1
    # Within ±2 min and in scope only: the controller changed 10 minutes later, other-team is team-b's.
    assert [(d["hostname"], d["matches"][0]["offset_s"]) for d in same["devices"]] == [("branch-rtr", 40)]
    assert body["unavailable"] == []


def test_related_without_maps_permission(world):
    with api_client(world, role="t_p3_rel2", permissions=["devices.view"]) as c:
        body = c.get(f"/api/v1/devices/{FG}/related").json()
    assert body["maps"] is None
    assert {d["hostname"] for d in body["same_time"]["devices"]} == {"branch-rtr", "other-team"}


def test_summary_alert_titles_name_the_condition(world):
    with api_client(world, role="t_p3_sum", permissions=["devices.view", "alerts.view"]) as c:
        body = c.get(f"/api/v1/devices/{FG}/summary").json()
    titles = [i["title"] for i in body["issues"] if i["source"] == "alert"]
    assert titles == ["High Latency · Degraded", "Packet loss 33% (limit 10%)"]


def test_summary_groups_repeated_alerts(world):
    """D-09: repeats of one condition from one rule are one issue with a count, and one summary group
    listing every alert; another condition, and an alert without a rule, stay separate."""
    db = world
    dev = uuid.uuid4()
    db.execute("INSERT INTO devices (id, hostname, ip_address, tags, status, ping_enabled, ping_interval,"
               " snmp_enabled, device_type, vendor, poll_mode)"
               " VALUES ($1, 'rpt-fw', '10.30.0.9'::inet, '[\"team-a\"]'::jsonb, 'degraded', true, 60, false,"
               " 'firewall', 'Fortinet', 'direct')", dev)
    rule = db.scalar("INSERT INTO alert_rules (name, metric, operator, threshold, trigger_on)"
                     " VALUES ('rpt High Latency', 'ping_status', 'eq', 0, 'any') RETURNING id")
    ids = []
    for hours, loss, metric in ((70, 33, "packet_loss"), (40, 41, "packet_loss"), (2, 38, "packet_loss"),
                                (1, None, "rtt")):
        title = f"Packet loss {loss}% (limit 10%)" if loss else "Latency 412 ms (limit 100 ms)"
        ids.append(db.scalar(
            "INSERT INTO alerts (rule_id, device_id, status, severity, message, triggered_at, metadata)"
            " VALUES ($1, $2, 'active', 'warning', $3, $4, $5::jsonb) RETURNING id",
            rule, dev, f"{title} — rpt-fw (10.30.0.9)", NOW - timedelta(hours=hours),
            {"new_status": "degraded", "condition": title, "condition_metric": metric}))
    db.execute("INSERT INTO alerts (device_id, status, severity, message, triggered_at)"
               " VALUES ($1, 'active', 'warning', 'Unruled', $2)", dev, NOW - timedelta(hours=3))
    with api_client(world, role="t_p3_grp", permissions=["devices.view", "alerts.view"]) as c:
        body = c.get(f"/api/v1/devices/{dev}/summary").json()
    alert_issues = [i for i in body["issues"] if i["source"] == "alert"]
    assert [(i["title"], i.get("count")) for i in alert_issues] == [
        ("Degraded (packet loss)", 3), ("Unruled", None), ("Latency 412 ms (limit 100 ms)", None)]
    loss = alert_issues[0]
    assert loss["detail"] == "latest: Packet loss 38% (limit 10%)"
    assert loss["alert_id"] == str(ids[2]), "the newest member"
    assert loss["rule_id"] == str(rule)
    assert loss["since"] < loss["last_at"]
    groups = body["alerts"]["groups"]
    assert [g["count"] for g in groups] == [3, 1, 1]
    assert [m["id"] for m in groups[0]["members"]] == [str(ids[2]), str(ids[1]), str(ids[0])]
    assert body["alerts"]["active"] == 5, "counts are per alert, not per group"
    keys = {a["group_key"] for a in body["alerts"]["items"]}
    assert f"{rule}|packet_loss|degraded|" in keys and None in keys
