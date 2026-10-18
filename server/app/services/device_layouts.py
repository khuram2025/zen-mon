"""Server-saved device page widget layouts (migrate-165).

A layout is one user's arrangement of the widgets on one device page tab
(``section``), saved either for one device (scope ``device``) or for every
device of one vendor and device type (scope ``template``). This is the scoping
the browser keys already used (``widgetLayout.ts``): per user, per device or
vendor/type, per tab, and a device layout wins over the vendor/type template.

``items`` is stored as the page sent it, after validation, so entries for
widgets that are temporarily absent (a group with no data this hour) keep their
position for when they come back. An empty list is a deliberate "standard
layout" override and is kept as such.
"""
from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MAX_ITEMS = 100
GRID_COLUMNS = 12
SECTION_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")
# Vendors the page treats as "no vendor": no shared template layout for them.
_NO_VENDOR = {"", "unknown", "not reported", "n/a"}


class LayoutItem(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    x: int = Field(ge=0, le=GRID_COLUMNS - 1)
    y: int = Field(ge=0, le=200)
    w: int = Field(ge=1, le=GRID_COLUMNS)
    h: int = Field(ge=1, le=24)


class LayoutWrite(BaseModel):
    scope: str = Field(default="device", pattern="^(device|template)$")
    vendor: str | None = Field(default=None, max_length=200)
    device_type: str | None = Field(default=None, max_length=100)
    items: list[LayoutItem] = Field(default_factory=list, max_length=MAX_ITEMS)


def normalize(value: str | None) -> str:
    """Lower case, trimmed, inner whitespace collapsed (as the page does)."""
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def template_identity(vendor: str | None, device_type: str | None) -> tuple[str, str] | None:
    maker, kind = normalize(vendor), normalize(device_type)
    if maker in _NO_VENDOR or not kind:
        return None
    return maker, kind


def valid_section(section: str) -> bool:
    return bool(SECTION_RE.match(section or ""))


def clean_items(items: list[LayoutItem]) -> list[dict[str, Any]]:
    """One entry per widget id (the last wins), x kept inside the grid."""
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        w = min(item.w, GRID_COLUMNS)
        by_id[item.id] = {"id": item.id, "x": min(item.x, GRID_COLUMNS - w), "y": item.y, "w": w, "h": item.h}
    return list(by_id.values())


def _iso(ts) -> str | None:
    return ts.isoformat() if ts is not None else None


def _items(raw) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


async def read_layouts(db: AsyncSession, user_id: UUID, device_id: UUID,
                       template: tuple[str, str] | None) -> dict[str, Any]:
    """Every section's device layout and, when the vendor/type is known, the
    vendor/type template layouts, for one user."""
    rows = (await db.execute(text("""
        SELECT scope, section, items, updated_at FROM device_widget_layouts
        WHERE user_id = :uid AND (
              (scope = 'device' AND device_id = :did)
           OR (scope = 'template' AND vendor = :vendor AND device_type = :dtype))
    """), {"uid": str(user_id), "did": str(device_id),
           "vendor": template[0] if template else None,
           "dtype": template[1] if template else None})).mappings().all()
    out: dict[str, Any] = {"device": {}, "template": {},
                           "template_key": ({"vendor": template[0], "device_type": template[1]}
                                            if template else None)}
    for r in rows:
        out[r["scope"]][r["section"]] = {"items": _items(r["items"]), "updated_at": _iso(r["updated_at"])}
    return out


async def save_layout(db: AsyncSession, user_id: UUID, device_id: UUID, section: str,
                      scope: str, template: tuple[str, str] | None,
                      items: list[dict[str, Any]]) -> dict[str, Any]:
    params = {"uid": str(user_id), "section": section, "items": json.dumps(items)}
    if scope == "device":
        sql = """
            INSERT INTO device_widget_layouts (user_id, scope, device_id, section, items, updated_at)
            VALUES (:uid, 'device', :did, :section, CAST(:items AS jsonb), NOW())
            ON CONFLICT (user_id, device_id, section) WHERE scope = 'device'
            DO UPDATE SET items = EXCLUDED.items, updated_at = NOW()
            RETURNING updated_at
        """
        params["did"] = str(device_id)
    else:
        assert template is not None
        sql = """
            INSERT INTO device_widget_layouts (user_id, scope, vendor, device_type, section, items, updated_at)
            VALUES (:uid, 'template', :vendor, :dtype, :section, CAST(:items AS jsonb), NOW())
            ON CONFLICT (user_id, vendor, device_type, section) WHERE scope = 'template'
            DO UPDATE SET items = EXCLUDED.items, updated_at = NOW()
            RETURNING updated_at
        """
        params.update(vendor=template[0], dtype=template[1])
    row = (await db.execute(text(sql), params)).first()
    await db.commit()
    return {"scope": scope, "section": section, "items": items, "updated_at": _iso(row[0]) if row else None}


async def delete_layout(db: AsyncSession, user_id: UUID, device_id: UUID, section: str,
                        scope: str, template: tuple[str, str] | None) -> int:
    if scope == "device":
        result = await db.execute(text("""
            DELETE FROM device_widget_layouts
            WHERE user_id = :uid AND scope = 'device' AND device_id = :did AND section = :section
        """), {"uid": str(user_id), "did": str(device_id), "section": section})
    else:
        assert template is not None
        result = await db.execute(text("""
            DELETE FROM device_widget_layouts
            WHERE user_id = :uid AND scope = 'template' AND vendor = :vendor
              AND device_type = :dtype AND section = :section
        """), {"uid": str(user_id), "vendor": template[0], "dtype": template[1], "section": section})
    await db.commit()
    return result.rowcount or 0
