"""Authenticate an OTA package before anything from it is written to disk.

The updater runs as root and installs whatever a package contains, so the
order of operations matters:

1. Look at the archive's table of contents and refuse anything that is not a
   plain file or directory with a safe relative name (no links, no devices, no
   absolute or ``..`` paths, size and count limits).
2. Read ``manifest.json`` and its signature into memory and verify the
   signature against the root-owned release keys.
3. Require the signed manifest to carry a complete per-file SHA-256 inventory
   (``payload_files``) that names exactly the files in the archive.
4. Only then write the files, one at a time, hashing each one against the
   signed inventory as it is written.

Nothing is handed to ``tarfile.extractall``; files are created by this module
with ``O_EXCL | O_NOFOLLOW`` inside a fresh private directory.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tarfile
import zlib
from pathlib import Path

from .crypto import SecurityError, verify_manifest_bytes

logger = logging.getLogger("zenplus.updater")

INVENTORY_KEY = "payload_files"
# Files that cannot appear in the signed inventory: the manifest and its
# signature are what carry it, and checksums.sha256 is written after signing.
INVENTORY_EXCLUDED = frozenset({"manifest.json", "manifest.json.sig", "checksums.sha256"})

MAX_MEMBERS = 50_000
MAX_FILE_BYTES = 2 * 1024**3
MAX_TOTAL_BYTES = 6 * 1024**3
MAX_MANIFEST_BYTES = 16 * 1024**2
MAX_SIGNATURE_BYTES = 4096
_CHUNK = 1024 * 1024
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _check_name(name: str) -> None:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise SecurityError(f"Unsafe path in package: {name!r}")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise SecurityError(f"Unsafe path in package: {name!r}")


def screen_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Return the archive members, or raise if any could escape or exhaust disk."""
    members = tar.getmembers()
    if len(members) > MAX_MEMBERS:
        raise SecurityError(f"Package has too many entries ({len(members)})")

    seen: set[str] = set()
    total = 0
    for member in members:
        _check_name(member.name)
        if member.name in seen:
            raise SecurityError(f"Duplicate entry in package: {member.name}")
        seen.add(member.name)
        if member.isdir():
            continue
        if not member.isreg():
            kind = "link" if (member.issym() or member.islnk()) else "special file"
            raise SecurityError(f"Package contains a {kind}: {member.name}")
        if member.size > MAX_FILE_BYTES:
            raise SecurityError(f"Package file is too large: {member.name}")
        total += member.size
        if total > MAX_TOTAL_BYTES:
            raise SecurityError("Package is too large when unpacked")
    return members


def _read_small(tar: tarfile.TarFile, member: tarfile.TarInfo, limit: int) -> bytes:
    if member.size > limit:
        raise SecurityError(f"{member.name} is larger than {limit} bytes")
    handle = tar.extractfile(member)
    if handle is None:
        raise SecurityError(f"{member.name} is not a regular file")
    with handle:
        return handle.read()


def _validate_inventory(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise SecurityError("Signed file inventory is empty or malformed")
    inventory: dict[str, str] = {}
    for name, digest in raw.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise SecurityError("Signed file inventory is malformed")
        _check_name(name)
        if name in INVENTORY_EXCLUDED:
            raise SecurityError(f"Signed file inventory lists a reserved file: {name}")
        if not _SHA256_HEX.match(digest):
            raise SecurityError(f"Signed file inventory has an invalid hash for {name}")
        inventory[name] = digest
    return inventory


def _compare_inventory(member_files: set[str], inventory: dict[str, str]) -> None:
    extra = sorted(member_files - set(inventory))
    missing = sorted(set(inventory) - member_files)
    if extra:
        raise SecurityError(
            f"Package contains {len(extra)} file(s) not in the signed inventory, "
            f"e.g. {extra[0]}"
        )
    if missing:
        raise SecurityError(
            f"Package is missing {len(missing)} file(s) listed in the signed inventory, "
            f"e.g. {missing[0]}"
        )


def _write_member(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    target: Path,
    expected_sha256: str | None,
) -> None:
    source = tar.extractfile(member)
    if source is None:
        raise SecurityError(f"{member.name} is not a regular file")
    digest = hashlib.sha256()
    written = 0
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with source, os.fdopen(fd, "wb") as out:
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > member.size:
                raise SecurityError(f"{member.name} is larger than its header says")
            digest.update(chunk)
            out.write(chunk)
    if written != member.size:
        raise SecurityError(f"{member.name} is truncated")
    if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
        raise SecurityError(f"Hash mismatch for {member.name}: not what the signed manifest lists")
    os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)


