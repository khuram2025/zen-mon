# NetFlow development review — verification, assessment and fix plan

Review verified: `/home/zen/NetFlow-development-review.md` (1 October 2026, appliance 10.11.40.31,
1.23.21). Code verified: `release/1.23.22` (`dca84fd4`), whose NetFlow files are byte-identical to
1.23.21. Branch with the fixes: `fix/netflow-development-review`.

## 1. Verdict

**All 34 findings are confirmed.** None was refuted; several are understated, a few overstate
one detail, and many line references are stale by a few lines. Each finding was checked by an
adversarial verifier instructed to refute it, most with a reproduction (scratch ClickHouse
24.10 + PostgreSQL 16 + the real API code, Playwright against the real UI, Go tests for the
decoders, and a new v5/v9/IPFIX traffic simulator).

Severity after verification: 11 × P1, 23 × P2. Changes from the review:
NF-05 P1 → P2 (saved views are convenience queries; the harm is vandalism/disclosure of
investigation targets), NF-20 P2 → P1 (most application bytes are misattributed, not a label
nuance).

Where the review is understated (the corrections matter for the fix):

* **NF-01** — 1.23.22 already calibrates relative times to the appliance clock, so the text
  now says "just now" while the badge, the critical "Device Offline" alert and the exporter
  dot still use the browser clock: the page contradicts itself. There are three clocks, not
  two (exporter flow time, collector receipt time, API time). Exporter liveness is also
  computed under the page's drill-down filters, so any filter can raise a false critical
  alert (X-04). The earlier NCCM review of the same appliance found the 12-hour skew was
  probably the reviewer's clock; the code defect stands either way.
* **NF-06** — a normal `systemctl restart` or OTA also loses the in-memory batch and the whole
  queue (cancelled context reused for the final flush; `main` does not wait for the writer).
  If ClickHouse is down at start the collector exits instead of listening (X-14).
* **NF-13** — omitting empty slots overstates p95/avg by up to ~200× on bursty interfaces,
  not only the 7.7% edge effect; `quantile()` is ClickHouse's sampled, non-deterministic
  estimator; long flows are dumped into their end slot (X-26).
* **NF-17** — even hour-aligned custom windows include one extra hour; the unfiltered page
  disagrees with its own chart for 72–168 h windows (chart raw, totals rollup).
* **NF-20** — classification uses the destination port only: response halves (usually most
  bytes) land in "Other" or a random bucket ("SAP" for client port 50000). Conversations
  fragment one exchange into a row per ephemeral port (X-30).
* **NF-21** — the ICMP detector is also blind to real floods (one record per active timeout)
  and to distributed floods.
* **NF-32** — IPv6 exporters all share one template-cache key, so two IPv6 exporters that
  reuse template ID 256 decode each other's data with the wrong layout (garbage, not zeros).

Two new defects are as serious as anything in the review:

* **X-02 (P1)** — NetFlow v5 sampling-mode bits are not masked: a 1-in-100 sampled Cisco v5
  exporter is stored ~165× too large (0x4064 read as 16,484). Every volume, capacity, top-N
  and report figure for such exporters is wrong, including 90-day rollups.
* **X-01 (P2)** — the `netflow.view` permission exists in the role editor but is enforced
  nowhere: removing NetFlow from a role removes no access (API or menu).

## 2. Method

1. Eight verification agents, one per finding group, each told to refute its findings from
   the code and reproduce where practical (evidence and fix designs are retained with the
   branch). Two further agents swept for defects the review missed.
2. The 81 raw extra-defect reports were deduplicated to 52 and each was re-checked by an
   independent skeptic against a frozen copy of the reviewed code: **50 upheld, 2 refuted**.
3. A feature inventory of the module (54 capabilities) and a competitive assessment of 12
   products using current (2025–2026) vendor documentation.
4. A traffic simulator (`scripts/netflow-simulator.py`) reproduced NF-07 (all flag-less TCP
   stored as zero flags), NF-32 (1,724 IPv6 flows stored as 0.0.0.0) and NF-33 (ifIndex 70000
   stored as 4464) against the unmodified collector.

## 3. Findings NF-01 … NF-34

| ID | Review | Verdict | Our severity | Title |
|---|---|---|---|---|
| NF-01 | P1 | confirmed | P1 | Exporter status uses a different clock from its data |
| NF-02 | P1 | confirmed | P2 | Compare uses incompatible windows and omits filters |
| NF-03 | P1 | confirmed | P1 | Loading and failures can appear as healthy or empty telemetry |
| NF-04 | P1 | confirmed | P1 | NetFlow queries do not enforce user visibility scope |
| NF-05 | P1 | confirmed | P2 | Saved-view writes lack role and ownership checks |
| NF-06 | P1 | confirmed | P1 | Collector discards a failed database batch |
| NF-07 | P1 | confirmed | P1 | Missing TCP flags are treated as real zeros |
| NF-08 | P1 | confirmed | P1 | Raw-flow navigation loses dashboard filters |
| NF-09 | P1 | confirmed | P1 | Anomaly Investigate does not preserve finding evidence |
| NF-10 | P2 | confirmed | P2 | Forensics silently stops at 500 records |
| NF-11 | P2 | confirmed | P2 | Search button does not commit a query |
| NF-12 | P2 | confirmed | P2 | Saved views cannot reproduce their saved query |
| NF-13 | P1 | confirmed | P1 | Capacity statistics use incomplete and unequal slots |
| NF-14 | P1 | confirmed | P1 | Capacity combines directions against one link speed |
| NF-15 | P2 | confirmed | P2 | Headroom shows utilization instead |
| NF-16 | P2 | confirmed | P2 | Capacity cannot guide users out of unknown speed |
| NF-17 | P2 | confirmed | P2 | Long-window rollup edges include extra history |
| NF-18 | P2 | confirmed | P2 | Average flow duration changes units and is capped |
| NF-19 | P2 | confirmed | P2 | TCP RST ratio uses all protocols as its denominator |
| NF-20 | P2 | confirmed | P1 | Service classification mislabels response traffic |
| NF-21 | P2 | confirmed | P2 | ICMP flood detection depends on cumulative record count |
| NF-22 | P2 | confirmed | P2 | Volumetric-outlier explanation uses the wrong total |
| NF-23 | P2 | confirmed | P2 | Alerts / Incidents is not an incident lifecycle |
| NF-24 | P2 | confirmed | P2 | Top Talkers changes ranking when View all is opened |
| NF-25 | P2 | confirmed | P2 | Donut totals and percentages use an undisclosed subset |
| NF-26 | P2 | confirmed | P2 | Overview search filters only ten loaded conversations |
| NF-27 | P2 | confirmed | P2 | Top Interfaces ignores most page filters without a scope label |
| NF-28 | P2 | confirmed | P2 | Heatmap lacks date, timezone and coverage semantics |
| NF-29 | P2 | confirmed | P2 | Light-theme status cards have very weak text contrast |
| NF-30 | P2 | confirmed | P2 | Narrow screens truncate decision-making KPI values |
| NF-31 | P2 | confirmed | P2 | Custom queries and database errors lack protective contracts |
| NF-32 | P1 | confirmed | P1 | IPv6 telemetry can become artificial IPv4 endpoints |
| NF-33 | P2 | confirmed | P2 | Interface indexes are narrowed to 16 bits |
| NF-34 | P2 | confirmed | P2 | Saved-view workflow gives poor feedback and incomplete control |

### NF-01 — Exporter status uses a different clock from its data

**Verdict:** confirmed · review P1 → assessed P1

**Evidence.** The browser clock is compared with the appliance clock at NetflowPage.tsx:550-556 (`const age = Date.now() - new Date(overview.data.last_seen).getTime()`, <2m receiving, <15m stale, else 'offline'), in buildAlerts at :1842-1853 (`const now = Date.now()` → 'Device Offline', critical) and in the DeviceStatusCard dot at :1997 (`Date.now() - new Date(e.last_seen)`). last_seen is appliance time: netflow.py:728 / :702 / :2502 use `max(received_at)`, and received_at is stamped by the collector host at main.go:135 (`receivedAt := time.Now().UTC()`). Relative windows are resolved on the appliance at netflow.py:54 (`end = datetime.now(timezone.utc)`). The doc's line refs O:550/1840/1997 and API:36/677/2484 are accurate. NetFlow files are byte-identical between 1.23.21 and this worktree (git diff 0e481598..HEAD shows no NetFlow changes).

**Reproduction.** (a) esbuild-bundled real serverClock.ts + utils.ts with Date.now mocked +12h and an exact copy of the health logic at :550-556. Before calibration: health=offline, relativeTime='12h ago'. After one X-Server-Time sample (1.23.22 behaviour): health=offline, relativeTime='just now'. (b) Scratch ClickHouse db nfv_clock, calling the real netflow_overview/netflow_exporters with the db name rewritten. Exporter 10.0.0.2 has a clock 20 min ahead and delivers a record every minute: overview 1h gives last_seen age 20.0 min (UI → 'offline' + critical 'Device Offline'). Exporter 10.0.0.3 has a correct clock and one ICMP flow 20 min ago: /exporters with page filter protocol=1 gives last_seen age 20.0 min → 'Device Offline', although it is receiving every minute.

**Corrections to the review.** 1) The worktree (1.23.22) is only partly mitigated. Commit e50a85a7 added X-Server-Time, serverClock.ts, /system/time and ClockSkewBanner, and relativeTime() (utils.ts:72) now uses serverNow(). So the 'Last flow 12h ago' text now reads 'just now', but the badge, the alerts and the dot still use raw Date.now(). The page now contradicts itself ('offline' + 'Device Offline' next to 'just now'). No NetFlow page renders ClockSkewBanner. The doc's 'expose authoritative response time plus clock drift' already exists platform-wide; NetFlow just doesn't use it.
2) There are three clock domains, not two. Window filtering uses the EXPORTER-derived `timestamp` (v9.go:332-339 applyFlowTimes, v5.go:135-141 flowTimestamp from unixSecs/sysUptime). received_at is the collector clock, and 'now' is the API clock. An exporter whose clock runs Δ ahead yields last_seen ≈ now−Δ, even when the browser and appliance are in sync (see extra defect).
3) Liveness comes from window-scoped and filter-scoped data. A historical custom window, or a page filter such as protocol, talker or hour, makes a live exporter look offline. The doc only covers the historical case.
4) On the '12h' question: the doc is dated 1 Oct 2026 and its 'independent clock' read 2026-10-01 00:00:20Z. That instant was still in the future when this verification ran (this host read 2026-09-30 ~17:20Z). Fresh flows were also found 'in the preceding appliance hour', which requires exporter timestamps ≈ appliance clock if that query …

### NF-02 — Compare uses incompatible windows and omits filters

**Verdict:** confirmed · review P1 → assessed P2

**Evidence.** The current window is relative and the prior window absolute. NetflowPage.tsx:378-383: rangeQS sends only `hours` unless isCustom, so the server resolves [now−H, now] (netflow.py:54). priorQS (:364-374) sends absolute from/to derived from range.fromISO/toISO, which come from useTimeRange's nowMs. That was Date.now() in 1.23.21 (git show 0e481598 TimeRangePicker.tsx:97) and is serverNow() minute-floored in 1.23.22 (TimeRangePicker.tsx:121-140).
priorQS copies only exporter and iface (:371-372). talker, protocol, dscp, app, netclass, tcpflag, hour and dow are all dropped, while rangeQS (:386-393) carries them. compareDelta (:517-523) therefore divides filtered current bytes by unfiltered prior bytes.
The Prior legend is unconditional on compareEnabled (:710). mergedSeries (:507-515) keeps only current timestamps (series.map), so prior-only buckets are dropped. connectNulls (:752) hides gaps. Refs O:363/422/505 accurate; T:99 matches the 1.23.21 file.

**Reproduction.** Node sim of priorQS/mergedSeries with a +12h browser. 1.23.21: current (server) 16:19→17:19Z, prior (browser-derived) 03:19→04:19Z next day, ending 10.98 h after appliance now → empty prior. 1.23.22 (calibrated): prior ends 59.6 s before current start. Bucket alignment for custom spans: 10:00→17:03 (bucket 300 s) plotted 0/86 prior points; 10:00→17:05 plotted 86/86; 3-day 2d23h17m (bucket 1800 s) plotted 0/144.

**Corrections to the review.** 1) The clock part is mitigated in this worktree (1.23.22). The prior window now derives from the calibrated serverNow(), so the gap to the server-resolved current window is ~0-120 s (minute floor plus the 60 s nowMs interval), not 12 h. The two windows are still resolved from two different 'now's, but the 'Prior legend without prior series' symptom from clock skew only reproduces on 1.23.21.
2) The filter omission is fully confirmed and is the larger remaining problem. With any talker/protocol/app filter active, the ±% badge compares apples to oranges.
3) The doc misses a second cause of a missing prior trace. For custom ranges whose span is not a multiple of the bucket size, `p.ts + span` never equals a current bucket ts, so no prior point is ever plotted while the % badge is still shown (see extra defect).
4) I rate this P2 rather than P1: Compare is an opt-in overlay, not a health claim, though the delta badge is misleading.
5) Minor: priorQS changes every minute for presets (new query key, no keepPreviousData), so the prior line and delta blink out during each refetch.

### NF-03 — Loading and failures can appear as healthy or empty telemetry

**Verdict:** confirmed · review P1 → assessed P1

**Evidence.** None of the four pages checks `isLoading`, `isError` or `error` on its main query. Every one falls back to empty data:
- Anomalies N:52 `const findings = data.data?.findings || []`. N:103-110 then renders `grouped.size === 0`, which shows "No anomalies detected" / "All flow-derived security checks passed for this time window." N:100 shows the Total tile in emerald with a ShieldCheck icon.
- Capacity C:45 `const rows = capacity.data || []`. C:84-89 shows four tiles at 0, and C:97-100 shows "No interface traffic in this window."
- Saved Views V:57 `{views.data?.length ?? 0} saved views` and V:60-63 "No saved views yet".
- Overview O:501-503 and O:524-529 turn every query into `|| 0` / `|| []`. The KPIs render "0 B" and "0" (O:662-698). EmptyState (O:2661-2667) always says "No NetFlow records collected for this range." AlertsPanel shows "No active incidents." with a green 0 badge (O:1926-1933). DeviceStatusCard shows the RST ratio as "0.00%" in emerald (O:1983). TcpFlagsCard shows "Loading…" forever if its query fails, because it only checks `!data` (O:2316-2326).
- On the Overview, the only loading indicators are two spinners (O:723, O:2469). The only error handling anywhere is the section page (O:211, O:256).
- Backend makes it worse: `/anomalies` (A:2013-2192) runs five detectors in sequence. Any failing query becomes a whole-endpoint 500 via `_query_sync` (A:289-294), so the API never reports partial or failed checks. A window with zero flows returns exactly the same payload as a clean window.

**Reproduction.** I ran this worktree's dashboard with Vite from the scratch dir (proxying to the dev API on :8095) and drove it with Playwright route interception in isolated browser contexts:
- `/netflow/anomalies` returning 500, 403, or never answering: every case rendered "CRITICAL 0 WARNING 0 INFO 0 TOTAL 0 No anomalies detected All flow-derived security checks passed for this time window."
- `/netflow/capacity` returning 500 or pending: "Critical 0 / Warning 0 / Healthy 0 / Unknown speed 0 … No interface traffic in this window."
- `/netflow/saved-views` returning 500 or pending: "0 saved views No saved views yet."
- Overview with every `/netflow/**` call returning 500: "TOTAL TRAFFIC 0 B … No NetFlow records collected for this range … Alerts / Incidents 0 No active incidents." Not one word of fail/error/could not appears on the page. With every call pending the page looks the same, and TCP Flags says "Loading…".
- Anomalies with one successful load (one critical finding) and then 500s: after 36 s (two failed polls) the page still showed the old finding and gave no stale or error indication.
- Dev API `GET /netflow/anomalies?from=2026-09-20T00:00:00Z&to=2026-09-20T01:00:00Z` (a window with no flows) returned `{"total":0,"findings":[]}`. That is indistinguishable from a clean window.

**Corrections to the review.** The core claim is accurate, and the cited ranges O:412-500, N:51-113, C:45-110 and V:23-78 match the repo.
One refinement: last good data is already kept when a same-key poll fails (TanStack v5 default, confirmed empirically). The real defects are:
- no staleness indicator on retained data;
- the zeros and false all-clear shown on first load or whenever the range or filters change (a new query key with no placeholderData).
The doc omits several related paths:
- The section pages (O:211, O:251, O:1086) show "0 rows" and "No NetFlow records collected" while loading. On a refetch error they replace the last good table with the error (O:256-262).
- Capacity's "No interface traffic" also appears whenever exporters send ifIndex 0, because the query filters on `input_snmp != 0` / `output_snmp != 0` (A:1962, A:1968). That is an unsupported-telemetry state presented as an empty one.

### NF-04 — NetFlow queries do not enforce user visibility scope

**Verdict:** confirmed · review P1 → assessed P1

**Evidence.** netflow.py:21 imports only `get_current_user, require_operator_user`; nothing from app.core.scoping is imported, and every read route takes `user: User = Depends(get_current_user)` (683, 772, 931, 949, 966, 1153, 1217, 1258, 1301, 1402, 1537, 1647, 1686, 1740, 1811, 1931, 2019, 2224, 2266, 2376, 2489) without ever reading it. scope_params (502-529) only collects drill-down filters. _scope (577-673) adds no visibility predicate. Four endpoints skip _scope entirely: interfaces (1542 `_exporter_filter(exporter)`), capacity (1936), anomalies (2023) and list_exporter_devices (2383-2392, raw SQL). A fix made only inside _scope would therefore still leak through those four. The metadata resolvers _exporter_device_overrides (1431-1454) and _resolve_interface_names (1457-1508) return device hostnames for any exporter. set_exporter_device (2438-2442) looks up any device id with no scope check. Compare devices.py:490 and 528, which call `scoping.entity_visible(...)` and return 404 for out-of-scope devices. The doc's line refs (502, 677, 1795, 1925, 2014, 2374) match the repo.

**Reproduction.** Scratch stack (dev API :8095, scratch PG :55441, isolated CH db nfv_scope, all cleaned up afterwards). Fixture: exporter 10.200.0.1 maps to device nfv-rtr-a tagged ['nfv-team-a']. Exporter 10.200.0.2 maps to nfv-rtr-b tagged ['nfv-team-b'] and carries 450,000,000 bytes. User nfv_scoped is a viewer with scope_tags ['nfv-team-a'], and scoping.visible_tags returned ['nfv-team-a'] for it. Over HTTP, GET /devices/<nfv-rtr-b id> returned 404 and GET /devices/<nfv-rtr-a id> returned 200. The same user got 200 from /netflow/overview, /exporters, /exporter-devices, /capacity and /anomalies. Calling the handlers directly (CH SQL redirected zenplus.flow_ to nfv_scope.flow_): netflow_exporters returned both exporters, including 10.200.0.2 with 450,000,000 bytes. list_exporter_devices returned device_hostname 'nfv-rtr-b' and its device_id. netflow_forensics(exporter='10.200.0.2') returned raw rows (src 10.2.2.20, 9,000,000 bytes). top_talkers merged traffic from both exporters.

**Corrections to the review.** The line refs are accurate, but the doc understates the problem. (a) The same unscoped data also leaves through code outside /netflow. link_utilization.link_detail (link_utilization.py:820-850, NetFlow overlay at 1002-1094) has no visibility check anywhere in that file. The report section engine's traffic_* sections (report_sections.py:556-680) run unscoped because GET /reports/view/{key} (reports.py:392-438) never computes a scope. The home DashboardPage NetFlow widgets (DashboardPage.tsx:194-218) and the DeviceDetailPage NetFlow tab call the unscoped endpoints directly. (b) Module-level RBAC is also missing: `netflow.view` is never enforced (see extra defect), so a role without it reads everything. (c) Once scoping is keyed on exporter ownership, the mapping endpoints (PUT/DELETE exporter-devices) let the caller change who can see an exporter's traffic, so they need their own authorization.

### NF-05 — Saved-view writes lack role and ownership checks

**Verdict:** confirmed · review P1 → assessed P2

**Evidence.** netflow.py:2312-2330 list_saved_views: `select(NetflowSavedView)` with no owner filter. 2332-2350 create uses only `get_current_user` and stores owner_id. 2353-2370 delete: `select(...).where(NetflowSavedView.id == view_id)` then `db.delete(view)`, with no owner_id or permission check. The API has no PATCH/PUT route. SavedViewOut (2207-2214) omits owner_id. migrate-052 has no visibility/sharing column. None of the saved-view handlers calls write_audit_log, unlike set_exporter_device (2451-2456). The doc's line refs 2313-2369 are correct.

**Reproduction.** Dev API :8095 with scratch users (removed afterwards). nfv_viewer2 (viewer) POSTed a view {src 10.9.9.9, dst_port 443, sort bytes asc}. nfv_scoped (a different viewer) GET /netflow/saved-views and received it, full query included. nfv_scoped then sent DELETE /netflow/saved-views/<viewer2's id> and got 200 {"deleted": ...}. nfv_noflow_u, whose custom role has only dashboard.view and devices.view (no netflow.view), created views named '   ' and '' (both 200). Afterwards `select count(*) from audit_logs where resource_type ilike '%saved%'` returned 0.

**Corrections to the review.** The facts are accurate, but I rate this P2, not P1. The affected objects are convenience queries: the harm is untraceable deletion or vandalism plus disclosure of colleagues' investigation targets. No telemetry is exposed and no privilege is gained beyond what NF-04 already covers. Listing every view to everyone is the stated product intent ('Reusable forensic queries shared across your team.', NetflowSavedViews.tsx:52). The defect is that there is no private option and no ownership or permission enforcement, not the listing itself. The doc understates several related issues found alongside it: no netflow.view gate (a role without it can create and delete), no audit trail, a 500 on a non-UUID id, and an unvalidated, unbounded payload (see extra defects).

### NF-06 — Collector discards a failed database batch

**Verdict:** confirmed · review P1 → assessed P1

**Evidence.** poller/cmd/netflow-collector/main.go:235-243: `flush := func() { ... if err := c.insert(ctx, batch); err != nil { fmt.Printf("ERROR: netflow insert failed: %v\n", err) }; batch = batch[:0] }`. The failed batch is never retried. main.go:164-176 enqueue() does `c.ingested.Add(1)` when a record is admitted to the channel, before anything is persisted. main.go:369-378 /health always returns `"status":"ok"` and has no insert-failure, inserted-record, spool or template-wait counters. main.go:206-208 and 221-223: data sets that arrive before their template (dropped records) are only logged at INFO and never counted. Nothing in server/ or dashboard/ reads the health endpoint. The only references to the collector are the support bundle's service lists (support/collectors/logs.py:24, inventory.py:25). The doc's line refs (164-257) are accurate.

**Reproduction.** I built the worktree collector (copied to scratch/verify/collector) and ran it against scratch ClickHouse 24.10 in its own DB nfv_collector, through a TCP proxy I could kill. (1) Sent 100 v5 flows: 100 stored. (2) Killed the proxy, sent 200 flows, restored the proxy, sent 50 more: 150 of 350 stored. /health still said {"status":"ok","records_ingested":350, all drop counters 0}. The log had one line: "ERROR: netflow insert failed: dial tcp 127.0.0.1:19901: connect: connection refused". (3) With NETFLOW_FLUSH_SECONDS=30 I sent 60 flows and then SIGTERM: 0 of 60 stored, log "ERROR: netflow insert failed: context canceled" (see extra defect). (4) Started the collector with ClickHouse unreachable: it exits 1 on the ping and UDP 2055 is never bound. (5) Checked the dedup design on CH 24.10: with insert_deduplication_token and non_replicated_deduplication_window on the raw table only, a retried token deduplicates raw rows but the MV target still double-counts (raw 4 rows, MV 6). With the window setting also on the MV target plus deduplicate_blocks_in_dependent_materialized_views=1, both raw and MV count once (4/4). I also saw a genuinely ambiguous insert: when an MV push fails, the raw row is already committed but the client gets an error (raw2 rows=1 after CANNOT_CONVERT_TYPE), so a blind retry would duplicate it.

**Corrections to the review.** The doc is accurate but understates the problem. (a) A normal graceful stop also loses data, not only an outage. The shutdown flush reuses the cancelled context, so every systemctl restart or OTA stop_services (scripts/build-release.py:931) drops the in-memory batch plus up to 4000 queued records. (b) If ClickHouse is unavailable at startup, main.go:45-51 exits via fatalf, so the collector does not listen at all and systemd retries every 10s. (c) /health reports status ok unconditionally and nothing in API/UI consumes it, so outage visibility is zero, not just an overstated counter. (d) Records that are dropped while waiting for a template are also uncounted. "No outage induced" in the doc is now superseded: loss was reproduced empirically.

### NF-07 — Missing TCP flags are treated as real zeros

**Verdict:** confirmed · review P1 → assessed P1

**Evidence.** Collector:
- poller/internal/netflow/v5.go:36: Record.TCPFlags is a plain uint8 with no presence marker.
- v9.go:424-425 (`case 6: r.TCPFlags = uint8(...)`) is the only assignment, so an absent field stays 0.
- The same decoder is used for IPFIX via ipfix.go:206.
API:
- server/app/api/v1/netflow.py:1681-1731 /tcp-flags counts `tcp_flags = 0` as no_flags and returns only counts, with no availability or coverage field.
- netflow.py:2014-2105: the syn_scan (2040-2041) and rst_flood (2069-2075) detectors silently find nothing when flags are absent, and the response has no per-check status.
- netflow.py:1316/1334/1359: device-status rst_ratio_pct also reports 0%.
- netflow.py:654-660: the `no_flags` drill filter conflates absent flags with explicit zero.
- migrate-072 keys flow_qos_1h on tcp_flags with no presence column.
UI:
- dashboard/src/pages/NetflowPage.tsx:2338: tile 'No flags (anomaly)' turns rose above 5%.
- NetflowPage.tsx:1680: describeTcpFlags(0) returns 'None'.
- NetflowAnomalies.tsx:107-108: 'No anomalies detected' / 'All flow-derived security checks passed for this time window.'
The doc's refs (API 1681, 2014; v9 292/assignV9Field) are accurate.

**Reproduction.** Go test in scratch against the worktree decoder. A v9 template without field 6 gives TCPFlags=0. A template with an explicit TCP_FLAGS=0 also gives 0. The two cases are indistinguishable in the Record and in ClickHouse. An IPFIX 2-byte tcpControlBits 0x0112 gives 0x12, so the standard bits are kept correctly. Dev API GET /api/v1/netflow/tcp-flags?hours=1 returns only {total_tcp, syn_only, ack_only, rst, fin, psh, urg, no_flags}, with no availability field.

**Corrections to the review.** The claim is correct and the line refs are accurate. Two understatements: the same false-healthy output appears in /device-status rst_ratio_pct (the 'RST ratio' proxy shows 0%) and in the tcpflag=no_flags drill filter; and the flow_qos_1h rollup cannot represent presence, so any fix needs a rollup change as well as raw. Root cause for the reviewed appliance is still unproven. Since v5 always carries flags, all-zero flags imply either a v9/IPFIX template without IE 6 (typical for firewall exporters) or an exporter that really sends zeros.

### NF-08 — Raw-flow navigation loses dashboard filters

**Verdict:** confirmed · review P1 → assessed P1

**Evidence.** Forensics state is seeded only from src/dst/proto/src_port/dst_port/min_bytes/dscp/exporter (NetflowForensics.tsx:53-60). `seeded` checks only src|dst|proto|exporter (F:63). The API query string is built from local state only (F:68-80), so the URL params talker/protocol/iface/app/netclass/tcpflag/hour/dow are neither shown nor sent. Talker/endpoint link: `forensicsTo={withParams('/netflow/forensics', { talker: row.ip })}` (NetflowPage.tsx:1278). withParams copies the whole current query and forces range=1h when it is absent (O:1076-1084). Conversation link: `withParams('/netflow/forensics', { src, dst, proto: String(row.protocol), dst_port })` (O:1134) uses `proto`, while dashboard filters use `protocol` (O:301-308). The footer quick links are bare `/netflow/forensics`, `/netflow/anomalies` and `/netflow/capacity` (O:876-894). They drop range and every filter in the overview, and they also drop exporter in the device view (NetflowDevicePage renders NetflowPage exporter=ip, O:154-157). Backend: /netflow/forensics already takes `scope: dict = Depends(scope_params)` (netflow.py:1810), so it already accepts talker, protocol, iface, app, netclass, tcpflag, hour, dow and group. `_scope` already defines talker as src OR dst (netflow.py:612-613). The defect is almost entirely in the UI.

