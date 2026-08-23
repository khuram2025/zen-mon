# Phase 3 — Distributed Collection & the Multi-Site Probe Fabric

**Goal:** monitoring *coverage* becomes fault-tolerant: pollers shard and fail over automatically; sensors are trustworthy vantage points (spooled, concurrent, updatable, watched); "down" means the network agrees (M-of-N quorum); a site losing its WAN produces exactly **one** alert. This phase delivers the multi-site reachability requirement end-to-end.
**Duration:** 6–10 weeks (~50 pd). **Hardware:** none beyond Phase 2 (optional: extra poller VMs when fleet demands); 2+ sensor VMs and a firewallable "site" lab network for the gate.
**Entry criteria:** Phase 2 gate passed. Design references: `zenplus_architect.md` §7.6 (sharding) and §8 (quorum); prior art `Documentation/enahce1/05-EPIC-E6-*.md` and `05-EPIC-E11-*.md`.

Order matters inside this phase: T1–T3 (pollers) and T4–T8 (sensor hardening) can run in parallel; T9–T11 (quorum) depend on T4/T5/T8; UI (T12) last.

---

## Workstream A — Poller scale-out

### P3-T1 · Shard model + lease claiming **[blocker]**
**Effort:** 5 pd
**Problem:** `LoadDevices`/`LoadSNMPDevices`/`LoadServiceChecks` have no shard predicate (`poller/internal/store/postgres.go:50-115`); a second poller double-polls everything. `POLLER_ID` is a label only.
**Change:**
1. Migration: `poller_instances(id text pk, hostname, capacity_shards int, last_heartbeat_at)`; `shard_leases(shard smallint pk, poller_id, lease_expires_at)`; `devices.shard smallint` + `service_checks.shard smallint` populated by trigger as `abs(hashtext(id::text)) % 256`.
2. Poller startup + every 30 s: upsert instance heartbeat; claim unowned/expired shards up to `capacity` with `FOR UPDATE SKIP LOCKED`; renew own leases (TTL 90 s); release surplus when instances join (rebalance toward `total/instances`).
3. Loaders add `WHERE shard = ANY($mine)`; device-sync tick re-reads owned shards.
4. Health endpoint reports owned shard count + lease age; controller UI page lists poller instances.
**Verify:**
1. Two pollers on staging: `SELECT poller_id, count(*) FROM shard_leases GROUP BY 1` → 128/128 (±10) within 2 min of the second starting.
2. Double-poll check: for 10 sample devices, `SELECT DISTINCT poller_id FROM ping_metrics WHERE device_id=… AND timestamp>now()-300` returns exactly one id each.
3. `kill -9` poller B → poller A owns 256/256 within lease TTL + one claim tick (≤ 2 min); per-minute `ping_metrics` counts show a gap ≤ 2 min for B's shards, then full rate.
4. Restart B → rebalance back to ~128/128 ≤ 2 min, no device polled twice during handover (repeat check 2 during the transition window).

### P3-T2 · Status ownership & flap-state handover **[blocker]**
**Effort:** 2 pd · **Depends on:** T1
**Problem:** `DownCount` flap counters and interface-counter snapshots (`prevIfs`) are in-process (`engine.go:115-116`, `snmp/collector.go:23-24`); after a shard moves, the new owner starts blind — first SNMP cycle emits `in_bps=0` and down-confirmation counters reset (false "up" risk).
**Change:** only the lease-holder writes `devices.status`/`service_checks.status` for its shards (guard in the UPDATE `WHERE`); persist per-device `down_count` and last-known status compactly in PG (batched with the P1-T10 refresh write) so a new owner seeds its counters; suppress the first bps sample after takeover (emit null instead of 0 — the diff needs two samples, `collector.go:656-662`).
**Verify:** move a shard (stop B) mid-outage of a device at down_count=2 → the device still confirms down on schedule (no reset to 0; alert timing unchanged ±1 cycle); interface charts across the handover show a 1-sample gap, **not** a zero-spike (query min(in_bps) around the window).

### P3-T3 · NetFlow horizontal path **[high]**
**Effort:** 3 pd · **Depends on:** P1-T15
**Problem:** v9/IPFIX template caches are per-process (`cmd/netflow-collector/main.go:62-63`) — any multi-instance or failover split silently drops data-records until templates re-arrive.
**Change:** persist learned templates + sampler config to Redis (write-through on template packets, load on start, TTL 24 h matching in-memory) so a restarted/failover collector decodes immediately; collector id from hostname (drop the hardcoded `netflow-01`).
**Verify:** replay a v9 stream with 30-min template refresh; restart the collector mid-stream → `records_ingested` resumes < 5 s with **zero** `waiting for template` drops (baseline shows drops until refresh); VIP failover (P2 G1 rerun) shows the same.

---

## Workstream B — Sensor hardening (trustworthy vantage points)

