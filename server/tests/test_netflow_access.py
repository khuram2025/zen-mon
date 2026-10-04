"""NetFlow visibility and permissions (NF-04 / CR-04): api/v1/netflow/access.py.

Layers:
  * unit: canonical addresses, FlowVisibility, the ClickHouse predicates, the
    404/400 detail helper, the ownership-write invalidation hook and the
    route inventory (every /netflow route requires netflow.view);
  * PostgreSQL (gated on ZENPLUS_NETFLOW_TEST_PG, a DISPOSABLE database with
    migrate-138 applied, e.g. postgresql+asyncpg://postgres@127.0.0.1:55441/a3b_mig_test):
    the ownership resolver (explicit mapping beats management-IP match),
    per-user visibility (scoped, unscoped, admin, fail-closed for unowned and
    untagged), cache invalidation on committed tag/mapping writes, device and
    mapping-write authorization;
  * HTTP end to end (gated on ZENPLUS_NETFLOW_E2E_URL + ZENPLUS_NETFLOW_E2E_PG,
    the API's own database): a scoped viewer whose device owns exporter
    127.0.0.61 sees only that exporter's flows on overview, forensics,
    capacity, anomalies and the exporter list; another exporter is a 404.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.netflow import access
from app.api.v1.netflow.access import FlowVisibility


def _vis(exporters=None, perms=("netflow.view",)):
    return FlowVisibility(
        restricted=exporters is not None,
        exporters=None if exporters is None else frozenset(exporters),
        user_id=None,
        permissions=frozenset(perms),
    )


# ── Unit ─────────────────────────────────────────────────────────────────────

def test_canonical_ip_unifies_spellings():
    assert access.canonical_ip("10.0.0.1/32") == "10.0.0.1"
    assert access.canonical_ip("::ffff:10.0.0.1") == "10.0.0.1"
    assert access.canonical_ip(" 2001:DB8:0::1 ") == "2001:db8::1"
    assert access.canonical_ip("not-an-ip") is None
    assert access.canonical_ip("") is None and access.canonical_ip(None) is None


def test_flow_visibility_semantics():
    open_vis = _vis(None)
    assert open_vis.allows("192.0.2.1") and not open_vis.is_empty and open_vis.exporter_list() is None
    scoped = _vis({"10.0.0.1", "2001:db8::1"})
    assert scoped.allows("::ffff:10.0.0.1") and scoped.allows("2001:DB8::1")
    assert not scoped.allows("10.0.0.2") and not scoped.allows("garbage")
    assert scoped.exporter_list() == ["10.0.0.1", "2001:db8::1"]
    assert _vis(set()).is_empty
    assert _vis(None, ("system.admin",)).can("netflow.manage")
    assert not _vis(None).can("netflow.manage")
    assert access.can_acknowledge_findings(_vis(None, ("alerts.acknowledge",)))
    assert access.can_acknowledge_findings(_vis(None, ("netflow.manage",)))
    assert not access.can_acknowledge_findings(_vis(None))


def test_visibility_predicate_never_widens():
    assert access.visibility_predicate(_vis(None)) == ("", {})
    # Empty scope: a false predicate, never an unfiltered query.
    assert access.visibility_predicate(_vis(set())) == ("0", {})
    sql, params = access.visibility_predicate(_vis({"10.0.0.1", "2001:db8::1"}), param="v")
    assert sql == "((exporter_ip6 = toIPv6('::') AND exporter_ip IN %(v_v4)s) OR exporter_ip6 IN %(v_v6)s)"
    assert params == {"v_v4": ["10.0.0.1"], "v_v6": ["2001:db8::1"]}
    # A table without IPv6 exporter column cannot hold the IPv6 exporter.
    sql, params = access.visibility_predicate(_vis({"2001:db8::1"}), v6_col=None)
    assert (sql, params) == ("0", {})
    sql, _ = access.visibility_predicate(_vis({"10.0.0.1"}), v6_col=None)
    assert sql == "(exporter_ip IN %(vis_exporters_v4)s)"
    assert access.exporters_predicate(["bogus"]) == ("0", {})


def test_require_exporter_visible_is_404_not_403():
    scoped = _vis({"10.0.0.1"})
    assert access.require_exporter_visible(scoped, "::ffff:10.0.0.1") == "10.0.0.1"
    with pytest.raises(HTTPException) as hidden:
        access.require_exporter_visible(scoped, "10.0.0.2")
    assert hidden.value.status_code == 404
    with pytest.raises(HTTPException) as bad:
        access.require_exporter_visible(scoped, "10.0.0")
    assert bad.value.status_code == 400
    assert access.require_exporter_visible(_vis(None), "192.0.2.1") == "192.0.2.1"


def test_ownership_write_statements_are_recognised():
    for stmt in (
        "UPDATE devices SET tags=$1::JSONB WHERE devices.id = $2::UUID",
        "UPDATE devices SET hostname=$1::VARCHAR, ip_address=$2::INET, updated_at=now() WHERE devices.id = $3",
        'INSERT INTO devices (id, hostname) VALUES ($1, $2)',
        "DELETE FROM devices WHERE id = $1",
        "UPDATE devices SET tags = (SELECT ...) WHERE tags ? :old",
        "UPDATE devices SET status = (SELECT s FROM x WHERE y), tags = $1 WHERE id = $2",
        "UPDATE devices d SET d.tags = $1 WHERE d.id = $2",
        "INSERT INTO netflow_exporter_devices (exporter_ip, device_id) VALUES (...) ON CONFLICT",
        "UPDATE netflow_exporter_devices SET note = $1",
        "delete from public.netflow_exporter_devices where exporter_ip = $1",
        "WITH x AS (SELECT 1) UPDATE devices SET ip_address = $1",
        "TRUNCATE devices CASCADE",
    ):
        assert access.is_ownership_write(stmt), stmt
    for stmt in (
        "SELECT * FROM devices",
        "UPDATE device_interfaces SET configured_speed_bps = 1",
        "UPDATE devices_status SET x = 1",
        "INSERT INTO audit_logs (resource_type) VALUES ('devices')",
        # Status traffic (sensors, health sweeps) must not flush the cache,
        # even when it mentions the columns outside an assignment.
        "UPDATE devices SET status = 'unknown', updated_at = NOW() WHERE ip_address = $1",
        "WITH m AS (SELECT jsonb_exists(md.tags, 'x') FROM devices md) UPDATE devices d SET "
        "status = $1, last_seen = $2 FROM m WHERE d.id = $3 RETURNING c.ip_address, d.tags",
    ):
        assert not access.is_ownership_write(stmt), stmt


def test_hook_invalidates_on_commit_only():
    conn = SimpleNamespace(info={})
    gen = access._cache.generation
    access._on_execute(conn, None, "UPDATE devices SET tags = $1", None, None, False)
    access._on_rollback(conn)
    access._on_commit(conn)
    assert access._cache.generation == gen          # rolled back: nothing changed
    access._on_execute(conn, None, "UPDATE devices SET tags = $1", None, None, False)
    assert access._cache.generation == gen          # not committed yet
    access._on_commit(conn)
    assert access._cache.generation == gen + 1
    access._on_execute(conn, None, "SELECT 1 FROM devices", None, None, False)
    access._on_commit(conn)
    assert access._cache.generation == gen + 1


def test_every_netflow_route_requires_netflow_view():
    """A route added later cannot skip the NetFlow permission."""
    from app.api.v1.netflow import router

    def calls(dependant):
        for dep in dependant.dependencies:
            yield dep.call
            yield from calls(dep)

    missing = [
        f"{sorted(r.methods)} {r.path}" for r in router.routes
        if access.require_view not in set(calls(r.dependant))
    ]
    assert router.routes and not missing, missing


def test_permissions_catalog_has_netflow_manage_and_export():
    from app.core.permissions import LEGACY_ROLE_PERMISSIONS, is_known_permission

    assert is_known_permission("netflow.manage") and is_known_permission("netflow.export")
    for role in ("operator", "editor"):
        assert {"netflow.manage", "netflow.export"} <= set(LEGACY_ROLE_PERMISSIONS[role])
    for role in ("viewer", "read_only"):
        assert "netflow.manage" not in LEGACY_ROLE_PERMISSIONS[role]


# ── PostgreSQL ───────────────────────────────────────────────────────────────

PG_DSN = os.environ.get("ZENPLUS_NETFLOW_TEST_PG")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="set ZENPLUS_NETFLOW_TEST_PG (disposable DB with migrate-138)")

TAG_A, TAG_B = "a3b-team-a", "a3b-team-b"


async def _fixture(db, text):
    """Devices A (team-a, 10.200.1.1), B (team-b, 10.200.1.2), C (untagged,
    10.200.1.3); exporter 127.0.0.61 and B's management IP mapped to A."""
    ids = {k: uuid.uuid4() for k in ("A", "B", "C")}
    for key, ip, tags in (("A", "10.200.1.1", [TAG_A]), ("B", "10.200.1.2", [TAG_B]), ("C", "10.200.1.3", [])):
        await db.execute(text(
            "INSERT INTO devices (id, hostname, ip_address, tags) "
            "VALUES (:id, :h, CAST(:ip AS inet), CAST(:tags AS jsonb))"),
            {"id": ids[key], "h": f"a3b-dev-{key.lower()}", "ip": ip, "tags": json.dumps(tags)})
    for exporter in ("127.0.0.61", "10.200.1.2"):
        await db.execute(text(
            "INSERT INTO netflow_exporter_devices (exporter_ip, device_id) VALUES (CAST(:e AS inet), :d)"),
            {"e": exporter, "d": ids["A"]})
    await db.commit()
    return ids


