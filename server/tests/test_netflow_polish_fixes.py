"""NetFlow development review, wave C polish: the four items the final
regression left open.

1. Detector exceptions are scoped to exporters (migrate-140,
   ``netflow_detector_exceptions.exporter_ips``): a tag-scoped manager's
   exception is bound to the exporters it may see and never suppresses
   another team's findings; matching honours the scope; the list shows a
   scoped caller the global exceptions and those touching its exporters
   only, never naming another team's exporters; changing or removing one
   outside the caller's scope is 404 (out of sight) or 403 (global, or
   partly another team's).
2. Report run share tokens: ``GET /report-schedules/{id}/runs`` and
   ``GET /reports/runs`` need reports.view and list a token only to the
   schedule's owner and reports.manage holders; the unauthenticated
   ``/reports/shared/{token}`` page stays the email capability but is kept
   out of caches, referrers and indexes.
3. Report surfaces that show alert messages (the "noisy sources" and
   "recent alerts" sections, ``/reports/data/technical``, the legacy
   PDF/Excel/CSV alert tables, scheduled legacy attachments) withhold a
   NetFlow alert's message from a requester without netflow.view, with the
   Alert Center's own redaction (``netflow_alert_service.redact_alert_payload``).
4. (frontend only, covered by the dashboard checks.)
Also: the threat-intel refresh loop's due-feeds query (found failing in the
API log during this pass).

Layers: pure unit tests; a PostgreSQL layer (``ZENPLUS_NETFLOW_TEST_PG``,
``ZENPLUS_NETFLOW_TEST_PG_ADMIN`` for the migration replay); HTTP end to end
against a running API (``ZENPLUS_NETFLOW_E2E_URL`` + ``ZENPLUS_NETFLOW_E2E_PG``,
the API's own database). Fixtures are prefixed ``nfpol`` and removed
afterwards.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from app.api.v1 import report_schedules as RS
from app.api.v1.netflow import access
from app.api.v1.netflow import anomalies as AN
from app.api.v1.netflow.access import FlowVisibility
from app.api.v1.netflow.common import NetflowError
from app.services import netflow_alert_service as nfa
from app.services import netflow_detection as det
from app.services import report_sections as RSEC
from app.services import report_service

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
MIGRATION = SCRIPTS / "migrate-140-netflow-exception-scope.sql"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _vis(exporters=None, perms=("netflow.view",)):
    return FlowVisibility(
        restricted=exporters is not None,
        exporters=None if exporters is None else frozenset(exporters),
        user_id=None, permissions=frozenset(perms),
    )


def _cand(exporter: str, src: str = "172.30.91.10", kind: str = "port_scan") -> det.Candidate:
    return det.Candidate(
        kind=kind, exporter=exporter, subject=(src,), severity="warning", confidence="medium", title="t",
        description="d", first_seen=T0, last_seen=T0, latest=(T0, T0), occurrences=1, method="m", threshold={},
        observed={}, evidence_filter={}, src=src, host=src,
    )


def _rule(exporters=None, **kw) -> det.ExceptionRule:
    base = dict(id="x1", kind="port_scan", src=ipaddress.ip_network("172.30.0.0/16"), dst=None, dst_port=None,
                reason="approved", expires_at=None, exporters=exporters)
    base.update(kw)
    return det.ExceptionRule(**base)


# ── 1. Exception scope: matching ─────────────────────────────────────────────

def test_an_exporter_scoped_exception_matches_its_exporters_only():
    team_a = _rule(frozenset({"10.0.0.1"}))
    assert team_a.matches(_cand("10.0.0.1"), T0)
    assert team_a.matches(_cand("::ffff:10.0.0.1"), T0)          # ClickHouse's IPv4-in-IPv6 spelling
    assert not team_a.matches(_cand("10.0.0.2"), T0)              # another team's exporter
    assert not team_a.matches(_cand(""), T0)
    assert _rule(None).matches(_cand("10.0.0.2"), T0)             # global: every exporter
    assert not _rule(frozenset()).matches(_cand("10.0.0.1"), T0)  # an emptied scope never becomes global
    cands = [_cand("10.0.0.1"), _cand("10.0.0.2")]
    det.apply_exceptions(cands, [team_a], T0)
    assert cands[0].exception and cands[1].exception is None
    assert "exporters" not in cands[0].exception                 # finding items never carry the scope list


def test_stored_scopes_are_read_as_canonical_addresses():
    assert det.exception_exporters(None) is None
    assert det.exception_exporters(["10.0.0.1/32", "::ffff:10.0.0.2", "2001:db8::1/128"]) == \
        frozenset({"10.0.0.1", "10.0.0.2", "2001:db8::1"})
    assert det.exception_exporters([]) == frozenset()


def test_existing_rule_positional_construction_still_means_global():
    r = det.ExceptionRule("x", None, None, None, 22, "ssh", None)
    assert r.exporters is None


# ── 1. Exception scope: what a new exception is bound to ─────────────────────

A1, A2, B1 = "10.0.0.1", "10.0.0.2", "10.0.0.9"


def test_a_restricted_creator_is_bound_to_its_visible_exporters():
    manage = ("netflow.view", "netflow.manage")
    assert AN._new_exception_scope(_vis({A2, A1}, manage), None) == [A1, A2]
    assert AN._new_exception_scope(_vis({A1, A2}, manage), []) == [A1, A2]         # empty list = not given
    assert AN._new_exception_scope(_vis({A1, A2}, manage), [A2, A2]) == [A2]       # narrowing is allowed
    with pytest.raises(NetflowError) as exc:
        AN._new_exception_scope(_vis({A1, A2}, manage), [A1, B1])                  # widening is not
    assert exc.value.status_code == 400 and "exporters" in (exc.value.fields or {})
    with pytest.raises(NetflowError):
        AN._new_exception_scope(_vis(set(), manage), None)                         # nothing to bind to
    with pytest.raises(NetflowError):
        AN._new_exception_scope(_vis({A1}, manage), ["10.0.0.0/24"])               # an exporter, not a network
    assert AN._new_exception_scope(_vis(None, manage), None) is None               # unrestricted: global
    assert AN._new_exception_scope(_vis(None, manage), [B1, "::ffff:10.0.0.1"]) == [A1, B1]


def test_the_list_filter_shows_global_and_intersecting_exceptions_only():
    assert AN._exception_visible_where(_vis(None)) == ("TRUE", {})
    assert AN._exception_visible_where(_vis(set())) == ("x.exporter_ips IS NULL", {})
    where, params = AN._exception_visible_where(_vis({A2, A1}))
    assert "x.exporter_ips IS NULL OR x.exporter_ips && CAST(:x_scope AS inet[])" in where
    assert params == {"x_scope": [A1, A2]}
    assert "x.exporter_ips::text[] AS exporter_ips" in AN._exception_select()[0]


def _row(exporter_ips, **kw):
    m = {"id": uuid.uuid4(), "kind": "port_scan", "src_cidr": "172.30.0.0/16", "dst_cidr": None, "dst_port": None,
         "reason": "r", "expires_at": None, "created_by": None, "created_at": T0, "matches": 0,
         "exporter_ips": exporter_ips}
    m.update(kw)
    return SimpleNamespace(_mapping=m)


def test_an_item_names_only_the_callers_exporters_and_says_who_may_change_it():
    manage = ("netflow.view", "netflow.manage")
    shared = _row([f"{A1}/32", f"{B1}/32"])
    item = AN._exception_item(shared, None, _vis({A1, A2}, manage))
    assert item["scope"] == "exporters" and item["exporters"] == [A1] and item["exporters_outside_scope"] is True
    assert B1 not in json.dumps(item, default=str)
    assert item["can_change"] is False                                   # it also suppresses team B's findings
    own = AN._exception_item(_row([f"{A1}/32"]), None, _vis({A1, A2}, manage))
    assert own["can_change"] is True and own["exporters_outside_scope"] is False
    glob = AN._exception_item(_row(None), None, _vis({A1}, manage))
    assert glob["scope"] == "global" and glob["exporters"] is None and glob["can_change"] is False
    assert glob["src_cidr"] == "172.30.0.0/16"                           # policy criteria are not masked
    assert AN._exception_item(_row(None), None, _vis(None, manage))["can_change"] is True
    assert AN._exception_item(_row([f"{A1}/32"]), None, _vis({A1}))["can_change"] is False   # no netflow.manage
    full = AN._exception_item(shared)                                    # audit log: everything
    assert full["exporters"] == [A1, B1]


# ── 1. migrate-140 ───────────────────────────────────────────────────────────

def test_migration_140_is_replay_safe_and_locked():
    sys.path.insert(0, str(SCRIPTS))
    import migration_order

    sql = MIGRATION.read_text()
    assert migration_order.engine_of(MIGRATION.name) == "postgres"
    assert migration_order.is_replay_safe(sql) and not migration_order.writes_rows(sql)
    assert "netflow_detector_exceptions.exporter_ips" in migration_order.added_columns(sql)
    lock = dict(line.split()[::-1] for line in (SCRIPTS / "migrations.lock").read_text().splitlines()
                if line.strip() and not line.startswith("#"))
    assert lock[MIGRATION.name] == hashlib.sha256(MIGRATION.read_bytes()).hexdigest()


PG_ADMIN = os.environ.get("ZENPLUS_NETFLOW_TEST_PG_ADMIN")


def _psql(url: str, *args: str, sql: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    out = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", url, *args],
                         input=sql, capture_output=True, text=True, timeout=120)
    if check:
        assert out.returncode == 0, out.stderr
    return out


def _schema(url: str) -> str:
    out = subprocess.run(["pg_dump", "-s", "--no-owner", url], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return "\n".join(l for l in out.stdout.splitlines() if not l.startswith(("\\restrict", "\\unrestrict")))


@pytest.mark.skipif(not PG_ADMIN, reason="set ZENPLUS_NETFLOW_TEST_PG_ADMIN to replay the migration")
def test_migration_140_replays_to_an_identical_schema_and_keeps_old_rows_global():
    db = f"nf_mig140_{os.getpid()}"
    target = urlparse(PG_ADMIN)._replace(path=f"/{db}").geturl()
    _psql(PG_ADMIN, "-c", f"DROP DATABASE IF EXISTS {db}")
    _psql(PG_ADMIN, "-c", f"CREATE DATABASE {db}")
    try:
        _psql(target, sql="CREATE TABLE users (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), username TEXT);"
                          "CREATE TABLE roles (name VARCHAR(50) PRIMARY KEY, permissions JSONB NOT NULL DEFAULT '[]',"
                          " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());")
        _psql(target, "-f", str(SCRIPTS / "migrate-052-netflow-saved-views.sql"))
        _psql(target, "-f", str(SCRIPTS / "migrate-138-netflow-governance.sql"))
        _psql(target, sql="INSERT INTO netflow_detector_exceptions (kind, src_cidr, reason) "
                          "VALUES ('port_scan', '10.0.0.0/8', 'before 140')")
        _psql(target, "-f", str(MIGRATION))
        first = _schema(target)
        _psql(target, "-f", str(MIGRATION))
        assert _schema(target) == first
        assert "exporter_ips inet[]" in first
        assert _psql(target, "-At", "-c", "SELECT exporter_ips IS NULL FROM netflow_detector_exceptions").stdout.strip() == "t"
        for bad in ("'{}'::inet[]", "ARRAY[NULL]::inet[]"):
            r = _psql(target, "-c", f"INSERT INTO netflow_detector_exceptions (src_cidr, reason, exporter_ips) "
                                    f"VALUES ('10.0.0.1', 'x', {bad})", check=False)
            assert r.returncode != 0 and "exporter_ips_check" in r.stderr
    finally:
        _psql(PG_ADMIN, "-c", f"DROP DATABASE IF EXISTS {db}")


PG_DSN = os.environ.get("ZENPLUS_NETFLOW_TEST_PG")


@pytest.mark.skipif(not PG_DSN, reason="set ZENPLUS_NETFLOW_TEST_PG (scratch DB with migrate-138 and 140)")
def test_applying_a_scoped_exception_leaves_other_exporters_findings_open_and_loads_back():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    tag = uuid.uuid4().hex[:8]
    ea, eb = "10.253.141.1", "10.253.141.2"
    src = "172.30.141.10"
    fps = [f"nfpol-{tag}-a", f"nfpol-{tag}-b"]
    xid = uuid.uuid4()

    async def run():
        engine = create_async_engine(PG_DSN)
        try:
            async with AsyncSession(engine) as db:
                for fp, exp in zip(fps, (ea, eb)):
                    await db.execute(text(
                        "INSERT INTO netflow_findings (fingerprint, kind, severity, confidence, status, title, src_ip, "
                        "exporter_ip, first_seen, last_seen) VALUES (:fp, 'port_scan', 'warning', 'medium', 'open', "
                        "'nfpol', CAST(:src AS inet), CAST(:e AS inet), NOW(), NOW())"), {"fp": fp, "src": src, "e": exp})
                await db.execute(text(
                    "INSERT INTO netflow_detector_exceptions (id, kind, src_cidr, reason, exporter_ips) VALUES "
                    "(:id, 'port_scan', '172.30.141.0/24', 'nfpol scoped', CAST(:x AS inet[]))"), {"id": xid, "x": [ea]})
                await db.commit()
                rules = {r.id: r for r in await det.load_exceptions(db)}
                rule = rules[str(xid)]
                assert rule.exporters == frozenset({ea})
                assert await det.suppress_matching(db, rule, datetime.now(timezone.utc)) == 1
                await db.commit()
                got = dict((await db.execute(text(
                    "SELECT fingerprint, status FROM netflow_findings WHERE fingerprint = ANY(:f)"), {"f": fps})).all())
                assert got == {fps[0]: "suppressed", fps[1]: "open"}
        finally:
            async with AsyncSession(engine) as db:
                await db.execute(text("DELETE FROM netflow_findings WHERE fingerprint = ANY(:f)"), {"f": fps})
                await db.execute(text("DELETE FROM netflow_detector_exceptions WHERE id = :id"), {"id": xid})
                await db.commit()
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.skipif(not PG_DSN, reason="set ZENPLUS_NETFLOW_TEST_PG (scratch DB with migrate-138)")
def test_the_threat_intel_refresh_loop_finds_due_feeds():
    """Found while checking the API log during this pass: every iteration of
    netflow_ti_loop failed with "operator does not exist: timestamp with time
    zone < interval" (asyncpg typed the bare ``:now`` in ``:now -
    make_interval(...)`` as an interval), so no URL feed ever refreshed itself.
    The query runs here through the same driver."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.services import netflow_ti as ti

    now = datetime.now(timezone.utc)
    tag = uuid.uuid4().hex[:8]
    feeds = {  # name -> (last_refreshed_at, refresh_hours, last_status, updated_at, due)
        "never": (None, 24, None, now, True),
        "stale": (now - timedelta(hours=30), 24, "OK", now - timedelta(hours=30), True),
        "fresh": (now - timedelta(hours=2), 24, "OK", now - timedelta(hours=2), False),
        "failed-recently": (now - timedelta(hours=30), 24, "Download failed (HTTP 503)", now - timedelta(minutes=10), False),
        "failed-long-ago": (now - timedelta(hours=30), 24, "Download failed (HTTP 503)", now - timedelta(hours=2), True),
    }

    async def run():
        engine = create_async_engine(PG_DSN)
        try:
            async with AsyncSession(engine) as db:
                ids = {}
                for name, (last, hours, status, updated, _due) in feeds.items():
                    ids[name] = (await db.execute(text(
                        "INSERT INTO netflow_ti_feeds (name, source, url, enabled, refresh_hours, last_refreshed_at, "
                        "last_status, updated_at) VALUES (:n, 'url', 'https://example.test/feed.txt', TRUE, :h, :l, :s, :u) "
                        "RETURNING id"), {"n": f"nfpol {tag} {name}", "h": hours, "l": last, "s": status, "u": updated})).scalar()
                await db.commit()
                try:
                    due = {r[0] for r in (await db.execute(text(ti.DUE_FEEDS_SQL), {"now": now})).all()}
                finally:
                    await db.execute(text("DELETE FROM netflow_ti_feeds WHERE name LIKE :p"), {"p": f"nfpol {tag} %"})
                    await db.commit()
                assert {n for n, i in ids.items() if i in due} == {n for n, f in feeds.items() if f[4]}
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── 2. Share tokens ──────────────────────────────────────────────────────────

