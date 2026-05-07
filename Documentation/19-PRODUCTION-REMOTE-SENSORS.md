# Production Remote Sensors Design

## Objective

ZenPlus should ship a professional remote sensor that can be downloaded from the main controller as an OVA/OVF appliance, deployed at branch sites or customer networks, receive monitoring assignments from the controller, probe local devices/services, buffer results during WAN outages, and send telemetry back securely.

The best product direction is not a heavy "second controller" at every site. The right model is a lightweight site probe: centrally configured, locally executing checks, outbound-only to the controller, with local durable buffering and controlled auto-update.

## Market Assessment

The leading platforms converge on this model:

- PRTG uses remote probes for multiple locations and firewall-protected networks. Probes receive configuration from the core server, run monitoring locally, and deliver results back over secured connections. PRTG also offers Linux multi-platform probes and mini probes for lighter deployments.
- Zabbix uses proxies for distributed monitoring. Proxies collect performance and availability data for the central server, reduce server load, monitor unreliable remote links, and store data locally before forwarding so temporary communication problems do not lose data.
- LogicMonitor uses Collectors installed on a host in each location. A Collector monitors many assigned devices, encrypts data, and sends it over outbound SSL/HTTPS. It is explicitly not installed on every resource.
- Site24x7 uses an On-Premise Poller for internal networks behind firewalls/VPNs. It collects data at the configured interval and sends it to the central collector. It also supports poller groups for workload distribution and failover.
- Dynatrace ActiveGate acts as an in-network secure proxy and remote monitoring execution point for cloud/data-center technologies, including SNMP, WMI, Prometheus, VMware, Kubernetes, and others.
- Checkmk distributed monitoring uses remote sites connected to a central site. This is stronger for large MSP-style operations but heavier than a lightweight probe.

References:

- PRTG architecture and remote probes: https://www.paessler.com/manuals/prtg/architecture_and_user_interfaces
- Zabbix proxies: https://www.zabbix.com/documentation/current/en/manual/distributed_monitoring/proxies
- LogicMonitor Collector: https://www.logicmonitor.com/support/collectors/collector-overview/about-the-logicmonitor-collector
- Site24x7 On-Premise Poller: https://app.site24x7.com/help/getting-started/on-premise-poller.html
- Dynatrace ActiveGate: https://docs.dynatrace.com/docs/ingest-from/dynatrace-activegate
- Checkmk distributed monitoring: https://docs.checkmk.com/latest/en/distributed_monitoring.html

## Assessment Of Current ZenPlus Implementation

ZenPlus already has a useful Phase 1 foundation:

- PostgreSQL schema for `sites`, `sensors`, and `sensor_assignments`.
- Dashboard/admin APIs for creating sensors, issuing one-time enrollment tokens, rotating keys, enabling/disabling sensors, and assigning devices/service checks.
- Sensor-facing API for enrollment, heartbeat, config pull with ETag, and batched ping/service/SNMP result upload.
- Mock Python sensor for end-to-end protocol validation.
- Dashboard card and inventory reporting surfaces that understand a sensor fleet.

What is missing for production:

- Real compiled sensor runtime. The Python mock must not be the shipping probe.
- OVA/OVF build pipeline for a locked-down appliance image.
- Local durable spool for results when the controller is unavailable.
- Signed remote configuration and signed sensor updates.
- Sensor self-health metrics: CPU, memory, disk, queue depth, check latency, packet loss to controller.
- Workload scheduler with per-check intervals, jitter, max concurrency, and backpressure.
- Proper remote SNMP collection path, including SNMP credentials delivery/handling.
- Version compatibility contract between controller and sensor.
- Fleet lifecycle: revoke, rotate, re-enroll, drain, upgrade ring, rollback.
- HA/failover model for multiple sensors per site or sensor groups.

## Recommended Architecture

Use a two-tier model:

1. **ZenPlus Controller**
   - Source of truth for devices, service checks, alert rules, assignments, credentials, and policy.
   - Stores time-series metrics and status transitions.
   - Generates downloadable sensor packages.
   - Manages enrollment, config versions, key rotation, upgrades, and fleet health.

2. **ZenPlus Sensor Appliance**
   - Lightweight VM deployed near monitored assets.
   - Runs local probe engine for ICMP, TCP, HTTP, TLS, DNS, SNMP, and later synthetic/browser checks.
   - Pulls assigned config from controller.
   - Pushes results outbound over HTTPS/mTLS.
   - Buffers locally when offline.
   - Has no inbound dependency from the controller in default mode.

```
Controller
  |
  | HTTPS/mTLS outbound from sensor
  v
Remote Sensor VM
  |-- ICMP/TCP/HTTP/TLS/DNS probes
  |-- SNMP polling/traps
  |-- local queue/spool
  |-- self-health monitor
  |
  v
Devices, services, network equipment at the site
```

