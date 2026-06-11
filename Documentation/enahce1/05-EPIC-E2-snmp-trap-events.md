# E2 — SNMP Trap → Events Pipeline & Trap-Based Alerting

## 1. Goal & competitive rationale
ZenPlus already receives SNMP traps but treats them as a write-only audit log: there is no first-class events store, no MIB-based human-readable translation, and no way to alert on a trap (e.g. `linkDown`, `coldStart`, vendor OIDs). 10 of 15 competitors (PRTG, LibreNMS, Observium, SolarWinds) ship a full trap receiver with rule-driven alerting; without it, ZenPlus can't react to asynchronous device-initiated events between polls. This epic turns the existing pipe into a queryable events subsystem with translation, dedup/correlation, trap-condition alert rules, and a real events UI — closing the single largest fault-management gap versus competitors.

## 2. Scope
### In scope
- Normalized events store (ClickHouse) fed by the existing trap path, plus an `events` abstraction reusable beyond traps.
- MIB-based trap-OID → name/severity translation using the existing MIB Library (`snmp_mibs`) via `snmptranslate`, with a cached OID dictionary.
- Trap-condition alert rules (match by trap OID/name, varbind value, device/group scope) wired into the existing alert engine + notification channels.
- Events UI: device-detail Events tab (replacing the ad-hoc "SNMP traps" activity card) and a global Events page with filter/search/ack.
- v1/v2c traps. Trap dedup/flood suppression.

### Out of scope
- SNMPv3 trap (USM engine discovery) — deferred, noted in `traps.go`.
- Syslog ingestion as events (separate epic; schema designed to allow it later).
- Inform acknowledgements (SNMP INFORM PDU response).
- Event-to-incident escalation workflows.

## 3. Current state in ZenPlus
**Exists & verified:**
- Poller trap receiver: `poller/internal/checker/snmp/traps.go` — `TrapListener`, `onTrap` decodes v1/v2c varbinds, extracts `snmpTrapOID.0`, best-effort `severityFromTrapOID` (cold/warm/linkDown/linkUp/authFailure only), emits `TrapRecord` to a `TrapSink`. Wired in `poller/internal/pinger/engine.go:194` (bind from `SNMP_TRAP_BIND`, default `0.0.0.0:162`).
- Sink/storage: `poller/internal/store/clickhouse.go` — `WriteTrap` (415), `RunTrapBatchWriter` (425), `insertTrapBatch` (459) → ClickHouse `zenplus.snmp_traps` (DDL `scripts/migrate-004-snmp-clickhouse.sql:158`).
- Read APIs: `server/app/api/v1/snmp.py:920` `GET /snmp/traps`; `server/app/api/v1/devices.py:676` `GET /devices/{id}/traps`. Consumed by `dashboard/src/pages/DeviceDetailPage.tsx:379` (traps-summary → `ActivityLogCard`).
- MIB Library: `snmp_mibs` table (`scripts/migrate-005-snmp-discovery.sql:56`), upload/list/delete in `snmp.py` (692–769); files on disk at `SNMP_MIBS_DIR`. `dashboard/src/pages/MibLibraryPage.tsx`.
- Alerting: `alert_rules`/`alerts` models (`server/app/models/alert.py`), engine `server/app/api/v1/alert_engine.py` (`POST /alert-engine/evaluate`), poller call `engine.go:450`. Rules key on `metric/operator/threshold` and device/group/service scope only.

**Missing:** no `events` table; `trap_name` == OID (no MIB translation, no `snmptranslate` call anywhere); no trap-typed alert rule or any trap → alert-engine path; no varbind-aware matching; no events UI beyond the read-only activity card; no dedup/flood control.

