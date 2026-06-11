# 23 — Server Monitoring Module

**Status:** live since 2026-06-10 (this document describes the implemented module plus the
agent-team recommendations that came out of the review).

ZenPlus monitors servers two ways:

* **Agent-based** — the ZenPlus Agent (Go, Windows/Linux) pushes telemetry to
  `/api/v1/agents/*`. This is the deep-visibility path: CPU/memory/disk/network,
  processes, services, event-log summaries, software inventory.
* **Agentless** — for hosts that can't take an agent: SNMP today (`collection_mode=snmp`
  links a server to an SNMP-polled device); WMI/WinRM/SSH collectors are scoped in
  EPIC E7 and slot into the same `servers` model via `collection_mode`.

---

## 1. Architecture (as built)

```
Windows/Linux host                    ZenPlus controller
┌───────────────┐  enroll(token) ┌──────────────────────────────────────┐
│ zenplus-agent │ ─────────────► │ FastAPI /api/v1/agents/*             │
│  collectors   │  heartbeat 30s │   ├─ auth: bearer api-key (hashed)   │
│  local spool  │ ─────────────► │   ├─ results/host ──► ClickHouse     │
│               │  results 60s   │   │     host_* tables (+5m rollups)  │
│               │ ─────────────► │   │     + Postgres last-known        │
│               │  commands/poll │   │       inventories                │
│               │ ◄───────────── │   │     + health status computation  │
│               │  cmd results   │   │     + baseline re-evaluation     │
└───────────────┘ ─────────────► │   └─ background staleness sweeper    │
                                 │        online→stale→offline + alerts │
                                 └──────────────────────────────────────┘
```

**Storage split**

| Data | Store | Tables | Retention |
|---|---|---|---|
| Time series (cpu/mem/fs/disk-io/net/process/service/eventlog/agent-health) | ClickHouse | `host_*`, `agent_health_metrics` | 14–60 d raw |
| 5-minute rollups (cpu/mem/fs/net) | ClickHouse | `host_*_5m` via MVs | 90–365 d |
| Last-known inventory (processes, services, filesystems, NICs, software) | Postgres | `server_*_inventory` | upsert-in-place |
| Fleet records | Postgres | `servers`, `agents`, `agent_policies`, `agent_enrollment_tokens`, `agent_commands(+results)`, `agent_diagnostics`, `agent_packages` | — |
| Compliance | Postgres | `software_baselines`, `software_baseline_rules`, `server_baseline_results` | — |

All of this is now created by migrations `migrate-030-server-monitoring.sql` (Postgres) and
`migrate-030-host-metrics-clickhouse.sql` (ClickHouse) — previously these tables existed
only on the build box and fresh installs would have broken.

**Server health & staleness (new)**

* Every ingested batch computes `servers.status` + human-readable `servers.status_reasons`:
  CPU ≥ 90/98 %, memory ≥ 90/97 %, any filesystem ≥ 85/95 % (warning/critical), watched
  auto-start service stopped (warning).
* A background sweeper (60 s) rolls agents `online → stale` (120 s without heartbeat)
  `→ offline` (10 min), marks agent-collected servers `stale` after 5 min of silence,
  raises a **critical "agent offline" alert**, and the next successful heartbeat
  auto-resolves it.

**Software baselines (new)**

A baseline declares software expectations for a class of servers
(scope = os_type + site + match-all tags):

* `required` rules — package must be installed, optionally at `min_version`+
  (`exact` / `contains` / `regex` matching, robust mixed-numeric version compare);
* `prohibited` rules — package must NOT be installed (e.g. AnyDesk/TeamViewer on servers).

Outcomes per (server, rule) — `compliant / missing / outdated / prohibited` — are stored
with `first_failed_at`, surfaced in the UI, and raise/resolve alerts automatically
(deduped per rule+server). Evaluation triggers: software inventory upload, baseline CRUD,
tag changes, manual *Evaluate now*.

**UI (new module under the SERVERS sidebar section)**

| Page | Route | Highlights |
|---|---|---|
| Inventory | `/servers` | KPI strip, facet filters (status/OS/mode/tag incl. counts), live CPU/Mem/Disk/Net gauges per row, clickable tags, bulk tag/decommission/delete, top-consumers strip, deploy-agent + register-server |
| Server detail | `/servers/:id` | 11 tabs: Overview (charts, filesystems, top processes) · Performance (range-picked charts) · Processes · Services · Storage · Network · Software · Compliance · Events · Agent (heartbeat/queue/commands + actions) · Settings (danger zone) |
| Agent Fleet | `/server-agents` | fleet KPIs, filters, bulk policy/ring/upgrade/diagnostics/disable, per-agent actions |
| Agent Policies | `/agent-policies` | collection intervals, top-N, service/process watchlists, ignores, rings |
| Baselines | `/server-baselines` | baseline CRUD with rules editor, per-baseline compliance results, evaluate-now |

