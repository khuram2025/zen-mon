"""Governed NetFlow saved views (NF-05, NF-12, NF-34, FE-10).

A saved view is a named, versioned query (contract §8):

    {v: 2, kind: 'forensics'|'overview'|'section', filter: {<FlowFilter keys>},
     window: {mode: 'relative', range} | {mode: 'fixed', from, to},
     sort?, order?, columns?, path?}

``path`` (optional, additive to the contract shape) names the page an overview
or section view reopens, e.g. ``/netflow/top-talkers`` or an exporter
deep-dive ``/netflow/devices/192.0.2.1``. ``params`` (optional, additive)
holds the page settings that change what such a page ranks or shows --
``by`` (sent / received / total), ``side`` (source / destination), ``resolve``,
``col`` and ``dir`` -- so a "top receivers" view never reopens as "top
senders" (FE-10); every key and value is validated. Every stored query is validated with
the same filter parser the flow endpoints use, so a saved view can never hold a
filter the API would reject. Legacy (v1) rows -- the old flat forensics blob
``{src, dst, proto, src_port, …, sort, order}`` with string values -- are
upgraded on read; conditions that cannot be restored are listed in
``query_issues`` and ``legacy_time`` says the original period was never saved.

Access (contract §6):

* ``visibility`` is ``private`` (the owner only) or ``shared`` (every NetFlow
  viewer). A private view of someone else is a 404 -- its existence is not
  confirmed -- except to administrators.
* The owner edits and deletes their views. ``netflow.manage`` (and
  administrators) moderate shared views: rename, re-describe, pin, re-query or
  delete them. Only the owner (or an administrator) changes who can see a
  view. A view whose owner was deleted is editable only by managers.
* For a user restricted to some exporters, shared views whose filter names an
  exporter outside that scope are left out of the list and are 404 by id, so
  the filters of other teams' investigations are not revealed. A shared view
  whose filter names hosts (talker, src, dst and their exclusions) is data
  about the owner's slice of the traffic: a restricted reader sees it only
  when the filter also pins an exporter the reader can see, or when the
  owner is restricted to tags the reader also holds (the owner sees no more
  than the reader). Views by unrestricted owners that name hosts without an
  exporter stay with unrestricted readers.
* The list is filtered and paged in SQL, so its cost does not grow with the
  number of views other users keep; each user may own at most
  ``MAX_VIEWS_PER_OWNER`` views.
* Request bodies are limited to ``MAX_BODY_BYTES`` while they are read
  (Content-Length or chunked), before any JSON is parsed.
* Every write is recorded in the audit log.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.netflow_saved_view import NetflowSavedView
from app.models.user import User
from app.services import netflow_classify, netflow_groups
from app.services.audit_service import write_audit_log

from .access import MANAGE_PERMISSION, MANAGE_SHARED_PERMISSION, FlowVisibility, flow_visibility, require_manage
from .common import NetflowError, NetflowRoute, current_meta, invalid_filter
from .filters import FILTER_ALIASES, FLOW_FILTER_KEYS, FlowFilter, parse_flow_filter

MAX_BODY_BYTES = 32 * 1024
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})


def _too_large() -> JSONResponse:
    return JSONResponse({"detail": "The request body is too large for a saved view"}, status_code=413)


class BoundedBodyRoute(NetflowRoute):
    """NetflowRoute that refuses a write body over ``MAX_BODY_BYTES`` while
    reading it -- a declared Content-Length, or the running total of a
    chunked body -- so an oversized body is never buffered whole or parsed
    (X-17). FastAPI reads the body before any dependency runs, so a
    Content-Length check in a dependency neither saw chunked bodies nor
    spared the parse."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded_handler(request: Request):
            if request.method in _WRITE_METHODS:
                length = request.headers.get("content-length")
                if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
                    return _too_large()
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        return _too_large()
                request._body = bytes(body)   # what FastAPI's body reader returns
            return await handler(request)

        return bounded_handler


router = APIRouter(route_class=BoundedBodyRoute)

