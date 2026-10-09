"""Every Inventory route is guarded, and every mutating one declares more than ``inventory.view``.

The Inventory router is mounted with ``policy("inventory")`` (app.core.route_policy): a GET needs
``inventory.view`` and anything else needs ``inventory.manage``, unless the handler declares its own
``require_permission``. Export is a GET that reads reconnaissance data (serials, software lists), so it
must declare ``inventory.export`` itself: the module default would let every viewer download it.

Modelled on test_netflow_route_guards.py. The static checks need no database; the behavioural check at the
end runs the real dependency chain against a role table of its own on the private cluster.
"""

from __future__ import annotations

import pytest

from app.core.route_policy import _iter_effective_routes, _walk
from inventory_fleet import api_client, fleet_db, fleet_info, fleet_template, pinned_clock  # noqa: F401
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

SAFE = {"GET", "HEAD", "OPTIONS"}
PREFIX = "/api/v1/inventory"

# The read endpoints (core wave, software/trends/settings/export wave). Later waves append theirs here; the checks
# below are written for any route under the prefix, this list only makes a vanished route a failure.
EXPECTED_GETS = {
    f"{PREFIX}/summary",
    f"{PREFIX}/assets",
    f"{PREFIX}/assets/facets",
    f"{PREFIX}/assets/{{kind}}/{{asset_id}}",
    f"{PREFIX}/os",
    f"{PREFIX}/hardware",
    f"{PREFIX}/changes",
    f"{PREFIX}/software",
    f"{PREFIX}/software/publishers",
    f"{PREFIX}/software/{{product_key}}",
    f"{PREFIX}/software/{{product_key}}/installations",
    f"{PREFIX}/trends",
    f"{PREFIX}/settings",
    f"{PREFIX}/export",
}

# The mutating routes and the permission each must declare in its own guard.
EXPECTED_WRITES = {("PUT", f"{PREFIX}/settings"): "inventory.manage"}

# A GET that declares inventory.export in its own guard, by path.
EXPORT_PATHS = {f"{PREFIX}/export"}

# POSTs (none today) that only read; keep the list explicit like the NetFlow test.
READ_ONLY_POSTS: set[str] = set()


def _inventory_routes():
    from app.main import app
    for r in _iter_effective_routes(app):
        path = getattr(r, "path_format", None) or r.path
        if not path.startswith(PREFIX):
            continue
        perms: set[str] = set()
        explicit = False
        module = None
        for d in _walk(r.dependant):
            if getattr(d.call, "_zp_guard", None):
                explicit = True
                perms |= set(getattr(d.call, "_zp_permissions", ()) or ())
            if hasattr(d.call, "_zp_module_default"):
                module = d.call._zp_module_default
        yield path, sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS")), perms, explicit, module


def test_the_expected_read_routes_exist():
    got = {path for path, methods, *_ in _inventory_routes() if methods == ["GET"]}
    assert EXPECTED_GETS <= got, f"missing: {sorted(EXPECTED_GETS - got)}"


def test_every_inventory_route_is_covered_by_the_inventory_module_policy_or_an_explicit_guard():
    unguarded = [f"{methods} {path}" for path, methods, _p, explicit, module in _inventory_routes()
                 if not explicit and module != "inventory"]
    assert not unguarded, "Inventory routes with no guard: " + ", ".join(unguarded)


def test_every_inventory_route_needs_inventory_view_or_more():
    """Either the module default (reads: inventory.view) or an explicit guard that names an inventory permission."""
    weak = []
    for path, methods, perms, explicit, module in _inventory_routes():
        if explicit and not any(p.startswith("inventory.") for p in perms):
            weak.append(f"{methods} {path} -> {sorted(perms)}")
    assert not weak, "explicit guards that do not name an inventory permission: " + ", ".join(weak)


def test_every_mutating_route_declares_more_than_view():
    unguarded = [
        f"{methods} {path}"
        for path, methods, perms, explicit, _m in _inventory_routes()
        if any(m not in SAFE for m in methods)
        and path not in READ_ONLY_POSTS
        and (not explicit or perms <= {"inventory.view"})
    ]
    assert not unguarded, "mutating Inventory routes guarded by inventory.view only: " + ", ".join(unguarded)


def test_export_routes_declare_inventory_export_explicitly():
    for path, _m, perms, explicit, _mod in _inventory_routes():
        if path in EXPORT_PATHS or path.endswith("/export"):
            assert explicit and "inventory.export" in perms, f"{path} must declare inventory.export (has {sorted(perms)})"


def test_the_mutating_routes_declare_the_permission_that_changes_them():
    found = {}
    for path, methods, perms, explicit, _m in _inventory_routes():
        for m in methods:
            if m not in SAFE:
                found[(m, path)] = (explicit, perms)
    assert set(found) == set(EXPECTED_WRITES), f"mutating inventory routes changed: {sorted(found)}"
    for key, permission in EXPECTED_WRITES.items():
        explicit, perms = found[key]
        assert explicit and permission in perms, (key, sorted(perms))


