# Audit: PING/ICMP Availability Engine

Date: 2026-06-10 — Auditor: senior network engineering review (read-only against live box 192.168.8.152)
Scope: `poller/internal/pinger/`, `poller/internal/checker/icmp.go`, `poller/cmd/poller`, ClickHouse ping tables/rollups, `server/app` status + alerting + reporting paths.

Live state at audit time: 27 devices (all ping-enabled, intervals 30–120 s), 24 down / 1 degraded / 2 up (lab), poller `poller-01` up 5 h 19 m, `last_cycle_ms` ≈ 3076.

---

## 1. Ping mechanics: cadence, packets, timeout, metrics, privileges

### Cadence and scheduling
- The engine ticks **every 1 second** and selects devices that are *due* per their own `ping_interval` (`poller/internal/pinger/engine.go:220`, `engine.go:321-338`). Per-device interval comes from `devices.ping_interval` (PG, default 60 s — `scripts/init-postgres.sql:43`); fallback 60 s if unset (`engine.go:327`, `effectiveInterval` at `engine.go:730`).
- Device list re-synced from PG every **60 s** (`DeviceSyncInterval`, `config.go:77`; SQL `WHERE ping_enabled = TRUE` at `poller/internal/store/postgres.go:50-57`). Status + DownCount are preserved across syncs (`engine.go:303-306`).
- Live intervals on this box: min 30 s, max 120 s (`SELECT min(ping_interval), max(ping_interval) FROM devices` → `30 | 120`).

### Per-probe parameters (hardcoded, no env override)
`poller/internal/config/config.go:69-78`:
- `PingCount = 3` packets per cycle, `PingInterval = 500 ms` between packets, `PingTimeout = 3 s` total, library `prometheus-community/pro-bing` (`pinger.go:8`).
- `DownThreshold = 3` consecutive failed cycles, `DegradedRTTMs = 100`, `DegradedLossPct = 10`.
- None of the ping tunables are environment-configurable (only `POLLER_ID` and `POLLER_PRIVILEGED` are read from env) — there is no UI/API to change count/timeout/thresholds.

### Metrics captured per cycle
`pinger.go:73-87` → `PingResult` (`result.go:25-38`): `IsUp` (= at least 1 of 3 replies), avg RTT, min/max RTT, **jitter = StdDevRtt**, packet-loss fraction, packets sent/recv, poller_id, timestamp. Written to ClickHouse `zenplus.ping_metrics` (`store/clickhouse.go:120-162`; schema `scripts/init-clickhouse.sql:6-24`).
- `IsUp` requires ≥1 reply of 3 (`pinger.go:60`); a device answering 1/3 packets is "up" with 66 % loss → classified `degraded`.
- Failed cycles are written with `rtt_ms = 0` (zero-value `RTT`, `pinger.go:61-71`) — this matters for the rollup math (see §3).
- IPv6 targets: `ping_metrics.ip_address` is an `IPv4` column; IPv6 device IPs are coerced to `0.0.0.0` (`clickhouse.go:137-139`). The ping itself would work (pro-bing supports v6) but the stored IP is wrong.

### Privileged vs unprivileged ICMP
- **Device pinger**: `Privileged = getEnv("POLLER_PRIVILEGED", "false")` (`config.go:78`). The live `/opt/zenplus/.env` does **not** set `POLLER_PRIVILEGED` → the fleet pinger runs **unprivileged (UDP/ICMP datagram sockets)**. This works because `net.ipv4.ping_group_range = 0 2147483647` on the box (verified via `sysctl`), but it silently ignores the CAP_NET_RAW machinery below.
- **Service-check ICMP** (`checker/icmp.go:60`): hardcodes `SetPrivileged(true)` (raw socket) with a one-shot unprivileged fallback on error (`icmp.go:66-73`). So the two ICMP paths use *different* socket types on the same box.
- systemd unit `zenplus-poller.service`: `AmbientCapabilities=CAP_NET_RAW`, plus a self-heal drop-in `10-setcap.conf` running `setcap cap_net_raw,cap_net_bind_service+ep /opt/zenplus/bin/zenplus-poller` on every start (verified: `getcap` → `cap_net_bind_service,cap_net_raw=ep`). Installer also setcaps (`scripts/provision-main-appliance-golden.sh:198`). Capability story is solid; the env default just doesn't use it for device pings.

