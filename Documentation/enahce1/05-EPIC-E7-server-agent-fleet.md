# E7 — Server/Host Agent Fleet Maturation + App/DB Checks

## 1. Goal & competitive rationale
ZenPlus monitors network devices well (ICMP/SNMP/service checks) but has no real server/host visibility — the live "Servers" and "Agent Fleet" modules ship empty (0 hosts, 0 agents). Datadog, PRTG, LibreNMS-with-agents, and Zabbix all win deals on host-level CPU/mem/disk/process/service telemetry plus managed-agent rollout. This epic delivers a cross-platform host agent, agentless WMI/SSH collection, fleet enrollment with ringed/staged rollout and auto-upgrade, and app/DB checks (HTTP/process/port/Windows-service/systemd) so ZenPlus competes for the full-stack monitoring buyer rather than just network ops.

## 2. Scope
### In scope
- Cross-platform host agent (Windows + Linux) reporting CPU/mem/disk/network/process/service inventory.
- Agentless collection: WMI/WinRM (Windows), SSH (Linux) for hosts where agents can't be installed.
- Agent enrollment reusing the proven sensor token model; policy + ring-based staged rollout; auto-upgrade with version pinning/rollback.
- App/DB checks: HTTP, TCP port, process-present, Windows-service-state, systemd-unit-state, plus a generic DB-ping (TCP+optional auth handshake) check type.
- Host inventory pages, fleet/policy management UI, host detail with live metrics.

### Out of scope
- Deep APM/tracing, log collection/shipping, eBPF.
- Per-query DB performance metrics (only liveness/port/handshake here).
- Automated remediation/runbooks; container/k8s node agents (future epic).
- Agent code-signing certificate procurement (infra/legal track, flagged as risk).

## 3. Current state in ZenPlus
Verified present:
- **Remote-collector spine exists and is mature.** `server/app/api/v1/sensor_api.py` implements `/sensor/enroll`, `/sensor/heartbeat`, `/sensor/config` (ETag via `_config_etag`), and `/sensor/results/{ping,service,snmp}` with per-sensor API-key auth (`_authenticate`, sha256 of `sensors.api_key_hash`). `sensors.py` is the admin CRUD + enrollment-token issuance (`_new_enrollment_token`, `regenerate-token`, `rotate-key`).
- **Schema:** `scripts/migrate-008-sensors.sql` defines `sites`, `sensors`, `sensor_assignments`; models in `server/app/models/sensor.py` (`Sensor`, `Site`, `SensorAssignment`). `device_sensor.py` is unrelated (per-device SNMP sub-sensors).
- **Go checker dispatch** in `poller/internal/checker/checker.go` (`CheckOne` switch over http/tcp/tls/icmp/dns) + `checker/http.go`, `tcp.go`, `snmp/collector.go`. Engine wiring in `poller/cmd/poller/main.go`.
- **ClickHouse conventions:** `scripts/init-clickhouse.sql` + `migrate-004-snmp-clickhouse.sql` (MergeTree, `toYYYYMM` partition, 5m/1h materialized-view rollups, TTL).
- **Dashboard routing** in `dashboard/src/App.tsx` (flat `<Route>` under a guarded shell).

Missing locally: **no Servers/Hosts module, no Agent Fleet/Policy/Ring backend, no WMI/SSH collectors, no process/service/DB check types.** The Go poller only does network checks; the agent is a separate binary that doesn't exist here. We design these from first principles, reusing the sensor enroll/heartbeat/config/results pattern verbatim.

## 4. Target design & architecture
We treat a **host agent as a specialized sensor** — same enrollment/heartbeat/config/results envelope, new payload types — so we inherit auth, ETag config pull, and batched ingest.

```
 Host Agent (Go, win/linux)            Agentless Collector (lives in poller)
   collect cpu/mem/disk/proc/svc          WMI/WinRM | SSH exec -> parse
        │ batched POST                         │ writes same metrics
        ▼                                      ▼
  /api/v1/agent/{enroll,heartbeat,config,results,upgrade-check}
        │ FastAPI                              ▲ policy/ring resolution
        ▼                                      │
  Postgres(config: hosts, agent_policies, agent_rings, host_checks)
  ClickHouse(host_metrics, host_check_metrics)   Redis(live status)
```

