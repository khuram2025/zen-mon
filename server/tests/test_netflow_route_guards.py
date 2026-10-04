"""Every mutating NetFlow route declares the permission that authorises it.

Routes that declare their own ``require_permission`` skip the router's module
default (app.core.route_policy), and every NetFlow route reads flow data
through ``flow_visibility``, which already guards on ``netflow.view``. A write
route that forgot its own manage/export guard would therefore be open to any
viewer, so the guard must be declared and this test fails the build when it is
not. POSTs that only read (previews, tests, the explainer) are listed by hand.
"""

from __future__ import annotations

from app.core.route_policy import _iter_effective_routes, _walk

SAFE = {"GET", "HEAD", "OPTIONS"}

# POST routes that read or compute and change nothing.
READ_ONLY_POSTS = {
    "/api/v1/netflow/explain",
    "/api/v1/netflow/settings/ip-groups/preview",
    "/api/v1/netflow/settings/app-rules/preview",
    "/api/v1/netflow/settings/applications/test",
}


def _netflow_routes():
    from app.main import app
    for r in _iter_effective_routes(app):
        path = getattr(r, "path_format", None) or r.path
        if path.startswith("/api/v1/netflow"):
            perms: set[str] = set()
            for d in _walk(r.dependant):
                if getattr(d.call, "_zp_guard", None):
                    perms |= set(getattr(d.call, "_zp_permissions", ()) or ())
            yield path, sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS")), perms


def test_every_mutating_route_declares_more_than_view():
    unguarded = [
        f"{methods} {path}"
        for path, methods, perms in _netflow_routes()
        if any(m not in SAFE for m in methods)
        and path not in READ_ONLY_POSTS
        and perms <= {"netflow.view"}
    ]
    assert not unguarded, "mutating NetFlow routes guarded by netflow.view only: " + ", ".join(unguarded)


def test_read_only_post_allowlist_has_no_stale_entries():
    paths = {p for p, _m, _perms in _netflow_routes()}
    assert READ_ONLY_POSTS <= paths


def test_every_netflow_route_requires_netflow_view():
    missing = [path for path, _m, perms in _netflow_routes() if not perms]
    assert not missing, missing