### Worker model / cycle timing (observed)
- `runPingCycle` is called **synchronously** from the engine's main select loop (`engine.go:256-257`) and blocks until the whole batch completes. Worker pool hardcoded to **100 goroutines** (`engine.go:349`), semaphore pattern in `PingBatch` (`pinger.go:90-118`).
- Live journal: every cycle takes ~3.06–3.10 s ("Ping cycle complete: 12 results in 3089ms") because the 24 down devices each consume the full 3 s timeout. The cycle duration is gated by the slowest device.

---

## 2. Up/down determination, flap damping, dependency awareness

### Down logic
`engine.go:374-460 processStatusChange`:
- `!IsUp` → `DownCount++`; status flips to `down` only at `DownCount >= 3` (3 consecutive failed *cycles*, i.e., 9 lost packets). With a 60 s interval that is **~3 minutes detection latency**; with 120 s, ~6 minutes. There is no fast-recheck ("confirm down now") path — Nagios/SolarWinds-style accelerated re-checks on first failure are absent.
- Recovery is immediate: first up result resets `DownCount = 0` and sets `up`/`degraded`.
- `degraded` if avg RTT > 100 ms **or** loss > 10 % (`engine.go:397`).

### Flap damping: none for degraded, asymmetric for down
- down has 3-strike entry damping but **degraded↔up flips on a single sample in both directions**. Live evidence: device `568cd587-…` produced **73 status changes in 24 h** (37 up→degraded, 36 degraded→up; ClickHouse `device_status_log` transition matrix). Each flip writes a status-log row, publishes Redis, and POSTs the alert engine.
- Alert-side damping is also missing: `alert_rules.cooldown` (default 300 s, `api/v1/alert_rules.py:58`) is **selected but never enforced** in `alert_engine.py` (fetched at line 388, never referenced again). Result on the live box: **76 alert rows inserted in the last 24 h** (278 total), nearly all from the one flapping device against 3 enabled global rules. Every flip also re-sends email/SMS/webhooks (`alert_engine.py:560-625`) — notification storm by design.

### Dependency awareness
- Mechanism exists: `topology_dependencies` table (`migrate-014`), API + Topology UI (`api/v1/topology.py:369`, `dashboard/src/pages/TopologyPage.tsx`), and `_find_suppressing_dependency` in `alert_engine.py:274-296` suppresses child alerts when an enabled `suppress_alerts` parent is `down/degraded/unknown` (suppressed alerts are inserted pre-resolved with metadata).
- **But it is empty on the live system**: `SELECT count(*) FROM topology_dependencies` → **0**. Dependencies are manual-only; nothing auto-derives them from discovery/topology (LLDP/CDP/default-gateway), so out of the box a dead router still pages for every device behind it.
- Design weaknesses even when populated:
  - **Race**: suppression checks the parent's *current PG status* at child-alert time. Parent and children are detected independently with the same 3-strike threshold; children frequently confirm down in the same cycle as (or before) the parent, so the first wave of alerts escapes suppression. No correlation/settling window (SolarWinds waits and groups).
  - Single-hop query only (works transitively in practice only because intermediate devices are themselves pinged and marked down).
  - Suppression only protects the *alert path*; status log/Redis/UI still churn.
- The poller-side parent suppression (`ParentCheckID`, `engine.go:674-679`) applies **only to service checks**, not device pings.

### Maintenance awareness for devices: missing/broken
- Maintenance windows exist **only for service checks** (`service_check_maintenance`, scope check/group/tag/all — `postgres.go:446-471`; poller mutes transitions at `engine.go:805-810`).
- Devices have a `'maintenance'` status in the enum (`init-postgres.sql:50-51`) and the UI counts it (`device_service.py:235`), but the poller ignores it: any ping result overwrites the status (the IsUp branch calls `UpdateDeviceStatus` **every cycle**, `engine.go:406-410`), and a `maintenance→up/down` transition fires the alert engine like any other change (`engine.go:413+`). Putting a device "in maintenance" is cosmetic and self-reverting within one ping interval.

