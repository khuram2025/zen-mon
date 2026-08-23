"""Security settings API — TLS / HTTPS hardening for the appliance.

Backs the Settings -> General -> Security tab. Lets an administrator:

  * generate a self-signed certificate the appliance serves immediately
  * generate a CSR for an enterprise CA (e.g. Active Directory Certificate
    Services), then install the CA-issued certificate against the kept key
  * upload an existing certificate + key (PEM) or a PKCS#12/PFX bundle
  * enable/disable HTTPS, HTTP->HTTPS redirect, HSTS and the minimum TLS
    version served by nginx
  * download the active certificate for distributing trust to browsers,
    agents and sensors

Endpoints (all under /api/v1/system/security, admin-only unless noted):

  GET    /system/security/tls                  Full status snapshot
  PUT    /system/security/tls/config           Apply HTTPS/TLS options
  POST   /system/security/tls/self-signed      Generate + install self-signed
  POST   /system/security/tls/csr              Generate CSR (key kept server-side)
  DELETE /system/security/tls/csr              Discard the pending CSR/key
  POST   /system/security/tls/certificate      Install PEM cert (+key/+chain)
  POST   /system/security/tls/pfx              Install from a PFX/P12 bundle
  DELETE /system/security/tls/certificate      Remove installed certificate
  GET    /system/security/tls/certificate/download   Public cert PEM (any user)

Privilege model: this process runs as the unprivileged ``zenplus`` user. All
key/cert generation and parsing happens here (python-cryptography), but files
are only *staged* into /opt/zenplus/data/tls-staging; the root-owned helper
/usr/local/sbin/zenplus-security-helper (granted via /etc/sudoers.d/
zenplus-security, installed by scripts/setup-security.sh) validates the staged
material, installs it into /etc/zenplus/tls, regenerates the nginx site config
from its own embedded template and reloads nginx. The helper never accepts
paths or config text from this process.
"""

import asyncio
import ipaddress
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_admin_user
from app.models.user import User
from app.services.audit_service import write_audit_log
from app.api.v1.settings import _get_system_setting, _upsert_system_setting

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

router = APIRouter(prefix="/system/security", tags=["Security Settings"])

SECURITY_HELPER = "/usr/local/sbin/zenplus-security-helper"
STAGING_DIR = "/opt/zenplus/data/tls-staging"
TLS_DIR = "/etc/zenplus/tls"
STATE_FILE = os.path.join(TLS_DIR, "state.json")
CERT_FILE = os.path.join(TLS_DIR, "server.crt")
CHAIN_FILE = os.path.join(TLS_DIR, "chain.crt")

TLS_SETTINGS_KEY = "security.tls"

HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(\*\.)?([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class TlsConfig(BaseModel):
    https_enabled: bool = False
    redirect_http: bool = True
    hsts_enabled: bool = False
    min_tls_version: str = Field("1.2", pattern="^1\\.[23]$")


class SelfSignedRequest(BaseModel):
    common_name: str = Field(..., min_length=1, max_length=64)
    san_dns: list[str] = Field(default_factory=list, max_length=20)
    san_ips: list[str] = Field(default_factory=list, max_length=20)
    days_valid: int = Field(1095, ge=1, le=3650)
    key_type: str = Field("rsa2048", pattern="^(rsa2048|rsa4096|ecdsa-p256)$")


class CsrRequest(BaseModel):
    common_name: str = Field(..., min_length=1, max_length=64)
    san_dns: list[str] = Field(default_factory=list, max_length=20)
    san_ips: list[str] = Field(default_factory=list, max_length=20)
    organization: str = Field("", max_length=64)
    organizational_unit: str = Field("", max_length=64)
    country: str = Field("", max_length=2)
    state: str = Field("", max_length=64)
    locality: str = Field("", max_length=64)
    key_type: str = Field("rsa2048", pattern="^(rsa2048|rsa4096|ecdsa-p256)$")


class CertificateUpload(BaseModel):
    certificate_pem: str = Field(..., min_length=1, max_length=65536)
    private_key_pem: str = Field("", max_length=65536)
    chain_pem: str = Field("", max_length=131072)
    key_passphrase: str = Field("", max_length=256)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_helper(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sudo", "-n", SECURITY_HELPER, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _helper_installed() -> bool:
    return os.path.exists(SECURITY_HELPER) and os.path.exists("/etc/sudoers.d/zenplus-security")


def _raise_helper_error(proc: subprocess.CompletedProcess, action: str) -> None:
    detail = (proc.stderr or proc.stdout or "").strip()[-500:]
    if "password is required" in detail or "sudo:" in detail and "sudoers" in detail:
        raise HTTPException(
            status_code=503,
            detail="Security helper is not authorized. Run "
                   "'sudo bash scripts/setup-security.sh' on the appliance, then retry.",
        )
    raise HTTPException(status_code=500, detail=f"{action} failed: {detail or 'unknown error'}")


async def _sudo_helper(*args: str, action: str, timeout: int = 60) -> str:
    if not _helper_installed():
        raise HTTPException(
            status_code=503,
            detail="Security helper is not installed. Run "
                   "'sudo bash scripts/setup-security.sh' on the appliance first.",
        )
    try:
        proc = await asyncio.to_thread(_run_helper, *args, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail=f"{action} timed out")
    if proc.returncode != 0:
        _raise_helper_error(proc, action)
    return proc.stdout


def _validate_sans(san_dns: list[str], san_ips: list[str]) -> list[x509.GeneralName]:
    names: list[x509.GeneralName] = []
    for h in san_dns:
        h = h.strip()
        if not h:
            continue
        if not HOSTNAME_RE.match(h):
            raise HTTPException(status_code=422, detail=f"Invalid DNS name: {h}")
        names.append(x509.DNSName(h))
    for ip in san_ips:
        ip = ip.strip()
        if not ip:
            continue
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid IP address: {ip}")
    return names


def _generate_key(key_type: str):
    if key_type == "rsa4096":
        return rsa.generate_private_key(public_exponent=65537, key_size=4096)
    if key_type == "ecdsa-p256":
        return ec.generate_private_key(ec.SECP256R1())
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _stage_write(name: str, data: bytes) -> None:
    os.makedirs(STAGING_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(STAGING_DIR, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _stage_path(name: str) -> str:
    return os.path.join(STAGING_DIR, name)


def _stage_clear(*names: str) -> None:
    for name in names:
        try:
            os.remove(_stage_path(name))
        except FileNotFoundError:
            pass


async def _install_staged_pair(cert_pem: bytes, key_pem: bytes,
                               chain_pem: Optional[bytes]) -> None:
    """Stage a validated cert/key(/chain) and have the root helper install it."""
    _stage_write("server.crt", cert_pem)
    _stage_write("server.key", key_pem)
    if chain_pem:
        _stage_write("chain.crt", chain_pem)
    else:
        _stage_clear("chain.crt")
    try:
        await _sudo_helper("install-cert", action="Certificate install")
    finally:
        # The helper deletes the staged key on success; make sure neither the
        # key nor stale cert material lingers on failure either.
        _stage_clear("server.crt", "server.key", "chain.crt")


def _cert_info(cert: x509.Certificate) -> dict:
    def _cn(name: x509.Name) -> str:
        attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return str(attrs[0].value) if attrs else name.rfc4514_string()

    san_dns: list[str] = []
    san_ips: list[str] = []
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        san_dns = san.get_values_for_type(x509.DNSName)
        san_ips = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        pass

    not_after = cert.not_valid_after_utc
    days_remaining = (not_after - datetime.now(timezone.utc)).days
    return {
        "subject": _cn(cert.subject),
        "issuer": _cn(cert.issuer),
        "self_signed": cert.subject == cert.issuer,
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": not_after.isoformat(),
        "days_remaining": days_remaining,
        "san_dns": list(san_dns),
        "san_ips": san_ips,
        "fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
        "key_algorithm": cert.public_key().__class__.__name__.replace("_", "").replace("PublicKey", ""),
        "serial_number": format(cert.serial_number, "x"),
    }


def _load_installed_cert() -> Optional[dict]:
    try:
        with open(CERT_FILE, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        info = _cert_info(x509.load_pem_x509_certificate(data))
        info["chain_installed"] = os.path.exists(CHAIN_FILE)
        return info
    except Exception:
        return {"subject": "(unparseable certificate)", "self_signed": False}


def _load_applied_state() -> Optional[dict]:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _load_pending_csr() -> Optional[dict]:
    try:
        with open(_stage_path("pending.json"), "r") as f:
            meta = json.load(f)
        with open(_stage_path("pending.csr"), "r") as f:
            meta["csr_pem"] = f.read()
        return meta
    except (OSError, ValueError):
        return None


def _parse_private_key(pem: bytes, passphrase: str):
    pw = passphrase.encode() if passphrase else None
    try:
        return serialization.load_pem_private_key(pem, password=pw)
    except TypeError:
        raise HTTPException(status_code=422,
                            detail="Private key is encrypted — provide its passphrase")
    except ValueError:
        raise HTTPException(status_code=422,
                            detail="Could not parse the private key (or wrong passphrase)")


def _public_keys_match(cert: x509.Certificate, key) -> bool:
    a = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    b = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return a == b


async def _get_tls_settings(db: AsyncSession) -> TlsConfig:
    raw = await _get_system_setting(db, TLS_SETTINGS_KEY)
    return TlsConfig(**raw) if raw else TlsConfig()


def _apply_args(cfg: TlsConfig) -> list[str]:
    return [
        f"https={'on' if cfg.https_enabled else 'off'}",
        f"redirect={'on' if cfg.redirect_http else 'off'}",
        f"hsts={'on' if cfg.hsts_enabled else 'off'}",
        f"min_tls={cfg.min_tls_version}",
    ]


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@router.get("/tls")
async def tls_status(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    pending = _load_pending_csr()
    return {
        "helper_installed": _helper_installed(),
        "settings": (await _get_tls_settings(db)).model_dump(),
        "applied": _load_applied_state(),
        "certificate": _load_installed_cert(),
        "pending_csr": pending and {
            "common_name": pending.get("common_name"),
            "created_at": pending.get("created_at"),
            "csr_pem": pending.get("csr_pem"),
        },
    }


# ---------------------------------------------------------------------------
# HTTPS / TLS configuration
# ---------------------------------------------------------------------------

@router.put("/tls/config")
async def update_tls_config(
    cfg: TlsConfig,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    if cfg.https_enabled and not os.path.exists(CERT_FILE):
        raise HTTPException(
            status_code=409,
            detail="No certificate installed. Generate a self-signed certificate "
                   "or install a CA-issued one before enabling HTTPS.",
        )
    await _sudo_helper("apply", *_apply_args(cfg), action="Applying TLS configuration",
                       timeout=90)
    await _upsert_system_setting(db, TLS_SETTINGS_KEY, cfg.model_dump())
    await write_audit_log(
        db, actor=user, action="security.tls.config", resource_type="system_settings",
        resource_id=TLS_SETTINGS_KEY, metadata=cfg.model_dump(),
    )
    return {"status": "applied", "settings": cfg.model_dump(), "applied": _load_applied_state()}


# ---------------------------------------------------------------------------
# Self-signed certificate
# ---------------------------------------------------------------------------

@router.post("/tls/self-signed")
async def generate_self_signed(
    req: SelfSignedRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    sans = _validate_sans(req.san_dns, req.san_ips)
    if not sans:
        # A cert without SANs is rejected by every modern client.
        try:
            sans = [x509.IPAddress(ipaddress.ip_address(req.common_name))]
        except ValueError:
            if not HOSTNAME_RE.match(req.common_name):
                raise HTTPException(status_code=422,
                                    detail="Common name must be a hostname or IP, "
                                           "or provide explicit SANs")
            sans = [x509.DNSName(req.common_name)]

    def _build() -> tuple[bytes, bytes]:
        key = _generate_key(req.key_type)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, req.common_name)])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(hours=1))
            .not_valid_after(now + timedelta(days=req.days_valid))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_encipherment=True, content_commitment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=False,
                    crl_sign=False, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM), _key_pem(key)

    cert_pem, key_pem = await asyncio.to_thread(_build)
    await _install_staged_pair(cert_pem, key_pem, None)
    await write_audit_log(
        db, actor=user, action="security.tls.self_signed", resource_type="certificate",
        resource_id=req.common_name,
        metadata={"days_valid": req.days_valid, "key_type": req.key_type},
    )
    return {"status": "installed", "certificate": _load_installed_cert()}


# ---------------------------------------------------------------------------
# CSR flow (enterprise / AD CS)
# ---------------------------------------------------------------------------

@router.post("/tls/csr")
async def generate_csr(
    req: CsrRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    sans = _validate_sans(req.san_dns, req.san_ips)
    if not sans:
        raise HTTPException(status_code=422,
                            detail="At least one SAN (DNS name or IP) is required")
    if req.country and not re.fullmatch(r"[A-Za-z]{2}", req.country):
        raise HTTPException(status_code=422, detail="Country must be a 2-letter code")

    def _build() -> tuple[bytes, bytes]:
        key = _generate_key(req.key_type)
        attrs = [x509.NameAttribute(NameOID.COMMON_NAME, req.common_name)]
        if req.organization:
            attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, req.organization))
        if req.organizational_unit:
            attrs.append(x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME,
                                            req.organizational_unit))
        if req.country:
            attrs.append(x509.NameAttribute(NameOID.COUNTRY_NAME, req.country.upper()))
        if req.state:
            attrs.append(x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, req.state))
        if req.locality:
            attrs.append(x509.NameAttribute(NameOID.LOCALITY_NAME, req.locality))
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name(attrs))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(key, hashes.SHA256())
        )
        return csr.public_bytes(serialization.Encoding.PEM), _key_pem(key)

    csr_pem, key_pem = await asyncio.to_thread(_build)
    # Keep the key server-side until the CA-issued certificate comes back.
    _stage_write("pending.key", key_pem)
    _stage_write("pending.csr", csr_pem)
    _stage_write("pending.json", json.dumps({
        "common_name": req.common_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).encode())
    await write_audit_log(
        db, actor=user, action="security.tls.csr_generated", resource_type="certificate",
        resource_id=req.common_name, metadata={"key_type": req.key_type},
    )
    return {"status": "pending", "csr_pem": csr_pem.decode()}


@router.delete("/tls/csr")
async def discard_csr(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    _stage_clear("pending.key", "pending.csr", "pending.json")
    await write_audit_log(
        db, actor=user, action="security.tls.csr_discarded", resource_type="certificate",
    )
    return {"status": "discarded"}


# ---------------------------------------------------------------------------
# Certificate install (PEM / PFX)
# ---------------------------------------------------------------------------

def _parse_cert_chain(pem: bytes) -> list[x509.Certificate]:
    try:
        return x509.load_pem_x509_certificates(pem)
    except ValueError:
        raise HTTPException(status_code=422,
                            detail="Could not parse PEM certificate data")


def _armour_if_bare(text: str) -> bytes:
    """Add PEM headers to a bare Base-64 body.

    The AD CS web enrolment page shows the issued certificate as Base-64 with
    no BEGIN/END lines, and operators paste exactly what they see. Wrapping it
    here is the difference between "it worked" and a parse error they have no
    way to interpret.
    """
    stripped = text.strip()
    if not stripped or "-----BEGIN" in stripped:
        return text.encode()
    body = "".join(stripped.split())
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", body):
        return text.encode()
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n".encode()


def _load_any_certificates(data: bytes) -> list[x509.Certificate]:
    """Parse certificates in any format a CA hands out.

    Active Directory Certificate Services offers four download options and
    operators use all of them: Base-64 X.509 (.cer, PEM), DER-encoded binary
    X.509 (.cer), and "download certificate chain" which produces PKCS#7
    (.p7b) in either encoding. Sniffing the container here means the UI can
    accept whatever the CA produced instead of asking the operator to convert
    it with openssl first.
    """
    if not data or not data.strip():
        raise HTTPException(status_code=422, detail="The uploaded file is empty")

    if b"-----BEGIN" in data:
        # PEM-armoured: either raw certificates or a PKCS#7 bundle.
        try:
            certs = x509.load_pem_x509_certificates(data)
            if certs:
                return certs
        except ValueError:
            pass
        try:
            certs = pkcs7.load_pem_pkcs7_certificates(data)
            if certs:
                return certs
        except Exception:
            pass
    else:
        # Binary: a bare DER certificate, or a DER PKCS#7 bundle.
        try:
            return [x509.load_der_x509_certificate(data)]
        except Exception:
            pass
        try:
            certs = pkcs7.load_der_pkcs7_certificates(data)
            if certs:
                return certs
        except Exception:
            pass

    raise HTTPException(
        status_code=422,
        detail="Unrecognised certificate file. Expected a .cer/.crt/.pem "
               "certificate (Base-64 or DER) or a .p7b certificate chain.",
    )


def _split_leaf_and_chain(
    certs: list[x509.Certificate], key=None
) -> tuple[x509.Certificate, list[x509.Certificate]]:
    """Pick the end-entity certificate out of a bundle, chain follows.

    A .p7b from AD CS contains the issued certificate plus every CA above it,
    in no guaranteed order, so the first element cannot be assumed to be the
    leaf. Prefer the one matching our private key; otherwise take the one that
    is not the issuer of any other certificate in the bundle.
    """
    if len(certs) == 1:
        return certs[0], []

    if key is not None:
        for cert in certs:
            if _public_keys_match(cert, key):
                return cert, [c for c in certs if c is not cert]

    issuers = {c.subject for c in certs}
    leaves = [
        c for c in certs
        if c.subject != c.issuer and not any(
            other is not c and other.issuer == c.subject for other in certs
        )
    ]
    if len(leaves) == 1:
        return leaves[0], [c for c in certs if c is not leaves[0]]
    # Ambiguous bundle — fall back to declared order rather than guessing wrong.
    _ = issuers
    return certs[0], certs[1:]


@router.post("/tls/certificate")
async def install_certificate(
    upload: CertificateUpload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    certs = _load_any_certificates(_armour_if_bare(upload.certificate_pem))
    if not certs:
        raise HTTPException(status_code=422, detail="No certificate found in upload")

    if upload.private_key_pem.strip():
        key = _parse_private_key(upload.private_key_pem.encode(), upload.key_passphrase)
        source = "uploaded"
    else:
        # No key supplied — this must be the CA's answer to our pending CSR.
        try:
            with open(_stage_path("pending.key"), "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
        except OSError:
            raise HTTPException(
                status_code=422,
                detail="No private key supplied and no pending CSR — generate a CSR "
                       "first or include the private key",
            )
        source = "csr"

    # Split only now: with the key in hand the leaf can be identified even when
    # the paste contains a whole chain in arbitrary order.
    cert, inline_chain = _split_leaf_and_chain(certs, key)

    if not _public_keys_match(cert, key):
        raise HTTPException(
            status_code=422,
            detail="The certificate does not match the private key"
                   + (" from the pending CSR" if source == "csr" else ""),
        )

    chain_parts = [c.public_bytes(serialization.Encoding.PEM) for c in inline_chain]
    if upload.chain_pem.strip():
        chain_parts += [c.public_bytes(serialization.Encoding.PEM)
                        for c in _load_any_certificates(_armour_if_bare(upload.chain_pem))]
    chain_pem = b"".join(chain_parts) if chain_parts else None

    await _install_staged_pair(
        cert.public_bytes(serialization.Encoding.PEM), _key_pem(key), chain_pem)
    if source == "csr":
        _stage_clear("pending.key", "pending.csr", "pending.json")

    info = _load_installed_cert()
    await write_audit_log(
        db, actor=user, action="security.tls.cert_installed", resource_type="certificate",
        resource_id=(info or {}).get("subject"), metadata={"source": source},
    )
    return {"status": "installed", "certificate": info}


@router.post("/tls/certificate/file")
async def install_certificate_file(
    file: UploadFile = File(...),
    chain_file: Optional[UploadFile] = File(None),
    key_file: Optional[UploadFile] = File(None),
    key_passphrase: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    """Install a CA-issued certificate from a file.

    This is the normal end of the CSR workflow: the operator downloads the
    issued certificate from the CA (for AD CS, the "Download certificate" or
    "Download certificate chain" link) and uploads the file as-is. The private
    key is the one held from the pending CSR unless a key file is supplied.
    """
    data = await file.read()
    if len(data) > 512 * 1024:
        raise HTTPException(status_code=422, detail="Certificate file too large")
    certs = _load_any_certificates(data)

    # Resolve the private key first — it identifies the leaf in a chain bundle.
    if key_file is not None:
        key_bytes = await key_file.read()
        key = _parse_private_key(key_bytes, key_passphrase)
        source = "uploaded"
    else:
        try:
            with open(_stage_path("pending.key"), "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
        except OSError:
            raise HTTPException(
                status_code=422,
                detail="No pending CSR on this appliance and no key file supplied. "
                       "Generate a CSR first, or upload the matching private key.",
            )
        source = "csr"

    cert, chain = _split_leaf_and_chain(certs, key)

    if not _public_keys_match(cert, key):
        raise HTTPException(
            status_code=422,
            detail="The certificate does not match the private key"
                   + (" from the pending CSR. Make sure this is the certificate "
                      "issued for that request." if source == "csr" else "."),
        )

    if chain_file is not None:
        extra = await chain_file.read()
        if extra.strip():
            chain += _load_any_certificates(extra)

    chain_pem = b"".join(
        c.public_bytes(serialization.Encoding.PEM) for c in chain) or None

    await _install_staged_pair(
        cert.public_bytes(serialization.Encoding.PEM), _key_pem(key), chain_pem)
    if source == "csr":
        _stage_clear("pending.key", "pending.csr", "pending.json")

    info = _load_installed_cert()
    await write_audit_log(
        db, actor=user, action="security.tls.cert_installed", resource_type="certificate",
        resource_id=(info or {}).get("subject"),
        metadata={"source": f"{source}_file", "filename": file.filename,
                  "chain_certs": len(chain)},
    )
    return {"status": "installed", "certificate": info, "chain_certificates": len(chain)}


@router.post("/tls/pfx")
async def install_pfx(
    file: UploadFile = File(...),
    password: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    data = await file.read()
    if len(data) > 256 * 1024:
        raise HTTPException(status_code=422, detail="PFX file too large")
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(
            data, password.encode() if password else None)
    except Exception:
        raise HTTPException(status_code=422,
                            detail="Could not parse the PFX/P12 bundle (wrong password?)")
    if key is None or cert is None:
        raise HTTPException(status_code=422,
                            detail="PFX bundle must contain both a certificate and its key")

    chain_pem = b"".join(
        c.public_bytes(serialization.Encoding.PEM) for c in (extra or [])) or None
    await _install_staged_pair(
        cert.public_bytes(serialization.Encoding.PEM), _key_pem(key), chain_pem)

    info = _load_installed_cert()
    await write_audit_log(
        db, actor=user, action="security.tls.cert_installed", resource_type="certificate",
        resource_id=(info or {}).get("subject"), metadata={"source": "pfx"},
    )
    return {"status": "installed", "certificate": info}


@router.delete("/tls/certificate")
async def remove_certificate(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    cfg = await _get_tls_settings(db)
    if cfg.https_enabled:
        raise HTTPException(status_code=409,
                            detail="Disable HTTPS before removing the certificate")
    await _sudo_helper("remove-cert", action="Certificate removal")
    await write_audit_log(
        db, actor=user, action="security.tls.cert_removed", resource_type="certificate",
    )
    return {"status": "removed"}


@router.get("/tls/certificate/download")
async def download_certificate(user: User = Depends(get_current_user)):
    """Public certificate PEM — for adding the appliance to trust stores
    (browsers, AD GPO, agent hosts). Available to any authenticated user."""
    try:
        with open(CERT_FILE, "rb") as f:
            data = f.read()
    except OSError:
        raise HTTPException(status_code=404, detail="No certificate installed")
    return Response(
        content=data,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="zenplus-server.crt"'},
    )
