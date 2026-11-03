import pytest
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from sqlalchemy.pool import NullPool
from app.api.v1 import system_updates
from app.core import database
from pg_fixture import pg_cluster,pg_db  # noqa: F401

@pytest.mark.asyncio
async def test_health_uses_the_configured_application_database(monkeypatch,pg_db):
    engine=create_async_engine(pg_db.url,poolclass=NullPool)
    monkeypatch.setattr(database,'AsyncSessionLocal',async_sessionmaker(engine))
    try:assert await system_updates._database_healthy()
    finally:await engine.dispose()

@pytest.mark.asyncio
async def test_bad_application_credentials_do_not_report_a_healthy_database(monkeypatch):
    class Failed:
        async def __aenter__(self):raise ConnectionError('unavailable')
        async def __aexit__(self,*_):return False
    monkeypatch.setattr(database,'AsyncSessionLocal',Failed)
    assert not await system_updates._database_healthy()
