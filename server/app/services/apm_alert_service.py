"""APM metric alert evaluation (AM-E6 / F8).

Ping/trap alerting is event-driven, host and network metrics have periodic
ClickHouse evaluators (host_alert_service / network_alert_service). Server APM
RED signals and browser RUM field metrics also arrive continuously, so they get
the same periodic treatment here.

Every ``EVAL_INTERVAL_S`` this loads enabled APM/RUM pull-path rules, computes
the current per-service or per-browser-application value, and raises or resolves
a scoped row in ``alerts``. Server RED reads its pre-sampling rollup; RUM reads
validated browser events and collapses finalized vitals once per view.

Rule semantics:
- Scope: ``target`` holds the APM service name or RUM ``application @ env``
  scope (exact, case-insensitive); empty target = every matching reporter in
  the window. Legacy RUM app-only targets continue to match each environment
  for that application independently.
- Window: ``max(min_duration, 300)`` seconds, then snapped down to a rollup
  bucket boundary and floored at two buckets (``apm_rollup``). Rollup rows carry
  their bucket *start*, so an unaligned window silently excluded the bucket
  holding most of the requested span; rates are divided by the seconds the
  window actually covers, not the nominal length.
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

from app.services import alert_phrasing as ap
from app.services import alert_notify_state as ns
from app.services.apm_rollup import rollup_window
from app.services.host_alert_service import dispatch_to_channels

logger = logging.getLogger("zenplus.apm_alerts")

EVAL_INTERVAL_S = 60
DEFAULT_WINDOW_S = 300       # rollup granularity floor (5m buckets)

# Entry spans only — inbound request RED, matching the /apm/services API.
_ENTRY_KINDS = "('SERVER','CONSUMER')"

# Server-side RED metrics and browser field-experience metrics share the alert
# lifecycle, but read different ClickHouse data sets.
RED_PULL_METRICS = {
    "apm_latency_p50", "apm_latency_p95", "apm_latency_p99",
    "apm_error_rate", "apm_throughput", "apm_apdex",
}
RUM_PULL_METRICS = {
    "apm_rum_lcp_p75", "apm_rum_inp_p75", "apm_rum_cls_p75",
    "apm_rum_error_session_rate", "apm_rum_resource_failure_rate",
    "apm_rum_new_error_groups",
}
APM_PULL_METRICS = RED_PULL_METRICS | RUM_PULL_METRICS
_RUM_SCOPE_SEPARATOR = " @ "

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
    """{service_name: {p50, p95, p99, error_rate, throughput, apdex, reqs}}.

    The window is bucket-aligned and its *covered* duration drives the rate
    math (see apm_rollup). Reading ``timestamp >= now() - 300s`` off a rollup
    labelled by bucket start used to see only the still-filling current bucket
    and then divide that partial count by a full 300 s, under-reporting
    throughput by up to 5x — enough to make any "throughput below X" rule fire
    permanently.
    """
    from app.core.database import get_clickhouse_client

    win = rollup_window(window_s)
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
            FROM zenplus.{win.table}
            WHERE timestamp >= %(s)s AND span_kind IN {_ENTRY_KINDS}
            GROUP BY service_name
            """,
            parameters={"s": win.start_str},
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
            "apm_throughput": reqs / win.covered_s,
            "apm_apdex": (sat + tol / 2.0) / reqs,
            "reqs": reqs,
        }
    return out


