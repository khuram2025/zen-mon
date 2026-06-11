# NetFlow / IPFIX Subsystem Audit — ZenPlus

Date: 2026-06-10
Auditor: automated deep-read + live-system verification (read-only)
Scope: `poller/internal/netflow/` (v5/v9/IPFIX parsers + tests), `poller/cmd/netflow-collector/main.go`,
`server/app/api/v1/netflow.py` (2109 lines, all read), `server/app/services/geoip.py`,
`server/app/models/netflow_saved_view.py`, `scripts/migrate-20260506-netflow-clickhouse.sql`,
dashboard pages `NetflowPage.tsx`, `NetflowAnomalies.tsx`, `NetflowCapacity.tsx`, `NetflowForensics.tsx`, `NetflowSavedViews.tsx`.

---

## 0. Live-system state (today)

```
$ curl -s http://localhost:8091/health
{"status":"ok","service":"zenplus-netflow-collector","records_ingested":0,
 "records_dropped_implausible":0,"records_dropped_queue_full":0,
 "records_dropped_unknown_exporter":0,"parse_errors":0,"queue_len":0,"queue_cap":4000}

ClickHouse (via /opt/zenplus/.env creds):
SELECT count() FROM zenplus.flow_records      -> 0
SELECT count() FROM zenplus.flow_traffic_5m   -> 0

$ ss -lun | grep 2055
UNCONN 0 0 *:2055 *:*          (collector listening, healthy, 5h30m uptime)

Postgres:
SELECT count(*) FROM device_interfaces;  -> 0   (27 rows in devices)
```

**The entire NetFlow module is dark on this appliance today**: the collector is healthy and
listening on UDP/2055, but no exporter is sending flows (0 records ever ingested this boot, both
ClickHouse tables empty). Additionally `device_interfaces` is empty, so even when flows arrive,
every interface join (names, ifSpeed, utilization %) returns nothing — the Capacity page will show
100% "Unknown speed". GeoIP databases ARE provisioned (`/opt/zenplus/data/geoip/{country,asn}.mmdb`,
17 MB, fetched 2026-06-02) and `netflow-ip-groups.json` exists.

Parser unit tests pass: `go test ./internal/netflow/` -> `ok ... 0.007s`.

---

## 1. Protocol coverage

### NetFlow v5 — `poller/internal/netflow/v5.go`
- Full fixed-format decode: header validation (`count > 30` rejected, truncation checked,
  v5.go:59–73), all 48-byte record fields including next-hop, in/out ifIndex, TCP flags, ToS,
  16-bit src/dst AS, masks (v5.go:93–127).
- Timestamping: `flowTimestamp()` (v5.go:135–141) converts sysUptime-relative LAST_SWITCHED to
  absolute time using exporter `unix_secs`, falling back to receive time. Correct.
- **BUG — v5 sampling mode bits not masked.** v5.go:84 reads
  `SamplingInterval: binary.BigEndian.Uint16(data[22:24])` raw. Per the v5 header spec the field is
  2 bits of sampling *mode* + 14 bits of interval. A Cisco exporter doing 1-in-100 packet sampling
  sends `0x4064`; ZenPlus stores SamplingInterval=16484 and the collector then multiplies bytes and
  packets by **16,484 instead of 100** (main.go:150–153) — a ~165× traffic inflation for any
  sampled v5 exporter. Fix: `& 0x3FFF` (and ignore mode!=1 if desired).

### NetFlow v9 — `poller/internal/netflow/v9.go`
- Template FlowSets (ID 0) parsed and cached per `(exporter, sourceID, templateID)`
  (v9.go:218–244); cache is mutex-guarded and TTL-expired at 24 h (v9.go:17, 71–85). Good.
- Options Templates (ID 1, RFC 3954 §6.1) parsed (v9.go:255–290); options *data* records are
  recognised and **skipped** from the flow path, with sampler learning (fields 34 / 50 / 305) via
  `extractSampler` (v9.go:364–392). This fixed the historical BUG-01 "55-byte fallback" that
  produced exabyte garbage — regression-tested in v9_test.go:106–126 and 131–172.
