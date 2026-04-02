# ZenPlus - Network Monitoring System

## Vision
Build a production-grade, scalable network monitoring platform capable of monitoring 10,000+ devices with sub-second latency, real-time dashboards, intelligent alerting, and extensible architecture.

## Why This Stack?

### Go (Polling Engine)
- **Raw performance**: Single raw ICMP socket can dispatch 10K pings in <1 second
- **Concurrency model**: Goroutines + channels are ideal for async network I/O
- **Low memory footprint**: Critical for running on edge/collector nodes
- **Rich networking ecosystem**: pro-bing, gosnmp, goflow

### ClickHouse (Time-Series Metrics)
- **Columnar storage**: 10-100x compression vs row-based DBs for metrics
- **Blazing fast aggregations**: Scans billions of rows/second for dashboards
- **Built-in TTL**: Automatic data retention and rollup without cron jobs
- **Materialized views**: Pre-aggregate at insert time, not query time

### PostgreSQL (Configuration & State)
- **ACID transactions**: Device inventory, users, alert rules need consistency
- **Rich relational model**: Foreign keys, constraints for config integrity
- **Mature ecosystem**: Alembic migrations, SQLAlchemy ORM
- **JSON support**: Flexible metadata storage with JSONB

### FastAPI (API & Dashboard Backend)
- **Async-native**: asyncpg + asynch drivers for non-blocking DB access
- **WebSocket/SSE support**: Real-time metric streaming to dashboards
- **Auto-generated OpenAPI docs**: Self-documenting API
- **Type safety**: Pydantic models catch errors at the boundary

### React + TypeScript (Frontend Dashboard)
- **Largest ecosystem**: Best charting, layout, and component libraries
- **Real-time patterns**: Mature WebSocket/SSE integration
- **shadcn/ui + Tremor**: Purpose-built dashboard components
- **Apache ECharts**: Handles 100K+ data points with WebGL rendering

## Architecture Overview

```
                    +------------------+
                    |   React Frontend |
                    |  (Dashboard UI)  |
                    +--------+---------+
                             |
                        SSE / REST
                             |
                    +--------+---------+
                    |   FastAPI Server  |
                    |  (API Gateway)    |
                    +--+------------+--+
                       |            |
              +--------+--+    +---+---------+
              | PostgreSQL |    |  ClickHouse |
              | (Config)   |    |  (Metrics)  |
              +--------+--+    +---+---------+
                       |            ^
                       |            | gRPC / HTTP
                       v            |
                    +--+------------+--+
                    |   Go Poller      |
                    |  (Ping Engine)   |
                    +------------------+
                             |
                        ICMP / SNMP
                             |
                    +------------------+
                    | Network Devices  |
                    +------------------+
```

## Data Flow

1. **Go Poller** reads device list from PostgreSQL on startup and periodically syncs
2. **Go Poller** sends ICMP pings using single raw socket (async send/receive)
3. Metrics (RTT, packet loss, jitter, status) are batched and inserted into **ClickHouse**
4. Device status changes trigger alerts written to **PostgreSQL**
5. **FastAPI** serves REST API for config CRUD and queries ClickHouse for metrics
6. **React Dashboard** connects via SSE for real-time updates, REST for historical data

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Ping architecture | Single raw socket, async | 10K+ devices/second vs 100 with goroutine-per-device |
| Metrics storage | ClickHouse MergeTree | 10x faster aggregation than TimescaleDB at scale |
| Config storage | PostgreSQL | ACID for inventory, users, alerts |
| API framework | FastAPI async | Native SSE/WebSocket, auto-docs |
| Frontend charts | Apache ECharts | 100K+ points, WebGL, streaming |
| UI components | shadcn/ui + Tremor | Dashboard-focused, lightweight |
| Communication | gRPC between Go<->FastAPI | Type-safe, streaming support |
| Real-time updates | SSE (Server-Sent Events) | Simpler than WebSocket for server->client |
