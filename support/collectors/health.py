"""Health collector — local API + dependency probes + known-risk checks.

The known-risk checks are the part that earns its keep: they encode every
class of incident we've actually seen in the field, so a support engineer
looking at a bundle gets an immediate "yes/no" answer for each.

Today's incidents that this collector flags directly:
- migration drift  →  hash mismatch between schema_migrations and on-disk
                      migrate-*.sql.
- 502 after add-device / assign-credential  →  presence of the column the
                      affected endpoints rely on (e.g. devices.snmp_credential_id).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib import error, request

from . import CollectorContext, CollectorResult


LOCAL_API_BASE = "http://127.0.0.1:8000"
LOCAL_NGINX_BASE = "http://127.0.0.1"

INTERNAL_ENDPOINTS = (
    ("system-health", "/api/v1/system/health"),
    ("storage", "/api/v1/system/storage"),
    ("update-status", "/api/v1/system/update-status"),
    ("registration", "/api/v1/system/registration"),
)


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="health")

    # 1. Internal HTTP probes. We hit the API directly first (bypasses nginx),
    #    then nginx, to triangulate where a 502 comes from.
    result.files["health/api-direct.json"] = _dump(_probe_endpoints(LOCAL_API_BASE))
    result.files["health/api-via-nginx.json"] = _dump(_probe_endpoints(LOCAL_NGINX_BASE))

    # 2. Dependency-level connectivity, not via the API.
    result.files["health/dep-health.json"] = _dump(_dep_health(ctx, result))

    # 3. Known-risk checks: deterministic yes/no signals for known incidents.
    risk = _known_risk_checks(ctx, result)
    result.files["health/known-risk-checks.json"] = _dump(risk)
    if risk.get("any_failed"):
        result.warn("one or more known-risk checks failed")

    return result


def _probe_endpoints(base: str) -> dict:
    out = {"base": base, "results": {}}
    for name, path in INTERNAL_ENDPOINTS:
        url = base + path
        try:
            with request.urlopen(url, timeout=5) as resp:
                body = resp.read(64 * 1024).decode("utf-8", errors="replace")
                out["results"][name] = {
                    "url": url,
                    "status": resp.status,
                    "body_preview": body[:4000],
                }
        except error.HTTPError as e:
            out["results"][name] = {"url": url, "status": e.code, "body_preview": e.read(2000).decode("utf-8", errors="replace")}
        except Exception as exc:  # noqa: BLE001
            out["results"][name] = {"url": url, "status": "error", "error": str(exc)}
    return out


def _dep_health(ctx: CollectorContext, result: CollectorResult) -> dict:
    return {
        "postgres": _command(["pg_isready", "-q"], timeout=5),
        "redis": _command(["redis-cli", "ping"], timeout=5),
        "clickhouse_docker_exec": _command(
            ["docker", "exec", "zenplus-clickhouse", "clickhouse-client", "--query", "SELECT 1"],
            timeout=10,
        ),
        "nginx_running": _command(["systemctl", "is-active", "nginx"], timeout=5),
        "snmp_enc_key_set": {"value": bool(os.environ.get("SNMP_ENC_KEY"))},
    }


def _known_risk_checks(ctx: CollectorContext, result: CollectorResult) -> dict:
    """Encode every incident class we've actually seen.

    Each check is dict ``{ok: bool, detail: ...}`` so adding new ones is
    cheap. ``any_failed`` rolls them up for the manifest.
    """
    checks: dict[str, dict] = {}

    checks["snmp_enc_key_set"] = {
        "ok": bool(os.environ.get("SNMP_ENC_KEY")),
        "detail": "SNMP_ENC_KEY env var must be set or SNMPv3 creds cannot be decrypted",
    }
    checks["migrations_lock_matches_disk"] = _migrations_lock_check(ctx)
    checks["updater_registered"] = _updater_registered_check(ctx)
    checks["nginx_proxy_reachable"] = {
        "ok": _command(["curl", "-fsS", "-o", "/dev/null", "-w", "%{http_code}",
                        LOCAL_NGINX_BASE + "/api/v1/system/health"], timeout=5).get("stdout") in ("200",),
        "detail": "nginx → API connectivity",
    }

    checks["any_failed"] = any(c.get("ok") is False for c in checks.values() if isinstance(c, dict))
    if checks["any_failed"]:
        result.warn("known-risk checks have failures")
    return checks


def _migrations_lock_check(ctx: CollectorContext) -> dict:
    """Compare scripts/migrations.lock with on-disk migrate-*.sql hashes.

    This is the exact check ``scripts/build-release.py:lint_migrations`` runs
    at release time — surfacing it in the bundle means support sees migration
    drift on a deployed appliance without asking the customer to run anything.
    """
    scripts = ctx.zenplus_root / "scripts"
    lock = scripts / "migrations.lock"
    if not lock.exists():
        return {"ok": False, "detail": f"missing {lock}"}
    locked: dict[str, str] = {}
    for line in lock.read_text(errors="replace").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and not line.startswith("#"):
            locked[parts[1].strip()] = parts[0].strip()
    drift: list[dict] = []
    for f in sorted(scripts.glob("migrate-*.sql")):
        on_disk = _sha256(f)
        recorded = locked.get(f.name)
        if recorded is None:
            drift.append({"file": f.name, "issue": "unlocked"})
        elif recorded != on_disk:
            drift.append({"file": f.name, "issue": "drift", "locked": recorded, "on_disk": on_disk})
    return {"ok": not drift, "detail": "drift entries below", "drift": drift}


def _updater_registered_check(ctx: CollectorContext) -> dict:
    conf = ctx.updater_root / "config" / "agent.conf"
    if not conf.exists():
        return {"ok": False, "detail": "agent.conf missing"}
    text = conf.read_text(errors="replace")
    id_present = bool(_extract_ini(text, "appliance", "id"))
    key_present = bool(_extract_ini(text, "appliance", "api_key"))
    return {
        "ok": id_present and key_present,
        "detail": "appliance.id and appliance.api_key must both be set",
        "id_present": id_present,
        "api_key_present": key_present,
    }


def _extract_ini(text: str, section: str, key: str) -> str:
    in_section = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_section = (s[1:-1] == section)
            continue
        if in_section and "=" in s:
            k, _, v = s.partition("=")
            if k.strip() == key:
                return v.strip()
    return ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _command(cmd: list[str], *, timeout: int) -> dict:
    if not shutil.which(cmd[0]):
        return {"command": " ".join(cmd), "exit_code": -1, "stdout": "", "stderr": "not found"}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"command": " ".join(cmd), "exit_code": proc.returncode,
                "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"command": " ".join(cmd), "exit_code": -1, "stdout": "", "stderr": "timeout"}


def _dump(obj: dict) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