def test_share_links_go_to_the_owner_and_report_managers_only(monkeypatch):
    perms = {"viewer": ["reports.view"], "manager": ["reports.view", "reports.manage"], "admin": ["system.admin"]}

    async def role_perms(db, role):
        return perms[role]

    monkeypatch.setattr(RS, "get_role_permissions", role_perms)
    from app.core import scoping
    monkeypatch.setattr(scoping, "get_role_permissions", role_perms)

    async def no_role_scope(db, role):
        return []
    monkeypatch.setattr(scoping, "get_role_scope_tags", no_role_scope)
    owner = uuid.uuid4()

    def sees(role, uid, owner_id, scope=None):
        return asyncio.run(RS.sees_share_links(None, SimpleNamespace(role=role, id=uid, scope_tags=scope or []),
                                               owner_id))

    assert sees("viewer", owner, owner) is True                   # the schedule's owner
    assert sees("viewer", uuid.uuid4(), owner) is False           # anyone else who may view reports
    assert sees("viewer", uuid.uuid4(), None) is False            # no owner recorded
    assert sees("manager", uuid.uuid4(), owner) is True
    assert sees("admin", uuid.uuid4(), None) is True
    # A tag-scoped reports.manage holder cannot manage schedules (_refuse_scoped), so it is not a report
    # manager: it gets no link to a fleet-wide report it does not own, but keeps its own schedules' links.
    assert sees("manager", uuid.uuid4(), owner, scope=["team-a"]) is False
    assert sees("manager", owner, owner, scope=["team-a"]) is True
    assert sees("admin", uuid.uuid4(), None, scope=["team-a"]) is True      # system.admin is never scoped


