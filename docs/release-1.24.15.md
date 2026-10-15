# ZenPlus 1.24.15 — Confirmed device status and flap detection

Device status no longer changes on a single ping. Degraded now needs 3 of the last 5
checks to be slow, lossy or unanswered. Recovery needs 3 checks within recovery limits
(80% of the Degraded limits by default), or a full window with no slow check. Down still
needs 3 consecutive failed checks, and a recovering device returns to Up before Degraded
is confirmed separately.

Devices whose confirmed status keeps changing are marked Flapping, separately for
reachability and for latency/loss. While a device flaps, its status alerts are held until a
status lasts 10 minutes; one notice is sent when flapping starts and one when it stops.

The same policy runs on the central poller and on remote-sensor ingest, from one shared
state machine verified by common test vectors. On the development appliance's last eight
days this cuts status changes by 79% and notifications by 82%. Every outage that lasted
10 minutes or more is still notified.

Migration 161 adds the device status state and flap episode tables, flap columns on
devices and a per-rule "notify while flapping" option. No history is deleted, and
availability percentages are unchanged. All limits are configurable under Settings →
General → Monitoring, including a legacy single-check preset. See
`docs/device-status-policy.md`.
