"""Alert rule authoring checks (1.24.21) through the real routes on a
disposable PostgreSQL, plus the pure value-domain / warning helpers.

* value domains: ping_status > 5, if_oper_status > 7, cpu > 150 and
  host_cpu_pct > 150 are 422s with a loc path and a readable message; a
  condition that is always true is rejected too;
* a ping_status condition is rewritten to what trigger_on implies (down ->
  == 0, up/degraded -> == 1), never refused; an old-wizard 'up' row with
  ping_status == 0 duplicates and updates and is stored as == 1;
* a server (host_*) metric on /alert-rules is a 422 at body.metric naming the
  server-rule editor — never the metric regex — and a server rule's
  definition cannot be changed through /alert-rules;
* every other pattern mismatch reads "<field>: '<value>' is not an accepted
  value" with its loc kept (global RequestValidationError handler);
* /host-alert-rules: target required (422 at body.target) on create and on
  update, finite thresholds, and a seeded rule round-trips through PUT;
* name/description warnings in create/update responses and from
  POST /alert-rules/preview-warnings;
* every seeded rule passes validation without warnings.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services import alert_rule_checks as checks
from app.services import network_conditions as nc
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db_module  # noqa: F401  (fixtures)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def db(pg_db_module):
    return pg_db_module


def manager(db):
    return api_client(db, role="t_rule_manager", permissions=["alerts.view", "alerts.manage"],
                      username="rule-manager", persist_user=True)


def viewer(db):
    return api_client(db, role="t_rule_viewer", permissions=["alerts.view"], username="rule-viewer",
                      persist_user=True)


BASE = {"name": "Rule", "severity": "warning", "notify_channels": []}


def _only(r, status=422):
    assert r.status_code == status, r.text
    assert "should match pattern" not in r.text and "^(" not in r.text
    return r.json()["detail"]


# ── Pure helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("metric,op,thr,never", [
    ("ping_status", ">", 5, True), ("ping_status", "<", 0, True), ("ping_status", "==", 7, True),
    ("ping_status", ">=", 0, False), ("if_oper_status", ">", 7, True), ("if_oper_status", "==", 9, True),
    ("cpu", ">", 150, True), ("host_cpu_pct", "gt", 150, True), ("rtt", "<", 0, True),
    ("apm_error_rate", ">", 1, True), ("packet_loss", ">", 100, True),
])
def test_domain_problem_never_true(metric, op, thr, never):
    msg = nc.domain_problem(metric, op, thr)
    if never:
        assert msg and "can never be true" in msg
    else:
        assert msg and "is always true" in msg


@pytest.mark.parametrize("metric,op,thr", [
    ("ping_status", "==", 0), ("ping_status", "eq", 1), ("if_oper_status", "==", 2), ("if_oper_status", "!=", 1),
    ("uptime_reset", "==", 1), ("cpu", ">", 90), ("cpu", ">=", 100), ("packet_loss", ">=", 100),
    ("host_filesystem_pct", "gt", 85), ("rtt", ">", 0), ("temperature", ">", 500), ("tpl_x", ">", -5),
])
def test_domain_problem_satisfiable(metric, op, thr):
    assert nc.domain_problem(metric, op, thr) is None


def test_domain_messages_name_the_values():
    assert nc.domain_problem("ping_status", ">", 5) == \
        "ping_status is 0 (down) or 1 (up); '> 5' can never be true"
    assert nc.domain_problem("packet_loss", ">=", 0) == \
        "packet_loss is a percentage from 0 to 100; '>= 0' is always true"
    assert nc.trigger_problem("up", "ping_status", "==", 0).startswith("trigger_on 'up' fires on ping_status 1")
    assert nc.trigger_problem("down", "ping_status", "==", 0) is None
    assert nc.trigger_problem("any", "ping_status", "==", 1) is None


@pytest.mark.parametrize("rule,field,fragment", [
    ({"name": "High CPU", "metric": "memory", "operator": ">", "threshold": 90}, "name", "mentions CPU"),
    ({"name": "High CPU usage", "metric": "host_memory_pct", "operator": "gt", "threshold": 90}, "name",
     "mentions CPU"),
    ({"name": "CPU", "description": "CPU above 90%", "metric": "cpu", "operator": ">", "threshold": 80},
     "description", "says 90%, but the threshold is 80"),
    ({"name": "High latency", "metric": "rtt", "operator": "<", "threshold": 100}, "name", "says high/above"),
    ({"name": "Low memory", "metric": "memory", "operator": ">", "threshold": 90}, "name", "says low/below"),
    ({"name": "Device down", "metric": "ping_status", "operator": "==", "threshold": 1, "trigger_on": "up"},
     "name", "says down"),
    ({"name": "Device recovered", "metric": "ping_status", "operator": "==", "threshold": 0}, "name",
     "says up/recovered"),
    ({"name": "Server offline", "metric": "host_cpu_pct", "operator": "gt", "threshold": 90}, "name",
     "says down/offline"),
])
def test_name_warnings(rule, field, fragment):
    warnings = checks.name_warnings(rule)
    assert any(w["field"] == field and fragment in w["message"] for w in warnings), warnings


@pytest.mark.parametrize("rule", [
    {"name": "Low disk space", "metric": "host_filesystem_pct", "operator": "gt", "threshold": 90},
    {"name": "WAN link down", "metric": "ping_status", "operator": "==", "threshold": 0},
    {"name": "Filesystem filling up", "metric": "host_filesystem_pct", "operator": "gt", "threshold": 85},
    {"name": "CPU and memory", "conditions": [{"metric": "cpu", "operator": ">", "threshold": 90},
                                             {"metric": "memory", "operator": ">", "threshold": 90}]},
    {"name": "Interface down", "metric": "if_oper_status", "operator": "==", "threshold": 2},
    {"name": "Template rule CPU", "metric": "tpl_fgt_cpu", "operator": ">", "threshold": 5},
])
def test_ordinary_names_have_no_warnings(rule):
    assert checks.name_warnings(rule) == []


def _seed_device_rules() -> list[dict]:
    sql = (SCRIPTS / "seed-devices.sql").read_text()
    rows = re.findall(r"\('([^']+)', '([^']+)', '(\w+)', '(\w+)', (\d+), \d+, '(\w+)', \d+, admin_id\)", sql)
    assert len(rows) >= 3
    return [{"name": n, "description": d, "metric": m, "operator": o, "threshold": float(t),
             "trigger_on": "any"} for n, d, m, o, t, _ in rows]


def test_every_seeded_rule_passes_validation_without_warnings(db):
    rows = [{"name": r[0], "description": r[1], "metric": r[2], "operator": r[3], "threshold": r[4],
             "trigger_on": r[5], "conditions": r[6]} for r in db.query(
        "SELECT name, description, metric, operator, threshold::float8, trigger_on, conditions FROM alert_rules")]
    assert len(rows) >= 12                                    # migrate-035 + migrate-037 seeds
    for rule in rows + _seed_device_rules():
        items = rule.get("conditions") or [{"metric": rule["metric"], "operator": rule["operator"],
                                        "threshold": rule["threshold"]}]
        assert checks.condition_errors(items, trigger_on=rule["trigger_on"],
                                       flat=not rule.get("conditions")) == [], rule["name"]
        assert checks.name_warnings(rule) == [], (rule["name"], checks.name_warnings(rule))


# ── /alert-rules ─────────────────────────────────────────────────────────────

def test_value_domains_are_422_with_loc_paths(db):
    with manager(db) as c:
        err, = _only(c.post("/api/v1/alert-rules", json={**BASE, "metric": "ping_status", "operator": ">",
                                                         "threshold": 5}))
        assert err["loc"] == ["body", "threshold"]
        assert err["msg"] == "ping_status is 0 (down) or 1 (up); '> 5' can never be true"
        err, = _only(c.post("/api/v1/alert-rules", json={
            **BASE, "metric": "if_oper_status", "operator": "==", "threshold": 2,
            "conditions": [{"metric": "if_oper_status", "operator": ">", "threshold": 7}]}))
        assert err["loc"] == ["body", "conditions", 0, "threshold"] and "can never be true" in err["msg"]
        err, = _only(c.post("/api/v1/alert-rules", json={**BASE, "metric": "cpu", "operator": ">",
                                                         "threshold": 150}))
        assert "percentage from 0 to 100" in err["msg"]
        err, = _only(c.post("/api/v1/alert-rules", json={**BASE, "metric": "packet_loss", "operator": ">=",
                                                         "threshold": 0}))
        assert "always true" in err["msg"]

        ok = c.post("/api/v1/alert-rules", json={**BASE, "name": "Device CPU", "metric": "cpu",
                                                 "operator": ">", "threshold": 90})
        assert ok.status_code == 201, ok.text
        rid = ok.json()["id"]
        err, = _only(c.put(f"/api/v1/alert-rules/{rid}", json={"threshold": 150}))
        assert err["loc"] == ["body", "threshold"]
        assert c.put(f"/api/v1/alert-rules/{rid}", json={"name": "Device CPU 2"}).status_code == 200


def test_normalize_trigger_conditions():
    items, changed = checks.normalize_trigger_conditions(
        [{"metric": "ping_status", "operator": "eq", "threshold": 0}, {"metric": "rtt", "operator": ">",
                                                                       "threshold": 50}], "up")
    assert changed and items == [{"metric": "ping_status", "operator": "==", "threshold": 1.0},
                                 {"metric": "rtt", "operator": ">", "threshold": 50}]
    assert checks.normalize_trigger_conditions(
        [{"metric": "ping_status", "operator": ">", "threshold": 5}], "down")[0][0]["threshold"] == 0.0
    assert checks.normalize_trigger_conditions(
        [{"metric": "ping_status", "operator": "==", "threshold": 1}], "degraded")[1] is False
    # "any" keeps the condition, and the value-domain check still refuses it.
    items, changed = checks.normalize_trigger_conditions(
        [{"metric": "ping_status", "operator": ">", "threshold": 5}], "any")
    assert not changed and items[0]["threshold"] == 5
    assert checks.condition_errors(items, trigger_on="any", flat=True)[0]["loc"] == ["body", "threshold"]
    assert checks.condition_errors(items, trigger_on="down", flat=True) == []


def test_ping_status_is_normalised_to_trigger_on(db):
    """A ping_status condition that disagrees with a down/up/degraded trigger
    is rewritten to the value the trigger implies, never refused."""
    with manager(db) as c:
        body = {**BASE, "name": "Device comes up", "metric": "ping_status", "operator": "==", "threshold": 0,
                "trigger_on": "up"}
        r = c.post("/api/v1/alert-rules", json=body)
        assert r.status_code == 201, r.text
        assert (r.json()["operator"], r.json()["threshold"]) == ("==", 1)
        rid = r.json()["id"]
        r = c.put(f"/api/v1/alert-rules/{rid}", json={"trigger_on": "down"})
        assert r.status_code == 200, r.text
        assert (r.json()["trigger_on"], r.json()["threshold"]) == ("down", 0)
        r = c.put(f"/api/v1/alert-rules/{rid}", json={"trigger_on": "degraded"})
        assert r.status_code == 200 and r.json()["threshold"] == 1
        # Compound conditions: the ping_status entry and the flat mirror both follow.
        r = c.post("/api/v1/alert-rules", json={
            **body, "name": "Down and slow", "trigger_on": "down", "metric": "ping_status", "threshold": 1,
            "conditions": [{"metric": "ping_status", "operator": ">", "threshold": 5},
                           {"metric": "rtt", "operator": ">", "threshold": 200}]})
        assert r.status_code == 201, r.text
        assert r.json()["conditions"][0] == {"metric": "ping_status", "operator": "==", "threshold": 0.0,
                                             "reset_threshold": None}
        assert (r.json()["operator"], r.json()["threshold"]) == ("==", 0)
        # Value domains still apply when the trigger is "any".
        err, = _only(c.post("/api/v1/alert-rules", json={**body, "trigger_on": "any", "threshold": 5,
                                                         "operator": ">"}))
        assert err["loc"] == ["body", "threshold"] and "can never be true" in err["msg"]


def test_old_wizard_up_row_duplicates_and_updates(db):
    """A row the old wizard saved (trigger_on 'up', ping_status == 0):
    duplicating it and re-saving it in the editor both succeed and store
    ping_status == 1."""
    rid = db.scalar(
        "INSERT INTO alert_rules (name, description, enabled, metric, operator, threshold, duration, "
        "severity, notify_channels, cooldown, trigger_on, recovery_alert, min_duration, created_at, "
        "updated_at) VALUES ('Device back up', 'Old wizard row', true, 'ping_status', 'eq', 0, 0, "
        "'info', '[]'::jsonb, 300, 'up', false, 0, now(), now()) RETURNING id::text")
    with manager(db) as c:
        row = c.get(f"/api/v1/alert-rules/{rid}").json()
        assert (row["trigger_on"], row["threshold"]) == ("up", 0)
        editable = {k: row[k] for k in ("description", "enabled", "metric", "operator", "threshold",
                                         "severity", "notify_channels", "cooldown", "trigger_on",
                                         "recovery_alert", "min_duration")}
        dup = c.post("/api/v1/alert-rules", json={**editable, "name": "Device back up (copy)"})
        assert dup.status_code == 201, dup.text
        assert (dup.json()["operator"], dup.json()["threshold"], dup.json()["trigger_on"]) == ("==", 1, "up")
        upd = c.put(f"/api/v1/alert-rules/{rid}", json={**editable, "name": "Device back up"})
        assert upd.status_code == 200, upd.text
    stored = db.query("SELECT operator, threshold::float8 FROM alert_rules WHERE id = $1::uuid", rid)
    assert stored == [("==", 1.0)]


def test_host_metrics_on_alert_rules_name_the_server_rule_editor(db):
    with manager(db) as c:
        err, = _only(c.post("/api/v1/alert-rules", json={**BASE, "metric": "host_cpu_pct", "operator": ">",
                                                         "threshold": 90}))
        assert err == {"loc": ["body", "metric"], "msg": checks.HOST_RULES_MESSAGE, "type": "value_error"}
        host = c.get("/api/v1/host-alert-rules").json()["items"][0]
        # What the generic wizard used to send for a server rule.
        detail = _only(c.put(f"/api/v1/alert-rules/{host['id']}", json={
            "name": host["name"], "metric": host["metric"], "operator": "gt", "threshold": host["threshold"],
            "conditions": [{"metric": host["metric"], "operator": "gt", "threshold": 90,
                            "reset_threshold": 80}]}))
        assert {tuple(e["loc"]) for e in detail} == {("body", "metric"), ("body", "conditions", 0, "metric")}
        assert all(e["msg"] == checks.HOST_RULES_MESSAGE for e in detail)
        # Switching a server rule to a device metric is refused as well ...
        err, = _only(c.put(f"/api/v1/alert-rules/{host['id']}", json={"metric": "cpu"}))
        assert err["loc"] == ["body", "metric"]
        # ... while non-definition edits (name, channels, priority, target/scope) still work.
        assert c.put(f"/api/v1/alert-rules/{host['id']}", json={"priority": "P3"}).status_code == 200
        c.put(f"/api/v1/alert-rules/{host['id']}", json={"priority": None})
        r = c.put(f"/api/v1/alert-rules/{host['id']}", json={"target": host.get("target")})
        assert r.status_code == 200, r.text
        svc = c.post("/api/v1/host-alert-rules", json={"name": "W32Time stopped", "metric": "host_service_down",
                                                       "target": "W32Time"}).json()
        r = c.put(f"/api/v1/alert-rules/{svc['id']}", json={"target": "W32Time2"})
        assert r.status_code == 200 and r.json()["target"] == "W32Time2", r.text
        err, = _only(c.put(f"/api/v1/alert-rules/{svc['id']}", json={"target": " "}))
        assert err["loc"] == ["body", "target"] and "service name" in err["msg"]


def test_pattern_errors_are_readable_and_keep_loc(db):
    with manager(db) as c:
        err, = _only(c.post("/api/v1/alert-rules", json={**BASE, "metric": "bogus_metric", "operator": ">",
                                                         "threshold": 1}))
        assert err["loc"] == ["body", "metric"] and err["type"] == "string_pattern_mismatch"
        assert err["msg"] == "metric: 'bogus_metric' is not an accepted value" and "ctx" not in err
        err, = _only(c.post("/api/v1/alert-rules", json={**BASE, "metric": "cpu", "operator": "~",
                                                         "threshold": 1}))
        assert err["msg"].startswith("operator: '~' is not an accepted value (expected one of >, <, >=")
        err, = _only(c.post("/api/v1/alert-rules", json={**BASE, "metric": "cpu", "operator": ">",
                                                         "threshold": 1, "condition_logic": "XOR"}))
        assert err["msg"] == "condition_logic: 'XOR' is not an accepted value (expected one of AND, OR)"
        err, = _only(c.post("/api/v1/alert-rules", json={
            **BASE, "metric": "cpu", "operator": ">", "threshold": 1,
            "conditions": [{"metric": "cpu", "operator": "=>", "threshold": 1}]}))
        assert err["loc"] == ["body", "conditions", 0, "operator"]
        assert err["msg"].startswith("conditions[0].operator: '=>' is not an accepted value")
        # Other validation errors keep pydantic's own wording.
        err = _only(c.post("/api/v1/alert-rules", json={"severity": "warning"}))
        assert {tuple(e["loc"]) for e in err} >= {("body", "name"), ("body", "metric")}


def test_word_operators_are_accepted_on_create_as_symbols(db):
    """Duplicating a seeded rule (stored as eq/gt) posts the word form."""
    with manager(db) as c:
        r = c.post("/api/v1/alert-rules", json={**BASE, "name": "Interface down (copy)",
                                                "metric": "if_oper_status", "operator": "eq", "threshold": 2})
        assert r.status_code == 201, r.text
        assert r.json()["operator"] == "==" and r.json()["warnings"] == []


def test_warnings_in_responses_and_preview(db):
    with manager(db) as c:
        r = c.post("/api/v1/alert-rules", json={**BASE, "name": "High CPU", "description": "Memory above 90%",
                                                "metric": "memory", "operator": ">", "threshold": 80})
        assert r.status_code == 201, r.text
        fields = {(w["field"], w["message"].split(",")[0]) for w in r.json()["warnings"]}
        assert ("name", "The name mentions CPU") in fields
        assert ("description", "The description says 90%") in fields
        rid = r.json()["id"]
        r = c.put(f"/api/v1/alert-rules/{rid}", json={"name": "High memory", "threshold": 90})
        assert r.status_code == 200 and r.json()["warnings"] == []
    with viewer(db) as c:
        r = c.post("/api/v1/alert-rules/preview-warnings", json={
            "name": "High CPU", "metric": "host_memory_pct", "operator": "gt", "threshold": 150,
            "ignored": "extra keys are fine"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["warnings"][0]["field"] == "name" and "mentions CPU" in body["warnings"][0]["message"]
        assert body["errors"][0]["loc"] == ["body", "threshold"]
        # The save normalises a ping_status/trigger disagreement, so it is no error.
        r = c.post("/api/v1/alert-rules/preview-warnings", json={
            "name": "Up", "metric": "ping_status", "operator": "==", "threshold": 0, "trigger_on": "up"})
        assert r.json()["errors"] == []


# ── /host-alert-rules ────────────────────────────────────────────────────────

def test_host_rule_validation(db):
    with manager(db) as c:
        err, = _only(c.post("/api/v1/host-alert-rules", json={"name": "CPU", "metric": "host_cpu_pct",
                                                              "threshold": 150}))
        assert err["loc"] == ["body", "threshold"] and "percentage from 0 to 100" in err["msg"]
        err, = _only(c.post("/api/v1/host-alert-rules", json={"name": "CPU", "metric": "host_cpu_pct",
                                                              "threshold": 90, "reset_threshold": -5}))
        assert err["loc"] == ["body", "reset_threshold"]
        err, = _only(c.post("/api/v1/host-alert-rules", json={"name": "Spooler", "metric": "host_service_down"}))
        assert err["loc"] == ["body", "target"] and "service name" in err["msg"]
        r = c.post("/api/v1/host-alert-rules", content='{"name": "x", "metric": "host_cpu_pct", "threshold": NaN}',
                   headers={"content-type": "application/json"})
        assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "threshold"]

        svc = c.post("/api/v1/host-alert-rules", json={"name": "Spooler down", "metric": "host_service_down",
                                                       "target": "Spooler"})
        assert svc.status_code == 200, svc.text
        sid = svc.json()["id"]
        err, = _only(c.put(f"/api/v1/host-alert-rules/{sid}", json={"target": ""}))
        assert err["loc"] == ["body", "target"]
        err, = _only(c.put(f"/api/v1/host-alert-rules/{sid}", json={"target": None}))
        assert err["loc"] == ["body", "target"]
        assert c.put(f"/api/v1/host-alert-rules/{sid}", json={"target": "Spooler2"}).json()["target"] == "Spooler2"

        cpu = c.post("/api/v1/host-alert-rules", json={"name": "Big CPU", "metric": "host_cpu_pct",
                                                       "threshold": 95})
        err, = _only(c.put(f"/api/v1/host-alert-rules/{cpu.json()['id']}", json={"threshold": 150}))
        assert err["loc"] == ["body", "threshold"]
        r = c.put(f"/api/v1/host-alert-rules/{cpu.json()['id']}", json={"name": "High memory"})
        assert r.status_code == 200 and "mentions memory" in r.json()["warnings"][0]["message"]


_UPDATE_FIELDS = ("name", "description", "enabled", "metric", "operator", "threshold", "severity",
                  "min_duration", "clear_duration", "reset_threshold", "recovery_alert", "cooldown",
                  "notify_channels", "target", "priority", "escalation_policy_id")


def test_existing_server_rules_round_trip_through_put(db):
    with manager(db) as c:
        items = c.get("/api/v1/host-alert-rules").json()["items"]
        seeded = next(r for r in items if r["name"] == "High CPU usage")
        # A rule with an explicit reset level too.
        reset = c.post("/api/v1/host-alert-rules", json={"name": "Memory above 90%", "metric": "host_memory_pct",
                                                         "threshold": 90, "reset_threshold": 80,
                                                         "server_id": None}).json()
        for rule in (seeded, reset):
            body = {k: rule[k] for k in _UPDATE_FIELDS}
            body["server_id"] = rule["server_id"]            # what an editor round-trips; ignored by PUT
            r = c.put(f"/api/v1/host-alert-rules/{rule['id']}", json=body)
            assert r.status_code == 200, r.text
            after = r.json()
            assert {k: after[k] for k in _UPDATE_FIELDS} == {k: rule[k] for k in _UPDATE_FIELDS}
            assert after["warnings"] == []