ADMIN_PERMISSION = "system.admin"
KINDS = ("forensics", "overview", "section")
VISIBILITIES = ("private", "shared")
RELATIVE_RANGES = ("1h", "24h", "7d", "1M", "month", "last_month", "quarter")
# v1 blobs sometimes carried a rolling window in hours.
_HOURS_TO_RANGE = {1: "1h", 24: "24h", 168: "7d", 720: "1M"}
_NUMERIC_KEYS = frozenset({
    "protocol", "port", "src_port", "dst_port", "service_port", "dscp", "hour", "dow", "asn",
    "ip_version", "min_bytes", "max_bytes",
})
_SORT_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
# Page settings a section/overview view may store (query.params): key -> the
# values it accepts (a compiled pattern or a tuple of literals).
PAGE_PARAMS: dict[str, Any] = {
    "by": ("src", "dst", "total"),
    "side": ("src", "dst"),
    "resolve": ("1",),
    "col": _SORT_RE,
    "dir": ("asc", "desc"),
}
_PATH_RE = re.compile(r"^/netflow(?:/[A-Za-z0-9._~:%@-]{1,80}){0,3}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_QUERY_BYTES = 16 * 1024
MAX_COLUMNS = 60
LIST_LIMIT_MAX = 200
MAX_VIEWS_PER_OWNER = 250
# Filter keys that name hosts (data about someone's traffic slice).
HOST_KEYS = ("talker", "src", "dst", "not_talker", "not_src", "not_dst")


# ── Saved query schema ────────────────────────────────────────────────────────

def _issue(key: str, value: Any, reason: str) -> dict:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return {"key": str(key)[:64], "value": text[:200], "reason": reason}


def _value_text(v: Any) -> str | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, list) and v and all(isinstance(x, (int, str)) and not isinstance(x, bool) for x in v):
        return ",".join(str(x).strip() for x in v)
    return None


def typed_filter(f: FlowFilter) -> dict:
    """A FlowFilter as the JSON the UI's ``FlowFilter`` type uses: numbers as
    numbers, ``dst_ports`` as a list, everything else as its canonical text."""
    out: dict[str, Any] = {}
    for key, text in f.canonical().items():
        if key in _NUMERIC_KEYS:
            out[key] = int(text)
        elif key == "dst_ports":
            out[key] = [int(p) for p in text.split(",")]
        else:
            out[key] = text
    return out


