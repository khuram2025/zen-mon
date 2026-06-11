# E11 — Synthetic Monitoring + WAN/Internet Path (Traceroute) + BGP Basics

## 1. Goal & competitive rationale
ZenPlus today proves *whether* an endpoint is up but not *why a user's journey breaks* or *where on the path it degrades*. ThousandEyes, Kentik, and ManageEngine win deals on three things we lack: multi-step web journeys with assertions, hop-by-hop path visualization (ISP vs. internal fault isolation), and BGP awareness. Shipping these turns ZenPlus from a uptime checker into a digital-experience + WAN-assurance product, directly attacking the "is it us or the ISP?" question that drives MTTR. We can build all three by extending the existing `checker` package and `Sensor` (distributed collector) plumbing rather than greenfield.

## 2. Scope
### In scope
- Multi-step synthetic web journeys: ordered HTTP steps, variable extraction/chaining, per-step assertions (status, body regex, JSON path, latency), and aggregate SSL-expiry surfacing.
- Agent/collector-driven traceroute: per-hop RTT/loss, ASN/ISP enrichment, internal-vs-external classification, path-change detection, visualization.
- BGP basics: peer state (Established/Idle), prefix counts, route/origin-AS change alerts via BMP/BGP-LS or SNMP `BGP4-MIB`.
- Multi-vantage execution via existing `Sensor` collectors; reuse alert engine, SLA, maintenance windows.
### Out of scope
- Browser/RUM (real DOM, JS execution) — journeys are HTTP-flow only (phase 2 candidate).
- Full route-analytics / DDoS detection, BGP route injection/RPKI validation, flow correlation (E-NetFlow).
- Voice/video synthetic (UDP/RTP MOS).

## 3. Current state in ZenPlus
- **Checkers** (`poller/internal/checker/`): `HTTPChecker` (`http.go`), `TCPChecker`, `TLSChecker` (extracts `TLSDaysRemaining`/`TLSExpiry`), `ICMPChecker` (`pro-bing`, privileged), `DNSChecker`. Dispatched in `checker.go:CheckOne` by `sc.CheckType`. `ServiceCheck`/`ServiceCheckResult` in `types.go` — note `Level int // 1=availability,2=health,3=transaction` (level 3 is **defined but unused**; the natural home for journeys).
- **Engine** (`poller/internal/pinger/engine.go`): `syncServiceChecks`→`LoadServiceChecks`, `runServiceCheckCycle` (worker-pool `CheckBatch`), `processServiceStatusChange`, `evaluateServiceAlerts`. Results batched to ClickHouse via `store/clickhouse.go:WriteServiceResult`/`insertServiceBatch` (table `service_metrics`).
- **API/DB**: `models/service_check.py` (`ServiceCheck`, `...Group`, `...Template`, `...Maintenance`), `api/v1/service_checks.py` (CRUD, `/test`, `/metrics`, `/sla`, `/uptime-stats`). ClickHouse `zenplus.service_metrics`(+`_5m` MV, `service_status_log`) in `scripts/init-clickhouse.sql`.
- **Collectors**: `models/sensor.py` — `Sensor` (enrollment token, api_key, heartbeat, `queue_depth`) + `SensorAssignment(target_type,target_id)`. This is our multi-vantage fabric.
- **Missing**: no traceroute checker, no BGP collector, no multi-step journey model, no path/BGP ClickHouse tables, no journey/path/BGP UI. RBAC is a single `User.role` string (`security.py`); no per-action checks yet.

## 4. Target design & architecture
Reuse the collector → checker → ClickHouse → API → dashboard spine. Add three new check families dispatched from `CheckOne`, all executable on a `Sensor` (vantage) so we get multi-point views.

```
 Sensor(vantage) ─ poller ─ checker.CheckOne ─┬─ journey   → step results + assertions
                                              ├─ traceroute→ per-hop rows (+ASN enrich)
                                              └─ bgp        → peer/prefix snapshots
        │ batch                                   │
        ▼                                         ▼
  ClickHouse: journey_step_metrics / path_hops / bgp_peer_metrics + 5m MVs
        │                                         │
   FastAPI /journeys /paths /bgp ──── React: JourneyBuilder, PathMap, BgpPeers
```

