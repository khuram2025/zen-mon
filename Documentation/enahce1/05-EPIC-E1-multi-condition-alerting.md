# E1 — Multi-Condition Alert Rules + Dependency-Aware Suppression

## 1. Goal & competitive rationale
ZenPlus alert rules today fire on a single metric/threshold and, in practice, only on device `ping_status` transitions. All 15 tracked competitors (PRTG, LibreNMS, Zabbix, Datadog, etc.) support compound conditions and dependency-based suppression. Without these, a single upstream outage produces an alert storm (one notification per downstream device) and operators cannot express rules like "RTT > 200ms **AND** packet_loss > 5% for 5m." This epic delivers AND/OR condition groups, per-condition evaluation windows, parent/child suppression wired from the topology graph, and rule→Channel routing — closing the largest functional gap versus the field.

## 2. Scope
### In scope
- Compound conditions: AND/OR groups, multiple metrics on one device, per-condition window/aggregation (avg/max/last).
- Server-side metric evaluation (rtt, packet_loss, jitter, SNMP scalars) — not just status transitions.
- Device-to-device parent/dependency graph + suppression: downstream alerts marked `suppressed` when an upstream parent is down.
- Rule→Channel routing (replace the partly-wired `notify_channels` path; surface a notification-channels CRUD).
- Backward compatibility for existing single-condition rules.

### Out of scope
- Visual Automated-Maps editor UI (consume its graph data; do not rebuild it).
- New channel transport types beyond existing email/SMS/webhook/slack/telegram.
- Anomaly/ML thresholds; escalation policies (separate epic).
- Flapping detection redesign (reuse existing `cooldown`/`max_repeat`).

## 3. Current state in ZenPlus
- **Rule model** `server/app/models/alert.py:AlertRule` — flat `metric/operator/threshold/duration` columns; scope via `device_id/group_id/service_check_id/service_check_group_id`; `notify_channels: JSONB`. `Alert` has `status/severity/message/extra_data(metadata)`.
- **Engine** `server/app/api/v1/alert_engine.py` — `POST /alert-engine/evaluate` fetches all enabled rules and matches **only on status transition** (`trigger_on`, scope). It **never reads `metric/operator/threshold` against metric values** — rtt/packet_loss thresholds are dead config. Channel dispatch (`_send_sms`, `_send_email`) is inlined here; webhook/slack/telegram are not dispatched.
- **CRUD** `server/app/api/v1/alert_rules.py` — full rule CRUD plus `/preview`, `/simulate`. Columns exist beyond the model (`trigger_on`, `min_duration`, `max_repeat`, `schedule_*`) via `scripts/migrate-001-alerts.sql`.
- **Poller** `poller/internal/pinger/engine.go:evaluateAlerts` (L450) POSTs `device_id/old_status/new_status/rtt_ms/packet_loss` to the engine on transition only.
- **Topology** `devices.parent_id` references **device_groups**, not another device (`scripts/init-postgres.sql:26`); there is **no device-to-device dependency edge**. No Maps router and no notification-channels CRUD router exist in `main.py` — both are **live-only, not in this repo**. Schema is raw idempotent SQL (`scripts/migrate-NNN-*.sql`), no Alembic.

## 4. Target design & architecture
Introduce a normalized **conditions** model and a **rule evaluation service** that the engine and a new poller metric-tap both feed.

```
poller(engine.go) --status change--> /alert-engine/evaluate ─┐
poller(engine.go) --metric sample---> /alert-engine/metric ──┤
                                                             v
                                          AlertEvaluationService
                                    (load rule + conditions, eval AND/OR
                                     groups over per-condition windows
                                     using cached recent samples in Redis)
                                                             │ fires?
                                                             v
                                    DependencyResolver (devices.parent_device_id
                                     graph): is any upstream parent DOWN?
                                       └─ yes → alert.status='suppressed'
                                       └─ no  → ChannelRouter → email/sms/webhook/slack/telegram
```