def test_run_listings_need_the_reports_view_permission():
    from app.api.v1 import report_runs

    for router, path in ((RS.router, "/report-schedules/{schedule_id}/runs"), (report_runs.router, "/reports/runs")):
        route = next(r for r in router.routes if r.path == path)
        names = [getattr(d.call, "__qualname__", "") for d in route.dependant.dependencies]
        assert any("require_permission" in n for n in names), (path, names)


# ── 3. Alert messages in reports ─────────────────────────────────────────────

NF_MSG = "Flow traffic for exporter 198.18.240.2 is 40.4 Mbps; top talker 172.30.92.10 -> 8.8.8.8:443"


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return [SimpleNamespace(_mapping=r) for r in self._rows]


class _FakeDb:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    async def execute(self, stmt, params=None):
        self.sql.append(str(stmt))
        return _Rows(self.rows)


def _alert_rows():
    return [
        {"id": 1, "device_id": None, "service_check_id": None, "status": "active", "severity": "warning",
         "message": NF_MSG, "triggered_at": T0, "acknowledged_at": None, "resolved_at": None, "hostname": "edge",
         "metadata": {"source": "netflow"}},
        {"id": 2, "device_id": None, "service_check_id": None, "status": "active", "severity": "critical",
         "message": "CPU 99% on core-1", "triggered_at": T0, "acknowledged_at": None, "resolved_at": None,
         "hostname": "core-1", "metadata": {"source": None}},
    ]


