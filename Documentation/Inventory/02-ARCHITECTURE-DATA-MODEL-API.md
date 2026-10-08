# 02 — Architecture, Data Model & API

*Status: Design proposal · 2026-10-07. Names pinned in `00-INDEX.md`. DDL is a design sketch. The shipped migration must also pass `build-release.py lint-migrations`: idempotent (`IF NOT EXISTS`, `ON CONFLICT`), probeable, `BEGIN/COMMIT`, and with GRANTs for tables and sequences.*

---

## 1. Design principles

1. **Raw evidence is never overwritten.** Collectors keep writing raw strings exactly as today. Normalised identity lives in its own layer, so a bad rule can be fixed and re-run without loss.
2. **Capture history at the database, not in each writer.** The Go poller, the agent ingest and the API all write identity. Triggers record changes no matter which writer made them.
3. **One asset model, many sources.** Servers and network devices are queried through one asset union. UDT endpoints join it in P3. A server linked to its SNMP device row (`servers.device_id`) counts **once**.
4. **Unknown is a value.** Missing OS, model or lifecycle data is shown as *Unknown*, counted, and surfaced as an Inventory-health worklist. It is never dropped or treated as compliant.
5. **Fail closed on scope.** Every endpoint applies tag scope. Aggregates for scoped users are computed over visible assets only. Fleet-wide trend data is hidden from scoped users rather than leaked.
6. **Inventory first, compliance consumes.** Lifecycle, policies and CVE matching read `inventory_identity` and the software keys. None of them builds its own identity layer.

## 2. Architecture

```
 COLLECTION (unchanged writers)                 NORMALISATION & HISTORY (new)                 SERVING (new)
 ┌──────────────────────────────┐   triggers   ┌──────────────────────────────────┐        ┌──────────────────────────┐
 │ Go poller ─► devices         │─────────────►│ inventory_identity (dirty rows)  │        │ api/v1/inventory.py      │
 │   (vendor, model, os_version,│              │   ▲ inventory_normalize_loop     │        │  summary · assets ·      │
 │    firmware, serial, sysDescr)│             │   │ (2 min, batches, adv. lock)  │        │  os · software · hardware│
 │ device_template_values       │─────────────►│   │ inventory_normalize.py      │───────►│  changes · export ·      │
 │   (fgt_fw_version, pan_sw_…) │              │   │ + JSON rule packs            │        │  catalog · settings      │
 │ device_entities (ENTITY-MIB) │              │ inventory_change_events ◄─ AFTER │        └────────────┬─────────────┘
 │ agent ingest ─► servers,     │─────────────►│   UPDATE on identity + software  │                     │
 │   server_software_inventory  │              │ inventory_daily_snapshots ◄─ loop│        ┌────────────▼─────────────┐
 │ (P3) SSH collector, KB list  │              │ (P2) catalog: products · rules · │        │ dashboard pages/inventory│
 │ (P3) udt_endpoints           │              │      publishers · asset_meta     │        │ reports sections · sched.│
 └──────────────────────────────┘              │ (P4) eol_definitions · overrides │        │ (P5) policy engine       │
                                               └──────────────────────────────────┘        │ (P6) CVE matcher         │
                                                                                           └──────────────────────────┘
```

## 3. Asset model

### 3.1 The asset union (`services/inventory_query.py`)

This is a CTE built in Python, not a database view, so it can change without a migration. One row per asset:

| Column | Server source | Device source |
|---|---|---|
| `asset_kind` | `'server'` | `'device'` |
| `asset_id` | `servers.id` | `devices.id` |
| `name` | `display_name` | `hostname` |
| `ip` | `primary_ip` | `ip_address` (or `managed_ip`) |
| `asset_type` | `os_type` | `device_type` |
| `vendor` / `model` / `serial` | `inventory_identity.hw_*` | `inventory_identity.hw_*` |
| `os_family` / `os_product` / `os_release` / `os_version` / `os_build` | `inventory_identity.os_*` | `inventory_identity.os_*` |
| `site` | `site_id → sites.name` | `location` (free text); `group_id → device_groups.name` |
| `tags` | `servers.tags` | `devices.tags` |
| `sources` | `collection_mode` (+ `'snmp'` when a linked device exists) | `'snmp'` / `managed_source` |
| `last_seen` | `servers.last_seen` | `devices.last_seen` |
| `inventory_at` | `servers.inventory_updated_at` | `devices.identity_updated_at` |
| `freshness` | computed: `fresh` / `aging` / `stale` / `never` (thresholds in settings §9) | same |

Dedupe rule: `devices d WHERE NOT EXISTS (SELECT 1 FROM servers s WHERE s.device_id = d.id)`. A linked device's SNMP data enriches the server row, for example its serial when the agent has none.

### 3.2 `inventory_identity`: one normalised identity row per asset (P0)

```sql
CREATE TABLE IF NOT EXISTS inventory_identity (
    asset_kind        varchar(16)  NOT NULL CHECK (asset_kind IN ('server','device','endpoint')),
    asset_id          uuid         NOT NULL,
    vendor_key        varchar(64),            -- 'fortinet', 'paloalto', 'cisco', 'microsoft', 'dell' …
    vendor_label      varchar(128),           -- 'Fortinet', 'Palo Alto Networks', …
    os_family         varchar(32),            -- 'windows','linux','fortios','panos','ios','ios-xe','nx-os','junos',
                                              -- 'tmos','aos-cx','arubaos','routeros','dellos10','other','unknown'
    os_product        varchar(128),           -- 'Windows Server', 'FortiOS', 'PAN-OS', 'IOS XE', 'Ubuntu'
    os_release        varchar(64),            -- lifecycle cycle: '2022', '7.2', '11.1', '17.9', '22.04'
    os_version        varchar(128),           -- normalised: '10.0.20348.587', '7.2.5', '11.1.10-h25', '17.9.4a'
    os_build          varchar(64),            -- '20348.587', '1517'
    os_edition        varchar(64),            -- 'Standard', 'Datacenter', 'Evaluation'
    os_source         varchar(16),            -- 'agent','profile','template','entity','managed','ssh'
    hw_vendor         varchar(128),
    hw_model          varchar(255),
    hw_serial         varchar(128),
    hw_source         varchar(16),
    lifecycle_slug    varchar(64),            -- P4: endoflife.date product, e.g. 'windows-server', 'fortios'
    norm_rev          integer,                -- NULL = dirty (a source changed since the last write)
    values_rev        integer,                -- rule-pack revision the stored values were computed under
    source_changed_at timestamptz NOT NULL DEFAULT now(),
    normalized_at     timestamptz,
    PRIMARY KEY (asset_kind, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_inv_identity_os     ON inventory_identity (os_family, os_release, os_version);
CREATE INDEX IF NOT EXISTS idx_inv_identity_hw     ON inventory_identity (vendor_key, hw_model);
CREATE INDEX IF NOT EXISTS idx_inv_identity_dirty  ON inventory_identity (asset_kind) WHERE norm_rev IS NULL;
```