- **Conditions** live in `alert_rule_conditions` (Postgres), grouped by `group_index` with per-group `combinator`. A rule's top-level `condition_logic` combines groups.
- **Evaluation window**: each condition has `window_sec` + `aggregation`. The engine pulls the last-N samples from Redis (poller already writes realtime) or ClickHouse for longer windows.
- **DependencyResolver** walks `device_parents` edges (closure-cached in Redis) to find any DOWN ancestor; if found, the new alert is recorded as `suppressed` with `suppressed_by` set.
- **ChannelRouter** centralizes dispatch (extracted from `alert_engine.py`) and adds webhook/slack/telegram.

## 5. Data model & migrations
Postgres (config), raw SQL `scripts/migrate-008-alert-conditions.sql`, idempotent `ADD COLUMN/CREATE TABLE IF NOT EXISTS`:

- **`alert_rule_conditions`**: `id UUID PK`, `rule_id UUID FK→alert_rules ON DELETE CASCADE`, `group_index INT`, `metric VARCHAR(50)`, `operator VARCHAR(4)`, `threshold DOUBLE PRECISION`, `window_sec INT DEFAULT 0`, `aggregation VARCHAR(8) DEFAULT 'last'` (last/avg/max/min/p95), `created_at`. Index `(rule_id, group_index)`.
- **`alert_rules` additions**: `condition_logic VARCHAR(4) DEFAULT 'AND'` (top-level group combinator), `group_combinator VARCHAR(4) DEFAULT 'OR'`, `suppress_when_parent_down BOOLEAN DEFAULT TRUE`. Backfill: for every existing rule, insert one condition row from its flat `metric/operator/threshold/min_duration→window_sec`. Keep legacy columns (read-through fallback).
- **`device_parents`** (dependency edges): `child_id UUID FK→devices`, `parent_id UUID FK→devices`, `source VARCHAR(16)` (manual/automap/snmp), PK `(child_id, parent_id)`. Populated from Automated-Maps export when present, else manual.
- **`alerts` additions**: `status` CHECK extended to include `'suppressed'`; `suppressed_by UUID NULL` (the parent device), `suppressed_at TIMESTAMPTZ NULL`.
- **`notification_channels`** already exists (`init-postgres.sql`); add CRUD only (no schema change).
- **ClickHouse**: none required for v1 (windows read from Redis recent buffer; longer windows query existing metrics tables). Optional `alert_events` ClickHouse table for audit/perf later.

## 6. API changes
- `POST /api/v1/alert-rules` / `PUT /{id}` — extended body: `conditions: [{group_index, metric, operator, threshold, window_sec, aggregation}]`, `condition_logic`, `group_combinator`, `suppress_when_parent_down`. Returns nested `conditions`. Single-condition bodies still accepted (auto-wrapped).
- `GET /api/v1/alert-rules` / `GET /{id}` — include `conditions` array.
- `POST /api/v1/alert-engine/metric` (internal) — new: `{device_id, samples:[{metric, value, ts}]}`; evaluates threshold conditions server-side.
- `POST /api/v1/alert-engine/evaluate` — unchanged contract; internally routes through `AlertEvaluationService` + suppression.
- `GET/POST/PUT/DELETE /api/v1/notification-channels` — channel CRUD (new router) + `POST /{id}/test`.
- `GET/POST/DELETE /api/v1/devices/{id}/parents` — manage dependency edges; `POST /api/v1/topology/import` to bulk-load edges from Automated-Maps.
- `GET /api/v1/alerts?status=suppressed` — already supported by `alert_service.get_alerts` (just a new status value).

## 7. Poller / collector changes
- `poller/internal/pinger/engine.go`: add `e.publishMetricSample(...)` that POSTs periodic rtt/packet_loss/jitter (and SNMP scalars from `checker/snmp/collector.go`) to `/alert-engine/metric` — batched, throttled (e.g. every poll for changed values). Reuse the existing `http.Client` pattern in `evaluateAlerts` (L450–489).
- Keep `evaluateAlerts` for transitions; metric thresholds go through the new endpoint. Add a `config` flag `AlertMetricPush` to gate it.
- No new protocol/library; reuse `encoding/json`, `net/http`. SNMP path already emits "normalized scalar for thresholding" (`collector.go:612`) — wire that into the sample push.

