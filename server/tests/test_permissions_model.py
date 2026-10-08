"""Permission vocabulary, built-in role definitions, delegation primitives."""
from __future__ import annotations

import json

import pytest

from app.core.permissions import (
    ALL_PERMISSIONS, BUILTIN_ROLE_DEFINITION_VERSION, BUILTIN_ROLE_DEFINITIONS, LEGACY_ROLE_PERMISSIONS,
    PERMISSION_IMPLIES, PERMISSION_RISK, RESERVED_ROLE_NAMES, SUPERUSER_PERMISSION, catalog,
    effective_permissions, exceeds, risk_of,
)
from app.services.role_service import reconcile_builtin_roles

INVENTORY_PERMS = ("inventory.view", "inventory.export", "inventory.manage")


def test_catalog_entries_carry_risk_and_implies():
    for m in catalog():
        for p in m["permissions"]:
            assert p["risk"] in ("standard", "sensitive", "privileged", "critical")
            assert all(i in ALL_PERMISSIONS for i in p["implies"])
    assert risk_of("system.admin") == "critical" and risk_of("users.manage") == "privileged"


def test_risk_and_implies_reference_real_permissions():
    assert set(PERMISSION_RISK) <= set(ALL_PERMISSIONS)
    assert set(PERMISSION_IMPLIES) <= set(ALL_PERMISSIONS)


def test_builtin_definitions_are_valid_and_least_privilege_ordered():
    for name, perms in BUILTIN_ROLE_DEFINITIONS.items():
        assert set(perms) <= set(ALL_PERMISSIONS), name
    ro, viewer, op = (set(BUILTIN_ROLE_DEFINITIONS[n]) for n in ("read_only", "viewer", "operator"))
    assert ro < viewer
    assert viewer - op <= {"alerts.acknowledge"} | set()  # operator is a superset of viewer
    assert "users.manage" not in op and "roles.manage" not in op and SUPERUSER_PERMISSION not in op
    # the persisted-vs-code drift (RP-12): NetPath is part of every built-in that can see it
    assert "netpath.view" in ro and {"netpath.view", "netpath.manage", "udt.view_users"} <= op
    assert viewer <= op


def test_new_netflow_permissions_are_not_granted_to_read_only_roles():
    for name in ("viewer", "read_only"):
        assert not {"netflow.manage", "netflow.manage_shared", "netflow.export"} & set(BUILTIN_ROLE_DEFINITIONS[name])


def test_exceeds_expands_full_administration():
    assert exceeds(["system.admin"], ["users.manage"])            # cannot hand out everything
    assert exceeds(["users.manage"], ["system.admin"]) == []       # an admin holds everything
    assert exceeds(["devices.view"], ["devices.view", "devices.manage"]) == []
    assert exceeds(["alerts.manage"], ["devices.view"]) == ["alerts.manage"]
    assert effective_permissions(["system.admin"]) == frozenset(ALL_PERMISSIONS)
    assert effective_permissions(["not.a.permission"]) == frozenset()


@pytest.mark.parametrize("name", ["owner", "admin", "root", "superuser", "editor", "viewer", "read_only"])
def test_reserved_names(name):
    assert name in RESERVED_ROLE_NAMES


# ── Inventory module (Phase 0 task 0.6) ───────────────────────────────────────

def test_catalog_lists_the_inventory_module():
    modules = {m["module"]: m for m in catalog()}
    assert modules["inventory"]["label"] == "Inventory"
    perms = {p["id"]: p for p in modules["inventory"]["permissions"]}
    assert tuple(perms) == INVENTORY_PERMS
    for p in perms.values():
        assert p["label"] and p["description"]
    assert "tag scope" in perms["inventory.view"]["description"]
    # serials and software lists are reconnaissance data: export is flagged, the others are not
    assert [perms[i]["risk"] for i in INVENTORY_PERMS] == ["standard", "sensitive", "standard"]
    assert perms["inventory.manage"]["implies"] == ["inventory.view"]
    assert perms["inventory.export"]["implies"] == ["inventory.view"]
    assert perms["inventory.view"]["implies"] == []


def test_inventory_implies_edges_and_risk():
    assert PERMISSION_IMPLIES["inventory.manage"] == ["inventory.view"]
    assert PERMISSION_IMPLIES["inventory.export"] == ["inventory.view"]
    assert "inventory.view" not in PERMISSION_IMPLIES
    assert PERMISSION_RISK["inventory.export"] == "sensitive"
    assert risk_of("inventory.export") == "sensitive"
    assert risk_of("inventory.view") == risk_of("inventory.manage") == "standard"


