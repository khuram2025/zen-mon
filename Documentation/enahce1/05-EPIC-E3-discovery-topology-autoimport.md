# E3 — Discovery → Auto-Topology Map + Auto-Import (End-to-End)

## 1. Goal & competitive rationale
Auvik, Domotz and Meraki win deals on "plug in, see your network mapped in under an hour." ZenPlus has the *pieces* — an ICMP sweep, a profile classifier, a discovery staging schema — but they are disconnected: live Automated Maps shows **35 nodes / 0 observed links / 35 unmapped** because nothing ever crawls LLDP/CDP. This epic wires scan → SNMP/LLDP/CDP neighbor crawl → topology links → profile-driven auto-import → dependency graph, delivering true zero-touch onboarding and feeding root-cause suppression (E1).

## 2. Scope
### In scope
- Scheduled + on-demand discovery jobs that go beyond ICMP into SNMP `sysObjectID`/`sysDescr` fingerprinting.
- LLDP (`lldpRemTable`) and CDP (`cdpCacheTable`) neighbor collection in the Go poller, plus L3 hints (ipNetToMedia/ARP, ipRouteNextHop).
- Persisting **observed links** into a `topology_links` table; resolving "unmapped neighbors" (chassis-ID/port-ID match against known/discovered devices).
- Profile-rule classification + auto-import policy (auto / queue / ignore) per subnet, with credential reuse.
- A dependency graph (`device_dependencies`) derived from topology, consumed by E1 suppression.
- Wiring `discovery.py` and `DiscoveryPage.tsx` to the real job/result/credential model and an Automated Maps render.

### Out of scope
- NetFlow/sFlow-derived traffic adjacency (separate epic).
- Cloud/SaaS topology, BGP/OSPF adjacency parsing, L2 spanning-tree reconstruction.
- Manual map drawing/whiteboard editing UX (only auto-layout + pin/hide).

## 3. Current state in ZenPlus
**Exists & verified:**
- `server/app/api/v1/discovery.py` — ICMP-only sweep (`scan_subnet`, `ping_host`, `/discovery/scan`). No jobs, no SNMP, no import. The richer model the UI expects is **not** here.
- `scripts/migrate-005-snmp-discovery.sql` — `discovery_jobs`, `discovery_results` (with `matched_profile_id`, `already_known`, `imported`, `imported_device_id`), `snmp_mibs`. Schema is ready; **no API/poller writes to it.**
- `dashboard/src/pages/DiscoveryPage.tsx` (775 lines) — already typed for `DiscoveryResult` (`job_id`, `snmp_responded`, `matched_*`, `imported`) and `Credential`; it is **ahead** of the backend serving `/discovery`.
- Poller classifier: `poller/internal/checker/snmp/profile.go` (`Classifier`, `MatchRules`, longest-prefix `sysObjectID` match) and `engine.go:handleSNMPResult` (`Match`/`Extract`/`UpsertSystemInfo`, `AssignProfileIfUnset`).
- SNMP collector: `collector.go` (`collectSystem`, `collectInterfaces`, `collectEntities`) and `store/postgres.go` (`LoadSNMPDevices`, `LookupDeviceByIP`, `UpsertInterfaces`).
- `device_profiles`, `snmp_credentials`, `device_interfaces` models; `snmp_credentials.py` CRUD + assign.

**Missing:**
- LLDP/CDP OIDs in `oids.go` (only RFC1213/IF-MIB/HOST-RESOURCES/ENTITY/vendor CPU-mem). No `collectNeighbors`.
- `topology_links`, `device_dependencies` tables; no topology/dependency API or model (`grep` for `topology|device_link|dependenc` finds nothing in `server/app`).
- Discovery scheduler, SNMP-walk discovery worker, auto-import service.

