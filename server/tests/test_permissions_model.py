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