**Dirty marking** uses cheap SQL triggers and never calls Python:

- **`devices` AFTER UPDATE OF** `vendor, model, os_version, firmware_version, serial_number, sys_object_id, sys_descr`, with `WHEN (row(OLD.…) IS DISTINCT FROM row(NEW.…))`. Upserts `inventory_identity` with `norm_rev = NULL, source_changed_at = now()`. AFTER INSERT seeds the row.
- **`servers` AFTER UPDATE OF** `os_name, os_version, kernel_or_build, architecture, cpu_model, hw_manufacturer, hw_model, hw_serial`, with the same pattern.
- **`device_template_values` AFTER INSERT OR UPDATE OF `value_text`**, `WHEN (NEW.metric_key IN ('fgt_fw_version','pan_sw_version','f5_version','f5_serial') AND OLD.value_text IS DISTINCT FROM NEW.value_text)`. This marks the owning device dirty.
- **ON DELETE** of a server or device removes its identity row; `inventory_change_events` keeps the history.

**Two revision columns, two jobs.** `norm_rev` is the dirty flag: dirty-marking sets it to NULL, and a normalised write sets it to the current revision. `values_rev` records the rule-pack revision under which the stored *values* were computed; every writer of normalised values sets it to `RULEPACK_REV` in the same statement as `norm_rev`, and dirty-marking never touches it. The identity capture trigger fires only when `OLD.values_rev IS NOT DISTINCT FROM NEW.values_rev`, so **a rule-pack revision change can never write history**, whichever writer made it and whether or not the row was dirty. The price: a genuine change that lands in the very write that moves the revision is absorbed. A false event is worse than a missing one.

**`inventory_normalize_loop`** runs every 2 min (45 s start delay) with advisory lock 1515074397, in separate short transactions for identity, software-key backfill and orphan cleanup:

1. Reads raw inputs **without locks** and normalises the batch off the event loop (`asyncio.to_thread`).
2. Locks only inside the write step: `SELECT … FOR UPDATE SKIP LOCKED` over the rows whose `source_changed_at` still equals the value read, then writes just those rows. A row an upload or the poller holds is skipped and stays dirty; nothing waits on it, and each transaction sets `lock_timeout = 3 s`. A timeout reports `contended`. A deadlock is deliberately not swallowed.
3. Order inside a tick: missing identity rows are created; **dirty** rows are written with capture enabled; **stale-revision** rows are written under the suppression setting (belt and braces beside `values_rev`).
4. Normalisers are called with `strict=True`, so an internal exception reaches the per-row failure handling (row stays dirty, hour-long back-off, one logged line with `%r` of the asset id) instead of being stamped clean with generic output. If `packs_healthy()` is False nothing is stamped at all.
5. `lifecycle_slug` is never written by the loop (the lifecycle phase owns it), and a known OS block is never replaced by an empty one: a source going missing is a gap.
6. Software keys are backfilled in 2,000-row keyset batches. The NULL-keyed scan uses the partial index `idx_ssi_unkeyed`; the `norm_rev < RULEPACK_REV` scan runs once per process start (a revision bump ships with a restart). The backfill never writes `version` or `updated_at` (the ingest prune depends on it). A row with no `source` on a non-Windows server is left unkeyed for its next upload.

Agent ingest also normalises its server inline, so a server's dashboard updates within seconds.

**Source precedence for network OS version**, most exact first:

1. a fresh template value (≤ 24 h): `fgt_fw_version`, `pan_sw_version`, `f5_version`;
2. `devices.os_version` from the profile extract (`os_source` `profile`);
3. `devices.firmware_version` (chassis `entPhysicalFirmwareRev`, `entity`);
4. a version found in `sys_descr` (`sysdescr`);
5. a **stale** template value (older than 24 h, `template_stale`): the lowest rank, used only when nothing else supplies a version, so a device that was unreachable for a day keeps its last known firmware instead of losing it.

The chosen source is recorded in `os_source` and shown in the UI ("from FortiGate template, 4 min ago"). A device unpolled for 24 h whose stored version came from a template keeps that block; a recomputed row without an OS version never erases a known one.

### 3.3 Raw-evidence columns added in P0

