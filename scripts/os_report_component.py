"""Build and apply the signed OS reporting component for legacy ZenPlus firmware.

Run the tagged management tool with the installed appliance Python using -I -B.
The component version and the appliance firmware version are independent.
"""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PROFILE = "zenplus-os-patch-reporter"
VERSION = "1.0.0"
TAG = f"{PROFILE}-v{VERSION}"
BASE = "1.25.1"
ORIGINAL = "90685c53c0419efea8c091606aa30e06af72de3b251d9bc145a88ceb68fde04e"
UPDATED = "a183f5a789ab89f8da4ca4f64270495cdb52879c3a30f4d114a649a86fcec46c"
COLLECTOR = "9b8138407eece28772705f9ffed2b7af6372bf19c7e72e2956e0c6e654059709"
FILES = {"original-agent.py": ORIGINAL, "agent.py": UPDATED, "os_patch_check.py": COLLECTOR}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def payload(repo):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args])

    original = git("show", f"v{BASE}:updater/agent.py")
    # Apply only the reviewed reporting change to the original legacy agent.
    patch = git("diff", "v1.25.30", "v1.25.31", "--", "updater/agent.py")
    with tempfile.TemporaryDirectory() as tmp:
        agent = Path(tmp, "updater", "agent.py")
        agent.parent.mkdir()
        agent.write_bytes(original)
        subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "--no-index", "-"],
                       cwd=tmp, input=patch, check=True)
        files = {"original-agent.py": original, "agent.py": agent.read_bytes(),
                 "os_patch_check.py": git("show", "v1.25.31:updater/os_patch_check.py")}
    if {k: digest(v) for k, v in files.items()} != FILES:
        raise ValueError("Unexpected legacy reporting payload")
    files["os_report_component.py"] = Path(__file__).read_bytes()
    return files


def validate(manifest):
    source = manifest.get("source") or {}
    if (manifest.get("release_profile") != PROFILE or manifest.get("version") != VERSION
            or manifest.get("supported_firmware") != [BASE] or manifest.get("payload_files") != {
                **FILES, "os_report_component.py": digest(Path(__file__).read_bytes())}
            or source.get("tag") != TAG or source.get("dirty") is not False or not source.get("commit")
            or "steps" in manifest or "rollback_steps" in manifest or "min_version" in manifest):
        raise ValueError("Package is not the qualified reporting component")


def replace(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        tmp = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def sync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def apply(root, extracted, action):
    from updater.trust import require_root_owned
    version = root / ".version"
    directory = root / "updater"
    agent = directory / "agent.py"
    probe = directory / "os_patch_check.py"
    for path in (version, directory, agent):
        require_root_owned(path, what="reporter installation target")
    if version.read_text().splitlines()[0] != BASE:
        raise ValueError("Unqualified appliance firmware version")
    current = digest(agent.read_bytes())
    if current not in (ORIGINAL, UPDATED):
        raise ValueError("Unqualified installed updater")
    if probe.is_symlink() or probe.exists():
        require_root_owned(probe, what="installed OS reporter")
        if digest(probe.read_bytes()) != COLLECTOR:
            raise ValueError("Conflicting installed OS reporter")
    if action == "install":
        if not probe.exists():
            replace(probe, (extracted / probe.name).read_bytes())
        if current != UPDATED:
            replace(agent, (extracted / "agent.py").read_bytes())
    elif action == "restore":
        if current != ORIGINAL:
            replace(agent, (extracted / "original-agent.py").read_bytes())
        probe.unlink(missing_ok=True)
        sync_dir(directory)
    else:
        raise ValueError("Unexpected component operation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "restore", "verify"])
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    root = Path("/opt/zenplus")
    sys.path.insert(0, str(root))
    from updater.config import load_config
    from updater.lockfile import UpdateLock
    from updater.package_trust import verify_and_extract

    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        extracted = Path(tmp) / "verified"
        manifest = verify_and_extract(str(args.package), str(extracted),
                                      public_key_paths=cfg.security.public_key_paths,
                                      revoked_fingerprints=cfg.security.revoked_key_fingerprints,
                                      max_age_days=cfg.security.max_manifest_age_days,
                                      allow_legacy=False, expected_version=VERSION)
        validate(manifest)
        if args.action != "verify":
            with UpdateLock():
                apply(root, extracted, args.action)


if __name__ == "__main__":
    main()