- Data sets with no learned template are counted (`DataSetsWaiting`) and *not* guess-decoded
  (v9.go:207–210) — correct behaviour; self-heals when the router re-advertises templates.
- Field coverage (`assignV9Field`, v9.go:394–459): IN/OUT bytes+packets (1/2/23/24),
  octet/packetTotalCount (85/86), protocol/ToS/TCP-flags/ports (4/5/6/7/11), IPv4 addrs + next-hop
  (8/12/15), ifIndexes (10/14), masks (9/13), 32-bit AS (16/17), relative and absolute flow times
  (21/22, 150–153 — absolute wins, v9.go:332–347).
- **Not covered:** IPv6 (IEs 27/28/62), MAC addresses, MPLS labels, VLAN (58/59), flow direction
  (61), BGP next hop (18), post-NAT fields, NBAR application-ID (95). See issue I-4 for the IPv6
  consequence.

### IPFIX (v10) — `poller/internal/netflow/ipfix.go`
- Template (set 2) and Options-Template (set 3) sets parsed; enterprise-specific IEs handled for
  alignment (4-byte PEN skipped, enterprise bit kept so they can't collide with standard IDs,
  ipfix.go:94–102); **variable-length fields (RFC 7011 §7) decoded correctly** including the
  255-escape 2-byte length (ipfix.go:186–201). Sampler learned from options data like v9
  (ipfix.go:65–73, tested in ipfix_test.go:83–114).
- Field decode reuses `assignV9Field` (IDs match for common IEs). Same IPv4-only limitation.
- IPFIX messages using only relative LAST/FIRST_SWITCHED fall back to receive-time timestamps
  (`applyFlowTimes(..., exportSecs, 0, receivedAt)` with sysUptime=0 → fallback path,
  ipfix.go:212 + v5.go:136) — acceptable.

### Sampling scaling
- Learned 1-in-N stored per exporter+domain; collector multiplies bytes/packets at ingest
  (main.go:148–153) and keeps the factor in `sampling_interval` so raw values are recoverable.
  Sanity bound (1 TB/flow, 10 G pkts/flow) applied **before** multiplication (main.go:35–38, 90–92)
  and the API re-checks `bytes / greatest(sampling_interval,1) <= 1e12` in `_scope`
  (netflow.py:509–515). Sound design — *except* the v5 mode-bit bug above, and:
- **Sampler learned only from options data.** If an exporter samples but never exports an options
  record carrying IE 34/50/305 (or the collector restarts and flows arrive before the next options
  refresh), flows are under-counted by N with no indication. There is no per-exporter "sampling
  rate assumed = 1" visibility anywhere in the UI/health output.

### sFlow
- **Not supported.** No parser, no listener on 6343 (`ss -lun` shows only 2055); port 6343 appears
  only as a label in `COMMON_PORTS` (netflow.py:197). Competitors (SolarWinds NTA, PRTG,
  LibreNMS) all accept sFlow.

### Template robustness summary
- 24 h TTL, thread-safe, per-exporter/domain keying: good.
- Templates are **not persisted** across collector restarts (in-memory only) — flows are dropped
  (`DataSetsWaiting`) until re-advertisement; typically seconds-to-minutes of loss. Acceptable but
  worth noting; nfdump/pmacct persist template caches.
- Expired/stale entries are never *deleted* from the maps (only ignored on read), so the maps grow
  monotonically with exporter churn — negligible in practice (bounded by exporters×templates).

---

## 2. Aggregation pipeline, retention, and the bandwidth math (the user's suspicion)

### Storage layout (`scripts/migrate-20260506-netflow-clickhouse.sql`, duplicated in main.go:293–353)
- `flow_records` (raw, MergeTree, daily partitions, **TTL 30 days**), ordered by
  `(timestamp, exporter_ip, src_addr, dst_addr, protocol, dst_port)`.
