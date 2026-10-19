"""/oncall/coverage paging semantics and built-in alert routing APIs (1.24.21),
through the real app routes on a disposable PostgreSQL.

* ``paging``/``reason`` per priority: a P4 without a default policy, or with
  a zero-step policy, is "not paged (by design)" — no gap, not unrouted; a P1
  that pages nobody keeps its gap (it has a response target);
* ``builtin_sources``: routed = family enabled + a default policy for its
  priority, paged = that policy has a step; ``issues`` lists enabled P1–P3
  families that are not routed, and routed families that page nobody when
  their priority is P1/P2 or has coverage gaps;
* GET/PUT /alerts/builtin-routing: alerts.view reads, alerts.manage writes,
  unknown keys are a 422 with a loc path, changes are audited.
"""
from __future__ import annotations

import pytest

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db_module  # noqa: F401  (fixtures)


@pytest.fixture(scope="module")
def world(pg_db_module):
    db = pg_db_module
    db.execute("DELETE FROM escalation_policies; DELETE FROM system_settings WHERE key = 'builtin_alert_routing'")
    noc = str(db.scalar("INSERT INTO notification_channels (name, type, config) VALUES ('NOC', 'email',"
                        " $1::jsonb) RETURNING id", {"recipients": "noc@example.test"}))
    return {"db": db, "noc": noc}


def manager(db):
    return api_client(db, role="t_cov_manager", permissions=["alerts.view", "alerts.manage"],
                      username="cov-manager", persist_user=True)


def viewer(db):
    return api_client(db, role="t_cov_viewer", permissions=["alerts.view"], username="cov-viewer",
                      persist_user=True)


def policy(db, name, prios, *, steps=(), ack_target=None):
    pid = str(db.scalar("INSERT INTO escalation_policies (name, default_priorities, ack_target_minutes,"
                        " fallback_targets) VALUES ($1, $2::varchar[], $3, '[]'::jsonb) RETURNING id",
                        name, list(prios), ack_target))
    for i, targets in enumerate(steps):
        db.execute("INSERT INTO escalation_steps (policy_id, position, delay_minutes, name, targets)"
                   " VALUES ($1, $2, $3, 'Step', $4::jsonb)", pid, i, i * 5, targets)
    return pid


def test_not_paged_by_design_versus_gaps(world):
    db, noc = world["db"], world["noc"]
    db.execute("DELETE FROM escalation_policies")
    with manager(db) as c:
        cov = c.get("/api/v1/oncall/coverage").json()
        p4 = cov["priorities"]["P4"]
        assert (p4["paging"], p4["reason"], p4["gaps"], p4["steps_ok"]) == ("none", "no_policy", [], True)
        assert "P4" not in cov["unrouted_priorities"]
        p1 = cov["priorities"]["P1"]
        assert (p1["paging"], p1["reason"], p1["steps_ok"]) == ("none", "no_policy", False)
        assert p1["gaps"] and "P1" in cov["unrouted_priorities"]
        assert "acknowledgement target" in p1["gaps"][0]
        assert cov["unrouted_priorities"] == ["P1", "P2", "P3"]        # P3 has its 60-minute target

        # The recommended P4 policy: no steps, no target -> not paged, not "covered".
        policy(db, "P4 audit only", ["P4"])
        # A P1 policy without steps keeps a gap: P1 has an acknowledgement target.
        policy(db, "P1 empty", ["P1"])
        policy(db, "P2 paging", ["P2"], steps=[[{"type": "channel", "id": noc}]])
        cov = c.get("/api/v1/oncall/coverage").json()
        p4 = cov["priorities"]["P4"]
        assert (p4["paging"], p4["reason"], p4["gaps"], p4["steps_ok"], p4["steps"]) == (
            "none", "no_steps", [], True, 0)
        assert p4["policy_name"] == "P4 audit only"
        p1 = cov["priorities"]["P1"]
        assert (p1["paging"], p1["reason"], p1["steps_ok"]) == ("none", "no_steps", False)
        assert any("nobody is paged" in g for g in p1["gaps"]) and p1["response_target_minutes"] == 5
        p2 = cov["priorities"]["P2"]
        assert (p2["paging"], p2["reason"], p2["steps_ok"], p2["steps"]) == ("steps", None, True, 1)
        assert cov["unrouted_priorities"] == ["P3"]

        # A P4 response target makes paging nobody a gap again.
        c.put("/api/v1/alerts/response-targets", json={"P4": 240})
        p4 = c.get("/api/v1/oncall/coverage").json()["priorities"]["P4"]
        assert p4["steps_ok"] is False and p4["gaps"]
        c.put("/api/v1/alerts/response-targets", json={"P4": None})


