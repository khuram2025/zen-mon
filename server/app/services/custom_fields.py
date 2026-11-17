"""Typed custom fields on devices and servers (migrate-192, docs/device-groups.md).

A field has a key (immutable), a label, a type (text, number, date, choice,
boolean; immutable) and the asset kinds it applies to. Values are stored in the
column of their type. Fields are facts for dynamic groups (``custom.<key>``),
optional columns on the Devices list, and alert message placeholders
(``{custom.<key>}``). A field used by a dynamic group cannot be deleted.
"""
from __future__ import annotations

import csv
import io
import ipaddress
import re
from datetime import date
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
TYPES = ("text", "number", "date", "choice", "boolean")
MAX_TEXT = 1000
MAX_IMPORT_ROWS = 20000
_TRUE = {"true", "yes", "y", "1", "on"}
_FALSE = {"false", "no", "n", "0", "off"}


def coerce(defn: dict, raw: Any) -> dict:
    """{column: value} for one value of a field, or 422 naming the problem. None/"" clears."""
    label = defn["label"]
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return {}
    t = defn["field_type"]
    if t == "text":
        v = str(raw).strip()
        if len(v) > MAX_TEXT:
            raise HTTPException(422, f"{label}: at most {MAX_TEXT} characters")
        return {"value_text": v}
    if t == "number":
        try:
            return {"value_num": float(str(raw).strip())}
        except ValueError:
            raise HTTPException(422, f"{label}: {raw!r} is not a number") from None
    if t == "date":
        try:
            return {"value_date": date.fromisoformat(str(raw).strip()[:10])}
        except ValueError:
            raise HTTPException(422, f"{label}: {raw!r} is not a date (YYYY-MM-DD)") from None
    if t == "boolean":
        if isinstance(raw, bool):
            return {"value_bool": raw}
        v = str(raw).strip().lower()
        if v in _TRUE:
            return {"value_bool": True}
        if v in _FALSE:
            return {"value_bool": False}
        raise HTTPException(422, f"{label}: {raw!r} is not yes or no")
    if t == "choice":
        choices = [str(c) for c in defn.get("choices") or []]
        match = next((c for c in choices if c.lower() == str(raw).strip().lower()), None)
        if match is None:
            raise HTTPException(422, f"{label}: {raw!r} is not one of {', '.join(choices)}")
        return {"value_text": match}
    raise HTTPException(422, f"{label}: unknown field type")


def display(row: dict) -> Any:
    for k in ("value_text", "value_num", "value_bool"):
        if row.get(k) is not None:
            v = row[k]
            if k == "value_num" and float(v).is_integer():
                return int(v)
            return v
    if row.get("value_date") is not None:
        return row["value_date"].isoformat()
    return None


async def defs(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(text("""
        SELECT d.id::text AS id, d.key, d.label, d.field_type, d.choices, d.asset_kinds, d.description, d.sort_order,
               d.show_in_list, d.created_at, d.updated_at,
               (SELECT count(*) FROM custom_field_values v WHERE v.field_id = d.id) AS value_count
          FROM custom_field_defs d ORDER BY d.sort_order, d.label
    """))).mappings().all()
    return [dict(r) for r in rows]


async def def_by_key(db: AsyncSession, key: str) -> Optional[dict]:
    return next((d for d in await defs(db) if d["key"] == key), None)


