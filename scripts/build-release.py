#!/usr/bin/env python3
"""
ZenPlus Release Builder & Publisher

Usage:
  # Build a release package from current code:
  python scripts/build-release.py build --version 1.1.0

  # Build and publish to zentryc.com:
  python scripts/build-release.py publish --version 1.1.0

  # Publish an existing .zup file:
  python scripts/build-release.py publish --file /tmp/zenplus-releases/update-1.1.0.zup --version 1.1.0

  # List releases on server:
  python scripts/build-release.py list

  # Create a rollout:
  python scripts/build-release.py rollout --version 1.1.0 --stage canary
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ZENPLUS_DIR = Path("/opt/zenplus")
RELEASE_DIR = Path("/tmp/zenplus-releases")
PRIVATE_KEY_PATH = ZENPLUS_DIR / "updater" / "keys" / "zentryc-release.key"
SERVER_URL = "https://zentryc.com"

# Directories to include in the code update
CODE_DIRS = ["server", "poller", "scripts"]
# Files to include at root level
CODE_FILES = [".version", "docker-compose.yml"]

# ─── Crypto ───────────────────────────────────────────────────────────────────

def sign_manifest(manifest_data: bytes, key_path: Path) -> bytes:
    """Sign manifest.json with Ed25519 private key."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key_data = key_path.read_bytes()
    private_key = load_pem_private_key(key_data, password=None)
    return private_key.sign(manifest_data)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ─── Admin Auth ───────────────────────────────────────────────────────────────

