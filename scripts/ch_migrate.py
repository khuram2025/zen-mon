#!/usr/bin/env python3
"""Converge the ClickHouse schema with the migrations present on disk.

Why this is not a plain ledger runner
-------------------------------------
The first version of this sync trusted its ledger: on an appliance whose ledger
table did not exist yet, it *baselined* a hardcoded list of legacy migrations —
recording them as applied without running them — on the assumption that any
appliance old enough to lack a ledger must already have run them.

That assumption was wrong, and it silently bricked SNMP metric storage in the
field: an appliance that had never received ``migrate-004-snmp-clickhouse.sql``
got it stamped as applied, so ``zenplus.snmp_metrics`` was never created, the
poller collected fine and then failed every write with "Table ... does not
exist", and CPU/memory read as "—" forever. The ledger said healthy; the schema
was not there.

So this module never trusts the ledger alone. For every migration it asks
ClickHouse which tables, views, columns and required table settings actually
exist:

* recorded + objects present      -> skip
* not recorded + objects present  -> baseline (record, do not re-run)
* not recorded + objects missing  -> apply, then record
* recorded + objects missing      -> the ledger is lying: re-apply and heal

The replay decision is derived from the SQL itself (see
``migration_order.is_replay_safe``) rather than a hand-maintained list, because
the hand-maintained list is what went stale. A migration that backfills with a
blind ``INSERT`` or issues an unguarded ``CREATE`` is only run when *none* of
its objects exist; otherwise it is reported as unresolved drift for a human,
never replayed into duplicate rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_order  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
DATABASE = "zenplus"

LEDGER_DDL = f"""
CREATE DATABASE IF NOT EXISTS {DATABASE};
CREATE TABLE IF NOT EXISTS {DATABASE}.schema_migrations
(
    filename String,
    checksum String,
    applied_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(applied_at)
ORDER BY filename
"""

TABLE_SETTING_INVARIANTS: dict[str, dict[str, object]] = {
    "migrate-094-host-metric-insert-dedup-clickhouse.sql": {
        "tables": (
            "host_cpu_metrics",
            "host_memory_metrics",
            "host_filesystem_metrics",
            "host_disk_io_metrics",
            "host_network_metrics",
            "host_process_metrics",
            "host_service_state",
            "host_event_log_summary",
            "agent_health_metrics",
        ),
        "setting": "non_replicated_deduplication_window",
        "value": "10000",
    },
}


class ClickHouseError(RuntimeError):
    """ClickHouse rejected a query or could not be reached."""


def ch_query(sql: str, *, timeout: int = 300) -> str:
    """Execute SQL against ClickHouse and return stdout.

    On appliances ClickHouse runs as the ``zenplus-clickhouse`` Docker
    container, so there is no clickhouse-client on the host PATH. Use a host
    binary when one exists, otherwise exec the client inside the container —
    matching how install.sh applies ClickHouse schema. SQL goes on stdin
    because host paths are not visible inside the container.
    """
    password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    host_client = shutil.which("clickhouse-client")
    if host_client:
        cmd = [host_client, "--host", "127.0.0.1", "--port", "9000", "--multiquery"]
    else:
        cmd = ["docker", "exec", "-i", "zenplus-clickhouse",
               "clickhouse-client", "--multiquery"]
    if password:
        cmd += ["--password", password]

    try:
        result = subprocess.run(
            cmd, input=sql, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ClickHouseError(f"ClickHouse client failed: {e}") from e
    if result.returncode != 0:
        raise ClickHouseError(f"ClickHouse query failed: {result.stderr.strip()}")
    return result.stdout


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def existing_objects(query=ch_query) -> set[str]:
    """Return every table/view that exists in the zenplus database, lowercased."""
    out = query(
        f"SELECT name FROM system.tables WHERE database = '{DATABASE}' "
        "FORMAT TabSeparated"
    )
    return {
        f"{DATABASE}.{line.strip()}".lower()
        for line in out.splitlines()
        if line.strip()
    }


def existing_columns(query=ch_query) -> set[str]:
    """Return ``table.column`` for every ClickHouse column in our database."""
    out = query(
        f"SELECT table, name FROM system.columns WHERE database = '{DATABASE}' "
        "FORMAT TabSeparated"
    )
    columns: set[str] = set()
    for line in out.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
            columns.add(f"{parts[0].strip()}.{parts[1].strip()}".lower())
    return columns


def missing_table_settings(filename: str, query=ch_query) -> list[str]:
    """Return required table settings that are absent for a known migration."""
    spec = TABLE_SETTING_INVARIANTS.get(filename)
    if spec is None:
        return []

    tables = tuple(str(table) for table in spec["tables"])
    setting = str(spec["setting"])
    value = str(spec["value"])
    names = ", ".join(f"'{_esc(table)}'" for table in tables)
    out = query(
        "SELECT name, create_table_query FROM system.tables "
        f"WHERE database = '{DATABASE}' AND name IN ({names}) "
        "FORMAT JSONEachRow"
    )
    definitions: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ClickHouseError(
                f"Could not inspect table-setting postconditions: {exc}"
            ) from exc
        definitions[str(row.get("name", "")).lower()] = "".join(
            str(row.get("create_table_query", "")).lower().split()
        )

    setting_pattern = re.compile(
        rf"(?:settings|,){re.escape(setting.lower())}="
        rf"{re.escape(value.lower())}(?:,|$)"
    )
    return [
        f"setting:{DATABASE}.{table}.{setting}={value}"
        for table in tables
        if not setting_pattern.search(definitions.get(table.lower(), ""))
    ]


def ledger_entries(query=ch_query) -> dict[str, str]:
    """Return {filename: checksum} from the ClickHouse migration ledger."""
    out = query(
        f"SELECT filename, checksum FROM {DATABASE}.schema_migrations FINAL "
        "FORMAT TabSeparated"
    )
    entries: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.rstrip("\n").split("\t")
        if parts and parts[0].strip():
            entries[parts[0].strip()] = parts[1].strip() if len(parts) > 1 else ""
    return entries


def record(filename: str, checksum: str, query=ch_query) -> None:
    """Record a migration as applied.

    The ledger is a ReplacingMergeTree ordered by filename, so re-inserting an
    existing filename supersedes the old row — no mutation needed to heal one.
    """
    query(
        f"INSERT INTO {DATABASE}.schema_migrations (filename, checksum) "
        f"VALUES ('{_esc(filename)}', '{_esc(checksum)}')"
    )


def _objects_present(objects: list[str], present: set[str]) -> int:
    return sum(1 for obj in objects if obj.lower() in present)


def sync(
    scripts_dir: Path = SCRIPTS_DIR,
    *,
    query=ch_query,
    dry_run: bool = False,
) -> dict:
    """Bring ClickHouse in line with the migrations on disk.

    Returns a summary dict with per-file outcomes. Never raises for a single
    bad migration — it is recorded under ``failed`` and retried next run — but
    does raise ClickHouseError if ClickHouse itself is unreachable, because
    "cannot tell" must not be reported as "up to date".
    """
    summary: dict[str, list] = {
        "applied": [], "baselined": [], "healed": [],
        "pending": [], "failed": [], "unresolved": [],
    }

    migrations = migration_order.ordered_migrations(scripts_dir, engine="clickhouse")
    if not migrations:
        return summary

    if dry_run:
        # A check must not write. If the ledger table is absent there is
        # nothing recorded yet, which is exactly what an empty dict means.
        present = existing_objects(query=query)
        present_columns = existing_columns(query=query)
        try:
            ledger = ledger_entries(query=query)
        except ClickHouseError:
            ledger = {}
    else:
        query(LEDGER_DDL)
        ledger = ledger_entries(query=query)
        present = existing_objects(query=query)
        present_columns = existing_columns(query=query)

    for path in migrations:
        name = path.name
        sql = path.read_text()
        objects = migration_order.created_objects(sql)
        columns = migration_order.added_columns(sql)
        recorded = name in ledger
        found = _objects_present(objects, present)
        found_columns = sum(1 for col in columns if col.lower() in present_columns)
        setting_missing = missing_table_settings(name, query=query)
        has_evidence = bool(objects or columns or name in TABLE_SETTING_INVARIANTS)
        satisfied = (
            has_evidence
            and found == len(objects)
            and found_columns == len(columns)
            and not setting_missing
        )

        if recorded and (satisfied or not has_evidence):
            continue

        if not recorded and satisfied:
            # Effects already exist (fresh install ran them, or a pre-ledger
            # appliance). Record without re-running.
            if dry_run:
                summary["baselined"].append(name)
                continue
            try:
                record(name, sha256_file(path), query=query)
                summary["baselined"].append(name)
            except ClickHouseError as e:
                summary["failed"].append({"filename": name, "error": str(e)})
            continue

        # Something is missing. Decide whether we may (re)run this file.
        replay_safe = migration_order.is_replay_safe(sql)
        if not replay_safe and (found > 0 or found_columns > 0):
            # Partially present and not safe to replay: running it would either
            # duplicate backfilled rows or hard-error on an existing object.
            summary["unresolved"].append({
                "filename": name,
                "reason": "partially applied and not replay-safe",
                "missing": (
                    [o for o in objects if o.lower() not in present]
                    + [f"column:{col}" for col in columns if col.lower() not in present_columns]
                    + setting_missing
                ),
            })
            continue

        if dry_run:
            summary["pending"].append(name)
            continue

        try:
            query(sql)
            post_objects = existing_objects(query=query) if objects else present
            post_columns = existing_columns(query=query) if columns else present_columns
            postcondition_missing = (
                [o for o in objects if o.lower() not in post_objects]
                + [
                    f"column:{col}"
                    for col in columns
                    if col.lower() not in post_columns
                ]
                + missing_table_settings(name, query=query)
            )
            if postcondition_missing:
                summary["failed"].append({
                    "filename": name,
                    "error": "postcondition failed: " + ", ".join(postcondition_missing),
                })
                continue
            record(name, sha256_file(path), query=query)
            present = post_objects
            present_columns = post_columns
            if recorded:
                summary["healed"].append(name)
            else:
                summary["applied"].append(name)
        except ClickHouseError as e:
            summary["failed"].append({"filename": name, "error": str(e)})

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Converge the ZenPlus ClickHouse schema")
    parser.add_argument("--scripts-dir", default=str(SCRIPTS_DIR))
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be applied without changing anything")
    parser.add_argument("--json", action="store_true", help="Emit the summary as JSON")
    args = parser.parse_args(argv or sys.argv[1:])

    try:
        summary = sync(Path(args.scripts_dir), dry_run=args.dry_run)
    except ClickHouseError as e:
        print(f"ClickHouse unreachable: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary))
    else:
        for key in ("applied", "healed", "baselined", "pending", "failed", "unresolved"):
            for item in summary[key]:
                label = item if isinstance(item, str) else item["filename"]
                print(f"{key:11s} {label}")

    drifted = summary["failed"] or summary["unresolved"] or summary["pending"]
    return 2 if drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