def _parse_filter(raw: Any, issues: list[dict] | None) -> FlowFilter:
    """Validate a stored filter object. With ``issues`` (reading), bad keys are
    reported and dropped; without (writing), any problem is a 400."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        if issues is None:
            raise invalid_filter("query.filter must be an object", {"query.filter": "Not an object"})
        issues.append(_issue("filter", raw, "not an object"))
        return FlowFilter()
    known = set(FLOW_FILTER_KEYS) | set(FILTER_ALIASES)
    values: dict[str, str] = {}
    for k, v in raw.items():
        if v is None or v == "" or v == []:
            continue
        if k not in known:
            if issues is None:
                raise invalid_filter(f"Unknown filter key '{str(k)[:40]}'", {f"query.filter.{str(k)[:40]}": "Unknown key"})
            issues.append(_issue(k, v, "not a NetFlow filter"))
            continue
        text = _value_text(v)
        if text is None:
            if issues is None:
                raise invalid_filter(f"Invalid value for filter '{k}'", {f"query.filter.{k}": "Invalid value"})
            issues.append(_issue(k, v, "not a valid value"))
            continue
        canon = FILTER_ALIASES.get(k, k)
        if canon != k and canon in raw:        # the canonical key wins over its alias
            continue
        values[canon] = text
    if issues is None:
        return parse_flow_filter(values)
    # Reading: keep what still parses, report the rest (a renamed IP group or
    # a removed application rule must not make the whole view unusable).
    try:
        return parse_flow_filter(values)
    except NetflowError as exc:
        fields = exc.fields or {}
        kept = {k: v for k, v in values.items() if k not in fields}
        for key, message in fields.items():
            if key in values:
                issues.append(_issue(key, values[key], message))
    try:
        return parse_flow_filter(kept)
    except NetflowError:
        for key, value in kept.items():
            issues.append(_issue(key, value, "could not be restored"))
        return FlowFilter()


def _parse_iso(value: Any, key: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be an ISO 8601 timestamp")
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if not 2000 <= dt.year <= datetime.now(timezone.utc).year + 1:
        raise ValueError(f"{key} is out of range")
    return dt


def _parse_window(raw: Any) -> dict:
    """``{'mode': 'relative', 'range'}`` or ``{'mode': 'fixed', 'from', 'to'}`` (UTC, Z)."""
    if not isinstance(raw, dict):
        raise ValueError("window must be an object")
    mode = raw.get("mode")
    if mode == "relative":
        if set(raw) - {"mode", "range"}:
            raise ValueError("a relative window has only mode and range")
        if raw.get("range") not in RELATIVE_RANGES:
            raise ValueError(f"window.range must be one of: {', '.join(RELATIVE_RANGES)}")
        return {"mode": "relative", "range": raw["range"]}
    if mode == "fixed":
        if set(raw) - {"mode", "from", "to"}:
            raise ValueError("a fixed window has only mode, from and to")
        start, end = _parse_iso(raw.get("from"), "window.from"), _parse_iso(raw.get("to"), "window.to")
        if end <= start:
            raise ValueError("window.to must be after window.from")
        iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"  # noqa: E731
        return {"mode": "fixed", "from": iso(start), "to": iso(end)}
    raise ValueError("window.mode must be relative or fixed")


def _v1_window(raw: dict) -> dict | None:
    """The window a v1 blob implies, or None when it never saved one."""
    if raw.get("time") is not None or raw.get("window") is not None:
        return _parse_window(raw.get("time") if raw.get("time") is not None else raw.get("window"))
    rng = raw.get("range")
    if rng == "custom" or (raw.get("from") and raw.get("to")):
        return _parse_window({"mode": "fixed", "from": raw.get("from"), "to": raw.get("to")})
    if rng:
        return _parse_window({"mode": "relative", "range": rng})
    if raw.get("hours") is not None:
        hours = raw.get("hours")
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            raise ValueError("hours must be a whole number") from None
        if hours not in _HOURS_TO_RANGE:
            raise ValueError("hours must be 1, 24, 168 or 720")
        return {"mode": "relative", "range": _HOURS_TO_RANGE[hours]}
    return None


_V1_WINDOW_KEYS = frozenset({"time", "window", "range", "from", "to", "hours"})
_V1_META_KEYS = frozenset({"sort", "order", "columns", "kind", "v", "path"})


def normalize_query(raw: Any, *, kind: str = "forensics", strict: bool) -> tuple[dict, list[dict], bool, int]:
    """Any stored or submitted query -> ``(v2 query, issues, legacy_time, source_version)``.

    ``strict`` (writes): every problem is a 400 whose ``fields`` name the key.
    Not strict (reads): what can be restored is restored and the rest listed
    in ``issues``; a view never disappears because one condition went stale.
    """
    issues: list[dict] | None = None if strict else []

    def fail(key: str, message: str):
        if strict:
            raise invalid_filter(message, {key: message})
        issues.append(_issue(key, "", message))

    if not isinstance(raw, dict):
        if strict:
            raise invalid_filter("query must be an object", {"query": "Not an object"})
        return ({"v": 2, "kind": kind if kind in KINDS else "forensics", "filter": {},
                 "window": {"mode": "relative", "range": "1h"}}, [_issue("query", raw, "not an object")], True, 0)

    version = 2 if raw.get("v") == 2 else 1
    out: dict[str, Any] = {"v": 2}
    q_kind = raw.get("kind", kind)
    if q_kind not in KINDS:
        fail("query.kind", f"kind must be one of: {', '.join(KINDS)}")
        q_kind = kind if kind in KINDS else "forensics"
    out["kind"] = q_kind

    legacy_time = False
    if version == 2:
        allowed = {"v", "kind", "filter", "window", "sort", "order", "columns", "path", "params"}
        extra = set(raw) - allowed
        for key in sorted(extra):
            fail(f"query.{key}", f"Unknown query field '{str(key)[:40]}'")
        f = _parse_filter(raw.get("filter"), issues)
        try:
            out_window = _parse_window(raw.get("window"))
        except ValueError as exc:
            fail("query.window", str(exc))
            out_window, legacy_time = {"mode": "relative", "range": "1h"}, True
    else:
        flat = {k: v for k, v in raw.items() if k not in _V1_WINDOW_KEYS | _V1_META_KEYS}
        source = raw.get("filters") if isinstance(raw.get("filters"), dict) else flat
        if source is not flat:
            for key in sorted(set(flat) - {"filters"}):
                fail(f"query.{key}", f"Unknown query field '{str(key)[:40]}'")
        f = _parse_filter(source, issues)
        try:
            out_window = _v1_window(raw)
        except ValueError as exc:
            fail("query.window", str(exc))
            out_window = None
        if out_window is None:
            out_window, legacy_time = {"mode": "relative", "range": "1h"}, True
    out["filter"] = typed_filter(f)
    out["window"] = out_window

    sort = raw.get("sort")
    if sort not in (None, ""):
        if isinstance(sort, str) and _SORT_RE.match(sort):
            out["sort"] = sort
        else:
            fail("query.sort", "sort must be a column name")
    order = raw.get("order")
    if order not in (None, ""):
        if order in ("asc", "desc"):
            out["order"] = order
        else:
            fail("query.order", "order must be asc or desc")
    columns = raw.get("columns")
    if columns not in (None, [], ""):
        if isinstance(columns, str):
            columns = [c for c in columns.split(",") if c]
        if (isinstance(columns, list) and len(columns) <= MAX_COLUMNS
                and all(isinstance(c, str) and _SORT_RE.match(c) for c in columns)):
            out["columns"] = list(dict.fromkeys(columns))
        else:
            fail("query.columns", f"columns must be a list of at most {MAX_COLUMNS} column names")
    path = raw.get("path")
    if path not in (None, ""):
        if isinstance(path, str) and _PATH_RE.match(path) and ".." not in path:
            out["path"] = path
        else:
            fail("query.path", "path must be a NetFlow page path such as /netflow/top-talkers")
    page_params = raw.get("params") if version == 2 else None
    if page_params not in (None, {}):
        if not isinstance(page_params, dict):
            fail("query.params", "params must be an object of page settings")
        else:
            kept: dict[str, str] = {}
            for key, value in page_params.items():
                rule = PAGE_PARAMS.get(key)
                text_ = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None
                ok = rule is not None and text_ is not None and (
                    text_ in rule if isinstance(rule, tuple) else bool(rule.match(text_)))
                if ok:
                    kept[key] = text_
                elif rule is None:
                    fail(f"query.params.{str(key)[:40]}",
                         f"Unknown page setting '{str(key)[:40]}' (expected one of: {', '.join(PAGE_PARAMS)})")
                else:
                    fail(f"query.params.{key}", f"Invalid value for page setting '{key}'")
            if kept:
                out["params"] = dict(sorted(kept.items()))
    if strict and len(json.dumps(out)) > MAX_QUERY_BYTES:
        raise invalid_filter("The saved query is too large", {"query": "Too large"})
    return out, (issues or []), legacy_time, version


def query_exporters(query: dict) -> list[str]:
    """Exporters a normalized query names (``exporter`` and ``iface=N@exporter``)."""
    flt = query.get("filter") or {}
    out = []
    if flt.get("exporter"):
        out.append(str(flt["exporter"]))
    iface = str(flt.get("iface") or "")
    if "@" in iface:
        out.append(iface.split("@", 1)[1])
    return out


# ── Access ────────────────────────────────────────────────────────────────────

def _is_admin(vis: FlowVisibility) -> bool:
    return ADMIN_PERMISSION in vis.permissions


def _is_owner(view: NetflowSavedView, vis: FlowVisibility) -> bool:
    return view.owner_id is not None and vis.user_id is not None and str(view.owner_id) == vis.user_id


def query_names_hosts(query: dict) -> bool:
    """Whether a normalized query's filter names hosts (HOST_KEYS)."""
    flt = query.get("filter") or {}
    return any(flt.get(k) not in (None, "", []) for k in HOST_KEYS)