```sql
ALTER TABLE devices ADD COLUMN IF NOT EXISTS sys_descr text;                    -- poller persists (Go change)
ALTER TABLE devices ADD COLUMN IF NOT EXISTS identity_updated_at timestamptz;   -- stamped on each successful system-info poll
ALTER TABLE servers ADD COLUMN IF NOT EXISTS inventory_updated_at timestamptz;  -- last accepted software/OS section
ALTER TABLE servers ADD COLUMN IF NOT EXISTS inventory_baselined_at timestamptz;-- first full software snapshot accepted
ALTER TABLE servers ADD COLUMN IF NOT EXISTS hw_manufacturer varchar(128);      -- filled by agent from P3
ALTER TABLE servers ADD COLUMN IF NOT EXISTS hw_model varchar(255);
ALTER TABLE servers ADD COLUMN IF NOT EXISTS hw_serial varchar(128);
ALTER TABLE servers ADD COLUMN IF NOT EXISTS bios_version varchar(128);
ALTER TABLE servers ADD COLUMN IF NOT EXISTS bios_date date;
ALTER TABLE servers ADD COLUMN IF NOT EXISTS virtualization varchar(32);        -- 'physical','vmware','hyper-v','kvm','xen','nutanix','cloud:*'
ALTER TABLE servers ADD COLUMN IF NOT EXISTS software_truncated boolean NOT NULL DEFAULT false; -- agent list hit its 1,000-entry cap on the last upload

ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS architecture varchar(16);   -- x64 / x86 / user
ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS source varchar(32);         -- hklm / hklm_wow6432 / hkcu / dpkg / rpm
ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS install_location text;
ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS first_seen_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS name_key varchar(160);      -- normalised product key
ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS publisher_key varchar(96);
ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS category varchar(32);       -- browser, security, runtime, os-component …
ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS norm_rev integer;
CREATE INDEX IF NOT EXISTS idx_ssi_name_key ON server_software_inventory (name_key, version);
CREATE INDEX IF NOT EXISTS idx_ssi_publisher ON server_software_inventory (publisher_key);

-- Existing servers that already hold software rows are already baselined: their next upload diffs normally.
UPDATE servers s SET inventory_baselined_at = now()
 WHERE inventory_baselined_at IS NULL
   AND EXISTS (SELECT 1 FROM server_software_inventory i WHERE i.server_id = s.id);
```

**Ingest changes** in `host_metric_service._upsert_inventory`:

- Keep `architecture`, `source` and `install_location`. Every agent string that reaches PostgreSQL in this path (software fields, the OS section, the CPU model) goes through one sanitiser: NUL bytes dropped, lone surrogates replaced.
- Compute `name_key`, `publisher_key`, `category` and `norm_rev` inline with `normalize_software(strict=True)`. An entry whose key computation fails is stored unkeyed with `norm_rev` NULL and one log line per upload; if `packs_healthy()` is False no row is stamped.
- **Dedupe same-name entries within one snapshot**, keeping the highest version by comparator (`semverish`); where versioning refuses to order the group, a natural-sort key and then the raw text decide, so the survivor never depends on the order the agent listed the entries. Otherwise the `(server_id, package_name)` key makes the version flap and writes false "changed" events.
- `version = COALESCE(EXCLUDED.version, existing)`: an empty `DisplayVersion` never erases a known version.
- **Server-side cap:** `SOFTWARE_LIST_SERVER_CAP = 5000` entries; the excess is dropped and the snapshot counts as truncated.
- **Truncation guard:** a list of ≥ 1,000 entries (`SOFTWARE_LIST_AGENT_CAP`, the agent's cap) is upserted, `servers.software_truncated` is stamped **before** the writes (so the capture triggers tag the events) and the final value after. The blanket prune is skipped; only rows that sort at or before the last listed name (compared in Go-compatible lower-cased code-point order, only when the list is verifiably in the agent's order) and are absent are pruned, and a `removed` event is written for them.
- **Ordering guard:** `servers.inventory_collected_at` holds the agent-side timestamp of the newest accepted snapshot. A snapshot older than it is ignored for the software, OS and identity sections (other sections still apply), so a deferred or replayed older batch cannot rewind state or write events in both directions. A stored value more than a day in the future, or older than 24 hours (agent clocks are not monotonic and the agent spools a batch for at most 24 hours), never blocks a snapshot. The timestamp is the batch's `collected_at` for `batch.inventory` and the sample's own timestamp for `inventory` samples, neither clock-skew-corrected.
- **Event budget:** at `SOFTWARE_EVENT_BUDGET_24H = 2000` software events for a server in the last 24 hours, the software section is captured silently and at most one marker event per server per day is written.
- **Failure isolation:** every piece of inventory work inside ingest (planning, stamps, inline identity, the guards above) runs in a savepoint or a guarded call. An exception is logged and leaves the row unkeyed or dirty for `inventory_normalize_loop` to repair. It must never fail or roll back an agent upload, and the existing error behaviour of the other sections is unchanged.
- Stamp `inventory_updated_at` after a non-empty software list or an OS section; stamp `inventory_baselined_at` after the first accepted non-empty software snapshot, **after** the row writes, so the first snapshot of a server is silent and every later one diffs.
- Inline identity: after the OS update, `normalize_server` runs on the server's raw columns and the result is written to `inventory_identity` with `norm_rev` and `values_rev` set, unless the stored row already equals it.
- Direct callers of `_upsert_inventory` that pass no `collected_at` keep the old behaviour, including its lock order (software rows, then the servers row, then the identity row).

## 4. Normalisation engine (`services/inventory_normalize.py` + `services/versioning.py`)

Pure functions with no I/O, driven by versioned JSON rule packs under `server/app/data/inventory/`. Each is golden-fixture tested.

### 4.1 OS (servers)

| Raw (`os_name \| os_version \| kernel_or_build`) | family | product | release | edition | version / build |
|---|---|---|---|---|---|
| `Microsoft Windows Server 2022 Standard Evaluation \| 21H2 \| 10.0.20348.587 Build 20348.587` | windows | Windows Server | 2022 | Standard Evaluation | 10.0.20348.587 / 20348.587 |
| `Microsoft Windows Server 2019 Datacenter \| 1809 \| 10.0.17763.6532` | windows | Windows Server | 2019 | Datacenter | 10.0.17763.6532 |
| `Microsoft Windows 11 Pro \| 23H2 \| 10.0.22631.4317` | windows | Windows 11 | 23H2 | Pro | 10.0.22631.4317 |
| `Ubuntu 22.04.4 LTS` (P3 SSH) | linux | Ubuntu | 22.04 | LTS | 22.04.4 |
| `Red Hat Enterprise Linux 9.4 (Plow)` (P3) | linux | RHEL | 9 | — | 9.4 |

