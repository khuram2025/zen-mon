# Tech Stack Specification

## Backend - Go Polling Engine

| Component | Library | Version | Purpose |
|-----------|---------|---------|---------|
| ICMP Ping | `github.com/prometheus-community/pro-bing` | latest | Primary ping library with unprivileged socket support |
| Mass Ping | Custom (raw socket) | - | Single-socket async send/receive for 10K+ scale |
| SNMP (Phase 2) | `github.com/gosnmp/gosnmp` | v3+ | SNMPv1/v2c/v3 Get/Walk/BulkWalk |
| gRPC | `google.golang.org/grpc` | latest | Communication with FastAPI |
| Protobuf | `google.golang.org/protobuf` | latest | Message serialization |
| ClickHouse Client | `github.com/ClickHouse/clickhouse-go/v2` | v2+ | Native protocol, batch inserts |
| PostgreSQL Client | `github.com/jackc/pgx/v5` | v5+ | Config reads, device list |
| Configuration | `github.com/spf13/viper` | latest | YAML/env config management |
| Logging | `go.uber.org/zap` | latest | Structured, high-performance logging |
| Scheduler | `github.com/robfig/cron/v3` | v3+ | Cron-based task scheduling |

### Go Module: `github.com/zenplus/poller`

```
poller/
├── cmd/
│   └── poller/
│       └── main.go              # Entry point
├── internal/
│   ├── config/
│   │   └── config.go            # Viper-based configuration
│   ├── pinger/
│   │   ├── pinger.go            # Core ICMP ping engine
│   │   ├── scheduler.go         # Device scheduling & batching
│   │   └── result.go            # Ping result types
│   ├── store/
│   │   ├── clickhouse.go        # ClickHouse metric writer
│   │   └── postgres.go          # PostgreSQL device reader
│   ├── alerter/
│   │   └── alerter.go           # Status change detection & alerts
│   └── api/
│       └── grpc_server.go       # gRPC service for FastAPI
├── proto/
│   └── poller.proto             # gRPC service definitions
├── go.mod
├── go.sum
└── Dockerfile
```

## Backend - FastAPI API Server

| Component | Library | Version | Purpose |
|-----------|---------|---------|---------|
| Framework | `fastapi` | 0.115+ | Async API framework |
| Server | `uvicorn[standard]` | latest | ASGI server with uvloop |
| PostgreSQL | `asyncpg` | latest | Async Postgres driver |
| ClickHouse | `asynch` | latest | Async ClickHouse driver |
| ORM | `sqlalchemy[asyncio]` | 2.0+ | Async ORM for PostgreSQL |
| Migrations | `alembic` | latest | Database schema migrations |
| Validation | `pydantic` | 2.0+ | Request/response models |
| Auth | `python-jose` + `passlib` | latest | JWT tokens + password hashing |
| SSE | `sse-starlette` | latest | Server-Sent Events |
| gRPC Client | `grpcio` + `grpclib` | latest | Communication with Go poller |
| Task Queue | `arq` | latest | Background tasks (Redis-backed) |
| Caching | `redis[hiredis]` | latest | Cache + pub/sub for SSE fan-out |

### FastAPI Project Structure

```
server/
├── app/
│   ├── main.py                  # FastAPI application factory
│   ├── core/
│   │   ├── config.py            # Settings (pydantic-settings)
│   │   ├── security.py          # JWT auth, password hashing
│   │   └── database.py          # DB session factories
│   ├── api/
│   │   ├── v1/
│   │   │   ├── devices.py       # Device CRUD endpoints
│   │   │   ├── metrics.py       # Metric query endpoints
│   │   │   ├── alerts.py        # Alert management
│   │   │   ├── dashboard.py     # Dashboard config
│   │   │   └── auth.py          # Authentication
│   │   └── websocket/
│   │       └── realtime.py      # SSE/WebSocket streams
│   ├── models/
│   │   ├── device.py            # SQLAlchemy device models
│   │   ├── alert.py             # Alert rule models
│   │   └── user.py              # User models
│   ├── schemas/
│   │   ├── device.py            # Pydantic request/response
│   │   ├── metric.py            # Metric schemas
│   │   └── alert.py             # Alert schemas
│   ├── services/
│   │   ├── device_service.py    # Business logic
│   │   ├── metric_service.py    # ClickHouse queries
│   │   └── alert_service.py     # Alert evaluation
│   └── migrations/
│       └── versions/            # Alembic migrations
├── requirements.txt
├── Dockerfile
└── alembic.ini
```