async def _cleanup(db, text):
    await db.execute(text("DELETE FROM netflow_exporter_devices WHERE host(exporter_ip) IN ('127.0.0.61', '10.200.1.2', '10.200.1.3')"))
    await db.execute(text("DELETE FROM devices WHERE hostname LIKE 'a3b-dev-%'"))
    await db.execute(text("DELETE FROM roles WHERE name LIKE 'a3b_%'"))
    await db.commit()


def _user(role, scope=()):
    return SimpleNamespace(id=uuid.uuid4(), role=role, scope_tags=list(scope), username=f"a3b-{role}")


@needs_pg
def test_ownership_and_visibility_matrix():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.core.security import invalidate_role_cache

    async def run():
        engine = create_async_engine(PG_DSN)
        try:
            async with AsyncSession(engine) as db:
                await _cleanup(db, text)
                await db.execute(text(
                    "INSERT INTO roles (name, display_name, permissions) VALUES "
                    "('a3b_noflow', 'A3b no NetFlow', '[\"dashboard.view\", \"devices.view\"]'::jsonb), "
                    "('a3b_manager', 'A3b NetFlow manager', '[\"netflow.view\", \"netflow.manage\"]'::jsonb)"))
                await db.commit()
                invalidate_role_cache()
                access.invalidate_visibility_cache()
                ids = await _fixture(db, text)
                a, b, c = (str(ids[k]) for k in "ABC")

                owners = await access.resolve_exporter_owners(db)
                assert owners["127.0.0.61"].device_id == a and owners["127.0.0.61"].source == "mapped"
                # An explicit mapping beats B's management-IP match (X-46).
                assert owners["10.200.1.2"].device_id == a
                assert owners["10.200.1.2"].overrides_device_id == b
                assert owners["10.200.1.1"].source == "ip-match" and owners["10.200.1.1"].device_id == a
                assert owners["10.200.1.3"].device_id == c
                assert "127.0.0.99" not in owners
                assert access.device_exporters(owners, a) == ["10.200.1.1", "10.200.1.2", "127.0.0.61"]
                assert access.device_exporters(owners, b) == []
                subset = await access.resolve_exporter_owners(db, ["::ffff:127.0.0.61", "127.0.0.99"])
                assert list(subset) == ["127.0.0.61"]

                team_a = await access.visibility_for_user(db, _user("viewer", [TAG_A]))
                assert team_a.restricted and team_a.exporters == {"10.200.1.1", "10.200.1.2", "127.0.0.61"}
                assert not team_a.allows("127.0.0.99")      # unowned: fail-closed
                assert not team_a.allows("10.200.1.3")      # owner untagged: fail-closed
                team_b = await access.visibility_for_user(db, _user("viewer", [TAG_B.upper()]))
                assert team_b.exporters == frozenset()        # B's only exporter was re-mapped to A
                assert access.visibility_predicate(team_b) == ("0", {})
                nobody = await access.visibility_for_user(db, _user("viewer", ["no-such-team"]))
                assert nobody.is_empty
                assert (await access.visibility_for_user(db, _user("viewer"))).exporters is None
                assert (await access.visibility_for_user(db, _user("admin", [TAG_B]))).exporters is None

                # Committed tag and mapping writes invalidate the cache at once.
                gen = access._cache.generation
                await db.execute(text("UPDATE devices SET tags = CAST(:t AS jsonb) WHERE id = :id"),
                                 {"t": f'["{TAG_A}"]', "id": ids["C"]})
                assert access._cache.generation == gen      # not before commit
                await db.commit()
                assert access._cache.generation > gen
                again = await access.visibility_for_user(db, _user("viewer", [TAG_A]))
                assert "10.200.1.3" in again.exporters
                await db.execute(text("DELETE FROM netflow_exporter_devices WHERE host(exporter_ip) = '10.200.1.2'"))
                await db.commit()
                team_b = await access.visibility_for_user(db, _user("viewer", [TAG_B]))
                assert team_b.exporters == {"10.200.1.2"}  # ip-match to B again

                # Device checks: 404 whether out of scope, unknown or malformed.
                scoped_viewer = _user("viewer", [TAG_A])
                assert (await access.require_device_visible(db, scoped_viewer, ids["A"])).hostname == "a3b-dev-a"
                for bad in (ids["B"], uuid.uuid4(), "not-a-uuid"):
                    with pytest.raises(HTTPException) as e:
                        await access.require_device_visible(db, scoped_viewer, bad)
                    assert e.value.status_code == 404

                # Mapping writes: netflow.manage, a visible exporter, a visible target.
                manager = _user("a3b_manager", [TAG_A])
                mvis = await access.visibility_for_user(db, manager)
                ip, dev = await access.authorize_mapping_write(db, manager, mvis, "127.0.0.61", ids["A"])
                assert ip == "127.0.0.61" and dev.id == a
                for exporter, target, code in (
                    ("127.0.0.99", ids["A"], 404),     # unowned: a scoped manager cannot claim it
                    ("10.200.1.2", ids["A"], 404),     # owned by team B now
                    ("127.0.0.61", ids["B"], 404),     # target device out of scope
                ):
                    with pytest.raises(HTTPException) as e:
                        await access.authorize_mapping_write(db, manager, mvis, exporter, target)
                    assert e.value.status_code == code, (exporter, target)
                viewer_vis = await access.visibility_for_user(db, scoped_viewer)
                with pytest.raises(HTTPException) as e:
                    await access.authorize_mapping_write(db, scoped_viewer, viewer_vis, "127.0.0.61", ids["A"])
                assert e.value.status_code == 403
                unscoped_mgr = _user("a3b_manager")
                uvis = await access.visibility_for_user(db, unscoped_mgr)
                assert (await access.authorize_mapping_write(db, unscoped_mgr, uvis, "127.0.0.99"))[0] == "127.0.0.99"

                # The FastAPI dependency enforces netflow.view (403) before scoping.
                dep = access.require_view
                with pytest.raises(HTTPException) as e:
                    await dep(user=_user("a3b_noflow"), db=db)
                assert e.value.status_code == 403
                assert (await access.flow_visibility(user=scoped_viewer, db=db)).restricted

                await _cleanup(db, text)
        finally:
            access.invalidate_visibility_cache()
            invalidate_role_cache()
            await engine.dispose()

    asyncio.run(run())


