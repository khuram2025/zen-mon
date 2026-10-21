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

from app.core.crypto import is_configured as snmp_crypto_is_configured
from app.core.database import get_db
from app.core.security import require_admin_user
from app.models.user import User
from app.services.audit_service import write_audit_log
from app.services.license_capacity import annotate_snapshot

router = APIRouter(prefix="/system", tags=["System Updates"])

UPDATER_DIR = Path("/opt/zenplus/updater")
CONFIG_PATH = UPDATER_DIR / "config" / "agent.conf"
SUBSCRIPTION_PATH = UPDATER_DIR / "config" / "subscription.json"
VERSION_FILE = Path("/opt/zenplus/.version")
LOG_FILE = UPDATER_DIR / "logs" / "update.log"
HISTORY_FILE = UPDATER_DIR / "logs" / "update-history.json"
TIMER_DROPIN_DIR = "/etc/systemd/system/zenplus-updater.timer.d"
TIMER_DROPIN_FILE = f"{TIMER_DROPIN_DIR}/override.conf"
# Root-owned helper (scripts/setup-updater-privileges.sh) that takes a number
# of hours and writes a fixed [Timer] drop-in. sudo grants exactly
# `<helper> set-interval *`; the helper validates the number.
TIMER_HELPER = "/usr/local/sbin/zenplus-updater-timer-helper"
TIMER_MIN_HOURS = 1
TIMER_MAX_HOURS = 168
# The VMware appliance image routes sudo through a typed broker that accepts
# only the original fixed drop-in text (and writes it with its own helper).
TYPED_PRIVILEGE_BROKER = "/usr/local/libexec/zentryc-privileged"


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


class SchemaHealth(BaseModel):
    """Whether the database schema matches the code this version expects.

    A version number alone proved to be a lie: an appliance ran v1.6.0 with its
    ClickHouse SNMP tables missing and reported "success" here, because the
    post-update health check only asked whether the API answered HTTP 200.
    """
    ok: Optional[bool] = None          # None = never checked
    checked_at: str = ""
    problem_count: int = 0
    problems: list[str] = []
    summary: str = ""


class UpdateStatus(BaseModel):
    current_version: str
    schema_health: Optional[SchemaHealth] = None
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
        if not data:
            return None
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
    """License node count: devices + servers, a linked device/server pair once.

    The definition lives in app.services.license_capacity so the count
    reported to zentryc.com is the count this appliance enforces. Runs in a
    fresh session and degrades to 0 on DB failure — license sync should never
    block on a transient query error.
    """
    from app.core.database import AsyncSessionLocal
    from app.services.license_capacity import count_nodes
    try:
        async with AsyncSessionLocal() as session:
            return await count_nodes(session)
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

def _read_schema_health() -> SchemaHealth:
    """Read the verdict scripts/sync-schema.py writes on every update.

    Reported next to the version number so an operator can see that code and
    schema agree — the thing this page could not previously tell them.
    """
    status_file = Path("/opt/zenplus/.schema-status.json")
    if not status_file.exists():
        return SchemaHealth(
            ok=None,
            summary="Not verified yet — runs on the next update, or "
                    "`sudo /opt/zenplus/scripts/sync-schema.py --check`",
        )
    try:
        data = json.loads(status_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return SchemaHealth(ok=None, summary=f"Verdict file unreadable: {e}")

    problems = [str(p) for p in (data.get("problems") or [])]
    ok = bool(data.get("ok"))
    pg = data.get("postgres") or {}
    ch = data.get("clickhouse") or {}

    if ok:
        applied = len(pg.get("applied", [])) + len(ch.get("applied", []))
        healed = len(ch.get("healed", []))
        parts = ["Schema matches this version"]
        if applied:
            parts.append(f"{applied} migration(s) applied")
        if healed:
            parts.append(f"{healed} repaired")
        summary = " — ".join(parts)
    else:
        summary = f"{len(problems)} unresolved schema problem(s)"

    return SchemaHealth(
        ok=ok,
        checked_at=data.get("checked_at", ""),
        problem_count=len(problems),
        problems=problems[:20],
        summary=summary,
    )


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
        schema_health=_read_schema_health(),
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
    """Set the updater timer interval without touching the shipped unit file.

    The root helper accepts only a number of hours and writes a fixed
    ``[Timer]`` drop-in, then reloads systemd and restarts the timer. The API
    never supplies drop-in text, so it cannot add other systemd directives.
    """
    hours = max(TIMER_MIN_HOURS, int(check_interval_hours))
    if hours > TIMER_MAX_HOURS:
        return False, f"interval must be {TIMER_MIN_HOURS} through {TIMER_MAX_HOURS} hours"

    if os.path.exists(TYPED_PRIVILEGE_BROKER):
        return _write_timer_override_via_broker(hours)

    try:
        result = subprocess.run(
            ["sudo", "-n", TIMER_HELPER, "set-interval", str(hours)],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "sudo command timed out"
    except Exception as e:
        return False, str(e)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or "unknown error"
        return False, f"timer helper failed: {detail}"
    return True, ""


def _write_timer_override_via_broker(hours: int) -> tuple[bool, str]:
    """Typed-broker form used by the VMware appliance image.

    Its broker accepts this exact drop-in text only, extracts the number and
    hands it to its own fixed-template helper.
    """
    override_body = (
        "# Managed by zenplus-api — do not edit by hand.\n"
        "[Timer]\n"
        "OnUnitActiveSec=\n"
        f"OnUnitActiveSec={hours}h\n"
    )

    try:
        mkdir = subprocess.run(
            ["sudo", "-n", "/bin/mkdir", "-p", TIMER_DROPIN_DIR],
            capture_output=True, text=True, timeout=10,
        )
        if mkdir.returncode != 0:
            return False, f"mkdir failed: {mkdir.stderr.strip()}"

        tee = subprocess.run(
            ["sudo", "-n", "/usr/bin/tee", TIMER_DROPIN_FILE],
            input=override_body, capture_output=True, text=True, timeout=30,
        )
        if tee.returncode != 0:
            return False, f"tee failed: {tee.stderr.strip()}"
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
                    # Remember what was reported so the shared node pool can be
                    # split into this appliance's and the others' usage.
                    _save_subscription(annotate_snapshot(fresh, node_count))
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
            # The directory is root-owned to prevent Python module planting.
            # Clear the pre-created app-writable data file in place.
            SUBSCRIPTION_PATH.write_text("{}\n")
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
            _save_subscription(annotate_snapshot(subscription, node_count))

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

    checks["snmp_encryption"] = "ok" if snmp_crypto_is_configured() else "error"

    overall = "ok" if all(v in ("ok", "active") for v in checks.values()) else "degraded"
    version, _ = _get_version_info()

    return {"status": overall, "version": version, "checks": checks}


# Storage status and disk/volume management moved to storage_management.py
# (same /api/v1/system/storage URLs).
