"""Device Templates phase 1 (migrate-189) against a disposable PostgreSQL.

Covers the acceptance list in docs/plans/device-templates-plan.md, phase 1:

* the device type catalogue replaces the seven-value CHECK and is enforced;
* drafts do not change what devices poll; publish bumps the revision and
  records history; a pilot changes only the pilot devices; rollback restores
  an earlier revision as a new one;
* a customised built-in keeps the customer's changes when a release updates
  the built-in, and receives the new metrics;
* switching template removes the old template's readings; controller
  children cannot be put on a template; the node count never changes;
* only ``devices.manage_templates`` (unscoped) changes templates;
* editing keeps every match rule (extract_*, default_*).
"""
from __future__ import annotations

import json

import pytest

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

TEMPLATE_ADMIN = ["devices.view", "devices.manage", "devices.manage_templates", "alerts.view"]
DEVICE_OPERATOR = ["devices.view", "devices.manage"]
NODE_COUNT = ("SELECT (SELECT count(*) FROM devices WHERE poll_mode IS DISTINCT FROM 'via_controller')")

GROUPS = [
    {"key": "system", "name": "System", "kind": "scalar", "metrics": [
        {"key": "cpu_pct", "name": "CPU", "oid": "1.3.6.1.4.1.99999.1.1", "unit": "%",
         "thresholds": {"warn": 80, "crit": 90, "op": ">="}},
        {"key": "mem_pct", "name": "Memory", "oid": "1.3.6.1.4.1.99999.1.2", "unit": "%"},
    ]},
    {"key": "tunnels", "name": "Tunnels", "kind": "table", "table": {"label_oid": "1.3.6.1.4.1.99999.2.1"},
     "metrics": [{"key": "tun_state", "name": "State", "oid": "1.3.6.1.4.1.99999.2.2", "type": "enum"}]},
]


def admin(db, **kw):
    return api_client(db, role=kw.pop("role", "tpl-admin"), permissions=kw.pop("permissions", TEMPLATE_ADMIN),
                      persist_user=True, username=kw.pop("username", "tpl-lead"), **kw)


def add_device(db, name, ip, *, profile=None, device_type="other", child_of=None, tags=()):
    return str(db.scalar(
        "INSERT INTO devices (hostname, ip_address, device_type, profile_id, managed_by_device_id, poll_mode, tags,"
        " snmp_enabled) VALUES ($1, $2::inet, $3, $4, $5, $6, $7::jsonb, TRUE) RETURNING id",
        name, ip, device_type, profile, child_of, "via_controller" if child_of else "direct", list(tags)))


def add_value(db, device, group, metric, value=1.0):
    db.execute("INSERT INTO device_template_values (device_id, group_key, metric_key, instance, series_key,"
               " value_num, updated_at) VALUES ($1, $2, $3, '', $4, $5, now())",
               device, group, metric, f"tpl_{metric}", value)


def values(db, device):
    return sorted(r[0] for r in db.query(
        "SELECT group_key || '.' || metric_key FROM device_template_values WHERE device_id = $1", device))


def builtin(db, key):
    return str(db.scalar("SELECT id FROM device_profiles WHERE builtin_key = $1", key))


@pytest.fixture
def world(pg_db):
    db = pg_db
    db.execute("DELETE FROM device_template_values; DELETE FROM devices; "
               "DELETE FROM device_profiles WHERE NOT builtin; DELETE FROM alert_rules")
    return db


