# Discovery rules (phases 1 to 3)

Discovery rules classify what a discovery scan finds: auto-import it with the
right group, template, tags and polling settings, keep it for review, or
ignore it. They live under **Discovery → Rules** and are off until an
administrator turns the engine on.

The research and the four-phase plan are in
`docs/plans/discovery-rule-engine-plan.md`. Phase 1 (1.25.11) acts on scan
results. Phase 2 also applies rules to devices and servers already in
inventory: through a previewed manual run, and to every device or server when
it is added (see "Rules on inventory"). Phase 3 keeps assets in line when they
or the rules change, reverts on un-match, holds large automatic changes for
approval, covers controller-managed children, adds map and dependency
actions, and exports/imports rules (see "Continuous rules").

## Rules

A rule is global (not owned by a discovery profile) and has:

* **When**: the asset kinds it applies to (network devices, servers) and,
  optionally, the discovery profiles it is limited to. A scan result counts as
  a server when it looks like one (Windows/Linux/ESXi OS or type `server`).
* **If**: a condition tree. Groups are *All of* (AND), *Any of* (OR) and
  *None of* (NOT), nested at most three levels, at most 50 conditions. An empty
  *All of* matches everything (a catch-all rule).
* **Then**: actions (below).
* **Stop processing**: a matching rule ends the evaluation.
* **Runs automatically on** (`triggers`): discovery scan results
  (`discovery`), devices and servers when they are added (`created`), and/or
  whenever they change or the rule changes (`changed`). A manual run on
  inventory can use any active rule.
* **When an asset stops matching** (`on_unmatch`): *Keep* (default) leaves the
  values the rule set; *Revert* restores the value it replaced, if the value
  is still what the rule set. Applies to the `changed` pass and manual runs;
  disabling or deleting a rule keeps its values.
* **When a value was changed by hand** (`enforcement`): *Set once* (default)
  leaves a later hand edit alone; *Enforce* puts the rule's value back.
* **Mode**: Draft (never evaluated), Simulating (evaluated on scans and on new
  devices, records what it would do as `simulated` run items, changes
  nothing), Active, Disabled.

### Facts and operators

| Type | Facts | Operators |
| --- | --- | --- |
| text | hostname, FQDN, sysName, sysDescr, sysLocation, vendor, model, device type, OS, serial, MAC | equals, does not equal, contains, does not contain, starts with, ends with, matches regex, is one of, is empty, is not empty |
| OID | sysObjectID | equals, is under (subtree), is not under, is one of, matches regex, is empty |
| version | OS version | equals, contains, starts with, matches regex, older than / at most / newer than / at least |
| IP | IP address | in subnet(s), not in subnet(s), in range, equals, is one of |
| number | identification confidence | =, ≠, <, ≤, >, ≥, between |
| set | open TCP ports, protocols detected | has any of, has all of, has none of, is empty |
| enum | credential status, result status | is, is not, is any of |
| reference | SNMP credential that worked, discovery profile | is, is not, is any of, is empty |
| boolean | server-class host | is true, is false |

* Text comparisons ignore case. OIDs are normalised (`iso.3…`, `.1.3…` and
  `1.3…` are the same) and "is under" is a subtree test: `1.3.6.1.4.1.12356`
  matches `…12356.101.1` but not `…123567`.
* Versions compare the first dotted number numerically (`v7.2.10,build1706`
  is 7.2.10, newer than 7.2.9; 7.2 equals 7.2.0).
* **Missing facts** never satisfy a positive test. Negative operators are true
  for a missing value: an unknown vendor "does not equal Fortinet". Use
  *is empty* / *is not empty* to test for the missing value itself. Preview
  shows the effect before a rule is active.
* **Regular expressions** are validated when the rule is saved: at most 200
  characters, no nested quantifiers such as `(a+)+` and no back-references,
  because Python's `re` has no time limit. They run against at most 2,048
  characters of the fact.

