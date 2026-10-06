#!/usr/bin/env python3
"""Re-sign a lab-tested OTA package with the production key.

The package that was tested on lab appliances carries a manifest signed with the
*test* key, which customer appliances do not trust. This tool checks that package
completely, then writes a copy whose ``manifest.json`` is byte-for-byte unchanged
and whose ``manifest.json.sig`` is the production signature. What customers install
is therefore exactly what was tested, and the production key never has to exist on
the development machine.

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
import io
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from updater.crypto import SecurityError, load_private_key, verify_signature  # noqa: E402
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

        manifest_bytes = (workdir / "unpacked" / "manifest.json").read_bytes()
        private_key = load_private_key(str(signing_key))
        signature = private_key.sign(manifest_bytes)
        if not verify_signature(manifest_bytes, signature, private_key.public_key()):
            raise SecurityError("new signature does not verify; refusing to write the package")

        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + ".partial")
        with tarfile.open(package, "r:gz") as src, tarfile.open(partial, "w:gz") as dst:
            for member in src.getmembers():
                if member.name == "manifest.json.sig":
                    member.size = len(signature)
                    dst.addfile(member, io.BytesIO(signature))
                elif member.isdir():
                    dst.addfile(member)
                else:
                    dst.addfile(member, src.extractfile(member))
        partial.rename(output)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {
        "version": manifest.get("version"),
        "source_commit": source.get("commit"),
        "signed_inventory_files": len(manifest.get("payload_files") or {}),
        "output": str(output),
        "output_sha256": digest,
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