def can_read(view: NetflowSavedView, vis: FlowVisibility, query: dict | None = None, *,
             owner_scope: frozenset[str] | None = None) -> bool:
    """Shared views are readable by every NetFlow viewer whose exporter scope
    covers the exporters the view names; private views by their owner (and
    administrators).

    A restricted reader additionally needs, for a shared view that names
    hosts, either an exporter pinned in the filter (already checked to be
    visible) or an owner restricted to tags the reader also holds
    (``owner_scope``: the owner's lowercased scope tags, None when the owner
    is unrestricted, gone or unknown -- fail closed). The SQL form of this
    rule is ``_readable_sql``."""
    if _is_owner(view, vis) or _is_admin(vis):
        return True
    if view.visibility != "shared":
        return False
    if vis.exporters is not None:
        q = query if query is not None else _normalized(view)[0]
        named = query_exporters(q)
        for exporter in named:
            if not vis.allows(exporter):
                return False
        if query_names_hosts(q) and not named:
            if not owner_scope or vis.scope is None or not owner_scope <= vis.scope:
                return False
    return True


SHARE_DENIED = "Sharing a view with every user needs the 'Manage shared views' permission"
EDIT_DENIED = ("Private views can only be changed by their owner; shared views need the "
               "'Manage shared views' permission")


def can_edit(view: NetflowSavedView, vis: FlowVisibility) -> bool:
    """A private view: its owner. A shared (or orphaned) view: only a holder of
    netflow.manage_shared (the released 1.23.24 rule), whoever created it, so
    losing that permission also ends the right to change what everyone sees.
    Administrators can edit any view."""
    if _is_admin(vis):
        return True
    if view.visibility == "shared" or view.owner_id is None:
        return vis.can(MANAGE_SHARED_PERMISSION)
    return _is_owner(view, vis)


