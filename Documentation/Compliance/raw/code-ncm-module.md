# NCM (Network Configuration Management) module — raw investigation report

Investigation date: 2026-08-18. Scope: what NCM does today, its exact schemas/endpoints/UI, what was
planned vs. built, and any existing EOL / firmware / CVE / vulnerability groundwork in the repo —
as input to the Compliance & Vulnerability Management module plan.

---

## 1. Executive summary

NCM in ZenPlus today is a **config backup + versioning + diff + change-alert** feature, nothing more.
It has:

- Versioned running-config storage per device with content-hash **and normalized-hash** dedup
  (`device_configs` table, every snapshot stored, `is_change` flag).
- Real SSH retrieval via **netmiko 4.7.0** (`server/requirements.txt:36`) with autodetect,
  10 supported platforms, custom pager-walking, and volatile-field masking; plus a manual-paste
  fallback endpoint.
- Reusable CLI **connection profiles** (`ncm_credentials`, AES-256-GCM encrypted via
  `server/app/core/crypto.py`) that are *already shared* with Discovery (SSH probes) and agentless
  server monitoring (`servers.ncm_credential_id`).
- Per-device enrollment + flexible schedules (`device_ncm`), driven by an **hourly systemd timer**
  hitting an **unauthenticated localhost endpoint** `POST /api/v1/ncm/run-scheduled`.
- Unified diff API + diff viewer UI, and config-change alerts inserted directly into `alerts`.

NCM has **no** policy/compliance rule engine, **no** firmware tracking, **no** EOL data, **no** CVE
anything, **no** config push/rollback, **no** audit trail, and **no** startup-config retrieval over
SSH (only via manual paste). All of that was explicitly deferred to "E4 Phase 2" in the epic doc and
roadmap ("config push + compliance + firmware EoL/CVE", Phase 4 of
`Documentation/enahce1/04-ROADMAP.md:57,70`).

Repo-wide grep for `eol`, `end_of_life`, `end-of-support`, `cve`, `vulnerab*` (excluding
node_modules/dist/venv/docs): **zero hits in code** — Python, Go, TSX, or SQL. The only
"firmware"-adjacent code is: (a) a dead `device.firmware_version` reference in the dashboard, and
(b) FortiGate firmware/AV/IPS **signature version strings collected via SNMP monitoring templates**
into `device_template_values` (migrate-062). The version-inventory raw material the Compliance
module needs (`devices.vendor/model/os_version/serial_number/sys_object_id`) exists and is populated
by the poller — see section 9.

---

## 2. History: planned vs. built

### 2.1 The plan — `Documentation/enahce1/05-EPIC-E4-ncm-config-management.md` (154 lines)

Phase 1 in-scope (lines 7–13): per-vendor SSH/CLI retrieval (Cisco IOS/NX-OS, PAN-OS, FortiOS),
encrypted CLI credentials, versioned content-addressed storage (running + startup), unified diff,
change/drift alerts via the alert pipeline, **immutable audit trail**, scheduled + on-demand pulls.

Explicitly out of scope, Phase 2 (lines 15–19):

> - Bulk config **push**/templates, golden-config remediation.
> - Compliance policy engine (CIS/PCI rule sets).
> - Firmware EoL / CVE enrichment via NIST NVD feed.
> - Real interactive console / TACACS+ change attribution.

Planned architecture (lines 33–62) put config pulling in the **Go poller** (`poller/internal/ncm/`
package, `configTicker` in `engine.go`, blobs in ClickHouse `config_blobs` ReplacingMergeTree,
`config_versions`/`config_audit_log`/`device_credentials`/`config_backup_jobs`/`vendor_drivers`
Postgres tables, poller→API ingest at `POST /api/v1/internal/ncm/versions`).

### 2.2 What was actually built (differs substantially)

Commit line (git log, oldest → newest):