- `flow_traffic_5m` (SummingMergeTree, **TTL 90 days**) fed by MV `flow_traffic_5m_mv`,
  keyed only `(timestamp, exporter_ip, protocol, dst_port)`.

### Finding A — the 5-minute rollup is dead weight; every endpoint scans raw
`grep flow_traffic_5m server/app/api/v1/netflow.py` → **zero matches**. All ~20 endpoints
(overview, timeseries, top-talkers, endpoints, conversations, protocols, ports, applications,
device-status, heatmap, interfaces, dscp, tcp-flags, network-classes, forensics, capacity,
anomalies, countries, ip-groups, exporters) query `zenplus.flow_records` directly, several with
`UNION ALL` self-doubling (top-talkers netflow.py:693–723, top-endpoints :780–812, interfaces
:1297–1315, network-classes :1495–1501, countries :1944–1950, capacity :1663–1675). The main
dashboard fires ~14 of these queries and refetches every 15–60 s (NetflowPage.tsx:354–429).
Consequences:
1. Days 31–90 exist *only* in the rollup that nothing reads → any window beyond 30 days
   (API allows `hours<=720` = 30 d; custom `from/to` is unbounded, netflow.py:28–45) silently
   returns partial/empty data.
2. At realistic flow rates (5–50 k flows/s) this is a serious scalability trap for an appliance:
   every 15 s the UI re-scans the window across 14 queries, two of them reading the table twice.
3. The rollup's dimension set (no src/dst addr, no ifIndex) couldn't serve top-talkers or
   capacity anyway — it was designed for a chart that no longer exists.

### Finding B — bps is computed by naive end-time bucketing; no flow splitting
Every rate in the product attributes **all bytes of a flow to the single bucket containing the
flow's end timestamp** (`timestamp` = last-switched, set at parse time, v9.go:332–340):

- `/timeseries` (netflow.py:644–658): `sum(bytes) * 8 / {bucket}` grouped by
  `toStartOfInterval(timestamp, INTERVAL {bucket} SECOND)`; bucket = 60 s for ≤6 h windows.
- `/overview` `current_bps` (netflow.py:597): `sumIf(bytes, timestamp >= end-300s)*8/300`.
- `/capacity` (netflow.py:1645–1683): per-interface 5-min `slot_bps = sum(bytes)*8/300`, then
  `quantile(0.95)(slot_bps)`, `avg`, `max`.

`first_switched_ms`/`last_switched_ms` are stored but **never used to prorate bytes across
buckets**. With Cisco's default active timeout of 30 minutes (and 1–5 min on most tuned configs),
a long-lived transfer's entire 30-minute byte count lands in one 60 s or 300 s slot:

- a steady 100 Mbps flow exported after a 30-min active timeout shows as a single 5-min slot at
  **600 Mbps** (6×) and 29 minutes of zero;
- `p95(slot_bps)` and `max_bps` on the Capacity page are inflated by the same factor, so
  "Critical ≥70%" classifications (NetflowCapacity.tsx:50–53) fire falsely;
- the timeseries is spiky sawtooth instead of the real rate.

**This is the most likely root cause of the user's "utilization numbers look wrong" complaint.**
Industry practice (nfdump, ElastiFlow, Kentik, SolarWinds NTA) is either start/end proration
(distribute `bytes × overlap/duration` per bucket) or at minimum documenting an exporter
active-timeout requirement ≤ bucket width.

