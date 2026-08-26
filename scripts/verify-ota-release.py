"""Verify a ZenPlus OTA archive before public publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import PurePosixPath

from cryptography.hazmat.primitives.serialization import load_pem_public_key


def read_member(tar: tarfile.TarFile, name: str) -> bytes:
    member = tar.getmember(name)
    handle = tar.extractfile(member)
    if handle is None:
        raise RuntimeError(f"archive member is not a regular file: {name}")
    return handle.read()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("package")
    parser.add_argument("public_key")
    parser.add_argument("--version", required=True)
    payload = parser.add_mutually_exclusive_group(required=True)
    payload.add_argument("--agent-version")
    payload.add_argument(
        "--appliance-only",
        action="store_true",
        help="Require a signed release that deliberately contains no agent installer",
    )
    parser.add_argument(
        "--required-source-archive",
        help="tar.gz of authoritative changed source files that must match code/ byte-for-byte",
    )
    args = parser.parse_args()

    with tarfile.open(args.package, "r:gz") as tar:
        members = {member.name: member for member in tar.getmembers()}
        for member in members.values():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"unsafe archive path: {member.name}")
            if member.issym() or member.islnk():
                raise RuntimeError(f"archive contains a link: {member.name}")

        manifest_bytes = read_member(tar, "manifest.json")
        signature = read_member(tar, "manifest.json.sig")
        public_key = load_pem_public_key(open(args.public_key, "rb").read())
        public_key.verify(signature, manifest_bytes)
        manifest = json.loads(manifest_bytes)

        if manifest.get("version") != args.version:
            raise RuntimeError("manifest version mismatch")
        installed_version = read_member(tar, "code/.version").decode().splitlines()[0]
        if installed_version != args.version:
            raise RuntimeError("code/.version does not match release version")

        checksum_lines = read_member(tar, "checksums.sha256").decode().splitlines()
        verified = 0
        for line in checksum_lines:
            expected, name = line.split(None, 1)
            name = name.strip()
            actual = hashlib.sha256(read_member(tar, name)).hexdigest()
            if actual != expected:
                raise RuntimeError(f"checksum mismatch: {name}")
            verified += 1

        forbidden = []
        for name in members:
            lowered = name.lower()
            base = PurePosixPath(lowered).name
            if base in {".env", "agent.conf", "subscription.json"} or base.endswith((".key", ".pem")):
                forbidden.append(name)
        if forbidden:
            raise RuntimeError(f"secret-bearing files found: {forbidden}")

        packages = manifest.get("agent_packages") or []
        agent_members = [name for name in members if name.startswith("agent-artifacts/")]
        agent_steps = [
            step for step in manifest.get("steps", [])
            if str(step.get("source", "")).startswith("agent-artifacts/")
        ]
        if args.appliance_only:
            if manifest.get("agent_artifacts_included") is not False:
                raise RuntimeError("manifest does not declare appliance-only release scope")
            if packages or agent_members or agent_steps:
                raise RuntimeError("appliance-only release unexpectedly contains agent artifacts")
            msi_name = None
            msi = None
            msi_hash = None
            expected_dest = None
        else:
            if manifest.get("agent_artifacts_included") is False:
                raise RuntimeError("manifest declares appliance-only scope but an agent was required")
            msi_name = f"zenplus-agent-{args.agent_version}.msi"
            msi_path = f"agent-artifacts/windows/{msi_name}"
            msi = members.get(msi_path)
            if msi is None or msi.size < 100_000_000:
                raise RuntimeError("full offline Windows MSI is missing or truncated")
            package = next(
                (item for item in packages if item.get("file_name") == msi_name),
                None,
            )
            if not package or package.get("version") != args.agent_version:
                raise RuntimeError("agent package metadata is missing or stale")
            msi_hash = hashlib.sha256(read_member(tar, msi_path)).hexdigest()
            if msi_hash != package.get("sha256") or msi.size != package.get("file_size"):
                raise RuntimeError("agent MSI metadata does not match its bytes")
            expected_dest = f"/opt/zenplus/artifacts/agents/windows/{msi_name}"
            install_step = next(
                (
                    step for step in manifest.get("steps", [])
                    if step.get("source") == msi_path and step.get("dest") == expected_dest
                ),
                None,
            )
            if install_step is None:
                raise RuntimeError("agent MSI appliance installation step is missing")

        required_sources = 0
        if args.required_source_archive:
            with tarfile.open(args.required_source_archive, "r:gz") as source_tar:
                for source_member in source_tar.getmembers():
                    if not source_member.isfile():
                        continue
                    source_name = source_member.name.removeprefix("./")
                    packaged_name = f"code/{source_name}"
                    if packaged_name not in members:
                        raise RuntimeError(
                            f"authoritative changed file missing from release: {source_name}"
                        )
                    source_handle = source_tar.extractfile(source_member)
                    if source_handle is None:
                        raise RuntimeError(
                            f"cannot read authoritative changed file: {source_name}"
                        )
                    if source_handle.read() != read_member(tar, packaged_name):
                        raise RuntimeError(
                            f"authoritative changed file differs in release: {source_name}"
                        )
                    required_sources += 1

    print(json.dumps({
        "release_version": args.version,
        "signature": "valid",
        "checksums_verified": verified,
        "archive_members": len(members),
        "release_scope": "appliance-only" if args.appliance_only else "appliance-and-agent",
        "agent_version": args.agent_version,
        "agent_msi": msi_name,
        "agent_msi_size": msi.size if msi is not None else None,
        "agent_msi_sha256": msi_hash,
        "agent_destination": expected_dest,
        "authoritative_changed_files_verified": required_sources,
        "forbidden_files": 0,
    }, indent=2))


if __name__ == "__main__":
    main()