**Reproduction.** Playwright against the worktree vite dev server (:5199 → dev API :8095): opening /netflow/forensics?range=24h&talker=10.0.0.5&protocol=6&iface=3 left Source IP empty and sent 0 requests. Clicking Search sent `hours=24&limit=500&sort=timestamp&order=desc`, with talker, protocol and iface all dropped. Direct call of netflow_forensics() against a private ClickHouse fixture (nfv_drill.flow_records): the UI-equivalent query returned 500 rows from 226 distinct sources. Backend talker=10.0.0.5 returned 350 rows, 50 of them destination-only. A naive talker→src mapping returned only 300. talker=10.0.0.0/24 returned 0 rows (string equality). Scripts: scratchpad/verify/drill/{fixture.sh,forensics_probe.py,exporter_probe.py,ui_probe.mjs}.

**Corrections to the review.** The doc is accurate on substance but slightly off in places. (1) The talker link is `?range=1h&talker=…` plus every other current page param, not bare `?talker=`. (2) Some context does survive: `range` and custom from/to are carried and read by useTimeRange, and `dscp` and `exporter` are parsed by Forensics (F:59-60). Only the footer quick links lose range and exporter, and they lose them on the overview too, not only in exporter detail. (3) The live effect is worse than 'required Search'. Clicking Search then runs an unfiltered query for the whole window while the URL still says talker=X. (4) The fix is a UI change: the backend already implements talker as src-or-dst and already accepts every drill-down key. The one backend gap is that `_scope` talker is string equality, so a CIDR or malformed talker silently matches nothing. (5) Line refs: the talker forensics link is O:1278 (1277 is the 'Filter dashboard' link). The Forensics parsing is F:53-63 and query building is F:68-80. (6) A conversation drill-down also silently drops iface, tcpflag, app, netclass, hour, dow and talker, so the raw rows do not reconcile with the filtered conversation totals. (7) Protocol values outside the six hard-coded options are applied but not shown: a proto=89 link makes the select display 'Any' while the query filters proto=89 (listed as an extra defect).

### NF-09 — Anomaly Investigate does not preserve finding evidence

**Verdict:** confirmed · review P1 → assessed P1

**Evidence.** Investigate link: `to={`/netflow/forensics?src=${encodeURIComponent(f.src)}`}` (NetflowAnomalies.tsx:153-160). It carries no range, from/to, protocol, dst, ports, tcpflag or exporter. The anomalies page's own `range` is not copied, so Forensics falls back to the 1h default (TimeRangePicker.tsx useTimeRange idx 0). The API response has no data to build precise evidence. `netflow_anomalies` returns only `{total, findings, generated_at}` (netflow.py:2188-2192), with no resolved window bounds. Findings carry only src/dst/metric (e.g. icmp_flood at 2145-2154). The sensitive_egress query groups by src,dst and filters `dst_port IN (22,23,3389,445,1433,3306,5432)` (2094-2111), but the finding does not return which ports matched (2118-2128). Detector predicates are not exposed: syn_scan = proto 6 + SYN set + ACK clear (2040-2042), which is exactly `_scope` tcpflag 'syn_only'. The finding id is `icmp-flood-<src>` (2147), with no window, so it is not unique across windows.

**Reproduction.** Fixture: 10.12.50.81 has 20 ICMP + 30 TCP flows 10h ago and 5 TCP flows in the last hour. The Investigate-equivalent query (1h, src only) returned 5 rows, all TCP. The evidence-preserving query (24h, proto=1) returned 20 rows, all ICMP. Playwright confirmed that /netflow/anomalies?range=24h requests `hours=24`, while the link target carries only src.

**Corrections to the review.** Correct as stated. N:153-177 in the doc: the link is N:153-160 (the remaining lines are closing markup). The root cause is partly backend: without a returned window and ports, the UI cannot freeze the evidence even if it wanted to. A preset relative window re-resolves on the server at each request, and the page refetches every 30s (N:50). Related extra: NetworkSecurityDashboard.tsx:416 and :495 link to /netflow/anomalies without the dashboard range, so its findings count (range.hours) disagrees with the Anomalies page (1h default).

### NF-10 — Forensics silently stops at 500 records

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** The UI hard-codes `limit: '500'` (NetflowForensics.tsx:69). The header shows only `${flows.data.length} rows · ${range.label}` (F:195), with no total, truncation notice or next page. Export CSV serializes `flows.data` only (F:89-106). API: `limit ≤ 2000` (netflow.py:1797). There is no cursor or offset parameter, and `ORDER BY {sort_col} {order}` has no tie-breaker (1864), so even OFFSET paging would be unstable. The server CSV path (`format=csv`, 1809, 1898-1913) exists but shares the 2000 cap, the UI does not use it, and it writes no audit record. Platform convention for complete exports (alerts.py:443-487): server-side, EXPORT_CAP 50,000 (alert_service.py:38), `X-Total-Count`/`X-Exported-Rows` headers, `write_audit_log(action='alert.export')`, and `_csv_safe` (alerts.py:490).

**Reproduction.** Fixture with 1,205 matching rows in 1h: netflow_forensics(limit=500) returned a bare list of 500 with no total. On 120k rows with many bytes=100 ties spread across parts, `ORDER BY bytes ASC LIMIT 500` returned different row sets on repeat (2 distinct result hashes in 12 runs). Page 1 (OFFSET 0) and page 2 (OFFSET 500) shared rows in 29 of 30 runs (scratchpad/verify/drill/offset_probe.py).

**Corrections to the review.** Accurate. Line refs are slightly off: the CSV export is F:89-106 (F:108-117 is saveView) and the row-count label is F:195. The doc does not note the missing tie-breaker, which makes any naive OFFSET paging produce duplicates. That is a design constraint for the fix.

### NF-11 — Search button does not commit a query

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** Draft input state feeds `qs` directly (NetflowForensics.tsx:53-80), and `queryKey: ['netflow','forensics', qs, armed], enabled: armed` (F:82-87). Once armed, every keystroke is a new query. The Search button only calls `setArmed(true)` (F:168-174), a no-op when already armed. `reset()` clears state and sets armed=false but never touches the URL (F:119-121), and the useState initialisers re-read the URL on reload (F:53-60). The URL is never written with applied filters, so reload, Back and share cannot reproduce a search. The inputs are not inside a <form>, so Enter does nothing (F:157-166). QueryClient `retry: 1` (main.tsx:14) re-sends failed requests, which explains the 'eventually' error. Save view posts the draft state, not the query that produced the visible results (F:108-116).

**Reproduction.** Playwright (vite :5199 → dev API :8095):
- After one Search, typing '10.1.2.3' fired 8 requests (src=1, 10, 10., 10.1, … 10.1.2.3), 7 of them 400s.
- Typing 'invalid-ip' with no Search click fired 11 requests (10 keystrokes plus 1 retry) and displayed 'Invalid src CIDR: invalid-ip'.
- Search with unchanged inputs fired 0 requests.
- /netflow/forensics?src=10.0.0.5 → Reset → the URL still had src=10.0.0.5; after reload the field was repopulated and the query auto-ran (1 request).
- Typing 'a' in Source port produced a 422 and the ErrorBoundary 'This page failed to render: Objects are not valid as a React child'.

**Corrections to the review.** Accurate, and in one respect understated. Clicking Search with unchanged inputs never refreshes (0 requests), not 'not necessarily'. Line refs: F:135-138 is the page title. Reset is F:119-121 and the Search button is F:168-174. Related consequences the doc does not list: typing a non-numeric port crashes the page (extra defect), and Enter does not submit.

### NF-12 — Saved views cannot reproduce their saved query

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** NetflowForensics.tsx:52-60 initialises the filter state from URL params only through useState initialisers, so it is never re-synced when the URL changes. :61-62 hardcodes `useState('timestamp')` and `useState('desc')` and never reads the URL. :63 `seeded = !!(src||dst||proto||exporter)` excludes src_port, dst_port, min_bytes and dscp. :111-115 saves the query {src,dst,proto,src_port,dst_port,min_bytes,dscp,exporter,sort,order} with no range, from or to. NetflowSavedViews.tsx:29-35 buildLink copies only the eight filter keys, dropping sort and order, and tests them by truthiness `if (q[k])`. TimeRangePicker.tsx:97-171: time lives in the URL as `range`, `from` and `to`, so a link could carry it but never does.

**Reproduction.** A Node replay of the exact save payload, buildLink and mount logic (scratchpad/verify/scope/roundtrip.mjs) gave these results.
- A port-only view (dst_port=443, sort bytes asc, 7d) produced link `/netflow/forensics?dst_port=443`. On mount it did not auto-run, sort came back as timestamp/desc and the range as the 1h default.
- A DSCP=0-only view produced `?dscp=0`, dscp '0' was kept, and it did not auto-run.
- A min_bytes-only view did not auto-run.
- A view with src and sort packets asc auto-ran but with sort timestamp/desc.
- A view created through the API with numeric {dscp:0, dst_port:0} produced `/netflow/forensics` with both values dropped.

**Corrections to the review.** The doc's F:124-132 points at JSX header markup; the save code is F:108-117. buildLink is at V:29-35, not V:36-43. One point is overstated: DSCP=0 saved from the UI is stored as the string "0", which is truthy in JS, so the link keeps `?dscp=0`. Truthiness only drops zero for numeric values supplied through the raw API. The real DSCP problem is that DSCP-only views do not auto-run. The doc also misses one effect: the time picker rewrites the URL with setSearchParams(replace) and keeps the originally seeded filter params. After a user edits a filter and changes the range, a reload or shared link restores the old seeded filter, not the one on screen.

### NF-13 — Capacity statistics use incomplete and unequal slots

**Verdict:** confirmed · review P1 → assessed P1

**Evidence.** netflow.py:1938 `bucket = 300`; :1953 `toStartOfInterval(timestamp, INTERVAL {bucket} SECOND) AS slot`; :1956 `sum(byte_count) * 8.0 / {bucket} AS slot_bps` divides every slot by 300 s, including the partial head/tail slots; :1961/:1967 `timestamp BETWEEN %(start)s AND %(end)s` is inclusive at both ends; :1944-1946 `quantile(0.95)(slot_bps)`, `avg(slot_bps)`, `max(slot_bps)` run only over slots that have rows. Nothing zero-fills idle slots or tells an idle slot apart from a collection gap. _resolve_window (:54-56) makes a relative window that ends at now(), so a 1h preset spans 13 slots, 2 of them partial. UI claims: NetflowCapacity.tsx:72 "billing & growth view" and :94 "Industry-standard 95p over 5-minute slots; billing benchmarks usually quote this number". The doc's line refs (API:1937-1979) are correct. There are no tests for /capacity (grep of server/tests finds none).

**Reproduction.** Scratch ClickHouse database nfv_capacity (DDL copied from migrate-20260506), fixture at scratchpad/verify/capacity/fixtures.sql. The REAL netflow_capacity() was called via run_capacity.py, which patches only the CH client/table name and _resolve_interface_names. F1: 40 Mbps constant, window 11:08:51-12:08:51. Result: avg_bps 36,933,333 (=3601×5e6×8/3900), p95 40,000,000, total_bytes 18,005,000,000 (3601 records, inclusive BETWEEN). Aligned 11:00:00-12:00:00 gave the same 36.93 Mbps (13 slots). F4: 7-day window, ifIndex 9 busy in 10 of 2016 slots at 100 Mbps, exporter alive in every slot. API: avg=p95=max=100,000,000. Zero-filled prototype: avg 496,032, p95 0, max 100,000,000. F5 (12 slots, 1..12 Mbps): API p95 11,450,000. F6 (8640 distinct slots): quantile(0.95) varied across 10 runs from 8,206,450 to 8,221,450; nearest-rank 8,208,000 was stable. Live dev API :8095 /netflow/capacity?hours=1: avg/(bytes×8/3600) = 1.2000 for all top-5 rows (read-only check: 10 occupied slots, simulator stopped 17:19).

**Corrections to the review.** The doc is right but understates the problem. (1) Its figures agree with each other: 39.09 = 42.35 × 3600/3900, so all 13 slots were occupied and the average was total/(13×300). A 1h relative window is therefore always about 7.7% low for steady traffic. The same edge effect is negligible at 7d (1 slot in 2017). (2) Omitting empty slots is the much larger error, and it hits p95 and avg alike, not just avg. With 10 busy slots of 100 Mbps in a 7-day window, the API reports avg = p95 = 100 Mbps; the zero-filled truth is avg 0.496 Mbps and p95 0. That is a 200x overstatement. The effect also shows live on the dev API: after the simulator stopped, the 1h avg_bps is exactly 1.2000x total_bytes×8/3600 for every interface (10 of 12 slots occupied), and the API gives no hint of the gap. (3) quantile() is ClickHouse's reservoir-sampled estimator. It is NON-DETERMINISTIC once a window has more than 8192 slots, which includes the 1M preset (8640 slots). In the fixture the same query returned 8,206,450 / 8,207,450 / 8,209,450 / 8,214,450 / 8,221,450 bps between runs. Burstable-billing nearest rank (discard the top 5%) is 8,208,000, and quantileExact gives 8,209,000. Below 8192 slots it interpolates linearly: 12 slots of 1..12 Mbps give 11.45 Mbps, while nearest-rank billing gives 12 Mbps. Because the result is ORDER BY p95 LIMIT 50, Top-N membership can change on every 60 s refetch. (4) BETWEEN is inclusive at both ends, so adjacent windows double-count boundary records, and even an …

### NF-14 — Capacity combines directions against one link speed

**Verdict:** confirmed · review P1 → assessed P1

**Evidence.** netflow.py:1957-1969: an inner UNION ALL of `input_snmp AS ifindex` and `output_snmp AS ifindex` over the same flow_records, grouped at :1970 by (exporter_ip, ifindex, slot) only, with no direction column. :2000 `utilization_p95_pct = p95 / speed * 100` divides that combined rate by one line rate. The platform's own SNMP module does it differently: link_utilization.py:75-78 `_util_pct` uses `max(in_bps, out_bps) / speed`, and :476 uses `_effective_speed`, so NetFlow capacity contradicts the product's existing utilization convention. Line refs API:1956-2008 are accurate (the function ends at 2005).

**Reproduction.** Fixture F2 (exporter 10.0.0.2, 1 rec/s each way, 75e6 B/s = 600 Mbps per direction between ifIndex 7 and 8, 12:00-12:59:59), through the real netflow_capacity() with if_speed 1e9 injected. Both rows: p95_bps 1,200,000,000, utilization_p95_pct 120.0, utilization_max_pct 120.0. F3 (in=out=5, 10 Mbps) gives p95 20,000,000. The prototype per-direction SQL gives ifIndex7 in/out 600,000,000 each (60%) and ifIndex5 in 10,000,000 / out 10,000,000.

**Corrections to the review.** The core claim is reproduced exactly: a 600/600 Mbps full-duplex fixture on a 1 Gbps interface reports utilization_p95_pct 120.0 on both ifIndexes. The sub-claim that 'A flow with the same input/output index can be counted twice' is only wrong under the combined metric. A hairpin flow (in=out=5) really does consume RX and TX, and once directions are separated it is correctly counted once per direction (in 10 Mbps / out 10 Mbps). The real double-count risk the doc misses is exporters that emit both ingress- and egress-monitored records for the same packets. The collector never parses flowDirection (IE 61; v9.go assignV9Field :394-460 has no case 61, and IPFIX reuses assignV9Field at ipfix.go:206), so such duplicates can't be told apart. total_bytes is also in+out combined, and it is returned but not shown.

### NF-15 — Headroom shows utilization instead

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** NetflowCapacity.tsx:112 `<Th>Headroom</Th>`; :119 `const widthPct = Math.min(100, Math.max(2, util ?? 0))`; :137-148 the bar `style={{ width: `${widthPct}%` }}`, coloured by util thresholds (>=70 rose, >=40 amber). The bar length is therefore utilization, so a fuller bar means less headroom. :146 prints 'no ifSpeed' for unknown. Nothing anywhere computes remaining capacity in bps or %.

**Reproduction.** Code reading plus the NF-14 fixture, where the API returns utilization_p95_pct 120.0. Per :119 widthPct = 100, i.e. a full rose bar under 'Headroom', while the true headroom is 0 or the value is a data problem (combined directions).

**Corrections to the review.** The doc's line refs are off: the header is :112, the clamp :119 and the bar :137-148, not C:135-159. It slightly overstates the masking. An over-capacity value still appears numerically in the adjacent 'Util (95p)' column (:134-136, e.g. '120.00%' in rose). Only the bar saturates at 100%, and 120% gets the same rose tone as 70%, with no over-capacity flag or explanation. The 2% minimum draws a green sliver for 0% utilization. Populated behaviour follows from the code; the live reviewer only saw unknown rows.

### NF-16 — Capacity cannot guide users out of unknown speed

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** NetflowCapacity.tsx has no action, link or explanation for unknown speed. Its only link is the breadcrumb (:61). Unknown rows show device '—' (:127), capacity '—' (:130) and 'no ifSpeed' (:146), and the summary card is just 'Unknown speed' (:88). The mapping dialog exists only as a private `function ExporterDeviceDialog` inside NetflowPage.tsx:914-1010 and is not exported. API netflow.py:1457-1508 `_resolve_interface_names` returns metadata only `if meta:` (:1505-1507). When the exporter matches a device but its ifIndex is not in device_interfaces, the row gets NO device_hostname either, so 'Device —' can't be told apart from an unmapped exporter. :1484-1491 selects only DeviceInterface.if_speed, and :1984 `speed = meta.get('if_speed') or 0` returns 0, not null (the dev API emits "if_speed":0 while the UI type says number|null).

**Reproduction.** Rolled-back transaction on scratch PG :55441; zero rows persisted, verified afterwards. Device 198.51.100.77 with interfaces: 16 (if_speed NULL, configured_speed_bps 100 Mbps), 17 (if_speed 0), 18 (SNMP 1 Gbps, override 50 Mbps). Real _resolve_interface_names gave:
- idx16 → if_speed None, although the override is set.
- idx18 → if_speed 1,000,000,000, ignoring the 50 Mbps circuit.
- idx99 (device matched, ifIndex not inventoried) → None, hostname lost.
- unmapped 203.0.113.9 → None, the same as idx99.
The dev API /netflow/capacity returns "if_speed":0,"device_hostname":null for every unresolved row.

**Corrections to the review.** Accurate in substance; line refs roughly right (API:1457 is the resolver def). The doc's suggestion to 'allow an audited circuit-capacity override' misses that one already exists platform-wide: device_interfaces.configured_speed_bps (migrate-033), set through PATCH /devices/{id}/interfaces/{if_index} and POST /devices/{id}/interfaces/bulk-speed (devices.py:700-760) and used by Link Utilization (_effective_speed, link_utilization.py:69-73) and alerting (network_alert_service.py:319 COALESCE(configured_speed_bps, if_speed)). NetFlow capacity ignores it (see extra defect), so an operator following the platform's supported path still sees 'no ifSpeed'. Those override endpoints also write no audit record, so the 'audited' part is genuinely missing.

### NF-17 — Long-window rollup edges include extra history

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** netflow.py:534 ROLLUP_MIN_SECONDS = 72h. _floor_hour (:554-556) floors only the start. Rollup queries use `timestamp BETWEEN %(start)s AND %(end)s` on hour-start buckets (:692/704, :736-741, :789-793, :845-848, :995-1009, :1156-1160, :1220-1223, :1265-1268), so the bucket starting at or before `end` covers up to 59m59s past it. The QoS rollup does the same (:1306-1322 device-status, :1653-1656 dscp, :1692-1696 tcp-flags). Eligibility flips to raw with exact bounds when iface/dscp/tcp_flag is set (:537-551) or talker/app/netclass/group is set for QoS (:562-574). The rollup's own backfill uses half-open hours (netflow_rollup.py:86,109), so only the API is wrong. Refs API:537-575 and 687-717 are accurate.

**Reproduction.** Scratch db nfv_clock: 1 flow/min of 1000 B from 2026-09-15 to 09-25. Called the real netflow_overview/netflow_timeseries/netflow_protocols (db name rewritten). All counts are flows.
| Custom window | Exact half-open raw | overview, no filter (rollup) | overview dscp=0 (matches every row, raw) | timeseries |
|---|---|---|---|---|
| 72h, 10:30→10:30 | 4320 | 4380 | 4321 | 4321 |
| 72h, 00:00→00:00 | 4320 | 4380 | 4321 | — |
| 8d | 11535 | 11580 | 11536 | — |
For the 72h window, protocols (rollup) also returned 4380. The prototype hybrid SQL (rollup for full interior hours UNION ALL raw for the head/tail) returned exactly the half-open raw result for all three windows: 4320/4320/11535 flows, identical uniqExact(src_addr) and max(received_at).

**Corrections to the review.** 1) Understated. Even hour-ALIGNED custom windows over-count: [00:00, +72h) includes the whole bucket that starts exactly at `end`, one extra hour. Relative ≥72h windows (7d and 1M presets) widen only the start, by up to 59m59s.
2) The inconsistency needs no filter change. For 72–168h windows, /timeseries uses a 1800 s bucket (:776), fails `bucket % 3600 == 0` (:778) and scans raw exactly, while /overview, /protocols, /top-* use the widened rollup. The same unfiltered page shows totals that don't match its chart.
3) The raw path itself is inclusive at both ends (BETWEEN), so it counts one extra boundary instant versus half-open.
4) Magnitude: at most ~2 h extra, i.e. ≤2.8% of a 72h window and ≤0.3% of 30 days. It is larger if edges contain peaks.

### NF-18 — Average flow duration changes units and is capped

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** server/app/api/v1/netflow.py:1358 `latency_ms = round(min(max(avg_dur_ms / 1000.0 if avg_dur_ms > 1000 else avg_dur_ms, 0), 250), 1)`, then returned as `flow_duration_ms` AND the deprecated `latency_ms` alias (1381, 1386). UI reads the alias: NetflowPage.tsx:1972 `const flowDuration = status?.latency_ms ?? 0` and renders `${flowDuration} ms` (1982). The DeviceStatus TS type (NetflowPage.tsx:107) does not even declare flow_duration_ms. The avg itself is computed three different ways: raw device-status 1332 `avg(toUInt64(last_switched_ms - first_switched_ms))` (UInt64-UInt64 is Int64 in CH, so toUInt64 wraps negatives to ~1.8e19); rollup device-status 1314 `sum(duration_ms)/sum(flows)` where migrate-072 / netflow_rollup.py:106 store signed `toInt64(last)-toInt64(first)`; top-conversations 1035 signed avg shown in ConversationDetailPanel as plain ms (NetflowPage.tsx:1420,1449) — so on the same page the conversation detail says ~19,600 ms while Exporter Health says 19.6 ms. Forensics 1861 returns `(last_switched_ms - first_switched_ms)` unguarded (negative values reach UI/CSV). Collector: v9.go:333-347 applyFlowTimes picks first and last independently, so a template with only flowEnd (153) stores first=0, and a flowEnd-abs + FIRST_SWITCHED-rel mix stores different time domains; uint32 sysUptime wrap is not handled; missing times are stored as 0/0, indistinguishable from genuine 0 ms single-packet flows. IPFIX IEs 154-162 (µs/ns/delta/duration) are not handled (v9.go:398-459).

**Reproduction.** python fixture of the API formula: 0→0, 120→120, 500→250, 999→250, 1000→250, 1001→1.0, 1500→1.5, 19624.77→19.6, 60000→60.0, 1.8e6→250 (all labelled 'ms'). Dev API :8095 /device-status?hours=24 on seeded data returned flow_duration_ms 19.6 while ClickHouse `avg(...)` over the same rows = 19624.77 ms. Scratch CH fixture (1000 flows, avg 48200 ms) + one wrapped row: raw-path avg 1.84e16 → card '250 ms'; rollup-path avg -4,242,522 → card '0 ms'. `SELECT toUInt64(5)-toUInt64(10)` = Int64 -5; toUInt64 of it = 18446744073709551611. Go scratch test of applyFlowTimes: only-153 → first=0,last=1790769600000 (diff 56 years); 153+22 → diff 1.79e12; wrap → -4294965000; none → 0/0.

**Corrections to the review.** Doc is accurate but understated. (1) The mapping is non-monotonic, not just a unit flip: 500 ms and 999 ms both display '250 ms' (cap), 1000 ms displays '250 ms', 1001 ms displays '1.0 ms'; so the doc's own acceptance fixture (500 ms) currently renders as 250 ms. Because realistic averages are bounded by exporter active timeouts (seconds to minutes), the /1000 branch almost always fires and the card effectively shows seconds labelled 'ms'. (2) Raw (<72h) and rollup (>=72h) paths disagree on wrapped/missing values: one wrapped flow among 1000 makes the 1h/24h card show '250 ms' and the 7d/1M card '0 ms' for identical data. (3) Line refs: formula is at API:1358 (doc 1355–1387 range OK); UI consumer is O:1972, not 1969.

### NF-19 — TCP RST ratio uses all protocols as its denominator

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** netflow.py:1333 raw `count() AS total_flows` (all protocols) and 1335 `countIf(bitAnd(toUInt8(tcp_flags), 4) = 4) AS rst_flows` (no protocol=6 guard); rollup 1311/1315 `sum(flows)` / `sumIf(flows, bitAnd(toUInt8(tcp_flags),4)=4)`; 1360 `loss_pct = round((rst_flows / flows * 100) if flows else 0, 2)`. UI: NetflowPage.tsx:1884-1888 alert 'High TCP RST Ratio … % of TCP flows carried RST' (threshold >=5) and 1973/1983 'TCP RST Ratio' tile coloured emerald below 5%. Separate /tcp-flags (1680-1731) filters `protocol = 6` and returns total_tcp; its card (NetflowPage.tsx:2327-2361) computes RST % over total_tcp — two different RST percentages on the same page. No capability signal: TCP records with tcp_flags=0 (exporter omits the field, see NF-07) sit in the denominator, so a flag-less exporter shows a confident '0.00%' in green. device-status honours the full scope incl. `tcp_flag` and `protocol` (1304-1323 via scope_params), unlike /tcp-flags which strips both (1690).

**Reproduction.** Scratch CH fixture (100 TCP RST, 100 TCP non-RST, 800 UDP) running the device-status raw SQL: api_rst_pct=10, TCP-only=50 — exactly the doc's acceptance case. Dev API :8095 on seeded data: /device-status?hours=24 rst_ratio_pct 0.05 (20/41439) vs /tcp-flags rst 20 of total_tcp 26083 = 0.077%; ?tcpflag=rst → rst_ratio_pct 100.0 (alert would fire); ?protocol=17 → 0.0; ?protocol=6 → 0.08; tcp-flags no_flags=4160 (16% of TCP) still counted in the denominator.

**Corrections to the review.** Accurate. Additional failure modes the doc does not state: (a) selecting the RST tile (tcpflag=rst) makes device-status report 100% and fires the 'High TCP RST Ratio' warning purely as a filter artifact; (b) a protocol=17 filter reports a 'TCP RST ratio' of 0.00% over UDP flows. Line ref for the UI alert is O:1884–1888 (doc says 1882).

### NF-20 — Service classification mislabels response traffic

**Verdict:** confirmed · review P2 → assessed P1

**Evidence.** Classification uses only dst_port, plus the IP protocol for portless protocols. src_port is never consulted: it is only displayed and filtered on (netflow.py:981,1025,1112,1835).
- netflow.py:282-286 `_application_for_flow(port, proto)`. It is called with dst_port at 1110 (conversations) and in /applications at 1284 (`GROUP BY dst_port, protocol`).
- netflow.py:307-318 `_port_name`: port 0 returns "none"; otherwise COMMON_PORTS is checked before the protocol, so the label ignores the protocol (UDP/22 -> "SSH"). Unknown ports become "TCP/<port>".
- Forensics row service: netflow.py:1896 `"service": _port_name(r[5], r[6])`, which is dst_port.
- App drill-down filter: netflow.py:627 `dst_port IN (...)`.
- Conversations section table: NetflowPage.tsx:1097 header `<Th>Application</Th>`, but cell 1114 renders `{row.service}`. The detail panel correctly uses row.application (1415).
- ESP: the API returns service "none" and application "VPN / Tunneling" (1109-1110).
- The detail panel says "Inferred from the destination service port." for every non-Other bucket, including protocol-derived ESP/ICMP (1461-1463). ApplicationDetailPanel:1359 says the same for the VPN bucket.
- The UI fallback `inferApplication` (1696-1707) is a divergent copy of the server map: 8000 -> Streaming vs Web on the server; 5060 -> Streaming vs VoIP.

