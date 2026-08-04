# Phase 1 — Single-Node Performance & Correctness

**Goal:** one appliance that is honest and fast under 10× today's load: no blocking calls on the event loop, no silent data loss, no unbounded growth, alerting that damps flaps and never loses a notification. Everything here pays off on one box **and** is a prerequisite for the HA pair (Phase 2).
**Duration:** 3–6 weeks (~46 pd, parallelizable across 2–3 engineers).
**Entry criteria:** Phase 0 Exit Gate passed. Tasks T2–T5 build on P0-T6/T8/T10.

Suggested split: Engineer A = API/data path (T2, T3, T6, T7, T8), Engineer B = alerting (T4, T5, T18) + lifecycle (T9, T10), Engineer C (Go) = poller/collector (T11–T15) + sensor/discovery (T16, T17). T1 first — every Verify below leans on it.

---

## P1-T1 · Load & soak harness **[blocker]**
**Effort:** 3 pd
**Problem:** No way to prove any ceiling claim or regression. Every gate from here on needs reproducible synthetic load.
**Change:** create `scripts/loadtest/` with:
1. **Device load**: snmpsim (or a container fleet of snmpd) presenting N simulated SNMP devices with ~48 interfaces each, plus N pingable loopback aliases; a seeder that registers them via the API.
2. **Agent simulator**: Python script that enrolls M fake agents and posts realistic heartbeat + metric batches on the real cadence (reuse `schemas/agent.py` payload shapes).
3. **Sensor simulator**: same pattern against `/api/v1/sensor/*` (or run real `zenplus-sensor` binaries pointed at throwaway targets).
4. **Flap driver**: toggles iptables DROP for chosen targets on a schedule.
5. **API load**: a k6/vegeta profile replaying the dashboard's polling mix (device list, KPIs, netflow overview, link-utilization list at 15–30 s intervals × V virtual users).
6. A one-page `README` describing standard profiles: **S-load** (300 dev/50 agents/5 VU) and **M-load** (500 dev/100 agents/10 VU).
**Verify:** S-load runs for 1 h against a scratch appliance: seeded device count correct, `ping_metrics`/`snmp_if_metrics`/`host_cpu_metrics` row rates match expectation (±10%), harness teardown removes seeded objects.

