# E9 — On-Call/Escalation + Incident Management + ITSM Connectors

## 1. Goal & competitive rationale
ZenPlus today fires per-rule notifications but has no concept of an *incident* that survives flaps, gets owned, escalates when un-acked, and syncs to an ITSM system of record. PagerDuty, Opsgenie, and Datadog On-Call win deals on exactly this: tiered on-call schedules, time-based escalation, a single incident timeline, and bidirectional Jira/ServiceNow tickets. Delivering on-call + escalation + an incident object + ITSM/Teams connectors closes the largest gap between ZenPlus and the "NOC-grade" tier and turns the existing Channels "Planned" badges (Teams, Jira, ServiceNow) into shipped value.

## 2. Scope
### In scope
- On-call **schedules** (rotations: weekly/daily/custom, timezone-aware) and **escalation policies** (tiered, time-based step delays, repeat).
- First-class **Incident** object: open/ack/assign/resolve, timeline, severity, dedup key, linked alerts, runbook URL/markdown.
- Escalation **runner** (background task) that advances un-acked incidents through policy tiers and notifies the on-call target.
- **Connectors**: Jira, ServiceNow (create/update/resolve ticket + comment sync), Microsoft Teams (Adaptive Card notify + ack via Action.Http).
- Wire device + service status changes into incident open/resolve via the existing `alert-engine` path.
- UI: Incident list + detail/timeline, On-Call schedule editor, Escalation policy editor, connector config in Channels.

### Out of scope
- Voice/phone calls, automated remediation/runbook *execution*, full ChatOps slash-commands, SSO-mapped on-call sync (Okta/AD groups), status pages (separate epic).
- Replacing the existing per-rule template notifications (kept for back-compat behind a flag).

## 3. Current state in ZenPlus
**Exists (verified):**
- Alerting dispatch: `server/app/api/v1/alert_engine.py` — `evaluate_status_change` / `evaluate_service_status_change` insert `alerts` rows and send via `_send_email`/`_send_sms`. Poller calls these over HTTP in `poller/internal/pinger/engine.go:451` (`evaluateAlerts`) and `:495` (`evaluateServiceAlerts`).
- Alert model + ack/resolve: `server/app/models/alert.py` (`Alert`, `acknowledged_by/at`, `resolved_at`), `server/app/services/alert_service.py` (`acknowledge_alert`, `resolve_alert`), `server/app/api/v1/alerts.py`.
- Notification plumbing: `notification_channels` + `notification_gateways` tables and CRUD in `server/app/api/v1/settings.py` (`/channels`, `/gateways`, `/channels/{id}/test`); dashboard `NotificationsPage.tsx` (email/sms/webhook/slack/telegram) and `GatewaysPage.tsx` (SMTP/SMS).
- "Incident" UI is really a **status-transition timeline**, not an object: `dashboard/src/pages/ServiceIncidentsPage.tsx` reads `/service-checks/{id}/status-history` from ClickHouse `service_status_log`. SLA/MTTR derived ad hoc in `service_check_service.get_service_sla`.
- RBAC: single `role` string on `User` (`server/app/models/user.py:18`, default `viewer`).
- Migrations: numbered SQL in `scripts/migrate-00N-*.sql` (no Alembic).

**Missing:** Incident object, on-call schedules, escalation policies, escalation runner, ITSM/Teams connectors, connector-typed channels, RBAC roles beyond a string. The local repo has no Channels/Gateways "newer build" beyond the above — connector pattern designed below from first principles, consistent with the channel/gateway dispatch model.

## 4. Target design & architecture
Introduce an **incident layer** between alert events and notifications. The alert-engine becomes a thin event ingester that calls a new `incident_service` to open/dedupe/resolve incidents; escalation is driven by a background **escalation runner** loop.

