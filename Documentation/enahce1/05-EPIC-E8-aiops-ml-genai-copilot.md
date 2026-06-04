# E8 — AIOps: ML Baselining/Anomaly + Correlation + Forecasting + GenAI Copilot

## 1. Goal & competitive rationale
ZenPlus today fires alerts only on hard status transitions (`alert_engine.py` → `evaluate_status_change`) and has zero ML — no dynamic baselines, no correlation, no forecasting, no AI assistant. Every 2025–26 competitor shipped agentic/GenAI (Auvik Aurora, Kentik AI Advisor, LogicMonitor Edwin, ManageEngine Zia, Datadog Watchdog/Bits, Dynatrace Davis). Without dynamic baselining we drown users in static-threshold noise and miss "slow brownout" degradations; without a copilot we lose deals on the demo. This epic delivers self-learning anomaly detection, noise-reducing correlation, capacity runout forecasts, and an Anthropic-grounded copilot that answers "why is site X slow?" over our own data.

## 2. Scope
### In scope
- Statistical dynamic baselining (rolling median/MAD, hour-of-week seasonal) + anomaly scoring on RTT, packet loss, jitter, SNMP CPU/mem, interface bps.
- Alert correlation/dedup/flapping suppression layered on the existing alert path.
- Capacity forecasting (Holt-Winters / Prophet-style) with runout-date estimates for interface utilization, disk, memory.
- GenAI copilot: FastAPI chat endpoint, RAG over ClickHouse metrics + Postgres config + flows/traps, and an MCP server exposing read-only tools, all using Anthropic models.

### Out of scope
- Autonomous remediation / write actions (copilot is read-only).
- Per-flow ML on NetFlow (NetFlow not in local repo; correlation will *consume* flows if present).
- Multi-tenant model isolation tuning, GPU/deep-learning models, training-data labeling UI.

## 3. Current state in ZenPlus
**Exists (verified):** ClickHouse metrics in `scripts/init-clickhouse.sql` — `ping_metrics`, `ping_metrics_5m/1h` rollups via materialized views, `device_status_log`; SNMP in `scripts/migrate-004-snmp-clickhouse.sql` (`snmp_metrics`, `snmp_metrics_5m/1h`, `snmp_if_metrics`). Writes flow through `poller/internal/store/clickhouse.go` (`insertBatch`, `insertSNMPBatch`, `insertSNMPIfBatch`, batched writers). Query layer: `server/app/services/metric_service.py::get_device_metrics` (auto granularity raw/5m/1h, `get_clickhouse_client` singleton in `app/core/database.py`). Alerting: `server/app/api/v1/alert_engine.py::evaluate_status_change` / `evaluate_service_status_change` insert into Postgres `alerts`, dispatch SMS/email; rules in `alert_rules` (`scripts/migrate-001-alerts.sql`, with `min_duration`, `max_repeat`, `cooldown`, `schedule_*`). Alert CRUD in `app/services/alert_service.py`; RBAC via `app/core/security.py::get_current_user` and `users.role` (default `viewer`).

**Missing:** any baseline/anomaly/forecast tables, dynamic-threshold rule type (current rules are status-only, no `metric/operator/threshold` evaluation against series — those template vars are hardcoded), no correlation/dedup/flapping engine (each transition independently inserts an alert; no `incidents` grouping), no scheduled analytics job runner, no Anthropic SDK / MCP, no copilot UI.

## 4. Target design & architecture
A new **analytics worker** (FastAPI background task + APScheduler, or a separate `analytics` container reusing `app/`) runs periodic ClickHouse aggregation jobs. Models are *statistical*, computed in-DB where possible (ClickHouse `quantile`, `medianExact`, `stddevPop`) and persisted back to ClickHouse for fast reads. Anomaly events feed a **correlation engine** that groups raw alerts into **incidents** (Postgres). The copilot is a FastAPI router calling Anthropic Messages API with tool-use; tools are served by an internal **MCP server** that wraps read-only metric/config queries.