## 8. Dashboard changes
- `dashboard/src/components/forms/AlertRuleFormDialog.tsx`: replace single metric/operator/threshold block with a **ConditionsBuilder** — repeatable rows grouped into AND/OR groups, each with metric, operator, threshold, window, aggregation; top-level AND/OR toggle; "suppress when parent down" switch; required Channel multiselect (fixes "Alerts: Needs channel").
- `dashboard/src/pages/AlertRulesPage.tsx`: render condition summary (e.g. "RTT>200 AND loss>5%") instead of one metric column.
- `dashboard/src/pages/AlertsPage.tsx`: add **Suppressed** filter/tab and a "suppressed by {parent}" badge.
- New `dashboard/src/pages/NotificationChannelsPage.tsx` + route for channel CRUD/test.
- New `DependencyEditor` (lightweight) on the device detail page to set parents when Automated-Maps isn't the source.

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|------|------|---------|-----------|
| 1 | Migration 008: conditions, device_parents, alert suppression cols; backfill | db | 1.5 | — |
| 2 | `AlertRuleCondition` model + nested schemas; rule CRUD reads/writes conditions | api | 2 | 1 |
| 3 | `AlertEvaluationService`: load rule+conditions, AND/OR group eval | api | 3 | 2 |
| 4 | Redis recent-sample buffer + windowed aggregation (avg/max/p95) | api | 2 | 3 |
| 5 | `DependencyResolver` (parent closure, cached) + suppression logic | api | 2 | 1 |
| 6 | `ChannelRouter` (extract dispatch; add webhook/slack/telegram) | api | 2 | 2 |
| 7 | New `/alert-engine/metric` endpoint wired to service | api | 1 | 3,4 |
| 8 | Notification-channels CRUD router + `/test` | api | 1.5 | 6 |
| 9 | `/devices/{id}/parents` + `/topology/import` endpoints | api | 1.5 | 1 |
| 10 | Poller metric sample push (rtt/loss/jitter/SNMP) | poller | 2 | 7 |
| 11 | ConditionsBuilder component in rule form | ui | 3 | 2 |
| 12 | Rule list/summary + required Channel multiselect | ui | 1.5 | 2,8 |
| 13 | AlertsPage suppressed filter + badge | ui | 1 | 5 |
| 14 | NotificationChannelsPage + DependencyEditor | ui | 2 | 8,9 |
| 15 | Backfill verification + back-compat for legacy single-cond rules | db/api | 1 | 1,2 |
| 16 | E2E + load tests; feature-flag gating | infra | 2 | all |

## 10. Acceptance criteria
- [ ] A rule with ≥2 conditions across metrics fires only when the AND/OR logic is satisfied over each condition's window.
- [ ] rtt/packet_loss/jitter thresholds evaluate server-side (not only ping transitions).
- [ ] When an upstream parent is DOWN, downstream device alerts are created with `status='suppressed'` and `suppressed_by` set; no notification is sent for them.
- [ ] When the parent recovers, suppression stops; newly fired downstream alerts notify normally.
- [ ] Every rule must select ≥1 Channel; rules route to email/SMS/webhook/slack/telegram per channel type.
- [ ] Existing single-condition rules continue firing unchanged after migration (backfilled to one condition).
- [ ] AlertsPage exposes a Suppressed view; suppressed alerts show their suppressing parent.
- [ ] Channel CRUD + Test send works; "Alerts: Needs channel" state clears once a channel exists and is bound.