```
e0cfe99 feat(ncm): Network Configuration Management — versioned config backup + diff (E4 slice 1)
ce957c6 feat(ncm): professional NCM — connection profiles, enrollment & SSH backup (E4 slice 2)
1c9c603 feat(ncm): professional NCM workspace — search, facets, device detail, scheduler (E4 slice 3)
2990826 feat(ncm): dedicated device page + complete config capture over paged CLI (E4 slice 4)
d3ab834 feat(ncm): config-change alerts when a device running-config changes between backups
187407a fix(ncm): normalize configs so re-encrypted secrets/keys don't look like changes
3908813 feat(ncm): retention, flexible schedules, per-version diff popup, backup-now progress
278a9b5 fix(ncm): schedule save 500 — parse HH:MM into a time object for asyncpg
dace356 feat(ncm): store every backup with change tags, bulk actions, UX
```

Built-vs-planned deltas (important for the Compliance plan, because the pattern that *shipped* is
the pattern to follow):

| Planned (epic doc) | Built |
|---|---|
| Go poller does SSH pulls (`poller/internal/ncm/`) | **Python/netmiko in the FastAPI process** via `asyncio.to_thread` — poller untouched, `grep firmware\|ncm poller/ → 0 hits` |
| ClickHouse `config_blobs` blob store | Config text stored inline in Postgres `device_configs.content TEXT` |
| `config_versions` + dedup (no row when unchanged) | `device_configs` — **every** snapshot stored, tagged `is_change` (migrate-029) |
| `config_audit_log` append-only audit trail | **Not built** — no audit table, no audit_log calls in `ncm.py` |
| `device_credentials` table name | `ncm_credentials` |
| `config_backup_jobs` cron table | schedule columns folded into `device_ncm` (interval/daily/weekly) |
| `vendor_drivers` seed table | hard-coded `PLATFORM_COMMANDS` dict in `ncm.py:33-45` |
| poller ingest endpoint `/internal/ncm/versions` | not built |
| `config_changed` alert **rule type** through notify channels | direct `INSERT INTO alerts` with `rule_id NULL` (Alert-Center-only, **no channel dispatch**) — `ncm.py:267-301` |
| running + startup over SSH | SSH fetches **running only**; startup possible only via manual paste (`config_type` pattern `^(running\|startup)$`, `ncm.py:632`) |
| NCM_ENABLED feature flag | none — always on |
| RBAC-gated config bodies | any authenticated user can read configs (see §7) |

---

## 3. Backend: `server/app/api/v1/ncm.py` (756 lines)

Two routers, both mounted in `server/app/main.py:58-59`:

```python
app.include_router(ncm.router, prefix="/api/v1")         # prefix="/ncm", tags=["NCM"]
app.include_router(ncm.device_router, prefix="/api/v1")  # prefix="/devices", tags=["NCM"]
```

There is **no separate service module** — all logic (SSH, hashing, diffing, alerting, retention,
scheduling) lives in `ncm.py` itself. `find server -iname '*ncm*'` returns only the router file.

### 3.1 Full endpoint inventory

| Method + path | Auth | Purpose | Location |
|---|---|---|---|
| `GET /api/v1/ncm/platforms` | `get_current_user` | list SUPPORTED_PLATFORMS for UI dropdowns | `ncm.py:318` |
| `GET /api/v1/ncm/credentials` | `get_current_user` | list connection profiles (no secrets; `has_password`/`has_enable` booleans + `used_by` count) | `ncm.py:323` |
| `POST /api/v1/ncm/credentials` | `require_operator_user` | create profile; secrets stored `crypto.encrypt()` | `ncm.py:340` |
| `PUT /api/v1/ncm/credentials/{cred_id}` | `require_operator_user` | update; secrets only overwritten when supplied | `ncm.py:358` |
| `DELETE /api/v1/ncm/credentials/{cred_id}` | `require_operator_user` | delete profile | `ncm.py:379` |
| `PUT /api/v1/devices/{device_id}/ncm` | `require_operator_user` | enroll/update device (credential, platform, schedule, retention, alert toggle) — upsert into `device_ncm` | `ncm.py:404` |
| `DELETE /api/v1/devices/{device_id}/ncm` | `require_operator_user` | unenroll | `ncm.py:435` |
| `POST /api/v1/ncm/bulk-assign` | `require_operator_user` | assign profile (+optional platform) to ≤500 devices, enable backup | `ncm.py:448` |
| `POST /api/v1/ncm/bulk-settings` | `require_operator_user` | apply schedule/retention/alert settings to ≤500 enrolled devices | `ncm.py:489` |
| `POST /api/v1/devices/{device_id}/config-fetch` | `require_operator_user` | on-demand SSH backup (netmiko) | `ncm.py:578` |
| `POST /api/v1/ncm/run-scheduled` | **NO AUTH** | back up every due enrolled device; called by localhost systemd timer | `ncm.py:593` |
| `POST /api/v1/devices/{device_id}/config-backup` | `require_operator_user` | manual paste capture (`content`, `config_type` running\|startup, `captured_by` manual\|api\|ssh) | `ncm.py:637` |
| `GET /api/v1/devices/{device_id}/configs?limit=` | `get_current_user` | list versions (metadata only, hash truncated to 12 chars) | `ncm.py:651` |
| `GET /api/v1/devices/{device_id}/configs/{version_id}` | `get_current_user` | full config **content** | `ncm.py:669` |
| `GET /api/v1/devices/{device_id}/configs-diff?a=&b=` | `get_current_user` | unified diff of two versions (normalized), `{diff, added, removed, identical}` | `ncm.py:686` |
| `GET /api/v1/ncm/overview` | `get_current_user` | fleet table: every device × enrollment × credential × version stats | `ncm.py:707` |

