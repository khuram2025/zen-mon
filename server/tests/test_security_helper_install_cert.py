"""zenplus-security-helper install-cert (ZP-08).

The staging directory belongs to the service account, so the root helper must
never follow a link out of it or copy a staged file verbatim: it snapshots each
file with O_NOFOLLOW into a root-only work directory and installs only
re-encoded certificate/key material. Real certificates (RSA and EC, chains with
several intermediates) must keep installing exactly as before.

The helper is sourced (it only runs main() when executed), its fixed paths are
pointed at a fake root, and `install -o root -g root` is shimmed so the test
runs unprivileged.
"""
from __future__ import annotations

import datetime as dt
import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "zenplus-security-helper"

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "openssl"], capture_output=True).returncode != 0,
    reason="openssl is required",
)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject_cn, subject_key, issuer_cn, issuer_key, *, ca):
    now = dt.datetime.now(dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject_cn))
        .issuer_name(_name(issuer_cn))
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if not ca:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName("zenplus.example.test")]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture(scope="module")
def pki():
    """Root -> intermediate A -> intermediate B -> RSA leaf and EC leaf."""
    root_key = ec.generate_private_key(ec.SECP256R1())
    int_a_key = ec.generate_private_key(ec.SECP256R1())
    int_b_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ec_key = ec.generate_private_key(ec.SECP384R1())
    root = _cert("Test Root", root_key, "Test Root", root_key, ca=True)
    int_a = _cert("Intermediate A", int_a_key, "Test Root", root_key, ca=True)
    int_b = _cert("Intermediate B", int_b_key, "Intermediate A", int_a_key, ca=True)
    return {
        "chain": [int_b, int_a],
        "root": root,
        "rsa": (_cert("rsa leaf", rsa_key, "Intermediate B", int_b_key, ca=False), rsa_key),
        "ec": (_cert("ec leaf", ec_key, "Intermediate B", int_b_key, ca=False), ec_key),
    }


@pytest.fixture
def box(tmp_path):
    staging = tmp_path / "opt" / "data" / "tls-staging"
    staging.mkdir(parents=True, mode=0o700)
    tls = tmp_path / "etc" / "zenplus" / "tls"
    work = tmp_path / "work"
    work.mkdir()
    return {"root": tmp_path, "staging": staging, "tls": tls, "work": work}


