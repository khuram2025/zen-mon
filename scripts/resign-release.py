#!/usr/bin/env python3
"""Re-sign a lab-tested OTA package with the production key.

The package that was tested on lab appliances carries a manifest signed with the
*test* key, which customer appliances do not trust. This tool checks that package
completely, then replaces its release signatures. Bundled sensor manifests are
re-signed too: their signature hashes in the outer inventory and checksums change,
but application files, binaries and sensor manifest bytes stay unchanged. Verify
and test the final signed artifact before publishing. The production key never
has to exist on the development machine.

Checks before anything is signed (all of them must pass):
  * the existing signature is valid under a test public key you name
  * the signed file inventory lists exactly the files in the archive, and every
    file matches its signed SHA-256
  * the archive holds only plain files and directories with safe names
  * the manifest records a clean, committed source (unless --allow-dirty-source)

Usage:
  python3 scripts/resign-release.py update-1.23.32.zup \
      --test-public-key test.pub --signing-key /secure/prod.key \
      --output final/update-1.23.32.zup
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from updater.crypto import SecurityError, load_private_key, load_public_key, verify_signature  # noqa: E402
from updater.package_trust import verify_and_extract  # noqa: E402


def resign(
    package: Path,
    output: Path,
    *,
    test_public_keys: list[str],
    signing_key: Path,
    max_age_days: int = 30,
    allow_dirty_source: bool = False,
) -> dict:
    """Write ``output`` and return a summary. Raises SecurityError if anything is wrong."""
    if output.exists():
        raise SecurityError(f"{output} already exists; refusing to overwrite a release artifact")

    workdir = Path(tempfile.mkdtemp(prefix="resign-"))
    partial = None
    changed_signatures = []
    try:
        # Full verification: signature, inventory, hashes, archive shape. The
        # unpacked copy is only evidence and is thrown away.
        manifest = verify_and_extract(
            str(package), str(workdir / "unpacked"),
            public_key_paths=test_public_keys, max_age_days=max_age_days,
        )
        source = manifest.get("source") or {}
        if not allow_dirty_source and (source.get("dirty") is not False or not source.get("commit")):
            raise SecurityError(
                "manifest does not record a clean, committed source; refusing to sign it for production")

        unpacked = workdir / "unpacked"
        original_manifest_bytes = (unpacked / "manifest.json").read_bytes()
        manifest_bytes = original_manifest_bytes
        private_key = load_private_key(str(signing_key))
        verification_keys = [load_public_key(path) for path in test_public_keys]
        verification_keys.append(private_key.public_key())
        # Sensor self-updates verify this separate signature using production
        # trust. Changing only the outer OTA signature would strand that path.
        for sensor_manifest in sorted(unpacked.glob("sensor-artifacts/**/manifest.json")):
            sensor_data = sensor_manifest.read_bytes()
            sensor_signature = sensor_manifest.with_name("manifest.json.sig")
            if not sensor_signature.is_file() or not any(
                verify_signature(sensor_data, sensor_signature.read_bytes(), key)
                for key in verification_keys
            ):
                raise SecurityError(f"Invalid bundled sensor signature: {sensor_manifest.relative_to(unpacked)}")
            try:
                metadata = json.loads(sensor_data)
            except (ValueError, TypeError) as exc:
                raise SecurityError("Malformed bundled sensor manifest") from exc
            binary = sensor_manifest.parent / "zenplus-sensor"
            if (not binary.is_file() or not isinstance(metadata, dict)
                    or metadata.get("binary") != binary.name
                    or metadata.get("sha256") != hashlib.sha256(binary.read_bytes()).hexdigest()):
                raise SecurityError("Bundled sensor manifest does not match its binary")
            new_sensor_signature = private_key.sign(sensor_data)
            relative = sensor_signature.relative_to(unpacked).as_posix()
            if sensor_signature.read_bytes() != new_sensor_signature:
                sensor_signature.write_bytes(new_sensor_signature)
                manifest["payload_files"][relative] = hashlib.sha256(new_sensor_signature).hexdigest()
                changed_signatures.append(relative)
        if changed_signatures:
            manifest_bytes = json.dumps(manifest, indent=2).encode()
            (unpacked / "manifest.json").write_bytes(manifest_bytes)
            checksum_file = unpacked / "checksums.sha256"
            if checksum_file.exists():
                checksum_file.write_text("".join(
                    f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(unpacked).as_posix()}\n"
                    for path in sorted(unpacked.rglob("*"))
                    if path.is_file() and path.relative_to(unpacked).as_posix()
                    not in ("checksums.sha256", "manifest.json.sig")
                ))
        signature = private_key.sign(manifest_bytes)
        if not verify_signature(manifest_bytes, signature, private_key.public_key()):
            raise SecurityError("new signature does not verify; refusing to write the package")

        output.parent.mkdir(parents=True, exist_ok=True)
        (unpacked / "manifest.json.sig").write_bytes(signature)
        # Package the verified private snapshot, never re-read an input archive
        # that could have changed after verification. Use an exclusive temp file.
        fd, partial_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".partial", dir=output.parent)
        partial = Path(partial_name)
        def public_mode(member):
            member.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
            member.uid = member.gid = 0
            member.uname = member.gname = "root"
            return member
        with os.fdopen(fd, "wb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as dst:
            for path in sorted(unpacked.rglob("*")):
                dst.add(path, arcname=path.relative_to(unpacked).as_posix(), recursive=False, filter=public_mode)
        os.link(partial, output)  # Atomic publication; refuses a raced existing output.
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)
        shutil.rmtree(workdir, ignore_errors=True)

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {
        "version": manifest.get("version"),
        "source_commit": source.get("commit"),
        "signed_inventory_files": len(manifest.get("payload_files") or {}),
        "output": str(output),
        "output_sha256": digest,
        "nested_signatures_changed": changed_signatures,
        "manifest_unchanged": manifest_bytes == original_manifest_bytes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("package", type=Path, help="the lab-tested .zup (signed with the test key)")
    parser.add_argument("--test-public-key", action="append", required=True,
                        help="public key the lab package is signed with; repeatable")
    parser.add_argument("--signing-key", type=Path, required=True, help="production private key")
    parser.add_argument("--output", type=Path, required=True, help="where to write the production package")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--allow-dirty-source", action="store_true",
                        help="Lab use only: sign a package built from uncommitted source")
    args = parser.parse_args()
    try:
        summary = resign(
            args.package, args.output,
            test_public_keys=args.test_public_key, signing_key=args.signing_key,
            max_age_days=args.max_age_days, allow_dirty_source=args.allow_dirty_source,
        )
    except SecurityError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
