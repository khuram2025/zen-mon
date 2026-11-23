# Multi-condition alert rules

One rule can watch several conditions and raise one incident when they are
true together (AND) or when any of them is (OR). Network-device SNMP rules
have flat combined conditions since migrate-021. Condition trees add groups,
states, linked assets and per-condition holds: **phase 1 (migrate-211)** for
server rules, **phase 2 (migrate-212)** for devices and service checks. The
full design, with the later phases and the decisions, is in
[plans/multi-condition-rules-plan.md](plans/multi-condition-rules-plan.md).

| Subject | Saved through | Stored metric | Evaluated by |
| --- | --- | --- | --- |
| Server | `/host-alert-rules` | its first condition on the server (`host_*`) | `host_alert_service._evaluate_tree` |
| Device | `/alert-rules` (`subject_type: device`) | `combined` | `combined_rule_service` |
| Service check | `/alert-rules` (`subject_type: service_check`) | `combined` | `combined_rule_service` |

`metric = 'combined'` (allowed by migrate-212) means no single-condition
evaluator ever loads the rule; the device and service status paths of
`alert_engine` exclude it explicitly.

## What a combined server rule is

`alert_rules.condition_tree` (JSONB) holds the conditions:

```json
{"logic": "OR", "items": [
  {"logic": "AND", "items": [
    {"id": "c1", "metric": "host_cpu_pct", "operator": ">", "threshold": 90},
    {"id": "c2", "metric": "host_memory_pct", "operator": ">", "threshold": 90}]},
  {"id": "c3", "metric": "host_service_down", "operator": ">", "threshold": 0, "target": "nginx"}
]}
```

- **Shape.** A root group (`AND` = all of, `OR` = any of) with conditions and
  at most one level of sub-groups; 2 to 10 conditions in all; a sub-group
  holds at least two. `app.services.rule_conditions.validate_tree` is the one
  validator (API and evaluator); it assigns condition ids (`c1`, `c2`, ...).
- **What can be combined** (`rule_conditions.SUBJECT_METRICS`):
  - *Server*: CPU, memory, disk utilisation, swap, clock offset, filesystem %,
    days until full, disk latency, NIC errors (levels); service stopped,
    process not running, automatic service stopped (states). Restart,
    shutdown and agent offline stand alone (events / heartbeat).
  - *Device*: down, degraded (states); latency (last ping), CPU and memory
    (newest SNMP sample), minutes SNMP has gone unanswered while the device
    answers ping (levels).
  - *Service check*: down, degraded (states); response time of the last run,
    certificate days left (levels). A paused, stale or never-run check is
    unknown.
- **Linked assets** (`on`): a server's device (`servers.device_id` or the same
  address), a device's server (the reverse), a service check's device
  (`service_checks.device_id`) and server (`service_checks.server_id`,
  migrate-212, else the server linked to its device). A linked asset that is
  missing or in maintenance reads as unknown, never as a breach. A server
  rule needs at least one condition on the server itself.
- **Per-condition hold** (`for_s`, 0-86400 s): the condition counts as true only
  after it has been true for that long; the rule's `min_duration` still holds
  the whole tree. Its start times are kept in `combined_rule_state.leaf_since`.
- **Instances.** `target` names one mount, disk, NIC, service or process; a
  filesystem, disk or NIC condition without one is true when any instance is.
  For the automatic-services condition `target` lists names to leave out, as
  on a single rule. Service and process conditions require a name.
- **Clear levels.** Each level condition may carry `reset_threshold`; without
  one, percent metrics clear 2 points on the healthy side and days-until-full
  one day above, as for single rules.
- **Flat columns.** `metric`/`operator`/`threshold` and `conditions` mirror the
  first condition (`rule_conditions.mirror`), so lists, exports and older
  pages keep working; `target` is NULL; `subject_type` is `server`.

## How it is judged (`host_alert_service._evaluate_tree`, `combined_rule_service`)

- Once a pass (60 s), each condition is read for each server: the newest
  sample if it is within the agent's telemetry freshness (no rolling average:
  combined rules hold every sample), the service and process inventories, the
  automatic-service and filesystem-forecast snapshots. Each source is read once
  per pass for all servers; a source that cannot be read leaves every
  incident as it is.