def can_share(vis: FlowVisibility) -> bool:
    """Creating a shared view, or making one shared."""
    return _is_admin(vis) or vis.can(MANAGE_SHARED_PERMISSION)


def can_change_visibility(view: NetflowSavedView, vis: FlowVisibility) -> bool:
    return can_edit(view, vis) and (_is_admin(vis) or _is_owner(view, vis) or view.owner_id is None)


def _normalized(view: NetflowSavedView) -> tuple[dict, list[dict], bool]:
    q, issues, legacy_time, _v = normalize_query(view.query, kind=view.kind or "forensics", strict=False)
    return q, issues, legacy_time


def _filter_json(alias: str) -> str:
    """The filter object of a stored query (JSONB): v2 keeps it under
    "filter"; v1 blobs are flat, or nest it under "filters". The column is
    JSON, so it is cast once here."""
    q = f"CAST({alias}.query AS jsonb)"
    return (f"(CASE WHEN {q}->>'v' = '2' THEN {q}->'filter' "
            f"WHEN jsonb_typeof({q}->'filters') = 'object' THEN {q}->'filters' "
            f"ELSE {q} END)")


async def _admin_roles(db: AsyncSession) -> list[str]:
    """Role names that carry system.admin (their holders are never scoped)."""
    try:
        async with db.begin_nested():
            rows = (await db.execute(text("SELECT name FROM roles WHERE permissions ? :p"),
                                     {"p": ADMIN_PERMISSION})).scalars().all()
    except Exception:
        rows = []
    return sorted(set(rows) | {"admin"})


def _readable_sql(vis: FlowVisibility, params: dict, alias: str = "v") -> str:
    """SQL predicate on ``netflow_saved_views <alias>``: a shared view a
    restricted reader may open (``can_read`` in SQL, so the list is filtered
    and paged in the database). The stored filter is extracted once per row;
    stored values that are not canonical fail closed."""
    params.update(sv_vis=sorted(vis.exporters or ()), sv_hosts=list(HOST_KEYS),
                  sv_scope=sorted(vis.scope or ()))
    f = "_svf.f"
    exporter = f"COALESCE({f}->>'exporter', '')"
    iface = f"COALESCE({f}->>'iface', '')"
    pinned = f"({exporter} <> '' OR position('@' in {iface}) > 0)"
    names_hosts = (f"EXISTS (SELECT 1 FROM jsonb_each({f}) kv "
                   "WHERE kv.key = ANY(CAST(:sv_hosts AS text[])) "
                   "AND kv.value NOT IN ('null'::jsonb, '\"\"'::jsonb, '[]'::jsonb))")
    owner_within = (
        f"EXISTS (SELECT 1 FROM users ou WHERE ou.id = {alias}.owner_id "
        "AND jsonb_typeof(ou.scope_tags) = 'array' AND jsonb_array_length(ou.scope_tags) > 0 "
        "AND ou.role <> ALL(CAST(:sv_admin_roles AS text[])) "
        "AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(ou.scope_tags) t "
        "WHERE btrim(t) <> '' AND lower(btrim(t)) <> ALL(CAST(:sv_scope AS text[]))))"
    )
    return (
        f"({alias}.is_shared AND EXISTS (SELECT 1 FROM (SELECT {_filter_json(alias)} AS f) _svf "
        f"WHERE jsonb_typeof({f}) <> 'object' OR ("
        f"({exporter} = '' OR {exporter} = ANY(CAST(:sv_vis AS text[]))) "
        f"AND (position('@' in {iface}) = 0 OR split_part({iface}, '@', 2) = ANY(CAST(:sv_vis AS text[]))) "
        f"AND (NOT {names_hosts} OR {pinned} OR {owner_within}))))"
    )


async def _owner_scopes(db: AsyncSession, views: list[NetflowSavedView]) -> dict[str, frozenset[str] | None]:
    """{owner id: lowercased scope tags, or None when unrestricted}."""
    ids = sorted({str(v.owner_id) for v in views if v.owner_id})
    if not ids:
        return {}
    admin_roles = set(await _admin_roles(db))
    rows = (await db.execute(text("SELECT id::text, role, scope_tags FROM users WHERE id = ANY(CAST(:ids AS uuid[]))"),
                             {"ids": ids})).all()
    from app.services.tag_service import tag_set

    out: dict[str, frozenset[str] | None] = {}
    for r in rows:
        tags = frozenset(tag_set(r.scope_tags))
        out[r.id] = None if (not tags or r.role in admin_roles) else tags
    return out


