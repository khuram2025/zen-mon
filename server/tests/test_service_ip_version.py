import pytest
from pydantic import ValidationError
from app.schemas.service_check import ServiceCheckCreate, ServiceCheckUpdate
from app.schemas.sensor import ServiceResultItem
from app.services.probe_tls import ProbeAddressAdapter, probe_local_address


def test_ip_version_validation_preserves_other_config():
    value = ServiceCheckCreate(name="HTTP", check_type="http", target_host="localhost", config={"ip_version": "ipv4", "custom": 1})
    assert value.config == {"ip_version": "ipv4", "custom": 1}
    for version in ("auto", "ipv4", "ipv6"):
        assert ServiceCheckUpdate(config={"ip_version": version}).config["ip_version"] == version
    with pytest.raises(ValidationError):
        ServiceCheckUpdate(config={"ip_version": "invalid"})


def test_request_adapters_scope_address_selection():
    v4, v6, auto = ProbeAddressAdapter("ipv4"), ProbeAddressAdapter("ipv6"), ProbeAddressAdapter()
    assert v4.poolmanager.connection_pool_kw["source_address"] == ("0.0.0.0", 0)
    assert v6.poolmanager.connection_pool_kw["source_address"] == ("::", 0)
    assert "source_address" not in auto.poolmanager.connection_pool_kw
    assert probe_local_address("auto") is None
    for adapter in (v4, v6, auto):
        adapter.close()


def test_old_sensor_payload_remains_valid():
    payload = dict(service_check_id="00000000-0000-0000-0000-000000000001", timestamp="2026-09-09T00:00:00Z", check_type="http", is_up=True)
    assert ServiceResultItem(**payload).network_diagnostics is None
    item = ServiceResultItem(**payload, network_diagnostics={"ip_version": "ipv4", "remote_ip": "127.0.0.1", "dns_ms": 0.25})
    assert item.network_diagnostics.dns_ms == 0.25
    with pytest.raises(ValidationError):
        ServiceResultItem(**payload, network_diagnostics={"dns_ms": -1})


def test_manual_http_probe_enforces_ip_version():
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace
    from app.services.service_workflow import execute_http_workflow

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    check = SimpleNamespace(target_url=f"http://127.0.0.1:{server.server_port}/", http_method="GET", http_expected_statuses="200", http_expected_status=200, http_content_match=None, http_follow_redirects=True, timeout=2, config={"ip_version": "ipv4"})
    try:
        assert asyncio.run(execute_http_workflow(check))["status"] == "up"
        check.config["ip_version"] = "ipv6"
        assert asyncio.run(execute_http_workflow(check))["status"] == "down"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