```
poller ─► ClickHouse(raw) ─► analytics worker ──► metric_baselines (CH)
                                  │  (jobs)        anomaly_events  (CH)
                                  │                capacity_forecasts (CH)
                                  ▼
            anomaly_events ─► correlation engine ─► incidents/incident_members (PG)
                                  │                        │
   alert_engine (existing) ──────┘                        ▼ notify (dedup'd)
                                                  copilot router ─► Anthropic API
                                                        ▲ tool-use
                                                  MCP server (read tools: metrics, devices, anomalies, incidents)
```

## 5. Data model & migrations
**ClickHouse** (`scripts/migrate-009-aiops-clickhouse.sql`):
- `metric_baselines(device_id UUID, metric_key LowCardinality(String), hour_of_week UInt8, median Float64, mad Float64, p05 Float64, p95 Float64, computed_at DateTime64(3), sample_count UInt32)` — `ReplacingMergeTree(computed_at) ORDER BY (device_id, metric_key, hour_of_week)`, 90d TTL.
- `anomaly_events(id UUID, device_id UUID, metric_key LowCardinality(String), timestamp DateTime64(3), observed Float64, expected Float64, deviation_sigma Float64, severity LowCardinality(String), direction Enum8('high'=1,'low'=2))` — `MergeTree PARTITION BY toYYYYMM(timestamp) ORDER BY (device_id, timestamp)`, 90d TTL.
- `capacity_forecasts(device_id UUID, metric_key LowCardinality(String), horizon_days UInt16, slope Float64, forecast_value Float64, threshold Float64, runout_date Nullable(Date), computed_at DateTime64(3))` — `ReplacingMergeTree(computed_at)`.

**Postgres** (`scripts/migrate-010-aiops.sql`):
- `incidents(id UUID pk, title, severity, status, root_cause_device_id, started_at, resolved_at, member_count, correlation_key, ai_summary text, created_at)`.
- `incident_members(incident_id FK, alert_id FK, device_id, added_at)` — idx `(incident_id)`.
- `alert_rules` add `metric VARCHAR`, `operator VARCHAR`, `threshold FLOAT`, `mode VARCHAR DEFAULT 'static' CHECK (mode IN ('static','dynamic'))`, `sensitivity FLOAT DEFAULT 3.0` (sigma).
- `copilot_conversations(id, user_id, created_at)`, `copilot_messages(id, conversation_id, role, content, tool_calls jsonb, created_at)`.
- `aiops_settings` row in `system_settings` (`anthropic_model`, `enabled`, daily token budget).
Migration notes: ReplacingMergeTree needs `FINAL`/dedup on read; baselines back-filled by a one-shot job over 30d history; back-compat — existing status-only rules default `mode='static'`.

## 6. API changes
- `POST /api/v1/aiops/baselines/recompute` (admin) — trigger baseline job; body `{device_ids?, metric_keys?}`.
- `GET /api/v1/aiops/anomalies?device_id=&from=&to=&severity=` — list anomaly events; returns `{anomalies:[{metric_key,timestamp,observed,expected,deviation_sigma,severity}]}`.
- `GET /api/v1/aiops/forecasts?device_id=&metric_key=` — `{runout_date, slope, horizon_days, forecast_value, threshold}`.
- `GET /api/v1/incidents?status=&severity=` and `GET /api/v1/incidents/{id}` — incident + members + `ai_summary`.
- `POST /api/v1/incidents/{id}/acknowledge|resolve`.
- `POST /api/v1/copilot/chat` — `{conversation_id?, message}` → streamed (SSE via existing `sse-starlette`) assistant text + tool-call trace; grounded answer.
- `GET /api/v1/copilot/conversations`, `GET /api/v1/copilot/conversations/{id}`.
- Internal: `POST /api/v1/aiops/evaluate-anomaly` (poller-callable, mirrors `alert-engine/evaluate`) so anomaly→alert path reuses notification dispatch.