### Other status-path findings
- Alert delivery is fire-and-forget HTTP to `http://localhost:8000/api/v1/alert-engine/evaluate` (`engine.go:463`); on API downtime or timeout the transition's alert is **lost permanently** (no retry/queue/reconciliation; error only logged, `engine.go:489-493`).
- `/api/v1/alert-engine/evaluate` (and `-service`, `-trap`) is **unauthenticated** ("No auth required - internal endpoint", `alert_engine.py:352-361`) while uvicorn listens on **0.0.0.0:8000** (verified with `ss`). Any LAN host can inject fake status changes → forged alerts + SMS/email floods, or fake recoveries that auto-resolve real alerts.
- If the poller process dies, device statuses freeze at their last value in PG; there is no server-side stale-poller sweep for *network devices* (one exists for the new Servers/agents module only). Devices would show "up" indefinitely with a dead poller.

---

## 3. Retention, rollups, and availability accuracy

### Retention (ClickHouse, `init-clickhouse.sql` / `migrate-012`)
| Table | Content | TTL |
|---|---|---|
| `ping_metrics` (raw) | every cycle | **30 days** |
| `ping_metrics_5m` | MV rollup | 90 days |
| `ping_metrics_1h` | MV rollup (incl. p95) | 365 days |
| `device_status_log` | transitions | none observed (no TTL clause) |

### Rollup math is structurally wrong (partial-aggregate rows)
- Both MVs aggregate **per insert block** into plain `MergeTree` targets (not `AggregatingMergeTree`), so each flush (5 s cadence, `FlushInterval`, `config.go:94`) produces *partial* rows. Live proof: `ping_metrics_5m` has **up to 11 rows per (device, 5-min bucket)** and `count(ping_metrics_5m) == count(ping_metrics) == 37 216` — the "rollup" currently has **zero compression** and is only correct if every reader re-weights by `sample_count`.
- `metric_service.py:76-92` does re-weight correctly (`sum(avg*sample_count)/sum(sample_count)`). But:
  - `devices.py:143-150 dashboard_uptime-stats` queries `countIf(is_up = 1)` against the rollup tables, which **have no `is_up` column** → every rollup attempt throws `UNKNOWN_IDENTIFIER` (reproduced live: `Code: 47 … Unknown expression … is_up … ping_metrics_5m`) and the bare `except: continue` (line 160-161) silently falls back to raw. Verified live: `GET /devices/dashboard/uptime-stats?hours=8760` returns happily with `"hours":8760` while reading only the 30-day raw table → **uptime numbers for any window > 30 days are silently computed from ≤30 days of data and labeled as the full window**.
  - the 1h MV (`migrate-012:100-128`) takes **unweighted** `avg(src_uptime_pct)` / `avg(src_avg_rtt_ms)` / `quantile(0.95)(src_avg_rtt_ms)` over partial 5m rows with varying `sample_count` → biased hourly aggregates and a p95 computed over averages-of-averages.
- `avg_rtt_ms` in both rollups includes the **rtt_ms = 0 rows from down samples** (MV has no `is_up = 1` filter, `migrate-012:48` / `init-clickhouse.sql:50`) → RTT charts at 5m/1h granularity are dragged toward zero in any bucket containing failures. The raw path masks this per-row (`metric_service.py:131-134`), the rollup path cannot.

### Availability reporting accuracy
- Reports compute availability as the **unweighted fraction of raw ping samples that were up** (`report_data_service.py:99-103`, `report_service.py:714-719`, trend `report_data_service.py:106-129` `avg(is_up)` on raw). Consequences:
  - **Maintenance windows are never excluded** for devices (none exist) — planned downtime counts against availability. (Service-check SLA also keeps counting during maintenance by design, `engine.go:717` comment.)
  - Sample-weighted, not time-weighted: a device pinged every 30 s carries 4× the weight of one pinged every 120 s in fleet-wide availability; a poller outage simply removes samples (gap = no penalty) rather than counting as unknown.
  - Reports read **only raw `ping_metrics`** (`report_service.py:637-651`) → any report window beyond 30 days silently truncates to 30 days, and the full window's rows are pulled into Python and filtered per device in O(N·devices) loops (`_device_uptime_pct`, `report_service.py:714`) — a 30-day report at 60 s cadence and 1 000 devices would fetch ~43 M rows into the API process.