---

## 2. Agent data review — what it sends today

Reviewed against the live Windows 10 agent (DESKTOP-F3HUMGT, agent v1.0.0).

**Good (verified flowing end-to-end):**

| Kind | Quality |
|---|---|
| CPU | total + per-core (12 cores) ✓ |
| Memory | total/used/available/swap + pct ✓ |
| Filesystems | per-mount totals + used % ✓ (caught a real C: at 89.9 %) |
| Disk I/O | read/write Bps, IOPS, queue, util ✓ |
| Network | per-interface rx/tx Bps, errors, drops, up/down ✓ |
| Processes | top-N with PID/CPU/mem/threads/handles/user ✓ |
| Services | watchlist states incl. `not_found` detection ✓ |
| Event logs | counts per log/level + sample IDs ✓ |
| Software | registry uninstall list w/ versions+vendors (38 pkgs) ✓ |
| Agent health | queue depth, spool bytes, config hash ✓ |

**Gaps observed in the data:**

1. `cpu_user/system/iowait` and `load_1/5/15` are always 0 on Windows. Map **Processor
   Queue Length → `load_1`** and fill user/system from `%User Time`/`%Privileged Time`.
2. `cached_bytes`/`committed_bytes` are 0 — fill from perf counters (`Cache Bytes`,
   `Committed Bytes`); committed-vs-limit is a better Windows memory-pressure signal
   than used %.
3. **No uptime/boot time** in telemetry (only `uptime_seconds` of the *agent process* in
   heartbeat). Send host `boot_time` in the OS inventory → enables uptime KPI + unexpected-
   reboot alerts.
4. Service watchlist is baked into the local `agent.yaml` — it must come from the policy
   config endpoint (the server already serves `service_watchlist`; agent should consume it).

---

## 3. Recommendations for the agent team (prioritized)

**P0 — correctness/operations**

1. **Re-enroll support without reinstall** — `enrollment_token` in config + a
   `zenplus-agent enroll --token …` subcommand. (The live agent is currently heartbeating
   with no/invalid key → 401; only a fresh token fixes it.)
2. **Consume server policy config** (watchlists, intervals, top-N, ignores) from
   `GET /agents/config` ETag flow instead of local YAML; report `config_hash` honestly.
3. **Map Windows CPU fields** (queue length → load_1, user/system split) and memory
   committed/cache (see §2).
4. **Send host boot_time + uptime** in inventory.

**P1 — inventory depth (the next differentiators)**

5. **Hardware inventory**: manufacturer, model, serial, CPU model/sockets/cores, total
   DIMM layout, GPU; virtualization flag (Hyper-V/VMware/KVM guest) + hypervisor host name.
6. **Patch level**: installed hotfixes/KBs (Windows `Win32_QuickFixEngineering`,
   `dpkg/rpm` security updates pending on Linux) + pending-reboot flag → feeds baselines
   ("KB503xxxx must be present") and a patch-compliance view.
7. **Listening ports / sockets** (pid, process, port, proto) → security posture +
   auto-detection of roles (IIS, SQL, AD) for the future app-monitoring phase.
8. **Logged-in users / RDP sessions** (Windows) and last-login (Linux).
9. **Full event text for critical events** (level ≥ error, capped/sampled) — counts alone
   can't drive root-cause; today only counts+IDs ship.
10. **Certificate expiry scan** (machine cert store / configured paths) — classic server
    monitoring win.

**P2 — protocol/robustness**

11. **Batch ledger dedup**: server currently relies on `last_metric_at`; agent should send
    a monotonically increasing `sequence_start/end` (it does) and retry idempotently —
    server will add a `(agent_id, batch_id)` ledger; don't reuse batch_ids.
12. **Gzip request bodies** (`Content-Encoding: gzip`) — inventory snapshots are the bulk.
13. **mTLS or at least cert pinning** for hostile networks; key rotation already has the
    `rotate_certificate` command path — implement it agent-side.
14. **Honor `backpressure`** hints in results/heartbeat responses (server reserves the field).
15. **Time sync check** — report clock skew vs `server_time` from heartbeat response; skewed
    hosts produce misleading charts.
16. **Implement the full command set** the server already defines: `status`, `collect_now`,
    `refresh_config`, `upload_diagnostics`, `rotate_certificate`, `restart_agent`,
    `upgrade_agent`. `collect_now`/`refresh_config` make the server→agent pull loop real:
    the UI queues a command, the agent polls it (≤30 s), acts, and posts the result.