## P1-T2 · Kill the singleton ClickHouse session **[blocker]**
**Effort:** 4 pd
**Problem:** `get_clickhouse_client()` is a process-wide single `clickhouse_connect` session (`core/database.py:35-51`); one session cannot run concurrent queries, and nearly all non-APM reads/writes call it **synchronously on the event loop** — all ClickHouse traffic per worker is serialized and blocks everything (netflow's `_query()` alone backs ~20 endpoints, `netflow.py:429-433`).
**Change:**
1. Add one async helper in `core/database.py`: `async def ch_query(sql, params=…, settings=…)` = `asyncio.to_thread(get_ch_client().query, …)` (thread-local client already exists and is correct, `database.py:59-79`); same for `ch_insert`.
2. Migrate every call site of `get_clickhouse_client()` (netflow, devices, link_utilization, traps, snmp, manual_maps, service_checks, metric/service_metric/host_metric/report services, both alert evaluators) to the helper. Mechanical but wide — do it router-by-router with the harness running.
3. Delete `get_clickhouse_client()` at the end; add a test asserting it's gone (`server/tests/test_no_singleton_ch.py` greps the app tree) so it can't creep back.
4. Bound the thread pool explicitly (uvicorn default is 40 × workers; set `anyio` capacity or a dedicated executor sized ~16) so CH can't be stampeded.
**Verify:**
1. Under M-load: `curl -w '%{time_total}'` on `/api/v1/devices` p95 < 300 ms **while** the k6 netflow mix runs (before-fix baseline recorded for comparison).
2. Event-loop stall probe (add a tiny `asyncio` heartbeat logger in DEBUG): max gap < 100 ms under M-load (baseline today: multi-second gaps during agent uploads).
3. Zero `Attempt to execute concurrent queries within the same session` errors in a 2 h M-load run (`journalctl -u zenplus-api | grep -c "concurrent queries"` = 0).
4. Grep-test from step 3 passes in CI.

## P1-T3 · Agent & sensor ingest: bounded batch writer **[blocker]**
**Effort:** 5 pd · **Depends on:** T2
**Problem:** `/agents/results/host` performs up to 9 synchronous CH inserts + row-by-row inventory upserts inline per upload (`host_metric_service.py:293-303,318-364`), batch size is uncapped (`schemas/agent.py:326`), idempotency fields are accepted but ignored (`agents.py:621-623`); sensor uploads block the same way (`sensor_api.py:519,596`) and 403 the whole batch on one stale ID (`:501-503`). Ceiling ≈ 240 agents; a recovering agent can stall the whole API.
**Change:**
1. Adopt the APM writer pattern (`apm_ingest.py` is the in-repo reference): per-kind bounded queues + one background flusher batching across requests (2000 rows / 1 s), `to_thread` inserts, HTTP 503 + `Retry-After` on full queue, drop/flush counters in an ingest-stats endpoint.
2. Cap request size: `metrics` ≤ 5,000 samples (Pydantic `max_length`), reject larger with 413 + guidance.
3. Enforce idempotency: `agent_ingest_ledger(agent_id, batch_id)` insert-first with `ON CONFLICT DO NOTHING`; duplicate batch → 200 with `duplicates` count actually set.
4. Inventory upserts become set-based statements (single `INSERT … ON CONFLICT` with `unnest`) executed by the flusher, not per-request loops.
5. Sensor uploads: same writer; per-item authorization (accept valid items, return per-item rejects) instead of whole-batch 403.
**Files:** `services/host_metric_service.py`, `api/v1/agents.py`, `api/v1/sensor_api.py`, `schemas/agent.py`, new migration for the ledger table.
**Verify:**
1. Harness at 300 simulated agents (3× today's ceiling): API p95 (device list) stays < 300 ms; CH `system.parts` new-part rate for `host_*` tables drops ≥ 5× vs baseline (batching proof: `SELECT count() FROM system.parts WHERE table LIKE 'host_%' AND modification_time > now()-3600`).
2. Replay the same batch file twice → CH row count unchanged (idempotency), response shows `duplicates > 0`.
3. `docker pause zenplus-clickhouse` 60 s during agent load → agents receive 503+Retry-After (simulator logs), **zero rows lost end-to-end** after unpause (sent-count vs `count()` reconciliation in the harness).
4. Sensor batch containing one unassigned device ID → other items accepted, response lists the rejected ID; nothing 403s wholesale.
5. 20,000-sample oversized POST → 413.

## P1-T4 · Notification outbox + leader-elected dispatcher **[blocker]**
**Effort:** 5 pd · **Depends on:** P0-T6, P0-T8
**Problem:** Notifications are sent synchronously in-process from request handlers and evaluator loops (sync `smtplib` inside async code, `alert_engine.py:98-133`), failures are `print()` and gone (`:714`), there is no retry/DLQ/escalation, channel rows are re-fetched N+1 per alert (`:631-654`), and trap alerts never notify at all (`:1121`).
**Change:**
1. Migration: `notification_outbox(id, alert_id, channel_id, payload, status queued|sending|delivered|failed|dead, attempts, next_attempt_at, last_error, created_at, delivered_at)` + `UNIQUE(alert_id, channel_id)`.
2. Alert writers (all five call sites) insert outbox rows **in the same transaction** as the alert row and stop dispatching inline.
3. New `notification_dispatcher_loop` (leader-elected via P0-T6 helper, tick 5 s): claim due rows `FOR UPDATE SKIP LOCKED`, deliver via the existing channel dispatchers moved to `to_thread`, exponential backoff (1 m → 5 m → 15 m → 1 h, max 8 attempts → `dead`), one channel+gateway fetch per tick (kill the N+1).
4. Wire trap alerts into the same path (fixes "trap rules are silent").
5. Surface `dead` rows in the UI alerts page + a `notification_failed` self-alert.
**Files:** new migration, `services/notification_dispatcher.py` (new), `alert_engine.py`, `host_alert_service.py`, `network_alert_service.py`, `server_health_service.py`, `main.py`.
**Verify:**
1. Stop Mailpit → flap a device → outbox row retries with backoff (`SELECT attempts, next_attempt_at …`); start Mailpit → delivered exactly once; `delivered_at` set.
2. `kill -9` the API between alert insert and delivery → after restart the notification is delivered exactly once (outbox survives; unique key prevents doubles).
3. Storm: flap 50 harness devices simultaneously → Mailpit receives exactly 50 (one per device), delivery completes < 2 min, API p95 unaffected during the storm (SMTP no longer on the event loop).
4. Trap-rule test: send a matching trap (`snmptrap` CLI) → email arrives (first time ever).
5. 8 forced failures → row goes `dead`, self-alert raised, visible in UI.

## P1-T5 · Alert semantics: flap damping + reliable poller→API path **[blocker]**
**Effort:** 3 pd · **Depends on:** T4
**Problem:** `cooldown` and `duration` are stored on rules but never read (`alert_engine.py:403,787`) — a flapping device pages every transition. The poller's alert POSTs are fire-and-forget to a hardcoded `http://localhost:8000` (`engine.go:463,545,565`, 10 s timeout, error = log) — every status change during an API restart is permanently lost.
**Change:**
1. Implement `cooldown` (suppress re-notification of the same rule+target within the window; alert row still recorded) and `min_duration` (breach must persist across a re-check before firing) in the event-driven engine; both already exist as columns/UI.
2. Poller: alert-engine URL from config/env (`ALERT_ENGINE_URL`, default stays localhost); failed POSTs go to a bounded on-disk retry queue (JSONL spool, replayed with backoff, drop-oldest at cap with counter — mirrors the sensor spool design).
3. Add a **reconciliation sweep** to the (already leader-elected) health sweeper: any device whose `devices.status` disagrees with the latest `device_status_log` entry older than 5 min is re-evaluated — belt-and-braces for lost events.
**Files:** `alert_engine.py`, `poller/internal/pinger/engine.go` (+ small spool package), `poller/internal/config/config.go`, `install.sh` (.env var), `server_health_service.py`.
**Verify:**
1. Flap driver: device down/up every 30 s for 30 min, rule cooldown 300 s → Mailpit shows ≤ 7 notifications (1 per 5 min), alert rows record every transition.
2. `min_duration=120` on a rule + 60 s blip → no alert; 3 min outage → alert.
3. `systemctl stop zenplus-api`, flap 5 devices, wait 2 min, start API → all 5 alerts appear ≤ 60 s after start (spool replay visible in poller journal), no duplicates (P0-T8 index).
4. Poller journal shows spool depth returning to 0.

## P1-T6 · Query hygiene, batch 1 (NetFlow + link-utilization + status log) **[high]**
**Effort:** 4 pd · **Depends on:** T2
**Problem:** ~35 NetFlow queries scan raw `flow_records` up to 720 h (`netflow.py`), `/netflow/overview` runs `uniqExact` over every distinct IP in the window (`:594-595`), top-talkers/capacity do UNION-ALL double scans; link-utilization aggregates the whole fleet from the raw table with the LIMIT applied in Python (`link_utilization.py:99-116,285`); the dashboard hits an unbounded full-table `GROUP BY` on `device_status_log` every poll (`devices.py:194-203`). The 5m rollups these need are **already populated and never read**.
**Change:**
1. Route NetFlow endpoints to `flow_traffic_5m` when `hours > 6` and the query doesn't need per-flow fields; keep raw for forensics with a hard row cap.
2. `uniqExact` → `uniq()` everywhere user-facing.
3. Link-utilization list reads `snmp_if_metrics_5m` with `ORDER BY … LIMIT n` in SQL; detail page keeps raw for the short window only.
4. `device_status_log` current-status query gets a 7-day predicate + a small `device_status_current` table maintained by the poller status write (or `argMax` over the bounded window).
5. Record a before/after query-log benchmark (`system.query_log`) for the top 10 queries under M-load with 30 days of seeded flows.
**Verify:**
1. Parity: for 3 fixed windows, old vs new endpoint outputs agree within 1% (script the comparison).
2. `system.query_log`: `/netflow/overview` and link-utilization list `read_rows` drop ≥ 10× on the seeded dataset; wall time < 500 ms each.
3. Dashboard KPI + netflow pages under M-load: p95 < 500 ms.

## P1-T7 · Build the missing 1 h rollups **[high]**
**Effort:** 1.5 pd
**Problem:** `snmp_metrics_1h` and `snmp_if_metrics_1h` exist with 365-day TTLs but their MVs were deferred in migrate-004 ("alias shadowing") and never written — the tables are permanently empty, so long-range charts read 5m (or raw) forever.
**Change:** new ClickHouse migration creating the 5m→1h MVs (rename intermediate aliases to avoid the shadowing issue documented in `migrate-004-snmp-clickhouse.sql:67-71,155`), backfill one-shot `INSERT … SELECT` guarded by `count()=0` (pattern from migrate-011/012); route range > 7 d queries in `devices.py`/`link_utilization.py` to `_1h`.
**Verify:** MV populates on live inserts (`count()` grows); backfill totals match a manual 5m aggregation for a sample device/day; a 30-day interface chart's query hits `_1h` in `system.query_log` and returns the same shape as before.

## P1-T8 · Redis read-through cache for hot aggregates **[high]**
**Effort:** 2 pd · **Depends on:** T2
**Problem:** 186 dashboard auto-refresh hooks × open tabs re-run the same fleet-wide aggregates every 15–30 s; Redis is connected but used for nothing except pub/sub.
**Change:** small `cache_json(key, ttl, producer)` helper (Redis GET → produce → SETEX); apply to the ~10 hottest read endpoints (dashboard KPIs, `/devices/current-uptime`, `/devices/current-metrics`, `/servers/latest-metrics`, `/alerts/stats`, netflow overview default window) with 30–60 s TTLs. Skip caching when caller passes explicit custom ranges.
**Verify:** two browser sessions open for 10 min → CH `query_log` count for the cached queries ≈ once per TTL, not once per tab per interval; cache hit ratio logged; data staleness ≤ TTL (flip a device, KPI updates within 60 s).

## P1-T9 · Postgres lifecycle purges **[blocker]**
**Effort:** 2 pd · **Depends on:** P0-T6
**Problem:** Nothing ever deletes from `alerts`, `audit_logs`, `report_runs` (full HTML in a TEXT column with a dead `expires_at`), `discovery_runs`/results, `agent_commands`/results, `snmp_credential_audit` — unbounded growth on the OLTP database.
**Change:** one leader-elected `lifecycle_sweeper_loop` (hourly) with per-table policies in `system_settings` (defaults: resolved alerts 180 d, audit 365 d, report_runs honor `expires_at` else 90 d, discovery 90 d, commands 30 d, credential audit 365 d); batched deletes (`DELETE … WHERE id IN (SELECT … LIMIT 5000)` loops); report HTML moves out of the row to `data/reports/` (or artifact store in Phase 2) with the row keeping a path.
**Verify:** seed aged rows (SQL fixture) → sweeper prunes them on schedule (journal line with per-table counts); a live report is still downloadable after its row's HTML moved to disk; `pg_stat_user_tables.n_dead_tup` returns to baseline after autovacuum; purge is idempotent (second run deletes 0).

## P1-T10 · Hot-path Postgres write reduction **[high]**
**Effort:** 2 pd
**Problem:** The poller UPDATEs `devices` on **every successful ping**, not just transitions (`engine.go:406-410` → `postgres.go:87-94`) ≈ 1.44 M dead tuples/day at 1 k devices on a 5-index table; heartbeats rewrite a GIN-indexed JSONB every 30 s (`agents.py:471-491`); sensor uploads UPDATE devices in a Python loop (`sensor_api.py:534-545`).
**Change:** poller writes `devices.status` only on transition; `last_seen`/`last_rtt_ms` refresh becomes a single batched `UPDATE … FROM (VALUES …)` every 30 s for changed devices; agent heartbeat updates `capabilities` only when its hash changes (compare in SQL or app); sensor device updates become one set-based statement per batch.
**Verify:** `pg_stat_user_tables` for `devices`/`agents`: `n_tup_upd` per hour drops ≥ 90% under M-load vs baseline; UI `last_seen` still advances (≤ 60 s lag acceptable); status transitions still alert (flap test).

## P1-T11 · Poller: decouple ping / service-check / SNMP cycles **[blocker]**
**Effort:** 2 pd
**Problem:** Ping and service-check cycles run synchronously on the same engine goroutine (`engine.go:256-259`) — an observed 5-check cycle took 10 s and stalled all pinging. SNMP is already `go`-dispatched with an overlap guard; the other two are not.
**Change:** run each cycle type in its own goroutine with its own skip-if-running guard (copy the `snmpRunning` pattern, `engine.go:891-897`); bound the transition-writeback goroutines with a worker pool (they're currently unbounded per flap, `engine.go:406-454`).
**Verify:** harness: 20 service checks against a blackholed target (10 s timeouts) → `ping_metrics` insert timestamps for a control device stay strictly 60 ± 2 s apart for 30 min (baseline shows multi-second gaps); goroutine count during a 100-device flap stays bounded (`:8081/health` exposes `runtime.NumGoroutine`, add it in this task).

## P1-T12 · Poller: honor per-device SNMP intervals **[high]**
**Effort:** 1 pd
**Problem:** `snmp_poll_interval` is loaded (`postgres.go:191`) and never used; `runSNMPCycle` polls every enabled device every 30 s (`engine.go:898-903`) — the comment claiming a next-due map is false. No pressure-relief for large fleets.
**Change:** implement the next-due map exactly as the comment describes (default 30 s, per-device override honored, floor 30 s / ceiling 1 h).
**Verify:** set one device to 300 s → `snmp_metrics` rows for it arrive every 300 ± 15 s while a control device stays at 30 s (`SELECT device_id, timestamp FROM snmp_metrics … ORDER BY timestamp`); UI editing of the interval round-trips.

## P1-T13 · Poller: buffer sizing + honest flush failure handling **[high]**
**Effort:** 1 pd · **Depends on:** P0-T10
**Problem:** `snmpIfBuffer` (4,000) is sized below one cycle's burst at ~80×48-port devices; a failed flush discards up to 1,000 rows with only a printf (`clickhouse.go:95-98`).
**Change:** size buffers from fleet at sync time (`devices × avg_interfaces × 2`, capped); failed flushes retry ×3 with backoff before counting as dropped (counter from P0-T10); make batch size/flush interval env-tunable.
**Verify:** harness at 200×48-port simulated devices for 1 h → `dropped_snmp_if` stays 0 and CH row rate matches expected samples (±2%); with `docker pause` 10 s mid-cycle → retries succeed, still 0 dropped.

## P1-T14 · Trap path hardening **[high]**
**Effort:** 2 pd
**Problem:** Trap listener accepts any community (`traps.go:93`), does one synchronous 2 s PG lookup per trap against a 10-conn pool (`traps.go:187-192`), and spawns an unbounded goroutine per trap for the alert POST (`engine.go:511-518`).
**Change:** optional `SNMP_TRAP_COMMUNITY` allowlist (empty = accept, log-only warn mode first release, enforce next); in-memory IP→device cache with 60 s TTL + singleflight; bounded worker pool for trap processing; batch the PG lookups.
**Verify:** storm test `snmptrap` loop at 500/s for 60 s → poller PG pool never exhausts (`pg_stat_activity` count ≤ 10), traps land in CH at drain rate, drop counter accounts for the remainder — nothing hangs; wrong community logged (warn mode) then rejected (enforce mode).

## P1-T15 · NetFlow: socket capacity + kernel-drop visibility + allowlist **[high]**
**Effort:** 2 pd
**Problem:** Single reader goroutine, default kernel receive buffer, no `SetReadBuffer`, no `SO_REUSEPORT` — bursts drop **in the kernel**, uncounted by the collector's otherwise-good drop counters. Exporter allowlist defaults to accept-all.
**Change:** `SetReadBuffer(8 MB)`; N reader goroutines via `SO_REUSEPORT` sockets in **one process** (template cache stays shared/process-wide, so this is safe — multi-process split waits for Phase 3 template persistence); expose kernel drops (`/proc/net/udp` queue/drops for :2055) in `:8091/health`; ship `NETFLOW_ALLOWED_EXPORTERS` populated by `install.sh` prompt, warn-mode default.
**Verify:** flood test (replay a pcap of flows at 50 k pps with `udpreplay`) → kernel drop gauge visible and near-zero at target rate where baseline showed silent loss (compare `records_ingested` vs sent count); unlisted exporter logged in warn mode.

## P1-T16 · Sensors: staleness sweeper, no double-polling, per-item accept **[blocker]**
**Effort:** 2 pd
**Problem:** A dead sensor stays `online` forever (sweep covers agents/servers only, `server_health_service.py:287-314`); every sensor-assigned device/check is **also** executed by the central poller (`postgres.go:55-57,112-115` have no assignment exclusion vs `sensor_api.py:346-381`), double-counting results and polluting metrics; one stale ID 403s a whole sensor batch (fixed in T3 §5 — verify here).
**Change:** add sensors to the staleness sweep (stale 120 s, offline 600 s, `sensor_offline` alert, resolve on heartbeat); poller loaders exclude targets with a sensor assignment or `default_sensor_id` (config flag `POLLER_INCLUDE_ASSIGNED=false` default); UI shows sensor status from the swept value.
**Files:** `server_health_service.py`, `poller/internal/store/postgres.go`, `sensors` UI card.
**Verify:**
1. Stop a staging sensor → `sensors.status` = stale ≤ 3 min, offline + alert ≤ 11 min; restart → online + alert resolves.
2. Assign a device to the sensor → `SELECT DISTINCT poller_id FROM ping_metrics WHERE device_id=… AND timestamp > now()-600` returns **only** the sensor UUID (central `poller-01` disappears); unassign → poller resumes within one device-sync (60 s).
3. Mixed-validity sensor batch → per-item accept (from T3) confirmed against a live sensor.

## P1-T17 · Discovery: ownership leases, cross-worker cancel, contained probes **[high]**
**Effort:** 3 pd · **Depends on:** P0-T9
**Problem:** Runs are in-process asyncio tasks tracked in a per-worker dict (`discovery_executor.py:45`) — cancel fails ~50% with 2 workers (`:820-825`); ICMP forks a `ping` process per target (up to 4,096 forks/scan, `discovery_probes.py:34-37`); TLS probing blocks the event loop (`:156-168`).
**Change:** add `claimed_by`/`lease_expires_at`/`cancel_requested` to `discovery_runs` (migration); owner renews lease each progress write; executor checks `cancel_requested` between batches; `recover_stuck_runs` (P0-T9) now keys on expired leases; ICMP via a raw-socket async pinger (or a single long-lived `fping` subprocess fed via stdin) with the existing semaphore; wrap TLS cert fetch in `to_thread`.
**Verify:** start a 2,000-target scan; cancel via the API from repeated requests (round-robins both workers) → run stops ≤ 10 s every time (10/10 trials); `ps` during a scan shows ≤ 2 helper processes (was: hundreds); API p95 during a 4,096-target scan stays < 500 ms under S-load; restart mid-scan → only the dead worker's run is recovered after lease expiry.

## P1-T18 · Evaluator N+1 elimination **[high]**
**Effort:** 2 pd · **Depends on:** P0-T6, P0-T8
**Problem:** `network_alert_service._apply` runs a SELECT per (rule × device × interface) every 60 s (`:432-453`) ≈ 60 k statements/min at 500 devices; `host_alert_service` similar (`:220-262` fires an unconditional UPDATE per non-breaching pair).
**Change:** set-based rewrite: one query per rule class fetching current values fleet-wide (already the case), then one `SELECT existing active alerts WHERE (rule_id, target) IN (…)`, one `INSERT … ON CONFLICT DO NOTHING` for new breaches (P0-T8 index), one `UPDATE … WHERE id IN (…)` for resolutions.
**Verify:** `pg_stat_statements` (enable on staging): evaluator statement count per tick drops from O(rules×targets) to ≤ 5 per rule class at M-load; behavior parity — same alerts raised/resolved for a scripted breach matrix (fixture with 3 rules × 10 servers, expected outcomes asserted).

## P1-T19 · APM service graph without the self-join **[parallel-ok]**
**Effort:** 2 pd
**Problem:** The service map self-joins `apm_spans` at query time with no LIMIT (`apm_services.py:311-317`) — first candidate for `MEMORY_LIMIT_EXCEEDED` at scale; `apm_service_graph` (the table built for this) is written by nothing; `_fetch_trace_spans` has no time predicate (`apm_traces.py:160-166`).
**Change:** populate `apm_service_graph` incrementally from the APM batch writer (aggregate parent→child edges per flush); service-map endpoint reads the table; add a time-bounded fallback; add `ts_bucket` predicate to `_fetch_trace_spans` derived from the trace's first-seen window.
**Verify:** seed OTLP traffic (harness or `telemetrygen`) → service map renders identically from the new table (edge set parity vs old query on a small dataset); `system.query_log` shows the self-join gone; map query < 200 ms at 10× span volume.

---

## Exit Gate — Phase 1

Run on staging with the harness. All **[blocker]** tasks complete; record in `gates/phase-1-gate-YYYYMMDD.md` (same template style as Phase 0).

| # | Drill | Pass criteria |
|---|---|---|
| G1 | **M-load endurance**: 500 devices / 100 agents / 10 VU dashboard mix, 24 h | API p95 < 500 ms throughout; event-loop max stall < 100 ms; poller drop counters 0; CH sent-vs-stored reconciliation ±0 rows for agent metrics; no OOM/restarts (`NRestarts` unchanged) |
| G2 | **Alert storm**: flap 100 devices simultaneously (iptables), rules with cooldown 300 s | Exactly 100 down notifications ≤ 2 min, zero duplicates; ≤ 100 more over the next 30 min of flapping (cooldown proof); all delivered (outbox `dead`=0) |
| G3 | **Restart resilience**: `systemctl restart zenplus-api` during G1 + a flap | No lost alerts (spool replay), no duplicate emails, discovery runs unaffected, agents get 503s then recover with zero data loss |
| G4 | **CH stall**: `docker pause zenplus-clickhouse` 120 s during G1 | Agents/sensors get 503 backpressure; poller retries then counts drops honestly; system fully recovers; dashboards degrade gracefully (errors, not hangs) |
| G5 | **Query benchmarks**: scripted top-10 query suite on 30-day seeded data | Each ≥ 10× fewer `read_rows` than the recorded Phase-0 baseline; parity checks within 1% |
| G6 | **Growth check**: compare `pg_database_size` and per-table sizes day 1 vs day 3 of G1 | Bounded growth consistent with retention policies; `n_dead_tup` steady-state |

**Deliverables recap:** load harness in-repo · all migrations numbered/locked · updated capacity statement (measured, not derived) added to `zenplus_architect.md` §9.1.
