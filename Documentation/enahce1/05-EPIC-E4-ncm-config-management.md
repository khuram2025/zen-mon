# E4 — Network Configuration Management (NCM): Backup / Versioning / Diff + Change Alerts

## 1. Goal & competitive rationale
NCM is the single biggest category gap versus SolarWinds NCM, ManageEngine Network Configuration Manager, Auvik, and LogicMonitor — all of which monetize config backup, drift detection, and rollback heavily. ZenPlus already monitors *health* but has zero *configuration* visibility, so a single fat-fingered ACL or an unauthorized firewall change is invisible until something breaks. Phase 1 (scheduled SSH/CLI config pull → versioned storage → diff viewer → change/drift alerts → audit trail) turns the inert "Backup Config" stub into a defensible, sticky feature that raises switching cost and unlocks MSP/compliance buyers.

## 2. Scope
### In scope (Phase 1)
- Per-vendor SSH/CLI config retrieval (Cisco IOS/NX-OS, Palo Alto PAN-OS, FortiGate FortiOS — the three seen live), with SNMP-assisted fallback where a vendor exposes config-copy via SNMP.
- Encrypted device login credentials (reusing `core/crypto.py` AES-256-GCM, mirrored by poller `snmp/crypto.go`).
- Versioned config storage (content-addressed, dedup by hash), running + startup config types.
- Unified diff viewer (side-by-side + inline), config-changed and drift alerts wired into the existing alert pipeline.
- Immutable audit trail (who/what/when, pull source, success/failure).
- Scheduled + on-demand pulls; the existing "Backup Config" button becomes live.

### Out of scope (Phase 2, outlined only)
- Bulk config **push**/templates, golden-config remediation.
- Compliance policy engine (CIS/PCI rule sets).
- Firmware EoL / CVE enrichment via NIST NVD feed.
- Real interactive console / TACACS+ change attribution.

## 3. Current state in ZenPlus
**Exists & verified:**
- Reusable secret crypto: `server/app/core/crypto.py` (`encrypt`/`decrypt`, AES-256-GCM, version||nonce||ct layout) and the Go mirror `poller/internal/checker/snmp/crypto.go` (`Decrypt`). Same `SNMP_ENC_KEY`.
- Credential CRUD pattern to clone: `server/app/api/v1/snmp_credentials.py` (`CredentialCreate/Response`, `/secrets`, `/assign`, default-flag handling) and `device_service._apply_credential` (`server/app/services/device_service.py:11`).
- Device model `server/app/models/device.py` (`Device`, `vendor`/`model`/`os_version`/`sys_object_id`, `snmp_credential_id`).
- Poller scheduling: `poller/internal/pinger/engine.go` ticker loop (`Run`, `snmpTicker` at :226, `syncSNMPDevices`/`runSNMPCycle`); device loading in `poller/internal/store/postgres.go` (`LoadSNMPDevices`). `golang.org/x/crypto` already a dep (SSH client available, no new module).
- Alerts: `server/app/models/alert.py` (`Alert`, `AlertRule`), `services/alert_service.py`; notification gateways (`/settings/gateways` SMTP/SMS) in `dashboard/src/pages/GatewaysPage.tsx`.
- Device detail UI stubs: `DeviceDetailPage.tsx:283` ("Open Console"), `:1379` ("Backup Config") — both inert buttons today.
- Migration convention: hand-written idempotent SQL in `scripts/migrate-00N-*.sql` (e.g. `migrate-008-sensors.sql`), plus `*-clickhouse.sql` siblings.

**Missing:** any config-pull collector, config storage tables/blobs, diff engine, device-login credentials (only SNMP creds today), NCM API/router, and any NCM dashboard surface.

