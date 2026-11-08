"""Custom page-access roles: limit a role to selected pages and network maps.

``roles.page_access`` (migrate-185) is NULL for every role that is not limited
(today's behaviour, and every role after the upgrade). Otherwise it is::

    {"pages": ["/devices", "/maps/manual", ...] | null,
     "maps":  ["<manual map uuid>", ...]        | null}

``pages`` lists sidebar pages by their navigation ``to`` (the leaf entries of
``dashboard/src/components/layout/navigation.ts``); ``maps`` lists the network
maps (the NOC wall views) members may open. ``null`` in either = no limit.

Enforcement is not a parallel permission system: a page list narrows the
role's permissions (:func:`restrict_permissions`), and
``security.get_role_permissions`` returns the narrowed set, so every existing
guard (route policy module defaults, ``require_permission``, stream guards,
delegation ceilings) refuses the API of a page the role does not list. A
permission stays when

* a listed page the role can open needs it (the page's navigation gate, its
  parent's gate included) - the same permission the sidebar checks;
* it extends one of those on a page that is not view-only (``PERMISSION_IMPLIES``,
  e.g. ``devices.manage`` on the Devices page, ``alerts.acknowledge`` on Active
  Alerts);
* a listed page shows it read-only (the page's ``uses``: dashboard widgets);
* a kept permission depends on it (``PERMISSION_IMPLIES``).

The API boundary is therefore the permission: two pages that read the same
permission (e.g. Devices and Link Utilization, both ``devices.view``) share an
API, and listing one makes that data readable. Full administration is never
limited (the role editor refuses the combination).

The map list is enforced on the map API itself (``app.api.v1.manual_maps``).

The page catalog (``page_catalog.json`` next to this module) is generated from
the navigation tree by ``dashboard/scripts/test-page-access.mjs --write``; CI
fails when it drifts. The same vectors are tested on both sides.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

from app.core.permissions import PERMISSION_IMPLIES, SUPERUSER_PERMISSION

CATALOG_PATH = Path(__file__).with_name("page_catalog.json")

MAX_PAGES = 200
MAX_MAPS = 500


@dataclass(frozen=True)
class Page:
    key: str
    label: str
    group_label: str
    parent: Optional[str]
    gates: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]  # (any-of, all-of) per gate
    uses: tuple[str, ...]
    view_only: bool
    personal: bool

    @property
    def title(self) -> str:
        return " > ".join(x for x in (self.group_label, self.parent, self.label) if x)


@lru_cache(maxsize=1)
def pages() -> dict[str, Page]:
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    out: dict[str, Page] = {}
    for p in raw.get("pages", []):
        out[p["key"]] = Page(
            key=p["key"],
            label=p.get("label") or p["key"],
            group_label=p.get("group_label") or "",
            parent=p.get("parent"),
            gates=tuple((tuple(g.get("any") or ()), tuple(g.get("all") or ())) for g in p.get("gates") or ()),
            uses=tuple(p.get("uses") or ()),
            view_only=bool(p.get("view_only")),
            personal=bool(p.get("personal")),
        )
    return out


def page_reachable(page: Page, granted: set[str] | frozenset[str]) -> bool:
    """The role's permissions open the page (every gate passes)."""
    for any_of, all_of in page.gates:
        if any(p not in granted for p in all_of):
            return False
        if any_of and not any(p in granted for p in any_of):
            return False
    return True


