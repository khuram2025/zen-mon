# Raw investigation: installed-software inventory & server-agent architecture

Investigator report for the Compliance & Vulnerability Management module plan.
All paths are absolute in `/opt/zenplus`. Line numbers verified 2026-08-18 on branch `feat/udt-module`.

---

## 1. Executive summary

ZenPlus **already collects and stores per-server installed-software inventory** (name / version / vendor / install date) through its Go server-monitoring agent, and already has a proto-compliance engine (**software baselines**: required/prohibited packages with min-version rules, alerting, per-rule evaluation results). What it does **not** have: OS patch/KB inventory, CVE matching, EOL tracking, or any vulnerability feed. Network devices carry `vendor` / `model` / `os_version` / `sys_object_id` / `serial_number` on the `devices` table (populated by SNMP profile regex extraction), which is the raw material for network-gear CVE/EOL matching. The appliance already has a signed, authenticated pull channel to zentryc.com (the updater's 15-minute check-in) that a vulnerability/patch feed can piggyback on architecturally.

---

## 2. Where the agent lives (and doesn't)

* **The host agent source is NOT in this repo.** It is a **Go** Windows/Linux agent developed in its own repository (`ZenPlus_Agent`). Evidence:
  * `/opt/zenplus/scripts/build-release.py:107-125` — `_agent_source_version()` looks for vendored source at `ZENPLUS_DIR / "ZenPlus_Agent" / "internal" / "model" / "model.go"` and parses `AgentVersion = "x.y.z"`; comment: *"The agent is built and versioned in its own repository; when its source is absent this builder … defers to the newest package in the artifact store."* On this box `ZenPlus_Agent/` does not exist.
  * Built artifacts live at `/opt/zenplus/artifacts/agents/windows/zenplus-agent-1.5.2.msi` (50.8 MB) with `/opt/zenplus/artifacts/agents/manifest.json` (`required_windows_version: "1.5.2"`, sha256 per package).
  * Design doc: `/opt/zenplus/Documentation/features/server-monitoring-04-windows-agent-msi-design.md` (740 lines) — "Use Go for the Windows agent", collectors list, MSI layout, spool design, YAML config example.
* `/opt/zenplus/go/` is **only the GOPATH module cache** (`pkg/mod`), not product source.
* `/opt/zenplus/poller/` is the appliance's **network poller** (Go: `cmd/poller`, `cmd/netflow-collector`, `cmd/sensor`; `internal/checker`, `pinger`, `netflow`, `store`). It does ICMP/SNMP/NetFlow against network devices; it plays **no role** in server software inventory.
* `/opt/zenplus/sensor-appliance/` is a remote-site probe VM (enrolls with a token, pulls assigned checks, pushes results over HTTPS; served via `/api/v1/sensor/appliance/*`). Distinct from the host agent; `server/app/api/v1/sensor_api.py` serves it.
* `/opt/zenplus/updater/` is the **appliance's own OTA updater agent** (Python), phoning home to zentryc.com — see §10.

---

## 3. Server-agent architecture (as built)

### 3.1 Agent-facing runtime API — `/opt/zenplus/server/app/api/v1/agents.py` (1284 lines)

Router prefix `/api/v1/agents`, docstring at lines 1-25. Auth: all endpoints except enroll/packages/install-scripts require `Authorization: Bearer <zpa_...>` + `X-Agent-Id: <uuid>` headers; API key stored as SHA-256 hash (`agents.api_key_hash`), prefix `zpa_` (line 67).

| Endpoint | Line | Purpose |
|---|---|---|
| `POST /agents/enroll` | 441 | Exchange enrollment token (or tokenless "pending" registration awaiting operator authorization) for a long-lived API key. Creates/reconciles a `servers` row by hostname (463-505) and an `agents` row (507-592). |
| `POST /agents/heartbeat` | 609 | Every 30 s (`DEFAULT_HEARTBEAT_S = 30`, line 72). Updates version, queue_depth, spool_bytes, config hash, `capabilities` (jsonb), `apm_status`; rolls `servers.last_seen`; returns `config_etag`, `has_commands`, `desired_version`. |
| `GET /agents/config` | 700 | ETag-aware signed policy config (intervals, watchlists, ignores, feature_flags, cardinality_limits). |
| `POST /agents/results/host` | 761 | Batched metric/inventory upload → `ingest_host_metric_batch` (§5). Default upload interval 60 s. |
| `POST /agents/events` | 810 | Status events (still a logging stub per doc 23 §6). |
| `POST /agents/diagnostics` | 825 | Diagnostics-bundle metadata upload. |
| `POST /agents/network-capture` | 865 | On-demand capture flow/interface-sample upload. |
| `GET /agents/packages/manifest` | 1002 | Latest package per platform/channel/arch from `agent_packages`. |
| `GET /agents/packages/{platform}/latest` | 1047 | Unauthenticated MSI/tar.gz download (sha256 in manifest gates integrity). |
| `GET /agents/install.ps1` / `install.sh` | 1119 / 1188 | Self-contained installer scripts (download → sha256 verify → silent MSI / systemd unit). |
| `POST /agents/commands/poll` | 1196 | Pull queued commands. Allowed commands (Postgres CHECK, migrate-030 line 147): `status, collect_now, refresh_config, upload_diagnostics, rotate_certificate, restart_agent, upgrade_agent` (+ capture commands added later). |
| `POST /agents/commands/{command_id}/result` | 1237 | Report command outcome → `agent_command_results`. |

**All transport is agent-initiated outbound HTTPS** (push for telemetry, pull for commands/config) — doc `23-SERVER-MONITORING-MODULE.md` lines 163-167. This matters for the vuln module: any new collection (patch level, KBs) must ride the same push envelope; "scan now" = queue a command.

### 3.2 Admin API — `/opt/zenplus/server/app/api/v1/servers.py` (3099 lines)

Four routers (lines 76-79): `/servers` (main), `/agent-policies` (`policies_router`), `/agent-fleet` (`fleet_router`), `/server-baselines` (`baselines_router`). JWT dashboard auth. Highlights:

* `GET /servers`, `/servers/facets`, `/servers/latest-metrics`, `POST /servers/bulk` (add_tags/remove_tags/set_environment/decommission/delete — tag changes re-trigger baseline evaluation, line 413).
* Per server: `/{id}/metrics`, `/processes`, `/processes/history`, `/services`, `/filesystems`, `/network`, `/events`, **`/software` (line 720)**, **`/compliance` (line 743)**, **`/evaluate-baselines` (line 770)**, `/commands`, `/agent`, network-capture endpoints, `/{id}/install-token` (1737).
* Fleet: `GET /agent-fleet` (2095), `GET/POST /agent-fleet/packages[/publish]` (2331/2347 — scans `/opt/zenplus/artifacts/agents/<platform>/zenplus-agent-<ver>.<ext>`, registers rows in `agent_packages`, newest = `is_latest`), `POST /agent-fleet/packages/download` (2441 — streams immutable MSI + mints rollout enrollment token), per-agent `authorize/revoke/rotate-certificate/request-diagnostics/commands/{command}/set-update-ring/delete`, `POST /agent-fleet/bulk` (2713).
* Policies: CRUD at 1955-2094; policy edits bump `config_version` → agents pick up via ETag.
* Baselines: CRUD + evaluate at 2930-3090 (§7).
* Capability gating helper `_agent_supports` (line 900) reads `agents.capabilities` jsonb with a legacy semver fallback map `CAPABILITY_MIN_AGENT_VERSION` (line 886: `network_capture_v1` ≥ 1.2.0, `capture_stop_v1` / `interface_traffic_v1` ≥ 1.3.0). **New collectors should advertise a capability string the same way** (e.g. a future `patch_inventory_v1`).

### 3.3 Schemas — `/opt/zenplus/server/app/schemas/agent.py` (572 lines)

Key upload envelope (lines 338-358):

```python
class MetricSample(BaseModel):
    kind: Literal["cpu", "memory", "filesystem", "disk_io", "network",
                  "process", "service_state", "event_log", "agent_health", "inventory"]
    timestamp: datetime
    data: Dict[str, Any] = Field(default_factory=dict)

class AgentResultsBatch(BaseModel):
    agent_id: str; server_id: str; batch_id: str
    sequence_start: int; sequence_end: int
    config_hash: Optional[str]; agent_version: str
    collected_at: datetime; sent_at: datetime
    metrics: List[MetricSample]
    inventory: Dict[str, Any] = Field(default_factory=dict)   # ← software rides here
    events: List[Dict[str, Any]]
```

`AgentEnrollRequest` (248) carries `os_name`, `os_version`, `kernel_or_build`, `architecture`. `AgentHeartbeatRequest` (294) carries `capabilities: Optional[List[str]]` and an optional `apm` block. Baseline schemas at 466-529.

### 3.4 Background loops — `/opt/zenplus/server/app/main.py:120-188`

Relevant: `health_sweeper_loop` (server_health_service; constants at `/opt/zenplus/server/app/services/server_health_service.py:42-45`: agent `online→stale` after 120 s, `→offline` after 600 s, server `stale` after 300 s, sweep every 60 s, critical "agent offline" alert auto-resolved on next heartbeat); `host_alert_evaluator_loop`; `_sync_agent_packages_once` (startup reconcile of MSI store → DB). **There is no agentless collection loop for servers** — see §8.

### 3.5 Fleet/agent tables (Postgres) — `/opt/zenplus/scripts/migrate-030-server-monitoring.sql`

(016 created the same core set earlier; 030 is the canonical fresh-install migration and adds baselines/status_reasons/alerts.server_id.)

* `servers` (line 21): `id, display_name, hostname, fqdn, primary_ip inet, site_id, device_id, os_type ('windows','linux','macos','bsd','other','unknown'), os_name, os_version, kernel_or_build, architecture, collection_mode ('agent','agentless_wmi','agentless_winrm','snmp','ssh','none'), status ('healthy','warning','critical','unknown','stale','disabled'), environment, owner, tags jsonb, last_seen, status_reasons jsonb (030 line 296), boot_time/cpu_cores/memory_total_bytes (migrate-042), windows_credential_id/snmp_credential_id/ncm_credential_id (migrate-034)`.
* `agents` (line 81): `agent_uid unique, hostname, platform, version, install_id, site_id, policy_id, status ('enrolling','online','stale','offline','disabled','updating','error'), api_key_hash/prefix/rotated_at, last_heartbeat_at, last_metric_at, last_config_hash, config_apply_error, queue_depth, spool_bytes, update_ring ('canary','beta','stable','pinned'), desired_version, current_version, certificate_expires_at, last_ip, tags jsonb` + `capabilities jsonb` (migrate-047, GIN indexed) + `clock_skew_s` (migrate-041) + `apm_status jsonb` (migrate-055) + authorization columns `pending_secret_hash, authorized_at/by, revoked_at/by, authorization_source, enrollment_token_prefix` (migrate-056).
* `agent_policies` (line 56): `platform, metric_interval_s (default 30), upload_interval_s (default 60), process_top_n (25), service_watchlist/process_watchlist/event_log_filters/disk_ignore/network_ignore jsonb, cardinality_limits jsonb, update_ring, feature_flags jsonb, config_version, is_builtin`. Built-ins seeded at line 285: "Windows Baseline", "Windows High Detail", "Linux Baseline". **`feature_flags` is the natural switch for enabling new compliance collectors per policy.**
* `agent_enrollment_tokens` (120), `agent_commands` (143) + `agent_command_results` (160), `agent_diagnostics` (168), `agent_packages` (185: `platform, arch, version, channel, file_name, file_size, sha256, signature, download_path, is_latest`, UNIQUE(platform,arch,version,channel)).

### 3.6 Dashboard UI

* `/opt/zenplus/dashboard/src/pages/servers/ServersPage.tsx`, `ServersDashboardPage.tsx`, `ServerDetailPage.tsx` — detail page inventory tabs at line 566: `'processes' | 'services' | 'software' | 'storage' | 'network'`; software tab fetches `GET /servers/{id}/software` (line 2020), CSV export (2064), empty-state copy at 2098: *"The agent uploads software inventory with its periodic inventory snapshot (every 6 hours by default)."*
* `AgentFleetPage.tsx` (691 lines), `AgentPoliciesPage.tsx` (458 lines), `BaselinesPage.tsx` (baseline CRUD: *"Declare required or prohibited software per server class — violations raise alerts automatically"*).
* Routes: `/servers`, `/server-agents`, `/agent-policies` (doc 23 §UI table).

---

## 4. Installed-software inventory: exact storage schema

**Postgres, last-known snapshot, upsert-in-place, no history.** `/opt/zenplus/scripts/migrate-030-server-monitoring.sql:263-271` (identical shape in migrate-016 line 293):

```sql
CREATE TABLE IF NOT EXISTS server_software_inventory (
    server_id uuid NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    package_name varchar(255) NOT NULL,
    version varchar(128),
    vendor varchar(255),
    install_date timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, package_name)
);
```

Sibling last-known inventory tables (same file): `server_process_inventory` (209, PK server_id+pid), `server_service_inventory` (224, PK server_id+service_name; `start_mode`, `state`, `pid`, `description`), `server_filesystem_inventory` (236), `server_network_interface_inventory` (251).

**Notable properties / constraints for the vuln module:**
* PK is `(server_id, package_name)` — one row per package name; if two versions of one product coexist the last write wins (versions collapse). `install_date` accepts `YYYYMMDD` registry format via `_dt_or_none` (`host_metric_service.py:56-72`).
* No normalized product identity (no CPE, no publisher normalization) and **no history** — a CVE-matching layer needs either a joinable normalized catalog table or CPE inference at match time.
* No architecture, no install location, no source (registry vs MSI vs store app), no OS-patch/KB rows.

---

## 5. Collection & ingestion path (cadence + data shape)

### 5.1 What the agent sends

* Envelope: `POST /api/v1/agents/results/host` with `AgentResultsBatch`. Software arrives in the **`inventory` dict** (top-level field or as `kind:"inventory"` samples), under key `software` (fallback `applications`), each item a dict with `package_name|name|display_name`, `version|display_version`, `vendor|publisher`, `install_date` — see accepted aliases in `/opt/zenplus/server/app/services/host_metric_service.py:462-485`.
* On Windows the agent reads the **registry uninstall list** — verified live in `/opt/zenplus/Documentation/23-SERVER-MONITORING-MODULE.md:101`: *"Software | registry uninstall list w/ versions+vendors (38 pkgs) ✓"*.
* The inventory snapshot also carries `os` (`name`, `version`, `kernel_or_build`, `architecture`, `fqdn`, `boot_time`, `uptime_seconds`), `services`, `filesystems`, `network_interfaces` — all handled in `_upsert_inventory` (`host_metric_service.py:366-525`).
* Cadence: metrics every `metric_interval_s` (30 s default), uploads every `upload_interval_s` (60 s default); the **inventory snapshot ships every ~6 hours by default** (only stated source: ServerDetailPage.tsx:2098 — agent source not in this repo to confirm). The `collect_now` command forces a fresh collection.

### 5.2 Server-side ingestion — `/opt/zenplus/server/app/services/host_metric_service.py`

* Entry: `ingest_host_metric_batch` (line 543). Groups samples by kind; `KIND_INSERTERS` (530) writes ClickHouse per-kind; inventory routed to `_upsert_inventory` (614-629).
* Software upsert (470-485): `INSERT ... ON CONFLICT (server_id, package_name) DO UPDATE SET version=EXCLUDED.version, vendor=EXCLUDED.vendor, install_date=COALESCE(EXCLUDED.install_date, existing), updated_at=NOW()`.
* **Prune-on-snapshot** (487-506): after upserting, rows with `updated_at < NOW() - INTERVAL '2 minutes'` are deleted **only for sections actually present in the payload** — an omitted section means "no update", a sent section is a full snapshot (this is how uninstalls disappear and why "prohibited" baselines self-heal).
* Clock-skew correction (574-591): batches shifted to server time when >300 s off; skew stored on `agents.clock_skew_s`.
* Post-ingest hooks (631-647): `compute_server_health`/`store_server_health` on every batch; **`baseline_service.evaluate_server` re-runs whenever the batch contained software** (`software_updated` flag) — the exact hook point where CVE re-matching should also be triggered.

### 5.3 ClickHouse side (metrics only — software is NOT in ClickHouse)

`/opt/zenplus/scripts/migrate-030-host-metrics-clickhouse.sql`: `zenplus.host_cpu_metrics`, `host_memory_metrics`, `host_filesystem_metrics`, `host_disk_io_metrics`, `host_network_metrics` (30 d TTL), `host_process_metrics` (14 d), `host_service_state` (60 d), `host_event_log_summary` (30 d), `agent_health_metrics` (30 d), plus `_5m` rollups (90-365 d). All `ORDER BY (server_id, …, timestamp)`.

---

## 6. Read paths (API + UI)

* `GET /api/v1/servers/{server_id}/software` — `servers.py:720-740`: returns `{items: [{package_name, version, vendor, install_date, updated_at}], total, truncated}`, ordered by `lower(package_name)`, limit ≤ 20000.
* `GET /api/v1/servers/{server_id}/compliance` — `servers.py:743-767`: joins `server_baseline_results` + `software_baselines` + `software_baseline_rules`, violations first, with summary counts by status.
* UI: ServerDetailPage software tab (filter + CSV export), BaselinesPage, plus baseline result surfacing in ServerDetail.

---

## 7. The existing compliance engine (software baselines) — the template to extend

Tables (`migrate-030-server-monitoring.sql:308-361`):

* `software_baselines`: `name unique, enabled, os_type?, site_id?, match_tags jsonb (AND-semantics: server must have every tag), alerting bool`.
* `software_baseline_rules`: `baseline_id FK, rule_type ('required','prohibited'), package_match varchar(255), match_type ('exact','contains','regex'), min_version varchar(128), severity ('info','warning','critical'), notes`.
* `server_baseline_results`: PK `(server_id, rule_id)`; `status ('compliant','missing','outdated','prohibited'), found_package, found_version, expected, severity, first_failed_at, evaluated_at`.

Engine: `/opt/zenplus/server/app/services/baseline_service.py` (299 lines):

* `_version_key` / `compare_versions` (38-65): tokenized mixed alnum comparison ('1.2.10' > '1.2.9', '1.2' == '1.2.0', numeric ranks above alpha so '1.2' > '1.2-beta'). **Reusable for CVE affected-version-range checks** (with caveats: no epoch/semver-prerelease semantics like debian/rpm version rules).
* `match_package` (70): exact/contains/regex (case-insensitive).
* `evaluate_server` (152): loads applicable baselines by os_type/site/tags → evaluates every rule against `server_software_inventory` → upserts `server_baseline_results` (first_failed_at latched, 202-207) → raises/resolves alerts via `create_server_alert`/`resolve_server_alerts` with dedupe key `baseline:{rule_id}` (source=`baseline`) → deletes results for no-longer-applicable rules (244-252).
* `evaluate_baseline` / `evaluate_all` (259, 291). Triggers: software ingest, baseline/rule CRUD, tag changes (servers.py:413), manual `POST /{server_id}/evaluate-baselines` and `POST /server-baselines/{id}/evaluate`.
* Alerts land in the standard `alerts` table via `alerts.server_id` (migrate-030 line 300).

This is exactly the shape a CVE evaluator wants: scope resolution → rule match → per-(asset, finding) upsert with latched first-seen → alert lifecycle with dedupe keys.

---

## 8. Agentless / WMI paths — software is NOT collected agentlessly

* `servers.collection_mode` admits `agentless_wmi` / `agentless_winrm` / `snmp` / `ssh` and migrate-034 links credential profiles (`windows_credential_id` → `windows_credentials`, `snmp_credential_id`, `ncm_credential_id`) — **but no background loop collects from agentless servers**. The `main.py` loop list (120-188) has no agentless server poller; the modes are today just inventory metadata + discovery-import classification (doc 23 §5: WinRM-validated → `agentless_winrm`, etc.).
* `windows_credentials` table — `/opt/zenplus/scripts/migrate-018-discovery-windows-creds.sql:4-20`: `name, username, domain, password_enc bytea (app.core.crypto), auth_method ('basic','ntlm','kerberos','credssp','certificate'), transport http/https, port (5985), ssl_verify` + `dc_host` (migrate-078, for UDT AD polling). API: `/opt/zenplus/server/app/api/v1/windows_credentials.py` (254 lines) — CRUD + `POST /{cred_id}/test` which runs `winrm_capability_probe`.
* Actual WinRM code paths (pywinrm, sync, wrapped in executor threads):
  * `discovery_probes.winrm_probe` (`/opt/zenplus/server/app/services/discovery_probes.py:401-494`) — single PowerShell round trip: `Get-CimInstance Win32_OperatingSystem / Win32_ComputerSystem / Win32_BIOS` → hostname, OS caption, version, arch, vendor, model, serial, domain. **No software list.**
  * `winrm_capability_probe` (542+) — per-gate rights check (WinRM session vs Security-log read).
  * `udt_ad_service.py` — WinRM to a DC for logon events (UDT feature).
* Conclusion: **agentless software inventory does not exist today.** Extending the `winrm_probe` pattern to run a `Get-ItemProperty HKLM:\...\Uninstall\*` sweep and feed the same `_upsert_inventory` path is straightforward server-side work (needs a new background loop + rate limiting), but nothing is built.

---

## 9. Network-device side (for firmware CVE/EOL matching)

* `devices` table columns — `/opt/zenplus/server/app/models/device.py:66-93`: `sys_object_id`, `vendor`, `model`, **`os_version` (String(255))**, `serial_number`, `profile_id`, plus `managed_by_device_id` / `poll_mode` for controller-managed children (APs/member switches — these get serials via template "children mapping", migrate-069).
* Population: the Go poller classifies devices via JSON SNMP profiles — `/opt/zenplus/poller/internal/checker/snmp/profile.go:40-57`: `sys_object_id_prefixes`, `sys_descr_regex`, and extraction regexes `extract_vendor`, `extract_model`, **`extract_os_version`** applied to sysDescr; upsert into Postgres at `/opt/zenplus/poller/internal/store/postgres.go:308-330` (`os_version = COALESCE(NULLIF($4,''), os_version)`). Server-side mirror for discovery in `/opt/zenplus/server/app/api/v1/snmp.py:262-296` (`matched_vendor/model/os_version` on discovery rows).
* NCM (`/opt/zenplus/server/app/api/v1/ncm.py`) stores versioned device configs (`device_configs`, `is_change` flag, migrate-029) — running-config text is a secondary source for exact firmware strings and for "recommend patch → config evidence" workflows.
* Caveat: `devices.os_version` is regex-extracted free text (e.g. "15.2(7)E3"), per-profile quality varies; there is no vendor-normalized firmware identity and no EOL data anywhere.

---

## 10. Existing zentryc.com sync channel (feed-sync precedent)

`/opt/zenplus/updater/` (Python, systemd timer):

* `config.py:14-18`: `ServerConfig.url = "https://zentryc.com"`, `check_interval_seconds = 900`, appliance `id` + `api_key` in `updater/config/agent.conf`; Ed25519 release public key at `updater/keys/zentryc-release.pub`; `max_manifest_age_days = 30`; `verify_tls = True`.
* `agent.py`: `checkin()` (line 148) POSTs `collect_inventory()` (hostname, arch, os_version, current_version, uptime, service status, disk usage, node count — `inventory.py`) to `POST /api/v1/appliances/checkin` on zentryc.com and receives release/subscription data; signed-manifest verification, ordered multi-release apply with `min_version` gate, rollback support. `clickhouse_sync.py` auto-applies CH migrations on update.
* Server-side subscription surface: `/opt/zenplus/server/app/api/v1/subscription.py`, `system_updates.py`.
* **Implication:** the vuln module's feed sync (CVE/EOL/patch metadata) has an existing pattern to copy: authenticated appliance-initiated HTTPS pull from zentryc.com every 15 min, signed payloads, staleness cap. Whether to extend the updater or add a FastAPI background loop is a plan decision; the credential (`appliance id + api_key`) and the signing infrastructure already exist.

---

## 11. Extending the agent to collect more (patch level, KBs)

**Feasibility: high — the pipeline is schema-tolerant end-to-end; the hard dependency is the separate agent repo.**

What already bends without protocol changes:
1. `MetricSample.kind` includes `"inventory"` and `data: Dict[str, Any]` — an agent can add new sections (e.g. `inventory.patches`, `inventory.os_updates`) today; the server ignores unknown sections silently (`_upsert_inventory` reads only known keys), so server-side support can ship before or after the agent.
2. `AgentResultsBatch.inventory: Dict[str, Any]` is free-form.
3. Rollout machinery is complete: `agent_packages` + rings (`canary/beta/stable/pinned`) + `desired_version` + `upgrade_agent` command + MSI publish flow — a new agent version deploys through existing plumbing.
4. Capability advertisement: agents send `capabilities` in heartbeat; server gates features via `_agent_supports` (`servers.py:900`) — add e.g. `patch_inventory_v1`.
5. Policy `feature_flags jsonb` flows through `GET /agents/config` untouched — per-policy enablement of a patch collector needs zero schema change.
6. The design doc already lists **"Windows update status"** under "Application-Ready Collectors — design now, implement later" (`server-monitoring-04-windows-agent-msi-design.md:387-398`), alongside listening ports, IIS, scheduled tasks, cert expiry.

Server-side work needed for KB/patch inventory:
* New table(s), e.g. `server_patch_inventory (server_id, patch_id/kb, title, installed_on, source)` mirroring `server_software_inventory` incl. the prune-on-snapshot idiom (`host_metric_service.py:494-506`).
* A new branch in `_upsert_inventory` + read endpoint + UI tab.
* Windows agent: `Get-HotFix`/WUA API (`Microsoft.Update.Session`) for KBs + `Win32_OperatingSystem.BuildNumber`/UBR for patch level; Linux agent: dpkg/rpm queries (`dpkg-query -W`, `rpm -qa`) — the Linux agent path exists in packaging (`install.sh`, `agent_packages` platform `linux`, "Linux Baseline" policy) but **no Linux package has ever been staged on this appliance** (`artifacts/agents/` contains only `windows/`).

---

## 12. Gaps relevant to the Compliance & Vulnerability module

1. **No OS patch/KB inventory** — only coarse `servers.os_name/os_version/kernel_or_build` (agent-reported) exists; no UBR/build-revision granularity guaranteed.
2. **No software identity normalization** — free-text `package_name`/`vendor` from the registry uninstall list; CVE matching needs a CPE/product-alias mapping layer.
3. **Version collapse** — PK `(server_id, package_name)` keeps a single version per name; side-by-side installs (e.g. multiple JREs with distinct display names usually survive, identical display names don't) and no inventory history/audit trail.
4. **No agentless software collection** — `agentless_wmi/winrm/ssh/snmp` modes are metadata only (§8); servers without agents will have empty inventory.
5. **Linux/macOS agents unproven** — schema and installer paths exist; no artifact shipped here.
6. **Inventory cadence** — ~6 h snapshot (UI claim); acceptable for vuln matching, but "evaluate after feed update" must read stored inventory, not wait for the next snapshot (the baseline engine already models this: evaluate on rule change *and* on ingest).
7. **Network gear**: `os_version` regex quality varies per profile; no EOL/EOS data; no vendor advisory mapping; controller-managed children have serials but may lack os_version.
8. **`/agents/events` is a stub** — if vuln findings should emit events, use the alerts path (`create_server_alert`) like baselines do.
9. **`agent_packages.signature`/`signed_by` columns exist but publish flow doesn't populate them** (publish inserts sha256 only, `servers.py:2396-2411`) — worth noting if the feed design leans on the same table patterns.
10. **Baseline engine runs in-request** (ingest hook, `commit=False`) — a CVE evaluator over thousands of packages × rules should be a background/advisory-locked loop like the 18 existing ones in `main.py`, not inline in ingest.

---

## 13. Quick reference — everything software-inventory in one list

| Concern | Location |
|---|---|
| Storage schema | `scripts/migrate-030-server-monitoring.sql:263-271` (`server_software_inventory`) |
| Ingest/upsert + prune | `server/app/services/host_metric_service.py:462-506` |
| Ingest entry + baseline trigger | `host_metric_service.py:543-649` (esp. 612-647) |
| Upload envelope | `server/app/schemas/agent.py:338-358` |
| Agent upload endpoint | `server/app/api/v1/agents.py:761` (`POST /api/v1/agents/results/host`) |
| Read API | `server/app/api/v1/servers.py:720` (`GET /servers/{id}/software`) |
| Compliance engine | `server/app/services/baseline_service.py` (all 299 lines) |
| Compliance tables | `migrate-030-server-monitoring.sql:308-361` |
| Compliance API | `servers.py:743,770,2930-3090`; UI `dashboard/src/pages/servers/BaselinesPage.tsx` |
| Software UI | `dashboard/src/pages/servers/ServerDetailPage.tsx:566-599,2020-2100` |
| Agent design doc | `Documentation/features/server-monitoring-04-windows-agent-msi-design.md` |
| Module architecture doc | `Documentation/23-SERVER-MONITORING-MODULE.md` |
| Device firmware fields | `server/app/models/device.py:66-93`; poller `internal/checker/snmp/profile.go:40-57`, `internal/store/postgres.go:308-330` |
| zentryc.com sync precedent | `updater/config.py`, `updater/agent.py:148` |
