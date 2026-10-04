# Alerts development review: verification, assessment and fixes

**Review verified:** `Alerts-development-review.md` (target: Alert Center on 10.11.40.31).
**Code base:** release 1.23.21 (`0e481598`), branch `fix/alerts-development-review`.
**Date:** 30 September 2026.

The review's findings were checked against the 1.23.21 source, and reproduced on this appliance (192.168.8.221, read-only) where data allowed. The fixes were then built and tested against a disposable PostgreSQL copy of the full schema, seeded with 1,306 open and 6,700 closed alerts, through a development API and the rebuilt UI.

**Verdict:** all 27 defects (A01–A27) are real. Several are worse than the review states, and this assessment found 11 more defects, one of them critical (X1). All 27 defects and all 11 extra findings are fixed. The 12 feature requests (F01–F12) are implemented in full or in part; the parts not built are listed under "Features".

## 1. Verification of the review findings

| ID | Verdict | Evidence / correction |
|---|---|---|
| A01 | Confirmed | `AlertsPage` requested `limit=200` and dropped `meta.total`. Here it looks harmless (100 active); on the customer box 1,227 of 1,427 were unreachable. |
| A02 | Confirmed, worse | The action routes had no scope check. Also, the "operator capability" they did check was `devices.manage`, not an alerts permission (see X2). |
| A03 | Confirmed, reproduced | On this box, a server alert (`agent_offline`) showed 8 "related" alerts, all from other devices, because of the fallback that matched on severity alone. |
| A04 | Confirmed | Silences could be listed for any entity id. DELETE removed a silence by id with no scope check and no audit record. |
| A05 | Confirmed | Rule edit, toggle and delete used `require_operator_user` (`devices.manage`), with no target scope and no audit. The host-rule API had the same gap. |
| A06 | Confirmed | `acknowledge_alert` / `resolve_alert` set state unconditionally and committed before the audit record was written. |
| A07 | Confirmed | Snooze resolved the open rows. The server path skipped the `resolved_by` marker, and the escalation sweeper then announced "all clear". Manual resolves were also announced as "all clear". |
| A08 | Confirmed | `minutes` had no bounds: negative values produced an already-expired silence, and 0 meant forever. |
| A09 | Confirmed, reproduced | Detail returned `entity: {kind: system}` for server alerts, and the list showed sensor alerts as "System". |
| A10 | Confirmed, worse | Server alerts kept their rule only in metadata, so they had no routing context. They were also never escalated (X7). |
| A11 | Confirmed | MTTR was averaged over the loaded rows only, with recovery notices and snoozes mixed in. On the seed data, 24 h recovery p50 is 38 min while the mean is 5.6 h: one skewed average is misleading. |
| A12 | Confirmed | History ranges filtered on `triggered_at`. An incident opened 35 days ago and closed today was missing from "Resolved 24h" (reproduced on seed data). |
| A13 | Confirmed in source | The window was built from the browser clock and memoised. The 12-hour offset and `NTPSynchronized=no` are an operations problem on the customer box: fix NTP there. The code now uses only the appliance clock. |
| A14 | Confirmed | Missing stats fell back to 0, and a zero Active count was shown in green. |
| A15 | Confirmed | Cards counted the whole organisation, while the critical banner counted only loaded rows. |
| A16 | Confirmed | Search matched only the message and device hostname. `%` and `_` were also not escaped (X11). |
| A17 | Confirmed | `q` was read once into state and never re-synced on Back/Forward. |
| A18 | Confirmed | Action buttons were shown regardless of permission. The routing editor read `/settings/channels`, which is admin-only. |
| A19 | Confirmed | `/settings/notifications` has no redirect entry, so it fell through to General settings. |
| A20 | Confirmed | "device-wide" was shown for any rule without an interface target, and compound rules showed only a count. |
| A21 | Confirmed | The quick editor dropped `reset_threshold`, rounded the hold time to minutes, and re-seeded on every 15 s refetch. |
| A22 | Confirmed | Enable/disable was one click with no impact information. |
| A23 | Confirmed | `acknowledged_by` was never serialised, and resolve had no reason or actor. |
| A24 | Confirmed, worse | Transports treated HTTP 4xx/5xx as sent (X5), so `notified=true` was set even for rejected deliveries. |
| A25 | Confirmed | There were no pending or suppressed views, and notices were mixed into history. |
| A26 | Confirmed | The error blocks had no retry, and detail dropped its content when a background refresh failed. |
| A27 | Confirmed in source | The UI was checked at 1440, 1024 and 390 px. Contrast ratios were not measured. |

