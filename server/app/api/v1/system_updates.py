"""System update status, storage, and control endpoints for the dashboard."""

import configparser
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin_user
from app.models.user import User
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/system", tags=["System Updates"])

UPDATER_DIR = Path("/opt/zenplus/updater")
CONFIG_PATH = UPDATER_DIR / "config" / "agent.conf"
SUBSCRIPTION_PATH = UPDATER_DIR / "config" / "subscription.json"
VERSION_FILE = Path("/opt/zenplus/.version")
LOG_FILE = UPDATER_DIR / "logs" / "update.log"
HISTORY_FILE = UPDATER_DIR / "logs" / "update-history.json"
TIMER_DROPIN_DIR = "/etc/systemd/system/zenplus-updater.timer.d"
TIMER_DROPIN_FILE = f"{TIMER_DROPIN_DIR}/override.conf"


# ─── Models ───────────────────────────────────────────────────────────────────

class UpdateConfig(BaseModel):
    auto_update: bool = True
    check_interval_hours: int = 4
    maintenance_window_start: str = ""
    maintenance_window_end: str = ""


class UpdateHistoryRecord(BaseModel):
    version: str
    from_version: str
    status: str
    changelog: str = ""
    severity: str = "normal"
    error: str = ""
    started_at: str = ""
    updated_at: str = ""
    completed_at: str = ""


class NodeLicense(BaseModel):
    total_node_cap: int = 0
    used_node_count: int = 0
    available_nodes: int = 0


class RemoteSubscription(BaseModel):
    id: str = ""
    name: str = ""
    plan: str = ""
    max_appliances: int = 0
    max_devices: int = 0
    used_slots: int = 0
    available_slots: int = 0
    is_active: bool = True
    is_expired: bool = False
    expires_at: Optional[str] = None
    days_remaining: Optional[int] = None
    license: Optional[NodeLicense] = None


class UpdateStatus(BaseModel):
    current_version: str
    installed_at: str = ""
    appliance_id: str
    server_url: str
    auto_update: bool
    check_interval_hours: int
    maintenance_window_start: str
    maintenance_window_end: str
    last_check: str
    next_check: str
    timer_active: bool
    updater_running: bool = False
    last_update: Optional[UpdateHistoryRecord] = None
    active_update: Optional[UpdateHistoryRecord] = None
    history: list[UpdateHistoryRecord] = []
    recent_log: list[str] = []
    subscription: Optional[RemoteSubscription] = None


class RegisterRequest(BaseModel):
    license_key: str


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_remote_error(resp) -> str:
    """Pull a human-readable message out of a zentryc.com error response.

    The server has two error shapes (see Documentation/14-REMOTE-SERVER-INTAKE.md A7):
      • Business errors: {"error": "..."} / {"detail": "..."} / {"message": "..."}
      • Validation 400s: DRF-style {"field": ["msg1", ...], ...}

    Without this helper the second shape surfaces as raw JSON in the dashboard,
    which is unreadable for the customer pasting a license key.
    """
    if not resp.headers.get("content-type", "").startswith("application/json"):
        return (resp.text or "").strip() or f"HTTP {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or "").strip() or f"HTTP {resp.status_code}"
    if isinstance(data, dict):
        for key in ("error", "detail", "message"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # DRF-style field-keyed errors
        parts = []
        for field, msgs in data.items():
            if isinstance(msgs, list) and msgs:
                parts.append(f"{field}: {'; '.join(str(m) for m in msgs)}")
            elif isinstance(msgs, str) and msgs.strip():
                parts.append(f"{field}: {msgs.strip()}")
        if parts:
            return " | ".join(parts)
    return str(data) or f"HTTP {resp.status_code}"


def _load_subscription() -> Optional[RemoteSubscription]:
    """Load cached subscription data written by the updater agent."""
    if not SUBSCRIPTION_PATH.exists():
        return None
    try:
        data = json.loads(SUBSCRIPTION_PATH.read_text())
        return RemoteSubscription(**data)
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def _save_subscription(data: dict) -> None:
    """Persist subscription data to the shared JSON file."""
    SUBSCRIPTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUBSCRIPTION_PATH.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(str(SUBSCRIPTION_PATH), 0o600)
    except OSError:
        pass


async def _count_nodes() -> int:
    """Local node count = devices + service_checks. One node per device or service.

    Run inside a fresh session so the caller doesn't have to thread one in,
    and silently degrade to 0 on DB failure — license sync should never block
    on a transient query error.
    """
    from sqlalchemy import text
    from app.core.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT (SELECT COUNT(*) FROM devices) "
                     "+ (SELECT COUNT(*) FROM service_checks)")
            )
            return int(result.scalar_one() or 0)
    except Exception:
        return 0


def _get_version_info() -> tuple[str, str]:
    """Return (version, installed_at) from .version file."""
    version = "unknown"
    installed_at = ""
    if VERSION_FILE.exists():
        lines = VERSION_FILE.read_text().strip().splitlines()
        if lines:
            version = lines[0].strip()
        if len(lines) > 1:
            installed_at = lines[1].strip()
    return version, installed_at


def _read_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        parser.read(CONFIG_PATH)
    return parser


def _write_config(parser: configparser.ConfigParser) -> None:
    with open(CONFIG_PATH, "w") as f:
        parser.write(f)


