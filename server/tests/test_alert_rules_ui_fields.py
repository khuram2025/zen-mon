"""Fields the alert rule UI reads (alerting audit 2026-11-19, rules UI pass).

* ``GET /alert-rules`` carries ``last_triggered_at`` per rule: the newest alert
  the rule raised, None for a rule that never fired (disposable PostgreSQL).
* ``GET /alert-rules/template-defaults`` carries, besides the six default
  templates per kind, the placeholders a save accepts per kind (``variables``)
  and the extras a family's own sender adds (``extras``), so the editor never
  offers ``{error}`` on a device rule (a save refuses it with 422).
"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from pg_fixture import pg_cluster, pg_db  # noqa: F401

from app.api.v1 import alert_rules as api
from app.services import alert_phrasing as ap


@contextlib.asynccontextmanager
async def _session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def _rule(db, name):
    return str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, notify_channels)"
        " VALUES ($1, 'cpu', '>', 90, 'warning', '[]'::jsonb) RETURNING id", name))


def test_rule_list_reports_when_each_rule_last_fired(pg_db):
    fired = _rule(pg_db, "Fired")
    quiet = _rule(pg_db, "Quiet")
    for at in ("2026-11-01T10:00:00Z", "2026-11-18T08:30:00Z", "2026-11-05T00:00:00Z"):
        pg_db.execute(
            "INSERT INTO alerts (rule_id, severity, status, message, triggered_at)"
            " VALUES ($1, 'warning', 'resolved', 'x', $2::text::timestamptz)", fired, at)

    async def run():
        async with _session(pg_db) as s:
            return await api.list_alert_rules(db=s, user=None)

    rows = {r["id"]: r for r in asyncio.run(run())["data"]}
    assert rows[fired]["last_triggered_at"].startswith("2026-11-18T08:30:00")
    assert rows[quiet]["last_triggered_at"] is None


def test_template_defaults_list_valid_placeholders_per_kind():
    out = asyncio.run(api.get_template_defaults(user=None))
    for kind in ("device", "service", "trap", "syslog"):
        assert out[kind] == ap.default_templates(kind)
        assert out["variables"][kind] == sorted(ap.template_variables(kind))
    device = set(out["variables"]["device"])
    assert {"site", "group", "site_suffix", "event_sentence"} <= device
    assert "error" not in device, "only service and APM synthetic rules fill {error}"
    assert "error" in out["variables"]["service"]
    # APM event rules add their own facts; a device rule never gets them.
    assert "failed_step" in out["extras"]["apm_synthetic_down"]
    assert all(isinstance(v, list) and v == sorted(v) for v in out["extras"].values())
