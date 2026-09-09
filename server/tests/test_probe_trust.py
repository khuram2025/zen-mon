import asyncio
import ipaddress
import os
import ssl
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.services.probe_trust import normalize_hosts, parse_trust_certificate
from app.services.probe_tls import ProbeTrustAdapter, ProbeTrustTransport, verified_context


def make_cert(ca=False, expired=False):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Probe test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=2))
            .not_valid_after(now + timedelta(days=-1 if expired else 1))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("service.test"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    return cert, key


def test_ca_and_self_signed_upload_validation():
    ca, _ = make_cert(ca=True)
    entry = parse_trust_certificate(ca.public_bytes(serialization.Encoding.DER), "Internal CA", "")
    assert entry["is_ca"] and entry["hosts"] == [] and len(entry["id"]) == 64
    leaf, key = make_cert()
    pem = leaf.public_bytes(serialization.Encoding.PEM)
    assert parse_trust_certificate(pem, "Service", "SERVICE.TEST, 127.0.0.1")["hosts"] == ["service.test", "127.0.0.1"]
    for scopes in ["", "other.test", "http://service.test", "*.test", "127.0.0.2"]:
        with pytest.raises(ValueError):
            parse_trust_certificate(pem, "Service", scopes)
    for data in [b"invalid", pem + pem, pem + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), b"x" * 65537]:
        with pytest.raises(ValueError):
            parse_trust_certificate(data, "Service", "service.test")
    expired, _ = make_cert(ca=True, expired=True)
    with pytest.raises(ValueError, match="expired"):
        parse_trust_certificate(expired.public_bytes(serialization.Encoding.PEM), "Expired", "")


def test_scope_normalization():
    assert normalize_hosts("Example.LOCAL., 2001:0db8::1 example.local") == ["example.local", "2001:db8::1"]
    for host in ["-bad.test", "bad..test", "host:443", "a/secret", "a" * 64 + ".test"]:
        with pytest.raises(ValueError):
            normalize_hosts(host)


def test_admin_certificate_lifecycle(client, as_admin, monkeypatch):
    from app.api.v1 import probe_trust as api
    policy = {"auto_fetch_intermediates": True, "certificates": []}
    audit = []

    async def load(db):
        return deepcopy(policy)

    async def save(db, value):
        policy.clear()
        policy.update(deepcopy(value))

    async def record(db, **event):
        audit.append(event)

    monkeypatch.setattr(api, "load_probe_trust", load)
    monkeypatch.setattr(api, "lock_probe_trust", load)
    monkeypatch.setattr(api, "save_probe_trust", save)
    monkeypatch.setattr(api, "write_audit_log", record)
    base = "/api/v1/system/security/probe-trust"
    ca, _ = make_cert(ca=True)
    upload = {"certificate": ("ca.crt", ca.public_bytes(serialization.Encoding.PEM), "application/x-pem-file")}
    result = client.post(base + "/certificates", files=upload, data={"label": "Internal CA", "hosts": ""})
    assert result.status_code == 200
    cert = result.json()["certificates"][0]
    assert "pem" not in cert and cert["is_ca"]
    assert client.post(base + "/certificates", files=upload).status_code == 409
    result = client.put(base, json={"auto_fetch_intermediates": False})
    assert result.status_code == 200 and result.json()["auto_fetch_intermediates"] is False
    assert len(client.get(base).json()["certificates"]) == 1
    assert client.delete(base + "/certificates/" + cert["id"]).status_code == 200
    assert client.get(base).json()["certificates"] == []
    assert client.delete(base + "/certificates/" + cert["id"]).status_code == 404
    assert [item["action"] for item in audit] == ["security.probe_trust.install", "security.probe_trust.config", "security.probe_trust.remove"]


@pytest.mark.parametrize("method,path", [("get", ""), ("put", ""), ("post", "/certificates"), ("delete", "/certificates/abc")])
def test_probe_trust_requires_admin(client, as_operator, method, path):
    response = getattr(client, method)("/api/v1/system/security/probe-trust" + path)
    assert response.status_code == 403


@pytest.fixture
def tls_service(tmp_path):
    cert, key = make_cert()
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"healthy")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, {"certificates": [parse_trust_certificate(cert_file.read_bytes(), "Test", "127.0.0.1")]}
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.skipif(not os.environ.get("ZENPLUS_PROBE_TLS_BINARY"), reason="requires built poller TLS helper")
def test_real_interactive_transports_and_trust_removal(tls_service):
    port, policy = tls_service
    url = f"https://127.0.0.1:{port}"

    async def httpx_check():
        async with httpx.AsyncClient(transport=ProbeTrustTransport(policy, 3)) as client:
            assert (await client.get(url)).text == "healthy"

    asyncio.run(httpx_check())
    with requests.Session() as session:
        session.mount("https://", ProbeTrustAdapter(policy, 3))
        assert session.get(url, timeout=3).text == "healthy"
    with pytest.raises(ssl.SSLError):
        verified_context("127.0.0.1", port, 3, {"certificates": []})
    with pytest.raises(ssl.SSLError):
        verified_context("localhost", port, 3, policy)
    with requests.Session() as session:
        session.mount("https://", ProbeTrustAdapter({"certificates": []}, 3))
        with pytest.raises(requests.exceptions.SSLError):
            session.get(url, timeout=3)
