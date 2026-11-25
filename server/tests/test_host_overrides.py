"""Manual host addresses for service checks (docs/service-check-connections.md).

Validation, the per-place table shared with the Go poller, the administrator
API (permissions, audit), what a sensor receives, and the on-demand test
transports: the connection goes to the manual address while the Host header,
the TLS server name and the certificate name check keep the name.

The SQL-level tests run on a disposable PostgreSQL; the transport tests talk
to a TLS server on 127.0.0.1 started by the test.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import ssl
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import uuid4

import httpx
import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import host_overrides as ho
from app.services import probe_tls
from app.services.probe_tls import HostOverrideTransport, ProbeAddressAdapter, ProbeTrustAdapter, ProbeTrustTransport, verified_context
from app.services.service_workflow import execute_http_workflow, tls_diagnosis

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

VECTORS = json.loads((Path(__file__).resolve().parents[2] / "scripts/dev/host-overrides-contract/vectors.json").read_text(encoding="utf-8"))
BASE = "/api/v1/system/monitoring/host-overrides"
SENSOR_A = "11111111-1111-4111-8111-111111111111"
SENSOR_B = "22222222-2222-4222-8222-222222222222"


# ── Validation ───────────────────────────────────────────────────────────────

def test_host_names_are_exact_names():
    assert ho.normalize_host(" Portal.Example.LOCAL. ") == "portal.example.local"
    assert ho.normalize_host("sql_01.corp") == "sql_01.corp"
    assert ho.normalize_host("intranet") == "intranet"
    for bad in ["", "   ", "*.example.local", "https://portal.example.local", "portal.example.local/login",
                "portal.example.local:8443", "10.1.2.3", "[2001:db8::1]", "2001:db8::1", "-bad.test", "bad..test",
                "a" * 64 + ".test", "sp ace.test"]:
        with pytest.raises(ho.HostOverrideError) as error:
            ho.normalize_host(bad)
        assert error.value.field == "host"


def test_addresses_are_one_ipv4_or_ipv6_address():
    assert ho.normalize_address(" 10.1.2.3 ") == "10.1.2.3"
    assert ho.normalize_address("[2001:0DB8::10]") == "2001:db8::10"
    assert ho.normalize_address("::ffff:10.1.2.3") == "10.1.2.3"
    for bad in ["", "portal.example.local", "10.1.2.3:443", "10.1.2.0/24", "10.1.2.3, 10.1.2.4", "0.0.0.0", "::",
                "224.0.0.1", "ff02::1", "fe80::1%eth0", "300.1.1.1"]:
        with pytest.raises(ho.HostOverrideError) as error:
            ho.normalize_address(bad)
        assert error.value.field == "address"


def test_entry_scope_and_note_rules():
    entry = ho.build_entry({"host": "Portal.Example.Local", "address": "10.1.2.3", "note": "  behind   the\tWAF "},
                           sensor_ids=[], actor="admin")
    assert (entry["host"], entry["address"], entry["scope"], entry["sensor_id"], entry["note"]) == \
        ("portal.example.local", "10.1.2.3", "all", None, "behind the WAF")
    assert entry["updated_by"] == "admin" and len(entry["id"]) == 32
    on_sensor = ho.build_entry({"host": "a.test", "address": "10.0.0.1", "scope": "sensor", "sensor_id": SENSOR_A.upper()},
                               sensor_ids=[SENSOR_A], actor=None)
    assert on_sensor["sensor_id"] == SENSOR_A
    # A sensor id sent with another scope is not stored.
    assert ho.build_entry({"host": "a.test", "address": "10.0.0.1", "scope": "central", "sensor_id": SENSOR_A},
                          sensor_ids=[SENSOR_A], actor=None)["sensor_id"] is None
    for data, field in [
        ({"host": "a.test", "address": "10.0.0.1", "scope": "branch"}, "scope"),
        ({"host": "a.test", "address": "10.0.0.1", "scope": "sensor"}, "sensor_id"),
        ({"host": "a.test", "address": "10.0.0.1", "scope": "sensor", "sensor_id": "not-a-uuid"}, "sensor_id"),
        ({"host": "a.test", "address": "10.0.0.1", "scope": "sensor", "sensor_id": SENSOR_B}, "sensor_id"),
        ({"host": "a.test", "address": "10.0.0.1", "note": "x" * 201}, "note"),
    ]:
        with pytest.raises(ho.HostOverrideError) as error:
            ho.build_entry(data, sensor_ids=[SENSOR_A], actor=None)
        assert error.value.field == field


def test_one_address_per_name_and_place_and_a_cap():
    document = {"entries": []}

    def add(**data):
        entry = ho.build_entry(data, sensor_ids=[SENSOR_A, SENSOR_B], actor="admin")
        ho.put_entry(document, entry)
        return entry

    first = add(host="portal.example.local", address="10.1.2.3")
    add(host="portal.example.local", address="10.9.9.9", scope="central")
    add(host="portal.example.local", address="192.168.50.7", scope="sensor", sensor_id=SENSOR_A)
    add(host="portal.example.local", address="192.168.60.7", scope="sensor", sensor_id=SENSOR_B)
    for duplicate in [dict(host="PORTAL.example.local.", address="10.1.2.4"),
                      dict(host="portal.example.local", address="10.1.2.4", scope="central"),
                      dict(host="portal.example.local", address="10.1.2.4", scope="sensor", sensor_id=SENSOR_A)]:
        with pytest.raises(ho.HostOverrideError) as error:
            add(**duplicate)
        assert error.value.status == 409
    assert len(document["entries"]) == 4

    # Editing a row keeps its id and may not collide with another row.
    changed = ho.build_entry({"host": "portal.example.local", "address": "10.1.2.99"}, sensor_ids=[], actor="admin", entry_id=first["id"])
    assert ho.put_entry(document, changed)["address"] == "10.1.2.3"
    assert [e["address"] for e in document["entries"] if e["id"] == first["id"]] == ["10.1.2.99"]
    collide = ho.build_entry({"host": "portal.example.local", "address": "10.1.2.99", "scope": "central"}, sensor_ids=[], actor="admin", entry_id=first["id"])
    with pytest.raises(ho.HostOverrideError):
        ho.put_entry(document, collide)

    assert ho.remove_entry(document, first["id"])["id"] == first["id"]
    assert ho.remove_entry(document, first["id"]) is None

    full = {"entries": [{"id": str(i), "host": f"h{i}.test", "address": "10.0.0.1", "scope": "all"} for i in range(ho.MAX_ENTRIES)]}
    with pytest.raises(ho.HostOverrideError, match="At most"):
        ho.put_entry(full, ho.build_entry({"host": "one-more.test", "address": "10.0.0.1"}, sensor_ids=[], actor=None))
    # A full table can still be edited.
    ho.put_entry(full, ho.build_entry({"host": "h0.test", "address": "10.0.0.2"}, sensor_ids=[], actor=None, entry_id="0"))


# ── The table per place (shared contract with the Go poller) ─────────────────

def test_contract_vectors_match_the_poller():
    assert VECTORS["places"] and VECTORS["lookups"]
    for place in VECTORS["places"]:
        assert ho.resolve_for(VECTORS["entries"], place["place"]) == place["expect"], place["place"]
    for case in VECTORS["lookups"]:
        assert ho.lookup(ho.resolve_for(VECTORS["entries"], case["place"]), case["host"]) == case["address"], case


def test_damaged_rows_are_skipped_not_followed():
    table = ho.resolve_for([
        {"host": "name.test", "address": "other.test", "scope": "all"},
        {"host": "10.0.0.1", "address": "10.0.0.2", "scope": "all"},
        {"host": "", "address": "10.0.0.3", "scope": "all"},
        {"host": "scope.test", "address": "10.0.0.4", "scope": "elsewhere"},
        {"host": "Good.Test.", "address": "::ffff:10.0.0.5"},
    ], "central")
    assert table == {"good.test": "10.0.0.5"}
    assert ho.lookup(None, "good.test") is None and ho.lookup(table, None) is None


def test_sensor_payload_holds_only_the_rows_for_that_sensor():
    assert ho.sensor_payload(VECTORS["entries"], SENSOR_A) == [
        {"host": "intranet.example.local", "address": "2001:db8::10"},
        {"host": "portal.example.local", "address": "192.168.50.7"},
    ]
    payload_b = ho.sensor_payload(VECTORS["entries"], SENSOR_B)
    assert {"host": "branch-only.example.local", "address": "172.16.4.4"} in payload_b
    assert all(row["host"] != "hq-only.example.local" for row in payload_b)
    # No scope and no other sensor's rows travel: the sensor is told only what it applies.
    assert all(set(row) == {"host", "address"} for row in payload_b)
    assert ho.sensor_payload([], SENSOR_A) == []


def test_sensor_capability():
    assert ho.sensor_applies(["host_overrides"]) and ho.sensor_applies(("other", "host_overrides"))
    for reported in (None, [], ["other"], "host_overrides", {"host_overrides": True}):
        assert not ho.sensor_applies(reported)


# ── Administrator API ────────────────────────────────────────────────────────

SENSORS = [
    {"id": SENSOR_A, "name": "Branch A", "status": "online", "version": "1.25.0", "applies": False},
    {"id": SENSOR_B, "name": "Branch B", "status": "online", "version": "1.26.0", "applies": True},
    {"id": "33333333-3333-4333-8333-333333333333", "name": "Never enrolled", "status": "pending", "version": None, "applies": False},
]


@pytest.fixture
def store(monkeypatch):
    from app.api.v1 import host_overrides as api
    state = NS(document={"entries": []}, audit=[], audit_fails=False)

    async def load(db):
        return deepcopy(state.document)

    async def save(db, value):
        state.pending = deepcopy(value)
        if not state.audit_fails:
            state.document = deepcopy(value)

    async def record(db, **event):
        if state.audit_fails:
            raise api.AuditUnavailable("audit down")
        state.audit.append(event)
        return True

    async def sensors(db):
        return deepcopy(SENSORS)

    monkeypatch.setattr(api, "load_host_overrides", load)
    monkeypatch.setattr(api, "lock_host_overrides", load)
    monkeypatch.setattr(api, "save_host_overrides", save)
    monkeypatch.setattr(api, "write_audit_log", record)
    monkeypatch.setattr(api, "load_sensors", sensors)
    return state


def test_admin_lifecycle_is_validated_and_audited(client, as_admin, store):
    created = client.post(BASE, json={"host": "Portal.Example.Local", "address": "10.1.2.3", "note": "behind the WAF"})
    assert created.status_code == 201
    body = created.json()
    entry = body["entries"][0]
    assert (entry["host"], entry["address"], entry["scope"], entry["updated_by"]) == ("portal.example.local", "10.1.2.3", "all", "admin-user")
    assert body["max_entries"] == ho.MAX_ENTRIES and len(body["sensors"]) == 3
    # An "everywhere" row names the running sensors that cannot apply it; a sensor that never enrolled runs no checks.
    assert entry["not_applied_by"] == ["Branch A"]

    refused = client.post(BASE, json={"host": "portal.example.local", "address": "10.1.2.4"})
    assert refused.status_code == 409 and refused.json()["detail"]["field"] == "host"
    for payload, field in [({"host": "*.example.local", "address": "10.1.2.3"}, "host"),
                           ({"host": "other.example.local", "address": "not-an-address"}, "address"),
                           ({"host": "other.example.local", "address": "10.1.2.3", "scope": "sensor"}, "sensor_id")]:
        response = client.post(BASE, json=payload)
        assert response.status_code == 422 and response.json()["detail"]["field"] == field
        assert response.json()["detail"]["message"]
    assert client.post(BASE, json={"host": "x.test", "address": "10.0.0.1", "scope": "nowhere"}).status_code == 422

    on_a = client.post(BASE, json={"host": "portal.example.local", "address": "192.168.50.7", "scope": "sensor", "sensor_id": SENSOR_A}).json()
    row_a = next(row for row in on_a["entries"] if row["scope"] == "sensor")
    assert row_a["sensor_name"] == "Branch A" and row_a["not_applied_by"] == ["Branch A"] and not row_a["sensor_missing"]
    on_b = client.post(BASE, json={"host": "branch.example.local", "address": "172.16.4.4", "scope": "sensor", "sensor_id": SENSOR_B}).json()
    assert next(row for row in on_b["entries"] if row["host"] == "branch.example.local")["not_applied_by"] == []

    changed = client.put(f"{BASE}/{entry['id']}", json={"host": "portal.example.local", "address": "10.1.2.99", "scope": "central"})
    assert changed.status_code == 200
    updated = next(row for row in changed.json()["entries"] if row["id"] == entry["id"])
    assert (updated["address"], updated["scope"], updated["not_applied_by"]) == ("10.1.2.99", "central", [])
    assert client.put(f"{BASE}/missing", json={"host": "portal.example.local", "address": "10.1.2.99"}).status_code == 404

    assert len(client.get(BASE).json()["entries"]) == 3
    assert client.delete(f"{BASE}/{entry['id']}").status_code == 200
    assert client.delete(f"{BASE}/{entry['id']}").status_code == 404
    assert len(client.get(BASE).json()["entries"]) == 2

    assert [event["action"] for event in store.audit] == [
        "monitoring.host_overrides.add", "monitoring.host_overrides.add", "monitoring.host_overrides.add",
        "monitoring.host_overrides.update", "monitoring.host_overrides.remove",
    ]
    assert all(event["required"] is True and event["resource_type"] == "host_override" for event in store.audit)
    update = store.audit[3]
    assert update["resource_id"] == entry["id"]
    assert update["metadata"]["address"] == "10.1.2.99" and update["metadata"]["previous"]["address"] == "10.1.2.3"
    assert store.audit[4]["metadata"]["host"] == "portal.example.local"


def test_a_change_that_cannot_be_audited_is_refused(client, as_admin, store):
    store.audit_fails = True
    response = client.post(BASE, json={"host": "portal.example.local", "address": "10.1.2.3"})
    assert response.status_code == 503 and "audit" in response.json()["detail"].lower()
    store.audit_fails = False
    assert client.get(BASE).json()["entries"] == []


def test_a_removed_sensor_is_shown_on_its_row(client, as_admin, store):
    store.document = {"entries": [{"id": "a1", "host": "gone.example.local", "address": "10.0.0.9", "scope": "sensor",
                                   "sensor_id": "99999999-9999-4999-8999-999999999999", "note": ""}]}
    row = client.get(BASE).json()["entries"][0]
    assert row["sensor_missing"] is True and row["sensor_name"] is None and row["not_applied_by"] == []


@pytest.mark.parametrize("role", ["as_operator", "as_viewer"])
@pytest.mark.parametrize("method,path,body", [
    ("get", "", None), ("post", "", {"host": "a.test", "address": "10.0.0.1"}),
    ("put", "/abc", {"host": "a.test", "address": "10.0.0.1"}), ("delete", "/abc", None),
])
def test_only_administrators_read_or_change_manual_addresses(client, request, store, role, method, path, body):
    request.getfixturevalue(role)
    kwargs = {"json": body} if body is not None else {}
    assert getattr(client, method)(BASE + path, **kwargs).status_code == 403
    assert store.audit == [] and store.document == {"entries": []}


# ── Stored document, sensor configuration and capabilities (PostgreSQL) ──────

@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def test_stored_rows_reach_only_the_sensors_they_apply_to(pg_db):
    from app.api.v1 import host_overrides as api
    from app.api.v1 import sensor_api

    old = str(pg_db.scalar("INSERT INTO sensors (name, status, version) VALUES ('Branch A', 'online', '1.25.0') RETURNING id"))
    new = str(pg_db.scalar("INSERT INTO sensors (name, status, version) VALUES ('Branch B', 'online', '1.26.0') RETURNING id"))
    admin = NS(id=uuid4(), username="admin", role="admin")

    async def run():
        async with session(pg_db) as db:
            assert await ho.load_host_overrides(db) == {"entries": []}
            etags = {sid: await sensor_api._config_etag(sid, db) for sid in (old, new)}

            # A heartbeat records what the sensor says it applies; a sensor that says nothing has nothing.
            await sensor_api._record_capabilities(db, new, ["host_overrides", "host_overrides"])
            await sensor_api._record_capabilities(db, old, None)
            await db.commit()
            sensors = {row["name"]: row for row in await api.load_sensors(db)}
            assert sensors["Branch B"]["applies"] is True and sensors["Branch A"]["applies"] is False

            result = await api.add_host_override(api.HostOverrideIn(host="Branch.Example.Local", address="172.16.4.4", scope="sensor", sensor_id=new), db=db, user=admin)
            assert result["entries"][0]["sensor_name"] == "Branch B" and result["entries"][0]["not_applied_by"] == []
            # Only the sensor the row applies to gets a new configuration.
            assert await sensor_api._config_etag(new, db) != etags[new]
            assert await sensor_api._config_etag(old, db) == etags[old]

            result = await api.add_host_override(api.HostOverrideIn(host="portal.example.local", address="10.1.2.3"), db=db, user=admin)
            everywhere = next(row for row in result["entries"] if row["scope"] == "all")
            assert everywhere["not_applied_by"] == ["Branch A"]
            assert await sensor_api._config_etag(old, db) != etags[old]

            stored = (await ho.load_host_overrides(db))["entries"]
            assert ho.sensor_payload(stored, new) == [
                {"host": "branch.example.local", "address": "172.16.4.4"},
                {"host": "portal.example.local", "address": "10.1.2.3"},
            ]
            assert ho.sensor_payload(stored, old) == [{"host": "portal.example.local", "address": "10.1.2.3"}]
            assert await ho.central_overrides(db) == {"portal.example.local": "10.1.2.3"}

            with pytest.raises(HTTPException) as duplicate:
                await api.add_host_override(api.HostOverrideIn(host="portal.example.local", address="10.1.2.4"), db=db, user=admin)
            assert duplicate.value.status_code == 409
            await db.rollback()

            # The sensor downgrades: its next heartbeat names nothing, and the row says so.
            await sensor_api._record_capabilities(db, new, [])
            await db.commit()
            listing = await api.list_host_overrides(db=db, user=admin)
            assert next(row for row in listing["entries"] if row["scope"] == "sensor")["not_applied_by"] == ["Branch B"]

            entry_id = everywhere["id"]
            await api.remove_host_override(entry_id, db=db, user=admin)
            assert await ho.central_overrides(db) == {}

    asyncio.run(run())
    # The document is what the Go poller reads: {"entries": [...]} under the settings key.
    value = pg_db.scalar("SELECT value FROM system_settings WHERE key = $1", ho.SETTINGS_KEY)
    assert [(e["host"], e["address"], e["scope"], e["sensor_id"]) for e in value["entries"]] == [("branch.example.local", "172.16.4.4", "sensor", new)]
    audit = pg_db.query("SELECT action, actor_username, resource_type, metadata FROM audit_logs WHERE action LIKE 'monitoring.host_overrides.%' ORDER BY created_at, action")
    assert sorted(row[0] for row in audit) == ["monitoring.host_overrides.add", "monitoring.host_overrides.add", "monitoring.host_overrides.remove"]
    assert all(row[1] == "admin" and row[2] == "host_override" for row in audit)
    assert {row[3]["host"] for row in audit} == {"branch.example.local", "portal.example.local"}


def test_sensor_config_and_heartbeat_schemas_carry_the_new_fields():
    from app.schemas.sensor import ConfigResponse, HeartbeatRequest, ProbeNetworkDiagnostics

    config = ConfigResponse(etag="e", sensor_id="s", sensor_name="n", host_overrides=[{"host": "a.test", "address": "10.0.0.1"}])
    assert config.model_dump()["host_overrides"] == [{"host": "a.test", "address": "10.0.0.1"}]
    assert ConfigResponse(etag="e", sensor_id="s", sensor_name="n").host_overrides == []
    assert HeartbeatRequest().capabilities is None  # an older sensor
    assert HeartbeatRequest(capabilities=["host_overrides"]).capabilities == ["host_overrides"]
    with pytest.raises(ValueError):
        HeartbeatRequest(capabilities=["Not A Capability"])
    # A sensor result keeps the manual address it used through ingestion.
    kept = ProbeNetworkDiagnostics(override_host="a.test", override_address="10.0.0.1").model_dump()
    assert (kept["override_host"], kept["override_address"]) == ("a.test", "10.0.0.1")


# ── On-demand test transports ────────────────────────────────────────────────

def make_cert():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Manual address test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("service.test"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    return cert, key


@pytest.fixture
def service(tmp_path):
    """HTTPS on 127.0.0.1 with a certificate for service.test (and 127.0.0.1); records Host and SNI."""
    cert, key = make_cert()
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    seen = NS(hosts=[], names=[], paths=[])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.hosts.append(self.headers.get("Host"))
            seen.paths.append(self.path)
            if self.path == "/moved":
                self.send_response(302)
                self.send_header("Location", "/landing")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b"healthy"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    ctx.sni_callback = lambda sock, name, context: seen.names.append(name)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def trusting():
        context = ssl.create_default_context(cafile=str(cert_file))
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        return context

    yield NS(port=server.server_port, seen=seen, cert_file=cert_file, context=trusting,
             policy={"certificates": [{"pem": cert_file.read_text(), "hosts": ["service.test"]}]})
    server.shutdown()
    server.server_close()
    thread.join()


OVERRIDES = {"service.test": "127.0.0.1", "wrong.test": "127.0.0.1"}


def test_httpx_connects_to_the_manual_address_and_keeps_the_name(service):
    used: dict[str, str] = {}

    async def run():
        transport = HostOverrideTransport(lambda: httpx.AsyncHTTPTransport(verify=service.context()), OVERRIDES, "auto", used)
        async with httpx.AsyncClient(transport=transport, timeout=3) as client:
            response = await client.get(f"https://service.test:{service.port}/moved", follow_redirects=True)
            assert response.text == "healthy"
            # The caller still sees the name, also after a relative redirect.
            assert response.url.host == "service.test" and response.url.path == "/landing"
            assert [item.url.host for item in response.history] == ["service.test"]
            # Same address, another name: the certificate (valid for 127.0.0.1 too) must be refused.
            with pytest.raises(httpx.ConnectError):
                await client.get(f"https://wrong.test:{service.port}/")
            # A name without a manual address is resolved as before (and .test does not resolve:
            # a refusal or, on a slow resolver, a timeout).
            with pytest.raises(httpx.TransportError):
                await client.get(f"https://unmapped.test:{service.port}/")

    asyncio.run(run())
    assert service.seen.hosts == [f"service.test:{service.port}"] * 2
    assert set(service.seen.names) == {"service.test", "wrong.test"} and service.seen.names[0] == "service.test"
    assert used == {"service.test": "127.0.0.1", "wrong.test": "127.0.0.1"}


def test_trust_transport_verifies_the_name_at_the_manual_address(service, monkeypatch):
    calls = []

    def fake_verified_context(host, port, timeout, policy, ip_version="auto", address=None):
        calls.append((host, port, address))
        return service.context()

    monkeypatch.setattr(probe_tls, "verified_context", fake_verified_context)
    used: dict[str, str] = {}

    async def run():
        async with httpx.AsyncClient(transport=ProbeTrustTransport(service.policy, 3, "auto", OVERRIDES, used), timeout=3) as client:
            assert (await client.get(f"https://service.test:{service.port}/")).text == "healthy"
            with pytest.raises(httpx.ConnectError):
                await client.get(f"https://wrong.test:{service.port}/")

    asyncio.run(run())
    # The verifier is asked about the name and told where to connect.
    assert calls == [("service.test", service.port, "127.0.0.1"), ("wrong.test", service.port, "127.0.0.1")]
    assert service.seen.hosts == [f"service.test:{service.port}"] and used["service.test"] == "127.0.0.1"


def test_requests_adapters_connect_to_the_manual_address_and_keep_the_name(service, monkeypatch):
    used: dict[str, str] = {}
    with requests.Session() as http:
        http.mount("https://", ProbeAddressAdapter("auto", OVERRIDES, used))
        response = http.get(f"https://service.test:{service.port}/", timeout=3, verify=str(service.cert_file))
        assert response.text == "healthy" and response.url.startswith("https://service.test:")
        with pytest.raises(requests.exceptions.SSLError):
            http.get(f"https://wrong.test:{service.port}/", timeout=3, verify=str(service.cert_file))
        with pytest.raises(requests.exceptions.ConnectionError):
            http.get(f"https://unmapped.test:{service.port}/", timeout=3, verify=str(service.cert_file))
    assert service.seen.hosts == [f"service.test:{service.port}"] and service.seen.names[0] == "service.test"
    assert used == {"service.test": "127.0.0.1", "wrong.test": "127.0.0.1"}

    calls = []

    def fake_verified_context(host, port, timeout, policy, ip_version="auto", address=None):
        calls.append((host, port, address))
        return service.context()

    monkeypatch.setattr(probe_tls, "verified_context", fake_verified_context)
    with requests.Session() as http:
        http.mount("https://", ProbeTrustAdapter(service.policy, 3, "auto", OVERRIDES))
        assert http.get(f"https://service.test:{service.port}/", timeout=3).text == "healthy"
        with pytest.raises(requests.exceptions.SSLError):
            http.get(f"https://wrong.test:{service.port}/", timeout=3)
    assert calls == [("service.test", service.port, "127.0.0.1"), ("wrong.test", service.port, "127.0.0.1")]
    assert service.seen.hosts[-1] == f"service.test:{service.port}"


def test_manual_address_of_the_other_family_is_refused_in_plain_words():
    assert probe_tls.address_family_error("a.test", "10.0.0.1", "auto") is None
    assert probe_tls.address_family_error("a.test", "2001:db8::1", "ipv6") is None
    assert "IPv4 only" in probe_tls.address_family_error("a.test", "2001:db8::1", "ipv4")

    async def run():
        transport = HostOverrideTransport(lambda: httpx.AsyncHTTPTransport(), {"a.test": "10.0.0.1"}, "ipv6")
        async with httpx.AsyncClient(transport=transport, timeout=1) as client:
            with pytest.raises(httpx.ConnectError, match="IPv6 only"):
                await client.get("http://a.test/")

    asyncio.run(run())


def workflow_check(url, **extra):
    return NS(target_url=url, target_host=None, target_port=None, http_method="GET", http_headers={}, http_body=None,
              http_expected_statuses="200", http_expected_status=200, http_content_match="healthy", http_follow_redirects=True,
              http_ignore_tls_errors=False, http_allow_insecure_auth=False, timeout=3, workflow_steps=[], workflow_operator="all",
              config={}, **extra)


def test_http_test_run_reports_the_manual_address_it_used(service, monkeypatch):
    monkeypatch.setattr(probe_tls, "verified_context", lambda host, port, timeout, policy, ip_version="auto", address=None: service.context())
    url = f"https://service.test:{service.port}/moved"

    passed = asyncio.run(execute_http_workflow(workflow_check(url, probe_trust=service.policy, host_overrides=OVERRIDES)))
    assert passed["status"] == "up", passed["error"]
    assert passed["details"]["manual_addresses"] == [{"host": "service.test", "address": "127.0.0.1"}]
    assert passed["details"]["steps"][0]["response_url"] == f"https://service.test:{service.port}/landing"

    # Certificate verification switched off still connects to the manual address.
    ignoring = workflow_check(url, probe_trust=service.policy, host_overrides=OVERRIDES)
    ignoring.http_ignore_tls_errors = True
    assert asyncio.run(execute_http_workflow(ignoring))["details"]["manual_addresses"] == [{"host": "service.test", "address": "127.0.0.1"}]

    # Without a manual address the name does not resolve (a slow resolver gives a timeout instead):
    # the test fails and names no address.
    failed = asyncio.run(execute_http_workflow(workflow_check(url, probe_trust=service.policy, host_overrides={})))
    assert failed["status"] == "down" and failed["diagnosis"] in ("dns", "timeout")
    assert failed["details"]["manual_addresses"] == []

    mismatch = workflow_check(url, probe_trust=service.policy, host_overrides=OVERRIDES)
    mismatch.config = {"ip_version": "ipv6"}
    refused = asyncio.run(execute_http_workflow(mismatch))
    assert refused["status"] == "down" and "IPv6 only" in refused["error"]


def test_tls_failures_are_told_apart():
    assert tls_diagnosis("x509: certificate signed by unknown authority (issuer recovery could not build a trusted chain)")[0] == "tls_trust"
    assert tls_diagnosis("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate")[0] == "tls_trust"
    assert tls_diagnosis("unable to get local issuer certificate")[0] == "tls_trust"
    assert tls_diagnosis("x509: certificate is valid for service.test, not wrong.test")[0] == "tls_name"
    assert tls_diagnosis("Hostname mismatch, certificate is not valid for 'wrong.test'")[0] == "tls_name"
    assert tls_diagnosis("x509: certificate has expired or is not yet valid")[0] == "tls"
    assert tls_diagnosis("tls: handshake failure")[0] == "tls"


@pytest.mark.skipif(not os.environ.get("ZENPLUS_PROBE_TLS_BINARY"), reason="requires built poller TLS helper")
def test_real_verifier_checks_the_name_at_the_manual_address(service):
    assert verified_context("service.test", service.port, 3, service.policy, "auto", "127.0.0.1") is not None
    with pytest.raises(ssl.SSLError):
        verified_context("wrong.test", service.port, 3, service.policy, "auto", "127.0.0.1")
    with pytest.raises(ssl.SSLError):
        verified_context("service.test", service.port, 3, {"certificates": []}, "auto", "127.0.0.1")
    with pytest.raises(ssl.SSLError):
        verified_context("service.test", service.port, 3, service.policy, "auto", "not an address")

    async def run():
        used: dict[str, str] = {}
        async with httpx.AsyncClient(transport=ProbeTrustTransport(service.policy, 3, "auto", OVERRIDES, used), timeout=3) as client:
            assert (await client.get(f"https://service.test:{service.port}/")).text == "healthy"
            with pytest.raises(httpx.ConnectError):
                await client.get(f"https://wrong.test:{service.port}/")
        assert used["service.test"] == "127.0.0.1"

    asyncio.run(run())
    with requests.Session() as http:
        http.mount("https://", ProbeTrustAdapter(service.policy, 3, "auto", OVERRIDES))
        assert http.get(f"https://service.test:{service.port}/", timeout=3).text == "healthy"
