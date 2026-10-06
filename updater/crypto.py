"""Cryptographic operations for update verification."""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

logger = logging.getLogger("zenplus.updater")


def generate_keypair(
    private_key_path: str, public_key_path: str
) -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate a new Ed25519 keypair and save to files."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    priv_path = Path(private_key_path)
    pub_path = Path(public_key_path)
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.parent.mkdir(parents=True, exist_ok=True)

    priv_pem = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    pub_pem = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)

    priv_path.write_bytes(priv_pem)
    priv_path.chmod(0o600)
    pub_path.write_bytes(pub_pem)

    logger.info("Generated Ed25519 keypair: %s, %s", private_key_path, public_key_path)
    return private_key, public_key


def load_public_key(path: str) -> Ed25519PublicKey:
    """Load an Ed25519 public key from PEM file."""
    key_data = Path(path).read_bytes()
    key = load_pem_public_key(key_data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"Key at {path} is not an Ed25519 public key")
    return key


def load_private_key(path: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from PEM file."""
    key_data = Path(path).read_bytes()
    key = load_pem_private_key(key_data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"Key at {path} is not an Ed25519 private key")
    return key


def sign_data(data: bytes, private_key: Ed25519PrivateKey) -> bytes:
    """Sign data with Ed25519 private key."""
    return private_key.sign(data)


def verify_signature(
    data: bytes, signature: bytes, public_key: Ed25519PublicKey
) -> bool:
    """Verify an Ed25519 signature. Returns True if valid, False otherwise."""
    try:
        public_key.verify(signature, data)
        return True
    except Exception:
        return False


def sha256_file(file_path: str) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Compute SHA-256 hash of bytes."""
    return hashlib.sha256(data).hexdigest()


def verify_checksums(checksums_file: str, base_dir: str) -> list[str]:
    """Verify all file checksums from a checksums.sha256 file.

    Returns list of files that failed verification (empty = all good).
    """
    failures = []
    base = Path(base_dir)

    with open(checksums_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("  ", 1)
            if len(parts) != 2:
                continue
            expected_hash, file_name = parts
            file_path = base / file_name
            if not file_path.exists():
                failures.append(f"MISSING: {file_name}")
                continue
            actual_hash = sha256_file(str(file_path))
            if actual_hash != expected_hash:
                failures.append(f"MISMATCH: {file_name}")

    return failures


def public_key_fingerprint(public_key: Ed25519PublicKey) -> str:
    """SHA-256 of the raw 32-byte public key; the identifier used to revoke a key."""
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def verify_manifest_bytes(
    manifest_data: bytes,
    signature: bytes,
    public_key_paths: "str | Path | list[str] | tuple[str, ...]",
    max_age_days: int = 30,
    revoked_fingerprints: "frozenset[str] | set[str]" = frozenset(),
) -> dict:
    """Verify and parse manifest bytes that are still held in memory.

    Checks:
    1. The Ed25519 signature is valid for at least one trusted, non-revoked key
    2. Manifest is not older than max_age_days
    3. JSON is well-formed

    Returns the parsed manifest dict on success, raises SecurityError on failure.
    Takes bytes, not a path, so a package can be authenticated before any of its
    contents are written to disk.
    """
    if isinstance(public_key_paths, (str, Path)):
        public_key_paths = [str(public_key_paths)]

    keys = []
    for key_path in public_key_paths:
        try:
            keys.append(load_public_key(str(key_path)))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unusable trust key %s: %s", key_path, exc)
    if not keys:
        raise SecurityError("No usable release public key is installed")

    verified = False
    for key in keys:
        if public_key_fingerprint(key) in revoked_fingerprints:
            continue
        if verify_signature(manifest_data, signature, key):
            verified = True
            break
    if not verified:
        raise SecurityError("Manifest signature verification failed")

    try:
        manifest = json.loads(manifest_data)
        release_date = datetime.fromisoformat(manifest["release_date"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SecurityError(f"Signed manifest is malformed: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SecurityError("Signed manifest is not a JSON object")

    if release_date.tzinfo is None:
        release_date = release_date.replace(tzinfo=timezone.utc)
    max_age = timedelta(days=max_age_days)
    now = datetime.now(timezone.utc)

    if now - release_date > max_age:
        raise SecurityError(
            f"Manifest too old: released {release_date.isoformat()}, "
            f"max age is {max_age_days} days"
        )

    if release_date > now + timedelta(hours=24):
        raise SecurityError(
            f"Manifest date is in the future: {release_date.isoformat()}"
        )

    logger.info(
        "Manifest verified: version=%s, released=%s",
        manifest.get("version"),
        manifest.get("release_date"),
    )
    return manifest


def verify_manifest(
    manifest_path: str,
    signature_path: str,
    public_key_path: str,
    max_age_days: int = 30,
) -> dict:
    """Verify and parse a manifest file (path-based wrapper, kept for callers)."""
    return verify_manifest_bytes(
        Path(manifest_path).read_bytes(),
        Path(signature_path).read_bytes(),
        public_key_path,
        max_age_days=max_age_days,
    )


class SecurityError(Exception):
    """Raised when a security check fails."""
