# ZenPlus — Advanced SNMP Monitoring
## Product Design & Implementation Plan

**Author:** Product Design review
**Target system:** ZenPlus 1.1.3 (http://10.12.50.81/devices, local install on `zenplus`)
**Status:** Draft for review
**Date:** 2026-04-14

---

## 1. Executive Summary

ZenPlus today monitors network devices with **ICMP ping only** — up/down, RTT,
packet loss, jitter. That tells us *a host is reachable*, not *a switch is
dropping packets on Gi0/3*, *a firewall is at 92% CPU*, or *a UPS is on battery*.
This plan upgrades ZenPlus into a real multi-vendor network & security
monitoring platform by adding **first-class SNMP v1/v2c/v3 monitoring** on top
of the existing poller, schema, and ClickHouse time-series store.

The good news: the system is already architected for this. The `devices` table
has stubbed `snmp_*` columns, the Go poller already has a pluggable `checker`
package (HTTP/TCP/TLS), and ClickHouse metric rollups are in place. We are
**extending**, not rebuilding.

**Headline capabilities after this project:**

- SNMP v1, v2c, and **v3 with auth + priv** (SHA/AES), credential profiles.
- Auto-discovery of devices by sysObjectID → vendor/model classification.
- Standard MIB polling: IF-MIB, HOST-RESOURCES-MIB, ENTITY-MIB, ENTITY-SENSOR-MIB, UCD-SNMP-MIB.
- Vendor packs: Cisco, Juniper, Fortinet, Palo Alto, Check Point, Sophos, MikroTik, HPE/Aruba, Huawei, Dell, Ubiquiti, F5, APC, Eaton.
- Security-device specifics: firewall session count, VPN tunnel state, HA/cluster state, IPS signature counts.
- Interface-level metrics: bandwidth in/out, errors, discards, admin/oper state, duplex, CRCs.
- Environmental: CPU, memory, temperature, fans, PSUs, disk.
- **SNMP traps** (async event ingestion) alongside polled metrics.
- Per-metric thresholds, baselines, and alerts wired through the existing alert engine.
- Per-interface dashboards, topology-aware views, and scheduled reports.

---

## 2. Current State Assessment

### 2.1 Stack (verified)

| Layer | Technology | Path |
|---|---|---|
| API | Python 3 / FastAPI / async SQLAlchemy | `/opt/zenplus/server` |
| Poller | Go (prometheus-community/pro-bing) | `/opt/zenplus/poller` |
| Dashboard | React + Vite SPA | `/opt/zenplus/dashboard/dist` |
| Config DB | PostgreSQL 16 | systemd `postgresql@16-main` |
| Time-series | ClickHouse (dockerized) | `scripts/init-clickhouse.sql` |
| Bus / realtime | Redis (pub/sub → WebSocket) | systemd `redis-server` |
| Edge | nginx reverse proxy | systemd `nginx` |

### 2.2 Device model — already SNMP-aware (partially)

File: `/opt/zenplus/server/app/models/device.py`

Existing columns:

- `snmp_enabled` (bool)
- `snmp_community` (string)
- `snmp_version` (enum: "1" | "2c" | "3", default "2c")
- `snmp_port` (int, default 161)

Missing for v3 and advanced use:

- `snmp_v3_username`, `snmp_v3_context`
- `snmp_auth_protocol` (MD5/SHA/SHA-256/SHA-512)
- `snmp_auth_passphrase` (encrypted)
- `snmp_priv_protocol` (DES/AES-128/AES-192/AES-256)
- `snmp_priv_passphrase` (encrypted)
- `snmp_timeout_ms`, `snmp_retries`, `snmp_max_repetitions`
- `sys_object_id`, `vendor`, `model`, `os_version` (auto-populated from discovery)
- `profile_id` (FK → device monitoring profile)

### 2.3 Poller

File: `/opt/zenplus/poller/internal/pinger/engine.go`

- Reloads devices from Postgres every 60 s.
- Runs ping cycle every 60 s, up to 100 concurrent ICMP checks.
- Writes metrics to ClickHouse and publishes to Redis; posts to
  `/api/v1/alert-engine/evaluate` on status transitions.
- A parallel `checker` package handles HTTP/TCP/TLS.

**SNMP fits here as a peer checker**: `poller/internal/checker/snmp.go`.

### 2.4 Alerting

- `AlertRule.metric` is an enum: `ping_status | rtt | packet_loss | jitter`.
- Must grow to include SNMP metric keys
  (`cpu | memory | if_in_bps | if_out_bps | if_errors | temperature | …`).

### 2.5 Devices page — today

Columns: hostname, ip_address, device_type, location, status, last_seen, last_rtt_ms.
No SNMP fields surfaced, no interface list, no hardware details.

---

## 3. Goals & Non-Goals

### 3.1 Goals

1. Monitor **any** SNMP-capable device — network, security, server, UPS, printer, IoT.
2. Match or beat the monitoring depth of LibreNMS / Observium / PRTG for the
   top 10 vendors used in Pakistani enterprise networks.
3. Zero-config discovery: give an IP + credentials → device is classified,
   MIBs loaded, interfaces enumerated, dashboards appear automatically.
4. Secure by default: SNMPv3 preferred, credentials encrypted at rest, audit-logged.
5. Keep the existing ping poller lightweight — SNMP must not degrade ping accuracy.

### 3.2 Non-goals (this phase)

- Full NetFlow / sFlow ingestion.
- Active configuration management (NETCONF / SSH config push).
- Packet capture or deep packet inspection.
- Syslog ingestion (future phase 4).

### 3.3 Success metrics

- 95 % of added devices auto-classified correctly on first poll.
- SNMP polling cycle completes within its interval for **≥ 5,000 devices**
  and **≥ 100,000 OIDs per cycle** on a single poller.
- p95 poll latency per device < 2 s for 60 s cycles.
- Mean time to detect an interface-down event ≤ 30 s (trap) / ≤ 90 s (poll).
- Zero plaintext SNMP credentials in database dumps.

---

## 4. SNMP Research Summary (what "advanced" means)

### 4.1 Protocol matrix

| Version | Auth | Encryption | Use case |
|---|---|---|---|
| v1 | community (plaintext) | none | legacy gear only |
| v2c | community (plaintext) | none | default for LAN-internal monitoring |
| v3 | user + MD5/SHA/SHA-256/SHA-512 | DES/AES-128/192/256 | preferred — WAN, security zones, compliance |

### 4.2 Library choice (Go poller)

- **`gosnmp/gosnmp`** — canonical Go SNMP lib, supports v1/v2c/v3, GetBulk,
  BulkWalk, traps. Active, MIT. **Chosen.**
- Runner-up: `soniah/gosnmp` (same lineage, merged).
- Rejected: shelling out to `snmpwalk` — slow, hard to parallelize, no v3 trap support.

### 4.3 MIB strategy

We do **not** need a full MIB compiler for phase 1. We carry a curated set of
OIDs per profile (numeric OIDs, human labels baked in). In phase 3 we add a
MIB compiler (`gosmi`) so users can upload vendor MIBs.

**Phase 1 baseline OIDs** (standard, work on 90 % of gear):

- `1.3.6.1.2.1.1` — system group (sysDescr, sysObjectID, sysUpTime, sysName, sysLocation)
- `1.3.6.1.2.1.2` / `1.3.6.1.2.1.31` — IF-MIB / IF-MIB-extensions (64-bit counters)
- `1.3.6.1.2.1.25` — HOST-RESOURCES-MIB (CPU, memory, storage, processes)
- `1.3.6.1.2.1.47` — ENTITY-MIB (chassis inventory)
- `1.3.6.1.2.1.99` — ENTITY-SENSOR-MIB (temperature, fans, PSUs, voltage)
- `1.3.6.1.4.1.2021` — UCD-SNMP-MIB (Linux CPU load, memory, disk for net-snmp hosts)

### 4.4 Vendor packs (phase 2)

Each pack = a JSON/YAML profile with OIDs, metric names, units, and graph
definitions. Packs ship with ZenPlus and are version-controlled.

| Vendor | Priority | Key MIBs |
|---|---|---|
| Cisco IOS / IOS-XE / NX-OS | P0 | CISCO-PROCESS-MIB, CISCO-MEMORY-POOL-MIB, CISCO-ENVMON-MIB, CISCO-IPSEC-FLOW-MONITOR-MIB |
| Fortinet FortiGate | P0 | FORTINET-FORTIGATE-MIB (sessions, CPU, VPN tunnels, HA) |
| Palo Alto PAN-OS | P0 | PAN-COMMON-MIB (sessions, GlobalProtect) |
| MikroTik RouterOS | P0 | MIKROTIK-MIB |
| HPE / Aruba switches | P1 | ARUBAWIRED-*, HP-ICF-* |
| Juniper Junos | P1 | JUNIPER-MIB, JUNIPER-IF-MIB |
| Sophos XG | P1 | SFOS-FIREWALL-MIB |
| Check Point | P1 | CHECKPOINT-MIB |
| Huawei | P1 | HUAWEI-MIB |
| Dell iDRAC / OS10 | P2 | IDRAC-MIB-SMIv2, DELL-NETWORKING-* |
| F5 BIG-IP | P2 | F5-BIGIP-SYSTEM-MIB |
| APC / Eaton UPS | P2 | PowerNet-MIB, XUPS-MIB |
| Ubiquiti UniFi / EdgeOS | P2 | UBNT-MIB |
| Printers | P3 | Printer-MIB (RFC 3805) |

### 4.5 Trap handling

- Trap receiver runs inside the Go poller (gosnmp TrapListener) on UDP/162.
- Traps are matched to `devices.ip_address`, enriched with the resolved
  trap-OID label, and forwarded to the alert engine.
- Trap storage: new ClickHouse table `snmp_traps` (30-day TTL).

---

## 5. Target Architecture

```
                   ┌──────────────────────┐
  nginx  ────────▶ │ FastAPI (server)     │ ──▶ Postgres (devices, profiles,
                   │  + SNMP config API   │      credentials, alert_rules)
                   │  + MIB repo API      │
                   └──────────┬───────────┘
                              │  Redis pub/sub (device config changes)
                              ▼
                   ┌──────────────────────┐      ┌────────────────────┐
                   │ Go poller            │ ───▶ │ ClickHouse          │
                   │  ├─ pinger           │      │  ping_metrics       │
                   │  ├─ checker (HTTP…)  │      │  service_metrics    │
                   │  ├─ snmp_poller  ◀── │      │  snmp_metrics       │
                   │  │    (gosnmp)       │      │  snmp_if_metrics    │
                   │  └─ trap_listener ◀─ │      │  snmp_traps         │
                   └──────────┬───────────┘      └────────────────────┘
                              │
                              ▼
                   /api/v1/alert-engine/evaluate  ──▶ notifications
```

### 5.1 Poller worker pools

Three independent pools inside the poller process:

1. **Ping pool** — unchanged (ICMP, 100 workers).
2. **SNMP poll pool** — new, 200 workers default, configurable. Uses BulkWalk
   for tables (IF-MIB), Get for scalars. Per-device session is cached and
   reused between cycles.
3. **Trap listener** — single goroutine on UDP/162, dispatches to a small
   decode pool.

All three share the same `MetricWriter` batched into ClickHouse.

### 5.2 Scheduler

- Each device has an effective `snmp_poll_interval` (default 60 s, min 30 s, max 1 h).
- Heavy profiles (full interface table on a 48-port switch) can be spaced on
  a 120 s or 300 s cycle independently of scalars.
- Scheduler uses a min-heap keyed by `next_poll_at`.

### 5.3 Security

- Credential storage: per-device v3 passphrases encrypted with
  **libsodium secretbox** using a server-managed key loaded from
  `/opt/zenplus/.env` (out of DB). Key is 32 bytes, rotated via
  `scripts/rotate-snmp-key.sh`.
- API never returns passphrases on GET; write-only fields.
- Audit log entry on every credential create / update / delete.
- SNMPv3 is recommended in the UI; v1/v2c shows a warning banner.

---

## 6. Data Model Changes

### 6.1 PostgreSQL — new / altered tables

```sql
-- extend devices
ALTER TABLE devices
  ADD COLUMN snmp_v3_username       text,
  ADD COLUMN snmp_v3_context        text,
  ADD COLUMN snmp_auth_protocol     text,       -- SHA | SHA256 | SHA512 | MD5
  ADD COLUMN snmp_auth_passphrase   bytea,      -- encrypted
  ADD COLUMN snmp_priv_protocol     text,       -- AES128 | AES192 | AES256 | DES
  ADD COLUMN snmp_priv_passphrase   bytea,      -- encrypted
  ADD COLUMN snmp_timeout_ms        int    DEFAULT 2000,
  ADD COLUMN snmp_retries           int    DEFAULT 2,
  ADD COLUMN snmp_max_repetitions   int    DEFAULT 25,
  ADD COLUMN snmp_poll_interval     int    DEFAULT 60,
  ADD COLUMN sys_object_id          text,
  ADD COLUMN vendor                 text,
  ADD COLUMN model                  text,
  ADD COLUMN os_version             text,
  ADD COLUMN profile_id             uuid REFERENCES device_profiles(id);

-- monitoring profiles (vendor packs)
CREATE TABLE device_profiles (
  id              uuid PRIMARY KEY,
  name            text NOT NULL,          -- e.g. "Cisco IOS-XE"
  vendor          text,
  match_rules     jsonb,                  -- sysObjectID prefixes, sysDescr regex
  oid_groups      jsonb NOT NULL,         -- [{key, oid, type, unit, table?}, …]
  version         int NOT NULL,
  builtin         bool DEFAULT false,
  created_at      timestamptz DEFAULT now()
);

-- interfaces discovered per device (IF-MIB)
CREATE TABLE device_interfaces (
  id              bigserial PRIMARY KEY,
  device_id       uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  if_index        int NOT NULL,
  if_name         text,
  if_descr        text,
  if_alias        text,
  if_type         int,
  if_speed        bigint,
  mac_address     macaddr,
  admin_status    text,
  oper_status     text,
  monitored       bool DEFAULT true,
  first_seen      timestamptz,
  last_seen       timestamptz,
  UNIQUE (device_id, if_index)
);

-- hardware inventory (ENTITY-MIB)
CREATE TABLE device_entities (
  id              bigserial PRIMARY KEY,
  device_id       uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  ent_index       int NOT NULL,
  parent_index    int,
  class           text,         -- chassis | module | port | fan | sensor | psu
  name            text,
  serial_number   text,
  model_name      text,
  hw_revision     text,
  fw_revision     text,
  UNIQUE (device_id, ent_index)
);

-- sensors (ENTITY-SENSOR-MIB)
CREATE TABLE device_sensors (
  id              bigserial PRIMARY KEY,
  device_id       uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  sensor_index    int NOT NULL,
  sensor_type     text,         -- celsius | fan | voltage | amperage | watts
  description     text,
  unit            text,
  monitored       bool DEFAULT true,
  UNIQUE (device_id, sensor_index)
);

-- audit
CREATE TABLE snmp_credential_audit (
  id          bigserial PRIMARY KEY,
  device_id   uuid NOT NULL,
  actor_id    uuid NOT NULL,
  action      text NOT NULL,  -- create | rotate | delete | test
  at          timestamptz DEFAULT now(),
  client_ip   inet
);
```

### 6.2 ClickHouse — new tables

```sql
CREATE TABLE snmp_metrics (
  device_id   UUID,
  metric_key  LowCardinality(String),   -- cpu | memory | uptime | temperature | …
  value       Float64,
  unit        LowCardinality(String),
  ts          DateTime64(3) DEFAULT now64(3)
) ENGINE = MergeTree
ORDER BY (device_id, metric_key, ts)
TTL toDateTime(ts) + INTERVAL 30 DAY;

CREATE TABLE snmp_if_metrics (
  device_id     UUID,
  if_index      UInt32,
  in_octets     UInt64,
  out_octets    UInt64,
  in_errors     UInt64,
  out_errors    UInt64,
  in_discards   UInt64,
  out_discards  UInt64,
  in_ucast_pkts UInt64,
  out_ucast_pkts UInt64,
  oper_status   UInt8,
  ts            DateTime64(3) DEFAULT now64(3)
) ENGINE = MergeTree
ORDER BY (device_id, if_index, ts)
TTL toDateTime(ts) + INTERVAL 30 DAY;

CREATE TABLE snmp_traps (
  device_id   UUID,
  source_ip   IPv4,
  trap_oid    String,
  bindings    String,   -- JSON
  severity    LowCardinality(String),
  ts          DateTime64(3) DEFAULT now64(3)
) ENGINE = MergeTree
ORDER BY (device_id, ts)
TTL toDateTime(ts) + INTERVAL 30 DAY;
```

5-minute and 1-hour rollup materialized views mirror the ping rollups.

---

## 7. UX / Dashboard Design

### 7.1 Devices page — redesigned

Current columns are kept. New additions:

- **Protocol** badge column: `PING`, `PING+SNMP`, `SNMP` — colored.
- **Vendor / Model** column (auto from discovery).
- **Health** column: composite of reachability, CPU, memory, interface errors.
  Single traffic-light with tooltip breakdown.
- **Interfaces** mini-sparkline: last 1 h utilization of the busiest interface.
- Row expand → quick panel with CPU, memory, top 3 interfaces.

### 7.2 Device detail page — new tabs

1. **Overview** — system info, uptime, vendor, model, OS, last poll, health
   score, reachability SLA for the last 30 days.
2. **Interfaces** — paginated table: index, name, alias, speed, admin/oper,
   in/out bps sparkline, errors, discards. Per-row click → interface detail
   with 24h/7d/30d graphs.
3. **Environment** — CPU, memory, disk, temperature, fans, PSUs as gauges + graphs.
4. **Inventory** — ENTITY-MIB tree (chassis → modules → ports) with serials.
5. **Security** (conditional, shown when profile = firewall/UTM) — session
   count, VPN tunnels, HA cluster state, IPS counters.
6. **Traps** — live stream of recent traps for this device.
7. **Alerts** — active and historical alerts, threshold editor.
8. **Settings** — SNMP credentials (write-only), profile, poll interval, per-interface monitoring toggles.

### 7.3 New screens

- **Discovery wizard** — enter CIDR range + credential profile → live progress
  bar as devices are discovered → confirm import.
- **Credential profiles** — manage reusable v2c/v3 profiles so operators don't
  re-enter passphrases per device.
- **MIB library** — list of installed vendor packs, version, upload custom MIB.
- **Global SNMP dashboard** — top N interfaces by utilization, top N devices
  by CPU, top N by temperature, trap firehose.
- **Topology map** (phase 3) — auto-built from LLDP-MIB / CDP-MIB.

### 7.4 Design principles

- **Progressive disclosure**: device list stays calm; depth lives in detail tabs.
- **One page, one question**: each tab answers a single operator question.
- **Time is a first-class dimension**: every graph has 1 h / 24 h / 7 d / 30 d.
- **Dark-mode first** (NOC screens run dark).
- **Keyboardable**: `/` for search, `g d` for devices, `g a` for alerts.

---

## 8. Phased Implementation — Checklist

Estimates are rough calendar weeks assuming one full-time engineer plus
review. Ticked items → shippable checkpoint.

### Phase 0 — Foundations (1 week)

- [ ] Create git branch `feature/snmp-monitoring`.
- [ ] Add `gosnmp/gosnmp` dependency to `poller/go.mod`.
- [ ] Add `pysnmp` (optional, for server-side test tooling only) to `server/requirements.txt`.
- [ ] Draft + merge Alembic migration for `devices` extra columns.
- [ ] Draft + merge Alembic migration for `device_profiles`, `device_interfaces`, `device_entities`, `device_sensors`, `snmp_credential_audit`.
- [ ] Draft + merge ClickHouse migration adding `snmp_metrics`, `snmp_if_metrics`, `snmp_traps` and their rollup MVs.
- [ ] Add `SNMP_ENC_KEY` to `.env.example` and document rotation.
- [ ] Write secretbox encrypt/decrypt helper in `server/app/core/crypto.py`.
- [ ] Update `server/app/models/device.py` to reflect new columns.
- [ ] Update `server/app/schemas/device.py` — `DeviceCreate`, `DeviceUpdate`, `DeviceResponse` with SNMP fields (passphrases write-only).

### Phase 1 — Core SNMP polling (3 weeks)

- [ ] Create `poller/internal/checker/snmp/` package.
- [ ] Implement session manager with per-device cached `*gosnmp.GoSNMP`.
- [ ] Implement `Collector` interface: `CollectSystem`, `CollectHostResources`, `CollectInterfaces`, `CollectSensors`, `CollectEntities`.
- [ ] Implement BulkWalk helper with retry + exponential backoff.
- [ ] Map raw OID values → normalized metric keys (`cpu`, `memory_used_pct`, `if_in_bps`, …).
- [ ] Extend `poller/internal/store/postgres.go` `LoadDevices()` to return SNMP config.
- [ ] Add SNMP worker pool to `poller/internal/pinger/engine.go` (separate from ping pool, shared MetricWriter).
- [ ] Add scheduler (min-heap by `next_poll_at`).
- [ ] Write interfaces & sensors & entities back to Postgres on first poll (discovery).
- [ ] Bandwidth math: convert counter deltas to bps, handle 32- vs 64-bit counter wraps.
- [ ] Extend `MetricWriter` with `WriteSNMPMetric` and `WriteSNMPIfMetric`.
- [ ] Publish interface state transitions to Redis for real-time UI.
- [ ] Extend alert engine metric enum: `cpu`, `memory`, `if_in_bps`, `if_out_bps`, `if_errors`, `if_oper_status`, `temperature`, `fan_state`, `psu_state`, `uptime_reset`.
- [ ] Unit tests for each Collector with fixture responses.
- [ ] Integration test against `snmpsim` docker image with Cisco + Linux walks.

### Phase 2 — Vendor packs & classification (2 weeks)

- [ ] Define `device_profile` JSON schema and validator.
- [ ] Ship built-in profiles: Generic-v2c, Linux-net-snmp, Cisco-IOS, Cisco-NXOS, Juniper-Junos, MikroTik-RouterOS, HPE-ProCurve, Aruba-OS, Fortinet-FortiGate, Palo-Alto-PAN-OS, Sophos-XG, CheckPoint, Huawei-VRP, Dell-OS10, F5-BIGIP, APC-UPS, Printer-RFC3805.
- [ ] Write classifier: sysObjectID prefix match → sysDescr regex fallback → profile.
- [ ] Auto-assign profile on first successful poll.
- [ ] Add profile override UI.
- [ ] Unit tests: one walk fixture per vendor → expected profile.

### Phase 3 — Discovery, traps, MIB upload (2 weeks)

- [ ] Implement CIDR range discovery endpoint `POST /api/v1/snmp/discover`.
- [ ] Background task: ping sweep → SNMP Get sysObjectID → classify → stage.
- [ ] WebSocket progress channel `ws://…/snmp/discover/{job_id}`.
- [ ] Staging review UI (confirm / deselect before import).
- [ ] Implement Go trap listener on UDP/162 with v2c + v3 auth/priv.
- [ ] Trap → device lookup → enrichment → ClickHouse `snmp_traps` + alert engine.
- [ ] Trap viewer page with live tail.
- [ ] Add `gosmi`-based MIB loader (optional at runtime).
- [ ] MIB upload endpoint + storage in `/opt/zenplus/data/mibs`.
- [ ] Signed vendor pack distribution (so updates ship with the updater service).

### Phase 4 — Dashboard & UX (3 weeks)

- [ ] Devices page: add Protocol, Vendor/Model, Health, Interface sparkline columns.
- [ ] Device detail shell with tabs: Overview, Interfaces, Environment, Inventory, Security, Traps, Alerts, Settings.
- [ ] Overview tab widgets and SLA calculation.
- [ ] Interfaces tab with virtualized table, per-row 1h sparkline.
- [ ] Interface detail graphs (bps, pps, errors, discards, utilization % of speed).
- [ ] Environment tab gauges + graphs.
- [ ] Inventory tab tree view.
- [ ] Security tab (vendor-conditional panels).
- [ ] Traps tab live tail via WebSocket.
- [ ] Settings tab — write-only credential fields, per-interface monitoring toggles.
- [ ] Discovery wizard end-to-end.
- [ ] Credential profiles CRUD page.
- [ ] MIB library page.
- [ ] Global SNMP dashboard (top N widgets).
- [ ] Dark-mode polish + keyboard shortcuts.
- [ ] Accessibility pass (WCAG 2.1 AA on new screens).

### Phase 5 — Alerting, thresholds, reports (1 week)

- [ ] Threshold editor UI for SNMP metrics (warning / critical, sustained duration, recovery).
- [ ] Per-interface threshold inheritance from device defaults.
- [ ] Baseline learning: 7-day rolling p95 of each metric, anomaly flag.
- [ ] Scheduled reports: PDF export of device health, interface utilization, SLA.
- [ ] Email / Slack / webhook channels — reuse existing notification backend.

### Phase 6 — Hardening, scale, docs (2 weeks)

- [ ] Load test with `snmpsim` cluster: 5,000 simulated devices, 100k OIDs per minute.
- [ ] Profile poller heap / goroutines; fix hot paths.
- [ ] Rate limit per poller → device (max N SNMP packets/s).
- [ ] Graceful degradation: if a device times out 3 cycles → exponential backoff up to 15 min.
- [ ] Credential rotation runbook.
- [ ] Backup & restore verification with new tables.
- [ ] Operator docs under `/opt/zenplus/dashboard/help/snmp/`.
- [ ] Release notes, version bump to **1.2.0**, tag, build installer.

---

## 9. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| SNMP poll storms saturate slow WAN links | High | Per-device rate limiter, GetBulk with max-repetitions tuned per profile, jittered scheduler. |
| Vendor MIBs are locked / quirky | Medium | Start with standard MIBs (IF, HOST-RESOURCES, ENTITY); vendor packs are additive. |
| Counter wraps on 1 Gb+ 32-bit counters | Medium | Prefer 64-bit HC counters (`ifHC*`); detect wrap and discard bad samples. |
| v3 credential leakage | High | Encrypt at rest, write-only API, audit log, never log passphrases, key rotation script. |
| Trap floods DoS the poller | Medium | Trap decode pool with bounded queue, per-source rate limit, drop + counter metric. |
| Too many interfaces per device blow up the DB | Medium | `monitored` flag per interface; default: only monitor interfaces with `ifAdminStatus=up` and nonzero speed. |
| Existing ping monitoring regresses | High | SNMP runs in a **separate** worker pool with its own queue; ping pool untouched. |
| Clickhouse write amplification | Medium | Batch writes, coalesce per cycle, test rollup MV performance. |

---

## 10. Testing Strategy

- **Unit**: each Collector against captured walk files (`testdata/walks/*.snmpwalk`).
- **Integration**: `snmpsim` Docker with 6 device personas (Cisco, Juniper, Fortinet, MikroTik, Linux, APC UPS) in CI.
- **End-to-end**: Playwright flows — add device → wait for discovery → assert interfaces visible → set threshold → induce fault in snmpsim → assert alert fires.
- **Load**: k6 + snmpsim farm; assert poll cycle ≤ interval at 5,000 devices.
- **Security**: credential round-trip encryption test; API tests confirming passphrases never returned; audit log coverage.
- **Upgrade**: take a 1.1.3 backup → apply migrations → verify no data loss and ping continues working throughout.

---

## 11. Rollout Plan

1. Internal dogfooding on `zenplus` with 10 real devices for 1 week.
2. Beta flag `features.snmp = true` in `.env`, off by default in 1.2.0-rc1.
3. Guided migration screen on first login of 1.2.0: "Enable SNMP for these
   devices?" with bulk credential entry.
4. GA in 1.2.0 with SNMP enabled by default for new installs, opt-in upgrade
   for existing ones.
5. Telemetry (opt-in) on poll cycle duration, device count, error rates to
   catch field issues early.

---

## 12. Open Questions

- [ ] Do we want to bundle a **read-only MIB browser** in the UI? (Nice-to-have, phase 3+.)
- [ ] SNMP over TLS (RFC 6353) — demand in target market? (Defer unless requested.)
- [ ] Multi-poller / distributed polling across sites — phase 7?
- [ ] Should alerts from SNMP traps deduplicate with polled-state alerts? (Proposed: yes, via alert correlation key = `device_id + metric_key`.)

---

## 13. File Touch List (starter map for engineering)

Server (Python / FastAPI):

- `server/app/models/device.py` — new columns
- `server/app/models/device_profile.py` — **new**
- `server/app/models/device_interface.py` — **new**
- `server/app/models/device_entity.py` — **new**
- `server/app/models/device_sensor.py` — **new**
- `server/app/schemas/device.py` — SNMP fields
- `server/app/schemas/snmp.py` — **new** (credential profile, discovery job, trap)
- `server/app/api/v1/devices.py` — expose SNMP fields
- `server/app/api/v1/snmp.py` — **new** (discover, test, credential profiles, MIB upload)
- `server/app/api/v1/alert_rules.py` — metric enum
- `server/app/core/crypto.py` — **new** (secretbox helpers)
- `server/alembic/versions/*_snmp_*.py` — migrations

Poller (Go):

- `poller/go.mod` — add gosnmp
- `poller/internal/checker/snmp/collector.go` — **new**
- `poller/internal/checker/snmp/session.go` — **new**
- `poller/internal/checker/snmp/profiles.go` — **new** (profile loader)
- `poller/internal/checker/snmp/traps.go` — **new**
- `poller/internal/store/postgres.go` — extend `LoadDevices`, writeback interfaces/entities/sensors
- `poller/internal/store/clickhouse.go` — `WriteSNMPMetric`, `WriteSNMPIfMetric`, `WriteTrap`
- `poller/internal/pinger/engine.go` — wire SNMP pool (rename file or split into `engine_snmp.go`)
- `poller/cmd/zenplus-poller/main.go` — wire trap listener start/stop
- `poller/config.yaml` — snmp section (worker count, rate limits, trap bind)

Infra / scripts:

- `scripts/init-clickhouse.sql` — new tables + MVs
- `scripts/rotate-snmp-key.sh` — **new**
- `data/profiles/*.json` — **new** vendor packs
- `docker-compose.yml` — expose UDP/162 for traps

Dashboard (React):

- `dashboard/src/pages/Devices/` — new columns + row expand
- `dashboard/src/pages/DeviceDetail/` — **new** tabbed shell and all tabs
- `dashboard/src/pages/SNMP/Discovery.tsx` — **new**
- `dashboard/src/pages/SNMP/CredentialProfiles.tsx` — **new**
- `dashboard/src/pages/SNMP/MibLibrary.tsx` — **new**
- `dashboard/src/pages/SNMP/GlobalDashboard.tsx` — **new**
- `dashboard/src/api/snmp.ts` — **new**
- `dashboard/src/components/charts/InterfaceSparkline.tsx` — **new**

---

## 14. Summary

We are not building a new product. We are unlocking the platform ZenPlus was
already designed to become: a reliable, multi-vendor, multi-protocol
monitoring system that understands *what is happening inside a device*, not
just *whether it answers a ping*. The schema already whispers "SNMP"; this
plan is how we make it speak.

*— End of plan. Review, then start with Phase 0.*