## 2. Additional defects found in this assessment

| ID | Severity | Defect | Fix |
|---|---|---|---|
| X1 | **Critical (security)** | `/api/v1/alert-engine/evaluate`, `evaluate-service` and `evaluate-trap` have no authentication, and nginx proxies all of `/api/` publicly. An unauthenticated POST through HTTPS reached request validation on this box (checked with an empty body, which changes nothing). A forged "up" event closes real outages; forged "down" events page people. | The router now requires a loopback caller without proxy headers (`X-Real-IP` / `X-Forwarded-For`), and returns 404 otherwise. `install.sh` nginx has `location /api/v1/alert-engine/ { return 404; }`. The poller is unaffected: it calls `localhost:8000` directly. |
| X2 | High | The `alerts.view` / `alerts.acknowledge` / `alerts.manage` permissions existed but were not enforced. Viewers hold `alerts.acknowledge` but got 403; a role with `devices.manage` could edit fleet alert rules. | Reads require `alerts.view`, triage requires `alerts.acknowledge`, and policy changes require `alerts.manage`, on the alerts, alert-rules and host-alert-rules APIs. |
| X3 | High | Service-check alerts were never delivered to webhook, Slack, Teams, Discord or PagerDuty channels; only SMS and email were implemented on that path. | Added webhook-family delivery to the service path. |
| X4 | Medium | The SMS path in the engine ignored a disabled gateway (the `enabled` column). | Added `_sms_gateway()`, which honours `enabled`, as SMTP already did. |
| X5 | Medium | SMS gateway and webhook transports ignored the HTTP status, so rejected sends counted as sent. SMTP refusal of every recipient also counted as sent. | Added `ensure_accepted()` and `DeliveryRejected`; the send functions now return accepted or skipped. |
| X6 | Medium | The PagerDuty `dedup_key` fell back to `<rule>:unknown` for non-device alerts, so one entity's recovery resolved another entity's incident. | The key now uses device, server, service-check or hostname identity. |
| X7 | Medium | Host (server) alerts never escalated, because escalation joins on `alerts.rule_id` and the host path left it empty. | `create_server_alert` now sets `rule_id` from metadata (the FK is ON DELETE SET NULL). |
| X8 | Low | `resolved_today` counted recovery-notice rows. | Now counts incidents only. |
| X9 | Low | With snooze no longer closing rows, the host evaluator's quiet-hours re-send would have paged a snoozed condition. | The re-send checks the silence first, as does the hold sweeper. |
| X10 | Low | Rule `simulate`, which sends real notifications, was open to `devices.manage`. | Now requires `alerts.manage`. |
| X11 | Low | Search did not escape `%` or `_`. | They are now escaped. |

## 3. What was changed

### Database: `scripts/migrate-135-alert-workflow.sql`
- `alerts.assigned_to` / `assigned_at`, plus indexes for paging, closed-date and rule filters.
- `alert_events`: an immutable lifecycle history (actor, source, reason, old and new status, detail).
- `alert_deliveries`: one receipt per channel per attempt (accepted, failed or skipped, with a sanitised error and latency).
- `alert_silences.alert_id` / `condition_label` for the suppression workspace.
- A `BEFORE INSERT` trigger stores the owning rule in `metadata.policy_snapshot` (conditions, routing with channel names, escalation) for every producer (F08). A failure never blocks the insert.
- A backfill marks historic manual and snooze closures from the audit log, so history metrics separate them from genuine recoveries.

Every code path feature-detects the migration, so a rolling upgrade works whichever order the migration and the restart happen in.