def _rum_fleet(window_s: int) -> dict[str, dict]:
    """Return alertable field metrics keyed by browser application/environment.

    Vitals are collapsed to the most recent finalized record per view before
    the percentile is calculated. This avoids weighting a view more heavily
    when a browser retries or sends an interim checkpoint. Session/resource
    rates are calculated independently across the same aligned window.
    """
    from app.core.database import get_clickhouse_client

    win = rollup_window(window_s)
    try:
        client = get_clickhouse_client()
        totals = client.query(
            """
            SELECT application_id, env,
                   uniqExactIf(session_id, sampled = 1) AS sessions,
                   uniqExactIf(session_id, event_type = 'error' AND sampled = 1) AS error_sessions,
                   countIf(event_type = 'resource' AND sampled = 1) AS resources,
                   countIf(event_type = 'resource' AND sampled = 1 AND
                           (status_code >= 400 OR attributes['failed'] = 'true')) AS failed_resources
            FROM zenplus.apm_rum_events
            WHERE timestamp >= %(s)s AND application_id != ''
            GROUP BY application_id, env
            """,
            parameters={"s": win.start_str},
        ).result_rows
        vitals = client.query(
            """
            SELECT application_id, env,
                   quantileTDigestIf(0.75)(lcp, lcp_present = 1), countIf(lcp_present = 1),
                   quantileTDigestIf(0.75)(inp, inp_present = 1), countIf(inp_present = 1),
                   quantileTDigestIf(0.75)(cls, cls_present = 1), countIf(cls_present = 1)
            FROM (
                SELECT application_id, env, view_id,
                       argMaxIf(lcp, timestamp, event_type = 'view' AND is_final = 1 AND has_lcp = 1) AS lcp,
                       max(event_type = 'view' AND is_final = 1 AND has_lcp = 1) AS lcp_present,
                       argMaxIf(inp, timestamp, event_type = 'view' AND is_final = 1 AND has_inp = 1) AS inp,
                       max(event_type = 'view' AND is_final = 1 AND has_inp = 1) AS inp_present,
                       argMaxIf(cls, timestamp, event_type = 'view' AND is_final = 1 AND has_cls = 1) AS cls,
                       max(event_type = 'view' AND is_final = 1 AND has_cls = 1) AS cls_present
                FROM zenplus.apm_rum_events
                WHERE timestamp >= %(s)s AND application_id != '' AND view_id != ''
                  AND sampled = 1
                GROUP BY application_id, env, view_id
            )
            GROUP BY application_id, env
            """,
            parameters={"s": win.start_str},
        ).result_rows
        # Error groups first seen inside this window (never seen in the 14-day
        # raw retention before it) — "a new kind of error appeared".
        new_groups = client.query(
            """
            SELECT application_id, env, count() FROM (
                SELECT application_id, env,
                       if(error_fingerprint != '', error_fingerprint,
                          lower(hex(MD5(concat(error_type, ':', error_message))))) AS fp,
                       min(timestamp) AS first_seen
                FROM zenplus.apm_rum_events
                WHERE timestamp >= now() - INTERVAL 14 DAY AND event_type = 'error'
                  AND application_id != ''
                GROUP BY application_id, env, fp
                HAVING first_seen >= %(s)s
            ) GROUP BY application_id, env
            """,
            parameters={"s": win.start_str},
        ).result_rows
    except Exception as exc:
        logger.warning("RUM alert: ClickHouse field-metric query failed: %s", exc)
        return {}

    out: dict[str, dict] = {}
    for app, env, sessions, error_sessions, resources, failed_resources in totals:
        session_count = int(sessions or 0)
        resource_count = int(resources or 0)
        if session_count <= 0:
            continue
        scope = f"{app}{_RUM_SCOPE_SEPARATOR}{env or 'unknown'}"
        out[scope] = {
            "apm_rum_error_session_rate": int(error_sessions or 0) / session_count,
            "sessions": session_count,
            "application_id": str(app),
            "env": str(env or "unknown"),
        }
        if resource_count > 0:
            out[scope]["apm_rum_resource_failure_rate"] = int(failed_resources or 0) / resource_count
    for app, env, lcp, lcp_samples, inp, inp_samples, cls, cls_samples in vitals:
        scope = f"{app}{_RUM_SCOPE_SEPARATOR}{env or 'unknown'}"
        metrics = out.setdefault(scope, {
            "application_id": str(app), "env": str(env or "unknown"),
        })
        if int(lcp_samples or 0) > 0:
            metrics["apm_rum_lcp_p75"] = float(lcp)
        if int(inp_samples or 0) > 0:
            metrics["apm_rum_inp_p75"] = float(inp)
        if int(cls_samples or 0) > 0:
            metrics["apm_rum_cls_p75"] = float(cls)
    for app, env, groups in new_groups:
        scope = f"{app}{_RUM_SCOPE_SEPARATOR}{env or 'unknown'}"
        metrics = out.setdefault(scope, {"application_id": str(app), "env": str(env or "unknown")})
        metrics["apm_rum_new_error_groups"] = float(groups or 0)
    # Scopes with sessions but no new groups must still evaluate to 0 so a
    # "> 0" rule resolves once the burst is over.
    for scope, metrics in out.items():
        metrics.setdefault("apm_rum_new_error_groups", 0.0)
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
    if metric == "apm_rum_lcp_p75":
        return f"field LCP p75 {value:.0f}ms"
    if metric == "apm_rum_inp_p75":
        return f"field INP p75 {value:.0f}ms"
    if metric == "apm_rum_cls_p75":
        return f"field CLS p75 {value:.3f}"
    if metric == "apm_rum_error_session_rate":
        return f"error-affected sessions {value * 100:.2f}%"
    if metric == "apm_rum_resource_failure_rate":
        return f"failed browser resources {value * 100:.2f}%"
    if metric == "apm_rum_new_error_groups":
        return f"{value:.0f} new browser error group{'' if value == 1 else 's'}"
    return f"{value:.3f}"


