"""Tag-scope enforcement on the server read routes, the agent fleet and baseline results.

A user limited to tags (users.scope_tags plus roles.scope_tags) must not learn that a server outside the
scope exists. GET /servers/{id} already answers 404 for one; these tests hold the rest of the read surface to
the same rule:

* every ``GET /servers/{id}/...`` sub-route answers the same 404 "Server not found" for an out-of-scope, an
  untagged and an unknown id, and never reaches the metric store for them;
* ``GET /agent-fleet`` lists, counts and searches only agents of visible servers (an agent with no server is
  untagged, so hidden), and does not name an out-of-scope agent as the source of a clone;
* ``GET /agent-fleet/{id}`` answers 404 "Agent not found" for the same set;
* ``GET /server-baselines/{id}/results`` returns rows of visible servers only.

The routes run for real against a disposable PostgreSQL from ``pg_fixture`` (never the appliance database);
only the ClickHouse readers are replaced, by recorders, so a test can also prove they were not reached.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import servers as servers_api
from app.core import route_policy, security
from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from pg_fixture import pg_cluster, pg_db, pg_db_module  # noqa: F401  (fixtures are used by name)

COMMANDS = {"alpha": "collect_now", "alpha2": "refresh_config", "bravo": "restart_agent",
            "charlie": "upload_diagnostics"}   # agent_commands.command is a CHECK-constrained set
AGENT_UIDS = {"alpha": "uid-alpha", "alpha2": "uid-alpha-two", "bravo": "uid-bravo-secret",
              "charlie": "uid-charlie-secret"}
SERVER_NOT_FOUND = {"detail": "Server not found"}
AGENT_NOT_FOUND = {"detail": "Agent not found"}

# What must never appear in an out-of-scope caller's response, in any field.
SECRETS = (
    "bravo-out-of-scope", "bravo.secret.example", "uid-bravo-secret", "10.2.0.2",
    "charlie-untagged", "charlie.secret.example", "uid-charlie-secret",
    "uid-unlinked-pending", "unlinked.secret.example", "10.9.0.9",
    "bravo-svc", "charlie-svc", "restart_agent", "upload_diagnostics",
)


class _RealRolesSession(AsyncSession):
    """conftest resolves roles from their code definitions unless the session answers the role lookup
    itself; these tests need the real ``roles`` table (custom roles, role-level scope tags)."""

    answers_role_lookup = True


# ── The world ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def world(pg_db_module):
    db = pg_db_module
    w = SimpleNamespace(db=db)

    def server(key, name, host, tags):
        sid = uuid.uuid4()
        db.execute("INSERT INTO servers (id, display_name, hostname, tags) VALUES ($1, $2, $3, $4)",
                   sid, name, host, tags)
        setattr(w, key, sid)
        return sid

    # Tags are compared case-insensitively: "Team-A" is in the scope of a user limited to "team-a".
    server("srv_a", "alpha-in-scope", "alpha.example", ["Team-A"])
    server("srv_a2", "alpha-two-in-scope", "alpha2.example", ["team-a", "edge"])
    server("srv_b", "bravo-out-of-scope", "bravo.secret.example", ["team-b"])
    server("srv_c", "charlie-untagged", "charlie.secret.example", [])

    def agent(key, server_id, uid, host, ip, *, authorized=True):
        aid = uuid.uuid4()
        db.execute(
            "INSERT INTO agents (id, server_id, agent_uid, hostname, platform, status, last_ip, authorized_at) "
            f"VALUES ($1, $2, $3, $4, 'linux', 'online', '{ip}', "
            f"{'NOW()' if authorized else 'NULL'})",
            aid, server_id, uid, host)
        setattr(w, key, aid)
        return aid

    agent("ag_a", w.srv_a, "uid-alpha", "alpha.example", "10.1.0.1")
    agent("ag_a2", w.srv_a2, "uid-alpha-two", "alpha2.example", "10.1.0.3")
    agent("ag_b", w.srv_b, "uid-bravo-secret", "bravo.secret.example", "10.2.0.2")
    agent("ag_c", w.srv_c, "uid-charlie-secret", "charlie.secret.example", "10.3.0.3")
    # Enrolled but not linked to any server yet: no tags, so not part of any team's slice.
    agent("ag_free", None, "uid-unlinked-pending", "unlinked.secret.example", "10.9.0.9", authorized=False)

    # alpha is a clone of bravo's agent (out of alpha's scope); alpha-two is a clone of alpha's (in scope).
    for n, (src, dst, uid) in enumerate([(w.ag_b, w.ag_a, "uid-alpha"), (w.ag_a, w.ag_a2, "uid-alpha-two")]):
        db.execute(
            "INSERT INTO agent_registration_resolutions (reported_agent_uid, pending_secret_hash, action, "
            "source_agent_id, assigned_agent_id, assigned_agent_uid, conflict_revision) "
            "VALUES ($1, $2, 'register_clone', $3, $4, $5, gen_random_uuid())",
            f"reported-{n}", f"hash-{n}", src, dst, uid)

    # Per-server data behind the sub-routes; each server's rows carry its own label.
    for key, label in (("srv_a", "alpha"), ("srv_a2", "alpha2"), ("srv_b", "bravo"), ("srv_c", "charlie")):
        sid = getattr(w, key)
        db.execute("INSERT INTO server_service_inventory (server_id, service_name) VALUES ($1, $2)",
                   sid, f"{label}-svc")
        db.execute("INSERT INTO server_filesystem_inventory (server_id, mount, total_bytes, used_bytes, "
                   "free_bytes, used_pct) VALUES ($1, $2, 1000, 400, 600, 40)", sid, f"/{label}-fs")
        db.execute("INSERT INTO server_network_interface_inventory (server_id, if_name) VALUES ($1, $2)",
                   sid, f"{label}-nic")
        db.execute("INSERT INTO server_software_inventory (server_id, package_name, version) "
                   "VALUES ($1, $2, '1.0')", sid, f"{label}-pkg")
        db.execute("INSERT INTO network_captures (server_id) VALUES ($1)", sid)
    for key, command in (("ag_a", COMMANDS["alpha"]), ("ag_a2", COMMANDS["alpha2"]),
                         ("ag_b", COMMANDS["bravo"]), ("ag_c", COMMANDS["charlie"])):
        db.execute("INSERT INTO agent_commands (agent_id, command) VALUES ($1, $2)", getattr(w, key), command)

    # One baseline across every server, and a second whose only results are on the out-of-scope server.
    for key, name in (("bl", "everywhere"), ("bl_b_only", "bravo-only")):
        bid = uuid.uuid4()
        rid = uuid.uuid4()
        db.execute("INSERT INTO software_baselines (id, name, enabled) VALUES ($1, $2, TRUE)", bid, name)
        db.execute("INSERT INTO software_baseline_rules (id, baseline_id, package_match) VALUES ($1, $2, $3)",
                   rid, bid, f"{name}-pkg")
        setattr(w, key, bid)
        setattr(w, key + "_rule", rid)
    for srv, status in ((w.srv_a, "missing"), (w.srv_a2, "compliant"), (w.srv_b, "missing"),
                        (w.srv_c, "outdated")):
        db.execute("INSERT INTO server_baseline_results (server_id, rule_id, baseline_id, status) "
                   "VALUES ($1, $2, $3, $4)", srv, w.bl_rule, w.bl, status)
    db.execute("INSERT INTO server_baseline_results (server_id, rule_id, baseline_id, status) "
               "VALUES ($1, $2, $3, 'missing')", w.srv_b, w.bl_b_only_rule, w.bl_b_only)

    # Roles: a plain read role, a role limited to team-a, and an administrator.
    for name, perms, scope in (("scope_plain", ["servers.view"], []),
                               ("scope_team_a", ["servers.view"], ["team-a"]),
                               ("scope_root", ["system.admin", "servers.view"], [])):
        db.execute("INSERT INTO roles (name, display_name, permissions, scope_tags) VALUES ($1, $1, $2, $3)",
                   name, perms, scope)

    def user(role, scope):
        return SimpleNamespace(id=uuid.uuid4(), username=f"{role}-user", email=f"{role}@example.com",
                               role=role, scope_tags=scope, is_active=True, last_login=None,
                               password_hash="")

    w.unscoped = user("scope_plain", [])
    w.own_scope = user("scope_plain", ["team-a"])
    w.role_scope = user("scope_team_a", [])
    w.both_scopes = user("scope_team_a", ["team-b"])      # the union: team-a and team-b
    w.no_match = user("scope_plain", ["nobody"])
    w.admin = user("scope_root", ["team-b"])             # system.admin is never scoped
    return w


@pytest.fixture
def api(world, monkeypatch):
    """``get(user, path)`` against the real app and the disposable database. Returns the response; the
    list of ClickHouse reads made so far is ``get.ch_reads``."""
    engine = create_async_engine(world.db.url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=_RealRolesSession, expire_on_commit=False)
    current = {}
    ch_reads: list[str] = []

    async def _db():
        async with factory() as session:
            yield session

    async def _user():
        return current["user"]

    def reader(result):
        def fake(server_id, *args, **kwargs):
            ch_reads.append(str(server_id))
            return result
        return fake

    def fake_detail(client, server_id, *args, **kwargs):
        ch_reads.append(str(server_id))
        return {}

    class _Client:
        def query(self, sql, parameters=None, **kw):
            ch_reads.append(str((parameters or {}).get("sid")))
            return SimpleNamespace(result_rows=[])

    monkeypatch.setattr(servers_api, "query_server_metrics", reader([]))
    monkeypatch.setattr(servers_api, "query_process_history", lambda sid, *a, **k: (ch_reads.append(str(sid)), [])[1])
    monkeypatch.setattr(servers_api, "query_latest_process_snapshot", reader([]))
    monkeypatch.setattr(servers_api, "query_server_memory_total", reader(0))
    monkeypatch.setattr(servers_api, "query_server_metric_detail", fake_detail)
    monkeypatch.setattr(servers_api, "get_clickhouse_client", lambda: object())
    monkeypatch.setattr("app.core.database.get_ch_client", lambda: _Client())

    security.invalidate_role_cache()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)   # no context manager: startup hooks would reach the default databases

    def get(user, path, **kwargs):
        current["user"] = user
        return client.get("/api/v1" + path, **kwargs)

    get.ch_reads = ch_reads
    try:
        yield get
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        security.invalidate_role_cache()


# ── /servers/{id}/... ────────────────────────────────────────────────

# route -> (reads a visible server's body, what that body holds for the server labelled ``label``); None = status only
SERVER_ROUTES = {
    "metrics": None,
    "metrics/detail": None,
    "processes": None,
    "processes/history?name=nginx": None,
    "services": (lambda b: [i["service_name"] for i in b["items"]], lambda label: [f"{label}-svc"]),
    "filesystems": (lambda b: [i["mount"] for i in b["items"]], lambda label: [f"/{label}-fs"]),
    "network": (lambda b: [i["if_name"] for i in b["items"]], lambda label: [f"{label}-nic"]),
    "events": None,
    "software": (lambda b: [i["package_name"] for i in b["items"]], lambda label: [f"{label}-pkg"]),
    "compliance": (lambda b: sorted(i["baseline_name"] for i in b["items"]),
                   lambda label: ["bravo-only", "everywhere"] if label == "bravo" else ["everywhere"]),
    "commands": (lambda b: [i["command"] for i in b["items"]], lambda label: [COMMANDS[label]]),
    "agent": (lambda b: [b["agent_uid"]], lambda label: [AGENT_UIDS[label]]),
    "network-captures": (lambda b: len(b["items"]), lambda label: 1),
}
LABELS = {"srv_a": "alpha", "srv_a2": "alpha2", "srv_b": "bravo", "srv_c": "charlie"}
VISIBLE_TO_TEAM_A = ("srv_a", "srv_a2")
SCOPED = ("own_scope", "role_scope", "both_scopes")
ROUTE_IDS = list(SERVER_ROUTES)


def _assert_body(route, key, body):
    """A visible server's data is returned whole: the guard blocks other servers, not this one."""
    if SERVER_ROUTES[route]:
        read, expected = SERVER_ROUTES[route]
        assert read(body) == expected(LABELS[key]), (route, key)