def verify_and_extract(
    package_path: str,
    extract_dir: str,
    *,
    public_key_paths,
    max_age_days: int = 30,
    revoked_fingerprints=frozenset(),
    allow_legacy: bool = False,
    fallback_signature: bytes = b"",
    expected_version: str | None = None,
) -> dict:
    """Verify ``package_path`` and unpack it into the new directory ``extract_dir``.

    Returns the verified manifest. Raises SecurityError for anything wrong with
    the archive or its signature; on failure the directory this function created
    is removed (a directory that already existed is never touched).
    """
    created = False
    try:
        with tarfile.open(package_path, "r:gz") as tar:
            members = screen_members(tar)
            by_name = {m.name: m for m in members}

            manifest_member = by_name.get("manifest.json")
            if manifest_member is None or not manifest_member.isreg():
                raise SecurityError("Package missing manifest.json")
            manifest_bytes = _read_small(tar, manifest_member, MAX_MANIFEST_BYTES)

            sig_member = by_name.get("manifest.json.sig")
            if sig_member is not None:
                if not sig_member.isreg():
                    raise SecurityError("manifest.json.sig is not a regular file")
                signature = _read_small(tar, sig_member, MAX_SIGNATURE_BYTES)
            else:
                signature = fallback_signature
            if not signature:
                raise SecurityError("Package has no manifest signature")

            manifest = verify_manifest_bytes(
                manifest_bytes,
                signature,
                public_key_paths,
                max_age_days=max_age_days,
                revoked_fingerprints=revoked_fingerprints,
            )
            if expected_version is not None and manifest.get("version") != expected_version:
                raise SecurityError(
                    f"Package is version {manifest.get('version')!r}, "
                    f"but the server offered {expected_version!r}"
                )

            file_names = {m.name for m in members if m.isreg()}
            raw_inventory = manifest.get(INVENTORY_KEY)
            if raw_inventory is None:
                if not allow_legacy:
                    raise SecurityError(
                        "Package manifest has no signed file inventory; refusing to install it"
                    )
                logger.warning(
                    "Installing a legacy package without a signed file inventory "
                    "(allow_legacy_packages is on)"
                )
                inventory: dict[str, str] | None = None
            else:
                inventory = _validate_inventory(raw_inventory)
                _compare_inventory(file_names - INVENTORY_EXCLUDED, inventory)

            os.mkdir(extract_dir, 0o700)
            created = True
            root = Path(extract_dir)
            for member in members:
                target = root / member.name
                if member.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    os.chmod(target, 0o755)
                    continue
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                if member.name == "manifest.json":
                    _write_verified_bytes(target, manifest_bytes)
                elif member.name == "manifest.json.sig":
                    _write_verified_bytes(target, signature)
                else:
                    expected = inventory.get(member.name) if inventory is not None else None
                    _write_member(tar, member, target, expected)
            if sig_member is None:
                _write_verified_bytes(root / "manifest.json.sig", signature)
            # The extraction directory is private until now; make it traversable
            # by the service accounts that read hook scripts and migrations.
            os.chmod(root, 0o755)
            return manifest
    except SecurityError:
        if created:
            shutil.rmtree(extract_dir, ignore_errors=True)
        raise
    except (tarfile.TarError, EOFError, zlib.error, OSError) as exc:
        if created:
            shutil.rmtree(extract_dir, ignore_errors=True)
        raise SecurityError(f"Package could not be unpacked safely: {exc}") from exc


def _write_verified_bytes(target: Path, data: bytes) -> None:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.chmod(target, 0o644)