## 4. Target design & architecture
```
 Scheduler (Postgres-backed) ─► discovery_jobs(pending)
        │ on-demand / cron
        ▼
 Poller DiscoveryWorker ── LoadDiscoveryJobs() ──► for each job:
   ICMP sweep ─► SNMP GET(sysObjectID,sysDescr,sysName)
              ─► Classifier.Match ─► discovery_results
              ─► collectNeighbors() {LLDP, CDP} ─► raw_neighbors
        ▼
 API auto-import policy ── per-profile/subnet ──► devices(+snmp_credential_id)
        ▼
 TopologyResolver: match chassisID/mgmtIP/portID ─► topology_links (resolved|unmapped)
        ▼
 DependencyBuilder: BFS from gateways ─► device_dependencies (parent→child)
        ▼
 GET /topology/graph ─► AutomatedMapsPage (nodes+links)   E1 suppression reads device_dependencies
```
Poller owns SNMP I/O (it already holds sessions via `SessionCache`); API owns policy, idempotent import, and graph serving. Neighbor resolution runs server-side so it can correlate across all jobs/known devices.

## 5. Data model & migrations
New `migrate-009-topology.sql` (Postgres, config-plane):
- `topology_links(id BIGSERIAL, src_device_id UUID, src_if_index INT, dst_device_id UUID NULL, dst_if_index INT NULL, protocol VARCHAR(8) /*lldp|cdp|arp|route*/, remote_chassis_id TEXT, remote_port_id TEXT, remote_sys_name TEXT, remote_mgmt_ip INET, state VARCHAR(12) /*resolved|unmapped*/, first_seen, last_seen, UNIQUE(src_device_id, src_if_index, remote_chassis_id, remote_port_id))`. Index `(state, last_seen)`, `(dst_device_id)`.
- `raw_neighbors(id, job_id UUID, src_ip INET, src_if_index, protocol, remote_chassis_id, remote_port_id, remote_sys_name, remote_mgmt_ip, raw JSONB, scanned_at)` — staging before resolution.
- `device_dependencies(parent_device_id, child_device_id, kind VARCHAR /*topology|gateway*/, confidence SMALLINT, derived_at, PRIMARY KEY(parent_device_id, child_device_id))` — consumed by E1.
- `discovery_schedules(id, cidrs TEXT[], cron TEXT, credential_id UUID, profile_policy JSONB /*{auto:[],queue:[],ignore:[]}*/, enabled BOOL, last_run_at, next_run_at)`.
- Extend `discovery_jobs`: add `schedule_id UUID NULL`, `phase VARCHAR /*ping|snmp|neighbors|resolve|done*/`.
Migration notes: all `CREATE … IF NOT EXISTS`, additive, plus `GRANT` block mirroring migrate-005's `zenplus` role pattern. No ClickHouse change (topology is config state, not time-series); optional `topology_link_events` in CH later for churn history.

## 6. API changes
- `POST /discovery/jobs` — create job(s) from `{cidrs[], credential_id, profile_policy}`; returns job ids. Replaces direct `/scan`.
- `GET /discovery/jobs?status=` / `GET /discovery/jobs/{id}` — progress (`phase`, scanned/responding counts).
- `GET /discovery/results?job_id=` — staging rows (already typed in `DiscoveryPage.tsx`).
- `POST /discovery/results/{id}/import` and `POST /discovery/import-batch {ids[]}` — idempotent create into `devices`, set `imported_device_id`, attach credential + matched profile.
- `POST /discovery/results/{id}/ignore` — mark ignored (suppress re-import).
- `POST/GET/PUT/DELETE /discovery/schedules` — CRUD for scheduled scans.
- `GET /topology/graph?root=&depth=` → `{nodes:[{device_id,label,type,status}], links:[{src,dst,protocol,state,src_if,dst_if}], unmapped:[…]}`.
- `POST /topology/resolve` — force re-resolution of `raw_neighbors`→`topology_links`+`device_dependencies`.
- `GET /topology/dependencies/{device_id}` — upstream/downstream (E1 consumes internally).

