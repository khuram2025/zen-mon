# Remote Sensor Architecture

## Product Model

```text
Main ZenPlus Appliance
  - customer tenant/controller
  - sensor inventory
  - assignment source of truth
  - credential policy
  - metrics ingestion
  - ClickHouse/Postgres storage
  - dashboards, analytics, reports, alerting
  - OTA/release management
        ^
        | HTTPS from sensor to appliance
        v
Remote Sensor Machine
  - systemd service: zenplus-sensor
  - local check scheduler
  - ICMP/TCP/HTTP/TLS/DNS/SNMP workers
  - local queue/spool
  - config pull client
  - result uploader
  - self-health reporter
        |
        v
Remote site devices and services
```

The sensor initiates all normal communication. The main appliance should not
need inbound access to the sensor.

## Key Design Decisions

### 1. Sensor Is Not A Controller

The sensor should not have:

- its own dashboard
- its own alert rule engine
- long-term metrics database
- user management
- report generation
- customer license handling

It should run probes, buffer results, and upload results.

### 2. Controller Owns Final State

The controller should own:

- device/service status
- alert evaluation
- maintenance windows
- deduplication
- analytics
- reporting
- audit trail

Sensors can submit raw check outcomes and local hints, but the controller
should decide final state.

### 3. Outbound HTTPS First

MVP communication:

- HTTPS from sensor to main appliance
- enrollment token for first registration
- API key for normal operation
- one sensor ID plus one API key per sensor

Later:

- mTLS
- signed config payloads
- signed command channel

### 4. One Binary Runtime

The Go runtime should remain one small service:

```text
zenplus-sensor
  enroller
  config client
  scheduler
  check workers
  result queue
  uploader
  heartbeat/self-health
  diagnostics endpoint or CLI
```

Do not ship the Python mock as product runtime.

## Data Flow

### Enrollment

```text
Admin creates sensor
  -> controller stores token hash and returns plaintext token once
Operator runs one-liner on Ubuntu sensor machine
  -> installer writes env file and starts service
Sensor POST /api/v1/sensor/enroll with token
  -> controller validates token, consumes it, returns sensor_id and api_key
Sensor stores sensor_id/api_key mode 0600
  -> sensor removes plaintext enrollment token from persistent env if possible
```

### Normal Operation

```text
Sensor heartbeat
  -> controller records last_seen, version, queue depth, health

Sensor GET config
  -> controller returns assignments and check policy

Sensor probes assigned targets
  -> ICMP, TCP, HTTP, TLS first

Sensor uploads result batch
  -> controller validates assignment ownership
  -> controller writes ClickHouse metrics
  -> controller updates Postgres current status
  -> controller evaluates alert rules
```

## MVP Runtime Components

### Enroller

Responsibilities:

- read `ZENPLUS_SERVER_URL`, `ZENPLUS_ENROLLMENT_TOKEN`, `ZENPLUS_SENSOR_NAME`
- call `/api/v1/sensor/enroll`
- persist `sensor_id` and `api_key`
- refuse to re-enroll unless state is reset
- clear plaintext token after successful enrollment

### Config Client

Responsibilities:

- call `/api/v1/sensor/config`
- send `If-None-Match`
- persist last good ETag
- keep last good config in memory
- later persist last good config on disk

### Scheduler

Responsibilities:

- schedule checks by per-target interval
- add jitter to avoid synchronized spikes
- enforce max concurrency
- enforce per-check timeout
- avoid overlapping runs for the same target

### Probe Workers

MVP check types:

- ICMP ping
- TCP connect
- HTTP/HTTPS status
- TLS certificate expiry/validity

Next check types:

- DNS
- SNMP poll
- SNMP trap forwarding
- content match enhancements
- multi-step synthetic checks

### Result Uploader

MVP:

- upload in batches every few seconds
- include `idempotency_key`
- retry transient failures in memory

Phase 2:

- SQLite spool
- exponential backoff
- max disk usage
- drop expired results with counter
- upload server backpressure handling

### Heartbeat

Heartbeat payload should include:

- sensor version
- hostname
- OS/arch
- uptime
- queue depth
- dropped count
- check worker count
- last config ETag
- controller latency
- disk free percent
- memory use
- local clock offset if measured

## Controller Components

### Sensor Admin API

Already exists, but needs hardening and tests:

- create sensor
- token regenerate
- key rotate
- assignment management
- enable/disable/delete
- sites

### Sensor Runtime API

Already exists, but needs hardening:

- enrollment
- heartbeat
- config
- result ingestion
- event ingestion
- binary/install artifact endpoints

### Assignment Policy

First release:

- explicit sensor assignment to device or service check
- default sensor ID on device/service check

Later:

- site default sensor
- sensor group
- active/standby
- active/active sharding
- assignment preview and load estimate

### Storage

ClickHouse:

- `ping_metrics.poller_id` can store sensor ID
- `service_metrics.poller_id` can store sensor ID
- later add explicit `sensor_id` alias/column if needed for clarity

Postgres:

- current device/service status
- sensor inventory/status
- assignments
- sensor health summary

## Failure Model

### Sensor Cannot Reach Controller

MVP:

- logs upload failure
- status becomes stale/offline on controller after heartbeat timeout

Phase 2:

- queue results locally
- continue probing with last good config
- upload when controller returns

### Controller Rejects Sensor

Reasons:

- disabled sensor
- invalid API key
- expired token
- assignment mismatch
- incompatible version

Sensor behavior:

- log reason
- keep service running for retryable errors
- stop or enter degraded mode for revoked credentials

### Sensor Machine Reboots

Sensor should:

- start via systemd
- load persisted state
- heartbeat immediately
- pull config immediately
- resume probing

### Update Restarts API

Sensor should:

- back off on `502`, `503`, connection refused
- retry without data loss after queue exists

## Capacity Targets

Initial practical targets:

- small sensor: 250 devices or 1,000 service checks
- medium sensor: 1,000 devices or 5,000 service checks
- heartbeat interval: 30s
- config poll interval: 60s
- upload interval: 5-10s
- max batch size: 500-1,000 results

These are product targets, not guarantees. We should load test and publish
support tiers after Phase 2.
