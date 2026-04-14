import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.device import Device
from app.models.service_check import ServiceCheck
from app.models.subscription import Subscription

SUBSCRIPTION_JSON = Path("/opt/zenplus/updater/config/subscription.json")

router = APIRouter(prefix="/subscription", tags=["Subscription"])


PLAN_LIMITS = {
    "trial": {"max_devices": 50, "max_service_checks": 20, "max_users": 5, "duration_days": 30},
    "starter": {"max_devices": 100, "max_service_checks": 50, "max_users": 10, "duration_days": 365},
    "professional": {"max_devices": 500, "max_service_checks": 200, "max_users": 25, "duration_days": 365},
    "enterprise": {"max_devices": 10000, "max_service_checks": 5000, "max_users": 100, "duration_days": 365},
}

PLAN_FEATURES = {
    "trial": [
        "Up to 50 devices",
        "Up to 20 service checks",
        "Up to 5 users",
        "Email & SMS notifications",
        "Basic reporting",
        "30-day data retention",
    ],
    "starter": [
        "Up to 100 devices",
        "Up to 50 service checks",
        "Up to 10 users",
        "Email & SMS notifications",
        "Basic reporting",
        "90-day data retention",
    ],
    "professional": [
        "Up to 500 devices",
        "Up to 200 service checks",
        "Up to 25 users",
        "All notification channels",
        "Advanced reporting & exports",
        "90-day data retention",
        "Priority support",
    ],
    "enterprise": [
        "Up to 10,000 devices",
        "Up to 5,000 service checks",
        "Up to 100 users",
        "All notification channels",
        "Custom reporting & API access",
        "1-year data retention",
        "Dedicated support",
        "Custom integrations",
    ],
}


class SubscriptionOut(BaseModel):
    id: str
    plan: str
    status: str
    started_at: datetime
    expires_at: Optional[datetime]
    max_devices: int
    max_service_checks: int
    max_users: int
    license_key: Optional[str]
    activated_by: Optional[str]
    days_remaining: Optional[int]
    usage: dict
    features: list[str]

    model_config = {"from_attributes": True}


class ActivateLicenseRequest(BaseModel):
    license_key: str


def _load_remote_subscription() -> dict | None:
    """Load the OTA subscription data cached by the updater agent."""
    if not SUBSCRIPTION_JSON.exists():
        return None
    try:
        return json.loads(SUBSCRIPTION_JSON.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _sync_from_remote(sub: Subscription, remote: dict) -> bool:
    """Update a local Subscription row with data from the OTA server.

    Returns True if any field was changed.
    """
    changed = False
    remote_plan = remote.get("plan", "").lower()
    if not remote_plan:
        return False

    if sub.plan != remote_plan:
        sub.plan = remote_plan
        changed = True

    # Map max_devices (or max_appliances) from OTA → max_devices locally
    remote_max = remote.get("max_devices") or remote.get("max_appliances")
    if remote_max is not None and sub.max_devices != remote_max:
        sub.max_devices = remote_max
        changed = True

    # Derive service-check and user limits from plan tier (only if plan is known)
    limits = PLAN_LIMITS.get(remote_plan)
    if limits:
        for local_attr, limit_key in [("max_service_checks", "max_service_checks"),
                                       ("max_users", "max_users")]:
            expected = limits.get(limit_key)
            if expected is not None and getattr(sub, local_attr) != expected:
                setattr(sub, local_attr, expected)
                changed = True

    # Expiry: null from OTA means "never expires"
    if "expires_at" in remote:
        remote_expires = remote["expires_at"]
        if remote_expires is None:
            if sub.expires_at is not None:
                sub.expires_at = None
                changed = True
        else:
            parsed = datetime.fromisoformat(remote_expires)
            if sub.expires_at != parsed:
                sub.expires_at = parsed
                changed = True

    # Status
    is_active = remote.get("is_active", True)
    is_expired = remote.get("is_expired", False)
    new_status = "expired" if is_expired else ("active" if is_active else "inactive")
    if sub.status != new_status:
        sub.status = new_status
        changed = True

    # Store the subscription name as activated_by for display
    remote_name = remote.get("name")
    if remote_name and sub.activated_by != remote_name:
        sub.activated_by = remote_name
        changed = True

    return changed


@router.get("")
async def get_subscription(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get current subscription details with usage stats."""
    result = await db.execute(
        select(Subscription).order_by(Subscription.created_at.desc()).limit(1)
    )
    sub = result.scalar_one_or_none()

    # Auto-create trial if no subscription exists
    if not sub:
        now = datetime.now(timezone.utc)
        sub = Subscription(
            plan="trial",
            status="active",
            started_at=now,
            expires_at=now + timedelta(days=30),
            max_devices=50,
            max_service_checks=20,
            max_users=5,
            activated_by=current_user.username,
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)

    # Sync with remote OTA subscription data if available
    remote = _load_remote_subscription()
    if remote and remote.get("plan"):
        if _sync_from_remote(sub, remote):
            await db.commit()
            await db.refresh(sub)

    # Check if expired
    now = datetime.now(timezone.utc)
    if sub.expires_at is not None and sub.expires_at < now and sub.status == "active":
        sub.status = "expired"
        await db.commit()
        await db.refresh(sub)

    # Get usage stats
    device_count = (await db.execute(select(func.count(Device.id)))).scalar() or 0
    check_count = (await db.execute(select(func.count(ServiceCheck.id)))).scalar() or 0
    user_count = (await db.execute(select(func.count(User.id)).where(User.is_active == True))).scalar() or 0

    # Use days_remaining from remote server if available, otherwise compute locally
    if remote and "days_remaining" in remote:
        days_remaining = remote["days_remaining"]
    elif sub.expires_at is None:
        days_remaining = None
    else:
        days_remaining = max(0, (sub.expires_at - now).days)

    return SubscriptionOut(
        id=str(sub.id),
        plan=sub.plan,
        status=sub.status,
        started_at=sub.started_at,
        expires_at=sub.expires_at,
        max_devices=sub.max_devices,
        max_service_checks=sub.max_service_checks,
        max_users=sub.max_users,
        license_key=sub.license_key,
        activated_by=sub.activated_by,
        days_remaining=days_remaining,
        usage={
            "devices": device_count,
            "service_checks": check_count,
            "users": user_count,
        },
        features=PLAN_FEATURES.get(sub.plan, []),
    )


@router.get("/plans")
async def list_plans(current_user: User = Depends(get_current_user)):
    """List available subscription plans."""
    plans = []
    for plan_id, limits in PLAN_LIMITS.items():
        plans.append({
            "id": plan_id,
            "name": plan_id.replace("_", " ").title(),
            "limits": limits,
            "features": PLAN_FEATURES.get(plan_id, []),
        })
    return plans


@router.post("/activate")
async def activate_license(
    data: ActivateLicenseRequest,
    current_user: User = Depends(get_current_user),
):
    """Deprecated — use POST /system/register instead.

    License activation is now handled through OTA appliance registration,
    which registers with zentryc.com and syncs subscription data automatically.
    """
    raise HTTPException(
        status_code=410,
        detail="This endpoint is deprecated. Use the License Activation in the Subscription tab "
               "which registers via POST /api/v1/system/register.",
    )
