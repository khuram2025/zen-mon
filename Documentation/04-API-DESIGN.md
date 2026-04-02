# API Design

## Base URL: `/api/v1`

## Authentication
- **POST** `/auth/login` - Login, returns JWT access + refresh tokens
- **POST** `/auth/refresh` - Refresh access token
- **POST** `/auth/logout` - Invalidate refresh token
- **GET** `/auth/me` - Current user profile

## Devices
- **GET** `/devices` - List devices (pagination, filter by status/group/tags)
- **POST** `/devices` - Create device
- **GET** `/devices/{id}` - Get device details
- **PUT** `/devices/{id}` - Update device
- **DELETE** `/devices/{id}` - Delete device
- **POST** `/devices/bulk` - Bulk import devices (CSV/JSON)
- **POST** `/devices/{id}/ping` - Trigger on-demand ping
- **GET** `/devices/{id}/metrics` - Get device metrics (time range, granularity)
- **GET** `/devices/{id}/status-history` - Status change timeline
- **GET** `/devices/summary` - Aggregate stats (total, up, down, degraded)

### Query Parameters for Metrics
```
GET /devices/{id}/metrics?
    from=2026-04-01T00:00:00Z
    &to=2026-04-02T00:00:00Z
    &granularity=5m          # raw, 5m, 1h, auto
    &metrics=rtt,packet_loss  # comma-separated
```

## Device Groups
- **GET** `/groups` - List groups (tree structure)
- **POST** `/groups` - Create group
- **PUT** `/groups/{id}` - Update group
- **DELETE** `/groups/{id}` - Delete group
- **GET** `/groups/{id}/devices` - Devices in group
- **GET** `/groups/{id}/summary` - Group aggregate stats

## Alerts
- **GET** `/alerts` - List alerts (filter: active/acknowledged/resolved)
- **GET** `/alerts/{id}` - Alert detail
- **POST** `/alerts/{id}/acknowledge` - Acknowledge alert
- **POST** `/alerts/{id}/resolve` - Resolve alert
- **GET** `/alerts/stats` - Alert statistics

## Alert Rules
- **GET** `/alert-rules` - List rules
- **POST** `/alert-rules` - Create rule
- **PUT** `/alert-rules/{id}` - Update rule
- **DELETE** `/alert-rules/{id}` - Delete rule
- **POST** `/alert-rules/{id}/test` - Test rule against recent data

## Dashboard
- **GET** `/dashboards` - List user dashboards
- **POST** `/dashboards` - Create dashboard
- **PUT** `/dashboards/{id}` - Update dashboard layout/widgets
- **DELETE** `/dashboards/{id}` - Delete dashboard

## Real-Time Streams
- **GET** `/stream/metrics` - SSE stream of live metrics
- **GET** `/stream/alerts` - SSE stream of new alerts
- **GET** `/stream/status` - SSE stream of device status changes

### SSE Event Format
```json
event: metric
data: {
    "device_id": "uuid",
    "timestamp": "2026-04-02T10:30:00Z",
    "rtt_ms": 12.5,
    "is_up": true,
    "packet_loss": 0.0
}

event: status_change
data: {
    "device_id": "uuid",
    "old_status": "up",
    "new_status": "down",
    "timestamp": "2026-04-02T10:30:00Z"
}

event: alert
data: {
    "alert_id": "uuid",
    "device_id": "uuid",
    "severity": "critical",
    "message": "Device router-01 is down"
}
```

## System
- **GET** `/system/health` - Health check
- **GET** `/system/poller-status` - Poller instances status
- **GET** `/system/stats` - System statistics (devices count, metrics rate)

## Notification Channels
- **GET** `/notification-channels` - List channels
- **POST** `/notification-channels` - Create channel
- **PUT** `/notification-channels/{id}` - Update channel
- **DELETE** `/notification-channels/{id}` - Delete channel
- **POST** `/notification-channels/{id}/test` - Send test notification

## Response Format

### Success
```json
{
    "data": { ... },
    "meta": {
        "total": 150,
        "page": 1,
        "per_page": 50
    }
}
```

### Error
```json
{
    "error": {
        "code": "DEVICE_NOT_FOUND",
        "message": "Device with id '...' not found",
        "details": {}
    }
}
```

## Rate Limiting
- 100 requests/minute for read endpoints
- 30 requests/minute for write endpoints
- SSE connections: max 10 per user