## 4. Target design & architecture
```
┌ Dashboard ─────────────┐     ┌ FastAPI (server/app) ───────────────┐
│ DeviceDetail ▸ Config   │ →   │ /api/v1/config-backups (CRUD/diff)  │
│  tab: versions, diff,   │ ←   │ /api/v1/device-credentials (CLI)    │
│  schedule, audit        │     │ ncm_service (diff, hash, retention) │
└─────────────────────────┘     └──────┬───────────────┬─────────────┘
                                  Postgres(cfg)    blob store (CH/FS)
                                        │ (creds, jobs, audit)
┌ Go Poller (engine.go) ───────────────┴───────────────────────────┐
│ configTicker → ncmCycle → per-vendor SSH driver (expect/CLI)      │
│   pull running+startup → normalize → sha256 → if changed: store   │
│   + publish redis ncm.changed → API persists version + alert      │
└───────────────────────────────────────────────────────────────────┘
```
The poller stays the only component touching device networks. It pulls config over SSH using vendor "drivers" (command set + prompt/pager handling), normalizes (strip timestamps/`! Last configuration change`), hashes, and only ships a new version when the hash differs from `latest`. Change/drift evaluation reuses the alert engine: a new `config_changed` rule type fires through existing notify channels.

## 5. Data model & migrations
New `scripts/migrate-009-ncm.sql` (Postgres, idempotent) + `migrate-009-ncm-clickhouse.sql`.

**Postgres:**
- `device_credentials` (mirrors `snmp_credentials`): `id, name, description, cred_type('ssh'|'telnet'|'api'), username, password BYTEA (crypto.encrypt), enable_password BYTEA, ssh_key BYTEA, port INT, is_default BOOL, created_by, timestamps`. `devices.config_credential_id UUID` (nullable FK, like `snmp_credential_id`).
- `config_backup_jobs`: `id, device_id FK, schedule_cron TEXT, config_types TEXT[] DEFAULT '{running,startup}', enabled BOOL, retention_count INT DEFAULT 30, last_run_at, last_status, next_run_at`.
- `config_versions`: `id, device_id FK, config_type, content_hash CHAR(64), size_bytes INT, blob_ref TEXT, captured_at TIMESTAMPTZ, captured_by('schedule'|'manual'|user_id), is_current BOOL, prev_version_id UUID`. Index `(device_id, config_type, captured_at DESC)`, unique `(device_id, config_type, content_hash)` partial where dedup.
- `config_audit_log`: `id, device_id, version_id, action('pull'|'diff_detected'|'pull_failed'|'cred_change'), actor, source, detail JSONB, created_at`. Append-only (no UPDATE/DELETE grant).
- `vendor_drivers` (seed, not per-tenant): `vendor, os_match, commands JSONB, prompt_regex, pager_disable_cmd`.

**Blob storage:** raw config text in ClickHouse `config_blobs` (`content_hash String, body String, compressed UInt8, captured_at DateTime`) `ENGINE=ReplacingMergeTree ORDER BY content_hash` — dedup + cheap retention. Alternative FS path (`/var/lib/zenplus/configs/<hash>`) behind a setting; default ClickHouse to match existing metrics topology. Configs are text and compress well; gzip before insert.

**Migration notes:** all `CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS devices.config_credential_id`; run via existing `updater/steps/run_migration.py`. Backfill none.

## 6. API changes
New router `server/app/api/v1/ncm.py` (+ `device_credentials.py`), mounted in `main.py` like the others.

| Method + Path | Purpose | Key fields |
|---|---|---|
| `GET /api/v1/device-credentials` | list CLI creds | mirrors snmp_credentials (no secrets) |
| `POST/PUT/DELETE /api/v1/device-credentials[/{id}]` | CRUD | `username,password,enable_password,ssh_key,port,cred_type,is_default` |
| `GET /api/v1/device-credentials/{id}/secrets` | reveal for edit | admin-only |
| `POST /api/v1/devices/{id}/config/backup` | trigger on-demand pull (wires the button) | `config_types[]` → `{job_id,status}` |
| `GET /api/v1/devices/{id}/config/versions` | list versions | `id,config_type,captured_at,size,is_current,hash` |
| `GET /api/v1/config-versions/{id}` | raw/rendered content | `body` (RBAC-gated) |
| `GET /api/v1/devices/{id}/config/diff?from=&to=` | unified diff | `{hunks[],added,removed,from_meta,to_meta}` |
| `GET/PUT /api/v1/devices/{id}/config/schedule` | backup job | `schedule_cron,config_types,retention_count,enabled` |
| `GET /api/v1/devices/{id}/config/audit` | audit trail | paginated `action,actor,source,detail` |
| `POST /api/v1/internal/ncm/versions` | poller → API ingest (mTLS/token) | `device_id,config_type,hash,body,source` |

