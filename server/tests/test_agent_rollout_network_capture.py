from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.api.v1 import agents, servers
from app.core import database
from app.schemas.agent import AgentPackageDownloadRequest, NetworkCaptureUpload
from app.services import network_capture_service


class Result:
    def __init__(self, rows=None, rowcount=0):
        self.rows = list(rows or [])
        self.rowcount = rowcount

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def _request() -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/v1/agent-fleet/packages/download",
        "raw_path": b"/api/v1/agent-fleet/packages/download",
        "query_string": b"",
        "headers": [(b"host", b"controller.example")],
        "client": ("192.0.2.10", 12345),
        "server": ("controller.example", 443),
    })


def test_capabilities_prefer_explicit_values_and_use_per_feature_fallbacks():
    assert servers._agent_supports(
        {"version": "1.0.0", "capabilities": ["network_capture_v1"]},
        "network_capture_v1",
    )
    assert servers._agent_supports(
        {"version": "1.2.0", "capabilities": []}, "network_capture_v1"
    )
    assert not servers._agent_supports(
        {"version": "1.2.9", "capabilities": []}, "capture_stop_v1"
    )
    assert servers._agent_supports(
        {"version": "1.3.0", "capabilities": []}, "capture_stop_v1"
    )
    # An explicit capability set is authoritative; semver must not add a
    # feature that a newer agent intentionally did not advertise.
    assert not servers._agent_supports(
        {"version": "9.0.0", "capabilities": ["network_capture_v1"]},
        "capture_stop_v1",
    )


def test_heartbeat_capabilities_are_normalized_and_bounded():
    assert agents._capability_list([
        " Network_Capture_V1 ", "network_capture_v1", "CAPTURE_STOP_V1", ""
    ]) == ["network_capture_v1", "capture_stop_v1"]
    assert agents._capability_list('["INTERFACE_TRAFFIC_V1"]') == [
        "interface_traffic_v1"
    ]


def test_network_capture_upload_validates_interface_samples_and_limits():
    payload = NetworkCaptureUpload.model_validate({
        "capture_id": str(uuid4()),
        "status": "cancelled",
        "interfaces": [{
            "interface": "Ethernet0",
            "interface_index": 7,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "rx_bytes": 100,
            "tx_bytes": 200,
            "rx_bps": 800,
            "tx_bps": 1600,
            "link_speed_bps": 1_000_000_000,
        }],
    })
    assert payload.status == "cancelled"
    assert payload.interfaces[0].rx_bps == 800

    legacy_flow = NetworkCaptureUpload.model_validate({
        "capture_id": str(uuid4()),
        "flows": [{"protocol": "tcp", "local_port": 80}],
    }).flows[0]
    assert legacy_flow.direction == "unknown"
    assert legacy_flow.kind == "unknown"
    local_flow = NetworkCaptureUpload.model_validate({
        "capture_id": str(uuid4()),
        "flows": [{"direction": "local", "kind": "connection"}],
    }).flows[0]
    assert local_flow.direction == "local"

    with pytest.raises(ValidationError):
        servers.NetworkCaptureStart(max_flows=10_001)
    with pytest.raises(ValidationError):
        servers.NetworkCaptureStart(interface="x" * 256)
    assert servers.NetworkCaptureStart().retention_s == 3600
    with pytest.raises(ValidationError):
        servers.NetworkCaptureStart(retention_s=899)
    with pytest.raises(ValidationError):
        servers.NetworkCaptureStart(retention_s=604_801)


@pytest.mark.asyncio
async def test_capture_ingest_maps_legacy_kind_and_persists_new_classification(
    monkeypatch,
):
    capture_id, server_id, agent_id = uuid4(), uuid4(), uuid4()
    payload = NetworkCaptureUpload.model_validate({
        "capture_id": str(capture_id),
        "flows": [
            {"protocol": "tcp", "local_ip": "10.0.0.1", "local_port": 50000,
             "remote_ip": "198.51.100.8", "remote_port": 443, "pid": 100},
            {"protocol": "tcp", "direction": "inbound", "kind": "listener",
             "local_ip": "0.0.0.0", "local_port": 80, "pid": 4},
        ],
    })

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    inserts = []

    def insert_rows(table, rows, columns):
        inserts.append((table, rows, columns))

    class CaptureDB:
        def __init__(self):
            self.commits = 0

        async def execute(self, statement, params=None):
            sql = str(statement)
            if sql.lstrip().startswith("SELECT id, server_id, agent_id, status"):
                return Result([{
                    "id": capture_id, "server_id": server_id,
                    "agent_id": agent_id, "status": "running",
                }])
            if sql.lstrip().startswith("UPDATE network_captures SET"):
                return Result(rowcount=1)
            raise AssertionError(sql)

        async def commit(self):
            self.commits += 1

    db = CaptureDB()
    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "_insert_capture_rows", insert_rows)
    response = await agents.post_network_capture(
        payload, db=db, x_agent_id=str(agent_id), authorization="Bearer key"
    )

    assert response["accepted"] == 2
    assert len(inserts) == 1
    table, rows, columns = inserts[0]
    assert table == "zenplus.host_network_flows"
    first = dict(zip(columns, rows[0]))
    second = dict(zip(columns, rows[1]))
    assert first["direction"] == "unknown"
    assert first["kind"] == "connection"
    assert second["direction"] == "inbound"
    assert second["kind"] == "listener"
    assert db.commits == 1