def create(c, name="Acme Edge", groups=GROUPS, **extra):
    body = {"name": name, "vendor": "Acme", "device_type": "sdwan_edge", "match_priority": 120,
            "match_rules": {"sys_object_id_prefixes": ["1.3.6.1.4.1.99999"], "sys_descr_regex": "(?i)acme",
                            "extract_model": r"model (\S+)", "default_vendor": "Acme"},
            "oid_groups": groups, **extra}
    r = c.post("/api/v1/snmp/profiles", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def content(tpl, **changes):
    body = {k: tpl[k] for k in ("name", "vendor", "description", "device_type", "icon", "match_priority",
                                "match_rules", "oid_groups")}
    body.update(changes)
    return body


# ── Migration ───────────────────────────────────────────────────────────────

def test_type_catalogue_replaces_the_check_and_is_enforced(world):
    db = world
    keys = {r[0] for r in db.query("SELECT key FROM device_types")}
    assert {"router", "switch", "firewall", "server", "access_point", "printer", "other",
            "load_balancer", "wireless_controller", "ups", "storage"} <= keys
    add_device(db, "ups-1", "192.0.2.10", device_type="ups")
    with pytest.raises(Exception):
        add_device(db, "toaster", "192.0.2.11", device_type="toaster")
    assert db.scalar("SELECT count(*) FROM device_profiles WHERE builtin AND builtin_key IS NULL") == 0
    assert db.scalar("SELECT count(*) FROM device_profiles p WHERE NOT EXISTS "
                     "(SELECT 1 FROM device_profile_versions v WHERE v.profile_id = p.id)") == 0


def test_builtin_metric_keys_are_unique_per_template(world):
    """Metric keys name the stored series (tpl_<key>); the save validation relies on it."""
    for name, groups in world.query("SELECT name, oid_groups FROM device_profiles WHERE builtin"):
        groups = json.loads(groups) if isinstance(groups, str) else groups
        seen = {}
        for g in groups:
            for m in g.get("metrics") or []:
                assert seen.setdefault(m["key"], g["key"]) == g["key"], (name, m["key"])


# ── Create, draft, publish, history ─────────────────────────────────────────

def test_draft_changes_nothing_until_published_then_history_records_it(world):
    db = world
    with admin(db) as c:
        tpl = create(c)
        assert tpl["revision"] == 1 and tpl["device_type"] == "sdwan_edge"
        dev = add_device(db, "edge-1", "192.0.2.21", profile=tpl["id"])
        add_value(db, dev, "tunnels", "tun_state")
        add_value(db, dev, "system", "cpu_pct")

        groups = json.loads(json.dumps(GROUPS))[:1]          # drop the tunnels group
        groups[0]["metrics"][0]["thresholds"] = {"warn": 85, "crit": 95, "op": ">="}
        r = c.put(f"/api/v1/snmp/profiles/{tpl['id']}/draft", json=content(tpl, oid_groups=groups))
        assert r.status_code == 200, r.text
        assert r.json()["has_draft"] is True
        stored = json.loads(db.scalar("SELECT oid_groups::text FROM device_profiles WHERE id = $1", tpl["id"]))
        assert [g["key"] for g in stored] == ["system", "tunnels"]           # devices still poll revision 1

        imp = c.post(f"/api/v1/snmp/profiles/{tpl['id']}/impact", json={}).json()
        assert imp["groups_removed"] == ["tunnels"] and imp["devices"] == 1
        assert imp["oids_before"] == 4 and imp["oids_after"] == 2

        r = c.post(f"/api/v1/snmp/profiles/{tpl['id']}/publish", json={"reason": "drop tunnels"})
        assert r.status_code == 200, r.text
        assert r.json()["revision"] == 2 and r.json()["has_draft"] is False
        assert values(db, dev) == ["system.cpu_pct"]                          # removed group's readings went

        versions = c.get(f"/api/v1/snmp/profiles/{tpl['id']}/versions").json()["data"]
        assert [(v["revision"], v["action"]) for v in versions] == [(2, "published"), (1, "created")]
        assert versions[0]["reason"] == "drop tunnels" and versions[0]["changed_by_name"] == "tpl-lead"
        assert db.scalar("SELECT count(*) FROM audit_logs WHERE action = 'device_template.publish'") == 1


def test_rollback_publishes_an_earlier_revision_as_a_new_one(world):
    db = world
    with admin(db) as c:
        tpl = create(c)
        c.put(f"/api/v1/snmp/profiles/{tpl['id']}/draft", json=content(tpl, oid_groups=GROUPS[:1]))
        c.post(f"/api/v1/snmp/profiles/{tpl['id']}/publish", json={})
        r = c.post(f"/api/v1/snmp/profiles/{tpl['id']}/rollback", json={"revision": 1})
        assert r.status_code == 200, r.text
        assert r.json()["revision"] == 3
        assert [g["key"] for g in r.json()["oid_groups"]] == ["system", "tunnels"]
        diff = c.get(f"/api/v1/snmp/profiles/{tpl['id']}/versions/2").json()["compare_to_current"]
        assert diff["groups_added"] == ["tunnels"]


def test_pilot_changes_only_the_pilot_devices_until_promoted_or_cancelled(world):
    db = world
    with admin(db) as c:
        tpl = create(c)
        a = add_device(db, "edge-a", "192.0.2.31", profile=tpl["id"])
        b = add_device(db, "edge-b", "192.0.2.32", profile=tpl["id"])
        outsider = add_device(db, "other", "192.0.2.33")
        for d in (a, b):
            add_value(db, d, "tunnels", "tun_state")
        c.put(f"/api/v1/snmp/profiles/{tpl['id']}/draft", json=content(tpl, oid_groups=GROUPS[:1]))

        assert c.post(f"/api/v1/snmp/profiles/{tpl['id']}/publish",
                      json={"pilot_device_ids": [outsider]}).status_code == 422
        r = c.post(f"/api/v1/snmp/profiles/{tpl['id']}/publish", json={"pilot_device_ids": [a]})
        assert r.status_code == 200, r.text
        assert r.json()["pilot_device_count"] == 1 and r.json()["revision"] == 1
        # What the poller reads (poller/internal/store LoadSNMPDevices):
        polled = dict(db.query("""
            SELECT d.hostname, (CASE WHEN p.pilot_device_ids IS NOT NULL AND d.id = ANY(p.pilot_device_ids)
                                     THEN p.pilot_oid_groups ELSE p.oid_groups END)::text
              FROM devices d JOIN device_profiles p ON p.id = d.profile_id"""))
        assert [g["key"] for g in json.loads(polled["edge-a"])] == ["system"]
        assert [g["key"] for g in json.loads(polled["edge-b"])] == ["system", "tunnels"]
        assert values(db, a) == [] and values(db, b) == ["tunnels.tun_state"]

        assert c.delete(f"/api/v1/snmp/profiles/{tpl['id']}/draft").status_code == 409
        r = c.post(f"/api/v1/snmp/profiles/{tpl['id']}/pilot/cancel")
        assert r.json()["pilot_device_count"] == 0 and r.json()["has_draft"] is True

        c.post(f"/api/v1/snmp/profiles/{tpl['id']}/publish", json={"pilot_device_ids": [a]})
        r = c.post(f"/api/v1/snmp/profiles/{tpl['id']}/publish", json={})       # promote to everyone
        assert r.json()["revision"] == 2 and r.json()["pilot_device_count"] == 0
        assert values(db, b) == []


def test_editing_keeps_every_match_rule(world):
    db = world
    with admin(db) as c:
        tpl = create(c)
        rules = dict(tpl["match_rules"], require_sys_descr=True)
        c.put(f"/api/v1/snmp/profiles/{tpl['id']}/draft", json=content(tpl, match_rules=rules, match_priority=300))
        out = c.post(f"/api/v1/snmp/profiles/{tpl['id']}/publish", json={}).json()
        assert out["match_rules"]["extract_model"] == r"model (\S+)"
        assert out["match_rules"]["default_vendor"] == "Acme"
        assert out["match_rules"]["require_sys_descr"] is True and out["match_priority"] == 300


def test_save_validation(world):
    db = world
    with admin(db) as c:
        tpl = create(c)
        dup = json.loads(json.dumps(GROUPS))
        dup[1]["metrics"][0]["key"] = "cpu_pct"
        r = c.put(f"/api/v1/snmp/profiles/{tpl['id']}/draft", json=content(tpl, oid_groups=dup))
        assert r.status_code == 422 and "unique" in json.dumps(r.json())
        r = c.put(f"/api/v1/snmp/profiles/{tpl['id']}/draft", json=content(tpl, device_type="toaster"))
        assert r.status_code == 422
        bad = dict(tpl["match_rules"], sys_descr_regex="(?<=x)y")
        assert c.put(f"/api/v1/snmp/profiles/{tpl['id']}/draft", json=content(tpl, match_rules=bad)).status_code == 422
        assert c.post("/api/v1/snmp/profiles", json={"name": "Acme Edge"}).status_code == 409


# ── Customised built-ins ────────────────────────────────────────────────────

def test_customised_builtin_keeps_changes_through_a_release_update(world):
    db = world
    fgt = builtin(db, "fortinet_fortigate")
    dev = add_device(db, "fw-1", "192.0.2.41", profile=fgt, device_type="firewall")
    with admin(db) as c:
        r = c.post(f"/api/v1/snmp/profiles/{fgt}/customize", json={"move_devices": True})
        assert r.status_code == 201, r.text
        mine = r.json()
        assert mine["parent_id"] == fgt and mine["match_priority"] == 110 and mine["device_count"] == 1
        assert db.scalar("SELECT profile_id::text FROM devices WHERE id = $1", dev) == mine["id"]

        detail = c.get(f"/api/v1/snmp/profiles/{mine['id']}").json()
        groups = detail["oid_groups"]
        dropped = groups[-1]["key"]
        edited = [g for g in groups if g["key"] != dropped]
        edited.append({"key": "branch_extra", "name": "Branch extra",
                       "metrics": [{"key": "branch_x", "name": "X", "oid": "1.3.6.1.4.1.12356.999.1"}]})
        r = c.put(f"/api/v1/snmp/profiles/{mine['id']}/draft", json=content(mine, oid_groups=edited))
        assert r.status_code == 200, r.text
        assert c.post(f"/api/v1/snmp/profiles/{mine['id']}/publish", json={}).status_code == 200
        overlay = c.get(f"/api/v1/snmp/profiles/{mine['id']}").json()["overlay"]
        assert overlay["disable_groups"] == [dropped] and overlay["add_groups"][0]["key"] == "branch_extra"
        assert set(overlay) == {"disable_groups", "add_groups"}       # nothing else counted as a change

        # A release migration updates the built-in (outside the API).
        db.execute("""
            UPDATE device_profiles SET oid_groups = oid_groups || jsonb_build_array(jsonb_build_object(
              'key', 'release_new', 'name', 'New in release', 'metrics', jsonb_build_array(jsonb_build_object(
                'key', 'release_metric', 'name', 'New', 'oid', '1.3.6.1.4.1.12356.998.1'))))
             WHERE id = $1""", fgt)
        assert c.post("/api/v1/snmp/profiles/resync").json() == {"external_updates": 1, "reresolved": 1}
        keys = [g["key"] for g in c.get(f"/api/v1/snmp/profiles/{mine['id']}").json()["oid_groups"]]
        assert "release_new" in keys and "branch_extra" in keys and dropped not in keys
        actions = [v["action"] for v in c.get(f"/api/v1/snmp/profiles/{mine['id']}/versions").json()["data"]]
        assert actions[0] == "parent_update"
        assert c.get(f"/api/v1/snmp/profiles/{fgt}/versions").json()["data"][0]["action"] == "external_update"
        assert c.post("/api/v1/snmp/profiles/resync").json() == {"external_updates": 0, "reresolved": 0}

        # Built-ins stay read-only; the parent cannot be deleted from under its customisation.
        assert c.put(f"/api/v1/snmp/profiles/{fgt}/draft", json=content(mine)).status_code == 400
        oid_change = json.loads(json.dumps(edited))
        oid_change[0]["metrics"][0]["oid"] = "1.3.6.1.4.1.1.1"
        assert c.put(f"/api/v1/snmp/profiles/{mine['id']}/draft",
                     json=content(mine, oid_groups=oid_change)).status_code == 422


def test_export_and_import_of_a_customised_template(world):
    db = world
    fgt = builtin(db, "fortinet_fortigate")
    with admin(db) as c:
        mine = c.post(f"/api/v1/snmp/profiles/{fgt}/customize", json={"move_devices": False}).json()
        bundle = c.get(f"/api/v1/snmp/profiles/{mine['id']}/export").json()
        assert bundle["format_version"] == 2 and bundle["profile"]["parent"]["builtin_key"] == "fortinet_fortigate"
        r = c.post("/api/v1/snmp/profiles/import", json=bundle)
        assert r.status_code == 201, r.text
        assert r.json()["parent_id"] == fgt and r.json()["name"] != mine["name"]
        orphan = {"format_version": 2, "profile": {**bundle["profile"], "parent": {"builtin_key": "nope", "name": "nope"}}}
        assert c.post("/api/v1/snmp/profiles/import", json=orphan).status_code == 422


# ── Assignment ──────────────────────────────────────────────────────────────

def test_assign_records_source_clears_old_readings_and_skips_children(world):
    db = world
    nodes = db.scalar(NODE_COUNT)
    with admin(db) as c:
        tpl = create(c)
        other = create(c, name="Other")
        ctrl = add_device(db, "fgt-ctrl", "192.0.2.51")
        dev = add_device(db, "edge-9", "192.0.2.52", profile=other["id"])
        child = add_device(db, "fap-1", "192.0.2.53", child_of=ctrl)
        add_value(db, dev, "system", "cpu_pct")
        nodes = db.scalar(NODE_COUNT)
        r = c.post(f"/api/v1/snmp/profiles/{tpl['id']}/assign",
                   json={"device_ids": [dev, child], "set_device_type": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["assigned"] == 1 and body["device_type_set"] == 1
        assert body["skipped"][0]["id"] == child and "controller" in body["skipped"][0]["reason"]
        row = db.query("SELECT profile_id::text, profile_source, device_type, device_type_source FROM devices "
                       "WHERE id = $1", dev)[0]
        assert row == (tpl["id"], "manual", "sdwan_edge", "template")
        assert values(db, dev) == []
        assert db.scalar("SELECT profile_id FROM devices WHERE id = $1", child) is None
        assert c.post("/api/v1/snmp/profiles/unassign", json={"device_ids": [dev]}).json()["unassigned"] == 1
        assert db.scalar("SELECT profile_source FROM devices WHERE id = $1", dev) is None
        devs = c.get(f"/api/v1/snmp/profiles/{tpl['id']}/devices").json()
        assert devs["data"] == []
    assert db.scalar(NODE_COUNT) == nodes


def test_suggestions_are_listed_and_accepted(world):
    db = world
    with admin(db) as c:
        tpl = create(c)
        other = create(c, name="Other")
        dev = add_device(db, "edge-7", "192.0.2.61", profile=other["id"])
        db.execute("UPDATE devices SET profile_suggested_id = $1, profile_suggested_at = now() WHERE id = $2",
                   tpl["id"], dev)
        rows = c.get("/api/v1/snmp/profiles/suggestions").json()["data"]
        assert rows[0]["suggested_name"] == "Acme Edge" and rows[0]["current_name"] == "Other"
        assert c.post("/api/v1/snmp/profiles/suggestions/accept", json={"device_ids": [dev]}).json() == {"moved": 1}
        assert db.scalar("SELECT profile_suggested_id FROM devices WHERE id = $1", dev) is None


def test_unmatched_and_classify(world):
    db = world
    with admin(db) as c:
        create(c)
        dev = add_device(db, "mystery", "192.0.2.71")
        db.execute("UPDATE devices SET sys_object_id = '.1.3.6.1.4.1.99999.5', sys_descr = 'ACME box' WHERE id = $1", dev)
        rows = c.get("/api/v1/snmp/profiles/unmatched").json()["data"]
        assert [r["hostname"] for r in rows] == ["mystery"] and rows[0]["would_match"]["name"] == "Acme Edge"
        out = c.post("/api/v1/snmp/profiles/classify", json={"device_id": dev}).json()
        assert out["winner"]["name"] == "Acme Edge" and out["current"]["id"] is None


def test_device_update_validates_type_and_records_manual_template(world):
    db = world
    with admin(db) as c:
        tpl = create(c)
        dev = add_device(db, "edge-3", "192.0.2.81")
        assert c.put(f"/api/v1/devices/{dev}", json={"device_type": "toaster"}).status_code == 422
        r = c.put(f"/api/v1/devices/{dev}", json={"device_type": "ups", "profile_id": tpl["id"]})
        assert r.status_code == 200, r.text
        assert db.query("SELECT profile_source, device_type_source FROM devices WHERE id = $1", dev)[0] == ("manual", "manual")


def test_test_on_device_refuses_what_the_appliance_does_not_poll(world):
    db = world
    with admin(db) as c:
        tpl = create(c)
        ctrl = add_device(db, "ctrl", "192.0.2.91")
        child = add_device(db, "child", "192.0.2.92", child_of=ctrl)
        sensor_dev = add_device(db, "remote", "192.0.2.93", profile=tpl["id"])
        sensor = db.scalar("INSERT INTO sensors (name, status) VALUES ('branch-sensor', 'online') RETURNING id")
        db.execute("UPDATE devices SET default_sensor_id = $1 WHERE id = $2", sensor, sensor_dev)
        r = c.post(f"/api/v1/snmp/profiles/{tpl['id']}/test", json={"device_id": child, "source": "published"})
        assert r.status_code == 409 and "controller" in r.text
        r = c.post(f"/api/v1/snmp/profiles/{tpl['id']}/test", json={"device_id": sensor_dev, "source": "published"})
        assert r.status_code == 409 and "sensor" in r.text


# ── Permissions ─────────────────────────────────────────────────────────────

def test_template_writes_need_manage_templates_and_an_unscoped_user(world):
    db = world
    with admin(db) as c:
        tpl = create(c)
    with api_client(db, role="dev-op", permissions=DEVICE_OPERATOR, username="op") as c:
        assert c.get("/api/v1/snmp/profiles").status_code == 200
        assert c.post("/api/v1/snmp/profiles", json={"name": "Nope"}).status_code == 403
        assert c.put(f"/api/v1/snmp/profiles/{tpl['id']}/draft", json=content(tpl)).status_code == 403
        assert c.post("/api/v1/device-types", json={"key": "nas", "label": "NAS"}).status_code == 403
        dev = add_device(db, "edge-x", "192.0.2.99")
        assert c.post(f"/api/v1/snmp/profiles/{tpl['id']}/assign", json={"device_ids": [dev]}).status_code == 200
    with api_client(db, role="scoped-tpl", permissions=TEMPLATE_ADMIN, username="scoped",
                    scope_tags=("team-a",)) as c:
        assert c.post("/api/v1/snmp/profiles", json={"name": "Scoped"}).status_code == 403
        hidden = add_device(db, "hidden", "192.0.2.98", tags=("team-b",))
        assert c.post(f"/api/v1/snmp/profiles/{tpl['id']}/assign", json={"device_ids": [hidden]}).status_code == 404


# ── Device types ────────────────────────────────────────────────────────────

def test_device_type_crud(world):
    db = world
    with admin(db) as c:
        r = c.post("/api/v1/device-types", json={"key": "nas", "label": "NAS", "category": "storage", "icon": "storage"})
        assert r.status_code == 201, r.text
        assert c.post("/api/v1/device-types", json={"key": "nas", "label": "Dup"}).status_code == 409
        assert c.post("/api/v1/device-types", json={"key": "nas2", "label": "X", "category": "bogus"}).status_code == 422
        assert c.put("/api/v1/device-types/nas", json={"label": "Network storage"}).json()["label"] == "Network storage"
        add_device(db, "nas-1", "192.0.2.101", device_type="nas")
        assert c.delete("/api/v1/device-types/nas").status_code == 409
        assert c.delete("/api/v1/device-types/router").status_code == 400
        db.execute("DELETE FROM devices WHERE device_type = 'nas'")
        assert c.delete("/api/v1/device-types/nas").status_code == 204
        types = c.get("/api/v1/device-types").json()["data"]
        assert types[0]["key"] == "router" and types[-1]["key"] == "other"


def test_every_builtin_round_trips_through_the_editor_without_changes(world):
    """Customise each built-in and save its groups unchanged: the API accepts them and the
    overlay stays empty (no field the schema normalises is mistaken for a customer change)."""
    db = world
    keys = [r[0] for r in db.query("SELECT builtin_key FROM device_profiles WHERE builtin ORDER BY name")]
    with admin(db) as c:
        for key in keys:
            mine = c.post(f"/api/v1/snmp/profiles/{builtin(db, key)}/customize", json={"move_devices": False}).json()
            r = c.put(f"/api/v1/snmp/profiles/{mine['id']}/draft", json=content(mine))
            assert r.status_code == 200, (key, r.text)
            assert c.post(f"/api/v1/snmp/profiles/{mine['id']}/publish", json={}).status_code == 200, key
            detail = c.get(f"/api/v1/snmp/profiles/{mine['id']}").json()
            assert detail["overlay"] == {}, (key, detail["overlay"])
            assert detail["oid_groups"] == detail["parent_groups"], key
