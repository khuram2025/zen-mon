# Device & Server Inventory — CVE/CPE-Matching Field Survey

Raw investigation report for the Compliance & Vulnerability Management module.
Scope: how ZenPlus models and collects asset identity (vendor, model, OS/firmware
name + version, serial) for **network devices**, **servers/agents**, and
**controller-managed child devices**; where each field lives; how fresh it is;
and what is missing for reliable CVE/CPE matching.

All paths relative to `/opt/zenplus`. Live-DB samples were taken from this
appliance's Postgres (`zenplus` DB) on 2026-08-18.

---

## 1. The `devices` table — network-gear identity

### 1.1 Schema

ORM: `server/app/models/device.py` — class `Device` (lines 32–107).
DDL history: base table (pre-migration `init-postgres.sql`), then:

- `scripts/migrate-004-snmp.sql` lines 7–22 — adds SNMPv3 columns **and the four identity columns**:

```sql
ADD COLUMN IF NOT EXISTS sys_object_id        VARCHAR(255),
ADD COLUMN IF NOT EXISTS vendor               VARCHAR(100),
ADD COLUMN IF NOT EXISTS model                VARCHAR(255),
ADD COLUMN IF NOT EXISTS os_version           VARCHAR(255),
ADD COLUMN IF NOT EXISTS profile_id           UUID;
```
Indexes: `idx_devices_vendor`, `idx_devices_sys_object_id` (migrate-004 L41–42).

- `scripts/migrate-069-managed-child-devices.sql` lines 31–38 — child-device columns:

```sql
ALTER TABLE devices ADD COLUMN IF NOT EXISTS managed_by_device_id UUID REFERENCES devices(id) ON DELETE SET NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS poll_mode VARCHAR(20) NOT NULL DEFAULT 'direct';   -- direct | via_controller
ALTER TABLE devices ADD COLUMN IF NOT EXISTS serial_number VARCHAR(128);
ALTER TABLE devices ADD COLUMN IF NOT EXISTS managed_ip INET;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS managed_source VARCHAR(64);
ALTER TABLE devices ADD COLUMN IF NOT EXISTS managed_instance VARCHAR(160);
ALTER TABLE devices ADD COLUMN IF NOT EXISTS managed_last_seen TIMESTAMPTZ;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS promote_managed BOOLEAN NOT NULL DEFAULT FALSE;
```
Plus: `ip_address` becomes **nullable** (L46), partial unique index
`idx_devices_serial_unique ON devices(serial_number) WHERE serial_number IS NOT NULL`
(L54–55 — serial is the durable hardware identity), and fallback identity index
`(managed_by_device_id, managed_source, managed_instance)` (L59–61).

- `scripts/migrate-060-udt.sql` L26 — `auto_rename_from_snmp BOOLEAN DEFAULT FALSE`
  (opt-in for sysName to overwrite `hostname`).

**Identity fields on `devices` usable for CVE matching:**

| column | type | populated by | notes |
|---|---|---|---|
| `sys_object_id` | varchar(255) | poller every SNMP poll | stored with leading dot, e.g. `.1.3.6.1.4.1.9.1.2695` |
| `vendor` | varchar(100) | profile classifier / discovery import / child sync | free text; **can be `''` empty-string, not NULL** (8/37 rows live) |
| `model` | varchar(255) | profile `extract_model` regex / child sync | rarely populated (12/37 live) |
| `os_version` | varchar(255) | profile `extract_os_version` regex on sysDescr / child sync | raw vendor text, 25/37 live |
| `serial_number` | varchar(128) | **only** managed-child sync | 0/37 live on this appliance |
| `device_type` | varchar(50) | discovery / operator | `switch`, `router`, `firewall`, `access_point`, `server`, `printer`, `other` |
| `hostname` | varchar(255) | operator or sysName (if `auto_rename_from_snmp`) | |
| `tags` (JSONB), `group_id`, `location` | | operator | scoping for policy/baseline targeting |
| `profile_id` | UUID → `device_profiles` | auto-assigned once, operator override wins | which vendor template matched |
| `last_seen`, `updated_at`, `status` | | poller | freshness |

API exposure: `server/app/schemas/device.py` `DeviceResponse` (L97+) exposes
`sys_object_id` (L129), `vendor` (L130), `model` (L131), `os_version` (L132),
`serial_number` (L143). Endpoints under `/api/v1/devices` (`server/app/api/v1/devices.py`).

