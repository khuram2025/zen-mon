"""Network map API on the real schema (disposable PostgreSQL, migrate-167 included).

Real routes through a plain ``TestClient`` (no startup hooks) with ``get_db``
pointed at the private ``pg_fixture`` database and ClickHouse stubbed out.
Never the appliance database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import manual_maps
from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from pg_fixture import PgDatabase, pg_cluster, pg_db  # noqa: F401

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=10)


def add_user(db: PgDatabase) -> SimpleNamespace:
    name = f"admin-{uuid.uuid4().hex[:8]}"
    user_id = db.scalar("INSERT INTO users (username, email, password_hash, role, full_name) "
                        "VALUES ($1, $2, 'x', 'admin', 'Map Admin') RETURNING id", name, f"{name}@example.test")
    return SimpleNamespace(id=user_id, username=name, email=f"{name}@example.test", full_name="Map Admin",
                           role="admin", is_active=True, last_login=None, password_hash="", scope_tags=[])


@pytest.fixture
def api(pg_db, monkeypatch):
    admin = add_user(pg_db)

    def no_clickhouse():
        raise RuntimeError("ClickHouse is not available in tests")

    monkeypatch.setattr(manual_maps, "get_clickhouse_client", no_clickhouse)

    async def session():
        engine = create_async_engine(pg_db.url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                yield db
        finally:
            await engine.dispose()

    async def signed_in():
        return admin

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = signed_in
    try:
        yield SimpleNamespace(client=TestClient(app), db=pg_db, admin=admin)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


# ── seed helpers ─────────────────────────────────────────────────────

_ip = iter(range(10, 250))


def device(db, hostname, *, dtype="switch", location="Al Aridh", status="up", ip=None):
    return str(db.scalar(
        "INSERT INTO devices (hostname, ip_address, device_type, location, status) "
        "VALUES ($1, $2::inet, $3, $4, $5) RETURNING id",
        hostname, ip or f"198.51.100.{next(_ip)}", dtype, location, status))


def iface(db, dev, idx, name, *, oper="up", ever_up=True, speed=1_000_000_000, descr=None):
    db.execute("INSERT INTO device_interfaces (device_id, if_index, if_name, if_descr, if_speed, oper_status, "
               "admin_status, last_up_at) VALUES ($1, $2, $3, $4, $5, $6, 'up', $7)",
               uuid.UUID(dev), idx, name, descr or name, speed, oper, NOW - timedelta(hours=2) if ever_up else None)


def lldp(db, local, lidx, lname, remote, rport, *, seen=None, chassis=None, host=None, protocol="cdp"):
    db.execute("INSERT INTO topology_links (local_device_id, local_if_index, local_if_name, remote_device_id, "
               "remote_chassis_id, remote_port_id, remote_hostname, remote_if_name, protocol, confidence, "
               "source, last_seen_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $6, $8, 90, 'udt-poller', $9)",
               uuid.UUID(local), lidx, lname, uuid.UUID(remote) if remote else None, chassis or f"ch-{uuid.uuid4().hex[:6]}",
               rport, host, protocol, seen or NOW)


def server(db, name, status="healthy"):
    return str(db.scalar("INSERT INTO servers (display_name, hostname, primary_ip, status, os_name) "
                         "VALUES ($1, $1, '203.0.113.5', $2, 'Ubuntu 24.04') RETURNING id", name, status))


def check(db, name, status="warning"):
    return str(db.scalar("INSERT INTO service_checks (name, check_type, target_host, status, last_response_ms) "
                         "VALUES ($1, 'tcp', 'app.example.test', $2, 12.5) RETURNING id", name, status))


def new_map(api, name="Al Aridh"):
    r = api.client.post("/api/v1/maps", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_node(api, map_id, **body):
    body.setdefault("x_pct", 50)
    body.setdefault("y_pct", 50)
    r = api.client.post(f"/api/v1/maps/{map_id}/nodes", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def get_map(api, map_id):
    r = api.client.get(f"/api/v1/maps/{map_id}")
    assert r.status_code == 200, r.text
    return r.json()


def add_shape(api, map_id, **body):
    body.setdefault("kind", "rectangle")
    r = api.client.post(f"/api/v1/maps/{map_id}/shapes", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def two_switches(api):
    """sw-a Gi1/0/50 ⇄ sw-b Te1/1/1, seen fresh from both sides."""
    a = device(api.db, "sw-a")
    b = device(api.db, "sw-b")
    iface(api.db, a, 50, "Gi1/0/50", descr="GigabitEthernet1/0/50")
    iface(api.db, b, 1, "Te1/1/1", speed=10_000_000_000, descr="TenGigabitEthernet1/1/1")
    lldp(api.db, a, 50, "Gi1/0/50", b, "TenGigabitEthernet1/1/1")
    lldp(api.db, b, 1, "Te1/1/1", a, "GigabitEthernet1/0/50")
    return a, b


# ── entity nodes ─────────────────────────────────────────────────────

def test_entity_nodes_create_read_list_and_status_mapping(api):
    m = new_map(api)
    dev = device(api.db, "core-1", status="down")
    srv = server(api.db, "app-01", status="critical")
    svc = check(api.db, "HTTPS portal", status="warning")
    n_dev = add_node(api, m, device_id=dev)
    n_srv = add_node(api, m, server_id=srv, label="App server")
    n_svc = add_node(api, m, service_check_id=svc)

    nodes = {n["id"]: n for n in get_map(api, m)["nodes"]}
    assert nodes[n_dev]["kind"] == "device" and nodes[n_dev]["entity_id"] == dev and nodes[n_dev]["status"] == "down"
    s = nodes[n_srv]
    assert (s["kind"], s["device_id"], s["server_id"], s["entity_id"]) == ("server", None, srv, srv)
    assert (s["hostname"], s["ip_address"], s["device_type"], s["model"], s["status"], s["label"]) == \
        ("app-01", "203.0.113.5", "server", "Ubuntu 24.04", "down", "App server")
    c = nodes[n_svc]
    assert (c["kind"], c["service_check_id"], c["hostname"], c["ip_address"], c["device_type"], c["model"],
            c["status"]) == ("service_check", svc, "HTTPS portal", "app.example.test", "service_check", "tcp", "degraded")

    listed = next(x for x in api.client.get("/api/v1/maps").json()["data"] if x["id"] == m)
    assert listed["node_count"] == 3
    assert listed["status_counts"] == {"up": 0, "down": 2, "degraded": 1, "unknown": 0, "maintenance": 0}

    for status, expected in (("healthy", "up"), ("warning", "degraded"), ("disabled", "maintenance"),
                             ("stale", "unknown"), ("unknown", "unknown")):
        api.db.execute("UPDATE servers SET status = $1 WHERE id = $2", status, uuid.UUID(srv))
        assert {n["id"]: n for n in get_map(api, m)["nodes"]}[n_srv]["status"] == expected

    live = api.client.get(f"/api/v1/maps/{m}/nodes-live").json()["data"]
    assert live[n_srv]["kind"] == "server" and live[n_srv]["cpu_pct"] is None
    assert live[n_svc]["rtt_ms"] == 12.5 and live[n_svc]["alerts"] == {"active": 0, "critical": 0, "warning": 0}
    assert live[n_dev]["status"] == "down"


def test_node_create_needs_exactly_one_existing_entity(api):
    m = new_map(api)
    dev = device(api.db, "sw-x")
    srv = server(api.db, "srv-x")
    assert api.client.post(f"/api/v1/maps/{m}/nodes", json={"x_pct": 10, "y_pct": 10}).status_code == 400
    assert api.client.post(f"/api/v1/maps/{m}/nodes", json={"device_id": dev, "server_id": srv}).status_code == 400
    for field, missing in (("server_id", "Server not found"), ("service_check_id", "Service check not found"),
                           ("device_id", "Device not found")):
        r = api.client.post(f"/api/v1/maps/{m}/nodes", json={field: str(uuid.uuid4())})
        assert (r.status_code, r.json()["detail"]) == (404, missing)
    n_srv = add_node(api, m, server_id=srv)
    r = api.client.put(f"/api/v1/maps/{m}/nodes/{n_srv}", json={"device_id": dev})
    assert r.status_code == 400
    n_dev = add_node(api, m, device_id=dev)
    other = device(api.db, "sw-y")
    assert api.client.put(f"/api/v1/maps/{m}/nodes/{n_dev}", json={"device_id": other}).status_code == 200


def test_check_constraint_requires_exactly_one_entity(api):
    m = new_map(api)
    dev = device(api.db, "sw-c")
    srv = server(api.db, "srv-c")
    with pytest.raises(Exception, match="manual_map_nodes_one_entity_check"):
        api.db.execute("INSERT INTO manual_map_nodes (map_id, device_id, server_id) VALUES ($1, $2, $3)",
                       uuid.UUID(m), uuid.UUID(dev), uuid.UUID(srv))
    with pytest.raises(Exception, match="manual_map_nodes_one_entity_check"):
        api.db.execute("INSERT INTO manual_map_nodes (map_id) VALUES ($1)", uuid.UUID(m))


# ── suggested links / auto-connect ──────────────────────────────────

def test_suggested_links_canonical_names_and_stale_filtering(api):
    m = new_map(api)
    a, b = two_switches(api)
    c = device(api.db, "sw-c")
    d = device(api.db, "sw-d")
    iface(api.db, a, 7, "Gi1/0/7")
    iface(api.db, c, 7, "Gi1/0/7")
    lldp(api.db, a, 7, "Gi1/0/7", c, "GigabitEthernet1/0/7", seen=OLD)        # old evidence
    iface(api.db, a, 9, "Gi1/0/9", oper="down", ever_up=False)
    iface(api.db, d, 9, "Gi1/0/9")
    lldp(api.db, a, 9, "Gi1/0/9", d, "Gi1/0/9")                                 # unused port
    na, nb, nc, nd = (add_node(api, m, device_id=x) for x in (a, b, c, d))

    body = api.client.get(f"/api/v1/maps/{m}/suggested-links").json()
    assert body["count"] == 1 and body["stale_count"] == 2
    s = body["data"][0]
    assert (s["source_node_id"], s["target_node_id"]) == (na, nb)
    assert (s["src_interface"], s["dst_interface"]) == ("Gi1/0/50", "Te1/1/1")
    assert s["physical_links"] == 1 and s["stale"] is False and s["reasons"] == []
    assert s["members"] == [{"src_interface": "Gi1/0/50", "dst_interface": "Te1/1/1"}]
    assert (s["src_oper"], s["dst_oper"], s["speed_bps"]) == ("up", "up", 1_000_000_000)
    assert s["last_seen_at"]

    every = api.client.get(f"/api/v1/maps/{m}/suggested-links?include_stale=1").json()
    assert every["count"] == 3 and every["stale_count"] == 2
    by_target = {x["target_node_id"]: x for x in every["data"]}
    assert by_target[nc]["stale"] and by_target[nc]["reasons"] == ["old_evidence"]
    assert by_target[nd]["reasons"] == ["local_port_never_up"]
    assert by_target[nd]["src_oper"] == "down"


def test_auto_connect_pairs_subset_and_link_metadata(api):
    m = new_map(api)
    a, b = two_switches(api)
    c = device(api.db, "sw-c")
    for idx in (1, 2):
        iface(api.db, a, 10 + idx, f"Gi1/0/1{idx}")
        iface(api.db, c, idx, f"Gi2/0/{idx}")
        lldp(api.db, a, 10 + idx, f"Gi1/0/1{idx}", c, f"GigabitEthernet2/0/{idx}")
    d = device(api.db, "sw-d")
    iface(api.db, d, 1, "Gi3/0/1")
    iface(api.db, a, 30, "Gi1/0/30")
    lldp(api.db, a, 30, "Gi1/0/30", d, "Gi3/0/1", seen=OLD)
    na, nb, nc, nd = (add_node(api, m, device_id=x) for x in (a, b, c, d))

    r = api.client.post(f"/api/v1/maps/{m}/auto-connect", json={"pairs": [[nc, na], [na, nd]]}).json()
    assert r["created"] == 1 and len(r["created_ids"]) == 1
    assert r["suggested"] == 2 and r["skipped_stale"] == 1
    link = next(x for x in get_map(api, m)["links"] if x["id"] == r["created_ids"][0])
    assert {link["source_node_id"], link["target_node_id"]} == {na, nc}
    assert link["label"] is None and link["link_type"] == "ethernet"
    md = link["metadata"]
    assert md["shape"] == "straight" and md["kind"] == "ethernet" and md["speed"] == "1G" and md["discovered"]
    assert md["width_scale"] == 1 and md["protocol"] == "cdp"
    assert md["physical_links"] == 2 and len(md["members"]) == 2

    rest = api.client.post(f"/api/v1/maps/{m}/auto-connect").json()
    assert rest["created"] == 1 and rest["skipped_stale"] == 1
    fiber = next(x for x in get_map(api, m)["links"] if x["id"] == rest["created_ids"][0])
    assert fiber["metadata"]["kind"] == "fiber" and fiber["link_type"] == "fiber"
    assert fiber["metadata"]["src_interface"] == "Gi1/0/50" and fiber["metadata"]["dst_interface"] == "Te1/1/1"
    assert "members" not in fiber["metadata"]

    stale = api.client.post(f"/api/v1/maps/{m}/auto-connect", json={"include_stale": True}).json()
    assert stale["created"] == 1 and stale["skipped_stale"] == 0


# ── annotation links ─────────────────────────────────────────────────

def test_annotation_link_endpoints_validation_and_pruning(api):
    m = new_map(api)
    dev = device(api.db, "fw-1", dtype="firewall")
    node = add_node(api, m, device_id=dev)
    cloud = add_shape(api, m, kind="image", metadata={"icon": "cloud"})
    zone = add_shape(api, m)

    url = f"/api/v1/maps/{m}/annotation-links"
    bad = api.client.post(url, json={"source": node, "target": str(uuid.uuid4()), "source_type": "node",
                                     "target_type": "shape"})
    assert bad.status_code == 400
    assert api.client.post(url, json={"source": node, "target": cloud, "source_type": "shape",
                                      "target_type": "shape"}).status_code == 400
    r = api.client.post(url, json={"source": node, "target": cloud, "source_type": "node", "target_type": "shape",
                                   "metadata": {"src_interface": "port1"}})
    assert r.status_code == 201
    al = r.json()
    assert al["id"].startswith("al-") and al["link_type"] == "manual" and al["label"] is None
    r2 = api.client.post(url, json={"id": "al-client-1", "source": zone, "target": cloud, "source_type": "shape",
                                    "target_type": "shape"})
    assert r2.status_code == 201 and r2.json()["id"] == "al-client-1"
    assert api.client.post(url, json={"id": "al-client-1", "source": zone, "target": cloud, "source_type": "shape",
                                      "target_type": "shape"}).status_code == 409

    up = api.client.put(f"{url}/{al['id']}", json={"label": "WAN", "metadata": {"src_interface": "port2"}})
    assert up.status_code == 200 and up.json()["label"] == "WAN" and up.json()["source"] == node
    assert api.client.put(f"{url}/{al['id']}", json={"target": str(uuid.uuid4())}).status_code == 400
    assert api.client.put(f"{url}/nope", json={"label": "x"}).status_code == 404

    # PUT /maps without annotation_links keeps them; with the key replaces them.
    api.client.put(f"/api/v1/maps/{m}", json={"metadata": {"theme": "dark"}})
    md = get_map(api, m)["metadata"]
    assert md["theme"] == "dark" and [x["id"] for x in md["annotation_links"]] == [al["id"], "al-client-1"]
    assert md["annotation_links"][0]["label"] == "WAN"

    # Deleting the node prunes its cable; deleting the zone prunes the other.
    assert api.client.delete(f"/api/v1/maps/{m}/nodes/{node}").status_code == 204
    assert [x["id"] for x in get_map(api, m)["metadata"]["annotation_links"]] == ["al-client-1"]
    assert api.client.delete(f"/api/v1/maps/{m}/shapes/{zone}").status_code == 204
    assert get_map(api, m)["metadata"]["annotation_links"] == []

    keep = api.client.post(url, json={"source": cloud, "target": add_shape(api, m), "source_type": "shape",
                                      "target_type": "shape"}).json()
    assert api.client.delete(f"{url}/{keep['id']}").status_code == 204
    assert api.client.delete(f"{url}/{keep['id']}").status_code == 404

    api.client.put(f"/api/v1/maps/{m}", json={"metadata": {"annotation_links": []}})
    assert get_map(api, m)["metadata"] == {"annotation_links": []}


def test_links_live_ignores_dangling_annotation_links(api):
    m = new_map(api)
    a, b = two_switches(api)
    na = add_node(api, m, device_id=a)
    shape = add_shape(api, m)
    gone = str(uuid.uuid4())
    api.client.put(f"/api/v1/maps/{m}", json={"metadata": {"annotation_links": [
        {"id": "al-ok", "source": na, "target": shape, "source_type": "node", "target_type": "shape",
         "metadata": {"src_interface": "Gi1/0/50"}},
        {"id": "al-dangling", "source": gone, "target": shape, "source_type": "node", "target_type": "shape"},
    ]}})
    data = api.client.get(f"/api/v1/maps/{m}/links-live").json()["data"]
    assert set(data) == {"al-ok"}
    assert data["al-ok"]["source"]["matched"] is True and data["al-ok"]["source"]["if_name"] == "Gi1/0/50"


# ── verify ───────────────────────────────────────────────────────────

def test_verify_reports_link_and_node_issues(api):
    m = new_map(api)
    a, b = two_switches(api)
    c = device(api.db, "sw-c", status="unknown")
    iface(api.db, a, 2, "Gi1/0/2", oper="down", ever_up=True)
    iface(api.db, a, 3, "Gi1/0/3", oper="down", ever_up=False)
    iface(api.db, a, 4, "Gi1/0/4")
    iface(api.db, b, 4, "Gi2/0/4")
    iface(api.db, c, 1, "Gi3/0/1")
    lldp(api.db, a, 4, "Gi1/0/4", c, "Gi3/0/1")  # sw-a Gi1/0/4 really goes to sw-c
    iface(api.db, a, 5, "Gi1/0/5")
    iface(api.db, b, 5, "Gi2/0/5")
    na, nb, nc = (add_node(api, m, device_id=x) for x in (a, b, c))
    shape = add_shape(api, m, kind="image")

    def link(src_if, dst_if, src=na, dst=nb):
        md = {k: v for k, v in (("src_interface", src_if), ("dst_interface", dst_if)) if v}
        return api.client.post(f"/api/v1/maps/{m}/links", json={"source_node_id": src, "target_node_id": dst,
                                                               "metadata": md}).json()["id"]

    ok = link("Gi1/0/50", "Te1/1/1")
    unbound = link(None, None)
    down = link("Gi1/0/2", "Gi2/0/4")
    never = link("Gi1/0/3", "Gi2/0/4")
    contradicted = link("Gi1/0/4", "Gi2/0/4")
    no_evidence = link("Gi1/0/5", "Gi2/0/5")
    missing = link("Gi9/9/9", "Gi2/0/5")
    half = link("Gi1/0/50", None, src=na, dst=nc)
    api.client.post(f"/api/v1/maps/{m}/annotation-links", json={
        "id": "al-1", "source": nb, "target": shape, "source_type": "node", "target_type": "shape"})

    body = api.client.get(f"/api/v1/maps/{m}/verify").json()
    got = {(i["link_id"], i["code"], i["severity"]) for i in body["links"]}
    assert not any(i["link_id"] == ok for i in body["links"])
    assert {(unbound, "unbound", "warning"), (down, "port_down", "critical"), (never, "port_never_up", "warning"),
            (contradicted, "contradicted", "warning"), (no_evidence, "no_evidence", "info"),
            (missing, "port_not_found", "warning"), (half, "half_bound", "warning"),
            ("al-1", "unbound", "info")} <= got
    assert next(i for i in body["links"] if i["link_id"] == "al-1")["annotation"] is True
    assert body["nodes"] == [{"node_id": nc, "severity": "warning", "code": "device_unknown",
                              "message": "sw-c status is unknown"}]
    assert body["summary"]["critical"] == 1 and body["summary"]["info"] >= 2 and body["generated_at"]


# ── populate / expand ────────────────────────────────────────────────

def site(api, location="Al Aridh"):
    """fw ⇄ core1/core2, cores ⇄ each other and acc1/acc2; stale fw ⇄ acc1."""
    ids = {}
    for name, dtype in (("fw", "firewall"), ("core1", "switch"), ("core2", "switch"),
                        ("acc1", "switch"), ("acc2", "switch"), ("printer", "printer")):
        ids[name] = device(api.db, f"{location}-{name}", dtype=dtype, location=location)
    port = iter(range(1, 100))

    def cable(x, y, seen=None):
        px, py = next(port), next(port)
        iface(api.db, ids[x], px, f"Gi1/0/{px}")
        iface(api.db, ids[y], py, f"Gi1/0/{py}")
        lldp(api.db, ids[x], px, f"Gi1/0/{px}", ids[y], f"GigabitEthernet1/0/{py}", seen=seen)

    for x, y in (("fw", "core1"), ("fw", "core2"), ("core1", "core2"), ("core1", "acc1"), ("core2", "acc1"),
                 ("core1", "acc2"), ("core2", "acc2")):
        cable(x, y)
    cable("fw", "acc1", seen=OLD)
    return ids


def test_populate_by_location_tiers_links_and_version(api):
    m = new_map(api)
    ids = site(api)
    device(api.db, "elsewhere", location="Riyadh")
    add_node(api, m, device_id=ids["printer"])

    opts = api.client.get("/api/v1/maps/populate/options").json()["locations"]
    assert {"name": "Al Aridh", "device_count": 6} in opts and {"name": "Riyadh", "device_count": 1} in opts

    assert api.client.post(f"/api/v1/maps/{m}/populate", json={}).status_code == 400
    r = api.client.post(f"/api/v1/maps/{m}/populate", json={"location": "al aridh"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["placed"] == 5 and body["skipped_existing"] == 1 and body["links"] == 7
    assert body["tiers"] == {"edge": 1, "core": 2, "tier2": 2}
    detail = get_map(api, m)
    by_host = {n["hostname"]: n for n in detail["nodes"]}
    assert by_host["Al Aridh-fw"]["y_pct"] < by_host["Al Aridh-core1"]["y_pct"] < by_host["Al Aridh-acc1"]["y_pct"]
    assert all(link["label"] is None and link["metadata"]["shape"] == "straight" for link in detail["links"])

    versions = api.client.get(f"/api/v1/maps/{m}/versions").json()["data"]
    assert [v["id"] for v in versions] == [body["version_id"]]
    assert versions[0]["node_count"] == 1 and versions[0]["reason"] == "Before build from site"
    assert versions[0]["created_by_name"] == "Map Admin"

    again = api.client.post(f"/api/v1/maps/{m}/populate", json={"location": "Al Aridh"}).json()
    assert again["placed"] == 0 and again["skipped_existing"] == 6 and again["links"] == 0


def test_populate_by_cidr_and_seed_and_replace(api):
    m = new_map(api)
    fw = device(api.db, "edge-fw", dtype="firewall", location=None, ip="192.0.2.1")
    sw = device(api.db, "edge-sw", location=None, ip="192.0.2.2")
    far = device(api.db, "far-sw", location=None, ip="192.0.2.200")
    iface(api.db, fw, 1, "port1")
    iface(api.db, sw, 1, "Gi1/0/1")
    iface(api.db, sw, 2, "Gi1/0/2")
    iface(api.db, far, 1, "Gi1/0/1")
    lldp(api.db, sw, 1, "Gi1/0/1", fw, "port1")
    lldp(api.db, sw, 2, "Gi1/0/2", far, "Gi1/0/1")
    assert api.client.post(f"/api/v1/maps/{m}/populate", json={"cidr": "not-a-cidr"}).status_code == 400
    r = api.client.post(f"/api/v1/maps/{m}/populate", json={"cidr": "192.0.2.0/25"}).json()
    assert r["placed"] == 2 and r["links"] == 1 and r["tiers"] == {"edge": 1, "core": 1}

    seeded = new_map(api, "seeded")
    r = api.client.post(f"/api/v1/maps/{seeded}/populate", json={"seed_device_id": fw, "depth": 1}).json()
    assert r["placed"] == 2
    r = api.client.post(f"/api/v1/maps/{seeded}/populate", json={"seed_device_id": fw, "depth": 2,
                                                                 "replace": True}).json()
    assert r["placed"] == 3 and r["skipped_existing"] == 0 and r["links"] == 2
    assert len(get_map(api, seeded)["nodes"]) == 3
    assert api.client.post(f"/api/v1/maps/{seeded}/populate",
                           json={"seed_device_id": str(uuid.uuid4())}).status_code == 404


def test_expand_places_managed_and_unmanaged_neighbours(api):
    m = new_map(api)
    a, b = two_switches(api)
    iface(api.db, a, 3, "Gi1/0/3")
    lldp(api.db, a, 3, "Gi1/0/3", None, "Gi0/1", chassis="aa:bb:cc:00:00:01", host="unmanaged-sw")
    lldp(api.db, a, 3, "Gi1/0/3", None, "Gi0/9", chassis="aa:bb:cc:00:00:02", host="gone", seen=OLD)
    na = add_node(api, m, device_id=a, x_pct=50, y_pct=20)
    srv_node = add_node(api, m, server_id=server(api.db, "srv"))
    assert api.client.post(f"/api/v1/maps/{m}/nodes/{srv_node}/expand").status_code == 400

    r = api.client.post(f"/api/v1/maps/{m}/nodes/{na}/expand", json={"include_unmanaged": True}).json()
    assert len(r["placed_node_ids"]) == 1 and len(r["link_ids"]) == 1
    assert len(r["unmanaged_shape_ids"]) == 1 and len(r["annotation_link_ids"]) == 1
    detail = get_map(api, m)
    placed = next(n for n in detail["nodes"] if n["id"] == r["placed_node_ids"][0])
    assert placed["device_id"] == b and placed["y_pct"] > 20
    shape = next(s for s in detail["shapes"] if s["id"] == r["unmanaged_shape_ids"][0])
    assert shape["kind"] == "image" and shape["text"] == "unmanaged-sw"
    assert shape["metadata"]["color"] == "#94a3b8" and shape["metadata"]["icon"] in ("switch", "router", "other")
    assert shape["metadata"]["unmanaged"] == {"hostname": "unmanaged-sw", "chassis_id": "aa:bb:cc:00:00:01",
                                              "port_id": "Gi0/1"}
    al = detail["metadata"]["annotation_links"][0]
    assert (al["source"], al["target"], al["source_type"], al["target_type"]) == (na, shape["id"], "node", "shape")
    assert al["metadata"]["src_interface"] == "Gi1/0/3" and al["metadata"]["dst_interface"] == "Gi0/1"

    again = api.client.post(f"/api/v1/maps/{m}/nodes/{na}/expand").json()
    assert again == {"placed_node_ids": [], "link_ids": [], "unmanaged_shape_ids": [], "annotation_link_ids": []}


# ── versions ─────────────────────────────────────────────────────────

def test_versions_round_trip_restore(api):
    m = new_map(api)
    a, b = two_switches(api)
    na, nb = add_node(api, m, device_id=a, label="A"), add_node(api, m, device_id=b)
    link = api.client.post(f"/api/v1/maps/{m}/links", json={"source_node_id": na, "target_node_id": nb}).json()["id"]
    shape = add_shape(api, m, text="Zone 1")
    al = api.client.post(f"/api/v1/maps/{m}/annotation-links", json={
        "source": na, "target": shape, "source_type": "node", "target_type": "shape"}).json()["id"]

    created = api.client.post(f"/api/v1/maps/{m}/versions", json={"reason": "before cleanup"})
    assert created.status_code == 201
    v = created.json()
    assert (v["node_count"], v["link_count"], v["shape_count"], v["reason"]) == (2, 1, 1, "before cleanup")
    full = api.client.get(f"/api/v1/maps/{m}/versions/{v['id']}").json()
    assert {n["id"] for n in full["snapshot"]["nodes"]} == {na, nb}
    assert next(n for n in full["snapshot"]["nodes"] if n["id"] == nb)["label"] is None

    api.client.delete(f"/api/v1/maps/{m}/nodes/{nb}")
    api.client.delete(f"/api/v1/maps/{m}/shapes/{shape}")
    api.client.put(f"/api/v1/maps/{m}", json={"name": "renamed"})
    assert get_map(api, m)["metadata"]["annotation_links"] == []

    r = api.client.post(f"/api/v1/maps/{m}/versions/{v['id']}/restore").json()
    assert r["restored"] is True and r["backup_version_id"]
    detail = get_map(api, m)
    assert detail["name"] == "Al Aridh"
    assert {n["id"] for n in detail["nodes"]} == {na, nb}
    assert [x["id"] for x in detail["links"]] == [link]
    assert [s["id"] for s in detail["shapes"]] == [shape]
    assert [x["id"] for x in detail["metadata"]["annotation_links"]] == [al]
    versions = api.client.get(f"/api/v1/maps/{m}/versions").json()["data"]
    assert [x["id"] for x in versions[:2]] == [r["backup_version_id"], v["id"]]
    assert versions[0]["reason"] == "Before restore" and versions[0]["node_count"] == 1

    assert api.client.get(f"/api/v1/maps/{m}/versions/{uuid.uuid4()}").status_code == 404


def test_versions_keep_the_newest_fifty(api):
    m = new_map(api)
    ids = [api.client.post(f"/api/v1/maps/{m}/versions", json={"reason": f"v{i}"}).json()["id"] for i in range(52)]
    listed = api.client.get(f"/api/v1/maps/{m}/versions").json()["data"]
    assert len(listed) == 50 and listed[0]["id"] == ids[-1] and listed[-1]["id"] == ids[2]
    assert api.db.scalar("SELECT count(*) FROM manual_map_versions WHERE map_id = $1", uuid.UUID(m)) == 50


# ── bulk ─────────────────────────────────────────────────────────────

def test_bulk_create_with_client_ids_and_bulk_delete(api):
    m = new_map(api)
    a, b = two_switches(api)
    existing = add_node(api, m, device_id=a)
    srv = server(api.db, "srv-bulk")
    r = api.client.post(f"/api/v1/maps/{m}/bulk", json={
        "nodes": [{"client_id": "n1", "device_id": b, "x_pct": 30, "y_pct": 30},
                  {"client_id": "n2", "server_id": srv, "x_pct": 60, "y_pct": 30}],
        "shapes": [{"client_id": "s1", "kind": "rectangle", "x_pct": 10, "y_pct": 10}],
        "links": [{"source": "n1", "target": existing, "metadata": {"src_interface": "Te1/1/1"}},
                  {"source": "n1", "target": "n2"}],
        "annotation_links": [{"source": "n2", "target": "s1", "source_type": "node", "target_type": "shape"},
                             {"id": "al-keep", "source": existing, "target": "s1", "source_type": "node",
                              "target_type": "shape"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["id_map"]) >= {"n1", "n2", "s1"} and len(body["link_ids"]) == 2
    assert body["annotation_link_ids"][1] == "al-keep" and body["annotation_link_ids"][0].startswith("al-")
    detail = get_map(api, m)
    assert len(detail["nodes"]) == 3 and len(detail["links"]) == 2 and len(detail["shapes"]) == 1
    first = next(x for x in detail["links"] if x["id"] == body["link_ids"][0])
    assert (first["source_node_id"], first["target_node_id"]) == (body["id_map"]["n1"], existing)
    assert [x["id"] for x in detail["metadata"]["annotation_links"]] == body["annotation_link_ids"]

    bad = api.client.post(f"/api/v1/maps/{m}/bulk", json={
        "nodes": [{"client_id": "x1", "device_id": b}],
        "links": [{"source": "x1", "target": "nowhere"}]})
    assert bad.status_code == 400
    assert len(get_map(api, m)["nodes"]) == 3  # the whole batch rolled back

    d = api.client.post(f"/api/v1/maps/{m}/bulk-delete", json={
        "node_ids": [body["id_map"]["n1"], str(uuid.uuid4())], "shape_ids": [body["id_map"]["s1"]],
        "link_ids": [body["link_ids"][0]], "annotation_link_ids": ["al-missing"]})
    assert d.status_code == 200, d.text
    assert d.json()["deleted"] == {"nodes": 1, "shapes": 1, "links": 2, "annotation_links": 2}
    detail = get_map(api, m)
    assert len(detail["nodes"]) == 2 and detail["links"] == [] and detail["shapes"] == []
    assert detail["metadata"]["annotation_links"] == []
    again = api.client.post(f"/api/v1/maps/{m}/bulk-delete", json={"link_ids": [body["link_ids"][1]]})
    assert again.status_code == 200 and again.json()["deleted"]["links"] == 0


def test_writes_bump_updated_at(api):
    m = new_map(api)
    before = api.db.scalar("SELECT updated_at FROM manual_maps WHERE id = $1", uuid.UUID(m))
    shape = add_shape(api, m)
    api.client.post(f"/api/v1/maps/{m}/annotation-links", json={
        "source": shape, "target": add_shape(api, m), "source_type": "shape", "target_type": "shape"})
    after = api.db.scalar("SELECT updated_at FROM manual_maps WHERE id = $1", uuid.UUID(m))
    assert after > before


def test_expand_avoids_existing_items_and_abbreviates_unmanaged_ports(api):
    m = new_map(api)
    a, b = two_switches(api)
    iface(api.db, a, 3, "Gi1/0/3")
    lldp(api.db, a, 3, "Gi1/0/3", None, "GigabitEthernet0/52", chassis="aa:bb:cc:00:00:09", host="closet-sw")
    na = add_node(api, m, device_id=a, x_pct=50, y_pct=20)
    # Occupy the first two candidates straight below the node (r = 320 and the next angle).
    other = device(api.db, "sw-other")
    blocker = add_node(api, m, device_id=other, x_pct=50, y_pct=33.33)
    add_shape(api, m, kind="rectangle", x_pct=40, y_pct=26, w_pct=6, h_pct=8)
    r = api.client.post(f"/api/v1/maps/{m}/nodes/{na}/expand").json()
    assert len(r["placed_node_ids"]) == 1 and len(r["unmanaged_shape_ids"]) == 1
    detail = get_map(api, m)
    nodes = {n["id"]: n for n in detail["nodes"]}
    placed = nodes[r["placed_node_ids"][0]]
    shape = next(s for s in detail["shapes"] if s["id"] == r["unmanaged_shape_ids"][0])
    centres = [(placed["x_pct"], placed["y_pct"]),
               (shape["x_pct"] + shape["w_pct"] / 2, shape["y_pct"] + shape["h_pct"] / 2)]
    taken = [(n["x_pct"], n["y_pct"]) for nid, n in nodes.items() if nid in (na, blocker)]

    def px(p):
        return p[0] * 40, p[1] * 24

    for c in centres:
        for t in taken:
            assert ((px(c)[0] - px(t)[0]) ** 2 + (px(c)[1] - px(t)[1]) ** 2) ** 0.5 >= 150
        x, y = px(c)
        assert not (40 * 40 - 60 <= x <= 46 * 40 + 60 and 26 * 24 - 60 <= y <= 34 * 24 + 60)
    (x1, y1), (x2, y2) = px(centres[0]), px(centres[1])
    assert ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 >= 149
    al = detail["metadata"]["annotation_links"][0]
    assert al["metadata"]["dst_interface"] == "Gi0/52"
    assert shape["metadata"]["unmanaged"]["port_id"] == "Gi0/52"


def test_suggested_links_abbreviate_unresolved_remote_ports(api):
    m = new_map(api)
    a = device(api.db, "sw-a")
    b = device(api.db, "sw-b")
    iface(api.db, a, 1, "Gi1/0/1")
    iface(api.db, b, 9, "Gi2/0/9")
    lldp(api.db, a, 1, "Gi1/0/1", b, "TenGigabitEthernet2/0/52")
    add_node(api, m, device_id=a)
    add_node(api, m, device_id=b)
    s = api.client.get(f"/api/v1/maps/{m}/suggested-links?include_stale=1").json()["data"][0]
    assert s["dst_interface"] == "Te2/0/52" and s["reasons"] == ["remote_port_missing"]


# ── review follow-ups ─────────────────────────────────────────────────

def test_populate_and_options_respect_the_callers_tag_scope(api, monkeypatch):
    mine = device(api.db, "site-a-sw", location="Site A")
    other = device(api.db, "site-b-sw", location="Site B")
    api.db.execute("UPDATE devices SET tags = '[\"site-a\"]'::jsonb WHERE id = $1", uuid.UUID(mine))
    api.db.execute("UPDATE devices SET tags = '[\"site-b\"]'::jsonb WHERE id = $1", uuid.UUID(other))

    async def scoped(db, user):
        return ["site-a"]

    monkeypatch.setattr(manual_maps.scoping, "visible_tags", scoped)
    opts = api.client.get("/api/v1/maps/populate/options").json()["locations"]
    assert [o["name"] for o in opts] == ["Site A"]

    map_id = new_map(api)
    r = api.client.post(f"/api/v1/maps/{map_id}/populate", json={"cidr": "198.51.100.0/24"})
    assert r.status_code == 200, r.text
    placed = {n["device_id"] for n in get_map(api, map_id)["nodes"]}
    assert placed == {mine}


def test_populate_rejects_a_subnet_wider_than_slash_8(api):
    map_id = new_map(api)
    r = api.client.post(f"/api/v1/maps/{map_id}/populate", json={"cidr": "0.0.0.0/0"})
    assert r.status_code == 400 and "too wide" in r.json()["detail"]


def test_version_snapshots_store_large_inline_images_once(api):
    map_id = new_map(api)
    img = "data:image/png;base64," + "A" * 20000
    sid = add_shape(api, map_id, kind="image", metadata={"src": img})
    for _ in range(3):
        assert api.client.post(f"/api/v1/maps/{map_id}/versions", json={"reason": "x"}).status_code in (200, 201)
    assert api.db.scalar("SELECT count(*) FROM manual_map_version_blobs WHERE map_id = $1", uuid.UUID(map_id)) == 1
    stored = api.db.scalar("SELECT snapshot::text FROM manual_map_versions WHERE map_id = $1 LIMIT 1", uuid.UUID(map_id))
    assert "zp-version-blob:" in stored and "AAAAAAAAAA" not in stored
    vid = api.client.get(f"/api/v1/maps/{map_id}/versions").json()["data"][0]["id"]
    snap = api.client.get(f"/api/v1/maps/{map_id}/versions/{vid}").json()["snapshot"]
    assert snap["shapes"][0]["metadata"]["src"] == img
    api.client.delete(f"/api/v1/maps/{map_id}/shapes/{sid}")
    assert api.client.post(f"/api/v1/maps/{map_id}/versions/{vid}/restore").status_code == 200
    shapes = get_map(api, map_id)["shapes"]
    assert shapes[0]["metadata"]["src"] == img


def test_bulk_accepts_null_icon_and_link_type_and_untyped_annotation_cables(api):
    map_id = new_map(api)
    a = device(api.db, "bulk-a")
    b = device(api.db, "bulk-b")
    r = api.client.post(f"/api/v1/maps/{map_id}/bulk", json={
        "nodes": [{"client_id": "n1", "device_id": a, "icon": None, "x_pct": 20, "y_pct": 20},
                  {"client_id": "n2", "device_id": b, "x_pct": 60, "y_pct": 20}],
        "shapes": [{"client_id": "s1", "kind": "image", "x_pct": 40, "y_pct": 60, "w_pct": 3, "h_pct": 4,
                    "metadata": {"icon": "cloud"}}],
        "links": [{"source": "n1", "target": "n2", "link_type": None}],
        "annotation_links": [{"source": "n1", "target": "s1"}],
    })
    assert r.status_code == 200, r.text
    m = get_map(api, map_id)
    assert m["links"][0]["link_type"] == "manual"
    al = m["metadata"]["annotation_links"][0]
    assert (al["source_type"], al["target_type"]) == ("node", "shape")


def test_live_interface_matching_respects_port_number_boundaries():
    ifaces = [{"if_name": "Gi1/0/50", "if_descr": "GigabitEthernet1/0/50", "if_alias": ""},
              {"if_name": "Fo1/1/1", "if_descr": "FortyGigabitEthernet1/1/1", "if_alias": ""}]
    assert manual_maps._match_interface("Gi1/0/5", ifaces) is None
    assert manual_maps._match_interface("FortyGigabitEthernet1/1/1", ifaces)["if_name"] == "Fo1/1/1"
    assert manual_maps._match_interface("gigabitethernet1/0/50", ifaces)["if_name"] == "Gi1/0/50"
