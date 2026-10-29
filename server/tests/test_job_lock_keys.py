"""Independent jobs progress while a competing worker of the same job skips."""
import importlib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services.job_lock_keys import JOB_LOCK_KEYS
from pg_fixture import pg_cluster, pg_db  # noqa: F401

WORKERS = (
    ("apm_synthetic_service", "SYNTHETIC_SWEEP_ADVISORY_LOCK", "apm.synthetic.sweep"),
    ("udt_sweeper", "UDT_SWEEP_ADVISORY_LOCK", "udt.sweep"),
    ("managed_device_service", "CHILD_SYNC_ADVISORY_LOCK", "managed_device.child_sync"),
    ("netflow_rollup", "NETFLOW_ROLLUP_ADVISORY_LOCK", "netflow.rollup"),
    ("netflow_rollup", "NETFLOW_SWEEP_ADVISORY_LOCK", "netflow.sweep"),
    ("udt_ad_service", "UDT_AD_ADVISORY_LOCK", "udt.ad"),
    ("apm_service_graph", "SERVICE_GRAPH_ADVISORY_LOCK", "apm.service_graph"),
    ("udt_gateway_service", "LOCK", "udt.gateway"),
    ("netpath_enrichment", "NETPATH_ENRICH_LOCK", "netpath.enrichment"),
)


def test_workers_use_distinct_registered_locks():
    actual = [getattr(importlib.import_module("app.services." + module), constant)
              for module, constant, _ in WORKERS]
    assert actual == [JOB_LOCK_KEYS[name] for _, _, name in WORKERS]
    assert len(set(actual)) == 9
    assert not set(actual).intersection({1515074391, 1515074392, 1515074393, 1515074394})


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [
    ("apm.synthetic.sweep", "udt.sweep"),
    ("managed_device.child_sync", "netflow.rollup"),
    ("netflow.sweep", "udt.ad", "apm.service_graph"),
    ("udt.gateway", "netpath.enrichment"),
])
async def test_previously_colliding_jobs_progress_independently(pg_db, names):
    sessions = async_sessionmaker(create_async_engine(pg_db.url, poolclass=NullPool))
    lock = text("SELECT pg_try_advisory_xact_lock(:key)")
    async with sessions() as first, sessions() as second:
        assert (await first.execute(lock, {"key": JOB_LOCK_KEYS[names[0]]})).scalar() is True
        assert (await second.execute(lock, {"key": JOB_LOCK_KEYS[names[0]]})).scalar() is False
        for independent in names[1:]:
            assert (await second.execute(lock, {"key": JOB_LOCK_KEYS[independent]})).scalar() is True
        await first.rollback()
        assert (await second.execute(lock, {"key": JOB_LOCK_KEYS[names[0]]})).scalar() is True


@pytest.mark.asyncio
async def test_session_lock_retains_ownership_across_commits(pg_db):
    engine = create_async_engine(pg_db.url, poolclass=NullPool)
    lock = text("SELECT pg_try_advisory_lock(:key)")
    params = {"key": JOB_LOCK_KEYS["udt.ad"]}
    async with engine.connect() as owner, engine.connect() as contender:
        assert (await owner.execute(lock, params)).scalar() is True
        await owner.commit()
        assert (await contender.execute(lock, params)).scalar() is False
        assert (await owner.execute(text("SELECT pg_advisory_unlock(:key)"), params)).scalar() is True
        assert (await contender.execute(lock, params)).scalar() is True
        assert (await contender.execute(text("SELECT pg_advisory_unlock(:key)"), params)).scalar() is True
    await engine.dispose()