### 1.2 Live sample values (this appliance, 37 devices)

```
vendor breakdown: Cisco 17 | Palo Alto Networks 9 | '' (empty string) 8 | Fortinet 3
model populated: 12/37   os_version populated: 25/37   serial_number populated: 0/37
```

```
vendor | model     | os_version | sys_object_id
Cisco  |           | 17.9.4a    | .1.3.6.1.4.1.9.1.2695     (Catalyst 9300)
Cisco  |           | 17.6.5     | .1.3.6.1.4.1.9.1.2494
Cisco  | C2960     | 15.2(4)E8  | .1.3.6.1.4.1.9.1.1208
''     |           |            | .1.3.6.1.4.1.47196.4.1.1.1.325   (unmapped enterprise — no vendor at all)
''     |           |            | .1.3.6.1.4.1.674.11000.5000.100.2.1.19  (Dell EMC OS10 — unmapped)
Fortinet | FortiGate |          |  (per chassis-entity: FGT_VM64 / FGVMSLTM25019822)
Palo Alto Networks | |          |  (PAN-OS version exists only in device_template_values)
```

Key live finding: **real model + serial live in `device_entities` chassis rows, not on `devices`**:

```
hostname                          devices.model  devices.os_version  ent.model_name  ent.serial_number
SMO-RUH-DAR-...-SEC-ASW02  Cisco  ''             17.6.5              C9300-48UXM     FVH2804L79E
FGT-FW01                Fortinet  FortiGate      ''                  FGT_VM64        FGVMSLTM25019822
DR-SMO-DC-01  Palo Alto Networks  ''             ''                  PA-VM           007951000634208
SMO-...-ASW04                ''    ''            ''                  R8Q71A          VN58LBC08J
```

---

## 2. How SNMP polling populates identity

### 2.1 Poller collection (Go)

`poller/internal/checker/snmp/collector.go`:

- `Collect()` (L50–196) runs system info **first** (comment L72–74: *"so that even
  if every subsequent walk times out we've still persisted the device's identity
  (sysObjectID → vendor/model)"*).
- `collectSystem()` (L200–229): single GET of `sysDescr, sysObjectID, sysUpTime,
  sysContact, sysName, sysLocation` (OID constants `oids.go` L6–11).
- `collectEntities()` (L579–631): ENTITY-MIB walk of `entPhysicalDescr, Contained,
  Class, Name, HWRev, FWRev, SerialNum, ModelName`.
  **`OIDEntPhysicalSWRev` (`1.3.6.1.2.1.47.1.1.1.1.10`) is defined in `oids.go` L59
  but never walked or stored** — per-module *software* revision is dropped.

### 2.2 Classification: sysObjectID/sysDescr → vendor/model/os_version

`poller/internal/checker/snmp/profile.go`:

- `Profile` mirrors a `device_profiles` row (L20–32). `MatchRules` (L37–57):
  `sys_object_id_prefixes` (longest prefix wins), `sys_descr_regex` fallback,
  and **extraction regexes** `extract_vendor` / `extract_model` /
  `extract_os_version` applied to sysDescr, with `default_vendor` / `default_model`
  static fallbacks.
- `Classifier.Match()` L156–190; `Extract()` L195–219 (first non-empty capture group).

`poller/internal/pinger/engine.go` `handleSNMPResult()` (L1149–1237):

```go
if prof := e.snmpClassifier.Match(rSystem.SysObjectID, rSystem.SysDescr); prof != nil {
    v, m, o := e.snmpClassifier.Extract(prof, rSystem.SysDescr)   // L1178-1180
    ...
    e.snmpLoader.AssignProfileIfUnset(ctx, d.ID, prof.ID)          // L1182
}
e.snmpLoader.UpsertSystemInfo(ctx, d.ID, rSystem.SysObjectID, vendor, model, osVersion, rSystem.SysName)  // L1187
```

Partial results are persisted even when the poll cycle errors (L1165–1172).

### 2.3 Writeback

`poller/internal/store/postgres.go` `UpsertSystemInfo()` (L313–331):

```sql
UPDATE devices
SET sys_object_id = COALESCE(NULLIF($1,''), sys_object_id),
    vendor        = COALESCE(NULLIF($2,''), vendor),
    model         = COALESCE(NULLIF($3,''), model),
    os_version    = COALESCE(NULLIF($4,''), os_version),
    hostname      = CASE WHEN auto_rename_from_snmp THEN COALESCE(NULLIF($6,''), hostname) ELSE hostname END
WHERE id = $5
```

**Consequence:** fields are *sticky* — an empty extraction never clears a stale
value. A device downgraded/replaced whose sysDescr stops matching keeps its old
`os_version` forever. There is **no per-field timestamp**; `devices.updated_at`
is the only freshness signal and moves on any change.

- `AssignProfileIfUnset()` (L355–361) — only when `profile_id IS NULL`; operator
  choice is never overwritten.
- `UpsertEntities()` (L539–573) → `device_entities` upsert on
  `(device_id, ent_index)` with `first_seen`/`last_seen`, columns
  `class, name, serial_number, model_name, hw_revision, fw_revision`
  (table DDL: `scripts/migrate-004-snmp.sql` L99–113).
- `LoadSNMPDevices()` (L182–284) feeds current `sys_object_id/vendor/model/os_version`
  back to the poller each device sync.

### 2.4 Freshness / cadence

- SNMP cycle ticker: **30 s** (`engine.go` L253); every enabled SNMP device is
  polled per cycle (bounded at 200 workers, L1050). Device list resync from
  Postgres every **60 s** (`config.go` L77 `DeviceSyncInterval`).
- Per-device `snmp_poll_interval` column exists (default 60 s).
- Practical effect: `devices` identity and `device_entities` are **minutes-fresh**
  on any reachable device. Live `device_template_values.updated_at` = today.

### 2.5 Profile packs (source of the extraction rules)

DB is source of truth (`device_profiles`, DDL migrate-004 L45–57:
`name, vendor, match_rules JSONB, oid_groups JSONB, version, builtin`).
Optional disk bootstrap `/opt/zenplus/data/profiles/*.json`
(`engine.go` `seedSNMPProfiles()` L1243+, env `SNMP_PROFILES_DIR`).

Builtin match/extract rules (verbatim):

| template (`device_profiles.name`) | match | extraction |
|---|---|---|
| Fortinet FortiGate (`migrate-062` L95–98) | sysOID `1.3.6.1.4.1.12356.101.1`, `.15` | `extract_model: "(Forti[A-Za-z]+-[0-9A-Za-z]+)"`, `extract_os_version: "v([0-9][0-9.,a-z]*)"` |
| Cisco IOS/IOS-XE (`migrate-062` L262–266) | `sys_descr_regex "(?i)(IOS Software|IOS-XE Software|IOS \(tm\))"` | `extract_os_version: "Version ([^,\s]+)"` — **no extract_model** |
| Cisco ASA (`migrate-062` L359–362) | `"(?i)Adaptive Security Appliance"` | `extract_os_version: "Version ([^,\s]+)"` — no model |
| Palo Alto PAN-OS (`migrate-062` L429–431) | sysOID `1.3.6.1.4.1.25461.2.3` | **no extract rules at all** → `os_version` stays empty |
| F5 BIG-IP (`migrate-062` L519–520) | sysOID `1.3.6.1.4.1.3375.2.1` | none |
| Juniper JunOS (`migrate-063` L15–18) | sysOID `1.3.6.1.4.1.2636.1.1.1.2` | `extract_model: "Juniper Networks, Inc\. (\S+)"`, `extract_os_version: "JUNOS ([0-9][^\s,]*)"` |
| Aruba Wireless Controller (`migrate-063` L96–99) | sysOID `1.3.6.1.4.1.14823.1.1` | `extract_model: "MODEL: ([^)]+)"`, `extract_os_version: "Version ([0-9][^\s,]*)"` |
| MikroTik (`data/profiles/mikrotik.json`) | sysOID `1.3.6.1.4.1.14988` | `extract_os_version: "RouterOS\s+(\S+)"` |
| Linux Server (`data/profiles/linux.json`) | sysOID `1.3.6.1.4.1.8072.3.2.10` | `extract_os_version: "Linux\s+\S+\s+(\S+)"` (kernel) |
| Windows Server (`data/profiles/windows.json`) | sysOID `1.3.6.1.4.1.311.1.1.3.1.2/.3` | `extract_os_version: "Windows[^,]*Version\s+([0-9.]+)"` |

### 2.6 Vendor firmware/serial scalars in monitoring templates

`device_template_values` (DDL `scripts/migrate-062-monitoring-templates.sql` L21–33,
PK `(device_id, group_key, metric_key, instance)`, `value_text` for strings,
`updated_at` per row) — written by the poller every template poll. These are the
**best firmware-version source for firewalls** but are *not* joined into `devices`:

| metric_key | OID | live sample `value_text` |
|---|---|---|
| `fgt_fw_version` (migrate-062 L112) | `1.3.6.1.4.1.12356.101.4.1.1.0` | `v7.6.7,build3704,260601 (GA.M)` |
| `fgt_av_version` (L113) | `...101.4.2.1.0` | `93.07725(2026-08-13 06:35)` |
| `fgt_ips_version` (L114) | `...101.4.2.2.0` | `6.00741(2015-12-01 02:30)` |
| `fgt_ha_serial` (L151, per HA member) | `...101.13.2.1.1.2` | `FGVMSLTM25019822` |
| `fgt_sw_version` (L235, per FortiSwitch) | `...101.24.1.1.1.5` | — |
| `pan_sw_version` (L494) | `1.3.6.1.4.1.25461.2.1.2.1.1.0` | `11.1.10-h25` |
| `pan_app_version` / `pan_av_version` / `pan_threat_version` / `pan_wildfire_version` (L495–498) | | `5647-6174`, `9136-10199` |
| `f5_version` (L632) | `1.3.6.1.4.1.3375.2.1.4.2.0` | — |
| `f5_serial` (L633) | `1.3.6.1.4.1.3375.2.1.3.3.3.0` | — |

Alerting on any of these is already allowed via `metric LIKE 'tpl\_%'` escape in
`alert_rules_metric_check` (migrate-062 L41–66). Canonical alert-metric list
maintenance gotcha: `scripts/migrate-073-alert-metric-constraint-canonical.sql`.

---

## 3. Discovery (identity at scan time)

### 3.1 Result schema

`scripts/migrate-017-discovery-v2.sql` L148–196 `discovery_results_v2`:
`ip_address, mac_address, hostname, fqdn, sys_name, sys_object_id,
serial_number VARCHAR(255), vendor VARCHAR(150), device_type, model VARCHAR(150),
os VARCHAR(150), os_version VARCHAR(60), protocols_detected, open_ports,
confidence_score, raw_data JSONB, scanned_at` — note discovery has a distinct
**`os` (name)** column that `devices` lacks.

### 3.2 Identification logic

`server/app/services/discovery_identify.py`:
- `_SYS_OBJECT_PREFIXES` (L25–44): 17 hardcoded enterprise-OID→vendor mappings
  (Cisco 9, Juniper 2636, Fortinet 12356, MikroTik 14988, Aruba 14823,
  Palo Alto 25461, HP 11/232, Ubiquiti 41112, TP-Link 11863, Dell 674,
  Microsoft 311, Net-SNMP 8072, Force10 6027, Netgear 4526, Brocade 1991, Zyxel 890).
- `_SYS_DESCR_PATTERNS` (L50–71): regex→`{vendor, os, device_type}` hints
  (Cisco IOS-XE/IOS/NX-OS/ASA, Junos, FortiOS, RouterOS, ArubaOS, PAN-OS, ESXi …).
- HTTP Server-header / page-title / SSH-banner patterns (L77–121).
- `identify()` (L199–384) merges probes; SNMP wins for network gear (L314–344);
  WinRM fills `vendor/model/os/os_version/serial_number` for Windows (L347–367).

### 3.3 WinRM probe — richest server identity

`server/app/services/discovery_probes.py` L431–482: one PowerShell round trip:

```powershell
$o = Get-CimInstance Win32_OperatingSystem; $c = Get-CimInstance Win32_ComputerSystem; $b = Get-CimInstance Win32_BIOS;
[pscustomobject]@{ Hostname=$env:COMPUTERNAME; OS=$o.Caption; Version=$o.Version; Arch=$o.OSArchitecture;
                   Vendor=$c.Manufacturer; Model=$c.Model; Serial=$b.SerialNumber; Domain=$c.Domain }
```
→ probe data keys `os, os_version, arch, vendor, model, serial, domain`.

### 3.4 Import into inventory — **serial is dropped**

`server/app/api/v1/discovery_v2.py` `import_results` (L801+): the device leg
builds `Device(...)` (L877–895) copying `sys_object_id, vendor, model, os_version,
profile_id` — **`result.serial_number` is not copied** (devices.serial_number is
reserved for managed children); the server leg `_create_server_from_result`
(L762–798) copies `os → servers.os_name`, `os_version`, but demotes hardware
vendor/model to a description string (L771–772:
`"Imported from network discovery — {vendor model}"`).

---

## 4. Servers & agents (OS + installed software)

### 4.1 `servers` table

`scripts/migrate-030-server-monitoring.sql` L21–47 (superset of the older
migrate-016 variant):

```sql
CREATE TABLE IF NOT EXISTS servers (
    id uuid PRIMARY KEY ..., display_name varchar(255) NOT NULL,
    hostname varchar(255), fqdn varchar(255), primary_ip inet,
    site_id uuid, device_id uuid REFERENCES devices(id),
    os_type varchar(20) CHECK (os_type IN ('windows','linux','macos','bsd','other','unknown')),
    os_name varchar(255), os_version varchar(128),
    kernel_or_build varchar(128), architecture varchar(32),
    collection_mode varchar(20) CHECK (... 'agent','agentless_wmi','agentless_winrm','snmp','ssh','none'),
    status ..., environment, owner, tags jsonb, last_seen timestamptz, ...)
```
Plus `migrate-042-server-uptime.sql` L12–14: `boot_time, cpu_cores,
memory_total_bytes`; `migrate-034-server-agentless-credentials.sql`:
`windows_credential_id / snmp_credential_id / ncm_credential_id`.
`servers.device_id` cross-links a server to its network-device record.

Live sample (`servers`, 3 rows):

```
JMP | windows | Microsoft Windows Server 2022 Standard Evaluation | 21H2 | 10.0.20348.587 Build 20348.587 | x86_64 | agent
```

### 4.2 What the agent reports

Enrollment: `POST /api/v1/agents/enroll` (`server/app/api/v1/agents.py` L441)
takes `AgentEnrollRequest` (`server/app/schemas/agent.py` L248–261):
`hostname, platform, fqdn, primary_ip, os_name, os_version, kernel_or_build,
architecture, version (agent), install_id` → creates/updates `servers` row
(agents.py L472–496) and the `agents` row (`agents` DDL migrate-030 L81–112:
`agent_uid, platform, version, current_version, capabilities via heartbeat…`).

Continuous ingest: `POST /api/v1/agents/results/host` (agents.py L761–805) with
`AgentResultsBatch` (schemas/agent.py L346–358): `metrics: [MetricSample]`
(kinds incl. `"inventory"`, L338–343) and a top-level `inventory: Dict`.

`server/app/services/host_metric_service.py` inventory ingest:
- `inventory.os` → `servers.os_name / os_version / kernel_or_build /
  architecture / fqdn` via COALESCE update (L509–525) — sticky like devices.
- `inventory.boot_time` → `servers.boot_time` (L380–384).
- `inventory.services` → `server_service_inventory` (L386–408).
- `inventory.filesystems` → `server_filesystem_inventory` (L410–435).
- `inventory.network_interfaces` → `server_network_interface_inventory` (L437–460).
- **`inventory.software` (or `applications`) → `server_software_inventory`**
  (L462–485), accepted keys per item:
  `package_name|name|display_name`, `version|display_version`,
  `vendor|publisher`, `install_date`.
- Prune (L494–506): rows not re-stamped within **2 minutes** of a sent section
  are deleted — inventory is a *complete snapshot per upload*; freshness =
  agent policy `upload_interval_s` (default 60 s, `agent_policies` migrate-030 L62–63).

Agent source is not in this repo (binaries under `artifacts/agents/`,
`ZenPlus Agent 1.5.4` visible in its own software inventory); the ingest contract
above is authoritative.

### 4.3 `server_software_inventory` — the CVE-matchable software list

DDL migrate-030 L263–271:

```sql
CREATE TABLE IF NOT EXISTS server_software_inventory (
    server_id uuid NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    package_name varchar(255) NOT NULL,
    version varchar(128), vendor varchar(255), install_date timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, package_name));
```

Live sample rows:

```
Google Chrome | 151.0.7922.110 | Google LLC
FortiClient VPN | 7.4.3.4726 | Fortinet Technologies Inc
Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.40.33810 | 14.40.33810.0 | Microsoft Corporation
PuTTY release 0.83 (64-bit) | 0.83.0.0 | Simon Tatham
RustDesk | 1.4.9.29722256 | Purslane Tech Pte. Ltd.
ZeroTier One | 1.16.1 | ZeroTier, Inc.
```

Read API: `GET /api/v1/servers/{server_id}/software` (`server/app/api/v1/servers.py`
L720–740, limit ≤ 20000).

### 4.4 Existing compliance engine (the module's closest precedent)

`software_baselines` / `software_baseline_rules` / `server_baseline_results`
(migrate-030 L308–358). Rules: `required|prohibited`, `package_match` with
`exact|contains|regex`, optional `min_version`, severity; scope by
`os_type + site + match_tags`.

`server/app/services/baseline_service.py`:
- `_version_key()` L38–51 + `compare_versions()` L54–65 — tokenized
  numeric/alpha version comparison (`'1.2.10' > '1.2.9'`, pads `'1.2'=='1.2.0'`).
  **Reusable for CVE `versionEndExcluding` checks.**
- `match_package()` L70–82; `_evaluate_rule()` L118–149 (statuses
  `compliant|missing|outdated|prohibited`); `evaluate_server()` L152+ upserts
  `server_baseline_results` and raises/resolves server alerts.
- Triggered from software-inventory ingest, CRUD, and
  `POST /api/v1/servers/{id}/evaluate-baselines` (servers.py L770–778);
  results at `GET /api/v1/servers/{id}/compliance` (L743–767).

---

## 5. Controller-managed child devices (APs / FortiSwitches)

`server/app/services/managed_device_service.py` (+ `migrate-069`):

- Vendor packs declare a `children` mapping on a table group
  (`OidGroupChildren`, `server/app/schemas/snmp.py` L130–146):
  `device_type, vendor, status_key, status_map, model_key, os_version_key,
  serial_key, ip_key`.
- Seeded mappings (migrate-069 L83–134): FortiGate `access_points`
  (`vendor:"Fortinet"`, `model_key:"fgt_ap_model"`), FortiGate `fortiswitch`
  (`os_version_key:"fgt_sw_version"`), Aruba `access_points`
  (`model_key:"aruba_ap_model"`, `ip_key:"aruba_ap_ip"`).
- `_controller_report()` (L95–144) reads the parent's `device_template_values`
  rows and emits child records `{hostname, device_type, vendor, model,
  os_version, serial, managed_ip, status}`.
- `_sync_controller()` (L147–299): identity resolution **serial first**
  (`by_serial`, L169–185), else `(controller, group_key, instance)`; INSERT
  (L188–223) writes `vendor, model, os_version, serial_number,
  poll_mode='via_controller'`; updates only overwrite non-NULL incoming values
  (L244–253). Children get `topology_dependencies` rows of type `'controller'`
  (L287–298).
- Cadence: sync loop every **60 s** (L46), children unreported for
  **10 min** decay to `unknown` (L49, L336–348). Opt-in per controller via
  `devices.promote_managed`.

So child devices *can* carry vendor+model (+serial/os_version where the pack
maps one), but coverage depends entirely on per-pack `children` key mappings —
today FortiAP children get model but **no serial or firmware version key**, and
Aruba AP children get model + IP only.

---

## 6. Adjacent identity sources

- **NCM configs** — `device_configs` (`scripts/migrate-023-ncm.sql` L7–18):
  full `running|startup` config text per device, hash-deduped, captured
  `manual|api|ssh` (`server/app/api/v1/ncm.py`). Nothing parses versions out of
  configs today, but config text is a viable secondary source (e.g. Cisco
  `version 17.9` line) and the substrate for config-compliance rules.
- **UDT endpoints** — `udt_endpoints` (`migrate-060` L29–49): per-MAC
  `vendor` (OUI lookup via `udt_oui`), `hostname`, `ip_address`,
  heuristic `endpoint_type` (+ `udt_class_rules` overrides, migrate-070).
  No OS/version — unusable for CVE matching but useful for "unmanaged asset"
  visibility counts.
- **NetFlow exporters** — `netflow_exporter_devices` (`migrate-054` L17–25)
  only maps exporter IP → device; no identity.
- **`device_interfaces`** (migrate-004 L76–92) — MACs/names, no identity value
  beyond linking.
- **Discovery `snmp_mibs`**, `discovery_results` v1 (migrate-005 L28+ incl.
  `matched_os_version`) — legacy, superseded by v2.

---

## 7. Central-server sync precedent (for the CVE/patch feed)

The updater already implements appliance↔zentryc.com sync
(`updater/config.py` L16: `url = "https://zentryc.com"`; `updater/agent.py`):
`POST /api/v1/appliances/register` (L117), `POST /api/v1/appliances/checkin`
(L157, pushes node-count inventory from `updater/inventory.py`),
`GET /api/v1/appliances/subscription` (L196), `POST /api/v1/updates/check`
(L214), `POST /api/v1/updates/report` (L265). API-key header auth, httpx client.
A CVE/EOL feed sync can clone this client pattern (and the server side lives in
the same Django app as the KB, per team memory).

Server-side settings UI precedent: `server/app/api/v1/system_updates.py`
(license/registration, L371, L565–605).

---

## 8. Gap analysis for CVE/CPE matching

### Network devices

1. **No normalized vendor/product identifiers.** `devices.vendor` is free text
   and inconsistent in *presence* (`''` empty-string vs NULL vs `"Palo Alto
   Networks"`); no CPE vendor/product columns anywhere. Vendor spellings come
   from three independent hardcoded lists that must be kept in sync:
   `discovery_identify.py` `_SYS_OBJECT_PREFIXES`, `device_profiles.match_rules`
   (`default_vendor`), and migrate-069 children mappings.
2. **Model coverage is poor** (12/37 live). Cisco/ASA/PAN/F5 templates have no
   `extract_model`. The authoritative model is in
   `device_entities WHERE class='chassis'` (`model_name`, e.g. `C9300-48UXM`)
   but nothing promotes it to `devices.model`. Similarly the **chassis serial**
   (`device_entities.serial_number`, 403 rows live) never reaches
   `devices.serial_number` (0/37 populated) — only controller-children get one.
3. **No OS *name* on devices** — only `os_version`. `IOS` vs `IOS-XE` vs `NX-OS`
   vs `PAN-OS` (different CPE products with disjoint CVE sets) is only implicit
   in `profile_id`/sysDescr. Discovery has an `os` column; `devices` does not.
4. **sysDescr is not persisted** on `devices` (poller extracts and discards;
   only `discovery_results_v2.raw_data` keeps it for scanned hosts). Re-parsing
   with improved rules requires a re-poll.
5. **PAN-OS / F5 versions never reach `devices.os_version`** (no extract rules)
   even though `pan_sw_version` = `11.1.10-h25` sits in
   `device_template_values`. No generalized "identity keys" writeback from
   template values (only children mappings do this, for children).
6. **Version strings are raw vendor text**: `17.9.4a`, `15.2(4)E8`,
   `v7.6.7,build3704,260601 (GA.M)`, `11.1.10-h25`. CVE range matching needs a
   per-vendor normalizer (strip `v`/`,buildNNNN`, Cisco train notation, PAN
   hotfix `-hNN`).
7. **Staleness is invisible**: `UpsertSystemInfo` COALESCE semantics never clear
   a field and there is no per-field `*_updated_at`; a long-dead device keeps
   showing its last-known firmware.
8. **entPhysicalSoftwareRev not collected** (`oids.go` L59 defined, unused) —
   per-module/per-member software versions (stack members, supervisors) missing;
   `device_entities` has only `hw_revision`/`fw_revision` (fw empty on most rows live).
9. **Unmapped enterprise OIDs → blank identity**: 8/37 live devices
   (sysOID `1.3.6.1.4.1.47196...`; also Dell EMC OS10 `674.11000...`) have no
   vendor/model/version at all. The module needs a much larger sysObjectID→CPE
   dictionary (feed-updatable, not hardcoded).
10. **No EOL data of any kind** (only competitive-analysis mentions under
    `Documentation/enahce1/`). No table for hardware/software EOL milestones.
11. **No identity-change history** — `devices.os_version` overwritten in place;
    a patch-verification workflow ("was 17.6.5, now 17.9.4a") has no record.
    (NCM config versions are the only historical trail, where enabled.)

### Servers / software

12. **`server_software_inventory` lacks architecture, package type/source
    (MSI/dpkg/rpm), and any normalized product key.** `package_name` is the raw
    Windows display name — e.g.
    `Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.40.33810`
    embeds arch and version in the name; PK `(server_id, package_name)` means a
    version-in-name upgrade creates a *new* row (old one pruned via the
    2-minute rule, so no history either).
13. **`servers.os_name/os_version/kernel_or_build` are free text**
    (`Microsoft Windows Server 2022 Standard Evaluation` / `21H2` /
    `10.0.20348.587 Build 20348.587`) — needs mapping to
    `cpe:/o:microsoft:windows_server_2022` + build for OS CVE matching; no
    edition/SKU normalization; Linux distro name/version parsing untested here
    (agent contract passes whatever the agent sends).
14. **No hardware vendor/model/serial columns on `servers`** even though the
    WinRM discovery probe collects Manufacturer/Model/BIOS SerialNumber — it is
    flattened into a description string on import (discovery_v2.py L771–772).
15. **No installed-KB/hotfix inventory** for Windows (only package list), and no
    Linux kernel-package list distinct from `kernel_or_build`.

### Cross-cutting

16. **No asset-identity join layer**: identity is scattered across `devices`,
    `device_entities`, `device_template_values`, `servers`,
    `server_software_inventory`, `discovery_results_v2` with different vendor
    spellings and no shared normalized (vendor, product, version, serial) view —
    the compliance module's first schema task is exactly this normalization
    (either materialized columns + backfill triggers, or a view/service that
    implements the precedence: template value ▸ chassis entity ▸ profile
    extraction ▸ discovery ▸ manual).
17. **Version comparison exists once** (`baseline_service.compare_versions`) and
    is generic-tokenized — fine for dotted versions, wrong for Cisco
    `15.2(4)E8`-style trains; CVE range evaluation needs vendor-aware semantics.
18. Useful, already-present nuggets for the module: FGT/PAN **signature DB
    versions** (AV/IPS/Threat/WildFire) for "protection content freshness"
    checks; `agents.capabilities`; RBAC `require_permission` dependency
    (migrate-074) for new endpoints; tags/groups/sites for scoping;
    alert `metric` CHECK constraint must be extended via migration for any new
    `vuln_*` alert metrics (canonical list: migrate-073).

---

## 9. Quick reference — every CVE-usable field and its home

| Asset class | Field | Table.column | Collector & cadence |
|---|---|---|---|
| Network device | vendor | `devices.vendor` | poller classifier per SNMP poll (30–60 s); discovery import; child sync (60 s) |
| Network device | model | `devices.model` (sparse) / `device_entities.model_name` (chassis) | profile regex / ENTITY-MIB walk per poll |
| Network device | OS version | `devices.os_version` (raw text) | profile `extract_os_version` on sysDescr |
| Network device | firmware (fw) | `device_template_values.value_text` (`fgt_fw_version`, `pan_sw_version`, `f5_version`, `fgt_sw_version`) | template poll, per-row `updated_at` |
| Network device | serial | `device_entities.serial_number` (chassis/modules); `devices.serial_number` (children only); `fgt_ha_serial`, `f5_serial` template values | ENTITY-MIB / template poll |
| Network device | sysObjectID | `devices.sys_object_id` | every poll |
| Network device | sysDescr | *not persisted* (discovery `raw_data` only) | — |
| Child AP/switch | vendor/model/os_version/serial | `devices.*` via children mapping | managed sync 60 s, stale 10 min |
| Server | OS name/version/build/arch | `servers.os_name/os_version/kernel_or_build/architecture` | agent enroll + inventory upload (60 s default) |
| Server | installed software | `server_software_inventory(package_name, version, vendor, install_date)` | inventory snapshot, 2-min prune |
| Server (discovered) | hw vendor/model/BIOS serial, OS caption | `discovery_results_v2.vendor/model/serial_number/os/os_version` (+`raw_data`) | on scan only; serial dropped at import |
| Endpoint (UDT) | OUI vendor, type | `udt_endpoints.vendor/endpoint_type` | UDT poll (default 5 min) |