## 4. Target design & architecture
```
device → UDP/162 → TrapListener.onTrap (traps.go)
        → translate (OID dict cache, MIB-backed) → enrich severity
        → TrapSink.WriteEvent  → ClickHouse zenplus.events (batched)
        → flood-suppress + POST /alert-engine/evaluate-trap (debounced)
              → match trap_alert_rules (OID/name/varbind/scope)
              → insert alerts row + notification_channels (existing)
UI: /events (global) + DeviceDetail Events tab → GET /events APIs (read ClickHouse)
```
Translation runs server-side (net-snmp `snmptranslate` against uploaded MIBs) and is exposed as `POST /snmp/oids/translate`; the poller fetches a compact OID→name/severity map at sync time and caches it, so per-trap decoding stays in-process and fast. Events are the new canonical store; `snmp_traps` is kept and dual-written for one release, then aliased.

## 5. Data model & migrations
**ClickHouse — `zenplus.events`** (new, generalizes `snmp_traps`):
`event_id UUID, ts DateTime64(3,'UTC'), source LowCardinality(String) /*trap|syslog|system*/, device_id Nullable(UUID), source_ip IPv4, trap_oid String, trap_name String, severity LowCardinality(String), message String, varbinds String /*JSON*/, dedup_key String, count UInt32 DEFAULT 1, poller_id String`. `ENGINE=MergeTree PARTITION BY toYYYYMM(ts) ORDER BY (device_id, ts) TTL ts+INTERVAL 90 DAY`. Skip indexes: `INDEX idx_trap_oid trap_oid TYPE bloom_filter`, `INDEX idx_sev severity TYPE set(8)`. Keep `snmp_traps` (back-compat reads); dual-write during phase 1.

**Postgres — `trap_alert_rules`** (new; reuse pattern from `alert.py`): `id, name, enabled, match_trap_oid VARCHAR, match_trap_name VARCHAR, match_varbind_oid VARCHAR, match_varbind_op VARCHAR(4), match_varbind_value TEXT, device_id FK, group_id FK, severity, notify_channels JSONB, cooldown INT, dedup_window_sec INT DEFAULT 60, created_by FK`. Add `event_id UUID` and `source VARCHAR` columns to `alerts` (nullable) so trap-born alerts link back. Add **`snmp_oid_dictionary`** cache table (`oid PK, name, mib_name, severity_hint, updated_at`) populated from MIB translation.

Migrations: `migrate-006-events.sql` (ClickHouse events table) + `migrate-007-trap-rules.sql` (Postgres). Both `IF NOT EXISTS`, additive, no destructive change to `snmp_traps`.

## 6. API changes
- `POST /api/v1/alert-engine/evaluate-trap` — internal, no auth (mirrors `/evaluate`). Body: `{device_id?, source_ip, trap_oid, trap_name, severity, varbinds[], ts}`. Matches `trap_alert_rules`, dedups by `dedup_key` within `dedup_window_sec`, inserts `alerts`, fans out to channels. Returns `{evaluated_rules, notifications_sent}`.
- `GET /api/v1/events` — auth. Query: `source, device_id, severity, trap_oid, q, hours, skip, limit`. Returns paginated normalized events from ClickHouse.
- `GET /api/v1/devices/{id}/events` — auth, device-scoped events.
- `POST /api/v1/snmp/oids/translate` — auth. Body `{oids:[...]}` → `[{oid,name,mib_name,severity_hint}]` via `snmptranslate -Of` against `SNMP_MIBS_DIR`; upserts `snmp_oid_dictionary`.
- `GET /api/v1/poller/oid-dictionary` — internal; compact map for poller cache (etag/`updated_at` gated).
- CRUD `GET/POST/PUT/DELETE /api/v1/trap-alert-rules` — auth (admin-gated via `get_current_user` role check, consistent with existing endpoints).
- Optional `POST /api/v1/events/{event_id}/ack` for events that became alerts.

