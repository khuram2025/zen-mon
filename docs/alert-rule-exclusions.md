# Turning a shared alert rule off for one device or server

Migration 187 (`alert_rule_exclusions`), service `app.services.alert_rule_exclusions`.

## The problem

A rule that applies to many assets — "Server restart" on every server,
"Device Down" on every device, a rule scoped to a group, type, location or
tag — could be switched off from a server's Alerts tab, but the switch called
`POST /host-alert-rules/{id}/toggle`, which flips `alert_rules.enabled`: the
rule stopped for **every** server.

## The model

A **shared rule** is any rule that is not pinned to the asset itself
(`device_id` / `server_id` of the rule is not this asset). An **exclusion** is
one row `(rule, device)` or `(rule, server)` meaning "this rule does not
evaluate this asset". The rule itself stays enabled and keeps evaluating
every other asset it applies to.

| Rule family | Can be excluded for |
| --- | --- |
| Device rules: status (Device Down, degraded, any change), SNMP/template metrics, interface rules, traps, syslog | devices (controller-managed FortiSwitch/FortiAP children are devices) |
| Device Tracker (`udt_*`) | the reporting switch (a device) |
| Server rules (`host_*`: CPU, memory, filesystem, disk, service/process down, agent offline, restart, shutdown) | servers |
| An unscoped Device Down rule | devices **and** agent servers (it pages for both) |
| Service checks, APM, NetFlow, path probes | not per device/server — no exclusions |

A rule pinned to the asset (its own rule) is enabled, disabled or deleted as
before. An exclusion is refused (422) for a rule that does not currently apply
to the asset.

## Where it is evaluated

Every evaluator loads all exclusions once per pass (`rx.load`, a savepoint so a
missing table before the migration never aborts the pass) and skips excluded
pairs:

* `network_alert_service.evaluate_network_rules` — SNMP/template metric rules,
  including controller-managed children;
* `alert_engine.evaluate_status_change` — device status transitions;
  `device_flap_notice_service` — flap notices; `alert_engine.evaluate_trap` —
  SNMP traps from a known device;
* `network_events` — syslog rules;
* `udt_alert_service` — Device Tracker events and port capacity;
* `host_alert_service.evaluate_host_rules` — every server rule, including the
  Device Down rule's server availability coverage.

An excluded availability rule still counts as covering the server for the
staleness sweep (`server_availability.covered`), so excluding "Device Down" or
"Agent offline" for a server does not make the built-in, unrouted *agent
offline* alert appear in its place.

## Open incidents

Excluding resolves the rule's open (`pending`, `active`, `acknowledged`)
incidents on that asset immediately: `status = resolved`,
`metadata.resolved_by = 'rule_excluded'` with who did it, an alert event
(`resolved`, source `rule_exclusion`) and **no recovery notification** — the
condition did not recover, and nobody should be told it did. Their
escalations stop with them.

Re-enabling deletes the row. Evaluation resumes on the next pass (a status
rule on the next status transition); a condition still breaching opens a new
incident after the rule's usual hold. Restart/shutdown events already
announced are not re-announced.

An evaluator pass that started just before the exclusion was saved uses the
exclusions it loaded at its start, so at most that one pass can still raise an
incident for the pair.

## Who

* Listing (`GET /alert-rules/{id}/exclusions`, `GET /alert-rules/for-asset`):
  `alerts.view`. Assets outside the caller's visibility scope are left out and
  counted in `hidden`.
* Changing (`POST /alert-rules/{id}/exclusions`,
  `DELETE /alert-rules/{id}/exclusions/{device|server}/{asset_id}`):
  `alerts.manage` **and** the asset inside the caller's visibility scope (404
  otherwise). A tag-scoped manager may turn a fleet rule off for an asset they
  can see — that narrows alerting only for what they own — while changing the
  rule itself still needs the rule in scope (`guard_rule_change`).

Each change is audited (`alert_rule.exclude` / `alert_rule.include`, with the
asset, reason and resolved alert count).

## API

* `GET /alert-rules/for-asset?asset_kind=device|server&asset_id=…` — the rules
  that apply to the asset (enabled or not): `shared`, `excluded` (or null) and
  `active` (on globally and not excluded here).
* `GET /alert-rules/{id}/exclusions` — `{kinds, items, hidden}`; each item has
  the asset kind, id, name, IP, `asset_path`, reason, `created_by_name` and
  `created_at`.
* `POST /alert-rules/{id}/exclusions` `{asset_kind, asset_id, reason?}` —
  idempotent; returns `{exclusion, created, resolved_alerts}`.
* `DELETE /alert-rules/{id}/exclusions/{asset_kind}/{asset_id}` —
  `{removed}`.
* `GET /alert-rules` adds `exclusion_count`; `GET /alert-rules/{id}/impact`
  adds `excluded_assets`; `GET /host-alert-rules?server_id=…` adds `excluded`
  and `active` per rule.

## UI

* Server → Alerts tab: a global rule's switch turns it off/on **for this server
  only** (with a confirmation and optional reason); the row says who disabled it
  and when. A rule that is off globally shows locked off.
* Device → Configuration tab: an *Alert rules* panel lists the rules that apply
  to the device with the same per-device switch.
* Alert detail → Policy: *Off for this device/server* next to the global
  *Disable for all*.
* Alert Rules: a shared rule disabled for some assets shows *Off for N*, which
  opens the list (linked asset, who, when, reason) with *Re-enable*.