def test_report_alert_rows_withhold_netflow_messages_like_the_alert_center():
    db = _FakeDb(_alert_rows())
    rows = asyncio.run(report_service._fetch_alerts(db, T0, T0, redact_netflow=True))
    assert "jsonb_build_object('source', a.metadata->'source') AS metadata" in db.sql[0]
    assert rows[0]["message"] == nfa.REDACTED_MESSAGE and "172.30.92.10" not in json.dumps(rows, default=str)
    assert rows[1]["message"] == "CPU 99% on core-1"
    assert rows[0] == nfa.redact_alert_payload(_alert_rows()[0])           # the Alert Center's helper
    plain = asyncio.run(report_service._fetch_alerts(_FakeDb(_alert_rows()), T0, T0))
    assert plain[0]["message"] == NF_MSG


def test_noisy_sources_use_a_redacted_dataset_for_requesters_without_netflow_view(monkeypatch):
    from app.services.netflow_report import ORPHANED_OWNER

    seen = []

    async def build_technical(db, frm, to, visible_tags=None, *, redact_netflow=False):
        seen.append(redact_netflow)
        msg = nfa.REDACTED_MESSAGE if redact_netflow else NF_MSG
        return {"noisy_alerts": [{"hostname": "edge", "sample_message": msg, "severity": "warning", "alert_count": 3}]}

    monkeypatch.setattr(RSEC, "build_technical", build_technical)

    def section(requester):
        ctx = RSEC.SectionCtx(None, T0, T0, None, requester=requester)
        return asyncio.run(RSEC._sec_noisy_sources(ctx))

    out = section(ORPHANED_OWNER)                          # no NetFlow visibility at all
    assert seen[-1] is True and "172.30.92.10" not in json.dumps(out)
    assert out["tables"][0]["rows"][0][1] == nfa.REDACTED_MESSAGE[:60]
    out = section(None)                                    # the appliance itself
    assert seen[-1] is False and out["tables"][0]["rows"][0][1] == NF_MSG[:60]


def test_withholding_follows_netflow_view(monkeypatch):
    async def role_perms(db, role):
        return {"nf": ["netflow.view"], "plain": ["alerts.view", "reports.view"], "admin": ["system.admin"]}[role]

    monkeypatch.setattr("app.core.security.get_role_permissions", role_perms)
    withheld = lambda role: asyncio.run(nfa.netflow_details_withheld(None, SimpleNamespace(role=role)))  # noqa: E731
    assert withheld("plain") is True and withheld("nf") is False and withheld("admin") is False
    assert asyncio.run(nfa.netflow_details_withheld(None, None)) is False


# ── HTTP end to end ──────────────────────────────────────────────────────────