## 7. Poller / collector changes
- `oids.go`: add LLDP-MIB (`lldpRemChassisId 1.0.8802.1.1.2.1.4.1.1.5`, `lldpRemPortId .7`, `lldpRemSysName .9`, `lldpRemManAddrTable 1.0.8802.1.1.2.1.4.2`) and CISCO-CDP-MIB (`cdpCacheDeviceId 1.3.6.1.4.1.9.9.23.1.2.1.1.6`, `cdpCacheDevicePort .7`, `cdpCacheAddress .4`, `cdpCachePlatform .8`).
- New `poller/internal/checker/snmp/neighbors.go`: `collectNeighbors(ctx, client) ([]Neighbor, error)` — bulk-walk both tables, parse `lldpRemTimeMark.localPort.index` composite OID indices to map back to local `ifIndex`, normalize chassis-ID subtype/MAC. Add `Neighbors []Neighbor` to `Result`; call after `collectEntities` in `Collect`.
- New `poller/internal/discovery/worker.go`: claims `discovery_jobs(pending)` via `FOR UPDATE SKIP LOCKED`, runs ICMP→SNMP fingerprint→`collectNeighbors`, writes `discovery_results` + `raw_neighbors`, updates `phase`. Reuses `Classifier`, `SessionCache`, `getScalar`.
- `store/postgres.go`: add `LoadPendingDiscoveryJobs`, `WriteDiscoveryResult`, `WriteRawNeighbors`, `UpsertTopologyLink`. Hook a `discoveryTicker` in `engine.go` alongside `runSNMPCycle`.
- Library: existing `github.com/gosnmp/gosnmp` (`BulkWalkAll`) — no new deps.

## 8. Dashboard changes
- Rewire `DiscoveryPage.tsx` to `/discovery/jobs` + `/discovery/results` (types already present); add **Schedules** tab and per-subnet profile-policy editor (auto/queue/ignore).
- Wire credential picker to `/snmp/credentials`; add reveal/select (Key/Eye icons already imported).
- New `dashboard/src/pages/AutomatedMapsPage.tsx` + route `/maps`: force-directed graph (e.g. `react-force-graph` or existing canvas) reading `/topology/graph`; node coloring by `status`, link styling by `protocol`, an **Unmapped** drawer listing unresolved neighbors with "create device / ignore."
- Restore the CDP/LLDP card removed in `DeviceDetailPage.tsx:1070` using `/topology/dependencies/{id}`.

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|------|------|---------|-----------|
| 1 | `migrate-009-topology.sql` (links, raw_neighbors, dependencies, schedules) + GRANTs | db | 1 | — |
| 2 | SQLAlchemy models + Pydantic schemas for topology/dependencies/schedules | api | 1 | 1 |
| 3 | LLDP/CDP OIDs in `oids.go` | poller | 0.5 | — |
| 4 | `neighbors.go` collector + `Result.Neighbors` + wire into `Collect` | poller | 2 | 3 |
| 5 | `discovery/worker.go` (claim jobs, SNMP fingerprint, write results+raw_neighbors) | poller | 2.5 | 4,2 |
| 6 | Store methods + `discoveryTicker` in `engine.go` | poller | 1 | 5 |
| 7 | Replace `/discovery/scan` with jobs/results/schedules API | api | 2 | 2 |
| 8 | Auto-import service (idempotent device create, policy eval, credential attach) | api | 2 | 7 |
| 9 | `TopologyResolver` (raw_neighbors → topology_links, chassis/mgmtIP match) | api | 2 | 6 |
| 10 | `DependencyBuilder` (BFS gateways → device_dependencies) | api | 1.5 | 9 |
| 11 | `/topology/graph` + `/topology/dependencies` endpoints | api | 1 | 9,10 |
| 12 | Discovery schedule executor (cron → create jobs) | api | 1 | 7 |
| 13 | Rewire `DiscoveryPage.tsx` to jobs/results + schedules tab + policy editor | ui | 2.5 | 7,8 |
| 14 | `AutomatedMapsPage.tsx` + `/maps` route + unmapped drawer | ui | 3 | 11 |
| 15 | E1 suppression reads `device_dependencies` (integration hook) | api | 1 | 10 |
| 16 | Feature flag, seed LLDP/CDP profiles, docs | infra | 0.5 | — |
| 17 | E2E + perf harness (mock SNMP agents) | infra | 2 | 5,9 |

