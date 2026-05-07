# Lightweight Sensor Architecture

## Executive Decision

ZenPlus should replace the current per-sensor Ubuntu OVA build path with a
two-artifact lightweight sensor model:

1. **Immutable Micro Sensor Appliance**
   - One reusable signed OVA/OVF image per ZenPlus sensor release.
   - Built from Alpine Linux or Debian minimal, not Ubuntu cloud image.
   - Contains only the OS, `zenplus-sensor` binary, service files, local queue,
     diagnostics CLI, and first-boot bootstrap reader.

2. **Controller-Generated Bootstrap Package**
   - Small per-sensor package generated instantly by the controller.
   - Contains controller URL, one-time enrollment token, sensor name, site,
     network mode, optional static IP/gateway/DNS, proxy, and console policy.
   - Delivered as NoCloud ISO, VMware OVF properties, or a small signed JSON
     bundle.

This is the professional scalable design. The controller should not rebuild a
full operating-system disk every time an operator clicks **Add Sensor**.

## Why The Current Approach Is Not Optimal

The current configured OVA works functionally, but it is not the right
long-term product design:

- Rebuilding or cloning a full VM image per sensor is slow.
- Each configured OVA is large because it carries the complete OS disk.
- Build failures become part of the normal operator workflow.
- Artifact storage grows quickly with every sensor.
- Configuration is locked into the image instead of being treated as lifecycle
  data.
- Regenerating IP/controller/token requires another image operation.

This is acceptable for proving bootability, but it is not the best production
model.

## Market Research Summary

Professional monitoring products converge on the same pattern: a remote probe,
proxy, collector, or agent runs near the monitored assets, receives central
configuration, buffers locally, and sends results outbound.

### PRTG Remote Probes

PRTG uses local and remote probes. A remote probe monitors devices and services
reachable from its network location, which is required for hosted/cloud PRTG
when internal LAN targets are not publicly reachable. PRTG also recommends
remote probes for multiple locations and to distribute monitoring load.

Design lesson for ZenPlus:

- The remote sensor should be a site-local execution point.
- The controller remains the core.
- The sensor should avoid inbound firewall requirements.

Reference: https://www.paessler.com/manuals/prtg/remote_probes_and_multiple_probes

### Zabbix Proxy

Zabbix Proxy collects performance and availability data on behalf of the Zabbix
server. It is specifically used for remote locations, unreliable links, server
load offload, and simplified distributed monitoring. It stores collected data
locally before sending it to the server and needs only one TCP connection to the
server, simplifying firewall rules.

Design lesson for ZenPlus:

- Local durable buffering is not optional.
- One outbound controller connection is the right enterprise firewall model.
- The sensor must continue polling during WAN outages.

Reference: https://www.zabbix.com/documentation/current/en/manual/distributed_monitoring/proxies

### Checkmk Distributed Monitoring

Checkmk supports distributed monitoring with remote sites. Its reasons include
performance, organization, availability, security-domain separation, and
unreliable or narrow-band links.

Design lesson for ZenPlus:

- Larger customers will eventually need sensor groups, site-level autonomy, and
  remote-site health.
- ZenPlus should start lighter than a full remote site, but keep a path to
  active/active and active/standby sensors per site.

Reference: https://docs.checkmk.com/latest/en/distributed_monitoring.html

### OpenTelemetry Collector

OpenTelemetry Collector is a single binary deployable in different patterns,
including agent and gateway modes. Its purpose is to receive, process, and
export telemetry without requiring users to operate multiple agents and
collectors.

Design lesson for ZenPlus:

- A single small binary with modular receivers/probers/exporters is the right
  runtime shape.
- Keep the sensor runtime boring, deterministic, and observable.

References:

- https://opentelemetry.io/docs/collector/
- https://opentelemetry.io/docs/collector/deployment/

### Prometheus Agent Mode

Prometheus Agent mode disables local querying, alerting, and full TSDB storage,
and optimizes the process for scraping and remote writing. It keeps temporary
WAL buffering for remote write outages and uses fewer resources than a full
Prometheus server.

Design lesson for ZenPlus:

- The sensor should not be a mini controller.
- It should scrape/probe, buffer, and forward.
- Alerting, reporting, and LLM intelligence should stay central.

Reference: https://prometheus.io/docs/prometheus/latest/prometheus_agent/

### Datadog Remote Configuration

Datadog uses remote configuration and fleet automation so agents can receive
configuration changes and participate in central fleet operations.

Design lesson for ZenPlus:

- The controller must manage sensor configuration after deployment.
- The sensor image should not be rebuilt for normal configuration changes.
- Upgrade rings, remote diagnostics, and remote configuration status must be
  planned early.

Reference: https://docs.datadoghq.com/agent/guide/setup_remote_config/

## Recommended Product Architecture

```text
ZenPlus Controller
  - sensor inventory
  - config source of truth
  - assignment policy
  - credentials vault
  - metrics ingestion
  - alerting/reporting/audit
  - future LLM/agent intelligence
        ^
        | outbound HTTPS/mTLS from sensor
        v
Lightweight Sensor Appliance
  - local probe scheduler
  - ICMP/TCP/HTTP/TLS/DNS/SNMP workers
  - durable local queue
  - config pull client
  - self health monitor
  - diagnostics CLI
  - signed updater
        |
        v
Remote site devices, services, network equipment
```