def test_full_duplex_utilization_uses_max_direction_and_clamps():
    rx, tx, overall = servers._interface_utilization(
        800_000_000, 200_000_000, 1_000_000_000, 1_000_000_000
    )
    assert rx == pytest.approx(80.0)
    assert tx == pytest.approx(20.0)
    assert overall == pytest.approx(80.0)

    rx, tx, overall = servers._interface_utilization(
        1_500_000_000, 0, 1_000_000_000, 1_000_000_000
    )
    assert rx == 100.0
    assert overall == 100.0


def test_traffic_stats_return_current_average_peak_and_p95():
    samples = []
    now = datetime.now(timezone.utc)
    for value in range(20):
        samples.append({
            "timestamp": now,
            "rx_bytes": value,
            "tx_bytes": value * 2,
            "rx_bps": float(value),
            "tx_bps": float(value * 2),
            "peak_rx_bps": 25.0,
            "peak_tx_bps": 50.0,
            "rx_utilization_pct": float(value),
            "tx_utilization_pct": float(value) / 2,
            "utilization_pct": float(value),
        })
    stats = servers._traffic_stats(samples)
    assert stats["current"]["rx_bps"] == 19.0
    assert stats["avg"]["rx_bps"] == pytest.approx(9.5)
    assert stats["peak"]["rx_bps"] == 25.0
    assert stats["p95"]["rx_bps"] == pytest.approx(18.05)


def test_capture_reads_use_thread_local_clickhouse_clients(monkeypatch):
    calls = []

    class Client:
        def query(self, sql, parameters=None):
            calls.append((sql, parameters))
            return SimpleNamespace(result_rows=[("ok",)])

    client = Client()
    monkeypatch.setattr(database, "get_ch_client", lambda: client)
    monkeypatch.setattr(
        database,
        "get_clickhouse_client",
        lambda: (_ for _ in ()).throw(AssertionError("shared client used")),
    )

    assert servers._run_clickhouse_query("SELECT 1", {"value": 1}) == [("ok",)]
    assert servers._query_capture_traffic("capture-1") == [("ok",)]
    assert len(calls) == 2
    assert calls[0] == ("SELECT 1", {"value": 1})
    assert calls[1][1] == {"cid": "capture-1"}


@pytest.mark.asyncio
async def test_flow_list_and_summary_dedupe_snapshots_before_filtering(
    monkeypatch,
):
    capture_id, server_id, agent_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    meta = {
        "id": capture_id,
        "server_id": server_id,
        "agent_id": agent_id,
        "status": "running",
        "flow_count": 41,
        "bytes_available": True,
        "archived_at": None,
        "purge_after": None,
        "requested_by": uuid4(),
        "requested_by_name": "admin",
    }

    class MetaDB:
        async def execute(self, statement, params=None):
            assert "network_captures" in str(statement)
            return Result([meta])

    queries = []

    def run_query(sql, parameters):
        queries.append((sql, parameters))
        normalized = sql.lstrip()
        if normalized.startswith("SELECT count()"):
            return [(1,)]
        if normalized.startswith("SELECT protocol"):
            return [("tcp", "outbound", "connection",
                     "10.0.0.1", 50123, "198.51.100.5", 443, 42,
                     "web.exe", "https", "established", 100, 200, 1,
                     now, now, 3)]
        if normalized.startswith("SELECT process_name"):
            return [("web.exe", "https", 100, 200, 1)]
        if normalized.startswith("SELECT remote_ip"):
            return [("198.51.100.5", 100, 200, 1)]
        raise AssertionError(sql)

    monkeypatch.setattr(servers, "_run_clickhouse_query", run_query)
    flows = await servers.network_capture_flows(
        capture_id,
        q="443",
        protocol="tcp",
        direction="outbound",
        kind="connection",
        scope="applications",
        bytes_known=True,
        sort="bytes_total",
        order="desc",
        page=1,
        page_size=50,
        db=MetaDB(),
        user=SimpleNamespace(id=uuid4()),
    )
    summary = await servers.network_capture_summary(
        capture_id, db=MetaDB(), user=SimpleNamespace(id=uuid4())
    )

    assert flows["total"] == 1
    assert flows["capture"]["flow_count"] == 41
    assert flows["items"][0]["bytes_sent"] == 100
    assert flows["items"][0]["direction"] == "outbound"
    assert flows["items"][0]["kind"] == "connection"
    assert flows["items"][0]["scope"] == "applications"
    assert summary["top_processes"][0]["bytes_received"] == 200
    assert len(queries) == 4
    for sql, parameters in queries:
        assert "argMax(bytes_sent, observed_at) AS bytes_sent" in sql
        assert "argMax(bytes_received, observed_at) AS bytes_received" in sql
        assert "argMax(direction, observed_at)" in sql
        assert "'unknown') AS direction" in sql
        assert "argMax(kind, observed_at)" in sql
        assert "'connection') AS kind" in sql
        assert "GROUP BY protocol, local_ip, local_port" in sql
        assert parameters["cid"] == str(capture_id)

    list_queries = [sql for sql, _ in queries
                    if "positionCaseInsensitive" in sql]
    assert len(list_queries) == 2
    for sql in list_queries:
        # The user filter appears after the connection-level GROUP BY, so it
        # can only match the newest snapshot selected by argMax.
        assert sql.rfind("positionCaseInsensitive") > sql.find(
            "GROUP BY protocol, local_ip, local_port"
        )
        assert "local_port = %(qport)s" in sql
        assert "remote_port = %(qport)s" in sql
        assert "direction = %(direction)s" in sql
        assert "kind = %(kind)s" in sql
        assert "bytes_known = %(bytes_known)s" in sql
        assert "NOT (pid <= 4 OR empty(trimBoth(process_name)))" in sql