@pytest.mark.parametrize("route", ROUTE_IDS)
@pytest.mark.parametrize("who", SCOPED)
def test_scoped_user_still_reads_servers_in_scope(api, world, who, route):
    for key in VISIBLE_TO_TEAM_A:
        r = api(getattr(world, who), f"/servers/{getattr(world, key)}/{route}")
        assert r.status_code == 200, (key, r.text)
        _assert_body(route, key, r.json())


@pytest.mark.parametrize("route", ROUTE_IDS)
@pytest.mark.parametrize("who", SCOPED[:2])
@pytest.mark.parametrize("target", ["srv_b", "srv_c", "unknown"])
def test_out_of_scope_untagged_and_unknown_servers_answer_the_same_404(api, world, who, target, route):
    sid = uuid.uuid4() if target == "unknown" else getattr(world, target)
    r = api(getattr(world, who), f"/servers/{sid}/{route}")
    assert (r.status_code, r.json()) == (404, SERVER_NOT_FOUND), r.text
    assert not any(s in r.text for s in SECRETS)
    assert api.ch_reads == [], "the metric store was reached for a server the caller may not see"


def test_the_404_for_an_out_of_scope_server_is_the_one_get_server_gives(api, world):
    own = api(world.own_scope, f"/servers/{world.srv_b}")
    unknown = api(world.own_scope, f"/servers/{uuid.uuid4()}")
    assert (own.status_code, own.json()) == (unknown.status_code, unknown.json()) == (404, SERVER_NOT_FOUND)


