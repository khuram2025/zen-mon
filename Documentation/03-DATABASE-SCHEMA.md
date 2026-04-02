# Database Schema Design

## PostgreSQL Schema

### devices
```sql
CREATE TABLE devices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname        VARCHAR(255) NOT NULL,
    ip_address      INET NOT NULL UNIQUE,
    device_type     VARCHAR(50) NOT NULL DEFAULT 'unknown',
    -- Types: router, switch, firewall, server, access_point, printer, other
    location        VARCHAR(255),
    group_name      VARCHAR(100),
    tags            JSONB DEFAULT '[]',
    
    -- Monitoring config
    ping_enabled    BOOLEAN DEFAULT TRUE,
    ping_interval   INTEGER DEFAULT 60,          -- seconds
    snmp_enabled    BOOLEAN DEFAULT FALSE,
    snmp_community  VARCHAR(255),
    snmp_version    VARCHAR(5) DEFAULT '2c',
    snmp_port       INTEGER DEFAULT 161,
    
    -- Current state (updated by poller)
    status          VARCHAR(20) DEFAULT 'unknown',
    -- Status: up, down, degraded, unknown, maintenance
    last_seen       TIMESTAMPTZ,
    last_rtt_ms     FLOAT,
    
    -- Metadata
    description     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      UUID REFERENCES users(id)
);

CREATE INDEX idx_devices_status ON devices(status);
CREATE INDEX idx_devices_group ON devices(group_name);
CREATE INDEX idx_devices_tags ON devices USING GIN(tags);
CREATE INDEX idx_devices_ip ON devices(ip_address);
```

### device_groups
```sql
CREATE TABLE device_groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    color       VARCHAR(7),   -- Hex color for UI
    parent_id   UUID REFERENCES device_groups(id),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

### alert_rules
```sql
CREATE TABLE alert_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    description     TEXT,
    enabled         BOOLEAN DEFAULT TRUE,
    
    -- Condition
    metric          VARCHAR(50) NOT NULL,       -- ping_status, rtt, packet_loss
    operator        VARCHAR(10) NOT NULL,       -- eq, gt, lt, gte, lte
    threshold       FLOAT NOT NULL,
    duration        INTEGER DEFAULT 0,          -- seconds condition must persist
    
    -- Scope
    device_id       UUID REFERENCES devices(id),    -- NULL = all devices
    group_name      VARCHAR(100),                    -- NULL = all groups
    
    -- Severity
    severity        VARCHAR(20) DEFAULT 'warning',   -- info, warning, critical
    
    -- Notification
    notify_channels JSONB DEFAULT '[]',  -- ["email", "webhook"]
    cooldown        INTEGER DEFAULT 300, -- seconds between re-alerts
    
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      UUID REFERENCES users(id)
);
```

### alerts (active & historical)
```sql
CREATE TABLE alerts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id         UUID REFERENCES alert_rules(id),
    device_id       UUID REFERENCES devices(id),
    
    status          VARCHAR(20) DEFAULT 'active',  -- active, acknowledged, resolved
    severity        VARCHAR(20) NOT NULL,
    message         TEXT NOT NULL,
    
    triggered_at    TIMESTAMPTZ DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by UUID REFERENCES users(id),
    resolved_at     TIMESTAMPTZ,
    
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX idx_alerts_status ON alerts(status);
CREATE INDEX idx_alerts_device ON alerts(device_id);
CREATE INDEX idx_alerts_triggered ON alerts(triggered_at DESC);
```

### users
```sql
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username        VARCHAR(100) NOT NULL UNIQUE,
    email           VARCHAR(255) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    full_name       VARCHAR(255),
    role            VARCHAR(20) DEFAULT 'viewer',  -- admin, editor, viewer
    is_active       BOOLEAN DEFAULT TRUE,
    last_login      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### notification_channels
