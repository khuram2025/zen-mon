"""Route → capability contract (RP-04, RP-05).

Frontend sidebar hiding is not an authorization boundary, so the API must
refuse a request by itself. Every route in the application falls into exactly
one of four classes, and ``tests/test_route_policy_contract.py`` fails the
build when a route fits none of them:

1. **Public** - listed in ``PUBLIC_ROUTES`` (login, machine ingest keyed by
   its own credential, health, bootstrap downloads, ...). Reviewed by hand.
2. **Self** - any authenticated account (profile, own password, server time).
3. **Explicitly guarded** - the handler declares ``require_permission(...)`` /
   ``require_admin_user`` itself. These are the precise per-action checks.
4. **Module default** - the router was mounted with :func:`module_access`;
   reads need one of the module's *view* permissions and mutations one of its
   *manage* permissions. A new endpoint added to a mounted router is therefore
   protected by default instead of silently open to every signed-in account.

Method semantics: GET/HEAD/OPTIONS are reads, everything else is a mutation.
A route that is a POST-but-read (search / preview) declares its own guard.
"""

from __future__ import annotations

from typing import Iterable, Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.permissions import SUPERUSER_PERMISSION
from app.core import security as _security
from app.core.security import get_current_user
from app.models.user import User

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Sentinel meaning "any permission that ends in .view" - used for shared
# reference data (tags, monitoring sites) every legitimate account may read
# but a zero-permission role may not.
ANY_VIEW = "*.view"

# Unauthenticated by design. Each entry authenticates by another mechanism
# (enrolment token, ingest key, one-time download token) or serves public
# material. Keep this list short and reviewed.
PUBLIC_ROUTES: frozenset[str] = frozenset({
    "/api/v1/auth/login",
    "/api/v1/auth/login/mfa",
    "/api/v1/auth/password-policy",
    "/api/v1/auth/oidc/config",
    "/api/v1/auth/oidc/login",
    "/api/v1/auth/oidc/callback",
    "/api/v1/auth/oidc/exchange",
    "/api/v1/system/health",
    "/api/v1/settings/company/logo",
    "/api/v1/settings/timezone",
    "/api/v1/reports/shared/{token}",
    "/api/v1/sensors/bootstrap/{sensor_id}/{download_token}/seed.iso",
    "/api/v1/sensors/bootstrap/{sensor_id}/{download_token}/configured.ova",
    "/api/v1/sensor/enroll",
    "/api/v1/sensor/heartbeat",
    "/api/v1/sensor/config",
    "/api/v1/sensor/results/ping",
    "/api/v1/sensor/results/service",
    "/api/v1/sensor/results/snmp",
    "/api/v1/sensor/events",
    "/api/v1/sensor/appliance/manifest",
    "/api/v1/sensor/appliance/{kind}",
    "/api/v1/sensor/bin/{platform}/manifest.json",
    "/api/v1/sensor/bin/{platform}/zenplus-sensor",
    "/api/v1/sensor/bin/{platform}/zenplus-sensor.sha256",
    "/api/v1/sensor/install.sh",
    "/api/v1/sensor/mock_sensor.py",
    "/api/v1/agents/enroll",
    "/api/v1/agents/heartbeat",
    "/api/v1/agents/config",
    "/api/v1/agents/results/host",
    "/api/v1/agents/events",
    "/api/v1/agents/diagnostics",
    "/api/v1/agents/network-capture",
    "/api/v1/agents/packages/manifest",
    "/api/v1/agents/packages/{platform}/versions/{version}",
    "/api/v1/agents/packages/{platform}/latest",
    "/api/v1/agents/install.ps1",
    "/api/v1/agents/install.sh",
    "/api/v1/agents/commands/poll",
    "/api/v1/agents/commands/{command_id}/result",
    "/api/v1/agents/apm/enroll",
    "/api/v1/agents/apm/discovery",
    "/v1/traces",
    "/api/v1/apm/rum/sdk.js",
    "/api/v1/apm/rum/ingest",
    "/v1development/profiles",
})