**Reproduction.** Built an isolated ClickHouse DB nfv_nf20 (real flow_records DDL) with fixtures:
- 20 HTTPS request halves (10.0.0.5:5000x -> 93.184.216.34:443, 5 KB each)
- 20 response halves (443 -> 5000x, 5 MB each)
- 1 response to PAT port 3306 (10 MB)
- 5 ESP flows
- DNS request and response
- v5-style ICMP 2048/771
The real endpoint functions were called with _query_sync patched only to swap the table name (harness in scratchpad/verify/nf20-22/harness.py).
/applications result:
- VPN 250 MB
- "Other" 100,000,300 B (all 21 HTTPS/DNS response halves)
- "Database" 10,000,000 B (the 443 -> 3306 response)
- "Web (HTTP/HTTPS)" 100,000 B, i.e. about 0.1% of the real HTTPS bytes
/top-conversations: 10 of the top 12 rows were `93.184.216.34 -> 10.0.0.5 dport=5000x service=TCP/5000x application=Other src_ports=[443]`. The ESP row showed service=none, application=VPN / Tunneling.
/forensics: `93.184.216.34:443 -> 10.0.0.5:50000 service=SAP`, other responses `TCP/500xx`, `8.8.8.8:53 -> ...:53000 service=UDP/53000`.
Prototype fix check: a multiIf service-port expression (known-port on one side, else ephemeral/lower-port) run on the same fixture grouped everything correctly:
- 443 request 100 KB / response 110 MB
- 53 request/response
- ESP and ICMP as n/a
The same inference works on the rollup via finalizeAggregation(src_ports) (verified on a copy of flow_conversations_1h).

**Corrections to the review.** The claim is accurate but understated. Classification relies exclusively on dst_port (not "primarily"); only portless protocols are classified by IP protocol.
Impacts the doc misses (all reproduced):
1. Application byte shares are systematically wrong. Response or download bytes (usually the majority) land in "Other", or in a random bucket when the client or PAT port collides with a known port. A PAT'd client port of 3306 is labelled "Database". Client port 50000, which is inside both the Linux (32768-60999) and Windows (49152-65535) ephemeral ranges, is labelled "SAP".
2. Top Conversations fragments each server->client exchange into one row per ephemeral client port (GROUP BY src,dst,protocol,dst_port at netflow.py:1005/1067). The request direction collapses into one small row. The hourly rollup key (migrate-071) inherits this cardinality.
3. `app=Web` drill-downs return only request halves.
Line refs are slightly off: service cell is O:1097/1114 (doc says O:1100); forensics service is A:1896 (doc says 1898). I rate it P1, not P2, because the headline Applications view misattributes most bytes.

### NF-21 — ICMP flood detection depends on cumulative record count

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** netflow.py:2131-2155:
`SELECT toString(src_addr) AS src, count() AS flows, sum(packets) AS pkts ... AND protocol = 1 GROUP BY src HAVING flows >= 5000 ORDER BY pkts DESC LIMIT 3`
- No rate, no time bucketing, no baseline, no destination-centric pass.
- The threshold is on record count, which depends on exporter active/inactive timeouts and ignores sampling. Packets and bytes are sampling-scaled; count() is not.
- The finding has fixed severity 'warning' with no threshold, window or rate fields.
- The ZenPlus sensor itself pings every monitored device every 60 s by default (poller/cmd/sensor/main.go:1893 `secondsOrDefault(device.PingInterval, time.Minute)`), which is exactly the pattern that crosses 5000 records over longer windows.
- The Network Security dashboard counts these as 'Threat Signals' (NetworkSecurityDashboard.tsx:252-254, 416-422).

**Reproduction.** In nfv_nf20 (exporter 192.0.2.2):
- Benign monitoring fixture: 10.9.9.9 pings 40 hosts once a minute for 7 days (403,200 one-packet records, about 0.67 pps).
- Flood fixture: 10.6.6.6 -> 10.0.0.1 at 100,000 pps for 10 minutes, exported as 10 records of 6,000,000 packets each.
Called the real netflow_anomalies():
- hours=1: 0 findings
- hours=24: 1 finding, `icmp_flood warning | 10.9.9.9 generated 57,600 ICMP flows (57,600 packets)`
- hours=168: 1 finding, 10.9.9.9 with 403,200 flows
The 60-million-packet flood was never reported at any window.

**Corrections to the review.** The claim is correct; the doc's arithmetic checks out (42,660 pkts / 86,400 s = 0.49 pps).
The doc misses the opposite failure: the detector is blind to real floods.
- A genuine 100k pps single-source flood exported with a 60 s active timeout produces about 1 record per minute, so it never reaches 5000 records at any window.
- Distributed floods (many sources, one victim) are also invisible, because it groups by src only.
ORDER BY pkts while thresholding on flows is inconsistent. Only 3 candidates are ever returned.
Line refs: actual A:2131-2155 (doc says 2137-2167).

### NF-22 — Volumetric-outlier explanation uses the wrong total

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** netflow.py:2157-2186:
- The comment reads "volumetric outliers via mean+3σ proxy", but no mean or σ is computed.
- Query `SELECT src, sum(bytes) ... GROUP BY src ORDER BY bytes DESC LIMIT 5`.
- `total = sum(int(r[1]) for r in rows)`, i.e. the top-5 subtotal only.
- `if rows[0] > total*0.5 and rows[0] > 10_000_000_000` -> description "alone produced more than half of all traffic in this window".
Further problems:
- The 10 GB floor is absolute: about 22 Mbps sustained for a 1 h window, trivial for 30 d.
- A site where all flows share one src (e.g. post-NAT exporter address) always passes the subtotal test.
- Base filter is `_exporter_filter` (2023), so the corrupt-flow guard in _scope (netflow.py:593-595) is not applied.

**Reproduction.** Fixture in nfv_nf20, exporter 192.0.2.3:
- Host A 10.3.0.1 with 30 GB
- Four hosts with 5 GB each
- 200 hosts with 1 GB each
Total is 250 GB, so A has 12% network-wide and 60% of the top-5 subtotal. netflow_anomalies(hours=1, exporter=192.0.2.3) returned `volumetric_outlier info | 10.3.0.1 alone produced more than half of all traffic in this window`.
A single 5 TB corrupt record (exporter 192.0.2.4, sampling 1) also produced a 'more than half' finding. The API's _scope guard excludes that record from every other aggregate.

**Corrections to the review.** The claim is accurate. Line refs are actual A:2157-2186; the doc's 2170-2207 range extends past the function's return at 2188-2192. The doc does not mention that the detector also ignores the corrupt-record guard, so a single corrupt record triggers it. That is listed as an extra defect.

### NF-23 — Alerts / Incidents is not an incident lifecycle

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** `buildAlerts` (O:1840-1913) is purely client-side.
- Exporter age uses `Date.now()` (O:1842-1844). Age ≥ 15 min gives "Device Offline"/critical with body "stopped exporting flows" and a WifiOff icon (O:1845-1853). There is no reachability input.
- A hardcoded `current_bps > 100_000_000` threshold produces 'High Bandwidth Usage', otherwise 'Throughput Healthy' with severity 'success' (O:1865-1882).
- 'Privileged Service Traffic' (id 'unauth', info) fires whenever the 'System Services' bucket has bytes > 0 (O:1893-1901). That bucket is ANY port 1-1023 not in APPLICATION_BUCKETS (A:259-267), so it fires on ordinary traffic.
- `flows > 1_000_000` gives 'Large Flow Volume' (O:1903-1911).
- The list is capped with `return list.slice(0, 6)` (O:1912). Exporter items are pushed first, so 6 or more silent exporters crowd out everything else, and the count badge shows the capped length.
- The badge counts every item, success and info included, and uses the 'warning' tone whenever the list is non-empty (O:1926).
- Title "Alerts / Incidents" (O:1922).
- The server has no NetFlow alert integration: only `netflow.view` exists in core/permissions.py:37-38, and no service besides netflow_rollup references flow data. The panel is not linked to Anomalies or the alert engine.

**Reproduction.** Playwright with mocked NetFlow responses: one exporter receiving 3 s ago, 39 Mbps, 1,514,234 flows, 'System Services' 4e9 bytes. The panel rendered "Alerts / Incidents … 3 · Throughput Healthy 39.00 Mbps · Privileged Service Traffic 3.7 GB on system ports · Large Flow Volume 1,514,234 flows". The badge class was `border-warning/30 bg-warning/10 text-warning`: an amber '3' with zero actionable items.

**Corrections to the review.** Accurate. The cited range O:1840-1960 matches the repo. Two nuances the doc omits:
- The badge shows the capped count, and uses warning colour even when every entry is 'success' or 'info'.
- The 'System Services' trigger is broader than "system-port traffic": it is any unclassified port 1-1023, so it fires on almost every network.
A related, stronger false-'Device Offline' path is listed separately as an additional defect: exporter last_seen is computed under the page filters.

### NF-24 — Top Talkers changes ranking when View all is opened

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** Overview: NetflowPage.tsx:430 `/netflow/top-talkers?${rangeQS}&limit=6&by=src`, donut 786-795 subtitle 'By bytes sent', value `t.src_bytes ?? t.bytes`, link `sectionLink('top-talkers')` (531-536) copies searchParams but never sets `by`. Section page: NetflowPage.tsx:197-208 builds qs from a fixed key list ['exporter','iface','talker','protocol','dscp','app','netclass','tcpflag','hour','dow'] — no `by`; meta description (160-165) 'by total bytes'; table (1234-1265) shows `row.bytes` (total) and only endpoints get As Source/As Destination columns. API: netflow.py:928 `by` default 'total' → rank 'bytes'; top-endpoints 954 also passes rank 'bytes' through the same _top_ips_detail, so the two drill-downs issue identical SQL. _top_ips_detail phase 1 (855-865) `ORDER BY {rank_expr} DESC` has no `HAVING rank > 0` and no tie-breaker.

**Reproduction.** Scratch CH fixture: ranking by sum(bytes) → 10.9.9.9, 198.51.100.7 (0 sent), 192.0.2.10 (0 sent), 10.1.1.2; ranking by sent → 10.9.9.9, 10.1.1.2, 10.1.1.1, 198.51.100.7 (0 sent padding). Dev API :8095 hours=24: top-talkers (default) and top-endpoints returned identical ordered lists; by=src swapped the top two rows.

**Corrections to the review.** Accurate. Extra facts: the NOC DashboardPage 'Top Talkers' (DashboardPage.tsx:206) also uses the default total ranking, so the platform has two meanings of 'Top Talkers'. With by=src, receive-only hosts (0 bytes sent) still pad the list when there are fewer senders than `limit`, and equal-total rows have nondeterministic order across refreshes (observed: 203.0.113.10/198.51.100.1 tie).

### NF-25 — Donut totals and percentages use an undisclosed subset

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** Donut data and queries:
- Talkers O:430 `limit=6&by=src` and O:789 value `t.src_bytes ?? t.bytes`.
- Endpoints O:435 `limit=6`, O:799.
- Applications O:809 `applicationData.slice(0, 6)`.
- Protocols O:818 `protocolData.slice(0, 6)`.
- Conversations O:440 `limit=10`, O:831 `conversationData.slice(0, 6)`.
DonutCard:
- O:1742 `const total = data.reduce(...)` sums only the passed slices.
- O:1792-1793 labels that sum 'Total'.
- O:1812 percentages are `d.value / total`.
- O:1799 `data.slice(0, 5)` renders five legend rows for up to six slices.
API:
- `_top_ips_detail` (A:818-918) returns only the top N. It has no denominator or remainder.
- Endpoint bytes come from `sum(bytes)` over an ARRAY JOIN of src and dst roles (A:860, A:892), so each flow is counted once per endpoint.
The same subset-denominator pattern appears in DscpCard: O:2274-2275 `top = data.slice(0, 6); total = top.reduce(...)`.

**Reproduction.** Playwright with mocked data: 6 talkers summing to 21e9, overview total 658.2e9.
- Top Talkers: 6 sectors, 5 legend rows, centre "TOTAL 19.6 GB" against a KPI of 613.0 GB. Percentages 28.6/23.8/19.0/14.3/9.5%, so the sixth slice (4.8%) has no legend entry.
- Applications: 6 of 10 buckets drawn, centre "TOTAL 78.2 GB" against 80.0 GiB across all buckets.
- Conversations: 6 sectors from 10 loaded rows.
ClickHouse fixture (private DB, same ARRAY JOIN as A:860): sum(bytes)=115,200,200; the sum of per-endpoint bytes is 230,400,400 (exactly 2x); the sum of per-IP src_bytes is 115,200,200.

**Corrections to the review.** Mostly accurate, with corrections:
- The Conversations donut uses 6 of the 10 loaded rows (O:831), not ten.
- The doc omits the Applications donut (O:809; the API returns every bucket, so the subset is purely client-side), the Protocol donut (O:818) and the DSCP card (O:2274-2275), which have the same undisclosed subset denominator.
- For Top Talkers (by=src) and Conversations, the full denominator already exists as overview.bytes: summed src_bytes equals total bytes.
- For Endpoints, a donut is inherently wrong: shares are non-additive, and attributed bytes sum to 2x total.

### NF-26 — Overview search filters only ten loaded conversations

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** `const [search, setSearch] = useState('')` (NetflowPage.tsx:282). The input sits in the page header next to the time picker, with placeholder 'Filter flow records (IP, service, protocol)…' (O:641-648), and is `hidden md:block`. `filteredConversations` filters only `conversationData` (O:538-548), and that data comes from `/top-conversations?…&limit=10` (O:437-440). It is consumed only by `<ConversationTable conversations={filteredConversations}>` (O:841). No KPI, chart or other widget reads `search`. Matching is substring (`c.src.includes(q)`, O:543-544), so '10.0.0.1' also matches 10.0.0.10 and 110.0.0.1. The table is titled 'Recent Flow Records' with subtitle 'Highest-volume endpoint pairs' (O:2073-2074), though it holds the top 10 by bytes and not recent records. The empty state 'No flow conversations match.' does not say that only 10 rows were searched.

**Reproduction.** Code-path trace only; no fixture was needed because the data flow is closed: search → filteredConversations → ConversationTable, with the source limited to 10 rows.

**Corrections to the review.** Accurate. Additions: the local filter matches by substring, so an IP query over-matches; the 'Recent Flow Records' title compounds the mislabel; the box is hidden below the md breakpoint. A cheap global option already exists: every overview endpoint applies the `talker` scope param (src OR dst; netflow.py:612-613) and the page already reads `?talker=` into rangeQS (O:381). An applied host filter can therefore reuse it once `_scope` accepts CIDRs (NF-08).

### NF-27 — Top Interfaces ignores most page filters without a scope label

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** NetflowPage.tsx:397-407 interfacesQS contains only hours/limit/from/to/exporter; query key (475-476) likewise omits filters. More importantly the API cannot honour filters even if sent: netflow.py:1529-1539 /interfaces declares only hours/minutes/limit/from/to/exporter (no `scope_params`), builds `_exporter_filter(exporter)` (1542) and FastAPI silently ignores unknown query params. Card subtitle (2466-2468) says 'Highest-traffic interfaces across all exporters' with no scope note while filter chips are active.

**Reproduction.** Dev API :8095: /interfaces?hours=24&limit=10 and /interfaces?hours=24&limit=10&protocol=17 returned byte-for-byte identical rows (protocol ignored).

**Corrections to the review.** Accurate, but the defect is in both layers — a UI-only change to interfacesQS would have no effect because the endpoint has no filter parameters. Line refs correct (O:397–407, API:1530).

### NF-28 — Heatmap lacks date, timezone and coverage semantics

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** Label: NetflowPage.tsx:2216 is hard-coded 'Bytes by hour over the last 7 days'. heatmapQS (:465-469) pins only presets to 168h and keeps the custom from/to, so custom windows get the wrong label.
Aggregation: the API (netflow.py:1409-1422) runs `toDayOfWeek(timestamp)`/`toHour(timestamp)` with sum(bytes) over the whole window. The column is DateTime64(3,'UTC') (migrate-20260506:5), so cells are UTC; session_timezone does not change this (verified). The response is only {max_bytes, cells}: no from/to, tz, basis or coverage.
Rendering: missing cells become 0 (`map.get(...) || 0`, :2228) with a '0 B' tooltip. Only '0h'/'23h' labels (:2245, :2255). The hue ramp runs green→red (:2206-2210) with a 'Less/More' legend and no numbers, on an undisclosed sqrt scale. The When chip (:606) shows 'Sun 22:00' with no tz, while the throughput chart axis and tooltip (utils.ts:88-111, toLocaleTimeString) and the custom picker (TimeRangePicker.tsx:339) are browser-local. The _scope hour/dow filters (netflow.py:664-667) are also UTC. Refs API:1397 and O:463/2194-2260 are accurate.

**Reproduction.** Real netflow_heatmap on nfv_clock.
- Custom 2026-09-15→09-25 with uniform traffic gives bytes per cell by weekday {Mon:60000, Tue:120000, Wed:120000, Thu:120000, Fri:60000, Sat:60000, Sun:60000}, max_bytes=120000.
- Relative 7d: 31 of 168 cells returned. The other 137 render as '0 B', and the payload keys are only ['max_bytes','cells'].
- ClickHouse with session_timezone='Asia/Riyadh': a 2026-09-20 22:00Z row gives toHour=22, toDayOfWeek=7 (Sun, UTC). toTimeZone(...,'Asia/Riyadh') gives Mon 01:00. For Asia/Kolkata the UTC hour splits into local 03 and 04. Note toDayOfWeek's 2nd arg is the week mode, so use toDayOfWeek(toTimeZone(ts, tz)).

**Corrections to the review.** 1) 'Chart labels use browser locale' is imprecise. The heatmap's own labels are fixed English UTC hours with no zone. It is the neighbouring throughput chart, tooltips, custom picker and flow times that are browser-local.
2) Clicking a cell does select the right records: the hour/dow filter is UTC, like the cells. The mismatch is presentation. A Riyadh user clicks 'Sun 22:00' and the chart spike appears at 'Mon 01:00'.
3) Understated: this is not only a labelling problem. Custom windows longer than 7 days sum unequal numbers of weekday occurrences, so intensity is biased. With uniform traffic over 10 days, Tue–Thu cells are exactly 2× the others and render red.
4) For the 7d preset, the window starts mid-hour, so the current (dow, hour) cell merges the partial current hour with the rest of the same hour 7 days earlier.
5) Absent cells cannot distinguish 'idle' from 'collector/exporter gap': the payload has no coverage data at all.

### NF-29 — Light-theme status cards have very weak text contrast

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** Capacity Summary (C:165-182):
- Tone map C:166-171, e.g. `rose: 'border-rose-500/30 bg-rose-500/5 text-rose-300'`.
- Label C:176 `text-[10px] uppercase tracking-wider opacity-80`; sub-label C:179 `text-[10px] opacity-70`.
Anomalies SevTile (N:175-192) is identical: map N:176-182, label N:187.
The light theme exists (globals.css:5-30; the `.dark` class is toggled by stores/theme.ts). The config has no `dark:` variants and no light override for the `*-300` palette classes (tailwind.config.js `darkMode:'class'`, `plugins:[]`).
The same pattern appears on further surfaces the doc does not cite:
- TcpFlagsCard tiles O:2333-2338 (`text-*-300` on `bg-*-500/10`, labels at opacity-70).
- Anomalies empty state `text-emerald-300` (N:107) and kind icon `text-amber-400` (N:119).
- The Capacity utilization column uses `text-rose-400 / text-amber-400 / text-emerald-400` (C:118).

**Reproduction.** Browser-computed styles in the light theme (Playwright, mocked data) match the doc's rgb values exactly:
- Critical `rgb(253,164,175)`: label 1.53:1, value 1.70:1.
- Warning `rgb(252,211,77)`: 1.26:1 / 1.33:1.
- Healthy `rgb(110,231,183)`: 1.31:1 / 1.39:1.
- Anomalies Info `rgb(103,232,249)`: 1.26:1 / 1.33:1.
- The slate 'Unknown speed' / 'Total' label (muted at 0.8 opacity): 3.12:1.
In dark theme the same tiles measure 6.8-13:1, so this is light-theme only.
Python WCAG check using the exact Tailwind 3.4 hex values:
- Capacity util text on a white card: rose-400 2.69, amber-400 1.67, emerald-400 1.92.
- Anomalies empty state emerald-300: 1.52.
- TcpFlagsCard light tiles: 1.32-1.76.
- Also found: the platform Badge success/warning/info variants measure only 2.95/3.15/2.51 in light theme, so moving to the existing semantic tokens alone would still fail 4.5:1.

**Corrections to the review.** The claim and the computed colours are exactly right, but the line references are stale:
- [C:187-208] does not exist (NetflowCapacity.tsx has 182 lines); the actual range is C:165-182.
- [N:202-231] does not exist (NetflowAnomalies.tsx has 192 lines); the actual range is N:175-192.
The issue is broader than the status cards: the Overview TCP Flags tiles, the Anomalies empty state and icons, and the Capacity utilization column also fail.
Font note: html font-size is 14px, so `text-2xl` renders at 19.25px semibold. That counts as large text, but even the 3:1 large-text threshold fails (1.33-1.70).

### NF-30 — Narrow screens truncate decision-making KPI values

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** KPI grid O:662 `grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5`. KpiCard O:1038-1072: a fixed `h-12 w-12` icon (O:1061), the value at O:1066 `truncate text-2xl` and the sub-label at O:1067 `truncate text-[10px]`. Unique Hosts renders `src / dst` as a single string (O:695).
Breakpoints key on the viewport, but the desktop sidebar shifts the content (Layout.tsx:225 `md:ml-[var(--sidebar-w)]`). At md widths the main column is only 512px, split into 3 columns.

**Reproduction.** Playwright viewport sweep with mocked overview (bytes 658.2e9, flows 1,514,234, hosts 1,061/2,345), measuring scrollWidth against clientWidth of each `.truncate.text-2xl`. No page-level horizontal overflow at any width.
- 360px: all three values truncated (75/82, 75/89, 75/118 px).
- 390px: "1,061 / 2,345" truncated (90/118). Traffic and flows fit exactly with the fallback fonts. All sub-lines truncated ("812,345,678 pac…", "39.00 Mbps curr…", "sources / destin…"). A screenshot confirms "1,061 / …".
- 768px: main is 512px with 3 columns of 152px. All three values truncated (70/82, 70/89, 70/118).
- 1280px: 5 columns. Unique Hosts truncated (107/118) and the flows sub-line truncated.
- 640, 1024, 1366, 1440 and 1536px: fit.

**Corrections to the review.** Confirmed. The cited range O:1038-1070 matches. The exact truncated strings depend on font and value (with fallback fonts at 390px, "613.0 GB" and "1,514,234" fit exactly while "1,061 / 2,345" truncated).
The doc understates the scope: this is not only a phone issue. Tablet and small-laptop widths (768-1023px with sidebar) truncate all three values, and a 1280px laptop truncates Unique Hosts.

### NF-31 — Custom queries and database errors lack protective contracts

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** netflow.py:36-57 `_resolve_window`: `if from_ts and to_ts:` — a lone from or to silently falls through to `now - hours`; no span, retention, future or year bounds (only end<=start rejected). netflow.py:289-294 `_query_sync` → `HTTPException(500, detail=f"ClickHouse query failed: {exc}")`, with no logger call, so the diagnostic exists only in the HTTP body. NetflowForensics.tsx:211-214 renders `response.data.detail` verbatim. scope_params 502-514 and forensics 1802-1807 (proto/src_port/dst_port/min_bytes/dscp_eq) have no ge/le. No ClickHouse budget anywhere: get_ch_client (core/database.py:66-72) sets no settings, clickhouse_connect 0.8.9 default send_receive_timeout=300s, no `max_execution_time` in the repo, queries run via asyncio.to_thread on the default executor with no cancellation.

**Reproduction.** Dev API :8095 (admin): /forensics?from=2026-09-01T00:00:00Z (no to) → 200 with last-1h rows (silent fallback). /overview?from=1900-01-01&to=2999-01-01 → 200 with all data. /overview, /timeseries, /top-talkers, /heatmap, /anomalies, /capacity with from=0001-01-01 → HTTP 500 {"detail":"ClickHouse query failed: HTTPDriver for http://127.0.0.1:18123 received ClickHouse error code 41 … CANNOT_PARSE_DATETIME … (version 24.10.2.80 (official build))"}. dst_port=99999999999999999999999999 / 70000 / -1, proto=999, dscp_eq=200, hour=99&dow=-3, huge iface/protocol → all 200 with empty results. /overview?hours=1440&from=2026-08-01…&to=2026-09-30… → 422 'Input should be less than or equal to 720'. clickhouse_connect with settings max_execution_time=1 on `SELECT sleep(3)` → DatabaseError 'error code 159 … TIMEOUT_EXCEEDED' (exc.code is None, so the code must be parsed from the message).

**Corrections to the review.** Largely accurate; refinements from reproduction: (1) Out-of-range numerics (port 70000/-1/1e26, dscp 200, hour 99, dow -3, huge iface/protocol) do not cause DB errors or leaks — parameters are bound safely and ClickHouse returns a silent empty 200. The harm is a misleading empty result, not injection. (2) The DB-error leak is reachable from user input through dates: from=0001-01-01 yields HTTP 500 with `HTTPDriver for http://127.0.0.1:18123 received ClickHouse error code 41 … CANNOT_PARSE_DATETIME … (version 24.10.2.80)` on overview/timeseries/top-talkers/heatmap/anomalies/capacity, exposing the internal host:port, driver and server version; genuine server errors (UNKNOWN_IDENTIFIER, ILLEGAL_AGGREGATION) would also echo SQL text. (3) 'Arbitrary custom spans' holds for direct API callers, but the UI always sends `hours=round(span)` with custom bounds (TimeRangePicker.tsx:141-143), so any custom window > ~30 days fails with an opaque 422 'Input should be less than or equal to 720' on a hidden parameter. The real scan cost of huge spans is capped by table TTLs (raw 30d, rollups 90d); the bigger risk is silent truncation — the window is labelled as requested while data only covers retention.

### NF-32 — IPv6 telemetry can become artificial IPv4 endpoints

**Verdict:** confirmed · review P1 → assessed P1

**Evidence.** Collector:
- poller/internal/netflow/v9.go:394-459: assignV9Field has no case for IE 27/28 (IPv6 src/dst), 62 (IPv6 next hop) or 29/30.
- v9.go:349-359 fillMissingAddrs sets nil addresses to 0.0.0.0. Records are still appended with real bytes, ports and protocol (v9.go:323-324, ipfix.go:213-214), so they are not dropped.
- Exporter fallback to 0.0.0.0 exists in all three decoders: v9.go:157-160, ipfix.go:34-37 and v5.go:88-91.
- The fallback address is also the template-cache key (v9.go:177,188; ipfix.go:54-58), so every IPv6 exporter shares one key.
Schema: flow_records exporter_ip/src_addr/dst_addr/next_hop are IPv4 (migrate-20260506:8-16, main.go:298-306), and the same in the rollups (migrate-071:22-24, migrate-072:17).
API impact:
- /network-classes (netflow.py:1756-1764) puts 0.0.0.0 in 'Public'.
- /countries (2217-2256) gives 'Private / Unknown'.
- Top talkers and the volumetric-outlier detector (2156-2185) aggregate all IPv6 traffic into one '0.0.0.0' host.
- Forensics (1819-1832) accepts an IPv6 literal, appends '/32' and silently returns [].