@pytest.mark.asyncio
async def test_flow_all_filter_values_keep_application_scope_without_hiding_kinds(
    monkeypatch,
):
    capture_id = uuid4()
    meta = {
        "id": capture_id, "server_id": uuid4(), "status": "completed",
        "flow_count": 12, "bytes_available": False,
        "archived_at": None, "purge_after": None,
    }

    class MetaDB:
        async def execute(self, statement, params=None):
            return Result([meta])

    queries = []

    def run_query(sql, parameters):
        queries.append((sql, parameters))
        return [(0,)] if sql.lstrip().startswith("SELECT count()") else []

    monkeypatch.setattr(servers, "_run_clickhouse_query", run_query)
    response = await servers.network_capture_flows(
        capture_id,
        q=None,
        protocol="all",
        direction="all",
        kind="all",
        scope="applications",
        bytes_known=None,
        sort="bytes_total",
        order="desc",
        page=1,
        page_size=50,
        db=MetaDB(),
        user=SimpleNamespace(id=uuid4()),
    )

    assert response["total"] == 0
    assert response["capture"]["flow_count"] == 12
    for sql, params in queries:
        assert "protocol = %(proto)s" not in sql
        assert "direction = %(direction)s" not in sql
        assert "kind = %(kind)s" not in sql
        assert "bytes_known = %(bytes_known)s" not in sql
        assert "NOT (pid <= 4 OR empty(trimBoth(process_name)))" in sql
        assert set(params) == {"cid", "lim", "off"}


@pytest.mark.asyncio
async def test_package_download_streams_exact_package_without_enrollment_secret(
    tmp_path, monkeypatch, caplog
):
    package_root = tmp_path / "agents"
    windows = package_root / "windows"
    windows.mkdir(parents=True)
    package = windows / "zenplus-agent-1.3.0.msi"
    original = b"signed-msi\x00zpa_enr_PLACEHOLDERTOKENPLACEHOLDERTOKEN\x00end"
    package.write_bytes(original)
    package_sha = hashlib.sha256(original).hexdigest()
    package_id = uuid4()

    async def sync(_db):
        return {"added": [], "updated": [], "removed": []}

    async def latest(_platform, _db):
        return {
            "id": package_id,
            "file_name": package.name,
            "file_size": len(original),
            "sha256": package_sha,
            "version": "1.3.0",
        }

    async def server_url(_request, _db):
        return "https://controller.example"

    class PackageDB:
        def __init__(self):
            self.calls = []
            self.commits = 0

        async def execute(self, statement, params=None):
            self.calls.append((str(statement), params or {}))
            return Result()

        async def commit(self):
            self.commits += 1

    db = PackageDB()
    monkeypatch.setattr(agents, "AGENT_PKG_DIR", package_root)
    monkeypatch.setattr(servers, "sync_agent_packages", sync)
    monkeypatch.setattr(servers, "_latest_package_for_download", latest)
    monkeypatch.setattr(servers, "_server_url", server_url)
    caplog.set_level(logging.INFO, logger="zenplus.servers")

    response = await servers.download_preconfigured_package(
        AgentPackageDownloadRequest(platform="windows"),
        _request(), db, SimpleNamespace(id=uuid4()),
    )

    assert Path(response.path).read_bytes() == original
    assert response.headers["x-controller-url"] == "https://controller.example"
    assert response.headers["x-package-sha256"] == package_sha
    assert response.headers["x-package-version"] == "1.3.0"
    assert response.headers["cache-control"] == "no-store, private"
    assert "x-enrollment-token" not in response.headers
    assert not any("agent_enrollment_tokens" in sql for sql, _ in db.calls)
    assert "token=" not in caplog.text


def test_windows_bootstrap_supports_canonical_setup_without_endpoint_secrets():
    script = agents._INSTALL_PS1

    assert "$manifest.file_name" in script
    assert '$extension -eq ".exe"' in script
    assert '"/machine", "/quiet", "/norestart"' in script
    assert '$extension -eq ".msi"' in script
    assert "ENROLLMENT_TOKEN" not in script
    assert "SITE_ID" not in script
    assert "POLICY_ID" not in script