@pytest.mark.parametrize("route", ROUTE_IDS)
@pytest.mark.parametrize("who", ["unscoped", "admin"])
def test_unrestricted_users_read_every_server(api, world, who, route):
    for key in LABELS:
        r = api(getattr(world, who), f"/servers/{getattr(world, key)}/{route}")
        assert r.status_code == 200, (key, r.text)
        _assert_body(route, key, r.json())


@pytest.mark.parametrize("route", ROUTE_IDS)
@pytest.mark.parametrize("who", ["unscoped", "admin"])
def test_unrestricted_users_get_404_for_an_unknown_server(api, world, who, route):
    r = api(getattr(world, who), f"/servers/{uuid.uuid4()}/{route}")
    assert (r.status_code, r.json()) == (404, SERVER_NOT_FOUND)


def test_scope_is_the_union_of_user_and_role_tags(api, world):
    # both_scopes: role team-a + own team-b -> alpha, alpha-two and bravo, never the untagged server.
    for key, expected in (("srv_a", 200), ("srv_a2", 200), ("srv_b", 200), ("srv_c", 404)):
        assert api(world.both_scopes, f"/servers/{getattr(world, key)}/network").status_code == expected, key


def test_a_scope_matching_nothing_sees_no_server(api, world):
    for key in LABELS:
        assert api(world.no_match, f"/servers/{getattr(world, key)}/network").status_code == 404, key