- **Poller/agent**: new `agent` binary under `poller/cmd/agent`, sharing `internal/checker`. New `internal/hostmetrics` (gopsutil) and `internal/checker/{process,winservice,systemd,dbping}.go`. Agentless lives server-side-adjacent in poller: `internal/checker/wmi` and `internal/checker/ssh` run on an existing sensor/poller against remote hosts.
- **API**: new `/agent/*` runtime router (mirrors `sensor_api.py`) + admin `/hosts`, `/agent-policies`, `/agent-rings`, `/host-checks`.
- **Rollout engine**: `config` response carries `desired_version` + `policy` resolved from the host's ring; agent self-updates on `upgrade-check`.

## 5. Data model & migrations
**Postgres — `migrate-009-hosts-agents.sql`:**
- `hosts(id, name, ip, os_family, os_version, collection_mode['agent'|'wmi'|'ssh'], site_id FK, status, agent_id FK NULL, last_seen_at, tags jsonb)`.
- `host_agents(...)` — extend the sensor pattern: add `kind ENUM('sensor','host_agent')` to `sensors` (CHECK), plus `agent_version`, `desired_version`, `ring_id FK`, `upgrade_state`. Reuse `sensors` rather than fork to inherit enroll code.
- `agent_rings(id, name, order_index, rollout_percent, auto_upgrade bool, pinned_version)`.
- `agent_policies(id, name, ring_id FK, collect_interval_s, processes jsonb, services jsonb, config jsonb)`.
- `host_checks(id, host_id FK, check_type['http'|'port'|'process'|'win_service'|'systemd'|'db_ping'], target jsonb, interval, timeout, enabled, status, last_*)`.
- `host_credentials(id, host_id, scheme['winrm'|'ssh'], username, secret_enc bytea)` — reuse the SNMP-cred encryption path (`poller/internal/checker/snmp/crypto.go` analog).
- Indexes: `host_checks(host_id, enabled)`, `hosts(status)`, `host_agents(ring_id)`.

**ClickHouse — `migrate-009-hosts-clickhouse.sql`** (follow `migrate-004` exactly):
- `host_metrics(host_id UUID, metric_key LowCardinality(String), value Float64, unit LowCardinality(String), timestamp DateTime64(3), agent_id String)` — MergeTree, `toYYYYMM` partition, 30d TTL + `_5m`/`_1h` MVs.
- `host_process_inventory` (ReplacingMergeTree by host_id+pid+ts, 7d TTL).
- `host_check_metrics(host_check_id, host_id, timestamp, check_type, is_up, response_ms, status_code, error_message, agent_id)` — mirrors `service_metrics`.

Migration notes: additive/idempotent (`IF NOT EXISTS`, `DO $$` guards like 008); the `sensors.kind` default `'sensor'` keeps existing rows valid.

## 6. API changes
Runtime (agent bearer auth, mirrors `sensor_api.py`):
- `POST /api/v1/agent/enroll` — token→api_key; body adds `os_family`, `agent_version`. Resp: `{agent_id, api_key, ring}`.
- `POST /api/v1/agent/heartbeat` — body `{agent_version, uptime, queue_depth}`; resp adds `config_etag`, `desired_version`.
- `GET /api/v1/agent/config` — ETag-aware; returns assigned `host_checks`, `processes`/`services` to watch, `collect_interval_s`, policy.
- `POST /api/v1/agent/results/host` — batched CPU/mem/disk/proc → `host_metrics`/`host_process_inventory`.
- `POST /api/v1/agent/results/host-check` — batched check outcomes → `host_check_metrics`.
- `GET /api/v1/agent/upgrade-check` — `{current}` → `{desired_version, artifact_url, sha256, rollback_to}`.