@pytest.mark.asyncio
async def test_package_sync_detects_same_size_same_name_sha_change(tmp_path, monkeypatch):
    package_root = tmp_path / "agents"
    windows = package_root / "windows"
    windows.mkdir(parents=True)
    payload = b"new-content-same-size"
    package = windows / "zenplus-agent-1.3.0.msi"
    package.write_bytes(payload)

    class SyncDB:
        def __init__(self):
            self.sql = []
            self.commits = 0

        async def execute(self, statement, params=None):
            sql = str(statement)
            self.sql.append((sql, params or {}))
            if "FROM agent_packages" in sql and sql.lstrip().startswith("SELECT"):
                return Result([{
                    "platform": "windows", "arch": "amd64", "version": "1.3.0",
                    "channel": "stable", "file_name": package.name,
                    "file_size": len(payload), "sha256": "0" * 64,
                }])
            return Result()

        async def commit(self):
            self.commits += 1

    db = SyncDB()
    monkeypatch.setattr(agents, "AGENT_PKG_DIR", package_root)
    result = await servers.sync_agent_packages(db)
    assert not any("INSERT INTO agent_packages" in sql for sql, _ in db.sql)
    assert any("SET is_latest = FALSE" in sql for sql, _ in db.sql)
    assert result["updated"] == []
    assert result["quarantined"] == [f"windows/{package.name}"]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_package_sync_commits_latest_flag_repairs(tmp_path, monkeypatch):
    package_root = tmp_path / "agents"
    windows = package_root / "windows"
    windows.mkdir(parents=True)
    payload = b"immutable-msi"
    package = windows / "zenplus-agent-1.3.0.msi"
    package.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    class RepairDB:
        def __init__(self):
            self.calls = []
            self.commits = 0

        async def execute(self, statement, params=None):
            sql = str(statement)
            self.calls.append((sql, params or {}))
            if "FROM agent_packages" in sql and sql.lstrip().startswith("SELECT"):
                return Result([{
                    "platform": "windows", "arch": "amd64", "version": "1.3.0",
                    "channel": "stable", "file_name": package.name,
                    "file_size": len(payload), "sha256": digest,
                }])
            if "is_latest IS DISTINCT FROM" in sql:
                return Result(rowcount=1)
            return Result()

        async def commit(self):
            self.commits += 1

    db = RepairDB()
    monkeypatch.setattr(agents, "AGENT_PKG_DIR", package_root)
    result = await servers.sync_agent_packages(db)

    assert result["repaired"] == [f"windows/{package.name}"]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_package_sync_and_media_type_accept_macos_pkg(tmp_path, monkeypatch):
    package_root = tmp_path / "agents"
    macos = package_root / "macos"
    macos.mkdir(parents=True)
    package = macos / "zenplus-agent-1.3.0.pkg"
    package.write_bytes(b"signed-macos-package")

    class PackageDB:
        def __init__(self):
            self.commits = 0

        async def execute(self, statement, params=None):
            sql = str(statement)
            if "FROM agent_packages" in sql and sql.lstrip().startswith("SELECT"):
                return Result()
            if "INSERT INTO agent_packages" in sql:
                return Result([(uuid4(),)])
            return Result()

        async def commit(self):
            self.commits += 1

    db = PackageDB()
    monkeypatch.setattr(agents, "AGENT_PKG_DIR", package_root)
    result = await servers.sync_agent_packages(db)

    assert result["added"] == [f"macos/{package.name}"]
    assert agents._package_media_type(package.name) == "application/vnd.apple.installer+xml"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_public_package_download_refuses_digest_mismatch(tmp_path, monkeypatch):
    package_root = tmp_path / "agents"
    windows = package_root / "windows"
    windows.mkdir(parents=True)
    package = windows / "zenplus-agent-1.3.0.msi"
    package.write_bytes(b"mutated-after-publication")
    published = {
        "file_name": package.name,
        "file_size": package.stat().st_size,
        "sha256": "0" * 64,
        "version": "1.3.0",
    }

    class PackageDB:
        async def execute(self, statement, params=None):
            return Result([published])

    monkeypatch.setattr(agents, "AGENT_PKG_DIR", package_root)
    with pytest.raises(HTTPException) as error:
        await agents.download_latest_package("windows", "amd64", PackageDB())
    assert error.value.status_code == 409
    assert "integrity" in error.value.detail.lower()


@pytest.mark.asyncio
async def test_authenticated_package_download_refuses_mismatch_before_token_mint(
    tmp_path, monkeypatch
):
    package_root = tmp_path / "agents"
    windows = package_root / "windows"
    windows.mkdir(parents=True)
    package = windows / "zenplus-agent-1.3.0.msi"
    package.write_bytes(b"mutated-after-publication")
    published = {
        "file_name": package.name,
        "file_size": package.stat().st_size,
        "sha256": "0" * 64,
        "version": "1.3.0",
    }

    async def sync(_db):
        return {"added": [], "updated": [], "removed": [],
                "quarantined": [], "repaired": []}

    async def latest(_platform, _db):
        return published

    class PackageDB:
        def __init__(self):
            self.calls = []

        async def execute(self, statement, params=None):
            self.calls.append(str(statement))
            return Result()

    db = PackageDB()
    monkeypatch.setattr(agents, "AGENT_PKG_DIR", package_root)
    monkeypatch.setattr(servers, "sync_agent_packages", sync)
    monkeypatch.setattr(servers, "_latest_package_for_download", latest)

    with pytest.raises(HTTPException) as error:
        await servers.download_preconfigured_package(
            AgentPackageDownloadRequest(platform="windows"),
            _request(), db, SimpleNamespace(id=uuid4()),
        )
    assert error.value.status_code == 409
    assert not any("INSERT INTO agent_enrollment_tokens" in sql for sql in db.calls)