def test_settings_reads_use_the_module_default_and_the_export_does_not():
    by_route = {(m, path): (explicit, perms, module) for path, methods, perms, explicit, module in _inventory_routes() for m in methods}
    explicit, perms, module = by_route[("GET", f"{PREFIX}/settings")]
    assert not explicit and module == "inventory"                                  # inventory.view through the router policy
    explicit, perms, module = by_route[("GET", f"{PREFIX}/export")]
    assert explicit and perms == {"inventory.export"}


def test_read_only_post_allowlist_has_no_stale_entries():
    paths = {p for p, *_ in _inventory_routes()}
    assert READ_ONLY_POSTS <= paths


def test_nothing_under_the_prefix_is_public():
    from app.core.route_policy import PUBLIC_ROUTES, SELF_ROUTES
    for path, *_ in _inventory_routes():
        assert path not in PUBLIC_ROUTES and path not in SELF_ROUTES


def test_the_router_is_mounted_exactly_once_with_the_module_policy():
    from app.main import app
    pairs = [(m, getattr(r, "path_format", None) or r.path) for r in _iter_effective_routes(app)
             if (getattr(r, "path_format", None) or r.path).startswith(PREFIX) for m in r.methods if m not in ("HEAD", "OPTIONS")]
    assert len(pairs) == len(set(pairs)), "an Inventory route is registered twice"


# ── behaviour: the real dependency chain, with roles of the test's own ───────────────

READ_URLS = [
    "/summary", "/assets", "/assets/facets", "/os", "/hardware", "/changes",
    "/assets/server/00000000-0000-0000-0000-000000000000",
    "/software", "/software/publishers", "/software/no-such-product", "/software/no-such-product/installations",
    "/trends?metric=assets", "/settings",
]


def test_inventory_view_opens_every_read_route_and_nothing_else_does(fleet_db, pinned_clock):
    with api_client(fleet_db, role="t_inv_view", permissions=["inventory.view"]) as c:
        for url in READ_URLS:
            resp = c.get(PREFIX + url)
            assert resp.status_code in (200, 404), (url, resp.status_code, resp.text)   # 404: the all-zero asset id
        assert c.get(PREFIX + "/summary").status_code == 200
    for role, perms in (("t_other_modules", ["servers.view", "devices.view", "reports.view", "udt.view"]),
                        ("t_nothing", []),
                        ("t_export_only", ["inventory.export"])):
        with api_client(fleet_db, role=role, permissions=perms) as c:
            for url in READ_URLS:
                assert c.get(PREFIX + url).status_code == 403, (role, url)


def test_a_missing_role_row_has_no_authority(fleet_db):
    """Fail closed: a user whose role does not exist reads nothing."""
    with api_client(fleet_db, role="t_deleted_role") as c:
        assert c.get(PREFIX + "/summary").status_code == 403


def test_the_built_in_roles_that_hold_inventory_view_can_read(fleet_db, pinned_clock):
    from app.core.permissions import BUILTIN_ROLE_DEFINITIONS
    for role in ("admin", "operator", "viewer", "read_only", "network_team", "systems_team", "security_team", "application_team"):
        perms = BUILTIN_ROLE_DEFINITIONS.get(role, [])
        expected = 200 if ("inventory.view" in perms or "system.admin" in perms) else 403
        with api_client(fleet_db, role=f"t_{role}", permissions=list(perms)) as c:
            assert c.get(PREFIX + "/summary").status_code == expected, role


def test_unauthenticated_requests_are_refused(fleet_db):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    for url in READ_URLS:
        assert c.get(PREFIX + url).status_code in (401, 403), url


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_a_viewer_cannot_mutate_anything_under_the_prefix(fleet_db, method):
    """T14 for the routes that exist: a write verb is a 403/404/405, never a 2xx, for a view-only role."""
    with api_client(fleet_db, role="t_inv_view", permissions=["inventory.view"]) as c:
        for url in ("/summary", "/assets", "/assets/server/00000000-0000-0000-0000-000000000000", "/settings", "/catalog/rules"):
            resp = getattr(c, method)(PREFIX + url)
            assert resp.status_code in (403, 404, 405, 422), (method, url, resp.status_code)


def test_export_is_the_one_read_route_that_needs_more_than_view(fleet_db, pinned_clock):
    """A viewer lists everything and downloads nothing; export alone is not enough to list; manage is needed to write."""
    with api_client(fleet_db, role="t_inv_view", permissions=["inventory.view"]) as c:
        for url in READ_URLS:
            assert c.get(PREFIX + url).status_code in (200, 404), url
        for dataset in ("assets", "software", "os", "hardware", "changes"):
            assert c.get(PREFIX + "/export", params={"dataset": dataset}).status_code == 403, dataset
        assert c.put(PREFIX + "/settings", json={"stale_days": 9}).status_code == 403
    with api_client(fleet_db, role="t_inv_export", permissions=["inventory.export"]) as c:
        assert c.get(PREFIX + "/export", params={"dataset": "assets"}).status_code == 200
        assert c.get(PREFIX + "/assets").status_code == 403 and c.get(PREFIX + "/software").status_code == 403
    with api_client(fleet_db, role="t_inv_manage", permissions=["inventory.manage"]) as c:
        assert c.put(PREFIX + "/settings", json={"stale_days": 7}).status_code == 200
        assert c.get(PREFIX + "/export", params={"dataset": "assets"}).status_code == 403
