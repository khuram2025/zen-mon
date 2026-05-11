# ZenPlus Remote Sensors

This folder is the planning and implementation home for ZenPlus remote sensors.

The product decision is:

> A sensor is a lightweight remote poller installed near monitored devices and
> services. It is not a second controller. It receives assignments from the main
> ZenPlus appliance, runs probes locally, and sends results back to the main
> appliance for storage, analytics, reporting, and alerting.

The first delivery should be a simple Ubuntu one-line installer for a real Go
sensor service. After that is stable, we can extend it through OTA updates and
eventually ship a full sensor OVA.

## Documents

- [01-assessment.md](01-assessment.md) - current repo assessment and product direction
- [02-architecture.md](02-architecture.md) - target sensor architecture
- [03-one-line-install.md](03-one-line-install.md) - Ubuntu sensor install flow
- [04-api-data-contract.md](04-api-data-contract.md) - controller/sensor API and data contract
- [05-security-operations.md](05-security-operations.md) - enrollment, auth, update, and operations model
- [06-task-plan.md](06-task-plan.md) - phased engineering task plan

## MVP Scope

The first useful sensor release should support:

- create a sensor from the main appliance
- show a one-line install command
- run that command on an Ubuntu sensor machine
- enroll the sensor with a one-time token
- heartbeat to the main appliance
- pull assigned device/service-check configuration
- run ICMP, TCP, HTTP, and TLS checks
- upload results to the main appliance
- show sensor health, version, last heartbeat, and queue state in the UI
- use the main appliance for analytics, storage, alerting, and reports

## Non-goals For The First Release

- no second dashboard on the sensor
- no independent alerting on the sensor
- no full customer database on the sensor
- no inbound connection requirement from controller to sensor
- no per-sensor OVA build in the basic phase

## Recommended First User Workflow

1. Admin opens `Settings -> Sensors` on the main appliance.
2. Admin clicks `Add Sensor`.
3. Controller creates a sensor record and one-time enrollment token.
4. UI shows a copy button for the Ubuntu one-liner.
5. Admin runs the one-liner on a remote Ubuntu sensor VM.
6. Sensor installs `zenplus-sensor`, enrolls, starts a systemd service, and heartbeats.
7. Admin assigns devices and service checks to the sensor.
8. Sensor monitors assigned targets and pushes results back.

## Research Basis

The design follows the same broad pattern used by mature monitoring systems:

- PRTG remote probes monitor from remote locations and connect back to the core.
- Zabbix proxies collect availability/performance data for remote sites, store
  it locally, and forward it to the server.
- Prometheus Agent mode scrapes locally, disables local alerting/querying, and
  remote-writes to a central system.
- OpenTelemetry Collector uses a single-binary collector with agent/gateway
  deployment patterns.

Primary references are listed in [01-assessment.md](01-assessment.md).