class ClaimDB:
    def __init__(self, token, *, previous=False):
        self.token = token
        self.previous = previous
        self.calls = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if "WHERE token_hash = :h" in sql:
            return Result([self.token])
        if "SELECT token_id FROM agent_enrollment_claims" in sql:
            return Result([(self.token["id"],)] if self.previous else [])
        if "UPDATE agent_enrollment_claims" in sql:
            return Result(rowcount=1)
        if "INSERT INTO agent_enrollment_claims" in sql:
            return Result(rowcount=1)
        if "UPDATE agent_enrollment_tokens SET" in sql:
            return Result([(self.token["id"],)])
        raise AssertionError(sql)


def _enrollment_token(*, uses=0, max_uses=1):
    return {
        "id": uuid4(),
        "platform": "windows",
        "revoked_at": None,
        "expires_at": None,
        "uses": uses,
        "max_uses": max_uses,
    }


@pytest.mark.asyncio
async def test_token_retry_same_agent_uid_does_not_consume_use_at_capacity():
    db = ClaimDB(_enrollment_token(uses=1, max_uses=1), previous=True)
    token, first_claim = await agents._claim_enrollment_token(
        db,
        token_hash="a" * 64,
        agent_uid=" host-01 ",
        platform="windows",
        client_ip="192.0.2.10",
    )

    assert token["uses"] == 1
    assert first_claim is False
    assert "FOR UPDATE" in db.calls[0][0]
    assert all(params.get("agent_uid", "host-01") == "host-01"
               for _, params in db.calls)
    assert not any("UPDATE agent_enrollment_tokens SET" in sql
                   for sql, _ in db.calls)


@pytest.mark.asyncio
async def test_token_first_claim_inserts_unique_host_and_consumes_one_use():
    db = ClaimDB(_enrollment_token(uses=0, max_uses=2))
    _, first_claim = await agents._claim_enrollment_token(
        db,
        token_hash="b" * 64,
        agent_uid="host-02",
        platform="windows",
        client_ip="192.0.2.11",
    )

    assert first_claim is True
    assert any("INSERT INTO agent_enrollment_claims" in sql for sql, _ in db.calls)
    assert sum("UPDATE agent_enrollment_tokens SET" in sql for sql, _ in db.calls) == 1


@pytest.mark.asyncio
async def test_token_new_host_is_rejected_when_unique_claim_capacity_is_full():
    db = ClaimDB(_enrollment_token(uses=2, max_uses=2))
    with pytest.raises(HTTPException) as error:
        await agents._claim_enrollment_token(
            db,
            token_hash="c" * 64,
            agent_uid="host-03",
            platform="windows",
            client_ip="192.0.2.12",
        )

    assert error.value.status_code == 401
    assert not any("INSERT INTO agent_enrollment_claims" in sql for sql, _ in db.calls)


@pytest.mark.asyncio
async def test_unlimited_tokens_are_listed_with_nullable_remaining_uses():
    token_id = uuid4()
    now = datetime.now(timezone.utc)
    row = {
        "id": token_id, "token_prefix": "zpa_enr_test", "platform": "windows",
        "hostname_hint": "fleet", "tags": [], "expires_at": now,
        "max_uses": 0, "uses": 12, "consumed_at": now, "consumed_ip": None,
        "revoked_at": None, "created_at": now, "server_id": None,
        "policy_id": None, "created_by_name": "admin", "policy_name": None,
        "server_name": None,
    }

    class TokenDB:
        async def execute(self, statement, params=None):
            self.sql = str(statement)
            return Result([row])

    db = TokenDB()
    response = await servers.list_enrollment_tokens(
        include_expired=False, limit=100, db=db, user=SimpleNamespace(id=uuid4())
    )
    assert "t.max_uses = 0 OR t.uses < t.max_uses" in db.sql
    assert response["items"][0]["unlimited"] is True
    assert response["items"][0]["remaining_uses"] is None