def validate_def(body: dict, *, existing: Optional[dict] = None) -> dict:
    out: dict[str, Any] = {}
    if existing is None:
        key = (body.get("key") or "").strip()
        if not KEY_RE.match(key):
            raise HTTPException(422, "Key: lowercase letters, digits and _, starting with a letter (max 50)")
        if body.get("field_type") not in TYPES:
            raise HTTPException(422, f"Type must be one of {', '.join(TYPES)}")
        out["key"], out["field_type"] = key, body["field_type"]
    ftype = existing["field_type"] if existing else out["field_type"]
    if "label" in body or existing is None:
        label = (body.get("label") or "").strip()
        if not 1 <= len(label) <= 80:
            raise HTTPException(422, "Label: 1 to 80 characters")
        out["label"] = label
    if ftype == "choice" and ("choices" in body or existing is None):
        choices = [str(c).strip() for c in (body.get("choices") or []) if str(c).strip()]
        if not choices or len(choices) > 100 or len(set(c.lower() for c in choices)) != len(choices):
            raise HTTPException(422, "Choices: 1 to 100 distinct values")
        if existing:
            gone = [c for c in existing.get("choices") or [] if c not in choices]
            if gone:
                raise HTTPException(422, f"Choices in use cannot be removed: {', '.join(gone)}")
        out["choices"] = choices
    if "asset_kinds" in body or existing is None:
        kinds = sorted(set(body.get("asset_kinds") or ["device", "server"]))
        if not kinds or any(k not in ("device", "server") for k in kinds):
            raise HTTPException(422, "Applies to: device and/or server")
        out["asset_kinds"] = kinds
    for k in ("description",):
        if k in body:
            out[k] = (body.get(k) or "").strip()[:500] or None
    if "sort_order" in body:
        out["sort_order"] = int(body.get("sort_order") or 100)
    if "show_in_list" in body:
        out["show_in_list"] = bool(body.get("show_in_list"))
    return out


async def values_for(db: AsyncSession, kind: str, asset_id: str) -> dict:
    rows = (await db.execute(text("""
        SELECT d.key, v.value_text, v.value_num, v.value_date, v.value_bool
          FROM custom_field_values v JOIN custom_field_defs d ON d.id = v.field_id
         WHERE v.asset_kind = :k AND v.asset_id = CAST(:id AS uuid)
    """), {"k": kind, "id": str(asset_id)})).mappings().all()
    return {r["key"]: display(dict(r)) for r in rows}


async def set_values(db: AsyncSession, kind: str, asset_id: str, values: dict, user_id) -> dict:
    """Write {key: value|None}; unknown keys and kinds a field does not apply to are refused."""
    by_key = {d["key"]: d for d in await defs(db)}
    for key, raw in values.items():
        d = by_key.get(key)
        if d is None:
            raise HTTPException(422, f"Unknown custom field: {key}")
        if kind not in d["asset_kinds"]:
            raise HTTPException(422, f"{d['label']} does not apply to {kind}s")
        cols = coerce(d, raw)
        if not cols:
            await db.execute(text("DELETE FROM custom_field_values WHERE field_id = CAST(:f AS uuid) "
                                  "AND asset_kind = :k AND asset_id = CAST(:id AS uuid)"),
                             {"f": d["id"], "k": kind, "id": str(asset_id)})
            continue
        full = {"value_text": None, "value_num": None, "value_date": None, "value_bool": None, **cols}
        await db.execute(text("""
            INSERT INTO custom_field_values (field_id, asset_kind, asset_id, value_text, value_num, value_date, value_bool,
                                             updated_by, updated_at)
            VALUES (CAST(:f AS uuid), :k, CAST(:id AS uuid), :t, :n, :d, :b, (SELECT id FROM users WHERE id = :u), now())
            ON CONFLICT (field_id, asset_kind, asset_id) DO UPDATE SET
                value_text = EXCLUDED.value_text, value_num = EXCLUDED.value_num, value_date = EXCLUDED.value_date,
                value_bool = EXCLUDED.value_bool, updated_by = EXCLUDED.updated_by, updated_at = now()
        """), {"f": d["id"], "k": kind, "id": str(asset_id), "t": full["value_text"], "n": full["value_num"],
               "d": full["value_date"], "b": full["value_bool"], "u": user_id})
    return await values_for(db, kind, asset_id)


async def list_values(db: AsyncSession, kind: str, keys: Optional[list[str]] = None) -> dict[str, dict]:
    """{asset_id: {key: value}} for the Devices list columns."""
    sql = """SELECT v.asset_id::text AS asset_id, d.key, v.value_text, v.value_num, v.value_date, v.value_bool
               FROM custom_field_values v JOIN custom_field_defs d ON d.id = v.field_id
              WHERE v.asset_kind = :k"""
    params: dict = {"k": kind}
    if keys:
        sql += " AND d.key = ANY(CAST(:keys AS text[]))"
        params["keys"] = keys
    out: dict[str, dict] = {}
    for r in (await db.execute(text(sql), params)).mappings().all():
        out.setdefault(r["asset_id"], {})[r["key"]] = display(dict(r))
    return out


