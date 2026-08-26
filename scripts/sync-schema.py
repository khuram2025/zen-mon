#!/usr/bin/env python3
"""Converge both databases with the migrations on disk, then prove it.

This is the single gate that makes "an update shipped code but not its schema"
impossible. It runs on every OTA update as a manifest hook, after the code has
landed and before the version marker advances, and it is deliberately
self-contained so the *new* copy of it — the one that just arrived in the
release payload — is what runs, not the older one already installed.

What it guarantees
------------------
1. Every ``migrate-*.sql`` on disk is considered, in release order, on every
   update — not only the ones a release author remembered to name. An appliance
   that skipped releases catches up in one pass.
2. PostgreSQL and ClickHouse both converge, with a ledger each.
3. The ClickHouse ledger is verified against the objects that actually exist,
   so a migration recorded as applied but never run is detected and healed.
4. It exits non-zero on any unresolved drift, which fails the update step and
   triggers rollback. A half-migrated appliance never gets stamped with the new
   version number.

Also runnable by hand:  sudo /opt/zenplus/scripts/sync-schema.py --check
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SELF_DIR = Path(__file__).resolve().parent
ZENPLUS_DIR = Path(os.environ.get("ZENPLUS_DIR", "/opt/zenplus"))
JSON_MARKER = "ZENPLUS_MIGRATION_JSON "

sys.path.insert(0, str(SELF_DIR))
import ch_migrate  # noqa: E402


def default_scripts_dir() -> Path:
    """The migration set to converge against: the appliance's, not the payload's.

    As an update hook this script executes from the extracted release payload,
    whose scripts/ directory now carries the full migration set anyway — but the
    installed one is the authoritative copy apply_code just refreshed, and it is
    what a hand-run invocation should target too.
    """
    installed = ZENPLUS_DIR / "scripts"
    if installed.is_dir() and any(installed.glob("migrate-*.sql")):
        return installed
    return SELF_DIR


def runner_for(scripts_dir: Path) -> Path:
    """Locate run-migrations.py next to the migration set, else next to us."""
    candidate = Path(scripts_dir) / "run-migrations.py"
    return candidate if candidate.exists() else SELF_DIR / "run-migrations.py"


STATUS_FILE = ZENPLUS_DIR / ".schema-status.json"


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from .env for CLICKHOUSE_PASSWORD and friends.

    systemd already injects these for the updater, but a human running this by
    hand has no such luck, and a missing ClickHouse password looks exactly like
    a schema problem.
    """
    if not path.exists():
        return
    try:
        content = path.read_text()
    except OSError:
        return
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run_postgres(scripts_dir: Path, *, check_only: bool) -> dict:
    """Apply pending PostgreSQL migrations against the on-disk migration set.

    Always points the runner at the appliance's own scripts directory, never at
    an extracted release payload: the payload only holds the migrations of one
    release, so running against it would leave every earlier gap invisible.
    """
    runner_args = [
        str(runner_for(scripts_dir)),
        "--scripts-dir", str(scripts_dir),
        "--database", os.environ.get("PGDATABASE", "zenplus"),
        "--json",
    ]
    if check_only:
        runner_args.append("--status")

    if getpass.getuser() == "postgres":
        cmd = [sys.executable, *runner_args]
    else:
        # psql peer auth: the postgres superuser is the one identity guaranteed
        # to connect on a stock appliance.
        cmd = ["sudo", "-u", "postgres", "python3", *runner_args]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": f"postgres migration runner failed to start: {e}",
                "applied": [], "skipped": [], "pending": [], "drift": [], "failed": []}

    report = {"applied": [], "skipped": [], "pending": [], "drift": [],
              "failed": [], "baselined": [], "unresolved": [], "reconciled": [],
              "healed": [], "invariant_drift": []}
    for line in result.stdout.splitlines():
        if line.startswith(JSON_MARKER):
            try:
                report = json.loads(line[len(JSON_MARKER):])
            except json.JSONDecodeError:
                pass
    if result.returncode not in (0, 2) and "error" not in report:
        report["error"] = (result.stderr or result.stdout).strip()[:2000]
    return report