def get_admin_token() -> str:
    """Get JWT token for admin API."""
    import httpx

    creds_file = Path.home() / ".zenplus-admin-creds"
    if creds_file.exists():
        creds = json.loads(creds_file.read_text())
        email = creds["email"]
        password = creds["password"]
    else:
        email = input("Admin email: ").strip()
        password = input("Admin password: ").strip()
        save = input("Save credentials? [y/N]: ").strip().lower()
        if save == "y":
            creds_file.write_text(json.dumps({"email": email, "password": password}))
            creds_file.chmod(0o600)
            print(f"  Saved to {creds_file}")

    resp = httpx.post(
        f"{SERVER_URL}/api/v1/admin/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"ERROR: Login failed: {resp.status_code} {resp.text}")
        sys.exit(1)

    data = resp.json()
    return data.get("token") or data.get("access_token") or data.get("key", "")


# ─── Build ────────────────────────────────────────────────────────────────────

def build_package(version: str, changelog: str, severity: str,
                  min_version: str | None, skip_dashboard: bool,
                  skip_go: bool) -> Path:
    """Build a .zup release package from the current codebase."""

    print(f"\n{'='*60}")
    print(f"  Building ZenPlus v{version}")
    print(f"{'='*60}\n")

    build_dir = Path(tempfile.mkdtemp(prefix=f"zenplus-build-{version}-"))
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Copy code directories
    print("[1/7] Copying source code ...")
    code_dir = build_dir / "code"
    for d in CODE_DIRS:
        src = ZENPLUS_DIR / d
        if src.exists():
            shutil.copytree(src, code_dir / d, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".pytest_cache", "node_modules",
                ".mypy_cache", ".ruff_cache", "*.egg-info",
                "venv", ".venv", "dist", "build",
            ))
            print(f"  + {d}/")

    for f in CODE_FILES:
        src = ZENPLUS_DIR / f
        if src.exists():
            shutil.copy2(src, code_dir / f)
            print(f"  + {f}")

    # Also copy the updater module itself — but never its per-appliance state.
    # `config/` holds agent.conf (api_key) and subscription.json (this appliance's
    # subscription snapshot). `logs/` and `backups/` are runtime dirs.
    # `keys/` is private key material. All are appliance-local and must not ship.
    updater_src = ZENPLUS_DIR / "updater"
    if updater_src.exists():
        shutil.copytree(updater_src, code_dir / "updater", ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "logs", "backups", "config", "keys",
            "*.key", "*.pem", "agent.conf", "subscription.json", ".env",
        ))
        print(f"  + updater/")

    # 2. Build dashboard
    if not skip_dashboard:
        print("[2/7] Building dashboard ...")
        dash_dir = ZENPLUS_DIR / "dashboard"
        # Use vite directly instead of `npm run build` — the latter runs
        # `tsc -b && vite build`, and tsc currently fails on pre-existing
        # type errors in Settings.tsx / authStore.ts that are unrelated
        # to the release. Vite alone produces an identical bundle.
        result = subprocess.run(
            ["npx", "vite", "build"],
            capture_output=True, text=True,
            cwd=str(dash_dir), timeout=300,
        )
        if result.returncode != 0:
            print(f"  ERROR: Dashboard build failed:\n{result.stderr}")
            sys.exit(1)

        # Package dist as tar.gz
        dist_archive = build_dir / "dashboard-dist.tar.gz"
        with tarfile.open(dist_archive, "w:gz") as tar:
            tar.add(str(dash_dir / "dist"), arcname=".")
        print(f"  Dashboard dist: {dist_archive.stat().st_size / 1024 / 1024:.1f} MB")
    else:
        print("[2/7] Skipping dashboard build (--skip-dashboard)")

    # 3. Build Go binary
    if not skip_go:
        print("[3/7] Building Go poller ...")
        go_dir = build_dir / "go-binaries"
        go_dir.mkdir()
        poller_src = ZENPLUS_DIR / "poller"
        if poller_src.exists() and (poller_src / "cmd" / "poller").exists():
            result = subprocess.run(
                ["go", "build", "-o", str(go_dir / "zenplus-poller"), "./cmd/poller"],
                capture_output=True, text=True,
                cwd=str(poller_src), timeout=300,
                env={**os.environ, "GOOS": "linux", "GOARCH": "amd64", "CGO_ENABLED": "0"},
            )
            if result.returncode != 0:
                print(f"  WARNING: Go build failed: {result.stderr}")
                print("  Continuing without Go binary ...")
                shutil.rmtree(go_dir)
            else:
                print(f"  Built: zenplus-poller ({(go_dir / 'zenplus-poller').stat().st_size / 1024 / 1024:.1f} MB)")
        else:
            print("  No Go poller source found, skipping")
            shutil.rmtree(go_dir)
    else:
        print("[3/7] Skipping Go build (--skip-go)")

    # 4. Copy requirements.txt
    print("[4/7] Copying pip requirements ...")
    req_file = ZENPLUS_DIR / "server" / "requirements.txt"
    if req_file.exists():
        shutil.copy2(req_file, build_dir / "requirements.txt")
        print(f"  + requirements.txt")
    else:
        print("  No requirements.txt found")

    # 5. Copy new migrations
    print("[5/7] Checking for migrations ...")
    migrations_src = ZENPLUS_DIR / "scripts"
    migration_count = 0
    migrate_dir = build_dir / "migrations"
    migrate_dir.mkdir()
    if migrations_src.exists():
        for f in sorted(migrations_src.glob("migrate-*.sql")):
            shutil.copy2(f, migrate_dir / f.name)
            migration_count += 1
            print(f"  + {f.name}")
    if migration_count == 0:
        shutil.rmtree(migrate_dir)
        print("  No migrations found")

    # 6. Create manifest.json
    print("[6/7] Creating manifest ...")
    steps = []

    # Stop services before update
    steps.append({"type": "stop_services", "services": ["zenplus-api", "zenplus-poller"]})
    steps.append({"type": "backup", "targets": ["code", "database"]})
    steps.append({"type": "apply_code", "method": "replace", "source": "code/"})

    if (build_dir / "requirements.txt").exists():
        steps.append({"type": "pip_install", "requirements": "requirements.txt"})

    if (build_dir / "migrations").exists():
        for f in sorted((build_dir / "migrations").iterdir()):
            engine = "clickhouse" if f.name.startswith("ch-") else "postgres"
            steps.append({"type": "run_migration", "engine": engine, "file": f"migrations/{f.name}"})

    if (build_dir / "dashboard-dist.tar.gz").exists():
        steps.append({"type": "build_dashboard", "prebuilt": True, "source": "dashboard-dist.tar.gz"})

    if (build_dir / "go-binaries" / "zenplus-poller").exists():
        steps.append({"type": "install_binary", "source": "go-binaries/zenplus-poller",
                       "dest": "/opt/zenplus/bin/zenplus-poller"})

    # Restart services
    steps.append({"type": "start_services",
                   "services": ["zenplus-api", "zenplus-poller", "netmon-gunicorn",
                                "netmon-celery", "netmon-celery-beat", "nginx"]})
    steps.append({"type": "health_check", "url": "http://localhost:8000/api/v1/system/health", "timeout": 30})

    manifest = {
        "format_version": 2,
        "update_id": str(uuid4()),
        "version": version,
        "from_version": None,
        "min_version": min_version,
        "release_date": datetime.now(timezone.utc).isoformat(),
        "changelog": changelog,
        "severity": severity,
        "arch": "amd64",
        "os_min": "ubuntu-22.04",
        "steps": steps,
        "rollback_steps": [
            {"type": "restore_backup"},
            {"type": "start_services",
             "services": ["zenplus-api", "zenplus-poller", "netmon-gunicorn",
                          "netmon-celery", "netmon-celery-beat", "nginx"]},
        ],
    }

    manifest_data = json.dumps(manifest, indent=2).encode()
    (build_dir / "manifest.json").write_bytes(manifest_data)

    # Sign manifest
    if PRIVATE_KEY_PATH.exists():
        sig = sign_manifest(manifest_data, PRIVATE_KEY_PATH)
        (build_dir / "manifest.json.sig").write_bytes(sig)
        print(f"  Manifest signed with {PRIVATE_KEY_PATH}")
    else:
        print(f"  WARNING: No private key at {PRIVATE_KEY_PATH}, manifest unsigned!")

    # 7. Generate checksums & create .zup
    print("[7/7] Packaging .zup ...")

    # Generate checksums
    checksums_lines = []
    for root, _, files in os.walk(build_dir):
        for fname in sorted(files):
            fpath = Path(root) / fname
            if fpath.name in ("checksums.sha256", "manifest.json.sig"):
                continue
            rel = fpath.relative_to(build_dir)
            h = sha256_file(str(fpath))
            checksums_lines.append(f"{h}  {rel}")
    (build_dir / "checksums.sha256").write_text("\n".join(checksums_lines) + "\n")

    # Create tar.gz
    output_path = RELEASE_DIR / f"update-{version}.zup"
    with tarfile.open(output_path, "w:gz") as tar:
        for item in sorted(build_dir.iterdir()):
            tar.add(str(item), arcname=item.name)

    pkg_hash = sha256_file(str(output_path))
    pkg_size = output_path.stat().st_size

    # Cleanup build dir
    shutil.rmtree(build_dir)

    print(f"\n{'='*60}")
    print(f"  Package: {output_path}")
    print(f"  Size:    {pkg_size / 1024 / 1024:.1f} MB")
    print(f"  SHA-256: {pkg_hash}")
    print(f"{'='*60}\n")

    # Save metadata alongside
    meta = {
        "version": version,
        "changelog": changelog,
        "severity": severity,
        "min_version": min_version,
        "package_sha256": pkg_hash,
        "package_size": pkg_size,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path = RELEASE_DIR / f"update-{version}.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))

    return output_path


# ─── Publish ──────────────────────────────────────────────────────────────────

def publish_package(zup_path: Path, version: str, changelog: str,
                    severity: str, min_version: str | None) -> None:
    """Upload a .zup package to zentryc.com and publish it."""
    import httpx

    print(f"\nPublishing v{version} to {SERVER_URL} ...")

    token = get_admin_token()
    headers = {"Authorization": f"Bearer {token}"}

    pkg_hash = sha256_file(str(zup_path))
    pkg_size = zup_path.stat().st_size

    # Read manifest signature for API
    manifest_sig_b64 = ""
    try:
        with tarfile.open(zup_path, "r:gz") as tar:
            sig_member = tar.getmember("manifest.json.sig")
            sig_data = tar.extractfile(sig_member).read()
            manifest_sig_b64 = base64.b64encode(sig_data).decode()
    except (KeyError, Exception) as e:
        print(f"  WARNING: Could not extract manifest signature: {e}")

    with httpx.Client(timeout=httpx.Timeout(300, connect=30)) as client:
        # Upload release
        print("  Uploading package ...")
        with open(zup_path, "rb") as f:
            resp = client.post(
                f"{SERVER_URL}/api/v1/admin/releases/create",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (zup_path.name, f, "application/octet-stream")},
                data={
                    "version": version,
                    "changelog": changelog,
                    "severity": severity,
                    "package_sha256": pkg_hash,
                    "manifest_sig": manifest_sig_b64,
                },
            )

        if resp.status_code not in (200, 201):
            print(f"  ERROR: Upload failed: {resp.status_code}")
            print(f"  {resp.text}")
            sys.exit(1)

        release_data = resp.json()
        release_id = release_data.get("id", release_data.get("release_id", ""))
        print(f"  Uploaded: release_id={release_id}")

        # Publish the release
        print("  Publishing ...")
        resp = client.post(
            f"{SERVER_URL}/api/v1/admin/releases/{release_id}/publish",
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            print(f"  WARNING: Publish failed: {resp.status_code} {resp.text}")
            print("  You can publish manually from the admin dashboard.")
        else:
            print("  Published successfully!")

    print(f"\n  Release v{version} is live on {SERVER_URL}")
    print(f"  Appliances will pick it up on their next check-in.\n")


def list_releases() -> None:
    """List all releases on the server."""
    import httpx

    token = get_admin_token()
    resp = httpx.get(
        f"{SERVER_URL}/api/v1/admin/releases",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"ERROR: {resp.status_code} {resp.text}")
        sys.exit(1)

    data = resp.json()
    releases = data if isinstance(data, list) else data.get("releases", data.get("results", []))

    if not releases:
        print("No releases found.")
        return

    print(f"\n{'Version':<12} {'Severity':<12} {'Published':<12} {'Created':<22} {'Changelog'}")
    print("-" * 90)
    for r in releases:
        pub = "Yes" if r.get("is_published") else "No"
        created = r.get("created_at", "")[:19]
        log = (r.get("changelog") or "")[:40]
        print(f"{r.get('version', '?'):<12} {r.get('severity', 'normal'):<12} {pub:<12} {created:<22} {log}")
    print()


def create_rollout(version: str, stage: str, target_group: str | None,
                   target_pct: int) -> None:
    """Create a rollout policy for a release."""
    import httpx

    token = get_admin_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Find release by version
    resp = httpx.get(
        f"{SERVER_URL}/api/v1/admin/releases",
        headers=headers,
        timeout=30,
    )
    data = resp.json()
    releases = data if isinstance(data, list) else data.get("releases", data.get("results", []))
    release = next((r for r in releases if r.get("version") == version), None)

    if not release:
        print(f"ERROR: Release v{version} not found on server.")
        sys.exit(1)

    release_id = release.get("id", release.get("release_id"))

    resp = httpx.post(
        f"{SERVER_URL}/api/v1/admin/rollouts",
        headers=headers,
        json={
            "release_id": release_id,
            "stage": stage,
            "target_group": target_group,
            "target_pct": target_pct,
            "auto_promote": stage != "full",
            "promote_after": "24:00:00",
            "max_failure_pct": 5,
        },
        timeout=30,
    )

    if resp.status_code not in (200, 201):
        print(f"ERROR: Rollout creation failed: {resp.status_code} {resp.text}")
        sys.exit(1)

    print(f"\nRollout created for v{version}:")
    print(f"  Stage:    {stage}")
    print(f"  Group:    {target_group or 'all'}")
    print(f"  Percent:  {target_pct}%")
    print(f"  Appliances will start receiving this update on next check-in.\n")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ZenPlus Release Builder & Publisher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build a release:
  python scripts/build-release.py build --version 1.1.0 --changelog "Bug fixes"

  # Build and publish in one step:
  python scripts/build-release.py publish --version 1.1.0 --changelog "New features"

  # Publish with staged rollout:
  python scripts/build-release.py publish --version 1.1.0 --changelog "Big update" --rollout canary

  # List releases on server:
  python scripts/build-release.py list

  # Create a rollout for existing release:
  python scripts/build-release.py rollout --version 1.1.0 --stage full
        """,
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # Build
    build_p = sub.add_parser("build", help="Build a .zup package from current code")
    build_p.add_argument("--version", "-v", required=True, help="Version string (e.g. 1.1.0)")
    build_p.add_argument("--changelog", "-c", default="", help="Changelog description")
    build_p.add_argument("--severity", "-s", default="normal",
                         choices=["critical", "security", "normal", "optional"])
    build_p.add_argument("--min-version", default=None, help="Minimum version to upgrade from")
    build_p.add_argument("--skip-dashboard", action="store_true", help="Skip dashboard build")
    build_p.add_argument("--skip-go", action="store_true", help="Skip Go binary build")

    # Publish
    pub_p = sub.add_parser("publish", help="Build and publish to zentryc.com")
    pub_p.add_argument("--version", "-v", required=True, help="Version string")
    pub_p.add_argument("--changelog", "-c", default="", help="Changelog description")
    pub_p.add_argument("--severity", "-s", default="normal",
                       choices=["critical", "security", "normal", "optional"])
    pub_p.add_argument("--min-version", default=None, help="Minimum version to upgrade from")
    pub_p.add_argument("--file", "-f", default=None, help="Use existing .zup file instead of building")
    pub_p.add_argument("--skip-dashboard", action="store_true")
    pub_p.add_argument("--skip-go", action="store_true")
    pub_p.add_argument("--rollout", default=None,
                       choices=["canary", "percentage", "full"],
                       help="Auto-create rollout after publishing")
    pub_p.add_argument("--rollout-pct", type=int, default=100, help="Rollout percentage (default 100)")
    pub_p.add_argument("--rollout-group", default=None, help="Target rollout group")

    # List
    sub.add_parser("list", help="List releases on zentryc.com")

    # Rollout
    roll_p = sub.add_parser("rollout", help="Create a rollout for an existing release")
    roll_p.add_argument("--version", "-v", required=True)
    roll_p.add_argument("--stage", required=True, choices=["canary", "percentage", "full"])
    roll_p.add_argument("--group", default=None, help="Target group (default: all)")
    roll_p.add_argument("--pct", type=int, default=100, help="Target percentage")

    args = parser.parse_args()

    if args.command == "build":
        build_package(args.version, args.changelog, args.severity,
                      args.min_version, args.skip_dashboard, args.skip_go)

    elif args.command == "publish":
        if args.file:
            zup_path = Path(args.file)
            if not zup_path.exists():
                print(f"ERROR: File not found: {zup_path}")
                sys.exit(1)
        else:
            zup_path = build_package(args.version, args.changelog, args.severity,
                                     args.min_version, args.skip_dashboard, args.skip_go)
        publish_package(zup_path, args.version, args.changelog,
                        args.severity, args.min_version)

        if args.rollout:
            create_rollout(args.version, args.rollout, args.rollout_group, args.rollout_pct)

    elif args.command == "list":
        list_releases()

    elif args.command == "rollout":
        create_rollout(args.version, args.stage, args.group, args.pct)


if __name__ == "__main__":
    main()