## 11. Test cases
| ID | Type | Precondition | Steps | Expected result |
|----|------|-------------|-------|-----------------|
| T1 | unit | Service loaded | Eval AND group, one cond false | Rule does not fire |
| T2 | unit | Service loaded | Eval OR group, one cond true | Rule fires |
| T3 | unit | window_sec=300, avg | Feed samples avg below threshold | No fire; above → fire |
| T4 | unit | p95 aggregation | Feed 20 samples, 1 spike | p95 below threshold → no fire |
| T5 | integration | rtt rule via /alert-engine/metric | POST rtt sample > threshold | Alert row created, channel notified |
| T6 | integration | Parent DOWN, child rule fires | Trigger child condition | Alert `status='suppressed'`, `suppressed_by`=parent, 0 notifications |
| T7 | integration | Parent recovers | Re-trigger child | Alert `active`, notification sent |
| T8 | integration | Multi-level chain A→B→C, A down | C fires | C suppressed (transitive ancestor down) |
| T9 | e2e | Rule form open | Add 2 conditions + OR group, save | Persisted with nested conditions; list shows summary |
| T10 | e2e | Channel exists | Bind channel, save rule | "Needs channel" cleared; test send succeeds |
| T11 | manual | Legacy single-cond rule | Run migration, view rule | One backfilled condition; still fires as before |
| T12 | integration | Webhook channel | Fire rule | Webhook POST received with payload |
| T13 | security | Non-admin user | Call POST /notification-channels | 403/forbidden per RBAC |
| T14 | security | Unauthed | Call internal /alert-engine/metric externally | Network-restricted/internal-only enforced |
| T15 | perf | 5k rules, 50k samples/min | Drive metric push | p95 eval latency < 200ms, no notification storms |
| T16 | perf | 10k-edge dependency graph | Mass parent-down event | Suppression resolves < 1s using cached closure |
| T17 | regression | Existing ping transition rule | Device down→up | Fires + recovery exactly as pre-change |
| T18 | integration | Cooldown set | Two rapid triggers | Second suppressed by cooldown, not re-notified |
| T19 | edge | Cyclic parent edge inserted | Resolve dependency | Cycle detected, no infinite loop, logged |
| T20 | manual | Suppressed alert in UI | Open AlertsPage Suppressed tab | Badge shows suppressing parent name |

## 12. Risks & rollout
- **Feature flags**: `ALERT_MULTI_CONDITION`, `ALERT_DEPENDENCY_SUPPRESSION`, poller `AlertMetricPush`. Default off; enable per-tenant after backfill verification.
- **Migration/back-compat**: migration 008 is idempotent and additive; legacy `metric/operator/threshold` columns retained and backfilled into one condition row so old rules keep working even with the flag off. New engine path falls back to legacy single-condition evaluation when no `conditions` rows exist.
- **Perf**: windowed aggregation reads from Redis recent buffer (avoid ClickHouse per-eval); dependency closure cached in Redis with invalidation on edge change. Cap conditions/rule (e.g. 20) and rules scanned per event via scope-indexed query.
- **Security**: `/alert-engine/*` stay internal-only (bind localhost / network policy, as today); channel CRUD behind `get_current_user` + admin RBAC; webhook URLs validated (no SSRF to internal ranges).
- **Suppression correctness risk**: a missing/incorrect parent edge could silently hide real alerts — mitigate by always recording suppressed alerts (visible in UI) rather than dropping them, and emitting a daily "suppressed count" digest.
- **Phased rollout**: (1) ship migration + CRUD + conditions read/write (flag off); (2) enable server-side metric eval on a pilot tenant; (3) enable dependency suppression once edges are imported and verified; (4) general availability.

Relevant files: `/home/zen/zen-mon-push/server/app/api/v1/alert_engine.py`, `/home/zen/zen-mon-push/server/app/api/v1/alert_rules.py`, `/home/zen/zen-mon-push/server/app/models/alert.py`, `/home/zen/zen-mon-push/server/app/schemas/alert.py`, `/home/zen/zen-mon-push/server/app/services/alert_service.py`, `/home/zen/zen-mon-push/poller/internal/pinger/engine.go`, `/home/zen/zen-mon-push/dashboard/src/components/forms/AlertRuleFormDialog.tsx`, `/home/zen/zen-mon-push/dashboard/src/pages/AlertRulesPage.tsx`, `/home/zen/zen-mon-push/dashboard/src/pages/AlertsPage.tsx`, `/home/zen/zen-mon-push/scripts/init-postgres.sql`, `/home/zen/zen-mon-push/scripts/migrate-001-alerts.sql`.