### 3.2 SSH retrieval engine (`ncm.py:62-187`)

- `PLATFORM_COMMANDS` (`ncm.py:33-45`): netmiko `device_type` → dump command. Notable:
  `cisco_ios/xe/nxos/asa` → `show running-config`; `juniper_junos` → `show configuration | display set`;
  `paloalto_panos` → `show config running`; `fortinet` → `show full-configuration`;
  `hp_comware`/`huawei` → `display current-configuration`; `linux` → `cat /etc/os-release`
  (linux is in the command map but **not** offered in `SUPPORTED_PLATFORMS`).
- `SUPPORTED_PLATFORMS` (`ncm.py:48-59`): autodetect, cisco_ios (labeled "Cisco IOS / IOS-XE"),
  cisco_nxos, cisco_asa, arista_eos, juniper_junos, paloalto_panos, fortinet, hp_comware, huawei.
- `_netmiko_fetch` (`ncm.py:62-105`): blocking; run via `asyncio.to_thread` from `_do_fetch`.
  `SSHDetect` autodetect falling back to `cisco_ios`; enable-secret handling; per-platform pager
  disable (`_disable_paging`, `ncm.py:113-132`: `terminal length 0`, `set cli pager off`,
  `screen-length disable`, etc.); custom raw-channel command runner `_run_command`
  (`ncm.py:135-174`) that walks `--More--` pagers by writing bursts of 20 spaces, with a 240s
  deadline; `_clean_cli` (`ncm.py:177-187`) strips ANSI escapes, backspaces, FortiOS/Cisco pager
  artifacts. `conn_timeout=20`, `timeout=60`, port from credential (default 22).
- Only `protocol='ssh'|'telnet'` allowed in `CredentialIn` (`ncm.py:310`), but the fetch path
  ignores the protocol field — everything goes through netmiko SSH.

### 3.3 Normalization / change detection (`ncm.py:190-264`)

`_VOLATILE_PATTERNS` (`ncm.py:193-205`) masks per-display churn before hashing/diffing: PEM bodies,
FortiOS `#conf_file_ver=` and `ENC <blob>` secrets, Cisco type-7/8/9 passwords and enable secrets,
`! Last configuration change`, `ntp clock-period`, `Building configuration...` headers.

`_save_config_version` (`ncm.py:216-264`):

- `content_hash` = sha256(raw), `norm_hash` = sha256(normalized).
- Change = `latest.norm_hash != nhash` (falls back to raw hash for pre-migrate-027 rows);
  first-ever capture counts as a change.
- **Every snapshot is inserted** (with `is_change` flag) so the UI shows full backup history.
- Retention inline at save time: `DELETE ... WHERE id NOT IN (SELECT id ... ORDER BY captured_at
  DESC LIMIT keep_versions)` per (device, config_type); `keep_versions` from `device_ncm`
  (default 5, max 100).

