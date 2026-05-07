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
| 09 | [Startup Guide](09-STARTUP-GUIDE.md) | How to start/stop the system, troubleshooting |
| 10 | [OTA Update System](10-OTA-UPDATE-SYSTEM.md) | Over-the-air update agent, manifests, rollback |
| 11 | [Server-Side Implementation](11-SERVER-SIDE-IMPLEMENTATION-GUIDE.md) | Update server API, release management |
| 12 | [**Appliance Build Guide**](12-APPLIANCE-BASE-SYSTEM.md) | **Complete OVA appliance spec: Base OS + Application deployment** |
| 13 | [**Ship-Ready Master Plan**](13-SHIP-READY-MASTER-PLAN.md) | **Audit + final design + remote-server handoff + pre-ship checklist** |
| 14 | [Remote-Server Intake](14-REMOTE-SERVER-INTAKE.md) | Questions for the zentryc.com team to confirm the contract + run a smoke test |
| 15 | [**Release Runbook**](15-RELEASE-RUNBOOK.md) | **The only doc you need open while cutting a release. Build → sign → push → verify.** |
| 16 | [**Installer & Knowledge-Base Guide**](16-INSTALLER-PUBLIC-GUIDE.md) | **One-liner install spec for the remote-server team to publish on docs.zentryc.com. Prereqs, permissions, OTA wiring, troubleshooting.** |
| 17 | [**Product Enhancement Assessment**](17-PRODUCT-ENHANCEMENT-ASSESSMENT.md) | **Market comparison, product gaps, target architecture, and Phase 1 stabilization plan.** |
| 18 | [**Migration Runner**](18-MIGRATION-RUNNER.md) | **Tracked PostgreSQL migration runner, schema_migrations table, commands, and updater integration.** |
| 19 | [**Production Remote Sensors**](19-PRODUCTION-REMOTE-SENSORS.md) | **Market assessment and OVA/OVF remote sensor appliance design.** |

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