### P3-T4 · Durable result spool **[blocker]**
**Effort:** 4 pd
**Problem:** Failed uploads are logged and **discarded** (`poller/cmd/sensor/main.go:386-392`); the schedule map is advanced before upload success (`:445`), so a WAN blip permanently loses that vantage's results — fatal for quorum (silent abstention).
**Change:** SQLite (WAL mode) spool at `/var/lib/zenplus-sensor/spool.db`: results enqueue locally first; uploader drains oldest-first in batches with backoff; cap 200 MB / 48 h with drop-oldest + counter; heartbeat reports real `queue_depth`/`queue_dropped_count` (fields exist, hardcoded 0 today at `:369`).
**Verify:** block the sensor's WAN 10 min while it probes → restore → controller CH gains the full gap's rows (per-minute count for that `poller_id` has no hole), no duplicates (T8); heartbeat showed rising then zero `queue_depth`; cap test (tiny cap override) drops oldest with counter incrementing, never blocks probing.

### P3-T5 · Concurrent execution + schedule integrity **[blocker]**
**Effort:** 3 pd
**Problem:** `maxWorkers` is parsed and unused; all checks run serially in the 1 s tick goroutine (`main.go:354-489`) — one hung 10 s probe stalls every other check *and* delays heartbeat; `uploadEvery` is ignored (uploads every tick); schedule state lost on restart.
**Change:** worker pool (semaphore, default 100) with per-target overlap guard and jitter; heartbeat/config/upload loops in their own goroutines on their configured intervals; persist last-run times in the spool DB so restart doesn't re-fire everything at once.
**Verify:** assign 200 checks including 10 against a blackholed target (10 s timeouts) → all healthy checks complete within their interval (controller `service_metrics` cadence steady per check); heartbeat interval stays 30 ± 2 s throughout; restart the sensor → no thundering herd (spread of first-run timestamps ≥ jitter window).

### P3-T6 · Sensor command channel + on-demand probe **[high]**
**Effort:** 3 pd
**Problem:** No way to task a sensor (`has_commands` hardcoded `False`, `sensor_api.py:285`); assignment changes take up to the 60 s config poll; "test now from site B" impossible.
**Change:** mirror the agent pattern: `sensor_commands` table + `POST /sensor/commands/poll` + result endpoint; heartbeat returns real `has_commands`; commands v1: `run_check_now(check_id)`, `refresh_config`, `upgrade_sensor` (T7), `upload_diagnostics`; UI: "Run from this sensor" button on the check page showing the returned result.
**Verify:** trigger run-now from the UI → result row (with correct `poller_id`) in ≤ 35 s (next heartbeat + poll + execution); `refresh_config` applies an assignment change without waiting the 60 s; command expiry works (queue one against a stopped sensor, expires per TTL).

### P3-T7 · Sensor self-update channel **[high]**
**Effort:** 3 pd
**Problem:** No OTA for sensors — upgrades are a manual installer re-run per site (`sensor_api.py:807-929`); artifacts + manifest are already published (`/api/v1/sensor/bin/...`) but never consumed.
**Change:** sensor checks the manifest daily + on `upgrade_sensor` command; ring field on sensors (`canary|stable`, reuse agent ring UX); download to temp, verify sha256, swap binary, restart via systemd, report old→new in next heartbeat; failed health after swap → automatic revert to `.bak`.
**Verify:** publish a canary sensor build → canary sensor updates within the window, stable one doesn't; version visible in Sensors UI; corrupt-binary test (bad sha) → refused, still running old; forced bad-health build → auto-revert observed in journal.

### P3-T8 · Server-side idempotency + clock guard for sensor uploads **[blocker]**
**Effort:** 1.5 pd
**Problem:** `idempotency_key` is sent (`main.go:300`) and never checked; spool retries (T4) would double-count votes; sensor timestamps get a naive `datetime.now()` fallback (`sensor_api.py:469-478`) with no skew rejection.
**Change:** `sensor_ingest_ledger(sensor_id, idempotency_key)` insert-first dedupe (same pattern as P1-T3); reject/adjust batches with |skew| > 300 s using the agent clock-skew approach (migrate-041 precedent); heartbeat response includes `server_time` for the sensor to log its offset.
**Verify:** replay the same spool batch twice → CH count unchanged, second response flags duplicate; skewed-clock sensor (fake `date -s` in a test VM) → results land at corrected timestamps, skew visible in sensor status.

---

## Workstream C — Sites, quorum, and vantage-aware alerting

### P3-T9 · Site model + probe policy schema **[blocker]**
**Effort:** 3 pd
**Change (migration + admin API + minimal UI):** `devices.site_id`, `service_checks.site_id` (nullable FKs to the existing `sites`); `sensor_groups` + members (site-linked); `service_checks.probe_policy JSONB` and the same on devices for ping quorum — shape per `zenplus_architect.md` §8.2 (`vantages` = sensors/groups/include_controller, `quorum` = `min_reporting`/`down_threshold`, `on_disagreement`); `check_vantage_status` table; site CRUD gains delete-dependency guard (currently unguarded, `sensors.py:527-534`).
**Verify:** migration applies + lock updated; create site → group → attach 2 sensors → set a check's policy via API; ETag/config for both sensors includes the check (config poll shows it); deleting a site with members is refused with a clear error.

