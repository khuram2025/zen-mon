"""Template families: a template and the templates customised from it (overlays).

An alert rule scoped to a template (``alert_rules.profile_id``) covers devices
whose template is that one or customised from it, at any depth. A customised
template whose ``interface_roles`` / ``rule_pack`` is NULL uses its parent's.
Loaded once per evaluator pass.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_MAX_DEPTH = 10


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


class Family:
    def __init__(self, rows: dict[str, dict]):
        self.rows = rows

    def ancestors(self, pid: Any) -> list[str]:
        """[pid, its parent, ...]; [] for no template."""
        out: list[str] = []
        cur = str(pid) if pid else None
        while cur and cur not in out and len(out) <= _MAX_DEPTH:
            out.append(cur)
            cur = (self.rows.get(cur) or {}).get("parent_id")
        return out

    def covers(self, rule_profile_id: Any, device_profile_id: Any) -> bool:
        return bool(rule_profile_id) and str(rule_profile_id) in self.ancestors(device_profile_id)

    def _inherited(self, pid: Any, field: str) -> Any:
        for p in self.ancestors(pid):
            value = (self.rows.get(p) or {}).get(field)
            if value is not None:
                return value
        return None

    def roles(self, pid: Any) -> Optional[list[dict]]:
        return self._inherited(pid, "interface_roles")

    def pack(self, pid: Any) -> Optional[list[dict]]:
        return self._inherited(pid, "rule_pack")


EMPTY = Family({})


async def load(db: AsyncSession) -> Family:
    """Every template's parent, roles and pack. Before migrate-193 roles/packs are None."""
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT id::text, parent_id::text, interface_roles, rule_pack FROM device_profiles"))).all()
    except Exception:
        try:
            async with db.begin_nested():
                rows = [(r[0], r[1], None, None) for r in (await db.execute(text(
                    "SELECT id::text, parent_id::text FROM device_profiles"))).all()]
        except Exception:
            logger.debug("template family unavailable", exc_info=True)
            return EMPTY
    return Family({r[0]: {"parent_id": r[1], "interface_roles": _json(r[2]), "rule_pack": _json(r[3])}
                   for r in rows})


async def descendants(db: AsyncSession, pid: Any) -> list[str]:
    """pid and every template customised from it (for counts and SQL scope)."""
    fam = await load(db)
    return [p for p in fam.rows if str(pid) in fam.ancestors(p)]
