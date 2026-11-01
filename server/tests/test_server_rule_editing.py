"""Editing server (host_*) rules from Alert Manager.

The alert wizard now opens a server rule on its Server tab and saves it through
/host-alert-rules, so that API has to carry what the wizard edits:

* the scope: one server, or (explicit null) every server;
* the notification schedule, which host_alert_service already honours;
* /alert-rules lists ``server_id``, so the wizard knows which server a rule
  watches instead of showing (and saving) it as "all".

Round trips through the real app routes against a disposable database.
"""

from __future__ import annotations

import pytest

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db_module  # noqa: F401  (fixtures)


@pytest.fixture(scope="module")
def world(pg_db_module):
    db = pg_db_module
    sid = str(db.scalar("INSERT INTO servers (display_name, hostname, collection_mode) "
                        "VALUES ('pay-db-03', 'pay-db-03', 'agent') RETURNING id"))
    return {"db": db, "server": sid}


def admin(db):
    return api_client(db, role="admin", username="rule-admin", persist_user=True)


def listed(c, rule_id):
    return next(r for r in c.get("/api/v1/alert-rules").json()["data"] if r["id"] == rule_id)


def test_agent_offline_rule_edits_keep_its_metric_and_scope(world):
    db, sid = world["db"], world["server"]
    with admin(db) as c:
        r = c.post("/api/v1/host-alert-rules", json={
            "name": "Agent offline", "metric": "host_agent_offline", "operator": "gt", "threshold": 0,
            "severity": "warning", "recovery_alert": True, "priority": "P3"})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        row = listed(c, rid)
        assert (row["metric"], row["server_id"], row["schedule_days"]) == ("host_agent_offline", None, [])

        # What the wizard sends for "Specific server" with a silence of 4 minutes and office-hours paging.
        r = c.put(f"/api/v1/host-alert-rules/{rid}", json={
            "name": "Agent offline — pay-db-03", "metric": "host_agent_offline", "operator": "gt", "threshold": 0,
            "min_duration": 240, "server_id": sid, "schedule_start": "07:00", "schedule_end": "19:00",
            "schedule_days": [5, 1, 1, 2], "reset_threshold": None, "clear_duration": None, "target": None})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["server_id"], body["min_duration"]) == (sid, 240)
        assert (body["schedule_start"], body["schedule_end"], body["schedule_days"]) == ("07:00", "19:00", [1, 2, 5])
        row = listed(c, rid)
        assert row["server_id"] == sid, "Alert Manager knows which server the rule watches"
        assert (row["schedule_start"], row["schedule_days"]) == ("07:00", [1, 2, 5])
        assert row["metric"] == "host_agent_offline"

        # Explicit null moves it back to every server; an empty day list means every day.
        r = c.put(f"/api/v1/host-alert-rules/{rid}", json={"server_id": None, "schedule_start": None,
                                                           "schedule_end": None, "schedule_days": None})
        assert r.status_code == 200, r.text
        assert (r.json()["server_id"], r.json()["schedule_start"], r.json()["schedule_days"]) == (None, None, [])
        assert db.scalar("SELECT server_id FROM alert_rules WHERE id = $1", rid) is None


def test_server_rule_create_with_scope_and_schedule(world):
    db, sid = world["db"], world["server"]
    with admin(db) as c:
        r = c.post("/api/v1/host-alert-rules", json={
            "name": "SQL stopped", "metric": "host_service_down", "target": "MSSQLSERVER", "server_id": sid,
            "severity": "critical", "schedule_start": "00:00", "schedule_end": "23:59", "schedule_days": [1, 2, 3, 4, 5]})
        assert r.status_code == 200, r.text
        row = listed(c, r.json()["id"])
        assert (row["server_id"], row["target"], row["schedule_days"]) == (sid, "MSSQLSERVER", [1, 2, 3, 4, 5])


@pytest.mark.parametrize("bad", [{"schedule_start": "25:00"}, {"schedule_end": "7pm"}, {"schedule_days": [0]},
                                 {"schedule_days": [8]}])
def test_server_rule_schedule_is_validated(world, bad):
    with admin(world["db"]) as c:
        r = c.post("/api/v1/host-alert-rules", json={"name": "CPU", "metric": "host_cpu_pct", "threshold": 90, **bad})
        assert r.status_code == 422, r.text


def test_device_rule_schedule_saves(world):
    """The wizard's Schedule step on any other rule: "Notify from/until" used to fail every save with a 500
    (asyncpg will not bind the text "08:00" to a time column)."""
    with admin(world["db"]) as c:
        r = c.post("/api/v1/alert-rules", json={"name": "Office hours", "metric": "ping_status", "operator": "==",
                                                "threshold": 0, "trigger_on": "down", "schedule_start": "08:00",
                                                "schedule_end": "18:00", "schedule_days": [1, 2, 3, 4, 5]})
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        assert (r.json()["schedule_start"], r.json()["schedule_end"]) == ("08:00", "18:00")
        r = c.put(f"/api/v1/alert-rules/{rid}", json={"schedule_start": "09:30", "schedule_end": None})
        assert r.status_code == 200, r.text
        assert (r.json()["schedule_start"], r.json()["schedule_end"]) == ("09:30", None)
        assert c.put(f"/api/v1/alert-rules/{rid}", json={"schedule_start": "9am"}).status_code == 422


def test_device_rules_list_without_a_server(world):
    with admin(world["db"]) as c:
        r = c.post("/api/v1/alert-rules", json={"name": "Down", "metric": "ping_status", "operator": "==",
                                                "threshold": 0, "trigger_on": "down"})
        assert r.status_code == 201, r.text
        assert listed(c, r.json()["id"])["server_id"] is None
        # A device rule is not editable through the server API.
        assert c.put(f"/api/v1/host-alert-rules/{r.json()['id']}", json={"name": "x"}).status_code == 404