Admin (JWT, mirrors `sensors.py`):
- `GET/POST/PUT/DELETE /api/v1/hosts`, `/api/v1/host-checks`, `/api/v1/agent-policies`, `/api/v1/agent-rings`.
- `POST /api/v1/hosts/{id}/test-credentials` — agentless WMI/SSH connectivity probe.
- `POST /api/v1/agent-rings/{id}/promote` — advance staged rollout %.

## 7. Poller / collector changes
- New module `poller/cmd/agent/main.go` reusing `internal/config`, `internal/store/clickhouse.go` batch insert, and an HTTP push client (model after the sensor result path).
- `poller/internal/hostmetrics/collect.go` — `github.com/shirou/gopsutil/v3` for cpu/mem/disk/net/process.
- `poller/internal/checker/process.go`, `winservice.go` (Windows SCM via `golang.org/x/sys/windows/svc/mgr`), `systemd.go` (dbus or `systemctl is-active`), `dbping.go` (TCP + optional pg/mysql handshake), `port.go`; register in `checker.go` `CheckOne` switch.
- `poller/internal/checker/ssh/` (`golang.org/x/crypto/ssh` running probe commands) and `wmi/` (WinRM via `github.com/masterzen/winrm`) for agentless mode — invoked by sensor/poller, not the host agent.
- `poller/internal/upgrade/` — download artifact, verify sha256, atomic swap, supervised restart, rollback on failed heartbeat.

## 8. Dashboard changes
New pages under `dashboard/src/pages/`: `HostsPage.tsx`, `HostDetailPage.tsx` (live CPU/mem/disk gauges + process/service tables, reusing chart components), `AgentFleetPage.tsx` (agents by ring/version, rollout %), `AgentPoliciesPage.tsx`, `AgentRingsPage.tsx`, `HostChecksPage.tsx`. Add routes in `App.tsx` (`hosts`, `hosts/:id`, `fleet`, `fleet/policies`, `fleet/rings`). Add agent-install modal (copy-paste one-liner, same UX as sensor enrollment). Nav entry "Servers".

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|------|------|---------|-----------|
| 1 | Migrations 009 PG (hosts/policies/rings/checks/creds; `sensors.kind`) | db | 2 | — |
| 2 | Migrations 009 ClickHouse (host_metrics + rollups, check_metrics) | db | 1.5 | — |
| 3 | `/agent/*` runtime router (enroll/heartbeat/config/results) | api | 4 | 1,2 |
| 4 | Admin hosts/host-checks/policies/rings CRUD | api | 4 | 1 |
| 5 | Rollout/ring resolution + upgrade-check endpoint | api | 3 | 3 |
| 6 | Agent skeleton `cmd/agent` + push client + enroll | poller | 4 | 3 |
| 7 | `hostmetrics` collector (gopsutil) | poller | 3 | 6 |
| 8 | Check types: process/port/win_service/systemd/db_ping | poller | 5 | 6 |
| 9 | Agent auto-upgrade (download/verify/swap/rollback) | poller | 4 | 5,6 |
| 10 | Agentless SSH collector | poller | 3 | 4 |
| 11 | Agentless WMI/WinRM collector | poller | 4 | 4 |
| 12 | Hosts list + Host detail (live metrics/process/service) UI | ui | 5 | 4 |
| 13 | Agent Fleet + Policies + Rings UI | ui | 5 | 4,5 |
| 14 | Host-checks CRUD UI + install modal | ui | 3 | 4 |
| 15 | Credential encryption + test-credentials probe | api/poller | 2 | 4,10,11 |
| 16 | Cross-platform build/sign/packaging (msi/deb) + CI | infra | 4 | 6 |
| 17 | Alert-rule integration for host metrics/checks | api | 2 | 2,3 |