def run_clickhouse(scripts_dir: Path, *, check_only: bool,
                   retries: int = 5, delay: int = 6) -> dict:
    """Converge ClickHouse, waiting for it to come up after a service restart.

    An unreachable ClickHouse is reported as an error rather than as "nothing
    to do" — silently treating "cannot tell" as "healthy" is how the schema gap
    went unnoticed for a whole release.
    """
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            return ch_migrate.sync(scripts_dir, dry_run=check_only)
        except ch_migrate.ClickHouseError as e:
            last_error = str(e)
            if attempt < retries:
                time.sleep(delay)
    return {"applied": [], "baselined": [], "healed": [], "pending": [],
            "failed": [], "unresolved": [], "error": f"ClickHouse unreachable: {last_error}"}


def summarize(pg: dict, ch: dict) -> dict:
    """Fold both engine reports into one verdict."""
    problems: list[str] = []
    if pg.get("error"):
        problems.append(f"postgres: {pg['error']}")
    for name in pg.get("pending", []):
        problems.append(f"postgres: {name} not applied")
    for name in pg.get("drift", []):
        problems.append(f"postgres: {name} checksum differs from the applied record")
    for name in pg.get("invariant_drift", []):
        problems.append(f"postgres: {name} definition is not canonical")
    for item in pg.get("failed", []):
        problems.append(f"postgres: {item['filename']} failed: {item.get('error', '')[:200]}")
    for name in pg.get("unresolved", []):
        problems.append(
            f"postgres: {name} cannot be verified and inserts rows — apply it by hand"
        )

    if ch.get("error"):
        problems.append(f"clickhouse: {ch['error']}")
    for name in ch.get("pending", []):
        problems.append(f"clickhouse: {name} not applied")
    for item in ch.get("failed", []):
        problems.append(f"clickhouse: {item['filename']} failed: {item.get('error', '')[:200]}")
    for item in ch.get("unresolved", []):
        missing = ", ".join(item.get("missing", []))
        problems.append(
            f"clickhouse: {item['filename']} {item.get('reason', 'unresolved')} "
            f"(missing: {missing})"
        )

    return {
        "ok": not problems,
        "problems": problems,
        "postgres": pg,
        "clickhouse": ch,
    }


def write_status(status: dict) -> None:
    """Persist the verdict so check-in and the dashboard can surface drift."""
    try:
        STATUS_FILE.write_text(json.dumps(status, indent=2) + "\n")
    except OSError as e:
        print(f"warning: could not write {STATUS_FILE}: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Converge PostgreSQL + ClickHouse schema with the migrations on disk"
    )
    parser.add_argument("--scripts-dir", default=None,
                        help="Migration set to converge against (default: the installed one)")
    parser.add_argument("--check", action="store_true",
                        help="Report drift without applying anything")
    parser.add_argument("--json", action="store_true", help="Emit the full report as JSON")
    parser.add_argument("--engine", choices=("postgres", "clickhouse", "both"), default="both")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    _load_env_file(ZENPLUS_DIR / ".env")
    scripts_dir = Path(args.scripts_dir) if args.scripts_dir else default_scripts_dir()
    print(f"Converging schema against {scripts_dir}")

    pg: dict = {}
    ch: dict = {}
    if args.engine in ("postgres", "both"):
        pg = run_postgres(scripts_dir, check_only=args.check)
    if args.engine in ("clickhouse", "both"):
        ch = run_clickhouse(scripts_dir, check_only=args.check)

    status = summarize(pg, ch)
    status["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    status["mode"] = "check" if args.check else "apply"
    write_status(status)

    if args.json:
        print(json.dumps(status, indent=2))
    else:
        for engine, report in (("postgres", pg), ("clickhouse", ch)):
            if not report:
                continue
            if report.get("error"):
                # Never render "cannot reach the database" as "up to date" —
                # that conflation is the whole reason this gate exists.
                print(f"{engine}: UNREACHABLE — {report['error']}")
                continue
            counts = {
                k: len(v) for k, v in report.items()
                if isinstance(v, list) and v
            }
            print(f"{engine}: " + (", ".join(f"{k}={n}" for k, n in counts.items()) or "up to date"))
        for problem in status["problems"]:
            print(f"DRIFT {problem}", file=sys.stderr)

    if not status["ok"]:
        print(
            f"\nSchema is not consistent with the installed code "
            f"({len(status['problems'])} problem(s)). Details: {STATUS_FILE}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