async def _readable(db: AsyncSession, view: NetflowSavedView, vis: FlowVisibility) -> bool:
    owner_scope = None
    if vis.exporters is not None and view.owner_id:
        owner_scope = (await _owner_scopes(db, [view])).get(str(view.owner_id))
    return can_read(view, vis, owner_scope=owner_scope)


# ── Models ────────────────────────────────────────────────────────────────────

def _clean_name(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise ValueError("Enter a name for the view")
    if len(v) > 120:
        raise ValueError("The name must be 120 characters or fewer")
    if _CONTROL_RE.search(v):
        raise ValueError("The name cannot contain control characters")
    return v


def _clean_description(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if len(v) > 500:
        raise ValueError("The description must be 500 characters or fewer")
    return v or None


class SavedViewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., max_length=400)
    description: str | None = Field(None, max_length=2000)
    query: dict
    pinned: bool = False
    visibility: Literal["private", "shared"] = "private"
    kind: Literal["forensics", "overview", "section"] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _clean_name(v)

    @field_validator("description")
    @classmethod
    def _desc(cls, v):
        return _clean_description(v)


class SavedViewPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, max_length=400)
    description: str | None = Field(None, max_length=2000)
    query: dict | None = None
    pinned: bool | None = None
    visibility: Literal["private", "shared"] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return None if v is None else _clean_name(v)

    @field_validator("description")
    @classmethod
    def _desc(cls, v):
        return _clean_description(v)


class DuplicateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, max_length=400)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return None if v is None else _clean_name(v)


async def _body_limit(request: Request) -> None:
    """Refuse oversized bodies (X-17): a saved view is a few hundred bytes.
    ``BoundedBodyRoute`` enforces the limit while the body is read; this
    dependency only keeps the declared-length answer explicit."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="The request body is too large for a saved view")


async def _check_owner_quota(db: AsyncSession, user: User) -> None:
    """At most MAX_VIEWS_PER_OWNER views per user (409). Serialized per owner
    with a transaction-scoped advisory lock so parallel creates cannot both
    pass the count."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"nf_saved_views:{user.id}"})
    owned = (await db.execute(text("SELECT count(*) FROM netflow_saved_views WHERE owner_id = :u"),
                              {"u": user.id})).scalar() or 0
    if owned >= MAX_VIEWS_PER_OWNER:
        raise HTTPException(status_code=409, detail=(
            f"You already have {MAX_VIEWS_PER_OWNER} saved views, the most one user can keep; "
            "delete some before saving more"))


async def _load_catalogs() -> None:
    """Current application names and IP groups, so app/group filters validate."""
    await netflow_classify.load_rules()
    await netflow_groups.load_groups()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def serialize(view: NetflowSavedView, vis: FlowVisibility, usernames: dict[str, str]) -> dict:
    query, issues, legacy_time = _normalized(view)
    owner = str(view.owner_id) if view.owner_id else None
    # PATCH/DELETE also need netflow.manage (route guard), so the buttons only
    # appear when the caller can actually use them.
    editable = can_edit(view, vis) and vis.can(MANAGE_PERMISSION)
    return {
        "id": str(view.id),
        "name": view.name,
        "description": view.description,
        "kind": view.kind or query.get("kind") or "forensics",
        "visibility": view.visibility,
        "pinned": bool(view.pinned),
        "query": query,
        "query_version": int(view.query_version or 1),
        "query_issues": issues,
        "legacy_time": legacy_time,
        "owner_id": owner,
        "owner_username": usernames.get(owner) if owner else None,
        "is_owner": _is_owner(view, vis),
        "can_edit": editable,
        "can_delete": editable,
        "can_change_visibility": can_change_visibility(view, vis) and vis.can(MANAGE_PERMISSION),
        "can_share": can_share(vis) and vis.can(MANAGE_PERMISSION),
        "updated_by_username": usernames.get(str(view.updated_by)) if view.updated_by else None,
        "created_at": _iso(view.created_at),
        "updated_at": _iso(view.updated_at),
    }


async def _usernames(db: AsyncSession, views: list[NetflowSavedView]) -> dict[str, str]:
    ids = {v.owner_id for v in views if v.owner_id} | {v.updated_by for v in views if v.updated_by}
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.username).where(User.id.in_(ids)))).all()
    return {str(r.id): r.username for r in rows}


