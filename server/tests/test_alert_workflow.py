"""Alert Center workflow semantics (Alerts review 2026-10-01).

Pure-logic coverage for the pieces every endpoint leans on: closure
classification (only genuine recoveries count as "recovered"), notice
detection, snooze validation, response-target deadlines, list filter
building, rule scope description/governance and delivery-log sanitising.
The DB-backed behaviour is exercised end-to-end by
tests/integration/test_alert_center_api.py against a real Postgres.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import alert_delivery_log as dl
from app.services import alert_rule_scope as rs
from app.services import alert_service as svc
from app.services import alert_workflow as wf


# ── closure / notices ──────────────────────────────────────────────────────

@pytest.mark.parametrize("meta,expected", [
    ({"resolved_by_recovery": True}, "recovered"),
    ({"resolved_by": "network_evaluator"}, "recovered"),
    ({}, "recovered"),
    ({"resolved_by": "manual"}, "manual"),
    ({"resolved_by": "snooze"}, "suppressed"),
    ({"resolved_by": "status_transition"}, "superseded"),
    ({"resolved_by": "migration"}, "administrative"),
    ({"is_recovery": True}, "notice"),
    ({"suppressed_by_dependency": True}, "dependency"),
    ({"hold_cancelled": True}, "hold_cancelled"),
    ({"flap_notice": True, "flap_event": "stop"}, "flapping"),
])
def test_closure_reason_separates_recovery_from_human_closure(meta, expected):
    assert wf.closure_reason("resolved", meta) == expected


def test_open_alerts_have_no_closure():
    for status in ("pending", "active", "acknowledged"):
        assert wf.closure_reason(status, {"resolved_by": "manual"}) is None


def test_notice_kind():
    assert wf.notice_kind({"is_recovery": "true"}) == "recovery"
    assert wf.notice_kind({"suppressed_by_dependency": True}) == "dependency"
    assert wf.notice_kind({"hold_cancelled": True}) == "hold_cancelled"
    assert wf.notice_kind({"flap_notice": True, "flap_event": "start"}) == "flapping"
    assert wf.notice_kind({"is_recovery": False}) is None
    assert wf.notice_kind(None) is None


def test_closure_sql_mirrors_python_labels():
    sql = wf.closure_reason_sql("a")
    for label in ("notice", "dependency", "hold_cancelled", "flapping", "manual", "suppressed",
                  "superseded", "administrative", "recovered"):
        assert f"'{label}'" in sql


# ── snooze validation (A08) ────────────────────────────────────────────────

@pytest.mark.parametrize("minutes", [-5, 0, 1, 4, 43201, 10**6])
def test_snooze_rejects_out_of_range_minutes(minutes):
    with pytest.raises(wf.WorkflowError) as exc:
        wf.validate_snooze(minutes, None, can_forever=True)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("minutes", [5, 60, 1440, 43200])
def test_snooze_accepts_bounded_minutes(minutes):
    wf.validate_snooze(minutes, None, can_forever=False)


def test_indefinite_snooze_needs_permission_and_reason():
    with pytest.raises(wf.WorkflowError) as exc:
        wf.validate_snooze(None, "planned decommission", can_forever=False)
    assert exc.value.status_code == 403
    with pytest.raises(wf.WorkflowError) as exc:
        wf.validate_snooze(None, "  ", can_forever=True)
    assert exc.value.status_code == 422
    wf.validate_snooze(None, "planned decommission", can_forever=True)


def test_snooze_rejects_booleans_masquerading_as_ints():
    with pytest.raises(wf.WorkflowError):
        wf.validate_snooze(True, None, can_forever=True)


def test_device_dedupe_matches_evaluator_identity():
    assert wf.device_dedupe("r1", None) == "rule:r1"
    assert wf.device_dedupe("r1", "") == "rule:r1"
    assert wf.device_dedupe("r1", "7") == "rule:r1:if:7"


def test_silence_match_sql_builds_the_same_identity():
    sql = wf.silence_match_sql("a", "s")
    assert "'rule' || chr(58)" in sql and "'if' || chr(58)" in sql
    assert "s.server_id = a.server_id" in sql


# ── response targets ───────────────────────────────────────────────────────

def test_ack_due_only_for_active_unsuppressed():
    t0 = datetime.now(timezone.utc) - timedelta(minutes=20)
    targets = {"critical": 15, "warning": 60, "info": None}
    due, overdue = wf.ack_due("active", "critical", t0, targets, suppressed=False)
    assert overdue and due == t0 + timedelta(minutes=15)
    assert wf.ack_due("active", "warning", t0, targets, False)[1] is False
    assert wf.ack_due("active", "info", t0, targets, False) == (None, False)
    assert wf.ack_due("acknowledged", "critical", t0, targets, False) == (None, False)
    assert wf.ack_due("active", "critical", t0, targets, True) == (None, False)


def test_response_targets_cleaning():
    """Targets are per priority; settings saved by earlier releases (severity
    keys) keep their meaning through the priority derivation."""
    assert wf._clean_targets({"critical": 5, "warning": 0, "info": "x"}) == {
        "P1": 5, "P2": 5, "P3": None, "P4": None}
    assert wf._clean_targets('{"warning": 30}')["P3"] == 30
    assert wf._clean_targets({"P3": 45, "warning": 30})["P3"] == 45     # a P-key wins
    assert wf._clean_targets(None) == wf.DEFAULT_RESPONSE_TARGETS == {
        "P1": 5, "P2": 15, "P3": 60, "P4": None}
    assert wf.legacy_target_view({"P1": 5, "P2": 15, "P3": 60, "P4": None}) == {
        "P1": 5, "P2": 15, "P3": 60, "P4": None, "critical": 15, "warning": 60, "info": None}


def test_ack_due_uses_the_alert_priority():
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    targets = {"P1": 5, "P2": 15, "P3": 60, "P4": None}
    # A critical-severity alert raised as P1 is overdue after 5 minutes ...
    due, overdue = wf.ack_due("active", "critical", t0, targets, False, priority="P1")
    assert overdue and due == t0 + timedelta(minutes=5)
    # ... the same severity at the derived P2 is not yet.
    assert wf.ack_due("active", "critical", t0, targets, False)[1] is False
    assert wf.ack_due("active", "info", t0, targets, False, priority="P4") == (None, False)


# ── filters (A01/A12/A16) ──────────────────────────────────────────────────

def test_views_separate_suppressed_from_active():
    w, _ = svc.AlertFilters(view="active").where()
    assert any("status = 'active'" in c and "NOT EXISTS (SELECT 1 FROM alert_silences" in c for c in w)
    w, _ = svc.AlertFilters(view="suppressed").where()
    assert any("IN ('active', 'acknowledged')" in c and "EXISTS (SELECT 1 FROM alert_silences" in c for c in w)


def test_view_defaults_to_incidents_but_legacy_status_keeps_everything():
    w, _ = svc.AlertFilters(view="resolved").where()
    assert any(c.startswith("NOT (COALESCE(a.metadata->>'is_recovery'") for c in w)
    w, p = svc.AlertFilters(status="resolved").where()
    assert not any("is_recovery" in c for c in w) and p["status"] == "resolved"


def test_rolling_window_uses_appliance_clock_and_basis():
    w, p = svc.AlertFilters(view="resolved", hours=24, basis="resolved").where()
    assert "a.resolved_at >= NOW() - make_interval(hours => :hours)" in w
    assert p["hours"] == 24


def test_explicit_bounds_win_over_hours():
    t = datetime(2026, 9, 1, tzinfo=timezone.utc)
    w, p = svc.AlertFilters(hours=24, from_time=t, basis="acknowledged").where()
    assert "a.acknowledged_at >= :from_time" in w and "hours" not in p


def test_search_escapes_like_wildcards_and_covers_entities():
    w, p = svc.AlertFilters(search="100%_x").where()
    assert p["q"] == "%100\\%\\_x%"
    clause = next(c for c in w if "ILIKE" in c)
    for col in ("d.hostname", "host(d.ip_address)", "sc.name", "s.hostname", "sen.name", "r.name"):
        assert col in clause


def test_search_by_alert_id_prefix():
    _, p = svc.AlertFilters(search="3FA85F64").where()
    assert p["idp"] == "3fa85f64%"


def test_scope_and_snapshot_filters():
    t = datetime(2026, 9, 30, tzinfo=timezone.utc)
    w, p = svc.AlertFilters(visible_tags=["web"], as_of=t, delivery="failed").where()
    assert p["vis_tags"] == ["web"] and p["as_of"] == t
    assert any("alert_deliveries" in c and "'failed'" in c for c in w)


def test_describe_lists_population_filters():
    f = svc.AlertFilters(severity=["critical"], assignee="me", search="core")
    assert f.describe() == ["Severity: critical", "Assigned to me", "Search: core"]
    assert svc.AlertFilters(view="active").describe() == []


def test_duration_stats():
    assert svc._stats([]) == {"count": 0, "mean_s": None, "p50_s": None, "p95_s": None}
    st = svc._stats([10, 20, 30, 40, -5])
    assert st["count"] == 4 and st["p50_s"] == 25.0 and st["mean_s"] == 25.0


def test_order_is_stable():
    for sort in svc.SORTS:
        assert "a.id" in svc.order_sql(sort)


# ── rule scope & governance (A05/A20/A22) ──────────────────────────────────

def _rule(**kw):
    base = dict(metric="ping_status", device_id=None, group_id=None, device_type=None,
                location=None, scope_tag=None, server_id=None, service_check_id=None,
                service_check_group_id=None, target=None)
    base.update(kw)
    return base


def test_describe_scope_labels():
    assert rs.describe_scope(_rule())["kind"] == "global"
    g = rs.describe_scope(_rule(group_id="g", scope_tag="web"), {"group": "Core"})
    assert g["kind"] == "group" and "Core" in g["label"] and "tagged web" in g["label"]
    assert rs.describe_scope(_rule(device_id="d", target="Gi0/1"), {"device": "sw1"})["kind"] == "interface"
    assert rs.describe_scope(_rule(metric="host_cpu_pct"))["label"] == "All monitored servers"
    assert rs.describe_scope(_rule(metric="service_status"))["kind"] == "global"


@pytest.mark.asyncio
async def test_scoped_users_cannot_touch_fleet_rules():
    class DB:
        async def execute(self, *a, **k):
            return SimpleNamespace(scalar=lambda: ["web"])
    db = DB()
    assert await rs.rule_in_user_scope(db, _rule(), None) is True
    assert await rs.rule_in_user_scope(db, _rule(), ["web"]) is False
    assert await rs.rule_in_user_scope(db, _rule(group_id="g"), ["web"]) is False
    assert await rs.rule_in_user_scope(db, _rule(scope_tag="Web"), ["web"]) is True
    assert await rs.rule_in_user_scope(db, _rule(device_id="d"), ["web"]) is True
    assert await rs.rule_in_user_scope(db, _rule(device_id="d"), ["mail"]) is False


def test_rule_changes_diff():
    out = rs.rule_changes({"name": "a", "threshold": 1, "updated_at": 1},
                          {"name": "b", "threshold": 1, "updated_at": 2})
    assert out == {"name": ["a", "b"], "updated_at": ["…", "…"]}


# ── delivery log (A24) ─────────────────────────────────────────────────────

def test_sanitize_error_strips_urls_and_secrets():
    msg = dl.sanitize_error(Exception(
        "POST https://hooks.slack.com/services/T0/B0/SECRETTOKEN failed token=abc123"))
    assert "SECRETTOKEN" not in msg and "abc123" not in msg
    assert "https://hooks.slack.com/…" in msg


def test_error_categories():
    assert dl.error_category(dl.DeliveryRejected("HTTP 500")) == "rejected"
    assert dl.error_category(TimeoutError("timed out")) == "timeout"
    assert dl.error_category(ConnectionRefusedError("Connection refused")) == "connection"


def test_ensure_accepted():
    dl.ensure_accepted(SimpleNamespace(status_code=204))
    with pytest.raises(dl.DeliveryRejected):
        dl.ensure_accepted(SimpleNamespace(status_code=401))


def test_delivery_kind_from_ctx():
    assert dl.kind_from_ctx({}) == "trigger"
    assert dl.kind_from_ctx({"is_recovery": True}) == "recovery"
    assert dl.kind_from_ctx({"escalation_level": 2}) == "escalation"
    assert dl.kind_from_ctx({"delivery_kind": "reminder", "is_recovery": True}) == "reminder"
