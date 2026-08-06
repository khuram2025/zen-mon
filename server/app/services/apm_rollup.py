"""Rollup-aware time windows for APM RED reads.

Every APM analytic reads ``apm_span_metrics_5m`` / ``apm_span_metrics_1h``,
whose rows are labelled with the **start** of their bucket. A naive
``timestamp >= now() - W`` filter therefore has two defects that broke both
APM evaluators:

1. **A whole bucket goes missing.** At 03:41 a 300 s window asks for
   ``timestamp >= 03:36``; the bucket labelled 03:35 (which covers 03:35–03:40,
   i.e. most of the requested window) sorts *before* 03:36 and is dropped. The
   query ends up covering only the current, still-filling bucket — between 0 and
   300 seconds of traffic, ~150 s on average.

2. **Rates are divided by the wrong denominator.** ``reqs / window_s`` assumes
   the window is full. With only the partial bucket present, throughput reads as
   low as 1/5th of the truth, so "throughput below X" rules fire constantly.

Both are fixed by snapping the window start **down** to a bucket boundary and
reporting the seconds actually covered:

    aligned_start = floor((now - W) / bucket) * bucket
    covered_s     = now - aligned_start        # >= W, < W + bucket

The window is up to one bucket wider than asked for. That is the honest reading
of a bucketed rollup — better a slightly longer window than a silently empty
one — and callers divide by ``covered_s``, so rates stay correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Rollup granularities, in seconds.
BUCKET_5M = 300
BUCKET_1H = 3_600

# Windows up to this long read the 5-minute rollup; longer ones read hourly.
ROLLUP_5M_MAX_S = 6 * 3_600


@dataclass(frozen=True)
class RollupWindow:
    """A bucket-aligned read window over one of the RED rollup tables."""

    table: str
    bucket_s: int
    #: Bucket-aligned window start (naive UTC — ClickHouse DateTime columns).
    start: datetime
    #: Query upper bound (exclusive); "now" for live reads.
    end: datetime
    #: Seconds actually covered by ``[start, end)`` — use this for rate math.
    covered_s: float

    @property
    def start_str(self) -> str:
        return self.start.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def end_str(self) -> str:
        return self.end.strftime("%Y-%m-%d %H:%M:%S")


def bucket_for(window_s: int) -> int:
    """Rollup granularity that serves a window of this length."""
    return BUCKET_5M if window_s <= ROLLUP_5M_MAX_S else BUCKET_1H


def table_for(window_s: int) -> str:
    return "apm_span_metrics_5m" if window_s <= ROLLUP_5M_MAX_S else "apm_span_metrics_1h"


def min_window_for(window_s: int) -> int:
    """Smallest window that still yields a usable sample at this granularity.

    A single 5-minute bucket is always partially filled, so anything asking for
    one bucket or less is widened to two. This is what keeps the SLO fast tier's
    5 m short window from evaluating against a bucket that is 10 seconds old.
    """
    bucket = bucket_for(window_s)
    return max(window_s, 2 * bucket)


def rollup_window(window_s: int, *, now: datetime | None = None,
                  widen_short: bool = True) -> RollupWindow:
    """Bucket-aligned window covering *at least* the last ``window_s`` seconds.

    ``widen_short`` applies the two-bucket floor from :func:`min_window_for`;
    pass ``False`` when the caller genuinely wants the raw requested span (e.g.
    a user-picked dashboard range, where a short-but-partial window is expected
    and only the rate denominator matters).
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    effective = min_window_for(window_s) if widen_short else window_s
    bucket = bucket_for(effective)
    epoch = now.timestamp()
    aligned = (int(epoch - effective) // bucket) * bucket
    start = datetime.utcfromtimestamp(aligned)
    return RollupWindow(
        table=table_for(effective),
        bucket_s=bucket,
        start=start,
        end=now,
        covered_s=max(epoch - aligned, float(bucket)),
    )


def align_ms_window(from_ms: int, to_ms: int) -> tuple[int, int, float]:
    """Bucket-align an explicit millisecond window (dashboard reads).

    Returns ``(aligned_from_ms, to_ms, covered_s)``. Only the lower bound moves:
    the upper bound is whatever the user asked for, and ``covered_s`` is the true
    span so callers can compute req/s without under-reporting.
    """
    span_s = max((to_ms - from_ms) / 1000.0, 1.0)
    bucket = bucket_for(int(span_s))
    aligned_from_s = (int(from_ms / 1000) // bucket) * bucket
    return aligned_from_s * 1000, to_ms, max(to_ms / 1000.0 - aligned_from_s, float(bucket))
