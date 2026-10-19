"""Readable request-validation errors (1.24.21).

FastAPI answers a body that fails its schema with ``422 {"detail": [...]}``,
one item per problem with ``loc`` (the field path), ``msg`` and ``type``.
For a field restricted by a regular expression the message was pydantic's
"String should match pattern '^(ping_status|rtt|...)$'" — for the alert rule
metric, a ~1000-character regex that names no field and no value; the editor
showed three of those joined in a toast.

The handler keeps the response shape and every ``loc`` (clients map errors
to fields with it) and rewrites only pattern mismatches as
``"<field>: '<value>' is not an accepted value"``, listing the accepted
values when the pattern is a short plain alternation (``^(AND|OR)$``). The
regex itself is dropped from ``ctx``. A non-finite number echoed back in
``input`` (a body sending ``NaN``) is rendered as text: JSON cannot carry it,
and the default handler failed with a 500 instead of the 422.
"""

from __future__ import annotations

import math
import re
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})
_ALTERNATION = re.compile(r"^\^\(([^()\[\]{}*+?\\]*)\)\$$")
_PLAIN_CHOICE = re.compile(r"^[\w<>=!.:/ -]+$")
MAX_LISTED_CHOICES = 12
MAX_SHOWN_VALUE = 80


def field_path(loc: Any) -> str:
    """``["body", "conditions", 0, "metric"]`` -> ``"conditions[0].metric"``."""
    out = ""
    for part in list(loc or []):
        if isinstance(part, int):
            out += f"[{part}]"
        elif not out and part in _LOCATIONS:
            continue
        else:
            out += ("." if out else "") + str(part)
    return out or "request"


def _choices(pattern: Any) -> list[str] | None:
    m = _ALTERNATION.match(str(pattern or ""))
    if not m:
        return None
    options = m.group(1).split("|")
    if len(options) > MAX_LISTED_CHOICES or not all(_PLAIN_CHOICE.match(o) for o in options):
        return None
    return options


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def readable(error: dict) -> dict:
    """One pydantic error item with a pattern mismatch made readable."""
    item = dict(error)
    if "input" in item:
        item["input"] = _json_safe(item["input"])
    if "ctx" in item:
        item["ctx"] = _json_safe(item["ctx"])
    if item.get("type") != "string_pattern_mismatch":
        return item
    value = str(item.get("input", ""))
    if len(value) > MAX_SHOWN_VALUE:
        value = value[:MAX_SHOWN_VALUE] + "…"
    ctx = dict(item.get("ctx") or {})
    options = _choices(ctx.pop("pattern", None))
    msg = f"{field_path(item.get('loc'))}: '{value}' is not an accepted value"
    if options:
        msg += f" (expected one of {', '.join(options)})"
    item["msg"] = msg
    if ctx:
        item["ctx"] = ctx
    else:
        item.pop("ctx", None)
    return item


async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422,
                        content={"detail": jsonable_encoder([readable(e) for e in exc.errors()])})
