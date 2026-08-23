# Phase 2 — The HA Pair (2 Active-Active Controllers + Witness)

**Goal:** either controller can die — power, disk, process — and monitoring, alerting, dashboards and ingest continue with zero manual action. PG RPO 0, ClickHouse gap ≤ replication lag, automatic recovery < 5 min, rolling updates with no blind window.
**Duration:** 4–8 weeks (~41 pd + ops). **Hardware:** +1 controller (match/upgrade spec: 8 vCPU/32 GB/1 TB NVMe — and bring node 1 to the same spec), +1 witness VM (2 vCPU/4 GB/20 GB).
**Entry criteria:** Phase 1 Exit Gate passed (non-negotiable: T2/T3/T4 removed the in-process state and blocking paths that would make two nodes incorrect).

**Target roles** (from `zenplus_architect.md` §7): API active-active · singleton loops leader-elected (already true after P0-T6 — the same advisory locks work unchanged across nodes because they live in Postgres) · PG active-passive (Patroni) · CH active-active replicated · Redis active-passive (Sentinel) · VIP via keepalived for TCP 80/443 + UDP 162/2055 · one poller per node (full sharding is Phase 3; interim: active/standby poller).

---

## P2-T1 · Provision nodes, network plan, firewall matrix **[blocker]**
**Effort:** 3 pd (ops)
**Change:** stand up controller-2 and witness on the LAN; allocate the VIP address; document the port matrix and open it host-to-host:

| Flow | Port | Between |
|---|---|---|
| PG streaming + Patroni REST | 5432, 8008 | c1↔c2 (+witness for REST checks) |
| etcd | 2379, 2380 | c1↔c2↔witness |
| ClickHouse native + HTTP + interserver | 9000, 8123, 9009 | c1↔c2 |
| ClickHouse Keeper | 9181, 9234 | c1↔c2↔witness |
| Redis + Sentinel | 6379, 26379 | c1↔c2↔witness |
| API node-to-node (nginx upstream cross-proxy) | 8000 | c1↔c2 |
| VRRP | protocol 112 | c1↔c2 |
| NTP | 123 | all → core switch 10.10.101.201 (public NTP is blocked at this site) |
**Verify:** connectivity matrix script (`nc -z` each flow both directions) all green; both nodes chrony-synced to the switch (`chronyc tracking` offset < 50 ms — clock skew breaks Patroni and CH replication debugging); witness reachable from both.

## P2-T2 · Configuration cleanup: no more localhost topology **[blocker]**
**Effort:** 2 pd
**Problem:** Every DSN and cross-component URL assumes one box (`install.sh:200-213`, `docker-compose.yml:14-15` binds CH to 127.0.0.1, nginx `proxy_pass 127.0.0.1:8000`, ncm timer curls 127.0.0.1, poller URLs fixed in P1-T5).
**Change:** parameterize: `.env` gains `NODE_NAME`, `PEER_API_URL`, LAN bind addresses; ClickHouse container binds LAN (guarded by firewall + password); API binds LAN interface; nginx config template gains an upstream pool `{c1:8000, c2:8000}` with passive health checks; `install.sh` grows `--role controller --node-name c1 --peer …` flags (multi-node aware install path); ncm-backup timer uses the local node URL variable.
**Verify:** clean 2-node install from the updated `install.sh` on scratch VMs completes; `curl http://c1/api/...` served by c2's API when c1's API is stopped (upstream failover proof); config diff between nodes is only the per-node variables.

## P2-T3 · PostgreSQL: etcd + Patroni + PgBouncer **[blocker]**
**Effort:** 5 pd · **Depends on:** T1, T2
**Change:**
1. etcd 3-node (c1, c2, witness).
2. Patroni manages PG 16 on c1/c2; bootstrap by adopting the existing data dir on c1 (standard Patroni takeover), standby built via `pg_basebackup`; `synchronous_mode: on`, `synchronous_node_count: 1` (RPO 0); `pgbackrest` (P0-T1) re-pointed so WAL archiving continues from whoever is primary.
3. PgBouncer on each node; app `DATABASE_URL` → local PgBouncer; PgBouncer targets the Patroni leader via HAProxy-style health check on Patroni REST (`:8008/primary`).
**Verify:**
1. `patronictl list` shows Leader + Sync standby, lag 0.
2. **Planned switchover** (`patronictl switchover`) under S-load: API errors < 5 s, zero failed writes after retry, harness reconciliation clean.
3. **Unplanned**: `kill -9` postgres on the primary → new leader ≤ 30 s; marker-row test proves RPO 0 (write marker each second from harness; after failover, last-marker == last-acked).
4. Old primary rejoins automatically as standby when restarted (`patronictl list`).
5. pgBackRest info healthy after both drills (archiving followed the leader).

