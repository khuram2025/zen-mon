"""Recommended default alert rules (alerting audit plan step 2.1)."""

from __future__ import annotations

from app.services import recommended_rules as rec


def _rule(**kw):
    base = dict(id="r1", name="Mine", enabled=True, metric="service_status", operator="==",
                threshold=0, trigger_on="down", trap_oid=None)
    base.update(kw)
    return base


def test_catalogue_keys_are_unique_and_complete():
    keys = [it["key"] for it in rec.CATALOGUE]
    assert len(keys) == len(set(keys))
    for it in rec.CATALOGUE:
        assert it["severity"] in ("warning", "critical") and it["priority"] in ("P2", "P3")
        assert it["family"] in ("service", "device", "server", "trap", "syslog")
    from app.api.v1.alert_rules import _CONDITION_METRICS
    import re
    from app.services import rule_conditions as rc
    for it in rec.CATALOGUE:
        if it.get("condition_tree"):
            # A combined recommendation: a valid tree for its subject, stored as the API stores it.
            tree = rc.validate_tree(it["condition_tree"], it["subject_type"])
            assert it["metric"] == rc.mirror(tree, it["subject_type"])["metric"]
            assert not rec.covers(_rule(metric=it["metric"], trigger_on="any"), it)
        elif it["family"] not in ("server", "trap", "syslog"):
            assert re.fullmatch(f"({_CONDITION_METRICS})", it["metric"]), it["metric"]


def test_an_unscoped_enabled_rule_covers_a_recommendation():
    down = rec.find("service_down")
    assert rec.covers(_rule(), down)
    assert rec.covers(_rule(trigger_on="any"), down)          # 'any' includes down
    assert not rec.covers(_rule(enabled=False), down)
    assert not rec.covers(_rule(service_check_id="c1"), down)  # scoped to one check
    assert not rec.covers(_rule(trigger_on="degraded"), down)
    assert rec.covers(_rule(trigger_on="degraded"), rec.find("service_degraded"))


def test_thresholds_need_not_match_but_operators_must():
    cpu = rec.find("device_cpu_critical")
    assert rec.covers(_rule(metric="cpu", operator="gt", threshold=97, trigger_on="any", severity="critical"), cpu)
    # The shipped warning-level "High device CPU" does not cover the critical recommendation.
    assert not rec.covers(_rule(metric="cpu", operator="gt", threshold=90, trigger_on="any", severity="warning"), cpu)
    assert not rec.covers(_rule(metric="cpu", operator="<", threshold=5, trigger_on="any", severity="critical"), cpu)


def test_warning_and_critical_certificate_recommendations_are_separate():
    warn, crit = rec.find("certificate_expiring"), rec.find("certificate_expiring_soon")
    mine = _rule(metric="service_tls_days", operator="<", threshold=21, trigger_on="any", severity="warning")
    assert rec.covers(mine, warn) and not rec.covers(mine, crit)


def test_trap_recommendation_is_covered_only_by_the_same_or_any_trap():
    cold = rec.find("device_cold_start")
    assert rec.covers(_rule(metric="trap", trap_oid=None), cold)
    assert rec.covers(_rule(metric="trap", trap_oid="1.3.6.1.6.3.1.1.5.1"), cold)
    assert not rec.covers(_rule(metric="trap", trap_oid="1.3.6.1.6.3.1.1.5.3"), cold)


def test_interface_quality_recommendations():
    """Alerting plan G-12: error rate > 1% for 10 min and flapping >= 5 changes in 15 min,
    both warning P3, next to (not replacing) the per-poll discard count item."""
    rate, flap = rec.find("interface_error_rate"), rec.find("interface_flapping")
    assert (rate["metric"], rate["operator"], rate["threshold"], rate["min_duration"]) == ("if_error_rate_pct", ">", 1, 600)
    assert (flap["metric"], flap["operator"], flap["threshold"], flap["min_duration"]) == ("if_flaps", ">=", 5, 900)
    assert {(it["severity"], it["priority"]) for it in (rate, flap)} == {("warning", "P3")}
    assert rec.find("interface_discards")["metric"] == "if_discards"
    # The old per-poll error count does not cover the rate; another flapping rule does.
    assert not rec.covers(_rule(metric="if_errors", operator=">", threshold=100, trigger_on="any"), rate)
    assert rec.covers(_rule(metric="if_flaps", operator="gte", threshold=3, trigger_on="any"), flap)
    assert not rec.covers(_rule(metric="if_flaps", operator="gte", threshold=3, trigger_on="any",
                                if_role="uplink"), flap)


def test_device_degraded_is_covered_by_latency_or_loss_on_degraded():
    rules = [_rule(metric="packet_loss", operator="gt", threshold=5, trigger_on="degraded")]
    assert rec._degraded_covered(rules)["id"] == "r1"
    assert rec._degraded_covered([_rule(metric="rtt", trigger_on="down")]) is None


# ── routes against the disposable PostgreSQL (pg_fixture) ──────────────────

import pytest  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.database import get_db  # noqa: E402
from app.core.security import get_current_user  # noqa: E402
from app.main import app  # noqa: E402
from pg_fixture import pg_cluster, pg_db  # noqa: E402,F401
from test_monitoring_settings_policy import add_user  # noqa: E402