Journeys are modeled as a `ServiceCheck` with `level=3` + child step rows. Traceroute/BGP are new `check_type`s (`traceroute`,`bgp`) reusing the existing scheduling/assignment/alert machinery. ASN enrichment uses an embedded MaxMind GeoLite2-ASN db on the server (offline-friendly).

## 5. Data model & migrations
**Postgres** (`migrate-009-synthetic-path-bgp.sql`):
- `synthetic_journey_steps(id, check_id FK service_checks, step_order, method, url, headers JSONB, body, extract JSONB /*{var: jsonpath|header|regex}*/, assertions JSONB, think_time_ms, enabled)`.
- Extend `service_checks`: add `vantage_sensor_ids UUID[]` (multi-point), `traceroute_config JSONB`, `bgp_config JSONB`. New `check_type` values `journey|traceroute|bgp`.
- `bgp_peers(id, check_id, peer_ip INET, peer_asn, description, expected_state)`.
**ClickHouse** (`migrate-009-clickhouse.sql`, mirror `service_metrics` pattern: `MergeTree`, `PARTITION BY toYYYYMM`, 30/90/365-day TTL):
- `journey_step_metrics(check_id UUID, step_order UInt16, sensor_id String, timestamp DT64, is_up UInt8, response_ms F64, status_code Nullable(UInt16), assertion_failed Nullable(String), bytes UInt32)` ORDER BY `(check_id, timestamp, step_order)` + `_5m` MV (per-step uptime/p95).
- `path_hops(check_id UUID, sensor_id String, timestamp DT64, hop UInt8, hop_ip IPv4, rtt_ms F64, loss_pct F32, asn UInt32, as_name String, is_internal UInt8, path_hash UInt64)` ORDER BY `(check_id, timestamp, hop)`; `path_hash` drives path-change detection.
- `bgp_peer_metrics(check_id UUID, peer_ip IPv4, timestamp DT64, state LowCardinality(String), prefixes_received UInt32, prefixes_advertised UInt32, established_secs UInt64, origin_as UInt32)`.
Migration notes: additive only; run via existing `updater/steps/run_migration.py`. Backfill none. `_mv` views are `IF NOT EXISTS`.

## 6. API changes
- `POST/GET/PUT/DELETE /api/v1/service-checks/{id}/journey-steps` — manage ordered steps (body: step_order, method, url, headers, extract, assertions[]).
- `POST /api/v1/service-checks/{id}/journey-steps/test` — synchronous full-journey run, returns per-step waterfall (status, ms, assertion results, extracted vars). Extends existing `/test`.
- `GET /api/v1/service-checks/{id}/journey-results?from&to` — step waterfalls + aggregate pass rate from `journey_step_metrics`.
- `GET /api/v1/service-checks/{id}/path?from&to&sensor_id` — latest + historical hop list with ASN/ISP, `path_changed` flag.
- `POST /api/v1/service-checks/{id}/path/run` — on-demand traceroute (queued to assigned sensor).
- `GET /api/v1/bgp/peers?check_id` and `GET /api/v1/service-checks/{id}/bgp?from&to` — peer state/prefix series + change events.
- All keep `Depends(get_current_user)`; mutating routes gain a `require_role("operator")` guard (new helper in `security.py`).

