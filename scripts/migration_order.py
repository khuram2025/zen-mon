"""Deterministic ordering and static analysis for ZenPlus SQL migrations.

Single source of truth shared by the release builder (``build-release.py``), the
PostgreSQL runner (``run-migrations.py``) and the OTA updater
(``updater/postgres_sync.py``, ``updater/clickhouse_sync.py``). Deliberately
dependency-free and standalone: it must import cleanly as the ``postgres`` user
running a bare script, inside the updater package, and on the build host.

Ordering
--------
Migration numbers are NOT unique — there are two 016s, two 030s, two 031s, two
039s, two 043s, and one date-stamped file — so a plain filename sort is not a
meaningful release order. The authority is ``scripts/migrations.lock``: an
append-only file whose *line order* is the order migrations were first shipped.
Anything not yet in the lock (a migration being written right now) sorts after
everything locked, by name, so local dev still works.

Replay safety
-------------
The updater decides at runtime whether an already-shipped migration may be
re-executed to heal a gap. That decision is derived from the SQL itself rather
than a hand-maintained list — a hardcoded list is exactly what went stale and
caused ClickHouse migrations to be recorded as applied when they never ran.
"""

from __future__ import annotations

import re
from pathlib import Path

LOCK_NAME = "migrations.lock"

_CREATE_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?"
    r"(?:TABLE|VIEW|DICTIONARY)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([A-Za-z0-9_.\"`]+)",
    re.IGNORECASE,
)
_CREATE_UNGUARDED_RE = re.compile(
    r"\bCREATE\s+(?:MATERIALIZED\s+)?(?:TABLE|VIEW|DICTIONARY)\s+(?!IF\s+NOT\s+EXISTS)",
    re.IGNORECASE,
)
_INSERT_RE = re.compile(
    r"\bINSERT\s+INTO\b(?P<body>(?:[^;]|;(?=\s*\Z))*)", re.IGNORECASE
)
_INSERT_GUARD_RE = re.compile(
    r"\bON\s+CONFLICT\b|\bWHERE\s+NOT\s+EXISTS\b|\bON\s+DUPLICATE\b", re.IGNORECASE
)
_ADD_CONSTRAINT_RE = re.compile(
    r"\bADD\s+CONSTRAINT\s+([A-Za-z0-9_.\"`]+)", re.IGNORECASE
)
_ALTER_ADD_RE = re.compile(
    r"\bALTER\s+TABLE\s+[A-Za-z0-9_.\"`]+\s+ADD\s+", re.IGNORECASE
)
_ALTER_GUARD_RE = re.compile(
    r"\A(?:COLUMN\s+|INDEX\s+|CONSTRAINT\s+)?IF\s+NOT\s+EXISTS\b", re.IGNORECASE
)
_DROP_UNGUARDED_RE = re.compile(
    r"\bDROP\s+(?:TABLE|VIEW|COLUMN|INDEX)\s+(?!IF\s+EXISTS)", re.IGNORECASE
)
# Dollar-quoted bodies ($$…$$, $tag$…$tag$) and single-quoted literals. Their
# CONTENTS must never reach the analysers below: _INSERT_RE ends a statement at
# the first `;`, so one semicolon inside a seed payload truncates the statement
# before its trailing ON CONFLICT and the INSERT reads as unguarded. That is not
# hypothetical — a template-seed migration whose JSON contained "only; empty"
# was classified unverifiable and failed the update on every appliance.
_DOLLAR_QUOTED_RE = re.compile(r"\$(?P<tag>[A-Za-z_0-9]*)\$.*?\$(?P=tag)\$", re.DOTALL)
_SINGLE_QUOTED_RE = re.compile(r"'(?:[^']|'')*'", re.DOTALL)
# DDL inside a DO $$ … $$ block is invisible to the statement-level analysers
# once the block is neutralised, so replay-safety is judged on the block itself:
# every such block in this tree wraps its DDL in an IF NOT EXISTS probe, and one
# that does not is not safe to re-execute.
_DDL_IN_BLOCK_RE = re.compile(
    r"\bALTER\s+TABLE\b|\bCREATE\s+(?:MATERIALIZED\s+)?(?:TABLE|VIEW|DICTIONARY)\b"
    r"|\bDROP\s+(?:TABLE|VIEW|COLUMN|INDEX|CONSTRAINT)\b",
    re.IGNORECASE,
)
_BLOCK_GUARD_RE = re.compile(r"\bIF\s+(?:NOT\s+)?EXISTS\b", re.IGNORECASE)


def engine_of(name: str | Path) -> str:
    """Return the database engine a migration filename targets."""
    stem = Path(name).name.lower()
    if "clickhouse" in stem or stem.startswith("ch-"):
        return "clickhouse"
    return "postgres"


def load_lock(lock_path: Path) -> list[tuple[str, str]]:
    """Read migrations.lock as an ordered list of (filename, sha256).

    Order is file order, which is release order. Blank lines and ``#`` comments
    are ignored; a duplicate filename keeps its first position.
    """
    if not lock_path.exists():
        return []

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lock_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0].strip(), parts[1].strip()
        if name in seen:
            continue
        seen.add(name)
        entries.append((name, digest))
    return entries


def lock_positions(lock_path: Path) -> dict[str, int]:
    """Map each locked migration filename to its release-order position."""
    return {name: i for i, (name, _) in enumerate(load_lock(lock_path))}


