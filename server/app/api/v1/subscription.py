from datetime import datetime, timedelta, timezone
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

router = APIRouter(prefix="/subscription", tags=["Subscription"])


PLAN_LIMITS = {
    "trial": {"max_devices": 50, "max_service_checks": 20, "max_users": 5, "duration_days": 30},
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
    expires_at: datetime
    max_devices: int
    max_service_checks: int
    max_users: int
    license_key: Optional[str]
    activated_by: Optional[str]
    days_remaining: int
    usage: dict
    features: list[str]

    model_config = {"from_attributes": True}


class ActivateLicenseRequest(BaseModel):
    license_key: str


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

    # Check if expired
    now = datetime.now(timezone.utc)
    if sub.expires_at < now and sub.status == "active":
        sub.status = "expired"
        await db.commit()
        await db.refresh(sub)

    # Get usage stats
    device_count = (await db.execute(select(func.count(Device.id)))).scalar() or 0
    check_count = (await db.execute(select(func.count(ServiceCheck.id)))).scalar() or 0
    user_count = (await db.execute(select(func.count(User.id)).where(User.is_active == True))).scalar() or 0

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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Activate a license key. (Placeholder for remote licensing server integration.)"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    # For now, just store the key — later will validate against remote server
    result = await db.execute(
        select(Subscription).order_by(Subscription.created_at.desc()).limit(1)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="No subscription found")

    sub.license_key = data.license_key
    sub.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return {"message": "License key stored. Remote validation will be available in a future update."}