- **Downtime duration is always zero**: the poller writes every status change with `durationSec = 0` (`engine.go:445`, `clickhouse.go:165-170`) and nothing ever back-fills it. Verified live: `countIf(duration_sec=0) = 190 of 190` rows in `device_status_log`. Yet duration is consumed as if real by: report "Top Problematic Devices" downtime scoring (`report_service.py:841-853` — downtime contribution always 0), PDF/Excel exports (`export_service.py:423,469`), and the device report UI total-downtime sum (`dashboard/src/lib/deviceReport.ts:315`). All downtime figures shown to users are fabricated zeros.
- `current-uptime` (`devices.py:166-209`) derives continuous uptime from the last `device_status_log` transition — reasonable, but inherits the unbounded status-log table and says nothing for never-flapped devices beyond `created_at` fallback.

---

## 4. Scalability of the ping engine

### Goroutine model
- 1 s scheduler tick → due-filter → batch with semaphore-bounded pool of **100 workers max** (`engine.go:349`); each worker runs a full pro-bing session (≥1.0 s for 3 packets @500 ms spacing even when healthy; 3 s when dead).
- Results: buffered channel to a single batch writer (cap 2 000, `clickhouse.go:59`; flush at 1 000 rows or 5 s). **`WriteResult` silently drops results when the buffer is full** (`clickhouse.go:77-82`, `select/default`) — no counter, no log line; at scale or during a ClickHouse stall, availability data silently disappears (gaps read as "no data", not downtime).
- Per up-device, **a new goroutine + a PG `UPDATE devices SET status,last_seen,last_rtt_ms` runs every cycle** (`engine.go:406-410`, `postgres.go:87-94`) even when nothing changed. At 10 k devices/60 s that is ~167 UPDATE/s of pure write churn on PG, with unbounded goroutine spawn if PG slows down.
- Status processing is serialized after the whole batch (`engine.go:363-371`) and each result takes the global engine mutex (`engine.go:375`).

### Ceiling estimate
- Throughput ≈ workers / per-ping-time: healthy fleet ≈ 100/1.0 s = **~100 devices/s → ~6 000 devices at 60 s intervals**; a fleet with many down devices degrades to 100/3 s ≈ **~33 devices/s → ~2 000 devices**. Because `runPingCycle` blocks the main loop, long cycles also delay service-check cycles and device sync (observed: back-to-back 3.1 s cycles with only 27 devices, 24 down — `journalctl`, `last_cycle_ms=3076` from `/health` on :8081).
- No sharding/distributed pollers for ping (single `POLLER_ID`; remote "sensors" exist as a separate subsystem), no adaptive worker sizing, no per-cycle deadline.
- Memory-side: device map + per-device state is trivial; the real scale limits are the synchronous cycle, the 100-worker cap, the per-result PG writes, and the report path pulling raw rows into Python.

### Test coverage
`engine_test.go` covers due-scheduling, 3-strike down, degraded-on-latency, service retry threshold, TLS warning, and maintenance suppression (8 tests). No tests for flap behavior, dependency suppression, rollup correctness, or batch-writer overflow.

---

## Summary of concrete defects found (with live proof)

1. **duration_sec always 0** → all downtime figures in reports/exports/UI are zeros. (`engine.go:445`; live: 190/190 rows zero.)
2. **dashboard uptime-stats rollup queries reference non-existent `is_up`** → silent fallback to 30-day raw for windows up to 1 year, mislabeled. (Live: UNKNOWN_IDENTIFIER + API returns `hours: 8760`.)
3. **No degraded hysteresis + cooldown never enforced** → 73 flips and 76 alerts in 24 h from one device on a 27-device lab.
4. **Rollup tables contain partial aggregates** (11 rows/bucket; 1:1 with raw) and **avg_rtt includes zeros from down samples**; 1h MV is unweighted.
5. **Device maintenance status is cosmetic** — poller overwrites it and alerts on the transition; maintenance never excluded from availability.
6. **Dependency suppression unused (0 rows) and race-prone**; first-wave alerts escape.
7. **Unauthenticated alert-engine endpoints on 0.0.0.0:8000** — LAN-spoofable alerts/recoveries.
8. **Silent data drop paths**: ClickHouse buffer overflow drops ping results; alert HTTP failures drop alerts; no stale-poller detection for devices.
9. **Reports read raw-only (30-day TTL) and aggregate in Python** — wrong beyond 30 days, heavy at scale.
10. Minor: IPv6 stored as 0.0.0.0 in ping_metrics; ping tunables not configurable; two ICMP paths use different socket modes (`POLLER_PRIVILEGED` unset vs hardcoded privileged in `checker/icmp.go:60`).