## P2-T4 · ClickHouse: Keeper + ReplicatedMergeTree conversion **[blocker]**
**Effort:** 8 pd — the largest single task in the program. **Depends on:** T1; do the conversion **before** joining node 2.
**Problem:** All 36 tables are plain MergeTree (zero replication paths); the NetFlow collector self-creates schema at startup (`cmd/netflow-collector/main.go:290-350`), which would race DDL against a replicated cluster; partitioning debt (monthly partitions on 30-day TTLs, unpartitioned `host_network_flows`, no codecs) requires table rebuilds anyway.
**Change:**
1. Deploy ClickHouse Keeper ×3 (c1, c2, witness) — native, no ZooKeeper.
2. Write a **conversion generator** (`scripts/ch-convert-to-replicated.py`) that, per table: creates `<table>_r` as `ReplicatedMergeTree('/clickhouse/tables/{shard}/zenplus/<table>', '{replica}')` with the **fixed** partitioning/codecs (daily `toYYYYMMDD` on all 30-day raw tables + `ttl_only_drop_parts=1`; `PARTITION BY toYYYYMMDD(observed_at)` on `host_network_flows`; `CODEC(Delta, ZSTD)` on timestamps and monotonic counters), then moves data (`ATTACH PARTITION FROM` where layout allows, `INSERT SELECT` otherwise), checksums, and `RENAME`s old↔new. MVs recreated to target the new tables.
3. Run the wave on the single node (replica count 1) during a maintenance window, per-table (resumable; netflow/apm tables largest).
4. Remove collector self-DDL (migration owns schema; collector keeps a startup existence *check* that fails loudly).
5. Join node 2: install CH, same Keeper config, `CREATE TABLE ... ` replicas materialize via replication (script re-runs DDL on node 2); wait for sync.
6. Update `updater/clickhouse_sync.py` execution to be leader-only + `ON CLUSTER`-free (run DDL on one node; Replicated DDL propagates via the shared Keeper path — document the chosen convention).
7. Nightly backup (P0-T2) updated to run on one replica with the other verified.
**Verify:**
1. Per-table: pre/post row counts and `cityHash64` sample checksums equal (generator prints a table report — attach it).
2. `SELECT count() FROM system.replicas WHERE is_readonly OR future_parts > 20 OR absolute_delay > 30` = 0 on both nodes.
3. Insert 1 M harness rows on c1 → visible on c2 ≤ 5 s (`SELECT count()` convergence); and vice versa.
4. TTL proof: on a converted raw table, yesterday-partition drop appears in `system.part_log` as `DropPart` (not merge-rewrites) after TTL fires.
5. Netflow collector on a node with missing table → exits loudly with a clear error (no auto-DDL).
6. OTA CH migration (a trivial test migration) applies once and replicates (ledger row present, table change visible on both).
7. `docker stop` CH on c1 for 10 min under load → c2 keeps ingesting (writers per T5); restart → c1 catches up (`absolute_delay → 0`), no gaps for the window in a per-minute count query on both replicas.

## P2-T5 · Application ClickHouse failover (Python + Go writers/readers) **[blocker]**
**Effort:** 3 pd · **Depends on:** T4
**Change:** `CLICKHOUSE_HOSTS=c1:8123,c2:8123` (Python) / `c1:9000,c2:9000` (Go): local-first, on connect/insert failure retry the peer with short backoff; poller/netflow batch writers try peer before counting a flush as failed (extends P1-T13 retry); readers (`ch_query`) same policy.
**Verify:** stop CH on the poller's node → poller journal shows peer failover, drop counters stay 0, rows land (query on survivor); API dashboards keep rendering (reads fail over); restore → traffic returns local (log line).

