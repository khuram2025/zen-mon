"""Reading ping availability out of the right ClickHouse table for a window.

Raw `zenplus.ping_metrics` is the highest-fidelity source but the shortest
lived; the 5-minute and 1-hour rollups written by the materialized views in
migrate-012 keep months to a year. A long window answered from raw therefore
describes a *much shorter* period than the caller asked for, silently — a
30-day availability figure computed over whatever week of raw samples happens
to survive.

Two traps make the rollups easy to get wrong, and both have bitten this
codebase:

* they have no ``is_up`` column, so ``countIf(is_up = 1)`` raises
  UNKNOWN_IDENTIFIER — which callers wrapped in ``except Exception`` read as
  "this table has no data" and quietly fall back to raw;
* ``uptime_pct`` is **not** a percentage. The views define it as
  ``avg(is_up)``, so it is a 0..1 fraction. Treating it as a percent reports a
  99.7%-available fleet as 1%.

Everything that needs windowed ping availability should go through here so the
Availability page, the report data service and the report engine agree on both
the source table and the arithmetic.
"""

from __future__ import annotations

RAW_TABLE = "ping_metrics"


def ping_table_for_hours(hours: float) -> str:
    """Finest table that can still cover a window this long.

    Kept deliberately coarse-grained: two callers picking different tables for
    the same window is how a page ends up showing a headline number and a
    trend line that disagree.
    """
    if hours <= 6:
        return RAW_TABLE
    if hours <= 168:  # 7 days
        return "ping_metrics_5m"
    return "ping_metrics_1h"


def is_rollup(table: str) -> bool:
    return table != RAW_TABLE


def uptime_agg_sql(table: str) -> str:
    """`up_samples, total_samples` expressions for the given table.

    Both shapes return sample counts (the rollup's weighted by how many raw
    samples each bucket represents) so callers can sum them across devices or
    subtract a maintenance window before taking the ratio.
    """
    if is_rollup(table):
        return ("sum(uptime_pct * sample_count) AS up_samples, "
                "sum(sample_count) AS total_samples")
    return "countIf(is_up = 1) AS up_samples, count() AS total_samples"


def rtt_agg_sql(table: str) -> str:
    """`avg_rtt, p95_rtt, rtt_samples` expressions for the given table.

    On the rollups the p95 is a quantile over per-bucket *averages*, not over
    individual probes — the raw samples are gone, so a true p95 is not
    recoverable. It reads a little low; that is the honest ceiling for a
    window older than raw retention.
    """
    if is_rollup(table):
        return ("avgIf(avg_rtt_ms, avg_rtt_ms > 0) AS avg_rtt, "
                "quantileExactIf(0.95)(avg_rtt_ms, avg_rtt_ms > 0) AS p95_rtt, "
                "countIf(avg_rtt_ms > 0) AS rtt_samples")
    return ("avgIf(rtt_ms, rtt_ms > 0) AS avg_rtt, "
            "quantileExactIf(0.95)(rtt_ms, rtt_ms > 0) AS p95_rtt, "
            "countIf(rtt_ms > 0) AS rtt_samples")