def _message(rule, service: str, detail: str) -> str:
    return f"{rule.name}: {service} — {detail}"


# ─── Alert raise / resolve (service-scoped rows in `alerts`) ─────────────────

async def _active_alert(db: AsyncSession, rule_id, service: str):
    """The open alert for this rule/service: (id, triggered_at) or None."""
    row = (await db.execute(text(
        "SELECT id, triggered_at FROM alerts "
        "WHERE rule_id = :rid AND status IN ('active','acknowledged') "
        "  AND metadata->>'service' = :svc "
        "ORDER BY triggered_at DESC LIMIT 1"
    ), {"rid": str(rule_id), "svc": service})).first()
    return (row[0], row[1]) if row else None


async def _raise(db: AsyncSession, rule, service: str, message: str, value: float):
    is_rum = rule.metric in RUM_PULL_METRICS
    rum_app, separator, rum_env = service.partition(_RUM_SCOPE_SEPARATOR)
    row = (await db.execute(text(
        "INSERT INTO alerts (rule_id, status, severity, message, triggered_at, metadata) "
        "VALUES (:rid, 'active', :sev, :msg, :ts, CAST(:meta AS jsonb)) RETURNING id"
    ), {
        "rid": str(rule.id), "sev": rule.severity or "warning",
        "msg": message, "ts": datetime.now(timezone.utc),
        "meta": json.dumps({
            "source": "apm_rum_metric" if is_rum else "apm_metric", "service": service,
            **({
                "application_id": rum_app,
                "environment": rum_env if separator else "",
            } if is_rum else {}),
            "rule_id": str(rule.id), "metric": rule.metric,
            "value": round(value, 4), "threshold": float(rule.threshold or 0),
        }),
    })).first()
    return row[0] if row else None


async def _resolve(db: AsyncSession, alert_id) -> int:
    now = datetime.now(timezone.utc)
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = :ts, "
        "metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
        "WHERE id = :id AND status IN ('active','acknowledged')"
    ), {"ts": now, "id": alert_id,
        "m": json.dumps({"resolved_by": "apm_evaluator", "resolved_at": now.isoformat()})})
    return res.rowcount or 0


def _render(template: str, variables: dict) -> str:
    for key, value in variables.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


async def _notify(db: AsyncSession, rule, service: str, value: float, *,
                  is_recovery: bool = False, duration: str = "") -> bool:
    """Send an APM threshold alert, or its all-clear. True when dispatched."""
    # Quiet hours: the alert row is already recorded; only the outbound
    # notification is gated by the rule's schedule window. Recovery notices go
    # out regardless — silence after a page reads as "still broken".
    from app.services.alert_schedule import notifications_allowed, get_configured_timezone
    tz = await get_configured_timezone(db)
    if not is_recovery and not notifications_allowed(
        getattr(rule, "schedule_start", None), getattr(rule, "schedule_end", None),
        getattr(rule, "schedule_days", None), tz,
    ):
        return False
    sev = rule.severity or "warning"
    rum_app, separator, rum_env = service.partition(_RUM_SCOPE_SEPARATOR)
    reading = ap.format_value(rule.metric, value)
    v = ap.rule_phrasing(rule, hostname=service, is_recovery=is_recovery,
                         reading=reading, duration=duration)
    v.update({"rule_name": rule.name or "Alert", "hostname": service,
              "ip_address": "", "severity": sev.upper(),
              "status": "RESOLVED" if is_recovery else "ALERT"})
    await dispatch_to_channels(db, rule.notify_channels or [], {
        "subject": _render(ap.DEFAULT_RECOVERY_EMAIL_SUBJECT if is_recovery
                           else ap.DEFAULT_EMAIL_SUBJECT, v),
        "body": _render(ap.DEFAULT_RECOVERY_EMAIL_BODY if is_recovery
                        else ap.DEFAULT_EMAIL_BODY, v),
        "message": _render(ap.DEFAULT_RECOVERY_SMS if is_recovery else ap.DEFAULT_SMS, v),
        "hostname": service, "ip_address": "",
        "status": "RESOLVED" if is_recovery else "ALERT", "severity": sev,
        "resolved": is_recovery,
        "headline_metric": {
            "label": ap.metric_noun(rule.metric), "value": reading,
            "secondary_label": "Threshold", "secondary_value": v.get("threshold_value"),
        },
        "details": [("Alert rule", rule.name),
                    ("Browser application" if rule.metric in RUM_PULL_METRICS else "Service",
                     rum_app if rule.metric in RUM_PULL_METRICS else service),
                    ("Environment", rum_env if rule.metric in RUM_PULL_METRICS and separator else None),
                    ("Condition", v.get("condition_label")),
                    ("Active for", duration if is_recovery else None)],
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "rule_id": str(rule.id), "rule_name": rule.name,
    })
    return True


