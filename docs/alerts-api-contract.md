# Alert Center API contract (Alerts review A01–A27, F01–F12)

Contract shared by the backend (`server/app/api/v1/alerts.py`) and the Alert
Center UI (`dashboard/src/pages/AlertsPage.tsx`, `AlertDetailPage.tsx`,
`components/alerts/*`). Everything is **additive**: existing callers of
`GET /alerts?status=…&limit=…`, `/alerts/stats` and `/alerts/device-counts`
keep working unchanged.

All timestamps are ISO-8601 UTC strings produced by the appliance. Every list,
summary and detail response carries `server_time` (appliance "now") so the UI
never measures ages against the browser clock.

## Permissions

| Action | Permission | Notes |
|---|---|---|
| Any read (`GET /alerts*`) | `alerts.view` | |
| acknowledge, resolve, snooze (finite), unsnooze, assign, comment, bulk | `alerts.acknowledge` | |
| Indefinite snooze, remove a silence created as indefinite | `alerts.manage` | reason required for indefinite |
| Rule create/update/delete/toggle, routing, response targets | `alerts.manage` | plus rule target scope |
| Channel directory (`GET /alerts/channel-directory`) | `alerts.manage` | |

Every per-alert action first checks the user's tag scope; an absent **or
out-of-scope** alert returns **404** (never 403) so ids cannot be probed.
The UI reads permissions from `useCan()` (`/auth/me` → `permissions`), and the
detail payload also returns `permissions` for the signed-in user.

## Alert row (list item, detail base)

```jsonc
{
  "id": "uuid",
  "status": "pending|active|acknowledged|resolved",
  "severity": "critical|warning|info",
  "message": "string",
  "triggered_at": "iso", "acknowledged_at": "iso|null", "resolved_at": "iso|null",
  "rule_id": "uuid|null",          // effective rule (server alerts resolve metadata.rule_id)
  "rule_name": "string|null",
  "device_id", "server_id", "service_check_id", "sensor_id": "uuid|null",
  "device_hostname", "device_ip", "server_hostname", "service_check_name", "sensor_name": "string|null",
  "entity": {
    "kind": "device|server|service_check|sensor|apm|system",
    "id": "uuid|string|null",
    "name": "string",               // never empty
    "subtitle": "string|null",      // IP, 'service check', 'server', synthetic/SLO name…
    "href": "/devices/<id>|/servers/<id>|/services/<id>|/sensors/<id>|/apm/...|null"
  },
  "acknowledged_by": {"id": "uuid", "name": "string"} | null,
  "assigned_to": {"id": "uuid", "name": "string"} | null,
  "assigned_at": "iso|null",
  "closure": {                      // null while open
    "reason": "recovered|manual|suppressed|superseded|administrative|hold_cancelled|dependency|notice",
    "label": "Recovered automatically|Closed by operator|…",
    "by": {"id","name"} | null,
    "note": "string|null"
  } | null,
  "is_notice": false,               // true for recovery notices / dependency-suppressed / hold-cancelled rows
  "notice_kind": "recovery|dependency|hold_cancelled|null",
  "suppression": {                  // the condition currently has an active silence
    "silence_id": "uuid", "until": "iso|null", "forever": bool,
    "reason": "string|null", "by": {"id","name"}|null, "created_at": "iso"
  } | null,
  "snoozed": bool, "snoozed_until": "iso|null",   // legacy mirror of suppression
  "ack_due_at": "iso|null",         // response target deadline (open + unacknowledged only)
  "ack_overdue": bool,
  "channels": [{"id","name","type","enabled"}],  // CURRENT rule routing (policy, not delivery)
  "delivery": {"attempts": n, "accepted": n, "failed": n, "last_at": "iso|null"} | null,
  "metadata": {}                    // raw, for the collapsed "raw metadata" panel only
}
```

Status semantics shown in the UI (workflow and condition are separate):
* `pending` – held for the rule's hold time; not yet announced.
* `active` + `suppression == null` – firing, needs attention.
* `active|acknowledged` + `suppression != null` – **Suppressed, condition still active**. Not resolved.
* `acknowledged` – a person owns it; the condition may still be firing. Severity styling is kept.
* `resolved` – closed; `closure.reason` says how. Only `recovered` is a genuine recovery.

## GET /alerts

Query parameters (all optional):