def test_every_server_read_subroute_declares_the_scope_guard():
    """A new ``GET /servers/{server_id}/...`` route that forgets the guard fails here. The two routes that
    also need the server row for their own answer check it inline (and are covered by the matrix above)."""
    inline = {"/servers/{server_id}/metrics/detail", "/servers/{server_id}/software"}
    unguarded, found = set(), set()
    for r in route_policy._iter_effective_routes(app):
        path = getattr(r, "path_format", None) or r.path
        if "GET" not in (r.methods or ()) or "/servers/{server_id}/" not in path:
            continue
        template = "/servers/{server_id}/" + path.split("/servers/{server_id}/", 1)[1]
        found.add(template)
        if servers_api._server_in_scope not in [d.call for d in route_policy._walk(r.dependant)]:
            unguarded.add(template)
    assert found >= {f"/servers/{{server_id}}/{r.split('?')[0]}" for r in ROUTE_IDS}
    assert unguarded == inline


# ── /agent-fleet ─────────────────────────────────────────────────────

def _uids(body):
    return {i["agent_uid"] for i in body["items"]}


def test_scoped_fleet_list_holds_only_agents_of_visible_servers(api, world):
    for who in SCOPED[:2]:
        r = api(getattr(world, who), "/agent-fleet")
        assert r.status_code == 200, r.text
        body = r.json()
        assert _uids(body) == {"uid-alpha", "uid-alpha-two"}, who
        assert body["total"] == 2
        # The summary counts are the caller's slice too, not the whole fleet.
        assert body["summary"]["total"] == 2 and body["summary"]["pending_authorization"] == 0
        assert not any(s in r.text for s in SECRETS), who