## 7. Poller / collector changes
- `checker/journey.go` — `JourneyChecker` runs steps sequentially over one `*http.Client` (cookie jar), templating `{{var}}` from prior `extract` (jsonpath via `tidwall/gjson`, header, regex). Each step yields a `JourneyStepResult`; first failed assertion fails the journey. Reuse `statusMatches` from `http.go`.
- `checker/traceroute.go` — `TracerouteChecker`: UDP/ICMP increasing-TTL probes (lib `github.com/aeden/traceroute` or in-house raw-socket using existing `pro-bing`/`CAP_NET_RAW`), N probes/hop for loss. ASN/`is_internal` (RFC1918/configured CIDRs) tagging.
- `checker/bgp.go` — `BGPChecker`: phase-1 SNMP poll of `BGP4-MIB::bgpPeerTable` (reuse `checker/snmp/`); design hook for gobgp BMP listener later.
- `checker/types.go` — add `JourneyStepResult`, `PathHopResult`, `BGPPeerResult`; extend `ServiceCheckResult` union or add typed result channels.
- `checker.go:CheckOne` — add `journey|traceroute|bgp` cases.
- `store/clickhouse.go` — new `insertJourneyBatch`/`insertPathBatch`/`insertBgpBatch` + buffers, mirroring `insertServiceBatch`.
- `engine.go` — extend `runServiceCheckCycle` to fan results to the right writer; honor `vantage_sensor_ids` so a check runs only on assigned sensors.

## 8. Dashboard changes
- New routes under `services/`: `services/:id/journey` (JourneyBuilder + waterfall), `services/:id/path` (PathMap), `services/:id/bgp`.
- `JourneyBuilder.tsx` — drag-order steps, per-step assertion editor, "Test run" waterfall (green/red rungs, per-step ms).
- `PathMap.tsx` — vertical hop list / sparkline RTT per hop, ISP-boundary badge, "path changed" banner, vantage selector.
- `BgpPeersPanel.tsx` — peer state chips, prefix-count trend, change log.
- Add `check_type` options (`journey/traceroute/bgp`) to `AddServiceCheck.tsx`; vantage multi-select bound to `Sensor`s.

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|------|------|---------|-----------|
| 1 | Postgres migration: journey_steps, bgp_peers, service_checks cols | db | 1 | — |
| 2 | ClickHouse migration: journey/path/bgp tables + 5m MVs | db | 1 | — |
| 3 | `JourneyChecker` (sequential HTTP, extract, assertions) | poller | 4 | 1 |
| 4 | `TracerouteChecker` + ASN/internal enrichment | poller | 4 | — |
| 5 | `BGPChecker` (SNMP BGP4-MIB poll) | poller | 3 | — |
| 6 | CH writers + engine fan-out + vantage routing | poller | 3 | 2,3,4,5 |
| 7 | Journey-step CRUD + `/journey-steps/test` API | api | 3 | 1,3 |
| 8 | Path + BGP query/run APIs | api | 3 | 2,4,5 |
| 9 | `require_role` RBAC helper + apply to mutations | api | 1 | — |
| 10 | JourneyBuilder UI + waterfall | ui | 4 | 7 |
| 11 | PathMap UI + vantage selector | ui | 4 | 8 |
| 12 | BgpPeersPanel UI | ui | 2 | 8 |
| 13 | Wire new check types/vantages into AddServiceCheck | ui | 2 | 7,8 |
| 14 | Alert rules: assertion-fail, path-change, peer-down | api | 2 | 6 |
| 15 | E2E + perf harness, docs, feature flags | infra | 3 | 10,11,12 |

## 10. Acceptance criteria
- [ ] User builds a ≥3-step journey, extracts a token from step 1, uses it in step 2, sets a JSONPath assertion, and "Test run" shows a per-step waterfall with pass/fail + ms.
- [ ] A journey check schedules from ≥2 vantages; failures alert and feed SLA/uptime like other checks.
- [ ] A traceroute check renders ordered hops with per-hop RTT/loss, ASN/ISP names, and an internal-vs-external boundary marker.
- [ ] Path change (different `path_hash`) raises a banner and an alert.
- [ ] BGP check shows peer state + prefix counts; a peer leaving Established raises an alert.
- [ ] SSL-expiry days surface on journey/HTTPS steps and respect `tls_warn_days`.
- [ ] Viewer role can read all three; only operator+ can create/edit/run.