## 7. Poller / collector changes
Minimal — analytics is server-side. Add `poller/internal/checker/threshold/` only if we move dynamic evaluation to the edge later; for v1 the poller is unchanged except an optional `WriteAnomalyHint` is **not** needed. Instead add **`server/app/analytics/`** (Python): `baseliner.py` (ClickHouse quantile/MAD queries), `anomaly.py` (robust z-score = `(observed-median)/(1.4826*mad)`, EWMA fallback), `forecaster.py` (Holt-Winters via `statsmodels`, optional `prophet`), `correlation.py` (time-window + topology dedup/flapping), `scheduler.py` (APScheduler: baselines hourly, anomaly scan every 1–2 min on 5m rollups, forecasts nightly). New Python deps: `anthropic`, `mcp`, `statsmodels`, `apscheduler`, optional `prophet`. The Go `clickhouse.go` batched-writer pattern is reused conceptually; no Go protocol changes.

## 8. Dashboard changes
- New route `aiops` → `AIOpsPage.tsx` (anomaly timeline, baseline-band overlays on existing metric charts).
- New route `incidents` → `IncidentsPage.tsx` + `IncidentDetailPage.tsx` (correlated alerts, AI summary, ack/resolve) — pattern follows `AlertsPage.tsx`.
- `CopilotDrawer.tsx` — global slide-over chat (SSE stream, tool-call chips, source citations); launcher button in app shell.
- Extend `DeviceDetailPage.tsx` charts with baseline bands + forecast runout badge; extend `AlertRulesPage.tsx` with `mode=dynamic` + `sensitivity` controls. Routes registered in `dashboard/src/App.tsx` under the authenticated layout.

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|------|------|---------|-----------|
| 1 | CH migration: baselines/anomaly/forecast tables + rollup MVs | db | 2 | — |
| 2 | PG migration: incidents, copilot, alert_rule dynamic cols | db | 1 | — |
| 3 | `baseliner.py` rolling median/MAD + hour-of-week, backfill job | api | 4 | 1 |
| 4 | `anomaly.py` robust z-score scan + write anomaly_events | api | 4 | 3 |
| 5 | `forecaster.py` Holt-Winters runout for util/disk/mem | api | 4 | 1 |
| 6 | `correlation.py` dedup/flapping + incident grouping | api | 5 | 2,4 |
| 7 | Wire anomaly→`alert-engine` notify reuse (`evaluate-anomaly`) | api | 2 | 4,6 |
| 8 | `scheduler.py` APScheduler jobs + container/entrypoint | infra | 2 | 3,4,5 |
| 9 | AIOps + incidents REST endpoints | api | 3 | 4,5,6 |
| 10 | MCP server: read tools (metrics, devices, anomalies, incidents) | api | 4 | 9 |
| 11 | Copilot router: Anthropic tool-use + SSE + prompt caching | api | 5 | 10 |
| 12 | RBAC: gate recompute/admin; viewer read-only copilot | api | 1 | 9,11 |
| 13 | `AIOpsPage` + baseline/forecast chart overlays | ui | 4 | 9 |
| 14 | `IncidentsPage` + detail (ack/resolve, AI summary) | ui | 4 | 9 |
| 15 | `CopilotDrawer` streaming chat + citations | ui | 5 | 11 |
| 16 | Dynamic-rule UI in `AlertRulesPage` (mode/sensitivity) | ui | 2 | 2 |
| 17 | Feature flags, token-budget guard, telemetry | infra | 2 | 11 |
| 18 | Load/perf test anomaly scan @ 50k devices; tune queries | infra | 3 | 4,8 |

