"""Route grouping for browser RUM view names.

A real application produces one URL per record (``/orders/1234``,
``/products/blue-shoes``); the Views explorer needs one row per *route*.
Two layers fold URLs into routes:

1. The intake validator already replaces numeric, UUID, long-hex and e-mail
   path segments with ``:id`` (see ``_scrub_path`` in ``api/v1/rum.py``), so
   ``/orders/1234`` arrives here as ``/orders/:id`` with no configuration.
2. Per-application rules configured on the RUM key handle everything that is
   not an obvious identifier — slugs, usernames, locale prefixes — with a
   small glob syntax:

   ``*``   matches exactly one path segment (or part of one: ``report-*``)
   ``**``  matches the rest of the path, including further slashes

   Rules are tried in order; the first match wins and its ``name`` becomes the
   view name. ``/products/*`` → ``/products/:slug`` turns
   ``/products/blue-shoes`` into ``/products/:slug``; ``/docs/**`` → ``/docs``
   collapses an entire section.

Names should look like routes (``/products/:slug``), because they are what
the Views tab, alerts and reports display.
"""
from __future__ import annotations

import re
from functools import lru_cache

MAX_RULES = 50
MAX_PATTERN_LENGTH = 512

RouteRule = tuple[str, str]  # (match glob, view name)


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    escaped = re.escape(glob)
    # re.escape turns "*" into "\*"; expand the double form first.
    pattern = escaped.replace(r"\*\*", ".*").replace(r"\*", "[^/]+")
    return re.compile(f"^{pattern}/?$")


@lru_cache(maxsize=256)
def compile_rules(rules: tuple[RouteRule, ...]) -> tuple[tuple[re.Pattern[str], str], ...]:
    compiled = []
    for match, name in rules[:MAX_RULES]:
        if not match or not name or not match.startswith("/") or not name.startswith("/"):
            continue
        try:
            compiled.append((_glob_to_regex(match[:MAX_PATTERN_LENGTH]), name[:MAX_PATTERN_LENGTH]))
        except re.error:
            continue
    return tuple(compiled)


def apply_route_rules(view_name: str, rules: tuple[RouteRule, ...] | list[RouteRule] | None) -> str:
    """Return the grouped view name, or the input unchanged when no rule matches."""
    if not rules or not view_name:
        return view_name
    for pattern, name in compile_rules(tuple(rules)):
        if pattern.match(view_name):
            return name
    return view_name


def rules_from_options(rum_options: object) -> tuple[RouteRule, ...]:
    """Extract ``(match, name)`` pairs from a key's stored ``rum_options``.

    Accepts the decoded JSON object, a JSON string (asyncpg returns jsonb as
    text through SQLAlchemy ``text()``), or None.
    """
    if not rum_options:
        return ()
    if isinstance(rum_options, (str, bytes)):
        import json
        try:
            rum_options = json.loads(rum_options)
        except ValueError:
            return ()
    if not isinstance(rum_options, dict):
        return ()
    raw = rum_options.get("route_rules") or []
    rules: list[RouteRule] = []
    for item in raw[:MAX_RULES]:
        if not isinstance(item, dict):
            continue
        match, name = str(item.get("match") or ""), str(item.get("name") or "")
        if match.startswith("/") and name.startswith("/"):
            rules.append((match, name))
    return tuple(rules)