def test_union_scope_fleet_adds_the_second_teams_agents(api, world):
    assert _uids(api(world.both_scopes, "/agent-fleet").json()) == {"uid-alpha", "uid-alpha-two", "uid-bravo-secret"}


def test_scoped_fleet_search_and_filters_cannot_reach_other_agents(api, world):
    for query in ("q=bravo", "q=uid-bravo-secret", "q=10.2", "authorization=pending", "q=unlinked", "q=charlie"):
        r = api(world.own_scope, "/agent-fleet?" + query)
        assert r.status_code == 200 and r.json()["items"] == [] and r.json()["total"] == 0, query
    # A filter narrows within the scope; it never widens it.
    assert _uids(api(world.own_scope, "/agent-fleet?q=alpha").json()) == {"uid-alpha", "uid-alpha-two"}


def test_unrestricted_fleet_list_still_holds_every_agent_including_unlinked(api, world):
    for who in ("unscoped", "admin"):
        body = api(getattr(world, who), "/agent-fleet").json()
        assert _uids(body) == {"uid-alpha", "uid-alpha-two", "uid-bravo-secret", "uid-charlie-secret",
                               "uid-unlinked-pending"}, who
        assert body["total"] == 5 and body["summary"]["total"] == 5
        assert body["summary"]["pending_authorization"] == 1


def test_a_scope_matching_nothing_gets_an_empty_fleet(api, world):
    body = api(world.no_match, "/agent-fleet").json()
    assert body["items"] == [] and body["total"] == 0 and body["summary"]["total"] == 0


def test_fleet_does_not_name_an_out_of_scope_agent_as_the_clone_source(api, world):
    # alpha was registered as a clone of bravo's agent; a team-a caller must not learn bravo's id or name.
    by_uid = {i["agent_uid"]: i for i in api(world.own_scope, "/agent-fleet").json()["items"]}
    assert by_uid["uid-alpha"]["clone_of_agent_id"] is None and by_uid["uid-alpha"]["clone_of_hostname"] is None
    # ...but a clone source inside the scope is still shown.
    assert by_uid["uid-alpha-two"]["clone_of_agent_id"] == str(world.ag_a)
    assert by_uid["uid-alpha-two"]["clone_of_hostname"] == "alpha.example"
    # Unrestricted callers see both.
    full = {i["agent_uid"]: i for i in api(world.unscoped, "/agent-fleet").json()["items"]}
    assert full["uid-alpha"]["clone_of_agent_id"] == str(world.ag_b)
    assert full["uid-alpha"]["clone_of_hostname"] == "bravo.secret.example"