### Finding C — capacity utilization sums both directions against one-way ifSpeed
`/capacity` UNION-ALLs ingress (`input_snmp`) and egress (`output_snmp`) rows and then groups by
`(exporter, ifindex, slot)` summing both into one `slot_bps` (netflow.py:1655–1677), which is then
divided by `if_speed` (netflow.py:1706: `utilization_p95_pct = p95/speed*100`). On a full-duplex
1 Gbps port carrying 600 Mbps in + 600 Mbps out this reports **120% utilization**; the correct
full-duplex answer is max(in, out) = 60% per direction. (`/interfaces`, netflow.py:1287–1346, gets
this right by keeping in_bytes/out_bytes separate — capacity does not.) Combined with Finding B,
utilization on the Capacity page can be wrong by an order of magnitude.

### Finding D — multi-exporter double counting
`/overview`, `/timeseries`, top-talkers etc. sum bytes across **all exporters**. The same flow
observed by two routers in its path (or by both ingress and egress on the same router with v9
egress templates) is counted twice in network-wide totals; there is no dedup or
per-observation-point normalization (standard caveat competitors handle via exporter scoping or
flow-direction filtering; ZenPlus only offers a manual exporter filter).

### Retention math
- Raw: 30 d TTL; API presets cap at `hours=720` = exactly 30 d — consistent, but custom
  `from`/`to` (netflow.py:30–42) is unbounded and silently returns nothing past TTL.
- Rollup: 90 d retention, unread (Finding A).
- No 1-hour/1-day tier for long-horizon capacity trending (SolarWinds keeps 1-y rollups).

### Top-talkers math
Sound aside from B/D: src and dst contributions split via UNION ALL with `src_bytes`/`dst_bytes`
(netflow.py:693–729); totals double-count by design (documented per-endpoint convention,
consistent with top-endpoints/countries). `flows` counts the union rows, so a single flow counts
once as src + once as dst — labelled simply "flows" in UI; mildly inflated but consistent.

---

## 3. Classification, GeoIP, AS, conversations, QoS

### Application classification = static destination-port buckets only
- `APPLICATION_BUCKETS` (netflow.py:230–245): 14 hand-made port sets → donut; fallback "System
  Services" (<1024) / "Other". `_application_for_port` (netflow.py:248–256) checks **dst_port
  only** — a flow recorded in the server→client direction (src_port=443, dst_port=ephemeral) lands
  in "Other". No NBAR2 (IE 95 ignored), no DPI, no SNI/DNS enrichment, no custom user-defined app
  mappings UI (competitors: SolarWinds NTA custom apps, ntopng nDPI).
- `COMMON_PORTS` (netflow.py:87–226): ~140 static port labels. Fine as far as it goes.

### GeoIP pipeline — built, working, but invisible in the UI
- `services/geoip.py`: vendored pure-Python MMDB reader (no pip dep), lazy + mtime-reloaded,
  graceful no-op when DBs absent; country + ASN lookups (geoip.py:186–216). DBs are provisioned on
  this box. `_enrich_hosts` (netflow.py:408–426) attaches `country/country_name/asn/as_name` to
  top-talkers/endpoints rows, and `/netflow/countries` aggregates top-600 endpoints by country
  (netflow.py:1923–1967, documented approximation).
- **No dashboard component consumes any of it**: `grep -ri "country|asn|ip-groups" dashboard/src`
  → zero UI hits. No geo map, no country table, no AS column. Same for reverse-DNS (`?resolve=1`,
  netflow.py:301–343 — no caller sets it) and IP groups (`/netflow/ip-groups`, netflow.py:1970 —
  no UI, groups only configurable by hand-editing a JSON file on the box).

### AS numbers
- `src_as`/`dst_as` are parsed (v5.go:122–123, v9.go:450–453), stored in ClickHouse — and **never
  queried**: no endpoint aggregates by the columns; the only AS data shown to the API consumer is
  MMDB-derived `asn`/`as_name` on top-N rows (which the UI drops). Peering/transit AS reports —
  a flagship NTA feature — do not exist.

### Conversations
- `/top-conversations` groups by `(src, dst, protocol, dst_port)` (netflow.py:893) —
  **unidirectional**; the reverse direction is a separate row (no bidirectional pairing like
  SolarWinds). Enriched with exporter hostnames and ifIndex→interface names (netflow.py:900–918).
  `avg_duration_ms` uses `last_switched_ms - first_switched_ms` signed-safe (netflow.py:869). OK.