**Reproduction.** Go tests in scratch against the worktree decoder:
(1) A v9 template with IE 27/28 (2001:db8::10 to ::20), bytes 5000, proto 6 decodes to src=0.0.0.0 dst=0.0.0.0 bytes=5000 proto=6. It is kept as a normal record.
(2) Two IPv6 exporters (2001:db8::a and ::b) announce IPFIX template 256 in domain 0 with different field orders. Exporter A's data is then decoded with B's template: exporter=0.0.0.0 src=10.0.0.2 dst=0.0.3.232 bytes=167772161. That is garbage that passes the 1 TB sanity guard.
(3) The /network-classes multiIf evaluated on CH classifies 0.0.0.0 as 'Public'. geoip.country_of('0.0.0.0') returns (None, None), which shows as 'Private / Unknown'.
(4) Dev API /netflow/forensics?src=2001:db8::1 returns HTTP 200 []. isIPAddressInRange(IPv4 string, '2001:db8::1/32') is 0, and the /32 suffix on an IPv6 literal is itself wrong (it matches 2001:db8:ffff::9).
(5) On CH 24.10, `ALTER TABLE flow_records MODIFY COLUMN src_addr IPv6` fails with code 524 ALTER_OF_COLUMN_IS_FORBIDDEN (key column).

**Corrections to the review.** The claim is correct but understated.
(a) The 0.0.0.0 exporter substitution happens in v9 and v5 as well as IPFIX.
(b) More serious than the doc says: all IPv6 exporters collapse onto one template-cache key (0.0.0.0/domain/template). Two IPv6 exporters that reuse a template ID (very common, 256/257) decode each other's data with the wrong layout. The result is garbage addresses and byte counts, not just zeros.
(c) IPv6 flows are not merely 'artificial endpoints'. They keep real byte counts and land in the Public network class, the 'Private / Unknown' country, and a single mega-talker eligible for the volumetric-outlier finding.
(d) Forensics silently returns nothing for IPv6 input instead of saying IPv6 is unsupported.
Line refs are off by 1-2 (fillMissingAddrs is at 349-359, the IPFIX exporter fallback at 34-37). A key-column type change is impossible in place, so 'add real IPv6 storage' requires a new table.

### NF-33 — Interface indexes are narrowed to 16 bits

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** Collector and schema:
- poller/internal/netflow/v5.go:28-29: `InputSNMP uint16` / `OutputSNMP uint16`.
- v9.go:434-435: `case 10: r.InputSNMP = uint16(uint64BE(value))`.
- v9.go:444-445: `case 14: r.OutputSNMP = uint16(...)`. IPFIX reuses this path (ipfix.go:206), and IPFIX IE 10/14 are unsigned32.
- flow_records input_snmp/output_snmp are UInt16 (migrate-20260506:17-18; main.go:307-308; init-clickhouse.sql:226).
- flow_conversations_1h uses AggregateFunction(groupUniqArray(10), UInt16) (migrate-071:31-32).
Consumers:
- netflow.py /interfaces (1555-1571) and /capacity (1958-1968) filter `input_snmp != 0`.
- link_utilization.py:452-458 and 1023-1081, and manual_maps.py:999-1012, also read these columns.
- Names resolve by (exporter_ip, ifindex) against device_interfaces.if_index, an Integer column (models/device_interface.py:22), so a truncated index can resolve to a different real interface.

**Reproduction.** A Go test with INPUT_SNMP=70000 (4 bytes) decodes to in_snmp=4464, and OUTPUT_SNMP=0x1a000200 decodes to 512. Computed truncations (low 16 bits): NX-OS Ethernet1/1 436207616 -> 0; port-channel1 369098752 -> 0; mgmt0 83886080 -> 0; port-channel2 369098753 -> 1; Vlan1 151060481 -> 1. The zeros vanish entirely because of the `!= 0` filters, and port-channel2 and Vlan1 collide on the same exporter. Arista Port-Channel1 1000001 -> 16961.
Migration checks on scratch CH 24.10:
- `ALTER flow_records MODIFY COLUMN input_snmp UInt32` works.
- `ALTER ... AggregateFunction(groupUniqArray(10),UInt16) -> UInt32` fails with CANNOT_CONVERT_TYPE.
- Widening raw while an MV still writes the UInt16 aggregate makes every INSERT fail ('while pushing to view'). The raw row is still committed, which is an ambiguous outcome.

**Corrections to the review.** The claim is correct. Line refs are off: the Record fields are at v5.go:28-29 (the doc's 25-26 are SrcAddr/DstAddr), and the casts are at v9.go:435 and 445 (the doc's 438-451 range misses the INPUT_SNMP cast).
The doc's 'Continue qualifying indexes by exporter/observation domain' is inaccurate. The v9 source ID and IPFIX observation domain are not persisted anywhere (flow_records has only v5 engine_type/engine_id), so interfaces are keyed only by (exporter_ip, ifindex).
Severity: P2 in general, but P1 for exporters with large ifIndex encodings (NX-OS-style). There, whole interfaces disappear (truncated to 0 and filtered out) or merge, which corrupts the Interfaces, Capacity, Link Utilization and Maps views. Also note link_utilization.py and manual_maps.py as additional consumers the doc does not mention.

### NF-34 — Saved-view workflow gives poor feedback and incomplete control

**Verdict:** confirmed · review P2 → assessed P2

**Evidence.** NetflowForensics.tsx:108-117 uses `window.prompt(...)` and then `if (!name) return`, so whitespace passes. `await api.post(...)` has no try/catch, so a 4xx or 5xx becomes an unhandled promise rejection with no feedback, and the success message is just `alert('Saved.')`. There is no pending or disabled state and no `invalidateQueries(['netflow','saved-views'])`. With staleTime 10_000 (main.tsx:12), the Saved Views list can show stale data for up to 10 s. NetflowSavedViews.tsx:24-27: the delete mutation has no onError and no isPending handling. :74 uses window.confirm. :57-63: loading and error states render the empty state. The API has no PATCH/PUT for saved views (only 2312 GET, 2332 POST, 2353 DELETE), and the UI always sends `pinned: false` (F:114), so description and pin cannot be edited anywhere. SavedViewOut (2207-2214) has no owner field.

**Reproduction.** Over HTTP, POST /netflow/saved-views accepted name '   ' and name '' with 200 and created both rows, confirming that neither server nor client validates whitespace. The rest was verified by reading the code.

**Corrections to the review.** The doc's line refs are off. F:124-132 should be F:108-117. V:23-126 exceeds the file, which has 107 lines; the relevant code is V:18-107. The doc also understates the edit gap: description and pin are not merely missing from this page, the backend has no update endpoint at all, so NF-34's edit and pin work needs the NF-05 PATCH route.

## 4. Defects the review missed (X-01 … X-52)

Deduplicated from 81 reports; each re-checked by an independent skeptic against the frozen reviewed code.