@pytest.mark.asyncio
async def test_concurrent_capture_unique_violation_returns_409():
    now = datetime.now(timezone.utc)

    class ConflictDB:
        def __init__(self):
            self.rolled_back = False

        async def execute(self, statement, params=None):
            sql = str(statement)
            if "FROM agents WHERE server_id" in sql:
                return Result([{
                    "id": uuid4(), "status": "online", "version": "1.3.0",
                    "capabilities": ["network_capture_v1"],
                    "last_heartbeat_at": now,
                }])
            if "SELECT id FROM network_captures" in sql:
                return Result()
            if "INSERT INTO network_captures" in sql:
                raise IntegrityError("insert", {}, Exception("duplicate"))
            raise AssertionError(sql)

        async def rollback(self):
            self.rolled_back = True

    db = ConflictDB()
    with pytest.raises(HTTPException) as error:
        await servers.start_network_capture(
            uuid4(), servers.NetworkCaptureStart(), db, SimpleNamespace(id=uuid4())
        )
    assert error.value.status_code == 409
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_capture_creation_persists_configured_post_completion_retention():
    now = datetime.now(timezone.utc)
    capture_id, agent_id = uuid4(), uuid4()

    class StartDB:
        def __init__(self):
            self.calls = []
            self.commits = 0

        async def execute(self, statement, params=None):
            sql = str(statement)
            self.calls.append((sql, params or {}))
            if "FROM agents WHERE server_id" in sql:
                return Result([{
                    "id": agent_id, "status": "online", "version": "1.3.0",
                    "capabilities": ["network_capture_v1"],
                    "last_heartbeat_at": now,
                }])
            if "SELECT id FROM network_captures" in sql:
                return Result()
            if "INSERT INTO network_captures" in sql:
                return Result([(capture_id, now)])
            if "INSERT INTO agent_commands" in sql:
                return Result(rowcount=1)
            raise AssertionError(sql)

        async def commit(self):
            self.commits += 1

    db = StartDB()
    response = await servers.start_network_capture(
        uuid4(),
        servers.NetworkCaptureStart(duration_s=30, retention_s=7200),
        db,
        SimpleNamespace(id=uuid4()),
    )

    insert_params = next(params for sql, params in db.calls
                         if "INSERT INTO network_captures" in sql)
    assert insert_params["ret"] == 7200
    assert response["retention_s"] == 7200
    assert response["purge_after"] is None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_archive_suppresses_purge_even_while_capture_is_active():
    capture_id = uuid4()
    archived_at = datetime.now(timezone.utc)

    class ArchiveDB:
        def __init__(self):
            self.calls = []
            self.commits = 0

        async def execute(self, statement, params=None):
            sql = str(statement)
            self.calls.append((sql, params or {}))
            if sql.lstrip().startswith("SELECT id, status, retention_s"):
                return Result([{
                    "id": capture_id, "status": "running", "retention_s": 3600,
                    "archived_at": None, "purge_after": None,
                }])
            if "UPDATE network_captures SET" in sql:
                return Result([{
                    "status": "running", "retention_s": 3600,
                    "archived_at": archived_at, "purge_after": None,
                }])
            raise AssertionError(sql)

        async def commit(self):
            self.commits += 1

    db = ArchiveDB()
    response = await servers.archive_network_capture(
        capture_id, db, SimpleNamespace(id=uuid4())
    )

    update_sql = next(sql for sql, _ in db.calls if "UPDATE network_captures SET" in sql)
    assert "purge_after = NULL" in update_sql
    assert response["status"] == "running"
    assert response["archived"] is True
    assert response["purge_after"] is None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_unarchive_terminal_capture_grants_fresh_retention_window():
    capture_id = uuid4()
    now = datetime.now(timezone.utc)
    fresh_deadline = now.replace(microsecond=0)

    class UnarchiveDB:
        def __init__(self):
            self.calls = []
            self.commits = 0

        async def execute(self, statement, params=None):
            sql = str(statement)
            self.calls.append((sql, params or {}))
            if sql.lstrip().startswith("SELECT id, status, retention_s"):
                return Result([{
                    "id": capture_id, "status": "completed", "retention_s": 7200,
                    "archived_at": now, "purge_after": None,
                }])
            if "UPDATE network_captures SET" in sql:
                return Result([{
                    "status": "completed", "retention_s": 7200,
                    "archived_at": None, "purge_after": fresh_deadline,
                }])
            raise AssertionError(sql)

        async def commit(self):
            self.commits += 1

    db = UnarchiveDB()
    response = await servers.unarchive_network_capture(
        capture_id, db, SimpleNamespace(id=uuid4())
    )

    update_sql = next(sql for sql, _ in db.calls if "UPDATE network_captures SET" in sql)
    assert "NOW() + make_interval(secs => retention_s)" in update_sql
    assert response["archived"] is False
    assert response["purge_after"] == fresh_deadline
    assert db.commits == 1


@pytest.mark.asyncio
async def test_explicit_purge_rejects_active_capture_before_clickhouse(monkeypatch):
    capture_id = uuid4()
    called = False

    def delete_data(_capture_id):
        nonlocal called
        called = True

    class ActiveDB:
        async def execute(self, statement, params=None):
            return Result([{
                "id": capture_id, "status": "stopping", "archived_at": None,
            }])

    monkeypatch.setattr(servers, "delete_capture_clickhouse_data", delete_data)
    with pytest.raises(HTTPException) as error:
        await servers.purge_network_capture(
            capture_id, ActiveDB(), SimpleNamespace(id=uuid4())
        )
    assert error.value.status_code == 409
    assert called is False