### Backend
- **`services/alert_workflow.py`**
  - Guarded transitions: the row is locked with `FOR UPDATE`, repeats are idempotent, invalid transitions return 409, and state, event and audit commit together.
  - Snooze now suppresses without resolving. A snooze-expiry sweeper records "Snooze expired — still active" and reminds the rule's channels.
  - Ownership, comments, response targets.
  - Closure classification: recovered, manual, suppressed, superseded, administrative, hold-cancelled, dependency.
- **`services/alert_service.py`**: one filter builder shared by the list, summary, trend and export.
  - Views: active, suppressed, pending, acknowledged, resolved, open.
  - Rolling windows on the appliance clock, chosen by triggered, resolved or acknowledged time.
  - Entity, owner, age, rule-state and delivery filters; search across all entity names and IPs, rule name and alert-id prefix.
  - Stable sorts; `as_of` pins later pages to a snapshot and reports how many newer rows arrived.
  - Summary counts with p50/p95 timings, trend, top rules and top entities.
- **`services/alert_rule_scope.py`**: describes a rule's scope, counts what it affects, stops tag-scoped users changing fleet rules, and builds the rule-change audit diff.
- **`services/alert_delivery_log.py`**: the delivery receipts, instrumented in the engine's device and service loops and in `dispatch_to_channels` (used by trap, host, escalation, hold, network, UDT, NetPath and APM alerts).
- **`api/v1/alerts.py`**: rewritten against `docs/alerts-api-contract.md`.
  - Every action is permission-checked and scope-checked, answering 404 when out of scope.
  - Added endpoints: summary, trend, export (CSV with formula-injection guard), bulk, assign, comments, snooze-preview, silences workspace, assignees, channel directory, response targets.
  - Detail now returns typed related incidents with a reason, recurrence and flapping, evidence, lifecycle events, deliveries, the full rule policy with scope and affected count, and the policy snapshot.
- **`api/v1/alert_rules.py` / `host_alert_rules.py`**
  - Every change requires `alerts.manage` and passes the scope guard; create, update, delete and toggle are audited with before/after values.
  - Toggle takes an explicit `{enabled, reason}`.
  - New `GET /alert-rules/{id}/impact`.
- **Escalation**: suppressed incidents do not escalate. A manual closure sends "Closed by operator — not a confirmed recovery"; superseded and legacy-snooze closures send nothing. Escalations are recorded as lifecycle events.

### Dashboard
- **Alert Center**
  - Tabs: Active, Acknowledged, Suppressed, Pending, Resolved, All open, Silences, each with a count.
  - Server-side paging with totals and pinned snapshots, sort, page size.
  - The URL holds every filter and search follows Back/Forward.
  - The summary strip states its scope and never shows a false zero (unavailable, stale and retry states).
  - Also: data-freshness and clock-skew line, bulk actions with per-item results, CSV export, saved views, the insights section (trend, aging, top sources, backlog health) and the silences workspace.
  - Controls are hidden when the user lacks the permission.
- **Detail**
  - Specific headline; workflow status and condition shown separately; actions checked against permissions, with clear 409 handling.
  - Timeline with actors and comments; evidence; recurrence; related incidents grouped by reason.
  - Typed entity card; policy card with a shared-policy warning, impact-confirmed toggle, and a quick editor that keeps unedited fields and exact hold times and does not re-seed on refetch.
  - "Rule changed since this alert fired"; configured routing shown separately from delivery attempts; routing editor uses the channel directory.
  - Delete-rule is removed from the incident page.
- **Snooze dialog**: bounded durations; indefinite only with `alerts.manage` and a reason; impact preview; states that it does not resolve.

## 4. Features (F01–F12)

