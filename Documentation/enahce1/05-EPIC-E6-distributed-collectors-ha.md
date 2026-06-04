# E6 — Distributed Collectors + Poller HA/Failover

## 1. Goal & competitive rationale
ZenPlus runs a single poller (`poller/cmd/poller/main.go`) that loads *all* ping-enabled devices on one box and writes straight to ClickHouse/Redis/Postgres — a hard scaling ceiling and a single point of failure. 13/15 competitors (PRTG remote probes, LibreNMS distributed pollers, Zabbix proxies) offer distributed collection and 11/15 offer HA. Distributed collectors unlock monitoring of NAT'd remote sites, horizontal scale past ~10k devices, and per-site fault isolation; poller HA removes the SPOF so a node crash no longer blinds the customer. Together they are table-stakes for mid-market/MSP deals we currently lose.

## 2. Scope
### In scope
- A **collector** build of the existing Go poller that pulls assignment from the API (HTTPS), polls locally, and forwards metrics/events to the central plane.
- **Enrollment** (token → collector_id + api_key), **heartbeat**, and **health** reporting, mirroring the existing `updater` registration pattern.
- **Central assignment/sharding**: devices/service-checks/SNMP targets bound to a collector; rendezvous-hash auto-balancing + manual pinning.
- **Poller HA**: active/standby for the central poller via a Redis lease (single-writer election).
- Dashboard **Collectors** page: list, enroll, health, reassign.
### Out of scope
- Full multi-master poller clustering (Raft) — design for it, defer build.
- mTLS PKI rotation automation, collector auto-update (reuse `updater` later).
- NetFlow/Servers/Agent-Fleet routing through collectors (separate epics).
- Cross-region ClickHouse replication.

## 3. Current state in ZenPlus
- **Single poller, no identity-driven sharding.** `config.Load()` (`poller/internal/config/config.go:65`) sets `Poller.ID` from `POLLER_ID` env (default `"poller-01"`); `PostgresStore.LoadDevices` (`poller/internal/store/postgres.go:50`) returns *every* `ping_enabled` device with no collector filter. `LoadServiceChecks` (`:97`) and `LoadSNMPDevices` (`:181`) are likewise unfiltered.
- **Direct DB coupling.** `main.go` (`poller/cmd/poller/main.go:42-63`) dials Postgres, ClickHouse and Redis directly; `pinger.Engine` (`engine.go:173`) syncs from PG and writes to CH/Redis itself. `poller_id` is already stamped on every CH row (`clickhouse.go:153`, `:288`, `:382`) and every Redis event — good, the data model already carries collector provenance.
- **Health endpoint exists but is local-only.** `startHealthServer` (`main.go:112`) serves `/health` from `engine.HealthStatus()` (`engine.go:268`) — never reported centrally.
- **No registry / enrollment / lease.** No `collectors` table, no heartbeat, no leader election. The alert call in `engine.go:451` hardcodes `http://localhost:8000` — assumes co-location.
- **Reusable enrollment precedent.** `updater/agent.py` + `server/app/api/v1/system_updates.py` (`/register`, `X-Appliance-ID` + `Bearer api_key`) is a working enroll/check-in pattern to mirror for collectors.
- **Missing:** distributed forwarding, assignment, heartbeat, HA. All net-new.

## 4. Target design & architecture
Collectors become **stateless forwarders**: no direct Postgres/ClickHouse/Redis access. They pull config over HTTPS and push results over HTTPS to a new **ingest API** that fans out to CH/Redis centrally.

```
 Remote site (behind NAT)                Central plane
 ┌──────────────────────┐   HTTPS pull   ┌────────────────────────────┐
 │ collector (Go)       │──GET /assign──▶│ FastAPI                    │
 │  pinger/snmp/checker │──POST /ingest─▶│  ├ assignment (PG)         │
 │  + heartbeat loop    │◀─heartbeat────▶│  ├ ingest → CH batch+Redis │
 └──────────────────────┘                │  └ collectors registry     │
                                         └────────────────────────────┘
 Central poller (active)  ── Redis lease "zenplus:poller:leader" ──  (standby idle)
```

