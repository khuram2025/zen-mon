"""Governed NetFlow saved views (NF-05, NF-12, NF-34, FE-10): api/v1/netflow/saved_views.py.

Layers:
  * unit — the v2 saved-query schema (strict on write, lenient on read), the
    v1 -> v2 upgrade (numeric strings, ``proto`` alias, missing window), numeric
    zero, windows, sort/order/columns/path, size bound; the read/edit/visibility
    rules; exporter scoping of shared views;
  * live API with its database (NETFLOW_B3_API, e.g. http://127.0.0.1:8113, and
    NETFLOW_B3_PG, e.g. postgresql+asyncpg://postgres@127.0.0.1:55441/zenplus):
    under the released 1.23.24 role model (creating/editing/deleting any view
    needs netflow.manage, creating/changing/deleting SHARED views needs
    netflow.manage_shared) a sharer (custom role view+manage+manage_shared), an
    editor (custom role view+manage), a NetFlow manager (operator), a read-only
    viewer, a scoped editor and the administrator exercise create / list / get /
    patch / duplicate / delete, private 404s, shared 403s, moderation, audit
    rows, UUID path parameters and payload validation. Test users, the two
    custom roles and views are removed afterwards.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.v1.netflow import saved_views as SV  # noqa: E402
from app.api.v1.netflow.access import FlowVisibility  # noqa: E402
from app.api.v1.netflow.common import NetflowError  # noqa: E402


def _norm(raw, strict=True, kind="forensics"):
    return SV.normalize_query(raw, kind=kind, strict=strict)


# ── Saved-query schema ────────────────────────────────────────────────────────

def test_v2_round_trips_with_numeric_zero_and_windows():
    q = {"v": 2, "kind": "forensics", "filter": {"dscp": 0, "dst_port": 0, "src": "10.0.0.0/8", "dst_ports": [3389, 22]},
         "window": {"mode": "fixed", "from": "2026-09-01T00:00:00Z", "to": "2026-09-02T00:00:00+00:00"},
         "sort": "bytes", "order": "asc", "columns": ["timestamp", "src", "bytes"]}
    out, issues, legacy_time, version = _norm(q)
    assert version == 2 and not issues and not legacy_time
    assert out["filter"] == {"src": "10.0.0.0/8", "dst_port": 0, "dst_ports": [22, 3389], "dscp": 0}
    assert out["window"] == {"mode": "fixed", "from": "2026-09-01T00:00:00.000Z", "to": "2026-09-02T00:00:00.000Z"}
    assert (out["sort"], out["order"], out["columns"]) == ("bytes", "asc", ["timestamp", "src", "bytes"])
    again, *_ = _norm(out)
    assert again == out                                          # stable under re-normalisation
    rel, *_ = _norm({"v": 2, "kind": "overview", "filter": {}, "window": {"mode": "relative", "range": "7d"},
                     "path": "/netflow/devices/192.0.2.1"})
    assert rel["window"] == {"mode": "relative", "range": "7d"} and rel["path"] == "/netflow/devices/192.0.2.1"


def test_v1_blob_is_upgraded():
    out, issues, legacy_time, version = _norm(
        {"src": "", "dst": "", "proto": "17", "src_port": "", "dst_port": "443", "min_bytes": "", "dscp": "0",
         "exporter": "", "sort": "bytes", "order": "asc"}, strict=False)
    assert version == 1 and legacy_time and not issues
    assert out == {"v": 2, "kind": "forensics", "filter": {"protocol": 17, "dst_port": 443, "dscp": 0},
                   "window": {"mode": "relative", "range": "1h"}, "sort": "bytes", "order": "asc"}
    # The old test payload {"hours": 1, "proto": "6"} keeps working on write.
    out, _i, legacy_time, _v = _norm({"hours": 1, "proto": "6"})
    assert out["filter"] == {"protocol": 6} and out["window"] == {"mode": "relative", "range": "1h"} and not legacy_time


def test_read_is_lenient_and_reports_what_was_dropped():
    out, issues, legacy_time, _ = _norm({"v": 2, "kind": "forensics",
                                         "filter": {"src": "10.0.0.0/8", "dscp": 99, "bogus": "1"},
                                         "window": {"mode": "relative", "range": "2y"}}, strict=False)
    assert out["filter"] == {"src": "10.0.0.0/8"} and legacy_time
    assert {i["key"] for i in issues} >= {"dscp", "bogus", "query.window"}


@pytest.mark.parametrize("raw", [
    {"v": 2, "kind": "forensics", "filter": {"dscp": 64}, "window": {"mode": "relative", "range": "1h"}},
    {"v": 2, "kind": "forensics", "filter": {"dst_port": 70000}, "window": {"mode": "relative", "range": "1h"}},
    {"v": 2, "kind": "forensics", "filter": {"src": "10.1"}, "window": {"mode": "relative", "range": "1h"}},
    {"v": 2, "kind": "forensics", "filter": {"nope": 1}, "window": {"mode": "relative", "range": "1h"}},
    {"v": 2, "kind": "forensics", "filter": {}, "window": {"mode": "relative", "range": "1h"}, "evil": 1},
    {"v": 2, "kind": "forensics", "filter": {}, "window": {"mode": "fixed", "from": "2026-09-02T00:00:00Z",
                                                            "to": "2026-09-01T00:00:00Z"}},
    {"v": 2, "kind": "forensics", "filter": {}, "window": {"mode": "fixed", "from": "0001-01-01", "to": "2026-01-01"}},
    {"v": 2, "kind": "wizard", "filter": {}, "window": {"mode": "relative", "range": "1h"}},
    {"v": 2, "kind": "section", "filter": {}, "window": {"mode": "relative", "range": "1h"}, "path": "/etc/passwd"},
    {"v": 2, "kind": "section", "filter": {}, "window": {"mode": "relative", "range": "1h"}, "path": "/netflow/../x"},
    {"v": 2, "kind": "forensics", "filter": {}, "window": {"mode": "relative", "range": "1h"}, "sort": "a;drop"},
    {"v": 2, "kind": "forensics", "filter": {}, "window": {"mode": "relative", "range": "1h"},
     "columns": ["x"] * 61},
    {"hours": 5},
    "not an object",
])
def test_write_rejects_invalid_queries(raw):
    with pytest.raises(NetflowError) as exc:
        _norm(raw)
    assert exc.value.status_code == 400 and isinstance(exc.value.detail, str)


def test_write_bounds_the_query_size():
    big = {"v": 2, "kind": "forensics", "filter": {}, "window": {"mode": "relative", "range": "1h"},
           "columns": [f"c{i:02d}_{'x' * 36}" for i in range(60)]}
    out, *_ = _norm(big)                                    # 60 columns of 40 chars fit
    assert len(out["columns"]) == 60


def test_query_exporters():
    assert SV.query_exporters({"filter": {"exporter": "192.0.2.1", "iface": "3@2001:db8::1"}}) == \
        ["192.0.2.1", "2001:db8::1"]
    assert SV.query_exporters({"filter": {"iface": "3"}}) == []


# ── Access rules ──────────────────────────────────────────────────────────────

OWNER, OTHER = uuid.uuid4(), uuid.uuid4()


def _view(visibility="shared", owner=OWNER, exporter=None):
    flt = {"exporter": exporter} if exporter else {}
    return SimpleNamespace(id=uuid.uuid4(), name="v", description=None, kind="forensics", visibility=visibility,
                           pinned=False, owner_id=owner, updated_by=owner, query_version=2,
                           query={"v": 2, "kind": "forensics", "filter": flt, "window": {"mode": "relative", "range": "1h"}},
                           created_at=None, updated_at=None)


def _vis(user, perms=("netflow.view",), exporters=None):
    return FlowVisibility(restricted=exporters is not None,
                          exporters=None if exporters is None else frozenset(exporters),
                          user_id=str(user) if user else None, permissions=frozenset(perms))


def test_read_edit_matrix():
    owner, other = _vis(OWNER), _vis(OTHER)
    manager = _vis(OTHER, ("netflow.view", "netflow.manage"))
    sharer = _vis(OTHER, ("netflow.view", "netflow.manage", "netflow.manage_shared"))
    owner_sharer = _vis(OWNER, ("netflow.view", "netflow.manage", "netflow.manage_shared"))
    admin = _vis(OTHER, ("system.admin",))
    private, shared, orphan = _view("private"), _view("shared"), _view("shared", owner=None)
    assert SV.can_read(private, owner) and not SV.can_read(private, other) and not SV.can_read(private, manager)
    assert SV.can_read(private, admin)
    assert SV.can_read(shared, other) and not SV.can_edit(shared, other)       # shared read != shared delete
    # Released 1.23.24 rule: a shared view changes only with netflow.manage_shared, whoever made it.
    assert not SV.can_edit(shared, owner) and not SV.can_edit(shared, manager)
    assert SV.can_edit(shared, sharer) and SV.can_edit(shared, owner_sharer) and SV.can_edit(shared, admin)
    assert not SV.can_edit(private, manager) and not SV.can_edit(private, sharer) and SV.can_edit(private, owner)
    assert SV.can_edit(orphan, sharer) and not SV.can_edit(orphan, manager)
    # Visibility can move only for the owner (or an administrator) who may also edit it.
    assert SV.can_change_visibility(shared, owner_sharer) and not SV.can_change_visibility(shared, sharer)
    assert SV.can_change_visibility(shared, admin) and SV.can_change_visibility(private, owner)
    assert not SV.can_share(owner) and not SV.can_share(manager)
    assert SV.can_share(sharer) and SV.can_share(admin)


def test_shared_views_follow_the_exporter_scope():
    scoped = _vis(OTHER, exporters={"192.0.2.1"})
    assert SV.can_read(_view("shared", exporter="192.0.2.1"), scoped)
    assert not SV.can_read(_view("shared", exporter="192.0.2.9"), scoped)
    assert SV.can_read(_view("shared"), scoped)                                # no exporter named
    nothing = _vis(OTHER, exporters=set())
    assert not SV.can_read(_view("shared", exporter="192.0.2.1"), nothing)


def test_serialize_exposes_owner_and_permissions():
    v = _view("shared")
    out = SV.serialize(v, _vis(OTHER), {str(OWNER): "alice"})
    assert out["owner_username"] == "alice" and out["is_owner"] is False
    assert out["can_edit"] is False and out["can_delete"] is False and out["query"]["v"] == 2


# ── Live API ──────────────────────────────────────────────────────────────────

API = os.environ.get("NETFLOW_B3_API")
PG = os.environ.get("NETFLOW_B3_PG")
ADMIN = (os.environ.get("NETFLOW_B3_USER", "admin"), os.environ.get("NETFLOW_B3_PASS", "admin123"))
PASS = "B3-Views-Passw0rd!"
# Custom roles (the users.role FK points at the roles table). The released
# viewer/read_only roles hold only netflow.view and cannot create views.
SHARER_ROLE, EDITOR_ROLE = "b3_sv_sharer", "b3_sv_editor"
ROLES = {SHARER_ROLE: ["netflow.view", "netflow.manage", "netflow.manage_shared"],
         EDITOR_ROLE: ["netflow.view", "netflow.manage"]}
USERS = {"b3_sv_alice": (SHARER_ROLE, []),            # owner who may also share
         "b3_sv_bob": (EDITOR_ROLE, []),              # manages private views, may not share
         "b3_sv_mgr": ("operator", []),               # holds netflow.manage_shared (moderator)
         "b3_sv_ro": ("viewer", []),                  # netflow.view only
         "b3_sv_scoped": (EDITOR_ROLE, ["b3-no-such-team"])}


def _api_up() -> bool:
    if not (API and PG):
        return False
    import requests

    try:
        requests.get(f"{API}/api/v1/health", timeout=3)
        return True
    except Exception:
        return False


live = pytest.mark.skipif(not _api_up(), reason="set NETFLOW_B3_API and NETFLOW_B3_PG")


def _run_pg(statements):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run():
        engine = create_async_engine(PG)
        out = []
        try:
            async with engine.begin() as conn:
                for sql, params in statements:
                    res = await conn.execute(text(sql), params)
                    out.append(res.fetchall() if res.returns_rows else None)
        finally:
            await engine.dispose()
        return out

    return asyncio.run(run())


def _login(user, password=PASS):
    import requests

    r = requests.post(f"{API}/api/v1/auth/login", json={"username": user, "password": password}, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def tokens():
    import json

    from app.core.security import hash_password

    names = list(USERS)
    cleanup = [
        ("DELETE FROM netflow_saved_views WHERE owner_id IN (SELECT id FROM users WHERE username = ANY(:u)) "
         "OR name LIKE 'b3-sv-%'", {"u": names}),
        ("DELETE FROM users WHERE username = ANY(:u)", {"u": names}),
        ("DELETE FROM roles WHERE name = ANY(:r)", {"r": list(ROLES)}),
    ]
    _run_pg(cleanup)
    _run_pg([("INSERT INTO roles (name, display_name, description, permissions, is_system) "
              "VALUES (:n, :n, 'b3 saved-view test role', CAST(:p AS jsonb), false)",
              {"n": n, "p": json.dumps(p)}) for n, p in ROLES.items()])
    _run_pg([("INSERT INTO users (username, email, password_hash, role, scope_tags) VALUES "
              "(:u, :m, :h, :r, CAST(:s AS jsonb))",
              {"u": u, "m": f"{u}@example.test", "h": hash_password(PASS), "r": role, "s": json.dumps(scope)})
             for u, (role, scope) in USERS.items()])
    out = {u: _login(u) for u in USERS}
    out["admin"] = _login(*ADMIN)
    yield out
    _run_pg(cleanup)


def _req(method, headers, path, **kw):
    import requests

    return requests.request(method, f"{API}/api/v1/netflow{path}", headers=headers, timeout=30, **kw)


def _query(**flt):
    return {"v": 2, "kind": "forensics", "filter": flt, "window": {"mode": "relative", "range": "24h"},
            "sort": "bytes", "order": "asc"}


@live
def test_private_and_shared_views_end_to_end(tokens):
    alice, bob, mgr, admin = tokens["b3_sv_alice"], tokens["b3_sv_bob"], tokens["b3_sv_mgr"], tokens["admin"]
    ro = tokens["b3_sv_ro"]
    # Released rule: a view is created only with netflow.manage; a netflow.view-only user can read, not write.
    denied = _req("POST", ro, "/saved-views", json={"name": "b3-sv-ro", "query": _query()})
    assert denied.status_code == 403, denied.text
    r = _req("POST", alice, "/saved-views", json={"name": "  b3-sv-private  ", "query": _query(dst_port=443, dscp=0)})
    assert r.status_code == 200, r.text
    private = r.json()
    assert private["name"] == "b3-sv-private" and private["visibility"] == "private"
    assert private["query"]["filter"] == {"dst_port": 443, "dscp": 0} and private["owner_username"] == "b3_sv_alice"
    pid = private["id"]
    # Bob cannot see, open, change or delete Alice's private view (404, not 403).
    assert pid not in {v["id"] for v in _req("GET", bob, "/saved-views").json()}
    for method, path, kw in (("GET", f"/saved-views/{pid}", {}), ("PATCH", f"/saved-views/{pid}", {"json": {"name": "x"}}),
                             ("DELETE", f"/saved-views/{pid}", {}), ("POST", f"/saved-views/{pid}/duplicate", {})):
        assert _req(method, bob, path, **kw).status_code == 404, (method, path)
    # A manager (even with manage_shared) and a read-only viewer do not see private views; an administrator does.
    assert _req("GET", mgr, f"/saved-views/{pid}").status_code == 404
    assert _req("GET", ro, f"/saved-views/{pid}").status_code == 404
    assert _req("GET", admin, f"/saved-views/{pid}").status_code == 200

    # Sharing needs netflow.manage_shared: Bob (view+manage) can neither create a shared view nor share one.
    bob_private = _req("POST", bob, "/saved-views", json={"name": "b3-sv-bob-priv", "query": _query()})
    assert bob_private.status_code == 200, bob_private.text
    bpid = bob_private.json()["id"]
    assert bob_private.json()["can_edit"] is True
    assert _req("POST", bob, "/saved-views", json={"name": "b3-sv-bob-shared", "visibility": "shared",
                                                   "query": _query()}).status_code == 403
    assert _req("PATCH", bob, f"/saved-views/{bpid}", json={"visibility": "shared"}).status_code == 403
    assert _req("GET", bob, f"/saved-views/{bpid}").json()["visibility"] == "private"
    assert _req("DELETE", bob, f"/saved-views/{bpid}").status_code == 200

    # Alice (manage_shared) shares hers: Bob and the viewer can read it (owner shown) but not change or delete it.
    r = _req("PATCH", alice, f"/saved-views/{pid}", json={"visibility": "shared", "description": "team view", "pinned": True})
    assert r.status_code == 200 and r.json()["visibility"] == "shared" and r.json()["pinned"] is True
    assert r.json()["can_edit"] is True and r.json()["can_delete"] is True
    listed = {v["id"]: v for v in _req("GET", bob, "/saved-views").json()}
    assert listed[pid]["owner_username"] == "b3_sv_alice" and listed[pid]["can_delete"] is False
    assert listed[pid]["can_edit"] is False
    assert pid in {v["id"] for v in _req("GET", ro, "/saved-views").json()}
    assert _req("PATCH", bob, f"/saved-views/{pid}", json={"name": "vandal"}).status_code == 403
    assert _req("DELETE", bob, f"/saved-views/{pid}").status_code == 403
    assert _req("PATCH", ro, f"/saved-views/{pid}", json={"name": "vandal"}).status_code == 403
    assert _req("DELETE", ro, f"/saved-views/{pid}").status_code == 403
    # Bob can duplicate it into a private copy of his own; a read-only viewer cannot (it creates a view).
    assert _req("POST", ro, f"/saved-views/{pid}/duplicate").status_code == 403
    dup = _req("POST", bob, f"/saved-views/{pid}/duplicate").json()
    assert dup["owner_username"] == "b3_sv_bob" and dup["visibility"] == "private" and dup["name"] == "Copy of b3-sv-private"
    assert dup["query"] == listed[pid]["query"]
    # The manager (netflow.manage_shared) moderates the shared view, but cannot make it private (owner/admin only).
    assert _req("PATCH", mgr, f"/saved-views/{pid}", json={"name": "b3-sv-moderated"}).status_code == 200
    assert _req("PATCH", mgr, f"/saved-views/{pid}", json={"visibility": "private"}).status_code == 403
    # Search and grouping inputs.
    found = _req("GET", bob, "/saved-views", params={"q": "moderated"}).json()
    assert [v["id"] for v in found] == [pid]
    mine = _req("GET", bob, "/saved-views", params={"mine": "true"}).json()
    assert {v["id"] for v in mine} == {dup["id"]}
    assert _req("DELETE", mgr, f"/saved-views/{pid}").status_code == 200
    assert _req("GET", alice, f"/saved-views/{pid}").status_code == 404
    assert _req("DELETE", bob, f"/saved-views/{dup['id']}").status_code == 200

    audit = _run_pg([("SELECT action, actor_username FROM audit_logs WHERE resource_type = 'netflow_saved_view' "
                      "AND resource_id = ANY(:ids) ORDER BY created_at", {"ids": [pid, dup["id"]]})])[0]
    actions = [(a, u) for a, u in audit]
    assert ("netflow.saved_view.create", "b3_sv_alice") in actions
    assert ("netflow.saved_view.update", "b3_sv_mgr") in actions
    assert ("netflow.saved_view.duplicate", "b3_sv_bob") in actions
    assert ("netflow.saved_view.delete", "b3_sv_mgr") in actions and ("netflow.saved_view.delete", "b3_sv_bob") in actions


@live
def test_scoped_viewer_does_not_see_other_teams_exporter_views(tokens):
    alice, scoped = tokens["b3_sv_alice"], tokens["b3_sv_scoped"]
    r = _req("POST", alice, "/saved-views",
             json={"name": "b3-sv-exporter", "visibility": "shared", "query": _query(exporter="127.0.0.61")})
    assert r.status_code == 200, r.text
    vid = r.json()["id"]
    plain = _req("POST", alice, "/saved-views", json={"name": "b3-sv-plain", "visibility": "shared", "query": _query()}).json()
    listed = {v["id"] for v in _req("GET", scoped, "/saved-views").json()}
    assert vid not in listed and plain["id"] in listed
    assert _req("GET", scoped, f"/saved-views/{vid}").status_code == 404
    # A scoped user cannot save a view naming an exporter outside their scope (the exporter stays hidden: 404)
    # and, lacking manage_shared, cannot publish one either.
    assert _req("POST", scoped, "/saved-views",
                json={"name": "b3-sv-probe", "query": _query(exporter="127.0.0.61")}).status_code == 404
    assert _req("POST", scoped, "/saved-views",
                json={"name": "b3-sv-probe", "visibility": "shared", "query": _query()}).status_code == 403
    env = _req("GET", scoped, "/saved-views", params={"envelope": 1}).json()
    assert env["meta"]["restricted"] is True
    for i in (vid, plain["id"]):
        assert _req("DELETE", alice, f"/saved-views/{i}").status_code == 200


@live
@pytest.mark.parametrize("body", [
    {"name": "   ", "query": {"hours": 1}},
    {"name": "", "query": {"hours": 1}},
    {"name": "x" * 121, "query": {"hours": 1}},
    {"name": "b3-sv-bad", "query": {"v": 2, "kind": "forensics", "filter": {"dscp": 64},
                                    "window": {"mode": "relative", "range": "1h"}}},
    {"name": "b3-sv-bad", "query": {"v": 2, "kind": "forensics", "filter": {"src": "not-an-ip"},
                                    "window": {"mode": "relative", "range": "1h"}}},
    {"name": "b3-sv-bad", "query": {"v": 2, "kind": "forensics", "filter": {}, "window": {"mode": "relative",
                                                                                          "range": "1h"}, "x": 1}},
    {"name": "b3-sv-bad", "query": {"hours": 1}, "owner_id": str(uuid.uuid4())},
    {"name": "b3-sv-bad", "query": {"hours": 1}, "visibility": "public"},
])
def test_invalid_payloads_are_400_with_a_string_detail(tokens, body):
    r = _req("POST", tokens["b3_sv_alice"], "/saved-views", json=body)
    assert r.status_code == 400, r.text
    assert isinstance(r.json()["detail"], str)


@live
def test_oversized_body_and_non_uuid_ids(tokens):
    alice = tokens["b3_sv_alice"]
    huge = {"name": "b3-sv-huge", "query": {"v": 2, "kind": "forensics", "filter": {},
                                            "window": {"mode": "relative", "range": "1h"}, "pad": "x" * 2_000_000}}
    assert _req("POST", alice, "/saved-views", json=huge).status_code == 413
    for method in ("GET", "PATCH", "DELETE"):
        r = _req(method, alice, "/saved-views/not-a-uuid", **({"json": {}} if method == "PATCH" else {}))
        assert r.status_code == 400 and isinstance(r.json()["detail"], str), method
    assert _req("DELETE", alice, f"/saved-views/{uuid.uuid4()}").status_code == 404


@live
def test_legacy_rows_are_upgraded_on_read(tokens):
    alice = tokens["b3_sv_alice"]
    vid = uuid.uuid4()
    _run_pg([("INSERT INTO netflow_saved_views (id, name, query, pinned, owner_id, is_shared, kind, query_version) "
              "SELECT :id, 'b3-sv-legacy', CAST(:q AS json), false, id, false, 'forensics', 1 "
              "FROM users WHERE username = 'b3_sv_alice'",
              {"id": vid, "q": '{"src":"","dst":"","proto":"17","src_port":"","dst_port":"443","min_bytes":"",'
                               '"dscp":"0","exporter":"","sort":"bytes","order":"asc"}'})])
    v = _req("GET", alice, f"/saved-views/{vid}").json()
    assert v["query_version"] == 1 and v["legacy_time"] is True and v["query_issues"] == []
    assert v["query"]["filter"] == {"protocol": 17, "dst_port": 443, "dscp": 0}
    assert (v["query"]["sort"], v["query"]["order"]) == ("bytes", "asc")
    # Saving it again stores the v2 form.
    r = _req("PATCH", alice, f"/saved-views/{vid}", json={"query": v["query"]})
    assert r.status_code == 200 and r.json()["query_version"] == 2
    assert _req("DELETE", alice, f"/saved-views/{vid}").status_code == 200


@live
def test_role_without_netflow_view_is_403(tokens):
    import requests

    # No token at all is a 401; the router-level permission is covered in test_netflow_core.
    r = requests.get(f"{API}/api/v1/netflow/saved-views", timeout=10)
    assert r.status_code in (401, 403)