### 3.4 Config-change alerting (`ncm.py:267-301`)

`_raise_change_alert`: only when `device_ncm.alert_on_change` is true and there was a prior
version. Direct insert:

```sql
INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata)
VALUES (:d, NULL, 'active', 'warning', :msg, :t, CAST(:meta AS jsonb))
```

message: `Running-config changed on {hostname} (+N / -M lines)`; metadata JSONB:
`{"config_change": true, "config_type", "from_version", "to_version", "added", "removed",
"source"}`. `rule_id NULL` → **Alert Center only, no notification-channel dispatch** (email/SMS
routing is rule-driven). This is a known limitation vs. the epic's plan.

### 3.5 Scheduling (`ncm.py:593-624` + systemd)

`POST /api/v1/ncm/run-scheduled` — docstring: *"no auth — called by the localhost systemd timer"*.
Due-selection SQL supports three schedule types on `device_ncm`:

- `interval`: `last_success_at < NOW() - make_interval(hours => schedule_interval_hours)`
- `daily`: `LOCALTIME >= schedule_time AND last_success_at::date < CURRENT_DATE`
- `weekly`: additionally `EXTRACT(DOW FROM NOW())::int = ANY(schedule_days)` (0=Sun..6=Sat)

Runs `_do_fetch` **serially** per due device (no concurrency bound needed at present fleet sizes;
would not scale to hundreds of devices per hour).

Systemd units (`scripts/systemd/`, installed in `/etc/systemd/system/`):

```
# zenplus-ncm-backup.timer
OnCalendar=hourly / Persistent=true / RandomizedDelaySec=120
# zenplus-ncm-backup.service
Type=oneshot
ExecStart=/usr/bin/curl -sS -m 600 -X POST http://127.0.0.1:8000/api/v1/ncm/run-scheduled
```

**Security note for the Compliance plan:** `/api/v1/ncm/run-scheduled` has no auth dependency at
all — anyone who can reach the API (it is proxied by nginx under `/api/`) can trigger a fleet-wide
SSH backup storm. Any Compliance "sync/scan now" internal endpoint should NOT copy this pattern
verbatim; if it must be timer-driven, gate on localhost or a shared token. (Also, the UI itself
calls it: NcmPage "Run scheduled" button posts to it with a 600s timeout, `NcmPage.tsx:89-93`.)

---

## 4. Data model (Postgres, migrations 023–029)

All in `/opt/zenplus/scripts/` (idempotent, applied via updater; note MEMORY rule: never edit
shipped migrations).

### 4.1 `device_configs` — migrate-023-ncm.sql (+ 027, 029)

```sql
CREATE TABLE IF NOT EXISTS device_configs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id     UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    config_type   VARCHAR(20) NOT NULL DEFAULT 'running',   -- running | startup
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    line_count    INTEGER NOT NULL DEFAULT 0,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    captured_by   VARCHAR(40) NOT NULL DEFAULT 'manual',    -- manual | api | ssh
    source_note   TEXT
);
CREATE INDEX IF NOT EXISTS idx_device_configs_device_time ON device_configs (device_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_device_configs_hash ON device_configs (device_id, content_hash);
-- migrate-027: ADD COLUMN norm_hash TEXT
-- migrate-029: ADD COLUMN is_change boolean NOT NULL DEFAULT true
```

### 4.2 `ncm_credentials` + `device_ncm` — migrate-024-ncm-credentials.sql (+ 025, 026, 028)