## 7. Poller / collector changes
- `poller/internal/checker/snmp/traps.go`: rename/extend `TrapRecord`→`EventRecord` (add `DedupKey`, `Varbinds` typed); replace static `severityFromTrapOID` with a lookup into an injected `OIDTranslator` interface; compute `dedup_key = oid|source_ip|ifIndex`.
- New `poller/internal/checker/snmp/oiddict.go`: `OIDTranslator` caching the server's OID dictionary (refresh via `GET /poller/oid-dictionary`, fallback to built-in well-known OIDs from `oids.go`).
- New `poller/internal/checker/snmp/trapalert.go`: debounced `POST /alert-engine/evaluate-trap`, reusing the HTTP pattern in `engine.go:450` (`evaluateAlerts`). Flood guard: token-bucket per `dedup_key`.
- `poller/internal/store/clickhouse.go`: add `WriteEvent`/`insertEventBatch` for `zenplus.events`; keep `WriteTrap` dual-write behind a flag during phase 1.
- `engine.go`: pass translator + alert dispatcher into `NewTrapListener` (currently `engine.go:194`); wire dictionary refresh into the existing sync ticker.
- Library: continue with `github.com/gosnmp/gosnmp`.

## 8. Dashboard changes
- New global page `dashboard/src/pages/EventsPage.tsx` + route `events` in `App.tsx` (alongside `alerts`/`alert-rules` at lines 92–93). Filterable table (severity, source, device, OID, text), severity chips, varbind expand, deep-link to device.
- `DeviceDetailPage.tsx`: replace the traps-summary card (379–575) with an **Events** tab consuming `GET /devices/{id}/events`; keep `ActivityLogCard` styling.
- New `dashboard/src/pages/TrapAlertRulesPage.tsx` + `TrapAlertRuleFormDialog.tsx` (model on `AlertRuleFormDialog.tsx`): trap-OID picker with live translate via `/snmp/oids/translate`, varbind condition builder, channel selector.
- `MibLibraryPage.tsx`: add a "Translate OID" inline tool calling the new endpoint, surfacing which uploaded MIB resolved it.

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|------|------|---------|------------|
| 1 | `events` ClickHouse table + `snmp_oid_dictionary`/`trap_alert_rules` Postgres migrations | db | 1.5 | — |
| 2 | `snmptranslate` service + `POST /snmp/oids/translate` + dictionary upsert | api | 2 | 1 |
| 3 | `GET /poller/oid-dictionary` (etag-gated) | api | 0.5 | 2 |
| 4 | Poller `OIDTranslator` cache (`oiddict.go`) | poller | 2 | 3 |
| 5 | Refactor `traps.go` → `EventRecord`, dedup_key, translator hook | poller | 2 | 4 |
| 6 | `WriteEvent`/`insertEventBatch` + dual-write flag (`clickhouse.go`) | poller | 1.5 | 5 |
| 7 | `trap_alert_rules` CRUD API + RBAC | api | 2 | 1 |
| 8 | `POST /alert-engine/evaluate-trap` (match, dedup, fan-out) | api | 2.5 | 7 |
| 9 | Poller `trapalert.go` debounced dispatch + flood guard | poller | 2 | 6,8 |
| 10 | `GET /events` + `GET /devices/{id}/events` | api | 1.5 | 1 |
| 11 | Global `EventsPage` + route | ui | 2.5 | 10 |
| 12 | DeviceDetail Events tab | ui | 1.5 | 10 |
| 13 | `TrapAlertRulesPage` + form dialog w/ OID picker | ui | 3 | 7,2 |
| 14 | MIB Library translate tool | ui | 1 | 2 |
| 15 | Seed well-known trap OIDs + severities into dictionary | db | 0.5 | 1 |
| 16 | E2E + perf (trap storm) + docs | infra | 2 | 9,11,13 |

## 10. Acceptance criteria
- [ ] A `linkDown` trap appears in `/events` and the device Events tab within 5s, showing `IF-MIB::linkDown` (translated), severity, and varbinds.
- [ ] Uploading a vendor MIB then translating its enterprise OID returns the symbolic name and resolving MIB.
- [ ] A trap alert rule matching `linkDown` on a group fires an alert + notification through an existing channel; a non-matching trap does not.
- [ ] Varbind condition (`ifOperStatus == 2`) gates rule firing correctly.
- [ ] A 1000-trap/min storm produces deduped events (count incremented) and at most one alert per `cooldown` window.
- [ ] Non-admins can view events but cannot create/edit trap alert rules.
- [ ] Existing `snmp_traps` reads and the legacy device activity card keep working through phase 1.