| Param | Values | Meaning |
|---|---|---|
| `view` | `active` · `suppressed` · `pending` · `acknowledged` · `resolved` · `open` · `all` | Alert Center tabs. `active` = status active AND not suppressed. `suppressed` = open (active/acknowledged) with an active silence. `acknowledged` excludes suppressed. `open` = pending+active+acknowledged. When `view` is absent the legacy `status` param applies exactly as before. |
| `status` | legacy single status | unchanged behaviour |
| `severity` | `critical`, `critical,warning` … | comma list |
| `device_id` `server_id` `service_check_id` `sensor_id` `rule_id` | uuid | entity / rule filter |
| `entity_kind` | `device|server|service_check|sensor|apm|system` | |
| `assignee` | `me` · `unassigned` · uuid | |
| `population` | `incidents` (default) · `notices` · `all` | notices = recovery / dependency / hold-cancelled rows |
| `hours` | 1..8760 | rolling window computed on the **appliance** clock at query time |
| `from`, `to` | iso | explicit custom bounds (take precedence over `hours`) |
| `basis` | `triggered` (default) · `resolved` · `acknowledged` | which timestamp the window filters |
| `age` | `gt1h` · `gt24h` · `gt7d` | open-for longer than (triggered_at older than) |
| `rule_state` | `disabled` · `missing` | backlog-health drill-downs |
| `search` | text | message, device hostname, device IP, service check name, server hostname, sensor name, rule name, alert id (full or ≥8-char prefix). `%`/`_` are literal. |
| `delivery` | `failed` · `none` | incidents with a failed notification attempt / with no recorded attempt |
| `as_of` | iso | paging snapshot: only rows triggered at or before it; `meta.newer` reports later arrivals |
| `sort` | `newest` (default) · `oldest` · `severity` (critical first, then oldest) · `closed` (resolved_at desc) | stable: ties broken by id |
| `skip` | ≥0 | |
| `limit` | 1..200 (default 50) | |

Response: `{ "data": [AlertRow], "meta": { "total", "skip", "limit", "sort", "view", "server_time", "as_of", "newer" } }`.

## GET /alerts/summary

