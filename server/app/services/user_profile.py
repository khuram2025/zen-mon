"""Validation and synchronization of user profile fields."""

import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

_EMAIL_RE = re.compile(r'^[^@\s<>()\[\],;:\\"]+@[^@\s<>()\[\],;:\\"]+\.[^@\s<>()\[\],;:\\"]{2,}$')


def clean_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 255 or not _EMAIL_RE.fullmatch(email):
        raise ValueError("Enter a valid email address")
    return email


async def sync_ldap_profile(db: AsyncSession, user: User, info: dict) -> None:
    user.full_name = (info.get("full_name") or "").strip()[:255] or None
    try:
        email = clean_email(info.get("email") or "")
    except ValueError:
        user.email = None
        return

    if email == user.email:
        return
    conflicting_user = await db.scalar(
        select(User.id).where(func.lower(User.email) == email, User.id != user.id).limit(1)
    )
    if conflicting_user is not None:
        user.email = None
        return

    # Another account may claim the address between the check and the write.
    # Losing that race must leave the LDAP account usable with a blank email.
    try:
        async with db.begin_nested():
            user.email = email
            await db.flush()
    except IntegrityError as exc:
        if "users_email_key" not in str(exc.orig).lower():
            raise
        await db.refresh(user)
        user.email = None