E2E_URL = os.environ.get("ZENPLUS_NETFLOW_E2E_URL")
E2E_PG = os.environ.get("ZENPLUS_NETFLOW_E2E_PG")
needs_e2e = pytest.mark.skipif(not (E2E_URL and E2E_PG),
                               reason="set ZENPLUS_NETFLOW_E2E_URL and ZENPLUS_NETFLOW_E2E_PG")

PASS = "NfPol-Pass-2026!"
TEAM_A, TEAM_B = "nfpol-team-a", "nfpol-team-b"
EXP_A, EXP_B = "198.18.240.1", "198.18.240.2"
MGMT_A, MGMT_B = "198.18.241.1", "198.18.241.2"
DEV_A, DEV_B = uuid.UUID("9f010000-0000-4000-8000-0000000000a1"), uuid.UUID("9f010000-0000-4000-8000-0000000000b1")
FIND_A, FIND_B = uuid.UUID("9f010000-0000-4000-8000-0000000f0a01"), uuid.UUID("9f010000-0000-4000-8000-0000000f0b01")
SCHED_OWNED, SCHED_ADMIN = uuid.UUID("9f010000-0000-4000-8000-00000005c0a1"), uuid.UUID("9f010000-0000-4000-8000-00000005c0b1")
ALERTS = [uuid.UUID(f"9f010000-0000-4000-8000-0000000a1e{i:02d}") for i in range(4)]
SECRET = "172.30.92.10"
ROLES = {
    "nfpol_no_netflow": ["dashboard.view", "devices.view", "alerts.view", "reports.view", "reports.export"],
    "nfpol_no_reports": ["dashboard.view", "netflow.view"],
    "nfpol_reporter": ["dashboard.view", "netflow.view", "reports.view"],
}
USERS = {
    "nfpol_op_a": ("operator", [TEAM_A]), "nfpol_op_b": ("operator", [TEAM_B]),
    "nfpol_mgr": ("operator", []),                     # unscoped report manager (reports.manage)
    "nfpol_view_a": ("viewer", [TEAM_A]), "nfpol_view_b": ("viewer", [TEAM_B]),
    "nfpol_view_none": ("viewer", ["nfpol-nomatch"]),
    "nfpol_nonf": ("nfpol_no_netflow", []), "nfpol_noreports": ("nfpol_no_reports", []),
    "nfpol_owner": ("nfpol_reporter", []), "nfpol_reporter2": ("nfpol_reporter", []),
}
TOKENS = {SCHED_OWNED: f"nfpol-owned-{uuid.uuid4().hex}", SCHED_ADMIN: f"nfpol-admin-{uuid.uuid4().hex}"}


def _pg(statements):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run():
        engine = create_async_engine(E2E_PG)
        out = []
        try:
            async with engine.begin() as conn:
                for sql, params in statements:
                    res = await conn.execute(text(sql), params)
                    out.append(res.fetchall() if res.returns_rows else None)
        finally:
            await engine.dispose()
        return out

    return asyncio.run(run())


def _cleanup():
    names = list(USERS)
    return [
        ("DELETE FROM report_runs WHERE title LIKE 'nfpol %'", {}),
        ("DELETE FROM report_schedules WHERE name LIKE 'nfpol %'", {}),
        ("DELETE FROM alerts WHERE id = ANY(:a)", {"a": ALERTS}),
        ("DELETE FROM netflow_findings WHERE id = ANY(:f)", {"f": [FIND_A, FIND_B]}),
        ("DELETE FROM netflow_detector_exceptions WHERE reason LIKE 'nfpol %'", {}),
        ("DELETE FROM netflow_exporter_devices WHERE host(exporter_ip) = ANY(:e)", {"e": [EXP_A, EXP_B]}),
        ("DELETE FROM devices WHERE id = ANY(:d) OR hostname LIKE 'nfpol-%'", {"d": [DEV_A, DEV_B]}),
        ("DELETE FROM audit_logs WHERE actor_id IN (SELECT id FROM users WHERE username = ANY(:u))", {"u": names}),
        ("DELETE FROM users WHERE username = ANY(:u)", {"u": names}),
        ("DELETE FROM roles WHERE name = ANY(:r)", {"r": list(ROLES)}),
    ]