def _get_timer_info() -> dict:
    """Get systemd timer status."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "zenplus-updater.timer",
             "--property=ActiveState,NextElapseUSecRealtime,LastTriggerUSec"],
            capture_output=True, text=True, timeout=5,
        )
        info = {}
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v
        return {
            "active": info.get("ActiveState") == "active",
            "next": info.get("NextElapseUSecRealtime", ""),
            "last": info.get("LastTriggerUSec", ""),
        }
    except Exception:
        return {"active": False, "next": "", "last": ""}


def _updater_service_is_running() -> bool:
    """Return True while the updater oneshot service is mid-run.

    Type=oneshot cycles through activating -> active -> inactive very fast,
    so we also treat any unfinished record in update-history.json as proof
    that an update is in flight.
    """
    # 1. Look at the history file — source of truth for work-in-progress
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text())
            for r in data:
                if r.get("status") in ("downloading", "applying"):
                    return True
    except (json.JSONDecodeError, OSError):
        pass

    # 2. Fall back to systemctl state
    try:
        result = subprocess.run(
            ["systemctl", "show", "zenplus-updater.service",
             "--property=ActiveState,SubState"],
            capture_output=True, text=True, timeout=5,
        )
        info = {}
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v
        return info.get("ActiveState") in ("activating", "active") and \
               info.get("SubState") in ("start", "start-pre", "start-post", "running")
    except Exception:
        return False


def _registration_is_valid() -> bool:
    """Return True if agent.conf has both an id and an api_key."""
    config = _read_config()
    return bool(
        config.get("appliance", "id", fallback="") and
        config.get("appliance", "api_key", fallback="")
    )


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/update-status")
async def get_update_status(user: User = Depends(require_admin_user)):
    """Get full update agent status including history and errors."""
    config = _read_config()
    timer = _get_timer_info()
    version, installed_at = _get_version_info()

    # Recent log lines
    recent_log = []
    if LOG_FILE.exists():
        try:
            all_lines = LOG_FILE.read_text().strip().splitlines()
            recent_log = all_lines[-30:]
        except Exception:
            pass

    # Load update history
    history_file = UPDATER_DIR / "logs" / "update-history.json"
    history_raw = []
    try:
        if history_file.exists():
            history_raw = json.loads(history_file.read_text())
            history_raw = list(reversed(history_raw))
    except (json.JSONDecodeError, OSError):
        pass

    last_update = None
    active_update = None
    for r in history_raw:
        if r.get("status") in ("downloading", "applying") and not active_update:
            active_update = UpdateHistoryRecord(**r)
        if r.get("status") in ("success", "failed", "rolled_back") and not last_update:
            last_update = UpdateHistoryRecord(**r)

    updater_running = _updater_service_is_running()

    interval_sec = config.getint("server", "check_interval_seconds", fallback=14400)

    return UpdateStatus(
        current_version=version,
        installed_at=installed_at,
        appliance_id=config.get("appliance", "id", fallback=""),
        server_url=config.get("server", "url", fallback="https://zentryc.com"),
        auto_update=config.getboolean("update", "auto_update", fallback=True),
        check_interval_hours=max(1, interval_sec // 3600),
        maintenance_window_start=config.get("update", "maintenance_window_start", fallback=""),
        maintenance_window_end=config.get("update", "maintenance_window_end", fallback=""),
        last_check=timer.get("last", ""),
        next_check=timer.get("next", ""),
        timer_active=timer.get("active", False),
        updater_running=updater_running,
        last_update=last_update,
        active_update=active_update,
        history=[UpdateHistoryRecord(**r) for r in history_raw[:10]],
        recent_log=recent_log,
        subscription=_load_subscription(),
    )


def _write_timer_override(check_interval_hours: int) -> tuple[bool, str]:
    """Install a systemd drop-in that overrides the updater timer interval.

    Uses the narrow NOPASSWD entries in /etc/sudoers.d/zenplus-updater and
    avoids touching the shipped unit file itself.
    """
    hours = max(1, int(check_interval_hours))
    override_body = (
        "# Managed by zenplus-api — do not edit by hand.\n"
        "[Timer]\n"
        "OnUnitActiveSec=\n"
        f"OnUnitActiveSec={hours}h\n"
    )

    try:
        # 1. Ensure the drop-in directory exists
        mkdir = subprocess.run(
            ["sudo", "-n", "/bin/mkdir", "-p", TIMER_DROPIN_DIR],
            capture_output=True, text=True, timeout=10,
        )
        if mkdir.returncode != 0:
            return False, f"mkdir failed: {mkdir.stderr.strip()}"

        # 2. Write the drop-in via tee (sudoers allows writing only this exact path)
        tee = subprocess.run(
            ["sudo", "-n", "/usr/bin/tee", TIMER_DROPIN_FILE],
            input=override_body, capture_output=True, text=True, timeout=10,
        )
        if tee.returncode != 0:
            return False, f"tee failed: {tee.stderr.strip()}"

        # 3. Reload systemd and restart the timer so the new interval takes effect
        reload_ = subprocess.run(
            ["sudo", "-n", "/bin/systemctl", "daemon-reload"],
            capture_output=True, text=True, timeout=10,
        )
        if reload_.returncode != 0:
            return False, f"daemon-reload failed: {reload_.stderr.strip()}"

        restart = subprocess.run(
            ["sudo", "-n", "/bin/systemctl", "restart", "zenplus-updater.timer"],
            capture_output=True, text=True, timeout=10,
        )
        if restart.returncode != 0:
            return False, f"timer restart failed: {restart.stderr.strip()}"

        return True, ""
    except subprocess.TimeoutExpired:
        return False, "sudo command timed out"
    except Exception as e:
        return False, str(e)


@router.put("/update-config")
async def update_config(
    body: UpdateConfig,
    user: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the update agent configuration."""
    config = _read_config()

    if not config.has_section("update"):
        config.add_section("update")
    if not config.has_section("server"):
        config.add_section("server")

    config.set("update", "auto_update", str(body.auto_update))
    config.set("server", "check_interval_seconds", str(body.check_interval_hours * 3600))
    config.set("update", "maintenance_window_start", body.maintenance_window_start)
    config.set("update", "maintenance_window_end", body.maintenance_window_end)

    _write_config(config)

    ok, err = _write_timer_override(body.check_interval_hours)
    if not ok:
        await write_audit_log(
            db,
            actor=user,
            action="system.update_config",
            resource_type="system_update",
            resource_id="updater",
            metadata={"status": "partial", "check_interval_hours": body.check_interval_hours},
        )
        await db.commit()
        # Non-fatal: config is saved, but the timer override couldn't be applied.
        # Surface the reason so support can fix the sudoers setup.
        return {
            "status": "partial",
            "message": (
                "Configuration saved, but the systemd timer override could not be "
                f"applied: {err}. Run scripts/setup-updater.sh to install the "
                "privilege rules."
            ),
        }

    await write_audit_log(
        db,
        actor=user,
        action="system.update_config",
        resource_type="system_update",
        resource_id="updater",
        metadata={"status": "ok", "check_interval_hours": body.check_interval_hours},
    )
    await db.commit()
    return {"status": "ok", "message": "Configuration updated"}


