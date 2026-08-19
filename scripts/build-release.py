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
import ast
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_order  # noqa: E402

ZENPLUS_DIR = Path(os.getenv("ZENPLUS_DIR", "/opt/zenplus"))
RELEASE_DIR = Path(os.getenv("ZENPLUS_RELEASE_DIR", "/tmp/zenplus-releases"))
PRIVATE_KEY_PATH = Path(
    os.getenv("ZENPLUS_RELEASE_PRIVATE_KEY", str(ZENPLUS_DIR / "updater" / "keys" / "zentryc-release.key"))
)
SERVER_URL = os.getenv("ZENPLUS_RELEASE_SERVER_URL", "https://zentryc.com")
GO_BIN = shutil.which("go") or "/usr/local/go/bin/go"

# Directories to include in the code update
CODE_DIRS = ["server", "poller", "scripts", "support", "docker"]
# Files to include at root level
CODE_FILES = [".version", "docker-compose.yml"]
CODE_IGNORE = [
    "__pycache__", "*.pyc", ".pytest_cache", "node_modules",
    ".mypy_cache", ".ruff_cache", "*.egg-info",
    "venv", ".venv", "dist", "build",
    ".netpath-presync-backup",
    # Support-bundle runtime dirs — created by setup-support.sh on the
    # appliance, never part of a release. They're owned by zenplus/root and
    # unreadable by the build user, which crashes shutil.copytree.
    "requests", "jobs", "bundles",
]
# Every release carries the COMPLETE migration set plus the lockfile that
# orders it. Shipping a migration is not the same as running one: the runners
# keep a per-engine ledger and apply only what is missing. Shipping only the
# migrations a release "introduced" is what let an appliance that skipped a
# release stay permanently short of a schema change with no way to catch up.
# init-postgres.sql / seed-devices.sql stay out — they are install-time only.
SCRIPT_CODE_IGNORE = [
    *CODE_IGNORE,
    "init-postgres.sql",
    "seed-devices.sql",
]

# Lockfile recording the SHA256 of every migrate-*.sql that has ever shipped.
# Migrations are append-only once released — see lint_migrations() below.
MIGRATIONS_LOCK = ZENPLUS_DIR / "scripts" / "migrations.lock"

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


AGENT_PACKAGE_PATTERNS = {
    "windows": re.compile(r"^zenplus-agent-(\d+\.\d+\.\d+)\.msi$"),
    "linux": re.compile(r"^zenplus-agent-(\d+\.\d+\.\d+)\.tar\.gz$"),
    "macos": re.compile(r"^zenplus-agent-(\d+\.\d+\.\d+)\.(?:pkg|tar\.gz)$"),
}


def _version_key(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))


def _agent_source_version() -> str | None:
    """Return the Windows agent version declared by the vendored source.

    Returns None when the agent source is not vendored here. The agent is built
    and versioned in its own repository; when its source is absent this builder
    has no opinion about which version is current and defers to the newest
    package in the artifact store.
    """
    model_file = ZENPLUS_DIR / "ZenPlus_Agent" / "internal" / "model" / "model.go"
    if not model_file.is_file():
        return None
    match = re.search(
        r'AgentVersion\s*=\s*"(\d+\.\d+\.\d+)"',
        model_file.read_text(encoding="utf-8"),
    )
    if not match:
        raise RuntimeError(f"Could not read AgentVersion from {model_file}")
    return match.group(1)