def test_builtin_sources_routed_paged_and_issues(world):
    db, noc = world["db"], world["noc"]
    db.execute("DELETE FROM escalation_policies")
    policy(db, "P2 paging", ["P2"], steps=[[{"type": "channel", "id": noc}]])
    policy(db, "P4 audit only", ["P4"])
    with manager(db) as c:
        cov = c.get("/api/v1/oncall/coverage").json()
        src = {s["key"]: s for s in cov["builtin_sources"]}
        assert set(src) == {"agent_offline", "sensor_offline", "sensor_degraded", "ncm_backup_failed",
                            "ncm_config_changed", "baseline_drift", "health_threshold"}
        a = src["agent_offline"]
        assert (a["priority"], a["enabled"], a["routed"], a["paged"], a["policy_name"]) == (
            "P2", True, True, True, "P2 paging")
        b = src["baseline_drift"]
        assert (b["priority"], b["routed"], b["paged"]) == ("P4", True, False)
        assert "no steps" in b["note"]
        d = src["sensor_degraded"]
        assert (d["priority"], d["enabled"], d["routed"], d["paged"]) == ("P3", True, False, False)
        h = src["health_threshold"]
        assert (h["enabled"], h["routed"], h["default_enabled"]) == (False, False, False)
        assert [(v["severity"], v["priority"]) for v in h["variants"]] == [
            ("critical", "P3"), ("warning", "P4"), ("info", "P4")]
        assert "NCM" in src["ncm_backup_failed"]["note"]
        n = src["ncm_config_changed"]
        assert (n["enabled"], n["routed"], n["default_enabled"]) == (False, False, False)
        assert "never resolves" in n["note"]
        issues = {i["source"]: i for i in cov["issues"]}
        # Enabled P1-P3 families without a route: sensor degraded and backup failed (P3).
        assert set(issues) == {"sensor_degraded", "ncm_backup_failed"}
        assert issues["sensor_degraded"]["severity"] == "warning"

        # Disabling a family takes it out of the issues; enabling health with
        # no P3 policy adds it.
        r = c.put("/api/v1/alerts/builtin-routing",
                  json={"families": {"sensor_degraded": False, "health_threshold": True}})
        assert r.status_code == 200, r.text
        cov = c.get("/api/v1/oncall/coverage").json()
        assert {i["source"] for i in cov["issues"]} == {"ncm_backup_failed", "health_threshold"}
        c.put("/api/v1/alerts/builtin-routing", json={"families": {"sensor_degraded": True,
                                                                     "health_threshold": False}})


def test_builtin_routed_but_nobody_paged_is_an_issue_for_p1_p2_or_gapped_priorities(world):
    """Routed to a zero-step policy: a P2 family (agent offline) and a P3
    family whose priority has gaps (P3 keeps its 60-minute acknowledgement
    target) are issues; a P4 family without gaps stays by design."""
    db = world["db"]
    db.execute("DELETE FROM escalation_policies")
    p2 = policy(db, "P2 empty", ["P2"])
    policy(db, "P3 empty", ["P3"])
    policy(db, "P4 audit only", ["P4"])
    with manager(db) as c:
        cov = c.get("/api/v1/oncall/coverage").json()
        assert cov["priorities"]["P3"]["gaps"], "P3 has an acknowledgement target"
        src = {s["key"]: s for s in cov["builtin_sources"]}
        assert (src["agent_offline"]["routed"], src["agent_offline"]["paged"]) == (True, False)
        assert "not a design choice" in src["agent_offline"]["note"]
        assert "not a design choice" not in src["baseline_drift"]["note"]
        issues = {i["source"]: i for i in cov["issues"]}
        assert set(issues) == {"agent_offline", "sensor_offline", "sensor_degraded", "ncm_backup_failed"}
        a = issues["agent_offline"]
        assert (a["severity"], a["policy_id"]) == ("danger", p2)
        assert "routed to P2 empty, which has no steps, so nobody is paged" in a["text"]
        assert issues["sensor_degraded"]["severity"] == "warning"
    db.execute("DELETE FROM escalation_policies")


def test_builtin_routing_setting_api(world):
    db = world["db"]
    with viewer(db) as c:
        r = c.get("/api/v1/alerts/builtin-routing")
        assert r.status_code == 200
        body = r.json()
        assert "health_threshold" not in body["enabled_families"] and "agent_offline" in body["enabled_families"]
        assert "ncm_config_changed" not in body["enabled_families"]
        fam = {f["key"]: f for f in body["families"]}
        assert fam["health_threshold"]["enabled"] is False and fam["health_threshold"]["min_age_minutes"] == 5
        assert fam["agent_offline"]["priority"] == "P2" and fam["agent_offline"]["label"] == "Agent offline"
        assert c.put("/api/v1/alerts/builtin-routing", json={"enabled_families": []}).status_code == 403
    audits = "SELECT count(*) FROM audit_logs WHERE action = 'alert.builtin_routing.update'"
    before = db.scalar(audits)
    with manager(db) as c:
        r = c.put("/api/v1/alerts/builtin-routing", json={"enabled_families": ["agent_offline", "bogus"]})
        assert r.status_code == 422
        err, = r.json()["detail"]
        assert err["loc"] == ["body", "enabled_families", 1] and "'bogus'" in err["msg"]
        assert c.put("/api/v1/alerts/builtin-routing", json={}).status_code == 422
        r = c.put("/api/v1/alerts/builtin-routing", json={"enabled_families": ["sensor_offline", "agent_offline"]})
        assert r.status_code == 200 and r.json()["enabled_families"] == ["agent_offline", "sensor_offline"]
        r = c.put("/api/v1/alerts/builtin-routing", json={"families": {"baseline_drift": True,
                                                                         "agent_offline": False}})
        assert r.json()["enabled_families"] == ["sensor_offline", "baseline_drift"]
        assert db.scalar(audits) == before + 2
        c.put("/api/v1/alerts/builtin-routing", json={"enabled_families": [
            "agent_offline", "sensor_offline", "sensor_degraded", "ncm_backup_failed", "ncm_config_changed",
            "baseline_drift"]})