@router.post("/check-update")
async def trigger_check(
    user: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Trigger an on-demand update check via systemctl.

    Uses ``systemctl --no-block start`` so the API returns as soon as the
    job is queued — we do not want to conflate a sudo refusal with the
    downstream service's own exit code (e.g. a legitimate manifest
    signature rejection would otherwise look like a "sudoers missing"
    error in the UI).
    """
    if not _registration_is_valid():
        raise HTTPException(
            status_code=400,
            detail="Appliance is not registered. Enter your license key in Settings > Subscription first.",
        )

    try:
        result = subprocess.run(
            ["sudo", "-n", "/bin/systemctl", "--no-block", "start",
             "zenplus-updater.service"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {"status": "ok", "message": "Update check queued"}

    if result.returncode == 0:
        await write_audit_log(
            db,
            actor=user,
            action="system.update_check",
            resource_type="system_update",
            resource_id="updater",
            metadata={"status": "queued"},
        )
        await db.commit()
        return {"status": "ok", "message": "Update check queued"}

    # Classify the failure so the user sees the real reason.
    stderr = (result.stderr or result.stdout).strip() or "unknown error"
    lower = stderr.lower()

    if "a password is required" in lower or "not allowed" in lower or "may not run sudo" in lower:
        raise HTTPException(
            status_code=500,
            detail=(
                "The API server is not permitted to start the updater service. "
                "Install /etc/sudoers.d/zenplus-updater via scripts/setup-updater.sh."
            ),
        )

    raise HTTPException(
        status_code=500,
        detail=f"systemctl refused to queue the job: {stderr}",
    )


# ─── Registration ─────────────────────────────────────────────────────────────

@router.get("/registration")
async def get_registration(user: User = Depends(require_admin_user)):
    """Get current registration status."""
    config = _read_config()
    appliance_id = config.get("appliance", "id", fallback="")
    result = {
        "registered": bool(appliance_id),
        "appliance_id": appliance_id,
        "server_url": config.get("server", "url", fallback="https://zentryc.com"),
    }
    sub = _load_subscription()
    if sub:
        result["subscription"] = sub
    return result


@router.post("/register")
async def register_appliance(
    body: RegisterRequest,
    user: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Register this appliance with zentryc.com using a license key."""
    import httpx
    import os
    import platform

    config = _read_config()
    server_url = config.get("server", "url", fallback="https://zentryc.com")

    # Prevent accidental re-registration if already registered
    existing_id = config.get("appliance", "id", fallback="")
    if existing_id:
        raise HTTPException(
            status_code=409,
            detail=f"Appliance is already registered (ID: {existing_id}). "
                   "To re-register, an admin must first reset the appliance credentials.",
        )

    version, _ = _get_version_info()
    hostname = platform.node()
    machine = platform.machine().lower()
    arch = "amd64" if machine in ("x86_64", "amd64") else "arm64" if machine in ("aarch64", "arm64") else machine

    os_version = "linux"
    try:
        os_release = Path("/etc/os-release").read_text()
        info = {}
        for line in os_release.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v.strip('"')
        os_version = f"{info.get('ID', 'linux')}-{info.get('VERSION_ID', '')}"
    except OSError:
        pass

    try:
        async with httpx.AsyncClient(timeout=30, verify=True) as client:
            resp = await client.post(
                f"{server_url}/api/v1/appliances/register",
                json={
                    "hostname": hostname,
                    "arch": arch,
                    "os_version": os_version,
                    "current_version": version,
                    "registration_token": body.license_key,
                },
            )

        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=_extract_remote_error(resp))

        data = resp.json()
        appliance_id = data.get("appliance_id", "")
        api_key = data.get("api_key", "")

        if not appliance_id or not api_key:
            raise HTTPException(status_code=500, detail="Server returned incomplete registration data")

    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach update server: {e}")

    if not config.has_section("appliance"):
        config.add_section("appliance")
    config.set("appliance", "id", appliance_id)
    config.set("appliance", "api_key", api_key)
    _write_config(config)
    os.chmod(str(CONFIG_PATH), 0o600)

    # Persist subscription data from registration response
    subscription = data.get("subscription")
    if subscription:
        _save_subscription(subscription)

    # Push current node count immediately so the remote license panel reflects
    # this appliance's usage on the very first refresh, not just after the
    # next periodic check-in.
    try:
        node_count = await _count_nodes()
        async with httpx.AsyncClient(timeout=15, verify=True) as client:
            ci = await client.post(
                f"{server_url}/api/v1/appliances/checkin",
                headers={
                    "X-Appliance-ID": appliance_id,
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "hostname": hostname,
                    "arch": arch,
                    "current_version": version,
                    "node_count": node_count,
                },
            )
            if ci.status_code == 200:
                fresh = ci.json().get("subscription")
                if fresh:
                    _save_subscription(fresh)
                    subscription = fresh
    except Exception:
        pass  # best-effort — registration already succeeded

    # Sync remote subscription into the local subscriptions DB table so the
    # Subscription tab reflects the real plan instead of hardcoded trial defaults.
    if subscription:
        try:
            from app.api.v1.subscription import _sync_from_remote
            from app.core.database import AsyncSessionLocal as async_session_factory
            from app.models.subscription import Subscription as SubModel
            from sqlalchemy import select as sa_select

            reg_token = body.license_key

            async def _sync_local_db():
                async with async_session_factory() as session:
                    res = await session.execute(
                        sa_select(SubModel).order_by(SubModel.created_at.desc()).limit(1)
                    )
                    sub = res.scalar_one_or_none()
                    if sub:
                        _sync_from_remote(sub, subscription)
                        sub.license_key = reg_token
                        await session.commit()

            import asyncio
            asyncio.ensure_future(_sync_local_db())
        except Exception:
            pass  # best-effort; subscription.json is the source of truth

    result = {
        "status": "ok",
        "appliance_id": appliance_id,
        "message": "Appliance registered successfully",
    }
    if subscription:
        result["subscription"] = subscription
    await write_audit_log(
        db,
        actor=user,
        action="system.register_appliance",
        resource_type="appliance",
        resource_id=appliance_id,
        metadata={"server_url": server_url, "has_subscription": bool(subscription)},
    )
    await db.commit()
    return result