async def _get_readable(db: AsyncSession, view_id: uuid.UUID, vis: FlowVisibility) -> NetflowSavedView:
    view = (await db.execute(select(NetflowSavedView).where(NetflowSavedView.id == view_id))).scalar_one_or_none()
    if view is None or not await _readable(db, view, vis):
        raise HTTPException(status_code=404, detail="Saved view not found")
    return view


def _audit_meta(view: NetflowSavedView, **extra) -> dict:
    return {"name": view.name, "visibility": view.visibility, "kind": view.kind,
            "owner_id": str(view.owner_id) if view.owner_id else None, **extra}


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/saved-views")
async def list_saved_views(
    q: str | None = Query(default=None, max_length=120, description="Search name and description"),
    kind: str | None = Query(default=None, pattern="^(forensics|overview|section)$"),
    visibility: str | None = Query(default=None, pattern="^(private|shared)$"),
    mine: bool = Query(default=False),
    limit: int = Query(default=LIST_LIMIT_MAX, ge=1, le=LIST_LIMIT_MAX),
    offset: int = Query(default=0, ge=0, le=100_000),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Saved views the caller may open: shared ones in their exporter scope,
    their own private ones (administrators: all). Pinned first, then most
    recently updated."""
    await _load_catalogs()
    stmt = select(NetflowSavedView)
    uid = uuid.UUID(vis.user_id) if vis.user_id else None
    if not _is_admin(vis):
        if vis.exporters is None:
            shared = NetflowSavedView.visibility == "shared"
        else:
            # Restricted reader: the scope rules of can_read, in SQL.
            sql_params: dict = {"sv_admin_roles": await _admin_roles(db)}
            shared = text(_readable_sql(vis, sql_params, alias=NetflowSavedView.__tablename__)).bindparams(
                **sql_params)
        stmt = stmt.where(or_(shared, NetflowSavedView.owner_id == uid))
    if mine:
        stmt = stmt.where(NetflowSavedView.owner_id == uid)
    if kind:
        stmt = stmt.where(NetflowSavedView.kind == kind)
    if visibility:
        stmt = stmt.where(NetflowSavedView.visibility == visibility)
    if q and q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(or_(NetflowSavedView.name.ilike(like, escape="\\"),
                              func.coalesce(NetflowSavedView.description, "").ilike(like, escape="\\")))
    # One scan: the page and the total matching count together.
    paged = stmt.add_columns(func.count().over().label("total")).order_by(
        NetflowSavedView.pinned.desc(), NetflowSavedView.updated_at.desc(), NetflowSavedView.id)
    result = (await db.execute(paged.offset(offset).limit(limit))).all()
    rows = [r[0] for r in result]
    if result:
        total = int(result[0][1])
    else:
        total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    if vis.restricted:
        current_meta().restricted = True
        # Defence in depth: the Python rule on the page (normalizes v1 blobs
        # the SQL form judged on their stored spelling).
        owners = await _owner_scopes(db, rows)
        rows = [v for v in rows if can_read(v, vis, owner_scope=owners.get(str(v.owner_id)))]
    current_meta().set_truncated(limit, len(rows), more=total > offset + limit)
    usernames = await _usernames(db, rows)
    return [serialize(v, vis, usernames) for v in rows]


@router.get("/saved-views/{view_id}")
async def get_saved_view(
    view_id: uuid.UUID,
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    await _load_catalogs()
    view = await _get_readable(db, view_id, vis)
    return serialize(view, vis, await _usernames(db, [view]))


@router.post("/saved-views", dependencies=[Depends(_body_limit)])
async def create_saved_view(
    body: SavedViewIn,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    await _load_catalogs()
    if body.visibility == "shared" and not can_share(vis):
        raise HTTPException(status_code=403, detail=SHARE_DENIED)
    query, _issues, _legacy, _v = normalize_query(body.query, kind=body.kind or "forensics", strict=True)
    if body.kind and query["kind"] != body.kind:
        raise invalid_filter("kind and query.kind disagree", {"kind": "Does not match query.kind"})
    for exporter in query_exporters(query):
        if not vis.allows(exporter):
            raise HTTPException(status_code=404, detail="Exporter not found")
    await _check_owner_quota(db, user)
    view = NetflowSavedView(
        name=body.name, description=body.description, query=query, pinned=body.pinned,
        owner_id=user.id, visibility=body.visibility, kind=query["kind"], query_version=2, updated_by=user.id,
    )
    db.add(view)
    await db.flush()
    await write_audit_log(db, actor=user, action="netflow.saved_view.create", resource_type="netflow_saved_view",
                          resource_id=str(view.id), metadata=_audit_meta(view, query=query))
    await db.commit()
    await db.refresh(view)
    return serialize(view, vis, {str(user.id): user.username})


@router.patch("/saved-views/{view_id}", dependencies=[Depends(_body_limit)])
async def update_saved_view(
    view_id: uuid.UUID,
    body: SavedViewPatch,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    await _load_catalogs()
    view = await _get_readable(db, view_id, vis)
    if not can_edit(view, vis):
        raise HTTPException(status_code=403, detail=EDIT_DENIED)
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise invalid_filter("Enter a name for the view", {"name": "Required"})
    if "pinned" in changes and changes["pinned"] is None:
        raise invalid_filter("pinned must be true or false", {"pinned": "Required"})
    if "visibility" in changes:
        if changes["visibility"] is None:
            raise invalid_filter("visibility must be private or shared", {"visibility": "Required"})
        if changes["visibility"] != view.visibility:
            if not can_change_visibility(view, vis):
                raise HTTPException(status_code=403, detail="Only the owner can change who can see this view")
            if changes["visibility"] == "shared" and not can_share(vis):
                raise HTTPException(status_code=403, detail=SHARE_DENIED)
    changed: list[str] = []
    if "query" in changes:
        if changes["query"] is None:
            raise invalid_filter("query cannot be empty", {"query": "Required"})
        query, _i, _l, _v = normalize_query(changes["query"], kind=view.kind or "forensics", strict=True)
        if query["kind"] != (view.kind or "forensics"):
            raise invalid_filter("A saved view cannot change its kind", {"query.kind": "Cannot change"})
        for exporter in query_exporters(query):
            if not vis.allows(exporter):
                raise HTTPException(status_code=404, detail="Exporter not found")
        if query != view.query or view.query_version != 2:
            view.query, view.query_version = query, 2
            changed.append("query")
    for key in ("name", "description", "pinned", "visibility"):
        if key in changes and changes[key] != getattr(view, key):
            setattr(view, key, changes[key])
            changed.append(key)
    if changed:
        view.updated_by = user.id
        view.updated_at = datetime.now(timezone.utc)
        await write_audit_log(db, actor=user, action="netflow.saved_view.update", resource_type="netflow_saved_view",
                              resource_id=str(view.id), metadata=_audit_meta(view, changed=changed))
        await db.commit()
        await db.refresh(view)
    return serialize(view, vis, await _usernames(db, [view]))


@router.post("/saved-views/{view_id}/duplicate", dependencies=[Depends(_body_limit)])
async def duplicate_saved_view(
    view_id: uuid.UUID,
    body: DuplicateIn | None = None,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    """A private copy owned by the caller (any view the caller can open)."""
    await _load_catalogs()
    source = await _get_readable(db, view_id, vis)
    query, _issues, _legacy = _normalized(source)
    name = (body.name if body and body.name else None) or f"Copy of {source.name}"[:120].strip()
    await _check_owner_quota(db, user)
    copy = NetflowSavedView(
        name=name, description=source.description, query=query, pinned=False, owner_id=user.id,
        visibility="private", kind=source.kind or query.get("kind") or "forensics", query_version=2,
        updated_by=user.id,
    )
    db.add(copy)
    await db.flush()
    await write_audit_log(db, actor=user, action="netflow.saved_view.duplicate", resource_type="netflow_saved_view",
                          resource_id=str(copy.id), metadata=_audit_meta(copy, source_id=str(source.id)))
    await db.commit()
    await db.refresh(copy)
    return serialize(copy, vis, {str(user.id): user.username})


@router.delete("/saved-views/{view_id}")
async def delete_saved_view(
    view_id: uuid.UUID,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    await _load_catalogs()
    view = await _get_readable(db, view_id, vis)
    if not can_edit(view, vis):
        raise HTTPException(status_code=403, detail=EDIT_DENIED)
    meta = _audit_meta(view)
    await db.delete(view)
    await write_audit_log(db, actor=user, action="netflow.saved_view.delete", resource_type="netflow_saved_view",
                          resource_id=str(view_id), metadata=meta)
    await db.commit()
    return {"deleted": str(view_id)}


__all__ = [
    "router", "normalize_query", "typed_filter", "query_exporters", "can_read", "can_edit",
    "can_change_visibility", "serialize", "query_names_hosts", "BoundedBodyRoute", "MAX_VIEWS_PER_OWNER",
]