```
poller ──HTTP──▶ alert-engine.evaluate[-service]
                      │  (still writes `alerts` for back-compat)
                      ▼
              incident_service.ingest(event)         Redis: incident:escalate zset (due times)
                ├─ dedupe_key match? update : open ──▶ enqueue tier-0 notify
                └─ recovery? auto-resolve incident
                      ▼
   escalation_runner (asyncio loop / APScheduler, leader-locked in Redis)
        pops due incidents → next tier → resolve on-call target → dispatch
                      ▼
        connector_registry.dispatch(channel) ── email/sms/slack/teams/jira/servicenow
                      ▼
        incident_events (timeline) + connector_links (external ticket ids)
```
On-call resolution: `schedule_service.current_on_call(schedule_id, at)` computes the active participant from rotation config + overrides. Escalation policy tiers reference either a `schedule_id` or explicit `channel_ids`. Connectors implement a common `Connector` interface (`notify`, `open_ticket`, `update_ticket`, `resolve_ticket`, `test`) registered by `type`, mirroring the existing channel/gateway dispatch but pluggable.

## 5. Data model & migrations
New `scripts/migrate-008-incidents-oncall.sql` (PostgreSQL — config/state of record; ClickHouse unchanged, reused for metrics/MTTR):

- `incidents`: `id uuid pk, dedupe_key text unique-ish (idx), title text, severity text, status text('open'|'acked'|'resolved'), device_id uuid null, service_check_id uuid null, escalation_policy_id uuid null, current_tier int default 0, assigned_to uuid null, acked_by uuid null, acked_at, resolved_at, runbook_url text, runbook_md text, opened_at, updated_at, extra_data jsonb`. Indexes: `(status)`, `(dedupe_key)`, `(opened_at desc)`.
- `incident_events`: `id uuid pk, incident_id fk, ts, kind text('opened'|'escalated'|'acked'|'assigned'|'note'|'notified'|'resolved'|'ticket_synced'), actor_id uuid null, channel_id uuid null, detail jsonb`. Index `(incident_id, ts)`.
- `oncall_schedules`: `id, name, timezone text, rotation jsonb (type, length, handoff_time, participants[]), created_by`. 
- `oncall_overrides`: `id, schedule_id fk, user_id, starts_at, ends_at` (idx `(schedule_id, starts_at, ends_at)`).
- `escalation_policies`: `id, name, repeat_count int default 0`. 
- `escalation_steps`: `id, policy_id fk, tier int, delay_seconds int, target_type text('schedule'|'channel'|'user'), schedule_id null, channel_ids jsonb, idx (policy_id, tier)`.
- `connector_links`: `incident_id fk, connector_type text, channel_id fk, external_id text, external_url text, last_synced_at`. Unique `(incident_id, channel_id)`.
- Extend `notification_channels.type` allowed values to include `teams|jira|servicenow`; reuse `config jsonb` (e.g. Jira: base_url, project_key, issue_type, api_token-ref). Secrets stored via existing `app/core/crypto.py` pattern.
- Add `escalation_policy_id` FK to `alert_rules` (nullable) so rules opt into incident escalation.

Migration notes: additive only, no destructive changes; back-compat retained because `alerts` table and existing dispatch are untouched.