| ID | Severity | Upheld | Related | Title | File |
|---|---|---|---|---|---|
| X-01 | P2 | yes | NF-04 | netflow.view RBAC permission is defined and seeded but never enforced by the API or the menu | `server/app/api/v1/netflow.py` |
| X-02 | P1 | yes | — | NetFlow v5 sampling header is stored without masking the 2-bit sampling-mode field, inflating sampled traffic ~165x (1:100) / ~17x (1:1000) | `poller/internal/netflow/v5.go` |
| X-03 | P2 | yes | NF-06 | Collector drops its in-memory batch and queued records on every stop/restart/OTA, and loses templates/samplers so data sets are discarded until re-advertised | `poller/cmd/netflow-collector/main.go` |
| X-04 | P2 | yes | NF-01 | Exporter liveness (last_seen) is computed under the page's drill-down filters, so any filter or heatmap click raises a false critical 'Device Offline' | `server/app/api/v1/netflow.py` |
| X-05 | P2 | yes | NF-16 | NetFlow capacity ignores the operator circuit-speed override (configured_speed_bps) and uses raw SNMP if_speed as the utilization denominator | `server/app/api/v1/netflow.py` |
| X-06 | P2 | yes | — | Bidirectional/firewall counters are discarded: OUT_BYTES/OUT_PKTS dropped when IN_* is present and ASA NSEL 231/232/298/299 not decoded | `poller/internal/netflow/v9.go` |
| X-07 | P2 | yes | — | IE 85/86 cumulative totals overwrite a present IE 1/2 delta and are summed as per-export deltas | `poller/internal/netflow/v9.go` |
| X-08 | P2 | yes | — | Sampling-rate learning is wrong for common exporters: inline IE 34/305 ignored, sampler IDs ignored, only the first options record read, PSAMP misread, and samplers reset to 1 on restart | `poller/internal/netflow/v9.go` |
| X-09 | P2 | yes | NF-21 | Flow counts and anomaly-detector thresholds ignore sampling (only bytes/packets are scaled), and sampling is never disclosed | `server/app/api/v1/netflow.py` |
| X-10 | P3 | yes | NF-01 | Exporter-supplied flow timestamps are trusted with no bound or skew detection: a skewed exporter's flows vanish from windows, fake 'offline', are TTL-dropped at insert, and can get whole batches rejected | `poller/internal/netflow/v9.go` |
| X-11 | P2 | yes | — | Rollup healer rebuilds hourly rollups from TTL-truncated raw data whenever raw retention is below 27 days, permanently destroying rollup history | `server/app/services/netflow_rollup.py` |
| X-12 | P2 | yes | NF-32 | Template cache lets a stale data template shadow a re-used options template ID (options records decoded as garbage flows), and all IPv6 exporters share one template key | `poller/internal/netflow/v9.go` |
| X-13 | P2 | yes | NF-06 | Writer stall collapses UDP intake: backpressure waits 250 ms per record on the single reader, and kernel socket drops are neither prevented nor counted | `poller/cmd/netflow-collector/main.go` |
| X-14 | P3 | yes | NF-06 | Collector exits instead of listening when ClickHouse is unavailable at startup | `poller/cmd/netflow-collector/main.go` |
| X-15 | P2 | yes | NF-04 | NetFlow data is exposed unscoped outside /netflow: link-utilization NetFlow overlay and report traffic sections aggregate all flow_records for scoped users | `server/app/api/v1/link_utilization.py` |
| X-16 | P3 | yes | NF-04 | Exporter-mapping writes bypass scope (device hostname oracle) and will become a visibility-escalation path once NF-04 keys visibility on exporter ownership | `server/app/api/v1/netflow.py` |
| X-17 | P3 | yes | NF-34 | Saved-view payload is unvalidated and unbounded server-side (multi-MB query dict accepted and broadcast to every user's list); empty/whitespace names accepted; list unpaginated | `server/app/api/v1/netflow.py` |
| X-18 | P2 | yes | NF-31 | Unknown or stale filter values (app, netclass, tcpflag, group) are silently dropped, returning whole-network totals under a filter chip (fail-open) | `server/app/api/v1/netflow.py` |
| X-19 | P2 | yes | NF-08 | Interface filter is a bare ifIndex, not (exporter, ifIndex): selecting one interface mixes every exporter's same-index traffic, and the Link Utilization deep link drops its exporter | `server/app/api/v1/netflow.py` |
| X-20 | P2 | yes | NF-31 | UI always sends `hours` (le=720), so custom windows over 30 days and calendar-range URLs 422 on every widget, and calendar keys otherwise query a trailing window | `dashboard/src/pages/NetflowPage.tsx` |
| X-21 | P2 | yes | NF-31 | Forensics page crashes to the ErrorBoundary on any FastAPI 422 (e.g. non-numeric port) because it renders the detail array as a React child | `dashboard/src/pages/NetflowForensics.tsx` |
| X-22 | P3 | yes | NF-03 | Forensics shows 'No flow records matched these filters' while the query is still loading or retrying | `dashboard/src/pages/NetflowForensics.tsx` |
| X-23 | P3 | yes | NF-02 | Compare overlay joins prior points by exact timestamp, so custom spans that are not a bucket multiple draw no prior trace while still showing a % delta | `dashboard/src/pages/NetflowPage.tsx` |
| X-24 | P2 | yes | NF-01 | Overview exporter health badge is frozen by useMemo and never ages to stale/offline on an open page | `dashboard/src/pages/NetflowPage.tsx` |
| X-25 | P3 | yes | NF-17 | Raw 30-day TTL vs 90-day rollups: whether a historical window shows data depends on which source each widget or filter uses, and expiry is shown as zero traffic | `server/app/api/v1/netflow.py` |
| X-26 | P2 | yes | NF-13 | Capacity attributes a flow's whole byte count to the 5-minute slot where it ended (no duration spreading), turning long flows into false utilization spikes | `server/app/api/v1/netflow.py` |
| X-27 | P2 | yes | — | Capacity ranks by absolute p95 bps with LIMIT 50, so small saturated circuits drop out of the list and out of the Critical/Warning counts | `server/app/api/v1/netflow.py` |
| X-28 | P3 | yes | NF-14 | flowDirection (IE 61) is not decoded, so exporters monitoring both ingress and egress double-count in capacity and every NetFlow total | `poller/internal/netflow/v9.go` |
| X-29 | P2 | yes | — | Conversation details pair every exporter with every ifIndex, naming interfaces on the wrong device (phantom In/Out ports) | `server/app/api/v1/netflow.py` |
| X-30 | P2 | yes | NF-20 | Top Conversations (raw and hourly rollup) fragment response traffic into one row per ephemeral client port | `server/app/api/v1/netflow.py` |
| X-31 | P3 | yes | NF-21 | SYN-scan detector cannot see vertical port scans and applies one fixed count threshold regardless of window length | `server/app/api/v1/netflow.py` |
| X-32 | P3 | yes | — | Sensitive-egress detector raises 'critical' on single benign flows, treats CGNAT/link-local as public, and never performs its documented internet-interface check | `server/app/api/v1/netflow.py` |
| X-33 | P3 | yes | NF-03 | Security dashboard advertises detectors (exfil, beacon) that do not exist and presents zero matches as 'No hostile patterns detected' | `dashboard/src/pages/dashboards/NetworkSecurityDashboard.tsx` |
| X-34 | P3 | yes | — | Anomaly counts are silently capped by per-detector LIMITs; the Total tile, severity tiles and API `total` report the capped number | `server/app/api/v1/netflow.py` |
| X-35 | P2 | yes | — | Throughput chart uses a category X axis over sparse buckets, so outages and idle gaps disappear and edge buckets dip | `dashboard/src/pages/NetflowPage.tsx` |
| X-36 | P3 | yes | NF-27 | Capacity, Top Interfaces and all anomaly detectors bypass the corrupt-flow guard that _scope applies to every other aggregate | `server/app/api/v1/netflow.py` |
| X-37 | P3 | yes | NF-31 | Long-window pages re-scan raw flow_records on fixed short polling intervals instead of using rollups (Anomalies 5 serial scans every 30 s, Capacity 2 scans every 60 s, heatmap 7 days every 60 s, unused continuity scan), with slow toString() IP predicates and an unbounded healer plan | `dashboard/src/pages/NetflowAnomalies.tsx` |
| X-38 | none | no (refuted) | NF-31 | talker and exporter filters are compared as raw strings without validation, so CIDR or malformed input silently matches nothing | `server/app/api/v1/netflow.py` |
| X-39 | P3 | yes | NF-19 | Device-status RST ratio follows the page's tcpflag/protocol filters, so selecting the RST tile fabricates a 100% 'High TCP RST Ratio' warning | `server/app/api/v1/netflow.py` |
| X-40 | P3 | yes | NF-20 | ICMP type/code carried in dst_port is treated as a transport port and misclassified | `server/app/api/v1/netflow.py` |
| X-41 | none | no (refuted) | NF-20 | UI fallback application classifier diverges from the server's buckets | `dashboard/src/pages/NetflowPage.tsx` |
| X-42 | P3 | yes | NF-17 | Window predicates use inclusive BETWEEN on both ends, double-counting the boundary instant between adjacent windows | `server/app/api/v1/netflow.py` |
| X-43 | P3 | yes | NF-08 | Forensics protocol select shows 'Any' while a non-listed protocol filter is applied, and the control cannot clear it | `dashboard/src/pages/NetflowForensics.tsx` |
| X-44 | P3 | yes | NF-09 | Security dashboard 'Anomalies →' / Threat Signals links drop the dashboard range | `dashboard/src/pages/dashboards/NetworkSecurityDashboard.tsx` |
| X-45 | P3 | yes | NF-10 | Client-side Forensics CSV export does not neutralise spreadsheet formulas | `dashboard/src/pages/NetflowForensics.tsx` |
| X-46 | P3 | yes | NF-04 | An explicit exporter mapping is silently ignored when the exporter IP also matches a device's management IP | `server/app/api/v1/netflow.py` |
| X-47 | P3 | yes | NF-16 | Link Utilization's has_netflow ignores the NetFlow exporter→device mapping | `server/app/api/v1/link_utilization.py` |
| X-48 | P3 | yes | NF-16 | Interface speed override endpoints write no audit record | `server/app/api/v1/devices.py` |
| X-49 | P3 | yes | — | DELETE /netflow/saved-views/{view_id} returns 500 for a non-UUID id | `server/app/api/v1/netflow.py` |
| X-50 | P3 | yes | — | rDNS helper caches every lookup that misses the 2-second deadline as 'no hostname' for an hour, including queued lookups it cancelled | `server/app/api/v1/netflow.py` |
| X-51 | P3 | yes | — | formatBps/formatBytes render 'undefined' units for values below 1 | `dashboard/src/lib/utils.ts` |
| X-52 | P3 | yes | — | No automated tests cover /netflow/anomalies, and the hard-coded table name prevents fixture testing | `server/tests/test_netflow_api.py` |

### X-01 — netflow.view RBAC permission is defined and seeded but never enforced by the API or the menu

**Skeptic verdict:** upheld (high confidence), severity P2. I confirmed this in the baseline code. 'netflow.view' appears only in server/app/core/permissions.py: it is defined at line 38 and granted to roles at lines 102, 118 and 124. Nothing anywhere else in server/app or dashboard/src checks it.
Backend:
- netflow.py:33 creates the router with no dependencies.
- main.py:149 includes the router with no dependencies either.
- Every route depends only on get_current_user, apart from scope_params, which only collects query parameters and checks no permissions.
- No require_permission call appears in netflow.py. Sibling modules udt.py and netpath.py do call it.
Frontend: the NetFlow node in navigation.ts (lines 146-157) has no `permission:` field, while NetPath at line 164 does.
As a result, taking netflow.view away from a role removes no access. Any signed-in user can read flow analytics, export forensics as CSV, and create saved views.
I lowered …

**Evidence.** permissions.py:37-39 defines netflow.view and the role seeds (102/118/124) grant it, but nothing else in server/app references it. netflow.py:21 imports only get_current_user and require_operator_user; netflow.py:33 is `router = APIRouter(prefix="/netflow", tags=["NetFlow"])` with no dependencies, and every read and saved-view route depends only on get_current_user. Sibling modules do enforce theirs (udt.py:88 require_permission('udt.view'), netpath.py:45 require_permission('netpath.view', 'netpath.manage')). navigation.ts:146-157 gives the NetFlow node no `permission:` while NetPath (:164) has one. Reproduced twice on the dev API: roles holding only [dashboard.view, devices.view] got HTTP 200 on /netflow/overview, top-talkers, forensics (including format=csv), capacity, anomalies, exporters, exporter-devices, saved-views, device-status and interfaces, and POST /netflow/saved-views succeeded; netpath/summary and udt/summary returned 403 for the same user. Distinct from NF-04 (tag/visibility scope) and NF-05 (saved-view ownership): even after those fixes, removing NetFlow from a role would remove no access.

### X-02 — NetFlow v5 sampling header is stored without masking the 2-bit sampling-mode field, inflating sampled traffic ~165x (1:100) / ~17x (1:1000)

**Skeptic verdict:** upheld (high confidence), severity P1. I checked this against the frozen baseline. poller/internal/netflow/v5.go:84 reads the whole 16-bit word at header bytes 22-23 into SamplingInterval, and line 107 copies it into Record.SamplingInterval without masking. No 0x3FFF or 16383 mask exists anywhere in the baseline poller or server (checked with grep). In the Cisco v5 header, the top 2 bits of that word are the sampling mode and the low 14 bits are the interval. So an exporter configured for deterministic 1-in-100 sends 0x4064, which the collector stores as 16484. In netflow-collector/main.go:150-152, the collector multiplies bytes and packets by SamplingInterval whenever it is greater than 1. The sane() check at main.go:143 runs on the raw counters before that multiplication, so it cannot catch the inflation, and the stored sampling_interval carries the same wrong factor. The result is about 165x inflation for mode 1 at 1:100 …

**Evidence.** v5.go:84 `SamplingInterval: binary.BigEndian.Uint16(data[22:24])` stores the full 16-bit word and v5.go:107 copies it as `uint32(pkt.SamplingInterval)`. In the v5 header the top 2 bits are the sampling mode and only the low 14 bits are the interval (nfdump/goflow mask with 0x3FFF). The collector then scales counters: main.go:150-153 `if record.SamplingInterval > 1 { record.Bytes *= ...; record.Packets *= ... }`. The sane() guard runs before the multiplication (main.go:143) and the API guard `bytes / greatest(sampling_interval,1) <= 1e12` (netflow.py:606, migrate-071/072) divides by the same wrong factor, so neither catches it. Reproduced independently in three Go tests: header 0x4064 (deterministic mode, 1-in-100) gives SamplingInterval=16484, so a 1500-byte flow is stored as 24,726,000 bytes instead of 150,000 (164.8x); random mode (0x8064) gives 327.7x. Every volume, bps, capacity, p95, top-N and report figure for such an exporter is inflated, and the error propagates into the 90-day rollups and flow_traffic_5m.

### X-03 — Collector drops its in-memory batch and queued records on every stop/restart/OTA, and loses templates/samplers so data sets are discarded until re-advertised

**Skeptic verdict:** upheld (high confidence), severity P2. I confirmed this against the baseline code. In main.go:65-71 the writer is started with `go c.runWriter(ctx)`, and main returns as soon as runUDP returns. Nothing waits for the writer, and the deferred conn.Close() runs right away. On shutdown, runWriter's `case <-ctx.Done(): flush()` calls c.insert(ctx, batch), which calls conn.PrepareBatch(ctx) with the context that SIGTERM has already cancelled. In clickhouse-go v2.23.0 (the version pinned in go.mod), clickhouse.go:257-263 acquire() checks `case <-ctx.Done(): return nil, ctx.Err()` before it does anything else. So the final flush is guaranteed to fail with "context canceled", even if main were still alive. The c.records channel (BatchSize*4) is never drained either. The systemd unit has no special stop handling, and build-release.py:931 stops zenplus-netflow-collector on every OTA, so this runs on every restart and every OTA.
Why I …

**Evidence.** main.go:65-71 starts `go c.runWriter(ctx)`, runs c.runUDP(ctx) and returns from main as soon as runUDP returns; nothing waits for the writer, and conn.Close() is deferred. The final flush cannot succeed anyway: main.go:246-248 `case <-ctx.Done(): flush()` calls c.insert(ctx, batch) (239) -> PrepareBatch(ctx) (262) with the context SIGTERM already cancelled, and the up-to-4000-record channel is never drained. Reproduced twice: 60 and 700 queued records at cancel -> 0 rows stored, log 'ERROR: netflow insert failed: context canceled', while the ingested counter reads 700. This happens on every systemctl restart and every OTA (scripts/build-release.py:931 stops zenplus-netflow-collector). Second restart loss: v9/IPFIX templates, options templates and samplers live only in memory (main.go:62-63, V9TemplateCache), so after a restart every data flowset is discarded as DataSetsWaiting (v9.go:207-210), logged only at INFO (main.go:206-208) and not counted in /health, until each exporter re-advertises (commonly up to 30 min on low-rate exporters). NF-06 covers only a failed insert, not the shutdown/restart path.

### X-04 — Exporter liveness (last_seen) is computed under the page's drill-down filters, so any filter or heatmap click raises a false critical 'Device Offline'

**Skeptic verdict:** upheld (high confidence), severity P2. I checked this against the baseline code and the defect is real. In netflow.py:2493-2494, netflow_exporters clears only `exporter` from the scope and then calls _scope() with every other drill-down: iface, talker, protocol, dscp, app, netclass, tcpflag, hour, dow and group. Those filters therefore apply to `max(received_at) AS last_seen`. scope_params (502-527) maps all of them straight from the query string. On the UI side, rangeQS (NetflowPage.tsx:378-393) adds all of these filters and is passed unchanged to /netflow/exports (453-456). buildAlerts takes the filtered e.last_seen and compares it to Date.now(): at 15 minutes or more it raises a critical 'Device Offline ... stopped exporting flows', and at 2 minutes or more a 'Stale Exporter' warning. The exporter status dots (1996-2003) use the same thresholds. The overview's last_seen (728) is also filter-scoped, and it drives the …

**Evidence.** netflow_exporters clears only the exporter filter: `scope_for_exp = {**scope, "exporter": None}; extra_sql, extra_params = _scope(**scope_for_exp)` (2493-2494), so talker, protocol, app, dscp, netclass, tcpflag, iface, hour and dow still apply to `max(received_at) AS last_seen` (2502). The overview's last_seen (netflow.py:728) is filter-scoped the same way. The UI treats it as receipt liveness: /exporters is called with the full rangeQS (NetflowPage.tsx:453-456), buildAlerts (1843-1853) raises critical 'Device Offline ... stopped exporting flows' at age >= 15 min, DeviceStatusCard dots go red (1996-2003) and the health badge (550-556) shows offline. Reproduced with the real _scope() on a private ClickHouse fixture (exporter sending every minute): unfiltered last_seen 2.5 min; protocol=1 -> 4320 min (critical); talker=10.9.9.9 -> 120 min (critical); heatmap cell dow=7/hour=17 -> 4295 min (critical). A second repro: age 5 s unfiltered vs 10,800 s with protocol=1. Distinct from NF-01 (browser vs appliance clock) and NF-23 (wording): the same false outage occurs with perfectly synchronized clocks.

### X-05 — NetFlow capacity ignores the operator circuit-speed override (configured_speed_bps) and uses raw SNMP if_speed as the utilization denominator

**Skeptic verdict:** upheld (high confidence), severity P2. Confirmed in the baseline. In server/app/api/v1/netflow.py, _resolve_interface_names (lines 1480-1497) selects only DeviceInterface.if_speed and never reads configured_speed_bps. netflow_capacity (lines 1984-2001) then uses meta['if_speed'] directly as the divisor for utilization_p95_pct and utilization_max_pct. The poller does not copy the override into if_speed: its upsert in poller/internal/store/postgres.go (around lines 647-690) keeps configured_speed_bps as a separate column and overwrites if_speed with the SNMP value on every poll. The dashboard (NetflowCapacity.tsx) only uses the utilization_p95_pct the server returns and has no fallback of its own. Everywhere else the platform prefers the override: link_utilization.py _effective_speed (lines 69-73) and network_alert_service.py:319 COALESCE(configured_speed_bps, if_speed). The devices.py PATCH describes the override as 'line …

**Evidence.** _resolve_interface_names selects only DeviceInterface.if_speed (netflow.py:1482-1491) and netflow_capacity uses it directly (:1984, :2000-2001). The rest of the platform honours the override: link_utilization.py:69-73 `_effective_speed` prefers configured_speed_bps, and network_alert_service.py:319 uses COALESCE(configured_speed_bps, if_speed). Reproduced in a rolled-back scratch-PG transaction: an interface with SNMP 1 Gbps and override 50 Mbps resolves to 1,000,000,000, so a saturated 50 Mbps WAN circuit reports 5% 'Healthy'; an interface with only an override (SNMP NULL) resolves to None and shows 'no ifSpeed'. NF-16 proposes adding an audited circuit-capacity override; one already exists and Capacity ignores it.

### X-06 — Bidirectional/firewall counters are discarded: OUT_BYTES/OUT_PKTS dropped when IN_* is present and ASA NSEL 231/232/298/299 not decoded

**Skeptic verdict:** upheld (high confidence), severity P2. In the baseline, assignV9Field (poller/internal/netflow/v9.go:396-411) is the only place counters are set, and both v9 and IPFIX use it. When IN_BYTES/IN_PKTS (1/2) are present, OUT_BYTES/OUT_PKTS (23/24) are dropped: the OUT value is used only when r.Bytes==0. If field 23 comes first in the template, the later case-1 write overwrites it. Nothing else in the netflow package keeps a reverse counter: there is no RevBytes field, no second record and no other case. There are no cases for NSEL 231/232/298/299 either. So an ASA NSEL template, which has no 1/2/85/86, decodes with Bytes=0 and Packets=0, and a FortiGate/nProbe bi-flow template (1+23 in one record) loses the responder direction. No validator or downstream path makes up for it. This is plain from the code, and the reporter's Go reproductions match it. The impact depends on the exporter: routers and switches that export …

**Evidence.** v9.go:400-407: `case 23: // OUT_BYTES ... use only if IN_BYTES absent` with `if r.Bytes == 0 { r.Bytes = uint64BE(value) }`, and the same for 24. Session-based firewall and nProbe bi-flow templates put initiator->responder in IN_BYTES and responder->initiator in OUT_BYTES in one record (Fortinet template docs list both; nfdump maps OUT_BYTES to the reverse direction). There are no cases for NSEL 231/232 (fwd/rev flow delta bytes) or 298/299, nor RFC 5103 reverse IEs. Reproduced in three Go tests: IN_BYTES=1000 + OUT_BYTES=900000 -> Bytes=1000; IN_BYTES=2000 + OUT_BYTES=900000 -> Bytes=2000, Packets=20 (99.8% of the session, the download direction, lost and unrecoverable); NSEL 231=5000/232=80000 -> Bytes=0, Packets=0. Volumes, top-N and capacity are understated for such exporters. The customer's all-zero TCP flags and heavy ESP are consistent with a firewall exporter, but its template was not captured, so field impact there is unconfirmed.

### X-07 — IE 85/86 cumulative totals overwrite a present IE 1/2 delta and are summed as per-export deltas

**Skeptic verdict:** upheld (high confidence), severity P2. The claim holds in the baseline. In assignV9Field (poller/internal/netflow/v9.go:394-411), `case 85: r.Bytes = uint64BE(value)` and `case 86: r.Packets = ...` assign with no guard. Compare IE 23/24, which only fill the fields when r.Bytes or r.Packets is still 0. The v9 path (v9.go:319) and the IPFIX path (ipfix.go:206) both call this function and walk fields in template order. So a template listing IE 1 then IE 85 ends up with Bytes set to the IE 85 value. Nothing downstream checks for this: the record carries no counter-kind flag. IANA defines IE 85/86 (octetTotalCount/packetTotalCount, the v9 IN_PERMANENT_BYTES/PKTS) as running totals since the metering process started, not per-export deltas. Because of that, every periodic export of a long-lived flow re-adds the whole running total when sum(bytes) runs, and templates that carry only totals are over-counted the same way. Two things …

**Evidence.** assignV9Field (v9.go:394-411) has `case 85: // octetTotalCount — 64-bit byte counter (BUG-06) r.Bytes = uint64BE(value)` and `case 86: r.Packets = ...`, assigned unconditionally. IANA IE 85/86 (v9 IN_PERMANENT_BYTES/PKTS) are running totals since metering start, not per-export deltas; 64-bit deltas already arrive as an 8-byte IE 1. A template carrying both IE 1 and IE 85 therefore stores the cumulative total, and each periodic export of a long-lived flow (Cisco permanent caches, firewall update events with total counters) is re-summed by every sum(bytes). Reproduced in a Go test: octetDelta=1000 followed by octetTotal=900000 gives Record.Bytes=900000.

### X-08 — Sampling-rate learning is wrong for common exporters: inline IE 34/305 ignored, sampler IDs ignored, only the first options record read, PSAMP misread, and samplers reset to 1 on restart

**Skeptic verdict:** upheld (high confidence), severity P2. I confirmed this by reading the code in the frozen baseline. It is a real defect.
1. **Inline sampling fields are ignored.** assignV9Field (v9.go:394-459) has no case for IE 34, 35, 48, 50, 302 or 305. A sampling interval sent inside a data record is dropped. Only the rate learned from options data is applied: parseV9DataSet gets it from cache.GetSampler (v9.go:189, ipfix.go:59).
2. **The rate is shared across the whole exporter.** v9SamplerKey (v9.go:130) is exporter/sourceID only, with no sampler or selector ID. An exporter running two samplers at different rates gets one rate, whichever was written last. Every flow is scaled by it.
3. **Only the first options record is read.** extractSampler (v9.go:371-392) takes row := body[:tpl.Length], so only the first record of an options data set is examined. Cisco sends a table of samplers with one record per sampler, so any sampler after the …

**Evidence.** assignV9Field (v9.go:394-459) has no case for SAMPLING_INTERVAL (34), SAMPLING_ALGORITHM (35), FLOW_SAMPLER_ID (48), selectorId (302) or samplingPacketInterval (305) in data templates; sampling is learned only from options data (v9.go:195-205, ipfix.go:65-73). The cache is keyed only by exporter+domain (v9.go:130 v9SamplerKey `"%s/%d"`), not sampler ID. extractSampler (v9.go:371-392) takes `row := body[:tpl.Length]`, i.e. only the first options record, and breaks on a variable-length (0xFFFF) field before the rate. isSamplerField treats 305 as 1-in-N although RFC 5476 sends 1-in-100 as 305=1 plus samplingPacketSpace(306)=99. Samplers are held only in memory (v9.go:108-123) and GetSampler defaults to 1, so after every collector restart sampled flows are stored unscaled until the next options export (often 10-30 min). Reproduced in Go tests: inline SAMPLING_INTERVAL=1000 -> SamplingInterval=1 (1000x under-report); field 34=512 -> 1; two samplers 1:100/1:1000, flow tagged sampler-id 2 -> 100 (10x under); PSAMP 305=1/306=99 -> 1 (100x under).

### X-09 — Flow counts and anomaly-detector thresholds ignore sampling (only bytes/packets are scaled), and sampling is never disclosed

**Skeptic verdict:** upheld (high confidence), severity P2. I checked this against the baseline and the claim holds. In poller/cmd/netflow-collector/main.go:148-153 the collector multiplies only record.Bytes and record.Packets by SamplingInterval. It does not create an extra record or apply any weight, so each stored row is one sampled flow record. In server/app/api/v1/netflow.py the detectors count those raw rows. The SYN scan query (around :2043) uses uniqExact(dst_addr) with HAVING dst_count >= 100. The RST query (:2075) uses HAVING total >= 1000, where total is count(). The ICMP query (:2139) uses HAVING flows >= 5000, where flows is count(). None of the thresholds is adjusted for sampling_interval. With 1:1000 packet sampling, a scan of 10,000 single-packet probes leaves about 10 records and about 10 distinct dst_addr values. That can never reach the >=100 threshold, and the endpoint then reports no anomaly and gives no sign that the …

**Evidence.** The collector multiplies only bytes and packets by N (main.go:150-153). Every API flow figure is count() of sampled records (e.g. overview :724, conversations :1028), so per-flow averages such as avg(raw_bytes) (:1037) are inflated relative to real flows. Detector thresholds count sampled records: SYN scan `HAVING dst_count >= 100` (:2043), RST `HAVING total >= 1000` (:2075), ICMP `HAVING flows >= 5000` (:2139). On a 1:1000 exporter a 10,000-probe scan leaves ~10 records and can never trigger, yet Anomalies reports no anomalies. sampling_interval is never selected or returned by any endpoint (grep finds it only in the corrupt guard at :606) and no Netflow*.tsx page references it. NF-21 covers ICMP threshold logic and FE-03 asks for disclosure; neither identifies unscaled counts or detectors blinded by sampling.

### X-10 — Exporter-supplied flow timestamps are trusted with no bound or skew detection: a skewed exporter's flows vanish from windows, fake 'offline', are TTL-dropped at insert, and can get whole batches rejected

**Skeptic verdict:** upheld (medium confidence), severity P3. I checked the frozen baseline and the mechanics hold up. applyFlowTimes (v9.go:332-340) stores absolute IEs 150-153 exactly as the exporter sent them. flowTimestamp (v5.go:135-141) derives flow time from the exporter's unixSecs and uptime, and only falls back to receivedAt when fields are missing or inconsistent. Nothing compares the result with the receipt time or limits it.
IPFIX passes sysUptimeMS=0 (ipfix.go:212), so IE 21/22 always fall back to receipt time. That one is actually harmless, since it errs toward the collector clock.
Every API window filters on `timestamp BETWEEN start AND end`. Overview last_seen is max(received_at), but only over rows inside that timestamp window (netflow.py:694-705 and 720-733). So for an exporter running 20 minutes fast, its fresh rows fall outside the window and last_seen ages to about 20 minutes. NetflowPage.tsx:550-556 shows 'offline' at 15 …

**Evidence.** flowTimestamp (v5.go:135-141) returns `time.Unix(unixSecs) - (sysUptime - flowUptime)` (exporter wall clock); applyFlowTimes (v9.go:332-340) stores absolute IEs 150-153 as sent. receivedAt is only a fallback; nothing compares exporter time with receipt time and no skew metric exists. Everything keys on this value: PARTITION BY toYYYYMMDD(timestamp), `TTL toDateTime(timestamp) + INTERVAL 30 DAY`, every API window `timestamp BETWEEN start AND end` (netflow.py:731), /exporters and overview last_seen, and the healer's HEAL_MIN_AGE_HOURS=2 'late flows' assumption (netflow_rollup.py:20-21,51). Reproduced: exporter 20 min ahead delivering every minute -> overview 1h last_seen age 20 min -> UI 'offline', and its newest 20 min excluded from every relative window; exporter 3 h behind -> stored but a 1h window returns 0 rows; router clock 2002-03-01 / flowEnd 2001-01-01 -> silently discarded at INSERT by the TTL while `ingested` still increments; one INSERT block spanning 101 days fails with Code 252 TOO_MANY_PARTS (max_partitions_per_insert_block=100) before TTL filtering, so a whole mixed-exporter batch is rejected and then discarded (NF-06). For an exporter lagging >= 2 h the healer treats a still-receiving hour as settled; its ALTER ... DELETE then separate INSERT..SELECT (netflow_rollup.py:206-214) double-counts raw rows that land between the two. Also IPFIX passes sysUptimeMS=0 …

### X-11 — Rollup healer rebuilds hourly rollups from TTL-truncated raw data whenever raw retention is below 27 days, permanently destroying rollup history

**Skeptic verdict:** upheld (high confidence), severity P2. I checked this against the frozen baseline and the defect is real.
- **The heal ceiling ignores the configured TTL.** In netflow_rollup.py, `HEAL_MAX_AGE_HOURS = 27*24` is fixed. The docstring says hours are left alone once raw data may be TTL-truncated, which assumes the raw TTL is 30 days. Nothing reads the live TTL.
- **Admins can lower the raw TTL well below 27 days.** In storage_service.py, `CATEGORIES['flows']` sets `min_days` to 3. `classify_table` maps `flow_records` to 'flows'. `apply_retention` → `_apply_ttl_sync` then issues `MODIFY TTL toDateTime(timestamp)+toIntervalDay(days)` on `flow_records`, clamped only to that 3-day minimum. So 3 to 26 days is a supported setting.
- **The rollups keep much longer history.** `flow_conversations_1h` and `flow_qos_1h` end in `_1h`, so they fall under the rollups_1h category (400 days by default, 30 minimum). They are meant to outlive raw …

**Evidence.** `HEAL_MAX_AGE_HOURS = 27 * 24` (netflow_rollup.py:52) hard-codes a 30-day raw retention (docstring :22-24: 'rebuilding from partial raw would destroy history'), but raw TTL is admin-configurable: storage_service.py:88-91 'flows' default 30, min 3 days; PUT /retention (storage_management.py:78); apply_retention (storage_service.py:316-331) runs MODIFY TTL on flow_records. Below 27 days, TTL merges leave boundary hours partially deleted; _plan (:168-178) sees have != raw_count for an hour aged <= 27 days and schedules 'heal'; _current_action (:199-203) confirms and _converge_hour (:206-214) runs ALTER ... DELETE then INSERT from the partial raw. Reproduced on scratch ClickHouse with the real 071 DDL and raw TTL 5 days: 60 flows inserted into the hour straddling the cutoff, MV wrote 60 flows/60,000 bytes, ClickHouse kept 32 raw rows (TTL also applied at insert); running the real module functions produced plan=[('2026-09-25 17:00:00','heal')] and the rollup afterwards held 32 flows/32,000 bytes — 47% of that hour's permanent history gone, repeating at the TTL edge as time advances.

### X-12 — Template cache lets a stale data template shadow a re-used options template ID (options records decoded as garbage flows), and all IPv6 exporters share one template key

**Skeptic verdict:** upheld (high confidence), severity P2. Checked against the baseline code; the defect is real.
1. Kind switch. In v9.go, Put (81-85) and PutOptions (99-103) write two separate maps and neither removes the same key from the other. ParseV9 checks cache.Get (data templates) first at line 188, with a 24h TTL, and only then GetOptions. ParseIPFIX does the same (ipfix.go ~58-65). So if an ID was a data template and is later re-advertised as an options template, the stale data template still wins. The options records are then decoded as flows and the sampler is never learned. Nothing upstream catches this except sane(), and the reported repro already passes sane(). It only happens when an exporter re-uses an ID for a different template kind within 24h, for example after a reboot or reconfiguration that assigns IDs in a different order. That is plausible but uncommon.
2. IPv6 exporters share one key. The collector listens with …

**Evidence.** Put (v9.go:81-85) and PutOptions (v9.go:99-103) write separate maps and never evict the same exporter/domain/ID from the other. ParseV9 checks cache.Get (data templates) first (v9.go:188) and GetOptions only on a miss (v9.go:195), with a 24 h TTL; ParseIPFIX has the same order (ipfix.go:58-65). When an exporter reboots/reconfigures and re-uses a former data-template ID for an options template, its options records are decoded as traffic for up to 24 h. Reproduced (TestMissedTemplateKindSwitch): options data for re-used ID 300 decoded as a flow (src=0.0.0.0 dst=1.2.0.0 bytes=65,565,537 packets=1,836,084,325 proto=114) that passes sane(), and the sampler was not learned. Separately v9.go:157-160 and ipfix.go:34-37 replace any non-IPv4 exporter with 0.0.0.0 before using it as the cache key, so all IPv6 exporters share template keys and template 256 from exporter A decodes exporter B's data. IPFIX template withdrawals (field count 0) are ignored. NF-32 notes the 0.0.0.0 substitution, not this cross-exporter template contamination.

### X-13 — Writer stall collapses UDP intake: backpressure waits 250 ms per record on the single reader, and kernel socket drops are neither prevented nor counted

**Skeptic verdict:** upheld (high confidence), severity P2. I checked the frozen baseline and the claim holds.
- **Single reader does all the work.** In poller/cmd/netflow-collector/main.go, runUDP (lines 104-158) is the only goroutine reading the socket. It parses each packet inline and calls enqueue once per record.
- **The wait is per record, not per packet.** When the channel is full, enqueue (lines 164-184) starts a fresh time.NewTimer(BackpressureWait) for every record and prints a WARN line for every record it drops. BackpressureWait defaults to 250 ms (line 399, NETFLOW_BACKPRESSURE_MS), and the queue holds BatchSize*4 records (line 61). A 30-record v9/IPFIX datagram therefore blocks the reader for about 7.5 s. That matches the reported 7.531 s repro.
- **Nothing stops or bounds a writer stall.** runWriter/insert calls PrepareBatch and Send with the root context and no per-call timeout.
- **Kernel drops are never prevented or counted.**
 …

**Evidence.** runUDP (main.go:121-158) parses and enqueues inline in the only reader goroutine. Once the 4000-record channel fills, enqueue (main.go:164-184) arms a new time.NewTimer(BackpressureWait=250ms) for every record and logs a WARN per dropped record (179). c.insert (239/262) has no per-call timeout, so a hung ClickHouse holds the writer up to driver defaults (DialTimeout 30 s, ReadTimeout 300 s; clickhouse-go v2.23.0). Reproduced (TestMissedBackpressurePerRecordStall): one 30-record datagram blocked the reader 7.531 s and wrote 30 log lines. No SetReadBuffer; this box's net.core.rmem_default/rmem_max are 212,992 bytes (~100 datagrams), so during the stall the kernel discards almost everything. /health (main.go:367-378) reports only records_dropped_queue_full and status 'ok' — no UDP RcvbufErrors, no sequence-gap accounting (flow_sequence stored but never evaluated), no last-insert error. NF-06 covers batch discard, not this loss amplification.

### X-14 — Collector exits instead of listening when ClickHouse is unavailable at startup

**Skeptic verdict:** upheld (high confidence), severity P3. Confirmed in baseline poller/cmd/netflow-collector/main.go:45-51: clickhouse.Open and conn.Ping failures call fatalf before runHealth, runWriter and runUDP start, so while ClickHouse is unreachable the collector crash-loops and never binds UDP :2055 or the health port. The unit file (poller/systemd/zenplus-netflow-collector.service) uses Restart=always and RestartSec=10, with no After= or Wants= on clickhouse-server. At boot, or after a ClickHouse restart that overlaps a collector restart, it loops until ClickHouse answers, and it only rebinds up to 10s after ClickHouse is back. The practical impact in baseline is smaller than the claim suggests. When ClickHouse is down during normal running, runWriter's flush logs 'insert failed' and drops the batch anyway (no spool). So flows sent during the outage are lost whether or not the socket is bound. The extra harm from this defect is: (a) up …

**Evidence.** main.go:44-51: fatalf on clickhouse.Open / conn.Ping failure happens before runUDP binds the socket. Reproduced: with ClickHouse unreachable the process exits 1 with 'ping clickhouse: ... connection refused' and UDP 2055 is not bound. With systemd Restart=always / RestartSec=10, every flow exported while ClickHouse is down or restarting is refused by the kernel and counted nowhere — a restart-order outage NF-06's failed-batch fix does not cover.

### X-15 — NetFlow data is exposed unscoped outside /netflow: link-utilization NetFlow overlay and report traffic sections aggregate all flow_records for scoped users

**Skeptic verdict:** upheld (high confidence), severity P2. This is real, based on reading the baseline code. link_utilization.py never imports or calls app.core.scoping. link_detail (line 820) only depends on get_current_user. It loads device_interfaces JOIN devices for any device_id, so it returns the hostname, IP and interface data. It then queries zenplus.flow_records WHERE exporter_ip = the device IP to build the NetFlow timeseries, talkers and protocols. Nothing checks the user's scope first. The router is included in main.py with no router-level dependency, so nothing else guards it. Scoping is a live feature elsewhere: devices.py filters with scoping.visible_tags and jsonb_tags_visible, and reports.py scopes executive, technical and business with visible_tags. That makes the missing check here a real inconsistency, not a design choice. The report section engine route is actually /reports/render/{key}, not /view (a small citation slip). …

**Evidence.** link_utilization.py contains no scoping or visible_tags call: link_detail (820-850) returns the device hostname, interface data and a NetFlow overlay (1002-1094: timeseries, top talkers, protocols from flow_records) for any device id. GET /reports/view/{key} (reports.py:392-438) never computes visible_tags, and SectionCtx (report_sections.py:135-195) carries no scope, so traffic_kpis, traffic_trend, traffic_protocols, traffic_ports and traffic_talkers (556-680) aggregate all flow_records for scoped users. NF-04's fix targets the /netflow endpoints; these readers would keep leaking after it.

### X-16 — Exporter-mapping writes bypass scope (device hostname oracle) and will become a visibility-escalation path once NF-04 keys visibility on exporter ownership

**Skeptic verdict:** upheld (medium confidence), severity P3. This is confirmed in the baseline, but its current impact is small. set_exporter_device (netflow.py ~2425) is gated only by require_operator_user, which lets through OPERATOR_ROLES or roles with devices.manage/superuser (security.py:174). It then looks up the device with a plain `select(Device.id, Device.hostname).where(Device.id == body.device_id)`, with no scoping.visible_tags or entity_visible check, and returns device_hostname. devices.py:490/528 does apply entity_visible and returns 404 for the same device. So a tag-scoped operator can bind any exporter to a device outside their scope, and a later ip-match miss causes _resolve_interface_names (~1476) to use that device's interface names in flow displays. delete_exporter_device runs the DELETE, always writes an audit row and returns 204, even when the DELETE removed no row.
Why the practical impact is low today:
(1) The "hostname …

**Evidence.** set_exporter_device (2425-2462) runs `select(Device.id, Device.hostname).where(Device.id == body.device_id)` with no scoping check and returns device_hostname, while GET /devices/{id} returns 404 for the same out-of-scope device (devices.py:490; reproduced 404 for nfv-rtr-b). The only gate is require_operator_user (devices.manage), not a NetFlow permission. delete_exporter_device (2465-2480) returns 204 and writes an audit row even when no mapping existed. Once NF-04 derives visibility from exporter->device ownership, whoever can write a mapping decides who sees that exporter's traffic — a write-side escalation NF-04's read-side fix does not address.

### X-17 — Saved-view payload is unvalidated and unbounded server-side (multi-MB query dict accepted and broadcast to every user's list); empty/whitespace names accepted; list unpaginated

**Skeptic verdict:** upheld (high confidence), severity P3. I checked this against the baseline code and the claim holds. In netflow.py (lines 2200-2204), SavedViewIn has `name: str = Field(..., max_length=120)` with no min_length and no stripping, and `query: dict` has no schema and no size limit. Nothing else stops a large or blank submission. The model (NetflowSavedView.query) is a plain JSON column, and name is String(120) NOT NULL, which still allows '' and '   '. The router has no dependencies of its own. Create and list only require get_current_user, with no permission check and no size check. The nginx site config (/etc/nginx/conf.d/zenplus.conf:75) sets client_max_body_size to 32m, so a body of about 2.2 MB gets through, which matches the reported repro. list_saved_views selects every row with no owner filter and no limit, so every user's list includes each stored query in full. Why P3 and not P2: the caller must be logged in. The …

**Evidence.** SavedViewIn (2200-2204) is `name: str = Field(..., max_length=120)` with no min_length or strip, and `query: dict` has no schema or size bound. list_saved_views (2312-2330) returns every row's full query to every user with no limit. Reproduced: a 2,268,917-byte JSON body from a role without netflow.view was accepted (200) and stored (length(query::text)=2,268,890); that row is then sent in every user's list response. Names '' and '   ' were accepted. NF-34 covers the browser prompt's missing whitespace check; the server-side absence of any validation or size bound is a separate failure (storage/list amplification).

### X-18 — Unknown or stale filter values (app, netclass, tcpflag, group) are silently dropped, returning whole-network totals under a filter chip (fail-open)

**Skeptic verdict:** upheld (high confidence), severity P2. I read the baseline code and ran it. scope_params (lines 502-528) accepts app, netclass, tcpflag and group as free strings with no validation (no Literal type, regex or allow-list). In _scope, each of these adds a clause only when the value matches a known entry. Application falls through when no branch matches. net_class adds nothing on a miss unless the value is 'Public'. tcp_flag does dict.get() and then `if flag_clause`. group adds nothing when _group_predicate returns None for an unknown group. No branch raises an error or fails closed.
I imported the baseline module and called _scope with application='NoSuchApp', net_class='Bogus', tcp_flag='bogus' and group='Gone' (with an empty IP-group list). Each call returned ('', {}), meaning an empty filter clause. So the query runs over the whole network.
The UI makes it worse. NetflowPage.tsx reads netclass and tcpflag straight from the …

**Evidence.** In _scope: application (618-641) adds a clause only for known bucket names; net_class (642-651) does nothing when _NET_CLASS_RANGES.get() misses and the value is not 'Public'; tcp_flag (652-663) `flag_clause = {...}.get(tcp_flag)` then `if flag_clause:`; group (668-671) `if pred:` after _group_predicate returns None for an unknown/renamed group (437-439). None returns a 4xx. Reproduced twice on the dev API: /overview with app=NoSuchApp, app=Web, tcpflag=bogus, tcpflag=synonly, netclass=Bogus, netclass='Private 10' or group=NoSuchGroup all returned byte-identical unfiltered totals (e.g. 54,291,339,390 bytes), while the real app='Web (HTTP/HTTPS)' returned 82,236,383. Saved views and shared links carrying a renamed IP group or retired app bucket therefore show network-wide numbers while the chip says the data is filtered; this fail-open matters more once filters interact with NF-04 scoping.

### X-19 — Interface filter is a bare ifIndex, not (exporter, ifIndex): selecting one interface mixes every exporter's same-index traffic, and the Link Utilization deep link drops its exporter

**Skeptic verdict:** upheld (high confidence), severity P2. I checked this in the baseline code and the claim holds. Two separate problems:
1. **The interface filter ignores the exporter.** In `_scope` (netflow.py:611), the filter is only `(input_snmp = iface OR output_snmp = iface)`. There is no exporter condition unless the caller also passes `exporter`, and the top-level `/netflow` page does not.
   - The Top Interfaces query groups rows by `(exporter_ip, ifindex)` (around line 1573), so the list shows one row per exporter and interface.
   - Clicking a row runs `onSelect(active ? null : it.ifindex)`, which sends only the ifIndex.
   - `setIfaceFilter` (lines 286-291) writes only the `iface` parameter.
   - The highlight check (`activeIface === it.ifindex`) and `activeIfaceLabel` (`find` on ifindex, lines 560-564) also compare only the ifIndex.
   - Result: on the all-exporter view, clicking "127.0.0.62 ifIndex 2" filters, highlights and …

**Evidence.** _scope emits `(input_snmp = %(iface)s OR output_snmp = %(iface)s)` (611) with no exporter term. Top Interfaces lists rows per (exporter, ifIndex) (1573 GROUP BY exporter_ip, ifindex), but a click calls `onSelect(active ? null : it.ifindex)` (NetflowPage.tsx:2498), setIfaceFilter (286-291) writes only iface, highlight compares `activeIface === it.ifindex` (2491) and activeIfaceLabel (560-564) picks the first matching row. Reproduced on the dev API: /interfaces lists 127.0.0.62 ifIndex 2 and 127.0.0.63 ifIndex 2; /overview?hours=24&iface=2 returned exporters=2 and bytes 31,979,663,479 = both exporters' ifIndex 2 combined; /exporters?iface=2 listed both. LinkUtilizationPage.tsx:1416 links to `/netflow?exporter=<ip>&iface=<idx>`, but NetflowPage takes exporter only from the device-route prop (276, 384), so 'Open in NetFlow explorer' lands on the all-exporter view filtered by a bare ifIndex with no exporter chip and the default 1h range.

### X-20 — UI always sends `hours` (le=720), so custom windows over 30 days and calendar-range URLs 422 on every widget, and calendar keys otherwise query a trailing window

**Skeptic verdict:** upheld (high confidence), severity P2. This is real. I checked it against the baseline code but did not re-run it: fastapi is not installed in the scratch Python, so I could not replay the 422 myself. The claim does not depend on that, because FastAPI validates `Query(le=720)` before the handler runs.
1. **Page always sends `hours`.** In `NetflowPage.tsx`, `rangeQS` (line 379), `priorQS` (365) and `interfacesQS` (400) all start from `new URLSearchParams({ hours: String(hours) })`.
2. **Custom and calendar ranges compute `hours` from the span.** In `TimeRangePicker.tsx:141-143`, `hours = round((to-from)/3600000)` for custom and calendar ranges.
3. **Nothing caps the span.** The custom picker's `apply()` only checks that both dates are present, valid and ordered, so Aug 1 to Sep 1 gives `hours=744`.
4. **Every endpoint rejects it.** They all declare `hours: int = Query(..., ge=1, le=720)` (netflow.py 678, 767, 923, ... 2485). …

**Evidence.** rangeQS/priorQS/heatmapQS/interfacesQS start from `new URLSearchParams({ hours: String(hours) })` (NetflowPage.tsx:379), where for custom/calendar ranges hours = round(span/3600) (TimeRangePicker.tsx:141-143); Forensics (69-70), Anomalies (44-45), Capacity (37) and Section pages do the same. Every endpoint declares `hours: Query(le=720)` (e.g. netflow.py:678, :767, :1398, :2485), validated before _resolve_window gives from/to precedence. Reproduced on the dev API: /overview, /timeseries, /heatmap and /exporters with hours=744 (or 1080) plus from/to return 422 'Input should be less than or equal to 720'; the same without hours (or hours=720) returns 200. A normal custom 'Aug 1 -> Sep 1' (744 h) therefore fails, the overview shows zeros and 'No NetFlow records' (NF-03 masking), and the 90-day rollups are unreachable from the UI — contradicting NF-31's premise that arbitrary custom spans are accepted. Calendar keys reachable by URL: range=last_month|quarter send hours=744/~2184 with no from/to -> 422; others (range=month) send only hours, so the server queries the trailing N h ending now while the page is labelled 'Last month'/'This month'.

### X-21 — Forensics page crashes to the ErrorBoundary on any FastAPI 422 (e.g. non-numeric port) because it renders the detail array as a React child

**Skeptic verdict:** upheld (high confidence), severity P2. I checked this against the baseline code. NetflowForensics.tsx:213 renders `{(flows.error as any)?.response?.data?.detail || ...message}` directly as a JSX child. The /forensics endpoint (netflow.py:1803-1805) declares src_port, dst_port and min_bytes as `int | None = Query(...)`. The Source port, Destination port and Min bytes fields are plain text inputs (Field has no type=number, only inputMode). qs (lines 74-77) forwards them raw. Nothing in the server defines a RequestValidationError handler: a grep of server/app finds no match. So a non-integer value gets FastAPI's default 422, where detail is an array of {type, loc, msg, input} objects. The axios response interceptor in lib/api.ts only records headers and handles 401, so it passes that detail through unchanged. React throws 'Objects are not valid as a React child' when given an array of plain objects. Layout.tsx:310 wraps the …

**Evidence.** NetflowForensics.tsx:213 renders `Query failed: {(flows.error as any)?.response?.data?.detail || ...}`. For a 422 FastAPI returns detail as an array of objects; React throws 'Objects are not valid as a React child (found: object with keys {type, loc, msg, input})' and Layout's ErrorBoundary (Layout.tsx:310) replaces the page, losing all typed filters. Port/DSCP/min-bytes are plain text inputs (Field 299-310, inputMode only) forwarded raw by qs (74-78), and once armed every keystroke refetches (NF-11). Reproduced with the dev API and Playwright: src_port=a, dst_port=abc, dst_port=443,80, min_bytes=1e6 and dscp=EF each return 422 int_parsing and crash the page. The section page already uses apiErrorMessage (lib/utils.ts:113-119), which flattens 422 arrays. NF-11/NF-31 describe the string 400 path, not this crash.

### X-22 — Forensics shows 'No flow records matched these filters' while the query is still loading or retrying

**Skeptic verdict:** upheld (high confidence), severity P3. I checked this against the frozen baseline and the defect is real.
In dashboard/src/pages/NetflowForensics.tsx, lines 82-85 define `useQuery({queryKey: ['netflow','forensics',qs,armed], enabled: armed})`. There is no placeholderData or keepPreviousData. The render chain at lines 207-218 goes: `!armed` → `flows.error` → `(flows.data?.length ?? 0) === 0` → "No flow records matched these filters." Nothing in that chain checks isPending or isFetching.
There are three cases where data is undefined and error is null, so the page shows the false empty message:
- **First search:** once armed=true the query is pending, so the empty card appears while it runs.
- **Changed query key:** any change to qs produces a new key with no cached data, so the same thing happens.
- **Retry:** main.tsx:14 sets `retry: 1`, so during the retry delay after a first failure, error stays null and the empty message …

**Evidence.** The render chain is !armed -> error -> `(flows.data?.length ?? 0) === 0` -> empty card (NetflowForensics.tsx:207-218). While pending, or during TanStack retries (error stays null until final failure), data is undefined so the page asserts no records matched; the only loading hint is a small header spinner (204). Playwright with a 4 s delayed response showed 'No flow records matched' while the request was in flight. NF-03 lists Overview, Anomalies, Capacity and Saved Views, not Forensics, so a fix scoped to those files would leave this page asserting false negatives during an investigation.

### X-23 — Compare overlay joins prior points by exact timestamp, so custom spans that are not a bucket multiple draw no prior trace while still showing a % delta

**Skeptic verdict:** upheld (high confidence), severity P3. I checked the baseline code and the claim holds. In NetflowPage.tsx:507-515, mergedSeries keys each prior point by `p.ts + span`, where span = toMs - fromMs, and then looks up each current point by its exact `p.ts`. The server (netflow.py:776-801) sets the bucket to 60, 300, 1800 or 7200 s depending on span, and returns ts = toStartOfInterval(timestamp, bucket), so every ts sits on an epoch multiple of the bucket in both the current and prior windows. So `prior_ts + span` can only equal a current ts when span % bucket == 0.
Nothing upstream prevents a mismatched span:
- The priorQS (364-374) shifts the window back by exactly the same span.
- The custom picker (TimeRangePicker.tsx:317/330) is a datetime-local input with minute granularity.
- Compare (compare=1, line 293) is not blocked for custom ranges.
For example, 10:00 to 17:03 is a 7h03m span with 300 s buckets; 25380 % 300 = 180, …

**Evidence.** mergedSeries (NetflowPage.tsx:507-515) does `priorByTs.set(p.ts + span, p.bps)` then `priorByTs.get(p.ts)`, with span = toMs - fromMs, while server buckets are epoch-aligned via toStartOfInterval (netflow.py:776, :800). When span % bucket != 0 no key ever matches, yet compareDelta (517-523) still sums both series and shows a '+x.x%' badge. Node simulation: custom 10:00->17:03 (300 s buckets) plots 0/86 prior points; 2d23h17m (1800 s) 0/144; 33h57m (122,220,000 ms ≡ 120,000 mod 300,000) all null; 7h05m 86/86. A separate mechanism from NF-02's window/filter issues that would survive an NF-02 fix keeping the exact-timestamp join.

### X-24 — Overview exporter health badge is frozen by useMemo and never ages to stale/offline on an open page

**Skeptic verdict:** upheld (high confidence), severity P2. Confirmed in baseline dashboard/src/pages/NetflowPage.tsx:550-556. `health` is a useMemo that calls Date.now() inside it, and its only dependency is [overview.data?.last_seen]. The backend (server/app/api/v1/netflow.py:728/756) returns last_seen as max(received_at) within the query window. Once exporters go silent, every 15 s refetch (pollMs, lines 271-274 and 412-416) returns the same ISO string, so the dependency never changes and the memo never runs again. Nothing else keeps it current: there is no now/tick state, no setInterval and no serverNow in the deps. Re-renders from other queries reuse the cached value. The badge at line 682 stays 'receiving' (or whatever it last computed) until last_seen falls out of the relative window and becomes null, and then it jumps to 'waiting'. It never reaches 'stale' or 'offline' on a page left open. Reloading the page or changing the range fixes …

**Evidence.** NetflowPage.tsx:550-556: `const health = useMemo(() => { ... const age = Date.now() - new Date(overview.data.last_seen).getTime() ... }, [overview.data?.last_seen])`. When an exporter goes silent the last_seen string stays identical across every 15 s refetch, so the memo never re-runs: the badge keeps 'receiving' until the last flow leaves the window (up to 1 h default, 7 days on 7d), then jumps to 'waiting', never 'stale' or 'offline' on a wall screen. The DeviceStatusCard dots (1997) compute age every render, so the same page shows a red dot next to a green 'receiving' badge; the badge also uses raw Date.now() while relativeTime uses the calibrated serverNow(). Contradicts NF-01's own acceptance (state transitions after genuine silence) by a separate mechanism.

### X-25 — Raw 30-day TTL vs 90-day rollups: whether a historical window shows data depends on which source each widget or filter uses, and expiry is shown as zero traffic

**Skeptic verdict:** upheld (high confidence), severity P3. I checked this against the frozen baseline. I did not reproduce it with data, because TTL would drop the expired rows. The DDL and code are enough to confirm the mismatch.
- **Retention:** scripts/migrate-20260506-netflow-clickhouse.sql:36 gives flow_records TTL 30 DAY. migrate-071:42 (flow_conversations_1h) and migrate-072:31 (flow_qos_1h) give TTL 90 DAY. No later migration or code changes the flow_records TTL.
- **Custom windows are not limited:** _resolve_window (netflow.py:36-56) accepts any custom from/to without a lower bound or retention clamp. Nothing in netflow.py or NetflowPage.tsx mentions retention.
- **Source choice:** _rollup_eligible and _qos_rollup_eligible (537-574) pick a source per request based only on span and filters, never on age.
- **Resulting behaviour for a 96 h window 40 days ago:**
  - Overview (687), protocols (736) and the other rollup paths return …

**Evidence.** flow_records has TTL 30 days (migrate-20260506:36); flow_conversations_1h and flow_qos_1h keep 90 days (migrate-071:41, migrate-072:32). Eligibility (netflow.py:537-551, 562-574, 776-778) picks the source per widget: heatmap always raw (1409-1417), /timeseries raw for 48-168 h spans, /exporters always raw (2503). For a 4-day custom window 40 days ago, overview, protocols and talkers (rollup) show totals while the throughput chart, heatmap and exporter list are empty; adding a DSCP/iface/TCP-flag chip turns every total to zero; a 1M Compare with an iface filter has an empty prior. Nothing distinguishes 'expired' from 'no traffic'. Derived from DDL and code (not reproduced, since TTL drops expired rows). NF-17 covers hour-floor edge inclusion; this is a retention mismatch across sources.

### X-26 — Capacity attributes a flow's whole byte count to the 5-minute slot where it ended (no duration spreading), turning long flows into false utilization spikes

**Skeptic verdict:** upheld (high confidence), severity P2. I checked this against the frozen baseline and the defect holds. In applyFlowTimes (v9.go:332-346, also used by ipfix.go:212) and in v5.go:97, the flow's Timestamp is set to its end time (flowEnd, or LAST_SWITCHED). FirstSwitchedMS is stored but never used for timing. The capacity SQL (netflow.py:1947-1966) groups by toStartOfInterval(timestamp, 300s) and computes slot_bps = sum(bytes)*8/300. So all of a flow's bytes are counted in the 5-minute slot where it ended. Nothing in the poller or the API spreads long flows across the slots they covered: grepping for split/spread/duration only finds duration used as a display or average metric. The capacity query also reads raw flow_records, not a rollup that could have fixed this. The fixture numbers follow directly from that formula: a 30-minute flow at 8 Mbps is 1.8e9 bytes, and 1.8e9*8/300 = 48,000,000 bps in one slot. That makes max = p95 …

**Evidence.** Flow timestamp is last-switched/flowEnd (v9.go:332-340 applyFlowTimes; v5.go:97). Capacity buckets `toStartOfInterval(timestamp, INTERVAL {bucket} SECOND)` and divides by 300 (netflow.py:1953-1956) although first_switched_ms/last_switched_ms are stored. With long active timeouts (Cisco IOS default 30 min) one long flow becomes a spike. Fixture F7: a 30-minute 8 Mbps flow (1.8e9 B) gives p95 = max = 48,000,000 bps and 480% utilization on a 10 Mbps link; a prototype spreading SQL gives max 8,000,000 and avg 4,000,000 over the hour. NF-13 covers partial edge slots and slot occupancy, not per-flow attribution.

### X-27 — Capacity ranks by absolute p95 bps with LIMIT 50, so small saturated circuits drop out of the list and out of the Critical/Warning counts

**Skeptic verdict:** upheld (high confidence), severity P2. I confirmed this in the baseline code. In server/app/api/v1/netflow.py, netflow_capacity (around lines 1937-1975) sums traffic per (exporter, ifindex), computes quantile(0.95) of slot_bps, then does ORDER BY p95 DESC LIMIT %(limit)s inside ClickHouse. That happens before any speed lookup (_resolve_interface_names), so rows can only be ranked by absolute bps, never by utilization. No mitigating path exists. The endpoint has no sort parameter and no utilization filter, and it returns no totals or coverage counts. NetflowCapacity.tsx sends limit=50 (the endpoint maximum is 100). It fills the Critical/Warning/Healthy/Unknown cards by looping only over the returned rows. No other endpoint or UI path supplies full-population counts. A concrete case: a site with 50 or more interfaces whose p95 is above 9.5 Mbps (common for 1G/10G LAN uplinks, since in and out traffic are added together per …

**Evidence.** `ORDER BY p95 DESC LIMIT %(limit)s` (netflow.py:1973-1974) with the UI requesting limit=50 (NetflowCapacity.tsx:37); summary cards count only the returned rows (48-54). A 10 Mbps WAN at 95% (p95 9.5 Mbps) is excluded whenever 50 LAN interfaces exceed 9.5 Mbps, so the page can show 'Critical 0' with a saturated circuit. Nondeterministic quantile() over long windows (NF-13) also makes Top-N membership change between refreshes. The review's Capacity UI direction notes counts cover 50 rows, but not that the ranking dimension is wrong for a capacity page.

### X-28 — flowDirection (IE 61) is not decoded, so exporters monitoring both ingress and egress double-count in capacity and every NetFlow total

**Skeptic verdict:** upheld (medium confidence), severity P3. Confirmed in the baseline. assignV9Field (v9.go:394-460) has no case for field 61 (flowDirection), and the IPFIX path reuses it, so the direction value is thrown away. Nothing in the pipeline can make up for this: flow_records is a plain MergeTree with no direction column (migrate-20260506 and the collector's CREATE in main.go:323), there is no dedupe engine, and neither the capacity UNION ALL (netflow.py:1957-1968) nor the other sums have any direction filter. When an exporter monitors the same traffic at both ingress and egress (for example Cisco Flexible NetFlow applied with both input and output), it emits two nearly identical records per flow (same input/output ifIndex, bytes and packets). The only thing telling them apart is flowDirection 0 vs 1, and the collector drops it, so every total and the per-interface capacity figures come out at about 2x. Why this is P3 and not P2: the …

**Evidence.** assignV9Field (v9.go:394-460) handles fields 1,2,4-17,21-24,85,86,150-153 but not 61 (DIRECTION/flowDirection); IPFIX reuses it (ipfix.go:206). flow_records has no direction column (migrate-20260506 DDL). Egress-monitored duplicates of ingress records cannot be identified, so the capacity UNION ALL (netflow.py:1957-1968) and all sums count such packets twice. FE-12 discusses collection points in general and NF-14 same-index double counting, not this concrete decode gap.

### X-29 — Conversation details pair every exporter with every ifIndex, naming interfaces on the wrong device (phantom In/Out ports)

**Skeptic verdict:** upheld (high confidence), severity P2. I checked this by reading the frozen baseline code. I did not run a ClickHouse repro, but the code leaves no room for doubt.
Both queries collect exporters and interface indexes into separate lists, so the link between an exporter and its own ifIndex is lost:
- Raw query (netflow.py ~1029-1031): `groupUniqArray(10)(exporter)`, `groupUniqArray(10)(input_snmp)` and `groupUniqArray(10)(output_snmp)`.
- Rollup query (~985-987): `groupUniqArray(10)(toString(exporter_ip))`, `groupUniqArrayMerge(10)(input_snmp)` and `groupUniqArrayMerge(10)(output_snmp)`, grouped only by src/dst/protocol/dst_port.
`_format_conversations` then pairs every exporter with every index:
- `iface_keys` at ~1099: `for ip in exporters for idx in indexes`.
- `input_interfaces` / `output_interfaces` at 1122-1133: `for ip in r[8] for idx in r[9]` and `for ip in r[8] for idx in r[10]`.
Nothing downstream filters out the …

**Evidence.** Raw path aggregates `groupUniqArray(10)(exporter)`, `groupUniqArray(10)(input_snmp)`, `groupUniqArray(10)(output_snmp)` independently (netflow.py:1029-1031); rollup path likewise (985-987). _format_conversations then rebuilds pairs as a cross product: iface_keys at 1099 `for ip in exporters for idx in indexes` and 1122-1133 `for ip in (r[8] or []) for idx in (r[9] or [])`. Reproduced: a conversation seen by 10.0.0.1 on ifIndex 3 and 10.0.0.2 on ifIndex 7 returned input_interfaces [(10.0.0.1,3),(10.0.0.1,7),(10.0.0.2,3),(10.0.0.2,7)]; two are fabricated and _resolve_interface_names turns them into real SNMP names shown by ConversationRecordedOn (NetflowPage.tsx:2132-2176) as '@ <hostname>'. Happens whenever a pair is observed at more than one collection point (FE-12).

### X-30 — Top Conversations (raw and hourly rollup) fragment response traffic into one row per ephemeral client port

**Skeptic verdict:** upheld (high confidence), severity P2. In the baseline, netflow_top_conversations groups by (src_addr, dst_addr, protocol, dst_port) on both paths. The raw path is at server/app/api/v1/netflow.py about lines 1015-1067, in both the inner ranking subquery and the outer GROUP BY. The rollup path is at about 973-1005. The rollup table key from migrate-071 (ORDER BY/GROUP BY ... dst_port) is the same. Nothing normalises flow direction anywhere: the collector (poller/cmd/netflow-collector/main.go:268-281) inserts SrcPort/DstPort exactly as exported, and neither the query nor _format_conversations swaps or merges directions. So the server-to-client half of each TCP connection gets its own row per ephemeral client port, while the client-to-server half collapses into one dst_port=443 row. I reproduced this on scratch ClickHouse (nfsk_X30) with the same GROUP BY: 10 HTTPS connections, each carrying 5 MB of response traffic, filled all …

**Evidence.** Both the raw path (1067) and the rollup path (1005) GROUP BY src, dst, protocol, dst_port, and the rollup key is the same (migrate-071 ORDER BY ... dst_port), so response halves split by client port. Reproduced: 10 of the top 12 conversations were the same 93.184.216.34 -> 10.0.0.5 HTTPS download, each keyed by a different client port (TCP/5000x, 'Other'), while the request direction was a single low-ranked row. NF-20 covers the service label on responses; this is the grouping key distorting the ranking itself.

### X-31 — SYN-scan detector cannot see vertical port scans and applies one fixed count threshold regardless of window length

**Skeptic verdict:** upheld (high confidence), severity P3. Checked against the baseline server/app/api/v1/netflow.py, lines 2027-2059. The SYN-scan query runs `GROUP BY src HAVING dst_count >= 100`, where dst_count is uniqExact(dst_addr). port_count is only used in the description string and never in the HAVING condition or severity. So a single-target vertical scan always has dst_count = 1 and can never produce a finding, however many ports it probes. No other detector in the anomalies battery looks at distinct ports: rst_flood, sensitive_egress, icmp_flood and volumetric_outlier are the only other kinds, and a grep for port_scan/vertical found nothing. The one partial overlap is rst_flood. It could flag the victim if the exporter also sees the victim's RST replies and there are at least 1000 flows with at least 50% RST. That flags the wrong host (the target, not the scanner) and depends on seeing return traffic, so it does not mitigate the …

**Evidence.** `GROUP BY src HAVING dst_count >= 100` counts only distinct destinations; port_count is computed but only displayed, and the fixed 100 threshold applies to any window up to 720 h. Reproduced: a 5,000-port SYN-only scan of one host (10.7.7.7 -> 10.0.0.10) in about 2 minutes returned 0 findings, while a 150-host sweep was detected. NF-21 covers the ICMP detector's window dependence; the SYN detector has its own blind spot.

### X-32 — Sensitive-egress detector raises 'critical' on single benign flows, treats CGNAT/link-local as public, and never performs its documented internet-interface check

**Skeptic verdict:** upheld (high confidence), severity P3. I read the baseline server/app/api/v1/netflow.py, lines 2093-2130, and every claim in the defect holds:
- The comment promises an "internet-facing iface" check, but the query never filters on interface or exporter role.
- Sources are limited to RFC1918. Destinations exclude only RFC1918, 127/8 and 224/4, so 100.64/10 (CGNAT), 169.254/16 (link-local), 0/8 and 240/4 all count as public.
- There is no HAVING clause, so no minimum on flows or bytes and no check for TCP flags or a completed handshake. One flow to port 22 is enough. The repro (10.0.0.5 to 140.82.112.3:22 for git over SSH) follows directly from this logic, as does 10.0.0.6 to 100.101.102.103:3389 (CGNAT/Tailscale).
- Severity is hard-coded to "critical".
- The description calls 1433/3306/3389/5432 "privileged service port", which they are not (all are above 1023).
Mitigating factors:
- These findings are returned only by GET …

**Evidence.** The comment at 2093 promises an 'internet-facing iface' check the query (2096-2117) never performs; destination exclusions omit 100.64/10, 169.254/16 and other special-use ranges; there is no flow/byte/handshake minimum; severity is hard-coded 'critical' (2121); the text calls 1433/3306/3389/5432 'privileged service port' although they are not privileged ports. Reproduced: one git-over-SSH flow 10.0.0.5 -> 140.82.112.3:22 produced a critical finding, and 10.0.0.6 -> 100.101.102.103:3389 (CGNAT/Tailscale) produced another.

### X-33 — Security dashboard advertises detectors (exfil, beacon) that do not exist and presents zero matches as 'No hostile patterns detected'

**Skeptic verdict:** upheld (high confidence), severity P3. Confirmed in the baseline. NetworkSecurityDashboard.tsx:422 hardcodes the footer 'scan · sweep · exfil · beacon checks'. It is a static string and does not depend on the /netflow/anomalies response (lines 252-302 read only `findings`). netflow.py (around lines 2030-2185) emits only five kinds: syn_scan, rst_flood, sensitive_egress, icmp_flood and volumetric_outlier. syn_scan counts uniqExact(dst_addr) and uniqExact(dst_port) for SYN-only sources, so it covers both 'scan' and 'sweep'. Nothing in the code checks periodicity or inter-arrival timing, so 'beacon' is not implemented at all. 'Exfil' is not implemented either. sensitive_egress only matches private-to-public flows on admin/DB ports (22, 23, 3389, 445, 1433, 3306, 5432). volumetric_outlier fires only when one source sends more than half of all bytes, so a low-and-slow or moderate exfiltration is never flagged. The empty state at …

**Evidence.** The Threat Signals KPI footer reads 'scan · sweep · exfil · beacon checks' (NetworkSecurityDashboard.tsx:422). /netflow/anomalies implements only syn_scan, rst_flood, sensitive_egress, icmp_flood and volumetric_outlier; grep finds no exfil or beacon logic in netflow.py. The empty state (497) says 'No hostile patterns detected in this window'. NF-03 covers the Anomalies page's all-clear; this separate dashboard claims coverage the product does not have.

### X-34 — Anomaly counts are silently capped by per-detector LIMITs; the Total tile, severity tiles and API `total` report the capped number

**Skeptic verdict:** upheld (high confidence), severity P3. I confirmed this in the baseline netflow.py. Each detector has a hard LIMIT: SYN scan LIMIT 8, RST flood LIMIT 5, sensitive egress LIMIT 5, ICMP LIMIT 3. The volumetric check emits at most rows[0]. None of these queries computes an uncapped count, and the response returns `"total": len(findings)` with no truncation flag or per-check match count. NetflowAnomalies.tsx builds the Critical, Warning and Info tiles and the tile labelled 'Total' from findings.length and filters over that capped list. Nothing on the page says the list is top-N. So with 40 hosts over the SYN-scan threshold, the API and UI report 8 findings, and the most any window can report is 22 (8+5+5+3+1). The behaviour is deterministic from the code, so I did not need a ClickHouse experiment. Mitigating factors: the detectors are clearly meant to show the top offenders (ORDER BY severity metric DESC), so the worst hosts …

**Evidence.** Detectors truncate their matches: SYN scan LIMIT 8 (2045), RST flood LIMIT 5 (2077), sensitive egress LIMIT 5 (2114), ICMP LIMIT 3 (2141); volumetric takes rows[0] of LIMIT 5 (2168-2176). The response sets `"total": len(findings)` (2189). The Anomalies UI shows findings.length as 'Total' and derives severity counts from the capped list (NetflowAnomalies.tsx:54-58, 100), and the Security dashboard Threat Signals count uses the same number. 40 scanning hosts display as 8 with no 'more' indication and no truncation flag.

### X-35 — Throughput chart uses a category X axis over sparse buckets, so outages and idle gaps disappear and edge buckets dip

**Skeptic verdict:** upheld (high confidence), severity P2. I confirmed this in the baseline code. At NetflowPage.tsx:744 the XAxis is `<XAxis dataKey="ts" tickFormatter=... />` with no `type`, so recharts uses its default category axis, which spaces points by index rather than by time. Nothing fills in the missing time slots before the chart draws. The API (netflow.py timeseries, both the raw flow_records query and the rollup query) does `GROUP BY ts ORDER BY ts` with no WITH FILL, so it only returns buckets that have rows. On the client, `series = timeseries.data || []` and `mergedSeries` just copies those points (or adds prior_bps looked up by ts), so the gaps stay missing there too. The result: a collection outage or idle stretch is drawn as two neighbouring points joined by a monotone curve, and the time tick labels are unevenly spaced. A second effect makes it worse: in compare mode, prior-period points are only looked up at current-period …

**Evidence.** NetflowPage.tsx:744 `<XAxis dataKey="ts" tickFormatter={timeAxisTickFormatter(hours)} .../>` sets no type, and recharts 2.15.4 defaults XAxis to type 'category'. The API returns only buckets containing rows (netflow.py:797-811 GROUP BY ts ORDER BY ts, no WITH FILL), so points are spaced by index, not time: a 6-hour collection outage in a 24h view is drawn as adjacent points joined by a monotone curve, and tick labels are unevenly spaced. PerformanceChart.tsx:315 already does this correctly (type='number' domain={[fromTs,toTs]} scale='time'). First/last buckets are partial but divided by the full bucket (`sum(bytes) * 8 / {bucket}`), so every refresh shows a dip at the current edge. The review mentions gaps/partial buckets only as UI direction.

### X-36 — Capacity, Top Interfaces and all anomaly detectors bypass the corrupt-flow guard that _scope applies to every other aggregate

**Skeptic verdict:** upheld (high confidence), severity P3. Checked against the baseline. `_scope()` (netflow.py:605-606) adds `flow_records.bytes / greatest(flow_records.sampling_interval, 1) <= 1000000000000` for flow_records. The rollup MVs (migrate-071/072) and services/netflow_rollup.py SANITY_GUARD use the same predicate. But /interfaces (1542, both UNION branches), /capacity (1936, both UNION branches) and /anomalies (2023, all five detector queries) build their WHERE only from `_exporter_filter()` (476-481), which returns just the exporter_ip clause. So any stored row over the threshold gets summed straight into Top Interfaces, the capacity p95/avg/max bps and the anomaly detectors, while /overview and the other aggregates exclude it. The two pages then disagree about the same window. The one thing that limits the damage is that the collector drops records over 1 TB before insert (poller/cmd/netflow-collector/main.go:36,91). That makes …

**Evidence.** _scope adds `flow_records.bytes / greatest(flow_records.sampling_interval, 1) <= 1000000000000` (netflow.py:606) and the rollup MVs use the same predicate (migrate-072), but /interfaces (1542), /capacity (1936) and /anomalies (2023) build their WHERE from `_exporter_filter(exporter)` (helper 476-481), which omits it. Reproduced: one 5 TB record produced 'Volumetric outlier ... more than half of all traffic'; one 5e15-byte row made capacity report max_bps 133,333,333,333,333 (133 Tbps) while the _scope-guarded sum for the same window returned 1,000,000 bytes. /interfaces also feeds DashboardPage.tsx:218 ('busiest interfaces'). The collector now drops such rows at ingest, so this is defence-in-depth for legacy/backfilled rows; the same _exporter_filter shortcut is also why these endpoints ignore page filters (NF-27) and will miss the NF-04 visibility predicate.

### X-37 — Long-window pages re-scan raw flow_records on fixed short polling intervals instead of using rollups (Anomalies 5 serial scans every 30 s, Capacity 2 scans every 60 s, heatmap 7 days every 60 s, unused continuity scan), with slow toString() IP predicates and an unbounded healer plan

**Skeptic verdict:** upheld (high confidence), severity P3. I checked the frozen baseline and the main claims hold. In NetflowAnomalies.tsx:50, `refetchInterval: isCustom ? false : 30_000` does not depend on the window. NetflowPage.tsx defines pollMs() to back off on multi-day windows, but Anomalies does not use it, so a 720 h preset is re-polled every 30 s. The netflow_anomalies endpoint (hours le=720) runs five `await _query` calls one after another (syn_scan, rst_flood, rfc1918_leak, icmp, volume), all against zenplus.flow_records, with no rollup path and no gather. NetflowCapacity.tsx:43 uses a fixed 60_000 interval. In device-status, a separate countDistinct(toStartOfFiveMinutes(timestamp)) runs over raw flow_records for the whole window just to get uptime_pct. `_query_sync` passes no settings, so there is no max_execution_time. The healer's `_plan` runs an unbounded GROUP BY over all raw rows (bounded only by SANITY_GUARD) and a full …

**Evidence.** NetflowPage deliberately scales polling (pollMs, NetflowPage.tsx:269-274), but Anomalies uses `refetchInterval: isCustom ? false : 30_000` (NetflowAnomalies.tsx:50) for every preset including 1M, running five sequential raw detectors with no gather or budget over up to 720 h (netflow.py:2016-2171); NetworkSecurityDashboard.tsx:255 polls it every 120 s. Capacity uses a fixed 60_000 (NetflowCapacity.tsx:43) for a raw UNION ALL scanned twice (1957-1968) with no rollup path, while other long-window endpoints switch to flow_conversations_1h/flow_qos_1h at >= 72 h (531-575). The overview heatmap is pinned to 168 h (NetflowPage.tsx:465-474) and reads raw (1409-1422) every 60 s even on the 1h view although flow_qos_1h keys by hour; /exporters, /countries and /ip-groups are raw-only. device-status still runs `countDistinct(toStartOfFiveMinutes(timestamp)) FROM zenplus.flow_records` over the whole window (1363-1370) after choosing the QoS rollup for everything else; its flow_continuity_pct/uptime_pct is displayed nowhere (DeviceStatusCard reads only latency_ms and packet_loss_pct, 1972-1973) and its denominator counts future/expired slots (a 2020-2030 window -> 0.0%). Predicates use isIPAddressInRange(toString(col), cidr) (2103-2110, _scope talker/netclass/group 613/645/443-444, forensics 1824/1831) although comments at 872-874 and 1015-1017 say per-row toString() 'tripled the query …

### X-38 — talker and exporter filters are compared as raw strings without validation, so CIDR or malformed input silently matches nothing

**Skeptic verdict:** refuted (medium confidence), severity none. This is not a defect. At baseline, `_scope` (server/app/api/v1/netflow.py:609-613) does compare exporter and talker by exact equality, but neither one is meant to take a CIDR.
Talker is an exact-IP drill-down. The only UI paths that set it are clicks on top-talker or endpoint rows: NetflowPage.tsx:793/803 `setParam('talker', name)` and 1277 `withParams('/netflow', {talker: row.ip})`. There is no free-text talker input, and nothing in the API or UI says talker accepts CIDR. CIDR search already exists where it is intended: the forensics `src`/`dst` params (netflow.py:1800-1830) validate with `ipaddress.ip_network`, return 400 on bad input, and use `isIPAddressInRange`. So `talker=10.0.0.0/24` returning 0 rows is correct exact-match behaviour for a hand-crafted URL.
Exporter is set from the device route (NetflowPage `exporter={ip}`) or the forensics free-text field. `exporter='abc'` …

**Evidence.** _scope uses `toString(src_addr) = %(talker)s OR toString(dst_addr) = %(talker)s` (612-613) and `exporter_ip = %(exporter)s` (609-610) with no validation. On the fixture talker=10.0.0.0/24 returned 0 rows and exporter='abc' returned 0 rows (HTTP 200), when the correct result was 350 matching rows or a validation error. Affects every endpoint using scope_params and matters as soon as a host/talker input is exposed (NF-08, NF-26). NF-31 covers numeric port/DSCP bounds, not address filters.

### X-39 — Device-status RST ratio follows the page's tcpflag/protocol filters, so selecting the RST tile fabricates a 100% 'High TCP RST Ratio' warning

**Skeptic verdict:** upheld (high confidence), severity P3. I confirmed this in the baseline code. In /device-status (netflow.py lines 1295-1375), both the rollup query and the raw query call `_scope(**scope)` with the full scope, so the tcp_flag and protocol filters are both applied. /tcp-flags, by contrast, removes them on purpose (line 1691: `{**scope, "tcp_flag": None, "protocol": None}`). With tcpflag=rst, `_scope` adds `protocol = 6 AND bitAnd(tcp_flags, 4) != 0` (line 656). Every remaining flow is then an RST flow, so rst_flows / flows is always 100%. With protocol=17 the ratio is always 0 but is still labelled a TCP RST ratio.
The UI path is also confirmed. NetflowPage.tsx sets tcpflag in rangeQS (line 391) and fetches /netflow/device-status with it (line 460), keyed on a rangeKey that includes filterTcpFlag. buildAlerts gets deviceStatus.data (line 559) and raises the 'High TCP RST Ratio' warning at packet_loss_pct >= 5 (line 1884). The …

**Evidence.** device-status uses the full scope (1305-1323 `_scope(**scope …)`), unlike /tcp-flags which strips tcp_flag and protocol (1690). The UI sends tcpflag in rangeQS (NetflowPage.tsx:392) and buildAlerts raises the warning at >= 5% (1884). Reproduced on the dev API: /device-status?hours=24&tcpflag=rst -> rst_ratio_pct 100.0 and packet_loss_pct 100.0; protocol=17 -> 0.0 labelled as a TCP RST ratio. NF-19 covers the all-protocol denominator; this is filter leakage into the same metric.

### X-40 — ICMP type/code carried in dst_port is treated as a transport port and misclassified

**Skeptic verdict:** upheld (high confidence), severity P3. I confirmed this by reading the frozen baseline. I could not run it: importing netflow.py failed because fastapi is not installed.
- **Collector:** v5.go:118 copies the raw dst_port field. v9.go:436 decodes only IE 11 (L4_DST_PORT). Nothing in poller/internal/netflow mentions ICMP, so IE 32/176/177 are ignored. Cisco v5 and many v9 exporters encode ICMP as type*256+code in dst_port, so those values get stored as ports: 771 is 3/3 port unreachable, 2048 is 8/0 echo request.
- **API:** _port_summary (netflow.py:330) always calls _application_for_port(port) and ignores proto. _port_name adds only TCP/ and UDP/ prefixes.
- **/protocols port breakdown (lines 1178-1194):** only filters dst_port != 0 and has no protocol filter. So ICMP rows produce ports 771 labelled "System Services" (because 1 <= port <= 1023) and 2048 labelled "Other", both with the bare number as the service name.
- …

**Evidence.** NetFlow v5 and many v9 exporters encode ICMP type*256+code in dst_port; the collector ignores ICMP_TYPE / IE 32/176/177 (assignV9Field, v9.go:394-460), and _port_summary (netflow.py:330-338) calls _application_for_port(port) without the protocol. Reproduced on /protocols ICMP port breakdown: (771,'771','System Services'), (2048,'2048','Other'); Forensics shows service '771' and '2048'.

### X-41 — UI fallback application classifier diverges from the server's buckets

**Skeptic verdict:** refuted (high confidence), severity none. The two tables really do disagree. inferApplication (NetflowPage.tsx:1696) puts 8000 and 5060/5061 in 'Streaming Media' and uses different bucket names from APPLICATION_BUCKETS (netflow.py:241-256). But the fallback never runs in practice. Its only caller is ConversationDetailPanel (line 1415, `row.application || inferApplication(row.dst_port)`). That panel is only rendered at line 1132, in the conversations section, from /netflow/top-conversations data. That endpoint shapes every row through _format_conversations (netflow.py:1076-1111), which always sets "application": _application_for_flow(port, proto). That function always returns a non-empty string: a protocol bucket, a port bucket, 'System Services' or 'Other'. Since that string is always truthy, the `||` fallback is never taken. So the same port cannot get a different label depending on whether the server supplied one, because the …

**Evidence.** inferApplication (NetflowPage.tsx:1696) maps 8000 and 5060/5061 to 'Streaming Media' and uses bucket names 'File Transfer (FTP/SMB)' and 'Remote Access (SSH/RDP/Telnet)', while the server's APPLICATION_BUCKETS (netflow.py:245-259) puts 8000 in Web, 5060 in VoIP, with different names. It is used whenever row.application is missing (1415), so the same port can be labelled differently depending on whether the server supplied an application.

### X-42 — Window predicates use inclusive BETWEEN on both ends, double-counting the boundary instant between adjacent windows

**Skeptic verdict:** upheld (high confidence), severity P3. The claim holds, but the effect is small. In the baseline, server/app/api/v1/netflow.py uses `timestamp BETWEEN %(start)s AND %(end)s` (inclusive at both ends) in about 40 predicates: summary 704/731/741, timeseries 789/806, and many others. Nothing mitigates it. `_floor_hour` only widens the start edge, and no path makes either end exclusive. The one place the codebase already uses half-open windows is netflow_rollup.py (`>= hour AND < hour + 1h`, lines 86/109/194), so the convention exists but the API ignores it.
Adjacent windows really are issued. The Compare toggle in NetflowPage.tsx:364-374 builds the prior window as [from-span, to-span], so the prior window ends exactly where the current one starts. compareDelta (517-521) then sums the prior bytes against the current bytes, so a boundary record lands in both totals.
How often a record sits exactly on the boundary:
- **Raw path:** …

**Evidence.** `WHERE timestamp BETWEEN %(start)s AND %(end)s` is used on raw and rollup paths (731, 704, 741, 789, 806, 1320, 1338, 1367, 1417, 2504, …). Reproduced: the 71 h custom raw path returned 4261 flows vs 4260 half-open; the 72 h raw path 4321 vs 4320. Adjacent windows (Compare prior/current, report periods) both count a flow stamped exactly at the boundary.

### X-43 — Forensics protocol select shows 'Any' while a non-listed protocol filter is applied, and the control cannot clear it

**Skeptic verdict:** upheld (high confidence), severity P3. I confirmed this by reading the baseline code; I did not run a browser test. In NetflowForensics.tsx, PROTO_OPTIONS (lines 41-48) lists only ''/6/17/1/47/50. `proto` is seeded directly from `params.get('proto')` (line 55), and nothing checks it against that list. qs sends `proto` whenever it is set (line 73), and `seeded` turns on the query automatically for proto (line 63). SelectField (lines 314-328) is a plain controlled native <select> with the static options and no fallback option. The conversation drill-down in NetflowPage.tsx sends `proto: String(row.protocol)` for any protocol number, for example 89 (OSPF), 2 (IGMP) or 51 (AH). When a controlled select's value matches no option, React selects none of them, so the browser shows the first option, 'Any'. The results are still filtered by proto=89. Choosing 'Any' again leaves selectedIndex unchanged, so no change event fires and …

**Evidence.** PROTO_OPTIONS (NetflowForensics.tsx:41-48) lists only ''/6/17/1/47/50; proto is seeded from the URL (55) and conversation drill-downs send any protocol (NetflowPage.tsx:1134 `proto: String(row.protocol)`, e.g. 89 OSPF, 112 VRRP, 51 AH, 2 IGMP). For a controlled <select value='89'> with no matching option React shows the first option, so the control reads 'Any' while qs sends proto=89 (73) and results are filtered (Playwright: select text 'Any', request '...&proto=89'). Re-choosing 'Any' fires no change event, so only Reset clears the hidden filter.

### X-44 — Security dashboard 'Anomalies →' / Threat Signals links drop the dashboard range

**Skeptic verdict:** upheld (high confidence), severity P3. This is confirmed in the frozen baseline. NetworkSecurityDashboard stores its range in local component state (useState<RangeKey>('6h'), line 184), not in the URL. The Threat Signals tile counts findings.length from /netflow/anomalies?hours=${range.hours} (line 254), so by default it counts 6h of findings. The KpiCard at line 416 and the 'Anomalies →' link at line 495 both go to a bare '/netflow/anomalies' with no query string. On that page, useTimeRange reads ?range from the URL, and when it is missing it falls back to TIME_RANGE_OPTIONS[0], which is 1h. So even in the default case the tile shows 6h of findings and the page it opens shows 1h; the same happens with 24h or 7d. Nothing else carries the range across: no shared context, no localStorage, and the links pass no params. A partial fix is possible because the page accepts '1h', '24h' and '7d' as keys. It has no 6h preset, though, …

**Evidence.** The dashboard counts findings for hours=${range.hours} (NetworkSecurityDashboard.tsx:254) but links to a bare /netflow/anomalies (416, 495). The Anomalies page then defaults to 1h, so its counts disagree with the tile that was clicked. Same context-loss family as NF-09 on a different navigation path.

### X-45 — Client-side Forensics CSV export does not neutralise spreadsheet formulas

**Skeptic verdict:** upheld (medium confidence), severity P3. This is real but narrow. At baseline NetflowForensics.tsx:93, `esc` only wraps each value in quotes and doubles embedded quotes. Excel and LibreOffice still evaluate a quoted field that starts with `=`, `+`, `-` or `@`. There is no other neutralisation anywhere on the path.
Where the text columns come from:
- **Interface columns:** `interfaceLabel()` (line 268-270) uses `display_name`. The server builds that in `_interface_display_name` (netflow.py:1511-1512) as `if_name or if_descr or if_alias`.
- **Who writes those fields:** only the poller's SNMP upsert (poller/internal/store/postgres.go:673-681) writes `device_interfaces.if_name`, `if_descr` and `if_alias`. So the value is whatever the monitored device reports over SNMP.
- **Service column:** comes from `_port_name`, a fixed port map, so it is not injectable.
Corrections to the claim:
- There is no user-editable display-name path.
- …

**Evidence.** `esc` only doubles quotes (NetflowForensics.tsx:93-97). input_interface/output_interface cells come from device_interfaces if_alias/if_descr (SNMP, device-controlled) or user display names, so a value like `=HYPERLINK(…)` is evaluated by Excel even when quoted. The platform convention `_csv_safe` (alerts.py:490-493) prefixes = + - @ with an apostrophe.

### X-46 — An explicit exporter mapping is silently ignored when the exporter IP also matches a device's management IP

**Skeptic verdict:** upheld (medium confidence), severity P3. The precedence is confirmed in the baseline code. In `_resolve_interface_names` (netflow.py ~1470-1476), `_exporter_device_overrides` runs only for IPs that are not in `ip_to_device`. In `list_exporter_devices` (~2405-2410), `source` is 'ip-match' whenever a device's management IP matches, and the override is used only when there is no match. `PUT /exporter-devices/{ip}` (2425-2462) never checks for an ip-match conflict. It upserts the mapping, writes an audit log and returns the mapped device, but interface names and the listing still use the ip-matched device.
Two things limit the impact:
1. The UI (NetflowPage.tsx 987-990) shows a "Resolved" badge with no Link control for ip-match exporters, so a normal UI user cannot create the conflicting mapping. It can only be created by calling the API directly, or by adding a device later whose management IP equals an exporter that was already …

**Evidence.** list_exporter_devices (2402-2413): `source = 'ip-match' if dev else ('mapped' if ip in overrides ...)`, so the override is consulted only when there is no ip-match; _resolve_interface_names (1470-1476) consults overrides only for unmapped IPs. PUT /exporter-devices (2425-2462) accepts such a mapping and reports success but it has no effect on names. NF-04's fix needs one deterministic owner per exporter, so this precedence must be settled first.

### X-47 — Link Utilization's has_netflow ignores the NetFlow exporter→device mapping

**Skeptic verdict:** upheld (high confidence), severity P3. This is confirmed by reading the frozen baseline code.
- **How the lookup is keyed:** In `link_utilization.py:444-464`, `nf_set` is filled only with `(toString(exporter_ip), ifindex)` from `flow_records`. At lines 499-500 the check is `has_nf = (_clean_ip(d.ip_address), idx) in nf_set`, so the key is the device's management IP.
- **Mapping is ignored:** Nothing in `link_utilization.py` reads `netflow_exporter_devices`. It does not call `netflow._exporter_device_overrides` (`netflow.py:1431`), which is the fallback `_resolve_interface_names` uses at `netflow.py:1471-1476`.
- **Nothing else covers it:** No caller, UI guard or database constraint makes up for this. The mapping table exists exactly for exporters whose source IP is not the management IP, such as loopback- or WAN-sourced export. For those devices every interface gets `has_netflow = false`.
- **What users see:** …

**Evidence.** has_nf = (_clean_ip(row[11]), idx) in nf_set (link_utilization.py:499-500), where nf_set is keyed on the flow exporter IP (449-464); this only works when the exporter IP equals the device management IP. Exporters linked through netflow_exporter_devices (migrate-054) never get has_netflow — the same loopback/WAN-sourced export case netflow.py:1471-1475 fixed for name resolution — so the SNMP<->NetFlow cross-link the NF-16 guidance needs breaks for exactly those exporters.

### X-48 — Interface speed override endpoints write no audit record

**Skeptic verdict:** upheld (high confidence), severity P3. This is confirmed in the baseline. In devices.py, update_device_interface_speed (PATCH /{device_id}/interfaces/{if_index}, lines 699-726) and bulk_update_device_interface_speed (POST /{device_id}/interfaces/bulk-speed, lines 729-760) run the UPDATE on device_interfaces.configured_speed_bps and commit, with no write_audit_log call. In devices.py, write_audit_log is only called in the maintenance endpoints (around lines 1928 and 1991). Nothing else writes the record either: main.py has only the gzip, server-time and CORS middlewares, with no audit middleware. The UI really uses the bulk endpoint (DeviceInterfacesPage.tsx:264). The override feeds _effective_speed in link_utilization.py, which drives utilization. That same module audits its tag edits (link.tags.update and link.tags.bulk), so the gap is inconsistent even within the utilization feature. What lowers the severity: device …

**Evidence.** update_device_interface_speed (devices.py:700-727) and bulk_update_device_interface_speed (730-760) change configured_speed_bps — which drives utilization, alert thresholds and (once X-05 is fixed) NetFlow capacity headroom — without calling write_audit_log, whereas the NetFlow exporter-mapping endpoints do audit (netflow.py:2451-2455, 2476-2480). NF-16 requires the capacity override to be audited.

### X-49 — DELETE /netflow/saved-views/{view_id} returns 500 for a non-UUID id

**Skeptic verdict:** upheld (high confidence), severity P3. In the baseline, delete_saved_view (netflow.py:2353-2365) declares `view_id: str` and puts it straight into `NetflowSavedView.id == view_id`. That column is `UUID(as_uuid=True)` (models/netflow_saved_view.py:13). Nothing checks the value before the query: FastAPI accepts any string, the handler never parses it, and there is no DataError/StatementError exception handler in app/main.py or app/core. When asyncpg tries to encode a malformed string as a UUID parameter it raises DataError, which surfaces as an unhandled 500. That matches the reproduction cited in the evidence. I could not rerun it myself because asyncpg is not installed in the system python. The dashboard only ever sends real ids from the list (NetflowSavedViews.tsx:25), so a normal user in the UI never hits this. It needs a hand-crafted request, and the only effect is a 500 where a 422 or 404 should be. No data is affected …

**Evidence.** `view_id: str` (2355) is compared against a UUID column (`NetflowSavedView.id == view_id`, 2358). Reproduced: DELETE /netflow/saved-views/not-a-uuid returned 'Internal Server Error' [500]. reports.py custom-report routes type the path parameter as uuid.UUID, the platform convention.

### X-50 — rDNS helper caches every lookup that misses the 2-second deadline as 'no hostname' for an hour, including queued lookups it cancelled

**Skeptic verdict:** upheld (high confidence), severity P3. I read the baseline _resolve_hostnames (netflow.py 357-383) and ran it on its own. When a lookup times out, results[ip] keeps its None seed. ex.shutdown(cancel_futures=True) cancels lookups still in the queue, and every entry, None included, is then cached for _RDNS_TTL=3600s. Lookups that finish after the deadline are never written back, so the comment at line 376 is wrong. I exec'd that section with a fake _rdns_one: 16 IPs take 3s and 34 take 10ms, run through 16 workers. The first call took 2.00s and returned 0/50 hostnames. A second call after 4s also returned 0/50, with 50 None entries cached. Nothing downstream mitigates it. Impact is small: the feature is opt-in (?resolve=1 on two top-N endpoints), no dashboard code sends resolve=1, and the only effect is hostnames missing for up to an hour. That makes it P3.

**Evidence.** _resolve_hostnames (368-382) seeds `results = {ip: None for ip in todo}`, waits on as_completed(futs, timeout=deadline), and on timeout runs `ex.shutdown(wait=False, cancel_futures=True)` then caches every entry, None included, for _RDNS_TTL = 3600 s. The comment at 376 ('slow lookups finish in the background and populate the cache') is false: completed background futures are never written back and queued lookups are cancelled; a new 16-thread executor is created per request. Reproduced: with 16 IPs taking 3 s and 34 resolving in 10 ms, the first call returned 0/50 after 2.00 s and a second call after background completion still returned 0/50 (50 None entries cached for 3600 s). Latent today (the UI does not send resolve=1), but it is the backend foundation FE-09 proposes to surface.

### X-51 — formatBps/formatBytes render 'undefined' units for values below 1

**Skeptic verdict:** upheld (high confidence), severity P3. This is real but cosmetic. In the baseline utils.ts, formatBps (line 21), formatBpsAxis and formatBytes compute the unit index with Math.floor(log(x)/log(k)) and never clamp it at 0. Any value in (0,1) therefore gives i=-1, and units[-1] is undefined. Node reproduction: formatBps(0.5) returns '500.00 undefined', and a 40-byte flow in a 7200 s bucket (40*8/7200) returns '44.44 undefined'. The only guard is the early return for 0 or falsy input, which does not cover fractional values.
The backend sends fractional floats, not rounded integers: timeseries uses sum(bytes)*8/{bucket} AS bps with float(r[1]), and current_bps uses sumIf(...)*8/rate_window. NetflowPage passes these straight to formatBps in the YAxis tickFormatter (745), the tooltip (748), the "current rate" sub-label (675) and the alert bodies (1870/1878). When the chart max is small, recharts picks fractional nice ticks such as …

**Evidence.** utils.ts:17-23: `const i = Math.min(Math.floor(Math.log(Math.abs(bps)) / Math.log(k)), units.length - 1)` has no lower clamp, so values in (0,1) give i=-1 and units[-1]. Verified with node: formatBps(0.5) -> '500.00 undefined', formatBps(0.25) -> '250.00 undefined', a 40-byte flow in a 7200 s bucket (1M range) -> '44.44 undefined'; formatBytes(0.5) -> '512.0 undefined'. NetflowPage uses it for the YAxis tickFormatter (744-745, where recharts nice ticks for a small max are fractional), the tooltip (748), ThroughputDisplay (2649) and alert bodies; quiet talker/protocol filters on long ranges hit it.

### X-52 — No automated tests cover /netflow/anomalies, and the hard-coded table name prevents fixture testing

**Skeptic verdict:** upheld (medium confidence), severity P3. The frozen baseline copy has no server/tests directory, so I checked HEAD in the repo instead (git show HEAD:server/tests/test_netflow_api.py). The coverage gap is real. The file has pure-helper tests plus live endpoint tests for overview, talkers, endpoints, conversations, applications, network classes, forensics CSV, saved views, device-status and heatmap. It has no test for /netflow/anomalies, and "anomal" appears nowhere in it. No other NetFlow test file exists besides the Go decoder tests (ipfix/v5/v9_test.go).
The second half of the claim is overstated. The baseline netflow.py has 30 occurrences of 'zenplus.flow_records', not about 40. The existing endpoint tests don't use fixtures either: they call the live API through the `auth` fixture against the real database. So a hard-coded table name doesn't stop anyone from adding an anomalies test in the same style. It only blocks …

**Evidence.** The test module covers overview, talkers, conversations, applications, forensics, saved views, device-status and heatmap, with no anomaly test. The endpoint hard-codes 'zenplus.flow_records' about 40 times, so it cannot be pointed at a fixture database.

## 5. Competitive assessment

Twelve products were assessed from current vendor documentation and release notes: SolarWinds
NTA, ManageEngine NetFlow Analyzer, Paessler PRTG, Plixer Scrutinizer/Plixer One, Kentik,
ntopng/nProbe, ElastiFlow, Progress Flowmon, Noction Flow Analyzer, Auvik TrafficInsights,
LogicMonitor and Cisco Secure Network Analytics.

**Where ZenPlus stands.** The foundations are competitive — a ClickHouse store with hourly
rollups, v5/v9/IPFIX decoding, click-to-filter drill-downs, raw forensics, exporter mapping
to SNMP inventory, reports integration — but the module falls short of table stakes in five
areas:

1. **Collection coverage.** 11 of 12 competitors accept **sFlow**, which many campus and
   data-centre switches (Aruba CX, HPE, Arista, Juniper EX, Dell) export exclusively; ZenPlus
   rejects it. **IPv6** is universal elsewhere; ZenPlus stores it as 0.0.0.0. **NBAR2/App-ID**
   fields are read by SolarWinds, ManageEngine, LogicMonitor, Plixer and Flowmon; ZenPlus
   guesses from destination ports. **Cisco ASA/FTD NSEL** records are stored with zero bytes.
2. **Trust in the numbers.** ManageEngine, Kentik and Plixer offer per-direction p95 billing
   with a configurable percentile; ZenPlus merges directions and mis-slots samples (NF-13/14).
   Plixer and SolarWinds show collector/exporter health (missed sequence numbers, "flow source
   not sending"); ZenPlus's counters sit on an unread localhost endpoint.
3. **Alerting.** Threshold alerts on flow data (application above/below, endpoint volume,
   exporter silent) are standard in SolarWinds, ManageEngine, PRTG, Plixer, Kentik, ntopng,
   Flowmon and Noction. ZenPlus computes "alerts" in the browser with a hard-coded 100 Mbps.
4. **Security analytics.** Plixer, Kentik, Flowmon, Cisco SNA, ntopng and ManageEngine persist
   detections with baselines, allowlists and lifecycle, and most check traffic against
   threat-intelligence reputation lists. ZenPlus reruns five stateless heuristics per page load.
5. **Governance.** Per-team scoping of flow data (Plixer, Kentik, SNA, ntopng, SolarWinds) is
   missing entirely; `netflow.view` is not even enforced.

**Cheap differentiators.** ZenPlus already owns data competitors charge extra for: UDT
(IP → MAC, switch port, AD user) enables a host profile that joins flows with identity, like
Plixer Endpoint Analytics or SNA + ISE; SNMP interface history enables a cross-check of flow
capacity against measured counters; the platform alert engine, escalation and reports can be
reused rather than rebuilt. A deterministic "what changed?" contribution analysis delivers most
of the value that Kentik and Flowmon market as AI-assisted investigation.

| Competitor | Positioning |
|---|---|
| SolarWinds NetFlow Traffic Analyzer (NTA) | Windows/SQL Server module on the SolarWinds Observability Self-Hosted (Orion) platform. It needs NPM, and flows are tied to NPM-managed nodes. It is a mature enterprise operations tool, but the 2025.x line has been maintenance-only: NTA 2025.4.3 (5 Feb 2026) lists no new features. Feature-complete for classic bandwidth and application reporting, with little new investment. |
| ManageEngine NetFlow Analyzer | The most feature-dense on-prem competitor and the closest functional benchmark for ZenPlus. Releases are frequent (12.8.x in 2025 up to 12.9.147 on 10 Sep 2026) and have added ML security analytics, flow deduplication, remote collectors and GenAI/MCP integration on top of long-standing billing and capacity reports. |
| Paessler PRTG (flow sensors) | A sensor-centric general monitoring tool where flow data is used for bandwidth accounting, not deep analytics. Flow data becomes channels and short-lived toplists. Paessler partners with Plixer for deep flow analysis. |
| Plixer Scrutinizer / Plixer One (FlowPro, Replicator, ML Engine, Endpoint Analytics) | A flow-first NDR and NPM platform. It is the benchmark for security analytics on flow data, collector and exporter health, multi-tenancy and vendor field coverage. The 2025-2026 releases (19.6 to 19.8.1) focus on an AI assistant and insights, multi-tier storage, and cloud, Zscaler and Kubernetes sources. |
| Kentik | SaaS network observability for service providers and large enterprises. It combines large-scale flow data with BGP, SNMP and synthetics, and added AI Advisor in Nov 2025. It sets the bar for exploratory query UX, capacity runout and DDoS workflows, but it is not an on-prem appliance. |
| ntopng (+ nProbe) | Open-core traffic analytics from ntop. Historical flows are stored in ClickHouse, which makes it the closest architecture to ZenPlus. It is strong on DPI, behavioural checks and a host-centric UX; flows are collected through nProbe. |
| ElastiFlow NetObserv | A collector and enrichment pipeline that feeds Elasticsearch, OpenSearch, Kafka or Splunk. The UI, alerting and ML come from the storage platform. Releases are frequent (7.12 in Jul 2025 to 7.26.3 in Aug 2026). No native ClickHouse output. |
| Progress Flowmon | European NDR/NPMD appliance family (collector, probes, ADS). Flowmon 13 (13 Nov 2025) rebuilt the backend with up to 7x faster queries and stream-based alerting. Flowmon 13.1 adds investigation playbooks and AI-assisted ADS. |
| Noction Flow Analyzer (NFA) | A low-cost on-prem (Linux) flow analyzer built on ClickHouse 25.3, making it the most direct architectural and price peer to ZenPlus NetFlow. Its 2026 releases have come quickly and added threshold alerting, BGP diagnostics, DDoS detection and Flowspec mitigation. |
| Auvik TrafficInsights | A SaaS network-management add-on for MSPs and mid-market customers. It offers simple top-N views and ML application classification, keeps flow data 14 days and is tightly integrated with Auvik maps. Shallow on analytics but polished for onboarding. |
| LogicMonitor (LM Envision network traffic flow + Edwin AI) | SaaS hybrid observability. Flow collection is a Collector module attached to monitored resources and keeps pre-aggregated top-N data rather than raw forensics. Edwin AI correlates alerts and suggests root cause. |
| Cisco Secure Network Analytics (formerly Stealthwatch) | Security-first flow NDR for large Cisco estates, built on behavioural modelling, deduplication/stitching and ISE identity. Releases 7.5.1-7.5.3 and 7.6 shipped through 2025-2026 (7.6 on UCS M8). The last order date for the M6 hardware appliances is 18 Dec 2026. Heavy, costly, security-only. |

| Capability | Category | ZenPlus today | Importance | Competitors with it |
|---|---|---|---|---|
| NetFlow v5 / v9 and IPFIX decoding | Collection | present | table-stakes | SolarWinds NTA, ManageEngine NetFlow Analyzer, PRTG, Plixer, Kentik, ntopng, ElastiFlow, Flowmon, Noction NFA, Auvik TrafficInsights, LogicMonitor, Cisco SNA |
| sFlow v5 | Collection | absent | table-stakes | SolarWinds NTA, ManageEngine NetFlow Analyzer, PRTG, Plixer, Kentik, ntopng, ElastiFlow, Flowmon, Noction NFA, Auvik TrafficInsights, LogicMonitor |
| J-Flow / Huawei NetStream | Collection | partial | table-stakes | SolarWinds NTA, ManageEngine NetFlow Analyzer, PRTG, ntopng, Flowmon, Noction NFA, Auvik TrafficInsights, LogicMonitor |
| Cisco ASA/FTD NSEL (firewall event flows) | Collection | absent | differentiator | SolarWinds NTA, ntopng, Plixer, ElastiFlow, LogicMonitor, Cisco SNA |
| Citrix AppFlow and vendor IPFIX enterprise fields (AVC, NSX, Gigamon) | Collection | absent | nice-to-have | ManageEngine NetFlow Analyzer, Flowmon, ElastiFlow, Plixer, ntopng |
| IPv6 flows and IPv6 exporters | Collection | absent | table-stakes | SolarWinds NTA, PRTG, Kentik, ntopng, Flowmon, Noction NFA, ElastiFlow, Plixer, Cisco SNA |
| Sampling-rate correction | Collection | partial | table-stakes | PRTG, Kentik, ElastiFlow, ntopng, SolarWinds NTA, Plixer |
| NBAR2 / App-ID / AVC application fields | Enrichment | absent | table-stakes | SolarWinds NTA, ManageEngine NetFlow Analyzer, LogicMonitor, Plixer, Flowmon, Noction NFA, ntopng |
| Per-interface in/out utilisation against ifSpeed | Capacity | partial | table-stakes | SolarWinds NTA, ManageEngine NetFlow Analyzer, PRTG, Plixer, Kentik, Noction NFA, LogicMonitor, Auvik TrafficInsights |
| 95th-percentile billing with a configurable method | Capacity | partial | differentiator | ManageEngine NetFlow Analyzer, Kentik, Plixer, SolarWinds NTA |
| Capacity trend and runout forecasting | Capacity | absent | differentiator | Kentik, SolarWinds NTA, ManageEngine NetFlow Analyzer, Auvik TrafficInsights |
| DSCP / ToS traffic breakdown | Analytics | present | table-stakes | SolarWinds NTA, ManageEngine NetFlow Analyzer, LogicMonitor, Plixer, ntopng, Kentik |
| CBQoS policy monitoring (SNMP) | Analytics | absent | nice-to-have | SolarWinds NTA, ManageEngine NetFlow Analyzer |
| Top-N reports (talkers, endpoints, applications, protocols, ports, countries) | Analytics | present | table-stakes | SolarWinds NTA, ManageEngine NetFlow Analyzer, PRTG, Plixer, Kentik, ntopng, ElastiFlow, Flowmon, Noction NFA, Auvik TrafficInsights, LogicMonitor, Cisco SNA |
| Conversation analysis | Analytics | present | table-stakes | SolarWinds NTA, ManageEngine NetFlow Analyzer, Plixer, Kentik, ntopng, Flowmon, Noction NFA, Auvik TrafficInsights, LogicMonitor, ElastiFlow, Cisco SNA |
| Raw-flow forensics search and export | Analytics | partial | table-stakes | ManageEngine NetFlow Analyzer, Plixer, Kentik, ntopng, ElastiFlow, Flowmon, Noction NFA, Auvik TrafficInsights, Cisco SNA |
| Saved views and reusable investigations | Analytics | partial | differentiator | SolarWinds NTA, Kentik, Plixer, Flowmon, Noction NFA |
| Traffic threshold alerting on flow data | Alerting | absent | table-stakes | SolarWinds NTA, ManageEngine NetFlow Analyzer, PRTG, Plixer, Kentik, ntopng, Flowmon, Noction NFA |
| Flow-reception (exporter silent) alert | Alerting | absent | table-stakes | SolarWinds NTA, Noction NFA, Plixer, PRTG |
| Anomaly / DDoS / scan detection with baselines | Security | partial | differentiator | Plixer, Kentik, Flowmon, Cisco SNA, ManageEngine NetFlow Analyzer, ntopng, Noction NFA, ElastiFlow, LogicMonitor |
| Threat-intelligence IP reputation | Security | absent | differentiator | Plixer, Kentik, ntopng, Flowmon, ElastiFlow, Cisco SNA |
| Endpoint / user identity correlation (MAC, switch port, AD/ISE user) | Enrichment | absent | differentiator | Plixer, Cisco SNA, Flowmon, ntopng, ManageEngine NetFlow Analyzer |
| Geo-IP enrichment | Enrichment | partial | table-stakes | ManageEngine NetFlow Analyzer, Kentik, ntopng, ElastiFlow, Plixer, Auvik TrafficInsights, Noction NFA, SolarWinds NTA |
| AS / BGP analytics | Enrichment | absent | nice-to-have | Kentik, Noction NFA, SolarWinds NTA, ntopng, ElastiFlow, Plixer |
| Reverse DNS | Enrichment | partial | table-stakes | SolarWinds NTA, ntopng, ElastiFlow, Flowmon, Plixer |
| IP groups / sites / departments | Enrichment | partial | table-stakes | SolarWinds NTA, ManageEngine NetFlow Analyzer, Plixer, Noction NFA, Kentik, ntopng, Cisco SNA, Auvik TrafficInsights, Flowmon |
| RBAC / multi-tenant scoping of flow data | Platform | absent | table-stakes | Plixer, Kentik, Cisco SNA, ntopng, SolarWinds NTA, LogicMonitor, Noction NFA |
| Scheduled / PDF reports | Reporting | present | table-stakes | SolarWinds NTA, ManageEngine NetFlow Analyzer, Plixer, Kentik, ntopng, Flowmon, PRTG, LogicMonitor, Cisco SNA, Noction NFA |
| Public API / integrations | Platform | partial | table-stakes | Kentik, ManageEngine NetFlow Analyzer, SolarWinds NTA, Plixer, ntopng, Flowmon, Cisco SNA, LogicMonitor, Noction NFA, PRTG |
| Tiered retention with disclosure | Platform | partial | table-stakes | SolarWinds NTA, LogicMonitor, Noction NFA, Plixer, Cisco SNA, Kentik, ManageEngine NetFlow Analyzer, Auvik TrafficInsights |
| Flow deduplication across exporters / boundary counting | Analytics | absent | differentiator | Cisco SNA, ManageEngine NetFlow Analyzer, Kentik |
| Collector and exporter health (loss, templates, sequence gaps) | Operations | partial | table-stakes | Plixer, SolarWinds NTA, PRTG, Cisco SNA, Auvik TrafficInsights, LogicMonitor |
| Collector HA / scale-out / distributed collection | Operations | absent | differentiator | ManageEngine NetFlow Analyzer, Cisco SNA, Plixer, Flowmon, ElastiFlow, ntopng, SolarWinds NTA, Kentik |
| Exporter onboarding help (config guides, approval, validation) | Operations | partial | differentiator | Auvik TrafficInsights, SolarWinds NTA, ManageEngine NetFlow Analyzer, LogicMonitor, PRTG, Noction NFA |
| NOC map / topology integration | UX | present | differentiator | SolarWinds NTA, Auvik TrafficInsights, PRTG, Kentik, Plixer, LogicMonitor, ntopng |
| Mobile / responsive UI | UX | partial | nice-to-have | ManageEngine NetFlow Analyzer, PRTG |
| Cloud flow logs (AWS VPC / Azure VNet / GCP) | Collection | absent | nice-to-have | Kentik, Plixer, ElastiFlow, Flowmon, ManageEngine NetFlow Analyzer |
| AI assistant / automated cause analysis | UX | absent | nice-to-have | Kentik, Plixer, ManageEngine NetFlow Analyzer, Flowmon, LogicMonitor |
| Flow replication / forwarding to other collectors | Operations | absent | nice-to-have | Plixer, ntopng, ElastiFlow |
| DDoS mitigation (RTBH / Flowspec) | Security | absent | nice-to-have | Kentik, Noction NFA |

| ID | Priority | Effort | Feature | Overlaps review |
|---|---|---|---|---|
| CR-01 | P1 | M | Decoder correctness and a per-template field registry | FE-01, FE-03 (fixes NF-07, NF-33 and the audit's v5 sampling defect) |
| CR-02 | P1 | L | IPv6 flows and IPv6 exporters end to end | FE-09 (item 3); fixes NF-32 |
| CR-03 | P1 | L | sFlow v5 collection (flow and counter samples) | none (its health counters feed FE-01; needs CR-01's 32-bit ifIndex) |
| CR-04 | P1 | M | Enforce NetFlow permissions and tag scoping on every flow query | none as an FE (fixes NF-04/NF-05); FE-10 builds governed saved views on top of it |
| CR-05 | P1 | M | Collector and exporter health workspace | FE-01 (plus FE-03 and NF-01) |
| CR-06 | P2 | M | Exporter onboarding wizard and pending-exporter approval | FE-01, FE-08 |
| CR-07 | P1 | M | Flow-based alert rules in the platform alert engine | FE-06 (alert integration part); fixes NF-23 |
| CR-08 | P1 | M | Per-direction capacity and a configurable 95th-percentile billing report | FE-05, FE-07 (fixes NF-13/14/15/16) |
| CR-09 | P2 | M | Capacity trend and runout forecast | FE-05 |
| CR-10 | P2 | M | Threat-intelligence IP reputation that also works offline | FE-06 (findings and lifecycle), FE-09 (enrichment) |
| CR-11 | P2 | L | Persistent detection engine with baselines and a finding lifecycle | FE-06 (fixes NF-09/21/22) |
| CR-12 | P2 | M | NBAR2 / App-ID ingestion and configurable application rules | FE-09 (items 2-3; fixes NF-20) |
| CR-13 | P2 | M | Managed IP groups and sites, with group dashboards and chargeback | FE-09 (item 1), FE-05 (group billing) |
| CR-14 | P2 | M | Host 360 profile that joins flows with UDT identity | FE-09 (items 4-5) |
| CR-15 | P2 | M | 'What changed?' spike explainer | FE-07 (traffic_changes section), FE-02 (shared filter) |
| CR-16 | P2 | L | Boundary-aware counting and multi-exporter deduplication | FE-12 |
| CR-17 | P2 | M | Complete forensics with retention-aware source planning | FE-04, FE-11 (fixes NF-10/11/12/17) |
| CR-18 | P2 | M | Cisco ASA/FTD NSEL and firewall/NAT fields | FE-09 (NAT, item 3); fixes the audit's NSEL defect |
| CR-19 | P2 | L | Collector durability and scale-out | FE-01 (delivery counters); fixes NF-06; the HA part overlaps none |
| CR-20 | P3 | S | AS, next-hop and geo views from fields already stored | FE-09 (item 5) |
| CR-21 | P3 | L | CBQoS policy monitoring (SNMP) | none |
| CR-22 | P3 | S | Flow replicator / forwarder | none |
| CR-23 | P3 | L | Cloud flow logs (AWS VPC / Azure VNet) | none |
| CR-24 | P3 | M | Scoped NetFlow API tokens, an OpenAPI contract and read-only MCP tools | none |
| CR-25 | P3 | S | Mobile and NOC-wall layout | none (fixes NF-30) |

Sources:

- **SolarWinds NetFlow Traffic Analyzer (NTA)**: <https://www.solarwinds.com/netflow-traffic-analyzer>, <https://documentation.solarwinds.com/en/success_center/nta/content/release_notes/nta_2025-4-3_release_notes.htm>, <https://documentation.solarwinds.com/en/success_center/nta/content/nta-netflow-specific-predefined-alerts-sw1797.htm>, <https://documentation.solarwinds.com/en/success_center/nta/content/nta-configuring-netflow-management-settings-sw211.htm>, <https://documentation.solarwinds.com/en/success_center/nta/content/nta-configuring-dns-resolution-sw380.htm>, <https://documentation.solarwinds.com/en/success_center/nta/content/nta-dynamically-monitoring-cbqos-sw1385.htm>
- **ManageEngine NetFlow Analyzer**: <https://www.manageengine.com/products/netflow/>, <https://manageengine.com/products/netflow/release-features.html>, <https://www.manageengine.com/products/netflow/help/billing.html>, <https://www.manageengine.com/products/netflow/help/percentile-report.html>, <https://www.manageengine.com/products/netflow/blog/netflow-analyzer-reports-with-95th-percentile-calculation.html>, <https://www.manageengine.com/products/netflow/help/capacity-planning-report.html>
- **Paessler PRTG (flow sensors)**: <https://www.paessler.com/manuals/prtg/monitoring_bandwidth_via_flows>, <https://www.paessler.com/manuals/prtg/netflow_v9_sensor>, <https://www.paessler.com/manuals/prtg/ipfix_custom_sensor>, <https://www.paessler.com/manuals/prtg/jflow_v5_sensor>, <https://www.paessler.com/monitoring/performance/netflow-traffic-analyzer>, <https://www.paessler.com/partners/uptimealliance/plixer>
- **Plixer Scrutinizer / Plixer One (FlowPro, Replicator, ML Engine, Endpoint Analytics)**: <https://docs.plixer.com/projects/plixer-scrutinizer-docs/en/latest/additional_resources/changelog_scrutinizer.html>, <https://docs.plixer.com/projects/plixer-scrutinizer-docs/en/latest/configuration_guides/guides_fa.html>, <https://docs.plixer.com/projects/plixer-scrutinizer-docs/en/latest/use_cases/cases_threat_hunting.html>, <https://docs.plixer.com/projects/plixer-scrutinizer-docs/en/latest/use_cases/cases_lateral_movement.html>, <https://docs.plixer.com/projects/plixer-scrutinizer-docs/en/19.2.0/features_and_functionality/classic_ui/system/system_leds.html>, <https://docs.plixer.com/projects/plixer-scrutinizer-docs/en/latest/features_and_functionality/fnf_ui/ui_admin/admin_resources/admin_exporters.html>
- **Kentik**: <https://kb.kentik.com/docs/release-notes-2025>, <https://www.kentik.com/press-releases/first-agentic-ai-solution-that-deeply-understands-the-network/>, <https://www.networkworld.com/article/4092276/kentik-bolsters-network-observability-platform-with-autonomous-investigation.html>, <https://kb.kentik.com/docs/capacity-planning>, <https://kb.kentik.com/docs/interface-classification>, <https://kb.kentik.com/docs/using-interface-classification>
- **ntopng (+ nProbe)**: <https://www.ntop.org/released-ntopng-6-4-more-insightful-than-ever/>, <https://github.com/ntop/ntopng/releases/tag/6.4>, <https://www.ntop.org/products/traffic-analysis/ntopng/>, <https://www.ntop.org/guides/ntopng/flows/nprobe.html>, <https://www.ntop.org/guides/ntopng/advanced_features/category_lists.html>, <https://www.ntop.org/products/netflow-probes/nprobe/>
- **ElastiFlow NetObserv**: <https://docs.elastiflow.com/changelog>, <https://forum.elastiflow.com/t/elastiflow-netobserv-v7-12-released/323>, <https://opensearch.org/solutions-providers/elastiflow/>, <https://docs.elastiflow.com/6.4/docs/flowcoll/introduction/>
- **Progress Flowmon**: <https://www.progress.com/blogs/unleashing-progress-flowmon-13-product-release>, <https://www.progresstalk.com/threads/progress-news-progress-openedge-abl-what-is-new-in-flowmon-13-1-and-flowmon-ads-13-1.204284/>, <https://www.progress.com/flowmon/platform/software-modules/anomaly-detection-system>, <https://www.progress.com/flowmon/platform/appliances/netflow-collector>, <https://www.helpnetsecurity.com/2025/09/23/progress-flowmon-ads-12-5/>
- **Noction Flow Analyzer (NFA)**: <https://www.noction.com/flow-analyzer>, <https://www.noction.com/docs/databases>, <https://www.noction.com/news/flow-analyzer-26-01>, <https://www.noction.com/news/flow-analyzer-26-02>, <https://www.noction.com/news/nfa-v26-03>, <https://www.noction.com/news/flow-analyzer-25-04>
- **Auvik TrafficInsights**: <https://www.auvik.com/network-management-software/use-case/netflow-analyzer/>, <https://www.auvik.com/network-management-software/network-traffic-analysis-software/>, <https://support.auvik.com/hc/en-us/articles/216505283-How-long-does-Auvik-store-data>, <https://support.auvik.com/hc/en-us/articles/360025584011-How-do-I-set-up-devices-for-TrafficInsights>, <https://support.auvik.com/hc/en-us/articles/360025288632-How-to-configure-NetFlow-on-Fortinet-FortiGate-firewalls>, <https://support.auvik.com/hc/en-us/articles/360025289032-How-to-configure-IPFIX-on-Aruba-OS>
- **LogicMonitor (LM Envision network traffic flow + Edwin AI)**: <https://www.logicmonitor.com/support/monitoring/networking-firewalls/netflow>, <https://www.logicmonitor.com/support/network-traffic-flow-monitoring-new-ui>, <https://www.logicmonitor.com/support/viewing-filtering-and-reporting-on-network-traffic-flow-data-new-ui>, <https://www.logicmonitor.com/support/monitoring/network-traffic-flow/viewing-filtering-and-reporting-on-netflow-data>, <https://www.logicmonitor.com/support/troubleshooting-network-traffic-flow-monitoring-operations-new-ui>, <https://www.logicmonitor.com/support/reports/report-types/netflow-device-metric-report>
- **Cisco Secure Network Analytics (formerly Stealthwatch)**: <https://www.cisco.com/c/en/us/products/collateral/security/stealthwatch/datasheet-c78-739398.html>, <https://www.cisco.com/c/dam/en/us/td/docs/security/stealthwatch/release_notes/7_5_Release_Notes/7_5_3_Release_Notes_DV_1_2.pdf>, <https://www.cisco.com/c/dam/en/us/td/docs/security/stealthwatch/release_notes/7_5_Release_Notes/7_5_2_Release_Notes_DV_1_2.pdf>, <https://blogs.cisco.com/security/secure-network-analytics-7-5-1-launch>, <https://www.cisco.com/c/dam/en/us/td/docs/security/stealthwatch/update_guides/7_6_0_Update_Guide_DV_1_1.pdf>, <https://www.cisco.com/c/en/us/products/collateral/security/stealthwatch/ucs-m6-secure-network-analytics-hw-eol.html>
## 6. Fix plan (implemented on this branch)

The contract for the implementation is `docs/netflow-review-contract.md`. Work is split into a
foundation wave and vertical feature slices so that every rule (time, filters, visibility,
classification, capacity statistics, error handling) has exactly one implementation.

| Area | Delivers | Review items |
|---|---|---|
| Collector | IPv6 flows and exporters, 32-bit ifIndex, field-presence tracking, v5 sampling mask, IE 85/86 and bidirectional counters, NSEL + NAT, NBAR2 App-ID, sFlow v5 (flow + counter samples), clock policy, durable delivery (retry, spool, dedup token, graceful drain), per-exporter statistics and template registry | NF-06, NF-07, NF-32, NF-33, X-02, X-03, X-06–X-08, X-10, X-12–X-14, X-28, CR-01/02/03/12/18/19 |
| Data layer | ClickHouse schema v2 (metadata-only upgrade), IPv6-aware rollups, duration-spread interface rollup, exporter-minute liveness, stats/template/app-name/counter tables, retention-aware healer, v5 sampling repair | NF-17, NF-32, NF-33, X-02, X-11, X-26 |
| API foundation | one window resolver (half-open, 93-day spans, timezone), one filter compiler (every key validated, exclusions, IPv6, exporter-qualified interfaces, counting mode), hybrid raw+rollup planner, safe errors with reference ids, response envelope with data-quality metadata, coverage/retention endpoints | NF-02, NF-17, NF-31, X-18–X-20, X-25, X-42, FE-02, FE-03, FE-11 |
| Access | `netflow.view` enforced, per-exporter tag scoping (fail-closed), `netflow.manage`/`netflow.export`, scoped out-of-module consumers, audited writes | NF-04, NF-05, X-01, X-15, X-16, X-46–X-48 |
| Overview | appliance-clock exporter state, server-side compare, honest states, telemetry insights, heatmap with timezone/coverage, true durations, RST over flagged TCP, what-changed explainer | NF-01–03, NF-18, NF-19, NF-23, NF-26, NF-28, NF-30, X-04, X-23, X-24, X-35, X-39, CR-15 |
| Breakdowns | bidirectional service classification, whole-scope denominators, ranking selector, filtered interfaces, AS views, preserved drill-down context | NF-08, NF-20, NF-24, NF-25, NF-27, X-19, X-29, X-30, X-40, CR-20 |
| Forensics & saved views | draft/applied search, cursor paging with totals, complete exports, filter builder, governed saved views | NF-05, NF-08, NF-10–NF-12, NF-34, X-17, X-21, X-22, X-43, X-45, X-49, FE-04, FE-10 |
| Capacity | per-direction statistics over complete, coverage-aware slots; headroom; resolution guidance; configurable billing percentile; trends and runout forecast; SNMP/sFlow cross-check | NF-13–NF-16, X-05, X-26, X-27, X-36, FE-05, CR-08, CR-09 |
| Detection | persistent engine with baselines, detector availability, evidence windows, lifecycle, exceptions, threat intelligence | NF-07, NF-09, NF-21, NF-22, X-09, X-31–X-34, X-37, X-44, FE-06, CR-10, CR-11 |
| Collector health | exporter status on the appliance clock, collector workspace, scalable mapping, collection points and counting, retention disclosure, onboarding guides | NF-01, NF-23, FE-01, FE-08, FE-11, FE-12, CR-05, CR-06, CR-16 |
| Settings & hosts | managed IP groups, application rules and NBAR names, enrichment status, host profile with UDT identity | FE-09, CR-12–CR-14, X-50 |
| Alerting & reports | NetFlow metrics in the platform alert engine, scoped and corrected report sections, management summary report | NF-04, NF-23, FE-07, CR-07 |

## 7. Implementation status

**All 34 review findings, all 50 upheld extra defects and FE-01 … FE-12 are implemented on
`fix/netflow-development-review`**, together with competitor features CR-01–CR-18, CR-19
(durability) and CR-20. Out of scope, recorded as future work: CBQoS polling (CR-21), flow
replication (CR-22), cloud flow logs (CR-23), API tokens/MCP (CR-24), distributed collectors on
sensors.

### How it was verified

* **Acceptance re-run.** Every finding's acceptance criterion was re-run live against an
  integrated stack: scratch ClickHouse 24.10 and PostgreSQL 16, the new collector fed by 12
  simulated exporters (v5, sampled v5, v9 with every scenario, IPFIX without TCP flags at
  1-in-100 with datagram loss, an IPv6 exporter, ASA-style NSEL, NBAR2 App-ID on v9 and IPFIX,
  sFlow over IPv4 and IPv6, and an exporter with a skewed clock). Checks used real scoped tokens,
  SQL and Playwright, including a browser clock ±12 h and the Riyadh time zone.
  * The first pass, by verifiers told to make each item fail, gave 92 pass and 20 partial, and
    surfaced 54 further defects. Two were P2 security issues: SSRF through threat-feed URLs, and
    scope leaks through detector status and NetFlow alert payloads.
  * Every one of the 74 open items was fixed with a regression test, then re-verified live:
    **74/74 pass**.
  * A final pass closed four more items: exporter-scoped detector exceptions (migrate-140),
    report-run share tokens, alert-sample redaction in reports, and accessibility labels. It
    also fixed a threat-intel refresh-loop bug.
* **Test suites at the end:**
  * Server: 2040 passed with every NetFlow live tier on, 0 failed; the default run is 1824
    passed and 222 skipped.
  * Frontend: TypeScript 0 errors, all 20 dashboard node test scripts pass, and the vite
    production build succeeds.
  * Collector: Go `-race` on the three collector packages (130 tests including a ClickHouse
    round trip), `go test ./...` for the poller, and about 15 million fuzz executions on the
    decoders.
* **Screenshots.** 78 final screenshots (26 pages × light 1440, dark 1440, 390 px) were reviewed
  with 0 page errors, 0 failed API calls and 0 horizontal overflow. An axe scan of Setup,
  Overview, the detector-exceptions tab and the alert wizard found 0 violations.
* **Collector adversarial hardening.** Beyond the review, it fixed:
  * a forged IPFIX template field count that allocated 4 GB from one datagram;
  * CPU amplification from zero-length fields and template-withdrawal floods;
  * unbounded template caches;
  * a fully rejected batch that stalled all collection;
  * loss at shutdown;
  * templates forgotten on restart.

### Upgrade notes

| Item | What happens / what to do |
|---|---|
| `migrate-137` (ClickHouse) | Metadata-only column additions and sort-key extensions, new tables and views. No data rewrite, except one asynchronous, replay-safe mutation that corrects rows from sampled NetFlow v5 exporters (X-02). The healer then rebuilds affected rollup hours that raw data still covers. Older rollup hours are disclosed as unverifiable. |
| `migrate-138`, `migrate-140`, `migrate-143` (PostgreSQL) | Governance tables, exception scope, NetFlow alert metrics. `netflow.manage` and `netflow.export` are granted to the operator and editor roles. |
| `migrate-144` (ClickHouse) | Insert deduplication on the rollup tables, so collector retries never double-count. |
| Collector | New unit settings: sFlow listener on UDP 6343, spool and template state under `StateDirectory=zenplus-netflow`. `install.sh` opens UDP 2055/6343 when ufw/firewalld is active. Raise `net.core.rmem_max`; until then the collector reports itself degraded. |
| API | `NETFLOW_COLLECTOR_HEALTH_URL` and `NETFLOW_EXPORT_DIR` (`/opt/zenplus/data/netflow-exports`, owned by zenplus) are set by `install.sh`. |
| Behaviour changes users will notice | NetFlow now requires `netflow.view`, and tag-scoped users see only their own exporters (unmapped exporters are hidden from them). Numbers that were previously confident zeros now show as unknown with a reason. Capacity is reported per direction. |

### Known limitations (documented, not defects)

* Delivery coverage uses the exporter's expected export interval, so a highly intermittent
  exporter can overstate coverage.
* The forensics export snapshot assumes one collector writing in receipt order (no HA collector
  exists yet).
* Detector exceptions created before migrate-140 remain global. Legacy `/reports/generate`
  reports still apply no tag scoping outside NetFlow alert messages; this predates the branch.
* **Outside NetFlow (pre-existing on the base commit):** `host_alert_service.evaluate_host_rules`
  runs a synchronous ClickHouse query on the event loop. A hung ClickHouse therefore freezes the
  whole API, including the NetFlow query budgets (NF-31 meets its budget only once that loop is
  not blocking). Raised as a separate task.

The deviations from the implementation contract are listed in
`docs/netflow-review-contract.md` §15.
