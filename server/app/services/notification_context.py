"""What every alert notification carries besides its rendered text.

``prepare`` runs once per dispatch (host_alert_service.dispatch_to_channels),
for every family, before anything is sent:

* **Where**: the asset's site and group. ``{site}``, ``{group}`` and
  ``{site_suffix}`` left in the subject, mail body or SMS are filled here, so
  a sender that does not know them never ships a placeholder verbatim, and the
  mail's details table gets "Site" and "Group" rows when they are known.
  The site is the one alert storms and warning grouping use
  (notification_grouping.group_for "site"): the site of the server record or
  of the sensor that polls the asset, else the device's location text. The
  group is the device's primary group (a server's or service check's linked
  device). An asset with neither, or an alert with no asset (NetPath, APM,
  appliance), gets "".
* **When**: ``local_time``, the event time in the organisation's regional
  settings, for the chat and webhook payloads (notification_payload).

Never raises: a send is never lost because its site could not be read.
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_phrasing as ap

logger = logging.getLogger(__name__)

# The text fields a sender rendered from templates.
_TEXT_FIELDS = ("subject", "body", "message")

# The asset comes from the ctx when the sender names it, else from the alert row.
_PLACE_SQL = """
WITH k AS (
    SELECT CAST(:did AS uuid) AS did, CAST(:sid AS uuid) AS sid,
           CAST(:scid AS uuid) AS scid, CAST(:aid AS uuid) AS aid
)
SELECT si.name AS site_name, d.location AS location, dg.name AS group_name
FROM k
LEFT JOIN alerts a ON a.id = k.aid
LEFT JOIN service_checks sc ON sc.id = COALESCE(k.scid, a.service_check_id)
LEFT JOIN servers sv ON sv.id = COALESCE(k.sid, a.server_id)
LEFT JOIN devices d ON d.id = COALESCE(k.did, a.device_id, sc.device_id, sv.device_id)
LEFT JOIN sensors sen ON sen.id = COALESCE(a.sensor_id, d.default_sensor_id, sc.default_sensor_id)
LEFT JOIN sites si ON si.id = COALESCE(sv.site_id, sen.site_id)
LEFT JOIN device_groups dg ON dg.id = d.group_id
"""


def _id(value) -> Optional[str]:
    """A uuid as text, or None (an id that is not a uuid names nothing)."""
    if value in (None, ""):
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


async def asset_place(db: AsyncSession, ctx: dict) -> dict:
    """{site, group, site_suffix} for the asset a dispatch ctx is about."""
    did, sid, scid, aid = (_id(ctx.get(k)) for k in ("device_id", "server_id", "service_check_id", "alert_id"))
    if not (did or sid or scid or aid):
        return ap.place_variables()
    try:
        async with db.begin_nested():
            row = (await db.execute(text(_PLACE_SQL), {"did": did, "sid": sid, "scid": scid,
                                                        "aid": aid})).first()
    except Exception:
        logger.debug("notification site lookup failed", exc_info=True)
        return ap.place_variables()
    if row is None:
        return ap.place_variables()
    site = (row.site_name or "").strip() or (row.location or "").strip()
    return ap.place_variables(site, row.group_name)


def fill_place(ctx: dict, place: dict) -> dict:
    """``ctx`` with the place placeholders filled and the details rows added."""
    out = dict(ctx)
    for field in _TEXT_FIELDS:
        value = out.get(field)
        if isinstance(value, str) and "{" in value:
            for key in ap.PLACE_KEYS:
                value = value.replace("{" + key + "}", str(place.get(key) or ""))
            out[field] = value
    site, group = place.get("site") or "", place.get("group") or ""
    out.setdefault("site", site)
    out.setdefault("group", group)
    details = list(out.get("details") or [])
    labels = {str(label).strip().lower(): str(v or "").strip() for label, v, *_ in details} if details else {}
    extra = []
    # A device whose site is its location text already shows it as "Location".
    if site and "site" not in labels and labels.get("location") != site:
        extra.append(("Site", site))
    if group and "group" not in labels and "device group" not in labels:
        extra.append(("Group", group))
    if extra:
        at = 1 if details and str(details[0][0]).strip().lower() == "alert rule" else 0
        out["details"] = details[:at] + extra + details[at:]
    return out


async def prepare(db: AsyncSession, ctx: dict, branding=None) -> dict:
    """The ctx every transport of one dispatch uses (see module docstring)."""
    try:
        out = fill_place(ctx, await asset_place(db, ctx))
    except Exception:
        logger.debug("notification place fill failed", exc_info=True)
        out = dict(ctx)
    if branding is not None and ctx.get("triggered_at") and not out.get("local_time"):
        try:
            from app.services.regional_time import timestamp_label
            out["local_time"] = timestamp_label(ctx.get("triggered_at"), branding)
        except Exception:
            pass
    return out


def local_time(ctx: Optional[dict]) -> str:
    """The event's regional time for a payload ("" when it has none)."""
    c = ctx or {}
    if c.get("local_time"):
        return str(c["local_time"])
    if not c.get("triggered_at"):
        return ""
    try:
        from app.services.branding import Branding
        from app.services.regional_time import timestamp_label
        return timestamp_label(c["triggered_at"], Branding())
    except Exception:
        return str(c["triggered_at"])
