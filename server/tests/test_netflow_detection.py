"""NetFlow detection engine, findings API and lifecycle (WP B5).

Covers NF-03 (no all-clear unless every detector ran with data), NF-07 (SYN/RST
detectors unavailable when flags are not exported; explicit zero flags stay
available), NF-09 (frozen evidence window + detector filters), NF-21 (ICMP by
sustained per-minute rate, not window length), NF-22 (dominant sender against
the exporter's whole traffic), X-31 (vertical port scans), X-32 (sensitive
egress: special-use ranges, ACK/minimum-bytes evidence), X-34 (honest match
counts), X-36 (volume guard), CR-11/FE-06 (persistence, lifecycle, exceptions).

Three tiers:

* pure unit tests (always);
* ClickHouse fixtures in a disposable database (``NETFLOW_TEST_CLICKHOUSE``);
* PostgreSQL persistence (``ZENPLUS_NETFLOW_TEST_PG``, a scratch database with
  migrate-138) and the live API of this tree (``NETFLOW_TEST_API`` +
  ``NETFLOW_TEST_TOKEN``, optional ``NETFLOW_TEST_TOKEN_RO`` for a
  ``netflow.view``-only user and ``NETFLOW_TEST_TOKEN_VIEWER`` for a user with
  ``alerts.acknowledge`` but not ``netflow.manage``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.api.v1.netflow import access, common as C  # noqa: E402
from app.api.v1.netflow.access import FlowVisibility  # noqa: E402
from app.api.v1.netflow.filters import FlowFilter  # noqa: E402
from app.services import netflow_detection as det  # noqa: E402
from app.services import netflow_ti as ti  # noqa: E402

UTC = timezone.utc


def T(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


# ── Settings registry ────────────────────────────────────────────────────────

def test_registry_has_no_phantom_detectors():
    # X-33: the security dashboard advertised exfil/beacon checks that never existed.
    assert set(det.KINDS) == {"syn_scan", "port_scan", "rst_flood", "sensitive_egress", "icmp_flood",
                              "icmp_flood_target", "icmp_sweep", "dominant_sender", "volume_spike", "threat_intel"}
    for spec in det.DETECTORS:
        assert spec.method and spec.description and spec.label
        assert set(spec.required) <= {"tcp_flags", "icmp_type", "baseline", "ti_feeds"}
    assert det.DETECTOR_BY_KIND["syn_scan"].required == ("tcp_flags",)
    assert det.DETECTOR_BY_KIND["rst_flood"].required == ("tcp_flags",)


def test_settings_merge_defaults_and_strict_validation():
    s = det.merge_settings(None)
    assert s["quiet_minutes"] == 60 and s["detectors"]["syn_scan"]["min_destinations"] == 100
    # Stored garbage falls back to defaults (detection never breaks on a bad row) ...
    lenient = det.merge_settings({"detectors": {"syn_scan": {"min_destinations": "lots"}, "nope": {}}})
    assert lenient["detectors"]["syn_scan"]["min_destinations"] == 100
    # ... while an API write is strict and names every field.
    with pytest.raises(det.SettingsError) as exc:
        det.merge_settings({"quiet_minutes": 5, "detectors": {
            "syn_scan": {"min_destinations": 5, "severity": "high"},
            "sensitive_egress": {"ports": [22, 70000]},
            "volume_spike": {"enabled": "yes"},
            "exfil": {},
        }}, strict=True)
    fields = exc.value.fields
    assert {"quiet_minutes", "detectors.syn_scan.min_destinations", "detectors.syn_scan.severity",
            "detectors.sensitive_egress.ports", "detectors.volume_spike.enabled", "detectors.exfil"} <= set(fields)
    with pytest.raises(det.SettingsError) as exc2:
        det.merge_settings({"detectors": {"port_scan": {"min_ports": 500, "critical_ports": 200}}}, strict=True)
    assert "detectors.port_scan.critical_ports" in exc2.value.fields
    ok = det.merge_settings({"detectors": {"sensitive_egress": {"ports": ["3389", 22, 22]}, "icmp_flood": {"min_pps": 0.0 + 500}}},
                            strict=True)
    assert ok["detectors"]["sensitive_egress"]["ports"] == [22, 3389]
    assert ok["detectors"]["icmp_flood"]["min_pps"] == 500
    payload = det.settings_payload(ok)
    row = next(d for d in payload["detectors"] if d["kind"] == "sensitive_egress")
    assert next(p for p in row["params"] if p["key"] == "ports")["value"] == [22, 3389]


# ── Address classes (X-32) ───────────────────────────────────────────────────

@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("140.82.112.3", True), ("100.64.1.1", False), ("169.254.10.1", False),
    ("198.51.100.20", False), ("203.0.113.9", False), ("224.0.0.5", False), ("10.1.2.3", False),
    ("2a00:1450::1", True), ("2001:db8::1", False), ("fe80::1", False), ("fd00::1", False),
    ("::ffff:8.8.8.8", True), ("::ffff:10.0.0.1", False), ("not-an-ip", False),
])
def test_public_ranges_exclude_special_use(ip, public):
    assert det.is_public_ip(ip) is public


def test_internal_ranges():
    assert det.is_internal_ip("10.0.0.5") and det.is_internal_ip("100.64.0.9") and det.is_internal_ip("fd12::1")
    assert not det.is_internal_ip("8.8.8.8") and not det.is_internal_ip("169.254.1.1")


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_runs_need_consecutive_minutes():
    assert det._runs([1, 2, 3, 7, 8, 10, 11, 12, 13], 3) == [(1, 3), (10, 13)]
    assert det._runs([5], 1) == [(5, 5)]
    assert det._runs([], 1) == []


def test_segments_merge_and_cap():
    old = [{"from": "2026-09-30T10:00:00Z", "to": "2026-09-30T10:05:00Z", "peak": 3}]
    new = [{"from": "2026-09-30T10:05:00Z", "to": "2026-09-30T10:10:00Z", "peak": 7},
           {"from": "2026-09-30T11:00:00Z", "to": "2026-09-30T11:05:00Z", "peak": 1}]
    merged = det._merge_segments(old, new)
    assert merged == [{"from": "2026-09-30T10:00:00Z", "to": "2026-09-30T10:10:00Z", "peak": 7},
                      {"from": "2026-09-30T11:00:00Z", "to": "2026-09-30T11:05:00Z", "peak": 1}]
    many = [{"from": f"2026-09-30T{h:02d}:00:00Z", "to": f"2026-09-30T{h:02d}:05:00Z"} for h in range(24)]
    many += [{"from": "2026-10-01T00:00:00Z", "to": "2026-10-01T00:05:00Z"}]
    assert len(det._merge_segments([], many)) == det.MAX_SEGMENTS
    assert det._merge_segments([], many)[-1]["from"] == "2026-10-01T00:00:00Z"


def test_evaluated_intervals_measure_engine_coverage():
    now = T("2026-10-01T12:00:00")
    iv = det.add_evaluated([], T("2026-10-01T10:00:00"), T("2026-10-01T10:05:00"), now)
    iv = det.add_evaluated(iv, T("2026-10-01T10:05:00"), T("2026-10-01T10:10:00"), now)
    iv = det.add_evaluated(iv, T("2026-10-01T11:00:00"), T("2026-10-01T11:05:00"), now)
    assert iv == [["2026-10-01T10:00:00Z", "2026-10-01T10:10:00Z"], ["2026-10-01T11:00:00Z", "2026-10-01T11:05:00Z"]]
    assert det.evaluated_seconds(iv, T("2026-10-01T10:00:00"), T("2026-10-01T12:00:00")) == 900
    assert det.evaluated_seconds(iv, T("2026-10-01T10:08:00"), T("2026-10-01T11:01:00")) == 180
    # Older than the retention of the list is dropped.
    old = det.add_evaluated([], now - timedelta(days=200), now - timedelta(days=199), now)
    assert old == []


def _cand(**kw) -> det.Candidate:
    base = dict(kind="icmp_flood", exporter="10.9.0.1", subject=("10.12.50.81",), severity="warning",
                confidence="medium", title="t", description="d", first_seen=T("2026-09-29T10:00:00"),
                last_seen=T("2026-09-30T10:00:00"), latest=(T("2026-09-30T09:55:00"), T("2026-09-30T10:00:00")),
                occurrences=3, method="m", threshold={}, observed={}, src="10.12.50.81", host="10.12.50.81",
                evidence_filter={"exporter": "10.9.0.1", "src": "10.12.50.81", "protocol": "1"})
    base.update(kw)
    return det.Candidate(**base)


def test_candidate_evidence_is_frozen_and_capped():
    c = _cand()
    ev = c.evidence()
    assert ev["window"] == {"from": "2026-09-29T10:00:00Z", "to": "2026-09-30T10:00:00Z"}   # NF-09: the judged 24 h
    assert ev["filter"] == {"exporter": "10.9.0.1", "src": "10.12.50.81", "protocol": "1"}
    assert ev["latest"] == {"from": "2026-09-30T09:55:00Z", "to": "2026-09-30T10:00:00Z"}
    assert ev["truncated_span"] is False
    long = _cand(first_seen=T("2026-07-01T00:00:00"))
    lev = long.evidence()
    assert lev["truncated_span"] is True and lev["window"]["from"] == "2026-08-30T10:00:00Z"   # 31 days (raw limit)
    # The fingerprint follows kind + exporter + subject, never the window: a recurring host is one finding.
    assert c.fingerprint == long.fingerprint
    assert c.fingerprint != _cand(exporter="10.9.0.2").fingerprint
    item = det.candidate_item(c)
    for key in ("id", "kind", "label", "severity", "confidence", "status", "title", "description", "src", "dst",
                "exporter", "first_seen", "last_seen", "occurrences", "evidence", "metrics", "suppressed",
                "persisted", "threshold", "observed", "baseline"):
        assert key in item
    assert item["status"] == "candidate" and item["persisted"] is False


def test_exception_rules_match_but_never_drop():
    now = T("2026-10-01T00:00:00")
    rule = det.ExceptionRule(id="x1", kind="icmp_flood", src=ipaddress.ip_network("10.12.50.0/24"), dst=None,
                             dst_port=None, reason="Monitoring host", expires_at=None)
    c = _cand()
    assert rule.matches(c, now)
    assert not rule.matches(_cand(kind="icmp_sweep"), now)
    assert not rule.matches(_cand(src="10.12.51.1", host="10.12.51.1"), now)
    expired = det.ExceptionRule(id="x2", kind=None, src=ipaddress.ip_network("10.12.50.81/32"), dst=None,
                                dst_port=None, reason="old", expires_at=now - timedelta(seconds=1))
    assert not expired.matches(c, now)
    port_rule = det.ExceptionRule(id="x3", kind=None, src=None, dst=ipaddress.ip_network("2a00:1450::/32"),
                                  dst_port=22, reason="backup over ssh", expires_at=None)
    egress = _cand(kind="sensitive_egress", dst="2a00:1450::5", observed={"ports": [22, 443]})
    assert port_rule.matches(egress, now)
    assert not port_rule.matches(_cand(kind="sensitive_egress", dst="2a00:1450::5", observed={"ports": [3389]}), now)
    cands = [c, _cand(kind="icmp_sweep")]
    det.apply_exceptions(cands, [rule], now)
    assert cands[0].exception["reason"] == "Monitoring host" and cands[1].exception is None
    item = det.candidate_item(cands[0])
    assert item["status"] == "suppressed" and item["suppressed"] is True    # kept, shown as suppressed
    assert item["suppression"]["reason"] == "Exception: Monitoring host"


def _cov(**exporters) -> det.Coverage:
    return det.Coverage(exporters={k.replace("_", "."): det.ExporterCoverage(exporter=k.replace("_", "."), **v)
                                   for k, v in exporters.items()})


def _ctx(cov: det.Coverage) -> det.RunContext:
    return det.RunContext(start=T("2026-10-01T00:00:00"), end=T("2026-10-01T00:05:00"), vis=access.UNRESTRICTED_SYSTEM,
                          base=FlowFilter(), settings=det.default_settings(), budget_s=10, limit=10, persist=False,
                          coverage=cov)


def test_flag_availability_nf07():
    # No exporter exported TCP flags -> unavailable with the exporters named.
    out = det.Outcome(kind="syn_scan", status="ok")
    assert det._flag_availability(_ctx(_cov(a10_0_0_1={"records": 50, "tcp_records": 40, "tcp_flagged": 0})), out) is False
    assert out.status == "unavailable" and "10.0.0.1" in out.reason and "cannot run" in out.reason
    # One of two exporters lacks flags -> runs, says which part it cannot see.
    out2 = det.Outcome(kind="syn_scan", status="ok")
    ctx2 = _ctx(det.Coverage(exporters={
        "10.0.0.1": det.ExporterCoverage("10.0.0.1", records=50, tcp_records=40, tcp_flagged=0),
        "10.0.0.2": det.ExporterCoverage("10.0.0.2", records=50, tcp_records=40, tcp_flagged=40),
    }))
    assert det._flag_availability(ctx2, out2) is True
    assert out2.status == "ok" and out2.exporters_unavailable == ["10.0.0.1"] and out2.coverage_pct == 50.0
    assert any("10.0.0.1" in n for n in out2.notes)
    # No TCP at all: nothing a SYN/RST check could see, not "unavailable".
    out3 = det.Outcome(kind="rst_flood", status="ok")
    assert det._flag_availability(_ctx(det.Coverage(exporters={"10.0.0.3": det.ExporterCoverage("10.0.0.3", records=9)})), out3)
    assert out3.status == "ok" and out3.notes == ["No TCP records in this window"]


def test_run_result_complete_only_when_every_enabled_detector_ran():
    def rr(*statuses):
        return det.RunResult(T("2026-10-01T00:00:00"), T("2026-10-01T00:05:00"),
                             [det.Outcome(kind=k, status=s) for k, s in zip(det.KINDS, statuses)], det.Coverage(),
                             T("2026-10-01T00:07:00"))
    assert rr(*["ok"] * 10).complete
    assert rr(*(["ok"] * 9 + ["disabled"])).complete
    assert not rr(*(["ok"] * 9 + ["unavailable"])).complete
    assert not rr(*(["ok"] * 9 + ["failed"])).complete
    assert not rr(*["disabled"] * 10).complete


def test_scoped_detector_status():
    now = T("2026-10-01T12:00:00")
    settings = det.default_settings()
    never = det.scoped_detectors({}, settings, None, now)
    assert {d["status"] for d in never} == {"no_coverage"}
    state = {
        "last_run_at": "2026-10-01T11:57:00Z", "last_bucket_end": "2026-10-01T11:55:00Z",
        "exporters": ["10.0.0.1", "10.0.0.2"],
        "detectors": {
            "syn_scan": {"status": "ok", "exporters_seen": ["10.0.0.1", "10.0.0.2"], "exporters_unavailable": ["10.0.0.1"],
                         "notes": ["TCP flags are not exported by 10.0.0.1"]},
            **{k: {"status": "ok", "exporters_seen": ["10.0.0.1", "10.0.0.2"]} for k in det.KINDS if k != "syn_scan"},
        },
    }
    rows = {d["kind"]: d for d in det.scoped_detectors(state, settings, None, now)}
    assert rows["syn_scan"]["status"] == "ok" and rows["syn_scan"]["exporters_unavailable"] == ["10.0.0.1"]
    # A user who only sees the flagless exporter gets "unavailable", not a clean "ok".
    only1 = {d["kind"]: d for d in det.scoped_detectors(state, settings, frozenset({"10.0.0.1"}), now)}
    assert only1["syn_scan"]["status"] == "unavailable" and "10.0.0.1" in only1["syn_scan"]["reason"]
    assert only1["dominant_sender"]["status"] == "ok"
    # A user whose exporters sent nothing in the evaluated period.
    other = {d["kind"]: d for d in det.scoped_detectors(state, settings, frozenset({"10.9.9.9"}), now)}
    assert other["dominant_sender"]["status"] == "no_coverage"
    # A stopped engine fails every check.
    stale = det.scoped_detectors(state, settings, None, now + timedelta(hours=1))
    assert {d["status"] for d in stale} == {"failed"}
    settings["detectors"]["icmp_sweep"]["enabled"] = False
    off = {d["kind"]: d for d in det.scoped_detectors(state, settings, None, now)}
    assert off["icmp_sweep"]["status"] == "disabled"
    assert det.engine_status(state, now)["status"] == "running"
    assert det.engine_status({}, now)["status"] == "never"


def test_finding_item_shape_is_contract_11():
    row = {
        "id": uuid.uuid4(), "fingerprint": "f" * 40, "kind": "port_scan", "severity": "critical", "confidence": "high",
        "status": "suppressed", "title": "TCP port scan", "description": "x", "src_ip": "10.7.7.7/32",
        "dst_ip": "10.0.0.10", "exporter_ip": "10.9.0.1", "first_seen": T("2026-10-01T00:00:00"),
        "last_seen": T("2026-10-01T00:05:00"), "occurrences": 1,
        "evidence": {"window": {"from": "2026-10-01T00:00:00Z", "to": "2026-10-01T00:05:00Z"}, "host": "10.7.7.7"},
        "metrics": {"method": "m", "threshold": {"ports_per_5min": 100}, "observed": {"peak_ports": 3000},
                    "exception_id": "e1"},
        "suppressed_until": T("2026-10-08T00:00:00"), "suppress_reason": "Exception: pentest", "notes": [{"id": "1"}],
    }
    item = det.finding_item(row)
    assert item["src"] == "10.7.7.7" and item["dst"] == "10.0.0.10" and item["persisted"] is True
    assert item["label"] == "Port scan (TCP SYN)" and item["notes_count"] == 1
    assert item["suppression"] == {"reason": "Exception: pentest", "until": "2026-10-08T00:00:00Z", "by": None,
                                   "exception_id": "e1"}
    assert item["threshold"] == {"ports_per_5min": 100} and item["observed"] == {"peak_ports": 3000}


# ── Findings API over fakes (validation, permissions) ────────────────────────

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.v1 import netflow  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.security import get_current_user  # noqa: E402

FID = uuid.UUID("11111111-2222-4333-8444-555555555555")


class FakeDB:
    def __init__(self, finding_row=None):
        self.finding_row = finding_row
        self.writes: list[str] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        rows: list = []
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            self.writes.append(sql)
        elif "FROM netflow_findings WHERE id" in sql and self.finding_row is not None:
            rows = [SimpleNamespace(_mapping=self.finding_row)]
        return SimpleNamespace(all=lambda: list(rows), first=lambda: rows[0] if rows else None,
                               scalar=lambda: 0, rowcount=0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    def add(self, obj):
        self.writes.append(f"ADD {type(obj).__name__}")

    def begin_nested(self):
        db = self

        class _Nested:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        return _Nested()

    async def flush(self):
        pass


def _vis(*perms, exporters=None) -> FlowVisibility:
    return FlowVisibility(restricted=exporters is not None, exporters=exporters, user_id=str(uuid.uuid4()),
                          permissions=frozenset({"netflow.view", *perms}))


def _client(vis: FlowVisibility, db: FakeDB) -> TestClient:
    app = FastAPI()
    app.include_router(netflow.router, prefix="/api/v1")
    user = SimpleNamespace(id=uuid.uuid4(), username="tester", role="viewer", is_active=True, scope_tags=[])

    async def fake_db():
        yield db

    async def get_user():
        return user

    async def visibility():
        return vis

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = get_user
    app.dependency_overrides[access.require_view] = get_user
    app.dependency_overrides[access.flow_visibility] = visibility
    def _guard(*perms):
        async def dependency():
            if not any(vis.can(p) for p in perms):
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail="Insufficient permissions")
            return user
        return dependency

    app.dependency_overrides[access.require_manage] = _guard(access.MANAGE_PERMISSION)
    app.dependency_overrides[access.require_export] = _guard(access.EXPORT_PERMISSION)
    app.dependency_overrides[access.require_triage] = _guard(access.MANAGE_PERMISSION, "alerts.acknowledge")
    return TestClient(app)


def _row(**kw) -> dict:
    row = {
        "id": FID, "fingerprint": "f" * 40, "kind": "syn_scan", "severity": "warning", "confidence": "medium",
        "status": "open", "title": "TCP host sweep", "description": "d", "src_ip": "10.7.7.8", "dst_ip": None,
        "exporter_ip": "10.9.0.1", "first_seen": T("2026-10-01T00:00:00"), "last_seen": T("2026-10-01T00:05:00"),
        "occurrences": 1, "evidence": {}, "metrics": {}, "alert_id": None, "assigned_to": None,
        "acknowledged_by": None, "acknowledged_at": None, "suppressed_until": None, "suppressed_by": None,
        "suppress_reason": None, "resolved_at": None, "notes": [], "created_at": None, "updated_at": None,
    }
    row.update(kw)
    return row


@pytest.mark.parametrize("params,field", [
    ({"status": "closed"}, "status"), ({"kind": "exfil"}, "kind"), ({"severity": "high"}, "severity"),
    ({"sort": "random"}, "sort"),
])
def test_findings_validation_is_400_with_string_detail(params, field):
    r = _client(_vis(), FakeDB()).get("/api/v1/netflow/findings", params={"hours": 24, **params})
    assert r.status_code == 400
    body = r.json()
    assert isinstance(body["detail"], str) and body["code"] == "invalid_filter"


def test_lifecycle_permissions_contract_6():
    path = f"/api/v1/netflow/findings/{FID}/actions"
    # netflow.view alone: read-only.
    db = FakeDB(_row())
    r = _client(_vis(), db).post(path, json={"action": "acknowledge"})
    assert r.status_code == 403 and isinstance(r.json()["detail"], str)
    assert not db.writes
    # alerts.acknowledge may acknowledge but not suppress.
    db2 = FakeDB(_row())
    r2 = _client(_vis("alerts.acknowledge"), db2).post(path, json={"action": "suppress", "reason": "x", "hours": 24})
    assert r2.status_code == 403 and "netflow.manage" in r2.json()["detail"]
    db3 = FakeDB(_row())
    r3 = _client(_vis("alerts.acknowledge"), db3).post(path, json={"action": "acknowledge"})
    assert r3.status_code == 200
    assert any("UPDATE netflow_findings" in w for w in db3.writes) and db3.commits == 1
    # Suppression needs a reason and an expiry.
    for body in ({"action": "suppress", "hours": 24}, {"action": "suppress", "reason": "pentest"}):
        r4 = _client(_vis("netflow.manage"), FakeDB(_row())).post(path, json=body)
        assert r4.status_code == 400
    # Out-of-scope exporter: 404, not 403 (contract §6).
    r5 = _client(_vis("netflow.manage", exporters=frozenset({"10.9.9.9"})), FakeDB(_row())).get(f"/api/v1/netflow/findings/{FID}")
    assert r5.status_code == 404
    # A non-UUID id is a validation error (NetFlow maps them to 400), not a 500.
    assert _client(_vis(), FakeDB()).get("/api/v1/netflow/findings/not-a-uuid").status_code == 400


def test_settings_and_exception_writes_need_manage():
    c = _client(_vis("alerts.acknowledge"), FakeDB())
    assert c.put("/api/v1/netflow/detectors", json={"quiet_minutes": 30}).status_code == 403
    assert c.post("/api/v1/netflow/detector-exceptions", json={"src_cidr": "10.0.0.1", "reason": "x"}).status_code == 403
    assert c.post("/api/v1/netflow/ti/feeds", json={"name": "x", "source": "upload", "content": "1.2.3.4"}).status_code == 403
    m = _client(_vis("netflow.manage"), FakeDB())
    r = m.post("/api/v1/netflow/detector-exceptions", json={"reason": "everything"})
    assert r.status_code == 400 and "cannot match everything" in r.json()["detail"]
    r2 = m.post("/api/v1/netflow/detector-exceptions", json={"src_cidr": "10.0.0.300", "reason": "x"})
    assert r2.status_code == 400
    r3 = m.put("/api/v1/netflow/detectors", json={"detectors": {"syn_scan": {"min_destinations": 1}}})
    assert r3.status_code == 400 and "min_destinations" in r3.json()["detail"]


# ── ClickHouse fixtures ──────────────────────────────────────────────────────

from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402


@pytest.fixture
def real_ch(monkeypatch):
    client = scratch_client()
    per_thread = threading.local()

    def thread_client():
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    monkeypatch.setattr(C, "get_ch_client", thread_client)
    C._retention_cache.update(value=None, at=0.0)
    det._adhoc_cache.clear()
    yield client
    C._retention_cache.update(value=None, at=0.0)


COLS = ["timestamp", "received_at", "exporter_ip", "exporter_ip6", "src_addr", "dst_addr", "src_addr6", "dst_addr6",
        "ip_version", "src_port", "dst_port", "protocol", "bytes", "packets", "first_switched_ms", "last_switched_ms",
        "fields_present", "fw_event", "tcp_flags", "sampling_interval"]
TRACKED = (1 << 31) | 0b1 | (1 << 1) | (1 << 2) | (1 << 5) | (1 << 11)
NO_FLAGS = (1 << 31) | (1 << 1) | (1 << 2) | (1 << 5)


def rec(t: datetime, exporter: str, src: str, dst: str, *, proto=6, sport=40000, dport=443, bytes_=500, packets=5,
        flags=0x18, fields=TRACKED, duration_s=1.0, fw_event=0, sampling=1) -> list:
    v6 = ":" in src or ":" in dst
    end_ms = int(t.timestamp() * 1000)
    return [t, t, exporter, "::", "0.0.0.0" if v6 else src, "0.0.0.0" if v6 else dst, src if v6 else "::",
            dst if v6 else "::", 6 if v6 else 4, sport, dport, proto, bytes_, packets,
            end_ms - int(duration_s * 1000), end_ms, fields, fw_event, flags, sampling]


def _insert(client, db, rows):
    client.insert(f"{db}.flow_records", rows, column_names=COLS)


def _base(now: datetime) -> datetime:
    return datetime.fromtimestamp((int(now.timestamp()) // 300) * 300, tz=UTC) - timedelta(hours=3)


def _fixtures(t0: datetime) -> list[list]:
    rows: list[list] = []
    s = lambda sec: t0 + timedelta(seconds=sec)  # noqa: E731
    # X-31: vertical scan, 3000 ports of one host in ~2 minutes (SYN only).
    rows += [rec(s(10 + i * 0.04), "10.9.0.1", "10.7.7.7", "10.0.0.10", sport=50000, dport=1 + i, flags=0x02,
                 bytes_=44, packets=1) for i in range(3000)]
    # Horizontal sweep: 150 hosts.
    rows += [rec(s(30 + i), "10.9.0.1", "10.7.7.8", f"10.0.1.{i + 1}", dport=445, flags=0x02, bytes_=44, packets=1)
             for i in range(150)]
    # NF-07: an exporter whose template has no TCP flags field (bit 0 clear): same sweep, flags read as 0.
    rows += [rec(s(30 + i), "10.9.0.2", "10.7.7.9", f"10.0.2.{i + 1}", dport=445, flags=0, fields=NO_FLAGS,
                 bytes_=44, packets=1) for i in range(200)]
    # NF-07: explicit zero flags (field exported, value 0): flags known, nothing to match.
    rows += [rec(s(40 + i), "10.9.0.3", "10.7.7.10", "10.0.3.1", flags=0, bytes_=60, packets=1) for i in range(50)]
    # X-34: five more sweeping sources on one exporter (counts must not be capped by the limit).
    for k in range(5):
        rows += [rec(s(60 + i), "10.9.0.7", f"10.7.8.{k + 1}", f"10.0.7.{i + 1}", dport=22, flags=0x02, bytes_=44,
                     packets=1) for i in range(120)]
    # NF-22: on 10.9.0.4 the leading host has 71 % of the top five but only 20 % of the exporter's bytes.
    rows.append(rec(s(100), "10.9.0.4", "10.4.0.1", "10.0.4.200", bytes_=2_000_000_000, packets=1_500_000))
    for k in range(4):
        rows.append(rec(s(101 + k), "10.9.0.4", f"10.4.0.{k + 2}", "10.0.4.200", bytes_=200_000_000, packets=150_000))
    for k in range(100):
        rows.append(rec(s(110 + k), "10.9.0.4", f"10.4.1.{k + 1}", "10.0.4.200", bytes_=72_000_000, packets=60_000))
    # Positive: 60 % of exporter 10.9.0.5.
    rows.append(rec(s(100), "10.9.0.5", "10.5.0.1", "10.0.5.200", bytes_=3_000_000_000, packets=2_000_000))
    rows.append(rec(s(101), "10.9.0.5", "10.5.0.2", "10.0.5.200", bytes_=2_000_000_000, packets=1_400_000))
    # X-36: a corrupt record and NSEL create/deny events never count; the real sender dominates.
    rows.append(rec(s(100), "10.9.0.6", "10.6.0.66", "10.0.6.200", bytes_=2 * 10**15, packets=10))
    rows.append(rec(s(101), "10.9.0.6", "10.6.0.68", "10.0.6.200", bytes_=9 * 10**11, packets=10, fw_event=1))
    rows.append(rec(s(102), "10.9.0.6", "10.6.0.67", "10.0.6.200", bytes_=2_000_000_000, packets=1_400_000))
    # X-32: sensitive egress.
    rows.append(rec(s(120), "10.9.0.8", "10.0.8.5", "140.82.112.3", dport=22, flags=0x02, bytes_=60, packets=1))  # one SYN
    rows.append(rec(s(121), "10.9.0.8", "10.0.8.6", "100.64.1.1", dport=3389, flags=0x18, bytes_=50_000))          # CGNAT
    rows.append(rec(s(122), "10.9.0.8", "10.0.8.6", "169.254.1.1", dport=3389, flags=0x18, bytes_=50_000))         # link-local
    rows.append(rec(s(123), "10.9.0.8", "10.0.8.6", "198.51.100.20", dport=3389, flags=0x18, bytes_=50_000))       # TEST-NET-2
    rows.append(rec(s(124), "10.9.0.8", "10.0.8.7", "52.1.2.3", dport=3389, flags=0x18, bytes_=80_000))            # established
    rows.append(rec(s(125), "10.9.0.8", "10.0.8.7", "52.1.2.3", dport=22, flags=0x18, bytes_=1_000))
    # IPv6 ICMPv6 echo sweep (type 128 -> dst_port 32768).
    rows += [rec(s(130 + i), "10.9.0.9", "2001:db8::5", f"2001:db8:1::{i + 1:x}", proto=58, sport=0, dport=32768,
                 flags=0, bytes_=64, packets=1) for i in range(120)]
    # NF-21: a sustained flood (6 minutes at 5000 pps) and a 2-minute burst below the duration.
    m0 = int(t0.timestamp()) // 60 + 2
    for m in range(6):
        end = datetime.fromtimestamp((m0 + m + 1) * 60, tz=UTC)
        rows.append(rec(end, "10.9.0.10", "10.6.6.6", "10.0.10.1", proto=1, sport=0, dport=2048, flags=0,
                        bytes_=300_000 * 84, packets=300_000, duration_s=60))
    for m in range(2):
        end = datetime.fromtimestamp((m0 + 10 + m + 1) * 60, tz=UTC)
        rows.append(rec(end, "10.9.0.10", "10.6.6.7", "10.0.10.2", proto=1, sport=0, dport=2048, flags=0,
                        bytes_=300_000 * 84, packets=300_000, duration_s=60))
    # Threat intel: an outbound session to a listed IPv4 address and inbound-only from a listed IPv6 one.
    rows.append(rec(s(140), "10.9.0.11", "10.0.11.5", "52.9.9.9", dport=443, bytes_=5_000))
    rows.append(rec(s(141), "10.9.0.11", "52.9.9.9", "10.0.11.5", sport=443, dport=40000, bytes_=90_000))
    rows.append(rec(s(142), "10.9.0.11", "2a00:1450::5", "fd00::7", sport=443, dport=40001, bytes_=7_000))
    return rows


@pytest.fixture
def fixture_run(real_ch, monkeypatch):
    now = datetime.now(UTC)
    t0 = _base(now)
    index = ti.TiIndex([("f1", "52.9.9.0/24"), ("f2", "2a00:1450::/32")],
                       {"f1": ti.FeedInfo("f1", "Lab C2 list", "botnet_c2"), "f2": ti.FeedInfo("f2", "Lab scanners", "scanner")})

    async def get_index(db=None):
        return index

    monkeypatch.setattr(ti, "get_index", get_index)
    with flow_db(real_ch) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        _insert(real_ch, db, _fixtures(t0))

        def run(start=t0, end=t0 + timedelta(minutes=30), vis=access.UNRESTRICTED_SYSTEM, base=FlowFilter(),
                kinds=None, limit=50, settings=None):
            ctx = det.RunContext(start=start, end=end, vis=vis, base=base, settings=settings or det.default_settings(),
                                 budget_s=60, limit=limit, persist=False, now=now)
            return asyncio.run(det.run_detectors(ctx, kinds))

        yield SimpleNamespace(run=run, t0=t0, db=db, client=real_ch)


def _by(result: det.RunResult, kind: str) -> det.Outcome:
    return next(o for o in result.outcomes if o.kind == kind)


def test_detectors_on_fixtures(fixture_run):
    res = fixture_run.run()
    t0 = fixture_run.t0
    statuses = {o.kind: o.status for o in res.outcomes}
    assert statuses["syn_scan"] == "ok" and statuses["port_scan"] == "ok"
    assert all(o.status != "failed" for o in res.outcomes), [(o.kind, o.reason, o.error_id) for o in res.outcomes]
    # volume_spike needs rollup history the fixture database does not have: honest no_coverage.
    assert statuses["volume_spike"] == "no_coverage" and "hours" in _by(res, "volume_spike").reason
    assert res.complete is False

    # X-31: the vertical scan is found (the old destination-count detector returned nothing).
    ps = _by(res, "port_scan")
    vert = [c for c in ps.candidates if c.src == "10.7.7.7"]
    assert len(vert) == 1 and vert[0].dst == "10.0.0.10" and vert[0].observed["peak_ports"] >= 2000
    assert vert[0].severity == "critical"
    assert vert[0].evidence_filter == {"exporter": "10.9.0.1", "src": "10.7.7.7", "dst": "10.0.0.10", "protocol": "6",
                                       "tcpflag": "syn_only"}

    syn = _by(res, "syn_scan")
    srcs = {c.src for c in syn.candidates}
    assert "10.7.7.8" in srcs
    # NF-07: the flagless exporter's sweep is not judged on fabricated zero flags, and it is named.
    assert "10.7.7.9" not in srcs and "10.9.0.2" in syn.exporters_unavailable
    # Explicit zero flags are known flags: 10.9.0.3 is available (nothing matched).
    assert "10.9.0.3" not in syn.exporters_unavailable
    # X-34: five sweepers on 10.9.0.7 plus 10.7.7.8 -> six matches whatever the limit.
    assert syn.match_count == 6 and not syn.truncated

    # X-32 sensitive egress: only the established session to a public address.
    eg = _by(res, "sensitive_egress")
    assert [(c.src, c.dst) for c in eg.candidates] == [("10.0.8.7", "52.1.2.3")]
    e = eg.candidates[0]
    assert e.severity == "warning" and e.confidence == "high"
    assert e.evidence_filter["dst"] == "52.1.2.3" and e.evidence_filter["dst_ports"] == "22,3389"   # NF-09

    # NF-22: the 20 % host is not "dominant"; the 60 % host is, with the exporter's whole total.
    dom = _by(res, "dominant_sender")
    dsrc = {c.src: c for c in dom.candidates}
    assert "10.4.0.1" not in dsrc
    assert "10.5.0.1" in dsrc and dsrc["10.5.0.1"].observed["share_pct"] == 60.0
    assert dsrc["10.5.0.1"].observed["total_bytes"] == 5_000_000_000
    # X-36: the corrupt record and the NSEL event are guarded out; the real sender has 100 %.
    assert "10.6.0.66" not in dsrc and "10.6.0.68" not in dsrc
    assert dsrc["10.6.0.67"].observed["share_pct"] == 100.0

    # IPv6 ICMP sweep.
    sw = _by(res, "icmp_sweep")
    v6 = [c for c in sw.candidates if c.src == "2001:db8::5"]
    assert len(v6) == 1 and v6[0].evidence_filter["protocol"] == "58" and v6[0].observed["peak_destinations"] == 120

    # NF-21: the sustained flood is found with its segment; the 2-minute burst is not.
    fl = _by(res, "icmp_flood")
    floods = {c.src: c for c in fl.candidates}
    assert set(floods) == {"10.6.6.6"}
    f = floods["10.6.6.6"]
    assert f.observed["peak_pps"] == 5000.0 and f.observed["sustained_minutes"] == 6
    m0 = (int(t0.timestamp()) // 60 + 2) * 60
    assert f.segments == [{"from": det._iso(datetime.fromtimestamp(m0, tz=UTC)),
                           "to": det._iso(datetime.fromtimestamp(m0 + 360, tz=UTC)), "peak": 5000.0}]
    assert f.confidence == "low" and "no ICMP history" in f.description
    assert f.evidence()["filter"] == {"exporter": "10.9.0.10", "src": "10.6.6.6", "protocol": "1"}
    tgt = {c.dst for c in _by(res, "icmp_flood_target").candidates}
    assert tgt == {"10.0.10.1"}

    # Threat intel: IPv4 outbound (critical, C2 feed) and IPv6 inbound-only (info).
    tcs = {c.extra["ti_address"]: c for c in _by(res, "threat_intel").candidates}
    assert set(tcs) == {"52.9.9.9", "2a00:1450::5"}
    out4 = tcs["52.9.9.9"]
    assert out4.severity == "critical" and out4.src == "10.0.11.5" and out4.dst == "52.9.9.9"
    assert out4.observed["sent_bytes"] == 5_000 and out4.observed["bytes"] == 95_000
    assert out4.description == "10.0.11.5 sent 5 kB to 52.9.9.9 and received 90 kB from it; listed by Lab C2 list (botnet_c2)"
    in6 = tcs["2a00:1450::5"]
    assert in6.severity == "info" and in6.observed["direction"] == "inbound" and in6.evidence_filter["talker"] == "2a00:1450::5"


def test_limit_reports_true_match_count(fixture_run):
    res = fixture_run.run(kinds=["syn_scan"], limit=2)
    syn = _by(res, "syn_scan")
    assert syn.match_count == 6 and len(syn.candidates) == 2 and syn.truncated        # X-34


def test_flagless_exporter_alone_is_unavailable(fixture_run):
    res = fixture_run.run(base=FlowFilter(exporter="10.9.0.2"), kinds=["syn_scan", "port_scan", "rst_flood", "dominant_sender"])
    st = {o.kind: o for o in res.outcomes}
    for k in ("syn_scan", "port_scan", "rst_flood"):
        assert st[k].status == "unavailable" and "10.9.0.2" in st[k].reason             # NF-07
    assert st["dominant_sender"].status == "ok"
    assert res.complete is False


def test_scope_and_empty_windows(fixture_run):
    # Scope before filters: a caller who sees another exporter gets no data, never another's findings.
    vis = FlowVisibility(restricted=True, exporters=frozenset({"10.99.0.1"}), user_id=None,
                         permissions=frozenset({"netflow.view"}))
    res = fixture_run.run(vis=vis)
    assert not res.candidates
    assert {o.status for o in res.outcomes} == {"no_coverage"}
    # A window with no records: nothing ran, nothing is claimed.
    t0 = fixture_run.t0
    empty = fixture_run.run(start=t0 - timedelta(hours=2), end=t0 - timedelta(hours=1))
    assert {o.status for o in empty.outcomes} == {"no_coverage"} and empty.complete is False


def test_benign_periodic_icmp_is_not_a_flood_over_seven_days(real_ch, monkeypatch):
    # NF-21 acceptance: 0.5 pps every minute for 7 days (302,400 packets, 10,080 records -- far over
    # the old 5,000-record threshold) is not a flood because the window got longer.
    now = datetime.now(UTC)
    end = datetime.fromtimestamp((int(now.timestamp()) // 60) * 60, tz=UTC) - timedelta(minutes=10)
    start = end - timedelta(days=7)
    rows = [rec(start + timedelta(minutes=i + 1), "10.9.1.1", "10.5.5.5", "10.0.0.1", proto=1, sport=0, dport=2048,
                flags=0, bytes_=30 * 84, packets=30, duration_s=60) for i in range(7 * 24 * 60)]
    with flow_db(real_ch) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        _insert(real_ch, db, rows)
        ctx = det.RunContext(start=start, end=end + timedelta(minutes=1), vis=access.UNRESTRICTED_SYSTEM,
                             base=FlowFilter(), settings=det.default_settings(), budget_s=60, limit=50,
                             persist=False, now=now)
        res = asyncio.run(det.run_detectors(ctx, ["icmp_flood", "icmp_flood_target", "icmp_sweep"]))
        assert all(o.status == "ok" for o in res.outcomes), [(o.kind, o.status, o.reason) for o in res.outcomes]
        assert not res.candidates


def test_adhoc_run_applies_exceptions_and_caches(fixture_run, monkeypatch):
    rules = [det.ExceptionRule(id="x1", kind="icmp_flood", src=ipaddress.ip_network("10.6.6.6/32"), dst=None,
                               dst_port=None, reason="Load test", expires_at=None)]

    async def load_exceptions(db, include_expired=False):
        return rules

    async def load_settings(db):
        return det.default_settings()

    monkeypatch.setattr(det, "load_exceptions", load_exceptions)
    monkeypatch.setattr(det, "load_settings", load_settings)
    t0 = fixture_run.t0
    res = asyncio.run(det.run_adhoc(None, t0, t0 + timedelta(minutes=30), access.UNRESTRICTED_SYSTEM, FlowFilter(),
                                    kinds=["icmp_flood"]))
    flood = [c for c in res.candidates if c.src == "10.6.6.6"]
    assert flood and flood[0].exception["reason"] == "Load test"            # kept, marked, not dropped
    again = asyncio.run(det.run_adhoc(None, t0, t0 + timedelta(minutes=30), access.UNRESTRICTED_SYSTEM, FlowFilter(),
                                      kinds=["icmp_flood"]))
    assert again is res


# ── PostgreSQL persistence ───────────────────────────────────────────────────

PG_DSN = os.environ.get("ZENPLUS_NETFLOW_TEST_PG")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="set ZENPLUS_NETFLOW_TEST_PG (scratch DB with migrate-138)")


@needs_pg
def test_upsert_lifecycle_auto_resolve_and_exceptions():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    exporter = "10.253.140.1"
    subject = f"10.253.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    def cand(start: datetime, **kw) -> det.Candidate:
        return _cand(kind="syn_scan", exporter=exporter, subject=(subject,), src=subject, host=subject,
                     first_seen=start, last_seen=start + timedelta(minutes=5),
                     latest=(start, start + timedelta(minutes=5)), occurrences=1,
                     segments=[{"from": det._iso(start), "to": det._iso(start + timedelta(minutes=5)), "peak": 150}],
                     observed={"peak_destinations": kw.pop("peak", 150)}, **kw)

    async def run():
        engine = create_async_engine(PG_DSN)
        t = T("2026-09-01T10:00:00")
        try:
            async with AsyncSession(engine) as db:
                c1 = cand(t)
                fp = c1.fingerprint
                await db.execute(text("DELETE FROM netflow_findings WHERE fingerprint = :f"), {"f": fp})
                st = await det.upsert_findings(db, [c1], t + timedelta(minutes=7))
                await db.commit()
                assert st["inserted"] == 1
                # Recurrence: one finding, more occurrences, merged segments, the peak kept.
                c2 = cand(t + timedelta(minutes=5), peak=120)
                st2 = await det.upsert_findings(db, [c2], t + timedelta(minutes=12))
                await db.commit()
                assert st2["updated"] == 1
                row = det.row_dict((await db.execute(text(f"SELECT {det.FINDING_COLUMNS} FROM netflow_findings WHERE fingerprint = :f"), {"f": fp})).first())
                assert row["status"] == "open" and row["occurrences"] == 2
                assert det._iso(row["first_seen"]) == det._iso(t) and det._iso(row["last_seen"]) == det._iso(t + timedelta(minutes=10))
                assert row["evidence"]["segments"] == [{"from": det._iso(t), "to": det._iso(t + timedelta(minutes=10)), "peak": 150}]
                assert row["evidence"]["window"] == {"from": det._iso(t), "to": det._iso(t + timedelta(minutes=10))}
                assert row["metrics"]["observed"]["peak_destinations"] == 150
                # Acknowledged stays acknowledged when it recurs.
                await db.execute(text("UPDATE netflow_findings SET status = 'acknowledged' WHERE fingerprint = :f"), {"f": fp})
                await det.upsert_findings(db, [cand(t + timedelta(minutes=10))], t + timedelta(minutes=17))
                await db.commit()
                assert (await db.execute(text("SELECT status FROM netflow_findings WHERE fingerprint = :f"), {"f": fp})).scalar() == "acknowledged"
                # Quiet for longer than quiet_minutes before the engine horizon -> resolved, with a note.
                n = await det.auto_resolve(db, t + timedelta(hours=3), 60, t + timedelta(hours=3))
                await db.commit()
                assert n >= 1
                row = det.row_dict((await db.execute(text(f"SELECT {det.FINDING_COLUMNS} FROM netflow_findings WHERE fingerprint = :f"), {"f": fp})).first())
                assert row["status"] == "resolved" and "Resolved automatically" in row["notes"][-1]["text"]
                # It recurs: reopened, not a second finding. first_seen and the evidence
                # window keep covering every counted occurrence (the card, the timeline
                # and Investigate agree); the latest occurrence is its own window.
                c4 = cand(t + timedelta(hours=4))
                st4 = await det.upsert_findings(db, [c4], t + timedelta(hours=4, minutes=7))
                await db.commit()
                assert st4["reopened"] == 1
                row = det.row_dict((await db.execute(text(f"SELECT {det.FINDING_COLUMNS} FROM netflow_findings WHERE fingerprint = :f"), {"f": fp})).first())
                assert row["status"] == "open" and det._iso(row["first_seen"]) == det._iso(t)
                assert row["occurrences"] == 4
                assert row["evidence"]["window"] == {"from": det._iso(t), "to": det._iso(t + timedelta(hours=4, minutes=5))}
                assert row["evidence"]["latest"] == {"from": det._iso(t + timedelta(hours=4)),
                                                     "to": det._iso(t + timedelta(hours=4, minutes=5))}
                assert [s["from"] for s in row["evidence"]["segments"]] == [det._iso(t), det._iso(t + timedelta(hours=4))]
                assert row["notes"][-1]["text"].startswith("Reopened")
                assert row["acknowledged_by"] is None and row["resolved_at"] is None
                # An exception suppresses (with its reason), it never deletes.
                rule = det.ExceptionRule(id=str(uuid.uuid4()), kind="syn_scan", src=ipaddress.ip_network(f"{subject}/32"),
                                         dst=None, dst_port=None, reason="Approved scanner", expires_at=None)
                assert await det.suppress_matching(db, rule, t + timedelta(hours=5)) == 1
                await db.commit()
                row = det.row_dict((await db.execute(text(f"SELECT {det.FINDING_COLUMNS} FROM netflow_findings WHERE fingerprint = :f"), {"f": fp})).first())
                assert row["status"] == "suppressed" and row["suppress_reason"] == "Exception: Approved scanner"
                assert row["metrics"]["exception_id"] == rule.id
                # A later occurrence keeps it suppressed and keeps counting.
                c5 = cand(t + timedelta(hours=5))
                det.apply_exceptions([c5], [rule], t + timedelta(hours=5))
                await det.upsert_findings(db, [c5], t + timedelta(hours=5, minutes=7))
                await db.commit()
                row = det.row_dict((await db.execute(text(f"SELECT {det.FINDING_COLUMNS} FROM netflow_findings WHERE fingerprint = :f"), {"f": fp})).first())
                assert row["status"] == "suppressed" and row["occurrences"] == 5
                # A new subject matched by an exception is stored suppressed from the start.
                c6 = _cand(kind="syn_scan", exporter=exporter, subject=(subject + "x",), src=subject, host=subject)
                det.apply_exceptions([c6], [rule], t)
                await det.upsert_findings(db, [c6], t)
                await db.commit()
                assert (await db.execute(text("SELECT status FROM netflow_findings WHERE fingerprint = :f"),
                                         {"f": c6.fingerprint})).scalar() == "suppressed"
                await db.execute(text("DELETE FROM netflow_findings WHERE fingerprint = ANY(:f)"), {"f": [fp, c6.fingerprint]})
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── Live API (own uvicorn of this tree) ──────────────────────────────────────

LIVE = os.environ.get("NETFLOW_TEST_API")
TOKEN = os.environ.get("NETFLOW_TEST_TOKEN")
live = pytest.mark.skipif(not (LIVE and TOKEN), reason="set NETFLOW_TEST_API and NETFLOW_TEST_TOKEN")


def _h(token: str | None = None) -> dict:
    return {"Authorization": f"Bearer {token or TOKEN}"}


def _get(path, token=None, **params):
    return requests.get(f"{LIVE}/api/v1/netflow{path}", params=params, headers=_h(token), timeout=120)


@live
def test_live_findings_contract_and_honest_detector_status():
    r = _get("/findings", hours=24, envelope=1, limit=5)
    assert r.status_code == 200
    body = r.json()
    meta, data = body["meta"], body["data"]
    assert meta["window"]["source"] == "pg"
    for key in ("items", "counts", "detectors", "engine", "complete", "total"):
        assert key in data
    for key in ("open", "acknowledged", "suppressed", "resolved", "by_severity", "by_severity_open"):
        assert key in data["counts"]
    # by_severity counts active (open + acknowledged) findings, by_severity_open open ones.
    assert sum(data["counts"]["by_severity_open"].values()) <= data["counts"]["open"]
    assert sum(data["counts"]["by_severity"].values()) <= data["counts"]["open"] + data["counts"]["acknowledged"]
    assert {d["kind"] for d in data["detectors"]} == set(det.KINDS)
    for d in data["detectors"]:
        assert d["status"] in det.DETECTOR_STATES
        if d["status"] != "ok":
            assert d["reason"]
    if data["complete"]:
        assert all(d["status"] in ("ok", "disabled") for d in data["detectors"])
        assert data["engine"]["status"] == "running"
    assert meta["truncated"]["more"] == (data["total"] > len(data["items"]))
    for it in data["items"]:
        ev = it["evidence"]
        assert ev["window"]["from"] < ev["window"]["to"] and isinstance(ev["filter"], dict)
        assert it["exporter"] and ev["filter"].get("exporter") == it["exporter"]
    # The detector filter narrows the counts but the picker keeps every detector's count.
    one = _get("/findings", hours=24, kind="syn_scan", limit=1).json()
    assert set(one["counts"]["by_kind"]) <= {"syn_scan"}
    assert one["counts"]["by_kind_all"] == data["counts"]["by_kind_all"]
    assert all(i["kind"] == "syn_scan" for i in one["items"])
    # Legacy consumers keep the plain shape.
    plain = _get("/findings", hours=1, limit=1).json()
    assert "items" in plain and "meta" not in plain


@live
def test_live_adhoc_run_and_legacy_anomalies():
    run = _get("/findings/run", hours=1, limit=1, envelope=1).json()
    data = run["data"]
    assert data["persisted"] is False and {d["kind"] for d in data["detectors"]} == set(det.KINDS)
    for d in data["detectors"]:
        assert d["returned"] <= max(1, d["match_count"])
        assert d["truncated"] == (d["match_count"] > d["returned"]) or d["kind"] in ("volume_spike", "icmp_flood", "icmp_flood_target")
    legacy = _get("/anomalies", hours=1).json()
    for key in ("total", "findings", "generated_at", "detectors", "complete"):
        assert key in legacy
    assert legacy["total"] >= len(legacy["findings"])                     # X-34: never the capped count
    # Validation: unknown detector -> 400 with a string detail; a raw window over 31 days -> 400.
    bad = _get("/findings/run", hours=1, kind="beacon")
    assert bad.status_code == 400 and isinstance(bad.json()["detail"], str)
    assert _get("/findings/run", hours=24 * 40).status_code == 400


@live
def test_live_finding_detail_timeline_and_note():
    items = _get("/findings", hours=24, status="all", limit=1).json()["items"]
    if not items:
        pytest.skip("no persisted findings on this API yet")
    fid = items[0]["id"]
    one = _get(f"/findings/{fid}").json()
    assert one["id"] == fid and one["detector"]["kind"] == one["kind"] and "permissions" in one
    tl = _get(f"/findings/{fid}/timeline", envelope=1).json()["data"]
    assert tl["points"] and tl["metric"]["key"] in tl["points"][0]
    note = f"b5 live test {uuid.uuid4().hex[:6]}"
    r = requests.post(f"{LIVE}/api/v1/netflow/findings/{fid}/actions", json={"action": "note", "note": note},
                      headers=_h(), timeout=30)
    assert r.status_code == 200 and r.json()["notes"][-1]["text"] == note


@live
def test_live_permissions():
    ro = os.environ.get("NETFLOW_TEST_TOKEN_RO")
    viewer = os.environ.get("NETFLOW_TEST_TOKEN_VIEWER")
    if not ro or not viewer:
        pytest.skip("set NETFLOW_TEST_TOKEN_RO and NETFLOW_TEST_TOKEN_VIEWER")
    items = _get("/findings", hours=24, status="all", limit=1).json()["items"]
    if not items:
        pytest.skip("no persisted findings on this API yet")
    fid = items[0]["id"]
    post = lambda tok, body: requests.post(f"{LIVE}/api/v1/netflow/findings/{fid}/actions", json=body,  # noqa: E731
                                           headers=_h(tok), timeout=30)
    assert post(ro, {"action": "note", "note": "x"}).status_code == 403
    assert post(viewer, {"action": "suppress", "reason": "x", "hours": 1}).status_code == 403
    assert _get("/findings", ro, hours=1, limit=1).status_code == 200
    put = requests.put(f"{LIVE}/api/v1/netflow/detectors", json={"quiet_minutes": 30}, headers=_h(viewer), timeout=30)
    assert put.status_code == 403


@live
def test_live_exception_roundtrip_is_audited_and_reversible():
    body = {"kind": "icmp_sweep", "src_cidr": "10.253.99.0/24", "reason": "b5 live test", "dst_port": 0}
    r = requests.post(f"{LIVE}/api/v1/netflow/detector-exceptions", json=body, headers=_h(), timeout=30)
    assert r.status_code == 201, r.text
    x = r.json()
    assert x["src_cidr"] == "10.253.99.0/24" and x["dst_port"] == 0 and x["kind_label"] == "ICMP sweep"
    listed = _get("/detector-exceptions").json()["items"]
    assert any(i["id"] == x["id"] for i in listed)
    d = requests.delete(f"{LIVE}/api/v1/netflow/detector-exceptions/{x['id']}", headers=_h(), timeout=30)
    assert d.status_code == 200 and d.json()["deleted"] == x["id"]
    bad = requests.put(f"{LIVE}/api/v1/netflow/detectors", json={"detectors": {"icmp_flood": {"min_pps": -1}}},
                       headers=_h(), timeout=30)
    assert bad.status_code == 400 and "min_pps" in bad.json()["detail"]
