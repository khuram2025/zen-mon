# Multi-condition alert rules

One rule can watch several conditions and raise one incident when they are
true together (AND) or when any of them is (OR). Network-device rules have
combined conditions since migrate-021; **phase 1 (migrate-211) adds them to
server rules**. The full design, with the later phases and the decisions, is
in [plans/multi-condition-rules-plan.md](plans/multi-condition-rules-plan.md).

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
- **What can be combined.** CPU, memory, disk utilisation, swap, clock offset,
  filesystem %, days until full, disk latency, NIC errors (levels), and
  service stopped, process not running, automatic service stopped (states).
  Restart, shutdown and agent offline stand alone (events / heartbeat).
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

## How it is judged (`host_alert_service._evaluate_tree`)

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
- One incident per rule + server (`dedupe` `rule:<id>`). The breach hold
  (`min_duration`) counts from the first pass the tree was true; once open,
  every condition is judged at its clear level and the incident resolves
  after the tree has been false for `clear_duration` (NULL = 300 s). Holds are
  kept in `service_metric_alert_state` (`host:<server>:#tree`); an unknown pass
  restarts them.
- Maintenance, exclusions, tag/group scope, dependencies (`suppressed_by_dependency`),
  cooldown, schedule, priority, escalation and recovery notices work exactly
  as for single server rules.
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

Server source, Trigger step: **Add condition** turns the rule into a combined
rule (`ServerConditionTree.tsx`): an All/Any switch, conditions, *Add group*
(one level), *Use one condition* (back to the first), and a read-back
sentence. Lists show combined rules as "Combined conditions" with the tree in
words.
