"""Certificate-expiry and response-time alert rules (alerting plan G-4, migrate-199).

Pure parts (conditions, lifecycle steps, readers, sentences) run without a
database; the evaluator lifecycle, the rule API validator and the service
status path run on a disposable PostgreSQL (pg_fixture). Notifications are
captured by replacing host_alert_service.dispatch_to_channels; ClickHouse is
replaced by the evaluator's response_fetcher hook.
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import host_alert_service
from app.services import service_metric_alert_service as sma
from app.services import service_metric_phrasing as sp

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
CHANNEL = "00000000-0000-0000-0000-00000000c0de"


# ── Conditions and the lifecycle step ───────────────────────────────────────

def cond(metric="service_tls_days", op="<", thr=30.0, reset=None):
    return {"metric": metric, "operator": op, "threshold": thr, "reset_threshold": reset}


def test_clear_level_defaults_and_explicit():
    assert sma.clear_level(cond()) == 31.0                       # 1 day on the healthy side
    assert sma.clear_level(cond("f5_cert_days", "<=", 7)) == 8.0
    assert sma.clear_level(cond("service_response_ms", ">", 1500)) == 1350.0   # 10 %
    assert sma.clear_level(cond("service_response_ms", ">", 1500, reset=1000)) == 1000.0
    assert sma.clear_level(cond(reset=45)) == 45.0
    assert sma.clear_hold_seconds(None) == 300 and sma.clear_hold_seconds(0) == 0
    assert sma.response_window_s(0) == 300 and sma.response_window_s(600) == 600


def step(value, *, open_=False, b=None, c=None, hold=0, clear=300, now=T0, condition=None):
    return sma.decide(value=value, condition=condition or cond(), open_incident=open_,
                      breach_since=b, clear_since=c, hold_s=hold, clear_hold_s=clear, now=now)


def test_breach_hold_then_raise():
    d = step(12, hold=600)
    assert (d.action, d.breach_since) == ("hold", T0)
    d = step(12, b=T0, hold=600, now=T0 + timedelta(seconds=599))
    assert d.action == "hold"
    assert step(12, b=T0, hold=600, now=T0 + timedelta(seconds=600)).action == "raise"
    assert step(12).action == "raise"                            # no hold
    d = step(45, b=T0, hold=600)                                 # healthy again: hold restarts
    assert (d.action, d.breach_since) == ("none", None)


def test_clear_level_and_stable_recovery_hold():
    # 30.5 is past the trigger but not past the clear level (31): still open.
    assert step(30.5, open_=True).action == "none"
    d = step(31, open_=True)
    assert (d.action, d.clear_since) == ("hold", T0)
    assert step(31, open_=True, c=T0, now=T0 + timedelta(seconds=299)).action == "hold"
    assert step(364, open_=True, c=T0, now=T0 + timedelta(seconds=300)).action == "resolve"
    assert step(364, open_=True, clear=0).action == "resolve"
    d = step(20, open_=True, c=T0)                               # breaching again: clear restarts
    assert (d.action, d.clear_since) == ("none", None)


def test_missing_data_never_resolves_or_counts():
    d = step(None, open_=True, c=T0 - timedelta(hours=2), clear=0)
    assert (d.action, d.breach_since, d.clear_since) == ("none", None, None)
    d = step(None, b=T0 - timedelta(hours=2))
    assert (d.action, d.breach_since) == ("none", None)


# ── Readers ─────────────────────────────────────────────────────────────────

def test_tls_reading_uses_expiry_and_freshness():
    check = {"check_interval": 60, "last_check_at": T0 - timedelta(minutes=5),
             "tls_expiry_date": T0 + timedelta(days=12, hours=6), "tls_days_remaining": 99}
    assert sma.tls_reading(check, T0) == (12.0, T0 + timedelta(days=12, hours=6))
    assert sma.tls_reading({**check, "tls_expiry_date": None}, T0) == (99.0, None)
    assert sma.tls_reading({**check, "tls_expiry_date": T0 - timedelta(hours=1)}, T0)[0] == -1.0
    # The latest run is older than 3 intervals (at least 15 minutes): no reading.
    stale = {**check, "last_check_at": T0 - timedelta(minutes=16)}
    assert sma.tls_reading(stale, T0) == (None, None)
    hourly = {**stale, "check_interval": 3600}
    assert sma.tls_reading(hourly, T0)[0] == 12.0
    # A failed run stores no certificate.
    assert sma.tls_reading({**check, "tls_expiry_date": None, "tls_days_remaining": None}, T0) == (None, None)


def test_response_value_average_latest_run_and_missing():
    check = {"check_interval": 600}
    assert sma.response_value({"avg": 2400.0, "count": 5, "last": 1.0, "last_at": T0}, check, T0, 300) == (2400.0, 300)
    # No run in the window, but the newest passing run is within two intervals.
    latest = {"avg": None, "count": 0, "last": 1800.0, "last_at": T0 - timedelta(minutes=15)}
    assert sma.response_value(latest, check, T0, 300) == (1800.0, None)
    old = {**latest, "last_at": T0 - timedelta(minutes=21)}
    assert sma.response_value(old, check, T0, 300) == (None, None)
    assert sma.response_value(None, check, T0, 300) == (None, None)


class FakeCH:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return NS(result_rows=self.rows)


def test_fetch_response_stats_reads_passing_runs_only():
    ch = FakeCH([("c1", 2400.0, 5, 2500.0, T0.replace(tzinfo=None)), ("c2", float("nan"), 0, 900.0, T0)])
    out = sma.fetch_response_stats({"c1": 60, "c2": 900}, 300, T0, client=ch)
    sql, params = ch.calls[0]
    assert "is_up = 1" in sql and "service_metrics" in sql
    assert params["ids"] == ["c1", "c2"]
    assert params["w_since"] == "2026-11-19 11:55:00"
    assert params["since"] == "2026-11-19 11:30:00"              # 2 x the longest interval
    assert out["c1"] == {"avg": 2400.0, "count": 5, "last": 2500.0, "last_at": T0}
    assert out["c2"]["avg"] is None and out["c2"]["count"] == 0
    assert sma.fetch_response_stats({}, 300, T0, client=ch) == {}


def test_f5_reading_epoch_and_staleness():
    exp = T0 + timedelta(days=12, hours=1)
    cert = {"epoch": exp.timestamp(), "updated_at": T0 - timedelta(hours=1)}
    assert sma.f5_reading(cert, T0) == (12.0, exp)
    assert sma.f5_reading({**cert, "updated_at": T0 - timedelta(hours=4)}, T0) == (None, None)
    assert sma.f5_reading({**cert, "epoch": 0}, T0) == (None, None)


def test_service_rule_scope():
    check = {"id": "c1", "group_id": "g1", "device_id": "d1", "tags": ["web"]}
    rule = NS(service_check_id=None, service_check_group_id=None, device_id=None, scope_tag=None)
    assert sma.service_rule_covers(rule, check)
    assert not sma.service_rule_covers(NS(**{**vars(rule), "service_check_id": "c2"}), check)
    assert sma.service_rule_covers(NS(**{**vars(rule), "service_check_group_id": "g1"}), check)
    assert not sma.service_rule_covers(NS(**{**vars(rule), "service_check_group_id": "g2"}), check)
    assert not sma.service_rule_covers(NS(**{**vars(rule), "device_id": "d2"}), check)
    assert sma.service_rule_covers(NS(**{**vars(rule), "scope_tag": "WEB"}), check)
    assert not sma.service_rule_covers(NS(**{**vars(rule), "scope_tag": "db"}), check)


# ── Sentences ───────────────────────────────────────────────────────────────

def test_sentences_from_the_brief():
    on = datetime(2026, 12, 1, 8, tzinfo=timezone.utc)
    assert sp.certificate_sentence(cert="shop.example.com", asset="Service check Shop HTTPS", days=12,
                                   expires_on=on, threshold=30) == (
        "Certificate for shop.example.com (Service check Shop HTTPS) expires in 12 days "
        "(on 2026-12-01; limit 30 days)")
    assert sp.response_sentence(asset="Service check Shop HTTPS", value=2400, threshold=1500,
                                window_s=300) == (
        "Response time of Service check Shop HTTPS is 2.4 s (limit 1.5 s, 5-minute average)")
    assert sp.certificate_sentence(cert="shop.example.com", asset="Service check Shop HTTPS", days=364,
                                   expires_on=datetime(2027, 11, 18, tzinfo=timezone.utc), threshold=30,
                                   recovery=True, renewed=True) == (
        "Certificate for shop.example.com (Service check Shop HTTPS) renewed: now expires in 364 days "
        "(on 2027-11-18)")
    assert sp.certificate_sentence(cert="/Common/shop.crt", asset="Load balancer f5-01 (10.0.0.9)", days=-2,
                                   expires_on=datetime(2026, 11, 17, tzinfo=timezone.utc), threshold=7,
                                   joiner="on") == (
        "Certificate /Common/shop.crt on Load balancer f5-01 (10.0.0.9) expired 2 days ago "
        "(on 2026-11-17; limit 7 days)")
    assert sp.certificate_sentence(cert="a", asset="b", days=0, expires_on=None, threshold=1) == \
        "Certificate for a (b) expires today (limit 1 day)"
    assert sp.response_sentence(asset="X", value=800, threshold=1500, window_s=600, recovery=True) == \
        "Response time of X is back to 800 ms (limit 1.5 s, 10-minute average)"
    assert sp.ms_text(12345) == "12 s" and sp.ms_text(1000) == "1 s"
    assert sp.certificate_name("CN=shop.example.com,O=Shop", "x") == "shop.example.com"
    assert sp.certificate_name("/C=US/CN=api.example.com", "x") == "api.example.com"
    assert sp.certificate_name("", "fallback.example") == "fallback.example"


# ── Evaluator on PostgreSQL ─────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def dispatch(db, channel_ids, ctx):
        calls.append((list(channel_ids), ctx))
        return len(channel_ids)

    monkeypatch.setattr(host_alert_service, "dispatch_to_channels", dispatch)
    return calls


def add_check(db, name="Shop HTTPS", *, expiry=None, last=T0, interval=60, device=None, subject=None,
              target="shop.example.com"):
    return str(db.scalar(
        "INSERT INTO service_checks (name, check_type, target_host, target_url, check_interval, status, "
        "last_check_at, tls_expiry_date, tls_subject, device_id) VALUES ($1, 'http', $2, $3, $4, 'up', $5, $6, $7, $8) "
        "RETURNING id", name, target, f"https://{target}/", interval, last, expiry,
        subject if subject is not None else f"CN={target}", device))


def add_rule(db, metric, op, thr, **cols):
    cols.setdefault("notify_channels", [CHANNEL])
    cols.setdefault("recovery_alert", True)
    names = "".join(f", {c}" for c in cols)
    vals = "".join(f", ${i + 5}" + ("::jsonb" if c == "notify_channels" else "")
                   for i, c in enumerate(cols))
    return str(db.scalar(
        f"INSERT INTO alert_rules (name, metric, operator, threshold, severity{names}) "
        f"VALUES ($1, $2, $3, $4, 'warning'{vals}) RETURNING id",
        f"{metric} rule", metric, op, thr, *cols.values()))


def alerts(db, rule):
    return db.query("SELECT status, message, metadata, service_check_id::text, device_id::text, priority "
                    "FROM alerts WHERE rule_id = $1 ORDER BY triggered_at", rule)


async def run(db, now, **kw):
    async with session(db) as s:
        return await sma.evaluate_service_metric_rules(s, now=now, **kw)


@pytest.mark.asyncio
async def test_certificate_lifecycle_hold_raise_renew_recover(pg_db, sent):
    db = pg_db
    check = add_check(db, expiry=T0 + timedelta(days=12, hours=2))
    rule = add_rule(db, "service_tls_days", "<", 30, service_check_id=check, min_duration=600)

    assert (await run(db, T0))["raised"] == 0                       # hold starts
    assert db.scalar("SELECT breach_since FROM service_metric_alert_state WHERE rule_id = $1", rule) == T0
    db.execute("UPDATE service_checks SET last_check_at = $1", T0 + timedelta(seconds=600))
    out = await run(db, T0 + timedelta(seconds=600))
    assert out["raised"] == 1
    [(status, message, meta, scid, _dev, priority)] = alerts(db, rule)
    assert status == "active" and scid == check and priority == "P3"
    assert message == ("Certificate for shop.example.com (Service check Shop HTTPS) expires in 12 days "
                       "(on 2026-12-01; limit 30 days).")
    assert meta["notified"] is True and meta["service_metric_key"] == f"check:{check}"
    [(channels, ctx)] = sent
    assert channels == [CHANNEL] and ctx["delivery_event"] == "trigger" and ctx["alert_id"]
    assert ctx["body"] == message and ctx["groupable"] is True
    assert "Service check Shop HTTPS" in ctx["subject"]

    # Still breaching: one incident, no second send.
    db.execute("UPDATE service_checks SET last_check_at = $1", T0 + timedelta(seconds=660))
    await run(db, T0 + timedelta(seconds=660))
    assert len(alerts(db, rule)) == 1 and len(sent) == 1

    # The check stops returning a certificate (failed run / stale): nothing resolves.
    db.execute("UPDATE service_checks SET tls_expiry_date = NULL, last_check_at = $1", T0 + timedelta(seconds=720))
    await run(db, T0 + timedelta(seconds=720))
    assert alerts(db, rule)[0][0] == "active" and alerts(db, rule)[0][2]["data_state"] == "missing"

    # Renewed: clear level held for the default 300 s, then one recovery.
    t = T0 + timedelta(seconds=780)
    db.execute("UPDATE service_checks SET tls_expiry_date = $1, last_check_at = $2",
               t + timedelta(days=364, hours=1), t)
    await run(db, t)
    assert alerts(db, rule)[0][0] == "active"
    db.execute("UPDATE service_checks SET last_check_at = $1", t + timedelta(seconds=300))
    out = await run(db, t + timedelta(seconds=300))
    assert out["resolved"] == 1
    [(status, _m, meta, *_)] = alerts(db, rule)
    assert status == "resolved" and meta["renewed"] is True
    _, rctx = sent[-1]
    assert rctx["delivery_event"] == "recovery" and rctx["is_recovery"] is True
    assert rctx["body"].startswith("Certificate for shop.example.com (Service check Shop HTTPS) renewed: "
                                   "now expires in 364 days (on 2027-11-18).")
    assert "Active for" in rctx["body"]


@pytest.mark.asyncio
async def test_maintenance_window_and_unnotified_trigger(pg_db, sent):
    db = pg_db
    check = add_check(db, expiry=T0 + timedelta(days=3))
    quiet = add_check(db, "Quiet HTTPS", expiry=T0 + timedelta(days=3), target="quiet.example.com")
    db.execute("INSERT INTO service_check_maintenance (scope_type, scope_check_id, starts_at, ends_at) "
               "VALUES ('check', $1, $2, $3)", check, T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    rule = add_rule(db, "service_tls_days", "<", 7, recovery_alert=True, notify_channels=[])
    await run(db, T0)
    rows = alerts(db, rule)
    assert [r[3] for r in rows] == [quiet]                         # the check in maintenance is skipped
    assert rows[0][2]["notified"] is False and sent == []
    # No trigger went out, so no recovery goes out either.
    db.execute("UPDATE service_checks SET tls_expiry_date = $1 WHERE id = $2", T0 + timedelta(days=300), quiet)
    db.execute("UPDATE alert_rules SET clear_duration = 0 WHERE id = $1", rule)
    await run(db, T0)
    assert alerts(db, rule)[0][0] == "resolved" and sent == []


@pytest.mark.asyncio
async def test_response_time_average_clear_level(pg_db, sent):
    db = pg_db
    check = add_check(db)
    other = add_check(db, "Other", target="other.example.com")
    rule = add_rule(db, "service_response_ms", ">", 1500, service_check_id=check, clear_duration=0)
    seen = []

    def fetcher(value):
        def fetch(intervals, window, now):
            seen.append((dict(intervals), window))
            return {check: {"avg": value, "count": 5, "last": value, "last_at": now}}
        return fetch

    await run(db, T0, response_fetcher=fetcher(2400.0))
    assert seen == [({check: 60}, 300)]                            # only covered checks, 5-minute window
    [(status, message, meta, *_)] = alerts(db, rule)
    assert message == "Response time of Service check Shop HTTPS is 2.4 s (limit 1.5 s, 5-minute average)."
    assert meta["window_s"] == 300
    await run(db, T0 + timedelta(minutes=1), response_fetcher=fetcher(1400.0))   # above the 1350 clear level
    assert alerts(db, rule)[0][0] == "active"
    await run(db, T0 + timedelta(minutes=2), response_fetcher=fetcher(None))     # no passing runs
    assert alerts(db, rule)[0][0] == "active"

    def broken(*a):
        raise RuntimeError("clickhouse down")
    await run(db, T0 + timedelta(minutes=3), response_fetcher=broken)
    assert alerts(db, rule)[0][0] == "active"
    await run(db, T0 + timedelta(minutes=4), response_fetcher=fetcher(800.0))
    assert alerts(db, rule)[0][0] == "resolved"
    assert sent[-1][1]["body"].startswith(
        "Response time of Service check Shop HTTPS is back to 800 ms (limit 1.5 s, 5-minute average).")
    assert other not in {r[3] for r in alerts(db, rule)}


def add_device(db, name="f5-01", ip="192.0.2.9", status="up"):
    return str(db.scalar("INSERT INTO devices (hostname, ip_address, status, device_type) "
                         "VALUES ($1, $2::inet, $3, 'load_balancer') RETURNING id", name, ip, status))


def add_cert(db, device, instance, label, expiry, updated=T0):
    db.execute("INSERT INTO device_template_values (device_id, group_key, metric_key, instance, series_key, "
               "label, value_num, updated_at) VALUES ($1, 'certificates', 'f5_cert_expiry_epoch', $2, $3, $4, $5, $6)",
               device, instance, f"tpl_f5_cert_expiry_epoch_{instance}", label, expiry.timestamp(), updated)


@pytest.mark.asyncio
async def test_f5_certificates_one_incident_per_certificate(pg_db, sent):
    db = pg_db
    f5 = add_device(db)
    idle = add_device(db, "f5-02", "192.0.2.10", status="maintenance")
    add_cert(db, f5, "1.2.3", "/Common/shop.crt", T0 + timedelta(days=5, hours=1))
    add_cert(db, f5, "4.5.6", "/Common/api.crt", T0 + timedelta(days=2, hours=1))
    add_cert(db, f5, "7.8.9", "/Common/ok.crt", T0 + timedelta(days=300))
    add_cert(db, idle, "1.2.3", "/Common/shop.crt", T0 + timedelta(days=1))
    rule = add_rule(db, "f5_cert_days", "<", 7)
    await run(db, T0)
    rows = alerts(db, rule)
    assert sorted(r[1] for r in rows) == [
        "Certificate /Common/api.crt on Load balancer f5-01 (192.0.2.9) expires in 2 days (on 2026-11-21; limit 7 days).",
        "Certificate /Common/shop.crt on Load balancer f5-01 (192.0.2.9) expires in 5 days (on 2026-11-24; limit 7 days).",
    ]
    assert {r[4] for r in rows} == {f5} and {r[2]["dimension"] for r in rows} == {"1.2.3", "4.5.6"}
    await run(db, T0 + timedelta(minutes=1))
    assert len(alerts(db, rule)) == 2 and len(sent) == 2
    # Excluding the rule for this device stops evaluation there (open incidents stay as they are).
    db.execute("INSERT INTO alert_rule_exclusions (rule_id, asset_kind, device_id, reason) VALUES ($1, 'device', $2, 'lab')", rule, f5)
    db.execute("UPDATE device_template_values SET updated_at = $1", T0 + timedelta(minutes=2))
    db.execute("UPDATE device_template_values SET value_num = $1 WHERE instance = '1.2.3'",
               (T0 + timedelta(days=400)).timestamp())
    db.execute("UPDATE alert_rules SET clear_duration = 0 WHERE id = $1", rule)
    await run(db, T0 + timedelta(minutes=2))
    assert all(r[0] == "active" for r in alerts(db, rule))


@pytest.mark.asyncio
async def test_service_status_path_never_fires_level_rules(pg_db, sent):
    from app.api.v1.alert_engine import ServiceStatusChangeEvent, evaluate_service_status_change
    db = pg_db
    check = add_check(db)
    level = add_rule(db, "service_tls_days", "<", 30, service_check_id=check)
    status = add_rule(db, "service_status", "==", 0, service_check_id=check)
    async with session(db) as s:
        await evaluate_service_status_change(ServiceStatusChangeEvent(
            service_check_id=check, check_name="Shop HTTPS", check_type="http",
            old_status="up", new_status="down", target="https://shop.example.com/"), db=s)
        await s.commit()
    assert alerts(db, level) == [] and len(alerts(db, status)) == 1


def test_migration_accepts_the_metrics_and_creates_state(pg_db):
    for metric in sorted(sma.METRICS):
        add_rule(pg_db, metric, "<", 5)
    assert pg_db.scalar("SELECT count(*) FROM alert_rules WHERE metric = ANY($1::text[])",
                        sorted(sma.METRICS)) == 3
    from pg_fixture import PsqlError
    with pytest.raises(PsqlError):
        pg_db.psql("INSERT INTO alert_rules (name, metric, operator, threshold) VALUES ('x', 'nope', '<', 1)")
    assert pg_db.scalar("SELECT has_table_privilege('zenplus', 'service_metric_alert_state', 'INSERT')")


# ── Rule API validator ──────────────────────────────────────────────────────

MANAGER = ["alerts.view", "alerts.manage", "devices.view", "devices.manage", "services.view", "services.manage"]


def test_rule_api_validates_the_new_metrics(pg_db):
    db = pg_db
    check = add_check(db)
    f5 = add_device(db)
    group = str(db.scalar("INSERT INTO device_groups (name) VALUES ('core') RETURNING id"))
    base = {"name": "Cert", "metric": "service_tls_days", "operator": "<", "threshold": 30,
            "severity": "warning", "notify_channels": []}
    with api_client(db, role="admin", persist_user=True, username="admin-1") as c:
        r = c.post("/api/v1/alert-rules", json={**base, "service_check_id": check})
        assert r.status_code == 201, r.text
        assert r.json()["metric"] == "service_tls_days"
        ok = c.post("/api/v1/alert-rules", json={**base, "conditions": [
            {"metric": "service_tls_days", "operator": "<", "threshold": 30, "reset_threshold": 45}]})
        assert ok.status_code == 201, ok.text
        assert c.post("/api/v1/alert-rules", json={**base, "metric": "f5_cert_days", "device_id": f5,
                                                  "group_id": group}).status_code == 201
        assert c.post("/api/v1/alert-rules", json={**base, "metric": "service_response_ms", "operator": ">",
                                                  "threshold": 1500, "service_check_id": check}).status_code == 201
        bad = [
            {**base, "operator": "=="},
            {**base, "conditions": [{"metric": "service_tls_days", "operator": "<", "threshold": 30},
                                    {"metric": "rtt", "operator": ">", "threshold": 5}]},
            {**base, "conditions": [{"metric": "service_tls_days", "operator": "<", "threshold": 30,
                                     "reset_threshold": 20}]},
            {**base, "metric": "service_response_ms", "operator": ">", "threshold": 0},
            {**base, "group_id": group},
            {**base, "metric": "f5_cert_days", "service_check_id": check},
            {**base, "metric": "service_tls_days_x"},
        ]
        for body in bad:
            assert c.post("/api/v1/alert-rules", json=body).status_code == 422, body
        rid = r.json()["id"]
        assert c.put(f"/api/v1/alert-rules/{rid}", json={"operator": "!="}).status_code == 422
        assert c.put(f"/api/v1/alert-rules/{rid}", json={"threshold": 14}).status_code == 200