## 7. Poller / collector changes
New package `poller/internal/ncm/`:
- `driver.go` — `Driver` interface (`Pull(ctx, conn) (map[string]Config, error)`), registry by `vendor/os`.
- `cisco.go`, `panos.go`, `fortios.go` — command sets (`terminal length 0` / `set cli pager off` / `config system console set output standard`; `show running-config` / `show config | display set` / `show full-configuration`).
- `ssh.go` — `golang.org/x/crypto/ssh` (already in `go.mod`) session, password/enable/key auth, prompt + pager handling, read timeout.
- `normalize.go` — strip volatile lines, CRLF→LF, sha256.
- `collector.go` — load NCM-enabled devices (new `store.LoadNCMDevices`, modeled on `LoadSNMPDevices`, decrypting creds with existing `snmp.Decrypt`), pull, dedup vs latest hash (cached in Redis), POST changed configs to `/internal/ncm/versions`, publish `ncm.changed` event.

`engine.go`: add `configTicker` (default 5 min, honoring per-device cron) + `runNCMCycle(ctx)` in the `select` loop, parallel to `snmpTicker`. Reuse the existing per-device goroutine + bounded-concurrency pattern from `runSNMPCycle`.

## 8. Dashboard changes
- New **Config** tab on `DeviceDetailPage.tsx`: version timeline, diff viewer (reuse a lightweight `react-diff-view`/custom hunk renderer), "Backup now" (wire the `:1379` button to `POST .../config/backup`), schedule editor, audit table.
- New `dashboard/src/pages/ConfigCredentialsPage.tsx` (clone of `SnmpProfilesPage`/credentials UI) under Settings; route in `App.tsx`.
- New global **NCM** page: cross-device "recent config changes" feed + drift dashboard.
- Hooks `useConfigVersions.ts`, `useConfigDiff.ts`; `api.ts` additions. Reuse `Tabs`, `Badge`, `ConfirmDialog`, `Toast`.

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|---|---|---|---|
| 1 | `migrate-009-ncm.sql` + clickhouse blobs | db | 1.5 | — |
| 2 | `device_credentials` model + CRUD API (clone snmp_credentials) | api | 2 | 1 |
| 3 | Config-credentials UI + Settings route | ui | 2 | 2 |
| 4 | `ncm_service` (hash, dedup, retention, diff) | api | 2.5 | 1 |
| 5 | NCM API router (versions/diff/schedule/audit) | api | 2.5 | 4 |
| 6 | Poller SSH transport + auth | poller | 2.5 | 1 |
| 7 | Vendor drivers (Cisco/PAN-OS/FortiOS) | poller | 3 | 6 |
| 8 | Poller normalize + dedup + ingest POST | poller | 2 | 6,5 |
| 9 | `configTicker`/`runNCMCycle` in engine + `LoadNCMDevices` | poller | 1.5 | 8 |
| 10 | `config_changed` alert rule type + wiring | api | 1.5 | 5 |
| 11 | DeviceDetail Config tab (timeline+diff+backup) | ui | 3 | 5 |
| 12 | Schedule editor + audit table UI | ui | 1.5 | 5 |
| 13 | Global NCM changes feed page | ui | 1.5 | 5 |
| 14 | Audit-log append-only enforcement + RBAC gating | api | 1 | 5 |
| 15 | Retention/GC job (ClickHouse TTL + version pruning) | infra | 1 | 1,4 |
| 16 | E2E + driver fixtures + docs | infra | 2 | all |