## Deployment Packaging

Primary package:

- OVA/OVF virtual appliance based on minimal Ubuntu LTS or Debian stable.
- 1 vCPU, 1 GB RAM, 12-20 GB disk for small sites.
- 2 vCPU, 2-4 GB RAM, 40 GB disk for medium/high-volume sites.
- Console first-boot wizard for network, hostname, proxy, controller URL, and enrollment token.
- No default SSH password. SSH disabled by default or key-only.
- Systemd-managed `zenplus-sensor` service.
- Local admin CLI: `zenplus-sensor status`, `enroll`, `diagnose`, `logs`, `reset`, `proxy`, `update`.

Secondary packages:

- Linux package for advanced customers who do not want an appliance.
- Container image for Kubernetes/edge compute environments.

Recommendation: ship OVA/OVF first, then `.deb` package, then container. The OVA/OVF gives the most controlled and professional customer experience.

## Sensor Runtime

Recommended implementation:

- Extend the existing Go poller into `sensor mode`, or create a sibling Go binary that reuses checker packages.
- Keep Python only as a dev/test harness.
- Use SQLite or BadgerDB for local durable queue. SQLite is easier to inspect and support.
- Use a local spool table:
  - `result_id`
  - `kind`
  - `target_id`
  - `collected_at`
  - `payload_json`
  - `attempt_count`
  - `next_attempt_at`
  - `expires_at`
- Flush in batches with exponential backoff and server-side backpressure handling.
- Enforce bounded disk usage with oldest-expired drop policy and `queue_dropped_count` heartbeat reporting.

Runtime modules:

- `enroller`: one-time token exchange for sensor API key/certificate.
- `config client`: ETag-aware pull, validates signature/version.
- `scheduler`: interval-aware queue with jitter and concurrency limits.
- `probe workers`: ICMP, TCP, HTTP, TLS, DNS, SNMP.
- `spooler`: local durable queue and retry.
- `uploader`: batched result delivery.
- `self monitor`: local sensor health, controller reachability, clock drift.
- `updater`: signed package update and rollback.

## Communication Model

Default: sensor-initiated outbound HTTPS only.

Required endpoints:

- `POST /api/v1/sensor/enroll`
- `POST /api/v1/sensor/heartbeat`
- `GET /api/v1/sensor/config`
- `POST /api/v1/sensor/results/ping`
- `POST /api/v1/sensor/results/service`
- `POST /api/v1/sensor/results/snmp`
- `POST /api/v1/sensor/events`
- `GET /api/v1/sensor/update/manifest`
- `POST /api/v1/sensor/update/status`

Security progression:

1. Phase A: one-time enrollment token + long-lived API key, stored mode `0600`.
2. Phase B: API key rotation and revocation.
3. Phase C: mTLS with controller-issued client certificate.
4. Phase D: signed config payloads and signed update manifests.

Minimum production requirement should be Phase B plus signed updates. mTLS should be planned early because enterprise customers will ask for it.

## Configuration Contract

Controller sends only the assignments for that sensor:

- Sensor metadata: ID, name, site, version compatibility.
- Devices assigned to sensor.
- Service checks assigned to sensor.
- SNMP profiles/credentials needed by assigned devices only.
- Poll intervals, timeouts, retry thresholds, maintenance windows.
- Upload limits: max batch size, max queue size, heartbeat interval.
- Feature flags based on sensor version.

Config must include:

- `config_version`
- `config_etag`
- `issued_at`
- `expires_at`
- `min_sensor_version`
- `signature`

Sensor behavior:

- Keep last known good config.
- Reject expired, unsigned, or incompatible config.
- Continue monitoring with last known good config if controller is temporarily offline.
- Stop using config after a hard TTL if security policy requires it.

## Result Ownership

Central controller remains the source of truth for status decisions and alerts.

The sensor should calculate local probe outcomes and submit raw/normalized results, but final alert evaluation should remain centralized. This keeps alert rules, maintenance windows, deduplication, escalation, audit logs, and future LLM analysis consistent.

Recommended result flow:

1. Sensor probes target.
2. Sensor writes result to local spool.
3. Sensor uploads batch.
4. Controller validates sensor owns target assignment.
5. Controller writes metrics and status transitions.
6. Controller evaluates alerts.

Sensors can optionally emit local status transition hints, but controller should verify and own the final state.

## HA And Scale Model

Add **sensor groups** after the first production sensor:

- One or more sensors per site.
- Active/standby for small sites.
- Active/active sharding for large sites.
- Controller assignment policy:
  - explicit sensor
  - default sensor per device/check
  - sensor group with hash-based sharding
  - failover to standby after heartbeat timeout