- Three-valued logic (`rule_conditions.judge`, `network_conditions.combine`):
  AND is false if any condition is false, unknown if any is unknown, else
  true; OR mirrors it. An unknown result never raises or resolves.
- Nothing to watch (no automatic service, the named service not installed)
  is false, not unknown; a server without samples for a metric is unknown.
- One incident per rule + subject: a server incident keeps `dedupe` `rule:<id>`;
  a device or service-check incident carries `source: combined_rule` and
  `subject_key` (`device:<id>`, `check:<id>`) and its `device_id` /
  `service_check_id`. The breach hold (`min_duration`) counts from the first
  pass the tree was true; once open, every condition is judged at its clear
  level and the incident resolves after the tree has been false for
  `clear_duration` (NULL = 300 s). Holds live in `combined_rule_state`
  (`server:<id>`, `device:<id>`, `check:<id>`, migrate-212); an unknown pass
  restarts them.
- Default clear levels: percent metrics 2 points on the healthy side, days
  (until full, certificate) one day above, response time 10 % under the
  limit, states when the state ends.
- Servers: maintenance, exclusions, tag/group scope, dependencies
  (`suppressed_by_dependency`), cooldown, schedule, priority, escalation and
  recovery notices work exactly as for single server rules.
- Devices (`combined_rule_service`): the device rule scope (device, group,
  type, location, tag, template); a device in maintenance is not judged; a
  rule excluded for a device, or a device that left the scope, has its open
  incident resolved silently; behind an unhealthy dependency parent the
  device is not judged and its incident is stamped
  `suppressed_by_dependency`; the device's snooze (`alert_silences`, dedupe
  `rule:<id>`) holds a new incident.
- Service checks: scoped by check, check group or tag; a check (or its
  device) in maintenance is not judged. The pass runs once a minute in one
  worker (`COMBINED_RULE_PASS`), each raise under a rule + subject lock; every
  send goes through `dispatch_to_channels` with the alert id and delivery event,
  and the rule's own message templates apply.
- Overrides: offered when every condition is a level (as for single rules,
  `alert_rules._overridable`). An override row's threshold / clear level
  applies to the condition with its `metric` (the first condition when it
  names none); holds and priority apply to the rule.

## What is sent

The incident metadata records `combined: true`, `matched` (condition id,
metric, instance, value) and `condition_states`. The event sentence names the
matched conditions with their readings:

> Server web-01 (10.0.0.5): host CPU 94% (limit 90%) and host memory 92% (limit 90%).

The recovery names every condition's reading ("... is back within the rule's
limits: host CPU 30% (limit 90%), ..."). `{condition_label}` is the tree in
words ("(Host CPU above 90% and host memory above 90%) or service nginx
stopped") everywhere a rule's condition is shown (escalation, incident
detail); `{matched_conditions}` is a template variable.

## API

`/alert-rules` accepts `condition_tree` with `subject_type` `device` or
`service_check` (the stored metric is `combined`); scope fields of the other
subject are refused, a combined rule stays combined (`condition_tree: null`
is a 422) and keeps its subject. Rows return `condition_tree` and
`subject_type`. Service checks accept an optional `server_id`.

`/host-alert-rules` accepts `condition_tree` on create and update (then
`metric`, `operator`, `threshold`, `target` and `reset_threshold` are ignored;
a `reset_threshold` beside a tree is a 422). `condition_tree: null` on an
update turns a combined rule back into a single rule on its first condition.
Changing `metric`/`operator`/`threshold`/`target` of a combined rule without a
tree is a 422. Rows return `condition_tree` and `condition_label`.

Sources whose evaluators judge only the first condition now refuse a second
one on `/alert-rules` (422): APM and RUM, Device Tracker, NetPath and service
status. Rules stored with several conditions before this check stay editable.

## Editor

The builder (`ServerConditionTree.tsx`, model `conditionTree.ts`): an All/Any
switch, conditions with *Reads* (the subject or a linked asset), *For* (the
condition's own hold), *Add group* (one level) and a read-back sentence.
Server source: **Add condition** turns a rule into a combined rule and *Use
one condition* turns it back. Device and Service Check sources: **Combine
conditions** on a new rule. Lists show combined rules as "Combined
conditions" with the tree in words.