# Authenticated, but not tied to any module (the caller's own profile etc.).
SELF_ROUTES: frozenset[str] = frozenset({
    "/api/v1/auth/me",
    "/api/v1/auth/logout",
    "/api/v1/auth/stream-ticket",
    "/api/v1/auth/mfa/status",
    "/api/v1/system/time",
    "/api/v1/subscription",
    "/api/v1/subscription/plans",
    "/api/v1/subscription/activate",
    "/api/v1/system/security/tls/certificate/download",
})

# Any route under these prefixes is the caller's own account (profile, own
# password, own MFA, own sessions) and needs no module permission.
SELF_PREFIXES: tuple[str, ...] = ("/api/v1/users/me/", "/api/v1/auth/mfa/")

_guard_cache: dict[int, bool] = {}


def _walk(dependant):
    for dep in getattr(dependant, "dependencies", []) or []:
        yield dep
        yield from _walk(dep)


def has_explicit_guard(route) -> bool:
    """Whether the route's own dependency tree declares a permission guard."""
    key = id(route)
    cached = _guard_cache.get(key)
    if cached is not None:
        return cached
    dependant = getattr(route, "dependant", None)
    found = any(getattr(d.call, "_zp_guard", None) for d in _walk(dependant)) if dependant else False
    _guard_cache[key] = found
    return found


def route_template(request: Request) -> str:
    """The matched route's full path template, e.g. ``/api/v1/reports/shared/{token}``.

    ``scope['route']`` carries the router-relative template (the mount prefix is
    not part of it), so the prefix is recovered from the concrete request path.
    """
    route = request.scope.get("route")
    relative = getattr(route, "path_format", None) or getattr(route, "path", None)
    concrete = request.url.path
    if not relative:
        return concrete
    try:
        instantiated = relative.format(**{k: str(v) for k, v in request.scope.get("path_params", {}).items()})
    except (KeyError, IndexError, ValueError):
        return concrete
    if concrete.endswith(instantiated):
        return concrete[: len(concrete) - len(instantiated)] + relative
    return concrete


def _grants(perms: Iterable[str], needed: tuple[str, ...]) -> bool:
    perm_set = set(perms)
    if SUPERUSER_PERMISSION in perm_set:
        return True
    for need in needed:
        if need == ANY_VIEW:
            if any(p.endswith(".view") for p in perm_set):
                return True
        elif need in perm_set:
            return True
    return False


def module_access(
    *,
    read: tuple[str, ...],
    write: tuple[str, ...],
    label: str,
    allow_public: bool = False,
):
    """Router-level default policy. See the module docstring.

    ``allow_public`` lets a router that mixes machine-ingest routes with
    authenticated ones skip authentication for the routes in ``PUBLIC_ROUTES``.
    """

    async def _check(request: Request, db: AsyncSession, user: User) -> User:
        template = route_template(request)
        if template in PUBLIC_ROUTES or template in SELF_ROUTES or template.startswith(SELF_PREFIXES):
            return user
        if has_explicit_guard(request.scope.get("route")):
            return user  # the route's own guard is the precise check
        needed = read if request.method in SAFE_METHODS else write
        perms = await _security.get_role_permissions(db, user.role)
        if _grants(perms, needed):
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient permissions ({label})",
        )

    if allow_public:
        from fastapi.security import HTTPBearer
        _bearer = HTTPBearer(auto_error=False)

        async def dependency(request: Request, db: AsyncSession = Depends(get_db)):
            if route_template(request) in PUBLIC_ROUTES:
                return None
            creds = await _bearer(request)
            # Resolve through dependency_overrides so substitution (tests,
            # embedding apps) behaves exactly as for a Depends() parameter.
            resolver = request.app.dependency_overrides.get(get_current_user, get_current_user)
            import inspect
            arity = len(inspect.signature(resolver).parameters)
            result = resolver(*(request, creds, db)[:arity]) if arity <= 3 else resolver(request, creds, db)
            user = await result if inspect.isawaitable(result) else result
            return await _check(request, db, user)
    else:
        async def dependency(  # type: ignore[misc]
            request: Request,
            db: AsyncSession = Depends(get_db),
            user: User = Depends(get_current_user),
        ):
            return await _check(request, db, user)

    dependency._zp_module_default = label  # type: ignore[attr-defined]
    return dependency


# ─────────────────────────────────────────────────────────────────
# Module defaults. (read permissions, write permissions): any one grants.
# Mounted per router in app.main via ``policy(name)``.
# ─────────────────────────────────────────────────────────────────