Target capacities:

- Small sensor: 250 devices or 1,000 service checks.
- Medium sensor: 1,000 devices or 5,000 service checks.
- Large sensor: 5,000+ checks with tuned concurrency.

These are starting SLOs; final numbers must come from load testing.

## Security Hardening

Sensor appliance must be locked down:

- Minimal OS packages.
- Non-root `zenplus-sensor` runtime user.
- Capabilities only where needed, such as raw socket capability for ICMP.
- Read-only application directory.
- Secrets under `/etc/zenplus-sensor/` mode `0600`.
- Local firewall default deny inbound.
- SSH disabled by default or key-only.
- Audit local enrollment, config change, key rotation, update, restart.
- Secure boot image process if available.
- Signed OVA checksums published by controller.

Controller-side safeguards:

- Never store plaintext enrollment tokens.
- Show enrollment token once.
- Sensor API keys/certificates revocable.
- Rate limit enrollment and upload endpoints.
- Validate every result target belongs to the submitting sensor.
- Record source IP, version, config version, and queue health.
- Alert when sensor heartbeat is stale, queue depth grows, or clock drift is high.

## OVA/OVF Download Flow

Dashboard flow:

1. Admin creates Site.
2. Admin creates Sensor and enters deployment settings:
   - sensor name
   - site/location
   - controller URL override if the sensor must call a NAT/FQDN address
   - DHCP or static sensor IP/CIDR
   - gateway and DNS servers for static mode
   - optional outbound HTTP/HTTPS proxy
3. Controller issues one-time enrollment token.
4. Dashboard shows:
   - OVA/OVF download link
   - SHA256 checksum
   - enrollment token
   - controller URL
   - bootstrap cloud-init user-data
   - bootstrap NoCloud seed ISO
5. Admin deploys OVA in VMware/VirtualBox/Proxmox.
6. Admin attaches the seed ISO as a CD-ROM before first boot.
7. Cloud-init writes `/etc/zenplus-sensor/sensor.env`, applies optional static network config, and starts `zenplus-sensor.service`.
8. Sensor enrolls, pulls config, starts heartbeat.

Best professional option:

- Generate a per-sensor bootstrap ISO or cloud-init seed with:
  - controller URL
  - enrollment token
  - initial proxy settings
  - site name
  - optional static network config

This avoids manual typing and feels much closer to enterprise appliances.
The OVA remains a generic signed appliance; per-sensor identity and network
settings are carried by the small seed ISO. This is faster and safer than
building a new 576 MB OVA for every sensor submission.

## Repository Structure Added For Build-Out

The production sensor work is structured under:

```text
sensor-appliance/
├── README.md
├── config/zenplus-sensor.env.example
├── packer/zenplus-sensor.pkr.hcl
├── scripts/build-real-ova.sh
├── scripts/build-ova.sh
├── scripts/firstboot.sh
├── scripts/install-sensor.sh
├── scripts/publish-artifacts.sh
└── systemd/zenplus-sensor.service
```

The controller serves appliance artifacts from:

```text
/opt/zenplus/artifacts/sensors/
```

Controller endpoints:

- `GET /api/v1/sensor/appliance/manifest`
- `GET /api/v1/sensor/appliance/ova`
- `GET /api/v1/sensor/appliance/ovf`
- `GET /api/v1/sensor/appliance/sha256`

The dashboard's Add Sensor flow now returns:

- one-time enrollment token,
- controller URL,
- OVA/OVF download links,
- appliance manifest link,
- bootstrap cloud-init content,
- bootstrap NoCloud seed ISO URL,
- development harness install command.

The operator-facing page is:

```text
http://10.12.50.81/settings/general?tab=sensors
```

That tab now includes a permanent **Sensor appliance download** status panel. It shows whether the controller has the OVA, OVF, and SHA256 artifacts published, their sizes, and direct download buttons when available. The status shows `not published` until release engineering publishes real artifacts into `/opt/zenplus/artifacts/sensors/`.

Production build flow:

```bash
cd /opt/zenplus
sensor-appliance/scripts/build-real-ova.sh
sudo sensor-appliance/scripts/publish-artifacts.sh \
  --ova sensor-appliance/out/real-ova/zenplus-sensor.ova \
  --ovf sensor-appliance/out/real-ova/zenplus-sensor.ovf \
  --metadata sensor-appliance/out/real-ova/BUILD-METADATA.json
```

The real build uses the official Ubuntu 24.04 LTS cloud image as the base,
injects the compiled Go `zenplus-sensor` runtime, systemd unit, runtime
directories, least-privilege user, and ICMP capability, then exports a
stream-optimized VMDK inside a bootable OVA. The appliance is configured by
cloud-init/bootstrap content generated from the controller after the operator
creates a sensor enrollment token.