## P2-T6 · Redis Sentinel **[high]**
**Effort:** 2 pd
**Change:** Redis replica on c2, sentinels ×3 (c1, c2, witness), quorum 2; Python (`realtime.py`, cache helper from P1-T8) and Go (`store/redis.go`) clients become sentinel-aware (`REDIS_SENTINELS=…`, master name `zenplus`).
**Verify:** `kill -9` redis master under an open dashboard → SSE reconnects and live status updates resume ≤ 30 s; cache helper serves (stale-tolerant) throughout; `SENTINEL get-master-addr-by-name zenplus` flips.

## P2-T7 · keepalived VIP + nginx pool + UDP failover **[blocker]**
**Effort:** 3 pd · **Depends on:** T2
**Change:** keepalived VRRP instance on c1/c2 for the VIP (TCP 80/443 and UDP 162/2055 arrive at the VIP holder); health-check script demotes a node whose local nginx/API is dead; nginx on both nodes serves the SPA and proxies to the upstream pool (local-first); poller trap listener and netflow collector run on **both** nodes (listeners bound to the VIP address via `net.ipv4.ip_nonlocal_bind` or bind-all + VIP routing) — only the VIP holder receives UDP; exporters/devices are configured with the VIP for traps and flows.
**Verify:**
1. `ip addr` shows VIP on exactly one node; `systemctl stop keepalived` on holder → VIP moves ≤ 3 s (continuous `curl -m1` loop from a client records the gap).
2. UDP: harness streams NetFlow at 1 k fps to the VIP; kill the holder node → flow ingest resumes on the survivor ≤ 10 s (collector health `records_ingested` advancing); measured gap recorded (template re-learn ≤ 5 min tolerated for v9 — count `waiting for template` window).
3. Trap test to the VIP lands on survivor after failover (`snmp_traps` row).
4. Browser session across a failover: SPA reloads cleanly, JWT still valid (stateless), SSE reconnects.

## P2-T8 · Shared artifact & file store **[blocker]**
**Effort:** 4 pd
**Problem:** `artifacts/{agents,sensors}` (604 MB OVA, 42.6 MB MSI), per-sensor bootstrap tokens, report files, MIBs, geoip and `netflow-ip-groups.json` are node-local; node 2 would 404, and large downloads occupy uvicorn workers (`FileResponse` from `agents.py:69`, `sensor_api.py:67,762`).
**Change:** deploy MinIO (single-node on each controller with **site replication** between them, or one MinIO + nightly sync — decide by ops preference; NFS acceptable fallback); move the listed paths behind a small storage abstraction (`app/core/objectstore.py`: local-path mode for S tier, S3 mode for HA); downloads served via presigned redirect or nginx `X-Accel-Redirect`; sensor OVA/ISO builds (`sensors.py:332-343`, up to 900 s in-request today) become background jobs writing to the store with a 202 + status poll.
**Verify:** upload/publish an MSI via c1 → download URL works via c2 (and with c1 powered off); create-sensor with OVA option returns ≤ 2 s with a job id, OVA appears in store when done; a 604 MB OVA download does not raise API p95 for parallel requests (measure under S-load); `netflow-ip-groups.json` edit on c1 visible to c2 ≤ 60 s.

## P2-T9 · Agent & sensor multi-URL failover **[blocker]** *(external dependency: ZenPlus_Agent repo)*
**Effort:** 4 pd (2 pd controller/sensor + 2 pd agent-side in its own repo)
**Problem:** Agents and sensors freeze exactly one controller URL at enroll (`agents.py:971,999`; sensor `ZENPLUS_SERVER_URL`, `main.go:144-147`) — with a VIP this *works*, but node-level DNS/VIP failures or future URL changes still strand the fleet.
**Change:** enrollment + config responses carry `controller_urls: [https://vip, https://c1, https://c2]`; sensor Go client and Windows agent iterate the list with per-URL backoff and remember last-good; env files updated on config poll; VIP remains first so behavior is unchanged in the happy path. Ship agent 1.4 via the canary→beta→stable rings; sensor via the Phase 3 self-update or the documented re-run installer.
**Verify:** staging sensor + agent with the URL list: block the VIP (iptables on the endpoint) → heartbeats continue via c1 direct ≤ 2 missed beats (controller `last_heartbeat_at` advances); agent ring rollout observed (canary agent updates, stable untouched until promoted); enrollment of a brand-new agent stores the list (inspect its env/registry).