# ── HTTP end to end ──────────────────────────────────────────────────────────

E2E_URL = os.environ.get("ZENPLUS_NETFLOW_E2E_URL")
E2E_PG = os.environ.get("ZENPLUS_NETFLOW_E2E_PG")
E2E_EXPORTER = os.environ.get("ZENPLUS_NETFLOW_E2E_EXPORTER", "127.0.0.61")
E2E_OTHER = os.environ.get("ZENPLUS_NETFLOW_E2E_OTHER", "127.0.0.62")
needs_e2e = pytest.mark.skipif(not (E2E_URL and E2E_PG),
                               reason="set ZENPLUS_NETFLOW_E2E_URL and ZENPLUS_NETFLOW_E2E_PG")

E2E_USER, E2E_PASS, E2E_TAG = "a3b_scoped_viewer", "A3b-Scoped-Passw0rd!", "a3b-e2e-team"
# A viewer whose scope matches no device, and a role without netflow.view.
E2E_NOMATCH, E2E_NOFLOW, E2E_NOFLOW_ROLE = "a3b_nomatch_viewer", "a3b_noflow_user", "a3b_e2e_noflow"


def _run_pg(statements):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run():
        engine = create_async_engine(E2E_PG)
        try:
            async with engine.begin() as conn:
                for sql, params in statements:
                    await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.fixture(scope="module")
