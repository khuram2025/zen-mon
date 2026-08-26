"""SLO error-budget burn evaluation (AM-E6 / F7).

Computes, per SLO in ``apm_slos``, the error-budget burn rate over the Google
SRE Workbook multi-window multi-burn-rate configuration and raises/resolves
burn alerts through the shared ``alerts`` table + notification channels.

Definitions (for a target like 99.9 over ``window_days``):
- budget fraction  = 1 - target/100            (0.001 for 99.9)
- bad fraction (w) = bad events / total events over window w
- burn rate (w)    = bad_fraction(w) / budget_fraction — burn 1.0 exhausts the
  budget exactly at window end; 14.4 exhausts a 30-day budget in ~2 days.

Canonical tiers (99.9-class config, doc 04 F7): **page** at 14.4× over 1h
(short 5m), **page** at 6× over 6h (short 30m), **ticket** at 1× over 3d
(short 6h). A tier breaches only when BOTH its long and short windows exceed
the factor — the short window is what makes the alert clear ~5 minutes after
recovery instead of at window end.

SLI sources read ONLY the RED rollups (never raw spans — rollups are computed
from 100% of spans pre-sampling):
- ``availability`` / ``error_rate``: exact, from error_count/request_count.
- ``latency``: fraction of requests slower than ``latency_threshold_ms``,
  estimated from the merged t-digest by interpolating a quantile grid
  (resolution ~0.1%; exact per-threshold counts don't exist in the rollup).
- ``custom``: not evaluated in v1 (skipped with a debug log).

Windows ≤ 6h read ``apm_span_metrics_5m``; longer windows and the budget
window read ``apm_span_metrics_1h`` (395-day TTL covers the 90-day max). Every
window is snapped down to a bucket boundary and floored at two buckets
(``apm_rollup``) — rollup rows are labelled with their bucket *start*, so a
plain ``timestamp >= now() - 300s`` filter dropped the bucket holding most of
the window and left ``short_burn`` pinned at 0, which silently disabled every
burn tier. The tier payload reports the effective window it was measured over.

Alerts dedupe on metadata ``slo:<id>:<tier>`` so both uvicorn workers can run
the loop (mirrors the host/network/apm evaluators). Notifications go to the
SLO's own ``notify_channels``; severity is critical for page tiers, warning
for the ticket tier.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.apm_rollup import min_window_for, rollup_window
from app.services.host_alert_service import dispatch_to_channels

logger = logging.getLogger("zenplus.apm_slo")

EVAL_INTERVAL_S = 60

_ENTRY_KINDS = "('SERVER','CONSUMER')"

# (tier, long_window_s, short_window_s, burn_factor, severity)
BURN_TIERS: list[tuple[str, int, int, float, str]] = [
    ("fast", 3_600, 300, 14.4, "critical"),
    ("mid", 6 * 3_600, 1_800, 6.0, "critical"),
    ("slow", 72 * 3_600, 6 * 3_600, 1.0, "warning"),
]

# Quantile grid for latency-SLI CDF interpolation over the merged t-digest.
_Q_GRID = [0.01, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70,
           0.80, 0.90, 0.95, 0.975, 0.99, 0.995, 0.999]


def _frac_above(threshold_ms: float, grid_values: list[float]) -> float:
    """Estimate the fraction of requests slower than threshold_ms by linear
    interpolation on the (quantile, value) curve. Values beyond the last grid
    point (p99.9) read as 0 — the resolution floor is ~0.1%."""
    pairs = [(q, v) for q, v in zip(_Q_GRID, grid_values) if v is not None]
    if not pairs:
        return 0.0
    if threshold_ms < pairs[0][1]:
        return 1.0
    if threshold_ms >= pairs[-1][1]:
        return 0.0
    for (q1, v1), (q2, v2) in zip(pairs, pairs[1:]):
        if v1 <= threshold_ms < v2:
            if v2 <= v1:  # flat segment — snap to the upper quantile
                return 1.0 - q2
            q = q1 + (q2 - q1) * (threshold_ms - v1) / (v2 - v1)
            return max(0.0, 1.0 - q)
    return 0.0


def _bad_fraction(service: str, env: str | None, operation: str | None,
                  sli_type: str, latency_threshold_ms: int | None,
                  window_s: int) -> tuple[float, int] | None:
    """(bad_fraction, total_requests) over the window, or None when no data.

    The window is snapped down to a rollup-bucket boundary (see apm_rollup):
    without that, a 5 m short window reads only the still-filling current bucket,
    so ``short_burn`` sat at 0 and no burn tier could ever satisfy its
    both-windows gate.
    """
    from app.core.database import get_clickhouse_client

    win = rollup_window(window_s)
    conds = ["timestamp >= %(since)s", f"span_kind IN {_ENTRY_KINDS}",
             "service_name = %(svc)s"]
    params: dict = {"since": win.start_str, "svc": service}
    if env:
        conds.append("env = %(env)s"); params["env"] = env
    if operation:
        conds.append("operation = %(op)s"); params["op"] = operation
    where = " AND ".join(conds)
    table = win.table

    q_list = ",".join(str(q) for q in _Q_GRID)
    try:
        client = get_clickhouse_client()
        if sli_type in ("availability", "error_rate"):
            row = client.query(
                f"SELECT sum(request_count), sum(error_count) "
                f"FROM zenplus.{table} WHERE {where}",
                parameters=params,
            ).result_rows[0]
            total, bad = int(row[0] or 0), int(row[1] or 0)
            if total <= 0:
                return None
            return bad / total, total
        if sli_type == "latency":
            row = client.query(
                f"SELECT sum(request_count), "
                f"       quantilesTDigestMerge({q_list})(duration_state) "
                f"FROM zenplus.{table} WHERE {where}",
                parameters=params,
            ).result_rows[0]
            total = int(row[0] or 0)
            if total <= 0:
                return None
            grid = [float(v) for v in (row[1] or [])]
            return _frac_above(float(latency_threshold_ms or 0), grid), total
    except Exception as exc:
        logger.warning("slo burn: clickhouse query failed (%s, %ds): %s", service, window_s, exc)
        return None
    return None  # custom SLIs are not evaluated in v1


def compute_slo_status(slo: dict) -> dict:
    """Full burn/budget picture for one SLO (loop + budget endpoint share this).

    ``slo`` needs: service_name, env (may be None), operation, sli_type,
    latency_threshold_ms, target, window_days.
    Runs blocking ClickHouse queries — call via asyncio.to_thread.
    """
    budget = max(1.0 - float(slo["target"]) / 100.0, 1e-9)

    def frac(window_s: int):
        return _bad_fraction(slo["service_name"], slo.get("env"), slo.get("operation"),
                             slo["sli_type"], slo.get("latency_threshold_ms"), window_s)

    tiers = []
    cache: dict[int, tuple[float, int] | None] = {}
    for tier, long_s, short_s, factor, severity in BURN_TIERS:
        for w in (long_s, short_s):
            if w not in cache:
                cache[w] = frac(w)
        long_r, short_r = cache[long_s], cache[short_s]
        long_burn = (long_r[0] / budget) if long_r else None
        short_burn = (short_r[0] / budget) if short_r else None
        tiers.append({
            "tier": tier, "long_window_s": long_s, "short_window_s": short_s,
            # What the rollup granularity actually let us measure — the fast
            # tier's nominal 5 m short window is read over 10 m, because one
            # 5-minute bucket is always partly unfilled.
            "long_window_effective_s": min_window_for(long_s),
            "short_window_effective_s": min_window_for(short_s),
            "long_requests": long_r[1] if long_r else 0,
            "short_requests": short_r[1] if short_r else 0,
            "factor": factor, "severity": severity,
            "long_burn": None if long_burn is None else round(long_burn, 2),
            "short_burn": None if short_burn is None else round(short_burn, 2),
            "breaching": bool(long_burn is not None and short_burn is not None
                              and long_burn >= factor and short_burn >= factor),
        })

    # Budget consumed over the SLO's own window (7/30/90 days).
    window_r = frac(int(slo["window_days"]) * 86_400)
    consumed = (window_r[0] / budget) if window_r else None
    return {
        "budget_fraction": budget,
        "window_days": int(slo["window_days"]),
        "window_requests": window_r[1] if window_r else 0,
        "budget_consumed": None if consumed is None else round(consumed, 4),
        "budget_remaining": None if consumed is None else round(max(0.0, 1.0 - consumed), 4),
        "tiers": tiers,
    }


def compute_slo_series(slo: dict) -> list[dict]:
    """Daily SLI / error-budget series over the SLO window (1h rollup).

    Each point is one UTC day: request volume, bad events, observed SLI, and
    remaining budget if that day's badness were the whole-window rate. Used by
    the SLO detail page — not by the burn evaluator.
    """
    from app.core.database import get_clickhouse_client

    window_days = int(slo.get("window_days") or 30)
    budget = max(1.0 - float(slo["target"]) / 100.0, 1e-9)
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    conds = ["timestamp >= %(since)s", f"span_kind IN {_ENTRY_KINDS}",
             "service_name = %(svc)s"]
    params: dict = {"since": since.strftime("%Y-%m-%d %H:%M:%S"), "svc": slo["service_name"]}
    if slo.get("env"):
        conds.append("env = %(env)s"); params["env"] = slo["env"]
    if slo.get("operation"):
        conds.append("operation = %(op)s"); params["op"] = slo["operation"]
    where = " AND ".join(conds)
    sli_type = slo.get("sli_type") or "availability"
    q_list = ",".join(str(q) for q in _Q_GRID)
    try:
        client = get_clickhouse_client()
        if sli_type in ("availability", "error_rate"):
            rows = client.query(
                f"SELECT toStartOfDay(timestamp) AS day, "
                f"       sum(request_count), sum(error_count) "
                f"FROM zenplus.apm_span_metrics_1h WHERE {where} "
                f"GROUP BY day ORDER BY day",
                parameters=params,
            ).result_rows
            out = []
            cum_req = cum_bad = 0
            for day, reqs, errs in rows:
                reqs, errs = int(reqs or 0), int(errs or 0)
                if reqs <= 0:
                    continue
                cum_req += reqs
                cum_bad += errs
                bad_frac = errs / reqs
                sli = max(0.0, 1.0 - bad_frac) * 100.0
                consumed = (cum_bad / cum_req) / budget if cum_req else 0.0
                out.append({
                    "timestamp": day.isoformat() if hasattr(day, "isoformat") else str(day),
                    "requests": reqs,
                    "bad": errs,
                    "sli": round(sli, 4),
                    "budget_remaining": round(max(0.0, 1.0 - consumed), 4),
                })
            return out
        if sli_type == "latency":
            threshold = float(slo.get("latency_threshold_ms") or 0)
            rows = client.query(
                f"SELECT toStartOfDay(timestamp) AS day, "
                f"       sum(request_count), "
                f"       quantilesTDigestMerge({q_list})(duration_state) "
                f"FROM zenplus.apm_span_metrics_1h WHERE {where} "
                f"GROUP BY day ORDER BY day",
                parameters=params,
            ).result_rows
            out = []
            cum_req = cum_bad = 0.0
            for day, reqs, grid in rows:
                reqs = int(reqs or 0)
                if reqs <= 0:
                    continue
                frac = _frac_above(threshold, [float(v) for v in (grid or [])])
                bad = frac * reqs
                cum_req += reqs
                cum_bad += bad
                sli = max(0.0, 1.0 - frac) * 100.0
                consumed = (cum_bad / cum_req) / budget if cum_req else 0.0
                out.append({
                    "timestamp": day.isoformat() if hasattr(day, "isoformat") else str(day),
                    "requests": reqs,
                    "bad": int(round(bad)),
                    "sli": round(sli, 4),
                    "budget_remaining": round(max(0.0, 1.0 - consumed), 4),
                })
            return out
    except Exception as exc:
        logger.warning("slo series: clickhouse query failed (%s): %s", slo.get("name"), exc)
        return []
    return []


# ─── Alert raise / resolve ───────────────────────────────────────────────────

async def _active_alert_id(db: AsyncSession, dedupe: str):
    row = (await db.execute(text(
        "SELECT id FROM alerts WHERE status IN ('active','acknowledged') "
        "AND metadata->>'dedupe' = :d ORDER BY triggered_at DESC LIMIT 1"
    ), {"d": dedupe})).first()
    return row[0] if row else None


async def _raise(db: AsyncSession, slo: dict, tier: dict, dedupe: str, message: str) -> None:
    await db.execute(text(
        "INSERT INTO alerts (status, severity, message, triggered_at, metadata) "
        "VALUES ('active', :sev, :msg, :ts, CAST(:meta AS jsonb))"
    ), {
        "sev": tier["severity"], "msg": message, "ts": datetime.now(timezone.utc),
        "meta": json.dumps({
            "source": "apm_slo_burn", "dedupe": dedupe,
            "slo_id": str(slo["id"]), "slo_name": slo["name"],
            "service": slo["service_name"], "tier": tier["tier"],
            "long_burn": tier["long_burn"], "short_burn": tier["short_burn"],
            "factor": tier["factor"],
        }),
    })


async def _resolve(db: AsyncSession, alert_id) -> int:
    now = datetime.now(timezone.utc)
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = :ts, "
        "metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
        "WHERE id = :id AND status IN ('active','acknowledged')"
    ), {"ts": now, "id": alert_id,
        "m": json.dumps({"resolved_by": "apm_slo_burn", "resolved_at": now.isoformat()})})
    return res.rowcount or 0


def _duration_label(seconds: int) -> str:
    if seconds >= 86_400 and seconds % 86_400 == 0:
        return f"{seconds // 86_400}d"
    if seconds >= 3_600:
        return f"{seconds // 3_600}h"
    return f"{seconds // 60}m"


def _burn_message(slo: dict, tier: dict) -> str:
    # Report the windows actually measured — bucket alignment widens the
    # nominal 5m short window to 10m, and saying "5m" would misdescribe it.
    long_lbl = _duration_label(tier.get("long_window_effective_s") or tier["long_window_s"])
    short_lbl = _duration_label(tier.get("short_window_effective_s") or tier["short_window_s"])
    return (f"SLO burn: {slo['name']} ({slo['service_name']}) burning at "
            f"{tier['long_burn']}x over {long_lbl} / {tier['short_burn']}x over {short_lbl} "
            f"(page threshold {tier['factor']}x, target {slo['target']}%)")


async def _notify(db: AsyncSession, slo: dict, tier: dict, message: str) -> None:
    await dispatch_to_channels(db, slo.get("notify_channels") or [], {
        "subject": f"[{tier['severity'].upper()}] SLO burn — {slo['name']}",
        "body": message, "message": message,
        "hostname": slo["service_name"], "ip_address": "",
        "status": "ALERT", "severity": tier["severity"],
        "details": [("Service", slo["service_name"]),
                    ("SLO", f"{slo['target']}% over {slo['window_days']}d"),
                    ("SLO burn", f"{tier['long_burn']}x"),
                    ("Tier", f"{tier['tier']} ({tier['factor']}x)")],
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "rule_id": str(slo["id"]), "rule_name": slo["name"],
    })


# ─── Main pass ───────────────────────────────────────────────────────────────

async def _load_slos(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(text(
        """SELECT s.id, s.name, s.operation, s.sli_type, s.latency_threshold_ms,
                  s.target, s.window_days, s.burn_alert_enabled, s.notify_channels,
                  svc.name AS service_name, e.name AS env
           FROM apm_slos s
           JOIN apm_services svc ON svc.id = s.service_id
           LEFT JOIN apm_environments e ON e.id = svc.env_id"""
    ))).mappings().all()
    return [dict(r) for r in rows]


async def evaluate_slo_burn(db: AsyncSession) -> dict[str, int]:
    slos = await _load_slos(db)
    if not slos:
        return {"slos": 0, "raised": 0, "resolved": 0}

    raised = resolved = 0
    for slo in slos:
        try:
            status = await asyncio.to_thread(compute_slo_status, slo)
        except Exception:
            logger.exception("slo burn: compute failed for %s", slo["id"])
            continue
        for tier in status["tiers"]:
            dedupe = f"slo:{slo['id']}:{tier['tier']}"
            existing = await _active_alert_id(db, dedupe)
            if tier["breaching"]:
                if existing is None:
                    msg = _burn_message(slo, tier)
                    await _raise(db, slo, tier, dedupe, msg)
                    raised += 1
                    await db.commit()
                    if slo.get("burn_alert_enabled", True):
                        await _notify(db, slo, tier, msg)
            elif existing is not None:
                resolved += await _resolve(db, existing)
        await db.commit()

    if raised or resolved:
        logger.info("slo burn eval: %d slos, %d raised, %d resolved", len(slos), raised, resolved)
    return {"slos": len(slos), "raised": raised, "resolved": resolved}


async def apm_slo_burn_loop() -> None:
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(35)  # stagger behind the other evaluators
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await evaluate_slo_burn(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("slo burn evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
