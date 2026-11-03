# Controller restart and update alerting

ZenPlus records controller recovery in `system_settings['monitoring.controller_recovery']`.
The signed OTA manifest records planned maintenance before stopping services, including
upgrades from an older updater. Graceful API shutdown also records an interruption.
Every API service invocation registers before background evaluators start and records
readiness after startup work completes. Workers share the systemd invocation identity
and serialize state changes with a database advisory lock. A 15-second database lease
detects interruptions longer than 90 seconds without requiring a clean shutdown.

Missing agent/sensor observations are temporarily unknown. There is no synthetic Down
incident to announce or resolve. Server list/detail show **Monitoring interrupted** until
the agent reports again. Open incidents, acknowledgement, routing, delivery keys and
customer thresholds remain intact. Fresh poller/service results and current metric
breaches still use normal confirmation and notification paths.

The agent communication supervisor caps exponential retry backoff at 600 seconds.
Recovery allows 660 seconds after readiness (maximum backoff plus two default 30-second
heartbeat ticks), then the existing rule silence allowance. Device Down defaults to
180 seconds, so a host that never reconnects can alert about 14 minutes after readiness,
plus the evaluator interval. Once an agent reports after readiness, it immediately
uses the normal silence allowance, even while other agents reconnect. Process/service
inventory waits for a new snapshot during the same recovery period.

The 30-minute recovery deadline is retained across repeated restarts until the
controller completes a stable reconnection window. No restart can renew an unfinished
deadline. If it expires, ZenPlus raises one **ZenPlus monitoring interrupted** P1
incident through the configured default P1 policy. It does not convert unobserved
targets into a fleet of false outages. Fresh target failures still alert, and targets
that remain silent alert after a stable reconnection window plus their normal threshold.
The health endpoint exposes `monitoring_ingestion=error` for a missing readiness lease
or an exceeded recovery deadline and reports the recovery phase/deadline.
An independent external monitor must watch
controller health to notify while ZenPlus itself is unavailable. This release does
not configure an external monitoring service.

Availability evaluation reloads each target's heartbeat and rechecks it immediately
before trigger dispatch. A heartbeat arriving after incident creation leaves that
incident unnotified, so its later resolution is silent. Existing confirmed incidents
can still send their legitimate recovery after a fresh heartbeat.

Database regression tests use private disposable PostgreSQL clusters, never appliance
databases. Release acceptance must additionally exercise API restart, OS reboot,
delayed reconnection, a genuinely disconnected target, fresh network failures, and
the signed OTA bridge path before full rollout.