def e2e_seed():
    from app.core.security import hash_password

    users = ((E2E_USER, "viewer", [E2E_TAG]), (E2E_NOMATCH, "viewer", ["a3b-no-such-team"]),
             (E2E_NOFLOW, E2E_NOFLOW_ROLE, []))
    cleanup = [
        ("DELETE FROM netflow_exporter_devices WHERE host(exporter_ip) = :e", {"e": E2E_EXPORTER}),
        ("DELETE FROM devices WHERE hostname = 'a3b-e2e-router'", {}),
        ("DELETE FROM users WHERE username = ANY(:u)", {"u": [u for u, _r, _s in users]}),
        ("DELETE FROM roles WHERE name = :r", {"r": E2E_NOFLOW_ROLE}),
    ]
    _run_pg(cleanup)
    device_id = uuid.uuid4()
    _run_pg([
        ("INSERT INTO devices (id, hostname, ip_address, tags) VALUES "
         "(:id, 'a3b-e2e-router', CAST('10.254.61.1' AS inet), CAST(:tags AS jsonb))",
         {"id": device_id, "tags": f'["{E2E_TAG}"]'}),
        ("INSERT INTO netflow_exporter_devices (exporter_ip, device_id) VALUES (CAST(:e AS inet), :d)",
         {"e": E2E_EXPORTER, "d": device_id}),
        ("INSERT INTO roles (name, display_name, permissions) VALUES "
         "(:r, 'A3b no NetFlow', '[\"dashboard.view\", \"devices.view\"]'::jsonb)", {"r": E2E_NOFLOW_ROLE}),
        *[
            ("INSERT INTO users (username, email, password_hash, role, scope_tags) VALUES "
             "(:u, :m, :h, :r, CAST(:s AS jsonb))",
             {"u": name, "m": f"{name}@example.test", "h": hash_password(E2E_PASS), "r": role,
              "s": json.dumps(scope)})
            for name, role, scope in users
        ],
    ])
    # The seed bypasses the API, so the API process's ownership cache (15 s,
    # kept warm by its background loops) only sees it once that expires.
    time.sleep(access.CACHE_TTL_S + 1)
    yield device_id
    _run_pg(cleanup)