## 6. API changes
New router `incidents.py` (`/api/v1/incidents`), plus on-call/escalation under settings:
- `GET /incidents?status&severity&assignee&device_id&service_check_id&skip&limit` → paginated list (mirrors `alerts` shape).
- `GET /incidents/{id}` → incident + timeline + connector_links.
- `POST /incidents/{id}/ack` → set acked, write event, halt escalation (body: `{note?}`).
- `POST /incidents/{id}/assign` → `{user_id}`.
- `POST /incidents/{id}/resolve` → `{note?}`; pushes resolve to linked tickets.
- `POST /incidents/{id}/note` → `{markdown}` timeline note (and optional `sync_to_ticket`).
- `POST /incidents` (manual incident) and `POST /incidents/from-alert/{alert_id}`.
- On-call: `GET/POST/PUT/DELETE /settings/oncall/schedules`, `GET /settings/oncall/schedules/{id}/current` (who's on now), `POST/DELETE /settings/oncall/overrides`.
- Escalation: `GET/POST/PUT/DELETE /settings/escalation-policies` (with nested steps).
- Connectors: extend `POST /settings/channels` to accept `teams|jira|servicenow`; `POST /settings/channels/{id}/test` already exists — extend to new types.
- Webhook callback `POST /api/v1/incidents/ack-callback` (token-signed) for Teams Action.Http / inbound ITSM resolve.

## 7. Poller / collector changes
Minimal. The poller already POSTs to `alert-engine/evaluate[-service]` (`engine.go:451/495`); no payload change required — the server-side ingest derives `dedupe_key` from `device_id`/`service_check_id`. Add only: include a stable `event_id`/`dedupe_key` hint and `severity` passthrough in the JSON payload in `evaluateAlerts`/`evaluateServiceAlerts` (Go map additions) so the server can dedupe without an extra DB read. No new protocols/libraries in Go.

## 8. Dashboard changes
- New **`IncidentsPage.tsx`** (route `/incidents`): list with status/severity/assignee filters, ack/assign/resolve inline actions (reuses `api`, react-query patterns from `NotificationsPage.tsx`).
- New **`IncidentDetailPage.tsx`** (`/incidents/:id`): timeline (extend the event-table UI from `ServiceIncidentsPage.tsx`), ack/assign/resolve, runbook panel, linked-ticket chips.
- New **`OnCallPage.tsx`** + **`EscalationPoliciesPage.tsx`** under Settings: rotation editor, "who's on call now" widget, tiered-step builder.
- Extend `NotificationsPage.tsx` `ChannelFormDialog` to add `teams`/`jira`/`servicenow` types with type-specific config fields; show connector test result.
- Repoint `ServiceIncidentsPage` "Incidents" entry points to the new incident object where one exists (keep transition timeline as a sub-tab).

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|------|------|---------|-----------|
| 1 | `migrate-008` schema + SQLAlchemy models (incidents, events, oncall, escalation, connector_links) | db | 2 | — |
| 2 | `incident_service` (open/dedupe/ack/assign/resolve + timeline writes) | api | 2.5 | 1 |
| 3 | `incidents.py` router + Pydantic schemas | api | 1.5 | 2 |
| 4 | Wire `alert_engine` ingest → `incident_service` (keep `alerts` back-compat) | api | 1.5 | 2 |
| 5 | `schedule_service.current_on_call` (rotation + overrides, tz-aware) | api | 2 | 1 |
| 6 | Escalation runner loop (Redis leader lock + due-zset) | infra/api | 2.5 | 2,5 |
| 7 | Connector interface + registry; refactor email/sms/slack into it | api | 2 | 2 |
| 8 | Microsoft Teams connector (Adaptive Card + Action.Http ack callback) | api | 2 | 7 |
| 9 | Jira connector (create/update/resolve/comment) | api | 2 | 7 |
| 10 | ServiceNow connector (incident table API) | api | 2 | 7 |
| 11 | Signed ack/resolve callback endpoint + token util | api | 1 | 4,8 |
| 12 | On-call + escalation settings APIs | api | 1.5 | 5 |
| 13 | `IncidentsPage` + `IncidentDetailPage` (timeline, actions) | ui | 3 | 3 |
| 14 | `OnCallPage` + `EscalationPoliciesPage` | ui | 3 | 12 |
| 15 | Extend channel form for teams/jira/servicenow + test | ui | 1.5 | 7 |
| 16 | RBAC: add `responder`/`oncall` capabilities to role checks | api/db | 1 | 1 |
| 17 | Poller payload: add `dedupe_key`/`severity` hints | poller | 0.5 | 4 |
| 18 | Tests + seed data + docs | qa | 2.5 | all |

## 10. Acceptance criteria
- [ ] A device/service going down opens exactly **one** incident; flapping updates the same incident (dedupe), not N new ones.
- [ ] Recovery auto-resolves the open incident and posts a timeline event + resolves linked tickets.
- [ ] An un-acked incident escalates to tier 1 after the configured delay and notifies the on-call user; acking halts escalation.
- [ ] "Who's on call now" reflects rotation + an active override.
- [ ] Creating a Jira/ServiceNow connector and attaching it to a policy produces a real ticket with the incident URL; resolve closes it.
- [ ] Teams card delivers and its **Ack** button acks the incident via callback.
- [ ] Incident detail shows a complete, ordered timeline (opened→notified→escalated→acked→resolved).
- [ ] Viewers cannot ack/assign/resolve; responders can.

## 11. Test cases
| ID | Type | Precondition | Steps | Expected result |
|----|------|--------------|-------|-----------------|
| T1 | unit | dedupe_key built from service_check_id | ingest 5 down events for same check | 1 incident, current_tier unchanged |
| T2 | integration | open incident + policy w/ 60s tier delay | wait past delay without ack | escalated to tier 1, on-call notified, event logged |
| T3 | integration | escalating incident | ack via API before tier 2 | status=acked, no further escalation |
| T4 | integration | open incident + recovery event | poller posts new_status=up | incident resolved, resolved_at set, ticket resolved |
| T5 | unit | weekly rotation, 3 participants, tz=Asia/Riyadh | query current_on_call across handoff boundary | correct participant each window |
| T6 | unit | override covering now | current_on_call | returns override user, not rotation user |
| T7 | integration | Jira connector configured | open incident on linked policy | issue created, connector_link.external_id set |
| T8 | integration | Jira-linked incident | resolve incident | Jira transitioned to Done, comment synced |
| T9 | integration | ServiceNow connector | open + resolve | sn_incident created then closed |
| T10 | e2e | Teams connector + ack callback | open incident → click Ack in card | incident acked, escalation stopped |
| T11 | security | callback endpoint | replay/forged token | 401; valid signed token accepted once |
| T12 | security | viewer role | POST /incidents/{id}/resolve | 403 |
| T13 | edge | connector API 500/timeout | open incident | notify retried/queued, incident still opens, error in timeline |
| T14 | edge | maintenance window active | down event during window | no incident opened (mute respected) |
| T15 | perf | 1k concurrent status changes | burst ingest | <2s p95 ingest, no duplicate incidents, runner keeps up |
| T16 | regression | legacy alert_rule w/o policy | trigger | existing email/sms still sent; `alerts` row written |
| T17 | unit | escalation policy repeat_count=2 | exhaust tiers unacked | wraps and re-notifies repeat_count times then stops |
| T18 | integration | assign incident | POST assign user_id | assigned_to set, timeline `assigned`, assignee notified |
| T19 | manual | UI | open IncidentDetail | timeline ordered, runbook + ticket chips render |
| T20 | unit | dedupe across device vs service | down on device + down on its check | two distinct incidents (different keys) |

## 12. Risks & rollout
- **Feature flags:** `INCIDENTS_ENABLED` gates ingest→incident path (default off → legacy notifications only); per-rule `escalation_policy_id` opt-in. Connectors gated per-channel `enabled`.
- **Back-compat:** `alerts` table, alert-engine signatures, and poller payload remain valid; incidents are additive. Existing email/sms/slack refactored behind the connector interface without behavior change (covered by T16).
- **Escalation runner safety:** single-leader via Redis lock to avoid double-paging; idempotent tier advance keyed on `(incident_id, tier)`; missed ticks recovered from the due-zset, not wall-clock drift.
- **Security:** connector secrets encrypted via `app/core/crypto.py`; outbound SSRF guard on Teams/webhook URLs; signed, single-use, expiring tokens for ack callbacks; RBAC gate on all mutating incident/on-call endpoints (default `viewer` denied).
- **Performance:** dedupe via indexed `dedupe_key`; ingest does at most one upsert; MTTR/SLA still read from ClickHouse rollups (`get_service_sla`). Rate-limit/retry connector calls in a bounded worker queue so ITSM latency can't stall ingest.
- **Phased rollout:** (1) incident object + ack/assign/resolve + UI, internal-only; (2) on-call + escalation runner behind flag for a pilot tenant; (3) Teams; (4) Jira; (5) ServiceNow + bidirectional sync. Each phase shippable independently with its own flag.