# ─── Main evaluation pass ────────────────────────────────────────────────────

def _services_in_scope(rule, fleet: dict[str, dict]) -> list[str]:
    if not rule.target:
        return list(fleet)
    t = rule.target.strip().lower()
    return [s for s in fleet if s.lower() == t]


def _rum_scopes_in_scope(rule, fleet: dict[str, dict]) -> list[str]:
    """Match a specific app/environment scope, with legacy app-only support."""
    if not rule.target:
        return list(fleet)
    target = rule.target.strip().casefold()
    exact = [scope for scope in fleet if scope.casefold() == target]
    if exact:
        return exact
    # Rules created before environment-aware RUM scoping stored only the app
    # identifier. Preserve them by evaluating that app independently per env.
    return [
        scope for scope in fleet
        if scope.partition(_RUM_SCOPE_SEPARATOR)[0].casefold() == target
    ]


async def evaluate_apm_rules(db: AsyncSession) -> dict[str, int]:
    metric_list = ",".join(f"'{m}'" for m in sorted(APM_PULL_METRICS))
    rules = (await db.execute(text(
        f"SELECT id, name, metric, operator, threshold, severity, min_duration, "
        f"notify_channels, target, recovery_alert, conditions, condition_logic, "
        f"schedule_start, schedule_end, schedule_days "
        f"FROM alert_rules WHERE enabled = true AND metric IN ({metric_list})"
    ))).all()
    if not rules:
        return {"rules": 0, "raised": 0, "resolved": 0}

    # Pre-fetch each data plane once per distinct window. Avoid touching the
    # RUM table on installations that have no RUM alert rules yet.
    red_windows = {
        max(r.min_duration or 0, DEFAULT_WINDOW_S)
        for r in rules if r.metric in RED_PULL_METRICS
    }
    rum_windows = {
        max(r.min_duration or 0, DEFAULT_WINDOW_S)
        for r in rules if r.metric in RUM_PULL_METRICS
    }
    red_fleet_cache = {w: _service_fleet(w) for w in red_windows}
    rum_fleet_cache = {w: _rum_fleet(w) for w in rum_windows}

    raised = resolved = 0
    for rule in rules:
        window = max(rule.min_duration or 0, DEFAULT_WINDOW_S)
        fleet = (rum_fleet_cache if rule.metric in RUM_PULL_METRICS else red_fleet_cache)[window]
        scope_selector = (
            _rum_scopes_in_scope if rule.metric in RUM_PULL_METRICS
            else _services_in_scope
        )
        for service in scope_selector(rule, fleet):
            # A percentile without samples is absent, not zero. Leave any
            # existing alert unchanged until field data resumes.
            if rule.metric not in fleet[service]:
                continue
            value = fleet[service][rule.metric]
            breach = _cmp(value, rule.operator, float(rule.threshold or 0))
            existing = await _active_alert(db, rule.id, service)
            if breach:
                if existing is None:
                    msg = _message(rule, service, _detail(rule.metric, value))
                    alert_id = await _raise(db, rule, service, msg, value)
                    raised += 1
                    await db.commit()
                    sent = await _notify(db, rule, service, value)
                    await ns.stamp(db, alert_id, sent)
                    await db.commit()
                elif await ns.is_pending(db, existing[0]):
                    # Quiet hours suppressed the trigger; the window is open
                    # now and the service is still breaching.
                    if await _notify(db, rule, service, value):
                        await ns.stamp(db, existing[0], True)
                        await db.commit()
            elif existing is not None:
                alert_id, started_at = existing
                notified = await ns.was_notified(db, alert_id)
                resolved += await _resolve(db, alert_id)
                if notified and getattr(rule, "recovery_alert", True):
                    await db.commit()
                    await _notify(db, rule, service, value, is_recovery=True,
                                  duration=ap.duration_between(started_at))
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