def test_inventory_builtin_role_grants():
    ro, viewer, op = (set(BUILTIN_ROLE_DEFINITIONS[n]) for n in ("read_only", "viewer", "operator"))
    assert "inventory.view" in ro and "inventory.view" in viewer and set(INVENTORY_PERMS) <= op
    # owner decision D4: viewers (and read-only) never get the sensitive export or any write
    for held in (ro, viewer):
        assert not {"inventory.export", "inventory.manage"} & held
    assert set(INVENTORY_PERMS) <= effective_permissions(BUILTIN_ROLE_DEFINITIONS["admin"])
    # the code-level fallback used when the roles table is unavailable mirrors the built-ins
    for name in ("read_only", "viewer", "operator", "admin"):
        assert set(LEGACY_ROLE_PERMISSIONS[name]) == set(BUILTIN_ROLE_DEFINITIONS[name]), name
    assert set(LEGACY_ROLE_PERMISSIONS["editor"]) == op


def test_builtin_roles_hold_everything_their_permissions_imply():
    for name, perms in BUILTIN_ROLE_DEFINITIONS.items():
        held = effective_permissions(perms)
        missing = {f"{p} -> {i}" for p in held for i in PERMISSION_IMPLIES.get(p, []) if i not in held}
        assert not missing, (name, sorted(missing))


def test_builtin_definition_version_covers_the_inventory_permissions():
    # Built-in rows are rewritten at startup only when the code version is newer than
    # the persisted one; v4 (servers.* split, team roles) shipped before any inventory.*
    # permission existed, so the inventory grants need at least v5.
    assert BUILTIN_ROLE_DEFINITION_VERSION >= 5


def test_roles_catalog_endpoint_lists_inventory(client, as_admin):
    body = client.get("/api/v1/roles/catalog").json()
    inventory = next(m for m in body["modules"] if m["module"] == "inventory")
    assert [p["id"] for p in inventory["permissions"]] == list(INVENTORY_PERMS)
    export = next(p for p in inventory["permissions"] if p["id"] == "inventory.export")
    assert export["risk"] == "sensitive" and export["implies"] == ["inventory.view"]


# ── built-in reconciliation never reaches custom roles ────────────────────────

class _Row:
    """Row-like: indexable the way ``row[0]`` is used by reconcile_builtin_roles."""

    def __init__(self, *values):
        self._values = values

    def __getitem__(self, i):
        return self._values[i]


class _Result:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row


class _RolesDB:
    """In-memory ``roles`` / ``role_history`` that interprets only the statements
    ``reconcile_builtin_roles`` issues. Like the real statement it returns every row with
    that name and reports ``is_system`` as the fourth column; the function itself skips
    custom roles."""

    def __init__(self, roles):
        self.roles = roles
        self.history = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql, p = " ".join(str(statement).split()), params or {}
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            return _Result()
        if sql.startswith("SELECT id, permissions"):
            for r in self.roles:
                if r["name"] == p["n"]:
                    return _Result(_Row(r["id"], r["permissions"], r["definition_version"] or 0,
                                        r["is_system"]))
            return _Result()
        if sql.startswith("UPDATE roles SET"):
            target = next(r for r in self.roles if r["id"] == p["id"])
            target.update(permissions=json.loads(p["p"]), definition_version=p["v"],
                          revision=target["revision"] + 1)
            return _Result()
        if sql.startswith("INSERT INTO role_history"):
            self.history.append(p)
            return _Result()
        raise AssertionError(f"unexpected statement: {sql}")

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        raise AssertionError("reconciliation rolled back")


def _roles_at_definition_v4():
    """Built-in rows as persisted by release 1.23.35 (definition version 4: the servers.*
    split and the four team roles, no inventory.* anywhere), plus custom roles that hold
    hand-picked permissions."""
    rows = [
        {"id": i, "name": name, "is_system": True, "definition_version": 4, "revision": 7,
         "permissions": sorted(p for p in perms if not p.startswith("inventory."))}
        for i, (name, perms) in enumerate(BUILTIN_ROLE_DEFINITIONS.items(), start=1)
    ]
    rows += [
        {"id": 100, "name": "helpdesk", "is_system": False, "definition_version": None, "revision": 3,
         "permissions": ["alerts.view", "devices.view", "apm.view"]},
        {"id": 101, "name": "net-managers", "is_system": False, "definition_version": 4, "revision": 1,
         "permissions": ["devices.view", "devices.manage", "reports.export"]},
    ]
    return rows