- **Mode flag.** New `RunMode` config: `embedded` (today's direct-DB poller, default — back-compat), or `collector` (forward over HTTPS). One binary, selected by `POLLER_MODE`.
- **Assignment.** API computes `collector_id` per device via rendezvous (HRW) hashing over *healthy* collectors, overridable by an explicit `devices.collector_id` pin. Collector calls `GET /api/v1/collector/assignment` and only polls returned targets.
- **Forwarding.** Collector buffers results and POSTs newline-delimited JSON batches to `/api/v1/ingest/{ping,service,snmp,trap,status}`. API reuses the *existing* CH batch-writer logic and Redis channels — same rows, now stamped with the collector's `poller_id`.
- **HA.** Central embedded poller acquires a Redis lease (`SET NX PX` + renew); only the leaseholder runs ping/SNMP cycles. Standby polls the lease and takes over on expiry. Collectors are unaffected (they're independent).

## 5. Data model & migrations
**Postgres** (`scripts/migrate-009-collectors.sql`):
- `collectors(id UUID pk, name, enroll_token_hash, api_key_hash, status TEXT CHECK in('pending','online','offline','draining'), site_id UUID null, version, arch, last_heartbeat TIMESTAMPTZ, last_ip INET, capabilities JSONB, max_devices INT, created_at)`.
- `collector_sites(id UUID pk, name, description)` — logical grouping for NAT'd locations.
- `devices.collector_id UUID NULL REFERENCES collectors(id)` (+ same on `service_checks`). NULL = auto-assign via HRW. Index `idx_devices_collector ON devices(collector_id) WHERE ping_enabled`.
- `collector_heartbeats` optional history (or keep latest only on `collectors`).
- HA: `poller_leases(name TEXT pk, holder TEXT, expires_at TIMESTAMPTZ)` as a fallback if Redis is the lease store of record we skip this; Redis key `zenplus:poller:leader` is primary.

**ClickHouse:** no schema change — `poller_id` columns already exist (`init-clickhouse.sql:17`, `service_metrics.poller_id`, `snmp_metrics.poller_id`). Optionally add a skip-index on `poller_id` for per-collector dashboards. Add `collector_metrics` MergeTree (collector_id, ts, devices_assigned, cycle_ms, queue_depth, drop_count, cpu) for collector observability.

**Migration notes:** additive only; `collector_id` defaults NULL so existing single-poller deploy is unchanged. Backfill a synthetic `collectors` row for `poller-01` so the registry shows the legacy node.

## 6. API changes
New router `server/app/api/v1/collectors.py` (`include_router(..., prefix="/api/v1")` in `main.py`):
- `POST /collector/enroll` — body `{enroll_token, hostname, arch, version, capabilities}` → `{collector_id, api_key}`. Mirrors `system_updates.register`.
- `GET /collector/assignment` — auth `X-Collector-ID`+`Bearer`; returns `{devices:[…], service_checks:[…], snmp_devices:[…], config_version}` filtered to this collector (HRW + pins). ETag/`config_version` so collectors skip no-op syncs.
- `POST /collector/heartbeat` — `{status, devices_active, cycle_ms, queue_depth, version}` → `{assignment_changed:bool}`; updates `last_heartbeat`, marks `online`.
- `POST /ingest/ping` `/ingest/service` `/ingest/snmp` `/ingest/snmp-if` `/ingest/trap` `/ingest/status` — NDJSON batches; validates collector auth, writes to CH + publishes Redis. Returns `{accepted, rejected}`.
- `POST /collector/alert-evaluate` proxy or have collectors call alert engine via central API (replaces hardcoded localhost in `engine.go:451`).
Admin/UI:
- `GET/POST/PATCH/DELETE /collectors` (registry CRUD, RBAC admin-only), `POST /collectors/{id}/drain`, `POST /devices/{id}/assign-collector`.
- `GET /poller/ha-status` — leader id, lease TTL, standbys.

## 7. Poller / collector changes
- **`internal/config/config.go`**: add `RunMode`, `Central{APIBaseURL, EnrollToken, CollectorID, APIKey}`, `Lease{Enabled,Key,TTL}`. Persist enrolled creds to a config file (reuse `updater` `agent.conf` style).
- **New `internal/transport/`**: `client.go` (HTTPS client w/ retry, gzip, auth headers), `assignment.go` (poll + cache), `ingest.go` (NDJSON batch POST with backpressure + on-disk spool when central is unreachable). This becomes an alternate implementation of the existing `MetricWriter`/`EventPublisher`/`SNMPMetricWriter` interfaces (`engine.go:27-79`) — the Engine doesn't change.
- **New `internal/enroll/`**: token→creds handshake; heartbeat goroutine.
- **New `internal/store/api_loader.go`**: implements `DeviceLoader`/`ServiceCheckLoader`/`SNMPLoader` by calling `/collector/assignment` instead of Postgres (write-backs like `UpdateDeviceStatus`, `UpsertInterfaces` go through ingest endpoints).
- **`cmd/poller/main.go`**: branch on `RunMode` — `collector` wires transport-based stores; `embedded` keeps today's path **plus** lease acquisition before `engine.Run`.
- **New `internal/lease/redis_lease.go`**: `SET NX PX`/renew using existing `go-redis` dep; gate `engine.Run` on leadership, release on shutdown.
- Reuse existing `pinger`, `checker`, `checker/snmp` packages untouched. New deps: none required (stdlib `net/http`); optionally `cenkalti/backoff`.

## 8. Dashboard changes
- New route `infrastructure/collectors` + `CollectorsPage.tsx`: table (name, site, status pill, last heartbeat, devices assigned, version), enroll modal (generate token, copy install one-liner), drain/delete, per-collector health sparkline (from `collector_metrics`).
- `CollectorDetailPage.tsx`: assigned devices, queue depth/cycle-time charts, version, reassign.
- Device pages (`DevicesPage.tsx`, `EditDevice.tsx`): "Collector" column + assignment dropdown (Auto / pinned collector).
- Settings → **High Availability** panel: leader id, lease TTL, standby list (`/poller/ha-status`), with a banner if no leader for >30s.
- `hooks/useCollectors.ts`, sidebar entry under a new "Infrastructure" group.

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|------|------|---------|------------|
| 1 | `migrate-009-collectors.sql` (collectors, sites, FK columns, indexes) | db | 1 | — |
| 2 | Enrollment endpoint + token hashing + creds model | api | 2 | 1 |
| 3 | Assignment endpoint + HRW sharding + pin override | api | 3 | 1 |
| 4 | Ingest endpoints reusing CH batch + Redis fan-out | api | 3 | 1 |
| 5 | Heartbeat + collector health/offline reaper | api | 2 | 2 |
| 6 | Go `RunMode`/Central/Lease config + creds persistence | poller | 2 | — |
| 7 | Go `transport/` (client, assignment cache, ingest spool) | poller | 4 | 6 |
| 8 | Go `enroll/` + heartbeat loop | poller | 2 | 6 |
| 9 | `api_loader.go` implementing loader/writer interfaces | poller | 3 | 7 |
| 10 | `lease/redis_lease.go` + gate `engine.Run` (HA) | poller | 3 | 6 |
| 11 | `main.go` mode branching + wiring | poller | 1 | 7,9,10 |
| 12 | Collector packaging (systemd unit, installer one-liner) | infra | 2 | 11 |
| 13 | Collectors registry CRUD + drain/reassign API | api | 2 | 2 |
| 14 | `CollectorsPage` + detail + enroll modal | ui | 4 | 13 |
| 15 | Device collector column/assignment UI | ui | 2 | 13 |
| 16 | HA status endpoint + Settings HA panel | api/ui | 2 | 10 |
| 17 | Integration test harness (2 collectors + standby) | infra | 3 | 11,16 |
| 18 | Docs + back-compat migration of `poller-01` | infra | 1 | 1,11 |

## 10. Acceptance criteria
- [ ] An operator enrolls a collector with a one-line installer; it appears `online` within one heartbeat.
- [ ] A device pinned to collector B is polled only by B; its CH rows carry B's `poller_id`.
- [ ] Auto-assigned devices distribute across healthy collectors via HRW; killing a collector reassigns its devices to survivors within the offline timeout, with no duplicate polling.
- [ ] A NAT'd collector (outbound-only HTTPS) successfully forwards ping/SNMP/service metrics; they render in dashboards identically to embedded mode.
- [ ] Central ingest unreachable → collector spools to disk and replays on recovery (no metric loss under a 5-min outage).
- [ ] With two embedded pollers, exactly one holds the lease and polls; killing the leader promotes the standby within `lease.TTL`.
- [ ] `embedded` mode with no collector config behaves exactly as today (regression).
- [ ] Collector enroll/registry endpoints reject non-admin users (RBAC).

## 11. Test cases
| ID | Type | Precondition | Steps | Expected result |
|----|------|--------------|-------|-----------------|
| T1 | unit | HRW hasher | Hash 10k device IDs over 3 collectors | Even (±5%) distribution, deterministic |
| T2 | unit | HRW hasher | Remove 1 collector, rehash | Only that collector's keys move; others stable |
| T3 | unit | Redis lease | Two instances race `SET NX PX` | Exactly one acquires; other returns not-leader |
| T4 | unit | Lease holder | Stop renewing | Lease expires; standby acquires next tick |
| T5 | integration | Enroll token issued | POST `/collector/enroll` | 200 + `collector_id`+`api_key`; row `online` after heartbeat |
| T6 | integration | Bad/expired token | Enroll with wrong token | 401/403; no row created |
| T7 | integration | Collector enrolled | GET `/assignment` for collector A | Returns only A's targets; respects pin override |
| T8 | integration | Ingest auth | POST `/ingest/ping` NDJSON batch | Rows in `ping_metrics` with correct `poller_id`; Redis `zenplus:metrics` event emitted |
| T9 | integration | Wrong collector creds | POST `/ingest/ping` | 401, zero rows written |
| T10 | e2e | 2 collectors + 200 devices, 100 pinned | Run 10 min | Each polls only its set; CH `poller_id` matches pin; no gaps |
| T11 | e2e | Healthy fleet | `kill -9` collector B | B marked `offline` after timeout; its auto devices repoll on A/C; no double-write |
| T12 | e2e | NAT sim (egress-only) | Run collector behind iptables DROP-inbound | Metrics still forwarded; assignment still pulled |
| T13 | e2e | Central ingest down 5 min | Block API, then restore | Collector spools, replays; ≤0 dropped samples within spool cap |
| T14 | e2e | 2 embedded pollers | Kill leader | Standby promotes within TTL; ping cycles resume; no overlap during transition |
| T15 | manual/regression | No collector config | Start poller as today | Behaves as single embedded poller; all metrics flow |
| T16 | security | Viewer role | Call `/collectors`, `/collectors/{id}/drain` | 403 |
| T17 | security | TLS | Collector → API over plain HTTP | Rejected; only HTTPS accepted |
| T18 | perf | 1 collector, 5k devices | Sustained 60s cycle | Cycle < interval; ingest p95 < 500ms; queue depth bounded |
| T19 | perf | Ingest API | 50k rows/s across 5 collectors | CH batch keeps up; no Redis backlog growth |
| T20 | integration | Drain | `POST /collectors/{id}/drain` | Status `draining`, devices reassigned, no new work sent |

## 12. Risks & rollout
- **Feature flags:** `POLLER_MODE=embedded` default keeps current behavior; `collectors_enabled` server flag gates the new endpoints/UI. HA gated by `lease.Enabled` (off by default for single-node installs).
- **Back-compat/migration:** all schema additive; `collector_id` NULL = legacy auto-poll; seed a `collectors` row for `poller-01`. Old poller binaries keep working until upgraded.
- **Perf:** ingest must not become the new bottleneck — reuse the proven CH batch writers, apply per-collector rate limits and bounded queues; spool-to-disk caps prevent OOM during central outages.
- **Security:** per-collector api_key (hashed at rest, like `updater`), HTTPS-only, optional mTLS phase 2; ingest is authn'd and schema-validated to prevent metric injection; admin-only registry/enroll.
- **Split-brain:** lease is the single source of truth; renew interval ≪ TTL; on Redis loss the leader self-demotes (fail-safe: stop polling rather than risk dual writers). Reassignment uses an offline grace window to avoid flapping.
- **Phased rollout:** (1) ship registry + enroll + ingest behind flag, dogfood one collector alongside embedded poller; (2) enable assignment/sharding for 2–3 design-partner sites; (3) enable Redis-lease HA on multi-node installs; (4) GA + clustering (Raft) as a follow-on epic.