```sql
CREATE TABLE notification_channels (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL,
    type        VARCHAR(50) NOT NULL,       -- email, webhook, slack, telegram
    config      JSONB NOT NULL,             -- Channel-specific config
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

### dashboard_configs
```sql
CREATE TABLE dashboard_configs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id),
    name        VARCHAR(255) NOT NULL,
    is_default  BOOLEAN DEFAULT FALSE,
    layout      JSONB NOT NULL,             -- react-grid-layout config
    widgets     JSONB NOT NULL,             -- Widget configs
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
```

---

## ClickHouse Schema

### ping_metrics (Raw data - 30 day retention)
```sql
CREATE TABLE ping_metrics (
    device_id       UUID,
    timestamp       DateTime64(3, 'UTC'),
    
    -- Ping results
    is_up           UInt8,                  -- 1 = up, 0 = down
    rtt_ms          Float64,               -- Round-trip time in ms
    packet_loss     Float32,               -- 0.0 to 1.0
    jitter_ms       Float64,               -- RTT variance
    min_rtt_ms      Float64,
    max_rtt_ms      Float64,
    packets_sent    UInt16,
    packets_recv    UInt16,
    
    -- Context
    poller_id       String,                -- Which poller instance
    ip_address      IPv4
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, timestamp)
TTL timestamp + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;
```

### ping_metrics_5m (5-minute rollup - 90 day retention)
```sql
CREATE TABLE ping_metrics_5m (
    device_id       UUID,
    timestamp       DateTime64(3, 'UTC'),
    
    avg_rtt_ms      Float64,
    min_rtt_ms      Float64,
    max_rtt_ms      Float64,
    avg_packet_loss Float32,
    avg_jitter_ms   Float64,
    uptime_pct      Float32,               -- % of checks that were up
    sample_count    UInt32,
    ip_address      IPv4
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, timestamp)
TTL timestamp + INTERVAL 90 DAY DELETE;
```

### Materialized View: Auto-rollup to 5m
```sql
CREATE MATERIALIZED VIEW ping_metrics_5m_mv
TO ping_metrics_5m
AS SELECT
    device_id,
    toStartOfFiveMinutes(timestamp) AS timestamp,
    avg(rtt_ms)          AS avg_rtt_ms,
    min(min_rtt_ms)      AS min_rtt_ms,
    max(max_rtt_ms)      AS max_rtt_ms,
    avg(packet_loss)     AS avg_packet_loss,
    avg(jitter_ms)       AS avg_jitter_ms,
    avg(is_up)           AS uptime_pct,
    count()              AS sample_count,
    any(ip_address)      AS ip_address
FROM ping_metrics
GROUP BY device_id, toStartOfFiveMinutes(timestamp);
```

### ping_metrics_1h (1-hour rollup - 1 year retention)
```sql
CREATE TABLE ping_metrics_1h (
    device_id       UUID,
    timestamp       DateTime64(3, 'UTC'),
    
    avg_rtt_ms      Float64,
    min_rtt_ms      Float64,
    max_rtt_ms      Float64,
    p95_rtt_ms      Float64,
    avg_packet_loss Float32,
    avg_jitter_ms   Float64,
    uptime_pct      Float32,
    sample_count    UInt32,
    ip_address      IPv4
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, timestamp)
TTL timestamp + INTERVAL 365 DAY DELETE;
```

### Materialized View: Auto-rollup to 1h
```sql
CREATE MATERIALIZED VIEW ping_metrics_1h_mv
TO ping_metrics_1h
AS SELECT
    device_id,
    toStartOfHour(timestamp) AS timestamp,
    avg(avg_rtt_ms)          AS avg_rtt_ms,
    min(min_rtt_ms)          AS min_rtt_ms,
    max(max_rtt_ms)          AS max_rtt_ms,
    quantile(0.95)(avg_rtt_ms) AS p95_rtt_ms,
    avg(avg_packet_loss)     AS avg_packet_loss,
    avg(avg_jitter_ms)       AS avg_jitter_ms,
    avg(uptime_pct)          AS uptime_pct,
    sum(sample_count)        AS sample_count,
    any(ip_address)          AS ip_address
FROM ping_metrics_5m
GROUP BY device_id, toStartOfHour(timestamp);
```

### device_status_log (Status change events)
```sql
CREATE TABLE device_status_log (
    device_id       UUID,
    timestamp       DateTime64(3, 'UTC'),
    old_status      String,
    new_status      String,
    reason          String,
    duration_sec    UInt64      -- How long the previous status lasted
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, timestamp)
TTL timestamp + INTERVAL 365 DAY DELETE;
```

## Data Retention Summary

| Table | Granularity | Retention | Purpose |
|-------|-------------|-----------|---------|
| `ping_metrics` | Raw (per-check) | 30 days | Debugging, detailed analysis |
| `ping_metrics_5m` | 5-minute avg | 90 days | Dashboard charts |
| `ping_metrics_1h` | 1-hour avg | 1 year | Long-term trends, SLA reports |
| `device_status_log` | Event-based | 1 year | Uptime tracking, incident history |

## ClickHouse Compression Settings

```sql
-- Optimized codec for time-series data
ALTER TABLE ping_metrics
    MODIFY COLUMN timestamp CODEC(DoubleDelta, LZ4),
    MODIFY COLUMN rtt_ms CODEC(Gorilla, LZ4),
    MODIFY COLUMN jitter_ms CODEC(Gorilla, LZ4),
    MODIFY COLUMN min_rtt_ms CODEC(Gorilla, LZ4),
    MODIFY COLUMN max_rtt_ms CODEC(Gorilla, LZ4),
    MODIFY COLUMN packet_loss CODEC(Gorilla, LZ4);
```