def write_lock(lock_path: Path, ordered: list[tuple[str, str]]) -> None:
    """Write migrations.lock preserving the given order.

    The lockfile is append-only: callers pass existing entries first, in their
    existing order, then the new ones. Re-sorting would rewrite history and
    silently change the order every appliance applies migrations in.
    """
    lines = [f"{digest}  {name}" for name, digest in ordered]
    lock_path.write_text("\n".join(lines) + "\n")


def sequence_of(name: str) -> str:
    """Return the numeric sequence token of a migration filename.

    ``migrate-030-server-monitoring.sql`` -> ``030``. Returns ``""`` when the
    filename does not carry one.
    """
    match = re.match(r"migrate-(\d+)-", Path(name).name)
    return match.group(1) if match else ""


def ordered_migrations(
    scripts_dir: Path,
    *,
    engine: str | None = None,
    lock_path: Path | None = None,
) -> list[Path]:
    """Return migrate-*.sql under scripts_dir in authoritative release order.

    Locked migrations come first in lockfile order, then unlocked ones sorted by
    name. Pass ``engine`` to filter to ``"postgres"`` or ``"clickhouse"``.
    """
    scripts_dir = Path(scripts_dir)
    lock_path = lock_path or (scripts_dir / LOCK_NAME)
    positions = lock_positions(lock_path)
    unlocked_base = len(positions)

    paths = [p for p in scripts_dir.glob("migrate-*.sql") if p.is_file()]
    if engine is not None:
        paths = [p for p in paths if engine_of(p) == engine]

    def key(path: Path) -> tuple[int, str]:
        return (positions.get(path.name, unlocked_base), path.name)

    return sorted(paths, key=key)


def analyzable(sql: str) -> str:
    """Strip comments and string-literal contents for pattern analysis.

    Every analyser in this module is regex-based, so anything that can contain
    a statement terminator or a SQL keyword without meaning one has to go first:
    comments, dollar-quoted bodies and single-quoted literals. Literals collapse
    to ``''`` rather than being deleted, so statement structure survives.
    """
    stripped = re.sub(r"--[^\n]*", "", sql)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    stripped = _DOLLAR_QUOTED_RE.sub("''", stripped)
    return _SINGLE_QUOTED_RE.sub("''", stripped)


def created_objects(sql: str) -> list[str]:
    """Return the fully-qualified objects a migration creates, in order.

    Used to probe whether a migration's effects are actually present, instead of
    trusting a ledger row that may have been written without the SQL ever
    running.
    """
    seen: set[str] = set()
    objects: list[str] = []
    for match in _CREATE_RE.finditer(analyzable(sql)):
        name = match.group(1).strip('`"')
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        objects.append(name)
    return objects


def writes_rows(sql: str) -> bool:
    """True when re-running this migration would duplicate data.

    Narrower than the inverse of :func:`is_replay_safe`, and the distinction
    matters: an unguarded ``CREATE`` merely errors on replay and the error is
    caught and reported, whereas an unguarded ``INSERT`` silently doubles seed
    rows. Only the latter is a reason to refuse to run a migration we cannot
    otherwise verify.
    """
    stripped = analyzable(sql)
    return any(
        not _INSERT_GUARD_RE.search(match.group("body"))
        for match in _INSERT_RE.finditer(stripped)
    )


def is_replay_safe(sql: str) -> bool:
    """True when a migration can be re-executed against a partially-migrated DB.

    Replay-safe means every statement either no-ops or replaces itself when the
    target already exists. Three idioms count as guarded:

    * ``IF NOT EXISTS`` / ``IF EXISTS`` on DDL.
    * ``DROP CONSTRAINT IF EXISTS x`` followed by ``ADD CONSTRAINT x`` — the
      drop-then-add form used throughout these migrations, which is idempotent
      even though the ADD carries no guard of its own.
    * ``INSERT ... ON CONFLICT`` or ``INSERT ... WHERE NOT EXISTS``.

    A bare ``INSERT`` is not replay-safe: it duplicates seed rows on a second
    run. Neither is an unguarded ``CREATE``, which hard-errors against an
    object that already exists, nor a ``DO`` block whose DDL carries no
    ``IF NOT EXISTS`` probe of its own.
    """
    no_comments = re.sub(r"--[^\n]*", "", sql)
    no_comments = re.sub(r"/\*.*?\*/", "", no_comments, flags=re.DOTALL)
    for block in _DOLLAR_QUOTED_RE.finditer(no_comments):
        body = block.group(0)
        if _DDL_IN_BLOCK_RE.search(body) and not _BLOCK_GUARD_RE.search(body):
            return False

    stripped = analyzable(sql)

    for match in _INSERT_RE.finditer(stripped):
        if not _INSERT_GUARD_RE.search(match.group("body")):
            return False
    if _CREATE_UNGUARDED_RE.search(stripped):
        return False
    for match in _ALTER_ADD_RE.finditer(stripped):
        rest = stripped[match.end():]
        if _ALTER_GUARD_RE.match(rest):
            continue
        constraint = _ADD_CONSTRAINT_RE.match("ADD " + rest[:200])
        if constraint and re.search(
            r"\bDROP\s+CONSTRAINT\s+IF\s+EXISTS\s+" + re.escape(constraint.group(1)) + r"\b",
            stripped,
            re.IGNORECASE,
        ):
            continue
        return False
    if _DROP_UNGUARDED_RE.search(stripped):
        return False
    return True