```sql
CREATE TABLE IF NOT EXISTS ncm_credentials (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(120) NOT NULL,
    description         TEXT,
    protocol            VARCHAR(10) NOT NULL DEFAULT 'ssh',   -- ssh | telnet
    port                INTEGER NOT NULL DEFAULT 22,
    username            VARCHAR(120) NOT NULL,
    password_enc        BYTEA,
    enable_password_enc BYTEA,
    is_default          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS device_ncm (
    device_id        UUID PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    credential_id    UUID REFERENCES ncm_credentials(id) ON DELETE SET NULL,
    platform         VARCHAR(40) NOT NULL DEFAULT 'autodetect',  -- netmiko device_type
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    schedule_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    last_status      VARCHAR(20),       -- success | failed
    last_error       TEXT,
    last_attempt_at  TIMESTAMPTZ,
    last_success_at  TIMESTAMPTZ
);
-- migrate-025: device_ncm ADD schedule_interval_hours INTEGER NOT NULL DEFAULT 24
-- migrate-026: device_ncm ADD alert_on_change BOOLEAN NOT NULL DEFAULT TRUE
-- migrate-028: device_ncm ADD keep_versions INTEGER NOT NULL DEFAULT 5,
--              schedule_type VARCHAR(10) NOT NULL DEFAULT 'interval',  -- interval|daily|weekly
--              schedule_time TIME, schedule_days INTEGER[]  -- 0=Sun..6=Sat
```

No SQLAlchemy ORM models exist for any NCM table — `ncm.py` uses raw `text()` SQL throughout.

### 4.3 Secrets crypto

`server/app/core/crypto.py`: AES-256-GCM via `cryptography` package; ciphertext layout
`1-byte version || 12-byte nonce || ciphertext+tag`; key from `Settings.SNMP_ENC_KEY`
(hex/base64/raw 32 bytes). Same key/format mirrored in Go (`poller/internal/checker/snmp/crypto.go`).
`ncm.py` calls `crypto.encrypt(...)` at profile create/update and `crypto.decrypt(...)` in
`_do_fetch` (`ncm.py:547-548`). **Secrets are never returned by any NCM endpoint** — there is no
`/secrets` reveal route (unlike the epic plan and unlike `snmp_credentials.py`).

---

## 5. NCM credentials are already a shared asset (key reuse point)

`ncm_credentials` is referenced outside NCM in two shipped features:

1. **Discovery SSH probes** — `scripts/migrate-032-discovery-ssh-creds.sql`:
   ```sql
   -- Discovery profiles: SSH / CLI connection profiles (ncm_credentials) for authenticated probes.
   ALTER TABLE discovery_profiles
       ADD COLUMN IF NOT EXISTS ssh_credential_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
   ```
   Consumed by `server/app/services/discovery_executor.py`.

2. **Agentless server monitoring** — `scripts/migrate-034-server-agentless-credentials.sql`:
   ```sql
   -- ssh -> ncm_credential_id
   ALTER TABLE servers
       ADD COLUMN IF NOT EXISTS windows_credential_id uuid REFERENCES windows_credentials(id) ON DELETE SET NULL,
       ADD COLUMN IF NOT EXISTS snmp_credential_id uuid REFERENCES snmp_credentials(id) ON DELETE SET NULL,
       ADD COLUMN IF NOT EXISTS ncm_credential_id uuid REFERENCES ncm_credentials(id) ON DELETE SET NULL;
   ```
   Consumed by `server/app/api/v1/servers.py` and `server/app/schemas/agent.py`.

So the product already treats `ncm_credentials` as the generic "SSH/CLI credential store" for
devices AND servers. A Compliance module that needs SSH access (e.g., to read `show version`,
OS package lists on agentless servers) should reuse `ncm_credentials` / `device_ncm.credential_id`
/ `servers.ncm_credential_id` rather than invent a fourth credential table. The credential set for
one device resolves via: `device_ncm.credential_id → ncm_credentials` (per-device),
`is_default = true` row as fallback (UI behavior, `NcmDevicePage.tsx:65-68`).

---

## 6. Dashboard UI

Routes (`dashboard/src/App.tsx:214-215`):

```tsx
<Route path="ncm" element={<NcmPage />} />
<Route path="ncm/:deviceId" element={<NcmDevicePage />} />
```

Sidebar (`dashboard/src/components/layout/navigation.ts:253`): top-level entry
`{ to: '/ncm', label: 'Config Backup', icon: FileCode, hint: 'Device configuration archive' }` —
**no `permission` field**, so it is visible to every signed-in role (nav nodes with `permission`
are hidden via `hasPermission`, `Sidebar.tsx:65`).

### 6.1 `dashboard/src/pages/NcmPage.tsx` (586 lines) — fleet workspace

- Header "Config Backup (NCM)"; actions: **Run scheduled** (POST `/ncm/run-scheduled`, 600s
  timeout), **Connection Profiles** dialog, Refresh.