@pytest.mark.parametrize("who", SCOPED[:2])
def test_fleet_detail_answers_404_for_agents_outside_the_scope(api, world, who):
    caller = getattr(world, who)
    for key in ("ag_a", "ag_a2"):
        assert api(caller, f"/agent-fleet/{getattr(world, key)}").status_code == 200, key
    unknown = api(caller, f"/agent-fleet/{uuid.uuid4()}")
    assert (unknown.status_code, unknown.json()) == (404, AGENT_NOT_FOUND)
    for key in ("ag_b", "ag_c", "ag_free"):     # other team, untagged server, no server at all
        r = api(caller, f"/agent-fleet/{getattr(world, key)}")
        assert (r.status_code, r.json()) == (404, AGENT_NOT_FOUND), key
        assert not any(s in r.text for s in SECRETS)


def test_fleet_detail_masks_a_foreign_clone_source_and_keeps_a_visible_one(api, world):
    own = api(world.own_scope, f"/agent-fleet/{world.ag_a}").json()
    assert own["clone_of_agent_id"] is None and own["clone_of_hostname"] is None
    assert api(world.own_scope, f"/agent-fleet/{world.ag_a2}").json()["clone_of_agent_id"] == str(world.ag_a)
    assert api(world.unscoped, f"/agent-fleet/{world.ag_a}").json()["clone_of_agent_id"] == str(world.ag_b)


@pytest.mark.parametrize("who", ["unscoped", "admin"])
def test_unrestricted_fleet_detail_reaches_every_agent(api, world, who):
    caller = getattr(world, who)
    for key in ("ag_a", "ag_b", "ag_c", "ag_free"):
        assert api(caller, f"/agent-fleet/{getattr(world, key)}").status_code == 200, key
    unknown = api(caller, f"/agent-fleet/{uuid.uuid4()}")
    assert (unknown.status_code, unknown.json()) == (404, AGENT_NOT_FOUND)


# ── /server-baselines/{id}/results ───────────────────────────────────

def _result_servers(body):
    return {i["server_name"] for i in body["items"]}


@pytest.mark.parametrize("who", SCOPED[:2])
def test_baseline_results_hold_only_visible_servers(api, world, who):
    r = api(getattr(world, who), f"/server-baselines/{world.bl}/results")
    assert r.status_code == 200, r.text
    assert _result_servers(r.json()) == {"alpha-in-scope", "alpha-two-in-scope"}
    assert not any(s in r.text for s in SECRETS)
    # The status filter narrows inside the scope.
    missing = api(getattr(world, who), f"/server-baselines/{world.bl}/results?status=missing").json()
    assert _result_servers(missing) == {"alpha-in-scope"}


@pytest.mark.parametrize("who", SCOPED[:2])
def test_a_baseline_whose_results_are_all_out_of_scope_returns_an_empty_list(api, world, who):
    r = api(getattr(world, who), f"/server-baselines/{world.bl_b_only}/results")
    assert (r.status_code, r.json()) == (200, {"items": []})


def test_unrestricted_baseline_results_hold_every_server(api, world):
    for who in ("unscoped", "admin"):
        body = api(getattr(world, who), f"/server-baselines/{world.bl}/results").json()
        assert _result_servers(body) == {"alpha-in-scope", "alpha-two-in-scope", "bravo-out-of-scope",
                                         "charlie-untagged"}, who
        missing = api(getattr(world, who), f"/server-baselines/{world.bl}/results?status=missing").json()
        assert _result_servers(missing) == {"alpha-in-scope", "bravo-out-of-scope"}


def test_union_scope_baseline_results_add_the_second_team(api, world):
    body = api(world.both_scopes, f"/server-baselines/{world.bl}/results").json()
    assert _result_servers(body) == {"alpha-in-scope", "alpha-two-in-scope", "bravo-out-of-scope"}
