"""Permission vocabulary, built-in role definitions, delegation primitives."""
from __future__ import annotations

import pytest

from app.core.permissions import (
    ALL_PERMISSIONS, BUILTIN_ROLE_DEFINITIONS, PERMISSION_IMPLIES, PERMISSION_RISK, RESERVED_ROLE_NAMES,
    SUPERUSER_PERMISSION, catalog, effective_permissions, exceeds, risk_of,
)


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
