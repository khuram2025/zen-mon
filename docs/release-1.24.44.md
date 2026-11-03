# ZenPlus 1.24.44

Prevent false Device Down and Agent offline notification bursts after appliance
updates, API restarts and operating-system reboots. A durable, bounded recovery
window distinguishes interrupted monitoring from a target outage. Agents that
reconnect resume their normal outage thresholds immediately; targets that remain
silent alert after the reconnection allowance and normal silence threshold.

Preserve existing incidents, recheck fresh heartbeat data before sending, protect
sensor timeout sweeps and stale process/service inventory, and show interrupted
monitoring on server list/detail. Health checks expose the monitoring readiness
lease for independent controller monitoring. Email/SMS wording and timestamps from
1.24.43 are retained.

Appliance-only release. Requires the signed 1.24.36 installer catalog bridge.