async def placeholders(db: AsyncSession, kind: str, asset_id: Any) -> dict:
    """{"custom.<key>": value} for alert message templates; every field present ("" when unset).
    Never raises: a message is never lost because custom fields could not be read."""
    if not asset_id:
        return {}
    try:
        async with db.begin_nested():
            keys = (await db.execute(text("SELECT key FROM custom_field_defs WHERE :k = ANY(asset_kinds)"),
                                     {"k": kind})).scalars().all()
            vals = await values_for(db, kind, str(asset_id))
    except Exception:
        return {}
    return {f"custom.{k}": ("" if vals.get(k) is None else vals[k]) for k in keys}


async def plan_import(db: AsyncSession, csv_text: str) -> dict:
    """Parse a CSV of values. The first column names the asset: ip_address, hostname (devices) or
    server (server display name or hostname); every other column is a field key. A blank cell
    leaves that value as it is. Returns what would be written and the rows that could not be
    matched, without writing."""
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    if not headers:
        raise HTTPException(422, "The CSV has no header row")
    ident = headers[0].lower()
    if ident not in ("ip_address", "hostname", "server"):
        raise HTTPException(422, "The first column must be ip_address, hostname or server")
    by_key = {d["key"]: d for d in await defs(db)}
    unknown = [h for h in headers[1:] if h not in by_key]
    if unknown:
        raise HTTPException(422, f"Unknown custom field column(s): {', '.join(unknown)}")
    devices = (await db.execute(text("SELECT id::text, hostname, host(ip_address) AS ip FROM devices"))).mappings().all()
    servers = (await db.execute(text("SELECT id::text, display_name, hostname, host(primary_ip) AS ip FROM servers"))).mappings().all()
    by_ip = {r["ip"]: ("device", r["id"]) for r in devices if r["ip"]}
    by_host = {(r["hostname"] or "").lower(): ("device", r["id"]) for r in devices if r["hostname"]}
    by_server = {}
    for r in servers:
        for n in (r["display_name"], r["hostname"]):
            if n:
                by_server[n.lower()] = ("server", r["id"])
    rows, problems = [], []
    for n, raw in enumerate(reader, start=2):
        if n - 1 > MAX_IMPORT_ROWS:
            raise HTTPException(422, f"At most {MAX_IMPORT_ROWS} rows per import")
        raw = {(k or "").strip(): (v or "") for k, v in raw.items()}
        key = raw.get(headers[0], "").strip()
        if not key:
            continue
        if ident == "ip_address":
            try:
                key = str(ipaddress.ip_address(key))
            except ValueError:
                problems.append({"row": n, "error": f"{key!r} is not an IP address"})
                continue
            asset = by_ip.get(key)
        elif ident == "hostname":
            asset = by_host.get(key.lower())
        else:
            asset = by_server.get(key.lower())
        if asset is None:
            problems.append({"row": n, "error": f"No {('server' if ident == 'server' else 'device')} {key!r}"})
            continue
        vals = {}
        try:
            for h in headers[1:]:
                d = by_key[h]
                if asset[0] not in d["asset_kinds"]:
                    raise HTTPException(422, f"{d['label']} does not apply to {asset[0]}s")
                cell = (raw.get(h) or "").strip()
                if not cell:          # a blank cell leaves the current value alone
                    continue
                coerce(d, cell)
                vals[h] = cell
        except HTTPException as exc:
            problems.append({"row": n, "error": str(exc.detail)})
            continue
        rows.append({"row": n, "asset_kind": asset[0], "asset_id": asset[1], "match": key, "values": vals})
    return {"columns": headers[1:], "rows": rows, "problems": problems}
