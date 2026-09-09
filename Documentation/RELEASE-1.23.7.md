# ZenPlus 1.23.7

Service detail reports now distinguish failed checks from missing monitoring results.

- Availability cards and the timeline percentage use observed time. A raw result remains valid until the next result, up to two configured check intervals; longer gaps are excluded from availability and displayed as missing coverage.
- Every availability card reports coverage. Historical availability can be high while coverage is low; this does not establish that the service was healthy during the gaps.
- Daily/hourly history uses the same calculation and weights partial hours by covered seconds. Calendar days follow the browser's local timezone; rolling SLA windows remain rolling windows.
- Initial unknown-to-up transitions no longer invent downtime back to check creation. Unknown status is not a failure.
- Healthy streaks start at recovery and restart after gaps in observed results. The displayed start time is fixed, with a live duration. Stale or unavailable recovery evidence does not produce a fabricated streak.
- Offline sensors produce an overdue-results notice. Missing samples no longer produce a green timeline, a perfect health score, or a previous response time presented as a selected-window average.
- The calendar and timeline identify missing data explicitly. An unavailable SLA request offers a retry action.

No database migration or historical-data rewrite is required. Reports are recalculated from existing observations, so historical percentages may change. Up to 30 days use raw results; older windows use sample-weighted five-minute rollups with coverage bounded by the samples in each bucket. Rollup outage lengths are estimates. Per-site availability remains the explicitly labelled observed-check percentage over 24 hours.

Validation: 45 Python regression tests passed (one optional external certificate fixture skipped); service availability UI regressions and all 36 route checks passed; production dashboard build passed. The repository-wide TypeScript check still has pre-existing errors outside the changed service page.

Includes the generic TLS intermediate recovery and scoped local certificate trust introduced in 1.23.6.