@pytest.fixture(scope="module")
def world():
    import requests

    from app.core.security import hash_password

    _pg(_cleanup())
    now = datetime.now(timezone.utc)
    nf_meta = {"source": "netflow", "rule_id": "nfpol-rule", "metric": "netflow.traffic_bps",
               "exporter": EXP_B, "snapshot": {"talkers": [{"src": SECRET}]}}
    _pg([
        *[("INSERT INTO roles (name, display_name, permissions) VALUES (:n, :n, CAST(:p AS jsonb))",
           {"n": n, "p": json.dumps(p)}) for n, p in ROLES.items()],
        *[("INSERT INTO users (username, email, password_hash, role, scope_tags) VALUES (:u, :m, :h, :r, CAST(:s AS jsonb))",
           {"u": u, "m": f"{u}@example.test", "h": hash_password(PASS), "r": r, "s": json.dumps(s)})
          for u, (r, s) in USERS.items()],
        ("INSERT INTO devices (id, hostname, ip_address, tags) VALUES "
         "(:a, 'nfpol-dev-a', CAST(:ia AS inet), CAST(:ta AS jsonb)), (:b, 'nfpol-dev-b', CAST(:ib AS inet), CAST(:tb AS jsonb))",
         {"a": DEV_A, "b": DEV_B, "ia": MGMT_A, "ib": MGMT_B, "ta": json.dumps([TEAM_A]), "tb": json.dumps([TEAM_B])}),
        ("INSERT INTO netflow_exporter_devices (exporter_ip, device_id) VALUES (CAST(:ea AS inet), :a), (CAST(:eb AS inet), :b)",
         {"ea": EXP_A, "eb": EXP_B, "a": DEV_A, "b": DEV_B}),
        *[("INSERT INTO netflow_findings (id, fingerprint, kind, severity, confidence, status, title, description, src_ip, "
           "exporter_ip, first_seen, last_seen) VALUES (:id, :fp, 'port_scan', 'warning', 'medium', 'open', "
           "'nfpol scan', 'nfpol', CAST(:src AS inet), CAST(:e AS inet), :t, :t)",
           {"id": fid, "fp": f"nfpol-{fid.hex}", "src": src, "e": exp, "t": now - timedelta(minutes=10)})
          for fid, src, exp in ((FIND_A, "172.30.91.10", EXP_A), (FIND_B, SECRET, EXP_B))],
        # Three NetFlow alerts with the same opening words (one noisy source) and a plain one.
        *[("INSERT INTO alerts (id, device_id, status, severity, message, triggered_at, metadata) VALUES "
           "(:id, :d, 'active', 'warning', :m, :t, CAST(:meta AS jsonb))",
           {"id": aid, "d": DEV_B, "m": f"Flow traffic for exporter {EXP_B} is 40.4 Mbps; top talker {SECRET} #{i}",
            "t": now - timedelta(minutes=5 + i), "meta": json.dumps(nf_meta)}) for i, aid in enumerate(ALERTS[:3])],
        ("INSERT INTO alerts (id, device_id, status, severity, message, triggered_at, metadata) VALUES "
         "(:id, :d, 'active', 'critical', 'nfpol plain alert: CPU 99% on nfpol-dev-b', :t, '{}'::jsonb)",
         {"id": ALERTS[3], "d": DEV_B, "t": now - timedelta(minutes=4)}),
        ("INSERT INTO report_schedules (id, name, enabled, report_type, period, format, created_by) VALUES "
         "(:a, 'nfpol owned', FALSE, 'alerts', 'last_24h', 'none', (SELECT id FROM users WHERE username = 'nfpol_owner')), "
         "(:b, 'nfpol admin', FALSE, 'alerts', 'last_24h', 'none', (SELECT id FROM users WHERE username = 'admin'))",
         {"a": SCHED_OWNED, "b": SCHED_ADMIN}),
        *[("INSERT INTO report_runs (schedule_id, report_type, period, title, token, html, status) VALUES "
           "(:s, 'alerts', 'last_24h', :title, :tok, '<html><body>nfpol report</body></html>', 'success')",
           {"s": sid, "title": f"nfpol run {sid.hex[-4:]}", "tok": tok}) for sid, tok in TOKENS.items()],
    ])
    time.sleep(access.CACHE_TTL_S + 1)            # the API's ownership cache sees the direct-PG seed
    tokens = {}
    for name in ["admin", *USERS]:
        pw = "admin123" if name == "admin" else PASS
        for _ in range(6):
            r = requests.post(f"{E2E_URL}/api/v1/auth/login", json={"username": name, "password": pw}, timeout=10)
            if r.status_code != 429:
                break
            time.sleep(10)
        r.raise_for_status()
        tokens[name] = r.json()["access_token"]
    yield tokens
    _pg(_cleanup())


def _call(tokens, user, method, path, **kw):
    import requests

    headers = {"Authorization": f"Bearer {tokens[user]}"} if user else {}
    r = requests.request(method, f"{E2E_URL}/api/v1{path}", headers=headers, timeout=120, **kw)
    try:
        body = r.json()
    except ValueError:
        body = r.content.decode("utf-8", "replace")      # text/csv carries no charset
    return r.status_code, body, r


def _finding_status() -> dict:
    rows = _pg([("SELECT id, status FROM netflow_findings WHERE id = ANY(:f)", {"f": [FIND_A, FIND_B]})])[0]
    return {r[0]: r[1] for r in rows}


def _stored_scope(xid) -> set[str] | None:
    v = _pg([("SELECT exporter_ips::text[] FROM netflow_detector_exceptions WHERE id = CAST(:x AS uuid)", {"x": xid})])[0][0][0]
    return None if v is None else {e.split("/")[0] for e in v}


def _listed(tokens, user) -> dict:
    status, body, _r = _call(tokens, user, "GET", "/netflow/detector-exceptions")
    assert status == 200, body
    return {i["id"]: i for i in body["items"]}