@pytest.mark.asyncio
async def test_explicit_purge_deletes_clickhouse_before_postgres(monkeypatch):
    capture_id = uuid4()
    events = []

    def delete_data(value):
        events.append(("clickhouse", str(value)))

    class PurgeDB:
        def __init__(self):
            self.commits = 0

        async def execute(self, statement, params=None):
            sql = str(statement)
            if sql.lstrip().startswith("SELECT id, status, archived_at"):
                return Result([{
                    "id": capture_id, "status": "completed",
                    "archived_at": datetime.now(timezone.utc),
                }])
            if sql.lstrip().startswith("DELETE FROM network_captures"):
                events.append(("postgres", str(params["id"])))
                return Result([(capture_id,)])
            raise AssertionError(sql)

        async def commit(self):
            self.commits += 1

    db = PurgeDB()
    monkeypatch.setattr(servers, "delete_capture_clickhouse_data", delete_data)
    response = await servers.purge_network_capture(
        capture_id, db, SimpleNamespace(id=uuid4())
    )

    assert events == [("clickhouse", str(capture_id)),
                      ("postgres", str(capture_id))]
    assert response["status"] == "purged"
    assert response["was_archived"] is True
    assert db.commits == 1


@pytest.mark.asyncio
async def test_explicit_purge_retains_metadata_when_clickhouse_fails(monkeypatch):
    capture_id = uuid4()

    def delete_data(_capture_id):
        raise RuntimeError("ClickHouse unavailable")

    class FailureDB:
        def __init__(self):
            self.deleted = False
            self.rolled_back = False

        async def execute(self, statement, params=None):
            sql = str(statement)
            if sql.lstrip().startswith("SELECT id, status, archived_at"):
                return Result([{
                    "id": capture_id, "status": "failed", "archived_at": None,
                }])
            if sql.lstrip().startswith("DELETE FROM network_captures"):
                self.deleted = True
            return Result()

        async def rollback(self):
            self.rolled_back = True

    db = FailureDB()
    monkeypatch.setattr(servers, "delete_capture_clickhouse_data", delete_data)
    with pytest.raises(HTTPException) as error:
        await servers.purge_network_capture(
            capture_id, db, SimpleNamespace(id=uuid4())
        )
    assert error.value.status_code == 503
    assert db.deleted is False
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_stop_rejects_agent_without_stop_capability():
    capture_id, agent_id = uuid4(), uuid4()

    class StopDB:
        async def execute(self, statement, params=None):
            sql = str(statement)
            if "FROM network_captures c" in sql:
                return Result([{
                    "id": capture_id, "agent_id": agent_id, "status": "running",
                    "agent_version": "1.2.0",
                    "agent_capabilities": ["network_capture_v1"],
                }])
            if "command = 'start_network_capture'" in sql:
                return Result()
            raise AssertionError(sql)

    with pytest.raises(HTTPException) as error:
        await servers.cancel_network_capture(
            capture_id, StopDB(), SimpleNamespace(id=uuid4())
        )
    assert error.value.status_code == 409
    assert "capture_stop_v1" in error.value.detail


@pytest.mark.asyncio
async def test_failed_capture_command_updates_control_row():
    class RecordingDB:
        def __init__(self):
            self.calls = []

        async def execute(self, statement, params=None):
            self.calls.append((str(statement), params or {}))
            return Result()

    db = RecordingDB()
    capture_id = uuid4()
    await network_capture_service.reconcile_capture_command_result(
        db,
        command="start_network_capture",
        params={"capture_id": str(capture_id)},
        success=False,
        error_message="unsupported command",
    )
    sql, params = db.calls[0]
    assert "status = 'failed'" in sql
    assert params == {"id": str(capture_id), "error": "unsupported command"}


@pytest.mark.asyncio
async def test_sweeper_advisory_lock_skips_second_worker():
    class LockedDB:
        def __init__(self):
            self.rolled_back = False

        async def execute(self, statement, params=None):
            assert "pg_try_advisory_xact_lock" in str(statement)
            return Result([(False,)])

        async def rollback(self):
            self.rolled_back = True

    db = LockedDB()
    result = await network_capture_service.sweep_stale_captures(db)
    assert result == {"completed": 0, "failed": 0, "cancelled": 0,
                      "expired_commands": 0}
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_retention_worker_purges_due_unarchived_capture_from_both_stores(
    monkeypatch,
):
    capture_id = uuid4()
    events = []

    def delete_data(value):
        events.append(("clickhouse", str(value)))

    class RetentionDB:
        def __init__(self):
            self.commits = 0

        async def execute(self, statement, params=None):
            sql = str(statement)
            if "pg_try_advisory_xact_lock" in sql:
                return Result([(True,)])
            if sql.lstrip().startswith("SELECT id"):
                assert "archived_at IS NULL" in sql
                assert "purge_after <= NOW()" in sql
                assert "FOR UPDATE SKIP LOCKED" in sql
                return Result([(capture_id,)])
            if sql.lstrip().startswith("DELETE FROM network_captures"):
                events.append(("postgres", str(params["id"])))
                return Result([(capture_id,)])
            raise AssertionError(sql)

        async def commit(self):
            self.commits += 1

    db = RetentionDB()
    monkeypatch.setattr(
        network_capture_service, "delete_capture_clickhouse_data", delete_data
    )
    result = await network_capture_service.purge_expired_captures(db)

    assert result == {"purged": 1, "failed": 0}
    assert events == [("clickhouse", str(capture_id)),
                      ("postgres", str(capture_id))]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_retention_worker_keeps_postgres_row_on_clickhouse_failure(monkeypatch):
    capture_id = uuid4()

    def delete_data(_value):
        raise RuntimeError("mutation failed")

    class RetentionDB:
        def __init__(self):
            self.deleted = False
            self.commits = 0

        async def execute(self, statement, params=None):
            sql = str(statement)
            if "pg_try_advisory_xact_lock" in sql:
                return Result([(True,)])
            if sql.lstrip().startswith("SELECT id"):
                return Result([(capture_id,)])
            if sql.lstrip().startswith("DELETE FROM network_captures"):
                self.deleted = True
            return Result()

        async def commit(self):
            self.commits += 1

    db = RetentionDB()
    monkeypatch.setattr(
        network_capture_service, "delete_capture_clickhouse_data", delete_data
    )
    result = await network_capture_service.purge_expired_captures(db)

    assert result == {"purged": 0, "failed": 1}
    assert db.deleted is False
    assert db.commits == 1


