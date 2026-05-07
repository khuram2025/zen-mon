from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value

    def first(self):
        return self.value

    def fetchall(self):
        return []


class MappingResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class RowResult:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row

    def fetchall(self):
        return [self.row]


class FakeDB:
    async def execute(self, statement, params=None):
        sql = str(statement)
        if "INSERT INTO alert_rules" in sql:
            return RowResult(
                SimpleNamespace(
                    id=uuid4(),
                    name=params["name"],
                    description=params["description"],
                    enabled=params["enabled"],
                    metric=params["metric"],
                    operator=params["operator"],
                    threshold=params["threshold"],
                    duration=params["duration"],
                    device_id=params["device_id"],
                    group_id=params["group_id"],
                    service_check_id=params["service_check_id"],
                    service_check_group_id=params["service_check_group_id"],
                    severity=params["severity"],
                    notify_channels=[],
                    cooldown=params["cooldown"],
                    device_type=params["device_type"],
                    location=params["location"],
                    trigger_on=params["trigger_on"],
                    recovery_alert=params["recovery_alert"],
                    min_duration=params["min_duration"],
                    max_repeat=params["max_repeat"],
                    schedule_start=params["schedule_start"],
                    schedule_end=params["schedule_end"],
                    schedule_days=[],
                    email_subject=params["email_subject"],
                    email_body=params["email_body"],
                    sms_template=params["sms_template"],
                    recovery_email_subject=params["recovery_email_subject"],
                    recovery_email_body=params["recovery_email_body"],
                    recovery_sms_template=params["recovery_sms_template"],
                    created_at=params["created_at"],
                    updated_at=params["updated_at"],
                    created_by=params["created_by"],
                )
            )
        if "COUNT(*) FROM audit_logs" in sql:
            return ScalarResult(1)
        if "FROM audit_logs" in sql:
            return MappingResult([
                {
                    "id": uuid4(),
                    "actor_id": uuid4(),
                    "actor_username": "admin",
                    "actor_role": "admin",
                    "action": "user.create",
                    "resource_type": "user",
                    "resource_id": str(uuid4()),
                    "metadata": {"username": "created-user"},
                    "created_at": datetime.now(timezone.utc),
                }
            ])
        return ScalarResult(None)

    async def commit(self):
        return None


def make_user(role: str = "admin"):
    return SimpleNamespace(
        id=uuid4(),
        username=f"{role}-user",
        email=f"{role}@example.com",
        full_name=f"{role.title()} User",
        role=role,
        is_active=True,
        last_login=None,
        password_hash="",
    )


@pytest.fixture
def client():
    async def fake_db():
        yield FakeDB()

    app.dependency_overrides[get_db] = fake_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def as_admin():
    async def override():
        return make_user("admin")

    app.dependency_overrides[get_current_user] = override
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def as_viewer():
    async def override():
        return make_user("viewer")

    app.dependency_overrides[get_current_user] = override
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def as_operator():
    async def override():
        return make_user("operator")

    app.dependency_overrides[get_current_user] = override
    yield
    app.dependency_overrides.pop(get_current_user, None)
