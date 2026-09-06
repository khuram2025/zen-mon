# Sensor detail page

Deployed to controller 192.168.8.221 on 2026-09-06.

Open Settings > Sensors and click a sensor name. The route is `/sensors/:id`.

The page displays controller connection and heartbeat freshness, site/location,
hostname, controller URL, source IP (which may reflect NAT), uptime, delivery
queue, installed and published signed versions, configuration reload, command
outcomes and recent activity. Manage sensor opens the existing administration
controls for site/location, assignments and enrollment.

Device and service tables include direct assignments and inherited group/default
assignments, respecting explicit monitoring policies and the user's tag scope.
Results are filtered to the selected sensor's poller ID. Each table includes
latest check status, last result, latency, observed 24-hour availability and
links to the target detail page. Search filters both tables. Data refreshes
every 10 seconds; command history refreshes every 5 seconds.

Missing or stale measurements are not presented as healthy. SNMP status denotes
sample freshness. Availability is based on received checks, not wall-clock
coverage. If measurement storage is unavailable, assignments remain visible
with a warning. The overview response excludes target credentials and service
URLs. Existing settings permissions protect access; command operations require
management permission.

Backend endpoint: `GET /api/v1/sensors/{sensor_id}/overview`.
No database migration is required.

Validation:

- 55 tests passed across sensor overview, monitoring sites, appliance API and
  security audit suites.
- Rollback-only database integration verified direct, group, default and offline
  inventory, explicit policy overrides, and real ClickHouse query execution.
- Dashboard production build and route smoke checks passed. The repository-wide
  TypeScript check still has pre-existing errors outside the changed page files.
- Live UI verified the Settings link, target search, management dialog, signed
  version display and a successful configuration reload command.
- VMware-Probe-01 displayed three devices and four service checks, all four
  services reporting up. The cisco-lab-01 SNMP check had no recent samples.
- No new release was available, so the disabled update button and existing
  successful upgrade history were verified; a new upgrade was not triggered.

Controller rollback backup:
`/opt/zenplus/backups/sensor-detail-20260906-104849`.
The initial `dist` and saved backend `sensors.py` are the pre-feature versions;
`dist-first` and `dist-before-label` are intermediate deployments.
