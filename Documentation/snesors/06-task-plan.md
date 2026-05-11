# Remote Sensors Engineering Task Plan

## Phase 0 - Stabilize Existing Foundation

Goal: make the current controller sensor foundation reliable enough to build on.

### Tasks

1. Verify `migrate-008-sensors.sql` on a fresh appliance and upgraded appliance.
2. Add API tests for:
   - create sensor
   - token TTL and one-time enrollment
   - heartbeat auth
   - config ETag `304`
   - ping/service result upload
   - disabled sensor rejection
3. Add assignment validation before accepting ping/service results.
4. Add constant-time API key hash comparison.
5. Add controller-side stale/offline status job or endpoint logic.
6. Ensure `/api/v1/sensor/config` handles empty assignments cleanly.
7. Verify `SensorsCard` works after current dashboard routing.
8. Rename visible text carefully where needed to "Remote Sensors" to avoid
   confusion with SNMP hardware sensors.

### Acceptance Criteria

- all sensor API tests pass
- a sensor can enroll and heartbeat through APIs
- unassigned result submission is rejected
- disabled sensor cannot upload results

## Phase 1 - Ubuntu One-Line Sensor MVP

Goal: create the basic working product the user described.

### Controller Tasks

1. Build `zenplus-sensor` as part of installer/release pipeline:
   ```bash
   CGO_ENABLED=0 go build -buildvcs=false -o artifacts/sensors/bin/linux-amd64/zenplus-sensor ./poller/cmd/sensor
   ```
2. Serve binary and checksum:
   - `GET /api/v1/sensor/bin/linux-amd64/zenplus-sensor`
   - `GET /api/v1/sensor/bin/linux-amd64/zenplus-sensor.sha256`
3. Replace mock `/api/v1/sensor/install.sh` with real installer.
4. Update `_install_command()` in `server/app/api/v1/sensors.py` to use:
   ```bash
   curl -fsSL <server>/api/v1/sensor/install.sh | sudo env ... bash
   ```
5. Show OS/arch support and token expiry in UI.
6. Add UI "Copy install command" and "Verify enrollment" states.

### Sensor Runtime Tasks

1. Polish `poller/cmd/sensor/main.go`:
   - clear token after enrollment
   - improve logs
   - include version and build commit
   - graceful shutdown
   - backoff on network errors
2. Add per-check jitter.
3. Add max concurrency worker pool.
4. Add better result idempotency keys.
5. Add support for:
   - ICMP
   - TCP
   - HTTP
   - HTTPS/TLS expiry
6. Add service unit hardening.

### Installer Tasks

1. Create `sensor-appliance/scripts/install-linux-sensor.sh` or equivalent.
2. Serve it from `/api/v1/sensor/install.sh`.
3. Install prerequisites.
4. Create `zenplus-sensor` user.
5. Install binary and checksum.
6. Install systemd unit.
7. Start service and print status.

### Acceptance Criteria

- Add Sensor in UI returns one-liner.
- Running one-liner on Ubuntu installs active service.
- Sensor enrolls and appears online.
- Assigning a service check causes sensor results to arrive.
- Controller stores results and status updates.
- Rebooting the sensor preserves enrollment.

## Phase 2 - Production Safety

Goal: make the sensor robust enough for pilot customers.

### Tasks

1. Add SQLite durable queue:
   - `results` table
   - result expiry
   - retry count
   - next attempt
   - max DB size
2. Add uploader with:
   - batch size
   - retry/backoff
   - backpressure handling
3. Add self-health metrics:
   - CPU/memory/disk
   - queue depth
   - drops
   - controller latency
4. Add dashboard health panel.
5. Add controller-side sensor offline/degraded calculation.
6. Add audit logs for lifecycle actions.
7. Add remote diagnostics bundle download.
8. Add signed binary manifest for install script.

### Acceptance Criteria

- WAN outage does not lose queued results inside configured retention.
- Controller shows queue depth and degraded/offline state.
- Installer refuses checksum mismatch.
- Support can collect diagnostics without SSH in normal cases.

## Phase 3 - Sensor Updates

Goal: update sensors centrally after deployment.

### Tasks

1. Define sensor release manifest:
   - version
   - arch
   - binary URL
   - checksum
   - signature
   - changelog
2. Add controller sensor update endpoints:
   - manifest
   - binary download
   - update status report
3. Add sensor updater logic:
   - download new binary
   - verify signature/checksum
   - swap binary safely
   - restart service
   - rollback on failure
4. Add update rings:
   - internal
   - canary
   - stable
5. Add UI for sensor version drift and update status.

### Acceptance Criteria

- controller can publish sensor version `sensor-0.1.1`
- a sensor updates without manual SSH
- failed update rolls back
- UI shows success/failure history

## Phase 4 - OVA And Bootstrap Package

Goal: ship a polished appliance for customers who do not want to prepare Ubuntu.

### Tasks

1. Build a reusable base OVA with:
   - `zenplus-sensor` binary
   - systemd unit
   - cloud-init
   - locked-down OS
2. Generate per-sensor bootstrap ISO:
   - server URL
   - enrollment token
   - sensor name
   - static IP/DNS/proxy if provided
3. Keep base OVA reusable; do not rebuild a full OVA for every sensor.
4. Publish artifacts from controller:
   - base OVA
   - seed ISO
   - checksums
5. Add UI download workflow.

### Acceptance Criteria

- Admin downloads base OVA and per-sensor seed ISO.
- Imported VM enrolls automatically.
- Static IP and proxy bootstraps work.
- No default password exists unless explicitly configured.

## Phase 5 - Enterprise Features

Goal: handle larger customers and MSP-style deployments.

### Tasks

1. mTLS sensor identity.
2. Signed config payloads.
3. Sensor groups:
   - active/standby
   - active/active sharding
   - failover
4. SNMP credentials delivery to sensors with encryption.
5. SNMP polling and traps from sensors.
6. Site-level analytics and reporting filters.
7. Capacity planner:
   - checks per minute
   - estimated sensor load
   - recommended sensor count
8. Policy controls:
   - max queue size
   - max concurrency
   - allowed check types per sensor/site

### Acceptance Criteria

- Large sites can run multiple sensors.
- A failed sensor can fail over assigned checks.
- SNMP checks work from branch networks without exposing credentials broadly.
- Reports can filter by site and sensor.

## Suggested Work Order For The Next Sprint

1. Fix and test controller runtime API hardening.
2. Replace mock install script with real Go sensor installer.
3. Build and serve `zenplus-sensor` binary from controller.
4. Polish Go sensor scheduler and upload path.
5. Add UI copy and status improvements.
6. Test on two Ubuntu VMs:
   - controller appliance
   - remote sensor machine
7. Document the customer-facing install flow.

## First Sprint Deliverables

- PR 1: sensor API tests and assignment validation
- PR 2: real Ubuntu sensor installer endpoint
- PR 3: Go sensor MVP polish
- PR 4: Sensors UI onboarding polish
- PR 5: docs and runbook update

## Demo Script

1. Open main appliance dashboard.
2. Go to `Settings -> Sensors`.
3. Click `Add Sensor`.
4. Copy one-line install command.
5. Run command on Ubuntu sensor VM.
6. Watch sensor turn `online`.
7. Assign one HTTP service check to sensor.
8. Show results updating in service detail and analytics.
9. Stop network from sensor to controller.
10. Show sensor becomes stale/offline.
11. Restore network.
12. Show sensor recovers.