@needs_e2e
def test_e2e_a_scoped_managers_exception_stays_on_its_teams_exporters(world):
    status, x, _r = _call(world, "nfpol_op_a", "POST", "/netflow/detector-exceptions",
                          json={"kind": "port_scan", "src_cidr": "172.30.0.0/16", "reason": "nfpol team-a scanner"})
    assert status == 201, x
    assert x["scope"] == "exporters" and set(x["exporters"]) == {EXP_A, MGMT_A} and x["can_change"] is True
    assert _stored_scope(x["id"]) == {EXP_A, MGMT_A}
    assert x["suppressed_findings"] == 1
    assert _finding_status() == {FIND_A: "suppressed", FIND_B: "open"}        # team B's finding untouched
    # Naming another team's exporter, or a network, is refused.
    for exporters in ([EXP_B], [EXP_A, EXP_B], ["198.18.240.0/24"]):
        status, body, _r = _call(world, "nfpol_op_a", "POST", "/netflow/detector-exceptions",
                                 json={"src_cidr": "172.30.0.0/16", "reason": "nfpol widen", "exporters": exporters})
        assert status == 400 and "exporters" in json.dumps(body), (exporters, body)
    # Reading: team A sees it, team B and an empty scope do not, not even its criteria.
    assert x["id"] in _listed(world, "nfpol_view_a")
    for user in ("nfpol_view_b", "nfpol_view_none", "nfpol_op_b"):
        listed = _listed(world, user)
        assert x["id"] not in listed and "nfpol team-a scanner" not in json.dumps(listed), user
    assert set(_listed(world, "admin")[x["id"]]["exporters"]) == {EXP_A, MGMT_A}
    # Another team's manager cannot touch it (out of sight: 404).
    assert _call(world, "nfpol_op_b", "PATCH", f"/netflow/detector-exceptions/{x['id']}", json={"reason": "nfpol x"})[0] == 404
    assert _call(world, "nfpol_op_b", "DELETE", f"/netflow/detector-exceptions/{x['id']}")[0] == 404
    # A PATCH never widens the scope (exporters are not a PATCH field).
    status, body, _r = _call(world, "nfpol_op_a", "PATCH", f"/netflow/detector-exceptions/{x['id']}",
                             json={"reason": "nfpol team-a scanner v2", "exporters": [EXP_B]})
    assert status == 200 and body["reason"] == "nfpol team-a scanner v2", body
    assert _stored_scope(x["id"]) == {EXP_A, MGMT_A}
    # The engine reads the scope back: a team-B match is not suppressed by it.
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    async def rules():
        engine = create_async_engine(E2E_PG)
        try:
            async with AsyncSession(engine) as db:
                return {r.id: r for r in await det.load_exceptions(db)}
        finally:
            await engine.dispose()

    rule = asyncio.run(rules())[x["id"]]
    assert rule.matches(_cand(EXP_A, "172.30.91.10"), datetime.now(timezone.utc))
    assert not rule.matches(_cand(EXP_B, SECRET), datetime.now(timezone.utc))
    # Its owner removes it: the suppressed finding reopens.
    status, body, _r = _call(world, "nfpol_op_a", "DELETE", f"/netflow/detector-exceptions/{x['id']}")
    assert status == 200 and body["reopened_findings"] == 1
    assert _finding_status() == {FIND_A: "open", FIND_B: "open"}


@needs_e2e
def test_e2e_global_and_shared_exceptions_are_visible_but_only_an_administrator_changes_them(world):
    status, glob, _r = _call(world, "admin", "POST", "/netflow/detector-exceptions",
                             json={"dst_port": 4444, "reason": "nfpol global policy", "apply_to_existing": False})
    assert status == 201 and glob["scope"] == "global" and glob["exporters"] is None, glob
    status, both, _r = _call(world, "admin", "POST", "/netflow/detector-exceptions",
                             json={"src_cidr": "172.30.0.0/16", "reason": "nfpol a+b", "exporters": [EXP_A, EXP_B],
                                   "apply_to_existing": False})
    assert status == 201 and both["exporters"] == [EXP_A, EXP_B], both
    try:
        # A global exception is policy over everyone's exporters: listed, criteria shown, read only.
        seen = _listed(world, "nfpol_view_b")[glob["id"]]
        assert seen["dst_port"] == 4444 and seen["scope"] == "global"
        listed_a = _listed(world, "nfpol_op_a")
        assert listed_a[glob["id"]]["can_change"] is False
        # A shared one: team A sees its own exporter and that others exist, never which.
        shared = listed_a[both["id"]]
        assert shared["exporters"] == [EXP_A] and shared["exporters_outside_scope"] is True
        assert shared["can_change"] is False
        assert EXP_B not in json.dumps(listed_a) and MGMT_B not in json.dumps(listed_a)
        assert _listed(world, "nfpol_view_none").get(both["id"]) is None
        for xid in (glob["id"], both["id"]):
            for user in ("nfpol_op_a", "nfpol_op_b"):
                assert _call(world, user, "PATCH", f"/netflow/detector-exceptions/{xid}", json={"reason": "nfpol x"})[0] == 403
                assert _call(world, user, "DELETE", f"/netflow/detector-exceptions/{xid}")[0] == 403
        # The viewer roles have no netflow.manage at all.
        assert _call(world, "nfpol_view_a", "DELETE", f"/netflow/detector-exceptions/{both['id']}")[0] == 403
    finally:
        for xid in (glob["id"], both["id"]):
            assert _call(world, "admin", "DELETE", f"/netflow/detector-exceptions/{xid}")[0] == 200