**Communication model (answering the push-vs-pull question):** the current design is
correct for firewalled fleets — *all* transport is agent-initiated HTTPS (push for
telemetry, long-poll for commands), so no inbound port on servers is ever needed. The
server "requests" things by queuing commands the agent picks up within one heartbeat
interval. Keep that; add WebSocket/long-poll upgrade later only if sub-second command
latency becomes a requirement.

---

## 4. Market alignment (agent + agentless approaches)

| Capability | Zabbix | Checkmk | Datadog | PRTG | ZenPlus now |
|---|---|---|---|---|---|
| Agent push + central policy | ✓ (active checks) | ✓ (bakery) | ✓ | partial | ✓ policies + rings |
| Agentless WMI/SSH/SNMP | ✓ | ✓ | partial | ✓ (core) | SNMP link now; WMI/SSH = E7 |
| Process/service/software inventory | ✓ | ✓ | ✓ | partial | ✓ |
| Software baseline / compliance rules | partial (templates) | partial | ✓ (SCA, paid) | ✗ | **✓ built-in** |
| Health thresholds out-of-the-box | template-based | ✓ rules | ✓ | sensor-based | ✓ defaults + reasons |
| Stale/offline detection + alert | ✓ | ✓ | ✓ | ✓ | ✓ sweeper |
| Tag-scoped grouping/policies | ✓ host groups | ✓ labels | ✓ tags | groups | ✓ tags (filter/baseline scope/bulk) |
| Staged agent upgrades (rings) | ✗ | partial | ✓ | ✗ | ✓ schema + commands (needs agent support) |
| App-level (IIS/AD/SQL) checks | ✓ | ✓ | ✓ | ✓ | next phase (E7 checks) |

Differentiated today: baselines-with-alerting and status_reasons (explainable health) are
ahead of same-tier competitors. Biggest remaining gaps to close, in order: agentless
WMI/SSH collectors (E7), host-metric conditions in the multi-condition alert-rules engine
(so users can author custom CPU/disk rules per tag/site, not just built-in thresholds),
uptime/patch inventory, then app-level checks (IIS/AD/SQL) once the agent ships
role detection.

## 5. Discovery integration (SolarWinds-style scan → import → monitor)

Network discovery (`/discovery`) feeds the Servers module directly (since 2026-06-10):

* **Scan** — a discovery profile takes subnets (CIDR), IP ranges, single IPs, or pasted
  lists, plus credentials: SNMP profiles and **Windows credentials** (WinRM; the wizard's
  Windows-credential selection now actually persists — it was silently dropped before).
  Probes: ICMP, TCP fingerprint (incl. 3389/5985/445), SSH banner, HTTP(S), SNMP system
  info, and WinRM `Get-CimInstance` (hostname/OS/version/arch/vendor/model/serial/domain).
* **Classify** — results carry `device_type`/`os`/`os_version`/ports; Windows/Linux hosts
  are recognized via OS probes or port heuristics (3389/5985/445 → windows, 22 → linux).
* **Import** — the import drawer has an **"Import as"** selector:
  * `Auto-route` (default): server-class hosts → `servers` (+ a linked ping/SNMP device
    when "enable monitoring" is on); network gear → `devices`.
  * `Network devices only` (legacy behavior) / `Servers only` / `Both`.
  Server rows are created with mapped `os_type`, `collection_mode`
  (WinRM-validated → `agentless_winrm`, SNMP → `snmp`, port 22 → `ssh`), environment,
  tags, and a `device_id` link when a device exists for the same IP.
  Dedup: servers match by primary IP or case-insensitive hostname — re-importing or a
  later agent enrollment on the same hostname updates the same record, never duplicates.
* **Schedules now run themselves** — a background scheduler loop (60 s) fires due
  discovery schedules; previously they only ran via the manual "Run scheduler now"
  button. Runs stranded by an API restart are auto-marked failed on startup.

Bulk onboarding paths: discovery import (hundreds per run, 4096-IP scan cap),
multi-use enrollment tokens (`max_uses` ≤ 100 per token) for agent rollouts, and
`POST /servers/bulk` for tagging/lifecycle at scale.

## 6. Known limitations / follow-ups

* Health thresholds are global defaults — move to per-policy overrides when needed.
* Baseline evaluation runs inline on CRUD; fine at ≤ hundreds of servers, queue it beyond.
* The alert dedupe SELECT-then-INSERT has a tiny cross-worker race (duplicate alert at
  worst, resolve clears all) — add a partial unique index if it ever matters.
* `/agents/events` endpoint is still a logging stub — wire to the E2 events store when the
  agent starts sending full event text.
* Listing live-gauges call ClickHouse on each poll (4 grouped queries) — cheap now;
  consider a 10 s in-process cache at ~1k servers.