@pytest.mark.asyncio
async def test_reconcile_gives_builtins_inventory_but_never_custom_roles():
    db = _RolesDB(_roles_at_definition_v4())
    custom_before = {r["name"]: dict(r) for r in db.roles if not r["is_system"]}

    changed = await reconcile_builtin_roles(db)

    added = {c["role"]: c["added"] for c in changed}
    # The reconcile also re-stamps the team roles (their rows are below the new version) but
    # they gain nothing: inventory.* is deliberately not part of any team role yet.
    assert added == {
        "admin": sorted(INVENTORY_PERMS),
        "operator": sorted(INVENTORY_PERMS),
        "viewer": ["inventory.view"],
        "read_only": ["inventory.view"],
        "network_team": [], "systems_team": [], "application_team": [], "security_team": [],
    }
    assert all(c["removed"] == [] for c in changed)
    by_name = {r["name"]: r for r in db.roles}
    for name in BUILTIN_ROLE_DEFINITIONS:
        assert set(by_name[name]["permissions"]) == set(BUILTIN_ROLE_DEFINITIONS[name]), name
        assert by_name[name]["definition_version"] == BUILTIN_ROLE_DEFINITION_VERSION
        assert by_name[name]["revision"] == 8
    for name in ("network_team", "systems_team", "application_team", "security_team"):
        assert not set(INVENTORY_PERMS) & set(by_name[name]["permissions"]), name
    # custom roles: permissions, version and revision are byte-for-byte what they were
    for name, before in custom_before.items():
        assert by_name[name] == before, name
        assert not set(INVENTORY_PERMS) & set(by_name[name]["permissions"])
    # one history entry per changed built-in and none for a custom role
    assert sorted(h["id"] for h in db.history) == [r["id"] for r in db.roles if r["is_system"]]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_reconcile_is_a_noop_once_rows_are_at_the_current_version():
    db = _RolesDB(_roles_at_definition_v4())
    assert await reconcile_builtin_roles(db)
    snapshot = json.dumps(db.roles, sort_keys=True)
    history = len(db.history)
    assert await reconcile_builtin_roles(db) == []
    assert json.dumps(db.roles, sort_keys=True) == snapshot and len(db.history) == history

# ── team roles and the Servers / APM split (migrate-154) ────────────────────

from app.core.permissions import BUILTIN_ROLE_META, MODULE_AREAS, PERMISSION_MODULES  # noqa: E402

TEAM_ROLES = ("network_team", "systems_team", "application_team", "security_team")


def test_servers_and_apm_are_separate_modules():
    modules = {m["module"]: m for m in catalog()}
    assert {p["id"] for p in modules["servers"]["permissions"]} == {"servers.view", "servers.manage"}
    assert {p["id"] for p in modules["apm"]["permissions"]} == {"apm.view", "apm.manage"}
    assert PERMISSION_IMPLIES["servers.manage"] == ["servers.view"]


def test_every_module_has_an_area():
    assert set(MODULE_AREAS) == {m for m, _l, _p in PERMISSION_MODULES}
    assert all(m["area"] for m in catalog())


def test_existing_builtins_keep_server_access_after_the_split():
    for name in ("operator", "viewer", "read_only"):
        perms = set(BUILTIN_ROLE_DEFINITIONS[name])
        assert "servers.view" in perms, name
        assert ("apm.manage" in perms) == ("servers.manage" in perms), name


def test_team_roles_are_built_in_described_and_reserved():
    for name in TEAM_ROLES:
        assert name in BUILTIN_ROLE_DEFINITIONS and name in BUILTIN_ROLE_META and name in RESERVED_ROLE_NAMES
        perms = set(BUILTIN_ROLE_DEFINITIONS[name])
        assert "dashboard.view" in perms
        # Team roles never change who can do what.
        assert not perms & {"users.manage", "roles.manage", "settings.manage", SUPERUSER_PERMISSION}, name
        for p in perms:
            assert set(PERMISSION_IMPLIES.get(p, [])) <= perms, (name, p)


def test_application_and_systems_teams_are_separated():
    app_team = set(BUILTIN_ROLE_DEFINITIONS["application_team"])
    sys_team = set(BUILTIN_ROLE_DEFINITIONS["systems_team"])
    net_team = set(BUILTIN_ROLE_DEFINITIONS["network_team"])
    assert {"apm.view", "apm.manage"} <= app_team and not app_team & {"servers.view", "servers.manage"}
    assert {"servers.view", "servers.manage"} <= sys_team and not sys_team & {"apm.view", "apm.manage"}
    assert not net_team & {"servers.view", "apm.view"}
    assert not (app_team | sys_team) & {"devices.view", "netflow.view", "ncm.view", "udt.view"}