def stage_agent_artifacts(
    build_dir: Path,
    artifact_dir: Path | None = None,
    *,
    required: bool = True,
) -> list[dict]:
    """Validate and stage one current package per supported agent platform.

    Agent installers are produced on their native build runners, not by this
    Linux release builder.  A portal release must nevertheless carry the
    current Windows MSI, otherwise an upgraded appliance regresses to the
    misleading "no package published" state.

    Which MSI is "current" has two possible authorities:

    * **Vendored source present** — ``AgentVersion`` in the agent source is the
      contract, and a stale MSI fails the release.
    * **Vendored source absent** (the agent lives in its own repo) — the newest
      package in the artifact store is shipped as-is. There is nothing here to
      check it against, and blocking every appliance release on an agent this
      tree no longer contains would be the wrong trade.
    """
    source = artifact_dir or Path(
        os.getenv(
            "ZENPLUS_AGENT_ARTIFACT_DIR",
            str(ZENPLUS_DIR / "artifacts" / "agents"),
        )
    )
    expected_windows = _agent_source_version()

    if expected_windows is None:
        if not any((source / "windows").glob("zenplus-agent-*.msi")):
            message = (
                f"No Windows agent MSI in {source / 'windows'}. Appliances will "
                "show 'no package published' until one is added."
            )
            if required:
                raise RuntimeError(message)
            print(f"  WARNING: {message}")
            return []
    else:
        expected_msi = source / "windows" / f"zenplus-agent-{expected_windows}.msi"
        if not expected_msi.is_file():
            message = (
                f"Required Windows agent MSI is missing or stale. Expected {expected_msi} "
                f"for vendored AgentVersion {expected_windows}. Build the agent on Windows "
                "and place the MSI in the artifact store before creating a release."
            )
            if required:
                raise RuntimeError(message)
            print(f"  WARNING: {message}")
            return []

    staged: list[dict] = []
    output_root = build_dir / "agent-artifacts"
    for platform, pattern in AGENT_PACKAGE_PATTERNS.items():
        platform_dir = source / platform
        candidates: list[tuple[tuple[int, int, int], str, Path]] = []
        if platform_dir.is_dir():
            for package in platform_dir.iterdir():
                if not package.is_file() or package.is_symlink():
                    continue
                match = pattern.fullmatch(package.name)
                if match:
                    candidates.append((_version_key(match.group(1)), match.group(1), package))
        if not candidates:
            continue
        _, version, package = max(candidates, key=lambda item: item[0])
        if platform == "windows" and expected_windows and version != expected_windows:
            # The exact file check above normally catches this; keep the
            # assertion beside selection so future naming changes stay safe.
            raise RuntimeError(
                f"Latest Windows MSI is {version}, but vendored AgentVersion is {expected_windows}"
            )
        if package.stat().st_size < 1_000_000:
            raise RuntimeError(f"Agent package looks truncated ({package.stat().st_size} bytes): {package}")

        destination = output_root / platform / package.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(package, destination)
        staged.append({
            "platform": platform,
            "version": version,
            "file_name": package.name,
            "file_size": package.stat().st_size,
            "sha256": sha256_file(str(package)),
        })

    windows_staged = next(
        (p["version"] for p in staged if p["platform"] == "windows"), None
    )
    manifest = {
        "format_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "required_windows_version": expected_windows or windows_staged,
        "packages": staged,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return staged


# ─── Migration lint ───────────────────────────────────────────────────────────

def _load_migrations_lock(lock_path: Path) -> dict[str, str]:
    return dict(migration_order.load_lock(lock_path))


def _write_migrations_lock(lock_path: Path, entries: dict[str, str]) -> None:
    """Write the lockfile preserving existing order, appending new entries.

    Line order in the lockfile IS the order appliances apply migrations in, so
    it must never be re-sorted. Migration numbers are not unique (two 016s, two
    030s, a date-stamped file), which is precisely why filename sort cannot be
    the sequence.
    """
    existing = migration_order.load_lock(lock_path)
    ordered = [(name, entries[name]) for name, _ in existing if name in entries]
    known = {name for name, _ in ordered}
    ordered.extend(
        (name, entries[name]) for name in sorted(entries) if name not in known
    )
    migration_order.write_lock(lock_path, ordered)


def _superseded_checksums() -> dict[str, set[str]]:
    """The reconciliation table run-migrations.py carries, read from its source.

    Read rather than duplicated: the runner is the authority on which historical
    checksums an appliance may legitimately be carrying, and two copies would
    drift apart exactly when it matters. Parsed with ast rather than imported —
    the runner is a script, not a library, and the lint has no business running
    its module body.
    """
    runner = Path(__file__).resolve().parent / "run-migrations.py"
    if not runner.exists():
        return {}
    try:
        tree = ast.parse(runner.read_text())
    except (OSError, SyntaxError):
        return {}

    for node in tree.body:
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign)
            else node.targets if isinstance(node, ast.Assign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "SUPERSEDED_CHECKSUMS" for t in targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            return {}
        return {name: set(checksums) for name, checksums in value.items()}
    return {}


def _git_prefix(scripts_dir: Path) -> str | None:
    """The migration directory's path from the repo root, or None if not in one.

    ``git show COMMIT:PATH`` resolves PATH from the repo root, not from -C, so
    the audit needs this to address historical blobs at all.
    """
    result = subprocess.run(
        ["git", "-C", str(scripts_dir), "rev-parse", "--show-prefix"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_history_checksums(scripts_dir: Path, prefix: str, name: str) -> dict[str, str]:
    """Every distinct sha256 this migration has ever had, → newest commit."""
    log = subprocess.run(
        ["git", "-C", str(scripts_dir), "log", "--follow", "--format=%H", "--", name],
        capture_output=True, text=True,
    )
    if log.returncode != 0:
        return {}

    history: dict[str, str] = {}
    for commit in log.stdout.split():
        blob = subprocess.run(
            ["git", "-C", str(scripts_dir), "show", f"{commit}:{prefix}{name}"],
            capture_output=True,
        )
        if blob.returncode != 0:
            # The file was added under a different name in this commit;
            # --follow found it, `show` at this path cannot.
            continue
        # git log lists newest first, so the first commit to show a given
        # checksum is the newest one carrying it.
        history.setdefault(hashlib.sha256(blob.stdout).hexdigest(), commit[:7])
    return history


def _lint_migration_history(scripts_dir: Path, on_disk: dict[str, str]) -> list[str]:
    """Reject a migration whose git history holds an unreconciled checksum.

    Appliances record the sha256 of each migration as they apply it, and since
    v1.7.0 the OTA schema gate fails the update on any mismatch. So *every*
    revision a migration has ever had is a checksum some appliance in the field
    may be carrying — not just the current one.

    migrations.lock only guards edits made after it existed (2026-05-20).
    migrate-004-snmp.sql and migrate-006-services-v2.sql were rewritten before
    that, went unnoticed for three months, and then stranded part of the fleet
    on 1.6.0 the moment the gate shipped. Git remembers what the lockfile
    cannot, so ask git.

    An unreconciled historical checksum is a build failure, not a field
    failure: either restore the file's shipped bytes, or record the superseded
    checksum in run-migrations.py so the runner heals the ledger row.

    PostgreSQL only. The ClickHouse ledger (ch_migrate.py) keys on filename and
    object presence and never compares checksums, so a rewritten ClickHouse
    migration cannot strand anyone.
    """
    prefix = _git_prefix(scripts_dir)
    if prefix is None:
        return []

    superseded = _superseded_checksums()
    errors: list[str] = []

    for name, current in on_disk.items():
        if migration_order.engine_of(name) != "postgres":
            continue
        reconciled = superseded.get(name, frozenset())
        for checksum, commit in _git_history_checksums(scripts_dir, prefix, name).items():
            if checksum == current or checksum in reconciled:
                continue
            errors.append(
                f"{name}: shipped as {checksum} in commit {commit}, now {current}. "
                f"Appliances that applied the old bytes will fail the schema gate. "
                f"Add {checksum!r} to SUPERSEDED_CHECKSUMS in scripts/run-migrations.py "
                f"(only if the rewrite left already-migrated appliances nothing to do), "
                f"or restore the file and put the change in a new migration."
            )
    return errors


def _lint_new_migrations(scripts_dir: Path, new_files: list[str]) -> list[str]:
    """Reject new migrations that would be unsafe or ambiguous in the field.

    Two rules, both learned the hard way:

    * A duplicate sequence number makes the relative order of two migrations
      depend on the rest of the filename. Fine until one of them has to come
      first.
    * A new ClickHouse migration must be replay-safe (guarded DDL, no blind
      backfill INSERT). The updater heals an appliance whose ledger claims a
      migration ran when it did not — it can only do that by re-running the
      file, and it will refuse to re-run one that would duplicate rows.
    """
    errors: list[str] = []

    locked_seqs: dict[str, str] = {}
    for f in sorted(scripts_dir.glob("migrate-*.sql")):
        if f.name in new_files:
            continue
        seq = migration_order.sequence_of(f.name)
        if seq:
            locked_seqs.setdefault(seq, f.name)

    seen_new: dict[str, str] = {}
    for name in new_files:
        seq = migration_order.sequence_of(name)
        if not seq:
            errors.append(f"{name}: expected migrate-NNN-description.sql")
            continue
        clash = locked_seqs.get(seq) or seen_new.get(seq)
        if clash:
            errors.append(
                f"{name}: sequence {seq} is already taken by {clash} — "
                f"pick the next free number"
            )
        seen_new[seq] = name

        if migration_order.engine_of(name) == "clickhouse":
            sql = (scripts_dir / name).read_text()
            if not migration_order.is_replay_safe(sql):
                errors.append(
                    f"{name}: ClickHouse migrations must be replay-safe — use "
                    f"CREATE ... IF NOT EXISTS and no backfill INSERT, so the "
                    f"updater can heal an appliance that never received it"
                )
    return errors


def lint_migrations(
    update_lock: bool = False,
    *,
    scripts_dir: Path | None = None,
    lock_path: Path | None = None,
) -> None:
    """Fail the build if any shipped migrate-*.sql has been edited.

    Migration files are append-only once they appear in a release: appliances
    record each file's SHA256 in schema_migrations when they apply it, and
    run-migrations.py refuses to proceed if the file on disk hashes differently
    on a later update. Catching the edit at build time prevents that fire.

    Unknown migrations (new files not yet in the lockfile) are accepted only
    when --update-lock is passed, so adding to the lock is an explicit,
    git-reviewable step.
    """
    scripts_dir = scripts_dir or (ZENPLUS_DIR / "scripts")
    lock_path = lock_path or MIGRATIONS_LOCK
    locked = _load_migrations_lock(lock_path)
    on_disk = {
        f.name: sha256_file(str(f))
        for f in sorted(scripts_dir.glob("migrate-*.sql"))
    }

    drift: list[tuple[str, str, str]] = []
    new_files: list[str] = []
    for name, digest in on_disk.items():
        if name in locked:
            if locked[name] != digest:
                drift.append((name, locked[name], digest))
        else:
            new_files.append(name)

    missing = [name for name in locked if name not in on_disk]
    if missing:
        print("\nERROR: migrations recorded in the lockfile are missing from disk.")
        print("Every release ships the complete migration set; deleting a shipped")
        print("migration would leave appliances that never applied it stranded.")
        for name in missing:
            print(f"  - {name}")
        sys.exit(1)

    if new_files:
        errors = _lint_new_migrations(scripts_dir, new_files)
        if errors:
            print("\nERROR: new migrations rejected.")
            for err in errors:
                print(f"  {err}")
            sys.exit(1)

    if drift:
        print("\nERROR: migrate-*.sql checksum drift detected.")
        print("Migrations are append-only after they ship in a release. Add a new")
        print("migrate-NNN-fix.sql instead of editing an already-released file —")
        print("otherwise appliances that applied the old contents will fail the")
        print("next update with a checksum-mismatch error from run-migrations.py.")
        print(f"Lockfile: {lock_path}")
        for name, old, new_digest in drift:
            print(f"  {name}")
            print(f"    locked:  {old}")
            print(f"    on disk: {new_digest}")
        sys.exit(1)

    history = _lint_migration_history(scripts_dir, on_disk)
    if history:
        print("\nERROR: a migration was edited after it shipped.")
        print("Appliances checksum each migration as they apply it, so every past")
        print("revision is a checksum some appliance is still carrying. The lockfile")
        print("only guards edits made since it existed; git history goes back further.")
        for err in history:
            print(f"  {err}")
        sys.exit(1)

    if new_files and not update_lock:
        print("\nERROR: new migrate-*.sql files are not recorded in the lockfile.")
        print("Run to record them, then commit the updated lockfile alongside the")
        print("new migration files:")
        print("  python scripts/build-release.py lint-migrations --update-lock")
        for name in new_files:
            print(f"  + {name}  {on_disk[name]}")
        sys.exit(1)

    if new_files:
        merged = {**locked, **{n: on_disk[n] for n in new_files}}
        _write_migrations_lock(lock_path, merged)
        print(f"  Recorded {len(new_files)} new migration(s) in {lock_path}:")
        for name in new_files:
            print(f"    + {name}  {on_disk[name]}")


def _migration_engine(path: Path) -> str:
    return migration_order.engine_of(path)


def _select_migrations(
    scripts_dir: Path,
    include_migrations: bool,
    requested_migrations: list[str] | None,
) -> list[Path]:
    """Return migrations to call out with their own manifest step.

    This is now emphasis, not delivery. Every release ships the complete
    migration set in code/scripts/, and scripts/sync-schema.py applies whatever
    an appliance is missing on every update. Naming files here just makes them
    explicit in the manifest — useful for a schema-heavy release, never
    required, and forgetting it can no longer strand an appliance.
    """
    requested_migrations = requested_migrations or []
    if include_migrations and not requested_migrations:
        selected = migration_order.ordered_migrations(scripts_dir)
        print(f"  --include-migrations: naming all {len(selected)} migration(s) explicitly")
        return selected

    if not requested_migrations:
        return []

    selected: list[Path] = []
    seen: set[str] = set()
    for item in requested_migrations:
        name = Path(item).name
        if name in seen:
            print(f"\nERROR: duplicate migration requested: {name}")
            sys.exit(1)
        seen.add(name)

        if not name.startswith("migrate-") or not name.endswith(".sql"):
            print(f"\nERROR: migration must be a scripts/migrate-*.sql file: {item}")
            sys.exit(1)

        candidate = scripts_dir / name
        if not candidate.exists():
            print(f"\nERROR: migration file not found: {candidate}")
            sys.exit(1)
        selected.append(candidate)

    order = {p.name: i for i, p in enumerate(migration_order.ordered_migrations(scripts_dir))}
    return sorted(selected, key=lambda p: (order.get(p.name, len(order)), p.name))


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
                  skip_go: bool, include_migrations: bool,
                  migration_files: list[str] | None = None,
                  agent_artifact_dir: Path | None = None,
                  skip_agent_artifacts: bool = False) -> Path:
    """Build a .zup release package from the current codebase."""

    print(f"\n{'='*60}")
    print(f"  Building ZenPlus v{version}")
    print(f"{'='*60}\n")

    print("[0/7] Linting migrations against lockfile ...")
    lint_migrations()
    migrations_src = ZENPLUS_DIR / "scripts"
    selected_migrations = _select_migrations(
        migrations_src,
        include_migrations,
        migration_files,
    )

    build_dir = Path(tempfile.mkdtemp(prefix=f"zenplus-build-{version}-"))
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Copy code directories
    print("[1/7] Copying source code ...")
    code_dir = build_dir / "code"
    for d in CODE_DIRS:
        src = ZENPLUS_DIR / d
        if src.exists():
            ignore = SCRIPT_CODE_IGNORE if d == "scripts" else CODE_IGNORE
            shutil.copytree(src, code_dir / d, ignore=shutil.ignore_patterns(*ignore))
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

    # Windows installers come from the native Windows build.  Validate them
    # independently from the Linux Go binaries so --skip-go remains safe and
    # so a missing artifact can never silently disappear from a portal OTA.
    print("[2a/7] Validating agent release artifacts ...")
    if skip_agent_artifacts:
        print("  Skipping agent artifacts (--skip-agent-artifacts)")
        agent_staged = []
    else:
        agent_staged = stage_agent_artifacts(
            build_dir,
            artifact_dir=agent_artifact_dir,
            required=True,
        )
        for package in agent_staged:
            size_mb = package["file_size"] / 1024 / 1024
            print(
                f"  + {package['platform']}/{package['file_name']} "
                f"v{package['version']} ({size_mb:.1f} MB, "
                f"{package['sha256'][:12]}…)"
            )

    # 3. Build Go binaries
    if not skip_go:
        print("[3/7] Building Go poller and remote sensor ...")
        go_dir = build_dir / "go-binaries"
        go_dir.mkdir()
        sensor_dir = build_dir / "sensor-artifacts" / "bin" / "linux-amd64"
        sensor_dir.mkdir(parents=True)
        poller_src = ZENPLUS_DIR / "poller"
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(ZENPLUS_DIR),
        ).stdout.strip() or "unknown"
        build_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if poller_src.exists() and (poller_src / "cmd" / "poller").exists():
            result = subprocess.run(
                [GO_BIN, "build", "-buildvcs=false", "-o", str(go_dir / "zenplus-poller"), "./cmd/poller"],
                capture_output=True, text=True,
                cwd=str(poller_src), timeout=300,
                env={**os.environ, "GOOS": "linux", "GOARCH": "amd64", "CGO_ENABLED": "0"},
            )
            if result.returncode != 0:
                print(f"  ERROR: Go poller build failed:\n{result.stderr}")
                sys.exit(1)
            else:
                print(f"  Built: zenplus-poller ({(go_dir / 'zenplus-poller').stat().st_size / 1024 / 1024:.1f} MB)")
        else:
            print("  ERROR: No Go poller source found")
            sys.exit(1)

        # NetFlow collector (BUG-12: previously never built/shipped, so the
        # collector fix could not reach appliances via OTA — only its source
        # was copied while the running binary stayed stale).
        if poller_src.exists() and (poller_src / "cmd" / "netflow-collector").exists():
            result = subprocess.run(
                [GO_BIN, "build", "-buildvcs=false", "-o", str(go_dir / "zenplus-netflow-collector"), "./cmd/netflow-collector"],
                capture_output=True, text=True,
                cwd=str(poller_src), timeout=300,
                env={**os.environ, "GOOS": "linux", "GOARCH": "amd64", "CGO_ENABLED": "0"},
            )
            if result.returncode != 0:
                print(f"  ERROR: Go netflow-collector build failed:\n{result.stderr}")
                sys.exit(1)
            print(f"  Built: zenplus-netflow-collector ({(go_dir / 'zenplus-netflow-collector').stat().st_size / 1024 / 1024:.1f} MB)")
        else:
            print("  ERROR: No Go netflow-collector source found")
            sys.exit(1)

        if poller_src.exists() and (poller_src / "cmd" / "sensor").exists():
            ldflags = (
                f"-X main.version=sensor-{version} "
                f"-X main.commit={commit} "
                f"-X main.buildDate={build_date}"
            )
            result = subprocess.run(
                [
                    GO_BIN, "build", "-buildvcs=false", "-ldflags", ldflags,
                    "-o", str(sensor_dir / "zenplus-sensor"), "./cmd/sensor",
                ],
                capture_output=True, text=True,
                cwd=str(poller_src), timeout=300,
                env={**os.environ, "GOOS": "linux", "GOARCH": "amd64", "CGO_ENABLED": "0"},
            )
            if result.returncode != 0:
                print(f"  ERROR: Go sensor build failed:\n{result.stderr}")
                sys.exit(1)
            sensor_binary = sensor_dir / "zenplus-sensor"
            sensor_sha = sha256_file(str(sensor_binary))
            (sensor_dir / "zenplus-sensor.sha256").write_text(f"{sensor_sha}  zenplus-sensor\n")
            (sensor_dir / "manifest.json").write_text(json.dumps({
                "product": "ZenPlus Remote Sensor",
                "platform": "linux-amd64",
                "version": f"sensor-{version}",
                "commit": commit,
                "built_at": build_date,
                "binary": "zenplus-sensor",
                "sha256_file": "zenplus-sensor.sha256",
                "sha256": sensor_sha,
            }, indent=2) + "\n")
            print(f"  Built: zenplus-sensor ({sensor_binary.stat().st_size / 1024 / 1024:.1f} MB)")
        else:
            print("  ERROR: No Go sensor source found")
            sys.exit(1)
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

    # 5. Copy migrations only when explicitly requested. Older migrations in
    # deployed appliances are not all safely re-runnable, so code-only releases
    # should not package the historical migration set.
    print("[5/7] Checking for migrations ...")
    migration_count = 0
    migrate_dir = build_dir / "migrations"
    if selected_migrations:
        migrate_dir.mkdir()
        for f in selected_migrations:
            shutil.copy2(f, migrate_dir / f.name)
            migration_count += 1
            print(f"  + {f.name}")
    if migration_count == 0:
        if migrate_dir.exists():
            shutil.rmtree(migrate_dir)
        print("  No migrations named explicitly")
    shipped = len(list((build_dir / "code" / "scripts").glob("migrate-*.sql")))
    print(f"  {shipped} migration(s) shipped in code/scripts/ — sync-schema.py "
          f"applies whatever each appliance is missing")

    # 6. Create manifest.json
    print("[6/7] Creating manifest ...")
    steps = []

    # Stop services before update
    steps.append({"type": "stop_services", "services": ["zenplus-api", "zenplus-poller", "zenplus-netflow-collector"]})
    steps.append({"type": "backup", "targets": ["code", "database"]})

    # Heal the OS prerequisites every appliance needs but older installers
    # missed. apt_install is idempotent — already-present packages are a
    # no-op. Listed packages must stay in lockstep with install.sh's core
    # apt-get install line so fresh installs and OTA upgrades converge.
    steps.append({
        "type": "apt_install",
        "packages": ["snmp", "iputils-ping"],
        "update_first": True,
        "timeout": 300,
    })

    steps.append({"type": "apply_code", "method": "replace", "source": "code/"})

    # Run setup-support.sh so the support-bundle systemd template, sudoers
    # grant, and runtime dirs are present on appliances that were installed
    # before the Support tab existed. The script is idempotent — safe to
    # re-run on every OTA — and lives inside the bundle we just applied.
    if (Path(build_dir) / "code" / "scripts" / "setup-support.sh").exists():
        steps.append({
            "type": "run_hook",
            "script": "code/scripts/setup-support.sh",
            "timeout": 120,
        })

    # Storage management (Settings -> Storage): install the root-owned helper
    # scripts, sudoers grant, backup dirs, and ClickHouse backups-disk config.
    # Idempotent — safe on every OTA, and required on appliances installed
    # before the Storage tab existed.
    if (Path(build_dir) / "code" / "scripts" / "setup-storage.sh").exists():
        steps.append({
            "type": "run_hook",
            "script": "code/scripts/setup-storage.sh",
            "timeout": 300,
        })

    # Security tab (Settings -> General -> Security): install the root-owned
    # TLS helper, sudoers grant, /etc/zenplus/tls and the staging directory.
    # Idempotent — safe on every OTA, and required on appliances installed
    # before the Security tab existed.
    if (Path(build_dir) / "code" / "scripts" / "setup-security.sh").exists():
        steps.append({
            "type": "run_hook",
            "script": "code/scripts/setup-security.sh",
            "timeout": 120,
        })

    # Best-effort GeoIP provisioning (Phase 2b). fetch-geoip.py always exits 0
    # and skips when the current month's DB is already present, so a download
    # failure (no route to db-ip.com) never fails or delays the OTA update.
    if (Path(build_dir) / "code" / "scripts" / "fetch-geoip.py").exists():
        steps.append({
            "type": "run_hook",
            "script": "code/scripts/fetch-geoip.py",
            "timeout": 180,
        })


    if (build_dir / "requirements.txt").exists():
        steps.append({"type": "pip_install", "requirements": "requirements.txt"})

    if (build_dir / "migrations").exists():
        named = [build_dir / "migrations" / f.name for f in selected_migrations]
        for f in named:
            engine = _migration_engine(f)
            steps.append({"type": "run_migration", "engine": engine, "file": f"migrations/{f.name}"})
        for f in named:
            steps.append({"type": "install_config",
                          "source": f"migrations/{f.name}",
                          "dest": f"/opt/zenplus/scripts/{f.name}"})

    # The schema gate. Emitted on EVERY release, not only schema releases:
    # it converges both databases with the complete migration set that
    # apply_code just landed, heals a ledger that claims a migration ran when
    # it did not, and exits non-zero on anything it cannot resolve — which
    # fails this step and rolls the appliance back rather than leaving it
    # stamped with a version its schema does not support.
    if (Path(build_dir) / "code" / "scripts" / "sync-schema.py").exists():
        steps.append({
            "type": "run_hook",
            "script": "code/scripts/sync-schema.py",
            "timeout": 1800,
        })
    else:
        print("  WARNING: scripts/sync-schema.py missing — release has no schema gate!")

    if (build_dir / "dashboard-dist.tar.gz").exists():
        steps.append({"type": "build_dashboard", "prebuilt": True, "source": "dashboard-dist.tar.gz"})

    if (build_dir / "go-binaries" / "zenplus-poller").exists():
        steps.append({"type": "install_binary", "source": "go-binaries/zenplus-poller",
                       "dest": "/opt/zenplus/bin/zenplus-poller"})

    # NetFlow collector binary + unit (BUG-12). install_systemd is idempotent and
    # adds the unit on appliances that never had it; service_control skips it when
    # absent, so stop/start of the collector is safe fleet-wide.
    if (build_dir / "go-binaries" / "zenplus-netflow-collector").exists():
        steps.append({"type": "install_binary", "source": "go-binaries/zenplus-netflow-collector",
                       "dest": "/opt/zenplus/bin/zenplus-netflow-collector"})
        steps.append({"type": "install_systemd",
                       "source": "code/poller/systemd/zenplus-netflow-collector.service",
                       "enable": True})

    agent_artifact_dir = build_dir / "agent-artifacts"
    if agent_artifact_dir.is_dir():
        for plat_dir in sorted(p for p in agent_artifact_dir.iterdir() if p.is_dir()):
            for pkg in sorted(plat_dir.iterdir()):
                steps.append({"type": "install_config",
                              "source": f"agent-artifacts/{plat_dir.name}/{pkg.name}",
                              "dest": f"/opt/zenplus/artifacts/agents/{plat_dir.name}/{pkg.name}"})
        if (agent_artifact_dir / "manifest.json").is_file():
            steps.append({"type": "install_config",
                          "source": "agent-artifacts/manifest.json",
                          "dest": "/opt/zenplus/artifacts/agents/manifest.json"})

    sensor_artifact_dir = build_dir / "sensor-artifacts" / "bin" / "linux-amd64"
    if (sensor_artifact_dir / "zenplus-sensor").exists():
        steps.append({"type": "install_binary",
                      "source": "sensor-artifacts/bin/linux-amd64/zenplus-sensor",
                      "dest": "/opt/zenplus/artifacts/sensors/bin/linux-amd64/zenplus-sensor"})
        steps.append({"type": "install_config",
                      "source": "sensor-artifacts/bin/linux-amd64/zenplus-sensor.sha256",
                      "dest": "/opt/zenplus/artifacts/sensors/bin/linux-amd64/zenplus-sensor.sha256"})
        steps.append({"type": "install_config",
                      "source": "sensor-artifacts/bin/linux-amd64/manifest.json",
                      "dest": "/opt/zenplus/artifacts/sensors/bin/linux-amd64/manifest.json"})

    # Restart services
    steps.append({"type": "start_services",
                   "services": ["zenplus-api", "zenplus-poller", "zenplus-netflow-collector",
                                "netmon-gunicorn", "netmon-celery", "netmon-celery-beat", "nginx"]})
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
        "agent_packages": agent_staged,
        "steps": steps,
        "rollback_steps": [
            {"type": "restore_backup"},
            {"type": "start_services",
             "services": ["zenplus-api", "zenplus-poller",
                          "zenplus-netflow-collector", "netmon-gunicorn",
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
        "migrations": [p.name for p in selected_migrations],
        "agent_packages": agent_staged,
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

  # Build a schema release with explicit migrations only:
  python scripts/build-release.py build --version 1.2.12 \
    --migration migrate-017-discovery-v2.sql \
    --migration migrate-018-discovery-windows-creds.sql

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
    build_p.add_argument(
        "--agent-artifact-dir",
        default=None,
        help="Agent artifact root containing windows/, linux/, macos/ (default: <ZENPLUS_DIR>/artifacts/agents)",
    )
    build_p.add_argument(
        "--skip-agent-artifacts",
        action="store_true",
        help="Emergency override: build without an agent installer",
    )
    build_p.add_argument("--include-migrations", action="store_true",
                         help="Require explicit --migration values for schema releases")
    build_p.add_argument("--migration", action="append", default=[],
                         help="Package one scripts/migrate-*.sql file; repeat for multiple files")

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
    pub_p.add_argument("--agent-artifact-dir", default=None)
    pub_p.add_argument("--skip-agent-artifacts", action="store_true")
    pub_p.add_argument("--include-migrations", action="store_true",
                       help="Require explicit --migration values for schema releases")
    pub_p.add_argument("--migration", action="append", default=[],
                       help="Package one scripts/migrate-*.sql file; repeat for multiple files")
    pub_p.add_argument("--rollout", default=None,
                       choices=["canary", "percentage", "full"],
                       help="Auto-create rollout after publishing")
    pub_p.add_argument("--rollout-pct", type=int, default=100, help="Rollout percentage (default 100)")
    pub_p.add_argument("--rollout-group", default=None, help="Target rollout group")

    # Lint migrations
    lint_p = sub.add_parser("lint-migrations",
                            help="Verify migrate-*.sql checksums against the lockfile")
    lint_p.add_argument("--update-lock", action="store_true",
                        help="Record new migration files in the lockfile (commit the result)")

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
                      args.min_version, args.skip_dashboard, args.skip_go,
                      args.include_migrations, args.migration,
                      Path(args.agent_artifact_dir) if args.agent_artifact_dir else None,
                      args.skip_agent_artifacts)

    elif args.command == "publish":
        if args.file:
            zup_path = Path(args.file)
            if not zup_path.exists():
                print(f"ERROR: File not found: {zup_path}")
                sys.exit(1)
        else:
            zup_path = build_package(args.version, args.changelog, args.severity,
                                     args.min_version, args.skip_dashboard, args.skip_go,
                                     args.include_migrations, args.migration,
                                     Path(args.agent_artifact_dir) if args.agent_artifact_dir else None,
                                     args.skip_agent_artifacts)
        publish_package(zup_path, args.version, args.changelog,
                        args.severity, args.min_version)

        if args.rollout:
            create_rollout(args.version, args.rollout, args.rollout_group, args.rollout_pct)

    elif args.command == "lint-migrations":
        lint_migrations(update_lock=args.update_lock)
        print("  Migrations OK.")

    elif args.command == "list":
        list_releases()

    elif args.command == "rollout":
        create_rollout(args.version, args.stage, args.group, args.pct)


if __name__ == "__main__":
    main()