Until the first real OVA is published, the manifest endpoint remains available
and reports artifacts as unavailable. Preview artifacts report `preview`; a
bootable appliance with `BUILD-METADATA.json` type `bootable-ova` reports
`ready`.

Preview publish flow for controller/download testing on a host without Packer/QEMU:

```bash
cd /opt/zenplus
sudo sensor-appliance/scripts/publish-preview-artifacts.sh
```

This publishes:

- `/opt/zenplus/artifacts/sensors/zenplus-sensor.ova`
- `/opt/zenplus/artifacts/sensors/zenplus-sensor.ovf`
- `/opt/zenplus/artifacts/sensors/SHA256SUMS`

The preview OVA is a tar archive containing the compiled sensor runtime, systemd unit, environment template, and install scripts. It is suitable for testing controller download/onboarding flow. It is not a bootable VM until the real appliance image pipeline is completed.

## Runtime Implementation Started

The first real lightweight runtime is now structured as:

```text
poller/cmd/sensor
```

Build command:

```bash
cd /opt/zenplus/poller
/usr/local/go/bin/go build -o /tmp/zenplus-sensor ./cmd/sensor
```

Current runtime capabilities:

- reads `/etc/zenplus-sensor/sensor.env` style environment,
- enrolls with controller using the one-time token,
- stores sensor ID/API key in local state,
- heartbeats to the controller,
- pulls config with ETag support,
- runs assigned device ICMP checks,
- runs assigned HTTP/TCP/TLS/DNS/ICMP service checks through existing Go checker code,
- uploads batched ping and service results.

Still required before commercial release:

- durable local spool,
- remote SNMP result upload path,
- signed config verification,
- mTLS,
- local diagnostics CLI,
- final Packer autoinstall seed and real OVA export automation.

## Future LLM/Agent Approach

Do not put the LLM inside every sensor initially. Keep sensors deterministic and lightweight.

Recommended agent architecture:

- Sensors collect high-quality telemetry, local context, probe traces, and self-health.
- Controller runs the LLM/agent layer centrally.
- LLM agent receives structured incidents:
  - affected targets
  - sensor perspective
  - recent config changes
  - topology/site context
  - SNMP/interface counters
  - failed probe traces
- LLM suggests root cause, runbook steps, and remediation.
- Any active remediation must require policy control and audit.

Later, add a small non-LLM "edge reasoning" module to the sensor for local correlation during WAN outage, but keep full LLM intelligence centralized.

## Implementation Plan

### Phase 1: Production Design Closure

- Freeze sensor API contract.
- Document config/result schemas.
- Add controller-side assignment validation for uploaded results.
- Add sensor heartbeat stale alert.
- Add sensor health dashboard.
- Replace `mock_sensor.py` labeling with "development harness only".

### Phase 2: Real Sensor Runtime

- Build `zenplus-sensor` Go binary.
- Reuse existing Go poller checker packages.
- Implement enrollment, heartbeat, config pull, scheduler, probe workers, local spool, and uploader.
- Support ICMP, TCP, HTTP, TLS, DNS first.
- Add SNMP polling second.
- Add local CLI and systemd unit.

### Phase 3: Appliance Packaging

- Build minimal OVA/OVF image.
- Add first-boot wizard.
- Add bootstrap ISO/cloud-init generation.
- Publish signed image checksum from controller.
- Add dashboard download/onboarding page.

### Phase 4: Fleet Operations

- Add signed sensor updates.
- Add key/cert rotation.
- Add sensor groups, active/standby failover, and assignment rebalancing.
- Add remote diagnostics bundle upload.
- Add version compatibility and rollout rings.

### Phase 5: Intelligent Operations

- Feed sensor perspective into the central event model.
- Add LLM incident summaries and root-cause suggestions.
- Add safe runbook execution with approval.
- Add local edge correlation for offline periods.

## Recommendation

Proceed with the OVA/OVF lightweight sensor appliance approach. It is the same broad pattern used by professional monitoring leaders, and it matches ZenPlus's current architecture.

The key design decision is to keep the remote sensor as a secure execution and buffering node, not as a full controller. Centralize configuration, alerting, reporting, audit, and future LLM intelligence in the main controller. This gives customers a simple deployment model while preserving professional security, consistency, and operability.

Important update:

- The long-term default packaging model should be **one reusable micro sensor OVA per release plus a small controller-generated bootstrap package per sensor**.
- Per-sensor configured OVA generation should remain available only as a compatibility/testing option, not the primary production workflow.
- The detailed lightweight architecture and implementation roadmap is now documented in:

```text
Documentation/20-LIGHTWEIGHT-SENSOR-ARCHITECTURE.md
```
