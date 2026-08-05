"""APM metric alert evaluation (AM-E6 / F8).

Ping/trap alerting is event-driven, host and network metrics have periodic
ClickHouse evaluators (host_alert_service / network_alert_service). APM RED
signals also arrive continuously — as 5-minute rollups in
``apm_span_metrics_5m`` — so they get the same periodic treatment here.

Every ``EVAL_INTERVAL_S`` this loads the enabled alert rules whose ``metric``
is one of the APM pull-path keys, computes the current per-service value from
the RED rollup (never raw ``apm_spans`` — the rollup is built from 100% of
spans pre-sampling, which is the doc-set's accuracy guarantee), and raises or
resolves a service-scoped row in ``alerts``.

Rule semantics:
- Scope: ``target`` holds the APM service name (exact, case-insensitive);
  empty target = every service reporting in the window.
- Window: ``max(min_duration, 300)`` seconds — 300 is the floor because the
  rollup granularity is 5 minutes.
- Units: latency keys are milliseconds; ``apm_error_rate`` and ``apm_apdex``
  are fractions in [0,1] (e.g. 0.02 = 2%, matching the /apm/services API);
  ``apm_throughput`` is requests/second.
- A service with zero requests in the window is *skipped*, not treated as
  breaching — "stopped reporting" detection belongs to a future no-data
  sweeper (F8's ``apm_nodata_sweeper``), not to threshold rules.

Alerts dedupe on (rule_id, metadata->>'service'): a rule/service pair raises
at most one active alert, so running in both uvicorn workers is harmless
(mirrors host/network evaluators). ``apm_slo_burn`` is *not* evaluated here —
the SLO burn loop (apm_slo_service) raises directly. ``apm_synthetic_down``
and ``apm_anomaly`` are whitelisted but their sources don't exist yet
(AM-E9/E12), so they are intentionally not handled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.host_alert_service import dispatch_to_channels

logger = logging.getLogger("zenplus.apm_alerts")

EVAL_INTERVAL_S = 60
DEFAULT_WINDOW_S = 300       # rollup granularity floor (5m buckets)

# Entry spans only — inbound request RED, matching the /apm/services API.
_ENTRY_KINDS = "('SERVER','CONSUMER')"

# The APM pull-path metrics this evaluator handles.
APM_PULL_METRICS = {
    "apm_latency_p50", "apm_latency_p95", "apm_latency_p99",
    "apm_error_rate", "apm_throughput", "apm_apdex",
}

_OPS = {
    "gt": lambda a, b: a > b, ">": lambda a, b: a > b,
    "gte": lambda a, b: a >= b, ">=": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "<": lambda a, b: a < b,
    "lte": lambda a, b: a <= b, "<=": lambda a, b: a <= b,
    "eq": lambda a, b: a == b, "==": lambda a, b: a == b,
    "neq": lambda a, b: a != b, "!=": lambda a, b: a != b,
}


def _cmp(value: float, operator: str, threshold: float) -> bool:
    fn = _OPS.get((operator or "").strip())
    return bool(fn and fn(value, threshold))


# ─── ClickHouse fetcher ──────────────────────────────────────────────────────

def _service_fleet(window_s: int) -> dict[str, dict]:
    """{service_name: {p50, p95, p99, error_rate, throughput, apdex, reqs}}."""
    from app.core.database import get_clickhouse_client

    since = (datetime.now(timezone.utc) - timedelta(seconds=window_s)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        client = get_clickhouse_client()
        rows = client.query(
            f"""
            SELECT service_name,
                   sum(request_count)                                                   AS reqs,
                   sum(error_count)                                                     AS errs,
                   arrayElement(quantilesTDigestMerge(0.5,0.95,0.99)(duration_state),1) AS p50,
                   arrayElement(quantilesTDigestMerge(0.5,0.95,0.99)(duration_state),2) AS p95,
                   arrayElement(quantilesTDigestMerge(0.5,0.95,0.99)(duration_state),3) AS p99,
                   sum(satisfied_count)                                                 AS sat,
                   sum(tolerating_count)                                                AS tol
            FROM zenplus.apm_span_metrics_5m
            WHERE timestamp >= %(s)s AND span_kind IN {_ENTRY_KINDS}
            GROUP BY service_name
            """,
            parameters={"s": since},
        ).result_rows
    except Exception as exc:
        logger.warning("apm alert: clickhouse RED query failed: %s", exc)
        return {}

    out: dict[str, dict] = {}
    for r in rows:
        reqs, errs = int(r[1] or 0), int(r[2] or 0)
        if reqs <= 0:
            continue
        sat, tol = int(r[6] or 0), int(r[7] or 0)
        out[str(r[0])] = {
            "apm_latency_p50": float(r[3] or 0),
            "apm_latency_p95": float(r[4] or 0),
            "apm_latency_p99": float(r[5] or 0),
            "apm_error_rate": errs / reqs,
            "apm_throughput": reqs / float(window_s),
            "apm_apdex": (sat + tol / 2.0) / reqs,
            "reqs": reqs,
        }
    return out


# ─── Value formatting ────────────────────────────────────────────────────────

def _detail(metric: str, value: float) -> str:
    if metric.startswith("apm_latency"):
        pct = metric.rsplit("_", 1)[-1]  # p50/p95/p99
        return f"{pct} {value:.0f}ms"
    if metric == "apm_error_rate":
        return f"error rate {value * 100:.2f}%"
    if metric == "apm_throughput":
        return f"{value:.2f} req/s"
    if metric == "apm_apdex":
        return f"apdex {value:.3f}"
    return f"{value:.3f}"


def _message(rule, service: str, detail: str) -> str:
    return f"{rule.name}: {service} — {detail}"


# ─── Alert raise / resolve (service-scoped rows in `alerts`) ─────────────────

async def _active_alert_id(db: AsyncSession, rule_id, service: str):
    row = (await db.execute(text(
        "SELECT id FROM alerts "
        "WHERE rule_id = :rid AND status IN ('active','acknowledged') "
        "  AND metadata->>'service' = :svc "
        "ORDER BY triggered_at DESC LIMIT 1"
    ), {"rid": str(rule_id), "svc": service})).first()
    return row[0] if row else None


async def _raise(db: AsyncSession, rule, service: str, message: str, value: float) -> None:
    await db.execute(text(
        "INSERT INTO alerts (rule_id, status, severity, message, triggered_at, metadata) "
        "VALUES (:rid, 'active', :sev, :msg, :ts, CAST(:meta AS jsonb))"
    ), {
        "rid": str(rule.id), "sev": rule.severity or "warning",
        "msg": message, "ts": datetime.now(timezone.utc),
        "meta": json.dumps({
            "source": "apm_metric", "service": service,
            "rule_id": str(rule.id), "metric": rule.metric,
            "value": round(value, 4), "threshold": float(rule.threshold or 0),
        }),
    })


async def _resolve(db: AsyncSession, alert_id) -> int:
    now = datetime.now(timezone.utc)
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = :ts, "
        "metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
        "WHERE id = :id AND status IN ('active','acknowledged')"
    ), {"ts": now, "id": alert_id,
        "m": json.dumps({"resolved_by": "apm_evaluator", "resolved_at": now.isoformat()})})
    return res.rowcount or 0


async def _notify(db: AsyncSession, rule, service: str, message: str, detail: str) -> None:
    # Quiet hours: the alert row is already recorded; only the outbound
    # notification is gated by the rule's schedule window.
    from app.services.alert_schedule import notifications_allowed, get_configured_timezone
    tz = await get_configured_timezone(db)
    if not notifications_allowed(
        getattr(rule, "schedule_start", None), getattr(rule, "schedule_end", None),
        getattr(rule, "schedule_days", None), tz,
    ):
        return
    sev = rule.severity or "warning"
    await dispatch_to_channels(db, rule.notify_channels or [], {
        "subject": f"[{sev.upper()}] {rule.name} — {service}",
        "body": message, "message": message,
        "hostname": service, "ip_address": "",
        "status": "ALERT", "severity": sev,
        "details": [("Service", service), ("Metric", rule.metric),
                    ("Value", detail), ("Threshold", f"{rule.operator} {rule.threshold}")],
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "rule_id": str(rule.id), "rule_name": rule.name,
    })


# ─── Main evaluation pass ────────────────────────────────────────────────────

def _services_in_scope(rule, fleet: dict[str, dict]) -> list[str]:
    if not rule.target:
        return list(fleet)
    t = rule.target.strip().lower()
    return [s for s in fleet if s.lower() == t]


async def evaluate_apm_rules(db: AsyncSession) -> dict[str, int]:
    metric_list = ",".join(f"'{m}'" for m in sorted(APM_PULL_METRICS))
    rules = (await db.execute(text(
        f"SELECT id, name, metric, operator, threshold, severity, min_duration, "
        f"notify_channels, target, schedule_start, schedule_end, schedule_days "
        f"FROM alert_rules WHERE enabled = true AND metric IN ({metric_list})"
    ))).all()
    if not rules:
        return {"rules": 0, "raised": 0, "resolved": 0}

    # Pre-fetch the per-service RED fleet once per distinct window.
    windows = {max(r.min_duration or 0, DEFAULT_WINDOW_S) for r in rules}
    fleet_cache = {w: _service_fleet(w) for w in windows}

    raised = resolved = 0
    for rule in rules:
        fleet = fleet_cache[max(rule.min_duration or 0, DEFAULT_WINDOW_S)]
        for service in _services_in_scope(rule, fleet):
            value = fleet[service][rule.metric]
            breach = _cmp(value, rule.operator, float(rule.threshold or 0))
            existing = await _active_alert_id(db, rule.id, service)
            if breach:
                if existing is None:
                    detail = _detail(rule.metric, value)
                    msg = _message(rule, service, detail)
                    await _raise(db, rule, service, msg, value)
                    raised += 1
                    await db.commit()
                    await _notify(db, rule, service, msg, detail)
            elif existing is not None:
                resolved += await _resolve(db, existing)
        await db.commit()

    if raised or resolved:
        logger.info("apm alert eval: %d rules, %d raised, %d resolved", len(rules), raised, resolved)
    return {"rules": len(rules), "raised": raised, "resolved": resolved}


async def apm_alert_evaluator_loop() -> None:
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(30)  # let the app boot and first rollup buckets land
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await evaluate_apm_rules(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("apm alert evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
