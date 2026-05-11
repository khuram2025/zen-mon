# Sensor API And Data Contract

## Surfaces

There are two API surfaces.

### Admin API

Dashboard/user authenticated:

```text
GET    /api/v1/sensors
POST   /api/v1/sensors
GET    /api/v1/sensors/{id}
PUT    /api/v1/sensors/{id}
DELETE /api/v1/sensors/{id}
POST   /api/v1/sensors/{id}/regenerate-token
POST   /api/v1/sensors/{id}/rotate-key
POST   /api/v1/sensors/{id}/disable
POST   /api/v1/sensors/{id}/enable
GET    /api/v1/sensors/{id}/assignments
PUT    /api/v1/sensors/{id}/assignments
GET    /api/v1/sites
POST   /api/v1/sites
PUT    /api/v1/sites/{id}
DELETE /api/v1/sites/{id}
```

### Sensor Runtime API

Sensor authenticated:

```text
POST /api/v1/sensor/enroll
POST /api/v1/sensor/heartbeat
GET  /api/v1/sensor/config
POST /api/v1/sensor/results/ping
POST /api/v1/sensor/results/service
POST /api/v1/sensor/results/snmp
POST /api/v1/sensor/events
```

## Authentication

Enrollment:

```json
{
  "enrollment_token": "zps_enr_xxx",
  "hostname": "branch-a-sensor",
  "os_info": "linux/amd64",
  "version": "sensor-0.1.0"
}
```

Response:

```json
{
  "sensor_id": "uuid",
  "api_key": "zps_key_xxx",
  "heartbeat_interval_s": 30,
  "config_poll_interval_s": 60
}
```

Normal sensor requests:

```text
Authorization: Bearer <api_key>
X-Sensor-Id: <sensor_uuid>
```

Server stores only hashes:

- enrollment token hash
- API key hash

Implementation hardening:

- use `hmac.compare_digest` for hash comparison
- rate limit failed enrollment attempts
- audit token generation, enrollment, key rotation, disable/delete
- make enrollment tokens single-use and short-lived

## Heartbeat Contract

Request:

```json
{
  "version": "sensor-0.1.0",
  "uptime_seconds": 3600,
  "queue_depth": 0,
  "queue_dropped_count": 0,
  "hostname": "branch-a-sensor",
  "os_info": "linux/amd64"
}
```

Response:

```json
{
  "ok": true,
  "server_time": "2026-05-11T00:00:00Z",
  "config_etag": "sha256",
  "has_commands": false
}
```

Future heartbeat fields:

- `cpu_pct`
- `memory_pct`
- `disk_free_bytes`
- `upload_latency_ms`
- `controller_rtt_ms`
- `last_config_at`
- `last_upload_at`
- `worker_active_count`
- `worker_error_count`
- `clock_offset_ms`

## Config Contract

Request:

```text
GET /api/v1/sensor/config
If-None-Match: <etag>
```

Response:

```json
{
  "etag": "sha256",
  "sensor_id": "uuid",
  "sensor_name": "branch-a",
  "devices": [
    {
      "id": "uuid",
      "hostname": "router-01",
      "ip_address": "10.0.0.1",
      "ping_enabled": true,
      "ping_interval": 60,
      "snmp_enabled": false
    }
  ],
  "service_checks": [
    {
      "id": "uuid",
      "name": "Customer Portal",
      "check_type": "http",
      "target_host": null,
      "target_port": null,
      "target_url": "https://example.com",
      "http_method": "GET",
      "http_expected_statuses": "200-299",
      "http_content_match": null,
      "http_follow_redirects": true,
      "tls_warn_days": 30,
      "tls_critical_days": 7,
      "check_interval": 60,
      "timeout": 10,
      "retry_count": 1,
      "enabled": true
    }
  ]
}
```

Future config fields:

- `config_version`
- `issued_at`
- `expires_at`
- `min_sensor_version`
- `max_batch_size`
- `max_queue_size_mb`
- `max_concurrency`
- `jitter_pct`
- `signature`

## Result Contract

Ping result batch:

```json
{
  "idempotency_key": "uuid",
  "items": [
    {
      "device_id": "uuid",
      "timestamp": "2026-05-11T00:00:00Z",
      "is_up": true,
      "rtt_ms": 12.4,
      "ip_address": "10.0.0.1"
    }
  ]
}
```

Service result batch:

```json
{
  "idempotency_key": "uuid",
  "items": [
    {
      "service_check_id": "uuid",
      "timestamp": "2026-05-11T00:00:00Z",
      "check_type": "http",
      "is_up": true,
      "response_ms": 94.1,
      "status_code": 200,
      "error": ""
    }
  ]
}
```

Required controller validation:

- sensor is enabled
- API key matches
- every `device_id` is assigned to this sensor or has this sensor as default
- every `service_check_id` is assigned to this sensor or has this sensor as default
- stale timestamps beyond accepted skew are rejected or marked late
- result batch size is bounded
- duplicate idempotency keys are safe

## Storage Mapping

MVP:

- ping results -> ClickHouse `ping_metrics`
- service results -> ClickHouse `service_metrics`
- sensor ID -> existing `poller_id` field
- current status -> Postgres `devices` and `service_checks`

Later:

- add explicit `sensor_id` alias/column where useful
- add `sensor_result_batches` for idempotency and audit
- add `sensor_health_metrics` for queue/cpu/memory/disk
- add `sensor_events` for lifecycle and transitions

## Version Compatibility

Controller should send compatibility metadata:

```json
{
  "min_sensor_version": "0.1.0",
  "recommended_sensor_version": "0.1.2",
  "features": {
    "http": true,
    "tcp": true,
    "tls": true,
    "snmp": false,
    "spool": false
  }
}
```

Sensor behavior:

- if version is too old, heartbeat as degraded and stop applying incompatible config
- if feature unsupported, skip assigned check and report config error
- controller UI should show "sensor update required"