- KPI cards: Devices / Enrolled / Backed up / Coverage % (from `/ncm/overview` counters).
- Device table (search + facet filters by status/type/location/vendor, pagination, page-size
  selector) with per-row status badge: `backed_up | failed | pending | unconfigured`
  (`statusKey`, lines 26-31).
- Bulk selection → **Assign profile** (`POST /ncm/bulk-assign`), **Backup settings**
  (`POST /ncm/bulk-settings`), **Run backup now** (parallel `Promise.allSettled` of
  `POST /devices/{id}/config-fetch`, 240s timeout each; skips unenrolled).
- `ProfilesDialog` (lines 532-586): CRUD for connection profiles (name, username, port, password,
  enable secret, default flag). No platform field on the profile — platform is per-device.
- Data polling: `/ncm/overview` refetched every 30s.

### 6.2 `dashboard/src/pages/NcmDevicePage.tsx` (276 lines) — per-device page

- Backup settings card: connection profile select, platform select (from `/ncm/platforms`),
  scheduled-backup switch + frequency (interval hours / daily time / weekly days), keep-versions,
  alert-on-change switch, last error / last checked display; Save → `PUT /devices/{id}/ncm`,
  Remove → `DELETE /devices/{id}/ncm`.
- "Backup now (SSH)" → `POST /devices/{id}/config-fetch` (240s timeout) with inline result line.
- Versions card: list with `Baseline` / `Changed` / `No change` chips, per-version View
  (full content), Download (.cfg via client-side blob), and Compare-to-previous (diff dialog via
  `GET /devices/{id}/configs-diff?a=&b=`), plus "Diff latest" of the newest two versions.
- `DiffView` (lines 29-42): simple line-classed `<pre>` renderer (+green / −red / @@ primary).

### 6.3 `dashboard/src/pages/DeviceDetailPage.tsx` — integration points

- The formerly-inert **"Backup Config" button is now live** (line ~2279; mutation at
  `DeviceDetailPage.tsx:2168-2176` → `POST /devices/{deviceId}/config-fetch`). It does NOT deep-link
  to `/ncm/{id}`.
- **Dead field**: `DeviceDetailPage.tsx:289-295` renders `device.firmware_version` ("Firmware
  Version" secondary stat), but no server endpoint returns `firmware_version` — the device detail
  response (`server/app/api/v1/devices.py:1690-1728`) exposes `os_version`, `vendor`, `model`,
  `sys_object_id`, `serial_number` but no `firmware_version`. The stat is silently dropped by the
  `.filter((s) => s.value)`. If Compliance adds a real firmware field, this UI slot already exists.

---

## 7. RBAC / permission state (gap to fix or mirror)

- Permission catalog defines NCM slugs — `server/app/core/permissions.py:50-53`:
  `("ncm", "Config Backup (NCM)", [("ncm.view", ...), ("ncm.manage", ...)])`; granted to
  operator/editor (`:101`) and viewer gets `ncm.view` (`:116`); seeded in
  `scripts/migrate-074-rbac-roles.sql:47,58,66`.
- **But `ncm.py` never uses `require_permission`** — it uses only `get_current_user` (read) and
  `require_operator_user` (write; passes for role in OPERATOR_ROLES or roles holding
  `system.admin`/`devices.manage`, `server/app/core/security.py:165-177`). So a custom role granted
  only `ncm.manage` but not `devices.manage` cannot actually run backups, and a role with NO ncm
  permission can still read every config body. `require_permission` is used today only in
  `users.py`, `roles.py`, `udt.py`, `netpath.py`.
- The `/ncm` nav entry has no permission gate either.
- **Recommendation carried to the plan:** the Compliance module should use `require_permission`
  from day one (new slugs like `compliance.view` / `compliance.manage`), and note the NCM
  enforcement gap as a companion fix.

---

## 8. Grep results: existing EOL / firmware / CVE groundwork

Command shape: `grep -rniE "<term>" --include='*.py|*.ts|*.tsx|*.go|*.sql|*.md'` excluding
`node_modules|dist|venv|.git`.

### 8.1 `eol` / `end_of_life` / `end-of-support`

**Zero hits in code** (.py/.go/.ts/.tsx/.sql). Hits only in documentation:
`Documentation/enahce1/01..05` (competitive analysis / roadmap / epic docs) and the
already-written sibling raw reports in `Documentation/Compliance/raw/`
(`code-software-inventory.md`, `code-device-inventory.md`, `code-zentryc-sync.md`,
`code-frontend.md` — produced by parallel investigators; consult them for server-inventory and
zentryc-feed details).

### 8.2 `cve` / `vulnerab*` / `nvd`

**Zero hits in code.** Docs only:
- `Documentation/enahce1/05-EPIC-E4...md:18`: "Firmware EoL / CVE enrichment via NIST NVD feed"
  (Phase 2, never built).
- `Documentation/enahce1/04-ROADMAP.md:57,70`: Phase 4 — "**E4 phase 2** (config push + compliance
  + firmware EoL/CVE)"; dependency map `E4 (NCM) ──▶ E4p2 (push/compliance/firmware-EoL)`.
