"""Shared rules for which filesystems count toward disk-capacity monitoring.

Optical/install media (UDF, ISO9660) and ephemeral/virtual mounts are excluded so
a full Windows setup ISO on D: does not mark the host critical while C: is healthy.
"""

from __future__ import annotations

# Lowercase fs_type values that must not affect health or filesystem alert rules.
EXCLUDED_FS_TYPES: frozenset[str] = frozenset({
    "udf",       # DVD / Windows install ISO (common false positive on D:)
    "iso9660",
    "cdfs",
    "tmpfs",
    "devtmpfs",
    "ramfs",
    "squashfs",
    "overlay",
    "autofs",
    "proc",
    "sysfs",
    "devfs",
})

_EXCLUDED_SQL = ", ".join(f"'{t}'" for t in sorted(EXCLUDED_FS_TYPES))
_CH_EXCLUDED_SQL = ", ".join(f"'{t}'" for t in sorted(EXCLUDED_FS_TYPES))


def pg_capacity_filter(column: str = "fs_type") -> str:
    """SQL fragment: true when a Postgres inventory row is monitorable."""
    return f"({column} IS NULL OR btrim({column}) = '' OR lower({column}) NOT IN ({_EXCLUDED_SQL}))"


def ch_capacity_filter(column: str = "fs_type") -> str:
    """SQL fragment for ClickHouse host_filesystem_metrics queries."""
    return f"({column} = '' OR lower({column}) NOT IN ({_CH_EXCLUDED_SQL}))"