## P2-T10 · Cross-node singleton + peer watchdog **[blocker]**
**Effort:** 1 pd · **Depends on:** T3
**Change:** none expected for leader election (P0-T6 advisory locks now arbitrate across nodes through the shared PG) — this task **proves** it and adds the peer watchdog: each node's watchdog timer (P0-T15) also curls the peer's shallow health and raises a `controller_peer_down` product alert (through the outbox) when absent > 3 min.
**Verify:** with both nodes up, 30-min log audit: each loop `acquired` on exactly one node per tick (grep both journals, merge, assert); power off c2 → c1 acquires all loops next tick **and** raises `controller_peer_down` (email via Mailpit); c2 returns → alert resolves.

## P2-T11 · Rolling zero-downtime updates **[blocker]**
**Effort:** 4 pd · **Depends on:** T7, T10
**Problem:** The updater stops the world per node; with a pair we can do better than "less downtime" — we can do none.
**Change:** updater gains cluster awareness: read own role (VIP holder?) via keepalived state; orchestration = non-holder updates first (stop services → apply → migrate (PG via runner on leader only; CH DDL leader-only per T4.6) → start → health-gate) → keepalived priority flip → former holder updates; coordination via a PG `cluster_updates` table (state machine, both updaters poll it) so no new infra; `zenplus status` shows cluster update state; abort path documented (unfinished node keeps old version, VIP stays on healthy one).
**Verify:** publish a test release; run a rolling update under S-load with the flap driver active → zero missed polls (per-minute `ping_metrics` count has no empty minute), zero alert losses (expected flap alerts all arrive), API 5xx burst ≤ 5 s at the VIP flip; version skew window shows both nodes serving (old+new) without errors on read paths; forced health-gate failure on node 2 → update halts with node 1 untouched and VIP stable, clear operator message.

## P2-T12 · HA runbooks **[blocker]**
**Effort:** 2 pd
**Change:** `Documentation/Desighn/runbooks/`: failover & failback (planned/unplanned, per tier), node rebuild from scratch (reuse T2 install flags + T4 replica bootstrap + P0-T4 restores), split-brain prevention & recovery (etcd/Keeper quorum loss with witness down — degraded-mode rules: which node may keep writing), backup/restore in HA mode, VIP move procedure, "one node down for a week" guidance.
**Verify:** a team member who did not build Phase 2 executes the **node rebuild** runbook on a wiped c2 and reaches full sync (patronictl + system.replicas green) inside one day, using only the runbook; every drill in the Exit Gate references its runbook section.

---

## Exit Gate — Phase 2: Game Day

Full-day drill on the staging pair under S-load + flap driver + NetFlow replay. Every scenario measured and recorded in `gates/phase-2-gate-YYYYMMDD.md`.

| # | Scenario | Pass criteria |
|---|---|---|
| G1 | **Power off c1 (VIP holder)** hard, 30 min | VIP on c2 ≤ 5 s; dashboards/API continue; alerts continue (flap alerts arrive throughout); singleton loops on c2 next tick; PG failover RPO 0 (marker rows); UDP flows resume ≤ 10 s; `controller_peer_down` alert raised. Power on → auto-rejoin (PG standby, CH catch-up `absolute_delay→0`), no manual steps |
| G2 | **Power off c2** (non-holder), 30 min | No user-visible impact at all (VIP unchanged); peer-down alert; rejoin clean |
| G3 | **Kill PG primary process** only | Patroni promotes ≤ 30 s; app reconnects via PgBouncer; zero write loss |
| G4 | **Stop CH on one node**, 10 min under load | Writers/readers fail over (drop counters 0); catch-up on restart; per-minute row counts show no gap on the surviving replica |
| G5 | **Kill Redis master** | SSE resumes ≤ 30 s; no API errors beyond reconnect blips |
| G6 | **Pull the VIP interface** (ifdown) instead of power | Same as G1 outcomes — proves keepalived health scripts, not just node death |
| G7 | **Rolling update** under load (T11) | Zero missed poll minutes; zero lost/duplicated alerts; 5xx window ≤ 5 s |
| G8 | **Witness down, then failover attempted** | Cluster keeps serving with witness down (no failover needed case); documented degraded-mode behavior matches runbook when a failover *is* forced |
| G9 | **Fleet failover** | With VIP blocked to one test agent + sensor: both continue reporting via direct node URL (T9) |

**Pass = all nine green with evidence.** Any red → fix, re-run the failed scenario plus G1.