## 10. Acceptance criteria
- [ ] A scheduled scan of a /24 with valid SNMP completes and shows responding hosts with vendor/model in < 10 min.
- [ ] LLDP and CDP neighbors are collected; `topology_links` populated; **observed links > 0** on the live 35-node set.
- [ ] Auto-import policy creates devices only for `auto` profiles; others land in the import queue or ignored list; re-import is idempotent (no dup `ip_address`).
- [ ] Automated Maps renders nodes **and links**; "unmapped neighbors" count drops as devices import; drawer lets you import/ignore.
- [ ] `device_dependencies` is populated and E1 suppresses child alerts when a parent (gateway/uplink) is down.
- [ ] End-to-end "subnet → mapped" achievable in **< 1 hour** for a typical SMB site.

## 11. Test cases
| ID | Type | Precondition | Steps | Expected result |
|----|------|--------------|-------|-----------------|
| T1 | unit | LLDP fixture PDUs | Parse `lldpRemTable` composite index | Correct local `ifIndex` + chassis/port |
| T2 | unit | CDP fixture PDUs | Parse `cdpCacheTable` | Device-id/port/address extracted |
| T3 | unit | sysObjectID `1.3.6.1.4.1.9.x` | `Classifier.Match`/`Extract` | Cisco profile, vendor/model set |
| T4 | unit | Two raw_neighbors A↔B by chassisID | `TopologyResolver` | One `resolved` bidirectional link, no dup |
| T5 | unit | Neighbor mgmtIP unknown | Resolve | Link `state=unmapped`, listed in drawer |
| T6 | integration | Mock SNMP /29, 3 agents | Run job worker | results + raw_neighbors rows; phase=done |
| T7 | integration | Profile policy auto for Cisco | Import-batch | devices created, `imported_device_id` set |
| T8 | integration | Result already in `devices` | Import | `already_known=true`, no duplicate |
| T9 | integration | Resolved chain GW→SW→AP | DependencyBuilder | `device_dependencies` parent→child correct |
| T10 | e2e | UI scan a subnet | Create job → results → import → /maps | Nodes + links visible, unmapped decreases |
| T11 | e2e | Schedule cron every 1h | Wait/trigger | New job auto-created, runs |
| T12 | failure | SNMP timeout on host | Run job | `snmp_responded=false`, job not stuck, phase advances |
| T13 | failure | Bad credential | Run job | `error_message` set, isolated to host |
| T14 | rbac | Read-only user | POST /discovery/jobs, /import | 403; GET /topology/graph allowed |
| T15 | rbac | Tenant A user | GET tenant B topology | Empty/forbidden, no cross-tenant leak |
| T16 | perf | /20 (4094) sweep | Run discovery | Completes < 15 min, mem bounded, batched |
| T17 | perf | 500 devices, 50 links each | /topology/graph | Response < 1.5 s, paginated/depth-limited |
| T18 | security | Credential reuse on import | Inspect stored device | Passphrases encrypted (LargeBinary), not plaintext in results |
| T19 | regression | Existing ICMP `/discovery/scan` callers | Hit legacy path | Still works or 308 to jobs; no break |
| T20 | regression | SNMP metric poll | Run normal cycle | CPU/mem/interfaces unchanged by neighbor collector |

## 12. Risks & rollout
- **Feature flags:** `discovery_v2`, `topology_maps`, `auto_import` (default off). E1 dependency consumption behind `suppression_uses_topology`.
- **Migration/back-compat:** migrate-009 is additive; keep `/discovery/scan` returning 308→`/discovery/jobs` for one release so existing `Discovery.tsx` callers don't break.
- **Perf:** neighbor walks add 2 bulk-walks per device — gate behind per-device `topology_enabled`, reuse `SessionCache`, cap concurrency via existing SNMP semaphore; large CIDRs stay batched (current `batch_size=50`).
- **Security:** import must encrypt SNMP passphrases via existing `core/crypto.py`; never persist community strings in `discovery_results`; RBAC on import/ignore; tenant scoping on `/topology/graph`.
- **Auto-import safety:** default policy = **queue, not auto**; require explicit per-profile opt-in to avoid surprise device explosions and licensing spikes.
- **Phased rollout:** (1) poller neighbor collection + raw_neighbors (observe-only); (2) resolver + read-only maps; (3) auto-import policy; (4) E1 suppression wiring. Validate "observed links > 0" on the existing 35-node deployment as the phase-2 exit gate.
