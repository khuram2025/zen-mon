"""Route → capability contract (RP-04, RP-05).

Every API route must be one of: explicitly public, self-service, explicitly
guarded by a permission, or covered by a router module default. A route that is
merely "authenticated" (any signed-in account) or unauthenticated and not on the
allowlist fails the build, so a new endpoint cannot silently ship open.
"""
from __future__ import annotations

from app.core.permissions import ALL_PERMISSIONS
from app.core.route_policy import ANY_VIEW, PUBLIC_ROUTES, route_inventory
from app.main import create_app

_ROWS = route_inventory(create_app())


def test_no_route_is_authenticated_only_or_unclassified():
    offenders = [f"{','.join(r['methods'])} {r['path']} ({r['handler']}) -> {r['kind']}"
                 for r in _ROWS if r["kind"] in ("authenticated-only", "unauthenticated")]
    assert not offenders, "unclassified routes:\n" + "\n".join(offenders)


def test_public_allowlist_has_no_stale_entries():
    paths = {r["path"] for r in _ROWS}
    stale = sorted(p for p in PUBLIC_ROUTES if p not in paths)
    assert not stale, f"PUBLIC_ROUTES lists routes that no longer exist: {stale}"


def test_live_streams_are_guarded():
    streams = [r for r in _ROWS if r["path"].startswith("/api/v1/stream/")]
    assert len(streams) == 3
    assert all(r["kind"].startswith("guarded") for r in streams)


def test_every_permission_named_by_a_guard_exists_in_the_catalog():
    known = set(ALL_PERMISSIONS) | {ANY_VIEW}
    unknown = {p for r in _ROWS for p in r["permissions"]} - known
    assert not unknown, f"guards reference permissions that are not in the catalog: {unknown}"


def test_operator_shortcut_is_gone():
    import app.core.security as security
    assert not hasattr(security, "require_operator_user")
    assert not hasattr(security, "ADMIN_ROLES")


def test_mutating_module_routes_are_not_readable_by_view_only_defaults():
    from app.core.route_policy import _POLICIES
    for name, (read, write) in _POLICIES.items():
        assert write, name
        if name not in ("access",):
            assert not any(w.endswith(".view") and w != ANY_VIEW for w in write) or name == "link_utilization", name


def test_server_routes_need_server_permissions_not_apm():
    """migrate-154: servers.* is separate from apm.*, so an Application team
    (apm.*) sees no servers and a Systems team (servers.*) sees no APM."""
    prefixes = ("/api/v1/servers", "/api/v1/agent-policies", "/api/v1/agent-fleet",
                "/api/v1/server-monitoring", "/api/v1/server-baselines")
    server_rows = [r for r in _ROWS if r["path"].startswith(prefixes)]
    assert server_rows
    for r in server_rows:
        assert r["kind"] == "module:servers" or r["kind"].startswith("guarded"), r
        assert not any(p.startswith("apm.") for p in r["permissions"]), r
        if r["kind"].startswith("guarded"):
            assert all(p.startswith("servers.") for p in r["permissions"]), r
    apm_rows = [r for r in _ROWS if r["path"].startswith("/api/v1/apm/") and r["kind"].startswith(("module", "guarded"))]
    assert apm_rows and not any(p.startswith("servers.") for r in apm_rows for p in r["permissions"])
