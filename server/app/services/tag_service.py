"""Tag registry helpers.

Tags live in two places by design: the `tags` table is the catalog (stable
id, canonical spelling, color, description) while assignments stay in the
devices.tags JSONB array, so jsonb_exists() filters, maintenance-window tag
scoping and the GIN index keep working unchanged.

Every device write goes through canonicalize_tags() so a device row only
ever carries registry spellings ("Core" and "core" can't drift apart) and
labels the registry has never seen auto-register instead of erroring —
CSV imports and raw API clients keep working with zero ceremony.
"""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Kept in sync with the seed palette in migrate-067-tag-registry.sql.
PALETTE = [
    "#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444",
    "#8b5cf6", "#ec4899", "#14b8a6", "#f97316", "#84cc16",
]

MAX_TAG_LEN = 64

# Every surface that stores tag assignments as an array, and every column
# that stores a single tag name as a scope. Declared once so a rename or
# delete cannot silently miss one — see the note in api/v1/tags.py.
#
# (table, column, kind) — kind is the storage shape: "jsonb" for JSONB text
# arrays, "text[]" for Postgres TEXT[]. service_checks and friends predate
# the registry with TEXT[]; converting them would touch the ORM model and
# every reader for no behavioral gain, so propagation speaks both shapes.
TAG_ARRAY_SURFACES: tuple[tuple[str, str, str], ...] = (
    ("devices", "tags", "jsonb"),
    ("servers", "tags", "jsonb"),
    ("device_interfaces", "tags", "jsonb"),
    ("apm_services", "tags", "jsonb"),
    ("service_checks", "tags", "text[]"),
    ("service_check_templates", "tags", "text[]"),
    ("netpath_probes", "tags", "text[]"),
)

# users.scope_tags participates in RENAME only: access must follow a renamed
# tag, but a DELETE keeps the (now matching-nothing) name in the scope —
# stripping it could silently widen a user whose last scope tag was deleted
# to an unrestricted view. Fail-closed. See migrate-104.
TAG_RENAME_ONLY_SURFACES: tuple[tuple[str, str, str], ...] = (
    ("users", "scope_tags", "jsonb"),
)

TAG_SCOPE_COLUMNS: tuple[tuple[str, str, str | None], ...] = (
    ("device_maintenance", "scope_tag", "scope_type = 'tag'"),
    ("service_check_maintenance", "scope_tag", "scope_type = 'tag'"),
    ("alert_rules", "scope_tag", None),
)


def auto_color(name: str) -> str:
    """Deterministic palette pick so a tag gets the same color everywhere."""
    return PALETTE[sum(name.lower().encode()) % len(PALETTE)]


def clean_tags(raw) -> list[str]:
    """Trim, drop empties, cap length, dedupe case-insensitively.

    First spelling wins and input order is preserved.
    """
    out: list[str] = []
    seen: set[str] = set()
    for t in raw or []:
        t = str(t).strip()[:MAX_TAG_LEN]
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
    return out


def tag_set(raw) -> set[str]:
    """Lowercased tag names from a devices.tags JSONB value, for scope matching.

    Callers read the column straight out of raw SQL, where it can arrive as a
    decoded list, as NULL for a device that has never been tagged, or as a JSON
    string depending on the driver. Comparison is case-insensitive because the
    registry canonicalises spelling but assignments written before it existed
    may not match its casing.
    """
    if not raw:
        return set()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return set()
    if not isinstance(raw, (list, tuple, set)):
        return set()
    # `if t` first: a None element would otherwise stringify to the literal
    # tag "none" and match a rule scoped to a tag of that name.
    return {str(t).strip().lower() for t in raw if t and str(t).strip()}


async def canonicalize_tags(db: AsyncSession, raw) -> list[str]:
    """Map each tag to its registry spelling, registering unknown ones.

    Runs inside the caller's transaction — a failed device write also rolls
    back any tags it would have registered.
    """
    out: list[str] = []
    for t in clean_tags(raw):
        row = (await db.execute(
            text("SELECT name FROM tags WHERE LOWER(name) = LOWER(:n)"),
            {"n": t},
        )).first()
        if row:
            out.append(row[0])
            continue
        await db.execute(
            text("INSERT INTO tags (name, color) VALUES (:n, :c) ON CONFLICT DO NOTHING"),
            {"n": t, "c": auto_color(t)},
        )
        out.append(t)
    return out


async def adopt_device_tags(db: AsyncSession, commit: bool = True) -> int:
    """Register any labels found on a tagged surface the registry doesn't know.

    Covers fleets that tagged rows before the registry existed and rows written
    by paths that bypass canonicalize_tags(). Named for devices for backwards
    compatibility; it sweeps every surface in TAG_ARRAY_SURFACES.
    """
    union = " UNION ".join(
        f"""SELECT DISTINCT el
            FROM {tbl} t, {"jsonb_array_elements_text(COALESCE(t." + col + ", '[]'::jsonb))"
                           if kind == "jsonb"
                           else "unnest(COALESCE(t." + col + ", ARRAY[]::text[]))"} el
            WHERE btrim(el) <> ''
              AND NOT EXISTS (SELECT 1 FROM tags x WHERE LOWER(x.name) = LOWER(el))"""
        for tbl, col, kind in TAG_ARRAY_SURFACES
    )
    missing = (await db.execute(text(union))).scalars().all()
    for name in missing:
        name = name.strip()[:MAX_TAG_LEN]
        await db.execute(
            text("INSERT INTO tags (name, color) VALUES (:n, :c) ON CONFLICT DO NOTHING"),
            {"n": name, "c": auto_color(name)},
        )
    if missing and commit:
        await db.commit()
    return len(missing)
