# Release 1.24.9 — Availability evidence and outage clarity

Device history now separates ping success, failed checks, confirmed outage count and confirmed downtime. The interactive timeline has explicit interval bounds, keyboard selection, unknown gaps and a recorded outage history with recovery timestamps. Historical and custom date ranges request the exact selected bounds for both the headline and chart.

The Availability dashboard explains weighted group/site ping percentages and the difference between failed checks, confirmed incidents and alerts. Devices with zero confirmed incidents no longer appear in the downtime leaderboard. The device incident API shares the report episode-pairing and maintenance semantics, includes carry-in outages and later recoveries, enforces device visibility and returns unavailable instead of zero when the history query fails.

The controller's existing policy remains three consecutive failed checks, three packets per check, at the configured device interval. A responding check resets confirmation and clears Down, while latency or packet loss may still mark Degraded. This policy is now visible in controller-owned reachability details. Remote sensor policies are not assumed. No telemetry, alerts, schemas, retention policies or monitoring thresholds are changed.

Validation: strict dashboard build, route smoke, availability contracts, scoped API/error/range/maintenance tests, report leaderboard regressions, and the poller confirmation/recovery sequence. Browser acceptance uses isolated development fixtures, including failed checks without outages, confirmed recovery, missing data, keyboard navigation and responsive layouts. Signed package acceptance and required CI remain release gates.
