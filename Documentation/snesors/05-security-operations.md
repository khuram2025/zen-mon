# Sensor Security And Operations

## Security Principles

- Sensors are customer infrastructure and should be treated as untrusted input.
- The controller must validate every result against assignments.
- Enrollment tokens are secrets and should be shown once.
- API keys are secrets and should be hashed at rest.
- HTTPS is required for production.
- The sensor should not accept inbound control traffic by default.
- Sensor update artifacts should be signed before production rollout.

## Enrollment Security

MVP:

- one-time enrollment token
- 24 hour token expiry
- token hash stored in Postgres
- plaintext token returned only once
- token consumed on enrollment
- sensor receives API key
- API key stored mode `0600`

Hardening tasks:

- use constant-time hash compare
- rate limit enrollment attempts by IP and token hash
- audit token creation, consumption, expiry, regeneration
- remove enrollment token from `/etc/zenplus-sensor/sensor.env` after enroll
- support manual reset/re-enroll

## API Key Lifecycle

Operations:

- rotate API key from controller UI
- disable sensor immediately
- delete sensor and cascade assignments
- regenerate enrollment token for pending or reset sensor

Sensor behavior:

- `401`: log auth failure and retry with backoff
- `403 disabled`: enter disabled state and stop probing
- key rotation command later: receive new key securely and persist

## mTLS Roadmap

Phase 1 does not need mTLS to prove value, but the design should not block it.

Future model:

- controller acts as private CA or delegates to customer CA
- enrollment returns short-lived bootstrap API key plus client certificate
- sensor authenticates with mTLS for all runtime endpoints
- API key can remain as secondary credential during migration

## Config Security

MVP:

- config is served only to authenticated sensor
- config includes only assignments for that sensor

Phase 2:

- include `config_version`, `issued_at`, and `expires_at`
- sign config payload with controller key
- sensor validates signature and stores last good config
- sensor rejects expired or incompatible config

## Update Security

Sensor updates should follow the same principle as appliance OTA:

- signed manifest
- package checksum
- version monotonicity
- rollback on failure
- staged rollout
- update history reported to controller

Do not let a sensor download and execute unsigned arbitrary scripts in
production. The one-line installer can be unsigned during internal testing, but
release artifacts should be verified before enterprise rollout.

## Network Model

Sensor outbound requirements:

```text
sensor -> main appliance: HTTPS 443
sensor -> monitored targets: ICMP/TCP/HTTP/TLS/SNMP as configured
```

No inbound controller-to-sensor connection is required for MVP.

Proxy support:

- `HTTP_PROXY`
- `HTTPS_PROXY`
- `NO_PROXY`

These should be accepted by the installer and propagated to the systemd service.

## Offline And Queue Operations

MVP:

- sensor logs failed uploads
- controller marks stale sensor offline after heartbeat timeout

Phase 2:

- SQLite local queue
- bounded max disk usage
- `queue_depth`
- `queue_dropped_count`
- exponential backoff
- result expiry
- upload backpressure response handling

Recommended queue limits:

- default max size: 256 MB
- default retention: 24 hours
- drop oldest expired first
- report every drop counter in heartbeat

## Observability

Controller UI should show:

- status: pending, online, degraded, offline, disabled
- version
- site
- last heartbeat
- last IP
- queue depth
- dropped result count
- current assignment count
- last config ETag
- latest error

Sensor diagnostics should include:

- controller reachability
- DNS resolution
- TLS validation
- enrollment state
- API auth test
- config pull test
- queue stats
- check worker stats
- recent logs

## Runbook Commands

On the sensor:

```bash
sudo systemctl status zenplus-sensor --no-pager
sudo journalctl -u zenplus-sensor -n 100 --no-pager
sudo cat /var/lib/zenplus-sensor/state.json
sudo cat /etc/zenplus-sensor/sensor.env
```

On the controller:

```bash
sudo zenplus status
curl -fsS http://127.0.0.1:8000/api/v1/system/health
```

## Support States

### Pending

Sensor record exists, token issued, no successful enrollment yet.

### Online

Sensor heartbeated recently and is not disabled.

### Offline

No heartbeat within timeout.

Recommended timeout:

```text
offline_after = max(3 * heartbeat_interval, 120 seconds)
```

### Degraded

Sensor is alive but has one or more issues:

- queue depth above threshold
- upload failures
- config too old
- disk low
- unsupported assigned check type

### Disabled

Controller rejects runtime activity. Sensor should stop probing after receiving
disabled status.