| ID | Status | Delivered | Not delivered |
|---|---|---|---|
| F01 | Done | Assign/unassign/assign-to-me, acknowledgement takes ownership, comments, hand-over notes, response targets (ack-overdue), unassigned-critical count | Team queues and watchers (the product has no team model yet) |
| F02 | Partial | Same-condition recurrence 24 h/7 d, flapping flag, previous occurrences, related incidents by entity, rule time window or upstream dependency, each with a reason | Automatic topology root-cause correlation |
| F03 | Done | Paging with snapshot pinning, stable sorts, full-population search and CSV export, bulk acknowledge/close/snooze/assign with per-item results | A preview of affected alerts before a bulk action |
| F04 | Done | Per-channel receipts with error categories, failed-delivery count and filter, open incidents with failed notifications | Manual re-send button |
| F05 | Partial | Evidence panel (status change, value vs threshold, interface, error, trap, SLO burn, upstream dependency) | Metric graphs, linked logs/traps and runbook links on the page |
| F06 | Done | Silences workspace (entity, condition, reason, owner, expiry, indefinite flag, open alerts muted), scoped and audited removal, expiry reminder | Links to maintenance windows and quiet-hours policy |
| F07 | Partial | Escalation levels with channel names, current escalation state, escalation events | On-call schedules and rotations |
| F08 | Done | Trigger-time policy snapshot, audited rule changes with before/after, lifecycle event history | — |
| F09 | Partial | Server-side p50/p95 time to recover/close/acknowledge with sample sizes, opened vs closed trend, aging, top rules/entities, delivery outcomes | Branded or scheduled management reports |
| F10 | Partial | Full URL state, shareable links, personal saved views | Team-shared saved views |
| F11 | Partial | `alert_id` and `event_type` in webhook payloads, PagerDuty dedup fix | Ticketing integration and two-way status sync |
| F12 | Done | Backlog health (rule disabled, rule deleted, older than 7 days, failed notification) with drill-downs | Retention policy changes |

**Deliberately not built:** a "reopen" action. The evaluators close incidents only on a status transition, so reopening an incident whose condition had already recovered would leave it open forever. Closure is reversible in practice because the evaluator raises a new incident if the condition returns.

## 5. Verification

- `server/tests`: **1,096 passed**, 8 skipped. This includes the new `test_alert_workflow.py` (46 cases) and the X1 guard test in `test_device_maintenance.py`.
- `server/tests/integration/test_alert_center_e2e.py` is gated on `ZENPLUS_ALERTS_E2E_URL` and must only run against a disposable seeded API. It **passed** against the scratch stack. It covers:
  - out-of-scope 404s on all eight action and detail routes, and mixed bulk requests;
  - role permissions and transition idempotency/409s;
  - snooze does not resolve; silences scoped; paging reaches the oldest row;
  - resolved-date basis, the population split, search by IP, server, service, sensor, synthetic and id prefix;
  - server alert entity, rule and routing; related incidents within scope; CSV export of the full population.
- Engine: a device down/up through the dev API recorded `failed` (unreachable webhook) and `skipped` (disabled channel) receipts against the right incidents. The snooze-expiry sweeper cleared the stamp, recorded the event and sent the reminder. A request through nginx (`X-Real-IP`) now gets 404.
- Dashboard: `tsc -b` clean and `vite build` OK. Checked with Playwright against the real dev API as admin, viewer and read_only, at 1440 and 390 px.

## 6. Deployment notes

1. Run the migration as postgres **before** restarting the API:
   `sudo -u postgres python3 /opt/zenplus/scripts/run-migrations.py --scripts-dir /opt/zenplus/scripts`
2. `sudo systemctl restart zenplus-api`, then rebuild the dashboard.
3. Optional hardening: add the `location /api/v1/alert-engine/ { return 404; }` block from `install.sh` to `/etc/nginx/conf.d/zenplus.conf`. The API guard already closes X1.
4. Behaviour changes operators will notice:
   - Snoozed alerts stay open and appear under **Suppressed**. Sidebar and summary Active counts exclude them, as before.
   - Viewers can acknowledge, because their role always granted `alerts.acknowledge`.
   - Rule editing needs `alerts.manage`; custom roles that relied on `devices.manage` alone need it added.
   - PagerDuty incidents already opened for non-device alerts under the old `<rule>:unknown` key will not be auto-resolved by the new key.