- `Documentation/enahce1/02-FEATURE-COMPARISON-MATRIX.md:30`: row "Compliance/FW-EoL/CVE" —
  ZenPlus "–" (gap vs. competitors).
- `Documentation/enahce1/03-CURRENT-PRODUCT-INVENTORY.md:160`:
  `| D3 | Compliance / firmware/EoL/vuln | ❌ | none |`.
- `Documentation/13-SHIP-READY-MASTER-PLAN.md`, `doc/virtual-appliance-taskable-plan.md`
  (appliance-hardening context, not feature code).

### 8.3 `firmware` in code — three hits, all display/collection, no tracking logic

1. `dashboard/src/pages/DeviceDetailPage.tsx:289-295` — dead `device.firmware_version` reference
   (see §6.3).
2. `scripts/migrate-062-monitoring-templates.sql:112-114` — FortiGate builtin monitoring template
   collects version **strings via SNMP**:
   ```json
   {"key":"fgt_fw_version","name":"Firmware","oid":"1.3.6.1.4.1.12356.101.4.1.1.0","type":"string"},
   {"key":"fgt_av_version","name":"AV Signatures","oid":"1.3.6.1.4.1.12356.101.4.2.1.0","type":"string"},
   {"key":"fgt_ips_version","name":"IPS Signatures","oid":"1.3.6.1.4.1.12356.101.4.2.2.0","type":"string"}
   ```
   Latest values land in Postgres **`device_template_values`** (schema in migrate-062:21-33:
   `device_id, group_key, metric_key, instance, series_key, label, unit, value_num, value_text,
   updated_at`, PK `(device_id, group_key, metric_key, instance)`), written by the Go poller;
   numeric series history goes to ClickHouse `snmp_metrics` under `tpl_*` keys.
3. `dashboard/src/components/devices/TemplateInsightsSection.tsx:333` — renders those text metrics
   ("firmware revisions, signature dates") on the device page.

### 8.4 Version-inventory fields that DO exist (Compliance raw material)

- `devices` table / `server/app/models/device.py`: `sys_object_id` (:66), `vendor` (:67),
  `model` (:68), `os_version` (:69), `serial_number` (:88, String(128), from controller-managed
  children feature), plus `device_type`, `poll_mode`, `managed_by_device_id`.
- Population path: SNMP profile match rules with regex extraction —
  `server/app/api/v1/snmp.py:262-296` (`extract_os_version` etc. against sysDescr); the Go poller
  continuously refreshes them: `poller/internal/store/postgres.go:308-322`
  (`os_version = COALESCE(NULLIF($4,''), os_version)`) using
  `poller/internal/checker/snmp/profile.go` (`ExtractOSVersion`, :52,192,241). Example FortiGate
  rule (migrate-062): `"extract_os_version": "v([0-9][0-9.,a-z]*)"`.
- Discovery: `server/app/models/discovery_v2.py:170-181` — `discovered_devices` rows carry
  `sys_object_id, serial_number, vendor, device_type, model, os (String(150)),
  os_version (String(60))`.
