# ZenPlus - Documentation Index

> A production-grade network monitoring system built with Go, FastAPI, ClickHouse, PostgreSQL, and React.

## Documents

| # | Document | Description |
|---|----------|-------------|
| 01 | [Project Overview](01-PROJECT-OVERVIEW.md) | Vision, architecture, data flow, design decisions |
| 02 | [Tech Stack](02-TECH-STACK.md) | All libraries, versions, project structures |
| 03 | [Database Schema](03-DATABASE-SCHEMA.md) | PostgreSQL + ClickHouse schemas, materialized views, retention |
| 04 | [API Design](04-API-DESIGN.md) | REST endpoints, SSE streams, request/response formats |
| 05 | [UI Design](05-UI-DESIGN.md) | Color palette, page layouts, wireframes, UX patterns |
| 06 | [Task List](06-TASK-LIST.md) | Complete task breakdown for all phases with checkboxes |
| 07 | [Go Poller Design](07-GO-POLLER-DESIGN.md) | Ping engine architecture, data types, scheduling, config |
| 08 | [Deployment](08-DEPLOYMENT.md) | Docker Compose, directory structure, scaling strategy |

## Quick Start (Phase 1)

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env with your passwords

# 2. Start infrastructure
docker-compose up -d postgres clickhouse redis

# 3. Start the poller
cd poller && go run cmd/poller/main.go

# 4. Start the API server
cd server && uvicorn app.main:app --reload

# 5. Start the dashboard
cd dashboard && npm run dev
```

## Phase Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| **Phase 1** | Foundation + Ping Monitoring | **In Progress** |
| Phase 2 | SNMP + Advanced Alerting + Notifications | Planned |
| Phase 3 | Topology + Bandwidth + SLA Reports | Planned |
| Phase 4 | Enterprise (Multi-tenant, RBAC, Distributed) | Planned |
