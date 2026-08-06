"""Service-dependency graph rollup (AM-E3 debt / E-6).

The service map used to be built by self-joining raw ``apm_spans`` against
itself on every page load — matching each child span to its parent to discover
"service A called service B". That works on a demo appliance and fails in two
ways in production:

* **Cost.** The join is over the raw span table for the whole selected range.
  It is the first query to OOM once span volume is real, and it re-does the
  same work for every viewer, every refresh.
* **Correctness under sampling.** The moment head/tail sampling lands, a parent
  and its child stop being guaranteed to survive together, so edges silently
  disappear from the map while the RED numbers (computed pre-sampling from
  rollups) stay right. A map that quietly loses edges is worse than no map.

This loop does the join **once per 5-minute bucket**, incrementally, and writes
the result to ``zenplus.apm_service_graph`` (SummingMergeTree, 90-day TTL). The
map API then reads a small pre-aggregated table.

Idempotency comes from a watermark: only buckets strictly newer than
``max(timestamp)`` already in the graph are aggregated, and only buckets that
have fully closed are considered — a still-filling bucket would be written with
partial counts and, because SummingMergeTree adds rather than replaces, could
never be corrected. Multi-worker safety is the usual transaction-scoped
Postgres advisory lock.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger("zenplus.apm.service_graph")

SERVICE_GRAPH_ADVISORY_LOCK = 1515074393

BUCKET_S = 300
EVAL_INTERVAL_S = 60
#: Never look further back than this in one pass (cold start / long outage).
MAX_CATCHUP_BUCKETS = 24 * 12  # 24h of 5-minute buckets
#: A bucket is only aggregated once it is this far in the past, so late-arriving
#: spans (batch export lag) are already in ClickHouse when we read it.
SETTLE_S = 120


def _floor_bucket(ts: float) -> int:
    return int(ts // BUCKET_S) * BUCKET_S


def _watermark_and_target() -> tuple[int, int]:
    """(first bucket to aggregate, last closed bucket) as epoch seconds."""
    from app.core.database import get_clickhouse_client

    now = datetime.now(timezone.utc).timestamp()
    # Last bucket that has both closed and settled.
    target = _floor_bucket(now - SETTLE_S) - BUCKET_S
    row = get_clickhouse_client().query(
        "SELECT toUnixTimestamp(max(timestamp)) FROM zenplus.apm_service_graph"
    ).result_rows
    last = int(row[0][0] or 0) if row else 0
    if last <= 0:
        # Cold start: begin at the oldest span we still hold, capped.
        span_row = get_clickhouse_client().query(
            "SELECT toUnixTimestamp(min(timestamp)) FROM zenplus.apm_spans"
        ).result_rows
        oldest = int(span_row[0][0] or 0) if span_row else 0
        if oldest <= 0:
            return target + BUCKET_S, target  # no spans at all -> nothing to do
        start = _floor_bucket(oldest)
    else:
        start = last + BUCKET_S
    floor = target - MAX_CATCHUP_BUCKETS * BUCKET_S
    return max(start, floor), target


def _aggregate_range(frm: int, to: int) -> int:
    """Aggregate buckets [frm, to] into apm_service_graph. Returns rows written."""
    from app.core.database import get_clickhouse_client

    client = get_clickhouse_client()
    # One INSERT..SELECT: the parent/child join runs server-side over a bounded
    # slice, never crossing the client back to Python.
    client.command(
        """
        INSERT INTO zenplus.apm_service_graph
            (timestamp, client_service, server_service, env,
             request_count, error_count, duration_sum_ms, sample_count)
        SELECT
            toStartOfInterval(child.timestamp, INTERVAL 5 MINUTE) AS bucket,
            parent.service_name                                   AS client_service,
            child.service_name                                    AS server_service,
            child.env                                             AS env,
            count()                                               AS request_count,
            countIf(child.has_error = 1)                          AS error_count,
            sum(child.duration_nano) / 1e6                        AS duration_sum_ms,
            count()                                               AS sample_count
        FROM zenplus.apm_spans AS child
        INNER JOIN zenplus.apm_spans AS parent
          ON child.parent_span_id = parent.span_id
         AND child.trace_id = parent.trace_id
        WHERE child.timestamp >= toDateTime(%(frm)s)
          AND child.timestamp <  toDateTime(%(to)s)
          AND parent.timestamp >= toDateTime(%(pfrm)s)
          AND parent.timestamp <  toDateTime(%(to)s)
          AND parent.service_name != child.service_name
        GROUP BY bucket, client_service, server_service, env
        """,
        parameters={
            "frm": frm, "to": to + BUCKET_S,
            # A parent can start slightly before its child's bucket; widen the
            # parent side by one bucket so cross-boundary calls still match.
            "pfrm": frm - BUCKET_S,
        },
    )
    written = client.query(
        "SELECT count() FROM zenplus.apm_service_graph "
        "WHERE timestamp >= toDateTime(%(frm)s) AND timestamp < toDateTime(%(to)s)",
        parameters={"frm": frm, "to": to + BUCKET_S},
    ).result_rows
    return int(written[0][0] or 0) if written else 0


async def build_service_graph() -> dict[str, int]:
    """Aggregate every closed, not-yet-aggregated bucket. Safe to call anytime."""
    frm, to = await asyncio.to_thread(_watermark_and_target)
    if frm > to:
        return {"buckets": 0, "rows": 0}
    rows = await asyncio.to_thread(_aggregate_range, frm, to)
    buckets = (to - frm) // BUCKET_S + 1
    if buckets:
        logger.debug("apm service graph: aggregated %d bucket(s), %d edge rows", buckets, rows)
    return {"buckets": buckets, "rows": rows}


async def apm_service_graph_loop() -> None:
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(45)  # stagger behind the other APM loops
    while True:
        try:
            async with AsyncSessionLocal() as db:
                got = (await db.execute(
                    text("SELECT pg_try_advisory_xact_lock(:k)"),
                    {"k": SERVICE_GRAPH_ADVISORY_LOCK},
                )).scalar()
                if got:
                    try:
                        await build_service_graph()
                    finally:
                        # Commit ends the transaction and releases the xact lock.
                        await db.commit()
                else:
                    await db.rollback()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("apm service graph aggregation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