### QoS / ToS
- DSCP distribution endpoint with RFC label map (netflow.py:1355–1410), DSCP drill-down filter
  (`bitShiftRight(tos,2)`, netflow.py:526) and DSCP in forensics. ECN bits ignored (fine). No
  per-class policy/queue-drop correlation (would need CBQoS-MIB SNMP — gap vs SolarWinds CBQoS).

---

## 4. Anomaly detection & capacity pages — promises vs reality

### `/netflow/anomalies` (netflow.py:1719–1898)
What actually runs — five fixed-threshold SQL detectors per request (no background job, no state,
no baselining):
1. SYN scan: SYN-only flows to ≥100 distinct dsts (critical >5000) — netflow.py:1735–1768.
2. RST flood: ≥1000 TCP flows and ≥50% RST per src — :1771–1797.
3. "Sensitive port egress": RFC1918 src → public dst on ports 22/23/3389/445/1433/3306/5432 —
   :1800–1835. (Name/desc say "RFC1918 leakage… leaving via internet-facing iface" but **no
   interface check is performed** — it's just private→public on sensitive ports.)
4. ICMP flood: ≥5000 ICMP flows per src — :1838–1861.
5. "Volumetric outlier": comment claims "mean+3σ proxy" (:1863) but the code is actually
   `top src > 50% of top-5 total AND > 10 GB` (:1879–1881) — **no σ is computed**.
The UI (NetflowAnomalies.tsx:84) honestly lists exactly these five algorithms. So the page is
truthful, but the detection itself is static-threshold only: no time-series baselining, no
learning, no DDoS amplification/DNS-tunnel/beaconing detectors, no alert-engine integration
(findings are not persisted, not notified — they vanish with the request). The "Plixer-FA-style"
banner comment (netflow.py:1715) oversells.

### Capacity page (NetflowCapacity.tsx + `/netflow/capacity`)
Promises "95th-percentile utilization vs SNMP ifSpeed — billing & growth view" (.tsx:72) and
"Industry-standard 95p over 5-minute slots; billing benchmarks usually quote this number"
(.tsx:94). Reality:
- p95 of naive end-bucketed slots (Finding B) — not billing-grade; a billing 95p from this data
  would be defensibly wrong.
- in+out summed vs one-way ifSpeed (Finding C).
- On this appliance `device_interfaces` is empty → `if_speed` always null → utilization always
  "—" / "Unknown speed" for every row; the page degrades to a raw-bps ranking.
- No growth/forecast ("growth view") — no trend line, no projected exhaustion date.

### Main-page "Device Status" + "Alerts / Incidents" — misleading labels
Backend was explicitly fixed to flag these as heuristics: `flow_derived: true`,
"Deprecated aliases kept for backward compatibility only; the UI must **not** present these as real
latency / packet-loss / uptime" (netflow.py:1151–1166). The UI ignores this:
- NetflowPage.tsx:1747–1748 renders MiniStats literally labelled **"Latency"** (avg flow duration
  ms) and **"Packet Loss"** (TCP RST ratio).
- NetflowPage.tsx:1651–1658 raises a **critical "Elevated Packet Loss"** alert at RST-ratio ≥5% —
  an RST-heavy but healthy network (port scans, busy web servers) will show a false critical.
- The "Alerts / Incidents" panel ("Live signals from the collector", .tsx:1690) is computed
  client-side per render, not persisted, not connected to the ZenPlus alert engine.

---

## 5. Interface mapping (ifIndex → names/speeds)

The join exists and is correctly built:
- `_resolve_interface_names` (netflow.py:1209–1252): flow `exporter_ip` → `devices.ip_address`
  (INET text-cast, handles `/32` suffix) → `device_interfaces` rows → `{if_name, if_descr,
  if_alias, if_speed}`; if_speed is in **bps** (poller prefers ifHighSpeed×1e6,
  poller/internal/checker/snmp/collector.go:460–464; stored by
  poller/internal/store/postgres.go:334–373).
- Consumed by `/interfaces`, `/capacity`, `/top-conversations`, `/forensics` so utilization % is
  *possible* in principle.

Caveats:
1. **Live box: `device_interfaces` has 0 rows** (27 devices) — the mapping yields nothing today;
   every interface renders as "ifIndex N", utilization always null. SNMP interface discovery has
   not populated the table for any device on this appliance.
2. **ifIndex truncated to 16 bits** end-to-end: `Record.InputSNMP/OutputSNMP uint16` (v5.go:28–29),
   `uint16(uint64BE(value))` for IEs 10/14 (v9.go:434–445), `input_snmp UInt16` in ClickHouse.
   v9/IPFIX allow 4-byte ifIndexes; large chassis (Nexus, Junos logical ifs) commonly exceed
   65535 → wrong ifIndex → joins to the wrong (or no) interface, silently.
3. Exporter matching is by exact source-IP equality; flows from a router whose flow-export source
   interface IP differs from the IP under which the device was discovered will never match
   (no per-device "also known as" exporter-IP mapping).

---

## 6. Collector engineering notes

- Single-goroutine UDP read loop → parse → per-record enqueue with 250 ms bounded backpressure
  (main.go:104–184); queue cap = 4×batch = 4000 records; batch insert 1000/5 s. No SO_RCVBUF
  tuning, no multiple readers; at tens of kflows/s the kernel will drop datagrams silently
  (only `droppedQueueFull` is observable, not kernel drops). Adequate for SMB, a trap for the
  "SolarWinds competitor" positioning.
- Exporter allowlist (env `NETFLOW_ALLOWED_EXPORTERS`, default allow-all) — good anti-spoofing
  option (main.go:96–102).
- Schema auto-created on startup (`ensureSchema`) — convenient, but it duplicates the SQL migration
  file; the two copies must be kept in sync by hand (they match today).
- Health endpoint on :8091 with drop counters — good operational visibility, but these counters
  are not surfaced in the ZenPlus UI anywhere.
- ClickHouse ORDER BY starts with `timestamp` (DateTime64(3), ~unique) which makes the remaining
  sort-key columns nearly useless for data skipping; fine for time-range scans, but per-host
  forensics over long windows always full-scans the window.

## 7. Saved views

CRUD complete and used (model netflow_saved_view.py; API netflow.py:1906–1920, 2022–2075; pages
NetflowForensics.tsx:108–117, NetflowSavedViews.tsx). Notes: any authenticated user can delete any
other user's view (no owner check, netflow.py:2063–2075); `pinned` can be set via API but no UI
exposes pinning; no update/rename endpoint (only create/delete).

---

## Issue list (wrong/broken/risky today)

| # | Sev | Issue | Evidence |
|---|-----|-------|----------|
| I-1 | critical | bps/p95/utilization math: all bytes of a flow attributed to the end-time bucket; first/last_switched stored but never used to prorate. Long flows (≥ active timeout) create 2–6× spikes; p95/max and Capacity "Critical" bands inflated. | netflow.py:644–658 (timeseries), :1645–1683 (capacity), v9.go:332–340 |
| I-2 | high | NetFlow v5 sampling: mode bits (top 2) not masked from `sampling_interval`; sampled-v5 traffic multiplied by `0x4000+N` instead of N (~165× at 1-in-100). | v5.go:84, main.go:150–153 |
| I-3 | high | Capacity utilization divides (in+out) by one-way ifSpeed → up to 2× overstatement on full-duplex links; contradicts the per-direction handling in `/interfaces`. | netflow.py:1655–1677, 1706 |
| I-4 | high | IPv6 flows decode with src/dst = 0.0.0.0 (IEs 27/28 unhandled, `fillMissingAddrs` backfills zeros) but bytes/ports/protocol still ingested → phantom "0.0.0.0" talkers polluting every aggregate on dual-stack networks. | v9.go:349–359, 394–459; ClickHouse `src_addr IPv4` |
| I-5 | high | 5-min rollup (`flow_traffic_5m`, 90 d TTL) is written but never read; all ~20 endpoints scan raw `flow_records` (30 d TTL), several twice via UNION ALL, refetched every 15–60 s by the UI. Data for days 31–90 is unreachable; serious appliance scalability trap. | grep flow_traffic_5m netflow.py → 0 hits; netflow.py:693, 780, 1297, 1495, 1663; NetflowPage.tsx:354–429 |
| I-6 | medium | UI presents RST-ratio as "Packet Loss" and flow duration as "Latency", incl. a client-side **critical** "Elevated Packet Loss" alert — backend explicitly deprecates this presentation (BUG-04 comment). | NetflowPage.tsx:1747–1748, 1651–1658 vs netflow.py:1151–1166 |
| I-7 | medium | ifIndex truncated to uint16 across parser/schema; 32-bit ifIndexes (big chassis) map to wrong interfaces silently. | v5.go:28–29, v9.go:434–445, schema `input_snmp UInt16` |
| I-8 | medium | Live appliance: NetFlow module completely dark (0 flows ever ingested this boot; both CH tables empty) and `device_interfaces` empty (0 rows / 27 devices) so interface names + utilization can never resolve. Module is effectively un-demoable on the box. | health endpoint output; CH counts; PG counts (§0) |
| I-9 | medium | Multi-exporter double counting in network-wide totals (overview/timeseries/talkers) when ≥2 exporters see the same flow; no dedup/normalization guidance. | netflow.py:587–629, 644–658 |
| I-10 | low | "Volumetric outlier" claims mean+3σ in comment but implements top>50%-of-top5 AND >10 GB; "sensitive egress" description claims interface awareness it doesn't have. | netflow.py:1863–1892, 1799–1835 |
| I-11 | low | Saved views: no ownership enforcement on delete; `pinned` unreachable from UI; no rename. | netflow.py:2063–2075 |
| I-12 | low | Custom `from`/`to` windows unbounded vs 30 d raw TTL → silent empty results; collector drop counters (queue-full, implausible, unknown-exporter) not surfaced in UI. | netflow.py:28–45; main.go:367–378 |

## Gaps (missing vs competitors)

1. No sFlow support (port 6343 not listened; SolarWinds NTA/PRTG/LibreNMS all ingest sFlow).
2. No IPv6 flow support end-to-end (parser IEs 27/28, ClickHouse IPv4 columns, UI).
3. No NBAR2/DPI/custom application mapping — classification is static dst-port buckets only;
   IE 95 (applicationId) discarded.
4. GeoIP/ASN/rDNS/IP-groups are backend-complete but have **zero UI** (no geo map/country table/AS
   report); stored flow `src_as`/`dst_as` columns never queried — no BGP/peering analytics.
5. No flow-based alerting integrated with the ZenPlus alert engine (bandwidth threshold, anomaly
   findings → notifications); anomalies and the "Alerts/Incidents" panel are ephemeral per-request.
6. No statistical baselining/forecasting: anomalies are fixed thresholds; capacity page has no
   trend/forecast ("growth view" promised, not delivered) and no >30 d horizon (rollup unread).
7. No bidirectional conversation pairing (uni-directional 4-tuples only).
8. No exporter management UI: exporters are implicit; allowlist only via env var; no per-exporter
   sampling-rate override or sampling visibility.
9. No VLAN / MAC / MPLS / direction(IE61) / post-NAT field support.
10. No template-cache persistence across collector restarts.
11. IP groups configurable only by hand-editing a JSON file on the appliance — no CRUD UI/API.
12. No CBQoS-style per-class QoS monitoring (DSCP volume split only).