## Packaging Strategy

### Package 1: Base Micro OVA

The base OVA is built once per release and reused for every sensor.

Recommended base:

- **Phase 1 production candidate:** Alpine Linux virt image.
- **Fallback:** Debian minimal if Alpine hardware/VMware support becomes a
  blocker.
- Avoid Ubuntu cloud image for the long-term sensor appliance because it is too
  heavy for this use case.

Target image:

- Compressed OVA: 80-180 MB.
- Installed disk: 700 MB to 1.5 GB.
- Provisioned disk: 4-8 GB small, 16-32 GB medium.
- RAM: 256 MB minimum, 512 MB recommended small, 1-2 GB medium.
- CPU: 1 vCPU small, 2 vCPU medium.

Included packages:

- Linux kernel and bootloader.
- `open-vm-tools` for VMware OVF properties and guest operations.
- `chrony` or minimal time sync.
- `nftables` or equivalent firewall.
- `ca-certificates`.
- `sqlite` runtime library only if needed; prefer pure-Go SQLite or embedded
  Badger/Bolt when stable.
- `zenplus-sensor` static Go binary.
- `zenplus-sensorctl` diagnostics CLI.

Not included:

- Full Python runtime.
- Web server.
- Database server.
- Controller UI.
- Build tools.
- SSH enabled by default.
- Unneeded cloud image packages.

### Package 2: Bootstrap Package

The controller generates this instantly for each sensor.

Supported formats:

1. **Seed ISO**
   - Best cross-hypervisor option.
   - Contains `sensor-bootstrap.json`, network config, and optional console
     user policy.

2. **VMware OVF Properties**
   - Best VMware experience.
   - Controller generates a small customized OVF descriptor with vApp
     properties.
   - Sensor reads OVF environment through `open-vm-tools`.

3. **Signed JSON Download**
   - Best for Linux package/container deployments.
   - Imported with `zenplus-sensorctl enroll --bootstrap file.json`.

Recommended UI wording:

- **Download Base Sensor OVA**
- **Download Sensor Bootstrap ISO**
- **Download VMware OVF Package**
- **Redownload Created Sensor**

The redownload action should always redownload the latest bootstrap package and
links to the matching base OVA. It should not trigger an OS image rebuild.

## First-Boot Configuration Flow

1. Sensor VM boots the immutable base OVA.
2. Bootstrap reader checks, in order:
   - attached seed ISO,
   - VMware OVF environment properties,
   - `/boot/zenplus-bootstrap.json`,
   - manual console wizard.
3. Sensor validates bootstrap signature and expiry.
4. Sensor applies network configuration:
   - DHCP by default,
   - static IP/CIDR/gateway/DNS if provided,
   - proxy if provided.
5. Sensor writes `/etc/zenplus-sensor/sensor.env`.
6. Sensor enrolls with one-time token.
7. Controller returns sensor ID, API key or client certificate, heartbeat
   interval, and configuration endpoint.
8. Sensor pulls assigned config and starts probes.

## Runtime Design

The runtime should be a single Go binary with modules:

- `bootstrap`: reads seed ISO, OVF properties, or local JSON.
- `enroller`: exchanges one-time token for sensor identity.
- `config`: pulls config with ETag, validates signature and version.
- `scheduler`: manages intervals, jitter, concurrency, and backpressure.
- `probe`: ICMP, TCP, HTTP, TLS, DNS, SNMP workers.
- `spool`: durable local queue.
- `uploader`: batches and retries result uploads.
- `health`: reports CPU, memory, disk, queue depth, clock drift, controller
  reachability, and probe latency.
- `diag`: creates support bundle.
- `updater`: signed update, rollback, and rollout ring.

## Local Queue Design

Use SQLite first for supportability.

Tables:

```text
sensor_state
config_cache
result_spool
upload_attempts
sensor_events
```

`result_spool` fields:

```text
id
kind
target_type
target_id
config_version
collected_at
payload_json
attempt_count
next_attempt_at
expires_at
size_bytes
```

Policies:

- Bounded queue size by disk percent and byte limit.
- Drop oldest expired results first.
- Report `queue_depth`, `queue_bytes`, and `queue_dropped_count` every
  heartbeat.
- Continue probing on WAN outage using last known good config.
- Stop using config only after hard TTL if enterprise policy requires it.

## Controller Configuration Model

Controller sends only the assignments owned by that sensor or sensor group:

- sensor identity and site metadata,
- devices,
- service checks,
- SNMP profiles required by those devices only,
- intervals/timeouts/retries,
- maintenance windows,
- upload limits,
- feature flags,
- minimum and maximum compatible sensor versions.

Config payload must include:

```text
config_version
config_etag
issued_at
expires_at
min_sensor_version
max_sensor_version
signature
```

## Security Model

Minimum production security:

- One-time enrollment token shown once.
- API key stored locally with `0600`.
- Controller stores only token/API-key hashes.
- Sensor outbound HTTPS only.
- Per-result assignment validation on controller.
- Local firewall denies inbound by default.
- Runtime user is non-root.
- Linux capabilities only where needed for ICMP.
- Signed update manifests.
- Signed bootstrap packages.

Enterprise security path:

- mTLS with controller-issued client certificates.
- Certificate rotation.
- Secure boot capable image pipeline.
- TPM-backed local secret storage where available.
- RBAC for sensor artifact download/regeneration.
- Audit events for bootstrap download, redownload, enroll, key rotate, revoke,
  update, and config change.

## Scale Model

### Sensor Sizes

Small site:

- 1 vCPU, 512 MB RAM, 8 GB disk.
- 250 devices or 1,000 checks.

Medium site:

- 2 vCPU, 1-2 GB RAM, 16-32 GB disk.
- 1,000 devices or 5,000 checks.

Large site:

- 4 vCPU, 4 GB RAM, 64 GB disk.
- 10,000+ checks after load testing.

### Sensor Groups

Add sensor groups before enterprise release:

- Single sensor for simple sites.
- Active/standby pair for critical sites.
- Active/active sharding for large sites.
- Hash target assignment across sensors.
- Failover after heartbeat timeout.
- Rebalance with drain mode.

### Controller Ingestion

Controller-side ingestion should use:

- batch upload endpoint,
- idempotency keys,
- assignment validation,
- ingestion queue,
- async workers,
- rate limiting per sensor,
- backpressure response when overloaded.

## LLM/Agent Future

Do not run a full LLM on every sensor.

Correct model:

- Sensors stay deterministic and lightweight.
- Controller runs central LLM/agent intelligence.
- Sensor contributes local perspective:
  - probe traces,
  - target reachability from that site,
  - WAN/controller reachability,
  - queue depth and drops,
  - recent config version,
  - SNMP/interface counters,
  - local DNS/TLS/HTTP failure details.

Future edge intelligence:

- Add small rule-based correlation at the sensor during WAN outage.
- Add optional local anomaly scoring only after the base sensor is stable.
- Keep remediation policy and approval central.

## UI And API Changes Required

### Sensors UI

The Sensors tab should show:

- Base sensor OVA status and version.
- Sensor runtime version.
- Bootstrap package status for each sensor.
- Redownload button per sensor.
- Regenerate bootstrap button.
- Revoke sensor button.
- Last enrollment status.
- Last heartbeat.
- Queue depth.
- Config version.
- IP/controller/proxy settings used at bootstrap.

### Add Sensor Flow

The Add Sensor form should create a sensor record and save deployment metadata:

- controller URL,
- network mode,
- static IP/CIDR/gateway/DNS,
- proxy,
- console access policy,
- selected base image version,
- selected deployment format.

The controller should then generate:

- bootstrap ISO,
- bootstrap JSON,
- VMware OVF package if VMware mode is selected,
- download links.

### Redownload Existing Sensor

Redownload should not rebuild the OS image.

It should return:

- current base OVA link,
- latest bootstrap ISO link,
- latest bootstrap JSON link,
- latest VMware OVF package link,
- artifact creation time,
- bootstrap expiry status,
- config preview.

If bootstrap has expired, UI should offer:

- **Regenerate Bootstrap**
- **Keep same network settings**
- **Edit network settings**

## Implementation Roadmap

### Phase A: Stop Per-Sensor OVA Rebuild As Main Path

- Keep current configured OVA endpoint as a compatibility/testing option.
- Mark it as legacy/advanced.
- Make Base OVA + Bootstrap ISO the default UI path.
- Store deployment metadata in the database so redownload/regenerate works
  even after the original token dialog is closed.

### Phase B: Micro Appliance Build

- Build Alpine-based OVA.
- Static compile `zenplus-sensor`.
- Add bootstrap reader.
- Add OpenRC/systemd equivalent service.
- Add firewall and locked-down user.
- Target compressed OVA below 180 MB.

### Phase C: Durable Runtime

- Add local SQLite spool.
- Add upload retry/backpressure.
- Add self-health reporting.
- Add `zenplus-sensorctl status`, `diag`, `enroll`, `reset`.

### Phase D: Professional Fleet Operations

- Add sensor groups.
- Add active/standby and active/active assignment policies.
- Add signed config.
- Add signed updates and rollout rings.
- Add mTLS.

### Phase E: LLM-Ready Incident Intelligence

- Normalize sensor perspective into incidents.
- Add root-cause context bundle.
- Add LLM incident explanation and runbook suggestions centrally.
- Add audited remediation approvals.

## Final Recommendation

The optimal ZenPlus path is:

```text
One tiny immutable sensor OVA per release
+ small signed controller-generated bootstrap package per sensor
+ continuous central configuration pull
+ local durable queue
+ outbound-only secure upload
+ controller-side intelligence and fleet management
```

This gives ZenPlus the professional behavior of market leaders while keeping
the sensor lightweight enough for branch offices, MSP customers, and restricted
network zones.