## 11. Test cases
| ID | Type | Precondition | Steps | Expected result |
|----|------|--------------|-------|-----------------|
| T1 | unit | — | Decode v2c PDU with `snmpTrapOID.0=linkDown` | `EventRecord.trap_oid` = `1.3.6.1.6.3.1.1.5.3`, severity `critical` |
| T2 | unit | — | Decode v1 trap w/ enterprise+generic | trap_oid composed from header fields |
| T3 | unit | dictionary has Cisco MIB | Translate `1.3.6.1.4.1.9...` | symbolic name + mib_name returned |
| T4 | unit | — | Build dedup_key for two identical linkDowns | keys equal; second increments count |
| T5 | integration | events migration applied | Send trap via `snmptrap` | row in `zenplus.events`, queryable via `/events` |
| T6 | integration | rule matches linkDown | Send linkDown | one `alerts` row, one channel send |
| T7 | integration | rule with varbind cond | Send trap with non-matching varbind | no alert |
| T8 | integration | source IP not a device | Send trap | event stored with `device_id=null`, visible in global events |
| T9 | e2e | UI up | Open device Events tab, send trap | row appears < 5s, varbinds expandable |
| T10 | e2e | admin | Create trap rule via OID picker, trigger | alert visible on Alerts page |
| T11 | rbac | viewer role | POST `/trap-alert-rules` | 403 |
| T12 | rbac | viewer role | GET `/events` | 200 |
| T13 | perf | poller running | 1000 traps/min for 2 min | events deduped, alerts ≤ 1/cooldown, no listener drop |
| T14 | failure | MIB dir empty | Translate unknown OID | falls back to numeric OID, no 500 |
| T15 | failure | alert-engine down | Send matching trap | event still stored; dispatch retried/logged, no crash |
| T16 | failure | malformed varbind (binary) | Send trap with non-UTF8 octet string | stored as hex (per `stringifyPDUValue`), no panic |
| T17 | regression | phase-1 dual-write | Send trap | both `snmp_traps` and `events` populated; legacy `/snmp/traps` unchanged |
| T18 | security | — | POST trap from spoofed IP w/ wrong community filter | dropped when community filter set |
| T19 | manual | v3 trap sent | Send SNMPv3 trap | gracefully ignored/logged (out of scope), no crash |
| T20 | integration | dictionary etag | Poller refresh with unchanged dict | 304, no re-download |

## 12. Risks & rollout
- **Feature flags:** `EVENTS_PIPELINE_ENABLED` (dual-write events), `TRAP_ALERTING_ENABLED` (dispatch + rules UI), `EVENTS_UI_ENABLED`. Ship dark, enable per-tenant.
- **Migration/back-compat:** all migrations additive; `snmp_traps` retained and dual-written for one release, then global events page reads `events` only and legacy endpoints become thin views. No poller redeploy required for read APIs.
- **Performance:** trap storms are the main risk — mitigate with poller-side token-bucket flood guard, `dedup_key` collapsing, debounced/batched alert-engine calls, and modest ClickHouse batch (reuse 256/2s pattern in `clickhouse.go`). Bloom-filter index keeps `/events` OID filters cheap.
- **Security:** `snmptranslate`/MIB upload already validated (`_MIB_NAME_RE`, 4MB cap); reuse for translate. Enforce community filter in `onTrap`; trap-rule CRUD admin-gated; varbind values escaped before notification templating (`_render`).
- **Phased rollout:** Phase 1 events store + dual-write + read UI (tasks 1,5,6,10–12); Phase 2 MIB translation + dictionary (2–4,14); Phase 3 trap alert rules + dispatch (7–9,13); Phase 4 cut legacy reads to `events`, drop dual-write. v3 traps tracked as a fast-follow epic.
