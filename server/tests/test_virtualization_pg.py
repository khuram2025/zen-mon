"""Connector lifecycle and worker ownership against a private disposable PostgreSQL."""
import asyncio
from datetime import datetime, timezone
from uuid import UUID
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from pg_fixture import pg_cluster, pg_db, SCRIPTS_DIR  # noqa: F401
from inventory_fleet import api_client
from app.api.v1 import virtualization as api
from app.core import crypto
from app.models.virtualization import VirtualizationConnector
from app.services import virtualization_service as service
from app.services.virtualization.common import Snapshot


def test_connector_lifecycle_encryption_scope_and_test_before_save(pg_db, monkeypatch):
    pg_db.psql_file(SCRIPTS_DIR / "migrate-171-virtualization.sql")  # replay is idempotent
    monkeypatch.setattr(crypto, "_cipher", lambda: AESGCM(b"x" * 32))
    snapshot = Snapshot()
    snapshot.resource("host", "h1", "Fixture host", metrics={"cpu_usage_percent": 25})
    monkeypatch.setattr(api, "collect", AsyncMock(return_value=snapshot))
    connection = dict(provider="nutanix_pc", endpoint="https://prism.example.test:9440",
                      username="monitor", secret="fixture-secret")
    with api_client(pg_db, role="admin", persist_user=True) as client:
        payload = dict(name="Fixture Prism", connection=connection, tags=["SNIH"], test_token="invalid")
        assert client.post("/api/v1/virtualization/connectors", json=payload).status_code == 400
        tested = client.post("/api/v1/virtualization/test", json=connection)
        assert tested.status_code == 200, tested.text
        payload["test_token"] = tested.json()["test_token"]
        response = client.post("/api/v1/virtualization/connectors", json=payload)
        assert response.status_code == 201, response.text
        connector_id = response.json()["id"]
        assert "fixture-secret" not in response.text
        stored = pg_db.scalar("SELECT config FROM virtualization_connectors WHERE id=$1", UUID(connector_id))
        assert "fixture-secret" not in str(stored)
        assert crypto.decrypt_config(stored)["secret"] == "fixture-secret"
        assert client.patch(f"/api/v1/virtualization/connectors/{connector_id}", json={"enabled": False}).status_code == 200
        assert client.post(f"/api/v1/virtualization/connectors/{connector_id}/poll").status_code == 400
    with api_client(pg_db, role="virt-reader", permissions=["servers.view"], scope_tags=("other",)) as client:
        assert client.get("/api/v1/virtualization/connectors").json() == []
        assert client.get(f"/api/v1/virtualization/connectors/{connector_id}").status_code == 404
    with api_client(pg_db, role="virt-reader", permissions=["servers.view"], scope_tags=("SNIH",)) as client:
        assert len(client.get("/api/v1/virtualization/connectors").json()) == 1
        assert client.get(f"/api/v1/virtualization/connectors/{connector_id}/configuration").status_code == 403
    with api_client(pg_db, role="admin", persist_user=True) as client:
        assert client.delete(f"/api/v1/virtualization/connectors/{connector_id}").status_code == 204
    assert pg_db.scalar("SELECT count(*) FROM virtualization_connectors") == 0


@pytest.mark.asyncio
async def test_workers_claim_once_and_persist_success(pg_db, monkeypatch):
    monkeypatch.setattr(crypto, "_cipher", lambda: AESGCM(b"x" * 32))
    snapshot = Snapshot()
    snapshot.resource("cluster", "c1", "Fixture cluster", metrics={"cpu_usage_percent": 40})
    monkeypatch.setattr(service, "collect", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(service, "write_metrics", lambda *args: None)
    engine = create_async_engine(pg_db.url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as seed:
            row = VirtualizationConnector(name="Fixture", provider="nutanix_pc", endpoint="https://prism.example.test:9440",
                config=crypto.encrypt_config(dict(provider="nutanix_pc", endpoint="https://prism.example.test:9440", username="monitor", secret="fixture-secret")))
            seed.add(row)
            await seed.commit()
        due = select(VirtualizationConnector).where(VirtualizationConnector.next_poll_at <= datetime.now(timezone.utc)).with_for_update(skip_locked=True)
        async with sessions() as first, sessions() as second:
            claimed = (await first.execute(due)).scalar_one()
            assert (await second.execute(due)).scalar_one_or_none() is None
            await service.poll_locked(first, claimed)
            await first.commit()
        async with sessions() as verify:
            persisted = await verify.get(VirtualizationConnector, row.id)
            assert persisted.status == "ok" and persisted.last_success_at
            assert persisted.snapshot["resources"][0]["metrics"]["cpu_usage_percent"] == 40
            assert (await verify.execute(due)).scalar_one_or_none() is None
    finally:
        await engine.dispose()
