from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_db
from app.main import app


NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_maintenance_create_requires_valid_range_and_scope():
    from app.schemas.device import DeviceMaintenanceCreate

    ok = DeviceMaintenanceCreate(
        scope_type="all", starts_at=NOW, ends_at=NOW + timedelta(hours=1)
    )
    assert ok.scope_type == "all"

    with pytest.raises(ValueError):
        DeviceMaintenanceCreate(
            scope_type="all", starts_at=NOW, ends_at=NOW - timedelta(minutes=1)
        )

    with pytest.raises(ValueError):
        DeviceMaintenanceCreate(
            scope_type="device", starts_at=NOW, ends_at=NOW + timedelta(hours=1)
        )

    with pytest.raises(ValueError):
        DeviceMaintenanceCreate(
            scope_type="tag", scope_tag="  ", starts_at=NOW, ends_at=NOW + timedelta(hours=1)
        )

    dev = DeviceMaintenanceCreate(
        scope_type="device", scope_device_id=uuid4(),
        starts_at=NOW, ends_at=NOW + timedelta(minutes=30),
    )
    assert dev.scope_device_id is not None


# ---------------------------------------------------------------------------
# SLA exclusion helper (reports)
# ---------------------------------------------------------------------------

def test_exclude_maintenance_samples_drops_only_window_rows():
    from app.services.report_service import _exclude_maintenance_samples

    did = str(uuid4())
    other = str(uuid4())
    t0 = datetime(2026, 8, 4, 10, 0, 0)

    rows = [
        {"device_id": did, "timestamp": t0, "is_up": 1},
        {"device_id": did, "timestamp": t0 + timedelta(minutes=10), "is_up": 0},  # in window
        {"device_id": did, "timestamp": t0 + timedelta(minutes=30), "is_up": 0},  # in window
        {"device_id": did, "timestamp": t0 + timedelta(minutes=70), "is_up": 1},
        {"device_id": other, "timestamp": t0 + timedelta(minutes=10), "is_up": 0},  # other device
    ]
    windows = {did: [(t0 + timedelta(minutes=5), t0 + timedelta(minutes=60))]}

    out = _exclude_maintenance_samples(rows, windows)
    assert len(out) == 3
    assert all(
        not (r["device_id"] == did and windows[did][0][0] <= r["timestamp"] <= windows[did][0][1])
        for r in out
    )
    # Empty window map is a no-op passthrough.
    assert _exclude_maintenance_samples(rows, {}) is rows


# ---------------------------------------------------------------------------
# Alert-engine suppression during an active window
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else ([row] if row else [])

    def first(self):
        return self._row

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._row

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class MaintenanceFakeDB:
    """Device exists; an active device_maintenance window covers it."""

    def __init__(self, in_maintenance: bool = True):
        self.in_maintenance = in_maintenance
        self.executed: list[str] = []

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.executed.append(sql)
        if "FROM device_maintenance" in sql:
            return _Result(row=SimpleNamespace(one=1) if self.in_maintenance else None)
        if "SELECT device_type, group_id, location FROM devices" in sql:
            return _Result(row=SimpleNamespace(device_type="router", group_id=None, location=None))
        if "SELECT group_id FROM devices" in sql:
            return _Result(row=SimpleNamespace(group_id=None))
        return _Result()

    async def commit(self):
        return None


@pytest.fixture
def maint_client():
    db = MaintenanceFakeDB(in_maintenance=True)

    async def fake_db():
        yield db

    app.dependency_overrides[get_db] = fake_db
    with TestClient(app) as test_client:
        yield test_client, db
    app.dependency_overrides.pop(get_db, None)


def test_status_alert_suppressed_during_maintenance(maint_client):
    client, db = maint_client
    resp = client.post(
        "/api/v1/alert-engine/evaluate",
        json={
            "device_id": str(uuid4()),
            "hostname": "core-sw-01",
            "ip_address": "10.0.0.1",
            "old_status": "up",
            "new_status": "down",
        },
    )
    assert resp.status_code == 200
    assert resp.json().get("suppressed") == "maintenance"
    # Nothing was inserted into alerts.
    assert not any("INSERT INTO alerts" in q for q in db.executed)


def test_trap_alert_suppressed_during_maintenance(maint_client):
    client, db = maint_client
    resp = client.post(
        "/api/v1/alert-engine/evaluate-trap",
        json={
            "device_id": str(uuid4()),
            "source_ip": "10.0.0.1",
            "trap_oid": "1.3.6.1.6.3.1.1.5.3",
            "trap_name": "linkDown",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("suppressed") == "maintenance"
    assert body.get("alerts_created") == 0
    assert not any("INSERT INTO alerts" in q for q in db.executed)