def test_clickhouse_capture_purge_targets_flows_and_traffic(monkeypatch):
    capture_id = uuid4()

    class Client:
        def __init__(self):
            self.commands = []

        def command(self, sql):
            self.commands.append(sql)

    client = Client()
    from app.core import database
    monkeypatch.setattr(database, "get_ch_client", lambda: client)
    network_capture_service.delete_capture_clickhouse_data(capture_id)

    assert len(client.commands) == 2
    assert "zenplus.host_network_flows" in client.commands[0]
    assert "zenplus.host_network_traffic_samples" in client.commands[1]
    assert all(str(capture_id) in sql for sql in client.commands)
    assert all("mutations_sync = 1" in sql for sql in client.commands)


@pytest.mark.asyncio
async def test_command_poll_commits_expirations_when_queue_is_empty(monkeypatch):
    async def authenticate(*_args):
        return {"id": uuid4()}

    async def expire(*_args, **_kwargs):
        return 1

    class EmptyDB:
        def __init__(self):
            self.commits = 0

        async def execute(self, statement, params=None):
            return Result()

        async def commit(self):
            self.commits += 1

    db = EmptyDB()
    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(network_capture_service, "expire_agent_commands", expire)
    result = await agents.commands_poll(db=db, x_agent_id="agent", authorization="key")
    assert result.has_commands is False
    assert db.commits == 1


def test_capture_migrations_cover_live_upgrade_and_fresh_install():
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    migration_045 = (scripts / "migrate-045-network-capture-clickhouse.sql").read_text()
    migration_046 = (scripts / "migrate-046-network-capture-single-active.sql").read_text()
    migration_047 = (scripts / "migrate-047-network-capture-lifecycle.sql").read_text()
    migration_048 = (scripts / "migrate-048-network-capture-traffic-clickhouse.sql").read_text()
    migration_049 = (scripts / "migrate-049-agent-enrollment-claims.sql").read_text()
    migration_050 = (scripts / "migrate-050-network-capture-retention.sql").read_text()
    migration_051 = (
        scripts / "migrate-051-network-capture-flow-classification-clickhouse.sql"
    ).read_text()
    assert "host_network_flows" in migration_045
    assert "host_network_traffic_samples" not in migration_045
    assert "host_network_traffic_samples" in migration_048
    assert "'stopping'" not in migration_046
    assert "'stopping'" in migration_047
    assert "stop_network_capture" in migration_047
    assert "capabilities JSONB" in migration_047
    assert "agent_enrollment_claims" in migration_049
    assert "PRIMARY KEY (token_id, agent_uid)" in migration_049
    assert "retention_s BETWEEN 900 AND 604800" in migration_050
    assert "set_network_capture_retention" in migration_050
    assert "COALESCE(NEW.completed_at, NOW())" in migration_050
    assert "IF NEW.archived_at IS NOT NULL" in migration_050
    assert "NOW() + make_interval(secs => retention_s)" in migration_050
    assert "ADD COLUMN IF NOT EXISTS direction" in migration_051
    assert "DEFAULT 'unknown'" in migration_051
    assert "ADD COLUMN IF NOT EXISTS kind" in migration_051
    assert "DEFAULT 'connection'" in migration_051

    locked = {}
    for line in (scripts / "migrations.lock").read_text().splitlines():
        digest, name = line.split(None, 1)
        locked[name.strip()] = digest
    for name in (
        "migrate-045-network-capture-clickhouse.sql",
        "migrate-046-network-capture-single-active.sql",
        "migrate-047-network-capture-lifecycle.sql",
        "migrate-048-network-capture-traffic-clickhouse.sql",
        "migrate-049-agent-enrollment-claims.sql",
        "migrate-050-network-capture-retention.sql",
        "migrate-051-network-capture-flow-classification-clickhouse.sql",
    ):
        assert hashlib.sha256((scripts / name).read_bytes()).hexdigest() == locked[name]
