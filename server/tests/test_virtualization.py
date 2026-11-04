"""Offline provider contract/security tests; no customer credentials or targets."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.services.virtualization.common import ConnectionConfig, ConnectorError, Snapshot, tls_context, validate_target
from app.services.virtualization.nutanix import PrismCollector, metrics
from app.api.v1 import virtualization as api
from app.services import virtualization_service as service
from app.core import security


def config(**values):
    return ConnectionConfig(**dict(provider="nutanix_pc", endpoint="https://prism.example.test:9440",
        username="monitor", secret="fixture-secret", **values))


def response(rows, *, v2=False, total=None):
    return {"entities" if v2 else "data": rows,
            "metadata": {"total_entities" if v2 else "totalAvailableResults": len(rows) if total is None else total}}


@pytest.mark.asyncio
async def test_pc_discovery_stats_alert_links_and_pagination():
    seen = []
    def handle(request):
        seen.append(request)
        assert request.method == "GET"
        p = request.url.path
        if p.endswith("/config/clusters"):
            return httpx.Response(200, json=response([{"extId": "c1", "name": "SNIH"}]))
        if p.endswith("/config/clusters/c1/hosts"):
            return httpx.Response(200, json=response([{"extId": "h1", "hostName": "host-a", "isDegraded": False, "memorySizeBytes": 4096}]))
        if p.endswith("/stats/clusters/c1") or p.endswith("/stats/clusters/c1/hosts/h1"):
            assert "$startTime" in request.url.params
            return httpx.Response(200, json={"data": {"hypervisorCpuUsagePpm": [{"timestamp": "2026-11-04T10:00:00Z", "value": 250000}],
                "aggregateHypervisorMemoryUsagePpm": [{"value": 500000}], "memoryCapacityBytes": [{"value": 4096}],
                "storageCapacityBytes": [{"value": 10000}], "storageUsageBytes": [{"value": 4000}]}})
        if p.endswith("/ahv/config/vms"):
            page = int(request.url.params["$page"])
            rows = [{"extId": f"v{i}", "name": f"vm-{i}", "host": {"extId": "h1"}, "powerState": "ON"} for i in range(page * 100, min(101, (page+1)*100))]
            return httpx.Response(200, json=response(rows, total=101))
        if p.endswith("/ahv/stats/vms"):
            return httpx.Response(200, json=response([{"extId": "v0", "stats": [{"timestamp": "2026-11-04T10:00:00Z", "hypervisorCpuUsagePpm": 400000, "hypervisorMemoryUsagePpm": 600000, "diskCapacityBytes": 1000, "diskUsagePpm": 250000}]}]))
        if p.endswith("/config/storage-containers"):
            return httpx.Response(200, json=response([]))
        if p.endswith("/esxi/config/vms"):
            return httpx.Response(404)
        if p.endswith("/serviceability/alerts"):
            return httpx.Response(200, json=response([{"extId": "alert1", "title": "VM issue", "severity": "CRITICAL", "sourceEntity": {"extId": "v0"}, "isAcknowledged": True, "isResolved": False}]))
        raise AssertionError(p)
    async with httpx.AsyncClient(base_url=config().endpoint, transport=httpx.MockTransport(handle)) as client:
        result = await PrismCollector(config(), client).collect()
    assert result.summary()["counts"] == {"cluster": 1, "host": 1, "vm": 101, "storage": 0}
    cluster, host, vm = result.resources[:3]
    assert cluster["metrics"]["cpu_usage_percent"] == 25
    assert cluster["metrics"]["memory_usage_percent"] == 50
    assert cluster["metrics"]["storage_usage_percent"] == 40
    assert host["parent_id"] == "cluster:c1"
    assert vm["metrics"]["cpu_usage_percent"] == 40
    assert vm["metrics"]["storage_used_bytes"] == 250
    assert vm["health"] == "critical"
    assert result.alerts[0]["resource_id"] == "vm:v0"
    assert result.alerts[0]["resource_url"].endswith("/ahv/config/vms/v0")
    assert result.capabilities["esxi_vms"] is False
    assert any(r.url.params.get("$page") == "1" for r in seen)


@pytest.mark.asyncio
async def test_element_uses_vm_offset_and_host_pages():
    cfg = ConnectionConfig(provider="nutanix_pe", endpoint=config().endpoint, username="monitor", secret="fixture")
    def handle(request):
        p = request.url.path
        if p.endswith("/vms"):
            assert "page" not in request.url.params
            offset = int(request.url.params["offset"])
            return httpx.Response(200, json=response([{"uuid": f"v{i}", "name": f"vm{i}", "memory_mb": 1024, "num_vcpus": 2} for i in range(offset, min(101, offset+100))], v2=True, total=101))
        assert request.url.params["page"] == "1"
        if p.endswith("/clusters"):
            rows = [{"uuid": "c1", "name": "cluster", "version": "7.0", "health_summary": {"health_status": "Good"}}]
        elif p.endswith("/alerts"):
            rows = [{"id": "a1", "severity": "Warning", "affected_entities": [{"uuid": "v0"}]}]
        else:
            rows = []
        return httpx.Response(200, json=response(rows, v2=True))
    async with httpx.AsyncClient(base_url=cfg.endpoint, transport=httpx.MockTransport(handle)) as client:
        result = await PrismCollector(cfg, client).collect()
    assert result.summary()["counts"]["vm"] == 101
    assert result.resources[0]["health"] == "healthy"
    assert result.resources[1]["metrics"]["memory_capacity_bytes"] == 1024**3
    assert "cpu_usage_percent" not in result.resources[1]["metrics"]
    assert result.alerts[0]["resource_id"] == "vm:v0"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 302, 500])
async def test_auth_and_transport_failures_never_echo_provider_bodies(status):
    async with httpx.AsyncClient(base_url=config().endpoint, transport=httpx.MockTransport(
            lambda r: httpx.Response(status, text="credential=fixture-secret", headers={"Location": "https://evil.test"}))) as client:
        with pytest.raises(ConnectorError) as e:
            await PrismCollector(config(), client).collect()
    assert "fixture-secret" not in str(e.value)


@pytest.mark.asyncio
async def test_duplicate_pagination_fails_closed():
    async with httpx.AsyncClient(base_url=config().endpoint, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=response([{"extId": "same"}], total=2)))) as client:
        with pytest.raises(ConnectorError, match="duplicate"):
            await PrismCollector(config(), client).pages("/api/clusters")


@pytest.mark.asyncio
async def test_truncated_pagination_fails_closed():
    calls = 0
    def handle(r):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response([{"extId": "one"}] if calls == 1 else [], total=2))
    async with httpx.AsyncClient(base_url=config().endpoint, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ConnectorError, match="before all"):
            await PrismCollector(config(), client).pages("/api/clusters")


def test_missing_negative_and_nan_metrics_are_unknown():
    assert metrics({"hypervisor_cpu_usage_ppm": -1, "storage.capacity_bytes": "NaN"}) == {}
    assert metrics({"hypervisor_cpu_usage_ppm": "0"})["cpu_usage_percent"] == 0
    assert metrics({"storage.capacity_bytes": 0, "storage.usage_bytes": 0}).get("storage_usage_percent") is None


@pytest.mark.parametrize("endpoint", ["http://prism.test", "https://monitor:pass@prism.test", "https://prism.test/path", "https://prism.test?secret=x", "https://prism.test/#x"])
def test_https_origin_required(endpoint):
    with pytest.raises(ValidationError):
        ConnectionConfig(provider="nutanix_pc", endpoint=endpoint, username="u", secret="s")


def test_tls_cannot_be_disabled():
    ctx = tls_context()
    import ssl
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    with pytest.raises(ValueError, match="Invalid PEM"):
        tls_context("not a certificate")
    with pytest.raises(ValidationError):
        config(verify=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["https://127.0.0.1", "https://169.254.169.254", "https://[::1]"])
async def test_metadata_and_loopback_targets_rejected(target):
    with pytest.raises(ConnectorError):
        await validate_target(target)


def test_test_receipt_bound_to_user_secret_endpoint_and_expiry():
    user = SimpleNamespace(id=uuid4())
    cfg = config()
    token = api.test_token(cfg, user)
    payload = api.ConnectorWrite(name="fixture", connection=cfg, test_token=token)
    api.require_test(payload, user)
    with pytest.raises(HTTPException):
        api.require_test(payload, SimpleNamespace(id=uuid4()))
    for field, val in [("endpoint", "https://another.test"), ("secret", "changed")]:
        values = cfg.private_dict(); values[field] = val
        changed = payload.model_copy(update={"connection": ConnectionConfig(**values)})
        with pytest.raises(HTTPException):
            api.require_test(changed, user)
    import jwt
    expired = jwt.encode(dict(sub=str(user.id), purpose="virtualization-test", fingerprint=api.fingerprint(cfg), exp=1), api.get_settings().JWT_SECRET, algorithm="HS256")
    with pytest.raises(HTTPException):
        api.require_test(payload.model_copy(update={"test_token": expired}), user)


def test_validation_responses_do_not_echo_credentials():
    app = FastAPI(); app.include_router(api.router)
    app.dependency_overrides[api.require_admin_user] = lambda: SimpleNamespace(id=uuid4())
    client = TestClient(app)
    payload = config().private_dict(); payload["endpoint"] = "http://invalid.test"
    response_ = client.post("/virtualization/test", json=payload)
    assert response_.status_code == 422
    assert "fixture-secret" not in response_.text


@pytest.mark.asyncio
async def test_failed_poll_preserves_snapshot_and_marks_stale(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    row = SimpleNamespace(config={}, status="ok", enabled=True, interval_seconds=300,
        last_success_at=old, last_error=None, snapshot={"resources": [{"name": "previous"}]}, summary={},
        id=uuid4(), name="test", provider="nutanix_pc", endpoint=config().endpoint, tags=[])
    monkeypatch.setattr(service, "decrypt_config", lambda c: config().private_dict())
    monkeypatch.setattr(service, "collect", AsyncMock(side_effect=ConnectorError("Prism denied read access (403)")))
    await service.poll_locked(None, row)
    assert row.status == "error" and row.last_success_at == old
    assert row.snapshot["resources"][0]["name"] == "previous"
    assert api.public(row)["stale"] is True
    assert "config" not in api.public(row)


@pytest.mark.asyncio
async def test_metric_history_failure_does_not_discard_current_inventory(monkeypatch):
    row = SimpleNamespace(id=uuid4(), config={}, interval_seconds=300)
    result = Snapshot(); result.resource("host", "h1", "Host", metrics={"cpu_usage_percent": 42})
    monkeypatch.setattr(service, "decrypt_config", lambda c: config().private_dict())
    monkeypatch.setattr(service, "collect", AsyncMock(return_value=result))
    monkeypatch.setattr(service, "write_metrics", lambda *a: (_ for _ in ()).throw(RuntimeError("database unavailable")))
    await service.poll_locked(None, row)
    assert row.status == "partial"
    assert row.snapshot["resources"][0]["metrics"]["cpu_usage_percent"] == 42
    assert row.last_success_at is not None


@pytest.mark.asyncio
async def test_scope_checked_for_detail_and_history(monkeypatch):
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(tags=["SNIH"])) ))
    monkeypatch.setattr(api, "visible_tags", AsyncMock(return_value=["other-site"]))
    with pytest.raises(HTTPException) as e:
        await api.accessible(db, SimpleNamespace(), uuid4())
    assert e.value.status_code == 404


def test_viewer_cannot_test_or_create(monkeypatch):
    app = FastAPI(); app.include_router(api.router)
    user = SimpleNamespace(id=uuid4(), role="viewer", scope_tags=[])
    app.dependency_overrides[security.get_current_user] = lambda: user
    app.dependency_overrides[api.get_db] = lambda: SimpleNamespace()
    monkeypatch.setattr(security, "get_role_permissions", AsyncMock(return_value=[]))
    client = TestClient(app)
    assert client.post("/virtualization/test", json=config().private_dict()).status_code == 403
    assert client.post("/virtualization/connectors", json={"name": "test", "connection": config().private_dict(), "test_token": "bad"}).status_code == 403
    assert client.get("/virtualization/connectors").status_code == 403
    monkeypatch.setattr(security, "get_role_permissions", AsyncMock(return_value=["servers.view"]))
    assert client.post("/virtualization/test", json=config().private_dict()).status_code == 403


def test_vmware_inventory_units_alarms_and_session_cleanup(monkeypatch):
    import pyVmomi
    import pyVim.connect
    from app.services.virtualization import vmware
    class Cluster(SimpleNamespace): pass
    class Host(SimpleNamespace): pass
    class VM(SimpleNamespace): pass
    class Datastore(SimpleNamespace): pass
    ds = Datastore(_moId="ds1", name="shared", overallStatus="green", triggeredAlarmState=[], summary=SimpleNamespace(capacity=1000, freeSpace=400))
    cluster = Cluster(_moId="c1", name="cluster", datastore=[ds], overallStatus="green", triggeredAlarmState=[], summary=SimpleNamespace(totalCpu=4000, numCpuCores=2, totalMemory=8 * 1024**3))
    host = Host(_moId="h1", name="esxi", parent=cluster, overallStatus="green", triggeredAlarmState=[],
        runtime=SimpleNamespace(powerState="poweredOn"), summary=SimpleNamespace(
            hardware=SimpleNamespace(cpuMhz=2000, numCpuCores=2, memorySize=8 * 1024**3),
            quickStats=SimpleNamespace(overallCpuUsage=1000, overallMemoryUsage=4096)))
    vm = VM(_moId="v1", name="vm", overallStatus="yellow", runtime=SimpleNamespace(host=host, powerState="poweredOn"),
        summary=SimpleNamespace(config=SimpleNamespace(numCpu=1, memorySizeMB=1024),
            quickStats=SimpleNamespace(overallCpuUsage=1000, guestMemoryUsage=512),
            storage=SimpleNamespace(committed=100, uncommitted=300)),
        triggeredAlarmState=[SimpleNamespace(key="a1", alarm=SimpleNamespace(info=SimpleNamespace(name="VM warning")), overallStatus="yellow", acknowledged=True)])
    vm.triggeredAlarmState[0].entity = vm
    cluster.triggeredAlarmState = vm.triggeredAlarmState
    destroyed, disconnected, connections = [], [], []
    view = SimpleNamespace(view=[cluster, host, vm, ds], Destroy=lambda: destroyed.append(True))
    content = SimpleNamespace(about=SimpleNamespace(fullName="vCenter 8.0"), rootFolder=None,
        viewManager=SimpleNamespace(CreateContainerView=lambda *args: view))
    monkeypatch.setattr(pyVmomi, "vim", SimpleNamespace(ClusterComputeResource=Cluster, HostSystem=Host, VirtualMachine=VM, Datastore=Datastore))
    def connect(**kwargs):
        connections.append(kwargs)
        return SimpleNamespace(RetrieveContent=lambda: content)
    monkeypatch.setattr(pyVim.connect, "SmartConnect", connect)
    monkeypatch.setattr(pyVim.connect, "Disconnect", lambda s: disconnected.append(True))
    cfg = ConnectionConfig(provider="vmware", endpoint="https://vcenter.test", username="monitor", secret="fixture")
    result = vmware.collect(cfg)
    assert result.resources[0]["metrics"]["storage_usage_percent"] == 60
    assert result.resources[0]["metrics"]["cpu_usage_percent"] == 25
    assert result.resources[1]["metrics"]["memory_usage_percent"] == 50
    assert result.resources[2]["metrics"]["cpu_usage_percent"] == 50
    assert result.alerts[0]["resource_id"] == "vm:v1"
    assert len(result.alerts) == 1
    assert connections[0]["sslContext"].check_hostname
    assert destroyed and disconnected


def test_hyperv_invokes_only_restricted_command_with_tls(monkeypatch):
    import json
    import sys
    from app.services.virtualization import hyperv
    arguments, commands = [], []
    class Context:
        def __init__(self, *args, **kwargs):
            arguments.append(kwargs)
            self.transport = SimpleNamespace(_build_session=lambda: SimpleNamespace(send=lambda *a, **k: None))
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class PS:
        had_errors = False
        def __init__(self, pool): pass
        def add_cmdlet(self, command): commands.append(command); return self
        def invoke(self):
            return [json.dumps({"schema_version": 1, "version": "Hyper-V 2022", "resources": [
                {"id": "host1", "kind": "host", "name": "hv1", "health": "healthy", "metrics": {"cpu_usage_percent": 42}}
            ], "alerts": [], "warnings": [], "capabilities": {"hosts": True}})]
    # This is a collector contract test, independent of OS-specific SSPI libraries.
    monkeypatch.setitem(sys.modules, "pypsrp.wsman", SimpleNamespace(WSMan=Context))
    monkeypatch.setitem(sys.modules, "pypsrp.powershell", SimpleNamespace(RunspacePool=Context, PowerShell=PS))
    cfg = ConnectionConfig(provider="hyperv", endpoint="https://hyperv.test:5986", username="DOMAIN\\monitor", secret="fixture")
    result = hyperv.collect(cfg)
    assert commands == ["Get-ZenPlusHypervisorSnapshot"]
    assert arguments[0]["ssl"] is True and arguments[0]["cert_validation"] is True
    assert arguments[1]["configuration_name"] == "ZenPlusMonitoring"
    assert result.resources[0]["metrics"]["cpu_usage_percent"] == 42


@pytest.mark.asyncio
async def test_sdk_worker_is_killed_on_cancellation(monkeypatch):
    started = asyncio.Event()
    class Process:
        returncode = None
        killed = False
        async def communicate(self, stdin):
            assert b'"secret": "fixture-secret"' in stdin
            started.set()
            await asyncio.sleep(30)
        def kill(self): self.killed = True
        async def wait(self): self.returncode = -1
    process = Process()
    monkeypatch.setattr(service.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(service.collect_sdk(config()))
    await started.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert process.killed and process.returncode == -1


def test_stored_credentials_are_encrypted_and_not_in_public_response(monkeypatch):
    from app.core import crypto
    monkeypatch.setattr(crypto, "get_settings", lambda: SimpleNamespace(SNMP_ENC_KEY="a" * 64))
    crypto._cipher.cache_clear()
    try:
        encrypted = crypto.encrypt_config(config().private_dict())
        assert "fixture-secret" not in str(encrypted)
        assert crypto.decrypt_config(encrypted)["secret"] == "fixture-secret"
    finally:
        crypto._cipher.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["trusted", "untrusted", "hostname_mismatch", "expired"])
async def test_real_tls_handshake_enforces_ca_hostname_and_expiry(tmp_path, case):
    import json
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ZenPlus test CA")])
    ca = x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(
        x509.random_serial_number()).not_valid_before(now-timedelta(days=5)).not_valid_after(now+timedelta(days=5)).add_extension(
        x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(ca_key, hashes.SHA256())
    name = "wrong.test" if case == "hostname_mismatch" else "localhost"
    cert = x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])).issuer_name(ca_name).public_key(
        key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=3)).not_valid_after(
        now-timedelta(days=1) if case == "expired" else now+timedelta(days=2)).add_extension(
        x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False).sign(ca_key, hashes.SHA256())
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get("X-Ntnx-Api-Key"))
            data = json.dumps(response([])).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert_file, key_file)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        cfg = ConnectionConfig(provider="nutanix_pc", endpoint=f"https://localhost:{server.server_port}", auth_type="api_key",
            secret="fixture-key", ca_pem="" if case == "untrusted" else ca.public_bytes(serialization.Encoding.PEM).decode())
        if case == "trusted":
            await PrismCollector(cfg).collect()
            assert received and all(k == "fixture-key" for k in received)
        else:
            with pytest.raises(ConnectorError, match="TLS or transport"):
                await PrismCollector(cfg).collect()
            assert not received
    finally:
        server.shutdown(); server.server_close(); thread.join()
