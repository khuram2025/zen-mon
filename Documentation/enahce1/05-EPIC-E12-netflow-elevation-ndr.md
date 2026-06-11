# E12 — NetFlow Elevation: ML App-ID + Forecasting + NDR-lite Security Module

## 1. Goal & competitive rationale
ZenPlus already exposes top talkers/conversations/apps, DSCP/QoS, scan-share, heatmaps, Forensics and rule-based Anomaly Detection — parity with PRTG/SolarWinds NTA. To leapfrog Auvik TrafficInsights and Kentik, we add three differentiators on top of the existing flow pipeline: ML application identification on **encrypted** flows (so HTTPS/QUIC isn't just "443"), per-interface **traffic forecasting/runout** for capacity planning, and a marketed **NDR-lite** module that turns scattered heuristics into baseline-driven DDoS/volumetric detection wired into the E1 alert engine. This converts NetFlow from a reporting tab into a security+capacity product line that justifies a premium tier.

## 2. Scope
### In scope
- ML App-ID classifier (SNI/JA3/JA3S + flow-stats features) producing `app_name`/`app_category`/`confidence`, overriding port heuristics.
- Per-interface forecasting (Holt-Winters/linear) with 95%-saturation **runout date**.
- NDR-lite module: rolling baselines per (device, direction, app, dst-port), DDoS/volumetric/scan detectors, correlation, and emission into E1's `/alert-engine`.
- New ClickHouse tables/rollups, Postgres config (detector configs, app-id labels, forecast snapshots), FastAPI endpoints, dashboard pages.
### Out of scope
- Full DPI/packet capture (we stay flow + TLS-handshake metadata only).
- Inline blocking / IPS enforcement (detect+alert only).
- Training a foundation model; we ship a pre-trained gradient-boosted classifier + online baselines, not deep learning.
- Rewriting existing flow collector ingest (assumed live; we extend it).

## 3. Current state in ZenPlus
**Verified locally.** Alert engine: `server/app/api/v1/alert_engine.py` exposes internal `POST /alert-engine/evaluate` (`StatusChangeEvent`) and `/evaluate-service`, fans out to `notification_channels`/`notification_gateways`, and inserts into `alerts`. Alert model: `server/app/models/alert.py` (`AlertRule` with `metric/operator/threshold/duration/severity/notify_channels`, scoping by `device_id/group_id`; `Alert` with `extra_data` JSONB `metadata`). ClickHouse access: `server/app/core/database.py::get_clickhouse_client` (singleton). Ingest pattern: `poller/internal/store/clickhouse.go` batched `PrepareBatch` writers (`ping_metrics`, `snmp_if_metrics`) with `RunBatchWriter` goroutines and bounded channels. Rollup convention `*_5m`/`*_1h` with raw-fallback: `server/app/services/metric_service.py`. Interface counters/bps already modeled: `snmp.InterfaceSample` (`InBps/OutBps`, `poller/internal/checker/snmp/types.go`). Realtime: Redis pub/sub channels in `redis.go` (`zenplus:alerts`, `zenplus:status_change`). RBAC: `get_current_user` + `User.role` (`viewer` default; `server/app/models/user.py`, `server/app/core/security.py`). Routing: `dashboard/src/App.tsx`.

**Missing locally.** No NetFlow collector, no `flow_records`/`flow_5m` tables, no anomaly/baseline tables, no App-ID. The brief states the flow backend lives in the newer build; this plan extends the *observed live* feature set and the *verified local* architecture (batched CH writers, rollup tables, internal alert-engine fan-out).

## 4. Target design & architecture
```
flow exporters ─┐
(NetFlow v9/IPFIX/sFlow) │
        ▼
 Go flow collector (poller/internal/flow) ── decode ── enrich(GeoIP/ASN, app-id) ── CH batch writer
        │                                              │ JA3/SNI/JA4 features
        │                                              ▼
        │                                    appid sidecar (gRPC, gradient-boost model)
        ▼
  rolling baselines (EWMA per key) ──► NDR detectors ──► POST /netflow/ndr/event ──► E1 /alert-engine/evaluate-flow
        ▼
 ClickHouse: flow_records → flow_5m / flow_1h ; flow_appid ; ndr_events
        ▲
 FastAPI /netflow/* (talkers, app-mix, forecast, ndr) ──► React NetFlow + Security pages
 forecast worker (nightly Holt-Winters over flow_1h/snmp_if) ──► Postgres iface_forecast
```
The App-ID model runs as a small Python gRPC sidecar (scikit/LightGBM, CPU) so the Go collector stays lean; the collector batches feature vectors and caches `(sni,ja3)->app` to keep p99 < 5ms. NDR baselines are EWMA state held in the collector and snapshotted to Redis for restart recovery; detectors compare live windows to baseline z-scores and emit events.

## 5. Data model & migrations
**ClickHouse (metrics).**
- `flow_records` (raw, TTL 7–14d): `timestamp, exporter_ip, device_id Nullable, in_if UInt32, out_if UInt32, src_ip IPv6, dst_ip IPv6, src_port UInt16, dst_port UInt16, proto UInt8, tcp_flags UInt8, dscp UInt8, bytes UInt64, packets UInt64, app_name LowCardinality(String), app_category LowCardinality(String), app_confidence Float32, src_asn UInt32, dst_asn UInt32, direction Enum8`. `ORDER BY (device_id, timestamp, dst_port)`, partition `toYYYYMMDD`.
- Rollups `flow_5m`/`flow_1h` (Summing/AggregatingMergeTree via materialized views) aggregating bytes/packets per (device, in_if, app, direction) — mirrors the `*_5m`/`*_1h` convention.
- `flow_appid` (TLS handshake/app labels): `timestamp, src_ip, dst_ip, sni, ja3, ja3s, ja4, app_name, app_category, confidence, model_version`.
- `ndr_events`: `id UUID, timestamp, device_id, detector LowCardinality, severity, score Float32, src_ip, dst_ip, dst_port, baseline Float64, observed Float64, dimensions JSON String, alert_id Nullable`. TTL 90d.

**Postgres (config).** Alembic migration:
- `ndr_detectors` (id, name, type[`ddos`/`volumetric`/`scan`/`sensitive_egress`], enabled, sensitivity Float, window_sec, baseline_days, scope_device_id/group_id, notify_channels JSONB, severity).
- `appid_labels` (id, match_type[`sni`/`ja3`/`port`], pattern, app_name, app_category, source[`builtin`/`custom`]) — user overrides.
- `iface_forecast` (device_id, if_index, generated_at, model, daily_growth_bps, p95_utilization, runout_date Nullable, confidence) — latest snapshot per interface.
- Extend `alert_rules.metric` enum to accept `flow_volume`/`ndr_event`/`iface_runout` (string column already, no DDL — engine-level change only).

## 6. API changes
- `GET /api/v1/netflow/top-talkers?device_id&from&to&dim=app|host|conv&limit` — aggregated from `flow_5m`; fields: `key, bytes, packets, flows, app_category, pct`.
- `GET /api/v1/netflow/app-mix?device_id&interval` — app/category share over time for stacked area.
- `GET /api/v1/netflow/forecast/{device_id}/{if_index}` — returns `history[], forecast[], runout_date, p95_utilization, confidence`.
- `GET /api/v1/netflow/ndr/events?severity&detector&device_id&from&to` — paginated NDR feed.
- `POST /api/v1/netflow/ndr/event` *(internal, no-auth like `/alert-engine`)* — collector posts `{detector, device_id, severity, score, baseline, observed, src_ip, dst_ip, dimensions}`; server persists to `ndr_events`, dedupes within `cooldown`, then calls the new `/alert-engine/evaluate-flow`.
- `POST /api/v1/alert-engine/evaluate-flow` *(internal)* — new sibling of `evaluate-service` in `alert_engine.py`; matches `alert_rules` where `metric IN ('ndr_event','flow_volume','iface_runout')`, scopes by device/group, inserts into `alerts`, fans out to channels (reuse `_send_sms`/`_send_email`).
- `GET/POST/PATCH /api/v1/netflow/detectors` and `/appid-labels` — CRUD (admin-only via role check).

## 7. Poller / collector changes
New Go package `poller/internal/flow`:
- `collector.go` — UDP listeners for NetFlow v9/IPFIX (`github.com/netsampler/goflow2`) + sFlow; template cache.
- `enrich.go` — GeoIP/ASN (MaxMind `oschwald/geoip2-golang`), device_id resolution by exporter IP/in_if.
- `appid.go` — gRPC client to the App-ID sidecar; LRU cache `(sni,ja3)->label`; fallback to `appid_labels` port map.
- `baseline.go` — per-key EWMA (mean+variance), window aggregation, Redis snapshot/restore.
- `ndr.go` — detectors: DDoS (fan-in flow-rate z-score), volumetric (bytes baseline breach), SYN/RST scan share, sensitive-port egress; emit to `/netflow/ndr/event`.
- `store_clickhouse.go` — batched `PrepareBatch` writers for `flow_records`/`flow_appid`/`ndr_events`, modeled exactly on `RunBatchWriter`/`insertSNMPIfBatch` (bounded channels, ticker flush). Wire into `poller/cmd/poller/main.go` startup like the SNMP writers.
New Python sidecar `services/appid/` (FastAPI/gRPC + LightGBM model artifact, versioned). Forecast worker: `server/app/services/forecast_service.py` (statsmodels Holt-Winters over `flow_1h`/`snmp_if_metrics`), scheduled nightly.

## 8. Dashboard changes
- Routes in `dashboard/src/App.tsx`: `netflow` (Traffic), `netflow/forecast`, `security` (NDR). Add nav entries in `components/layout`.
- `pages/NetFlowPage.tsx` — app-mix stacked area + top-talkers (reuse `components/charts`, `reports/Top10Table.tsx`), App-ID confidence badge column.
- `pages/CapacityForecastPage.tsx` — per-interface forecast chart with shaded runout band + "runs out in N days" KPI tiles (`reports/KpiTile.tsx`).
- `pages/SecurityNdrPage.tsx` — live NDR event feed (Redis WS via existing `api/websocket/realtime`), detector filters, severity chips, drill-in to talkers; "Acknowledge"/"Create rule" actions.
- `pages/AlertRulesPage.tsx` — add `ndr_event`/`flow_volume`/`iface_runout` metric options.
- `pages/GeneralSettingsPage.tsx` / new `NdrDetectorsPage.tsx` — detector sensitivity + App-ID label management (admin only).

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|------|------|---------|-----------|
| 1 | CH migrations: `flow_records`, `flow_5m/1h` MVs, `flow_appid`, `ndr_events` | db | 2 | — |
| 2 | PG Alembic: `ndr_detectors`, `appid_labels`, `iface_forecast`; metric enum support | db | 1.5 | — |
| 3 | Go `flow` collector: NetFlow v9/IPFIX/sFlow decode + template cache | poller | 5 | — |
| 4 | Enrichment (GeoIP/ASN, device_id resolution) | poller | 2 | 3 |
| 5 | App-ID sidecar: feature schema, LightGBM training pipeline, gRPC serve | infra | 5 | — |
| 6 | Collector App-ID integration + LRU cache + port fallback | poller | 3 | 3,5 |
| 7 | Batched CH writers for flow/appid/ndr (mirror `RunBatchWriter`) | poller | 2 | 1,3 |
| 8 | NDR baselines (EWMA + Redis snapshot) + detectors | poller | 5 | 4,7 |
| 9 | `/netflow/ndr/event` ingest + dedupe + `/alert-engine/evaluate-flow` | api | 3 | 2,8 |
| 10 | NetFlow query API (talkers, app-mix) | api | 3 | 1 |
| 11 | Forecast worker (Holt-Winters) + `/netflow/forecast` | api | 3 | 2 |
| 12 | Detector & app-label CRUD APIs (admin RBAC) | api | 2 | 2 |
| 13 | NetFlow + App-ID dashboard page | ui | 4 | 10 |
| 14 | Capacity Forecast page | ui | 3 | 11 |
| 15 | Security NDR page (WS live feed) | ui | 4 | 9 |
| 16 | Alert-rule metric options + detector settings UI | ui | 2 | 9,12 |
| 17 | Feature flag `netflow_ndr`, load/perf test harness, docs | infra | 2 | 7,8 |

## 10. Acceptance criteria
- [ ] Encrypted HTTPS/QUIC flows show a real app (e.g. "Microsoft Teams") not "tcp/443", with a confidence badge; unknown falls back to port heuristic.
- [ ] User overrides an app via `appid_labels` and it takes effect within one poll cycle.
- [ ] Each monitored interface shows a forecast curve + runout date (or "no saturation in 12mo").
- [ ] A simulated SYN flood/volumetric spike raises an NDR event within the detector window and a correlated alert via E1 channels (email/SMS).
- [ ] NDR detector sensitivity is configurable; lowering it suppresses borderline events.
- [ ] Security page shows live events over WebSocket; acknowledging updates status.
- [ ] Non-admin (`viewer`) can view NetFlow/Security but cannot edit detectors/labels.
- [ ] Existing talkers/QoS/heatmap features remain unchanged (regression).

## 11. Test cases
| ID | Type | Precondition | Steps | Expected result |
|----|------|--------------|-------|-----------------|
| T1 | unit | decoder | Feed captured IPFIX template+data | Fields parsed; bytes/packets/flags correct |
| T2 | unit | appid model | Score known SNI=teams.microsoft.com | `app_name=Teams`, confidence>0.8 |
| T3 | unit | port fallback | Flow with no TLS handshake, dst 53 | Classified `DNS` via `appid_labels` |
| T4 | unit | EWMA baseline | Stream steady traffic then 10x spike | z-score breach flagged once, not per-flow |
| T5 | integration | collector+CH | Send 50k flows/s for 60s | All land in `flow_records`; `flow_5m` MV populated |
| T6 | integration | forecast | Seed 30d rising `flow_1h` | Runout date returned, monotonic, confidence set |
| T7 | integration | NDR→E1 | Detector + alert_rule `metric=ndr_event` | `ndr_events` row + `alerts` row + channel fan-out |
| T8 | integration | dedupe | Re-fire same detector within cooldown | Single alert, event counter increments |
| T9 | e2e | UI | Open NetFlow page | App-mix + talkers render with confidence badges |
| T10 | e2e | UI | Open Capacity Forecast | Curve + runout KPI shown for an interface |
| T11 | e2e | UI | Trigger NDR, watch Security page | Event appears live via WS; acknowledge persists |
| T12 | security/RBAC | viewer token | PATCH `/netflow/detectors` | 403; GET succeeds |
| T13 | security | internal endpoint | POST `/netflow/ndr/event` from off-host | Bound to internal/poller token; spoofed events rejected |
| T14 | failure | sidecar down | Kill App-ID gRPC | Collector falls back to port map, no flow loss, logs warning |
| T15 | failure | CH unavailable | Stop ClickHouse 30s | Bounded channels drop-with-counter, no poller crash, recover on restore |
| T16 | failure | malformed flow | Send truncated/garbage packet | Dropped + metric incremented, no panic |
| T17 | perf | load | 100k flows/s sustained | p99 enrich+appid < 5ms; CH write lag < flush interval |
| T18 | perf | query | 2 weeks `flow_5m` | Top-talkers API < 800ms |
| T19 | edge | IPv6/asym | IPv6 + asymmetric routing | Conversations stitched; no dup direction double-count |
| T20 | regression | existing | Run pre-existing talkers/QoS/heatmap | Outputs byte-identical to pre-change baseline |

## 12. Risks & rollout
- **Feature flags:** `netflow_ndr` and `appid_ml` gate collector emission, NDR alerting, and UI nav; ship dark → internal tenants → GA.
- **App-ID accuracy:** encrypted-traffic ML risks false labels; cap by emitting `app_confidence` and only override port-heuristic above a threshold; allow `appid_labels` overrides; version `model_version` for rollback.
- **Alert storms:** NDR baselines need warm-up (`baseline_days`); suppress events until baseline mature, enforce per-detector cooldown and dedupe in `/ndr/event`, default conservative sensitivity.
- **Perf/cost:** raw `flow_records` is high-cardinality — enforce TTL (7–14d), serve UI from rollups, partition by day; collector uses bounded channels + drop-counter exactly like `clickhouse.go` to protect the poller during CH outages.
- **Back-compat:** all changes additive; `alert_rules.metric` is a free string so no enum migration breaks E1; existing flow tabs untouched.
- **Security:** `/netflow/ndr/event` and `/alert-engine/evaluate-flow` are internal — restrict to poller network/shared token; detector/label CRUD admin-only via `User.role`.
- **Rollout phases:** (1) collector + raw ingest behind flag; (2) App-ID + UI read-only; (3) forecasting; (4) NDR baselines in shadow (log only); (5) NDR→E1 alerting GA after false-positive tuning.