## 11. Test cases
| ID | Type | Precondition | Steps | Expected result |
|----|------|-------------|-------|-----------------|
| T1 | unit | — | `JourneyChecker` runs 2 steps, extract var via gjson | step2 URL templated, both pass |
| T2 | unit | — | Assertion regex fails on step body | journey `is_up=false`, `assertion_failed` set to step |
| T3 | unit | — | Status assertion uses `2xx` pattern (reuse `statusMatches`) | 204 passes, 503 fails |
| T4 | unit | — | Traceroute parses N-probe hop with 1 lost probe | `loss_pct≈33`, avg rtt computed |
| T5 | unit | RFC1918 first hops | classify hops | internal hops flagged `is_internal=1`, ISP hops 0 |
| T6 | unit | — | path_hash differs run-to-run | path-change event emitted once |
| T7 | integration | SNMP BGP sim | poll BGP4-MIB Established peer | row with state=Established, prefixes>0 |
| T8 | integration | mock journey target | full cycle → CH | `journey_step_metrics` rows per step; 5m MV aggregates |
| T9 | integration | 2 assigned sensors | run check | rows for both `sensor_id`s; unassigned sensor writes none |
| T10 | e2e | logged-in operator | build journey, save, test | waterfall renders, persisted on reload |
| T11 | e2e | path data present | open PathMap, switch vantage | hops re-render per vantage; ISP badge shown |
| T12 | e2e | peer flaps | view BgpPeersPanel | state chip → red, change log entry |
| T13 | manual | journey step times out | run | step marked failed with timeout error, later steps skipped |
| T14 | security | viewer role | POST journey-steps | 403 from `require_role` |
| T15 | security | journey extract from header | secret not echoed in step results API | extracted secret redacted in responses/logs |
| T16 | perf | 500 journeys×4 steps, 3 vantages | run 1 cycle | CH batch insert <2s, no buffer drops (`queue_depth` stable) |
| T17 | perf | 200 traceroutes, 30 hops | concurrent run | bounded by worker pool, no FD exhaustion |
| T18 | regression | existing http/tcp/tls/dns/icmp checks | run cycle | unchanged behavior, `service_metrics` writes intact |
| T19 | regression | `/test` legacy endpoint | call on http check | still returns prior shape |
| T20 | manual | maintenance window over journey | trigger fail in window | suppressed from alerts/SLA |

## 12. Risks & rollout
- **Feature flags**: gate each family (`feat_journeys`, `feat_path`, `feat_bgp`) in `core/config.py`; hide UI routes and reject new `check_type`s when off. Ship dark, enable per-tenant.
- **Back-compat**: all migrations additive; existing checks/`service_metrics` untouched; `level=3` was already reserved. Old pollers ignore unknown check types (default branch in `CheckOne` already errors gracefully) — gate vantage assignment to upgraded sensors via `Sensor.version`.
- **Perf**: traceroute is FD- and time-heavy — clamp hops (≤30), probes (≤3), and concurrency via the existing worker pool; sample path at a slower interval than availability. Use CH batch writers already proven for `service_metrics`.
- **Security**: privileged raw sockets already covered by `CAP_NET_RAW`; redact journey-extracted secrets (T15); BGP/SNMP creds reuse existing encrypted SNMP credential store; add `require_role` since current RBAC is a bare `role` string.
- **Phased rollout**: P1 journeys (highest demand, lowest infra risk) → P2 traceroute path-map → P3 BGP. Each phase: internal dogfood on 2 sensors, then 10% tenants, then GA, with rollback = flag-off (no schema rollback needed).

Key files verified: `poller/internal/checker/{http,icmp,tls,dns,tcp,checker,types}.go`, `poller/internal/pinger/engine.go`, `poller/internal/store/clickhouse.go`, `server/app/models/{service_check,sensor,user}.py`, `server/app/api/v1/service_checks.py`, `server/app/core/security.py`, `scripts/init-clickhouse.sql`, `dashboard/src/App.tsx`.
