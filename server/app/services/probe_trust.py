"""Administrator-managed public certificates for outbound service probes."""
import ipaddress
import json
import re
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from sqlalchemy import text

SETTINGS_KEY = "security.probe_trust"
MAX_CERTIFICATES = 32


async def load_probe_trust(db):
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key=:key"), {"key": SETTINGS_KEY})).first()
    value = row[0] if row else None
    return value or {"auto_fetch_intermediates": True, "certificates": []}


async def lock_probe_trust(db):
    # Serialize settings edits, including creation of the first row.
    await db.execute(text("SELECT pg_advisory_xact_lock(783241906)"))
    return await load_probe_trust(db)


async def save_probe_trust(db, policy):
    await db.execute(text("INSERT INTO system_settings(key,value) VALUES (:key,CAST(:value AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"), {"key": SETTINGS_KEY, "value": json.dumps(policy)})


def normalize_hosts(raw):
    hosts = []
    for host in re.split(r"[,\s]+", raw.strip()):
        if not host:
            continue
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            host = host.rstrip(".").encode("idna").decode("ascii").lower()
            if len(host) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in host.split(".")):
                raise ValueError("Scopes must be exact DNS names or IP addresses, without URLs, ports or wildcards")
        if host not in hosts:
            hosts.append(host)
    if len(hosts) > 32:
        raise ValueError("At most 32 host scopes are allowed")
    return hosts


def parse_trust_certificate(data, label, raw_hosts):
    if len(data) > 65536:
        raise ValueError("Certificate must be at most 64 KiB")
    if b"PRIVATE KEY" in data:
        raise ValueError("Upload the public certificate only, without a private key")
    if b"-----BEGIN" in data:
        if data.count(b"-----BEGIN CERTIFICATE-----") != 1:
            raise ValueError("Upload one certificate at a time")
        cert = x509.load_pem_x509_certificate(data)
    else:
        cert = x509.load_der_x509_certificate(data)
    now = datetime.now(timezone.utc)
    if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
        raise ValueError("Certificate is expired or not yet valid")
    hosts = normalize_hosts(raw_hosts)
    try:
        is_ca = cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        is_ca = False
    if is_ca:
        try:
            if not cert.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
                raise ValueError("CA certificate does not permit certificate signing")
        except x509.ExtensionNotFound:
            pass
    else:
        if not hosts:
            raise ValueError("Self-signed service certificates require at least one exact hostname or IP scope")
        if cert.issuer != cert.subject:
            raise ValueError("Upload the issuing CA, or a self-signed service certificate with a host scope")
        cert.verify_directly_issued_by(cert)
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound:
            raise ValueError("Service certificate must contain subject alternative names matching its scopes") from None
        dns = [name.lower().rstrip(".") for name in san.get_values_for_type(x509.DNSName)]
        ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        for host in hosts:
            try:
                ipaddress.ip_address(host)
                matches = host in ips
            except ValueError:
                matches = any(host == name or (name.startswith("*.") and host.count(".") == name.count(".") and host.endswith(name[1:])) for name in dns)
            if not matches:
                raise ValueError(f"Certificate subject alternative names do not match scope {host}")
    return {
        "id": cert.fingerprint(hashes.SHA256()).hex(),
        "label": label.strip()[:100] or cert.subject.rfc4514_string()[:100],
        "pem": cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        "hosts": hosts, "is_ca": is_ca,
        "subject": cert.subject.rfc4514_string(), "issuer": cert.issuer.rfc4514_string(),
        "not_before": cert.not_valid_before_utc.isoformat(), "not_after": cert.not_valid_after_utc.isoformat(),
    }
