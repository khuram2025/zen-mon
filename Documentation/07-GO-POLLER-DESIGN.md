# Go Ping Engine - Detailed Design

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Go Poller Process                   │
│                                                       │
│  ┌─────────────┐     ┌──────────────────────────┐   │
│  │  Config      │     │  Device Registry          │   │
│  │  (Viper)     │────>│  (sync from PostgreSQL)   │   │
│  └─────────────┘     └──────────┬───────────────┘   │
│                                  │                    │
│                                  ▼                    │
│  ┌──────────────────────────────────────────────┐   │
│  │           Ping Scheduler                      │   │
│  │  ┌────────┐ ┌────────┐ ┌────────┐           │   │
│  │  │Batch 1 │ │Batch 2 │ │Batch N │  ...      │   │
│  │  │500 devs│ │500 devs│ │500 devs│           │   │
│  │  └───┬────┘ └───┬────┘ └───┬────┘           │   │
│  └──────┼──────────┼──────────┼─────────────────┘   │
│         └──────────┼──────────┘                      │
│                    ▼                                  │
│  ┌──────────────────────────────────────────────┐   │
│  │         ICMP Engine (Single Raw Socket)        │   │
│  │                                                │   │
│  │  Sender Goroutine ──────> Raw Socket ────>    │   │
│  │                                    ↓          │   │
│  │  Receiver Goroutine <──── Raw Socket <────    │   │
│  │         │                                      │   │
│  │         ▼                                      │   │
│  │  Response Matcher (seq_num → device mapping)  │   │
│  └──────────────┬───────────────────────────────┘   │
│                  │                                    │
│         ┌────────┴────────┐                          │
│         ▼                 ▼                           │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │ ClickHouse   │  │ Redis Pub    │                 │
│  │ Batch Writer │  │ (status chg) │                 │
│  └──────────────┘  └──────────────┘                 │
└─────────────────────────────────────────────────────┘
```

## Key Components

### 1. Device Registry
```go
// Syncs device list from PostgreSQL every 60 seconds
type DeviceRegistry struct {
    mu      sync.RWMutex
    devices map[uuid.UUID]*Device
    db      *pgxpool.Pool
}

type Device struct {
    ID           uuid.UUID
    Hostname     string
    IPAddress    net.IP
    PingInterval time.Duration
    PingEnabled  bool
    Status       string  // up, down, degraded, unknown
    LastSeen     time.Time
    LastRTT      float64
    DownCount    int     // consecutive failed pings
}
```

### 2. ICMP Ping Engine
```go
// Single raw socket, async send/receive pattern
type PingEngine struct {
    conn       *icmp.PacketConn
    pending    sync.Map  // seq_num -> *PingRequest
    seqCounter atomic.Int32
    results    chan *PingResult
}

type PingRequest struct {
    DeviceID  uuid.UUID
    IPAddress net.IP
    SeqNum    int
    SentAt    time.Time
    Timeout   time.Duration
}

type PingResult struct {
    DeviceID   uuid.UUID
    IPAddress  net.IP
    IsUp       bool
    RTT        time.Duration
    PacketLoss float32
    Jitter     time.Duration
    MinRTT     time.Duration
    MaxRTT     time.Duration
    Sent       int
    Received   int
    Timestamp  time.Time
}
```

### 3. Ping Scheduler
```go
// Batches devices and staggers ping intervals
type Scheduler struct {
    registry   *DeviceRegistry
    engine     *PingEngine
    batchSize  int           // 500 devices per batch
    tickRate   time.Duration // 50ms between batches
}

// Schedule algorithm:
// 1. Group devices by ping_interval
// 2. For each interval group, divide into batches of 500
// 3. Use time-wheel to stagger batches across the interval
// 4. Example: 10,000 devices at 60s interval
//    = 20 batches × 50ms = 1 second to send all pings
//    = 59 seconds idle before next round
```

### 4. ClickHouse Batch Writer
```go
// Buffers metrics and flushes to ClickHouse in batches
type MetricWriter struct {
    ch        driver.Conn
    buffer    chan *PingResult
    batchSize int           // 1000 metrics per batch
    flushInterval time.Duration // 5 seconds max
}

// Flush triggers:
// 1. Buffer reaches batchSize (1000)
// 2. flushInterval elapsed (5s)
// 3. Graceful shutdown signal
```

### 5. Status Change Detector
```go
// Detects when a device transitions between states
type StatusDetector struct {
    registry    *DeviceRegistry
    alertThreshold int  // consecutive failures before "down" (default: 3)
    degradedThreshold float64  // RTT ms threshold for "degraded" (default: 100ms)
}

// State machine:
// unknown → up (first successful ping)
// up → degraded (RTT > threshold OR packet_loss > 10%)
// up → down (alertThreshold consecutive failures)
// degraded → up (RTT normal AND no packet loss)
// degraded → down (alertThreshold consecutive failures)
// down → up (successful ping)
```

## Configuration (config.yaml)

```yaml
poller:
  id: "poller-01"
  
  # Ping settings
  ping:
    timeout: 3s
    count: 3              # packets per check
    interval: 500ms       # between packets in a single check
    batch_size: 500       # devices per batch
    tick_rate: 50ms       # between batches
    privileged: true      # use raw socket (requires CAP_NET_RAW)
  
  # Status detection
  status:
    down_threshold: 3     # consecutive failures
    degraded_rtt_ms: 100  # RTT threshold for degraded
    degraded_loss_pct: 10 # packet loss threshold

  # Device sync
  device_sync_interval: 60s

# Database connections
postgres:
  host: localhost
  port: 5432
  database: zenplus
  user: zenplus
  password: ${POSTGRES_PASSWORD}
  max_connections: 10

clickhouse:
  host: localhost
  port: 9000
  database: zenplus
  user: default
  password: ${CLICKHOUSE_PASSWORD}
  
  # Batch writer
  batch_size: 1000
  flush_interval: 5s

redis:
  host: localhost
  port: 6379
  db: 0
  password: ${REDIS_PASSWORD}

# Logging
log:
  level: info           # debug, info, warn, error
  format: json          # json, console
  output: stdout

# HTTP health endpoint
health:
  port: 8081
```

## Performance Targets

| Metric | Target | How |
|--------|--------|-----|
| Devices monitored | 10,000+ per poller | Single raw socket, batch scheduling |
| Ping cycle time | <5s for full sweep | 500/batch × 50ms = 1s for 10K |
| Metric insertion | 10K+ rows/second | ClickHouse batch insert (1K per batch) |
| Memory usage | <200MB for 10K devices | Pre-allocated buffers, no per-device goroutines |
| CPU usage | <10% single core | Async I/O, minimal processing |
| Status detection | <10s from failure | 3 consecutive failures × 3s timeout = 9s |

## Linux Capabilities

```bash
# Option 1: Set capability on binary (preferred)
sudo setcap cap_net_raw+ep /usr/local/bin/zenplus-poller

# Option 2: Docker with NET_RAW
docker run --cap-add=NET_RAW zenplus-poller

# Option 3: Unprivileged ICMP (Linux 3.0+)
sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
```