@needs_e2e
def test_e2e_report_run_tokens_go_to_the_owner_and_report_managers_only(world):
    # nfpol_op_a is a tag-scoped operator: it holds reports.manage (released operator role) but, being
    # limited to a visibility scope, cannot manage schedules and so is not a report manager here.
    owned = f"/report-schedules/{SCHED_OWNED}/runs"
    for user in ("nfpol_noreports",):
        assert _call(world, user, "GET", owned)[0] == 403
        assert _call(world, user, "GET", "/reports/runs?limit=100")[0] == 403
    for user, sees in (("nfpol_owner", True), ("admin", True), ("nfpol_mgr", True), ("nfpol_reporter2", False),
                       ("nfpol_op_a", False),
                       ("nfpol_nonf", False)):
        status, body, _r = _call(world, user, "GET", owned)
        assert status == 200, (user, body)
        assert body["can_view_links"] is sees and len(body["data"]) == 1, user
        run = body["data"][0]
        assert (run["token"] == TOKENS[SCHED_OWNED]) is sees and run["token_withheld"] is (not sees), user
        if not sees:
            assert TOKENS[SCHED_OWNED] not in json.dumps(body)
        status, runs, _r = _call(world, user, "GET", "/reports/runs?limit=100")
        assert status == 200
        mine = {r["title"]: r for r in runs if r["title"].startswith("nfpol run")}
        assert len(mine) == 2, user
        blob = json.dumps(runs)
        assert (TOKENS[SCHED_OWNED] in blob) is sees, user
        assert (TOKENS[SCHED_ADMIN] in blob) is (user in ("admin", "nfpol_mgr")), user      # only its owner / managers
    # The owner of one schedule gets no link to another's runs.
    status, body, _r = _call(world, "nfpol_owner", "GET", f"/report-schedules/{SCHED_ADMIN}/runs")
    assert status == 200 and body["data"][0]["token"] is None
    assert _call(world, "admin", "GET", f"/report-schedules/{uuid.uuid4()}/runs")[0] == 404
    # The link itself still opens without signing in, out of caches, referrers and indexes.
    status, html, r = _call(world, None, "GET", f"/reports/shared/{TOKENS[SCHED_OWNED]}")
    assert status == 200 and "nfpol report" in html
    assert r.headers["Referrer-Policy"] == "no-referrer" and "no-store" in r.headers["Cache-Control"]
    assert "noindex" in r.headers["X-Robots-Tag"]
    assert _call(world, None, "GET", f"/reports/shared/nfpol-{uuid.uuid4().hex}")[0] == 404


@needs_e2e
def test_e2e_report_alert_messages_are_withheld_without_netflow_view(world):
    status, tech, _r = _call(world, "nfpol_nonf", "GET", "/reports/data/technical")
    assert status == 200, tech
    blob = json.dumps(tech)
    assert SECRET not in blob and EXP_B not in blob
    samples = [n["sample_message"] for n in tech["noisy_alerts"]]
    assert nfa.REDACTED_MESSAGE in samples and "nfpol plain alert: CPU 99% on nfpol-dev-b" in samples
    status, tech_admin, _r = _call(world, "admin", "GET", "/reports/data/technical")
    assert status == 200 and SECRET in json.dumps(tech_admin)
    # The section report (noisy sources + recent alerts).
    status, rep, _r = _call(world, "nfpol_nonf", "GET", "/reports/render/alerts?format=json&hours=1")
    assert status == 200, rep
    secs = {s["id"]: s for s in rep["sections"]}
    for sec in ("noisy_sources", "recent_alerts"):
        assert SECRET not in json.dumps(secs[sec]) and EXP_B not in json.dumps(secs[sec]), sec
    assert nfa.REDACTED_MESSAGE[:60] in json.dumps(secs["noisy_sources"], ensure_ascii=False)
    status, rep_admin, _r = _call(world, "admin", "GET", "/reports/render/alerts?format=json&hours=1")
    # (the table cell holds the first 60 characters: the exporter, not yet the talker)
    assert EXP_B in json.dumps({s["id"]: s for s in rep_admin["sections"]}["noisy_sources"])
    # The legacy export (CSV alert table).
    body = {"report_type": "alert_analysis", "period": "last_24h", "format": "csv"}
    status, csv_text, _r = _call(world, "nfpol_nonf", "POST", "/reports/generate", json=body)
    assert status == 200 and "nfpol plain alert" in csv_text
    assert SECRET not in csv_text and nfa.REDACTED_MESSAGE in csv_text
    status, csv_admin, _r = _call(world, "admin", "POST", "/reports/generate", json=body)
    assert status == 200 and SECRET in csv_admin


@needs_e2e
def test_e2e_scheduled_legacy_attachments_follow_the_owners_netflow_permission(world, monkeypatch):
    """A legacy schedule's attachment (PDF/Excel/CSV alert tables) withholds
    NetFlow alert messages unless the schedule's owner has netflow.view."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.api.v1.report_schedules import _row_to_dict
    from app.services import report_scheduler as RSCH

    running = {"owner": None}
    seen: dict[str, bool] = {}

    async def fake_csv(**kw):
        seen[running["owner"]] = kw["redact_netflow"]
        return b"ok"

    async def fake_executive(db, start, end, visible_tags=None):
        return {"kpis": {}}

    # Only the attachment wiring is under test: no ClickHouse, no mail.
    monkeypatch.setattr(RSCH, "generate_csv_report", fake_csv)
    monkeypatch.setattr(RSCH, "build_executive", fake_executive)
    monkeypatch.setattr(RSCH.report_html, "build_report_html", lambda meta, data: "<html>nfpol</html>")
    monkeypatch.setattr(RSCH.report_html, "build_report_email_html", lambda *a, **k: "")
    monkeypatch.setattr(RSCH.report_html, "build_report_email_text", lambda *a, **k: "")
    owners = {"nfpol_nonf": True, "admin": False, "nfpol_owner": False}     # owner -> withheld
    ids = {u: uuid.uuid4() for u in owners}
    _pg([("INSERT INTO report_schedules (id, name, enabled, report_type, period, format, created_by) VALUES "
          "(:id, :n, FALSE, 'alert_analysis', 'last_24h', 'csv', (SELECT id FROM users WHERE username = :u))",
          {"id": ids[u], "n": f"nfpol legacy {u}", "u": u}) for u in owners])

    async def run_all():
        engine = create_async_engine(E2E_PG)
        try:
            async with AsyncSession(engine) as db:
                for user, sid in ids.items():
                    row = (await db.execute(text("SELECT * FROM report_schedules WHERE id = :id"), {"id": sid})).first()
                    running["owner"] = user
                    result = await RSCH.generate_and_deliver(db, _row_to_dict(row), triggered_by="manual")
                    assert result["status"] == "success", result
        finally:
            await engine.dispose()

    asyncio.run(run_all())
    assert seen == owners
