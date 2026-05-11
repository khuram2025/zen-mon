# Remote Sensors Assessment

## Executive Summary

ZenPlus should implement remote sensors as lightweight, centrally managed probe
agents. The main appliance remains the controller, database, analytics engine,
alerting engine, reporting system, and source of truth.

The first practical delivery should be an Ubuntu one-line installer that runs
on a customer-provided sensor VM:

```bash
curl -fsSL https://<main-appliance>/api/v1/sensor/install.sh | sudo env \
  ZENPLUS_SERVER_URL="https://<main-appliance>" \
  ZENPLUS_ENROLLMENT_TOKEN="<one-time-token>" \
  ZENPLUS_SENSOR_NAME="branch-a" \
  bash
```

After this works reliably, we can extend the same sensor runtime into:

- a signed `.deb` package
- a reusable OVA
- a controller-generated bootstrap ISO
- sensor OTA updates
- mTLS and signed configuration
- local durable buffering
- HA sensor groups

## Market And Technical Pattern

Mature monitoring tools converge on the same model: keep the central server as
source of truth, deploy a remote probe/proxy/agent near the monitored targets,
pull or receive central config, probe locally, buffer during outages, and send
data back to the central system.

Primary references:

- Zabbix Proxy: Zabbix documents proxies as collectors for remote locations,
  unreliable communications, offloading the server, and simplifying distributed
  monitoring. Zabbix also stores collected proxy data locally before forwarding
  to avoid temporary communication loss.
  https://www.zabbix.com/documentation/current/en/manual/distributed_monitoring/proxies
- Prometheus Agent: Prometheus Agent disables local querying, alerting, and full
  TSDB storage, then optimizes the process for scraping and remote writing. It
  keeps temporary WAL buffering when the remote endpoint is down.
  https://prometheus.io/docs/prometheus/latest/prometheus_agent/
- OpenTelemetry Collector: OpenTelemetry documents a single Collector binary
  deployable in agent and gateway patterns.
  https://opentelemetry.io/docs/collector/deploy/
- PRTG Remote Probes: PRTG remote probes initiate connections to the core and
  automatically update in the classic probe model.
  https://www.paessler.com/manuals/prtg/remote_probes_and_multiple_probes

Design lessons for ZenPlus:

- outbound-only from sensor to controller is the right firewall model
- sensors should not own alerting or analytics
- sensors should run checks locally and forward results centrally
- durable buffering is needed after the basic MVP
- central upgrade and config lifecycle must be designed early

## Current ZenPlus Assets

The repo already has a useful base.

### Database

`scripts/migrate-008-sensors.sql` adds:

- `sites`
- `sensors`
- `sensor_assignments`
- `devices.default_sensor_id`
- `service_checks.default_sensor_id`

The schema already supports:

- one-time enrollment token hash
- per-sensor API key hash and display prefix
- pending/online/degraded/offline/disabled states
- version, heartbeat, queue depth, dropped count
- site/location/tags
- assignments to devices, service checks, and groups

### Controller APIs

`server/app/api/v1/sensors.py` already provides dashboard/admin endpoints:

- create/list/update/delete sensors
- generate enrollment token
- rotate API key
- enable/disable sensors
- manage assignments
- manage sites
- generate cloud-init/bootstrap artifacts

`server/app/api/v1/sensor_api.py` already provides sensor runtime endpoints:

- `POST /api/v1/sensor/enroll`
- `POST /api/v1/sensor/heartbeat`
- `GET /api/v1/sensor/config`
- `POST /api/v1/sensor/results/ping`
- `POST /api/v1/sensor/results/service`
- `POST /api/v1/sensor/results/snmp`
- `POST /api/v1/sensor/events`
- `GET /api/v1/sensor/install.sh`

### Sensor Runtime

`poller/cmd/sensor/main.go` already has a basic Go sensor:

- reads controller URL, token, and sensor name from env
- enrolls with the controller
- stores sensor ID and API key
- heartbeats
- pulls config with ETag
- runs device and service checks using existing checker code
- uploads ping and service batches

`scripts/mock_sensor.py` is useful for protocol testing but should not be the
shipping sensor.

### UI

`dashboard/src/components/SensorsCard.tsx` already has the admin flow:

- list sensors
- show status and heartbeat
- add sensor
- show install command/token/bootstrap details
- show appliance download panel
- manage assignments and actions

### Appliance Build

`sensor-appliance/` contains OVA/OVF scaffolding. This is useful later, but the
first practical product milestone should be a Linux one-liner because it is
faster to ship and easier to debug.

## Current Gaps

### Must Fix For MVP

- The served `/api/v1/sensor/install.sh` is still a mock sensor installer.
- The one-liner should install the real Go `zenplus-sensor` service.
- A real sensor binary artifact must be published by the controller.
- The controller should validate that a sensor is allowed to submit results for
  each target ID.
- The Go sensor needs a clearer scheduler model with per-check interval, jitter,
  timeout, retry, and concurrency limits.
- Results should include stable idempotency keys and controller-side de-dupe.
- The dashboard needs a clean sensor onboarding view with copyable one-liner,
  health, assignment state, and test connection feedback.
- The docs should distinguish "sensor" from SNMP hardware sensors to avoid user
  confusion. Prefer "Remote Sensors" in UI text.

### Should Fix Soon After MVP

- Add local durable queue/spool on the sensor.
- Add sensor update mechanism.
- Add signed sensor binary checksums.
- Add signed configuration or config hash verification.
- Add sensor health metrics: CPU, memory, disk, queue depth, upload latency,
  dropped results, controller reachability, clock skew.
- Add offline/degraded status calculation on the controller.
- Add explicit site and sensor filters to dashboards/reports.

### Later Enterprise Work

- mTLS sensor identity.
- sensor groups and HA failover.
- SNMP credential delivery to sensors with encryption and least privilege.
- remote diagnostics bundle.
- OVA and bootstrap ISO production flow.
- proxy support and controlled egress allowlist.
- fleet-wide sensor update rings.

## Product Recommendation

Ship in this order:

1. **MVP Ubuntu one-liner sensor**
   - fastest path to prove customer value
   - reuses existing Go sensor and controller APIs
   - good for internal and friendly-customer tests

2. **Production hardening**
   - durable queue
   - assignment validation
   - checksums/signatures
   - better UI and diagnostics

3. **Sensor OVA**
   - after runtime and APIs are proven
   - uses the same binary, service, and config flow

4. **Enterprise lifecycle**
   - sensor updates
   - mTLS
   - HA groups
   - full remote SNMP and advanced checks
