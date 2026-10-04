"""NetFlow governance: migrate-138 + models, the shared interface-speed rule,
interface-speed audit, and NetFlow scoping outside /netflow (link
utilization overlay and has_netflow, maps link rates).

Gates (all optional; unset -> those tests skip):
  ZENPLUS_NETFLOW_TEST_PG_ADMIN  libpq URL of a PostgreSQL superuser session
                                 (postgres://postgres@127.0.0.1:55441/postgres):
                                 migrate-138 is replayed in a throwaway database.
  ZENPLUS_NETFLOW_TEST_PG        DISPOSABLE database with the full schema and
                                 migrate-138 (postgresql+asyncpg://...).
  ZENPLUS_NETFLOW_TEST_CH        ClickHouse HTTP URL; a private database is
                                 created and dropped (needs zenplus.flow_records
                                 at schema v2 to copy its structure).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from app.services.interface_speed import effective_speed, effective_speed_source, effective_speed_sql

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
MIGRATION = SCRIPTS / "migrate-138-netflow-governance.sql"

PG_ADMIN = os.environ.get("ZENPLUS_NETFLOW_TEST_PG_ADMIN")
PG_DSN = os.environ.get("ZENPLUS_NETFLOW_TEST_PG")
CH_URL = os.environ.get("ZENPLUS_NETFLOW_TEST_CH")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="set ZENPLUS_NETFLOW_TEST_PG (disposable DB with migrate-138)")
needs_pg_ch = pytest.mark.skipif(not (PG_DSN and CH_URL),
                                 reason="set ZENPLUS_NETFLOW_TEST_PG and ZENPLUS_NETFLOW_TEST_CH")


# ── Interface speed ──────────────────────────────────────────────────────────

def test_effective_speed_prefers_configured_then_override_then_snmp():
    assert effective_speed_source(50_000_000, 80_000_000, 1_000_000_000) == (50_000_000, "configured")
    assert effective_speed_source(None, 80_000_000, 1_000_000_000) == (80_000_000, "override")
    assert effective_speed_source(0, None, 1_000_000_000) == (1_000_000_000, "snmp")
    assert effective_speed_source(None, None, None) == (None, "unknown")
    assert effective_speed_source(-5, 0, 0) == (None, "unknown")
    assert effective_speed(50_000_000, None, None) == 50_000_000   # override-only interface
    assert effective_speed("100", None, "x") == 100


def test_effective_speed_sql_mirrors_the_rule():
    sql = effective_speed_sql("di.configured_speed_bps", "o.speed_in_bps", "di.if_speed")
    assert sql.index("configured") < sql.index("speed_in_bps") < sql.index("if_speed")
    assert effective_speed_sql("a", None, "b") == "COALESCE(NULLIF(GREATEST(a, 0), 0), NULLIF(GREATEST(b, 0), 0))"


def test_link_utilization_uses_the_shared_rule():
    import app.api.v1.link_utilization as lu

    assert not hasattr(lu, "_effective_speed")
    assert lu.effective_speed is effective_speed


# ── migrate-138 ──────────────────────────────────────────────────────────────

def test_migration_is_replay_safe_and_seeds_nothing():
    sys.path.insert(0, str(SCRIPTS))
    import migration_order

    sql = MIGRATION.read_text()
    assert migration_order.is_replay_safe(sql)
    assert not migration_order.writes_rows(sql)
    created = set(migration_order.created_objects(sql))
    assert {"netflow_ip_groups", "netflow_app_rules", "netflow_exporter_settings",
            "netflow_interface_overrides", "netflow_findings", "netflow_detector_exceptions",
            "netflow_ti_feeds", "netflow_ti_entries", "netflow_export_jobs"} <= created
    # Private/shared is migrate-150's is_shared; this file only adds the rest.
    assert {"netflow_saved_views.kind", "netflow_saved_views.query_version",
            "netflow_saved_views.updated_by"} <= migration_order.added_columns(sql)
    assert "netflow_saved_views.visibility" not in migration_order.added_columns(sql)


PREREQUISITES = """
CREATE TABLE users (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), username TEXT);
CREATE TABLE roles (name VARCHAR(50) PRIMARY KEY, permissions JSONB NOT NULL DEFAULT '[]',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
INSERT INTO roles (name, permissions) VALUES
  ('operator', '["netflow.view"]'), ('editor', '["netflow.view", "netflow.manage"]'),
  ('viewer', '["netflow.view"]');
"""


def _psql(url: str, *args: str, sql: str | None = None) -> str:
    out = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", url, *args],
                         input=sql, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _schema(url: str) -> str:
    out = subprocess.run(["pg_dump", "-s", "--no-owner", url], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    # pg_dump >= 16.x brackets the dump with a random \restrict key.
    return "\n".join(l for l in out.stdout.splitlines() if not l.startswith(("\\restrict", "\\unrestrict")))


@pytest.mark.skipif(not PG_ADMIN, reason="set ZENPLUS_NETFLOW_TEST_PG_ADMIN to replay the migration")
def test_migration_replays_to_an_identical_schema():
    db = f"nf_mig138_{os.getpid()}"
    u = urlparse(PG_ADMIN)
    target = u._replace(path=f"/{db}").geturl()
    _psql(PG_ADMIN, "-c", f"DROP DATABASE IF EXISTS {db}")
    _psql(PG_ADMIN, "-c", f"CREATE DATABASE {db}")
    try:
        _psql(target, sql=PREREQUISITES)
        _psql(target, "-f", str(SCRIPTS / "migrate-052-netflow-saved-views.sql"))
        _psql(target, sql="INSERT INTO netflow_saved_views (id, name, query) "
                          "VALUES (gen_random_uuid(), 'legacy', '{\"src\": \"10.0.0.1\"}')")
        # migrate-150 (runs before 138 in migrations.lock) adds is_shared and
        # marks every existing view shared; mirror that prerequisite here.
        _psql(target, sql="ALTER TABLE netflow_saved_views ADD COLUMN is_shared BOOLEAN NOT NULL DEFAULT FALSE; "
                          "UPDATE netflow_saved_views SET is_shared = TRUE;")
        _psql(target, "-f", str(MIGRATION))
        first = _schema(target)
        _psql(target, "-f", str(MIGRATION))
        assert _schema(target) == first
        roles = _psql(target, "-At", "-c",
                      "SELECT name, permissions::text FROM roles ORDER BY name")
        lines = dict(line.split("|", 1) for line in roles.strip().splitlines())
        for role in ("operator", "editor"):
            perms = json.loads(lines[role])
            assert perms.count("netflow.manage") == 1 and perms.count("netflow.export") == 1
        assert "netflow.manage" not in json.loads(lines["viewer"])
        # Existing views keep today's behaviour: shared, forensics, v1 query.
        assert _psql(target, "-At", "-c",
                     "SELECT is_shared, kind, query_version FROM netflow_saved_views").strip() == "t|forensics|1"
        # CHECK constraints reject what the API must never store.
        for bad in (
            "INSERT INTO netflow_app_rules (application) VALUES ('nothing to match')",
            "INSERT INTO netflow_app_rules (application, port_lo, port_hi) VALUES ('x', 90, 80)",
            "INSERT INTO netflow_app_rules (application, port_lo) VALUES ('  ', 80)",
            "INSERT INTO netflow_exporter_settings (exporter_ip, role) VALUES ('10.0.0.1', 'bogus')",
            "INSERT INTO netflow_findings (fingerprint, status, first_seen, last_seen) VALUES ('f', 'closed', now(), now())",
            "UPDATE netflow_saved_views SET kind = 'public'",
        ):
            out = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", target, "-c", bad],
                                 capture_output=True, text=True, timeout=60)
            assert out.returncode != 0 and "violates check constraint" in out.stderr, bad
    finally:
        _psql(PG_ADMIN, "-c", f"DROP DATABASE IF EXISTS {db}")


def _run(coro_fn):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    async def main():
        engine = create_async_engine(PG_DSN)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                await coro_fn(db)
        finally:
            await engine.dispose()

    asyncio.run(main())


async def _clean_models(db):
    from sqlalchemy import text

    await db.rollback()
    for sql in (
        "DELETE FROM netflow_ti_feeds WHERE name LIKE 'a3b-model-%'",
        "DELETE FROM netflow_ip_groups WHERE name LIKE 'a3b-model-%'",
        "DELETE FROM netflow_app_rules WHERE application LIKE 'a3b-model-%'",
        "DELETE FROM netflow_interface_overrides WHERE exporter_ip = '198.51.100.77'",
        "DELETE FROM netflow_exporter_settings WHERE exporter_ip = '198.51.100.77'",
        "DELETE FROM netflow_findings WHERE fingerprint LIKE 'a3b-model-%'",
        "DELETE FROM netflow_detector_exceptions WHERE reason = 'a3b-model'",
        "DELETE FROM netflow_export_jobs WHERE query @> '{\"a3b\": true}'",
        "DELETE FROM netflow_saved_views WHERE name LIKE 'a3b-model-%'",
    ):
        await db.execute(text(sql))
    await db.commit()


@needs_pg
def test_models_round_trip():
    from sqlalchemy import delete, select
    from app.models import netflow_governance as m
    from app.models.netflow_exporter_device import NetflowExporterDevice
    from app.models.netflow_saved_view import NetflowSavedView

    async def body(db):
        await _clean_models(db)
        try:
            now = datetime.now(timezone.utc)
            grp = m.NetflowIpGroup(name="a3b-model-group", cidrs=["10.0.0.0/8"], tags=["x"])
            rule = m.NetflowAppRule(application="a3b-model-app", protocol=6, port_lo=8000, port_hi=8010)
            setting = m.NetflowExporterSetting(exporter_ip="198.51.100.77", role="core", count_direction="ingress")
            override = m.NetflowInterfaceOverride(exporter_ip="198.51.100.77", ifindex=4_294_967_295,
                                                  speed_in_bps=50_000_000, role="wan")
            finding = m.NetflowFinding(fingerprint="a3b-model-fp", kind="syn_scan", first_seen=now,
                                       last_seen=now, src_ip="192.0.2.9", evidence={"k": 1})
            exc = m.NetflowDetectorException(kind="icmp_flood", src_cidr="192.0.2.0/24", reason="a3b-model")
            feed = m.NetflowTiFeed(name="a3b-model-feed", source="upload")
            job = m.NetflowExportJob(query={"v": 2, "a3b": True}, status="queued")
            view = NetflowSavedView(name="a3b-model-view", query={"v": 2}, visibility="private",
                                    kind="overview", query_version=2)
            db.add_all([grp, rule, setting, override, finding, exc, feed, job, view])
            await db.commit()
            db.add(m.NetflowTiEntry(feed_id=feed.id, prefix="203.0.113.0/24"))
            child = m.NetflowIpGroup(name="a3b-model-child", cidrs=["10.1.0.0/16"], parent_id=grp.id)
            db.add(child)
            await db.commit()
            got = (await db.execute(select(m.NetflowAppRule).where(m.NetflowAppRule.id == rule.id))).scalar_one()
            assert (got.port_lo, got.port_hi, got.priority, got.enabled) == (8000, 8010, 100, True)
            assert (await db.execute(select(m.NetflowFinding.status).where(
                m.NetflowFinding.id == finding.id))).scalar_one() == "open"
            assert (await db.execute(select(NetflowSavedView.visibility).where(
                NetflowSavedView.id == view.id))).scalar_one() == "private"
            assert (await db.execute(select(m.NetflowIpGroup.parent_id).where(
                m.NetflowIpGroup.id == child.id))).scalar_one() == grp.id
            # Address columns compare with, and come back as, plain strings.
            assert (await db.execute(select(m.NetflowExporterSetting.exporter_ip).where(
                m.NetflowExporterSetting.exporter_ip == "198.51.100.77"))).scalar_one() == "198.51.100.77"
            assert (await db.execute(select(m.NetflowDetectorException.src_cidr).where(
                m.NetflowDetectorException.id == exc.id))).scalar_one() == "192.0.2.0/24"
            assert (await db.execute(select(m.NetflowInterfaceOverride.speed_in_bps).where(
                m.NetflowInterfaceOverride.exporter_ip == "198.51.100.77",
                m.NetflowInterfaceOverride.ifindex == 4_294_967_295))).scalar_one() == 50_000_000
            assert (await db.execute(select(m.NetflowTiEntry.feed_id).where(
                m.NetflowTiEntry.prefix == "203.0.113.0/24"))).scalar_one() == feed.id
            assert (await db.execute(select(NetflowExporterDevice).where(
                NetflowExporterDevice.exporter_ip == "198.51.100.77"))).first() is None
            await db.execute(delete(m.NetflowTiFeed).where(m.NetflowTiFeed.id == feed.id))   # cascades
            await db.commit()
            assert (await db.execute(select(m.NetflowTiEntry).where(
                m.NetflowTiEntry.feed_id == feed.id))).first() is None
        finally:
            await _clean_models(db)

    _run(body)


# ── Interface speed audit (devices.py) ───────────────────────────────────────

@needs_pg
def test_interface_speed_changes_are_audited():
    from sqlalchemy import text
    from app.api.v1.devices import (
        InterfaceBulkSpeedUpdate, InterfaceSpeedUpdate,
        bulk_update_device_interface_speed, update_device_interface_speed,
    )

    async def body(db):
        actor = (await db.execute(text("SELECT id, username, role FROM users ORDER BY created_at LIMIT 1"))).first()
        user = SimpleNamespace(id=actor.id, username=actor.username, role=actor.role)
        dev = uuid.uuid4()
        await db.execute(text("INSERT INTO devices (id, hostname) VALUES (:id, 'a3b-speed-dev')"), {"id": dev})
        for idx, speed in ((1, None), (2, 10_000_000), (3, None)):
            await db.execute(text(
                "INSERT INTO device_interfaces (device_id, if_index, if_name, if_speed, configured_speed_bps) "
                "VALUES (:d, :i, :n, 1000000000, :s)"), {"d": dev, "i": idx, "n": f"eth{idx}", "s": speed})
        await db.commit()
        try:
            out = await update_device_interface_speed(dev, 2, InterfaceSpeedUpdate(configured_speed_bps=50_000_000),
                                                      db=db, user=user)
            assert out["configured_speed_bps"] == 50_000_000 and "previous_speed_bps" not in out
            await bulk_update_device_interface_speed(
                dev, InterfaceBulkSpeedUpdate(if_indexes=[1, 3, 99], configured_speed_bps=None), db=db, user=user)
            rows = (await db.execute(text(
                "SELECT action, resource_id, metadata, actor_id FROM audit_logs "
                "WHERE resource_type = 'device_interface' AND metadata->>'device_id' = :d ORDER BY created_at"),
                {"d": str(dev)})).all()
            assert [r.action for r in rows] == ["device_interface.speed.update",
                                                "device_interface.speed.bulk_update",
                                                "device_interface.speed.bulk_update"]
            single, bulk1, bulk3 = (r.metadata if isinstance(r.metadata, dict) else json.loads(r.metadata)
                                    for r in rows)
            assert single["configured_speed_bps"] == {"old": 10_000_000, "new": 50_000_000}
            assert rows[0].resource_id == f"{dev}:2" and rows[0].actor_id == actor.id
            # FE-05: one entry per interface, keyed like a single change, so the
            # interface's speed history finds it.
            assert sorted(r.resource_id for r in rows[1:]) == [f"{dev}:1", f"{dev}:3"]
            assert {bulk1["if_index"], bulk3["if_index"]} == {1, 3} and bulk1["bulk_size"] == 2
            assert bulk1["configured_speed_bps"] == {"old": None, "new": None}
            with pytest.raises(Exception) as missing:
                await update_device_interface_speed(dev, 42, InterfaceSpeedUpdate(configured_speed_bps=1),
                                                    db=db, user=user)
            assert getattr(missing.value, "status_code", None) == 404
        finally:
            await db.rollback()
            await db.execute(text("DELETE FROM audit_logs WHERE metadata->>'device_id' = :d"), {"d": str(dev)})
            await db.execute(text("DELETE FROM devices WHERE id = :d"), {"d": dev})
            await db.commit()

    _run(body)


# ── NetFlow outside /netflow: link utilization and maps ──────────────────────

class _ChProxy:
    """ClickHouse client that serves the SNMP table from the test database."""

    def __init__(self, client, db):
        self.client, self.db = client, db

    def query(self, sql, parameters=None, **kw):
        return self.client.query(sql.replace("zenplus.snmp_if_metrics", f"{self.db}.snmp_if_metrics"),
                                 parameters=parameters, **kw)


@pytest.fixture
def flow_db(monkeypatch):
    import clickhouse_connect
    from app.api.v1.netflow import common

    u = urlparse(CH_URL)
    client = clickhouse_connect.get_client(host=u.hostname, port=u.port or 8123,
                                           username=u.username or "default", password=u.password or "")
    db = f"nf_gov_test_{os.getpid()}"
    client.command(f"DROP DATABASE IF EXISTS {db}")
    client.command(f"CREATE DATABASE {db}")
    client.command(f"CREATE TABLE {db}.flow_records AS zenplus.flow_records")
    # The link list reads interface presence from the 5-minute rollup; feed it
    # through a copy of the real view so the test sees what ingestion writes.
    client.command(f"CREATE TABLE {db}.flow_interface_5m AS zenplus.flow_interface_5m")
    view = client.query("SHOW CREATE TABLE zenplus.flow_interface_5m_mv").result_rows[0][0]
    client.command(view.replace("zenplus.", f"{db}."))
    client.command(f"""CREATE TABLE {db}.snmp_if_metrics (
        device_id UUID, if_index UInt32, timestamp DateTime64(3, 'UTC'), in_octets UInt64,
        out_octets UInt64, in_errors UInt64, out_errors UInt64, in_discards UInt64,
        out_discards UInt64, in_ucast_pkts UInt64, out_ucast_pkts UInt64, oper_status UInt8,
        in_bps Float64, out_bps Float64, poller_id String) ENGINE = MergeTree ORDER BY (device_id, if_index, timestamp)""")
    now = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=90)
    cols = ["timestamp", "received_at", "exporter_ip", "exporter_ip6", "src_addr", "src_addr6", "dst_addr",
            "dst_addr6", "input_if", "output_if", "input_snmp", "output_snmp", "protocol", "src_port",
            "dst_port", "bytes", "packets", "sampling_interval", "fw_event"]
    rows = [
        # device X: IPv4 exporter 127.0.0.61 (mapped), IPv6 exporter 2001:db8::61 (mapped)
        (now, now, "127.0.0.61", "::", "10.1.1.10", "::", "93.184.216.34", "::", 4464, 4466, 4464, 4466, 6, 51000, 443, 1000, 5, 1, 0),
        (now, now, "127.0.0.61", "::", "93.184.216.34", "::", "10.1.1.10", "::", 4466, 4464, 4466, 4464, 6, 443, 51000, 5000, 6, 1, 0),
        (now, now, "0.0.0.0", "2001:db8::61", "0.0.0.0", "2001:db8:1::10", "0.0.0.0", "2001:db8:2::20", 4464, 7, 4464, 7, 17, 5353, 53, 700, 2, 1, 0),
        # guarded rows: an implausible byte count and an NSEL create event
        (now, now, "127.0.0.61", "::", "10.1.1.66", "::", "10.9.9.9", "::", 4464, 4466, 4464, 4466, 6, 1, 2, 5_000_000_000_000, 1, 1, 0),
        (now, now, "127.0.0.61", "::", "10.1.1.77", "::", "10.9.9.9", "::", 4464, 4466, 4464, 4466, 6, 1, 2, 999, 1, 1, 1),
        # device Y's exporter (management-IP match)
        (now, now, "127.0.0.62", "::", "10.2.2.20", "::", "10.3.3.30", "::", 1, 3, 1, 3, 6, 40000, 22, 800, 4, 1, 0),
    ]
    client.insert(f"{db}.flow_records", rows, column_names=cols)
    monkeypatch.setattr(common, "CH_DB", db)
    import app.core.database as database
    import app.api.v1.manual_maps as manual_maps
    proxy = _ChProxy(client, db)
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: proxy)       # link_utilization
    monkeypatch.setattr(manual_maps, "get_clickhouse_client", lambda: proxy)
    yield client
    client.command(f"DROP DATABASE IF EXISTS {db}")


async def _seed_devices(db):
    from sqlalchemy import text

    ids = {"X": uuid.uuid4(), "Y": uuid.uuid4()}
    await db.execute(text(
        "INSERT INTO devices (id, hostname, ip_address, tags) VALUES "
        "(:x, 'a3b-gov-x', CAST('10.254.9.1' AS inet), '[\"gov-x\"]'::jsonb), "
        "(:y, 'a3b-gov-y', CAST('127.0.0.62' AS inet), '[\"gov-y\"]'::jsonb)"), {"x": ids["X"], "y": ids["Y"]})
    await db.execute(text(
        "INSERT INTO device_interfaces (device_id, if_index, if_name, if_speed) VALUES "
        "(:x, 4464, 'Gi0/1', 1000000000), (:x, 4466, 'Gi0/2', 1000000000), (:y, 1, 'eth1', 100000000)"),
        {"x": ids["X"], "y": ids["Y"]})
    for exporter in ("127.0.0.61", "2001:db8::61"):
        await db.execute(text(
            "INSERT INTO netflow_exporter_devices (exporter_ip, device_id) VALUES (CAST(:e AS inet), :d)"),
            {"e": exporter, "d": ids["X"]})
    await db.execute(text(
        "INSERT INTO roles (name, display_name, permissions) VALUES "
        "('a3b_gov_noflow', 'no flow', '[\"devices.view\", \"maps.view\"]'::jsonb) ON CONFLICT DO NOTHING"))
    await db.commit()
    return ids


async def _cleanup_devices(db):
    from sqlalchemy import text

    await db.rollback()
    await db.execute(text("DELETE FROM manual_maps WHERE name = 'a3b-gov-map'"))
    await db.execute(text("DELETE FROM netflow_exporter_devices WHERE host(exporter_ip) IN ('127.0.0.61', '2001:db8::61')"))
    await db.execute(text("DELETE FROM devices WHERE hostname LIKE 'a3b-gov-%'"))
    await db.execute(text("DELETE FROM roles WHERE name = 'a3b_gov_noflow'"))
    await db.commit()


def _viewer(scope=(), role="viewer"):
    return SimpleNamespace(id=uuid.uuid4(), role=role, scope_tags=list(scope), username="a3b-gov")


@needs_pg_ch
def test_link_detail_overlay_follows_ownership_and_visibility(flow_db):
    from app.api.v1 import link_utilization as lu
    from app.api.v1.netflow import access
    from app.core.security import invalidate_role_cache

    async def body(db):
        await _cleanup_devices(db)
        ids = await _seed_devices(db)
        invalidate_role_cache()
        access.invalidate_visibility_cache()
        try:
            async def detail(user, key, idx):
                return (await lu.link_detail(ids[key], idx, hours=1, from_time=None, to_time=None,
                                             db=db, user=user))["netflow"]

            team_x = _viewer(["gov-x"])
            nf = await detail(team_x, "X", 4464)
            # its management IP (ip-match) and both mapped exporters
            assert nf["status"] == "ok" and nf["exporters"] == ["10.254.9.1", "127.0.0.61", "2001:db8::61"]
            assert nf["has_flows"]
            # guarded rows (5 TB record, NSEL create event) are excluded
            assert sum(p["bytes"] for p in nf["protocols"]) == 1000 + 5000 + 700
            ips = {t["ip"] for t in nf["top_talkers"]}
            assert {"10.1.1.10", "93.184.216.34", "2001:db8:1::10"} <= ips
            assert not any(ip.startswith("::ffff:") for ip in ips) and "10.1.1.66" not in ips
            # Y is outside team X's tag scope: its link is not found at all (X-15, wave C),
            # the same answer GET /devices/{id} gives.
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as hidden:
                await detail(team_x, "Y", 1)
            assert hidden.value.status_code == 404
            assert (await detail(_viewer(["gov-y"]), "Y", 1))["has_flows"]
            assert (await detail(_viewer(role="a3b_gov_noflow"), "X", 4464))["status"] == "forbidden"
        finally:
            await _cleanup_devices(db)
            access.invalidate_visibility_cache()
            invalidate_role_cache()

    _run(body)


@needs_pg_ch
def test_maps_link_rates_are_scoped_to_visible_exporters(flow_db):
    from sqlalchemy import text
    from app.api.v1 import manual_maps as mm
    from app.api.v1.netflow import access
    from app.core.security import invalidate_role_cache

    async def body(db):
        await _cleanup_devices(db)
        ids = await _seed_devices(db)
        invalidate_role_cache()
        access.invalidate_visibility_cache()
        try:
            map_id = (await db.execute(text(
                "INSERT INTO manual_maps (name) VALUES ('a3b-gov-map') RETURNING id"))).scalar_one()
            nx = (await db.execute(text(
                "INSERT INTO manual_map_nodes (map_id, device_id) VALUES (:m, :d) RETURNING id"),
                {"m": map_id, "d": ids["X"]})).scalar_one()
            ny = (await db.execute(text(
                "INSERT INTO manual_map_nodes (map_id, device_id) VALUES (:m, :d) RETURNING id"),
                {"m": map_id, "d": ids["Y"]})).scalar_one()
            link = (await db.execute(text(
                "INSERT INTO manual_map_links (map_id, source_node_id, target_node_id, metadata) "
                "VALUES (:m, :a, :b, CAST(:md AS jsonb)) RETURNING id"),
                {"m": map_id, "a": nx, "b": ny,
                 "md": json.dumps({"src_interface": "Gi0/1", "dst_interface": "eth1"})})).scalar_one()
            await db.commit()

            async def rates(user):
                data = (await mm.links_live(map_id, db=db, user=user))["data"][str(link)]
                return data["source"], data["target"]

            window = mm._LIVE_WINDOW_SECONDS
            src, dst = await rates(_viewer())                      # unrestricted
            # X/Gi0/1 = ifIndex 4464 on both of X's exporters, guarded rows excluded
            assert src["in_bps"] == round((1000 + 700) * 8 / window, 1)
            assert src["out_bps"] == round(5000 * 8 / window, 1)
            assert dst["in_bps"] == round(800 * 8 / window, 1)
            src, dst = await rates(_viewer(["gov-x"]))             # Y's exporter hidden
            assert src["in_bps"] == round((1000 + 700) * 8 / window, 1)
            assert dst["in_bps"] is None                           # no SNMP sample either
            src, dst = await rates(_viewer(role="a3b_gov_noflow"))  # no netflow.view
            assert src["in_bps"] is None and dst["in_bps"] is None
        finally:
            await _cleanup_devices(db)
            access.invalidate_visibility_cache()
            invalidate_role_cache()

    _run(body)


@needs_pg_ch
def test_link_list_has_netflow_follows_mapping_and_visibility(flow_db):
    """X-47: an exporter linked through netflow_exporter_devices lights up its
    device's interfaces; exporters the caller cannot see never do."""
    from app.api.v1 import link_utilization as lu
    from app.api.v1.netflow import access
    from app.api.v1.netflow import common
    from app.core.security import invalidate_role_cache

    now = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=60)

    async def body(db):
        await _cleanup_devices(db)
        ids = await _seed_devices(db)
        flow_db.insert(f"{common.CH_DB}.snmp_if_metrics", [
            (ids["X"], 4464, now, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1000.0, 2000.0, ""),
            (ids["X"], 4466, now, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1000.0, 2000.0, ""),
            (ids["X"], 5000, now, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1000.0, 2000.0, ""),
            (ids["Y"], 1, now, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1000.0, 2000.0, ""),
        ], column_names=["device_id", "if_index", "timestamp", "in_octets", "out_octets", "in_errors",
                         "out_errors", "in_discards", "out_discards", "in_ucast_pkts", "out_ucast_pkts",
                         "oper_status", "in_bps", "out_bps", "poller_id"])
        from sqlalchemy import text
        await db.execute(text("INSERT INTO device_interfaces (device_id, if_index, if_name) VALUES (:x, 5000, 'Gi0/9')"),
                         {"x": ids["X"]})
        await db.commit()
        invalidate_role_cache()
        access.invalidate_visibility_cache()
        try:
            async def flags(user):
                out = await lu.list_links(hours=1, from_time=None, to_time=None, limit=500, search="a3b-gov",
                                          status=None, favorites_only=False, tag=None, min_util=None,
                                          issue=None, sort="name", db=db, user=user)
                return {(i["hostname"], i["if_index"]): i["has_netflow"] for i in out["items"]}, out["summary"]

            everyone, summary = await flags(_viewer())
            assert everyone == {("a3b-gov-x", 4464): True, ("a3b-gov-x", 4466): True,
                                ("a3b-gov-x", 5000): False, ("a3b-gov-y", 1): True}
            assert summary["with_netflow"] == 3
            team_x, _ = await flags(_viewer(["gov-x"]))
            # X-15 (wave C): a device outside the tag scope is not listed at all.
            assert team_x[("a3b-gov-x", 4464)] and ("a3b-gov-y", 1) not in team_x
            noflow, _ = await flags(_viewer(role="a3b_gov_noflow"))
            assert not any(noflow.values())
        finally:
            await _cleanup_devices(db)
            access.invalidate_visibility_cache()
            invalidate_role_cache()

    _run(body)
