# Task List - ZenPlus Monitoring System

## Phase 1: Foundation & Ping Monitoring
> Goal: Set up infrastructure, build ping engine, basic dashboard showing device availability

### Task 1.1: Project Scaffolding
- [x] **T1.1.1** Initialize Git repository with `.gitignore`
- [x] **T1.1.2** Create Docker Compose with PostgreSQL, ClickHouse, Redis
- [x] **T1.1.3** Create Go module for poller (`poller/`)
- [x] **T1.1.4** Create FastAPI project structure (`server/`)
- [x] **T1.1.5** Create React + Vite + TypeScript project (`dashboard/`)
- [x] **T1.1.6** Write Dockerfiles for all services
- [x] **T1.1.7** Verify all services start with `docker-compose up`

### Task 1.2: Database Setup
- [x] **T1.2.1** Create PostgreSQL schema (devices, users, alert_rules, alerts)
- [x] **T1.2.2** Set up Alembic migrations for PostgreSQL
- [x] **T1.2.3** Create ClickHouse tables (ping_metrics, rollup tables)
- [x] **T1.2.4** Create ClickHouse materialized views for auto-rollup
- [x] **T1.2.5** Seed database with test devices
- [x] **T1.2.6** Verify retention TTL works correctly

### Task 1.3: Go Ping Engine
- [x] **T1.3.1** Implement configuration loading (Viper + YAML)
- [x] **T1.3.2** Implement PostgreSQL device reader (load device list)
- [x] **T1.3.3** Implement ICMP ping engine using pro-bing
- [x] **T1.3.4** Implement ping scheduler (round-robin batching)
- [x] **T1.3.5** Implement ClickHouse batch writer (metrics insertion)
- [x] **T1.3.6** Implement device status change detection
- [x] **T1.3.7** Implement Redis publisher for real-time status updates
- [x] **T1.3.8** Add graceful shutdown handling
- [x] **T1.3.9** Add health check endpoint (HTTP)
- [x] **T1.3.10** Test: Ping 10+ devices, verify metrics in ClickHouse

### Task 1.4: FastAPI Backend
- [x] **T1.4.1** Set up FastAPI app with async DB connections (asyncpg, asynch)
- [x] **T1.4.2** Implement health check endpoint
- [x] **T1.4.3** Implement device CRUD endpoints
- [x] **T1.4.4** Implement metrics query endpoint (with time range + granularity)
- [x] **T1.4.5** Implement device summary endpoint (counts by status)
- [x] **T1.4.6** Implement SSE stream for real-time metrics (Redis sub)
- [x] **T1.4.7** Implement basic JWT authentication
- [x] **T1.4.8** Add CORS middleware configuration
- [x] **T1.4.9** Test: All endpoints return correct data

### Task 1.5: React Dashboard - Core
- [x] **T1.5.1** Set up Vite + React + TypeScript + Tailwind + shadcn/ui
- [x] **T1.5.2** Implement app layout (sidebar, header, theme toggle)
- [x] **T1.5.3** Implement auth flow (login page, JWT storage, protected routes)
- [x] **T1.5.4** Implement API client with react-query
- [x] **T1.5.5** Implement SSE hook for real-time updates

### Task 1.6: React Dashboard - Pages
- [x] **T1.6.1** Build main dashboard with KPI cards (total, up, down, degraded)
- [x] **T1.6.2** Build response time chart (ECharts time-series)
- [x] **T1.6.3** Build device status heatmap widget
- [x] **T1.6.4** Build active alerts panel
- [x] **T1.6.5** Build devices list page with sorting, filtering, pagination
- [x] **T1.6.6** Build device detail page with metrics charts
- [x] **T1.6.7** Build device add/edit form
- [x] **T1.6.8** Build alert list page
- [ ] **T1.6.9** Build draggable dashboard grid (react-grid-layout) *(deferred to Phase 2)*
- [x] **T1.6.10** Test: Full flow - add device, see it in dashboard, view metrics

### Task 1.7: Integration & Testing
- [x] **T1.7.1** End-to-end test: Device goes down → alert fires → dashboard updates
- [x] **T1.7.2** Load test: Add 100+ devices, verify poller handles them
- [x] **T1.7.3** Verify ClickHouse materialized views produce correct rollups
- [x] **T1.7.4** Verify SSE real-time updates work across multiple browser tabs
- [x] **T1.7.5** Fix any integration issues discovered during testing