@router.post("/reset-registration")
async def reset_registration(
    user: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Clear local registration so a new license key can be entered.

    Admin-only recovery path. This does NOT notify the remote OTA server —
    the operator is responsible for asking the admin to free the slot.
    """
    if getattr(user, "role", "") not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Admin role required")

    config = _read_config()
    if not config.has_section("appliance"):
        return {"status": "ok", "message": "Nothing to reset"}

    config.set("appliance", "id", "")
    config.set("appliance", "api_key", "")
    _write_config(config)
    try:
        os.chmod(str(CONFIG_PATH), 0o600)
    except OSError:
        pass

    # Drop the cached subscription so the UI reflects the new state
    try:
        if SUBSCRIPTION_PATH.exists():
            SUBSCRIPTION_PATH.unlink()
    except OSError:
        pass

    await write_audit_log(
        db,
        actor=user,
        action="system.reset_registration",
        resource_type="appliance",
        resource_id="local",
    )
    await db.commit()
    return {
        "status": "ok",
        "message": (
            "Registration cleared. Enter a new license key in Settings > Subscription "
            "and ask your OTA admin to free the old appliance slot."
        ),
    }


@router.get("/subscription")
async def get_remote_subscription(user: User = Depends(require_admin_user)):
    """Get cached subscription info from the OTA server.

    This returns the subscription data received during the last
    registration or check-in with zentryc.com.
    """
    sub = _load_subscription()
    if not sub:
        return {"subscription": None, "message": "No subscription data cached. Registration or check-in required."}
    return {"subscription": sub}


@router.post("/refresh-subscription")
async def refresh_subscription(
    user: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Push node count to zentryc.com via check-in and pull fresh subscription.

    We deliberately use the check-in endpoint (not GET /appliances/subscription)
    because the dashboard's Refresh button is the natural moment to report this
    appliance's node usage upstream — node_count is required for the remote
    license panel to show accurate used_node_count / available_nodes.

    Any release advice in the response is ignored here; the periodic updater
    timer is responsible for actually applying updates.
    """
    import httpx
    import platform

    config = _read_config()
    server_url = config.get("server", "url", fallback="https://zentryc.com")
    appliance_id = config.get("appliance", "id", fallback="")
    api_key = config.get("appliance", "api_key", fallback="")

    if not appliance_id or not api_key:
        raise HTTPException(status_code=400, detail="Appliance not registered")

    version, _ = _get_version_info()
    node_count = await _count_nodes()

    machine = platform.machine().lower()
    arch = "amd64" if machine in ("x86_64", "amd64") else "arm64" if machine in ("aarch64", "arm64") else machine

    try:
        async with httpx.AsyncClient(timeout=30, verify=True) as client:
            resp = await client.post(
                f"{server_url}/api/v1/appliances/checkin",
                headers={
                    "X-Appliance-ID": appliance_id,
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "hostname": platform.node(),
                    "arch": arch,
                    "current_version": version,
                    "node_count": node_count,
                },
            )

        if resp.status_code == 404:
            return {"subscription": None, "message": "Appliance is not linked to any subscription"}

        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=_extract_remote_error(resp))

        data = resp.json()
        subscription = data.get("subscription")
        if subscription:
            _save_subscription(subscription)

        await write_audit_log(
            db,
            actor=user,
            action="system.refresh_subscription",
            resource_type="appliance",
            resource_id=appliance_id,
            metadata={"node_count": node_count, "has_subscription": bool(subscription)},
        )
        await db.commit()
        return {"subscription": subscription, "node_count": node_count}

    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach update server: {e}")


# ─── Health ───────────────────────────────────────────────────────────────────