def normalize(raw: Any) -> Optional[dict]:
    """The stored value as ``{"pages": list|None, "maps": list|None}``, or None
    when the role is not limited. Anything that is not a mapping (NULL, or an
    unexpected shape) means no limit."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Optional[list[str]]] = {"pages": None, "maps": None}
    for field in ("pages", "maps"):
        value = raw.get(field)
        if isinstance(value, list):
            out[field] = sorted({str(v) for v in value if isinstance(v, str) and v})
    if out["pages"] is None and out["maps"] is None:
        return None
    return out


# Used when the page limit of a role cannot be read and nothing was cached:
# no page, no map (hide, never reveal).
LOOKUP_FAILED: dict = {"pages": [], "maps": []}


def restrict_permissions(perms: Iterable[str], access: Optional[dict]) -> list[str]:
    """Permissions a role keeps under its page limit (see the module docstring)."""
    perms = list(perms)
    listed = (access or {}).get("pages")
    if listed is None or SUPERUSER_PERMISSION in perms:
        return perms
    granted = set(perms)
    catalog = pages()
    keep: set[str] = set()
    extendable: set[str] = set()
    for key in listed:
        page = catalog.get(key)
        if page is None or page.personal or not page_reachable(page, granted):
            continue
        for any_of, all_of in page.gates:
            for p in (*all_of, *any_of):
                if p in granted:
                    keep.add(p)
                    if not page.view_only:
                        extendable.add(p)
        keep.update(p for p in page.uses if p in granted)
    keep.update(p for p in granted if any(q in extendable for q in PERMISSION_IMPLIES.get(p, ())))
    for p in list(keep):
        keep.update(q for q in PERMISSION_IMPLIES.get(p, ()) if q in granted)
    return [p for p in perms if p in keep]


def allowed_maps(access: Optional[dict]) -> Optional[set[str]]:
    """Map ids the role is limited to, or None for every map."""
    maps = (access or {}).get("maps")
    return None if maps is None else {str(m).lower() for m in maps}


def is_limited(access: Optional[dict]) -> bool:
    return access is not None and (access.get("pages") is not None or access.get("maps") is not None)


def within(inner: Optional[dict], outer: Optional[dict]) -> bool:
    """Every page / map ``inner`` opens is open under ``outer`` too (used for
    delegation: a page-limited manager cannot hand out wider access)."""
    for field in ("pages", "maps"):
        limit = (outer or {}).get(field)
        if limit is None:
            continue
        mine = (inner or {}).get(field)
        if mine is None:
            return False
        allowed = set(limit)
        if field == "pages":
            allowed |= {k for k, p in pages().items() if p.personal}
        if not set(mine) <= allowed:
            return False
    return True


class PageAccessError(ValueError):
    pass


def validate(raw: Any) -> Optional[dict]:
    """Clean an editor payload. Raises PageAccessError for unknown pages or
    malformed map ids. Personal pages are dropped (they are always open).
    Map existence is checked by the caller (it needs the database)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PageAccessError("page_access must be an object with 'pages' and 'maps'")
    unknown_fields = set(raw) - {"pages", "maps"}
    if unknown_fields:
        raise PageAccessError(f"Unknown page_access fields: {', '.join(sorted(unknown_fields))}")
    out: dict[str, Optional[list[str]]] = {"pages": None, "maps": None}
    pages_in = raw.get("pages")
    if pages_in is not None:
        if not isinstance(pages_in, list) or not all(isinstance(p, str) for p in pages_in):
            raise PageAccessError("pages must be a list of page addresses")
        if len(pages_in) > MAX_PAGES:
            raise PageAccessError(f"At most {MAX_PAGES} pages")
        catalog = pages()
        unknown = sorted({p for p in pages_in if p not in catalog})
        if unknown:
            raise PageAccessError(f"Unknown pages: {', '.join(unknown[:6])}")
        out["pages"] = sorted({p for p in pages_in if not catalog[p].personal})
    maps_in = raw.get("maps")
    if maps_in is not None:
        if not isinstance(maps_in, list) or len(maps_in) > MAX_MAPS:
            raise PageAccessError(f"maps must be a list of at most {MAX_MAPS} map ids")
        cleaned = set()
        for m in maps_in:
            try:
                cleaned.add(str(uuid.UUID(str(m))))
            except (ValueError, TypeError, AttributeError):
                raise PageAccessError(f"Not a map id: {m!r}")
        out["maps"] = sorted(cleaned)
    if out["pages"] is None and out["maps"] is None:
        return None
    return out


def describe(access: Optional[dict]) -> dict:
    """Readable form for the audit log and role history."""
    if not is_limited(access):
        return {"pages": "all", "maps": "all"}
    catalog = pages()
    listed = access.get("pages")
    return {
        "pages": "all" if listed is None else [catalog[k].title if k in catalog else k for k in listed],
        "maps": "all" if access.get("maps") is None else list(access["maps"]),
    }