---

## Phase 2: Advanced Monitoring & Alerting (Future)
> Goal: SNMP monitoring, advanced alerting, notifications

### Task 2.1: SNMP Monitoring
- [ ] **T2.1.1** Add SNMP poller to Go engine (gosnmp)
- [ ] **T2.1.2** Implement SNMP auto-discovery
- [ ] **T2.1.3** Create ClickHouse schema for SNMP metrics
- [ ] **T2.1.4** Add SNMP configuration to device model
- [ ] **T2.1.5** Build SNMP metrics dashboard widgets

### Task 2.2: Advanced Alerting
- [ ] **T2.2.1** Implement alert rule evaluation engine
- [ ] **T2.2.2** Implement alert escalation (repeat/escalate after N minutes)
- [ ] **T2.2.3** Implement alert dependencies (don't alert on downstream if upstream is down)
- [ ] **T2.2.4** Build alert rule configuration UI

### Task 2.3: Notifications
- [ ] **T2.3.1** Implement email notifications
- [ ] **T2.3.2** Implement webhook notifications
- [ ] **T2.3.3** Implement Slack integration
- [ ] **T2.3.4** Implement Telegram integration
- [ ] **T2.3.5** Build notification channel management UI

---

## Phase 3: Network Intelligence (Future)
> Goal: Topology discovery, bandwidth monitoring, SLA reporting

### Task 3.1: Network Topology
- [ ] **T3.1.1** Implement LLDP/CDP neighbor discovery
- [ ] **T3.1.2** Build automatic topology mapping
- [ ] **T3.1.3** Build interactive topology visualization (Cytoscape.js)
- [ ] **T3.1.4** Implement topology change detection & alerts

### Task 3.2: Bandwidth & Performance
- [ ] **T3.2.1** Implement NetFlow/sFlow collector (Go)
- [ ] **T3.2.2** Implement interface bandwidth monitoring via SNMP
- [ ] **T3.2.3** Build bandwidth utilization dashboards
- [ ] **T3.2.4** Implement Top-N reports (top talkers, top interfaces)

### Task 3.3: SLA & Reporting
- [ ] **T3.3.1** Implement SLA calculation engine
- [ ] **T3.3.2** Build SLA dashboard with uptime percentages
- [ ] **T3.3.3** Implement scheduled PDF report generation
- [ ] **T3.3.4** Build custom report builder UI

---

## Phase 4: Enterprise Features (Future)
> Goal: Multi-tenancy, RBAC, distributed polling, API integrations

### Task 4.1: Enterprise
- [ ] **T4.1.1** Implement multi-tenant support
- [ ] **T4.1.2** Implement granular RBAC (role-based access control)
- [ ] **T4.1.3** Implement distributed poller architecture
- [ ] **T4.1.4** Implement audit logging
- [ ] **T4.1.5** Add LDAP/SAML authentication
- [ ] **T4.1.6** Implement REST API key management
- [ ] **T4.1.7** Build system administration dashboard

---

## Current Status

**Active Phase:** Phase 1 - COMPLETE
**Status:** All services deployed and running on 10.12.50.80
**Last Updated:** 2026-04-02

### Completion Summary
- T1.1 Project Scaffolding: COMPLETE
- T1.2 Database Setup: COMPLETE (PG + CH + materialized views + 29 seed devices)
- T1.3 Go Ping Engine: COMPLETE (ICMP pinging 29 devices every 60s, writing to ClickHouse)
- T1.4 FastAPI Backend: COMPLETE (all endpoints tested, JWT auth working)
- T1.5 React Dashboard Core: COMPLETE (Vite + Tailwind + React 18)
- T1.6 React Dashboard Pages: COMPLETE (dashboard, devices, detail, alerts, login)
- T1.7 Integration Testing: COMPLETE (full data pipeline verified)

### Live Services
- Dashboard: http://10.12.50.80:3000
- API + Swagger: http://10.12.50.80:8000/docs
- Poller Health: http://10.12.50.80:8081/health
- Login: admin / admin123
