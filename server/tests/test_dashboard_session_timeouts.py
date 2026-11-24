from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
import jwt
from app.core.config import get_settings

from app.core.security import create_access_token, create_stream_ticket
from app.services import session_service


def test_wallboard_session_and_stream_ticket_have_independent_lifetimes():
    claims = {"sub": "test-account", "jti": "test-session"}
    settings = get_settings()
    def decode(token):
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    fixed = decode(create_access_token(claims))
    assert fixed["exp"] > fixed["iat"]
    managed = decode(create_access_token(claims, server_managed_session=True))
    assert "exp" not in managed and managed["jti"] == "test-session"
    stream = decode(create_stream_ticket(SimpleNamespace(id="test-account"), "test-session"))
    assert stream["exp"] - stream["iat"] == 60
    with pytest.raises(ValueError):
        create_access_token({"sub": "test-account"}, server_managed_session=True)


@pytest.mark.asyncio
async def test_background_poll_does_not_extend_idle_deadline():
    from datetime import datetime, timedelta, timezone
    seen = datetime.now(timezone.utc) - timedelta(minutes=2)
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(first=lambda: (seen,))
    await session_service.assert_session_active(db, "test-session", "test-account")
    update = str(db.execute.await_args_list[-1].args[0])
    assert "last_seen_at" in update and "expires_at" not in update


@pytest.mark.asyncio
async def test_late_activity_cannot_revive_a_session():
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(rowcount=0)
    with pytest.raises(HTTPException) as denied:
        await session_service.record_activity(db, "test-session", "test-account")
    assert denied.value.status_code == 401
    assert "expires_at>clock_timestamp()" in str(db.execute.await_args.args[0])
    db.commit.assert_not_awaited()


def test_only_admin_can_manage_timeouts(client, as_viewer):
    path = "/api/v1/system/security/session-timeouts"
    assert client.get(path).status_code == 403
    assert client.put(path, json={"minutes": 0}).status_code == 403
    assert client.put(path + "/users/30a505eb-24c7-48b9-9a47-37e40a27c17c", json={"minutes": 0}).status_code == 403