def _get(token, path):
    import requests

    r = requests.get(f"{E2E_URL.rstrip('/')}/api/v1{path}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    return r.status_code, body


def _exporters_in(body) -> set[str]:
    """Every exporter address mentioned anywhere in a response body."""
    found: set[str] = set()

    def walk(v, key=""):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str) and key in ("exporter", "exporter_ip", "ip") and v.startswith("127.0.0."):
            found.add(v)

    walk(body)
    return found


def _login(username, password=E2E_PASS):
    import requests

    r = requests.post(f"{E2E_URL.rstrip('/')}/api/v1/auth/login",
                      json={"username": username, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


@needs_e2e
def test_scoped_viewer_sees_only_owned_exporter(e2e_seed):
    token = _login(E2E_USER)

    status, exporters = _get(token, "/netflow/exporters?hours=24")
    assert status == 200, exporters
    assert _exporters_in(exporters) <= {E2E_EXPORTER}

    for path in ("/netflow/overview?hours=24", "/netflow/forensics?hours=24&limit=200",
                 "/netflow/capacity?hours=24", "/netflow/anomalies?hours=24",
                 "/netflow/top-talkers?hours=24"):
        status, body = _get(token, path)
        assert status == 200, (path, body)
        assert _exporters_in(body) <= {E2E_EXPORTER}, path

    # Forensics rows all come from the owned exporter.
    status, rows = _get(token, "/netflow/forensics?hours=24&limit=200")
    items = rows.get("data", rows) if isinstance(rows, dict) else rows
    items = items.get("items", items) if isinstance(items, dict) else items
    assert items, "fixture exporter produced no flows in the window"
    assert {row.get("exporter_ip") or row.get("exporter") for row in items} == {E2E_EXPORTER}

    # An out-of-scope exporter filter is a 404, not an empty 200 or a leak.
    for path in ("/netflow/forensics", "/netflow/overview"):
        status, _ = _get(token, f"{path}?hours=24&exporter={E2E_OTHER}")
        assert status == 404, path

    # The scoped totals are exactly an administrator's totals for the owned
    # exporter: scope restricts, it does not approximate.
    admin = _login(os.environ.get("ZENPLUS_ADMIN", "admin"), os.environ.get("ZENPLUS_ADMIN_PASS", "admin123"))
    # One fixed window for all three calls, so live traffic cannot skew them.
    end = datetime.now(timezone.utc).replace(microsecond=0)
    window = (f"from={(end - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
              f"&to={end.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    _, mine = _get(token, f"/netflow/overview?{window}")
    _, theirs = _get(admin, f"/netflow/overview?{window}&exporter={E2E_EXPORTER}")
    _, everyone = _get(admin, f"/netflow/overview?{window}")
    def pick(body):
        data = body.get("data", body)      # with or without the §9.3 envelope
        return data["flows"], data["bytes"]

    assert pick(mine) == pick(theirs) and pick(mine)[0] > 0
    assert pick(everyone)[0] > pick(mine)[0]

    # The envelope discloses that a scope was applied (contract §9.3).
    _, env = _get(token, f"/netflow/overview?{window}&envelope=1")
    assert env["meta"]["restricted"] is True
    _, env = _get(admin, f"/netflow/overview?{window}&envelope=1")
    assert env["meta"]["restricted"] is False


@needs_e2e
def test_empty_scope_is_empty_and_missing_permission_is_403(e2e_seed):
    """A scope that matches no device sees no flows (200 with empty results,
    never an unfiltered answer); a role without netflow.view gets 403."""
    nomatch = _login(E2E_NOMATCH)
    status, body = _get(nomatch, "/netflow/overview?hours=24&envelope=1")
    assert status == 200, body
    assert body["meta"]["restricted"] is True and body["data"]["flows"] == 0
    for path in ("/netflow/exporters?hours=24", "/netflow/exporter-devices",
                 "/netflow/top-talkers?hours=24", "/netflow/capacity?hours=24"):
        status, body = _get(nomatch, path)
        assert status == 200 and not _exporters_in(body), (path, body)
        data = body.get("data", body) if isinstance(body, dict) else body
        assert not (data.get("items") if isinstance(data, dict) else data), (path, body)
    status, _ = _get(nomatch, f"/netflow/forensics?hours=24&exporter={E2E_EXPORTER}")
    assert status == 404

    noflow = _login(E2E_NOFLOW)
    for path in ("/netflow/overview?hours=24", "/netflow/exporters?hours=24",
                 "/netflow/forensics?hours=24", "/netflow/saved-views"):
        status, _ = _get(noflow, path)
        assert status == 403, path