@router.get("/health")
async def system_health():
    """Comprehensive health check."""
    checks = {}
    checks["api"] = "ok"

    try:
        result = subprocess.run(
            ["pg_isready", "-h", "127.0.0.1", "-p", "5432"],
            capture_output=True, text=True, timeout=5,
        )
        checks["postgresql"] = "ok" if result.returncode == 0 else "error"
    except Exception:
        checks["postgresql"] = "error"

    try:
        import httpx as _httpx
        r = _httpx.get("http://127.0.0.1:8123/ping", timeout=3)
        checks["clickhouse"] = "ok" if r.status_code == 200 else "error"
    except Exception:
        checks["clickhouse"] = "error"

    try:
        result = subprocess.run(
            ["redis-cli", "-h", "127.0.0.1", "ping"],
            capture_output=True, text=True, timeout=5,
        )
        out = result.stdout.strip()
        checks["redis"] = "ok" if out == "PONG" or "NOAUTH" in out else "error"
    except Exception:
        checks["redis"] = "error"

    for svc in ["zenplus-api", "zenplus-poller", "nginx"]:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5,
            )
            checks[svc] = result.stdout.strip()
        except Exception:
            checks[svc] = "unknown"

    overall = "ok" if all(v in ("ok", "active") for v in checks.values()) else "degraded"
    version, _ = _get_version_info()

    return {"status": overall, "version": version, "checks": checks}


# ─── Storage ─────────────────────────────────────────────────────────────────

DATA_MOUNT = "/data"
VG_NAME = "ubuntu-vg"
LV_NAME = "data-lv"


class DiskInfo(BaseModel):
    name: str
    size_bytes: int
    size_human: str
    type: str
    mountpoint: str
    in_use: bool


class PhysicalVolume(BaseModel):
    name: str
    size_bytes: int
    size_human: str
    free_bytes: int
    free_human: str


class StorageBreakdown(BaseModel):
    name: str
    path: str
    size_bytes: int
    size_human: str


class ClickHouseStorageCheck(BaseModel):
    ok: bool
    host_path: str           # where data lives on the host (/data/clickhouse)
    container_path: str      # where the container sees it (/var/lib/clickhouse)
    host_path_exists: bool
    on_data_volume: bool     # host_path is on the same filesystem as /data
    bind_mount_ok: bool      # docker-compose declares the expected bind mount
    clickhouse_reported_path: str  # what `system.disks` returns (empty if unreachable)
    message: str


class StorageStatus(BaseModel):
    # /data volume
    total_bytes: int
    used_bytes: int
    free_bytes: int
    usage_percent: float
    filesystem: str
    mount_point: str
    # LVM info
    vg_name: str
    vg_total_bytes: int
    vg_free_bytes: int
    lv_name: str
    lv_size_bytes: int
    pv_count: int
    physical_volumes: list[str]           # legacy: names only, kept for compatibility
    pv_details: list[PhysicalVolume]      # per-PV size + free space
    unclaimed_vg_bytes: int               # vg_free_bytes not yet claimed by data-lv
    # Breakdown by component
    breakdown: list[StorageBreakdown]
    # ClickHouse specifics
    clickhouse_table_sizes: list[dict]
    clickhouse_total_bytes: int
    clickhouse_storage: ClickHouseStorageCheck
    # Available disks for expansion
    available_disks: list[DiskInfo]
    # Health
    health: str  # ok / warning / critical
    health_message: str