## 10. Acceptance criteria
- [ ] Baselines computed per device/metric/hour-of-week and visible as bands on metric charts.
- [ ] A simulated RTT spike (>sensitivity·σ above baseline) produces an `anomaly_events` row within one scan interval and (if rule `mode=dynamic`) a notification.
- [ ] A linearly-growing interface utilization yields a `runout_date` forecast and a badge on the device page.
- [ ] Five flapping alerts from one device collapse into a single incident; duplicate alerts within window are suppressed.
- [ ] Copilot answers "why is <device> slow?" citing real anomalies/metrics via MCP tools, streamed token-by-token.
- [ ] Viewer role can read AIOps/incidents/copilot but cannot trigger recompute; admin can.
- [ ] AIOps fully disableable via feature flag with zero impact on existing alert path.

## 11. Test cases
| ID | Type | Precondition | Steps | Expected result |
|----|------|-------------|-------|-----------------|
| T1 | unit | 14d RTT series | Run `baseliner` | median/MAD/p05/p95 per hour-of-week match numpy reference ±1% |
| T2 | unit | Series with one outlier | Run `anomaly` | Robust z-score flags outlier; non-outliers below threshold |
| T3 | unit | Flat-then-zero-variance metric | Run anomaly | MAD=0 handled (no div-by-zero), no false anomaly |
| T4 | unit | Linear-growth util to 100% | `forecaster` | runout_date within ±1 day of analytic answer |
| T5 | integration | Insert spike into `ping_metrics` | Run scan | `anomaly_events` row created, correct sigma/direction |
| T6 | integration | Dynamic rule on RTT, anomaly fires | Trigger scan | `alerts` row + notification via existing dispatch |
| T7 | integration | 5 transitions/2min one device | Run correlation | One incident, 5 members, others deduped |
| T8 | integration | Two devices same uplink down | Run correlation | Single incident, root_cause set to shared upstream |
| T9 | e2e | Logged-in admin | Open AIOps page | Charts render baseline bands + anomalies |
| T10 | e2e | Open Copilot, ask "why slow?" | Submit | Streamed answer cites MCP tool results + device names |
| T11 | manual | Copilot asks for device not owned | Query | MCP returns scoped data only; no leakage |
| T12 | security | Viewer token | `POST /aiops/baselines/recompute` | 403 Forbidden |
| T13 | security | Prompt-injection in device name | Copilot query | Injected instruction ignored; tool boundary holds |
| T14 | perf | 50k devices, 5m rollups | Run anomaly scan | Completes < 60s, CH CPU within budget |
| T15 | perf | Copilot 20 concurrent chats | Load | p95 first-token < 3s; token budget enforced |
| T16 | failure | Anthropic API 529/timeout | Copilot query | Graceful error, conversation persisted, no crash |
| T17 | failure | ClickHouse down during scan | Run scheduler | Job logs error, retries, existing alerts unaffected |
| T18 | regression | Feature flag off | Cause device down | `evaluate_status_change` fires exactly as before |
| T19 | regression | Static rule unchanged | Status change | Notification identical to pre-E8 behavior |
| T20 | integration | Forecast nightly job | Run | `capacity_forecasts` upserted (ReplacingMergeTree FINAL returns latest) |

## 12. Risks & rollout
**Flags:** `aiops.enabled`, `aiops.anomaly`, `aiops.forecast`, `copilot.enabled` in `system_settings`; all default off so the existing `alert_engine` path is untouched (T18/T19 guard regressions). **Migration/back-compat:** additive only; alert rules default `mode='static'`; ReplacingMergeTree reads must use `FINAL` or argMax to avoid stale rows. **Perf:** anomaly scan runs on 5m rollups not raw, partition-pruned by time; cap per-scan device fan-out and queue overflow like the poller's non-blocking buffers. **Security/cost:** copilot is strictly read-only via MCP, queries are tenant/RBAC-scoped, prompt-injection mitigated by treating tool output as data; enforce per-org daily token budget and use Anthropic prompt caching on the system/tool schema to control spend; never send credentials/secrets into prompts. **Rollout:** Phase 1 — baselines + anomaly read-only (charts only, no notify); Phase 2 — dynamic rules + correlation/incidents; Phase 3 — forecasting; Phase 4 — copilot beta to admins, then GA. Each phase is independently flaggable and shippable.