def run_install(box) -> subprocess.CompletedProcess:
    script = f"""
set -euo pipefail
source {shlex.quote(str(HELPER))}
STAGING_DIR={shlex.quote(str(box['staging']))}
TLS_DIR={shlex.quote(str(box['tls']))}
STATE_FILE="$TLS_DIR/state.json"
CERT_WORK_PARENT={shlex.quote(str(box['work']))}
install() {{
    local args=()
    while [ $# -gt 0 ]; do
        case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
    done
    command install "${{args[@]}}"
}}
install_cert
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)


def stage(box, name: str, data: bytes) -> Path:
    path = box["staging"] / name
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def key_pem(key, fmt=serialization.PrivateFormat.PKCS8) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption())


def blocks(data: bytes, label: str) -> int:
    return data.count(f"-----BEGIN {label}-----".encode())


def assert_nothing_installed(box):
    assert not box["tls"].exists() or not any(box["tls"].iterdir())


@pytest.mark.parametrize("kind,fmt", [
    ("rsa", serialization.PrivateFormat.TraditionalOpenSSL),   # BEGIN RSA PRIVATE KEY
    ("rsa", serialization.PrivateFormat.PKCS8),
    ("ec", serialization.PrivateFormat.TraditionalOpenSSL),    # BEGIN EC PRIVATE KEY
])
def test_real_certificates_with_a_multi_intermediate_chain_install(pki, box, kind, fmt):
    leaf, key = pki[kind]
    chain = b"".join(_pem(c) for c in pki["chain"])
    stage(box, "server.crt", _pem(leaf))
    stage(box, "server.key", key_pem(key, fmt))
    stage(box, "chain.crt", chain)

    result = run_install(box)
    assert result.returncode == 0, result.stderr
    assert "certificate installed" in result.stdout

    tls = box["tls"]
    assert x509.load_pem_x509_certificate((tls / "server.crt").read_bytes()) == leaf
    installed_chain = x509.load_pem_x509_certificates((tls / "chain.crt").read_bytes())
    assert installed_chain == pki["chain"], "intermediates must stay complete and in order"
    full = x509.load_pem_x509_certificates((tls / "fullchain.pem").read_bytes())
    assert full == [leaf, *pki["chain"]]
    installed_key = serialization.load_pem_private_key((tls / "server.key").read_bytes(), None)
    assert installed_key.public_key().public_numbers() == key.public_key().public_numbers()
    assert stat.S_IMODE((tls / "server.key").stat().st_mode) == 0o600
    for name in ("server.crt", "chain.crt", "fullchain.pem"):
        assert stat.S_IMODE((tls / name).stat().st_mode) == 0o644
    assert not (box["staging"] / "server.key").exists(), "staged key is removed after install"
    assert not any(box["work"].iterdir()), "work directory is cleaned up"


def test_certificate_without_chain_installs_and_clears_an_old_chain(pki, box):
    leaf, key = pki["rsa"]
    box["tls"].mkdir(parents=True)
    (box["tls"] / "chain.crt").write_bytes(_pem(pki["root"]))
    stage(box, "server.crt", _pem(leaf))
    stage(box, "server.key", key_pem(key))
    result = run_install(box)
    assert result.returncode == 0, result.stderr
    assert not (box["tls"] / "chain.crt").exists()
    assert (box["tls"] / "fullchain.pem").read_bytes() == (box["tls"] / "server.crt").read_bytes()


def test_only_reencoded_material_is_published(pki, box):
    """A PEM bundle carrying other blocks (a key, text) is never copied verbatim."""
    leaf, key = pki["rsa"]
    other_key = key_pem(ec.generate_private_key(ec.SECP256R1()))
    stage(box, "server.crt", b"Bag Attributes: junk\n" + _pem(leaf) + other_key)
    stage(box, "server.key", key_pem(key) + b"trailing text\n")
    stage(box, "chain.crt", _pem(pki["chain"][0]) + other_key + b"comment\n" + _pem(pki["chain"][1]))
    result = run_install(box)
    assert result.returncode == 0, result.stderr
    for name in ("server.crt", "chain.crt", "fullchain.pem"):
        data = (box["tls"] / name).read_bytes()
        assert b"PRIVATE KEY" not in data, f"{name} leaked a key block"
        assert b"junk" not in data and b"comment" not in data
    assert blocks((box["tls"] / "server.crt").read_bytes(), "CERTIFICATE") == 1
    assert blocks((box["tls"] / "chain.crt").read_bytes(), "CERTIFICATE") == 2
    assert blocks((box["tls"] / "server.key").read_bytes(), "PRIVATE KEY") == 1


@pytest.mark.parametrize("name", ["server.crt", "server.key", "chain.crt"])
def test_a_symlinked_staged_file_is_refused(pki, box, name):
    """The 'PEM bundle' read of a root-only file: never followed, nothing installed."""
    leaf, key = pki["rsa"]
    secret = box["root"] / "root-only-secret.pem"
    contents = {"server.crt": _pem(leaf), "server.key": key_pem(key),
                "chain.crt": _pem(pki["chain"][0]) + key_pem(key)}
    secret.write_bytes(contents[name])
    for staged, data in contents.items():
        if staged != name:
            stage(box, staged, data)
    (box["staging"] / name).symlink_to(secret)
    result = run_install(box)
    assert result.returncode != 0
    assert "refused" in result.stderr
    assert_nothing_installed(box)


def test_a_symlinked_staging_directory_is_refused(pki, box, tmp_path):
    leaf, key = pki["rsa"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "server.crt").write_bytes(_pem(leaf))
    (elsewhere / "server.key").write_bytes(key_pem(key))
    box["staging"].rmdir()
    box["staging"].symlink_to(elsewhere, target_is_directory=True)
    result = run_install(box)
    assert result.returncode != 0
    assert_nothing_installed(box)


def test_fifo_and_oversized_files_are_refused_without_hanging(pki, box):
    leaf, key = pki["rsa"]
    stage(box, "server.key", key_pem(key))
    os.mkfifo(box["staging"] / "server.crt")
    result = run_install(box)
    assert result.returncode != 0 and "refused" in result.stderr
    (box["staging"] / "server.crt").unlink()
    stage(box, "server.crt", _pem(leaf) + b"#" * (1024 * 1024 + 1))
    result = run_install(box)
    assert result.returncode != 0 and "refused" in result.stderr
    assert_nothing_installed(box)


def test_validation_failures_keep_their_messages(pki, box):
    leaf, _ = pki["rsa"]
    _, other = pki["ec"]
    result = run_install(box)
    assert result.returncode != 0 and "no staged certificate" in result.stderr
    stage(box, "server.crt", _pem(leaf))
    result = run_install(box)
    assert result.returncode != 0 and "no staged private key" in result.stderr
    stage(box, "server.key", key_pem(other))
    result = run_install(box)
    assert result.returncode != 0 and "does not match the certificate" in result.stderr
    stage(box, "server.key", b"not a key\n")
    result = run_install(box)
    assert result.returncode != 0 and "not a valid PEM key" in result.stderr
    stage(box, "server.crt", b"not a certificate\n")
    result = run_install(box)
    assert result.returncode != 0 and "not a valid PEM X.509 certificate" in result.stderr
    assert_nothing_installed(box)


def test_a_chain_without_certificates_is_refused(pki, box):
    leaf, key = pki["rsa"]
    stage(box, "server.crt", _pem(leaf))
    stage(box, "server.key", key_pem(key))
    stage(box, "chain.crt", key_pem(key))
    result = run_install(box)
    assert result.returncode != 0 and "staged chain is not valid PEM" in result.stderr
    assert_nothing_installed(box)


def test_generated_site_logs_without_query_strings():
    text = HELPER.read_text()
    assert "access_log /var/log/nginx/access.log zenplus_safe;" in text
    assert "log_format zenplus_safe " in text  # guarded on the format existing