def _human_size(size_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _get_mount_info() -> dict:
    """Get filesystem info for /data mount."""
    try:
        stat = os.statvfs(DATA_MOUNT)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        return {
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "usage_percent": round((used / total) * 100, 1) if total > 0 else 0,
        }
    except OSError:
        return {"total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "usage_percent": 0}


def _get_filesystem_type() -> str:
    """Get filesystem type for /data."""
    try:
        result = subprocess.run(
            ["df", "-T", DATA_MOUNT],
            capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            return lines[1].split()[1]
    except Exception:
        pass
    return "unknown"


def _get_lvm_info() -> dict:
    """Get LVM volume group and logical volume info."""
    info = {
        "vg_total_bytes": 0, "vg_free_bytes": 0,
        "lv_size_bytes": 0, "pv_count": 0,
        "physical_volumes": [],     # list[str] — legacy
        "pv_details": [],           # list[dict] — name, size, free
    }
    try:
        # VG info
        result = subprocess.run(
            ["sudo", "/usr/sbin/vgs", VG_NAME, "--noheadings", "--nosuffix", "--units", "b",
             "-o", "vg_size,vg_free,pv_count"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if len(parts) >= 3:
                info["vg_total_bytes"] = int(float(parts[0]))
                info["vg_free_bytes"] = int(float(parts[1]))
                info["pv_count"] = int(parts[2])

        # LV info
        result = subprocess.run(
            ["sudo", "/usr/sbin/lvs", f"{VG_NAME}/{LV_NAME}", "--noheadings", "--nosuffix",
             "--units", "b", "-o", "lv_size"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            info["lv_size_bytes"] = int(float(result.stdout.strip()))

        # PV list with size + free
        result = subprocess.run(
            ["sudo", "/usr/sbin/pvs", "--noheadings", "--nosuffix", "--units", "b",
             "-o", "pv_name,pv_size,pv_free,vg_name"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) < 4 or parts[3] != VG_NAME:
                    continue
                pv_name = parts[0]
                pv_size = int(float(parts[1]))
                pv_free = int(float(parts[2]))
                info["physical_volumes"].append(pv_name)
                info["pv_details"].append({
                    "name": pv_name,
                    "size_bytes": pv_size,
                    "size_human": _human_size(pv_size),
                    "free_bytes": pv_free,
                    "free_human": _human_size(pv_free),
                })
    except Exception:
        pass
    return info


def _get_dir_size(path: str) -> int:
    """Get total size of a directory using du."""
    try:
        result = subprocess.run(
            ["sudo", "du", "-sb", path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return int(result.stdout.split()[0])
    except Exception:
        pass
    return 0


def _get_breakdown() -> list[dict]:
    """Get storage breakdown by component directory."""
    dirs = [
        ("ClickHouse", f"{DATA_MOUNT}/clickhouse"),
        ("PostgreSQL", f"{DATA_MOUNT}/postgresql"),
        ("Redis", f"{DATA_MOUNT}/redis"),
        ("Backups", f"{DATA_MOUNT}/backups"),
        ("Temp", f"{DATA_MOUNT}/tmp"),
    ]
    breakdown = []
    for name, path in dirs:
        if os.path.isdir(path):
            size = _get_dir_size(path)
            breakdown.append({
                "name": name,
                "path": path,
                "size_bytes": size,
                "size_human": _human_size(size),
            })
    return breakdown


def _get_clickhouse_table_sizes() -> tuple[list[dict], int]:
    """Query ClickHouse for per-table storage sizes."""
    tables = []
    total = 0
    try:
        import httpx as _httpx
        query = (
            "SELECT database, table, "
            "sum(bytes_on_disk) AS size_bytes, "
            "sum(rows) AS total_rows, "
            "count() AS part_count "
            "FROM system.parts "
            "WHERE active AND database = 'zenplus' "
            "GROUP BY database, table "
            "ORDER BY size_bytes DESC "
            "FORMAT JSON"
        )
        ch_user = os.environ.get("CLICKHOUSE_USER", "default")
        ch_pass = os.environ.get("CLICKHOUSE_PASSWORD", "")
        r = _httpx.get(
            "http://127.0.0.1:8123/",
            params={"query": query, "user": ch_user, "password": ch_pass},
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            for row in data.get("data", []):
                size = int(row["size_bytes"])
                total += size
                tables.append({
                    "database": row["database"],
                    "table": row["table"],
                    "size_bytes": size,
                    "size_human": _human_size(size),
                    "rows": int(row["total_rows"]),
                    "parts": int(row["part_count"]),
                })
    except Exception:
        pass
    return tables, total


CLICKHOUSE_HOST_PATH = "/data/clickhouse"
CLICKHOUSE_CONTAINER_PATH = "/var/lib/clickhouse"
COMPOSE_FILE = "/opt/zenplus/docker-compose.yml"


def _get_clickhouse_storage_check() -> dict:
    """Verify ClickHouse data lives on /data and the bind mount is intact.

    The goal is to catch drift from the documented appliance layout:
      - host dir /data/clickhouse must exist
      - docker-compose must bind-mount /data/clickhouse to /var/lib/clickhouse
      - ClickHouse itself must report /var/lib/clickhouse/ as its data path
      - /data/clickhouse must live on the same filesystem as /data (so that
        when the data-lv grows, ClickHouse immediately benefits)
    """
    result = {
        "host_path": CLICKHOUSE_HOST_PATH,
        "container_path": CLICKHOUSE_CONTAINER_PATH,
        "host_path_exists": False,
        "on_data_volume": False,
        "bind_mount_ok": False,
        "clickhouse_reported_path": "",
        "ok": False,
        "message": "",
    }

    # (1) Host directory exists
    result["host_path_exists"] = os.path.isdir(CLICKHOUSE_HOST_PATH)

    # (2) Host dir sits on the same filesystem as /data (i.e. not a separate
    #     mount that would be skipped when data-lv grows).
    try:
        data_dev = os.stat(DATA_MOUNT).st_dev
        ch_dev = os.stat(CLICKHOUSE_HOST_PATH).st_dev if result["host_path_exists"] else 0
        result["on_data_volume"] = data_dev != 0 and data_dev == ch_dev
    except OSError:
        pass

    # (3) docker-compose declares the expected bind mount
    try:
        with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
            compose_text = f.read()
        expected = f"{CLICKHOUSE_HOST_PATH}:{CLICKHOUSE_CONTAINER_PATH}"
        result["bind_mount_ok"] = expected in compose_text
    except OSError:
        pass

    # (4) Ask ClickHouse itself where its data lives. This is best-effort —
    #     failure to reach ClickHouse doesn't fail the overall check, it just
    #     leaves the reported path empty.
    try:
        import httpx as _httpx
        ch_user = os.environ.get("CLICKHOUSE_USER", "default")
        ch_pass = os.environ.get("CLICKHOUSE_PASSWORD", "")
        r = _httpx.get(
            "http://127.0.0.1:8123/",
            params={
                "query": "SELECT path FROM system.disks WHERE name = 'default' FORMAT TSV",
                "user": ch_user,
                "password": ch_pass,
            },
            timeout=3,
        )
        if r.status_code == 200:
            result["clickhouse_reported_path"] = r.text.strip()
    except Exception:
        pass

    # Roll up health. ClickHouse reachability is a bonus, not a requirement,
    # because a storage panel must still render when the DB is down.
    reported = result["clickhouse_reported_path"]
    reported_ok = (not reported) or reported.rstrip("/") == CLICKHOUSE_CONTAINER_PATH.rstrip("/")

    result["ok"] = bool(
        result["host_path_exists"]
        and result["on_data_volume"]
        and result["bind_mount_ok"]
        and reported_ok
    )

    if result["ok"]:
        result["message"] = (
            "ClickHouse data is stored on the /data volume and will automatically "
            "grow when storage is expanded."
        )
    else:
        issues = []
        if not result["host_path_exists"]:
            issues.append(f"{CLICKHOUSE_HOST_PATH} does not exist")
        elif not result["on_data_volume"]:
            issues.append(f"{CLICKHOUSE_HOST_PATH} is not on the /data volume")
        if not result["bind_mount_ok"]:
            issues.append("docker-compose is missing the expected ClickHouse bind mount")
        if reported and not reported_ok:
            issues.append(
                f"ClickHouse reports its data path as '{reported}' "
                f"(expected '{CLICKHOUSE_CONTAINER_PATH}/')"
            )
        result["message"] = "ClickHouse storage configuration has drifted: " + "; ".join(issues)

    return result


def _get_available_disks() -> list[dict]:
    """Find block devices not currently used by LVM or mounted."""
    disks = []

    # Get list of PVs already in a VG
    pvs_in_vg = set()
    try:
        result = subprocess.run(
            ["sudo", "/usr/sbin/pvs", "--noheadings", "-o", "pv_name,vg_name"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].strip():
                    pvs_in_vg.add(parts[0].strip())
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for dev in data.get("blockdevices", []):
                if dev.get("type") != "disk":
                    continue
                dev_path = f"/dev/{dev['name']}"

                # Skip disks already in a VG
                if dev_path in pvs_in_vg:
                    continue

                # Check if any partition/child is in use (mounted or has a filesystem)
                children = dev.get("children", [])
                has_used_children = any(
                    c.get("mountpoint") or c.get("fstype")
                    for c in children
                )
                # Also skip if any child partition is a PV in a VG
                has_pv_children = any(
                    f"/dev/{c['name']}" in pvs_in_vg
                    for c in children
                )

                # A disk is available if: no children, no mountpoint, no fstype, not a PV in a VG
                if not children and not dev.get("mountpoint") and not dev.get("fstype") and not has_pv_children:
                    size = int(dev.get("size", 0))
                    disks.append({
                        "name": dev_path,
                        "size_bytes": size,
                        "size_human": _human_size(size),
                        "type": "disk",
                        "mountpoint": "",
                        "in_use": False,
                    })
                elif not has_used_children and not has_pv_children and not dev.get("mountpoint"):
                    # Disk with unused partitions — available but flagged
                    size = int(dev.get("size", 0))
                    disks.append({
                        "name": dev_path,
                        "size_bytes": size,
                        "size_human": _human_size(size),
                        "type": "disk",
                        "mountpoint": "",
                        "in_use": False,
                    })
    except Exception:
        pass
    return disks


@router.get("/storage")
async def get_storage_status(user: User = Depends(require_admin_user)):
    """Get comprehensive storage status for the /data volume."""
    mount_info = _get_mount_info()
    fs_type = _get_filesystem_type()
    lvm_info = _get_lvm_info()
    breakdown = _get_breakdown()
    ch_tables, ch_total = _get_clickhouse_table_sizes()
    ch_storage = _get_clickhouse_storage_check()
    available_disks = _get_available_disks()

    # Determine health. Usage-based critical/warning takes priority; when the
    # /data-lv is already oversized vs actual usage, an unclaimed-VG-space
    # warning only fires if nothing more important is going on.
    usage = mount_info["usage_percent"]
    unclaimed = lvm_info["vg_free_bytes"]
    # 1 GiB threshold — below this, rounding noise from LVM extents isn't
    # worth warning about.
    UNCLAIMED_THRESHOLD = 1 * 1024 * 1024 * 1024

    if usage >= 95:
        health = "critical"
        health_message = "Storage critically low! Expand storage immediately to avoid data loss."
    elif usage >= 85:
        health = "warning"
        health_message = "Storage running low. Consider expanding storage soon."
    elif usage >= 75:
        health = "warning"
        health_message = "Storage usage is above 75%. Plan for expansion."
    elif not ch_storage["ok"]:
        health = "warning"
        health_message = ch_storage["message"]
    elif unclaimed >= UNCLAIMED_THRESHOLD:
        health = "warning"
        health_message = (
            f"{_human_size(unclaimed)} of unclaimed space in the volume group. "
            "Click 'Grow Volume' to add it to /data."
        )
    else:
        health = "ok"
        health_message = "Storage usage is healthy."

    return StorageStatus(
        total_bytes=mount_info["total_bytes"],
        used_bytes=mount_info["used_bytes"],
        free_bytes=mount_info["free_bytes"],
        usage_percent=usage,
        filesystem=fs_type,
        mount_point=DATA_MOUNT,
        vg_name=VG_NAME,
        vg_total_bytes=lvm_info["vg_total_bytes"],
        vg_free_bytes=lvm_info["vg_free_bytes"],
        lv_name=LV_NAME,
        lv_size_bytes=lvm_info["lv_size_bytes"],
        pv_count=lvm_info["pv_count"],
        physical_volumes=lvm_info["physical_volumes"],
        pv_details=lvm_info["pv_details"],
        unclaimed_vg_bytes=unclaimed,
        breakdown=breakdown,
        clickhouse_table_sizes=ch_tables,
        clickhouse_total_bytes=ch_total,
        clickhouse_storage=ch_storage,
        available_disks=available_disks,
        health=health,
        health_message=health_message,
    )


class AddDiskRequest(BaseModel):
    disk: str  # e.g. "/dev/sdb"


EXPAND_SCRIPT = "/opt/zenplus/bin/expand-storage.sh"


@router.post("/storage/rescan")
async def rescan_disks(
    user: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Rescan SCSI bus to detect newly attached or resized disks."""
    errors = []
    rescanned = []
    try:
        # Find all SCSI host adapters and rescan each
        result = subprocess.run(
            ["bash", "-c", "ls /sys/class/scsi_host/"],
            capture_output=True, text=True, timeout=5,
        )
        hosts = result.stdout.strip().split()
        for host in hosts:
            scan_path = f"/sys/class/scsi_host/{host}/scan"
            try:
                subprocess.run(
                    ["sudo", "bash", "-c", f"echo '- - -' > {scan_path}"],
                    capture_output=True, text=True, timeout=10,
                )
                rescanned.append(host)
            except Exception as e:
                errors.append(f"{host}: {e}")

        # Also rescan existing block devices for size changes
        result = subprocess.run(
            ["bash", "-c", "ls /sys/class/block/sd*/device/rescan 2>/dev/null || true"],
            capture_output=True, text=True, timeout=5,
        )
        for rescan_path in result.stdout.strip().splitlines():
            if rescan_path:
                try:
                    subprocess.run(
                        ["sudo", "bash", "-c", f"echo 1 > {rescan_path}"],
                        capture_output=True, text=True, timeout=10,
                    )
                except Exception:
                    pass

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rescan failed: {e}")

    # Brief pause for kernel to settle
    import asyncio
    await asyncio.sleep(2)

    # Return updated disk list
    available = _get_available_disks()
    await write_audit_log(
        db,
        actor=user,
        action="system.storage_rescan",
        resource_type="storage",
        resource_id="/data",
        metadata={"rescanned_hosts": rescanned, "available_disks": len(available), "errors": errors},
    )
    await db.commit()
    return {
        "status": "ok",
        "message": f"Rescanned {len(rescanned)} SCSI hosts",
        "rescanned_hosts": rescanned,
        "available_disks": available,
        "errors": errors,
    }


@router.post("/storage/add-disk")
async def add_disk(
    body: AddDiskRequest,
    user: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a new disk to the LVM volume group and expand /data."""
    disk = body.disk

    # Validate disk path format — only allow /dev/sdX or /dev/vdX patterns
    if not disk.startswith("/dev/") or ".." in disk or ";" in disk or "|" in disk or "&" in disk or "$" in disk:
        raise HTTPException(status_code=400, detail="Invalid disk path")

    # Check it's a valid block device
    check = subprocess.run(
        ["test", "-b", disk],
        capture_output=True, text=True, timeout=5,
    )
    if check.returncode != 0:
        raise HTTPException(status_code=400, detail=f"{disk} is not a valid block device. Try clicking 'Rescan Disks' first.")

    # Check it's not already a PV in our VG
    check = subprocess.run(
        ["sudo", "/usr/sbin/pvs", disk, "--noheadings", "-o", "vg_name"],
        capture_output=True, text=True, timeout=5,
    )
    if check.returncode == 0:
        vg = check.stdout.strip()
        if vg == VG_NAME:
            raise HTTPException(status_code=400, detail=f"{disk} is already part of the {VG_NAME} volume group")

    # Run expand-storage.sh (script has its own preflight check for read-only FS)
    result = subprocess.run(
        ["sudo", EXPAND_SCRIPT, disk],
        capture_output=True, text=True, timeout=120,
    )

    if result.returncode != 0:
        stderr = result.stderr or result.stdout
        # Provide user-friendly error messages
        if "read-only" in stderr.lower():
            detail = "Root filesystem is read-only. Cannot archive LVM metadata. Please check disk health or reboot the appliance."
        elif "already a physical volume" in stderr.lower():
            detail = f"{disk} is already initialized. Try using 'Grow Volume' instead."
        elif "not a valid block device" in stderr.lower():
            detail = f"{disk} was not found. It may have been detached. Click 'Rescan Disks' to refresh."
        else:
            detail = f"Storage expansion failed: {stderr.strip()}"
        raise HTTPException(status_code=500, detail=detail)

    # Get updated status
    mount_info = _get_mount_info()

    await write_audit_log(
        db,
        actor=user,
        action="system.storage_add_disk",
        resource_type="storage",
        resource_id=disk,
        metadata={"new_total": mount_info["total_bytes"], "new_free": mount_info["free_bytes"]},
    )
    await db.commit()
    return {
        "status": "ok",
        "message": f"Disk {disk} added successfully. Storage expanded to {_human_size(mount_info['total_bytes'])}.",
        "output": result.stdout,
        "new_total": mount_info["total_bytes"],
        "new_free": mount_info["free_bytes"],
        "new_usage_percent": mount_info["usage_percent"],
    }


@router.post("/storage/grow")
async def grow_volume(
    user: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Grow the LV and filesystem after a VM disk resize or PV expansion."""
    # First try to resize any existing PVs (picks up VM disk growth)
    lvm_info = _get_lvm_info()
    for pv in lvm_info["physical_volumes"]:
        subprocess.run(
            ["sudo", "/usr/sbin/pvresize", pv],
            capture_output=True, text=True, timeout=30,
        )

    # Run expand-storage.sh --grow
    result = subprocess.run(
        ["sudo", EXPAND_SCRIPT, "--grow"],
        capture_output=True, text=True, timeout=120,
    )

    if result.returncode != 0:
        combined = result.stderr + result.stdout
        # If there's no free space, that's not an error
        if "no free extents" in combined.lower() or "no free space" in combined.lower() or result.returncode == 5:
            return {
                "status": "ok",
                "message": "No free space in volume group. Resize the VM disk or add a new disk first.",
                "output": combined,
                "grew": False,
            }
        if "read-only" in combined.lower():
            raise HTTPException(
                status_code=500,
                detail="Root filesystem is read-only. Cannot expand storage. Please check disk health or reboot.",
            )
        raise HTTPException(
            status_code=500,
            detail=f"Grow failed: {(result.stderr or result.stdout).strip()}",
        )

    mount_info = _get_mount_info()

    await write_audit_log(
        db,
        actor=user,
        action="system.storage_grow",
        resource_type="storage",
        resource_id="/data",
        metadata={"new_total": mount_info["total_bytes"], "new_free": mount_info["free_bytes"]},
    )
    await db.commit()
    return {
        "status": "ok",
        "message": f"Volume expanded successfully to {_human_size(mount_info['total_bytes'])}.",
        "output": result.stdout,
        "grew": True,
        "new_total": mount_info["total_bytes"],
        "new_free": mount_info["free_bytes"],
        "new_usage_percent": mount_info["usage_percent"],
    }