## 10. Acceptance criteria
- [ ] Operator creates a host, gets a one-line install command; Windows + Linux agents enroll and turn `online`.
- [ ] Host detail shows live CPU/mem/disk/network and a process/service list updating within one collect interval.
- [ ] Agentless WMI and SSH hosts report the same core metrics without an installed agent; `test-credentials` validates before save.
- [ ] App/DB checks (http/port/process/win_service/systemd/db_ping) produce up/down status and history.
- [ ] Assigning agents to a ring with rollout % staged-upgrades only that fraction; failed upgrade auto-rolls back and surfaces `upgrade_state=failed`.
- [ ] Stale agents (no heartbeat > N) flip to `offline`; non-admins are read-only on fleet/policy edits.
- [ ] Host metric thresholds raise alerts via existing alert engine.

## 11. Test cases
| ID | Type | Precondition | Steps | Expected result |
|----|------|--------------|-------|-----------------|
| T1 | e2e | Linux VM, valid token | Run install one-liner | Agent enrolls, status online, version recorded |
| T2 | e2e | Windows VM, valid token | Install MSI | Enrolls; win_service checks available |
| T3 | integration | Enrolled agent | POST results/host batch | Rows in `host_metrics`; detail shows gauges |
| T4 | unit | gopsutil mock | Collect cpu/mem/disk | Correct keys/units emitted |
| T5 | integration | host_check process=nginx | Stop nginx | Check flips down, history logged |
| T6 | integration | systemd unit active | `systemctl stop` | systemd check down within interval |
| T7 | integration | Windows svc running | Stop service | win_service check down |
| T8 | integration | db_ping to Postgres:5432 | Block port | is_up=false, error captured |
| T9 | e2e | Agentless SSH host + creds | Save + run | Metrics collected, no agent installed |
| T10 | e2e | Agentless WMI host | test-credentials then collect | Probe ok; metrics flow |
| T11 | integration | Bad SSH creds | Save | test-credentials returns auth failure, not saved |
| T12 | e2e | Ring rollout 25%, 4 agents | Promote | Exactly 1 agent upgrades, others pinned |
| T13 | e2e | Agent on bad version | Push artifact w/ wrong sha256 | Verify fails, rollback, upgrade_state=failed |
| T14 | integration | Online agent | Stop heartbeats > threshold | Status→offline |
| T15 | security | Valid api_key | Call /agent/config with wrong X-Agent-Id | 401 |
| T16 | security | Expired enrollment token | enroll | 401 token expired |
| T17 | rbac | Viewer role | PUT /agent-rings | 403 |
| T18 | perf | 500 agents, 60s interval | Sustain ingest | <500ms p95 result POST, no ClickHouse backlog |
| T19 | regression | Existing sensors present | Run migration 009 | sensor enroll/heartbeat unchanged, kind='sensor' |
| T20 | unit | ETag config | Unchanged policy | 304 returned (matches `_config_etag`) |

## 12. Risks & rollout
- **Feature flags:** `hosts`, `agent_fleet`, `agentless` gated; ship metrics-only first, then checks, then rollout engine.
- **Back-compat:** reusing `sensors` via additive `kind` column keeps the live sensor flow untouched (T19); all migrations idempotent like 008.
- **Security:** WMI/SSH/DB credentials encrypted at rest (reuse SNMP `crypto.go` pattern); agent api-keys hashed; signed upgrade artifacts (sha256 mandatory, code-signing cert is an infra/legal dependency and a launch risk for Windows SmartScreen).
- **Perf:** batched ingest into ClickHouse MergeTree with rollups (proven by SNMP path); process inventory uses ReplacingMergeTree + short TTL to cap cardinality; cap per-host process rows.
- **Phased rollout:** internal dogfood ring → 1 design-partner ring at 10% → GA. Auto-upgrade defaults off per ring; first prod rollout is manual-promote only until rollback is exercised in staging.

Key verified files: `server/app/api/v1/sensor_api.py`, `server/app/api/v1/sensors.py`, `server/app/models/sensor.py`, `scripts/migrate-008-sensors.sql`, `scripts/migrate-004-snmp-clickhouse.sql`, `poller/internal/checker/checker.go`, `poller/internal/checker/types.go`, `poller/cmd/poller/main.go`, `dashboard/src/App.tsx`. Servers/Agent-Fleet backend confirmed **absent** in this local repo.
