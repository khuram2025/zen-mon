"""Escalation matrix, on-call and contact-method APIs on a disposable PostgreSQL.

Round trips through the real app routes (route policy, role lookup, scope):

* /oncall/schedules CRUD, timeline, now, overrides, coverage; a schedule an
  escalation policy uses cannot be deleted;
* /escalation-policies CRUD and validation (step order, unknown targets, one
  default policy per priority), the read-only dry runs (alerts.view, masked
  addresses), /recommended (the report's four policies);
* /users/me/contact-methods and /users/{id}/contact-methods, users.timezone;
* /alerts/response-targets with P-keys; alert rules carry priority and
  escalation_policy_id; /alerts/{id}/route scope-checked like the detail;
* permissions: alerts.view reads, alerts.manage writes, nothing without them.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db_module  # noqa: F401  (fixtures)

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture(scope="module")
def world(pg_db_module):
    db = pg_db_module
    db.execute("DELETE FROM escalation_policies; DELETE FROM oncall_schedules")

    def user(name, active=True):
        return str(db.scalar("INSERT INTO users (username, email, password_hash, full_name, role, is_active)"
                             " VALUES ($1, $2, '!', $3, 'operator', $4) RETURNING id",
                             name, f"{name}@example.test", name.title(), active))

    users = {n: user(n) for n in ("aisha", "omar", "lena")}
    users["gone"] = user("gone", active=False)
    noc = str(db.scalar("INSERT INTO notification_channels (name, type, config) VALUES ('NOC mail', 'email',"
                        " $1::jsonb) RETURNING id", {"recipients": "noc@example.test"}))
    for tag in ("team-a", "team-b"):
        db.execute("INSERT INTO devices (hostname, ip_address, tags) VALUES ($1, $2::inet, $3::jsonb)",
                   f"core-{tag}", "10.70.0.1" if tag == "team-a" else "10.70.0.2", [tag])
    return {"db": db, "users": users, "noc": noc}


def admin(db, **kw):
    return api_client(db, role="admin", username="esc-admin", persist_user=True, **kw)


def manager(db):
    return api_client(db, role="t_esc_manager", permissions=["alerts.view", "alerts.manage"],
                      username="esc-manager", persist_user=True)


def viewer(db, scope=()):
    return api_client(db, role="t_esc_viewer", permissions=["alerts.view"], scope_tags=scope,
                      username="esc-viewer", persist_user=True)


def nobody(db):
    return api_client(db, role="t_esc_none", permissions=["devices.view"], username="esc-none")


def layer(members, **kw):
    return {"name": "Weekly", "shift_hours": 168, "handoff_at": (NOW - timedelta(days=1)).isoformat(),
            "members": members, **kw}


def test_schedule_round_trip_timeline_overrides(world):
    db, u = world["db"], world["users"]
    with manager(db) as c:
        r = c.post("/api/v1/oncall/schedules", json={"name": "Bad zone", "timezone": "Mars/Base"})
        assert r.status_code == 422 and "IANA" in r.json()["detail"]
        r = c.post("/api/v1/oncall/schedules", json={"name": "x", "layers": [layer([str(uuid.uuid4())])]})
        assert r.status_code == 422
        r = c.post("/api/v1/oncall/schedules", json={
            "name": "Primary NOC", "timezone": "Asia/Riyadh",
            "layers": [layer([u["aisha"], u["omar"]]),
                       layer([u["lena"]], name="Nights", position=5,
                             restrict_to=[{"days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "06:00"}])]})
        assert r.status_code == 201, r.text
        s = r.json()
        sid = s["id"]
        assert s["timezone"] == "Asia/Riyadh" and [lv["position"] for lv in s["layers"]] == [0, 5]
        assert s["layers"][0]["member_names"] == ["Aisha", "Omar"] and s["now"]["users"]
        assert c.post("/api/v1/oncall/schedules", json={"name": "primary noc"}).status_code == 409

        tl = c.get(f"/api/v1/oncall/schedules/{sid}/timeline").json()
        assert tl["timezone"] == "Asia/Riyadh" and tl["shifts"]
        assert {sh["layer"] for sh in tl["shifts"]} == {"Weekly", "Nights"}
        assert c.get(f"/api/v1/oncall/schedules/{sid}/timeline",
                     params={"from": NOW.isoformat(), "to": (NOW + timedelta(days=90)).isoformat()}).status_code == 422

        start, end = NOW + timedelta(hours=1), NOW + timedelta(hours=3)
        r = c.post(f"/api/v1/oncall/schedules/{sid}/overrides",
                   json={"user_id": u["lena"], "starts_at": start.isoformat(), "ends_at": end.isoformat(),
                         "reason": "Holiday cover"})
        assert r.status_code == 201, r.text
        oid = r.json()["id"]
        assert c.post(f"/api/v1/oncall/schedules/{sid}/overrides",
                      json={"user_id": u["gone"], "starts_at": start.isoformat(),
                            "ends_at": end.isoformat()}).status_code == 422
        tl = c.get(f"/api/v1/oncall/schedules/{sid}/timeline",
                   params={"from": start.isoformat(), "to": end.isoformat()}).json()
        assert [(sh["user_name"], sh["override"]) for sh in tl["shifts"]] == [("Lena", True)]
        assert c.get(f"/api/v1/oncall/schedules/{sid}").json()["overrides"][0]["reason"] == "Holiday cover"
        assert c.delete(f"/api/v1/oncall/schedules/{sid}/overrides/{oid}").json() == {"deleted": True}

        now = c.get(f"/api/v1/oncall/schedules/{sid}/now").json()
        assert now["source"] == "layer" and now["users"][0]["name"] in ("Aisha", "Omar", "Lena")

        r = c.put(f"/api/v1/oncall/schedules/{sid}", json={"name": "Primary NOC", "timezone": "Europe/London",
                                                          "layers": [layer([u["omar"]])]})
        assert r.status_code == 200 and r.json()["timezone"] == "Europe/London"
        assert len(r.json()["layers"]) == 1
    with viewer(db) as c:
        assert c.get("/api/v1/oncall/schedules").status_code == 200
        assert c.post("/api/v1/oncall/schedules", json={"name": "Nope"}).status_code == 403
        assert c.delete(f"/api/v1/oncall/schedules/{sid}").status_code == 403
    with nobody(db) as c:
        assert c.get("/api/v1/oncall/schedules").status_code == 403
        assert c.get("/api/v1/oncall/coverage").status_code == 403


def _policy(name, targets, **kw):
    return {"name": name, "default_priorities": kw.pop("prio", []),
            "steps": [{"delay_minutes": 0, "name": "First", "targets": targets}], **kw}


def test_policy_crud_validation_and_dry_runs(world):
    db, u, noc = world["db"], world["users"], world["noc"]
    with manager(db) as c:
        sid = c.post("/api/v1/oncall/schedules", json={"name": "Policy schedule",
                                                         "layers": [layer([u["aisha"]])]}).json()["id"]
        tgt = [{"type": "schedule", "id": sid}, {"type": "channel", "id": noc}]
        r = c.post("/api/v1/escalation-policies", json={**_policy("P2 policy", tgt, prio=["P2"]),
                   "steps": [{"delay_minutes": 0, "targets": tgt},
                             {"delay_minutes": 15, "targets": [{"type": "user", "id": u["omar"]}]}],
                   "review_minutes": 30, "repeat_minutes": 10, "max_repeats": 1,
                   "fallback_targets": [{"type": "channel", "id": noc}]})
        assert r.status_code == 201, r.text
        pol = r.json()
        assert pol["default_priorities"] == ["P2"] and [s["delay_minutes"] for s in pol["steps"]] == [0, 15]
        assert pol["used_by_rules"] == 0
        pid = pol["id"]

        bad = [
            ({**_policy("x", tgt), "steps": [{"delay_minutes": 15, "targets": tgt},
                                              {"delay_minutes": 5, "targets": tgt}]}, 422, "increase"),
            (_policy("x", [{"type": "user", "id": str(uuid.uuid4())}]), 422, "Unknown escalation targets"),
            (_policy("x", tgt, prio=["P2"]), 409, "P2 is already the default of P2 policy"),
            (_policy("P2 POLICY", tgt), 409, "already exists"),
            ({**_policy("x", tgt), "steps": [{"delay_minutes": i, "targets": tgt} for i in range(11)]}, 422,
             "At most 10"),
        ]
        for body, code, text_ in bad:
            r = c.post("/api/v1/escalation-policies", json=body)
            assert r.status_code == code and text_ in r.json()["detail"], (body, r.text)
        # A disabled policy may share a default priority; it routes nothing.
        assert c.post("/api/v1/escalation-policies",
                      json={**_policy("Spare P2", tgt, prio=["P2"]), "enabled": False}).status_code == 201

        r = c.put(f"/api/v1/escalation-policies/{pid}", json={**_policy("P2 policy", tgt, prio=["P2", "P3"]),
                                                              "ack_expiry_minutes": 120})
        assert r.status_code == 200 and r.json()["default_priorities"] == ["P2", "P3"]
        assert r.json()["ack_expiry_minutes"] == 120 and len(r.json()["steps"]) == 1
        assert c.get(f"/api/v1/escalation-policies/{uuid.uuid4()}").status_code == 404
        assert c.delete(f"/api/v1/oncall/schedules/{sid}").status_code == 409     # in use

    with viewer(db) as c:
        assert [p["name"] for p in c.get("/api/v1/escalation-policies").json()["items"]][:1]
        r = c.post(f"/api/v1/escalation-policies/{pid}/preview", json={"priority": "P2"})
        assert r.status_code == 200, r.text
        prev = r.json()
        step0 = prev["steps"][0]
        assert {x["via"] for x in step0["recipients"]} == {"schedule:Policy schedule", "channel:NOC mail"}
        assert "a***@example.test" in {x["address"] for x in step0["recipients"]}      # masked for viewers
        assert prev["review"] is None and prev["fallback"] == []                  # PUT replaced them
        r = c.post("/api/v1/escalation-policies/dry-run", json={
            "priority": "P1", "policy": {"name": "Draft", "steps": [
                {"delay_minutes": 0, "targets": [{"type": "user", "id": u["gone"]}]}]}})
        assert r.status_code == 200
        dry = r.json()
        assert dry["steps"][0]["uses_fallback"] and "Gone is not an active user." in dry["steps"][0]["gaps"]
        assert any("No fallback route" in w for w in dry["warnings"])
        assert c.post("/api/v1/escalation-policies", json=_policy("v", tgt)).status_code == 403
        assert c.delete(f"/api/v1/escalation-policies/{pid}").status_code == 403
    with nobody(db) as c:
        assert c.post(f"/api/v1/escalation-policies/{pid}/preview", json={}).status_code == 403
    with admin(db) as c:
        r = c.post(f"/api/v1/escalation-policies/{pid}/preview", json={"priority": "P2"})
        assert "aisha@example.test" in {x["address"] for x in r.json()["steps"][0]["recipients"]}
        assert c.delete(f"/api/v1/escalation-policies/{pid}").json()["deleted"] is True


def test_recommended_matrix_and_coverage(world):
    db, u, noc = world["db"], world["users"], world["noc"]
    with manager(db) as c:
        primary = c.post("/api/v1/oncall/schedules", json={"name": "Rec primary",
                                                              "layers": [layer([u["aisha"]])]}).json()["id"]
        backup = c.post("/api/v1/oncall/schedules", json={"name": "Rec backup",
                                                             "layers": [layer([u["omar"]])]}).json()["id"]
        c.post("/api/v1/oncall/schedules", json={"name": "Empty rota", "layers": [layer([])]})
        assert c.post("/api/v1/escalation-policies/recommended", json={}).status_code == 422
        body = {"primary_schedule_id": primary, "backup_schedule_id": backup, "lead_user_id": u["lena"],
                "noc_channel_ids": [noc], "team_channel_ids": []}
        r = c.post("/api/v1/escalation-policies/recommended", json=body)
        if r.status_code == 409:                      # an earlier test left a default behind
            r = c.post("/api/v1/escalation-policies/recommended", json={**body, "replace_defaults": True})
        assert r.status_code == 201, r.text
        items = r.json()["items"]
        assert [p["default_priorities"] for p in items] == [["P1"], ["P2"], ["P3"], ["P4"]]
        p1 = items[0]
        assert [s["delay_minutes"] for s in p1["steps"]] == [0, 5, 15, 30]
        assert (p1["ack_target_minutes"], p1["review_minutes"]) == (5, 15)
        assert items[3]["steps"] == []
        assert c.post("/api/v1/escalation-policies/recommended", json=body).status_code == 409
        again = c.post("/api/v1/escalation-policies/recommended", json={**body, "replace_defaults": True})
        assert again.status_code == 201 and again.json()["items"][0]["name"].endswith("(2)")

        cov = c.get("/api/v1/oncall/coverage").json()
        assert cov["unrouted_priorities"] == []
        assert cov["priorities"]["P1"]["steps_ok"] is True
        assert "Empty rota" in {s["name"] for s in cov["schedules_without_members"]}


def test_contact_methods_and_timezone(world):
    db, u = world["db"], world["users"]
    with viewer(db) as c:
        me = c.get("/api/v1/users/me/contact-methods").json()
        assert me["methods"] == [] and me["implicit_email"] is True
        r = c.put("/api/v1/users/me/contact-methods", json={
            "timezone": "Asia/Riyadh",
            "methods": [{"kind": "sms", "address": "+966 50 123 4567", "label": "Mobile"},
                        {"kind": "email", "address": "pager@example.test"}]})
        assert r.status_code == 200, r.text
        assert r.json()["timezone"] == "Asia/Riyadh" and r.json()["implicit_email"] is False
        for bad in ({"methods": [{"kind": "sms", "address": "call me"}]},
                    {"methods": [{"kind": "email", "address": "a@x.test"}, {"kind": "email", "address": "A@x.test"}]},
                    {"methods": [], "timezone": "Nowhere/City"}):
            assert c.put("/api/v1/users/me/contact-methods", json=bad).status_code == 422
        # Somebody else's needs alerts.manage or users.manage.
        assert c.get(f"/api/v1/users/{u['omar']}/contact-methods").status_code == 403
    with manager(db) as c:
        r = c.put(f"/api/v1/users/{u['omar']}/contact-methods",
                  json={"methods": [{"kind": "sms", "address": "+15550100"}]})
        assert r.status_code == 200 and r.json()["methods"][0]["address"] == "+15550100"
        assert r.json()["timezone"] is None                                         # absent = unchanged
        assert c.get(f"/api/v1/users/{uuid.uuid4()}/contact-methods").status_code == 404
    with admin(db) as c:
        r = c.put(f"/api/v1/users/{u['lena']}", json={"timezone": "Europe/London"})
        assert r.status_code == 200 and r.json()["timezone"] == "Europe/London"
        assert c.put(f"/api/v1/users/{u['lena']}", json={"timezone": "Mars/Base"}).status_code == 422


def test_response_targets_rules_and_alert_route(world):
    db, noc = world["db"], world["noc"]
    with manager(db) as c:
        t = c.get("/api/v1/alerts/response-targets").json()
        assert {k: t[k] for k in ("P1", "P2", "P3", "P4")} == {"P1": 5, "P2": 15, "P3": 60, "P4": None}
        r = c.put("/api/v1/alerts/response-targets", json={"P1": 10, "P4": 240})
        assert r.json()["P1"] == 10 and r.json()["P2"] == 15 and r.json()["P4"] == 240
        r = c.put("/api/v1/alerts/response-targets", json={"warning": 45})               # legacy key
        assert r.json()["P3"] == 45 and r.json()["warning"] == 45 and r.json()["P1"] == 10

        pid = c.post("/api/v1/escalation-policies",
                     json=_policy("Route test", [{"type": "channel", "id": noc}])).json()["id"]
        base = {"name": "Core down", "metric": "ping_status", "operator": "==", "threshold": 0,
                "severity": "critical", "notify_channels": []}
        assert c.post("/api/v1/alert-rules", json={**base, "escalation_policy_id": str(uuid.uuid4())}
                      ).status_code == 422
        assert c.post("/api/v1/alert-rules", json={**base, "priority": "P7"}).status_code == 422
        rule = c.post("/api/v1/alert-rules", json={**base, "priority": "P1", "escalation_policy_id": pid}).json()
        assert (rule["priority"], rule["effective_priority"], rule["escalation_policy_id"]) == ("P1", "P1", pid)
        rule2 = c.put(f"/api/v1/alert-rules/{rule['id']}", json={"priority": None, "escalation_policy_id": None}).json()
        assert (rule2["priority"], rule2["effective_priority"], rule2["escalation_policy_id"]) == (None, "P2", None)
        c.put(f"/api/v1/alert-rules/{rule['id']}", json={"escalation_policy_id": pid})
        host = c.post("/api/v1/host-alert-rules", json={"name": "Mem", "metric": "host_memory_pct",
                                                         "threshold": 90, "priority": "P2",
                                                         "escalation_policy_id": pid})
        assert host.status_code == 200 and host.json()["escalation_policy_id"] == pid
        assert c.put(f"/api/v1/host-alert-rules/{host.json()['id']}",
                     json={"escalation_policy_id": str(uuid.uuid4())}).status_code == 422
        assert c.get(f"/api/v1/escalation-policies/{pid}").json()["used_by_rules"] == 2

    def alert(hostname):
        dev = db.scalar("SELECT id FROM devices WHERE hostname = $1", hostname)
        return str(db.scalar("INSERT INTO alerts (device_id, rule_id, severity, priority, message, status,"
                             " metadata) VALUES ($1, $2, 'critical', 'P1', 'down', 'active', '{}'::jsonb)"
                             " RETURNING id", dev, rule["id"]))

    mine, theirs = alert("core-team-a"), alert("core-team-b")
    with viewer(db, scope=("team-a",)) as c:
        r = c.get(f"/api/v1/alerts/{mine}/route")
        assert r.status_code == 200, r.text
        route = r.json()
        assert route["policy"] == {"id": pid, "name": "Route test", "source": "rule"}
        assert route["priority"] == "P1" and route["state"]["next_action"] == "step:0"
        assert c.get(f"/api/v1/alerts/{theirs}/route").status_code == 404                # out of scope
        detail = c.get(f"/api/v1/alerts/{mine}").json()
        assert detail["priority"] == "P1" and detail["priority_label"] == "P1 Critical"
        assert detail["ack_due_at"] is not None                                          # P1 target: 10 min
    with nobody(db) as c:
        assert c.get(f"/api/v1/alerts/{mine}/route").status_code == 403


def test_direct_pages_without_a_usable_gateway_are_a_coverage_gap(world):
    db, u = world["db"], world["users"]
    db.execute("DELETE FROM alert_rules; DELETE FROM escalation_policies; DELETE FROM notification_gateways")
    people = [{"type": "user", "id": u["lena"]}]
    with manager(db) as c:
        r = c.post("/api/v1/escalation-policies", json=_policy("Lead only", people, prio=["P1"]))
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        cov = c.get("/api/v1/oncall/coverage").json()
        p1 = cov["priorities"]["P1"]
        assert any("No usable SMTP or SMS gateway for direct pages" in g for g in p1["gaps"])
        assert p1["steps_ok"] is False and cov["direct_page_gateways"] == {"email": False, "sms": False}
        prev = c.post(f"/api/v1/escalation-policies/{pid}/preview", json={"priority": "P1"}).json()
        assert any("No usable SMTP or SMS gateway for direct pages" in w for w in prev["warnings"])
        # One enabled SMTP gateway, not flagged default, carries the direct email pages.
        db.execute("INSERT INTO notification_gateways (name, type, config, is_default, enabled)"
                   " VALUES ('mail', 'smtp', '{}'::jsonb, false, true)")
        cov = c.get("/api/v1/oncall/coverage").json()
        assert cov["direct_page_gateways"] == {"email": True, "sms": False}
        assert not any("gateway" in g for g in cov["priorities"]["P1"]["gaps"])
        assert cov["priorities"]["P1"]["steps_ok"] is True
        prev = c.post(f"/api/v1/escalation-policies/{pid}/preview", json={"priority": "P1"}).json()
        assert not any("gateway" in w for w in prev["warnings"])