Same filter params as `/alerts` except `view`, `sort`, `skip`, `limit`. Plus
`window_hours` (default: the list's `hours`, else 24) for the closed-incident metrics window
(filtered on `resolved_at`, appliance clock), or `from`/`to`.

```jsonc
{
  "server_time": "iso",
  "filtered": true,                 // any entity/severity/search/assignee filter applied
  "filters": ["Severity: critical", "Device: core-sw-1", …],
  "open": {
    "total": n,                     // pending + active + acknowledged (incl. suppressed)
    "active": n,                    // active, not suppressed
    "acknowledged": n,              // acknowledged, not suppressed
    "suppressed": n, "pending": n,
    "by_severity": {"critical": n, "warning": n, "info": n},   // active+acknowledged, not suppressed
    "unassigned_critical": n,
    "ack_overdue": n,
    "oldest_triggered_at": "iso|null",
    "aging": {"lt_1h": n, "h1_24": n, "d1_7": n, "gt_7d": n}
  },
  "window": {
    "from": "iso", "to": "iso", "basis": "resolved",
    "opened": n,                    // incidents triggered in window
    "closed": n,                    // incidents resolved in window (notices excluded)
    "by_reason": {"recovered": n, "manual": n, "suppressed": n, "superseded": n},
    "notices": n,                   // recovery/dependency/hold-cancelled rows in window (excluded)
    "time_to_recover": {"count": n, "mean_s": x|null, "p50_s": x|null, "p95_s": x|null},  // reason=recovered only
    "time_to_close":   {"count": n, "mean_s", "p50_s", "p95_s"},                          // all closed incidents
    "time_to_ack":     {"count": n, "mean_s", "p50_s", "p95_s"}                           // incidents acknowledged in window
    "clock_anomalies": n,             // closures/acks stamped before the trigger (excluded)
    "delivery": {"attempts", "accepted", "failed", "skipped", "alerts_with_failures"}
  },
  "diagnostics": {                  // backlog health (open incidents only)
    "open_rule_disabled": n, "open_rule_missing": n, "open_older_7d": n, "open_delivery_failed": n
  },
  "response_targets": {"critical": 15, "warning": 60, "info": null}   // minutes to acknowledge
}
```

## GET /alerts/trend

`hours` = the trend window, not a row filter (default 168, max 2160), `bucket` = `hour|day` (default: hour ≤ 48h, else day) +
entity/severity filters. Returns `{server_time, bucket, series: [{t, opened, recovered, closed_other}], top_rules: [{rule_id, name, count}], top_entities: [{kind, id, name, count}]}`.
Incidents only (notices excluded). Buckets with no rows are present with zeros.

## GET /alerts/stats (unchanged shape, corrected meaning)

`{active, acknowledged, resolved_today, critical, warning, info, suppressed, pending, server_time}` —
`active`/`critical`/`warning`/`info` exclude suppressed conditions; `resolved_today` counts incidents
only (no recovery notices), appliance UTC day.

## GET /alerts/{id}

Alert row plus:

```jsonc
{
  "permissions": {"can_triage": bool, "can_manage_rules": bool, "can_suppress_forever": bool, "can_edit_rule": bool},
  "rule": {                         // CURRENT policy, null when no rule
    "id","name","enabled","severity","metric","operator","threshold",
    "conditions": [{"metric","operator","threshold","reset_threshold"}], "condition_logic": "AND|OR",
    "condition_label": "CPU > 90% AND Memory > 80%",
    "trigger_on": "any|down|degraded|up|null", "recovery_alert": bool,
    "min_duration": s, "cooldown": s, "max_repeat": n,
    "schedule": {"start","end","days"} | null,
    "escalation_levels": [{"after_minutes","notify_channels":[ids],"channel_names":[..],"repeat_every_minutes"}],
    "scope": {"kind": "device|group|tag|location|device_type|service_check|service_group|server|interface|apm|global",
              "label": "All devices in group “Core”", "target": "string|null"},
    "affected": {"count": n|null, "noun": "devices|servers|service checks|…", "shared": bool},
    "notify_channels": [ids], "editable": bool   // false when user lacks alerts.manage or scope
  } | null,
  "policy_snapshot": {…} | null,    // rule as it was when this alert triggered (F08); same keys, subset
  "escalation_state": {"level","last_at","repeats","closed"} | null,
  "evidence": [{"label","value"}],  // metric/value/threshold/status transition readings from metadata
  "recurrence": {"same_condition_24h": n, "same_condition_7d": n, "flapping": bool,
                  "previous": [{"id","triggered_at","resolved_at","status"}]},
  "related": [ AlertRow & {"relation": "same_entity|same_rule_window|dependency_parent", "relation_label": "string"} ],
  "related_alerts": [...],          // legacy mirror (same_entity only)
  "events": [{"at","kind","label","actor": {"id","name"}|null,"source": "user|system|evaluator","reason","old_status","new_status","detail":{}}],
  "deliveries": [{"at","channel_id","channel_name","channel_type","kind": "trigger|recovery|escalation|all_clear|reminder|closure","outcome": "accepted|failed|skipped","error": "string|null","latency_ms"}]
}
```

Event kinds: `triggered`, `held`, `confirmed`, `acknowledged`, `assigned`, `unassigned`, `comment`,
`snoozed`, `unsnoozed`, `suppression_expired`, `escalated`, `resolved` (manual), `recovered`
(evaluator), `superseded`, `hold_cancelled`, `closed`.

## Actions

All return **the updated AlertRow** plus `"changed": bool` unless stated. Invalid lifecycle
transitions return **409** `{detail}` without mutating. State change, lifecycle event and audit
record commit in one transaction.

| Endpoint | Body | Rules |
|---|---|---|
| `POST /alerts/{id}/acknowledge` | `{note?}` | from `active`; `acknowledged` → 200 changed=false; `pending`/`resolved` → 409 |
| `POST /alerts/{id}/resolve` | `{reason?}` (≤500) | from pending/active/acknowledged; `resolved` → 200 changed=false. Records `closure.reason=manual`. |
| `POST /alerts/{id}/assign` | `{user_id: uuid|null, note?}` | open alerts only; user must exist and be active |
| `POST /alerts/{id}/comments` | `{text}` (1..2000) | any status |
| `GET /alerts/{id}/snooze-preview` | – | `{condition_label, entity, rule, open_alerts: n, channels, existing: silence|null, can_forever}` |
| `POST /alerts/{id}/snooze` | `{minutes: 5..43200 | null, reason?}` | `null` = indefinite → needs `alerts.manage` + reason (≥3 chars). ≤0 or >43200 → 422. **Does not resolve**: open alerts for the condition stay open and are stamped suppressed. 400 when the alert has no condition identity. |
| `POST /alerts/{id}/unsnooze` | – | removes the condition's silence; `{removed: n}` |
| `POST /alerts/bulk` | `{ids: [≤500], action: acknowledge|resolve|snooze|assign, reason?, minutes?, user_id?}` | `{results: [{id, ok, changed, error}], summary: {ok, changed, failed}}` — each id individually scope- and transition-checked |
| `GET /alerts/export` | list filters + `format=csv` | full matching population (cap 50 000 rows), `Content-Disposition: attachment` |
| `GET /alerts/silences` | `server_id?`, `device_id?`, `include_expired?` | scoped; without an entity returns all visible silences: `{items: [{id, entity: {kind,id,name}, dedupe, condition_label, rule_name, until, forever, reason, created_by: {id,name}|null, created_at, alert_id, open_alerts: n}]}` |
| `DELETE /alerts/silences/{id}` | – | `alerts.acknowledge` + scope; an indefinite silence needs `alerts.manage`; audited |
| `GET /alerts/assignees` | – | `{items: [{id, name, username}]}` active users |
| `GET /alerts/channel-directory` | – | `alerts.manage`: `{data: [{id,name,type,enabled}]}` |
| `GET /alerts/response-targets` / `PUT` | `{critical, warning, info}` minutes or null (PUT: `alerts.manage`) | |

## Alert rules (`/alert-rules`)

* Mutations require `alerts.manage`. A user with a tag scope may only create or change rules whose
  target is inside that scope (a single device/server/service check they can see, or a scope tag in
  their scope). Fleet-wide rules (global, group, location, device type, APM) → **403**
  `"This rule applies fleet-wide; changing it needs unrestricted access."`
* `POST /alert-rules/{id}/toggle` accepts optional `{enabled: bool, reason?: string}`; without a body it flips.
* `GET /alert-rules/{id}/impact` → `{scope, affected: {count, noun, shared}, open_alerts: n, enabled}`.
* Create/update/delete/toggle write audit records with before/after of changed fields.

## Escalation, on-call and grouping (1.24.18)

Reads need `alerts.view`; changes need `alerts.manage`. Preview and dry-run are
POST but read-only. Behaviour is described in `docs/alert-notification-policy.md`.

| Endpoint | Purpose |
|---|---|
| `GET/POST /escalation-policies`, `GET/PUT/DELETE /escalation-policies/{id}` | Reusable policies: `default_priorities`, `ack_target_minutes`, `repeat_minutes`, `max_repeats`, `review_minutes`, `ack_expiry_minutes`, `fallback_targets`, `steps[{position, delay_minutes, name, targets[{type: schedule\|user\|channel, id}]}]`. A second enabled policy claiming the same default priority is refused (409). |
| `POST /escalation-policies/{id}/preview`, `POST /escalation-policies/dry-run` | Who each step would reach (saved policy, or an unsaved draft); never sends. |
| `POST /escalation-policies/recommended` | Creates the four P1–P4 matrix policies (`replace_defaults` to move existing defaults). |
| `GET/POST /oncall/schedules`, `GET/PUT/DELETE /oncall/schedules/{id}` | Schedules with `timezone` and `layers[{name, position, shift_hours, handoff_at, members[], restrict_to[{days, start, end}]}]` (days 0 = Monday). |
| `GET /oncall/schedules/{id}/timeline?from&to`, `/now`, `POST/DELETE /oncall/schedules/{id}/overrides[/{override_id}]` | Shifts and gaps; who is on call now; time-boxed overrides. |
| `GET /oncall/coverage` | Unrouted priorities, schedule gaps, people without contact methods, missing direct-page gateways. |
| `GET/PUT /users/me/contact-methods`, `GET/PUT /users/{id}/contact-methods` | Email/SMS contact methods and `timezone`. |
| `GET/PUT /alerts/response-targets` | Acknowledgement minutes keyed `P1`–`P4` (legacy severity keys still accepted and mirrored). |
| `GET /alerts/{id}/route` | The incident's route: policy and source (`rule`, `legacy_levels`, `priority_default`, `none`), current step, next action and time, recipients per step. |
| `GET/PUT /alerts/notification-grouping` | Grouping settings (`enabled`, `group_by`, `group_wait_seconds`, `group_interval_seconds`, `max_items`, `budget_per_recipient_per_hour`, `critical_reserve`) plus `recommended`. |
| `GET /alerts/digests?status&limit`, `GET /alerts/digests/{id}`, `POST /alerts/digests/{id}/release` | Held summaries with reason, release condition and child incidents; release now. |

Alert rows and details gain `priority` and `priority_label`; details gain
`notification_holds`. Rules accept `priority` (`P1`–`P4` or null for "from
severity") and `escalation_policy_id`.
