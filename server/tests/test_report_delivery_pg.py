"""Report delivery on a real PostgreSQL (docs/reports-delivery.md, migrate-166).

* a run is written as ``running`` with its window and link expiry, and
  finished with the email as sent, the attachment, its notes and the
  targets it still owes; a temporary SMTP failure is retried from the stored
  email by the next tick and the run ends ``success``;
* housekeeping: a run left ``running`` by a dead worker is failed, old runs
  are deleted, old attachments and email bodies are cleared, and the
  scheduler heartbeat is readable;
* the schedules and runs API: channel validation, run-now, the paged run
  history, the stored attachment, revoking a link and the status endpoint.

Runs on the disposable cluster of ``pg_fixture`` only, never the appliance
database; report rendering and SMTP are replaced by recorders.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import report_scheduler as rs
from app.services import report_sections as _sections

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

UTC = timezone.utc


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def _company(db, **kw):
    value = {"company_name": "Acme", "timezone": "Asia/Riyadh", "base_url": "https://nms.acme.example", **kw}
    db.execute("INSERT INTO system_settings (key, value) VALUES ('company', $1::jsonb) "
               "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", value)


def _schedule(db, **kw) -> uuid.UUID:
    sid = uuid.uuid4()
    row = dict(name="Weekly SLA", report_type="availability", period="last_7d", fmt="pdf",
               recipients=["ops@example.com", "cto@example.org"])
    row.update(kw)
    db.execute(
        "INSERT INTO report_schedules (id, name, enabled, report_type, period, format, frequency, hour, minute, "
        "timezone, recipients) VALUES ($1, $2, TRUE, $3, $4, $5, 'daily', 8, 0, 'Asia/Riyadh', $6::jsonb)",
        sid, row["name"], row["report_type"], row["period"], row["fmt"], row["recipients"])
    return sid


@pytest.fixture
def fakes(monkeypatch):
    seen = {"individual": [], "fail_once": set()}

    async def build(db, entries, frm, to, filters=None, requester=None, branding=None):
        return [{"id": "availability_kpis", "title": "Availability",
                 "kpis": [{"label": "Availability", "value": "99.9%"}], "charts": [], "tables": [], "notes": []}]

    def individually(gw, recipients, subject, text_body, html_body, attachment=None, inline_images=None, **kw):
        seen["individual"].append(dict(recipients=list(recipients), subject=subject, attachment=attachment))
        bad = [r for r in recipients if r in seen["fail_once"]]
        seen["fail_once"] -= set(bad)
        return [r for r in recipients if r not in bad], [(r, "451 4.7.1 try later") for r in bad]

    async def gateway(db, gateway_id=None):
        return {"host": "smtp.example", "from_email": "reports@acme.example"}

    monkeypatch.setattr(_sections, "build_sections", build)
    monkeypatch.setattr(_sections, "render_html", lambda meta, secs: "<html>report</html>")
    monkeypatch.setattr(_sections, "render_pdf", lambda meta, secs: b"%PDF-1.7 fake")
    monkeypatch.setattr(rs, "_send_individually", individually)
    monkeypatch.setattr(rs, "_resolve_smtp_gateway", gateway)
    return seen


def _sched_dict(db, sid) -> dict:
    async def go():
        async with session(db) as s:
            from sqlalchemy import text
            row = (await s.execute(text("SELECT * FROM report_schedules WHERE id = :id"), {"id": sid})).first()
            return rs._row_to_sched(row)
    return asyncio.run(go())


def test_migration_166_columns_and_period(pg_db):
    cols = {c for (c,) in pg_db.query("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_name = 'report_runs'")}
    assert {"artifact", "failed_targets", "next_retry_at", "email_html", "kind", "window_from"} <= cols
    sid = _schedule(pg_db, period="previous_work_week")
    assert pg_db.scalar("SELECT period FROM report_schedules WHERE id = $1", sid) == "previous_work_week"


def test_a_run_is_stored_and_a_temporary_failure_is_retried(pg_db, fakes, monkeypatch):
    _company(pg_db)
    sid = _schedule(pg_db)
    fakes["fail_once"] = {"cto@example.org"}

    sched = _sched_dict(pg_db, sid)

    async def run():
        async with session(pg_db) as s:
            return await rs.generate_and_deliver(s, sched)

    out = asyncio.run(run())
    assert out["status"] == "partial" and out["delivered"] == ["ops@example.com"] and out["retry_at"]
    row = pg_db.query(
        "SELECT status, kind, expires_at - generated_at, artifact, artifact_name, email_subject, failed_targets, "
        "next_retry_at, window_from < window_to FROM report_runs WHERE id = $1", uuid.UUID(out["run_id"]))[0]
    status, kind, life, artifact, name, subject, failed, next_retry, ordered = row
    assert (status, kind, ordered) == ("partial", "scheduled", True)
    assert life == timedelta(days=30) and bytes(artifact) == b"%PDF-1.7 fake" and name.endswith(".pdf")
    assert subject.startswith("Weekly SLA — Last 7 days")
    failed = failed if isinstance(failed, list) else json.loads(failed)
    assert [t["id"] for t in failed] == ["cto@example.org"] and next_retry is not None

    # The next tick after the delay resends the stored email to the owed address only.
    pg_db.execute("UPDATE report_runs SET next_retry_at = NOW() - INTERVAL '1 minute'")

    async def retry():
        async with session(pg_db) as s:
            return await rs.run_due_retries(s, datetime.now(UTC))

    assert asyncio.run(retry()) == [out["run_id"]]
    last = fakes["individual"][-1]
    assert last["recipients"] == ["cto@example.org"] and last["subject"] == subject
    assert last["attachment"][1] == b"%PDF-1.7 fake"
    status, delivered, retry_count, next_retry = pg_db.query(
        "SELECT status, delivered_to, retry_count, next_retry_at FROM report_runs WHERE id = $1",
        uuid.UUID(out["run_id"]))[0]
    delivered = delivered if isinstance(delivered, list) else json.loads(delivered)
    assert status == "success" and delivered == ["ops@example.com", "cto@example.org"]
    assert retry_count == 1 and next_retry is None


def test_housekeeping(pg_db):
    _company(pg_db)
    sid = _schedule(pg_db)
    now = datetime.now(UTC)
    rows = {
        "stale": (now - timedelta(hours=1), "running"),
        "ancient": (now - timedelta(days=400), "success"),
        "old_artifact": (now - timedelta(days=60), "success"),
        "fresh": (now - timedelta(days=1), "success"),
    }
    ids = {}
    for key, (at, status) in rows.items():
        rid = uuid.uuid4()
        ids[key] = rid
        pg_db.execute(
            "INSERT INTO report_runs (id, schedule_id, report_type, period, title, token, html, status, "
            "generated_at, artifact, email_html) VALUES ($1, $2, 'availability', 'last_7d', $3, $4, '<p>', $5, $6, "
            "'\\x25504446'::bytea, '<html>')", rid, sid, key, uuid.uuid4().hex, status, at)
    rs._last_retention_at = None

    async def tick():
        async with session(pg_db) as s:
            await rs._housekeeping(s, now)
            return await rs.scheduler_last_tick(s)

    last = asyncio.run(tick())
    assert last is not None and abs(last - now) < timedelta(seconds=1)
    got = {r[0]: r[1:] for r in pg_db.query(
        "SELECT title, status, artifact IS NOT NULL, email_html IS NOT NULL FROM report_runs")}
    assert "ancient" not in got
    assert got["stale"][0] == "failed"
    assert got["old_artifact"][1:] == (False, False)
    assert got["fresh"][1:] == (True, True)


def test_schedules_and_runs_api(pg_db, fakes):
    _company(pg_db)
    channel = uuid.uuid4()
    pg_db.execute("INSERT INTO notification_channels (id, name, type, config, enabled) "
                  "VALUES ($1, 'NOC', 'email', '{}'::jsonb, FALSE)", channel)
    with api_client(pg_db, role="admin", persist_user=True) as client:
        base = {"name": "Weekly SLA", "report_type": "availability", "period": "last_7d", "format": "pdf",
                "recipients": ["ops@example.com"]}
        assert client.post("/api/v1/report-schedules", json={**base, "notify_channels": [str(channel)]}) \
            .status_code == 400                                     # disabled channel
        assert client.post("/api/v1/report-schedules", json={**base, "notify_channels": ["nope"]}).status_code == 400
        r = client.post("/api/v1/report-schedules", json=base)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]

        run = client.post(f"/api/v1/report-schedules/{sid}/run-now").json()
        assert run["status"] == "success", run
        assert pg_db.scalar("SELECT kind FROM report_runs WHERE id = $1", uuid.UUID(run["run_id"])) == "manual"

        page = client.get("/api/v1/reports/runs?limit=10").json()
        assert page["total"] == 1 and page["data"][0]["kind"] == "manual" and page["data"][0]["has_artifact"]
        rid = page["data"][0]["id"]
        art = client.get(f"/api/v1/reports/runs/{rid}/artifact")
        assert art.status_code == 200 and art.content == b"%PDF-1.7 fake"
        hist = client.get(f"/api/v1/report-schedules/{sid}/runs").json()["data"]
        assert hist[0]["id"] == rid

        token = page["data"][0]["token"]
        assert client.get(f"/api/v1/reports/shared/{token}").status_code == 200
        assert client.post(f"/api/v1/reports/runs/{rid}/revoke").json()["revoked"] is True
        assert client.get(f"/api/v1/reports/shared/{token}").status_code == 410

        status = client.get("/api/v1/report-schedules/status").json()
        assert status["share_links_available"] is True and status["settings"]["link_expiry_days"] == 30
        assert client.put("/api/v1/reports/settings", json={"link_expiry_days": 7}).status_code == 200
        assert client.get("/api/v1/reports/settings").json()["settings"]["link_expiry_days"] == 7