### P3-T10 · Quorum evaluator + site-isolation suppression **[blocker]**
**Effort:** 5 pd · **Depends on:** T4, T5, T8, T9, P1-T16 (sensor staleness)
**Change:**
1. Result ingest updates `check_vantage_status` set-based (status, consecutive_failures, last_result_at per vantage).
2. Leader-elected `quorum_evaluator_loop` (tick 15 s) classifies each policy-enabled target per the §8.3 matrix (confirmed down / partial / up / insufficient-data) and drives alert transitions through the outbox with **vantage context** in the payload (list of down/up sites, counts).
3. Site-isolation rule: sensor stale/offline **or** >70% of its assigned targets failing while ≥1 other vantage sees them up → single `site_unreachable` alert (per sensor), exclude its votes, suppress dependent target alerts (reuse the topology-suppression path, `alert_engine.py:289-311`).
4. Legacy single-vantage checks (no policy) keep the existing behavior unchanged.
**Verify (lab, 3 vantages: sensor-A, sensor-B, controller):**
1. Block target at A+B (iptables on target from those networks) → `confirmed down` alert ≤ 45 s naming both sites; Mailpit shows exactly one notification.
2. Block at A only → `partial/degraded` warn (distinct template), **no** page; status matrix shows A=down, B/controller=up.
3. Power off sensor-A entirely → exactly one `site_unreachable` alert; its in-flight target alerts suppressed; quorum for shared checks continues on B+controller (verify a real target-down still pages via 2-of-2 remaining).
4. Restore A → site alert resolves; spool backfill (T4) does **not** retro-fire stale transitions (evaluator ignores results older than 2× interval — assert no new alerts on backfill).
5. `service_checks.status` single scalar now reflects the quorum verdict (not last-writer) — flip order of uploads in a scripted race and confirm stable outcome.

### P3-T11 · Per-vantage ClickHouse rollup **[high]**
**Effort:** 1.5 pd
**Problem:** Rollups deliberately collapse vantages (`service_metric_service.py:71-85`) and `poller_id` is in no sort key — per-site history means full scans.
**Change:** new MV `service_metrics_by_vantage_5m` (and ping equivalent) with `ORDER BY (service_check_id, poller_id, timestamp)`, 90-day TTL, daily partitions; API endpoints for per-vantage series; blended series stays for existing charts.
**Verify:** MV populates from live traffic; per-vantage query for a check reads < 10k rows (`system.query_log`) vs full-scan baseline; series for A vs B visibly differ during scenario 2 above.

### P3-T12 · Vantage UI **[high]**
**Effort:** 4 pd · **Depends on:** T10, T11
**Change:** check/device detail gains a **site × status matrix** (chips: up/down/stale per vantage, last-seen ages) + per-vantage RTT/latency series toggle; alerts render vantage context ("Down from Riyadh-DC, Jeddah-Branch; up from HQ"); Sensors page shows spool depth, version, ring, staleness (now real from T4/T7/P1-T16); site dashboard lists per-site sensor + probe health.
**Verify:** during gate scenarios the matrix reflects each state within one refresh interval; screenshots attached to the gate report; notification template renders site names (Mailpit inspection).

---

## Exit Gate — Phase 3

Lab: 2 controllers (Phase 2), 2 pollers, 2 sensor VMs on separate firewallable networks + controller-as-vantage, S-load running. Record in `gates/phase-3-gate-YYYYMMDD.md`.

| # | Drill | Pass criteria |
|---|---|---|
| G1 | **Poller kill** under load | Shards reassign ≤ 2 min; metric gap ≤ 2 min for affected shards only; no device double-polled during handover; no zero-bps spike; flap-in-progress still confirms on schedule |
| G2 | **Poller scale-in/out** | Add third poller → rebalance ≤ 2 min, even split; remove → same |
| G3 | **Site WAN cut, 10 min** (firewall sensor-A's network) | Exactly **one** `site_unreachable` alert; zero false target-down pages; quorum continues on remaining vantages; on restore: alert resolves, spool backfills the gap into CH with no duplicates and no retro-alerts |
| G4 | **Quorum truth table** | Scenarios 1/2/4 from T10 verify pass exactly as specified (confirmed-down pages; single-vantage blip does not; insufficient-data does not flap) |
| G5 | **Sensor fleet ops** | Ring-based sensor update canary→stable observed; run-now from UI ≤ 35 s; corrupt update refused; command expiry works |
| G6 | **NetFlow failover rerun** | Collector restart + VIP failover with zero template-wait drops (T3) |
| G7 | **48 h mixed soak** | All Phase 1/2 gate invariants still hold (no duplicate alerts, no drops, no growth anomalies) with the new fabric active |

Passing G1–G7 delivers the original product promise: **reachability checked from multiple sites, with agreement, surviving the loss of any single node, poller, or site.**