_POLICIES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "devices": (("devices.view",), ("devices.manage",)),
    "tags": ((ANY_VIEW,), ("devices.manage", "service_checks.manage", "servers.manage", "apm.manage")),
    "monitoring_sites": ((ANY_VIEW,), ("devices.manage", "service_checks.manage")),
    "alerts": (("alerts.view",), ("alerts.manage",)),
    # Escalation matrix and on-call (1.24.17): read with alerts.view, change
    # with alerts.manage. POST dry runs declare their own alerts.view guard.
    "escalation": (("alerts.view",), ("alerts.manage",)),
    "oncall": (("alerts.view",), ("alerts.manage",)),
    "events": (("alerts.view", "devices.view"), ("alerts.manage",)),
    "settings": (("settings.view",), ("settings.manage",)),
    "service_checks": (("service_checks.view",), ("service_checks.manage",)),
    "reports": (("reports.view",), ("reports.manage",)),
    "discovery": (("discovery.view",), ("discovery.run",)),
    "snmp": (("devices.view", "discovery.view"), ("devices.manage",)),
    "windows_credentials": (("devices.manage", "service_checks.manage"),) * 2,
    "netflow": (("netflow.view",), ("netflow.manage",)),
    "link_utilization": (("devices.view",), ("devices.manage",)),
    "maps": (("maps.view",), ("maps.manage",)),
    "servers": (("servers.view",), ("servers.manage",)),
    "apm": (("apm.view",), ("apm.manage",)),
    "udt": (("udt.view",), ("udt.manage",)),
    "netpath": (("netpath.view",), ("netpath.manage",)),
    "inventory": (("inventory.view",), ("inventory.manage",)),
    "ncm": (("ncm.view",), ("ncm.manage",)),
    "access": (("users.view", "users.manage", "roles.view", "roles.manage"),
               ("users.manage", "roles.manage")),
}


def policy(name: str, *, allow_public: bool = False):
    """FastAPI dependency for ``include_router(..., dependencies=[Depends(policy(...))])``."""
    read, write = _POLICIES[name]
    return Depends(module_access(read=read, write=write, label=name, allow_public=allow_public))


# ─────────────────────────────────────────────────────────────────
# Static inventory (used by the contract test and `scripts/route-inventory`).
# ─────────────────────────────────────────────────────────────────

def _iter_effective_routes(app):
    from fastapi.routing import APIRoute
    for r in app.routes:
        if type(r).__name__ == "_IncludedRouter":
            yield from r.effective_route_contexts()
        elif isinstance(r, APIRoute):
            yield r


def route_inventory(app) -> list[dict]:
    """Classify every API route: public / self / guarded / module default /
    unprotected. 'unprotected' (authentication only, or nothing) is the class the
    contract test forbids."""
    rows = []
    for r in _iter_effective_routes(app):
        deps = list(_walk(r.dependant))
        calls = [d.call for d in deps]
        guard = next((c for c in calls if getattr(c, "_zp_guard", None)), None)
        module = next((c._zp_module_default for c in calls if hasattr(c, "_zp_module_default")), None)
        internal = any(getattr(c, "__name__", "") == "require_internal_caller" for c in calls)
        authed = any(getattr(c, "__name__", "") in ("get_current_user", "get_current_user_stream") for c in calls) or bool(module)
        path = getattr(r, "path_format", None) or r.path
        methods = sorted(m for m in (r.methods or set()) if m not in ("HEAD", "OPTIONS"))
        if path in PUBLIC_ROUTES:
            kind = "public"
        elif internal:
            kind = "internal"
        elif path in SELF_ROUTES or path.startswith(SELF_PREFIXES):
            kind = "self"
        elif guard is not None:
            kind = "guarded:" + guard._zp_guard
        elif module:
            kind = "module:" + module
        elif authed:
            kind = "authenticated-only"
        else:
            kind = "unauthenticated"
        rows.append({
            "methods": methods,
            "path": path,
            "kind": kind,
            "permissions": list(getattr(guard, "_zp_permissions", ()) or ()),
            "handler": f"{r.endpoint.__module__.rsplit('.', 1)[-1]}.{r.endpoint.__name__}",
        })
    return rows