## 10. Acceptance criteria
- [ ] User stores per-device CLI credentials; password never returned except via `/secrets` (admin).
- [ ] "Backup Config" button triggers an on-demand pull and a new version appears within ~30s for Cisco/PAN/Forti.
- [ ] Scheduled pulls run on cron; unchanged configs create **no** new version (dedup by hash) but log an audit `pull` entry.
- [ ] Changed config creates a new version, fires a `config_changed` alert via configured channels, and is visible in diff (side-by-side + inline).
- [ ] Audit trail shows actor/source/timestamp and cannot be edited/deleted via API.
- [ ] Retention keeps last N versions; older blobs GC'd.
- [ ] Non-admins cannot view config bodies or credentials.

## 11. Test cases
| ID | Type | Precondition | Steps | Expected |
|---|---|---|---|---|
| T1 | unit | crypto key set | encrypt(pw) → poller `Decrypt` | roundtrip equal |
| T2 | unit | two config texts | run diff engine | correct add/remove hunks |
| T3 | unit | identical config | normalize+hash twice | hashes equal; no version |
| T4 | unit | config w/ timestamp line | normalize | volatile lines stripped |
| T5 | integration | Cisco mock SSH | run cisco driver | running+startup captured |
| T6 | integration | PAN-OS mock | pull | `set`-format config stored |
| T7 | integration | FortiOS mock | pull | full-config stored |
| T8 | integration | existing version | pull unchanged | no new row, audit `pull` |
| T9 | integration | changed config | pull | new version + `config_changed` alert |
| T10 | e2e | device + cred + schedule | click "Backup now" → open Config tab | version + diff render |
| T11 | e2e | 2 versions | select from/to diff | inline+side-by-side correct |
| T12 | manual | wrong password | trigger pull | `pull_failed` audit, alert, no version |
| T13 | manual | SSH timeout/unreachable | trigger pull | graceful fail, retry, no crash |
| T14 | security | non-admin user | GET config body / secrets | 403 |
| T15 | security | any user | attempt DELETE audit row | denied (append-only) |
| T16 | perf | 500 NCM devices | one cron cycle | completes < interval, bounded concurrency |
| T17 | perf | 30 versions × 200KB | retention GC | prunes to N, blobs removed |
| T18 | regression | NCM disabled (flag off) | normal ping/SNMP cycle | unaffected, no new tickers fire |
| T19 | integration | dedup blob | 2 devices same config | single `config_blobs` row (ReplacingMergeTree) |
| T20 | manual | cred rotation | update password | `cred_change` audited, next pull uses new pw |

## 12. Risks & rollout
- **Feature flag:** `NCM_ENABLED` (settings) gates the poller ticker and API router; ships dark. Per-device opt-in via `config_backup_jobs.enabled`.
- **Security:** new CLI passwords are the most sensitive secrets in the product — reuse `crypto.encrypt` (BYTEA, never plaintext-at-rest), `/secrets` admin-gated, audit every credential change, mTLS/token on the `/internal/ncm` ingest path. SSH host-key pinning (store known-host on first pull) to prevent MITM.
- **Perf:** SSH pulls are slow/serial per device — bound concurrency (reuse `runSNMPCycle` worker pattern), cache `latest hash` in Redis to avoid shipping bodies; compress blobs; ClickHouse `ReplacingMergeTree` + TTL for retention.
- **Back-compat:** purely additive migrations (`IF NOT EXISTS`), no changes to existing tables except a nullable `config_credential_id`. Poller is backward compatible if it ignores NCM tables when flag off.
- **Phased rollout:** (1) creds + manual on-demand pull for Cisco only → (2) scheduling + diff + alerts + PAN/Forti → (3) global feed + retention GC → then Phase 2 (push/templates, compliance, NIST CVE/EoL). Dogfood internally on lab Cisco/Palo/Forti before GA.
