"""System update status and control endpoints for the dashboard."""

import configparser
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/system", tags=["System Updates"])

UPDATER_DIR = Path("/opt/zenplus/updater")
CONFIG_PATH = UPDATER_DIR / "config" / "agent.conf"
VERSION_FILE = Path("/opt/zenplus/.version")
LOG_FILE = UPDATER_DIR / "logs" / "update.log"


class UpdateConfig(BaseModel):
    auto_update: bool = True
    check_interval_hours: int = 4
    maintenance_window_start: str = ""
    maintenance_window_end: str = ""


class UpdateStatus(BaseModel):
    current_version: str
    appliance_id: str
    server_url: str
    auto_update: bool
    check_interval_hours: int
    maintenance_window_start: str
    maintenance_window_end: str
    last_check: str
    next_check: str
    timer_active: bool
    recent_log: list[str]


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


@router.get("/update-status", response_model=UpdateStatus)
async def get_update_status(user: User = Depends(get_current_user)):
    """Get current update agent status."""
    config = _read_config()
    timer = _get_timer_info()

    # Current version
    version = "unknown"
    if VERSION_FILE.exists():
        lines = VERSION_FILE.read_text().strip().splitlines()
        if lines:
            version = lines[0].strip()

    # Recent log lines
    recent_log = []
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text().strip().splitlines()
            recent_log = lines[-20:]  # last 20 lines
        except Exception:
            pass

    interval_sec = config.getint("server", "check_interval_seconds", fallback=14400)

    return UpdateStatus(
        current_version=version,
        appliance_id=config.get("appliance", "id", fallback=""),
        server_url=config.get("server", "url", fallback="https://zentryc.com"),
        auto_update=config.getboolean("update", "auto_update", fallback=True),
        check_interval_hours=interval_sec // 3600,
        maintenance_window_start=config.get("update", "maintenance_window_start", fallback=""),
        maintenance_window_end=config.get("update", "maintenance_window_end", fallback=""),
        last_check=timer.get("last", ""),
        next_check=timer.get("next", ""),
        timer_active=timer.get("active", False),
        recent_log=recent_log,
    )


@router.put("/update-config")
async def update_config(body: UpdateConfig, user: User = Depends(get_current_user)):
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

    # Update systemd timer interval
    timer_path = Path("/etc/systemd/system/zenplus-updater.timer")
    if timer_path.exists():
        try:
            content = timer_path.read_text()
            # Replace OnUnitActiveSec line
            import re
            content = re.sub(
                r"OnUnitActiveSec=.*",
                f"OnUnitActiveSec={body.check_interval_hours}h",
                content,
            )
            timer_path.write_text(content)
            subprocess.run(["systemctl", "daemon-reload"], timeout=10)
        except Exception:
            pass

    return {"status": "ok", "message": "Configuration updated"}


@router.post("/check-update")
async def trigger_check(user: User = Depends(get_current_user)):
    """Trigger an on-demand update check."""
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "start", "zenplus-updater.service"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Failed to trigger: {result.stderr}")
        return {"status": "ok", "message": "Update check triggered"}
    except subprocess.TimeoutExpired:
        return {"status": "ok", "message": "Update check triggered (running)"}