@pytest.fixture
def api(pg_db):
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules")
    admin = add_user(pg_db)

    async def session():
        engine = create_async_engine(pg_db.url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                yield db
        finally:
            await engine.dispose()

    async def signed_in():
        return admin

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = signed_in
    try:
        yield SimpleNamespace(client=TestClient(app), db=pg_db)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


def test_accepting_creates_one_rule_and_marks_it_accepted(api):
    items = {it["key"]: it for it in api.client.get("/api/v1/alert-rules/recommended").json()["items"]}
    assert items["service_down"]["status"] == "available"
    r = api.client.post("/api/v1/alert-rules/recommended/service_down/accept", json={})
    assert r.status_code == 201, r.text
    [(name, trig, hold, prio, pack)] = api.db.query(
        "SELECT name, trigger_on, min_duration, priority, pack_key FROM alert_rules")
    assert (name, trig, hold, prio, pack) == ("Service check down", "down", 120, "P2", "default:service_down")
    items = {it["key"]: it for it in api.client.get("/api/v1/alert-rules/recommended").json()["items"]}
    assert items["service_down"]["status"] == "accepted"
    assert api.client.post("/api/v1/alert-rules/recommended/service_down/accept", json={}).status_code == 409


def test_server_recommendations_use_the_host_rule_path(api):
    r = api.client.post("/api/v1/alert-rules/recommended/disk_io_saturated/accept", json={"enabled": False})
    assert r.status_code == 201, r.text
    [(metric, hold, clear, enabled)] = api.db.query(
        "SELECT metric, min_duration, clear_duration, enabled FROM alert_rules")
    assert (metric, hold, clear, enabled) == ("host_disk_util_pct", 900, 900, False)


def test_an_existing_rule_shows_the_recommendation_as_covered(api):
    api.db.execute("INSERT INTO alert_rules (name, metric, operator, threshold, trigger_on, severity)"
                   " VALUES ('Service Down and Recovery', 'service_status', '==', 0, 'down', 'critical')")
    items = {it["key"]: it for it in api.client.get("/api/v1/alert-rules/recommended").json()["items"]}
    assert items["service_down"]["status"] == "covered"
    assert items["service_down"]["covered_by"]["name"] == "Service Down and Recovery"
    assert api.client.post("/api/v1/alert-rules/recommended/nope/accept", json={}).status_code == 404


def test_certificate_recommendations_pass_the_service_metric_validator(api):
    for key in ("certificate_expiring", "certificate_expiring_soon", "f5_certificate_expiring"):
        r = api.client.post(f"/api/v1/alert-rules/recommended/{key}/accept", json={})
        assert r.status_code == 201, (key, r.text)
    items = {it["key"]: it for it in api.client.get("/api/v1/alert-rules/recommended").json()["items"]}
    assert {items[k]["status"] for k in ("certificate_expiring", "certificate_expiring_soon",
                                         "f5_certificate_expiring")} == {"accepted"}


# ── Server depth recommendations (alerting plan G-9/G-10) ──────────────────

def test_server_depth_recommendations_are_warning_p3_on_built_metrics():
    import re
    from app.api.v1.host_alert_rules import _METRIC
    expected = {"swap_high": ("host_swap_pct", "gt", 80, 900),
                "filesystem_full_soon": ("host_fs_days_left", "lt", 7, 3600),
                "clock_skew": ("host_clock_skew_s", "gt", 60, 600),
                "auto_service_stopped": ("host_auto_service_stopped", "gt", 0, 300)}
    for key, (metric, op, thr, hold) in expected.items():
        it = rec.find(key)
        assert it["family"] == "server" and (it["severity"], it["priority"]) == ("warning", "P3")
        assert (it["metric"], it["operator"], it["threshold"], it["min_duration"]) == (metric, op, thr, hold)
        assert re.fullmatch(_METRIC, it["metric"]), key
    swap = rec.find("swap_high")
    assert rec.covers(_rule(metric="host_swap_pct", operator=">", threshold=90, trigger_on="any"), swap)
    # A rule that leaves services out (target patterns) is scoped: it does not cover.
    assert not rec.covers(_rule(metric="host_auto_service_stopped", operator="gt", threshold=0,
                                trigger_on="any", target="gupdate*"), rec.find("auto_service_stopped"))


def test_accepting_the_server_depth_recommendations(api):
    for key in ("swap_high", "filesystem_full_soon", "clock_skew", "auto_service_stopped"):
        r = api.client.post(f"/api/v1/alert-rules/recommended/{key}/accept", json={})
        assert r.status_code == 201, (key, r.text)
    rows = {m: (op, float(t), hold) for m, op, t, hold in api.db.query(
        "SELECT metric, operator, threshold, min_duration FROM alert_rules")}
    assert rows == {"host_swap_pct": ("gt", 80.0, 900), "host_fs_days_left": ("lt", 7.0, 3600),
                    "host_clock_skew_s": ("gt", 60.0, 600), "host_auto_service_stopped": ("gt", 0.0, 300)}
