# SNMP Monitoring Engine Audit — ZenPlus

Date: 2026-06-10
Scope: `poller/internal/checker/snmp/`, `poller/internal/pinger/engine.go` (SNMP cycle), `poller/internal/store/{postgres,clickhouse}.go`, `server/app/api/v1/{snmp,snmp_credentials,devices,alert_engine,topology}.py`, `server/app/services/{metric_service,device_service}.py`, models, `scripts/migrate-004-snmp*.sql`, `dashboard/src/pages/{DeviceInterfacesPage,DeviceDetailPage,MibLibraryPage}.tsx`, live appliance at 192.168.8.152.

---

## 0. Live appliance state (read-only checks)

- **0 of 27 devices have SNMP enabled** — the engine is idle on this box:
  `sudo -u postgres psql -d zenplus -c "SELECT count(*) FILTER (WHERE snmp_enabled), count(*) FROM devices"` → `0|27`; `SELECT count(*) FROM device_interfaces` → `0`.
- **Profile packs are missing in production.** `journalctl -u zenplus-poller`:
  ```
  profile load: read profile dir /opt/zenplus/data/profiles: open /opt/zenplus/data/profiles: no such file or directory
  SNMP profile seed failed: no profiles loaded from /opt/zenplus/data/profiles
  ```
  `ls /opt/zenplus/data/` shows only `geoip/` and `netflow-ip-groups.json`. **No profile JSON files exist anywhere in the repo** (`git ls-files | grep -i profile` → only .go/.py/.tsx code). `device_profiles` table is **empty**. Consequence: device classification (vendor/model/OS extraction), `AssignProfileIfUnset`, the discovery sweep's `_classify_from_db` (server/app/api/v1/snmp.py:207-295) and the SnmpProfilesPage are all dead on a fresh appliance.
- Trap listener **is** bound: `ss -ulnp | grep :162` → `UNCONN ... *:162` (systemd drop-in grants `cap_net_bind_service`).
- `snmp_mibs` table: 0 rows.

---

## 1. What is actually polled today

One collector pass per device per cycle — `Collector.Collect` (poller/internal/checker/snmp/collector.go:47-142) runs, in order:

1. **System group** (RFC1213) — single GET of sysDescr/sysObjectID/sysUpTime/sysContact/sysName/sysLocation (collector.go:146-175, oids.go:6-11). sysUpTime emitted as scalar metric `uptime` (seconds).
2. **HOST-RESOURCES-MIB** — `hrProcessorLoad` walk averaged into one `cpu` %, `hrStorageTable` filtered to `hrStorageRam`, first RAM row → `memory_total_bytes`, `memory_used_bytes`, `memory` % (collector.go:177-245).
3. **IF-MIB ifTable + ifXTable** — full walks of ifDescr, ifType, ifSpeed, ifPhysAddress, ifAdminStatus, ifOperStatus, ifIn/OutOctets, ifIn/OutUcastPkts, ifIn/OutErrors, ifIn/OutDiscards, **plus 64-bit HC counters** ifHCIn/OutOctets, ifHCIn/OutUcastPkts, ifName, ifAlias, ifHighSpeed (collector.go:397-523, oids.go:13-38). ifHighSpeed (Mbps) preferred over ifSpeed when nonzero (collector.go:460-466). HC counters preferred when value > 0 (collector.go:477-499).
4. **ENTITY-MIB** inventory — entPhysicalDescr/Contained/Class/Name/HWRev/FWRev/Serial/Model → `device_entities` (collector.go:525-577). PSU/fan appear only as inventory *classes* (oids.go:113-126) — no operational status.
5. **ENTITY-SENSOR-MIB (RFC 3433)** — type/scale/precision/value/units → `device_sensors` + normalized scalar keys `temperature_<idx>`, `fan_<idx>`, `voltage_<idx>`, `amperage_<idx>`, `power_<idx>`, `humidity_<idx>` (collector.go:579-629, 834-850). Scale/precision math (applyScale, collector.go:817-832) is correct per RFC 3433.

