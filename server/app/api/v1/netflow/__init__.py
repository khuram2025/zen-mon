"""NetFlow analytics API, mounted at ``/netflow`` (``/api/v1/netflow`` in the app).

Endpoints live in topic modules, each collecting its routes on a prefix-less
``APIRouter``; shared helpers live in ``common``:

    overview     /overview, /timeseries, /device-status, /heatmap, /exporters
    breakdowns   /top-talkers, /top-endpoints, /top-conversations, /protocols,
                 /ports, /applications, /interfaces, /dscp, /tcp-flags,
                 /network-classes, /countries, /ip-groups
    forensics    /forensics
    capacity     /capacity
    anomalies    /anomalies
    saved_views  /saved-views
    exporters    /exporter-devices
    health, settings, hosts, explain
                 placeholders for upcoming work (no routes yet)

This module registers every sub-module route on the one ``router`` that
main.py includes, in the order they had when this was a single file (FastAPI
matches routes in registration order), so ``router.routes`` stays a flat list
of ``APIRoute``s. ``include_router`` is not used for the sub-routers: on this
FastAPI version it nests them (``router.routes`` would hold ``_IncludedRouter``
nodes) and it cannot interleave routes coming from different modules.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.routing import APIRoute

from . import (
    anomalies,
    breakdowns,
    capacity,
    explain,
    exporters,
    forensics,
    health,
    hosts,
    overview,
    saved_views,
    settings,
)

# Re-exported for code and tests that import them from app.api.v1.netflow.
from .common import (
    _NET_CLASS_RANGES,
    _application_for_flow,
    _application_for_port,
    _resolve_window,
    _scope,
)

__all__ = [
    "router",
    "_NET_CLASS_RANGES",
    "_application_for_flow",
    "_application_for_port",
    "_resolve_window",
    "_scope",
]

router = APIRouter(prefix="/netflow", tags=["NetFlow"])

# APIRoute attributes carried over when a sub-module route is registered on
# ``router`` (every add_api_route() option besides path/endpoint/route class).
_ROUTE_ATTRS = (
    "response_model",
    "status_code",
    "tags",
    "dependencies",
    "summary",
    "description",
    "response_description",
    "responses",
    "deprecated",
    "methods",
    "operation_id",
    "response_model_include",
    "response_model_exclude",
    "response_model_by_alias",
    "response_model_exclude_unset",
    "response_model_exclude_defaults",
    "response_model_exclude_none",
    "include_in_schema",
    "response_class",
    "name",
    "callbacks",
    "openapi_extra",
    "generate_unique_id_function",
    "strict_content_type",
)

_mounted: set[int] = set()


def _mount(sub: APIRouter, *paths: str) -> None:
    """Register ``sub``'s not-yet-mounted routes on ``router``, in ``sub``'s
    order; only the ones whose path is in ``paths`` when any are given."""
    for route in sub.routes:
        if id(route) in _mounted or (paths and route.path not in paths):
            continue
        if not isinstance(route, APIRoute):
            raise TypeError(f"netflow: unsupported route type {type(route).__name__}")
        _mounted.add(id(route))
        router.add_api_route(
            route.path,
            route.endpoint,
            route_class_override=type(route),
            **{attr: getattr(route, attr) for attr in _ROUTE_ATTRS},
        )


# The original single-file registration order.
_mount(overview.router, "/overview", "/timeseries")
_mount(
    breakdowns.router,
    "/top-talkers", "/top-endpoints", "/top-conversations", "/protocols", "/ports", "/applications",
)
_mount(overview.router, "/device-status", "/heatmap")
_mount(breakdowns.router, "/interfaces", "/dscp", "/tcp-flags", "/network-classes")
_mount(forensics.router)
_mount(capacity.router)
_mount(anomalies.router)
_mount(breakdowns.router, "/countries", "/ip-groups")
_mount(saved_views.router)
_mount(exporters.router)
_mount(overview.router, "/exporters")
# Everything not mounted yet, placeholders last: a route added to any module
# later is registered after the ones above instead of being silently dropped.
for _module in (
    overview, breakdowns, forensics, capacity, anomalies, saved_views, exporters,
    health, settings, hosts, explain,
):
    _mount(_module.router)