The Windows release comes **from the build number**, not the marketing string. The build-to-release table lives in the pack: 17763 → 2019, 20348 → 2022, 26100 → 2025 / 24H2, 19045 → 22H2, 22631 → 23H2, and so on.

### 4.2 Network OS

| Vendor / raw | family | release | version | build |
|---|---|---|---|---|
| Fortinet `v7.2.5,build1517,230606 (GA.F)` | fortios | 7.2 | 7.2.5 | 1517 |
| Palo Alto `11.1.10-h25` | panos | 11.1 | 11.1.10-h25 | — |
| Cisco IOS-XE `17.09.04a` | ios-xe | 17.9 | 17.9.4a | — |
| Cisco IOS `15.2(7)E8` | ios | 15.2 | 15.2(7)E8 | — |
| Juniper `21.4R3-S5.4` | junos | 21.4 | 21.4R3-S5.4 | — |
| F5 `17.1.1.3` | tmos | 17.1 | 17.1.1.3 | — |
| Aruba AOS-CX `10.13.1000` | aos-cx | 10.13 | 10.13.1000 | — |

`os_family` is derived from **hard evidence first**: the sysObjectID prefix, then the sysDescr rules, then the operator-chosen profile name (with a version-format check that separates classic IOS from IOS-XE), then the model, the vendor's only OS, and finally the version's shape (`os_patterns.json`). A profile name can seed the family but never outranks a sysObjectID or sysDescr that names a different one, and prefixes that are positively *not* a network OS of this pack (ClearPass, ScreenOS, F5OS) stop the search. Cisco IOS-XE is recognised by its platform markers (`CAT3K_CAA`, `LINUX_IOSD`, `_IOSXE`, `CAT9K_IOSXE`) and, failing that, by version shape. ASA keeps its interim build (`9.16(4)57`). No explicit `device_profiles.os_family` column exists in P0.

Normaliser API: every `normalize_*` stays total by default; with `strict=True` an internal exception propagates so loops and ingest can apply their per-row failure handling. `normalize_device` also takes `stale_template_values`. The pinned test fingerprint per `RULEPACK_REV` covers the packs and a digest of the normaliser's output over the golden corpus, so an engine change without a revision bump fails CI.

### 4.3 Vendor canonicalisation (`vendor_aliases.json`)

| Raw values seen | Canonical |
|---|---|
| "Palo Alto Networks", "Palo Alto", "PAN" | Palo Alto Networks / `paloalto` |
| "Aruba (HPE)", "Aruba", "Hewlett Packard Enterprise Aruba" | HPE Aruba Networking / `aruba` |
| "Cisco Systems", "Cisco", "ciscoSystems" | Cisco / `cisco` |
| "Microsoft Corporation", "Microsoft Corp." | Microsoft / `microsoft` |
| "Google LLC", "Google Inc." | Google / `google` |

### 4.4 Software names

The rule pipeline runs in order, stops at the first product match, and records which rule fired:

1. **Exact product rules** in the pack (about 300 common enterprise titles), matching `name regex + optional publisher regex`. Each yields product, publisher, category, edition and cycle extraction. Examples:
   - `^Microsoft Visual C\+\+ (\d{4}(?:-\d{4})?) Redistributable \((x64|x86)\)` → *Microsoft Visual C++ Redistributable*, edition `$1`, category `runtime`.
   - `^Java (\d+) Update (\d+)` → *Java SE Runtime*, cycle `$1`, category `runtime`.
   - `^Python (\d+\.\d+)\.\d+` → *Python*, cycle `$1`, category `development`.
2. **Noise rules:**
   - `KB\d{6,7}`, "Security Update for", "Hotfix for" → category `update` (hidden by default; feeds the P3 patch view);
   - drivers → `driver`;
   - "… Language Pack", MUI and similar → `os-component`.