**Vendor-specific OIDs** — fallback only when HOST-RESOURCES returned nothing (collector.go:237-243, 249-373), detected by sysObjectID prefix:

| Vendor | CPU | Memory | Extras |
|---|---|---|---|
| Cisco (1.3.6.1.4.1.9.) | cpmCPUTotal5minRev → 1minRev → OLD-CISCO avgBusy5 | CISCO-MEMORY-POOL used+free, **all pools summed** (comment says "processor pools" but no filter — collector.go:280-300) | — |
| Fortinet (12356.) | fgSysCpuUsage | fgSysMemUsage | fgSysSesCount → `sessions` |
| Palo Alto (25461.) | **wrong OIDs — see issue PAN-1** | **wrong OID** | — |
| Juniper (2636.) | jnxOperatingCPU avg across slots | jnxOperatingBuffer avg | — |
| Aruba (14823.) | wlsxSwitchTotalCpuUtilization | **none** (OIDArubaAPMem declared, never used) | — |

**Not covered at all:** F5, MikroTik, Arista, HPE ProCurve, Dell, Huawei, Check Point, Ubiquiti (they only get HOST-RESOURCES, which several of those don't implement usefully).

**Declared but dead OIDs** (oids.go, zero references outside the constants file — verified by grep): `OIDCiscoEnhMemUsed/Free` (Nexus/IOS-XE HC mempool), `OIDFortiSesRate`, `OIDPanGPCPU`, `OIDJnxTemp`, `OIDArubaAPMem`, `OIDEntPhySensorOperStatus`, `OIDEntPhysicalMfgName`, `OIDEntPhysicalSWRev`.

### Issue PAN-1 — Palo Alto CPU/memory OIDs are factually wrong
oids.go:90-91:
```go
OIDPanSysCPU = "1.3.6.1.4.1.25461.2.1.2.1.1.0" // panSysCPULinuxPercent
OIDPanSysMem = "1.3.6.1.4.1.25461.2.1.2.1.2.0" // panSysMemoryUtilization (KB total)
```
In PAN-COMMON-MIB, `25461.2.1.2.1.1` is **panSysSwVersion** (a version string like "11.0.2") and `25461.2.1.2.1.2` is **panSysHwVersion** — there is no "panSysCPULinuxPercent" object. `getScalar` (collector.go:376-395) parses the string with `asInt` → 0, and `0 >= 0` passes the check at collector.go:325, so **a PA firewall without usable HOST-RESOURCES data records cpu=0% and memory=0% forever** — green dashboards on a saturated box. The fallback `OIDPanCPU` (25461.2.1.2.3.1.0) is panSessionUtilization, openly labeled "proxy for CPU" — session-table utilization is not CPU and is misleading when charted as `cpu`.

### Storage layer
- ClickHouse `zenplus.snmp_metrics` (raw scalars, 30-day TTL) + `snmp_metrics_5m` MV (90d) — migrate-004-snmp-clickhouse.sql:9-49.
- `zenplus.snmp_if_metrics` (raw per-interface, 30-day TTL) + `snmp_if_metrics_5m` MV (90d) — :74-133.
- **`snmp_metrics_1h` and `snmp_if_metrics_1h` are created but have no materialized view and no backfill job** (explicit "deferred" notes at :67-71 and :155) and no reader anywhere in server/ or poller/ (grep `snmp_metrics_1h|snmp_if_metrics_1h` → only the SQL file). They will stay empty; the promised 1-year retention does not exist. `snmp_if_metrics_5m` is written but also never read (API always queries raw — devices.py:628 "Always bucketed from the raw snmp_if_metrics table").
- Postgres: `device_interfaces`, `device_entities`, `device_sensors` upserted each poll (postgres.go:331-440); rows never pruned ("a separate cleanup job can prune" — postgres.go:333, no such job exists).

---

## 2. Interface utilization math

### Delta/rate computation (poller)
`diffInterfaces` (collector.go:633-689) keeps an **in-memory** previous snapshot per device+ifIndex and computes `bps = delta_octets * 8 / dt` where `dt` is the true wall-clock gap between polls — so a missed cycle just widens dt and the rate stays correct. `rateBps` (collector.go:696-709):

```go
case cur >= prev:               delta = cur - prev
case !hc && prev < MaxUint32:   delta = (MaxUint32 - prev) + cur + 1   // 32-bit wrap
default:                        return 0                               // 64-bit reset → drop
```

Correctness problems:

1. **Reboot on 32-bit counters → spurious traffic spike.** A counter reset (device reboot, ifIndex re-init, `clear counters`) where `cur < prev` is *always* treated as a wrap for non-HC counters. Example: prev=4,000,000,000, cur=1,000 → delta ≈ 295 MB over 30 s ≈ 78 Mbps phantom traffic; worst case ~4.29 GB. There is **no sysUpTime discontinuity check** (sysUpTime is collected right there in the same poll — collector.go:72-83 — but never compared) and **no sanity clamp against ifSpeed**. Industry practice (SolarWinds, LibreNMS, Zabbix) drops the sample when uptime decreased or the implied rate exceeds line rate.
2. **32-bit counters wrap faster than the poll interval on fast links.** Fixed 30 s cycle (engine.go:230): a 1 Gbps interface wraps ifInOctets every ~34 s, 10 Gbps every ~3.4 s. Multiple wraps within one interval are mathematically undetectable → silent undercount. The code prefers HC counters when present, but when a v2c device exposes only 32-bit counters (or v1 — see §3) on >1G ports there is no warning, no event, nothing in the UI.
3. **HC-preference heuristic flips counter sources.** `ok && asUint(*v) > 0` (collector.go:478, 484): an idle interface with HC support reads HC=0 → falls back to the 32-bit value; once traffic starts it switches to the HC value. If the 32-bit counter had already wrapped (so the two counters differ), the first delta after the switch is garbage. Also `iface.HasHC` is a *single* flag set by either direction (collector.go:480, 486) but `rateBps` applies it to both directions — an interface with HC-out but zero HC-in gets its 32-bit in-counter treated as 64-bit, so a real 32-bit wrap returns 0 instead of the wrap-corrected delta.
4. **First poll after poller restart writes bps=0** rather than skipping the sample (prev cache is process-memory only, collector.go:23-24, 655-662) → every restart paints a dip-to-zero across all interface graphs.

### Errors/discards/packets are never delta'd — systemically wrong displays
The poller writes **raw cumulative counter values** for in/out errors, discards and ucast packets into `snmp_if_metrics` (types.go:110-126, collector.go:669-686 — only octets get diffed). Then:
- the 5m rollup MV does `sum(in_errors)` over the bucket (migrate-004-snmp-clickhouse.sql:127-130) — summing a *cumulative* counter sampled ~10× per bucket inflates it ~10×;
- the API does the same for chart buckets: `sum(in_errors) ... sum(in_ucast_pkts)` (devices.py:725-732);
- the UI then **sums those sums across all buckets**: "Total Errors" KPI and In/Out Errors rows (DeviceDetailPage.tsx:2622, 2680-2683).

Net effect: a device with a static lifetime counter of 5 errors will show "Total Errors ≈ 5 × samples-per-bucket × buckets" (easily tens of thousands). Error/discard reporting is unusable as built. Correct design: diff the counters in the poller exactly like octets (delta per sample), then `sum()` is meaningful.

A second, smaller error display bug: the device-detail interface list reads `i.in_errors`/`i.out_errors` from `/devices/{id}/interfaces` (DeviceDetailPage.tsx:1016), but that endpoint selects no error columns (devices.py:470-472) → the Errors column is **always 0**. Same page also has dead sort: `case 'errors': return 0` (DeviceInterfacesPage.tsx:142-143).

### Utilization % formula — wrong for full duplex, inconsistent across the product
- DeviceInterfacesPage.tsx:101 and DeviceDetailPage.tsx:1015, 2460:
  `util = min(100, (inBps + outBps) / speed * 100)`
  That is the **half-duplex** formula. On a full-duplex link (i.e. virtually every switched/routed port today) capacity is `speed` *per direction*; the correct figure is `max(in,out)/speed` (per direction). A 1 Gbps link doing 500M in + 500M out shows **100%** while it is actually at 50% each way; up to 2× overstatement, then silently clamped by `min(100, …)`.
- topology.py:669-671 uses `bps = max(in_bps, out_bps); util = bps/speed` — the correct full-duplex formula. So the Topology map and the Interfaces page can disagree by 2× on the same link at the same moment.
- **No duplex awareness anywhere**: dot3StatsDuplexStatus / ifMauType are not polled, so the product cannot even pick the right formula per interface.
- Speed source: ifHighSpeed×1e6 preferred, else ifSpeed (collector.go:457-466). When ifHighSpeed is absent and ifSpeed is pegged at 4,294,967,295 (the RFC 2863 sentinel for >4.29G), the sentinel is stored as a real speed → nonsense utilization on fast links of older gear. No per-interface manual speed override exists (relevant for shaped circuits/sub-rate links — a standard NPM feature).

### Poll interval and missed polls
- `snmp_poll_interval` is loaded from the DB (postgres.go:191, 227), exposed in the device form (DeviceFormDialog.tsx:372, range 30–3600 s) and displayed on the device page (DeviceDetailPage.tsx:2841) — **and completely ignored**. `runSNMPCycle` polls *every enabled device* on a fixed 30 s ticker (engine.go:230, 898-903). The comment at engine.go:226-229 claims "per-device intervals are honored inside runSNMPCycle via a next-due map" — no such map exists. A user setting 600 s to be gentle to a fragile WAN device still gets hit every 30 s; a user setting 30 s for a critical device gets nothing extra.
- Overlap protection: a cycle that runs >30 s causes subsequent ticks to be skipped (`snmpRunning`, engine.go:891-896), stretching everyone's effective interval. Rates remain mathematically correct (true dt) but sample spacing becomes irregular fleet-wide because of one slow device cohort.
- Per-device hard budget 20 s (engine.go:935); on budget expiry partial results (system info, inventory) are persisted but time-series writes are skipped (engine.go:1064-1073) — note `diffInterfaces` already consumed the delta in that case, so that interval's traffic is simply lost (gap, not corruption).

---

## 3. SNMP v1/v2c/v3, bulk, timeouts, scalability

### Protocol support
- Session factory (session.go:51-72): v1, v2c (default), v3 USM with MD5/SHA/SHA-224/256/384/512 auth and DES/AES/AES-192/AES-256 priv; auto security level from supplied creds (session.go:77-88); v3 contextName supported. AES-192/256 here are the Cisco-style (non-Reeder) variants in gosnmp — some devices need the "C" variants (`AES192C/256C`), which are not selectable.
- **SNMPv1 table polling is broken.** Every table read uses `BulkWalkAll` (collector.go:185-205, 406, 526-536, 582-589). gosnmp v1.43.2 `BulkWalk` always issues GETBULK (gosnmp.go:610-611) and `GetBulk` hard-fails on Version1: `"GETBULK not supported in SNMPv1"` (gosnmp.go:491-493, error returned straight out of walk.go:63-65). There is no GetNext fallback in gosnmp or in our code. So a v1 device gets sysDescr/uptime via GET and **nothing else** — no interfaces, no CPU/memory, no sensors. Worse, it is *silent*: `r.Err` is only set when system **and** interfaces both fail (collector.go:134), so the poll is marked successful (`MarkSuccess`) and the device just looks like it has zero interfaces.
- `3DES` is accepted by the DB constraint (migrate-004-snmp.sql:36-37) but rejected by `parsePrivProtocol` (session.go:128-142) → a device saved with 3DES fails session creation every poll.

### Bulk parameters, timeouts, retries
- MaxRepetitions default 25, device-configurable, capped at 255 (session.go:33-36).
- Flat timeout default 2 s, retries 2 (session.go:25-31); deliberately no exponential timeout (good call, documented). Worst case per failed PDU exchange ≈ 6 s; the 20 s device budget caps total damage.
- Session cache: one cached UDP socket per device, fingerprinted on credentials, evicted after 3 consecutive failures (session.go:144-236). Sessions are serialized per device (single-flight cycle) — correct, since gosnmp sessions are not goroutine-safe.

### Scheduling model and capacity
- Single global cycle, 200 worker goroutines (engine.go:916), one device = one job, ~20–38 *sequential* `BulkWalkAll`s per device (14 ifTable cols + 6 ifXTable + 2 HR-CPU/4 HR-storage + 8 ENTITY + 5 SENSOR + vendor extras). On a LAN with 1 ms RTT and a 48-port switch this is roughly 60–120 round trips ≈ well under 1 s; over a 100 ms WAN it is 6–12 s per device.
- Ceiling estimate: healthy LAN fleet ≈ 200 workers × (30 s / ~1 s per device) ≈ **a few thousand devices**; but the budget math collapses with dead devices — 200 workers × 20 s budget means **just ~300 unreachable devices** push the cycle past 30 s and start stretching everyone's interval. There is no per-device scheduling, no jitter/spreading (every device is hit at the same instant every 30 s — synchronized burst on the network and the ClickHouse writer), no multi-poller sharding (poller_id is recorded but nothing partitions devices across pollers).
- Counter cache `prevIfs` is never evicted when a device is deleted (sessions are dropped, engine.go:876, but the collector cache has no Drop) — slow unbounded growth on churny fleets.
- ClickHouse writes are batched (`RunSNMPBatchWriter`, clickhouse.go:319-342) — sane.

---

## 4. Interface state monitoring

What exists:
- ifAdminStatus/ifOperStatus strings are stored on `device_interfaces` and **overwritten on every poll** (postgres.go:351-360) — last-state only, no transition log, no timestamps of change.
- `oper_status` is written to each `snmp_if_metrics` row, but collapsed to 1 bit: `up`=1, everything else (down/testing/dormant/notPresent/lowerLayerDown) = 0 (collector.go:664-667). You can reconstruct history from ClickHouse by eyeballing, but nothing does.
- linkDown/linkUp **traps** are severity-mapped (traps.go:203-217) and trap alert rules can filter on OID 1.3.6.1.6.3.1.1.5.3 (alert_engine.py:937-958, AlertRuleFormDialog.tsx:252-260). This is passive — only works if the operator configures every device to send traps to this box; the trap doesn't update `device_interfaces.oper_status` either.

What does NOT exist:
- **No polling-based interface-down detection or alert.** The alert engine evaluates only `ping_status`, `rtt`, `packet_loss` (alert_engine.py:425-429). The DB CHECK constraint *advertises* `if_oper_status`, `if_in_bps`, `if_errors`, `cpu`, `memory`, `temperature`… (migrate-004-snmp.sql:175-180) and such rules can be created, but `_eval_one` looks the metric up in a dict that never contains those keys → `values.get(metric)` is None → **the rule silently never fires** (alert_engine.py:317-324). Even `jitter`, offered in the rule UI dropdown (AlertRuleFormDialog.tsx:276), is absent from `metric_values` — a jitter rule is a no-op.
- **No flap detection** of any kind: `grep -rni flap server/ poller/ dashboard/src` → zero hits.
- No admin-up/oper-down (fault) vs admin-down (intentional) distinction in alerting; no hold-down/dampening concepts.
- `device_interfaces.monitored` flag exists in schema/UI but nothing reads it — the poller walks and stores every interface regardless; there is no way to actually unmonitor a port.

---

## 5. Routing protocol awareness

**None.** `grep -rniE "bgp|ospf|eigrp|isis" poller/ --include=*.go` (excluding netflow) → zero hits; same for server/app except netflow.py:80,117 which merely label IP protocol 89 as "OSPF" and port 179 as "BGP" in flow displays. There is no BGP4-MIB (1.3.6.1.2.1.15) peer-state polling, no OSPF-MIB (1.3.6.1.2.1.14) neighbor polling, no vendor session-state OIDs (cbgpPeer2State, etc.), no alerts on peer transitions. For a SolarWinds NPM competitor this is a headline gap: "BGP peer down" is a canonical NOC alert.

---

## 6. MIB library

`MibLibraryPage.tsx` → `POST/GET/DELETE /api/v1/snmp/mibs` (snmp.py:692-770). It is a **file shelf**: validated filename, 4 MB cap, sha256, stored in `/opt/zenplus/data/mibs`, row in `snmp_mibs`. There is no MIB compilation, no OID→name resolution, no trap decoding via uploaded MIBs, no custom-OID poller fed by them. The UI says so itself: "Files are stored on disk; runtime compilation lands in a later update" (MibLibraryPage.tsx:58). Trap names are raw OIDs except 5 hard-coded generics (traps.go:203-217). Uploading a MIB today changes nothing about monitoring — the page is honest but the feature is cosmetic.

`device_profiles.oid_groups` (the mechanism intended to let profiles drive vendor metric collection, profile.go:28-31 "reserved for Phase 3") is stored and editable via `/snmp/profiles` CRUD (snmp.py:825-891) but **never read by the poller** — vendor collection is hard-coded Go (collector.go:249-373). Operators can author OID groups in the UI that do nothing.

---

## 7. Security observations (SNMP subsystem)

- Device-level v3 passphrases: encrypted AES-256-GCM (BYTEA) and decrypted only in poller memory (crypto.go, postgres.go:230-244) — good.
- **`snmp_credentials` stores v3 passphrases in plaintext TEXT** (migrate-016:30; acknowledged at snmp_credentials.py:339-341), revealed by `GET /snmp-credentials/{id}/secrets` (admin-only, audited).
- **`discovery_jobs` copies v3 passphrases in plaintext** into another table on every sweep (snmp.py:511-537).
- Trap listener accepts any community and any source IP (traps.go:92-93 "we accept any"), and trap-matched alert rules fire notifications (engine.go:507-530 → alert_engine.py trap path) — a single spoofed UDP packet to :162 can generate critical alerts/emails. No source ACL, no community check, no rate limit.
- Discovery sweep shells out to `snmpget` with the community/passphrase on argv (snmp.py:94-134) — visible in `ps` during sweeps.

---

## 8. Summary scorecard

| Capability | State |
|---|---|
| ifTable/ifXTable, HC counters | Yes (HC preferred, with heuristic flaws) |
| CPU/memory standard | Yes (HOST-RESOURCES) |
| CPU/memory vendor | Cisco/Fortinet/Juniper OK; Palo Alto broken OIDs; Aruba CPU-only; F5/MikroTik/Arista/HPE/Dell absent |
| Hardware sensors | RFC 3433 only; PSU/fan oper-status not polled; vendor ENVMON MIBs absent |
| Counter math | bps deltas OK; 32-bit reboot spike; no uptime/line-rate guard; errors/pkts never delta'd |
| Utilization % | Half-duplex formula in interface pages (wrong), full-duplex in topology (inconsistent) |
| v1 / v2c / v3 | v2c/v3 work; **v1 table polling broken (GETBULK)**; 3DES advertised but unsupported |
| Scheduling | Fixed 30 s for all; per-device interval ignored; single poller; no jitter |
| Interface state | Last-state only; no events, no alerts, no flap detection; traps passive-only |
| Routing protocols | Nothing (no BGP/OSPF MIBs) |
| MIB library | Storage only, no compilation/use |
| SNMP metric alerting | cpu/memory/temp/if_* rule types accepted but never evaluated (silent no-op) |