- Servers/agents: `server/app/schemas/agent.py:79,257` — agent registration reports `os_version`
  (+ `os_name`, `kernel_or_build`, `architecture`) persisted by
  `server/app/api/v1/agents.py:329-338,474-490`. (Full server software-inventory detail is in the
  sibling report `code-software-inventory.md`.)

**Conclusion:** there is no EOL/CVE data model, feed, matcher, or UI anywhere. What exists is a
reasonably reliable per-asset `vendor/model/os_version/serial_number/sys_object_id` inventory for
network devices (SNMP-extracted, poller-refreshed), OS identity for servers, and one
template-driven precedent for pulling exact firmware strings via SNMP OIDs.

---

## 9. How the Compliance module should relate to NCM — findings-driven guidance

1. **Separate module, sibling nav entry.** NCM is deliberately scoped as "Config Backup" (its nav
   label). Compliance is a different lifecycle (feeds, matching, remediation) — give it its own
   router file (`server/app/api/v1/compliance.py` or similar), own pages, own permission slugs.
   Do not bolt endpoints onto `ncm.py`; it is already a 756-line single-file module with no
   service layer.

2. **Reuse, don't duplicate, these NCM assets:**
   - `ncm_credentials` (+ `device_ncm.credential_id`, `servers.ncm_credential_id`) for any SSH
     interrogation (e.g., `show version`, package lists). Precedent for cross-module reuse already
     exists (discovery, agentless servers).
   - `device_ncm.platform` (netmiko device_type, autodetect-resolved and persisted after first
     successful fetch, `ncm.py:568-573`) — the best OS-family signal for CPE/EOL matching of
     network gear, more precise than `devices.vendor`.
   - `device_configs` snapshots — a future config-compliance (CIS/PCI rule) engine should evaluate
     rules against the **latest `device_configs` row per device** (`ORDER BY captured_at DESC
     LIMIT 1`, dedup semantics per §3.3) rather than re-fetching; use `_normalize_config`-style
     masking awareness (secrets are masked in normalized form only; raw `content` still contains
     type-7/ENC blobs).
   - The netmiko plumbing (`_netmiko_fetch`/`_run_command`/`_clean_cli`) if Compliance needs any
     CLI command output — consider extracting these into a shared `app/services/cli_fetch.py`
     rather than importing from a router module.
   - The alert-insert pattern (`ncm.py:287-298`) works for Alert-Center-only alerts, but note its
     limitation: `rule_id NULL` alerts never dispatch to email/SMS channels. If compliance
     findings must notify, plan a rule-type integration instead.
   - Scheduling: either a `zenplus-compliance-sync.timer` clone of `zenplus-ncm-backup.timer`
     (hourly curl to an internal endpoint — but authenticated/localhost-gated, see §3.5 warning),
     or piggyback the updater's zentryc sync (see `code-zentryc-sync.md`).

3. **Data-model attachment points:** key everything by `devices.id` / `servers.id`. For devices,
   the match tuple is (`vendor`, `model`, `os_version`, optionally `sys_object_id` prefix and
   `device_template_values` firmware strings). A per-device compliance state table can mirror the
   `device_ncm` one-row-per-device pattern (PK device_id, ON DELETE CASCADE).

4. **The firmware display slot already exists**: returning a real `firmware_version` (e.g., best of
   `device_template_values` fgt_fw_version-style strings or `os_version`) from the device-detail
   endpoint would light up `DeviceDetailPage.tsx:289-295` with no UI work.

5. **Permission slugs + enforcement:** add a `compliance` module to
   `server/app/core/permissions.py` PERMISSION_MODULES, seed roles via a new migration
   (mirroring migrate-074), and actually gate endpoints with `require_permission` (pattern in
   `udt.py`/`netpath.py`) — unlike NCM, which defined slugs but enforces none of them (§7).

6. **What NOT to inherit:** no audit trail (compliance likely needs one — the epic's planned
   `config_audit_log` design at `05-EPIC-E4...md:57` is still a good template); the unauthenticated
   internal endpoint; single-file router with embedded SSH/business logic; raw-SQL-only (fine, it
   is the house style for new modules — UDT/NCM/tags all use `text()` SQL, but keep it in a
   service module).
