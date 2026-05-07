from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.security import create_access_token, require_operator_user
from conftest import make_user


def test_auth_me_returns_current_user(client, as_admin):
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer test"})

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "admin-user"
    assert body["role"] == "admin"


def test_access_token_contains_subject_and_role():
    token = create_access_token(
        {"sub": "00000000-0000-0000-0000-000000000001", "role": "admin"},
        expires_delta=timedelta(minutes=5),
    )

    assert isinstance(token, str)
    assert token.count(".") == 2


def test_viewer_cannot_access_admin_surfaces(client, as_viewer):
    protected_paths = [
        "/api/v1/users",
        "/api/v1/settings/gateways",
        "/api/v1/snmp-credentials",
        "/api/v1/system/update-status",
        "/api/v1/audit-logs",
    ]

    for path in protected_paths:
        response = client.get(path, headers={"Authorization": "Bearer test"})
        assert response.status_code == 403, path


def test_viewer_cannot_access_operator_mutation_surfaces(client, as_viewer):
    device_id = uuid4()
    rule_id = uuid4()
    service_check_id = uuid4()
    protected_requests = [
        ("post", "/api/v1/discovery/scan", {"subnets": ["127.0.0.0/30"]}),
        ("post", f"/api/v1/devices/{device_id}/ping-test", None),
        ("post", f"/api/v1/devices/{device_id}/snmp-test", None),
        ("post", f"/api/v1/service-checks/{service_check_id}/test", None),
        ("post", f"/api/v1/alert-rules/{rule_id}/toggle", None),
        ("post", f"/api/v1/alert-rules/{rule_id}/simulate", None),
    ]

    for method, path, payload in protected_requests:
        response = getattr(client, method)(
            path,
            headers={"Authorization": "Bearer test"},
            json=payload,
        )
        assert response.status_code == 403, path


@pytest.mark.asyncio
async def test_operator_dependency_allows_operator_and_admin():
    operator = await require_operator_user(make_user("operator"))
    admin = await require_operator_user(make_user("admin"))

    assert operator.role == "operator"
    assert admin.role == "admin"


@pytest.mark.asyncio
async def test_operator_dependency_rejects_viewer():
    with pytest.raises(HTTPException) as exc_info:
        await require_operator_user(make_user("viewer"))

    assert exc_info.value.status_code == 403


def test_admin_can_access_audit_logs(client, as_admin):
    response = client.get("/api/v1/audit-logs", headers={"Authorization": "Bearer test"})

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] == 1
    assert body["data"][0]["action"] == "user.create"