### Actions

| Action | Kind | Permission to activate |
| --- | --- | --- |
| Import decision: Auto-import (as device, server, both or auto), Keep for review, Ignore | single | `discovery.run` (+ `servers.manage` when it can create servers) |
| Device group, monitoring template, device type, location, SNMP credential (by id), ping interval, SNMP poll interval | single | `devices.manage` |
| Owner | single | `devices.manage` / `servers.manage` |
| Server environment (servers) | single | `servers.manage` |
| Turn ping monitoring on (only ever on) | single | `devices.manage` |
| Add tags (devices, servers, children) | additive | `devices.manage` / `servers.manage` |
| Add to network map (placed on the bottom row to be arranged; never removes an operator's node) | additive | `maps.manage` |
| Depends on (uplink parent; adds a `topology_dependencies` row, alerts suppressed while the parent is down) | single, added only | `devices.manage` |

The permission follows the asset kinds the rule applies to: device settings
need `devices.manage`, server settings `servers.manage`.

Group, template and SNMP credential are stored as ids and checked when the
rule is saved; no secret is ever copied into a rule, a run item or an export.
Tags are stored in the tag registry's spelling (`tag_service.canonicalize_tags`).

### Order and conflicts

Rules are evaluated in priority order (top of the list first; Move up / Move
down reorders). For each single-valued action the **first matching rule wins**;
a later matching rule that sets the same action is recorded as *shadowed*. Tags
from every matching rule are merged. A matching rule with *Stop processing*
ends the evaluation. Priorities are unique, so there are no ties.

The import decision follows the same first-match rule, so the usual pattern
works: specific *Auto-import* rules at the top, a catch-all *Ignore* at the
bottom. (The plan's "ignore beats import" was dropped for this reason.)

## What happens to a scan result

At the end of every scan (and when an operator re-evaluates a past scan) the
active rules are evaluated for each result that answered and is not yet
imported or ignored. The plan is stored on the result (`rule_plan`,
`disposition`, `matched_rule_ids`) and shown in the Rule and Decision columns
of the scan results, with the reason.

The profile's **import mode** decides what the rules may do:

| Profile import mode | Auto-import | Ignore |
| --- | --- | --- |
| `review` (default) | shown as "would import" | shown as "would ignore" |
| `auto_match` | done | done |
| `ignore_match` | shown as "would import" | done |

Dispositions: `imported`, `ignored`, `review` (a rule kept it, or reasons
`not_ready`, `cap`, `license_capacity_reached`, `conflict`, `error`),
`would_import` / `would_ignore` (profile does not allow it), `existing`
(already monitored: annotated, never changed in phase 1), `no_match`.

* **Auto-import** goes through the same code as the Import dialog
  (`app.services.discovery_import`), under the `discovery-import` lock and the
  license budget (`license_capacity.reserve`, see `docs/license-capacity.md`).
  Only results that are import-ready (new, credentials valid or not tested)
  are auto-imported. At most `max_auto_import_per_run` per scan;
  `license_reserve_nodes` nodes are always left free. What does not fit stays
  in review with the reason and the rules run is `partial`.
* **Ignore** adds the address to the Ignored list with the reason
  "Discovery rule: <name>"; later scans skip it like a manual ignore.
* **Settings** for an imported device: the Import dialog's explicit choices,
  then the rule values, then the profile defaults. The result's suggested
  group, template and tags are the rule value else the profile default (tags:
  profile defaults plus rule tags), so a later manual import uses them too.

A failure inside the engine is written to the scan's activity log and never
fails the scan.

**Re-evaluate rules** on a finished scan (Scan Results, `discovery.run`)
evaluates the current active rules again on the results not yet imported or
ignored: *Annotate only* refreshes the Rule and Decision columns, *Apply* also
acts as the profile allows. Apply is how results held back by the per-scan
limit or the license capacity are retried once room is made.

## Preview

The rule editor previews an unsaved rule against any past scan, either among
the active rules (what a scan would really do) or alone, and can pretend the
profile were `auto_match`. It shows the counts ("would import 17, ignore 3,
review 22; license room 12"), the per-result decision and settings and a "why"
trace for each rule. Sample facts can be typed in to test one device. Preview
and the real evaluation call the same functions (`engine.evaluate`,
`engine.disposition`, `runner.assign_capacity`), and a test compares them.

## Rules on inventory (phase 2)

The same evaluation (`engine.evaluate`) decides what the rules want for a
device or server already in inventory; `discovery_rules.inventory.plan_changes`
turns that into changes, using the asset's current values and the
**assignments** rules made before (`discovery_engine_assignments`: every value
a rule set, with the value it replaced).

| Situation | Result |
| --- | --- |
| No rule has set it, current value differs | changed; the assignment records the value replaced |
| A rule set it and it still has that value | left alone; follows the rule if the rule's value changed |
| A rule set it, then someone changed it by hand | *Set once*: kept, reported as `kept_manual_edit`; *Enforce*: the rule's value is put back |
| Tags | only ever added; a rule-added tag removed by hand is a hand edit like the above |
| SNMP credential on a device with its own (inline) SNMP settings | not changed (`not_reversible`); a rule only switches between saved credentials or fills an empty one |
| Import decision | ignored on inventory |

Inventory facts (the rule builder shows which facts apply to scan results,
devices or servers): devices add tags, group, template, location, owner,
status, ping/SNMP on; servers add OS type, environment, collection mode,
owner, tags, status. Controller-managed children are not evaluated (phase 3).

**Manual run** (Discovery → Rules → Run on inventory, or Devices → select →
Run discovery rules, or the device page): choose the rules (all active, or
some) and the devices/servers (kinds, groups, tags, subnet, name or the
selected ones), preview, apply. The preview returns a digest of exactly those
changes; apply recomputes them under a lock and refuses (409
`preview_stale`) when anything changed since, so what is applied is what was
previewed. Re-checked per value: a value changed between the plan and the
write is skipped (`skipped_changed`). A second run of the same rules changes
nothing.

**When added**: a once-a-minute sweep (one API worker at a time,
`zenplus:discovery_rules_pass`) applies the active rules with the "devices
and servers when they are added" trigger to every device and server created
since the upgrade, once each (`discovery_engine_asset_state`). It does not
touch the add itself, so adding a device never waits for, or fails because of,
the rules; the rules apply within about a minute. While the engine is off new
assets are only marked as seen: turning the engine on never reaches back.

**Undo** (run history): restores every value the run set that still equals
what it set, skips values changed since and lists both; the assignment is
removed (or set back). An undo is itself a run (`trigger = undo`); a run is
undone once. For a scan's run, undo reverts nothing and lists the devices the
scan imported so they can be deleted deliberately: undo never deletes a
device.

**Explain** (device page → Automation): which rule set each value, in which
run, hand edits the rules kept, rules that match now and what a run would
still change.

**Conflicts** (Rules page): over the current inventory, which rule's value is
overridden by which higher rule on how many assets, rules that match nothing,
rules that match but never win.

## Continuous rules (phase 3)

**Re-evaluation on change.** Rules with the `changed` trigger run in the same
once-a-minute pass as the "when added" sweep (`zenplus:discovery_rules_pass`,
one API worker at a time; `discovery_rules.reconcile`). The pass fingerprints
every device, server and child in SQL (md5 of the facts rules test, plus map
placements and dependency parents; **status is left out**, so up/down flips
never re-evaluate) and evaluates only assets whose fingerprint or whose rules
revision changed since `discovery_engine_asset_state` recorded them, in
batches of 500. Changes go through the same planner and apply path as manual
runs (set once / enforce, tags only added, re-checked value by value).

**Revert on un-match** (see the rule options above) runs here and in manual
runs; preview shows reverts as their own outcome.

**Change cap with approval.** When a pass would change more values than
`max_changes_per_run` (engine settings, default 100) it applies nothing: the
whole planned set is stored on a run with status `needs_approval` and the
Rules page shows it for review. **Approve** applies it (each value re-checked);
**Reject** discards it and marks those assets evaluated, so it is not proposed
again until an asset or a rule changes. While a run waits the pass does
nothing; if the rules change meanwhile the waiting plan is set aside
(`rejected`, superseded) and planned again. The cap does not apply to manual
runs (already previewed) or to the "when added" sweep.

**Controller-managed children** (FortiSwitch, FortiAP read through a
FortiGate) are their own asset kind, `child`. They have the facts controller
and managed-through besides the device facts, and take only organising
actions: group, location, owner, tags, map. Polling, credential, template and
interval actions are refused for them when a rule is saved; they are never
polled, pinged or licensed because of a rule.

**Export / import.** `GET /export` writes every rule with references by name
(device group, template, SNMP credential, discovery profile, map, parent
device): no ids and no secrets. `POST /import` creates the rules as Drafts at
the end of the order; a rule whose name does not resolve on this appliance is
skipped and reported (several templates with one name: the newest version is
used and noted).

**Starter rules.** `GET /starter` lists them; `POST /starter` adds the chosen
ones as Drafts, once each: Fortinet (FortiGate as firewall), Cisco, F5 (load
balancer), MikroTik (router), Aruba (wireless controller), Juniper, each by
its sysObjectID subtree, tagging and keeping for review; plus two ignore
examples (printers, IP phones). Nothing is created at upgrade.

**Policies.** NCCM enrolment, Device Tracker, maintenance windows, alert rule
exclusions and notification routing for a class of devices are not rule
actions: they are sections of the monitoring policies planned in
`docs/plans/device-templates-plan.md` (phase 4), reached from a rule through
an "apply policy" action, so one thing never has two owners.

## Engine settings

`system_settings['discovery_rules']`, edited on the Rules page:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Scans evaluate rules only while on |
| `max_auto_import_per_run` | 50 | Auto-imports per scan or re-evaluation |
| `license_reserve_nodes` | 0 | Nodes always left free for manual adds |
| `history_days` | 180 | Rule-run history retention (minimum 7) |
| `max_changes_per_run` | 100 | An automatic `changed` pass that would change more values waits for approval |

## Permissions and scope

* `discovery.view`: see rules, the catalog, previews and history.
* `discovery.manage_rules` (new; administrators and the built-in Network Team
  role; built-in definition version 7): create, edit, order, change mode,
  engine settings. Activating a rule also needs every permission its actions
  use; `activated_by` is stored.
* `discovery.run`: re-evaluate a past scan (it can import).
* Rules are fleet-wide: a user whose view is limited by tags cannot change
  or run them (403), the stance fleet-wide alert rules take. Run history
  shown to a scoped viewer leaves out items about devices and servers outside
  the viewer's tags (scan results are not tag-scoped). The device/server
  explain view follows the asset's visibility.
* Running rules on inventory, and undoing a run, need `discovery.manage_rules`
  plus the permissions of the values written (`devices.manage`,
  `servers.manage`).
* A subnet-to-tag rule is how a newly imported device becomes visible to the
  right team: untagged devices stay hidden from scoped users.

Every rule change, mode change, reorder, settings change and re-evaluation is
written to the audit log (`discovery.rules.*`).

## API

Under `/api/v1/discovery-v2/rules`:

| Endpoint | Use |
| --- | --- |
| `GET /catalog` | Fields, operators, actions; the builder is drawn from it |
| `GET/PUT /settings` | Engine settings |
| `GET ""`, `POST ""` | List (priority order), create (`position` first/last) |
| `GET/PUT/DELETE /{id}` | One rule; `PUT` takes `expected_revision` (409 when stale) |
| `POST /{id}/mode`, `POST /{id}/clone`, `GET /{id}/versions` | Mode, draft copy, version history |
| `POST /reorder` | New order (every rule once) |
| `POST /preview` | Plans for an unsaved rule against a past scan or sample facts; writes nothing |
| `POST /evaluate-scan/{run_id}` | Re-evaluate a finished scan; `apply` acts as the profile allows |
| `GET /history`, `GET /history/{id}/items` | Rule runs and what each rule did |
| `POST /inventory/preview`, `POST /inventory/apply` | Manual run on devices/servers: preview with a digest, apply exactly that |
| `POST /history/{id}/undo` | Undo a run |
| `GET /assets/{device\|server}/{id}` | Explain one asset |
| `GET /conflicts` | Overridden rules, rules that match nothing or never win |
| `GET /history/{id}/pending`, `POST /history/{id}/approve`, `POST /history/{id}/reject` | Review a run held by the change cap |
| `GET /export`, `POST /import` | Rules as JSON with references by name |
| `GET /starter`, `POST /starter` | Starter rules, added as Drafts |

## Data

Migration 190: `discovery_engine_rules`, `discovery_engine_rule_versions`,
`discovery_engine_runs`, `discovery_engine_run_items`, three columns on
`discovery_results_v2`, the settings row. The legacy `discovery_rules` table
(migration 017) was never written by any code path and is left in place,
superseded.

Migration 191: `discovery_engine_assignments`, `discovery_engine_asset_state`,
`targets` / `preview_digest` / `undo_of` on runs, more run-item outcomes,
`system_settings['discovery_rules_state']` (`created_since`, the upgrade
time). Imported and rule-set templates and device types are recorded in
`devices.profile_source` / `device_type_source` as `rule`. `on_unmatch` and
`max_changes_per_run` (per rule) are still unused.

Migration 194: `facts_hash` on `discovery_engine_asset_state`, `plan` /
`approved_by` / `approved_at` on runs, status `rejected`, asset kind `child`.

## Not yet built

A service check created from a template (deferred: creating monitored checks
automatically needs its own review); a per-rule change cap and a match-growth
breaker; the "apply policy" action (with the Device Templates plan's policy
bundles); interface, topology, sensor/site and server-policy rules (phase 4).

## Tests

* `server/tests/test_discovery_rules_inventory.py`: the inventory change
  planner (new values, hand edits under set-once and enforce, tags, inline
  SNMP, servers, first match, digest) and inventory facts.
* `server/tests/test_discovery_rules_reconcile_pg.py` (disposable PostgreSQL):
  the phase 3 acceptance (firmware change re-evaluates only that device and
  status flips never do, revert on un-match, children organised and never
  licensed, change cap with approve/reject, two workers apply once, maps and
  dependencies with undo, export/import and starter rules, device type
  catalogue, migrate-194 re-applied over existing data, child explain).
* `server/tests/test_discovery_rules_inventory_pg.py` (disposable PostgreSQL):
  the phase 2 acceptance (300 devices preview = apply, second run changes
  nothing, stale preview refused, hand edits, undo, "when added" sweep once
  and engine-off marking, Simulating rules write nothing, explain, servers,
  permissions and tag scope, conflicts, scan undo lists imports).

* `server/tests/test_discovery_rules_engine.py`: operators, groups, order,
  shadowing, merge, stop, dispositions, capacity assignment, 5,000 assets x
  100 rules in under 2 s, and the contract vectors in
  `scripts/dev/discovery-rules-contract/vectors.json`.
* `server/tests/test_discovery_rules_pg.py` (disposable PostgreSQL): the phase
  1 acceptance (auto-import with rule settings, review profile, preview equals
  apply, license capacity and reserve, ignore list, existing devices
  untouched, CRUD/order/versions/audit, permissions and tag scope).