## Frontend - React Dashboard

| Component | Library | Version | Purpose |
|-----------|---------|---------|---------|
| Framework | React + TypeScript | 19+ | UI framework |
| Build Tool | Vite | 6+ | Fast dev server & bundler |
| UI Components | `shadcn/ui` | latest | Base component system (Tailwind) |
| Dashboard Widgets | `@tremor/react` | 3.x | KPI cards, sparklines, stat tables |
| Charts | `echarts` + `echarts-for-react` | 5.5+ | Time-series, gauges, heatmaps |
| Mini Charts | `uplot` | 1.6+ | Lightweight inline sparklines |
| Network Topology | `cytoscape` | 3.30+ | Device topology visualization |
| Grid Layout | `react-grid-layout` | 1.5+ | Draggable dashboard panels |
| State Management | `zustand` | 5.x | Lightweight global state |
| Data Fetching | `@tanstack/react-query` | 5.x | Server state + caching |
| Routing | `react-router` | 7.x | Client-side routing |
| Styling | `tailwindcss` | 4.x | Utility-first CSS |
| Icons | `lucide-react` | latest | Consistent icon set |
| Date/Time | `dayjs` | latest | Lightweight date library |
| Tables | `@tanstack/react-table` | 8.x | Headless data tables |

### Frontend Project Structure

```
dashboard/
├── src/
│   ├── main.tsx                 # App entry
│   ├── App.tsx                  # Root layout + routing
│   ├── components/
│   │   ├── ui/                  # shadcn/ui components
│   │   ├── charts/
│   │   │   ├── TimeSeriesChart.tsx
│   │   │   ├── LatencyGauge.tsx
│   │   │   └── StatusHeatmap.tsx
│   │   ├── topology/
│   │   │   └── NetworkMap.tsx
│   │   ├── dashboard/
│   │   │   ├── DashboardGrid.tsx
│   │   │   ├── DeviceCard.tsx
│   │   │   ├── AlertBanner.tsx
│   │   │   └── StatusIndicator.tsx
│   │   └── layout/
│   │       ├── Sidebar.tsx
│   │       ├── Header.tsx
│   │       └── ThemeToggle.tsx
│   ├── pages/
│   │   ├── Dashboard.tsx        # Main overview
│   │   ├── Devices.tsx          # Device management
│   │   ├── DeviceDetail.tsx     # Single device view
│   │   ├── Alerts.tsx           # Alert management
│   │   ├── Topology.tsx         # Network map
│   │   └── Settings.tsx         # System settings
│   ├── hooks/
│   │   ├── useSSE.ts            # SSE connection hook
│   │   ├── useDevices.ts        # Device queries
│   │   └── useMetrics.ts        # Metric queries
│   ├── stores/
│   │   ├── authStore.ts         # Auth state
│   │   └── dashboardStore.ts    # Dashboard preferences
│   ├── lib/
│   │   ├── api.ts               # API client (fetch wrapper)
│   │   └── utils.ts             # Utilities
│   └── types/
│       └── index.ts             # TypeScript types
├── package.json
├── vite.config.ts
├── tailwind.config.ts
├── tsconfig.json
└── Dockerfile
```

## Infrastructure

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Containerization | Docker + Docker Compose | Local dev & deployment |
| Reverse Proxy | Nginx / Caddy | TLS termination, routing |
| Cache / Pub-Sub | Redis 7+ | SSE fan-out, caching, task queue |
| Database | PostgreSQL 16+ | Configuration store |
| Metrics DB | ClickHouse 24+ | Time-series metrics |

### Docker Compose Services

```yaml
services:
  postgres:     # Port 5432
  clickhouse:   # Port 8123 (HTTP), 9000 (Native)
  redis:        # Port 6379
  poller:       # Go polling engine
  server:       # FastAPI API
  dashboard:    # React (dev: Vite, prod: Nginx)
```