3. **Generic fallback:**
   - strip a trailing version (`\s+v?\d+(\.\d+){1,3}\b.*$`);
   - strip architecture markers (`(x64)`, `(64-bit)`, `x86`), trailing edition words and copyright years;
   - collapse whitespace and slugify.
   - Result: `name_key = slug(publisher_key + '-' + residual)`, `category = 'other'`, and the row is flagged *unmatched* (P2's "Match not found" queue).

**Category set:** `browser, security, remote-access, database, web-server, runtime, development, productivity, monitoring, backup, virtualization, networking, driver, os-component, update, other`.

`security` covers AV, EDR, firewall agents and the ZenPlus agent. It powers the P5 *required agent* policy and the Overview's *security agent coverage* figure.

### 4.5 Version comparators (`services/versioning.py`)

These are shared with baselines (P5 replaces `baseline_service.compare_versions`) and with the P6 CVE matcher (Compliance task 1.3, done once here).

| Scheme | Used for | Notes |
|---|---|---|
| `win_build` | Windows OS / MSI four-part | Numeric per part |
| `semverish` | Most apps | Dotted numeric with alpha suffix ordering (`4a > 4`), ignores build metadata |
| `fortios` | FortiOS | `major.minor.patch` then build |
| `panos` | PAN-OS | `x.y.z` then `-hN` hotfix ordering |
| `cisco_ios` / `cisco_iosxe` | Cisco | Train-aware; **refuses cross-train comparison** (returns `None`, never a guess) |
| `junos` | Juniper | `R`, `S` and `X` components |
| `dpkg` / `rpm` | Linux (P3) | Epoch:upstream-revision with `~`; EVR |

A comparator returns `-1 | 0 | 1 | None`. `None` means *not comparable*, and the UI shows "—", never a wrong ordering.

## 5. Change capture

### 5.1 `inventory_change_events` (P0)

```sql
CREATE TABLE IF NOT EXISTS inventory_change_events (
    id           bigserial PRIMARY KEY,
    observed_at  timestamptz NOT NULL DEFAULT now(),
    asset_kind   varchar(16) NOT NULL,
    asset_id     uuid        NOT NULL,          -- no FK: history survives asset deletion
    asset_name   varchar(255),                  -- snapshot of the name at event time
    category     varchar(16) NOT NULL CHECK (category IN ('software','os','firmware','hardware')),
    action       varchar(16) NOT NULL CHECK (action IN ('added','removed','changed')),
    subject      varchar(255) NOT NULL,         -- package name, or 'os_version' / 'hw_serial' / 'hw_model'
    subject_key  varchar(160),                  -- name_key for software (joins the catalog)
    old_value    text,
    new_value    text,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb   -- vendor, architecture, source, os_source …
);
CREATE INDEX IF NOT EXISTS idx_inv_events_time  ON inventory_change_events (observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_events_asset ON inventory_change_events (asset_kind, asset_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_events_cat   ON inventory_change_events (category, action, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_events_key   ON inventory_change_events (subject_key, observed_at DESC);
```

### 5.2 Triggers

| Source | Trigger | Emits |
|---|---|---|
| `server_software_inventory` | AFTER INSERT, only if the server's `inventory_baselined_at IS NOT NULL` | `software / added` (new_value = version) |
| same | AFTER UPDATE OF `version`, `WHEN (OLD.version IS NOT NULL AND NEW.version IS NOT NULL AND OLD.version <> NEW.version)` | `software / changed` (old → new). A version that goes missing and returns is a gap, not two changes. Upgrade vs downgrade is labelled at read time by comparator |
| same | AFTER DELETE, only if the parent server **still exists** and is baselined | `software / removed`. Cascade deletes of a removed server are skipped automatically: the parent row is already gone when the cascade runs |
| `inventory_identity` | AFTER UPDATE when `os_version` changes with OLD and NEW both non-NULL **and `values_rev` is unchanged** | `firmware / changed` (devices) or `os / changed` (servers) |
| same | AFTER UPDATE when `hw_serial` or `hw_model` changes with OLD and NEW both non-NULL | `hardware / changed` (RMA / chassis swap signal) |

NULL → value is *discovery*, not change, and is never emitted. Value → NULL is ignored because poller writes are sticky. While `servers.software_truncated` is true, software events carry `detail.snapshot_truncated = true` so the read side can discount a list that was cut at the agent's cap.

### 5.3 Suppression (no fabricated history)

- **First snapshot of a new server:** no events (the baselined gate).
- **Rule-pack re-normalisation**: `values_rev` (§3.2) makes it silent for every writer. The loop additionally runs its stale-revision pass under `set_config('zenplus.inventory_suppress','on', true)` (`SET` cannot take bind parameters), and every capture trigger checks `current_setting('zenplus.inventory_suppress', true)`.
- **P3 key-widening migration** and bulk admin operations also run with the setting on.
- **Truncated snapshots** (a list that reached the agent's 1,000-entry cap) never trigger the blanket prune. Only rows that sort at or before the last listed name, and are absent, are pruned: the agent sorts by lower-cased name and cuts the tail, so those rows are provably uninstalled and the rest are unknown (§3.3).
- **Stale or reordered snapshots** are ignored (§3.3), so a deferred older snapshot cannot rewind state or write events in both directions.
- **Event budget**: a server with 2,000 or more software events in the last 24 hours has the software section of further uploads captured silently, with one marker event per day (`(change budget exceeded)`), so one misbehaving agent cannot write unbounded history.
- **Never backfill events from history counts.** This is the project rule (AGENTS.md, UDT section) and applies equally here.

### 5.4 Retention

The `inventory` setting `change_retention_days` defaults to **400**, which covers PCI's 12-month evidence window plus margin. `inventory_snapshot_loop` purges daily in batches of 10k.

## 6. Daily snapshots (P0)

```sql
CREATE TABLE IF NOT EXISTS inventory_daily_snapshots (
    day        date         NOT NULL,
    metric     varchar(64)  NOT NULL,   -- 'assets', 'os_release', 'software_products', 'coverage', 'health', 'changes'
    dimension  varchar(255) NOT NULL DEFAULT '',   -- 'server' | 'windows/Windows Server/2022' | 'missing_serial' …
    value      bigint       NOT NULL,
    PRIMARY KEY (day, metric, dimension)
);
```

`inventory_snapshot_loop` wakes every 5 minutes and runs once per day at or after the configured hour:15 in the **appliance timezone** (the `company` setting's timezone, default UTC: the zone report schedules and alert quiet hours already use) under advisory lock 1515074398. A missed day is caught up on the next wake. `snapshot_day` deletes the day's rows of its six metric families and rewrites them in one transaction, so a re-run never leaves rows that no longer apply, and the previous day's `changes` family is recomputed once the day is over. It writes a few hundred rows a day and keeps 3 years. `trends_enabled` is a display switch only: recording continues, because history cannot be backfilled.

It feeds the Overview's 30/90-day trend lines and the reports' "as of" snapshots. It is fleet-wide (unscoped), so **scoped users don't receive trend series**: the widget shows "Trends are available to unrestricted users".

## 7. Catalog (P2) and asset metadata (P2)

```sql
CREATE TABLE IF NOT EXISTS inventory_software_products (
    id            bigserial PRIMARY KEY,
    product_key   varchar(160) NOT NULL UNIQUE,     -- stable URL key, = name_key
    name          varchar(255) NOT NULL,
    publisher     varchar(255),
    publisher_key varchar(96),
    category      varchar(32)  NOT NULL DEFAULT 'other',
    hidden        boolean      NOT NULL DEFAULT false,   -- noise hidden from catalog counts by default
    business_purpose text,                              -- CIS 2.1 field
    cpe_vendor    varchar(96), cpe_product varchar(160),  -- P6 join key
    lifecycle_slug varchar(64),                         -- P4 join key
    version_scheme varchar(24) NOT NULL DEFAULT 'semverish',
    origin        varchar(16)  NOT NULL DEFAULT 'learned' CHECK (origin IN ('builtin','learned','manual')),
    norm_status   varchar(24)  NOT NULL DEFAULT 'normalized'
                  CHECK (norm_status IN ('normalized','publisher_only','unmatched','manual')),
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS inventory_software_rules (
    id bigserial PRIMARY KEY, priority integer NOT NULL DEFAULT 100, enabled boolean NOT NULL DEFAULT true,
    name_pattern text NOT NULL, publisher_pattern text, match_type varchar(8) NOT NULL DEFAULT 'regex'
        CHECK (match_type IN ('exact','contains','regex')),
    product_key varchar(160) NOT NULL, edition_template text, cycle_template text, category varchar(32),
    notes text, created_by uuid, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS inventory_publishers (
    alias varchar(255) PRIMARY KEY, publisher_key varchar(96) NOT NULL, label varchar(255) NOT NULL);
CREATE TABLE IF NOT EXISTS inventory_asset_meta (
    asset_kind varchar(16) NOT NULL, asset_id uuid NOT NULL,
    owner varchar(255), department varchar(255), business_service varchar(255),
    criticality smallint CHECK (criticality BETWEEN 1 AND 5),
    lifecycle_state varchar(16) CHECK (lifecycle_state IN ('planned','in_service','maintenance','retiring','retired')),
    purchase_date date, warranty_end date, support_contract varchar(255),
    last_reviewed_at timestamptz, last_reviewed_by uuid,    -- CIS 1.1 "review twice a year"
    excluded boolean NOT NULL DEFAULT false, notes text,
    updated_at timestamptz NOT NULL DEFAULT now(), updated_by uuid,
    PRIMARY KEY (asset_kind, asset_id));
CREATE TABLE IF NOT EXISTS inventory_saved_views (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), page varchar(32) NOT NULL, name varchar(120) NOT NULL,
    filters jsonb NOT NULL, owner_id uuid NOT NULL, shared boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
```

- **Rule precedence:** local enabled rules (by `priority`), then the built-in pack, then the generic fallback.
- **Rule save flow:** saving a rule increments `current_rev`, and the loop re-keys affected rows with events suppressed.
- **Rule preview:** `POST /inventory/catalog/rules/preview` returns the raw names a draft rule would capture, with host counts, *before* saving.
- **"Report inaccuracy":** on a product page it opens the rule editor pre-filled from the raw evidence.

## 8. API contract (`server/app/api/v1/inventory.py`)

Mounted with `policy("inventory")`: GET needs `inventory.view`, writes need `inventory.manage`. Every handler resolves `scope = await scoping.visible_tags(db, user)` and applies `jsonb_tags_visible('s.tags')` / `('d.tags')` inside the asset union.

List endpoints share these conventions:

- **Query params:** `q, page, page_size (≤ 500), sort, order, kind, os_family, os_release, vendor, model, site, location, tag, source, freshness, category`.
- **Response:** `{items, total, page, page_size, generated_at}`.

| Method & path | Permission | Returns |
|---|---|---|
| `GET /inventory/summary` | view | KPI block (shape below), cached 60 s per scope key |
| `GET /inventory/assets` | view | Asset union rows (§3.1) |
| `GET /inventory/assets/facets` | view | Counts per kind, os_family, vendor, model, site/location, tag, source, freshness |
| `GET /inventory/assets/{kind}/{id}` | view | Flyout payload: identity with sources and ages, top software (servers), modules (devices), recent changes, meta. 404 when out of scope |
| `PATCH /inventory/assets/{kind}/{id}/meta` (P2) | manage | Upsert `inventory_asset_meta`; audit-logged |
| `GET /inventory/os` | view | Tree `[{family, product, releases:[{release, count, versions:[{version, count}]}]}]`, filter `kind` |
| `GET /inventory/software` | view | Catalog rows `{product_key, name, publisher, category, hosts, installs, versions, latest_version, first_seen, last_change, norm_status}`; `hidden=include` shows noise |
| `GET /inventory/software/publishers` | view | Publisher facet with product and host counts |
| `GET /inventory/software/{product_key}` | view | Product header + version distribution `[{version, hosts, cmp_rank}]` (sorted by comparator) + raw-name evidence |
| `GET /inventory/software/{product_key}/installations` | view | `{server, version, architecture, source, install_date, first_seen_at, updated_at}` |
| `GET /inventory/hardware` | view | `{network:{vendors:[…], models:[{vendor, model, count, os_versions, serial_coverage}]}, modules:[{class, model, count}], servers:{manufacturers, models, cpu_models, memory_buckets, disk_buckets, virtualization}}` |
| `GET /inventory/changes` | view | Events (§5) with filters `category, action, kind, asset_id, subject_key, since, until`; plus `stats` (counts per category/action over the window) |
| `GET /inventory/trends` | view | Snapshot series for a metric/dimension; **unscoped users only** (scoped: 200 with `restricted: true`) |
| `GET /inventory/export` | **export** | Streams CSV for `dataset=assets\|software\|installations\|os\|hardware\|changes` with the same filters; formula-injection-safe (cells starting `= + - @` tab/CR are prefixed `'`, porting `exportCsv.ts esc()`); `write_audit_log(action="inventory.export", metadata={dataset, filters, rows})` |
| `GET/POST/PUT/DELETE /inventory/catalog/rules` (P2) | manage | Rule CRUD; `POST …/preview` |
| `PATCH /inventory/catalog/products/{product_key}` (P2) | manage | Category, hidden, business_purpose, name, lifecycle_slug, cpe |
| `GET/POST/DELETE /inventory/views` (P2) | view (own) / manage (shared) | Saved views |
| `GET/PUT /inventory/settings` | view / manage | Settings blob (§9) |

`GET /inventory/summary` shape:

```json
{
  "generated_at": "2026-10-07T14:05:12Z",
  "assets":   {"total": 412, "server": 128, "device": 284},
  "coverage": {"fresh": 389, "aging": 15, "stale": 6, "never": 2, "fresh_pct": 94.4},
  "os":       {"families": 9, "releases": 31, "top": [{"family":"windows","product":"Windows Server","release":"2022","count":61}]},
  "software": {"products": 1840, "installs": 39120, "publishers": 410,
               "top": [{"product_key":"google-chrome","name":"Google Chrome","hosts":120,"versions":14}],
               "sprawl": [{"product_key":"google-chrome","versions":14,"hosts":120}],
               "security_agent_coverage": {"covered": 121, "total": 128}},
  "hardware": {"vendors": 12, "models": 57, "top_models": [{"vendor":"Fortinet","model":"FortiGate-200F","count":14}]},
  "changes_7d": {"software": {"added": 58, "removed": 21, "changed": 340}, "os": 4, "firmware": 7, "hardware": 1},
  "health":   {"missing_os_version": 9, "missing_model": 22, "missing_serial": 31, "unknown_vendor": 8,
               "stale": 6, "never": 2, "software_truncated": 1, "unmatched_software": 37},
  "lifecycle": null
}
```

Each `health` key maps one-to-one to an `/inventory/assets?health=<key>` filter, so every count is a worklist.

## 9. Settings (`system_settings['inventory']`)

```json
{
  "fresh_hours":  {"server": 24, "device": 2},
  "stale_days":   7,
  "change_retention_days": 400,
  "snapshot_hour": 0,
  "hide_categories_by_default": ["update", "driver", "os-component"],
  "trends_enabled": true
}
```

## 10. Later-phase engines (specs refined when scheduled)

### 10.1 Lifecycle (P4)

- **`eol_definitions`** has one row per `(slug, cycle)`:
  - dates: `release_date, eoas_from, eol_from, eoes_from`;
  - flags: `is_lts`;
  - `latest_version`, `latest_date`;
  - `labels jsonb` (the product's own milestone names);
  - `source` (`endoflife.date` / `vendor` / `manual`), `as_of`.
- **Initial data:** loaded from the endoflife.date `/products/full` snapshot (about 3 MB, MIT) **bundled in the signed release**. It is refreshed through the zentryc feed channel (Compliance doc 06) or a `.zvb` sideload. The appliance never calls endoflife.date directly.
- **`inventory_lifecycle_overrides`:**
  - covers `(slug | vendor_key+model, cycle)` with manual dates, reason, owner, `expires_at`;
  - fills gaps for Junos, NX-OS, classic IOS, Arista, Aruba, SonicOS and Check Point;
  - holds hardware end-of-sale and end-of-support dates (CSV import, plus the optional Cisco EoX API when entitled credentials exist).
- **Stage** is computed per asset at query time: `supported | security_only | eol | extended | unknown`, plus `days_to_eol`. `latest_version` gives the **N / N-1 / older** currency split.
- **Alerts:** new metrics `inventory_eol_reached` and `inventory_eol_within_90d`, added through a constraint-supersede migration.

### 10.2 Policy engine (P5)

This generalises `software_baselines` (servers) and borrows NCM compliance's exceptions and snapshots (`migrate-131`).

**Rule types:**

| Rule | Example |
|---|---|
| `software_prohibited` | No TeamViewer on servers tagged `pci` |
| `software_required` | EDR category present on every Windows server |
| `software_min_version` | Chrome ≥ 150 |
| `software_currency` | Within N-1 of the observed or feed latest |
| `os_min_build` | Windows Server 2022 ≥ 20348.2700 |
| `firmware_min_version` | FortiOS 7.2 ≥ 7.2.10 |
| `firmware_allowed_trains` | — |
| `eol_without_exception` | — |
| `inventory_stale` | > 7 days |
| `asset_owner_required` | CIS 1.1 |

**Scope:** kind, os_family/release, vendor/model, site/location, tags.

**Results:** one row per `(asset, rule)`, with status `pass | fail | unknown | excepted`, evidence JSONB, and `first_failed_at` latched.

**Exceptions:** carry reason, owner and expiry, and **require `compliance.approve`** to activate. Pending exceptions show as *requested*.

**History and alerts:** daily snapshots. Alerts on transitions only, through the existing alert engine (dedupe `policy:{rule_id}:{asset}`).

**Framework mapping:** `compliance_framework_controls` maps rule → control:

- CIS v8.1: 1.1, 1.2, 2.1, 2.2, 2.3, 2.5
- NIST CSF 2.0: ID.AM-01, -02, -08
- ISO 27001: A.5.9, A.8.19
- PCI DSS 4.0: 12.5.1, 12.3.4, 6.3.3

The **audit pack** export bundles snapshot CSVs, policy results, the exception register and the change log for a date range.

**Migration of existing baselines:** baseline rules are copied into policies, the old tables stay read-only for rollback, and `/server-baselines` redirects.

### 10.3 Vulnerability correlation (P6)

As specified in `Documentation/Compliance/03` and `04`, with these inputs re-pointed:

| Compliance-set input | Re-pointed to |
|---|---|
| `compliance_product_aliases` | `inventory_software_products` |
| `compliance_asset_meta` | `inventory_asset_meta` |
| `compliance_oid_dictionary` | `inventory_identity.os_family` |
| Version comparators | `services/versioning.py` |

## 11. RBAC, scoping and audit

| Concern | Rule |
|---|---|
| Route guards | Router `policy("inventory")` plus explicit `require_permission("inventory.export")` on export, and `inventory.manage` on every mutation (precise permissions, no operator shortcut: RP-05). `test_route_policy_contract.py` covers the router. New `test_inventory_route_guards.py` asserts that every non-GET route declares more than `.view` (pattern: `test_netflow_route_guards.py`) |
| Cross-module visibility | `inventory.view` alone shows asset identity and software inventory **within tag scope**. This intentionally widens server software visibility, which today needs `apm.view`. Links to `/devices/:id` and `/servers/:id` render as links only when `useCan('devices.view')` / `useCan('apm.view')`; otherwise as plain text |
| Scope | Tag scope on every query. Detail endpoints return 404 out of scope. Trends and reports refuse scoped users, consistent with `reports.py` `/data/inventory` |
| Audit | `write_audit_log` on export, catalog rule and product changes, asset-meta changes, settings changes, and (P5) policy and exception changes |
| Built-ins | `BUILTIN_ROLE_DEFINITION_VERSION` 3 → 4. Release notes announce the new permissions and that **custom roles must be granted `inventory.view` explicitly** (doc §4 of the roles assessment) |

## 12. Reports integration (P2)

New sections in `services/report_sections.py`, registered in `SECTION_REGISTRY`:

- `inventory_overview` (KPIs + health)
- `inventory_os_distribution`
- `inventory_software_summary` (top products, sprawl)
- `inventory_hardware_models`
- `inventory_changes` (window)
- `inventory_lifecycle` (P4)

**Preset changes:**

- The `inventory` preset is extended so it covers servers and software, not only network devices.
- A new preset, `inventory_audit`, adds per-asset tables for auditors.
- `report_schedules._REPORT_TYPES` gains the new keys.
- Scheduling and delivery work through `report_scheduler.generate_and_deliver` with no new code.

## 13. Performance budgets

**Sizing target:** 5,000 servers × 400 packages (2 M software rows), 10,000 devices and 1 M change events.

| Endpoint | Budget (p95) | Mechanism |
|---|---|---|
| `/inventory/summary` | < 300 ms cold, < 20 ms cached | Aggregates over indexed keys; 60 s cache per scope key |
| List endpoints | < 400 ms | Keyset-friendly ordering, `page_size ≤ 500`, facet queries run in parallel |
| `/inventory/software` | < 400 ms | `GROUP BY name_key` on `idx_ssi_name_key` |
| Normalise loop | No agent-ingest latency impact | Batches of 500; pure-Python normaliser via `asyncio.to_thread` |

If the software catalog exceeds budget on the perf fixture, add an `inventory_software_rollup` table refreshed by the normalise loop. This is the documented escape hatch and is not built unless the test fails.

## 14. Known limitations and notes for later phases (from the Phase 0 review)

A six-reviewer adversarial review of Phase 0 produced 44 findings; the confirmed ones that threaten history, uploads or CI are fixed. These are consciously **not** fixed in Phase 0 and are inputs to later phases.

| Topic | Note |
|---|---|
| Scoped event queries (P1) | Neither `inventory_identity` nor `inventory_change_events` stores tags, and events deliberately have no foreign key. The natural scoped feed (`EXISTS` on the asset row, `ORDER BY observed_at DESC LIMIT 50`) walks the whole time index (about 4 s at 3 M events for a narrow scope). Scoped event queries must be **asset-driven**: build the visible-asset set first (tags live in the asset union) and join `inventory_change_events` through `idx_inv_events_asset` with a lateral join, with a perf test on a 3 M-event fixture and a scope that matches a quiet team. Decide explicitly what scoped users see for deleted assets. |
| Product upgrades shown as install plus removal | A product whose display name embeds its version (VC++ redistributables, Java) or whose casing flips is captured as one `added` plus one `removed`. The read side pairs them: same asset, same `subject_key`, same `observed_at`, one added and one removed becomes an upgrade or downgrade by comparator. State this rule in the Changes page spec. |
| Unattributed events | Events do not record which agent reported them, and the batch ledger that could attribute them expires before the events do (400 days). A fleet enrollment token can bind a second agent to an existing server by hostname (pre-existing, deliberate for reinstalls). Follow-up: set a transaction-local `zenplus.inventory_actor` setting in ingest and have the capture triggers copy it into `detail`. |
| Volatile `sysDescr` | A device whose `sysDescr` changes every poll re-dirties its identity row every poll. Normalisation output is unchanged (no false events); the cost is write amplification (about one identity upsert per poll, and a normalise per tick). Mitigation if it ever matters: debounce `sys_descr`-only changes in a separate trigger and re-normalise rows older than 24 hours as the trailing edge. |
| First-poll transient | Fixed for the usual case by writing entities before system info in the poller. Two rarer variants remain (a legacy import that inserts the profile model before the first poll; an entity walk that comes back empty on two consecutive polls flips `devices.model`). A settle rule in the dirty selection would close both. |
| Template aging | A template-derived version is kept while the device is unpolled. A device that is polled but whose template group is no longer polled can be replaced by a sticky older profile version, which is a real change under an unchanged `values_rev` and writes an event. |
| Lock order for callers without `collected_at` | Direct callers keep the payload-dependent lock order; a software-only snapshot can deadlock against `DELETE FROM servers` (the transaction is retried). The route always passes `collected_at`. |
| `removed` events before key backfill | A `removed` event written for a row that is still unkeyed after the migration has a NULL `subject_key` for ever. Accepted; the read side falls back to the subject name. |
| First upload after upgrade | A server whose snapshot lists the same product twice with different versions (HKLM and WOW6432Node) stored the last listed version before; the new rule stores the highest, so each affected package records one `changed` event on the first upload after the upgrade. |
| Rule-pack fingerprint | The pinned fingerprint per revision covers the packs and the golden-corpus output, but nothing ties 'has shipped' to a released revision, so a shipped entry can still be edited in place. A CI step comparing against `origin/main` would close it. |
| Poison pill outside this module | A lone surrogate in an agent batch makes `_host_results_batch_digest` (agents.py) raise before ingest runs, so the agent receives a 500 and retries for ever. Pre-existing. |
| Poller database tests in CI | The Go PostgreSQL tests (42703 fallback, sticky semantics, migrated schema) run only where a PostgreSQL is available (`SYSINFO_TEST_DSN`, or `SYSINFO_TEST_PG=1` for the self-provisioning cluster); the required poller check does not run them. |
| Poller start against an older schema | `install.sh` treats a failed migration runner as a warning, and the poller unit has no schema check. The poller degrades to the pre-154 statement and logs a WARN every 10 minutes; the degraded state is not yet shown in its health JSON. |
